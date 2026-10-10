# Commonality Analysis 方法與統計檢定

## 1. 問題定義

> 已知有一批良率異常（或故障）的樣本，**找出與異常顯著相關的機台、腔體、recipe、步驟**。

核心是「**共同性**」：壞的樣本共同經過了什麼？但**只看分子會錯**，一定要有分母
（同期間經過同一機台、卻正常生產的樣本）。層級由上而下：

```
良率異常 → bin/參數 pareto → 異常樣本集合 → 可疑站別 → 可疑機台/腔體 → 可疑時間窗 → 根因
```

---

## 2. 基礎方法（先做這層）

### 2.1 2×2 列聯表
對每個 tool（或 chamber/recipe/step）建表：

|  | 異常樣本 | 正常樣本 |
|---|---|---|
| 經過該機台 | a | b |
| 未經過 | c | d |

- 指標：**odds ratio** = (a/c) / (b/d)、risk ratio、attributable risk
- 檢定：**Fisher's exact test**（樣本小、失敗稀有 → 首選）、**chi-square（含 Yates 校正）**
- 輸出：排名表 + 信賴區間（OR 的 CI 用 Woolf 或 exact）

### 2.2 超幾何檢定（Hypergeometric test）★半導體業標準做法

> **命名說明**：這類檢定在業界統稱 **Tool Commonality Analysis（TCA）**；
> 本文件早期寫的「Williams' test」查不到可靠權威出處，已改為中性名稱（2026-10-09 修正）。
> 文獻例：*Tool commonality analysis for yield enhancement*（SEMATECH 系列）；
> 組合式根因的後續研究見 *Identifying ill tool combinations via Gibbs sampler*（WSC 2012）。
情境：N 個 lot 中有 K 個壞 lot；某機台處理 n 個 lot，其中 k 個是壞 lot。
問「這個機台處理到這麼多壞 lot 的機率有多大？」

```
P(X ≥ k) = Σ_{i=k..min(K,n)} C(K,i) · C(N-K, n-i) / C(N, n)
```

- 為何用超幾何而非二項式：**抽樣無放回**（一個 lot 被某機台處理，就不會同時被另一台處理）
- 好處：天然處理「不等的機台產能」（產能大的機台不會因為樣本多就被誤判）
- 這是 yield engineering 裡 tool commonality 的經典檢定

### 2.2.1 三個容易被問倒的細節

**(a) 單尾超幾何檢定 ≡ Fisher's exact test（greater）**
兩者是同一個檢定的不同說法：一個用「抽樣無放回」的機率模型，一個用 2×2 列聯表＋勝算比的語言。
實作時兩者會給出**完全相同**的 p 值（本專案單元測試中有恆等驗證）。
雙尾 Fisher 是另一個檢定（常用於「雙向都可能」的情境），共同性分析不需要。

**(b) 分析單位必須是「批次（lot）」——不是「加工事件」**
同一 lot 內的事件不獨立（同批、同時、同一片晶圓）。把 11 萬個事件當獨立樣本會產生
**偽重複（pseudo-replication）**，p 值小到毫無意義。
以批次為單位也正是超幾何檢定的設定（抽樣無放回）。

**(c) 直接量測「方法本身」的可信度**
用真實的群組結構只模擬 outcome，可同時得到：
- **負控制**：完全沒有根因時，校正後還剩幾個假陽性（未校正的假陽性期望 = m × α）
- **正控制**：注入已知根因（壞率 ×N）時，排第 1／前 3 的比例（偵測率）

沒有負控制的分析在面試裡站不住。

### 2.3 Pareto / drill-down
- bin pareto、參數異常 pareto、機台貢獻 pareto（累積貢獻 80/20）
- 適合當第一步的視覺化與溝通工具（主管要的是「先看哪裡」）

### 2.4 群組類型（group type）與下鑽順序

「群組類型」＝ 我們在問哪一種**嫌疑維度**：`tool`（機台）、`recipe`（配方）、
`recipe_step`（配方步驟）、`stage`（製程分段）、`recipe_step_x_stage`（組合）。
「群組」＝ 該維度裡的具體對象（`01M02`、`stage 104`）。

