"""M3：共同性分析（commonality analysis）的統計核心。

設計原則
────────────────────────────────────────────────────────────
1. **分析單位是「批次（lot）」，不是「加工事件」**：同一 lot 內的事件彼此不獨立
   （同批、同時、同一片晶圓），把 11 萬個事件當獨立樣本會產生**偽重複
   （pseudo-replication）**，p 值會小到毫無統計意義。
2. **主檢定＝單尾超幾何檢定**（業界 Tool Commonality Analysis, TCA 的標準做法）：
   N 個批次中有 K 個壞批次；某群組（機台／recipe／步驟…）處理 n 個批次、其中 k 個壞
   → 問「這麼集中是巧合嗎？」
       P(X ≥ k) = Σ_{i=k..min(K,n)} C(K,i)·C(N−K,n−i) / C(N,n)
   用超幾何而非二項式，是因為**抽樣無放回**（一個批次被甲機台處理就不會同時被乙機台處理），
   也因此天然處理「各機台產能不等」的問題。
3. **數學事實（會被問，先講清楚）**：
   單尾超幾何檢定 ≡ Fisher's exact test（greater）。兩者是同一個檢定的不同說法
   （Fisher 用 2×2 表格＋勝算比的語言，TCA 用抽樣無放回的語言）。
   本模組兩者都輸出以便對照，並額外輸出**雙尾 Fisher** 供參考。
4. **效應量與檢定並重**：輸出 odds ratio（勝算比）＋ 95% 信賴區間（Woolf 法，
   必要時加 0.5 連續性校正）。只看 p 值會被大樣本騙——樣本夠大時微小差異也會「顯著」。
5. 有 scipy 時用 `scipy.stats.hypergeom`（向量化、快）；沒有時用純 Python
   （lgamma 對數空間求和），確保任何環境都能跑。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

try:  # scipy 為選配（容器內有；助理端驗證時可能沒有）
    import numpy as _np
    from scipy.stats import hypergeom as _hypergeom
    HAS_SCIPY = True
except Exception:  # pragma: no cover
    _np = None
    _hypergeom = None
    HAS_SCIPY = False


# ─────────────────────────────────────────────────────────────
# 基礎：超幾何分布
# ─────────────────────────────────────────────────────────────
def log_binom(n: int, k: int) -> float:
    """log(C(n, k))，用 lgamma 避免大數溢位"""
    if k < 0 or k > n:
        return -math.inf
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def hypergeom_logpmf(k: int, N: int, K: int, n: int) -> float:
    """P(X = k)，X ~ Hypergeometric(N, K, n)"""
    if not (0 <= k <= K and 0 <= n - k <= N - K):
        return -math.inf
    return log_binom(K, k) + log_binom(N - K, n - k) - log_binom(N, n)


def hypergeom_sf(k: int, N: int, K: int, n: int) -> float:
    """P(X ≥ k)（純 Python；對數空間求和避免下溢）"""
    lo, hi = max(k, 0), min(K, n)
    if lo > hi:
        return 0.0
    lps = [hypergeom_logpmf(i, N, K, n) for i in range(lo, hi + 1)]
    m = max(lps)
    if m == -math.inf:
        return 0.0
    total = math.fsum(math.exp(lp - m) for lp in lps)
    return min(1.0, math.exp(m) * total)


def hypergeom_sf_many(ks, N: int, K: int, n) -> list[float]:
    """向量化 P(X ≥ k)（有 scipy 時走 scipy；否則逐項）"""
    if HAS_SCIPY:
        ks = _np.asarray(ks, dtype=float)
        ns = _np.asarray(n, dtype=float) if hasattr(n, "__len__") else n
        # scipy 定義：sf(k) = P(X > k) → 要 P(X ≥ k) 傳 k-1
        return _hypergeom.sf(ks - 1, N, K, ns).tolist()
    if hasattr(ks, "__len__"):
        return [hypergeom_sf(int(k), N, K, int(ni)) for k, ni in zip(ks, n)]
    return [hypergeom_sf(int(ks), N, K, int(n))]


def hypergeom_pmf_many(ks, N: int, K: int, n) -> list[float]:
    if HAS_SCIPY:
        return _hypergeom.pmf(_np.asarray(ks, dtype=float), N, K, n).tolist()
    if hasattr(ks, "__len__"):
        return [math.exp(hypergeom_logpmf(int(k), N, K, int(ni))) for k, ni in zip(ks, n)]
    return [math.exp(hypergeom_logpmf(int(ks), N, K, int(n)))]


# ─────────────────────────────────────────────────────────────
# Fisher's exact test
# ─────────────────────────────────────────────────────────────
def fisher_exact_greater(a: int, b: int, c: int, d: int) -> float:
    """單尾（greater）：P(X ≥ a)，等同超幾何檢定（TCA 用的檢定）"""
    N = a + b + c + d
    K = a + c          # 壞批次總數
    n = a + b          # 群組處理的批次數
    return hypergeom_sf(a, N, K, n)


def fisher_exact_two_sided(a: int, b: int, c: int, d: int) -> float:
    """雙尾：所有機率不大於觀測表的表格機率之和（與 scipy 慣例一致）"""
    N = a + b + c + d
    K = a + c
    n = a + b
    lo = max(0, n - (N - K))
    hi = min(K, n)
    p_obs = math.exp(hypergeom_logpmf(a, N, K, n))
    tol = p_obs * (1 + 1e-7)
    total = 0.0
    for i in range(lo, hi + 1):
        p = math.exp(hypergeom_logpmf(i, N, K, n))
        if p <= tol:
            total += p
    return min(1.0, total)


# ─────────────────────────────────────────────────────────────
# 效應量
# ─────────────────────────────────────────────────────────────
def odds_ratio(a: int, b: int, c: int, d: int) -> tuple[float, bool, str]:
    """勝算比 OR = (a/c) / (b/d) = a·d / (b·c)

    回傳 (OR, 是否做了校正, 方法說明)。任一格為 0 → 無法直接計算，
    採 Haldane–Anscombe 校正（四格各加 0.5），並在輸出中標明。
    """
    if b * c == 0 or c == 0:
        aa, bb, cc, dd = a + 0.5, b + 0.5, c + 0.5, d + 0.5
        return (aa * dd) / (bb * cc), True, "haldane-anscombe +0.5（有 0 格）"
    return (a * d) / (b * c), False, "未校正"


def or_ci_woolf(a: int, b: int, c: int, d: int, alpha: float = 0.05) -> tuple[float, float, float, bool]:
    """Woolf 對數常態近似信賴區間；回傳 (OR, low, high, 是否校正)"""
    corrected = (a == 0 or b == 0 or c == 0 or d == 0)
    aa, bb, cc, dd = (a + 0.5, b + 0.5, c + 0.5, d + 0.5) if corrected else (a, b, c, d)
    or_ = (aa * dd) / (bb * cc)
    se = math.sqrt(1 / aa + 1 / bb + 1 / cc + 1 / dd)
    # 注意尾端：信賴區間要用上尾分位數 z_{1−α/2}（+1.96），
    # 用 _z(α/2) = −1.96 會讓上下界顛倒（M3 由單元測試抓到）
    z = _z(1 - alpha / 2)
    return or_, math.exp(math.log(or_) - z * se), math.exp(math.log(or_) + z * se), corrected


def _z(p: float) -> float:
    """標準常態分位數（Acklam 近似，足夠 1e-8 精度）"""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > ph:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q, r = p - 0.5, (p - 0.5) ** 2
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def contingency_stats(a: int, b: int, c: int, d: int, alpha: float = 0.05) -> dict:
    """一次算完 2×2 表格的所有統計量

    表格語意（列＝群組、欄＝結果）：
               壞批次   好批次
      屬於群組    a        b
      不屬於      c        d
    """
    or_, corrected, method = odds_ratio(a, b, c, d)
    or_ci, lo, hi, ci_corrected = or_ci_woolf(a, b, c, d, alpha)
    return {
        "a": a, "b": b, "c": c, "d": d,
        "n_total": a + b + c + d,
        "n_bad_total": a + c,
        "n_group": a + b,
        "k_bad": a,
        "p_bad_in_group": (a / (a + b)) if (a + b) else None,
        "p_bad_outside": (c / (c + d)) if (c + d) else None,
        "odds_ratio": or_,
        "or_method": method,
        "or_ci_low": lo,
        "or_ci_high": hi,
        "ci_method": "Woolf（含 +0.5 校正）" if (corrected or ci_corrected) else "Woolf",
        "p_hyper": fisher_exact_greater(a, b, c, d),
        "p_fisher_greater": fisher_exact_greater(a, b, c, d),   # 與 p_hyper 相同（數學恆等）
        "p_fisher_two_sided": fisher_exact_two_sided(a, b, c, d),
    }


# ─────────────────────────────────────────────────────────────
# 群組排名（引擎主體）
# ─────────────────────────────────────────────────────────────
@dataclass
class RankConfig:
    """支持度門檻：樣本太少的群組檢定沒有意義，但仍要列出（標 flagged）"""
    min_group_units: int = 5      # 群組至少要處理 5 個批次
    min_total_bad: int = 3        # 全部壞批次至少 3 個
    min_bad_in_group: int = 1     # 群組內至少 1 個壞批次
    alpha: float = 0.05


@dataclass
class GroupResult:
    group_type: str
    group_key: str
    n_group: int
    k_bad: int
    n_total: int
    n_bad_total: int
    a: int = field(init=False)
    b: int = field(init=False)
    c: int = field(init=False)
    d: int = field(init=False)
    stats: dict = field(default_factory=dict, init=False)
    testable: bool = field(init=False, default=False)
    flag: str = field(init=False, default="")

    def __post_init__(self):
        self.a = self.k_bad
        self.b = self.n_group - self.k_bad
        self.c = self.n_bad_total - self.k_bad
        self.d = (self.n_total - self.n_group) - self.c

    def finalize(self, cfg: RankConfig) -> "GroupResult":
        notes = []
        if self.d < 0:
            notes.append("d<0：成員歸屬重叠或壞批次定義不一致")
            self.d = max(self.d, 0)
        if self.n_bad_total < cfg.min_total_bad:
            notes.append(f"壞批次總數 {self.n_bad_total} < {cfg.min_total_bad}")
        if self.n_group < cfg.min_group_units:
            notes.append(f"群組批次數 {self.n_group} < {cfg.min_group_units}")
        if self.k_bad < cfg.min_bad_in_group:
            notes.append("群組內無壞批次")
        self.testable = not notes
        self.flag = "；".join(notes)
        self.stats = contingency_stats(self.a, self.b, self.c, self.d, cfg.alpha)
        return self

    def as_row(self) -> dict:
        return {"group_type": self.group_type, "group_key": self.group_key,
                **self.stats, "testable": self.testable, "flag": self.flag}


def rank_groups(units: dict, memberships, cfg: RankConfig | None = None) -> list[GroupResult]:
    """計算所有群組的列聯表與檢定量

    units      : {unit_id: is_bad(bool)}——單位是批次，不是事件
    memberships: iterable of (unit_id, group_type, group_key)
    """
    cfg = cfg or RankConfig()
    n_total = len(units)
    n_bad_total = sum(1 for v in units.values() if v)
    agg: dict[tuple, list[int]] = {}
    for unit_id, gtype, gkey in memberships:
        if unit_id not in units:
            continue
        key = (gtype, gkey)
        cell = agg.setdefault(key, [0, 0])      # [n_group, k_bad]
        cell[0] += 1
        if units[unit_id]:
            cell[1] += 1
    out = []
    for (gtype, gkey), (n_group, k_bad) in agg.items():
        out.append(GroupResult(gtype, gkey, n_group, k_bad, n_total, n_bad_total).finalize(cfg))
    return out
