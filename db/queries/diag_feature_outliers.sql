-- 特徵值健全性診斷：找出「原始特徵值本身離譜」的（機台 × 步驟 × 感測器 × 特徵）
-- 用途：MSPC 的分數若出現 10^12 倍，先確認是模型問題還是特徵本身的問題
--       （slope = 差值 / 時距，時距趨近 0 時會爆掉）
SELECT e.tool_id AS 機台, e.recipe_step AS 步驟, f.sensor AS 感測器,
       count(*) AS 事件數,
       round(min(f.slope)::numeric, 4)  AS 最小斜率,
       round(percentile_disc(0.5) WITHIN GROUP (ORDER BY f.slope)::numeric, 6) AS 中位數斜率,
       round(max(f.slope)::numeric, 4)  AS 最大斜率,
       round(min(f.std_val)::numeric, 4) AS 最小標準差,
       round(max(f.std_val)::numeric, 4) AS 最大標準差,
       count(*) FILTER (WHERE abs(f.slope) > 1e6) AS 斜率絕對值超過1e6的筆數,
       count(*) FILTER (WHERE f.std_val = 0)      AS 標準差為0的筆數
FROM stg.fact_sensor_feature f
JOIN stg.fact_process_event e ON e.event_id = f.event_id
WHERE e.tool_id IN ('03M01', '06M01') AND e.recipe_step IN ('1', '6', '8')
  AND f.sensor IN ('ACTUALSTEPDURATION', 'ETCHBEAMVOLTAGE', 'IONGAUGEPRESSURE')
GROUP BY e.tool_id, e.recipe_step, f.sensor
HAVING count(*) FILTER (WHERE abs(f.slope) > 1e6) > 0
    OR max(f.slope) > 1e5 OR min(f.slope) < -1e5
ORDER BY 最大斜率 DESC NULLS LAST;
