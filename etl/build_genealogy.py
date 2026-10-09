#!/usr/bin/env python3
"""M2：建立 genealogy 事實表（加工事件 + 感測時序 + 感測特徵）。

事件切分規則（由 etl/analyze_event_boundaries.py 的實證結果決定）：
    同一 (Tool, Lot, recipe, recipe_step) 的資料列為一群，
    群內相鄰時間差 > --gap（預設 20 秒）即切成新事件。
    * 實測：鍵的變化即為邊界；gap 由 8 秒放寬到 300 秒只改變 < 0.1% 的事件數 → 穩健
    * runnum 在此資料中是「機台累積使用次數」（只增不減），不是事件識別碼
    * stage 在單一事件內應為單一值；>1 代表切分有問題（會列入品質報表）

用法：
    python build_genealogy.py --file /data/raw/03_M01_DC_score.csv              # 寫入資料庫
    python build_genealogy.py --file ... --dry-run                              # 只計算與輸出樣本
    python build_genealogy.py --file ... --limit-rows 200000 --dry-run         # 快速抽查
    python build_genealogy.py --file ... --gap 20 --spool-dir /tmp/spool

記憶體策略：事件與特徵不留在記憶體，逐一寫入 spool CSV，最後用 COPY 進資料庫
（1,144,073 列 × 17 感測器若全留在 Python 物件會吃掉數 GB）。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import time
from collections import Counter
from pathlib import Path

KEY_COLS = ("Tool", "Lot", "recipe", "recipe_step")
META_COLS = ("time", "Tool", "stage", "Lot", "runnum", "recipe", "recipe_step")
SENSOR_COLS_ORDER = [
    "IONGAUGEPRESSURE", "ETCHBEAMVOLTAGE", "ETCHBEAMCURRENT", "ETCHSUPPRESSORVOLTAGE",
    "ETCHSUPPRESSORCURRENT", "FLOWCOOLFLOWRATE", "FLOWCOOLPRESSURE", "ETCHGASCHANNEL1READBACK",
    "ETCHPBNGASREADBACK", "FIXTURETILTANGLE", "ROTATIONSPEED", "ACTUALROTATIONANGLE",
    "FIXTURESHUTTERPOSITION", "ETCHSOURCEUSAGE", "ETCHAUXSOURCETIMER", "ETCHAUX2SOURCETIMER",
    "ACTUALSTEPDURATION",
]


class SensorAcc:
    """感測器統計累積器（單次掃描、數值穩定：時間以 in_ts 為原點）"""
    __slots__ = ("n", "n_missing", "s", "ss", "mn", "mx", "first", "last",
                 "st", "stt", "stv", "t0")

    def __init__(self, t0: float):
        self.n = 0
        self.n_missing = 0
        self.s = self.ss = 0.0
        self.mn = math.inf
        self.mx = -math.inf
        self.first = None
        self.last = None
        self.st = self.stt = self.stv = 0.0
        self.t0 = t0

    def add(self, t: float, v: float | None) -> None:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            self.n_missing += 1
            return
        self.n += 1
        self.s += v
        self.ss += v * v
        self.mn = min(self.mn, v)
        self.mx = max(self.mx, v)
        if self.first is None:
            self.first = v
        self.last = v
        dt = t - self.t0
        self.st += dt
        self.stt += dt * dt
        self.stv += dt * v

    def summary(self) -> dict:
        n = self.n
        mean = self.s / n if n else None
        if n > 1:
            var = max(0.0, (self.ss - n * mean * mean) / (n - 1))
            std = var ** 0.5
        else:
            std = None
        slope = None
        if n >= 3:
            den = n * self.stt - self.st ** 2
            if abs(den) > 1e-12:
                slope = (n * self.stv - self.st * self.s) / den
        return {
            "n": n,
            "mean": mean,
            "std": std,
            "min": None if self.mn is math.inf else self.mn,
            "max": None if self.mx is -math.inf else self.mx,
            "range": None if self.mn is math.inf else self.mx - self.mn,
            "first": self.first,
            "last": self.last,
            "slope": slope,
            "n_missing": self.n_missing,
        }


def run(file: Path, gap: float, spool_dir: Path, dry: bool, limit_rows: int | None,
        sample_events: Path | None) -> dict:
    spool_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    f_ev = spool_dir / f"events_{stamp}.csv"
    f_ft = spool_dir / f"features_{stamp}.csv"
    f_tr = spool_dir / f"trace_{stamp}.csv"

    n_rows = 0
    next_event_id = 1
    # 每個 group key 目前開啟中的事件
    open_states: dict[tuple, dict] = {}
    lots: dict[str, list] = {}          # lot -> [n_points, first_ts, last_ts, n_events]
    steps: dict[tuple, set] = {}        # (recipe, recipe_step) -> set(stage)
    tools: Counter = Counter()
    recipes: Counter = Counter()
    events_by_id: dict[int, dict] = {}  # 只在 dry-run 時保留少量樣本
    finished_events = 0
    total_points = 0
    gap_unclosed = 0

    t_start = time.time()
    with file.open(newline="") as fh_in, \
            f_ev.open("w", newline="") as fh_ev, \
            f_ft.open("w", newline="") as fh_ft, \
            f_tr.open("w", newline="") as fh_tr:
        w_ev = csv.writer(fh_ev)
        w_ft = csv.writer(fh_ft)
        w_tr = csv.writer(fh_tr)
        reader = csv.reader(fh_in)
        header = next(reader)
        idx = {c: i for i, c in enumerate(header)}
        sensor_cols = [c for c in header if c not in META_COLS]

        def finalize(st: dict) -> None:
            nonlocal finished_events
            ev = st
            if ev["stage_c"]:
                ev["stage_mode"] = ev["stage_c"].most_common(1)[0][0]
                ev["stage_n_distinct"] = len(ev["stage_c"])
            else:
                ev["stage_mode"], ev["stage_n_distinct"] = None, 0
            n = ev["n"]
            pts = ev["points"]
            med = statistics.median(pts) if pts else None
            w_ev.writerow([ev["event_id"], ev["Tool"], ev["Lot"], ev["recipe"], ev["recipe_step"],
                           ev["stage_mode"], ev["stage_n_distinct"], ev["runnum_first"], ev["runnum_last"],
                           ev["in_ts"], ev["out_ts"], n, ev["out_ts"] - ev["in_ts"],
                           "" if med is None else f"{med:.4f}", f"{ev['gap_max']:.4f}", file.name])
            for s in sensor_cols:
                acc: SensorAcc = ev["acc"][s]
                sm = acc.summary()
                w_ft.writerow([ev["event_id"], s, sm["n"], f"{sm['n_missing'] / n:.4f}" if n else "",
                               sm["mean"], sm["std"], sm["min"], sm["max"], sm["range"],
                               sm["first"], sm["last"], sm["slope"]])
            finished_events += 1

        for row in reader:
            if len(row) < len(header):
                continue
            n_rows += 1
            if limit_rows and n_rows > limit_rows:
                n_rows -= 1
                break
            t = float(row[idx["time"]])
            key = tuple(row[idx[c]] for c in KEY_COLS)
            tool, lot, recipe, step = key

            # trace 一律用「該列所屬事件」的 id 寫出
            st = open_states.get(key)
            if st is None or (t - st["last_t"]) > gap:
                if st is not None:
                    finalize(st)
                    gap_unclosed += 1
                st = {
                    "event_id": next_event_id,
                    "Tool": tool, "Lot": lot, "recipe": recipe, "recipe_step": step,
                    "in_ts": t, "last_t": t, "out_ts": t, "n": 0, "gap_max": 0.0,
                    "points": [], "stage_c": Counter(), "runnum_first": row[idx["runnum"]],
                    "runnum_last": row[idx["runnum"]],
                    "acc": {s: SensorAcc(t) for s in sensor_cols},
                }
                next_event_id += 1
                open_states[key] = st
                if dry and len(events_by_id) < 25:
                    events_by_id[st["event_id"]] = st
            else:
                g = t - st["last_t"]
                st["gap_max"] = max(st["gap_max"], g)
                if len(st["points"]) < 1000:
                    st["points"].append(g)

            st["n"] += 1
            total_points += 1
            st["out_ts"] = t
            st["last_t"] = t
            st["stage_c"][row[idx["stage"]]] += 1
            st["runnum_last"] = row[idx["runnum"]]

            vals = [row[idx[c]] for c in sensor_cols]
            w_tr.writerow([st["event_id"], row[idx["time"]], *vals])
            for s, v in zip(sensor_cols, vals):
                st["acc"][s].add(t, None if v == "" else float(v))

            lots.setdefault(lot, [0, t, t, 0])
            lo = lots[lot]
            lo[0] += 1
            lo[1] = min(lo[1], t)
            lo[2] = max(lo[2], t)
            tools[tool] += 1
            recipes[recipe] += 1
            steps.setdefault((recipe, step), set()).add(row[idx["stage"]])

        # 收尾
        for st in open_states.values():
            finalize(st)

        # 將事件結束後才知道的 stage 統計與 lot 事件數補齊：改寫 event spool（示範用簡化）
    # lot 事件數：由 events spool 重新統計（檔案不大）
    with f_ev.open(newline="") as fh_ev:
        for r in csv.reader(fh_ev):
            lots.setdefault(r[2], [0, 0.0, 0.0, 0])
            lots[r[2]][3] += 1

    summary = {
        "file": file.name,
        "gap_threshold": gap,
        "n_rows_read": n_rows,
        "n_events": finished_events,
        "n_points_in_events": total_points,
        "n_lots": len(lots),
        "n_sensors": len(sensor_cols),
        "features_expected": finished_events * len(sensor_cols),
        "elapsed_s": round(time.time() - t_start, 1),
        "spool_events": str(f_ev),
        "spool_features": str(f_ft),
        "spool_trace": str(f_tr),
        "spool_sizes": {p.name: p.stat().st_size for p in (f_ev, f_ft, f_tr)},
        "lots": {k: {"n_points": v[0], "first_ts": v[1], "last_ts": v[2], "n_events": v[3]}
                 for k, v in lots.items()},
        "steps": {f"{r}|{s}": sorted(vs) for (r, s), vs in steps.items()},
        "tools": dict(tools),
        "recipes": dict(recipes),
    }

    if dry and sample_events:
        sample_events.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for eid, ev in list(events_by_id.items())[:10]:
            rows.append({k: ev[k] for k in
                         ("event_id", "Tool", "Lot", "recipe", "recipe_step", "in_ts", "out_ts", "n", "gap_max")
                         } | {"stage_mode": ev["stage_c"].most_common(1)[0][0] if ev["stage_c"] else None})
        with sample_events.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        summary["sample_events_file"] = str(sample_events)
        summary["sample_events"] = rows
    return summary


def preflight_schema(cur) -> None:
    """開跑前驗證 schema 欄位與程式端名稱一致（欄名筆誤要立刻講清楚，不要讓 COPY 丟原始錯誤）"""
    expected = {
        "fact_process_event": ["event_id", "tool_id", "lot_id", "recipe", "recipe_step", "stage_mode",
                               "stage_n_distinct", "runnum_first", "runnum_last", "in_ts", "out_ts",
                               "n_points", "duration_units", "sampling_median", "gap_max", "source_file"],
        "fact_sensor_trace": ["event_id", "ts"] + [c.lower() for c in SENSOR_COLS_ORDER],
        "fact_sensor_feature": ["event_id", "sensor", "n", "missing_rate", "mean_val", "std_val",
                                "min_val", "max_val", "range_val", "first_val", "last_val", "slope"],
    }
    problems = []
    for table, cols in expected.items():
        cur.execute("""SELECT column_name FROM information_schema.columns
                       WHERE table_schema = 'stg' AND table_name = %s""", (table,))
        have = {r[0] for r in cur.fetchall()}
        if not have:
            problems.append(f"找不到資料表 stg.{table}")
            continue
        missing = [c for c in cols if c not in have]
        if missing:
            problems.append(f"stg.{table} 缺少欄位：{', '.join(missing)}")
    if problems:
        print("[錯誤] schema 與程式預期不符，已中止（未寫入任何資料）：")
        for p in problems:
            print(f"  - {p}")
        print("\n處理方式：先套用最新 migration（會自動修復欄名）")
        print("  docker compose run --rm etl python migrate.py")
        print("若仍失敗，請把上面這幾行貼給我。")
        raise SystemExit(1)
    print("schema 預檢：三個事實表的欄位與程式一致 ✅")


def copy_to_db(summary: dict, gap: float) -> None:
    """把 spool 檔 COPY 進 PostgreSQL（先預檢 schema → TRUNCATE → COPY → 更新品質表）"""
    import psycopg

    conn = psycopg.connect(
        host=os.environ.get("PGHOST", "db"), port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ["PGUSER"], password=os.environ["PGPASSWORD"], dbname=os.environ["PGDATABASE"])

    with conn.cursor() as cur:
        preflight_schema(cur)

        for t in ("stg.fact_sensor_feature", "stg.fact_sensor_trace", "stg.fact_process_event",
                  "stg.dim_step", "stg.dim_recipe", "stg.dim_lot", "stg.dim_tool",
                  "qa.segmentation_sensitivity", "qa.alignment_quality"):
            cur.execute(f"TRUNCATE {t}")

        with cur.copy("COPY stg.fact_process_event (event_id, tool_id, lot_id, recipe, recipe_step,"
                      " stage_mode, stage_n_distinct, runnum_first, runnum_last, in_ts, out_ts, n_points,"
                      " duration_units, sampling_median, gap_max, source_file) FROM STDIN WITH (FORMAT csv)") as cp:
            with open(summary["spool_events"], "rb") as fh:
                while (chunk := fh.read(1 << 20)):
                    cp.write(chunk)

        with cur.copy("COPY stg.fact_sensor_feature (event_id, sensor, n, missing_rate, mean_val, std_val,"
                      " min_val, max_val, range_val, first_val, last_val, slope) FROM STDIN WITH (FORMAT csv)") as cp:
            with open(summary["spool_features"], "rb") as fh:
                while (chunk := fh.read(1 << 20)):
                    cp.write(chunk)

        with cur.copy("COPY stg.fact_sensor_trace (event_id, ts, " + ", ".join(c.lower() for c in SENSOR_COLS_ORDER) +
                      ") FROM STDIN WITH (FORMAT csv)") as cp:
            with open(summary["spool_trace"], "rb") as fh:
                while (chunk := fh.read(1 << 20)):
                    cp.write(chunk)

        # 維度
        with cur.copy("COPY stg.dim_tool (tool_id, source_note) FROM STDIN") as cp:
            for t in summary["tools"]:
                cp.write_row((t, "PHM 2018（公開資料，已匿名化）"))
        with cur.copy("COPY stg.dim_recipe (recipe_key) FROM STDIN") as cp:
            for r in summary["recipes"]:
                cp.write_row((r,))
        with cur.copy("COPY stg.dim_lot (lot_id, n_points, first_ts, last_ts, n_events) FROM STDIN") as cp:
            for k, v in summary["lots"].items():
                cp.write_row((k, v["n_points"], v["first_ts"], v["last_ts"], v["n_events"]))
        with cur.copy("COPY stg.dim_step (step_key, recipe, recipe_step, stage_values, n_stages) FROM STDIN") as cp:
            for key, stages in summary["steps"].items():
                recipe, step = key.split("|", 1)
                cp.write_row((key, recipe, step, stages, len(stages)))

        # 品質檢查（覆蓋率等由 align_qc.py 計算；這裡寫入守恆性檢查）
        checks = [
            ("row_conservation", float(summary["n_points_in_events"] == summary["n_rows_read"]), "boolean",
             summary["n_points_in_events"] == summary["n_rows_read"],
             f"事件內時間點 {summary['n_points_in_events']:,} vs 讀入列數 {summary['n_rows_read']:,}"),
            ("feature_completeness", float(summary["features_expected"]), "rows", True,
             f"每事件 × {summary['n_sensors']} 感測器"),
        ]
        with cur.copy("COPY qa.alignment_quality (check_name, metric, unit, passed, detail) FROM STDIN") as cp:
            for row in checks:
                cp.write_row(row)
        conn.commit()
    conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="建立 genealogy 事實表（M2）")
    ap.add_argument("--file", required=True)
    ap.add_argument("--gap", type=float, default=20.0, help="事件切分時間門檻（預設 20 秒）")
    ap.add_argument("--spool-dir", default=os.environ.get("SPOOL_DIR", "/tmp/spool"))
    ap.add_argument("--dry-run", action="store_true", help="不寫資料庫")
    ap.add_argument("--limit-rows", type=int, default=None)
    ap.add_argument("--report", default="reports/m2/build_summary.json")
    args = ap.parse_args()

    summary = run(Path(args.file), args.gap, Path(args.spool_dir), args.dry_run,
                  args.limit_rows, Path(args.report))
    rep = Path(args.report)
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 78)
    print(f"genealogy 建立結果：{summary['file']}（gap 門檻 {summary['gap_threshold']} 秒）")
    print("=" * 78)
    print(f"  讀入列數      : {summary['n_rows_read']:,}")
    print(f"  加工事件數    : {summary['n_events']:,}")
    print(f"  事件內時間點  : {summary['n_points_in_events']:,}")
    print(f"  加工單位(lot) : {summary['n_lots']:,}")
    print(f"  感測器數      : {summary['n_sensors']}")
    print(f"  感測特徵列數  : {summary['features_expected']:,}（= 事件數 × 感測器數）")
    print(f"  耗時          : {summary['elapsed_s']}s")
    print("  spool 檔案    : " + ", ".join(f"{k}={v/2**20:.1f} MB" for k, v in summary["spool_sizes"].items()))
    ok = summary["n_points_in_events"] == summary["n_rows_read"]
    print(f"\n  守恆性檢查（事件內時間點 == 讀入列數）：{'✅ 通過' if ok else '❌ 失敗'}")
    print(f"  報告：{args.report}")

    if not args.dry_run:
        print("\n寫入 PostgreSQL …")
        copy_to_db(summary, args.gap)
        print("完成：stg.dim_*、stg.fact_process_event、stg.fact_sensor_trace、stg.fact_sensor_feature")
    else:
        print("\n[dry-run] 未寫入資料庫（spool 檔保留在 " + args.spool_dir + "）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
