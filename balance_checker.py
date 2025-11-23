"""
Automatic Balance Checker - Detects Deposits & Withdrawals
===========================================================

FULLY CORRECTED VERSION - Proper column names for all tables

Key fixes:
1. follower_users table (not follower_agents)
2. Encrypted credential decryption
3. portfolio_users uses 'api_key' column (not 'user_id')
4. portfolio_trades uses integer 'user_id' FK to portfolio_users.id
5. portfolio_transactions uses string 'user_id' (api_key)

Author: Nike Rocket Team
Updated: November 23, 2025 (FULLY CORRECTED)
"""

import asyncio
import asyncpg
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, List
import logging
import os
from cryptography.fernet import Fernet

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Setup encryption for decrypting Kraken credentials
ENCRYPTION_KEY = os.getenv("CREDENTIALS_ENCRYPTION_KEY")
if ENCRYPTION_KEY:
    cipher = Fernet(ENCRYPTION_KEY.encode())
else:
    cipher = None
    logger.warning("⚠️ CREDENTIALS_ENCRYPTION_KEY not set - balance checking will fail!")


async def table_exists(conn: asyncpg.Connection, table_name: str) -> bool:
    """Check if a table exists in the database"""
    try:
        result = await conn.fetchval("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = $1
            )
        """, table_name)
        return result
    except Exception as e:
        logger.error(f"Error checking if table {table_name} exists: {e}")
        return False


def decrypt_credentials(encrypted_key: str, encrypted_secret: str) -> tuple:
    """Decrypt Kraken API credentials"""
    if not cipher or not encrypted_key or not encrypted_secret:
        return None, None
    
    try:
        api_key = cipher.decrypt(encrypted_key.encode()).decode()
        api_secret = cipher.decrypt(encrypted_secret.encode()).decode()
        return api_key, api_secret
    except Exception as e:
        logger.error(f"Error decrypting credentials: {e}")
        return None, None


class BalanceChecker:
    """Automatically detects deposits and withdrawals"""
    
    def __init__(self, db_pool: asyncpg.Pool, threshold: float = 10.0):
        """
        Args:
            db_pool: PostgreSQL connection pool
            threshold: Minimum balance difference to detect (default $10)
        """
        self.db_pool = db_pool
        self.threshold = Decimal(str(threshold))
    
    async def check_all_users(self):
        """
        Check balance for all users with active agents
        
        FULLY CORRECTED with proper table and column names
        """
        async with self.db_pool.acquire() as conn:
            # Check correct tables
            tables_to_check = ['follower_users', 'portfolio_users']
            
            for table in tables_to_check:
                if not await table_exists(conn, table):
                    logger.warning(
                        f"⚠️ Table '{table}' does not exist yet. "
                        f"Skipping balance check. Will retry on next interval."
                    )
                    return  # Exit gracefully
            
            # Check encryption key
            if not cipher:
                logger.error("❌ Cannot check balances - CREDENTIALS_ENCRYPTION_KEY not set!")
                return
            
            try:
                # CORRECTED QUERY with proper column names
                users = await conn.fetch("""
                    SELECT DISTINCT 
                        fu.api_key as user_api_key,
                        fu.kraken_api_key_encrypted,
                        fu.kraken_api_secret_encrypted
                    FROM follower_users fu
                    JOIN portfolio_users pu ON pu.api_key = fu.api_key
                    WHERE fu.agent_active = true
                    AND fu.credentials_set = true
                    AND pu.initial_capital > 0
                """)
                
                if len(users) == 0:
                    logger.info("✓ No active users to check balance for")
                    return
                
                logger.info(f"📊 Checking balance for {len(users)} active users...")
                
                for user in users:
                    try:
                        # Decrypt credentials
                        kraken_key, kraken_secret = decrypt_credentials(
                            user['kraken_api_key_encrypted'],
                            user['kraken_api_secret_encrypted']
                        )
                        
                        if not kraken_key or not kraken_secret:
                            logger.warning(f"⚠️ Could not decrypt credentials for {user['user_api_key']}")
                            continue
                        
                        # Check balance
                        await self.check_user_balance(
                            user['user_api_key'],
                            kraken_key,
                            kraken_secret
                        )
                    except Exception as e:
                        logger.error(f"❌ Error checking user {user['user_api_key']}: {e}")
                        
            except Exception as e:
                logger.error(f"❌ Error in check_all_users: {e}")
                import traceback
                traceback.print_exc()
    
    async def check_user_balance(
        self, 
        user_api_key: str, 
        kraken_api_key: str, 
        kraken_api_secret: str
    ):
        """Check balance for a specific user"""
        
        # 1. Get current Kraken balance
        current_balance = await self.get_kraken_balance(
            kraken_api_key, 
            kraken_api_secret
        )
        
        if current_balance is None:
            logger.warning(f"⚠️ Could not get Kraken balance for {user_api_key}")
            return
        
        # 2. Get expected balance from database
        expected_balance = await self.calculate_expected_balance(user_api_key)
        
        # 3. Calculate difference
        difference = current_balance - expected_balance
        
        # 4. Check if significant difference
        if abs(difference) > self.threshold:
            transaction_type = 'deposit' if difference > 0 else 'withdrawal'
            amount = abs(difference)
            
            logger.info(
                f"✅ Detected {transaction_type} for {user_api_key}: "
                f"${amount:.2f} (Current: ${current_balance:.2f}, "
                f"Expected: ${expected_balance:.2f})"
            )
            
            # 5. Record transaction
            await self.record_transaction(
                user_api_key=user_api_key,
                transaction_type=transaction_type,
                amount=amount,
                balance_before=expected_balance,
                balance_after=current_balance
            )
        
        # 6. Update last known balance
        await self.update_last_known_balance(user_api_key, current_balance)
    
    async def get_kraken_balance(
        self, 
        api_key: str, 
        api_secret: str
    ) -> Optional[Decimal]:
        """Get USDT balance from Kraken"""
        try:
            import krakenex
            from pykrakenapi import KrakenAPI
            
            # Initialize Kraken API
            kraken = krakenex.API(key=api_key, secret=api_secret)
            k = KrakenAPI(kraken)
            
            # Get balance
            balance = k.get_account_balance()
            
            # Get USDT balance
            usdt_balance = 0
            for currency in ['USDT', 'ZUSD', 'USD']:
                if currency in balance.index:
                    usdt_balance = float(balance.loc[currency]['vol'])
                    break
            
            return Decimal(str(usdt_balance))
            
        except Exception as e:
            logger.error(f"❌ Error getting Kraken balance: {e}")
            return None
    
    async def calculate_expected_balance(self, user_api_key: str) -> Decimal:
        """Calculate expected balance based on last known balance + trades"""
        async with self.db_pool.acquire() as conn:
            # Check if portfolio_users table exists
            if not await table_exists(conn, 'portfolio_users'):
                logger.warning("⚠️ portfolio_users table doesn't exist")
                return Decimal('0')
            
            # CORRECTED: Use api_key column
            user_data = await conn.fetchrow("""
                SELECT last_known_balance, last_balance_check, initial_capital
                FROM portfolio_users
                WHERE api_key = $1
            """, user_api_key)
            
            if not user_data:
                return Decimal('0')
            
            last_balance = Decimal(str(user_data['last_known_balance'] or 0))
            last_check = user_data['last_balance_check']
            
            # Check if portfolio_trades table exists
            if not await table_exists(conn, 'portfolio_trades'):
                return last_balance
            
            # CORRECTED: Join through portfolio_users since portfolio_trades.user_id is integer FK
            if last_check:
                trades = await conn.fetch("""
                    SELECT pt.pnl 
                    FROM portfolio_trades pt
                    JOIN portfolio_users pu ON pt.user_id = pu.id
                    WHERE pu.api_key = $1 
                    AND pt.exit_time > $2
                """, user_api_key, last_check)
            else:
                trades = await conn.fetch("""
                    SELECT pt.pnl 
                    FROM portfolio_trades pt
                    JOIN portfolio_users pu ON pt.user_id = pu.id
                    WHERE pu.api_key = $1
                """, user_api_key)
            
            # Add trade profits to last balance
            trade_pnl = sum(Decimal(str(trade['pnl'] or 0)) for trade in trades)
            expected = last_balance + trade_pnl
            
            return expected
    
    async def record_transaction(
        self,
        user_api_key: str,
        transaction_type: str,
        amount: float,
        balance_before: Decimal,
        balance_after: Decimal
    ):
        """Record deposit or withdrawal transaction"""
        async with self.db_pool.acquire() as conn:
            # Check if table exists
            if not await table_exists(conn, 'portfolio_transactions'):
                logger.warning("⚠️ portfolio_transactions table doesn't exist - cannot record transaction")
                return
            
            # portfolio_transactions.user_id is the api_key string
            await conn.execute("""
                INSERT INTO portfolio_transactions (
                    user_id, 
                    transaction_type, 
                    amount, 
                    balance_before, 
                    balance_after,
                    detection_method,
                    notes
                ) VALUES ($1, $2, $3, $4, $5, 'automatic', $6)
            """, 
                user_api_key, 
                transaction_type, 
                amount,
                float(balance_before),
                float(balance_after),
                f'Auto-detected via balance checker on {datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")} UTC'
            )
    
    async def update_last_known_balance(self, user_api_key: str, balance: Decimal):
        """Update user's last known balance and check time"""
        async with self.db_pool.acquire() as conn:
            # CORRECTED: Use api_key column
            await conn.execute("""
                UPDATE portfolio_users
                SET last_known_balance = $2,
                    last_balance_check = CURRENT_TIMESTAMP
                WHERE api_key = $1
            """, user_api_key, float(balance))
    
    async def get_transaction_history(
        self, 
        user_api_key: str, 
        limit: int = 50
    ) -> List[Dict]:
        """Get transaction history for a user"""
        async with self.db_pool.acquire() as conn:
            # Check if table exists
            if not await table_exists(conn, 'portfolio_transactions'):
                return []
            
            # portfolio_transactions.user_id is api_key string
            transactions = await conn.fetch("""
                SELECT 
                    transaction_type,
                    amount,
                    balance_before,
                    balance_after,
                    detection_method,
                    notes,
                    created_at
                FROM portfolio_transactions
                WHERE user_id = $1
                ORDER BY created_at DESC
                LIMIT $2
            """, user_api_key, limit)
            
            return [dict(tx) for tx in transactions]
    
    async def get_balance_summary(self, user_api_key: str) -> Dict:
        """Get balance summary for a user - WITH ZERO DIVISION PROTECTION"""
        async with self.db_pool.acquire() as conn:
            # Check if tables exist
            if not await table_exists(conn, 'portfolio_users'):
                return {}
            
            # CORRECTED: Use api_key column and JOIN for portfolio_trades
            summary = await conn.fetchrow("""
                SELECT 
                    pu.initial_capital,
                    pu.last_known_balance,
                    pu.last_balance_check,
                    COALESCE(
                        (SELECT SUM(amount) FROM portfolio_transactions 
                         WHERE user_id = $1 AND transaction_type = 'deposit'),
                        0
                    ) as total_deposits,
                    COALESCE(
                        (SELECT SUM(amount) FROM portfolio_transactions 
                         WHERE user_id = $1 AND transaction_type = 'withdrawal'),
                        0
                    ) as total_withdrawals,
                    COALESCE(
                        (SELECT SUM(pt.pnl) FROM portfolio_trades pt
                         WHERE pt.user_id = pu.id),
                        0
                    ) as total_profit
                FROM portfolio_users pu
                WHERE pu.api_key = $1
            """, user_api_key)
            
            if not summary:
                return {}
            
            initial = Decimal(str(summary['initial_capital'] or 0))
            deposits = Decimal(str(summary['total_deposits'] or 0))
            withdrawals = Decimal(str(summary['total_withdrawals'] or 0))
            profit = Decimal(str(summary['total_profit'] or 0))
            
            # SAFETY CHECK: Ensure initial capital is never zero
            if initial <= 0:
                logger.warning(
                    f"User {user_api_key} has invalid initial_capital: {initial}. "
                    f"Setting to 1 to prevent division by zero."
                )
                initial = Decimal('1')
            
            net_deposits = deposits - withdrawals
            total_capital = initial + net_deposits
            
            # SAFETY CHECK: Ensure total capital is never zero
            if total_capital <= 0:
                logger.warning(
                    f"User {user_api_key} has invalid total_capital: {total_capital}. "
                    f"Setting to initial capital."
                )
                total_capital = initial
            
            current_value = total_capital + profit
            
            # Calculate ROI with zero division protection
            try:
                roi_on_initial = float((profit / initial * 100)) if initial > 0 else 0.0
            except (ZeroDivisionError, InvalidOperation):
                roi_on_initial = 0.0
            
            try:
                roi_on_total = float((profit / total_capital * 100)) if total_capital > 0 else 0.0
            except (ZeroDivisionError, InvalidOperation):
                roi_on_total = 0.0
            
            # Cap ROI at reasonable values
            roi_on_initial = min(max(roi_on_initial, -10000), 10000)
            roi_on_total = min(max(roi_on_total, -10000), 10000)
            
            return {
                'initial_capital': float(initial),
                'total_deposits': float(deposits),
                'total_withdrawals': float(withdrawals),
                'net_deposits': float(net_deposits),
                'total_capital': float(total_capital),
                'total_profit': float(profit),
                'current_value': float(current_value),
                'roi_on_initial': roi_on_initial,
                'roi_on_total': roi_on_total,
                'last_balance_check': summary['last_balance_check']
            }


