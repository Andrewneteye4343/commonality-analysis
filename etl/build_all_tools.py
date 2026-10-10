#!/usr/bin/env python3
"""M3：一次載入多台機台的 genealogy（供跨機台共同性分析）。

為什麼需要這支
────────────────────────────────────────────────────────────
`build_genealogy.py` 一次處理一個檔案，而且會 TRUNCATE（單機台用）。
要跑跨機台的 tool commonality，就必須：
1. **事件 ID 不能撞號**：每台機台的事件 ID 從 1 開始，直接 append 會互撞
   → 本程式在掃描時給每台機台一個位移（offset = 目前已寫入的最大 event_id）
2. **維度表要能合併**：不同機台會有相同 recipe 編號、相同批次（同一批被兩台機台加工過）
   → 維度表走「暫存表 + ON CONFLICT」；批次統計量用**累加**而非覆蓋

用法
────────────────────────────────────────────────────────────
    python build_all_tools.py                       # 掃描 /data/raw 全部 *_DC_score.csv
    python build_all_tools.py --tools 01,03,04      # 只跑指定機台
    python build_all_tools.py --dry-run             # 只掃描與統計，不寫資料庫
"""
from __future__ import annotations

import argparse
import sys
import json
import os
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from script_version import add_version_arg

from build_genealogy import copy_to_db, preflight_schema, run
from download_data import parse_tools


def db_loaded_tools() -> set[str]:
    """已載入的機台（冪等用：避免重跑時把同一台機台載入兩次）"""
    import psycopg
    conn = psycopg.connect(
        host=os.environ.get("PGHOST", "db"), port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""),
        dbname=os.environ.get("PGDATABASE", "commonality"))
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT tool_id FROM stg.fact_process_event")
        tools = {r[0] for r in cur.fetchall()}
    conn.close()
    return tools


def truncate_all() -> None:
    """--reset：清空事實／維度表與 qa 檢查表，讓多機台以乾淨狀態重新載入"""
    import psycopg
    conn = psycopg.connect(
        host=os.environ.get("PGHOST", "db"), port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""),
        dbname=os.environ.get("PGDATABASE", "commonality"))
    with conn.cursor() as cur:
        for t in ("stg.fact_sensor_feature", "stg.fact_sensor_trace", "stg.fact_process_event",
                  "stg.dim_step", "stg.dim_recipe", "stg.dim_lot", "stg.dim_tool",
                  "qa.segmentation_sensitivity", "qa.alignment_quality"):
            cur.execute(f"TRUNCATE {t}")
        conn.commit()
    conn.close()


def db_max_event_id() -> int:
    import psycopg
    conn = psycopg.connect(
        host=os.environ.get("PGHOST", "db"), port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""),
        dbname=os.environ.get("PGDATABASE", "commonality"))
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(event_id), 0) FROM stg.fact_process_event")
        v = int(cur.fetchone()[0])
    conn.close()
    return v


def main() -> int:
    ap = argparse.ArgumentParser(description="多機台 genealogy 載入")
    ap.add_argument("--raw", default=os.environ.get("DATA_DIR", "/data/raw"))
    ap.add_argument("--tools", nargs="*", default=[], help="機台編號（可用 01,03,04…；相容 PowerShell 的 1,2,4,6 寫法）；預設全部")
    ap.add_argument("--gap", type=float, default=20.0)
    ap.add_argument("--spool-dir", default=os.environ.get("SPOOL_DIR", "/tmp/spool"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset", action="store_true",
                    help="先清空事實/維度表再重新載入全部機台（乾淨重建用，避免重複載入）")
    ap.add_argument("--reload-loaded", action="store_true",
                    help="即使機台已載入也重新載入（預設跳過已載入者）")
    ap.add_argument("--report", default="reports/m3/build_all_tools.json")
    add_version_arg(ap)
    args = ap.parse_args()

    files = sorted(Path(args.raw).glob("*_DC_score.csv"))
    if args.tools:
        want = set(parse_tools(args.tools))
        files = [f for f in files if f.name.split("_")[0] in want]
    if not files:
        print(f"[錯誤] {args.raw} 找不到 *_DC_score.csv（先跑 download_data.py --tools ...）")
        return 2

    if args.reset and not args.dry_run:
        print("--reset：清空 stg 事實/維度表與 qa 檢查表，重新以乾淨狀態載入全部機台")
        truncate_all()
    loaded = set() if (args.dry_run or args.reset) else db_loaded_tools()
    if loaded and not args.reload_loaded:
        norm = lambda x: x.replace("_", "").replace("-", "").upper()
        loaded_norm = {norm(t) for t in loaded}
        before = len(files)
        files = [f for f in files if norm(f.name.split("_")[0]) not in loaded_norm]
        if before - len(files):
            print(f"已載入的機台自動跳過 {before - len(files)} 台（資料庫目前有：{sorted(loaded)}）"
                  f"→ 不重複載入；要重載請加 --reset")
        if not files:
            print("五台機台都已在資料庫中，沒有需要載入的檔案。")
            return 0
    offset = 0 if args.dry_run else (0 if args.reset else db_max_event_id())
    if offset:
        print(f"資料庫已有事件（最大 event_id = {offset:,}）→ 新機台從 {offset + 1:,} 開始編號")

    print("=" * 94)
    print(f"多機台 genealogy 載入｜{len(files)} 台機台｜gap = {args.gap} 秒")
    print("=" * 94)
    print(f"  {'機台':<10}{'讀入列數':>12}{'事件數':>10}{'時間點':>12}{'感測特徵':>12}{'耗時':>8}")
    totals = {"n_rows": 0, "n_events": 0, "n_points": 0, "n_features": 0}
    report = {"gap": args.gap, "tools": [], "start_offset": offset}
    first = True
    t_all = time.time()
    for f in files:
        tool = f.name.split("_DC_")[0]
        summary = run(f, args.gap, Path(args.spool_dir), dry=True, limit_rows=None,
                      sample_events=None, event_offset=offset)
        ok = summary["n_points_in_events"] == summary["n_rows_read"]
        print(f"  {tool:<10}{summary['n_rows_read']:>12,}{summary['n_events']:>10,}"
              f"{summary['n_points_in_events']:>12,}{summary['features_expected']:>12,}"
              f"{summary['elapsed_s']:>7.0f}s" + ("" if ok else "  ❌ 守恆性失敗"))
        if not ok:
            print(f"[中止] {tool} 守恆性檢查失敗（事件內時間點 != 讀入列數）")
            return 1
        report["tools"].append({"tool": tool, "file": f.name, "event_offset": offset,
                                "n_rows": summary["n_rows_read"], "n_events": summary["n_events"],
                                "n_features": summary["features_expected"]})
        totals["n_rows"] += summary["n_rows_read"]
        totals["n_events"] += summary["n_events"]
        totals["n_points"] += summary["n_points_in_events"]
        totals["n_features"] += summary["features_expected"]

        if not args.dry_run:
            print(f"            → 寫入資料庫（{summary['n_events']:,} 事件、"
                  f"{summary['features_expected']:,} 特徵列）…", flush=True)
            copy_to_db(summary, args.gap, append=not first)
            first = False
        offset += summary["n_events"]

    print("-" * 94)
    print(f"  {'合計':<10}{totals['n_rows']:>12,}{totals['n_events']:>10,}"
          f"{totals['n_points']:>12,}{totals['n_features']:>12,}{time.time()-t_all:>7.0f}s")
    report["totals"] = totals
    rep = Path(args.report)
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  報告：{rep}")
    if args.dry_run:
        print("[dry-run] 未寫入資料庫")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
