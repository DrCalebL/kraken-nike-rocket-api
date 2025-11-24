# UPDATED PORTFOLIO API - AUTO-DETECT INITIAL CAPITAL
# ====================================================
# FULLY CORRECTED VERSION - Proper column names for all tables
# FIXED: Uses pnl_usd column (not pnl)
# NO CIRCULAR IMPORTS

from fastapi import APIRouter, Request, HTTPException
from datetime import datetime, timedelta
from decimal import Decimal
import asyncpg
import os
from cryptography.fernet import Fernet

router = APIRouter()

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
        print(f"Error decrypting credentials: {e}")
        return None, None


async def get_kraken_credentials(api_key: str):
    """Get user's Kraken API credentials from database"""
    DATABASE_URL = os.getenv("DATABASE_URL")
    if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    
    conn = await asyncpg.connect(DATABASE_URL)
    
    user = await conn.fetchrow("""
        SELECT 
            kraken_api_key_encrypted, 
            kraken_api_secret_encrypted,
            credentials_set
        FROM follower_users
        WHERE api_key = $1
        AND credentials_set = true
    """, api_key)
    
    await conn.close()
    
    if not user:
        return None
    
    kraken_key, kraken_secret = decrypt_credentials(
        user['kraken_api_key_encrypted'],
        user['kraken_api_secret_encrypted']
    )
    
    if not kraken_key or not kraken_secret:
        return None
    
    return {
        'kraken_key': kraken_key,
        'kraken_secret': kraken_secret
    }


async def get_current_kraken_balance(kraken_key: str, kraken_secret: str):
    """Get current USDT balance from Kraken"""
    try:
        import krakenex
        from pykrakenapi import KrakenAPI
        
        kraken = krakenex.API(key=kraken_key, secret=kraken_secret)
        k = KrakenAPI(kraken)
        balance = k.get_account_balance()
        
        usdt_balance = 0
        for currency in ['USDT', 'ZUSD', 'USD']:
            if currency in balance.index:
                usdt_balance = float(balance.loc[currency]['vol'])
                break
        
        return Decimal(str(usdt_balance))
        
    except Exception as e:
        print(f"Error getting Kraken balance: {e}")
        return None


@router.post("/api/portfolio/initialize")
async def initialize_portfolio_autodetect(request: Request):
    """Initialize portfolio tracking - AUTO-DETECTS initial capital from Kraken"""
    api_key = request.headers.get("X-API-Key")
    
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")
    
    try:
        DATABASE_URL = os.getenv("DATABASE_URL")
        if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
            DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        
        conn = await asyncpg.connect(DATABASE_URL)
        
        credentials = await get_kraken_credentials(api_key)
        
        if not credentials:
            await conn.close()
            return {
                "status": "error",
                "message": "Please set up your trading agent first."
            }
        
        existing = await conn.fetchrow(
            "SELECT * FROM portfolio_users WHERE api_key = $1",
            api_key
        )
        
        if existing:
            await conn.close()
            return {
                "status": "already_initialized",
                "message": "Portfolio already initialized",
                "initial_capital": float(existing['initial_capital'])
            }
        
        kraken_balance = await get_current_kraken_balance(
            credentials['kraken_key'],
            credentials['kraken_secret']
        )
        
        if kraken_balance is None:
            await conn.close()
            return {
                "status": "error",
                "message": "Could not connect to Kraken. Please check your agent setup."
            }
        
        if kraken_balance <= 0:
            await conn.close()
            return {
                "status": "error",
                "message": f"Your Kraken balance is $0. Please deposit funds first."
            }
        
        MINIMUM_BALANCE = 10
        if kraken_balance < MINIMUM_BALANCE:
            await conn.close()
            return {
                "status": "error",
                "message": f"Minimum balance: ${MINIMUM_BALANCE}. Your balance: ${float(kraken_balance):.2f}"
            }
        
        initial_capital = float(kraken_balance)
        
        await conn.execute("""
            INSERT INTO portfolio_users (api_key, initial_capital, created_at, last_known_balance)
            VALUES ($1, $2, CURRENT_TIMESTAMP, $2)
        """, api_key, initial_capital)
        
        await conn.execute("""
            INSERT INTO portfolio_transactions (
                user_id, transaction_type, amount, detection_method, notes
            ) VALUES ($1, 'initial', $2, 'automatic', $3)
        """, api_key, initial_capital, 
            f'Auto-detected from Kraken balance: ${initial_capital:,.2f}')
        
        await conn.close()
        
        return {
            "status": "success",
            "message": f"Portfolio initialized with ${initial_capital:,.2f}",
            "initial_capital": initial_capital,
            "detected_from": "kraken_balance"
        }
        
    except Exception as e:
        print(f"Error initializing portfolio: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/portfolio/balance-summary")
