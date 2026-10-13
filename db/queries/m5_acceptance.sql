-- M5-1 驗收查詢（單變量 FDC 管制圖）
--
-- ⚠ **每個查詢都綁定 run_id**：同一個資料表會有多個 run（不同基準線模式／不同參數），
--   不綁定的話驗收數字會把不同次的執行混在一起。要換 run 就把下面的 'M5_univ_arl370_b0.3'
--   換成你要看的那個（第 0 段可列出所有 run）。
--
-- 用法：docker compose run --rm etl python run_query.py db/queries/m5_acceptance.sql
--       docker compose run --rm etl python run_query.py db/queries/m5_acceptance.sql --only 2
--
-- 前提：先跑 analysis/run_fdc.py（預設 run_id = M5_univ_arl370_b0.3）

-- 0) 現有的 run 清單（先確認你要看哪一個；括號內是基準線模式）
SELECT run_id AS 執行代號,
       string_agg(DISTINCT baseline_mode, ',') AS 基準線模式,
       count(DISTINCT tool_id) AS 機台數,
       count(*) AS 基準期列數,
       min(created_at) AS 建立時間
FROM mart.fdc_baselines
GROUP BY run_id
ORDER BY 建立時間 DESC;

-- 1) 基準期統計量：每個機台／感測器／方法的 σ 與管制限（可重現「當時用什麼標準」）
SELECT tool_id AS 機台, recipe_step AS 步驟, sensor AS 感測器, method AS 管制圖,
       count(DISTINCT baseline_mode) AS 基準線模式數,
       string_agg(DISTINCT baseline_mode, ',') AS 基準線模式,
       round(avg(slope)::numeric, 6) AS 平均漂移斜率,
       count(*) AS 序列數,
       round(avg(sigma)::numeric, 4) AS 平均σ,
       round(min(param_2)::numeric, 3) AS 最小參數2,
       round(max(param_2)::numeric, 3) AS 最大參數2,
       count(DISTINCT sigma_method) AS σ估法數,
       string_agg(DISTINCT sigma_method, ',') AS σ估法,
       count(DISTINCT baseline_mode) AS 基準線模式數,
       string_agg(DISTINCT baseline_mode, ',') AS 基準線模式,
       round(avg(slope)::numeric, 6) AS 平均漂移斜率
FROM mart.fdc_baselines
WHERE run_id = 'M5_univ_arl370_b0.3'
GROUP BY tool_id, recipe_step, sensor, method
ORDER BY tool_id, sensor, method;

-- 2) 品質檢查：**基準期內不該有警報**（有的話代表基準期本身含異常或 σ 估法有問題）
SELECT tool_id AS 機台, method AS 管制圖,
       count(*) FILTER (WHERE is_baseline) AS 基準期警報列數,
       count(*) AS 總警報列數,
       round(100.0 * count(*) FILTER (WHERE is_baseline) / NULLIF(count(*), 0), 3) AS 基準期佔比百分比
FROM mart.fdc_excursions
WHERE run_id = 'M5_univ_arl370_b0.3'
GROUP BY tool_id, method
ORDER BY 基準期佔比百分比 DESC;

-- 3) 逐機台警報率＋**負控制**（零故障機台的警報率就是誤報率）
--    同時列出「任一圖觸發」與「共識（≥2 個感測器×特徵組合）」兩種口徑
WITH per_event AS (
    SELECT e.tool_id, e.event_id,
           count(DISTINCT (e.sensor, e.feature)) AS n_pairs
    FROM mart.fdc_excursions e
    WHERE NOT e.is_baseline AND e.run_id = 'M5_univ_arl370_b0.3' 
    GROUP BY e.tool_id, e.event_id
), tot AS (
    SELECT ev.tool_id, count(*) AS n_events
    FROM stg.fact_process_event ev
    GROUP BY ev.tool_id
), faults AS (
    SELECT tool_id, count(*) AS n_faults FROM mart.fault_events GROUP BY tool_id
)
SELECT t.tool_id AS 機台, t.n_events AS 事件數,
       coalesce(f.n_faults, 0) AS 故障次數,
       count(pe.event_id) AS 警報事件_任一圖,
       round(100.0 * count(pe.event_id) / t.n_events, 2) AS 警報率_任一圖百分比,
       count(*) FILTER (WHERE pe.n_pairs >= 2) AS 警報事件_共識,
       round(100.0 * count(*) FILTER (WHERE pe.n_pairs >= 2) / t.n_events, 2) AS 警報率_共識百分比,
       CASE WHEN coalesce(f.n_faults, 0) = 0 THEN '負控制（零故障）' ELSE '' END AS 備註
FROM tot t
LEFT JOIN faults f ON f.tool_id = t.tool_id
LEFT JOIN per_event pe ON pe.tool_id = t.tool_id
GROUP BY t.tool_id, t.n_events, f.n_faults
ORDER BY 警報率_共識百分比 DESC NULLS LAST;

