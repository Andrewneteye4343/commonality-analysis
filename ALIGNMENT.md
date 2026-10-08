# 資料對齊設計（Data Alignment）

Commonality analysis 的成敗幾乎都在對齊：**join 錯了，後面的統計再漂亮都是垃圾**。

---

## 1. 先認清四種粒度

| 粒度 | 單位 | 時間精度 | 本專案對應資料 |
|---|---|---|---|
| Lot | 一批（通常 25 片） | 天～小時 | 派工、recipe 決定 |
| Wafer | 一片 | 分鐘 | 機台進出時間、WAT/CP 結果 |
| Wafer × Step | 一片在一站的加工 | 秒 | 機台事件（tool/chamber/recipe/timestamp） |
| Sensor trace | 機台在一站的感測序列 | **4 秒**（PHM）／秒級 | FDC 原始資料 |

**對齊的目標**：把以上四層合成一張「**每片 wafer 在每一站經過哪台機台/腔體/recipe，且該站的感測特徵是什麼**」的事實表，
再把 WAT/CP 結果與故障標籤貼上去。

---

## 2. 事實表（star schema）設計

### 維度
- `dim_lot(lot_id, product_id, start_ts, priority)`
- `dim_wafer(wafer_id, lot_id, slot_no)`
- `dim_tool(tool_id, tool_type, area)`
- `dim_chamber(tool_id, chamber_id)`
- `dim_recipe(recipe_id, recipe_version, step_count)`
- `dim_step(step_no, step_name, area, is_measurement)`

### 事實
- **`fact_process_event`（核心 genealogy / event table）**
  `lot_id, wafer_id, step_no, step_name, tool_id, chamber_id, recipe_id, recipe_version,
   in_ts, out_ts, slot_no, runnum, is_rework`
  → 這是「wafer genealogy（晶圓履歷）」，commonality 的所有查詢都從這裡出發
- `fact_sensor_trace(tool_id, chamber_id, recipe_id, recipe_step, ts, sensor_id, value)`
  （或寬表：每個 sensor 一欄；分析時建議保留長表 + 衍生特徵表）
- `fact_sensor_feature(wafer_id, step_no, tool_id, chamber_id, sensor_id, mean, std, min, max,
   slope, area, duration, n_points, missing_rate)`
- `fact_wat(wafer_id, test_key, param, value, unit, measure_ts, tester_id, probe_card_id, site_x, site_y)`
- `fact_cp(wafer_id, die_x, die_y, bin_code, cp_ts)`
- `fact_fault(tool_id, fault_type, fault_ts)`（PHM 的 train_faults / groundtruth TTF 轉換而來）

---

## 3. 對齊五步

### 步驟 1：鍵值與後設資料正規化
- 統一大小寫、去空白、補零（`3M01` vs `03M01` vs `03m01`）
- 時間統一為同一時區（建議 UTC 儲存、顯示時轉 fab 本地時間；PHM 的 `time` 是相對秒數，需建立
  epoch 對應）
- lot／wafer／step／recipe 的命名規則要先寫成對照表（真實 fab 常見多套編碼）

### 步驟 2：建立 wafer × step 事件
- 最佳情況：機台 log 直接有 in/out 時間
- 常見情況：只有 lot 級時間 → 用 **slot 順序 × 每片處理時間** 推估每片時間窗，
  並且**量化推估誤差**（不要把推估值當真值）
- PHM 2018：同一 `Tool`+`Lot`+`runnum`+`recipe`+`recipe_step` 的連續時間戳即為一次加工事件
  → `in_ts=min(time)`、`out_ts=max(time)`、`duration=ACTUALSTEPDURATION`

