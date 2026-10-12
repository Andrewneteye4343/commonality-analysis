"""M4 統計引擎的單元測試（`analysis/glm.py`）。

測試策略：**能用解析解驗的，就不要只驗「跑得動」**。
- logistic regression：單一二元共變數時，exp(β) 必須等於 2×2 勝算比（與 M3 的
  `commonality_basic.odds_ratio` 交叉驗證）→ 這是最強的內部一致性檢查
- 截距模型：β 必須等於 logit(p̄)（封閉解）
- 收縮：ridge 越大 |β| 必須單調變小；Beta-binomial 必須能把模擬資料的真參數估回來
- 群聚穩健標準誤：群內相關時必須大於 naive 標準誤
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glm import (  # noqa: E402
    bh_qvalues, beta_binomial_fit, cluster_robust_se, invert, logistic_fit,
    matmul, shrink_rates, shrinkage_weight, solve_linear, spearman, topk_overlap,
    _average_ranks, _log_beta_binomial_pmf, _normal_cdf, _z_quantile,
    contrast_p, hazard_sum_fit, hazard_sum_grad_hess, _hazard_negll,
)
from commonality_basic import odds_ratio  # noqa: E402  與 M3 交叉驗證用


# ── 線性代數 ────────────────────────────────────────────────
def test_solve_linear_known_solution():
    A = [[2.0, 1.0], [1.0, 3.0]]
    b = [3.0, 5.0]
    x = solve_linear(A, b)
    assert x == pytest.approx([0.8, 1.4], abs=1e-12)
    assert [A[0][0] * x[0] + A[0][1] * x[1], A[1][0] * x[0] + A[1][1] * x[1]] == pytest.approx(b)


def test_invert_times_original_is_identity():
    A = [[4.0, 7.0, 2.0], [3.0, 6.0, 1.0], [2.0, 5.0, 3.0]]
    I = matmul(A, invert(A))
    for i in range(3):
        for j in range(3):
            assert I[i][j] == pytest.approx(1.0 if i == j else 0.0, abs=1e-9)


def test_solve_linear_rejects_singular():
    with pytest.raises(ValueError):
        solve_linear([[1.0, 2.0], [2.0, 4.0]], [1.0, 2.0])


# ── 常態分布工具 ────────────────────────────────────────────
def test_normal_cdf_and_quantile_roundtrip():
    for p in (0.001, 0.025, 0.5, 0.975, 0.999):
        assert _normal_cdf(_z_quantile(p)) == pytest.approx(p, abs=1e-6)
    assert _z_quantile(0.975) == pytest.approx(1.959964, abs=1e-5)


# ── logistic regression ────────────────────────────────────
def test_intercept_only_equals_closed_form():
    y = [1] * 30 + [0] * 70
    X = [[1.0] for _ in y]
    fit = logistic_fit(X, y, colnames=["intercept"])
    assert fit.beta[0] == pytest.approx(math.log(0.3 / 0.7), abs=1e-8)
    assert fit.converged


def test_single_binary_covariate_matches_2x2_odds_ratio():
    """核心一致性檢查：logistic 的 exp(β) == 2×2 勝算比。"""
    for (k1, n1, k2, n2) in [(10, 100, 20, 100), (3, 40, 9, 40), (25, 200, 40, 200), (1, 50, 8, 50)]:
        y = [1] * k1 + [0] * (n1 - k1) + [1] * k2 + [0] * (n2 - k2)
        X = [[1.0, 0.0]] * n1 + [[1.0, 1.0]] * n2
        fit = logistic_fit(X, y, colnames=["intercept", "group"])
        # M3 的 odds_ratio(a,b,c,d) = (a·d)/(b·c) = 「群組1 相對 群組2」；
        # logistic 的 exp(β) = 「群組2 相對 基準（群組1）」→ 兩者互為倒數，故取 1/OR 比較
        or_m3 = odds_ratio(k1, n1 - k1, k2, n2 - k2)[0]
        assert fit.odds_ratio(1) == pytest.approx(1 / or_m3, rel=1e-6), (k1, n1, k2, n2)
        # 直接用定義再驗一次（不依賴 M3 的函式），確保方向正確
        expected_direct = (k2 / (n2 - k2)) / (k1 / (n1 - k1))
        assert fit.odds_ratio(1) == pytest.approx(expected_direct, rel=1e-6)
        assert fit.converged


def test_recovers_simulated_coefficients():
    rng = random.Random(20261010)
    true = [0.4, 1.1, -0.6]
    X, y = [], []
    for _ in range(4000):
        row = [1.0, rng.gauss(0, 1), rng.gauss(0, 1)]
        z = sum(a * b for a, b in zip(true, row))
        X.append(row)
        y.append(1 if rng.random() < 1 / (1 + math.exp(-z)) else 0)
    fit = logistic_fit(X, y, colnames=["intercept", "a", "b"])
    for i in range(3):
        assert fit.beta[i] == pytest.approx(true[i], abs=0.12)
    # 係數的 95% 信賴區間應覆蓋真值
    for i in range(3):
        lo, hi = fit.or_ci(i, use_cluster=False)
        assert math.exp(true[i]) == pytest.approx(math.exp(true[i]))
        assert lo < math.exp(true[i]) < hi, i


def test_ridge_shrinks_monotonically():
    y = [1] * 20 + [0] * 80
    X = [[1.0, 1.0]] * 20 + [[1.0, 0.0]] * 80
    mags = []
    for lam in (0.0, 0.5, 2.0, 10.0, 100.0):
        fit = logistic_fit(X, y, ridge=lam, colnames=["intercept", "x"])
        mags.append(abs(fit.beta[1]))
    assert mags == sorted(mags, reverse=True), mags
    assert mags[0] > mags[-1]


def test_ridge_handles_perfect_separation():
    """零故障機台的專屬批次（k = 0）→ MLE 不存在，ridge 必須給出有限解。"""
    y = [1] * 60 + [0] * 40 + [0] * 30      # 第二組全為 0 → 完全分離
    X = [[1.0, 0.0]] * 100 + [[1.0, 1.0]] * 30
    unpenalized = logistic_fit(X, y, ridge=0.0, colnames=["intercept", "x"], max_iter=60)
    penalized = logistic_fit(X, y, ridge=1.0, colnames=["intercept", "x"])
    # 未懲罰的估計會往負無窮跑（或直接標記分離）；懲罰後必須是有限且明顯較小的係數
    assert abs(penalized.beta[1]) < abs(unpenalized.beta[1]) + 1e-9
    assert abs(penalized.beta[1]) < 5
    assert penalized.separation_warning == ""
    assert penalized.odds_ratio(1) < 1.0    # 方向正確（保護性）


def test_cluster_robust_se_matches_analytic_formula():
    """截距模型的群聚穩健標準誤有封閉解，必須逐位吻合。

    推導：X = 1、w = p̄(1−p̄) → bread = (XᵀWX)⁻¹ = 1/(n·w)；
    meat = Σ_g (Σ_{i∈g} r_i)²；V = bread·meat·bread → SE = √meat/(n·w)，
    再乘小樣本修正 √(g/(g−1))。
    """
    rng = random.Random(3)
    X, y, clusters = [], [], []
    for c in range(25):
        u = rng.gauss(0, 1.0)              # 群聚層級隨機效應
        for _ in range(8):
            X.append([1.0])
            y.append(1 if rng.random() < 1 / (1 + math.exp(-(-0.4 + u))) else 0)
            clusters.append(c)
    fit = logistic_fit(X, y, ridge=0.0, colnames=["intercept"])
    n = len(y)
    pbar = sum(y) / n
    w = pbar * (1 - pbar)
    sums: dict = {}
    for i, g in enumerate(clusters):
        sums[g] = sums.get(g, 0.0) + (y[i] - pbar)
    meat = sum(sv * sv for sv in sums.values())
    g_n = len(sums)
    expected = math.sqrt(meat) / (n * w) * math.sqrt(g_n / (g_n - 1))
    cse, info = cluster_robust_se(X, y, fit.beta, clusters, ridge=0.0, colnames=["intercept"])
    assert info["n_clusters"] == g_n
    assert cse[0] == pytest.approx(expected, rel=1e-12)


def _simulate_cluster_design(seed: int, cluster_level_treatment: bool, n_clusters: int = 25,
                             size: int = 10) -> tuple[list, list, list]:
    """兩種群聚設計：
    - cluster_level_treatment=True：整個 recipe 用同一台機台（共變數在群聚層級）
    - False：同一 recipe 內不同批次用不同機台（共變數在群內變動）
    """
    rng = random.Random(seed)
    X, y, clusters = [], [], []
    for c in range(n_clusters):
        u = rng.gauss(0, 1.4)                       # 群聚層級隨機效應
        cluster_treat = 1.0 if rng.random() < 0.5 else 0.0
        for _ in range(size):
            treat = cluster_treat if cluster_level_treatment else (1.0 if rng.random() < 0.5 else 0.0)
            p = 1 / (1 + math.exp(-(-0.3 + 0.5 * treat + u)))
            X.append([1.0, treat])
            y.append(1 if rng.random() < p else 0)
            clusters.append(f"r{c}")
    return X, y, clusters


def _mean_se_ratio(cluster_level_treatment: bool, reps: int = 40) -> tuple[float, int]:
    ratios, wins = [], 0
    for rep in range(reps):
        X, y, clusters = _simulate_cluster_design(2000 + rep, cluster_level_treatment)
        fit = logistic_fit(X, y, ridge=1.0, colnames=["intercept", "treat"])
        cse, _ = cluster_robust_se(X, y, fit.beta, clusters, ridge=1.0,
                                   colnames=["intercept", "treat"])
        r = cse[1] / fit.se[1]
        ratios.append(r)
        wins += 1 if r > 1 else 0
    return sum(ratios) / len(ratios), wins


def test_cluster_robust_se_inflates_when_covariate_is_cluster_level():
    """共變數在群聚層級（整個 recipe 都用同一台機台）→ naive 標準誤低估、群聚穩健標準誤膨脹。

    實測（25 群聚 × 10 筆、ICC 中等、ridge=1）：平均膨脹 **≈ 1.7 倍**、40/40 次都膨脹。
    注意膨脹倍數不等於 √(群聚大小)=3.2：設計效應為 √(1+(m−1)ρ)，
    取決於群內相關 ρ 與懲罰強度，不是固定值。
    """
    mean_ratio, wins = _mean_se_ratio(cluster_level_treatment=True)
    assert mean_ratio > 1.4, f"平均膨脹倍數只有 {mean_ratio:.3f}"
    assert wins == 40, f"只有 {wins}/40 次大於 naive"


def test_cluster_robust_se_barely_changes_when_covariate_varies_within_cluster():
    """共變數在群內變動 → 群聚雜訊在組內比較中抵消，膨脹很小（實測 ≈ 0.94–1.1）。

    這個測試記錄了一個容易誤解的地方：**群聚穩健標準誤不會無條件變大**；
    它修正的是「共變數與群聚結構相關」造成的低估。本專案的機台隸屬
    （同一 recipe 內不同批次可能用不同機台）正屬於這一類，因此
    真正的偽重複問題要用機台×批次的層級設計處理，而不是盲目套 cluster SE。
    """
    mean_ratio, _wins = _mean_se_ratio(cluster_level_treatment=False)
    assert 0.7 < mean_ratio < 1.6, f"平均倍數 {mean_ratio:.3f} 落在此情境的預期範圍外"


# ── Beta-binomial 收縮 ─────────────────────────────────────
def test_beta_binomial_recovers_simulated_parameters():
    rng = random.Random(11)
    true_a, true_b = 3.0, 12.0                 # μ = 0.2、θ = 15
    ks, ns = [], []
    for _ in range(300):
        n = rng.choice([20, 50, 100])
        p = rng.betavariate(true_a, true_b)
        ks.append(sum(1 for _ in range(n) if rng.random() < p))
        ns.append(n)
    fit = beta_binomial_fit(ks, ns)
    assert fit["mu"] == pytest.approx(true_a / (true_a + true_b), abs=0.06)
    assert fit["theta"] == pytest.approx(true_a + true_b, rel=0.6)
    assert 0 < fit["overdispersion"] < 1


def test_shrinkage_moves_small_samples_more():
    """收縮強度必須隨 n 變大而減弱（小樣本機台被拉得更多）。"""
    a, b = 3.0, 12.0                        # 先驗平均 = 0.2
    prior_mean = a / (a + b)
    small = shrink_rates([1], [3], a, b)[0]      # 1/3 = 0.333
    large = shrink_rates([334], [1000], a, b)[0]  # 同樣 0.334 但 n 大
    assert abs(small - prior_mean) < abs(large - prior_mean)
    assert shrinkage_weight(3, a, b) < shrinkage_weight(1000, a, b)
    assert small < large                          # 小樣本被拉低


def test_shrink_rates_zero_count_not_zero():
    """k = 0 的群組收縮後不是 0（避免『零故障機台』的排名被過度極端化）。"""
    r = shrink_rates([0], [927], 1.0, 20.0)[0]
    assert 0 < r < 0.01


def test_beta_binomial_log_pmf_matches_manual_integration():
    """驗證 beta-binomial PMF：與數值積分（Beta 先驗 × 二項概似）一致。"""
    k, n, a, b = 3, 10, 2.0, 5.0
    steps = 200000
    lo, hi = 0.0, 1.0
    h = (hi - lo) / steps
    total = 0.0
    for i in range(steps + 1):
        p = lo + i * h
        if p <= 0 or p >= 1:
            continue
        w = 1.0 if i in (0, steps) else (4.0 if i % 2 else 2.0)   # Simpson
        log_binom = math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
        lik = math.exp(log_binom + k * math.log(p) + (n - k) * math.log(1 - p))
        prior = math.exp((a - 1) * math.log(p) + (b - 1) * math.log(1 - p)
                         + math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b))
        total += w * lik * prior
    total *= h / 3
    assert math.exp(_log_beta_binomial_pmf(k, n, a, b)) == pytest.approx(total, rel=2e-3)


# ── cloglog（「至少一台造成」的機制） ──────────────────────
def test_cloglog_single_covariate_equals_hazard_ratio():
    """cloglog 模型：exp(β) = h₁/h₀，其中 h = −log(1−p)（各組的風險率）。

    推導：cloglog(p) = log(−log(1−p)) = β → exp(β) = −log(1−p)（風險率）。
    兩組時 exp(β_covariate) = log(1−p₁)/log(1−p₀)（精確解，非近似）。
    """
    for (k0, n0, k1, n1) in [(20, 100, 60, 100), (5, 200, 20, 150), (1, 50, 12, 60)]:
        p0, p1 = k0 / n0, k1 / n1
        y = [1] * k0 + [0] * (n0 - k0) + [1] * k1 + [0] * (n1 - k1)
        X = [[1.0, 0.0]] * n0 + [[1.0, 1.0]] * n1
        fit = logistic_fit(X, y, colnames=["intercept", "group"], link="cloglog")
        expected = math.log(1 - p1) / math.log(1 - p0)
        assert fit.odds_ratio(1) == pytest.approx(expected, rel=1e-6)
        assert fit.converged


def test_cloglog_is_closer_to_probability_ratio_than_logit_when_events_are_common():
    """事件常見時，cloglog 的 exp(β) 較接近機率比；logit 給的勝算比會明顯更大。

    罕見事件（<5%）時兩者都 ≈ 機率比（實測 3.03 vs 3.06、機率比 3.00）；
    事件常見時差距出現：p₀=0.2、p₁=0.5 → 機率比 2.5、cloglog 3.11、logit 4.00。
    """
    # 常見事件
    k0, n0, k1, n1 = 200, 1000, 500, 1000
    y = [1] * k0 + [0] * (n0 - k0) + [1] * k1 + [0] * (n1 - k1)
    X = [[1.0, 0.0]] * n0 + [[1.0, 1.0]] * n1
    cc = logistic_fit(X, y, colnames=["i", "g"], link="cloglog")
    lg = logistic_fit(X, y, colnames=["i", "g"], link="logit")
    prob_ratio = (k1 / n1) / (k0 / n0)
    assert cc.odds_ratio(1) == pytest.approx(3.1063, rel=1e-3)     # 解析值 = −log(0.5)/−log(0.8)
    assert lg.odds_ratio(1) == pytest.approx(4.0, rel=1e-6)
    assert abs(cc.odds_ratio(1) - prob_ratio) < abs(lg.odds_ratio(1) - prob_ratio)
    # 罕見事件：兩者都接近機率比
    y2 = [1] * 10 + [0] * 990 + [1] * 30 + [0] * 970
    X2 = [[1.0, 0.0]] * 1000 + [[1.0, 1.0]] * 1000
    cc2 = logistic_fit(X2, y2, colnames=["i", "g"], link="cloglog")
    lg2 = logistic_fit(X2, y2, colnames=["i", "g"], link="logit")
    assert cc2.odds_ratio(1) == pytest.approx(3.0, rel=0.05)
    assert lg2.odds_ratio(1) == pytest.approx(3.0, rel=0.05)


def test_cloglog_rejects_unknown_link():
    with pytest.raises(ValueError):
        logistic_fit([[1.0]], [1], link="probit")


def test_cloglog_recovers_multiplicative_hazards():
    """線性 cloglog ＋指示變數＝**風險率相乘**（hazard ratio）機制：

        cloglog P(bad) = β₁·I(機台1) + β₂·I(機台2)   ⟺   λ = h₁^I₁ · h₂^I₂

    （若機制其實是「風險率相加」λ = h₁I₁ + h₂I₂，線性 cloglog 只是一個近似
    ——這正是本專案要小心的地方，見 run_logistic 的 logit/cloglog 對照）
    """
    rng = random.Random(2026)
    h = [0.3, 1.2]
    X, y = [], []
    while len(X) < 6000:
        on = [1.0 if rng.random() < 0.5 else 0.0 for _ in h]
        if not any(on):
            continue
        lam = math.exp(sum(math.log(hi) * oi for hi, oi in zip(h, on)))
        X.append(on)
        y.append(1 if rng.random() < 1 - math.exp(-lam) else 0)
    fit = logistic_fit(X, y, colnames=["a", "b"], link="cloglog")
    assert fit.converged
    assert math.exp(fit.beta[0]) == pytest.approx(h[0], rel=0.12)
    assert math.exp(fit.beta[1]) == pytest.approx(h[1], rel=0.12)


def test_cloglog_with_additive_hazards_is_only_an_approximation():
    """記錄一個重要限制：機制是「相加」時，線性 cloglog 只能近似（會高估風險率）。

    真機制 λ = 0.3·I(a) + 1.2·I(b)（本專案壞批次的定義正是「任一機台」＝相加型），
    而線性 cloglog 假設相乘 → 估計值會偏離真值。實測 ≈ 1.5–1.9 倍。
    因此 run_logistic 必須**同時**報告模型無關的「專屬批次比率」作為下界。
    """
    rng = random.Random(7)
    h = [0.3, 1.2]
    X, y = [], []
    while len(X) < 4000:
        on = [1.0 if rng.random() < 0.5 else 0.0 for _ in h]
        if not any(on):
            continue
        lam = sum(hi * oi for hi, oi in zip(h, on))          # 相加型（真實機制）
        X.append(on)
        y.append(1 if rng.random() < 1 - math.exp(-lam) else 0)
    fit = logistic_fit(X, y, colnames=["a", "b"], link="cloglog")
    est = [math.exp(fit.beta[0]), math.exp(fit.beta[1])]
    assert est[0] > h[0] * 1.2, est       # 明確高估（不是僥倖吻合）
    assert est[0] / h[0] < 3.0            # 但不至於離譜
    # 排序仍正確（低風險機台仍低於高風險機台）
    assert fit.beta[0] < fit.beta[1]


# ── 風險率相加模型 ─────────────────────────────────────────
def test_hazard_sum_recovers_hazards_and_is_consistent_with_linear_models():
    """機制是「相加」時，hazard 模型必須比 logit/cloglog 更接近真值。"""
    rng = random.Random(2026)
    h = [0.05, 0.4]
    X, y = [], []
    while len(X) < 5000:
        on = [1.0 if rng.random() < 0.5 else 0.0 for _ in h]
        if not any(on):
            continue
        lam = sum(hi * oi for hi, oi in zip(h, on))
        X.append(on)
        y.append(1 if rng.random() < 1 - math.exp(-lam) else 0)
    from glm import hazard_sum_fit
    fit = hazard_sum_fit(X, y, colnames=["a", "b"])
    assert fit["converged"]
    for i in range(2):
        assert fit["hazard"][i] == pytest.approx(h[i], rel=0.25)
    logit = logistic_fit(X, y, colnames=["a", "b"], link="logit")
    clog = logistic_fit(X, y, colnames=["a", "b"], link="cloglog")
    err_h = abs(fit["hazard"][1] - h[1])
    assert err_h < abs(logit.odds_ratio(1) - h[1]) / 2
    assert err_h < abs(clog.odds_ratio(1) - h[1]) / 2


def test_hazard_sum_lrt_flags_only_real_risk():
    """邊界修正 LRT：真的沒有風險的群組不得被判定顯著（零雜訊情境）。

    這是真實資料 `04M01` 的對應檢定（其專屬批次 0/927）：
    Wald 檢定在此退化（測的是 h = 1），必須用 LRT。
    """
    from glm import hazard_sum_fit, hazard_sum_lrt
    rng = random.Random(11)
    h_true = [0.0, 0.5]                     # 第一台完全無風險
    X, y = [], []
    while len(X) < 4000:
        on = [1.0 if rng.random() < 0.5 else 0.0 for _ in h_true]
        if not any(on):
            continue
        lam = sum(hi * oi for hi, oi in zip(h_true, on))
        X.append(on)
        y.append(1 if rng.random() < 1 - math.exp(-lam) else 0)
    fit = hazard_sum_fit(X, y, colnames=["zero", "real"])
    _lr0, p_zero = hazard_sum_lrt(X, y, fit["theta"], 0, ridge=fit["ridge"])
    _lr1, p_real = hazard_sum_lrt(X, y, fit["theta"], 1, ridge=fit["ridge"])
    assert p_zero > 0.05, p_zero            # 無風險者不顯著
    assert p_real < 1e-6, p_real            # 有風險者極顯著
    assert fit["hazard"][1] == pytest.approx(h_true[1], rel=0.2)


def test_hazard_sum_cluster_se_positive_and_ordered():
    from glm import hazard_sum_cluster_se, hazard_sum_fit
    rng = random.Random(3)
    h_true = [0.05, 0.4]
    X, y, cl = [], [], []
    while len(X) < 2000:
        on = [1.0 if rng.random() < 0.5 else 0.0 for _ in h_true]
        if not any(on):
            continue
        lam = sum(hi * oi for hi, oi in zip(h_true, on))
        X.append(on)
        y.append(1 if rng.random() < 1 - math.exp(-lam) else 0)
        cl.append(f"r{len(X) % 20}")
    fit = hazard_sum_fit(X, y, colnames=["a", "b"])
    cse, info = hazard_sum_cluster_se(X, y, fit["theta"], cl, ridge=fit["ridge"])
    assert info["n_clusters"] == 20
    assert all(v > 0 for v in cse)
    assert all(math.isfinite(v) for v in cse)


# ── 排名相關 ───────────────────────────────────────────────
def test_average_ranks_with_ties():
    assert _average_ranks([10, 20, 20, 30]) == [1.0, 2.5, 2.5, 4.0]


def test_spearman_perfect_and_reversed():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
    assert spearman([1, 2, 3, 4], [7, 7, 7, 7]) == 0.0


def test_spearman_matches_hand_computation():
    r = spearman([1, 2, 3, 4, 5], [2, 1, 4, 3, 5])
    assert r == pytest.approx(0.8, abs=1e-12)


def test_topk_overlap():
    assert topk_overlap(["a", "b", "c"], ["b", "a", "d"], 2) == pytest.approx(1.0)
    assert topk_overlap(["a", "b", "c"], ["d", "e", "f"], 3) == 0.0
    assert topk_overlap(["a", "b", "c"], ["a", "e", "f"], 3) == pytest.approx(1 / 5)


# ── BH-FDR（與 M3 實作一致） ───────────────────────────────
def test_bh_qvalues_known_example():
    p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216]
    q = bh_qvalues(p)
    assert q[0] == pytest.approx(0.01, abs=1e-12)
    assert q[9] == pytest.approx(0.216, abs=1e-12)
    assert all(q[i] >= p[i] - 1e-15 for i in range(len(p)))
    assert [round(v, 3) for v in q] == sorted(round(v, 3) for v in q)


def test_bh_qvalues_matches_m3_implementation():
    from multiple_testing import benjamini_hochberg  # M3 的實作
    for p in ([0.0002, 0.004, 0.03, 0.31, 0.99], [0.9, 0.5, 0.2, 0.05, 0.01]):
        q_m3 = benjamini_hochberg(p)
        assert bh_qvalues(p) == pytest.approx([q_m3[i] for i in range(len(p))], abs=1e-12)


# ── 線性對比檢定（無截距模型的正確檢定） ─────────────────────
def test_contrast_p_analytic():
    """c = (1, −1)、V = diag(0.04, 0.01)、β = (1.0, 0.5)
    → 對比 = 0.5、SE = sqrt(0.05)、z = 2.2360679…、p = 2(1−Φ(z)) ≈ 0.025347"""
    est, p = contrast_p([1.0, -1.0], [1.0, 0.5], [[0.04, 0.0], [0.0, 0.01]])
    assert est == pytest.approx(0.5, abs=1e-12)
    assert p == pytest.approx(0.0253472, abs=1e-6)


def test_contrast_p_zero_and_huge_contrast():
    est0, p0 = contrast_p([1.0, -1.0], [0.7, 0.7], [[0.04, 0.0], [0.0, 0.04]])
    assert est0 == pytest.approx(0.0, abs=1e-15) and p0 > 0.99
    _est, p1 = contrast_p([1.0, 0.0], [5.0, 0.0], [[0.01, 0.0], [0.0, 0.01]])
    assert p1 < 1e-12


def test_contrast_p_handles_zero_variance():
    est, p = contrast_p([1.0, -1.0], [1.0, 0.0], [[0.0, 0.0], [0.0, 0.0]])
    assert p == 1.0 and est == pytest.approx(1.0)


# ── hazard 的解析梯度／Hessian（效能修正的正確性錨點） ────────
def test_hazard_analytic_gradient_matches_numeric_gradient():
    """梯度用中心差分（可靠）互驗：解析式必須與數值梯度一致。"""
    rng = random.Random(3)
    X, y = [], []
    for _ in range(400):
        row = [1.0 if rng.random() < 0.4 else 0.0 for _ in range(5)]
        if not any(row):
            continue
        X.append(row)
        y.append(1 if rng.random() < 0.12 else 0)
    ridge = 0.01
    for theta in ([0.0] * 5, [-2.0, -1.5, -3.0, -2.5, -1.0], [-0.5, -0.8, -0.6, -0.7, -0.9]):
        g_ana, _H = hazard_sum_grad_hess(theta, X, y, ridge)
        h = 1e-6
        for a in range(5):
            tp = list(theta)
            tm = list(theta)
            tp[a] += h
            tm[a] -= h
            g_num = (_hazard_negll(tp, X, y, ridge) - _hazard_negll(tm, X, y, ridge)) / (2 * h)
            assert abs(g_ana[a] - g_num) < 1e-3 * max(1.0, abs(g_num)), (theta, a, g_ana[a], g_num)


def test_hazard_analytic_hessian_matches_differencing_the_analytic_gradient():
    """Hessian 用「解析梯度的中心差分」互驗。

    ⚠ 不要拿 `_numeric_grad_hess` 當標準——它用固定步長 h = 1e-4，對風險率模型的
    二階導數截斷誤差很大（實測在 θ = (−2, −1.5, …) 處與解析式差到 1e-3 以上）。
    **數值 Hessian 不只慢，本身也不夠準**：這正是改用解析式的第二個理由。
    """
    rng = random.Random(7)
    X, y = [], []
    for _ in range(300):
        row = [1.0 if rng.random() < 0.4 else 0.0 for _ in range(4)]
        if not any(row):
            continue
        X.append(row)
        y.append(1 if rng.random() < 0.15 else 0)
    ridge = 0.01
    for theta in ([0.0] * 4, [-1.2, -0.8, -1.5, -1.0]):
        _g, H = hazard_sum_grad_hess(theta, X, y, ridge)
        t = 1e-5
        for a in range(4):
            tp = list(theta)
            tm = list(theta)
            tp[a] += t
            tm[a] -= t
            gp, _ = hazard_sum_grad_hess(tp, X, y, ridge)
            gm, _ = hazard_sum_grad_hess(tm, X, y, ridge)
            for b in range(4):
                num = (gp[b] - gm[b]) / (2 * t)
                assert abs(H[a][b] - num) < 1e-4 * max(1.0, abs(num)), (theta, a, b, H[a][b], num)
        # 對稱性
        for a in range(4):
            for b in range(4):
                assert abs(H[a][b] - H[b][a]) < 1e-12


def test_hazard_sum_fit_still_recovers_hazards_with_analytic_hessian():
    rng = random.Random(11)
    h_true = [0.03, 0.25]
    X, y = [], []
    while len(X) < 4000:
        row = [1.0 if rng.random() < 0.5 else 0.0 for _ in h_true]
        if not any(row):
            continue
        lam = sum(hi * xi for hi, xi in zip(h_true, row))
        X.append(row)
        y.append(1 if rng.random() < 1 - math.exp(-lam) else 0)
    fit = hazard_sum_fit(X, y)
    assert fit["converged"]
    for a in range(2):
        assert abs(fit["hazard"][a] - h_true[a]) < 0.25 * h_true[a]
