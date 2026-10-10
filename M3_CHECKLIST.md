# M3 Checklist — 基礎共同性分析（把「猜」變成「算」）

> 全部指令在 **Windows PowerShell** 執行（單行、不使用 `$` 變數）。
> 第一段（引擎，用現有 03M01）約 **20 分鐘**；第二段（多機台）含下載約 **1–2 小時**（下載可放背景）。

---

## 0. 重取新版專案（**解壓到上一層**）＋**版本確認**＋重建 image／容器

```powershell
Expand-Archive -Path "$env:USERPROFILE\Downloads\commonality-analysis-m3.zip" -DestinationPath D:\repos -Force
cd D:\repos\commonality-analysis
docker compose run --rm etl python build_all_tools.py --version
docker compose build etl
docker compose up -d --force-recreate etl db
```

**第一步就是版本確認**：應印出 `build_all_tools.py version 2026-10-09.r3`。
若出現 `unrecognized arguments: --version` 或版本不符 → **檔案沒更新到**（不是程式問題），
請重新解壓（確認解壓到 `D:\repos`，不是解壓進專案資料夾裡）。
所有交付的腳本都支援 `--version`，就是為了讓「跑的是哪一版」變成可驗證的事實。

⚠️ **兩件事都要做，不能只做一件**：
- `docker compose build etl`：這次 `etl/requirements.txt` 新增了 **matplotlib**（畫 Pareto／森林圖）與
  **pytest**（跑單元測試）。套件是在 **image 層**，沒 rebuild 就會出現
  `No module named pytest` 或畫圖失敗。
- `docker compose up -d --force-recreate etl db`：`docker-compose.yml` 新增 `./analysis`、`./reports` 掛載
  → 重建容器才會生效。

> `up -d --force-recreate` **只重建容器，不會重建 image**；`build` 才是重裝套件。
> 只要沒動 Dockerfile／requirements，就不用再 build。
多了掛載的原因：`analysis/` 是 M3 的統計程式（容器內用 `python -m analysis.xxx` 呼叫），
而 `./reports` 是為了讓報告**落地到專案資料夾**（否則 `run --rm` 一結束就隨容器消失）。

---

## 1. 套用 migration 004（M3 的資料模型）

```powershell
docker compose run --rm etl python migrate.py
```

預期：`[apply] 004_m3_commonality.sql`、`[apply] 005_m3_method_eval_nullable.sql`
（005 只是把「負控制怎麼表示」以 COMMENT 寫進資料庫，屬文件化變更，不動任何結構），
物件清單應新增
`mart.fault_events`、`mart.event_labels`、`mart.lot_units`、`mart.lot_membership`、
`mart.commonality_results`、`mart.method_eval`、`v_lot_outcome_summary`。

---

## 2. 單元測試（**容器內會多跑 scipy 對照**）

```powershell
docker compose run --rm etl python -m pytest analysis/tests -q
```

預期：**29 passed**（助理端 28 項通過 + scipy 對照 1 項；容器有 scipy 所以全跑）。

> 測試策略：用 `itertools` **暴力列舉**驗證超幾何分布與雙尾 Fisher（與我實作的公式演算法不同），
> 並用剛體不變式（`p_hyper == p_fisher_greater`）與 scipy 對照。
> 這組測試在助理端**真的抓到一個 bug**：`or_ci_woolf` 的 z 值取錯尾端 → 信賴區間上下界顛倒（已修）。

---

## 3. 標籤：從 TTF 反推故障時刻 → 批次層級標籤

```powershell
docker compose run --rm etl python load_fault_labels.py --raw /data/raw --window-hours 24
```

預期輸出（**我這邊用相同資料實測的值**）：

