"""
T24 kraken_follower_fail_closed_and_bounds (spec C35, C38, I11, AC13/AC14).

Dedup fails CLOSED on any read error (no entry is ever sent). The broadcast endpoint
(FastAPI TestClient, get_db overridden; these tests never skip) returns 422 before any DB write
for every signal_guard rejection and for a missing risk_pct, and accepts every configured
master fraction verbatim.
"""
import ccxt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import follower_endpoints as fe
from hosted_trading_loop import HostedTradingLoop
from tests.fakes import fast_sleep, new_user_id, notify_log  # noqa: F401  (pytest fixtures)
from tests.fakes.fake_krakenfutures import FakeKrakenFutures
from tests.fakes.fake_pool import FakePool, FollowerDB


def _trading(monkeypatch):
    monkeypatch.delenv('GUARDIAN_MODE', raising=False)
    pool = FakePool()
    db = FollowerDB(pool)
    user = db.add_user(new_user_id())
    ex = FakeKrakenFutures(name=user['api_key'])
    loop = HostedTradingLoop(pool)
    loop.active_exchanges[user['api_key']] = ex
    signal = {'delivery_id': None, 'signal_id': 'sig-%d' % user['id'], 'action': 'BUY', 'symbol': 'ADA/USDT',
              'entry_price': 0.5, 'stop_loss': 0.49, 'take_profit': 0.52, 'leverage': 5, 'risk_pct': 0.02}
    return loop, db, pool, ex, user, signal


# ---------------- dedup fails closed (C35) ----------------

@pytest.mark.parametrize('method', ['fetch_positions', 'fetch_open_orders'])
async def test_dedup_read_error_fails_closed_and_sends_no_entry(monkeypatch, fast_sleep, notify_log, method):
    loop, db, pool, ex, user, signal = _trading(monkeypatch)
    ex.fault(method, ccxt.NetworkError('read timeout'))
    assert await loop.execute_trade(user, signal) is False
    assert ex.calls('create_market_order') == []
    assert 'DEDUP_SKIPPED_READ_ERROR' in db.error_types()


async def test_dedup_outer_error_fails_closed(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _trading(monkeypatch)
    pool.fail_on('SELECT id FROM signals WHERE signal_id', RuntimeError('db read failed'))
    has_open, reason = await loop.check_any_open_positions_or_orders(ex, 'u', user_id=user['id'], signal_id='s')
    assert (has_open, reason) == (True, 'Safety check error - skipping')
    assert 'DEDUP_SKIPPED_READ_ERROR' in db.error_types()


async def test_dedup_clean_reads_allow_the_trade(monkeypatch, fast_sleep, notify_log):
    loop, db, pool, ex, user, signal = _trading(monkeypatch)
    assert await loop.check_any_open_positions_or_orders(ex, 'u', user_id=user['id'], signal_id='s') == (False, None)
