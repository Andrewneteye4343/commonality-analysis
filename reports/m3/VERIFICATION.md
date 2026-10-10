# M3 驗收紀錄（Multi-tool Commonality Analysis）

- 日期：2026-10-10
- 程式快照：`VERSION = 2026-10-10.r6`（`docker compose run --rm etl python run_query.py --version` 可驗）
- 資料：PHM 2018 Ion Mill Etch，**5 台機台**（01M02、02M02、03M01、04M01、06M01）+ UCI SECOM
- 環境：Windows 11 + WSL2 + Docker Desktop；`postgres:16-alpine`；執行容器 `ca-python:dev`

## 1. 載入（`build_all_tools.py --reset`）

| 項目 | 數值 |
|---|---|
| 讀入列數 | **7,198,950** |
| 加工事件 | **203,976** |
| 事件內時間點合計 | 7,198,950（= trace 列數，**守恆 ✅**） |
| 感測特徵列數 | 3,467,592（= 事件 × 17 感測器 ✅） |
| 耗時 | 289 秒 |
| 報告 | `reports/m3/build_all_tools.json` |

## 2. 批次標籤（`load_fault_labels.py --window-hours 24`）

| 定義 | 壞批次 | 比例 | 窗內事件 |
|---|---|---|---|
| ANY | **475 / 7,188** | **6.61%** | 6,348 |
| FlowCool Pressure Dropped Below Limit | 258 / 3,368 | 7.66% | 4,806 |
| Flowcool Pressure Too High Check Flowcool Pump | 250 / 6,261 | 3.99% | 1,606 |
| Flowcool leak | 64 / 3,368 | 1.90% | 1,066 |

分母依故障型態而變，是因為只有「含該故障型態的機台」才產生該 scope 的標籤（語意正確）。

### 2.1 共用機台（分析前題的實測查證）

| 檢查 | 數值 | 判讀 |
|---|---|---|
| `03_M01_DC_score.csv` lot id 範圍 / 密度 | 125 – 18,977 / **0.1634** | 全域編號（非每台各自從 1 編號） |
| 5 台 distinct lot | 7,188 | 大於任一單台（最大 3,869） |
| （lot, 機台A, 機台B）配對 | 11,911 | — |
| **被 ≥2 台機台加工過的批次** | **3,952 / 7,188 = 55.0%** | 共用機台現象真實存在 |
| 跨機台加工時間區間重疊 | 4,474 / 11,911 = 37.56% | 因同一 lot 平均 9.4 次上同一台機台，跨度寬而交錯，屬預期 |

→ 因此驗收分**兩個視角**：全部批次（反映真實路線污染） vs 只算單一機台批次（乾淨歸因）。

## 3. 機台層級共同性（`run_commonality --window-hours 24 --fault-type ANY --group-types tool`）

### 視角 A：全部批次（`analysis_id = M3_ANY_w24h`）

| tool | n_lots | k_bad | 壞率 | OR | p_hyper | q (BH) | rank |
|---|---|---|---|---|---|---|---|
| 01M02 | 629 | 142 | 22.58% | 5.45 | 6.03e-44 | 3.02e-43 | 1 |
| 03M01 | 3,081 | 337 | 10.94% | 3.53 | 1.54e-37 | 3.85e-37 | 2 |
| 02M02 | 3,512 | 285 | 8.12% | 1.62 | 3.02e-07 | 5.04e-07 | 3 |
| 06M01 | 3,395 | 276 | 8.13% | 1.60 | 5.75e-07 | 7.18e-07 | 4 |
| **04M01** | 3,869 | 258 | 6.67% | **1.02** | 0.4313 | **0.4313（不顯著）** | **5** |

### 視角 B：只算單一機台批次（`--exclusive-lots`，`analysis_id = M3_ANY_w24h_exclusive`）

| tool | n_lots | k_bad | 壞率 | OR | q (BH) | testable |
|---|---|---|---|---|---|---|
| 01M02 | 113 | 29 | 25.66% | **12.34** | 5.57e-18 | ✅ |
| 03M01 | 540 | 32 | 5.93% | 2.01 | 0.0027 | ✅ |
| 02M02 | 1,110 | 46 | 4.14% | 1.31 | 0.134 | ✅ |
| 06M01 | 546 | 7 | 1.28% | **0.31** | 1.0 | ✅ |
| **04M01** | **927** | **0** | **0.00%** | — | — | ❌ `k_bad 0 < 3`（**負控制**） |

## 4. 驗收閘門

| # | 閘門 | 結果 |
|---|---|---|
| 1 | 有故障機台排在前面 | ✅ 01M02 → 03M01 → 02M02 → 06M01 |
| 2 | **完全零故障的 04M01 不進前 3** | ✅ 排第 5，且是 5 台中唯一不顯著（q = 0.43） |
| 3 | **負控制：零故障機台的專屬批次不得有壞批次** | ✅ **0 / 927 = 0.00%** |
| 4 | 多重比較校正 | ✅ BH-FDR 已套用；本次多機台執行的群組數／顯著數見 `reports/m3/fdr_summary.json`（單機台結構的評估實驗另為：194 群組、未校正 59 顯著 vs 巧合期望 9.7、Bonferroni 2、BH 29） |
| 5 | 名次指標可重現 | ✅ `rank_min` / `rank_max` / 並列數（順序無關定義）；重跑一致 |

## 5. 誠實缺口（不得在文件或履歷中誇大）

- **機台層級的正向結果部分是同義反覆**：壞批次定義來自各機台自己的故障窗，有故障的機台
  排名靠前可預期。有資訊量的是**負控制**；方法的辨識能力由合成注入實驗量測
  （recipe / stage @1 = 100%、`recipe_step` @1 = **0%** 且名次 11–13 → 共線性造成辨識不能）。
- **PHM 2018 沒有良率**：故障是設備類（FlowCool 壓力/洩漏），不是晶圓良率。
  良率對應由 UCI SECOM 承擔（1567 樣本、fail 104 / 6.64%）。
- **共用路線造成混淆已量化**：全部批次視角下 02M02（q 0.134）、06M01（方向翻轉至 OR 0.31）
  的顯著性都來自共用路線 → 須由 M4 的多變數模型（logistic regression 同時放各機台共變數）解決。
- **`Mart` 層標籤為「設備故障窗口」的代理指標**，不等於實際報廢或良率損失。

## 6. 重現指令

```powershell
docker compose run --rm etl python run_query.py --version                     # 應為 2026-10-10.r6
docker compose run --rm etl python build_all_tools.py --reset
docker compose run --rm etl python load_fault_labels.py --raw /data/raw --window-hours 24
docker compose run --rm etl python -m analysis.run_commonality --window-hours 24 --fault-type ANY --group-types tool
docker compose run --rm etl python -m analysis.run_commonality --window-hours 24 --fault-type ANY --group-types tool --exclusive-lots --analysis-id M3_ANY_w24h_exclusive --out reports/m3_exclusive
docker compose run --rm etl python run_query.py db/queries/m3_acceptance.sql
docker compose run --rm etl python -m pytest analysis/tests -q                 # 29 passed
```
