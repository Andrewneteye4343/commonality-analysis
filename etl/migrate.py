#!/usr/bin/env python3
"""套用資料庫 migration（版本化 SQL，取代「每次 down -v 重建」）。

用法：
    python migrate.py                 # 依檔名順序套用 db/migrations/*.sql
    python migrate.py --list          # 只列出狀態
    python migrate.py --dir /work/db/migrations

原理：
    * 建立 public.schema_migrations(version, checksum, applied_at) 記錄已套用版本
    * 每個檔案在一個 transaction 內執行；失敗會 rollback 且不會被記錄
    * 已套用過的檔案會比對 checksum，若內容變了會警告（避免偷偷改歷史）
"""
from __future__ import annotations

import argparse
import sys
import hashlib
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from script_version import add_version_arg

DEFAULT_DIR = "/work/db/migrations"


def db_connect():
    import psycopg
    return psycopg.connect(
        host=os.environ.get("PGHOST", "db"),
        port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        dbname=os.environ["PGDATABASE"],
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS public.schema_migrations (
                         version text PRIMARY KEY,
                         checksum text NOT NULL,
                         applied_at timestamptz DEFAULT now())""")
    conn.commit()


def applied(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT version, checksum FROM public.schema_migrations")
        return dict(cur.fetchall())


def main() -> int:
    ap = argparse.ArgumentParser(description="套用 SQL migration")
    ap.add_argument("--dir", default=os.environ.get("MIGRATIONS_DIR", DEFAULT_DIR))
    ap.add_argument("--list", action="store_true")
    add_version_arg(ap)
    args = ap.parse_args()

    mdir = Path(args.dir)
    if not mdir.exists():
        print(f"[錯誤] 找不到 migration 目錄：{mdir}")
        return 2
    files = sorted(p for p in mdir.glob("*.sql"))

    conn = db_connect()
    ensure_table(conn)
    done = applied(conn)

    print(f"migration 目錄：{mdir}（{len(files)} 個檔案）")
    pending = 0
    for f in files:
        dig = sha256(f)
        state = done.get(f.name)
        if state == dig:
            print(f"  [skip] {f.name}（已套用）")
            continue
        if state and state != dig:
            print(f"  [警告] {f.name} 內容與已套用版本不同（歷史被改動）→ **不重新套用**；"
                  f"要改 schema 請新增下一個版本檔（004_…）")
            continue
        print(f"  [apply] {f.name} …", flush=True)
        sql = f.read_text(encoding="utf-8")
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            print(f"  [失敗] {f.name}: {type(e).__name__}: {e}")
            return 1
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO public.schema_migrations (version, checksum) VALUES (%s, %s)
                           ON CONFLICT (version) DO UPDATE SET checksum = EXCLUDED.checksum,
                                                               applied_at = now()""", (f.name, dig))
        conn.commit()
        pending += 1
        print("          完成")

    if args.list or True:
        with conn.cursor() as cur:
            cur.execute("""SELECT table_schema, table_name FROM information_schema.tables
                           WHERE table_schema IN ('raw','stg','mart','qa')
                           ORDER BY 1, 2""")
            rows = cur.fetchall()
        print(f"\n目前資料庫物件（{len(rows)} 張表）：")
        for s, t in rows:
            print(f"  {s}.{t}")
    conn.close()
    print(f"\n本次套用 {pending} 個 migration。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