async def get_balance_summary(request: Request):
    """Get comprehensive balance summary"""
    api_key = request.headers.get("X-API-Key") or request.query_params.get("key")
    
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")
    
    try:
        from balance_checker import BalanceChecker
        
        DATABASE_URL = os.getenv("DATABASE_URL")
        if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
            DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        
        db_pool = await asyncpg.create_pool(DATABASE_URL)
        checker = BalanceChecker(db_pool)
        summary = await checker.get_balance_summary(api_key)
        await db_pool.close()
        
        if not summary:
            return {
                "status": "not_initialized",
                "message": "Portfolio not initialized"
            }
        
        return {
            "status": "success",
            **summary
        }
        
    except Exception as e:
        print(f"Error in balance summary: {e}")
        return {
            "status": "success",
            "initial_capital": 0,
            "total_deposits": 0,
            "total_withdrawals": 0,
            "net_deposits": 0,
            "total_capital": 0,
            "total_profit": 0,
            "current_value": 0,
            "roi_on_initial": 0,
            "roi_on_total": 0,
            "last_balance_check": None
        }


@router.get("/api/portfolio/transactions")
async def get_transactions(request: Request):
    """Get transaction history"""
    api_key = request.headers.get("X-API-Key") or request.query_params.get("key")
    limit = int(request.query_params.get("limit", 50))
    
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")
    
    try:
        from balance_checker import BalanceChecker
        
        DATABASE_URL = os.getenv("DATABASE_URL")
        if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
            DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        
        db_pool = await asyncpg.create_pool(DATABASE_URL)
        checker = BalanceChecker(db_pool)
        transactions = await checker.get_transaction_history(api_key, limit)
        await db_pool.close()
        
        return {
            "status": "success",
            "transactions": transactions
        }
        
    except Exception as e:
        print(f"Error loading transactions: {e}")
        return {
            "status": "success",
            "transactions": []
        }


