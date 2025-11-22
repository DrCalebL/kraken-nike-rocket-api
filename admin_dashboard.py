"""
Admin Dashboard - Monitor Hosted Follower Agents (SaaS Model)
==============================================================

View:
- User signups
- Setup completion (Kraken credentials)  
- Active follower agents
- Trading activity
- Error logs

Access: /admin?password=YOUR_ADMIN_PASSWORD

All agents run centrally on Railway - no individual deployments
"""

import os
import psycopg2
from datetime import datetime, timedelta
from typing import List, Dict, Optional

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme123")


def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def get_all_users_with_status() -> List[Dict]:
    """Get all users with setup and trading status"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    query = """
    SELECT 
        u.email,
        u.api_key,
        u.created_at,
        u.kraken_api_key IS NOT NULL AND u.kraken_api_secret IS NOT NULL as setup_complete,
        COALESCE((SELECT COUNT(*) FROM trades t WHERE t.api_key = u.api_key), 0) as total_trades,
        (SELECT MAX(timestamp) FROM trades t WHERE t.api_key = u.api_key) as last_trade_at,
        COALESCE((SELECT SUM(profit) FROM trades t WHERE t.api_key = u.api_key), 0) as total_profit
    FROM users u
    ORDER BY u.created_at DESC
    """
    
    cur.execute(query)
    rows = cur.fetchall()
    
    users = []
    for row in rows:
        email, api_key, created_at, setup_complete, total_trades, last_trade_at, total_profit = row
        
        # Determine agent status
        if not setup_complete:
            agent_status = "setup_pending"
            status_emoji = "⏳"
        elif last_trade_at and last_trade_at > datetime.utcnow() - timedelta(hours=2):
            agent_status = "active"
            status_emoji = "🟢"
        elif total_trades > 0:
            agent_status = "inactive"
            status_emoji = "🔴"
        else:
            agent_status = "waiting"
            status_emoji = "🟡"
        
        users.append({
            'email': email,
            'api_key': api_key,
            'created_at': created_at,
            'setup_complete': setup_complete,
            'total_trades': total_trades,
            'last_trade_at': last_trade_at,
            'total_profit': float(total_profit),
            'agent_status': agent_status,
            'status_emoji': status_emoji
        })
    
    cur.close()
    conn.close()
    return users


def get_recent_errors(hours: int = 24) -> List[Dict]:
    """Get recent errors"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Check if error_logs table exists
    cur.execute("SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'error_logs')")
    if not cur.fetchone()[0]:
        cur.close()
        conn.close()
        return []
    
    query = """
    SELECT e.timestamp, e.api_key, e.error_type, e.error_message, u.email
    FROM error_logs e
    LEFT JOIN users u ON e.api_key = u.api_key
    WHERE e.timestamp > NOW() - INTERVAL '%s hours'
    ORDER BY e.timestamp DESC
    LIMIT 50
    """
    
    cur.execute(query, (hours,))
    rows = cur.fetchall()
    
    errors = [{
        'timestamp': row[0],
        'api_key': row[1],
        'error_type': row[2],
        'error_message': row[3],
        'email': row[4] or 'Unknown'
    } for row in rows]
    
    cur.close()
    conn.close()
    return errors


