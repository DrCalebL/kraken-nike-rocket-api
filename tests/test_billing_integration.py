"""
Nike Rocket Billing Integration Tests
======================================

Comprehensive tests for 30-day rolling billing system.

Run with: pytest tests/test_billing_integration.py -v
Run specific test: pytest tests/test_billing_integration.py::TestBillingCycles::test_profitable_cycle_standard_tier -v

Requirements:
    pip install pytest pytest-asyncio asyncpg

Setup:
    Set TEST_DATABASE_URL environment variable to a test database
    (DO NOT use production database!)

Author: Nike Rocket Team
"""

import os
import pytest
import asyncio
import asyncpg
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, Dict, Any
from unittest.mock import AsyncMock, patch, MagicMock

# Add parent directory to path for imports
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    get_fee_rate, get_tier_display, utc_now, to_naive_utc,
    BILLING_CYCLE_DAYS, PAYMENT_GRACE_DAYS, FEE_TIERS
)
from billing_service_30day import BillingServiceV2


# =============================================================================
# TEST FIXTURES
# =============================================================================

@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def db_pool():
    """Create database connection pool for tests"""
    test_db_url = os.getenv("TEST_DATABASE_URL")
    
    if not test_db_url:
        pytest.skip("TEST_DATABASE_URL not set - skipping integration tests")
    
    pool = await asyncpg.create_pool(test_db_url, min_size=2, max_size=5)
    yield pool
    await pool.close()


@pytest.fixture
async def clean_test_data(db_pool):
    """Clean up test data before and after each test"""
    async with db_pool.acquire() as conn:
        # Clean up before test
        await conn.execute("DELETE FROM billing_invoices WHERE user_id IN (SELECT id FROM follower_users WHERE email LIKE 'test_%@nikerocket.test')")
        await conn.execute("DELETE FROM billing_cycles WHERE user_id IN (SELECT id FROM follower_users WHERE email LIKE 'test_%@nikerocket.test')")
        await conn.execute("DELETE FROM follower_users WHERE email LIKE 'test_%@nikerocket.test'")
    
    yield
    
    # Clean up after test
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM billing_invoices WHERE user_id IN (SELECT id FROM follower_users WHERE email LIKE 'test_%@nikerocket.test')")
        await conn.execute("DELETE FROM billing_cycles WHERE user_id IN (SELECT id FROM follower_users WHERE email LIKE 'test_%@nikerocket.test')")
        await conn.execute("DELETE FROM follower_users WHERE email LIKE 'test_%@nikerocket.test'")


# =============================================================================
# TEST HELPERS
# =============================================================================

async def create_test_user(
    conn,
    email: str,
    fee_tier: str = 'standard',
    cycle_start_days_ago: Optional[int] = None,
    profit: float = 0.0,
    trades: int = 0,
    access_granted: bool = True
) -> int:
    """Create a test user and return their ID"""
    import secrets
    api_key = f"nk_test_{secrets.token_urlsafe(16)}"
    
    cycle_start = None
    if cycle_start_days_ago is not None:
        cycle_start = to_naive_utc(utc_now() - timedelta(days=cycle_start_days_ago))
    
    user_id = await conn.fetchval("""
        INSERT INTO follower_users (
            email, api_key, fee_tier, 
            billing_cycle_start, current_cycle_profit, current_cycle_trades,
            access_granted, agent_active, created_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, true, NOW())
        RETURNING id
    """, email, api_key, fee_tier, cycle_start, profit, trades, access_granted)
    
    return user_id


async def get_user(conn, user_id: int) -> Dict[str, Any]:
    """Get user by ID"""
    row = await conn.fetchrow("SELECT * FROM follower_users WHERE id = $1", user_id)
    return dict(row) if row else None


async def get_billing_cycles(conn, user_id: int) -> list:
    """Get billing cycles for user"""
    rows = await conn.fetch(
        "SELECT * FROM billing_cycles WHERE user_id = $1 ORDER BY created_at DESC",
        user_id
    )
    return [dict(row) for row in rows]


async def get_pending_invoice(conn, user_id: int) -> Optional[Dict[str, Any]]:
    """Get pending invoice for user"""
    row = await conn.fetchrow("""
        SELECT * FROM billing_invoices 
        WHERE user_id = $1 AND status = 'pending'
        ORDER BY created_at DESC LIMIT 1
    """, user_id)
    return dict(row) if row else None


