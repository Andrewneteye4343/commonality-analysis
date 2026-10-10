"""腳本版本戳記：讓「使用者手上跑的是哪一版」變成可驗證的事實。

為什麼需要（實際踩過）
────────────────────────────────────────────────────────────
修好的檔案用 zip 交付、由使用者解壓覆蓋——但**沒有任何方法確認他手上是不是最新版**。
實際發生過：交付了新版的 `--reset`，使用者跑到舊檔（`unrecognized arguments: --reset`），
雙方都在猜「是程式錯還是檔案沒更新」。

做法：所有會被交付的腳本都支援 `--version`，交付指示第一步一律是
    docker compose run --rm etl python <script> --version
輸出與 `VERSION` 檔一致才繼續；不一致就是「檔案沒更新」，不是程式問題。

改版規則：**任何交付出去的腳本內容有變動，就把 VERSION 往上加一階**（例如 r3 → r4）。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

# 單一來源：與專案根目錄的 VERSION 檔同步（找不到檔案時用這個常數）
VERSION = "2026-10-10.r8"


def current_version() -> str:
    for cand in (Path(__file__).resolve().parent.parent / "VERSION",
                 Path("/work/VERSION"),
                 Path(os.environ.get("VERSION_FILE", "/nonexistent"))):
        try:
            if cand.is_file():
                v = cand.read_text(encoding="utf-8").strip()
                if v:
                    return v
        except OSError:
            continue
    return VERSION


def add_version_arg(ap: argparse.ArgumentParser) -> None:
    """掛上 --version（argparse 內建 action，印出後 exit 0）"""
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s version {current_version()}")