# ============================================================================
# SCHEDULER - Run balance checks periodically
# ============================================================================

class BalanceCheckerScheduler:
    """Run balance checks on a schedule"""
    
    def __init__(self, db_pool: asyncpg.Pool, interval_minutes: int = 60, startup_delay_seconds: int = 30):
        """
        Args:
            db_pool: PostgreSQL connection pool
            interval_minutes: How often to check (default 60 minutes)
            startup_delay_seconds: Wait time before first check (default 30s)
        """
        self.checker = BalanceChecker(db_pool)
        self.interval_minutes = interval_minutes
        self.startup_delay_seconds = startup_delay_seconds
        self.running = False
    
    async def start(self):
        """Start the scheduler with startup delay"""
        self.running = True
        
        # Wait for database initialization
        logger.info(
            f"⏳ Balance checker starting in {self.startup_delay_seconds} seconds "
            f"(allowing database initialization to complete)..."
        )
        await asyncio.sleep(self.startup_delay_seconds)
        
        logger.info(
            f"✅ Balance checker started "
            f"(checks every {self.interval_minutes} minutes)"
        )
        
        while self.running:
            try:
                await self.checker.check_all_users()
                logger.info(
                    f"✅ Balance check complete. "
                    f"Next check in {self.interval_minutes} minutes"
                )
            except Exception as e:
                logger.error(f"❌ Error in balance check: {e}")
                import traceback
                traceback.print_exc()
            
            # Wait for next check
            await asyncio.sleep(self.interval_minutes * 60)
    
    def stop(self):
        """Stop the scheduler"""
        self.running = False
        logger.info("⏸️ Balance checker stopped")


if __name__ == "__main__":
    print("Balance Checker Module - Import this into your main application")
