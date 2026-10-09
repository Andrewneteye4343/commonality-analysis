# commonality-analysis — WAT/機台/時序資料的良率共同性分析

用**公開資料 + 可控合成資料**，建立一套「良率異常 → 找出可疑機台/腔體/recipe」的
commonality analysis（共同性分析）pipeline，全程以 Docker 建置、可重現、可展示。

## 為什麼做這個專案

- 目標職缺（台積電 IMC／智慧製造）的核心工作：**從 WAT／CP／機台時序資料找出良率異常的根因**
- 面試實測發現的缺口：**時序資料分析與 forecasting 的實作經驗**，以及「評估指標要含
  偵測率、誤報率、領先時間」的量化能力
- 現有作品（WM-811K 晶圓瑕疵、台股資料管線、RAG、n8n）都缺「機台 genealogy + 時序資料」這一塊

## 三軌資料設計（誠實說明：公開資料沒有「WAT + 良率 + 機台歷程」的完整組合）

| 軌道 | 資料 | 提供什麼 | 缺什麼 |
|---|---|---|---|
| A｜真實（機台時序） | **[PHM 2018 Data Challenge](https://c3.ndc.nasa.gov/dashlink/resources/1009/)**（離子束蝕刻機，Seagate 提供，NASA DASHlink 公開釋出） | 20 台機台、Lot/stage/recipe/recipe_step、**17 個感測器時序（4 秒一筆）**、故障標籤與 TTF | 沒有 WAT 電性參數、沒有 CP 良率 |
| B｜真實（製程 + 良率） | **[UCI SECOM](https://archive.ics.uci.edu/dataset/179/secom)** | 1,567 筆製程感測特徵（590 欄）+ pass/fail 標籤 + 時間戳 | 欄位匿名、無機台 ID、無 genealogy |
| C｜合成（可控 ground truth） | 自建 fab 模擬器 | 完整 genealogy（lot→wafer→step→tool/chamber/recipe）+ **WAT 參數**（Vt/Idsat/Ioff/Rs/Cox…）+ CP bin + 故障標籤；**可注入已知根因** | 不是真實資料（但用來驗證方法與量測偵測率/誤報率） |

### 資料來源與授權（公開學術資料集，皆已匿名化）

| 軌道 | 來源連結 | 授權／說明 |
|---|---|---|
| A｜PHM 2018 | 資料集頁面：<https://c3.ndc.nasa.gov/dashlink/resources/1009/>（**免登入**）<br>檔案下載前綴：`https://c3.ndc.nasa.gov/dashlink/static/media/dataset/<檔名>`<br>官方說明與 TTF 評分公式：<https://phmsociety.org/wp-content/uploads/2018/05/PHM-Data-Challenge-2018-vFinal-v2_0.pdf> | PHM Society Data Challenge，NASA DASHlink 公開釋出；本專案使用 tool 03 的 `_DC_score.csv`／`_DC_test.csv`／`_DC_groundtruth.csv` |
| B｜UCI SECOM | 資料集頁面：<https://archive.ics.uci.edu/dataset/179/secom><br>直接下載：<https://archive.ics.uci.edu/static/public/179/secom.zip> | UCI Machine Learning Repository，2008 年捐贈，可自由使用；欄位已匿名化（f0001…f0590） |
| C｜合成資料 | 本專案自行產生（`M6` 的 fab 模擬器，尚未實作） | 本專案授權 |

- 下載、SHA256 校驗與血緣記錄由 [`etl/download_data.py`](etl/download_data.py) 執行（可重跑、可續傳）
- 實測檔案大小、checksum、欄位與語意落差見 [`DATASETS.md`](DATASETS.md)
- 本專案的資料均為**公開學術資料集**且已匿名化；文件與程式碼不對應、也不暗示任何特定公司的資料

**關鍵設計**：軌道 C 是這個專案最重要的一環——只有「知道正確答案」的資料，才能回答
「這套方法找得出來嗎？誤報多少？多早找到？」。這也是把面試話術從「我會做分析」
升級成「我能量化我的方法」的關鍵。

## 預期產出

1. **Docker 化 pipeline**：postgres + ETL + 分析 + Streamlit 深色儀表板
2. **對齊引擎**：把 lot/wafer/step 事件、秒級 trace、量測值對齊成可分析的事實表（含 join 覆蓋率報表）
3. **Commonality 排名**：每個 tool/chamber/recipe 的異常關聯分數（odds ratio、超幾何 p 值、FDR 校正）
4. **FDC 模組**：EWMA/CUSUM、Hotelling T²、PCA/MSPC、trace 對齊與 chamber matching
5. **Forecasting 模組**：機台 TTF 故障預測（用 PHM 2018 官方評分規則）、虛擬量測（VM）、良率預測，
   含預測區間與不確定性量化
6. **合成資料產生器 + 方法評估報告**：偵測率、誤報率、領先時間、預測誤差與區間覆蓋率
7. **文件與作品集素材**：README、方法論、結果圖表、面試可講的量化結論

## 為什麼要有 forecasting（專案第二大目標）

面試自述的缺口是**「缺乏時間序列 forecasting 的實作經驗」**。本專案的三個預測任務都有真值可比對：

- **F1 機台故障 TTF 預測**：PHM 2018 原任務本身，且**官方有評分公式與 ground truth**
  → 可以直接與公開競賽分數比較（比自訂指標更具公信力）
- **F2 虛擬量測（VM）**：用感測特徵預測 WAT 參數（真實 fab 最常見的 ML 落地場景）
- **F3 良率預測**：lot/wafer 級良率預測（合成資料有真值）

詳細設計見 `FORECASTING.md`。

## M1 快速開始（Windows + WSL2 + Docker Desktop）

```powershell
cd D:\repos\commonality-analysis
Copy-Item .env.example .env
docker compose build
docker compose up -d db
docker compose run --rm etl python download_data.py
docker compose run --rm etl python load_secom.py
docker compose run --rm etl python profile_phm.py --file /data/raw/03_M01_DC_score.csv
docker compose up -d dashboard
```

驗收查詢：

```powershell
docker compose exec db psql -U ca -d commonality -c "SELECT label, n, pct FROM qa.secom_label_distribution;"
```

詳細步驟、預期輸出與概念說明見 **`M1_CHECKLIST.md`**。

## M2 快速開始（genealogy 事實表）

```powershell
docker compose up -d --force-recreate etl db
docker compose run --rm etl python migrate.py
docker compose run --rm etl python analyze_event_boundaries.py --file /data/raw/03_M01_DC_score.csv
docker compose run --rm etl python build_genealogy.py --file /data/raw/03_M01_DC_score.csv
docker compose run --rm etl python align_qc.py --db
docker compose run --rm etl python align_qc.py --simulate --file /data/raw/03_M01_DC_score.csv --rows 300000
```

實測結果：**29,002 個加工事件**（gap 門檻 20 秒）、1,144,073 個時間點守恆、**493,034 筆感測特徵**。
詳細步驟與預期輸出見 **`M2_CHECKLIST.md`**。

## 目錄結構

```
commonality-analysis/
├── docker-compose.yml        # db / etl / dashboard / jupyter(選用 profile)
├── .env.example              # 環境變數範本（複製成 .env）
├── etl/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── download_data.py      # M1：下載 + SHA256 + MANIFEST（可續傳、可重跑）
│   ├── load_secom.py         # M1：解析 → 入庫 → 驗收報告（支援 --dry-run）
│   ├── profile_phm.py        # M1：PHM 感測檔剖析（串流讀取）
│   ├── migrate.py            # M2：套用版本化 migration（不再需要 down -v 重建）
│   ├── analyze_event_boundaries.py  # M2：事件切分的實證分析（決定 gap 門檻）
│   ├── build_genealogy.py    # M2：建立事件 / 感測時序 / 感測特徵事實表
│   └── align_qc.py           # M2：對齊品質檢查 + 時鐘偏移壓力測試
├── db/
│   ├── init/01_schema.sql    # M1 schema（容器首次啟動自動執行）
│   ├── migrations/002_m2_genealogy.sql   # M2 migration（由 migrate.py 套用）
│   └── queries/              # m1_acceptance.sql / m2_acceptance.sql
├── dashboard/                # Streamlit 深色儀表板（M1：資料概況）
├── docs/GITHUB_PUSH.md       # 推到 GitHub 的步驟、憑證注意事項與疑難排解
├── data/README.md            # 資料目錄說明與授權
└── reports/                  # 分析輸出（m1/、m2/ 各含實測報告與驗證紀錄）
```

## 環境需求

- Windows 11 + WSL2 + Docker Desktop（建議給 Docker 8 GB 以上記憶體）
- 磁碟：映像約 1.5 GB + 資料約 500 MB（**存在 Docker named volume，不佔專案資料夾**）
- 無 GPU 需求（M8 的深度學習模型會縮小規模或以 Colab 執行）

## 里程碑

見 `ROADMAP.md`（M1–M10）。每完成一個里程碑：commit + tag（v0.1、v0.2…）。

## 檔案

- `DATASETS.md` — 資料源查證結果、下載指令、實測欄位與大小
- `ALIGNMENT.md` — 資料對齊設計（genealogy 事實表、時間窗 join、粒度、常見坑）
- `METHODS.md` — commonality 方法清單、統計檢定、評估指標
- `FORECASTING.md` — 時序預測設計（三個預測任務、方法階梯、驗證策略、評估指標、常見坑）
- `ROADMAP.md` — M1–M10 里程碑、Docker 架構、驗收標準
