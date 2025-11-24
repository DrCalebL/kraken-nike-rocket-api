"""
Admin Dashboard - STREAMLINED VERSION
======================================
Uses the SAME tables as the user dashboard:
- follower_users (users & agent status)
- portfolio_users (portfolio tracking)
- portfolio_trades (trade records with pnl_usd)
- portfolio_transactions (deposits/withdrawals)

NO duplicate tables - single source of truth!
"""

import os
import psycopg2
from datetime import datetime
from typing import List, Dict, Optional

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme123")


def get_db_connection():
    """Get database connection with proper URL format"""
    db_url = DATABASE_URL
    if db_url and db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(db_url)


def table_exists(table_name: str) -> bool:
    """Check if a table exists"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = %s
            )
        """, (table_name,))
        exists = cur.fetchone()[0]
        cur.close()
        conn.close()
        return exists
    except:
        return False


def get_all_users_with_status() -> List[Dict]:
    """
    Get all users with their stats
    
    TABLES USED (same as user dashboard):
    - follower_users: user info + agent_active status
    - portfolio_users: portfolio tracking
    - portfolio_trades: trade records with pnl_usd
    """
    conn = get_db_connection()
    cur = conn.cursor()
    
    if not table_exists('follower_users'):
        cur.close()
        conn.close()
        return []
    
    try:
        # Get all users from follower_users (same table as user dashboard)
        cur.execute("""
            SELECT 
                fu.id,
                fu.email,
                fu.api_key,
                fu.agent_active,
                fu.credentials_set,
                fu.created_at
            FROM follower_users fu
            ORDER BY fu.created_at DESC
        """)
        
        users = []
        for row in cur.fetchall():
            user_id, email, api_key, agent_active, credentials_set, created_at = row
            
            # Determine status from agent_active flag (same as user dashboard)
            if agent_active:
                status = {'status': 'active', 'status_text': 'Active', 'emoji': '🟢', 'detail': 'Trading'}
            elif credentials_set:
                status = {'status': 'ready', 'status_text': 'Ready', 'emoji': '🟡', 'detail': 'Agent Off'}
            else:
                status = {'status': 'pending', 'status_text': 'Setup Pending', 'emoji': '⏳', 'detail': 'No Credentials'}
            
            # Get trade stats from portfolio_trades (same table as user dashboard)
            total_trades = 0
            total_profit = 0.0
            winning_trades = 0
            last_trade_time = None
            
            if table_exists('portfolio_trades') and table_exists('portfolio_users'):
                try:
                    cur.execute("""
                        SELECT 
                            COUNT(*),
                            COALESCE(SUM(pt.pnl_usd), 0),
                            COUNT(CASE WHEN pt.pnl_usd > 0 THEN 1 END),
                            MAX(pt.exit_time)
                        FROM portfolio_trades pt
                        JOIN portfolio_users pu ON pt.user_id = pu.id
                        WHERE pu.api_key = %s
                    """, (api_key,))
                    trade_row = cur.fetchone()
                    if trade_row:
                        total_trades = trade_row[0] or 0
                        total_profit = float(trade_row[1] or 0)
                        winning_trades = trade_row[2] or 0
                        last_trade_time = trade_row[3]
                except Exception as e:
                    print(f"Error getting trade stats: {e}")
            
            # Get portfolio info
            initial_capital = 0
            current_balance = 0
            total_deposits = 0
            total_withdrawals = 0
            
            if table_exists('portfolio_users'):
                try:
                    cur.execute("""
                        SELECT 
                            initial_capital,
                            last_known_balance,
                            COALESCE(total_deposits, 0),
                            COALESCE(total_withdrawals, 0)
                        FROM portfolio_users
                        WHERE api_key = %s
                    """, (api_key,))
                    portfolio_row = cur.fetchone()
                    if portfolio_row:
                        initial_capital = float(portfolio_row[0] or 0)
                        current_balance = float(portfolio_row[1] or 0)
                        total_deposits = float(portfolio_row[2] or 0)
                        total_withdrawals = float(portfolio_row[3] or 0)
                except Exception as e:
                    print(f"Error getting portfolio: {e}")
            
            # Calculate win rate
            win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
            
            # Calculate ROI
            total_capital = initial_capital + total_deposits - total_withdrawals
            roi = (total_profit / total_capital * 100) if total_capital > 0 else 0
            
            # Format last trade time
            if last_trade_time:
                last_trade_str = last_trade_time.strftime('%Y-%m-%d %H:%M')
            else:
                last_trade_str = 'Never'
            
            users.append({
                'email': email,
                'api_key': api_key,
                'agent_status': status['status'],
                'status_text': status['status_text'],
                'status_emoji': status['emoji'],
                'status_detail': status['detail'],
                'total_trades': total_trades,
                'winning_trades': winning_trades,
                'win_rate': win_rate,
                'last_trade_str': last_trade_str,
                'total_profit': total_profit,
                'initial_capital': initial_capital,
                'current_balance': current_balance,
                'total_deposits': total_deposits,
                'total_withdrawals': total_withdrawals,
                'roi': roi,
                'created_at': created_at
            })
        
        cur.close()
        conn.close()
        return users
        
    except Exception as e:
        print(f"Error in get_all_users_with_status: {e}")
        import traceback
        traceback.print_exc()
        cur.close()
        conn.close()
        return []


