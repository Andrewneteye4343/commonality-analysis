# 資料源查證結果（2026-10 實測）

所有連結都經實際 HTTP 請求與檔案下載驗證，非只讀文件。

---

## A. PHM 2018 Data Challenge — 離子束蝕刻機（Ion Mill Etch）時序資料 ★主資料集

- **主辦**：PHM Society 2018 Data Challenge，資料由 **Seagate Technology** 提供
- **公開位置**：
  - NASA DASHlink 資源頁：`https://c3.ndc.nasa.gov/dashlink/resources/1009/`（**免登入**）
  - 官方完整資料（含 20 台機台訓練檔）：Google Drive `phm_data_challenge_2018.tar.gz`
    → `https://drive.google.com/uc?export=download&id=15Jx9Scq9FqpIGn8jbAQB_lcHSXvIoPzb`（檔案大，需確認大小）
  - 官方說明 PDF：`https://phmsociety.org/wp-content/uploads/2018/05/PHM-Data-Challenge-2018-vFinal-v2_0.pdf`

### 已驗證可下載（HTTP 200）的檔案

| 檔案 | 大小 | 內容 |
|---|---|---|
| `01_M02_DC_score.csv` | 473.7 MB | **感測器時序 + Tool/Lot/recipe**（機台驗證集） |
| `02_M02_DC_score.csv` | 473.7 MB | 同上 |
| `03_M01_DC_score.csv` | 385.1 MB | 同上（已實測標頭，見下） |
| `04_M01_DC_score.csv` | — | 同上 |
| `06_M01_DC_score.csv` | 430.7 MB | 同上 |
| `01~06_M0x_DC_test.csv` | 21–43 MB | **TTF 預測目標格式**（`time` + 3 個 TTF 欄位） |
| `01~06_M0x_DC_groundtruth.csv` | 19–34 MB | TTF 真值（供評分） |

下載前綴：`https://c3.ndc.nasa.gov/dashlink/static/media/dataset/<檔名>`

### 實測欄位（`03_M01_DC_score.csv`，24 欄，取樣間隔 4 秒）

```
time, Tool, stage, Lot, runnum, recipe, recipe_step,
IONGAUGEPRESSURE, ETCHBEAMVOLTAGE, ETCHBEAMCURRENT,
ETCHSUPPRESSORVOLTAGE, ETCHSUPPRESSORCURRENT,
FLOWCOOLFLOWRATE, FLOWCOOLPRESSURE,
ETCHGASCHANNEL1READBACK, ETCHPBNGASREADBACK,
FIXTURETILTANGLE, ROTATIONSPEED, ACTUALROTATIONANGLE,
FIXTURESHUTTERPOSITION, ETCHSOURCEUSAGE,
ETCHAUXSOURCETIMER, ETCHAUX2SOURCETIMER, ACTUALSTEPDURATION
```

實測資料列（節錄）：

```
52161012,03M01,33,18290,13678142,3,3,0.42103516270961305,-0.2792846334232807,...
```

→ `Tool=03M01`、`stage=33`、`Lot=18290`、`runnum=13678142`、`recipe=3`、`recipe_step=3`，
感測器值皆已匿名化/正規化（約 ±1）、單位未提供。

### 故障類型（3 種，皆有 TTF 標籤）

1. `TTF_FlowCool Pressure Dropped Below Limit`（冷卻氣壓過低）
2. `TTF_Flowcool Pressure Too High Check Flowcool Pump`（冷卻氣壓過高，檢查泵）
3. `TTF_Flowcool leak`（冷卻洩漏）

### 對本專案的價值與限制

- ✅ **真正的機台 + lot + recipe + 時序 + 故障標籤**，可直接做 commonality 與 FDC
- ✅ 20 台機台 → 天然的多機台比較；`stage`/`recipe_step` 提供製程分段
- ⚠️ **沒有 WAT 電性參數、沒有 CP/良率**（故障是設備類，不是良率類）
- ⚠️ 資料量大（單檔 2 百萬列以上），必須抽樣/分批與欄位裁剪

---

## B. UCI SECOM — 半導體製程感測 + pass/fail ★輔助資料集

- 頁面：`https://archive.ics.uci.edu/dataset/179/secom`
- 直接下載（已驗證 HTTP 200）：`https://archive.ics.uci.edu/static/public/179/secom.zip`（1.9 MB）
- 內容（實測）：
  - `secom.data`：**1,567 列 × 590 欄**（製程感測/量測點，大量缺失值 NaN）
  - `secom_labels.data`：**1,567 筆**，標籤 `-1=pass（1,463 筆）、1=fail（104 筆）`，附時間戳
    （格式為 `dd/mm/yyyy hh:mm:ss`，如 `19/07/2008 11:55:00`）
- 限制：欄位**匿名**、**無機台 ID**、**無 genealogy** → 不能單獨做 tool commonality；
  適合做「製程訊號 → pass/fail」的統計檢定、SPC 與類別不平衡處理
- 類別不平衡：104/1567 ≈ **6.6% 失敗率**（與真實良率的稀有事件特性相符）

---

## C. 合成資料產生器（自建）

公開資料都沒有「WAT + 良率 + 機台歷程」的完整組合（真實 fab 資料是商業機密），
因此專案第三軌自建模擬器，規格：

**產生的表**
- `lot` / `wafer`（25 片/lot，含 slot 順序）
- `process_event`：wafer × step × tool/chamber/recipe_version + in/out 時間
- `sensor_trace`：每個 process_event 生成 4 秒間隔的時序（多感測器）
- `wat_measure`：每片 wafer 的 WAT 參數（Vt、Idsat、Ioff、Rs、Cox…；有合理的機台/腔體效應）
- `cp_result`：die 座標 + bin code（含空間型態）

**可注入的根因（各有 ground truth）**
1. 單一機台漂移（tool drift，隨時間慢慢偏）
2. 腔體不匹配（chamber mismatch：同機台不同 chamber 差異常數）
3. recipe 版本變更造成的位移（step change）
4. 複合根因（機台組合，如「A 機台 + B 腔體」才出問題）
5. 對照組（不注入任何根因）→ 用來量測**誤報率**

**評估指標**（本專案的核心科學價值）
- 偵測率（sensitivity/recall）：注入的根因被排進前 N 名的比例
- 誤報率（false discovery rate）：多重比較校正前後的差別
- 領先時間（detection lead time）：從根因開始到被偵測出的時間
- 穩健性：不同資料量、不同效應量下的表現（small-sample 的 shrinkage 效果）

---

## 授權與引用注意

- PHM 2018 資料：PHM Society / Seagate 公開釋出，學術與個人研究使用；引用請註明
  PHM Society 2018 Data Challenge。
- SECOM：UCI ML Repository，2008 年捐贈，可自由使用；引用請註明 UCI 與原始捐贈者。
- 專案 README 需明確寫出資料來源與「資料已匿名化、不含真實廠商識別資訊」，
  避免任何影射特定公司（面試時的誠信風險）。
