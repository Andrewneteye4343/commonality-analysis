#!/usr/bin/env python3
"""M3：方法評估——用合成注入的 ground truth 量測偵測率與偽發現率。

為什麼一定要做（本專案的差異化）
────────────────────────────────────────────────────────────
共同性分析會吐出一個排名清單。問題是：**這份清單可信嗎？**
- 正控制（injected）：注入一個已知根因（某群組壞率×倍數），方法排得出來嗎？排第幾名？
- 負控制（null）：**完全沒有根因**時，方法會報出幾個假陽性？
  負控制是最關鍵的一項——沒有負控制的分析，在面試裡站不住。

設計巧思：**用真實資料的群組結構、只模擬結果（outcome）**
────────────────────────────────────────────────────────────
模擬時沿用真實的群組大小與批次分佈（例如某些 recipe 只跑過 12 批），
因此量到的偵測率會**反映真實的樣本量與檢定力限制**，
而不是「在理想資料上很好、在真實資料上抓不到」。

用法
────────────────────────────────────────────────────────────
    python -m analysis.eval_commonality --membership-csv /tmp/m3/lot_membership.csv \
        --group-type recipe --replicates 100 --effect-ratio 3 --base-rate 0.07
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from commonality_basic import RankConfig, rank_groups      # noqa: E402
from multiple_testing import evaluate                       # noqa: E402


# 負控制的 target_group 哨兵值。
# 為什麼不用 NULL：`target_group` 是 mart.method_eval 主鍵的一部分，
# PostgreSQL 的主鍵欄位隱含 NOT NULL（實測 DROP NOT NULL 會被拒絕：
# `column "target_group" is in a primary key`）→ 用明確的哨兵字串代表「沒有注入群組」。
NULL_TARGET = "(none)"


def load_membership_db(group_type: str | None = None):
    """從 mart.lot_membership 讀真實群組結構"""
    import psycopg
    conn = psycopg.connect(
        host=os.environ.get("PGHOST", "db"), port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""),
        dbname=os.environ.get("PGDATABASE", "commonality"))
    with conn.cursor() as cur:
        sql = "SELECT lot_id, group_type, group_key FROM mart.lot_membership"
        params = ()
        if group_type:
            sql += " WHERE group_type = %s"
            params = (group_type,)
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.close()
    groups: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    lots: set[str] = set()
    for lot, gt, gk in rows:
        lots.add(lot)
        groups[gt][gk].append(lot)
    return sorted(lots), groups


def load_base_rate_db(window_hours: int, fault_type: str) -> float | None:
    import psycopg
    conn = psycopg.connect(
        host=os.environ.get("PGHOST", "db"), port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""),
        dbname=os.environ.get("PGDATABASE", "commonality"))
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*), count(*) FILTER (WHERE outcome_bad) FROM mart.lot_units
                       WHERE window_hours = %s AND fault_type = %s""", (window_hours, fault_type))
        n, bad = cur.fetchone()
    conn.close()
    return (bad / n) if n else None


def load_membership(path: Path, group_type: str | None):
    groups: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    lots: set[str] = set()
    with path.open(newline="") as fh:
        for r in csv.DictReader(fh):
            if group_type and r["group_type"] != group_type:
                continue
            lots.add(r["lot_id"])
            groups[r["group_type"]][r["group_key"]].append(r["lot_id"])
    return sorted(lots), groups


def load_base_rate(path: Path, window_hours: int, fault_type: str, default: float) -> float:
    if not path or not Path(path).exists():
        return default
    n = bad = 0
    with Path(path).open(newline="") as fh:
        for r in csv.DictReader(fh):
            if int(r["window_hours"]) != window_hours or r["fault_type"] != fault_type:
                continue
            n += 1
            bad += 1 if str(r["outcome_bad"]).lower() in ("true", "t", "1") else 0
    return (bad / n) if n else default


def simulate(lots, groups, gtype, base_rate, rng, injection=None):
    """回傳 memberships（真實結構）與 units（模擬結果）"""
    units = {}
    for lot in lots:
        p = base_rate
        if injection and lot in injection["lots"]:
            p = min(1.0, base_rate * injection["effect_ratio"])
        units[lot] = rng.random() < p
    mem = []
    for key, lot_list in groups[gtype].items():
        for lot in lot_list:
            mem.append((lot, gtype, key))
    return units, mem


