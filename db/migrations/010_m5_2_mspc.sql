-- M5-2-1：多變量統計製程管制（MSPC）的模型與警報
--
-- 逐 (機台 × recipe_step) 建模（M5-2-0 實測：步驟解釋 86.3% 的變異）。
-- 兩個統計量：T²（主成分空間）與 SPE/Q（殘差空間）；管制線是**基準期經驗分位數**，
-- 因為真實資料的尾部是理論分布的 11.5–15.5 倍（理論線會給 5–7% 誤報率）。
-- 同時保存 robust（迭代修剪共變異數）與 plain 兩版，方便直接比較。

CREATE TABLE IF NOT EXISTS mart.mspc_models (
    run_id              text NOT NULL,
    tool_id             text NOT NULL,
    recipe_step         text NOT NULL,
    robust              boolean NOT NULL,
    n_vars              integer,
    n_base_complete     integer,
    n_monitor           integer,
    pca_k               integer,
    var_explained_actual double precision,
    cond_number         double precision,
    lim_t2              double precision,
    lim_spe             double precision,
    base_alarm_t2       double precision,
    base_alarm_spe      double precision,
    imputed_rate        double precision,
    dropped_vars        integer,
    created_at          timestamptz DEFAULT now(),
    PRIMARY KEY (run_id, tool_id, recipe_step, robust)
);

CREATE INDEX IF NOT EXISTS ix_mspc_models_run ON mart.mspc_models (run_id, robust);

CREATE TABLE IF NOT EXISTS mart.mspc_alerts (
    run_id        text NOT NULL,
    tool_id       text NOT NULL,
    recipe_step   text NOT NULL,
    robust        boolean NOT NULL,
    event_id      bigint NOT NULL,
    in_ts         numeric,                 -- ⚠ 與 stg.fact_process_event.in_ts 同型別（numeric）
    stat          text NOT NULL,           -- 't2' | 'spe'
    value         double precision,
    limit_value   double precision,
    top1          text, top1_contrib double precision,
    top2          text, top2_contrib double precision,
    top3          text, top3_contrib double precision,
    PRIMARY KEY (run_id, tool_id, recipe_step, robust, event_id, stat)
);

CREATE INDEX IF NOT EXISTS ix_mspc_alerts_run ON mart.mspc_alerts (run_id, robust, event_id);

-- 事件層視圖：同一事件只要有一個統計量越界就算警報（供驗收查詢使用）
CREATE OR REPLACE VIEW mart.v_mspc_event_alerts AS
SELECT run_id, tool_id, recipe_step, robust, event_id,
       min(in_ts)                        AS in_ts,
       bool_or(stat = 't2')              AS alert_t2,
       bool_or(stat = 'spe')             AS alert_spe,
       max(value / nullif(limit_value, 0)) AS worst_ratio
FROM mart.mspc_alerts
GROUP BY run_id, tool_id, recipe_step, robust, event_id;
