# M1 Checklist — 環境建置 + 資料取得

> 全部指令在 **Windows PowerShell** 執行（單行、不使用 `$` 變數）。
> 每一步都有「預期輸出」，若對不上先不要往下做，直接貼給我。
> 預計時間：**30–50 分鐘**（其中下載 385 MB 的檔案約 5–15 分鐘，取決於網速）。

---

## 0. 前置檢查（2 分鐘）

```powershell
docker version
docker compose version
```

預期：兩個都印出 Client 與 Server 版本（Server 有版本 = Docker Desktop 正在跑）。
若出現 `cannot connect to the Docker daemon` → 先開啟 Docker Desktop，等右下角圖示變綠。

**資料放哪？** 你的專案在 `D:\repos\commonality-analysis`，但**下載的資料不會放在 `D:\`**——
原因見第 5 步的說明（WSL2 跨檔案系統很慢）。

---

## 1. 放置專案檔

把交付的 zip 解壓到 `D:\repos\commonality-analysis`（平鋪結構，`docker-compose.yml` 直接在這一層）。

```powershell
cd D:\repos\commonality-analysis
dir
```

預期看到：`docker-compose.yml`、`.env.example`、`README.md`、`M1_CHECKLIST.md`、`db\`、`etl\`、`dashboard\`、`data\`

---

## 2. 建立環境變數檔

```powershell
Copy-Item .env.example .env
```

→ 這個檔案定義資料庫帳密與連接埠。`.gitignore` 已排除它，不會被 commit。

---

## 3. 建置映像（Image）

```powershell
docker compose build
```

預期輸出（重點在最後）：`Successfully tagged ca-python:dev`、`Successfully tagged ca-dashboard:dev`

**概念：image 與 container 的差別**
- **image** 是唯讀模板（像食譜），**container** 是執行中的實例（像端上桌的菜）
- image 保存套件／volume 保存下載快取
- `build` = 把食譜寫好烤成模板；`up` / `run` = 用它開一份實例
- Dockerfile 裡我們**先 COPY requirements.txt 再 pip install，最後才 COPY 程式碼**：
  這樣改程式碼時 Docker 可以重用「套件已安裝」的快取層，不用每次重裝（這就是 layer cache）

---

## 4. 啟動資料庫

```powershell
docker compose up -d db
docker compose ps
```

預期：`ca_db` 狀態為 **Up (healthy)**。
（`-d` 是 detach，背景執行；`healthy` 來自 compose 裡的 `healthcheck`）

**概念：`db/init/01_schema.sql` 只在「資料庫第一次建立」時執行**
因為 postgres 映像只在資料目錄空的時候跑初始化腳本。之後改 schema 要：

```powershell
docker compose down -v
docker compose up -d db
```

⚠️ `-v` 會**刪除 volume（連資料一起刪）**——這正是 M1 可以放心用的原因（資料都能重新下載）。

---

## 5. 下載資料

```powershell
docker compose run --rm etl python download_data.py
```

預期輸出（實測值）：

```
▶ secom                    1.9 MB    UCI SECOM
▶ phm_sensor_03          385.1 MB    PHM 2018 機台 03 感測器時序
▶ phm_target_03           33.5 MB    TTF 預測目標
▶ phm_truth_03            33.2 MB    TTF 真值
合計約 453 MB
MANIFEST：/data/raw/MANIFEST.json
```

**概念一：`run` vs `up`**
- `up -d db` = 常駐服務（資料庫要一直活著）
- `run --rm etl ...` = **一次性容器**：跑完就刪（`--rm`），適合下載、ETL、分析這種有頭有尾的工作
- 常見錯誤：對 etl 用 `docker compose up etl` → 它會一直重啟（因為沒有常駐用途）

**概念二：為什麼資料不放 `D:\`（bind mount vs named volume）**
Windows 的檔案要進容器，WSL2 得透過 9P 檔案系統轉譯（`/mnt/d/...`）。
小檔沒感覺，但這個專案要**反覆掃描 385 MB 的 CSV**，跨檔案系統會非常慢。
所以 compose 把下載目錄設成容器內的 `/data/raw`，並掛 **named volume `rawdata`**
（存在 WSL2 的 Linux 原生檔案系統）→ 讀取速度差好幾倍。

**概念三：為什麼要掛 `pipcache` volume**
一次性容器每次都是**全新乾淨的環境**。若不像 compose 那樣把 `/root/.cache/pip` 掛出來，
每次 `run` 都得重新下載安裝套件（幾百 MB、數分鐘）。這也是你在 RAG 專案踩過的同一類坑
（compose run 的模型快取必須掛 volume）。

**中斷怎麼辦？** 直接重跑同一行即可——腳本支援 **HTTP Range 續傳**，已下載且大小相符的檔案會跳過。

只想先跑通流程、不想等 385 MB：

```powershell
docker compose run --rm etl python download_data.py --only secom
```

---

## 6. 載入 SECOM 並驗證

```powershell
docker compose run --rm etl python load_secom.py
```

預期輸出（實測值）：

```
  特徵矩陣        : 1567 列 × 590 欄
  標籤            : 1567 筆  →  pass(-1) = 1463、fail(1) = 104
  失敗率          : 6.64%
  時間戳可解析    : 1567/1567
  有缺失值的欄位  : 538/590
  全空欄位        : 0
  常數欄位        : 116
  缺失最嚴重的 5 欄：f0158(91%), f0159(91%), f0293(91%), f0294(91%), f0086(86%)
  驗收條件（1567×590、1463 pass / 104 fail）：✅ 通過
