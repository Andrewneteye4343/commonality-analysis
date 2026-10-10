#!/usr/bin/env python3
"""M3：把 groundtruth 的 TTF 反推成故障時刻，並產生批次的故障鄰近度標籤。

核心觀念：TTF 不變量
────────────────────────────────────────────────────────────
groundtruth 每個時間點記錄「距下一次某類故障還有幾秒」（TTF, Time To Failure）。
因此在任何有值的列上都有  t + TTF ≈ 常數 = 該次故障的時刻。

這個不變量有三個好處：
1. **不必知道故障時刻**就能估出來（用同一類故障所有觀測的中位數，穩健）
2. 能**自動偵測同一類故障發生多次**：t + TTF 會出現多個聚類
3. 能給出**估計不確定度**：聚類內 t + TTF 的離散程度

用法：
    python load_fault_labels.py --raw /data/raw --window-hours 24
    python load_fault_labels.py --raw /data/raw --dry-run            # 只印結果與寫報告
    python load_fault_labels.py --events-csv /tmp/events.csv --dry-run   # 沒有資料庫時用事件 CSV 驗證
"""
from __future__ import annotations

import argparse
import sys
import csv
import json
import os
import statistics
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from script_version import add_version_arg

from download_data import parse_tools

PREFIX = "TTF_"
WINDOW_CHOICES = (6, 12, 24, 48)


def normalize_tool(name: str) -> str:
    """檔名是 03_M01，資料裡的 Tool 欄位是 03M01 → 一律正規化後才對得上。

    （M2 的資料剖析就發現過這個落差：同一台機台在不同來源寫法不同，
      不統一會讓 join 靜默地變成 0 筆。）
    """
    return name.replace("_", "").replace("-", "").upper()


# ─────────────────────────────────────────────────────────────
# ① TTF → 故障時刻
# ─────────────────────────────────────────────────────────────
def extract_faults(path: Path, cluster_gap_s: float = 3600.0) -> dict:
    """回傳 {fault_type: [ {fault_ts, n_obs, ttf_min_s, spread_s}, ... ]}

    作法是對 t + TTF 做「四捨五入到 4 秒的直方圖」，再依時間缺口聚類。
    記憶體用量與檔案大小無關（只留直方圖），1.1M 列也能跑。
    """
    hist: dict[str, Counter] = defaultdict(Counter)
    ttf_min: dict[str, float] = {}
    n_rows = 0
    with path.open(newline="") as fh:
        rd = csv.reader(fh)
        header = next(rd)
        idx = {name: i for i, name in enumerate(header)}
        fault_cols = [c for c in header if c.startswith(PREFIX)]
        for row in rd:
            if len(row) < len(header):
                continue
            n_rows += 1
            t = float(row[idx["time"]])
            for col in fault_cols:
                v = row[idx[col]]
                if v == "":
                    continue
                ttf = float(v)
                ft = col[len(PREFIX):]
                hist[ft][round((t + ttf) / 4.0) * 4] += 1        # 4 秒解析度
                if ttf < ttf_min.get(ft, float("inf")):
                    ttf_min[ft] = ttf

    out: dict[str, list] = {}
    for ft, ctr in hist.items():
        buckets = sorted(ctr)
        clusters, cur = [], [buckets[0]]
        for b in buckets[1:]:
            if b - cur[-1] > cluster_gap_s:
                clusters.append(cur); cur = [b]
            else:
                cur.append(b)
        clusters.append(cur)
        items = []
        for cl in clusters:
            weights = [ctr[b] for b in cl]
            n_obs = sum(weights)
            # 加權中位數（穩健估計故障時刻）
            acc, med = 0, cl[-1]
            for b, w in zip(cl, weights):
                acc += w
                if acc >= n_obs / 2:
                    med = b
                    break
            p05, p95 = _weighted_pct(cl, weights, 0.05), _weighted_pct(cl, weights, 0.95)
            items.append({"fault_ts": float(med), "n_obs": n_obs,
                          "ttf_min_s": ttf_min.get(ft), "spread_s": float(p95 - p05)})
        out[ft] = sorted(items, key=lambda d: d["fault_ts"])
    return {"n_rows": n_rows, "faults": out,
            "file": path.name}


def _weighted_pct(buckets, weights, q):
    total = sum(weights)
    target = total * q
    acc = 0
    for b, w in zip(buckets, weights):
        acc += w
        if acc >= target:
            return b
    return buckets[-1]