**實作方式**：`mart.lot_membership` 同時存所有維度的成員關係；`run_commonality --group-types`
只是**篩選器**（`mem = [m for m in memberships if group_types is None or m[1] in group_types]`），
**不加參數＝全部維度**。所以「跑出來只有 tool」永遠是參數造成的，不是資料缺漏。

**為何 M3 第二段先單獨驗 `tool`**：

1. **驗收條件本身就是機台層級的**：「有故障機台靠前、零故障機台不進前 3」是可證偽的閘門，
   而只有機台維度有明確的已知答案（負控制）
2. **混入其他維度會讓群組數暴增**（各維度 × 5 台機台）→ 多重比較校正變嚴，
   每個訊號要跟更多雜訊競爭，反而看不清最粗的那一層對不對
3. **`tool` 與其他維度高度共線**：每個 recipe／stage 群組的批次也分屬不同機台，
   混在一起就分不清「是 recipe 的效應還是機台的效應」（M4 要解的混淆，見 3.1）
4. **單機台時期，`tool` 群組是零資訊**：只有 03M01 時，`tool` 群組 n = 3,081 = 全部批次、
   k = 216 = 全部壞批次 → OR = 1、無鑑別力。這也是必須先載入 5 台機台才跑這段的原因

**不可跨類型比較名次**：不同維度的群組**共用成員**（同一批 lot 既屬某機台、又屬某 recipe、
又屬某 stage），名次沒有共同基準。正確用法是**逐層下鑽（Pareto drill-down）**：

```
tool 層（5 台哪一台可疑）
   └─ 在該機台內 → recipe / stage（哪個配方/站別）
        └─ 再往下 → recipe_step / 組合（哪個步驟）
```

**一次跑全部維度**（不加 `--group-types`）會產生數百至上千個群組，BH 校正後 q 值明顯變嚴。
這不是壞事，但解讀時要記得：**顯著數變少不等於訊號消失，而是校正變嚴格**。

實測對照（單機台 03M01、194 群組的合成注入評估）：`recipe` 與 `stage` 注入的偵測率 @1 = 100%；
`recipe_step` @1 = **0%**、名次 11–13（與 stage 共線造成辨識不能）→ 單變數排名不足以分辨
重疊維度，必須靠 3.1 的多變數模型。

**怎麼跑其他維度**（先列出資料庫實際有哪些維度，再逐一跑、各自給 `analysis_id` 以便並存比較）：

```powershell
docker compose exec db psql -U ca -d commonality -c "SELECT group_type, count(DISTINCT group_key) AS n_groups, count(*) AS memberships FROM mart.lot_membership GROUP BY 1 ORDER BY 3 DESC;"

docker compose run --rm etl python -m analysis.run_commonality --window-hours 24 --fault-type ANY --group-types recipe,stage --analysis-id M3_ANY_w24h_recipestage --out reports/m3_recipestage
```

---

## 3. 進階方法（避免誤判）

### 3.1 控制共變數：logistic regression
- 依變數：異常與否；自變數：tool dummy + **控制項**（product、recipe、時間、站別）
- 為何需要：壞 lot 可能剛好都是同一產品、同一時期——不控制就會誤判機台
- 混合效應模型（mixed-effects / random intercept per tool）：處理**同一 lot 內 wafer 不獨立**
  （群聚相關）的問題

### 3.2 Bayesian hierarchical / shrinkage
- 小樣本機台若「處理 1 批壞 1 批」用 Fisher 檢定會得到極端結論
- 用 partial pooling（收縮）讓小樣本估計往群體平均靠 → 實務上大幅降低誤報
  （Beta-binomial 模型天然對應良率資料）

### 3.3 組合式 commonality（要找「機台組合」）
- 只找單一機台會漏掉「A 機台 + B 腔體才出問題」的情況
- 方法：association rule mining、貪婪搜尋、Gibbs sampling / Bayesian 搜尋
- 文獻已知問題：貪婪搜尋的**偽陽性與偽陰性都高** → 要用統計校正或貝氏方法
  （WSC 2012 有兩篇針對此問題的論文，見 METHODS_refs）

