#!/usr/bin/env python3
"""M2：對齊品質檢查（Alignment QC）

兩種模式：

1) --db      從資料庫讀事實表，檢查守恆性、覆蓋率、重複匹配率、切分品質，
             並把結果寫進 qa.alignment_quality（每項都有 passed 欄位）。

2) --simulate 在記憶體中對 CSV 子集做「時鐘偏移（clock skew）壓力測試」：
             用同一份 trace 去對「被刻意平移過的事件時間窗」做 interval join，
             觀察覆蓋率如何變化 —— 這是證明 QC 真的抓得到錯位的實驗，
             也說明為什麼實務上時間窗要留 buffer。

用法：
    python align_qc.py --db
    python align_qc.py --simulate --file /data/raw/03_M01_DC_score.csv --rows 300000
"""
from __future__ import annotations

import argparse
import sys
import bisect
import csv
import json
import os
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from script_version import add_version_arg

KEY_COLS = ("Tool", "Lot", "recipe", "recipe_step")


# ─────────────────────────────────────────────────────────────
# 模式 1：資料庫檢查
#
# 效能原則（2026-10-08 修正）：**不要在資料庫做非等值的大 join**。
#   舊版「重複匹配率」寫成 fact_sensor_trace × fact_process_event 的
#   `ts BETWEEN in_ts AND out_ts` 非等值 join，代價是 1,144,073 × 29,002
#   ≈ 331 億次比較 → 跑數小時，並在 PostgreSQL 的暫存目錄產生大量檔案
#   （暫存目錄位於 pgdata volume，而 Docker 的 volume 實體在 C: 底下的
#    虛擬磁碟 ext4.vhdx）。
#   新版改為：
#     ① 覆蓋率用 event_id **等值** join（有索引）驗證，成本 = 掃 114 萬列
#     ② 重複匹配改在 **29,002 個事件**（不是 114 萬列）層級做掃掠，
#        只把事件窗拉進 Python（約 1.5 MB），配對數很少時幾乎零成本
#     ③ 所有查詢加上 statement_timeout，避免單一查詢吃光資源
# ─────────────────────────────────────────────────────────────
def db_connect():
    import psycopg
    return psycopg.connect(
        host=os.environ.get("PGHOST", "db"), port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ["PGUSER"], password=os.environ["PGPASSWORD"], dbname=os.environ["PGDATABASE"])


def q1(cur, sql: str, params=None):
    cur.execute(sql, params or ())
    return cur.fetchone()


def sweep_overlaps(events: list) -> tuple[list, int]:
    """事件窗重疊掃掠（同一台機台內）。

    events: [(event_id, tool_id, in_ts, out_ts)]，已依 (tool_id, in_ts) 排序。
    回傳 (重疊配對清單, 重疊總數)。事件窗很短且大多不重疊，
    用「活動中視窗」掃掠即可，成本約 O(n log n)。
    """
    pairs = []
    active: list[tuple[int, float, float]] = []   # (event_id, in_ts, out_ts)
    cur_tool = None
    for event_id, tool_id, in_ts, out_ts in events:
        in_ts, out_ts = float(in_ts), float(out_ts)
        if tool_id != cur_tool:
            active = []                            # 換機台就清空（不同機台不互相比較）
            cur_tool = tool_id
        active = [a for a in active if a[2] >= in_ts]   # 移除已結束的視窗
        for a_id, a_in, a_out in active:
            if not (a_out < in_ts or a_in > out_ts):    # 區間有交集
                pairs.append((a_id, event_id))
        active.append((event_id, in_ts, out_ts))
    return pairs, len(pairs)


