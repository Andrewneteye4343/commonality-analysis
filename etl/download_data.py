#!/usr/bin/env python3
"""M1：資料下載 + 完整性驗證（可續傳、可重跑、產生 MANIFEST.json）。

用法（容器內）：
    python download_data.py                  # 下載 M1 需要的全部資料
    python download_data.py --only secom     # 只下 SECOM
    python download_data.py --skip-phm       # 跳過 385 MB 的 PHM 感測檔
    python download_data.py --tools 03,04    # 指定 PHM 機台編號（也可寫 --tools 3 4；會自動補零）
    python download_data.py --force          # 重新下載（忽略既有檔案）

設計原則：
  * 只用標準函式庫 → image 不需額外套件，下載層不會因套件問題壞掉
  * 檔案已存在且大小相符就跳過（idempotent），支援中斷續傳（HTTP Range）
  * 每個檔案都算 SHA256 寫進 MANIFEST.json → 資料可追溯、可驗證沒被換過
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from script_version import add_version_arg

# ── 資料來源（2026-10 實測可下載，免登入）────────────────────────────
DASHLINK = "https://c3.ndc.nasa.gov/dashlink/static/media/dataset"
SECOM_URL = "https://archive.ics.uci.edu/static/public/179/secom.zip"

# PHM 2018 檔名對應（由 DASHlink 資源頁實測取得）
PHM_TOOL_MAP = {"01": "M02", "02": "M02", "03": "M01", "04": "M01", "06": "M01"}

UA = "commonality-analysis/0.1 (academic use; contact: repository owner)"
CHUNK = 1 << 20  # 1 MiB


def parse_tools(raw) -> list[str]:
    """把使用者給的機台清單正規化成 ['01', '02', ...]

    為什麼需要：**PowerShell 會把 `01,02,04,06` 當成數字陣列**，
    傳進程式時變成 `1,2,4,6`（前導零消失）→ 直接比對機台對照表就會失敗。
    因此這裡一律零填充到兩位數，並容忍逗號／空白／分號等各種分隔方式。
    同時容忍 PowerShell 把清單拆成多個參數（"--tools 01 02 04"）。
    """
    import re
    if isinstance(raw, (list, tuple)):
        raw = " ".join(str(x) for x in raw)
    parts = [t for t in re.split(r"[,\s;]+", str(raw).strip()) if t]
    out = []
    for t in parts:
        t = t.strip()
        if t.isdigit():
            t = t.zfill(2)
        out.append(t)
    return out


def phm_files(tool: str) -> dict[str, tuple[str, str]]:
    """回傳 {kind: (檔名, 說明)}"""
    m = PHM_TOOL_MAP[tool]
    base = f"{tool}_{m}_DC"
    return {
        "sensor": (f"{base}_score.csv", "感測器時序（24 欄，含 Tool/Lot/recipe）"),
        "target": (f"{base}_test.csv", "TTF 預測目標格式（time + 3 TTF）"),
        "truth": (f"{base}_groundtruth.csv", "TTF 真值（供官方評分）"),
    }


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def sha256_of(path: Path, progress: bool = True) -> str:
    h = hashlib.sha256()
    done = 0
    with path.open("rb") as fh:
        while True:
            b = fh.read(CHUNK)
            if not b:
                break
            h.update(b)
            done += len(b)
            if progress and done % (64 * CHUNK) < CHUNK:
                print(f"      hash {human(done)} …", flush=True)
    return h.hexdigest()


def remote_size(url: str) -> int | None:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            cl = r.headers.get("Content-Length")
            return int(cl) if cl else None
    except Exception:
        return None


def download(url: str, dest: Path, force: bool = False) -> dict:
    """下載單一檔案；支援續傳。回傳 metadata。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    exp = remote_size(url)
    if dest.exists() and not force:
        if exp is None or dest.stat().st_size == exp:
            print(f"  [skip] 已存在且大小相符：{dest.name}（{human(dest.stat().st_size)}）")
            return {"path": str(dest), "bytes": dest.stat().st_size, "skipped": True}
        print(f"  [resume] 大小不符（本機 {dest.stat().st_size} / 遠端 {exp}）→ 續傳")

    start = dest.stat().st_size if dest.exists() and not force else 0
    headers = {"User-Agent": UA}
    if start:
        headers["Range"] = f"bytes={start}-"
    req = urllib.request.Request(url, headers=headers)
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        if start and r.status != 206:  # 伺服器不支援續傳 → 從頭來
            start = 0
        total = (start + int(r.headers.get("Content-Length", 0))) or exp or 0
        mode = "ab" if start else "wb"
        got = start
        last = 0
        with dest.open(mode) as fh:
            while True:
                b = r.read(CHUNK)
                if not b:
                    break
                fh.write(b)
                got += len(b)
                if got - last >= 25 * CHUNK:  # 每 25 MiB 回報一次
                    last = got
                    pct = f"{100 * got / total:5.1f}%" if total else "  ?  "
                    print(f"      {pct}  {human(got)}", flush=True)
    dt = time.time() - t0
    size = dest.stat().st_size
    print(f"  [done] {dest.name}：{human(size)}，耗時 {dt:.0f}s（{human(int(size / max(dt, 1e-6)))}/s）")
    return {"path": str(dest), "bytes": size, "skipped": False}