```

**這幾個數字為什麼重要（M3 會用到）**
- **失敗率 6.64%**：典型的類別不平衡。若用 accuracy 評估模型，一個「全部猜良品」的模型就有 93.4% 準確率 → 所以本專案一律用 macro-F1、PR 曲線、偵測率／誤報率
- **538/590 欄有缺失、116 欄是常數**：缺失值與常數欄本身是資料品質問題，也是特徵篩選的第一步
- 想快速驗證而不寫資料庫時：`docker compose run --rm etl python load_secom.py --dry-run`

---

## 7. 剖析 PHM 感測檔（M2 的基礎）

```powershell
docker compose run --rm etl python profile_phm.py --file /data/raw/03_M01_DC_score.csv
```

預期輸出（**我這邊實測的結果**，耗時約 45 秒）：

```
================================================================================
檔案：03_M01_DC_score.csv（385.1 MB）
================================================================================
  欄位數 24｜列數 1,144,073
  時間範圍（原始 time 欄）：52161012 ～ 64499690（跨度 12,338,678 單位 ≈ 3427.4 小時）
  類別欄位唯一值：{'Tool': 1, 'stage': 110, 'Lot': 3081, 'recipe': 31, 'recipe_step': 34}
  數值識別碼唯一值：{'runnum': 1180}
  加工事件數（Tool×Lot×runnum×recipe×recipe_step）：28,960
    每個事件的列數：min=1 median=23.0 max=5382（合計 1,144,073）
  感測欄位：17 個，全部 0% 缺失
    IONGAUGEPRESSURE      min -1.6959  max 4.1507  mean -0.0377
    ETCHBEAMVOLTAGE       min -1.3229  max 1.3096  mean  0.0225
    ...（其餘感測欄皆為匿名化後的 ±2 級距數值）
