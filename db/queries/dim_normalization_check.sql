-- D 項驗證：維度鍵值是否已統一寫法
-- 用法：docker compose run --rm etl python run_query.py db/queries/dim_normalization_check.sql

-- 1) 逐機台的 recipe_step 值（正規化後每台應該只有整數字串，沒有 '1.0'）
SELECT tool_id AS 機台,
       count(DISTINCT recipe_step) AS 步驟數,
       string_agg(DISTINCT recipe_step, ',' ORDER BY recipe_step) AS 步驟值
FROM stg.fact_process_event
GROUP BY tool_id
ORDER BY tool_id;

-- 2) 仍帶小數（.0）的殘留值：套用 migration 009 後必須是 0 列
SELECT count(*) AS 未正規化的列數
FROM stg.fact_process_event
WHERE recipe_step ~ '^-?\d+\.0+$' OR recipe ~ '^-?\d+\.0+$';

-- 3) 同一批次是否還被拆成兩個步驟鍵（正規化前的症狀）
SELECT lot_id AS 批次, recipe AS 配方,
       count(DISTINCT recipe_step) AS 不同步驟寫法數
FROM stg.fact_process_event
GROUP BY lot_id, recipe
HAVING count(DISTINCT recipe_step) > 1 AND count(DISTINCT recipe_step) < 20
ORDER BY 不同步驟寫法數 DESC
LIMIT 10;

-- 4) lot_membership 是否已重建（重跑 load_fault_labels.py 後，'recipe_step' 維度的鍵不該有小數）
SELECT group_type AS 維度, count(*) AS 成員列數,
       count(*) FILTER (WHERE group_key ~ '\.0') AS 含小數的鍵數
FROM mart.lot_membership
GROUP BY group_type
ORDER BY 維度;
