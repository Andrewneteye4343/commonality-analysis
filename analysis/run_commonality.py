#!/usr/bin/env python3
"""M3：共同性分析排名（引擎主體）。

流程
────────────────────────────────────────────────────────────
    mart.lot_units（批次 × 壞/好）  ─┐
                                    ├─→ 每群組的 2×2 列聯表 → 超幾何檢定（= Fisher greater）
    mart.lot_membership（批次×群組）─┘                        → odds ratio + 95% CI
                                                              → Bonferroni / BH-FDR 校正
                                                              → 排名 + Pareto / 森林圖

用法
────────────────────────────────────────────────────────────
    # 資料庫模式（正式）
    python -m analysis.run_commonality --window-hours 24 --fault-type ANY

    # CSV 模式（沒有資料庫時驗證用；由 load_fault_labels.py --dump-dir 產生）
    python -m analysis.run_commonality --units-csv /tmp/m3/lot_units.csv \
        --membership-csv /tmp/m3/lot_membership.csv --out reports/m3

輸出一律寫入 reports/m3/：commonality_ranking.csv、commonality_summary.md、（可選）圖。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from commonality_basic import RankConfig, rank_groups           # noqa: E402
from multiple_testing import evaluate                            # noqa: E402


# ─────────────────────────────────────────────────────────────
# 讀取
# ─────────────────────────────────────────────────────────────
def load_from_db(window_hours: int, fault_type: str):
    import psycopg
    conn = psycopg.connect(
        host=os.environ.get("PGHOST", "db"), port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""),
        dbname=os.environ.get("PGDATABASE", "commonality"))
    with conn.cursor() as cur:
        cur.execute("""SELECT lot_id, outcome_bad FROM mart.lot_units
                       WHERE window_hours = %s AND fault_type = %s""", (window_hours, fault_type))
        units = {r[0]: bool(r[1]) for r in cur.fetchall()}
        cur.execute("""SELECT lot_id, group_type, group_key FROM mart.lot_membership""")
        memberships = [(r[0], r[1], r[2]) for r in cur.fetchall()]
    conn.close()
    return units, memberships


def load_from_csv(units_csv: Path, membership_csv: Path, window_hours: int, fault_type: str):
    units = {}
    with units_csv.open(newline="") as fh:
        for r in csv.DictReader(fh):
            if int(r.get("window_hours", window_hours)) != window_hours:
                continue
            if r.get("fault_type", fault_type) != fault_type:
                continue
            units[r["lot_id"]] = str(r["outcome_bad"]).lower() in ("true", "t", "1")
    memberships = []
    with membership_csv.open(newline="") as fh:
        for r in csv.DictReader(fh):
            memberships.append((r["lot_id"], r["group_type"], r["group_key"]))
    return units, memberships


# ─────────────────────────────────────────────────────────────
# 排名
# ─────────────────────────────────────────────────────────────
def build_ranking(units: dict, memberships: list, cfg: RankConfig, group_types: set[str] | None):
    mem = [m for m in memberships if group_types is None or m[1] in group_types]
    results = rank_groups(units, mem, cfg)

    testable = [r for r in results if r.testable]
    pvals = [r.stats["p_hyper"] for r in testable]
    q, pb, fdr = evaluate(pvals, alpha=cfg.alpha)
    for r, qi, pbi in zip(testable, q, pb):
        r.stats["q_bh"] = qi
        r.stats["p_bonferroni"] = pbi

    # 全部（含不可檢定）都要有欄位，排序時不可檢定的放最後
    rows = []
    for r in results:
        row = r.as_row()
        row.setdefault("q_bh", None)
        row.setdefault("p_bonferroni", None)
        rows.append(row)
    rows.sort(key=lambda d: (d["q_bh"] is None, d["p_hyper"]))

    # 名次（同群組類型內分別排名，避免不同類型互相干擾）
    by_type_rank_p: Counter = Counter()
    by_type_rank_q: Counter = Counter()
    for row in rows:
        gt = row["group_type"]
        by_type_rank_p[gt] += 1
        row["rank_p"] = by_type_rank_p[gt]
    ordered_q = sorted([r for r in rows if r["q_bh"] is not None], key=lambda d: d["q_bh"])
    for row in ordered_q:
        by_type_rank_q[row["group_type"]] += 1
        row["rank_bh"] = by_type_rank_q[row["group_type"]]
    for row in rows:
        row.setdefault("rank_bh", None)
    return rows, fdr


# ─────────────────────────────────────────────────────────────
# 輸出
# ─────────────────────────────────────────────────────────────
CSV_COLS = ["group_type", "group_key", "a", "b", "c", "d", "n_group", "k_bad", "n_total", "n_bad_total",
            "p_bad_in_group", "p_bad_outside", "odds_ratio", "or_ci_low", "or_ci_high", "or_method", "ci_method",
            "p_hyper", "p_fisher_two_sided", "p_bonferroni", "q_bh", "rank_p", "rank_bh", "testable", "flag"]


def write_csv(rows: list, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_summary(rows, fdr, units, cfg, path: Path, window_hours: int, fault_type: str) -> None:
    n_bad = sum(1 for v in units.values() if v)
    top = [r for r in rows if r["testable"]][:15]
    lines = [
        f"# M3 共同性分析排名（窗口 {window_hours} 小時｜故障範圍 {fault_type}）",
        "",
        f"- 分析單位：**批次（lot）**共 {len(units):,} 個；壞批次 {n_bad:,}（{100*n_bad/max(len(units),1):.2f}%）",
        f"- 檢定：單尾超幾何（= Fisher's exact greater）；效應量：odds ratio + 95% CI（Woolf）",
        f"- 多重比較：BH-FDR（q 值）與 Bonferroni 並列；支持度門檻 n_group≥{cfg.min_group_units}、"
        f"K≥{cfg.min_total_bad}、k≥{cfg.min_bad_in_group}",
        "",
        "## 校正前後對照",
        "",
        f"- 實際檢定群組數 m = {fdr.n_tests}",
        f"- 未校正 p < {fdr.alpha}：**{fdr.n_sig_raw}** 個（純巧合的期望值 = m×α = {fdr.expected_false_positives_raw}）",
        f"- Bonferroni 校正後：**{fdr.n_sig_bonferroni}** 個",
        f"- BH-FDR 校正後：**{fdr.n_sig_bh}** 個",
        "",
    ]
    if fdr.n_sig_raw > fdr.expected_false_positives_raw * 0.8:
        lines.append("> ⚠️ 未校正的顯著個數與「純巧合期望值」同量級 → 未校正清單不可直接交付，"
                     "必須看 BH 校正後的結果。")
        lines.append("")
    lines += ["## 前 15 名（依 BH 校正後 q 值）", "",
              "| 排名 | 群組類型 | 群組 | 群組批次 n | 壞批次 k | 群組壞率 | 其他壞率 | OR (95% CI) | p（超幾何） | q（BH） |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in top:
        ci = f"{r['odds_ratio']:.2f} ({r['or_ci_low']:.2f}–{r['or_ci_high']:.2f})"
        pbg = f"{100*float(r['p_bad_in_group']):.1f}%" if r["p_bad_in_group"] is not None else "—"
        pbo = f"{100*float(r['p_bad_outside']):.1f}%" if r["p_bad_outside"] is not None else "—"
        qv = f"{r['q_bh']:.2e}" if r["q_bh"] is not None else "—"
        lines.append(f"| {r['rank_bh']} | {r['group_type']} | {r['group_key']} | {r['n_group']:,} | {r['k_bad']} | "
                     f"{pbg} | {pbo} | {ci} | {r['p_hyper']:.2e} | {qv} |")
    lines += ["", "## 誠實限制", "",
              "- 這是**單一機台**的內部共同性（群組＝recipe／步驟／stage）：真正要回答「哪台機台有問題」需要多機台，"
              "M3 第二段補上。", 
              "- 標籤是「故障前 24 小時內處理的批次」＝**故障鄰近度**，不是真良率（PHM 無 WAT／CP）。", 
              "- 未控制時間與產品：故障前後排定的步驟本來就會次序相關（排程混淆）→ "
              "**排名高的步驟可能是「剛好在那段時間跑」而非因果**，這是 M4（logistic regression 加時間/recipe 控制）要處理的。",
              "- 群組成員定義為「該批次只要經過就算成員」，因此一個批次會同時屬於多個 recipe_step／stage："
              "`recipe:215` 與 `recipe_step_x_stage:215|3` 這類群組其實是**同一批批次的不同包裝**（共線性），"
              "統計上會重複計數 → 讀排名時要看**群組類型**，不要把不同類型的名次混在一起解讀。",
              ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def make_plots(rows: list, units: dict, path_dir: Path, top_n: int = 15) -> list[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:  # matplotlib 不在精簡環境時仍要能跑完排名
        print(f"[提示] 沒有 matplotlib（{e}）→ 跳過圖表，排名 CSV/MD 仍已產生")
        return []
    made = []
    top = [r for r in rows if r["testable"]][:top_n]

    # Pareto：壞批次貢獻
    labels = [f"{r['group_type']}:{r['group_key']}" for r in top]
    ks = [r["k_bad"] for r in top]
    total_bad = sum(1 for v in units.values() if v)
    cum = []
    acc = 0
    for k in ks:
        acc += k
        cum.append(100.0 * acc / max(total_bad, 1))
    fig, ax1 = plt.subplots(figsize=(10, 5))
    ax1.bar(range(len(ks)), ks, color="#c0392b", alpha=0.85)
    ax1.set_ylabel("壞批次數（群組內）")
    ax1.set_xticks(range(len(ks)))
    ax1.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax2 = ax1.twinx()
    ax2.plot(range(len(cum)), cum, color="#2c3e50", marker="o", markersize=3)
    ax2.set_ylabel("累積涵蓋壞批次 %")
    ax2.set_ylim(0, 105)
    plt.title(f"Pareto：前 {top_n} 群組的壞批次貢獻（單機台 03M01）")
    plt.tight_layout()
    f1 = path_dir / "pareto_top15.png"
    plt.savefig(f1, dpi=130)
    plt.close()
    made.append(str(f1))

    # 森林圖：OR 與 95% CI（對數刻度）
    fig, ax = plt.subplots(figsize=(9, max(4, 0.35 * len(top))))
    ys = list(range(len(top)))[::-1]
    for y, r in zip(ys, top):
        lo, hi = max(r["or_ci_low"], 1e-3), max(r["or_ci_high"], 1e-3)
        color = "#c0392b" if (r["q_bh"] is not None and r["q_bh"] < 0.05) else "#7f8c8d"
        ax.plot([lo, hi], [y, y], color=color, linewidth=2)
        ax.plot([r["odds_ratio"]], [y], "o", color=color, markersize=5)
    ax.axvline(1.0, color="black", linestyle="--", linewidth=1)
    ax.set_xscale("log")
    ax.set_yticks(ys)
    ax.set_yticklabels([f"{r['group_type']}:{r['group_key']} (k={r['k_bad']})" for r in top], fontsize=8)
    ax.set_xlabel("odds ratio（對數刻度，含 95% CI；紅色 = BH 校正後顯著）")
    plt.title("森林圖：共同性效應量")
    plt.tight_layout()
    f2 = path_dir / "forest_top15.png"
    plt.savefig(f2, dpi=130)
    plt.close()
    made.append(str(f2))
    return made


def write_db(analysis_id: str, rows: list) -> None:
    import psycopg
    conn = psycopg.connect(
        host=os.environ.get("PGHOST", "db"), port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""),
        dbname=os.environ.get("PGDATABASE", "commonality"))
    cols = ("analysis_id, group_type, group_key, n_total, n_bad_total, n_group, k_bad, a, b, c, d, "
            "p_bad_in_group, p_bad_outside, odds_ratio, or_ci_low, or_ci_high, or_method, ci_method, "
            "p_hyper, p_fisher_two_sided, p_bonferroni, q_bh, rank_p, rank_bh, testable, flag").split(", ")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM mart.commonality_results WHERE analysis_id = %s", (analysis_id,))
        with cur.copy(f"COPY mart.commonality_results ({', '.join(cols)}) FROM STDIN") as cp:
            for r in rows:
                cp.write_row([analysis_id] + [r.get(c) for c in cols[1:]])
        conn.commit()
    conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="M3 共同性分析排名")
    ap.add_argument("--window-hours", type=int, default=24)
    ap.add_argument("--fault-type", default="ANY")
    ap.add_argument("--group-types", default="", help="只分析這些群組類型（逗號分隔）；預設全部")
    ap.add_argument("--min-group-units", type=int, default=5)
    ap.add_argument("--min-total-bad", type=int, default=3)
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--units-csv", default="")
    ap.add_argument("--membership-csv", default="")
    ap.add_argument("--out", default="reports/m3")
    ap.add_argument("--analysis-id", default="")
    ap.add_argument("--no-plots", action="store_true")
    ap.add_argument("--no-db", action="store_true", help="不寫資料庫（CSV 模式預設不寫）")
    ap.add_argument("--exclusive-lots", action="store_true",
                    help="只分析「只被一台機台加工過」的批次（排除共用機台的互相污染）")
    args = ap.parse_args()

    cfg = RankConfig(min_group_units=args.min_group_units, min_total_bad=args.min_total_bad, alpha=args.alpha)
    gtypes = {g.strip() for g in args.group_types.split(",") if g.strip()} or None

    if args.units_csv and args.membership_csv:
        units, memberships = load_from_csv(Path(args.units_csv), Path(args.membership_csv),
                                           args.window_hours, args.fault_type)
        mode = "csv"
    else:
        units, memberships = load_from_db(args.window_hours, args.fault_type)
        mode = "db"

    if not units:
        print("[錯誤] 沒有讀到任何批次單位——先跑 migrate.py 與 load_fault_labels.py")
        return 2

    # ── 排除「被多台機台加工過」的批次（共用機台互相污染：只要經過任一退化機台就算壞批次）
    if args.exclusive_lots:
        from collections import defaultdict as _dd
        tools_of = _dd(set)
        for unit, gtype, gkey in memberships:
            if gtype == "tool":
                tools_of[unit].add(gkey)
        keep = {u for u, ts in tools_of.items() if len(ts) <= 1}
        before_units, before_mem = len(units), len(memberships)
        units = {u: v for u, v in units.items() if u in keep}
        memberships = [m for m in memberships if m[0] in keep]
        print(f"  [exclusive-lots] 只保留單一機台批次：{len(units):,} / {before_units:,} 批次"
              f"（成員列 {len(memberships):,} / {before_mem:,}）")

    rows, fdr = build_ranking(units, memberships, cfg, gtypes)
    out = Path(args.out)
    analysis_id = args.analysis_id or f"M3_{args.fault_type}_w{args.window_hours}h"

    print("=" * 92)
    print(f"共同性分析（{mode} 模式）｜分析單位 {len(units):,} 批次｜"
          f"壞批次 {sum(units.values()):,}｜檢定群組 {fdr.n_tests}")
    print("=" * 92)
    print(f"  未校正顯著 {fdr.n_sig_raw}（純巧合期望 {fdr.expected_false_positives_raw}）｜"
          f"Bonferroni 後 {fdr.n_sig_bonferroni}｜BH-FDR 後 {fdr.n_sig_bh}")
    print(f"\n  {'群組類型':<20}{'群組':<16}{'n':>6}{'k':>5}{'群組壞率':>10}{'其他壞率':>10}{'OR':>9}{'p_hyper':>12}{'q_bh':>12}")
    for r in [r for r in rows if r["testable"]][:12]:
        pbg = 100 * float(r["p_bad_in_group"]) if r["p_bad_in_group"] is not None else 0
        pbo = 100 * float(r["p_bad_outside"]) if r["p_bad_outside"] is not None else 0
        qv = r["q_bh"] if r["q_bh"] is not None else float("nan")
        print(f"  {r['group_type']:<20}{r['group_key']:<16}{r['n_group']:>6,}{r['k_bad']:>5}"
              f"{pbg:>9.1f}%{pbo:>9.1f}%{r['odds_ratio']:>9.2f}{r['p_hyper']:>12.3e}{qv:>12.3e}")

    # 未列入檢定的群組一律要看得見——負控制（k_bad = 0）常常落在這裡，
    # 「安靜地被過濾掉」會讓最重要的證據消失（實際踩過：零故障機台的專屬批次群組）
    skipped = [r for r in rows if not r["testable"]]
    if skipped:
        why = []
        for r in sorted(skipped, key=lambda x: (-x["n_group"], x["group_type"], x["group_key"])):
            if r["n_group"] < cfg.min_group_units:
                reason = f"n_group {r['n_group']} < {cfg.min_group_units}"
            elif r["k_bad"] < cfg.min_total_bad:
                reason = (f"k_bad {r['k_bad']} < {cfg.min_total_bad}"
                          + ("（零壞批次 → 反向控制證據，應為此結果）" if r["k_bad"] == 0 else ""))
            else:
                reason = "其他"
            why.append((r, reason))
        print(f"\n  未列入檢定（{len(skipped)} 個群組；預設門檻 n≥{cfg.min_group_units}、k≥{cfg.min_total_bad}）:")
        print(f"  {'群組類型':<20}{'群組':<16}{'n':>6}{'k':>5}{'壞率':>9}  原因")
        for r, reason in why[:12]:
            pbg = 100 * float(r["p_bad_in_group"]) if r["p_bad_in_group"] is not None else 0
            print(f"  {r['group_type']:<20}{r['group_key']:<16}{r['n_group']:>6,}{r['k_bad']:>5}{pbg:>8.2f}%  {reason}")
        if len(why) > 12:
            print(f"  …（另有 {len(why) - 12} 個；完整清單見 commonality_ranking.csv 的 testable=false）")

    write_csv(rows, out / "commonality_ranking.csv")
    write_summary(rows, fdr, units, cfg, out / "commonality_summary.md",
                  args.window_hours, args.fault_type)
    (out / "fdr_summary.json").write_text(json.dumps(fdr.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    plots = [] if args.no_plots else make_plots(rows, units, out)
    print(f"\n  輸出：{out}/commonality_ranking.csv、commonality_summary.md、fdr_summary.json"
          + (f"、{len(plots)} 張圖" if plots else ""))

    if mode == "db" and not args.no_db:
        write_db(analysis_id, rows)
        print(f"  已寫入 mart.commonality_results（analysis_id = {analysis_id}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