def main() -> int:
    ap = argparse.ArgumentParser(description="下載 commonality-analysis 所需資料")
    ap.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "data/raw"))
    ap.add_argument("--only", default="", help="只下載指定項目：secom, phm_sensor, phm_target, phm_truth")
    ap.add_argument("--skip-phm", action="store_true", help="跳過 PHM（只下 SECOM）")
    ap.add_argument("--tools", nargs="*", default=["03"],
                    help="PHM 機台編號，可用：01,02,03,04,06（逗號或空白分隔；會自動補前導零）")
    ap.add_argument("--force", action="store_true", help="重新下載")
    ap.add_argument("--no-hash", action="store_true", help="跳過 SHA256（僅快速測試用）")
    add_version_arg(ap)
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"資料目錄：{data_dir.resolve()}")

    # ── 建立待下載清單 ───────────────────────────────────────────
    todo: list[tuple[str, str, str]] = []  # (key, url, 說明)
    todo.append(("secom", SECOM_URL, "UCI SECOM：1567×590 製程感測 + pass/fail 標籤（1.9 MB）"))

    if not args.skip_phm:
        for tool in parse_tools(args.tools):
            if tool not in PHM_TOOL_MAP:
                print(f"[錯誤] 未知的機台編號 {tool}；可用：{sorted(PHM_TOOL_MAP)}", file=sys.stderr)
                return 2
            for kind, (fname, desc) in phm_files(tool).items():
                todo.append((f"phm_{kind}_{tool}", f"{DASHLINK}/{fname}", f"PHM 2018 機台 {tool}：{desc}"))

    if args.only:
        wanted = {k.strip() for k in args.only.split(",")}
        todo = [t for t in todo if t[0] in wanted or t[0].split("_")[-1] in wanted]
        if not todo:
            print(f"[錯誤] --only {args.only} 沒有對應任何項目", file=sys.stderr)
            return 2

    # ── 下載 ────────────────────────────────────────────────────
    records, failed = [], []
    for key, url, desc in todo:
        print(f"\n▶ {key}\n  {desc}\n  {url}")
        try:
            meta = download(url, data_dir / url.split("/")[-1], force=args.force)
        except urllib.error.HTTPError as e:
            print(f"  [失敗] HTTP {e.code}")
            failed.append((key, f"HTTP {e.code}"))
            continue
        except Exception as e:  # noqa: BLE001
            print(f"  [失敗] {type(e).__name__}: {e}")
            failed.append((key, str(e)))
            continue
        rec = {"key": key, "source_url": url, "description": desc, **meta,
               "downloaded_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")}
        if not args.no_hash:
            print("      計算 SHA256 …", flush=True)
            rec["sha256"] = sha256_of(Path(meta["path"]))
        records.append(rec)

    # ── 寫 MANIFEST ─────────────────────────────────────────────
    manifest_path = data_dir / "MANIFEST.json"
    old = []
    if manifest_path.exists():
        try:
            old = json.loads(manifest_path.read_text(encoding="utf-8")).get("files", [])
        except Exception:  # noqa: BLE001
            old = []
    by_key = {r["key"]: r for r in old}
    for r in records:
        by_key[r["key"]] = r
    manifest = {
        "project": "commonality-analysis",
        "milestone": "M1",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        # 記錄「執行時使用的相對/容器路徑」而非絕對路徑，避免把本機目錄結構寫進公開 repo
        "data_dir": str(data_dir),
        "files": sorted(by_key.values(), key=lambda r: r["key"]),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 總結 ────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("下載總結")
    print("=" * 78)
    total = 0
    for r in manifest["files"]:
        total += r.get("bytes", 0)
        flag = "跳過" if r.get("skipped") else "下載"
        print(f"  {r['key']:<18} {human(r.get('bytes', 0)):>10}  [{flag}]  sha256={str(r.get('sha256', '-'))[:16]}")
    print(f"  {'合計':<18} {human(total):>10}")
    print(f"\nMANIFEST：{manifest_path}")
    if failed:
        print("\n[失敗項目]")
        for k, e in failed:
            print(f"  {k}: {e}")
        return 1
    print("\n所有檔案下載完成且已記錄 checksum。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
