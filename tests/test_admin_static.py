"""
Static checks of the admin surface (admin_dashboard.py, main.py): psycopg2/DB-free, so they run in the gate.

- QC regression-followers-4: info-severity guardian rows (GUARDIAN_YIELD_INFLIGHT, GUARDIAN_WOULD_*, ...)
  stay in error_logs (spec) but never count as errors on the dashboard tiles/stats.
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name):
    with open(os.path.join(ROOT, name), encoding='utf-8') as f:
        return f.read()


def _count_queries(src):
    """Every SQL string constant (implicit concatenation merged by the parser) that counts error_logs rows."""
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            sql = ' '.join(node.value.split())
            if 'FROM error_logs' in sql and 'COUNT(*)' in sql:
                out.append(sql)
    return out


def test_dashboard_error_counters_exclude_info_severity_guardian_rows():
    src = _src('admin_dashboard.py')
    queries = _count_queries(src)
    assert len(queries) >= 6                      # per-user 24 h, 1 h tile, total, 24 h, 7 d, by type
    for sql in queries:
        assert "COALESCE(context->>'severity', '') <> 'info'" in sql, sql
