-- M5-2-1 驗收查詢（多變量 MSPC）
-- 用法：docker compose run --rm etl python run_query.py db/queries/m5_2_acceptance.sql
-- 所有查詢都綁定 run_id（可用 \set 或直接改下面的變數）

-- [0] 這個資料庫裡有哪些 run（避免拿舊資料當新結果）
SELECT run_id, robust, count(*) AS 模型數, min(created_at) AS 建立時間
FROM mart.mspc_models GROUP BY run_id, robust ORDER BY 建立時間 DESC;

-- [1] 模型摘要：主成分數、累積解釋、條件數、管制線
SELECT tool_id AS 機台, recipe_step AS 步驟, robust AS 穩健版,
       n_vars AS 變數數, n_base_complete AS 基準期完整向量, pca_k AS 主成分數,
       round(var_explained_actual::numeric, 3) AS 累積解釋,
       round(cond_number::numeric, 1) AS 條件數,
       round(lim_t2::numeric, 1) AS T2管制線, round(lim_spe::numeric, 1) AS SPE管制線,
       round(base_alarm_t2::numeric, 4) AS 基準期T2警報率,
       round(base_alarm_spe::numeric, 4) AS 基準期SPE警報率,
       round(imputed_rate::numeric, 4) AS 缺失代入比例
FROM mart.mspc_models
WHERE run_id = 'M5_mspc_b0.3'
ORDER BY tool_id, recipe_step, robust DESC;

-- [2] **核心比較：逐機台的警報事件數與警報率**（robust 與 plain 兩版並列）
--     分母用 mart.mspc_models.n_monitor（**實際被建模的監控期事件**），
--     不是全部事件——MSPC 只覆蓋樣本足夠的集團，用全量當分母會低估警報率。
WITH mon AS (
    SELECT run_id, robust, tool_id,
           count(*)            AS 建模集團數,
           sum(n_monitor)      AS 監控期事件
    FROM mart.mspc_models
    WHERE run_id = 'M5_mspc_b0.3'
    GROUP BY run_id, robust, tool_id
)
SELECT m.robust AS 穩健版, m.tool_id AS 機台,
       count(DISTINCT a.event_id) AS 警報事件數,
       m.建模集團數, m.監控期事件,
       round(100.0 * count(DISTINCT a.event_id) / nullif(m.監控期事件, 0), 3) AS 警報率百分比
FROM mon m
LEFT JOIN mart.v_mspc_event_alerts a
       ON a.run_id = m.run_id AND a.robust = m.robust AND a.tool_id = m.tool_id
GROUP BY m.robust, m.tool_id, m.建模集團數, m.監控期事件
ORDER BY m.robust DESC, 警報率百分比 DESC;

-- [3] 負控制機台（零故障機台）的警報率＝真正的誤報率
--     M5-1 單變量的 04M01 是 63.1%，MSPC 應該明顯下降
WITH mon AS (
    SELECT run_id, robust, tool_id, sum(n_monitor) AS 監控期事件
    FROM mart.mspc_models
    WHERE run_id = 'M5_mspc_b0.3' AND tool_id = '04M01'
    GROUP BY run_id, robust, tool_id
)
SELECT m.robust AS 穩健版, m.tool_id AS 機台,
       count(DISTINCT a.event_id) AS 警報事件,
       m.監控期事件,
       round(100.0 * count(DISTINCT a.event_id) / nullif(m.監控期事件, 0), 3) AS 誤報率百分比
FROM mon m
LEFT JOIN mart.v_mspc_event_alerts a
       ON a.run_id = m.run_id AND a.robust = m.robust AND a.tool_id = m.tool_id
GROUP BY m.robust, m.tool_id, m.監控期事件
ORDER BY m.robust DESC;

