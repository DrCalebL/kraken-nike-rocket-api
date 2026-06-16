"""
Unit tests for the reduce-only bracket race fix in HostedTradingLoop:
  - _get_open_position / _wait_for_position  (entry-fill confirmation)
  - _position_is_open                        (emergency-close flat check)
  - _emergency_close_position                (flat-skip behaviour)

These touch no database, so they run with HostedTradingLoop(db_pool=None) and a
mocked CCXT exchange. pytest.ini sets asyncio_mode=auto, so async tests need no
decorator.

Key behaviours under test reflect real CCXT krakenfutures quirks:
  - fetch_positions() is called with NO args; positions carry the *unified*
    symbol ('ADA/USD:USD'), matched client-side against the PF_ id.
  - there is no fetch_order; the position is the only confirmation signal.
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hosted_trading_loop import HostedTradingLoop


def _loop():
    # Helpers under test never touch the DB pool.
    return HostedTradingLoop(db_pool=None)


def _exchange(positions=None, positions_seq=None, unified='ADA/USD:USD'):
    """Mock CCXT exchange. `positions_seq` gives a per-call side_effect (lists or
    Exceptions); `positions` gives a constant return value."""
    ex = MagicMock()
    ex.market.return_value = {'symbol': unified}
    if positions_seq is not None:
        ex.fetch_positions.side_effect = positions_seq
    else:
        ex.fetch_positions.return_value = positions or []
    return ex


class TestWaitForPosition:
    async def test_confirms_once_position_appears(self):
        ex = _exchange(positions_seq=[
            [],
            [],
            [{'symbol': 'ADA/USD:USD', 'contracts': 1797, 'side': 'short', 'entryPrice': 0.1754}],
        ])
        ok, contracts, price = await _loop()._wait_for_position(
            ex, 'PF_ADAUSD', 'sell', timeout=5.0, poll_interval=0.01)
        assert ok is True
        assert contracts == 1797
        assert price == 0.1754

    async def test_not_confirmed_when_never_appears(self):
        ex = _exchange(positions=[])
        ok, contracts, price = await _loop()._wait_for_position(
            ex, 'PF_ADAUSD', 'sell', timeout=0.05, poll_interval=0.01)
        assert ok is False
        assert contracts == 0.0
        assert price is None

    async def test_long_side_matches_buy_entry(self):
        ex = _exchange(positions=[
            {'symbol': 'ADA/USD:USD', 'contracts': 5, 'side': 'long', 'entryPrice': 100.0},
        ])
        ok, contracts, _ = await _loop()._wait_for_position(
            ex, 'PF_ADAUSD', 'buy', timeout=0.05, poll_interval=0.01)
        assert ok is True and contracts == 5

    async def test_ignores_other_symbol(self):
        # A BTC position must not satisfy an ADA confirmation.
        ex = _exchange(positions=[
            {'symbol': 'BTC/USD:USD', 'contracts': 9, 'side': 'short'},
        ])
        ok, contracts, _ = await _loop()._wait_for_position(
            ex, 'PF_ADAUSD', 'sell', timeout=0.05, poll_interval=0.01)
        assert ok is False and contracts == 0.0

    async def test_recovers_after_transient_api_error(self):
        ex = _exchange(positions_seq=[
            Exception("kraken 503"),
            [{'symbol': 'ADA/USD:USD', 'contracts': 2, 'side': 'short'}],
        ])
        ok, contracts, _ = await _loop()._wait_for_position(
            ex, 'PF_ADAUSD', 'sell', timeout=5.0, poll_interval=0.01)
        assert ok is True and contracts == 2


class TestPositionIsOpen:
    async def test_true_when_position_present(self):
        ex = _exchange(positions=[{'symbol': 'ADA/USD:USD', 'contracts': 3, 'side': 'short'}])
        assert await _loop()._position_is_open(ex, 'PF_ADAUSD') is True

    async def test_false_when_flat(self):
        ex = _exchange(positions=[])
        assert await _loop()._position_is_open(ex, 'PF_ADAUSD') is False

    async def test_assumes_open_on_api_error(self):
        # Fail-safe: better to attempt a close than skip a real position.
        ex = _exchange(positions_seq=[Exception("api down")])
        assert await _loop()._position_is_open(ex, 'PF_ADAUSD') is True


class TestEmergencyCloseFlatSkip:
    async def test_skips_close_when_already_flat(self, monkeypatch):
        ex = _exchange(positions=[])  # flat -> nothing to close

        async def _noop(*args, **kwargs):
            return None

        # The method always notifies/logs at the end; stub those out (no DB/network).
        monkeypatch.setattr('hosted_trading_loop.notify_bracket_incomplete', _noop)
        monkeypatch.setattr('hosted_trading_loop.log_error_to_db', _noop)

        ok = await _loop()._emergency_close_position(
            ex, 'PF_ADAUSD', 'buy', 1797, 'u@e.com', 'nk_key', 'E1', reason='test')
        assert ok is True
        ex.create_order.assert_not_called()  # never fired a reduce-only close
