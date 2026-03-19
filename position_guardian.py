"""
Nike Rocket - Position Guardian v1.0
============================================================================
Background task that detects and closes unprotected positions.

Three recovery modes:
1. Startup recovery   - On boot, checks all users for positions without TP/SL
2. DB-driven recovery - Every 2 min, checks open_positions with status='needs_recovery'
3. Exchange scan      - Every 10 min, full exchange scan for unknown unprotected positions

When an unprotected position is found, the guardian closes it at market.

Author: Nike Rocket Team
Created: March 19, 2026
"""

import asyncio
import logging
import os
import json
from datetime import datetime
from typing import Optional, Dict, List

import ccxt
from cryptography.fernet import Fernet

from order_utils import notify_admin

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('POSITION_GUARDIAN')

# Configuration
GUARDIAN_INTERVAL_SECONDS = 120       # DB-driven check every 2 minutes
EXCHANGE_SCAN_EVERY_N_CYCLES = 5     # Full exchange scan every 5th cycle (~10 min)
CLOSE_MAX_RETRIES = 5
CLOSE_INITIAL_BACKOFF = 2.0          # 2s, 4s, 8s, 16s, 32s

# Encryption
ENCRYPTION_KEY = os.getenv("CREDENTIALS_ENCRYPTION_KEY")
_cipher = Fernet(ENCRYPTION_KEY.encode()) if ENCRYPTION_KEY else None


def _decrypt_credentials(encrypted_key: str, encrypted_secret: str):
    """Decrypt Kraken API credentials"""
    if not _cipher:
        return None, None
    try:
        api_key = _cipher.decrypt(encrypted_key.encode()).decode()
        api_secret = _cipher.decrypt(encrypted_secret.encode()).decode()
        return api_key, api_secret
    except Exception:
        return None, None


