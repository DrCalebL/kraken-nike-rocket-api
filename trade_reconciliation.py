"""
Trade Reconciliation Script
============================
Reads actual closed trades from Kraken Futures and backfills them 
into portfolio_trades table for accurate P&L tracking.

Run this ONE TIME to sync historical trades that were missed.

Usage:
    python trade_reconciliation.py

Requires:
    - DATABASE_URL environment variable
    - User credentials in follower_users table
"""

import asyncio
import asyncpg
import ccxt
import os
from datetime import datetime, timedelta
from cryptography.fernet import Fernet

DATABASE_URL = os.getenv("DATABASE_URL")
CREDENTIALS_ENCRYPTION_KEY = os.getenv("CREDENTIALS_ENCRYPTION_KEY")


def decrypt_credential(encrypted_value: str) -> str:
    """Decrypt a stored credential"""
    if not CREDENTIALS_ENCRYPTION_KEY or not encrypted_value:
        return ""
    try:
        f = Fernet(CREDENTIALS_ENCRYPTION_KEY.encode() if isinstance(CREDENTIALS_ENCRYPTION_KEY, str) else CREDENTIALS_ENCRYPTION_KEY)
        return f.decrypt(encrypted_value.encode()).decode()
    except Exception as e:
        print(f"Decryption error: {e}")
        return ""


async def get_kraken_closed_trades(api_key: str, api_secret: str, since_days: int = 30):
    """
    Fetch closed trades from Kraken Futures
    
    Returns list of round-trip trades with P&L
    """
    try:
        exchange = ccxt.krakenfutures({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True
        })
        
        # Fetch recent trades (fills)
        since = int((datetime.utcnow() - timedelta(days=since_days)).timestamp() * 1000)
        
        all_trades = []
        
        # Fetch my trades from Kraken
        trades = exchange.fetch_my_trades(symbol=None, since=since, limit=100)
        all_trades.extend(trades)
        
        print(f"📊 Fetched {len(all_trades)} trades from Kraken")
        
        # Group trades by symbol and calculate round trips
        round_trips = []
        positions = {}  # Track open positions to match closes
        
        for trade in sorted(all_trades, key=lambda t: t['timestamp']):
            symbol = trade['symbol']
            side = trade['side']  # 'buy' or 'sell'
            amount = trade['amount']
            price = trade['price']
            timestamp = trade['timestamp']
            fee = trade.get('fee', {}).get('cost', 0) or 0
            
            trade_key = symbol
            
            if trade_key not in positions:
                positions[trade_key] = {
                    'entries': [],
                    'side': None,
                    'total_amount': 0,
                    'avg_entry': 0
                }
            
            pos = positions[trade_key]
            
            # Determine if this is opening or closing
            is_opening = (
                pos['total_amount'] == 0 or  # No existing position
                (pos['side'] == 'long' and side == 'buy') or  # Adding to long
                (pos['side'] == 'short' and side == 'sell')  # Adding to short
            )
            
            if is_opening:
                # Opening or adding to position
                if pos['total_amount'] == 0:
                    pos['side'] = 'long' if side == 'buy' else 'short'
                
                # Calculate new average entry
                total_cost = pos['avg_entry'] * pos['total_amount'] + price * amount
                pos['total_amount'] += amount
                pos['avg_entry'] = total_cost / pos['total_amount'] if pos['total_amount'] > 0 else 0
                
                pos['entries'].append({
                    'timestamp': timestamp,
                    'price': price,
                    'amount': amount,
                    'side': side
                })
                
                print(f"  📈 OPEN {pos['side'].upper()} {symbol}: {amount} @ ${price:.5f}")
                
            else:
                # Closing position
                close_amount = min(amount, pos['total_amount'])
                entry_price = pos['avg_entry']
                exit_price = price
                
                # Calculate P&L
                if pos['side'] == 'long':
                    pnl = (exit_price - entry_price) * close_amount
                else:  # short
                    pnl = (entry_price - exit_price) * close_amount
                
                pnl_pct = ((exit_price / entry_price) - 1) * 100 if pos['side'] == 'long' else ((entry_price / exit_price) - 1) * 100
                
                round_trips.append({
                    'symbol': symbol,
                    'side': pos['side'],
                    'entry_price': entry_price,
                    'exit_price': exit_price,
                    'quantity': close_amount,
                    'pnl_usd': pnl,
                    'pnl_pct': pnl_pct,
                    'entry_time': pos['entries'][0]['timestamp'] if pos['entries'] else timestamp,
                    'exit_time': timestamp,
                    'fee': fee
                })
                
                status = "🟢 WIN" if pnl > 0 else "🔴 LOSS"
                print(f"  {status} CLOSE {pos['side'].upper()} {symbol}: {close_amount} @ ${exit_price:.5f} | Entry: ${entry_price:.5f} | P&L: ${pnl:.2f} ({pnl_pct:.2f}%)")
                
                # Update remaining position
                pos['total_amount'] -= close_amount
                if pos['total_amount'] <= 0:
                    positions[trade_key] = {
                        'entries': [],
                        'side': None,
                        'total_amount': 0,
                        'avg_entry': 0
                    }
        
        return round_trips
        
    except Exception as e:
        print(f"❌ Error fetching Kraken trades: {e}")
        import traceback
        traceback.print_exc()
        return []


