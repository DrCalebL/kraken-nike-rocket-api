"""
Nike Rocket Follower System - API Endpoints
============================================

FastAPI endpoints for managing followers, signals, and payments.

Endpoints:
- POST /api/broadcast-signal - Receive signals from master algo
- GET /api/latest-signal - Followers poll for new signals
- POST /api/report-pnl - Followers report trade results
- POST /api/users/register - New user signup
- GET /api/users/verify - Verify user access
- GET /api/users/stats - Get user statistics
- POST /api/setup-agent - Setup hosted trading agent (NEW!)
- GET /api/agent-status - Get agent status (NEW!)
- POST /api/stop-agent - Stop trading agent (NEW!)
- POST /api/payments/create - Create payment link
- POST /api/payments/webhook - Coinbase Commerce webhook

Author: Nike Rocket Team
Updated: November 21, 2025
"""

from fastapi import APIRouter, HTTPException, Header, Depends, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from typing import Optional, List
import os
import secrets
import hashlib
import hmac
import json
import logging
from pydantic import BaseModel, EmailStr

from follower_models import (
    User, Signal, SignalDelivery, Trade, Payment, SystemStats,
    get_db_session
)

# Initialize logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize router
router = APIRouter()

# Environment variables
MASTER_API_KEY = os.getenv("MASTER_API_KEY", "your-master-key-here")
COINBASE_WEBHOOK_SECRET = os.getenv("COINBASE_WEBHOOK_SECRET", "")
COINBASE_API_KEY = os.getenv("COINBASE_COMMERCE_API_KEY", "")

# Signal expiration settings
SIGNAL_EXPIRATION_MINUTES = 15  # Signals expire after 15 minutes


# ==================== REQUEST MODELS ====================

class SignalBroadcast(BaseModel):
    """Signal from master algorithm"""
    action: str  # BUY or SELL
    symbol: str  # ADA/USDT
    entry_price: float
    stop_loss: float
    take_profit: float
    leverage: float
    timeframe: Optional[str] = None
    trend_strength: Optional[float] = None
    volatility: Optional[float] = None
    notes: Optional[str] = None


class TradeReport(BaseModel):
    """Trade result from follower agent"""
    trade_id: str
    signal_id: Optional[str] = None
    kraken_order_id: Optional[str] = None
    
    opened_at: str  # ISO datetime
    closed_at: str  # ISO datetime
    
    symbol: str
    side: str  # BUY or SELL
    
    entry_price: float
    exit_price: float
    position_size: float
    leverage: float
    
    profit_usd: float
    profit_percent: Optional[float] = None
    notes: Optional[str] = None


class UserRegistration(BaseModel):
    """New user signup"""
    email: EmailStr
    kraken_account_id: Optional[str] = None


class PaymentCreate(BaseModel):
    """Create payment charge"""
    amount: float
    for_month: str  # "2025-11"


class ExecutionConfirmation(BaseModel):
    """Confirm signal execution"""
    delivery_id: int


class SetupAgentRequest(BaseModel):
    """Request to setup trading agent with Kraken credentials (NEW!)"""
    kraken_api_key: str
    kraken_api_secret: str


# ==================== DEPENDENCY INJECTION ====================

def get_db():
    """Database session dependency"""
    from sqlalchemy import create_engine
    DATABASE_URL = os.getenv("DATABASE_URL")
    if not DATABASE_URL:
        raise Exception("DATABASE_URL not set")
    
    # Handle Railway postgres:// to postgresql://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    
    engine = create_engine(DATABASE_URL)
    session = get_db_session(engine)
    try:
        yield session
    finally:
        session.close()


def verify_master_key(x_master_key: str = Header(None)):
    """Verify master API key from broadcasting algo"""
    if x_master_key != MASTER_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid master API key")
    return True


def verify_user_key(x_api_key: str = Header(None), db: Session = Depends(get_db)):
    """Verify user API key and return user"""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="API key required")
    
    user = db.query(User).filter(User.api_key == x_api_key).first()
    if not user:
        raise HTTPException(status_code=404, detail="Invalid API key")
    
    return user


