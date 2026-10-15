"""M5-4-0 兩層串接前置診斷的單元測試（合成資料，不需資料庫）。

重點：洩漏防線必須被**機製化**驗證（改變其他批次的資料 → 本批次結果不變），
以及 2×2 檢定必須與 M3 的既有實作一致（兩層用同一套統計才知道差異來自輸入）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "analysis"))

import commonality_basic as cb            # noqa: E402
import m5_4_precheck as pc                # noqa: E402


def test_parse_sensor_handles_pipe_and_bare_names():
    assert pc.parse_sensor("IONGAUGEPRESSURE|slope") == "IONGAUGEPRESSURE"
    assert pc.parse_sensor("flowcoolpressure") == "FLOWCOOLPRESSURE"
    assert pc.parse_sensor("") == ""
    assert pc.parse_sensor(None) == ""


def test_aggregation_only_uses_the_lots_own_alarms():
    """洩漏防線（機製化）：批次 A 的聚合結果不得受批次 B 的警報影響。"""
    rows = [{"lot_id": "A", "event_id": 1, "sensor": "S1|x"},
            {"lot_id": "A", "event_id": 2, "sensor": "S1|x"},
            {"lot_id": "B", "event_id": 3, "sensor": "S2|x"}]
    before = pc.aggregate_alarms_per_lot(rows)["A"]
    rows_later = rows + [{"lot_id": "B", "event_id": 4, "sensor": "S3|x"},
                         {"lot_id": "B", "event_id": 5, "sensor": "S3|x"}]
    after = pc.aggregate_alarms_per_lot(rows_later)["A"]
    assert before == after                              # A 完全不受影響
    assert pc.aggregate_alarms_per_lot(rows_later)["B"]["n_alarms"] == 3


def test_aggregation_ignores_rows_without_lot():
    rows = [{"lot_id": None, "event_id": 1, "sensor": "S1|x"},
            {"lot_id": "", "event_id": 2, "sensor": "S1|x"}]
    assert pc.aggregate_alarms_per_lot(rows) == {}       # 不可靜默歸到別的批次


def test_dependency_summary_counts_single_sensor_lots():
    per_lot = {"A": {"n_alarms": 3, "n_sensors": 1, "sensors": ["S1"], "n_events": 3},
               "B": {"n_alarms": 4, "n_sensors": 2, "sensors": ["S1", "S2"], "n_events": 4},
               "C": {"n_alarms": 0, "n_sensors": 0, "sensors": [], "n_events": 0}}
    dep = pc.dependency_summary(per_lot)
    assert dep["n_lots"] == 2                            # 沒警報的批次不算
    assert abs(dep["frac_single_sensor"] - 0.5) < 1e-12
    assert abs(dep["mean_sensors"] - 1.5) < 1e-12


def test_build_2x2_matches_m3_fisher_and_odds_ratio():
    alerted = {"L1", "L2", "L3"}
    bad = {"L1", "L2", "L4"}
    all_lots = {"L1", "L2", "L3", "L4", "L5"}
    tab = pc.build_2x2(alerted, bad, all_lots)
    assert (tab["a_alert_bad"], tab["b_alert_good"], tab["c_noalert_bad"],
            tab["d_noalert_good"]) == (2, 1, 1, 1)
    # 必須與 M3 的實作完全一致（同一套統計）
    assert tab["fisher_p_greater"] == cb.fisher_exact_greater(2, 1, 1, 1)
    assert abs(tab["odds_ratio"] - 2.0) < 1e-12


def test_contingency_alert_vs_outcome_single_cell_inputs():
    r = pc.contingency_alert_vs_outcome(True, True, 10, 4, 5)
    assert (r["a"], r["b"], r["c"], r["d"]) == (1, 0, 0, 0)


def test_load_from_csv_flags_orphan_alarms():
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "events.csv").write_text(
        "event_id,lot_id,tool_id\n1,L1,T1\n2,L1,T1\n3,L2,T2\n", encoding="utf-8")
    (d / "lots.csv").write_text(
        "lot_id,tool_id,outcome_bad,n_events,first_ts,last_ts\n"
        "L1,T1,1,2,100,200\nL2,T2,0,1,300,400\n", encoding="utf-8")
    (d / "alarms.csv").write_text(
        "event_id,in_ts,top1\n1,150,S1|x\n99,150,S1|x\n", encoding="utf-8")
    data = pc.load_from_csv(d / "alarms.csv", d / "events.csv", d / "lots.csv")
    assert data["alarms"][0]["lot_id"] == "L1"           # 對得上
    assert data["alarms"][1]["orphan"] is True           # event 99 不存在 → 孤兒（不可靜默丟棄）
    assert data["units"]["L1"]["outcome_bad"] is True
    assert data["lots"]["L1"]["n_events"] == 2


def test_strict_attribution_removes_dilution():
    """核心：寬鬆歸屬會稀釋（各台比率趨同），嚴格歸屬能分開。

    設定：3 台機台、10 個批次；警報只發生在 T1 上，但多數批次經手三台。
    """
    membership = []
    for i in range(10):
        for tool in ("T1", "T2", "T3"):
            membership.append((f"L{i}", tool, 5))
    alarms_by_lot_tool = {(f"L{i}", "T1"): 3 for i in range(10)}      # 只有 T1 有警報
    strict = pc.per_tool_rates_strict(alarms_by_lot_tool, membership)
    assert strict["T1"]["rate"] == 1.0
    assert strict["T2"]["rate"] == 0.0 and strict["T3"]["rate"] == 0.0

    wide = pc.per_tool_rates_lot_level({f"L{i}" for i in range(10)}, membership)
    assert all(abs(v["rate"] - 1.0) < 1e-12 for v in wide.values())   # 三台都被記成 100% ✗


def test_rank_table_reports_both_attributions_and_ranks():
    membership = [(f"L{i}", t, 5) for i in range(6) for t in ("T1", "T2")]
    alarms = {(f"L{i}", "T1"): 1 for i in range(3)}
    rows = pc.rank_table(pc.per_tool_rates_lot_level({"L0", "L1", "L2"}, membership),
                         pc.per_tool_rates_strict(alarms, membership))
    assert [r["tool"] for r in rows][0] == "T1"
    assert rows[0]["rank_strict"] == 1
    assert {r["tool"] for r in rows} == {"T1", "T2"}                 # 兩版都要出現


def test_csv_membership_has_one_row_per_lot_tool():
    """membership 必須每個 (批次, 機台) 一列（與 mart.lot_membership 同語意）。

    先前寫成每個事件一列 → 嚴格歸屬版的分母變成事件數（合成實測 7.8% 應為 46.7%）。
    """
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "events.csv").write_text(
        "event_id,lot_id,tool_id,in_ts\n"
        "1,L1,T1,100\n2,L1,T1,200\n3,L1,T2,300\n", encoding="utf-8")
    (d / "lots.csv").write_text(
        "lot_id,tool_id,outcome_bad,n_events\nL1,T1,0,2\nL1,T2,0,1\n", encoding="utf-8")
    (d / "alarms.csv").write_text("event_id,in_ts,top1\n1,150,S|x\n", encoding="utf-8")
    data = pc.load_from_csv(d / "alarms.csv", d / "events.csv", d / "lots.csv")
    assert len(data["membership"]) == 2                       # (L1,T1)、(L1,T2) 各一列
    assert sorted(n for _l, _t, n in data["membership"]) == [1, 2]   # n_events 是事件數
    strict = pc.per_tool_rates_strict(data["alarms_by_lot_tool"], data["membership"])
    assert strict["T1"]["rate"] == 1.0 and strict["T2"]["rate"] == 0.0   # 分母是 1，不是事件數
