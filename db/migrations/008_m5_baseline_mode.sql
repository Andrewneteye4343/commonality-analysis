-- M5-1 修正：基準線模式與趨勢斜率要記錄下來
--
-- 為什麼：真實感測資料隨機台老化漂移 → 固定基準期不成立（實測基準期警報率 38%，名目 0.27%）。
-- 新增 trend 模式後，**判斷這個標準是怎麼來的**變成驗收的必要資訊：
-- 同一個 run_id 用不同模式跑，結果會完全不同，必須能分辨。
ALTER TABLE mart.fdc_baselines ADD COLUMN IF NOT EXISTS baseline_mode text;
ALTER TABLE mart.fdc_baselines ADD COLUMN IF NOT EXISTS slope double precision;

COMMENT ON COLUMN mart.fdc_baselines.baseline_mode IS
    'fixed＝固定基準期／trend＝線性趨勢外推／rolling＝滾動視窗';
COMMENT ON COLUMN mart.fdc_baselines.slope IS
    'trend 模式估出的每事件漂移量（感測單位／事件）；fixed 與 rolling 為 NULL';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'mart' AND table_name = 'fdc_baselines'
                     AND column_name = 'baseline_mode') THEN
        RAISE EXCEPTION 'mart.fdc_baselines.baseline_mode 建立失敗';
    END IF;
    RAISE NOTICE '008_m5_baseline_mode.sql 套用完成：baseline_mode、slope';
END $$;
