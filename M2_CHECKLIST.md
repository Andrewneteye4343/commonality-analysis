# M2 Checklist — 資料模型與對齊引擎（genealogy 事實表）

> 全部指令在 **Windows PowerShell** 執行（單行、不使用 `$` 變數）。
> 預計時間：**30–60 分鐘**（其中 genealogy 建立約 3–6 分鐘，取決於磁碟速度）。

---

## 0. 同步新版專案檔（重要：這次解壓到 **上一層**）

```powershell
Expand-Archive -Path "$env:USERPROFILE\Downloads\commonality-analysis-m2.zip" -DestinationPath D:\repos -Force
cd D:\repos\commonality-analysis
dir
```

預期看到新檔案：`etl/migrate.py`、`etl/analyze_event_boundaries.py`、`etl/build_genealogy.py`、`etl/align_qc.py`、`db/migrations/002_m2_genealogy.sql`、`db/queries/m2_acceptance.sql`、`M2_CHECKLIST.md`

⚠️ **解壓到 `D:\repos`（上一層），不是解壓進 `D:\repos\commonality-analysis`** —— 上次就是這樣多長出一層 `commonality-analysis\commonality-analysis\`。

改動了 `docker-compose.yml`（etl 服務新增 `./db:/work/db:ro` 掛載與 `SPOOL_DIR`），所以要重建：

```powershell
docker compose up -d --force-recreate etl db
```

---

## 1. 事件切分的實證分析（先看資料，再動手）

```powershell
docker compose run --rm etl python analyze_event_boundaries.py --file /data/raw/03_M01_DC_score.csv
```

預期輸出（**我這邊實測的結果**，約 6 分鐘）：

```
① 取樣間隔分布
              4 :  1,114,020
              2 :     12,393
              6 :     11,873
     > 60（缺口）:      5,293
② 每個 Lot 的列數分布
   lot 數 3081｜min 67｜median 272｜p90 732｜max 7273
   相鄰列換 lot 的次數：5,262 / 1,144,073（大致連續）
③ runnum 行為：{'lots': 3081, 'increasing': 1193, 'decreasing': 0, 'single_value': 1888}
④ stage 數 110｜recipe_step 數 34
⑤ 事件切分（三種候選鍵結果相同）
   tool_lot_recipe_step   gap 8 → 29,008｜gap 20 → 29,002｜gap 300 → 28,985
```

**這份輸出要讀出什麼（M2 的決策依據）**
1. **取樣以 4 秒為主**，但存在 2 秒／6 秒的抖動 → gap 門檻不能設太小（設 4 秒會把抖動誤切成兩段）
2. **runnum 只增不減、且一個 lot 內常只有 1～2 個值** → 它是**機台累積使用次數**，不是事件 ID（真實 fab 的 runnum 常用於機台老化分析，M7 預測會用到）
3. **事件數對 gap 門檻不敏感**（8→300 秒只差 0.08%）→ 這代表**事件邊界其實由鍵決定**，gap 只是防止把中斷誤接在一起的安全網。**穩健的切分**就是這個意思：換個合理門檻結果幾乎不變
4. **stage（110 值）與 recipe_step（34 值）是多對多**，不是同一件事 → 事件鍵用 recipe_step，stage 當檢查欄位（同一事件內 stage 必須唯一）

---

## 2. 套用 migration（版本化 schema）

```powershell
docker compose run --rm etl python migrate.py
```

**全新資料庫**會依序套用：`[apply] 002_m2_genealogy.sql` → `[apply] 003_fix_sensor_column_names.sql`。

**你的資料庫**（已套用過 002）預期看到：

```
  [警告] 002_m2_genealogy.sql 內容與已套用版本不同（歷史被改動）→ **不重新套用**；要改 schema 請新增下一個版本檔（004_…）
  [apply] 003_fix_sensor_column_names.sql …
          NOTICE: 已修正欄位：iongaugpressure → iongaugepressure
          NOTICE: 已修正欄位：etchauxtimer → etchauxsourcetimer
          NOTICE: 已修正欄位：etchaux2timer → etchaux2sourcetimer
          NOTICE: schema 自檢通過：stg.fact_sensor_trace 具備全部 17 個感測欄位
          完成
```

⚠️ 那個 002 的警告是**預期行為**：我在 002 裡把三個感測欄名寫成縮寫（`iongaugpressure`／`etchauxtimer`／`etchaux2timer`），與 CSV 原始欄名不符，已修正；因為你的資料庫記錄的 checksum 是舊版，migrate 會**跳過不重套**（改動歷史不該自動重跑），改由 **003** 冪等地修復欄名。這是刻意的設計，不是錯誤。

再跑一次會全部顯示 `[skip]`——**這就是 migration 的價值：不用 `down -v` 砍掉整個資料庫**。

**概念：為什麼不用 `down -v` 重建？**
M1 沒差（資料能重下載），但接下來每加一張表都砍掉重建，等於**每次都把已載入的 1,144,073 列重跑一遍**。Migration 把「schema 變更」變成**可累積、可追溯**的歷史（`public.schema_migrations` 記錄版本與 checksum）。

---

## 3. 建立 genealogy 事實表

```powershell
docker compose run --rm etl python build_genealogy.py --file /data/raw/03_M01_DC_score.csv
```

預期輸出（**我這邊實測的結果**）：

```
  讀入列數      : 1,144,073
  加工事件數    : 29,002
  事件內時間點  : 1,144,073
  加工單位(lot) : 3,081
  感測器數      : 17
  感測特徵列數  : 493,034（= 事件數 × 感測器數）
  耗時          : 83.5s
  spool 檔案    : events=3.0 MB, features=80.8 MB, trace=362.2 MB
  守恆性檢查（事件內時間點 == 讀入列數）：✅ 通過
