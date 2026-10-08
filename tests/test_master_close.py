"""
T08 Kraken follower CLOSE (massive-rocket-algos spec 2026-10-06-t1-live-design.md sections 19.2-19.9 and 21).

The master sends POST /api/broadcast-close for its 168 h time exits. The follower records it once (master_closes),
creates one close_job per matched open_positions row (master_trade_id join only), and the trading loop runs each
job: cancel TP by id -> exit_hint='TIME' -> reduce-only close of min(venue, row remainder) -> verified flat ->
cancel SL by id. Invariants I1-I8 of 19.8 are pinned here; the shared planner is tested in test_close_common.py.

Every async path runs under asyncio.run() inside plain test functions, so the module needs no pytest plugin. The
endpoint tests need fastapi + sqlalchemy (requirements-gate.txt) and run against an in-memory SQLite database.
"""
import ast
import asyncio
import hashlib
import json
import os
import threading
from datetime import datetime, timedelta

import ccxt
import pytest

import close_guard as cg
import follower_guard
import hosted_trading_loop as htl
import position_guardian as pg
from hosted_trading_loop import HostedTradingLoop
from position_monitor import PositionMonitor
from tests.fakes import fast_sleep, guardian_env, new_user_id, notify_log  # noqa: F401  (pytest fixtures)
from tests.fakes.fake_close import CloseDB
from tests.fakes.fake_krakenfutures import FakeKrakenFutures
from tests.fakes.fake_pool import FakePool, FollowerDB

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYM = 'ADA/USD:USD'
MTID = '0123456789abcdef0123456789abcdef'
MTID2 = 'fedcba9876543210fedcba9876543210'
SECRET_TEXT = 'apiKey=Zq8Lr2Vx9Tn4Wm7Kp3Hs6Jd1Fg5Bc0Ny secret=Qw3Er5Ty7Ui9Op1As2Df4Gh6Jk8Lz0Xc'


def run(coro):
    return asyncio.run(coro)


class Env:
    pass


def _env(monkeypatch, tmp_path, **user_fields):
    monkeypatch.delenv('CLOSE_JOBS_ENABLED', raising=False)
    monkeypatch.delenv('CLOSE_SKIP_INACTIVE_USERS', raising=False)
    monkeypatch.delenv('GUARDIAN_MODE', raising=False)
    e = Env()
    e.pool = FakePool()
    e.db = FollowerDB(e.pool)
    e.cdb = CloseDB(e.db)
    e.user = e.db.add_user(new_user_id(), **user_fields)
    e.ex = FakeKrakenFutures(name=e.user['api_key'])
    e.loop = HostedTradingLoop(e.pool)
    e.loop.active_exchanges[e.user['api_key']] = e.ex
    e.log_path = str(tmp_path / 'close_events.jsonl')
    monkeypatch.setattr(htl, 'CLOSE_EVENTS', htl.make_close_event_log(e.log_path))
    return e


def _bracket(e, side='long', qty=100.0, row_qty=None, venue_qty=None, status='open'):
    exit_side = 'sell' if side == 'long' else 'buy'
    sl, tp = (0.49, 0.52) if side == 'long' else (0.51, 0.48)
    sl_oid = e.ex.add_order(SYM, 'stop', exit_side, qty, sl)
    tp_oid = e.ex.add_order(SYM, 'lmt', exit_side, qty, tp)
    e.ex.open_position(SYM, side, qty if venue_qty is None else venue_qty, 0.5)
    row = e.db.add_row(user_id=e.user['id'], side='BUY' if side == 'long' else 'SELL',
                       quantity=qty if row_qty is None else row_qty, target_sl=sl, target_tp=tp,
                       sl_order_id=sl_oid, tp_order_id=tp_oid, status=status)
    return row, sl_oid, tp_oid


def _orders(ex):
    return [(m, i) for m, i in ex.calls() if m in ('create_order', 'create_market_order', 'create_limit_order',
                                                   'cancel_order')]


def _creates(ex):
    return [i for m, i in ex.calls() if m in ('create_order', 'create_market_order', 'create_limit_order')]


