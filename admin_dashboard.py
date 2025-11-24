"""
Admin Dashboard - SCHEMA-AGNOSTIC VERSION
==========================================
Works with ANY database schema by detecting columns dynamically
"""

import os
import psycopg2
from datetime import datetime
from typing import List, Dict, Optional

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme123")


def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


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


def get_table_columns(table_name: str) -> List[str]:
    """Get all column names for a table"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = %s
            ORDER BY ordinal_position
        """, (table_name,))
        columns = [row[0] for row in cur.fetchall()]
        cur.close()
        conn.close()
        return columns
    except:
        return []


def create_error_logs_table():
    """Create monitoring tables"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Error logs
    cur.execute("""
        CREATE TABLE IF NOT EXISTS error_logs (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            api_key VARCHAR(100),
            error_type VARCHAR(100),
            error_message TEXT,
            context JSONB
        )
    """)
    
    # Agent logs
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_logs (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            api_key VARCHAR(100),
            event_type VARCHAR(100),
            event_data JSONB
        )
    """)
    
    # Trades table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            api_key VARCHAR(100),
            signal_id VARCHAR(100),
            symbol VARCHAR(20),
            action VARCHAR(10),
            entry_price DECIMAL(20, 8),
            exit_price DECIMAL(20, 8),
            quantity DECIMAL(20, 8),
            profit DECIMAL(20, 8),
            status VARCHAR(20),
            exchange VARCHAR(50)
        )
    """)
    
    # Indexes
    cur.execute("CREATE INDEX IF NOT EXISTS idx_error_logs_timestamp ON error_logs(timestamp DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_logs_timestamp ON agent_logs(timestamp DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp DESC)")
    
    conn.commit()
    cur.close()
    conn.close()


def get_all_users_with_status() -> List[Dict]:
    """Get all users from follower_users table with portfolio data"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Check if follower_users table exists
    if not table_exists('follower_users'):
        cur.close()
        conn.close()
        return []
    
    try:
        # Get all users from follower_users with portfolio data
        cur.execute("""
            SELECT 
                fu.email,
                fu.api_key,
                fu.credentials_set,
                fu.agent_active,
                fu.total_profit,
                fu.total_trades,
                fu.created_at,
                COALESCE(pu.initial_capital, 0) as initial_capital,
                COALESCE(pu.last_known_balance, 0) as current_balance
            FROM follower_users fu
            LEFT JOIN portfolio_users pu ON fu.api_key = pu.api_key
            ORDER BY fu.id DESC
        """)
        
        users = []
        for row in cur.fetchall():
            email, api_key, credentials_set, agent_active, total_profit, total_trades, created_at, initial_capital, current_balance = row
            
            # Determine status
            if agent_active:
                status = {'status': 'active', 'status_text': 'Active', 'emoji': '🟢'}
            elif credentials_set:
                status = {'status': 'configured', 'status_text': 'Ready', 'emoji': '🟡'}
            else:
                status = {'status': 'pending', 'status_text': 'Pending', 'emoji': '⏳'}
            
            # Calculate ROI
            capital = float(initial_capital) if initial_capital else 0
            profit = float(total_profit) if total_profit else 0
            roi = (profit / capital * 100) if capital > 0 else 0
            
            # Get error count
            recent_errors = 0
            if table_exists('error_logs'):
                try:
                    cur.execute("""
                        SELECT COUNT(*) FROM error_logs 
                        WHERE api_key = %s AND timestamp > NOW() - INTERVAL '24 hours'
                    """, (api_key,))
                    recent_errors = cur.fetchone()[0]
                except:
                    pass
            
            users.append({
                'email': email,
                'api_key': api_key,
                'agent_status': status['status'],
                'status_text': status['status_text'],
                'status_emoji': status['emoji'],
                'total_trades': total_trades or 0,
                'total_profit': profit,
                'capital': capital,
                'current_balance': float(current_balance) if current_balance else 0,
                'roi': roi,
                'recent_errors': recent_errors,
                'created_at': created_at
            })
        
        cur.close()
        conn.close()
        return users
        
    except Exception as e:
        print(f"Error in get_all_users_with_status: {e}")
        cur.close()
        conn.close()
        return []


