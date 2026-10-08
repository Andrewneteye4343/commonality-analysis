# ROADMAP — commonality-analysis（M1–M8）

執行環境：**Windows 11 + WSL2 + Docker Desktop**（與既有的 RAG／台股專案一致）
每個里程碑：**教學 → 你動手 → 我驗收 → commit + tag**。

---

## Docker 架構（先定案）

```
services:
  db         postgres:16            # 資料倉儲（維度表 + 事實表）
  etl        python:3.11-slim       # 下載 → 清理 → 對齊 → 入庫（一次性 compose run）
  analysis   jupyter/scipy-notebook # 分析與統計（Notebook + CLI）
  dashboard  python + streamlit     # 深色主題報表（異常警報、commonality 排名、trace 疊圖）
volumes:
  pgdata        # 資料庫資料
  rawdata       # 原始檔（PHM/SECOM），避免重複下載（單檔 385 MB 起）
  pipcache      # ★ compose run 為一次性容器，套件快取必須掛 volume 才不會每次重裝
```

**設計原則**
- **一次性 vs 常駐**：`etl`／`analysis` 用 `docker compose run --rm`（一次性），`db`／`dashboard` 用 `up -d`
- **記憶體控制**：PHM 單檔 200 萬列以上 → 用 **chunked read** 或 **DuckDB/Parquet** 落地，不全載入 DataFrame
- **可重現**：所有資料下載用固定 URL + 檔案大小/checksum 驗證；資料版本寫進 `data/README`
- **指令風格**：PowerShell 單行、避免 `$` 變數（你的偏好）

---

## M1｜環境建置 + 資料取得（半天）

**學習目標**：Docker Compose 多服務、volume 快取、一次性 vs 常駐容器
**產出**
- `docker-compose.yml`（db + etl + analysis + dashboard 骨架）
- `scripts/download_data.py`：下 SECOM（1.9 MB）+ PHM 2018 單一機台感測檔（385 MB）+ 對應 groundtruth
- `data/README.md`：資料來源、檔名、大小、下載日期、checksum
**驗收**：`docker compose up -d db` 後能連線；etl 容器跑完下載腳本並印出檔案大小與 SHA256；SECOM 載入驗證為 1567×590、標籤 1463 pass / 104 fail

---

## M2｜資料模型與對齊引擎（1–2 天）

**學習目標**：genealogy 事實表、時間窗 join、粒度與覆蓋率
**產出**
- `db/schema.sql`：`dim_*` 與 `fact_process_event` / `fact_sensor_trace` / `fact_wat` / `fact_cp` / `fact_fault`
- `etl/`：把 PHM 資料轉成 event + trace 格式（`Tool/Lot/runnum/recipe/recipe_step` → 事件；`time` → trace）
- `etl/align.py`：時間窗 join + 每 wafer-step 的感測特徵聚合（mean/std/min/max/slope/duration/missing_rate）
- `reports/alignment_quality.md`：**覆蓋率、未匹配比例、重複匹配率、時間落差分布**
**驗收**：
- 事件表與 trace 的 join 覆蓋率 > 95%（未匹配的原因要能解釋）
- 重複匹配率 ≈ 0
- 對齊品質報表可重跑、數字一致（同 seed / 同資料）

---

## M3｜基礎 Commonality（1–2 天）

**學習目標**：2×2 列聯表、Fisher's exact、超幾何（Williams'）、效應量、FDR
**產出**
- `analysis/commonality_basic.py`：對每個 tool/chamber/recipe/step 產生 a/b/c/d、OR + CI、Fisher p、超幾何 p
- `analysis/multiple_testing.py`：Bonferroni 與 BH-FDR，輸出校正前後對照
- `reports/commonality_ranking.csv` + 圖（Pareto、森林圖 forest plot）
**驗收**：在 PHM 真實資料上，**已知發生 Flowcool 故障的機台必須被排進前 3 名**（這是真實標籤的驗證）

---

## M4｜進階 Commonality（2 天）

**學習目標**：控制共變數、混合效應、貝氏收縮、組合式根因、ML 可解釋性
**產出**
- logistic regression（含 recipe/時間控制項）+ mixed-effects（lot 隨機效應）
- Beta-binomial / Bayesian shrinkage 排名（與 M3 排名對照）
- 組合式搜尋（tool × chamber 對組合）+ Gibbs/貝氏版本與貪婪版本比較
- Random Forest + SHAP 排名（與統計方法交叉驗證）
**驗收**：報告「不同方法排名的重疊度（rank correlation）」與「小樣本機台的收縮效果」；
說明每種方法在什麼情況下會誤判

---

## M5｜時序 / FDC（2–3 天）

**學習目標**：管制圖、多變量 SPC、trace 對齊
**產出**
- EWMA / CUSUM 逐感測器監控（含 ARL 概念）
- Hotelling T² + PCA/MSPC（T² 與 SPE/Q 統計量）
- trace 對齊（時間正規化 + DTW）與 chamber matching 距離矩陣
- `reports/fdc_excursions.csv`：被判定異常的 wafer-step 清單
**驗收**：把 FDC 找到的異常 wafer-step 丟進 M3 的共同性分析 → 確認能收斂到正確機台
（**兩層串接**是本專案的關鍵驗收）

