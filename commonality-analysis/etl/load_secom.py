#!/usr/bin/env python3
"""M1：把 SECOM 載入 PostgreSQL，並產生完整性與資料品質報告。

用法（容器內）：
    python load_secom.py                  # 解析 → 入庫 → 驗證
    python load_secom.py --dry-run        # 只解析與驗證，不寫資料庫（可在沒有 DB 的環境測試）
    python load_secom.py --data-dir /data/raw

驗收條件（M1）：
    * 特徵：1567 列 × 590 欄
    * 標籤：pass(-1) = 1463、fail(1) = 104
    * 每欄缺失率寫入 qa.secom_column_profile
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path

# ─────────────────────────────────────────────────────────────
# 解析（純函式，方便測試）
# ─────────────────────────────────────────────────────────────
def parse_label_line(line: str) -> tuple[int, datetime | None, str]:
    """SECOM 標籤行格式：`-1 "19/07/2008 11:55:00"`（日期 dd/mm/yyyy）"""
    parts = line.strip().split()
    if len(parts) < 2:
        raise ValueError(f"無法解析標籤行：{line!r}")
    label = int(parts[0])
    raw_ts = " ".join(parts[1:]).replace('"', "")
    ts = None
    try:
        ts = datetime.strptime(raw_ts, "%d/%m/%Y %H:%M:%S")
    except ValueError:
        ts = None
    return label, ts, raw_ts


def parse_indices(indices_path: str | None) -> list[int]:
    """SECom 官方沒有給欄名，UCI 版以 0-based 索引標註；若有索引檔則沿用。"""
    if not indices_path or not Path(indices_path).exists():
        return []
    return [int(x) for x in Path(indices_path).read_text().split() if x.strip().lstrip("-").isdigit()]


def open_member(zip_path: Path, name: str) -> io.TextIOWrapper:
    zf = zipfile.ZipFile(zip_path)
    return io.TextIOWrapper(zf.open(name), encoding="utf-8", errors="replace")


def parse_secom(zip_path: Path) -> dict:
    """回傳 {features: list[list[float|None]], labels: list[(label, ts, src)], column_profile: [...]}"""
    with open_member(zip_path, "secom.data") as fh_data, open_member(zip_path, "secom_labels.data") as fh_lab:
        features: list[list[float | None]] = []
        n_cols = None
        for i, line in enumerate(fh_data):
            if not line.strip():
                continue
            vals: list[float | None] = []
            for tok in line.split():
                if tok.strip().lower() in ("nan", "na", "null", ""):
                    vals.append(None)
                else:
                    vals.append(float(tok))
            if n_cols is None:
                n_cols = len(vals)
            elif len(vals) != n_cols:
                raise ValueError(f"第 {i} 列欄位數不一致：{len(vals)} vs {n_cols}")
            features.append(vals)

        labels = [parse_label_line(l) for l in fh_lab if l.strip()]

    if len(features) != len(labels):
        raise ValueError(f"特徵列數 {len(features)} 與標籤列數 {len(labels)} 不一致")

    # 每欄品質剖面
    profile = []
    n = len(features)
    for c in range(n_cols or 0):
        col = [row[c] for row in features]
        present = [v for v in col if v is not None]
        n_missing = n - len(present)
        mean = sum(present) / len(present) if present else None
        if len(present) > 1:
            var = sum((v - mean) ** 2 for v in present) / (len(present) - 1)
            std = var ** 0.5
        else:
            std = None
        profile.append({
            "feature_id": f"f{c + 1:04d}",
            "n_total": n,
            "n_missing": n_missing,
            "missing_rate": round(n_missing / n, 4),
            "n_unique": len(set(present)),
            "min_val": min(present) if present else None,
            "max_val": max(present) if present else None,
            "mean_val": mean,
            "std_val": std,
        })

    return {"features": features, "labels": labels, "n_cols": n_cols, "profile": profile}


# ─────────────────────────────────────────────────────────────
# 入庫
# ─────────────────────────────────────────────────────────────
def db_connect():
    import psycopg
    return psycopg.connect(
        host=os.environ.get("PGHOST", "db"),
        port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        dbname=os.environ["PGDATABASE"],
        autocommit=False,
    )


def load(parsed: dict, data_dir: Path, manifest: dict | None) -> None:
    raw_labels = [(i + 1, lab, ts, src) for i, (lab, ts, src) in enumerate(parsed["labels"])]
    n_cols = parsed["n_cols"]
    cols = [f"f{c + 1:04d}" for c in range(n_cols)]

    with db_connect() as conn, conn.cursor() as cur:
        # 長表特徵
        cur.execute("TRUNCATE raw.secom_features")
        with cur.copy("COPY raw.secom_features (sample_id, feature_id, value) FROM STDIN") as cp:
            for i, row in enumerate(parsed["features"], start=1):
                for fid, v in zip(cols, row):
                    if v is None:
                        continue  # 缺失 ⇒ 不寫入（長表語意：沒有值就是沒有）
                    cp.write_row((i, fid, v))

        # 標籤
        cur.execute("TRUNCATE raw.secom_labels")
        with cur.copy("COPY raw.secom_labels (sample_id, label, measured_at, source_ts) FROM STDIN") as cp:
            for sid, lab, ts, src in raw_labels:
                cp.write_row((sid, lab, ts, src))

        # 寬表（依實際欄數動態建立）
        cur.execute("DROP TABLE IF EXISTS stg.secom_wide")
        ddl = ", ".join(f"{c} double precision" for c in cols)
        cur.execute(f"CREATE TABLE stg.secom_wide (sample_id integer PRIMARY KEY, {ddl})")
        collist = ", ".join(cols)
        with cur.copy(f"COPY stg.secom_wide (sample_id, {collist}) FROM STDIN") as cp:
            for i, row in enumerate(parsed["features"], start=1):
                cp.write_row((i, *row))

        # 欄位品質剖面
        cur.execute("TRUNCATE qa.secom_column_profile")
        with cur.copy(
            "COPY qa.secom_column_profile (feature_id, n_total, n_missing, missing_rate, n_unique,"
            " min_val, max_val, mean_val, std_val) FROM STDIN"
        ) as cp:
            for p in parsed["profile"]:
                cp.write_row((p["feature_id"], p["n_total"], p["n_missing"], p["missing_rate"],
                              p["n_unique"], p["min_val"], p["max_val"], p["mean_val"], p["std_val"]))

        # 資料血緣
        for f in (manifest or {}).get("files", []):
            cur.execute(
                """INSERT INTO raw.data_catalog
                   (file_key, path, source_url, description, bytes, sha256, downloaded_at, row_count, notes)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (file_key) DO UPDATE
                   SET loaded_at = now(), bytes = EXCLUDED.bytes, sha256 = EXCLUDED.sha256,
                       row_count = EXCLUDED.row_count, notes = EXCLUDED.notes""",
                (f["key"], f.get("path"), f.get("source_url"), f.get("description"), f.get("bytes"),
                 f.get("sha256"), f.get("downloaded_at"),
                 len(parsed["features"]) if f["key"] == "secom" else None,
                 "SECOM：1567×590，標籤 1463 pass / 104 fail" if f["key"] == "secom" else None),
            )
        conn.commit()


# ─────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="載入 SECOM 並驗證（M1）")
    ap.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "data/raw"))
    ap.add_argument("--dry-run", action="store_true", help="只解析與驗證，不寫資料庫")
    ap.add_argument("--emit-sql", action="store_true",
                    help="印出將執行的 DDL 與 COPY 內容樣本（不連資料庫，方便檢查與測試）")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    zip_path = data_dir / "secom.zip"
    if not zip_path.exists():
        print(f"[錯誤] 找不到 {zip_path}；請先執行 python download_data.py --only secom", file=sys.stderr)
        return 2

    manifest_path = data_dir / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None

    print(f"解析 {zip_path} …")
    parsed = parse_secom(zip_path)
    labs = [l[0] for l in parsed["labels"]]
    n_pass, n_fail = labs.count(-1), labs.count(1)

    miss = [p for p in parsed["profile"] if p["n_missing"] > 0]
    fully_missing = [p for p in parsed["profile"] if p["n_missing"] == p["n_total"]]
    const = [p for p in parsed["profile"] if p["n_unique"] == 1]

    print("\n" + "=" * 78)
    print("SECOM 資料驗證報告")
    print("=" * 78)
    print(f"  特徵矩陣        : {len(parsed['features'])} 列 × {parsed['n_cols']} 欄")
    print(f"  標籤            : {len(labs)} 筆  →  pass(-1) = {n_pass}、fail(1) = {n_fail}")
    print(f"  失敗率          : {100 * n_fail / len(labs):.2f}%")
    ts_ok = sum(1 for _, t, _ in parsed["labels"] if t is not None)
    print(f"  時間戳可解析    : {ts_ok}/{len(labs)}")
    print(f"  有缺失值的欄位  : {len(miss)}/{parsed['n_cols']}")
    print(f"  全空欄位        : {len(fully_missing)}")
    print(f"  常數欄位        : {len(const)}")
    if miss:
        worst = sorted(miss, key=lambda p: -p["missing_rate"])[:5]
        print("  缺失最嚴重的 5 欄：" + ", ".join(f"{p['feature_id']}({p['missing_rate']:.0%})" for p in worst))

    ok = (len(parsed["features"]) == 1567 and parsed["n_cols"] == 590 and n_pass == 1463 and n_fail == 104)
    print(f"\n  驗收條件（1567×590、1463 pass / 104 fail）：{'✅ 通過' if ok else '❌ 未通過'}")

    if args.emit_sql:
        cols = [f"f{c + 1:04d}" for c in range(parsed["n_cols"])]
        ddl = f"CREATE TABLE stg.secom_wide (sample_id integer PRIMARY KEY, " + \
              ", ".join(f"{c} double precision" for c in cols) + ")"
        print("\n--- 產生的 DDL（stg.secom_wide）---")
        print(ddl[:300] + f"  …（其餘 {len(cols) - 8} 欄省略）… " + ddl[-60:])
        print(f"  總欄數 = {len(cols) + 1}（含 sample_id）")
        print("\n--- COPY 內容樣本 ---")
        print("  raw.secom_labels ← ", end="")
        print([(i + 1, lab, str(ts)) for i, (lab, ts, _) in enumerate(parsed["labels"][:2])])
        print("  raw.secom_features ← ", end="")
        sample = [(1, c, v) for c, v in list(zip(cols, parsed["features"][0]))[:3] if v is not None]
        print(sample)
        n_values = sum(1 for row in parsed["features"] for v in row if v is not None)
        n_null = len(parsed["features"]) * parsed["n_cols"] - n_values
        print(f"  raw.secom_features 預計筆數 = {n_values:,}（缺失 {n_null:,} 筆不寫入）")
        print(f"  qa.secom_column_profile 預計筆數 = {len(parsed['profile']):,}")
        print(f"  stg.secom_wide 預計筆數 = {len(parsed['features']):,}")
        if args.dry_run:
            print("\n[dry-run] 不寫入資料庫。")
            return 0 if ok else 1
        print("\n[emit-sql] 僅列印，不寫入資料庫。")
        return 0 if ok else 1

    if args.dry_run:
        print("\n[dry-run] 不寫入資料庫。")
        return 0 if ok else 1

    print("\n寫入 PostgreSQL …")
    load(parsed, data_dir, manifest)
    print("完成：raw.secom_features、raw.secom_labels、stg.secom_wide、qa.secom_column_profile、raw.data_catalog")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
