"""
T20 kraken_follower_sl_first_and_tp_policy (spec C34/C36, I6, I12, AC10/AC11/AC14).

execute_trade on a stateful fake krakenfutures + FakePool: SL before TP, bracket sized to
the confirmed fill, TP-failure policy per GUARDIAN_MODE (enforce keeps; otherwise close
FIRST and cancel the SL only after a verified flat), in-flight token lifetime, the market
entry double-entry guard, POSITION_RECORD_FAILED and the 2-decimal risk log.
"""
import asyncio
import logging

import ccxt
import pytest

import follower_guard
import order_utils
from hosted_trading_loop import HostedTradingLoop
from tests.fakes import fast_sleep, new_user_id, notify_log  # noqa: F401  (pytest fixtures)
from tests.fakes.fake_krakenfutures import FakeKrakenFutures
from tests.fakes.fake_pool import FakePool, FollowerDB


# ---------------- order_utils: market-entry double-entry guard (C36) ----------------

async def test_market_entry_error_with_a_possible_position_is_not_resent(fast_sleep, notify_log):
    ex = FakeKrakenFutures()
    ex.raise_after_entry_fill = 1                         # the entry fills, then the call times out

    async def maybe_open():
        return True

    order = await order_utils.place_entry_order_with_retry(ex, 'PF_ADAUSD', 'buy', 100,
                                                           confirm_position_fn=maybe_open)
    assert order == {'id': 'unknown-after-exception', 'info': {}}
    assert len(ex.calls('create_market_order')) == 1


async def test_market_entry_error_on_a_flat_account_is_retried(fast_sleep, notify_log):
    ex = FakeKrakenFutures()
    ex.fault('create_market_order', ccxt.NetworkError('timeout before the order reached Kraken'))

    async def flat():
        return False

    order = await order_utils.place_entry_order_with_retry(ex, 'PF_ADAUSD', 'buy', 100, confirm_position_fn=flat)
    assert order['id'].startswith('ord-') and len(ex.calls('create_market_order')) == 2


async def test_a_failing_position_check_counts_as_a_possible_position(fast_sleep, notify_log):
    ex = FakeKrakenFutures()
    ex.fault('create_market_order', ccxt.NetworkError('timeout'))

    async def broken():
        raise ccxt.NetworkError('positions endpoint down')

    order = await order_utils.place_entry_order_with_retry(ex, 'PF_ADAUSD', 'buy', 100,
                                                           confirm_position_fn=broken)
    assert order['id'] == 'unknown-after-exception' and len(ex.calls('create_market_order')) == 1


# ---------------- execute_trade: SL first, confirmed size, TP policy, in-flight token (C34) ----------------

def _setup(monkeypatch, mode=None):
    if mode is None:
        monkeypatch.delenv('GUARDIAN_MODE', raising=False)
    else:
        monkeypatch.setenv('GUARDIAN_MODE', mode)
    pool = FakePool()
    db = FollowerDB(pool)
    uid = new_user_id()
    user = db.add_user(uid)
    ex = FakeKrakenFutures(name=user['api_key'])
    loop = HostedTradingLoop(pool)
    loop.active_exchanges[user['api_key']] = ex
    db.deliveries[9000 + uid] = uid
    signal = {'delivery_id': 9000 + uid, 'signal_id': 'sig-%d' % uid, 'action': 'BUY', 'symbol': 'ADA/USDT',
              'entry_price': 0.5, 'stop_loss': 0.49, 'take_profit': 0.52, 'leverage': 5, 'risk_pct': 0.0125}
    return loop, db, pool, ex, user, signal


def _index(ex, method, pred=lambda info: True):
    for i, (m, info) in enumerate(ex.calls()):
        if m == method and pred(info):
            return i
    return None


def _is_sl(info):
    return info.get('type') in ('stop_market', 'stop')


def _is_close(info):
    return info.get('type') == 'market' and info.get('params', {}).get('reduceOnly') is True


