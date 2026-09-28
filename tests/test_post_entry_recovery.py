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
