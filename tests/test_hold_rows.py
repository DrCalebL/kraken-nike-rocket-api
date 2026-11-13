"""
T1LIVE-7 hold ROWS (kind='hold', target_tp NULL, no TP leg) in position_monitor and position_guardian, in every guardian
mode, plus the NULL-target_tp readers. A hold row is never closed, never TP-repaired, raises no TP_LEVEL_UNKNOWN; ADA
closes are recorded while a BNB hold is open (symbol-scoped position check).
"""
import asyncio
import os

import pytest

import hosted_trading_loop as htl
import position_guardian as pg
import position_monitor as pm
from hosted_trading_loop import HostedTradingLoop
from position_monitor import PositionMonitor
from tests.fakes import fast_sleep, guardian_env, new_user_id, notify_log  # noqa: F401  (pytest fixtures)
from tests.fakes.fake_hold import HoldDB
from tests.fakes.fake_krakenfutures import FakeKrakenFutures
from tests.fakes.fake_pool import FakePool, FollowerDB

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADA = 'ADA/USD:USD'
BNB = 'BNB/USD:USD'


def run(coro):
    return asyncio.run(coro)


# ---------------- pure helpers ----------------

def test_hold_exit_type_labels():
    pos = dict(target_sl=570.0, exit_hint=None, close_fill_qty=None)
    assert pm.hold_exit_type(pos, 569.0) == 'SL' and pm.hold_exit_type(pos, 570.0) == 'SL'
    assert pm.hold_exit_type(pos, 570.0 * 1.02) == 'SL'                 # stop-market slippage up to 2%
    assert pm.hold_exit_type(pos, 600.0) is None                        # neither the stop nor the leader exit
    assert pm.hold_exit_type(dict(pos, exit_hint='LEADER', close_fill_qty=6.0), 600.0) == 'LEADER'
    assert pm.hold_exit_type(dict(pos, exit_hint='LEADER', close_fill_qty=None), 600.0) is None   # hint, no fill
    assert pm.hold_exit_type(dict(pos, exit_hint='LEADER', close_fill_qty=0), 569.0) == 'SL'
    assert pm.hold_exit_type(dict(pos, target_sl=None), 569.0) is None
    assert pm.hold_exit_type(pos, None) is None and pm.hold_exit_type(pos, float('nan')) is None
    assert pm.hold_exit_type(dict(pos, exit_hint='TIME', close_fill_qty=6.0), 600.0) is None


def test_monitor_reads_the_kind_column():
    pool = FakePool()
    run(PositionMonitor(pool).get_open_positions())
    (_m, sql, _a), = pool.calls
    assert 'op.kind' in sql


# ---------------- monitor ----------------

class Env:
    pass


def _env(monkeypatch):
    for k in ('HOLD_SL_MATCH_PCT', 'GUARDIAN_MODE'):
        monkeypatch.delenv(k, raising=False)
    e = Env()
    e.pool = FakePool()
    e.db = FollowerDB(e.pool)
    e.hold = HoldDB(e.db)
    e.user = e.db.add_user(new_user_id())
    e.ex = FakeKrakenFutures(name=e.user['api_key'])
    e.recorded = []
    e.mon = PositionMonitor(e.pool)
    monkeypatch.setattr(e.mon, 'decrypt_credentials', lambda k, s: ('k', 's'))
    monkeypatch.setattr(e.mon, 'get_exchange', lambda *a: e.ex)

    async def no_pnl(*a, **k):
        return None

    async def record(position, exit_price, exit_type, closed_at, kraken_pnl=None):
        e.recorded.append((position['id'], exit_type))
        return True

    async def no_scan(user_info):
        return []

    monkeypatch.setattr(e.mon, 'get_kraken_realized_pnl', no_pnl)
    monkeypatch.setattr(e.mon, 'record_trade_close', record)
    monkeypatch.setattr(e.mon, 'scan_exchange_fills', no_scan)
    return e


