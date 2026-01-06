"""
Nike Rocket Follower System - Database Models (WITH ENCRYPTED CREDENTIALS)
==========================================================================

Updated models that support:
- Encrypted storage of Kraken API credentials
- Agent status tracking (active/stopped)
- Multi-agent support
- Risk percentage per signal (2% or 3%)

Author: Nike Rocket Team
Updated: November 24, 2025
"""

from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker
from datetime import datetime, timedelta
from cryptography.fernet import Fernet
import os

from config import get_fee_rate, get_tier_display

Base = declarative_base()

# Encryption key for credentials (store in environment variable!)
ENCRYPTION_KEY = os.getenv("CREDENTIALS_ENCRYPTION_KEY")
if ENCRYPTION_KEY:
    cipher = Fernet(ENCRYPTION_KEY.encode())
else:
    # Generate new key if not set (for first time setup)
    cipher = None


class User(Base):
    """
    Follower user model - WITH ENCRYPTED KRAKEN CREDENTIALS
    """
    __tablename__ = "follower_users"
    
    id = Column(Integer, primary_key=True, index=True)
    api_key = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, unique=True, nullable=False)
    
    # User tier for fee rates
    # 'team' = 0% fees (team members)
    # 'vip' = 5% fees (VIP customers)
    # 'standard' = 10% fees (typical customers)
    fee_tier = Column(String, default='standard')
    
    # Encrypted Kraken credentials
    kraken_api_key_encrypted = Column(Text, nullable=True)
    kraken_api_secret_encrypted = Column(Text, nullable=True)
    credentials_set = Column(Boolean, default=False)
    
    # Agent status
    agent_active = Column(Boolean, default=False)
    agent_started_at = Column(DateTime, nullable=True)
    agent_last_poll = Column(DateTime, nullable=True)
    
    # Access control
    access_granted = Column(Boolean, default=True)
    suspended_at = Column(DateTime, nullable=True)
    suspension_reason = Column(String, nullable=True)
    
    # Account tracking
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)
    
    # 30-Day Rolling Billing (current cycle)
    billing_cycle_start = Column(DateTime, nullable=True)
    current_cycle_profit = Column(Float, default=0.0)
    current_cycle_trades = Column(Integer, default=0)
    
    # Pending invoice (after cycle ends)
    pending_invoice_id = Column(String, nullable=True)
    pending_invoice_amount = Column(Float, default=0.0)
    invoice_due_date = Column(DateTime, nullable=True)
    
    # Tier change scheduling
    next_cycle_fee_tier = Column(String, nullable=True)
    
    # All-time tracking
    total_profit = Column(Float, default=0.0)
    total_trades = Column(Integer, default=0)
    total_fees_paid = Column(Float, default=0.0)
    
    # Kraken account info
    kraken_account_id = Column(String, nullable=True)
    
    # Relationships
    signal_deliveries = relationship("SignalDelivery", back_populates="user")
    trades = relationship("Trade", back_populates="user")
    payments = relationship("Payment", back_populates="user")
    
    def set_kraken_credentials(self, api_key: str, api_secret: str):
        """Encrypt and store Kraken credentials"""
        if not cipher:
            raise Exception("Encryption key not configured")
        
        self.kraken_api_key_encrypted = cipher.encrypt(api_key.encode()).decode()
        self.kraken_api_secret_encrypted = cipher.encrypt(api_secret.encode()).decode()
        self.credentials_set = True
    
    def get_kraken_credentials(self):
        """Decrypt and return Kraken credentials"""
        if not self.credentials_set or not cipher:
            return None, None
        
        try:
            api_key = cipher.decrypt(self.kraken_api_key_encrypted.encode()).decode()
            api_secret = cipher.decrypt(self.kraken_api_secret_encrypted.encode()).decode()
            return api_key, api_secret
        except Exception:
            return None, None
    
    def check_payment_status(self) -> bool:
        """
        Check if user has access (30-day billing system)
        
        Returns True if:
        - No pending invoice, OR
        - Pending invoice exists but not yet overdue
        
        Returns False if:
        - Pending invoice is overdue (past invoice_due_date)
        """
        # If no pending invoice, user has access
        if not self.pending_invoice_id:
            return True
        
        # If invoice exists but no due date set, allow access (shouldn't happen)
        if not self.invoice_due_date:
            return True
        
        # Check if invoice is overdue
        from config import utc_now
        now = utc_now()
        
        # Make due_date timezone-aware for comparison
        due_date = self.invoice_due_date
        if due_date.tzinfo is None:
            from datetime import timezone
            due_date = due_date.replace(tzinfo=timezone.utc)
        
        # If past due date, access is revoked
        return now <= due_date
    
    @property
    def fee_percentage(self) -> float:
        """Get fee percentage based on user tier"""
        return get_fee_rate(self.fee_tier)
    
    @property
    def fee_tier_display(self) -> str:
        """Get display name for fee tier"""
        return get_tier_display(self.fee_tier)


class Signal(Base):
    """Trading signal from master algorithm"""
    __tablename__ = "signals"
    
    id = Column(Integer, primary_key=True, index=True)
    signal_id = Column(String, unique=True, index=True, nullable=False)
    
    # Signal details
    action = Column(String, nullable=False)  # BUY or SELL
    symbol = Column(String, nullable=False)
    entry_price = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=False)
    take_profit = Column(Float, nullable=False)
    leverage = Column(Float, default=1.0)
    
    # Risk percentage (0.02 = 2% aggressive, 0.03 = 3% conservative)
    risk_pct = Column(Float, default=0.02)
    
    # Market context
    timeframe = Column(String, nullable=True)
    trend_strength = Column(Float, nullable=True)
    volatility = Column(Float, nullable=True)
    notes = Column(String, nullable=True)
    
    # Timing
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    expires_at = Column(DateTime, nullable=True)
    
    # Relationships
    deliveries = relationship("SignalDelivery", back_populates="signal")


