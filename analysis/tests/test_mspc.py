"""M5-2-1 MSPC 核心的單元測試。

原則：只放「已知答案」的檢查——
  * 貢獻分解必須**精確**加總成 T² / SPE（恆等式，不是近似）
  * 趨勢外推必須只用到基準期（監控期的殘差要能被基準期斜率解釋掉）
  * 穩健共變異數在離群值存在時必須比一般版本更小（機制驗證，不是看結果漂不漂亮）
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "analysis"))

import mspc  # noqa: E402


def _blob(n=800, p=6, seed=1):
    """低秩 + 雜訊的合成基準期（先標準化尺度不需一致，那是模型的工作）。"""
    rng = np.random.default_rng(seed)
    f = rng.standard_normal((n, 2))
    load = rng.standard_normal((2, p))
    return f @ load + 0.4 * rng.standard_normal((n, p))


def test_t2_contributions_sum_exactly_to_t2():
    """Σ_j c_j == T² 是恆等式（這次分解能不能用來找兇手，全靠這條）。"""
    rng = np.random.default_rng(2)
    X = _blob(600, 5)
    mu, S = X.mean(axis=0), np.cov(X, rowvar=False)
    ev, P = mspc.pca_fit(S)
    k = mspc.choose_k(ev, 0.90)
    Z = X - mu
    t2, spe = mspc.t2_spe(Z, np.zeros_like(mu), P, ev, k)
    for i in (0, 7, 123, 599):
        c = mspc.t2_contributions(Z[i], P, ev, k)
        assert abs(c.sum() - t2[i]) < 1e-8 * max(1.0, abs(t2[i]))


def test_spe_contributions_sum_exactly_to_spe():
    rng = np.random.default_rng(3)
    X = _blob(600, 5)
    mu, S = X.mean(axis=0), np.cov(X, rowvar=False)
    ev, P = mspc.pca_fit(S)
    k = mspc.choose_k(ev, 0.90)
    Z = X - mu
    t2, spe = mspc.t2_spe(Z, np.zeros_like(mu), P, ev, k)
    for i in (1, 42, 599):
        c = mspc.spe_contributions(Z[i], P, k)
        assert abs(c.sum() - spe[i]) < 1e-8 * max(1.0, abs(spe[i]))


def test_choose_k_finds_low_rank_structure():
    X = _blob(2000, 8, seed=4)
    mu, S = X.mean(axis=0), np.cov(X, rowvar=False)
    ev, _ = mspc.pca_fit(S)
    k = mspc.choose_k(ev, 0.90)
    assert 2 <= k <= 4                      # 真結構是 2 個因子


def test_choose_k_clamped_to_valid_range():
    ev = np.array([10.0, 1.0])
    assert mspc.choose_k(ev, 0.99) <= 1     # 上限 p-1（要留殘差空間）


def test_detrend_extrapolates_baseline_trend_only():
    """漂移資料：基準期估斜率 → 監控期殘差應趨近 0；用基準期平均置中則會系統性偏移。"""
    t = np.arange(0, 1200, dtype=float)
    y = 5.0 + 0.02 * t + np.random.default_rng(5).normal(0, 0.05, len(t))
    a, b = mspc.fit_trend(t[:400], y[:400])
    assert abs(b - 0.02) < 0.002                    # 斜率估得準
    resid = mspc.detrend(t, y, a, b)
    assert abs(np.mean(resid[800:])) < 0.15         # 外推段沒有系統性偏移
    naive = y - np.mean(y[:400])                    # 對照：只用基準期平均置中
    assert abs(np.mean(naive[800:])) > 3.0          # 這才是漂移造成的假警報來源


def test_robust_covariance_is_smaller_with_outliers():
    """5% 極端離群值：穩健版的共變異數行列式必須明顯較小（否則離群值在主導模型）。"""
    rng = np.random.default_rng(6)
    X = rng.standard_normal((500, 4)) * 0.5
    out = rng.standard_normal((25, 4)) * 25.0
    Y = np.vstack([X, out])
    Z = (Y - Y.mean(axis=0)) / Y.std(axis=0, ddof=1)
    mu_r, S_r, n_kept = mspc.robust_center_cov(Z)
    S_p = np.cov(Z, rowvar=False)
    assert n_kept < len(Z)
    assert np.linalg.det(S_r) < np.linalg.det(S_p) / 10


def test_empirical_limit_is_upper_quantile():
    v = np.arange(1, 10001, dtype=float)          # 1..10000
    lim = mspc.empirical_limit(v, alpha=0.01)
    assert 9900 <= lim <= 10000


def test_build_model_rejects_too_few_samples():
    """樣本數守門：不足就不建模（回 None），不可硬 fit 出一個看似正常的模型。"""
    t = np.arange(30, dtype=float)
    Y = np.random.default_rng(7).standard_normal((30, 8))
    assert mspc.build_model(t, Y, [f"v{i}" for i in range(8)]) is None


def test_missing_values_imputed_to_baseline_center():
    """缺失以基準期中心代入 → 與「該變數正好等於中心」的向量得分相同。"""
    t = np.arange(900, dtype=float)
    Y = _blob(900, 5, seed=8)
    m = mspc.build_model(t[:600], Y[:600], [f"v{i}" for i in range(5)])
    row = Y[700].copy()
    row[2] = np.nan
    t2a, spea = m.score(np.array([t[700]]), row.reshape(1, -1))
    alt = Y[700].copy()
    a, b = m.trend["v2"]
    # 缺失的語意＝「標準化後設為模型中心（x_j = 0）」。換算回原始尺度就是
    # 趨勢線上的值再加 mu[j]×scale[j]（穩健中心不一定剛好是 0）。
    alt[2] = a + b * t[700] + m.mu[2] * m.scale[2]
    t2b, speb = m.score(np.array([t[700]]), alt.reshape(1, -1))
    assert abs(t2a[0] - t2b[0]) < 1e-6
    assert abs(spea[0] - speb[0]) < 1e-6


def test_shift_detected_and_normal_not():
    """注入 3σ 持續偏移 → T²/SPE 明顯越界；正常資料大多是安靜的。"""
    rng = np.random.default_rng(9)
    Y = _blob(1200, 6, seed=9)
    t = np.arange(1200, dtype=float)
    m = mspc.build_model(t[:800], Y[:800], [f"v{i}" for i in range(6)])
    good = Y[800:1100]
    bad = Y[1100:1200].copy()
    bad[:, 0] += 3.0 * bad[:, 0].std()           # 只在一個變數上注入
    t2g, speg = m.score(t[800:1100], good)
    t2b, speb = m.score(t[1100:1200], bad)
    det_bad = np.mean((t2b > m.lim_t2) | (speb > m.lim_spe))
    det_good = np.mean((t2g > m.lim_t2) | (speg > m.lim_spe))
    assert det_bad > 0.8                          # 明顯偏移必須被抓到
    assert det_good < 0.05                        # 正常段不該到處響


def test_near_constant_variable_is_dropped_and_scores_stay_sane():
    """近乎常數的變數（σ ~ 1e-13）若不排除，標準化後任何微小偏差都會變成天文數字。

    真實踩到：5 感測器全量資料的 ACTUALSTEPDURATION（由 recipe 固定）造成超標倍數 10^32。
    """
    rng = np.random.default_rng(21)
    n = 600
    t = np.arange(n, dtype=float)
    X = rng.standard_normal((n, 3))
    X[:, 2] = 7.0 + rng.standard_normal(n) * 1e-13      # 近乎常數（σ < 相對門檻）
    m = mspc.build_model(t[:400], X[:400], ["a", "b", "near_const"])
    assert m is not None
    assert "near_const" in m.dropped_degenerate
    assert "near_const" not in m.vars
    # 監控期（含一個該變數的微小偏移）不得產生天文數字分數
    later = X[400:].copy()
    later[:, 2] += 1e-9
    t2, spe = m.score(t[400:], later)
    assert np.all(np.isfinite(t2)) and np.all(np.isfinite(spe))
    ratio = np.maximum(t2 / m.lim_t2, spe / m.lim_spe)
    assert ratio.max() < 1e6, ratio.max()


def test_model_is_none_when_all_but_one_variable_is_constant():
    t = np.arange(500, dtype=float)
    X = np.ones((500, 3)) + np.random.default_rng(3).standard_normal((500, 3)) * 1e-14
    assert mspc.build_model(t, X, ["a", "b", "c"]) is None


def test_extreme_value_is_capped_and_ranking_survives():
    """原始特徵值離譜時（模擬 slope = 差值/極短時距 爆掉）：
    分數必須有界、截斷次數必須被計數、貢獻分解仍精確加總、兇手仍是對的那個變數。
    """
    t = np.arange(900, dtype=float)
    X = _blob(900, 4, seed=31)
    m = mspc.build_model(t[:600], X[:600], [f"v{i}" for i in range(4)])
    row = X[700].copy()
    row[1] = 1e9 * max(1.0, abs(np.median(X[:600, 1])))      # 離譜值
    t2, spe = m.score(np.array([t[700]]), row.reshape(1, -1))
    assert m.n_capped == 1
    assert t2[0] < 1e6 and spe[0] < 1e6                      # 有界（未截斷時會是 10^18 級）
    c = m.contributions(float(t[700]), row)
    assert abs(c["t2"].sum() - c["t2_total"]) < 1e-6         # 恆等式仍成立
    assert int(np.argmax(np.abs(c["t2"]))) == 1              # 兇手仍是 v1
