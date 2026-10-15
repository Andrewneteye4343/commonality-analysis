-- M5-4-1：兩層串接的第一層聚合（事件層警報 → 批次層特徵）
--
-- 為什麼要有兩種 unit_kind（這是 M5-4-0 實測後才定案的）
-- ────────────────────────────────────────────────────────────
--   PHM 的 lot_id 是「全域」的：55.0% 的批次會經過多台機台。
--   ① unit_kind='lot'（寬鬆，與 M3 同語意）：
--        批次的警報記在它經過的每一台機台上 → 與 M3 的 TCA 完全可比，
--        但**會稀釋**（實測第 2～5 名全擠在 13.3–14.4%，差 1.1 個百分點）。
--   ② unit_kind='lot_tool'（嚴格）：
--        警報只記在真正發生警報的那台機台 → 鑑別力明顯較好
--        （實測第 2～5 名 1.8–6.6%，差 3.7 倍）。
--   兩者都必須落表、都必須報告；不可只報好看的那一個（判準已在計畫中寫死）。
--
-- 洩漏防線：本表的每一列只使用「該單位自己的事件與警報」，
--   不使用其他批次、也不使用該單位之後的時間（由 tests 機製化驗證）。
CREATE TABLE IF NOT EXISTS mart.lot_alert_features (
    run_id            text NOT NULL,
    unit_kind         text NOT NULL,          -- 'lot'（寬鬆）| 'lot_tool'（嚴格）
    lot_id            text NOT NULL,
    tool_id           text NOT NULL,          -- unit_kind='lot' 時為 ''（機台見 lot_membership）
    n_events          integer NOT NULL,       -- 該單位的事件數（分母）
    has_alarm         boolean NOT NULL,
    n_alarm_events    integer NOT NULL,       -- 有警報的「事件」數（去重）
    n_alarms          integer NOT NULL,       -- 警報「列」數（M5-2 的明細列）
    n_sensors         integer NOT NULL,       -- 涉及幾個感測器（依賴性揭露用）
    sensors           text,                   -- 逗號分隔（可追溯）
    first_alarm_ts    numeric,
    last_alarm_ts     numeric,
    first_alarm_pos   double precision,       -- 最早警報在該單位事件序列中的相對位置 0–1
    outcome_bad       boolean,                -- 批次層標籤（與 M3 同來源；僅供比對，不進模型）
    PRIMARY KEY (run_id, unit_kind, lot_id, tool_id)
);

COMMENT ON TABLE mart.lot_alert_features IS
  'M5-4-1：事件層 FDC/MSPC 警報聚合到批次層。unit_kind=lot 為 M3 同語意（會稀釋）、lot_tool 為嚴格歸屬。';

CREATE INDEX IF NOT EXISTS ix_laf_run_kind ON mart.lot_alert_features (run_id, unit_kind);
CREATE INDEX IF NOT EXISTS ix_laf_has_alarm ON mart.lot_alert_features (run_id, unit_kind, has_alarm);
