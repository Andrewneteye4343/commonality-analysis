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

### 2.2 超幾何檢定（Hypergeometric / Williams' test）★半導體業標準做法
情境：N 個 lot 中有 K 個壞 lot；某機台處理 n 個 lot，其中 k 個是壞 lot。
問「這個機台處理到這麼多壞 lot 的機率有多大？」

```
P(X ≥ k) = Σ_{i=k..min(K,n)} C(K,i) · C(N-K, n-i) / C(N, n)
```

- 為何用超幾何而非二項式：**抽樣無放回**（一個 lot 被某機台處理，就不會同時被另一台處理）
- 好處：天然處理「不等的機台產能」（產能大的機台不會因為樣本多就被誤判）
- 這是 yield engineering 裡 tool commonality 的經典檢定

### 2.3 Pareto / drill-down
- bin pareto、參數異常 pareto、機台貢獻 pareto（累積貢獻 80/20）
- 適合當第一步的視覺化與溝通工具（主管要的是「先看哪裡」）

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
