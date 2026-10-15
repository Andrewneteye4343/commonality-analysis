-- M5-4 驗收查詢（兩層串接：FDC 警報 → 共同性分析）
-- 用法：docker compose run --rm etl python run_query.py db/queries/m5_4_acceptance.sql
-- 所有查詢綁定 run_id（直接改下面的常數即可）
--
-- 表格語意（與 M3 完全相同，只把屬性換掉）：
--   列＝組內/組外（本機台 / 其他機台）、欄＝屬性（M3 用『批次是否不良』、M5-4 用『批次是否有警報』）
--   OR＝「本機台單位的屬性比例 ÷ 其他機台單位的屬性比例」（a*d / b*c）
--   ⚠ 這裡的 OR 是**未校正**版；分析腳本用 M3 的 contingency_stats（含 +0.5 連續性校正）
--      → 兩者數值會差幾個百分點（實測寬鬆 01M02 為 10.57 vs 9.85），**排序完全一致**；
--        驗收看的是定性結論與一致性檢查，不是小數第三位。
--   ⚠ 這不是「警報與不良的關聯」——那個在 M5-4-0 的可行性預覽已經算過（OR 2.712）。

-- 0) 這個資料庫裡有哪些 run（避免拿舊資料當新結果）
-- ⚠ PostgreSQL 沒有 min(boolean)（會報 UndefinedFunction）→ 用 count(欄位) 數非空值
SELECT run_id, unit_kind, count(*) AS 單位數,
       count(*) FILTER (WHERE has_alarm) AS 帶警報,
       count(outcome_bad) AS 有標籤數, max(n_alarms) AS 最大警報列數
FROM mart.lot_alert_features
GROUP BY run_id, unit_kind
ORDER BY run_id, unit_kind;

-- 1) 逐機台（寬鬆歸屬）：警報比例與壞批次比例（M3 的對照就在同一列）
SELECT m.group_key AS 機台,
       count(*) AS 經手批次,
       count(*) FILTER (WHERE f.has_alarm) AS 帶警報批次,
       round(100.0 * count(*) FILTER (WHERE f.has_alarm) / count(*), 2) AS 警報率_pct,
       count(*) FILTER (WHERE f.outcome_bad) AS 壞批次,
       round(100.0 * count(*) FILTER (WHERE f.outcome_bad) / count(*), 2) AS 壞批次率_pct
FROM mart.lot_membership m
JOIN mart.lot_alert_features f
  ON f.lot_id = m.lot_id AND f.unit_kind = 'lot' AND f.run_id = 'M5_mspc_b0.3'
WHERE m.group_type = 'tool'
GROUP BY m.group_key
ORDER BY 警報率_pct DESC;

-- 2) 逐機台（嚴格歸屬）：警報只記真正發生的機台
SELECT tool_id AS 機台,
       count(*) AS 單位數,
       count(*) FILTER (WHERE has_alarm) AS 帶警報單位,
       round(100.0 * count(*) FILTER (WHERE has_alarm) / count(*), 2) AS 警報率_pct,
       count(*) FILTER (WHERE outcome_bad) AS 壞批次單位
FROM mart.lot_alert_features
WHERE run_id = 'M5_mspc_b0.3' AND unit_kind = 'lot_tool'
GROUP BY tool_id
ORDER BY 警報率_pct DESC;

-- 3) 判準 1／2：前 2 名是誰（負控制 04M01 不得進前 2、真故障機台至少一台進前 2）
WITH r AS (
    SELECT tool_id AS 機台,
           round(100.0 * count(*) FILTER (WHERE has_alarm) / count(*), 2) AS 警報率_pct,
           row_number() OVER (ORDER BY count(*) FILTER (WHERE has_alarm)::numeric / count(*) DESC) AS 名次
    FROM mart.lot_alert_features
    WHERE run_id = 'M5_mspc_b0.3' AND unit_kind = 'lot_tool'
    GROUP BY tool_id
)
SELECT 名次, 機台, 警報率_pct,
       (SELECT count(*) FROM mart.fault_events fe WHERE fe.tool_id = r.機台) AS 故障次數,
       CASE WHEN 名次 <= 2 AND 機台 = '04M01' THEN '✗ 判準1 失敗（負控制進前 2）'
            WHEN 名次 <= 2 THEN '✓ 判準1 通過且進前 2'
            WHEN 機台 = '04M01' THEN '✓ 判準1 通過（負控制未進前 2）'
            ELSE '' END AS 判讀
