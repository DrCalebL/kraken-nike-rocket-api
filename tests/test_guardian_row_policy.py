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
