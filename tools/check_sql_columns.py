"""欄位引用檢查（v3）：同一別名只要在某處綁到 mart.fdc_* 表，就檢查它的每個欄位引用。

踩過的三個坑（都在這裡修掉）：
 ① pglast 節點的迭代不會自動深入子查詢 → 用 `pglast.visitors.Visitor`。
 ② Visitor 方法簽章是 `visit_X(self, ancestors, node)`。
 ③ **別名遮蔽**：第 6 段外層用 CTE `cons` 並叫它 `c`，子查詢裡又用 `c` 指 `mart.fdc_excursions`
    → 早期的實作用「別名→單一表」對應，遇到遮蔽就整段跳過，於是回報「沒問題」而使用者的查詢爆炸。
    改法：別名→**一組**表；只要其中之一是結果表，就把欄位拿來對結果表的 DDL 比對
    （寧可誤報也不要漏報——遮蔽情形下就是分不清，這時應該讓它紅）。
"""
import re
import sys
from pathlib import Path

from pglast import parse_sql
from pglast.ast import ColumnRef, RangeVar
from pglast.visitors import Visitor

ROOT = Path(__file__).resolve().parent.parent
DDL_FILES = sorted((ROOT / 'db/migrations').glob('*.sql'))
TABLES = ('mart.fdc_excursions', 'mart.fdc_baselines')


def schema_of(table: str) -> set:
    cols = set()
    for f in DDL_FILES:
        txt = f.read_text(encoding='utf-8')
        m = re.search(rf'CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\((.*?)\n\);', txt, re.S)
        if m:
            for line in m.group(1).splitlines():
                line = line.strip()
                if not line or line.startswith('--'):
                    continue
                name = line.split()[0].strip(',')
                if name and not name.upper().startswith(
                        ('PRIMARY', 'UNIQUE', 'FOREIGN', 'CHECK', 'CONSTRAINT')):
                    cols.add(name)
        for m2 in re.finditer(
                rf'ALTER TABLE {re.escape(table)}\s+ADD COLUMN IF NOT EXISTS (\w+)', txt):
            cols.add(m2.group(1))
    return cols


class Collector(Visitor):
    def __init__(self):
        self.tables = {}          # 別名 → set(表名)
        self.refs = []

    def visit_RangeVar(self, ancestors, node):
        # pglast 把 schema 放在 schemaname、表名放在 relname（不是 'mart.fdc_excursions' 一整串）
        full = f"{node.schemaname}.{node.relname}" if node.schemaname else node.relname
        alias = node.alias.aliasname if node.alias else full
        self.tables.setdefault(alias, set()).add(full)
        self.tables.setdefault(full, set()).add(full)

    def visit_ColumnRef(self, ancestors, node):
        names = [p if isinstance(p, str) else getattr(p, 'sval', None) for p in node.fields]
        if all(names) and len(names) == 2:
            self.refs.append(tuple(names))


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else 'db/queries/m5_acceptance.sql'
    sql = (ROOT / target).read_text(encoding='utf-8')
    clean = re.sub(r'--[^\n]*', '', sql)
    schemas = {t: schema_of(t) for t in TABLES}
    all_cols = set().union(*schemas.values())
    for t, cols in schemas.items():
        print(f"  {t}：{len(cols)} 欄")

    problems, checked = [], 0
    for i, stmt in enumerate(parse_sql(clean), 1):
        col = Collector()
        col(stmt)
        for alias, name in col.refs:
            bound = col.tables.get(alias, set())
            if not (bound & set(TABLES)):
                continue                       # 這個別名沒有綁到結果表 → 不歸我管
            checked += 1
            if name not in all_cols:
                near = sorted(c for c in all_cols if name[:3] in c or c[:3] in name)
                problems.append((i, alias, name, sorted(bound), near))

    print(f"  檢查了 {checked} 處對結果表的欄位引用")
    if problems:
        print(f"\n  ❌ 欄位引用錯誤 {len(problems)} 處：")
        for seg, alias, name, bound, near in problems:
            print(f"     第 {seg} 段：{alias}.{name}（別名綁到 {bound}）"
                  f"{'→ 相近的欄位：' + '、'.join(near) if near else ''}")
        return 1
    print("  ✅ 全部與 DDL 一致")
    return 0


if __name__ == '__main__':
    sys.exit(main())
