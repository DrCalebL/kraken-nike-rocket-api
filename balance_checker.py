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
- Added error logging to error_logs table for admin dashboard

Author: Nike Rocket Team
Version: 3.2 (Consolidated + Error Logging)
"""
import asyncio
import asyncpg
import os
import json
from decimal import Decimal
from datetime import datetime, timedelta
import logging
from cryptography.fernet import Fernet
from typing import Optional, Dict

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("balance_checker")


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
                        await log_error_to_db(
                            self.db_pool, user['api_key'], "BALANCE_CHECK_USER_ERROR",
                            str(e), {"user_id": user['id'], "function": "check_all_users"}
                        )
                        
                logger.info("✅ Balance check complete. Next check in 60 minutes")
                
        except Exception as e:
            logger.error(f"Error in check_all_users: {e}")
            import traceback
            traceback.print_exc()
            await log_error_to_db(
                self.db_pool, "system", "BALANCE_CHECK_ALL_ERROR",
                str(e), {"function": "check_all_users", "traceback": traceback.format_exc()[:500]}
            )


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
        
        CRITICAL FIX: 
        - Uses CASH BALANCE for deposit/withdrawal detection (excludes unrealized P&L)
        - Uses TOTAL EQUITY for dashboard display (includes unrealized P&L)
        This prevents false deposit/withdrawal records when unrealized P&L changes
        """
        
        # Get current Kraken balance (returns both cash and equity)
        balance_info = await self.get_kraken_balance(
            kraken_api_key, 
            kraken_api_secret
        )
        
        if balance_info is None:
            logger.warning(f"Could not get Kraken balance for {api_key[:15]}...")
            return
        
        cash_balance = balance_info['cash_balance']
        total_equity = balance_info['total_equity']
        
        # Calculate expected balance (includes trading P&L)
        expected_balance = await self.calculate_expected_balance(user_id, api_key)
        
        # Check for discrepancy using CASH BALANCE (not total equity)
        # This prevents false detection from unrealized P&L changes
        discrepancy = abs(float(cash_balance) - float(expected_balance))
        
        # Record any discrepancy > $0.01 (skip dust)
        if discrepancy > 0.01:
            if cash_balance > expected_balance:
                # More money than expected = deposit
                transaction_type = 'deposit'
                amount = float(cash_balance) - float(expected_balance)
                logger.info(
                    f"💰 Detected deposit for {api_key[:10]}...: "
                    f"Expected ${expected_balance:.2f}, Cash ${cash_balance:.2f}, "
                    f"+${amount:.2f}"
                )
            else:
                # Less money than expected = fees, funding, or withdrawal
                # We cannot distinguish between these via API
                transaction_type = 'fees_funding_withdrawal'
                amount = float(expected_balance) - float(cash_balance)
                logger.info(
                    f"💸 Detected fees/funding/withdrawal for {api_key[:10]}...: "
                    f"Expected ${expected_balance:.2f}, Cash ${cash_balance:.2f}, "
                    f"-${amount:.2f}"
                )
            
            # Record transaction
            await self.record_transaction(
                user_id=user_id,
                api_key=api_key,
                transaction_type=transaction_type,
                amount=amount
            )
        else:
            logger.info(f"✅ User {api_key[:10]}...: Cash ${cash_balance:.2f} matches expected")
        
        # ISSUE #3 FIX: Also check exchange transaction history
        # This catches transactions that balance-based detection might miss
        exchange_txs = await self.check_exchange_transactions(
            user_id, api_key, kraken_api_key, kraken_api_secret
        )
        if exchange_txs:
            logger.info(f"   Found {len(exchange_txs)} transactions via exchange API")
        
        # Update last known balance with TOTAL EQUITY (for dashboard display)
        await self.update_last_known_balance(user_id, api_key, total_equity)
        logger.info(f"   📊 Updated last_known_balance to ${total_equity:.2f} (total equity)")
    
    
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
                logger.info(f"   ℹ️ Could not fetch deposits from Kraken API: {e}")
            
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
                logger.info(f"   ℹ️ Could not fetch withdrawals from Kraken API: {e}")
            
            return new_transactions
            
        except Exception as e:
            logger.error(f"Error checking exchange transactions: {e}")
            await log_error_to_db(
                self.db_pool, api_key, "EXCHANGE_TX_CHECK_ERROR",
                str(e), {"user_id": user_id, "function": "check_exchange_transactions"}
            )
            return []


    async def get_kraken_balance(
        self, 
        api_key: str, 
        api_secret: str
    ) -> dict:
        """
        Get current balance info from Kraken FUTURES account using CCXT
        
        Returns dict with:
        - cash_balance: USD cash only (for deposit/withdrawal detection)
        - total_equity: Cash + unrealized P&L (for dashboard display)
        
        IMPORTANT: 
        - Use cash_balance for comparing expected vs actual (deposit/withdrawal detection)
        - Use total_equity for dashboard display (matches Kraken's "Total value")
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
            
            total_equity = None
            usd_cash = 0
            
            # Get USD cash balance first
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
            
            logger.info(f"💵 Cash balance: ${usd_cash:.2f}")
            
            # Try to get portfolio value from raw info (includes unrealized P&L)
            if balance.get('info'):
                info = balance['info']
                
                if isinstance(info, dict) and 'accounts' in info:
                    # Check flex account for portfolio value
                    flex = info['accounts'].get('flex', {})
                    if flex and isinstance(flex, dict):
                        # Try various field names for portfolio value
                        pv = (
                            flex.get('portfolioValue') or
                            flex.get('pv') or
                            flex.get('balanceValue') or
                            flex.get('balance_value') or
                            flex.get('equity')
                        )
                        if pv and float(pv) > 0:
                            total_equity = float(pv)
                            logger.info(f"📊 Portfolio value (flex): ${total_equity:.2f}")
            
            # If no portfolio value found, use cash balance
            if total_equity is None or total_equity == 0:
                total_equity = usd_cash
                logger.info(f"⚠️ No portfolio value found, using cash balance")
            
            logger.info(f"✅ Kraken Futures: Cash ${usd_cash:.2f}, Total Equity ${total_equity:.2f}")
            
            return {
                'cash_balance': Decimal(str(usd_cash)),
                'total_equity': Decimal(str(total_equity))
            }
            
        except Exception as e:
            logger.error(f"❌ Error fetching Kraken balance: {e}")
            import traceback
            traceback.print_exc()
            await log_error_to_db(
                self.db_pool, api_key[:15] + "...", "KRAKEN_FETCH_BALANCE_ERROR",
                str(e), {"function": "get_kraken_balance", "traceback": traceback.format_exc()[:500]}
            )
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
            # Include both legacy 'withdrawal' and new 'fees_funding_withdrawal' types
            withdrawals_result = await conn.fetchval("""
                SELECT COALESCE(SUM(amount), 0)
                FROM portfolio_transactions
                WHERE (follower_user_id = $1 OR user_id = $2)
                  AND transaction_type IN ('withdrawal', 'fees_funding_withdrawal')
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
        Record a deposit or fees/funding/withdrawal transaction
        
        OPTIMIZED: For fees_funding_withdrawal, aggregates into daily records
        to prevent table bloat from hourly balance checks.
        
        CONSOLIDATED: Uses both follower_user_id (new) and user_id (legacy api_key) for compatibility
        """
        async with self.db_pool.acquire() as conn:
            if transaction_type == 'fees_funding_withdrawal':
                # UPSERT pattern: Update today's record if exists, otherwise create new
                # This keeps one fees record per user per day instead of one per hour
                existing = await conn.fetchrow("""
                    SELECT id, amount FROM portfolio_transactions
                    WHERE follower_user_id = $1
                      AND transaction_type = 'fees_funding_withdrawal'
                      AND DATE(created_at) = CURRENT_DATE
                    LIMIT 1
                """, user_id)
                
                if existing:
                    # Add to existing daily record
                    new_amount = float(existing['amount']) + float(amount)
                    await conn.execute("""
                        UPDATE portfolio_transactions
                        SET amount = $1,
                            notes = 'Daily total: Trading fees, funding payments, or withdrawals',
                            created_at = NOW()
                        WHERE id = $2
                    """, new_amount, existing['id'])
                    logger.info(f"📊 Updated daily fees for {api_key[:10]}...: +${amount:.2f} (total: ${new_amount:.2f})")
                else:
                    # Create new daily record
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
                        user_id,
                        api_key,
                        transaction_type,
                        float(amount),
                        'automatic',
                        'Daily total: Trading fees, funding payments, or withdrawals'
                    )
                    logger.info(f"✅ Created daily fees record for {api_key[:10]}...: ${amount:.2f}")
            else:
                # Deposits and other types: always create individual records
                if transaction_type == 'deposit':
                    notes = 'Detected deposit via balance increase'
                else:
                    notes = f'Auto-detected {transaction_type} via balance checker'
                
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
                    user_id,
                    api_key,
                    transaction_type,
                    float(amount),
                    'automatic',
                    notes
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
            
            # Get withdrawals (include both legacy and new type)
            withdrawals_result = await conn.fetchval("""
                SELECT COALESCE(SUM(amount), 0)
                FROM portfolio_transactions
                WHERE (follower_user_id = $1 OR user_id = $2)
                  AND transaction_type IN ('withdrawal', 'fees_funding_withdrawal')
            """, user_id, api_key)
            withdrawals = float(withdrawals_result or 0)
            
            # Get trading P&L from actual trades
            profit_result = await conn.fetchval("""
                SELECT COALESCE(SUM(profit_usd), 0)
                FROM trades
                WHERE user_id = $1 AND closed_at IS NOT NULL
            """, user_id)
            profit = float(profit_result or 0)
            
            # Get when user started tracking (first trade or portfolio init)
            started_tracking = await conn.fetchval("""
                SELECT COALESCE(
                    (SELECT MIN(opened_at) FROM trades WHERE user_id = $1),
                    (SELECT started_tracking_at FROM follower_users WHERE id = $1)
                )
            """, user_id)
            
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
                'started_tracking': started_tracking.isoformat() if started_tracking else None,
                'last_balance_check': None
            }


    async def get_transaction_history(
        self, 
        api_key: str, 
        limit: int = 50,
        offset: int = 0,
        start_date: str = None,
        end_date: str = None
    ) -> list:
        """
        Get transaction history for a user with pagination and date filtering
        
        Args:
            api_key: User's API key
            limit: Max records to return
            offset: Pagination offset
            start_date: Filter from this date (YYYY-MM-DD)
            end_date: Filter to this date (YYYY-MM-DD)
        
        NOTE: fees_funding_withdrawal records are already aggregated by day
        at write time, so no complex aggregation needed here.
        
        CONSOLIDATED: Uses both FKs for compatibility
        """
        async with self.db_pool.acquire() as conn:
            # Get user_id
            user_id = await conn.fetchval("""
                SELECT id FROM follower_users WHERE api_key = $1
            """, api_key)
            
            # Build query with optional date filters
            if start_date and end_date:
                transactions = await conn.fetch("""
                    SELECT 
                        transaction_type,
                        amount,
                        created_at,
                        detection_method,
                        notes
                    FROM portfolio_transactions
                    WHERE (follower_user_id = $1 OR user_id = $2)
                      AND DATE(created_at) >= $5::date
                      AND DATE(created_at) <= $6::date
                    ORDER BY created_at DESC
                    LIMIT $3 OFFSET $4
                """, user_id, api_key, limit, offset, start_date, end_date)
            elif start_date:
                transactions = await conn.fetch("""
                    SELECT 
                        transaction_type,
                        amount,
                        created_at,
                        detection_method,
                        notes
                    FROM portfolio_transactions
                    WHERE (follower_user_id = $1 OR user_id = $2)
                      AND DATE(created_at) >= $5::date
                    ORDER BY created_at DESC
                    LIMIT $3 OFFSET $4
                """, user_id, api_key, limit, offset, start_date)
            elif end_date:
                transactions = await conn.fetch("""
                    SELECT 
                        transaction_type,
                        amount,
                        created_at,
                        detection_method,
                        notes
                    FROM portfolio_transactions
                    WHERE (follower_user_id = $1 OR user_id = $2)
                      AND DATE(created_at) <= $5::date
                    ORDER BY created_at DESC
                    LIMIT $3 OFFSET $4
                """, user_id, api_key, limit, offset, end_date)
            else:
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
                    LIMIT $3 OFFSET $4
                """, user_id, api_key, limit, offset)
            
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
                await log_error_to_db(
                    self.db_pool, "system", "BALANCE_CHECK_LOOP_ERROR",
                    str(e), {"function": "_run", "traceback": traceback.format_exc()[:500]}
                )
            
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