```

**這份輸出要讀出四件事**
1. **這個檔只有 1 台機台**（`Tool = 03M01`）→ 單機台不能做「機台間」的 commonality。
   M3 要用 `--tools 01,02,03,04,06` 多抓幾台（每台約 385 MB）才能做跨機台比較
2. **`stage` 有 110 個值、`recipe_step` 有 34 個值** → 製程分段很細，特徵必須**按 recipe_step 分段聚合**，
   否則不同物理意義的步驟會被平均掉
3. **3,081 個 lot、1,180 次 runnum、28,960 個加工事件** → 這就是 commonality 的「分母」來源；
   每個事件列數差很大（1 ～ 5,382 列），代表加工時間差異大（**trace 長度不一致 → M5 需要對齊/重採樣**）
4. **感測值全部 0% 缺失、數值約在 ±2 之間** → 資料已正規化且單位被匿名化，
   所以**不能做物理量級的解讀**（例如「0.42 是多大的壓力」不可考），但**相對比較完全有效**

**為什麼要先做這一步**：commonality analysis 的分母是「這片 wafer 經過哪些機台」，
所以要先把「連續的感測器列」切成「**一次加工事件**」（Tool × Lot × runnum × recipe × recipe_step）。
這份剖析確認了事件切分的鍵與粒度——M2 會據此建 `fact_process_event`。

想先快速抽查（不跑完 114 萬列）：

```powershell
docker compose run --rm etl python profile_phm.py --file /data/raw/03_M01_DC_score.csv --max-rows 200000
```

---

## 8. 驗收查詢（資料庫端）

```powershell
docker compose exec db psql -U ca -d commonality -f /docker-entrypoint-initdb.d/../queries/m1_acceptance.sql
```

若上面路徑在容器內找不到（因為 queries 沒掛進 db 容器），改用逐條查詢：

```powershell
docker compose exec db psql -U ca -d commonality -c "SELECT file_key, pg_size_pretty(bytes) AS size, left(sha256,16) AS sha_head FROM raw.data_catalog ORDER BY 1;"
docker compose exec db psql -U ca -d commonality -c "SELECT label, label_name, n, pct FROM qa.secom_label_distribution;"
docker compose exec db psql -U ca -d commonality -c "SELECT count(DISTINCT sample_id) AS samples, count(DISTINCT feature_id) AS features, count(value) AS values FROM raw.secom_features;"
docker compose exec db psql -U ca -d commonality -c "SELECT count(*) AS wide_table_columns FROM information_schema.columns WHERE table_schema='stg' AND table_name='secom_wide';"
```

預期：`fail | 1 | 104 | 6.64`、`samples 1567 / features 590`、`wide_table_columns 591`（含 sample_id）。

---

## 9. 啟動儀表板（可選，但建議做）

```powershell
docker compose up -d dashboard
```

打開 <http://localhost:8501>，預期看到：
- 上方四個指標：資料檔數、SECOM 樣本數 1,567、失敗率 6.64%、欄位數 590
- 左圖：標籤分布（pass 綠 / fail 紅，台灣慣例把不良標紅）
- 右圖：每欄缺失率分布
- 下方：**資料血緣表**（檔案、大小、sha256 前 16 碼、下載時間）

---

## 10. 驗收清單（逐項打勾）

- [ ] `docker version` 兩個版本都印得出來
- [ ] `docker compose build` 成功（ca-python:dev、ca-dashboard:dev）
- [ ] `docker compose ps` 顯示 ca_db **healthy**
- [ ] `download_data.py` 完成，`MANIFEST.json` 有 4 個檔案與 SHA256
- [ ] `load_secom.py` 印出 **✅ 通過**（1567×590、1463 pass / 104 fail）
- [ ] `profile_phm.py` 印出欄位數 24、列數、事件數
- [ ] 驗收查詢：`fail = 104`、`pct = 6.64`、`wide_table_columns = 591`
- [ ] dashboard 在 <http://localhost:8501> 打得開且四個指標正確
- [ ] `git init` + 第一次 commit（見下）

---

## 11. 版本控制

```powershell
git init
git add .
git commit -m "M1: docker env + data acquisition (PHM 2018 tool 03, SECOM) + lineage"
git tag v0.1
```

---

## 12. 疑難排解

| 症狀 | 原因與處理 |
|---|---|
| `port is already allocated` | 5433 被佔用（或你其他專案用掉）→ 改 `.env` 的 `DB_HOST_PORT=5434` 後重跑 |
| `docker compose run etl` 說找不到 image | 還沒 build → 先 `docker compose build` |
| psql 說 `relation "qa.secom_label_distribution" does not exist` | 初始化腳本沒跑到（volume 之前已存在）→ `docker compose down -v` 再 `up -d db` |
| 下載中斷 / 很慢 | 重跑同一行即續傳；或先 `--only secom` 跑通流程（1.9 MB） |
| `profile_phm.py` 跑很久 | 正常（200 萬列純 Python 解析約數分鐘）；先用 `--max-rows 200000` 抽查 |
| Windows 上 `python` 指令找不到 | 所有 Python 都在容器內執行，主機不需要裝 Python |

---

## 13. 完成後請回報這三段的輸出

1. `docker compose ps`（確認 healthy）
2. `docker compose run --rm etl python load_secom.py` 的最後 12 行
3. `docker compose run --rm etl python profile_phm.py --file /data/raw/03_M01_DC_score.csv` 的完整輸出

收到後我會核對是否與我這邊的實測一致，然後進入 **M2（資料模型與對齊引擎）**。

---

## 你應該能回答的概念題（自我檢查）

1. image 與 container 的差別？什麼情況用 `run --rm`、什麼情況用 `up -d`？
2. 為什麼 385 MB 的資料要放 named volume，而不是直接 bind mount `D:\`？
3. 為什麼一次性容器一定要掛 pip 快取 volume？
4. 為什麼 `db/init/*.sql` 只會執行一次？要重跑該怎麼做、代價是什麼？
5. 為什麼要算 SHA256 並寫進 MANIFEST？（提示：資料血緣、可重現、避免資料被換掉而不自知）
6. SECOM 的失敗率 6.64% 對後續建模與評估指標選擇有什麼影響？
