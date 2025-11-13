"""
Nike Rocket Follower System - Database Models
==============================================

Database models for managing followers, signals, and profit tracking.
Designed for Railway PostgreSQL database.

Author: Nike Rocket Team
"""

from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime
import secrets

Base = declarative_base()


class User(Base):
    """
    Follower user account
    
    Tracks:
    - User credentials and API key
    - Subscription status
    - Monthly P&L and fees
    - Payment history
    """
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    api_key = Column(String, unique=True, index=True, nullable=False)  # nk_xxxxx format
    
    # Account status
    created_at = Column(DateTime, default=datetime.utcnow)
    access_granted = Column(Boolean, default=True)
    suspended_at = Column(DateTime, nullable=True)
    suspension_reason = Column(String, nullable=True)
    
    # Monthly tracking (resets at month end)
    monthly_profit = Column(Float, default=0.0)  # Total profit this month
    monthly_trades = Column(Integer, default=0)   # Number of trades this month
    monthly_fee_due = Column(Float, default=0.0)  # 10% of monthly_profit
    monthly_fee_paid = Column(Boolean, default=False)
    last_reset_date = Column(DateTime, default=datetime.utcnow)
    
    # All-time stats
    total_profit = Column(Float, default=0.0)
    total_fees_paid = Column(Float, default=0.0)
    total_trades = Column(Integer, default=0)
    
    # Kraken account info (optional - for verification)
    kraken_account_id = Column(String, nullable=True)
    
    # Relationships
    trades = relationship("Trade", back_populates="user", cascade="all, delete-orphan")
    signals_received = relationship("SignalDelivery", back_populates="user", cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="user", cascade="all, delete-orphan")
    
    @staticmethod
    def generate_api_key():
        """Generate unique API key in format: nk_xxxxxxxxxxxxx"""
        return f"nk_{secrets.token_urlsafe(16)}"
    
    def calculate_monthly_fee(self):
        """Calculate 10% fee on monthly profit (only if profitable)"""
        if self.monthly_profit > 0:
            self.monthly_fee_due = self.monthly_profit * 0.10
        else:
            self.monthly_fee_due = 0.0
        return self.monthly_fee_due
    
    def reset_monthly_stats(self):
        """Reset monthly stats at beginning of new month"""
        self.monthly_profit = 0.0
        self.monthly_trades = 0
        self.monthly_fee_due = 0.0
        self.monthly_fee_paid = False
        self.last_reset_date = datetime.utcnow()
    
    def check_payment_status(self):
        """Check if user should be suspended for non-payment"""
        if self.monthly_fee_due > 0 and not self.monthly_fee_paid:
            # Check if it's been more than 7 days since month end
            days_since_reset = (datetime.utcnow() - self.last_reset_date).days
            if days_since_reset > 37:  # 30 days in month + 7 day grace period
                return False  # Should be suspended
        return True


class Signal(Base):
    """
    Trading signal broadcast by master algorithm
    
    Stores all signals sent to followers for tracking and verification
    """
    __tablename__ = "signals"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # Signal identification
    signal_id = Column(String, unique=True, index=True)  # UUID
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Trading details
    action = Column(String, nullable=False)  # BUY or SELL
    symbol = Column(String, nullable=False)  # ADA/USDT
    entry_price = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=False)
    take_profit = Column(Float, nullable=False)
    leverage = Column(Float, nullable=False)
    
    # Optional metadata
    timeframe = Column(String, nullable=True)
    trend_strength = Column(Float, nullable=True)
    volatility = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)
    
    # Delivery tracking
    deliveries = relationship("SignalDelivery", back_populates="signal", cascade="all, delete-orphan")


class SignalDelivery(Base):
    """
    Tracks which users received which signals
    
    Used for verification and debugging
    """
    __tablename__ = "signal_deliveries"
    
    id = Column(Integer, primary_key=True, index=True)
    
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    
    delivered_at = Column(DateTime, default=datetime.utcnow)
    acknowledged = Column(Boolean, default=False)  # Did follower agent confirm receipt?
    
    # Relationships
    signal = relationship("Signal", back_populates="deliveries")
    user = relationship("User", back_populates="signals_received")


