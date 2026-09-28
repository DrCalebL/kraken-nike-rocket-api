"""
T21 kraken_follower_post_entry_recovery (spec C34 items 7-9, C37 legacy edits, R3-H1, A7, I19, AC23).

Every execute_trade exit after the entry may have filled ends with an SL-protected 'open' row,
a verified-flat account with no service orders, or a needs_recovery row carrying the signal
levels, the qty and every known leg id. The legacy recovery gets its safety edits and the A7
"bracket intact -> restore, never close" check; enforce restores a completed row to 'open'.
"""
from datetime import timedelta

import ccxt
import pytest

import hosted_trading_loop as htl
import position_guardian as pg
from hosted_trading_loop import HostedTradingLoop
from tests.fakes import fast_sleep, guardian_env, new_user_id, notify_log  # noqa: F401  (pytest fixtures)
from tests.fakes.fake_krakenfutures import FakeKrakenFutures
from tests.fakes.fake_pool import FakePool, FollowerDB

SYM = 'ADA/USD:USD'


def _trading(monkeypatch):
    monkeypatch.delenv('GUARDIAN_MODE', raising=False)
    pool = FakePool()
    db = FollowerDB(pool)
    user = db.add_user(new_user_id())
    ex = FakeKrakenFutures(name=user['api_key'])
    loop = HostedTradingLoop(pool)
    loop.active_exchanges[user['api_key']] = ex
    signal = {'delivery_id': None, 'signal_id': 'sig-%d' % user['id'], 'action': 'BUY', 'symbol': 'ADA/USDT',
              'entry_price': 0.5, 'stop_loss': 0.49, 'take_profit': 0.52, 'leverage': 5, 'risk_pct': 0.0125}
    return loop, db, pool, ex, user, signal


# ---------------- needs_recovery plumbing (C34 items 8-9) ----------------

