"""M5-3-0 前置診斷：trace 對齊與 tool matching 的可行性。

為什麼要先診斷
────────────────────────────────────────────────────────────
M5-1／M5-2 監控的是「事件層聚合特徵」——一個事件（4～184 個原始點）被壓成 5 個數。
同樣的均值可以有完全不同的 trace 形狀，這層資訊目前完全沒用到。
但對齊是否真能帶來可用資訊，必須先用資料回答，而不是先做再說。

回答四件事
────────────────────────────────────────────────────────────
  A 形狀：每個（機台 × 步驟）的事件長度與取樣間隔分布（只用事件表，成本極低）
  B 成本：要讀幾列 trace → **先估後擋**（超過 --max-rows 就拒絕，除非 --force）
  C 對齊方法：時間正規化重採樣 vs DTW，以「組間／組內距離比」客觀比較
     判準：同機台同步驟的事件應彼此更接近 → 比值 > 1 才代表 trace 帶有機台資訊
  D 值不值得：兩種對齊方式的比值對照（值高者即為 M5-3-1 要採用的方法）

用法
    docker compose run --rm etl python analysis/trace_precheck.py --tools "03"
    python analysis/trace_precheck.py --trace-csv <檔> --no-db      # 助理端驗證
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent / "etl", _HERE.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np                                          # noqa: E402

import run_fdc                                              # noqa: E402
import script_version                                        # noqa: E402
from script_version import add_version_arg                   # noqa: E402

DEFAULT_SENSORS = ("FLOWCOOLPRESSURE", "IONGAUGEPRESSURE", "ETCHBEAMVOLTAGE",
                   "ETCHBEAMCURRENT", "ACTUALSTEPDURATION")


# ── 對齊工具（純函式，可單元測試）────────────────────────────────
def resample_to_length(ts: np.ndarray, ys: np.ndarray, length: int) -> np.ndarray:
    """依**相對時間**把 trace 重採樣成固定長度（線性插值）。

    用相對時間（0→1）而非原始時間：本專案的步驟長度差很多，不先時間正規化
    就等於在比「持續多久」而不是「形狀長什麼樣」。
    """
    ts = np.asarray(ts, dtype=float)
    ys = np.asarray(ys, dtype=float)
    m = np.isfinite(ts) & np.isfinite(ys)
    if m.sum() < 2:
        return np.full(length, np.nan)
    t, y = ts[m], ys[m]
    span = t[-1] - t[0]
    if span <= 0:
        return np.full(length, float(np.mean(y)))
    return np.interp(np.linspace(0.0, 1.0, length), (t - t[0]) / span, y)


def dtw_distance(a: np.ndarray, b: np.ndarray, band: int | None = None) -> float:
    """帶狀 DTW（Sakoe-Chiba band 限制計算量），回傳路徑**平均**距離以便跨長度比較。"""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if a.size == 0 or b.size == 0:
        return float("nan")
    n, m = a.size, b.size
    w = band if band is not None else max(int(0.1 * max(n, m)), 5)
    prev = np.full(m + 1, np.inf)
    prev[0] = 0.0
    for i in range(1, n + 1):
        cur = np.full(m + 1, np.inf)
        for j in range(max(1, i - w), min(m, i + w) + 1):
            cur[j] = abs(a[i - 1] - b[j - 1]) + min(prev[j], cur[j - 1], prev[j - 1])
        prev = cur
    return float(prev[m] / max(n, m))


def zshape(v: np.ndarray, norm: str = "zscore") -> np.ndarray | None:
    """序列前處理。

    norm="zscore"：置中、除以自身標準差 → **只留形狀**（丟掉水準與振幅）
    norm="none"  ：完全不動 → **保留水準與振幅**

    ⚠ 為什麼必須兩種都跑：像 ACTUALSTEPDURATION（步驟耗時）這種變數，
      「機台之間的差異」正好表現在**水準**上（同一 recipe 同一步驟，某台就是花比較久）。
      只用 zscore 會先把最有用的資訊丟掉，然後得到「沒有訊號」的錯誤結論。
    """
    v = np.asarray(v, dtype=float)
    if v.size < 3 or not np.isfinite(v).all():
        return None
    if norm == "none":
        return v
    s = float(v.std())
    return None if s == 0 else (v - v.mean()) / s


def pairwise_mean_dtw(seqs: list, cap: int = 40) -> float | None:
    """一組序列的平均兩兩 DTW 距離（序列數超過 cap 就只取前 cap 個，控制成本）。"""
    seqs = seqs[:cap]
    if len(seqs) < 2:
        return None
    ds = [dtw_distance(seqs[i], seqs[j])
          for i in range(len(seqs)) for j in range(i + 1, len(seqs))]
    ds = [d for d in ds if np.isfinite(d)]
    return float(np.mean(ds)) if ds else None


def between_within_ratio(within: list, between: list) -> float | None:
    """組間／組內距離比；>1 表示「不同機台」比「同機台不同事件」更不相似。"""
    w = [x for x in within if x is not None and np.isfinite(x)]
    b = [x for x in between if x is not None and np.isfinite(x)]
    if not w or not b:
        return None
    mw = float(np.mean(w))
    return float(np.mean(b) / mw) if mw > 0 else None


def to_mean_ratio(seqs_by_tool: dict, min_n: int = 3) -> tuple:
    """以「到組內平均指紋的距離」定義組內與組間（Fisher 型比值，O(n)）。

    為什麼要用這個（而不只是兩兩 DTW）
    ────────────────────────────────────────────────────────────
      ① 兩兩距離是 O(n²) 次 DTW → 事件數拉大時跑不動，而**檢定力**正是問題所在
         （CI 寬 ±0.4 → 只能說「沒有證據」，不能說「證明沒有」）。
      ② 「與黃金指紋的距離」本來就是業界 tool matching 的標準形式。
    定義：組內 = 事件到自己機台平均指紋的平均距離（等長序列，resample 後適用）；
          組間 = 兩機台平均指紋之間的距離。
    """
    means, within = {}, []
    length = None
    for tool, v in seqs_by_tool.items():
        arr = [x for x in v if x is not None and x.size]
        if len(arr) < min_n:
            continue
        try:
            M = np.vstack(arr)
        except ValueError:                     # 同一機台內長度就不一致 → 不適用
            return None, None, None
        if length is None:
            length = M.shape[1]
        elif M.shape[1] != length:
            # ⚠ 跨機台長度不一致（dtw 模式）→ 質心無法相減，必須回報不可用。
            #    先前只檢查單一機台內部，實際在此崩潰（(75,) vs (90,)）。
            return None, None, None
        mu = M.mean(axis=0)
        means[tool] = mu
        within.append(float(np.mean([np.linalg.norm(x - mu) for x in M])))
    if len(means) < 2 or not within:
        return None, None, None
    tools_here = sorted(means)
    between = [float(np.linalg.norm(means[a] - means[b]))
               for i, a in enumerate(tools_here) for b in tools_here[i + 1:]]
    mw = float(np.mean(within))
    return (float(np.mean(between)) / mw if mw > 0 else None), within, between


def identify_rate(M_a: np.ndarray, M_b: np.ndarray, mu_a, mu_b) -> float:
    """可辨識率（平衡正確率）：用「離哪個機台質心較近」分類事件，答對的比例。

    0.5＝完全沒有機台資訊；1.0＝完全可分。
    為什麼不能只看置換檢定的 p 值：樣本大時虛無分布極窄，任何微不足道的差異都會
    變成 p=0.005、數十個標準差（實際踩到：比值 0.613 卻報 +47.8σ）→
    「統計顯著」不等於「可辨識」，效應量必須獨立量測。

    ⚠ 必須用「每個事件到兩個質心的距離」做比較，不是比較兩組各自的組內散佈
      （先前寫成 d_other = ‖x_other − mu_other‖ → 變成比較兩個組內散佈，恆約 0.5）。
    """
    def acc(M, mu_own, mu_other):
        """留一法（leave-one-out）：自己的質心用「排除該事件」重算。

        ⚠ 不可用含自己的質心：事件天然離含自己的質心更近 → in-sample 偏誤。
          實測合成負控制（兩群同分布）得到 0.71 而不是 0.5（因為 20 維下質心被自己拉近）。
        """
        n = M.shape[0]
        mu_loo = (n * mu_own - M) / (n - 1) if n > 1 else mu_own
        d_own = np.linalg.norm(M - mu_loo, axis=1)
        d_other = np.linalg.norm(M - mu_other, axis=1)   # 別機台質心本來就不含該事件
        return float(np.mean(d_own < d_other))
    return 0.5 * (acc(M_a, mu_a, mu_b) + acc(M_b, mu_b, mu_a))


def identify_by_key(by_key: dict, min_n: int = 10) -> tuple:
    """跨集團、跨機台對的可辨識率。

    回傳 (清單, 使用的集團數, 需要但可用的集團數, 總集團數)。
    ⚠ 一定要回報「用了幾個集團」：dtw 模式的長度多半不一致 → 只在極少數集團上
      算得出來，卻印出一個很高的值（實測 0.902）→ 會誤導判讀。
    """
    out, used, eligible, total = [], 0, 0, 0
    for key, by_tool in by_key.items():
        total += 1
        mats, mus, length = {}, {}, None
        for t, v in by_tool.items():
            arr = [x for x in v if x is not None and x.size]
            if len(arr) < min_n:
                continue
            try:
                M = np.vstack(arr)
            except ValueError:
                continue
            if length is None:
                length = M.shape[1]
            if M.shape[1] != length:
                continue
            mats[t], mus[t] = M, M.mean(axis=0)
        tools_here = sorted(mats)
        if len(tools_here) < 2:
            continue
        eligible += 1
        before = len(out)
        for i, a in enumerate(tools_here):
            for b in tools_here[i + 1:]:
                out.append(identify_rate(mats[a], mats[b], mus[a], mus[b]))
        if len(out) > before:
            used += 1
    return out, used, eligible, total


def within_between(by_key: dict, dist_mode: str = "to_mean",
                   pairwise_cap: int = 40, min_n: int = 3) -> tuple:
    """跨集團彙整組內／組間距離清單。by_key = {(recipe, step): {tool: [序列]}}。"""
    within, between = [], []
    for key, by_tool in by_key.items():
        tools_here = [t for t, v in by_tool.items() if len(v) >= min_n]
        if len(tools_here) < 2:
            continue
        if dist_mode == "to_mean":
            _, w, b = to_mean_ratio({t: by_tool[t] for t in tools_here}, min_n)
            within.extend(w or [])
            between.extend(b or [])
        else:
            for t in tools_here:
                m = pairwise_mean_dtw(by_tool[t], pairwise_cap)
                if m is not None:
                    within.append(m)
            for i, a in enumerate(tools_here):
                for b_ in tools_here[i + 1:]:
                    ds = [dtw_distance(x, y) for x in by_tool[a][:6] for y in by_tool[b_][:6]]
                    ds = [d for d in ds if np.isfinite(d)]
                    if ds:
                        between.append(float(np.mean(ds)))
    return within, between


def permutation_null(by_key: dict, dist_mode: str = "to_mean", n_perm: int = 100,
                     pairwise_cap: int = 40, min_n: int = 3, seed: int = 7) -> dict:
    """置換檢定：在每個集團內隨機重排機台標籤（保持各機台事件數不變）。

    為什麼不能用「比值 > 1」當門檻
    ────────────────────────────────────────────────────────────
      高維時組內距離隨維度增長、質心間距隨樣本數縮小 → 比值在**沒有訊號**時
      也會系統性偏向某個不等於 1 的值（實測合成負控制為 0.28）。
      所以判準必須與「重排後自己的分布」比較，而不是與 1 比較。
    回傳虛無分布的 mean/sd/95 分位與 p 值（單尾：觀測 ≥ 虛無）。
    """
    obs_w, obs_b = within_between(by_key, dist_mode, pairwise_cap, min_n)
    obs = between_within_ratio(obs_w, obs_b)
    if obs is None:
        return {"observed": None, "null_mean": None, "null_q95": None, "p_value": None,
                "n_perm": 0, "effect": None}
    rng = np.random.default_rng(seed)
    nulls = []
    for _ in range(n_perm):
        perm: dict = {}
        for key, by_tool in by_key.items():
            items, labels = [], []
            for t, v in by_tool.items():
                items.extend(v)
                labels.extend([t] * len(v))
            order = rng.permutation(len(items))
            shuffled: dict = defaultdict(list)
            for j, lab in enumerate(labels):
                shuffled[lab].append(items[order[j]])
            perm[key] = shuffled
        w, b = within_between(perm, dist_mode, pairwise_cap, min_n)
        r = between_within_ratio(w, b)
        if r is not None:
            nulls.append(r)
    if not nulls:
        return {"observed": obs, "null_mean": None, "null_q95": None, "p_value": None,
                "n_perm": 0, "effect": None}
    nu = np.array(nulls)
    q95 = float(np.percentile(nu, 95))
    p = float((np.sum(nu >= obs) + 1) / (nu.size + 1))          # 單尾；最小解析度 1/(n+1)
    sd = float(nu.std())
    return {"observed": obs, "null_mean": float(nu.mean()), "null_sd": sd,
            "null_q95": q95, "p_value": p,
            "effect": (float(obs - nu.mean()) / sd) if sd > 0 else None,
            "n_perm": int(nu.size)}


def cost_gate(n_rows: int, max_rows: int, force: bool) -> tuple:
    """成本擋關：先估後擋。回傳 (放行?, 說明)。"""
    if n_rows <= max_rows:
        return True, f"預估讀取 {n_rows:,} 列（上限 {max_rows:,}）"
    if force:
        return True, f"★ 預估 {n_rows:,} 列超過上限 {max_rows:,}，但明寫 --force → 放行 ★"
    return False, (f"預估 {n_rows:,} 列，超過上限 {max_rows:,} → 拒絕執行。"
                   f"請縮小 --tools／--max-events-per-group，或明寫 --force")


# ── 資料讀取 ────────────────────────────────────────────────────
def sensor_columns(sensors: tuple, available: tuple) -> tuple:
    """使用者的大寫感測器名 → trace 表的小寫欄名（對不上要能回報，不可靜默）。"""
    low = {c.lower() for c in available}
    out, missing = {}, []
    for s in sensors:
        (out.update({s: s.lower()}) if s.lower() in low else missing.append(s))
    return out, missing


def stride_sample(events: list, k: int) -> list:
    """全期間等距抽樣（預設）：每 k 個取 1。

    ⚠ 不可用「前 N 個事件」——那會集中在最早期，低估組內變異，讓組間/組內比值虛高
      （第一版就是這樣，比值被高估）。等距抽樣才涵蓋整個時間範圍。
    """
    if k <= 1:
        return list(events)
    return events[::k][:max(1, len(events) // k)]


def bootstrap_ratio_ci(within: list, between: list, reps: int = 200,
                       seed: int = 7) -> tuple:
    """比值的 bootstrap 95% 信賴區間（對事件層重抽，不是對數值重抽）。

    問題不是「比值是多少」，而是「比值是否真的離 1 有距離」——沒有區間就無法回答。
    """
    w = [x for x in within if x is not None and np.isfinite(x)]
    b = [x for x in between if x is not None and np.isfinite(x)]
    if len(w) < 2 or len(b) < 2:
        return None, None
    rng = np.random.default_rng(seed)
    w_a, b_a = np.array(w), np.array(b)
    rs = []
    for _ in range(reps):
        mw = float(np.mean(rng.choice(w_a, w_a.size, replace=True)))
        mb = float(np.mean(rng.choice(b_a, b_a.size, replace=True)))
        if mw > 0:
            rs.append(mb / mw)
    if not rs:
        return None, None
    return float(np.percentile(rs, 2.5)), float(np.percentile(rs, 97.5))


def phase_a(cur, tools: tuple, max_events: int, min_points: int = 10,
            max_points: int = 2000, sample_mode: str = "stride") -> tuple:
    """A 形狀：取全部事件的（機台, recipe, 步驟, 點數），在 Python 端篩選＋等距抽樣。

    ⚠ entries 一定要保留 recipe：跨機台的比較必須用 **(recipe, recipe_step)** 當分組鍵。
      只用 recipe_step 會把「不同 recipe 的同號步驟」放在一起比 → 放大組間距離、
      讓判準變鬆（第一版就是這樣，唯一通過的那個結果可能是假的）。
    """
    cur.execute("""
        SELECT tool_id, recipe_step, count(*) AS n_events,
               min(n_points) AS n_min, max(n_points) AS n_max,
               percentile_disc(0.5) WITHIN GROUP (ORDER BY n_points) AS n_med,
               percentile_disc(0.5) WITHIN GROUP (ORDER BY sampling_median) AS s_med
        FROM stg.fact_process_event WHERE tool_id = ANY(%s)
        GROUP BY tool_id, recipe_step ORDER BY tool_id, recipe_step
    """, (list(tools),))
    shape_groups = {}
    for r in cur.fetchall():
        shape_groups[(r[0], r[1])] = {
            "n_events": int(r[2]), "n_points_min": int(r[3]), "n_points_max": int(r[4]),
            "n_points_median": float(r[5]), "sampling_median": float(r[6]) if r[6] else None}

    cur.execute("""SELECT tool_id, recipe, recipe_step, event_id, n_points
                   FROM stg.fact_process_event WHERE tool_id = ANY(%s)
                   ORDER BY tool_id, recipe, recipe_step, event_id""", (list(tools),))
    per_group: dict = defaultdict(list)
    for tool, recipe, step, eid, npts in cur.fetchall():
        per_group[(tool, recipe, step)].append({"event_id": int(eid), "n_points": int(npts)})

    stats = {"total": sum(len(v) for v in per_group.values()), "dropped_too_few": 0,
             "dropped_too_many": 0, "used": 0, "min_points": min_points,
             "max_points": max_points, "sample_mode": sample_mode}
    entries: dict = {}
    for key, evs in per_group.items():
        keep = []
        for e in evs:
            if e["n_points"] < min_points:
                stats["dropped_too_few"] += 1
            elif e["n_points"] > max_points:
                stats["dropped_too_many"] += 1
            else:
                keep.append(e)
        if sample_mode == "stride" and len(keep) > max_events:
            keep = stride_sample(keep, int(np.ceil(len(keep) / max_events)))
        elif sample_mode == "head":
            keep = keep[:max_events]
        elif sample_mode == "random":
            rng = np.random.default_rng(11)
            idx = rng.choice(len(keep), size=min(len(keep), max_events), replace=False)
            keep = [keep[i] for i in sorted(idx)]
        if keep:
            entries[key] = keep
            stats["used"] += len(keep)
    stats["n_groups_tool_recipe_step"] = len(entries)
    return shape_groups, entries, stats


def read_traces(conn, event_ids: list, cols: dict) -> dict:
    """伺服器端串流讀取 trace，回傳 {(event_id, 感測器名): (ts, ys)}。"""
    out: dict = {}
    names = list(cols)
    col_sql = ", ".join(f"t.{cols[n]}" for n in names)
    with conn.cursor(name="trace_precheck") as cur:
        cur.itersize = 20000
        cur.execute(f"SELECT t.event_id, t.ts, {col_sql} FROM stg.fact_sensor_trace t "
                    f"WHERE t.event_id = ANY(%s) ORDER BY t.event_id, t.ts",
                    (event_ids,))
        cur_ts: dict = defaultdict(list)
        cur_ys: dict = {n: defaultdict(list) for n in names}
        prev = None
        for row in cur:
            eid = int(row[0])
            cur_ts[eid].append(float(row[1]))
            for k, n in enumerate(names):
                v = row[2 + k]
                cur_ys[n][eid].append(float(v) if v is not None else float("nan"))
    for eid, ts_list in cur_ts.items():
        ts = np.array(ts_list, dtype=float)
        for n in names:
            out[(eid, n)] = (ts, np.array(cur_ys[n][eid], dtype=float))
    return out


def build_sequences(entries: dict, traces: dict, cols: dict, sensor: str,
                    mode: str, resample_len: int, norm: str = "zscore") -> dict:
    """{(tool, recipe, step): [已標準化的序列]}——每種（感測器, 模式）只算一次。"""
    out: dict = {}
    for (tool, recipe, step), evs in entries.items():
        seqs = []
        for e in evs:
            ts, ys = traces.get((e["event_id"], sensor), (None, None))
            if ts is None or ys is None:
                continue
            m = np.isfinite(ts) & np.isfinite(ys)
            if m.sum() < 3:
                continue
            v = (resample_to_length(ts[m], ys[m], resample_len) if mode == "resample"
                 else ys[m])
            z = zshape(v, norm)
            if z is not None:
                seqs.append(z)
        if seqs:
            out[(tool, recipe, step)] = seqs
    return out


def shared_coverage(entries: dict, min_events: int) -> dict:
    """{(recipe, step): {tool: n_events}} 只保留「至少 2 個機台且各 >= min_events」的鍵。

    這是 tool matching 的**前提**：沒有共享的 recipe 步驟，就沒有可比較的對象。
    """
    by_key: dict = defaultdict(dict)
    for (tool, recipe, step), evs in entries.items():
        if len(evs) >= min_events:
            by_key[(recipe, step)][tool] = len(evs)
    return {k: v for k, v in by_key.items() if len(v) >= 2}


def main() -> int:
    ap = argparse.ArgumentParser(description="M5-3-0 trace 對齊前置診斷（唯讀）")
    ap.add_argument("--tools", default="03")
    ap.add_argument("--sensors", default=",".join(DEFAULT_SENSORS))
    ap.add_argument("--max-events-per-group", type=int, default=60)
    ap.add_argument("--min-points", type=int, default=10,
                    help="trace 點數下限（步驟 4 的中位數是 1 點，無從對齊）")
    ap.add_argument("--max-points", type=int, default=2000,
                    help="trace 點數上限（排除異常長的合併事件）")
    ap.add_argument("--min-events-shared", type=int, default=5,
                    help="一個（recipe, 步驟）要算「共享」所需的最少事件數（每個機台）")
    ap.add_argument("--sample-mode", choices=("stride", "head", "random"), default="stride",
                    help="stride＝全期間等距（預設）；head＝前 N 個（低估組內變異，別用）")
    ap.add_argument("--max-rows", type=int, default=2_000_000)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--resample-len", type=int, default=50)
    ap.add_argument("--max-groups", type=int, default=20,
                    help="參與分析的共享（recipe, 步驟）前 N 個（成本控制）")
    ap.add_argument("--pairwise-cap", type=int, default=40)
    ap.add_argument("--dist-mode", choices=("to_mean", "pairwise"), default="to_mean",
                    help="to_mean＝到組內平均指紋的距離（O(n)、支援大樣本）；"
                         "pairwise＝兩兩 DTW（O(n^2)，第一版用它）")
    ap.add_argument("--bootstrap", type=int, default=200)
    ap.add_argument("--min-identify", type=float, default=0.75,
                    help="可辨識門檻（平衡正確率）；須同時勝過置換虛無且達到此值（0.5＝無資訊）")
    ap.add_argument("--norms", default="zscore,none",
                    help="序列前處理：zscore＝只留形狀；none＝保留水準與振幅（耗時類變數的訊號在水準上）")
    ap.add_argument("--permutations", type=int, default=100,
                    help="置換檢定次數（判準以此為主；比 1 更正確）")
    ap.add_argument("--trace-csv", default="")
    ap.add_argument("--no-db", action="store_true")
    ap.add_argument("--report", default="reports/m5/trace_precheck.json")
    add_version_arg(ap)
    args = ap.parse_args()

    tools_in = run_fdc.parse_csv_list(args.tools)
    sensors = run_fdc.parse_csv_list(args.sensors)
    report = {"version": script_version.current_version(), "tools": list(tools_in),
              "sensors": list(sensors), "resample_len": args.resample_len,
              "group_key": "recipe+recipe_step"}

    # ── 取資料 ───────────────────────────────────────────────
    if args.trace_csv:
        raw: dict = defaultdict(lambda: defaultdict(lambda: {"ts": [], "ys": defaultdict(list)}))
        with Path(args.trace_csv).open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                recipe = row.get("recipe", "(csv)")
                b = raw[(row["tool_id"], recipe, row["recipe_step"])][int(row["event_id"])]
                b["ts"].append(float(row["ts"]))
                for sn in sensors:
                    v = row.get(sn.lower(), "")
                    b["ys"][sn].append(float(v) if v not in ("", None) else float("nan"))
        entries = {k: [{"event_id": eid, "n_points": len(b["ts"])} for eid, b in sorted(v.items())]
                   for k, v in raw.items()}
        shape_groups = {(k[0], k[2]): {"n_events": len(v)} for k, v in entries.items()}
        stats = {"total": sum(len(v) for v in entries.values()), "used": sum(len(v) for v in entries.values()),
                 "dropped_too_few": 0, "dropped_too_many": 0, "sample_mode": "csv",
                 "min_points": args.min_points, "max_points": args.max_points}
        print(f"[模式] CSV（助理端驗證）｜{args.trace_csv}")
        col_names = {sn: sn.lower() for sn in sensors}
        traces = {}
        for key, evs in raw.items():
            for eid, b in evs.items():
                ts = np.array(b["ts"], dtype=float)
                for sn in sensors:
                    traces[(eid, sn)] = (ts, np.array(b["ys"][sn], dtype=float))
    else:
        import psycopg
        conn = psycopg.connect(host=os.environ.get("PGHOST", "db"),
                               port=int(os.environ.get("PGPORT", 5432)),
                               dbname=os.environ.get("PGDATABASE", "commonality"),
                               user=os.environ["PGUSER"],
                               password=os.environ.get("PGPASSWORD", ""))
        cur = conn.cursor()
        tools, missing = run_fdc.resolve_tool_ids(cur, tools_in)
        if missing:
            print(f"[代號] 解析不到的機台 {missing}（exit 2）")
            return 2
        cur.execute("""SELECT column_name FROM information_schema.columns
                       WHERE table_schema='stg' AND table_name='fact_sensor_trace'""")
        available = tuple(r[0] for r in cur.fetchall())
        cols, missing_s = sensor_columns(sensors, available)
        if missing_s:
            print(f"[感測器] trace 表沒有這些欄位：{missing_s}（exit 2）")
            return 2
        print(f"[代號] --tools {list(tools_in)} → {tools}")
        print(f"[感測器] 對應 trace 欄位：{list(cols.values())}")

        shape_groups, entries_all, stats = phase_a(
            cur, tools, args.max_events_per_group, args.min_points, args.max_points,
            args.sample_mode)
        report["filter"] = stats
        print(f"[篩選] 總事件 {stats['total']:,}｜點數 < {args.min_points} 丟棄 "
              f"{stats['dropped_too_few']:,}｜點數 > {args.max_points} 丟棄 "
              f"{stats['dropped_too_many']:,}｜可用 {stats['used']:,}"
              f"（{stats['sample_mode']} 抽樣）")

        # ⚠ 跨機台比較的分組鍵是 (recipe, recipe_step)，不是 recipe_step
        shared = shared_coverage(entries_all, args.min_events_shared)
        report["shared_coverage"] = [{"recipe": k[0], "recipe_step": k[1], "n_tools": len(v),
                                      "tools": v} for k, v in sorted(
            shared.items(), key=lambda kv: -len(kv[1]))[:30]]
        print(f"\n[A0] 跨機台共享空間（分組鍵＝recipe＋recipe_step；"
              f"門檻：每機台 ≥ {args.min_events_shared} 個可用事件）")
        print(f"     共享（recipe, 步驟）組合 {len(shared)} 個"
              f"（總組合 {stats['n_groups_tool_recipe_step']} 個）")
        for (recipe, step), by_tool in sorted(shared.items(),
                                              key=lambda kv: -min(kv[1].values()))[:10]:
            print(f"     recipe {recipe}／步驟 {step}：{len(by_tool)} 台 → "
                  + "、".join(f"{t}({n})" for t, n in sorted(by_tool.items())))
        if not shared:
            print("     ★ 沒有任何共享的（recipe, 步驟）→ tool matching 在此資料上不可行 ★")
            report["verdict"] = "no_shared_recipe_step"
            Path(args.report).parent.mkdir(parents=True, exist_ok=True)
            Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
            return 5

        # 只保留共享組合中事件數較多的前 N 組（成本控制）
        selected = dict(sorted(shared.items(), key=lambda kv: -min(kv[1].values()))[:args.max_groups])
        entries = {k: v for k, v in entries_all.items() if (k[1], k[2]) in selected}
        est_rows = sum(e["n_points"] for v in entries.values() for e in v)
        ok, msg = cost_gate(est_rows, args.max_rows, args.force)
        print(f"\n[成本] 選用共享組合 {len(selected)} 個｜事件 "
              f"{sum(len(v) for v in entries.values()):,}｜{msg}")
        if not ok:
            return 4
        ids = [e["event_id"] for v in entries.values() for e in v]
        traces = read_traces(conn, ids, cols)
        print(f"[讀取] 取得 {len(traces):,} 條（事件 × 感測器）序列")
        col_names = cols
        conn.close()

    # ── A 形狀 ───────────────────────────────────────────────
    print(f"\n[A] 逐（機台 × 步驟）事件長度（事件數最多的前 10 個）")
    shape = []
    for (tool, step), g in sorted(shape_groups.items(), key=lambda kv: -kv[1]["n_events"])[:10]:
        st = (f"點數 {g['n_points_min']}–{g['n_points_max']}"
              f"（中位 {g['n_points_median']:.0f}）｜取樣間隔中位 {g.get('sampling_median')}"
              ) if g.get("n_points_median") is not None else "點數（CSV 模式無統計）"
        print(f"    {tool}/{step}：事件 {g['n_events']:,}｜{st}")
        shape.append({"tool": tool, "recipe_step": step, **g})
    report["shape"] = shape

    # ── C/D 對齊比較（同一個 recipe＋步驟、跨機台）─────────────
    print(f"\n[C/D] 對齊方式比較：組間／組內距離比（同一 recipe＋步驟、跨機台）"
          f"｜距離定義 {args.dist_mode}")
    print(f"    {'感測器':<20}{'前處理':<7}{'表徵':<9}{'組間/組內':>8}{'bootstrap CI':>16}"
          f"{'組數':>4}{'可辨識率':>8}{'集團':>7}  判準（置換檢定＋門檻 {args.min_identify}，隨機 0.5）")
    report["alignment"] = {}
    n_pass = 0
    for sensor in sensors:
      for norm in [n.strip() for n in args.norms.split(",") if n.strip()]:
        for mode in ("resample", "dtw"):
            seqs = build_sequences(entries, traces, col_names, sensor, mode,
                                   args.resample_len, norm)
            by_key: dict = defaultdict(dict)
            for (tool, recipe, step), v in seqs.items():
                by_key[(recipe, step)][tool] = v
            within, between = within_between(by_key, args.dist_mode, args.pairwise_cap)
            n_groups = len([1 for k, bt in by_key.items()
                            if len([t for t, v in bt.items() if len(v) >= 3]) >= 2])
            null = permutation_null(by_key, args.dist_mode, args.permutations,
                                    args.pairwise_cap)
            rates, id_groups, id_eligible, id_total = identify_by_key(by_key)
            identify = float(np.mean(rates)) if rates else None
            id_complete = (id_eligible > 0 and id_groups == id_eligible)
            ratio = between_within_ratio(within, between)
            lo, hi = bootstrap_ratio_ci(within, between, args.bootstrap)
            q95, p = null["null_q95"], null["p_value"]
            beats_null = (q95 is not None and ratio is not None and ratio > q95)
            # ⚠ 雙條件：置換檢定（非隨機）＋ AUC（可辨識）。只看 p 值會被檢定力綁架。
            # ⚠ 可辨識率必須在「所有合格集團」上都算得出來才算數：
            #    否則會出現「比值樣本不足、可辨識率卻很高」的矛盾列（實際踩到 dtw 0.902）
            passed = bool(beats_null and identify is not None and id_complete
                          and identify >= args.min_identify)
            n_pass += int(passed)
            ci = f"[{lo:.3f},{hi:.3f}]" if lo is not None else "—"
            if q95 is None or not id_complete:
                sig = (f"—（樣本不足：可辨識率僅用 {id_groups}/{id_eligible} 個集團，"
                       f"不可比）" if not id_complete else "—（樣本不足）")
            elif passed:
                sig = (f"✓ 可辨識（{identify:.3f} vs 隨機 0.5、p={p:.3f}）")
            elif beats_null:
                sig = (f"✗ 非隨機但不可辨識（{identify:.3f} < {args.min_identify}；"
                       f"p={p:.3f} 只是檢定力大）")
            else:
                sig = (f"✗ 未勝過虛無（{identify if identify is None else round(identify, 3)}"
                       f"、p={p:.3f}）")
            print(f"    {sensor:<20}{norm:<7}{mode:<9}"
                  f"{(f'{ratio:.3f}' if ratio is not None else '—'):>8}{ci:>16}{n_groups:>4}"
                  f"{(f'{identify:.3f}' if identify is not None else '—'):>8}"
                  f"{(f'{id_groups}/{id_eligible}' if id_eligible else '—'):>7}  {sig}")
            report["alignment"].setdefault(sensor, {})[f"{norm}:{mode}"] = {
                "dist_mode": args.dist_mode, "n_within": len(within), "n_between": len(between),
                "between_within_ratio": ratio, "ci95": [lo, hi], "n_groups": n_groups,
                "permutation": null, "beats_null": beats_null,
                "norm": norm, "identify_rate_mean": identify,
                "n_identify_pairs": len(rates), "identify_groups_used": id_groups,
                "identify_groups_eligible": id_eligible, "identify_complete": id_complete,
                "chance_level": 0.5, "min_identify": args.min_identify,
                "usable": passed,
                "mean_within": float(np.mean(within)) if within else None,
                "mean_between": float(np.mean(between)) if between else None}

    report["n_pass_criteria"] = n_pass
    report["n_combinations"] = len(sensors) * len([n for n in args.norms.split(",") if n.strip()]) * 2
    report["min_identify"] = args.min_identify
    report["chance_level"] = 0.5
    report["caveat"] = ("門檻 0.75；二元分類隨機基準 0.5。可辨識率必須在全部合格集團上"
                        "算得出來才納入判準。")
    verdict = ("proceed" if n_pass else "shape_cannot_identify_tool")
    report["verdict"] = verdict
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  報告：{out}")
    print(f"  判準結果：{n_pass}/{report['n_combinations']} 個（感測器 × 前處理 × 表徵）通過")
    if n_pass == 0:
        print("  ⚠ 措辭提醒：這是『沒有證據支持』，不是『證明沒有』——")
        print("     若 CI 寬度過大（例如 ±0.4），代表檢定力不足，需先加大樣本再下結論。")
    print(f"  距離定義 {args.dist_mode}｜置換 {args.permutations} 次"
          f"（門檻＝虛無 95% 上界，不是 1）")
    if n_pass:
        best = max(((s, m, v[ratio_key]) for s, d in report["alignment"].items()
                    for m, v in d.items() if v.get("usable")
                    for ratio_key in ("between_within_ratio",)),
                   key=lambda t: t[2], default=None)
        if best:
            print(f"  → 建議用 {best[0]} 的 {best[1]}（比值 {best[2]:.3f}）做 M5-3-1")
    else:
        print("  → ★ 沒有任何組合通過判準（門檻 0.75；隨機 0.5）★")
        print("  → trace 形狀在此資料上不足以做機台匹配；M5-3-1（形狀對齊）不應進行，")
        print("    建議把 M5-3 改成「trace 的變異數分解」或「以步驟時長為主的 tool matching」")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