def get_recent_transactions(limit: int = 20) -> List[Dict]:
    """Get recent deposits/withdrawals across all users"""
    if not table_exists('portfolio_transactions'):
        return []
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT 
                pt.user_id as api_key,
                pt.transaction_type,
                pt.amount,
                pt.detected_at,
                pt.detection_method,
                fu.email
            FROM portfolio_transactions pt
            LEFT JOIN follower_users fu ON fu.api_key = pt.user_id
            ORDER BY pt.detected_at DESC
            LIMIT %s
        """, (limit,))
        
        transactions = []
        for row in cur.fetchall():
            transactions.append({
                'api_key': row[0],
                'type': row[1],
                'amount': float(row[2] or 0),
                'timestamp': row[3],
                'method': row[4],
                'email': row[5] or 'Unknown'
            })
        
        cur.close()
        conn.close()
        return transactions
    except Exception as e:
        print(f"Error getting transactions: {e}")
        return []


def get_recent_trades(limit: int = 20) -> List[Dict]:
    """Get recent trades across all users"""
    if not table_exists('portfolio_trades') or not table_exists('portfolio_users'):
        return []
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT 
                pu.api_key,
                fu.email,
                pt.symbol,
                pt.side,
                pt.pnl_usd,
                pt.pnl_percent,
                pt.exit_time,
                pt.leverage
            FROM portfolio_trades pt
            JOIN portfolio_users pu ON pt.user_id = pu.id
            LEFT JOIN follower_users fu ON fu.api_key = pu.api_key
            WHERE pt.exit_time IS NOT NULL
            ORDER BY pt.exit_time DESC
            LIMIT %s
        """, (limit,))
        
        trades = []
        for row in cur.fetchall():
            trades.append({
                'api_key': row[0],
                'email': row[1] or 'Unknown',
                'symbol': row[2],
                'side': row[3],
                'pnl_usd': float(row[4] or 0),
                'pnl_percent': float(row[5] or 0),
                'exit_time': row[6],
                'leverage': float(row[7] or 1)
            })
        
        cur.close()
        conn.close()
        return trades
    except Exception as e:
        print(f"Error getting trades: {e}")
        return []


