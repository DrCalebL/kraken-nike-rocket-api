"""
Nike Rocket - Portfolio Tracking Models
========================================
DEPRECATED: Portfolio tables have been consolidated into follower_users and trades tables.

This file is kept as a stub to prevent import errors.
No tables are created by this module.

Author: Nike Rocket Team
Updated: November 28, 2025
"""

# Empty stub - no SQLAlchemy models
# All portfolio data now lives in:
# - follower_users (initial_capital, last_known_balance, portfolio_initialized)
# - trades (trade history with profit_usd, profit_percent)
# - portfolio_transactions (deposits/withdrawals)


def init_portfolio_db(engine):
    """No-op - portfolio tables are deprecated"""
    print("✅ Portfolio database tables created (using consolidated schema)")
    pass
