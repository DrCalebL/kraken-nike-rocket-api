"""
Nike Rocket - Hosted Trading Loop (30-Day Billing + Error Logging + Order Retry)
==================================================================================
Background task that polls for signals and executes trades for ALL active users.
Runs on Railway as part of main.py startup.

Features:
- Polls for latest signal ONCE per cycle (batched, not per-user)
- Checks which users haven't acknowledged the signal
- Decrypts user Kraken credentials from database
- Calculates position size (2-3% risk formula)
- Executes 3-order bracket (Entry + TP + SL) WITH RETRY LOGIC
- ORDER RETRY: 3 attempts with exponential backoff (1s -> 2s -> 4s)
- DISCORD NOTIFICATIONS: Admin notified on order failures
- Logs all activity for admin dashboard
- ENFORCES 30-DAY BILLING: Skips users with overdue invoices
- ERROR LOGGING: All errors logged to error_logs table for admin visibility

OPTIMIZED:
- Batched signal fetching (1 query instead of N queries)
- Cached exchange instances
- Scales to 100+ users without hitting rate limits

SCALED FOR 5000+ USERS:
- Random shuffle for fair execution order (no user always first/last)
- Parallel batch execution (25 users per batch)
- 50ms stagger delay between batches (under Kraken limits)
- 5000 users executes in ~10-15 seconds per signal

FAILSAFE:
- Entry order fails → Trade aborted, admin notified
- TP/SL fails → Position recorded, admin notified IMMEDIATELY
- All failures logged to error_logs table

Author: Nike Rocket Team
Updated: December 1, 2025 - Scaled for 5000+ users
"""

import asyncio
import ccxt
import logging
import math
import json
import random
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import os

# Import order utilities with retry logic
from order_utils import (
    place_entry_order_with_retry,
    place_tp_order_with_retry,
    place_sl_order_with_retry,
    notify_entry_failed,
    notify_bracket_incomplete,
    notify_signal_invalid,
    notify_signal_invalid_values,
    notify_critical_error
)

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('HOSTED_TRADING')


async def log_error_to_db(pool, api_key: str, error_type: str, error_message: str, context: Optional[Dict] = None):
    """Log error to error_logs table for admin dashboard visibility"""
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

# ==================== CONFIGURATION ====================

# Risk settings (default if not specified in signal)
DEFAULT_RISK_PERCENTAGE = 0.02  # 2% default, but signal can override

# Polling settings
POLL_INTERVAL_SECONDS = 10
ACTIVE_MINUTES_PER_HOUR = 3  # Poll first 3 minutes of each hour

# Symbol mapping: API format → Kraken Futures format
SYMBOL_MAP = {
    'BTC/USDT': 'PF_XBTUSD',
    'ETH/USDT': 'PF_ETHUSD',
    'ADA/USDT': 'PF_ADAUSD',
    'SOL/USDT': 'PF_SOLUSD',
    'DOT/USDT': 'PF_DOTUSD',
    'LINK/USDT': 'PF_LINKUSD',
    'AVAX/USDT': 'PF_AVAXUSD',
    'MATIC/USDT': 'PF_MATICUSD',
    'XRP/USDT': 'PF_XRPUSD',
    'DOGE/USDT': 'PF_DOGEUSD',
    'LTC/USDT': 'PF_LTCUSD',
}


def convert_symbol_to_kraken(api_symbol: str) -> str:
    """Convert API symbol format to Kraken Futures format"""
    # Direct mapping
    if api_symbol in SYMBOL_MAP:
        return SYMBOL_MAP[api_symbol]
    
    # Try dynamic conversion: BTC/USDT → PF_XBTUSD
    if '/' in api_symbol:
        base = api_symbol.split('/')[0]
        # Special case for BTC
        if base == 'BTC':
            base = 'XBT'
        return f"PF_{base}USD"
    
    return api_symbol


def should_poll_now() -> bool:
    """Check if we're in active polling window (first 3 minutes of hour)"""
    now = datetime.now()
    return now.minute < ACTIVE_MINUTES_PER_HOUR


def get_sleep_until_next_window() -> int:
    """Calculate seconds until next polling window"""
    now = datetime.now()
    
    if now.minute < ACTIVE_MINUTES_PER_HOUR:
        return 0  # Already in window
    
    # Calculate next hour start
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    sleep_seconds = (next_hour - now).total_seconds()
    
    # Minimum sleep of 5 seconds to prevent tight loop at edge cases
    return max(5, int(sleep_seconds))


