"""M5-3′ 漂移前導檢查的單元測試（合成資料，不需資料庫）。

每個判準都要有「有訊號 → 抓到」與「沒訊號 → 不亂抓」兩側，否則判準本身不可信。
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "analysis"))

import drift_precheck as dp  # noqa: E402


def test_lin_slope_recovers_known_line():
    x = np.arange(50, dtype=float)
    y = 3.0 - 2.5 * x
    a, b, r2, se = dp.lin_slope(x, y)   # 第 4 個值是斜率標準誤
    assert abs(a - 3.0) < 1e-9 and abs(b + 2.5) < 1e-9 and abs(r2 - 1.0) < 1e-9


def test_lin_slope_requires_enough_points_and_variation():
    assert np.isnan(dp.lin_slope(np.array([1.0, 2.0]), np.array([1.0, 2.0]))[1])
    assert np.isnan(dp.lin_slope(np.full(10, 5.0), np.arange(10, dtype=float))[1])


def test_detrend_residual_of_a_line_is_zero():
    x = np.arange(40, dtype=float)
    y = 7.0 + 0.5 * x
    r = dp.detrend_residual(x, y)
    assert np.allclose(r[np.isfinite(r)], 0.0, atol=1e-9)


def test_robust_sigma_resists_outliers():
    # ⚠ 測資不能讓 MAD = 0（例如五個相同值＋一個離群值）——那會觸發標準差回退，
    #    測到的是回退行為而不是穩健性。改用有變異的資料。
    r = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 100.0])
    assert dp.robust_sigma(r) < 0.5 * float(np.std(r, ddof=1))
    assert np.isnan(dp.robust_sigma(np.array([1.0, 2.0])))          # 點數不足


def test_baseline_sigma_ignores_late_drift():
    """後段大範圍漂移時，全序列尺度會被拉大 → 必須用前置基準期估尺度。

    （先前這裡斷言 robust_sigma(r) > 1.5，是我的期望寫錯：25% 污染不會推
      中位數，MAD 不會膨脹。真正讓全序列尺度變大的是「趨勢擬合吸收漂移」，
      而 baseline_sigma 兩者都能避開。）
    """
    rng = np.random.default_rng(17)
    r = rng.normal(0, 0.5, 200)
    r[150:] += 6.0                                    # 後 25% 大幅偏移
    assert dp.baseline_sigma(r, 0.3) < 1.0            # 前置基準期不受影響
    assert dp.baseline_sigma(r, 0.3) <= dp.robust_sigma(r) + 1e-12


def test_cochran_q_rejects_planted_and_not_uniform():
    """整體檢定：斜率不同 → 拒絕；三台相同 → 不拒絕。"""
    ses = {"A": 0.01, "B": 0.01, "C": 0.01}
    q = dp.cochran_q({"A": 1.0, "B": 1.0, "C": 5.0}, ses)
    assert q["reject"] and q["most_deviant"] == "C" and abs(q["most_deviant_z"]) > 3
    q2 = dp.cochran_q({"A": 1.0, "B": 1.0, "C": 1.0}, ses)
    assert not q2["reject"] and abs(q2["Q"]) < 1e-12


def test_cochran_q_calibration_is_near_five_percent_under_the_null():
    """負控制（判準校準）：沒有差異時，拒絕率應接近名目 5%，不可亂抓。

    這是最重要的一個測試——判準若在虛無下就常拒絕，P2 的結論毫無意義。
    """
    rng = np.random.default_rng(31)
    n_reject = 0
    for _ in range(300):
        ses = {k: rng.uniform(0.05, 0.3) for k in "ABCD"}
        slopes = {k: rng.normal(1.0, ses[k]) for k in "ABCD"}     # 同一真值，只差估計誤差
        if dp.cochran_q(slopes, ses)["reject"]:
            n_reject += 1
    rate = n_reject / 300
    assert 0.0 <= rate <= 0.12, rate
    same = dp.peer_deviation({"A": 1.0, "B": 1.0, "C": 1.0})
    assert all(abs(v["z"] or 0.0) < 2.0 for v in same.values())


def test_lead_fault_stats_detects_planted_pre_fault_bump():
    rng = np.random.default_rng(4)
    ts = np.arange(0, 4 * 3600 * 24 * 3, 4.0)          # 秒為單位、4 秒取樣
    z = np.abs(rng.normal(1.0, 0.3, ts.size))
    fault = ts[-1]
    inw = dp.in_window_by_fault_time(ts, [fault], window_hours=24)
    z[inw] = 12.0                                       # 故障前 24 小時植入明顯漂移
    st = dp.lead_fault_stats(z, ts, in_window=inw)
    assert st["ratio"] is not None and st["ratio"] > 2.0, st
    assert st["pre_hit_rate"] is not None and st["pre_hit_rate"] >= 0.9, st


def test_lead_fault_stats_negative_control():
    """沒有植入訊號時，故障窗命中率必須與平時相當（比值接近 1，不得亂報）。"""
    rng = np.random.default_rng(5)
    ts = np.arange(0, 4 * 3600 * 24 * 3, 4.0)
    z = np.abs(rng.normal(1.0, 0.3, ts.size))
    st = dp.lead_fault_stats(z, ts, [ts[-1]], window_hours=24)
    assert st["ratio"] is None or 0.3 < st["ratio"] < 3.0, st


def test_lead_fault_stats_without_faults_is_safe():
    ts = np.arange(100, dtype=float)
    z = np.abs(np.random.default_rng(6).normal(1.0, 0.2, 100))
    st = dp.lead_fault_stats(z, ts, [])
    assert st["pre_n"] == 0 and st["ratio"] is None


def test_in_window_uses_seconds_not_hours():
    """故障窗的單位是**秒**（in_ts 是秒、窗寬要乘 3600）；寫成 ft-24 只會開 24 秒。"""
    ts = np.arange(0, 200000, 4.0)                    # 4 秒取樣、共 200,000 秒
    fault = 150000.0
    m = dp.in_window_by_fault_time(ts, [fault], window_hours=24)
    # 端點都算進去 → 24 小時的取樣點數是 21601（先前期望 21600 是差一）
    assert abs(m.sum() - 21601) <= 1
    assert ts[m].min() >= fault - 24 * 3600 and ts[m].max() <= fault
    # 錯誤寫法（ft - 24）只會涵蓋 6 個點 → 這正是先前「故障窗事件數 0~5」的原因
    assert ((ts >= fault - 24) & (ts <= fault)).sum() <= 7


def test_cochran_q_does_not_explode_when_slopes_are_nearly_identical():
    """核心回歸測試：斜率量級 1e-9、差異 1e-9 時，Q 必須由標準誤決定，不可爆成天文數字。

    先前用「同儕斜率離散度」當分母 → 實測得到 z = −210,574（毫無意義）。
    """
    slopes = {"A": 1.0000e-9, "B": 1.0001e-9, "C": 2.0000e-9}
    ses = {"A": 1e-10, "B": 1e-10, "C": 1e-10}
    q = dp.cochran_q(slopes, ses)
    assert q["Q"] is not None and q["Q"] < 1e4           # 有界
    assert q["reject"] and q["most_deviant"] == "C"


def test_cochran_q_needs_standard_errors():
    q = dp.cochran_q({"A": 1.0, "B": 1.0, "C": 5.0}, {})
    assert q["Q"] is None and not q["reject"]


def test_lin_slope_standard_error_shrinks_with_more_points():
    rng = np.random.default_rng(23)
    x = np.arange(100, dtype=float)
    y1 = 0.5 * x + rng.normal(0, 1.0, 100)
    y2 = 0.5 * np.repeat(x, 4) + rng.normal(0, 1.0, 400)
    se1 = dp.lin_slope(x, y1)[3]
    se2 = dp.lin_slope(np.repeat(x, 4), y2)[3]
    assert np.isfinite(se1) and se2 < se1                # 點數變 4 倍 → 標準誤下降


def test_csv_event_id_is_int_so_labels_match():
    """CSV 模式的 event_id 必須是 int：標籤表的鍵是整數，留字串會靜默全對不上。

    ⚠ 用 tempfile 不用 tmp_path：助理端的測試 shim 不支援 pytest fixture
      （容器內用 pytest 兩種都行）。
    """
    import tempfile
    p = Path(tempfile.mkdtemp()) / "s.csv"
    p.write_text("tool_id,recipe,recipe_step,sensor,event_id,in_ts,mean_val\n"
                 "A,R1,1,SEN,7,100,1.0\nA,R1,1,SEN,8,200,1.1\n", encoding="utf-8")
    series = list(dp.iter_csv(p, ("SEN",), "mean_val"))
    ids = [e["event_id"] for _, evs in series for e in evs]
    assert ids == [7, 8] and all(isinstance(i, int) for i in ids)


def test_peer_adjusted_residual_flags_tool_specific_step():
    """植入：C 台在後段跳升、A／B 不動 → C 的同儕殘差要大，A／B 要接近 0。"""
    bin_sec = 24 * 3600.0
    series = {}
    for tool in ("A", "B", "C"):
        rows = []
        for k in range(120):
            ts = k * bin_sec
            val = 50.0 + (8.0 if (tool == "C" and k >= 90) else 0.0)
            rows.append((ts, val, k))
        series[tool] = rows
    adj = dp.peer_adjusted_residual(series, bin_sec)
    late = slice(90, 120)
    c_late = [r[1] for r in adj["C"][late]]
    a_late = [abs(r[1]) for r in adj["A"][late]]
    # ⚠ 不做絕對斷言（原本寫 max|A| < 1e-9 是錯的）：只有 3 台時，偏離的那台
    #    會把「同儕中位數」拉走 → 正常的 A 也會出現殘差（實測 −4）。
    #    因此 3 台時只能說「C 明顯比其他人突出」，不能做歸因（需要 ≥4 台）。
    # 三台、只有一台偏離 Δ 時，理論上偏離者的殘差是 2Δ/3、正常者是 Δ/3
    # → 比值剛好是 2.0（所以不能用「> 2 倍」當門檻，我先前寫 strict > 是錯的）
    assert min(c_late) > 5.0
    assert min(c_late) > max(a_late)              # 只能保證「比被拉動的正常機台大」


def test_peer_adjusted_residual_is_flat_under_common_aging():
    """核心負控制：三台一起漂移（共同老化）→ 同儕殘差必須全部接近 0。

    這正是「同儕對照」存在的理由：沒有它，共同老化會被誤當成個別機台異常。
    """
    bin_sec = 24 * 3600.0
    series = {}
    for tool in ("A", "B", "C"):
        series[tool] = [(k * bin_sec, 50.0 + 0.05 * k, k) for k in range(120)]   # 三台完全同步
    adj = dp.peer_adjusted_residual(series, bin_sec)
    for tool in ("A", "B", "C"):
        vals = [abs(r[1]) for r in adj[tool]]
        assert max(vals) < 1e-9, (tool, max(vals))


def test_peer_adjusted_residual_skips_bins_without_peers():
    """同一個時間窗內沒有其他機台 → 不能產生殘差（不可拿不同時間的資料硬比）。"""
    bin_sec = 3600.0
    series = {"A": [(0.0, 1.0, 1), (10 * bin_sec, 9.0, 2)],
              "B": [(0.0, 1.0, 3)]}                 # B 只出現在第一個窗
    adj = dp.peer_adjusted_residual(series, bin_sec)
    assert len(adj["A"]) == 1 and len(adj["B"]) == 1     # A 的第二個窗沒有同儕 → 略過