# ==================== SIGNAL ENDPOINTS ====================

@router.post("/api/broadcast-signal")
async def broadcast_signal(
    signal: SignalBroadcast,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_master_key)
):
    """
    Receive signal from master algorithm
    
    Called by: Your Kraken algo when it opens a position
    Auth: Requires MASTER_API_KEY
    """
    try:
        # Generate signal ID
        signal_id = secrets.token_urlsafe(16)
        
        # Store signal in database
        db_signal = Signal(
            signal_id=signal_id,
            action=signal.action,
            symbol=signal.symbol,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            leverage=signal.leverage,
            timeframe=signal.timeframe,
            trend_strength=signal.trend_strength,
            volatility=signal.volatility,
            notes=signal.notes
        )
        db.add(db_signal)
        db.commit()
        db.refresh(db_signal)
        
        # Get all active users
        active_users = db.query(User).filter(
            User.access_granted == True
        ).all()
        
        # Create delivery records
        for user in active_users:
            delivery = SignalDelivery(
                signal_id=db_signal.id,
                user_id=user.id
            )
            db.add(delivery)
        
        db.commit()
        
        logger.info(f"📡 Signal broadcast: {signal.action} on {signal.symbol}")
        logger.info(f"   Delivered to {len(active_users)} active followers")
        logger.info(f"   ⏰ Expires in {SIGNAL_EXPIRATION_MINUTES} minutes")
        
        return {
            "status": "success",
            "signal_id": signal_id,
            "delivered_to": len(active_users),
            "expires_in_minutes": SIGNAL_EXPIRATION_MINUTES,
            "timestamp": datetime.utcnow().isoformat()
        }
    
    except Exception as e:
        logger.error(f"❌ Error broadcasting signal: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/latest-signal")
async def get_latest_signal(
    user: User = Depends(verify_user_key),
    db: Session = Depends(get_db)
):
    """
    Get latest signal for follower
    
    Called by: Follower agents every 10 seconds
    Auth: Requires user API key
    Returns: Latest unacknowledged signal, or null
    """
    try:
        # Check if user has access
        if not user.access_granted:
            return {
                "access_granted": False,
                "reason": user.suspension_reason or "Payment required",
                "amount_due": user.monthly_fee_due
            }
        
        # Get latest unacknowledged signal for this user
        delivery = db.query(SignalDelivery).join(Signal).filter(
            SignalDelivery.user_id == user.id,
            SignalDelivery.acknowledged == False
        ).order_by(Signal.created_at.desc()).first()
        
        if not delivery:
            return {
                "access_granted": True,
                "signal": None,
                "message": "No new signals"
            }
        
        # Check if signal is expired (older than 15 minutes)
        signal_age_seconds = (datetime.utcnow() - delivery.signal.created_at).total_seconds()
        signal_age_minutes = signal_age_seconds / 60
        
        if signal_age_minutes > SIGNAL_EXPIRATION_MINUTES:
            # Signal is too old - mark as acknowledged to prevent execution
            delivery.acknowledged = True
            db.commit()
            
            logger.info(f"⚠️ Signal expired and skipped:")
            logger.info(f"   Signal ID: {delivery.signal.signal_id}")
            logger.info(f"   Symbol: {delivery.signal.symbol}")
            logger.info(f"   Age: {signal_age_minutes:.1f} minutes")
            
            return {
                "access_granted": True,
                "signal": None,
                "message": f"Signal expired ({signal_age_minutes:.0f} min old)"
            }
        
        # Return signal for execution
        return {
            "access_granted": True,
            "signal": {
                "signal_id": delivery.signal.signal_id,
                "delivery_id": delivery.id,
                "action": delivery.signal.action,
                "symbol": delivery.signal.symbol,
                "entry_price": delivery.signal.entry_price,
                "stop_loss": delivery.signal.stop_loss,
                "take_profit": delivery.signal.take_profit,
                "leverage": delivery.signal.leverage,
                "timeframe": delivery.signal.timeframe,
                "trend_strength": delivery.signal.trend_strength,
                "volatility": delivery.signal.volatility,
                "notes": delivery.signal.notes,
                "created_at": delivery.signal.created_at.isoformat(),
                "age_seconds": int(signal_age_seconds)
            }
        }
    
    except Exception as e:
        logger.error(f"❌ Error fetching signal: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/acknowledge-signal")