async def test_record_unprotected_position_default_call_sites_write_null_ids(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _trading(monkeypatch)
    ex.open_position(SYM, 'long', 50, 0.5)
    ex.fault('create_order', ccxt.NetworkError('close timeout'), times=5)
    ok = await loop._emergency_close_position(
        ex, 'PF_ADAUSD', 'sell', 50, 'u@e.com', user['api_key'], 'E1', reason='SL order failed after all retries',
        user_id=user['id'], signal_db_id=77, signal_symbol='ADA/USDT', side='BUY', leverage=5,
        entry_fill_price=0.5, target_tp=0.52, target_sl=0.49)
    assert ok is False
    (method, sql, args), = pool.sql('INSERT INTO open_positions')
    assert args[3] is None and args[4] is None            # tp_order_id, sl_order_id -> NULL as before
    assert "NOW(), 'needs_recovery'" in sql
    assert 'NEEDS_RECOVERY_RECORDED' in db.error_types()


async def test_emergency_close_failure_records_the_known_leg_ids(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _trading(monkeypatch)
    ex.open_position(SYM, 'long', 50, 0.5)
    ex.fault('create_order', ccxt.NetworkError('close timeout'), times=5)
    await loop._emergency_close_position(
        ex, 'PF_ADAUSD', 'sell', 50, 'u@e.com', user['api_key'], 'E1', reason='TP order failed after all retries',
        user_id=user['id'], signal_db_id=77, signal_symbol='ADA/USDT', side='BUY', leverage=5,
        entry_fill_price=0.5, target_tp=0.52, target_sl=0.49, sl_order_id='sl-1')
    row, = [r for r in db.rows if r['user_id'] == user['id']]
    assert row['status'] == 'needs_recovery' and row['sl_order_id'] == 'sl-1' and row['tp_order_id'] is None


async def test_needs_recovery_insert_failure_notifies(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _trading(monkeypatch)
    pool.fail_on('INSERT INTO open_positions', RuntimeError('insert rejected'))
    await loop._record_unprotected_position(
        user_id=user['id'], user_api_key=user['api_key'], signal_db_id=77, entry_order_id='E1',
        signal_symbol='ADA/USDT', kraken_symbol='PF_ADAUSD', side='BUY', quantity=50, reason='X')
    assert any(n['title'].startswith('NEEDS_RECOVERY_RECORD_FAILED') and n['level'] == 'error' for n in notify_log)


async def test_verified_flat_needs_two_flat_reads(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _trading(monkeypatch)
    assert await loop._verified_flat(ex, 'PF_ADAUSD') is True
    assert len(ex.calls('fetch_positions')) == 2


async def test_verified_flat_is_false_on_a_position_a_late_position_or_a_read_error(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _trading(monkeypatch)
    ex.open_position(SYM, 'long', 50, 0.5)
    assert await loop._verified_flat(ex, 'PF_ADAUSD') is False
    ex.empty_position_reads = 1                           # first read false-empty, the second shows it
    assert await loop._verified_flat(ex, 'PF_ADAUSD') is False
    ex.fault('fetch_positions', ccxt.NetworkError('read timeout'))
    assert await loop._verified_flat(ex, 'PF_ADAUSD') is False


async def test_cancel_order_verified_treats_not_found_as_done_and_gives_up_after_3(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _trading(monkeypatch)
    ex.open_position(SYM, 'long', 50, 0.5)
    oid = ex.add_order(SYM, 'stop', 'sell', 50, 0.49)
    assert await loop._cancel_order_verified(ex, oid, 'PF_ADAUSD') is True and oid not in ex.orders
    assert await loop._cancel_order_verified(ex, 'no-such-order', 'PF_ADAUSD') is True
    stuck = ex.add_order(SYM, 'stop', 'sell', 50, 0.49)
    ex.fault('cancel_order', ccxt.NetworkError('cancel timeout'), times=3)
    assert await loop._cancel_order_verified(ex, stuck, 'PF_ADAUSD') is False and stuck in ex.orders


# ---------------- execute_trade: exceptions after the entry was sent (C34 item 7, R3-H1) ----------------

@pytest.mark.parametrize('exc', [ccxt.InsufficientFunds('margin'), ccxt.InvalidOrder('bad'),
                                 ccxt.AuthenticationError('auth'), TypeError('log line')])
async def test_post_entry_exception_in_each_branch_records_needs_recovery(monkeypatch, fast_sleep, notify_log, exc):
    loop, db, pool, ex, user, signal = _trading(monkeypatch)
    real = ex.amount_to_precision
    calls = {'n': 0}

    def amount_to_precision(symbol, amount):
        calls['n'] += 1
        if calls['n'] == 2:                               # the bracket_qty computation, after the entry
            raise exc
        return real(symbol, amount)

    monkeypatch.setattr(ex, 'amount_to_precision', amount_to_precision)
    assert await loop.execute_trade(user, signal) is False
    row, = [r for r in db.rows if r['user_id'] == user['id']]
    assert row['status'] == 'needs_recovery'
    assert (row['target_sl'], row['target_tp'], row['quantity']) == (0.49, 0.52, 12500.0)
    assert row['sl_order_id'] is None and row['tp_order_id'] is None
    assert 'POST_ENTRY_EXCEPTION' in db.error_types()
    assert any(e[0] == 'NEEDS_RECOVERY_RECORDED' and e[1] == 'POST_ENTRY_EXCEPTION:%s' % type(exc).__name__
               for e in db.errors)
    assert any(n['level'] == 'error' for n in notify_log)


async def test_post_entry_exception_after_the_sl_records_the_sl_id(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _trading(monkeypatch)

    async def tp_raises(**kwargs):
        raise TypeError('boom after the SL')

    monkeypatch.setattr(htl, 'place_tp_order_with_retry', tp_raises)
    assert await loop.execute_trade(user, signal) is False
    sl_oid, = list(ex.orders)
    row, = [r for r in db.rows if r['user_id'] == user['id']]
    assert row['status'] == 'needs_recovery' and row['sl_order_id'] == sl_oid


async def test_post_entry_exception_verified_flat_cancels_the_placed_legs(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _trading(monkeypatch)

    async def tp_raises(**kwargs):
        ex.positions.clear()                              # the position is gone (e.g. closed by the user)
        raise TypeError('boom after the SL')

    monkeypatch.setattr(htl, 'place_tp_order_with_retry', tp_raises)
    assert await loop.execute_trade(user, signal) is False
    assert ex.orders == {}                                # the service SL was cancelled
    assert [r for r in db.rows if r['user_id'] == user['id']] == []


async def test_post_entry_exception_verified_flat_failed_cancel_records_leftover_ids(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _trading(monkeypatch)
    ex.fault('cancel_order', ccxt.NetworkError('cancel timeout'), times=3)

    async def tp_raises(**kwargs):
        ex.positions.clear()
        raise TypeError('boom after the SL')

    monkeypatch.setattr(htl, 'place_tp_order_with_retry', tp_raises)
    assert await loop.execute_trade(user, signal) is False
    sl_oid, = list(ex.orders)
    row, = [r for r in db.rows if r['user_id'] == user['id']]
    assert row['status'] == 'needs_recovery' and row['sl_order_id'] == sl_oid
    assert any(e[0] == 'NEEDS_RECOVERY_RECORDED' and e[1] == 'POST_ENTRY_EXCEPTION_LEFTOVER' for e in db.errors)


# ---------------- legacy _recover_single_position with the safety edits (C37, A7) ----------------

def _recovery_row(env, user, **fields):
    fields.setdefault('status', 'needs_recovery')
    return env.row(user, **fields)


def _nr_view(env, row):
    return next(r for r in env.db._needs_recovery_rows('fetch', '', ()) if r['id'] == row['id'])


async def test_legacy_recovery_skips_only_while_a_later_delivery_is_acknowledged(monkeypatch, notify_log):
    env = guardian_env(monkeypatch)
    user, ex = env.user()
    row = _recovery_row(env, user, opened_at=env.clock.utcnow() - timedelta(seconds=30))
    ex.open_position(SYM, 'long', 100, 0.5)
    env.db.ack(user['id'], env.clock.utcnow() - timedelta(seconds=60))   # acknowledged BEFORE the row: ignored
    await env.g._recover_single_position(_nr_view(env, row))
    assert env.db.row(row['id'])['status'] == 'closed'                   # acted at once (today's timing)
    row2 = _recovery_row(env, user, opened_at=env.clock.utcnow() - timedelta(seconds=30))
    ex.open_position(SYM, 'long', 100, 0.5)
    env.db.ack(user['id'], env.clock.utcnow() - timedelta(seconds=5))    # a LATER delivery: busy
    before = len(ex.calls())
    await env.g._recover_single_position(_nr_view(env, row2))
    assert len(ex.calls()) == before and env.db.row(row2['id'])['status'] == 'needs_recovery'


async def test_legacy_recovery_is_symbol_matched_and_confirms_flat_after_5s(monkeypatch, notify_log):
    env = guardian_env(monkeypatch)
    user, ex = env.user()
    row = _recovery_row(env, user, sl_order_id='ord-sl-old', tp_order_id=None)
    ex.open_position('BTC/USD:USD', 'long', 0.01, 60000)                # another symbol: never 'the position'
    ex.add_order(SYM, 'stop', 'sell', 100, 0.49, oid='ord-sl-old')
    await env.g._recover_single_position(_nr_view(env, row))
    assert pg.LEGACY_FLAT_CONFIRM_S in env.sleep.delays                 # confirming re-read 5 s later
    assert env.db.row(row['id'])['status'] == 'closed_manual'
    assert 'ord-sl-old' not in ex.orders                                # the row's own leg was cancelled
    assert 'BTC/USD:USD' in ex.positions                                # the BTC position was not touched
    (_, sql, args), = env.pool.sql('SET status = $1, last_fill_at = NOW()')
    assert "AND status = 'needs_recovery'" in sql and args[0] == 'closed_manual'


async def test_legacy_recovery_single_empty_read_is_not_flat(monkeypatch, notify_log):
    env = guardian_env(monkeypatch)
    user, ex = env.user()
    row = _recovery_row(env, user, sl_order_id='ord-sl', tp_order_id=None)
    ex.open_position(SYM, 'long', 100, 0.5)
    ex.add_order(SYM, 'stop', 'sell', 100, 0.49, oid='ord-sl')
    ex.empty_position_reads = 1                                         # one false-empty read
    await env.g._recover_single_position(_nr_view(env, row))
    assert env.db.row(row['id'])['status'] == 'closed'                  # the re-read saw it: closed, not closed_manual
    assert ex.positions == {} and 'ord-sl' not in ex.orders             # closed, then the row's SL cancelled


async def test_legacy_recovery_another_active_row_is_ambiguous(monkeypatch, notify_log):
    env = guardian_env(monkeypatch)
    user, ex = env.user()
    row = _recovery_row(env, user)
    env.row(user, status='open')
    ex.open_position(SYM, 'long', 100, 0.5)
    await env.g._recover_single_position(_nr_view(env, row))
    assert ex.calls() == [] and 'GUARDIAN_ROW_AMBIGUOUS' in env.db.error_types()
    assert env.db.row(row['id'])['status'] == 'needs_recovery'


async def test_legacy_recovery_restores_an_intact_bracket_instead_of_closing(monkeypatch, notify_log):
    env = guardian_env(monkeypatch)
    user, ex = env.user()
    row = _recovery_row(env, user, sl_order_id='ord-sl', tp_order_id='ord-tp', quantity=100.0)
    ex.open_position(SYM, 'long', 100, 0.501)
    ex.add_order(SYM, 'stop', 'sell', 100, 0.49, oid='ord-sl')
    ex.add_order(SYM, 'lmt', 'sell', 100, 0.52, oid='ord-tp')
    await env.g._recover_single_position(_nr_view(env, row))            # A7
    assert ex.calls('create_order') == [] and ex.calls('cancel_order') == []
    r = env.db.row(row['id'])
    assert r['status'] == 'open' and r['quantity'] == 100.0 and r['entry_fill_price'] == 0.501


async def test_legacy_recovery_sl_only_still_closes(monkeypatch, notify_log):
    env = guardian_env(monkeypatch)
    user, ex = env.user()
    row = _recovery_row(env, user, sl_order_id='ord-sl', tp_order_id=None)
    ex.open_position(SYM, 'long', 100, 0.5)
    ex.add_order(SYM, 'stop', 'sell', 100, 0.49, oid='ord-sl')
    await env.g._recover_single_position(_nr_view(env, row))
    assert ex.positions == {} and env.db.row(row['id'])['status'] == 'closed'
    assert 'ord-sl' not in ex.orders


def _empty_position_read(monkeypatch, ex, n, really_flat=False):
    """fetch_positions() call number n reads []: one false-empty read, or (really_flat) because the position
    closed just before it and stays gone. In these flows read 2 is _attempt_market_close's flat check."""
    real, count = ex.fetch_positions, [0]

    def fetch_positions(symbols=None, params=None):
        count[0] += 1
        if count[0] == n:
            if really_flat:
                ex.positions.clear()
            else:
                ex.empty_position_reads = 1
        return real(symbols, params)

    monkeypatch.setattr(ex, 'fetch_positions', fetch_positions)


@pytest.mark.parametrize('really_flat', [False, True])
async def test_legacy_recovery_flat_read_at_close_time_is_confirmed_before_anything_changes(
        monkeypatch, notify_log, really_flat):
    env = guardian_env(monkeypatch)
    user, ex = env.user()
    row = _recovery_row(env, user, sl_order_id='ord-sl', tp_order_id=None)   # a TP_FAILED_CLOSE_UNVERIFIED row
    ex.open_position(SYM, 'long', 100, 0.5)
    ex.add_order(SYM, 'stop', 'sell', 100, 0.49, oid='ord-sl')             # its SL is live
    _empty_position_read(monkeypatch, ex, 2, really_flat)                  # only the close-time read is flat
    await env.g._recover_single_position(_nr_view(env, row))
    assert ex.calls('create_order') == []                                  # ALREADY_FLAT: no close order was sent
    assert env.sleep.delays == [pg.LEGACY_FLAT_CONFIRM_S]                   # edit (3): re-read 5 s later
    if really_flat:                                                        # confirmed flat: legs cancelled, closed_manual
        assert env.db.row(row['id'])['status'] == 'closed_manual' and 'ord-sl' not in ex.orders
    else:                                                                  # one false-empty read changes nothing
        assert env.db.row(row['id'])['status'] == 'needs_recovery' and SYM in ex.positions
        assert ex.calls('cancel_order') == [] and 'ord-sl' in ex.orders