-- [4] 故障窗內偵測率（窗 24h，與 M3／M5-1 一致）
--     ⚠ 口徑：分母是「該 run 建模過機台的全部有標籤事件」；MSPC 只覆蓋樣本足夠的集團，
--        所以這裡會**低估**偵測率。程式自己印的偵測率以「本次實際評分的集團」為分母。
WITH mon AS (
    SELECT DISTINCT run_id, robust, tool_id
    FROM mart.mspc_models WHERE run_id = 'M5_mspc_b0.3'
), denom AS (
    SELECT m.robust, count(*) AS 窗內事件
    FROM mart.event_labels l
    JOIN mon m ON m.tool_id = l.tool_id
    WHERE l.window_hours = 24 AND l.in_window
    GROUP BY m.robust
), num AS (
    SELECT m.robust, count(DISTINCT a.event_id) AS 窗內警報事件
    FROM mart.v_mspc_event_alerts a
    JOIN mart.event_labels l ON l.event_id = a.event_id AND l.window_hours = 24
    JOIN mon m ON m.robust = a.robust AND m.tool_id = a.tool_id
    WHERE a.run_id = 'M5_mspc_b0.3' AND l.in_window
    GROUP BY m.robust
)
SELECT d.robust AS 穩健版, d.窗內事件,
       coalesce(n.窗內警報事件, 0) AS 窗內警報事件,
       round(100.0 * coalesce(n.窗內警報事件, 0) / nullif(d.窗內事件, 0), 2) AS 偵測率百分比
FROM denom d LEFT JOIN num n ON n.robust = d.robust
ORDER BY d.robust DESC;

-- [5] 貢獻診斷排名：哪個（感測器 × 特徵）最常是「兇手」（給 M5-2-2 與面試用）
--     ⚠ 用**中位數**與 p90，不用平均：少數數值爆掉的集團會把平均拉到 10^32（實際踩到）。
--        同時把倍數上限截到 1e4，讓輸出可讀（原始值仍可在明細中查）。
SELECT top1 AS 最可能的貢獻變數, count(*) AS 次數,
       round(percentile_disc(0.5) WITHIN GROUP (ORDER BY least(value / nullif(limit_value, 1), 1e4))::numeric, 2)
         AS 中位數超標倍數,
       round(percentile_disc(0.9) WITHIN GROUP (ORDER BY least(value / nullif(limit_value, 1), 1e4))::numeric, 1)
         AS p90超標倍數,
       count(*) FILTER (WHERE value / nullif(limit_value, 1) > 1e6) AS 疑似數值爆炸次數
FROM mart.mspc_alerts
WHERE run_id = 'M5_mspc_b0.3' AND top1 IS NOT NULL AND top1 <> ''
GROUP BY top1 ORDER BY 次數 DESC LIMIT 15;

-- [6] 哪個統計量先響（T² = 已知模式的幅度；SPE = 模型沒解釋的新變異）
-- ⚠ 「同時」不能用 stat='t2' AND stat='spe'（同一列只會有一個統計量，恆為 0）→ 用事件層視圖
SELECT a.robust AS 穩健版,
       count(*) FILTER (WHERE a.alert_t2) AS T2警報事件,
       count(*) FILTER (WHERE a.alert_spe) AS SPE警報事件,
       count(*) FILTER (WHERE a.alert_t2 AND a.alert_spe) AS 同時警報事件,
       count(*) AS 警報事件總數
FROM mart.v_mspc_event_alerts a
WHERE a.run_id = 'M5_mspc_b0.3'
GROUP BY a.robust ORDER BY a.robust DESC;

-- [7] 最嚴重的前 10 個警報（含貢獻分解）
SELECT tool_id AS 機台, recipe_step AS 步驟, event_id AS 事件, stat AS 統計量,
       round((value / nullif(limit_value, 1))::numeric, 1) AS 超標倍數,
       top1 AS 兇手1, round(top1_contrib::numeric, 1) AS 貢獻1,
       top2 AS 兇手2, top3 AS 兇手3
FROM mart.mspc_alerts
WHERE run_id = 'M5_mspc_b0.3'
ORDER BY value / nullif(limit_value, 1) DESC LIMIT 10;