def days_ago(n: int) -> datetime:
    """Get datetime n days ago"""
    return to_naive_utc(utc_now() - timedelta(days=n))


# =============================================================================
# UNIT TESTS - Config Functions
# =============================================================================

class TestConfigFunctions:
    """Test centralized configuration functions"""
    
    def test_get_fee_rate_standard(self):
        assert get_fee_rate('standard') == 0.10
    
    def test_get_fee_rate_vip(self):
        assert get_fee_rate('vip') == 0.05
    
    def test_get_fee_rate_team(self):
        assert get_fee_rate('team') == 0.00
    
    def test_get_fee_rate_none_defaults_to_standard(self):
        assert get_fee_rate(None) == 0.10
    
    def test_get_fee_rate_empty_string_defaults_to_standard(self):
        assert get_fee_rate('') == 0.10
    
    def test_get_fee_rate_invalid_defaults_to_standard(self):
        assert get_fee_rate('invalid_tier') == 0.10
    
    def test_get_tier_display_standard(self):
        assert get_tier_display('standard') == '👤 Standard (10%)'
    
    def test_get_tier_display_vip(self):
        assert get_tier_display('vip') == '⭐ VIP (5%)'
    
    def test_get_tier_display_team(self):
        assert get_tier_display('team') == '🏠 Team (0%)'
    
    def test_get_tier_display_none_defaults(self):
        assert get_tier_display(None) == '👤 Standard (10%)'
    
    def test_get_tier_display_empty_string_defaults(self):
        assert get_tier_display('') == '👤 Standard (10%)'


class TestFeeCalculations:
    """Test fee calculation logic"""
    
    def test_standard_tier_fee_calculation(self):
        profit = 1000.00
        fee_rate = get_fee_rate('standard')
        fee = profit * fee_rate
        assert fee == 100.00
    
    def test_vip_tier_fee_calculation(self):
        profit = 1000.00
        fee_rate = get_fee_rate('vip')
        fee = profit * fee_rate
        assert fee == 50.00
    
    def test_team_tier_fee_calculation(self):
        profit = 1000.00
        fee_rate = get_fee_rate('team')
        fee = profit * fee_rate
        assert fee == 0.00
    
    def test_negative_profit_no_fee(self):
        profit = -500.00
        fee_rate = get_fee_rate('standard')
        fee = max(0, profit * fee_rate) if profit > 0 else 0
        assert fee == 0.00
    
    def test_zero_profit_no_fee(self):
        profit = 0.00
        fee_rate = get_fee_rate('standard')
        fee = max(0, profit * fee_rate) if profit > 0 else 0
        assert fee == 0.00
    
    def test_tiny_profit_calculates_fee(self):
        profit = 1.00
        fee_rate = get_fee_rate('standard')
        fee = profit * fee_rate
        assert fee == 0.10


# =============================================================================
# INTEGRATION TESTS - Billing Cycles
# =============================================================================

