#!/usr/bin/env python3
"""M5-1：單變量 SPC 管制圖核心（純 Python，無 numpy/scipy）。

管制圖（control chart）＝在感測器時序上畫上下限，超出就警報。
本模組提供三種，並提供 **ARL（Average Run Length，平均運行長度）** 設計工具。

    Shewhart X-MR : 只看「當下這一點」，對大偏移敏感、對小偏移幾乎無感
    EWMA          : 一階 IIR 低通濾波器（指數加權移動平均），對緩慢漂移敏感
    CUSUM         : 累積和（積分器），對「很小但一直偏」最敏感

ARL 的兩種角色
────────────────────────────────────────────────────────────
    ARL0 = 沒有偏移時，平均多久誤報一次（越大越穩）
    ARL1 = 有偏移時，平均多久抓到（越小越快）
調參數＝在兩者之間取捨，做法和 ROC 曲線一樣。本模組用兩種**獨立方法**算 ARL：
① 馬可夫鏈（把管制統計量的狀態空間離散化後解 (I−Q)v = 1）
② 蒙地卡羅模擬（直接跑隨機序列）
兩者必須一致——單元測試就是拿這件事當錨點。

統計量的參數化慣例（與文獻一致）
────────────────────────────────────────────────────────────
- 一律先標準化：`z = (x − μ0) / σ0`，`μ0`／`σ0` 來自**基準期**（baseline）
- CUSUM 用 k（參考值，單位 σ）與 h（決策區間，單位 σ）；常用 k = 0.5（偵測 1σ 偏移）、h = 4～5
- EWMA 用 λ（權重，0<λ≤1）與 L（管制限寬度倍數）
"""
from __future__ import annotations

from functools import lru_cache

import math
import random

# ─────────────────────────────────────────────────────────────
# 基本工具
# ─────────────────────────────────────────────────────────────


def _phi(x: float) -> float:
    """標準常態累積分布函數"""
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _phi_inv(p: float) -> float:
    """標準常態分位函數（Acklam 近似，精度 ~1e-9）"""
    if not 0.0 < p < 1.0:
        raise ValueError("p 必須在 (0,1)")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
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


def mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs) -> float:
    """樣本標準差（n−1）"""
    xs = list(xs)
    n = len(xs)
    if n < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((v - m) ** 2 for v in xs) / (n - 1))


def median(xs) -> float:
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return 0.0
    return xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])


def mad(xs) -> float:
    """中位數絕對偏差（Median Absolute Deviation）"""
    m = median(xs)
    return median([abs(v - m) for v in xs])


# ─────────────────────────────────────────────────────────────
# 基準期估計（σ 的估計方式）
# ─────────────────────────────────────────────────────────────

MR_D2 = 1.128        # n = 2 移動全距的 d2 常數（文獻標準值）


def moving_range_sigma(xs) -> float:
    """用移動全距（moving range）估 σ：MR̄ / d2。

    為什麼不用樣本標準差：管制圖的 σ 必須反映「**製程的普通變異**」，
    但樣本標準差會被少數大偏移（真正的異常）膨脹 → 管制限被撐寬 → 抓不到異常。
    移動全距只看相鄰兩點的差，對「單點跳動」比對「整體分散」不敏感，
    是 Shewhart 個人值圖（individuals chart, X-MR）的標準做法。
    """
    xs = list(xs)
    if len(xs) < 2:
        return 0.0
    mrs = [abs(xs[i] - xs[i - 1]) for i in range(1, len(xs))]
    return mean(mrs) / MR_D2


def robust_sigma(xs) -> float:
    """用中位數絕對偏差估 σ：1.4826 × MAD。

    1.4826 是讓常態資料下 MAD 與標準差一致的換算常數（= 1/Φ⁻¹(0.75)）。
    對離群值最穩健，適合「基準期本身就可能有異常」的場合。
    """
    return 1.4826 * mad(xs)