def get_stats_summary() -> Dict:
    """
    Get summary statistics
    
    FORMULAS:
    ═══════════════════════════════════════════════════════════════
    total_users = COUNT(*) FROM follower_users
    setup_completed = COUNT(*) FROM follower_users WHERE credentials_set = true
    setup_pending = total_users - setup_completed
    setup_rate = (setup_completed / total_users) × 100
    
    active_now = COUNT(*) FROM follower_users WHERE agent_active = true
    active_rate = (active_now / setup_completed) × 100
    
    total_trades = COUNT(*) FROM portfolio_trades
    total_profit = SUM(pnl_usd) FROM portfolio_trades
    
    total_volume = SUM(initial_capital + total_deposits) FROM portfolio_users
    avg_profit_per_user = total_profit / users_with_trades
    
    platform_fee_owed = SUM(pnl_usd * 0.10) WHERE pnl_usd > 0  (10% of profits)
    ═══════════════════════════════════════════════════════════════
    """
    conn = get_db_connection()
    cur = conn.cursor()
    
    # User counts from follower_users
    total_users = 0
    setup_completed = 0
    active_now = 0
    
    if table_exists('follower_users'):
        try:
            cur.execute("SELECT COUNT(*) FROM follower_users")
            total_users = cur.fetchone()[0]
            
            cur.execute("SELECT COUNT(*) FROM follower_users WHERE credentials_set = true")
            setup_completed = cur.fetchone()[0]
            
            cur.execute("SELECT COUNT(*) FROM follower_users WHERE agent_active = true")
            active_now = cur.fetchone()[0]
        except Exception as e:
            print(f"Error counting users: {e}")
    
    # Trade stats from portfolio_trades
    total_trades = 0
    total_profit = 0.0
    total_wins = 0.0
    total_losses = 0.0
    winning_trades = 0
    losing_trades = 0
    
    if table_exists('portfolio_trades'):
        try:
            cur.execute("""
                SELECT 
                    COUNT(*),
                    COALESCE(SUM(pnl_usd), 0),
                    COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN pnl_usd ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN pnl_usd < 0 THEN pnl_usd ELSE 0 END), 0),
                    COUNT(CASE WHEN pnl_usd > 0 THEN 1 END),
                    COUNT(CASE WHEN pnl_usd < 0 THEN 1 END)
                FROM portfolio_trades
            """)
            row = cur.fetchone()
            if row:
                total_trades = row[0] or 0
                total_profit = float(row[1] or 0)
                total_wins = float(row[2] or 0)
                total_losses = abs(float(row[3] or 0))
                winning_trades = row[4] or 0
                losing_trades = row[5] or 0
        except Exception as e:
            print(f"Error getting trade stats: {e}")
    
    # Portfolio totals
    total_volume = 0.0
    total_deposits_all = 0.0
    total_withdrawals_all = 0.0
    users_with_portfolio = 0
    
    if table_exists('portfolio_users'):
        try:
            cur.execute("""
                SELECT 
                    COUNT(*),
                    COALESCE(SUM(initial_capital), 0),
                    COALESCE(SUM(total_deposits), 0),
                    COALESCE(SUM(total_withdrawals), 0)
                FROM portfolio_users
            """)
            row = cur.fetchone()
            if row:
                users_with_portfolio = row[0] or 0
                total_volume = float(row[1] or 0) + float(row[2] or 0)
                total_deposits_all = float(row[2] or 0)
                total_withdrawals_all = float(row[3] or 0)
        except Exception as e:
            print(f"Error getting portfolio stats: {e}")
    
    # Calculate derived metrics
    setup_pending = total_users - setup_completed
    setup_rate = (setup_completed / total_users * 100) if total_users > 0 else 0
    active_rate = (active_now / setup_completed * 100) if setup_completed > 0 else 0
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
    profit_factor = (total_wins / total_losses) if total_losses > 0 else (999 if total_wins > 0 else 0)
    avg_profit_per_user = total_profit / users_with_portfolio if users_with_portfolio > 0 else 0
    
    # Platform fee (10% of profits)
    platform_fee_owed = total_wins * 0.10
    
    cur.close()
    conn.close()
    
    return {
        # User metrics
        'total_users': total_users,
        'setup_completed': setup_completed,
        'setup_pending': setup_pending,
        'setup_rate': setup_rate,
        'active_now': active_now,
        'active_rate': active_rate,
        
        # Trade metrics
        'total_trades': total_trades,
        'winning_trades': winning_trades,
        'losing_trades': losing_trades,
        'win_rate': win_rate,
        'profit_factor': profit_factor,
        
        # Profit metrics
        'total_profit': total_profit,
        'total_wins': total_wins,
        'total_losses': total_losses,
        'avg_profit_per_user': avg_profit_per_user,
        
        # Volume metrics
        'total_volume': total_volume,
        'total_deposits': total_deposits_all,
        'total_withdrawals': total_withdrawals_all,
        'users_with_portfolio': users_with_portfolio,
        
        # Platform revenue
        'platform_fee_owed': platform_fee_owed
    }


