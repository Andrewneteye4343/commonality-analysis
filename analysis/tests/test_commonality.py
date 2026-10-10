"""M3 統計核心的單元測試。

驗證策略（重要）：**用演算法完全不同的方法做獨立驗證**，不是拿自己的公式再算一次。
- 超幾何分布：用 `itertools` 暴力列舉所有可能的抽樣結果，統計 X 的分布 → 對照公式實作
- Fisher 雙尾：暴力列舉固定邊界（margins）下的所有 2×2 表格 → 對照實作
- 有 scipy 的環境（容器內）額外與 `scipy.stats` 對照；沒有 scipy 時該項自動跳過

執行：
    python -m pytest analysis/tests -q
"""
from __future__ import annotations

import itertools
import math
import random
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commonality_basic import (  # noqa: E402
    HAS_SCIPY, RankConfig, contingency_stats, fisher_exact_greater, fisher_exact_two_sided,
    hypergeom_logpmf, hypergeom_sf, odds_ratio, or_ci_woolf, rank_groups,
)
from multiple_testing import bonferroni, benjamini_hochberg, evaluate  # noqa: E402


# ─────────────────────────────────────────────────────────────
# ① 暴力列舉驗證超幾何分布
# ─────────────────────────────────────────────────────────────
def _brute_hypergeom_dist(N: int, K: int, n: int) -> Counter:
    """暴力列舉：N 個位置中 K 個是壞的，抽 n 個（無放回），統計抽到幾個壞的"""
    positions = list(range(N))
    bad = set(range(K))
    dist = Counter()
    for comb in itertools.combinations(positions, n):
        dist[sum(1 for p in comb if p in bad)] += 1
    return dist


@pytest.mark.parametrize("N,K,n", [(10, 4, 5), (12, 3, 4), (15, 7, 6), (9, 2, 3), (20, 5, 8)])
def test_hypergeom_pmf_matches_bruteforce(N, K, n):
    dist = _brute_hypergeom_dist(N, K, n)
    total = sum(dist.values())
    for k in range(0, n + 1):
        expected = dist.get(k, 0) / total
        got = math.exp(hypergeom_logpmf(k, N, K, n))
        assert got == pytest.approx(expected, rel=1e-12, abs=1e-15), (N, K, n, k)


@pytest.mark.parametrize("N,K,n", [(10, 4, 5), (12, 3, 4), (15, 7, 6), (20, 5, 8)])
def test_hypergeom_sf_matches_bruteforce(N, K, n):
    dist = _brute_hypergeom_dist(N, K, n)
    total = sum(dist.values())
    for k in range(-1, n + 2):
        expected = sum(v for kk, v in dist.items() if kk >= k) / total
        assert hypergeom_sf(k, N, K, n) == pytest.approx(expected, rel=1e-12, abs=1e-15), (N, K, n, k)


def test_fisher_greater_is_hypergeom():
    """數學恆等：單尾 Fisher == 超幾何檢定（TCA 用的檢定）"""
    for (a, b, c, d) in [(8, 12, 3, 30), (1, 20, 5, 50), (0, 10, 4, 40), (15, 5, 10, 40)]:
        N, K, n = a + b + c + d, a + c, a + b
        assert fisher_exact_greater(a, b, c, d) == pytest.approx(hypergeom_sf(a, N, K, n))


def test_fisher_two_sided_matches_bruteforce():
    """雙尾：暴力列出固定邊界下所有表格的機率，加總 ≤ 觀測表機率的項"""
    for (a, b, c, d) in [(3, 1, 1, 3), (5, 5, 2, 8), (0, 4, 3, 5), (7, 2, 3, 6)]:
        N, K, n = a + b + c + d, a + c, a + b
        lo, hi = max(0, n - (N - K)), min(K, n)
        p_obs = math.exp(hypergeom_logpmf(a, N, K, n))
        expected = sum(math.exp(hypergeom_logpmf(i, N, K, n)) for i in range(lo, hi + 1)
                       if math.exp(hypergeom_logpmf(i, N, K, n)) <= p_obs * (1 + 1e-7))
        assert fisher_exact_two_sided(a, b, c, d) == pytest.approx(min(1.0, expected), rel=1e-10)