async def backfill_trades(conn, user_id: int, round_trips: list, fee_tier: str = 'standard'):
    """
    Insert round-trip trades into portfolio_trades table
    """
    fee_rates = {'team': 0.0, 'vip': 0.05, 'standard': 0.10}
    fee_rate = fee_rates.get(fee_tier, 0.10)
    
    inserted = 0
    total_pnl = 0
    total_fees = 0
    
    for trade in round_trips:
        # Check if trade already exists (avoid duplicates)
        existing = await conn.fetchrow("""
            SELECT id FROM portfolio_trades 
            WHERE user_id = $1 
            AND symbol = $2 
            AND ABS(EXTRACT(EPOCH FROM exit_time) - $3) < 60
        """, user_id, trade['symbol'], trade['exit_time'] / 1000)
        
        if existing:
            print(f"  ⏭️ Skipping duplicate: {trade['symbol']} @ {datetime.fromtimestamp(trade['exit_time']/1000)}")
            continue
        
        # Calculate fee (only on profits)
        fee_charged = max(0, trade['pnl_usd'] * fee_rate) if trade['pnl_usd'] > 0 else 0
        
        # Insert trade
        await conn.execute("""
            INSERT INTO portfolio_trades (
                user_id, symbol, side, entry_price, exit_price,
                quantity, profit_usd, profit_percent, exit_type,
                fee_charged, entry_time, exit_time, notes
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        """,
            user_id,
            trade['symbol'],
            trade['side'].upper(),
            trade['entry_price'],
            trade['exit_price'],
            trade['quantity'],
            trade['pnl_usd'],
            trade['pnl_pct'],
            'manual_close',
            fee_charged,
            datetime.fromtimestamp(trade['entry_time'] / 1000),
            datetime.fromtimestamp(trade['exit_time'] / 1000),
            'Reconciled from Kraken history'
        )
        
        inserted += 1
        total_pnl += trade['pnl_usd']
        total_fees += fee_charged
    
    # Update user totals
    if inserted > 0:
        await conn.execute("""
            UPDATE follower_users
            SET 
                total_profit = COALESCE(total_profit, 0) + $1,
                total_trades = COALESCE(total_trades, 0) + $2,
                total_fees = COALESCE(total_fees, 0) + $3,
                monthly_profit = COALESCE(monthly_profit, 0) + $1,
                monthly_trades = COALESCE(monthly_trades, 0) + $2,
                monthly_fee_due = COALESCE(monthly_fee_due, 0) + $3
            WHERE id = $4
        """, total_pnl, inserted, total_fees, user_id)
    
    return inserted, total_pnl, total_fees


