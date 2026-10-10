-- M3 驗收查詢（共同性分析）
--   docker compose exec db psql -U ca -d commonality -c "<單行 SQL>"

-- 1) 故障反推結果（由 TTF 不變量得到；單機台 03M01 應為 12 次：8+3+1）
SELECT tool_id, fault_type, count(*) AS n_faults, min(fault_ts) AS first_fault, max(fault_ts) AS last_fault
FROM mart.fault_events GROUP BY 1, 2 ORDER BY 1, 2;

-- 2) 批次層級標籤（驗收：03M01 / ANY / 24 小時 = 216 / 3,081 = 7.01%）
SELECT * FROM mart.v_lot_outcome_summary ORDER BY tool_id, fault_type, window_hours;

-- 3) 群組成員數（單機台應為：tool 1、recipe 31、recipe_step 34、stage 110）
SELECT group_type, count(DISTINCT group_key) AS n_groups, count(*) AS n_membership_rows
FROM mart.lot_membership GROUP BY 1 ORDER BY 1;

-- 4) 共同性排名前 15（依 BH 校正後 q 值）
SELECT group_type, group_key, n_group, k_bad, round(p_bad_in_group::numeric, 3) AS p_in,
       round(odds_ratio::numeric, 2) AS or_, p_hyper, q_bh, rank_bh
FROM mart.commonality_results
WHERE analysis_id = 'M3_ANY_w24h' AND testable
ORDER BY q_bh LIMIT 15;

-- 5) 校正前後對照（未校正顯著 59、巧合期望 9.7、Bonferroni 2、BH 29）
SELECT count(*) FILTER (WHERE testable)                                  AS n_tests,
       count(*) FILTER (WHERE testable AND p_hyper < 0.05)               AS n_sig_raw,
       count(*) FILTER (WHERE testable AND p_bonferroni < 0.05)          AS n_sig_bonferroni,
       count(*) FILTER (WHERE testable AND q_bh < 0.05)                  AS n_sig_bh,
       round(count(*) FILTER (WHERE testable) * 0.05, 1)                 AS expected_false_positives
FROM mart.commonality_results WHERE analysis_id = 'M3_ANY_w24h';

-- 6) 方法評估（負控制 rows 的 fpr_bh_mean 應遠小於 0.05；正控制 rows 的 detection_top3 應高）
SELECT scenario, group_type, target_group, effect_ratio, detection_top1, detection_top3,
       round(mean_rank::numeric, 1) AS mean_rank, round(n_sig_bh_mean::numeric, 2) AS n_sig_bh,
       round(fpr_bh_mean::numeric, 4) AS fpr
FROM mart.method_eval ORDER BY group_type, scenario;

-- 7) 跨機台（載入多台之後）：哪台機台在「可疑批次」中過度集中
--    驗收：有故障的機台（01M02/02M02/03M01/06M01）應排在前面，完全無故障的 04M01 不得進前 3
SELECT group_key AS tool, n_group, k_bad, round(p_bad_in_group::numeric, 4) AS p_in,
       round(odds_ratio::numeric, 2) AS or_, p_hyper, q_bh, rank_bh
FROM mart.commonality_results
WHERE analysis_id LIKE 'M3_ANY_w24h%' AND group_type = 'tool' AND testable
ORDER BY p_hyper;

-- 8) 共用機台檢查：被多台機台加工過的批次數（跨機台分析要處理的混淆來源）
SELECT count(*) AS lots_touched_by_multiple_tools FROM (
  SELECT lot_id FROM mart.lot_membership WHERE group_type = 'tool'
  GROUP BY lot_id HAVING count(*) > 1) t;

-- 9) 每台機台的事件數與壞批次數（確認多機台載入成功）
SELECT l.tool_id, count(*) AS n_lots, count(*) FILTER (WHERE l.outcome_bad) AS n_bad_lots,
       sum(l.n_events) AS n_events
FROM mart.lot_units l WHERE l.fault_type = 'ANY' AND l.window_hours = 24
GROUP BY 1 ORDER BY 2 DESC;