def test_fisher_two_sided_never_below_min_tail():
    """有效性質：雙尾 p ≥ min(右尾, 左尾)。

    注意「雙尾 p ≥ 單尾 greater」**不成立**：當觀測表落在左尾極端（a 為最小可能值）時
    greater = 1.0，而雙尾只加總機率 ≤ 觀測表的項 → 會比 1 小。這是常見誤解，特別記下來。
    """
    rng = random.Random(0)
    for _ in range(200):
        a = rng.randint(0, 12); b = rng.randint(0, 40); c = rng.randint(0, 12); d = rng.randint(0, 40)
        N, K, n = a + b + c + d, a + c, a + b
        greater = fisher_exact_greater(a, b, c, d)
        lesser = 1.0 - hypergeom_sf(a + 1, N, K, n)
        two = fisher_exact_two_sided(a, b, c, d)
        assert two >= min(greater, lesser) - 1e-12
        assert 0 < two <= 1.0


def test_hand_computed_tea_tasting():
    """經典例子：6 杯中 3 杯有加牛奶，全猜對的機率 = 1/C(6,3) = 0.05"""
    assert fisher_exact_greater(3, 0, 0, 3) == pytest.approx(1 / 20, rel=1e-12)
    # 雙尾 = 兩側各 1/20（X=0 與 X=3 的機率都是 1/20）→ 0.10，與 scipy 一致
    assert fisher_exact_two_sided(3, 0, 0, 3) == pytest.approx(2 / 20, rel=1e-12)


def test_hand_computed_simple_table():
    """手算：N=100, K=10, n=20, k=5 → P(X≥5) 用超幾何公式直接算"""
    expected = sum(math.comb(10, i) * math.comb(90, 20 - i) for i in range(5, 11)) / math.comb(100, 20)
    assert hypergeom_sf(5, 100, 10, 20) == pytest.approx(expected, rel=1e-12)


# ─────────────────────────────────────────────────────────────
# ② 效應量
# ─────────────────────────────────────────────────────────────
def test_odds_ratio_exact():
    or_, corrected, method = odds_ratio(10, 90, 5, 95)
    assert or_ == pytest.approx((10 * 95) / (90 * 5))
    assert corrected is False and "未校正" in method


def test_odds_ratio_zero_bad_in_group():
    """a = 0 → OR 就是 0（組內沒有任何壞批次），不需要校正；校正只用在無法計算時"""
    or_, corrected, method = odds_ratio(0, 20, 5, 75)
    assert or_ == 0.0 and corrected is False


def test_or_ci_corrects_when_cell_is_zero():
    """但信賴區間在 0 格時無法取對數 → 必須加 0.5 校正並回報 corrected=True"""
    or_, lo, hi, corrected = or_ci_woolf(0, 20, 5, 75)
    assert corrected is True
    assert 0 < lo < or_ < hi


def test_or_ci_brackets_point_estimate():
    or_, lo, hi, corrected = or_ci_woolf(10, 90, 5, 95)
    assert lo < or_ < hi
    assert corrected is False


def test_or_ci_wider_with_smaller_sample():
    _, lo1, hi1, _ = or_ci_woolf(10, 90, 5, 95)
    _, lo2, hi2, _ = or_ci_woolf(2, 18, 1, 19)
    assert (hi2 - lo2) > (hi1 - lo1)


def test_contingency_stats_consistency():
    s = contingency_stats(8, 12, 3, 30)
    assert s["n_total"] == 8 + 12 + 3 + 30
    assert s["n_bad_total"] == 8 + 3
    assert s["n_group"] == 8 + 12
    assert s["k_bad"] == 8
    assert s["p_hyper"] == pytest.approx(s["p_fisher_greater"])
    assert s["p_fisher_two_sided"] >= s["p_hyper"] - 1e-12


# ─────────────────────────────────────────────────────────────
# ③ 多重比較
# ─────────────────────────────────────────────────────────────
def test_bonferroni_basic():
    assert bonferroni([0.01, 0.2, 0.5]) == [pytest.approx(0.03), pytest.approx(0.6), pytest.approx(1.0)]


def test_bh_textbook_example():
    """教科書例子：p = 0.01…0.05，m=5 → q 全為 0.05"""
    q = benjamini_hochberg([0.01, 0.02, 0.03, 0.04, 0.05])
    assert all(v == pytest.approx(0.05) for v in q)