```
機台 03M01｜事件 29,002 筆
  故障 FlowCool Pressure Dropped Below Limit   時刻 52,262,284｜觀測 13,168 列｜不確定度 ±0 秒
  故障 FlowCool Pressure Dropped Below Limit   時刻 55,447,568｜觀測 296,397 列｜不確定度 ±0 秒
  故障 FlowCool Pressure Dropped Below Limit   時刻 58,505,880｜觀測 326,490 列｜不確定度 ±0 秒
  故障 FlowCool Pressure Dropped Below Limit   時刻 59,198,524｜觀測 53,713 列｜不確定度 ±0 秒
  故障 FlowCool Pressure Dropped Below Limit   時刻 59,223,272｜觀測 2,825 列｜不確定度 ±0 秒
  故障 FlowCool Pressure Dropped Below Limit   時刻 59,253,116｜觀測 6,473 列｜不確定度 ±0 秒
  故障 FlowCool Pressure Dropped Below Limit   時刻 60,412,412｜觀測 116,986 列｜不確定度 ±0 秒
  故障 FlowCool Pressure Dropped Below Limit   時刻 60,425,592｜觀測 2,319 列｜不確定度 ±0 秒
  故障 Flowcool Pressure Too High Check Flowcool Pump 時刻 52,304,268｜觀測 14,548 列
  故障 Flowcool Pressure Too High Check Flowcool Pump 時刻 59,256,192｜觀測 685,132 列
  故障 Flowcool Pressure Too High Check Flowcool Pump 時刻 60,608,960｜觀測 129,872 列
  故障 Flowcool leak                           時刻 59,963,144｜觀測 784,931 列

批次層級標籤（跨機台合併，窗 24 小時）
  壞批次定義 [ANY]              ：  216 / 3,081 =   7.01%（窗內事件 1,312 筆）
  壞批次定義 [FlowCool Pressure Dropped Below Limit]：172 / 3,081 = 5.58%
  壞批次定義 [Flowcool Pressure Too High …]          ： 69 / 3,081 = 2.24%
  壞批次定義 [Flowcool leak]                          ： 36 / 3,081 = 1.17%
```

**三個要讀懂的地方**

1. **`不確定度 ±0 秒`** 代表 `t + TTF` 精確為常數——這驗證了 TTF 不變量確實成立（不是近似）
2. **工具 03 在記錄期間有 12 次故障注入**（不是 3 次）：單一故障類型會發生多次，
   用「t+TTF 出現幾個聚類」自動判定次數，不必事先知道故障時刻
3. **獨立合理性檢查**：壞批次 7.01%；而 12 次故障 × 24 小時 ÷ 記錄總長 3,427 小時 ≈ **8.4%**
   ——兩個完全獨立的算法吻合，代表標籤沒有系統性偏差

---

## 4. 共同性排名（單機台）

```powershell
docker compose run --rm etl python -m analysis.run_commonality --window-hours 24 --fault-type ANY
```

預期（**實測值**）：

```
共同性分析｜分析單位 3,081 批次｜壞批次 216｜檢定群組 194
  未校正顯著 59（純巧合期望 9.7）｜Bonferroni 後 2｜BH-FDR 後 29

  群組類型              群組        n     k    群組壞率   其他壞率      OR     p_hyper        q_bh
  stage               104        14     6     42.9%     6.8%   10.20   2.066e-04   9.728e-03
  stage               1         329    40     12.2%     6.4%    2.03   2.349e-04   9.728e-03
  recipe              215       326    39     12.0%     6.4%    1.98   4.012e-04   9.728e-03
  recipe              5          43     9     20.9%     6.8%    3.62   2.388e-03   2.976e-02
```

產出：`reports/m3/commonality_ranking.csv`、`commonality_summary.md`、`fdr_summary.json`、
`pareto_top15.png`、`forest_top15.png`（後兩張需要 matplotlib，容器內已安裝）。

**這段最該記住的結論**：未校正顯著 **59** 個，但純巧合的期望值是 **9.7** 個——
看起來「很多都很顯著」，實際上要交付的是 **BH 校正後的 29 個**。這就是為什麼共同性分析一定要做校正。

---

