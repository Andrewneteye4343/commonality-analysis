"""M4-2 路線模型的測試（`analysis/run_logistic.py`）。

最重要的兩項：
1. **去混淆驗證**（`test_route_model_deconfounds_innocent_tool`）：人造資料重現 M3 的機制
   ——一台「無辜」機台與故障機台共用路線 → 其原始壞率被拉高，但模型必須還原出
   它的自身效應接近無辜水準。這是 M4 對 M3 的核心修補，必須有測試守住。
2. **設計矩陣正確性**：無截距、指示變數、控制項合併、時間趨勢。

注意：這裡的「真值」是**資料產生機制**（路線含故障機台才會壞），
不是某個特定模型的參數——加性對數勝算模型是近似，所以驗證的是**方向與量級**
（無辜機台的自身效應遠小於故障機台、原始與模型推得壞率的差距大），不是精確係數。
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run_logistic import build_design, compare_with_m3, fit_route_model  # noqa: E402


def _synthetic_route_data(seed: int = 42, n_lots: int = 3000,
                          faulting=("A", "B"), innocent=("C", "D"), noise: float = 0.02,
                          fault_hit: float = 0.12, n_recipes: int = 4):
    """人造資料重現真實資料的機制與量級。

    真實機制（PHM 2018 標籤定義）：**批次壞掉 ⟺ 它落在所屬機台故障窗內**——
    也就是「路線含故障機台」且「剛好撞上故障窗」。故障窗在時間上很稀疏
    （真實資料壞批次只有 6.6%），因此這是**罕見事件**情境：
    `fault_hit` 控制撞上故障窗的機率（預設 0.12，加上路線含故障機台的比例後
    總壞率約 9%，與真實資料同量級）。
    """
    rng = random.Random(seed)
    tools = list(faulting) + list(innocent)
    units, memberships, lot_time = {}, [], {}
    t = 0
    for i in range(n_lots):
        route = rng.sample(tools, rng.choice([1, 1, 2, 2, 3]))
        lot = f"L{i:05d}"
        t += rng.randint(10, 100)
        bad = bool(set(route) & set(faulting)) and rng.random() < fault_hit
        if rng.random() < noise:
            bad = not bad
        units[lot] = bad
        lot_time[lot] = (str(t), rng.randint(1, 5))
        for tool in route:
            memberships.append((lot, "tool", tool, 1))
        for r in rng.sample([f"R{i + 1}" for i in range(n_recipes)], rng.choice([1, 1, 2])):
            memberships.append((lot, "recipe", r, 1))
        for s in [f"S{rng.randint(0, 25)}"]:
            memberships.append((lot, "stage", s, 1))
    return units, memberships, lot_time


def _rows_by_key(rows):
    return {r["group_key"]: r for r in rows}


# ── 設計矩陣 ────────────────────────────────────────────────
def test_build_design_has_no_intercept_and_correct_indicators():
    units, memberships, lot_time = _synthetic_route_data(n_lots=60)
    d = build_design(units, memberships, "tool", controls=[], control_top_n=3,
                     time_trend=False, lot_time=lot_time)
    assert d["colnames"] == ["tool=A", "tool=B", "tool=C", "tool=D"]
    assert len(d["X"]) == len(units) == 60
    assert all(len(row) == 4 for row in d["X"])
    # 每一列至少有一個 1（每批至少經過一台機台）
    assert all(sum(row) >= 1 for row in d["X"])
    # 指示變數必須與成員關係一致
    for i, lot in enumerate(d["lots"]):
        expected = {f"tool={k}" for (_l, gt, k, _n) in memberships if _l == lot and gt == "tool"}
        got = {d["colnames"][j] for j, v in enumerate(d["X"][i]) if v == 1.0}
        assert got == expected, lot


def test_build_design_controls_pooling_and_time_trend():
    units, memberships, lot_time = _synthetic_route_data(n_lots=200)
    d = build_design(units, memberships, "tool", controls=["stage"], control_top_n=5,
                     time_trend=True, lot_time=lot_time)
    names = d["colnames"]
    assert "stage=__other__" in names          # 其餘水準合併
    assert "time_trend" in names
    assert sum(1 for n in names if n.startswith("stage=")) == 6   # 前 5 名 + 其他
    idx = names.index("time_trend")
    vals = [row[idx] for row in d["X"]]
    assert min(vals) == 0.0 and max(vals) == 1.0                   # 標準化到 [0,1]
    assert d["meta"]["n_groups"] == 4
    assert d["meta"]["control_cols"] == 6


def test_build_design_cluster_is_primary_recipe():
    units, memberships, lot_time = _synthetic_route_data(n_lots=100)
    d = build_design(units, memberships, "tool", controls=[], control_top_n=3,
                     time_trend=False, lot_time=lot_time)
    assert set(d["cluster"]) <= {"R1", "R2", "R3", "R4"}
    assert len(d["cluster"]) == len(d["lots"])


def test_build_design_rejects_unknown_group_type():
    units, memberships, lot_time = _synthetic_route_data(n_lots=30)
    with pytest.raises(SystemExit):
        build_design(units, memberships, "chamber", controls=[], control_top_n=3,
                     time_trend=False, lot_time=lot_time)


# ── 去混淆：M4 的核心價值 ──────────────────────────────────
def test_route_model_deconfounds_innocent_tool():
    """用**與機制匹配**的 hazard 模型（風險率相加）驗證去混淆。

    （logit／cloglog 在這個機制下會誤判無辜機台 —— 見
    `test_hazard_model_beats_linear_models_when_mechanism_is_additive`）
    """
    units, memberships, lot_time = _synthetic_route_data(seed=42, n_lots=3000)
    d = build_design(units, memberships, "tool", controls=[], control_top_n=4,
                     time_trend=False, lot_time=lot_time)
    rows, _diag = fit_route_model(d, ridge=1.0, model="hazard")
    R = _rows_by_key(rows)

    # ① 無辜機台 C 的原始壞率被共用路線拉高（真實資料 04M01 的 6.67% 現象）
    assert R["C"]["raw_bad_rate"] > 0.05, R["C"]
    # ② 但模型還原出的自身效應接近 0（罕見事件下加性模型可以正確辨識無辜機台）
    assert R["C"]["expected_bad_rate"] < 0.02, R["C"]
    assert R["C"]["or_"] < 0.05, R["C"]         # 風險率量級（不是勝算比）
    assert R["C"]["beta"] < R["A"]["beta"] - 0.5
    assert R["C"]["beta"] < R["B"]["beta"] - 0.5
    # ③ 「原始 − 模型推得」＝被歸因於路線共用的量，必須接近原始壞率本身
    assert R["C"]["excess_bad_rate"] > 0.05
    assert R["C"]["excess_bad_rate"] / R["C"]["raw_bad_rate"] > 0.7
    # ④ 故障機台的自身效應必須高於無辜機台，且方向正確
    assert R["A"]["expected_bad_rate"] > 3 * R["C"]["expected_bad_rate"]
    # 效果量是「風險率」（HR）而非勝算比，因此比較相對大小而非絕對門檻
    assert R["A"]["or_"] > 5 * R["C"]["or_"], (R["A"]["or_"], R["C"]["or_"])
    # ⑤ 排名：故障機台在無辜機台之前
    order = [r["group_key"] for r in rows]
    assert order.index("A") < order.index("C")
    assert order.index("B") < order.index("D")


def test_route_model_fails_in_saturated_regime_documented_limitation():
    """**已知限制**：壞率極高時（路線含故障機台幾乎一定壞），任何加性模型都會高估無辜機台。

    這是 M4 驗收要求「說明每種方法在什麼情況下會誤判」的實測記錄：
    真實資料壞率 6.6%（罕見）時模型可以正確辨識無辜機台；
    但若故障窗覆蓋率極高（例如 95%），模型只能用「重複計算」的方式逼近飽和效應，
    無辜機台的自身效應會被高估到 20% 以上。因此報告必須同時提供
    **模型無關**的「專屬批次比率」作為對照。
    """
    units, memberships, lot_time = _synthetic_route_data(seed=42, n_lots=2000, fault_hit=1.0,
                                                         noise=0.05)
    d = build_design(units, memberships, "tool", controls=[], control_top_n=4,
                     time_trend=False, lot_time=lot_time)
    rows, _diag = fit_route_model(d, ridge=1.0)
    R = _rows_by_key(rows)
    assert R["C"]["raw_bad_rate"] > 0.5                    # 無辜機台原始壞率極高（共用路線）
    assert R["C"]["expected_bad_rate"] > 0.10              # 模型仍高估它的自身效應（限制）
    assert R["A"]["beta"] > R["C"]["beta"]                 # 但排序仍正確


def test_hazard_model_beats_linear_models_when_mechanism_is_additive():
    """機制是「相加」（至少一台造成）時，風險率相加模型的自身效應估計必須最準。

    真實資料的壞批次定義正是這個機制，因此這是 M4 選用 hazard 模型的依據：
    合成資料實測（route 含 A/B → 12% 機率壞）：
      logit   給無辜機台 C 的模型壞率 ≈ 19%      （比原始 9% 還高，明顯誤判）
      cloglog 給 C ≈ 20%                        （同樣誤判）
      hazard  給 C ≈ 0.9%                       （正確：自身效應接近 0）
    """
    units, memberships, lot_time = _synthetic_route_data(seed=42, n_lots=3000)
    d = build_design(units, memberships, "tool", controls=[], control_top_n=4,
                     time_trend=False, lot_time=lot_time)
    res = {}
    for model in ("logit", "cloglog", "hazard"):
        rows, _diag = fit_route_model(d, ridge=1.0, model=model)
        res[model] = _rows_by_key(rows)
    raw_c = res["hazard"]["C"]["raw_bad_rate"]
    # 無辜機台的原始壞率被共用路線拉高，但 hazard 模型能還原其自身效應接近 0
    assert raw_c > 0.05
    assert res["hazard"]["C"]["expected_bad_rate"] < 0.02, res["hazard"]["C"]
    # 兩個線性模型則嚴重高估無辜機台自身的貢獻（這就是它們的誤判情境）
    for model in ("logit", "cloglog"):
        assert res[model]["C"]["expected_bad_rate"] > 3 * res["hazard"]["C"]["expected_bad_rate"], model
    # 故障機台在三個模型下都必須排前面（排序不受機制不匹配影響）
    for model in ("logit", "cloglog", "hazard"):
        assert res[model]["A"]["beta"] > res[model]["C"]["beta"], model
    # hazard 模型的超額量必須接近原始壞率本身（幾乎全部來自污染）
    assert res["hazard"]["C"]["excess_bad_rate"] / raw_c > 0.7


def test_route_model_ridge_shrinks_own_effect_but_keeps_order():
    units, memberships, lot_time = _synthetic_route_data(seed=7, n_lots=1500)
    d = build_design(units, memberships, "tool", controls=[], control_top_n=4,
                     time_trend=False, lot_time=lot_time)
    out = {}
    for lam in (0.5, 2.0, 8.0):
        rows, _diag = fit_route_model(d, ridge=lam)
        out[lam] = _rows_by_key(rows)
    # 懲罰越強，極端係數被拉得越平（無辜機台的負係數變得不那麼極端）
    assert out[0.5]["C"]["beta"] < out[8.0]["C"]["beta"]
    # 但排序不變：故障機台仍在前
    for lam in (0.5, 2.0, 8.0):
        assert out[lam]["A"]["beta"] > out[lam]["C"]["beta"]


# ── 輸出的一致性 ───────────────────────────────────────────
def test_fit_route_model_output_consistency():
    units, memberships, lot_time = _synthetic_route_data(seed=99, n_lots=800)
    d = build_design(units, memberships, "tool", controls=[], control_top_n=4,
                     time_trend=False, lot_time=lot_time)
    # logit 與 hazard 兩種模型都要滿足同一組輸出契約
    for model in ("logit", "hazard"):
        rows, diag = fit_route_model(d, ridge=1.0, model=model)
        assert diag["se_info"]["n_clusters"] >= 1
        for r in rows:
            assert r["model"] == model
            assert r["raw_bad_rate"] == pytest.approx(r["k_bad"] / r["n_group"], rel=1e-12)
            assert r["excess_bad_rate"] == pytest.approx(
                r["raw_bad_rate"] - r["expected_bad_rate"], abs=1e-12)
            if model == "logit":
                # 水準（單獨路線機率）由原始係數決定；or_ 則是「與其他台對比」的比值
                assert r["expected_bad_rate"] == pytest.approx(
                    1 / (1 + math.exp(-r["raw_beta"])), rel=1e-9)
                assert r["effect_type"] == "OR"
                assert r["or_"] == pytest.approx(math.exp(r["contrast"]), rel=1e-12)
                assert r["beta"] == pytest.approx(r["raw_beta"], rel=1e-12)
                assert r["p_method"] == "contrast_vs_others"
            else:
                assert r["expected_bad_rate"] == pytest.approx(
                    1 - math.exp(-r["or_"]), rel=1e-9)
                assert r["effect_type"] == "HR"
                assert r["or_"] == pytest.approx(math.exp(r["beta"]), rel=1e-12)
                assert r["contrast"] is None
                assert r["p_method"] == "lrt_boundary"
            assert r["or_ci_low"] < r["or_"] < r["or_ci_high"]
            assert 0.0 <= r["q_bh"] <= 1.0
            assert r["q_bh"] >= r["p_wald"] - 1e-15
        # q 值必須單調對應 p 值（同序）
        ps = [r["p_wald"] for r in rows]
        qs = [r["q_bh"] for r in rows]
        assert ps == sorted(ps)
        assert qs == sorted(qs)
        assert [r["rank_bh"] for r in rows] == list(range(1, len(rows) + 1))


def test_compare_with_m3_rank_correlation():
    units, memberships, lot_time = _synthetic_route_data(seed=11, n_lots=600)
    d = build_design(units, memberships, "tool", controls=[], control_top_n=4,
                     time_trend=False, lot_time=lot_time)
    rows, _diag = fit_route_model(d, ridge=1.0)
    # 造一份「與 M4 同序」的 M3 結果 → Spearman 應為 1、前 3 名完全重疊
    m3_same = [{"group_type": "tool", "group_key": r["group_key"],
                "p_hyper": r["p_wald"], "odds_ratio": r["or_"]} for r in rows]
    c = compare_with_m3(m3_same, rows, "tool")
    assert c["spearman_p"] == pytest.approx(1.0)
    assert c["top3_overlap"] == pytest.approx(1.0)
    # 反序 → Spearman −1
    m3_rev = [{"group_type": "tool", "group_key": r["group_key"],
               "p_hyper": len(rows) - i, "odds_ratio": 1 / r["or_"]} for i, r in enumerate(rows)]
    c2 = compare_with_m3(m3_rev, rows, "tool")
    assert c2["spearman_p"] == pytest.approx(-1.0)


def test_compare_with_m3_handles_too_few_common_groups():
    units, memberships, lot_time = _synthetic_route_data(seed=5, n_lots=100)
    d = build_design(units, memberships, "tool", controls=[], control_top_n=4,
                     time_trend=False, lot_time=lot_time)
    rows, _diag = fit_route_model(d, ridge=1.0)
    c = compare_with_m3([{"group_type": "tool", "group_key": "A", "p_hyper": 0.01,
                          "odds_ratio": 3.0}], rows, "tool")
    assert c["n_common"] == 1 and "note" in c


# ── 對比檢定（無截距模型的正確檢定） ─────────────────────────
def test_contrast_test_controls_false_positives_on_null_data():
    """回歸測試：β_j = 0 的 Wald 檢定在無截距模型下等於檢定「基準機率 = 50%」，
    負控制資料會讓**每一台都極度顯著**（實測踩到）。改用對比檢定後必須無誤判。"""
    units, memberships, lot_time = _synthetic_route_data(seed=7, n_lots=1500, n_recipes=30)
    rng = random.Random(99)
    units = {lot: rng.random() < 0.10 for lot in units}       # 完全沒有機台效應
    d = build_design(units, memberships, "tool", controls=[], control_top_n=0,
                     time_trend=False, lot_time=lot_time)
    # logit/cloglog 的對比檢定必須能表示「與路線無關的壞率」→ 負控制下不該誤判
    for model in ("logit", "cloglog"):
        rows, _diag = fit_route_model(d, ridge=1.0, model=model)
        flagged = [r["group_key"] for r in rows if r["q_bh"] < 0.05]
        assert len(flagged) <= 1, f"{model} 在負控制下誤判：{flagged}"
        assert all(r["p_method"] != "wald" for r in rows), "不可再用無意義的 β = 0 檢定"


def test_hazard_model_cannot_represent_route_independent_bad_rate():
    """**已揭露的局限（不是 bug）**：無截距的風險率相加模型 λ = Σh_t·on_t 沒有基準項，
    數學上無法產生「與路線無關的壞率」——若所有路線壞率相同 (=c>0)，則
    h_A = c（單走 A）且 h_A + h_B = c（走 A+B）→ 2c = c → c = 0，矛盾。
    因此均勻壞率的資料一定被強制分配給各機台（全部判顯著）。
    含意：hazard 的逐欄檢定必須搭配「壞率是路線相依」的敘述使用；真正的負控制
    要看真實資料（04M01 的 p = 0.5）而不是均勻壞率模擬。"""
    units, memberships, lot_time = _synthetic_route_data(seed=7, n_lots=1500, n_recipes=30)
    rng = random.Random(99)
    units = {lot: rng.random() < 0.10 for lot in units}       # 均勻壞率
    d = build_design(units, memberships, "tool", controls=[], control_top_n=0,
                     time_trend=False, lot_time=lot_time)
    rows, _diag = fit_route_model(d, ridge=1.0, model="hazard")
    assert len([r for r in rows if r["q_bh"] < 0.05]) == len(rows)   # 全部顯著（吸收基準）
    assert all(r["or_"] > 0.0 for r in rows)                         # 風險率被迫為正


def test_mechanism_matching_is_what_decontaminates_innocent_tools():
    """驗收條件的合成版：壞率只由 A、B 造成（機制與真實標籤一致）時，
    只有風險率相加模型能把無辜機台的「自身效應」壓到趨近 0；
    勝算／風險率相乘的模型會把無辜機台高估到原始壞率的兩倍以上。"""
    units, memberships, lot_time = _synthetic_route_data(seed=3, n_lots=2000, n_recipes=30,
                                                        noise=0.0)
    d = build_design(units, memberships, "tool", controls=[], control_top_n=0,
                     time_trend=False, lot_time=lot_time)

    hz, _ = fit_route_model(d, ridge=1.0, model="hazard")
    by_hz = {r["group_key"]: r for r in hz}
    for t in ("C", "D"):                                     # 無辜：自身效應必須趨近 0
        assert by_hz[t]["expected_bad_rate"] < 0.01, (
            f"hazard 未剝離 {t}：模型壞率 {by_hz[t]['expected_bad_rate']:.4f}")
        assert by_hz[t]["raw_bad_rate"] > 0.05               # 原始壞率其實不低（污染）
    for t in ("A", "B"):                                     # 真有故障：效應必須保留
        assert by_hz[t]["expected_bad_rate"] > 0.05

    for model in ("logit", "cloglog"):                        # 機制不匹配 → 無辜被高估
        rows, _ = fit_route_model(d, ridge=1.0, model=model)
        by = {r["group_key"]: r for r in rows}
        for t in ("C", "D"):
            assert by[t]["expected_bad_rate"] > 2 * by[t]["raw_bad_rate"], (
                f"{model} 對 {t} 的剝離應失敗（這是機制不匹配的證據）")


def test_contrast_test_orders_faulting_tools_above_innocent_ones():
    """對比檢定的名次（依 p 值）在共用路線下仍應把有故障的機台排在前面
    ——注意：判「效應是否被剝離」要用 expected_bad_rate，不是 p 值
    （p 值測的是「這台與其他台不同」，共用路線會讓無辜機台也偏離平均）。"""
    units, memberships, lot_time = _synthetic_route_data(seed=5, n_lots=3000, n_recipes=30,
                                                        noise=0.0)
    d = build_design(units, memberships, "tool", controls=[], control_top_n=0,
                     time_trend=False, lot_time=lot_time)
    rows, _ = fit_route_model(d, ridge=1.0, model="hazard")
    order = [r["group_key"] for r in rows]
    assert set(order[:2]) == {"A", "B"}, f"前兩名應為故障機台，實得 {order}"
