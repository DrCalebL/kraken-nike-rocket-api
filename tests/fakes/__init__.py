"""
Shared offline helpers for the Wave 1 Kraken follower tests: no network, no database.

Import fixtures into a test module with `from tests.fakes import fast_sleep, notify_log`.
"""
import asyncio
import itertools
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_real_sleep = asyncio.sleep
_user_ids = itertools.count(501)


def new_user_id():
    """Unique user ids per test: the in-flight registry is process-global."""
    return next(_user_ids)


class FakeClock:
    """Epoch clock for the guardian (time()) and the fake DB (utcnow(), naive UTC)."""

    def __init__(self, start=1_790_000_000.0):
        self.t = float(start)

    def time(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds

    def utcnow(self):
        return datetime.fromtimestamp(self.t, timezone.utc).replace(tzinfo=None)

    def ms(self):
        return int(self.t * 1000)


class SleepRecorder:
    """Injectable guardian sleep: records the delay, advances the fake clock, runs one-shot hooks."""

    def __init__(self, clock=None):
        self.clock = clock
        self.delays = []
        self.hooks = []

    async def __call__(self, delay, result=None):
        self.delays.append(delay)
        if self.clock is not None:
            self.clock.advance(delay)
        hooks, self.hooks = self.hooks, []
        for hook in hooks:
            hook()
        await _real_sleep(0)
        return result


@pytest.fixture
def fast_sleep(monkeypatch):
    """asyncio.sleep yields once instead of waiting (retry backoffs, fill polls, flat checks)."""
    async def _fast(delay=0, result=None):
        await _real_sleep(0)
        return result
    monkeypatch.setattr(asyncio, 'sleep', _fast)


@pytest.fixture
def notify_log(monkeypatch):
    """Capture notify_admin from every module that uses it (the real one would e-mail the admin)."""
    sent = []

    async def _notify(title, details, level='warning'):
        sent.append({'title': title, 'details': details, 'level': level})
        return True

    import hosted_trading_loop
    import order_utils
    import position_guardian
    for module in (order_utils, hosted_trading_loop, position_guardian):
        monkeypatch.setattr(module, 'notify_admin', _notify, raising=False)
    return sent


class GuardianEnv:
    """PositionGuardian wired to a FakePool/FollowerDB, a FakeVenue and a FakeClock."""

    def __init__(self, guardian, db, pool, venue, clock, sleep):
        self.g, self.db, self.pool, self.venue, self.clock, self.sleep = guardian, db, pool, venue, clock, sleep

    def user(self, **fields):
        """A follower with its own fake Kraken account, pre-attached to the guardian."""
        user = self.db.add_user(new_user_id(), **fields)
        ex = self.venue.account(user['api_key'], now_ms=self.clock.ms)
        self.g.active_exchanges[user['api_key']] = ex
        return user, ex

    def row(self, user, **fields):
        fields.setdefault('opened_at', self.clock.utcnow() - timedelta(minutes=10))
        return self.db.add_row(user_id=user['id'], **fields)

    def bracket(self, user, ex, qty=100.0, entry=0.5, sl=0.49, tp=0.52, status='open', side='long',
                opened_ago_s=600):
        """A consistent row + live position + service SL/TP at the recorded levels."""
        sl_oid = ex.add_order('ADA/USD:USD', 'stop', 'sell' if side == 'long' else 'buy', qty, sl)
        tp_oid = ex.add_order('ADA/USD:USD', 'lmt', 'sell' if side == 'long' else 'buy', qty, tp)
        ex.open_position('ADA/USD:USD', side, qty, entry, ts_ms=self.clock.ms() - (opened_ago_s + 5) * 1000)
        row = self.row(user, side='BUY' if side == 'long' else 'SELL', quantity=qty, entry_fill_price=entry,
                       target_sl=sl, target_tp=tp, sl_order_id=sl_oid, tp_order_id=tp_oid, status=status,
                       opened_at=self.clock.utcnow() - timedelta(seconds=opened_ago_s))
        return row, sl_oid, tp_oid

    def order_calls(self, ex):
        return [c for c in ex.calls() if c[0] in ('create_order', 'cancel_order', 'create_market_order',
                                                  'create_limit_order')]


def guardian_env(monkeypatch, mode=None, latency_s=0.0, max_concurrency=None):
    import position_guardian
    from tests.fakes.fake_krakenfutures import FakeVenue
    from tests.fakes.fake_pool import FakePool, FollowerDB
    if mode is None:
        monkeypatch.delenv('GUARDIAN_MODE', raising=False)
    else:
        monkeypatch.setenv('GUARDIAN_MODE', mode)
    clock = FakeClock()
    pool = FakePool()
    db = FollowerDB(pool, now=clock.utcnow)
    sleep = SleepRecorder(clock)
    guardian = position_guardian.PositionGuardian(pool, clock=clock.time, sleep=sleep,
                                                  max_concurrency=max_concurrency)
    return GuardianEnv(guardian, db, pool, FakeVenue(latency_s=latency_s), clock, sleep)