### 步驟 3：時間窗 join（interval join）
- Sensor 一筆的 `ts` 落在 `[in_ts, out_ts]` → 歸屬於該 wafer-step
- 三種邊界情況都要處理：
  1. **跨界**（trace 跨越事件邊界）→ 依重疊比例分配，或設門檻後丟棄
  2. **多重匹配**（同一 tool 同時有多筆事件重疊 → 資料有問題，需標記）
  3. **未匹配**（trace 沒有對應事件，或事件沒有 trace）→ **一定要出報表統計覆蓋率**
- SQL 範例（含邊界與覆蓋率）：

```sql
-- 事件與 trace 的時間窗 join
SELECT e.lot_id, e.wafer_id, e.step_no, e.tool_id, e.chamber_id,
       avg(t.value) AS mean_value, stddev_samp(t.value) AS std_value, count(*) AS n_points
FROM fact_process_event e
JOIN fact_sensor_trace t
  ON t.tool_id = e.tool_id
 AND t.ts >= e.in_ts AND t.ts < e.out_ts
GROUP BY 1,2,3,4,5;
```

### 步驟 4：粒度聚合
- 每個 wafer-step 的每個 sensor 產生統計特徵（mean/std/min/max/slope/area/duration/missing_rate）
- **按 recipe_step 分段聚合**（不同製程步驟的物理意義不同，混在一起平均會抹掉訊號）
- 時間正規化：不同加工時間的 trace 長度不同 → 取相對時間（0–100%）後重採樣

### 步驟 5：標籤對齊與完整性驗證
- WAT/CP：以 `wafer_id` 對上事件表（同一片 wafer 的所有站）
- 故障標籤：以 `tool_id + 時間窗`對上（PHM 的故障是「機台在某段時間發生」）
- **產出資料品質報表**：各表筆數、join 覆蓋率、未匹配比例、重複鍵、時間落差分布
  （這份報表本身就是面試可展示的產出）

---

## 4. 常見坑（這些坑就是本專案的學習重點）

| 坑 | 症狀 | 對策 |
|---|---|---|
| 時鐘不同步 | 機台時鐘與 MES 差數秒至數分鐘 | 估計並校正 offset；時間窗加容忍帶（buffer） |
| lot 與 wafer 粒度混用 | 「這個 lot 經過 A 機台」被誤當成「每片 wafer 都經過 A」 | 明確記錄粒度，commonality 的分母要算 lot 還是 wafer 要先決定 |
| 重工（rework） | 同一 wafer 同一站出現多次 | 保留全部事件並標記 `is_rework`；分析時決定取第一次/最後一次 |
| recipe 版本變更 | 前後期資料分布不同（regime shift） | 以 recipe_version 分層；變更點前後分開分析 |
| 缺失值有資訊 | 感測器沒回報常代表故障或斷線 | 不要把 missing 直接填平均值；用 missing_rate 當特徵 |
| 類別不平衡 | 失敗樣本極少（SECOM 6.6%、真實良率更低） | 用適合稀有事件的檢定（Fisher/超幾何）、效應量、FDR 校正 |
| 資料洩漏 | 隨機切分造成過度樂觀 | **一律時間切分驗證**（與 WM-811K 用 lot 隔離切分同一精神） |
| 多重比較 | 同時檢定上百個 tool × step | Bonferroni（保守）或 Benjamini-Hochberg FDR（控制偽發現率） |

---

## 5. 對齊正確性怎麼驗證（不要只靠肉眼抽查）

1. **覆蓋率指標**：事件表有多少比例能配到 trace、配到 WAT、配到 CP
2. **重複匹配率**：一筆 trace 被多個事件吃掉的比率（應接近 0）
3. **時間落差分布**：`trace.ts - event.in_ts` 的分布應該落在合理區間（例如 0～加工時間）
4. **已知事件的回溯測試**：拿已知故障期間的資料，確認 trace 確實落在該機台該時段
5. **Negative control**：確定沒有故障的期間，trace 不應該被標成異常（誤報率基礎線）
6. **資料筆數守恆**：聚合前後的筆數、加權平均的一致性檢查
