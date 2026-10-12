"""
One-off admin close of legacy (no master_trade_id) follower positions.

POST /api/admin/close-legacy-positions only adds a new way to SELECT rows; it creates close_jobs + one master_closes
row that the existing executor (hosted_trading_loop.execute_close) runs unchanged. Endpoint tests run against
in-memory SQLite (same harness as test_master_close.py); the executor test reuses that file's fake exchange.
"""
import json
from datetime import datetime

import pytest

import follower_endpoints as fe
from tests.fakes import fast_sleep, guardian_env, new_user_id, notify_log  # noqa: F401  (pytest fixtures)
from tests.test_master_close import (KEY, SYM, _api, _bracket, _env, _events, _q, run)

BODY = {'symbol': 'ADA/USDT', 'side': 'long', 'dry_run': True, 'reason': 'legacy_rollback_cleanup',
        'expected_count': 2}
URL = '/api/admin/close-legacy-positions'
BEFORE_EXPIRY = datetime(2026, 10, 12, 12, 0, 0)


def _post(a, body=None, key=KEY, **over):
    b = dict(BODY if body is None else body, **over)
    return a.client.post(URL, json=b, headers={'X-Master-Key': key} if key else {})


def _api_admin(monkeypatch, tmp_path, **kw):
    a = _api(monkeypatch, tmp_path, **kw)
    monkeypatch.setattr(fe, '_admin_now', lambda: BEFORE_EXPIRY)
    monkeypatch.delenv('CLOSE_JOBS_ENABLED', raising=False)
    return a


def _seed_legacy(a, with_noise=True):
    """Two legacy ADA longs (the real situation) plus rows that must never be selected."""
    fm = a.fm
    s = a.Session()

    def user(name, **kw):
        u = fm.User(api_key='nk_%s_0123456789' % name, email='%s@example.com' % name,
                    agent_active=kw.pop('agent_active', True), credentials_set=kw.pop('credentials_set', True), **kw)
        s.add(u)
        s.flush()
        return u.id

    def signal(sid, mtid=None):
        sg = fm.Signal(signal_id=sid, action='BUY', symbol='ADA/USDT', entry_price=0.25, stop_loss=0.2492,
                       take_profit=0.26158, leverage=2, risk_pct=0.01, master_trade_id=mtid)
        s.add(sg)
        s.flush()
        return sg.id

    legacy, t1 = signal('legacy-1'), signal('t1-1', 'a' * 32)
    rows = {}

    def row(name, uid, sig, side='BUY', symbol='ADA/USDT', status='open', qty=900.0, sl='SL-1', tp='TP-1'):
        op = fm.OpenPosition(user_id=uid, signal_id=sig, entry_order_id='E-%s' % name, symbol=symbol,
                             kraken_symbol='PF_ADAUSD', side=side, quantity=qty, entry_fill_price=0.2505,
                             target_tp=0.26158, target_sl=0.2492, sl_order_id=sl, tp_order_id=tp, status=status)
        s.add(op)
        s.flush()
        rows[name] = op.id

    u1, u2 = user('U8uPZTS6eRLz'), user('8ocDpJrYakpM')
    row('a', u1, legacy, qty=906.0)
    row('b', u2, legacy, qty=899.0)
    if with_noise:
        row('t1', user('t1user'), t1)                                    # T1 row, same symbol/side
        row('short', user('shortuser'), legacy, side='SELL')             # other side
        row('xrp', user('xrpuser'), legacy, symbol='XRP/USDT')           # other symbol
        row('closed', user('closeduser'), legacy, status='closed')       # not open
        row('inactive', user('stopped', agent_active=False), legacy)     # inactive user
        row('nosig', user('ownuser'), None)                              # no signal at all
    s.commit()
    s.close()
    return rows


def _counts(a):
    return (_q(a, 'SELECT COUNT(*) FROM master_closes'), _q(a, 'SELECT COUNT(*) FROM close_jobs'))


# ---------------------------------------------------------------- auth / expiry / body

def test_503_without_a_configured_master_key(monkeypatch, tmp_path):
    a = _api_admin(monkeypatch, tmp_path, key=None)
    assert _post(a, key='anything').status_code == 503


@pytest.mark.parametrize('key', [None, 'wrong-key'])
def test_auth_required_and_nothing_written(monkeypatch, tmp_path, key):
    a = _api_admin(monkeypatch, tmp_path)
    _seed_legacy(a)
    assert _post(a, key=key, dry_run=False).status_code == 401
    assert _counts(a) == ([(0,)], [(0,)])