### 3.4 機器學習 + 可解釋性
- Random Forest / XGBoost 把 tool/chamber/step 當類別特徵；用 **SHAP / permutation importance** 排名
- 注意：**資料洩漏**（把 label 相關的欄位當特徵）、**時間切分驗證**、類別不平衡處理
- ML 的角色是「找交互作用與非線性」，不是取代統計檢定

### 3.5 多重比較校正
- 一次檢定上百個 tool × step × recipe → 必然出現假陽性
- **Bonferroni**（保守，控制 FWER）／**Benjamini-Hochberg FDR**（控制偽發現率，實務常用）
- 報告時要同時給「校正前後的顯著數量」

---

## 4. 時序 / FDC 方法（第二層：從 trace 找異常）

| 方法 | 用途 | 重點 |
|---|---|---|
| Shewhart 管制圖 | 單變數、大偏移 | 3σ 規則；對小偏移不敏感 |
| **EWMA** | 單變數、小偏移 | λ 越小越敏感但反應慢 |
| **CUSUM** | 單變數、持續小偏移 | 累積偏差，適合漂移偵測 |
| **Hotelling T²** | 多變量常態 | 需估計共變異數矩陣與其反矩陣 |
| **PCA / MSPC** | 高維、共線性 | T²（主成分空間）+ **SPE/Q**（殘差空間） |
| Change point detection | 找出漂移起點 | PELT、Bayesian online change point |
| Trace 對齊 | 不同長度/時序的比較 | 時間正規化重採樣、**DTW（動態時間彎曲）** |
| Chamber matching | 同機台不同腔體比對 | trace 指紋距離 + 統計檢定 |
| 空間分析 | wafer map 型態 | 徑向/環狀/局部型態 → 與機台共同性交叉 |

**重要**：FDC 負責「偵測異常」，commonality 負責「把異常歸因到機台」。
兩層要串起來：`異常 trace → 異常 wafer-step → 共同性分析 → 機台/腔體排名`。

---

## 5. 常用統計檢定一覽

| 目的 | 檢定 | 適用時機 |
|---|---|---|
| 兩組比例差異（稀有事件） | **Fisher's exact** | 失敗樣本少、2×2 表 |
| 兩組比例差異（樣本大） | chi-square（+Yates） | 期望次數 ≥ 5 |
| 「壞的一起經過某機台」 | **超幾何 / Williams'** | 機台產能不等、無放回抽樣 |
| 兩組平均差異 | t-test / **Mann-Whitney U** | 常態 / 非常態或小樣本 |
| 多組平均差異 | ANOVA / **Kruskal-Wallis** | 多機台比較 |
| 變異數差異 | Levene / **F-test** | 「不只平均跑掉，是變異變大」 |
| 分布差異 | **KS 檢定** | trace 或參數分布整體比較 |
| 多變量異常 | **Hotelling T²**、SPE/Q | 多感測器同時監控 |
| 類別關聯（多分類） | chi-square / G-test | bin 分布與機台關聯 |
| 迴歸係數顯著性 | Wald / likelihood ratio | logistic regression |
| 無母數、自訂統計量 | **permutation test** | 分布假設不成立時 |
| 多重比較 | Bonferroni / **BH-FDR** | 同時檢定多個機台 |

**效應量與 p 值同等重要**：OR/risk ratio、Cpk 變化、良率損失換算成 die 數
（例：300 mm 晶圓、die 120 mm² → 約 530 顆/片；良率掉 8% ≈ 42 顆/片、75 片 ≈ 3,180 顆）

---

## 6. 評估這套方法好不好（本專案的科學核心）

| 指標 | 定義 | 為何重要 |
|---|---|---|
| 偵測率（recall） | 注入的根因被排進前 N 名的比例 | 方法有沒有用 |
| 誤報率（FDR） | 被判定可疑但其實正常的機台比例 | 沒校正會滿天假警報 |
| 領先時間（lead time） | 根因開始到被偵測出的時間 | 能不能提前止血 |
| 穩健性 | 不同效應量/資料量下的表現 | 小樣本會不會亂報 |
| 可解釋性 | 排名與注入根因的一致性 | 工程師能不能照著行動 |

