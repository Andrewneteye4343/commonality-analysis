"""建構子共用：維度鍵值正規化（D 項）

問題（實測）：PHM 各機台檔案的 `recipe_step` 寫法不一致 —— 03M01 是整數字串 `1`，
02M02 是浮點字串 `1.0`。同一個製程步驟被拆成兩個群組鍵（`.../1/...` 與 `.../1.0/...`），
維度表與 lot_membership 也會出現重複鍵。

規則（保守，不猜）：
  * 去前後空白
  * `1.0` / `2.00` / `10.000` → `1` / `2` / `10`（只在小數部分全為 0 時才去掉）
  * `1.5`（真的有非零小數）→ 原樣保留（不擅自取整）
  * 空值 → 空字串（呼叫端自行決定要不要擋）
"""
from __future__ import annotations

import re

_TRAILING_ZERO_DECIMAL = re.compile(r"^([+-]?\d+)\.0+$")


def norm_key(value) -> str:
    """把維度鍵值正規化成同一種寫法（recipe、recipe_step 共用）。"""
    if value is None:
        return ""
    s = str(value).strip()
    m = _TRAILING_ZERO_DECIMAL.match(s)
    return m.group(1) if m else s
