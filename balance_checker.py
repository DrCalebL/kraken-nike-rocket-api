"""
Automatic Balance Checker - Detects Deposits & Withdrawals
===========================================================

This module automatically detects when users deposit or withdraw funds
from their Kraken account by comparing actual balance to expected balance.

INCLUDES ZERO DIVISION PROTECTION for safe ROI calculations.

Usage:
    from balance_checker import BalanceChecker
    
    checker = BalanceChecker(db_pool)
    await checker.check_all_users()

Author: Nike Rocket Team
Updated: November 23, 2025 (with zero division protection)
"""

import asyncio
import asyncpg
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, List
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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
        """Check balance for all users with active agents"""
        async with self.db_pool.acquire() as conn:
            # Get all users with agents
            users = await conn.fetch("""
                SELECT DISTINCT fa.follower_user_id as user_id, fa.api_key, fa.api_secret
                FROM follower_agents fa
                JOIN portfolio_users pu ON pu.user_id = fa.follower_user_id
                WHERE fa.status = 'running'
                AND pu.initial_capital > 0
            """)
            
            logger.info(f"Checking balance for {len(users)} users...")
            
            for user in users:
                try:
                    await self.check_user_balance(
                        user['user_id'],
                        user['api_key'],
                        user['api_secret']
                    )
                except Exception as e:
                    logger.error(f"Error checking user {user['user_id']}: {e}")
    
    async def check_user_balance(
        self, 
        user_id: str, 
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
            logger.warning(f"Could not get Kraken balance for {user_id}")
            return
        
        # 2. Get expected balance from database
        expected_balance = await self.calculate_expected_balance(user_id)
        
        # 3. Calculate difference
        difference = current_balance - expected_balance
        
        # 4. Check if significant difference
        if abs(difference) > self.threshold:
            transaction_type = 'deposit' if difference > 0 else 'withdrawal'
            amount = abs(difference)
            
            logger.info(
                f"✅ Detected {transaction_type} for {user_id}: "
                f"${amount:.2f} (Current: ${current_balance:.2f}, "
                f"Expected: ${expected_balance:.2f})"
            )
            
            # 5. Record transaction
            await self.record_transaction(
                user_id=user_id,
                transaction_type=transaction_type,
                amount=amount,
                balance_before=expected_balance,
                balance_after=current_balance
            )
        
        # 6. Update last known balance
        await self.update_last_known_balance(user_id, current_balance)
    
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
            
            # Get USDT balance (or ZUSD depending on your setup)
            usdt_balance = 0
            for currency in ['USDT', 'ZUSD', 'USD']:
                if currency in balance.index:
                    usdt_balance = float(balance.loc[currency]['vol'])
                    break
            
            return Decimal(str(usdt_balance))
            
        except Exception as e:
            logger.error(f"Error getting Kraken balance: {e}")
            return None
    
    async def calculate_expected_balance(self, user_id: str) -> Decimal:
        """Calculate expected balance based on last known balance + trades"""
        async with self.db_pool.acquire() as conn:
            # Get last known balance and check time
            user_data = await conn.fetchrow("""
                SELECT last_known_balance, last_balance_check, initial_capital
                FROM portfolio_users
                WHERE user_id = $1
            """, user_id)
            
            if not user_data:
                return Decimal('0')
            
            last_balance = Decimal(str(user_data['last_known_balance'] or 0))
            last_check = user_data['last_balance_check']
            
            # Get all trades since last check
            trades_pnl = await conn.fetchval("""
                SELECT COALESCE(SUM(pnl), 0)
                FROM portfolio_trades
                WHERE user_id = $1
                AND closed_at > $2
            """, user_id, last_check)
            
            trades_pnl = Decimal(str(trades_pnl or 0))
            
            # Expected = last known + trades PnL
            expected = last_balance + trades_pnl
            
            logger.debug(
                f"Expected balance for {user_id}: "
                f"${expected:.2f} (Last: ${last_balance:.2f}, "
                f"Trades PnL: ${trades_pnl:.2f})"
            )
            
            return expected
    
    async def record_transaction(
        self,
        user_id: str,
        transaction_type: str,
        amount: Decimal,
        balance_before: Decimal,
        balance_after: Decimal
    ):
        """Record a detected transaction in database"""
        async with self.db_pool.acquire() as conn:
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
                user_id,
                transaction_type,
                amount,
                balance_before,
                balance_after,
                f"Auto-detected {transaction_type} of ${amount:.2f}"
            )
            
            logger.info(f"✅ Recorded {transaction_type} of ${amount:.2f} for {user_id}")
    
    async def update_last_known_balance(self, user_id: str, balance: Decimal):
        """Update last known balance for user"""
        async with self.db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE portfolio_users
                SET last_known_balance = $1,
                    last_balance_check = CURRENT_TIMESTAMP
                WHERE user_id = $2
            """, balance, user_id)
    
    async def get_transaction_history(
        self, 
        user_id: str, 
        limit: int = 50
    ) -> List[Dict]:
        """Get transaction history for a user"""
        async with self.db_pool.acquire() as conn:
            transactions = await conn.fetch("""
                SELECT 
                    transaction_type,
                    amount,
                    balance_before,
                    balance_after,
                    detection_method,
                    created_at,
                    notes
                FROM portfolio_transactions
                WHERE user_id = $1
                ORDER BY created_at DESC
                LIMIT $2
            """, user_id, limit)
            
            return [dict(tx) for tx in transactions]
    
    async def get_balance_summary(self, user_id: str) -> Dict:
        """
        Get balance summary for a user - WITH ZERO DIVISION PROTECTION
        
        This method includes multiple layers of protection against division by zero:
        1. Checks if initial_capital <= 0 and sets to 1 if needed
        2. Checks if total_capital <= 0 and falls back to initial
        3. Try-catch on all ROI calculations
        4. Caps ROI at reasonable values (±10,000%)
        """
        async with self.db_pool.acquire() as conn:
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
                        (SELECT SUM(pnl) FROM portfolio_trades 
                         WHERE user_id = $1),
                        0
                    ) as total_profit
                FROM portfolio_users pu
                WHERE pu.user_id = $1
            """, user_id)
            
            if not summary:
                return {}
            
            initial = Decimal(str(summary['initial_capital'] or 0))
            deposits = Decimal(str(summary['total_deposits'] or 0))
            withdrawals = Decimal(str(summary['total_withdrawals'] or 0))
            profit = Decimal(str(summary['total_profit'] or 0))
            
            # SAFETY CHECK 1: Ensure initial capital is never zero or negative
            if initial <= 0:
                logger.warning(
                    f"User {user_id} has invalid initial_capital: {initial}. "
                    f"Setting to 1 to prevent division by zero."
                )
                initial = Decimal('1')  # Prevent division by zero
            
            net_deposits = deposits - withdrawals
            total_capital = initial + net_deposits
            
            # SAFETY CHECK 2: Ensure total capital is never zero or negative
            if total_capital <= 0:
                logger.warning(
                    f"User {user_id} has invalid total_capital: {total_capital}. "
                    f"Setting to initial capital."
                )
                total_capital = initial  # Fallback to initial
            
            current_value = total_capital + profit
            
            # SAFETY CHECK 3: Calculate ROI with zero division protection
            try:
                roi_on_initial = float((profit / initial * 100)) if initial > 0 else 0.0
            except (ZeroDivisionError, InvalidOperation) as e:
                logger.error(
                    f"ROI calculation error for user {user_id}: "
                    f"initial={initial}, profit={profit}. Error: {e}"
                )
                roi_on_initial = 0.0
            
            try:
                roi_on_total = float((profit / total_capital * 100)) if total_capital > 0 else 0.0
            except (ZeroDivisionError, InvalidOperation) as e:
                logger.error(
                    f"ROI calculation error for user {user_id}: "
                    f"total_capital={total_capital}, profit={profit}. Error: {e}"
                )
                roi_on_total = 0.0
            
            # SAFETY CHECK 4: Cap ROI at reasonable values (prevent infinity display)
            # Max ROI: ±10,000% (100x return)
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
    
    def __init__(self, db_pool: asyncpg.Pool, interval_minutes: int = 60):
        """
        Args:
            db_pool: PostgreSQL connection pool
            interval_minutes: How often to check (default 60 minutes)
        """
        self.checker = BalanceChecker(db_pool)
        self.interval_minutes = interval_minutes
        self.running = False
    
    async def start(self):
        """Start the scheduler"""
        self.running = True
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
                logger.error(f"Error in balance check: {e}")
                import traceback
                traceback.print_exc()
            
            # Wait for next check
            await asyncio.sleep(self.interval_minutes * 60)
    
    def stop(self):
        """Stop the scheduler"""
        self.running = False
        logger.info("⏸️ Balance checker stopped")


# ============================================================================
# USAGE EXAMPLES
# ============================================================================

async def example_usage():
    """Example of how to use the balance checker"""
    
    # Create database connection pool
    db_pool = await asyncpg.create_pool(
        "postgresql://user:pass@host:port/database"
    )
    
    # Option 1: Check all users once
    checker = BalanceChecker(db_pool)
    await checker.check_all_users()
    
    # Option 2: Get balance summary for specific user (SAFE - no division by zero)
    summary = await checker.get_balance_summary("nk_abc123")
    print(f"Balance summary: {summary}")
    print(f"ROI on Initial: {summary['roi_on_initial']:.2f}%")
    print(f"ROI on Total: {summary['roi_on_total']:.2f}%")
    
    # Option 3: Get transaction history
    history = await checker.get_transaction_history("nk_abc123", limit=10)
    for tx in history:
        print(f"{tx['created_at']}: {tx['transaction_type']} ${tx['amount']}")
    
    # Option 4: Run scheduler (checks every hour)
    scheduler = BalanceCheckerScheduler(db_pool, interval_minutes=60)
    await scheduler.start()  # This runs forever


if __name__ == "__main__":
    # Run example
    asyncio.run(example_usage())
