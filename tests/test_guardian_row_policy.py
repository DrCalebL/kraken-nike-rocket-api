"""
T23 kraken_follower_row_policy_inflight_and_load (spec C37, I5, I12, I17, R3-M6, A2, R3-L7, AC14/AC18/AC20).

Row ownership (one consistent active row or ROW_AMBIGUOUS; only position_monitor changes
'open' rows; only service orders are cancelled; rowless positions keep today's rule),
evidence-based flats (FlatTracker + fills, A2's since/entry-order exclusion), in-flight
yielding and the load bounds of the guardian's own executor.
"""
import asyncio
import time
from datetime import timedelta

import ccxt
import pytest

import follower_guard
import position_guardian as pg
from hosted_trading_loop import HostedTradingLoop
from position_monitor import PositionMonitor
from tests.fakes import guardian_env, notify_log  # noqa: F401  (pytest fixture)

SYM = 'ADA/USD:USD'


# ---------------- legacy paths yield to in-flight executions (every mode) ----------------

@pytest.mark.parametrize('mode', ['legacy', 'shadow', 'enforce'])
async def test_legacy_paths_skip_busy_users_in_every_mode(monkeypatch, notify_log, mode):
    env = guardian_env(monkeypatch, mode=mode)
    user, ex = env.user()
    ex.open_position(SYM, 'long', 100, 0.5)
    row = env.row(user, status='needs_recovery')
    tok = follower_guard.mark(user['id'])
    try:
        assert await env.g._check_user_for_unprotected(dict(user)) == 0
        await env.g._recover_single_position(next(r for r in env.db._needs_recovery_rows('fetch', '', ())
                                                  if r['id'] == row['id']))
    finally:
        follower_guard.clear(user['id'], tok)
    assert ex.calls() == [] and env.db.row(row['id'])['status'] == 'needs_recovery'


async def test_an_inflight_token_that_ages_out_is_reported_as_a_critical_alarm(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='legacy')
    user, ex = env.user()
    now = [1000.0]
    monkeypatch.setattr(follower_guard, '_clock', lambda: now[0])
    gate = asyncio.Event()

    async def stuck_execute_trade():
        tok = follower_guard.mark(user['id'])
        try:
            await gate.wait()
        finally:
            follower_guard.clear(user['id'], tok)

    task = asyncio.create_task(stuck_execute_trade())
    await asyncio.sleep(0)
    now[0] += follower_guard.INFLIGHT_MAX_AGE_S + 1
    assert follower_guard.is_inflight(user['id']) is False       # aged out while execute_trade still runs (A6)
    await env.g.run_cycle(1, 'legacy')
    gate.set()
    await task
    assert 'ALARM_INFLIGHT_TOKEN_EXPIRED' in env.db.error_types()
    assert any(n['title'].endswith('ALARM_INFLIGHT_TOKEN_EXPIRED') and n['level'] == 'error' for n in notify_log)


# ---------------- row ownership, rowless rule, in-flight, TP crossed, load (enforce) ----------------

