-- ═══════════════════════════════════════════════════════════════════════════
-- 004：M3 共同性分析（commonality analysis）的資料模型
--
-- 資料流：
--   groundtruth（TTF）─→ mart.fault_events（故障時刻）
--   fact_process_event ─→ mart.event_labels（事件×故障類型：距下次故障幾小時、是否在退化窗內）
--   事件×故障標籤 ─→ mart.lot_units（**分析單位 = 批次**，不是事件）
--   事件 ─→ mart.lot_membership（批次屬於哪些群組：機台/recipe/步驟/stage）
--   mart.lot_units × mart.lot_membership ─→ mart.commonality_results（列聯表 + 檢定 + 校正）
--   合成注入實驗 ─→ mart.method_eval（偵測率 / 假陽性率）
--
-- 為什麼分析單位是「批次」而不是「事件」：
--   同一 lot 內的事件彼此不獨立（同批、同時、同一片晶圓），把 2.9 萬個事件當獨立樣本
--   會產生偽重複（pseudo-replication），p 值會小到毫無統計意義。
-- ═══════════════════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS mart;

-- ─────────────────────────────────────────────────────────────
-- 事實：故障事件（由 TTF 反推）
-- TTF（Time To Failure）在每個時間點記錄「距下一次該類故障還有幾秒」，
-- 因此 t + TTF ≈ 常數 = 故障時刻；用這個不變量把故障時刻估出來，
-- 並且能區分「同一類故障發生多次」（t+TTF 會出現多個聚類）。
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mart.fault_events (
    tool_id        text   NOT NULL,
    fault_type     text   NOT NULL,           -- 原始欄名（去掉 TTF_ 前綴）
    fault_ts       numeric NOT NULL,          -- 估計故障時刻（原始時間軸單位）
    n_obs          integer NOT NULL,          -- 支持這個估計的觀測列數
    ttf_min_s      numeric,                   -- 該聚類中最小 TTF（秒）
    spread_s       numeric,                   -- t+TTF 的 p95−p05（估計不確定度）
    ttf_source     text,                      -- groundtruth 檔名
    detector       text DEFAULT 'ttf_invariant',
    PRIMARY KEY (tool_id, fault_type, fault_ts)
);
COMMENT ON TABLE mart.fault_events IS '由 groundtruth 的 t+TTF 不變量反推的故障時刻（公開資料、已匿名化）';

-- ─────────────────────────────────────────────────────────────
-- 事實：事件 × 故障類型 → 距下次故障的時間與是否落在退化窗內
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mart.event_labels (
    event_id          bigint NOT NULL,
    tool_id           text   NOT NULL,
    fault_type        text   NOT NULL,        -- 'ANY' = 任一故障類型
    window_hours      integer NOT NULL,       -- 退化窗長度（小時）
    hours_to_fault    numeric,                -- 距下一次故障幾小時（NULL = 之後沒有故障了）
    in_window         boolean NOT NULL,       -- 0 ≤ hours_to_fault ≤ window_hours
    n_faults_ahead    integer,                -- 之後還有幾次同類故障
    PRIMARY KEY (event_id, fault_type, window_hours)
);
CREATE INDEX IF NOT EXISTS ix_mel_win ON mart.event_labels (fault_type, window_hours, in_window);
COMMENT ON TABLE mart.event_labels IS '加工事件的故障鄰近度標籤（壞批次的定義來源）';

-- ─────────────────────────────────────────────────────────────
-- 分析單位：批次（lot）
-- outcome_bad = 該批次有任一事件落在「故障前 window_hours 小時」內
-- （物理意義：機台已開始退化時處理的批次，就是可疑批次）
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mart.lot_units (
    lot_id        text    NOT NULL,
    tool_id       text    NOT NULL,
    fault_type    text    NOT NULL,           -- 'ANY' 或特定故障類型
    window_hours  integer NOT NULL,
    outcome_bad   boolean NOT NULL,
    n_events      integer NOT NULL,
    n_events_in_window integer NOT NULL,
    first_ts      numeric,
    last_ts       numeric,
    PRIMARY KEY (lot_id, fault_type, window_hours)
);
CREATE INDEX IF NOT EXISTS ix_mlu_win ON mart.lot_units (fault_type, window_hours, outcome_bad);
COMMENT ON TABLE mart.lot_units IS '共同性分析的分析單位＝批次；不是事件（避免偽重複）';

