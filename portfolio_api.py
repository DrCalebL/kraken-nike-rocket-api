# UPDATED PORTFOLIO API - AUTO-DETECT INITIAL CAPITAL
# ====================================================
# FULLY CORRECTED VERSION - Proper column names for all tables
# FIXED: Uses pnl_usd column (not pnl)
# CONSOLIDATED: Updates follower_users as primary source of truth
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
    """
    Initialize portfolio tracking - AUTO-DETECTS initial capital from Kraken
    
    SIMPLIFIED: Only uses follower_users table
    """
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
        
        # Check if already initialized in follower_users
        fu_existing = await conn.fetchrow(
            "SELECT portfolio_initialized, initial_capital FROM follower_users WHERE api_key = $1",
            api_key
        )
        
        if fu_existing and fu_existing['portfolio_initialized']:
            initial_cap = fu_existing['initial_capital']
            await conn.close()
            return {
                "status": "already_initialized",
                "message": "Portfolio already initialized",
                "initial_capital": float(initial_cap or 0)
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
        
        # Update follower_users (primary source of truth)
        await conn.execute("""
            UPDATE follower_users SET
                initial_capital = $1,
                last_known_balance = $1,
                portfolio_initialized = true,
                started_tracking_at = CURRENT_TIMESTAMP
            WHERE api_key = $2
        """, initial_capital, api_key)
        
        # Get user_id for proper FK
        user_id = await conn.fetchval(
            "SELECT id FROM follower_users WHERE api_key = $1",
            api_key
        )
        
        # Record initial transaction with proper FKs
        await conn.execute("""
            INSERT INTO portfolio_transactions (
                follower_user_id, user_id, transaction_type, amount, detection_method, notes
            ) VALUES ($1, $2, 'initial', $3, 'automatic', $4)
        """, user_id, api_key, initial_capital, 
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
    """Get comprehensive balance summary including trading profit from trades table"""
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
        
        # Also get total profit from actual trades
        conn = await db_pool.acquire()
        trade_stats = await conn.fetchrow("""
            SELECT 
                COALESCE(SUM(t.profit_usd), 0) as total_profit,
                COUNT(*) as total_trades
            FROM trades t
            JOIN follower_users fu ON t.user_id = fu.id
            WHERE fu.api_key = $1
        """, api_key)
        await db_pool.release(conn)
        await db_pool.close()
        
        if not summary:
            return {
                "status": "not_initialized",
                "message": "Portfolio not initialized"
            }
        
        # Override total_profit with actual trading profit
        if trade_stats:
            summary['total_profit'] = float(trade_stats['total_profit'] or 0)
            summary['total_trades'] = int(trade_stats['total_trades'] or 0)
            
            # Recalculate ROI with actual profit
            initial_capital = summary.get('initial_capital', 0)
            total_capital = summary.get('total_capital', 0)
            
            if initial_capital > 0:
                summary['roi_on_initial'] = (summary['total_profit'] / initial_capital) * 100
            if total_capital > 0:
                summary['roi_on_total'] = (summary['total_profit'] / total_capital) * 100
        
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
        DATABASE_URL = os.getenv("DATABASE_URL")
        if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
            DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        
        conn = await asyncpg.connect(DATABASE_URL)
        
        # Get user info directly from follower_users
        user_row = await conn.fetchrow("""
            SELECT id, initial_capital, last_known_balance, portfolio_initialized
            FROM follower_users
            WHERE api_key = $1
        """, api_key)
        
        if not user_row:
            await conn.close()
            return {
                "status": "no_data",
                "message": "User not found"
            }
        
        user_id = user_row['id']
        initial_capital = float(user_row['initial_capital'] or 0)
        current_value = float(user_row['last_known_balance'] or 0)
        
        # Get total profit from trades
        profit_result = await conn.fetchval("""
            SELECT COALESCE(SUM(profit_usd), 0)
            FROM trades
            WHERE user_id = $1 AND closed_at IS NOT NULL
        """, user_id)
        total_profit = float(profit_result or 0)
        
        # Build summary dict
        summary = {
            'initial_capital': initial_capital,
            'total_deposits': 0,
            'total_withdrawals': 0,
            'total_profit': total_profit,
            'current_value': current_value if current_value > 0 else initial_capital + total_profit,
            'roi_on_initial': (total_profit / initial_capital * 100) if initial_capital > 0 else 0,
            'roi_on_total': (total_profit / initial_capital * 100) if initial_capital > 0 else 0
        }
        
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
        elif period == "1y":
            start_date = now - timedelta(days=365)
            period_label = "Last 1 Year"
        else:  # "all" or any other value
            start_date = datetime(2020, 1, 1)
            period_label = "All Time"
        
        # ═══════════════════════════════════════════════════════════════
        # FIXED: Read from trades table (copytrade results)
        # ═══════════════════════════════════════════════════════════════
        trades_query = await conn.fetch("""
            SELECT 
                t.profit_usd as pnl_usd,
                t.profit_percent as pnl_percent,
                'closed' as status,
                t.closed_at as exit_time,
                t.opened_at as entry_time
            FROM trades t
            JOIN follower_users fu ON t.user_id = fu.id
            WHERE fu.api_key = $1
            AND t.closed_at >= $2
            ORDER BY t.closed_at DESC
        """, api_key, start_date)
        
        first_trade = await conn.fetchval("""
            SELECT MIN(t.opened_at)
            FROM trades t
            JOIN follower_users fu ON t.user_id = fu.id
            WHERE fu.api_key = $1
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
        
        # Get all trades sorted by time (from trades table, not portfolio_trades)
        conn = await asyncpg.connect(DATABASE_URL)
        
        trades = await conn.fetch("""
            SELECT 
                t.profit_usd as pnl_usd,
                t.closed_at as exit_time,
                t.symbol,
                t.side
            FROM trades t
            JOIN follower_users fu ON t.user_id = fu.id
            WHERE fu.api_key = $1
            AND t.closed_at IS NOT NULL
            ORDER BY t.closed_at ASC
        """, api_key)
        
        # Get portfolio start date
        start_date = await conn.fetchval("""
            SELECT created_at FROM follower_users WHERE api_key = $1
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
                "max_drawdown": 0,
                "total_trades": 0,
                "total_pnl": 0
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
        
    except Exception as e:
        print(f"Error in equity curve: {e}")
        import traceback
        traceback.print_exc()
        return {
            "status": "error",
            "message": str(e),
            "equity_curve": [],
            "initial_capital": 0,
            "current_equity": 0,
            "max_equity": 0,
            "min_equity": 0,
            "max_drawdown": 0,
            "total_trades": 0,
            "total_pnl": 0
        }


# ==================== TRADE EXPORT ENDPOINTS ====================

from fastapi.responses import StreamingResponse
import io
import csv

@router.get("/portfolio/trades/monthly-csv")
async def export_monthly_trades(request: Request, key: str, year: int, month: int):
    """
    Export monthly trades as CSV for customer dashboard
    
    Includes:
    - Individual trade details
    - Net P&L summary at bottom
    """
    DATABASE_URL = os.getenv("DATABASE_URL")
    if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        
        # Verify user exists
        user = await conn.fetchrow(
            "SELECT id, email, fee_tier FROM follower_users WHERE api_key = $1",
            key
        )
        
        if not user:
            await conn.close()
            raise HTTPException(status_code=404, detail="User not found")
        
        # Get trades for the specified month
        start_date = datetime(year, month, 1)
        if month == 12:
            end_date = datetime(year + 1, 1, 1)
        else:
            end_date = datetime(year, month + 1, 1)
        
        trades = await conn.fetch("""
            SELECT 
                closed_at,
                symbol,
                side,
                entry_price,
                exit_price,
                position_size,
                leverage,
                profit_usd,
                profit_percent,
                notes
            FROM trades
            WHERE user_id = $1
            AND closed_at >= $2
            AND closed_at < $3
            ORDER BY closed_at ASC
        """, user['id'], start_date, end_date)
        
        await conn.close()
        
        # Create CSV
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Header
        month_name = start_date.strftime('%B %Y')
        writer.writerow([f"Trade History - {month_name}"])
        writer.writerow([f"User: {user['email']}"])
        writer.writerow([f"Fee Tier: {user['fee_tier'] or 'standard'}"])
        writer.writerow([])
        
        # Column headers
        writer.writerow([
            'Date (UTC)', 'Symbol', 'Side', 'Entry Price', 'Exit Price',
            'Position Size', 'Leverage', 'P&L ($)', 'P&L (%)', 'Notes'
        ])
        
        # Trade rows
        total_pnl = 0
        winning_trades = 0
        losing_trades = 0
        
        for trade in trades:
            pnl = float(trade['profit_usd'] or 0)
            total_pnl += pnl
            
            if pnl > 0:
                winning_trades += 1
            elif pnl < 0:
                losing_trades += 1
            
            writer.writerow([
                trade['closed_at'].strftime('%Y-%m-%d %H:%M:%S'),
                trade['symbol'],
                trade['side'],
                f"${trade['entry_price']:.4f}",
                f"${trade['exit_price']:.4f}",
                trade['position_size'],
                f"{trade['leverage']}x",
                f"${pnl:+.2f}",
                f"{trade['profit_percent']:+.2f}%",
                trade['notes'] or ''
            ])
        
        # Summary section
        writer.writerow([])
        writer.writerow(['=' * 50])
        writer.writerow(['MONTHLY SUMMARY'])
        writer.writerow(['=' * 50])
        writer.writerow(['Total Trades', len(trades)])
        writer.writerow(['Winning Trades', winning_trades])
        writer.writerow(['Losing Trades', losing_trades])
        writer.writerow(['Win Rate', f"{(winning_trades/len(trades)*100):.1f}%" if trades else "N/A"])
        writer.writerow([])
        writer.writerow(['NET P&L', f"${total_pnl:+.2f}"])
        
        # Calculate fee based on tier
        fee_rates = {'team': 0.0, 'vip': 0.05, 'standard': 0.10}
        fee_rate = fee_rates.get(user['fee_tier'] or 'standard', 0.10)
        fee_due = max(0, total_pnl * fee_rate) if total_pnl > 0 else 0
        
        writer.writerow(['Fee Rate', f"{int(fee_rate * 100)}%"])
        writer.writerow(['Fee Due', f"${fee_due:.2f}"])
        
        # Prepare response
        output.seek(0)
        filename = f"trades_{year}_{month:02d}_{user['email'].split('@')[0]}.csv"
        
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error exporting monthly trades: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/portfolio/trades/yearly-csv")
async def export_yearly_trades(request: Request, key: str, year: int):
    """
    Export yearly trades as CSV for customer dashboard
    
    Includes:
    - All trades for the year
    - Monthly breakdown
    - Yearly summary
    """
    DATABASE_URL = os.getenv("DATABASE_URL")
    if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        
        # Verify user exists
        user = await conn.fetchrow(
            "SELECT id, email, fee_tier FROM follower_users WHERE api_key = $1",
            key
        )
        
        if not user:
            await conn.close()
            raise HTTPException(status_code=404, detail="User not found")
        
        # Get all trades for the year
        start_date = datetime(year, 1, 1)
        end_date = datetime(year + 1, 1, 1)
        
        trades = await conn.fetch("""
            SELECT 
                closed_at,
                symbol,
                side,
                entry_price,
                exit_price,
                position_size,
                leverage,
                profit_usd,
                profit_percent,
                notes
            FROM trades
            WHERE user_id = $1
            AND closed_at >= $2
            AND closed_at < $3
            ORDER BY closed_at ASC
        """, user['id'], start_date, end_date)
        
        await conn.close()
        
        # Create CSV
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Header
        writer.writerow([f"Trade History - {year}"])
        writer.writerow([f"User: {user['email']}"])
        writer.writerow([f"Fee Tier: {user['fee_tier'] or 'standard'}"])
        writer.writerow([])
        
        # Column headers
        writer.writerow([
            'Date (UTC)', 'Symbol', 'Side', 'Entry Price', 'Exit Price',
            'Position Size', 'Leverage', 'P&L ($)', 'P&L (%)', 'Notes'
        ])
        
        # Track monthly stats
        monthly_pnl = {}
        total_pnl = 0
        winning_trades = 0
        losing_trades = 0
        
        for trade in trades:
            pnl = float(trade['profit_usd'] or 0)
            total_pnl += pnl
            
            # Track by month
            month_key = trade['closed_at'].strftime('%Y-%m')
            if month_key not in monthly_pnl:
                monthly_pnl[month_key] = {'pnl': 0, 'trades': 0, 'wins': 0}
            monthly_pnl[month_key]['pnl'] += pnl
            monthly_pnl[month_key]['trades'] += 1
            
            if pnl > 0:
                winning_trades += 1
                monthly_pnl[month_key]['wins'] += 1
            elif pnl < 0:
                losing_trades += 1
            
            writer.writerow([
                trade['closed_at'].strftime('%Y-%m-%d %H:%M:%S'),
                trade['symbol'],
                trade['side'],
                f"${trade['entry_price']:.4f}",
                f"${trade['exit_price']:.4f}",
                trade['position_size'],
                f"{trade['leverage']}x",
                f"${pnl:+.2f}",
                f"{trade['profit_percent']:+.2f}%",
                trade['notes'] or ''
            ])
        
        # Monthly breakdown
        writer.writerow([])
        writer.writerow(['=' * 50])
        writer.writerow(['MONTHLY BREAKDOWN'])
        writer.writerow(['=' * 50])
        writer.writerow(['Month', 'Trades', 'Wins', 'Win Rate', 'P&L'])
        
        for month_key in sorted(monthly_pnl.keys()):
            m = monthly_pnl[month_key]
            win_rate = (m['wins'] / m['trades'] * 100) if m['trades'] > 0 else 0
            writer.writerow([
                month_key,
                m['trades'],
                m['wins'],
                f"{win_rate:.1f}%",
                f"${m['pnl']:+.2f}"
            ])
        
        # Yearly summary
        writer.writerow([])
        writer.writerow(['=' * 50])
        writer.writerow(['YEARLY SUMMARY'])
        writer.writerow(['=' * 50])
        writer.writerow(['Total Trades', len(trades)])
        writer.writerow(['Winning Trades', winning_trades])
        writer.writerow(['Losing Trades', losing_trades])
        writer.writerow(['Win Rate', f"{(winning_trades/len(trades)*100):.1f}%" if trades else "N/A"])
        writer.writerow([])
        writer.writerow(['NET P&L', f"${total_pnl:+.2f}"])
        
        # Calculate fee based on tier
        fee_rates = {'team': 0.0, 'vip': 0.05, 'standard': 0.10}
        fee_rate = fee_rates.get(user['fee_tier'] or 'standard', 0.10)
        fee_due = max(0, total_pnl * fee_rate) if total_pnl > 0 else 0
        
        writer.writerow(['Fee Rate', f"{int(fee_rate * 100)}%"])
        writer.writerow(['Estimated Annual Fee', f"${fee_due:.2f}"])
        
        # Prepare response
        output.seek(0)
        filename = f"trades_{year}_{user['email'].split('@')[0]}.csv"
        
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error exporting yearly trades: {e}")
        raise HTTPException(status_code=500, detail=str(e))
