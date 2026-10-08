# M1 驗證紀錄

## 助理端（Linux，**無 Docker**）已實際驗證

| 項目 | 方法 | 結果 |
|---|---|---|
| 資料源可下載 | 實際 `curl`／腳本下載，比對 `Content-Length` | ✅ `secom.zip` 1,964,989 B；`03_M01_DC_score.csv` 403,780,928 B（與 DASHlink 標示的 385.1 MB 一致）；`03_M01_DC_test.csv` 35,130,060 B；`03_M01_DC_groundtruth.csv` 34,852,177 B |
| 下載腳本 | 實跑 `download_data.py`，產生 `MANIFEST.json` | ✅ 4 個檔案 + SHA256 + 下載時間 |
| SECOM 解析與驗收 | 實跑 `load_secom.py --dry-run` | ✅ **1567 列 × 590 欄**、pass **1463** / fail **104**、失敗率 **6.64%**、時間戳 1567/1567 可解析 |
| SECOM 資料品質 | 同上 | 538/590 欄有缺失、116 欄為常數、最缺 5 欄缺失率 91% |
| 入庫 SQL 正確性 | `--emit-sql` + `pglast`（真正的 PostgreSQL parser） | ✅ 動態產生的 **591 欄 CREATE TABLE** 與 COPY 欄位清單語法正確；預計寫入 882,579 筆特徵值（缺失 41,951 筆不寫入，總計 924,530 = 1567×590） |
| schema 檔語法 | `pglast` 解析 | ✅ `01_schema.sql` 18 個語句、`m1_acceptance.sql` 6 個語句 |
| compose 檔 | `pyyaml` 解析 | ✅ 4 個服務（db/etl/dashboard/jupyter）、3 個 volume、jupyter 走 `notebooks` profile |
| PHM 檔案剖析 | 實跑 `profile_phm.py`（1,144,073 列，耗時 44 秒） | ✅ 24 欄、**28,960 個加工事件**、3,081 lot、31 recipe、17 感測欄 0% 缺失 |
| 測試中發現的缺陷 | — | `runnum`（數值識別碼）原先被誤列為感測欄 → 已修正為獨立統計 |

## 助理端**無法**驗證（需在你的機器上確認）

- **Docker 容器層**：這個環境沒有安裝 Docker，因此 image build、`up -d db` 的 healthcheck、
  `run --rm etl` 的實際執行、postgres 初始化腳本是否被觸發、dashboard 網頁呈現，
  都要由你在 Windows + Docker Desktop 上驗證。
- 若上述任一項失敗，請把完整錯誤訊息貼給我（含 `docker compose ps` 與出錯指令的輸出）。

## 預期與實測的差異說明

- `load_secom.py` 我這邊跑的是 `--dry-run`／`--emit-sql`（沒有資料庫可連），
  因此**「順利 commit 到 postgres」這一步未實測**；但寫入所使用的 SQL 語句已用 PostgreSQL
  parser 驗證過語法，且 COPY 的資料列內容已抽樣檢查（`sample_id=1 → label=-1, 2008-07-19 11:55:00`）。