async def _log_error_to_db(pool, api_key: str, error_type: str, error_message: str, context: Optional[Dict] = None):
    """Log error to error_logs table"""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO error_logs (api_key, error_type, error_message, context)
                   VALUES ($1, $2, $3, $4)""",
                api_key[:20] + "..." if api_key and len(api_key) > 20 else api_key,
                error_type,
                error_message[:500] if error_message else None,
                json.dumps(context) if context else None
            )
    except Exception as e:
        logger.error(f"Failed to log error to DB: {e}")


class PositionGuardian:
    """
    Detects and closes unprotected positions (no TP/SL orders).
    """

    def __init__(self, db_pool):
        self.db_pool = db_pool
        self.active_exchanges: Dict[str, ccxt.krakenfutures] = {}

    def _get_exchange(self, user_api_key: str, kraken_key: str, kraken_secret: str) -> Optional[ccxt.krakenfutures]:
        """Get or create cached exchange instance"""
        if user_api_key in self.active_exchanges:
            return self.active_exchanges[user_api_key]
        try:
            exchange = ccxt.krakenfutures({
                'apiKey': kraken_key,
                'secret': kraken_secret,
                'enableRateLimit': True,
            })
            self.active_exchanges[user_api_key] = exchange
            return exchange
        except Exception as e:
            logger.error(f"Failed to create exchange: {e}")
            return None

    async def _attempt_market_close(
        self,
        exchange: ccxt.krakenfutures,
        symbol: str,
        exit_side: str,
        quantity: float,
    ) -> Optional[str]:
        """
        Attempt market close with retry. Returns order ID on success, None on failure.
        """
        for attempt in range(1, CLOSE_MAX_RETRIES + 1):
            try:
                order = exchange.create_order(
                    symbol=symbol,
                    type='market',
                    side=exit_side,
                    amount=quantity,
                    params={'reduceOnly': True}
                )
                logger.info(f"   ✅ Market close successful (attempt {attempt}): {order.get('id')}")
                return order.get('id')
            except Exception as e:
                logger.warning(f"   ❌ Market close attempt {attempt}/{CLOSE_MAX_RETRIES} failed: {e}")
                if attempt < CLOSE_MAX_RETRIES:
                    backoff = CLOSE_INITIAL_BACKOFF * (2 ** (attempt - 1))
                    await asyncio.sleep(backoff)

        logger.error(f"   ❌ All {CLOSE_MAX_RETRIES} close attempts failed for {symbol}")
        return None

    # ==================== Startup Recovery ====================

    async def startup_recovery(self):
        """
        Run once on boot. For each active user, check if they have open positions
        on Kraken without matching TP/SL orders. Close any unprotected positions.
        """
        logger.info("🔍 Running startup recovery scan...")

        try:
            async with self.db_pool.acquire() as conn:
                users = await conn.fetch("""
                    SELECT id, api_key, kraken_api_key_encrypted, kraken_api_secret_encrypted
                    FROM follower_users
                    WHERE agent_active = true
                    AND credentials_set = true
                    AND kraken_api_key_encrypted IS NOT NULL
                """)

            recovered = 0
            for user in users:
                try:
                    result = await self._check_user_for_unprotected(dict(user))
                    if result:
                        recovered += result
                except Exception as e:
                    logger.warning(f"Startup scan error for user {user['api_key'][:15]}...: {e}")

            if recovered > 0:
                logger.info(f"🚨 Startup recovery: closed {recovered} unprotected position(s)")
                await notify_admin(
                    title="🛡️ Guardian Startup Recovery",
                    details={
                        "Positions Closed": recovered,
                        "Trigger": "Service restart/deploy",
                        "Action": "Unprotected positions closed at market",
                    },
                    level="warning"
                )
            else:
                logger.info("✅ Startup recovery: all positions protected")

        except Exception as e:
            logger.error(f"❌ Startup recovery failed: {e}")
            await _log_error_to_db(
                self.db_pool, "system", "GUARDIAN_STARTUP_ERROR",
                str(e), {"function": "startup_recovery"}
            )

    async def _check_user_for_unprotected(self, user: dict) -> int:
        """
        Check a single user for unprotected positions on Kraken.
        Returns number of positions closed.
        """
        kraken_key, kraken_secret = _decrypt_credentials(
            user['kraken_api_key_encrypted'],
            user['kraken_api_secret_encrypted']
        )
        if not kraken_key:
            return 0

        exchange = self._get_exchange(user['api_key'], kraken_key, kraken_secret)
        if not exchange:
            return 0

        user_short = user['api_key'][:15] + "..."

        try:
            positions = exchange.fetch_positions()
        except Exception as e:
            logger.debug(f"Could not fetch positions for {user_short}: {e}")
            return 0

        # Find positions with non-zero contracts
        open_positions = []
        for pos in positions:
            contracts = abs(float(pos.get('contracts') or pos.get('contractSize') or 0))
            if contracts > 0:
                open_positions.append(pos)

        if not open_positions:
            return 0

        # Fetch open orders
        try:
            open_orders = exchange.fetch_open_orders()
        except Exception as e:
            logger.warning(f"Could not fetch orders for {user_short}: {e}")
            return 0

        closed_count = 0
        for pos in open_positions:
            symbol = pos.get('symbol')
            contracts = abs(float(pos.get('contracts') or pos.get('contractSize') or 0))
            pos_side = pos.get('side', '').lower()  # 'long' or 'short'

            # Check if there are protective orders for this symbol
            symbol_orders = [o for o in open_orders if o.get('symbol') == symbol]

            has_tp = any(
                o.get('type') in ('limit',) and o.get('reduceOnly', False)
                for o in symbol_orders
            )
            has_sl = any(
                o.get('type') in ('stop', 'stop_market', 'stopMarket') and o.get('reduceOnly', False)
                for o in symbol_orders
            )

            # Also check: does the DB know about this position?
            async with self.db_pool.acquire() as conn:
                db_position = await conn.fetchval("""
                    SELECT COUNT(*) FROM open_positions
                    WHERE user_id = $1 AND status = 'open'
                    AND tp_order_id IS NOT NULL AND sl_order_id IS NOT NULL
                """, user['id'])

            if has_tp or has_sl or (db_position and db_position > 0):
                # Position has some protection or is tracked — skip
                continue

            # UNPROTECTED POSITION FOUND
            exit_side = 'sell' if pos_side == 'long' else 'buy'
            logger.critical(
                f"🚨 GUARDIAN: Unprotected position found for {user_short}: "
                f"{symbol} {pos_side} {contracts} contracts - closing at market"
            )

            # Convert symbol for Kraken futures if needed
            close_symbol = symbol
            order_id = await self._attempt_market_close(exchange, close_symbol, exit_side, contracts)

            if order_id:
                closed_count += 1
                logger.info(f"✅ GUARDIAN: Closed unprotected position for {user_short}")

                await notify_admin(
                    title="🛡️ Guardian: Unprotected Position Closed",
                    details={
                        "User": user_short,
                        "Symbol": symbol,
                        "Side": pos_side,
                        "Contracts": contracts,
                        "Close Order": order_id,
                        "Trigger": "Startup recovery scan",
                    },
                    level="warning"
                )
            else:
                logger.error(f"❌ GUARDIAN: Failed to close unprotected position for {user_short}")
                await _log_error_to_db(
                    self.db_pool, user['api_key'], "GUARDIAN_CLOSE_FAILED",
                    f"Failed to close unprotected {symbol} {pos_side} {contracts}",
                    {"function": "startup_recovery"}
                )

        return closed_count

    # ==================== DB-Driven Recovery ====================

    async def recover_db_positions(self):
        """
        Check for positions with status='needs_recovery' and attempt to close them.
        """
        try:
            async with self.db_pool.acquire() as conn:
                positions = await conn.fetch("""
                    SELECT
                        op.id, op.user_id, op.entry_order_id,
                        op.symbol, op.kraken_symbol, op.side, op.quantity,
                        op.entry_fill_price, op.opened_at,
                        u.api_key, u.email,
                        u.kraken_api_key_encrypted, u.kraken_api_secret_encrypted
                    FROM open_positions op
                    JOIN follower_users u ON op.user_id = u.id
                    WHERE op.status = 'needs_recovery'
                """)

            if not positions:
                return

            logger.info(f"🔧 Found {len(positions)} position(s) needing recovery")

            for pos in positions:
                pos = dict(pos)
                await self._recover_single_position(pos)

        except Exception as e:
            logger.error(f"❌ DB recovery scan failed: {e}")
            await _log_error_to_db(
                self.db_pool, "system", "GUARDIAN_DB_RECOVERY_ERROR",
                str(e), {"function": "recover_db_positions"}
            )

    async def _recover_single_position(self, pos: dict):
        """Attempt to close a single needs_recovery position."""
        user_short = pos['api_key'][:15] + "..."
        kraken_symbol = pos['kraken_symbol'] or pos['symbol']
        side = (pos.get('side') or '').upper()
        quantity = float(pos.get('quantity') or 0)

        if not quantity:
            logger.warning(f"⚠️ GUARDIAN: Position {pos['id']} has zero quantity, marking closed")
            await self._mark_position_status(pos['id'], 'closed_manual')
            return

        kraken_key, kraken_secret = _decrypt_credentials(
            pos['kraken_api_key_encrypted'],
            pos['kraken_api_secret_encrypted']
        )
        if not kraken_key:
            logger.error(f"❌ GUARDIAN: Cannot decrypt credentials for {user_short}")
            return

        exchange = self._get_exchange(pos['api_key'], kraken_key, kraken_secret)
        if not exchange:
            return

        # Check if position still exists on Kraken
        try:
            kraken_positions = exchange.fetch_positions()
            has_position = False
            for kp in kraken_positions:
                contracts = abs(float(kp.get('contracts') or kp.get('contractSize') or 0))
                if contracts > 0:
                    has_position = True
                    break

            if not has_position:
                # Position already closed (user closed manually or liquidated)
                logger.info(f"✅ GUARDIAN: Position {pos['id']} no longer exists on Kraken - marking closed")
                await self._mark_position_status(pos['id'], 'closed_manual')
                return
        except Exception as e:
            logger.warning(f"⚠️ GUARDIAN: Could not check Kraken positions for {user_short}: {e}")
            # Don't close blindly — skip this cycle and try again next time
            return

        # Position still open — close it
        exit_side = 'sell' if side in ('BUY', 'LONG') else 'buy'

        logger.info(f"🔧 GUARDIAN: Closing needs_recovery position {pos['id']} for {user_short}: {kraken_symbol} {side} {quantity}")

        order_id = await self._attempt_market_close(exchange, kraken_symbol, exit_side, quantity)

        if order_id:
            await self._mark_position_status(pos['id'], 'closed')
            logger.info(f"✅ GUARDIAN: Position {pos['id']} closed successfully: {order_id}")

            await notify_admin(
                title="🛡️ Guardian: Recovery Close Successful",
                details={
                    "User": pos.get('email', user_short),
                    "Position ID": pos['id'],
                    "Symbol": kraken_symbol,
                    "Side": side,
                    "Quantity": quantity,
                    "Close Order": order_id,
                    "Original Entry": pos.get('entry_order_id', 'unknown'),
                    "Time Unprotected": str(datetime.utcnow() - pos['opened_at']) if pos.get('opened_at') else 'unknown',
                },
                level="warning"
            )
        else:
            logger.error(f"❌ GUARDIAN: Failed to close position {pos['id']} - will retry next cycle")
            await _log_error_to_db(
                self.db_pool, pos['api_key'], "GUARDIAN_RECOVERY_CLOSE_FAILED",
                f"Failed to close position {pos['id']}: {kraken_symbol} {side} {quantity}",
                {"position_id": pos['id'], "function": "recover_single_position"}
            )

    async def _mark_position_status(self, position_id: int, status: str):
        """Update position status in DB."""
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE open_positions SET status = $1, last_fill_at = NOW() WHERE id = $2",
                    status, position_id
                )
        except Exception as e:
            logger.error(f"Failed to update position {position_id} status: {e}")

    # ==================== Exchange Scan (Catch-All) ====================

    async def scan_exchange_for_unprotected(self):
        """
        Full exchange scan — checks ALL active users for positions without protective orders.
        This is the most expensive check (API calls per user) and runs every ~10 minutes.
        """
        logger.info("🔍 Running full exchange scan for unprotected positions...")

        try:
            async with self.db_pool.acquire() as conn:
                users = await conn.fetch("""
                    SELECT id, api_key, kraken_api_key_encrypted, kraken_api_secret_encrypted
                    FROM follower_users
                    WHERE agent_active = true
                    AND credentials_set = true
                    AND kraken_api_key_encrypted IS NOT NULL
                """)

            issues_found = 0
            for user in users:
                try:
                    result = await self._check_user_for_unprotected(dict(user))
                    if result:
                        issues_found += result
                except Exception as e:
                    logger.debug(f"Exchange scan error for user: {e}")

            if issues_found > 0:
                logger.warning(f"🚨 Exchange scan: closed {issues_found} unprotected position(s)")
            else:
                logger.debug("✅ Exchange scan: all positions protected")

        except Exception as e:
            logger.error(f"❌ Exchange scan failed: {e}")

    # ==================== Main Loop ====================

    async def run(self):
        """Main guardian loop."""
        logger.info("=" * 60)
        logger.info("🛡️ POSITION GUARDIAN v1.0 STARTED")
        logger.info("=" * 60)
        logger.info(f"🔄 DB check interval: {GUARDIAN_INTERVAL_SECONDS}s")
        logger.info(f"🔍 Exchange scan: every {EXCHANGE_SCAN_EVERY_N_CYCLES * GUARDIAN_INTERVAL_SECONDS}s")
        logger.info("=" * 60)

        # Run startup recovery once
        await self.startup_recovery()

        cycle = 0
        while True:
            try:
                cycle += 1

                # DB-driven recovery (every cycle)
                await self.recover_db_positions()

                # Exchange scan (every Nth cycle)
                if cycle % EXCHANGE_SCAN_EVERY_N_CYCLES == 0:
                    await self.scan_exchange_for_unprotected()

                await asyncio.sleep(GUARDIAN_INTERVAL_SECONDS)

            except asyncio.CancelledError:
                logger.info("🛑 Position guardian cancelled")
                break

            except Exception as e:
                logger.error(f"❌ Error in guardian loop: {e}")
                import traceback
                traceback.print_exc()
                await _log_error_to_db(
                    self.db_pool, "system", "GUARDIAN_LOOP_ERROR",
                    str(e), {"cycle": cycle, "traceback": traceback.format_exc()[:500]}
                )
                await asyncio.sleep(10)


async def start_position_guardian(db_pool):
    """Start the position guardian (call from main.py startup)."""
    await asyncio.sleep(45)  # Start after position monitor (40s)

    guardian = PositionGuardian(db_pool)
    await guardian.run()
