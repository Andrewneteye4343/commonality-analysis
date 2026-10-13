"""M5-2-1 多變量 MSPC 監控（PCA + Hotelling T² + SPE + contribution plots）。

與 M5-1 的關係
────────────────────────────────────────────────────────────
M5-1 用 25 張單變量管制圖 → 警報彼此相關、共識規則失效，零故障機台（04M01）與
故障機台的警報率無法區分（63.1% vs 66.0%）。本模組改用業界標準的多變量架構：
把 25 個變數壓成 **2 個統計量**（T² 與 SPE），多重檢定問題由 25 降到 2。

M5-2-0 前置診斷決定的三件事（不是憑習慣）
────────────────────────────────────────────────────────────
1. 逐 `(機台 × recipe_step)` 建模 —— 實測步驟解釋 **86.3%** 的變異，混步驟建模會讓
   模型去追蹤「現在是哪個步驟」而不是異常。
2. 管制線用**經驗分位數** —— 實測 q99.73 是理論值的 11.5–15.5 倍，理論線會給
   5–7% 的誤報率（名目 0.27%）。
3. T² 只用前 k 個主成分 —— 條件數 1,326–31,685，全維度 S⁻¹ 不可靠。

同時輸出兩種模型（`robust` 開／關），讓我們第一次就跑出「哪一版在真實資料上站得住」。

用法
    docker compose run --rm etl python analysis/run_mspc.py \
        --tools "01,02,03,04,06" --baseline-frac 0.3 --run-id M5_mspc_b0.3
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

import mspc                                                 # noqa: E402
import run_fdc                                              # noqa: E402
import script_version                                        # noqa: E402
from script_version import add_version_arg                   # noqa: E402

DEFAULT_SENSORS = ("FLOWCOOLPRESSURE", "IONGAUGEPRESSURE", "ETCHBEAMVOLTAGE",
                   "ETCHBEAMCURRENT", "ACTUALSTEPDURATION")


# ── 依序串流，逐 (機台 × recipe_step) 組成矩陣（記憶體只留一個集團）──
def iter_group_matrices(series_iter, features, sensors=None, max_groups: int = 0):
    """yield ((tool, recipe_step), {"events", "ts", "vars", "X"}).

    輸入順序必須是 (tool, recipe_step, sensor, …)（`run_fdc` 的兩個 iterator 都保證），
    所以同一個 (tool, step) 的所有感測器會連續出現 → 集團一換就立即產出，不必全放記憶體。
    """
    keep = set(sensors) if sensors else None
    cur_key = None
    ts_map: dict = {}
    cells: dict = {}
    n_groups = 0

    def _emit(key, ts_map, cells):
        if not cells:
            return None
        vars_ = sorted({v for d in cells.values() for v in d})
        events = sorted(ts_map, key=lambda e: (ts_map[e], e))
        X = np.full((len(events), len(vars_)), np.nan, dtype=float)
        idx = {v: i for i, v in enumerate(vars_)}
        for i, e in enumerate(events):
            for k, val in cells.get(e, {}).items():
                X[i, idx[k]] = val
        return (key, {"events": events, "ts": np.array([ts_map[e] for e in events]),
                      "vars": [f"{s}|{f}" for s, f in vars_], "X": X})

    for (tool, step, sensor), rows in series_iter:
        if keep and sensor not in keep:
            continue
        key = (tool, step)
        if cur_key is not None and key != cur_key:
            out = _emit(cur_key, ts_map, cells)
            if out:
                n_groups += 1
                if max_groups and n_groups > max_groups:
                    print(f"[停止] 已達 --max-groups {max_groups}（明列上限，不是靜默截斷）")
                    return
                yield out
            ts_map, cells = {}, {}
        cur_key = key
        for r in rows:
            eid = r["event_id"]
            ts_map[eid] = float(r["in_ts"])       # 防禦：來源若給了字串也要轉
            d = cells.setdefault(eid, {})
            for f in features:
                v = r.get(f)
                if v is not None:
                    d[(sensor, f)] = float(v)
    if cur_key is not None:
        out = _emit(cur_key, ts_map, cells)
        if out:
            yield out


# ── 單一集團：建模 + 監控 ────────────────────────────────────
def process_group(tool, step, g, args, stats_out, alert_rows, skipped,
                  alert_events_by_variant, ts_of_event, tool_of_event, blown):
    ts, X, vars_ = g["ts"], g["X"], g["vars"]
    n = X.shape[0]
    n_base = max(3, int(round(n * args.baseline_frac)))
    Xb, tb = X[:n_base], ts[:n_base]
    Xm, tm = X[n_base:], ts[n_base:]

    # 常數變數（基準期沒有變異）→ 排除並明列
    n_monitor = int(n - n_base)
    cov = {"n_events": int(n), "n_monitor": n_monitor, "modeled": False}
    # ⚠ ddof=0 且先吃掉全 NaN 欄，否則單列/全缺失欄會噴 "Degrees of freedom <= 0" 警告
    # 全 NaN 欄在極小集團裡會讓 nanvar 噴 "Degrees of freedom <= 0"（雜訊，非錯誤）
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        stds = np.nan_to_num(np.nanstd(Xb, axis=0, ddof=0), nan=0.0)
    drop = [vars_[j] for j in range(len(vars_))
            if not np.isfinite(stds[j]) or stds[j] == 0]
    keep = [j for j in range(len(vars_)) if vars_[j] not in drop]
    if len(keep) < 2:
        skipped.append({"tool": tool, "recipe_step": step, "reason": "可用變數 < 2",
                        "vars_total": len(vars_), "dropped": drop})
        return cov
    vars_use = [vars_[j] for j in keep]
    Xb_k, Xm_k = Xb[:, keep], Xm[:, keep]

    complete = ~np.isnan(Xb_k).any(axis=1)
    n_complete = int(complete.sum())
    need = max(args.min_complete_factor * len(vars_use), args.min_complete)
    if n_complete < need:
        skipped.append({"tool": tool, "recipe_step": step,
                        "reason": f"基準期完整向量 {n_complete} < 門檻 {need:.0f}",
                        "n_base": n_base, "dropped": drop, "n_complete": n_complete})
        return cov

    imputed = int(np.isnan(Xm_k).any(axis=1).sum()) if Xm_k.size else 0
    impute_rate = imputed / max(len(Xm_k), 1)

    for robust in (True, False):
        model = mspc.build_model(tb[complete], Xb_k[complete], vars_use,
                                 var_explained=args.var_explained, alpha=args.alpha,
                                 robust=robust)
        if model is None:
            continue
        if Xm_k.size == 0:
            continue
        cov["modeled"] = True
        if getattr(model, "n_capped", 0):
            print(f"    [截斷] {tool}/{step} robust={robust}：{model.n_capped} 個標準化殘差"
                  f"超過 ±{model.sigma_cap:.0f}σ → 已截斷（偵測力不變，貢獻排名才可解讀）")
        if model.dropped_degenerate:
            print(f"    [排除] {tool}/{step} robust={robust}：基準期近乎常數的變數 "
                  f"{len(model.dropped_degenerate)} 個 → {model.dropped_degenerate[:4]}")
        t2, spe = model.score(tm, Xm_k)
        # 輸出健全性：分數若是管制線的天文數字倍，代表還有變數在爆 → 大聲說出來
        _r = np.maximum(t2 / max(model.lim_t2, 1e-12),
                        spe / max(model.lim_spe, 1e-12))
        if _r.size and float(np.quantile(_r, 0.999)) > args.max_score_ratio:
            blown.append({"tool": tool, "recipe_step": step, "robust": robust,
                          "q999_ratio": float(np.quantile(_r, 0.999)),
                          "max_ratio": float(_r.max()), "n_vars": len(model.vars),
                          "dropped_degenerate": model.dropped_degenerate})
        base_t2, base_spe = model.score(tb[complete], Xb_k[complete])
        var_exp = float(model.eigvals[:model.k].sum() / model.eigvals.sum())
        label = "robust" if robust else "plain"
        stats_out.append({
            "run_id": args.run_id, "tool_id": tool, "recipe_step": step, "robust": robust,
            "n_vars": len(vars_use), "n_base_complete": n_complete, "n_monitor": int(len(tm)),
            "pca_k": model.k, "var_explained_actual": var_exp,
            "cond_number": float(model.eigvals[0] / model.eigvals[-1]),
            "lim_t2": model.lim_t2, "lim_spe": model.lim_spe,
            "base_alarm_t2": float(np.mean(base_t2 > model.lim_t2)),
            "base_alarm_spe": float(np.mean(base_spe > model.lim_spe)),
            "imputed_rate": impute_rate,
            "dropped_vars": len(drop),
        })
        # 事件層警報：任一統計量越界即算（同時也記錄「兩者同時」供對照）
        a_t2, a_spe = t2 > model.lim_t2, spe > model.lim_spe
        events = g["events"][n_base:]
        for i, e in enumerate(events):
            ts_of_event[e] = float(tm[i])
            tool_of_event[e] = tool
            if a_t2[i] or a_spe[i]:
                alert_events_by_variant[(label, "any")].add(e)
                if a_t2[i] and a_spe[i]:
                    alert_events_by_variant[(label, "both")].add(e)
                if len(alert_rows) < args.max_alert_rows:
                    row = Xm_k[i]
                    contrib = model.contributions(float(tm[i]), row)
                    for stat, arr in (("t2", contrib["t2"]), ("spe", contrib["spe"])):
                        hit = a_t2[i] if stat == "t2" else a_spe[i]
                        if not hit:
                            continue
                        order = np.argsort(-np.abs(arr))[:3]
                        top = [(vars_use[j], float(arr[j])) for j in order]
                        alert_rows.append({
                            "run_id": args.run_id, "tool_id": tool,
                            "recipe_step": step, "robust": robust, "event_id": e,
                            "in_ts": float(tm[i]), "stat": stat,
                            "value": float(contrib["t2_total"] if stat == "t2"
                                           else contrib["spe_total"]),
                            "limit_value": float(model.lim_t2 if stat == "t2"
                                                 else model.lim_spe),
                            "top1": top[0][0], "top1_contrib": top[0][1],
                            "top2": top[1][0] if len(top) > 1 else "",
                            "top2_contrib": top[1][1] if len(top) > 1 else 0.0,
                            "top3": top[2][0] if len(top) > 2 else "",
                            "top3_contrib": top[2][1] if len(top) > 2 else 0.0,
                        })
    return cov


# ── 寫入資料庫 ────────────────────────────────────────────────
def write_db(models_rows, alert_rows, run_id, robust):
    import psycopg
    conn = psycopg.connect(host=os.environ.get("PGHOST", "db"),
                           port=int(os.environ.get("PGPORT", 5432)),
                           dbname=os.environ.get("PGDATABASE", "commonality"),
                           user=os.environ["PGUSER"],
                           password=os.environ.get("PGPASSWORD", ""))
    with conn, conn.cursor() as cur:
        cur.execute("DELETE FROM mart.mspc_models WHERE run_id = %s AND robust = %s",
                    (run_id, robust))
        cur.execute("DELETE FROM mart.mspc_alerts WHERE run_id = %s AND robust = %s",
                    (run_id, robust))
        if models_rows:
            cols = ("run_id", "tool_id", "recipe_step", "robust", "n_vars",
                    "n_base_complete", "n_monitor", "pca_k", "var_explained_actual",
                    "cond_number", "lim_t2", "lim_spe", "base_alarm_t2",
                    "base_alarm_spe", "imputed_rate", "dropped_vars")
            with cur.copy(f"COPY mart.mspc_models ({', '.join(cols)}) FROM STDIN") as cp:
                for r in models_rows:
                    cp.write_row(tuple(r[c] for c in cols))
        if alert_rows:
            cols = ("run_id", "tool_id", "recipe_step", "robust", "event_id", "in_ts",
                    "stat", "value", "limit_value", "top1", "top1_contrib",
                    "top2", "top2_contrib", "top3", "top3_contrib")
            with cur.copy(f"COPY mart.mspc_alerts ({', '.join(cols)}) FROM STDIN") as cp:
                for r in alert_rows:
                    cp.write_row(tuple(r[c] for c in cols))
    conn.close()
    return len(models_rows), len(alert_rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="M5-2-1 多變量 MSPC（PCA + T² + SPE）")
    ap.add_argument("--tools", default="01,02,03,04,06")
    ap.add_argument("--sensors", default=",".join(DEFAULT_SENSORS))
    ap.add_argument("--features", default="mean_val,std_val,range_val,slope,missing_rate")
    ap.add_argument("--baseline-frac", type=float, default=0.3)
    ap.add_argument("--var-explained", type=float, default=0.90,
                    help="主成分數 k 的累積解釋變異門檻（預設 90%%，且 k>=2）")
    ap.add_argument("--alpha", type=float, default=0.0027,
                    help="經驗分位數管制線的名目誤報率（單一統計量）")
    ap.add_argument("--min-complete", type=int, default=40,
                    help="集團基準期完整向量的絕對下限")
    ap.add_argument("--min-complete-factor", type=float, default=5.0,
                    help="相對下限：完整向量數 >= 此倍數 × 變數數（共變異數矩陣要估得動）")
    ap.add_argument("--max-alert-rows", type=int, default=100000,
                    help="明細列上限（超過就只留摘要，並在報告中大聲說明）")
    ap.add_argument("--max-groups", type=int, default=0, help="0 = 不限；測試用")
    ap.add_argument("--max-score-ratio", type=float, default=1e6,
                    help="監控期（分數/管制線）的 99.9 分位超過此值 → 警告還有變數在數值上爆掉")
    ap.add_argument("--max-alarm-rate", type=float, default=0.05,
                    help="負控制機台警報率超過此值就發出「監控不在管制內」警告")
    ap.add_argument("--series-csv", default="")
    ap.add_argument("--labels-csv", default="")
    ap.add_argument("--no-db", action="store_true")
    ap.add_argument("--run-id", default="M5_mspc_b0.3")
    ap.add_argument("--window-hours", type=int, default=24)
    ap.add_argument("--report", default="reports/m5/mspc_summary.json")
    add_version_arg(ap)
    args = ap.parse_args()

    features = run_fdc.parse_csv_list(args.features)
    tools_in = run_fdc.parse_csv_list(args.tools)
    sensors_in = run_fdc.parse_csv_list(args.sensors)

    if args.series_csv:
        series_iter = run_fdc.iter_csv_series(Path(args.series_csv), features)
        tools, sensors = tools_in, sensors_in
        print(f"[模式] CSV（助理端驗證）｜{args.series_csv}")
    else:
        import psycopg
        conn = psycopg.connect(host=os.environ.get("PGHOST", "db"),
                               port=int(os.environ.get("PGPORT", 5432)),
                               dbname=os.environ.get("PGDATABASE", "commonality"),
                               user=os.environ["PGUSER"],
                               password=os.environ.get("PGPASSWORD", ""))
        tools, missing = run_fdc.resolve_tool_ids(conn.cursor(), tools_in)
        if missing:
            print(f"[代號] 解析不到的機台 {missing}（exit 2）→ 請確認 --tools")
            return 2
        sensors, missing_s = run_fdc.resolve_sensors(conn.cursor(), sensors_in)
        if missing_s:
            print(f"[感測器] 解析不到 {missing_s}（exit 2）")
            return 2
        print(f"[代號] --tools {list(tools_in)} → {tools}")
        print(f"[感測器] 解析到 {len(sensors)} 個")
        series_iter = run_fdc.iter_db_series(tools, sensors, features, "ANY")

    print(f"[設定] MSPC｜每集團基準期比例 {args.baseline_frac}｜主成分累積解釋 "
          f"{args.var_explained:.0%}｜管制線＝基準期經驗分位數（名目 {args.alpha:.4f}）")
    print(f"       逐 (機台 × recipe_step) 建模（M5-2-0 實測步驟解釋 86.3% 變異）"
          f"｜同時跑 robust 與 plain 兩版")

    stats_out: list = []
    alert_rows: list = []
    skipped: list = []
    alert_events_by_variant: dict = defaultdict(set)
    ts_of_event: dict = {}
    tool_of_event: dict = {}
    blown: list = []
    n_groups = 0
    cov_total = {"n_events": 0, "n_monitor": 0, "modeled_monitor": 0}
    for (tool, step), g in iter_group_matrices(series_iter, features, sensors,
                                               args.max_groups):
        n_groups += 1
        cov = process_group(tool, step, g, args, stats_out, alert_rows, skipped,
                            alert_events_by_variant, ts_of_event, tool_of_event, blown)
        if cov:
            cov_total["n_events"] += cov["n_events"]
            cov_total["n_monitor"] += cov["n_monitor"]
            if cov["modeled"]:
                cov_total["modeled_monitor"] += cov["n_monitor"]

    if n_groups == 0:
        print("[錯誤] 掃描到 0 個集團 → 不是「沒有異常」，是根本沒查到資料（exit 3）")
        return 3
    print(f"\n[掃描] 集團（機台 × recipe_step）{n_groups} 個｜建模 "
          f"{len(stats_out)} 個（robust+plain 各一次）｜跳過 {len(skipped)} 個")
    _tot = max(cov_total["n_monitor"], 1)
    print(f"[覆蓋] 事件總數 {cov_total['n_events']:,}｜其中監控期 {cov_total['n_monitor']:,}"
          f"｜**實際被建模的監控期事件 {cov_total['modeled_monitor']:,}"
          f"（{100 * cov_total['modeled_monitor'] / _tot:.1f}%）**"
          f"→ 這是 MSPC 的真實代價：樣本不足的集團無法建模，必須明列")
    if blown:
        print(f"\n  ★ 警告：{len(blown)} 個（集團 × 版本）的分數超過管制線 10^6 倍以上"
              f" → 還有變數在數值上爆掉，這些集團的【貢獻排名不可信】★")
        for b in sorted(blown, key=lambda x: -x["max_ratio"])[:5]:
            print(f"        {b['tool']}/{b['recipe_step']} robust={b['robust']}："
                  f"99.9 分位 {b['q999_ratio']:.2e} 倍、最大 {b['max_ratio']:.2e} 倍"
                  f"｜已排除近乎常數變數 {len(b['dropped_degenerate'])} 個")
    if skipped:
        print(f"[跳過] 前 8 個（完整明細在報告）：")
        for sk in skipped[:8]:
            print(f"        {sk['tool']}/{sk['recipe_step']}：{sk['reason']}")

    # 標籤與評估（沿用 M5-1 的 evaluate，口徑一致才可比）
    labelled, fault_times = None, {}
    if args.labels_csv:
        # ⚠ load_labels 與 load_fault_times 是兩個獨立函式（不是回傳 tuple）
        _lp = Path(args.labels_csv)
        labelled = run_fdc.load_labels(_lp, window_hours=args.window_hours)
        try:
            fault_times = run_fdc.load_fault_times(_lp)
        except (KeyError, ValueError):
            fault_times = {}
            print("[標籤] CSV 內沒有 fault_ts 欄 → 只算窗內偵測率，不計領先時間")
    elif not args.series_csv and not args.no_db:
        conn2 = psycopg.connect(host=os.environ.get("PGHOST", "db"),
                                port=int(os.environ.get("PGPORT", 5432)),
                                dbname=os.environ.get("PGDATABASE", "commonality"),
                                user=os.environ["PGUSER"],
                                password=os.environ.get("PGPASSWORD", ""))
        labelled = run_fdc.load_labels(conn2, window_hours=args.window_hours)
        fault_times = run_fdc.load_fault_times(conn2)
        conn2.close()

    report = {"version": script_version.current_version(),
              "run_id": args.run_id, "n_groups": n_groups, "coverage": cov_total, "blown_scores": blown,
              "n_models": len(stats_out), "skipped": skipped,
              "n_alert_rows": len(alert_rows),
              "alert_rows_capped": len(alert_rows) >= args.max_alert_rows,
              "models": [s for s in stats_out], "evaluation": {}}

    print(f"\n[警報] 明細列 {len(alert_rows):,}"
          f"{'（已達 --max-alert-rows 上限，只保留前段明細）' if report['alert_rows_capped'] else ''}")
    for label in ("robust", "plain"):
        for mode in ("any", "both"):
            s = alert_events_by_variant.get((label, mode), set())
            print(f"  {label:<7}{mode:<5}警報事件 {len(s):,}")
    if labelled is not None:
        for label in ("robust", "plain"):
            ev = run_fdc.evaluate(alert_events_by_variant.get((label, "any"), set()),
                                  labelled, fault_times, tool_of_event, ts_of_event)
            report["evaluation"][label] = ev
            print(f"\n  ■ 模型 {label}（任一統計量越界）")
            print(f"    窗內事件 {ev['n_events_in_window']:,} 中偵測到 "
                  f"{ev['detection_rate_in_window']:.4f}"
                  if ev.get("detection_rate_in_window") is not None else "    窗內事件 0")
            print(f"    逐機台警報率：" + "｜".join(
                f"{t} {v['alert_rate']:.4f}" if v["alert_rate"] is not None else f"{t} —"
                for t, v in ev.get("by_tool_alert_rate", {}).items()))
            if ev.get("false_alarm_rate_control") is not None:
                fa = ev["false_alarm_rate_control"]
                flag = "★ 監控不在管制內（誤報率過高，不要用這次排名下結論）★ " \
                    if fa > args.max_alarm_rate else ""
                print(f"    負控制機台誤報率 {fa:.4f}  {flag}")
            if ev.get("fault_detection_rate") is not None:
                print(f"    故障偵測率 {ev['fault_detection_rate']:.3f}｜領先時間（小時）"
                      f"平均 {ev.get('lead_hours_mean')}、最短 {ev.get('lead_hours_min')}")

    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_path = out.with_name("mspc_alerts.csv")
    if alert_rows:
        cols = list(alert_rows[0].keys())
        with csv_path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(alert_rows)
    print(f"\n  報告：{out}｜明細：{csv_path if alert_rows else '（無警報）'}")

    if not args.no_db and not args.series_csv:
        for robust in (True, False):
            m = [s for s in stats_out if s["robust"] is robust]
            a = [r for r in alert_rows if r["robust"] is robust]
            if m or a:
                nm, na = write_db(m, a, args.run_id, robust)
                print(f"  已寫入 mart.mspc_models（{nm} 列）、"
                      f"mart.mspc_alerts（{na} 列）｜robust={robust}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