@router.get("/api/portfolio/stats")
async def get_portfolio_stats(request: Request, period: str = "30d"):
    """
    Get portfolio statistics for a specific time period
    
    ═══════════════════════════════════════════════════════════════
    FORMULA DOCUMENTATION:
    ═══════════════════════════════════════════════════════════════
    
    1. TOTAL PROFIT (period):
       Formula: SUM(pnl_usd) for trades in the selected period
       
    2. ROI ON INITIAL CAPITAL:
       Formula: (total_profit / initial_capital) × 100
       
    3. ROI ON TOTAL CAPITAL:
       Formula: (total_profit / total_capital) × 100
       Where: total_capital = initial_capital + net_deposits
       
    4. PROFIT FACTOR:
       Formula: SUM(winning_pnl) / ABS(SUM(losing_pnl))
       
    5. WIN RATE:
       Formula: (winning_trades / total_trades) × 100
       
    6. BEST TRADE: MAX(pnl_usd)
    7. WORST TRADE: MIN(pnl_usd)
    8. AVG MONTHLY PROFIT: total_profit / months_active
    9. MAX DRAWDOWN: MAX((peak - trough) / peak) × 100
    10. SHARPE RATIO: (avg_return / volatility) × sqrt(252)
    11. DAYS ACTIVE: (current_date - first_trade_date).days
    ═══════════════════════════════════════════════════════════════
    """
    api_key = request.headers.get("X-API-Key") or request.query_params.get("key")
    
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")
    
    try:
        from balance_checker import BalanceChecker
        
        DATABASE_URL = os.getenv("DATABASE_URL")
        if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
            DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        
        db_pool = await asyncpg.create_pool(DATABASE_URL)
        checker = BalanceChecker(db_pool)
        summary = await checker.get_balance_summary(api_key)
        await db_pool.close()
        
        if not summary:
            return {
                "status": "no_data",
                "message": "Portfolio not initialized"
            }
        
        conn = await asyncpg.connect(DATABASE_URL)
        
        # Calculate date range based on period
        now = datetime.utcnow()
        if period == "7d":
            start_date = now - timedelta(days=7)
            period_label = "Last 7 Days"
        elif period == "30d":
            start_date = now - timedelta(days=30)
            period_label = "Last 30 Days"
        elif period == "90d":
            start_date = now - timedelta(days=90)
            period_label = "Last 90 Days"
        else:
            start_date = datetime(2020, 1, 1)
            period_label = "All Time"
        
        # ═══════════════════════════════════════════════════════════════
        # FIXED: Use pnl_usd column instead of pnl
        # ═══════════════════════════════════════════════════════════════
        trades_query = await conn.fetch("""
            SELECT 
                pt.pnl_usd,
                pt.pnl_percent,
                pt.status,
                pt.exit_time,
                pt.entry_time
            FROM portfolio_trades pt
            JOIN portfolio_users pu ON pt.user_id = pu.id
            WHERE pu.api_key = $1
            AND pt.exit_time >= $2
            ORDER BY pt.exit_time DESC
        """, api_key, start_date)
        
        first_trade = await conn.fetchval("""
            SELECT MIN(pt.entry_time)
            FROM portfolio_trades pt
            JOIN portfolio_users pu ON pt.user_id = pu.id
            WHERE pu.api_key = $1
        """, api_key)
        
        await conn.close()
        
        total_trades = len(trades_query)
        
        if total_trades == 0:
            return {
                "status": "no_trades",
                "period": period,
                "period_label": period_label,
                "total_profit": 0,
                "all_time_profit": summary.get('total_profit', 0),
                "roi_on_initial": summary.get('roi_on_initial', 0),
                "roi_on_total": summary.get('roi_on_total', 0),
                "initial_capital": summary.get('initial_capital', 0),
                "current_value": summary.get('current_value', 0),
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "win_rate": 0,
                "profit_factor": 0,
                "best_trade": 0,
                "worst_trade": 0,
                "avg_trade": 0,
                "avg_monthly_profit": 0,
                "max_drawdown": 0,
                "sharpe_ratio": 0,
                "days_active": 0,
                "total_deposits": summary.get('total_deposits', 0),
                "total_withdrawals": summary.get('total_withdrawals', 0)
            }
        
        # Extract PnL values
        pnl_values = [float(t['pnl_usd'] or 0) for t in trades_query]
        
        # 1. TOTAL PROFIT (period)
        period_profit = sum(pnl_values)
        
        # 2. WINNING/LOSING TRADES
        winning_pnl = [p for p in pnl_values if p > 0]
        losing_pnl = [p for p in pnl_values if p < 0]
        winning_trades = len(winning_pnl)
        losing_trades = len(losing_pnl)
        
        # 3. WIN RATE = (wins / total) × 100
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
        
        # 4. PROFIT FACTOR = SUM(wins) / ABS(SUM(losses))
        total_wins = sum(winning_pnl) if winning_pnl else 0
        total_losses = abs(sum(losing_pnl)) if losing_pnl else 0
        profit_factor = (total_wins / total_losses) if total_losses > 0 else (999 if total_wins > 0 else 0)
        
        # 5. BEST TRADE = MAX(pnl_usd)
        best_trade = max(pnl_values) if pnl_values else 0
        
        # 6. WORST TRADE = MIN(pnl_usd)
        worst_trade = min(pnl_values) if pnl_values else 0
        
        # 7. AVERAGE TRADE = SUM(pnl) / COUNT
        avg_trade = period_profit / total_trades if total_trades > 0 else 0
        
        # 8. DAYS ACTIVE = (now - first_trade).days
        days_active = (now - first_trade).days if first_trade else 0
        
        # 9. AVG MONTHLY PROFIT = total_profit / months
        months_active = max(1, days_active / 30)
        avg_monthly_profit = summary.get('total_profit', 0) / months_active
        
        # 10. MAX DRAWDOWN (deposit/withdrawal adjusted)
        # ═══════════════════════════════════════════════════════════════
        # CRITICAL: Max Drawdown must be calculated on TRADING PnL only
        # 
        # Problem: Deposits and withdrawals corrupt the equity curve
        # - Withdrawal of $500 looks like a 50% drawdown (wrong!)
        # - Deposit of $1000 masks a real drawdown (wrong!)
        #
        # Solution: Build equity curve from CUMULATIVE TRADING PnL only
        # - Start at $0 (or initial capital)
        # - Add each trade's pnl_usd chronologically
        # - This gives us pure trading performance, unaffected by
        #   deposits or withdrawals
        #
        # Formula: 
        # equity[i] = initial_capital + SUM(pnl_usd[0:i])
        # drawdown[i] = (running_peak - equity[i]) / running_peak
        # max_drawdown = MAX(all drawdowns)
        # ═══════════════════════════════════════════════════════════════
        
        initial_capital = summary.get('initial_capital', 0)
        if initial_capital <= 0:
            initial_capital = summary.get('current_value', 1000)
        
        # Sort trades by exit_time (oldest first) to build proper equity curve
        trades_sorted = sorted(trades_query, key=lambda t: t['exit_time'])
        
        # Build TRADING-ONLY equity curve (ignores deposits/withdrawals)
        # This measures pure trading performance
        equity_curve = [initial_capital]  # Start with initial capital
        cumulative_pnl = 0
        
        for trade in trades_sorted:
            cumulative_pnl += float(trade['pnl_usd'] or 0)
            equity_curve.append(initial_capital + cumulative_pnl)
        
        # Calculate max drawdown using running peak on trading-only curve
        max_drawdown = 0
        running_peak = equity_curve[0]
        
        for value in equity_curve:
            # Update running peak if we hit a new high
            if value > running_peak:
                running_peak = value
            
            # Calculate drawdown from peak (as percentage)
            if running_peak > 0:
                drawdown = (running_peak - value) / running_peak * 100
                max_drawdown = max(max_drawdown, drawdown)
        
        # 11. SHARPE RATIO
        if len(pnl_values) > 1:
            import statistics
            avg_return = statistics.mean(pnl_values)
            std_dev = statistics.stdev(pnl_values)
            sharpe_ratio = (avg_return / std_dev) if std_dev > 0 else 0
            sharpe_ratio = sharpe_ratio * (252 ** 0.5)
        else:
            sharpe_ratio = 0
        
        return {
            "status": "success",
            "period": period,
            "period_label": period_label,
            "total_profit": round(period_profit, 2),
            "all_time_profit": round(summary.get('total_profit', 0), 2),
            "roi_on_initial": round(summary.get('roi_on_initial', 0), 2),
            "roi_on_total": round(summary.get('roi_on_total', 0), 2),
            "initial_capital": round(summary.get('initial_capital', 0), 2),
            "current_value": round(summary.get('current_value', 0), 2),
            "total_deposits": round(summary.get('total_deposits', 0), 2),
            "total_withdrawals": round(summary.get('total_withdrawals', 0), 2),
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "win_rate": round(win_rate, 1),
            "profit_factor": round(profit_factor, 2),
            "best_trade": round(best_trade, 2),
            "worst_trade": round(worst_trade, 2),
            "avg_trade": round(avg_trade, 2),
            "avg_monthly_profit": round(avg_monthly_profit, 2),
            "max_drawdown": round(max_drawdown, 1),
            "sharpe_ratio": round(sharpe_ratio, 2),
            "days_active": days_active
        }
        
    except Exception as e:
        print(f"Error in portfolio stats: {e}")
        import traceback
        traceback.print_exc()
        return {
            "status": "no_data",
            "message": str(e)
        }