## 5. 方法評估（負控制 + 正控制，本專案的量化核心）

```powershell
docker compose run --rm etl python -m analysis.eval_commonality --replicates 100 --effect-ratio 3
```

預期（**實測值**，跑約 1 分鐘；用真實群組結構、只模擬 outcome）：

| 群組類型 | 情境 | 注入群組 | @1（樂觀） | @1（嚴格） | @3 | 名次範圍 | 並列數 | BH 顯著 | 偽發現率 |
|---|---|---|---|---|---|---|---|---|---|
| recipe | **null（負控制）** | — | — | — | — | — | — | **0.03** | 0.001 |
| recipe | injected | 3 | **100%** | **100%** | 100% | 1.0–1.0 | 1.0 | 1.03 | 0.001 |
| recipe_step | null | — | — | — | — | — | — | **0.00** | 0.000 |
| recipe_step | injected | 3 | **0%** | 0% | **0%** | **11.0–13.0** | 3.0 | 0.12 | 0.004 |
| recipe_step_x_stage | null | — | — | — | — | — | — | 0.22 | 0.001 |
| recipe_step_x_stage | injected | 3\|4 | **100%** | **0%** | **100%** | **1.0–5.0** | **5.0** | 5.46 | 0.018 |
| stage | null | — | — | — | — | — | — | 0.02 | 0.000 |
| stage | injected | 1 | **100%** | **100%** | 100% | 1.0–1.0 | 1.0 | 1.02 | 0.000 |
| tool | null | — | — | — | — | — | — | 0.00 | 0.000 |
| tool | injected | 03M01 | 100% | 100% | 100% | 1.0–1.0 | 1.0 | 0.00 | 0.000 |

**名次為什麼有兩個版本（很重要）**：群組成員高度重疊時，很多群組的 p 值**完全相同**
（例：`215|1`～`215|5` 的 p 都是 4.012e-04）。此時「第 1 名」沒有定義——
用 `sorted()` 排名會取決於資料庫回傳順序，**指標不可重現**（實測同一份資料、同一個 seed，
不同執行得到 @1 = 0% 與 34% 兩種結果）。因此改用順序無關的定義：

- `@1（樂觀）`＝ 沒有任何群組的 p **嚴格小於**目標群組（最樂觀名次為 1）
- `@1（嚴格）`＝ 目標群組是**唯一**最小（連並列都沒有）
- `名次範圍`／`並列數`：`rank_min–rank_max` 與並列群組數，直接量化「歸因有多不確定」

**怎麼讀（這是 M3 最重要的一段）**

1. **負控制全過**：完全沒有根因時，BH 校正後平均只剩 **0.00–0.22** 個假陽性（偽發現率 ≪ 5%）
   → 這份排名清單是「可交付」的
2. **正控制大多很強**：recipe / stage 注入後 **100% 排第 1**
3. **`recipe_step` 注入後完全抓不到（0%、平均名次 12）** ← 這是**真實且重要的失敗**，原因不是程式錯：
   一個批次會經過多個步驟，`recipe_step` 各群組的成員高度重疊（共線性）→
   把壞率抬到步驟 3，也會同時抬起步驟 1、2、4… → **辨識不能被辨識（not identifiable）**。
   這正是 **M4 要用 logistic regression（同時放多個步驟）取代「一次測一個群組」** 的實證理由。
4. **`recipe_step_x_stage` 的 `@1` 樂觀 100%、嚴格 0%、並列數 5** ← 這是最誠實的一格：
   目標群組**幾乎每次都是並列最優**（訊息在），但平均與 **5 個**群組並列同一個 p 值
   → **可以說「這一組群組有問題」，不能說「就是 `3|4` 這一個」**。共線性讓歸因變成「一組嫌疑犯」。
5. **`tool` 的檢定力 = 0**（只有一台機台，無從比較）→ 所以下一段要多機台。

---

## 6. 第二段：多機台（跨機台 tool commonality）