-- 10) 【決定性檢查】那 3,952 個「共用批次」是真的同一批 lot，還是 ID 不具全域唯一性？
--     做法：把每個 (機台, 批次) 的加工時間範圍拉出來，看跨機台的時間是否**重疊**。
--     真實的共用：同一批在 A 機台做完才到 B（時間先後不重疊）
--     ID 不唯一：兩台機台的同號批次時間會互相重疊（或根本無關）→ 此時共用機台的分析不可信
WITH s AS (
  SELECT tool_id, lot_id, min(in_ts) AS f, max(out_ts) AS l
  FROM stg.fact_process_event GROUP BY 1, 2)
SELECT count(*) AS shared_pairs,
       count(*) FILTER (WHERE NOT (a.l < b.f OR b.l < a.f)) AS time_overlapping_pairs,
       round(100.0 * count(*) FILTER (WHERE NOT (a.l < b.f OR b.l < a.f)) / nullif(count(*), 0), 2) AS pct_overlap
FROM s a JOIN s b ON a.lot_id = b.lot_id AND a.tool_id < b.tool_id;

-- 11) 看幾個共用批次的實際時間（抽樣判讀）
WITH s AS (
  SELECT tool_id, lot_id, min(in_ts) AS f, max(out_ts) AS l
  FROM stg.fact_process_event GROUP BY 1, 2)
SELECT a.lot_id, a.tool_id AS tool_a, a.f AS a_in, a.l AS a_out,
       b.tool_id AS tool_b, b.f AS b_in, b.l AS b_out,
       (NOT (a.l < b.f OR b.l < a.f)) AS time_overlap
FROM s a JOIN s b ON a.lot_id = b.lot_id AND a.tool_id < b.tool_id
ORDER BY a.lot_id LIMIT 10;

-- 12) 機台層級共同性（M3 驗收核心）：有故障機台應靠前、完全無故障的 04M01 不應進前 3
SELECT group_key AS tool, n_group AS n_lots, k_bad, round(p_bad_in_group::numeric, 4) AS p_bad_in_tool,
       round(odds_ratio::numeric, 2) AS or_, p_hyper, q_bh, rank_bh
FROM mart.commonality_results
WHERE analysis_id = 'M3_ANY_w24h' AND group_type = 'tool' AND testable
ORDER BY p_hyper;

-- 13) 對照：排除共用批次後的機台層級排名（乾淨歸因；由 run_commonality --exclusive-lots 產生）
SELECT group_key AS tool, n_group AS n_lots, k_bad, round(p_bad_in_group::numeric, 4) AS p_bad_in_tool,
       round(odds_ratio::numeric, 2) AS or_, p_hyper, q_bh
FROM mart.commonality_results
WHERE analysis_id LIKE '%exclusive%' AND group_type = 'tool' AND testable
ORDER BY p_hyper;

-- 14) lot id 是否為全域名稱空間（決定「共用批次」是真共用還是 ID 碰撞）
--     判讀：若為每台機台自己從 1 編號 → min≈1、max≈distinct、密度≈1
--           實測結果為 min=125、max=18,977、密度 0.163 → 全域編號，共用批次為真
SELECT tool_id,
       count(DISTINCT lot_id) AS n_lots,
       min(lot_id) AS lot_id_min,
       max(lot_id) AS lot_id_max,
       round(count(DISTINCT lot_id)::numeric / nullif(max(lot_id) - min(lot_id) + 1, 0), 4) AS id_density
FROM stg.fact_process_event
GROUP BY 1 ORDER BY 1;

-- 15) 機台層級「全部群組」：含未列入檢定的（負控制要看這一列）
--     預期（exclusive 分析）：04M01 以 k_bad = 0 出現於此，testable = false
SELECT group_key AS tool, n_group AS n_lots, k_bad,
       CASE WHEN n_group > 0 THEN round(k_bad::numeric / n_group, 4) END AS p_bad_in_tool,
       testable, rank_bh
FROM mart.commonality_results
WHERE analysis_id LIKE '%exclusive%' AND group_type = 'tool'
ORDER BY testable DESC, n_group DESC;
