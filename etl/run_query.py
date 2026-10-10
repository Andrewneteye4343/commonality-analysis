#!/usr/bin/env python3
"""跑一個 .sql 檔（含多個陳述式）並逐段印出結果。

用法（容器內）：
    docker compose run --rm etl python run_query.py db/queries/m3_acceptance.sql
    docker compose run --rm etl python run_query.py db/queries/m3_acceptance.sql --only 12

為什麼需要：驗收查詢有十幾個陳述式，用 `psql -c` 一次只能跑一個；
而且 `psql -f` 需要把 `db/` 掛進 db 容器（本專案只掛在 etl 容器）。
這支工具讓「跑驗收」變成一個可重複的單一指令。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from script_version import add_version_arg  # noqa: E402


def split_statements(sql: str) -> list[tuple[str, str]]:
    """切成 (標題, 陳述式)；標題取自 `-- n)` 開頭的註解，方便對照 checklist"""
    sql = re.sub(r"^\s*--.*$", "", sql, flags=re.MULTILINE)   # 去掉整行註解
    out: list[tuple[str, str]] = []
    for raw in sql.split(";"):
        stmt = raw.strip()
        if stmt:
            out.append((f"#{len(out) + 1}", stmt))
    return out


def titles(sql: str) -> dict[str, str]:
    """從原檔抓 `-- 3) 說明` 這類註解，當作輸出標題"""
    found = {}
    for m in re.finditer(r"^--\s*(\d+)\)\s*(.+)$", sql, flags=re.MULTILINE):
        found[m.group(1)] = m.group(2).strip()[:70]
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description="執行 .sql 檔並印出結果")
    ap.add_argument("file", nargs="?", default="db/queries/m3_acceptance.sql")
    ap.add_argument("--only", default="", help="只跑指定的段號（例如 12 或 10,12）")
    ap.add_argument("--max-rows", type=int, default=25, help="每段最多印幾列（預設 25）")
    add_version_arg(ap)
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        print(f"[錯誤] 找不到 {path}")
        return 2
    sql = path.read_text(encoding="utf-8")
    stmts = split_statements(sql)
    want = {s.strip() for s in args.only.split(",") if s.strip()}
    notes = titles(sql)

    import psycopg
    conn = psycopg.connect(
        host=os.environ.get("PGHOST", "db"), port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""),
        dbname=os.environ.get("PGDATABASE", "commonality"))
    print(f"檔案：{path}（{len(stmts)} 個陳述式）")
    for label, stmt in stmts:
        num = label.lstrip("#")
        if want and num not in want:
            continue
        note = notes.get(num, "")
        print("\n" + "=" * 96)
        print(f"[{num}] {note}")
        print("=" * 96)
        with conn.cursor() as cur:
            cur.execute(stmt)
            desc = getattr(cur, "description", None)
            if desc:
                cols = [getattr(d, "name", f"c{i+1}") for i, d in enumerate(desc)]
                rows = cur.fetchmany(args.max_rows + 1)
                more = len(rows) > args.max_rows
                rows = rows[: args.max_rows]
                widths = [max(len(str(c)), *(len(str(r[i])) for r in rows)) if rows else len(str(c))
                          for i, c in enumerate(cols)]
                print("  " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cols)))
                print("  " + "-+-".join("-" * w for w in widths))
                for r in rows:
                    print("  " + " | ".join(str(v).ljust(widths[i]) for i, v in enumerate(r)))
                if not rows:
                    print("  （0 列）")
                if more:
                    print(f"  …（僅顯示前 {args.max_rows} 列）")
            else:
                print(f"  （無回傳；rowcount = {getattr(cur, 'rowcount', -1)}）")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