---

## M6｜合成資料產生器 + 方法評估（2–3 天）★專案核心

**學習目標**：可控制的 ground truth、方法評估指標
**產出**
- `sim/generator.py`：產生 lot/wafer/genealogy/trace/WAT/CP，可注入 5 類根因（單機漂移、腔體不匹配、
  recipe 變更、組合根因、無根因對照）
- `sim/evaluate.py`：計算偵測率、誤報率（FDR）、領先時間、排名穩健性
- `reports/simulation_eval.md`：不同效應量 × 資料量下的表現曲線
**驗收**：產出「效應量 vs 偵測率」曲線與「無根因資料的假陽性數量」；
說明 pipeline 在什麼條件下會失效（誠實揭露限制）

---

## M7｜Forecasting 基礎：機台故障 / TTF 預測（2–3 天）★新增

**學習目標**：時序預測的基本功、基準線思維、官方評分規則
**產出**
- `forecast/baseline.py`：持續法、歷史平均、固定時距（**沒有基準線的預測沒有意義**）
- `forecast/classical.py`：ARIMA/SARIMA、ETS（單一感測器）
- `forecast/gbdt.py`：LightGBM + lag/滾動統計特徵（過去 N 步 mean/std/min/max/slope、EWMA、runnum、
  機台使用壽命、距上次保養時間）
- `forecast/phm_score.py`：**實作 PHM 2018 官方評分函式**（`exp(-0.001·GT)·|GT−SUB|` 等四種情況）
- `forecast/validate.py`：**walk-forward / rolling-origin 驗證**（嚴格只用 t 之前的資訊）
**驗收**：在 PHM 真實資料上跑出 TTF 預測，並與三種基準線比較，報告官方分數；
明確列出「哪些特徵可能造成作弊（如 ETCHSOURCEUSAGE）」並只用合法特徵

---

## M8｜Forecasting 進階：機率式預測與 VM／良率預測（2–3 天）★新增

**學習目標**：DL 時序模型、預測區間、跨任務遷移
**產出**
- `forecast/dl.py`：LSTM/GRU 或 TCN（無 GPU → 縮小模型或用 Colab；與 WM-811K 相同策略）
- `forecast/probabilistic.py`：quantile regression / **conformal prediction** → 輸出預測區間與覆蓋率
- `forecast/vm.py`（F2 虛擬量測）：用感測特徵預測 WAT 參數
- `forecast/yield_forecast.py`（F3 良率預測）：lot/wafer 級良率預測
- `reports/forecast_eval.md`：MAE/RMSE、**pinball loss**、區間覆蓋率、prec/recall（稀有事件看 PR 曲線）、
  **領先時間**、誤報率、按機台分層的結果
**驗收**：產出「模型 vs 基準線」的完整比較表；預測區間覆蓋率接近名目值（例如 90% 區間實際涵蓋 85–95%）；
排序或分層顯示哪些機台最難預測並解釋原因

---

## M9｜整合與儀表板（2 天）

**學習目標**：把分析與預測變成別人能用的工具
**產出**
- Streamlit 深色儀表板（你偏好的風格）：
  ① 異常警報總覽 ② commonality 排名與森林圖 ③ trace 疊圖（正常 vs 異常）
  ④ **預測面板**（TTF 預測曲線 + 預測區間 + 領先時間）⑤ wafer map 與 bin 分布
- 台灣慣例色彩：異常/不良用紅色、正常用綠色
**驗收**：`docker compose up -d` 後開網頁即可操作，不需下命令；
預測面板能回答「哪台機台可能在何時出問題、有多確定」

---

## M10｜文件化與作品集（1–2 天）

**學習目標**：把技術成果轉成可被信任的敘事
**產出**
- `README.md`（含架構圖、資料流、方法、結果、可重現步驟）
- `docs/methodology.md`、`docs/findings.md`（量化結論：偵測率、誤報率、領先時間、預測誤差與區間覆蓋率）
- 面試話術：3 分鐘版本（問題 → 資料 → 對齊 → commonality → **forecasting** → 量化結果 → 限制）
**驗收**：任何人 clone 後照 README 能重現；資料來源與授權誠實揭露

---

## 學習地圖（你缺的知識點對應到哪個里程碑）

| 知識點 | 里程碑 |
|---|---|
| Docker Compose 多服務與 volume、一次性容器 | M1 |
| 資料模型設計（star schema）、時間窗 join | M2 |
| 假設檢定、p 值、效應量、多重比較校正 | M3 |
| 共變數控制、混合效應、貝氏收縮 | M4 |
| 時序分析（EWMA/CUSUM/T²/PCA/DTW） | M5 |
| 實驗設計與評估指標（偵測率/誤報率/領先時間） | M6 |
| **時序預測**（基準線、ARIMA/ETS、lag features + GBDT） | **M7** |
| **深度學習時序、預測區間與不確定性量化** | **M8** |
| 虛擬量測（VM）與良率預測的落地方式 | M8 |
| 資料產品化與可重現性 | M9–M10 |

## 語言與工具

Python（pandas/polars/duckdb、scipy、statsmodels、scikit-learn、pymc 或 statsmodels 貝氏）、
PostgreSQL、Streamlit、Plotly、Docker Compose。
