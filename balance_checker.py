"""
Nike Rocket - Balance Checker
==============================
Automated balance monitoring and deposit/withdrawal detection.

FULLY CORRECTED VERSION FOR KRAKEN FUTURES:
- Uses follower_users table (not follower_agents)
- Uses api_key column (not user_id)  
- Uses pnl_usd column (not pnl)
- Proper integer FK handling
- USES CCXT for Kraken Futures (same as trading algo)

Author: Nike Rocket Team
Updated: November 24, 2025
"""
import asyncio
import asyncpg
import os
from decimal import Decimal
from datetime import datetime, timedelta
import logging
from cryptography.fernet import Fernet

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Setup encryption
ENCRYPTION_KEY = os.getenv("CREDENTIALS_ENCRYPTION_KEY")
if ENCRYPTION_KEY:
    cipher = Fernet(ENCRYPTION_KEY.encode())
else:
    cipher = None


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
    """
    Monitors user Kraken balances and detects deposits/withdrawals
    
    CORRECTED: Uses follower_users table and api_key column
    UPDATED: Uses Kraken Futures API
    """
    
    def __init__(self, db_pool):
        self.db_pool = db_pool


    async def check_all_users(self):
        """
        Check balance for all users with active portfolio tracking
        
        CORRECTED: Queries follower_users table
        """
        try:
            async with self.db_pool.acquire() as conn:
                
                # Check if portfolio_trades table exists (CORRECTED: graceful check)
                table_check = await conn.fetchval("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables 
                        WHERE table_name = 'portfolio_trades'
                    )
                """)
                
                if not table_check:
                    logger.info("✓ Portfolio trades table not yet created")
                    return
                
                # CORRECTED: Query follower_users table with proper JOINs
                users = await conn.fetch("""
                    SELECT DISTINCT 
                        pu.id as user_id,
                        pu.api_key,
                        fu.kraken_api_key_encrypted,
                        fu.kraken_api_secret_encrypted
                    FROM portfolio_users pu
                    JOIN follower_users fu ON fu.api_key = pu.api_key
                    WHERE fu.credentials_set = true
                    AND fu.kraken_api_key_encrypted IS NOT NULL
                    AND fu.kraken_api_secret_encrypted IS NOT NULL
                """)
                
                if not users:
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
                            logger.warning(f"⚠️  Could not decrypt credentials for {user['api_key']}")
                            continue
                        
                        await self.check_user_balance(
                            user['api_key'],
                            kraken_key,
                            kraken_secret
                        )
                    except Exception as e:
                        logger.error(f"Error checking user {user['api_key']}: {e}")
                        
                logger.info("✅ Balance check complete. Next check in 60 minutes")
                
        except Exception as e:
            logger.error(f"Error in check_all_users: {e}")
            import traceback
            traceback.print_exc()


    async def check_user_balance(
        self, 
        api_key: str, 
        kraken_api_key: str, 
        kraken_api_secret: str
    ):
        """Check a single user's balance and detect changes"""
        
        # Get current Kraken balance
        current_balance = await self.get_kraken_balance(
            kraken_api_key, 
            kraken_api_secret
        )
        
        if current_balance is None:
            logger.warning(f"Could not get Kraken balance for {api_key}")
            return
        
        # Calculate expected balance
        expected_balance = await self.calculate_expected_balance(api_key)
        
        # Check for significant discrepancy (>$1)
        discrepancy = abs(float(current_balance) - float(expected_balance))
        
        if discrepancy > 1.0:
            transaction_type = 'deposit' if current_balance > expected_balance else 'withdrawal'
            amount = abs(current_balance - expected_balance)
            
            logger.info(
                f"✅ Detected {transaction_type} for {api_key}: "
                f"Expected ${expected_balance:.2f}, Actual ${current_balance:.2f}, "
                f"Difference: ${amount:.2f}"
            )
            
            # Record transaction
            await self.record_transaction(
                api_key=api_key,
                transaction_type=transaction_type,
                amount=amount
            )
        else:
            logger.info(f"✅ User {api_key[:10]}...: Balance ${current_balance:.2f} (no change)")
        
        # Update last known balance
        await self.update_last_known_balance(api_key, current_balance)


    async def get_kraken_balance(
        self, 
        api_key: str, 
        api_secret: str
    ) -> Decimal:
        """
        Get current balance from Kraken FUTURES account using CCXT
        
        UPDATED: Uses CCXT library (same as the working trading algo)
        """
        try:
            import ccxt
            
            # Initialize Kraken Futures exchange using CCXT
            exchange = ccxt.krakenfutures({
                'apiKey': api_key,
                'secret': api_secret,
                'enableRateLimit': True,
                'options': {
                    'defaultType': 'future',
                }
            })
            
            logger.info("🔐 Fetching balance from Kraken Futures via CCXT...")
            
            # Fetch balance (CCXT handles all authentication)
            balance = await asyncio.to_thread(exchange.fetch_balance)
            
            # DEBUG: Log the response structure
            logger.info(f"🔍 Balance response keys: {list(balance.keys())}")
            
            # Try multiple currency options (Kraken multi-collateral)
            for currency in ['USD', 'USDT', 'USDC']:
                # Check total balance
                if 'total' in balance and currency in balance['total']:
                    equity = float(balance['total'][currency])
                    if equity > 0:
                        logger.info(f"✅ Retrieved Kraken Futures balance: ${equity:.2f} {currency}")
                        return Decimal(str(equity))
            
            # Try free balance as fallback
            for currency in ['USD', 'USDT', 'USDC']:
                if 'free' in balance and currency in balance['free']:
                    equity = float(balance['free'][currency])
                    if equity > 0:
                        logger.info(f"✅ Retrieved Kraken Futures available: ${equity:.2f} {currency}")
                        return Decimal(str(equity))
            
            logger.warning("⚠️ No balance found in USD, USDT, or USDC")
            logger.warning("💡 Make sure you have funds in your Kraken Futures wallet")
            return Decimal('0')
                
        except Exception as e:
            logger.error(f"Error getting Kraken Futures balance: {e}")
            import traceback
            traceback.print_exc()
            return None


    async def calculate_expected_balance(self, api_key: str) -> Decimal:
        """
        Calculate expected balance based on initial capital + deposits - withdrawals + trades
        
        CORRECTED: Uses api_key and pnl_usd column
        """
        async with self.db_pool.acquire() as conn:
            
            # Get last balance check time
            last_check = await conn.fetchval("""
                SELECT last_balance_check 
                FROM portfolio_users 
                WHERE api_key = $1
            """, api_key)
            
            # Check if portfolio_trades table exists
            table_exists = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'portfolio_trades'
                )
            """)
            
            if not table_exists:
                logger.info("Portfolio trades table doesn't exist yet")
                # Just return initial capital + deposits - withdrawals
                result = await conn.fetchrow("""
                    SELECT 
                        initial_capital,
                        COALESCE(
                            (SELECT SUM(amount) FROM portfolio_transactions 
                             WHERE user_id = $1 AND transaction_type = 'deposit'),
                            0
                        ) as total_deposits,
                        COALESCE(
                            (SELECT SUM(amount) FROM portfolio_transactions 
                             WHERE user_id = $1 AND transaction_type = 'withdrawal'),
                            0
                        ) as total_withdrawals
                    FROM portfolio_users
                    WHERE api_key = $1
                """, api_key)
                
                if not result:
                    return Decimal('0')
                
                expected = Decimal(str(result['initial_capital'])) + \
                          Decimal(str(result['total_deposits'])) - \
                          Decimal(str(result['total_withdrawals']))
                
                logger.info(f"Expected balance for {api_key[:10]}...: ${expected:.2f} (no trades yet)")
                return expected
            
            # CORRECTED: Use pnl_usd column and proper JOINs
            result = await conn.fetchrow("""
                SELECT 
                    pu.initial_capital,
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
                        (SELECT SUM(pt.pnl_usd) FROM portfolio_trades pt
                         JOIN portfolio_users pu2 ON pt.user_id = pu2.id
                         WHERE pu2.api_key = $1),
                        0
                    ) as total_profit
                FROM portfolio_users pu
                WHERE pu.api_key = $1
            """, api_key)
            
            if not result:
                return Decimal('0')
            
            expected = Decimal(str(result['initial_capital'])) + \
                      Decimal(str(result['total_deposits'])) - \
                      Decimal(str(result['total_withdrawals'])) + \
                      Decimal(str(result['total_profit']))
            
            logger.info(
                f"Expected balance for {api_key[:10]}...: ${expected:.2f} "
                f"(IC: ${result['initial_capital']:.2f}, "
                f"Deposits: ${result['total_deposits']:.2f}, "
                f"Withdrawals: ${result['total_withdrawals']:.2f}, "
                f"Profit: ${result['total_profit']:.2f})"
            )
            
            return expected


    async def record_transaction(
        self,
        api_key: str,
        transaction_type: str,
        amount: Decimal
    ):
        """Record a deposit or withdrawal transaction"""
        async with self.db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO portfolio_transactions (
                    user_id,
                    transaction_type,
                    amount,
                    detection_method,
                    notes
                ) VALUES ($1, $2, $3, 'automatic', $4)
            """,
                api_key,
                transaction_type,
                float(amount),
                f'Auto-detected {transaction_type} via balance checker'
            )
            
            # Update totals in portfolio_users
            if transaction_type == 'deposit':
                await conn.execute("""
                    UPDATE portfolio_users 
                    SET total_deposits = total_deposits + $1
                    WHERE api_key = $2
                """, float(amount), api_key)
            else:  # withdrawal
                await conn.execute("""
                    UPDATE portfolio_users 
                    SET total_withdrawals = total_withdrawals + $1
                    WHERE api_key = $2
                """, float(amount), api_key)
            
            logger.info(f"✅ Recorded {transaction_type} of ${amount:.2f} for {api_key[:10]}...")


    async def update_last_known_balance(self, api_key: str, balance: Decimal):
        """Update the last known balance for a user"""
        async with self.db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE portfolio_users 
                SET last_known_balance = $1,
                    last_balance_check = CURRENT_TIMESTAMP
                WHERE api_key = $2
            """, float(balance), api_key)


    async def get_balance_summary(
        self, 
        api_key: str
    ) -> dict:
        """
        Get comprehensive balance summary for a user
        
        CORRECTED: Uses api_key and pnl_usd column
        """
        async with self.db_pool.acquire() as conn:
            # Check if portfolio_trades table exists
            trades_exist = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'portfolio_trades'
                )
            """)
            
            if not trades_exist:
                # Return basic summary without trades
                result = await conn.fetchrow("""
                    SELECT 
                        initial_capital,
                        COALESCE(total_deposits, 0) as total_deposits,
                        COALESCE(total_withdrawals, 0) as total_withdrawals,
                        last_known_balance as current_value,
                        last_balance_check
                    FROM portfolio_users
                    WHERE api_key = $1
                """, api_key)
                
                if not result:
                    return None
                
                initial = float(result['initial_capital'] or 0)
                deposits = float(result['total_deposits'] or 0)
                withdrawals = float(result['total_withdrawals'] or 0)
                current = float(result['current_value'] or initial)
                
                net_deposits = deposits - withdrawals
                total_capital = initial + net_deposits
                total_profit = current - total_capital
                
                return {
                    'initial_capital': initial,
                    'total_deposits': deposits,
                    'total_withdrawals': withdrawals,
                    'net_deposits': net_deposits,
                    'total_capital': total_capital,
                    'total_profit': total_profit,
                    'current_value': current,
                    'roi_on_initial': (total_profit / initial * 100) if initial > 0 else 0,
                    'roi_on_total': (total_profit / total_capital * 100) if total_capital > 0 else 0,
                    'last_balance_check': result['last_balance_check']
                }
            
            # CORRECTED: Use pnl_usd column with proper JOINs
            result = await conn.fetchrow("""
                SELECT 
                    pu.initial_capital,
                    COALESCE(pu.total_deposits, 0) as total_deposits,
                    COALESCE(pu.total_withdrawals, 0) as total_withdrawals,
                    COALESCE(
                        (SELECT SUM(pt.pnl_usd) FROM portfolio_trades pt
                         WHERE pt.user_id = pu.id),
                        0
                    ) as total_profit,
                    pu.last_known_balance as current_value,
                    pu.last_balance_check
                FROM portfolio_users pu
                WHERE pu.api_key = $1
            """, api_key)
            
            if not result:
                return None
            
            initial = float(result['initial_capital'] or 0)
            deposits = float(result['total_deposits'] or 0)
            withdrawals = float(result['total_withdrawals'] or 0)
            profit = float(result['total_profit'] or 0)
            current = float(result['current_value'] or initial)
            
            # If current_value is 0 or None, recalculate from components
            if current == 0:
                current = initial + deposits - withdrawals + profit
            
            net_deposits = deposits - withdrawals
            total_capital = initial + net_deposits
            
            return {
                'initial_capital': initial,
                'total_deposits': deposits,
                'total_withdrawals': withdrawals,
                'net_deposits': net_deposits,
                'total_capital': total_capital,
                'total_profit': profit,
                'current_value': current,
                'roi_on_initial': (profit / initial * 100) if initial > 0 else 0,
                'roi_on_total': (profit / total_capital * 100) if total_capital > 0 else 0,
                'last_balance_check': result['last_balance_check']
            }


    async def get_transaction_history(
        self, 
        api_key: str, 
        limit: int = 50
    ) -> list:
        """Get transaction history for a user"""
        async with self.db_pool.acquire() as conn:
            transactions = await conn.fetch("""
                SELECT 
                    transaction_type,
                    amount,
                    detected_at,
                    detection_method,
                    notes
                FROM portfolio_transactions
                WHERE user_id = $1
                ORDER BY detected_at DESC
                LIMIT $2
            """, api_key, limit)
            
            return [dict(t) for t in transactions]