class SignalDelivery(Base):
    """Tracks signal delivery to each user"""
    __tablename__ = "signal_deliveries"
    
    id = Column(Integer, primary_key=True, index=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("follower_users.id"), nullable=False)
    
    # Delivery tracking
    delivered_at = Column(DateTime, default=datetime.utcnow)
    acknowledged = Column(Boolean, default=False)
    acknowledged_at = Column(DateTime, nullable=True)
    
    # Execution tracking
    executed = Column(Boolean, default=False)
    executed_at = Column(DateTime, nullable=True)
    execution_price = Column(Float, nullable=True)
    
    # Failure tracking
    failed = Column(Boolean, default=False)
    failure_reason = Column(String, nullable=True)
    retry_count = Column(Integer, default=0)
    
    # Relationships
    signal = relationship("Signal", back_populates="deliveries")
    user = relationship("User", back_populates="signal_deliveries")


class OpenPosition(Base):
    """
    Tracks open positions waiting for TP or SL to fill.
    Used by position monitor to calculate actual P&L.
    """
    __tablename__ = "open_positions"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("follower_users.id"), nullable=False)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True)
    
    # Kraken order IDs for tracking
    entry_order_id = Column(String, nullable=False)
    tp_order_id = Column(String, nullable=True)
    sl_order_id = Column(String, nullable=True)
    
    # Position details
    symbol = Column(String, nullable=False)  # BTC/USDT format
    kraken_symbol = Column(String, nullable=False)  # PF_XBTUSD format
    side = Column(String, nullable=False)  # BUY or SELL
    quantity = Column(Float, nullable=False)
    leverage = Column(Float, default=1.0)
    
    # Actual fill price from Kraken (may differ from signal due to slippage)
    entry_fill_price = Column(Float, nullable=True)
    
    # Target prices from signal
    target_tp = Column(Float, nullable=False)
    target_sl = Column(Float, nullable=False)
    
    # Timing
    opened_at = Column(DateTime, default=datetime.utcnow)
    
    # Status
    status = Column(String, default='open')  # open, closed, error
    
    # Relationships
    user = relationship("User")


class Trade(Base):
    """Completed trade record"""
    __tablename__ = "trades"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("follower_users.id"), nullable=False)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True)
    
    # Trade identifiers
    trade_id = Column(String, unique=True, nullable=False)
    kraken_order_id = Column(String, nullable=True)
    
    # Timing
    opened_at = Column(DateTime, nullable=False)
    closed_at = Column(DateTime, nullable=False, index=True)
    
    # Trade details
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)  # BUY or SELL
    
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=False)
    position_size = Column(Float, nullable=False)
    leverage = Column(Float, default=1.0)
    
    # P&L
    profit_usd = Column(Float, nullable=False)
    profit_percent = Column(Float, nullable=True)
    
    # Fee tracking (10% of profit)
    fee_charged = Column(Float, default=0.0)
    
    # Notes
    notes = Column(String, nullable=True)
    
    # Relationships
    user = relationship("User", back_populates="trades")


class Payment(Base):
    """Payment record"""
    __tablename__ = "payments"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("follower_users.id"), nullable=False)
    
    # Payment details
    amount_usd = Column(Float, nullable=False)
    currency = Column(String, default="USD")
    
    # Coinbase Commerce
    coinbase_charge_id = Column(String, unique=True, nullable=True)
    status = Column(String, default="pending")  # pending, completed, failed
    
    # Timing
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    
    # Context
    for_month = Column(String, nullable=True)  # "2025-11"
    profit_amount = Column(Float, default=0.0)
    
    # Transaction details
    tx_hash = Column(String, nullable=True)
    
    # Relationships
    user = relationship("User", back_populates="payments")


class SystemStats(Base):
    """System-wide statistics snapshot"""
    __tablename__ = "system_stats"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # Counts
    total_users = Column(Integer, default=0)
    active_users = Column(Integer, default=0)
    suspended_users = Column(Integer, default=0)
    
    # Trading
    total_signals = Column(Integer, default=0)
    total_trades = Column(Integer, default=0)
    total_profit = Column(Float, default=0.0)
    total_fees_collected = Column(Float, default=0.0)
    
    # Timestamp
    snapshot_at = Column(DateTime, default=datetime.utcnow, index=True)


def get_db_session(engine):
    """Get database session"""
    Session = sessionmaker(bind=engine)
    return Session()


def init_db(engine):
    """Initialize database tables"""
    Base.metadata.create_all(bind=engine)
    print("✅ Database tables created successfully")
    
    # Check schema
    from sqlalchemy import inspect
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    
    required_tables = [
        'follower_users', 'signals', 'signal_deliveries', 
        'trades', 'payments', 'system_stats', 'open_positions'
    ]
    
    missing = [t for t in required_tables if t not in tables]
    if missing:
        print(f"⚠️ Missing tables: {missing}")
    else:
        print("✅ Database schema up to date")


# Export everything
__all__ = [
    'Base', 'User', 'Signal', 'SignalDelivery', 'OpenPosition', 'Trade', 
    'Payment', 'SystemStats', 'get_db_session', 'init_db'
]