def run_once(units, mem, cfg, target_key=None):
    """跑一次完整分析；名次採**與排序順序無關**的定義。

    為什麼要特別處理名次：群組成員高度重疊（共線性）時，很多群組的 p 值**完全相同**
    （例如 `215|1`～`215|5`），此時「第 1 名」沒有定義，用 `sorted()` 的名次會取決於
    資料庫回傳順序 → 指標不可重現。因此改用：
        rank_min = 1 + #{p < p_target}   （最樂觀名次）
        rank_max =     #{p ≤ p_target}   （最保守名次）
    兩者相等代表沒有平手；差距越大代表共線性越嚴重、歸因越不可靠。
    """
    results = rank_groups(units, mem, cfg)
    testable = [r for r in results if r.testable]
    pvals = [r.stats["p_hyper"] for r in testable]
    q, pb, fdr = evaluate(pvals, alpha=cfg.alpha)
    ordered = sorted(zip(testable, q, pb), key=lambda t: t[0].stats["p_hyper"])
    rank_of_target = rank_min = rank_max = None
    n_tied = None
    if target_key is not None:
        tgt = next((r for r in testable if r.group_key == target_key), None)
        if tgt is not None:
            pt = tgt.stats["p_hyper"]
            eps = 1e-15
            lower = sum(1 for r in testable if r.stats["p_hyper"] < pt - eps)
            leq = sum(1 for r in testable if r.stats["p_hyper"] <= pt + eps)
            rank_min, rank_max = lower + 1, leq
            n_tied = rank_max - rank_min + 1
    sig_bh = [r for (r, qi, _pi) in zip(testable, q, pb) if qi < cfg.alpha]
    sig_raw = [r for (r, _qi, pi) in zip(testable, q, pb) if pi < cfg.alpha]
    non_target_sig = [r.group_key for r in sig_bh if r.group_key != target_key]
    return {
        "rank_of_target": rank_min,       # 保留舊欄位名（= 最樂觀名次）
        "rank_min": rank_min,
        "rank_max": rank_max,
        "n_tied_at_target": n_tied,
        "n_tests": fdr.n_tests,
        "n_sig_raw": fdr.n_sig_raw,
        "n_sig_bh": fdr.n_sig_bh,
        "n_sig_bonferroni": fdr.n_sig_bonferroni,
        "non_target_significant": non_target_sig,
        "top_group": ordered[0][0].group_key if ordered else None,
        "top_p": ordered[0][0].stats["p_hyper"] if ordered else None,
        "top_or": ordered[0][0].stats["odds_ratio"] if ordered else None,
        "sig_raw_groups": [r.group_key for r in sig_raw],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="M3 方法評估（偵測率 / 偽發現率）")
    ap.add_argument("--membership-csv", default="", help="不給則從資料庫讀（mart.lot_membership）")
    ap.add_argument("--units-csv", default="", help="用真實資料算基準壞率")
    ap.add_argument("--group-type", default="recipe")
    ap.add_argument("--replicates", type=int, default=100)
    ap.add_argument("--effect-ratio", type=float, default=3.0)
    ap.add_argument("--base-rate", type=float, default=0.07)
    ap.add_argument("--window-hours", type=int, default=24)
    ap.add_argument("--fault-type", default="ANY")
    ap.add_argument("--target", default="", help="注入的群組（預設取批次數最多的群組）")
    ap.add_argument("--min-group-units", type=int, default=5)
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=20261009)
    ap.add_argument("--out", default="reports/m3")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--no-db", action="store_true")
    args = ap.parse_args()

    if args.membership_csv:
        lots, groups = load_membership(Path(args.membership_csv), None)
        base_rate = load_base_rate(Path(args.units_csv) if args.units_csv else None,
                                  args.window_hours, args.fault_type, args.base_rate)
    else:
        lots, groups = load_membership_db(None)
        base_rate = load_base_rate_db(args.window_hours, args.fault_type) or args.base_rate
        print(f"[mode] 從資料庫讀取群組結構（mart.lot_membership，{len(lots):,} 批次）")
    cfg = RankConfig(min_group_units=args.min_group_units, alpha=args.alpha)
    targets = {}
    for gtype, gs in groups.items():
        # 決定性選擇：批次數最多的群組；同數量時依 key 排序（避免字典順序造成不可重現）
        key = max(sorted(gs), key=lambda k: (len(gs[k]), k))
        targets[gtype] = key if not args.target else (args.target if args.target in gs else key)

    print("=" * 92)
    print("M3 方法評估：合成注入實驗")
    print("=" * 92)
    print(f"  真實群組結構：{len(lots):,} 個批次｜群組類型 " +
          "、".join(f"{g}({len(groups[g])} 組)" for g in sorted(groups)))
    print(f"  基準壞率（真實資料算出）p0 = {base_rate:.4f}｜注入倍數 = {args.effect_ratio}×｜"
          f"重複次數 = {args.replicates}｜α = {args.alpha}")

    report = {"run_id": args.run_id or f"eval_{args.group_type}_x{args.effect_ratio}",
              "base_rate": base_rate, "effect_ratio": args.effect_ratio,
              "replicates": args.replicates, "seed": args.seed,
              "n_lots": len(lots), "n_groups": {g: len(v) for g, v in groups.items()},
              "scenarios": []}

    for gtype in sorted(groups):
        target = targets[gtype]
        # ── 負控制：沒有根因
        rng = random.Random(args.seed)
        agg_null = Counter()
        null_details = []
        for _ in range(args.replicates):
            units, mem = simulate(lots, groups, gtype, base_rate, rng)
            res = run_once(units, mem, cfg)
            agg_null["n_sig_raw"] += res["n_sig_raw"]
            agg_null["n_sig_bh"] += res["n_sig_bh"]
            agg_null["n_sig_bonferroni"] += res["n_sig_bonferroni"]
            null_details.append(res)
        r = args.replicates
        null_row = {
            "scenario": "null", "group_type": gtype, "target_group": None,
            "effect_ratio": 1.0, "n_replicates": r,
            "detection_top1": None, "detection_top1_strict": None, "detection_top3": None,
            "mean_rank": None, "mean_rank_max": None, "mean_ties_at_target": None,
            "n_sig_raw_mean": agg_null["n_sig_raw"] / r,
            "n_sig_bh_mean": agg_null["n_sig_bh"] / r,
            "n_sig_bonferroni_mean": agg_null["n_sig_bonferroni"] / r,
            "fpr_bh_mean": (agg_null["n_sig_bh"] / r) / max(len(groups[gtype]), 1),
            "detail": f"m×α 期望假陽性 = {len(groups[gtype]) * args.alpha:.1f}",
        }
        report["scenarios"].append(null_row)

        # ── 正控制：注入根因到 target
        rng = random.Random(args.seed + 1)
        agg_inj = Counter()
        inj_details = []
        inj_lots = groups[gtype][target]
        for _ in range(args.replicates):
            units, mem = simulate(lots, groups, gtype, base_rate, rng,
                                  injection={"lots": set(inj_lots), "effect_ratio": args.effect_ratio})
            res = run_once(units, mem, cfg, target_key=target)
            agg_inj["top1"] += 1 if res["rank_min"] == 1 else 0
            agg_inj["top1_strict"] += 1 if res["rank_max"] == 1 else 0
            agg_inj["top3"] += 1 if (res["rank_min"] or 99) <= 3 else 0
            agg_inj["rank_sum"] += res["rank_min"] or 0
            agg_inj["rank_max_sum"] += res["rank_max"] or 0
            agg_inj["ties"] += res["n_tied_at_target"] or 0
            agg_inj["n_sig_raw"] += res["n_sig_raw"]
            agg_inj["n_sig_bh"] += res["n_sig_bh"]
            agg_inj["non_target"] += len(res["non_target_significant"])
            inj_details.append(res)
        inj_row = {
            "scenario": "injected", "group_type": gtype, "target_group": target,
            "effect_ratio": args.effect_ratio, "n_replicates": r,
            "detection_top1": agg_inj["top1"],
            "detection_top1_strict": agg_inj["top1_strict"],
            "detection_top3": agg_inj["top3"],
            "mean_rank": agg_inj["rank_sum"] / r,
            "mean_rank_max": agg_inj["rank_max_sum"] / r,
            "mean_ties_at_target": agg_inj["ties"] / r,
            "n_sig_raw_mean": agg_inj["n_sig_raw"] / r,
            "n_sig_bh_mean": agg_inj["n_sig_bh"] / r,
            "n_sig_bonferroni_mean": None,
            "fpr_bh_mean": (agg_inj["non_target"] / r) / max(len(groups[gtype]) - 1, 1),
            "detail": (f"注入群組 {target}（{len(inj_lots)} 批）；壞率由 {base_rate:.4f} "
                       f"提升到 {min(1.0, base_rate * args.effect_ratio):.4f}"),
        }
        report["scenarios"].append(inj_row)

    # 印出對照表
    print(f"\n  {'群組類型':<22}{'情境':<10}{'注入群組':<12}{'@1(樂觀)':>10}{'@1(嚴格)':>10}"
          f"{'@3':>7}{'名次範圍':>12}{'並列數':>8}{'BH顯著':>8}{'偽發現率':>10}")
    for s in report["scenarios"]:
        d1 = "—" if s["detection_top1"] is None else f"{100*s['detection_top1']/s['n_replicates']:.0f}%"
        d3 = "—" if s["detection_top3"] is None else f"{100*s['detection_top3']/s['n_replicates']:.0f}%"
        strict = "—" if s.get("detection_top1_strict") is None else f"{100*s['detection_top1_strict']/s['n_replicates']:.0f}%"
        if s["mean_rank"] is None:
            mr = "—"
        else:
            mr = f"{s['mean_rank']:.1f}–{s['mean_rank_max']:.1f}"
        ties = "—" if s.get("mean_ties_at_target") is None else f"{s['mean_ties_at_target']:.1f}"
        gt = s["group_type"][:21]
        print(f"  {gt:<22}{s['scenario']:<10}{str(s['target_group'] or '—'):<12}{d1:>10}{strict:>10}"
              f"{d3:>7}{mr:>12}{ties:>8}{s['n_sig_bh_mean']:>8.2f}{s['fpr_bh_mean']:>10.3f}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "method_eval.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                                          encoding="utf-8")
    print(f"\n  報告：{out}/method_eval.json")

    if not args.no_db:
        try:
            write_db(report)
            print(f"  已寫入 mart.method_eval（run_id = {report['run_id']}）")
        except Exception as e:
            print(f"  [提示] 未寫入資料庫（{type(e).__name__}: {e}）——CSV/JSON 報告仍已產生")
    return 0


