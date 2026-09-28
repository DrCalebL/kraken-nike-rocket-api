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
