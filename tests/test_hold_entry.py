"""
T1LIVE-7 BNB hold entry on the Kraken follower: execute_hold_entry (market entry -> confirmed position -> stop-market
SL sized to the fill, NO TP), its refusals, the ADA + BNB same-user race, the delivery filters (a legacy path never gets
a hold), POST /api/broadcast-hold and the leader_exit close path.
"""
import asyncio
import time

import ccxt
import pytest

import close_guard as cg
import follower_guard
import hold_guard as hg
import hosted_trading_loop as htl
from hosted_trading_loop import HostedTradingLoop
from tests.fakes import fast_sleep, new_user_id, notify_log  # noqa: F401  (pytest fixtures)
from tests.fakes.fake_close import CloseDB
from tests.fakes.fake_hold import HoldDB
from tests.fakes.fake_krakenfutures import FakeKrakenFutures
from tests.fakes.fake_pool import FakePool, FollowerDB

BNB = 'BNB/USD:USD'
ADA = 'ADA/USD:USD'
MTID = '0123456789abcdef0123456789abcdef'


def run(coro):
    return asyncio.run(coro)


class Env:
    pass


def _env(monkeypatch, equity=10000.0, enabled=True, **user_fields):
    monkeypatch.delenv('GUARDIAN_MODE', raising=False)
    for k in ('HOLD_SIZE_SCALE', 'HOLD_MAX_RISK_PCT', 'HOLD_SL_MATCH_PCT'):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv('HOLD_USER_ALLOWLIST', '*')
    if enabled:
        monkeypatch.setenv('HOLD_ENABLED', '1')
    else:
        monkeypatch.delenv('HOLD_ENABLED', raising=False)
    e = Env()
    e.pool = FakePool()
    e.db = FollowerDB(e.pool)
    e.hold = HoldDB(e.db)
    e.cdb = CloseDB(e.db)
    e.user = e.db.add_user(new_user_id(), **user_fields)
    e.ex = FakeKrakenFutures(name=e.user['api_key'], equity=equity)
    e.loop = HostedTradingLoop(e.pool)
    e.loop.active_exchanges[e.user['api_key']] = e.ex
    e.loop.hold_available = True
    return e


def _orders(ex, method='create_order'):
    return ex.calls(method)


def _entries(ex):
    return [c for c in ex.calls('create_market_order')]


def _is_sl(info):
    return info.get('type') in ('stop_market', 'stop')


async def _enter(e, **kw):
    item = e.hold.add_delivery(e.user, **kw)
    await e.loop._execute_hold_for_user(item)
    return item


def _hold_rows(e):
    return [r for r in e.db.rows if r.get('kind') == 'hold']


# ======================================================================================================
# the entry
# ======================================================================================================