def run_db() -> int:
    conn = db_connect()
    checks: list[tuple] = []
    timings: list[tuple[str, float]] = []
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = '180s'")   # 防護：單一查詢不得超過 3 分鐘

        def timed(name, fn):
            t0 = time.time()
            val = fn()
            timings.append((name, round(time.time() - t0, 2)))
            return val

        n_events = timed("事件數", lambda: q1(cur, "SELECT count(*) FROM stg.fact_process_event")[0])
        n_trace = timed("trace 列數", lambda: q1(cur, "SELECT count(*) FROM stg.fact_sensor_trace")[0])
        n_points = timed("事件時間點合計", lambda: q1(cur, "SELECT coalesce(sum(n_points),0) FROM stg.fact_process_event")[0])
        n_feat = timed("特徵列數", lambda: q1(cur, "SELECT count(*) FROM stg.fact_sensor_feature")[0])
        n_sensors = timed("感測器數", lambda: q1(cur, "SELECT count(DISTINCT sensor) FROM stg.fact_sensor_feature")[0])
        n_lots = timed("lot 數", lambda: q1(cur, "SELECT count(*) FROM stg.dim_lot")[0])
        n_lots_ev = timed("有事件的 lot 數", lambda: q1(cur, "SELECT count(DISTINCT lot_id) FROM stg.fact_process_event")[0])
        bad_stage = timed("stage 一致性", lambda: q1(cur, "SELECT count(*) FROM stg.fact_process_event WHERE stage_n_distinct > 1")[0])
        big_gap = timed("gap 檢查", lambda: q1(cur, "SELECT count(*) FROM stg.fact_process_event WHERE gap_max > 20")[0])
        empty_ev = timed("空事件檢查", lambda: q1(cur, """SELECT count(*) FROM stg.fact_process_event e
                              WHERE NOT EXISTS (SELECT 1 FROM stg.fact_sensor_trace t WHERE t.event_id = e.event_id)""")[0])

        # 覆蓋率：等值 join（event_id 有索引）→ 只掃 114 萬列，不做非等值比較
        off_window = timed("時間窗偏移筆數", lambda: q1(cur, """
            SELECT count(*) FROM stg.fact_sensor_trace t
            JOIN stg.fact_process_event e ON e.event_id = t.event_id
            WHERE t.ts < e.in_ts OR t.ts > e.out_ts""")[0])
        cov = round(100.0 * (n_trace - off_window) / n_trace, 4) if n_trace else 0.0

        # 重複匹配：把事件窗拉進 Python 掃掠（29,002 列，不是 114 萬列）
        cur.execute("SELECT event_id, tool_id, in_ts, out_ts FROM stg.fact_process_event ORDER BY tool_id, in_ts")
        events = cur.fetchall()
        t0 = time.time()
        pairs, _ = sweep_overlaps(events)
        timings.append(("事件窗重疊掃掠", round(time.time() - t0, 2)))

        # 只有真的重疊的事件窗才需要查受影響的 trace 列數（配對數通常為 0 或個位數）
        affected = 0
        for a_id, b_id in pairs[:200]:      # 上限 200 對，避免異常資料時失控
            cur.execute("""SELECT count(*) FROM stg.fact_sensor_trace t
                           JOIN stg.fact_process_event e ON e.event_id = %s
                           WHERE t.event_id = %s AND t.ts BETWEEN e.in_ts AND e.out_ts""", (a_id, b_id))
            affected += cur.fetchone()[0]
        dup = round(100.0 * affected / n_trace, 4) if n_trace else 0.0

        checks = [
            ("row_conservation", float(n_trace == n_points), "boolean", n_trace == n_points,
             f"trace 列數 {n_trace:,} vs 事件 n_points 合計 {n_points:,}"),
            ("feature_completeness", float(n_feat), "rows", n_feat == n_events * n_sensors,
             f"特徵列數 {n_feat:,} = 事件 {n_events:,} × 感測器 {n_sensors}"),
            ("lot_coverage", round(100.0 * n_lots_ev / max(n_lots, 1), 4), "percent",
             n_lots_ev == n_lots, f"有事件的 lot {n_lots_ev:,} / 全部 lot {n_lots:,}"),
            ("trace_interval_coverage", cov, "percent", cov >= 95.0,
             f"trace 落在自己所屬事件時間窗內的比例（門檻 95%；偏移 {off_window:,} 筆）"),
            ("duplicate_match_rate", dup, "percent", dup < 0.01,
             f"一筆 trace 落入同機台多個事件時間窗的比例（門檻 < 0.01%；重疊事件窗 {len(pairs)} 對、"
             f"受影響 {affected} 列）"),
            ("stage_consistency", float(bad_stage), "events", bad_stage == 0,
             "單一事件內 stage 不唯一的事件數（切分品質）"),
            ("gap_within_threshold", float(big_gap), "events", big_gap == 0,
             "事件內最大相鄰時間差 > 20 秒的事件數"),
            ("events_without_trace", float(empty_ev), "events", empty_ev == 0,
             "沒有任何 trace 資料列的事件數"),
        ]

        cur.execute("TRUNCATE qa.alignment_quality")
        with cur.copy("COPY qa.alignment_quality (check_name, metric, unit, passed, detail) FROM STDIN") as cp:
            for row in checks:
                cp.write_row(row)
        conn.commit()

    conn.close()
    print("=" * 90)
    print("對齊品質檢查（Alignment QC）")
    print("=" * 90)
    head = f"  {'檢查項目':<26}{'數值':>14}{'單位':>10}  {'結果':<6} 說明"
    print(head)
    for name, val, unit, passed, detail in checks:
        # 小數值（例如 0.0002%）用 4 位小數，避免顯示成 0.00 蓋掉實際差異
        if val is None:
            v = "—"
        elif abs(val) < 1:
            v = f"{val:,.4f}"
        else:
            v = f"{val:,.2f}"
        mark = "✅" if passed else "❌"
        print(f"  {name:<26}{v:>14}{unit:>10}  {mark:<6} {detail}")
    print("\n  各步驟耗時（秒）：" + "｜".join(f"{k} {v}" for k, v in timings))
    failed = [c[0] for c in checks if not c[3]]
    verdict = "✅ 全部通過" if not failed else "❌ 未通過 → " + ", ".join(failed)
    print(f"\n  驗收：{verdict}")
    return 0 if not failed else 1


