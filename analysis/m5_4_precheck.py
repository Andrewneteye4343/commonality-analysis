"""M5-4-0 前置診斷：兩層串接（FDC 警報 → 共同性分析）的可行性。

對應 docs/M5_4_PLAN.md 的 M5-4-0。唯讀，不寫任何表。
任一關不成立就停手回報，不硬做。

  [A] 可聚合性：事件 → 批次（lot）→ 機台；批次是否跨機台（會影響聚合語意）
  [B] 訊號量：有多少批次帶警報、每個帶警報批次有幾個警報
      （太少＝檢定力不足；太多＝沒有鑑別力，兩者都要看得見）
  [C] 洩漏檢查：① 警報是否全部對得上事件與批次；② 警報時刻是否落在該批次的時間範圍內；
      ③ 聚合只使用「該批次自己的警報」（以測試機製化，見 tests）
  [D] 依賴性：同一批次內的警報是否集中在少數感測器（共因相關 → 超幾何的獨立性假設會被違反）
      → 結論的主要證據要用「跨方法一致性」，不是 p 值
  [E] 可行性預覽（不是驗收）：帶警報批次 × 批次不良 的 2×2 與 Fisher 檢定
      （判準已在計畫中寫死，這裡只是先看有沒有可能性）

用法
    docker compose run --rm etl python analysis/m5_4_precheck.py --run-id M5_mspc_b0.3
    python analysis/m5_4_precheck.py --alarms-csv ... --events-csv ... --lots-csv ... --no-db
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

import numpy as np                                          # noqa: E402

import commonality_basic as cb                              # noqa: E402
import run_fdc                                              # noqa: E402
import script_version                                        # noqa: E402
from script_version import add_version_arg                   # noqa: E402

FEATURE_COUNT_TOLERANCE = 0.5        # 一個批次的警報比例過半 → 鑑別力可疑，要提醒


# ── 純函式（可單元測試）─────────────────────────────────────────
def parse_sensor(top1: str) -> str:
    """從 M5-2 的 top1 欄位取出感測器名（格式 `SENSOR|feature`）。"""
    if not top1:
        return ""
    return top1.split("|")[0].strip().upper()


def aggregate_alarms_per_lot(alarm_rows: list) -> dict:
    """把警報聚合到批次；**只使用該批次自己的警報**。

    alarm_rows：每列至少含 {"lot_id", "event_id", "sensor"}
    ⚠ 這裡不做任何跨批次的聚合或時間比較——那是 M5-4-1 的定義，
      但「不得用其他批次的資訊」這件事必須在這一層就成立（洩漏防線）。
    """
    out: dict = defaultdict(lambda: {"n_alarms": 0, "sensors": set(), "events": set()})
    for row in alarm_rows:
        lot = row.get("lot_id")
        if lot is None or lot == "":
            continue
        d = out[lot]
        d["n_alarms"] += 1
        d["events"].add(row.get("event_id"))
        s = parse_sensor(row.get("sensor") or row.get("top1") or "")
        if s:
            d["sensors"].add(s)
    return {lot: {"n_alarms": d["n_alarms"], "n_sensors": len(d["sensors"]),
                  "sensors": sorted(d["sensors"]), "n_events": len(d["events"])}
            for lot, d in out.items()}


def dependency_summary(per_lot: dict) -> dict:
    """同一批次內警報的感測器集中程度（共因相關的證據）。

    n_sensors == 1 的批次佔比越高 → 警報集中在單一感測器（相關性強）；
    反之分散在多感測器 → 較接近獨立（超幾何假設較合理）。
    """
    lots = [v for v in per_lot.values() if v["n_alarms"] > 0]
    if not lots:
        return {"n_lots": 0}
    ns = np.array([v["n_sensors"] for v in lots], dtype=float)
    na = np.array([v["n_alarms"] for v in lots], dtype=float)
    return {"n_lots": len(lots),
            "frac_single_sensor": float(np.mean(ns <= 1)),
            "mean_sensors": float(ns.mean()),
            "mean_alarms_per_lot": float(na.mean()),
            "median_alarms_per_lot": float(np.median(na))}


def per_tool_rates_lot_level(alerted_lots: set, membership: list) -> dict:
    """寬鬆歸屬（M3 同語意）：批次的警報記在它經過的每一台機台上。

    membership：[(lot_id, tool_id, n_events), ...]
    ⚠ 這個定義會**稀釋**：當 55% 的批次經手多台機台時，
      各機台的比率會全部收斂到全域的「批次帶警報率」，機台之間分不開。
    """
    out: dict = defaultdict(lambda: [0, 0])
    for lot, tool, _n in membership:
        out[tool][1] += 1
        if lot in alerted_lots:
            out[tool][0] += 1
    return {t: {"alerted": v[0], "total": v[1],
                "rate": (v[0] / v[1] if v[1] else None)} for t, v in out.items()}


def per_tool_rates_strict(alarms_by_lot_tool: dict, membership: list) -> dict:
    """嚴格歸屬：警報只記在**真正發生警報的那台機台**上。

    分母同樣是「該機台經手的批次」（否則會把沒經過的批次也算進來）。
    這才是「哪台機台產生警報」的公平檢定。
    """
    denom: dict = defaultdict(int)
    for lot, tool, _n in membership:
        denom[tool] += 1
    num: dict = defaultdict(int)
    for (_lot, tool), _c in alarms_by_lot_tool.items():
        num[tool] += 1
    return {t: {"alerted": num.get(t, 0), "total": denom[t],
                "rate": (num.get(t, 0) / denom[t] if denom[t] else None)}
            for t in denom}


def rank_table(wide: dict, strict: dict) -> list:
    """把兩種歸屬並列成排名表（供人工判讀；兩種都要報，不可只報好看的那個）。"""
    tools = sorted(set(wide) | set(strict))
    rows = []
    for t in tools:
        w = wide.get(t, {}).get("rate")
        st = strict.get(t, {}).get("rate")
        rows.append({"tool": t, "rate_wide": w, "rate_strict": st,
                     "alerted_wide": wide.get(t, {}).get("alerted"),
                     "alerted_strict": strict.get(t, {}).get("alerted"),
                     "total": wide.get(t, {}).get("total")})
    rows.sort(key=lambda r: -(r["rate_strict"] if r["rate_strict"] is not None else -1))
    for i, r in enumerate(rows, 1):
        r["rank_strict"] = i
    rows_w = sorted(rows, key=lambda r: -(r["rate_wide"] if r["rate_wide"] is not None else -1))
    for i, r in enumerate(rows_w, 1):
        r["rank_wide"] = i
    return rows


def contingency_alert_vs_outcome(alerted: bool, outcome_bad: bool,
                                 tot_lots: int, tot_bad: int, tot_alerted: int) -> dict:
    """(帶警報 × 批次不良) 的 2×2 與單尾 Fisher 檢定（沿用 M3 的實作）。

    單尾方向：警報是否**富集於**不良批次（greater）。
    """
    a = int(alerted and outcome_bad)          # 警報 ∧ 不良
    b = int(alerted and not outcome_bad)      # 警報 ∧ 良品
    c = int((not alerted) and outcome_bad)
    d = int((not alerted) and (not outcome_bad))
    p = cb.fisher_exact_greater(a, b, c, d)
    try:
        orv, _, _ = cb.odds_ratio(a, b, c, d)
    except Exception:
        orv = float("nan")
    return {"a": a, "b": b, "c": c, "d": d, "fisher_p_greater": float(p),
            "odds_ratio": float(orv)}


def build_2x2(alerted_lots: set, bad_lots: set, all_lots: set) -> dict:
    """由三個集合建出 2×2 並檢定（批次層）。"""
    a = len(alerted_lots & bad_lots)
    b = len(alerted_lots - bad_lots)
    c = len(bad_lots - alerted_lots)
    d = len(all_lots - alerted_lots - bad_lots)
    p = cb.fisher_exact_greater(a, b, c, d)
    orv, lo, hi, valid = cb.or_ci_woolf(a, b, c, d)
    return {"a_alert_bad": a, "b_alert_good": b, "c_noalert_bad": c, "d_noalert_good": d,
            "fisher_p_greater": float(p), "odds_ratio": float(orv),
            "or_ci": [float(lo), float(hi)], "or_ci_corrected": bool(valid)}


# ── 資料讀取 ────────────────────────────────────────────────────
def connect():
    import psycopg
    return psycopg.connect(host=os.environ.get("PGHOST", "db"),
                           port=int(os.environ.get("PGPORT", 5432)),
                           dbname=os.environ.get("PGDATABASE", "commonality"),
                           user=os.environ["PGUSER"],
                           password=os.environ.get("PGPASSWORD", ""))


def load_from_db(conn, run_id: str, robust: bool, window_hours: int) -> dict:
    cur = conn.cursor()
    # [A] 批次與機台
    cur.execute("""
        SELECT e.lot_id, count(DISTINCT e.tool_id) AS n_tools, count(*) AS n_events
        FROM stg.fact_process_event e GROUP BY e.lot_id
    """)
    lots = {r[0]: {"n_tools": int(r[1]), "n_events": int(r[2])} for r in cur.fetchall()}
    # [A] 批次層標籤（M3 用的既有表）
    cur.execute("""SELECT lot_id, tool_id, outcome_bad, n_events FROM mart.lot_units
                   WHERE fault_type = 'ANY' AND window_hours = %s""", (window_hours,))
    units = {r[0]: {"tool_id": r[1], "outcome_bad": bool(r[2]), "n_events": int(r[3])}
             for r in cur.fetchall()}
    # [B/C] 警報（含 event → lot → 時刻）
    cur.execute("""
        SELECT a.event_id, a.tool_id, a.in_ts::double precision, a.top1, e.lot_id,
               e.lot_id IS NULL AS orphan
        FROM mart.mspc_alerts a
        LEFT JOIN stg.fact_process_event e ON e.event_id = a.event_id
        WHERE a.run_id = %s AND a.robust = %s
    """, (run_id, robust))
    alarms = [{"event_id": r[0], "tool_id": r[1], "in_ts": r[2], "top1": r[3],
               "lot_id": r[4], "orphan": bool(r[5])} for r in cur.fetchall()]
    # [C] 批次的時間範圍：**必須用事實表直接算** ✗ 不可用 mart.lot_units
    #     —— lot_units 是故障窗標籤表（first_ts/last_ts 屬窗內語意），
    #     實際踩到 39/1879 筆「警報落在批次範圍外」的假警報。
    cur.execute("""SELECT lot_id, min(in_ts)::double precision, max(in_ts)::double precision
                   FROM stg.fact_process_event GROUP BY lot_id""")
    spans = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    # membership：(批次 → 機台) 以及該機台在該批次的事件數（M3 的既有結構）
    cur.execute("""SELECT lot_id, group_key, n_events FROM mart.lot_membership
                   WHERE group_type = 'tool'""")
    membership = [(r[0], r[1], int(r[2])) for r in cur.fetchall()]
    # (批次 × 機台) 嚴格歸屬版：只用該機台在該批次的警報（敏感度分析用）
    cur.execute("""
        SELECT e.lot_id, a.tool_id, count(*) FROM mart.mspc_alerts a
        JOIN stg.fact_process_event e ON e.event_id = a.event_id
        WHERE a.run_id = %s AND a.robust = %s GROUP BY e.lot_id, a.tool_id
    """, (run_id, robust))
    alarms_by_lot_tool = {(r[0], r[1]): int(r[2]) for r in cur.fetchall()}
    return {"lots": lots, "units": units, "alarms": alarms, "spans": spans,
            "membership": membership, "alarms_by_lot_tool": alarms_by_lot_tool}


def load_from_csv(alarms_csv: Path, events_csv: Path, lots_csv: Path) -> dict:
    lot_of, tool_of, ts_of = {}, {}, {}
    with events_csv.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            eid = int(row["event_id"])
            lot_of[eid] = row["lot_id"]
            tool_of[eid] = row.get("tool_id", "")
            if row.get("in_ts"):
                ts_of[eid] = float(row["in_ts"])
    lots, units = defaultdict(lambda: {"n_tools": set(), "n_events": 0}), {}
    for eid, lot in lot_of.items():
        lots[lot]["n_tools"].add(tool_of.get(eid, ""))
        lots[lot]["n_events"] += 1
    lots = {k: {"n_tools": len(v["n_tools"]), "n_events": v["n_events"]} for k, v in lots.items()}
    spans_ev: dict = defaultdict(lambda: [float("inf"), float("-inf")])
    for eid, t in ts_of.items():
        lo, hi = spans_ev[lot_of[eid]]
        spans_ev[lot_of[eid]] = [min(lo, t), max(hi, t)]
    with lots_csv.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            units[row["lot_id"]] = {"tool_id": row["tool_id"],
                                    "outcome_bad": row["outcome_bad"].strip().lower()
                                    in ("1", "true", "t"),
                                    "n_events": int(row["n_events"])}
    # [C]② 的批次範圍：優先由事件檔的 in_ts 直接算（與 DB 模式同語意）；
    # 事件檔沒有 in_ts 就沒有可信來源 → 寧可回報「未檢查」，不要用錯的表硬算。
    spans = {k: (v[0], v[1]) for k, v in spans_ev.items()} if ts_of else {}
    alarms, alarms_by_lot_tool = [], defaultdict(int)
    with alarms_csv.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            eid = int(row["event_id"])
            lot = lot_of.get(eid)
            tool = row.get("tool_id") or tool_of.get(eid, "")
            alarms.append({"event_id": eid, "tool_id": tool,
                           "in_ts": float(row["in_ts"]) if row.get("in_ts") else None,
                           "top1": row.get("top1", ""), "lot_id": lot,
                           "orphan": eid not in lot_of})
            if lot:
                alarms_by_lot_tool[(lot, tool)] += 1
    # membership 必須是【每個 (批次, 機台) 一列】——與 mart.lot_membership 同語意。
    # ⚠ 先前寫成每個事件一列 → 嚴格歸屬版的分母變成事件數（實測 7.8% 應為 46.7%）。
    m_counts: dict = defaultdict(int)
    for eid, lot in lot_of.items():
        m_counts[(lot, tool_of.get(eid, ""))] += 1
    membership = [(lot, tool, n) for (lot, tool), n in m_counts.items()]
    return {"lots": lots, "units": units, "alarms": alarms, "spans": spans,
            "membership": membership, "alarms_by_lot_tool": dict(alarms_by_lot_tool)}


def main() -> int:
    ap = argparse.ArgumentParser(description="M5-4-0 兩層串接前置診斷（唯讀）")
    ap.add_argument("--run-id", default="M5_mspc_b0.3")
    ap.add_argument("--robust", type=int, default=1, choices=(0, 1),
                    help="用 robust 版（1，預設）或 plain 版（0）的警報")
    ap.add_argument("--window-hours", type=int, default=24)
    ap.add_argument("--alarms-csv", default="")
    ap.add_argument("--events-csv", default="")
    ap.add_argument("--lots-csv", default="")
    ap.add_argument("--no-db", action="store_true")
    ap.add_argument("--report", default="reports/m5/m5_4_precheck.json")
    add_version_arg(ap)
    args = ap.parse_args()

    report = {"version": script_version.current_version(), "run_id": args.run_id,
              "robust": bool(args.robust), "window_hours": args.window_hours}

    if args.alarms_csv:
        print(f"[模式] CSV（助理端驗證）")
        data = load_from_csv(Path(args.alarms_csv), Path(args.events_csv), Path(args.lots_csv))
    else:
        conn = connect()
        print(f"[模式] 資料庫｜run_id={args.run_id}｜robust={bool(args.robust)}")
        data = load_from_db(conn, args.run_id, bool(args.robust), args.window_hours)
        conn.close()

    lots, units, alarms, spans = data["lots"], data["units"], data["alarms"], data["spans"]
    membership = data.get("membership", [])
    alarms_by_lot_tool = data.get("alarms_by_lot_tool", {})

    # ── [A] 可聚合性 ────────────────────────────────────────────
    multi = {k: v for k, v in lots.items() if v["n_tools"] > 1}
    print(f"\n[A] 可聚合性")
    print(f"    批次 {len(lots):,} 個｜事件 {sum(v['n_events'] for v in lots.values()):,}")
    print(f"    跨機台的批次：{len(multi)} 個（{len(multi) / max(len(lots), 1):.1%}）")
    if multi:
        print("    ⚠ 這不是資料異常：PHM 的 lot_id 是全域的，同一批次會經過多台機台。")
        print("      正確語意是【沿用 M3】：unit＝批次（含結果標籤）、membership＝(批次→機台)")
        print("      —— M3 的驗收就是用這個結構通過的；副作用是稀釋，需在結論中揭露。")
    else:
        print("    （本批資料沒有跨機台批次）")
    if multi:
        for k in list(multi)[:5]:
            print(f"      {k}：{multi[k]['n_tools']} 台、{multi[k]['n_events']} 事件")
    report["a_aggregation"] = {"n_lots": len(lots), "n_multi_tool_lots": len(multi),
                               "n_events": sum(v["n_events"] for v in lots.values())}

    # ── [B] 訊號量 ──────────────────────────────────────────────
    per_lot = aggregate_alarms_per_lot(
        [{"lot_id": a["lot_id"], "event_id": a["event_id"], "sensor": a["top1"]}
         for a in alarms if a["lot_id"]])
    alerted = set(per_lot)
    all_lots = set(lots)
    print(f"\n[B] 訊號量")
    print(f"    警報列 {len(alarms):,}｜帶警報的批次 {len(alerted):,}/{len(all_lots):,}"
          f"（{len(alerted) / max(len(all_lots), 1):.1%}）")
    na = np.array([v["n_alarms"] for v in per_lot.values()], dtype=float) if per_lot else np.array([])
    if na.size:
        print(f"    每個帶警報批次的警報數：中位 {np.median(na):.0f}、"
              f"p90 {np.percentile(na, 90):.0f}、最大 {na.max():.0f}")
    frac_alert = np.array([per_lot[k]["n_alarms"] / max(lots[k]["n_events"], 1)
                           for k in per_lot], dtype=float)
    if frac_alert.size:
        print(f"    批次內警報比例：中位 {np.median(frac_alert):.3f}、"
              f"超過 {FEATURE_COUNT_TOLERANCE:.0%} 的批次 "
              f"{int((frac_alert > FEATURE_COUNT_TOLERANCE).sum())} 個"
              f"{'（鑑別力可疑，需提醒）' if (frac_alert > 0.5).any() else ''}")
    by_tool = defaultdict(lambda: [0, 0])
    for lot, tool, _n in membership:
        by_tool[tool][1] += 1
        if lot in alerted:
            by_tool[tool][0] += 1
    print("    逐機台（unit＝批次、membership 來自 mart.lot_membership）"
          "：帶警報批次／該機台經手的批次")
    for tool in sorted(by_tool):
        n_a, n_t = by_tool[tool]
        print(f"      {tool}：{n_a}/{n_t}（{n_a / max(n_t, 1):.1%}）")
    if len(multi) > 0:
        share = len(multi) / max(len(lots), 1)
        print(f"    ⚠ 這是 M3 已處理過的結構：{share:.1%} 的批次經手多台機台 →"
              f" 一個批次的警報會『記在它經過的每一台機台』（M3 同語意）")
        print("      → 副作用是稀釋：真正的肇事機台的分數會被同時經手的機台攤薄")
        print("      → M5-4-3 會另跑一個 (批次 × 機台) 嚴格歸屬版當敏感度分析")
    report["b_signal"] = {"n_alarms": len(alarms), "n_alerted_lots": len(alerted),
                          "n_lots": len(all_lots),
                          "by_tool": {k: {"alerted": v[0], "total": v[1]}
                                      for k, v in sorted(by_tool.items())}}

    # ── [C] 洩漏檢查 ────────────────────────────────────────────
    orphans = [a for a in alarms if a["orphan"]]
    outside = 0
    checked = 0
    for a in alarms:
        sp = spans.get(a["lot_id"]) if a["lot_id"] else None
        if sp and a["in_ts"] is not None:
            checked += 1
            if not (sp[0] - 1e-9 <= a["in_ts"] <= sp[1] + 1e-9):
                outside += 1
    print(f"\n[C] 洩漏檢查")
    print(f"    ① 警報對不到事件的列數：{len(orphans)}"
          f"{'（必須為 0 ✗）' if orphans else '（✓）'}")
    print(f"    ② 警報時刻落在該批次時間範圍外：{outside}/{checked}"
          f"{'（必須為 0 ✗）' if outside else '（✓）'}")
    print("    ③ 聚合只使用「該批次自己的警報」→ 由 tests 的"
          "test_aggregation_ignores_other_lots 機製化保證")
    report["c_leakage"] = {"n_orphan_alarms": len(orphans),
                           "n_time_outside_lot": outside, "n_time_checked": checked}

    # ── [D] 依賴性 ──────────────────────────────────────────────
    dep = dependency_summary(per_lot)
    print(f"\n[D] 依賴性（共因相關）")
    if dep.get("n_lots"):
        print(f"    帶警報批次 {dep['n_lots']:,}｜平均警報數 {dep['mean_alarms_per_lot']:.1f}"
              f"（中位 {dep['median_alarms_per_lot']:.0f}）")
        print(f"    只用 1 個感測器的批次佔比：{dep['frac_single_sensor']:.1%}"
              f"｜平均感測器數 {dep['mean_sensors']:.2f}")
        print("    → 佔比越高＝警報越集中在少數感測器＝相關性越強"
              "（超幾何的獨立性假設越不可靠）")
        print("    → 因此 M5-4 的主要證據用「跨方法一致性」，p 值只當輔助")
    report["d_dependency"] = dep

    # ── [E] 可行性預覽（不是驗收）────────────────────────────────
    print(f"\n[E] 可行性預覽（不是驗收；判準已在計畫中寫死）")
    bad = {lot for lot, v in units.items() if v.get("outcome_bad")}
    common = all_lots & set(units)
    if common:
        tab = build_2x2(alerted & common, bad & common, common)
        print(f"    2×2（帶警報 × 批次不良）：a={tab['a_alert_bad']} b={tab['b_alert_good']} "
              f"c={tab['c_noalert_bad']} d={tab['d_noalert_good']}")
        # ⚠ or_ci_woolf 的第 4 個回傳值是「是否做了連續性校正」（有 0 細胞時），
        #    不是「不可靠」。先前的註記邏輯寫反了 ✗（我在非校正情況印出「小細胞數」）。
        ci_txt = (f"95% CI {tab['or_ci'][0]:.3f}–{tab['or_ci'][1]:.3f}"
                  + ("（有 0 細胞 → 已加 0.5 連續性校正，區間為近似值）"
                     if tab["or_ci_corrected"] else ""))
        print(f"    Fisher（單尾 greater）p={tab['fisher_p_greater']:.4g}｜"
              f"OR={tab['odds_ratio']:.3f}｜{ci_txt}")
        print("    ⚠ 這只是可行性預覽：真正的驗收是 M5-4-2 的機台層 enrichment 排名"
              "（負控制 04M01 不得進前 2 名）")
        report["e_preview"] = tab
    else:
        print("    （批次層標籤表與事件表沒有共同批次 → 無法預覽）")

    # ── [F] 兩種歸屬的機台排名對照（給 M5-4-1 決定主分析用哪一種）──
    wide = per_tool_rates_lot_level(alerted, membership)
    strict = per_tool_rates_strict(alarms_by_lot_tool, membership)
    rows_rank = rank_table(wide, strict)
    print(f"\n[F] 逐機台比率：寬鬆歸屬（M3 同語意）vs 嚴格歸屬（只記真正發生警報的機台）")
    print(f"    {'機台':<8}{'寬鬆':>10}{'排名':>6}{'嚴格':>10}{'排名':>6}{'經手批次':>10}")
    for r in rows_rank:
        w = "—" if r["rate_wide"] is None else f"{r['rate_wide']:.1%}"
        st = "—" if r["rate_strict"] is None else f"{r['rate_strict']:.1%}"
        print(f"    {r['tool']:<8}{w:>10}{r['rank_wide']:>6}{st:>10}{r['rank_strict']:>6}"
              f"{r['total']:>10,}")
    if len(rows_rank) >= 3:
        rs = [r["rate_strict"] for r in rows_rank if r["rate_strict"] is not None]
        rw = [r["rate_wide"] for r in rows_rank if r["rate_wide"] is not None]
        print(f"    寬鬆版的第 2～{len(rw)} 名差距："
              f"{(max(rw) - min(rw)) * 100:.2f} 個百分點（差越小＝稀釋越嚴重）")
        print(f"    嚴格版的第 2～{len(rs)} 名差距："
              f"{(max(rs) - min(rs)) * 100:.2f} 個百分點")
    print("    判讀：兩版都要報；判準（04M01 不得進前 2、真故障機台至少一台進前 2）不變。")
    print("          若只有嚴格版成立 → 必須明說「寬鬆版因稀釋而失效」，不可只報嚴格版。")
    report["f_attribution"] = rows_rank

    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  報告：{out}")
    print("  判讀：① [A] 若批次跨機台 → 先定義聚合語意，不要硬做")
    print("        ② [B] 帶警報批次過少（<5%）或比例過高（>50%）→ 檢定力或鑑別力有問題")
    print("        ③ [C] 任何一項不是 0 → 這是 bug，不是資料結論")
    print("        ④ [D] 單感測器佔比高 → 以一致性為主證據")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