def _events(e):
    if not os.path.exists(e.log_path):
        return []
    with open(e.log_path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def _names(e):
    return [ev['event'] for ev in _events(e)]


def _steps(e):
    return [ev.get('step_name') for ev in _events(e) if ev['event'] == cg.EV_JOB_STEP]


# ======================================================================================================
# execute_close (19.7): order, sizing and the I1/I3 money invariants
# ======================================================================================================

def test_close_long_cancels_tp_then_closes_reduce_only_then_cancels_sl(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, sl_oid, tp_oid = _bracket(e)
    e.cdb.add_job(row)
    run(e.loop.process_close_jobs())
    seq = [(m, i.get('id'), i.get('type'), i.get('side'), i.get('amount'), (i.get('params') or {}).get('reduceOnly'))
           for m, i in _orders(e.ex)]
    assert seq == [('cancel_order', tp_oid, None, None, None, None),
                   ('create_order', None, 'market', 'sell', 100.0, True),
                   ('cancel_order', sl_oid, None, None, None, None)]
    assert e.ex.positions == {} and e.ex.orders == {}
    r = e.db.row(row['id'])
    assert r['exit_hint'] == 'TIME' and r['close_fill_qty'] == 100.0
    assert r['status'] == 'open'                         # only position_monitor moves 'open' rows (I5)
    assert e.cdb.job(row['id'])['status'] == 'done'


def test_close_short_buys_back_reduce_only(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, sl_oid, tp_oid = _bracket(e, side='short', qty=80.0)
    e.cdb.add_job(row, side='SELL')
    run(e.loop.process_close_jobs())
    (info,) = _creates(e.ex)
    assert (info['type'], info['side'], info['amount'], info['params']) == ('market', 'buy', 80.0, {'reduceOnly': True})
    assert e.ex.positions == {} and e.cdb.job(row['id'])['status'] == 'done'


def test_every_ccxt_call_runs_off_the_event_loop_and_positions_are_read_no_arg(monkeypatch, tmp_path, fast_sleep,
                                                                              notify_log):
    e = _env(monkeypatch, tmp_path)
    row, _, _ = _bracket(e)
    e.cdb.add_job(row)
    run(e.loop.process_close_jobs())
    main = threading.main_thread().name
    assert e.ex.threads() and all(t != main for t in e.ex.threads())     # asyncio.to_thread for every call
    reads = e.ex.calls('fetch_positions')
    assert reads and all(info['symbols'] is None for _, info in reads)  # gotcha 2: no-arg + client-side match


def test_venue_larger_than_row_closes_only_the_row_and_keeps_the_sl(monkeypatch, tmp_path, fast_sleep, notify_log):
    """I1 + 19.7 step 7: qty = min(venue, row); the user's extra contracts are never closed."""
    e = _env(monkeypatch, tmp_path)
    row, sl_oid, tp_oid = _bracket(e, qty=100.0, venue_qty=150.0)
    e.cdb.add_job(row)
    run(e.loop.process_close_jobs())
    (info,) = _creates(e.ex)
    assert info['amount'] == 100.0 and info['params'] == {'reduceOnly': True}
    assert e.ex.positions[SYM]['contracts'] == 50.0
    assert sl_oid in e.ex.orders                          # not flat -> the SL stays (I3)
    job = e.cdb.job(row['id'])
    assert job['status'] == 'pending' and job['attempts'] == 1
    assert 'CLOSE_RESIDUAL' in e.db.error_types() and any('CLOSE_RESIDUAL' in n['title'] for n in notify_log)
    assert e.db.row(row['id'])['close_fill_qty'] == 100.0
    # next cycle: the row's share is closed; what is left is the user's own -> done, no second order, SL cancelled
    run(e.loop.process_close_jobs())
    assert len(_creates(e.ex)) == 1
    assert e.ex.positions[SYM]['contracts'] == 50.0
    assert sl_oid not in e.ex.orders
    assert e.cdb.job(row['id'])['status'] == 'done'
    assert cg.EV_RESIDUAL_NOT_OURS in _names(e)


@pytest.mark.parametrize('venue_side,payload_side', [('short', 'BUY'), ('long', 'SELL')])
def test_side_mismatch_fails_the_job_without_any_order(monkeypatch, tmp_path, fast_sleep, notify_log, venue_side,
                                                       payload_side):
    e = _env(monkeypatch, tmp_path)
    row, sl_oid, tp_oid = _bracket(e)                      # row BUY
    if venue_side == 'short':
        e.ex.positions[SYM]['side'] = 'short'
    e.cdb.add_job(row, side=payload_side)
    run(e.loop.process_close_jobs())
    assert _orders(e.ex) == []
    assert e.cdb.job(row['id'])['status'] == 'failed'
    assert 'CLOSE_SIDE_MISMATCH' in e.db.error_types()
    assert any('CLOSE_SIDE_MISMATCH' in n['title'] for n in notify_log)       # admin email
    assert cg.EV_SIDE_MISMATCH in _names(e)


def test_sl_fills_between_read_and_close_the_reduce_only_order_fills_nothing(monkeypatch, tmp_path, fast_sleep,
                                                                              notify_log):
    e = _env(monkeypatch, tmp_path)
    row, sl_oid, tp_oid = _bracket(e)
    e.cdb.add_job(row)
    real = e.ex.create_order

    def create_order(symbol, type, side, amount, price=None, params=None):
        e.ex.set_price(SYM, 0.489)                         # the SL triggers first: the account is flat
        return real(symbol, type, side, amount, price, params)

    monkeypatch.setattr(e.ex, 'create_order', create_order)
    run(e.loop.process_close_jobs())
    assert e.ex.positions == {}
    r = e.db.row(row['id'])
    assert not r.get('close_fill_qty')                    # nothing filled by the close: the monitor labels it SL
    assert e.cdb.job(row['id'])['status'] == 'done'
    assert 'CLOSE_RESIDUAL' not in e.db.error_types()


def test_tp_partial_fill_before_its_cancel_shrinks_the_close(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, sl_oid, tp_oid = _bracket(e)
    e.cdb.add_job(row)
    real = e.ex.cancel_order

    def cancel_order(id, symbol=None, params=None):
        if id == tp_oid:
            e.ex.positions[SYM]['contracts'] -= 40.0      # 40 of the TP filled just before the cancel
        return real(id, symbol, params)

    monkeypatch.setattr(e.ex, 'cancel_order', cancel_order)
    run(e.loop.process_close_jobs())
    (info,) = _creates(e.ex)
    assert info['amount'] == 60.0
    assert e.ex.positions == {} and e.db.row(row['id'])['close_fill_qty'] == 60.0
    assert e.cdb.job(row['id'])['status'] == 'done'


def test_accepted_but_not_flat_is_caught_by_verified_flat(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, sl_oid, tp_oid = _bracket(e)
    e.ex.ignore_reduce_only_market = True                  # Kraken accepts the order but nothing fills
    e.cdb.add_job(row)
    run(e.loop.process_close_jobs())
    assert len(_creates(e.ex)) == 1
    assert e.ex.positions[SYM]['contracts'] == 100.0
    assert sl_oid in e.ex.orders                           # never cancel the SL while not flat (I3)
    assert e.cdb.job(row['id'])['status'] == 'pending'
    assert 'CLOSE_RESIDUAL' in e.db.error_types()
    assert not e.db.row(row['id']).get('close_fill_qty')


def test_residual_after_max_attempts_fails_the_job_and_emails(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, sl_oid, tp_oid = _bracket(e)
    e.ex.ignore_reduce_only_market = True
    e.cdb.add_job(row, attempts=cg.CLOSE_JOB_MAX_ATTEMPTS - 1)
    run(e.loop.process_close_jobs())
    assert e.cdb.job(row['id'])['status'] == 'failed'
    assert sl_oid in e.ex.orders
    assert 'CLOSE_JOB_FAILED' in e.db.error_types()
    assert any('CLOSE_JOB_FAILED' in n['title'] for n in notify_log)


def test_failed_tp_cancel_stops_the_attempt_before_any_order(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, sl_oid, tp_oid = _bracket(e)
    e.ex.fault('cancel_order', ccxt.NetworkError('cancel timeout'), times=3)
    e.cdb.add_job(row)
    run(e.loop.process_close_jobs())
    assert _creates(e.ex) == [] and tp_oid in e.ex.orders and sl_oid in e.ex.orders
    job = e.cdb.job(row['id'])
    assert job['status'] == 'pending' and job['attempts'] == 1
    assert e.db.row(row['id']).get('exit_hint') is None   # exit_hint is written only right before the close


def test_failed_sl_cancel_after_flat_leaves_it_for_the_guardian(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, sl_oid, tp_oid = _bracket(e)
    e.ex.fault('cancel_order', ccxt.NetworkError('cancel timeout'), times=3, when=lambda info: info['id'] == sl_oid)
    e.cdb.add_job(row)
    run(e.loop.process_close_jobs())
    assert e.ex.positions == {} and sl_oid in e.ex.orders
    assert e.cdb.job(row['id'])['status'] == 'done'
    assert 'CLOSE_LEG_LEFT' in e.db.error_types() and cg.EV_LEG_LEFT in _names(e)


def test_venue_dust_under_one_lot_is_done_without_an_order(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, sl_oid, tp_oid = _bracket(e, venue_qty=0.5)      # ADA lot = 1 contract
    e.cdb.add_job(row)
    run(e.loop.process_close_jobs())
    assert _orders(e.ex) == []
    assert e.cdb.job(row['id'])['status'] == 'done' and cg.EV_DUST in _names(e)


def test_close_path_writes_only_exit_hint_close_fill_qty_and_close_jobs(monkeypatch, tmp_path, fast_sleep, notify_log):
    """I5: the close path never moves a row's status and never touches another table."""
    e = _env(monkeypatch, tmp_path)
    row, _, _ = _bracket(e, venue_qty=150.0)
    e.cdb.add_job(row)
    run(e.loop.process_close_jobs())
    run(e.loop.process_close_jobs())
    allowed = (' '.join(htl.SQL_CLOSE_JOB_SET.split()), ' '.join(htl.SQL_SET_EXIT_HINT.split()),
               ' '.join(htl.SQL_ADD_CLOSE_FILL.split()))
    muts = e.pool.mutations()
    assert muts and all(sql in allowed for _m, sql, _a in muts), [sql for _m, sql, _a in muts]


def test_an_unreadable_fill_after_the_close_records_no_fill(monkeypatch, tmp_path, fast_sleep, notify_log):
    """QC (review 2 #3): when the re-read after the close fails, the fill is unknown: record nothing (an over-count
    would mark the row share closed and the next attempt would cancel the SL of a position that is still open)."""
    e = _env(monkeypatch, tmp_path)
    e.ex.open_position(SYM, 'long', 100, 0.5)
    e.ex.ignore_reduce_only_market = True                  # accepted, nothing filled
    real = e.loop._get_open_position
    n = {'calls': 0}

    async def flaky(exchange, symbol):
        n['calls'] += 1
        if n['calls'] == 1:
            raise ccxt.NetworkError('read timed out')
        return await real(exchange, symbol)
    monkeypatch.setattr(e.loop, '_get_open_position', flaky)
    out = run(e.loop._close_existing_position(e.ex, SYM, 'sell', 100.0, venue_qty=100.0))
    assert out['sent'] is True and out['flat'] is False and out['filled'] == 0.0


def test_an_unreadable_fill_on_a_verified_flat_counts_the_close(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    e.ex.open_position(SYM, 'long', 100, 0.5)
    real = e.loop._get_open_position
    n = {'calls': 0}

    async def flaky(exchange, symbol):
        n['calls'] += 1
        if n['calls'] == 1:
            raise ccxt.NetworkError('read timed out')
        return await real(exchange, symbol)
    monkeypatch.setattr(e.loop, '_get_open_position', flaky)
    out = run(e.loop._close_existing_position(e.ex, SYM, 'sell', 100.0, venue_qty=100.0))
    assert out['flat'] is True and out['filled'] == 100.0


def test_close_existing_position_writes_no_rows_and_sends_one_reduce_only_order(monkeypatch, tmp_path, fast_sleep,
                                                                                notify_log):
    e = _env(monkeypatch, tmp_path)
    e.ex.open_position(SYM, 'long', 30, 0.5)
    out = run(e.loop._close_existing_position(e.ex, 'PF_ADAUSD', 'sell', 30.0))
    assert out['sent'] is True and out['flat'] is True and out['filled'] == 30.0
    (info,) = _creates(e.ex)
    assert info['params'] == {'reduceOnly': True}
    assert e.pool.calls == []                             # unlike _emergency_close_position: no needs_recovery row


# ======================================================================================================
# I4 idempotency, I8 read errors, needs_recovery, in-flight users, inactive users, kill switch, expiry
# ======================================================================================================

def test_done_job_is_never_run_again(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, _, _ = _bracket(e)
    e.cdb.add_job(row)
    run(e.loop.process_close_jobs())
    n = len(_orders(e.ex))
    run(e.loop.process_close_jobs())
    assert len(_orders(e.ex)) == n


def test_restart_mid_job_after_the_close_sends_no_second_order(monkeypatch, tmp_path, fast_sleep, notify_log):
    """The process died after the close filled but before the job was marked: the re-run sees the venue flat."""
    e = _env(monkeypatch, tmp_path)
    row, sl_oid, tp_oid = _bracket(e)
    e.ex.external_cancel(tp_oid)
    e.ex.positions.clear()
    e.db.row(row['id']).update(exit_hint='TIME', close_fill_qty=100.0)
    e.cdb.add_job(row)
    run(e.loop.process_close_jobs())
    assert _creates(e.ex) == []
    assert e.cdb.job(row['id'])['status'] == 'done'


def test_venue_read_error_never_acts(monkeypatch, tmp_path, fast_sleep, notify_log):
    """I8 (+ section 21 redaction): the read error is retried, never acted on, and its text is logged redacted."""
    e = _env(monkeypatch, tmp_path)
    row, sl_oid, tp_oid = _bracket(e)
    e.ex.fault('fetch_positions', ccxt.NetworkError('read timeout ' + SECRET_TEXT))
    e.cdb.add_job(row)
    run(e.loop.process_close_jobs())
    assert _orders(e.ex) == []
    job = e.cdb.job(row['id'])
    assert job['status'] == 'pending' and job['attempts'] == 1 and job['last_error']
    with open(e.log_path, encoding='utf-8') as f:
        text = f.read()
    for leaked in ('Zq8Lr2Vx9Tn4Wm7Kp3Hs6Jd1Fg5Bc0Ny', 'Qw3Er5Ty7Ui9Op1As2Df4Gh6Jk8Lz0Xc'):
        assert leaked not in text and leaked not in job['last_error']
        assert all(leaked not in str(err) for err in e.db.errors)


def test_credentials_that_do_not_decrypt_keep_the_job_pending(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, _, _ = _bracket(e)
    e.loop.active_exchanges.clear()
    monkeypatch.delenv('CREDENTIALS_ENCRYPTION_KEY', raising=False)
    e.cdb.add_job(row)
    run(e.loop.process_close_jobs())
    assert e.ex.calls() == []
    job = e.cdb.job(row['id'])
    assert job['status'] == 'pending' and job['attempts'] == 1


def test_needs_recovery_row_defers_until_the_lane_resolves_it(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, _, _ = _bracket(e, status='needs_recovery')
    e.cdb.add_job(row)
    run(e.loop.process_close_jobs())
    assert e.ex.calls() == []                             # deferred before any venue read
    assert e.cdb.job(row['id'])['status'] == 'deferred'
    e.db.row(row['id'])['status'] = 'open'                # the recovery lane restored it
    run(e.loop.process_close_jobs())
    assert len(_creates(e.ex)) == 1 and e.cdb.job(row['id'])['status'] == 'done'


def test_needs_recovery_row_closed_by_the_lane_ends_the_job(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, _, _ = _bracket(e, status='needs_recovery')
    e.cdb.add_job(row, status='deferred')
    e.db.row(row['id'])['status'] = 'closed_manual'
    run(e.loop.process_close_jobs())
    assert e.ex.calls() == [] and e.cdb.job(row['id'])['status'] == 'done'


def test_user_in_flight_is_skipped_and_retried_next_cycle(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, _, _ = _bracket(e)
    e.cdb.add_job(row)
    tok = follower_guard.mark(e.user['id'])
    try:
        run(e.loop.process_close_jobs())
    finally:
        follower_guard.clear(e.user['id'], tok)
    assert e.ex.calls() == []
    job = e.cdb.job(row['id'])
    assert job['status'] == 'pending' and job['attempts'] == 0
    skipped = [ev for ev in _events(e) if ev['event'] == cg.EV_JOB_SKIPPED]
    assert skipped and skipped[0]['reason'] == 'user_in_flight'
    run(e.loop.process_close_jobs())
    assert len(_creates(e.ex)) == 1


def test_execute_close_holds_the_users_in_flight_token(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, _, _ = _bracket(e)
    e.cdb.add_job(row)
    seen = []
    real = e.ex.create_order

    def create_order(*a, **k):
        seen.append(follower_guard.is_inflight(e.user['id']))
        return real(*a, **k)

    monkeypatch.setattr(e.ex, 'create_order', create_order)
    run(e.loop.process_close_jobs())
    assert seen == [True]
    assert follower_guard.is_inflight(e.user['id']) is False


@pytest.mark.parametrize('fields', [{'agent_active': False}, {'access_granted': False},
                                    {'pending_invoice_id': 'inv-1', 'invoice_due_date': datetime(2020, 1, 1)}])
def test_inactive_users_are_closed_by_default(monkeypatch, tmp_path, fast_sleep, notify_log, fields):
    """C-1a (owner 2026-10-06): a CLOSE only removes risk, so stopped/unpaid/no-access users are closed too."""
    e = _env(monkeypatch, tmp_path, **fields)
    row, _, _ = _bracket(e)
    e.cdb.add_job(row)
    run(e.loop.process_close_jobs())
    assert len(_creates(e.ex)) == 1 and e.cdb.job(row['id'])['status'] == 'done'


@pytest.mark.parametrize('fields', [{'agent_active': False}, {'access_granted': False},
                                    {'pending_invoice_id': 'inv-1', 'invoice_due_date': datetime(2020, 1, 1)}])
def test_inactive_users_are_skipped_with_an_alarm_when_the_owner_flips_the_flag(monkeypatch, tmp_path, fast_sleep,
                                                                                 notify_log, fields):
    e = _env(monkeypatch, tmp_path, **fields)
    monkeypatch.setenv('CLOSE_SKIP_INACTIVE_USERS', '1')
    row, _, _ = _bracket(e)
    e.cdb.add_job(row)
    run(e.loop.process_close_jobs())
    assert e.ex.calls() == []
    assert e.cdb.job(row['id'])['status'] == 'skipped'
    assert 'CLOSE_JOB_SKIPPED' in e.db.error_types()
    skipped = [ev for ev in _events(e) if ev['event'] == cg.EV_JOB_SKIPPED]
    assert skipped and skipped[0]['reason'] == 'inactive_user'


@pytest.mark.parametrize('fields', [{}, {'pending_invoice_id': 'inv-1'},
                                    {'pending_invoice_id': 'inv-1', 'invoice_due_date': datetime(2999, 1, 1)}])
def test_active_user_is_not_skipped_when_the_flag_is_set(monkeypatch, tmp_path, fast_sleep, notify_log, fields):
    e = _env(monkeypatch, tmp_path, **fields)
    monkeypatch.setenv('CLOSE_SKIP_INACTIVE_USERS', '1')
    row, _, _ = _bracket(e)
    e.cdb.add_job(row)
    run(e.loop.process_close_jobs())
    assert len(_creates(e.ex)) == 1


def test_kill_switch_records_nothing_and_acts_on_nothing_then_runs_the_backlog(monkeypatch, tmp_path, fast_sleep,
                                                                                notify_log):
    e = _env(monkeypatch, tmp_path)
    monkeypatch.setenv('CLOSE_JOBS_ENABLED', '0')
    row, _, _ = _bracket(e)
    e.cdb.add_job(row)
    run(e.loop.process_close_jobs())
    assert e.ex.calls() == [] and e.pool.sql(htl.SQL_CLOSE_JOBS_DUE) == []
    assert e.cdb.job(row['id'])['status'] == 'pending'
    monkeypatch.setenv('CLOSE_JOBS_ENABLED', '1')
    run(e.loop.process_close_jobs())
    assert len(_creates(e.ex)) == 1 and e.cdb.job(row['id'])['status'] == 'done'


def test_job_older_than_24h_expires_and_keeps_both_legs(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, sl_oid, tp_oid = _bracket(e)
    e.cdb.add_job(row, age_s=cg.CLOSE_JOB_MAX_AGE_S + 1)
    run(e.loop.process_close_jobs())
    assert e.ex.calls() == [] and sl_oid in e.ex.orders and tp_oid in e.ex.orders
    assert e.cdb.job(row['id'])['status'] == 'expired'
    assert 'CLOSE_JOB_EXPIRED' in e.db.error_types()
    assert any('CLOSE_JOB_EXPIRED' in n['title'] for n in notify_log)
    assert cg.EV_JOB_EXPIRED in _names(e)


def test_close_jobs_run_before_new_signals_each_cycle(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    run(e.loop.poll_and_execute())
    sqls = [sql for _m, sql, _a in e.pool.calls]
    i_close = next(i for i, s in enumerate(sqls) if 'FROM close_jobs cj' in s)
    i_signals = next(i for i, s in enumerate(sqls) if 'AND sd.acknowledged = false' in s)
    assert i_close < i_signals


def test_a_failing_close_job_query_never_blocks_new_signals(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    e.pool.fail_on('FROM close_jobs cj', RuntimeError('relation "close_jobs" does not exist'))
    run(e.loop.poll_and_execute())
    assert any('AND sd.acknowledged = false' in sql for _m, sql, _a in e.pool.calls)


def test_jobs_run_in_batches_of_25(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    peak = {'now': 0, 'max': 0}
    real = e.loop._run_close_job

    async def counting(job):
        peak['now'] += 1
        peak['max'] = max(peak['max'], peak['now'])
        await asyncio.sleep(0)
        peak['now'] -= 1

    monkeypatch.setattr(e.loop, '_run_close_job', counting)
    for _ in range(30):
        u = e.db.add_user(new_user_id())
        r = e.db.add_row(user_id=u['id'])
        e.cdb.add_job(r)
    run(e.loop.process_close_jobs())
    assert peak['max'] == htl.CLOSE_JOB_BATCH == 25
    assert real is not None


# ======================================================================================================
# Section 21 logging
# ======================================================================================================

def test_every_job_step_is_logged_with_the_envelope(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, _, _ = _bracket(e)
    e.cdb.add_job(row)
    run(e.loop.process_close_jobs())
    evs = _events(e)
    assert evs
    for ev in evs:
        assert all(k in ev for k in cg.REQUIRED_EVENT_FIELDS), ev
        assert ev['master_trade_id'] == MTID and ev['venue'] == 'kraken'
    assert _steps(e) == ['read_row', 'read_venue', 'cancel_tp', 'reread_venue', 'set_exit_hint', 'close',
                         'verify_flat', 'cancel_sl']
    outcome = [ev for ev in evs if ev['event'] == cg.EV_JOB_OUTCOME]
    assert outcome[-1]['status'] == 'done' and outcome[-1]['position_id'] == row['id']
    close = next(ev for ev in evs if ev.get('step_name') == 'close')
    assert close['qty'] == 100.0 and close['exit_side'] == 'sell' and close['reduce_only'] is True
    assert close['order_id'] and close['filled'] == 100.0


def test_no_follower_or_exchange_key_is_ever_logged(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, _, _ = _bracket(e)
    e.cdb.add_job(row)
    run(e.loop.process_close_jobs())
    with open(e.log_path, encoding='utf-8') as f:
        text = f.read()
    assert e.user['api_key'] not in text
    for line in text.splitlines():
        rec = {k: v for k, v in json.loads(line).items() if not cg.SAFE_HEX_KEY_RE.search(k)}   # code/config sha
        assert not cg.SECRET_VALUE_RE.search(json.dumps(rec)), rec


def test_close_flags_are_part_of_the_config_sha(monkeypatch, tmp_path):
    monkeypatch.delenv('CLOSE_JOBS_ENABLED', raising=False)
    monkeypatch.delenv('CLOSE_SKIP_INACTIVE_USERS', raising=False)
    a = htl.make_close_event_log(str(tmp_path / 'a.jsonl')).config_sha
    monkeypatch.setenv('CLOSE_SKIP_INACTIVE_USERS', '1')
    b = htl.make_close_event_log(str(tmp_path / 'b.jsonl')).config_sha
    assert a != b and htl.close_flags() == {'CLOSE_JOBS_ENABLED': True, 'CLOSE_SKIP_INACTIVE_USERS': True}


@pytest.mark.parametrize('raw,expected', [(None, True), ('', True), ('1', True), ('true', True), ('0', False),
                                          ('false', False), ('off', False), ('junk', True)])
def test_close_jobs_enabled_flag_parsing(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv('CLOSE_JOBS_ENABLED', raising=False)
    else:
        monkeypatch.setenv('CLOSE_JOBS_ENABLED', raw)
    assert htl.close_flags()['CLOSE_JOBS_ENABLED'] is expected


# ======================================================================================================
# position_monitor: TIME label (19.6)
# ======================================================================================================

def _monitor(monkeypatch, e, recorded):
    mon = PositionMonitor(e.pool)
    monkeypatch.setattr(mon, 'decrypt_credentials', lambda k, s: ('k', 's'))
    monkeypatch.setattr(mon, 'get_exchange', lambda *a: e.ex)

    async def no_pnl(*a, **k):
        return None

    async def record(position, exit_price, exit_type, closed_at, kraken_pnl=None):
        recorded.append(exit_type)
        return True

    async def no_scan(user_info):
        return []

    monkeypatch.setattr(mon, 'get_kraken_realized_pnl', no_pnl)
    monkeypatch.setattr(mon, 'record_trade_close', record)
    monkeypatch.setattr(mon, 'scan_exchange_fills', no_scan)
    return mon


def _mon_position(e, row):
    r = e.db.row(row['id'])
    return dict(r, user_api_key=e.user['api_key'], kraken_api_key_encrypted='enc', kraken_api_secret_encrypted='enc',
                fee_tier='standard', avg_entry_price=None, filled_quantity=None)


def test_monitor_labels_a_time_exit_and_cancels_both_legs(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, sl_oid, tp_oid = _bracket(e)
    e.ex.external_cancel(tp_oid)
    e.ex.create_order(SYM, 'market', 'sell', 100.0, None, {'reduceOnly': True})
    e.db.row(row['id']).update(exit_hint='TIME', close_fill_qty=100.0)
    recorded = []
    mon = _monitor(monkeypatch, e, recorded)
    run(mon.check_position(_mon_position(e, row)))
    assert recorded == ['TIME']
    assert sl_oid not in e.ex.orders
    assert {i['id'] for _m, i in e.ex.calls('cancel_order')} == {sl_oid, tp_oid}   # the TP is already gone: done


def test_monitor_keeps_the_distance_label_when_the_close_filled_nothing(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, sl_oid, tp_oid = _bracket(e)
    e.ex.external_cancel(tp_oid)
    e.ex.set_price(SYM, 0.489)                             # the SL filled; exit_hint was written, no close fill
    e.db.row(row['id']).update(exit_hint='TIME', close_fill_qty=None)
    recorded = []
    mon = _monitor(monkeypatch, e, recorded)
    run(mon.check_position(_mon_position(e, row)))
    assert recorded == ['SL']


def test_monitor_without_a_hint_is_unchanged(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, sl_oid, tp_oid = _bracket(e)
    e.ex.set_price(SYM, 0.53)                               # the TP filled (no OCO: the SL stays)
    recorded = []
    mon = _monitor(monkeypatch, e, recorded)
    run(mon.check_position(_mon_position(e, row)))
    assert recorded == ['TP']
    assert [i['id'] for _m, i in e.ex.calls('cancel_order')] == [sl_oid]


def test_monitor_reads_the_close_columns(monkeypatch, tmp_path):
    e = _env(monkeypatch, tmp_path)
    run(PositionMonitor(e.pool).get_open_positions())
    (_m, sql, _a), = e.pool.calls
    assert 'op.exit_hint' in sql and 'op.close_fill_qty' in sql


def test_monitor_tick_mid_close_records_one_time_trade(monkeypatch, tmp_path, fast_sleep, notify_log):
    e = _env(monkeypatch, tmp_path)
    row, sl_oid, tp_oid = _bracket(e)
    e.cdb.add_job(row)
    recorded = []
    mon = _monitor(monkeypatch, e, recorded)
    real = e.loop._cancel_order_verified

    async def hooked(exchange, oid, symbol):
        if oid == sl_oid and not recorded:
            await mon.check_position(_mon_position(e, row))   # the monitor ticks after the close, before SL cancel
        return await real(exchange, oid, symbol)

    monkeypatch.setattr(e.loop, '_cancel_order_verified', hooked)
    run(e.loop.process_close_jobs())
    run(e.loop.process_close_jobs())
    assert recorded == ['TIME']
    assert e.cdb.job(row['id'])['status'] == 'done' and e.ex.orders == {}


# ======================================================================================================
# position_guardian: an open close job makes the user busy (19.6)
# ======================================================================================================

def test_guardian_busy_rules_for_close_jobs(monkeypatch, notify_log):
    async def main():
        env = guardian_env(monkeypatch, mode='enforce')
        cdb = CloseDB(env.db)
        user, ex = env.user()
        row, _, _ = env.bracket(user, ex)
        assert await env.g._busy(user['id']) is False
        job = cdb.add_job(row)
        assert await env.g._busy(user['id']) is True                  # pending
        job.update(status='done', updated_at=env.clock.utcnow())
        assert await env.g._busy(user['id']) is True                  # done < CLOSE_BUSY_WINDOW_S ago
        env.clock.advance(cg.CLOSE_BUSY_WINDOW_S + 1)
        assert await env.g._busy(user['id']) is False
        job.update(status='deferred')
        assert await env.g._busy(user['id']) is False                 # the recovery lane must still run
        for status in ('failed', 'skipped', 'expired'):
            job.update(status=status)
            assert await env.g._busy(user['id']) is False
    run(main())


def test_guardian_close_jobs_read_error_is_not_busy(monkeypatch, notify_log):
    """QC (review 3 #4, as HL _close_busy): a failed close_jobs read (no table after a failed migration, a db blip)
    is NOT busy; otherwise every user would be busy on every pass and the Wave 1 guardian would silently stop."""
    async def main():
        env = guardian_env(monkeypatch, mode='enforce')
        user, ex = env.user()
        env.pool.fail_on('FROM close_jobs', RuntimeError('relation "close_jobs" does not exist'))
        assert await env.g._busy(user['id']) is False
    run(main())


def test_guardian_delivery_read_error_still_counts_as_busy(monkeypatch, notify_log):
    async def main():
        env = guardian_env(monkeypatch, mode='enforce')
        user, ex = env.user()
        env.pool.fail_on('signal_deliveries', RuntimeError('db down'))
        assert await env.g._busy(user['id']) is True                  # Wave 1 rule, unchanged
    run(main())


def test_guardian_full_pass_never_replaces_a_tp_the_close_cancelled(monkeypatch, notify_log):
    async def main():
        env = guardian_env(monkeypatch, mode='enforce')
        cdb = CloseDB(env.db)
        user, ex = env.user()
        row, sl_oid, tp_oid = env.bracket(user, ex)
        ex.external_cancel(tp_oid)                                  # execute_close cancelled the TP
        cdb.add_job(row)                                            # ... and the job is still pending
        await env.g.invariant_pass(full=True, dry=False)
        assert [c for c in ex.calls() if c[0] in ('create_order', 'cancel_order')] == []
    run(main())


# ======================================================================================================
# Static pins: shared Wave 1 files unchanged, main.py migration block only, models, gate config
# ======================================================================================================

def _sha_lf(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read().replace(b'\r\n', b'\n')).hexdigest()


@pytest.mark.parametrize('rel,sha', [
    ('signal_guard.py', 'ed2e51e48e43890dd6d96282e3a8aa38781f9b9bbdd4fc8cb5c272a2ae63b481'),
    ('follower_guard.py', '10ef3904273e8461b0cad2347bb6cd2eabdd2d96e9b2d9d5cb8c8c8b9e9d007e'),
    ('bracket_guard.py', '3af9754d61c4e2bc1bb357f1b637b9cc7b32f85dbd47bc9dd59578000071500a'),
])
def test_wave1_shared_files_are_byte_identical_to_ddb1e38(rel, sha):
    """I6: entry validation and the Wave 1 guards are untouched (sha of the ddb1e38 blob, CRLF->LF)."""
    assert _sha_lf(os.path.join(ROOT, rel)) == sha


MAIN_SHA_DDB1E38 = '646f1888c2b3baba3355eff76a9e86b41b6a0af1dc9cf04020c2b8f407b1ebaa'
MIG_START, MIG_END = '# T1-CLOSE MIGRATIONS:START', '# T1-CLOSE MIGRATIONS:END'


def _main_text():
    with open(os.path.join(ROOT, 'main.py'), 'rb') as f:
        return f.read().replace(b'\r\n', b'\n').decode('utf-8')


def test_main_py_changes_only_inside_the_migration_markers():
    lines = _main_text().split('\n')
    starts = [i for i, l in enumerate(lines) if l.strip() == MIG_START]
    ends = [i for i, l in enumerate(lines) if l.strip() == MIG_END]
    assert len(starts) == 1 and len(ends) == 1 and starts[0] < ends[0]
    rest = '\n'.join(lines[:starts[0]] + lines[ends[0] + 1:])
    assert hashlib.sha256(rest.encode('utf-8')).hexdigest() == MAIN_SHA_DDB1E38


def _close_migrations():
    tree = ast.parse(_main_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == '_CLOSE_MIGRATIONS' for t in n.targets))
    return [' '.join(ast.literal_eval(n).split()) for n in node.value.elts]


def test_close_migrations_are_additive_and_idempotent():
    stmts = _close_migrations()
    joined = ' | '.join(stmts)
    for needle in ('ALTER TABLE signals ADD COLUMN IF NOT EXISTS master_trade_id VARCHAR(32)',
                   'CREATE UNIQUE INDEX IF NOT EXISTS ux_signals_master_trade_id ON signals(master_trade_id) '
                   'WHERE master_trade_id IS NOT NULL',
                   'ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS exit_hint VARCHAR(16)',
                   'ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS close_fill_qty NUMERIC',
                   'CREATE TABLE IF NOT EXISTS master_closes', 'CREATE TABLE IF NOT EXISTS close_jobs',
                   'position_id INTEGER PRIMARY KEY REFERENCES open_positions(id)'):
        assert needle in joined, needle
    for s in stmts:
        assert s.startswith(('ALTER TABLE ', 'CREATE TABLE IF NOT EXISTS ', 'CREATE UNIQUE INDEX IF NOT EXISTS ',
                             'CREATE INDEX IF NOT EXISTS ')), s
        if s.startswith('ALTER TABLE '):
            assert ' ADD COLUMN IF NOT EXISTS ' in s and ' DROP ' not in s
        assert not any(w in s.upper() for w in ('DROP ', 'DELETE ', 'UPDATE ', 'TRUNCATE ', 'RENAME '))


def _model_columns(cls_name):
    with open(os.path.join(ROOT, 'follower_models.py'), encoding='utf-8') as f:
        tree = ast.parse(f.read())
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == cls_name)
    cols = {}
    table = None
    for s in cls.body:
        if isinstance(s, ast.Assign) and isinstance(s.targets[0], ast.Name):
            if s.targets[0].id == '__tablename__':
                table = ast.literal_eval(s.value)
            elif isinstance(s.value, ast.Call) and ast.unparse(s.value.func) == 'Column':
                cols[s.targets[0].id] = ast.unparse(s.value)
    return table, cols


def test_models_carry_the_close_columns_and_tables():
    _, sig = _model_columns('Signal')
    assert sig['master_trade_id'] == 'Column(String(32), nullable=True)'
    _, op = _model_columns('OpenPosition')
    assert op['exit_hint'] == 'Column(String(16), nullable=True)'
    assert op['close_fill_qty'] == 'Column(Numeric, nullable=True)'
    table, mc = _model_columns('MasterClose')
    assert table == 'master_closes'
    assert set(mc) == {'master_trade_id', 'symbol', 'side', 'reason', 'master_closed_ms', 'received_at', 'matched'}
    assert 'primary_key=True' in mc['master_trade_id']
    table, cj = _model_columns('CloseJob')
    assert table == 'close_jobs'
    assert set(cj) == {'position_id', 'user_id', 'master_trade_id', 'status', 'attempts', 'last_error', 'created_at',
                       'updated_at'}
    assert "ForeignKey('open_positions.id')" in cj['position_id'] and 'primary_key=True' in cj['position_id']


def test_gate_runs_the_close_tests_and_compares_the_close_files():
    import importlib.util
    spec = importlib.util.spec_from_file_location('kraken_run_gates', os.path.join(ROOT, 'tools', 'run_gates.py'))
    gates = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gates)
    assert 'tests/test_close_common.py' in gates.TESTS and 'tests/test_master_close.py' in gates.TESTS
    hl = gates.SHARED['hyperliquid-nike-rocket-api']
    for rel in ('close_guard.py', 'tests/test_close_common.py', 'tests/fixtures/close_v2.json'):
        assert rel in hl
    assert 'tests/fixtures/close_v2.json' in gates.SHARED['massive-rocket-algos']
    for rel in ('close_guard.py', 'position_monitor.py', 'follower_models.py'):
        assert rel in gates.COMPILE


# ======================================================================================================
# Endpoints (fastapi + sqlalchemy against in-memory SQLite; requirements-gate.txt)
# ======================================================================================================

FIXTURE = os.path.join(ROOT, 'tests', 'fixtures', 'close_v2.json')
KEY = 'test-master-key-not-real'


def _fixture():
    with open(FIXTURE, encoding='utf-8') as f:
        return json.load(f)


class Api:
    pass


def _api(monkeypatch, tmp_path, key=KEY):
    import fastapi
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import follower_endpoints as fe
    import follower_models as fm

    a = Api()
    a.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    fm.Base.metadata.create_all(a.engine)
    index = next(s for s in _close_migrations() if s.startswith('CREATE UNIQUE INDEX'))
    with a.engine.begin() as c:
        c.execute(text(index))
    a.Session = sessionmaker(bind=a.engine)

    def get_db():
        s = a.Session()
        try:
            yield s
        finally:
            s.close()

    app = fastapi.FastAPI()
    app.include_router(fe.router)
    app.dependency_overrides[fe.get_db] = get_db
    monkeypatch.setattr(fe, 'MASTER_API_KEY', key)
    monkeypatch.setattr(fe, '_now_ms', lambda: _fixture()['now_ms'])
    a.log_path = str(tmp_path / 'close_events.jsonl')
    monkeypatch.setattr(htl, 'CLOSE_EVENTS', htl.make_close_event_log(a.log_path))
    a.client = TestClient(app)
    a.fm, a.text = fm, text
    return a


def _q(a, sql, **params):
    with a.engine.connect() as c:
        return [tuple(r) for r in c.execute(a.text(sql), params).fetchall()]


def _close_payload(mtid=MTID, side='BUY', **over):
    d = {'protocol': 2, 'action': 'CLOSE', 'master_trade_id': mtid, 'symbol': 'ADA/USDT', 'side': side,
         'reason': 'time_exit', 'master_closed_ms': _fixture()['now_ms']}
    d.update(over)
    return d


def _post_close(a, payload, key=KEY):
    return a.client.post('/api/broadcast-close', json=payload, headers={'X-Master-Key': key} if key else {})


def _seed(a):
    """Users and rows for the I2 matching cases; returns {name: open_positions.id}."""
    fm = a.fm
    s = a.Session()
    users = {}
    for name in ('open', 'recovery', 'closed', 'own', 'legacy', 'skipped', 'other_trade'):
        u = fm.User(api_key='nk_%s_user' % name, email='%s@example.com' % name)
        s.add(u)
        s.flush()
        users[name] = u.id
    sig = fm.Signal(signal_id='sig-v2', action='BUY', symbol='ADA/USDT', entry_price=0.5, stop_loss=0.49,
                    take_profit=0.52, leverage=2, risk_pct=0.01, master_trade_id=MTID)
    legacy = fm.Signal(signal_id='sig-v1', action='BUY', symbol='ADA/USDT', entry_price=0.5, stop_loss=0.49,
                       take_profit=0.52, leverage=2, risk_pct=0.01)
    other = fm.Signal(signal_id='sig-v2b', action='BUY', symbol='ADA/USDT', entry_price=0.5, stop_loss=0.49,
                      take_profit=0.52, leverage=2, risk_pct=0.01, master_trade_id=MTID2)
    s.add_all([sig, legacy, other])
    s.flush()
    rows = {}

    def row(name, signal_id, status='open'):
        op = fm.OpenPosition(user_id=users[name], signal_id=signal_id, entry_order_id='E-%s' % name,
                             symbol='ADA/USDT', kraken_symbol='PF_ADAUSD', side='BUY', quantity=100.0,
                             target_tp=0.52, target_sl=0.49, status=status)
        s.add(op)
        s.flush()
        rows[name] = op.id

    row('open', sig.id)
    row('recovery', sig.id, status='needs_recovery')
    row('closed', sig.id, status='closed')
    row('own', None)                                       # the user's own trade: NULL signal_id
    row('legacy', legacy.id)                               # pre-v2 entry
    row('other_trade', other.id)                           # a different master trade on the same symbol
    s.commit()
    s.close()
    return rows


def test_close_endpoint_503_when_no_master_key_is_configured(monkeypatch, tmp_path):
    a = _api(monkeypatch, tmp_path, key=None)
    r = _post_close(a, _close_payload(), key='anything')
    assert r.status_code == 503
    assert _q(a, 'SELECT COUNT(*) FROM master_closes') == [(0,)]


@pytest.mark.parametrize('key', [None, 'wrong-key'])
def test_close_endpoint_401_on_a_missing_or_wrong_key_and_writes_nothing(monkeypatch, tmp_path, key):
    a = _api(monkeypatch, tmp_path)
    r = _post_close(a, _close_payload(), key=key)
    assert r.status_code == 401
    assert _q(a, 'SELECT COUNT(*) FROM master_closes') == [(0,)]


def test_close_endpoint_contract_matrix_from_the_shared_fixture(monkeypatch, tmp_path):
    a = _api(monkeypatch, tmp_path)
    fx = _fixture()
    for case in fx['cases']:
        before = _q(a, 'SELECT COUNT(*) FROM master_closes')
        r = _post_close(a, case['payload'])
        assert r.status_code == case['expect'], (case['name'], r.status_code, r.text)
        if case['expect'] == 422:
            assert _q(a, 'SELECT COUNT(*) FROM master_closes') == before, case['name']
            assert r.json()['detail'].startswith(case['field']), (case['name'], r.json())


def test_close_endpoint_matches_only_by_master_trade_id_and_is_idempotent(monkeypatch, tmp_path):
    """I2 + I4: open and needs_recovery rows of THIS master trade only; a repeat creates nothing."""
    a = _api(monkeypatch, tmp_path)
    rows = _seed(a)
    r = _post_close(a, _close_payload())
    assert r.status_code == 200
    assert r.json() == {'accepted': True, 'duplicate': False, 'followers_matched': 2}
    jobs = _q(a, 'SELECT position_id, status, attempts, master_trade_id FROM close_jobs ORDER BY position_id')
    assert jobs == sorted([(rows['open'], 'pending', 0, MTID), (rows['recovery'], 'pending', 0, MTID)])
    assert _q(a, 'SELECT matched, side, reason FROM master_closes') == [(2, 'BUY', 'time_exit')]
    r2 = _post_close(a, _close_payload(master_closed_ms=_fixture()['now_ms'] - 1000))
    assert r2.status_code == 200
    assert r2.json() == {'accepted': True, 'duplicate': True, 'followers_matched': 2}
    assert len(_q(a, 'SELECT position_id FROM close_jobs')) == 2
    assert _q(a, 'SELECT COUNT(*) FROM master_closes') == [(1,)]
    # the close endpoint never touches a row (only the monitor moves 'open'; I5)
    assert _q(a, "SELECT COUNT(*) FROM open_positions WHERE status = 'open'") == [(4,)]


def test_close_endpoint_with_no_match_logs_close_no_match(monkeypatch, tmp_path):
    a = _api(monkeypatch, tmp_path)
    r = _post_close(a, _close_payload(mtid='a' * 32))
    assert r.status_code == 200 and r.json() == {'accepted': True, 'duplicate': False, 'followers_matched': 0}
    with open(a.log_path, encoding='utf-8') as f:
        evs = [json.loads(l) for l in f if l.strip()]
    assert [ev['event'] for ev in evs if ev['event'].startswith('CLOSE_')][-1] == cg.EV_NO_MATCH
    for ev in evs:
        assert all(k in ev for k in cg.REQUIRED_EVENT_FIELDS)


def test_close_endpoint_logs_receipt_and_jobs_and_never_the_master_key(monkeypatch, tmp_path, caplog):
    a = _api(monkeypatch, tmp_path)
    rows = _seed(a)
    caplog.set_level('DEBUG')
    _post_close(a, _close_payload())
    _post_close(a, _close_payload(), key='wrong-' + KEY)
    with open(a.log_path, encoding='utf-8') as f:
        text = f.read()
    evs = [json.loads(l) for l in text.splitlines() if l.strip()]
    received = next(ev for ev in evs if ev['event'] == cg.EV_RECEIVED)
    assert received['master_trade_id'] == MTID and received['followers_matched'] == 2
    assert sorted(received['position_ids']) == sorted([rows['open'], rows['recovery']])
    assert sorted(ev['position_id'] for ev in evs if ev['event'] == cg.EV_JOB_CREATED) == \
        sorted([rows['open'], rows['recovery']])
    assert KEY not in text and KEY not in caplog.text


def test_close_on_the_signal_endpoint_is_still_422(monkeypatch, tmp_path):
    a = _api(monkeypatch, tmp_path)
    body = dict(_close_payload(), entry_price=0.5, stop_loss=0.49, take_profit=0.52, leverage=2, risk_pct=0.01)
    r = a.client.post('/api/broadcast-signal', json=body, headers={'X-Master-Key': KEY})
    assert r.status_code == 422
    assert _q(a, 'SELECT COUNT(*) FROM signals') == [(0,)]


def _entry(**over):
    d = {'action': 'BUY', 'symbol': 'ADA/USDT', 'entry_price': 0.5, 'stop_loss': 0.49, 'take_profit': 0.52,
         'leverage': 2, 'risk_pct': 0.01}
    d.update(over)
    return d


def test_v2_entry_stores_master_trade_id_and_a_repeat_is_a_duplicate(monkeypatch, tmp_path):
    a = _api(monkeypatch, tmp_path)
    s = a.Session()
    s.add_all([a.fm.User(api_key='nk_a', email='a@example.com'), a.fm.User(api_key='nk_b', email='b@example.com')])
    s.commit()
    s.close()
    r = a.client.post('/api/broadcast-signal', json=_entry(protocol=2, master_trade_id=MTID),
                      headers={'X-Master-Key': KEY})
    assert r.status_code == 200 and r.json()['delivered_to'] == 2
    assert _q(a, 'SELECT master_trade_id FROM signals') == [(MTID,)]
    r2 = a.client.post('/api/broadcast-signal', json=_entry(protocol=2, master_trade_id=MTID),
                       headers={'X-Master-Key': KEY})
    assert r2.status_code == 200 and r2.json()['duplicate'] is True and r2.json()['delivered_to'] == 0
    assert _q(a, 'SELECT COUNT(*) FROM signals') == [(1,)]
    assert _q(a, 'SELECT COUNT(*) FROM signal_deliveries') == [(2,)]


@pytest.mark.parametrize('bad', ['0123456789ABCDEF0123456789ABCDEF', 'abc', '', 'g' * 32, 12])
def test_v2_entry_with_a_bad_master_trade_id_is_422_and_stores_nothing(monkeypatch, tmp_path, bad):
    a = _api(monkeypatch, tmp_path)
    r = a.client.post('/api/broadcast-signal', json=_entry(protocol=2, master_trade_id=bad),
                      headers={'X-Master-Key': KEY})
    assert r.status_code == 422
    assert _q(a, 'SELECT COUNT(*) FROM signals') == [(0,)]


def test_v1_entries_are_unchanged_and_never_collide(monkeypatch, tmp_path):
    a = _api(monkeypatch, tmp_path)
    for _ in range(2):
        r = a.client.post('/api/broadcast-signal', json=_entry(), headers={'X-Master-Key': KEY})
        assert r.status_code == 200 and 'duplicate' not in r.json()
    assert _q(a, 'SELECT master_trade_id FROM signals') == [(None,), (None,)]