async def acknowledge_signal(
    data: ExecutionConfirmation,
    user: User = Depends(verify_user_key),
    db: Session = Depends(get_db)
):
    """
    Acknowledge signal receipt/execution
    
    Called by: Follower agent after executing signal
    Auth: Requires user API key
    """
    try:
        # Find delivery
        delivery = db.query(SignalDelivery).filter(
            SignalDelivery.id == data.delivery_id,
            SignalDelivery.user_id == user.id
        ).first()
        
        if not delivery:
            raise HTTPException(status_code=404, detail="Delivery not found")
        
        # Mark as acknowledged
        delivery.acknowledged = True
        delivery.acknowledged_at = datetime.utcnow()
        
        db.commit()
        
        logger.info(f"✓ Signal acknowledged by {user.email}")
        
        return {
            "status": "success",
            "message": "Signal acknowledged"
        }
    
    except Exception as e:
        logger.error(f"❌ Error acknowledging signal: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== TRADE REPORTING ====================

@router.post("/api/report-pnl")
async def report_pnl(
    trade: TradeReport,
    user: User = Depends(verify_user_key),
    db: Session = Depends(get_db)
):
    """
    Report trade result from follower
    
    Called by: Follower agent after closing trade
    Auth: Requires user API key
    """
    try:
        # Parse timestamps
        opened_at = datetime.fromisoformat(trade.opened_at.replace('Z', '+00:00'))
        closed_at = datetime.fromisoformat(trade.closed_at.replace('Z', '+00:00'))
        
        # Calculate 10% fee on profitable trades
        fee_charged = max(0, trade.profit_usd * 0.10) if trade.profit_usd > 0 else 0.0
        
        # Store trade record
        db_trade = Trade(
            user_id=user.id,
            trade_id=trade.trade_id,
            kraken_order_id=trade.kraken_order_id,
            opened_at=opened_at,
            closed_at=closed_at,
            symbol=trade.symbol,
            side=trade.side,
            entry_price=trade.entry_price,
            exit_price=trade.exit_price,
            position_size=trade.position_size,
            leverage=trade.leverage,
            profit_usd=trade.profit_usd,
            profit_percent=trade.profit_percent,
            fee_charged=fee_charged,
            notes=trade.notes
        )
        db.add(db_trade)
        
        # Update user stats
        user.monthly_profit += trade.profit_usd
        user.monthly_trades += 1
        user.total_profit += trade.profit_usd
        user.total_trades += 1
        
        # Calculate fee due (10% of profits)
        if trade.profit_usd > 0:
            user.monthly_fee_due += fee_charged
        
        db.commit()
        
        logger.info(f"💰 Trade reported by {user.email}:")
        logger.info(f"   Symbol: {trade.symbol}")
        logger.info(f"   Profit: ${trade.profit_usd:.2f}")
        logger.info(f"   Fee: ${fee_charged:.2f}")
        
        return {
            "status": "success",
            "trade_id": db_trade.id,
            "profit_usd": trade.profit_usd,
            "fee_charged": fee_charged,
            "monthly_profit": user.monthly_profit,
            "monthly_fee_due": user.monthly_fee_due
        }
    
    except Exception as e:
        logger.error(f"❌ Error reporting trade: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== USER MANAGEMENT ====================

@router.post("/api/users/register")
async def register_user(
    data: UserRegistration,
    db: Session = Depends(get_db)
):
    """
    Register new user
    
    Called by: Signup form
    Public: No auth required
    """
    try:
        # Check if email already exists
        existing = db.query(User).filter(User.email == data.email).first()
        if existing:
            raise HTTPException(status_code=400, detail="Email already registered")
        
        # Generate API key
        api_key = f"nk_{secrets.token_urlsafe(32)}"
        
        # Create user
        user = User(
            email=data.email,
            api_key=api_key,
            kraken_account_id=data.kraken_account_id,
            access_granted=True,  # Grant access immediately
            monthly_fee_paid=True  # First month free
        )
        
        db.add(user)
        db.commit()
        db.refresh(user)
        
        logger.info(f"✅ New user registered: {data.email}")
        
        return {
            "status": "success",
            "message": "Account created successfully",
            "api_key": api_key,
            "email": data.email,
            "access_granted": True
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error registering user: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/users/verify")
async def verify_user(
    user: User = Depends(verify_user_key),
    db: Session = Depends(get_db)
):
    """
    Verify user access status
    
    Called by: Follower agent on startup
    Auth: Requires user API key
    """
    # Check payment status
    payment_ok = user.check_payment_status()
    
    if not payment_ok and user.access_granted:
        # Suspend user for non-payment
        user.access_granted = False
        user.suspended_at = datetime.utcnow()
        user.suspension_reason = "Monthly fee overdue"
        db.commit()
        logger.warning(f"⚠️ User suspended for non-payment: {user.email}")
    
    return {
        "access_granted": user.access_granted,
        "email": user.email,
        "monthly_profit": user.monthly_profit,
        "monthly_fee_due": user.monthly_fee_due,
        "monthly_fee_paid": user.monthly_fee_paid,
        "suspension_reason": user.suspension_reason if not user.access_granted else None
    }


@router.get("/api/users/stats")
async def get_user_stats(
    user: User = Depends(verify_user_key),
    db: Session = Depends(get_db)
):
    """
    Get user statistics
    
    Called by: User dashboard or follower agent
    Auth: Requires user API key
    """
    # Get recent trades
    recent_trades = db.query(Trade).filter(
        Trade.user_id == user.id
    ).order_by(Trade.closed_at.desc()).limit(10).all()
    
    return {
        "email": user.email,
        "access_granted": user.access_granted,
        
        # Monthly stats
        "monthly_profit": user.monthly_profit,
        "monthly_trades": user.monthly_trades,
        "monthly_fee_due": user.monthly_fee_due,
        "monthly_fee_paid": user.monthly_fee_paid,
        
        # All-time stats
        "total_profit": user.total_profit,
        "total_trades": user.total_trades,
        "total_fees_paid": user.total_fees_paid,
        
        # Recent trades
        "recent_trades": [
            {
                "trade_id": trade.trade_id,
                "symbol": trade.symbol,
                "profit": trade.profit_usd,
                "closed_at": trade.closed_at.isoformat()
            }
            for trade in recent_trades
        ]
    }


# ==================== HOSTED AGENT SETUP (NEW!) ====================

@router.post("/api/setup-agent")
async def setup_agent(
    data: SetupAgentRequest,
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: Session = Depends(get_db)
):
    """
    Setup customer's trading agent with Kraken credentials
    
    Called by: /setup page after customer enters credentials
    Auth: Requires user API key
    
    This endpoint:
    1. Encrypts and stores Kraken credentials
    2. Marks user as ready for agent activation
    3. Multi-agent manager will pick them up automatically
    """
    
    # Find user
    user = db.query(User).filter(User.api_key == x_api_key).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    # Validate Kraken credentials format
    if not data.kraken_api_key or not data.kraken_api_secret:
        raise HTTPException(status_code=400, detail="Both API key and secret required")
    
    if len(data.kraken_api_key) < 10:
        raise HTTPException(status_code=400, detail="Invalid Kraken API key format")
    
    if len(data.kraken_api_secret) < 20:
        raise HTTPException(status_code=400, detail="Invalid Kraken API secret format")
    
    try:
        # Encrypt and store credentials
        user.set_kraken_credentials(data.kraken_api_key, data.kraken_api_secret)
        
        # Mark as ready
        user.credentials_set = True
        
        # Ensure access is granted
        if not user.access_granted:
            user.access_granted = True
            user.suspended_at = None
            user.suspension_reason = None
        
        db.commit()
        
        logger.info(f"✅ Credentials set for user: {user.email}")
        logger.info(f"   Agent will start automatically within 5 minutes")
        
        return {
            "status": "success",
            "message": "Trading agent configured successfully",
            "agent_status": "starting",
            "note": "Your agent will start automatically within 5 minutes",
            "email": user.email
        }
        
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Error setting up agent: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to setup agent: {str(e)}")


@router.get("/api/agent-status")
async def get_agent_status(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: Session = Depends(get_db)
):
    """
    Get customer's agent status
    
    Called by: Dashboard to show if agent is running
    Auth: Requires user API key
    """
    
    # Find user
    user = db.query(User).filter(User.api_key == x_api_key).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    # Check if credentials are set
    if not user.credentials_set:
        return {
            "agent_configured": False,
            "agent_active": False,
            "message": "Agent not configured. Please set up your Kraken credentials.",
            "setup_url": "/setup"
        }
    
    # Check if agent is active
    return {
        "agent_configured": True,
        "agent_active": user.agent_active,
        "agent_started_at": user.agent_started_at.isoformat() if user.agent_started_at else None,
        "agent_last_poll": user.agent_last_poll.isoformat() if user.agent_last_poll else None,
        "access_granted": user.access_granted,
        "email": user.email
    }


@router.post("/api/stop-agent")
async def stop_agent(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: Session = Depends(get_db)
):
    """
    Stop customer's trading agent
    
    Called by: User dashboard when they want to pause trading
    Auth: Requires user API key
    """
    
    # Find user
    user = db.query(User).filter(User.api_key == x_api_key).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    # Mark agent as inactive
    user.agent_active = False
    
    # Clear credentials (for security)
    user.kraken_api_key_encrypted = None
    user.kraken_api_secret_encrypted = None
    user.credentials_set = False
    
    db.commit()
    
    logger.info(f"🛑 Agent stopped for user: {user.email}")
    
    return {
        "status": "success",
        "message": "Trading agent stopped",
        "agent_active": False
    }


# ==================== PAYMENT ENDPOINTS ====================

@router.get("/api/pay/{api_key}")
async def create_payment_page(
    api_key: str,
    db: Session = Depends(get_db)
):
    """
    Generate payment page for user
    
    Called by: User clicking payment link
    Public: No auth required (uses API key in URL)
    """
    # Find user
    user = db.query(User).filter(User.api_key == api_key).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Check if payment needed
    if user.monthly_fee_due <= 0:
        return {
            "message": "No payment due",
            "monthly_profit": user.monthly_profit,
            "access_granted": user.access_granted
        }
    
    # Create Coinbase Commerce charge
    try:
        import requests
        
        response = requests.post(
            "https://api.commerce.coinbase.com/charges",
            json={
                "name": "Nike Rocket - Monthly Fee",
                "description": f"10% profit sharing for {user.email}",
                "pricing_type": "fixed_price",
                "local_price": {
                    "amount": str(user.monthly_fee_due),
                    "currency": "USD"
                },
                "metadata": {
                    "user_id": user.id,
                    "user_email": user.email,
                    "api_key": api_key,
                    "for_month": datetime.utcnow().strftime("%Y-%m"),
                    "profit_amount": user.monthly_profit
                }
            },
            headers={
                "X-CC-Api-Key": COINBASE_API_KEY,
                "X-CC-Version": "2018-03-22"
            }
        )
        
        if response.status_code == 201:
            charge = response.json()["data"]
            
            # Store payment record
            payment = Payment(
                user_id=user.id,
                amount_usd=user.monthly_fee_due,
                currency="USD",
                coinbase_charge_id=charge["id"],
                status="pending",
                for_month=datetime.utcnow().strftime("%Y-%m"),
                profit_amount=user.monthly_profit
            )
            db.add(payment)
            db.commit()
            
            return {
                "payment_url": charge["hosted_url"],
                "amount": user.monthly_fee_due,
                "for_month": datetime.utcnow().strftime("%Y-%m"),
                "profit": user.monthly_profit
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to create payment")
    
    except Exception as e:
        logger.error(f"❌ Error creating payment: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/payments/webhook")
async def coinbase_webhook(
    request: dict,
    x_cc_webhook_signature: str = Header(None),
    db: Session = Depends(get_db)
):
    """
    Coinbase Commerce webhook
    
    Called by: Coinbase when payment completes
    Auth: Webhook signature verification
    """
    try:
        # Verify webhook signature
        if COINBASE_WEBHOOK_SECRET:
            payload = json.dumps(request)
            signature = hmac.new(
                COINBASE_WEBHOOK_SECRET.encode(),
                payload.encode(),
                hashlib.sha256
            ).hexdigest()
            
            if not hmac.compare_digest(signature, x_cc_webhook_signature or ""):
                raise HTTPException(status_code=401, detail="Invalid signature")
        
        # Process payment event
        event = request.get("event", {})
        event_type = event.get("type")
        
        if event_type == "charge:confirmed":
            # Payment completed
            charge = event.get("data", {})
            metadata = charge.get("metadata", {})
            
            user_id = metadata.get("user_id")
            if not user_id:
                logger.warning("⚠️ Payment webhook missing user_id")
                return {"status": "ignored"}
            
            # Find user
            user = db.query(User).filter(User.id == int(user_id)).first()
            if not user:
                logger.warning(f"⚠️ User not found: {user_id}")
                return {"status": "user_not_found"}
            
            # Update payment record
            payment = db.query(Payment).filter(
                Payment.coinbase_charge_id == charge["id"]
            ).first()
            
            if payment:
                payment.status = "completed"
                payment.completed_at = datetime.utcnow()
                payment.tx_hash = charge.get("payments", [{}])[0].get("transaction_id")
            
            # Mark user as paid and restore access
            user.monthly_fee_paid = True
            user.total_fees_paid += user.monthly_fee_due
            user.access_granted = True
            user.suspended_at = None
            user.suspension_reason = None
            
            db.commit()
            
            logger.info(f"✅ Payment confirmed for {user.email}")
            logger.info(f"   Amount: ${user.monthly_fee_due:.2f}")
            logger.info(f"   Access restored!")
            
            return {"status": "processed"}
        
        return {"status": "ignored"}
    
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")
        return {"status": "error", "message": str(e)}


# ==================== ADMIN ENDPOINTS ====================

@router.get("/api/admin/stats")
async def get_system_stats(
    db: Session = Depends(get_db),
    _: bool = Depends(verify_master_key)
):
    """
    Get system-wide statistics
    
    Called by: Admin dashboard
    Auth: Requires MASTER_API_KEY
    """
    from sqlalchemy import func
    
    total_users = db.query(User).count()
    active_users = db.query(User).filter(User.access_granted == True).count()
    suspended_users = db.query(User).filter(User.access_granted == False).count()
    
    total_trades = db.query(Trade).count()
    total_profit = db.query(func.sum(Trade.profit_usd)).scalar() or 0
    total_fees = db.query(func.sum(Trade.fee_charged)).scalar() or 0
    
    total_signals = db.query(Signal).count()
    
    return {
        "users": {
            "total": total_users,
            "active": active_users,
            "suspended": suspended_users
        },
        "trading": {
            "total_signals": total_signals,
            "total_trades": total_trades,
            "total_profit": total_profit,
            "total_fees_collected": total_fees
        },
        "updated_at": datetime.utcnow().isoformat()
    }


# Export router
__all__ = ["router"]
