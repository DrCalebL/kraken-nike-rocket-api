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
from tests.fakes.fake_close import CloseDB
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
    e.cdb = CloseDB(e.db)
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
    assert sl in e.ex.orders                                        # the SL is NOT cancelled on an unknown exit (H1)


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
    assert ex.positions[BNB]['contracts'] == 6.66 and ex.calls('create_limit_order') == []
    if mode == 'enforce':                                            # review M2: the stop is re-placed, nothing else
        (m, info), = env.order_calls(ex)
        assert m == 'create_order' and info['side'] == 'sell' and info['amount'] == 6.66
        assert info['params']['reduceOnly'] is True and info['params']['triggerPrice'] == 570.0
        assert env.db.row(row['id'])['sl_order_id'] in ex.orders
        assert 'GUARDIAN_HOLD_SL_REPLACED' in env.db.error_types()
    else:
        assert env.order_calls(ex) == []                             # legacy / shadow: alarm only
    if mode != 'legacy':
        assert 'GUARDIAN_HOLD_SL_MISSING' in env.db.error_types()


async def test_a_failed_hold_sl_replacement_is_an_alarm_never_a_close(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, _ = _hold_bracket(env, user, ex, with_sl=False)
    import ccxt
    ex.fault('create_order', ccxt.ExchangeError('rejected'), times=20)
    await env.g.invariant_pass(full=True, dry=False)
    assert ex.positions[BNB]['contracts'] == 6.66
    assert not any(i.get('params', {}).get('reduceOnly') and i['type'] == 'market' for _m, i in ex.calls('create_order'))
    assert 'GUARDIAN_HOLD_SL_REPLACE_FAILED' in env.db.error_types()


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
    assert "VALUES ($1, $2, $3, NULL, $4, $5, $6, $7, $8, $9, $10, NULL, $11, NOW(), 'needs_recovery', 'hold')" in loop
    assert "SET status = 'open', entry_order_id = $2, sl_order_id = $3" in loop      # the promotion keeps target_tp NULL


# ---------------- review H1: one empty read never touches a live hold ----------------

def test_one_empty_positions_read_leaves_a_live_hold_open_and_its_sl_untouched(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    row, sl = _hold_row(e)
    e.ex.empty_position_reads = 1                                   # the first read misses PF_BNBUSD
    run(e.mon.check_position(_position(e, row)))
    assert e.recorded == [] and e.db.row(row['id'])['status'] == 'open'
    assert sl in e.ex.orders and e.ex.calls('cancel_order') == [] and e.hold.sitouts == set()
    assert e.pool.sql("SET status = 'needs_review'") == []


def test_one_empty_read_with_a_trade_near_the_stop_is_not_labelled_sl(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    row, sl = _hold_row(e)
    e.ex.trades.append({'id': 't', 'order': 'x', 'timestamp': 1, 'symbol': BNB, 'side': 'sell', 'price': 569.0,
                        'amount': 1.0, 'info': {}})                 # an old fill near the stop
    e.ex.empty_position_reads = 1
    run(e.mon.check_position(_position(e, row)))
    assert e.recorded == [] and e.hold.sitouts == set() and sl in e.ex.orders
    assert e.db.row(row['id'])['status'] == 'open'


def test_a_confirmed_flat_hold_still_closes_after_the_second_read(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    row, sl = _hold_row(e)
    e.ex.set_price(BNB, 569.0)
    run(e.mon.check_position(_position(e, row)))
    assert e.recorded == [(row['id'], 'SL')]


async def test_enforce_closes_at_market_when_the_price_is_already_through_the_hold_stop(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, _ = _hold_bracket(env, user, ex, with_sl=False)
    ex.prices[BNB] = 560.0                                           # already beyond the 570 stop
    await env.g.invariant_pass(full=True, dry=False)
    assert ex.positions == {}
    (m, info), = env.order_calls(ex)
    assert info['type'] == 'market' and info['side'] == 'sell' and info['params']['reduceOnly'] is True
    assert info['amount'] == 6.66 and ex.orders == {}                # no stop was placed above the market
    assert 'GUARDIAN_HOLD_SL_CLOSED_BEYOND_STOP' in env.db.error_types()


async def test_enforce_places_the_stop_when_the_price_is_still_above_it(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, _ = _hold_bracket(env, user, ex, with_sl=False)
    ex.prices[BNB] = 571.0
    await env.g.invariant_pass(full=True, dry=False)
    assert ex.positions[BNB]['contracts'] == 6.66 and len(ex.orders) == 1


@pytest.mark.parametrize('mode', ['legacy', 'shadow'])
async def test_legacy_and_shadow_never_close_a_hold_that_is_through_its_stop(monkeypatch, notify_log, mode):
    env = guardian_env(monkeypatch, mode=mode)
    user, ex = env.user()
    row, _ = _hold_bracket(env, user, ex, with_sl=False)
    ex.prices[BNB] = 560.0
    await _every_mode_pass(env, mode)
    assert env.order_calls(ex) == [] and ex.positions[BNB]['contracts'] == 6.66


async def test_a_hold_closed_beyond_its_stop_is_then_labelled_sl_by_the_monitor(monkeypatch, notify_log, fast_sleep):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, _ = _hold_bracket(env, user, ex, with_sl=False)
    ex.prices[BNB] = 560.0
    await env.g.invariant_pass(full=True, dry=False)
    assert pm.hold_exit_type(dict(env.db.row(row['id'])), 560.0) == 'SL'


async def test_the_beyond_stop_decision_uses_a_fresh_ticker_not_the_pass_cache(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, _ = _hold_bracket(env, user, ex, with_sl=False)
    ex.prices[BNB] = 560.0                                           # the market is through the stop NOW

    async def stale(exchange, sym):
        return 700.0                                                 # the cached value from earlier in the pass

    monkeypatch.setattr(env.g, '_ticker_last', stale)
    await env.g.invariant_pass(full=True, dry=False)
    assert ex.positions == {} and 'GUARDIAN_HOLD_SL_CLOSED_BEYOND_STOP' in env.db.error_types()


# ---------------- audit 2: a stale position dict never mislabels a leader exit ----------------

def _stale(e, row):
    return dict(_position(e, row), exit_hint=None, close_fill_qty=None)       # what the cycle loaded minutes ago


def test_a_leader_exit_written_after_the_cycle_loaded_the_row_is_still_labelled_leader(monkeypatch, fast_sleep,
                                                                                     notify_log):
    e = _env(monkeypatch)
    row, sl = _hold_row(e)
    stale = _stale(e, row)
    e.db.row(row['id']).update(exit_hint='LEADER', close_fill_qty=6.66)         # execute_close ran in between
    e.ex.create_order(BNB, 'market', 'sell', 6.66, None, {'reduceOnly': True})
    run(e.mon.check_position(stale))
    assert e.recorded == [(row['id'], 'LEADER')] and e.hold.sitouts == set()
    assert e.db.row(row['id'])['status'] == 'open' and e.pool.sql("SET status = 'needs_review'") == []


def test_the_same_race_on_an_ada_row_is_labelled_leader_and_time(monkeypatch, fast_sleep, notify_log):
    for hint, want in (('LEADER', 'LEADER'), ('TIME', 'TIME')):
        e = _env(monkeypatch)
        ada, sl, tp = _ada_row(e)
        stale = _stale(e, ada)
        e.db.row(ada['id']).update(exit_hint=hint, close_fill_qty=100.0)
        e.ex.external_cancel(tp)
        e.ex.create_order(ADA, 'market', 'sell', 100, None, {'reduceOnly': True})
        run(e.mon.check_position(stale))
        assert e.recorded == [(ada['id'], want)]


def test_while_the_master_close_is_still_running_the_label_is_deferred(monkeypatch, fast_sleep, notify_log):
    import follower_guard
    e = _env(monkeypatch)
    row, sl = _hold_row(e, exit_hint='LEADER', close_fill_qty=None)
    e.ex.create_order(BNB, 'market', 'sell', 6.66, None, {'reduceOnly': True})     # filled; the fill is not recorded yet
    tok = follower_guard.mark(e.user['id'])                                       # execute_close holds the user
    try:
        run(e.mon.check_position(_position(e, row)))
    finally:
        follower_guard.clear(e.user['id'], tok)
    assert e.recorded == [] and e.db.row(row['id'])['status'] == 'open' and sl in e.ex.orders
    assert e.pool.sql("SET status = 'needs_review'") == [] and e.hold.sitouts == set()


def test_a_pending_close_job_also_defers_and_a_finished_one_does_not(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    row, sl = _hold_row(e, exit_hint='LEADER', close_fill_qty=None)
    e.ex.create_order(BNB, 'market', 'sell', 6.66, None, {'reduceOnly': True})
    e.cdb.add_job(row, master_trade_id='a' * 32, side='BUY', status='pending')
    run(e.mon.check_position(_position(e, row)))
    assert e.recorded == []
    e.cdb.job(row['id']).update(status='failed')
    run(e.mon.check_position(_position(e, row)))                                  # no fill ever: the SL / distance label
    assert len(e.pool.sql("SET status = 'needs_review'")) == 1                   # undeferred: the unknown-exit review


def test_a_failed_label_read_defers(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    row, sl = _hold_row(e)
    e.ex.set_price(BNB, 569.0)
    e.pool.fail_on(pm.SQL_EXIT_LABEL, RuntimeError('db down'))
    run(e.mon.check_position(_position(e, row)))
    assert e.recorded == [] and e.db.row(row['id'])['status'] == 'open'


# ---------------- audit 3: a tracked row on one symbol never hides a naked rowless position on another ----------------

@pytest.mark.parametrize('mode', ['legacy', 'enforce'])
async def test_a_tracked_ada_row_does_not_hide_a_naked_rowless_bnb_position(monkeypatch, notify_log, mode):
    env = guardian_env(monkeypatch, mode=mode)
    user, ex = env.user()
    env.bracket(user, ex)                                              # a tracked ADA bracket (both leg ids)
    ex.open_position(BNB, 'long', 6.66, 600.0, ts_ms=env.clock.ms() - 900_000)       # naked, rowless
    await env.g._check_user_for_unprotected(user, rowless_only=(mode == 'enforce'))
    closes = [i for _m, i in ex.calls('create_order') if i.get('params', {}).get('reduceOnly')]
    assert [i['symbol'] for i in closes] == [BNB] and BNB not in ex.positions and ADA in ex.positions


async def test_a_tracked_row_on_the_same_symbol_still_protects_its_position(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='legacy')
    user, ex = env.user()
    row, sl, tp = env.bracket(user, ex)
    ex.external_cancel(sl)
    ex.external_cancel(tp)                                             # legs gone, but the row is tracked for this symbol
    await env.g._check_user_for_unprotected(user)
    assert ex.calls('create_order') == [] and ADA in ex.positions


async def test_an_unresolvable_row_symbol_falls_back_to_the_account_wide_rule_and_never_closes(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='legacy')
    user, ex = env.user()
    env.bracket(user, ex)                                              # a tracked ADA row
    ex.open_position(BNB, 'long', 6.66, 600.0, ts_ms=env.clock.ms() - 900_000)   # naked, rowless
    real_market = ex.market

    def flaky(symbol):
        if symbol == 'PF_ADAUSD':
            raise RuntimeError('market lookup failed')
        return real_market(symbol)

    monkeypatch.setattr(ex, 'market', flaky)
    await env.g._check_user_for_unprotected(user)
    assert ex.calls('create_order') == [] and BNB in ex.positions      # unresolved: the old rule hides it, nothing closes
