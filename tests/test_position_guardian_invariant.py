"""
T22 kraken_follower_guardian_invariant (spec C37, I1-I4, I16-I17, AC18/AC19).

Enforce: chaos cases a-d on one consistent 'open' row, orders read before positions (both
no-arg, guardian threads), failed reads never act (the 3rd in a row raises GUARDIAN_BLIND),
repaired ids UPDATEd with the status guard,
per-symbol evaluation (another symbol's row no longer hides a naked rowed position),
needs_recovery re-bracketed and restored, invalid levels legacy-closed, agent_active=false
users still protected, flat-aware closes, critical alarms deduped per hour.
Shadow: legacy acts, the invariant only logs GUARDIAN_WOULD_<KIND>. Legacy: today's rule.
"""
from datetime import timedelta

import ccxt

import position_guardian as pg
from tests.fakes import guardian_env, notify_log  # noqa: F401  (pytest fixture)

SYM = 'ADA/USD:USD'


# ---------------- legacy paths and flat-aware closes (legacy/shadow modes) ----------------

def _creates(ex):
    return [info for m, info in ex.calls('create_order')]


async def test_attempt_market_close_on_a_flat_symbol_sends_no_order(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    result = await env.g._attempt_market_close(ex, 'PF_ADAUSD', 'sell', 100)
    assert result == pg.ALREADY_FLAT and env.order_calls(ex) == []


async def test_shadow_legacy_still_closes_needs_recovery_rows(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='shadow')
    user, ex = env.user()
    ex.open_position(SYM, 'long', 100, 0.5)
    row = env.row(user, status='needs_recovery', quantity=100.0)
    await env.g.run_cycle(1, 'shadow')
    assert ex.positions == {} and env.db.row(row['id'])['status'] == 'closed'


async def test_legacy_mode_keeps_todays_rowless_rule_on_the_fifth_cycle(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='legacy')
    user, ex = env.user()
    ex.open_position(SYM, 'short', 100, 0.5)            # no row, no reduce-only order: today's close rule
    for cycle in (1, 2, 3, 4):
        await env.g.run_cycle(cycle, 'legacy')
    assert SYM in ex.positions and env.order_calls(ex) == []
    await env.g.run_cycle(5, 'legacy')
    assert ex.positions == {}


# ---------------- the two-leg invariant (enforce acts, shadow logs GUARDIAN_WOULD_<KIND>) ----------------

async def test_case_a_cancelled_tp_is_replaced_and_its_id_updated(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    ex.external_cancel(tp_oid)
    await env.g.invariant_pass(full=False, dry=False)
    (info,) = _creates(ex)
    assert (info['type'], info['side'], info['amount'], info['price'], info['params']) == \
        ('limit', 'sell', 100.0, 0.52, {'reduceOnly': True})
    new_tp = env.db.row(row['id'])['tp_order_id']
    assert new_tp in ex.orders and new_tp != tp_oid
    (_, sql, _args), = env.pool.sql('UPDATE open_positions SET tp_order_id')
    assert "AND status IN ('open', 'needs_recovery')" in sql
    assert ex.calls('cancel_order') == []


async def test_case_b_cancelled_sl_is_replaced_with_a_stop_market(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    ex.external_cancel(sl_oid)
    await env.g.invariant_pass(full=False, dry=False)
    (info,) = _creates(ex)
    assert (info['type'], info['side'], info['amount'], info['price'], info['params']) == \
        ('stop', 'sell', 100.0, None, {'triggerPrice': 0.49, 'reduceOnly': True})
    assert env.db.row(row['id'])['sl_order_id'] in ex.orders
    (_, sql, _args), = env.pool.sql('UPDATE open_positions SET sl_order_id')
    assert "AND status IN ('open', 'needs_recovery')" in sql
    assert any(n['title'].endswith('SL_REPAIRED') for n in notify_log)


async def test_case_c_both_legs_cancelled_sl_is_placed_first(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    ex.external_cancel(sl_oid)
    ex.external_cancel(tp_oid)
    await env.g.invariant_pass(full=False, dry=False)
    assert [i['type'] for i in _creates(ex)] == ['stop', 'limit']


async def test_case_d_sl_missing_with_price_beyond_closes_and_leaves_the_open_row(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    ex.external_cancel(sl_oid)
    ex.prices[SYM] = 0.485                               # beyond the 0.49 SL of a long
    await env.g.invariant_pass(full=False, dry=False)
    (info,) = _creates(ex)
    assert info['type'] == 'market' and info['params'] == {'reduceOnly': True} and info['amount'] == 100.0
    assert ex.positions == {}
    assert env.db.row(row['id'])['status'] == 'open'     # position_monitor records the P&L
    assert 'GUARDIAN_CLOSE' in env.db.error_types()


async def test_reads_orders_first_then_positions_no_arg_on_guardian_threads(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    env.bracket(user, ex)
    await env.g.invariant_pass(full=False, dry=False)
    reads = [(m, info) for m, info in ex.calls() if m in ('fetch_open_orders', 'fetch_positions')]
    assert reads[0] == ('fetch_open_orders', {'symbol': None})
    assert reads[1] == ('fetch_positions', {'symbols': None})
    assert all(t.startswith('guardian') for t in ex.threads())


async def test_read_failure_makes_zero_order_calls(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    ex.external_cancel(sl_oid)
    ex.fault('fetch_open_orders', ccxt.NetworkError('read timeout'))
    await env.g.invariant_pass(full=False, dry=False)
    assert env.order_calls(ex) == []
    assert 'GUARDIAN_READ_FAILED' in env.db.error_types()


async def test_three_failed_reads_in_a_row_raise_guardian_blind_and_never_act(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    ex.external_cancel(sl_oid)                                             # a repair is due: blind passes never act
    ex.fault('load_markets', ccxt.NetworkError('markets timeout'))         # pass 1
    ex.fault('fetch_open_orders', ccxt.NetworkError('orders timeout'))     # pass 2
    ex.fault('fetch_positions', ccxt.NetworkError('positions timeout'))    # pass 3
    for n in (1, 2, 3):
        await env.g.invariant_pass(full=False, dry=False)
        assert env.db.error_types().count('GUARDIAN_BLIND') == (1 if n == 3 else 0)
    assert env.order_calls(ex) == [] and 'GUARDIAN_READ_FAILED' in env.db.error_types()
    assert [x['level'] for x in notify_log if x['title'].endswith('GUARDIAN_BLIND')] == ['error']
    await env.g.invariant_pass(full=False, dry=False)                      # a good read: SL repaired, count reset
    assert [i['type'] for i in _creates(ex)] == ['stop']
    env.clock.advance(3700)                                                # past the hourly alarm dedup
    ex.fault('fetch_positions', ccxt.NetworkError('positions timeout'), times=2)
    for _ in range(2):
        await env.g.invariant_pass(full=False, dry=False)
    assert env.db.error_types().count('GUARDIAN_BLIND') == 1               # 2 failures after a good read: not blind


async def test_an_open_row_on_another_symbol_no_longer_hides_a_naked_rowed_position(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    env.row(user, symbol='BTC/USDT', kraken_symbol='PF_XBTUSD', sl_order_id='x-sl', tp_order_id='x-tp',
            target_sl=59000.0, target_tp=62000.0, quantity=0.01)
    ex.open_position('BTC/USD:USD', 'long', 0.01, 60000.0, ts_ms=env.clock.ms() - 700_000)
    ex.add_order('BTC/USD:USD', 'stop', 'sell', 0.01, 59000.0, oid='x-sl')
    ex.add_order('BTC/USD:USD', 'lmt', 'sell', 0.01, 62000.0, oid='x-tp')
    row, sl_oid, tp_oid = env.bracket(user, ex)
    ex.external_cancel(sl_oid)
    await env.g.invariant_pass(full=False, dry=False)
    (info,) = _creates(ex)
    assert info['symbol'] == SYM and info['type'] == 'stop'


async def test_needs_recovery_row_with_price_inside_is_rebracketed_and_set_open(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    ex.open_position(SYM, 'long', 100, 0.5, ts_ms=env.clock.ms() - 125_000)
    row = env.row(user, status='needs_recovery', quantity=100.0, opened_at=env.clock.utcnow() - timedelta(seconds=120))
    await env.g.invariant_pass(full=False, dry=False)
    assert [i['type'] for i in _creates(ex)] == ['stop', 'limit']
    r = env.db.row(row['id'])
    assert r['status'] == 'open' and r['sl_order_id'] in ex.orders and r['tp_order_id'] in ex.orders


async def test_needs_recovery_row_with_invalid_levels_is_legacy_closed(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    ex.open_position(SYM, 'long', 100, 0.5, ts_ms=env.clock.ms() - 700_000)
    row = env.row(user, status='needs_recovery', quantity=100.0, target_sl=0.0, target_tp=0.0)
    await env.g.invariant_pass(full=False, dry=False)
    assert ex.positions == {} and env.db.row(row['id'])['status'] == 'closed'


async def test_open_row_with_invalid_levels_is_alarm_only(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    ex.open_position(SYM, 'long', 100, 0.5, ts_ms=env.clock.ms() - 700_000)
    env.row(user, status='open', quantity=100.0, target_sl=0.53, target_tp=0.52)   # inverted for a long
    await env.g.invariant_pass(full=False, dry=False)
    assert env.order_calls(ex) == [] and 'GUARDIAN_ROW_LEVELS_INVALID' in env.db.error_types()


async def test_agent_inactive_user_with_an_open_row_is_still_protected(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user(agent_active=False)
    row, sl_oid, tp_oid = env.bracket(user, ex)
    ex.external_cancel(sl_oid)
    await env.g.invariant_pass(full=False, dry=False)
    assert [i['type'] for i in _creates(ex)] == ['stop']


async def test_critical_alarms_notify_once_per_user_symbol_code_per_hour(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    for step in (0, 120, 3720):
        env.clock.advance(step)
        ex.external_cancel(env.db.row(row['id'])['sl_order_id'])
        await env.g.invariant_pass(full=False, dry=False)
    assert len([c for c in _creates(ex) if c['type'] == 'stop']) == 3
    assert len([n for n in notify_log if n['title'].endswith('SL_REPAIRED')]) == 2


async def test_shadow_invariant_only_logs_would_actions(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='shadow')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    ex.external_cancel(sl_oid)
    await env.g.run_cycle(2, 'shadow')
    assert env.order_calls(ex) == []
    assert 'GUARDIAN_WOULD_PLACE_SL' in env.db.error_types()
    assert env.pool.mutations() == []


async def test_default_mode_is_shadow_and_makes_zero_invariant_mutations(monkeypatch, notify_log):
    env = guardian_env(monkeypatch)                          # GUARDIAN_MODE unset
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    ex.external_cancel(sl_oid)
    mode = pg.follower_guard.guardian_mode(pg.MODES)
    assert mode == 'shadow'
    for cycle in (1, 2, 3, 4, 5):
        await env.g.run_cycle(cycle, mode)
    assert env.order_calls(ex) == [] and env.pool.mutations() == []


async def test_a_tp_rejected_every_pass_never_blocks_the_sl_repair(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    ex.external_cancel(tp_oid)
    ex.fault('create_order', ccxt.ExchangeError('tp rejected'), times=100, when=lambda i: i['type'] == 'limit')
    await env.g.invariant_pass(full=False, dry=False)          # TP fails 3x -> backoff
    ex.external_cancel(sl_oid)
    env.clock.advance(120)
    await env.g.invariant_pass(full=False, dry=False)
    assert env.db.row(row['id'])['sl_order_id'] in ex.orders   # the SL was re-placed despite the TP backoff
    assert 'GUARDIAN_TP_PLACE_FAILED' in env.db.error_types()


async def test_an_sl_removed_after_every_pass_is_replaced_3_times_then_closed(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    for _ in range(4):
        ex.external_cancel(env.db.row(row['id'])['sl_order_id'])
        await env.g.invariant_pass(full=False, dry=False)
        env.clock.advance(120)
    assert len([i for i in _creates(ex) if i['type'] == 'stop']) == 3
    assert ex.positions == {}                                  # 4th repair within 30 min -> SL_UNMAINTAINABLE close
    assert 'GUARDIAN_CHURN' in env.db.error_types() and 'GUARDIAN_CLOSE' in env.db.error_types()


async def test_a_service_sl_at_a_wrong_level_is_replaced_make_before_break(monkeypatch, notify_log):
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    ex.orders[sl_oid]['info']['stopPrice'] = 0.47              # the service SL now rests at the wrong level
    await env.g.invariant_pass(full=False, dry=False)
    kinds = [(m, info.get('type')) for m, info in ex.calls() if m in ('create_order', 'cancel_order')]
    assert kinds == [('create_order', 'stop'), ('cancel_order', None)]   # new SL live BEFORE the old one goes
    assert sl_oid not in ex.orders and env.db.row(row['id'])['sl_order_id'] in ex.orders


# ---------------- coordinator amendment CA-B7-4: churn state lives only while its row is active ----------------

async def test_churn_state_is_dropped_once_its_row_is_no_longer_active(monkeypatch, notify_log):
    """CA-B7-4: the pass-end GC (with FlatTracker.gc) drops ChurnGuard state of pairs without an active row."""
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    ex.external_cancel(sl_oid)
    await env.g.invariant_pass(full=False, dry=False)          # SL repaired: churn state for (user, symbol)
    key = (user['id'], SYM)
    assert env.g._churn[key].placed['SL']
    env.db.ack(user['id'], env.clock.utcnow())                 # an execute_trade in flight: the user yields
    await env.g.invariant_pass(full=False, dry=False)
    assert env.g._churn[key].placed['SL']                      # the row is still active: its state is kept
    env.db.row(row['id'])['status'] = 'closed'                 # position_monitor recorded the close ...
    ex.positions.clear()
    ex.orders.clear()                                          # ... and cancelled the leftover leg
    env.clock.advance(200)
    await env.g.invariant_pass(full=True, dry=False)
    assert key not in env.g._churn


async def test_a_new_row_on_the_same_symbol_starts_with_fresh_churn_state(monkeypatch, notify_log):
    """CA-B7-4: churn state is per row, so a row that replaced a closed one between two passes inherits nothing."""
    env = guardian_env(monkeypatch, mode='enforce')
    user, ex = env.user()
    row, sl_oid, tp_oid = env.bracket(user, ex)
    ex.external_cancel(sl_oid)
    await env.g.invariant_pass(full=False, dry=False)          # SL repaired for the first row
    key = (user['id'], SYM)
    old = env.g._churn[key]
    assert old.placed['SL']
    env.db.row(row['id'])['status'] = 'closed'                 # closed and replaced before the next pass
    ex.positions.clear()
    ex.orders.clear()
    env.bracket(user, ex)                                      # a new position with its own row and bracket
    await env.g.invariant_pass(full=False, dry=False)
    assert env.g._churn[key] is not old and env.g._churn[key].placed['SL'] == []
