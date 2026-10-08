# data/ — 資料目錄

`data/raw/` 只在「本機直接執行腳本」時使用（例如助理端測試）。
**在 Docker 環境下，原始資料存在 named volume `rawdata`，不會出現在這個資料夾**
（原因見下方「為什麼資料不放這裡」）。

## 檔案清單（由 `etl/download_data.py` 下載，實際大小以 MANIFEST.json 為準）

| 檔名 | 來源 | 說明 | 授權／注意 |
|---|---|---|---|
| `secom.zip` | UCI ML Repository（dataset 179） | `secom.data`（1567×590）、`secom_labels.data`（1567 筆，-1=pass／1=fail）、`secom.names` | 2008 年捐贈，可自由使用；引用請註明 UCI |
| `03_M01_DC_score.csv` | NASA DASHlink（PHM 2018 Data Challenge） | 機台 03 的感測器時序（24 欄、4 秒取樣、200 萬列以上） | PHM Society／Seagate 公開釋出；資料已匿名化，單位未提供 |
| `03_M01_DC_test.csv` | 同上 | TTF 預測目標格式（`time` + 3 個 TTF 欄位） | 同上 |
| `03_M01_DC_groundtruth.csv` | 同上 | TTF 真值（供官方評分公式使用） | 同上 |
| `MANIFEST.json` | 本專案產生 | 每個檔案的 URL、大小、SHA256、下載時間 | 資料血緣依據 |

## 為什麼資料不放這裡（Docker 環境）

Windows 上的檔案透過 WSL2 的 9P 檔案系統提供給容器（也就是 `/mnt/d/...`），
在大量讀取（385 MB 的 CSV 反覆掃描）時效能會明顯變差。
因此 `docker-compose.yml` 把下載目錄設為容器內的 `/data/raw`，
並掛載 **named volume `rawdata`**（存在 WSL2 VM 的 Linux 原生檔案系統）→ 讀取快得多。

指令一律在容器內執行，所以不需要知道資料實際放在哪：

```powershell
docker compose run --rm etl python download_data.py
docker compose run --rm etl python profile_phm.py --file /data/raw/03_M01_DC_score.csv
```

## 誠信與引用

- 所有資料為**公開學術資料集**，內容已匿名化，**不含任何可識別的廠商資訊**。
- 專案文件與程式碼不得以任何方式暗示資料來自特定公司（面試時的誠信紅線）。
- 引用時請註明：PHM Society 2018 Data Challenge、UCI ML Repository SECOM。
