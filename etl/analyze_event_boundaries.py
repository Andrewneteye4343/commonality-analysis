#!/usr/bin/env python3
"""M2 前置：事件切分（event segmentation）的實證分析。

為什麼要做這一步：
    commonality analysis 的分母是「這片 wafer 在這一站經過哪台機台」，
    所以必須先把連續的感測器列切成「一次加工事件」。
    但「什麼算一次事件」不能憑想像——要用資料決定（時間間隔分布、鍵的變化、runnum 行為）。

用法：
    python analyze_event_boundaries.py --file /data/raw/03_M01_DC_score.csv

輸出（都會寫進 reports/m2/event_boundaries.json）：
    1. 取樣間隔分布（4 秒？有沒有缺漏）
    2. 以不同 gap 門檻切事件，事件數與每事件列數分布
    3. 鍵的組合行為：runnum 是單調計數器還是每次重置？stage 與 recipe_step 的關係
    4. 建議的事件鍵與 gap 門檻（附理由）
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
from collections import Counter, defaultdict
from pathlib import Path

KEY_CANDIDATES = {
    "tool_lot_runnum_recipe_step": ("Tool", "Lot", "runnum", "recipe", "recipe_step"),
    "tool_lot_stage_recipe_step": ("Tool", "Lot", "stage", "recipe", "recipe_step"),
    "tool_lot_recipe_step": ("Tool", "Lot", "recipe", "recipe_step"),
}
GAPS = [8, 12, 20, 60, 300]          # 秒（或原始時間單位）門檻


def quantiles(xs: list[int]) -> dict:
    if not xs:
        return {}
    xs = sorted(xs)
    def q(p):
        return xs[min(len(xs) - 1, int(p * len(xs)))]
    return {"n": len(xs), "min": xs[0], "p25": q(.25), "median": q(.5),
            "p75": q(.75), "p90": q(.90), "p99": q(.99), "max": xs[-1]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--out", default="reports/m2/event_boundaries.json")
    args = ap.parse_args()

    path = Path(args.file)
    delta_counter: Counter = Counter()          # 取樣間隔分布（秒，取整）
    runnum_seq: dict[str, list] = defaultdict(list)   # Lot -> [(t, runnum)]
    per_lot_rows: Counter = Counter()
    stage_of_step: dict[str, Counter] = defaultdict(Counter)
    step_of_stage: dict[str, Counter] = defaultdict(Counter)
    # 事件切分：正確做法是「先按鍵分組，再依時間缺口切段」
    # （不能用連續性判斷：同一 lot 的資料列可能與其他 lot 交錯，會把一次加工誤切成多段）
    seg_rows: dict[str, dict[int, list[int]]] = {k: {g: [] for g in GAPS} for k in KEY_CANDIDATES}
    last_state: dict[tuple[str, int], dict] = {}   # (key_name, gap) -> {group_key: [last_t, count]}
    n_rows = 0
    t_prev_global = None
    lot_changes = 0
    lot_prev = None

    with path.open(newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        idx = {c: i for i, c in enumerate(header)}

        for row in reader:
            if len(row) < len(header):
                continue
            n_rows += 1
            t = int(row[idx["time"]])
            if t_prev_global is not None:
                d = t - t_prev_global
                delta_counter[d if d <= 60 else 61] += 1     # >60 一律歸為 61 桶
            t_prev_global = t
            lot = row[idx["Lot"]]
            if lot_prev is not None and lot != lot_prev:
                lot_changes += 1
            lot_prev = lot
            per_lot_rows[lot] += 1
            runnum_seq[lot].append((t, row[idx["runnum"]]))
            st = row[idx["stage"]]
            rs = row[idx["recipe_step"]]
            stage_of_step[rs][st] += 1
            step_of_stage[st][rs] += 1

            for name, cols in KEY_CANDIDATES.items():
                gk = tuple(row[idx[c]] for c in cols)
                for g in GAPS:
                    table = last_state.setdefault((name, g), {})
                    s = table.get(gk)
                    if s is None:
                        table[gk] = [t, 1]
                    elif (t - s[0]) > g:
                        seg_rows[name][g].append(s[1])
                        table[gk] = [t, 1]
                    else:
                        s[0] = t
                        s[1] += 1

    # 收尾最後一段
    for (name, g), table in last_state.items():
        for gk, s in table.items():
            seg_rows[name][g].append(s[1])

    # runnum 行為：在每個 Lot 內，runnum 是否遞增
    runnum_behaviour = {"lots": 0, "increasing": 0, "decreasing": 0, "reset_or_mixed": 0, "single_value": 0}
    runnum_changes = []
    for lot, seq in runnum_seq.items():
        runnum_behaviour["lots"] += 1
        vals = [int(v[1]) for v in seq]
        uniq = len(set(vals))
        if uniq == 1:
            runnum_behaviour["single_value"] += 1
            continue
        inc = sum(1 for a, b in zip(vals, vals[1:]) if b > a)
        dec = sum(1 for a, b in zip(vals, vals[1:]) if b < a)
        runnum_changes.append(uniq)
        if dec == 0 and inc > 0:
            runnum_behaviour["increasing"] += 1
        elif inc == 0 and dec > 0:
            runnum_behaviour["decreasing"] += 1
        else:
            runnum_behaviour["reset_or_mixed"] += 1

    # stage 與 recipe_step 的多對多程度
    step_per_stage = {s: len(c) for s, c in step_of_stage.items()}
    stage_per_step = {r: len(c) for r, c in stage_of_step.items()}

    delta_top = delta_counter.most_common(8)
    out = {
        "file": path.name,
        "n_rows": n_rows,
        "sampling_delta_top": delta_top,
        "sampling_delta_note": "鍵值 61 代表「> 60 秒」的所有間隔（缺口）",
        "lot_interleaving": {
            "n_rows": n_rows,
            "lot_changes_between_consecutive_rows": lot_changes,
            "note": "若次數接近 n_rows，代表不同 lot 的資料列在檔案中交錯排列",
        },
        "per_lot_rows": {"n_lots": len(per_lot_rows), **quantiles(list(per_lot_rows.values()))},
        "segmentation": {
            k: {str(g): quantiles(v[g]) for g in GAPS} for k, v in seg_rows.items()
        },
        "runnum_behaviour": runnum_behaviour,
        "runnum_values_per_lot": quantiles(runnum_changes) if runnum_changes else {},
        "stage_recipe_step_mapping": {
            "n_stages": len(step_of_stage),
            "n_recipe_steps": len(stage_of_step),
            "recipe_steps_per_stage_median": statistics.median(step_per_stage.values()) if step_per_stage else None,
            "stages_per_recipe_step_median": statistics.median(stage_per_step.values()) if stage_per_step else None,
        },
    }

    outp = Path(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 78)
    print(f"事件切分實證分析：{path.name}（{n_rows:,} 列）")
    print("=" * 78)
    print("\n① 取樣間隔分布（前 8 名，單位＝原始 time 欄）")
    for d, c in delta_top:
        label = "> 60（缺口）" if d == 61 else f"{d}"
        print(f"    {label:>14} : {c:>10,}")
    print(f"\n② 每個 Lot（官方定義為 wafer id）的列數分布")
    q = out["per_lot_rows"]
    print(f"    lot 數 {q['n_lots']}｜min {q['min']}｜median {q['median']}｜p90 {q['p90']}｜max {q['max']}")
    li = out["lot_interleaving"]
    print(f"    相鄰列換 lot 的次數：{li['lot_changes_between_consecutive_rows']:,} / {li['n_rows']:,}"
          f"（{'資料列交錯排列' if li['lot_changes_between_consecutive_rows'] > li['n_rows'] * 0.3 else '大致連續'}）")
    print(f"\n③ runnum 行為（在每個 Lot 內是否遞增）")
    rb = out["runnum_behaviour"]
    print(f"    {rb}")
    if out["runnum_values_per_lot"]:
        print(f"    每個 lot 的 runnum 唯一值數：{out['runnum_values_per_lot']}")
    print(f"\n④ stage 與 recipe_step 的對應關係")
    m = out["stage_recipe_step_mapping"]
    print(f"    stage 數 {m['n_stages']}｜recipe_step 數 {m['n_recipe_steps']}")
    print(f"    每個 stage 對應的 recipe_step 數（中位數）：{m['recipe_steps_per_stage_median']}")
    print(f"    每個 recipe_step 對應的 stage 數（中位數）：{m['stages_per_recipe_step_median']}")
    print(f"\n⑤ 事件切分：候選鍵 × gap 門檻（事件數 / 每事件列數中位數 / 最大）")
    print(f"    {'候選鍵':<32}{'gap':>5}{'事件數':>10}{'median':>8}{'p90':>7}{'max':>8}")
    for k, by_gap in out["segmentation"].items():
        for g, st in by_gap.items():
            if st.get("n"):
                print(f"    {k:<32}{g:>5}{st['n']:>10,}{st['median']:>8}{st['p90']:>7}{st['max']:>8}")
            else:
                print(f"    {k:<32}{g:>5}{'—':>10}")
    print(f"\n已寫入 {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