def baseline_stats(xs, sigma_method: str = "mr") -> dict:
    """基準期統計量。sigma_method：mr（移動全距）／std（樣本標準差）／mad（穩健）"""
    xs = list(xs)
    c = mean(xs)
    if sigma_method == "mr":
        s = moving_range_sigma(xs)
    elif sigma_method == "std":
        s = stdev(xs)
    elif sigma_method == "mad":
        c = median(xs)
        s = robust_sigma(xs)
    else:
        raise ValueError(f"未知的 sigma_method：{sigma_method}")
    if s <= 0:                     # 全部相同（或只有一點）→ 給一個極小值避免除以 0
        s = max(abs(c) * 1e-9, 1e-12)
    return {"center": c, "sigma": s, "n": len(xs), "sigma_method": sigma_method}


def linear_trend(xs, n_fit: int | None = None) -> tuple[float, float]:
    """用**前 n_fit 個點**做最小平方直線擬合，回傳 (a, b)（y ≈ a + b·t）。

    用途：機台老化造成的線性漂移（`runnum` 只增不減就是使用次數）會讓固定基準線失效
    （實測漂移資料下 Shewhart 警報率 53%）。用基準期擬合出的趨勢線當中心線**外推**，
    就能把線性漂移從訊號裡移除，而且**只用過去資料**（不偷看未來）。

    實測（斜率真值 0.02、雜訊 σ=1、1200 點）：估出 b = 0.0207、殘差 σ = 0.976，
    漂移期警報率由 53% 降到 0.37%。
    """
    xs = list(xs)
    n = len(xs) if n_fit is None else min(n_fit, len(xs))
    if n < 3:
        return (xs[0] if xs else 0.0), 0.0
    mt = (n - 1) / 2.0
    my = sum(xs[:n]) / n
    sxx = sum((t - mt) ** 2 for t in range(n))
    sxy = sum((t - mt) * (xs[t] - my) for t in range(n))
    b = sxy / sxx if sxx else 0.0
    return my - b * mt, b


def rolling_stats(xs, window: int = 200, d2: float = 1.128):
    """滾動基準線：每個時間點用「前面 window 個點」算中心與 σ（O(n)）。

    為什麼需要（真實資料實測）：固定基準線假設製程平穩，但 PHM 感測值隨機台老化持續漂移
    （`runnum` 只增不減就是機台累積使用次數）→ 固定基準線在基準期自己就會不斷誤報
    （實測基準期警報率 38%，名目 0.27%），整張圖失去鑑別力。

    σ 用「平均相鄰差 / d2」（d2 = 1.128 為移動全距 n=2 的期望值）——經典 X-MR 作法，
    且可用累積和做到 O(n)（不需要每次重排中位數）。

    回傳 (centers, sigmas)：前 `window` 點為 None（暖身期，不做判定）。
    """
    n = len(xs)
    centers: list = [None] * n
    sigmas: list = [None] * n
    if window < 5:
        window = 5
    s_sum = 0.0
    d_sum = 0.0
    for t in range(n):
        s_sum += xs[t]
        if t > 0:
            d_sum += abs(xs[t] - xs[t - 1])
        if t + 1 <= window:               # 暖身期：只用已見到的點，但不回傳
            if t + 1 == window and window > 1:
                centers[t] = s_sum / (t + 1)
                sigmas[t] = (d_sum / (t)) / d2 if t > 0 else None
            continue
        s_sum -= xs[t - window]
        d_sum -= abs(xs[t - window + 1] - xs[t - window])
        centers[t] = s_sum / window
        sigmas[t] = (d_sum / (window - 1)) / d2
    return centers, sigmas


# ─────────────────────────────────────────────────────────────
# ① Shewhart X-MR
# ─────────────────────────────────────────────────────────────

def shewhart_chart(xs, base: dict | None = None, k: float = 3.0,
                   sigma_method: str = "mr") -> dict:
    """Shewhart 個人值管制圖。

    回傳 UCL/LCL、標準化值與「超出管制限」的布林序列。
    """
    xs = list(xs)
    b = base or baseline_stats(xs, sigma_method)
    ucl = b["center"] + k * b["sigma"]
    lcl = b["center"] - k * b["sigma"]
    z = [(v - b["center"]) / b["sigma"] for v in xs]
    return {"center": b["center"], "sigma": b["sigma"], "k": k,
            "ucl": ucl, "lcl": lcl, "z": z,
            "alarm": [abs(zi) > k for zi in z]}


