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


# ---------------- SEC-3 (QC security-2): admin password - no default, constant-time, never printed ----------------

def _tree(name):
    return ast.parse(_src(name))


def test_sec3_admin_password_has_no_default():
    assign = next(n for n in _tree('admin_dashboard.py').body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == 'ADMIN_PASSWORD' for t in n.targets))
    assert ast.unparse(assign.value) == "os.getenv('ADMIN_PASSWORD')"


def test_sec3_every_admin_check_is_constant_time_and_nothing_prints_the_password():
    tree = _tree('main.py')
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            assert 'ADMIN_PASSWORD' not in names, ast.unparse(node)          # no ==/!= on the password
        if isinstance(node, ast.Call) and ast.unparse(node.func) == 'print':
            assert 'ADMIN_PASSWORD' not in ast.unparse(node) and 'x_admin_key' not in ast.unparse(node)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'admin_password_ok')
    assert 'hmac.compare_digest' in [ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)]
    uses = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func) == 'admin_password_ok']
    assert len(uses) >= 21


def test_sec3_admin_password_ok_answers_503_while_unset_and_compares_exactly():
    tree = _tree('main.py')
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'admin_password_ok')

    class HTTPException(Exception):
        def __init__(self, status_code, detail=None):
            self.status_code = status_code

    import hmac
    from typing import Optional
    ns = {'hmac': hmac, 'HTTPException': HTTPException, 'Optional': Optional, 'ADMIN_PASSWORD': None}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), 'main.py', 'exec'), ns)
    try:
        ns['admin_password_ok']('anything')
        raise AssertionError('no 503 while ADMIN_PASSWORD is unset')
    except HTTPException as e:
        assert e.status_code == 503
    ns['ADMIN_PASSWORD'] = 's3cret-value'
    assert ns['admin_password_ok']('s3cret-value') is True
    for bad in ('', None, 's3cret-valu', 's3cret-value ', 'changeme123'):
        assert ns['admin_password_ok'](bad) is False


def test_sec3_root_listing_no_longer_advertises_admin_routes():
    fn = next(n for n in _tree('main.py').body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'root')
    text = ast.unparse(fn)
    assert 'reset-database' not in text and 'password=' not in text
