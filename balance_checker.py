"""
Nike Rocket - Balance Checker (CONSOLIDATED v2)
================================================
Automated balance monitoring and deposit/withdrawal detection.

CONSOLIDATED VERSION - Uses follower_users as single source of truth
MERGED FROM: balance_checker_FIXED.py (all logic preserved)

Changes:
- Uses follower_users instead of portfolio_users
- Uses follower_user_id FK in portfolio_transactions
- Keeps all fallback logic and edge cases
- Keeps robust scheduler with proper stop() method

Author: Nike Rocket Team
Version: 3.1 (Consolidated + All Logic)
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
logger = logging.getLogger("balance_checker")

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
    
    CONSOLIDATED: Uses follower_users as single source of truth
    """
    
    def __init__(self, db_pool):
        self.db_pool = db_pool


    async def check_all_users(self):
        """
        Check balance for all users with portfolio tracking enabled
        
        CONSOLIDATED: Queries follower_users directly
        """
        try:
            async with self.db_pool.acquire() as conn:
                
                # Check if required tables exist (graceful check)
                table_check = await conn.fetchval("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables 
                        WHERE table_name = 'follower_users'
                    )
                """)
                
                if not table_check:
                    logger.info("✓ Tables not yet created")
                    return
                
                # CONSOLIDATED: Query follower_users where portfolio is initialized
                users = await conn.fetch("""
                    SELECT DISTINCT
                        fu.id,
                        fu.api_key,
                        fu.kraken_api_key_encrypted,
                        fu.kraken_api_secret_encrypted
                    FROM follower_users fu
                    WHERE fu.credentials_set = true
                      AND fu.kraken_api_key_encrypted IS NOT NULL
                      AND fu.kraken_api_secret_encrypted IS NOT NULL
                      AND fu.portfolio_initialized = true
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
                            logger.warning(f"⚠️  Could not decrypt credentials for {user['api_key'][:15]}...")
                            continue
                        
                        await self.check_user_balance(
                            user['id'],
                            user['api_key'],
                            kraken_key,
                            kraken_secret
                        )
                    except Exception as e:
                        logger.error(f"Error checking user {user['api_key'][:15]}...: {e}")
                        
                logger.info("✅ Balance check complete. Next check in 60 minutes")
                
        except Exception as e:
            logger.error(f"Error in check_all_users: {e}")
            import traceback
            traceback.print_exc()


    async def check_user_balance(
        self, 
        user_id: int,
        api_key: str, 
        kraken_api_key: str, 
        kraken_api_secret: str
    ):
        """
        Check a single user's balance and detect changes
        
        IMPROVED: Better logic to distinguish trading losses/fees from actual withdrawals
        ISSUE #3 FIX: Also checks exchange transaction history
        """
        
        # Get current Kraken balance
        current_balance = await self.get_kraken_balance(
            kraken_api_key, 
            kraken_api_secret
        )
        
        if current_balance is None:
            logger.warning(f"Could not get Kraken balance for {api_key[:15]}...")
            return
        
        # Calculate expected balance (includes trading P&L)
        expected_balance = await self.calculate_expected_balance(user_id, api_key)
        
        # Check for significant discrepancy
        # Use larger threshold ($5) to avoid false positives from:
        # - Trading fees
        # - Slippage
        # - Small unrealized P&L changes
        # - Funding fees
        discrepancy = abs(float(current_balance) - float(expected_balance))
        
        # Only flag as deposit/withdrawal if discrepancy is significant
        if discrepancy > 5.0:
            transaction_type = 'deposit' if current_balance > expected_balance else 'withdrawal'
            amount = abs(current_balance - expected_balance)
            
            logger.info(
                f"💰 Detected {transaction_type} for {api_key[:10]}...: "
                f"Expected ${expected_balance:.2f}, Actual ${current_balance:.2f}, "
                f"Difference: ${amount:.2f}"
            )
            
            # Record transaction
            await self.record_transaction(
                user_id=user_id,
                api_key=api_key,
                transaction_type=transaction_type,
                amount=amount
            )
        elif discrepancy > 1.0:
            # Small discrepancy - likely trading fees or slippage, not deposit/withdrawal
            logger.info(
                f"📊 User {api_key[:10]}...: Small discrepancy ${discrepancy:.2f} "
                f"(likely fees/slippage, not recording as transaction)"
            )
        else:
            logger.info(f"✅ User {api_key[:10]}...: Balance ${current_balance:.2f} matches expected")
        
        # ISSUE #3 FIX: Also check exchange transaction history
        # This catches transactions that balance-based detection might miss
        exchange_txs = await self.check_exchange_transactions(
            user_id, api_key, kraken_api_key, kraken_api_secret
        )
        if exchange_txs:
            logger.info(f"   Found {len(exchange_txs)} transactions via exchange API")
        
        # Update last known balance
        await self.update_last_known_balance(user_id, api_key, current_balance)
    
    
    # ==================== ISSUE #3 FIX: Check Exchange Transaction History ====================
    
    async def check_exchange_transactions(
        self, 
        user_id: int,
        api_key: str,
        kraken_api_key: str, 
        kraken_api_secret: str
    ) -> list:
        """
        ISSUE #3 FIX: Check Kraken's deposit/withdrawal history directly
        
        This catches transactions that the balance-based detection might miss:
        - Small deposits/withdrawals (< $5)
        - Deposits that coincide with trading losses
        - Withdrawals that coincide with trading profits
        
        Returns list of new transactions found
        """
        try:
            import ccxt
            
            exchange = ccxt.krakenfutures({
                'apiKey': kraken_api_key,
                'secret': kraken_api_secret,
                'enableRateLimit': True,
            })
            
            new_transactions = []
            
            # Fetch deposit history
            try:
                deposits = await asyncio.to_thread(exchange.fetch_deposits)
                
                async with self.db_pool.acquire() as conn:
                    for deposit in deposits:
                        # Check if we already recorded this
                        tx_id = deposit.get('txid') or deposit.get('id')
                        if not tx_id:
                            continue
                            
                        existing = await conn.fetchval("""
                            SELECT id FROM portfolio_transactions 
                            WHERE external_tx_id = $1
                        """, tx_id)
                        
                        if not existing and deposit.get('status') == 'ok':
                            amount = float(deposit.get('amount', 0))
                            if amount > 0:
                                # Record the deposit with both FKs for compatibility
                                await conn.execute("""
                                    INSERT INTO portfolio_transactions 
                                    (follower_user_id, user_id, transaction_type, amount, 
                                     detection_method, notes, external_tx_id)
                                    VALUES ($1, $2, 'deposit', $3, 'exchange_api', $4, $5)
                                """, user_id, api_key,
                                    amount, 
                                    f"Auto-detected via Kraken API: {deposit.get('currency', 'USD')}", 
                                    tx_id)
                                
                                new_transactions.append({
                                    'type': 'deposit',
                                    'amount': amount,
                                    'tx_id': tx_id
                                })
                                logger.info(f"   💰 Found deposit via API: ${amount:.2f}")
            except Exception as e:
                logger.debug(f"   Could not fetch deposits: {e}")
            
            # Fetch withdrawal history
            try:
                withdrawals = await asyncio.to_thread(exchange.fetch_withdrawals)
                
                async with self.db_pool.acquire() as conn:
                    for withdrawal in withdrawals:
                        tx_id = withdrawal.get('txid') or withdrawal.get('id')
                        if not tx_id:
                            continue
                            
                        existing = await conn.fetchval("""
                            SELECT id FROM portfolio_transactions 
                            WHERE external_tx_id = $1
                        """, tx_id)
                        
                        if not existing and withdrawal.get('status') == 'ok':
                            amount = float(withdrawal.get('amount', 0))
                            if amount > 0:
                                await conn.execute("""
                                    INSERT INTO portfolio_transactions 
                                    (follower_user_id, user_id, transaction_type, amount,
                                     detection_method, notes, external_tx_id)
                                    VALUES ($1, $2, 'withdrawal', $3, 'exchange_api', $4, $5)
                                """, user_id, api_key,
                                    amount,
                                    f"Auto-detected via Kraken API: {withdrawal.get('currency', 'USD')}",
                                    tx_id)
                                
                                new_transactions.append({
                                    'type': 'withdrawal',
                                    'amount': amount,
                                    'tx_id': tx_id
                                })
                                logger.info(f"   💸 Found withdrawal via API: ${amount:.2f}")
            except Exception as e:
                logger.debug(f"   Could not fetch withdrawals: {e}")
            
            return new_transactions
            
        except Exception as e:
            logger.error(f"Error checking exchange transactions: {e}")
            return []


    async def get_kraken_balance(
        self, 
        api_key: str, 
        api_secret: str
    ) -> Decimal:
        """
        Get current TOTAL EQUITY from Kraken FUTURES account using CCXT
        
        UPDATED: Now returns total equity (cash + unrealized P&L) instead of just cash balance
        This matches Kraken's "Total value" display
        """
        try:
            import ccxt
            
            logger.info("🔐 Fetching balance from Kraken Futures via CCXT...")
            
            # Initialize Kraken Futures exchange using CCXT
            exchange = ccxt.krakenfutures({
                'apiKey': api_key,
                'secret': api_secret,
                'enableRateLimit': True,
                'options': {
                    'defaultType': 'future',
                }
            })
            
            # Fetch balance synchronously in thread (CCXT is sync)
            balance = await asyncio.to_thread(exchange.fetch_balance)
            
            # Debug log
            logger.info(f"🔍 Balance response keys: {list(balance.keys())}")
            
            # Get USD cash balance
            usd_cash = 0
            if 'USD' in balance:
                usd_info = balance['USD']
                if isinstance(usd_info, dict):
                    usd_cash = float(usd_info.get('total', 0) or 0)
                else:
                    usd_cash = float(usd_info or 0)
            elif 'total' in balance:
                total_info = balance['total']
                if isinstance(total_info, dict):
                    usd_cash = float(total_info.get('USD', 0) or 0)
            
            # Fetch open positions to get unrealized P&L
            unrealized_pnl = 0
            try:
                positions = await asyncio.to_thread(exchange.fetch_positions)
                for pos in positions:
                    if pos and pos.get('contracts', 0) != 0:
                        pnl = float(pos.get('unrealizedPnl', 0) or 0)
                        unrealized_pnl += pnl
                        logger.info(f"📊 Position {pos.get('symbol')}: unrealized P&L ${pnl:.2f}")
            except Exception as e:
                logger.warning(f"⚠️ Could not fetch positions for unrealized P&L: {e}")
            
            # Total equity = cash + unrealized P&L
            total_equity = usd_cash + unrealized_pnl
            
            logger.info(f"✅ Kraken Futures: Cash ${usd_cash:.2f} + Unrealized ${unrealized_pnl:.2f} = Total ${total_equity:.2f}")
            return Decimal(str(total_equity))
            
        except Exception as e:
            logger.error(f"❌ Error fetching Kraken balance: {e}")
            import traceback
            traceback.print_exc()
            return None


    async def calculate_expected_balance(self, user_id: int, api_key: str) -> Decimal:
        """
        Calculate expected balance based on initial capital + deposits - withdrawals + trading P&L
        
        - Gets initial_capital from follower_users
        - Uses follower_user_id FK for transactions
        - Reads trading P&L from trades table
        """
        async with self.db_pool.acquire() as conn:
            
            # Try to get initial capital from follower_users first
            fu_info = await conn.fetchrow("""
                SELECT initial_capital
                FROM follower_users
                WHERE id = $1
            """, user_id)
            
            initial_capital = float(fu_info['initial_capital'] or 0) if fu_info and fu_info['initial_capital'] else 0.0
            
            # Get deposits from portfolio_transactions
            # Try new FK first, fall back to api_key
            deposits_result = await conn.fetchval("""
                SELECT COALESCE(SUM(amount), 0)
                FROM portfolio_transactions
                WHERE (follower_user_id = $1 OR user_id = $2) 
                  AND transaction_type = 'deposit'
            """, user_id, api_key)
            total_deposits = float(deposits_result or 0)
            
            # Get withdrawals from portfolio_transactions
            withdrawals_result = await conn.fetchval("""
                SELECT COALESCE(SUM(amount), 0)
                FROM portfolio_transactions
                WHERE (follower_user_id = $1 OR user_id = $2)
                  AND transaction_type = 'withdrawal'
            """, user_id, api_key)
            total_withdrawals = float(withdrawals_result or 0)
            
            # Get trading P&L from trades table (closed trades only)
            # This is where position monitor records actual trade results
            trading_pnl_result = await conn.fetchval("""
                SELECT COALESCE(SUM(profit_usd), 0)
                FROM trades
                WHERE user_id = $1 AND closed_at IS NOT NULL
            """, user_id)
            trading_pnl = float(trading_pnl_result or 0)
            
            # Calculate expected balance
            # Formula: Initial + Deposits - Withdrawals + Trading P&L
            expected = Decimal(str(initial_capital)) + \
                      Decimal(str(total_deposits)) - \
                      Decimal(str(total_withdrawals)) + \
                      Decimal(str(trading_pnl))
            
            logger.info(
                f"Expected balance for {api_key[:10]}...: ${expected:.2f} "
                f"(Initial: ${initial_capital:.2f}, "
                f"Deposits: ${total_deposits:.2f}, "
                f"Withdrawals: ${total_withdrawals:.2f}, "
                f"Trading P&L: ${trading_pnl:.2f})"
            )
            
            return expected


    async def record_transaction(
        self,
        user_id: int,
        api_key: str,
        transaction_type: str,
        amount: float
    ):
        """
        Record a deposit or withdrawal transaction
        
        CONSOLIDATED: Uses both follower_user_id (new) and user_id (legacy api_key) for compatibility
        """
        async with self.db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO portfolio_transactions (
                    follower_user_id,
                    user_id,
                    transaction_type,
                    amount,
                    detection_method,
                    notes
                ) VALUES ($1, $2, $3, $4, $5, $6)
            """,
                user_id,      # New proper FK
                api_key,      # Legacy column for backwards compat
                transaction_type,
                float(amount),
                'automatic',
                f'Auto-detected {transaction_type} via balance checker'
            )
            
            logger.info(f"✅ Recorded {transaction_type} of ${amount:.2f} for {api_key[:10]}...")


    async def update_last_known_balance(self, user_id: int, api_key: str, balance: Decimal):
        """
        Update the last known balance for a user
        """
        async with self.db_pool.acquire() as conn:
            # Update follower_users
            await conn.execute("""
                UPDATE follower_users 
                SET last_known_balance = $1
                WHERE id = $2
            """, float(balance), user_id)


    async def get_balance_summary(
        self, 
        api_key: str
    ) -> dict:
        """
        Get comprehensive balance summary for a user
        """
        async with self.db_pool.acquire() as conn:
            # Get user ID from follower_users
            user_row = await conn.fetchrow("""
                SELECT id, initial_capital, last_known_balance, portfolio_initialized
                FROM follower_users
                WHERE api_key = $1
            """, api_key)
            
            if not user_row:
                return None
            
            user_id = user_row['id']
            
            # Get initial capital from follower_users
            initial = float(user_row['initial_capital'] or 0)
            current = float(user_row['last_known_balance'] or 0)
            
            if initial == 0:
                return None
            
            # Get deposits (use both FKs for compatibility)
            deposits_result = await conn.fetchval("""
                SELECT COALESCE(SUM(amount), 0)
                FROM portfolio_transactions
                WHERE (follower_user_id = $1 OR user_id = $2)
                  AND transaction_type = 'deposit'
            """, user_id, api_key)
            deposits = float(deposits_result or 0)
            
            # Get withdrawals
            withdrawals_result = await conn.fetchval("""
                SELECT COALESCE(SUM(amount), 0)
                FROM portfolio_transactions
                WHERE (follower_user_id = $1 OR user_id = $2)
                  AND transaction_type = 'withdrawal'
            """, user_id, api_key)
            withdrawals = float(withdrawals_result or 0)
            
            # Get trading P&L from actual trades
            profit_result = await conn.fetchval("""
                SELECT COALESCE(SUM(profit_usd), 0)
                FROM trades
                WHERE user_id = $1 AND closed_at IS NOT NULL
            """, user_id)
            profit = float(profit_result or 0)
            
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
                'last_balance_check': None
            }


    async def get_transaction_history(
        self, 
        api_key: str, 
        limit: int = 50
    ) -> list:
        """
        Get transaction history for a user
        
        CONSOLIDATED: Uses both FKs for compatibility
        """
        async with self.db_pool.acquire() as conn:
            # Get user_id
            user_id = await conn.fetchval("""
                SELECT id FROM follower_users WHERE api_key = $1
            """, api_key)
            
            transactions = await conn.fetch("""
                SELECT 
                    transaction_type,
                    amount,
                    created_at,
                    detection_method,
                    notes
                FROM portfolio_transactions
                WHERE follower_user_id = $1 OR user_id = $2
                ORDER BY created_at DESC
                LIMIT $3
            """, user_id, api_key, limit)
            
            return [dict(t) for t in transactions]


class BalanceCheckerScheduler:
    """
    Scheduler for balance checker with startup delay
    
    ROBUST: Proper task management with stop() support
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
            logger.info("🛑 Balance checker stopped")


async def start_balance_checker(db_pool):
    """
    Start the balance checker scheduler
    
    Call from main.py startup:
    
    ```python
    from balance_checker import start_balance_checker, BalanceCheckerScheduler
    
    @app.on_event("startup")
    async def startup_event():
        ...
        scheduler = BalanceCheckerScheduler(db_pool)
        asyncio.create_task(scheduler.start())
    ```
    """
    scheduler = BalanceCheckerScheduler(db_pool)
    await scheduler.start()
