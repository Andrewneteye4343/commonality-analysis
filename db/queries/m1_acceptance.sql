-- M1 驗收查詢：在 db 容器內執行
--   docker compose exec db psql -U ca -d commonality -f /docker-entrypoint-initdb.d/../queries/m1_acceptance.sql
-- 或逐段複製貼上：
--   docker compose exec db psql -U ca -d commonality -c "SELECT ..."

-- 1) 資料血緣：檔案、大小、checksum、筆數
SELECT file_key, pg_size_pretty(bytes) AS size, left(sha256, 16) AS sha256_head,
       row_count, downloaded_at
FROM raw.data_catalog
ORDER BY file_key;

-- 2) SECOM 標籤分布（驗收：pass 1463 / fail 104）
SELECT label, label_name, n, pct FROM qa.secom_label_distribution;

-- 3) SECOM 特徵筆數（驗收：1567 列 × 590 欄 = 924,530 筆有效值；缺失值不列入長表）
SELECT count(*) AS n_rows,
       count(DISTINCT sample_id) AS n_samples,
       count(DISTINCT feature_id) AS n_features,
       count(value) AS n_values,
       sum((value IS NULL)::int) AS n_nulls
FROM raw.secom_features;

-- 4) 寬表欄數（驗收：591 欄 = sample_id + 590 特徵）
SELECT count(*) AS n_columns
FROM information_schema.columns
WHERE table_schema = 'stg' AND table_name = 'secom_wide';

-- 5) 缺失率最高的 10 欄（資料品質）
SELECT feature_id, n_total, n_missing, missing_rate
FROM qa.secom_column_profile
ORDER BY missing_rate DESC
LIMIT 10;

-- 6) 標籤與樣本編號對應檢查（防止對齊錯誤：兩表 sample_id 必須完全一致）
SELECT (SELECT count(*) FROM raw.secom_labels) AS n_labels,
       (SELECT count(*) FROM (SELECT DISTINCT sample_id FROM raw.secom_features) t) AS n_samples_in_features,
       (SELECT count(*) FROM (
            SELECT sample_id FROM raw.secom_labels
            EXCEPT SELECT sample_id FROM raw.secom_features) t) AS labels_without_features;
