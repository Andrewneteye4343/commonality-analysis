#!/usr/bin/env python3
"""M4-2：多變數 logistic 共同性模型（路線模型）— 解決 M3 的路線共用混淆。

問題（M3 實測）
────────────────────────────────────────────────────────────
M3 的單變數排名把「路線共用」誤認為「機台效應」：
- 完全零故障的 04M01，原始壞率仍有 6.67%（其 258 個壞批次全部來自與其他機台共用的批次）
- 06M01 由全部批次 OR 1.60 變成專屬批次 0.31（方向翻轉）→ 它的風險是「借來的」

模型
────────────────────────────────────────────────────────────
**路線模型（route model）**：以「這批 lot 經過哪些機台」為共變數，**無截距**：

    logit P(bad) = β₁·I(經過 01M02) + β₂·I(經過 02M02) + … + β₅·I(經過 04M01)

- 無截距是刻意的：截距會與「全部機台指示變數」共線；無截距時 `exp(β_t)` 是
  「只經過機台 t 的批次」的勝算（單機台路線 = 只有一個 1）
- 共用路線的批次其對數勝算為各機台係數相加 → **每個 β 都是「控制路線其他成員後」的自身效應**
  （這正是 M3 單變數做法缺的東西）
- **ridge 懲罰是必要的**：零故障機台的專屬批次 k = 0 → 完全分離，最大概似估計不存在
  （M4-1 已實作並測試）。收縮倍數由 `--ridge` 控制，預設 1.0
- **群聚穩健標準誤**：預設以「批次的主要 recipe」分群（同一 recipe 的批次不獨立）。
  注意 M4-1 的實測：群聚穩健標準誤只在共變數位於群聚層級時才明顯膨脹
- 輸出 `expected_bad_rate = expit(β_t)`＝模型推得「只經過這台機台」的壞率，
  與 `raw_bad_rate = k/n` 相減即 `excess_bad_rate`＝**可歸因於路線共用的差距**

用法
────────────────────────────────────────────────────────────
    # 容器內（推薦）
    docker compose run --rm etl python -m analysis.run_logistic --group-types tool \\
        --window-hours 24 --fault-type ANY

    # 加控制項（測試機台效應是否只是 recipe／時段的代理）
    docker compose run --rm etl python -m analysis.run_logistic --group-types tool \\
        --controls recipe --control-top-n 15 --time-trend

    # 與 M3 對照（讀 mart.commonality_results 的 analysis_id）
    ... --compare-with M3_ANY_w24h
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
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

from glm import (  # noqa: E402
    _normal_cdf, _z_quantile, _mu_from_eta, bh_qvalues, cluster_robust_cov, contrast_p,
    cluster_robust_se, hazard_sum_cluster_se,
    hazard_sum_fit, hazard_sum_lrt, logistic_fit, spearman, topk_overlap,
)
from script_version import add_version_arg  # noqa: E402



# ─────────────────────────────────────────────────────────────
# 資料讀取（欄位與 M3 的 run_commonality 完全一致）
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
        cur.execute("""SELECT lot_id, group_type, group_key, n_events FROM mart.lot_membership""")
        memberships = [(r[0], r[1], r[2], int(r[3] or 0)) for r in cur.fetchall()]
        cur.execute("""SELECT lot_id, first_ts, n_events FROM mart.lot_units
                       WHERE window_hours = %s AND fault_type = %s""", (window_hours, fault_type))
        lot_time = {r[0]: (r[1], int(r[2] or 0)) for r in cur.fetchall()}
    conn.close()
    return units, memberships, lot_time


def load_from_csv(units_csv: Path, membership_csv: Path, window_hours: int, fault_type: str):
    units, lot_time = {}, {}
    with units_csv.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if int(r.get("window_hours", window_hours)) != window_hours:
                continue
            if r.get("fault_type", fault_type) != fault_type:
                continue
            units[r["lot_id"]] = str(r["outcome_bad"]).lower() in ("true", "t", "1")
            lot_time[r["lot_id"]] = (r.get("first_ts"), int(r.get("n_events") or 0))
    memberships = []
    with membership_csv.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            memberships.append((r["lot_id"], r["group_type"], r["group_key"],
                                int(r.get("n_events") or 0)))
    return units, memberships, lot_time


# ─────────────────────────────────────────────────────────────
# 設計矩陣
# ─────────────────────────────────────────────────────────────
def build_design(units: dict, memberships: list, group_type: str, *,
                 controls: list[str], control_top_n: int, time_trend: bool,
                 lot_time: dict) -> dict:
    """組出設計矩陣（無截距）。

    回傳 dict：X, y, colnames, lots, group_of_lot, cluster, meta, n_events
    """
    lots = sorted(units)
    y = [1 if units[l] else 0 for l in lots]
    idx = {l: i for i, l in enumerate(lots)}

    mem_by_type: dict[str, list] = defaultdict(list)
    for lot, gtype, gkey, _n in memberships:
        if lot in idx:
            mem_by_type[gtype].append((lot, gkey))

    # ① 主要群組欄位（要檢定的維度）
    keys = sorted({gkey for _lot, gkey in mem_by_type.get(group_type, [])})
    if not keys:
        raise SystemExit(f"[錯誤] 找不到群組類型 {group_type} 的成員（檢查 mart.lot_membership）")
    colnames = [f"{group_type}={k}" for k in keys]
    X = [[0.0] * len(colnames) for _ in lots]
    group_col = {f"{group_type}={k}": j for j, k in enumerate(keys)}
    for lot, gkey in mem_by_type.get(group_type, []):
        X[idx[lot]][group_col[f"{group_type}={gkey}"]] = 1.0

    meta = {"group_type": group_type, "n_groups": len(keys), "control_cols": 0,
            "pooled_levels": {}, "design_note": "", "n_time_cols": 0}

    # ② 控制項（取最常見的前 N 個水準，其餘合併為「其他」）
    for ctype in controls:
        pairs = mem_by_type.get(ctype, [])
        if not pairs:
            continue
        counts = Counter(gkey for _lot, gkey in pairs)
        top = [k for k, _c in counts.most_common(control_top_n)]
        pooled = {k for k in counts if k not in set(top)}
        meta["pooled_levels"][ctype] = len(pooled)
        top_cols = {}
        meta["control_cols"] += len(top)
        for k in top:
            colnames.append(f"{ctype}={k}")
            top_cols[k] = len(colnames) - 1
        other_col = None
        if pooled:
            colnames.append(f"{ctype}=__other__")
            meta["control_cols"] += 1
            other_col = len(colnames) - 1
        for row in X:
            row.extend([0.0] * (len(colnames) - len(row)))
        for lot, gkey in pairs:
            r = idx[lot]
            if gkey in top_cols:
                X[r][top_cols[gkey]] = 1.0
            elif other_col is not None:
                X[r][other_col] = 1.0

    # ③ 時間趨勢（批次層級，標準化到 [0,1]）
    if time_trend:
        vals = []
        for l in lots:
            raw = lot_time.get(l, (None, 0))[0]
            try:
                vals.append(float(raw))
            except (TypeError, ValueError):
                vals.append(float("nan"))
        finite = [v for v in vals if not math.isnan(v)]
        if len(finite) >= 3 and max(finite) > min(finite):
            lo, hi = min(finite), max(finite)
            colnames.append("time_trend")
            meta["n_time_cols"] += 1
            for i, v in enumerate(vals):
                X[i].append(0.0 if math.isnan(v) else (v - lo) / (hi - lo))
        else:
            meta["design_note"] += "（時間欄位不足，未加入時間趨勢）"

    # ④ 分群（群聚穩健標準誤用）：每批的主要 recipe = 字母序最小者（決定性、可重現）
    recipes = defaultdict(list)
    for lot, gkey in mem_by_type.get("recipe", []):
        recipes[lot].append(gkey)
    clusters = [min(recipes[l]) if recipes.get(l) else "__none__" for l in lots]

    meta["design_note"] = (f"無截距；{len(keys)} 個 {group_type} 欄位"
                           + (f"＋{meta['control_cols']} 個控制欄位" if meta["control_cols"] else "")
                           + (f"＋時間趨勢" if meta["n_time_cols"] else "")
                           + meta["design_note"])
    meta["all_cols"] = len(colnames)
    return {"X": X, "y": y, "colnames": colnames, "lots": lots,
            "group_keys": keys, "cluster": clusters, "meta": meta}


# ─────────────────────────────────────────────────────────────
# 擬合與排名
# ─────────────────────────────────────────────────────────────
MODELS = ("logit", "cloglog", "hazard")
MODEL_LABEL = {"logit": "logit（加性對數勝算）",
               "cloglog": "cloglog（風險率相乘）",
               "hazard": "hazard（風險率相加，對「至少一台造成」機制的正確模型）"}


def fit_route_model(design: dict, ridge: float, model: str = "logit",
                    alpha: float = 0.05, lrt_max_groups: int = 0
                    ) -> tuple[list[dict], dict]:
    """擬合路線模型。

    三種模型解決同一個問題（控制路線其他成員的混淆），但對「壞批次機制」的假設不同：
    - `logit`   ：勝算 = Π OR_t^{I_t}（相乘）；ROADMAP 指定的標準做法
    - `cloglog` ：λ = Π h_t^{I_t}（相乘的風險率）
    - `hazard`  ：λ = Σ h_t·I_t（**相加**）—— 壞批次定義是「曾落在**任一**機台故障窗內」，
                  機制上就是相加，因此這是唯一完全匹配的模型（實測偏差最小）
    回傳 (rows, 診斷 dict)；rows 內 or_ 欄位存「效果量」（OR 或風險率，由 effect_type 標示）。
    """
    X, y, names = design["X"], design["y"], design["colnames"]
    gtype = design["meta"]["group_type"]

    if model in ("logit", "cloglog"):
        fit = logistic_fit(X, y, ridge=ridge, colnames=names, link=model)
        V, info = cluster_robust_cov(X, y, fit.beta, design["cluster"], ridge=ridge,
                                     colnames=names, link=model)
        cse = [math.sqrt(max(V[i][i], 0.0)) for i in range(len(V))]
        beta, se, se_c, theta = fit.beta, fit.se, cse, fit.beta
        effect_type = "OR"
        # 無截距模型的 β_j = 0 檢定等同「基準機率 = 50%」，沒有意義（負控制下會全判顯著）
        # → 改用「該台 vs 其他台加權平均」的對比檢定
        p_method = "contrast_vs_others"
        diag = {"converged": fit.converged, "n_iter": fit.n_iter,
                "warning": fit.separation_warning, "loglik": fit.loglik}
    elif model == "hazard":
        fit = hazard_sum_fit(X, y, ridge=max(ridge * 0.01, 1e-4), colnames=names)
        cse, info = hazard_sum_cluster_se(X, y, fit["theta"], design["cluster"],
                                          ridge=max(ridge * 0.01, 1e-4))
        theta = fit["theta"]
        beta, se, se_c = fit["theta"], fit["se"], cse
        effect_type = "HR"
        # hazard 模型的檢定必須用邊界修正的 LRT（H₀: h = 0），
        # Wald 檢定在此會退化（它測的是 h = 1，毫無意義）——實測踩到
        p_method = "lrt_boundary"
        lrt = {}
        # LRT 的成本是「每群一次額外擬合」→ 群組多時（例：recipe 維度 156 群）只對最大的
        # N 群做，其餘標為未列入檢定（**不靜默過濾**：diag 會列出被略過的群組）
        lrt_keys = list(design["group_keys"])
        skipped_lrt: list[str] = []
        if lrt_max_groups and len(lrt_keys) > lrt_max_groups:
            sizes = {k: sum(1 for r in X if r[names.index(f"{gtype}={k}")] == 1.0)
                     for k in lrt_keys}
            lrt_keys = sorted(lrt_keys, key=lambda k: -sizes[k])[:lrt_max_groups]
            skipped_lrt = sorted(set(design["group_keys"]) - set(lrt_keys))
        for key in lrt_keys:
            lrt[key] = hazard_sum_lrt(X, y, theta, names.index(f"{gtype}={key}"),
                                     ridge=max(ridge * 0.01, 1e-4))
        diag = {"converged": fit["converged"], "n_iter": fit["n_iter"], "warning": "",
                "loglik": fit["loglik"], "skipped_lrt": skipped_lrt}
    else:
        raise ValueError(f"未知的模型：{model}")

    z = _z_quantile(1 - alpha / 2)
    rows = []
    for j, key in enumerate(design["group_keys"]):
        se_c_i = None
        col = f"{gtype}={key}"
        i = names.index(col)
        n = sum(1 for r in X if r[i] == 1.0)
        k = sum(1 for r, yy in zip(X, y) if r[i] == 1.0 and yy == 1)
        est_c = None
        if model == "hazard":
            effect = math.exp(theta[i])
            expected = 1.0 - math.exp(-effect)
            lr, p = lrt[key] if key in lrt else (None, None)
            se_c_i = se_c[i]
        else:
            effect = math.exp(theta[i])
            expected = _mu_from_eta(theta[i], model)
            p = 2 * (1 - _normal_cdf(abs(theta[i] / se_c[i]))) if se_c[i] > 0 else 1.0
            # 對比檢定：c = e_i − 其他群組的（依批次數加權）平均
            others = [jj for jj in range(len(design["group_keys"])) if jj != i]
            n_other = {jj: sum(1 for r in X if r[jj] == 1.0) for jj in others}
            tot = sum(n_other.values())
            if tot > 0:
                c = [0.0] * len(theta)
                c[i] = 1.0
                for jj, w in n_other.items():
                    c[jj] = -w / tot
                est_c, p = contrast_p(c, theta, V)
                var_c = sum(c[a] * V[a][b] * c[b]
                            for a in range(len(c)) for b in range(len(c)))
                # 注意：不可覆寫 se_c（那是整個係數的 SE 串列），否則下面的 se_c[i] 會爆
                se_contrast = math.sqrt(max(var_c, 0.0))
                se_c_i = se_contrast
                effect = math.exp(est_c)                 # 對比 OR（與其他台的比值）
                expected = _mu_from_eta(theta[i], model)  # 水準仍報單獨路線的機率
        rows.append({
            "model": model, "effect_type": effect_type, "p_method": p_method,
            "group_type": gtype, "group_key": key,
            "n_group": n, "k_bad": k,
            "raw_bad_rate": (k / n) if n else None,
            "beta": theta[i], "se": se[i], "se_cluster": se_c[i],
            "se_for_ci": se_c_i if model in ("logit", "cloglog") else se_c[i],
            "or_": effect,
            "or_ci_low": effect * math.exp(-z * (se_c_i if model in ("logit", "cloglog") else se_c[i])),
            "or_ci_high": effect * math.exp(z * (se_c_i if model in ("logit", "cloglog") else se_c[i])),
            "raw_beta": theta[i],
            "contrast": est_c if model in ("logit", "cloglog") else None,
            "p_wald": p,
            "expected_bad_rate": expected,
            "excess_bad_rate": ((k / n) if n else 0.0) - expected,
        })
    # BH 只針對有做檢定的列（未列入者 p = None，不參與校正、不排名）
    tested = [i for i, r in enumerate(rows) if r["p_wald"] is not None]
    q = bh_qvalues([rows[i]["p_wald"] for i in tested])
    qmap = {i: q[j] for j, i in enumerate(tested)}
    for rank, i in enumerate(sorted(tested, key=lambda i: rows[i]["p_wald"]), start=1):
        rows[i]["q_bh"] = qmap[i]
        rows[i]["rank_bh"] = rank
    for r in rows:
        if r["p_wald"] is None:
            r["q_bh"] = None
            r["rank_bh"] = None
    rows.sort(key=lambda r: (r["p_wald"] is None,
                             r["p_wald"] if r["p_wald"] is not None else 0.0))
    n_par = len(names)
    n_cl = info.get("n_clusters", 0)
    ratio = (n_cl / n_par) if n_par else 0.0
    if n_par and ratio < 5:
        # 群聚穩健 SE 的 meat 只有 G 個項，卻要估 k 個參數 → G 不夠大時 SE 嚴重低估。
        # 實測（均勻壞率負控制、40 個 recipe 欄位）：G=40 → 誤判 3/40；G=20 → 11.5/40；
        # G=8 → 16.5/40。因此 G/k < 5 時 p 值不可信。
        diag["se_warning"] = (f"群聚數 {n_cl}／參數數 {n_par} = {ratio:.1f}（建議 ≥ 5）"
                              f"→ 群聚穩健 p 值不可信；請看效果量或改用 M3／收縮法")
    else:
        diag["se_warning"] = ""
    diag["n_params"] = n_par
    diag["cluster_per_param"] = ratio
    return rows, {"se_info": info, **diag}


def compare_with_m3(conn_rows: list[dict], rows: list[dict], group_type: str) -> dict:
    """與 M3 的單變數結果比較：名次相關與前 k 名重疊率"""
    by_key = {r["group_key"]: r for r in conn_rows if r["group_type"] == group_type}
    common = [r for r in rows if r["group_key"] in by_key]
    if len(common) < 2:
        return {"n_common": len(common), "note": "共同群組不足，無法計算重疊度"}
    m3_order = sorted(common, key=lambda r: by_key[r["group_key"]]["p_hyper"])
    m4_order = sorted(common, key=lambda r: r["p_wald"])
    rank = {r["group_key"]: i for i, r in enumerate(m3_order)}
    rank4 = {r["group_key"]: i for i, r in enumerate(m4_order)}
    k3 = min(3, len(common))
    return {
        "n_common": len(common),
        "spearman_p": spearman([rank[r["group_key"]] for r in common],
                               [rank4[r["group_key"]] for r in common]),
        "spearman_or": spearman([by_key[r["group_key"]]["odds_ratio"] for r in common],
                                [r["or_"] for r in common]),
        "top3_overlap": topk_overlap([r["group_key"] for r in m3_order],
                                     [r["group_key"] for r in m4_order], k3),
        "m3_top": [r["group_key"] for r in m3_order[:k3]],
        "m4_top": [r["group_key"] for r in m4_order[:k3]],
    }


# ─────────────────────────────────────────────────────────────
# 輸出
# ─────────────────────────────────────────────────────────────
CSV_COLS = ["model", "effect_type", "p_method", "group_type", "group_key", "n_group", "k_bad",
            "raw_bad_rate", "beta", "se", "se_cluster", "or_", "or_ci_low", "or_ci_high",
            "p_wald", "q_bh", "rank_bh", "expected_bad_rate", "excess_bad_rate"]


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_summary(rows: list[dict], design: dict, meta_extra: dict, path: Path,
                  window_hours: int, fault_type: str, ridge: float) -> None:
    L = [f"# M4-2 路線模型（多變數）結果｜{fault_type}｜退化窗 {window_hours} 小時\n"]
    L.append(f"- 分析單位（批次）：{len(design['lots']):,}｜壞批次：{sum(design['y']):,}｜"
             f"群組類型：{design['meta']['group_type']}（{design['meta']['n_groups']} 組）")
    L.append(f"- 設計矩陣：{design['meta']['design_note']}｜ridge = {ridge}")
    L.append(f"- 標準誤：群聚穩健（以批次的主要 recipe 分群，"
             f"{len(set(design['cluster'])):,} 群）\n")
    L.append("## 三種模型為什麼會給不同答案\n")
    L.append("壞批次的定義是「批次曾落在**任一**所屬機台的故障窗內」＝「至少一台造成」的機制。")
    L.append("- `logit`／`cloglog` 是**線性**預測式，對指示變數等同「效果量相乘」"
             "（勝算或風險率相乘）→ 路線長度會變時只能妥協逼近，容易高估無辜群組")
    L.append("- `hazard`（風險率相加，`λ = Σ h_t·I_t`）才是與機制**完全匹配**的模型\n")

    for model in [m for m in MODELS]:
        mrows = [r for r in rows if r.get("model") == model]
        if not mrows:
            continue
        etype = mrows[0]["effect_type"]
        info = (meta_extra.get("models") or {}).get(model, {})
        L.append(f"### {MODEL_LABEL[model]}")
        L.append(f"（iteration {info.get('n_iter', '?')}、收斂 {info.get('converged')}、"
                 f"對數概似 {info.get('loglik', float('nan')):.1f}）")
        if info.get("comparison") and "note" not in info["comparison"]:
            c = info["comparison"]
            L.append(f"- 與 M3 對照：共同群組 {c['n_common']}｜名次 Spearman "
                     f"{c['spearman_p']:.3f}｜前 3 名重疊率 {c['top3_overlap']:.3f}"
                     f"（M3：{'、'.join(c['m3_top'])}｜M4：{'、'.join(c['m4_top'])}）")
        L.append(f"\n| 群組 | n | k | 原始壞率 | 模型推得壞率 | 差距（污染量） | {etype} | 95% CI | p_wald | q_bh |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for r in mrows:
            L.append(f"| {r['group_key']} | {r['n_group']:,} | {r['k_bad']:,} | "
                     f"{100 * r['raw_bad_rate']:.2f}% | {100 * r['expected_bad_rate']:.2f}% | "
                     f"{100 * r['excess_bad_rate']:+.2f}% | {r['or_']:.3f} | "
                     f"{r['or_ci_low']:.3f}–{r['or_ci_high']:.3f} | {r['p_wald']:.3e} | "
                     f"{r['q_bh']:.3e} |")
        L.append("")
    L.append("---\n")
    L.append("**怎麼讀**：`模型推得壞率`＝控制路線其他成員後，該群組自己的貢獻；"
             "`差距`＝原始壞率中被其他（共線）群組拉高的部分。"
             "差距接近原始壞率 → 該群組的原始風險幾乎全部來自路線共用，而非自身。")
    L.append("**注意**：模型推得壞率的可信度取決於模型與機制是否匹配；"
             "報告時應同時附上模型無關的「專屬批次比率」（`run_commonality --exclusive-lots`）。")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def write_db(run_id: str, rows: list[dict], model_note: str, design: dict,
             ridge: float, n_events: int, model: str) -> None:
    import psycopg
    conn = psycopg.connect(
        host=os.environ.get("PGHOST", "db"), port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""),
        dbname=os.environ.get("PGDATABASE", "commonality"))
    method = "hazard_sum" if model == "hazard" else model
    cols = ("run_id, group_type, group_key, method, effect_type, model_note, n_obs, n_events, "
            "n_group, k_bad, beta, se, se_cluster, or_, or_ci_low, or_ci_high, p_wald, q_bh, "
            "rank_bh, raw_bad_rate, expected_bad_rate, excess_bad_rate, ridge")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM mart.logistic_results WHERE run_id = %s", (run_id,))
        with cur.copy(f"COPY mart.logistic_results ({cols}) FROM STDIN") as cp:
            for r in rows:
                cp.write_row([run_id, r["group_type"], r["group_key"], method, r["effect_type"],
                              model_note, len(design["lots"]), n_events, r["n_group"], r["k_bad"],
                              r["beta"], r["se"], r["se_cluster"], r["or_"], r["or_ci_low"],
                              r["or_ci_high"], r["p_wald"], r["q_bh"], r["rank_bh"],
                              r["raw_bad_rate"], r["expected_bad_rate"], r["excess_bad_rate"],
                              ridge])
        conn.commit()
    conn.close()


def load_m3_rows(analysis_id: str) -> list[dict]:
    import psycopg
    conn = psycopg.connect(
        host=os.environ.get("PGHOST", "db"), port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""),
        dbname=os.environ.get("PGDATABASE", "commonality"))
    with conn.cursor() as cur:
        cur.execute("""SELECT group_type, group_key, p_hyper, odds_ratio
                       FROM mart.commonality_results WHERE analysis_id = %s""", (analysis_id,))
        rows = [{"group_type": r[0], "group_key": r[1], "p_hyper": r[2], "odds_ratio": r[3]}
                for r in cur.fetchall()]
    conn.close()
    return rows


# ─────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="M4-2 路線模型（多變數 logistic 共同性分析）")
    ap.add_argument("--window-hours", type=int, default=24)
    ap.add_argument("--fault-type", default="ANY")
    ap.add_argument("--group-types", default="tool", help="要檢定的群組類型（預設 tool）")
    ap.add_argument("--controls", default="", help="控制項群組類型（逗號分隔，例如 recipe,stage）")
    ap.add_argument("--control-top-n", type=int, default=15, help="控制項最多取幾個水準（其餘合併）")
    ap.add_argument("--time-trend", action="store_true", help="加入批次時間趨勢項")
    ap.add_argument("--ridge", type=float, default=1.0, help="ridge 懲罰強度（必須 > 0）")
    ap.add_argument("--lrt-max-groups", type=int, default=0,
                    help="hazard 的 LRT 只對最大的 N 個群組做（0 = 全部）；"
                         "其餘標為未列入檢定並在報告列明")
    ap.add_argument("--model", default="all", choices=["logit", "cloglog", "hazard", "all"],
                    help="模型：logit（加性對數勝算，相乘的勝算）／cloglog（相乘的風險率）／"
                         "hazard（風險率相加，對「至少落在任一機台故障窗內」的機制正確）／all（三者都跑）")
    ap.add_argument("--units-csv", default="")
    ap.add_argument("--membership-csv", default="")
    ap.add_argument("--analysis-id", default="")
    ap.add_argument("--compare-with", default="", help="要對照的 M3 analysis_id")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--out", default="reports/m4")
    ap.add_argument("--no-db", action="store_true")
    add_version_arg(ap)
    args = ap.parse_args()

    if args.ridge <= 0:
        print("[錯誤] --ridge 必須 > 0（完全分離需要懲罰才有有限解）")
        return 2

    mode = "csv" if args.units_csv else "db"
    if mode == "db":
        units, memberships, lot_time = load_from_db(args.window_hours, args.fault_type)
        n_events = sum(n for _t, n in lot_time.values())
        print(f"[mode] 從資料庫讀取（mart.lot_units / mart.lot_membership，{len(units):,} 批次）")
    else:
        units, memberships, lot_time = load_from_csv(
            Path(args.units_csv), Path(args.membership_csv), args.window_hours, args.fault_type)
        n_events = sum(n for _t, n in lot_time.values())
        print(f"[mode] 從 CSV 讀取（{len(units):,} 批次）")
    if not units:
        print("[錯誤] 沒有批次單位——先跑 migrate.py 與 load_fault_labels.py")
        return 2

    gtype = args.group_types.split(",")[0].strip()
    controls = [c.strip() for c in args.controls.split(",") if c.strip()]
    design = build_design(units, memberships, gtype, controls=controls,
                          control_top_n=args.control_top_n, time_trend=args.time_trend,
                          lot_time=lot_time)
    print(f"  設計矩陣：{len(design['lots']):,} 列 × {len(design['colnames'])} 欄"
          f"（{design['meta']['design_note']}）")
    models = list(MODELS) if args.model == "all" else [args.model]
    all_rows: list[dict] = []
    meta_extra: dict = {"ridge": args.ridge, "models": {}, "meta": design["meta"]}
    m3_rows = load_m3_rows(args.compare_with) if (args.compare_with and mode == "db") else []

    for model in models:
        rows, diag = fit_route_model(design, args.ridge, model=model,
                                     lrt_max_groups=args.lrt_max_groups)
        all_rows.extend(rows)
        if diag.get("warning"):
            print(f"  [提醒] {model}：{diag['warning']}")
        if not diag.get("converged"):
            print(f"  [提醒] {model}：未收斂（{diag['n_iter']} 次迭代）")
        si = diag["se_info"]
        if diag.get("se_warning"):
            print(f"  [警告] {diag['se_warning']}")
        print(f"\n  路線模型（{gtype}）｜{MODEL_LABEL[model]}｜ridge = {args.ridge}｜"
              f"群聚穩健標準誤：{si['n_clusters']} 群（{si['inflation']:.4f} 倍修正）")
        print(f"  {'群組':<10}{'n':>6}{'k':>5}{'原始壞率':>10}{'模型壞率':>10}{'差距':>9}"
              f"{rows[0]['effect_type']:>7}{'p_wald':>12}{'q_bh':>12}")
        for r in rows:
            p_txt = f"{r['p_wald']:>12.3e}" if r['p_wald'] is not None else f"{'未檢定':>12}"
            q_txt = f"{r['q_bh']:>12.3e}" if r['q_bh'] is not None else f"{'—':>12}"
            print(f"  {r['group_key']:<10}{r['n_group']:>6,}{r['k_bad']:>5,}"
                  f"{100 * r['raw_bad_rate']:>9.2f}%{100 * r['expected_bad_rate']:>9.2f}%"
                  f"{100 * r['excess_bad_rate']:>+8.2f}%{r['or_']:>7.3f}{p_txt}{q_txt}")
        entry = {"ridge": args.ridge, "n_iter": diag["n_iter"],
                 "converged": diag["converged"], "loglik": diag.get("loglik"),
                 "n_clusters": si["n_clusters"]}
        if m3_rows:
            comp = compare_with_m3(m3_rows, rows, gtype)
            entry["comparison"] = comp
            if "note" in comp:
                print(f"    與 M3 對照：{comp['note']}")
            else:
                print(f"    與 M3（{args.compare_with}）對照：名次 Spearman {comp['spearman_p']:.3f}｜"
                      f"效果量 Spearman {comp['spearman_or']:.3f}｜"
                      f"前 3 名重疊率 {comp['top3_overlap']:.3f}")
                print(f"      M3 前 3：{'、'.join(comp['m3_top'])}｜M4 前 3：{'、'.join(comp['m4_top'])}")
        meta_extra["models"][model] = entry

    out = Path(args.out)
    write_csv(all_rows, out / "logistic_ranking.csv")
    write_summary(all_rows, design, meta_extra, out / "logistic_summary.md",
                  args.window_hours, args.fault_type, args.ridge)
    (out / "logistic_meta.json").write_text(
        json.dumps(meta_extra, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n  輸出：{out}/logistic_ranking.csv、logistic_summary.md、logistic_meta.json")

    if mode == "db" and not args.no_db:
        for model in models:
            rows = [r for r in all_rows if r["model"] == model]
            base = args.run_id or f"M4_{gtype}_w{args.window_hours}h_r{args.ridge}"
            run_id = f"{base}_{model}"
            rows0 = [r for r in all_rows if r["model"] == model]
            pm = rows0[0]["p_method"] if rows0 else "wald"
            model_note = (f"{design['meta']['design_note']}；model={model}；ridge={args.ridge}；"
                          f"群聚=recipe；p={pm}")
            write_db(run_id, rows, model_note, design, args.ridge, n_events, model)
            print(f"  已寫入 mart.logistic_results（run_id = {run_id}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
