-- ═══════════════════════════════════════════════════════════════════════════
-- 005：釐清 mart.method_eval 的「負控制」如何表示（哨兵值，不改主鍵）
--
-- 背景（實際踩到的兩次，照實記下來）：
--   ① 最早的 005 想把 target_group 改成允許 NULL → PostgreSQL 拒絕：
--        column "target_group" is in a primary key
--      **主鍵欄位隱含 NOT NULL，不允許 DROP NOT NULL**。
--      （`pglast` 只驗語法，驗不到這種 catalog 語意——這是這次驗證沒攔到的主因）
--   ② 要讓 NULL 合法就必須動主鍵（移除並重組），風險是在本機無法先驗證的 DDL；
--      因此改用**語意明確的哨兵值**：
--
--        負控制（null 情境、沒有注入任何根因）→ target_group = '(none)'
--
--      零 schema 風險，而且負控制一定能寫進資料庫——負控制是方法評估最關鍵的一列，
--      不能因為 schema 限制而缺席。
--
-- 本檔只留下文件化的 COMMENT（安全、可重複執行），把不可見的設計決定留在資料庫裡。
-- ═══════════════════════════════════════════════════════════════════════════

COMMENT ON COLUMN mart.method_eval.target_group IS
    '注入根因的群組；負控制（null 情境、無注入）以哨兵值 (none) 表示——'
    '本欄為主鍵的一部分，PostgreSQL 不允許其為 NULL';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM mart.method_eval WHERE scenario = 'null' LIMIT 1) THEN
        RAISE NOTICE '提示：mart.method_eval 尚無負控制列；跑 eval_commonality.py 後會出現 scenario=null 且 target_group=(none)';
    END IF;
    RAISE NOTICE 'schema 說明已更新：負控制以 target_group = (none) 表示';
END $$;