async def reconcile_all_users():
    """
    Reconcile trades for all users with credentials
    """
    print("=" * 60)
    print("🔄 TRADE RECONCILIATION")
    print("=" * 60)
    
    pool = await asyncpg.create_pool(DATABASE_URL)
    
    async with pool.acquire() as conn:
        # Get all users with credentials
        users = await conn.fetch("""
            SELECT 
                id, email, api_key, fee_tier,
                kraken_api_key_encrypted, kraken_api_secret_encrypted
            FROM follower_users
            WHERE credentials_set = true
            AND kraken_api_key_encrypted IS NOT NULL
        """)
        
        print(f"📋 Found {len(users)} users with credentials")
        
        for user in users:
            print(f"\n{'='*40}")
            print(f"👤 User: {user['email']}")
            print(f"   Tier: {user['fee_tier'] or 'standard'}")
            
            # Decrypt credentials
            api_key = decrypt_credential(user['kraken_api_key_encrypted'])
            api_secret = decrypt_credential(user['kraken_api_secret_encrypted'])
            
            if not api_key or not api_secret:
                print("   ⚠️ Could not decrypt credentials, skipping")
                continue
            
            # Fetch trades from Kraken
            print(f"   📡 Fetching trades from Kraken (last 30 days)...")
            round_trips = await get_kraken_closed_trades(api_key, api_secret, since_days=30)
            
            if not round_trips:
                print("   📭 No closed trades found")
                continue
            
            print(f"   📊 Found {len(round_trips)} round-trip trades")
            
            # Backfill into database
            inserted, total_pnl, total_fees = await backfill_trades(
                conn, 
                user['id'], 
                round_trips,
                user['fee_tier'] or 'standard'
            )
            
            status = "🟢" if total_pnl >= 0 else "🔴"
            print(f"\n   ✅ Inserted {inserted} trades")
            print(f"   {status} Total P&L: ${total_pnl:.2f}")
            print(f"   💰 Fees due: ${total_fees:.2f}")
    
    await pool.close()
    print("\n" + "=" * 60)
    print("✅ RECONCILIATION COMPLETE")
    print("=" * 60)


async def reconcile_single_user(user_id: int):
    """
    Reconcile trades for a single user by ID
    """
    print(f"🔄 Reconciling user {user_id}...")
    
    pool = await asyncpg.create_pool(DATABASE_URL)
    
    async with pool.acquire() as conn:
        user = await conn.fetchrow("""
            SELECT 
                id, email, api_key, fee_tier,
                kraken_api_key_encrypted, kraken_api_secret_encrypted
            FROM follower_users
            WHERE id = $1
        """, user_id)
        
        if not user:
            print(f"❌ User {user_id} not found")
            return
        
        print(f"👤 User: {user['email']}")
        
        api_key = decrypt_credential(user['kraken_api_key_encrypted'])
        api_secret = decrypt_credential(user['kraken_api_secret_encrypted'])
        
        if not api_key or not api_secret:
            print("❌ Could not decrypt credentials")
            return
        
        round_trips = await get_kraken_closed_trades(api_key, api_secret, since_days=30)
        
        if not round_trips:
            print("📭 No closed trades found")
            return
        
        inserted, total_pnl, total_fees = await backfill_trades(
            conn, 
            user['id'], 
            round_trips,
            user['fee_tier'] or 'standard'
        )
        
        print(f"\n✅ Inserted {inserted} trades | P&L: ${total_pnl:.2f} | Fees: ${total_fees:.2f}")
    
    await pool.close()


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        # Reconcile specific user
        user_id = int(sys.argv[1])
        asyncio.run(reconcile_single_user(user_id))
    else:
        # Reconcile all users
        asyncio.run(reconcile_all_users())