```

**三個設計取捨（面試會問）**

1. **為什麼不是「把整檔讀進 DataFrame」？**
   1,144,073 列 × 17 感測器留在 Python 物件裡會吃掉數 GB 記憶體。作法是**單次串流掃描 + 逐事件寫入 spool CSV**，最後用 PostgreSQL 的 `COPY` 批次匯入（比 `INSERT` 快一個數量級）。

2. **為什麼感測時序用寬表而不是長表？**
   長表會是 **1,940 萬列**（1,144,073 × 17），體積與索引成本都高；寬表只要 114 萬列。需要長表時用 `stg.v_sensor_trace_long`（`unnest` unpivot）即可。

3. **為什麼事件 id 在掃描時就決定？**
   事件切分與 trace 歸屬必須在同一次掃描完成（否則要重讀 385 MB）。事件一開啟就配號，trace 直接帶著該號寫入。

---

## 4. 對齊品質檢查

```powershell
docker compose run --rm etl python align_qc.py --db
```

預期輸出（8 項檢查，全部應為 ✅）：

```
  row_conservation             ✅ trace 列數 1,144,073 vs 事件 n_points 合計 1,144,073
  feature_completeness         ✅ 493,034 = 事件 29,002 × 感測器 17
  lot_coverage                 ✅ 有事件的 lot 3,081 / 全部 lot 3,081
  trace_interval_coverage      ✅ 100.0000%（偏移 0 筆；門檻 95%）
  duplicate_match_rate         ✅ 0.0002%（門檻 < 0.01%；重疊事件窗 2 對、受影響 2 列）
  stage_consistency            ✅ 單一事件內 stage 不唯一的事件數 = 0
  gap_within_threshold         ✅ 事件內最大相鄰時間差 > 20 秒的事件數 = 0
  events_without_trace         ✅ 沒有任何 trace 資料列的事件數 = 0

各步驟耗時（秒）：…（每個檢查都會印耗時，總計應在數十秒內）

