"""
Nike Rocket Position Monitor
=============================

Monitors open positions for TP/SL fills and records actual P&L.

This ensures:
- Accurate profit tracking (uses real fill prices, not signals)
- Fair billing (users only pay on actual profits)
- Dashboard accuracy (shows real trading results)

Flow:
1. Hosted trading loop places orders → saves to open_positions table
2. Position monitor runs every 60 seconds
3. Checks each open position against Kraken
4. When TP or SL fills → calculates real P&L → records in trades table
5. Updates user statistics
6. Removes from open_positions

Author: Nike Rocket Team
Created: November 24, 2025
"""

import asyncio
import logging
import os
import secrets
from datetime import datetime
from typing import Optional, Dict, Any

import ccxt
from cryptography.fernet import Fernet

# Configuration
CHECK_INTERVAL_SECONDS = 60  # Check every minute
FEE_PERCENTAGE = 0.10  # 10% of profits

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("POSITION_MONITOR")

# Encryption for credentials
ENCRYPTION_KEY = os.getenv("CREDENTIALS_ENCRYPTION_KEY")
cipher = Fernet(ENCRYPTION_KEY.encode()) if ENCRYPTION_KEY else None


class PositionMonitor:
    """
    Monitors open positions and records actual P&L when they close.
    """
    
    def __init__(self, db_pool):
        self.db_pool = db_pool
        self.logger = logger
        self.active_exchanges: Dict[str, ccxt.krakenfutures] = {}
    
    def decrypt_credentials(self, encrypted_key: str, encrypted_secret: str):
        """Decrypt Kraken API credentials"""
        if not cipher:
            return None, None
        try:
            api_key = cipher.decrypt(encrypted_key.encode()).decode()
            api_secret = cipher.decrypt(encrypted_secret.encode()).decode()
            return api_key, api_secret
        except Exception:
            return None, None
    
    def get_exchange(self, user_api_key: str, kraken_key: str, kraken_secret: str) -> Optional[ccxt.krakenfutures]:
        """Get or create exchange instance for user"""
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
            self.logger.error(f"Failed to create exchange: {e}")
            return None
    
    async def get_open_positions(self) -> list:
        """Fetch all open positions from database"""
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT 
                    op.id,
                    op.user_id,
                    op.signal_id,
                    op.entry_order_id,
                    op.tp_order_id,
                    op.sl_order_id,
                    op.symbol,
                    op.kraken_symbol,
                    op.side,
                    op.quantity,
                    op.leverage,
                    op.entry_fill_price,
                    op.target_tp,
                    op.target_sl,
                    op.opened_at,
                    u.api_key as user_api_key,
                    u.kraken_api_key_encrypted,
                    u.kraken_api_secret_encrypted
                FROM open_positions op
                JOIN follower_users u ON op.user_id = u.id
                WHERE op.status = 'open'
            """)
            return [dict(row) for row in rows]
    
    async def check_order_status(self, exchange: ccxt.krakenfutures, order_id: str, symbol: str) -> Dict[str, Any]:
        """Check if an order has filled - NOT USED for Kraken Futures"""
        # Kraken Futures doesn't support fetchOrder, use check_position_closed instead
        return {'status': 'unknown', 'error': 'Use check_position_closed instead'}
    
    async def check_position_closed(self, exchange: ccxt.krakenfutures, kraken_symbol: str, side: str, quantity: float, tp_order_id: str, sl_order_id: str) -> Dict[str, Any]:
        """
        Check if position is still open on Kraken.
        Uses multiple verification methods to avoid false positives.
        
        Returns:
            - {'closed': False} if position still exists
            - {'closed': True, 'exit_price': price, 'exit_type': 'TP'/'SL'} if position closed
        """
        try:
            # Method 1: Check open positions
            positions = exchange.fetch_positions([kraken_symbol])
            
            for pos in positions:
                if pos['symbol'] == kraken_symbol:
                    contracts = abs(float(pos.get('contracts', 0) or 0))
                    
                    if contracts > 0:
                        # Position still open
                        return {'closed': False, 'current_size': contracts}
            
            # No position found - verify by checking if TP or SL filled
            # Check open orders - if both TP and SL still exist, position wasn't closed
            try:
                open_orders = exchange.fetch_open_orders(kraken_symbol)
                tp_exists = any(o['id'] == tp_order_id for o in open_orders)
                sl_exists = any(o['id'] == sl_order_id for o in open_orders)
                
                if tp_exists and sl_exists:
                    # Both orders still exist but no position? Weird - keep monitoring
                    self.logger.warning(f"⚠️ No position but both TP/SL orders exist for {kraken_symbol}")
                    return {'closed': False, 'anomaly': True}
                
                # One order filled - determine which one
                if not tp_exists and sl_exists:
                    # TP filled, position closed
                    exit_type = 'TP'
                elif tp_exists and not sl_exists:
                    # SL filled, position closed
                    exit_type = 'SL'
                else:
                    # Both canceled? Might be manual close
                    self.logger.warning(f"⚠️ Both TP/SL canceled for {kraken_symbol} - manual close?")
                    return {'closed': False, 'manual_close': True}
                
            except Exception as e:
                self.logger.warning(f"Could not check open orders: {e}")
                # Fall back to position check only
                exit_type = 'UNKNOWN'
            
            # Get exit price from recent trades
            exit_price = None
            try:
                trades = exchange.fetch_my_trades(kraken_symbol, limit=20)
                if trades:
                    # Find the closing trade (opposite side of entry)
                    entry_side = side  # 'BUY' or 'SELL'
                    exit_side = 'SELL' if entry_side == 'BUY' else 'BUY'
                    
                    # Get most recent exit trade
                    exit_trades = [t for t in trades if t.get('side', '').upper() == exit_side]
                    if exit_trades:
                        latest = exit_trades[-1]
                        exit_price = float(latest.get('price', 0))
            except Exception as e:
                self.logger.warning(f"Could not fetch trades for exit price: {e}")
            
            return {'closed': True, 'exit_price': exit_price, 'exit_type': exit_type}
            
        except Exception as e:
            self.logger.error(f"Error checking position: {e}")
            return {'closed': False, 'error': str(e)}
    
    async def record_trade(self, position: dict, exit_price: float, exit_type: str, closed_at: datetime):
        """Record completed trade in database"""
        try:
            # Calculate P&L
            entry_price = float(position['entry_fill_price'])
            quantity = float(position['quantity'])
            side = str(position['side'])
            
            # P&L calculation
            if side == 'BUY':
                # Long: profit = (exit - entry) * quantity
                profit_usd = (exit_price - entry_price) * quantity
            else:
                # Short: profit = (entry - exit) * quantity
                profit_usd = (entry_price - exit_price) * quantity
            
            profit_percent = (profit_usd / (entry_price * quantity)) * 100 if entry_price > 0 else 0
            
            # Calculate fee (10% of profit, only if profitable)
            fee_charged = max(0, profit_usd * FEE_PERCENTAGE)
            
            # Generate trade ID
            trade_id = f"trade_{secrets.token_urlsafe(12)}"
            
            async with self.db_pool.acquire() as conn:
                # Insert trade record
                # Note: signal_id in trades table might be nullable, handle None case
                signal_id = position.get('signal_id')
                
                await conn.execute("""
                    INSERT INTO trades 
                    (user_id, trade_id, kraken_order_id,
                     opened_at, closed_at, symbol, side,
                     entry_price, exit_price, position_size, leverage,
                     profit_usd, profit_percent, fee_charged, notes)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                """,
                    int(position['user_id']),
                    trade_id,
                    str(position['entry_order_id']),
                    position['opened_at'],
                    closed_at,
                    str(position['symbol']),
                    side,
                    entry_price,
                    exit_price,
                    quantity,
                    float(position['leverage']),
                    profit_usd,
                    profit_percent,
                    fee_charged,
                    f"Closed by {exit_type}"
                )
                
                # Update user statistics
                await conn.execute("""
                    UPDATE follower_users 
                    SET 
                        total_profit = total_profit + $1,
                        total_trades = total_trades + 1,
                        monthly_profit = monthly_profit + $1,
                        monthly_trades = monthly_trades + 1,
                        monthly_fee_due = monthly_fee_due + $2
                    WHERE id = $3
                """, profit_usd, fee_charged, position['user_id'])
                
                # Mark position as closed
                await conn.execute("""
                    UPDATE open_positions SET status = 'closed' WHERE id = $1
                """, position['id'])
            
            # Log result
            emoji = "🟢" if profit_usd >= 0 else "🔴"
            self.logger.info(f"{emoji} Trade closed: {position['symbol']} {side}")
            self.logger.info(f"   Entry: ${entry_price:.2f} → Exit: ${exit_price:.2f}")
            self.logger.info(f"   P&L: ${profit_usd:+.2f} ({profit_percent:+.2f}%)")
            if fee_charged > 0:
                self.logger.info(f"   Fee: ${fee_charged:.2f} (10% of profit)")
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error recording trade: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    async def check_position(self, position: dict):
        """Check a single position - is it still open on Kraken?"""
        user_short = position['user_api_key'][:15] + "..."
        
        try:
            # Decrypt credentials
            kraken_key, kraken_secret = self.decrypt_credentials(
                position['kraken_api_key_encrypted'],
                position['kraken_api_secret_encrypted']
            )
            
            if not kraken_key:
                self.logger.error(f"❌ {user_short}: Cannot decrypt credentials")
                return
            
            # Get exchange instance
            exchange = self.get_exchange(position['user_api_key'], kraken_key, kraken_secret)
            if not exchange:
                return
            
            kraken_symbol = position['kraken_symbol']
            
            # Check if position is still open on Kraken
            result = await self.check_position_closed(
                exchange, 
                kraken_symbol, 
                position['side'], 
                position['quantity'],
                position['tp_order_id'],
                position['sl_order_id']
            )
            
            if not result.get('closed'):
                # Position still open, nothing to do
                if result.get('anomaly'):
                    self.logger.warning(f"⚠️ {user_short}: Anomaly detected - will continue monitoring")
                if result.get('manual_close'):
                    # Both TP and SL canceled - might be manual close
                    # Mark for review
                    async with self.db_pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE open_positions SET status = 'needs_review' WHERE id = $1",
                            position['id']
                        )
                    self.logger.warning(f"⚠️ {user_short}: Manual close detected - marked for review")
                return
            
            # Position closed! Get details
            exit_price = result.get('exit_price')
            exit_type_detected = result.get('exit_type', 'UNKNOWN')
            
            if exit_price is None:
                # Couldn't get exit price, mark for manual review
                self.logger.warning(f"⚠️ {user_short}: Could not get exit price - marked for review")
                async with self.db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE open_positions SET status = 'needs_review' WHERE id = $1",
                        position['id']
                    )
                return
            
            # Verify exit type by comparing to targets
            entry = position['entry_fill_price']
            tp = position['target_tp']
            sl = position['target_sl']
            
            # Calculate distance to TP and SL
            dist_to_tp = abs(exit_price - tp)
            dist_to_sl = abs(exit_price - sl)
            
            if dist_to_tp < dist_to_sl:
                exit_type = 'TP'
                self.logger.info(f"🎯 {user_short}: TP HIT on {position['symbol']} @ ${exit_price:.2f}")
            else:
                exit_type = 'SL'
                self.logger.info(f"🛑 {user_short}: SL HIT on {position['symbol']} @ ${exit_price:.2f}")
            
            # Cross-check with detected type
            if exit_type_detected != 'UNKNOWN' and exit_type_detected != exit_type:
                self.logger.warning(f"⚠️ Exit type mismatch: detected {exit_type_detected}, calculated {exit_type}")
            
            # Record the trade with actual P&L
            await self.record_trade(position, exit_price, exit_type, datetime.now())
            
            # Cancel remaining order (TP or SL that didn't fill)
            try:
                if exit_type == 'TP':
                    exchange.cancel_order(position['sl_order_id'], kraken_symbol)
                else:
                    exchange.cancel_order(position['tp_order_id'], kraken_symbol)
                self.logger.info(f"   ✅ Cancelled remaining order")
            except Exception as e:
                # Order might already be cancelled
                self.logger.debug(f"   Could not cancel order: {e}")
                
        except Exception as e:
            self.logger.error(f"❌ Error checking position {position['id']}: {e}")
    
    async def check_all_positions(self):
        """Check all open positions"""
        positions = await self.get_open_positions()
        
        if not positions:
            return
        
        self.logger.debug(f"📊 Checking {len(positions)} open positions...")
        
        for position in positions:
            await self.check_position(position)
            # Small delay between checks to avoid rate limits
            await asyncio.sleep(0.5)
    
    async def run(self):
        """Main loop - checks positions every 60 seconds"""
        self.logger.info("=" * 60)
        self.logger.info("📊 POSITION MONITOR STARTED")
        self.logger.info("=" * 60)
        self.logger.info(f"🔄 Check interval: {CHECK_INTERVAL_SECONDS} seconds")
        self.logger.info(f"💰 Fee percentage: {FEE_PERCENTAGE*100:.0f}% of profits")
        self.logger.info("=" * 60)
        
        check_count = 0
        last_status_log = datetime.now()
        
        while True:
            try:
                check_count += 1
                
                await self.check_all_positions()
                
                # Log status every 5 minutes
                if (datetime.now() - last_status_log).total_seconds() >= 300:
                    positions = await self.get_open_positions()
                    self.logger.info(f"💓 Position monitor alive - Check #{check_count}, {len(positions)} open positions")
                    last_status_log = datetime.now()
                
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
                
            except asyncio.CancelledError:
                self.logger.info("🛑 Position monitor cancelled")
                break
                
            except Exception as e:
                self.logger.error(f"❌ Error in position monitor: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(10)


async def start_position_monitor(db_pool):
    """
    Start the position monitor.
    
    Call this from main.py startup:
    
    ```python
    from position_monitor import start_position_monitor
    
    @app.on_event("startup")
    async def startup_event():
        ...
        asyncio.create_task(start_position_monitor(db_pool))
    ```
    """
    # Wait for other systems to initialize
    await asyncio.sleep(40)
    
    logger.info("🚀 Starting position monitor...")
    
    monitor = PositionMonitor(db_pool)
    await monitor.run()
