"""M5-3-0 trace 對齊前置診斷的單元測試（合成資料，不需資料庫）。

原則：對齊函式必須用「已知答案」驗證——重採樣要能精確還原線性訊號、
DTW 對純位移要幾乎為零、成本擋關要真的會擋。
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "analysis"))

import trace_precheck as tp  # noqa: E402


def test_resample_reproduces_linear_ramp_exactly():
    ts = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
    ys = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
    out = tp.resample_to_length(ts, ys, 5)
    assert np.allclose(out, ys)
    out9 = tp.resample_to_length(ts, ys, 9)
    assert np.allclose(out9, np.linspace(0, 4, 9))          # 線性訊號 → 內插即精確


def test_resample_is_time_normalised_not_raw_time():
    """兩段形狀相同但持續時間差 50 倍 → 重採樣後必須一致（否則等於在比持續時間）。

    用線性訊號：分段線性內插對線性訊號是精確的，所以差異只可能來自「沒有做時間正規化」。
    （先前用正弦測卻要求 atol=1e-6 —— 粗取樣與細取樣的正弦本來就有 O(h²) 差異，
      那是**測試期望寫錯**，不是程式錯。）
    """
    a_ts, a_ys = np.linspace(0, 10, 11), np.linspace(0, 1, 11)
    b_ts, b_ys = np.linspace(0, 500, 101), np.linspace(0, 1, 101)     # 長度差 50 倍
    assert np.allclose(tp.resample_to_length(a_ts, a_ys, 20),
                       tp.resample_to_length(b_ts, b_ys, 20), atol=1e-9)


def test_dtw_zero_for_identical_and_small_for_shift():
    """⚠ 不要用 np.roll 測位移：它會環繞，造成波形不連續（先前測試期望寫錯）。

    相位平移用「重新取樣」的方式造；形狀不同用「斜坡 vs 正弦」造（餘弦只是相位平移，
    拿來當「完全不同」是錯的——DTW 本來就該把它視為接近）。
    """
    u = np.linspace(0, 3 * np.pi, 60)
    v = np.sin(u)
    assert tp.dtw_distance(v, v) < 1e-9
    shifted = np.sin(u + 0.4)                                   # 純相位平移
    different = np.linspace(-1, 1, 60)                          # 斜坡：形狀完全不同
    d_shift, d_diff = tp.dtw_distance(v, shifted), tp.dtw_distance(v, different)
    assert d_shift < 0.5
    assert d_diff > d_shift * 3, (d_shift, d_diff)


def test_shape_norm_none_keeps_level_so_duration_signal_survives():
    """前處理必須可選：zscore 會丟掉水準（耗時類變數的訊號就在水準上）。"""
    v = np.linspace(0, 10, 30)
    assert np.allclose(tp.zshape(v, "none"), v)                    # none → 原值
    assert abs(float(tp.zshape(v, "zscore").mean())) < 1e-9         # zscore → 置中
    assert abs(float(tp.zshape(v, "zscore").std()) - 1.0) < 1e-9


def test_zshape_removes_level_and_amplitude():
    v = np.sin(np.linspace(0, 3 * np.pi, 40))
    assert np.allclose(tp.zshape(v), tp.zshape(v * 7.5 - 20.0), atol=1e-9)
    assert tp.zshape(np.ones(10)) is None                      # 常數序列 → 無法正規化


def test_between_within_ratio_math():
    assert abs(tp.between_within_ratio([1.0, 1.0], [2.0, 2.0]) - 2.0) < 1e-12
    assert tp.between_within_ratio([], [1.0]) is None
    assert tp.between_within_ratio([0.0], [1.0]) is None        # 組內為 0 → 無法比值


def test_cost_gate_blocks_over_budget():
    ok, msg = tp.cost_gate(500, 1000, force=False)
    assert ok and "預估讀取 500" in msg
    ok, msg = tp.cost_gate(5000, 1000, force=False)
    assert not ok and "拒絕執行" in msg
    ok, msg = tp.cost_gate(5000, 1000, force=True)
    assert ok and "--force" in msg


def _mk_entries_traces(spec, n_per_group=8, n_points=60, seed=5):
    """spec: [(tool, recipe, step, sign)] → (entries, traces)。

    sign 控制斜坡方向：形狀不同的機台要能被診斷判成「組間 > 組內」。
    """
    rng = np.random.default_rng(seed)
    entries, traces = {}, {}
    eid = 0
    for tool, recipe, step, sign in spec:
        evs = []
        for _ in range(n_per_group):
            eid += 1
            ts = np.linspace(0, 200, n_points)
            ys = np.linspace(0, sign, n_points) + rng.normal(0, 0.05, n_points)
            traces[(eid, "SEN")] = (ts, ys)
            evs.append({"event_id": eid, "n_points": n_points})
        entries[(tool, recipe, step)] = evs
    return entries, traces


def _ratio(entries, traces, resample_len=40, mode="resample", pairwise_cap=40):
    seqs = tp.build_sequences(entries, traces, {"SEN": "sen"}, "SEN", mode, resample_len)
    by_key = {}
    for (tool, recipe, step), v in seqs.items():
        by_key.setdefault((recipe, step), {})[tool] = v
    within, between = [], []
    for key, by_tool in by_key.items():
        tools_here = [t for t, v in by_tool.items() if len(v) >= 3]
        if len(tools_here) < 2:
            continue
        for t in tools_here:
            m = tp.pairwise_mean_dtw(by_tool[t], pairwise_cap)
            if m is not None:
                within.append(m)
        for i in range(len(tools_here)):
            for j in range(i + 1, len(tools_here)):
                a, b = by_tool[tools_here[i]][:6], by_tool[tools_here[j]][:6]
                ds = [tp.dtw_distance(x, y) for x in a for y in b]
                between.append(float(np.mean(ds)))
    return tp.between_within_ratio(within, between), within, between


def test_shared_coverage_requires_same_recipe_not_just_same_step_number():
    """核心修正的測試：兩個機台跑「同號步驟但不同 recipe」→ 不可視為可比較。"""
    entries = {
        ("A", "R1", "3"): [{"event_id": i, "n_points": 50} for i in range(8)],
        ("B", "R2", "3"): [{"event_id": 100 + i, "n_points": 50} for i in range(8)],
    }
    assert tp.shared_coverage(entries, min_events=5) == {}          # 不同 recipe → 不共享
    entries[("B", "R1", "3")] = [{"event_id": 200 + i, "n_points": 50} for i in range(8)]
    shared = tp.shared_coverage(entries, min_events=5)
    assert set(shared) == {("R1", "3")}
    assert set(shared[("R1", "3")]) == {"A", "B"}
    # 門檻：事件數不足就不算共享
    assert tp.shared_coverage(entries, min_events=9) == {}


def test_end_to_end_ratio_above_one_when_shape_carries_tool_identity():
    """同一 recipe＋步驟、兩機台形狀不同 → 組間距離必須大於組內距離。"""
    spec = [("A", "R1", "1", +1.0), ("B", "R1", "1", -1.0),
            ("A", "R1", "2", +1.0), ("B", "R1", "2", -1.0)]
    entries, traces = _mk_entries_traces(spec)
    ratio, _, _ = _ratio(entries, traces)
    assert ratio is not None and ratio > 2.0, ratio


def test_identical_tools_give_ratio_near_one():
    """負控制：兩機台形狀相同 → 比值應接近 1（方法本身不得虛高）。"""
    spec = [("A", "R1", "1", +1.0), ("B", "R1", "1", +1.0)]
    entries, traces = _mk_entries_traces(spec, seed=11)
    ratio, _, _ = _ratio(entries, traces)
    assert ratio is not None and 0.5 < ratio < 2.0, ratio


def test_dtw_mode_path_works_end_to_end():
    spec = [("A", "R1", "1", +1.0), ("B", "R1", "1", -1.0)]
    entries, traces = _mk_entries_traces(spec)
    ratio, _, _ = _ratio(entries, traces, mode="dtw")
    assert ratio is not None and ratio > 2.0, ratio


def test_stride_sample_covers_whole_period_not_just_the_beginning():
    """修正抽樣偏誤的核心測試：等距抽樣必須涵蓋到尾端事件（頭端抽樣只到第 10 個）。"""
    events = [{"event_id": i, "n_points": 50} for i in range(100)]
    assert len(tp.stride_sample(events, 1)) == 100                 # k=1 → 全部保留
    stride = tp.stride_sample(events, 10)
    assert len(stride) == 10
    assert stride[0]["event_id"] == 0 and stride[-1]["event_id"] == 90
    naive_head = events[:10]
    assert naive_head[-1]["event_id"] == 9
    assert stride[-1]["event_id"] > naive_head[-1]["event_id"] * 5


def test_bootstrap_ci_excludes_one_when_signal_is_real():
    rng = np.random.default_rng(2)
    within = list(rng.normal(1.0, 0.1, 40))
    between = list(rng.normal(2.0, 0.1, 40))
    lo, hi = tp.bootstrap_ratio_ci(within, between, reps=300)
    assert lo is not None and lo > 1.0, (lo, hi)


def test_bootstrap_ci_contains_one_when_there_is_no_signal():
    """負控制：組間與組內同分布 → 區間必須含 1（不可宣稱有鑑別力）。"""
    rng = np.random.default_rng(3)
    within = list(rng.normal(1.0, 0.3, 40))
    between = list(rng.normal(1.0, 0.3, 40))
    lo, hi = tp.bootstrap_ratio_ci(within, between, reps=300)
    assert lo is not None and lo <= 1.0 <= hi, (lo, hi)


def test_bootstrap_ci_needs_enough_samples():
    assert tp.bootstrap_ratio_ci([1.0], [2.0]) == (None, None)


def test_to_mean_ratio_detects_separated_groups_and_negative_control():
    """to_mean 判準：分離的兩群 → 比值大；同分布兩群 → 比值接近 1。"""
    rng = np.random.default_rng(9)
    sep = {"A": [rng.normal(+1, 0.1, 30) for _ in range(20)],
           "B": [rng.normal(-1, 0.1, 30) for _ in range(20)]}
    ratio, w, b = tp.to_mean_ratio(sep)
    assert ratio is not None and ratio > 5, ratio
    same = {"A": [rng.normal(0, 1.0, 30) for _ in range(20)],
            "B": [rng.normal(0, 1.0, 30) for _ in range(20)]}
    ratio2, _, _ = tp.to_mean_ratio(same)
    # ⚠ 不能用「> 1」當門檻：高維時比值在無訊號下也系統性偏離 1（實測 0.28）。
    #    正確判準是與置換虛無分布比較 → 用 permutation_null。
    assert ratio2 is not None and ratio2 < ratio, (ratio2, ratio)


def test_permutation_null_rejects_real_signal_and_accepts_noise():
    """置換檢定才是判準：分離的兩群必須勝過虛無；同分布兩群必須勝不過。"""
    rng = np.random.default_rng(13)
    sep = {}
    for step in ("1", "2", "3"):
        sep[(step,)] = {"A": [rng.normal(+1.5, 0.2, 25) for _ in range(12)],
                        "B": [rng.normal(-1.5, 0.2, 25) for _ in range(12)]}
    r_sep = tp.permutation_null(sep, "to_mean", n_perm=60)
    assert r_sep["observed"] > r_sep["null_q95"], r_sep
    assert r_sep["p_value"] <= 0.05, r_sep

    noise = {}
    for step in ("1", "2", "3"):
        noise[(step,)] = {"A": [rng.normal(0, 1.0, 25) for _ in range(12)],
                          "B": [rng.normal(0, 1.0, 25) for _ in range(12)]}
    r_noise = tp.permutation_null(noise, "to_mean", n_perm=60)
    assert not (r_noise["observed"] > r_noise["null_q95"]), r_noise


def test_to_mean_ratio_returns_none_for_ragged_sequences():
    """DTW 模式的序列長度不一 → 不可用 to_mean（需回報 None，不能硬算）。"""
    ragged = {"A": [np.zeros(10), np.zeros(12)], "B": [np.zeros(11), np.zeros(9)]}
    assert tp.to_mean_ratio(ragged) == (None, None, None)


def test_identify_rate_is_one_for_separated_and_half_for_identical():
    """AUC 才是「可辨識」的判準：完全分離＝1.0；同分布＝約 0.5。"""
    rng = np.random.default_rng(21)
    A = rng.normal(+2.0, 0.2, (40, 20))
    B = rng.normal(-2.0, 0.2, (40, 20))
    auc = tp.identify_rate(A, B, A.mean(axis=0), B.mean(axis=0))
    assert auc > 0.99, auc
    C = rng.normal(0, 1.0, (40, 20))
    D = rng.normal(0, 1.0, (40, 20))
    auc2 = tp.identify_rate(C, D, C.mean(axis=0), D.mean(axis=0))
    # 留一法之後，同分布的兩群應接近 0.5（未用留一法時實測 0.71，
    # 那是 in-sample 偏誤，不是真的可辨識）
    assert 0.35 < auc2 < 0.65, auc2


def test_identify_by_key_skips_ragged_groups_instead_of_crashing():
    """跨機台長度不一致（dtw 模式）→ 略過該集團，不可崩潰。"""
    by_key = {("R", "1"): {"A": [np.zeros(75)] * 12, "B": [np.zeros(90)] * 12},
              ("R", "2"): {"A": [np.zeros(50)] * 12, "B": [np.zeros(50)] * 12}}
    rates, used, eligible, total = tp.identify_by_key(by_key, min_n=10)
    assert len(rates) == 1 and used == 1 and eligible == 1 and total == 2                                    # 只有第二個集團可用
    assert not tp.to_mean_ratio(by_key[("R", "1")], 10)[0]   # 第一個集團回報不可用


def test_to_mean_ratio_returns_none_across_unequal_lengths():
    """跨機台長度不一致必須回報不可用（先前只檢查單一機台內部 → 實際崩潰）。"""
    assert tp.to_mean_ratio({"A": [np.zeros(75)] * 5, "B": [np.zeros(90)] * 5}, 3) == (None, None, None)
