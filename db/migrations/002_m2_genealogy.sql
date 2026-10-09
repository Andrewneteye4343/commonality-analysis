-- M2 migration：genealogy 事實表（維度 + 事件 + trace + 感測特徵）
-- 由 etl/migrate.py 依序套用（記錄在 public.schema_migrations，可重複執行不會重複套用）

CREATE SCHEMA IF NOT EXISTS stg;

-- ─────────────────────────────────────────────────────────────
-- 維度表
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stg.dim_tool (
    tool_id     text PRIMARY KEY,
    source_note text
);

-- 注意：本資料集的 `Lot` 欄位，官方文件寫的是「wafer id」。
-- 真實 fab 的 lot 通常包含 25 片 wafer，階層是 lot → wafer；
-- 但本公開資料沒有 wafer 層級的欄位，因此這裡以「加工單位（process unit）」建模，
-- 並在 source_semantics 欄位記錄這個語意落差（誠實揭露，不可當成真實 lot 使用）。
CREATE TABLE IF NOT EXISTS stg.dim_lot (
    lot_id           text PRIMARY KEY,
    source_semantics text DEFAULT 'PHM 官方定義為 wafer id；本專案視為加工單位',
    n_points         integer,
    first_ts         numeric,
    last_ts          numeric,
    n_events         integer
);
COMMENT ON TABLE stg.dim_lot IS '加工單位維度；source_semantics 記錄 Lot/wafer 語意落差';

CREATE TABLE IF NOT EXISTS stg.dim_recipe (
    recipe_key text PRIMARY KEY          -- recipe（含 recipe_step 的複合鍵由 fact 表承載）
);

CREATE TABLE IF NOT EXISTS stg.dim_step (
    step_key     text PRIMARY KEY,       -- 'recipe=3|recipe_step=3'
    recipe       text,
    recipe_step  text,
    stage_values text[],                 -- 該 step 觀察到的 stage 值（多對多的證據）
    n_stages     integer
);

-- ─────────────────────────────────────────────────────────────
-- 事實表 1：加工事件（genealogy / wafer-step 事件）
-- 事件定義（由 etl/analyze_event_boundaries.py 實證得出）：
--   同一 (Tool, Lot, recipe, recipe_step) 的資料列，若相鄰時間差 > gap 門檻（預設 20 秒）即切成新事件。
--   實測：鍵的變化即為邊界；gap 門檻從 8→300 秒只改變 <0.1% 的事件數。
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stg.fact_process_event (
    event_id        bigint PRIMARY KEY,      -- 依切割順序遞增
    tool_id         text NOT NULL,
    lot_id          text NOT NULL,
    recipe          text NOT NULL,
    recipe_step     text NOT NULL,
    stage_mode      text,                    -- 該事件內出現最多次的 stage
    stage_n_distinct integer,                -- 該事件內出現過的 stage 種類數（應為 1；>1 表示切分有問題）
    runnum_first    text,
    runnum_last     text,
    in_ts           numeric NOT NULL,        -- 原始 time 欄（相對時間，非絕對時鐘）
    out_ts          numeric NOT NULL,
    n_points        integer NOT NULL,
    duration_units  numeric,                 -- out_ts - in_ts（原始時間單位）
    sampling_median numeric,                 -- 事件內相鄰時間差的中位數（應 ≈ 4）
    gap_max         numeric,                 -- 事件內最大相鄰時間差（用來檢查切分品質）
    source_file     text
);
COMMENT ON TABLE stg.fact_process_event IS 'wafer-step 加工事件（genealogy 核心事實表）';
CREATE INDEX IF NOT EXISTS ix_fpe_lot ON stg.fact_process_event (lot_id);
CREATE INDEX IF NOT EXISTS ix_fpe_tool_step ON stg.fact_process_event (tool_id, recipe_step);
CREATE INDEX IF NOT EXISTS ix_fpe_ts ON stg.fact_process_event (in_ts, out_ts);

