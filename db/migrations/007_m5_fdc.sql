-- M5-1：FDC 管制圖結果表
--
-- 設計原則
--   ① 管制圖的**基準期統計量必須落地**：否則無法重現「當時的管制限是多少」，
--      也就無法回答「這條警報是不是用當時的標準判的」。
--   ② 警報是稀疏事件 → `mart.fdc_excursions` 只存**真的有觸發**的 (事件 × 感測器 × 特徵 × 方法)。

-- 基準期統計量（每個「機台 × recipe_step × 感測器 × 特徵 × 管制圖」一列）
CREATE TABLE IF NOT EXISTS mart.fdc_baselines (
    run_id            text   NOT NULL,   -- 例如 M5_univ_w24h_arl370
    tool_id           text   NOT NULL,
    recipe_step       text   NOT NULL,
    sensor            text   NOT NULL,
    feature           text   NOT NULL,   -- mean_val / std_val / range_val / slope / missing_rate
    method            text   NOT NULL,   -- shewhart / ewma / cusum
    n_base            integer NOT NULL,  -- 基準期點數
    center            double precision,
    sigma             double precision,
    sigma_method      text,              -- mr / std / mad
    param_1           double precision,  -- shewhart: k ／ ewma: lambda ／ cusum: k
    param_2           double precision,  -- ewma: L ／ cusum: h ／ shewhart: NULL
    arl0_target       double precision,
    ucl               double precision,
    lcl               double precision,
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, tool_id, recipe_step, sensor, feature, method)
);
COMMENT ON TABLE mart.fdc_baselines IS
    'FDC 管制圖的基準期統計量與管制限（可重現「當時用什麼標準判定」）';

CREATE INDEX IF NOT EXISTS idx_fdc_baselines_lookup
    ON mart.fdc_baselines (run_id, tool_id, recipe_step);

-- 異常事件（只存觸發的組合）
CREATE TABLE IF NOT EXISTS mart.fdc_excursions (
    run_id            text   NOT NULL,
    event_id          bigint NOT NULL,
    tool_id           text   NOT NULL,
    recipe_step       text   NOT NULL,
    lot_id            text,
    in_ts             numeric,           -- 原始時間軸（同 stg.fact_process_event.in_ts）
    sensor            text   NOT NULL,
    feature           text   NOT NULL,
    method            text   NOT NULL,   -- shewhart / ewma / cusum
    seq_no            integer,           -- 該群組內的時間序號（1 起算）
    is_baseline       boolean NOT NULL DEFAULT false,  -- 是否落在基準期（不該有警報）
    z_value           double precision,  -- 標準化統計量（shewhart 用）
    stat_value        double precision,  -- 管制統計量（ewma 的 z_t、cusum 的 C⁺/C⁻）
    limit_ucl         double precision,
    limit_lcl         double precision,
    direction         text,              -- high / low
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, event_id, sensor, feature, method)
);
COMMENT ON TABLE mart.fdc_excursions IS
    'FDC 判定異常的（加工事件 × 感測器 × 特徵 × 管制圖）；稀疏表，只存觸發者';

CREATE INDEX IF NOT EXISTS idx_fdc_excursions_event ON mart.fdc_excursions (run_id, event_id);
CREATE INDEX IF NOT EXISTS idx_fdc_excursions_tool ON mart.fdc_excursions (run_id, tool_id);

-- 事件層級的彙總檢視（一個事件有幾個感測器/方法觸發）
CREATE OR REPLACE VIEW mart.v_fdc_event_alerts AS
SELECT run_id, event_id, tool_id, recipe_step, lot_id, in_ts,
       count(*)                       AS n_alerts,
       count(DISTINCT sensor)         AS n_sensors,
       count(DISTINCT method)         AS n_methods,
       bool_or(is_baseline)           AS any_baseline,
       min(created_at)                AS first_seen
FROM mart.fdc_excursions
GROUP BY run_id, event_id, tool_id, recipe_step, lot_id, in_ts;

COMMENT ON VIEW mart.v_fdc_event_alerts IS
    '事件層級的 FDC 警報彙總（n_alerts = 該事件觸發的（感測器 × 特徵 × 方法）組合數）';

-- 自我檢查
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = 'mart' AND table_name = 'fdc_baselines') THEN
        RAISE EXCEPTION 'mart.fdc_baselines 建立失敗';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = 'mart' AND table_name = 'fdc_excursions') THEN
        RAISE EXCEPTION 'mart.fdc_excursions 建立失敗';
    END IF;
    RAISE NOTICE '007_m5_fdc.sql 套用完成：mart.fdc_baselines、mart.fdc_excursions、'
                 'mart.v_fdc_event_alerts';
END $$;
