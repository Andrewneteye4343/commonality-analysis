"""M5-4-2／M5-4-3：兩層串接的第二層共同性分析與判準評估。

第一層（M5-1／M5-2）產出事件層警報 → M5-4-1 聚合成批次層特徵
→ 本程式做第二層：**看排名是否收斂到真正故障的機台**。

兩種歸屬都跑、都報（判準見 docs/M5_4_PLAN.md）：
  ① 寬鬆（unit_kind='lot'）    ：批次的警報記在它經過的每一台機台（M3 同語意）
  ② 嚴格（unit_kind='lot_tool'）：警報只記在真正發生警報的機台

判準（動手前已寫死）
  1. 負控制：04M01（零故障）不得進前 2 名
  2. 命中  ：真故障機台至少一台進前 2 名
  3. 一致  ：與 M3（壞批次 OR）／M5-2（事件層警報率）的機台排序方向一致

⚠ 依賴性揭露：同一批次會出現在多台機台（55.0%）→ 各機台的 2×2 **不獨立** ✗
   → 主要證據是「跨方法一致性」，p 值只當輔助（M5-4-0 [D] 已量測）。

用法
    docker compose run --rm etl python analysis/run_m5_4_commonality.py --run-id M5_mspc_b0.3
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

import commonality_basic as cb                              # noqa: E402
import script_version                                        # noqa: E402
from script_version import add_version_arg                   # noqa: E402

CONTROL_TOOLS = ("04M01",)          # 零故障機台 = 負控制


# ── 純函式（可單元測試）─────────────────────────────────────────
def pick(stats: dict, *names) -> float:
    """從 M3 的 contingency_stats 取欄位；**缺鍵要大聲失敗，不可回傳 nan**。

    ⚠ 實際踩到：原本寫 st.get("or", nan) —— 但真正的鍵是 `odds_ratio`
      → 全部統計量靜默變成 nan，報表照樣產出、看起來像跑完了。
    """
    for n in names:
        if n in stats and stats[n] is not None:
            return float(stats[n])
    raise KeyError(f"contingency_stats 缺少預期的欄位 {names}；實際欄位：{sorted(stats)}")


def group_contingency(group_units: set, attr: set, all_units: set) -> dict:
    """M3 的表格語意（**列＝組內/組外、欄＝屬性有/無**）。

                屬性=有   屬性=無
    組內          a         b
    組外          c         d

    ⚠ 這一條前後錯了兩次，兩次都是靠單元測試的手算值抓到：
      ① 先把「非警報」當成 M3 的「好批次」欄填進 b/d ✗
         → 寬鬆版所有機台警報率變 100%，報表照樣印得出來（靜默）。
      ② 再把「屬性」放在列、把「壞批次」放在欄 ✗
         → 表格不完整（a+b ≠ 組大小），OR 的語意整個變調。
      現在：列＝群組、欄＝屬性；a+b 一定等於組大小、c+d 等於組外大小（測試會驗）。
      M3 的 rank_groups 就是這個結構（屬性＝批次是否不良）；
      M5-4 只把屬性換成『批次是否有警報』，統計機器完全不動 —— 這正是第二層。
    """
    gin, gout = group_units, all_units - group_units
    return {"a": len(gin & attr), "b": len(gin - attr),
            "c": len(gout & attr), "d": len(gout - attr)}


def stats_from_cells(cells: dict) -> dict:
    """由四個細胞算統計量（沿用 M3 的 contingency_stats，統計機器完全不動）。"""
    # 表格完整性：四個細胞必須覆蓋全體（a+b = 組內、c+d = 組外）
    n = cells["a"] + cells["b"] + cells["c"] + cells["d"]
    assert n > 0, "2×2 沒有任何單位"
    st = cb.contingency_stats(cells["a"], cells["b"], cells["c"], cells["d"])
    return {**cells, "in_group": cells["a"] + cells["b"],
            "bad_total": cells["a"] + cells["c"],
            "odds_ratio": pick(st, "odds_ratio", "or"),
            "p_value": pick(st, "p_fisher_greater", "p_hyper", "p_value"),
            "or_ci": [pick(st, "or_ci_low", "or_low"), pick(st, "or_ci_high", "or_high")]}


def rank_tools(per_tool: dict, metric: str = "rate") -> list:
    vals = [(t, v.get(metric)) for t, v in per_tool.items() if v.get(metric) is not None]
    return sorted(vals, key=lambda kv: -kv[1])


def order_agreement(orders: list) -> dict:
    """多個排序之間的一致性（機台只有 5 台，不宣稱統計檢定）。"""
    pairs = [{"i": i, "j": j, "same": list(orders[i]) == list(orders[j])}
             for i in range(len(orders)) for j in range(i + 1, len(orders))]
    return {"n_pairs": len(pairs), "n_same": sum(1 for p in pairs if p["same"]),
            "pairs": pairs, "note": "機台只有 5 台，不做統計檢定，只報排序是否相同"}


def evaluate_criteria(rank_wide: list, rank_strict: list, fault_tools: set,
                      control_tools: tuple = CONTROL_TOOLS) -> dict:
    """三條事前判準的機器化評估（判準不因結果而調整）。"""
    top2_w = [t for t, _ in rank_wide[:2]]
    top2_s = [t for t, _ in rank_strict[:2]]
    return {"1_control_not_top2": {
                "wide": not any(t in control_tools for t in top2_w), "wide_top2": top2_w,
                "strict": not any(t in control_tools for t in top2_s),
                "strict_top2": top2_s},
            "2_fault_tool_top2": {
                "wide": any(t in fault_tools for t in top2_w),
                "strict": any(t in fault_tools for t in top2_s)},
            "3_agreement": {}}


def compute_wide(data: dict) -> dict:
    """寬鬆版：unit＝批次；群組＝該機台經手的批次（M3 同語意，會稀釋）。"""
    has = data["alert_units"].get("lot", {})
    alerted = {lot for (lot, _t), v in has.items() if v}
    all_units = set(data["bad_lot"])
    bad = {lot for lot, b in data["bad_lot"].items() if b}
    by_tool: dict = defaultdict(set)
    for lot, tool in data["membership"]:
        if lot in all_units:
            by_tool[tool].add(lot)
    out = {}
    bad_all = {L for L, b in data["bad_lot"].items() if b}
    for tool, gin in by_tool.items():
        st = stats_from_cells(group_contingency(gin, alerted, all_units))
        st["total"] = len(gin)
        st["alerted"] = len(gin & alerted)
        st["n_bad_in_group"] = len(gin & bad_all)
        st["rate"] = (st["alerted"] / len(gin)) if gin else None
        out[tool] = st
    return out


def compute_strict(data: dict) -> dict:
    """嚴格版：unit＝(批次 × 機台)；警報只記真正發生的機台。"""
    has = data["alert_units"].get("lot_tool", {})
    all_units = set(has)
    alerted = {u for u, v in has.items() if v}
    bad = {u for u in all_units if data["bad_lot"].get(u[0])}
    by_tool: dict = defaultdict(set)
    for unit in all_units:
        by_tool[unit[1]].add(unit)
    out = {}
    for tool, gin in by_tool.items():
        st = stats_from_cells(group_contingency(gin, alerted, all_units))
        st["total"] = len(gin)
        st["alerted"] = len(gin & alerted)
        st["n_bad_in_group"] = len(gin & {u for u in all_units
                                          if data["bad_lot"].get(u[0])})
        st["rate"] = (st["alerted"] / len(gin)) if gin else None
        out[tool] = st
    return out


def compute_m3_reference(data: dict) -> dict:
    """M3 的對照（**結果面**：壞批次）——同一套統計與 membership，只換屬性。"""
    all_units = set(data["bad_lot"])
    bad = {lot for lot, b in data["bad_lot"].items() if b}
    by_tool: dict = defaultdict(set)
    for lot, tool in data["membership"]:
        if lot in all_units:
            by_tool[tool].add(lot)
    out = {}
    for tool, gin in by_tool.items():
        st = stats_from_cells(group_contingency(gin, bad, all_units))
        st["total"] = len(gin)
        st["n_bad_in_group"] = len(gin & bad)
        out[tool] = st
    return out


# ── 資料讀取 ────────────────────────────────────────────────────
def connect():
    import psycopg
    return psycopg.connect(host=os.environ.get("PGHOST", "db"),
                           port=int(os.environ.get("PGPORT", 5432)),
                           dbname=os.environ.get("PGDATABASE", "commonality"),
                           user=os.environ["PGUSER"],
                           password=os.environ.get("PGPASSWORD", ""))


def load(conn, run_id: str) -> dict:
    cur = conn.cursor()
    cur.execute("""SELECT unit_kind, lot_id, tool_id, has_alarm
                   FROM mart.lot_alert_features WHERE run_id = %s""", (run_id,))
    alert_units: dict = defaultdict(dict)
    for kind, lot, tool, has in cur.fetchall():
        alert_units[kind][(lot, tool)] = bool(has)
    cur.execute("""SELECT lot_id, bool_or(outcome_bad) FROM mart.lot_units
                   WHERE fault_type = 'ANY' GROUP BY lot_id""")
    bad_lot = {r[0]: bool(r[1]) for r in cur.fetchall()}
    cur.execute("""SELECT lot_id, group_key FROM mart.lot_membership
                   WHERE group_type = 'tool'""")
    membership = [(r[0], r[1]) for r in cur.fetchall()]
    cur.execute("SELECT tool_id, count(*) FROM mart.fault_events GROUP BY tool_id")
    faults = {r[0]: int(r[1]) for r in cur.fetchall()}
    # ⚠ 這是「事件被至少一列警報覆蓋」的比例，**與 M5-2 報表的事件層警報率不是同一口徑**
    #   （M5-2 用模型覆蓋口徑；實測 01M02 這裡 2.00% vs 報表 5.71%）。
    #   絕對值不可直接比，但兩者排序一致（已在 M5-4 結論中註明）；欄位名一律照實寫。
    cur.execute("""
        SELECT e.tool_id, count(*) FILTER (WHERE a.event_id IS NOT NULL) AS n_alerts,
               count(*) AS n_events
        FROM stg.fact_process_event e
        LEFT JOIN (SELECT DISTINCT event_id FROM mart.mspc_alerts
                   WHERE run_id = %s AND robust = true) a ON a.event_id = e.event_id
        GROUP BY e.tool_id
    """, (run_id,))
    event_rate = {r[0]: (r[1] / r[2] if r[2] else None) for r in cur.fetchall()}
    return {"alert_units": alert_units, "bad_lot": bad_lot, "membership": membership,
            "faults": faults, "event_rate": event_rate}


def load_csv(d: Path) -> dict:
    alert_units: dict = defaultdict(dict)
    with (d / "lot_alert_features.csv").open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            alert_units[row["unit_kind"]][(row["lot_id"], row["tool_id"])] = \
                row["has_alarm"].strip().lower() in ("1", "true", "t")
    bad_lot: dict = {}
    with (d / "lot_units.csv").open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            cur = row["outcome_bad"].strip().lower() in ("1", "true", "t")
            bad_lot[row["lot_id"]] = bool(bad_lot.get(row["lot_id"])) or cur
    membership = []
    with (d / "lot_membership.csv").open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row["group_type"] == "tool":
                membership.append((row["lot_id"], row["group_key"]))
    faults, event_rate = {}, {}
    if (d / "faults.csv").exists():
        with (d / "faults.csv").open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                faults[row["tool_id"]] = int(row["n_faults"])
    if (d / "event_rate.csv").exists():
        with (d / "event_rate.csv").open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                event_rate[row["tool_id"]] = float(row["rate"])
    return {"alert_units": alert_units, "bad_lot": bad_lot, "membership": membership,
            "faults": faults, "event_rate": event_rate}


def main() -> int:
    ap = argparse.ArgumentParser(description="M5-4-2／M5-4-3 第二層共同性分析與判準評估")
    ap.add_argument("--run-id", default="M5_mspc_b0.3")
    ap.add_argument("--csv-dir", default="", help="助理端驗證：讀 CSV 而非資料庫")
    ap.add_argument("--no-db", action="store_true")
    ap.add_argument("--out-csv", default="reports/m5/m5_4_tool_ranking.csv")
    ap.add_argument("--report", default="reports/m5/m5_4_commonality.json")
    add_version_arg(ap)
    args = ap.parse_args()

    if args.csv_dir:
        print(f"[模式] CSV（助理端驗證）｜{args.csv_dir}")
        data = load_csv(Path(args.csv_dir))
    else:
        conn = connect()
        print(f"[模式] 資料庫｜run_id={args.run_id}")
        data = load(conn, args.run_id)
        conn.close()

    wide = compute_wide(data)
    strict = compute_strict(data)
    m3ref = compute_m3_reference(data)
    fault_tools = {t for t, n in data["faults"].items() if n > 0}

    def show(title: str, per: dict, with_total: bool = False):
        print(f"\n{title}")
        print(f"    {'機台':<8}{'警報率':>9}{'OR':>10}{'95% CI':>18}{'p 值':>11}{'故障':>6}"
              + (f"{'經手批次':>11}" if with_total else ""))
        rank = rank_tools(per, "rate")
        for i, (tool, rate) in enumerate(rank, 1):
            v = per[tool]
            lo, hi = v["or_ci"]
            print(f"    {i}. {tool:<6}{rate:>9.1%}{v['odds_ratio']:>10.2f}"
                  f"{f'[{lo:.2f},{hi:.2f}]':>18}{v['p_value']:>11.3g}"
                  f"{data['faults'].get(tool, 0):>6}"
                  + (f"{v['total']:>11,}" if with_total else ""))
        return rank

    rank_w = show("① 寬鬆歸屬（M3 同語意）：批次警報率與壞批次 enrichment", wide, True)
    rank_s = show("② 嚴格歸屬：警報只記真正發生的機台", strict, True)
    print("\n③ 對照組（判準 3 的依據）")
    print(f"    {'機台':<8}{'M3 壞批次 OR':>14}{'事件警報覆蓋率':>16}{'故障次數':>9}"
          f"   （覆蓋率＝事件被任一列警報覆蓋；口徑與 M5-2 報表不同，只看排序）")
    for tool in sorted(m3ref, key=lambda t: -m3ref[t]["odds_ratio"]):
        er = data["event_rate"].get(tool)
        print(f"    {tool:<8}{m3ref[tool]['odds_ratio']:>14.2f}"
              f"{(er if er is not None else float('nan')):>20.2%}"
              f"{data['faults'].get(tool, 0):>9}")

    orders = [[t for t, _ in rank_w], [t for t, _ in rank_s],
              [t for t, _ in rank_tools(m3ref, "odds_ratio")],
              [t for t, _ in sorted(data["event_rate"].items(), key=lambda kv: -(kv[1] or 0))]]
    agree = order_agreement(orders)
    crit = evaluate_criteria(rank_w, rank_s, fault_tools)
    crit["3_agreement"] = {"n_same_pairs": agree["n_same"], "n_pairs": agree["n_pairs"],
                           "orders": orders, "note": agree["note"]}

    print("\n[判準]（事前寫死，不因結果調整）")
    print(f"    1. 負控制 04M01 不得進前 2：寬鬆 "
          f"{'✓' if crit['1_control_not_top2']['wide'] else '✗'}"
          f"（前 2：{crit['1_control_not_top2']['wide_top2']}）"
          f"｜嚴格 {'✓' if crit['1_control_not_top2']['strict'] else '✗'}"
          f"（前 2：{crit['1_control_not_top2']['strict_top2']}）")
    print(f"    2. 真故障機台至少一台進前 2：寬鬆 "
          f"{'✓' if crit['2_fault_tool_top2']['wide'] else '✗'}"
          f"｜嚴格 {'✓' if crit['2_fault_tool_top2']['strict'] else '✗'}")
    print(f"    3. 排序一致性：{agree['n_same']}/{agree['n_pairs']} 對排序完全相同"
          f"（寬鬆／嚴格／M3／M5-2 四者）")
    print("       ⚠ 機台只有 5 台，不做統計檢定；且同一批次出現在多台機台 → "
          "各機台的 2×2 不獨立")
    print("       → 主要證據是跨方法一致性，p 值只當輔助（M5-4-0 [D] 已量測）")

    out_rows = []
    for tool in sorted(set(wide) | set(strict) | set(m3ref)):
        out_rows.append({
            "tool": tool, "faults": data["faults"].get(tool, 0),
            "wide_rate": wide.get(tool, {}).get("rate"),
            "wide_or": wide.get(tool, {}).get("odds_ratio"),
            "wide_p": wide.get(tool, {}).get("p_value"),
            "strict_rate": strict.get(tool, {}).get("rate"),
            "strict_or": strict.get(tool, {}).get("odds_ratio"),
            "strict_p": strict.get(tool, {}).get("p_value"),
            "m3_or": m3ref.get(tool, {}).get("odds_ratio"),
            "m5_2_event_rate": data["event_rate"].get(tool)})
    outp = Path(args.out_csv)
    outp.parent.mkdir(parents=True, exist_ok=True)
    with outp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(out_rows[0]))
        writer.writeheader()
        writer.writerows(out_rows)
    rep = Path(args.report)
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text(json.dumps({"version": script_version.current_version(),
                               "run_id": args.run_id, "criteria": crit,
                               "agreement": agree, "wide": wide, "strict": strict,
                               "m3_reference": m3ref, "faults": data["faults"],
                               "event_rate": data["event_rate"]},
                              ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n  明細：{outp}\n  報告：{rep}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
