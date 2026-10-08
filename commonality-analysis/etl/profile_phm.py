#!/usr/bin/env python3
"""M1：PHM 2018 感測檔剖析（串流讀取，避免 385 MB 檔撐爆記憶體）。

用法：
    python profile_phm.py --file /data/raw/03_M01_DC_score.csv
    python profile_phm.py --file ... --max-rows 200000      # 快速抽查
    python profile_phm.py --all                              # 剖析目錄內所有 *_DC_score.csv

輸出：欄位清單、列數、Tool/Lot/recipe 等類別欄位的唯一值數量、時間範圍、
      每個感測欄的 min/max/mean/缺失率，以及「每個 (Tool, Lot, recipe, recipe_step) 事件的筆數統計」
      —— 這些就是 M2 建立 genealogy 事件表的基礎。
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import statistics
from collections import Counter, defaultdict
from pathlib import Path

CAT_COLS = ["Tool", "stage", "Lot", "recipe", "recipe_step"]
ID_COLS = ["runnum"]           # 數值識別碼，不是感測器 → 不列入感測統計
NON_SENSOR = ["time", *CAT_COLS, *ID_COLS]


def profile(path: Path, max_rows: int | None = None) -> dict:
    with path.open(newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        idx = {name: i for i, name in enumerate(header)}
        sensor_cols = [c for c in header if c not in NON_SENSOR]
        ids: dict[str, set] = {c: set() for c in ID_COLS if c in idx}

        n = 0
        cats: dict[str, Counter] = {c: Counter() for c in CAT_COLS if c in idx}
        t_min = t_max = None
        stats: dict[str, list] = {c: [0, None, None, 0.0, 0] for c in sensor_cols}  # cnt,min,max,sum,nan
        events: Counter = Counter()
        rows_per_event: Counter = Counter()

        for row in reader:
            if len(row) < len(header):
                continue
            n += 1
            if max_rows and n > max_rows:
                n -= 1
                break
            t = row[idx["time"]]
            if t:
                tv = float(t)
                t_min = tv if t_min is None else min(t_min, tv)
                t_max = tv if t_max is None else max(t_max, tv)
            for c in cats:
                cats[c][row[idx[c]]] += 1
            for c in ids:
                ids[c].add(row[idx[c]])
            key = tuple(row[idx[c]] for c in ("Tool", "Lot", "runnum", "recipe", "recipe_step") if c in idx)
            events[key] += 1
            for c in sensor_cols:
                v = row[idx[c]]
                if v == "":
                    stats[c][4] += 1
                    continue
                try:
                    f = float(v)
                except ValueError:
                    stats[c][4] += 1
                    continue
                s = stats[c]
                s[0] += 1
                s[1] = f if s[1] is None else min(s[1], f)
                s[2] = f if s[2] is None else max(s[2], f)
                s[3] += f

    sensor_summary = []
    for c, (cnt, mn, mx, ssum, nan) in stats.items():
        sensor_summary.append({
            "sensor": c, "n": cnt, "n_missing": nan,
            "missing_rate": round(nan / n, 4) if n else None,
            "min": mn, "max": mx, "mean": (ssum / cnt) if cnt else None,
        })

    return {
        "file": str(path), "bytes": path.stat().st_size,
        "columns": header, "n_cols": len(header), "n_rows": n,
        "time_min": t_min, "time_max": t_max,
        "series_hours": ((t_max - t_min) / 3600) if (t_min is not None and t_max is not None) else None,
        "distinct": {c: len(v) for c, v in cats.items()},
        "id_distinct": {c: len(v) for c, v in ids.items()},
        "top_values": {c: v.most_common(5) for c, v in cats.items()},
        "n_events": len(events),
        "event_rows_stats": {
            "min": min(events.values()) if events else None,
            "median": statistics.median(events.values()) if events else None,
            "max": max(events.values()) if events else None,
            "total": sum(events.values()),
        },
        "sensors": sensor_summary,
    }


def report(p: dict, show_sensors: int = 10) -> None:
    print("\n" + "=" * 78)
    print(f"檔案：{Path(p['file']).name}（{p['bytes'] / 2**20:.1f} MB）")
    print("=" * 78)
    print(f"  欄位數 {p['n_cols']}｜列數 {p['n_rows']:,}")
    print(f"  欄位：{', '.join(p['columns'])}")
    if p["time_min"] is not None:
        secs = p["time_max"] - p["time_min"]
        print(f"  時間範圍（原始 time 欄）：{p['time_min']:.0f} ～ {p['time_max']:.0f}"
              f"（跨度 {secs:,.0f} 單位 ≈ {secs/3600:.1f} 小時；取樣間隔約 4 秒）")
    print(f"  類別欄位唯一值：{p['distinct']}")
    if p.get("id_distinct"):
        print(f"  數值識別碼唯一值：{p['id_distinct']}")
    for c, top in p["top_values"].items():
        print(f"    {c}: {top}")
    ev = p["event_rows_stats"]
    print(f"  加工事件數（Tool×Lot×runnum×recipe×recipe_step）：{p['n_events']:,}")
    print(f"    每個事件的列數：min={ev['min']} median={ev['median']} max={ev['max']}（合計 {ev['total']:,}）")
    print(f"\n  感測欄位統計（依缺失率排序，前 {show_sensors} 個）：")
    print(f"    {'sensor':<28}{'missing':>9}{'min':>12}{'max':>12}{'mean':>12}")
    for s in sorted(p["sensors"], key=lambda x: -(x["missing_rate"] or 0))[:show_sensors]:
        mn = f"{s['min']:.4f}" if s["min"] is not None else "-"
        mx = f"{s['max']:.4f}" if s["max"] is not None else "-"
        me = f"{s['mean']:.4f}" if s["mean"] is not None else "-"
        print(f"    {s['sensor']:<28}{s['missing_rate']:>8.2%}{mn:>12}{mx:>12}{me:>12}")


def main() -> int:
    ap = argparse.ArgumentParser(description="PHM 2018 感測檔剖析（M1 驗收用）")
    ap.add_argument("--file")
    ap.add_argument("--all", action="store_true", help="剖析 DATA_DIR 內所有 *_DC_score.csv")
    ap.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "data/raw"))
    ap.add_argument("--max-rows", type=int, default=None)
    args = ap.parse_args()

    files: list[Path] = []
    if args.file:
        files = [Path(args.file)]
    if args.all:
        files += [Path(f) for f in sorted(glob.glob(str(Path(args.data_dir) / "*_DC_score.csv")))]
    if not files:
        print("[錯誤] 請指定 --file 或 --all", flush=True)
        return 2

    for f in files:
        if not f.exists():
            print(f"[錯誤] 找不到 {f}")
            return 2
        report(profile(f, args.max_rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
