# M2 驗證紀錄

## 助理端（Linux，**無 Docker**）已實際驗證

| 項目 | 方法 | 結果 |
|---|---|---|
| 事件切分分析 | 實跑 `analyze_event_boundaries.py`（1,144,073 列，約 6 分鐘） | ✅ 取樣 4 秒為主（1,114,020 筆）、2 秒 12,393、6 秒 11,873、>60 秒缺口 5,293；相鄰換 lot 僅 5,262 次；runnum 只增不減（1,193 遞增、0 遞減、1,888 單值） |
| 門檻穩健性 | 三種候選鍵 × gap 8/12/20/60/300 | ✅ 三種鍵結果**完全相同**；事件數 29,008→28,985（差 0.08%）→ 邊界由鍵決定，gap 只是安全網 |
| genealogy 建立 | 實跑 `build_genealogy.py --dry-run`（1,144,073 列，83.5 秒） | ✅ **29,002 事件**、1,144,073 個時間點、**493,034 感測特徵**、守恆性通過 |
| 每事件列數 | 從 spool 檔重新統計 | ✅ min 1、median 23、p90 90、max 3,589（與分析階段一致） |
| 切分品質（全量） | 從 spool 檔驗證 | ✅ `stage_n_distinct > 1` 的事件數 = **0**；`gap_max > 20` 的事件數 = **0** |
| spool 檔案大小 | 實測 | events 3.0 MB、features 80.8 MB、trace 362.2 MB |
| 記憶體策略 | 單次串流 + 逐事件寫 spool + COPY | ✅ 全程未把 1,144,073 × 17 放進記憶體（本機僅 2.9 GB RAM 仍可完成） |
| 時鐘偏移壓力測試 | 實跑 `align_qc.py --simulate`（前 300,000 列、8,036 事件） | ✅ 無偏移 100.0000%／0.0017%；+2 秒 97.39%；+30 秒 94.01%；+30 秒 + 30 秒 buffer → 100% 但重複匹配率 18.43%；+300 秒 + 300 秒 buffer → 重複匹配率 80.24% |
| SQL 語法 | `pglast`（真正的 PostgreSQL parser） | ✅ `002_m2_genealogy.sql` 22 個語句、`m2_acceptance.sql` 9 個語句 |
| compose 檔 | `pyyaml` 解析 | ✅ etl 新增 `./db:/work/db:ro` 掛載與 `SPOOL_DIR`／`MIGRATIONS_DIR` 環境變數 |

## 助理端**無法**驗證（需在你的機器上確認）

- **migration 實際套用**：`migrate.py` 與 postgres 的互動（transaction、`schema_migrations` 記錄、`TRUNCATE` 權限）
- **COPY 匯入**：三張事實表的 `COPY ... FROM STDIN WITH (FORMAT csv)` 是否被 postgres 接受
  （欄位順序與型別已對照 schema 檢查，但實際執行需資料庫）
- **`align_qc.py --db`**：所有 SQL 檢查都需要資料庫（語法已用 pglast 驗證）
- 因此 **`align_qc.py --db` 的八項檢查結果需由你回報**；我已用 spool 檔在助理端獨立驗證其中兩項
  （`stage_consistency`、`gap_within_threshold` 皆為 0）

## 實測中發現並修正的缺陷（2026-10-08，使用者端第一次執行 M2）

**症狀**：`build_genealogy.py` 寫入資料庫時報
`psycopg.errors.UndefinedColumn: column "iongaugepressure" of relation "fact_sensor_trace" does not exist`

**根因（助理端責任）**：`002_m2_genealogy.sql` 的寬表與長表檢視把三個感測欄名寫成縮寫——
`iongaugpressure`、`etchauxtimer`、`etchaux2timer`；而 CSV 原始欄名（也是 `build_genealogy.py`
`SENSOR_COLS_ORDER` 使用的名字）是 `IONGAUGEPRESSURE`、`ETCHAUXSOURCETIMER`、`ETCHAUX2SOURCETIMER`。
程式端正確、schema 端錯誤，18 個欄位中錯 3 個 → `COPY` 在檢查第一個欄位時就被 PostgreSQL 拒絕。

**修正方式（含正確的 migration 紀律）**
1. 修正 `002` 的欄名（全新資料庫直接正確）——但**已套用過的資料庫的實體 schema 不會變**（`CREATE TABLE IF NOT EXISTS` 會跳過）
2. 新增 `003_fix_sensor_column_names.sql`：**冪等**修復（舊欄名存在才 `RENAME`）＋重建檢視＋結尾自檢
   （缺任何欄位就整個 migration 失敗，不留半套狀態）
3. `migrate.py` 的 checksum 政策改為**偵測到歷史被改動時「警告 + 跳過」而非重新套用**
   （改動已套用的 migration 後自動重跑，會讓資料庫狀態不可預測）