def write_db(report: dict) -> None:
    import psycopg
    conn = psycopg.connect(
        host=os.environ.get("PGHOST", "db"), port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""),
        dbname=os.environ.get("PGDATABASE", "commonality"))
    with conn.cursor() as cur:
        for s in report["scenarios"]:
            # 負控制沒有注入群組 → 用哨兵值（見 NULL_TARGET 的說明），DELETE／INSERT 用同一個值才對得上
            target = s["target_group"] or NULL_TARGET
            cur.execute("DELETE FROM mart.method_eval WHERE run_id = %s AND scenario = %s "
                        "AND group_type = %s AND target_group = %s",
                        (report["run_id"], s["scenario"], s["group_type"], target))
            cur.execute("""INSERT INTO mart.method_eval
                (run_id, scenario, group_type, target_group, effect_ratio, n_replicates,
                 detection_top1, detection_top3, mean_rank, n_sig_raw_mean, n_sig_bh_mean,
                 fpr_bh_mean, seed, detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (report["run_id"], s["scenario"], s["group_type"], target,
                 s["effect_ratio"], s["n_replicates"], s["detection_top1"], s["detection_top3"],
                 s["mean_rank"], s["n_sig_raw_mean"], s["n_sig_bh_mean"], s["fpr_bh_mean"],
                 report["seed"], s.get("detail")))
        conn.commit()
    conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