async def test_two_active_rows_are_ambiguous_and_nothing_is_touched(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    env.row(user, quantity=1000.0, target_sl=0.49, target_tp=0.52, sl_order_id='old-sl', tp_order_id='old-tp',
            opened_at=env.clock.utcnow() - timedelta(days=2))
    sl = ex.add_order(SYM, 'stop', 'sell', 800, 0.46)
    tp = ex.add_order(SYM, 'lmt', 'sell', 800, 0.49)
    env.row(user, quantity=800.0, entry_fill_price=0.47, target_sl=0.46, target_tp=0.49, sl_order_id=sl, tp_order_id=tp)
    ex.open_position(SYM, 'long', 800, 0.47, ts_ms=env.clock.ms() - 600_000)
    ex.prices[SYM] = 0.47
    await env.g.invariant_pass(full=True, dry=False)
    assert env.order_calls(ex) == [] and env.pool.mutations() == []
    assert 'GUARDIAN_ROW_AMBIGUOUS' in env.db.error_types()


async def test_after_a_tp_fill_the_guardian_waits_for_position_monitor(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    ex.set_price(SYM, 0.52)                                   # TP fills; the SL leftover stays (no OCO)
    assert ex.positions == {} and list(ex.orders) == [sl_oid]
    await env.g.invariant_pass(full=True, dry=False)
    assert ex.calls('cancel_order') == [] and env.pool.mutations() == []
    loop = HostedTradingLoop(env.pool)
    has_open, reason = await loop.check_any_open_positions_or_orders(ex, 'u', user_id=None, signal_id=None)
    assert has_open is True and reason.startswith('Open orders exist')       # dedup keeps skipping
    monitor = PositionMonitor(env.pool)
    monitor.active_exchanges[user['api_key']] = ex
    monkeypatch.setattr(monitor, 'decrypt_credentials', lambda k, s: ('key', 'secret'))

    async def no_fill_scan(user_info):
        return []

    monkeypatch.setattr(monitor, 'scan_exchange_fills', no_fill_scan)
    await monitor.check_position(dict(env.db.row(row['id']), user_api_key=user['api_key'],
                                      kraken_api_key_encrypted='k', kraken_api_secret_encrypted='s',
                                      avg_entry_price=None, filled_quantity=None, fill_count=1,
                                      total_cost_basis=None, first_fill_at=None, fee_tier='standard'))
    assert len(env.db.trades) == 1 and env.db.row(row['id'])['status'] == 'closed'
    assert ex.orders == {}                                    # position_monitor cancelled the SL
    before = len(env.order_calls(ex))
    await env.g.invariant_pass(full=True, dry=False)
    assert len(env.order_calls(ex)) == before


@pytest.mark.parametrize('legs', [
    [('lmt', 0.52)],                                          # limit-TP only
    [('stop_limit', 0.49)],                                   # stop-limit only
    [('stop', 0.49), ('lmt', 0.51), ('lmt', 0.53)],           # scale-out
])
async def test_rowless_protected_positions_are_never_touched(monkeypatch, notify_log, legs):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    ex.open_position(SYM, 'long', 100, 0.5, ts_ms=env.clock.ms() - 900_000)
    for kind, level in legs:
        if kind == 'stop_limit':
            ex.add_order(SYM, 'stop', 'sell', 100, level, limit=level)
        else:
            ex.add_order(SYM, kind, 'sell', 50 if len(legs) > 1 and kind == 'lmt' else 100, level)
    for cycle in (1, 2, 3, 4, 5):
        await env.g.run_cycle(cycle, 'enforce')
    assert env.order_calls(ex) == [] and SYM in ex.positions


@pytest.mark.parametrize('legs', [[], [('take_profit', 0.52)]])
async def test_rowless_unprotected_positions_close_only_on_the_fifth_cycle(monkeypatch, notify_log, legs):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    ex.open_position(SYM, 'long', 100, 0.5, ts_ms=env.clock.ms() - 900_000)
    for kind, level in legs:
        ex.add_order(SYM, kind, 'sell', 100, level)
    for cycle in (1, 2, 3, 4):
        await env.g.run_cycle(cycle, 'enforce')
    assert SYM in ex.positions
    await env.g.run_cycle(5, 'enforce')
    assert ex.positions == {}


async def test_a_customer_stop_on_a_rowed_position_is_kept(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    env.bracket(user, ex)
    ex.add_order(SYM, 'stop', 'sell', 100, 0.47, oid='cust-stop')
    await env.g.invariant_pass(full=False, dry=False)
    assert 'cust-stop' in ex.orders and ex.calls('cancel_order') == []
    assert 'GUARDIAN_FOREIGN_LEG_KEPT' in env.db.error_types()


async def test_acknowledged_at_from_another_process_is_skipped(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    ex.external_cancel(sl_oid)
    env.db.ack(user['id'], env.clock.utcnow() - timedelta(seconds=30))
    await env.g.invariant_pass(full=False, dry=False)
    assert ex.calls() == []
    env.clock.advance(200)                                    # the acknowledgment ages out of the 180 s window
    await env.g.invariant_pass(full=False, dry=False)
    assert [info['type'] for m, info in ex.calls('create_order')] == ['stop']


async def test_tp_level_crossed_is_never_placed(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    ex.external_cancel(tp_oid)
    ex.prices[SYM] = 0.53                                     # a long's 0.52 TP is already crossed
    await env.g.invariant_pass(full=False, dry=False)
    assert env.order_calls(ex) == []
    assert 'GUARDIAN_TP_LEVEL_CROSSED' in env.db.error_types()


async def test_load_200_users_bounded_concurrency_rows_first_and_own_executor(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce', latency_s=0.02, max_concurrency=8)
    rowed = []
    for i in range(200):
        user, ex = env.user()
        env.g._markets_loaded.add(user['api_key'])            # steady state: markets were loaded at startup
        if i >= 150:
            env.bracket(user, ex)
            rowed.append(user['api_key'])
    entered = []
    real = env.g._enforce_user

    async def recording(user, dry, active_ids):
        entered.append(user['api_key'])
        return await real(user, dry, active_ids)

    monkeypatch.setattr(env.g, '_enforce_user', recording)
    t0 = time.monotonic()
    await env.g.invariant_pass(full=True, dry=False)
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0
    assert 1 <= env.venue.peak <= 8
    assert set(entered[:50]) == set(rowed) and len(entered) == 200     # users with active rows first
    assert all(thread.startswith('guardian') for _a, _m, _i, thread in env.venue.calls)


async def test_a_pass_longer_than_its_cadence_raises_budget_and_passes_never_overlap(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce', latency_s=0.02, max_concurrency=2)
    for _ in range(40):
        user, ex = env.user()
        env.g._markets_loaded.add(user['api_key'])
        env.bracket(user, ex)
    env.g.cadence_s = 0.5
    running, peak = [0], [0]
    real = env.g._pass_users

    async def tracked(full):
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        try:
            await asyncio.sleep(0)
            return await real(full)
        finally:
            running[0] -= 1

    monkeypatch.setattr(env.g, '_pass_users', tracked)
    await asyncio.gather(env.g.invariant_pass(full=False, dry=False),
                         env.g.invariant_pass(full=False, dry=False))
    assert peak[0] == 1
    assert 'GUARDIAN_BUDGET' in env.db.error_types()


# ---------------- confirmed flats and fill evidence (R3-M6, A2) ----------------

async def test_flat_open_row_alarms_hourly_and_a_manual_reopen_is_ambiguous(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    await env.g.invariant_pass(full=False, dry=False)          # live pass: fingerprint noted
    ex.positions.clear()                                      # flat (the SL/TP leftovers stay)
    for step in (0, 300, 400, 600):
        env.clock.advance(step)
        await env.g.invariant_pass(full=False, dry=False)
    assert env.db.error_types().count('GUARDIAN_FLAT_ROW_UNRECORDED') == 1
    assert env.order_calls(ex) == [] and env.pool.mutations() == []
    ex.create_market_order(SYM, 'buy', 100)                    # the customer re-opens by hand
    await env.g.invariant_pass(full=False, dry=False)
    assert 'GUARDIAN_ROW_AMBIGUOUS' in env.db.error_types()
    assert [c for c in env.order_calls(ex) if c[0] != 'create_market_order'] == []


async def test_needs_recovery_closed_manual_only_after_240s_confirmed_flat(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row = env.row(user, status='needs_recovery', sl_order_id='svc-sl')
    ex.add_order(SYM, 'stop', 'sell', 100, 0.49, oid='svc-sl')
    open_row = env.row(user, symbol='BTC/USDT', kraken_symbol='PF_XBTUSD', status='open',
                       target_sl=59000.0, target_tp=62000.0, quantity=0.01)
    await env.g.invariant_pass(full=False, dry=False)
    env.clock.advance(120)
    await env.g.invariant_pass(full=False, dry=False)
    assert env.db.row(row['id'])['status'] == 'needs_recovery' and 'svc-sl' in ex.orders
    env.clock.advance(130)
    await env.g.invariant_pass(full=False, dry=False)
    assert env.db.row(row['id'])['status'] == 'closed_manual' and ex.orders == {}
    (_, sql, args), = env.pool.sql('SET status = $1, last_fill_at = NOW()')
    assert "AND status = 'needs_recovery'" in sql and args == ('closed_manual', row['id'])
    assert env.db.row(open_row['id'])['status'] == 'open'     # CLEAR never touches an 'open' row


async def test_rowless_flat_leftovers_only_service_orders_are_cancelled(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    env.row(user, status='closed', sl_order_id='svc-sl', last_fill_at=env.clock.utcnow() - timedelta(minutes=5))
    ex.add_order(SYM, 'stop', 'sell', 100, 0.49, oid='svc-sl')
    ex.add_order(SYM, 'lmt', 'sell', 50, 0.60, oid='cust-tp')
    await env.g.invariant_pass(full=True, dry=False)
    assert pg.bg.FLAT_CONFIRM_S in env.sleep.delays
    assert list(ex.orders) == ['cust-tp']
    assert 'GUARDIAN_FOREIGN_LEG_KEPT' in env.db.error_types()


async def test_execute_trade_starting_during_confirm_flat_blocks_every_mutation(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    env.row(user, status='closed', sl_order_id='svc-sl', last_fill_at=env.clock.utcnow() - timedelta(minutes=5))
    ex.add_order(SYM, 'stop', 'sell', 100, 0.49, oid='svc-sl')
    tokens = []
    env.sleep.hooks.append(lambda: tokens.append(follower_guard.mark(user['id'])))
    try:
        await env.g.invariant_pass(full=True, dry=False)
    finally:
        for tok in tokens:
            follower_guard.clear(user['id'], tok)
    assert tokens and ex.calls('cancel_order') == [] and 'svc-sl' in ex.orders
    assert 'GUARDIAN_YIELD_INFLIGHT' in env.db.error_types()


async def test_single_empty_read_is_never_counted_as_flat(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    ex.empty_position_reads = 1
    await env.g.invariant_pass(full=False, dry=False)
    assert pg.bg.FLAT_CONFIRM_S in env.sleep.delays                 # the 5 s confirming re-read happened
    assert row['id'] not in env.g._flat.flat and env.order_calls(ex) == []


async def _two_false_flats(env, ex):
    await env.g.invariant_pass(full=False, dry=False)          # live: fingerprint noted
    for _ in range(2):
        env.clock.advance(120)
        ex.empty_position_reads = 2                           # both the read and the 5 s re-read are empty
        await env.g.invariant_pass(full=False, dry=False)
    env.clock.advance(120)


async def test_false_flats_then_the_same_fill_time_clears_and_repairs(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    await _two_false_flats(env, ex)
    assert env.g._flat.pending_check(row['id'])
    ex.external_cancel(sl_oid)
    await env.g.invariant_pass(full=False, dry=False)
    assert 'GUARDIAN_FALSE_FLAT_CLEARED' in env.db.error_types()
    assert [info['type'] for m, info in ex.calls('create_order')] == ['stop']
    assert ex.calls('fetch_my_trades') == []                  # equal fingerprint: no fills read needed


async def test_false_flats_then_a_new_fill_time_without_an_entry_fill_clears_and_repairs(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    await _two_false_flats(env, ex)
    # fillTime moved (still within ROW_POS_SKEW_S of the row) but no entry-side fill exists
    ex.positions[SYM]['timestamp'] = int(follower_guard.row_ts(row['opened_at']) * 1000) + 60_000
    ex.external_cancel(sl_oid)
    await env.g.invariant_pass(full=False, dry=False)
    assert len(ex.calls('fetch_my_trades')) == 1
    assert [info['type'] for m, info in ex.calls('create_order')] == ['stop']


async def test_fills_read_error_means_no_action_that_pass(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    await _two_false_flats(env, ex)
    ex.positions[SYM]['timestamp'] = int(follower_guard.row_ts(row['opened_at']) * 1000) + 60_000
    ex.external_cancel(sl_oid)
    ex.fault('fetch_my_trades', ccxt.NetworkError('fills timeout'))
    await env.g.invariant_pass(full=False, dry=False)
    assert ex.calls('create_order') == [] and 'GUARDIAN_READ_FAILED' in env.db.error_types()
    await env.g.invariant_pass(full=False, dry=False)
    assert [info['type'] for m, info in ex.calls('create_order')] == ['stop']


async def test_a2_the_rows_own_entry_fill_is_not_new_entry_evidence(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    entry = ex.create_market_order(SYM, 'buy', 100)            # the row's own entry fill (t0)
    ex.positions.clear()                                      # hidden: the reads come back empty
    env.clock.advance(5)
    row = env.row(user, status='needs_recovery', entry_order_id=entry['id'], quantity=100.0,
                  opened_at=env.clock.utcnow())
    for _ in range(2):                                        # two confirmed flats, inside the 60 s pad
        env.clock.advance(15)
        await env.g.invariant_pass(full=False, dry=False)
    ex.open_position(SYM, 'long', 100, 0.5, ts_ms=env.clock.ms() - 40_000)   # the position becomes visible
    await env.g.invariant_pass(full=False, dry=False)
    assert 'GUARDIAN_ROW_AMBIGUOUS' not in env.db.error_types()
    assert [info['type'] for m, info in ex.calls('create_order')] == ['stop', 'limit']
    assert env.db.row(row['id'])['status'] == 'open'


# ---------------- QC coverage-followers-6: busy re-checks right before a legacy market close ----------------

def _mark_busy_on_orders_read(monkeypatch, ex, user, tokens):
    real = ex.fetch_open_orders

    def fetch_open_orders(*args, **kwargs):
        if not tokens:                                   # execute_trade starts after the first busy check
            tokens.append(follower_guard.mark(user['id']))
        return real(*args, **kwargs)

    monkeypatch.setattr(ex, 'fetch_open_orders', fetch_open_orders)


@pytest.mark.parametrize('mode', ['legacy', 'shadow'])
async def test_legacy_recovery_rechecks_busy_after_its_reads_and_never_closes(monkeypatch, notify_log, mode):
    env = guardian_env(monkeypatch, mode=mode)
    user, ex = env.user()
    row = env.row(user, status='needs_recovery', sl_order_id=None, tp_order_id=None)
    ex.open_position(SYM, 'long', 100, 0.5)
    tokens = []
    _mark_busy_on_orders_read(monkeypatch, ex, user, tokens)
    try:
        await env.g._recover_single_position(next(r for r in env.db._needs_recovery_rows('fetch', '', ())
                                                  if r['id'] == row['id']))
    finally:
        for tok in tokens:
            follower_guard.clear(user['id'], tok)
    assert tokens and ex.calls('create_order') == [] and SYM in ex.positions
    assert env.db.row(row['id'])['status'] == 'needs_recovery'


@pytest.mark.parametrize('rowless_only', [False, True])
async def test_rowless_scan_rechecks_busy_before_closing(monkeypatch, notify_log, rowless_only):
    env = guardian_env(monkeypatch, mode='enforce' if rowless_only else 'legacy')
    user, ex = env.user()
    ex.open_position(SYM, 'long', 100, 0.5)              # rowless, no orders: today's rule would close it
    tokens = []
    _mark_busy_on_orders_read(monkeypatch, ex, user, tokens)
    try:
        closed = await env.g._check_user_for_unprotected(dict(user), rowless_only=rowless_only)
    finally:
        for tok in tokens:
            follower_guard.clear(user['id'], tok)
    assert tokens and closed == 0 and ex.calls('create_order') == [] and SYM in ex.positions