# ─────────────────────────────────────────────────────────────
# ② 標籤：事件 → 距下次故障幾小時
# ─────────────────────────────────────────────────────────────
def label_events(events: list, faults: dict, window_hours: int) -> list:
    """events: [(event_id, tool_id, out_ts)]；回傳每個 (event, fault_type) 的標籤"""
    rows = []
    scopes = {"ANY": sorted({f["fault_ts"] for fs in faults.values() for f in fs})}
    for ft, fs in faults.items():
        scopes[ft] = sorted(f["fault_ts"] for f in fs)
    for event_id, tool_id, out_ts in events:
        for scope, ts_list in scopes.items():
            ahead = [ts for ts in ts_list if ts >= out_ts]
            if ahead:
                h = (ahead[0] - out_ts) / 3600.0
                in_win = 0.0 <= h <= window_hours
            else:
                h, in_win = None, False
            rows.append((event_id, tool_id, scope, window_hours, h, in_win, len(ahead)))
    return rows


# ─────────────────────────────────────────────────────────────
# ③ 批次層級彙總（分析單位）
# ─────────────────────────────────────────────────────────────
def build_lot_tables(labels: list, events_meta: dict) -> tuple[list, list, dict]:
    """回傳 (lot_units, lot_membership, summary)——**一次彙總所有機台**

    重要語意：**分析的單位是批次，而且批次是跨機台合併的**。
    同一批 lot 可能被多台機台加工過（真實 fab 必然如此），因此
      outcome_bad = 只要該批次的任一個事件落在「任一台機台」的退化窗內 → 這批可疑
      （物理意義：經過已退化機台加工的批次就是可疑批次）
    並且該批次會同時成為多個 tool 群組的成員——這正是共同性分析要處理的「共用機台」問題。
    """
    agg: dict[tuple, list] = defaultdict(lambda: [0, 0])          # (lot, scope, win) -> [n_events, n_in_window]
    membership: dict[str, dict] = defaultdict(lambda: {"n": 0, "tools": set(), "recipe": set(),
                                                       "recipe_step": set(), "stage": set(), "combo": set()})
    span: dict[str, list] = {}

    for (event_id, tool_id, scope, win, h, in_win, _n) in labels:
        meta = events_meta[event_id]
        lot = meta["lot_id"]
        cell = agg[(lot, scope, win)]
        cell[0] += 1
        cell[1] += 1 if in_win else 0
        m = membership[lot]
        m["n"] += 1
        m["tools"].add(tool_id)
        m["recipe"].add(meta["recipe"])
        m["recipe_step"].add(meta["recipe_step"])
        m["stage"].add(meta["stage"])
        m["combo"].add((meta["recipe"], meta["recipe_step"]))
        sp = span.setdefault(lot, [None, None])
        ts = meta["out_ts"]
        sp[0] = ts if sp[0] is None else min(sp[0], ts)
        sp[1] = ts if sp[1] is None else max(sp[1], ts)

    lot_units = []
    for (lot, scope, win), (n_ev, n_win) in agg.items():
        first, last = span[lot]
        tool_list = ",".join(sorted(membership[lot]["tools"]))
        lot_units.append((lot, tool_list, scope, win, n_win > 0, n_ev, n_win, first, last))

    mem_rows = []
    for lot, m in membership.items():
        for t in sorted(m["tools"]):
            mem_rows.append((lot, "tool", t, m["n"]))
        for r in m["recipe"]:
            mem_rows.append((lot, "recipe", r, m["n"]))
        for st in m["recipe_step"]:
            mem_rows.append((lot, "recipe_step", st, m["n"]))
        for g in m["stage"]:
            mem_rows.append((lot, "stage", g, m["n"]))
        for (r, st) in m["combo"]:
            mem_rows.append((lot, "recipe_step_x_stage", f"{r}|{st}", m["n"]))

    summary = {
        "n_lots": len(membership),
        "membership_rows": len(mem_rows),
        "group_counts": dict(Counter(g for (_l, g, _k, _n) in mem_rows)),
        "distinct_groups": {g: len({k for (_l, gg, k, _n) in mem_rows if gg == g}) for g in
                            {g for (_l, g, _k, _n) in mem_rows}},
        "lots_processed_by_multiple_tools": sum(1 for m in membership.values() if len(m["tools"]) > 1),
    }
    return lot_units, mem_rows, summary


