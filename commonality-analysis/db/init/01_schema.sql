-- M1 資料庫初始化：schema 分層 + 資料血緣 + SECOM 原始層
-- 這個檔案在 postgres 容器「第一次建立資料庫」時自動執行（/docker-entrypoint-initdb.d）
-- 之後要重跑：docker compose down -v 再 up（會清空資料），或手動 psql -f

CREATE SCHEMA IF NOT EXISTS raw;    -- 原始層：原樣落地，不做商業邏輯
CREATE SCHEMA IF NOT EXISTS stg;    -- 標準化層：對齊後的事實表（M2 起）
CREATE SCHEMA IF NOT EXISTS mart;   -- 分析層：commonality 排名、預測結果（M3 起）
CREATE SCHEMA IF NOT EXISTS qa;     -- 資料品質：覆蓋率、缺失率、對齊報表（M2 起）

COMMENT ON SCHEMA raw  IS '原始資料層（landing）';
COMMENT ON SCHEMA stg  IS '標準化／對齊層：genealogy 事實表';
COMMENT ON SCHEMA mart IS '分析結果層：commonality 排名、FDC、預測輸出';
COMMENT ON SCHEMA qa   IS '資料品質報表';

-- ─────────────────────────────────────────────────────────────
-- 資料血緣（data lineage）：每個檔案從哪來、多大、checksum、載入幾筆
-- 目的：任何分析結果都能往上追溯到「哪個版本、哪一天下載的檔案」
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS raw.data_catalog (
    file_key      text PRIMARY KEY,
    path          text,
    source_url    text,
    description   text,
    bytes         bigint,
    sha256        text,
    downloaded_at timestamptz,
    loaded_at     timestamptz DEFAULT now(),
    row_count     bigint,
    notes         text
);
COMMENT ON TABLE raw.data_catalog IS '資料檔案血緣與完整性紀錄（由 etl 腳本寫入）';

-- ─────────────────────────────────────────────────────────────
-- SECOM：原始層
--   labels：1567 筆，-1 = pass、1 = fail，附時間戳（來源格式 dd/mm/yyyy hh:mm:ss）
--   features：以長表（sample_id × feature_id）保存，欄位匿名（f0001..f0590）
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS raw.secom_labels (
    sample_id   integer PRIMARY KEY,
    label       smallint NOT NULL CHECK (label IN (-1, 1)),
    measured_at timestamp,
    source_ts   text
);
COMMENT ON TABLE raw.secom_labels IS 'SECOM 標籤：-1=pass、1=fail（104/1567 為 fail）';

CREATE TABLE IF NOT EXISTS raw.secom_features (
    sample_id  integer NOT NULL,
    feature_id text    NOT NULL,
    value      double precision,
    PRIMARY KEY (sample_id, feature_id)
);
COMMENT ON TABLE raw.secom_features IS 'SECOM 製程感測特徵（長表；欄位匿名，NaN 以 NULL 表示）';

CREATE INDEX IF NOT EXISTS ix_secom_features_feature ON raw.secom_features (feature_id);

-- 寬表（f0001..f0590）由 etl/load_secom.py 依實際欄數動態建立於 stg.secom_wide

-- ─────────────────────────────────────────────────────────────
-- 資料品質：SECOM 每欄缺失率與分布（缺失值本身就是資訊，不能直接丟）
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS qa.secom_column_profile (
    feature_id   text PRIMARY KEY,
    n_total      integer,
    n_missing    integer,
    missing_rate numeric(6, 4),
    n_unique     integer,
    min_val      double precision,
    max_val      double precision,
    mean_val     double precision,
    std_val      double precision,
    loaded_at    timestamptz DEFAULT now()
);
COMMENT ON TABLE qa.secom_column_profile IS 'SECOM 各欄位品質剖面（缺失率為後續特徵篩選依據）';

-- ─────────────────────────────────────────────────────────────
-- 便利檢視：標籤分布
-- ─────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW qa.secom_label_distribution AS
SELECT label,
       CASE WHEN label = -1 THEN 'pass' ELSE 'fail' END AS label_name,
       count(*) AS n,
       round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct
FROM raw.secom_labels
GROUP BY label
ORDER BY label;
