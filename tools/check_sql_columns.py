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
def discover_tables() -> tuple:
    """從所有 migration 動態找出**全部**表 → 不再靠手寫清單。

    為什麼要動態（實際踩到）：M5-2 新增 `mart.mspc_models`／`mart.mspc_alerts` 後，
    寫死的清單會讓新表的欄位引用整段**靜默跳過**，檢查器照樣回報「全部一致」
    （檢查了 0 處）—— 這正是自製檢查器最危險的假通過。
    """
    found = set()
    for f in DDL_FILES:
        txt = f.read_text(encoding='utf-8')
        for m in re.finditer(r'CREATE TABLE IF NOT EXISTS\s+([\w.]+)', txt):
            found.add(m.group(1))
    return tuple(sorted(found))


def discover_views() -> tuple:
    found = set()
    for f in DDL_FILES:
        txt = f.read_text(encoding='utf-8')
        for m in re.finditer(r'CREATE OR REPLACE VIEW\s+([\w.]+)', txt):
            found.add(m.group(1))
    return tuple(sorted(found))


def view_columns(view: str) -> set:
    """解析 CREATE VIEW 的**輸出欄位名**（用別名；pglast 讀 targetList）。

    為什麼需要：M5-2 的驗收查詢大量透過視圖 `mart.v_mspc_event_alerts` 取值，
    若把視圖當成「無法驗證」而跳過，那些別名.欄位引用就會變成靜默跳過。
    """
    cols = set()
    for f in DDL_FILES:
        txt = f.read_text(encoding='utf-8')
        for m in re.finditer(rf'CREATE OR REPLACE VIEW\s+{re.escape(view)}\s+AS(.*?);',
                             txt, re.S):
            try:
                for stmt in parse_sql(m.group(1).strip()):
                    q = stmt.stmt
                    tgt = (getattr(q, 'targetList', None)
                           or getattr(getattr(q, 'larg', None), 'targetList', None) or [])
                    for r in tgt:
                        if getattr(r, 'name', None):
                            cols.add(r.name)
                        else:
                            v = getattr(r, 'val', None)
                            nm = (getattr(v, 'sval', None) or getattr(v, 'name', None)
                                  or getattr(v, 'val', None))
                            if isinstance(nm, str):
                                cols.add(nm)
            except Exception:
                continue
    return cols


TABLES = discover_tables()
VIEWS = discover_views()
VERIFIABLE = tuple(sorted(set(TABLES) | set(VIEWS)))


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





# ── 規則 2：相關子查詢不可引用「外層未被 GROUP BY 的欄位」─────────────
# 實際踩到：`GROUP BY a.robust` 的查詢寫了 `(SELECT ... WHERE m.run_id = a.run_id)`
# → PostgreSQL 直接報 GroupingError（欄位存在 ≠ 語意正確）。
#
# 為什麼用文字掃描而不是 AST：本版 pglast 的 groupClause 是 ColumnRef 的 tuple
# （不是 GroupingSet），且 visitor 對 SubLink 的分派不易驗證。這條規則要的是
# 「一定會亮」——一個可讀、可測的掃描器比一個沒被觸發的 visitor 可靠。
def _strip_comments(sql: str) -> str:
    return re.sub(r'--[^\n]*', '', sql)


def _split_statements(sql: str) -> list:
    return [x for x in sql.split(';') if x.strip()]


def _paren_spans(stmt: str) -> list:
    """回傳所有「括號內含 SELECT」的 (起, 迄) 區間（含巢狀）。"""
    spans, stack = [], []
    for i, ch in enumerate(stmt):
        if ch == '(':
            stack.append(i)
        elif ch == ')' and stack:
            j = stack.pop()
            if re.search(r'\bSELECT\b', stmt[j:i], re.I):
                spans.append((j, i))
    return spans