def test_expired_endpoint_is_410_always_and_writes_nothing(monkeypatch, tmp_path):
    a = _api_admin(monkeypatch, tmp_path)
    _seed_legacy(a)
    for when in (datetime(2026, 10, 19, 0, 0, 0), datetime(2027, 1, 1)):
        monkeypatch.setattr(fe, '_admin_now', lambda when=when: when)
        for dry in (True, False):
            assert _post(a, dry_run=dry).status_code == 410
        assert _post(a, body={'junk': 1}).status_code == 410            # even a malformed body
    assert _counts(a) == ([(0,)], [(0,)])
    monkeypatch.setattr(fe, '_admin_now', lambda: datetime(2026, 10, 18, 23, 59, 59))
    assert _post(a).status_code == 200                                  # still open the second before


@pytest.mark.parametrize('over', [
    {'dry_run': 'false'}, {'dry_run': None}, {'side': 'BUY'}, {'side': 'both'}, {'symbol': 'ADA/USD:USD'},
    {'symbol': 'NOPE/USDT'}, {'reason': 'time_exit'}, {'expected_count': 0}, {'expected_count': '2'},
    {'expected_count': True}, {'expected_count': 51}, {'extra': 1}])
def test_bad_body_is_422_and_nothing_is_written(monkeypatch, tmp_path, over):
    a = _api_admin(monkeypatch, tmp_path)
    _seed_legacy(a)
    assert _post(a, **over).status_code == 422
    assert _counts(a) == ([(0,)], [(0,)])


def test_missing_field_and_non_object_body_are_422(monkeypatch, tmp_path):
    a = _api_admin(monkeypatch, tmp_path)
    _seed_legacy(a)
    b = dict(BODY)
    del b['dry_run']                                                    # dry_run has no default: explicit only
    assert _post(a, body=b).status_code == 422
    r = a.client.post(URL, content=b'[1]', headers={'X-Master-Key': KEY, 'content-type': 'application/json'})
    assert r.status_code == 422
    assert _counts(a) == ([(0,)], [(0,)])


# ---------------------------------------------------------------- dry run

def test_dry_run_lists_exactly_the_two_legacy_rows_and_writes_nothing(monkeypatch, tmp_path, caplog):
    a = _api_admin(monkeypatch, tmp_path)
    rows = _seed_legacy(a)
    caplog.set_level('DEBUG')
    r = _post(a)
    assert r.status_code == 200
    j = r.json()
    assert j['dry_run'] is True and j['count'] == 2 and 'jobs_created' not in j
    assert [x['position_id'] for x in j['rows']] == [rows['a'], rows['b']]
    first = j['rows'][0]
    assert first == {'position_id': rows['a'], 'user_key_prefix': 'nk_U8uPZ', 'symbol': 'ADA/USDT', 'side': 'long',
                     'recorded_qty': 906.0, 'entry_price': 0.2505, 'created_at': first['created_at'],
                     'sl_order_id_present': True, 'tp_order_id_present': True}
    assert first['created_at'] and j['rows'][1]['user_key_prefix'] == 'nk_8ocDp'
    text = json.dumps(j) + caplog.text
    assert 'nk_U8uPZTS6eRLz' not in text and 'nk_8ocDpJrYakpM' not in text and KEY not in text
    assert 'nk_U8uPZ_' not in text
    assert _counts(a) == ([(0,)], [(0,)])
    before = _q(a, 'SELECT id, status FROM open_positions ORDER BY id')
    _post(a)
    assert _q(a, 'SELECT id, status FROM open_positions ORDER BY id') == before


def test_dry_run_reports_missing_leg_ids(monkeypatch, tmp_path):
    a = _api_admin(monkeypatch, tmp_path)
    rows = _seed_legacy(a, with_noise=False)
    with a.engine.begin() as c:
        c.execute(a.text('UPDATE open_positions SET tp_order_id = NULL WHERE id = :i'), {'i': rows['b']})
    j = _post(a).json()
    assert [(x['sl_order_id_present'], x['tp_order_id_present']) for x in j['rows']] == [(True, True), (True, False)]


def test_needs_recovery_rows_are_selected_too(monkeypatch, tmp_path):
    a = _api_admin(monkeypatch, tmp_path)
    rows = _seed_legacy(a, with_noise=False)
    with a.engine.begin() as c:
        c.execute(a.text("UPDATE open_positions SET status = 'needs_recovery' WHERE id = :i"), {'i': rows['b']})
    assert _post(a).json()['count'] == 2


