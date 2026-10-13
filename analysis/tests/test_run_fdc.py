"""M5-1 測試：run_fdc 的分段、偵測、評估數學（含「不可用未來資訊」的決定性檢查）"""
from __future__ import annotations

import csv
import random
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import spc  # noqa: E402
from spc import arl_cusum  # noqa: E402
from run_fdc import (  # noqa: E402
    build_charts, evaluate, iter_csv_series, load_fault_times, load_labels,
    parse_csv_list,
)

FEATURES = ("mean_val", "std_val")


def _series(n, mu=0.0, sd=1.0, seed=1, shift_at=None, shift=0.0):
    rng = random.Random(seed)
    out = [rng.gauss(mu, sd) for _ in range(n)]
    if shift_at is not None:
        for i in range(shift_at, n):
            out[i] += shift
    return out


# ── 清單解析（PowerShell 前導零陷阱） ───────────────────────
def test_parse_csv_list_zero_pads_and_accepts_separators():
    assert parse_csv_list("1,2,04 6") == ("01", "02", "04", "06")
    assert parse_csv_list("01;02,03") == ("01", "02", "03")
    assert parse_csv_list("") == ()
    assert parse_csv_list("FLOWCOOLPRESSURE,IONGAUGEPRESSURE") == (
        "FLOWCOOLPRESSURE", "IONGAUGEPRESSURE")     # 非數字不加零


# ── 分段：基準期必須是前段，且不能偷看監控期 ────────────────
def test_baseline_split_uses_only_the_first_frac():
    xs = _series(200, seed=3)
    info, charts = build_charts(xs, base_frac=0.3, arl0=370.4, sigma_method="mad",
                                lambda_ewma=0.1, k_cusum=0.5)
    assert info["n_base"] == 60
    ref = spc.baseline_stats(xs[:60], "mad")
    assert info["center"] == pytest.approx(ref["center"], rel=1e-12)
    assert info["sigma"] == pytest.approx(ref["sigma"], rel=1e-12)


def test_baseline_is_not_contaminated_by_the_monitored_part():
    """**決定性檢查**：把監控期整段換掉，基準期統計量必須完全不變。

    這是「不可用未來資訊」的機器化驗證——若管制限會被後段資料影響，
    這條斷言就會失敗。
    """
    xs = _series(200, seed=5)
    info1, ch1 = build_charts(xs, 0.3, 370.4, "mad", 0.1, 0.5)
    mutated = xs[:60] + [v + 100.0 for v in xs[60:]]     # 只改監控期
    info2, ch2 = build_charts(mutated, 0.3, 370.4, "mad", 0.1, 0.5)
    assert info1["center"] == pytest.approx(info2["center"], rel=1e-12)
    assert info1["sigma"] == pytest.approx(info2["sigma"], rel=1e-12)
    # 但監控期的警報必須大幅增加（管制限沒動）
    base_alarms = sum(1 for i, a in enumerate(ch1["shewhart"]["alarm"]) if a and i >= 60)
    mut_alarms = sum(1 for i, a in enumerate(ch2["shewhart"]["alarm"]) if a and i >= 60)
    assert mut_alarms > base_alarms


# ── 偵測：注入偏移必須被至少一種方法抓到 ───────────────────
def test_injected_shift_is_detected_after_baseline():
    xs = _series(300, seed=7, shift_at=120, shift=2.0)
    info, charts = build_charts(xs, 0.3, 370.4, "mr", 0.1, 0.5)
    for method in ("shewhart", "ewma", "cusum"):
        alarmed_after = [i for i, a in enumerate(charts[method]["alarm"]) if a and i >= info["n_base"]]
        assert alarmed_after, f"{method} 沒抓到 2σ 的持續偏移"
        assert min(alarmed_after) >= info["n_base"]      # 基準期不該誤判


def test_cusum_has_much_shorter_arl1_than_shewhart_for_small_shift():
    """小偏移（0.8σ）時 CUSUM 的**平均**偵測時間應遠短於 Shewhart —— 這是 CUSUM 存在的理由。

    用 ARL1 比較（決定性、不依賴單次亂數）；單一 realization 誰先觸發是雜訊決定的，
    不能拿來當判準（實際踩到：某個 seed 下 Shewhart 靠一個 3σ 雜訊點先觸發）。
    """
    h = 4.81884765625                     # 由 ARL0=370.4（雙側）反推的決策區間
    arl1_cusum = arl_cusum(0.5, h, delta=0.8, sides=2)
    arl1_shewhart = 1.0 / (1.0 - spc._phi(3.0 - 0.8))   # 單側 0.8σ 偏移下的 Shewhart
    assert arl1_cusum < arl1_shewhart / 2, (arl1_cusum, arl1_shewhart)