那 2 對重疊事件窗是**資料本身的邊界點接觸**（兩者共用同一個時間點），不是切分錯誤：
`events 2360[53251590,53252566]` 與 `2361[53252566,53252570]`、`4231[53990238,53990338]` 與
`4232[53990238,53990238]`，各只有 1 列受影響 → 2 / 1,144,073 = 0.0002%。
門檻訂在 < 0.01% 就是為了容納這種資料實況，但仍能抓出真正的對齊錯誤（會是百分比量級）。
```

---

## 5. 時鐘偏移壓力測試（這一題是 M2 的靈魂）

```powershell
docker compose run --rm etl python align_qc.py --simulate --file /data/raw/03_M01_DC_score.csv --rows 300000
```

預期輸出（**我這邊實測**）：

| 情境 | 覆蓋率 % | 重複匹配率 % |
|---|---|---|
| 無偏移、無 buffer | **100.0000** | 0.0017 |
| 時鐘快 2 秒、無 buffer | 97.3867 | 0.0007 |
| 時鐘慢 2 秒、無 buffer | 97.3893 | 0.0000 |
| 時鐘快 30 秒、無 buffer | 94.0143 | 0.0003 |
| 時鐘快 30 秒、buffer 30 秒 | **100.0000** | **18.4323** |
| 時鐘快 300 秒、buffer 30 秒 | 89.2673 | 14.9833 |
| 時鐘快 300 秒、buffer 300 秒 | 100.0000 | **80.2433** |

**怎麼讀這張表（這是本里程碑最重要的一段）**
- 無偏移時覆蓋率 100% → 事件切分與 trace 歸屬一致（baseline 正確）
- **偏移只要超過取樣間隔（4 秒），覆蓋率就開始掉**（2 秒偏移掉到 97.4%）→ 說明 QC 真的抓得到錯位，不是擺設
- **加 buffer 可以把覆蓋率救回 100%，但重複匹配率會爆掉**（30 秒 buffer → 18.4%；300 秒 → 80.2%）
- 結論：**buffer 不是越大越好**。要同時看「覆蓋率」與「重複匹配率」兩個指標，取兩者的折衷 —— 這正是為什麼真實 fab 的對齊工作要花時間，也是為什麼「只看覆蓋率」的 QC 會誤導人
- 那個 0.0017% 的殘留重複匹配不是 bug：它來自**不同 lot 在時間上重疊**的邊界情況（實測 1,144,073 列中有 5,262 次相鄰換 lot），列在報告裡誠實揭露

---

## 6. 驗收查詢

```powershell
docker compose exec db psql -U ca -d commonality -c "SELECT (SELECT sum(n_points) FROM stg.fact_process_event) AS points_in_events, (SELECT count(*) FROM stg.fact_sensor_trace) AS trace_rows;"
docker compose exec db psql -U ca -d commonality -c "SELECT count(*) AS events, min(n_points), percentile_cont(0.5) WITHIN GROUP (ORDER BY n_points) AS median, max(n_points) FROM stg.fact_process_event;"
docker compose exec db psql -U ca -d commonality -c "SELECT check_name, metric, unit, passed FROM qa.alignment_quality ORDER BY 1;"
docker compose exec db psql -U ca -d commonality -c "SELECT sensor, round(mean_val::numeric,4) AS mean, round(slope::numeric,6) AS slope FROM stg.fact_sensor_feature WHERE event_id = 1 ORDER BY sensor LIMIT 5;"
docker compose exec db psql -U ca -d commonality -c "SELECT ts, sensor, value FROM stg.v_sensor_trace_long WHERE event_id = 1 AND sensor='FLOWCOOLPRESSURE' ORDER BY ts LIMIT 5;"
```

預期：`points_in_events = trace_rows = 1144073`；`events = 29002`、median 23、max 3589；QC 全 passed。

完整 9 條驗收查詢在 `db/queries/m2_acceptance.sql`。

---

## 7. 推送（每個里程碑的慣例）

```powershell
git add .
git commit -m "M2: genealogy event table + sensor trace/features + alignment QC with clock-skew test"
git tag v0.2
git push origin main
git push origin v0.2
```

---

## 8. 驗收清單

- [ ] `migrate.py` 顯示 `[apply] 002_m2_genealogy.sql`，重跑變 `[skip]`
- [ ] `build_genealogy.py` 印出 **29,002 事件**、**493,034 特徵**、守恆性 ✅
- [ ] `align_qc.py --db` 八項檢查全 ✅（覆蓋率 ≥ 95%、重複匹配率 = 0%）
- [ ] `align_qc.py --simulate` 的覆蓋率／重複匹配率趨勢與上表一致
- [ ] 驗收查詢：`points_in_events = trace_rows = 1144073`
- [ ] commit + tag `v0.2` + push

---

## 9. 疑難排解

| 症狀 | 處理 |
|---|---|
| `UndefinedColumn: column "iongaugepressure" ... does not exist` | 這是 002 初版的欄名筆誤，**已修正** → 重取新版專案後跑 `docker compose run --rm etl python migrate.py`（會套用 003 修復欄名） |
| `[錯誤] schema 與程式預期不符` | `build_genealogy.py` 的開跑前預檢擋下來的訊息（會列出缺哪些欄位）→ 先跑 `migrate.py` |
| `relation "stg.fact_process_event" does not exist` | 沒跑 migration → `docker compose run --rm etl python migrate.py` |
| `migrate.py` 說找不到 migration 目錄 | compose 未更新（缺 `./db:/work/db:ro`）→ `docker compose up -d --force-recreate etl db` |
| `COPY` 失敗、欄位數不符 | schema 是舊版 → `docker compose down -v` 後從 M1 重跑（migration 002 會重新套用） |
| genealogy 跑到一半中斷 | 重跑即可（先 TRUNCATE 再 COPY，不會產生重複資料） |
| `align_qc.py --simulate` 很慢 | 用 `--rows 100000` 縮小樣本 |
| **C: 空間被吃掉** | ① PostgreSQL 的暫存檔（`pg_stat_database.temp_bytes`）與 `pgdata` volume 都實體位於 **Docker 的 ext4.vhdx（在 C:）**；② 容器 `/tmp/spool` 約 450 MB 屬容器可寫層，也記在這個磁碟；③ ext4.vhdx **只增不縮**。處理：停掉吃資源的查詢 → `docker system df -v` 看用量 → Docker Desktop 設定把 Disk image location 移到 D: 後重啟 |
| `align_qc.py --db` 跑很久、C: 一直被吃 | 舊版 QC 有一條 `ts BETWEEN in_ts AND out_ts` 的非等值 join（331 億次比較）→ **已修正**為等值 join + 事件層級掃掠（秒級完成） |

---

## 10. 概念題（M3 前我會確認你懂）

1. 為什麼事件切分的門檻要用**資料**決定，而不是直接抄手冊寫「30 秒」？你要怎麼證明你選的門檻是穩健的？
2. 這份資料的 **`runnum` 為什麼不能當事件 ID**？那它可以拿來做什麼分析？
3. 感測時序用**寬表**（114 萬列）而不是**長表**（1,940 萬列），代價是什麼？（提示：查詢彈性 vs 儲存與索引）
4. 為什麼 genealogy 建立時要先把資料寫到 **spool CSV**，而不是直接 `INSERT` 進資料庫？
5. 時鐘偏移測試裡，為什麼「覆蓋率 100%」不能證明對齊正確？還要看哪個指標？
6. 有了 migration 之後，什麼情況下**仍然必須**用 `docker compose down -v`？代價是什麼？
