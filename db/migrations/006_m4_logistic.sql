-- 006_m4_logistic.sql｜M4-2：多變數模型（logistic regression）的結果表
--
-- 為什麼要新表而不塞進 mart.commonality_results
-- ────────────────────────────────────────────────────────────
-- commonality_results 是「單變數 2×2 + 超幾何」的結構（a,b,c,d、p_hyper、odds_ratio）。
-- logistic regression 的輸出是係數/標準誤/信賴區間，欄位語意不同；硬塞會讓兩個方法的
-- 欄位互相誤用（例如把 p_wald 當 p_hyper 用）。分表能讓 M3 與 M4 的結果並列比較。

CREATE TABLE IF NOT EXISTS mart.logistic_results (
    run_id            text        NOT NULL,   -- 例如 M4_route_tool_w24h_r1.0
    group_type        text        NOT NULL,   -- tool / recipe / stage / …
    group_key         text        NOT NULL,
    method            text        NOT NULL,   -- 模型名稱：logit / cloglog / hazard_sum
    effect_type       text        NOT NULL,   -- OR（勝算比）或 HR（風險率）
    model_note        text        NOT NULL,   -- 模型設定（ridge、控制項、群聚變數）
    n_obs             integer,                -- 參與擬合的批次數
    n_events          integer,                -- 涵蓋的加工事件數（僅供追溯）
    n_group           integer,                -- 該群組的批次數（n）
    k_bad             integer,                -- 該群組的壞批次數（k）
    beta              double precision,       -- 對數勝算係數
    se                double precision,       -- naive 標準誤
    se_cluster        double precision,       -- 群聚穩健標準誤（若有分群）
    or_               double precision,       -- exp(beta)
    or_ci_low         double precision,
    or_ci_high        double precision,
    p_wald            double precision,       -- Wald 檢定 p 值（用群聚穩健標準誤）
    q_bh              double precision,       -- BH-FDR 校正後
    rank_bh           integer,
    raw_bad_rate      double precision,       -- 原始壞率 k/n（未校正）
    expected_bad_rate double precision,       -- expit(beta)：模型推得「路線只有該群組」的壞率
    excess_bad_rate   double precision,       -- raw − expected：可歸因於路線共用的差距
    ridge             double precision,
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, group_type, group_key, method)
);

COMMENT ON COLUMN mart.logistic_results.method IS
    '以「批次經過哪些機台」為共變數的多變數模型（無截距）：logit / cloglog / hazard_sum；後者為風險率相加，對應「曾落在任一機台故障窗內」的機制';
COMMENT ON COLUMN mart.logistic_results.expected_bad_rate IS
    'expit(beta)：模型推得「只經過這個群組」時的壞率，用來與原始壞率對照，量化路線共用的污染';
COMMENT ON COLUMN mart.logistic_results.excess_bad_rate IS
    'raw_bad_rate − expected_bad_rate：正值代表原始壞率被其他共線群組（例如共用機台）拉高；值越接近 raw_bad_rate 代表該群組的原始風險幾乎全來自路線共用';

CREATE INDEX IF NOT EXISTS idx_logistic_results_run ON mart.logistic_results (run_id, group_type);

-- 結尾自檢：欄位必須存在（若這行失敗代表 migration 沒套用成功）
DO $$
DECLARE missing text;
BEGIN
    SELECT string_agg(c, ', ') INTO missing
    FROM unnest(ARRAY['run_id','group_type','group_key','method','effect_type','model_note','n_obs','n_events',
                      'n_group','k_bad','beta','se','se_cluster','or_','or_ci_low','or_ci_high',
                      'p_wald','q_bh','rank_bh','raw_bad_rate','expected_bad_rate','excess_bad_rate',
                      'ridge','created_at']) AS c
    WHERE NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'mart' AND table_name = 'logistic_results' AND column_name = c);
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'mart.logistic_results 缺少欄位：%', missing;
    END IF;
    RAISE NOTICE 'mart.logistic_results 檢查通過（24 欄）';
END $$;
