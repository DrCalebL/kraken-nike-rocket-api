"""
T1LIVE-7 ONE SLOT PER STRATEGY: check_any_open_positions_or_orders(strategy=...) ignores ONLY the position and orders of
the user's own active service row of the OTHER strategy (symbol + side + recorded order ids). Anything unowned still
blocks and every read error fails closed.
"""
import ccxt

from hosted_trading_loop import HostedTradingLoop
from tests.fakes import fast_sleep, new_user_id, notify_log  # noqa: F401  (pytest fixtures)
from tests.fakes.fake_hold import HoldDB
from tests.fakes.fake_krakenfutures import FakeKrakenFutures
from tests.fakes.fake_pool import FakePool, FollowerDB

ADA = 'ADA/USD:USD'
BNB = 'BNB/USD:USD'


def _env():
    pool = FakePool()
    db = FollowerDB(pool)
    HoldDB(db)
    user = db.add_user(new_user_id())
    ex = FakeKrakenFutures(name=user['api_key'])
    loop = HostedTradingLoop(pool)
    return loop, db, pool, ex, user


def _ada(db, ex, user, status='open', side='long'):
    sl = ex.add_order(ADA, 'stop', 'sell', 100, 0.49)
    tp = ex.add_order(ADA, 'lmt', 'sell', 100, 0.52)
    ex.open_position(ADA, side, 100, 0.5)
    return db.add_row(user_id=user['id'], kraken_symbol='PF_ADAUSD', status=status, sl_order_id=sl, tp_order_id=tp,
                      side='BUY' if side == 'long' else 'SELL')


def _hold(db, ex, user, status='open', kind='hold'):
    sl = ex.add_order(BNB, 'stop', 'sell', 6.66, 570.0)
    ex.open_position(BNB, 'long', 6.66, 600.0)
    return db.add_row(user_id=user['id'], symbol='BNB/USDT', kraken_symbol='PF_BNBUSD', status=status, kind=kind,
                      sl_order_id=sl, tp_order_id=None, target_tp=None, target_sl=570.0, quantity=6.66,
                      entry_fill_price=600.0)


async def _check(loop, ex, user, strategy):
    return await loop.check_any_open_positions_or_orders(ex, 'u', user_id=user['id'], signal_id=None,
                                                         strategy=strategy)


async def test_an_ada_entry_ignores_the_users_own_open_hold(fast_sleep, notify_log):
    loop, db, pool, ex, user = _env()
    _hold(db, ex, user)
    assert await _check(loop, ex, user, 't1') == (False, None)


async def test_a_hold_entry_ignores_the_users_own_open_ada_bracket(fast_sleep, notify_log):
    loop, db, pool, ex, user = _env()
    _ada(db, ex, user)
    assert await _check(loop, ex, user, 'hold') == (False, None)


async def test_default_strategy_is_t1_and_without_a_user_nothing_is_ignored(fast_sleep, notify_log):
    loop, db, pool, ex, user = _env()
    _hold(db, ex, user)
    has_open, why = await loop.check_any_open_positions_or_orders(ex, 'u', user_id=None, signal_id=None)
    assert has_open is True and why.startswith('Open position exists')


async def test_the_same_strategy_slot_is_never_ignored(fast_sleep, notify_log):
    loop, db, pool, ex, user = _env()
    _hold(db, ex, user)
    has_open, why = await _check(loop, ex, user, 'hold')            # a second hold on top of a hold
    assert has_open is True and why.startswith('Open position exists')
    loop2, db2, pool2, ex2, user2 = _env()
    _ada(db2, ex2, user2)
    assert (await _check(loop2, ex2, user2, 't1'))[0] is True


async def test_a_manual_position_still_blocks_both_strategies(fast_sleep, notify_log):
    loop, db, pool, ex, user = _env()
    ex.open_position(BNB, 'long', 1, 600.0)                         # no row: the user's own trade
    assert (await _check(loop, ex, user, 't1'))[0] is True
    assert (await _check(loop, ex, user, 'hold'))[0] is True


async def test_a_manual_position_next_to_an_owned_one_blocks(fast_sleep, notify_log):
    loop, db, pool, ex, user = _env()
    _hold(db, ex, user)
    ex.open_position('BTC/USD:USD', 'long', 0.1, 60000.0)           # unowned
    has_open, why = await _check(loop, ex, user, 't1')
    assert has_open is True and 'BTC' in why


async def test_an_unowned_order_next_to_the_owned_position_blocks(fast_sleep, notify_log):
    loop, db, pool, ex, user = _env()
    _hold(db, ex, user)
    ex.add_order(BNB, 'lmt', 'sell', 1, 700.0)                      # not one of the row's order ids
    has_open, why = await _check(loop, ex, user, 't1')
    assert has_open is True and why.startswith('Open orders exist')


async def test_an_owned_order_id_on_another_symbol_is_not_ignored(fast_sleep, notify_log):
    loop, db, pool, ex, user = _env()
    row = _hold(db, ex, user)
    stray = ex.add_order(ADA, 'stop', 'sell', 10, 0.4, oid=row['sl_order_id'] + '-x')
    db.row(row['id'])['tp_order_id'] = stray                         # a recorded id, but on the wrong symbol
    has_open, why = await _check(loop, ex, user, 't1')
    assert has_open is True and why.startswith('Open orders exist')


async def test_a_side_mismatch_blocks(fast_sleep, notify_log):
    loop, db, pool, ex, user = _env()
    row = _hold(db, ex, user)
    db.row(row['id'])['side'] = 'SELL'                              # the row says short, the venue says long
    assert (await _check(loop, ex, user, 't1'))[0] is True


async def test_two_rows_of_the_other_strategy_are_ambiguous_and_block(fast_sleep, notify_log):
    loop, db, pool, ex, user = _env()
    _hold(db, ex, user)
    db.add_row(user_id=user['id'], symbol='BNB/USDT', kraken_symbol='PF_BNBUSD', status='open', kind='hold',
               sl_order_id='x', target_tp=None)
    assert (await _check(loop, ex, user, 't1'))[0] is True


async def test_a_needs_recovery_row_is_not_an_owned_slot(fast_sleep, notify_log):
    loop, db, pool, ex, user = _env()
    _hold(db, ex, user, status='needs_recovery')
    assert (await _check(loop, ex, user, 't1'))[0] is True


async def test_a_failed_slot_read_fails_closed(fast_sleep, notify_log):
    loop, db, pool, ex, user = _env()
    _hold(db, ex, user)
    import hosted_trading_loop as htl
    pool.fail_on(htl.SQL_OTHER_KIND_ROWS, RuntimeError('db down'))
    has_open, why = await _check(loop, ex, user, 't1')
    assert has_open is True and 'slot check failed' in why
    assert 'DEDUP_SKIPPED_READ_ERROR' in db.error_types()


async def test_failed_exchange_reads_fail_closed_even_with_an_owned_slot(fast_sleep, notify_log):
    loop, db, pool, ex, user = _env()
    _hold(db, ex, user)
    ex.fault('fetch_positions', ccxt.NetworkError('down'))
    assert (await _check(loop, ex, user, 't1'))[0] is True
    ex.fault('fetch_open_orders', ccxt.NetworkError('down'))
    assert (await _check(loop, ex, user, 't1'))[0] is True


async def test_other_users_rows_are_never_consulted(fast_sleep, notify_log):
    loop, db, pool, ex, user = _env()
    other = db.add_user(new_user_id())
    _hold(db, ex, other)                                            # another user's row owns this position
    assert (await _check(loop, ex, user, 't1'))[0] is True