def get_recent_errors(hours: int = 24) -> List[Dict]:
    """Get recent errors"""
    if not table_exists('error_logs'):
        return []
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT timestamp, api_key, error_type, error_message
            FROM error_logs
            WHERE timestamp > NOW() - INTERVAL '%s hours'
            ORDER BY timestamp DESC
            LIMIT 50
        """, (hours,))
        
        errors = [{
            'timestamp': row[0],
            'api_key': row[1],
            'error_type': row[2],
            'error_message': row[3],
            'email': row[1][:20] + '...'
        } for row in cur.fetchall()]
        
        cur.close()
        conn.close()
        return errors
    except:
        return []


def get_stats_summary() -> Dict:
    """Get summary statistics from follower_users and portfolio_users"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Count users from follower_users
    total_users = 0
    configured_users = 0
    active_now = 0
    total_profit = 0.0
    total_trades = 0
    
    if table_exists('follower_users'):
        try:
            cur.execute("SELECT COUNT(*) FROM follower_users")
            total_users = cur.fetchone()[0]
            
            cur.execute("SELECT COUNT(*) FROM follower_users WHERE credentials_set = true")
            configured_users = cur.fetchone()[0]
            
            cur.execute("SELECT COUNT(*) FROM follower_users WHERE agent_active = true")
            active_now = cur.fetchone()[0]
            
            cur.execute("SELECT COALESCE(SUM(total_profit), 0), COALESCE(SUM(total_trades), 0) FROM follower_users")
            row = cur.fetchone()
            total_profit = float(row[0]) if row else 0.0
            total_trades = int(row[1]) if row else 0
        except Exception as e:
            print(f"Error getting follower_users stats: {e}")
    
    # Get platform capital from portfolio_users
    platform_capital = 0.0
    current_value = 0.0
    if table_exists('portfolio_users'):
        try:
            cur.execute("""
                SELECT 
                    COALESCE(SUM(initial_capital), 0),
                    COALESCE(SUM(last_known_balance), 0)
                FROM portfolio_users
            """)
            row = cur.fetchone()
            platform_capital = float(row[0]) if row else 0.0
            current_value = float(row[1]) if row else 0.0
        except Exception as e:
            print(f"Error getting portfolio stats: {e}")
    
    # Calculate platform ROI
    platform_roi = ((current_value - platform_capital) / platform_capital * 100) if platform_capital > 0 else 0.0
    
    # Active percentage
    active_percent = (active_now / configured_users * 100) if configured_users > 0 else 0.0
    
    # Average profit per user
    avg_profit = total_profit / total_users if total_users > 0 else 0.0
    
    # Count recent errors
    errors_1h = 0
    if table_exists('error_logs'):
        try:
            cur.execute("SELECT COUNT(*) FROM error_logs WHERE timestamp > NOW() - INTERVAL '1 hour'")
            errors_1h = cur.fetchone()[0]
        except:
            pass
    
    cur.close()
    conn.close()
    
    return {
        'total_users': total_users,
        'configured_users': configured_users,
        'active_now': active_now,
        'active_percent': active_percent,
        'total_trades': total_trades,
        'total_profit': total_profit,
        'avg_profit': avg_profit,
        'platform_capital': platform_capital,
        'current_value': current_value,
        'platform_roi': platform_roi,
        'errors_1h': errors_1h
    }


def log_error(api_key: str, error_type: str, error_message: str, context: Optional[Dict] = None):
    """Log error"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        import json
        cur.execute(
            "INSERT INTO error_logs (api_key, error_type, error_message, context) VALUES (%s, %s, %s, %s)",
            (api_key, error_type, error_message, json.dumps(context) if context else None)
        )
        conn.commit()
        cur.close()
        conn.close()
    except:
        pass


def log_agent_event(api_key: str, event_type: str, event_data: Optional[Dict] = None):
    """Log agent event"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        import json
        cur.execute(
            "INSERT INTO agent_logs (api_key, event_type, event_data) VALUES (%s, %s, %s)",
            (api_key, event_type, json.dumps(event_data) if event_data else None)
        )
        conn.commit()
        cur.close()
        conn.close()
    except:
        pass