def test_bh_properties_random():
    rng = random.Random(42)
    p = [rng.random() for _ in range(200)]
    q = benjamini_hochberg(p)
    assert all(0 <= qi <= 1 for qi in q)
    assert all(qi >= pi - 1e-12 for qi, pi in zip(q, p))          # q ≥ p
    pairs = sorted(zip(p, q))
    assert all(pairs[i][1] <= pairs[i + 1][1] + 1e-12 for i in range(len(pairs) - 1))  # 單調


def test_bh_controls_null_false_positives():
    """純 null（p ~ U(0,1)）下，BH 的偽發現率應接近名目 α，未校正則約為 α·m"""
    rng = random.Random(7)
    m = 1000
    false_raw = false_bh = 0
    trials = 40
    for _ in range(trials):
        p = [rng.random() for _ in range(m)]
        q = benjamini_hochberg(p)
        false_raw += sum(1 for v in p if v < 0.05)
        false_bh += sum(1 for v in q if v < 0.05)
    assert false_raw / trials > 30        # 未校正約 50 個假陽性
    assert false_bh / trials <= 2         # BH 後幾乎為 0


def test_evaluate_summary_counts():
    p = [0.001, 0.01, 0.2, 0.5, 0.9]
    q, pb, s = evaluate(p, alpha=0.05)
    assert s.n_tests == 5
    assert s.n_sig_raw == sum(1 for v in p if v < 0.05)
    assert s.expected_false_positives_raw == pytest.approx(0.25)


# ─────────────────────────────────────────────────────────────
# ④ 排名引擎
# ─────────────────────────────────────────────────────────────
def test_rank_groups_table_consistency():
    """每個群組的 a+b+c+d 必須等於總單位數；a+c = 壞單位總數"""
    units = {i: (i % 7 == 0) for i in range(1, 301)}
    memberships = [(i, "recipe", f"R{i % 5}") for i in range(1, 301)]
    memberships += [(i, "tool", f"T{i % 3}") for i in range(1, 301)]
    res = rank_groups(units, memberships)
    n_bad = sum(units.values())
    for r in res:
        assert r.a + r.b + r.c + r.d == len(units)
        assert r.a + r.c == n_bad
        assert r.a + r.b == r.n_group


def test_rank_groups_injected_cause_ranks_top():
    """注入根因：某群組的壞率明顯偏高 → 該群組的 p 值必須最小"""
    units = {}
    memberships = []
    rng = random.Random(3)
    for i in range(1, 501):
        g = f"G{i % 10}"
        is_bad = rng.random() < (0.5 if g == "G3" else 0.05)
        units[i] = is_bad
        memberships.append((i, "group", g))
    res = rank_groups(units, memberships)
    testable = [r for r in res if r.testable]
    best = min(testable, key=lambda r: r.stats["p_hyper"])
    assert best.group_key == "G3"
    assert best.stats["odds_ratio"] > 3


def test_rank_groups_flags_low_support():
    units = {i: (i % 5 == 0) for i in range(1, 101)}
    memberships = [(1, "rare", "only-one"), (2, "rare", "only-one")] + \
                  [(i, "big", f"B{i % 4}") for i in range(1, 101)]
    res = rank_groups(units, memberships, RankConfig(min_group_units=5))
    rare = next(r for r in res if r.group_key == "only-one")
    assert rare.testable is False and "群組批次數" in rare.flag


# ─────────────────────────────────────────────────────────────
# ⑤ 與 scipy 對照（容器內會執行；助理端無 scipy 時自動跳過）
# ─────────────────────────────────────────────────────────────
@pytest.mark.skipif(not HAS_SCIPY, reason="此環境沒有 scipy（容器內會執行）")
def test_scipy_agreement():
    from scipy.stats import fisher_exact, hypergeom
    tables = [(8, 12, 3, 30), (1, 20, 5, 50), (0, 10, 4, 40), (15, 5, 10, 40), (30, 70, 10, 90)]
    for (a, b, c, d) in tables:
        N, K, n = a + b + c + d, a + c, a + b
        assert hypergeom_sf(a, N, K, n) == pytest.approx(hypergeom.sf(a - 1, N, K, n), rel=1e-12)
        exp_greater = fisher_exact([[a, b], [c, d]], alternative="greater")[1]
        exp_two = fisher_exact([[a, b], [c, d]], alternative="two-sided")[1]
        assert fisher_exact_greater(a, b, c, d) == pytest.approx(exp_greater, rel=1e-10)
        assert fisher_exact_two_sided(a, b, c, d) == pytest.approx(exp_two, abs=1e-10)
