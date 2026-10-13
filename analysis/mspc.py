"""M5-2-1 多變量統計製程管制（MSPC）核心。

業界標準架構（見 METHODS.md §0、README 的落差表）
────────────────────────────────────────────────────────────
    PCA → Hotelling T²（主成分空間，偵測「已知模式的異常幅度」）
        → SPE/Q（殘差空間，偵測「模型沒解釋的新變異」）
        → contribution plots（診斷是哪個變數貢獻了這次異常）

本專案 M5-2-0 的實測限制（決定了這裡的設計，不是憑習慣寫的）
────────────────────────────────────────────────────────────
1. **步驟解釋 86.3% 的變異**（PHM 5 台實測）→ 呼叫端必須逐
   `(機台 × recipe_step)` 建模，不可把不同步驟混進一張 PCA。
2. **尾部極重**：基準期馬氏距離 q99.73 是理論值的 11.5–15.5 倍
   → 管制線一律用**經驗分位數**（`empirical_limit`），並提供穩健共變異數
   （`robust_center_cov` 迭代修剪）作為對照。
3. **條件數 1,326–31,685** → 不使用全維度 S⁻¹，只用前 k 個主成分的 T²。

統計定義（實作細節）
────────────────────────────────────────────────────────────
標準化後的向量 x（p 維）、載荷矩陣 P（p×p，行 = 特徵向量）、特徵值 λ（遞減）：

    得分           t_i = p_iᵀ x
    T²             = Σ_{i≤k} t_i² / λ_i
    重建           x̂ = P_k t_k
    SPE（Q）       = ‖x − x̂‖²
    T² 貢獻        c_j = x_j · Σ_{i≤k} (t_i / λ_i) · p_ji   → **Σ_j c_j == T²**（可驗證）
    SPE 貢獻       c_j = (x_j − x̂_j)²                       → **Σ_j c_j == SPE**
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# 穩健共變異數的預設值：每次迭代修剪最遠的 1%，重估 3 次
# 尺度守門：基準期「幾乎是常數」的變數，標準化後任何微小偏差都會變成天文數字
# （實際在 5 感測器全量資料上踩到：超標倍數達 10^32）。σ 低於門檻就排除該變數，
# 由呼叫端明列出來，不可靜默丟掉。
# 標準化殘差的截斷上限（winsorize）：即使尺度守門通過，若某事件的原始特徵值本身離譜
# （例如 slope = 差值/極短時距 → 時距趨近 0 時爆掉），分數仍會到 10^12。
# 截斷到 ±SIGMA_CAP 個標準差：偵測力不變（單一變數 50σ 就遠超管制線），但貢獻排名可解讀。
# **被截斷的次數會被計數並回報**，不可靜默截斷。
SIGMA_CAP = 50.0
SCALE_ATOL = 1e-12          # 絕對下限
SCALE_RTOL = 1e-9           # 相對下限：σ / max(1, |中位數|)

ROBUST_TRIM = 0.01
ROBUST_ITERS = 3
# 監控統計量的名目誤報率（單一統計量）＝ 1 - 0.9973
ALPHA = 0.0027


# ── 趨勢（只用過去資料：以基準期擬合、對監控期外推）────────────────
def fit_trend(t: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """最小平方直線 y ≈ a + b·t（時間軸用真實時間，不用事件序號）。

    為什麼用真實時間：事件間距不平均，用序號外推會在監控期放大誤差。
    """
    ok = np.isfinite(t) & np.isfinite(y)
    if ok.sum() < 3:
        return float(np.nanmean(y[ok])) if ok.any() else 0.0, 0.0
    tt, yy = t[ok], y[ok]
    b, a = np.polyfit(tt, yy, 1)
    return float(a), float(b)


def detrend(t: np.ndarray, y: np.ndarray, a: float, b: float) -> np.ndarray:
    """扣除趨勢線（a、b 必須來自基準期擬合 → 不會用到未來資訊）。"""
    return y - (a + b * t)


# ── 穩健共變異數（真實資料有極端事件；M5-2-0 實測尾部是理論的 11.5–15.5 倍）──
def robust_center_cov(Z: np.ndarray, trim: float = ROBUST_TRIM,
                      iters: int = ROBUST_ITERS) -> tuple[np.ndarray, np.ndarray, int]:
    """迭代修剪：每次丟掉馬氏距離最遠的 trim 比例，重估平均與共變異數。

    回傳 (mu, S, n_kept)。**不依賴 scipy**（MCD 太重），但效果方向相同：
    讓少數極端事件不再主導共變異數矩陣。
    """
    keep = np.ones(Z.shape[0], dtype=bool)
    mu = Z.mean(axis=0)
    S = np.cov(Z, rowvar=False) if Z.shape[1] > 1 else np.array([[Z.var()]])
    for _ in range(max(iters, 0)):
        idx = np.nonzero(keep)[0]
        sub = Z[idx]
        mu = sub.mean(axis=0)
        S = np.cov(sub, rowvar=False)
        if S.ndim == 0:
            S = np.array([[float(S)]])
        try:
            Si = np.linalg.pinv(S)
        except np.linalg.LinAlgError:
            break
        d = Z - mu
        d2 = np.einsum("ij,jk,ik->i", d, Si, d)
        n_drop = int(math.floor(trim * Z.shape[0]))
        if n_drop <= 0:
            break
        order = np.argsort(d2)
        new_keep = np.zeros_like(keep)
        new_keep[order[: Z.shape[0] - n_drop]] = True
        if np.array_equal(new_keep, keep):
            break
        keep = new_keep
    return mu, S, int(keep.sum())


# ── PCA（以相關矩陣的特徵分解；不做 SVD 是為了讓「載荷」語意明確）──
def pca_fit(S: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """回傳 (eigvals 由大到小, loadings 行向量為對應特徵向量)。"""
    ev, P = np.linalg.eigh(S)
    order = np.argsort(ev)[::-1]
    ev = ev[order]
    P = P[:, order]
    ev = np.clip(ev, 1e-12, None)
    return ev, P


def choose_k(eigvals: np.ndarray, var_explained: float = 0.90,
             k_min: int = 2, k_max: int | None = None) -> int:
    """依累積解釋變異率選主成分數；下限 k_min、上限（預設 p-1，保留殘差空間）。"""
    total = float(eigvals.sum())
    cum = np.cumsum(eigvals) / total
    k = int(np.searchsorted(cum, var_explained) + 1)
    p = len(eigvals)
    hi = k_max if k_max is not None else max(p - 1, 1)
    return int(min(max(k, min(k_min, p)), hi))


# ── 統計量 ────────────────────────────────────────────────────
def t2_spe(Z: np.ndarray, mu: np.ndarray, loadings: np.ndarray,
           eigvals: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """回傳 (T², SPE) 兩條序列。"""
    X = Z - mu
    T = X @ loadings[:, :k]
    t2 = np.einsum("ij,j->i", T ** 2, 1.0 / eigvals[:k])
    Xhat = T @ loadings[:, :k].T
    spe = ((X - Xhat) ** 2).sum(axis=1)
    return t2, spe


def spe_from_scores(x: np.ndarray, loadings: np.ndarray, k: int) -> float:
    t = x @ loadings[:, :k]
    xhat = t @ loadings[:, :k].T
    return float(((x - xhat) ** 2).sum())


def empirical_limit(values: np.ndarray, alpha: float = ALPHA) -> float:
    """經驗分位數管制線（1−alpha）。真實資料非高斯時，這才是可用的線。"""
    v = values[np.isfinite(values)]
    if v.size == 0:
        return float("inf")
    q = 1.0 - alpha
    try:
        return float(np.quantile(v, q, method="higher"))
    except TypeError:                      # numpy < 1.22
        return float(np.quantile(v, q, interpolation="higher"))


def t2_contributions(x: np.ndarray, loadings: np.ndarray, eigvals: np.ndarray,
                     k: int) -> np.ndarray:
    """每變數對 T² 的**有號**貢獻：Σ_j c_j == T²（測試會驗證這個等式）。

    x 必須是「已置中且已標準化」的向量。
    """
    t = x @ loadings[:, :k]
    forced = (t / eigvals[:k]) @ loadings[:, :k].T      # p 維
    return x * forced


def spe_contributions(x: np.ndarray, loadings: np.ndarray, k: int) -> np.ndarray:
    """每變數對 SPE 的貢獻（平方殘差）：Σ_j c_j == SPE。"""
    t = x @ loadings[:, :k]
    xhat = t @ loadings[:, :k].T
    return (x - xhat) ** 2


# ── 模型 ─────────────────────────────────────────────────────
@dataclass
class MspcModel:
    """單一 (機台 × recipe_step) 集團的 MSPC 模型（只用基準期擬合，無未來資訊）。"""
    vars: list
    trend: dict                                  # 變數 → (a, b)（時間趨勢，外推用）
    scale: np.ndarray                            # 變數標準差（去趨勢殘差）
    mu: np.ndarray                               # 建模用中心
    loadings: np.ndarray
    eigvals: np.ndarray
    k: int
    lim_t2: float
    lim_spe: float
    n_base: int
    n_base_complete: int
    n_kept_robust: int
    robust: bool
    meta: dict = field(default_factory=dict)
    dropped_degenerate: list = field(default_factory=list)
    keep_idx: list = field(default_factory=list)   # 原始欄位中「被保留」的索引
    sigma_cap: float = SIGMA_CAP
    n_capped: int = 0                              # 上次評分中被截斷的值個數（要回報）
    n_missing: int = 0                             # 上次評分中缺失代入的個數（要回報）

    def score(self, t: np.ndarray, Y: np.ndarray):
        """對原始（未去趨勢、未標準化）觀測矩陣評分。缺失值以基準期中心補（並回報比例）。

        ⚠ Y 是**原始全欄位**；模型可能已排除近乎常數的變數 → 這裡必須先取出 keep_idx，
        否則形狀不符（實際被單元測試抓到）。
        """
        Y = Y[:, self.keep_idx] if self.keep_idx else Y
        R = np.empty_like(Y, dtype=float)
        miss = np.isnan(Y)
        for j, name in enumerate(self.vars):
            a, b = self.trend[name]
            R[:, j] = detrend(t, Y[:, j], a, b)
        R = R / self.scale
        if miss.any():
            R = np.where(miss, self.mu, R)      # 缺失 → 以基準期中心代入（保守）
        X = R - self.mu
        self.n_missing = int(miss.sum())
        self.n_capped = int(np.sum(np.abs(X) > self.sigma_cap))
        X = np.clip(X, -self.sigma_cap, self.sigma_cap)     # 截斷（次數已計數）
        return t2_spe(X, np.zeros_like(self.mu), self.loadings, self.eigvals, self.k)

    def contributions(self, t_i: float, row: np.ndarray) -> dict:
        """單一事件的貢獻分解（row 為原始值向量；含缺失時以 0 補）。"""
        row = row[self.keep_idx] if self.keep_idx else row
        R = np.empty_like(row, dtype=float)
        for j, name in enumerate(self.vars):
            a, b = self.trend[name]
            v = row[j]
            R[j] = 0.0 if not np.isfinite(v) else (v - (a + b * t_i)) / self.scale[j]
        x = R - self.mu
        # 必須與 score() 用同一套截斷，否則貢獻不會加總成回報的 T²／SPE
        x = np.clip(x, -self.sigma_cap, self.sigma_cap)
        c_t2 = t2_contributions(x, self.loadings, self.eigvals, self.k)
        c_spe = spe_contributions(x, self.loadings, self.k)
        return {"t2_total": float(c_t2.sum()), "spe_total": float(c_spe.sum()),
                "t2": c_t2, "spe": c_spe}


def build_model(t_base: np.ndarray, Y_base_complete: np.ndarray, vars_: list,
                var_explained: float = 0.90, alpha: float = ALPHA,
                robust: bool = True) -> MspcModel | None:
    """以基準期完整向量建立模型。(時間, 原始觀測) → MspcModel。

    `Y_base_complete` 必須是完整（無缺失）的基準期觀測；呼叫端負責挑選。
    """
    n, p = Y_base_complete.shape
    if n < max(5 * p, 20):
        return None
    trend, scale = {}, np.ones(p, dtype=float)
    R = np.empty_like(Y_base_complete, dtype=float)
    degenerate: list = []
    for j, name in enumerate(vars_):
        a, b = fit_trend(t_base, Y_base_complete[:, j])
        r = detrend(t_base, Y_base_complete[:, j], a, b)
        sd = float(np.std(r, ddof=1))
        med = float(np.median(Y_base_complete[:, j]))
        tol = max(SCALE_ATOL, SCALE_RTOL * max(1.0, abs(med)))
        if not np.isfinite(sd) or sd < tol:
            degenerate.append(name)          # 近乎常數 → 標準化後會爆，必須排除
            continue
        trend[name] = (a, b)
        scale[j] = sd
        R[:, j] = r / sd
    keep_idx = list(range(p))
    if degenerate:
        keepc = [j for j, v in enumerate(vars_) if v not in set(degenerate)]
        if len(keepc) < 2:
            return None
        R = R[:, keepc]
        scale = scale[keepc]
        vars_ = [vars_[j] for j in keepc]
        keep_idx = keepc
    if robust:
        mu, S, n_kept = robust_center_cov(R)
    else:
        mu, S = R.mean(axis=0), np.cov(R, rowvar=False)
        n_kept = n
    eigvals, loadings = pca_fit(S)
    k = choose_k(eigvals, var_explained)
    t2_b, spe_b = t2_spe(R, mu, loadings, eigvals, k)
    return MspcModel(vars=vars_, trend=trend, scale=scale, mu=mu, loadings=loadings,
                     eigvals=eigvals, k=k,
                     lim_t2=empirical_limit(t2_b, alpha),
                     lim_spe=empirical_limit(spe_b, alpha),
                     n_base=n, n_base_complete=n, n_kept_robust=n_kept, robust=robust,
                     dropped_degenerate=degenerate, keep_idx=keep_idx)