class HostedTradingLoop:
    """
    Main hosted trading loop that executes for all users
    """
    
    def __init__(self, db_pool):
        """
        Initialize with database pool
        
        Args:
            db_pool: asyncpg database pool from main.py
        """
        self.db_pool = db_pool
        self.active_exchanges = {}  # Cache of user exchanges: {api_key: exchange}
        self.logger = logging.getLogger('HOSTED_TRADING')
    
    async def get_active_users(self) -> List[Dict]:
        """Get all users with active agents and valid credentials"""
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT 
                    id,
                    api_key,
                    email,
                    kraken_api_key_encrypted,
                    kraken_api_secret_encrypted,
                    credentials_set,
                    agent_active
                FROM follower_users
                WHERE agent_active = true
                AND credentials_set = true
                AND access_granted = true
                AND (
                    pending_invoice_id IS NULL 
                    OR invoice_due_date IS NULL 
                    OR invoice_due_date > CURRENT_TIMESTAMP
                )
            """)
            
            return [dict(row) for row in rows]
    
    async def get_pending_signals_batched(self) -> List[Dict]:
        """
        OPTIMIZED: Get all pending signals with user info in ONE query.
        
        Instead of N queries (one per user), this fetches everything at once.
        Returns list of {user_info + signal_info} dicts.
        """
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT 
                    u.id as user_id,
                    u.api_key,
                    u.email,
                    u.kraken_api_key_encrypted,
                    u.kraken_api_secret_encrypted,
                    sd.id as delivery_id,
                    s.signal_id,
                    s.action,
                    s.symbol,
                    s.entry_price,
                    s.stop_loss,
                    s.take_profit,
                    s.leverage,
                    COALESCE(s.risk_pct, 0.02) as risk_pct,
                    s.created_at as signal_created_at
                FROM follower_users u
                JOIN signal_deliveries sd ON sd.user_id = u.id
                JOIN signals s ON sd.signal_id = s.id
                WHERE u.agent_active = true
                  AND u.credentials_set = true
                  AND u.access_granted = true
                  AND (
                      u.pending_invoice_id IS NULL 
                      OR u.invoice_due_date IS NULL 
                      OR u.invoice_due_date > CURRENT_TIMESTAMP
                  )
                  AND sd.acknowledged = false
                  AND s.created_at > NOW() - INTERVAL '15 minutes'
                ORDER BY s.created_at DESC
            """)
            
            return [dict(row) for row in rows]
    
    def decrypt_credentials(self, encrypted_key: str, encrypted_secret: str) -> tuple:
        """Decrypt Kraken credentials"""
        from cryptography.fernet import Fernet
        
        encryption_key = os.getenv("CREDENTIALS_ENCRYPTION_KEY")
        if not encryption_key:
            raise Exception("CREDENTIALS_ENCRYPTION_KEY not set")
        
        cipher = Fernet(encryption_key.encode())
        
        api_key = cipher.decrypt(encrypted_key.encode()).decode()
        api_secret = cipher.decrypt(encrypted_secret.encode()).decode()
        
        return api_key, api_secret
    
    def get_or_create_exchange(self, user: Dict) -> ccxt.krakenfutures:
        """Get or create CCXT exchange instance for user"""
        api_key = user['api_key']
        
        # Return cached if exists
        if api_key in self.active_exchanges:
            return self.active_exchanges[api_key]
        
        # Decrypt credentials
        kraken_key, kraken_secret = self.decrypt_credentials(
            user['kraken_api_key_encrypted'],
            user['kraken_api_secret_encrypted']
        )
        
        # Create exchange
        exchange = ccxt.krakenfutures({
            'apiKey': kraken_key,
            'secret': kraken_secret,
            'enableRateLimit': True,
        })
        
        # Load markets
        exchange.load_markets()
        
        # Cache it
        self.active_exchanges[api_key] = exchange
        
        return exchange
    
    async def get_latest_signal(self, user_api_key: str) -> Optional[Dict]:
        """Get latest unacknowledged signal for user"""
        async with self.db_pool.acquire() as conn:
            # Get user ID
            user_row = await conn.fetchrow(
                "SELECT id FROM follower_users WHERE api_key = $1",
                user_api_key
            )
            
            if not user_row:
                return None
            
            user_id = user_row['id']
            
            # Get latest unacknowledged signal
            row = await conn.fetchrow("""
                SELECT 
                    sd.id as delivery_id,
                    s.signal_id,
                    s.action,
                    s.symbol,
                    s.entry_price,
                    s.stop_loss,
                    s.take_profit,
                    s.leverage,
                    COALESCE(s.risk_pct, 0.02) as risk_pct,
                    s.created_at
                FROM signal_deliveries sd
                JOIN signals s ON sd.signal_id = s.id
                WHERE sd.user_id = $1
                AND sd.acknowledged = false
                ORDER BY s.created_at DESC
                LIMIT 1
            """, user_id)
            
            if not row:
                return None
            
            # Check if signal is too old (> 15 minutes)
            signal_age = (datetime.utcnow() - row['created_at']).total_seconds()
            if signal_age > 900:  # 15 minutes
                self.logger.info(f"   Signal expired ({signal_age/60:.1f} min old)")
                # Mark as acknowledged (expired)
                await conn.execute(
                    "UPDATE signal_deliveries SET acknowledged = true WHERE id = $1",
                    row['delivery_id']
                )
                return None
            
            return dict(row)
    
    async def acknowledge_signal(self, delivery_id: int):
        """Mark signal as acknowledged after execution"""
        async with self.db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE signal_deliveries 
                SET acknowledged = true, 
                    acknowledged_at = NOW(),
                    executed = true,
                    executed_at = NOW()
                WHERE id = $1
            """, delivery_id)
    
    async def get_user_equity(self, exchange: ccxt.krakenfutures) -> float:
        """Get user's Kraken Futures equity"""
        try:
            balance = exchange.fetch_balance()
            
            # Try different balance keys
            for key in ['USD', 'USDT', 'total']:
                if key in balance and isinstance(balance[key], (int, float)):
                    return float(balance[key])
                if key in balance and isinstance(balance[key], dict):
                    if 'total' in balance[key]:
                        return float(balance[key]['total'])
            
            # Fallback to info
            if 'info' in balance:
                info = balance['info']
                if isinstance(info, dict):
                    for field in ['equity', 'balance', 'portfolioValue', 'pv']:
                        if field in info:
                            return float(info[field])
            
            self.logger.warning("Could not determine equity from balance")
            return 0.0
            
        except Exception as e:
            self.logger.error(f"Error fetching balance: {e}")
            await log_error_to_db(
                self.db_pool, "exchange", "EQUITY_FETCH_ERROR",
                str(e), {"function": "get_user_equity"}
            )
            return 0.0
    
    async def check_existing_position(self, exchange: ccxt.krakenfutures, symbol: str) -> bool:
        """Check if user already has position in this symbol"""
        try:
            positions = exchange.fetch_positions([symbol])
            
            for pos in positions:
                if pos.get('contracts', 0) != 0 or pos.get('contractSize', 0) != 0:
                    size = pos.get('contracts') or pos.get('contractSize') or 0
                    if abs(float(size)) > 0:
                        return True
            
            return False
            
        except Exception as e:
            self.logger.warning(f"Error checking positions: {e}")
            return False
    
    async def check_any_open_positions_or_orders(self, exchange: ccxt.krakenfutures, user_short: str) -> tuple:
        """
        SAFETY CHECK: Verify user has NO open positions or orders on ANY symbol
        
        Returns:
            tuple: (has_open, reason_string)
            - (True, "reason") if there ARE open positions/orders (should SKIP trade)
            - (False, None) if account is clear (safe to trade)
        
        This prevents:
        1. Double positions on same symbol (entry filled, exit didn't, new signal comes)
        2. Multiple symbols open (risk management assumes single position)
        3. Orphaned TP/SL orders affecting new trades
        """
        try:
            # ===== CHECK 1: Any open positions (any symbol) =====
            try:
                positions = exchange.fetch_positions()
                
                for pos in positions:
                    size = pos.get('contracts') or pos.get('contractSize') or 0
                    if abs(float(size)) > 0:
                        symbol = pos.get('symbol', 'Unknown')
                        side = pos.get('side', 'Unknown')
                        self.logger.warning(
                            f"   🚫 {user_short}: Found open position - {symbol} {side} ({size} contracts)"
                        )
                        return (True, f"Open position exists: {symbol} {side}")
            except Exception as e:
                self.logger.warning(f"   ⚠️ {user_short}: Error fetching positions: {e}")
                # Continue to check orders even if positions check fails
            
            # ===== CHECK 2: Any open orders (TP/SL leftovers) =====
            try:
                open_orders = exchange.fetch_open_orders()
                
                if open_orders and len(open_orders) > 0:
                    # Group by symbol for cleaner logging
                    symbols_with_orders = set()
                    for order in open_orders:
                        symbols_with_orders.add(order.get('symbol', 'Unknown'))
                    
                    symbols_list = ', '.join(symbols_with_orders)
                    self.logger.warning(
                        f"   🚫 {user_short}: Found {len(open_orders)} open order(s) on: {symbols_list}"
                    )
                    return (True, f"Open orders exist: {len(open_orders)} orders on {symbols_list}")
            except Exception as e:
                self.logger.warning(f"   ⚠️ {user_short}: Error fetching open orders: {e}")
                # If we can't check orders, be cautious and allow trade
                # (positions check is more critical)
            
            # ===== ALL CLEAR =====
            return (False, None)
            
        except Exception as e:
            self.logger.error(f"   ❌ {user_short}: Error in safety check: {e}")
            await log_error_to_db(
                self.db_pool, user_short, "SAFETY_CHECK_ERROR",
                str(e), {"function": "check_any_open_positions_or_orders"}
            )
            # On error, be cautious - allow trade but log warning
            return (False, None)
    
    async def execute_trade(self, user: Dict, signal: Dict) -> bool:
        """
        Execute trade for a user
        
        Position sizing formula (2% risk):
        - risk_amount = equity * 0.02
        - risk_per_unit = |entry - stop_loss|
        - position_size = (risk_amount / risk_per_unit) * leverage
        
        Then places 3-order bracket:
        1. Market entry order
        2. Limit take-profit order (reduce-only)
        3. Stop-loss order (reduce-only)
        """
        user_api_key = user['api_key']
        user_short = user_api_key[:15] + "..."
        
        try:
            # Get exchange
            exchange = self.get_or_create_exchange(user)
            
            # Convert symbol
            api_symbol = signal['symbol']
            kraken_symbol = convert_symbol_to_kraken(api_symbol)
            
            self.logger.info(f"   📊 {user_short}: Executing {signal['action']} {api_symbol}")
            
            # ===== SAFETY CHECK: No open positions or orders on ANY symbol =====
            # This prevents:
            # - Double trades (position exists, new signal comes)
            # - Multi-symbol exposure (risk management assumes single position)
            # - Orphaned orders interfering with new trades
            has_open, reason = await self.check_any_open_positions_or_orders(exchange, user_short)
            if has_open:
                self.logger.warning(f"   ⏭️ {user_short}: SKIPPING TRADE - {reason}")
                self.logger.warning(f"   ℹ️ {user_short}: Clean up positions/orders on Kraken before next signal")
                return False
            
            # Get equity
            equity = await self.get_user_equity(exchange)
            if equity <= 0:
                self.logger.error(f"   ❌ {user_short}: No equity found")
                return False
            
            # ==================== VALIDATE SL/TP PRESENCE ====================
            # CRITICAL: Do not take trades without both SL and TP
            if not signal.get('stop_loss') or not signal.get('take_profit'):
                missing = []
                if not signal.get('stop_loss'):
                    missing.append('stop_loss')
                if not signal.get('take_profit'):
                    missing.append('take_profit')
                
                self.logger.error(f"   ❌ {user_short}: Signal missing {', '.join(missing)} - SKIPPING TRADE")
                
                # Notify admin via Discord
                await notify_signal_invalid(
                    signal_id=signal.get('signal_id'),
                    symbol=signal.get('symbol'),
                    action=signal.get('action'),
                    missing_fields=missing,
                    reason=f"Signal missing required fields: {', '.join(missing)}"
                )
                
                await log_error_to_db(
                    self.db_pool,
                    user.get('api_key', 'unknown'),
                    "SIGNAL_MISSING_SL_TP",
                    f"Signal missing: {', '.join(missing)}",
                    {"signal_id": signal.get('signal_id'), "symbol": signal.get('symbol')}
                )
                return False
            
            # Extract signal data
            action = signal['action']  # BUY or SELL
            entry_price = float(signal['entry_price'])
            stop_loss = float(signal['stop_loss'])
            take_profit = float(signal['take_profit'])
            leverage = float(signal.get('leverage', 5.0))
            
            # Validate SL/TP are non-zero
            if stop_loss <= 0 or take_profit <= 0:
                self.logger.error(f"   ❌ {user_short}: Invalid SL ({stop_loss}) or TP ({take_profit}) - SKIPPING TRADE")
                
                # Notify admin via Discord
                await notify_signal_invalid_values(
                    signal_id=signal.get('signal_id'),
                    symbol=signal.get('symbol'),
                    action=action,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    reason=f"SL ({stop_loss}) or TP ({take_profit}) is zero or negative"
                )
                
                await log_error_to_db(
                    self.db_pool,
                    user.get('api_key', 'unknown'),
                    "SIGNAL_INVALID_SL_TP",
                    f"Invalid SL ({stop_loss}) or TP ({take_profit})",
                    {"signal_id": signal.get('signal_id'), "symbol": signal.get('symbol')}
                )
                return False
            
            # Get risk percentage from signal (2% aggressive, 3% conservative)
            # Falls back to default if not specified
            risk_pct = float(signal.get('risk_pct', DEFAULT_RISK_PERCENTAGE))
            
            # ==================== POSITION SIZING ====================
            # 2% (or 3%) risk formula - leverage does NOT multiply position size
            # Leverage only affects margin required, not the actual risk
            risk_amount = equity * risk_pct
            risk_per_unit = abs(entry_price - stop_loss)
            
            if risk_per_unit <= 0:
                self.logger.error(f"   ❌ {user_short}: Invalid SL distance")
                
                # Notify admin via Discord
                await notify_signal_invalid_values(
                    signal_id=signal.get('signal_id'),
                    symbol=signal.get('symbol'),
                    action=action,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    reason=f"SL distance is zero (entry={entry_price}, SL={stop_loss})"
                )
                
                return False
            
            # Position size = risk amount / risk per unit (NO leverage multiplication)
            position_size = risk_amount / risk_per_unit
            
            # Round to exchange precision
            quantity = float(exchange.amount_to_precision(kraken_symbol, position_size))
            
            # Calculate position value for margin check
            position_value = quantity * entry_price
            
            # ✅ KRAKEN-SPECIFIC: Verify leverage meets margin requirements
            # Kraken requires: (position_value / leverage) × 1.5 ≤ available_balance
            # Therefore: leverage ≥ (position_value × 1.5) / available_balance
            #
            # The signal includes leverage calculated by the main algo for its account.
            # This code recalculates for THIS account's available margin as a safety net.
            # Use the HIGHER of:
            #   1. Signal's leverage (from main algo)
            #   2. Calculated min leverage (for this account's available margin)
            #
            # Why? If user has manual trades open or other margin usage, they might need
            # higher leverage than the signal indicates. This prevents "insufficient funds" errors.
            available_balance = equity * 0.95  # Use 95% for safety
            min_leverage_for_margin = (position_value * 1.5) / available_balance
            
            # Adjust leverage if needed (take the maximum)
            original_leverage = leverage
            if min_leverage_for_margin > leverage:
                leverage = int(math.ceil(min_leverage_for_margin))
                # Cap at reasonable max (50x)
                leverage = min(leverage, 50)
                
                if leverage > original_leverage:
                    self.logger.warning(
                        f"   ⚠️ Leverage increased to fit Kraken margin requirements: "
                        f"{original_leverage}x → {leverage}x (manual trades or margin usage detected)"
                    )
            
            self.logger.info(f"   💰 Equity: ${equity:,.2f}")
            self.logger.info(f"   🎯 Risk: ${risk_amount:,.2f} ({risk_pct*100:.0f}%)")
            self.logger.info(f"   📐 Position: {quantity} units @ {leverage}x leverage")
            self.logger.info(f"   💵 Position value: ${position_value:,.2f}, Margin needed: ${position_value/leverage:,.2f}")
            
            # ==================== SET LEVERAGE ====================
            try:
                exchange.set_leverage(int(leverage), kraken_symbol)
                self.logger.info(f"   ⚙️ Leverage set to {int(leverage)}x")
            except Exception as e:
                self.logger.warning(f"   ⚠️ Could not set leverage: {e}")
            
            # ==================== EXECUTE 3-ORDER BRACKET WITH RETRY ====================
            
            side = action.lower()  # 'buy' or 'sell'
            exit_side = 'sell' if side == 'buy' else 'buy'
            user_email = user.get('email', 'unknown')
            user_api_key = user.get('api_key', 'unknown')
            
            # 1. Entry order (market) - WITH RETRY
            self.logger.info(f"   📝 Placing entry order...")
            entry_order = await place_entry_order_with_retry(
                exchange=exchange,
                symbol=kraken_symbol,
                side=side,
                quantity=quantity,
                user_email=user_email,
                user_api_key=user_api_key
            )
            
            if not entry_order:
                self.logger.error(f"   ❌ Entry order FAILED after all retries - ABORTING TRADE")
                await notify_entry_failed(
                    user_email=user_email,
                    user_api_key=user_api_key,
                    symbol=kraken_symbol,
                    side=side,
                    quantity=quantity,
                    error="All retry attempts exhausted"
                )
                await log_error_to_db(
                    self.db_pool,
                    user_api_key,
                    "ENTRY_ORDER_FAILED",
                    "Entry order failed after all retries",
                    {"symbol": kraken_symbol, "side": side, "quantity": quantity}
                )
                return
            
            self.logger.info(f"   ✅ Entry: {entry_order['id']}")
            
            # Wait for fill
            await asyncio.sleep(2)
            
            # 2. Take-profit order (limit, reduce-only) - WITH RETRY
            self.logger.info(f"   📝 Placing take-profit order...")
            tp_price = float(exchange.price_to_precision(kraken_symbol, take_profit))
            tp_order = await place_tp_order_with_retry(
                exchange=exchange,
                symbol=kraken_symbol,
                exit_side=exit_side,
                quantity=quantity,
                tp_price=tp_price,
                user_email=user_email,
                user_api_key=user_api_key
            )
            
            tp_placed = tp_order is not None
            if tp_placed:
                self.logger.info(f"   ✅ TP @ ${tp_price}: {tp_order['id']}")
            else:
                self.logger.error(f"   ❌ TP order FAILED - ABORTING TRADE!")
                # Emergency close the entry position
                await self._emergency_close_position(
                    exchange, kraken_symbol, exit_side, quantity, 
                    user_email, user_api_key, entry_order['id'],
                    reason="TP order failed after all retries"
                )
                return False
            
            # 3. Stop-loss order (stop, reduce-only) - WITH RETRY
            self.logger.info(f"   📝 Placing stop-loss order...")
            sl_price = float(exchange.price_to_precision(kraken_symbol, stop_loss))
            sl_order = await place_sl_order_with_retry(
                exchange=exchange,
                symbol=kraken_symbol,
                exit_side=exit_side,
                quantity=quantity,
                sl_price=sl_price,
                user_email=user_email,
                user_api_key=user_api_key
            )
            
            sl_placed = sl_order is not None
            if sl_placed:
                self.logger.info(f"   ✅ SL @ ${sl_price}: {sl_order['id']}")
            else:
                self.logger.error(f"   ❌ SL order FAILED - ABORTING TRADE!")
                # Cancel the orphaned TP order
                try:
                    exchange.cancel_order(tp_order['id'], kraken_symbol)
                    self.logger.info(f"   ✅ Orphaned TP order {tp_order['id']} cancelled")
                except Exception as cancel_e:
                    self.logger.warning(f"   ⚠️ Could not cancel orphaned TP: {cancel_e}")
                
                # Emergency close the entry position
                await self._emergency_close_position(
                    exchange, kraken_symbol, exit_side, quantity,
                    user_email, user_api_key, entry_order['id'],
                    reason="SL order failed after all retries"
                )
                return False
            
            # ==================== RECORD OPEN POSITION ====================
            # Get entry fill price (may differ from signal due to slippage)
            entry_fill_price = entry_price  # Default to signal price
            try:
                # Try to get actual fill price from order
                filled_order = exchange.fetch_order(entry_order['id'], kraken_symbol)
                if filled_order.get('average'):
                    entry_fill_price = float(filled_order['average'])
                    self.logger.info(f"   📊 Entry fill price: ${entry_fill_price:.2f}")
            except Exception as e:
                self.logger.warning(f"   ⚠️ Could not fetch fill price, using signal price: {e}")
            
            # Save to open_positions table
            try:
                async with self.db_pool.acquire() as conn:
                    # Get signal DB ID from signal_id string
                    signal_db_id = await conn.fetchval(
                        "SELECT id FROM signals WHERE signal_id = $1",
                        signal.get('signal_id')
                    )
                    
                    # Use consistent UTC timestamp for both position and billing cycle
                    position_opened_at = datetime.utcnow()
                    
                    await conn.execute("""
                        INSERT INTO open_positions 
                        (user_id, signal_id, entry_order_id, tp_order_id, sl_order_id,
                         symbol, kraken_symbol, side, quantity, leverage,
                         entry_fill_price, target_tp, target_sl, opened_at, status)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                    """, 
                        user['id'],
                        signal_db_id,
                        entry_order['id'],
                        tp_order['id'] if tp_order else None,
                        sl_order['id'] if sl_order else None,
                        signal['symbol'],  # BTC/USDT format
                        kraken_symbol,  # PF_XBTUSD format
                        action.upper(),  # BUY or SELL
                        quantity,
                        leverage,
                        entry_fill_price,
                        tp_price,
                        sl_price,
                        position_opened_at,
                        'open'
                    )
                    
                    # Start billing cycle if not started (first trade OPEN)
                    # Use position's opened_at to ensure the trade is included in the cycle
                    await conn.execute("""
                        UPDATE follower_users SET 
                            billing_cycle_start = $2
                        WHERE id = $1 AND billing_cycle_start IS NULL
                    """, user['id'], position_opened_at)
                    
                    self.logger.info(f"   📝 Open position recorded in database")
            except Exception as e:
                self.logger.error(f"   ⚠️ Failed to record position (trade still placed): {e}")
            
            self.logger.info(f"   🎉 {user_short}: Trade executed successfully!")
            
            return True
            
        except ccxt.InsufficientFunds as e:
            self.logger.error(f"   ❌ {user_short}: Insufficient funds - {str(e)[:100]}")
            await log_error_to_db(
                self.db_pool, user_api_key, "INSUFFICIENT_FUNDS",
                str(e)[:200], {"symbol": kraken_symbol, "side": action, "function": "execute_trade"}
            )
            return False
            
        except ccxt.InvalidOrder as e:
            self.logger.error(f"   ❌ {user_short}: Invalid order - {str(e)[:100]}")
            await log_error_to_db(
                self.db_pool, user_api_key, "INVALID_ORDER",
                str(e)[:200], {"symbol": kraken_symbol, "side": action, "function": "execute_trade"}
            )
            return False
            
        except ccxt.AuthenticationError as e:
            self.logger.error(f"   ❌ {user_short}: Auth failed - credentials may be invalid")
            await log_error_to_db(
                self.db_pool, user_api_key, "AUTH_ERROR",
                "Kraken API authentication failed - credentials may be invalid",
                {"function": "execute_trade"}
            )
            # Remove from cache so it re-authenticates next time
            if user_api_key in self.active_exchanges:
                del self.active_exchanges[user_api_key]
            return False
            
        except Exception as e:
            self.logger.error(f"   ❌ {user_short}: Error - {type(e).__name__}: {str(e)[:100]}")
            await log_error_to_db(
                self.db_pool, user_api_key, f"TRADE_EXECUTION_ERROR",
                str(e)[:200], {"symbol": kraken_symbol, "side": action, "error_type": type(e).__name__, "function": "execute_trade"}
            )
            return False
    
    async def _emergency_close_position(
        self,
        exchange,
        symbol: str,
        exit_side: str,
        quantity: float,
        user_email: str,
        user_api_key: str,
        entry_order_id: str,
        reason: str
    ):
        """
        Emergency market close when TP/SL placement fails.
        
        This protects users from having unprotected positions due to API failures.
        The position is closed at market price and admin is notified.
        """
        self.logger.critical(f"   🚨🚨🚨 EMERGENCY CLOSE TRIGGERED 🚨🚨🚨")
        self.logger.critical(f"   Reason: {reason}")
        self.logger.critical(f"   Symbol: {symbol}, Side: {exit_side}, Qty: {quantity}")
        
        close_success = False
        close_order_id = None
        
        try:
            close_order = exchange.create_order(
                symbol=symbol,
                type='market',
                side=exit_side,
                amount=quantity,
                params={'reduceOnly': True}
            )
            close_order_id = close_order.get('id')
            close_success = True
            self.logger.critical(f"   ✅ EMERGENCY CLOSE SUCCESSFUL: {close_order_id}")
            self.logger.critical(f"   Position closed at market - check for slippage!")
            
        except Exception as e:
            self.logger.critical(f"   ❌❌❌ EMERGENCY CLOSE FAILED: {e} ❌❌❌")
            self.logger.critical(f"   🚨 MANUAL INTERVENTION REQUIRED!")
        
        # Notify admin via Discord
        await notify_bracket_incomplete(
            user_email=user_email,
            user_api_key=user_api_key,
            symbol=symbol,
            entry_order_id=entry_order_id,
            tp_placed=False,
            sl_placed=False,
            error=f"{reason} - Emergency close {'SUCCESSFUL' if close_success else 'FAILED'}"
        )
        
        # Log to database
        await log_error_to_db(
            self.db_pool,
            user_api_key,
            "EMERGENCY_CLOSE",
            f"{reason}. Close: {'OK' if close_success else 'FAILED'}",
            {
                "symbol": symbol,
                "entry_order_id": entry_order_id,
                "close_order_id": close_order_id,
                "close_success": close_success
            }
        )
    
    async def poll_and_execute(self):
        """
        Single poll cycle - check all users for signals
        
        OPTIMIZED: Uses batched query to get all pending signals at once
        instead of querying per-user. Reduces N queries to 1 query.
        
        SCALED FOR 5000+ USERS:
        - Random shuffle for complete fairness
        - Parallel batch execution (25 users per batch)
        - 50ms delay between batches to stay within rate limits
        - 5000 users executes in ~10-15 seconds
        """
        # OPTIMIZED: Single query gets all pending signals with user info
        pending = await self.get_pending_signals_batched()
        
        if not pending:
            return  # No pending signals, skip silently
        
        # FAIRNESS: Randomize execution order so no user is always first/last
        random.shuffle(pending)
        
        self.logger.info(f"📡 Found {len(pending)} pending signal(s) to execute")
        
        # PARALLEL BATCH EXECUTION
        # Batch size 25 = ~100 tokens per batch (safe under 500 limit)
        BATCH_SIZE = 25
        BATCH_DELAY = 0.05  # 50ms between batches
        
        for i in range(0, len(pending), BATCH_SIZE):
            batch = pending[i:i + BATCH_SIZE]
            
            # Execute batch in parallel
            await asyncio.gather(*[
                self._execute_signal_for_user(item) for item in batch
            ], return_exceptions=True)
            
            # Rate limit delay between batches
            if i + BATCH_SIZE < len(pending):
                await asyncio.sleep(BATCH_DELAY)
    
    async def _execute_signal_for_user(self, item: Dict):
        """
        Execute a single signal for a single user.
        Extracted for parallel batch execution.
        """
        user_short = item['api_key'][:15] + "..."
        
        try:
            # Build user dict for execute_trade
            user = {
                'id': item['user_id'],
                'api_key': item['api_key'],
                'email': item['email'],
                'kraken_api_key_encrypted': item['kraken_api_key_encrypted'],
                'kraken_api_secret_encrypted': item['kraken_api_secret_encrypted'],
            }
            
            # Build signal dict for execute_trade
            signal = {
                'delivery_id': item['delivery_id'],
                'signal_id': item['signal_id'],
                'action': item['action'],
                'symbol': item['symbol'],
                'entry_price': item['entry_price'],
                'stop_loss': item['stop_loss'],
                'take_profit': item['take_profit'],
                'leverage': item['leverage'],
                'risk_pct': item['risk_pct'],
                'created_at': item['signal_created_at'],
            }
            
            self.logger.info(f"✨ {user_short}: Signal found - {signal['action']} {signal['symbol']}")
            
            # Execute trade
            success = await self.execute_trade(user, signal)
            
            if success:
                # Acknowledge signal
                await self.acknowledge_signal(signal['delivery_id'])
                self.logger.info(f"✅ {user_short}: Trade executed and acknowledged")
            else:
                self.logger.warning(f"⚠️ {user_short}: Trade failed, will retry next poll")
                
        except Exception as e:
            self.logger.error(f"❌ {user_short}: Error - {e}")
            await log_error_to_db(
                self.db_pool, item.get('api_key', 'unknown'), "SIGNAL_PROCESSING_ERROR",
                str(e)[:200], {"signal_id": item.get('signal_id'), "function": "_execute_signal_for_user"}
            )
    
    async def run(self):
        """
        Main loop - polls continuously every 10 seconds
        
        Note: Unlike standalone follower agents that sync with algo timing,
        the hosted model should always be ready since signals come via broadcast.
        """
        self.logger.info("=" * 60)
        self.logger.info("🚀 HOSTED TRADING LOOP STARTED")
        self.logger.info("=" * 60)
        self.logger.info(f"🔄 Poll interval: {POLL_INTERVAL_SECONDS} seconds")
        self.logger.info(f"💰 Risk per trade: From signal (2-3%), default {DEFAULT_RISK_PERCENTAGE*100:.0f}%")
        self.logger.info("=" * 60)
        
        poll_count = 0
        last_status_log = datetime.now()
        
        while True:
            try:
                poll_count += 1
                
                await self.poll_and_execute()
                
                # Log status every 5 minutes to show we're alive
                if (datetime.now() - last_status_log).total_seconds() >= 300:
                    users = await self.get_active_users()
                    self.logger.info(f"💓 Trading loop alive - Poll #{poll_count}, {len(users)} active users")
                    last_status_log = datetime.now()
                
                # Wait before next poll
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                    
            except asyncio.CancelledError:
                self.logger.info("🛑 Trading loop cancelled")
                break
                
            except Exception as e:
                self.logger.error(f"❌ Error in trading loop: {e}")
                import traceback
                traceback.print_exc()
                await log_error_to_db(
                    self.db_pool, "system", "TRADING_LOOP_ERROR",
                    str(e)[:200], {"poll_count": poll_count, "traceback": traceback.format_exc()[:500]}
                )
                # Critical system error - notify
                await notify_critical_error(
                    error_type="TRADING_LOOP_ERROR",
                    error=str(e),
                    location="hosted_trading_loop.run",
                    context={"poll_count": poll_count, "traceback": traceback.format_exc()[:200]}
                )
                # Wait before retrying
                await asyncio.sleep(10)


async def start_hosted_trading(db_pool):
    """
    Start the hosted trading loop.
    
    Call this from main.py startup:
    
    ```python
    from hosted_trading_loop import start_hosted_trading
    
    @app.on_event("startup")
    async def startup_event():
        # ... existing code ...
        
        # Start hosted trading loop
        asyncio.create_task(start_hosted_trading(db_pool))
    ```
    """
    # Wait for database to be ready
    await asyncio.sleep(35)  # Wait after balance checker starts
    
    loop = HostedTradingLoop(db_pool)
    await loop.run()


# For testing standalone
if __name__ == "__main__":
    print("This module should be imported and run from main.py")
    print("See start_hosted_trading() function")
