"""
T20 kraken_follower_sl_first_and_tp_policy (spec C34/C36, I6, I12, AC10/AC11/AC14).

execute_trade on a stateful fake krakenfutures + FakePool: SL before TP, bracket sized to
the confirmed fill, TP-failure policy per GUARDIAN_MODE (enforce keeps; otherwise close
FIRST and cancel the SL only after a verified flat), in-flight token lifetime, the market
entry double-entry guard, POSITION_RECORD_FAILED and the 2-decimal risk log.
"""
import asyncio
import logging

import ccxt
import pytest

import follower_guard
import order_utils
from hosted_trading_loop import HostedTradingLoop
from tests.fakes import fast_sleep, new_user_id, notify_log  # noqa: F401  (pytest fixtures)
from tests.fakes.fake_krakenfutures import FakeKrakenFutures
from tests.fakes.fake_pool import FakePool, FollowerDB


# ---------------- order_utils: market-entry double-entry guard (C36) ----------------

async def test_market_entry_error_with_a_possible_position_is_not_resent(fast_sleep, notify_log):
    ex = FakeKrakenFutures()
    ex.raise_after_entry_fill = 1                         # the entry fills, then the call times out

    async def maybe_open():
        return True

    order = await order_utils.place_entry_order_with_retry(ex, 'PF_ADAUSD', 'buy', 100,
                                                           confirm_position_fn=maybe_open)
    assert order == {'id': 'unknown-after-exception', 'info': {}}
    assert len(ex.calls('create_market_order')) == 1


async def test_market_entry_error_on_a_flat_account_is_retried(fast_sleep, notify_log):
    ex = FakeKrakenFutures()
    ex.fault('create_market_order', ccxt.NetworkError('timeout before the order reached Kraken'))

    async def flat():
        return False

    order = await order_utils.place_entry_order_with_retry(ex, 'PF_ADAUSD', 'buy', 100, confirm_position_fn=flat)
    assert order['id'].startswith('ord-') and len(ex.calls('create_market_order')) == 2


async def test_a_failing_position_check_counts_as_a_possible_position(fast_sleep, notify_log):
    ex = FakeKrakenFutures()
    ex.fault('create_market_order', ccxt.NetworkError('timeout'))

    async def broken():
        raise ccxt.NetworkError('positions endpoint down')

    order = await order_utils.place_entry_order_with_retry(ex, 'PF_ADAUSD', 'buy', 100,
                                                           confirm_position_fn=broken)
    assert order['id'] == 'unknown-after-exception' and len(ex.calls('create_market_order')) == 1