def generate_admin_html(users: List[Dict], stats: Dict) -> str:
    """Generate admin dashboard HTML"""
    
    # Get recent trades and transactions
    recent_trades = get_recent_trades(10)
    recent_transactions = get_recent_transactions(10)
    
    # User rows
    user_rows = ""
    if not users:
        user_rows = "<tr><td colspan='9' style='text-align: center; padding: 40px;'>No users yet</td></tr>"
    else:
        for user in users:
            status_class = f"status-{user['agent_status']}"
            profit_class = "profit-positive" if user['total_profit'] >= 0 else "profit-negative"
            roi_class = "profit-positive" if user['roi'] >= 0 else "profit-negative"
            
            profit_display = f"+${user['total_profit']:.2f}" if user['total_profit'] >= 0 else f"-${abs(user['total_profit']):.2f}"
            roi_display = f"+{user['roi']:.1f}%" if user['roi'] >= 0 else f"{user['roi']:.1f}%"
            
            user_rows += f"""
            <tr>
                <td><span class="status-badge {status_class}">{user['status_emoji']} {user['status_text']}</span></td>
                <td>{user['email']}</td>
                <td class="api-key">{user['api_key'][:12]}...</td>
                <td>${user['current_balance']:,.0f}</td>
                <td>{user['total_trades']}</td>
                <td>{user['win_rate']:.0f}%</td>
                <td class="{profit_class}">{profit_display}</td>
                <td class="{roi_class}">{roi_display}</td>
                <td>{user['last_trade_str']}</td>
            </tr>
            """
    
    # Recent trades rows
    trade_rows = ""
    if not recent_trades:
        trade_rows = "<tr><td colspan='6' style='text-align: center; padding: 20px;'>No trades yet</td></tr>"
    else:
        for trade in recent_trades:
            pnl_class = "profit-positive" if trade['pnl_usd'] >= 0 else "profit-negative"
            pnl_display = f"+${trade['pnl_usd']:.2f}" if trade['pnl_usd'] >= 0 else f"-${abs(trade['pnl_usd']):.2f}"
            side_class = "side-long" if trade['side'].upper() == 'LONG' else "side-short"
            time_str = trade['exit_time'].strftime('%m/%d %H:%M') if trade['exit_time'] else '-'
            
            trade_rows += f"""
            <tr>
                <td>{trade['email'][:15]}...</td>
                <td><span class="{side_class}">{trade['side']}</span> {trade['symbol']}</td>
                <td>{trade['leverage']:.0f}x</td>
                <td class="{pnl_class}">{pnl_display}</td>
                <td>{trade['pnl_percent']:.1f}%</td>
                <td>{time_str}</td>
            </tr>
            """
    
    # Recent transactions rows
    tx_rows = ""
    if not recent_transactions:
        tx_rows = "<tr><td colspan='4' style='text-align: center; padding: 20px;'>No transactions yet</td></tr>"
    else:
        for tx in recent_transactions:
            tx_class = "profit-positive" if tx['type'] == 'deposit' else "profit-negative"
            tx_icon = "💰" if tx['type'] == 'deposit' else "💸"
            tx_sign = "+" if tx['type'] == 'deposit' else "-"
            time_str = tx['timestamp'].strftime('%m/%d %H:%M') if tx['timestamp'] else '-'
            
            tx_rows += f"""
            <tr>
                <td>{tx['email'][:15]}...</td>
                <td>{tx_icon} {tx['type'].title()}</td>
                <td class="{tx_class}">{tx_sign}${tx['amount']:,.2f}</td>
                <td>{time_str}</td>
            </tr>
            """
    
    # Colors for stats
    profit_color = "#10b981" if stats['total_profit'] >= 0 else "#ef4444"
    
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="60">
    <title>🚀 $NIKEPIG Admin Dashboard</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); 
            min-height: 100vh; 
            padding: 20px;
            color: #e5e7eb;
        }}
        .container {{ max-width: 1800px; margin: 0 auto; }}
        
        .header {{ 
            background: rgba(255,255,255,0.05); 
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 16px; 
            padding: 30px; 
            margin-bottom: 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        h1 {{ color: #667eea; font-size: 28px; }}
        .header-time {{ color: #9ca3af; font-size: 14px; }}
        
        .stats-grid {{ 
            display: grid; 
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); 
            gap: 15px; 
            margin-bottom: 20px; 
        }}
        .stat-card {{ 
            background: rgba(255,255,255,0.05);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px; 
            padding: 20px;
            transition: transform 0.2s;
        }}
        .stat-card:hover {{ transform: translateY(-2px); }}
        .stat-label {{ color: #9ca3af; font-size: 12px; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }}
        .stat-value {{ font-size: 28px; font-weight: bold; color: #fff; }}
        .stat-value.green {{ color: #10b981; }}
        .stat-value.red {{ color: #ef4444; }}
        .stat-value.blue {{ color: #3b82f6; }}
        .stat-value.yellow {{ color: #f59e0b; }}
        .stat-sub {{ font-size: 12px; color: #6b7280; margin-top: 4px; }}
        
        .section {{ 
            background: rgba(255,255,255,0.05);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px; 
            padding: 20px; 
            margin-bottom: 20px;
        }}
        .section h2 {{ color: #667eea; margin-bottom: 15px; font-size: 18px; }}
        
        table {{ width: 100%; border-collapse: collapse; }}
        th {{ 
            background: rgba(102, 126, 234, 0.2); 
            padding: 12px; 
            text-align: left; 
            font-weight: 600; 
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: #a5b4fc;
        }}
        td {{ 
            padding: 12px; 
            border-bottom: 1px solid rgba(255,255,255,0.05);
            font-size: 14px;
        }}
        tr:hover {{ background: rgba(255,255,255,0.02); }}
        
        .status-badge {{ 
            display: inline-block; 
            padding: 4px 10px; 
            border-radius: 12px; 
            font-size: 11px; 
            font-weight: 600; 
        }}
        .status-active {{ background: rgba(16, 185, 129, 0.2); color: #34d399; }}
        .status-ready {{ background: rgba(245, 158, 11, 0.2); color: #fbbf24; }}
        .status-pending {{ background: rgba(107, 114, 128, 0.2); color: #9ca3af; }}
        
        .profit-positive {{ color: #10b981; font-weight: 600; }}
        .profit-negative {{ color: #ef4444; font-weight: 600; }}
        
        .side-long {{ color: #10b981; font-weight: 600; }}
        .side-short {{ color: #ef4444; font-weight: 600; }}
        
        .api-key {{ font-family: monospace; font-size: 12px; color: #6b7280; }}
        
        .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
        @media (max-width: 1200px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}
        
        .formula-box {{
            background: rgba(102, 126, 234, 0.1);
            border: 1px solid rgba(102, 126, 234, 0.3);
            border-radius: 8px;
            padding: 15px;
            margin-top: 15px;
            font-family: monospace;
            font-size: 12px;
            color: #a5b4fc;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>🚀 $NIKEPIG Admin Dashboard</h1>
                <p style="color: #6b7280; margin-top: 5px;">Real-time platform metrics</p>
            </div>
            <div class="header-time">
                Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}<br>
                <span style="font-size: 12px;">Auto-refresh: 60s</span>
            </div>
        </div>
        
        <!-- Main Stats -->
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">Total Users</div>
                <div class="stat-value">{stats['total_users']}</div>
                <div class="stat-sub">{stats['setup_completed']} setup complete</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Active Now</div>
                <div class="stat-value green">{stats['active_now']}</div>
                <div class="stat-sub">{stats['active_rate']:.0f}% of setup users</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Total Trades</div>
                <div class="stat-value blue">{stats['total_trades']}</div>
                <div class="stat-sub">{stats['win_rate']:.0f}% win rate</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Total Profit</div>
                <div class="stat-value {'green' if stats['total_profit'] >= 0 else 'red'}">
                    {'+'if stats['total_profit'] >= 0 else ''}${stats['total_profit']:,.2f}
                </div>
                <div class="stat-sub">Profit factor: {stats['profit_factor']:.2f}x</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Total Volume</div>
                <div class="stat-value">${stats['total_volume']:,.0f}</div>
                <div class="stat-sub">{stats['users_with_portfolio']} portfolios</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Avg Profit/User</div>
                <div class="stat-value {'green' if stats['avg_profit_per_user'] >= 0 else 'red'}">
                    {'+'if stats['avg_profit_per_user'] >= 0 else ''}${stats['avg_profit_per_user']:,.2f}
                </div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Platform Fee Owed</div>
                <div class="stat-value yellow">${stats['platform_fee_owed']:,.2f}</div>
                <div class="stat-sub">10% of profits</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Net Deposits</div>
                <div class="stat-value">${stats['total_deposits'] - stats['total_withdrawals']:,.0f}</div>
                <div class="stat-sub">+${stats['total_deposits']:,.0f} / -${stats['total_withdrawals']:,.0f}</div>
            </div>
        </div>
        
        <!-- Users Table -->
        <div class="section">
            <h2>👥 All Users ({len(users)})</h2>
            <div style="overflow-x: auto;">
                <table>
                    <thead>
                        <tr>
                            <th>Status</th>
                            <th>Email</th>
                            <th>API Key</th>
                            <th>Balance</th>
                            <th>Trades</th>
                            <th>Win Rate</th>
                            <th>Profit</th>
                            <th>ROI</th>
                            <th>Last Trade</th>
                        </tr>
                    </thead>
                    <tbody>{user_rows}</tbody>
                </table>
            </div>
        </div>
        
        <!-- Recent Activity -->
        <div class="grid-2">
            <div class="section">
                <h2>📈 Recent Trades</h2>
                <table>
                    <thead>
                        <tr>
                            <th>User</th>
                            <th>Trade</th>
                            <th>Leverage</th>
                            <th>PnL</th>
                            <th>%</th>
                            <th>Time</th>
                        </tr>
                    </thead>
                    <tbody>{trade_rows}</tbody>
                </table>
            </div>
            
            <div class="section">
                <h2>💰 Recent Transactions</h2>
                <table>
                    <thead>
                        <tr>
                            <th>User</th>
                            <th>Type</th>
                            <th>Amount</th>
                            <th>Time</th>
                        </tr>
                    </thead>
                    <tbody>{tx_rows}</tbody>
                </table>
            </div>
        </div>
        
        <!-- Formulas Reference -->
        <div class="section">
            <h2>📊 Formula Reference</h2>
            <div class="formula-box">
                <strong>User Metrics:</strong><br>
                • Setup Rate = (setup_completed / total_users) × 100<br>
                • Active Rate = (active_now / setup_completed) × 100<br><br>
                
                <strong>Trade Metrics:</strong><br>
                • Win Rate = (winning_trades / total_trades) × 100<br>
                • Profit Factor = SUM(winning_pnl) / ABS(SUM(losing_pnl))<br>
                • Avg Profit/User = total_profit / users_with_portfolio<br><br>
                
                <strong>User ROI:</strong><br>
                • Total Capital = initial_capital + deposits - withdrawals<br>
                • ROI = (total_profit / total_capital) × 100<br><br>
                
                <strong>Platform Revenue:</strong><br>
                • Fee Owed = SUM(pnl_usd WHERE pnl_usd > 0) × 10%
            </div>
        </div>
    </div>
</body>
</html>"""