class Trade(Base):
    """
    Completed trade reported by follower agent
    
    Tracks actual execution results for profit calculation
    """
    __tablename__ = "trades"
    
    id = Column(Integer, primary_key=True, index=True)
    
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    signal_id = Column(String, nullable=True)  # Links to original signal
    
    # Trade identification
    trade_id = Column(String, index=True)  # From follower agent
    kraken_order_id = Column(String, nullable=True)  # Actual Kraken order ID
    
    # Execution details
    opened_at = Column(DateTime, nullable=False)
    closed_at = Column(DateTime, nullable=False)
    
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)  # BUY or SELL
    
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=False)
    position_size = Column(Float, nullable=False)  # In base currency
    leverage = Column(Float, nullable=False)
    
    # P&L
    profit_usd = Column(Float, nullable=False)  # Final P&L in USD
    profit_percent = Column(Float, nullable=True)  # % return
    
    # Fee tracking
    fee_charged = Column(Float, default=0.0)  # 10% of profit if positive
    
    # Metadata
    reported_at = Column(DateTime, default=datetime.utcnow)
    notes = Column(Text, nullable=True)
    
    # Relationship
    user = relationship("User", back_populates="trades")
    
    def calculate_fee(self):
        """Calculate 10% fee on profitable trades"""
        if self.profit_usd > 0:
            self.fee_charged = self.profit_usd * 0.10
        else:
            self.fee_charged = 0.0
        return self.fee_charged


class Payment(Base):
    """
    Payment record from Coinbase Commerce
    
    Tracks monthly profit-sharing payments
    """
    __tablename__ = "payments"
    
    id = Column(Integer, primary_key=True, index=True)
    
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    
    # Payment details
    amount_usd = Column(Float, nullable=False)
    currency = Column(String, nullable=False)  # USDC, USDT, BTC, ETH
    
    # Coinbase Commerce details
    coinbase_charge_id = Column(String, unique=True, nullable=True)
    coinbase_payment_id = Column(String, nullable=True)
    
    # Status
    status = Column(String, default="pending")  # pending, completed, failed, expired
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    
    # What this payment covers
    for_month = Column(String, nullable=False)  # "2025-11" format
    profit_amount = Column(Float, nullable=False)  # Profit this payment is for
    
    # Metadata
    tx_hash = Column(String, nullable=True)  # Blockchain transaction hash
    notes = Column(Text, nullable=True)
    
    # Relationship
    user = relationship("User", back_populates="payments")


class SystemStats(Base):
    """
    System-wide statistics
    
    Tracks overall performance metrics
    """
    __tablename__ = "system_stats"
    
    id = Column(Integer, primary_key=True, index=True)
    date = Column(DateTime, default=datetime.utcnow, unique=True)
    
    # User metrics
    total_users = Column(Integer, default=0)
    active_users = Column(Integer, default=0)  # Received signal in last 7 days
    suspended_users = Column(Integer, default=0)
    
    # Trading metrics
    total_signals = Column(Integer, default=0)
    total_trades = Column(Integer, default=0)
    total_profit = Column(Float, default=0.0)
    total_fees_collected = Column(Float, default=0.0)
    
    # Performance
    avg_profit_per_trade = Column(Float, default=0.0)
    win_rate = Column(Float, default=0.0)
    
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# Database initialization
def init_db(engine):
    """Initialize database with all tables"""
    Base.metadata.create_all(bind=engine)
    print("✅ Database tables created successfully")


def get_db_session(engine):
    """Create database session"""
    from sqlalchemy.orm import sessionmaker
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return SessionLocal()


# Example usage
if __name__ == "__main__":
    print("Nike Rocket Follower System - Database Models")
    print("=" * 50)
    print("\nModels defined:")
    print("  ✅ User - Follower accounts")
    print("  ✅ Signal - Master signals")
    print("  ✅ SignalDelivery - Delivery tracking")
    print("  ✅ Trade - Completed trades")
    print("  ✅ Payment - Payment records")
    print("  ✅ SystemStats - System metrics")
    print("\nReady to deploy to Railway!")