async def test_sl_before_tp_and_bracket_sized_to_confirmed_partial_fill(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _setup(monkeypatch)
    ex.fill_ratio = 0.6                                   # 12500 requested, 7500 filled
    assert await loop.execute_trade(user, signal) is True
    sl_i = _index(ex, 'create_order', _is_sl)
    tp_i = _index(ex, 'create_limit_order')
    assert sl_i is not None and tp_i is not None and sl_i < tp_i
    (_, sl_info), = [c for c in ex.calls('create_order') if _is_sl(c[1])]
    (_, tp_info), = ex.calls('create_limit_order')
    assert float(sl_info['amount']) == 7500.0 and float(tp_info['amount']) == 7500.0
    row, = [r for r in db.rows if r['user_id'] == user['id']]
    assert row['status'] == 'open' and row['quantity'] == 7500.0
    assert row['sl_order_id'] and row['tp_order_id']


async def test_enforce_tp_failure_keeps_position_and_inserts_null_tp(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _setup(monkeypatch, mode='enforce')
    ex.fault('create_limit_order', ccxt.ExchangeError('tp rejected'), times=3)
    assert await loop.execute_trade(user, signal) is True
    assert _index(ex, 'create_order', _is_close) is None           # never closed
    row, = [r for r in db.rows if r['user_id'] == user['id']]
    assert row['status'] == 'open' and row['tp_order_id'] is None and row['sl_order_id']
    assert 'TP_PLACE_FAILED' in db.error_types()
    assert any('TP_PLACE_FAILED' in n['title'] and n['level'] == 'warning' for n in notify_log)
    assert 'ADA/USD:USD' in ex.positions                            # SL-protected position kept


async def test_shadow_tp_failure_closes_before_cancelling_the_sl(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _setup(monkeypatch)            # default mode = shadow
    ex.fault('create_limit_order', ccxt.ExchangeError('tp rejected'), times=3)
    assert await loop.execute_trade(user, signal) is False
    close_i = _index(ex, 'create_order', _is_close)
    cancel_i = _index(ex, 'cancel_order')
    assert close_i is not None and cancel_i is not None and close_i < cancel_i
    assert ex.positions == {} and ex.orders == {}                   # flat and the SL removed
    assert [r for r in db.rows if r['user_id'] == user['id']] == []  # clean exit: no row


async def test_shadow_tp_failure_sl_cancel_failure_records_needs_recovery_with_sl_id(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _setup(monkeypatch)
    ex.fault('create_limit_order', ccxt.ExchangeError('tp rejected'), times=3)
    ex.fault('cancel_order', ccxt.NetworkError('cancel timeout'), times=3)
    assert await loop.execute_trade(user, signal) is False
    row, = [r for r in db.rows if r['user_id'] == user['id']]
    sl_oid, = list(ex.orders)
    assert row['status'] == 'needs_recovery' and row['sl_order_id'] == sl_oid and row['tp_order_id'] is None
    assert row['target_sl'] == 0.49 and row['target_tp'] == 0.52
    assert any(e[0] == 'NEEDS_RECOVERY_RECORDED' and e[1] == 'LEFTOVER_SL_CANCEL_FAILED' for e in db.errors)


async def test_shadow_tp_failure_unverified_close_records_row_and_keeps_the_sl(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _setup(monkeypatch)
    ex.fault('create_limit_order', ccxt.ExchangeError('tp rejected'), times=3)
    ex.ignore_reduce_only_market = True                             # close accepted, position still there
    assert await loop.execute_trade(user, signal) is False
    assert ex.calls('cancel_order') == []                           # the live SL is never cancelled
    sl_oid, = list(ex.orders)
    row, = [r for r in db.rows if r['user_id'] == user['id']]
    assert row['status'] == 'needs_recovery' and row['sl_order_id'] == sl_oid
    assert any(e[0] == 'NEEDS_RECOVERY_RECORDED' and e[1] == 'TP_FAILED_CLOSE_UNVERIFIED' for e in db.errors)


async def test_sl_failure_closes_once_and_never_places_a_tp(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _setup(monkeypatch)
    ex.fault('create_order', ccxt.ExchangeError('sl rejected'), times=6, when=_is_sl)
    assert await loop.execute_trade(user, signal) is False
    assert len([c for c in ex.calls('create_order') if _is_close(c[1])]) == 1
    assert ex.calls('create_limit_order') == []
    assert ex.positions == {}
    assert [r for r in db.rows if r['user_id'] == user['id']] == []


async def test_inflight_token_precedes_ack_and_dedup_and_is_held_through_the_insert(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _setup(monkeypatch)
    events = []
    real_mark, real_clear = follower_guard.mark, follower_guard.clear
    monkeypatch.setattr(follower_guard, 'mark', lambda uid: (events.append('mark'), real_mark(uid))[1])
    monkeypatch.setattr(follower_guard, 'clear', lambda uid, tok: (events.append('clear'), real_clear(uid, tok))[1])
    real_ack, real_dedup = loop.acknowledge_signal, loop.check_any_open_positions_or_orders

    async def ack(delivery_id):
        events.append('ack')
        return await real_ack(delivery_id)

    async def dedup(*a, **k):
        events.append('dedup')
        return await real_dedup(*a, **k)

    monkeypatch.setattr(loop, 'acknowledge_signal', ack)
    monkeypatch.setattr(loop, 'check_any_open_positions_or_orders', dedup)
    held = []
    insert = pool._handlers[[n for n, _ in pool._handlers].index('INSERT INTO open_positions')][1]
    pool.on('INSERT INTO open_positions',
            lambda m, s, a: (held.append(follower_guard.is_inflight(user['id'])), insert(m, s, a))[1])
    assert await loop.execute_trade(user, signal) is True
    assert events[:3] == ['mark', 'ack', 'dedup'] and events[-1] == 'clear'
    assert held == [True]
    assert follower_guard.is_inflight(user['id']) is False


async def test_token_cleared_on_failure_and_on_exception(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _setup(monkeypatch)
    ex.open_position('ADA/USD:USD', 'long', 10, 0.5)                # dedup CHECK 1 -> skip (failure path)
    assert await loop.execute_trade(user, signal) is False
    assert follower_guard.is_inflight(user['id']) is False

    async def boom(delivery_id):
        raise RuntimeError('db down')

    monkeypatch.setattr(loop, 'acknowledge_signal', boom)
    with pytest.raises(RuntimeError):
        await loop.execute_trade(user, signal)
    assert follower_guard.is_inflight(user['id']) is False


async def test_a_second_concurrent_execution_keeps_the_user_in_flight(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _setup(monkeypatch)
    gate = asyncio.Event()
    real_ack = loop.acknowledge_signal
    calls = {'n': 0}

    async def ack(delivery_id):
        calls['n'] += 1
        if calls['n'] == 2:
            await gate.wait()                                       # the second execution stalls here
        return await real_ack(delivery_id)

    monkeypatch.setattr(loop, 'acknowledge_signal', ack)
    first = asyncio.create_task(loop.execute_trade(user, signal))
    second = asyncio.create_task(loop.execute_trade(user, dict(signal, delivery_id=signal['delivery_id'])))
    assert await first is True
    assert follower_guard.is_inflight(user['id']) is True           # the second token still holds
    gate.set()
    assert await second is False                                    # dedup: the first position exists
    assert follower_guard.is_inflight(user['id']) is False


async def test_market_entry_exception_with_a_position_is_not_resent(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _setup(monkeypatch)
    ex.raise_after_entry_fill = 1                                   # fills, then times out
    assert await loop.execute_trade(user, signal) is True
    assert len(ex.calls('create_market_order')) == 1                # never re-sent
    assert ex.positions['ADA/USD:USD']['contracts'] == 12500.0
    row, = [r for r in db.rows if r['user_id'] == user['id']]
    assert row['entry_order_id'] == 'unknown-after-exception' and row['sl_order_id'] and row['tp_order_id']


async def test_insert_failure_raises_position_record_failed(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _setup(monkeypatch)
    pool.fail_on('INSERT INTO open_positions', RuntimeError('insert rejected'))
    assert await loop.execute_trade(user, signal) is True
    assert 'POSITION_RECORD_FAILED' in db.error_types()
    assert any('POSITION_RECORD_FAILED' in n['title'] and n['level'] == 'error' for n in notify_log)
    assert len(ex.orders) == 2                                      # the position keeps SL + TP


async def test_risk_log_prints_two_decimals(monkeypatch, fast_sleep, notify_log, caplog):
    loop, db, pool, ex, user, signal = _setup(monkeypatch)
    with caplog.at_level(logging.INFO, logger='HOSTED_TRADING'):
        assert await loop.execute_trade(user, signal) is True
    assert '(1.25%)' in caplog.text
