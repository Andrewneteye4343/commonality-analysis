"""M5-3′ 前導檢查：單機漂移指紋「有沒有判讀空間」。

要回答的問題（依序，任一關不成立就要停下回報，不硬做）
────────────────────────────────────────────────────────────
  P1 可用性：每台機台在哪些（recipe, 步驟）有足夠長的事件序列可追蹤？
  P2 工具特有 vs 共同老化：
     隨機台老化本來就會漂移（`runnum` 只增不減）→ 這是必然存在的，
     不是訊號。**必須用同儕對照**：同一（recipe, 步驟）下，這台機台的漂移
     斜率是否與其他機台不同？若各台斜率一致 → 只是共同老化 ✗ → 沒有判讀價值。
  P3 是否領先故障：故障窗（24 小時）前的漂移殘差，是否系統性大於該機台的
     平時水準？若沒有 → 漂移指紋沒有預測力 ✗（誠實的負面結論）。

判讀原則
    P2 通過（工具特有）才接著看 P3；若漂移只是共同老化，P3 的高低不具意義。
    P3 用「每台機台自己的趨勢線」去趨勢後取標準化殘差 |z|（離群值用 MAD 穩健尺度）。

用法
    docker compose run --rm etl python analysis/drift_precheck.py --tools "03"
    python analysis/drift_precheck.py --csv <檔> --faults-csv <檔> --no-db   # 助理端
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

FEATURE_DEFAULT = "mean_val"
DRIFT_Z = 3.0            # 漂移旗標門檻（|z| > 3）
PEER_Z = 2.0             # 同儕比較：斜率偏離其他機台中位數幾個穩健標準差算「工具特有」


# ── 純函式（可單元測試）─────────────────────────────────────────
def lin_slope(x: np.ndarray, y: np.ndarray) -> tuple:
    """最小平方直線；回傳 (截距, 斜率, R², 斜率的標準誤)。

    標準誤的用途：比較兩台機台的斜率時，分母必須是**估計誤差**，不是
    「幾個同儕斜率的離散度」——後者在同儕斜率幾乎相同時會趨近 0，
    讓任何微小差異變成天文數字（實際踩到 z = −210,574，毫無意義）。
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 4 or np.ptp(x[m]) == 0:
        return float("nan"), float("nan"), float("nan"), float("nan")
    x, y = x[m], y[m]
    b, a = np.polyfit(x, y, 1)
    pred = a + b * x
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    sxx = float(np.sum((x - x.mean()) ** 2))
    se = float(np.sqrt((ss_res / (x.size - 2)) / sxx)) if sxx > 0 else float("nan")
    return (float(a), float(b), (1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")), se)


def detrend_residual(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """去掉自己的趨勢線後的殘差（趨勢 = 老化；殘差 = 偏離老化的異常）。"""
    a, b, _, _ = lin_slope(x, y)
    if not np.isfinite(a):
        return np.full(np.asarray(y).shape, np.nan)
    return np.asarray(y, dtype=float) - (a + b * np.asarray(x, dtype=float))


def robust_sigma(r: np.ndarray) -> float:
    """MAD 型穩健標準差（1.4826×MAD）；退化時回退到標準差，再不行回傳 nan。

    ⚠ 為什麼實際上都改用 baseline_sigma：合成測試踩到「植入 +6 的漂移，
      故障窗命中率卻算成 0」。原因是**趨勢擬合會吸收一部分漂移**，
      殘差因此留下結構性成分、把全序列的 MAD 一起拉大（不是教科書說的
      「>25% 污染就會壞」——MAD 真正失效是中位數被推移，即 >50% 污染）。
      用序列前段（基準期）估尺度可以同時避開這兩件事，
      也與 M5-1／M5-2 的基準期慣例一致。
    """
    r = np.asarray(r, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 3:
        return float("nan")
    mad = float(np.median(np.abs(r - np.median(r))))
    s = 1.4826 * mad
    if s <= 0:
        s = float(np.std(r, ddof=1))
    return s if s > 0 else float("nan")


def baseline_sigma(r: np.ndarray, frac: float = 0.3) -> float:
    """用序列**前段**（基準期）估殘差尺度——避免 MAD 被後段大範圍漂移污染。"""
    r = np.asarray(r, dtype=float)
    n = r.size
    if n < 6:
        return float("nan")
    k = max(3, int(n * frac))
    return robust_sigma(r[:k])


CHI2_CRIT_95 = {1: 3.841, 2: 5.991, 3: 7.815, 4: 9.488, 5: 11.070, 6: 12.592,
                7: 14.067, 8: 15.507, 9: 16.919, 10: 18.307}


def cochran_q(slopes: dict, ses: dict) -> dict:
    """Cochran's Q：檢定「這些機台的斜率是否全部相等」（固定效應）。

    為什麼要先做整體檢定：同儕只有 2–3 台時，「同儕中位數」本身就是不穩定的量
    （兩台的「中位數」等於平均）→ 會把正常的機台也判成偏離（合成測試實際踩到：
    A 被算出 z = −3.5）。統計上能可靠回答的是「這一組的斜率是否一致」，
    之後才談「誰最偏離」，且必須說明事後歸因在同儕少時很弱。
    臨界值用硬編碼表（df ≤ 10），避免為了 χ² 分位數引入 scipy 依賴。
    """
    ks = sorted(k for k in slopes if k in ses and np.isfinite(slopes[k])
                and ses[k] is not None and np.isfinite(ses[k]) and ses[k] > 0)
    if len(ks) < 3:
        return {"n": len(ks), "Q": None, "df": None, "reject": False,
                "note": "少於 3 台 → 不做整體檢定"}
    w = np.array([1.0 / ses[k] ** 2 for k in ks])
    b = np.array([slopes[k] for k in ks])
    bbar = float(np.sum(w * b) / np.sum(w))
    Q = float(np.sum(w * (b - bbar) ** 2))
    df = len(ks) - 1
    crit = CHI2_CRIT_95.get(df)
    most = None
    if crit is not None:
        se_mean = float(np.sqrt(1.0 / np.sum(w)))
        zs = {k: (slopes[k] - bbar) / np.sqrt(ses[k] ** 2 + se_mean ** 2) for k in ks}
        most = max(zs, key=lambda k: abs(zs[k]))
        return {"n": len(ks), "Q": Q, "df": df, "crit95": crit, "reject": bool(Q > crit),
                "weighted_mean": bbar, "most_deviant": most,
                "most_deviant_z": float(zs[most]), "z_by_tool": {k: float(v) for k, v in zs.items()}}
    return {"n": len(ks), "Q": Q, "df": df, "crit95": None, "reject": False,
            "note": f"df={df} 超出臨界值表"}


def peer_deviation(slopes: dict, ses: dict | None = None) -> dict:
    """同儕比較：這台機台的斜率偏離「其他機台斜率中位數」幾個標準誤（Welch 型 z）。

    ⚠ 分母**不可**用「同儕斜率的離散度」：同儕只有 2–4 台，且它們的斜率可能幾乎相同
      → 離散度趨近 0 → 任何微差都變成 z = −210,574（實際踩到），完全不可信。
      正確分母是**回歸斜率本身的標準誤**（來自殘差散布），與同儕數量多寡無關。
    另附相對差異（scale-free），供人工檢視。
    """
    ses = ses or {}
    tools = sorted(k for k, v in slopes.items() if v is not None and np.isfinite(v))
    out = {}
    for t in tools:
        others = [slopes[u] for u in tools if u != t]
        if len(others) < 2:
            out[t] = {"z": None, "others_median": float(np.median(others)) if others else None}
            continue
        med = float(np.median(others))
        se_other = [ses.get(u) for u in tools if u != t and ses.get(u) is not None
                    and np.isfinite(ses.get(u))]
        se_t = ses.get(t)
        if not se_other or se_t is None or not np.isfinite(se_t):
            out[t] = {"z": None, "others_median": med,
                      "note": "缺少標準誤 → 不做同儕判準"}
            continue
        se_pool = float(np.sqrt(np.mean(np.square(se_other))))
        denom = float(np.sqrt(se_t ** 2 + se_pool ** 2))
        rel = (slopes[t] - med) / abs(med) if med != 0 else float("nan")
        out[t] = {"z": (float((slopes[t] - med) / denom) if denom > 0 else None),
                  "others_median": med, "rel_diff": float(rel),
                  "se_self": float(se_t), "se_peers": se_pool}
    return out


def peer_adjusted_residual(series_by_tool: dict, bin_seconds: float) -> dict:
    """把每個事件的值換成「相對同期同儕中位數」的偏離（扣掉共同老化）。

    為什麼需要它：用「自己趨勢線」去趨勢時，若老化不是直線，殘差會隨時間系統性放大
    → 指標幾乎一直亮著（實測基準命中率 25–49%，而 3σ 旗標理應只有 1–2%）
    → 這種指標量的是「比基準期晚多少」，不是「異常」。
    這裡改成：在同一時間窗內比較這台與**其他機台**的中位數 → 共同老化被扣掉，
    只留工具特異的偏離（正是業界 tool matching 的形式）。

    series_by_tool：{tool: [(ts, value, event_id), ...]}
    ⚠ event_id 從頭一路帶下來，不做「用時間回推」配對（同時間可能多個事件 → 會靜默配錯）。
    回傳：{tool: [(ts, value − 同儕中位數, n_peers, event_id), ...]}
    """
    tools = sorted(series_by_tool)
    if len(tools) < 2:
        return {}
    bins: dict = defaultdict(lambda: defaultdict(list))
    for tool in tools:
        for ts, val, _eid in series_by_tool[tool]:
            if val is None or not np.isfinite(val) or not np.isfinite(ts):
                continue
            bins[int(ts // bin_seconds)][tool].append(val)
    peer_med = {b: {t: float(np.median(v)) for t, v in d.items() if v} for b, d in bins.items()}
    out: dict = {t: [] for t in tools}
    for tool in tools:
        for ts, val, eid in series_by_tool[tool]:
            if val is None or not np.isfinite(val) or not np.isfinite(ts):
                continue
            b = int(ts // bin_seconds)
            others = [peer_med[b][u] for u in tools if u != tool and u in peer_med.get(b, {})]
            if not others:
                continue
            out[tool].append((float(ts), float(val) - float(np.median(others)), len(others), eid))
    return out


def in_window_by_fault_time(ts: np.ndarray, fault_ts: list,
                            window_hours: float = 24.0) -> np.ndarray:
    """依故障時刻自己標記故障窗（**只在沒有 mart.event_labels 時使用**）。

    ⚠ `in_ts` 的單位是**秒**，所以窗寬必須乘 3600。先前寫成 `ft - 24`（＝24 秒）→
      等於只開 24 秒的窗、故障窗前事件數幾乎為 0，然後把「空窗」誤判成「沒有預測力」。
      DB 模式一律改用 `run_fdc.load_labels`（`mart.event_labels` 是既有且已驗證的定義）。
    """
    ts = np.asarray(ts, dtype=float)
    mask = np.zeros(ts.shape, dtype=bool)
    for ft in fault_ts:
        mask |= (ts >= ft - window_hours * 3600.0) & (ts <= ft)
    return mask


def lead_fault_stats(z_abs: np.ndarray, ts: np.ndarray, fault_ts: list = None,
                     window_hours: float = 24.0, in_window: np.ndarray = None) -> dict:
    """故障窗內外的漂移強度對照（in_window 可直接給，優先於 fault_ts 推算）。

    z_abs：每個事件的漂移強度（標準化殘差絕對值）
    """
    z_abs = np.asarray(z_abs, dtype=float)
    ts = np.asarray(ts, dtype=float)
    ok = np.isfinite(z_abs) & np.isfinite(ts)
    z_abs, ts = z_abs[ok], ts[ok]
    base_hit = float(np.mean(z_abs > DRIFT_Z)) if z_abs.size else None
    base_med = float(np.median(z_abs)) if z_abs.size else None
    if in_window is not None:
        iw = np.asarray(in_window, dtype=bool)[ok] if np.asarray(in_window).shape == z_abs.shape \
            else np.asarray(in_window, dtype=bool)
        mask = iw
    else:
        mask = in_window_by_fault_time(ts, fault_ts or [], window_hours)
    pre = z_abs[mask]
    return {"base_hit_rate": base_hit, "base_median": base_med,
            "pre_hit_rate": (float(np.mean(pre > DRIFT_Z)) if pre.size else None),
            "pre_median": (float(np.median(pre)) if pre.size else None),
            "pre_n": int(pre.size),
            "ratio": (float(np.mean(pre > DRIFT_Z) / base_hit)
                      if pre.size and base_hit and base_hit > 0 else None)}


# ── 資料讀取 ────────────────────────────────────────────────────
def iter_db(tools: tuple, sensors: tuple, feature: str):
    """串流 (機台, recipe, 步驟, 感測器) 的事件序列（含 recipe —— 同儕對照要用）。"""
    import psycopg
    conn = psycopg.connect(host=os.environ.get("PGHOST", "db"),
                           port=int(os.environ.get("PGPORT", 5432)),
                           dbname=os.environ.get("PGDATABASE", "commonality"),
                           user=os.environ["PGUSER"],
                           password=os.environ.get("PGPASSWORD", ""))
    sql = f"""
        SELECT e.tool_id, e.recipe, e.recipe_step, f.sensor,
               e.event_id, e.in_ts::double precision, f.{feature}::double precision
        FROM stg.fact_sensor_feature f
        JOIN stg.fact_process_event e ON e.event_id = f.event_id
        WHERE (%s = ARRAY[]::text[] OR e.tool_id = ANY(%s))
          AND (%s = ARRAY[]::text[] OR f.sensor = ANY(%s))
        ORDER BY e.tool_id, e.recipe, e.recipe_step, f.sensor, e.in_ts, e.event_id
    """
    with conn, conn.cursor(name="drift_stream") as cur:
        cur.itersize = 20000
        cur.execute(sql, (list(tools), list(tools), list(sensors), list(sensors)))
        key_prev, buf = None, []
        for row in cur:
            key = (row[0], row[1], row[2], row[3])
            if key_prev is not None and key != key_prev:
                yield key_prev, buf
                buf = []
            key_prev = key
            val = row[6]
            buf.append({"event_id": row[4], "in_ts": float(row[5]),
                        "value": None if val is None else float(val)})
        if key_prev is not None:
            yield key_prev, buf
    conn.close()


def iter_csv(path: Path, sensors: tuple, feature: str):
    """CSV 模式：欄位 tool_id, recipe, recipe_step, sensor, event_id, in_ts, <feature>。"""
    rows = defaultdict(list)
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("sensor") and row["sensor"] not in sensors:
                continue
            key = (row["tool_id"], row.get("recipe", "(csv)"), row["recipe_step"],
                   row.get("sensor", "SEN"))
            val = row.get(feature, "")
            # ⚠ event_id 必須轉 int：DB 模式是 bigint（int），若這裡留字串，
            #   與 mart.event_labels 的整數鍵比對會「靜默全對不上」（實際踩到：
            #   窗內事件數 0，然後被誤讀成「漂移沒有預測力」）。
            rows[key].append({"event_id": int(row["event_id"]), "in_ts": float(row["in_ts"]),
                              "value": float(val) if val not in ("", None) else None})
    for key, evs in rows.items():
        yield key, sorted(evs, key=lambda r: r["in_ts"])


def main() -> int:
    ap = argparse.ArgumentParser(description="M5-3′ 漂移指紋前導檢查（唯讀）")
    ap.add_argument("--tools", default="03")
    ap.add_argument("--sensors", default="FLOWCOOLPRESSURE,IONGAUGEPRESSURE,ETCHBEAMVOLTAGE,"
                                         "ETCHBEAMCURRENT,ACTUALSTEPDURATION")
    ap.add_argument("--feature", default=FEATURE_DEFAULT,
                    help="用哪個聚合特徵追蹤水準（預設 mean_val ＝ trace 的平均水準）")
    ap.add_argument("--window-hours", type=float, default=24.0)
    ap.add_argument("--bin-hours", type=float, default=24.0,
                    help="同儕對照的時間窗寬（小時）——同一窗內比這台與其他台的中位數")
    ap.add_argument("--baseline-frac", type=float, default=0.3,
                    help="估殘差尺度的前置比例（避免 MAD 被後段大範圍漂移污染）")
    ap.add_argument("--min-events", type=int, default=30,
                    help="一條（機台, recipe, 步驟, 感測器）序列至少要有幾個事件才納入")
    ap.add_argument("--csv", default="")
    ap.add_argument("--faults-csv", default="")
    ap.add_argument("--labels-csv", default="",
                    help="CSV 模式的故障窗標記（event_id,in_window）")
    ap.add_argument("--no-db", action="store_true")
    ap.add_argument("--report", default="reports/m5/drift_precheck.json")
    add_version_arg(ap)
    args = ap.parse_args()

    tools_in = run_fdc.parse_csv_list(args.tools)
    sensors = tuple(run_fdc.parse_csv_list(args.sensors))
    report = {"version": script_version.current_version(), "feature": args.feature,
              "tools_requested": list(tools_in), "sensors": list(sensors),
              "window_hours": args.window_hours, "min_events": args.min_events}

    if args.csv:
        print(f"[模式] CSV（助理端驗證）｜{args.csv}")
        series = list(iter_csv(Path(args.csv), sensors, args.feature))
        faults = {}
        if args.faults_csv:
            for row in csv.DictReader(Path(args.faults_csv).open(encoding="utf-8")):
                faults.setdefault(row["tool_id"], []).append(float(row["fault_ts"]))
        labelled = {}
        if args.labels_csv:
            for row in csv.DictReader(Path(args.labels_csv).open(encoding="utf-8")):
                labelled[int(row["event_id"])] = row["in_window"].strip().lower() in ("1", "true", "t")
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
        print(f"[代號] --tools {list(tools_in)} → {tools}")
        series = list(iter_db(tuple(tools), sensors, args.feature))
        faults = run_fdc.load_fault_times(conn)
        # ⚠ 故障窗一律沿用 mart.event_labels（既有、已驗證的定義：0 <= hours_to_fault <= window）
        labelled = run_fdc.load_labels(conn, "ANY", int(round(args.window_hours)))
        conn.close()

    # ── P1 可用性 ───────────────────────────────────────────────
    print(f"\n[P1] 可用序列（每個（機台, recipe, 步驟, 感測器）至少 {args.min_events} 個事件）")
    by_key: dict = {}
    for (tool, recipe, step, sensor), evs in series:
        vals = np.array([e["value"] if e["value"] is not None else np.nan for e in evs], float)
        ts = np.array([e["in_ts"] for e in evs], float)
        if np.isfinite(vals).sum() < args.min_events:
            continue
        by_key[(tool, recipe, step, sensor)] = {
            "ts": ts, "values": vals, "eids": [e["event_id"] for e in evs]}
    tools_seen = sorted({k[0] for k in by_key})
    print(f"    序列 {len(by_key)} 條｜機台 {tools_seen}")
    report["p1"] = {"n_series": len(by_key), "tools": tools_seen}
    if not by_key:
        print("    ★ 沒有任何可用序列 → 無法做漂移追蹤（exit 3）★")
        return 3

    # ── P2 工具特有 vs 共同老化 ─────────────────────────────────
    print(f"\n[P2] 同儕對照：同一（recipe, 步驟, 感測器）下，各機台的漂移斜率是否不同？")
    print(f"     （判準：|z| ≥ {PEER_Z} 才叫工具特有；各台一致＝共同老化 ✗）")
    slopes_by_group: dict = defaultdict(dict)
    ses_by_group: dict = defaultdict(dict)
    for (tool, recipe, step, sensor), d in by_key.items():
        _, b, r2, se = lin_slope(d["ts"], d["values"])
        slopes_by_group[(recipe, step, sensor)][tool] = b
        ses_by_group[(recipe, step, sensor)][tool] = se
    p2_rows, n_groups_tested = [], 0
    for gkey, slopes in sorted(slopes_by_group.items()):
        valid = {t: s for t, s in slopes.items() if np.isfinite(s)}
        if len(valid) < 3:
            continue
        q = cochran_q(valid, ses_by_group[gkey])
        if q["Q"] is None:
            continue
        n_groups_tested += 1
        if not q["reject"]:
            continue
        md = q["most_deviant"]
        p2_rows.append({"recipe": gkey[0], "recipe_step": gkey[1], "sensor": gkey[2],
                        "Q": q["Q"], "df": q["df"], "crit95": q["crit95"],
                        "most_deviant": md, "most_deviant_z": q["most_deviant_z"],
                        "slope": valid.get(md),
                        "slopes": {t: valid[t] for t in sorted(valid)}})
    p2_rows.sort(key=lambda r: -r["Q"])
    for r in p2_rows[:10]:
        print(f"     recipe {r['recipe']}／步驟 {r['recipe_step']}／{r['sensor']}："
              f"Q={r['Q']:.1f} > 臨界 {r['crit95']:.2f}（df={r['df']}）"
              f"｜最偏離 {r['most_deviant']}（斜率 {r['slope']:.3e}，z {r['most_deviant_z']:+.2f}）")
    frac = (len(p2_rows) / n_groups_tested) if n_groups_tested else 0.0
    print(f"     → 斜率不一致（工具特有）的（recipe, 步驟, 感測器）組合："
          f"{len(p2_rows)}/{n_groups_tested}（{frac:.1%}）")
    print("       ⚠ 事後歸因只在同儕夠多時可靠；同儕 2–3 台時只看『這一組不一致』這件事。")
    if frac < 0.1:
        print("     ✗ 絕大多數組合的斜率一致 → 漂移主要是【共同老化】，不是個別機台劣化")
    else:
        print("     ✓ 有相當比例的組合是【工具特有】漂移 → 值得看它是否領先故障")
    report["p2"] = {"n_groups_tested": n_groups_tested,
                    "n_groups_tool_specific": len(p2_rows), "fraction": frac,
                    "top": p2_rows[:30]}

    # ── P3 是否領先故障 ─────────────────────────────────────────
    print(f"\n[P3] 故障窗（前 {args.window_hours:.0f} 小時）的漂移強度 vs 平時")
    print(f"     （漂移強度＝去趨勢殘差 ÷ 前置 {args.baseline_frac:.0%} 基準期的穩健尺度，"
          f"門檻 |z| > {DRIFT_Z}）")
    # 兩種指標並列：① 相對自己趨勢線（會隨時間系統性放大）② 相對同期同儕（扣掉共同老化）
    per_event: dict = defaultdict(lambda: {"ts": [], "z": [], "eid": []})
    per_event_peer: dict = defaultdict(lambda: {"ts": [], "z": [], "eid": []})
    by_group_series: dict = defaultdict(dict)
    for (tool, recipe, step, sensor), d in by_key.items():
        r = detrend_residual(d["ts"], d["values"])
        s0 = baseline_sigma(r, args.baseline_frac)
        if np.isfinite(s0):
            z = np.abs(r / s0)
            per_event[tool]["ts"].extend(list(d["ts"]))
            per_event[tool]["z"].extend(list(np.where(np.isfinite(z), z, np.nan)))
            per_event[tool]["eid"].extend(d["eids"])
        by_group_series[(recipe, step, sensor)][tool] = list(
            zip(d["ts"], d["values"], d["eids"]))

    bin_sec = max(args.bin_hours, 1e-9) * 3600.0
    for gkey, series_by_tool in by_group_series.items():
        if len(series_by_tool) < 2:
            continue
        for tool, rows in peer_adjusted_residual(series_by_tool, bin_sec).items():
            if not rows:
                continue
            ts_a = np.array([r0[0] for r0 in rows], float)
            res_a = np.array([r0[1] for r0 in rows], float)
            s_a = baseline_sigma(res_a, args.baseline_frac)
            if not np.isfinite(s_a):
                continue
            per_event_peer[tool]["ts"].extend(list(ts_a))
            per_event_peer[tool]["z"].extend(list(np.abs(res_a / s_a)))
            per_event_peer[tool]["eid"].extend([r0[3] for r0 in rows])

    rows_p3 = []
    n_labelled = sum(1 for v in labelled.values() if v)
    print(f"     故障窗標記（mart.event_labels，窗 {args.window_hours:.0f} 小時）："
          f"{len(labelled):,} 個事件有標記、其中 {n_labelled:,} 個落在窗內")
    for tool, d in sorted(per_event.items()):
        ft = faults.get(tool, [])
        eids = d["eid"]
        inw = np.array([bool(labelled.get(e)) for e in eids], dtype=bool) if labelled else None
        n_matched = int(sum(1 for e in set(eids) if e in labelled)) if labelled else 0
        st = lead_fault_stats(np.array(d["z"], float), np.array(d["ts"], float), ft,
                              args.window_hours, in_window=inw)
        dp_ = per_event_peer.get(tool, {"z": [], "ts": [], "eid": []})
        inw_p = (np.array([bool(labelled.get(e)) for e in dp_["eid"]], dtype=bool)
                 if labelled else None)
        sp = lead_fault_stats(np.array(dp_["z"], float), np.array(dp_["ts"], float), ft,
                              args.window_hours, in_window=inw_p)
        st.update({"tool": tool, "faults": len(ft), "n_events_distinct": len(set(eids)),
                   "n_events_labelled_matched": n_matched,
                   "n_events": int(np.isfinite(np.array(d["z"], float)).sum()),
                   "peer": sp})
        if labelled and st["n_events_distinct"] and n_matched == 0:
            print(f"     ★ {tool}：標籤一個都對不上（{st['n_events_distinct']} 個事件）"
                  f"→ 這是 bug，不是沒有預測力 ★")
        rows_p3.append(st)
        tag = "" if ft else "（無故障 → 負控制）"
        def _r(v):
            return "—" if v is None else f"{v:.4f}"

        def _s(v):
            return "—" if v is None else f"{v:.2f}"

        print(f"     {tool}{tag}：{st['n_events']:,} 事件、{len(ft)} 次故障"
              f"（標籤對上 {st['n_events_labelled_matched']}/{st['n_events_distinct']}）")
        print(f"         ①自己趨勢線：窗內 {_r(st['pre_hit_rate'])}（{st['pre_n']} 事件）"
              f" vs 窗外 {_r(st['base_hit_rate'])}｜比值 {_s(st['ratio'])}")
        print(f"         ②同儕對照：  窗內 {_r(sp['pre_hit_rate'])}（{sp['pre_n']} 事件）"
              f" vs 窗外 {_r(sp['base_hit_rate'])}｜比值 {_s(sp['ratio'])}")
    report["p3"] = rows_p3

    # ── 總結 ────────────────────────────────────────────────────
    with_faults = [r for r in rows_p3 if r["faults"]]
    good = [r for r in with_faults
            if r["peer"]["ratio"] is not None and r["peer"]["ratio"] >= 2.0
            and (r["peer"]["pre_hit_rate"] or 0) >= 0.2]
    # 飽和警示：基準命中率遠高於名目值 → 指標本身在「一直亮」，其結論不可用
    sat = [r for r in rows_p3 if (r["peer"]["base_hit_rate"] or 0) > 0.1]
    if sat:
        print(f"  ★ 飽和警示：{len(sat)} 台機台的同儕對照基準命中率 > 10%"
              f"（{'、'.join(r['tool'] for r in sat)}）→ 指標過度觸發，比值不可解讀 ★")
    controls = [r for r in rows_p3 if not r["faults"]]
    for c in controls:
        print(f"  （負控制 {c['tool']}：窗內命中率 "
              f"{c['pre_hit_rate'] if c['pre_hit_rate'] is None else round(c['pre_hit_rate'], 4)}"
              f" — 應與窗外相近，否則代表尺度或窗定義有問題）")
    print("\n[總結]")
    if frac < 0.1:
        print("  ★ P2 不成立：漂移主要是共同老化 → 與個別機台劣化的關聯很弱 ★")
    if not with_faults:
        print("  ★ P3 無法評估：這批機台沒有故障標籤 ★")
    elif good:
        print(f"  ✓ P3 有訊號：{len(good)} 台機台的故障窗漂移明顯高於平時"
              f"（{', '.join(r['tool'] for r in good)}）→ 值得進行正式的 M5-3′")
    else:
        print("  ✗ P3 不成立：故障窗的漂移強度與平時沒有明顯差別 → "
              "漂移指紋沒有預測力（誠實的負面結論）")
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  報告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