# ─────────────────────────────────────────────────────────────
# ④ 資料庫寫入
# ─────────────────────────────────────────────────────────────
def write_db(fault_rows, lot_units, mem_rows, window_hours) -> None:
    import psycopg
    conn = psycopg.connect(
        host=os.environ.get("PGHOST", "db"), port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""),
        dbname=os.environ.get("PGDATABASE", "commonality"))
    with conn.cursor() as cur:
        # 冪等：同一 (tool, window) 的舊標籤先清掉再寫
        cur.execute("DELETE FROM mart.event_labels WHERE window_hours = %s", (window_hours,))
        cur.execute("DELETE FROM mart.lot_units WHERE window_hours = %s", (window_hours,))
        cur.execute("TRUNCATE mart.fault_events")
        cur.execute("TRUNCATE mart.lot_membership")

        with cur.copy("COPY mart.fault_events (tool_id, fault_type, fault_ts, n_obs, ttf_min_s, "
                      "spread_s, ttf_source) FROM STDIN") as cp:
            for r in fault_rows:
                cp.write_row(r)
        with cur.copy("COPY mart.lot_units (lot_id, tool_id, fault_type, window_hours, outcome_bad, "
                      "n_events, n_events_in_window, first_ts, last_ts) FROM STDIN") as cp:
            for r in lot_units:
                cp.write_row(r)
        with cur.copy("COPY mart.lot_membership (lot_id, group_type, group_key, n_events) FROM STDIN") as cp:
            for r in mem_rows:
                cp.write_row(r)
        conn.commit()
    conn.close()


def write_event_labels(labels) -> None:
    import psycopg
    conn = psycopg.connect(
        host=os.environ.get("PGHOST", "db"), port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""),
        dbname=os.environ.get("PGDATABASE", "commonality"))
    with conn.cursor() as cur:
        with cur.copy("COPY mart.event_labels (event_id, tool_id, fault_type, window_hours, "
                      "hours_to_fault, in_window, n_faults_ahead) FROM STDIN") as cp:
            for r in labels:
                cp.write_row(r)
        conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
def read_events_from_db(tools: list[str] | None = None):
    """從 stg.fact_process_event 讀事件（tools 可選；預設讀全部機台）"""
    import psycopg
    conn = psycopg.connect(
        host=os.environ.get("PGHOST", "db"), port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""),
        dbname=os.environ.get("PGDATABASE", "commonality"))
    with conn.cursor() as cur:
        sql = ("SELECT event_id, tool_id, lot_id, recipe, recipe_step, stage_mode, out_ts "
               "FROM stg.fact_process_event")
        params = ()
        if tools:
            sql += " WHERE tool_id = ANY(%s)"
            params = (list(tools),)
        cur.execute(sql + " ORDER BY event_id", params)
        rows = cur.fetchall()
    conn.close()
    meta = {r[0]: {"tool_id": r[1], "lot_id": r[2], "recipe": r[3], "recipe_step": r[4],
                   "stage": r[5], "out_ts": float(r[6])} for r in rows}
    return meta


