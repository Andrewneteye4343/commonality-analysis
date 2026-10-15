"""M5-4-2／M5-4-3 的單元測試（合成資料，不需資料庫）。

最重要的一條：**統計量取欄位時缺鍵要大聲失敗**（不可靜默變 nan）——
實際踩到：我用了 `st.get("or", nan)`，但真正的鍵是 `odds_ratio`
→ 全部統計量變 nan，報表照樣產出、看起來像跑完了。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "analysis"))

import commonality_basic as cb                     # noqa: E402
import run_m5_4_commonality as mc                  # noqa: E402


def test_pick_raises_loudly_on_missing_key():
    st = cb.contingency_stats(30, 70, 20, 880)
    assert mc.pick(st, "odds_ratio") == st["odds_ratio"]
    assert mc.pick(st, "or", "odds_ratio") == st["odds_ratio"]      # 相容舊名
    try:
        mc.pick(st, "or", "p_value")                                # 兩個都不存在
        raise AssertionError("應該要 raise")
    except KeyError as exc:
        assert "contingency_stats 缺少預期的欄位" in str(exc)


def test_stats_from_cells_matches_m3_statistics():
    out = mc.stats_from_cells({"a": 30, "b": 70, "c": 20, "d": 880})
    ref = cb.contingency_stats(30, 70, 20, 880)
    assert out["odds_ratio"] == ref["odds_ratio"]
    assert out["p_value"] == ref["p_fisher_greater"]
    assert out["in_group"] == 100 and out["bad_total"] == 50


def test_group_contingency_is_pure_set_arithmetic():
    """四個細胞一律由集合運算得出（先前人工對應填錯 → 警報率全變 100%）。"""
    all_u = {"L1", "L2", "L3", "L4"}
    gin = {"L1", "L2"}
    flagged = {"L1", "L3"}
    bad = {"L1", "L3"}
    c = mc.group_contingency(gin, flagged, all_u)
    assert c == {"a": 1, "b": 1, "c": 1, "d": 1}       # a=L1 b=L2 c=L3 d=L4
    assert c["a"] + c["b"] == len(gin)                 # 表格完整性（這條抓過兩次錯）


def _data():
    """3 台機台、4 個批次；L1 同時經手 T1/T2（用來驗證稀釋語意）。"""
    return {
        "alert_units": {
            "lot": {("L1", ""): True, ("L2", ""): False, ("L3", ""): True, ("L4", ""): False},
            "lot_tool": {("L1", "T1"): True, ("L1", "T2"): False,
                         ("L2", "T1"): False, ("L3", "T2"): True,
                         ("L3", "T3"): False, ("L4", "T3"): False},
        },
        "bad_lot": {"L1": True, "L2": False, "L3": True, "L4": False},
        "membership": [("L1", "T1"), ("L1", "T2"), ("L2", "T1"), ("L3", "T2"),
                       ("L3", "T3"), ("L4", "T3")],
        "faults": {"T1": 5, "T2": 0, "T3": 1},
        "event_rate": {"T1": 0.05, "T2": 0.01, "T3": 0.02},
    }


def test_wide_attributes_lot_alarm_to_every_tool_that_processed_it():
    """稀釋語意（機製化）：L1 的警報同時記在 T1 與 T2 上。

    手算：T1 群組 {L1,L2}；wide 警報 {L1,L3}；壞批次 {L1,L3}；全體 {L1..L4}
      → a=1(L1) b=0 c=1(L3) d=1(L4)
      T2 群組 {L1,L3} → a=2 b=0 c=0 d=2
    """
    wide = mc.compute_wide(_data())
    assert (wide["T1"]["a"], wide["T1"]["b"], wide["T1"]["c"], wide["T1"]["d"]) == (1, 1, 1, 1)
    assert wide["T1"]["alerted"] == 1 and wide["T1"]["total"] == 2
    assert wide["T2"]["alerted"] == 2 and wide["T2"]["rate"] == 1.0
    assert (wide["T2"]["a"], wide["T2"]["b"], wide["T2"]["c"], wide["T2"]["d"]) == (2, 0, 0, 2)


def test_strict_attributes_alarm_only_to_the_tool_where_it_happened():
    """手算：T1 單位 {(L1,T1),(L2,T1)}；警報只有 (L1,T1) → a=1 b=0
      組外壞單位 3 個（含 (L1,T2)）；組外好單位 2 個"""
    strict = mc.compute_strict(_data())
    # 手算（修正版）：L1 同時在 T1/T2 → 壞單位共 4 個（(L1,T1)(L1,T2)(L3,T2)(L3,T3)）
    # 組外壞 = 3、組外好 = {(L2,T1),(L4,T3)} = 2
    assert (strict["T1"]["a"], strict["T1"]["b"], strict["T1"]["c"], strict["T1"]["d"]) == (1, 1, 1, 3)
    assert strict["T1"]["rate"] == 0.5 and strict["T1"]["total"] == 2
    assert strict["T2"]["alerted"] == 1 and strict["T2"]["total"] == 2   # 只有 L3
    assert strict["T3"]["alerted"] == 0 and strict["T3"]["total"] == 2


def test_rank_tools_sorts_desc_and_skips_none():
    per = {"A": {"rate": 0.1}, "B": {"rate": None}, "C": {"rate": 0.3}}
    assert [t for t, _ in mc.rank_tools(per, "rate")] == ["C", "A"]


def test_evaluate_criteria_uses_pret_registered_rules():
    # 故障機台集合要包含斷言用到的機台（先前只放 {"X","Z"} 卻斷言 Y → 期望寫錯）
    crit = mc.evaluate_criteria([("X", 0.9), ("Y", 0.8)],
                                [("Y", 0.9), ("04M01", 0.8)], {"X", "Y"})
    assert crit["1_control_not_top2"]["wide"] is True        # 寬鬆版前 2 沒有負控制
    assert crit["1_control_not_top2"]["strict"] is False     # 嚴格版有 → 不通過
    assert crit["2_fault_tool_top2"]["wide"] is True         # X 是真故障機台
    assert crit["2_fault_tool_top2"]["strict"] is True       # Y 也是
    # 負控制：若真故障集合不含前 2 名，判準 2 必須為 False
    crit2 = mc.evaluate_criteria([("X", 0.9)], [("Y", 0.9)], {"Z"})
    assert crit2["2_fault_tool_top2"]["wide"] is False
    assert crit2["2_fault_tool_top2"]["strict"] is False


def test_order_agreement_counts_identical_orders():
    same = mc.order_agreement([["A", "B"], ["A", "B"]])
    assert same["n_same"] == 1 and same["n_pairs"] == 1
    diff = mc.order_agreement([["A", "B"], ["B", "A"], ["A", "B"]])
    assert diff["n_same"] == 1 and diff["n_pairs"] == 3


def test_m3_reference_uses_outcome_not_alarms():
    """M3 對照：屬性換成壞批次（與警報完全無關）。"""
    ref = mc.compute_m3_reference(_data())
    assert ref["T1"]["n_bad_in_group"] == 1   # L1
    assert ref["T2"]["n_bad_in_group"] == 2   # L1 與 L3 都是壞批次（手算又漏了 L1）
    assert ref["T3"]["n_bad_in_group"] == 1   # L3
    assert ref["T1"]["in_group"] == 2         # 經手 2 個批次