### 6.1 下載其餘 4 台機台（**+1.9 GB，可放背景跑**）

```powershell
docker compose run --rm etl python download_data.py --tools 01,02,04,06
```

> **PowerShell 參數陷阱（已修）**：PowerShell 會把 `01,02,04,06` 當成**數字陣列**，
> 傳進程式時前導零消失（變成 `1,2,4,6`）→ 舊版的機台比對會失敗
> （`[錯誤] 未知的機台編號 1`）。現在 `--tools` 會自動補前導零，
> 下列寫法都可：`--tools 01,02,04,06`、`--tools 1,2,4,6`、`--tools 01 02 04 06`。
> 想完全避免轉換，也可以加引號：`--tools "01,02,04,06"`。

會抓 4 台 × 3 檔（`_score` 感測時序、`_test` TTF 目標、`_groundtruth` TTF 真值）：

| 機台 | 模組 | score 檔大小 | 預估列數 |
|---|---|---|---|
| `01_M02` | M02 | 584 MB | ≈ 173 萬 |
| `02_M02` | M02 | 473 MB | ≈ 141 萬 |
| `04_M01` | M01 | 573 MB | ≈ 170 萬 |
| `06_M01` | M01 | 430 MB | ≈ 128 萬 |

**這 4 台的故障狀況（我已用 HTTP Range 讀 groundtruth 的頭尾確認）**：
`01_M02` 三類故障都發生、`02_M02` 只有「壓力過高」、`06_M01` 只有「壓力過高」、
**`04_M01` 整份記錄完全沒有故障（最乾淨的負控制）**。

### 6.2 一次載入所有機台

```powershell
docker compose run --rm etl python build_all_tools.py --reset
```

**為什麼要加 `--reset`**：多機台載入是「逐檔 append」，任何一個檔案中途失敗都會讓
**事實表與維度表不一致**（維度表在每個新載入週期會被清空，但事實表是累加的——
實際踩過：tool 02 因 `qa.alignment_quality` 主鍵衝突而整個交易回滾，結果事實表有兩台、
`stg.dim_lot` 卻只剩一台）。`--reset` 會先清空 `stg` 事實／維度表與 `qa` 檢查表，
再以乾淨狀態載入全部 5 台；**約 7–10 分鐘**。

冪等設計：**不加 `--reset` 時，已載入的機台會自動跳過**（不會重複載入）；
要強制重載請用 `--reset`。

這支是為多機台寫的（`build_genealogy.py` 單檔會 TRUNCATE，跑第二台會蓋掉第一台）：
- **事件 ID 自動位移**（避免不同機台的事件撞號）
- 維度表走「暫存表 + upsert」；**同一批 lot 被兩台機台加工過時，統計量累加而非覆蓋**

### 6.3 驗收查詢（一次跑完 13 段）

```powershell
docker compose run --rm etl python run_query.py db/queries/m3_acceptance.sql
docker compose run --rm etl python run_query.py db/queries/m3_acceptance.sql --only 10,12
```

重點三段：**10）共用批次是否真的共用**（時間是否重疊 → 判定 ID 是否全域唯一）、
**12）機台層級排名**（有故障機台靠前、04M01 不進前 3）、
**13）排除共用批次後的排名**（乾淨歸因，需先跑 `--exclusive-lots` 版本）。

### 6.4 重新產生標籤（含多機台）並跑 tool 層級共同性

> `--reset` 之後 **event_id 會全部重編**，所以一定要重新跑標籤（第 3 步）再排名。

```powershell
docker compose run --rm etl python load_fault_labels.py --raw /data/raw --window-hours 24
docker compose run --rm etl python -m analysis.run_commonality --window-hours 24 --fault-type ANY --group-types tool
docker compose exec db psql -U ca -d commonality -c "SELECT group_key AS tool, n_group, k_bad, round(p_bad_in_group::numeric,4) AS p_in, p_hyper, q_bh FROM mart.commonality_results WHERE group_type='tool' AND testable ORDER BY p_hyper;"
```