def test_the_short_side_selects_only_shorts(monkeypatch, tmp_path):
    a = _api_admin(monkeypatch, tmp_path)
    rows = _seed_legacy(a)
    j = _post(a, side='short', expected_count=1).json()
    assert [x['position_id'] for x in j['rows']] == [rows['short']] and j['rows'][0]['side'] == 'short'


# ---------------------------------------------------------------- refusals

def test_t1_row_is_excluded_and_never_counted(monkeypatch, tmp_path):
    a = _api_admin(monkeypatch, tmp_path)
    rows = _seed_legacy(a)
    ids = [x['position_id'] for x in _post(a).json()['rows']]
    assert rows['t1'] not in ids
    r = _post(a, expected_count=3)                                       # counting the T1 row would make 3
    assert r.status_code == 422 and r.json()['matched'] == 2
    assert _counts(a) == ([(0,)], [(0,)])


def test_a_t1_row_slipping_into_the_selection_refuses(monkeypatch, tmp_path):
    a = _api_admin(monkeypatch, tmp_path)
    _seed_legacy(a)
    real = fe.SQL_ADMIN_LEGACY_ROWS
    leaky = fe.text(str(real).replace('AND s.master_trade_id IS NULL', ''))
    monkeypatch.setattr(fe, 'SQL_ADMIN_LEGACY_ROWS', leaky)
    r = _post(a, dry_run=False, expected_count=3)
    assert r.status_code == 422 and 'T1' in r.json()['detail']
    assert _counts(a) == ([(0,)], [(0,)])


@pytest.mark.parametrize('n', [1, 3])
@pytest.mark.parametrize('dry', [True, False])
def test_count_mismatch_refuses_and_writes_nothing(monkeypatch, tmp_path, n, dry):
    a = _api_admin(monkeypatch, tmp_path)
    _seed_legacy(a)
    r = _post(a, dry_run=dry, expected_count=n)
    assert r.status_code == 422 and 'expected_count' in r.json()['detail']
    assert _counts(a) == ([(0,)], [(0,)])


def test_a_row_with_a_close_job_from_another_close_refuses(monkeypatch, tmp_path):
    a = _api_admin(monkeypatch, tmp_path)
    rows = _seed_legacy(a)
    with a.engine.begin() as c:
        c.execute(a.text("INSERT INTO master_closes (master_trade_id, symbol, side, reason, master_closed_ms, "
                         "received_at, matched) VALUES ('%s', 'ADA/USDT', 'BUY', 'time_exit', 1, :n, 1)" % ('b' * 32)),
                  {'n': BEFORE_EXPIRY})
        c.execute(a.text("INSERT INTO close_jobs (position_id, user_id, master_trade_id, status, attempts, "
                         "created_at, updated_at) VALUES (:p, 1, '%s', 'pending', 0, :n, :n)" % ('b' * 32)),
                  {'p': rows['a'], 'n': BEFORE_EXPIRY})
    r = _post(a, dry_run=False)
    assert r.status_code == 422
    assert _q(a, 'SELECT COUNT(*) FROM close_jobs') == [(1,)] and _q(a, 'SELECT COUNT(*) FROM master_closes') == [(1,)]


# ---------------------------------------------------------------- real call

def test_real_call_creates_two_jobs_in_one_transaction_then_is_idempotent(monkeypatch, tmp_path):
    a = _api_admin(monkeypatch, tmp_path)
    rows = _seed_legacy(a)
    r = _post(a, dry_run=False)
    assert r.status_code == 200
    j = r.json()
    assert j['accepted'] is True and j['duplicate'] is False and j['jobs_created'] == 2 and j['count'] == 2
    mtid = j['master_trade_id']
    assert len(mtid) == 32 and int(mtid, 16) >= 0 and mtid == fe._admin_mtid('ADA/USDT', 'BUY', [rows['a'], rows['b']])
    assert _q(a, 'SELECT position_id, user_id, master_trade_id, status, attempts FROM close_jobs ORDER BY position_id') \
        == [(rows['a'], 1, mtid, 'pending', 0), (rows['b'], 2, mtid, 'pending', 0)]
    assert _q(a, 'SELECT master_trade_id, symbol, side, reason, matched FROM master_closes') \
        == [(mtid, 'ADA/USDT', 'BUY', 'admin_legacy_close', 2)]
    # rows themselves are untouched (only the monitor moves 'open')
    assert _q(a, 'SELECT status FROM open_positions WHERE id IN (%d, %d)' % (rows['a'], rows['b'])) == [('open',), ('open',)]
    # second call: nothing new, says duplicate
    r2 = _post(a, dry_run=False)
    assert r2.status_code == 200
    assert r2.json()['duplicate'] is True and r2.json()['jobs_created'] == 0 and r2.json()['master_trade_id'] == mtid
    assert _counts(a) == ([(1,)], [(2,)])


