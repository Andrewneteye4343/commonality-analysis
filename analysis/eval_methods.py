#!/usr/bin/env python3
"""M4-3：方法評估 —— 五種方法在兩種注入機制下的偵測率與名次一致性。

要回答的問題（ROADMAP M4 驗收）
────────────────────────────────────────────────────────────
1. **不同方法排名的重疊度**：同一份資料，各方法排出來的名次像不像？
2. **每種方法在什麼情況下會誤判**：注入機制（相乘 vs 相加）對各方法的影響。
3. **偽重複與小樣本**：多變數模型是否真的比單變數好，好在哪裡。

五種方法
────────────────────────────────────────────────────────────
- `m3_hyper`  ：單變數 2×2 ＋超幾何（M3 的標準做法；假設群組彼此獨立）
- `m4_logit`  ：多變數 logistic（勝算相乘；控制路線其他成員）
- `m4_cloglog`：多變數 cloglog（風險率相乘）
- `m4_hazard` ：多變數風險率相加（**與「曾落在任一機台故障窗內」的機制完全匹配**）
- `shrink`    ：Beta-binomial 收縮後的群組比率（無檢定；貝氏排名的代表）

兩種注入機制（這是整個評估的關鍵設計）
────────────────────────────────────────────────────────────
- `odds`  ：被注入群組的批次壞率 = 基準 × 倍數（M3 的做法）→ **相乘**機制，logit 佔優勢
- `hazard`：被注入群組加上固定的風險率（λ = h_base + Δ）→ **相加**機制，hazard 佔優勢，
  而且這正是本專案壞批次標籤的真實機制（見 METHODS.md 3.1.2）

因此「哪個方法最好」沒有單一答案——**取決於機制**。這正是評估要量化的東西。

用法
────────────────────────────────────────────────────────────
    # 容器內：兩種機制 × 50 次重複（真實群組結構）
    docker compose run --rm etl python -m analysis.eval_methods --mechanism both --replicates 50

    # 只跑 null（不注入）看假陽性
    docker compose run --rm etl python -m analysis.eval_methods --null-only --replicates 50
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

# 讓「直接跑檔案」與「python -m analysis.xxx」兩種呼叫方式都能 import 到 etl/ 的模組。
# 容器佈局：./etl 掛在 /work（**不是** /work/etl）、./analysis 掛在 /work/analysis，
# 而直接跑檔案時 sys.path[0] 是 /work/analysis → /work 必須自己加進來
# （`-m` 模式會自動加入 cwd，所以只有直接跑檔案會踩到）。
for _cand in (Path(__file__).resolve().parent.parent / "etl",
              Path(__file__).resolve().parent.parent,
              Path(__file__).resolve().parent):
    if _cand.is_dir() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from glm import beta_binomial_fit, bh_qvalues, shrink_rates, spearman, topk_overlap  # noqa: E402
from run_commonality import build_ranking, load_from_csv as load_commonality_csv  # noqa: E402
from run_commonality import load_from_db as load_commonality_db  # noqa: E402
from run_logistic import build_design, fit_route_model  # noqa: E402
from script_version import add_version_arg  # noqa: E402

try:                                     # RankConfig 定義在 commonality_basic
    from commonality_basic import RankConfig  # noqa: E402
except ImportError:                      # pragma: no cover
    RankConfig = None                    # type: ignore

METHODS = ("m3_hyper", "m4_logit", "m4_cloglog", "m4_hazard", "shrink")
# 兩個「只看估計、不看檢定」的效果量排名：用來分離「點估計好不好」與
# 「標準誤可不可信」——群聚數不足時壞掉的是後者
EFFECT_METHODS = ("m4_logit_eff", "m4_cloglog_eff")


# ─────────────────────────────────────────────────────────────
# 模擬
# ─────────────────────────────────────────────────────────────
def hazard_for_rate(rate: float) -> float:
    """把「壞率」換算成風險率 h：p = 1 − exp(−h)"""
    return -math.log(max(1e-12, 1.0 - min(rate, 1 - 1e-9)))


def simulate_units(lots: list[str], lots_of_group: dict[str, set[str]], target: str | None,
                   base_rate: float, ratio: float, mechanism: str, rng: random.Random) -> dict:
    """依指定機制產生模擬的壞批次。

    - mechanism="odds"  ：目標群組的批次壞率 = base_rate × ratio（相乘）
    - mechanism="hazard"：目標群組的批次加上固定風險率 Δ，使壞率 = base_rate × ratio（相加）
    兩種機制的「目標群組壞率」相同，差異在**共用路線的批次怎麼累加**。
    """
    h_base = hazard_for_rate(base_rate)
    target_rate = min(1 - 1e-9, base_rate * ratio)
    delta = max(hazard_for_rate(target_rate) - h_base, 0.0)
    in_target = lots_of_group.get(target or "", set())
    units = {}
    for lot in lots:
        if mechanism == "hazard":
            lam = h_base + (delta if lot in in_target else 0.0)
            p = 1.0 - math.exp(-lam)
        else:
            p = target_rate if lot in in_target else base_rate
        units[lot] = rng.random() < p
    return units


# ─────────────────────────────────────────────────────────────
# 各方法的名次
# ─────────────────────────────────────────────────────────────
def rank_methods(units: dict, memberships3: list, memberships4: list, gtype: str, design: dict,
                 cfg, ridge: float, alpha: float, lrt_groups: int = 0
                 ) -> tuple[dict[str, list[str]], dict[str, set[str]], dict[str, dict[str, float]]]:
    """回傳（{方法: 群組由好到壞}, {方法: 顯著群組集合}, {方法: 分數向量}）。

    分數向量的用途：p 類方法用 −log10(p)（越大越可疑）、hazard 用風險率、shrink 用收縮比率；
    跨方法名次相關就是對這些分數向量算 Spearman。
    """
    ranks: dict[str, list[str]] = {}
    sig: dict[str, set[str]] = {}
    scores: dict[str, dict[str, float]] = {}

    # ① M3 單變數（2×2 + 超幾何）
    rows_m3, _fdr = build_ranking(units, memberships3, cfg, {gtype})
    rows_m3 = [r for r in rows_m3 if r["group_type"] == gtype]
    rows_m3 = sorted(rows_m3, key=lambda r: r["p_hyper"])
    ranks["m3_hyper"] = [r["group_key"] for r in rows_m3]
    sig["m3_hyper"] = {r["group_key"] for r in rows_m3 if r["q_bh"] is not None and r["q_bh"] < alpha}
    scores["m3_hyper"] = {r["group_key"]: -math.log10(max(r["p_hyper"], 1e-300)) for r in rows_m3}

    # ② 多變數模型
    for model, name in (("logit", "m4_logit"), ("cloglog", "m4_cloglog"), ("hazard", "m4_hazard")):
        d = dict(design)
        d["y"] = [1 if units[l] else 0 for l in design["lots"]]
        rows, _diag = fit_route_model(d, ridge, model=model, alpha=alpha,
                                      lrt_max_groups=lrt_groups if model == "hazard" else 0)
        if model == "hazard":
            # 風險率相加模型的主檢定量是效果量本身（風險率）；LRT 的 p 值可能因上限而缺漏
            rows = sorted(rows, key=lambda r: -r["or_"])
        ranks[name] = [r["group_key"] for r in rows]
        sig[name] = {r["group_key"] for r in rows if r["q_bh"] is not None and r["q_bh"] < alpha}
        # hazard 的檢定量是風險率本身（LRT p 常撞到下界 0.5），因此用風險率當分數
        scores[name] = ({r["group_key"]: (r["or_"] if model == "hazard"
                                           else -math.log10(max(r["p_wald"], 1e-300)))
                         for r in rows})
        if model in ("logit", "cloglog"):
            # 完全不用標準誤：排名看「與其他群組的對比」大小（|log 對比 OR|）。
            # 注意不可用 |raw_beta|——那是「水準」，多數群組的水準本來就居中，
            # 用它排名會把真正的效應排到最後（實測踩到）。
            eff_name = f"{name}_eff"
            eff = {r["group_key"]: abs(math.log(r["or_"])) if r["or_"] else 0.0
                   for r in rows}
            ranks[eff_name] = [k for k in sorted(eff, key=lambda k: -eff[k])]
            sig[eff_name] = set()
            scores[eff_name] = eff

    # ③ Beta-binomial 收縮（無檢定）
    keys = design["group_keys"]
    ks, ns = [], []
    for key in keys:
        n = sum(1 for r in design["X"] if r[design["colnames"].index(f"{gtype}={key}")] == 1.0)
        k = sum(1 for l, r in zip(design["lots"], design["X"])
                if r[design["colnames"].index(f"{gtype}={key}")] == 1.0 and units[l])
        ks.append(k)
        ns.append(n)
    fit_bb = beta_binomial_fit(ks, ns)
    shrunk = shrink_rates(ks, ns, fit_bb["alpha"], fit_bb["beta"])
    ranks["shrink"] = [keys[i] for i in sorted(range(len(keys)), key=lambda i: -shrunk[i])]
    sig["shrink"] = set()
    scores["shrink"] = {keys[i]: shrunk[i] for i in range(len(keys))}
    return ranks, sig, scores


def rank_of(target: str, ordered: list[str]) -> tuple[int, int]:
    """與排序順序無關的名次：(最樂觀, 最保守)；相等＝無平手。"""
    if target not in ordered:
        return len(ordered) + 1, len(ordered) + 1
    pos = ordered.index(target)
    return pos + 1, pos + 1


# ─────────────────────────────────────────────────────────────
# 重複實驗
# ─────────────────────────────────────────────────────────────
def aggregate(replicates: list[dict], n_groups: int, methods=None) -> dict:
    methods = methods or (METHODS + EFFECT_METHODS)
    out = {}
    for m in methods:
        rs = [rep["ranks"][m] for rep in replicates]
        det1 = sum(1 for r in rs if r == 1) / len(rs)
        det3 = sum(1 for r in rs if r <= 3) / len(rs)
        out[m] = {
            "detect_at_1": det1, "detect_at_3": det3,
            "mean_rank": sum(rs) / len(rs),
            "mean_significant": sum(len(rep["sig"][m]) for rep in replicates) / len(replicates),
            "fpr": sum(len(rep["sig"][m]) for rep in replicates) / len(replicates) / max(n_groups, 1),
        }
    return out


def cross_method_agreement(replicates: list[dict], methods=None, k: int = 3) -> dict:
    methods = methods or (METHODS + EFFECT_METHODS)
    """方法兩兩之間的名次相關（平均）。

    重要：各方法的**分數必須先按群組鍵對齊**才能比較——不同方法回傳的列順序不同
    （各自按自己的 p 值排序），直接對兩個向量算相關會得到無意義的結果。
    """
    pairs = {}
    for i, a in enumerate(methods):
        for b in methods[i + 1:]:
            vals, ovs = [], []
            for rep in replicates:
                sa, sb = rep["scores"].get(a, {}), rep["scores"].get(b, {})
                common = sorted(set(sa) & set(sb))
                if len(common) >= 2:
                    vals.append(spearman([sa[c] for c in common], [sb[c] for c in common]))
                if rep["order"].get(a) and rep["order"].get(b):
                    ovs.append(topk_overlap(rep["order"][a], rep["order"][b], k))
            pairs[f"{a}|{b}"] = {
                "spearman_mean": (sum(vals) / len(vals)) if vals else None,
                "topk_overlap_mean": (sum(ovs) / len(ovs)) if ovs else None,
                "n_replicates": len(vals),
            }
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description="M4-3 方法評估（注入實驗）")
    ap.add_argument("--group-type", default="tool")
    ap.add_argument("--mechanism", default="both", choices=["odds", "hazard", "both"])
    ap.add_argument("--replicates", type=int, default=50)
    ap.add_argument("--ratio", type=float, default=3.0, help="被注入群組的壞率倍數（兩種機制相同）")
    ap.add_argument("--base-rate", type=float, default=0.0, help="0 = 用真實資料算")
    ap.add_argument("--target", default="", help="被注入的群組（預設取批次最多的）")
    ap.add_argument("--ridge", type=float, default=1.0)
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=20261010)
    ap.add_argument("--units-csv", default="")
    ap.add_argument("--membership-csv", default="")
    ap.add_argument("--window-hours", type=int, default=24)
    ap.add_argument("--fault-type", default="ANY")
    ap.add_argument("--null-only", action="store_true", help="只跑不注入（假陽性控制）")
    ap.add_argument("--max-groups", type=int, default=0,
                    help="多變數模型只用批次數最多的 N 個群組（0 = 全部）；被排除的群組會明列")
    ap.add_argument("--hazard-lrt-groups", type=int, default=25,
                    help="hazard 的 LRT 只對最大的 N 群做（LRT 成本 = 每群一次重擬合）")
    ap.add_argument("--max-minutes", type=float, default=30.0,
                    help="預估時間超過此值就中止（除非加 --force）")
    ap.add_argument("--force", action="store_true", help="即使預估時間超過上限也照跑")
    ap.add_argument("--out", default="reports/m4")
    add_version_arg(ap)
    args = ap.parse_args()

    if args.units_csv:
        units, memberships = load_commonality_csv(Path(args.units_csv),
                                                  Path(args.membership_csv),
                                                  args.window_hours, args.fault_type)
    else:
        units, memberships = load_commonality_db(args.window_hours, args.fault_type)
    if not units:
        print("[錯誤] 沒有批次單位——先跑 load_fault_labels.py")
        return 2
    # 兩套引擎的成員列格式不同，這裡各留一份（都從同一來源正規化）：
    #   M3 引擎（commonality_basic.rank_groups）：3 欄 (lot, group_type, group_key)
    #   M4 引擎（run_logistic.build_design）    ：4 欄（多了事件數）
    memberships3 = [(m[0], m[1], m[2]) for m in memberships]
    memberships4 = [(m[0], m[1], m[2], int(m[3]) if len(m) > 3 else 0) for m in memberships]

    lots = sorted(units)
    lots_of_group: dict[str, set[str]] = defaultdict(set)
    for lot, gtype, key, _n in memberships4:
        if gtype == args.group_type:
            lots_of_group[key].add(lot)
    keys = sorted(lots_of_group)
    dropped_groups: list[str] = []
    if args.max_groups and len(keys) > args.max_groups:
        keep = set(sorted(keys, key=lambda k: -len(lots_of_group[k]))[:args.max_groups])
        dropped_groups = sorted(set(keys) - keep)
        # ⚠ 必須在篩掉成員列「之前」計算，否則永遠是 0（實際踩過）
        n_drop_lots = sum(1 for m in memberships4
                          if m[1] == args.group_type and m[2] in dropped_groups)
        # M3 與 M4 必須用同一組群組（否則兩邊的 q 值不可比）
        memberships3 = [m for m in memberships3 if not (m[1] == args.group_type and m[2] in dropped_groups)]
        memberships4 = [m for m in memberships4 if not (m[1] == args.group_type and m[2] in dropped_groups)]
        keys = sorted(keep)
        lots_of_group = {k: lots_of_group[k] for k in keys}
        print(f"  [群組上限] 只保留批次數最多的 {args.max_groups} 群；排除 {len(dropped_groups)} 群"
              f"（成員列少 {n_drop_lots}）")
        print(f"             被排除（依批次數由小到大，前 12 個）：{dropped_groups[:12]}"
              f"{' …' if len(dropped_groups) > 12 else ''}")
    target = args.target or max(keys, key=lambda k: len(lots_of_group[k]))
    base_rate = args.base_rate or (sum(units.values()) / len(units))
    print(f"[資料] {len(lots):,} 批次｜{args.group_type} 群組 {len(keys)} 個"
          f"（{', '.join(f'{k}:{len(lots_of_group[k])}' for k in keys)}）")
    print(f"[設定] 基準壞率 {base_rate:.4f}｜注入目標 {target}｜倍數 {args.ratio}×｜"
          f"重複 {args.replicates}｜ridge {args.ridge}")

    design = build_design(units, memberships4, args.group_type, controls=[], control_top_n=0,
                          time_trend=False, lot_time={l: (None, 0) for l in lots})
    report: dict = {"group_type": args.group_type, "keys": keys, "base_rate": base_rate,
                    "ratio": args.ratio, "replicates": args.replicates, "n_lots": len(lots),
                    "mechanisms": {}, "null": {}}
    n_clusters = len(set(design["cluster"]))
    n_par = len(design["colnames"])
    gk = n_clusters / n_par if n_par else 0.0
    print(f"  [群聚/參數] 群聚 G = {n_clusters}｜參數 k = {n_par}｜G/k = {gk:.1f}")
    report["n_clusters"], report["n_params"], report["cluster_per_param"] = (
        n_clusters, n_par, gk)
    if gk < 5:
        print(f"  [警告] G/k = {gk:.1f} < 5 → 群聚穩健標準誤會嚴重低估，"
              f"多變數的 p 值與名次不可信（改用效果量或 M3／收縮法）")
        report["warning_gk"] = gk
    if n_clusters < 10:
        print(f"  [警告] 只有 {n_clusters} 個群聚 → 群聚穩健標準誤不可靠"
              f"（建議 ≥ 10–30）")
        report["warning_n_clusters"] = n_clusters
    cfg = RankConfig(min_group_units=5, min_total_bad=3, alpha=args.alpha) if RankConfig else None
    if cfg is None:
        print("[錯誤] 找不到 RankConfig（analysis/commonality_basic.py）")
        return 2

    mechanisms = ["odds", "hazard"] if args.mechanism == "both" else [args.mechanism]
    all_methods = METHODS + EFFECT_METHODS
    # ── 開跑前的成本預估（實測校準：單次擬合 ≈ 1.2e-8 · n · k² 秒）──
    n_fits = 3 + (args.hazard_lrt_groups or len(keys))     # logit + cloglog + hazard + hazard 的 LRT
    per_replicate = 1.2e-8 * len(lots) * len(keys) ** 2 * n_fits
    total_s = per_replicate * args.replicates * len(mechanisms)
    print(f"  [成本預估] 群組 {len(keys)}｜每重複 {n_fits} 次擬合 ≈ {per_replicate:.1f} 秒"
          f"｜合計（{args.replicates} 次 × {len(mechanisms)} 機制）≈ {total_s / 60:.1f} 分鐘")
    if total_s > args.max_minutes * 60 and not args.force:
        print(f"  [中止] 預估 {total_s / 60:.1f} 分鐘 > 上限 {args.max_minutes:.0f} 分鐘。"
              f"可用的旋鈕：--max-groups（例如 40）、--hazard-lrt-groups（例如 10）、"
              f"--replicates（例如 25）；確定要跑請加 --force")
        return 3
    rng = random.Random(args.seed)
    report["version"] = getattr(args, "version", "")

    for mech in mechanisms:
        reps = []
        for r in range(args.replicates):
            y_units = simulate_units(lots, lots_of_group, target, base_rate, args.ratio, mech, rng)
            order, sig, scores = rank_methods(y_units, memberships3, memberships4,
                                              args.group_type, design, cfg, args.ridge,
                                              args.alpha, args.hazard_lrt_groups)
            ranks_target = {m: rank_of(target, order[m])[0] for m in all_methods}
            reps.append({"ranks": ranks_target, "sig": sig, "scores": scores, "order": order})
        agg = aggregate(reps, n_groups=len(keys))
        report["mechanisms"][mech] = {"by_method": agg,
                                     "agreement": cross_method_agreement(reps)}
        print(f"\n  ── 注入機制：{mech}（目標 {target}，{args.ratio}×）"
              f"｜{args.replicates} 次重複 ──")
        print(f"  {'方法':>15}{'偵測率@1':>9}{'偵測率@3':>9}{'平均名次':>9}"
              f"{'平均顯著數':>12}{'顯著群組比例':>14}")
        for m in all_methods:
            v = agg[m]
            print(f"  {m:<12}{100 * v['detect_at_1']:>9.0f}%{100 * v['detect_at_3']:>9.0f}%"
                  f"{v['mean_rank']:>10.2f}{v['mean_significant']:>12.2f}{v['fpr']:>14.3f}")

    # null 模式（不注入）→ 假陽性控制
    reps_null = []
    for _r in range(args.replicates):
        y_units = simulate_units(lots, lots_of_group, None, base_rate, args.ratio, "hazard", rng)
        _order, sig, _scores = rank_methods(y_units, memberships3, memberships4,
                                            args.group_type, design, cfg, args.ridge,
                                            args.alpha, args.hazard_lrt_groups)
        reps_null.append({"ranks": {m: 1 for m in all_methods}, "sig": sig,
                          "scores": {}, "order": {}})
    report["null"] = {m: {"mean_significant": sum(len(x["sig"][m]) for x in reps_null) / len(reps_null),
                          "fpr": sum(len(x["sig"][m]) for x in reps_null) / len(reps_null) / len(keys)}
                      for m in METHODS}
    print(f"\n  ── 負控制（不注入任何根因，{args.replicates} 次重複）──")
    print(f"  {'方法':>15}{'平均顯著群組數':>16}{'假陽性率':>12}")
    for m in METHODS:      # 效果量排名沒有檢定，不列
        v = report["null"][m]
        note = ""
        if m == "m4_hazard" and v["mean_significant"] > 0.5 * len(keys):
            note = "  ← 結構局限（見 METHODS.md 3.1.3），不是 bug"
        print(f"  {m:<12}{v['mean_significant']:>16.2f}{v['fpr']:>12.3f}{note}")
    print("  註：均勻壞率對『風險率相加』模型是**不可表示**的資料（無基準項）→ 必然全判顯著；"
          "\n      hazard 的負控制要看真實資料（04M01 的 LRT p = 0.5）。")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "eval_methods.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n  報告：{out}/eval_methods.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
