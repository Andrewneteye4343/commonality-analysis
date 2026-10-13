#!/usr/bin/env python3
"""M5-1 單元測試：單變量 SPC 管制圖與 ARL。

驗證策略（與前面里程碑同一套紀律）：
1. **封閉解**：Shewhart 的 ARL0 = 1/(2(1−Φ(k))) 是精確式（k=3 → 370.4）→ 可當絕對錨點
2. **兩種獨立方法互驗**：ARL 用「馬可夫鏈」與「蒙地卡羅模擬」各算一次，必須一致
   （與 M3 用暴力列舉驗超幾何公式同一個精神）
3. **文獻值**：單側 CUSUM k=0.5、h=5 的 ARL0 ≈ 465（教科書常用值）
4. **行為測試**：注入已知偏移必須抓到、穩健 σ 必須不被單一離群值帶走
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spc import (  # noqa: E402
    arl_cusum, arl_ewma, arl_monte_carlo, arl_shewhart, baseline_stats,
    cusum_chart, design_cusum, design_ewma, design_shewhart, ewma_chart,
    ewma_limits, ewma_series, moving_range_sigma, robust_sigma, shewhart_chart,
)


def _normal_series(n, mu=0.0, sd=1.0, seed=1, shift_at=None, shift=0.0):
    rng = random.Random(seed)
    out = [rng.gauss(mu, sd) for _ in range(n)]
    if shift_at is not None:
        for i in range(shift_at, n):
            out[i] += shift
    return out


# ── 封閉解錨點 ──────────────────────────────────────────────
def test_shewhart_arl_closed_form():
    """k = 3 的 ARL0 是 370.4（教科書標準值）；k = 2 是 22.0"""
    assert arl_shewhart(3.0) == pytest.approx(370.4, abs=0.5)
    assert arl_shewhart(2.0) == pytest.approx(22.0, abs=0.1)


def test_design_shewhart_inverts_the_closed_form():
    k = design_shewhart(370.4)
    assert k == pytest.approx(3.0, abs=1e-3)
    assert arl_shewhart(design_shewhart(100.0)) == pytest.approx(100.0, rel=1e-3)


# ── 兩法互驗：CUSUM ─────────────────────────────────────────
def test_arl_cusum_matches_published_table():
    """文獻設計表的 CUSUM ARL0 是**雙側**值（k=0.5：h=4 → 168、h=5 → 465）。

    本模組的馬可夫鏈解的是單側鏈（見 arl_cusum docstring），故用 sides=2。
    單側值約為雙側的 2 倍，這個關係本身就是「雙側 = 單側/2」近似的驗證。
    """
    assert arl_cusum(0.5, 4.0, m=60, sides=2) == pytest.approx(168.0, rel=0.06)
    assert arl_cusum(0.5, 5.0, m=60, sides=2) == pytest.approx(465.0, rel=0.06)
    one_sided = arl_cusum(0.5, 5.0, m=60)
    assert one_sided == pytest.approx(2 * arl_cusum(0.5, 5.0, m=60, sides=2), rel=1e-12)


def test_arl_cusum_markov_matches_monte_carlo_one_sided():
    """馬可夫鏈 vs 蒙地卡羅（單側）：兩法必須一致"""
    mc = arl_monte_carlo("cusum", {"k": 0.5, "h": 4.0, "sides": 1},
                         n_runs=4000, seed=7)
    mk = arl_cusum(0.5, 4.0, m=60)
    assert mk == pytest.approx(mc, rel=0.15), (mk, mc)


def test_arl_cusum_two_sided_approximation_is_verified_by_simulation():
    """雙側 ARL 用「單側/2」近似 —— 用模擬驗證這個近似的誤差在容忍範圍內"""
    mc = arl_monte_carlo("cusum", {"k": 0.5, "h": 5.0, "sides": 2},
                         n_runs=3000, seed=11)
    approx = arl_cusum(0.5, 5.0, m=60, sides=2)
    # 模擬與「單側/2」近似必須一致（實測 451.6 vs 445，誤差 ~1.5%）
    assert approx == pytest.approx(mc, rel=0.12), (approx, mc)
    assert approx < arl_cusum(0.5, 5.0, m=60)     # 雙側比單側短


def test_arl_cusum_smaller_for_larger_shift():
    """偏移越大，ARL1 越小（單調性）"""
    a0 = arl_cusum(0.5, 5.0, delta=0.0)
    a1 = arl_cusum(0.5, 5.0, delta=1.0)
    a2 = arl_cusum(0.5, 5.0, delta=2.0)
    assert a0 > a1 > a2


def test_design_cusum_hits_target_arl():
    """設計函式以單側為目標（雙側要先把目標除以 2）"""
    h = design_cusum(k=0.5, arl0=930.0)
    assert arl_cusum(0.5, h, m=60) == pytest.approx(930.0, rel=0.05)
    assert arl_cusum(0.5, h, m=60, sides=2) == pytest.approx(465.0, rel=0.05)


# ── 兩法互驗：EWMA ──────────────────────────────────────────
def test_arl_ewma_markov_matches_monte_carlo():
    """兩法必須同量級，且差異方向可解釋：

    馬可夫鏈用**漸近**管制限；模擬用**逐點精確**限（開頭較窄）→ 模擬的 ARL 較短。
    實測 λ=0.2、L=3：馬可夫鏈 648、模擬 557（模擬較短 ✓ 符合預期）。
    """
    mc = arl_monte_carlo("ewma", {"lam": 0.2, "L": 3.0}, n_runs=3000, seed=13)
    mk = arl_ewma(0.2, 3.0, m=61)
    assert mk == pytest.approx(mc, rel=0.25), (mk, mc)
    assert mk > mc, "漸近限比前幾點的精確限寬 → 馬可夫鏈的 ARL 應較長"


def test_arl_ewma_smaller_for_larger_shift():
    assert arl_ewma(0.2, 3.0, delta=0.0) > arl_ewma(0.2, 3.0, delta=1.0) > \
           arl_ewma(0.2, 3.0, delta=2.0)


def test_design_ewma_hits_target_arl():
    L = design_ewma(lam=0.1, arl0=500.0)
    assert arl_ewma(0.1, L, m=61) == pytest.approx(500.0, rel=0.06)


# ── EWMA 序列與管制限 ───────────────────────────────────────
def test_ewma_series_recursion_and_extremes():
    xs = [1.0, 2.0, 3.0, 4.0]
    z = ewma_series(xs, lam=0.5, init=0.0)
    # z1 = 0.5*1 + 0.5*0 = 0.5；z2 = 0.5*2 + 0.5*0.5 = 1.25 …
    assert z == pytest.approx([0.5, 1.25, 2.125, 3.0625], rel=1e-12)
    assert ewma_series(xs, lam=1.0, init=0.0) == pytest.approx(xs)   # λ=1 → 等於原序列


def test_ewma_limits_converge_to_asymptotic():
    ucl, _ = ewma_limits(0.0, 1.0, lam=0.1, L=3.0, n_steps=400)
    asym = 3.0 * math.sqrt(0.1 / (2 - 0.1))
    assert ucl[-1] == pytest.approx(asym, rel=1e-6)
    assert ucl[0] < ucl[-1]          # 開頭較窄（變異數還沒收斂）


# ── 行為：抓得到已知偏移、誤報率合理 ────────────────────────
def test_shewhart_false_alarm_rate_matches_theory():
    """3σ 管制限的理論誤報率是 0.27%（= 1/370.4）。用大樣本檢查量級。"""
    noise = _normal_series(4000, seed=3)
    ch = shewhart_chart(noise, k=3.0)
    rate = sum(ch["alarm"]) / len(noise)
    assert 0.001 < rate < 0.006, rate        # 理論 0.0027 ± 抽樣誤差


def test_shewhart_detects_large_shift():
    shifted = _normal_series(400, seed=3, shift_at=200, shift=3.0)
    base = baseline_stats(shifted[:200], "mr")
    ch = shewhart_chart(shifted, base=base, k=3.0)
    assert sum(ch["alarm"][200:]) > 50       # 偏移後大量觸發（3σ 偏移機率約 50%）
    # 基準期只有隨機誤報（200 點 × 0.27% ≈ 0.5 個），不強求為 0
    assert sum(ch["alarm"][:200]) <= 3


def test_cusum_detects_small_sustained_shift_that_shewhart_misses():
    """CUSUM 的賣點：1σ 的持續偏移（Shewhart 幾乎抓不到）"""
    xs = _normal_series(300, seed=5, shift_at=100, shift=1.0)
    base = baseline_stats(xs[:100], "mr")
    sw = shewhart_chart(xs, base=base, k=3.0)
    cs = cusum_chart(xs, k=0.5, h=5.0, base=base)
    assert any(cs["alarm"][100:])            # CUSUM 抓到
    # Shewhart 對同一段資料的偵測點數明顯較少（或沒有）
    assert sum(cs["alarm"][100:]) >= sum(sw["alarm"][100:])


def test_ewma_detects_gradual_drift():
    xs = [0.0] * 100 + [0.02 * i for i in range(1, 101)]
    base = baseline_stats([_normal_series(100, sd=0.1, seed=9)[i] for i in range(100)], "mr")
    ch = ewma_chart(xs, lam=0.2, L=3.0, base=base)
    assert any(ch["alarm"][110:])            # 漂移後會抓


# ── 穩健 σ（為什麼不用樣本標準差） ─────────────────────────
def test_robust_sigma_resists_single_outlier():
    xs = _normal_series(500, seed=21)
    xs_bad = list(xs)
    xs_bad[250] = 50.0                       # 一個大離群值
    s_std = baseline_stats(xs_bad, "std")["sigma"]
    s_mad = baseline_stats(xs_bad, "mad")["sigma"]
    s_mr = baseline_stats(xs_bad, "mr")["sigma"]
    assert s_std > 1.5                       # 樣本標準差被帶走
    assert s_mad == pytest.approx(1.0, rel=0.2)     # 穩健估法不受影響
    assert s_mr < 1.5                                # 移動全距影響有限
    assert robust_sigma(xs) == pytest.approx(moving_range_sigma(xs), rel=0.3)


def test_moving_range_sigma_known_value():
    # 序列 1,2,3,4,5 → 移動全距都是 1 → MR̄ = 1 → σ̂ = 1/1.128
    assert moving_range_sigma([1, 2, 3, 4, 5]) == pytest.approx(1 / 1.128, rel=1e-12)


# ── 邊界情況 ────────────────────────────────────────────────
def test_degenerate_inputs_do_not_crash():
    assert baseline_stats([], "mr")["sigma"] > 0
    assert baseline_stats([2.0], "mr")["sigma"] > 0      # 單點
    assert baseline_stats([2.0, 2.0, 2.0], "mr")["sigma"] > 0   # 全同值
    assert ewma_series([], 0.5) == []
    ch = shewhart_chart([1.0, 1.0], k=3.0)
    assert len(ch["alarm"]) == 2
    with pytest.raises(ValueError):
        ewma_series([1.0], 0.0)
    with pytest.raises(ValueError):
        baseline_stats([1.0, 2.0], "unknown")
