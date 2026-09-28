"""
T24 static half (spec C38, R3-M4): fastapi-free AST checks of follower_endpoints.py, so the
endpoint rules are proven even where fastapi cannot be installed.
"""
import ast
import os

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'follower_endpoints.py')


def _tree():
    with open(SRC, encoding='utf-8') as f:
        return ast.parse(f.read())


def _function(tree, name):
    return next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _body_without_docstring(fn):
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], 'value', None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return body


def test_validation_is_the_first_statement_and_precedes_the_try():
    body = _body_without_docstring(_function(_tree(), 'broadcast_signal'))
    first, second = body[0], body[1]
    assert isinstance(first, ast.Assign) and isinstance(first.value, ast.Call)
    assert ast.unparse(first.value.func) == 'signal_guard.validate_signal_payload'
    assert [ast.unparse(a) for a in first.value.args] == [
        'signal.action', 'signal.entry_price', 'signal.stop_loss', 'signal.take_profit', 'signal.leverage',
        'signal.risk_pct']
    assert isinstance(second, ast.If)
    raised = [n for n in ast.walk(second) if isinstance(n, ast.Raise)]
    assert raised and 'status_code=422' in ast.unparse(raised[0])
    first_try = next(i for i, stmt in enumerate(body) if isinstance(stmt, ast.Try))
    assert first_try > 1


def test_signal_broadcast_risk_pct_is_a_required_float():
    cls = next(n for n in ast.walk(_tree()) if isinstance(n, ast.ClassDef) and n.name == 'SignalBroadcast')
    field = next(s for s in cls.body if isinstance(s, ast.AnnAssign) and ast.unparse(s.target) == 'risk_pct')
    assert ast.unparse(field.annotation) == 'float'
    assert field.value is None


# ---------------- SEC-2: no default master key, constant-time compare ----------------

def test_sec2_master_key_has_no_default_and_is_compared_in_constant_time():
    tree = _tree()
    assign = next(n for n in tree.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == 'MASTER_API_KEY' for t in n.targets))
    assert ast.unparse(assign.value) == "os.getenv('MASTER_API_KEY')"
    fn = _function(tree, 'verify_master_key')
    calls = [ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)]
    assert 'hmac.compare_digest' in calls
    assert not any(isinstance(n, ast.Compare) and any(isinstance(op, ast.NotEq) for op in n.ops)
                   for n in ast.walk(fn))