class BalanceCheckerScheduler:
    """
    Scheduler for balance checker with startup delay
    
    CORRECTED: 30-second startup delay to prevent race conditions
    """
    
    def __init__(self, db_pool, check_interval_minutes: int = 60, startup_delay_seconds: int = 30):
        self.db_pool = db_pool
        self.check_interval = check_interval_minutes * 60  # Convert to seconds
        self.startup_delay = startup_delay_seconds
        self.checker = BalanceChecker(db_pool)
        self.task = None
    
    async def start(self):
        """Start the balance checker with initial delay"""
        logger.info(
            f"⏳ Balance checker starting in {self.startup_delay} seconds "
            f"(allowing database initialization to complete)..."
        )
        
        # Wait for database to be ready
        await asyncio.sleep(self.startup_delay)
        
        logger.info(f"✅ Balance checker started (checks every {self.check_interval // 60} minutes)")
        
        self.task = asyncio.create_task(self._run())
    
    async def _run(self):
        """Run balance checks in a loop"""
        while True:
            try:
                await self.checker.check_all_users()
            except Exception as e:
                logger.error(f"Error in balance check loop: {e}")
                import traceback
                traceback.print_exc()
            
            await asyncio.sleep(self.check_interval)
    
    async def stop(self):
        """Stop the balance checker"""
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
