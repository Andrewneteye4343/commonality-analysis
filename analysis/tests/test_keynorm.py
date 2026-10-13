"""維度鍵值正規化（etl/keynorm.py）的單元測試。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "etl"))

from keynorm import norm_key  # noqa: E402


def test_strips_trailing_zero_decimals():
    assert norm_key("1.0") == "1"
    assert norm_key("2.00") == "2"
    assert norm_key("10.000") == "10"
    assert norm_key("-3.0") == "-3"


def test_keeps_real_decimals_and_plain_values():
    assert norm_key("1") == "1"
    assert norm_key("1.5") == "1.5"          # 真的有非零小數 → 不擅自取整
    assert norm_key("215") == "215"


def test_trims_whitespace_and_handles_empty():
    assert norm_key("  7.0 ") == "7"
    assert norm_key("") == ""
    assert norm_key(None) == ""


def test_real_world_case_from_phm():
    """實測：03M01 是 '1'、02M02 是 '1.0' → 正規化後必須相同（否則分組被打散）"""
    assert norm_key("1") == norm_key("1.0") == "1"