def test_real_call_never_touches_other_rows(monkeypatch, tmp_path):
    a = _api_admin(monkeypatch, tmp_path)
    rows = _seed_legacy(a)
    _post(a, dry_run=False)
    jobbed = {r[0] for r in _q(a, 'SELECT position_id FROM close_jobs')}
    assert jobbed == {rows['a'], rows['b']}
    assert _q(a, 'SELECT COUNT(*) FROM trades') == [(0,)]


def test_events_use_reason_admin_legacy_close_and_never_log_keys(monkeypatch, tmp_path, caplog):
    a = _api_admin(monkeypatch, tmp_path)
    rows = _seed_legacy(a)
    caplog.set_level('DEBUG')
    _post(a, dry_run=False)
    _post(a, dry_run=False)
    with open(a.log_path, encoding='utf-8') as f:
        text = f.read()
    evs = [json.loads(line) for line in text.splitlines() if line.strip()]
    rec = next(e for e in evs if e['event'] == 'CLOSE_RECEIVED')
    assert rec['reason'] == 'admin_legacy_close' and sorted(rec['position_ids']) == sorted([rows['a'], rows['b']])
    created = [e for e in evs if e['event'] == 'CLOSE_JOB_CREATED']
    assert len(created) == 2 and all(e['reason'] == 'admin_legacy_close' for e in created)
    assert any(e['event'] == 'CLOSE_DUPLICATE' for e in evs)
    for secret in ('nk_U8uPZTS6eRLz', 'nk_8ocDpJrYakpM', KEY):
        assert secret not in text and secret not in caplog.text


def test_kill_switch_still_records_jobs_and_says_so(monkeypatch, tmp_path):
    a = _api_admin(monkeypatch, tmp_path)
    _seed_legacy(a)
    assert _post(a).json()['close_jobs_enabled'] is True
    monkeypatch.setenv('CLOSE_JOBS_ENABLED', '0')
    r = _post(a, dry_run=False)
    assert r.json()['close_jobs_enabled'] is False and r.json()['jobs_created'] == 2


def test_stale_selection_rolls_the_whole_call_back(monkeypatch, tmp_path):
    """A row that stops being legacy/open between the select and the insert -> 409, no master_closes, no jobs."""
    a = _api_admin(monkeypatch, tmp_path)
    rows = _seed_legacy(a)
    real = fe._admin_record

    def racing(db, mtid, symbol, side, sel, now):
        with a.engine.begin() as c:
            c.execute(a.text("UPDATE open_positions SET status = 'closed' WHERE id = :i"), {'i': rows['b']})
        return real(db, mtid, symbol, side, sel, now)

    monkeypatch.setattr(fe, '_admin_record', racing)
    r = _post(a, dry_run=False)
    assert r.status_code == 409
    assert _counts(a) == ([(0,)], [(0,)])


# ---------------------------------------------------------------- the existing executor runs the jobs unchanged

def test_executor_closes_a_legacy_job_via_the_existing_path(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, sl_oid, tp_oid = _bracket(e, side='long', qty=906.0)
    mtid = fe._admin_mtid('ADA/USDT', 'BUY', [row['id']])
    e.cdb.add_close(mtid, side='BUY', symbol='ADA/USDT')
    e.cdb.closes[mtid]['reason'] = 'admin_legacy_close'
    e.cdb.add_job(row, master_trade_id=mtid)
    run(e.loop.process_close_jobs())
    seq = [(m, i.get('id'), i.get('type'), i.get('side'), i.get('amount'), (i.get('params') or {}).get('reduceOnly'))
           for m, i in e.ex.calls() if m in ('create_order', 'cancel_order')]
    assert seq == [('cancel_order', tp_oid, None, None, None, None),
                   ('create_order', None, 'market', 'sell', 906.0, True),
                   ('cancel_order', sl_oid, None, None, None, None)]
    assert e.ex.positions == {} and e.ex.orders == {}
    assert e.cdb.job(row['id'])['status'] == 'done'
    assert e.db.row(row['id'])['close_fill_qty'] == 906.0
