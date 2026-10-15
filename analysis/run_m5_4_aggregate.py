"""M5-4-1：兩層串接的第一層聚合（事件層 FDC/MSPC 警報 → 批次層特徵）。

產出 `mart.lot_alert_features`，兩種歸屬都落表：
  unit_kind='lot'      寬鬆（與 M3 同語意）：批次的警報記在它經過的每一台機台
  unit_kind='lot_tool' 嚴格：警報只記在真正發生警報的那台機台

為什麼要兩種（M5-4-0 實測）：PHM 的 lot_id 是全域的，55.0% 的批次經手多台機台 →
寬鬆版會被稀釋（第 2～5 名差距 1.1 個百分點），嚴格版拉開到 3.7 倍。
判準已在 docs/M5_4_PLAN.md 寫死，**兩版都要報告**，不可只報好看的那一個。

洩漏防線：每個單位的特徵只由「該單位自己的事件與警報」算出
（由 tests 機製化驗證：改動別的批次 → 本批次結果不變）。

用法
    docker compose run --rm etl python analysis/run_m5_4_aggregate.py --run-id M5_mspc_b0.3
    python analysis/run_m5_4_aggregate.py --events-csv ... --alarms-csv ... --out-csv ... --no-db
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent / "etl", _HERE.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import run_fdc                                              # noqa: E402
import script_version                                        # noqa: E402
from script_version import add_version_arg                   # noqa: E402

KINDS = ("lot", "lot_tool")


# ── 純函式（可單元測試）─────────────────────────────────────────
def parse_sensor(top1: str) -> str:
    return (top1 or "").split("|")[0].strip().upper()


def relative_position(sorted_ts: list, target_ts: float) -> float:
    """target_ts 在該單位事件序列中的相對位置（0–1）；只有一個事件時回 0.0。"""
    if not sorted_ts:
        return 0.0
    n = len(sorted_ts)
    if n == 1:
        return 0.0
    lo, hi = sorted_ts[0], sorted_ts[-1]
    if hi <= lo:
        return 0.0
    return float(min(max((target_ts - lo) / (hi - lo), 0.0), 1.0))


def build_units(events: list, alarms: list, kind: str) -> tuple:
    """把事件與警報聚合成單位。

    events：[{"event_id","lot_id","tool_id","in_ts"}…]
    alarms：[{"event_id","tool_id","in_ts","sensor"}…]（sensor 可為空）
    kind  ：'lot'（寬鬆）或 'lot_tool'（嚴格）
    ⚠ 一個單位只使用自己的事件與警報 —— 不得跨單位借用（洩漏防線）。
    回傳 (units, stats)。
    """
    assert kind in KINDS, kind
    ev_by_id = {e["event_id"]: e for e in events}
    ts_by_unit: dict = defaultdict(list)
    events_of: dict = defaultdict(list)
    for e in events:
        key = (e["lot_id"], e["tool_id"]) if kind == "lot_tool" else (e["lot_id"], "")
        ts_by_unit[key].append(float(e["in_ts"]))
        events_of[key].append(e)

    units: dict = {}
    for key, eids in events_of.items():
        ts_sorted = sorted(ts_by_unit[key])
        units[key] = {"lot_id": key[0], "tool_id": key[1], "n_events": len(eids),
                      "has_alarm": False, "n_alarm_events": 0, "n_alarms": 0,
                      "sensors": set(), "first_alarm_ts": None, "last_alarm_ts": None,
                      "first_alarm_pos": None, "_ts_sorted": ts_sorted}

    n_orphan = 0
    for a in alarms:
        e = ev_by_id.get(a["event_id"])
        if e is None:
            n_orphan += 1                      # 對不上事件 → 明列，不可靜默丟棄
            continue
        key = (e["lot_id"], e["tool_id"]) if kind == "lot_tool" else (e["lot_id"], "")
        u = units.get(key)
        if u is None:
            n_orphan += 1
            continue
        u["has_alarm"] = True
        u["n_alarms"] += 1
        # n_alarm_events（去重）在第二輪算，避免在這裡做 O(n^2) 的集合運算
        s = parse_sensor(a.get("sensor") or "")
        if s:
            u["sensors"].add(s)
        t = a.get("in_ts")
        if t is not None:
            t = float(t)
            u["first_alarm_ts"] = t if u["first_alarm_ts"] is None else min(u["first_alarm_ts"], t)
            u["last_alarm_ts"] = t if u["last_alarm_ts"] is None else max(u["last_alarm_ts"], t)

    # n_alarm_events 需要去重，改在第二輪算（上面那個 list comprehension 是無效的）
    alarm_events_by_key: dict = defaultdict(set)
    for a in alarms:
        e = ev_by_id.get(a["event_id"])
        if e is None:
            continue
        key = (e["lot_id"], e["tool_id"]) if kind == "lot_tool" else (e["lot_id"], "")
        if key in units:
            alarm_events_by_key[key].add(a["event_id"])

    for key, u in units.items():
        u["n_alarm_events"] = len(alarm_events_by_key.get(key, ()))
        if u["has_alarm"] and u["first_alarm_ts"] is not None:
            u["first_alarm_pos"] = relative_position(u["_ts_sorted"], u["first_alarm_ts"])
        else:
            u["first_alarm_pos"] = None
        u["sensors"] = ",".join(sorted(u["sensors"]))
        u["n_sensors"] = len(u["sensors"].split(",")) if u["sensors"] else 0
        del u["_ts_sorted"]
    stats = {"unit_kind": kind, "n_units": len(units), "n_orphan_alarms": n_orphan}
    return units, stats


def rows_for_copy(units: dict, run_id: str, kind: str, outcome_by_lot: dict) -> list:
    """轉成 COPY 用的列（欄位順序必須與 migration 011 一致）。"""
    rows = []
    for key in sorted(units):
        u = units[key]
        rows.append([run_id, kind, u["lot_id"], u["tool_id"], u["n_events"],
                     u["has_alarm"], u["n_alarm_events"], u["n_alarms"], u["n_sensors"],
                     u["sensors"], u["first_alarm_ts"], u["last_alarm_ts"],
                     u["first_alarm_pos"], outcome_by_lot.get(u["lot_id"])])
    return rows


def summarize(units: dict, kind: str) -> dict:
    n = len(units)
    alarm = [u for u in units.values() if u["has_alarm"]]
    by_tool: dict = defaultdict(lambda: [0, 0])
    for u in units.values():
        t = u["tool_id"] or "(批次層)"
        by_tool[t][1] += 1
        if u["has_alarm"]:
            by_tool[t][0] += 1
    return {"unit_kind": kind, "n_units": n, "n_alarmed": len(alarm),
            "alarmed_share": (len(alarm) / n if n else None),
            "by_tool": {t: {"alarmed": v[0], "total": v[1]} for t, v in sorted(by_tool.items())}}


# ── 資料讀取與寫入 ──────────────────────────────────────────────
def connect():
    import psycopg
    return psycopg.connect(host=os.environ.get("PGHOST", "db"),
                           port=int(os.environ.get("PGPORT", 5432)),
                           dbname=os.environ.get("PGDATABASE", "commonality"),
                           user=os.environ["PGUSER"],
                           password=os.environ.get("PGPASSWORD", ""))


def load_db(conn, run_id: str, robust: bool, tools: list) -> tuple:
    cur = conn.cursor()
    cur.execute("""
        SELECT event_id, lot_id, tool_id, in_ts::double precision
        FROM stg.fact_process_event
        WHERE (%s = ARRAY[]::text[] OR tool_id = ANY(%s))
        ORDER BY lot_id, tool_id, in_ts, event_id
    """, (tools, tools))
    events = [{"event_id": r[0], "lot_id": r[1], "tool_id": r[2], "in_ts": r[3]}
              for r in cur.fetchall()]
    cur.execute("""
        SELECT event_id, tool_id, in_ts::double precision, top1
        FROM mart.mspc_alerts WHERE run_id = %s AND robust = %s
    """, (run_id, robust))
    alarms = [{"event_id": r[0], "tool_id": r[1], "in_ts": r[2], "sensor": r[3]}
              for r in cur.fetchall()]
    cur.execute("""SELECT lot_id, bool_or(outcome_bad) FROM mart.lot_units
                   WHERE fault_type = 'ANY' GROUP BY lot_id""")
    outcome_by_lot = {r[0]: bool(r[1]) for r in cur.fetchall()}
    return events, alarms, outcome_by_lot


def write_db(conn, rows: list, run_id: str, kind: str) -> int:
    cur = conn.cursor()
    cur.execute("DELETE FROM mart.lot_alert_features WHERE run_id = %s AND unit_kind = %s",
                (run_id, kind))
    if not rows:
        conn.commit()
        return 0
    with cur.copy("""COPY mart.lot_alert_features (run_id, unit_kind, lot_id, tool_id,
                     n_events, has_alarm, n_alarm_events, n_alarms, n_sensors, sensors,
                     first_alarm_ts, last_alarm_ts, first_alarm_pos, outcome_bad)
                     FROM STDIN""") as cp:
        for r in rows:
            cp.write_row(r)
    conn.commit()
    return len(rows)


def load_csv(events_csv: Path, alarms_csv: Path) -> tuple:
    events = []
    with events_csv.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            events.append({"event_id": int(row["event_id"]), "lot_id": row["lot_id"],
                           "tool_id": row["tool_id"], "in_ts": float(row["in_ts"])})
    alarms = []
    if alarms_csv:
        with alarms_csv.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                alarms.append({"event_id": int(row["event_id"]),
                               "tool_id": row.get("tool_id", ""),
                               "in_ts": float(row["in_ts"]) if row.get("in_ts") else None,
                               "sensor": row.get("top1", "")})
    return events, alarms, {}


def main() -> int:
    ap = argparse.ArgumentParser(description="M5-4-1 警報聚合 → mart.lot_alert_features")
    ap.add_argument("--run-id", default="M5_mspc_b0.3")
    ap.add_argument("--robust", type=int, default=1, choices=(0, 1))
    ap.add_argument("--tools", default="", help="留空＝全部機台")
    ap.add_argument("--events-csv", default="")
    ap.add_argument("--alarms-csv", default="")
    ap.add_argument("--out-csv", default="", help="助理端驗證：把結果寫成 CSV（不連資料庫）")
    ap.add_argument("--no-db", action="store_true")
    ap.add_argument("--report", default="reports/m5/m5_4_aggregate.json")
    add_version_arg(ap)
    args = ap.parse_args()

    report = {"version": script_version.current_version(), "run_id": args.run_id,
              "robust": bool(args.robust)}
    tools = list(run_fdc.parse_csv_list(args.tools)) if args.tools else []

    if args.events_csv:
        print(f"[模式] CSV（助理端驗證）")
        events, alarms, outcome_by_lot = load_csv(Path(args.events_csv),
                                                  Path(args.alarms_csv) if args.alarms_csv else None)
        conn = None
    else:
        conn = connect()
        print(f"[模式] 資料庫｜run_id={args.run_id}｜robust={bool(args.robust)}")
        events, alarms, outcome_by_lot = load_db(conn, args.run_id, bool(args.robust), tools)
    print(f"[輸入] 事件 {len(events):,}｜警報列 {len(alarms):,}"
          f"｜批次標籤 {len(outcome_by_lot):,}")

    for kind in KINDS:
        units, stats = build_units(events, alarms, kind)
        s = summarize(units, kind)
        rows = rows_for_copy(units, args.run_id, kind, outcome_by_lot)
        print(f"\n[{kind}] 單位 {s['n_units']:,}｜帶警報 {s['n_alarmed']:,}"
              f"（{(s['alarmed_share'] or 0):.1%}）｜孤兒警報 {stats['n_orphan_alarms']}")
        if kind == "lot_tool":
            for t, v in list(s["by_tool"].items())[:6]:
                print(f"      {t}：{v['alarmed']}/{v['total']}"
                      f"（{v['alarmed'] / max(v['total'], 1):.1%}）")
        if conn is not None:
            n = write_db(conn, rows, args.run_id, kind)
            print(f"      寫入 mart.lot_alert_features：{n:,} 列（run_id={args.run_id}）")
        elif args.out_csv:
            outp = Path(args.out_csv).with_name(Path(args.out_csv).stem + f"_{kind}.csv")
            outp.parent.mkdir(parents=True, exist_ok=True)
            with outp.open("w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["run_id", "unit_kind", "lot_id", "tool_id", "n_events",
                            "has_alarm", "n_alarm_events", "n_alarms", "n_sensors", "sensors",
                            "first_alarm_ts", "last_alarm_ts", "first_alarm_pos", "outcome_bad"])
                w.writerows(rows)
            print(f"      寫出 {outp}：{len(rows):,} 列")
        report[f"unit_{kind}"] = s
    if conn is not None:
        conn.close()

    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  報告：{out}")
    print("  提醒：兩種歸屬都要報告；判準見 docs/M5_4_PLAN.md（04M01 不得進前 2、"
          "真故障機台至少一台進前 2、與 M3／M5-2 方向一致）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