**負控制（negative control）**：在沒有注入任何根因的資料上跑同一套 pipeline，
量測「沒有根因時報出來的假陽性數量」——這是很多分析沒做、卻最該做的一步。

### 6.1 共用機台造成的混淆（實測量化，2026-10）

M3 在真實資料上量到的現象，值得獨立成一節：

- **55% 的批次被兩台以上機台加工過** → 一批 lot 的壞率不能直接歸功/歸咎於單一台機台，
  只要同批 lot 走過別的已退化機台就會被標成壞批次
- **完全零故障的 `04M01`（真實資料負控制）批次壞率仍達 6.67%**（OR 1.02）
  → 這 6.67% 可全部歸因於共用路線，不是它自己的故障
- 但檢定**正確地沒有把它列為顯著**（q = 0.43，5 台中唯一不顯著）→ **負控制通過**
- 乾淨歸因的做法：只取「只被一台機台加工過」的批次（`--exclusive-lots`）再跑一次，
  兩個視角並列才是誠實的結論

**這也是 M4 的動機**：只有在模型裡同時放入各機台共變數（logistic regression），
才能在共用路線下分離出各自的效應；單變數排名會把「路線共用」誤認為「機台效應」。

**方法評估時的注意**：機台層級的排名有一部分是「同義反覆」——壞批次的定義就來自
各機台自己的故障窗，所以有故障的機台排名靠前是可預期的。真正有資訊量的是
**負控制（零故障機台不得顯著）**；方法的辨識能力則由 6.2 的合成注入實驗量測。

#### 6.1.1 兩個視角的實測對照（2026-10，PHM 2018 五台機台）

| 機台 | 全部批次 n / k / OR | 只算單一機台批次 n / k / 壞率 / OR | q (BH, exclusive) | 判讀 |
|---|---|---|---|---|
| 01M02 | 629 / 142 / 5.45 | 113 / 29 / **25.7%** / **12.34** | 5.6e-18 | 訊號最強（3 種故障、時間密度高） |
| 03M01 | 3,081 / 337 / 3.53 | 540 / 32 / 5.9% / 2.01 | 0.0027 | 仍顯著 |
| 02M02 | 3,512 / 285 / 1.62 | 1,110 / 46 / 4.1% / 1.31 | 0.134 | **不顯著**（全部批次視角的顯著來自共用路線） |
| 06M01 | 3,395 / 276 / 1.60 | 546 / 7 / 1.3% / **0.31** | 1.0 | **不顯著，且方向翻轉**（風險完全是借來的） |
| **04M01** | 3,869 / **258** / 1.02 | 927 / **0** / **0.00%** | —（k=0，不列入檢定） | **負控制：專屬批次零壞批次** |

- **04M01 的 258 個壞批次全部來自共用批次**（其專屬批次 927 批零壞、共用批次 2,942 批中 258 批壞 = 8.77%）
  → 「零故障機台看起來有 6.7% 壞率」完全是路線共用造成，不是它自己的問題
- `06M01` 由 OR 1.60 變成 0.31（低於基準）→ 在全部批次視角下它的風險是**向其他機台借來的**
- **結論**：單變數排名在共用路線下會系統性高估「路線共用」、低估計真正有問題的機台
  （`01M02` 的 OR 從 5.45 升到 12.34）→ M4 必須用多變數模型同時放各機台共變數

---

## 7. 參考文獻（本專案方法論依據）

- Tool commonality analysis for yield enhancement（半導體工具共同性分析的經典做法）
- A statistical approach to identify semiconductor process equipment related yield problems
- Commonality Analysis for Detecting Failures Caused by Inspection Tools（Yonsei University）
- Identifying ill tool combinations via Gibbs Sampler for semiconductor manufacturing yield diagnosis
  （WSC 2012；指出貪婪搜尋的 FP/FN 問題）
- Markov-chain based missing value estimation for tool commonality analysis（WSC 2012；
  缺值對 TCA 的影響）
- PHM Society 2018 Data Challenge 官方說明（感測器與故障定義）
- Systematic Data Mining Methodologies for Yield Improvement（Qorvo，業界實例）