-- 4) 故障窗內偵測率（窗 24h，與 M3 一致；口徑＝共識）
WITH per_event AS (
    SELECT tool_id, event_id, count(DISTINCT (sensor, feature)) AS n_pairs
    FROM mart.fdc_excursions WHERE NOT is_baseline
      AND run_id = 'M5_univ_arl370_b0.3' GROUP BY tool_id, event_id
)
SELECT l.tool_id AS 機台,
       count(*) AS 窗內事件數,
       count(pe.event_id) FILTER (WHERE pe.n_pairs >= 1) AS 偵測到_任一圖,
       count(pe.event_id) FILTER (WHERE pe.n_pairs >= 2) AS 偵測到_共識,
       round(100.0 * count(pe.event_id) FILTER (WHERE pe.n_pairs >= 1) / NULLIF(count(*), 0), 2)
           AS 偵測率_任一圖百分比,
       round(100.0 * count(pe.event_id) FILTER (WHERE pe.n_pairs >= 2) / NULLIF(count(*), 0), 2)
           AS 偵測率_共識百分比
FROM mart.event_labels l
LEFT JOIN per_event pe ON pe.event_id = l.event_id
WHERE l.fault_type = 'ANY' AND l.window_hours = 24 AND l.in_window
GROUP BY l.tool_id
ORDER BY l.tool_id;

-- 5) **領先時間**：每次故障之前，該機台最早與最晚的共識警報距故障多久（小時）
WITH cons AS (
    SELECT e.tool_id, e.event_id, min(e.in_ts) AS alert_ts
    FROM mart.fdc_excursions e
    WHERE NOT e.is_baseline AND e.run_id = 'M5_univ_arl370_b0.3' 
    GROUP BY e.tool_id, e.event_id
    HAVING count(DISTINCT (e.sensor, e.feature)) >= 2
)
SELECT f.tool_id AS 機台, f.fault_type AS 故障型別,
       round(f.fault_ts::numeric, 1) AS 故障時刻,
       count(c.event_id) AS 故障前共識警報數,
       round(min((f.fault_ts - c.alert_ts) / 3600.0)::numeric, 2) AS 最早領先小時,
       round(max((f.fault_ts - c.alert_ts) / 3600.0)::numeric, 2) AS 最晚領先小時
FROM mart.fault_events f
LEFT JOIN cons c ON c.tool_id = f.tool_id AND c.alert_ts <= f.fault_ts
GROUP BY f.tool_id, f.fault_type, f.fault_ts
ORDER BY f.tool_id, f.fault_ts;

-- 6) 每台機台最早被抓到的時間（相對第一次故障）
SELECT f.tool_id AS 機台,
       min(f.fault_ts) AS 第一次故障,
       (SELECT min(c.in_ts) FROM mart.fdc_excursions c
         WHERE c.tool_id = f.tool_id AND NOT c.is_baseline
           AND c.run_id = 'M5_univ_arl370_b0.3' 
           AND c.in_ts <= min(f.fault_ts)) AS 最早警報時刻,
       round(((min(f.fault_ts) - (SELECT min(c.in_ts) FROM mart.fdc_excursions c
                WHERE c.tool_id = f.tool_id AND NOT c.is_baseline
                  AND c.run_id = 'M5_univ_arl370_b0.3' 
                  AND c.in_ts <= min(f.fault_ts))) / 3600.0)::numeric, 2) AS 領先小時
FROM mart.fault_events f
GROUP BY f.tool_id
ORDER BY 領先小時 DESC NULLS LAST;

-- 7) 觸發最多的感測器 × 特徵（哪個物理量最有診斷價值 → 給 M5-2／面試用）
SELECT sensor AS 感測器, feature AS 特徵, method AS 管制圖,
       count(*) AS 警報列數,
       count(DISTINCT event_id) AS 涉及事件數,
       round(100.0 * count(DISTINCT event_id) /
             (SELECT count(*) FROM stg.fact_process_event), 2) AS 事件覆蓋率百分比
FROM mart.fdc_excursions
WHERE NOT is_baseline AND method = 'shewhart' AND run_id = 'M5_univ_arl370_b0.3'
GROUP BY sensor, feature, method
ORDER BY 警報列數 DESC
LIMIT 20;

-- 8) 參數一致性檢查（同一 run_id 內不該有多組參數）
SELECT run_id, baseline_mode, method, param_1, param_2, arl0_target, sigma_method,
       count(*) AS 序列數
FROM mart.fdc_baselines
WHERE run_id = 'M5_univ_arl370_b0.3'
GROUP BY run_id, baseline_mode, method, param_1, param_2, arl0_target, sigma_method
ORDER BY run_id, method, 序列數 DESC;
