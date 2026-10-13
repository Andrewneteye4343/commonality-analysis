"""M5-2-0 前置診斷：多變量 MSPC（PCA + Hotelling T² + SPE）的可行性檢查。

為什麼要「先診斷再建模」
────────────────────────────────────────────────────────────
M5-1 的教訓：單變量管制圖做完才發現真實資料不滿足平穩假設，整條路走了一趟。
MSPC 有它自己的前提（多變量常態近似、共變異數矩陣良態、足夠的基準期樣本），
所以先用**同一份真實資料**回答四個問題，再決定模型怎麼設：

  A 完整性：有多少事件擁有「所有變數都非缺失」的完整向量？（MSPC 需要完整向量）
  B 樣本數：每個機台的基準期完整事件數是否足夠估計 p×p 共變異數矩陣
  C 共線性：主成分能壓縮到幾維？條件數多差？（共線性高是 MSPC 的利基，不是問題）
  D 尾部：基準期的馬氏距離分布有多偏離多變量常態 → 管制線該用理論線還是經驗分位數
  E 漂移：**先移除或先不移除各變數的趨勢**，會讓主成分結構完全不同（本專案資料會漂移）

輸出為 JSON 報告 + 螢幕摘要；**唯讀，不寫資料庫**。

用法
    # 資料庫模式（容器內）
    docker compose run --rm etl python analysis/mspc_precheck.py \
        --tools "01,02,03,04,06" \
        --sensors FLOWCOOLPRESSURE,IONGAUGEPRESSURE,ETCHBEAMVOLTAGE,ETCHBEAMCURRENT,ACTUALSTEPDURATION \
        --baseline-frac 0.3

    # 本機 CSV 驗證模式（助理端）
    python analysis/mspc_precheck.py --series-csv <檔> --no-db
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from collections import defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent / "etl", _HERE.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np                                          # noqa: E402

from script_version import add_version_arg                  # noqa: E402

DEFAULT_FEATURES = ("mean_val", "std_val", "range_val", "slope", "missing_rate")


# ── 事件 × 變數 矩陣 ─────────────────────────────────────────────
def build_matrix(series_iter, features, baseline_frac, max_events, sensors=None):
    """把逐（機台 × step × 感測器）的序列，樞紐成「每個機台一張 事件 × 變數 矩陣」。

    回傳 {tool: {"ts": [...], "events": [...], "vars": [...], "X": [[...]]}}
    """
    per_tool: dict[str, dict] = defaultdict(
        lambda: {"ts": {}, "steps": {}, "cells": defaultdict(dict), "sensors": set()})
    keep_sensors = set(sensors) if sensors else None
    for (tool, step, sensor), rows in series_iter:
        if keep_sensors and sensor not in keep_sensors:
            continue
        t = per_tool[tool]
        t["sensors"].add(sensor)
        for r in rows:
            eid = r["event_id"]
            t["ts"][eid] = r["in_ts"]
            t["steps"][eid] = step
            for f in features:
                v = r.get(f)
                if v is not None:
                    t["cells"][eid][(sensor, f)] = float(v)

    out = {}
    for tool, t in per_tool.items():
        vars_ = sorted({k for cells in t["cells"].values() for k in cells})
        events = sorted(t["ts"], key=lambda e: (t["ts"][e], e))
        if max_events:
            events = events[:max_events]
        X = np.full((len(events), len(vars_)), np.nan, dtype=float)
        idx = {v: i for i, v in enumerate(vars_)}
        for i, e in enumerate(events):
            for k, v in t["cells"].get(e, {}).items():
                if k in idx:
                    X[i, idx[k]] = v
        out[tool] = {"events": events, "vars": [f"{s}|{f}" for s, f in vars_],
                     "ts": [t["ts"][e] for e in events],
                     "steps": [t["steps"].get(e, "?") for e in events], "X": X}
    return out


# ── 統計工具（不使用 scipy：以模擬取代理論分位數）────────────────
def pca_spectrum(X: np.ndarray) -> dict:
    """對已標準化的矩陣做 PCA（以相關矩陣的特徵分解）。"""
    n, p = X.shape
    C = np.corrcoef(X, rowvar=False)
    C = np.nan_to_num(C, nan=0.0)
    ev = np.linalg.eigvalsh(C)[::-1].clip(min=1e-12)
    cum = np.cumsum(ev) / ev.sum()
    return {"p": int(p), "n": int(n),
            "top1": float(cum[0]), "top2": float(cum[1]) if p > 1 else 1.0,
            "top3": float(cum[2]) if p > 2 else 1.0,
            "top5": float(cum[min(4, p - 1)]),
            "k90": int(np.searchsorted(cum, 0.90) + 1),
            "k95": int(np.searchsorted(cum, 0.95) + 1),
            "k99": int(np.searchsorted(cum, 0.99) + 1),
            "cond": float(ev[0] / ev[-1]),
            "ev_min": float(ev[-1])}


def mahalanobis_d2(X: np.ndarray) -> np.ndarray:
    """以樣本平均與樣本共變異數計算馬氏距離平方（Phase I：同一批資料）。"""
    mu = X.mean(axis=0)
    S = np.cov(X, rowvar=False)
    try:
        Si = np.linalg.pinv(S)
    except np.linalg.LinAlgError:
        return np.zeros(X.shape[0])
    d = X - mu
    return np.einsum("ij,jk,ik->i", d, Si, d)


def mardia_kurtosis(X: np.ndarray, d2: np.ndarray) -> dict:
    """多變量峰度檢定（常態時 b2 = p(p+2)，Var = 8p(p+2)/n）。"""
    n, p = X.shape
    b2 = float(np.mean(d2 ** 2))
    exp = p * (p + 2)
    var = 8.0 * p * (p + 2) / n
    return {"b2": b2, "expected": exp, "z": (b2 - exp) / math.sqrt(var) if var else 0.0}


def mardia_skewness_subsample(X: np.ndarray, d2: np.ndarray, m: int = 1200,
                             seed: int = 7) -> dict:
    """多變量偏度（O(n²) → 取子樣本）。常態時 b1 = p(p+2)(p+1)/n，Var = 8p(p+2)(p+1)/n²。"""
    n, p = X.shape
    rng = np.random.default_rng(seed)
    if n > m:
        sel = rng.choice(n, size=m, replace=False)
        mu = X.mean(axis=0)
        S = np.cov(X, rowvar=False)
        d = X[sel] - mu
        Si = np.linalg.pinv(S)
        dd = np.einsum("ij,jk,ik->i", d, Si, d)
    else:
        dd = d2
        m = n
    b1 = float(np.sum(dd ** 3) / (m * m))
    exp = p * (p + 2) * (p + 1) / m
    var = 8.0 * p * (p + 2) * (p + 1) / (m * m)
    return {"b1": b1, "expected": exp, "z": (b1 - exp) / math.sqrt(var) if var else 0.0}


def d2_limit_exact(p: int, n: int, q: float = 0.9973):
    """Phase I 馬氏距離平方的**精確**分位數（有 scipy 時）。

    標準結果：d²_i ~ ((n-1)²/n) · Beta(p/2, (n-p-1)/2)（以同一批資料估計平均與共變異數時）。
    容器內有 scipy，就用它；沒有（助理端）就回 None，改用同構模擬。
    """
    if n <= p + 1:
        return None
    try:
        from scipy.stats import beta as _beta
    except ImportError:
        return None
    return float((n - 1) ** 2 / n * _beta.ppf(q, p / 2, (n - p - 1) / 2))


def chi2_limit_simulated(p: int, n: int, q: float = 0.9973,
                         n_sim: int = 400, seed: int = 11) -> float:
    """以模擬取得 Phase I 馬氏距離平方在 q 分位的理論值（無 scipy，用同構模擬最可靠）。"""
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n_sim):
        Z = rng.standard_normal((n, p))
        vals.append(np.quantile(mahalanobis_d2(Z), q))
    return float(np.mean(vals))


def detrend(X: np.ndarray) -> np.ndarray:
    """逐變數以最小平方直線去除趨勢（模擬「先移除機台老化」的情境）。"""
    n = X.shape[0]
    t = np.arange(n, dtype=float)
    out = X.copy()
    for j in range(X.shape[1]):
        y = X[:, j]
        m = ~np.isnan(y)
        if m.sum() < 3 or np.nanstd(y) == 0:
            continue
        A = np.vstack([t[m], np.ones(m.sum())]).T
        coef, *_ = np.linalg.lstsq(A, y[m], rcond=None)
        out[:, j] = y - (coef[0] * t + coef[1])
    return out


def step_variance_share(X: np.ndarray, steps: list) -> dict:
    """每個變數的變異有多少被 recipe_step 解釋（單因子 ANOVA 的 R²）。

    比例高 → 同一機台內不同步驟本來就不同水準 → 全部混進一張 PCA 會產生混合分布
    （巨大峰度與偏度）。正確做法是**逐步驟建模**（業界 FDC 也是 per-recipe/step）。
    """
    rs = []
    for j in range(X.shape[1]):
        y = X[:, j]
        ok_m = ~np.isnan(y)
        if ok_m.sum() < 10:
            continue
        y = y[ok_m]
        g = np.array([steps[i] for i in range(len(steps)) if ok_m[i]], dtype=object)
        ss_tot = float(((y - y.mean()) ** 2).sum())
        if ss_tot <= 0:
            continue
        ss_between = 0.0
        for lev in set(g.tolist()):
            sel = (g == lev)
            if sel.sum() >= 2:
                ss_between += sel.sum() * (y[sel].mean() - y.mean()) ** 2
        rs.append(ss_between / ss_tot)
    if not rs:
        return {"n_vars": 0, "mean_r2": 0.0, "median_r2": 0.0, "n_high": 0}
    arr = np.array(rs)
    return {"n_vars": int(len(arr)), "mean_r2": float(arr.mean()),
            "median_r2": float(np.median(arr)), "n_high": int((arr > 0.5).sum())}


def centre_by_step(Zn: np.ndarray, steps: list) -> np.ndarray:
    """把每個變數依「所屬 recipe_step」各自扣掉基準期均值（移除步驟混合）。"""
    out = Zn.copy()
    g = np.array(steps, dtype=object)
    for lev in set(g.tolist()):
        sel = (g == lev)
        if sel.sum() >= 2:
            out[sel] -= out[sel].mean(axis=0)
    return out


def log_transform(X: np.ndarray, positive_cols: list) -> np.ndarray:
    """對「基準期非負且有變異」的變數取 log1p（slope / mean_val 可為負 → 不可取對數）。

    range_val / std_val / slope 這類正值右偏變數的重尾，是 MSPC 理論管制線失效的
    主因（實測 q99.73 差 15 倍）。取對數是最便宜且可解釋的修正。
    """
    out = X.copy()
    for j in positive_cols:
        out[:, j] = np.log1p(out[:, j])
    return out


def report_block(X: np.ndarray, label: str, q: float) -> dict:
    """單一情境（原始 or 去趨勢）的完整診斷。"""
    n, p = X.shape
    spec = pca_spectrum(X)
    d2 = mahalanobis_d2(X)
    k = mardia_kurtosis(X, d2)
    s = mardia_skewness_subsample(X, d2)
    emp = [float(np.quantile(d2, v)) for v in (0.5, 0.9, 0.99, q)]
    theo = chi2_limit_simulated(p, n, q)
    exact = d2_limit_exact(p, n, q)
    # 自製檢查器要先被驗證：模擬值與精確值必須接近（差距 >10% 就表示模擬或公式有問題）
    check = None if exact is None else abs(theo - exact) / exact
    return {"label": label, "pca": spec,
            "d2_quantiles": {"q50": emp[0], "q90": emp[1], "q99": emp[2], f"q{q}": emp[3]},
            "d2_theory_simulated": theo,
            "d2_theory_exact_beta": exact,
            "simulation_vs_exact_rel_diff": check,
            "empirical_over_theoretical": emp[3] / theo if theo else None,
            "mardia_kurtosis": k, "mardia_skewness": s,
            "baseline_alarm_rate_at_theory": float(np.mean(d2 > theo))}


def main() -> int:
    ap = argparse.ArgumentParser(description="M5-2-0 MSPC 前置診斷（唯讀）")
    ap.add_argument("--tools", default="01,02,03,04,06")
    ap.add_argument("--sensors", default=",".join(
        ["FLOWCOOLPRESSURE", "IONGAUGEPRESSURE", "ETCHBEAMVOLTAGE",
         "ETCHBEAMCURRENT", "ACTUALSTEPDURATION"]))
    ap.add_argument("--features", default=",".join(DEFAULT_FEATURES))
    ap.add_argument("--baseline-frac", type=float, default=0.3)
    ap.add_argument("--series-csv", default="")
    ap.add_argument("--no-db", action="store_true")
    ap.add_argument("--max-events", type=int, default=0)
    ap.add_argument("--report", default="reports/m5/mspc_precheck.json")
    add_version_arg(ap)
    args = ap.parse_args()

    import run_fdc
    features = run_fdc.parse_csv_list(args.features)
    tools_in = run_fdc.parse_csv_list(args.tools)
    sensors_in = run_fdc.parse_csv_list(args.sensors)

    if args.series_csv:
        series_iter = run_fdc.iter_csv_series(Path(args.series_csv), features)
        tools, sensors = tools_in, sensors_in
    else:
        import psycopg
        conn = psycopg.connect(host=os.environ.get("PGHOST", "db"),
                               port=int(os.environ.get("PGPORT", 5432)),
                               dbname=os.environ.get("PGDATABASE", "commonality"),
                               user=os.environ["PGUSER"],
                               password=os.environ.get("PGPASSWORD", ""))
        tools, missing = run_fdc.resolve_tool_ids(conn.cursor(), tools_in)
        if missing:
            # 過濾條件命中 0 筆是錯誤，不是結果 → 直接失敗，不要安靜地產生空報告
            print(f"[代號] 解析不到的機台 {missing}（exit 2）→ 請確認 --tools 用的是 "
                  f"資料庫裡的 tool_id，或用前綴（例如 \"03\" 會解析到 03M01）")
            return 2
        sensors, missing_s = run_fdc.resolve_sensors(conn.cursor(), sensors_in)
        if missing_s:
            print(f"[感測器] 解析不到：{missing_s}")
            return 2
        print(f"[代號] --tools {list(tools_in)} → {tools}")
        print(f"[感測器] 解析到 {len(sensors)} 個：{sensors}")
        series_iter = run_fdc.iter_db_series(tools, sensors, features, "ANY")

    mats = build_matrix(series_iter, features, args.baseline_frac, args.max_events,
                        sensors=sensors)
    if not mats:
        print("[錯誤] 掃描到 0 個事件 → 不是「沒有異常」，是根本沒查到資料（exit 3）")
        return 3

    q = 1.0 - 0.0027
    import script_version
    report = {"version": script_version.current_version(), "baseline_frac": args.baseline_frac,
              "features": list(features), "tools": tools, "tools_out": {}}

    print(f"\n[設定] 基準期比例 {args.baseline_frac}｜特徵 {list(features)}｜"
          f"目標名目誤報率 0.27%（單一統計量）")
    pooled = {"orig": [], "detrend": [], "log1p": [], "step": []}
    for tool, m in sorted(mats.items()):
        X_all = m["X"]
        n_ev, p = X_all.shape
        n_base = max(3, int(round(n_ev * args.baseline_frac)))
        Xb_raw = X_all[:n_base]
        # 完整性：所有變數都非缺失的完整向量
        complete = ~np.isnan(Xb_raw).any(axis=1)
        n_complete = int(complete.sum())
        col_missing = [float(np.isnan(Xb_raw[:, j]).mean()) for j in range(p)]
        # 常數欄位（例如 missing_rate 全 0）必須排除並明列
        stds = np.nanstd(Xb_raw, axis=0)
        drop = [m["vars"][j] for j in range(p) if not np.isfinite(stds[j]) or stds[j] == 0]
        keep = [j for j in range(p) if m["vars"][j] not in drop]
        Z = Xb_raw[np.ix_(complete, keep)]
        info = {"n_events": int(n_ev), "p": int(p), "n_base": int(n_base),
                "n_complete_baseline": n_complete,
                "complete_rate": n_complete / max(n_base, 1),
                "dropped_constant_vars": drop,
                "max_col_missing_rate": max(col_missing) if col_missing else 0.0,
                "p_used": int(Z.shape[1])}
        print(f"\n  ■ {tool}：事件 {n_ev:,}｜變數 {p}（實際使用 {Z.shape[1]}）"
              f"｜基準期事件 {n_base:,}｜其中完整向量 {n_complete:,}"
              f"（{100 * info['complete_rate']:.1f}%）")
        if drop:
            print(f"    [排除] 基準期為常數的變數 {len(drop)} 個：{drop}")
        if Z.shape[0] > Z.shape[1] + 5 and Z.shape[1] >= 2:
            # 標準化（用基準期自身的平均/標準差 → Phase I）
            mu, sd = Z.mean(axis=0), Z.std(axis=0, ddof=1)
            Zn = (Z - mu) / np.where(sd == 0, 1.0, sd)
            r_orig = report_block(Zn, "原始（未去趨勢）", q)
            Zd = detrend(Zn)
            r_det = report_block(Zd, "已去趨勢", q)
            # 對數變體：對「基準期全為正值」的變數取 log1p 再標準化
            # 「非負且有變異」才取對數（slope / mean_val 可以是負值 → 不能取 log）
            pos_cols = [j for j in range(Z.shape[1])
                        if np.all(Z[:, j] >= 0) and np.ptp(Z[:, j]) > 0]
            Zl = log_transform(Z, pos_cols)
            mu_l, sd_l = Zl.mean(axis=0), Zl.std(axis=0, ddof=1)
            Zln = (Zl - mu_l) / np.where(sd_l == 0, 1.0, sd_l)
            r_log = report_block(Zln, "對數轉換（正值變數 log1p）", q)
            steps_base = [m["steps"][i] for i in np.nonzero(complete)[0]]
            r_step = report_block(centre_by_step(Zn, steps_base),
                                  "去步驟均值（per-step centering）", q)
            sv = step_variance_share(Z, steps_base)
            info["orig"] = r_orig
            info["detrend"] = r_det
            info["log1p"] = r_log
            info["per_step"] = r_step
            info["step_variance_share"] = sv
            info["n_log_transformed"] = len(pos_cols)
            pooled["orig"].append(r_orig)
            pooled["detrend"].append(r_det)
            pooled["log1p"].append(r_log)
            pooled["step"].append(r_step)
            print(f"    [轉換] 基準期全為正值的變數 {len(pos_cols)}/{Z.shape[1]} 個 → 取 log1p")
            print(f"    [步驟] 變異被 recipe_step 解釋：平均 {100 * sv['mean_r2']:.1f}%、"
                  f"中位數 {100 * sv['median_r2']:.1f}%（{sv['n_high']}/{sv['n_vars']} 個變數 >50%）")
            for tag, r in (("原始", r_orig), ("去趨勢", r_det), ("對數", r_log), ("去步驟均值", r_step)):
                s = r["pca"]
                print(f"    [{tag}] 主成分：1→{100 * s['top1']:.1f}%、"
                      f"2→{100 * s['top2']:.1f}%、3→{100 * s['top3']:.1f}%｜"
                      f"90% 需 {s['k90']} 維／99% 需 {s['k99']} 維｜條件數 {s['cond']:.1f}")
                _ex = r.get("d2_theory_exact_beta")
                _chk = r.get("simulation_vs_exact_rel_diff")
                print(f"            馬氏距離 q99.73：實測 {r['d2_quantiles'][f'q{q}']:.1f}"
                      f" vs 理論值 {r['d2_theory_simulated']:.1f}"
                      f"（模擬；精確 Beta 值 "
                      f"{'—' if _ex is None else f'{_ex:.1f}'}）"
                      f"（比 {r['empirical_over_theoretical']:.2f} 倍）｜"
                      f"理論線下的實際誤報率 {100 * r['baseline_alarm_rate_at_theory']:.2f}%")
                if _chk is not None:
                    print(f"            [自我檢查] 模擬 vs 精確公式相對差 "
                          f"{100 * _chk:.1f}%"
                          f"{'（⚠ 模擬或公式有問題）' if _chk > 0.10 else '（一致 ✓）'}")
                print(f"            常態性：多變量峰度 z={r['mardia_kurtosis']['z']:+.1f}"
                      f"、偏度 z={r['mardia_skewness']['z']:+.1f}（|z|>3 → 明顯非常態）")
        else:
            print(f"    [跳過] 完整樣本 {Z.shape[0]} 不足以估計 {Z.shape[1]}×"
                  f"{Z.shape[1]} 共變異數矩陣（需要至少 2～3 倍）")
        report["tools_out"][tool] = info

    def _agg(rs, key):
        vals = [r[key] for r in rs if r.get(key)]
        return float(np.mean(vals)) if vals else None

    def _agg_ev(rs, key):
        vals = [r["pca"][key] for r in rs if "pca" in r]
        return float(np.mean(vals)) if vals else None

    if pooled["orig"]:
        print("\n[彙總] 全部機台平均")
        for tag, rs in (("原始", pooled["orig"]), ("去趨勢", pooled["detrend"]),
                        ("對數", pooled["log1p"]), ("去步驟均值", pooled["step"])):
            print(f"  {tag}：主成分 1→{100 * _agg_ev(rs, 'top1'):.1f}%、"
                  f"90% 需 {_agg_ev(rs, 'k90'):.1f} 維｜"
                  f"條件數 {_agg_ev(rs, 'cond'):.1f}｜"
                  f"q99.73 實測/理論 = {_agg(rs, 'empirical_over_theoretical'):.2f} 倍｜"
                  f"峰度 z={np.mean([r['mardia_kurtosis']['z'] for r in rs]):+.1f}")
        report["summary"] = {
            "orig_top1": _agg_ev(pooled["orig"], "top1"),
            "detrend_top1": _agg_ev(pooled["detrend"], "top1"),
            "orig_ratio": _agg(pooled["orig"], "empirical_over_theoretical"),
            "detrend_ratio": _agg(pooled["detrend"], "empirical_over_theoretical"),
            "orig_kurtosis_z": float(np.mean([r["mardia_kurtosis"]["z"] for r in pooled["orig"]])),
            "detrend_kurtosis_z": float(np.mean([r["mardia_kurtosis"]["z"] for r in pooled["detrend"]])),
            "log1p_top1": _agg_ev(pooled["log1p"], "top1"),
            "log1p_ratio": _agg(pooled["log1p"], "empirical_over_theoretical"),
            "log1p_kurtosis_z": float(np.mean([r["mardia_kurtosis"]["z"] for r in pooled["log1p"]])),
            "per_step_top1": _agg_ev(pooled["step"], "top1"),
            "per_step_ratio": _agg(pooled["step"], "empirical_over_theoretical"),
            "per_step_kurtosis_z": float(np.mean([r["mardia_kurtosis"]["z"] for r in pooled["step"]]))}

    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  報告：{out}")
    print("  判讀：① 完整向量比例低 → 要先處理缺失（或缺值填補）")
    print("        ② 條件數過大／主成分壓縮後 <=p → 共線性高，正是 MSPC 的利基")
    print("        ③ 實測/理論 >1.5 倍或 |峰度 z|>3 → 管制線一律用【經驗分位數】，並考慮穩健共變異數")
    print("        ④ 三種變體中「實測/理論」倍數最低者，才是 M5-2-1 該用的前處理（以實測為準）")
    print("        ⑤ 條件數極大（>1e6）→ 不可用全部維度的 S⁻¹，要用 PCA 前 k 個主成分的 T²")
    print("        ⑥ 【最重要】若「去步驟均值」的倍數遠低於原始 → 主成分主要是步驟混合造成的，"
          "M5-2-1 必須逐 recipe_step 建模")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