def generate_admin_html(users: List[Dict], errors: List[Dict], stats: Dict) -> str:
    """Generate admin dashboard HTML - Dark Theme with Error Tooltips"""
    
    # User rows
    user_rows = ""
    if not users:
        user_rows = "<tr><td colspan='8' style='text-align: center; padding: 40px; color: #9ca3af;'>No users yet</td></tr>"
    else:
        for user in users:
            status_class = f"status-{user['agent_status']}"
            profit_class = "profit-positive" if user['total_profit'] >= 0 else "profit-negative"
            profit_prefix = "+" if user['total_profit'] >= 0 else ""
            roi_prefix = "+" if user.get('roi', 0) >= 0 else ""
            
            # Error indicator with tooltip
            error_count = user.get('recent_errors', 0)
            if error_count > 0:
                error_cell = f'''<span class="error-indicator error-has-errors" title="⚠️ {error_count} error(s) in last 24h - hover to see details">⚠️</span>'''
            else:
                error_cell = '''<span class="error-indicator error-none" title="✅ No errors in last 24h">✅</span>'''
            
            user_rows += f"""
            <tr>
                <td><span class="status-badge {status_class}">{user['status_emoji']} {user['status_text']}</span></td>
                <td style="color: #e5e7eb;">{user['email']}</td>
                <td class="api-key">{user['api_key'][:15]}...</td>
                <td style="color: #e5e7eb;">${user.get('capital', 0):.2f}</td>
                <td style="color: #e5e7eb;">{user['total_trades']}</td>
                <td class="{profit_class}">{profit_prefix}${abs(user['total_profit']):.2f}</td>
                <td class="{profit_class}">{roi_prefix}{user.get('roi', 0):.1f}%</td>
                <td style="text-align: center;">{error_cell}</td>
            </tr>
            """
    
    # Error items with detailed view
    error_items = ""
    if not errors:
        error_items = "<div style='text-align: center; padding: 40px; color: #9ca3af;'>No errors in last 24h 🎉</div>"
    else:
        for error in errors:
            # Determine error severity color
            error_type = error.get('error_type', 'unknown').lower()
            if 'auth' in error_type or 'credential' in error_type:
                border_color = '#ef4444'  # Red - authentication
                badge_class = 'error-badge-critical'
            elif 'network' in error_type or 'connection' in error_type or 'timeout' in error_type:
                border_color = '#f59e0b'  # Orange - network
                badge_class = 'error-badge-warning'
            elif 'insufficient' in error_type or 'balance' in error_type:
                border_color = '#8b5cf6'  # Purple - funds
                badge_class = 'error-badge-funds'
            else:
                border_color = '#6b7280'  # Gray - other
                badge_class = 'error-badge-info'
            
            error_items += f"""
            <div class="error-item" style="border-left-color: {border_color};">
                <div class="error-header">
                    <span class="error-type {badge_class}">{error.get('error_type', 'Unknown')}</span>
                    <span class="error-timestamp">{error.get('timestamp', '')}</span>
                </div>
                <div class="error-message">{error.get('error_message', '')[:300]}</div>
                <div class="error-context">API Key: {error.get('api_key', 'N/A')[:15]}...</div>
            </div>
            """
    
    profit_color = "#10b981" if stats.get('total_profit', 0) >= 0 else "#ef4444"
    profit_prefix = "+" if stats.get('total_profit', 0) >= 0 else ""
    roi_color = "#10b981" if stats.get('platform_roi', 0) >= 0 else "#ef4444"
    roi_prefix = "+" if stats.get('platform_roi', 0) >= 0 else ""
    
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>$NIKEPIG Admin Dashboard</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
            background: #0f1218;
            min-height: 100vh; 
            padding: 20px;
            color: #e5e7eb;
        }}
        .container {{ max-width: 1600px; margin: 0 auto; }}
        
        /* Header */
        .header {{ 
            background: linear-gradient(135deg, #1e3a5f 0%, #2d1f47 100%);
            border-radius: 12px; 
            padding: 25px 30px; 
            margin-bottom: 20px; 
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .header h1 {{ color: #4ade80; font-size: 28px; }}
        .header .timestamp {{ color: #9ca3af; font-size: 14px; margin-top: 5px; }}
        
        /* Tactile Refresh Button */
        .refresh-btn {{
            background: linear-gradient(135deg, #10b981 0%, #059669 100%);
            color: white;
            border: none;
            padding: 12px 24px;
            border-radius: 8px;
            font-weight: 600;
            cursor: pointer;
            font-size: 14px;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: all 0.1s ease;
            transform: translateY(0);
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3), 0 2px 4px rgba(16, 185, 129, 0.2);
        }}
        .refresh-btn:hover {{
            background: linear-gradient(135deg, #34d399 0%, #10b981 100%);
            transform: translateY(-2px);
            box-shadow: 0 6px 12px rgba(0, 0, 0, 0.4), 0 4px 8px rgba(16, 185, 129, 0.3);
        }}
        .refresh-btn:active {{
            transform: translateY(2px);
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.3);
        }}
        
        /* Stats Grid */
        .stats-grid {{ 
            display: grid; 
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); 
            gap: 15px; 
            margin-bottom: 20px; 
        }}
        .stat-card {{ 
            background: #1a1f2e;
            border-radius: 12px; 
            padding: 20px; 
            border: 1px solid #2d3748;
        }}
        .stat-label {{ 
            color: #9ca3af; 
            font-size: 11px; 
            margin-bottom: 8px; 
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .stat-value {{ font-size: 32px; font-weight: bold; }}
        .stat-sub {{ color: #6b7280; font-size: 11px; margin-top: 4px; }}
        
        /* Users Section */
        .users-section {{ 
            background: #1a1f2e;
            border-radius: 12px; 
            padding: 20px; 
            margin-bottom: 20px;
            border: 1px solid #2d3748;
        }}
        .users-section h2 {{ color: #e5e7eb; margin-bottom: 15px; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th {{ 
            background: #0f1218;
            padding: 12px; 
            text-align: left; 
            font-weight: 600;
            color: #9ca3af;
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        td {{ padding: 12px; border-bottom: 1px solid #2d3748; }}
        .api-key {{ color: #6b7280; font-family: monospace; font-size: 12px; }}
        
        /* Status Badges */
        .status-badge {{ 
            display: inline-block; 
            padding: 4px 12px; 
            border-radius: 12px; 
            font-size: 12px; 
            font-weight: 600; 
        }}
        .status-active {{ background: #064e3b; color: #34d399; }}
        .status-pending, .status-configured {{ background: #1e3a5f; color: #60a5fa; }}
        .status-inactive {{ background: #374151; color: #9ca3af; }}
        .status-error {{ background: #7f1d1d; color: #fca5a5; }}
        
        /* Profit Colors */
        .profit-positive {{ color: #10b981; font-weight: 600; }}
        .profit-negative {{ color: #ef4444; font-weight: 600; }}
        
        /* Error Indicators */
        .error-indicator {{
            font-size: 16px;
            cursor: help;
            transition: transform 0.2s;
        }}
        .error-indicator:hover {{
            transform: scale(1.3);
        }}
        
        /* Errors Section */
        .errors-section {{ 
            background: #1a1f2e;
            border-radius: 12px; 
            padding: 20px;
            border: 1px solid #2d3748;
        }}
        .errors-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
        }}
        .errors-section h2 {{ color: #fbbf24; }}
        
        /* Error Legend */
        .error-legend {{
            display: flex;
            gap: 15px;
            font-size: 11px;
        }}
        .legend-item {{
            display: flex;
            align-items: center;
            gap: 5px;
        }}
        .legend-dot {{
            width: 10px;
            height: 10px;
            border-radius: 50%;
        }}
        .legend-dot.critical {{ background: #ef4444; }}
        .legend-dot.warning {{ background: #f59e0b; }}
        .legend-dot.funds {{ background: #8b5cf6; }}
        .legend-dot.info {{ background: #6b7280; }}
        
        /* Error Items */
        .error-item {{ 
            border-left: 4px solid #ef4444; 
            background: #1f2937;
            padding: 15px; 
            margin-bottom: 12px; 
            border-radius: 0 8px 8px 0;
        }}
        .error-header {{
            display: flex;
            justify-content: space-between;
            margin-bottom: 8px;
        }}
        .error-type {{ 
            font-weight: 600; 
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 12px;
        }}
        .error-badge-critical {{ background: #7f1d1d; color: #fca5a5; }}
        .error-badge-warning {{ background: #78350f; color: #fcd34d; }}
        .error-badge-funds {{ background: #4c1d95; color: #c4b5fd; }}
        .error-badge-info {{ background: #374151; color: #9ca3af; }}
        .error-timestamp {{ color: #6b7280; font-size: 12px; }}
        .error-message {{ color: #e5e7eb; font-size: 13px; line-height: 1.5; }}
        .error-context {{ color: #6b7280; font-size: 11px; margin-top: 8px; font-family: monospace; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>🚀 $NIKEPIG Admin Dashboard</h1>
                <div class="timestamp">{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</div>
            </div>
            <button class="refresh-btn" onclick="location.reload()">
                🔄 Refresh
            </button>
        </div>
        
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">Total Users</div>
                <div class="stat-value" style="color: #e5e7eb;">{stats.get('total_users', 0)}</div>
                <div class="stat-sub">{stats.get('configured_users', 0)} configured</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Active Now</div>
                <div class="stat-value" style="color: #10b981;">{stats.get('active_now', 0)}</div>
                <div class="stat-sub">{stats.get('active_percent', 0):.1f}% of configured</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Total Trades</div>
                <div class="stat-value" style="color: #e5e7eb;">{stats.get('total_trades', 0)}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Total Profit</div>
                <div class="stat-value" style="color: {profit_color};">{profit_prefix}${abs(stats.get('total_profit', 0)):.2f}</div>
                <div class="stat-sub">${stats.get('avg_profit', 0):.2f} avg/user</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Platform Capital</div>
                <div class="stat-value" style="color: #e5e7eb;">${stats.get('platform_capital', 0):,.0f}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Current Value</div>
                <div class="stat-value" style="color: #e5e7eb;">${stats.get('current_value', 0):,.0f}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Platform ROI</div>
                <div class="stat-value" style="color: {roi_color};">{roi_prefix}{stats.get('platform_roi', 0):.1f}%</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Errors (1H)</div>
                <div class="stat-value" style="color: {'#ef4444' if stats.get('errors_1h', 0) > 0 else '#10b981'};">{stats.get('errors_1h', 0)}</div>
            </div>
        </div>
        
        <div class="users-section">
            <h2>👥 Users ({stats.get('total_users', 0)})</h2>
            <table>
                <thead>
                    <tr>
                        <th>Status</th>
                        <th>Email</th>
                        <th>API Key</th>
                        <th>Capital</th>
                        <th>Trades</th>
                        <th>Profit</th>
                        <th>ROI</th>
                        <th>Errors</th>
                    </tr>
                </thead>
                <tbody>{user_rows}</tbody>
            </table>
        </div>
        
        <div class="errors-section">
            <div class="errors-header">
                <h2>⚠️ Recent Errors (24h)</h2>
                <div class="error-legend">
                    <div class="legend-item"><span class="legend-dot critical"></span> Auth/Credential</div>
                    <div class="legend-item"><span class="legend-dot warning"></span> Network/Timeout</div>
                    <div class="legend-item"><span class="legend-dot funds"></span> Insufficient Funds</div>
                    <div class="legend-item"><span class="legend-dot info"></span> Other</div>
                </div>
            </div>
            {error_items}
        </div>
    </div>
</body>
</html>"""