4. `build_genealogy.py` 新增 `preflight_schema()`：開跑前比對三張事實表欄位，缺欄位就**在寫入任何資料前**
   中止並列出缺哪些欄位、該跑哪個指令

**助理端可驗證的部分（已做）**
- 修正後 `002` 的 DDL 欄位與 `build_genealogy.py` 的 COPY 欄位清單**逐欄逐序完全相同**（19/19、16/16、12/12）
- `003` 通過 `pglast` 語法驗證（3 個語句）
- `preflight_schema()` 的**錯誤路徑**用假 cursor 實測：缺 3 個舊欄名時正確中止（exit 1）並列出全部缺欄
- `preflight_schema()` 的**正常路徑**實測通過
- 修正後重跑 20 萬列 dry-run 煙霧測試：5,239 事件、守恆 ✅（無回歸）

**助理端無法驗證**：`003` 的 `ALTER TABLE ... RENAME COLUMN` 與 `CREATE OR REPLACE VIEW` 在真實資料庫的執行結果
（本機無 Docker）→ 需由使用者回報 `migrate.py` 輸出與 `align_qc.py --db` 結果。

**教訓**：跨檔案（SQL schema ↔ Python COPY 欄位清單）的名稱**必須用機器比對**，不能靠人工抄寫。
已納入 `yield-commonality-analysis` skill 的 Pitfalls。


## 效能缺陷與修正（2026-10-08，使用者端回報 C: 空間被大量消耗）

**症狀**：`align_qc.py --db` 執行時 Windows C: 空間被大量吃掉。

**根因（助理端責任，兩層）**
1. **QC 查詢寫法錯誤**：舊版「重複匹配率」是
   `stg.fact_sensor_trace × stg.fact_process_event` 的
   `ts BETWEEN e.in_ts AND e.out_ts` **非等值 join** →
   1,144,073 × 29,002 ≈ **331 億次比較**（單執行緒 PostgreSQL，跑數小時）
2. **暫存檔落點**：PostgreSQL 的 `work_mem` 預設只有 4 MB，這種量級的
   join／sort／hash 一旦超出就會寫**暫存檔**，而暫存檔在 `PGDATA` 內；
   `PGDATA` 是 named volume `pgdata`，其實體位於 **Docker Desktop 的
   `ext4.vhdx`（在 C:）**。再加上容器內 `/tmp/spool` 的 450 MB（容器可寫層）
   與 WAL，全部記在同一個虛擬磁碟 → C: 一直被吃掉，而且 **vhdx 只增不縮**。

**修正**
- 「覆蓋率」改成 **event_id 等值 join**（走索引）：只掃 114 萬列，不做非等值比較
- 「重複匹配率」改在 **29,002 個事件**層級做掃掠（事件窗約 1.5 MB 拉進 Python），
  只對真正重疊的配對查受影響列數（上限 200 對）→ 取代 331 億次比較
- 所有查詢加上 `SET statement_timeout = '180s'`，每個檢查輸出耗時

**助理端實測（用真實 spool 事件檔驗證新演算法）**
- 事件數 29,002｜同機台重疊事件窗 **2 對**｜掃掠耗時 **0.02 秒**
- 重疊配對：`2360[53251590,53252566] ↔ 2361[53252566,53252570]`、
  `4231[53990238,53990338] ↔ 4232[53990238,53990238]`；各只有 **1 列**受影響
  → `duplicate_match_rate` 預期 **0.0002%**（2 / 1,144,073），屬**資料本身的邊界點接觸**，
  不是切分錯誤 → 驗收門檻因此訂為 **< 0.01%**（仍能抓出百分比量級的真實錯誤）
- `trace_interval_coverage` 預期 **100.0000%**

**誠實揭露**：修好的是**查詢效率**，我無法在自己的機器上量測你那邊的磁碟行為
（本機無 Docker）。C: 空間是否回收需靠你端操作（移動 Docker 磁碟映像檔）。

## 已知限制（誠實揭露）

- 本資料集只有 **1 台機台（03M01）**，所以「跨機台 commonality」在 M3 需要再下載其他機台（每台約 385 MB）；
  `download_data.py --tools 01,02,03,04,05,06` 支援一次抓多台。
- `stg.dim_lot` 的 `Lot` 欄位官方定義為「wafer id」，與真實 fab 的「lot 含 25 片 wafer」階層不同 →
  已用 `source_semantics` 欄位記錄此語意落差，**不可**當成真實 lot 使用。
- 時間欄位是**相對時間**（非絕對時鐘），因此 M2 的所有時間比較都在同一時間軸內進行；
  跨系統對齊（MES vs 機台）的真實情境由 M6 合成資料模擬。
- 時鐘偏移測試的 baseline 重複匹配率為 0.0017%（不是 0）：來自不同 lot 在時間上重疊的邊界情況
  （1,144,073 列中有 5,262 次相鄰換 lot），已在報告中揭露。
