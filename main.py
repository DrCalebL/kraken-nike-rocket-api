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
    """Get all users - SCHEMA AGNOSTIC"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Check if users table exists
    if not table_exists('users'):
        cur.close()
        conn.close()
        return []
    
    # Get users table columns
    user_columns = get_table_columns('users')
    
    # Find the API key column (could be 'api_key', 'user_api_key', etc.)
    api_key_col = None
    for col in user_columns:
        if 'api' in col.lower() and 'key' in col.lower():
            api_key_col = col
            break
    
    if not api_key_col:
        api_key_col = 'api_key'  # Default guess
    
    # Find email column
    email_col = 'email' if 'email' in user_columns else user_columns[0]
    
    try:
        # Get all users
        cur.execute(f"SELECT {email_col}, {api_key_col} FROM users ORDER BY id DESC")
        
        users = []
        for row in cur.fetchall():
            email, api_key = row
            
            # Get agent status (if agent_logs exists)
            status = {'status': 'pending', 'status_text': 'Setup Pending', 'emoji': '⏳', 'detail': 'Waiting'}
            if table_exists('agent_logs'):
                cur.execute("""
                    SELECT timestamp FROM agent_logs 
                    WHERE api_key = %s AND event_type = 'heartbeat'
                    ORDER BY timestamp DESC LIMIT 1
                """, (api_key,))
                heartbeat = cur.fetchone()
                
                if heartbeat:
                    time_diff = (datetime.utcnow() - heartbeat[0]).seconds
                    if time_diff < 300:  # 5 minutes
                        status = {'status': 'active', 'status_text': 'Active', 'emoji': '🟢', 'detail': 'Running'}
            
            # Get trade stats (if trades table exists)
            total_trades = 0
            total_profit = 0.0
            if table_exists('trades'):
                try:
                    cur.execute("SELECT COUNT(*), COALESCE(SUM(profit), 0) FROM trades WHERE api_key = %s", (api_key,))
                    trade_row = cur.fetchone()
                    total_trades = trade_row[0] if trade_row else 0
                    total_profit = float(trade_row[1]) if trade_row else 0.0
                except:
                    pass
            
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
                'status_detail': status['detail'],
                'total_trades': total_trades,
                'last_trade_str': 'Never',
                'total_profit': total_profit,
                'recent_errors': recent_errors,
                'created_at': datetime.utcnow()
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
    """Get summary statistics"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Count users
    total_users = 0
    if table_exists('users'):
        try:
            cur.execute("SELECT COUNT(*) FROM users")
            total_users = cur.fetchone()[0]
        except:
            pass
    
    # Count active agents
    active_now = 0
    setup_completed = 0
    if table_exists('agent_logs'):
        try:
            cur.execute("""
                SELECT COUNT(DISTINCT api_key) FROM agent_logs 
                WHERE event_type = 'heartbeat' 
                AND timestamp > NOW() - INTERVAL '5 minutes'
            """)
            active_now = cur.fetchone()[0]
            
            cur.execute("""
                SELECT COUNT(DISTINCT api_key) FROM agent_logs 
                WHERE event_type = 'kraken_auth_success'
            """)
            setup_completed = cur.fetchone()[0]
        except:
            pass
    
    # Count trades
    total_trades = 0
    total_profit = 0.0
    if table_exists('trades'):
        try:
            cur.execute("SELECT COUNT(*), COALESCE(SUM(profit), 0) FROM trades")
            row = cur.fetchone()
            total_trades = row[0] if row else 0
            total_profit = float(row[1]) if row else 0.0
        except:
            pass
    
    # Count errors
    recent_errors = 0
    if table_exists('error_logs'):
        try:
            cur.execute("SELECT COUNT(*) FROM error_logs WHERE timestamp > NOW() - INTERVAL '1 hour'")
            recent_errors = cur.fetchone()[0]
        except:
            pass
    
    cur.close()
    conn.close()
    
    return {
        'total_users': total_users,
        'setup_completed': setup_completed,
        'setup_pending': total_users - setup_completed,
        'setup_rate': f"{(setup_completed/total_users*100) if total_users > 0 else 0:.1f}%",
        'total_trades': total_trades,
        'active_now': active_now,
        'active_rate': f"{(active_now/setup_completed*100) if setup_completed > 0 else 0:.1f}%",
        'total_profit': total_profit,
        'avg_profit_per_user': total_profit / setup_completed if setup_completed > 0 else 0.0,
        'recent_errors': recent_errors
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
    """Generate admin dashboard HTML"""
    
    # User rows
    user_rows = ""
    if not users:
        user_rows = "<tr><td colspan='8' style='text-align: center; padding: 40px;'>No users yet</td></tr>"
    else:
        for user in users:
            status_class = f"status-{user['agent_status']}"
            profit_class = "profit-positive" if user['total_profit'] >= 0 else "profit-negative"
            
            user_rows += f"""
            <tr>
                <td><span class="status-badge {status_class}">{user['status_emoji']} {user['status_text']}</span></td>
                <td>{user['email']}</td>
                <td class="api-key">{user['api_key'][:15]}...</td>
                <td>{user['total_trades']}</td>
                <td class="{profit_class}">${user['total_profit']:.2f}</td>
                <td>{'⚠️ ' + str(user['recent_errors']) if user['recent_errors'] > 0 else '✅ 0'}</td>
            </tr>
            """
    
    # Error items
    error_items = ""
    if not errors:
        error_items = "<div style='text-align: center; padding: 40px;'>No errors 🎉</div>"
    else:
        for error in errors:
            error_items += f"""
            <div class="error-item">
                <div class="error-header">
                    <span class="error-type">{error['error_type']}</span>
                    <span class="error-timestamp">{error['timestamp']}</span>
                </div>
                <div class="error-message">{error['error_message'][:200]}</div>
            </div>
            """
    
    profit_color = "#10b981" if stats['total_profit'] >= 0 else "#ef4444"
    
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>$NIKEPIG Admin - Dashboard</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; padding: 20px; }}
        .container {{ max-width: 1600px; margin: 0 auto; }}
        .header {{ background: white; border-radius: 12px; padding: 30px; margin-bottom: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }}
        h1 {{ color: #667eea; }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 20px; }}
        .stat-card {{ background: white; border-radius: 12px; padding: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }}
        .stat-label {{ color: #666; font-size: 13px; margin-bottom: 8px; text-transform: uppercase; }}
        .stat-value {{ font-size: 36px; font-weight: bold; color: #333; }}
        .users-section {{ background: white; border-radius: 12px; padding: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); margin-bottom: 20px; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th {{ background: #f9fafb; padding: 12px; text-align: left; font-weight: 600; }}
        td {{ padding: 12px; border-bottom: 1px solid #e5e7eb; }}
        .status-badge {{ display: inline-block; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 600; }}
        .status-active {{ background: #d1fae5; color: #065f46; }}
        .status-pending {{ background: #e5e7eb; color: #374151; }}
        .profit-positive {{ color: #10b981; font-weight: 600; }}
        .profit-negative {{ color: #ef4444; font-weight: 600; }}
        .errors-section {{ background: white; border-radius: 12px; padding: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }}
        .error-item {{ border-left: 4px solid #ef4444; background: #fef2f2; padding: 15px; margin-bottom: 12px; border-radius: 4px; }}
        .error-type {{ font-weight: 600; color: #991b1b; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🚀 $NIKEPIG Admin Dashboard</h1>
            <p>Schema-Agnostic Version | {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
        </div>
        
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">Total Users</div>
                <div class="stat-value">{stats['total_users']}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Active Now</div>
                <div class="stat-value" style="color: #10b981">{stats['active_now']}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Total Trades</div>
                <div class="stat-value">{stats['total_trades']}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Total Profit</div>
                <div class="stat-value" style="color: {profit_color}">${stats['total_profit']:.2f}</div>
            </div>
        </div>
        
        <div class="users-section">
            <h2>👥 Users</h2>
            <table>
                <thead>
                    <tr>
                        <th>Status</th>
                        <th>Email</th>
                        <th>API Key</th>
                        <th>Trades</th>
                        <th>Profit</th>
                        <th>Errors (24h)</th>
                    </tr>
                </thead>
                <tbody>{user_rows}</tbody>
            </table>
        </div>
        
        <div class="errors-section">
            <h2>⚠️ Recent Errors</h2>
            {error_items}
        </div>
    </div>
</body>
</html>"""