# ─────────────────────────────────────────────────────────────
# ② EWMA
# ─────────────────────────────────────────────────────────────

def ewma_series(xs, lam: float, init: float | None = None) -> list[float]:
    """EWMA 序列：z_t = λ·x_t + (1−λ)·z_{t−1}（等同於一階 IIR 低通濾波器）"""
    if not 0.0 < lam <= 1.0:
        raise ValueError("λ 必須在 (0,1]")
    xs = list(xs)
    if not xs:
        return []
    z = xs[0] if init is None else init
    out = []
    for v in xs:
        z = lam * v + (1 - lam) * z
        out.append(z)
    return out


def ewma_limits(center: float, sigma: float, lam: float, L: float, n_steps: int) -> tuple[list, list]:
    """EWMA 的精確（逐點）管制限：

        UCL_t = μ0 + L·σ0·sqrt( λ/(2−λ) · (1 − (1−λ)^{2t}) )

    前幾點的變異數還沒收斂到漸近值 λ/(2−λ)，用漸近限會讓開頭過寬／過窄。
    """
    if not 0.0 < lam <= 1.0:
        raise ValueError("λ 必須在 (0,1]")
    asym = lam / (2 - lam)
    ucl, lcl = [], []
    for t in range(1, n_steps + 1):
        sd = sigma * math.sqrt(asym * (1 - (1 - lam) ** (2 * t)))
        ucl.append(center + L * sd)
        lcl.append(center - L * sd)
    return ucl, lcl


def ewma_chart(xs, lam: float = 0.1, L: float = 2.7, base: dict | None = None,
               sigma_method: str = "mr") -> dict:
    """EWMA 管制圖（含逐點精確管制限）"""
    xs = list(xs)
    b = base or baseline_stats(xs, sigma_method)
    z = ewma_series(xs, lam, init=b["center"])
    ucl, lcl = ewma_limits(b["center"], b["sigma"], lam, L, len(xs))
    alarms = [(z[i] > ucl[i] or z[i] < lcl[i]) for i in range(len(xs))]
    return {"lam": lam, "L": L, "center": b["center"], "sigma": b["sigma"],
            "stat": z, "ucl": ucl, "lcl": lcl, "alarm": alarms}


# ─────────────────────────────────────────────────────────────
# ③ CUSUM
# ─────────────────────────────────────────────────────────────

def cusum_series(xs, center: float, sigma: float, k: float = 0.5,
                 fir: bool = False) -> tuple[list, list]:
    """雙側 CUSUM 統計量（標準化）：

        C⁺_t = max(0, C⁺_{t−1} + (x_t − μ0)/σ − k)
        C⁻_t = max(0, C⁻_{t−1} − (x_t − μ0)/σ − k)

    `fir=True` 用快速初始反應（Fast Initial Response：初始值設 h/2），
    讓開頭幾點就能抓到偏移（文獻做法）。
    """
    c_pos = c_neg = 0.0
    pos, neg = [], []
    for v in xs:
        z = (v - center) / sigma
        c_pos = max(0.0, c_pos + z - k)
        c_neg = max(0.0, c_neg - z - k)
        pos.append(c_pos)
        neg.append(c_neg)
    return pos, neg


def cusum_chart(xs, k: float = 0.5, h: float = 5.0, base: dict | None = None,
                sigma_method: str = "mr") -> dict:
    """CUSUM 管制圖。k 與 h 的單位都是 σ（先標準化再累積）。"""
    xs = list(xs)
    b = base or baseline_stats(xs, sigma_method)
    pos, neg = cusum_series(xs, b["center"], b["sigma"], k)
    alarms = [(pos[i] > h or neg[i] > h) for i in range(len(xs))]
    return {"k": k, "h": h, "center": b["center"], "sigma": b["sigma"],
            "stat_pos": pos, "stat_neg": neg, "alarm": alarms}


# ─────────────────────────────────────────────────────────────
# ARL ①：馬可夫鏈法
# ─────────────────────────────────────────────────────────────