# ─────────────────────────────────────────────────────────────
# 模式 2：時鐘偏移壓力測試（不需要資料庫）
# ─────────────────────────────────────────────────────────────
def build_intervals(file: Path, gap: float, rows: int) -> tuple[list, list, list]:
    """回傳 (intervals, traces, raw_ts)：intervals = [(event_id, in_ts, out_ts)]"""
    intervals: list[tuple[int, float, float]] = []
    traces: list[tuple[int, float]] = []
    raw_ts: list[float] = []
    open_state: dict[tuple, list] = {}
    n = 0
    with file.open(newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        idx = {c: i for i, c in enumerate(header)}
        for row in reader:
            if len(row) < len(header):
                continue
            n += 1
            if n > rows:
                break
            t = float(row[idx["time"]])
            raw_ts.append(t)
            key = tuple(row[idx[c]] for c in KEY_COLS)
            st = open_state.get(key)
            if st is None or (t - st[2]) > gap:
                st = [len(intervals) + 1, t, t]
                open_state[key] = st
                intervals.append((st[0], st[1], st[2]))
            st[2] = t
            # 讓 intervals 中的 out_ts 跟著更新
            intervals[st[0] - 1] = (st[0], st[1], st[2])
            traces.append((st[0], t))
    return intervals, traces, raw_ts


def join_coverage(intervals: list, ts_list: list, shift: float, buffer: float) -> tuple[float, float]:
    """把事件窗平移 shift 秒（模擬時鐘偏移）後做 interval join，回傳 (覆蓋率%, 重複匹配率%)"""
    iv = sorted(((a + shift - buffer, b + shift + buffer) for _, a, b in intervals), key=lambda x: x[0])
    starts = [x[0] for x in iv]
    covered = 0
    multi = 0
    for ts in ts_list:
        # 候選：start <= ts 的最後幾個（最多回看 200 個就足夠，因為事件很短）
        hi = bisect.bisect_right(starts, ts)
        cnt = 0
        for j in range(max(0, hi - 200), hi):
            if iv[j][1] >= ts:
                cnt += 1
        if cnt >= 1:
            covered += 1
        if cnt > 1:
            multi += 1
    n = len(ts_list) or 1
    return round(100.0 * covered / n, 4), round(100.0 * multi / n, 4)


def run_simulate(file: Path, gap: float, rows: int, out: Path) -> int:
    print(f"建立事件時間窗（前 {rows:,} 列，gap={gap} 秒）…", flush=True)
    intervals, traces, ts_list = build_intervals(file, gap, rows)
    print(f"  事件數 {len(intervals):,}｜trace 列數 {len(traces):,}")

    scenarios = [
        ("無偏移、無 buffer", 0.0, 0.0),
        ("時鐘快 2 秒，無 buffer", 2.0, 0.0),
        ("時鐘慢 2 秒，無 buffer", -2.0, 0.0),
        ("時鐘快 30 秒，無 buffer", 30.0, 0.0),
        ("時鐘快 30 秒，buffer 30 秒", 30.0, 30.0),
        ("時鐘快 300 秒，buffer 30 秒", 300.0, 30.0),
        ("時鐘快 300 秒，buffer 300 秒", 300.0, 300.0),
    ]
    results = []
    print(f"\n  {'情境':<28}{'覆蓋率%':>10}{'重複匹配率%':>12}")
    for name, shift, buf in scenarios:
        cov, dup = join_coverage(intervals, ts_list, shift, buf)
        results.append({"scenario": name, "shift_s": shift, "buffer_s": buf,
                        "coverage_pct": cov, "duplicate_match_pct": dup})
        print(f"  {name:<28}{cov:>10.4f}{dup:>12.4f}")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "file": file.name, "rows": rows, "gap_threshold_s": gap,
        "n_events": len(intervals), "n_trace_rows": len(traces),
        "scenarios": results,
        "interpretation": [
            "無偏移時覆蓋率應為 100%：代表事件切分與 trace 歸屬一致（baseline）",
            "時鐘偏移 > 取樣間隔（4 秒）時，覆蓋率會掉 → 說明對齊 QC 抓得到錯位",
            "加上 buffer 後覆蓋率回升，但 buffer 太大會讓相鄰事件互相重疊 → 重複匹配率上升",
            "因此 buffer 要取「時鐘偏移量級」與「事件間隔」之間的折衷，並用本表量化",
        ],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  報告：{out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="對齊品質檢查")
    ap.add_argument("--db", action="store_true", help="從資料庫檢查（需資料庫）")
    ap.add_argument("--simulate", action="store_true", help="時鐘偏移壓力測試（不需資料庫）")
    ap.add_argument("--file")
    ap.add_argument("--rows", type=int, default=300_000)
    ap.add_argument("--gap", type=float, default=20.0)
    ap.add_argument("--out", default="reports/m2/alignment_skew_test.json")
    add_version_arg(ap)
    args = ap.parse_args()

    if args.db:
        return run_db()
    if args.simulate:
        if not args.file:
            print("[錯誤] --simulate 需要 --file")
            return 2
        return run_simulate(Path(args.file), args.gap, args.rows, Path(args.out))
    print("請指定 --db 或 --simulate")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
