-- D 項修正：維度鍵值正規化（recipe / recipe_step）
--
-- 問題（實測）：PHM 各機台檔案的寫法不一致 —— 03M01 是整數字串 `1`，02M02 是浮點字串 `1.0`。
-- 同一個製程步驟因此被拆成兩個群組鍵，分組統計與 lot_membership 都會出現重複鍵
-- （例：`02M02/1.0/ACTUALSTEPDURATION/std_val`）。
--
-- 規則（保守）：只把「小數部分全為 0」的值收斂成整數字串（1.0 → 1）；`1.5` 這種不動。
-- 對應的 Python 實作在 `etl/keynorm.py`（新載入的資料在寫入前就正規化）。

-- 1) 事實表
DO $$
DECLARE
    n_recipe integer;
    n_step   integer;
BEGIN
    UPDATE stg.fact_process_event
       SET recipe = regexp_replace(btrim(recipe), '^([+-]?\d+)\.0+$', '\1')
     WHERE recipe <> regexp_replace(btrim(recipe), '^([+-]?\d+)\.0+$', '\1');
    GET DIAGNOSTICS n_recipe = ROW_COUNT;

    UPDATE stg.fact_process_event
       SET recipe_step = regexp_replace(btrim(recipe_step), '^([+-]?\d+)\.0+$', '\1')
     WHERE recipe_step <> regexp_replace(btrim(recipe_step), '^([+-]?\d+)\.0+$', '\1');
    GET DIAGNOSTICS n_step = ROW_COUNT;

    RAISE NOTICE '009：fact_process_event 更新 recipe % 列、recipe_step % 列', n_recipe, n_step;
END $$;

-- 2) 維度表 dim_step：正規化後重建（同鍵者保留 stage 數最多的那一列）
CREATE TEMP TABLE _dim_step_norm ON COMMIT DROP AS
SELECT DISTINCT ON (r || '|' || s)
       r || '|' || s AS step_key, r AS recipe, s AS recipe_step, stage_values, n_stages
FROM (
    SELECT regexp_replace(btrim(recipe), '^([+-]?\d+)\.0+$', '\1')      AS r,
           regexp_replace(btrim(recipe_step), '^([+-]?\d+)\.0+$', '\1') AS s,
           stage_values, n_stages
    FROM stg.dim_step
) t
ORDER BY r || '|' || s, n_stages DESC NULLS LAST;

DELETE FROM stg.dim_step;
INSERT INTO stg.dim_step (step_key, recipe, recipe_step, stage_values, n_stages)
SELECT step_key, recipe, recipe_step, stage_values, n_stages FROM _dim_step_norm;

-- 3) dim_recipe（同一類問題）
UPDATE stg.dim_recipe
   SET recipe_key = regexp_replace(btrim(recipe_key), '^([+-]?\d+)\.0+$', '\1')
 WHERE recipe_key <> regexp_replace(btrim(recipe_key), '^([+-]?\d+)\.0+$', '\1');

DO $$
DECLARE
    n_left integer;
BEGIN
    SELECT count(*) INTO n_left
    FROM stg.fact_process_event
    WHERE recipe_step ~ '^-?\d+\.0+$' OR recipe ~ '^-?\d+\.0+$';
    IF n_left > 0 THEN
        RAISE EXCEPTION '009：仍有 % 列未正規化', n_left;
    END IF;
    RAISE NOTICE '009 套用完成：事實表與維度表的 recipe/recipe_step 已統一寫法';
    RAISE NOTICE '⚠ 請接著重跑 load_fault_labels.py 重建 mart.lot_membership（它是 TRUNCATE 重建，安全）';
END $$;