**M3 的驗收條件（ROADMAP 明列）**：
已知發生 Flowcool 故障的機台（`01M02`／`02M02`／`03M01`／`06M01`）應排在前面；
**完全無故障的 `04M01` 不得進前 3 名**。

---

## 7. 驗收查詢

`db/queries/m3_acceptance.sql` 共 9 條，關鍵 4 條：

```powershell
docker compose exec db psql -U ca -d commonality -c "SELECT tool_id, fault_type, count(*) AS n_faults FROM mart.fault_events GROUP BY 1,2 ORDER BY 1,2;"
docker compose exec db psql -U ca -d commonality -c "SELECT * FROM mart.v_lot_outcome_summary ORDER BY 1,2,3;"
docker compose exec db psql -U ca -d commonality -c "SELECT count(*) FILTER (WHERE testable) AS n_tests, count(*) FILTER (WHERE testable AND p_hyper<0.05) AS n_raw, count(*) FILTER (WHERE testable AND q_bh<0.05) AS n_bh FROM mart.commonality_results;"
docker compose exec db psql -U ca -d commonality -c "SELECT scenario, group_type, target_group, detection_top3, round(fpr_bh_mean::numeric,4) AS fpr FROM mart.method_eval ORDER BY 1,2;"
```

---

## 8. 推送

```powershell
git add .
git commit -m "M3: commonality engine (hypergeometric/Fisher + BH-FDR) + TTF-derived lot labels + method evaluation"
git tag v0.3
git push origin main
git push origin v0.3
```

---

## 9. 驗收清單

- [ ] `migrate.py` 顯示 `[apply] 004_m3_commonality.sql` 與 `[apply] 005_m3_method_eval_nullable.sql`
- [ ] `pytest` → **29 passed**（含 scipy 對照）
- [ ] `load_fault_labels.py` → **12 次故障**、壞批次 **216 / 3,081 = 7.01%**、不確定度 ±0 秒
- [ ] `run_commonality` → 檢定群組 **194**、未校正顯著 **59**（巧合期望 9.7）、Bonferroni **2**、**BH 29**
- [ ] `eval_commonality` → 負控制 FPR ≈ 0，正控制 recipe/stage **@1 100%**、
      `recipe_step` 名次 11–13（辨識不能）、`recipe_step_x_stage` 並列數 5
- [ ] `mart.method_eval` 有寫入（負控制的 `target_group` 為 `(none)`，共 10 列：5 群組 × 2 情境）
- [ ] （第二段）`build_all_tools.py` 各機台列數合理、守恆性全過
- [ ] （第二段）tool 排名：有故障機台在前段、**`04M01` 不在前 3**
- [ ] commit + tag `v0.3` + push

---

## 10. 疑難排解

