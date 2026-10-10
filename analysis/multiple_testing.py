"""M3：多重比較校正（Bonferroni / Benjamini–Hochberg FDR）。

為什麼一定要做
────────────────────────────────────────────────────────────
共同性分析一次會測「機台 × recipe × 步驟 × stage」等**上千個群組**。
在 α = 0.05 下，就算完全沒有根因，也會有約 5% 的群組「顯著」——測 1000 個就冒出 50 個假陽性。
不做校正的排名在面試裡站不住（也真的會害製程工程師白查一批機台）。

兩種校正的取捨
────────────────────────────────────────────────────────────
- **Bonferroni**：控制 family-wise error rate（FWER），`p_adj = min(1, p·m)`。
  極保守，適合「不容許任何一次誤報」的情境，但在上千個檢定下幾乎不會有任何結果存活。
- **Benjamini–Hochberg（BH）**：控制 false discovery rate（FDR），也就是
  「被判定顯著的群組中，預期有多少比例是假的」。共同性分析是**篩選（screening）**
  工作（先縮小範圍再交給工程師查），因此 BH 是更合適的預設。
  這也是 scRNA-seq 差異表現分析（你熟悉的場景）的標準做法。

BH 的步驟（step-up）
────────────────────────────────────────────────────────────
1. p 值由小到大排序：p(1) ≤ … ≤ p(m)
2. q(i) = p(i) · m / i
3. 取累積最小值（由大到小修正，確保 q 單調不遞減）：q(i) = min(q(i), q(i+1))
"""
from __future__ import annotations

from dataclasses import dataclass


def bonferroni(pvals: list[float], m: int | None = None) -> list[float]:
    """Bonferroni 校正（FWER）"""
    if not pvals:
        return []
    m = m or len(pvals)
    return [min(1.0, p * m) for p in pvals]


def benjamini_hochberg(pvals: list[float], m: int | None = None) -> list[float]:
    """BH step-up，回傳 q 值（順序與輸入一致）"""
    n = len(pvals)
    if n == 0:
        return []
    m_eff = m or n
    order = sorted(range(n), key=lambda i: pvals[i])
    q = [1.0] * n
    prev = 1.0
    # 由大到小走，取累積最小值以保證單調
    for rank in range(n, 0, -1):
        idx = order[rank - 1]
        val = pvals[idx] * m_eff / rank
        prev = min(prev, val)
        q[idx] = min(1.0, prev)
    return q


@dataclass
class FdrSummary:
    n_tests: int
    n_sig_raw: int          # 未校正 p < alpha 的個數
    n_sig_bonferroni: int
    n_sig_bh: int
    alpha: float

    @property
    def expected_false_positives_raw(self) -> float:
        """未校正時「純屬巧合」的預期顯著個數 = m × α"""
        return self.n_tests * self.alpha

    def as_dict(self) -> dict:
        return {
            "n_tests": self.n_tests,
            "n_sig_raw": self.n_sig_raw,
            "n_sig_bonferroni": self.n_sig_bonferroni,
            "n_sig_bh": self.n_sig_bh,
            "alpha": self.alpha,
            "expected_false_positives_raw": round(self.expected_false_positives_raw, 2),
            "note": ("未校正的顯著個數若接近 m×α，代表多半是巧合；"
                     "BH 校正後的個數才是可交付的候選清單"),
        }


def evaluate(pvals: list[float], alpha: float = 0.05) -> tuple[list[float], list[float], FdrSummary]:
    """回傳 (q_bh, p_bonferroni, summary)"""
    q = benjamini_hochberg(pvals)
    pb = bonferroni(pvals)
    s = FdrSummary(
        n_tests=len(pvals),
        n_sig_raw=sum(1 for p in pvals if p < alpha),
        n_sig_bonferroni=sum(1 for p in pb if p < alpha),
        n_sig_bh=sum(1 for p in q if p < alpha),
        alpha=alpha,
    )
    return q, pb, s
