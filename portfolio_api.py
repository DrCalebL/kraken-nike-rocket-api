"""
Nike Rocket - Portfolio Tracking API (FIXED)
============================================
Auto-creates portfolio users from follower system API keys.

Author: Nike Rocket Team
Updated: November 20, 2025
"""
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from sqlalchemy import create_engine
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
import numpy as np
import os

from portfolio_models import User, Trade, DepositEvent, WithdrawalEvent, Base
# Import from follower system to check API keys
from follower_models import User as FollowerUser

# Router
router = APIRouter(prefix="/api/portfolio")

# Database setup
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL) if DATABASE_URL else None

# Dependency to get DB session
def get_db():
    from sqlalchemy.orm import sessionmaker
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Authentication with auto-create
async def get_current_user(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: Session = Depends(get_db)
) -> User:
    """
    Authenticate user by API key and auto-create portfolio user if needed
    
    This checks:
    1. If portfolio user exists → use it
    2. If not, check if API key exists in follower system → auto-create portfolio user
    3. If neither → reject with 401
    """
    # First check if portfolio user exists
    user = db.query(User).filter(User.api_key == x_api_key).first()
    if user:
        return user
    
    # Portfolio user doesn't exist, check follower system
    follower_user = db.query(FollowerUser).filter(FollowerUser.api_key == x_api_key).first()
    if not follower_user:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    # Valid follower API key! Auto-create portfolio user
    new_portfolio_user = User(
        api_key=x_api_key,
        initial_capital=0.0,  # Will be set when they initialize
        current_balance=0.0,
        total_deposits=0.0,
        total_withdrawals=0.0,
        started_tracking_at=None  # Will be set when they initialize
    )
    
    db.add(new_portfolio_user)
    db.commit()
    db.refresh(new_portfolio_user)
    
    return new_portfolio_user

# Pydantic models
class InitializePortfolioRequest(BaseModel):
    initial_capital: float

class TradeRequest(BaseModel):
    symbol: str
    side: str  # 'BUY' or 'SELL'
    entry_price: float
    exit_price: float
    quantity: float
    leverage: float = 1.0
    entry_time: datetime
    exit_time: datetime
    pnl_usd: float
    account_balance_at_entry: Optional[float] = None

class DepositRequest(BaseModel):
    amount: float
    note: Optional[str] = None

class WithdrawRequest(BaseModel):
    amount: float
    note: Optional[str] = None


# ==================== ENDPOINTS ====================

