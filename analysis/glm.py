"""M4 統計引擎：logistic regression（IRLS）、群聚穩健標準誤、Beta-binomial 收縮、Spearman 相關。

為什麼要自己寫（不用 statsmodels / scikit-learn）
────────────────────────────────────────────────────────────
1. **可驗證性**：本專案的交付紀律要求「新增的每條執行路徑都要在交付前被實際執行過」。
   助理端沒有 statsmodels/sklearn，若用它們寫程式，就只能靠使用者回報才發現錯誤
   （M3 已經吃過這個虧）。純 Python 的版本可以在交付前用已知解析解與模擬資料完整驗證。
2. **教學價值**：IRLS 的每一步（勝算 → 對數勝算 → 加權最小平方的迭代）都是這個專案的核心概念，
   藏在套件裡就看不到。
3. **相依性最小**：容器不需新增任何套件，`requirements.txt` 不變 → 不用重建 image。

三個方法各自解決 M3 實測到的問題
────────────────────────────────────────────────────────────
- **logistic regression（多變數）**：M3 的單變數排名把「路線共用」誤認為「機台效應」
  （55% 批次被多台機台加工過）。多變數模型同時放入所有機台共變數，
  係數代表「在其他機台也做過的前提下，這台機台自己的效應」。
- **ridge 懲罰**：完全分離（perfect separation）會讓最大概似估計跑到無窮大
  ——實測案例：零故障機台的專屬批次 0/927（k=0），MLE 不存在。ridge 提供有限解。
- **群聚穩健標準誤（sandwich / Huber-White）**：同一 recipe 或同一時段的批次不是獨立的
  （偽重複 pseudo-replication），naive 標準誤會低估 → p 值過度樂觀。
- **Beta-binomial 收縮**：小樣本群組的極端比率（例如 0/1、5/6）不穩定；
  經驗貝氏把它們往整體平均拉，樣本越小拉越多。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


# ────────────────────────────────────────────────────────────
# 線性代數（純 Python；矩陣一律用「list of rows」表示）
# ────────────────────────────────────────────────────────────
def solve_linear(A: list[list[float]], b: list[float]) -> list[float]:
    """解 A x = b（高斯消去 + 部分樞軸）。A 會被複製，不會被改動。"""
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-300:
            raise ValueError("矩陣奇異（singular）：可能有完全共線的欄位")
        if piv != col:
            M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        for r in range(col + 1, n):
            f = M[r][col] / pv
            if f:
                for c in range(col, n + 1):
                    M[r][c] -= f * M[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = M[r][n] - sum(M[r][c] * x[c] for c in range(r + 1, n))
        x[r] = s / M[r][r]
    return x


def invert(A: list[list[float]]) -> list[list[float]]:
    """求反矩陣（高斯-喬登）。"""
    n = len(A)
    M = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-300:
            raise ValueError("矩陣奇異（singular），無法求反")
        M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        M[col] = [v / pv for v in M[col]]
        for r in range(n):
            if r != col and M[r][col]:
                f = M[r][col]
                M[r] = [a - f * b for a, b in zip(M[r], M[col])]
    return [row[n:] for row in M]


def matmul(A: list[list[float]], B: list[list[float]]) -> list[list[float]]:
    n, m, p = len(A), len(B), len(B[0])
    return [[sum(A[i][k] * B[k][j] for k in range(m)) for j in range(p)] for i in range(n)]


def xtwx(X: list[list[float]], w: list[float]) -> list[list[float]]:
    """Xᵀ W X（W 為對角權重）"""
    k = len(X[0])
    out = [[0.0] * k for _ in range(k)]
    for i, row in enumerate(X):
        wi = w[i]
        if not wi:
            continue
        for a in range(k):
            xa = row[a] * wi
            if xa:
                for b in range(a, k):
                    v = xa * row[b]
                    out[a][b] += v
                    if a != b:
                        out[b][a] += v
    return out


def xtv(X: list[list[float]], v: list[float]) -> list[float]:
    """Xᵀ v"""
    return [sum(X[i][a] * v[i] for i in range(len(X))) for a in range(len(X[0]))]


# ────────────────────────────────────────────────────────────
# 列樣式壓縮（加速）
# ────────────────────────────────────────────────────────────
def compress_rows(X: list[list[float]], y: list[int]) -> tuple[list[list[float]], list[float], list[float]]:
    """把完全相同設計列合併，回傳 (唯一列, 各列的 y 總和, 各列的次數)。

    為什麼可以合併：σ(x·β) 只取決於線性預測值 x·β，**相同的列有相同的機率**
    → 對數概似、分數函數與資訊矩陣都能用「樣式 × 次數」精確重寫（結果完全相同）。
    本專案的路線模型只有 2^k 種樣式（k 台機台），因此注入實驗能快上數個量級。
    """
    idx: dict[tuple, int] = {}
    rows: list[list[float]] = []
    sy: list[float] = []
    cnt: list[float] = []
    for i, row in enumerate(X):
        key = tuple(row)
        j = idx.get(key)
        if j is None:
            idx[key] = j = len(rows)
            rows.append(list(row))
            sy.append(0.0)
            cnt.append(0.0)
        sy[j] += y[i]
        cnt[j] += 1.0
    return rows, sy, cnt


# ────────────────────────────────────────────────────────────
# Logistic regression（IRLS / Newton-Raphson）
# ────────────────────────────────────────────────────────────
@dataclass
class LogisticFit:
    beta: list[float]
    se: list[float]
    cov: list[list[float]]
    loglik: float
    n_iter: int
    converged: bool
    ridge: float
    n_obs: int
    link: str = "logit"
    separation_warning: str = ""
    cluster_se: list[float] | None = None
    cluster_info: dict = field(default_factory=dict)

    def odds_ratio(self, i: int) -> float:
        return math.exp(self.beta[i])

    def or_ci(self, i: int, alpha: float = 0.05, use_cluster: bool = True) -> tuple[float, float]:
        z = _z_quantile(1 - alpha / 2)
        se = self.effective_se(i, use_cluster)
        return math.exp(self.beta[i] - z * se), math.exp(self.beta[i] + z * se)

    def wald_p(self, i: int, use_cluster: bool = True) -> float:
        se = self.effective_se(i, use_cluster)
        if se <= 0:
            return 1.0
        z = self.beta[i] / se
        return 2 * (1 - _normal_cdf(abs(z)))

    def effective_se(self, i: int, use_cluster: bool = True) -> float:
        if use_cluster and self.cluster_se:
            return self.cluster_se[i]
        return self.se[i]


def _expit(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


# ── 連結函數（link）─────────────────────────────────────────
# logit ：加性對數勝算 → 適合「每個共變數各自貢獻一點風險」的機制
# cloglog：P(bad) = 1 − exp(−Σ_t h_t)（h_t = exp(β_t) 為各機台的「風險率」）
#          → **正好對應「路線只要含任一故障機台就會壞」的 OR 型機制**
#          （本專案的壞批次定義正是「曾落在任一故障機台的故障窗內」）
def _mu_from_eta(eta: float, link: str) -> float:
    if link == "logit":
        return _expit(eta)
    if link == "cloglog":
        e = math.exp(eta) if eta < 700 else float("inf")
        p = 1.0 - math.exp(-e)
        return min(max(p, 1e-300), 1 - 1e-16)
    raise ValueError(f"未知的連結函數：{link}")


def _dmu_deta(mu: float, eta: float, link: str) -> float:
    """dμ/dη（用於 IRLS 的權重與工作反應）"""
    if link == "logit":
        return mu * (1 - mu)
    if link == "cloglog":
        e = math.exp(eta) if eta < 700 else float("inf")
        return max((1.0 - mu) * e, 1e-300)
    raise ValueError(f"未知的連結函數：{link}")


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _z_quantile(p: float) -> float:
    """標準常態分位數（Acklam 近似，誤差 ~1e-9）。"""
    if not 0 < p < 1:
        raise ValueError("p 必須在 (0,1)")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def logistic_fit(X: list[list[float]], y: list[int], ridge: float = 0.0,
                 max_iter: int = 200, tol: float = 1e-10,
                 colnames: list[str] | None = None, link: str = "logit") -> LogisticFit:
    """IRLS 擬合廣義線性模型（link = "logit" 或 "cloglog"）。

    ridge：對「非截距」係數加 λ‖β‖² 的懲罰（Bayesian 後驗變異數 (XᵀWX+λI)⁻¹）。
           完全分離（MLE 不存在）時，ridge > 0 才會有有限解。
    """
    if link not in ("logit", "cloglog"):
        raise ValueError(f"未知的連結函數：{link}")
    n, k = len(X), len(X[0])
    beta = [0.0] * k
    names = colnames or [f"x{i}" for i in range(k)]
    is_intercept = [nm.lower() in ("intercept", "const", "(intercept)") for nm in names]
    pen = [0.0 if is_intercept[i] else ridge for i in range(k)]

    # 壓縮相同設計列：迭代用樣式層級計算（與逐列完全等價，但快上數個量級）
    Xc, sy, cnt = compress_rows(X, y)
    nc = len(Xc)
    converged, n_iter = False, 0
    for it in range(1, max_iter + 1):
        n_iter = it
        eta = [sum(Xc[i][a] * beta[a] for a in range(k)) for i in range(nc)]
        p = [_mu_from_eta(e, link) for e in eta]
        dmude = [_dmu_deta(p[i], eta[i], link) for i in range(nc)]
        # Fisher scoring：資訊 I = XᵀWX、分數 U = Xᵀ(y−μ)，以「次數」加權
        w = [max(d * d / max(p[i] * (1 - p[i]), 1e-12), 1e-12) * cnt[i]
             for i, d in enumerate(dmude)]
        r = [(sy[i] - cnt[i] * p[i]) for i in range(nc)]
        A = xtwx(Xc, w)
        for i in range(k):
            A[i][i] += pen[i]
        grad = xtv(Xc, r)
        for i in range(k):
            grad[i] -= pen[i] * beta[i]
        try:
            step = solve_linear(A, grad)
        except ValueError:
            break
        beta = [beta[i] + step[i] for i in range(k)]
        if max(abs(s) for s in step) < tol:
            converged = True
            break

    eta = [sum(X[i][a] * beta[a] for a in range(k)) for i in range(n)]
    p = [_mu_from_eta(e, link) for e in eta]
    dmude = [_dmu_deta(p[i], eta[i], link) for i in range(n)]
    w = [max(d * d / max(p[i] * (1 - p[i]), 1e-12), 1e-12) for i, d in enumerate(dmude)]
    A = xtwx(X, w)
    for i in range(k):
        A[i][i] += pen[i]
    cov = invert(A)
    se = [math.sqrt(max(cov[i][i], 0.0)) for i in range(k)]
    loglik = 0.0
    for i in range(n):
        pi = min(max(p[i], 1e-300), 1 - 1e-16)
        loglik += y[i] * math.log(pi) + (1 - y[i]) * math.log(1 - pi)
    loglik -= 0.5 * sum(pen[i] * beta[i] ** 2 for i in range(k))   # 懲罰後的目標函數

    warn = ""
    if max(abs(b) for b in beta) > 20:
        warn = "係數過大 → 可能有完全分離（建議 ridge > 0）"
    elif not converged:
        warn = f"未在 {max_iter} 次迭代內收斂"
    return LogisticFit(beta=beta, se=se, cov=cov, loglik=loglik, n_iter=n_iter,
                       converged=converged, ridge=ridge, n_obs=n, link=link,
                       separation_warning=warn)


def cluster_robust_cov(X: list[list[float]], y: list[list[int]] | list[int], beta: list[float],
                       clusters: list, ridge: float = 0.0,
                       colnames: list[str] | None = None,
                       link: str = "logit") -> tuple[list[list[float]], dict]:
    """群聚穩健共變異矩陣（sandwich）。**對比檢定需要整個矩陣**，不只是對角線。"""
    """群聚穩健標準誤（Huber-White sandwich，以 clusters 分群）。

    為什麼需要：同一 recipe／同一時段的批次彼此相關（偽重複），naive 標準誤會低估、
    p 值過度樂觀。sandwich 對這種群內相關不做分配假設即可修正。

    V = (XᵀWX + λI)⁻¹ [ Σ_g (X_gᵀ r_g)(X_gᵀ r_g)ᵀ ] (XᵀWX + λI)⁻¹
    """
    n, k = len(X), len(X[0])
    names = colnames or [f"x{i}" for i in range(k)]
    is_intercept = [nm.lower() in ("intercept", "const", "(intercept)") for nm in names]
    pen = [0.0 if is_intercept[i] else ridge for i in range(k)]

    eta = [sum(X[i][a] * beta[a] for a in range(k)) for i in range(n)]
    p = [_mu_from_eta(e, link) for e in eta]
    dmude = [_dmu_deta(p[i], eta[i], link) for i in range(n)]
    w = [max(d * d / max(p[i] * (1 - p[i]), 1e-12), 1e-12) for i, d in enumerate(dmude)]
    r = [y[i] - p[i] for i in range(n)]
    A = xtwx(X, w)
    for i in range(k):
        A[i][i] += pen[i]
    Ainv = invert(A)

    groups: dict = {}
    for i, g in enumerate(clusters):
        acc = groups.get(g)
        if acc is None:
            acc = groups[g] = [0.0] * k
        for a in range(k):
            acc[a] += X[i][a] * r[i]
    meat = [[0.0] * k for _ in range(k)]
    for sg in groups.values():
        for a in range(k):
            if sg[a]:
                for b in range(k):
                    meat[a][b] += sg[a] * sg[b]
    V = matmul(matmul(Ainv, meat), Ainv)

    g = len(groups)
    infl = g / max(g - 1, 1)                     # 小樣本修正
    if n > k:
        infl *= (n - 1) / (n - k)
    V = [[V[i][j] * infl for j in range(k)] for i in range(k)]
    return V, {"n_clusters": g, "inflation": infl}


def cluster_robust_se(X: list[list[float]], y: list[list[int]] | list[int], beta: list[float],
                      clusters: list, ridge: float = 0.0,
                      colnames: list[str] | None = None, link: str = "logit") -> tuple[list[float], dict]:
    """群聚穩健標準誤（= 共變異矩陣對角線開根號）"""
    V, info = cluster_robust_cov(X, y, beta, clusters, ridge=ridge, colnames=colnames, link=link)
    return [math.sqrt(max(V[i][i], 0.0)) for i in range(len(V))], info


def contrast_p(c: list[float], beta: list[float], V: list[list[float]]) -> tuple[float, float]:
    """線性對比的 Wald 檢定：H0 為 cᵀβ = 0，回傳 (cᵀβ, p 值)。

    為什麼需要：**無截距**的路線模型裡，單一係數 β_j = 0 的檢定等同於檢定
    「基準機率 = 50%」（logit）或「風險率 = 1」（cloglog／hazard），這個虛無假設
    沒有意義——實測上負控制（完全不注入任何效應）資料會讓**每一台都變成極度顯著**。
    有意義的問題是「這一台和其他台比起來如何」，因此用對比向量
        c = e_j − 其他係數的（依批次數加權）平均
    """
    k = len(beta)
    est = sum(c[i] * beta[i] for i in range(k))
    var = sum(c[i] * V[i][j] * c[j] for i in range(k) for j in range(k))
    if var <= 0:
        return est, 1.0
    z = est / math.sqrt(var)
    return est, 2 * (1 - _normal_cdf(abs(z)))


# ────────────────────────────────────────────────────────────
# 風險率相加模型（additive hazards）— 對「至少一台造成」機制的正確模型
# ────────────────────────────────────────────────────────────
# 為什麼需要（M4-2 的關鍵發現）
# ────────────────────────────────────────────────────────────
# 本專案的壞批次定義是「批次曾落在**任一**所屬機台的故障窗內」＝**至少一台**造成的機制：
#
#     P(bad) = 1 − exp(−Σ_{t∈路線} h_t)        h_t > 0 為機台 t 的風險率
#
# logistic（logit）與 cloglog 都是**線性**預測式，對指示變數等同於「風險率相乘」：
#     cloglog → λ = Π h_t^{I_t}；logit → 勝算 = Π OR_t^{I_t}
# 當路線長度會變（1–3 台機台）且真實機制是相加時，線性模型只能妥協逼近，
# 無辜機台的自身效應會被高估（合成資料實測：真實 2% 被估成 19%）。
# 因此本專案另外實作**非線性**的風險率相加模型（Σ h_t，用對數參數化保證 h > 0）。


def _hazard_negll(theta: list[float], X: list[list[float]], y: list[int],
                  ridge: float, compressed: tuple | None = None) -> float:
    """負對數概似（可傳入 compress_rows 的結果加速；樣式層級與逐列完全等價）"""
    if compressed is None:
        Xc, sy, cnt = compress_rows(X, y)
    else:
        Xc, sy, cnt = compressed
    h = [math.exp(t) for t in theta]
    ll = 0.0
    for i, row in enumerate(Xc):
        lam = 0.0
        for a, xa in enumerate(row):
            if xa:
                lam += h[a] * xa
        p = 1.0 - math.exp(-lam)
        p = min(max(p, 1e-300), 1 - 1e-16)
        ll += sy[i] * math.log(p) + (cnt[i] - sy[i]) * math.log(1 - p)
    return -ll + 0.5 * ridge * sum(t * t for t in theta)


def _numeric_grad_hess(f, theta: list[float]) -> tuple[list[float], list[list[float]]]:
    """數值梯度與 Hessian（中心差分；參數少（≤10）時足夠快且不易寫錯）。

    二階偏導的四點公式（i ≠ j）：
        ∂²f/∂xᵢ∂xⱼ ≈ [f(x+eᵢ+eⱼ) − f(x+eᵢ−eⱼ) − f(x−eᵢ+eⱼ) + f(x−eᵢ−eⱼ)] / (4h²)
    i = j 時退化為 [f(x+2eᵢ) − 2f(x) + f(x−2eᵢ)] / (4h²)（步長 2h 的標準二階差分）。
    四個點必須一一對應正負號，先前版本的正負號順序寫錯 → 不收斂、標準誤假性極小。
    """
    k = len(theta)
    h = 1e-4
    f0 = f(theta)
    grad = [(f([theta[j] + (h if j == i else 0) for j in range(k)])
             - f([theta[j] - (h if j == i else 0) for j in range(k)])) / (2 * h)
            for i in range(k)]
    hess = [[0.0] * k for _ in range(k)]
    for i in range(k):
        for j in range(i, k):
            if i == j:
                plus = f([theta[a] + (2 * h if a == i else 0) for a in range(k)])
                minus = f([theta[a] - (2 * h if a == i else 0) for a in range(k)])
                val = (plus - 2 * f0 + minus) / (4 * h * h)
            else:
                pp = f([theta[a] + (h if a in (i, j) else 0) for a in range(k)])
                pm = f([theta[a] + (h if a == i else (-h if a == j else 0)) for a in range(k)])
                mp = f([theta[a] + (-h if a == i else (h if a == j else 0)) for a in range(k)])
                mm = f([theta[a] - (h if a in (i, j) else 0) for a in range(k)])
                val = (pp - pm - mp + mm) / (4 * h * h)
            hess[i][j] = hess[j][i] = val
    return grad, hess


def hazard_sum_grad_hess(theta: list[float], X: list[list[float]], y: list[int], ridge: float,
                         compressed: tuple | None = None
                         ) -> tuple[list[float], list[list[float]]]:
    """風險率相加模型的**解析**梯度與 Hessian（O(n·k²)，只掃資料一次）。

    推導（λ_i = Σ_t h_t·x_it、h_t = exp(θ_t)、p_i = 1 − exp(−λ_i)、
    第 i 列有 c_i 個觀測、其中 sy_i 個壞）：

        ∂p_i/∂θ_t = (1 − p_i)·h_t·x_it
        A_i       = c_i − sy_i / p_i
        ∂NLL/∂θ_t = h_t · Σ_i x_it·A_i
        ∂²NLL/∂θ_t∂θ_s = δ_ts·(∂NLL/∂θ_t) + h_t·h_s·Σ_i x_it·x_is·sy_i·(1−p_i)/p_i²

    為什麼必須這樣做（實測教訓）：用數值 Hessian 時，每個牛頓迭代要呼叫對數概似
    約 k² 次、每次又是 O(n·k) → 整體 ≈ **k⁴** 成長。實測成長指數 3.86：
    k=12 時 3.5 秒，外推到 recipe 維度的 k=156 → 單次擬合就要數十小時。
    解析式把每迭代降到 O(n·k²) 且只掃一次資料。
    """
    if compressed is None:
        compressed = compress_rows(X, y)
    Xc, sy, cnt = compressed
    k = len(theta)
    h = [math.exp(t) for t in theta]
    grad = [0.0] * k
    H = [[0.0] * k for _ in range(k)]
    for row, s_i, c_i in zip(Xc, sy, cnt):
        lam = 0.0
        for a, xa in enumerate(row):
            if xa:
                lam += h[a] * xa
        # expm1 讓極小風險率的 1−exp(−λ) 不失精度（λ ~ 1e-9 時 1−exp(−λ) 會直接變 0）
        p = -math.expm1(-lam)
        p = min(max(p, 1e-300), 1 - 1e-16)
        # 數值穩定：p 極小時用 λ 代替（兩者在該區間相等），避免 p*p 下溢成 0；
        # 且只有真有壞觀測的樣式（s_i > 0）才需要二階項，其餘 coef = 0
        pe = p if p > 1e-8 else max(lam, 1e-140)
        A = c_i - s_i / pe
        # 上界 1e300 避免 (pe*pe) 下溢或 coef 溢位成 inf（pe >= 1e-140 → pe² >= 1e-280）
        coef = min(s_i * (1.0 - pe) / (pe * pe), 1e300) if s_i > 0 else 0.0
        # 只走非零欄位（路線樣式稀疏）
        nz = [(a, xa) for a, xa in enumerate(row) if xa]
        for a, xa in nz:
            grad[a] += h[a] * xa * A
        if coef:
            for a, xa in nz:
                ha = h[a] * xa
                Ha = H[a]
                for b, xb in nz:
                    Ha[b] += ha * h[b] * xb * coef
    # 對角線的 δ_ts 項是「**資料項**的梯度 + ridge」：
    #   ∂²NLL/∂θ_t² = ∂²NLL_資料/∂θ_t² + ridge
    # 這裡若誤用已含 ridge·θ 的梯度，會差 ridge·(1 − θ_t)（實測 k=1 時差 2·ridge，被測試抓到）
    dg = list(grad)
    for a in range(k):
        grad[a] += ridge * theta[a]
        H[a][a] += dg[a] + ridge
    return grad, H


def hazard_sum_fit(X: list[list[float]], y: list[int], ridge: float = 0.01,
                   max_iter: int = 100, tol: float = 1e-9,
                   colnames: list[str] | None = None) -> dict:
    """擬合 P(bad) = 1 − exp(−Σ h_j X_j)，h_j = exp(θ_j)。

    回傳 dict：theta, hazard（= exp(θ), 相當於「風險率」）, se, cov, loglik,
    n_iter, converged, link="hazard_sum"。
    """
    k = len(X[0])
    names = colnames or [f"x{j}" for j in range(k)]
    # 起始值：用各欄位自身的事件率取對數（避免遠離最優點）
    theta = []
    for j in range(k):
        n_j = sum(1 for row in X if row[j])
        k_j = sum(1 for row, yy in zip(X, y) if row[j] and yy)
        rate = (k_j / n_j) if n_j else 1e-3
        theta.append(math.log(max(rate, 1e-3)))

    compressed = compress_rows(X, y)
    f = lambda th: _hazard_negll(th, X, y, ridge, compressed)   # noqa: E731
    converged, n_iter = False, 0
    for it in range(1, max_iter + 1):
        n_iter = it
        grad, hess = hazard_sum_grad_hess(theta, X, y, ridge, compressed)
        try:
            step = solve_linear(hess, [-g for g in grad])
        except ValueError:
            break
        scale = 1.0
        base = f(theta)
        improved = False
        for _ in range(12):                                # 步長折半確保下降
            cand = [theta[j] + scale * step[j] for j in range(k)]
            if f(cand) < base:
                improved = True
                break
            scale /= 2
        if not improved:
            # 線搜尋找不到更低的點：若梯度/步長已經很小，就當作收斂
            # （浮點誤差會讓 f(cand) == f(theta)，此時不算失敗）
            gn = max(abs(v) for v in grad)
            if gn < 1e-4 * max(len(X), 1) or (step and max(abs(x) for x in step) < 1e-6):
                converged = True
            break
        theta = [theta[j] + scale * step[j] for j in range(k)]
        # 收斂判準要看「梯度是否已接近 0」，不能只看步長：
        # 步長折半失敗（線搜尋找不到更低的點）時步長會很小，但那不代表到最優點
        if max(abs(g) for g in grad) < tol * max(len(X), 1) or max(abs(scale * s) for s in step) < tol:
            converged = True
            break

    grad, hess = hazard_sum_grad_hess(theta, X, y, ridge, compressed)
    try:
        cov = invert(hess)
    except ValueError:
        cov = [[0.0] * k for _ in range(k)]
    se = [math.sqrt(max(cov[j][j], 0.0)) for j in range(k)]
    return {"theta": theta, "hazard": [math.exp(t) for t in theta], "se": se, "cov": cov,
            "names": names, "loglik": -f(theta), "n_iter": n_iter, "converged": converged,
            "ridge": ridge, "link": "hazard_sum", "n_obs": len(X)}


def hazard_sum_lrt(X: list[list[float]], y: list[int], theta_full: list[float],
                   col_index: int, ridge: float = 0.01) -> tuple[float, float]:
    """邊界修正的概似比檢定：H₀: h_j = 0（這個群組完全沒有風險）vs H₁: h_j > 0。

    為什麼不能用 Wald 檢定：對數參數化下 Wald 檢定 θ_j = 0 等於檢定 **h_j = 1**
    （風險率 1 ＝必然發生），這個虛無假設毫無意義——實測時每一台都會變成
    「極度顯著」（真實資料曾出現全部 0E-8）。而 h_j = 0 位於參數空間邊界，
    Wald 統計量在此退化，必須用**似然比**。

    做法：把該欄位移除後重新擬合（少一個參數），
    LR = 2(LL_全模型 − LL_縮減模型)，p = 0.5·P(χ²₁ > LR)
    （邊界虛無假設的 50:50 混合修正）。
    """
    ll_full = -_hazard_negll(theta_full, X, y, ridge)
    Xr = [[v for j, v in enumerate(row) if j != col_index] for row in X]
    if not Xr or not Xr[0]:
        return 0.0, 1.0
    fit_r = hazard_sum_fit(Xr, y, ridge=ridge)
    lr = max(2.0 * (ll_full - fit_r["loglik"]), 0.0)
    return lr, 0.5 * math.erfc(math.sqrt(lr / 2.0))


def hazard_sum_predict(theta: list[float], row: list[float]) -> float:
    lam = sum(math.exp(theta[a]) * xa for a, xa in enumerate(row) if xa)
    return 1.0 - math.exp(-lam)


def hazard_sum_cluster_se(X: list[list[float]], y: list[int], theta: list[float],
                          clusters: list, ridge: float = 0.01) -> tuple[list[float], dict]:
    """風險率相加模型的群聚穩健標準誤：bread = 解析 Hessian、meat = 分數函數群聚和。"""
    k = len(X[0])
    _grad, hess = hazard_sum_grad_hess(theta, X, y, ridge)
    try:
        bread = invert(hess)
    except ValueError:
        return [0.0] * k, {"n_clusters": 0, "inflation": 1.0}
    scores: dict = {}
    for i, row in enumerate(X):
        lam = sum(math.exp(theta[a]) * row[a] for a in range(k) if row[a])
        # 數值穩定：λ 極小時 1−exp(−λ) 下溢成 0（實測 ValueError: float division by zero）。
        # 用 expm1 並對分母設下限——此時分數函數本來就會極大（模型說安全卻真的壞了），
        # 讓它保持有限大（→ 標準誤變大、p 值變大）比直接爆掉誠實。
        den = max(-math.expm1(-lam), 1e-140)
        # dL/dθ_j = x_j e^{θ_j} · [ y e^{−λ}/(1−e^{−λ}) − (1−y) ]
        common = y[i] * math.exp(-lam) / den - (1 - y[i])
        g = clusters[i]
        acc = scores.get(g)
        if acc is None:
            acc = scores[g] = [0.0] * k
        for a in range(k):
            if row[a]:
                acc[a] += row[a] * math.exp(theta[a]) * common
    meat = [[0.0] * k for _ in range(k)]
    for sg in scores.values():
        for a in range(k):
            if sg[a]:
                for b in range(k):
                    meat[a][b] += sg[a] * sg[b]
    V = matmul(matmul(bread, meat), bread)
    g_n = len(scores)
    infl = g_n / max(g_n - 1, 1)
    if len(X) > k:
        infl *= (len(X) - 1) / (len(X) - k)
    return [math.sqrt(max(V[j][j] * infl, 0.0)) for j in range(k)], {"n_clusters": g_n, "inflation": infl}


# ────────────────────────────────────────────────────────────
# Beta-binomial 經驗貝氏收縮
# ────────────────────────────────────────────────────────────
def _log_beta_binomial_pmf(k: float, n: float, a: float, b: float) -> float:
    return (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
            + math.lgamma(k + a) + math.lgamma(n - k + b) - math.lgamma(n + a + b)
            + math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b))


def beta_binomial_fit(ks: list[float], ns: list[float]) -> dict:
    """以最大概似估計 Beta-binomial 的先驗參數 (α, β)。

    用「粗網格 + 兩輪局部細化」的最佳化（純 Python、確定性、可重現）。
    參數化為 μ = α/(α+β)（整體平均）與 θ = α+β（精度，越大表示群組間差異越小）。
    """
    if not ks or len(ks) != len(ns):
        raise ValueError("ks 與 ns 必須等長且非空")
    if sum(ns) <= 0:
        raise ValueError("總樣本數必須 > 0")
    total_k, total_n = sum(ks), sum(ns)
    mu0 = total_k / total_n

    def neg_ll(logit_mu: float, log_theta: float) -> float:
        mu = _expit(logit_mu)
        theta = math.exp(log_theta)
        a, b = mu * theta, (1 - mu) * theta
        if a <= 1e-9 or b <= 1e-9:
            return 1e18
        try:
            return -sum(_log_beta_binomial_pmf(k, n, a, b) for k, n in zip(ks, ns))
        except (ValueError, OverflowError):
            return 1e18

    def refine(lo_mu, hi_mu, lo_th, hi_th, steps):
        best, best_v = (None, None), 1e18
        for i in range(steps + 1):
            lm = lo_mu + (hi_mu - lo_mu) * i / steps
            for j in range(steps + 1):
                lt = lo_th + (hi_th - lo_th) * j / steps
                v = neg_ll(lm, lt)
                if v < best_v:
                    best_v, best = v, (lm, lt)
        return best, best_v

    lo_mu, hi_mu = math.log(mu0 / (1 - mu0)) - 4, math.log(mu0 / (1 - mu0)) + 4
    lo_th, hi_th = math.log(0.05 + 1e-6), math.log(5e4)
    for steps, shrink in ((60, 1.0), (40, 0.16), (40, 0.03)):
        (lm, lt), v = refine(lo_mu, hi_mu, lo_th, hi_th, steps)
        span_mu = (hi_mu - lo_mu) * shrink / 2
        span_th = (hi_th - lo_th) * shrink / 2
        lo_mu, hi_mu = lm - span_mu, lm + span_mu
        lo_th, hi_th = lt - span_th, lt + span_th

    mu = _expit(lm)
    theta = math.exp(lt)
    a, b = mu * theta, (1 - mu) * theta
    return {"alpha": a, "beta": b, "mu": mu, "theta": theta, "neg_loglik": v,
            "overdispersion": 1.0 / (theta + 1.0)}


def shrink_rates(ks: list[float], ns: list[float], alpha: float, beta: float) -> list[float]:
    """收縮後的比率（後驗平均）= (k + α) / (n + α + β)。"""
    return [(k + alpha) / (n + alpha + beta) for k, n in zip(ks, ns)]


def shrinkage_weight(n: float, alpha: float, beta: float) -> float:
    """資料本身所佔的權重；n 越小 → 越往先驗平均收縮（= n / (n + α + β)）"""
    return n / (n + alpha + beta)


# ────────────────────────────────────────────────────────────
# 排名相關（名次重疊度）
# ────────────────────────────────────────────────────────────
def _average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for t in range(i, j + 1):
            ranks[order[t]] = avg
        i = j + 1
    return ranks


def spearman(x: list[float], y: list[float]) -> float:
    """Spearman 等級相關（並列名次取平均；完全同序 = 1、完全反序 = -1）。"""
    if len(x) != len(y) or len(x) < 2:
        raise ValueError("兩序列需等長且長度 ≥ 2")
    rx, ry = _average_ranks(x), _average_ranks(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def topk_overlap(a: list[str], b: list[str], k: int) -> float:
    """兩份排序清單前 k 名的重疊比例（用於「不同方法排名是否一致」）。"""
    if k <= 0:
        raise ValueError("k 必須 ≥ 1")
    sa, sb = set(a[:k]), set(b[:k])
    return len(sa & sb) / max(len(sa | sb), 1)


# ────────────────────────────────────────────────────────────
# 多重比較：BH-FDR（M3 已實作於 analysis/multiple_testing.py，這裡提供便利包裝）
# ────────────────────────────────────────────────────────────
def bh_qvalues(pvals: list[float]) -> list[float]:
    """Benjamini-Hochberg FDR：回傳與輸入同序的 q 值。"""
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    q = [0.0] * m
    prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        val = min(prev, pvals[i] * m / rank)
        q[i] = prev = val
    return q