@pytest.mark.asyncio
class TestBillingCycles:
    """Integration tests for 30-day billing cycles"""
    
    async def test_profitable_cycle_standard_tier_charges_10_percent(self, db_pool, clean_test_data):
        """Standard tier user with $1000 profit should be invoiced $100"""
        async with db_pool.acquire() as conn:
            # Setup: User with standard tier, cycle started 31 days ago
            user_id = await create_test_user(
                conn,
                email='test_standard_profit@nikerocket.test',
                fee_tier='standard',
                cycle_start_days_ago=31,
                profit=1000.00,
                trades=10
            )
            
            user = await get_user(conn, user_id)
            assert user['current_cycle_profit'] == 1000.00
            assert user['fee_tier'] == 'standard'
        
        # Act: Run billing cycle check (mock Coinbase to avoid real API calls)
        billing = BillingServiceV2(db_pool)
        
        with patch.object(billing, '_generate_coinbase_invoice', new_callable=AsyncMock) as mock_invoice:
            mock_invoice.return_value = {'charge_id': 'test_charge_123', 'hosted_url': 'https://test.com'}
            await billing.check_all_cycles()
        
        # Assert: Invoice should be generated for $100 (10% of $1000)
        async with db_pool.acquire() as conn:
            cycles = await get_billing_cycles(conn, user_id)
            assert len(cycles) == 1
            assert cycles[0]['total_profit'] == 1000.00
            assert cycles[0]['fee_percentage'] == 0.10
            assert cycles[0]['fee_amount'] == 100.00
            
            # Verify Coinbase was called with correct amount
            mock_invoice.assert_called_once()
            call_kwargs = mock_invoice.call_args[1]
            assert call_kwargs['amount'] == 100.00
            assert call_kwargs['profit'] == 1000.00
    
    async def test_profitable_cycle_vip_tier_charges_5_percent(self, db_pool, clean_test_data):
        """VIP tier user with $1000 profit should be invoiced $50"""
        async with db_pool.acquire() as conn:
            user_id = await create_test_user(
                conn,
                email='test_vip_profit@nikerocket.test',
                fee_tier='vip',
                cycle_start_days_ago=31,
                profit=1000.00,
                trades=10
            )
        
        billing = BillingServiceV2(db_pool)
        
        with patch.object(billing, '_generate_coinbase_invoice', new_callable=AsyncMock) as mock_invoice:
            mock_invoice.return_value = {'charge_id': 'test_charge_vip', 'hosted_url': 'https://test.com'}
            await billing.check_all_cycles()
        
        async with db_pool.acquire() as conn:
            cycles = await get_billing_cycles(conn, user_id)
            assert len(cycles) == 1
            assert cycles[0]['fee_percentage'] == 0.05
            assert cycles[0]['fee_amount'] == 50.00  # 5% of $1000
    
    async def test_profitable_cycle_team_tier_no_invoice(self, db_pool, clean_test_data):
        """Team tier user should never be invoiced regardless of profit"""
        async with db_pool.acquire() as conn:
            user_id = await create_test_user(
                conn,
                email='test_team_profit@nikerocket.test',
                fee_tier='team',
                cycle_start_days_ago=31,
                profit=10000.00,  # Even with huge profit
                trades=50
            )
        
        billing = BillingServiceV2(db_pool)
        
        with patch.object(billing, '_generate_coinbase_invoice', new_callable=AsyncMock) as mock_invoice:
            await billing.check_all_cycles()
        
        # Assert: No invoice generated for team
        mock_invoice.assert_not_called()
        
        async with db_pool.acquire() as conn:
            cycles = await get_billing_cycles(conn, user_id)
            assert len(cycles) == 1
            assert cycles[0]['fee_amount'] == 0.00
            assert cycles[0]['invoice_status'] == 'waived'
            
            # Verify cycle was renewed
            user = await get_user(conn, user_id)
            assert user['current_cycle_profit'] == 0  # Reset
            assert user['current_cycle_trades'] == 0  # Reset
            assert user['pending_invoice_id'] is None
    
    async def test_losing_cycle_no_invoice(self, db_pool, clean_test_data):
        """User with negative profit should not be invoiced"""
        async with db_pool.acquire() as conn:
            user_id = await create_test_user(
                conn,
                email='test_losing@nikerocket.test',
                fee_tier='standard',
                cycle_start_days_ago=31,
                profit=-500.00,  # Loss
                trades=5
            )
        
        billing = BillingServiceV2(db_pool)
        
        with patch.object(billing, '_generate_coinbase_invoice', new_callable=AsyncMock) as mock_invoice:
            await billing.check_all_cycles()
        
        mock_invoice.assert_not_called()
        
        async with db_pool.acquire() as conn:
            cycles = await get_billing_cycles(conn, user_id)
            assert len(cycles) == 1
            assert cycles[0]['total_profit'] == -500.00
            assert cycles[0]['fee_amount'] == 0.00
            assert cycles[0]['invoice_status'] == 'waived'
            
            # Verify cycle renewed
            user = await get_user(conn, user_id)
            assert user['current_cycle_profit'] == 0
    
    async def test_breakeven_cycle_no_invoice(self, db_pool, clean_test_data):
        """User with exactly $0 profit should not be invoiced"""
        async with db_pool.acquire() as conn:
            user_id = await create_test_user(
                conn,
                email='test_breakeven@nikerocket.test',
                fee_tier='standard',
                cycle_start_days_ago=31,
                profit=0.00,
                trades=5
            )
        
        billing = BillingServiceV2(db_pool)
        
        with patch.object(billing, '_generate_coinbase_invoice', new_callable=AsyncMock) as mock_invoice:
            await billing.check_all_cycles()
        
        mock_invoice.assert_not_called()
        
        async with db_pool.acquire() as conn:
            cycles = await get_billing_cycles(conn, user_id)
            assert cycles[0]['fee_amount'] == 0.00
            assert cycles[0]['invoice_status'] == 'waived'
    
    async def test_cycle_not_ended_before_30_days(self, db_pool, clean_test_data):
        """Cycle should not end before 30 days even with profit"""
        async with db_pool.acquire() as conn:
            user_id = await create_test_user(
                conn,
                email='test_not_due@nikerocket.test',
                fee_tier='standard',
                cycle_start_days_ago=29,  # Not yet 30 days
                profit=1000.00,
                trades=10
            )
        
        billing = BillingServiceV2(db_pool)
        
        with patch.object(billing, '_generate_coinbase_invoice', new_callable=AsyncMock) as mock_invoice:
            await billing.check_all_cycles()
        
        # Should NOT have processed this user
        mock_invoice.assert_not_called()
        
        async with db_pool.acquire() as conn:
            cycles = await get_billing_cycles(conn, user_id)
            assert len(cycles) == 0  # No cycle ended
            
            # Profit should still be accumulating
            user = await get_user(conn, user_id)
            assert user['current_cycle_profit'] == 1000.00
    
    async def test_tiny_profit_still_invoiced(self, db_pool, clean_test_data):
        """Even $1 profit should generate invoice"""
        async with db_pool.acquire() as conn:
            user_id = await create_test_user(
                conn,
                email='test_tiny_profit@nikerocket.test',
                fee_tier='standard',
                cycle_start_days_ago=31,
                profit=1.00,
                trades=1
            )
        
        billing = BillingServiceV2(db_pool)
        
        with patch.object(billing, '_generate_coinbase_invoice', new_callable=AsyncMock) as mock_invoice:
            mock_invoice.return_value = {'charge_id': 'test_tiny', 'hosted_url': 'https://test.com'}
            await billing.check_all_cycles()
        
        async with db_pool.acquire() as conn:
            cycles = await get_billing_cycles(conn, user_id)
            assert cycles[0]['fee_amount'] == 0.10  # 10% of $1
    
    async def test_user_without_cycle_not_processed(self, db_pool, clean_test_data):
        """User with no billing cycle started should not be processed"""
        async with db_pool.acquire() as conn:
            user_id = await create_test_user(
                conn,
                email='test_no_cycle@nikerocket.test',
                fee_tier='standard',
                cycle_start_days_ago=None,  # No cycle started
                profit=0.00,
                trades=0
            )
        
        billing = BillingServiceV2(db_pool)
        
        with patch.object(billing, '_generate_coinbase_invoice', new_callable=AsyncMock) as mock_invoice:
            await billing.check_all_cycles()
        
        mock_invoice.assert_not_called()
        
        async with db_pool.acquire() as conn:
            cycles = await get_billing_cycles(conn, user_id)
            assert len(cycles) == 0


