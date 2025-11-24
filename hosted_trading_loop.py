"""
Nike Rocket - Hosted Trading Loop
==================================
Background task that polls for signals and executes trades for ALL active users.
Runs on Railway as part of main.py startup.

Features:
- Polls /api/latest-signal for each active user every 10 seconds
- Decrypts user Kraken credentials from database
- Calculates position size (2% risk formula)
- Executes 3-order bracket (Entry + TP + SL)
- Smart polling (first 3 minutes of each hour)
- Logs all activity for admin dashboard

Author: Nike Rocket Team
Updated: November 24, 2025
"""

import asyncio
import ccxt
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import os

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('HOSTED_TRADING')

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
            
            # Check for existing position
            if await self.check_existing_position(exchange, kraken_symbol):
                self.logger.info(f"   ⚠️ {user_short}: Already has position in {kraken_symbol}, skipping")
                return False
            
            # Get equity
            equity = await self.get_user_equity(exchange)
            if equity <= 0:
                self.logger.error(f"   ❌ {user_short}: No equity found")
                return False
            
            # Extract signal data
            action = signal['action']  # BUY or SELL
            entry_price = float(signal['entry_price'])
            stop_loss = float(signal['stop_loss'])
            take_profit = float(signal['take_profit'])
            leverage = float(signal.get('leverage', 5.0))
            
            # Get risk percentage from signal (2% aggressive, 3% conservative)
            # Falls back to default if not specified
            risk_pct = float(signal.get('risk_pct', DEFAULT_RISK_PERCENTAGE))
            
            # ==================== POSITION SIZING ====================
            risk_amount = equity * risk_pct
            risk_per_unit = abs(entry_price - stop_loss)
            
            if risk_per_unit <= 0:
                self.logger.error(f"   ❌ {user_short}: Invalid SL distance")
                return False
            
            base_position_size = risk_amount / risk_per_unit
            position_size = base_position_size * leverage
            
            # Round to exchange precision
            quantity = float(exchange.amount_to_precision(kraken_symbol, position_size))
            
            self.logger.info(f"   💰 Equity: ${equity:,.2f}")
            self.logger.info(f"   🎯 Risk: ${risk_amount:,.2f} ({risk_pct*100:.0f}%)")
            self.logger.info(f"   📐 Position: {quantity} contracts")
            
            # ==================== EXECUTE 3-ORDER BRACKET ====================
            
            side = action.lower()  # 'buy' or 'sell'
            exit_side = 'sell' if side == 'buy' else 'buy'
            
            # 1. Entry order (market)
            self.logger.info(f"   📝 Placing entry order...")
            entry_order = exchange.create_market_order(kraken_symbol, side, quantity)
            self.logger.info(f"   ✅ Entry: {entry_order['id']}")
            
            # Wait for fill
            await asyncio.sleep(2)
            
            # 2. Take-profit order (limit, reduce-only)
            self.logger.info(f"   📝 Placing take-profit order...")
            tp_price = float(exchange.price_to_precision(kraken_symbol, take_profit))
            tp_order = exchange.create_limit_order(
                kraken_symbol, exit_side, quantity, tp_price,
                params={'reduceOnly': True}
            )
            self.logger.info(f"   ✅ TP @ ${tp_price}: {tp_order['id']}")
            
            # 3. Stop-loss order (stop, reduce-only)
            self.logger.info(f"   📝 Placing stop-loss order...")
            sl_price = float(exchange.price_to_precision(kraken_symbol, stop_loss))
            
            # Try stop_market first, fallback to stop
            try:
                sl_order = exchange.create_order(
                    kraken_symbol, 'stop_market', exit_side, quantity,
                    params={'stopPrice': sl_price, 'reduceOnly': True}
                )
            except Exception:
                sl_order = exchange.create_order(
                    kraken_symbol, 'stop', exit_side, quantity,
                    params={'triggerPrice': sl_price, 'reduceOnly': True}
                )
            
            self.logger.info(f"   ✅ SL @ ${sl_price}: {sl_order['id']}")
            
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
                        tp_order['id'],
                        sl_order['id'],
                        signal['symbol'],  # BTC/USDT format
                        kraken_symbol,  # PF_XBTUSD format
                        action.upper(),  # BUY or SELL
                        quantity,
                        leverage,
                        entry_fill_price,
                        tp_price,
                        sl_price,
                        datetime.now(),
                        'open'
                    )
                    self.logger.info(f"   📝 Open position recorded in database")
            except Exception as e:
                self.logger.error(f"   ⚠️ Failed to record position (trade still placed): {e}")
            
            self.logger.info(f"   🎉 {user_short}: Trade executed successfully!")
            
            return True
            
        except ccxt.InsufficientFunds as e:
            self.logger.error(f"   ❌ {user_short}: Insufficient funds - {str(e)[:100]}")
            return False
            
        except ccxt.InvalidOrder as e:
            self.logger.error(f"   ❌ {user_short}: Invalid order - {str(e)[:100]}")
            return False
            
        except ccxt.AuthenticationError as e:
            self.logger.error(f"   ❌ {user_short}: Auth failed - credentials may be invalid")
            # Remove from cache so it re-authenticates next time
            if user_api_key in self.active_exchanges:
                del self.active_exchanges[user_api_key]
            return False
            
        except Exception as e:
            self.logger.error(f"   ❌ {user_short}: Error - {type(e).__name__}: {str(e)[:100]}")
            return False
    
    async def poll_and_execute(self):
        """Single poll cycle - check all users for signals"""
        users = await self.get_active_users()
        
        if not users:
            return  # No active users, skip silently
        
        signals_found = 0
        
        for user in users:
            user_short = user['api_key'][:15] + "..."
            
            try:
                # Get latest signal for this user
                signal = await self.get_latest_signal(user['api_key'])
                
                if signal:
                    signals_found += 1
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
