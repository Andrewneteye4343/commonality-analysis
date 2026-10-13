#!/usr/bin/env python3
"""M5-1：單變量 FDC 管制圖 —— 在真實感測特徵上跑 Shewhart / EWMA / CUSUM。

做什麼
────────────────────────────────────────────────────────────
對每個「機台 × recipe_step × 感測器 × 特徵」建立**時間序列**（依事件的 in_ts 排序），
前 `--baseline-frac` 比例的點當**基準期**（只用它估中心與 σ），其餘點才是監控期。

    Shewhart X-MR : 3σ 管制限（看單點跳動）
    EWMA          : λ 權重 + L 倍限（看緩慢漂移）
    CUSUM         : k 參考值 + h 決策區間（看微小但持續的偏移）

三個方法的參數都由 **ARL0 目標**反推（`spc.design_*`），讓三張圖的**誤報率可比**——
否則「哪個方法先抓到」只是參數沒調好的假象。

為何要用「基準期」而不是全期統計
────────────────────────────────────────────────────────────
1. **不可用未來資訊**：管制限必須只用過去資料（與 PHM 官方、時序預測同一原則）
2. 全期統計會把真正的異常算進「正常範圍」→ 管制限被撐寬 → 抓不到
3. 基準期若有異常，`--sigma-method mad` 用穩健估法（中位數 ± MAD）抵抗離群值

輸出
────────────────────────────────────────────────────────────
    mart.fdc_baselines  ：基準期統計量與管制限（可重現「當時的標準」）
    mart.fdc_excursions ：觸發警報的（事件 × 感測器 × 特徵 × 方法）
    reports/fdc/*       ：匯總報表與偵測率／領先時間評估

用法
────────────────────────────────────────────────────────────
    docker compose run --rm etl python -m analysis.run_fdc --mode univariate \\
        --tools "01,02,03,04,06" --sensors FLOWCOOLPRESSURE,IONGAUGEPRESSURE \\
        --features mean_val,std_val --arl0 370.4

    # 不寫資料庫（本機驗證用）
    python -m analysis.run_fdc --series-csv <檔> --labels-csv <檔> --baseline-frac 0.3 --no-db
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

# 容器佈局：./etl 掛在 /work、./analysis 掛在 /work/analysis。
# 直接跑 `python analysis/run_fdc.py` 時 sys.path[0] = /work/analysis，
# 所以 script_version（在 /work）需要「<父目錄>」或「<父目錄>/etl」在路徑上。
_HERE = Path(__file__).resolve().parent
_PARENT = _HERE.parent
for _p in (_HERE, _PARENT / "etl", _PARENT):
    if _p.is_dir():
        sys.path.insert(0, str(_p))

import spc  # noqa: E402
from script_version import add_version_arg  # noqa: E402

DEFAULT_SENSORS = ("FLOWCOOLPRESSURE", "FLOWCOOLFLOWRATE", "IONGAUGEPRESSURE",
                   "ETCHBEAMVOLTAGE", "ETCHBEAMCURRENT", "ETCHSUPPRESSORVOLTAGE",
                   "ETCHSUPPRESSORCURRENT", "ETCHSOURCEUSAGE", "ACTUALSTEPDURATION",
                   "ROTATIONSPEED", "FIXTURETILTANGLE")
DEFAULT_FEATURES = ("mean_val", "std_val", "range_val", "slope", "missing_rate")

# 特徵的「方向語意」：這些特徵偏高才可疑（missing_rate 也算高），供報告排序用
FEATURE_DIRECTION = {"mean_val": "both", "std_val": "high", "range_val": "high",
                     "slope": "both", "missing_rate": "high"}


def parse_csv_list(spec: str) -> tuple[str, ...]:
    """逗號／空白／分號分隔的清單（PowerShell 會把 01,02 轉成整數 → 補回前導零）"""
    if not spec:
        return ()
    out = []
    for part in spec.replace(";", ",").replace(" ", ",").split(","):
        p = part.strip()
        if not p:
            continue
        out.append(p.zfill(2) if p.isdigit() and len(p) <= 2 else p)
    return tuple(out)


# ─────────────────────────────────────────────────────────────
# 三個管制圖（統一介面，方便比較）
# ─────────────────────────────────────────────────────────────

def _sigma_by_scope(resid: list, n_base: int, sigma_method: str,
                    scope: str, stride: int) -> list:
    """σ 的估計範圍。

    **真實資料實測（03M01）**：基期只取前 30%，若那一段剛好很安靜，
    σ 會被壓到接近 0（實測有序列 σ=0.0004，之後每個點都在響）→ 警報率中位數 28.7%。
    改用 expanding（到 t 為止的資料，**無資料洩漏**）→ 8.3%；series（整條序列，回顧式）→ 6.9%。
    """
    n = len(resid)
    if scope == "baseline":
        return [spc.baseline_stats(resid[:n_base], sigma_method)["sigma"]] * n
    if scope == "series":
        return [spc.baseline_stats(resid, sigma_method)["sigma"]] * n
    if scope != "expanding":
        raise ValueError(f"未知的 sigma_scope：{scope}")
    # expanding：每隔 stride 點重算一次（σ 變化很慢，不需要逐點重算；逐點是 O(n²)）
    sig = None
    out: list = []
    step = max(int(stride), 1)
    for t in range(n):
        if t < 20:                     # 樣本太少，σ 不可用
            out.append(None)
            continue
        if sig is None or (t % step == 0):
            sig = spc.baseline_stats(resid[:t], sigma_method)["sigma"]
        out.append(sig)
    return out


def build_charts(values: list[float], base_frac: float, arl0: float,
                 sigma_method: str, lambda_ewma: float, k_cusum: float,
                 mode: str = "fixed", window: int = 200,
                 min_sigma_rel: float = 1e-9,
                 methods: tuple = ("shewhart", "ewma", "cusum"),
                 sigma_scope: str = "baseline",
                 sigma_stride: int = 50) -> tuple[dict, dict]:
    """回傳 (基準期資訊, {method: 圖})。

    兩種基準線模式（真實資料實測後新增，理由見 `--baseline-mode` 的說明）：

        fixed   前 `base_frac` 比例當基準期，整段共用同一組中心與 σ（原始做法）
        rolling 每個時間點用**前面 `window` 個點**算中心與 σ（處理機台老化漂移）

    作法：**先把數值標準化** z_t = (x_t − center_t) / σ_t，再對 z 跑三張圖。
    fixed 模式因為 center/σ 是常數，數學上等價於直接對原始值畫圖；
    rolling 模式則讓三張圖都在「相對於近期水準」的尺度上比較。

    退化序列（σ ≈ 0，例如整段都是同一個值）→ 回傳 `skip_reason`，呼叫端要列出來（不可安靜跳過）。
    """
    n = len(values)
    if mode == "trend":
        # 線性老化：用基準期擬合趨勢線並外推當中心（只用過去資料）
        n_base = max(5, int(round(n * base_frac)))
        if n_base >= n:
            n_base = max(1, n - 1)
        a_t, b_t = spc.linear_trend(values, n_fit=n_base)
        centers = [a_t + b_t * t_ for t_ in range(n)]
        resid_all = [values[i] - centers[i] for i in range(n)]   # 全序列殘差（給 sigma_scope 用）
        resid_base = resid_all[:n_base]
        bstat = spc.baseline_stats(resid_base, sigma_method)
        judged_from = n_base
        base_center = 0.0
        skipped = 0
        spread = max(resid_base) - min(resid_base) if resid_base else 0.0
        sigmas = _sigma_by_scope(resid_all, n_base, sigma_method, sigma_scope, sigma_stride)
        sigma_global = sigmas[n - 1] if sigmas else bstat["sigma"]
    elif mode == "rolling":
        centers, sigmas = spc.rolling_stats(values, window)
        n_base = min(window, n)
        judged_from = window
        base_center = sigma_global = None
        skipped = sum(1 for s_ in sigmas if s_ is None)
        spread = float("inf")
    else:
        n_base = max(5, int(round(n * base_frac)))
        if n_base >= n:
            n_base = max(1, n - 1)
        base = spc.baseline_stats(values[:n_base], sigma_method)
        centers = [base["center"]] * n
        judged_from = 0
        base_center, sigma_global = base["center"], base["sigma"]
        sigmas = _sigma_by_scope(values, n_base, sigma_method, sigma_scope, sigma_stride)
        skipped = 0
        a_t, b_t = base["center"], 0.0
        bv = values[:n_base]
        spread = (max(bv) - min(bv)) if bv else 0.0

    # 退化檢查：σ 相對中心太小 → 任何微小變動都會被判異常 → 直接跳過並回報
    # 退化判斷用「基準期極差」而不是 σ：baseline_stats 會給 σ 一個 1e-9 級的下限，
    # 光是比 σ 會漏掉「整段都是同一個值」這種序列（實測被這個下限掩蓋過）。
    ref = max(abs(base_center or 0.0), 1.0) if mode != "trend" else 1.0
    degenerate = spread <= min_sigma_rel * ref
    if mode in ("fixed", "trend") and (degenerate or sigma_global is None):
        return ({"n_base": n_base, "center": base_center, "sigma": sigma_global,
                 "sigma_method": sigma_method, "mode": mode, "slope": locals().get("b_t", 0.0),
                 "skip_reason": f"基準期幾乎是常數（極差={spread:.3g} ≤ "
                                f"{min_sigma_rel * ref:.3g}）→ σ 不可用"},
                {})

    # 標準化（σ 為 0 或 None 的點不做判定）
    z: list = [None] * n
    n_undecided = 0
    for i, x in enumerate(values):
        c, sg = centers[i], sigmas[i]
        if c is None or sg is None or sg <= min_sigma_rel * (abs(c) if c else 1.0):
            n_undecided += 1
            continue
        z[i] = (x - c) / sg

    unit = {"center": 0.0, "sigma": 1.0}

    def _mask(flags):
        """未判定的點（暖身期／σ 不可用）一律不算警報"""
        return [bool(f) and z[i] is not None for i, f in enumerate(flags)]

    zc = [0.0 if v is None else v for v in z]
    charts: dict = {}
    if "shewhart" in methods:
        k_sw = spc.design_shewhart(arl0)
        sw = spc.shewhart_chart(zc, base=unit, k=k_sw)
        charts["shewhart"] = {
            "center": 0.0, "sigma": 1.0, "param_1": k_sw, "param_2": None,
            "ucl": sw["ucl"], "lcl": sw["lcl"], "stat": sw["z"],
            "alarm": _mask(sw["alarm"]), "arl0": spc.arl_shewhart(k_sw)}
    if "ewma" in methods:
        L_ew = spc.design_ewma(lambda_ewma, arl0)
        ew = spc.ewma_chart(zc, lam=lambda_ewma, L=L_ew, base=unit)
        charts["ewma"] = {
            "center": 0.0, "sigma": 1.0, "param_1": lambda_ewma, "param_2": L_ew,
            "ucl": ew["ucl"], "lcl": ew["lcl"], "stat": ew["stat"],
            "alarm": _mask(ew["alarm"]), "arl0": spc.arl_ewma(lambda_ewma, L_ew)}
    if "cusum" in methods:
        h_cu = spc.design_cusum(k_cusum, arl0 * 2)
        cu = spc.cusum_chart(zc, k=k_cusum, h=h_cu, base=unit)
        charts["cusum"] = {
            "center": 0.0, "sigma": 1.0, "param_1": k_cusum, "param_2": h_cu,
            "ucl": [h_cu] * n, "lcl": [-h_cu] * n,
            "stat": [(cu["stat_pos"][i] if cu["stat_pos"][i] >= cu["stat_neg"][i]
                      else -cu["stat_neg"][i]) for i in range(n)],
            "alarm": _mask(cu["alarm"]), "arl0": spc.arl_cusum(k_cusum, h_cu, sides=2)}
    info = {"n_base": n_base, "center": base_center, "sigma": sigma_global,
            "sigma_method": sigma_method, "mode": mode, "window": window,
            "sigma_scope": sigma_scope,
            "slope": locals().get("b_t", 0.0),
            "n_undecided": n_undecided, "judged_from": judged_from,
            "skip_reason": None, "n_warmup_skipped": skipped,
            "z": z}
    return info, charts


# ─────────────────────────────────────────────────────────────
# 評估：偵測率、領先時間、負控制
# ─────────────────────────────────────────────────────────────

def evaluate(alert_events: set, labelled: dict, fault_times: dict,
             tool_of_event: dict, ts_of_event: dict) -> dict:
    """計算偵測率、**領先時間**與負控制。

    - `labelled`：{event_id: 是否落在故障窗內}
    - `fault_times`：{tool_id: [故障時刻（原始時間軸）…]}（`mart.fault_events.fault_ts`）
    - `ts_of_event`：{event_id: in_ts}（同一條原始時間軸，才能算領先時間）
      ⚠ 不可用 event_id 當時間 proxy——它只在同機台內單調，跨機台不可比
    - 領先時間 = 故障時刻 − 該機台在**故障窗內**最早被觸發的警報時刻（小時）
    """
    in_win = [e for e, v in labelled.items() if v]
    out_win = [e for e, v in labelled.items() if not v]
    det = [e for e in in_win if e in alert_events]
    res = {
        "n_events_labelled": len(labelled),
        "n_events_in_window": len(in_win),
        "n_events_out_window": len(out_win),
        "n_alerts": len(alert_events),
        "detection_rate_in_window": (len(det) / len(in_win)) if in_win else None,
        "alert_rate_out_window": (len([e for e in out_win if e in alert_events]) / len(out_win))
        if out_win else None,
    }
    # 逐機台警報率（**分開看才誠實**：窗外警報包含「窗前的早期偵測」＝領先時間的一部分，
    # 把它算成誤報會低估方法的價值；真正乾淨的誤報率要用「完全沒有故障的機台」當負控制）
    by_tool_alert: dict = {}
    tools_all = sorted({t for t in tool_of_event.values()} | set(fault_times))
    for tool in tools_all:
        ev = [e for e in labelled if tool_of_event.get(e) == tool]
        al = [e for e in ev if e in alert_events]
        by_tool_alert[tool] = {
            "n_events": len(ev), "n_alerts": len(al),
            "alert_rate": (len(al) / len(ev)) if ev else None,
            "n_faults": len(fault_times.get(tool, [])),
        }
    res["by_tool_alert_rate"] = by_tool_alert
    control = {t: v for t, v in by_tool_alert.items() if v["n_faults"] == 0 and v["n_events"]}
    res["control_tools"] = control
    if control:
        tot_ev = sum(v["n_events"] for v in control.values())
        tot_al = sum(v["n_alerts"] for v in control.values())
        res["false_alarm_rate_control"] = tot_al / tot_ev

    # 逐機台 × 逐故障：往前找最近一次警報 → 領先時間（小時）
    per_fault = []
    for tool, times in sorted(fault_times.items()):
        alerts_tool = {e: ts_of_event.get(e) for e in alert_events
                       if tool_of_event.get(e) == tool and ts_of_event.get(e) is not None}
        for ft in sorted(times):
            prior = [ts for ts in alerts_tool.values() if ts <= ft]
            lead_h = (ft - max(prior)) / 3600.0 if prior else None
            # ⚠ 必須用 bool(prior)：空 list 的 `is not None` 是 True（實際踩到）
            per_fault.append({"tool": tool, "fault_ts": ft,
                              "n_alerts_before": len(prior),
                              "lead_hours": lead_h,
                              "detected": bool(prior)})
    if per_fault:
        det_faults = [f for f in per_fault if f["detected"]]
        leads = [f["lead_hours"] for f in det_faults if f["lead_hours"] is not None]
        res["n_faults"] = len(per_fault)
        res["fault_detection_rate"] = len(det_faults) / len(per_fault)
        res["lead_hours_mean"] = (sum(leads) / len(leads)) if leads else None
        res["lead_hours_min"] = min(leads) if leads else None
        res["lead_hours_max"] = max(leads) if leads else None
    res["per_fault"] = per_fault
    return res


# ─────────────────────────────────────────────────────────────
# 資料載入（DB 或 CSV）
# ─────────────────────────────────────────────────────────────

def _norm_code(code: str) -> str:
    """使用者代號正規化：03_M01 → 03M01（與 etl/load_fault_labels.normalize_tool 同規則）"""
    return (code or "").replace("_", "").replace("-", "").strip().upper()


def resolve_tool_ids(cur, requested: tuple) -> tuple[list, list]:
    """把使用者給的代號解析成資料庫裡的 tool_id。

    **實際踩到的坑**：`--tools "03"` 傳的是 2 位數檔名代號，但 `stg.fact_process_event.tool_id`
    是 `03M01` → `tool_id = ANY('{03}')` 永遠不匹配，查詢「成功」地回傳 0 列，
    整個分析安靜地什麼都沒做（掃描 0 條序列、寫入 0 列）。

    規則：完全相符（03M01）優先；否則視為前綴代號（03 → 03M01、03_M01 → 03M01）。
    回傳 (解析到的清單, 解析不到的代號清單)。
    """
    cur.execute("SELECT DISTINCT tool_id FROM stg.fact_process_event ORDER BY 1")
    available = [r[0] for r in cur.fetchall()]
    by_norm = {_norm_code(t): t for t in available}
    resolved, missing = [], []
    for code in requested:
        key = _norm_code(code)
        if not key:
            continue
        if key in by_norm:
            resolved.append(by_norm[key])
            continue
        pref = sorted(t for k, t in by_norm.items() if k.startswith(key))
        if pref:
            resolved.extend(pref)
        else:
            missing.append(code)
    return list(dict.fromkeys(resolved)), missing


def resolve_sensors(cur, requested: tuple) -> tuple[list, list]:
    """感測器名稱先對一次實際有的清單（拼錯 → 靜默 0 筆）"""
    cur.execute("SELECT DISTINCT sensor FROM stg.fact_sensor_feature ORDER BY 1")
    available = [r[0] for r in cur.fetchall()]
    by_up = {str(s_).upper(): s_ for s_ in available}
    resolved, missing = [], []
    for name in requested:
        if name.upper() in by_up:
            resolved.append(by_up[name.upper()])
        else:
            missing.append(name)
    return list(dict.fromkeys(resolved)), missing


def preflight(tools: tuple, sensors: tuple, features: tuple) -> tuple[list, list]:
    """開跑前的守門：確認機台代號、感測器名稱、特徵欄位都存在。

    回傳 (解析後的機台清單, 解析後的感測器清單)；無法解析時直接 SystemExit(2)。
    """
    import psycopg
    conn = psycopg.connect(host=os.environ.get("PGHOST", "db"),
                           port=int(os.environ.get("PGPORT", 5432)),
                           dbname=os.environ.get("PGDATABASE", "commonality"),
                           user=os.environ["PGUSER"],
                           password=os.environ.get("PGPASSWORD", ""))
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('stg.fact_sensor_feature'), "
                    "to_regclass('stg.fact_process_event')")
        row = cur.fetchone()
        if not row or not row[0] or not row[1]:
            print("[錯誤] 找不到 stg.fact_sensor_feature 或 stg.fact_process_event。"
                  "請先跑 etl/build_all_tools.py 載入 M2/M3 的事實表。")
            raise SystemExit(2)

        t_ids, t_missing = resolve_tool_ids(cur, tools)
        s_ids, s_missing = resolve_sensors(cur, sensors)

        if tools:
            print(f"[代號] --tools {list(tools)} → {t_ids or '（無）'}")
        if t_missing:
            cur.execute("SELECT DISTINCT tool_id FROM stg.fact_process_event ORDER BY 1")
            avail = [r[0] for r in cur.fetchall()]
            print(f"[警告] 解析不到的機台代號：{t_missing}｜資料庫現有：{avail}")
        if not t_ids:
            print(f"[錯誤] 指定的機台代號一個都對不上（--tools {list(tools)}）→ 中止。"
                  f"資料庫現有機台：{avail if t_missing else '（空）'}"
                  f"；若資料庫是空的，請先跑 build_all_tools.py。")
            raise SystemExit(2)

        if s_missing:
            cur.execute("SELECT DISTINCT sensor FROM stg.fact_sensor_feature ORDER BY 1")
            avail_s = [r[0] for r in cur.fetchall()]
            print(f"[警告] 解析不到的感測器：{s_missing}｜資料庫現有 {len(avail_s)} 個："
                  f"{avail_s[:8]}{' …' if len(avail_s) > 8 else ''}")
        if not s_ids:
            print(f"[錯誤] 指定的感測器一個都對不上（--sensors）→ 中止。")
            raise SystemExit(2)
        print(f"[感測器] 解析到 {len(s_ids)} 個：{s_ids[:6]}{' …' if len(s_ids) > 6 else ''}")
    finally:
        conn.close()
    return t_ids, s_ids


def iter_db_series(tools: tuple, sensors: tuple, features: tuple, fault_type: str):
    """串流讀取（機台 × recipe_step × 感測器）的感測特徵，依時間排序。

    一次只 yield 一個 (tool, recipe_step, sensor) 的序列，避免把 350 萬列全放進記憶體。
    """
    import psycopg
    conn = psycopg.connect(host=os.environ.get("PGHOST", "db"),
                           port=int(os.environ.get("PGPORT", 5432)),
                           dbname=os.environ.get("PGDATABASE", "commonality"),
                           user=os.environ["PGUSER"],
                           password=os.environ.get("PGPASSWORD", ""))
    # ⚠ missing_rate 是 numeric(6,4) → psycopg 回傳 Decimal；算式裡混到 Decimal 會 TypeError。
    #    一律在 SQL 轉 double precision（Python 端還有第二層 float() 防護）。
    cols = ", ".join(f"f.{f}::double precision AS {f}" for f in features)
    sql = f"""
        SELECT e.tool_id, e.recipe_step, e.event_id, e.in_ts::double precision, e.lot_id, f.sensor, {cols}
        FROM stg.fact_sensor_feature f
        JOIN stg.fact_process_event e ON e.event_id = f.event_id
        WHERE (%s = ARRAY[]::text[] OR e.tool_id = ANY(%s))
          AND (%s = ARRAY[]::text[] OR f.sensor = ANY(%s))
        ORDER BY e.tool_id, e.recipe_step, f.sensor, e.in_ts, e.event_id
    """
    with conn, conn.cursor(name="fdc_stream") as cur:      # 具名游標 = 伺服器端串流
        cur.itersize = 20000
        cur.execute(sql, (list(tools), list(tools), list(sensors), list(sensors)))
        key_prev = None
        buf: list[dict] = []
        for row in cur:
            key = (row[0], row[1], row[5])          # tool, recipe_step, sensor
            if key_prev is not None and key != key_prev:
                yield key_prev, buf
                buf = []
            key_prev = key
            # 第二層防護：任何 numeric→Decimal 都要在這裡變成 float，否則算術會炸
            def _f(v):
                return None if v is None else float(v)

            buf.append({"event_id": row[2], "in_ts": _f(row[3]), "lot_id": row[4],
                        **{f: _f(row[6 + i]) for i, f in enumerate(features)}})
        if key_prev is not None:
            yield key_prev, buf
    conn.close()


def iter_csv_series(path: Path, features: tuple):
    """CSV 版（本機驗證用）：欄位 tool_id, recipe_step, event_id, in_ts, sensor, <features>

    ⚠ **必須依 (tool_id, recipe_step, sensor, in_ts) 排序**（DB 版有 ORDER BY，CSV 沒有）。
    若同一個 key 在 yield 之後又出現，代表輸入沒排序 → 直接中止並說明，
    否則每個 key 只會拿到零散幾點、全部因點數不足被跳過（**靜默地什麼都沒做**，實際踩過）。
    """
    buf: list[dict] = []
    key_prev = None
    seen: set = set()
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            key = (row["tool_id"], row["recipe_step"], row["sensor"])
            if key_prev is not None and key != key_prev:
                seen.add(key_prev)
                yield key_prev, buf
                buf = []
            elif key in seen:
                raise ValueError(
                    f"輸入未依 (tool_id, recipe_step, sensor, in_ts) 排序："
                    f"{key} 在 yield 之後又出現。請先排序："
                    f"Import-Csv x.csv | Sort-Object tool_id, recipe_step, sensor, in_ts")
            key_prev = key
            buf.append({"event_id": int(row["event_id"]), "in_ts": row["in_ts"],
                        **{f: (float(row[f]) if row.get(f) not in (None, "", "None") else None)
                           for f in features}})
    if key_prev is not None:
        yield key_prev, buf


def load_labels(conn_or_path, scope: str = "ANY", window_hours: int = 24):
    """事件是否落在故障窗內。

    實際 schema（stg 的 fact 表與 mart 的標籤表逐欄比對過）：
        mart.event_labels(event_id, tool_id, fault_type, window_hours, hours_to_fault,
                          in_window, n_faults_ahead)
    所以篩選條件是 `fault_type`（不是 fault_scope ✗）＋ `window_hours`。
    """
    out = {}
    if isinstance(conn_or_path, Path):
        for row in csv.DictReader(conn_or_path.open(encoding="utf-8", newline="")):
            if row.get("fault_type", scope) == scope and \
               int(row.get("window_hours", window_hours)) == window_hours:
                out[int(row["event_id"])] = row["in_window"].strip().lower() in ("1", "true", "t")
        return out
    cur = conn_or_path.cursor()
    cur.execute("SELECT event_id, in_window FROM mart.event_labels "
                "WHERE fault_type = %s AND window_hours = %s", (scope, window_hours))
    for eid, inw in cur.fetchall():
        out[int(eid)] = bool(inw)
    return out


def load_fault_times(conn_or_path):
    """故障時刻（實際欄名是 `fault_ts`，不是 fault_epoch_s ✗）"""
    out: dict[str, list] = defaultdict(list)
    if isinstance(conn_or_path, Path):
        for row in csv.DictReader(conn_or_path.open(encoding="utf-8", newline="")):
            out[row["tool_id"]].append(float(row["fault_ts"]))
        return out
    cur = conn_or_path.cursor()
    cur.execute("SELECT tool_id, fault_ts FROM mart.fault_events")
    for tool, ts in cur.fetchall():
        out[tool].append(float(ts))
    return out


def write_db(rows_baseline: list[dict], rows_excursion: list[dict], run_id: str,
             features: tuple):
    import psycopg
    conn = psycopg.connect(host=os.environ.get("PGHOST", "db"),
                           port=int(os.environ.get("PGPORT", 5432)),
                           dbname=os.environ.get("PGDATABASE", "commonality"),
                           user=os.environ["PGUSER"],
                           password=os.environ.get("PGPASSWORD", ""))
    with conn:
        with conn.cursor() as cur:
            for tbl in ("mart.fdc_excursions", "mart.fdc_baselines"):
                cur.execute(f"DELETE FROM {tbl} WHERE run_id = %s", (run_id,))
            with cur.copy("COPY mart.fdc_baselines (run_id, tool_id, recipe_step, sensor, "
                          "feature, method, n_base, center, sigma, sigma_method, param_1, "
                          "param_2, arl0_target, ucl, lcl, baseline_mode, slope) FROM STDIN") as cp:
                for r in rows_baseline:
                    cp.write_row([r["run_id"], r["tool_id"], r["recipe_step"], r["sensor"],
                                  r["feature"], r["method"], r["n_base"], r["center"],
                                  r["sigma"], r["sigma_method"], r["param_1"], r["param_2"],
                                  r["arl0_target"], r["ucl"], r["lcl"],
                                  r.get("baseline_mode"), r.get("slope")])
            with cur.copy("COPY mart.fdc_excursions (run_id, event_id, tool_id, recipe_step, "
                          "lot_id, in_ts, sensor, feature, method, seq_no, is_baseline, "
                          "z_value, stat_value, limit_ucl, limit_lcl, direction) FROM STDIN") as cp:
                for r in rows_excursion:
                    cp.write_row([r["run_id"], r["event_id"], r["tool_id"], r["recipe_step"],
                                  r.get("lot_id"), r.get("in_ts"), r["sensor"], r["feature"],
                                  r["method"], r["seq_no"], r["is_baseline"], r.get("z_value"),
                                  r.get("stat_value"), r.get("limit_ucl"), r.get("limit_lcl"),
                                  r.get("direction")])
    conn.close()
    return len(rows_baseline), len(rows_excursion)


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="M5-1 單變量 FDC 管制圖")
    ap.add_argument("--mode", default="univariate", choices=["univariate"])
    ap.add_argument("--tools", default="")
    ap.add_argument("--sensors", default=",".join(DEFAULT_SENSORS))
    ap.add_argument("--features", default="mean_val,std_val,range_val,slope,missing_rate")
    ap.add_argument("--baseline-frac", type=float, default=0.3)
    ap.add_argument("--sigma-method", default="mad", choices=["mr", "std", "mad"])
    ap.add_argument("--baseline-mode", default="fixed", choices=["fixed", "rolling", "trend"],
                    help="fixed＝前 --baseline-frac 比例當固定基準期（平穩製程）；"
                         "trend＝用基準期擬合線性趨勢並外推（**機台老化漂移建議用這個**）；"
                         "rolling＝每個時間點用前面 --baseline-window 個點（窗mean 會落後漂移，"
                         "對 EWMA/CUSUM 幾乎無效，保留作為對照）")
    ap.add_argument("--baseline-window", type=int, default=200,
                    help="rolling 模式的視窗點數（也是暖身期長度）")
    ap.add_argument("--min-sigma-rel", type=float, default=1e-9,
                    help="σ 相對中心的下限；低於此值的序列視為退化（常數）→ 跳過並列出")
    ap.add_argument("--arl0", type=float, default=370.4)
    ap.add_argument("--lambda-ewma", type=float, default=0.1)
    ap.add_argument("--k-cusum", type=float, default=0.5)
    ap.add_argument("--fault-scope", default="ANY", help="故障型別（ANY = 任一型別）")
    ap.add_argument("--window-hours", type=int, default=24, help="退化窗長度（與 M3 一致）")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--series-csv", default="")
    ap.add_argument("--labels-csv", default="")
    ap.add_argument("--faults-csv", default="")
    ap.add_argument("--out", default="/work/reports/fdc")
    ap.add_argument("--max-series", type=int, default=0, help="只跑前 N 條序列（快速測試）")
    ap.add_argument("--methods", default="",
                    help="要跑哪些管制圖（逗號分隔：shewhart,ewma,cusum）。"
                         "留空＝自動：fixed/rolling 用三種全部；trend 只用 shewhart"
                         "（實測 trend 下 EWMA/CUSUM 會把「基期斜率誤差」當成持續偏移，"
                         "負控制機台累積警報由 24 升到 279 → 見 M5_CHECKLIST §5.1）")
    ap.add_argument("--sigma-scope", default="",
                    choices=["", "baseline", "expanding", "series"],
                    help="σ 從哪裡估：baseline＝只用基期（預設於 fixed 模式）；"
                         "expanding＝到 t 為止的資料（**trend 模式的預設**，無資料洩漏）；"
                         "series＝整條序列（回顧式分析，用到未來資訊，報告要標明）")
    ap.add_argument("--sigma-stride", type=int, default=50,
                    help="expanding 模式下每隔幾個點重算 σ（σ 變化慢，省時間）")
    ap.add_argument("--min-points", type=int, default=50,
                    help="序列點數低於此值就跳過並列出（太少無法建立可信的管制線）")
    ap.add_argument("--max-series-alarm-rate", type=float, default=0.20,
                    help="單一序列的監控期警報率超過此比例就標為「不在管制內」並【排除於共識之外】"
                         "（會在報表明列）。理由：警報率 100%% 的管制圖沒有鑑別力，"
                         "它的警報會污染事件層的共識判斷。設 1 = 不過濾")
    ap.add_argument("--max-alarm-rate", type=float, default=0.05,
                    help="監控期警報率超過此比例就大聲警告「管制圖不在管制內」（預設 5%%）")
    ap.add_argument("--min-baseline", type=int, default=100,
                    help="trend 模式的基期點數下限；低於此值會警告（斜率估不準，外推誤差大）")
    ap.add_argument("--list-top", type=int, default=8, help="列出警報最多的前 N 個來源")
    ap.add_argument("--min-alerts", type=int, default=2,
                    help="共識規則：一個事件要有 ≥N 個（感測器 × 特徵）組合同意才算異常"
                         "（預設 2；設 1 = 任一即可，設 0 = 不做共識）")
    ap.add_argument("--bonferroni", action="store_true",
                    help="把 ARL0 目標乘以同時平行的圖數（每個事件有 感測器×特徵×3 張圖）"
                         "——不調整的話，30~165 張圖並行會讓事件的誤報率遠高於單張圖的 0.27%%")
    ap.add_argument("--no-db", action="store_true")
    add_version_arg(ap)
    args = ap.parse_args()

    tools = parse_csv_list(args.tools)
    sensors = parse_csv_list(args.sensors)
    features = tuple(f.strip() for f in args.features.split(",") if f.strip())
    if args.methods:
        methods = tuple(m.strip().lower() for m in args.methods.split(",") if m.strip())
        unknown = [m for m in methods if m not in ("shewhart", "ewma", "cusum")]
        if unknown:
            raise SystemExit(f"[錯誤] 未知的管制圖：{unknown}（可用：shewhart, ewma, cusum）")
        methods_auto = False
    elif args.baseline_mode == "trend":
        methods = ("shewhart",)
        methods_auto = True
    else:
        methods = ("shewhart", "ewma", "cusum")
        methods_auto = False
    run_id = args.run_id or f"M5_univ_arl{int(args.arl0)}_b{args.baseline_frac}"

    print(f"[設定] 管制圖＝單變量｜ARL0 目標 {args.arl0}｜基準線模式 {args.baseline_mode}"
          f"（比例 {args.baseline_frac}"
          f"{'，視窗 ' + str(args.baseline_window) if args.baseline_mode == 'rolling' else ''}）"
          f"｜σ 估法 {args.sigma_method}｜run_id {run_id}")
    print(f"       感測器 {len(sensors)} 個｜特徵 {list(features)}")

    # σ 範圍：trend 模式預設用 expanding（實測基期 σ 在真實資料會崩）
    if args.sigma_scope:
        sigma_scope = args.sigma_scope
        scope_auto = False
    else:
        sigma_scope = "expanding" if args.baseline_mode == "trend" else "baseline"
        scope_auto = args.baseline_mode == "trend"

    n_charts_per_event = len(sensors) * len(features) * len(methods)
    arl0_chart = args.arl0 * (n_charts_per_event if args.bonferroni else 1)
    if args.bonferroni:
        print(f"       平行圖數 {n_charts_per_event}｜Bonferroni 調整後的單圖 ARL0 目標 = "
              f"{arl0_chart:,.0f}")
    else:
        print(f"       σ 範圍 {sigma_scope}（{args.sigma_method} 估法）"
          + ("　← trend 模式預設（基期 σ 在真實資料會被壓到接近 0）" if scope_auto else ""))
    print(f"       管制圖 {list(methods)}｜平行圖數 {n_charts_per_event}（每事件）→ "
              f"事件層誤報率會遠高於單圖 0.27%；必要時加 --bonferroni 或提高 --min-alerts")
    if methods_auto:
        print("       （trend 模式自動只用 shewhart：EWMA/CUSUM 會把基期斜率誤差當成持續偏移，"
              "要用請加 --methods shewhart,ewma,cusum）")

    rows_baseline: list[dict] = []
    rows_excursion: list[dict] = []
    alert_events: set = set()
    pairs_per_event: dict = defaultdict(set)      # event_id → {(sensor, feature)…}
    tool_of_event: dict = {}
    ts_by_event: dict = {}
    n_series = n_points = 0
    base_alarm = out_alarm = 0
    base_points = out_points = 0
    skipped_series: list = []          # 退化的（機台, 步驟, 感測器, 特徵）→ 必須列出
    short_baselines: list = []         # trend 模式基期太短的（斜率估不準）→ 必須列出
    few_point_series: list = []        # 點數太少的序列（無可信管制線）→ 必須列出
    out_of_control: list = []          # 警報率離譜（不在管制內）的序列 → 排除於共識並明列
    alarm_by_source: dict = defaultdict(int)   # (感測器, 特徵, 管制圖) → 警報列數
    n_undecided = 0

    consensus_events: set = set()
    if args.series_csv:
        source = iter_csv_series(Path(args.series_csv), features)
    else:
        # 開跑前先解析代號：`--tools 03` 不等於資料庫的 `03M01`，不先解析就會安靜地 0 筆
        tools, sensors = preflight(tools, sensors, features)
        source = iter_db_series(tools, sensors, features, args.fault_scope)
    for (tool, step, sensor), recs in source:
        n_series += 1
        if args.max_series and n_series > args.max_series:
            break
        # 逐機台警報率的分母需要「所有事件」，不是只有警報的事件
        for r in recs:
            tool_of_event.setdefault(r["event_id"], tool)
        for feat in features:
            vals = [r[feat] for r in recs]
            if sum(1 for v in vals if v is not None) < 20:
                continue                      # 點數太少不做（也不靜默：下面報表會列）

            # 逐特徵：**丟掉**缺失點（不是補值）；丟掉的點不參與管制圖判定
            clean = [(i, v) for i, v in enumerate(vals) if v is not None]
            if len(clean) < args.min_points:
                few_point_series.append((tool, step, sensor, feat, len(clean)))
                continue
            xs = [v for _, v in clean]
            idx = [i for i, _ in clean]
            # 先看這條序列的警報率是否離譜（警報率 100% 的管制圖沒有鑑別力）
            info, charts = build_charts(xs, args.baseline_frac, arl0_chart,
                                        args.sigma_method, args.lambda_ewma, args.k_cusum,
                                        mode=args.baseline_mode,
                                        window=args.baseline_window,
                                        min_sigma_rel=args.min_sigma_rel,
                                        methods=methods,
                                        sigma_scope=sigma_scope,
                                        sigma_stride=args.sigma_stride)
            if (args.baseline_mode == "trend"
                    and 0 < info.get("n_base", 0) < args.min_baseline):
                short_baselines.append((tool, step, sensor, feat, info["n_base"]))
            if info.get("skip_reason"):
                skipped_series.append((tool, step, sensor, feat, info["skip_reason"]))
                continue
            n_undecided += info.get("n_undecided", 0)
            _judged = sum(len([j for j, a in enumerate(ch["alarm"])
                               if j >= info["n_base"]]) for ch in charts.values()) or 1
            _alarms = sum(sum(1 for j, a in enumerate(ch["alarm"])
                              if a and j >= info["n_base"]) for ch in charts.values())
            series_rate = _alarms / _judged
            exclude_series = series_rate > args.max_series_alarm_rate
            if exclude_series:
                out_of_control.append((tool, step, sensor, feat, series_rate))
            n_points += len(xs)
            for method, ch in charts.items():
                rows_baseline.append({
                    "run_id": run_id, "tool_id": tool, "recipe_step": step,
                    "sensor": sensor, "feature": feat, "method": method,
                    "n_base": info["n_base"], "center": info["center"],
                    "sigma": info["sigma"], "sigma_method": info["sigma_method"],
                    "baseline_mode": info["mode"], "slope": info.get("slope"),
                    "param_1": ch["param_1"], "param_2": ch["param_2"],
                    "arl0_target": args.arl0,
                    "ucl": (ch["ucl"][-1] if isinstance(ch["ucl"], list) else ch["ucl"]),
                    "lcl": (ch["lcl"][-1] if isinstance(ch["lcl"], list) else ch["lcl"]),
                })
                for j, alarmed in enumerate(ch["alarm"]):
                    is_base = j < info["n_base"]
                    if is_base:
                        base_points += 1
                    else:
                        out_points += 1
                    if not alarmed:
                        continue
                    if is_base:
                        base_alarm += 1
                    else:
                        out_alarm += 1
                    alarm_by_source[(sensor, feat, method)] += 1
                    src = recs[idx[j]]
                    stat = ch["stat"][j]
                    ucl = ch["ucl"][j] if isinstance(ch["ucl"], list) else ch["ucl"]
                    lcl = ch["lcl"][j] if isinstance(ch["lcl"], list) else ch["lcl"]
                    rows_excursion.append({
                        "run_id": run_id, "event_id": src["event_id"], "tool_id": tool,
                        "recipe_step": step, "lot_id": src.get("lot_id"),
                        "in_ts": src["in_ts"],
                        "sensor": sensor, "feature": feat, "method": method,
                        "seq_no": j + 1, "is_baseline": is_base,
                        "z_value": stat if method == "shewhart" else None,
                        "stat_value": stat, "limit_ucl": ucl, "limit_lcl": lcl,
                        "direction": "high" if stat > 0 else "low",
                    })
                    if not is_base:
                        if exclude_series:
                            continue        # 這條序列不在管制內 → 不參與共識（但明列出來）
                        alert_events.add(src["event_id"])
                        pairs_per_event[src["event_id"]].add((sensor, feat))
                        tool_of_event[src["event_id"]] = tool
                        ts_by_event[src["event_id"]] = float(src["in_ts"])

    if n_series == 0:
        print("[錯誤] 掃描到 0 條序列 → 沒有任何東西被分析（不是「沒有異常」，是查詢沒撈到資料）。"
              "請檢查 --tools／--sensors 是否正確、以及 stg.fact_sensor_feature 是否有資料。")
        raise SystemExit(3)
    print(f"[掃描] 序列 {n_series} 條｜資料點 {n_points:,}｜"
          f"基準期觸發 {base_alarm}/{base_points}（{100 * base_alarm / max(base_points, 1):.3f}%）｜"
          f"監控期觸發 {out_alarm}/{out_points}（{100 * out_alarm / max(out_points, 1):.3f}%）")
    if out_of_control:
        print(f"[排除] 單一序列警報率 >{100 * args.max_series_alarm_rate:.0f}% 的序列 "
              f"{len(out_of_control)} 條（不在管制內 → 不參與共識；前 8 名）")
        for tool_, step_, sensor_, feat_, rate_ in sorted(
                out_of_control, key=lambda x: -x[4])[:8]:
            print(f"        {tool_}/{step_}/{sensor_}/{feat_}：警報率 {100 * rate_:.1f}%")

    if few_point_series:
        print(f"[跳過] 點數不足 {args.min_points} 的序列 {len(few_point_series)} 條"
              f"（列出前 5）")
        for tool_, step_, sensor_, feat_, cnt_ in few_point_series[:5]:
            print(f"        {tool_}/{step_}/{sensor_}/{feat_}：只有 {cnt_} 點")

    if short_baselines:
        print(f"[警告] trend 模式基期不足 {args.min_baseline} 點的序列 {len(short_baselines)} 條"
              f"（斜率外推誤差大；列出前 5）")
        for tool_, step_, sensor_, feat_, nb_ in short_baselines[:5]:
            print(f"        {tool_}/{step_}/{sensor_}/{feat_}：基期只有 {nb_} 點"
                  f"（建議 --baseline-frac 提高，或改用 fixed 模式）")
    if skipped_series:
        print(f"[跳過] σ 退化的序列 {len(skipped_series)} 條（整段近乎常數；列出前 10）")
        for tool_, step_, sensor_, feat_, why in skipped_series[:10]:
            print(f"        {tool_}/{step_}/{sensor_}/{feat_}：{why}")
    if n_undecided:
        print(f"[未判定] 暖身期或 σ 不可用的點 {n_undecided:,} 個（不計入警報）")
    if alarm_by_source:
        top = sorted(alarm_by_source.items(), key=lambda kv: -kv[1])[:args.list_top]
        print(f"[來源] 警報最多的（感測器 × 特徵 × 管制圖）前 {len(top)} 名：")
        for (sensor_, feat_, method_), cnt in top:
            print(f"        {sensor_}/{feat_}/{method_}：{cnt:,} 列"
                  f"（{100.0 * cnt / max(len(rows_excursion), 1):.1f}%）")
    mon_rate = out_alarm / max(out_points, 1)
    if mon_rate > args.max_alarm_rate:
        print(f"★★ [警告] 監控期警報率 {100 * mon_rate:.2f}% 遠高於名目值（單圖 0.27%）→ "
              f"**管制圖不在管制內**：現在的「偵測」沒有鑑別力，不要用這次的排名下結論。"
              f"請先檢查 σ 範圍（--sigma-scope）、基準線模式（--baseline-mode）"
              f"與警報來源排名（下面的 [來源] 段）。★")
    print(f"[警報] 觸發的（事件 × 感測器 × 特徵 × 方法）組合 {len(rows_excursion):,}｜"
          f"涉及事件 {len(alert_events):,}（原始，任一圖觸發即算）")
    # 共識規則：把「30~165 張圖裡只要有一張跳動」收斂成「多個獨立來源同時跳動」
    consensus_events = {e for e, ps in pairs_per_event.items() if len(ps) >= max(args.min_alerts, 1)}
    if args.min_alerts > 1:
        print(f"[共識] 需 ≥{args.min_alerts} 個（感測器 × 特徵）組合同意 → "
              f"事件數由 {len(alert_events):,} 收斂到 {len(consensus_events):,}"
              f"（{100 * len(consensus_events) / max(len(alert_events), 1):.1f}%）")

    # ── 評估 ──
    eval_res = {}
    try:
        if args.labels_csv or not args.no_db:
            # psycopg 只在真的要用資料庫時才 import（給了 --labels-csv 就不該需要它）
            need_db = not args.labels_csv
            conn = None
            if need_db:
                import psycopg
                conn = psycopg.connect(
                    host=os.environ.get("PGHOST", "db"), port=int(os.environ.get("PGPORT", 5432)),
                    dbname=os.environ.get("PGDATABASE", "commonality"),
                    user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""))
            labels = load_labels(Path(args.labels_csv) if args.labels_csv else conn,
                                 scope=args.fault_scope, window_hours=args.window_hours)
            faults = load_fault_times(Path(args.faults_csv) if args.faults_csv else conn)
            if conn:
                conn.close()
            ts_of_event = ts_by_event
            eval_res_raw = evaluate(alert_events, labels, faults, tool_of_event, ts_of_event)
            eval_res = evaluate(consensus_events, labels, faults, tool_of_event, ts_of_event)
            eval_res["raw_any_chart"] = {
                k: eval_res_raw[k] for k in
                ("n_alerts", "detection_rate_in_window", "alert_rate_out_window",
                 "false_alarm_rate_control", "by_tool_alert_rate")
                if k in eval_res_raw}
            eval_res["min_alerts"] = args.min_alerts
            def _fmt(x, nd=4):
                return "—" if x is None else f"{x:.{nd}f}"

            rates = {k: _fmt(v["alert_rate"], 3) for k, v in eval_res["by_tool_alert_rate"].items()}
            print(f"[評估] 故障窗內事件 {eval_res['n_events_in_window']:,} 中偵測到 "
                  f"{_fmt(eval_res['detection_rate_in_window'])}｜逐機台警報率 {rates}")
            if eval_res.get("control_tools"):
                ct = eval_res["control_tools"]
                print(f"       負控制（零故障機台，{', '.join(ct)}）："
                      f"誤報率 = {_fmt(eval_res['false_alarm_rate_control'])}")
            if eval_res.get("n_faults"):
                print(f"       故障偵測率 {_fmt(eval_res['fault_detection_rate'], 3)}"
                      f"｜領先時間（小時）：平均 {_fmt(eval_res['lead_hours_mean'], 3)}"
                      f"、最短 {_fmt(eval_res['lead_hours_min'], 3)}"
                      f"、最長 {_fmt(eval_res['lead_hours_max'], 3)}")
    except Exception as exc:                                  # noqa: BLE001
        print(f"[評估] 略過（{type(exc).__name__}: {exc}）")

    # ── 輸出 ──
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "fdc_summary.json").write_text(json.dumps(
        {"run_id": run_id, "baseline_mode": args.baseline_mode,
         "baseline_window": args.baseline_window,
         "n_series": n_series, "n_points": n_points,
         "methods": list(methods), "sigma_scope": sigma_scope,
         "monitor_alarm_rate": mon_rate, "charts_in_control": mon_rate <= args.max_alarm_rate,
         "n_out_of_control_series": len(out_of_control),
         "max_series_alarm_rate": args.max_series_alarm_rate,
         "n_few_points": len(few_point_series), "n_short_baseline": len(short_baselines),
         "n_skipped_degenerate": len(skipped_series), "n_undecided": n_undecided,
         "alarm_top": [[list(k), v] for k, v in
                       sorted(alarm_by_source.items(), key=lambda kv: -kv[1])[:args.list_top]],
         "n_charts_per_event": n_charts_per_event, "arl0_chart": arl0_chart,
         "bonferroni": bool(args.bonferroni), "min_alerts": args.min_alerts,
         "n_alert_events_raw": len(alert_events), "n_alert_events_consensus": len(consensus_events),
         "baseline_alarm_rate": base_alarm / max(base_points, 1),
         "monitor_alarm_rate": out_alarm / max(out_points, 1),
         "n_excursion_rows": len(rows_excursion), "n_alert_events": len(alert_events),
         "evaluation": eval_res}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if rows_excursion:
        with (out / "fdc_excursions.csv").open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows_excursion[0]))
            w.writeheader()
            w.writerows(rows_excursion)
    print(f"  報告：{out}/fdc_summary.json、fdc_excursions.csv")

    if not args.no_db:
        nb, ne = write_db(rows_baseline, rows_excursion, run_id, features)
        print(f"  已寫入 mart.fdc_baselines（{nb:,} 列）、mart.fdc_excursions（{ne:,} 列）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())