@router.get("/api/portfolio/equity-curve")
async def get_equity_curve(request: Request):
    """
    Get trading-only equity curve for charting
    
    ═══════════════════════════════════════════════════════════════
    EQUITY CURVE EXPLANATION:
    ═══════════════════════════════════════════════════════════════
    
    This returns a TRADING-ONLY equity curve that:
    - Starts at initial capital
    - Adds each trade's PnL chronologically
    - IGNORES deposits and withdrawals
    
    This gives a pure measure of trading performance that can be
    charted over time to visualize:
    - Overall growth/decline trend
    - Drawdown periods (visible as dips)
    - Recovery periods
    - Volatility of returns
    
    Response format:
    {
        "status": "success",
        "equity_curve": [
            {"date": "2024-01-15", "equity": 1000, "pnl": 0},
            {"date": "2024-01-16", "equity": 1050, "pnl": 50},
            {"date": "2024-01-17", "equity": 980, "pnl": -70},
            ...
        ],
        "initial_capital": 1000,
        "current_equity": 1200,
        "max_equity": 1300,
        "min_equity": 900,
        "max_drawdown": 15.4
    }
    ═══════════════════════════════════════════════════════════════
    """
    api_key = request.headers.get("X-API-Key") or request.query_params.get("key")
    
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")
    
    try:
        from balance_checker import BalanceChecker
        
        DATABASE_URL = os.getenv("DATABASE_URL")
        if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
            DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        
        # Get initial capital from balance summary
        db_pool = await asyncpg.create_pool(DATABASE_URL)
        checker = BalanceChecker(db_pool)
        summary = await checker.get_balance_summary(api_key)
        await db_pool.close()
        
        initial_capital = summary.get('initial_capital', 0) if summary else 0
        if initial_capital <= 0:
            initial_capital = summary.get('current_value', 1000) if summary else 1000
        
        # Get all trades sorted by time
        conn = await asyncpg.connect(DATABASE_URL)
        
        trades = await conn.fetch("""
            SELECT 
                pt.pnl_usd,
                pt.exit_time,
                pt.symbol,
                pt.side
            FROM portfolio_trades pt
            JOIN portfolio_users pu ON pt.user_id = pu.id
            WHERE pu.api_key = $1
            AND pt.exit_time IS NOT NULL
            ORDER BY pt.exit_time ASC
        """, api_key)
        
        # Get portfolio start date
        start_date = await conn.fetchval("""
            SELECT created_at FROM portfolio_users WHERE api_key = $1
        """, api_key)
        
        await conn.close()
        
        if not trades:
            return {
                "status": "no_trades",
                "equity_curve": [
                    {
                        "date": start_date.isoformat() if start_date else datetime.utcnow().isoformat(),
                        "equity": initial_capital,
                        "pnl": 0,
                        "cumulative_pnl": 0
                    }
                ],
                "initial_capital": initial_capital,
                "current_equity": initial_capital,
                "max_equity": initial_capital,
                "min_equity": initial_capital,
                "max_drawdown": 0
            }
        
        # Build equity curve from trading PnL only
        equity_curve = []
        cumulative_pnl = 0
        running_peak = initial_capital
        max_drawdown = 0
        max_equity = initial_capital
        min_equity = initial_capital
        
        # Add starting point
        equity_curve.append({
            "date": start_date.isoformat() if start_date else trades[0]['exit_time'].isoformat(),
            "equity": round(initial_capital, 2),
            "pnl": 0,
            "cumulative_pnl": 0,
            "trade": "Starting Balance"
        })
        
        for trade in trades:
            pnl = float(trade['pnl_usd'] or 0)
            cumulative_pnl += pnl
            current_equity = initial_capital + cumulative_pnl
            
            # Track max/min equity
            max_equity = max(max_equity, current_equity)
            min_equity = min(min_equity, current_equity)
            
            # Track max drawdown
            if current_equity > running_peak:
                running_peak = current_equity
            if running_peak > 0:
                drawdown = (running_peak - current_equity) / running_peak * 100
                max_drawdown = max(max_drawdown, drawdown)
            
            equity_curve.append({
                "date": trade['exit_time'].isoformat(),
                "equity": round(current_equity, 2),
                "pnl": round(pnl, 2),
                "cumulative_pnl": round(cumulative_pnl, 2),
                "trade": f"{trade['side']} {trade['symbol']}"
            })
        
        current_equity = initial_capital + cumulative_pnl
        
        return {
            "status": "success",
            "equity_curve": equity_curve,
            "initial_capital": round(initial_capital, 2),
            "current_equity": round(current_equity, 2),
            "max_equity": round(max_equity, 2),
            "min_equity": round(min_equity, 2),
            "max_drawdown": round(max_drawdown, 2),
            "total_trades": len(trades),
            "total_pnl": round(cumulative_pnl, 2)
        }