def test_cusum_detects_earlier_on_average_across_seeds():
    """多 seed 的行為檢查（避開單一 realization 的巧合）"""
    cusum_gap, shewhart_gap = [], []
    for seed in range(12):
        xs = _series(400, seed=seed, shift_at=150, shift=0.8)
        info, charts = build_charts(xs, 0.3, 370.4, "mr", 0.1, 0.5)
        for method, acc in (("cusum", cusum_gap), ("shewhart", shewhart_gap)):
            idx = [i for i, a in enumerate(charts[method]["alarm"]) if a and i >= 150]
            acc.append((min(idx) - 150) if idx else 250)      # 沒抓到算 250（上限）
    assert sum(cusum_gap) / len(cusum_gap) < sum(shewhart_gap) / len(shewhart_gap)


# ── 評估數學 ────────────────────────────────────────────────
def test_evaluate_detection_rate_and_lead_time():
    labelled = {1: False, 2: True, 3: True, 4: False, 5: True}
    alerts = {2, 3, 4}                       # 命中 2 個窗內、誤報 1 個窗外
    ts = {1: 100.0, 2: 4000.0, 3: 5000.0, 4: 6000.0, 5: 9000.0}
    tool_of_event = {2: "01M02", 3: "01M02", 4: "01M02", 5: "01M02"}
    faults = {"01M02": [10000.0]}
    res = evaluate(alerts, labelled, faults, tool_of_event, ts)
    assert res["n_events_in_window"] == 3 and res["n_events_out_window"] == 2
    assert res["detection_rate_in_window"] == pytest.approx(2 / 3)
    assert res["alert_rate_out_window"] == pytest.approx(1 / 2)
    assert res["fault_detection_rate"] == 1.0
    # 領先時間 = 故障時刻 − 該機台最後一次警報 = 10000 − 6000 = 4000 秒 ≈ 1.111 h
    assert res["per_fault"][0]["lead_hours"] == pytest.approx(4000 / 3600, rel=1e-9)


def test_evaluate_marks_missed_fault():
    labelled = {1: True}
    faults = {"01M02": [5000.0]}
    res = evaluate(set(), labelled, faults, {1: "01M02"}, {1: 100.0})
    assert res["fault_detection_rate"] == 0.0
    assert res["per_fault"][0]["detected"] is False
    assert res["lead_hours_mean"] is None


def test_evaluate_ignores_alerts_after_the_fault():
    """故障**之後**的警報不算領先時間（否則會高估預警能力）"""
    labelled = {1: True}
    ts = {1: 8000.0}                          # 警報在故障（5000）之後
    res = evaluate({1}, labelled, {"T": [5000.0]}, {1: "T"}, ts)
    assert res["per_fault"][0]["detected"] is False


# ── CSV 串流 ────────────────────────────────────────────────
def test_iter_csv_series_groups_and_preserves_order():
    tmp = tempfile.TemporaryDirectory()
    p = Path(tmp.name) / "series.csv"
    with p.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["tool_id", "recipe_step", "event_id", "in_ts", "lot_id", "sensor",
                    "mean_val", "std_val"])
        for tool in ("01M02", "04M01"):
            for i in range(1, 4):
                w.writerow([tool, "1", i, i * 10, f"L{i}", "S1", float(i), "", ])
    out = list(iter_csv_series(p, FEATURES))
    keys = [k for k, _ in out]
    assert keys == [("01M02", "1", "S1"), ("04M01", "1", "S1")]
    recs = out[0][1]
    assert [r["event_id"] for r in recs] == [1, 2, 3]
    assert recs[1]["mean_val"] == 2.0
    assert recs[0]["std_val"] is None          # 空值 → None（不是 0）
    tmp.cleanup()