def test_hold_entry_places_a_confirmed_stop_only_position(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    run(_enter(e))
    pos = e.ex.positions[BNB]
    assert pos['side'] == 'long' and pos['contracts'] == 6.66            # min(10000*.02/30, 10000*.5/600) at mark 600
    (_, lev), = e.ex.calls('set_leverage')
    assert lev['leverage'] == 10
    sls = [i for _m, i in _orders(e.ex) if _is_sl(i)]
    assert len(sls) == 1 and float(sls[0]['amount']) == 6.66 and sls[0]['params']['stopPrice'] == 570.0
    assert e.ex.calls('create_limit_order') == [] and not any(i['type'] == 'limit' for _m, i in _orders(e.ex))
    row, = _hold_rows(e)
    assert row['status'] == 'open' and row['target_tp'] is None and row['tp_order_id'] is None
    assert row['sl_order_id'] and row['target_sl'] == 570.0 and row['quantity'] == 6.66
    assert row['kraken_symbol'] == 'PF_BNBUSD' and row['symbol'] == 'BNB/USDT'
    assert list(e.ex.orders) == [row['sl_order_id']]


def test_sl_follows_the_confirmed_partial_fill(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.ex.fill_ratio = 0.5
    run(_enter(e))
    row, = _hold_rows(e)
    assert row['quantity'] == 3.33
    (_, sl), = [(m, i) for m, i in _orders(e.ex) if _is_sl(i)]
    assert float(sl['amount']) == 3.33


def test_the_delivery_is_acknowledged_before_any_order(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    item = e.hold.add_delivery(e.user)
    order = []
    real = e.loop.acknowledge_signal

    async def ack(d):
        order.append(('ack', len(e.ex.calls('create_market_order'))))
        return await real(d)

    monkeypatch.setattr(e.loop, 'acknowledge_signal', ack)
    run(e.loop._execute_hold_for_user(item))
    assert order == [('ack', 0)]


def test_a_second_hold_offer_for_a_user_who_holds_is_not_entered(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    run(_enter(e))
    run(_enter(e, delivery_id=8001))
    assert len(_entries(e.ex)) == 1 and len(_hold_rows(e)) == 1


def test_leverage_is_set_before_the_entry_and_never_above_ten(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    run(_enter(e))
    names = [m for m, _i in e.ex.calls()]
    assert names.index('set_leverage') < names.index('create_market_order')
    assert all(i['leverage'] == 10 for _m, i in e.ex.calls('set_leverage'))


def test_a_failed_leverage_set_means_no_entry(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.ex.fault('set_leverage', ccxt.ExchangeError('nope'))
    run(_enter(e))
    assert _entries(e.ex) == [] and _hold_rows(e) == []
    assert 'HOLD_LEVERAGE_SET_FAILED' in e.db.error_types()


# ======================================================================================================
# sizing refusals
# ======================================================================================================

@pytest.mark.parametrize('price,code', [(574.0, 'HOLD_MARK_NEAR_STOP'), (575.0, 'HOLD_MARK_NEAR_STOP')])
def test_a_mark_that_makes_the_entry_unsafe_is_refused(monkeypatch, fast_sleep, notify_log, price, code):
    e = _env(monkeypatch)
    e.ex.prices[BNB] = price
    run(_enter(e))
    assert _entries(e.ex) == [] and e.ex.positions == {} and code in e.db.error_types()


def test_a_late_join_at_a_far_price_is_sized_not_refused(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.ex.prices[BNB] = 648.0
    run(_enter(e))
    assert e.ex.positions[BNB]['contracts'] == 2.56


def test_no_allowlist_means_nobody_and_star_means_everybody(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    monkeypatch.delenv('HOLD_USER_ALLOWLIST')
    item = e.hold.add_delivery(e.user)
    assert run(e.loop.get_pending_holds_batched()) == []
    run(e.loop._execute_hold_for_user(item))
    assert e.ex.calls() == []
    monkeypatch.setenv('HOLD_USER_ALLOWLIST', '*')
    assert len(run(e.loop.get_pending_holds_batched())) == 1


def test_the_follower_is_sized_at_its_own_mark(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.ex.prices[BNB] = 610.0
    run(_enter(e))
    assert e.ex.positions[BNB]['contracts'] == 5.0                        # 200 / (610 - 570)


def test_margin_shrinks_the_size_and_leverage_stays_ten(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.ex.free_margin = 300.0
    run(_enter(e))
    assert e.ex.positions[BNB]['contracts'] == 3.16
    assert all(i['leverage'] == 10 for _m, i in e.ex.calls('set_leverage'))


def test_no_margin_skips_with_hold_margin_skip(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.ex.free_margin = 0.5
    run(_enter(e))
    assert _entries(e.ex) == [] and 'HOLD_MARGIN_SKIP' in e.db.error_types()
    assert any('HOLD_MARGIN_SKIP' in n['title'] for n in notify_log)


def test_an_unreadable_free_margin_skips(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    monkeypatch.setattr(e.ex, 'fetch_balance', lambda params=None: {'USD': {'total': 10000.0}, 'info': {}})
    run(_enter(e))
    assert _entries(e.ex) == [] and 'HOLD_MARGIN_UNKNOWN' in e.db.error_types()


def test_zero_equity_and_unreadable_mark_are_refused(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch, equity=0.0)
    run(_enter(e))
    assert _entries(e.ex) == [] and 'HOLD_NO_EQUITY' in e.db.error_types()
    e2 = _env(monkeypatch)
    e2.ex.fault('fetch_ticker', ccxt.NetworkError('down'))
    run(_enter(e2))
    assert _entries(e2.ex) == [] and 'HOLD_READ_ERROR' in e2.db.error_types()


def test_a_scaled_size(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    monkeypatch.setenv('HOLD_SIZE_SCALE', '0.5')
    run(_enter(e))
    assert e.ex.positions[BNB]['contracts'] == 3.33


# ======================================================================================================
# switches and gates
# ======================================================================================================

def test_hold_disabled_by_default_never_trades_or_even_reads(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch, enabled=False)
    run(_enter(e))
    assert e.ex.calls() == [] and _hold_rows(e) == [] and e.pool.sql('UPDATE signal_deliveries') == []
    assert run(e.loop.get_pending_holds_batched()) == []
    monkeypatch.delenv('HOLD_USER_ALLOWLIST')
    assert htl.hold_flags() == {'HOLD_ENABLED': False, 'HOLD_USER_ALLOWLIST': False}


def test_unavailable_market_disables_the_hold(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.loop.hold_available = False
    run(_enter(e))
    assert e.ex.calls() == [] and run(e.loop.get_pending_holds_batched()) == []


def test_startup_check_finds_or_misses_pf_bnbusd(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.loop.hold_available = None
    monkeypatch.setattr(HostedTradingLoop, '_load_public_markets', staticmethod(lambda: {'x': {'id': 'PF_BNBUSD'}}))
    assert run(e.loop.check_hold_market(force=True)) is True
    monkeypatch.setattr(HostedTradingLoop, '_load_public_markets', staticmethod(lambda: {'x': {'id': 'PF_ADAUSD'}}))
    assert run(e.loop.check_hold_market(force=True)) is False
    assert 'HOLD_MARKET_MISSING' in e.db.error_types() and any('HOLD_MARKET_MISSING' in n['title'] for n in notify_log)

    def boom():
        raise ccxt.NetworkError('down')

    e.loop.hold_available = None
    monkeypatch.setattr(HostedTradingLoop, '_load_public_markets', staticmethod(boom))
    assert run(e.loop.check_hold_market(force=True)) is False


def test_the_allowlist_gates_entry_and_the_pending_list(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    monkeypatch.setenv('HOLD_USER_ALLOWLIST', '999999')
    item = e.hold.add_delivery(e.user)
    assert run(e.loop.get_pending_holds_batched()) == []
    run(e.loop._execute_hold_for_user(item))
    assert _entries(e.ex) == []
    monkeypatch.setenv('HOLD_USER_ALLOWLIST', str(e.user['id']))
    assert len(run(e.loop.get_pending_holds_batched())) == 1
    run(e.loop._execute_hold_for_user(item))
    assert len(_entries(e.ex)) == 1


def test_the_personal_off_switch_blocks(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    item = e.hold.add_delivery(e.user)
    user = dict(e.user, hold_enabled=False)
    assert run(e.loop.execute_hold_entry(user, {'delivery_id': item['delivery_id']})) is False
    assert e.ex.calls() == []


def test_refused_while_in_flight_and_the_delivery_stays_open(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    tok = follower_guard.mark(e.user['id'])
    try:
        run(_enter(e))
    finally:
        follower_guard.clear(e.user['id'], tok)
    assert e.ex.calls() == [] and e.pool.sql('UPDATE signal_deliveries') == []


def test_stale_closed_and_sat_out_offers_are_acknowledged_and_never_traded(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    run(_enter(e, offer_ms=int(time.time() * 1000) - 16 * 60_000))
    assert 'HOLD_OFFER_STALE' in e.db.error_types()
    e.hold.closed.add(MTID)
    run(_enter(e, delivery_id=8002))
    assert 'HOLD_ALREADY_CLOSED' in e.db.error_types()
    e.hold.closed.clear()
    e.hold.sitouts.add((e.user['id'], MTID))
    run(_enter(e, delivery_id=8003))
    assert 'HOLD_SAT_OUT' in e.db.error_types()
    assert e.ex.calls() == [] and len(e.pool.sql('UPDATE signal_deliveries')) == 3


def test_a_failed_closed_or_sitout_read_fails_closed_and_keeps_the_delivery(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.pool.fail_on(htl.SQL_HOLD_SITOUT, RuntimeError('db down'))
    run(_enter(e))
    assert e.ex.calls() == [] and e.pool.sql('UPDATE signal_deliveries') == []
    assert 'HOLD_SKIPPED_READ_ERROR' in e.db.error_types()


def test_a_manual_bnb_position_blocks_the_hold(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.ex.open_position(BNB, 'long', 1.0, 590.0)
    run(_enter(e))
    assert _entries(e.ex) == [] and _hold_rows(e) == []


# ======================================================================================================
# failure paths: SL failure -> emergency close
# ======================================================================================================

def test_sl_failure_closes_the_hold_immediately(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.ex.fault('create_order', ccxt.ExchangeError('rejected'), times=50, when=_is_sl)
    run(_enter(e))
    assert e.ex.positions == {} and _hold_rows(e) == []
    closes = [i for _m, i in _orders(e.ex) if i.get('params', {}).get('reduceOnly') and i['type'] == 'market']
    assert len(closes) == 1 and closes[0]['side'] == 'sell'


def test_sl_and_close_failure_records_a_needs_recovery_hold_row_without_tp(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.ex.fault('create_order', ccxt.ExchangeError('rejected'), times=100,
               when=lambda info: info.get('type') != 'x')
    run(_enter(e))
    row, = _hold_rows(e)
    assert row['status'] == 'needs_recovery' and row['target_tp'] is None and row['tp_order_id'] is None
    assert row['target_sl'] == 570.0 and 'EMERGENCY_CLOSE_FAILED' in e.db.error_types()


def test_an_exception_after_the_entry_records_a_hold_recovery_row(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)

    async def boom(*a, **k):
        raise RuntimeError('bracket blew up')

    monkeypatch.setattr(htl, 'place_sl_order_with_retry', boom)
    run(_enter(e))
    row, = _hold_rows(e)
    assert row['status'] == 'needs_recovery' and row['target_tp'] is None and 'POST_ENTRY_EXCEPTION' in e.db.error_types()


# ======================================================================================================
# ADA + BNB for the same user
# ======================================================================================================

def _ada_item(e, delivery_id=9101):
    e.db.deliveries[delivery_id] = e.user['id']
    return {'user_id': e.user['id'], 'api_key': e.user['api_key'], 'email': e.user['email'],
            'kraken_api_key_encrypted': 'enc-k', 'kraken_api_secret_encrypted': 'enc-s',
            'delivery_id': delivery_id, 'signal_id': 'sig-ada-1', 'action': 'BUY', 'symbol': 'ADA/USDT',
            'entry_price': 0.5, 'stop_loss': 0.49, 'take_profit': 0.52, 'leverage': 5, 'risk_pct': 0.0125,
            'signal_created_at': e.db.now(), 'hold_enabled': True}


def _wire_polls(e, ada):
    acked = set()
    real = e.loop.acknowledge_signal

    async def ack(d):
        acked.add(d)
        return await real(d)

    e.loop.acknowledge_signal = ack
    e.pool.on("AND COALESCE(s.strategy, 't1') = 't1' AND s.created_at",
              lambda m, s, a: [r for r in [ada] if r['delivery_id'] not in acked])
    e.hold.pending[:] = []
    e.pool.on(htl.SQL_PENDING_HOLDS, lambda m, s, a: [r for r in e.hold.pending if r['delivery_id'] not in acked])
    return acked


def test_ada_and_bnb_for_one_user_run_one_entry_per_cycle_and_both_end_up_open(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.loop.hold_available = None
    monkeypatch.setattr(HostedTradingLoop, '_load_public_markets', staticmethod(lambda: {'x': {'id': 'PF_BNBUSD'}}))
    ada = _ada_item(e)
    _wire_polls(e, ada)
    e.hold.add_delivery(e.user)
    run(e.loop.poll_and_execute())                                   # cycle 1: ADA only (one entry per user)
    assert list(e.ex.positions) == [ADA] and _hold_rows(e) == []
    run(e.loop.poll_and_execute())                                   # cycle 2: the hold; the ADA bracket is the other slot
    assert set(e.ex.positions) == {ADA, BNB}
    ada_row, = [r for r in e.db.rows if (r.get('kind') or 't1') == 't1']
    hold_row, = _hold_rows(e)
    assert ada_row['tp_order_id'] and ada_row['sl_order_id'] and ada_row['target_tp'] == 0.52
    assert hold_row['tp_order_id'] is None and hold_row['target_tp'] is None
    assert len(e.ex.orders) == 3                                     # ADA SL + TP, BNB SL
    run(e.loop.poll_and_execute())                                   # nothing left to do
    assert len(_entries(e.ex)) == 2


def test_hold_first_then_ada_also_works(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    run(_enter(e))
    ada = _ada_item(e)
    acked = _wire_polls(e, ada)
    e.hold.pending[:] = []
    run(e.loop.poll_and_execute())
    assert set(e.ex.positions) == {ADA, BNB} and ada['delivery_id'] in acked


def test_two_entry_deliveries_for_one_user_are_serialised_within_a_cycle(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.loop.hold_available = None
    monkeypatch.setattr(HostedTradingLoop, '_load_public_markets', staticmethod(lambda: {'x': {'id': 'PF_BNBUSD'}}))
    ada = _ada_item(e)
    _wire_polls(e, ada)
    e.hold.add_delivery(e.user)
    seen = []
    real_t1, real_hold = e.loop._execute_signal_for_user, e.loop._execute_hold_for_user

    async def t1(item):
        seen.append('t1')
        return await real_t1(item)

    async def hold(item):
        seen.append('hold')
        return await real_hold(item)

    e.loop._execute_signal_for_user, e.loop._execute_hold_for_user = t1, hold
    run(e.loop.poll_and_execute())
    assert seen == ['t1']


def test_a_hold_that_slipped_into_the_t1_path_is_refused(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    sig = dict(_ada_item(e), strategy='hold', take_profit=None, delivery_id=9102)
    assert run(e.loop.execute_trade(dict(e.user, id=e.user['id']), sig)) is False
    assert e.ex.calls() == []


def _ada_signal_for_leverage():
    return dict(entry_price=0.5, stop_loss=0.499, take_profit=0.52, leverage=5, risk_pct=0.02)


@pytest.mark.parametrize('hold_on,expected', [(True, 10), (False, 16)])
def test_ada_auto_raised_leverage_is_capped_at_ten_for_hold_users(monkeypatch, fast_sleep, notify_log, hold_on,
                                                                  expected):
    e = _env(monkeypatch, enabled=hold_on)
    sig = dict(_ada_item(e), **_ada_signal_for_leverage())
    user = dict(e.user, hold_enabled=True)
    run(e.loop.execute_trade(user, sig))
    assert e.ex.leverage == expected


def test_ada_leverage_cap_does_not_apply_to_a_user_with_hold_off(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    sig = dict(_ada_item(e), **_ada_signal_for_leverage())
    user = dict(e.user, hold_enabled=False)
    run(e.loop.execute_trade(user, sig))
    assert e.ex.leverage == 16


# ======================================================================================================
# a legacy / agent path never gets a hold
# ======================================================================================================

def test_every_ada_and_legacy_delivery_query_excludes_holds(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.pool.on('SELECT id FROM follower_users WHERE api_key', lambda m, s, a: {'id': e.user['id']})
    run(e.loop.get_pending_signals_batched())
    run(e.loop.get_latest_signal(e.user['api_key']))
    sqls = [s for _m, s, _a in e.pool.calls if 'signal_deliveries' in s and 'FROM' in s]
    assert len(sqls) >= 1
    for s in sqls:
        assert "COALESCE(s.strategy, 't1') = 't1'" in s, s


def test_the_pending_hold_query_only_selects_hold_signals_for_hold_enabled_users(monkeypatch):
    assert "s.strategy = 'hold'" in htl.SQL_PENDING_HOLDS
    assert "COALESCE(u.hold_enabled, true) = true" in htl.SQL_PENDING_HOLDS
    assert "INTERVAL '15 minutes'" in htl.SQL_PENDING_HOLDS


# ======================================================================================================
# leader_exit through the close jobs
# ======================================================================================================

def _hold_row_with_position(e):
    run(_enter(e))
    row, = _hold_rows(e)
    return row


def test_a_leader_exit_closes_the_hold_labels_leader_and_cancels_the_sl(monkeypatch, fast_sleep, notify_log, tmp_path):
    monkeypatch.setattr(htl, 'CLOSE_EVENTS', htl.make_close_event_log(str(tmp_path / 'c.jsonl')))
    e = _env(monkeypatch)
    row = _hold_row_with_position(e)
    e.cdb.add_job(row, master_trade_id=MTID, side='BUY')
    e.cdb.closes[MTID].update(reason='leader_exit', symbol='BNB/USDT')
    run(e.loop.process_close_jobs())
    assert e.cdb.job(row['id'])['status'] == 'done'
    assert e.db.row(row['id'])['exit_hint'] == 'LEADER' and e.db.row(row['id'])['close_fill_qty'] == 6.66
    assert e.ex.positions == {} and e.ex.orders == {}


def test_a_time_exit_still_writes_time(monkeypatch, fast_sleep, notify_log, tmp_path):
    monkeypatch.setattr(htl, 'CLOSE_EVENTS', htl.make_close_event_log(str(tmp_path / 'c.jsonl')))
    e = _env(monkeypatch)
    row = _hold_row_with_position(e)
    e.cdb.add_job(row, master_trade_id=MTID, side='BUY')
    run(e.loop.process_close_jobs())
    assert e.db.row(row['id'])['exit_hint'] == 'TIME'


# ======================================================================================================
# POST /api/broadcast-hold and the endpoint filters (SQLite)
# ======================================================================================================

def _hold_payload(**over):
    from tests.test_master_close import _fixture
    now = _fixture()['now_ms']
    d = {'master_trade_id': MTID, 'leader_trade_id': 'L-1', 'action': 'BUY', 'symbol': 'BNB/USDT',
         'ref_entry': '600.0', 'stop_loss': '570.0', 'r_b': '0.02', 'n2': '0.5', 'leader_entry_ms': now - 3_600_000,
         'offer_ms': now - 1000, 'offer_kind': 'entry', 'protocol': 'hold-1'}
    d.update(over)
    return d


def _post_hold(a, payload, key=None):
    from tests.test_master_close import KEY
    key = KEY if key is None else key
    return a.client.post('/api/broadcast-hold', json=payload, headers={'X-Master-Key': key} if key else {})


def _api(monkeypatch, tmp_path):
    from tests.test_master_close import _api as base
    monkeypatch.setenv('HOLD_ENABLED', '1')
    monkeypatch.setenv('HOLD_USER_ALLOWLIST', '*')
    a = base(monkeypatch, tmp_path)
    fm = a.fm
    s = a.Session()
    for i in range(1, 5):
        s.add(fm.User(api_key='nk_hold_user_%d' % i, email='h%d@example.com' % i, access_granted=True,
                      agent_active=True, credentials_set=True))
    s.commit()
    s.close()
    return a


def _count(a, sql, **p):
    return _q(a, sql, **p)[0][0]


from tests.test_master_close import _q  # noqa: E402


def test_broadcast_hold_needs_the_master_key(monkeypatch, tmp_path):
    a = _api(monkeypatch, tmp_path)
    assert _post_hold(a, _hold_payload(), key='wrong').status_code == 401
    assert _count(a, 'SELECT COUNT(*) FROM signals') == 0


def test_broadcast_hold_rejects_bad_payloads_with_422_and_writes_nothing(monkeypatch, tmp_path):
    a = _api(monkeypatch, tmp_path)
    for over in ({'action': 'SELL'}, {'r_b': '0.2'}, {'stop_loss': '601'}, {'symbol': 'ADA/USDT'},
                 {'protocol': 'hold-2'}, {'offer_ms': 1}):
        r = _post_hold(a, _hold_payload(**over))
        assert r.status_code == 422, (over, r.text)
    assert _count(a, 'SELECT COUNT(*) FROM signals') == 0 and _count(a, 'SELECT COUNT(*) FROM hold_offers') == 0


def test_broadcast_hold_is_a_noop_while_the_service_switch_is_off(monkeypatch, tmp_path):
    a = _api(monkeypatch, tmp_path)
    monkeypatch.delenv('HOLD_ENABLED')
    r = _post_hold(a, _hold_payload())
    assert r.status_code == 200 and r.json()['skipped'] == 'HOLD_DISABLED'
    assert _count(a, 'SELECT COUNT(*) FROM signals') == 0


def test_broadcast_hold_creates_a_tp_less_hold_signal_and_deliveries_and_is_idempotent(monkeypatch, tmp_path):
    a = _api(monkeypatch, tmp_path)
    r = _post_hold(a, _hold_payload())
    assert r.status_code == 200 and r.json()['delivered_to'] == 4 and r.json()['duplicate'] is False
    sig = _q(a, 'SELECT strategy, take_profit, stop_loss, entry_price, hold_r_b, hold_n2, master_trade_id, action '
                'FROM signals')
    assert sig == [('hold', None, 570.0, 600.0, 0.02, 0.5, MTID, 'BUY')]
    assert _count(a, 'SELECT COUNT(*) FROM signal_deliveries WHERE offer_ms IS NOT NULL') == 4
    again = _post_hold(a, _hold_payload())
    assert again.json()['duplicate'] is True
    assert _count(a, 'SELECT COUNT(*) FROM signal_deliveries') == 4 and _count(a, 'SELECT COUNT(*) FROM signals') == 1


def test_a_reoffer_reuses_the_signal_and_skips_followers_who_hold_or_were_stopped_out(monkeypatch, tmp_path):
    a = _api(monkeypatch, tmp_path)
    _post_hold(a, _hold_payload())
    s = a.Session()
    fm = a.fm
    sig = s.query(fm.Signal).one()
    users = {u.api_key: u.id for u in s.query(fm.User).all()}
    s.add(fm.OpenPosition(user_id=users['nk_hold_user_1'], signal_id=sig.id, entry_order_id='E', symbol='BNB/USDT',
                          kraken_symbol='PF_BNBUSD', side='BUY', quantity=1.0, target_tp=None, target_sl=570.0,
                          kind='hold', status='open'))
    s.add(fm.HoldSitout(user_id=users['nk_hold_user_2'], master_trade_id=MTID, reason='STOPPED_OUT'))
    u3 = s.query(fm.User).filter_by(api_key='nk_hold_user_3').one()
    u3.hold_enabled = False
    for d in s.query(fm.SignalDelivery).all():
        d.acknowledged = True                                    # the first offer was handled
    s.commit()
    s.close()
    r = _post_hold(a, _hold_payload(offer_kind='reoffer', offer_ms=_hold_payload()['offer_ms'] + 500))
    assert r.status_code == 200 and r.json()['delivered_to'] == 1       # only user 4
    assert _count(a, 'SELECT COUNT(*) FROM signals') == 1
    assert _count(a, 'SELECT COUNT(*) FROM hold_offers') == 2


def test_a_late_joiner_gets_the_reoffer_but_a_pending_delivery_is_not_doubled(monkeypatch, tmp_path):
    a = _api(monkeypatch, tmp_path)
    _post_hold(a, _hold_payload())
    r = _post_hold(a, _hold_payload(offer_kind='reoffer', offer_ms=_hold_payload()['offer_ms'] + 500))
    assert r.json()['delivered_to'] == 0                                  # all four still have an open delivery


def test_broadcast_hold_changed_parameters_or_a_closed_trade_are_not_offered(monkeypatch, tmp_path):
    a = _api(monkeypatch, tmp_path)
    _post_hold(a, _hold_payload())
    r = _post_hold(a, _hold_payload(stop_loss='580.0', offer_ms=_hold_payload()['offer_ms'] + 500))
    assert r.status_code == 422
    from tests.test_master_close import _post_close, _close_payload
    assert _post_close(a, _close_payload(MTID, symbol='ADA/USDT', reason='leader_exit')).status_code == 200
    r = _post_hold(a, _hold_payload(offer_ms=_hold_payload()['offer_ms'] + 900))
    assert r.json()['skipped'] == 'HOLD_ALREADY_CLOSED' and r.json()['delivered_to'] == 0


def test_the_allowlist_limits_deliveries(monkeypatch, tmp_path):
    a = _api(monkeypatch, tmp_path)
    monkeypatch.setenv('HOLD_USER_ALLOWLIST', 'h2@example.com')
    r = _post_hold(a, _hold_payload())
    assert r.json()['delivered_to'] == 1


def test_a_master_trade_id_of_a_t1_signal_is_not_a_hold(monkeypatch, tmp_path):
    a = _api(monkeypatch, tmp_path)
    s = a.Session()
    s.add(a.fm.Signal(signal_id='sig-t1', action='BUY', symbol='ADA/USDT', entry_price=0.5, stop_loss=0.49,
                      take_profit=0.52, leverage=2, risk_pct=0.01, master_trade_id=MTID))
    s.commit()
    s.close()
    assert _post_hold(a, _hold_payload()).status_code == 422


def test_the_legacy_latest_signal_endpoint_never_returns_a_hold(monkeypatch, tmp_path):
    a = _api(monkeypatch, tmp_path)
    _post_hold(a, _hold_payload())
    for key in ('nk_hold_user_1', 'nk_hold_user_4'):
        r = a.client.get('/api/latest-signal', headers={'X-API-Key': key})
        assert r.status_code == 200 and r.json().get('signal') is None, r.text


def test_the_legacy_endpoint_still_returns_a_t1_signal_next_to_a_hold(monkeypatch, tmp_path):
    a = _api(monkeypatch, tmp_path)
    _post_hold(a, _hold_payload())
    s = a.Session()
    fm = a.fm
    t1 = fm.Signal(signal_id='sig-ada', action='BUY', symbol='ADA/USDT', entry_price=0.5, stop_loss=0.49,
                   take_profit=0.52, leverage=2, risk_pct=0.01)
    s.add(t1)
    s.flush()
    uid = s.query(fm.User).filter_by(api_key='nk_hold_user_1').one().id
    s.add(fm.SignalDelivery(signal_id=t1.id, user_id=uid))
    s.commit()
    s.close()
    r = a.client.get('/api/latest-signal', headers={'X-API-Key': 'nk_hold_user_1'})
    assert r.json()['signal']['signal_id'] == 'sig-ada'


def test_a_leader_exit_close_is_accepted_and_acknowledges_unexecuted_hold_deliveries(monkeypatch, tmp_path):
    from tests.test_master_close import _post_close, _close_payload
    a = _api(monkeypatch, tmp_path)
    _post_hold(a, _hold_payload())
    r = _post_close(a, _close_payload(MTID, symbol='ADA/USDT', reason='leader_exit'))
    assert r.status_code == 200 and r.json()['duplicate'] is False
    assert _count(a, 'SELECT COUNT(*) FROM master_closes WHERE reason = :r', r='leader_exit') == 1
    assert _count(a, 'SELECT COUNT(*) FROM signal_deliveries WHERE acknowledged = :f', f=False) == 0
    assert _count(a, 'SELECT COUNT(*) FROM signal_deliveries WHERE acknowledged = :t', t=True) == 4


def test_a_bnb_symbol_close_is_accepted(monkeypatch, tmp_path):
    from tests.test_master_close import _post_close, _close_payload
    a = _api(monkeypatch, tmp_path)
    r = _post_close(a, _close_payload(MTID, symbol='BNB/USDT', reason='leader_exit'))
    assert r.status_code == 200


def test_hold_migrations_are_additive_and_idempotent():
    import ast
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'main.py'), encoding='utf-8') as f:
        tree = ast.parse(f.read())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == '_HOLD_MIGRATIONS' for t in n.targets))
    stmts = [' '.join(ast.literal_eval(n).split()) for n in node.value.elts]
    joined = ' | '.join(stmts)
    for needle in ("signals ADD COLUMN IF NOT EXISTS strategy VARCHAR(8) DEFAULT 't1'",
                   'open_positions ADD COLUMN IF NOT EXISTS kind VARCHAR(8) DEFAULT \'t1\'',
                   'open_positions ALTER COLUMN target_tp DROP NOT NULL',
                   'signals ALTER COLUMN take_profit DROP NOT NULL',
                   'follower_users ADD COLUMN IF NOT EXISTS hold_enabled BOOLEAN DEFAULT TRUE',
                   'signal_deliveries ADD COLUMN IF NOT EXISTS offer_ms BIGINT',
                   'CREATE TABLE IF NOT EXISTS hold_offers', 'CREATE TABLE IF NOT EXISTS hold_sitouts'):
        assert needle in joined, needle
    for s in stmts:
        assert s.startswith(('ALTER TABLE ', 'CREATE TABLE IF NOT EXISTS ', 'DO $$ BEGIN IF EXISTS')), s
        if s.startswith('ALTER TABLE ') and 'ADD COLUMN' in s:
            assert 'IF NOT EXISTS' in s, s
        assert 'DROP TABLE' not in s and 'DROP COLUMN' not in s and 'DELETE' not in s


def test_endpoint_unset_allowlist_delivers_to_nobody_and_an_expired_delivery_does_not_block_a_reoffer(monkeypatch,
                                                                                                     tmp_path):
    from datetime import datetime, timedelta
    a = _api(monkeypatch, tmp_path)
    monkeypatch.delenv('HOLD_USER_ALLOWLIST')
    assert _post_hold(a, _hold_payload()).json()['delivered_to'] == 0
    monkeypatch.setenv('HOLD_USER_ALLOWLIST', '*')
    assert _post_hold(a, _hold_payload(offer_ms=_hold_payload()['offer_ms'] + 100)).json()['delivered_to'] == 4
    s = a.Session()
    for d in s.query(a.fm.SignalDelivery).all():                      # the first offer expired unexecuted
        d.delivered_at = datetime.utcnow() - timedelta(minutes=20)
    s.commit()
    s.close()
    r = _post_hold(a, _hold_payload(offer_kind='reoffer', offer_ms=_hold_payload()['offer_ms'] + 500))
    assert r.json()['delivered_to'] == 4


def test_a_close_acknowledgement_leaves_acknowledged_at_null(monkeypatch, tmp_path):
    from tests.test_master_close import _post_close, _close_payload
    a = _api(monkeypatch, tmp_path)
    _post_hold(a, _hold_payload())
    _post_close(a, _close_payload(MTID, symbol='ADA/USDT', reason='leader_exit'))
    assert _count(a, 'SELECT COUNT(*) FROM signal_deliveries WHERE acknowledged_at IS NOT NULL') == 0


# ======================================================================================================
# review M1/M3: a CLOSE that races the entry, atomic acknowledge; LOW-4 symbol; LOW-5 dedup
# ======================================================================================================

def _late_close_handler(e):
    def handler(method, sql, args):
        user_id, signal_id = args
        mtid = e.hold.mtid_of_signal.get(signal_id)
        if mtid not in e.hold.closed:
            return 'INSERT 0 0'
        rows = [r for r in e.db.rows if r['user_id'] == user_id and r.get('signal_id') == signal_id
                and r['status'] in ('open', 'needs_recovery')]
        for r in rows:
            e.cdb.add_job(r, master_trade_id=mtid, side='BUY')
        return 'INSERT 0 %d' % len(rows)
    e.pool.on(htl.SQL_LATE_CLOSE_JOB, handler)


def _close_during_entry(e, monkeypatch, mtid):
    real = e.loop._wait_for_position

    async def hooked(*a, **k):
        e.hold.closed.add(mtid)                                     # the master's CLOSE lands after our checks
        return await real(*a, **k)

    monkeypatch.setattr(e.loop, '_wait_for_position', hooked)


def test_a_hold_that_opened_after_the_close_gets_a_close_job_and_is_closed(monkeypatch, fast_sleep, notify_log,
                                                                         tmp_path):
    monkeypatch.setattr(htl, 'CLOSE_EVENTS', htl.make_close_event_log(str(tmp_path / 'c.jsonl')))
    e = _env(monkeypatch)
    _late_close_handler(e)
    _close_during_entry(e, monkeypatch, MTID)
    run(_enter(e))
    row, = _hold_rows(e)
    job = e.cdb.job(row['id'])
    assert job['status'] == 'pending' and 'LATE_ENTRY_AFTER_CLOSE' in e.db.error_types()
    e.cdb.closes[MTID].update(reason='leader_exit', symbol='BNB/USDT')
    run(e.loop.process_close_jobs())
    assert e.ex.positions == {} and e.cdb.job(row['id'])['status'] == 'done'


def test_an_ada_entry_that_opened_after_the_close_gets_a_close_job(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    _late_close_handler(e)
    e.hold.mtid_of_signal[77] = MTID
    _close_during_entry(e, monkeypatch, MTID)
    run(e.loop.execute_trade(e.user, _ada_item(e)))
    row, = [r for r in e.db.rows if (r.get('kind') or 't1') == 't1']
    assert e.cdb.job(row['id'])['status'] == 'pending'


def test_a_normal_entry_queues_no_close_job(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    _late_close_handler(e)
    run(_enter(e))
    assert e.cdb.jobs == {} and 'LATE_ENTRY_AFTER_CLOSE' not in e.db.error_types()


def test_a_delivery_acknowledged_by_the_close_is_never_entered_hold_or_ada(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    item = e.hold.add_delivery(e.user)
    e.db.acked_deliveries.add(item['delivery_id'])                  # broadcast-close acknowledged it first
    run(e.loop._execute_hold_for_user(item))
    ada = _ada_item(e)
    e.db.acked_deliveries.add(ada['delivery_id'])
    assert run(e.loop.execute_trade(e.user, ada)) is False
    assert e.ex.calls() == [] and e.db.rows == []


def test_the_acknowledge_is_one_atomic_update_that_returns_the_id(monkeypatch):
    assert 'acknowledged = false' in htl.SQL_ACK_DELIVERY and 'RETURNING id' in htl.SQL_ACK_DELIVERY
    e = _env(monkeypatch)
    assert run(e.loop.acknowledge_signal(5)) is True and run(e.loop.acknowledge_signal(5)) is False


@pytest.mark.parametrize('symbol', ['BNB/USDT', 'BTC/USDT', 'XRP/USDT'])
def test_the_t1_path_refuses_every_non_ada_symbol(monkeypatch, fast_sleep, notify_log, symbol):
    e = _env(monkeypatch)
    sig = dict(_ada_item(e), symbol=symbol)
    assert run(e.loop.execute_trade(e.user, sig)) is False
    assert e.ex.calls() == [] and e.pool.sql('UPDATE signal_deliveries') == []


def test_an_existing_row_for_the_same_signal_blocks_the_entry(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.db.add_row(user_id=e.user['id'], signal_id=77, status='closed')      # CHECK 0: any row, any status
    has_open, why = run(e.loop.check_any_open_positions_or_orders(e.ex, 'u', user_id=e.user['id'],
                                                                  signal_id='sig-ada-1'))
    assert has_open is True and why == 'Position already exists for this signal'


def test_the_exit_type_widen_is_guarded_and_the_hold_migration_fails_loud_and_closed():
    import ast
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'main.py'), encoding='utf-8') as f:
        src = f.read()
    node = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == '_HOLD_MIGRATIONS' for t in n.targets))
    do = [' '.join(ast.literal_eval(n).split()) for n in node.value.elts if ast.literal_eval(n).startswith('DO ')]
    assert len(do) == 1
    sql = do[0]
    for needle in ("table_name = 'trades'", "column_name = 'exit_type'", "data_type = 'character varying'",
                   'character_maximum_length < 16', 'ALTER TABLE trades ALTER COLUMN exit_type TYPE VARCHAR(16)',
                   'IF EXISTS (SELECT 1 FROM information_schema.columns', 'END IF'):
        assert needle in sql, needle
    assert "SET lock_timeout = '5s'" in src
    assert 'HOLD_MIGRATION_INCOMPLETE' in src and "os.environ['HOLD_ENABLED'] = '0'" in src
    assert 'notify_admin as _mig_notify' in src and 'INSERT INTO error_logs' in src


def test_every_close_cycle_sweeps_rows_of_already_closed_trades_into_jobs(monkeypatch, fast_sleep, notify_log, tmp_path):
    monkeypatch.setattr(htl, 'CLOSE_EVENTS', htl.make_close_event_log(str(tmp_path / 'c.jsonl')))
    e = _env(monkeypatch)
    run(_enter(e))
    row, = _hold_rows(e)
    e.hold.closed.add(MTID)                                          # the CLOSE raced the entry and the late check missed

    def sweep(method, sql, args):
        e.cdb.add_job(row, master_trade_id=MTID, side='BUY')
        return 'INSERT 0 1'

    e.pool.on(htl.SQL_LATE_CLOSE_SWEEP, sweep)
    run(e.loop.process_close_jobs())
    assert e.cdb.job(row['id'])['status'] == 'done' and e.ex.positions == {}
    assert 'LATE_ENTRY_AFTER_CLOSE' in e.db.error_types()
    assert 'ON CONFLICT (position_id) DO NOTHING' in htl.SQL_LATE_CLOSE_SWEEP
    assert "op.status IN ('open', 'needs_recovery')" in htl.SQL_LATE_CLOSE_SWEEP


def test_a_failing_sweep_never_blocks_the_close_jobs(monkeypatch, fast_sleep, notify_log, tmp_path):
    monkeypatch.setattr(htl, 'CLOSE_EVENTS', htl.make_close_event_log(str(tmp_path / 'c.jsonl')))
    e = _env(monkeypatch)
    run(_enter(e))
    row, = _hold_rows(e)
    e.cdb.add_job(row, master_trade_id=MTID, side='BUY')
    e.pool.fail_on(htl.SQL_LATE_CLOSE_SWEEP, RuntimeError('db'))
    run(e.loop.process_close_jobs())
    assert e.cdb.job(row['id'])['status'] == 'done'


def test_hold_flags_docstring_says_a_blank_allowlist_is_nobody():
    assert 'NOBODY' in htl.hold_flags.__doc__ and 'every active follower' not in htl.hold_flags.__doc__


# ---------------- main.py hold migrations at startup (executed against a fake psycopg2) ----------------

def _run_hold_migration_block(monkeypatch, fail, connect_error=None, migrated=False, connect_failures=0):
    import os
    import sys
    import textwrap
    import threading
    import types
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'main.py'), encoding='utf-8') as f:
        src = f.read()
    a = src.index('    # Run schema migrations BEFORE any ORM queries')          # fee_tier + CLOSE + HOLD, all of them
    b = src.index('    # T1-CLOSE MIGRATIONS:END')
    block = textwrap.dedent(src[a:b])
    executed = []

    class Cur:
        row = None

        def execute(self, stmt, *args):
            executed.append(stmt)
            if stmt.startswith('SELECT'):                 # the information_schema pre-checks
                self.row = (('YES',) if 'is_nullable' in stmt else (1,)) if migrated else None
                return
            if fail(stmt, executed.count(stmt)):
                raise RuntimeError('lock timeout')

        def fetchone(self):
            return self.row

        def close(self):
            pass

    class Conn:
        autocommit = False

        def cursor(self):
            return Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    attempts = {'n': 0}

    def connect(url):
        attempts['n'] += 1
        if connect_error or attempts['n'] <= connect_failures:
            raise connect_error or OSError('refused')
        return Conn()

    monkeypatch.setitem(sys.modules, 'psycopg2', types.SimpleNamespace(connect=connect))
    sleeps = []
    monkeypatch.setattr('time.sleep', lambda s: sleeps.append(s))
    threads = []

    class FakeThread:
        def __init__(self, target=None, **kw):
            threads.append(target)

        def start(self):
            pass

    monkeypatch.setattr(threading, 'Thread', FakeThread)
    monkeypatch.setenv('HOLD_ENABLED', '1')
    ns = {'DATABASE_URL': 'postgres://fake', 'os': os}
    try:
        exec(compile(block, 'main_hold_block', 'exec'), ns)
    finally:
        _run_hold_migration_block.sleeps, _run_hold_migration_block.connects = sleeps, attempts['n']
    return executed, threads


def _ddl(executed):
    return [s.strip() for s in executed if s.strip().startswith(('ALTER', 'CREATE', 'DO'))]


def test_hold_migrations_apply_cleanly(monkeypatch):
    executed, threads = _run_hold_migration_block(monkeypatch, lambda s, n: False)
    assert any('hold_offers' in s for s in executed) and threads == []
    import os
    assert os.environ['HOLD_ENABLED'] == '1'


def test_a_transient_lock_timeout_is_retried_and_succeeds(monkeypatch):
    executed, threads = _run_hold_migration_block(
        monkeypatch, lambda s, n: 'ADD COLUMN IF NOT EXISTS strategy' in s and n < 3)
    assert sum('ADD COLUMN IF NOT EXISTS strategy' in s for s in executed) == 3 and threads == []


@pytest.mark.parametrize('col', ['signals ADD COLUMN IF NOT EXISTS strategy',
                                 'open_positions ADD COLUMN IF NOT EXISTS kind',
                                 'follower_users ADD COLUMN IF NOT EXISTS hold_enabled'])
def test_a_failing_essential_column_aborts_startup(monkeypatch, col):
    with pytest.raises(RuntimeError, match='HOLD_MIGRATION_ESSENTIAL_FAILED'):
        _run_hold_migration_block(monkeypatch, lambda s, n: col in s)


def test_a_connection_failure_aborts_startup(monkeypatch):
    with pytest.raises(RuntimeError, match='MIGRATION_(ESSENTIAL_)?FAILED'):
        _run_hold_migration_block(monkeypatch, lambda s, n: False, connect_error=OSError('refused'))


def test_a_non_essential_failure_degrades_to_hold_off_with_a_background_alert(monkeypatch):
    import os
    executed, threads = _run_hold_migration_block(monkeypatch, lambda s, n: s.startswith('DO $$'))
    assert os.environ['HOLD_ENABLED'] == '0'
    assert len(threads) == 1 and callable(threads[0])                  # the alert runs on a thread, not at import
    assert sum(s.startswith('DO $$') for s in executed) == 3             # retried before degrading


def test_no_asyncio_run_runs_at_import_time_in_the_hold_block():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'main.py'), encoding='utf-8') as f:
        src = f.read()
    a = src.index('    # T1-CLOSE MIGRATIONS:START')
    block = src[a:src.index('    # T1-CLOSE MIGRATIONS:END')]
    assert block.count('asyncio.run') == 1 and block.index('def _alert') < block.index('asyncio.run')
    assert 'ADA path DOES' in block or 'the ADA path itself reads' in block


def test_a_migrated_database_issues_no_ddl_at_all_so_a_redeploy_takes_no_table_lock(monkeypatch):
    executed, threads = _run_hold_migration_block(monkeypatch, lambda s, n: False, migrated=True)
    ddl = [s for s in _ddl(executed) if not s.startswith('DO $$')]
    assert ddl == [], ddl                                        # zero ALTER / CREATE statements
    assert threads == []


def test_a_fresh_database_still_gets_every_statement(monkeypatch):
    executed, _ = _run_hold_migration_block(monkeypatch, lambda s, n: False, migrated=False)
    ddl = _ddl(executed)
    assert sum(s.startswith('ALTER TABLE') for s in ddl) >= 10 and sum(s.startswith('CREATE TABLE') for s in ddl) == 4


def test_only_the_missing_column_is_added(monkeypatch):
    import re
    executed, _ = _run_hold_migration_block(monkeypatch, lambda s, n: False, migrated=False)
    # pre-check queries precede each statement and carry the table and column as parameters, never in the text
    assert any('information_schema.columns' in s for s in executed)


@pytest.mark.parametrize('col', ['signals ADD COLUMN IF NOT EXISTS hold_r_b', 'signals ADD COLUMN IF NOT EXISTS hold_n2',
                                 'signals ADD COLUMN IF NOT EXISTS hold_leader_trade_id',
                                 'signals ADD COLUMN IF NOT EXISTS hold_leader_entry_ms',
                                 'signal_deliveries ADD COLUMN IF NOT EXISTS offer_ms'])
def test_the_orm_mapped_hold_columns_are_essential(monkeypatch, col):
    with pytest.raises(RuntimeError, match='HOLD_MIGRATION_ESSENTIAL_FAILED'):
        _run_hold_migration_block(monkeypatch, lambda s, n: col in s)


def test_the_connection_is_retried_seven_times_without_a_sleep_after_the_last(monkeypatch):
    with pytest.raises(RuntimeError, match='MIGRATION_(ESSENTIAL_)?FAILED'):
        _run_hold_migration_block(monkeypatch, lambda s, n: False, connect_failures=99)
    assert _run_hold_migration_block.connects == 7 and len(_run_hold_migration_block.sleeps) == 6
    executed, _ = _run_hold_migration_block(monkeypatch, lambda s, n: False, connect_failures=3)
    assert _run_hold_migration_block.connects == 6 and any('hold_offers' in s for s in executed)   # fee 4 + close + hold


def test_a_statement_is_tried_three_times_with_two_sleeps_only(monkeypatch):
    with pytest.raises(RuntimeError):
        _run_hold_migration_block(monkeypatch, lambda s, n: 'ADD COLUMN IF NOT EXISTS strategy' in s)
    assert _run_hold_migration_block.sleeps.count(1) >= 1 and _run_hold_migration_block.sleeps == [1, 2]


def test_railway_has_a_healthcheck_above_the_worst_case_startup():
    import json
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'railway.json'), encoding='utf-8') as f:
        deploy = json.load(f)['deploy']
    assert deploy['healthcheckPath'] == '/' and deploy['healthcheckTimeout'] >= 600


# ---------------- master contract: n2 is a notional / equity multiple ----------------

def test_the_masters_n2_above_one_is_accepted_and_sized():
    ref, stop, atr = 600.264, 570.1, 3.0
    n2 = 0.02 * ref / (2.5 * atr)
    r_b = n2 * (ref - stop) / ref
    assert round(n2, 4) == 1.6007 and round(r_b, 4) == 0.0804
    now = 1_790_000_000_000
    payload = {'master_trade_id': MTID, 'leader_trade_id': 'L-1', 'action': 'BUY', 'symbol': 'BNB/USDT',
               'ref_entry': '600.264', 'stop_loss': '570.1', 'r_b': '0.0804', 'n2': '1.6007',
               'leader_entry_ms': now - 3_600_000, 'offer_ms': now - 1000, 'offer_kind': 'entry', 'protocol': 'hold-1'}
    assert hg.validate_hold_payload(payload, {'BNB/USDT': 'PF_BNBUSD'}, now) is None
    s = hg.size_hold(10000, 600.264, '600.264', '570.1', '0.0804', '1.6007', 0.01, 10000)
    assert s.ok and 26 < s.qty < 27 and s.margin_needed < 10000 * 0.95      # risk leg ~26.66, cap ~26.67
    assert hg.validate_hold_payload(dict(payload, n2='10.01'), {'BNB/USDT': 'x'}, now).startswith('n2')


def test_a_big_n2_is_still_bounded_by_the_ten_x_margin_fit():
    s = hg.size_hold(10000, 600.0, 600.0, 570.0, 0.15, 10, 0.01, 1000)
    assert s.ok and s.shrunk and s.margin_needed <= 1000 * 0.95 + 1e-9


# ---------------- QC: the fee_tier and CLOSE migrations get the same treatment ----------------

def test_a_fully_migrated_database_issues_zero_ddl_across_all_migration_blocks(monkeypatch):
    executed, threads = _run_hold_migration_block(monkeypatch, lambda s, n: False, migrated=True)
    ddl = [s for s in _ddl(executed) if not s.startswith('DO $$')]
    assert ddl == [], ddl                          # fee_tier, every CLOSE statement incl. the unique index, every HOLD one
    assert any('pg_indexes' in s for s in executed) and any("column_name = 'fee_tier'" in s for s in executed)
    assert threads == []


def test_a_fresh_database_runs_fee_tier_and_the_close_statements_under_lock_timeout(monkeypatch):
    executed, _ = _run_hold_migration_block(monkeypatch, lambda s, n: False, migrated=False)
    ddl = _ddl(executed)
    assert any('fee_tier' in s for s in ddl) and any('ux_signals_master_trade_id' in s for s in ddl)
    assert any('ix_close_jobs_status' in s for s in ddl) and any('close_fill_qty' in s for s in ddl)
    assert sum("SET lock_timeout = '5s'" in s for s in executed) == 3      # fee_tier, CLOSE, HOLD sessions


def test_the_fee_tier_alter_is_retried_then_aborts_startup(monkeypatch):
    with pytest.raises(RuntimeError, match='FEE_TIER_MIGRATION_FAILED'):
        _run_hold_migration_block(monkeypatch, lambda s, n: 'fee_tier VARCHAR' in s)
    assert _run_hold_migration_block.sleeps == [1, 2][:2] or _run_hold_migration_block.sleeps[:2] == [1, 2]


@pytest.mark.parametrize('needle', ['signals ADD COLUMN IF NOT EXISTS master_trade_id', 'ux_signals_master_trade_id',
                                    'open_positions ADD COLUMN IF NOT EXISTS exit_hint',
                                    'open_positions ADD COLUMN IF NOT EXISTS close_fill_qty',
                                    'CREATE TABLE IF NOT EXISTS master_closes', 'CREATE TABLE IF NOT EXISTS close_jobs'])
def test_an_essential_close_statement_failing_aborts_startup(monkeypatch, needle):
    with pytest.raises(RuntimeError, match='CLOSE_MIGRATION_ESSENTIAL_FAILED'):
        _run_hold_migration_block(monkeypatch, lambda s, n: needle in s)


def test_a_failing_close_index_degrades_and_alerts_but_keeps_hold_on(monkeypatch):
    import os
    executed, threads = _run_hold_migration_block(monkeypatch, lambda s, n: 'ix_close_jobs_status' in s)
    assert len(threads) == 1 and os.environ['HOLD_ENABLED'] == '1'
    assert sum('ix_close_jobs_status' in s and s.startswith('CREATE') for s in executed) == 3


# ---------------- the same Binance hold under a second master_trade_id ----------------

MTID_B = 'fedcba9876543210fedcba9876543210'


def test_a_new_master_trade_id_for_a_known_leader_trade_is_refused_with_409_and_an_alarm(monkeypatch, tmp_path):
    a = _api(monkeypatch, tmp_path)
    with a.engine.begin() as c:
        c.execute(a.text('CREATE TABLE error_logs (api_key TEXT, error_type TEXT, error_message TEXT, context TEXT)'))
    assert _post_hold(a, _hold_payload()).status_code == 200
    r = _post_hold(a, _hold_payload(master_trade_id=MTID_B, offer_ms=_hold_payload()['offer_ms'] + 50))
    assert r.status_code == 409 and 'HOLD_DUPLICATE_LEADER_TRADE' in r.json()['detail']
    assert _count(a, 'SELECT COUNT(*) FROM signals') == 1 and _count(a, 'SELECT COUNT(*) FROM hold_offers') == 1
    assert _count(a, 'SELECT COUNT(*) FROM signal_deliveries') == 4
    assert _q(a, 'SELECT error_type FROM error_logs') == [('HOLD_DUPLICATE_LEADER_TRADE',)]


def test_the_refusal_does_not_need_the_error_logs_table_and_a_different_leader_trade_is_fine(monkeypatch, tmp_path):
    a = _api(monkeypatch, tmp_path)
    assert _post_hold(a, _hold_payload()).status_code == 200
    assert _post_hold(a, _hold_payload(master_trade_id=MTID_B, offer_ms=_hold_payload()['offer_ms'] + 50)
                      ).status_code == 409                                 # no error_logs table here: still refused
    r = _post_hold(a, _hold_payload(master_trade_id=MTID_B, leader_trade_id='L-2',
                                    offer_ms=_hold_payload()['offer_ms'] + 60))
    assert r.status_code == 200                                             # a genuinely new Binance hold


def test_a_user_holding_the_same_leader_trade_gets_no_delivery_even_for_another_mtid(monkeypatch, tmp_path):
    import follower_endpoints as fe
    a = _api(monkeypatch, tmp_path)
    _post_hold(a, _hold_payload())
    s = a.Session()
    fm = a.fm
    sig = s.query(fm.Signal).one()
    # a second hold signal row carrying the same leader trade (e.g. written before this check existed)
    twin = fm.Signal(signal_id='sig-twin', action='BUY', symbol='BNB/USDT', entry_price=600.0, stop_loss=570.0,
                     take_profit=None, strategy='hold', hold_r_b=0.02, hold_n2=0.5, hold_leader_trade_id='L-1',
                     master_trade_id=MTID_B)
    s.add(twin)
    s.flush()
    uid = s.query(fm.User).filter_by(api_key='nk_hold_user_1').one().id
    s.add(fm.OpenPosition(user_id=uid, signal_id=sig.id, entry_order_id='E', symbol='BNB/USDT',
                          kraken_symbol='PF_BNBUSD', side='BUY', quantity=1.0, target_tp=None, target_sl=570.0,
                          kind='hold', status='open'))
    for d in s.query(fm.SignalDelivery).all():
        d.acknowledged = True
    s.commit()
    eligible = {u.api_key for u in fe._hold_eligible_users(s, twin.id, MTID_B, 'L-1')}
    s.close()
    assert 'nk_hold_user_1' not in eligible and 'nk_hold_user_4' in eligible


def test_execute_never_opens_a_second_hold_for_the_same_leader_trade(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.db.add_row(user_id=e.user['id'], symbol='BNB/USDT', kraken_symbol='PF_BNBUSD', kind='hold', status='open',
                 signal_id=99, target_tp=None, sl_order_id='x')
    e.hold.leader_of_signal[99] = 'L1'
    run(_enter(e, mtid=MTID_B, delivery_id=8101))                          # same leader trade, a new master_trade_id
    assert _entries(e.ex) == [] and 'HOLD_DUPLICATE_LEADER_TRADE' in e.db.error_types()
    assert len(e.pool.sql('UPDATE signal_deliveries')) == 1                # acknowledged: never retried


def test_a_different_leader_trade_is_not_blocked_by_the_same_leader_check(monkeypatch, fast_sleep, notify_log):
    e = _env(monkeypatch)
    e.hold.leader_of_signal[99] = 'L1'
    e.db.add_row(user_id=e.user['id'], symbol='BNB/USDT', kraken_symbol='PF_BNBUSD', kind='hold', status='closed',
                 signal_id=99, target_tp=None)
    run(_enter(e, mtid=MTID_B, delivery_id=8102, leader='L-2'))
    assert len(_entries(e.ex)) == 1 and 'HOLD_DUPLICATE_LEADER_TRADE' not in e.db.error_types()