def read_events_from_csv(path: Path, column_map: dict | None = None):
    """從 CSV 讀事件（助理端驗證用；欄位：event_id, Tool, Lot, out_ts, recipe, recipe_step, stage_mode）"""
    meta = {}
    with path.open(newline="") as fh:
        rd = csv.DictReader(fh)
        for r in rd:
            eid = int(r["event_id"])
            meta[eid] = {"tool_id": r.get("tool_id") or r.get("Tool"),
                         "lot_id": r.get("lot_id") or r.get("Lot"),
                         "recipe": r.get("recipe"), "recipe_step": r.get("recipe_step"),
                         "stage": r.get("stage_mode") or r.get("stage"),
                         "out_ts": float(r["out_ts"])}
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description="M3：故障標籤與批次層級分析單位")
    ap.add_argument("--raw", default=os.environ.get("DATA_DIR", "/data/raw"))
    ap.add_argument("--tools", nargs="*", default=[], help="機台編號；預設自動掃描 groundtruth 檔")
    ap.add_argument("--window-hours", type=int, default=24, help="退化窗長度（小時）")
    ap.add_argument("--events-csv", default="", help="沒有資料庫時：從 CSV 讀事件（助理端驗證用）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dump-dir", default="", help="把結果表另存成 CSV（沒有資料庫時供後續分析使用）")
    ap.add_argument("--report", default="reports/m3/fault_labels.json")
    add_version_arg(ap)
    args = ap.parse_args()

    raw = Path(args.raw)
    files = sorted(raw.glob("*_DC_groundtruth.csv"))
    if args.tools:
        want = set(parse_tools(args.tools))
        files = [f for f in files if f.name.split("_")[0] in want]
    if not files:
        print(f"[錯誤] 在 {raw} 找不到 *_DC_groundtruth.csv（先跑 download_data.py）")
        return 2

    report = {"window_hours": args.window_hours, "tools": {}, "lots": {}}
    all_fault_rows, all_labels, all_lot_units, all_mem = [], [], [], []
    for f in files:
        res = extract_faults(f)
        tool = normalize_tool(f.name.split("_DC_")[0])      # 例：03_M01 → 03M01（與資料一致）
        report["tools"][tool] = {"groundtruth_rows": res["n_rows"], "faults": res["faults"]}
        for ft, items in res["faults"].items():
            for it in items:
                all_fault_rows.append((tool, ft, it["fault_ts"], it["n_obs"],
                                       it["ttf_min_s"], it["spread_s"], f.name))

    if args.events_csv:
        meta = read_events_from_csv(Path(args.events_csv))
        print(f"[mode] 事件來源：CSV {args.events_csv}（{len(meta):,} 筆）")
    else:
        meta = read_events_from_db()
        print(f"[mode] 事件來源：資料庫 stg.fact_process_event（{len(meta):,} 筆）")

    # 逐機台標籤 → 最後一次彙總成批次層級（跨機台合併）
    all_labels = []
    for tool, info in report["tools"].items():
        ev = [(eid, normalize_tool(m["tool_id"] or ""), m["out_ts"])
              for eid, m in meta.items() if normalize_tool(m["tool_id"] or "") == tool]
        if not ev:
            print(f"  [跳過] {tool}：資料庫沒有對應事件")
            info["n_events"] = 0
            continue
        labels = label_events(ev, info["faults"], args.window_hours)
        all_labels.extend(labels)
        info["n_events"] = len(ev)

    if not all_labels:
        print("[錯誤] 沒有任何事件被標籤——確認 load 的機台與 fact_process_event 的 tool_id 一致")
        return 1
    all_lot_units, all_mem, lot_summary = build_lot_tables(all_labels, meta)
    report["lot_summary"] = lot_summary
    for scope in sorted({r[2] for r in all_lot_units}):
        rows = [r for r in all_lot_units if r[2] == scope]
        bad = sum(1 for r in rows if r[4])
        report["lots"][scope] = {"n_lots": len(rows), "n_bad_lots": bad,
                                 "pct_bad": round(100.0 * bad / max(len(rows), 1), 3),
                                 "n_events_in_window": sum(r[6] for r in rows)}

    # 印出結果
    print("=" * 86)
    print(f"故障反推與標籤（退化窗 {args.window_hours} 小時）")
    print("=" * 86)
    for tool, info in report["tools"].items():
        if "faults" not in info:
            continue
        print(f"\n機台 {tool}｜事件 {info.get('n_events', 0):,} 筆")
        for ft, items in info["faults"].items():
            for it in items:
                print(f"  故障 {ft[:46]:<48} 時刻 {it['fault_ts']:>12,.0f}｜"
                      f"觀測 {it['n_obs']:>9,} 列｜不確定度 ±{it['spread_s']:>7,.0f} 秒")
    print(f"\n批次層級標籤（跨機台合併，窗 {args.window_hours} 小時）")
    for scope, st in sorted(report["lots"].items()):
        print(f"  壞批次定義 [{scope:<24}]：{st['n_bad_lots']:>5,} / {st['n_lots']:>5,} = {st['pct_bad']:>6.2f}%"
              f"（窗內事件 {st['n_events_in_window']:,} 筆）")
    print(f"  被多台機台加工過的批次：{lot_summary['lots_processed_by_multiple_tools']:,}"
          f"（共同性分析要處理的『共用機台』問題）")

    rep = Path(args.report)
    rep.parent.mkdir(parents=True, exist_ok=True)
    report["totals"] = {"fault_rows": len(all_fault_rows), "label_rows": len(all_labels),
                        "lot_unit_rows": len(all_lot_units), "membership_rows": len(all_mem)}
    rep.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n  報告：{rep}")

    if args.dump_dir:
        d = Path(args.dump_dir)
        d.mkdir(parents=True, exist_ok=True)
        with (d / "lot_units.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["lot_id", "tool_id", "fault_type", "window_hours", "outcome_bad",
                        "n_events", "n_events_in_window", "first_ts", "last_ts"])
            w.writerows(all_lot_units)
        with (d / "lot_membership.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["lot_id", "group_type", "group_key", "n_events"])
            w.writerows(all_mem)
        with (d / "event_labels.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["event_id", "tool_id", "fault_type", "window_hours", "hours_to_fault",
                        "in_window", "n_faults_ahead"])
            w.writerows(all_labels)
        print(f"  已另存 CSV：{d}/lot_units.csv、lot_membership.csv、event_labels.csv")

    if args.dry_run:
        print("[dry-run] 未寫入資料庫")
        return 0
    print("\n寫入資料庫 …")
    write_db(all_fault_rows, all_lot_units, all_mem, args.window_hours)
    write_event_labels(all_labels)
    print("完成：mart.fault_events、mart.event_labels、mart.lot_units、mart.lot_membership")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