def get_stats_summary() -> Dict:
    """Get summary statistics"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM users WHERE kraken_api_key IS NOT NULL AND kraken_api_secret IS NOT NULL")
    setup_completed = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM trades")
    total_trades = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(DISTINCT api_key) FROM trades WHERE timestamp > NOW() - INTERVAL '2 hours'")
    active_now = cur.fetchone()[0]
    
    cur.execute("SELECT COALESCE(SUM(profit), 0) FROM trades")
    total_profit = float(cur.fetchone()[0])
    
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
        'avg_profit_per_user': total_profit / setup_completed if setup_completed > 0 else 0.0
    }


def create_error_logs_table():
    """Create error_logs table"""
    conn = get_db_connection()
    cur = conn.cursor()
    
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
    
    cur.execute("CREATE INDEX IF NOT EXISTS idx_error_logs_timestamp ON error_logs(timestamp DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_error_logs_api_key ON error_logs(api_key)")
    
    conn.commit()
    cur.close()
    conn.close()


def log_error(api_key: str, error_type: str, error_message: str, context: Optional[Dict] = None):
    """
    Log error to database
    
    Error types:
    - setup_failed
    - kraken_auth_failed
    - trade_execution_failed
    - signal_processing_failed
    - balance_check_failed
    """
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
        print(f"✅ Logged error: {api_key} - {error_type}")
    except Exception as e:
        print(f"❌ Failed to log error: {e}")


def generate_admin_html(users: List[Dict], errors: List[Dict], stats: Dict) -> str:
    """Generate admin dashboard HTML"""
    
    # User rows
    user_rows = ""
    for user in users:
        status_class = f"status-{user['agent_status']}"
        profit_class = "profit-positive" if user['total_profit'] >= 0 else "profit-negative"
        
        last_trade_str = "Never"
        if user['last_trade_at']:
            delta = datetime.utcnow() - user['last_trade_at']
            if delta.seconds < 3600:
                last_trade_str = f"{delta.seconds // 60}m ago"
            elif delta.seconds < 86400:
                last_trade_str = f"{delta.seconds // 3600}h ago"
            else:
                last_trade_str = f"{delta.days}d ago"
        
        setup_status = "✅ Yes" if user['setup_complete'] else "⏳ Pending"
        
        user_rows += f"""
        <tr>
            <td><span class="status-badge {status_class}">{user['status_emoji']} {user['agent_status'].replace('_', ' ').title()}</span></td>
            <td>{user['email']}</td>
            <td class="api-key">{user['api_key'][:15]}...</td>
            <td class="timestamp">{user['created_at'].strftime('%Y-%m-%d %H:%M')}</td>
            <td>{setup_status}</td>
            <td>{user['total_trades']}</td>
            <td class="timestamp">{last_trade_str}</td>
            <td class="{profit_class}">${user['total_profit']:.2f}</td>
        </tr>
        """
    
    # Error items
    error_items = ""
    if not errors:
        error_items = "<div class='no-data'>No errors in last 24h 🎉</div>"
    else:
        for error in errors:
            error_items += f"""
            <div class="error-item">
                <div class="error-header">
                    <span class="error-type">{error['error_type']}</span>
                    <span class="error-timestamp">{error['timestamp'].strftime('%Y-%m-%d %H:%M:%S')}</span>
                </div>
                <div class="error-email">User: {error['email']} ({error['api_key'][:15]}...)</div>
                <div class="error-message">{error['error_message']}</div>
            </div>
            """
    
    profit_color = "#10b981" if stats['total_profit'] >= 0 else "#ef4444"
    
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>$NIKEPIG Admin Dashboard</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; padding: 20px; }}
        .container {{ max-width: 1600px; margin: 0 auto; }}
        .header {{ background: white; border-radius: 12px; padding: 30px; margin-bottom: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }}
        h1 {{ color: #667eea; margin-bottom: 5px; }}
        .subtitle {{ color: #666; font-size: 14px; }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 15px; margin-bottom: 20px; }}
        .stat-card {{ background: white; border-radius: 12px; padding: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }}
        .stat-label {{ color: #666; font-size: 13px; margin-bottom: 8px; text-transform: uppercase; }}
        .stat-value {{ font-size: 36px; font-weight: bold; color: #333; }}
        .stat-subtext {{ color: #10b981; font-size: 14px; margin-top: 5px; font-weight: 600; }}
        .users-section {{ background: white; border-radius: 12px; padding: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); margin-bottom: 20px; }}
        .section-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }}
        h2 {{ color: #333; font-size: 20px; }}
        .refresh-btn {{ background: #10b981; color: white; border: none; padding: 10px 20px; border-radius: 8px; font-weight: 600; cursor: pointer; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th {{ background: #f9fafb; padding: 12px; text-align: left; font-weight: 600; color: #333; border-bottom: 2px solid #e5e7eb; font-size: 13px; }}
        td {{ padding: 12px; border-bottom: 1px solid #e5e7eb; font-size: 14px; }}
        tr:hover {{ background: #f9fafb; }}
        .status-badge {{ display: inline-flex; align-items: center; gap: 5px; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 600; }}
        .status-active {{ background: #d1fae5; color: #065f46; }}
        .status-waiting {{ background: #fef3c7; color: #92400e; }}
        .status-inactive {{ background: #fee2e2; color: #991b1b; }}
        .status-setup_pending {{ background: #e5e7eb; color: #374151; }}
        .profit-positive {{ color: #10b981; font-weight: 600; }}
        .profit-negative {{ color: #ef4444; font-weight: 600; }}
        .errors-section {{ background: white; border-radius: 12px; padding: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }}
        .error-item {{ border-left: 4px solid #ef4444; background: #fef2f2; padding: 15px; margin-bottom: 12px; border-radius: 4px; }}
        .error-header {{ display: flex; justify-content: space-between; margin-bottom: 8px; }}
        .error-type {{ font-weight: 600; color: #991b1b; }}
        .error-timestamp {{ font-size: 12px; color: #666; }}
        .error-email {{ font-size: 12px; color: #666; margin-bottom: 5px; }}
        .error-message {{ color: #991b1b; font-size: 13px; }}
        .no-data {{ text-align: center; padding: 40px; color: #666; }}
        .api-key {{ font-family: 'Courier New', monospace; font-size: 12px; color: #666; }}
        .timestamp {{ font-size: 12px; color: #666; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🚀 $NIKEPIG's Massive Rocket - Admin Dashboard</h1>
            <p class="subtitle">Hosted Follower Agents | Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
        </div>
        
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">Total Signups</div>
                <div class="stat-value">{stats['total_users']}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Setup Completed</div>
                <div class="stat-value">{stats['setup_completed']}</div>
                <div class="stat-subtext">{stats['setup_rate']} completion</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Active Now</div>
                <div class="stat-value">{stats['active_now']}</div>
                <div class="stat-subtext">{stats['active_rate']} active</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Total Trades</div>
                <div class="stat-value">{stats['total_trades']}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Total Profit</div>
                <div class="stat-value" style="color: {profit_color}">${stats['total_profit']:.2f}</div>
                <div class="stat-subtext">${stats['avg_profit_per_user']:.2f} avg/user</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Setup Pending</div>
                <div class="stat-value">{stats['setup_pending']}</div>
            </div>
        </div>
        
        <div class="users-section">
            <div class="section-header">
                <h2>👥 All Users ({stats['total_users']})</h2>
                <button class="refresh-btn" onclick="location.reload()">🔄 Refresh</button>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Status</th>
                        <th>Email</th>
                        <th>API Key</th>
                        <th>Signed Up</th>
                        <th>Setup</th>
                        <th>Trades</th>
                        <th>Last Trade</th>
                        <th>Profit</th>
                    </tr>
                </thead>
                <tbody>{user_rows}</tbody>
            </table>
        </div>
        
        <div class="errors-section">
            <div class="section-header">
                <h2>⚠️ Recent Errors (24h)</h2>
            </div>
            {error_items}
        </div>
    </div>
    <script>setTimeout(() => location.reload(), 30000);</script>
</body>
</html>"""