def test_load_labels_and_faults_from_csv():
    tmp = tempfile.TemporaryDirectory()
    lp = Path(tmp.name) / "labels.csv"
    with lp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["event_id", "fault_type", "window_hours", "in_window"])
        w.writerow([1, "ANY", 24, "True"])
        w.writerow([2, "ANY", 24, "False"])
        w.writerow([3, "ANY", 12, "True"])     # 不同窗長 → 不該被 24h 篩選收進來
    labels = load_labels(lp, scope="ANY", window_hours=24)
    assert labels == {1: True, 2: False}

    fp = Path(tmp.name) / "faults.csv"
    with fp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["tool_id", "fault_ts"])
        w.writerow(["01M02", "10000"])
        w.writerow(["01M02", "20000"])
    faults = load_fault_times(fp)
    assert faults["01M02"] == [10000.0, 20000.0]
    tmp.cleanup()


# ── 版本一致性（防止「交付的檔案版本」與「VERSION 檔」漂移） ──────
def test_version_constant_matches_version_file():
    """script_version.VERSION（找不到 VERSION 檔時的備援）必須與專案 VERSION 檔一致。

    實際踩過：VERSION 已升到 r16，但 script_version 的備援常數還停在 r15，
    在沒有 /work/VERSION 的環境下 --version 會回報舊版本——交付時最怕這種靜默漂移。
    """
    root = Path(__file__).resolve().parents[2]
    # 專案佈局：etl/script_version.py；容器佈局：/work/script_version.py（./etl 掛在 /work）
    for cand_dir in (root / "etl", root, Path("/work")):
        if cand_dir.is_dir():
            sys.path.insert(0, str(cand_dir))
    import script_version
    # VERSION 檔可能不在可及範圍（容器只掛 ./etl → /work/VERSION 由 compose 另外掛入；
    # 專案佈局則在 <repo>/VERSION）。找不到就跳過並說明，但**不可以靜默通過**。
    candidates = [root / "VERSION", root.parent / "VERSION", Path("/work/VERSION"),
                  Path("/VERSION")]
    file_ver = None
    for cand in candidates:
        if cand.is_file():
            file_ver = cand.read_text(encoding="utf-8").strip()
            break
    if file_ver is None:
        pytest.skip(f"找不到 VERSION 檔（已找 {[str(c) for c in candidates]}）→ "
                    "無法驗證備援常數；請確認 compose 有掛 ./VERSION:/work/VERSION:ro")
    assert script_version.VERSION == file_ver, (script_version.VERSION, file_ver)


# ── 代號解析（實際踩到：--tools "03" 對不上資料庫的 03M01 → 安靜回 0 筆） ──
class _FakeCur:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        self._sql = sql

    def fetchall(self):
        return list(self._rows)


def test_resolve_tool_ids_accepts_code_and_full_id():
    from run_fdc import resolve_tool_ids
    cur = _FakeCur([("03M01",), ("01M02",), ("06M01",)])
    assert resolve_tool_ids(cur, ("03",))[0] == ["03M01"]          # 2 位數代號 → 前綴比對
    assert resolve_tool_ids(cur, ("03M01",))[0] == ["03M01"]        # 完整 ID
    assert resolve_tool_ids(cur, ("03_M01",))[0] == ["03M01"]       # 檔名寫法
    assert resolve_tool_ids(cur, ("0",))[0] == ["01M02", "03M01", "06M01"]   # 較長前綴
    ids, missing = resolve_tool_ids(cur, ("99",))
    assert ids == [] and missing == ["99"]                          # 對不上要回報，不可靜默


def test_resolve_sensors_is_case_insensitive():
    from run_fdc import resolve_sensors
    cur = _FakeCur([("FLOWCOOLPRESSURE",), ("IONGAUGEPRESSURE",)])
    assert resolve_sensors(cur, ("flowcoolpressure",))[0] == ["FLOWCOOLPRESSURE"]
    ids, missing = resolve_sensors(cur, ("NOSUCH",))
    assert ids == [] and missing == ["NOSUCH"]


# ── 真實資料的關鍵性質：機台老化造成線性漂移（固定基準線會失效） ──
def _drift_series(seed=3, n=1200, slope=0.02, step_at=900, step=6.0):
    rng = random.Random(seed)
    y = [slope * i + rng.gauss(0, 1) for i in range(n)]
    for i in range(step_at, n):
        y[i] += step
    return y