FROM r ORDER BY 名次;

-- 4) 稀釋示範：同一個機台在兩種歸屬下的 OR（寬鬆會把 04M01 推成「顯著」）
WITH wide AS (
    SELECT m.group_key AS 機台,
           count(*) FILTER (WHERE f.has_alarm) AS a,
           count(*) FILTER (WHERE NOT f.has_alarm) AS b
    FROM mart.lot_membership m
    JOIN mart.lot_alert_features f
      ON f.lot_id = m.lot_id AND f.unit_kind = 'lot' AND f.run_id = 'M5_mspc_b0.3'
    WHERE m.group_type = 'tool'
    GROUP BY m.group_key
), tot AS (
    SELECT count(*) AS n_total,
           count(*) FILTER (WHERE has_alarm) AS n_alarm
    FROM mart.lot_alert_features
    WHERE run_id = 'M5_mspc_b0.3' AND unit_kind = 'lot'
), strict AS (
    SELECT tool_id AS 機台,
           count(*) FILTER (WHERE has_alarm) AS a,
           count(*) FILTER (WHERE NOT has_alarm) AS b
    FROM mart.lot_alert_features
    WHERE run_id = 'M5_mspc_b0.3' AND unit_kind = 'lot_tool'
    GROUP BY tool_id
), stot AS (
    SELECT count(*) AS n_total, count(*) FILTER (WHERE has_alarm) AS n_alarm
    FROM mart.lot_alert_features
    WHERE run_id = 'M5_mspc_b0.3' AND unit_kind = 'lot_tool'
)
SELECT w.機台,
       round(((w.a::numeric * (t.n_total - w.a - w.b)) /
              NULLIF(w.b::numeric * (t.n_alarm - w.a), 0)), 2) AS 寬鬆_OR,
       round(((s.a::numeric * (st.n_total - s.a - s.b)) /
              NULLIF(s.b::numeric * (st.n_alarm - s.a), 0)), 2) AS 嚴格_OR,
       (SELECT count(*) FROM mart.fault_events fe WHERE fe.tool_id = w.機台) AS 故障次數
FROM wide w JOIN tot t ON true
JOIN strict s ON s.機台 = w.機台
JOIN stot st ON true
ORDER BY 嚴格_OR DESC NULLS LAST;

-- 5) 一致性檢查：批次層的警報計數必須等於「該批次自己的警報列數」（洩漏／鍵對不上的偵測器）
WITH by_lot AS (
    SELECT e.lot_id, count(*) AS n_alarms, count(DISTINCT a.event_id) AS n_events
    FROM mart.mspc_alerts a
    JOIN stg.fact_process_event e ON e.event_id = a.event_id
    WHERE a.run_id = 'M5_mspc_b0.3' AND a.robust = true
    GROUP BY e.lot_id
)
-- ⚠ 欄位名要精確：「檢查的批次數」是分母（不是不一致數），看「不一致總數」必須為 0。
SELECT count(*) AS 檢查的批次數,
       count(*) FILTER (WHERE f.n_alarms <> coalesce(b.n_alarms, 0)) AS 警報列數不符,
       count(*) FILTER (WHERE f.n_alarm_events <> coalesce(b.n_events, 0)) AS 警報事件數不符,
       count(*) FILTER (WHERE f.n_alarms <> coalesce(b.n_alarms, 0)
                          OR f.n_alarm_events <> coalesce(b.n_events, 0)) AS 不一致總數
FROM mart.lot_alert_features f
LEFT JOIN by_lot b ON b.lot_id = f.lot_id
WHERE f.run_id = 'M5_mspc_b0.3' AND f.unit_kind = 'lot';

-- 6) 嚴格版的單位數應等於 (批次 × 機台) 的共現列數（形狀一致性）
SELECT (SELECT count(*) FROM mart.lot_alert_features
        WHERE run_id = 'M5_mspc_b0.3' AND unit_kind = 'lot_tool') AS 嚴格單位數,
       (SELECT count(*) FROM mart.lot_membership WHERE group_type = 'tool') AS 共現列數,
       (SELECT count(*) FROM mart.lot_alert_features
        WHERE run_id = 'M5_mspc_b0.3' AND unit_kind = 'lot') AS 批次數,
       (SELECT count(DISTINCT lot_id) FROM mart.lot_units WHERE fault_type = 'ANY') AS 標籤批次數;
