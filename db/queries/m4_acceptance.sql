-- M4 驗收查詢（多變數模型）
--
-- 用法：docker compose run --rm etl python run_query.py db/queries/m4_acceptance.sql
--       docker compose run --rm etl python run_query.py db/queries/m4_acceptance.sql --only 2

-- 1) 機台層級：三種模型並列（原始壞率 vs 模型推得壞率 vs 差距）
SELECT method AS 模型, group_key AS 機台, n_group AS n, k_bad AS k,
       round(raw_bad_rate::numeric, 4) AS 原始壞率,
       round(expected_bad_rate::numeric, 4) AS 模型壞率,
       round(excess_bad_rate::numeric, 4) AS 差距,
       effect_type AS 效果類型,
       round(or_::numeric, 4) AS 效果量,
       round(p_wald::numeric, 8) AS p_wald
FROM mart.logistic_results
WHERE group_type = 'tool'
ORDER BY method, p_wald;

-- 2) M4 驗收核心：校正後「零故障機台」的自身效應必須趨近 0，有故障機台必須保留
SELECT method AS 模型, group_key AS 機台,
       round(raw_bad_rate::numeric, 4) AS 原始壞率,
       round(expected_bad_rate::numeric, 4) AS 模型壞率,
       round(excess_bad_rate::numeric, 4) AS 差距,
       CASE
         WHEN group_key IN ('04M01', '06M01') AND expected_bad_rate < 0.02 THEN '✅ 污染已剝離'
         WHEN group_key IN ('04M01', '06M01') THEN '⚠ 未剝離（檢查模型設定）'
         WHEN group_key IN ('01M02', '03M01') AND expected_bad_rate > 0.5 * raw_bad_rate THEN '✅ 效應保留'
         WHEN group_key IN ('01M02', '03M01') THEN '⚠ 效應被過度吸收'
         ELSE '—'
       END AS 判定
FROM mart.logistic_results
WHERE group_type = 'tool'
ORDER BY method, group_key;

-- 3) 差距佔原始壞率的比例（越接近 1 = 該機台的原始風險幾乎全來自路線共用）
SELECT method AS 模型, group_key AS 機台,
       round(raw_bad_rate::numeric, 4) AS 原始壞率,
       round((excess_bad_rate / NULLIF(raw_bad_rate, 0))::numeric, 3) AS 污染佔比
FROM mart.logistic_results
WHERE group_type = 'tool' AND raw_bad_rate > 0
ORDER BY method, 污染佔比 DESC;

-- 4) 與 M3 單變數結果的逐台對照（含 M3 的「全部批次」與「專屬批次」兩個視角）
SELECT l.method AS M4模型, l.group_key AS 機台,
       round(c.odds_ratio::numeric, 3) AS M3_OR,
       c.analysis_id AS M3分析,
       round(c.p_hyper::numeric, 8) AS M3_p,
       round(l.or_::numeric, 4) AS M4_效果量, l.effect_type AS 類型, l.p_method AS 檢定,
       round(l.p_wald::numeric, 8) AS M4_p
FROM mart.logistic_results l
JOIN mart.commonality_results c
  ON c.group_key = l.group_key AND c.group_type = l.group_type
WHERE l.group_type = 'tool' AND c.analysis_id LIKE 'M3_ANY_w24h%'
ORDER BY l.method, l.group_key, c.analysis_id;

-- 4b) 三視角三角驗證（本專案最關鍵的一張表）
--     ① 原始壞率（全部批次）② 專屬批次壞率（模型無關）③ hazard 模型推得壞率（模型校正）
--     三者若「② ≈ ③ 且都遠小於 ①」→ 該機台的原始風險確認為路線共用造成
SELECT l.group_key AS 機台,
       round(l.raw_bad_rate::numeric, 4) AS 原始壞率_全部批次,
       round(c_excl.k_bad::numeric / NULLIF(c_excl.n_group, 0), 4) AS 專屬批次壞率_模型無關,
       round(l.expected_bad_rate::numeric, 4) AS 模型校正後壞率,
       round(l.excess_bad_rate::numeric, 4) AS 路線共用造成的差距
FROM mart.logistic_results l
LEFT JOIN mart.commonality_results c_excl
  ON c_excl.group_key = l.group_key AND c_excl.group_type = l.group_type
 AND c_excl.analysis_id = 'M3_ANY_w24h_exclusive'
WHERE l.group_type = 'tool' AND l.method = 'hazard_sum'
ORDER BY l.raw_bad_rate DESC;

-- 5) 模型設定追溯（每個 run_id 的設計矩陣與收斂資訊）
SELECT run_id, method, model_note, n_obs, n_events, ridge, created_at
FROM mart.logistic_results
GROUP BY run_id, method, model_note, n_obs, n_events, ridge, created_at
ORDER BY run_id, method;

-- 4c) 檢定方法的正確性驗證（r12）：負控制機台不該顯著、真故障機台才顯著
--     logit/cloglog 應顯示 contrast_vs_others（無截距模型的 β = 0 檢定沒有意義）
--     hazard 應顯示 lrt_boundary（Wald 在此會測到 h = 1）
SELECT method AS 模型, group_key AS 機台, p_method AS 檢定,
       round(or_::numeric, 4) AS 效果量,
       round(p_wald::numeric, 6) AS p值,
       CASE WHEN group_key = '04M01' AND p_wald > 0.05 THEN '✅ 負控制通過'
            WHEN group_key = '04M01' THEN '❌ 負控制失敗（零故障機台不該顯著）'
            WHEN p_wald < 0.05 THEN '✅ 效應保留'
            ELSE '—' END AS 判定
FROM mart.logistic_results
WHERE group_type = 'tool'
ORDER BY method, p_wald;
