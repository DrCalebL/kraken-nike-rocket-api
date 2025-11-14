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
- POST /api/payments/create - Create payment link
- POST /api/payments/webhook - Coinbase Commerce webhook

Author: Nike Rocket Team
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
from pydantic import BaseModel, EmailStr

from follower_models import (
    User, Signal, SignalDelivery, Trade, Payment, SystemStats,
    get_db_session
)

# Initialize router
router = APIRouter()

# Environment variables
MASTER_API_KEY = os.getenv("MASTER_API_KEY", "your-master-key-here")
COINBASE_WEBHOOK_SECRET = os.getenv("COINBASE_WEBHOOK_SECRET", "")
COINBASE_API_KEY = os.getenv("COINBASE_COMMERCE_API_KEY", "")


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
        
        print(f"📡 Signal broadcast: {signal.action} on {signal.symbol}")
        print(f"   Delivered to {len(active_users)} active followers")
        
        return {
            "status": "success",
            "signal_id": signal_id,
            "delivered_to": len(active_users),
            "timestamp": datetime.utcnow().isoformat()
        }
    
    except Exception as e:
        print(f"❌ Error broadcasting signal: {e}")
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
        
        # Mark as acknowledged
        delivery.acknowledged = True
        db.commit()
        
        # Return signal details
        signal = delivery.signal
        return {
            "access_granted": True,
            "signal": {
                "signal_id": signal.signal_id,
                "action": signal.action,
                "symbol": signal.symbol,
                "entry_price": signal.entry_price,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "leverage": signal.leverage,
                "timeframe": signal.timeframe,
                "created_at": signal.created_at.isoformat()
            }
        }
    
    except Exception as e:
        print(f"❌ Error fetching signal: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== TRADE REPORTING ====================

@router.post("/api/report-pnl")
async def report_pnl(
    trade: TradeReport,
    user: User = Depends(verify_user_key),
    db: Session = Depends(get_db)
):
    """
    Receive trade result from follower
    
    Called by: Follower agent after position closes
    Auth: Requires user API key
    """
    try:
        # Create trade record
        db_trade = Trade(
            user_id=user.id,
            signal_id=trade.signal_id,
            trade_id=trade.trade_id,
            kraken_order_id=trade.kraken_order_id,
            opened_at=datetime.fromisoformat(trade.opened_at),
            closed_at=datetime.fromisoformat(trade.closed_at),
            symbol=trade.symbol,
            side=trade.side,
            entry_price=trade.entry_price,
            exit_price=trade.exit_price,
            position_size=trade.position_size,
            leverage=trade.leverage,
            profit_usd=trade.profit_usd,
            profit_percent=trade.profit_percent,
            notes=trade.notes
        )
        
        # Calculate fee (10% of profit if positive)
        fee = db_trade.calculate_fee()
        db.add(db_trade)
        
        # Update user stats
        user.monthly_profit += trade.profit_usd
        user.monthly_trades += 1
        user.total_profit += trade.profit_usd
        user.total_trades += 1
        
        # Calculate new monthly fee
        user.calculate_monthly_fee()
        
        db.commit()
        
        print(f"💰 P&L reported by {user.email}")
        print(f"   Trade profit: ${trade.profit_usd:.2f}")
        print(f"   Fee charged: ${fee:.2f}")
        print(f"   Monthly total: ${user.monthly_profit:.2f}")
        
        return {
            "status": "recorded",
            "trade_id": trade.trade_id,
            "profit": trade.profit_usd,
            "fee_charged": fee,
            "monthly_profit": user.monthly_profit,
            "monthly_fee_due": user.monthly_fee_due
        }
    
    except Exception as e:
        print(f"❌ Error recording trade: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== USER MANAGEMENT ====================

@router.post("/api/users/register")
async def register_user(
    registration: UserRegistration,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_master_key)
):
    """
    Register new follower user
    
    Called by: Your signup form/website
    Auth: Requires MASTER_API_KEY
    
    Flow: User registers → Verification email sent → User clicks link → API key shown
    """
    try:
        # Check if email already exists
        existing = db.query(User).filter(User.email == registration.email).first()
        if existing:
            # If already exists and verified, offer to resend API key via email
            if existing.verified:
                # Send API key to their email
                from email_service import send_api_key_email
                email_sent = send_api_key_email(existing.email, existing.api_key)
                
                if email_sent:
                    return {
                        "status": "key_resent",
                        "message": "Your account already exists! We've sent your API key to your email.",
                        "email": existing.email
                    }
                else:
                    # Email service not configured - show them the key in dev mode
                    return {
                        "status": "already_verified",
                        "message": "Email already registered. Email service not configured.",
                        "api_key": existing.api_key,
                        "note": "Save this key! In production, it would be emailed to you."
                    }
            
            # If exists but not verified, resend verification email
            if not existing.verified:
                # Generate new verification token
                verification_token = secrets.token_urlsafe(32)
                existing.verification_token = verification_token
                existing.verification_expires = datetime.utcnow() + timedelta(hours=1)
                db.commit()
                
                # Send verification email
                from email_service import send_verification_email
                email_sent = send_verification_email(existing.email, verification_token)
                
                if email_sent:
                    print(f"📧 Verification email resent to: {registration.email}")
                    return {
                        "status": "verification_sent",
                        "message": "Verification email sent! Check your inbox."
                    }
                else:
                    # If email service not configured, show token in response (development only)
                    print(f"⚠️ Email service not configured - showing verification link")
                    return {
                        "status": "verification_required",
                        "message": "Email service not configured",
                        "verification_link": f"/verify/{verification_token}"
                    }
            else:
                raise HTTPException(status_code=400, detail="Email already registered and verified. Please login or contact support.")
        
        # Create new user
        api_key = User.generate_api_key()
        verification_token = secrets.token_urlsafe(32)
        
        user = User(
            email=registration.email,
            api_key=api_key,
            kraken_account_id=registration.kraken_account_id,
            verified=False,
            verification_token=verification_token,
            verification_expires=datetime.utcnow() + timedelta(hours=1),
            access_granted=False  # Only grant after verification
        )
        
        db.add(user)
        db.commit()
        db.refresh(user)
        
        # Send verification email
        from email_service import send_verification_email
        email_sent = send_verification_email(user.email, verification_token)
        
        if email_sent:
            print(f"✅ New user registered: {registration.email}")
            print(f"📧 Verification email sent")
            
            return {
                "status": "verification_sent",
                "email": user.email,
                "message": "Verification email sent! Check your inbox to get your API key."
            }
        else:
            # If email service not configured, show token in response (development only)
            print(f"✅ New user registered: {registration.email}")
            print(f"⚠️ Email service not configured - showing verification link")
            
            return {
                "status": "verification_required",
                "email": user.email,
                "message": "Email service not configured. Use the verification link below:",
                "verification_link": f"/verify/{verification_token}"
            }
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error registering user: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/verify/{token}", response_class=HTMLResponse)
async def verify_email(
    token: str,
    db: Session = Depends(get_db)
):
    """
    Verify user email and show API key
    
    Called by: User clicking link in verification email
    Auth: Token-based (secure)
    """
    try:
        # Find user by token
        user = db.query(User).filter(
            User.verification_token == token,
            User.verification_expires > datetime.utcnow()
        ).first()
        
        if not user:
            # Token invalid or expired
            return HTMLResponse(
                content="""
                <!DOCTYPE html>
                <html>
                <head>
                    <title>Verification Failed</title>
                    <style>
                        body {
                            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                            display: flex;
                            align-items: center;
                            justify-content: center;
                            min-height: 100vh;
                            margin: 0;
                            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                        }
                        .container {
                            background: white;
                            border-radius: 20px;
                            padding: 40px;
                            max-width: 500px;
                            text-align: center;
                            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                        }
                        h1 { color: #ef4444; }
                        a {
                            display: inline-block;
                            margin-top: 20px;
                            padding: 12px 24px;
                            background: #667eea;
                            color: white;
                            text-decoration: none;
                            border-radius: 8px;
                        }
                    </style>
                </head>
                <body>
                    <div class="container">
                        <h1>❌ Verification Failed</h1>
                        <p>This verification link is invalid or has expired.</p>
                        <p>Verification links expire after 1 hour.</p>
                        <a href="/signup">Sign Up Again</a>
                    </div>
                </body>
                </html>
                """,
                status_code=400
            )
        
        # Mark user as verified
        user.verified = True
        user.access_granted = True
        user.verification_token = None
        user.verification_expires = None
        db.commit()
        
        # Optionally send API key via email for extra security
        from email_service import send_api_key_email
        send_api_key_email(user.email, user.api_key)
        
        print(f"✅ User verified: {user.email}")
        
        # Show API key on page
        return HTMLResponse(
            content=f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Email Verified!</title>
                <style>
                    body {{
                        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        min-height: 100vh;
                        margin: 0;
                        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                        padding: 20px;
                    }}
                    .container {{
                        background: white;
                        border-radius: 20px;
                        padding: 40px;
                        max-width: 600px;
                        box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                    }}
                    h1 {{ color: #10b981; text-align: center; }}
                    .warning {{
                        background: #fee2e2;
                        border-left: 4px solid #ef4444;
                        padding: 15px;
                        border-radius: 4px;
                        margin: 20px 0;
                    }}
                    .api-key {{
                        background: #f0f7ff;
                        border: 2px dashed #667eea;
                        padding: 15px;
                        border-radius: 8px;
                        font-family: 'Courier New', monospace;
                        font-size: 16px;
                        word-break: break-all;
                        margin: 20px 0;
                    }}
                    .btn {{
                        display: inline-block;
                        padding: 12px 24px;
                        background: #667eea;
                        color: white;
                        text-decoration: none;
                        border-radius: 8px;
                        margin: 5px;
                    }}
                    .btn-success {{
                        background: #10b981;
                    }}
                    .steps {{
                        background: #f9fafb;
                        padding: 20px;
                        border-radius: 8px;
                        margin: 20px 0;
                    }}
                    .steps ol {{
                        margin: 10px 0;
                        padding-left: 20px;
                    }}
                    .steps li {{
                        margin: 10px 0;
                    }}
                </style>
            </head>
            <body>
                <div class="container">
                    <h1>✅ Email Verified!</h1>
                    
                    <div class="warning">
                        <strong>⚠️ IMPORTANT:</strong> Save your API key NOW! This is the only time you'll see it on screen. 
                        We've also sent it to your email for safekeeping.
                    </div>
                    
                    <p><strong>Your API Key:</strong></p>
                    <div class="api-key" id="api-key">{user.api_key}</div>
                    
                    <button class="btn btn-success" onclick="copyKey()">📋 Copy API Key</button>
                    
                    <div class="steps">
                        <h3>🎯 Next Steps:</h3>
                        <ol>
                            <li>Copy your API key above (or check your email)</li>
                            <li>Click "Deploy to Render" below</li>
                            <li>Paste your API key when prompted</li>
                            <li>Enter your Kraken API credentials</li>
                            <li>Start receiving trading signals!</li>
                        </ol>
                        <div style="text-align: center; margin-top: 20px;">
                            <a href="https://render.com/deploy?repo=https://github.com/DrCalebL/kraken-follower-agent" class="btn btn-success">
                                Deploy to Render →
                            </a>
                        </div>
                    </div>
                </div>
                
                <script>
                    function copyKey() {{
                        const apiKey = document.getElementById('api-key').textContent;
                        navigator.clipboard.writeText(apiKey).then(() => {{
                            const btn = event.target;
                            btn.textContent = '✓ Copied!';
                            btn.style.background = '#10b981';
                            setTimeout(() => {{
                                btn.textContent = '📋 Copy API Key';
                                btn.style.background = '#10b981';
                            }}, 2000);
                        }});
                    }}
                </script>
            </body>
            </html>
            """,
            status_code=200
        )
    
    except Exception as e:
        print(f"❌ Error verifying email: {e}")
        return HTMLResponse(
            content=f"<h1>Error</h1><p>{str(e)}</p>",
            status_code=500
        )


@router.post("/api/users/register")
async def register_user(
    registration: UserRegistration,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_master_key)
):
    """
    Register new follower user
    
    Called by: Your signup form/website
    Auth: Requires MASTER_API_KEY
    """
    try:
        # Check if email already exists
        existing = db.query(User).filter(User.email == registration.email).first()
        if existing:
            raise HTTPException(status_code=400, detail="Email already registered")
        
        # Create new user
        api_key = User.generate_api_key()
        user = User(
            email=registration.email,
            api_key=api_key,
            kraken_account_id=registration.kraken_account_id,
            access_granted=True
        )
        
        db.add(user)
        db.commit()
        db.refresh(user)
        
        print(f"✅ New user registered: {registration.email}")
        
        return {
            "status": "success",
            "email": user.email,
            "api_key": api_key,
            "message": "User registered successfully"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error registering user: {e}")
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
        print(f"⚠️ User suspended for non-payment: {user.email}")
    
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
        print(f"❌ Error creating payment: {e}")
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
                print("⚠️ Payment webhook missing user_id")
                return {"status": "ignored"}
            
            # Find user
            user = db.query(User).filter(User.id == int(user_id)).first()
            if not user:
                print(f"⚠️ User not found: {user_id}")
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
            
            print(f"✅ Payment confirmed for {user.email}")
            print(f"   Amount: ${user.monthly_fee_due:.2f}")
            print(f"   Access restored!")
            
            return {"status": "processed"}
        
        return {"status": "ignored"}
    
    except Exception as e:
        print(f"❌ Webhook error: {e}")
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


# Import for func
from sqlalchemy import func

# Export router
__all__ = ["router"]