@router.post("/initialize")
async def initialize_portfolio(
    data: InitializePortfolioRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Initialize portfolio tracking with starting capital
    """
    if user.started_tracking_at:
        return {
            "status": "already_initialized",
            "message": "Portfolio already initialized",
            "initial_capital": user.initial_capital,
            "started_at": user.started_tracking_at.isoformat()
        }
    
    user.initial_capital = data.initial_capital
    user.current_balance = data.initial_capital
    user.started_tracking_at = datetime.utcnow()
    
    db.commit()
    
    return {
        "status": "success",
        "message": "Portfolio initialized",
        "initial_capital": user.initial_capital,
        "current_balance": user.current_balance
    }


@router.post("/trades")
async def add_trade(
    trade_data: TradeRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Add a completed trade to portfolio
    """
    # Create trade record
    trade = Trade(
        user_id=user.id,
        symbol=trade_data.symbol,
        side=trade_data.side.upper(),
        entry_price=trade_data.entry_price,
        exit_price=trade_data.exit_price,
        quantity=trade_data.quantity,
        leverage=trade_data.leverage,
        entry_time=trade_data.entry_time,
        exit_time=trade_data.exit_time,
        pnl_usd=trade_data.pnl_usd,
        account_balance_at_entry=trade_data.account_balance_at_entry or user.current_balance,
        status='WIN' if trade_data.pnl_usd > 0 else 'LOSS'
    )
    
    # Calculate P&L percentage
    if trade_data.account_balance_at_entry and trade_data.account_balance_at_entry > 0:
        trade.pnl_percent = (trade_data.pnl_usd / trade_data.account_balance_at_entry) * 100
    
    db.add(trade)
    
    # Update user's current balance
    user.current_balance += trade_data.pnl_usd
    
    db.commit()
    db.refresh(trade)
    
    return {
        "status": "success",
        "trade_id": trade.id,
        "pnl_usd": trade.pnl_usd,
        "new_balance": user.current_balance
    }


@router.post("/deposit")
async def record_deposit(
    data: DepositRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Record a capital deposit
    """
    event = DepositEvent(
        user_id=user.id,
        amount=data.amount,
        note=data.note
    )
    
    db.add(event)
    
    # Update balances
    user.current_balance += data.amount
    user.total_deposits += data.amount
    
    db.commit()
    db.refresh(event)
    
    return {
        "status": "success",
        "event_id": event.id,
        "amount": event.amount,
        "new_balance": user.current_balance,
        "total_deposits": user.total_deposits
    }


@router.post("/withdraw")
async def record_withdrawal(
    data: WithdrawRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Record a capital withdrawal
    """
    event = WithdrawalEvent(
        user_id=user.id,
        amount=data.amount,
        note=data.note
    )
    
    db.add(event)
    
    # Update balances
    user.current_balance -= data.amount
    user.total_withdrawals += data.amount
    
    db.commit()
    db.refresh(event)
    
    return {
        "status": "success",
        "event_id": event.id,
        "amount": event.amount,
        "new_balance": user.current_balance,
        "total_withdrawals": user.total_withdrawals
    }


@router.get("/stats")
async def get_performance_stats(
    period: str = "30d",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get performance statistics (SAFE from deposits/withdrawals)
    
    All metrics calculated from trade P&L only, not account balance.
    """
    # Check if portfolio is initialized
    if not user.started_tracking_at:
        return {
            "status": "not_initialized",
            "message": "Portfolio not initialized yet. Please initialize with starting capital."
        }
    
    # Parse period parameter
    if period.lower() == "all":
        trades = db.query(Trade).filter(
            Trade.user_id == user.id,
            Trade.status.in_(['WIN', 'LOSS'])
        ).all()
        period_label = "All-Time"
        days_value = None
    else:
        days_value = int(period.replace("d", "").replace("D", ""))
        since_date = datetime.utcnow() - timedelta(days=days_value)
        trades = db.query(Trade).filter(
            Trade.user_id == user.id,
            Trade.exit_time >= since_date,
            Trade.status.in_(['WIN', 'LOSS'])
        ).all()
        period_label = f"{days_value}-Day"
    
    if not trades:
        return {
            "status": "no_data",
            "message": "No closed trades in this period",
            "period": period_label,
            "initialized": True
        }
    
    # Separate wins and losses
    wins = [t for t in trades if t.status == 'WIN']
    losses = [t for t in trades if t.status == 'LOSS']
    
    # Total Profit
    period_profit = sum(t.pnl_usd for t in trades)
    
    # Profit Factor
    total_wins_amount = sum(t.pnl_usd for t in wins)
    total_losses_amount = abs(sum(t.pnl_usd for t in losses))
    profit_factor = (total_wins_amount / total_losses_amount) if total_losses_amount > 0 else 0
    
    # Best trade
    best_trade = max((t.pnl_usd for t in wins), default=0)
    
    # Average monthly profit
    if period.lower() == "all":
        months_active = (datetime.utcnow() - user.started_tracking_at).days / 30.44 if user.started_tracking_at else 1
    else:
        months_active = days_value / 30.44 if days_value else 1
    avg_monthly_profit = period_profit / months_active if months_active > 0 else 0
    
    # Win rate
    win_rate = (len(wins) / len(trades) * 100) if trades else 0
    
    # ROI on INITIAL capital
    roi_on_initial = (period_profit / user.initial_capital * 100) if user.initial_capital > 0 else 0
    
    # ROI adjusted for deposits
    total_capital_deployed = user.initial_capital + user.total_deposits
    roi_adjusted = (period_profit / total_capital_deployed * 100) if total_capital_deployed > 0 else 0
    
    # Sharpe Ratio
    if len(trades) > 1:
        returns = [t.pnl_percent for t in trades if t.pnl_percent is not None]
        if returns:
            sharpe_ratio = (np.mean(returns) / np.std(returns)) * np.sqrt(252) if np.std(returns) > 0 else 0
        else:
            sharpe_ratio = 0
    else:
        sharpe_ratio = 0
    
    # Max Drawdown
    equity_curve = []
    equity = 0
    peak = 0
    max_dd = 0
    
    for trade in sorted(trades, key=lambda t: t.exit_time):
        equity += trade.pnl_usd
        equity_curve.append(equity)
        if equity > peak:
            peak = equity
        drawdown = ((peak - equity) / peak * 100) if peak > 0 else 0
        if drawdown > max_dd:
            max_dd = drawdown
    
    # Recovery from drawdown
    if peak > 0:
        recovery_from_dd = ((equity - (peak - (peak * max_dd / 100))) / (peak * max_dd / 100) * 100) if max_dd > 0 else 0
    else:
        recovery_from_dd = 0
    
    # Average R:R ratio
    avg_win = (total_wins_amount / len(wins)) if wins else 0
    avg_loss = (total_losses_amount / len(losses)) if losses else 0
    avg_rr = (avg_win / avg_loss) if avg_loss > 0 else 0
    
    return {
        "period": period_label,
        "period_param": period,
        
        # Primary metrics
        "total_profit": round(period_profit, 2),
        "roi_on_initial": round(roi_on_initial, 1),
        "profit_factor": round(profit_factor, 2),
        "best_trade": round(best_trade, 2),
        "avg_monthly_profit": round(avg_monthly_profit, 2),
        
        # Secondary metrics
        "total_trades": len(trades),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(win_rate, 1),
        "sharpe_ratio": round(sharpe_ratio, 2),
        "max_drawdown": round(max_dd, 1),
        "recovery_from_dd": round(recovery_from_dd, 1),
        "avg_rr_ratio": round(avg_rr, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "risk_per_trade": 2.0,
        
        # Capital tracking
        "roi_adjusted_for_deposits": round(roi_adjusted, 1),
        "initial_capital": user.initial_capital,
        "current_balance": round(user.current_balance, 2),
        "total_deposits": user.total_deposits,
        "total_withdrawals": user.total_withdrawals,
        "total_capital_deployed": total_capital_deployed,
        
        # Breakdown
        "gross_wins": round(total_wins_amount, 2),
        "gross_losses": round(total_losses_amount, 2),
        
        # Time tracking
        "started_tracking": user.started_tracking_at.isoformat() if user.started_tracking_at else None,
        "days_active": round(months_active * 30.44, 0) if period.lower() == "all" else days_value
    }
