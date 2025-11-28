"""
Nike Rocket Position Monitor v2.1 - 30-Day Billing
===================================================

REFACTORED: Position-based tracking instead of individual fills

Key changes from v2.0:
- NO IMMEDIATE FEE CHARGING - profits accumulate in billing cycle
- Fee calculation happens at end of 30-day cycle via billing_service_30day.py
- Trades table still records profit_usd but fee_charged is always 0

CRITICAL v2.1 UPDATE - SIGNAL MATCHING:
- Only tracks trades that match a Nike Rocket signal
- Manual/user trades are SKIPPED (no fees charged)
- This ensures users aren't charged for their own trades

30-DAY BILLING FLOW:
1. Trade closes → Record profit in trades table (fee_charged = 0)
2. Update current_cycle_profit in follower_users (accumulated)
3. At end of 30 days, billing service calculates fee on total profit
4. Coinbase invoice sent if profitable

This ensures:
- Dashboard shows "1 position" not "10 trades"
- Accurate weighted-average entry price
- Clean P&L calculation on close
- Better stats that reflect actual round-trip performance
- Users only pay fees on copytraded signals
- Fees billed monthly, not per-trade

Author: Nike Rocket Team
Version: 2.1 (30-Day Billing)
"""

import asyncio
import logging
import os
import secrets
import time
from datetime import datetime
from typing import Optional, Dict, Any, List

import ccxt
from cryptography.fernet import Fernet

# Configuration
CHECK_INTERVAL_SECONDS = 60
FILL_LOOKBACK_HOURS = 24  # How far back to look for fills

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("POSITION_MONITOR")

# Encryption for credentials
ENCRYPTION_KEY = os.getenv("CREDENTIALS_ENCRYPTION_KEY")
cipher = Fernet(ENCRYPTION_KEY.encode()) if ENCRYPTION_KEY else None