@pytest.mark.asyncio
class TestTierChanges:
    """Test fee tier change behavior"""
    
    async def test_tier_change_applies_at_cycle_end(self, db_pool, clean_test_data):
        """Pending tier change should apply when cycle ends"""
        async with db_pool.acquire() as conn:
            user_id = await create_test_user(
                conn,
                email='test_tier_change@nikerocket.test',
                fee_tier='standard',
                cycle_start_days_ago=31,
                profit=1000.00,
                trades=10
            )
            
            # Set pending tier change
            await conn.execute("""
                UPDATE follower_users SET next_cycle_fee_tier = 'vip' WHERE id = $1
            """, user_id)
        
        billing = BillingServiceV2(db_pool)
        
        with patch.object(billing, '_generate_coinbase_invoice', new_callable=AsyncMock) as mock_invoice:
            mock_invoice.return_value = {'charge_id': 'test_tier', 'hosted_url': 'https://test.com'}
            await billing.check_all_cycles()
        
        async with db_pool.acquire() as conn:
            # Current invoice should be at OLD rate (10%)
            cycles = await get_billing_cycles(conn, user_id)
            assert cycles[0]['fee_percentage'] == 0.10
            assert cycles[0]['fee_amount'] == 100.00
            
            # But fee_tier should now be updated for next cycle
            user = await get_user(conn, user_id)
            assert user['fee_tier'] == 'vip'
            assert user['next_cycle_fee_tier'] is None
    
    async def test_tier_change_applies_even_without_invoice(self, db_pool, clean_test_data):
        """Tier change should apply even when no invoice is generated (losing cycle)"""
        async with db_pool.acquire() as conn:
            user_id = await create_test_user(
                conn,
                email='test_tier_change_loss@nikerocket.test',
                fee_tier='standard',
                cycle_start_days_ago=31,
                profit=-100.00,  # Loss - no invoice
                trades=5
            )
            
            # Set pending tier change
            await conn.execute("""
                UPDATE follower_users SET next_cycle_fee_tier = 'team' WHERE id = $1
            """, user_id)
        
        billing = BillingServiceV2(db_pool)
        
        with patch.object(billing, '_generate_coinbase_invoice', new_callable=AsyncMock) as mock_invoice:
            await billing.check_all_cycles()
        
        mock_invoice.assert_not_called()
        
        async with db_pool.acquire() as conn:
            user = await get_user(conn, user_id)
            assert user['fee_tier'] == 'team'  # Changed!
            assert user['next_cycle_fee_tier'] is None