-- ─────────────────────────────────────────────────────────────
-- 群組成員：批次屬於哪些群組（一個批次可屬多個群組）
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mart.lot_membership (
    lot_id      text NOT NULL,
    group_type  text NOT NULL,                -- tool / recipe / recipe_step / stage / recipe_step_x_stage
    group_key   text NOT NULL,
    n_events    integer NOT NULL,
    PRIMARY KEY (lot_id, group_type, group_key)
);
CREATE INDEX IF NOT EXISTS ix_mlm_group ON mart.lot_membership (group_type, group_key);
COMMENT ON TABLE mart.lot_membership IS '批次與群組（機台/recipe/步驟…）的對應，供列聯表統計';

-- ─────────────────────────────────────────────────────────────
-- 結果：每群組的 2×2 列聯表 + 檢定 + 多重比較校正
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mart.commonality_results (
    analysis_id       text NOT NULL,
    group_type        text NOT NULL,
    group_key         text NOT NULL,
    n_total           integer,               -- 總批次數 N
    n_bad_total       integer,               -- 壞批次總數 K
    n_group           integer,               -- 群組處理的批次數 n
    k_bad             integer,               -- 群組內的壞批次數 k
    a                 integer, b integer, c integer, d integer,
    p_bad_in_group    double precision,
    p_bad_outside     double precision,
    odds_ratio        double precision,
    or_ci_low         double precision,
    or_ci_high        double precision,
    or_method         text,
    ci_method         text,
    p_hyper           double precision,      -- 單尾超幾何（= 單尾 Fisher greater）
    p_fisher_two_sided double precision,
    p_bonferroni      double precision,
    q_bh              double precision,      -- BH-FDR q 值
    rank_p            integer,               -- 依 p_hyper 排名
    rank_bh           integer,               -- 依 q_bh 排名
    testable          boolean,               -- 是否通過支持度門檻
    flag              text,                  -- 未通過的原因
    created_at        timestamptz DEFAULT now(),
    PRIMARY KEY (analysis_id, group_type, group_key)
);
COMMENT ON TABLE mart.commonality_results IS '共同性分析排名結果（含校正前後 p 值與效應量）';

-- ─────────────────────────────────────────────────────────────
-- 方法評估：用合成注入的 ground truth 量測偵測率與假陽性率
-- （這是本專案「量化方法效能」的核心，缺負控制的分析在面試裡站不住）
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mart.method_eval (
    run_id             text NOT NULL,
    scenario           text NOT NULL,        -- null（無注入）/ injected（注入根因）
    group_type         text NOT NULL,
    target_group       text,                 -- 注入的群組（null 情境為 NULL）
    effect_ratio       double precision,     -- 注入的壞率倍數
    n_replicates       integer NOT NULL,
    detection_top1     integer,              -- 注入根因排第 1 的次數
    detection_top3     integer,
    mean_rank          double precision,
    n_sig_raw_mean     double precision,     -- 未校正顯著個數（平均）
    n_sig_bh_mean      double precision,     -- BH 校正後顯著個數（平均）
    fpr_bh_mean        double precision,     -- 偽發現率（平均）
    seed               integer,
    detail             text,
    created_at         timestamptz DEFAULT now(),
    PRIMARY KEY (run_id, scenario, group_type, target_group)
);
COMMENT ON TABLE mart.method_eval IS '合成注入實驗：偵測率（正控制）與偽發現率（負控制）';

-- ─────────────────────────────────────────────────────────────
-- 檢視：批次標籤總覽（快速確認標籤比例是否合理）
-- ─────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW mart.v_lot_outcome_summary AS
SELECT tool_id, fault_type, window_hours,
       count(*)                                    AS n_lots,
       count(*) FILTER (WHERE outcome_bad)         AS n_bad_lots,
       round(100.0 * count(*) FILTER (WHERE outcome_bad) / nullif(count(*), 0), 3) AS pct_bad,
       sum(n_events)                               AS n_events
FROM mart.lot_units
GROUP BY 1, 2, 3;
COMMENT ON VIEW mart.v_lot_outcome_summary IS '批次層級的壞批次比例（檢查標籤是否合理的第一站）';