class PositionMonitor:
    """
    Monitors positions and records actual P&L when they close.
    
    v2: Position-based tracking with fill aggregation
    """
    
    def __init__(self, db_pool):
        self.db_pool = db_pool
        self.logger = logger
        self.active_exchanges: Dict[str, ccxt.krakenfutures] = {}
    
    # ==================== Credential & Exchange Management ====================
    
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
    
    # ==================== Signal Matching ====================
    
    async def find_matching_signal(self, symbol: str, side: str, lookback_hours: int = 48) -> Optional[dict]:
        """
        Check if there's a Nike Rocket signal that matches this trade.
        
        CRITICAL: This ensures we only track/charge fees on copytraded positions,
        not manual trades the user makes on their own.
        
        Args:
            symbol: Trading pair (e.g., 'ADA/USD:USD', 'BTC/USD:USD')
            side: 'long' or 'short'
            lookback_hours: How far back to look for matching signals (default 48h)
            
        Returns:
            Matching signal dict if found, None if no match (manual trade)
        """
        try:
            async with self.db_pool.acquire() as conn:
                # Normalize symbol - extract base (e.g., 'ADA' from 'ADA/USD:USD')
                symbol_base = symbol.split('/')[0].upper() if '/' in symbol else symbol.upper()
                symbol_base = symbol_base.replace('PF_', '').replace('USD', '').replace(':USD', '')
                
                # Look for a recent signal matching this symbol and direction
                signal = await conn.fetchrow("""
                    SELECT id, signal_id, symbol, direction, created_at
                    FROM signals
                    WHERE UPPER(symbol) LIKE $1
                      AND LOWER(direction) = LOWER($2)
                      AND created_at >= NOW() - INTERVAL '%s hours'
                    ORDER BY created_at DESC
                    LIMIT 1
                """ % lookback_hours, f'%{symbol_base}%', side)
                
                if signal:
                    self.logger.info(f"✅ Found matching signal: {signal['symbol']} {signal['direction']} (signal_id: {signal['signal_id']})")
                    return dict(signal)
                else:
                    self.logger.info(f"⚠️ No matching signal found for {symbol} {side} - likely manual trade")
                    return None
                    
        except Exception as e:
            self.logger.error(f"Error checking for matching signal: {e}")
            # On error, assume it's a signal trade to avoid missing fees
            return {'id': None, 'signal_id': 'unknown'}
    
    # ==================== Fill Recording (Audit Trail) ====================
    
    async def record_fill(self, user_id: int, fill: dict) -> bool:
        """
        Record a single execution fill to position_fills table.
        
        This is the audit trail - every execution from the exchange.
        Returns True if new fill was recorded, False if duplicate.
        """
        try:
            order_id = fill.get('order_id') or fill.get('order')
            # Generate fill_id: use exchange's id, or create from order+timestamp
            fill_id = fill.get('id') or fill.get('fill_id')
            if not fill_id:
                fill_id = f"{order_id}_{fill.get('timestamp', int(time.time() * 1000))}"
            
            async with self.db_pool.acquire() as conn:
                # Check if already recorded by fill_id
                existing = await conn.fetchval("""
                    SELECT id FROM position_fills 
                    WHERE user_id = $1 AND fill_id = $2
                """, user_id, fill_id)
                
                if existing:
                    return False  # Already recorded
                
                # Parse timestamp
                fill_timestamp = None
                if fill.get('timestamp'):
                    ts = fill['timestamp']
                    if isinstance(ts, (int, float)):
                        # Convert to naive datetime (PostgreSQL TIMESTAMP without timezone)
                        fill_timestamp = datetime.utcfromtimestamp(ts / 1000)
                    elif isinstance(ts, datetime):
                        # Strip timezone if present
                        fill_timestamp = ts.replace(tzinfo=None) if ts.tzinfo else ts
                
                # Record the fill
                await conn.execute("""
                    INSERT INTO position_fills 
                    (user_id, kraken_order_id, fill_id, symbol, side, price, quantity, cost, fill_timestamp, source)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (user_id, fill_id) DO NOTHING
                """,
                    user_id,
                    order_id,
                    fill_id,
                    fill.get('symbol', 'UNKNOWN'),
                    fill.get('side', 'UNKNOWN'),
                    float(fill.get('price', 0)),
                    float(fill.get('amount', 0) or fill.get('quantity', 0)),
                    float(fill.get('cost', 0) or (fill.get('price', 0) * fill.get('amount', 0))),
                    fill_timestamp,
                    'exchange_api'
                )
                
                self.logger.info(f"   📝 Fill recorded: {fill.get('symbol')} {fill.get('side')} {fill.get('amount')} @ ${fill.get('price', 0):.4f}")
                return True
                
        except Exception as e:
            self.logger.error(f"   Failed to record fill: {e}")
            return False
    
    # ==================== Position Aggregation ====================
    
    async def get_aggregated_position(self, user_id: int, symbol: str) -> dict:
        """
        Calculate aggregated position from all unassigned fills.
        
        Returns:
            {
                'net_side': 'long' or 'short',
                'net_quantity': float,
                'avg_entry_price': float,
                'total_cost': float,
                'fill_count': int,
                'first_fill': datetime,
                'last_fill': datetime
            }
        """
        async with self.db_pool.acquire() as conn:
            result = await conn.fetchrow("""
                SELECT 
                    SUM(CASE WHEN side = 'buy' THEN quantity ELSE 0 END) as buy_qty,
                    SUM(CASE WHEN side = 'buy' THEN cost ELSE 0 END) as buy_cost,
                    SUM(CASE WHEN side = 'sell' THEN quantity ELSE 0 END) as sell_qty,
                    SUM(CASE WHEN side = 'sell' THEN cost ELSE 0 END) as sell_cost,
                    COUNT(*) as fill_count,
                    MIN(fill_timestamp) as first_fill,
                    MAX(fill_timestamp) as last_fill
                FROM position_fills
                WHERE user_id = $1 AND symbol = $2 AND position_id IS NULL
            """, user_id, symbol)
            
            if not result or result['fill_count'] == 0:
                return None
            
            buy_qty = float(result['buy_qty'] or 0)
            buy_cost = float(result['buy_cost'] or 0)
            sell_qty = float(result['sell_qty'] or 0)
            sell_cost = float(result['sell_cost'] or 0)
            
            # Calculate net position
            if sell_qty > buy_qty:
                net_side = 'short'
                net_quantity = sell_qty - buy_qty
                # For short: we sold high, cost basis is sell_cost - buy_cost
                total_cost = sell_cost - buy_cost
            else:
                net_side = 'long'
                net_quantity = buy_qty - sell_qty
                # For long: we bought low, cost basis is buy_cost - sell_cost
                total_cost = buy_cost - sell_cost
            
            # Weighted average entry
            if net_quantity > 0:
                avg_entry = abs(total_cost) / net_quantity
            else:
                avg_entry = 0
            
            return {
                'net_side': net_side,
                'net_quantity': net_quantity,
                'avg_entry_price': avg_entry,
                'total_cost': total_cost,
                'fill_count': result['fill_count'],
                'first_fill': result['first_fill'],
                'last_fill': result['last_fill']
            }
    
    async def sync_user_position(self, user_id: int, symbol: str, current_exchange_qty: float = None):
        """
        Sync fills into an aggregated position record.
        
        Creates or updates open_positions with aggregated data from fills.
        """
        try:
            agg = await self.get_aggregated_position(user_id, symbol)
            
            if not agg or agg['net_quantity'] <= 0:
                return  # No position
            
            async with self.db_pool.acquire() as conn:
                # Check if we already have an open position for this symbol
                existing = await conn.fetchrow("""
                    SELECT id, filled_quantity, avg_entry_price, fill_count
                    FROM open_positions 
                    WHERE user_id = $1 AND symbol = $2 AND status = 'open'
                """, user_id, symbol)
                
                if existing:
                    # Update existing position with new aggregate data
                    if (existing['filled_quantity'] != agg['net_quantity'] or 
                        existing['fill_count'] != agg['fill_count']):
                        
                        await conn.execute("""
                            UPDATE open_positions SET
                                avg_entry_price = $1,
                                filled_quantity = $2,
                                fill_count = $3,
                                total_cost_basis = $4,
                                last_fill_at = $5,
                                side = $6
                            WHERE id = $7
                        """,
                            agg['avg_entry_price'],
                            agg['net_quantity'],
                            agg['fill_count'],
                            abs(agg['total_cost']),
                            agg['last_fill'],
                            agg['net_side'],
                            existing['id']
                        )
                        
                        self.logger.info(
                            f"   📊 Position updated: {symbol} {agg['net_side']} "
                            f"{agg['net_quantity']:.2f} @ ${agg['avg_entry_price']:.4f} "
                            f"({agg['fill_count']} fills)"
                        )
                else:
                    # No existing position - this is fill-only tracking
                    # We'll just log it for now; proper positions are created by trading loop
                    self.logger.info(
                        f"   📊 Aggregated fills: {symbol} {agg['net_side']} "
                        f"{agg['net_quantity']:.2f} @ ${agg['avg_entry_price']:.4f} "
                        f"({agg['fill_count']} fills, no open_positions record)"
                    )
                    
        except Exception as e:
            self.logger.error(f"Error syncing position: {e}")
    
    # ==================== Exchange History Scanning ====================
    
    async def scan_exchange_fills(self, user_info: dict) -> List[dict]:
        """
        Scan exchange for recent fills and record any new ones.
        
        Returns list of newly recorded fills.
        """
        user_short = user_info['api_key'][:15] + "..."
        
        try:
            kraken_key, kraken_secret = self.decrypt_credentials(
                user_info['kraken_api_key_encrypted'],
                user_info['kraken_api_secret_encrypted']
            )
            
            if not kraken_key:
                return []
            
            exchange = self.get_exchange(user_info['api_key'], kraken_key, kraken_secret)
            if not exchange:
                return []
            
            # Fetch recent trades from Kraken
            trades = exchange.fetch_my_trades(limit=100)
            
            if not trades:
                return []
            
            new_fills = []
            symbols_affected = set()
            
            for trade in trades:
                # Check if recent (within lookback period)
                trade_timestamp = trade.get('timestamp', 0)
                if isinstance(trade_timestamp, (int, float)):
                    trade_age_hours = (time.time() * 1000 - trade_timestamp) / (1000 * 3600)
                    if trade_age_hours > FILL_LOOKBACK_HOURS:
                        continue
                
                # Record the fill
                fill_data = {
                    'order_id': trade.get('order'),
                    'id': trade.get('id'),
                    'symbol': trade.get('symbol'),
                    'side': trade.get('side'),
                    'price': trade.get('price'),
                    'amount': trade.get('amount'),
                    'cost': trade.get('cost'),
                    'timestamp': trade_timestamp,
                }
                
                was_new = await self.record_fill(user_info['id'], fill_data)
                if was_new:
                    new_fills.append(fill_data)
                    symbols_affected.add(trade.get('symbol'))
            
            # Sync positions for affected symbols
            for symbol in symbols_affected:
                await self.sync_user_position(user_info['id'], symbol)
            
            if new_fills:
                self.logger.info(f"⚡ {user_short}: Recorded {len(new_fills)} new fills")
            
            return new_fills
            
        except Exception as e:
            self.logger.debug(f"Could not scan fills for {user_short}: {e}")
            return []
    
    # ==================== Position Monitoring ====================
    
    async def get_active_users(self) -> list:
        """Get all active users with Kraken credentials"""
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT 
                    id,
                    api_key,
                    kraken_api_key_encrypted,
                    kraken_api_secret_encrypted,
                    fee_tier
                FROM follower_users
                WHERE kraken_api_key_encrypted IS NOT NULL
            """)
            return [dict(row) for row in rows]
    
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
                    op.avg_entry_price,
                    op.filled_quantity,
                    op.fill_count,
                    op.total_cost_basis,
                    op.target_tp,
                    op.target_sl,
                    op.opened_at,
                    op.first_fill_at,
                    op.last_fill_at,
                    u.api_key as user_api_key,
                    u.kraken_api_key_encrypted,
                    u.kraken_api_secret_encrypted,
                    u.fee_tier
                FROM open_positions op
                JOIN follower_users u ON op.user_id = u.id
                WHERE op.status = 'open'
            """)
            return [dict(row) for row in rows]
    
    async def check_position_closed(self, exchange: ccxt.krakenfutures, kraken_symbol: str, side: str, quantity: float, tp_order_id: str, sl_order_id: str) -> Dict[str, Any]:
        """
        Check if position is still open on Kraken.
        
        Returns:
            - {'closed': False} if position still exists
            - {'closed': True, 'exit_price': price, 'exit_type': 'TP'/'SL'} if closed
        """
        try:
            positions = exchange.fetch_positions()
            
            self.logger.info(f"🔍 Kraken positions API returned {len(positions)} items")
            
            if positions:
                for i, pos in enumerate(positions):
                    pos_symbol = pos.get('symbol', 'Unknown')
                    pos_contracts = pos.get('contracts') or pos.get('contractSize') or 0
                    pos_side = pos.get('side', 'Unknown')
                    
                    self.logger.info(f"   Position {i}: symbol={pos_symbol}, contracts={pos_contracts}, side={pos_side}")
                    
                    symbol_base = kraken_symbol.replace('PF_', '').replace('USD', '')
                    
                    if symbol_base in str(pos_symbol).upper():
                        contracts = abs(float(pos_contracts))
                        if contracts > 0:
                            self.logger.info(f"✅ Position found: {pos_symbol} {pos_side} ({contracts} contracts)")
                            return {'closed': False, 'current_size': contracts}
            
            # Check for ANY position
            for pos in positions:
                contracts = abs(float(pos.get('contracts') or pos.get('contractSize') or 0))
                if contracts > 0:
                    self.logger.info(f"✅ Position found (any symbol): {pos.get('symbol')} ({contracts} contracts)")
                    return {'closed': False, 'current_size': contracts}
            
            self.logger.info(f"📭 No open positions found in Kraken API response")
            
            # No position found - check which order filled
            exit_type = 'UNKNOWN'
            try:
                open_orders = exchange.fetch_open_orders(kraken_symbol)
                tp_exists = any(o['id'] == tp_order_id for o in open_orders)
                sl_exists = any(o['id'] == sl_order_id for o in open_orders)
                
                if tp_exists and sl_exists:
                    self.logger.warning(f"⚠️ No position but both TP/SL orders exist for {kraken_symbol}")
                    return {'closed': False, 'anomaly': True}
                
                if not tp_exists and sl_exists:
                    exit_type = 'TP'
                elif tp_exists and not sl_exists:
                    exit_type = 'SL'
                else:
                    # Both orders gone - could be TP filled (which also cancels SL)
                    # Don't assume manual close - check recent trades below
                    self.logger.info(f"📊 Both TP/SL orders completed for {kraken_symbol} - checking trades for exit price")
                    
            except Exception as e:
                self.logger.warning(f"Could not check open orders: {e}")
            
            # Get exit price from recent trades
            exit_price = None
            try:
                trades = exchange.fetch_my_trades(kraken_symbol, limit=20)
                if trades:
                    # Most recent trade is likely the exit
                    trades.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
                    exit_price = float(trades[0].get('price', 0))
            except Exception as e:
                self.logger.warning(f"Could not get exit price: {e}")
            
            return {
                'closed': True,
                'exit_price': exit_price,
                'exit_type': exit_type
            }
            
        except Exception as e:
            self.logger.error(f"Error checking position: {e}")
            return {'closed': False, 'error': str(e)}
    
    async def record_trade_close(self, position: dict, exit_price: float, exit_type: str, closed_at: datetime):
        """
        Record a closed position as ONE trade with actual P&L.
        
        IMPORTANT: Only records trades that match a Nike Rocket signal.
        Manual trades are skipped to avoid charging fees on user's own trades.
        
        Uses aggregated position data (avg entry, total size) for accurate P&L.
        """
        try:
            # Use aggregated entry if available, otherwise fall back to single entry
            entry_price = position.get('avg_entry_price') or position.get('entry_fill_price')
            position_size = position.get('filled_quantity') or position.get('quantity')
            fill_count = position.get('fill_count') or 1
            leverage = position.get('leverage', 1)
            side = position.get('side')
            symbol = position['symbol']
            
            if not entry_price or not position_size:
                self.logger.error("Missing entry price or position size")
                return False
            
            # ==================== SIGNAL MATCHING CHECK ====================
            # Check if this trade matches a Nike Rocket signal
            # If the position already has a signal_id (from open_positions), use it
            # Otherwise, try to find a matching signal in the signals table
            
            signal_id = position.get('signal_id')
            
            if not signal_id:
                # Position doesn't have signal_id - check signals table
                matching_signal = await self.find_matching_signal(symbol, side)
                
                if not matching_signal:
                    # No matching signal - this is a MANUAL TRADE
                    self.logger.info(f"⏭️ SKIPPING manual trade: {symbol} {side}")
                    self.logger.info(f"   (No Nike Rocket signal found - user's own trade)")
                    self.logger.info(f"   Entry: ${entry_price:.4f}, Exit: ${exit_price:.4f}")
                    
                    # Calculate P&L for logging only (not recorded/charged)
                    if side == 'long':
                        manual_pnl = (exit_price - entry_price) * position_size
                    else:
                        manual_pnl = (entry_price - exit_price) * position_size
                    self.logger.info(f"   P&L (not tracked): ${manual_pnl:+.2f}")
                    
                    # Still mark fills and position as processed to avoid reprocessing
                    async with self.db_pool.acquire() as conn:
                        if position.get('id'):
                            await conn.execute("""
                                UPDATE position_fills 
                                SET position_id = $1
                                WHERE user_id = $2 AND symbol = $3 AND position_id IS NULL
                            """, position['id'], position['user_id'], symbol)
                            
                            await conn.execute("""
                                UPDATE open_positions SET status = 'closed_manual' WHERE id = $1
                            """, position['id'])
                    
                    return False  # Not recorded (manual trade)
                
                signal_id = matching_signal.get('signal_id') or matching_signal.get('id')
            
            # ==================== PROCEED WITH RECORDING ====================
            # This is a Nike Rocket signal trade - record it (NO FEE - 30-day billing)
            
            # Calculate P&L
            if side == 'long':
                profit_usd = (exit_price - entry_price) * position_size
            else:  # short
                profit_usd = (entry_price - exit_price) * position_size
            
            profit_percent = ((exit_price - entry_price) / entry_price) * 100
            if side == 'short':
                profit_percent = -profit_percent
            
            # 30-DAY BILLING: No per-trade fee calculation
            # Fee is calculated at end of 30-day cycle by billing_service_30day.py
            fee_charged = 0  # Always 0 - fees handled by billing service
            
            # Generate trade ID
            trade_id = f"trade_{secrets.token_urlsafe(12)}"
            
            async with self.db_pool.acquire() as conn:
                # Record the trade (fee_charged = 0)
                await conn.execute("""
                    INSERT INTO trades 
                    (user_id, signal_id, trade_id, kraken_order_id, opened_at, closed_at,
                     symbol, side, entry_price, exit_price, position_size, leverage,
                     profit_usd, profit_percent, exit_type, fee_charged, notes)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
                """,
                    position['user_id'],
                    str(signal_id) if signal_id else None,
                    trade_id,
                    position.get('entry_order_id'),
                    position.get('opened_at') or position.get('first_fill_at'),
                    closed_at,
                    symbol,
                    side,
                    entry_price,
                    exit_price,
                    position_size,
                    leverage,
                    profit_usd,
                    profit_percent,
                    exit_type,
                    fee_charged,  # Always 0 for 30-day billing
                    f"Signal trade. Aggregated from {fill_count} fills. Avg entry: ${entry_price:.4f}"
                )
                
                # Update user stats - accumulate profit for 30-day billing
                # Note: total_fees NOT updated here - handled by billing service at cycle end
                await conn.execute("""
                    UPDATE follower_users SET 
                        total_trades = COALESCE(total_trades, 0) + 1,
                        total_profit = COALESCE(total_profit, 0) + $1,
                        current_cycle_profit = COALESCE(current_cycle_profit, 0) + $1,
                        current_cycle_trades = COALESCE(current_cycle_trades, 0) + 1
                    WHERE id = $2
                """, profit_usd, position['user_id'])
                
                # Start billing cycle if not started (first trade)
                await conn.execute("""
                    UPDATE follower_users SET 
                        billing_cycle_start = CURRENT_TIMESTAMP
                    WHERE id = $1 AND billing_cycle_start IS NULL
                """, position['user_id'])
                
                # Mark fills as assigned to this position (audit trail)
                if position.get('id'):
                    await conn.execute("""
                        UPDATE position_fills 
                        SET position_id = $1
                        WHERE user_id = $2 AND symbol = $3 AND position_id IS NULL
                    """, position['id'], position['user_id'], symbol)
                    
                    # Mark position as closed
                    await conn.execute("""
                        UPDATE open_positions SET status = 'closed' WHERE id = $1
                    """, position['id'])
            
            # Log result
            emoji = "🟢" if profit_usd >= 0 else "🔴"
            self.logger.info(f"{emoji} SIGNAL TRADE closed: {symbol} {side}")
            self.logger.info(f"   Entry: ${entry_price:.4f} (avg from {fill_count} fills)")
            self.logger.info(f"   Exit: ${exit_price:.4f} ({exit_type})")
            self.logger.info(f"   Size: {position_size:.2f} contracts")
            self.logger.info(f"   P&L: ${profit_usd:+.2f} ({profit_percent:+.2f}%)")
            self.logger.info(f"   📅 Profit added to 30-day billing cycle")
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error recording trade close: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    async def check_position(self, position: dict):
        """Check a single position - is it still open on Kraken?"""
        user_short = position['user_api_key'][:15] + "..."
        
        try:
            kraken_key, kraken_secret = self.decrypt_credentials(
                position['kraken_api_key_encrypted'],
                position['kraken_api_secret_encrypted']
            )
            
            if not kraken_key:
                self.logger.error(f"❌ {user_short}: Cannot decrypt credentials")
                return
            
            exchange = self.get_exchange(position['user_api_key'], kraken_key, kraken_secret)
            if not exchange:
                return
            
            # First scan for any new fills
            user_info = {
                'id': position['user_id'],
                'api_key': position['user_api_key'],
                'kraken_api_key_encrypted': position['kraken_api_key_encrypted'],
                'kraken_api_secret_encrypted': position['kraken_api_secret_encrypted'],
            }
            await self.scan_exchange_fills(user_info)
            
            kraken_symbol = position['kraken_symbol']
            
            # Check if position is still open
            result = await self.check_position_closed(
                exchange, 
                kraken_symbol, 
                position['side'], 
                position.get('filled_quantity') or position['quantity'],
                position['tp_order_id'],
                position['sl_order_id']
            )
            
            if not result.get('closed'):
                if result.get('anomaly'):
                    self.logger.warning(f"⚠️ {user_short}: Anomaly detected - continuing to monitor")
                if result.get('manual_close'):
                    async with self.db_pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE open_positions SET status = 'needs_review' WHERE id = $1",
                            position['id']
                        )
                    self.logger.warning(f"⚠️ {user_short}: Manual close detected - marked for review")
                return
            
            # Position closed
            exit_price = result.get('exit_price')
            exit_type = result.get('exit_type', 'UNKNOWN')
            
            if exit_price is None:
                self.logger.warning(f"⚠️ {user_short}: Could not get exit price - marked for review")
                async with self.db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE open_positions SET status = 'needs_review' WHERE id = $1",
                        position['id']
                    )
                return
            
            # Verify exit type
            entry = position.get('avg_entry_price') or position['entry_fill_price']
            tp = position['target_tp']
            sl = position['target_sl']
            
            dist_to_tp = abs(exit_price - tp)
            dist_to_sl = abs(exit_price - sl)
            
            if dist_to_tp < dist_to_sl:
                exit_type = 'TP'
                self.logger.info(f"🎯 {user_short}: TP HIT on {position['symbol']} @ ${exit_price:.4f}")
            else:
                exit_type = 'SL'
                self.logger.info(f"🛑 {user_short}: SL HIT on {position['symbol']} @ ${exit_price:.4f}")
            
            # Record the trade closure
            await self.record_trade_close(position, exit_price, exit_type, datetime.utcnow())
            
            # Cancel remaining order
            try:
                remaining_order = position['sl_order_id'] if exit_type == 'TP' else position['tp_order_id']
                exchange.cancel_order(remaining_order, kraken_symbol)
                self.logger.info(f"   ✅ Cancelled remaining {('SL' if exit_type == 'TP' else 'TP')} order")
            except Exception as e:
                self.logger.debug(f"   Could not cancel order: {e}")
                
        except Exception as e:
            self.logger.error(f"❌ Error checking position {position['id']}: {e}")
            import traceback
            traceback.print_exc()
    
    async def check_all_positions(self):
        """Check all open positions and scan for new fills"""
        positions = await self.get_open_positions()
        
        if positions:
            self.logger.debug(f"📊 Checking {len(positions)} open positions...")
            
            for position in positions:
                await self.check_position(position)
                await asyncio.sleep(0.5)
        
        # Also scan fills for users without open_positions records
        # This catches manual trades or trades from other sources
        users = await self.get_active_users()
        users_with_positions = {p['user_id'] for p in positions}
        
        for user in users:
            if user['id'] not in users_with_positions:
                await self.scan_exchange_fills(user)
                await asyncio.sleep(0.5)
    
    async def run(self):
        """Main loop - checks positions every 60 seconds"""
        self.logger.info("=" * 60)
        self.logger.info("📊 POSITION MONITOR v2 STARTED")
        self.logger.info("=" * 60)
        self.logger.info(f"🔄 Check interval: {CHECK_INTERVAL_SECONDS} seconds")
        self.logger.info(f"💰 Fee tiers: Team=0%, VIP=5%, Standard=10%")
        self.logger.info(f"📅 30-Day Rolling Billing: Fees charged at cycle end, not per-trade")
        self.logger.info(f"📝 Position-based tracking: Aggregates fills → 1 trade per position")
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
                    users = await self.get_active_users()
                    self.logger.info(
                        f"💓 Monitor alive - Check #{check_count}, "
                        f"{len(positions)} positions, {len(users)} users"
                    )
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
    """Start the position monitor (call from main.py startup)"""
    await asyncio.sleep(40)
    
    logger.info("🚀 Starting position monitor v2...")
    
    monitor = PositionMonitor(db_pool)
    await monitor.run()