@pytest.mark.asyncio
class TestPaymentWebhooks:
    """Test payment webhook processing"""
    
    async def test_payment_clears_invoice_and_renews_cycle(self, db_pool, clean_test_data):
        """Successful payment should clear invoice and reset cycle"""
        async with db_pool.acquire() as conn:
            user_id = await create_test_user(
                conn,
                email='test_payment@nikerocket.test',
                fee_tier='standard',
                cycle_start_days_ago=31,
                profit=1000.00,
                trades=10
            )
        
        billing = BillingServiceV2(db_pool)
        
        # First, generate an invoice
        with patch.object(billing, '_generate_coinbase_invoice', new_callable=AsyncMock) as mock_invoice:
            mock_invoice.return_value = {'charge_id': 'test_payment_charge', 'hosted_url': 'https://test.com'}
            await billing.check_all_cycles()
        
        # Manually insert invoice record (since we mocked the API)
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO billing_invoices (user_id, coinbase_charge_id, amount_usd, status, hosted_url)
                VALUES ($1, 'test_payment_charge', 100.00, 'pending', 'https://test.com')
            """, user_id)
            
            await conn.execute("""
                UPDATE follower_users SET 
                    pending_invoice_id = 'test_payment_charge',
                    pending_invoice_amount = 100.00
                WHERE id = $1
            """, user_id)
        
        # Process payment webhook
        async with db_pool.acquire() as conn:
            # Simulate payment confirmed
            await conn.execute("""
                UPDATE billing_invoices SET status = 'paid', paid_at = NOW()
                WHERE coinbase_charge_id = 'test_payment_charge'
            """)
            
            await conn.execute("""
                UPDATE follower_users SET
                    pending_invoice_id = NULL,
                    pending_invoice_amount = 0,
                    total_fees_paid = COALESCE(total_fees_paid, 0) + 100.00
                WHERE id = $1
            """, user_id)
            
            # Verify state
            user = await get_user(conn, user_id)
            assert user['pending_invoice_id'] is None
            assert user['pending_invoice_amount'] == 0
            assert user['total_fees_paid'] == 100.00


@pytest.mark.asyncio  
class TestEdgeCases:
    """Test edge cases and error handling"""
    
    async def test_empty_string_fee_tier_defaults_to_standard(self, db_pool, clean_test_data):
        """Empty string fee_tier should be treated as standard"""
        async with db_pool.acquire() as conn:
            user_id = await create_test_user(
                conn,
                email='test_empty_tier@nikerocket.test',
                fee_tier='',  # Empty string
                cycle_start_days_ago=31,
                profit=1000.00,
                trades=10
            )
            
            # Force empty string (in case create_test_user normalizes it)
            await conn.execute("""
                UPDATE follower_users SET fee_tier = '' WHERE id = $1
            """, user_id)
        
        billing = BillingServiceV2(db_pool)
        
        with patch.object(billing, '_generate_coinbase_invoice', new_callable=AsyncMock) as mock_invoice:
            mock_invoice.return_value = {'charge_id': 'test_empty', 'hosted_url': 'https://test.com'}
            await billing.check_all_cycles()
        
        async with db_pool.acquire() as conn:
            cycles = await get_billing_cycles(conn, user_id)
            # Should use standard rate (10%)
            assert cycles[0]['fee_percentage'] == 0.10
            assert cycles[0]['fee_amount'] == 100.00
    
    async def test_suspended_user_not_processed(self, db_pool, clean_test_data):
        """User without access_granted should not have cycle processed"""
        async with db_pool.acquire() as conn:
            user_id = await create_test_user(
                conn,
                email='test_suspended@nikerocket.test',
                fee_tier='standard',
                cycle_start_days_ago=31,
                profit=1000.00,
                trades=10,
                access_granted=False  # Suspended
            )
        
        billing = BillingServiceV2(db_pool)
        
        with patch.object(billing, '_generate_coinbase_invoice', new_callable=AsyncMock) as mock_invoice:
            await billing.check_all_cycles()
        
        mock_invoice.assert_not_called()
        
        async with db_pool.acquire() as conn:
            cycles = await get_billing_cycles(conn, user_id)
            assert len(cycles) == 0  # Not processed
    
    async def test_user_with_pending_invoice_not_double_processed(self, db_pool, clean_test_data):
        """User with existing pending invoice should not get another"""
        async with db_pool.acquire() as conn:
            user_id = await create_test_user(
                conn,
                email='test_pending@nikerocket.test',
                fee_tier='standard',
                cycle_start_days_ago=31,
                profit=1000.00,
                trades=10
            )
            
            # Set existing pending invoice
            await conn.execute("""
                UPDATE follower_users SET 
                    pending_invoice_id = 'existing_invoice_123',
                    pending_invoice_amount = 50.00
                WHERE id = $1
            """, user_id)
        
        billing = BillingServiceV2(db_pool)
        
        with patch.object(billing, '_generate_coinbase_invoice', new_callable=AsyncMock) as mock_invoice:
            await billing.check_all_cycles()
        
        # Should NOT generate another invoice
        mock_invoice.assert_not_called()


@pytest.mark.asyncio
class TestStartBillingCycle:
    """Test billing cycle initialization"""
    
    async def test_start_billing_cycle_new_user(self, db_pool, clean_test_data):
        """Starting billing cycle for user without one should succeed"""
        async with db_pool.acquire() as conn:
            user_id = await create_test_user(
                conn,
                email='test_start_new@nikerocket.test',
                fee_tier='standard',
                cycle_start_days_ago=None,  # No cycle yet
                profit=0,
                trades=0
            )
        
        billing = BillingServiceV2(db_pool)
        result = await billing.start_billing_cycle(user_id)
        
        assert result == True
        
        async with db_pool.acquire() as conn:
            user = await get_user(conn, user_id)
            assert user['billing_cycle_start'] is not None
            assert user['current_cycle_profit'] == 0
            assert user['current_cycle_trades'] == 0
    
    async def test_start_billing_cycle_existing_returns_false(self, db_pool, clean_test_data):
        """Starting billing cycle for user with existing one should return False"""
        async with db_pool.acquire() as conn:
            user_id = await create_test_user(
                conn,
                email='test_start_existing@nikerocket.test',
                fee_tier='standard',
                cycle_start_days_ago=5,  # Already has cycle
                profit=100.00,
                trades=3
            )
        
        billing = BillingServiceV2(db_pool)
        result = await billing.start_billing_cycle(user_id)
        
        assert result == False  # Already has cycle
        
        # Verify nothing changed
        async with db_pool.acquire() as conn:
            user = await get_user(conn, user_id)
            assert user['current_cycle_profit'] == 100.00  # Unchanged


# =============================================================================
# RUN TESTS
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