def _alarm_rate(chart, lo, hi):
    idx = range(lo, hi)
    return sum(1 for i in idx if chart["alarm"][i]) / max(len(list(idx)), 1)


def test_linear_trend_recovers_known_slope():
    from spc import linear_trend
    a, b = linear_trend(_drift_series(step=0.0), n_fit=360)
    assert b == pytest.approx(0.02, abs=0.005)
    assert abs(a) < 0.5


def test_trend_mode_handles_tool_drift_that_fixed_mode_cannot():
    """固定基準線在漂移資料上會嚴重誤報（實測 53%）；trend 模式要吸收漂移並仍抓到階梯。

    這條測試固定住真實資料（PHM 感測值隨機台老化漂移）踩到的性質：
    若之後有人把 trend 模式改壞，這裡會失敗。
    """
    y = _drift_series()
    info_f, ch_f = build_charts(y, 0.3, 370.4, "mad", 0.1, 0.5, mode="fixed")
    info_t, ch_t = build_charts(y, 0.3, 370.4, "mad", 0.1, 0.5, mode="trend")

    assert _alarm_rate(ch_f["shewhart"], info_f["n_base"], 900) > 0.2     # 固定模式：漂移期大量誤報
    assert _alarm_rate(ch_t["shewhart"], info_t["n_base"], 900) < 0.02    # 趨勢模式：回到名目水準
    assert any(ch_t["shewhart"]["alarm"][i] for i in range(900, 960))    # 階梯仍被抓到
    assert info_t["slope"] == pytest.approx(0.02, abs=0.005)
    assert info_t["mode"] == "trend" and info_f["mode"] == "fixed"


def test_degenerate_constant_series_is_skipped_with_reason():
    """整段常數（σ≈0）→ 必須回報 skip_reason，讓呼叫端列出來（不可安靜跳過）"""
    info, charts = build_charts([5.0] * 100, 0.3, 370.4, "mad", 0.1, 0.5, mode="fixed")
    assert info["skip_reason"] is not None
    assert charts == {}


def test_rolling_mode_marks_warmup_points_as_undecided():
    y = _drift_series(step=0.0)
    info, charts = build_charts(y, 0.3, 370.4, "mad", 0.1, 0.5, mode="rolling", window=200)
    assert info["judged_from"] == 200
    assert all(not charts["shewhart"]["alarm"][i] for i in range(200))   # 暖身期不做判定


def _quiet_then_loud(seed=11, n=600, quiet=180):
    """重現真實資料的失效型態：基期很安靜（σ 被壓小），之後變吵。

    真實 03M01 實測：某序列基期 σ=0.0004，之後散布是它的 390 倍 → 每個點都在響。
    """
    rng = random.Random(seed)
    out = [rng.gauss(0, 0.02) for _ in range(quiet)]
    out += [rng.gauss(0, 1.0) for _ in range(n - quiet)]
    return out


def test_sigma_scope_baseline_collapses_on_quiet_baseline():
    y = _quiet_then_loud()
    info, ch = build_charts(y, 0.3, 370.4, "mad", 0.1, 0.5, mode="trend",
                            methods=("shewhart",), sigma_scope="baseline")
    rate = _alarm_rate(ch["shewhart"], info["n_base"], len(y))
    assert rate > 0.5                       # 基期 σ 崩掉 → 幾乎每個點都響（真實資料的症狀）


def test_sigma_scope_expanding_avoids_the_collapse():
    """expanding（只用過去資料、無洩漏）要能大幅壓低這種序列的警報率"""
    y = _quiet_then_loud()
    _, ch_base = build_charts(y, 0.3, 370.4, "mad", 0.1, 0.5, mode="trend",
                              methods=("shewhart",), sigma_scope="baseline")
    info, ch_exp = build_charts(y, 0.3, 370.4, "mad", 0.1, 0.5, mode="trend",
                                methods=("shewhart",), sigma_scope="expanding")
    r_base = _alarm_rate(ch_base["shewhart"], info["n_base"], len(y))
    r_exp = _alarm_rate(ch_exp["shewhart"], info["n_base"], len(y))
    # 實測 0.645 vs 0.962（中位數型 σ 在「變異數階梯」下天生適應慢，
    # 所以只要求「有實質改善」，不假裝能回到名目值）
    assert r_exp < r_base * 0.8, (r_exp, r_base)
    assert info["sigma_scope"] == "expanding"