| 症狀 | 處理 |
|---|---|
| `unrecognized arguments: --reset`（或任何新參數） | 你手上的檔案較舊 → 重新解壓，並用 `--version` 確認版本（應為 `2026-10-09.r3`） |
| `No module named pytest` / `No module named matplotlib` | image 沒重建（套件在 image 層）→ `docker compose build etl` |
| `python: can't open file '/work/analysis/...'` | compose 沒重建（缺 `./analysis` 掛載）→ `docker compose up -d --force-recreate etl db` |
| 報告找不到（`reports/m3/…`） | 同上（缺 `./reports` 掛載）→ 重建容器 |
| `relation "mart.lot_units" does not exist` | 沒跑 migration 004／005 → `docker compose run --rm etl python migrate.py` |
| `NotNullViolation: null value in column "target_group"` | 已修：負控制改寫入哨兵值 `(none)`。原因是 `target_group` 屬主鍵的一部分，**主鍵欄位隱含 NOT NULL**（連 `DROP NOT NULL` 也會被 PostgreSQL 拒絕：`column "target_group" is in a primary key`）→ 用哨兵值比動主鍵安全 |
| 同一份資料、同一個 seed 但名次不同 | 共線性造成大量並列 p 值 → 用 `@1（樂觀）/（嚴格）` 與「並列數」判讀，不要引用單一 sorted 名次 |
| `[錯誤] 沒有任何事件被標籤` | 機台 ID 寫法不一致（檔名 `03_M01` vs 資料 `03M01`）→ 已用 `normalize_tool()` 處理；若仍失敗回報 |
| `load_fault_labels` 說 `[跳過] …：資料庫沒有對應事件` | 該機台的 score 檔還沒用 `build_all_tools.py` 載入 |
| `[錯誤] 未知的機台編號 1` | PowerShell 把清單轉成數字（前導零消失）→ 已修（自動補零）；也可寫成 `--tools "01,02,04,06"` |
| `UniqueViolation: alignment_quality_pkey` | 多機台 append 時重複寫 QC 表 → 已修（append 模式略過，改由驗收查詢逐一核對） |
| 事實表有幾台、`dim_lot` 卻只有一台 | 某次載入中途失敗造成不一致 → 用 `build_all_tools.py --reset` 乾淨重建 |
| 某台機台被載入兩次（事件數變兩倍） | `--reset` 或讓它自動跳過已載入機台；確認沒有重複 |
| 磁碟不足 | 5 台機台的感測時序在資料庫約 **2–3 GB**；`rawdata` volume 會到 **2.4 GB**（Docker 磁碟映像檔已在 D:） |
| `eval_commonality` 很慢 | `--replicates 20` 先試；正式報告再用 100 |

---

## 11. 概念題（M4 前我會確認你懂）

1. 為什麼共同性分析的**分析單位必須是批次（lot）而不是加工事件**？如果把 29,002 個事件當獨立樣本，p 值會往哪個方向偏？為什麼？
2. 「單尾超幾何檢定」與「Fisher's exact test（greater）」是什麼關係？為什麼我們同時輸出兩者、還多輸出一個雙尾版本？
3. 這次未校正顯著 **59** 個、而巧合期望只有 **9.7** 個。這個對比能不能直接說「有 59 個真訊號」？為什麼？
4. Bonferroni 與 BH-FDR 控制的東西有什麼不同？為什麼共同性分析預設選 BH？
5. 負控制（null 情境）在驗證什麼？如果沒有做負控制，你交付排名清單的風險是什麼？
6. `recipe_step` 注入根因卻**完全排不上前 3**（平均名次 12），原因是什麼？這對「要怎麼設計 M4 的方法」給了什麼線索？
7. `odds_ratio` 與 `p_hyper` 各回答什麼問題？為什麼只看其中一個會誤判？（提示：大樣本 vs 小樣本各會被哪一個騙）
8. 標籤用「故障前 24 小時內處理的批次」，這個定義有哪些**混淆風險**？（提示：排程、產品、維修後的行為）

---

## 8. 驗收紀錄（2026-10-10 實測通過）

完整證據、兩視角排名表與誠實缺口見 **`reports/m3/VERIFICATION.md`**。摘要：

| 閘門 | 結果 |
|---|---|
| 載入 5 台 | 7,198,950 列 / 203,976 事件 / 守恆 ✅ |
| 壞批次（ANY） | 475 / 7,188 = 6.61% |
| 機台排名 | 01M02 > 03M01 > 02M02 > 06M01 > **04M01（唯一不顯著 q=0.43）** |
| **負控制** | 04M01 專屬批次 **0 / 927 = 0.00%** |
| 共用機台量化 | 3,952 / 7,188 = 55.0%（lot id 全域名稱空間，密度 0.1634 佐證） |
| 誠實缺口 | 機台層級正向結果部分同義反覆；`recipe_step` 注入 @1 = 0%（共線性）；PHM 無良率 |

版本：`VERSION = 2026-10-10.r7`（`run_query.py --version` 可驗）
