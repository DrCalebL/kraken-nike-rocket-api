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


# ---------------- /api/broadcast-signal ----------------

class _Query:
    def filter(self, *args):
        return self

    def all(self):
        return []

    def count(self):
        return 0

    def scalar(self):
        return 0


class FakeSession:
    def __init__(self):
        self.writes = []

    def add(self, obj):
        self.writes.append(('add', obj))

    def commit(self):
        self.writes.append(('commit',))

    def refresh(self, obj):
        obj.id = 1

    def query(self, *args):
        return _Query()

    def close(self):
        pass


def _client(monkeypatch, key='mk_test'):
    monkeypatch.setattr(fe, 'MASTER_API_KEY', key)
    session = FakeSession()
    app = FastAPI()
    app.include_router(fe.router)

    def _db():
        yield session

    app.dependency_overrides[fe.get_db] = _db
    return TestClient(app), session


GOOD = {'action': 'BUY', 'symbol': 'ADA/USDT', 'entry_price': 0.5, 'stop_loss': 0.49, 'take_profit': 0.52,
        'leverage': 5, 'risk_pct': 0.0125}
HEADERS = {'X-Master-Key': 'mk_test'}


@pytest.mark.parametrize('override', [
    {'risk_pct': 0.05}, {'risk_pct': 0}, {'risk_pct': -0.01}, {'action': 'HOLD'},
    {'leverage': 0}, {'leverage': 100}, {'stop_loss': 0.53}, {'action': 'SELL'},
])
def test_each_signal_guard_rejection_is_a_422_before_any_db_write(monkeypatch, override):
    client, session = _client(monkeypatch)
    response = client.post('/api/broadcast-signal', json=dict(GOOD, **override), headers=HEADERS)
    assert response.status_code == 422
    assert session.writes == []


def test_non_finite_risk_pct_is_a_422_before_any_db_write(monkeypatch):
    client, session = _client(monkeypatch)
    body = ('{"action":"BUY","symbol":"ADA/USDT","entry_price":0.5,"stop_loss":0.49,'
            '"take_profit":0.52,"leverage":5,"risk_pct":NaN}')
    response = client.post('/api/broadcast-signal', content=body,
                           headers=dict(HEADERS, **{'content-type': 'application/json'}))
    assert response.status_code == 422
    assert session.writes == []


def test_missing_risk_pct_is_a_422_before_any_db_write(monkeypatch):
    client, session = _client(monkeypatch)
    body = {k: v for k, v in GOOD.items() if k != 'risk_pct'}
    response = client.post('/api/broadcast-signal', json=body, headers=HEADERS)
    assert response.status_code == 422
    assert session.writes == []


@pytest.mark.parametrize('risk', [0.01, 0.0125, 0.02, 0.025, 0.03])
def test_every_master_fraction_is_accepted_and_stored_verbatim(monkeypatch, risk):
    client, session = _client(monkeypatch)
    response = client.post('/api/broadcast-signal', json=dict(GOOD, risk_pct=risk), headers=HEADERS)
    assert response.status_code == 200
    stored = session.writes[0][1]
    assert stored.risk_pct == risk


# ---------------- SEC-2: no default master key, constant-time compare ----------------

def test_sec2_unset_key_disables_broadcast_and_admin_stats_with_503(monkeypatch):
    client, session = _client(monkeypatch, key=None)
    assert client.post('/api/broadcast-signal', json=GOOD, headers={'X-Master-Key': 'anything'}).status_code == 503
    assert client.get('/api/admin/stats', headers={'X-Master-Key': 'anything'}).status_code == 503
    assert session.writes == []


def test_sec2_wrong_or_missing_key_is_401_and_the_right_key_passes(monkeypatch):
    client, session = _client(monkeypatch)
    assert client.post('/api/broadcast-signal', json=GOOD, headers={'X-Master-Key': 'wrong'}).status_code == 401
    assert client.post('/api/broadcast-signal', json=GOOD).status_code == 401
    assert client.post('/api/broadcast-signal', json=GOOD, headers=HEADERS).status_code == 200