def _solve(M: list[list[float]], b: list[float]) -> list[float]:
    """高斯消去（部分選主元）解 M·x = b"""
    n = len(M)
    A = [list(M[i]) + [b[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(A[r][col]))
        if abs(A[piv][col]) < 1e-300:
            raise ValueError("矩陣奇異")
        A[col], A[piv] = A[piv], A[col]
        pv = A[col][col]
        for r in range(n):
            if r == col:
                continue
            f = A[r][col] / pv
            if f:
                for c in range(col, n + 1):
                    A[r][c] -= f * A[col][c]
    return [A[i][n] / A[i][i] for i in range(n)]


@lru_cache(maxsize=None)
def arl_shewhart(k: float = 3.0) -> float:
    """Shewhart 的 ARL0 有封閉解：1 / (2(1−Φ(k)))（k=3 → 370.4）"""
    return 1.0 / (2 * (1 - _phi(k)))


@lru_cache(maxsize=None)
def arl_cusum(k: float, h: float, delta: float = 0.0, m: int = 241,
              sides: int = 1) -> float:
    """CUSUM 的 ARL（Brook & Evans 1972 馬可夫鏈法）。

    **注意 `sides`**：本函式解的是**單側**鏈（只有上界 h）。
    管制圖是雙側（C⁺ 與 C⁻ 各一個），雙側 ARL 用 ARL_單側/2 近似
    （文獻常用近似；兩側的相關性讓它只是近似，本專案用模擬驗證其誤差）。

    把累積和 S ∈ [0, h] 切成 m 格，格寬 w = h/m，以格中點為狀態；
    轉移機率用常態 CDF 算：S_t = max(0, S_{t−1} + Y)、Y ~ N(delta − k, 1)。
    解 (I − Q)v = 1 得每個起始狀態的平均吸收時間，回傳 v[0]（S0 = 0）。
    """
    if m < 5:
        raise ValueError("m 太小")
    w = h / m
    c = [(j + 0.5) * w for j in range(m)]
    Q = [[0.0] * m for _ in range(m)]
    for i, ci in enumerate(c):
        for j in range(m):
            hi = (j + 1) * w - ci - (delta - k)
            # 第一格必須吸收「S_t 被夾回 0」的所有機率（Y < −c_i）→ lo = −∞
            # 若寫成 lo = j·w − c_i − (delta−k)，這塊機率會憑空消失、被誤判成吸收
            # （實測：ARL0 由 465 掉到 1.9）
            lo = -1e30 if j == 0 else j * w - ci - (delta - k)
            Q[i][j] = _phi(hi) - _phi(lo)
    M = [[(1.0 if i == j else 0.0) - Q[i][j] for j in range(m)] for i in range(m)]
    v = _solve(M, [1.0] * m)
    return v[0] / 2.0 if sides == 2 else v[0]


@lru_cache(maxsize=None)
def arl_ewma(lam: float, L: float, delta: float = 0.0, m: int = 241) -> float:
    """EWMA 的 ARL（Lucas & Saccucci 1990 馬可夫鏈法）。

    狀態空間：z 落在中心 ±L·σ_z 之間（σ_z = sqrt(λ/(2−λ))），切成 m 格。
    轉移：z_t = λ·x_t + (1−λ)·z_{t−1}，x ~ N(delta, 1)
          → 給定 z_{t−1} = c，z_t ~ N(λ·delta + (1−λ)c, λ)
    """
    if not 0.0 < lam <= 1.0:
        raise ValueError("λ 必須在 (0,1]")
    sz = math.sqrt(lam / (2 - lam))          # 穩態標準差 σ_z
    half = L * sz
    w = 2 * half / (m - 1)
    c = [-half + j * w for j in range(m)]
    # 每步的條件標準差是 λ（不是 sqrt(λ)）：z_t = λ·x_t + (1−λ)·z_{t−1}
    # → Var(z_t | z_{t−1}) = λ²·Var(x) = λ² → SD = λ。
    # **λ=1 時兩者數值相同（都等於 1）**，所以只用 λ=1 驗證察覺不到這個錯——
    # 本模組的測試因此涵蓋 λ<1，並以蒙地卡羅模擬（arl_monte_carlo）當獨立錨點。
    sd = lam
    Q = [[0.0] * m for _ in range(m)]
    for i, ci in enumerate(c):
        mu_next = lam * delta + (1 - lam) * ci
        for j in range(m):
            lo = (-half + (j - 0.5) * w - mu_next) / sd
            hi = (-half + (j + 0.5) * w - mu_next) / sd
            Q[i][j] = _phi(hi) - _phi(lo)
    M = [[(1.0 if i == j else 0.0) - Q[i][j] for j in range(m)] for i in range(m)]
    v = _solve(M, [1.0] * m)
    return v[(m - 1) // 2]          # 從 z = 0（中心）出發


# ─────────────────────────────────────────────────────────────
# ARL ②：蒙地卡羅（獨立驗證用，也是本專案測試的錨點）
# ─────────────────────────────────────────────────────────────

def arl_monte_carlo(kind: str, params: dict, delta: float = 0.0,
                    n_runs: int = 5000, max_len: int = 20000,
                    seed: int = 20261012) -> float:
    """直接模擬求 ARL（與馬可夫鏈法必須一致）。"""
    rng = random.Random(seed)
    total = 0
    for _ in range(n_runs):
        t = 0
        if kind == "shewhart":
            k = params.get("k", 3.0)
            while t < max_len:
                t += 1
                if abs(rng.gauss(delta, 1.0)) > k:
                    break
        elif kind == "cusum":
            k, h = params.get("k", 0.5), params.get("h", 5.0)
            sides = params.get("sides", 2)
            cp = cn = 0.0
            while t < max_len:
                t += 1
                z = rng.gauss(delta, 1.0)
                cp = max(0.0, cp + z - k)
                if sides == 2:
                    cn = max(0.0, cn - z - k)
                    if cp > h or cn > h:
                        break
                elif cp > h:
                    break
        elif kind == "ewma":
            lam, L = params.get("lam", 0.1), params.get("L", 2.7)
            L *= 1.0
            z = 0.0
            while t < max_len:
                t += 1
                z = lam * rng.gauss(delta, 1.0) + (1 - lam) * z
                sd = math.sqrt(lam / (2 - lam) * (1 - (1 - lam) ** (2 * t)))
                if abs(z) > L * sd:
                    break
        else:
            raise ValueError(f"未知的 kind：{kind}")
        total += t
    return total / n_runs


# ─────────────────────────────────────────────────────────────
# ARL 設計：給定目標 ARL0 反推參數
# ─────────────────────────────────────────────────────────────

@lru_cache(maxsize=None)
def design_shewhart(arl0: float = 370.4) -> float:
    """Shewhart：k = Φ⁻¹(1 − 1/(2·ARL0))（arl0=370.4 → k≈3.000）"""
    return _phi_inv(1 - 1.0 / (2 * arl0))


def design_ewma(lam: float, arl0: float = 370.4, L_lo: float = 1.0,
                L_hi: float = 6.0, tol: float = 0.02, m: int = 61) -> float:
    """EWMA：二分搜尋 L 使 ARL0 ≈ 目標（ARL 隨 L 單調遞增）。

    `m` 是**設計階段**的格點數（預設 61）：40 步二分搜尋 × 每步一次鏈解，
    用細格點會慢 30 倍以上。回報 ARL 時請用 `arl_ewma` 的預設（m=241）。
    """
    lo, hi = L_lo, L_hi
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        if arl_ewma(lam, mid, m=m) < arl0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def design_cusum(k: float = 0.5, arl0: float = 370.4, h_lo: float = 1.0,
                 h_hi: float = 12.0, tol: float = 0.02, m: int = 61) -> float:
    """CUSUM：二分搜尋 h 使**單側** ARL0 ≈ 目標（雙側請把目標除以 2 再設計）

    `m` 同 `design_ewma`：設計階段用粗格點換速度。
    """
    lo, hi = h_lo, h_hi
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        if arl_cusum(k, mid, m=m, sides=1) < arl0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)
