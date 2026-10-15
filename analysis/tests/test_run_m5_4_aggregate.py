"""M5-4-1 聚合的單元測試（合成資料，不需資料庫）。

最重要的一條：**欄位順序必須與 migration 011 完全一致**（跨檔案機器比對）——
寫入欄位錯位是靜默錯誤，跑完只會看到「有寫入」但資料全錯。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "analysis"))

import run_m5_4_aggregate as ag  # noqa: E402

EXPECTED_COLS = ["run_id", "unit_kind", "lot_id", "tool_id", "n_events", "has_alarm",
                 "n_alarm_events", "n_alarms", "n_sensors", "sensors",
                 "first_alarm_ts", "last_alarm_ts", "first_alarm_pos", "outcome_bad"]


def _events():
    # L1 經手 T1、T2；L2 只在 T2
    return [{"event_id": 1, "lot_id": "L1", "tool_id": "T1", "in_ts": 100.0},
            {"event_id": 2, "lot_id": "L1", "tool_id": "T1", "in_ts": 200.0},
            {"event_id": 3, "lot_id": "L1", "tool_id": "T2", "in_ts": 300.0},
            {"event_id": 4, "lot_id": "L2", "tool_id": "T2", "in_ts": 400.0},
            {"event_id": 5, "lot_id": "L2", "tool_id": "T2", "in_ts": 500.0}]


def test_relative_position_known_values():
    ts = [100.0, 200.0, 300.0]
    assert ag.relative_position(ts, 100.0) == 0.0
    assert abs(ag.relative_position(ts, 200.0) - 0.5) < 1e-12
    assert ag.relative_position(ts, 300.0) == 1.0
    assert ag.relative_position(ts, 50.0) == 0.0        # 夾住，不給負值
    assert ag.relative_position([100.0], 100.0) == 0.0  # 只有一個事件 → 0.0，不除以零


def test_build_units_lot_and_lot_tool_denominators():
    ev = _events()
    lots, _ = ag.build_units(ev, [], "lot")
    pairs, _ = ag.build_units(ev, [], "lot_tool")
    assert set(lots) == {("L1", ""), ("L2", "")}
    assert lots[("L1", "")]["n_events"] == 3            # 寬鬆：批次全部事件
    assert set(pairs) == {("L1", "T1"), ("L1", "T2"), ("L2", "T2")}
    assert pairs[("L1", "T1")]["n_events"] == 2         # 嚴格：只算該機台在該批次的事件
    assert pairs[("L1", "T2")]["n_events"] == 1


def test_unit_features_do_not_depend_on_other_units():
    """洩漏防線（機製化）：改動別的批次 → 本單位的特徵完全不變。"""
    ev = _events()
    alarms = [{"event_id": 1, "tool_id": "T1", "in_ts": 150.0, "sensor": "S1|slope"}]
    before, _ = ag.build_units(ev, alarms, "lot")
    later = ev + [{"event_id": 6, "lot_id": "L2", "tool_id": "T2", "in_ts": 900.0}]
    alarms_later = alarms + [{"event_id": 6, "tool_id": "T2", "in_ts": 900.0,
                              "sensor": "S9|slope"}]
    after, _ = ag.build_units(later, alarms_later, "lot")
    assert before[("L1", "")] == after[("L1", "")]      # L1 完全不受影響


def test_build_units_counts_alarms_sensors_and_position():
    ev = _events()
    alarms = [{"event_id": 1, "tool_id": "T1", "in_ts": 100.0, "sensor": "S1|slope"},
              {"event_id": 1, "tool_id": "T1", "in_ts": 100.0, "sensor": "S2|mean_val"},
              {"event_id": 2, "tool_id": "T1", "in_ts": 200.0, "sensor": "S1|slope"}]
    units, stats = ag.build_units(ev, alarms, "lot_tool")
    u = units[("L1", "T1")]
    assert u["n_alarms"] == 3 and u["n_alarm_events"] == 2      # 列數 vs 事件數（去重）
    assert u["n_sensors"] == 2 and u["sensors"] == "S1,S2"
    assert u["first_alarm_ts"] == 100.0 and u["last_alarm_ts"] == 200.0
    assert u["first_alarm_pos"] == 0.0                          # 最早警報在序列開頭
    assert stats["n_orphan_alarms"] == 0


def test_orphan_alarms_are_counted_not_silently_dropped():
    ev = _events()
    alarms = [{"event_id": 999, "tool_id": "T1", "in_ts": 1.0, "sensor": "S1|slope"}]
    units, stats = ag.build_units(ev, alarms, "lot")
    assert stats["n_orphan_alarms"] == 1
    assert not any(u["has_alarm"] for u in units.values())


def test_rows_for_copy_matches_migration_column_order():
    """跨檔案機器比對：COPY 的欄位順序必須與 migration 011 的 CREATE TABLE 完全一致。"""
    sql_path = (Path(__file__).resolve().parents[2]
                / "db" / "migrations" / "011_m5_4_lot_alert_features.sql")
    sql = sql_path.read_text(encoding="utf-8")
    body = sql[sql.index("CREATE TABLE IF NOT EXISTS mart.lot_alert_features"):]
    body = body[:body.index(");")]
    cols = re.findall(r"^\s{4}([a-z_]+)\s+(?:text|integer|boolean|numeric|double precision)",
                      body, flags=re.M)
    assert cols == EXPECTED_COLS, (cols, EXPECTED_COLS)

    units, _ = ag.build_units(_events(),
                              [{"event_id": 1, "tool_id": "T1", "in_ts": 100.0,
                                "sensor": "S1|slope"}], "lot")
    rows = ag.rows_for_copy(units, "RUN1", "lot", {"L1": True})
    assert all(len(r) == len(EXPECTED_COLS) for r in rows)
    assert rows[0][0] == "RUN1" and rows[0][1] == "lot" and rows[0][13] is True


def test_summarize_reports_per_tool_shares():
    units, _ = ag.build_units(_events(),
                              [{"event_id": 1, "tool_id": "T1", "in_ts": 100.0,
                                "sensor": "S1|slope"}], "lot_tool")
    s = ag.summarize(units, "lot_tool")
    assert s["n_units"] == 3 and s["n_alarmed"] == 1
    # T1 只在 L1 出現一次 → 單位數 1（不是事件數；我先前把兩者搞混，期望寫錯）
    assert s["by_tool"]["T1"] == {"alarmed": 1, "total": 1}
    assert s["by_tool"]["T2"] == {"alarmed": 0, "total": 2}
