"""M5-2-0 前置診斷工具的單元測試（合成資料，不需資料庫）。

重點：統計量必須先用「已知答案」的合成資料驗證，否則診斷報告本身也可能在說謊。
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "analysis"))

import mspc_precheck as mp  # noqa: E402


def _two_factor(n=4000, p=8, seed=3):
    """2 個潛在因子 + 雜訊 → 前兩個主成分應吃掉大部分變異。"""
    rng = np.random.default_rng(seed)
    f = rng.standard_normal((n, 2))
    load = rng.standard_normal((2, p))
    return f @ load + 0.3 * rng.standard_normal((n, p))


def test_pca_spectrum_detects_low_rank_structure():
    spec = mp.pca_spectrum(_two_factor())
    assert spec["top2"] > 0.85           # 兩個因子 → 前兩主成分吃掉絕大多數變異
    assert spec["k90"] <= 4              # 不需要用到全部 8 維


def test_pca_spectrum_on_independent_columns_is_flat():
    rng = np.random.default_rng(5)
    spec = mp.pca_spectrum(rng.standard_normal((4000, 8)))
    assert spec["top1"] < 0.35           # 獨立 → 沒有單一主導成分
    assert spec["k90"] >= 5


def test_mahalanobis_mean_is_about_p():
    """Phase I 的馬氏距離平方平均 ≈ p(n-1)/n（用同一批資料估參數時的已知結果）。"""
    X = np.random.default_rng(9).standard_normal((3000, 6))
    d2 = mp.mahalanobis_d2(X)
    assert abs(d2.mean() - 6 * 2999 / 3000) < 0.15


def test_detrend_removes_linear_trend():
    n = 500
    t = np.arange(n, dtype=float)
    X = np.column_stack([2.0 * t + 1.0, -0.5 * t + 3.0, t * 0 + 7.0])
    Z = mp.detrend(X)
    assert np.allclose(Z[:, 0], 0, atol=1e-6)
    assert np.allclose(Z[:, 1], 0, atol=1e-6)
    # 常數欄（標準差 0）必須保持原值、不被動到——detrend 只處理有變異的欄位
    assert np.allclose(Z[:, 2], 7.0, atol=1e-9)


def test_log_transform_only_touches_listed_columns():
    X = np.array([[0.0, -1.0], [3.0, 2.0], [8.0, -4.0]])
    Z = mp.log_transform(X, [0])
    assert np.allclose(Z[:, 0], np.log1p(X[:, 0]))
    assert np.allclose(Z[:, 1], X[:, 1])           # 未列出的欄位不得被動到


def test_simulated_limit_matches_exact_when_scipy_available():
    """模擬法與精確 Beta 公式必須一致（沒有 scipy 時跳過並說明，不可靜默通過）。"""
    exact = mp.d2_limit_exact(8, 2000, 0.9973)
    if exact is None:
        import pytest
        pytest.skip("沒有 scipy → 無法與精確 Beta 公式對照（容器內應會有 scipy）")
    sim = mp.chi2_limit_simulated(8, 2000, 0.9973, n_sim=60)
    assert abs(sim - exact) / exact < 0.10, (sim, exact)


def test_step_variance_share_high_when_groups_differ():
    """兩個步驟各自在不同水準 → 步驟解釋的變異比例應該很高。"""
    rng = np.random.default_rng(4)
    a = rng.normal(0.0, 0.2, size=(300, 3))
    b = rng.normal(5.0, 0.2, size=(300, 3))
    X = np.vstack([a, b])
    steps = ["1"] * 300 + ["2"] * 300
    sv = mp.step_variance_share(X, steps)
    assert sv["mean_r2"] > 0.95


def test_step_variance_share_low_when_groups_identical():
    rng = np.random.default_rng(6)
    X = rng.normal(0.0, 1.0, size=(600, 3))
    sv = mp.step_variance_share(X, ["1"] * 300 + ["2"] * 300)
    assert sv["mean_r2"] < 0.05


def test_centre_by_step_removes_between_group_offsets():
    X = np.array([[0.0, 10.0], [2.0, 12.0], [100.0, 40.0], [104.0, 44.0]])
    steps = ["1", "1", "2", "2"]
    Z = mp.centre_by_step(X, steps)
    assert np.allclose(Z[(np.array(steps) == "1")].mean(axis=0), 0.0)
    assert np.allclose(Z[(np.array(steps) == "2")].mean(axis=0), 0.0)
    # 組內差異必須保留（不可整組壓成 0）
    assert abs(Z[1, 0] - Z[0, 0]) > 0