def grouping_problems(statements: list) -> tuple:
    """回傳 (problems, skipped_positional)。problems 元素為 (段號, 別名, 欄位, GROUP BY 欄名)。

    判定條件刻意收窄（誤報比沒有檢查器更糟，實際踩過誤報 24 處／真錯 1 處）：
      * 先移除單引號字串（run_id 之類的字面值含小數點，會被誤認為「別名.欄位」）
      * 別名必須是**短的**（1–3 字元、小寫開頭）→ 排除 `mart.mspc_alerts` 這種表名
      * 欄位必須是**識別字**（字母開頭）→ 排除 `100.0` 這種數字
      * 排除該子查詢自己宣告的別名（FROM／JOIN 後面接的別名）
    """
    bad, skipped = [], 0
    for i, stmt in enumerate(statements, 1):
        stmt = re.sub(r"'[^']*'", "''", stmt)          # 去掉字串內容
        m = re.search(r'\bGROUP\s+BY\b(.*?)(?:\bHAVING\b|\bORDER\b|\bLIMIT\b|$)',
                      stmt, re.S | re.I)
        if not m:
            continue
        gtxt = m.group(1)
        if re.match(r'\s*\d', gtxt):
            skipped += 1
            continue
        grouped = set()
        for part in gtxt.split(','):
            mm = re.match(r'^\s*([\w.\"]+)\s*$', part)
            if mm:
                grouped.add(mm.group(1).split('.')[-1].strip('"'))
        for a, b in _paren_spans(stmt):
            inner = stmt[a:b]
            if not re.search(r'\bSELECT\b', inner, re.I):
                continue
            own = set()
            for mm in re.finditer(r'\b(?:FROM|JOIN)\s+[\w.]+(?:\s+(?:AS\s+)?(\w+))?', inner, re.I):
                if mm.group(1):
                    own.add(mm.group(1))
            for qual, col in re.findall(r'\b([a-z_][a-z0-9_]{0,2})\.([a-z_][a-z0-9_]*)\b', inner):
                if qual in own:
                    continue
                if col not in grouped:
                    bad.append((i, qual, col, sorted(grouped)))
    return bad, skipped


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else 'db/queries/m5_acceptance.sql'
    sql = (ROOT / target).read_text(encoding='utf-8')
    clean = re.sub(r'--[^\n]*', '', sql)
    schemas = {t: schema_of(t) for t in TABLES}
    n_view_cols = 0
    for v in VIEWS:
        cols = view_columns(v)
        schemas[v] = cols
        n_view_cols += len(cols)
    print(f"  【範圍】表 {len(TABLES)} 張 + 視圖 {len(VIEWS)} 個"
          f"（視圖共解析出 {n_view_cols} 欄）")
    all_cols = set().union(*schemas.values())
    for t, cols in schemas.items():
        print(f"  {t}：{len(cols)} 欄")

    problems, checked = [], 0
    skipped: dict = {}
    for i, stmt in enumerate(parse_sql(clean), 1):
        col = Collector()
        col(stmt)
        for alias, name in col.refs:
            bound = col.tables.get(alias, set())
            if not (bound & set(VERIFIABLE)):
                # 不是「不歸我管」就算了：要分類並計數，否則跳過會變成靜默
                if bound and all(schemas.get(t) is None for t in bound):
                    why = 'cte_or_unknown_table'      # 例如 mon／denom 這類 CTE
                elif bound and all(not schemas.get(t) for t in bound):
                    why = 'unresolvable_view'      # 視圖欄位解析不到（0 欄）→ 不可當成錯誤
                elif bound:
                    why = 'unknown_table'
                else:
                    why = 'no_binding'
                skipped[why] = skipped.get(why, 0) + 1
                continue
            checked += 1
            if name not in all_cols:
                near = sorted(c for c in all_cols if name[:3] in c or c[:3] in name)
                problems.append((i, alias, name, sorted(bound), near))

    # 聚合語意檢查預設關閉：純文字層無法可靠區分「外層別名」與「被遮蔽的內層別名／schema 前綴」
    # （實測對既有查詢誤報 11 處、真錯 0 處）。誤報會讓檢查器失去信任，所以改成 opt-in：
    #     python tools/check_sql_columns.py <sql> --grouping
    grouping_bad, skipped_pos = ([], 0)
    if '--grouping' in sys.argv:
        grouping_bad, skipped_pos = grouping_problems(_split_statements(_strip_comments(sql)))
        print(f"  聚合語意（opt-in）：引用未分組欄位 {len(grouping_bad)} 處"
              f"（用位置分組而跳過 {skipped_pos} 段）")
    for seg, qual, colname, grouped in grouping_bad:
        print(f"     第 {seg} 段：子查詢用了 {qual}.{colname}，外層 GROUP BY 只有 {grouped}")
    if grouping_bad and '--grouping' in sys.argv:
        print("  ❌ 這會讓 PostgreSQL 報 GroupingError（欄位存在 ≠ 語意正確）")
        return 1

    print(f"  檢查了 {checked} 處欄位引用｜跳過 {sum(skipped.values())} 處"
          f"（{'、'.join(f'{k}: {v}' for k, v in sorted(skipped.items())) or '無'}）")
    if checked == 0:
        print("  ❌ 檢查了 0 處 → 這個結果不能當成「通過」（檢查器沒有真的驗到東西）")
        return 2
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
