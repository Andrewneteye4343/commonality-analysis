-- M2 驗收查詢（在 db 容器內執行）
--   docker compose exec db psql -U ca -d commonality -c "<單行 SQL>"

-- 1) 事實表筆數總覽（驗收：事件 29,002、trace 1,144,073、特徵 493,034）
SELECT 'dim_tool' AS t, count(*) FROM stg.dim_tool
UNION ALL SELECT 'dim_lot', count(*) FROM stg.dim_lot
UNION ALL SELECT 'dim_recipe', count(*) FROM stg.dim_recipe
UNION ALL SELECT 'dim_step', count(*) FROM stg.dim_step
UNION ALL SELECT 'fact_process_event', count(*) FROM stg.fact_process_event
UNION ALL SELECT 'fact_sensor_trace', count(*) FROM stg.fact_sensor_trace
UNION ALL SELECT 'fact_sensor_feature', count(*) FROM stg.fact_sensor_feature
ORDER BY 1;

-- 2) 守恆性：事件內時間點合計 == trace 列數（兩者必須相等）
SELECT (SELECT sum(n_points) FROM stg.fact_process_event) AS points_in_events,
       (SELECT count(*) FROM stg.fact_sensor_trace)       AS trace_rows,
       (SELECT sum(n_points) FROM stg.fact_process_event) = (SELECT count(*) FROM stg.fact_sensor_trace) AS conserved;

-- 3) 事件大小分布（median 23、max 3589）
SELECT min(n_points) AS min_pts, percentile_cont(0.5) WITHIN GROUP (ORDER BY n_points) AS median_pts,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY n_points) AS p90_pts, max(n_points) AS max_pts
FROM stg.fact_process_event;

-- 4) 切分品質：單一事件內 stage 是否唯一、事件內最大相鄰時間差是否在門檻內
SELECT count(*) FILTER (WHERE stage_n_distinct > 1) AS events_multi_stage,
       count(*) FILTER (WHERE gap_max > 20)         AS events_gap_too_large,
       count(*)                                     AS total_events
FROM stg.fact_process_event;

-- 5) 對齊品質檢查結果（由 python align_qc.py --db 寫入）
SELECT check_name, metric, unit, passed, detail FROM qa.alignment_quality ORDER BY check_name;

-- 6) 抽一個事件，看它的感測特徵（M3 的輸入長相）
SELECT sensor, n, round(mean_val::numeric, 4) AS mean, round(std_val::numeric, 4) AS std,
       round(slope::numeric, 6) AS slope, missing_rate
FROM stg.fact_sensor_feature WHERE event_id = 1 ORDER BY sensor LIMIT 8;

-- 7) 抽一個事件的原始時序（長表檢視，驗證 unpivot 可用）
SELECT ts, sensor, value FROM stg.v_sensor_trace_long
WHERE event_id = 1 AND sensor = 'FLOWCOOLPRESSURE' ORDER BY ts LIMIT 5;

-- 8) 每個 recipe_step 的事件數與感測特徵變異（M3 commonality 的第一步）
SELECT recipe_step, count(*) AS events, count(DISTINCT lot_id) AS lots,
       round(avg(n_points), 1) AS avg_points
FROM stg.fact_process_event GROUP BY recipe_step ORDER BY events DESC LIMIT 10;

-- 9) 加工單位（lot）的事件數分布（commonality 的分母基礎）
SELECT count(*) AS n_lots, min(n_events) AS min_e, percentile_cont(0.5) WITHIN GROUP (ORDER BY n_events) AS median_e,
       max(n_events) AS max_e
FROM stg.dim_lot;