def _hold_row(e, **over):
    sl = e.ex.add_order(BNB, 'stop', 'sell', 6.66, 570.0)
    e.ex.open_position(BNB, 'long', 6.66, 600.0)
    fields = dict(user_id=e.user['id'], symbol='BNB/USDT', kraken_symbol='PF_BNBUSD', kind='hold', quantity=6.66,
                  entry_fill_price=600.0, target_tp=None, target_sl=570.0, sl_order_id=sl, tp_order_id=None,
                  signal_id=88)
    fields.update(over)
    return e.db.add_row(**fields), sl


def _ada_row(e):
    sl = e.ex.add_order(ADA, 'stop', 'sell', 100, 0.49)
    tp = e.ex.add_order(ADA, 'lmt', 'sell', 100, 0.52)
    e.ex.open_position(ADA, 'long', 100, 0.5)
    return e.db.add_row(user_id=e.user['id'], sl_order_id=sl, tp_order_id=tp, signal_id=77), sl, tp


def _position(e, row):
    return dict(e.db.row(row['id']), user_api_key=e.user['api_key'], kraken_api_key_encrypted='enc',
                kraken_api_secret_encrypted='enc', fee_tier='standard', avg_entry_price=None, filled_quantity=None)


def test_a_stopped_out_hold_is_recorded_as_sl_and_the_user_sits_out(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.hold.mtid_of_signal[88] = 'M' * 32
    row, sl = _hold_row(e)
    e.ex.set_price(BNB, 569.0)                                      # the stop fills
    assert e.ex.positions == {}
    run(e.mon.check_position(_position(e, row)))
    assert e.recorded == [(row['id'], 'SL')]
    assert (e.user['id'], 'M' * 32) in e.hold.sitouts
    assert e.ex.calls('cancel_order') == []                         # nothing left to cancel: a hold has no TP leg


def test_a_leader_exit_is_labelled_leader_and_cancels_the_sl(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    row, sl = _hold_row(e, exit_hint='LEADER', close_fill_qty=6.66)
    e.ex.create_order(BNB, 'market', 'sell', 6.66, None, {'reduceOnly': True})     # execute_close's fill at 600
    run(e.mon.check_position(_position(e, row)))
    assert e.recorded == [(row['id'], 'LEADER')]
    assert sl not in e.ex.orders
    assert e.hold.sitouts == set()                                  # a leader exit is no stop-out


def test_a_hold_exit_that_is_neither_the_stop_nor_the_leader_goes_to_needs_review(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.hold.mtid_of_signal[88] = 'M' * 32
    row, sl = _hold_row(e)
    e.ex.create_order(BNB, 'market', 'sell', 6.66, None, {'reduceOnly': True})     # the user closed it by hand
    updates = []
    real = e.pool._dispatch

    async def spy(method, sql, args):
        if 'SET status = \'needs_review\'' in sql:
            updates.append(args)
        return await real(method, sql, args)

    e.pool._dispatch = spy
    run(e.mon.check_position(_position(e, row)))
    assert e.recorded == [] and updates == [(row['id'],)]
    assert (e.user['id'], 'M' * 32) in e.hold.sitouts
    assert sl not in e.ex.orders                                    # the leftover stop never outlives the position


def test_a_hold_with_a_null_tp_never_raises_in_the_monitor(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    row, sl = _hold_row(e)
    run(e.mon.check_position(_position(e, row)))                    # still open: nothing to do, nothing raises
    assert e.recorded == [] and 'POSITION_MONITOR_ERROR' not in e.db.error_types()
    assert e.db.row(row['id'])['status'] == 'open'


def test_an_ada_close_is_recorded_while_the_bnb_hold_is_open(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    ada, sl, tp = _ada_row(e)
    hold, _hsl = _hold_row(e)
    e.ex.set_price(ADA, 0.53)                                       # the ADA TP fills; the BNB hold stays open
    assert list(e.ex.positions) == [BNB]
    run(e.mon.check_position(_position(e, ada)))
    assert e.recorded == [(ada['id'], 'TP')]                        # was: "Position found (any symbol)" -> never closed
    run(e.mon.check_position(_position(e, hold)))
    assert e.recorded == [(ada['id'], 'TP')]                        # the hold is still open and untouched


def test_the_bnb_hold_close_is_recorded_while_ada_is_open(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    _ada_row(e)
    hold, sl = _hold_row(e)
    e.ex.set_price(BNB, 560.0)
    run(e.mon.check_position(_position(e, hold)))
    assert e.recorded == [(hold['id'], 'SL')]


def test_check_position_closed_is_symbol_scoped_on_both_reads(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.ex.open_position(BNB, 'long', 6.66, 600.0)
    res = run(e.mon.check_position_closed(e.ex, 'PF_ADAUSD', 'BUY', 100, 'tp', 'sl', 'u'))
    assert res['closed'] is True                                    # another symbol's position proves nothing
    res = run(e.mon.check_position_closed(e.ex, 'PF_BNBUSD', 'BUY', 6.66, None, 'sl', 'u'))
    assert res == {'closed': False, 'current_size': 6.66}
    e.ex.fault('fetch_positions', RuntimeError('boom'))             # first read fails -> the retry path
    res = run(e.mon.check_position_closed(e.ex, 'PF_ADAUSD', 'BUY', 100, 'tp', 'sl', 'u'))
    assert res.get('closed') is False and res.get('retry_no_positions') is True   # retry never closes on a miss


def test_an_ada_t1_row_is_labelled_leader_on_a_leader_exit(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    ada, sl, tp = _ada_row(e)
    e.db.row(ada['id']).update(exit_hint='LEADER', close_fill_qty=100.0)
    e.ex.external_cancel(tp)
    e.ex.create_order(ADA, 'market', 'sell', 100, None, {'reduceOnly': True})
    run(e.mon.check_position(_position(e, ada)))
    assert e.recorded == [(ada['id'], 'LEADER')] and sl not in e.ex.orders


# ---------------- guardian ----------------

def _hold_bracket(env, user, ex, with_sl=True, status='open'):
    sl = ex.add_order(BNB, 'stop', 'sell', 6.66, 570.0) if with_sl else None
    ex.open_position(BNB, 'long', 6.66, 600.0, ts_ms=env.clock.ms() - 605_000)
    row = env.row(user, symbol='BNB/USDT', kraken_symbol='PF_BNBUSD', kind='hold', quantity=6.66,
                  entry_fill_price=600.0, target_tp=None, target_sl=570.0, sl_order_id=sl, tp_order_id=None,
                  status=status)
    return row, sl


async def _every_mode_pass(env, mode):
    if mode == 'legacy':
        await env.g.startup_recovery()
        for cycle in (1, 2, 3, 4, 5):
            await env.g.run_cycle(cycle, 'legacy')
    else:
        for cycle in (1, 2, 3, 4, 5, 6):
            await env.g.run_cycle(cycle, mode)
        await env.g.invariant_pass(full=True, dry=(mode == 'shadow'))


@pytest.mark.parametrize('mode', ['legacy', 'shadow', 'enforce'])
async def test_an_open_hold_row_is_never_closed_or_repaired_in_any_mode(monkeypatch, notify_log, mode):
    env = guardian_env(monkeypatch, mode=mode)
    user, ex = env.user()
    row, sl = _hold_bracket(env, user, ex)
    await _every_mode_pass(env, mode)
    assert env.order_calls(ex) == [] and list(ex.orders) == [sl] and ex.positions[BNB]['contracts'] == 6.66
    assert env.pool.mutations() == []
    bad = [t for t in env.db.error_types() if any(x in t for x in ('TP_LEVEL_UNKNOWN', 'ROW_LEVELS_INVALID',
                                                                      'WOULD_PLACE_TP', 'WOULD_CLOSE', 'CLOSE'))]
    assert bad == []


@pytest.mark.parametrize('mode', ['legacy', 'shadow', 'enforce'])
async def test_a_hold_whose_stop_is_missing_is_alarmed_not_closed(monkeypatch, notify_log, mode):
    env = guardian_env(monkeypatch, mode=mode)
    user, ex = env.user()
    row, _ = _hold_bracket(env, user, ex, with_sl=False)
    await _every_mode_pass(env, mode)
    assert env.order_calls(ex) == [] and ex.positions[BNB]['contracts'] == 6.66
    if mode != 'legacy':
        assert 'GUARDIAN_HOLD_SL_MISSING' in env.db.error_types()


@pytest.mark.parametrize('mode', ['shadow', 'enforce'])
async def test_ada_and_a_hold_side_by_side_the_guardian_repairs_only_ada(monkeypatch, notify_log, mode):
    env = guardian_env(monkeypatch, mode=mode)
    user, ex = env.user()
    hold, hsl = _hold_bracket(env, user, ex)
    ada, asl, atp = env.bracket(user, ex)
    ex.external_cancel(atp)                                          # ADA lost its TP
    await env.g.invariant_pass(full=True, dry=(mode == 'shadow'))
    placed = [i for m, i in env.order_calls(ex) if m == 'create_order' or True]
    if mode == 'enforce':
        limits = ex.calls('create_order')
        assert len(limits) == 1 and limits[0][1]['symbol'] == ADA        # the ADA TP, nothing on BNB
        assert hsl in ex.orders
    else:
        assert env.order_calls(ex) == []
        assert any('WOULD_PLACE_TP' in t for t in env.db.error_types())
    assert placed is not None and ex.positions[BNB]['contracts'] == 6.66


async def test_an_unprotected_hold_in_needs_recovery_is_closed_reduce_only_never_given_a_tp(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, _ = _hold_bracket(env, user, ex, with_sl=False, status='needs_recovery')
    await env.g.invariant_pass(full=True, dry=False)
    assert ex.calls('create_limit_order') == []
    closes = [i for _m, i in ex.calls('create_order') if i.get('params', {}).get('reduceOnly')]
    assert closes and all(i['type'] == 'market' for i in closes)


# ---------------- NULL target_tp readers ----------------

def _src(name):
    with open(os.path.join(ROOT, name), encoding='utf-8') as f:
        return f.read()


def test_every_target_tp_reader_is_null_safe():
    import follower_guard as fg
    assert fg.rec_from_row({'side': 'BUY', 'target_sl': 570.0, 'target_tp': None}, 'last', 'x') is None
    admin = _src('admin_dashboard.py')
    assert "float(tp) if tp is not None else None" in admin and "if tp is not None else 0.0" in admin
    assert "if pos['tp'] is not None else '-'" in admin
    main = _src('main.py')
    assert "float(row['target_tp']) if row['target_tp'] else None" in main
    assert "pos.target_tp ?" in main
    # the monitor reads target_tp only for kind != 'hold'
    mon = _src('position_monitor.py')
    assert mon.index("hold = _is_hold(position)") < mon.index("tp = position['target_tp']")
    # the model allows NULL
    models = _src('follower_models.py')
    assert 'target_tp = Column(Float, nullable=True)' in models and 'take_profit = Column(Float, nullable=True)' in models


def test_every_ada_insert_still_writes_a_tp_and_only_the_hold_inserts_write_null():
    loop = _src('hosted_trading_loop.py')
    assert 'target_tp, target_sl, opened_at, status)\n                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)' in loop
    assert "VALUES ($1, $2, $3, NULL, $4, $5, $6, 'BUY', $7, $8, $9, NULL, $10, $11, 'open', 'hold')" in loop
