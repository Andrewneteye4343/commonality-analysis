-- ═══════════════════════════════════════════════════════════════════════════
-- 003：修正感測器欄名筆誤（修復「002 修正前就已建立」的資料庫）
--
-- 背景：002 初版把三個欄位名寫成縮寫（iongaugpressure / etchauxtimer /
--       etchaux2timer），與 CSV 原始欄名不符，導致 COPY 匯入時報
--       UndefinedColumn: column "iongaugepressure" ... does not exist。
--       002 已修正，但已套用過的資料庫其資料表仍是舊欄名 → 用本檔修復。
--
-- 本檔兩個特性：
--   * 冪等：舊欄名不存在時什麼都不做（全新資料庫套用 002 後跑本檔＝無操作）
--   * 自檢：結尾驗證所有欄位都在，缺任何一個就直接失敗（不留半套狀態）
-- ═══════════════════════════════════════════════════════════════════════════

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'stg' AND table_name = 'fact_sensor_trace'
                 AND column_name = 'iongaugpressure') THEN
        ALTER TABLE stg.fact_sensor_trace RENAME COLUMN iongaugpressure TO iongaugepressure;
        RAISE NOTICE '已修正欄位：iongaugpressure → iongaugepressure';
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'stg' AND table_name = 'fact_sensor_trace'
                 AND column_name = 'etchauxtimer') THEN
        ALTER TABLE stg.fact_sensor_trace RENAME COLUMN etchauxtimer TO etchauxsourcetimer;
        RAISE NOTICE '已修正欄位：etchauxtimer → etchauxsourcetimer';
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'stg' AND table_name = 'fact_sensor_trace'
                 AND column_name = 'etchaux2timer') THEN
        ALTER TABLE stg.fact_sensor_trace RENAME COLUMN etchaux2timer TO etchaux2sourcetimer;
        RAISE NOTICE '已修正欄位：etchaux2timer → etchaux2sourcetimer';
    END IF;
END $$;

-- 長表檢視重建（欄位改名後檢視會自動跟隨，這裡明確重建以確保定義正確）
CREATE OR REPLACE VIEW stg.v_sensor_trace_long AS
SELECT t.event_id, t.ts, u.sensor, u.value
FROM stg.fact_sensor_trace t,
     LATERAL unnest(
        ARRAY['IONGAUGEPRESSURE','ETCHBEAMVOLTAGE','ETCHBEAMCURRENT','ETCHSUPPRESSORVOLTAGE',
              'ETCHSUPPRESSORCURRENT','FLOWCOOLFLOWRATE','FLOWCOOLPRESSURE','ETCHGASCHANNEL1READBACK',
              'ETCHPBNGASREADBACK','FIXTURETILTANGLE','ROTATIONSPEED','ACTUALROTATIONANGLE',
              'FIXTURESHUTTERPOSITION','ETCHSOURCEUSAGE','ETCHAUXSOURCETIMER','ETCHAUX2SOURCETIMER',
              'ACTUALSTEPDURATION'],
        ARRAY[iongaugepressure, etchbeamvoltage, etchbeamcurrent, etchsuppressorvoltage,
              etchsuppressorcurrent, flowcoolflowrate, flowcoolpressure, etchgaschannel1readback,
              etchpbngasreadback, fixturetiltangle, rotationspeed, actualrotationangle,
              fixtureshutterposition, etchsourceusage, etchauxsourcetimer, etchaux2sourcetimer,
              actualstepduration]
     ) AS u(sensor, value);

-- 自檢：17 個感測欄位必須全部存在，缺一即失敗（migration 為單一 transaction）
DO $$
DECLARE
    missing text;
BEGIN
    SELECT string_agg(c, ', ') INTO missing
    FROM unnest(ARRAY['iongaugepressure','etchbeamvoltage','etchbeamcurrent','etchsuppressorvoltage',
                      'etchsuppressorcurrent','flowcoolflowrate','flowcoolpressure','etchgaschannel1readback',
                      'etchpbngasreadback','fixturetiltangle','rotationspeed','actualrotationangle',
                      'fixtureshutterposition','etchsourceusage','etchauxsourcetimer','etchaux2sourcetimer',
                      'actualstepduration']) AS c
    WHERE NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'stg' AND table_name = 'fact_sensor_trace' AND column_name = c);

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'stg.fact_sensor_trace 仍缺少欄位：%', missing;
    END IF;
    RAISE NOTICE 'schema 自檢通過：stg.fact_sensor_trace 具備全部 17 個感測欄位';
END $$;