-- ─────────────────────────────────────────────────────────────
-- 事實表 2：感測器原始時序
-- 設計取捨：1,144,073 列 × 17 個感測器 → 若用長表會是 **1,940 萬列**
--   （體積大、索引成本高、本機資源吃緊），因此存成**寬表**（1 列 = 1 個時間點），
--   需要長表時再透過 stg.v_sensor_trace_long 檢視（unpivot）。
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stg.fact_sensor_trace (
    event_id                bigint NOT NULL,
    ts                      numeric NOT NULL,
    iongaugepressure        double precision,
    etchbeamvoltage         double precision,
    etchbeamcurrent         double precision,
    etchsuppressorvoltage   double precision,
    etchsuppressorcurrent   double precision,
    flowcoolflowrate        double precision,
    flowcoolpressure        double precision,
    etchgaschannel1readback double precision,
    etchpbngasreadback      double precision,
    fixturetiltangle        double precision,
    rotationspeed           double precision,
    actualrotationangle     double precision,
    fixtureshutterposition  double precision,
    etchsourceusage         double precision,
    etchauxsourcetimer      double precision,
    etchaux2sourcetimer     double precision,
    actualstepduration      double precision
);
CREATE INDEX IF NOT EXISTS ix_fst_event ON stg.fact_sensor_trace (event_id);
COMMENT ON TABLE stg.fact_sensor_trace IS '感測器時序寬表（1 列 = 1 時間點；已歸屬到加工事件）。'
    '欄位名必須與 CSV 原始欄名一致（IONGAUGEPRESSURE / ETCHAUXSOURCETIMER / ETCHAUX2SOURCETIMER）；'
    '若資料庫在此修正前建立，跑 003_fix_sensor_column_names.sql 修復';

-- 需要長表（依 sensor 過濾／聚合）時用這個檢視，底層仍是寬表的掃描
CREATE OR REPLACE VIEW stg.v_sensor_trace_long AS
SELECT t.event_id, t.ts, u.sensor, u.value
FROM stg.fact_sensor_trace t,
     LATERAL unnest(
        ARRAY['IONGAUGEPRESSURE','ETCHBEAMVOLTAGE','ETCHBEAMCURRENT','ETCHSUPPRESSORVOLTAGE',
              'ETCHSUPPRESSORCURRENT','FLOWCOOLFLOWRATE','FLOWCOOLPRESSURE','ETCHGASCHANNEL1READBACK',
              'ETCHPBNGASREADBACK','FIXTURETILTANGLE','ROTATIONSPEED','ACTUALROTATIONANGLE',
              'FIXTURESHUTTERPOSITION','ETCHSOURCEUSAGE','ETCHAUXSOURCETIMER','ETCHAUX2SOURCETIMER',
              'ACTUALSTEPDURATION'],
        ARRAY[iongaugpressure, etchbeamvoltage, etchbeamcurrent, etchsuppressorvoltage,
              etchsuppressorcurrent, flowcoolflowrate, flowcoolpressure, etchgaschannel1readback,
              etchpbngasreadback, fixturetiltangle, rotationspeed, actualrotationangle,
              fixtureshutterposition, etchsourceusage, etchauxsourcetimer, etchaux2sourcetimer,
              actualstepduration]
     ) AS u(sensor, value);
COMMENT ON VIEW stg.v_sensor_trace_long IS '長表檢視（unpivot），供依 sensor 過濾的分析使用';

-- ─────────────────────────────────────────────────────────────
-- 事實表 3：每事件 × 每感測器的統計特徵（M3 起 commonality 的輸入）
-- 依 recipe_step 分段聚合——不同製程步驟物理意義不同，不可混在一起平均
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stg.fact_sensor_feature (
    event_id     bigint NOT NULL,
    sensor       text   NOT NULL,
    n            integer,
    missing_rate numeric(6, 4),
    mean_val     double precision,
    std_val      double precision,
    min_val      double precision,
    max_val      double precision,
    range_val    double precision,
    first_val    double precision,
    last_val     double precision,
    slope        double precision,        -- 對時間的線性回歸斜率（漂移方向）
    PRIMARY KEY (event_id, sensor)
);
COMMENT ON TABLE stg.fact_sensor_feature IS '每加工事件×每感測器的統計特徵（含漂移斜率）';

-- ─────────────────────────────────────────────────────────────
-- 品質報表：對齊／切分品質
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS qa.alignment_quality (
    check_name   text PRIMARY KEY,
    metric       double precision,
    unit         text,
    passed       boolean,
    detail       text,
    computed_at  timestamptz DEFAULT now()
);
COMMENT ON TABLE qa.alignment_quality IS '對齊品質檢查結果（覆蓋率、未匹配率、重複匹配率…）';

CREATE TABLE IF NOT EXISTS qa.segmentation_sensitivity (
    candidate_key text,
    gap_threshold integer,
    n_events      bigint,
    median_points double precision,
    p90_points    double precision,
    max_points    bigint,
    PRIMARY KEY (candidate_key, gap_threshold)
);
COMMENT ON TABLE qa.segmentation_sensitivity IS '事件切分對 gap 門檻的敏感度（穩健性證據）';
