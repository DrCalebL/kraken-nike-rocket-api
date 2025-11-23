"""
Nike Rocket Follower System - Main API
=======================================
Updated main.py with hosted agents system + Admin Dashboard.
Includes automatic deposit/withdrawal detection via balance_checker.

FIXED VERSION with startup_delay_seconds=30 to prevent race condition.

Author: Nike Rocket Team
Updated: November 23, 2025 - COMPLETE VERSION
"""
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from sqlalchemy import create_engine
import os
import asyncio
import asyncpg

# Import follower system
from follower_models import init_db
from follower_endpoints import router as follower_router

# Import portfolio system
from portfolio_models import init_portfolio_db
from portfolio_api import router as portfolio_router

# Import balance checker for automatic deposit/withdrawal detection
# FIXED: Using the version with startup delay support
from balance_checker import BalanceCheckerScheduler

# Import admin dashboard
from admin_dashboard import (
    get_all_users_with_status,
    get_recent_errors,
    get_stats_summary,
    generate_admin_html,
    create_error_logs_table,
    ADMIN_PASSWORD
)

# Initialize FastAPI
app = FastAPI(
    title="Nike Rocket Follower API",
    description="Trading signal distribution and profit tracking",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Database setup
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if DATABASE_URL:
    engine = create_engine(DATABASE_URL)
    init_db(engine)
    init_portfolio_db(engine)
    print("✅ Database initialized")
else:
    print("⚠️ DATABASE_URL not set - database features disabled")

# Include routers
app.include_router(follower_router, tags=["follower"])
app.include_router(portfolio_router, tags=["portfolio"])

# Health check
@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "$NIKEPIG's Massive Rocket API",
        "version": "1.0.1",
        "endpoints": {
            "signup": "/signup",
            "login": "/login",
            "setup": "/setup",
            "dashboard": "/dashboard",
            "admin": "/admin?password=xxx",
            "reset_database": "/admin/reset-database?password=xxx",
            "broadcast": "/api/broadcast-signal",
            "latest_signal": "/api/latest-signal",
            "report_pnl": "/api/report-pnl",
            "register": "/api/users/register",
            "verify": "/api/users/verify",
            "stats": "/api/users/stats",
            "agent_status": "/api/agent-status",
            "setup_agent": "/api/setup-agent",
            "stop_agent": "/api/stop-agent",
            "portfolio_stats": "/api/portfolio/stats",
            "portfolio_trades": "/api/portfolio/trades",
            "portfolio_deposit": "/api/portfolio/deposit",
            "portfolio_withdraw": "/api/portfolio/withdraw",
            "pay": "/api/pay/{api_key}",
            "webhook": "/api/payments/webhook"
        },
        "user_links": {
            "new_users": "Visit /signup to create an account",
            "returning_users": "Visit /login to access your dashboard"
        }
    }

@app.get("/health")
async def health():
    return {"status": "healthy"}

# Admin Dashboard (NEW!)
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(password: str = ""):
    """
    Admin dashboard to monitor hosted follower agents
    
    Access: /admin?password=YOUR_ADMIN_PASSWORD
    
    Shows:
    - User signups
    - Setup completion rates
    - Active agents
    - Trading activity
    - Error logs
    """
    # Check password
    if password != ADMIN_PASSWORD:
        return HTMLResponse("""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <title>$NIKEPIG Admin Access</title>
                <style>
                    body {{
                        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                        display: flex;
                        justify-content: center;
                        align-items: center;
                        height: 100vh;
                        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                        margin: 0;
                    }}
                    .login-box {{
                        background: white;
                        padding: 40px;
                        border-radius: 12px;
                        box-shadow: 0 4px 12px rgba(0,0,0,0.2);
                        text-align: center;
                        min-width: 300px;
                    }}
                    h1 {{
                        color: #667eea;
                        margin-bottom: 10px;
                        font-size: 24px;
                    }}
                    .subtitle {{
                        color: #666;
                        font-size: 14px;
                        margin-bottom: 25px;
                    }}
                    input {{
                        padding: 12px;
                        border: 2px solid #e5e7eb;
                        border-radius: 8px;
                        width: 100%;
                        font-size: 14px;
                        box-sizing: border-box;
                    }}
                    input:focus {{
                        outline: none;
                        border-color: #667eea;
                    }}
                    button {{
                        padding: 12px 24px;
                        background: #667eea;
                        color: white;
                        border: none;
                        border-radius: 8px;
                        font-weight: 600;
                        cursor: pointer;
                        margin-top: 15px;
                        width: 100%;
                        font-size: 14px;
                    }}
                    button:hover {{
                        background: #5568d3;
                    }}
                    .error {{
                        color: #ef4444;
                        margin-top: 15px;
                        font-size: 14px;
                    }}
                </style>
            </head>
            <body>
                <div class="login-box">
                    <h1>🔒 $NIKEPIG Admin</h1>
                    <p class="subtitle">Hosted Follower Agents Dashboard</p>
                    <form method="GET">
                        <input 
                            type="password" 
                            name="password" 
                            placeholder="Enter admin password" 
                            required 
                            autofocus
                        >
                        <button type="submit">Access Dashboard</button>
                    </form>
                    """ + (f"""<p class="error">❌ Invalid password</p>""" if password else "") + """
                </div>
            </body>
            </html>
        """)
    
    # Ensure error_logs table exists
    try:
        create_error_logs_table()
    except Exception as e:
        print(f"Note: Error logs table setup - {e}")
    
    # Get dashboard data
    try:
        users = get_all_users_with_status()
        errors = get_recent_errors(hours=24)
        stats = get_stats_summary()
        
        # Generate and return HTML
        html = generate_admin_html(users, errors, stats)
        return HTMLResponse(html)
        
    except Exception as e:
        return HTMLResponse(f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Admin Dashboard Error</title>
                <style>
                    body {{
                        font-family: Arial, sans-serif;
                        padding: 40px;
                        background: #f5f5f5;
                    }}
                    .error-box {{
                        background: white;
                        padding: 30px;
                        border-radius: 12px;
                        border-left: 4px solid #ef4444;
                        max-width: 600px;
                        margin: 0 auto;
                    }}
                    h1 {{ color: #ef4444; }}
                    code {{
                        background: #f9fafb;
                        padding: 2px 6px;
                        border-radius: 4px;
                        font-family: monospace;
                    }}
                </style>
            </head>
            <body>
                <div class="error-box">
                    <h1>⚠️ Dashboard Error</h1>
                    <p><strong>Error:</strong> {str(e)}</p>
                    <p>Make sure <code>admin_dashboard.py</code> is in your repo and DATABASE_URL is set.</p>
                </div>
            </body>
            </html>
        """)

# Database Reset Endpoint (NEW!)
@app.post("/admin/reset-database")
async def reset_database(password: str = ""):
    """
    DANGER ZONE: Reset entire database
    
    Deletes all data from all tables while preserving structure.
    Access: POST /admin/reset-database?password=YOUR_ADMIN_PASSWORD
    
    Returns:
        JSON with status and deleted row counts
    """
    
    # Check password
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid password")
    
    try:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        
        # Tables to clear (in dependency order - children first, parents last)
        tables = [
            'trades',
            'portfolio_trades',
            'portfolio_withdrawals',
            'portfolio_deposits',
            'error_logs',
            'agent_logs',
            'signal_deliveries',
            'signals',
            'payments',
            'follower_users',
            'portfolio_users',
            'users',
            'system_stats'
        ]
        
        deleted_counts = {}
        
        # Delete all data from each table
        for table in tables:
            try:
                # Count rows before deletion
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                count_before = cur.fetchone()[0]
                
                # Delete all rows
                cur.execute(f"DELETE FROM {table}")
                
                deleted_counts[table] = {
                    'rows_deleted': count_before,
                    'status': 'success'
                }
                
                print(f"✅ Cleared {table}: {count_before} rows deleted")
                
            except Exception as e:
                deleted_counts[table] = {
                    'rows_deleted': 0,
                    'status': 'error',
                    'error': str(e)[:100]
                }
                print(f"⚠️ Error clearing {table}: {str(e)[:100]}")
        
        # Commit all deletions
        conn.commit()
        cur.close()
        conn.close()
        
        total_deleted = sum(
            t.get('rows_deleted', 0) 
            for t in deleted_counts.values() 
            if isinstance(t, dict)
        )
        
        print(f"🎉 Database reset complete! {total_deleted} total rows deleted")
        
        return {
            "status": "success",
            "message": f"🎉 Database reset complete! Deleted {total_deleted} rows",
            "deleted": deleted_counts,
            "tables_cleared": len([t for t in deleted_counts.values() if t.get('status') == 'success'])
        }
        
    except Exception as e:
        print(f"❌ Database reset failed: {str(e)}")
        return {
            "status": "error",
            "message": f"Database reset failed: {str(e)}",
            "error": str(e)
        }

# Serve static background images (NEW!)
@app.get("/static/backgrounds/{filename}")
async def get_background(filename: str):
    """Serve background images for performance cards"""
    filepath = f"backgrounds/{filename}"
    if os.path.exists(filepath):
        return FileResponse(filepath)
    else:
        raise HTTPException(status_code=404, detail="Background image not found")

# Signup page
@app.get("/signup", response_class=HTMLResponse)
async def signup_page():
    """Serve the signup HTML page"""
    try:
        with open("signup.html", "r") as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>Signup page not found</h1><p>Please contact support.</p>",
            status_code=404
        )

# Setup page (NEW!)
@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    """Setup page for configuring trading agent"""
    try:
        with open("setup.html", "r") as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>Setup page not found</h1><p>Please contact support.</p>",
            status_code=404
        )

# Login page for returning users (NEW!)
@app.get("/login", response_class=HTMLResponse)
@app.get("/access", response_class=HTMLResponse)
async def login_page():
    """Login page for returning users to access their dashboard"""
    try:
        with open("login.html", "r") as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse("""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Access Dashboard - $NIKEPIG's Massive Rocket</title>
                <style>
                    * { margin: 0; padding: 0; box-sizing: border-box; }
                    body {
                        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                        min-height: 100vh;
                        display: flex;
                        justify-content: center;
                        align-items: center;
                        padding: 20px;
                    }
                    .container {
                        background: white;
                        border-radius: 16px;
                        box-shadow: 0 8px 32px rgba(0,0,0,0.2);
                        max-width: 500px;
                        width: 100%;
                        overflow: hidden;
                    }
                    .header {
                        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                        padding: 40px 30px;
                        text-align: center;
                    }
                    .header h1 {
                        color: white;
                        font-size: 28px;
                        margin-bottom: 8px;
                    }
                    .header p {
                        color: rgba(255,255,255,0.9);
                        font-size: 14px;
                    }
                    .content {
                        padding: 40px 30px;
                    }
                    .welcome {
                        text-align: center;
                        margin-bottom: 30px;
                    }
                    .welcome h2 {
                        color: #374151;
                        font-size: 24px;
                        margin-bottom: 10px;
                    }
                    .welcome p {
                        color: #6b7280;
                        font-size: 14px;
                    }
                    .form-group {
                        margin-bottom: 20px;
                    }
                    label {
                        display: block;
                        color: #374151;
                        font-weight: 600;
                        margin-bottom: 8px;
                        font-size: 14px;
                    }
                    input {
                        width: 100%;
                        padding: 14px;
                        border: 2px solid #e5e7eb;
                        border-radius: 8px;
                        font-size: 14px;
                        font-family: 'Courier New', monospace;
                        transition: border-color 0.2s;
                    }
                    input:focus {
                        outline: none;
                        border-color: #667eea;
                    }
                    .button {
                        width: 100%;
                        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                        color: white;
                        border: none;
                        padding: 16px;
                        border-radius: 8px;
                        font-size: 16px;
                        font-weight: 600;
                        cursor: pointer;
                        transition: transform 0.2s, box-shadow 0.2s;
                    }
                    .button:hover {
                        transform: translateY(-2px);
                        box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
                    }
                    .help-box {
                        background: #f9fafb;
                        border-left: 4px solid #667eea;
                        padding: 15px;
                        border-radius: 8px;
                        margin-top: 20px;
                    }
                    .help-box p {
                        color: #6b7280;
                        font-size: 13px;
                        margin: 0 0 10px 0;
                    }
                    .help-box p:last-child {
                        margin: 0;
                    }
                    .new-user-link {
                        text-align: center;
                        margin-top: 20px;
                        padding-top: 20px;
                        border-top: 1px solid #e5e7eb;
                    }
                    .new-user-link a {
                        color: #667eea;
                        text-decoration: none;
                        font-weight: 600;
                    }
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="header">
                        <h1>🚀 $NIKEPIG's Massive Rocket</h1>
                        <p>Access Your Trading Dashboard</p>
                    </div>
                    
                    <div class="content">
                        <div class="welcome">
                            <h2>👋 Welcome Back!</h2>
                            <p>Enter your API key to access your dashboard</p>
                        </div>
                        
                        <form onsubmit="event.preventDefault(); window.location.href='/dashboard?key='+document.getElementById('apiKey').value">
                            <div class="form-group">
                                <label for="apiKey">Your API Key</label>
                                <input 
                                    type="text" 
                                    id="apiKey" 
                                    name="apiKey" 
                                    placeholder="nk_..." 
                                    required
                                    autocomplete="off"
                                >
                            </div>
                            
                            <button type="submit" class="button">
                                🔓 Access Dashboard
                            </button>
                        </form>
                        
                        <div class="help-box">
                            <p><strong>💡 Where to find your API key:</strong></p>
                            <p>• Check the welcome email sent to your inbox</p>
                            <p>• Your API key starts with "nk_"</p>
                            <p>• If you lost it, contact support to recover your account</p>
                        </div>
                        
                        <div class="new-user-link">
                            <p style="color: #6b7280; font-size: 14px; margin-bottom: 8px;">
                                Don't have an account yet?
                            </p>
                            <a href="/signup">🚀 Sign Up Now - It's Free!</a>
                        </div>
                    </div>
                </div>
            </body>
            </html>
        """, status_code=200)

# Portfolio Dashboard (USER-FRIENDLY VERSION) - COMPLETE HTML!
@app.get("/dashboard", response_class=HTMLResponse)
async def portfolio_dashboard(request: Request):
    """Portfolio tracking dashboard with API key input"""
    
    # Get API key from query parameter (optional)
    api_key = request.query_params.get('key', '')
    
    html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>$NIKEPIG's Massive Rocket - Portfolio Dashboard</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&display=swap" rel="stylesheet">
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }}
        
        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        
        /* API Key Login Screen */
        .login-screen {{
            max-width: 500px;
            margin: 100px auto;
            background: white;
            padding: 40px;
            border-radius: 16px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.2);
        }}
        
        .login-screen h1 {{
            color: #667eea;
            text-align: center;
            margin-bottom: 10px;
            font-size: 32px;
        }}
        
        .login-screen p {{
            text-align: center;
            color: #6b7280;
            margin-bottom: 30px;
        }}
        
        .input-group {{
            margin-bottom: 20px;
        }}
        
        .input-group label {{
            display: block;
            margin-bottom: 8px;
            color: #374151;
            font-weight: 600;
        }}
        
        .input-group input {{
            width: 100%;
            padding: 12px;
            border: 2px solid #e5e7eb;
            border-radius: 8px;
            font-size: 16px;
        }}
        
        .input-group input:focus {{
            outline: none;
            border-color: #667eea;
        }}
        
        .btn {{
            width: 100%;
            padding: 14px;
            background: #667eea;
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.2s;
        }}
        
        .btn:hover {{
            background: #5568d3;
        }}
        
        .btn:disabled {{
            background: #9ca3af;
            cursor: not-allowed;
        }}
        
        /* Setup Wizard */
        .setup-wizard {{
            max-width: 600px;
            margin: 50px auto;
            background: white;
            padding: 40px;
            border-radius: 16px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.2);
        }}
        
        .setup-wizard h2 {{
            color: #667eea;
            margin-bottom: 10px;
        }}
        
        .setup-wizard p {{
            color: #6b7280;
            margin-bottom: 20px;
        }}
        
        /* Dashboard */
        .hero {{
            text-align: center;
            color: white;
            padding: 40px 20px;
            margin-bottom: 40px;
        }}
        
        .hero h1 {{
            font-size: 48px;
            font-weight: 700;
            margin-bottom: 20px;
        }}
        
        .period-selector {{
            margin: 20px 0;
        }}
        
        .period-selector select {{
            padding: 12px 24px;
            font-size: 16px;
            border-radius: 25px;
            border: 2px solid rgba(255,255,255,0.3);
            background: rgba(255,255,255,0.1);
            color: white;
            cursor: pointer;
            font-weight: 600;
        }}
        
        .period-selector option {{
            background: #764ba2;
            color: white;
        }}
        
        .hero-profit {{
            font-size: 72px;
            font-weight: 800;
            margin: 20px 0;
            text-shadow: 0 4px 8px rgba(0,0,0,0.2);
        }}
        
        .hero-label {{
            font-size: 24px;
            opacity: 0.9;
        }}
        
        .hero-subtext {{
            font-size: 16px;
            opacity: 0.7;
            margin-top: 10px;
        }}
        
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 40px;
        }}
        
        .stat-card {{
            background: white;
            border-radius: 16px;
            padding: 24px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
        }}
        
        .stat-label {{
            font-size: 14px;
            color: #6b7280;
            margin-bottom: 8px;
        }}
        
        .stat-value {{
            font-size: 32px;
            font-weight: 700;
            color: #1f2937;
        }}
        
        .stat-detail {{
            font-size: 12px;
            color: #9ca3af;
            margin-top: 4px;
        }}
        
        .error {{
            background: #fee2e2;
            color: #991b1b;
            padding: 20px;
            border-radius: 12px;
            margin: 20px 0;
            text-align: center;
        }}
        
        .success {{
            background: #d1fae5;
            color: #065f46;
            padding: 20px;
            border-radius: 12px;
            margin: 20px 0;
            text-align: center;
        }}
        
        .logout-btn {{
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 10px 20px;
            background: rgba(255,255,255,0.2);
            color: white;
            border: 2px solid white;
            border-radius: 8px;
            cursor: pointer;
            font-weight: 600;
        }}
        
        .logout-btn:hover {{
            background: rgba(255,255,255,0.3);
        }}
        
        /* Agent Status Styles */
        .agent-status-container {{
            max-width: 800px;
            margin: 0 auto 24px auto;
        }}
        
        .agent-status {{
            padding: 16px 20px;
            border-radius: 12px;
            font-size: 15px;
            text-align: center;
            transition: all 0.3s ease;
            border: 2px solid;
        }}
        
        .agent-status strong {{
            font-weight: 600;
        }}
        
        .status-active {{
            background: #d1fae5;
            border-color: #10b981;
            color: #065f46;
        }}
        
        .status-configuring {{
            background: #dbeafe;
            border-color: #3b82f6;
            color: #1e40af;
            animation: pulse 2s ease-in-out infinite;
        }}
        
        .status-ready {{
            background: #fef3c7;
            border-color: #f59e0b;
            color: #92400e;
        }}
        
        .status-error {{
            background: #fee2e2;
            border-color: #ef4444;
            color: #991b1b;
        }}
        
        .status-unknown {{
            background: #f3f4f6;
            border-color: #9ca3af;
            color: #4b5563;
        }}
        
        @keyframes pulse {{
            0%, 100% {{
                opacity: 1;
            }}
            50% {{
                opacity: 0.7;
            }}
        }}
        
        /* Info message styling */
        .info {{
            padding: 16px;
            border-radius: 8px;
            margin-bottom: 20px;
            background: #dbeafe;
            color: #1e40af;
            border: 1px solid #93c5fd;
            display: block;
        }}
        
        .info strong {{
            font-weight: 600;
        }}
        
        .info small {{
            font-size: 13px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Login Screen -->
        <div id="login-screen" class="login-screen">
            <h1>🚀 $NIKEPIG's Massive Rocket</h1>
            <p>Portfolio Performance Tracker</p>
            
            <div class="input-group">
                <label for="api-key-input">Enter Your API Key:</label>
                <input 
                    type="text" 
                    id="api-key-input" 
                    placeholder="nk_..." 
                    value="{api_key}"
                >
            </div>
            
            <button class="btn" onclick="login()">View Dashboard</button>
            
            <div id="login-error" style="display: none;"></div>
        </div>
        
        <!-- Setup Wizard -->
        <div id="setup-wizard" class="setup-wizard" style="display: none;">
            <h2>🎯 Welcome to $NIKEPIG's Massive Rocket!</h2>
            <p>We'll automatically detect your Kraken balance and start tracking your performance!</p>
            
            <div style="background: #f0f9ff; border-left: 4px solid #3b82f6; padding: 15px; margin: 20px 0; border-radius: 4px;">
                <div style="color: #1e40af; font-weight: 600; margin-bottom: 5px;">📊 Auto-Detection</div>
                <div style="color: #1e40af; font-size: 14px;">
                    We'll query your current Kraken balance and use it as your starting capital. 
                    Make sure your trading agent is set up first!
                </div>
            </div>
            
            <button class="btn" onclick="initializePortfolio()">Start Tracking</button>
            
            <div id="setup-message" style="display: none;"></div>
        </div>
        
        <!-- Dashboard -->
        <div id="dashboard" style="display: none;">
            <button class="logout-btn" onclick="logout()">Change API Key</button>
            
            <!-- Agent Status Display -->
            <div class="agent-status-container">
                <div id="agent-status-display" class="agent-status status-unknown">
                    ⏳ Checking agent status...
                </div>
            </div>
            
            <!-- Portfolio Overview Section (NEW!) -->
            <div class="portfolio-overview" style="
                background: white;
                border-radius: 12px;
                padding: 30px;
                margin-bottom: 30px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            ">
                <h2 style="margin: 0 0 20px 0; color: #667eea; font-size: 24px;">
                    💰 Portfolio Overview
                </h2>
                
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 25px;">
                    <div class="overview-card">
                        <div style="color: #6b7280; font-size: 14px; margin-bottom: 5px;">Current Value</div>
                        <div id="current-value" style="font-size: 32px; font-weight: bold; color: #10b981;">$0</div>
                    </div>
                    
                    <div class="overview-card">
                        <div style="color: #6b7280; font-size: 14px; margin-bottom: 5px;">Initial Capital</div>
                        <div id="initial-capital-display" style="font-size: 28px; font-weight: 600; color: #374151;">$0</div>
                    </div>
                    
                    <div class="overview-card">
                        <div style="color: #6b7280; font-size: 14px; margin-bottom: 5px;">Net Deposits</div>
                        <div id="net-deposits" style="font-size: 28px; font-weight: 600; color: #3b82f6;">$0</div>
                    </div>
                    
                    <div class="overview-card">
                        <div style="color: #6b7280; font-size: 14px; margin-bottom: 5px;">Total Profit</div>
                        <div id="total-profit-overview" style="font-size: 28px; font-weight: 600; color: #10b981;">$0</div>
                    </div>
                </div>
                
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; padding: 15px; background: #f9fafb; border-radius: 8px;">
                    <div>
                        <div style="font-size: 13px; color: #6b7280;">Total Deposits</div>
                        <div id="total-deposits" style="font-size: 18px; font-weight: 600; color: #10b981;">+$0</div>
                    </div>
                    <div>
                        <div style="font-size: 13px; color: #6b7280;">Total Withdrawals</div>
                        <div id="total-withdrawals" style="font-size: 18px; font-weight: 600; color: #ef4444;">-$0</div>
                    </div>
                    <div>
                        <div style="font-size: 13px; color: #6b7280;">Total Capital</div>
                        <div id="total-capital" style="font-size: 18px; font-weight: 600; color: #374151;">$0</div>
                    </div>
                    <div>
                        <div style="font-size: 13px; color: #6b7280;">Last Balance Check</div>
                        <div id="last-check" style="font-size: 14px; color: #6b7280;">—</div>
                    </div>
                </div>
            </div>
            
            <!-- Agent Control Section (NEW!) -->
            <div class="agent-control" style="
                background: white;
                border-radius: 12px;
                padding: 30px;
                margin-bottom: 30px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            ">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 20px;">
                    <div>
                        <h2 style="margin: 0 0 10px 0; color: #667eea; font-size: 24px;">
                            🤖 Trading Agent Control
                        </h2>
                        <div style="display: flex; align-items: center; gap: 10px;">
                            <div style="font-size: 14px; color: #6b7280;">Status:</div>
                            <div id="agent-status-badge" style="
                                padding: 4px 12px;
                                border-radius: 12px;
                                font-size: 13px;
                                font-weight: 600;
                                background: #fee2e2;
                                color: #991b1b;
                            ">
                                Checking...
                            </div>
                        </div>
                        <div id="agent-details" style="font-size: 13px; color: #6b7280; margin-top: 5px;">
                            <!-- Agent details will load here -->
                        </div>
                    </div>
                    
                    <div style="display: flex; gap: 10px;">
                        <button id="start-agent-btn" onclick="startAgent()" style="
                            padding: 12px 24px;
                            background: #10b981;
                            color: white;
                            border: none;
                            border-radius: 8px;
                            font-weight: 600;
                            cursor: pointer;
                            transition: all 0.2s;
                            display: none;
                        ">
                            ▶️ Start Agent
                        </button>
                        
                        <button id="stop-agent-btn" onclick="stopAgent()" style="
                            padding: 12px 24px;
                            background: #ef4444;
                            color: white;
                            border: none;
                            border-radius: 8px;
                            font-weight: 600;
                            cursor: pointer;
                            transition: all 0.2s;
                            display: none;
                        ">
                            ⏸️ Stop Agent
                        </button>
                        
                        <button onclick="checkAgentStatus()" style="
                            padding: 12px 20px;
                            background: #667eea;
                            color: white;
                            border: none;
                            border-radius: 8px;
                            font-weight: 600;
                            cursor: pointer;
                            transition: all 0.2s;
                        ">
                            🔄 Refresh
                        </button>
                    </div>
                </div>
                
                <div id="agent-message" style="
                    margin-top: 15px;
                    padding: 12px;
                    border-radius: 6px;
                    display: none;
                "></div>
            </div>
            
            <div class="hero">
                <h1>🚀 $NIKEPIG'S MASSIVE ROCKET PERFORMANCE</h1>
                
                <div class="period-selector">
                    <select id="period-selector" onchange="changePeriod()">
                        <option value="7d">Last 7 Days</option>
                        <option value="30d" selected>Last 30 Days</option>
                        <option value="90d">Last 90 Days</option>
                        <option value="all">All-Time</option>
                    </select>
                </div>
                
                <div class="hero-profit" id="total-profit">$0</div>
                <div class="hero-label" id="profit-label">Total Profit</div>
                <div class="hero-subtext" id="time-tracking">Trading since...</div>
                
                <!-- Social Sharing Buttons (NEW!) -->
                <div style="margin: 30px 0;">
                    <div style="display: flex; gap: 15px; justify-content: center; flex-wrap: wrap; margin-bottom: 20px;">
                        <button onclick="showBackgroundSelectorForTwitter()" style="
                            padding: 12px 24px;
                            background: #1DA1F2;
                            color: white;
                            border: none;
                            border-radius: 8px;
                            font-weight: 600;
                            cursor: pointer;
                            font-size: 14px;
                            display: flex;
                            align-items: center;
                            gap: 8px;
                            box-shadow: 0 4px 12px rgba(29, 161, 242, 0.3);
                        ">
                            <span>𝕏</span> Share to X (+ Download Image)
                        </button>
                        
                        <button onclick="showBackgroundSelectorForDownload()" style="
                            padding: 12px 24px;
                            background: #8b5cf6;
                            color: white;
                            border: none;
                            border-radius: 8px;
                            font-weight: 600;
                            cursor: pointer;
                            font-size: 14px;
                            display: flex;
                            align-items: center;
                            gap: 8px;
                            box-shadow: 0 4px 12px rgba(139, 92, 246, 0.3);
                        ">
                            <span>📸</span> Download Image
                        </button>
                    </div>
                    
                    <!-- Background Selector (Hidden by default) -->
                    <div id="background-selector" style="
                        display: none;
                        background: rgba(255,255,255,0.95);
                        padding: 20px;
                        border-radius: 12px;
                        max-width: 600px;
                        margin: 0 auto;
                        box-shadow: 0 8px 24px rgba(0,0,0,0.2);
                    ">
                        <h3 style="color: #667eea; margin: 0 0 15px 0; font-size: 18px;">Choose Your Background</h3>
                        <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 15px; margin-bottom: 15px;">
                            <div onclick="selectBackground('charles')" class="bg-option" data-bg="charles" style="
                                height: 150px;
                                border-radius: 8px;
                                cursor: pointer;
                                background-image: url('https://raw.githubusercontent.com/DrCalebL/nike-rocket-api/main/static/bg-charles.png');
                                background-size: cover;
                                background-position: center;
                                border: 3px solid #667eea;
                                transition: all 0.2s;
                                position: relative;
                                overflow: hidden;
                            ">
                                <div style="position: absolute; bottom: 0; left: 0; right: 0; background: rgba(0,0,0,0.7); padding: 8px; text-align: center; color: white; font-weight: 600;">
                                    📚 Charles & Nike
                                </div>
                            </div>
                            
                            <div onclick="selectBackground('casino')" class="bg-option" data-bg="casino" style="
                                height: 150px;
                                border-radius: 8px;
                                cursor: pointer;
                                background-image: url('https://raw.githubusercontent.com/DrCalebL/nike-rocket-api/main/static/bg-casino.png');
                                background-size: cover;
                                background-position: center;
                                border: 3px solid transparent;
                                transition: all 0.2s;
                                position: relative;
                                overflow: hidden;
                            ">
                                <div style="position: absolute; bottom: 0; left: 0; right: 0; background: rgba(0,0,0,0.7); padding: 8px; text-align: center; color: white; font-weight: 600;">
                                    🎰 Casino Wins
                                </div>
                            </div>
                            
                            <div onclick="selectBackground('gaming')" class="bg-option" data-bg="gaming" style="
                                height: 150px;
                                border-radius: 8px;
                                cursor: pointer;
                                background-image: url('https://raw.githubusercontent.com/DrCalebL/nike-rocket-api/main/static/bg-gaming.png');
                                background-size: cover;
                                background-position: center;
                                border: 3px solid transparent;
                                transition: all 0.2s;
                                position: relative;
                                overflow: hidden;
                            ">
                                <div style="position: absolute; bottom: 0; left: 0; right: 0; background: rgba(0,0,0,0.7); padding: 8px; text-align: center; color: white; font-weight: 600;">
                                    🎮 Couch Trading
                                </div>
                            </div>
                            
                            <div onclick="selectBackground('money')" class="bg-option" data-bg="money" style="
                                height: 150px;
                                border-radius: 8px;
                                cursor: pointer;
                                background-image: url('https://raw.githubusercontent.com/DrCalebL/nike-rocket-api/main/static/bg-money.png');
                                background-size: cover;
                                background-position: center;
                                border: 3px solid transparent;
                                transition: all 0.2s;
                                position: relative;
                                overflow: hidden;
                            ">
                                <div style="position: absolute; bottom: 0; left: 0; right: 0; background: rgba(0,0,0,0.7); padding: 8px; text-align: center; color: white; font-weight: 600;">
                                    💰 Money Rain
                                </div>
                            </div>
                        </div>
                        <button id="selector-action-btn" onclick="handleSelectorAction()" style="
                            width: 100%;
                            padding: 12px;
                            background: #10b981;
                            color: white;
                            border: none;
                            border-radius: 8px;
                            font-weight: 600;
                            cursor: pointer;
                            font-size: 14px;
                        ">
                            ✅ Download Image
                        </button>
                    </div>
                </div>
            </div>
            
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-label">ROI on Initial Capital</div>
                    <div class="stat-value" id="roi-initial">0%</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-label">ROI on Total Capital</div>
                    <div class="stat-value" id="roi-total">0%</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-label">Profit Factor</div>
                    <div class="stat-value" id="profit-factor">0x</div>
                    <div class="stat-detail" id="pf-detail">Wins / Losses</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-label">Best Trade</div>
                    <div class="stat-value" id="best-trade">$0</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-label">Avg Monthly Profit</div>
                    <div class="stat-value" id="monthly-avg">$0</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-label">Total Trades</div>
                    <div class="stat-value" id="total-trades">0</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-label">Max Drawdown</div>
                    <div class="stat-value" id="max-dd">0%</div>
                    <div class="stat-detail" id="dd-recovery">Recovery</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-label">Sharpe Ratio</div>
                    <div class="stat-value" id="sharpe">0.0</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-label">Days Active</div>
                    <div class="stat-value" id="days-active">0</div>
                </div>
            </div>
            
            <!-- Transaction History Section (NEW!) -->
            <div class="transaction-history" style="
                background: white;
                border-radius: 12px;
                padding: 30px;
                margin-top: 30px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            ">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
                    <h2 style="margin: 0; color: #667eea; font-size: 24px;">
                        📜 Transaction History
                    </h2>
                    <button onclick="loadTransactionHistory()" style="
                        background: #667eea;
                        color: white;
                        border: none;
                        padding: 8px 16px;
                        border-radius: 6px;
                        cursor: pointer;
                        font-size: 14px;
                    ">
                        🔄 Refresh
                    </button>
                </div>
                
                <div id="transaction-list" style="max-height: 400px; overflow-y: auto;">
                    <!-- Transactions will be loaded here -->
                    <div style="text-align: center; padding: 40px; color: #9ca3af;">
                        Loading transactions...
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    // ═══════════════════════════════════════════════════════════════════════
// COMPLETE JAVASCRIPT SECTION FOR main.py DASHBOARD - WITH ALL UX IMPROVEMENTS
// ═══════════════════════════════════════════════════════════════════════
// Replace the entire <script> section in your dashboard (lines 1334-2139) with this
// ═══════════════════════════════════════════════════════════════════════

<script>
    let currentApiKey = '{api_key}';
    let currentPeriod = '30d';
    
    // On page load
    if (currentApiKey) {{
        document.getElementById('api-key-input').value = currentApiKey;
        login();
    }}
    
    // ═══════════════════════════════════════════════════════════════
    // NEW: Agent Status Functions
    // ═══════════════════════════════════════════════════════════════
    
    async function checkAgentStatusAPI() {{
        try {{
            const response = await fetch(`/api/agent-status?key=${{currentApiKey}}`, {{
                headers: {{'X-API-Key': currentApiKey}}
            }});
            
            const data = await response.json();
            return data;
            
        }} catch (error) {{
            console.error('Error checking agent status:', error);
            return {{ status: 'error', message: error.message }};
        }}
    }}
    
    async function displayAgentStatus() {{
        try {{
            const statusData = await checkAgentStatusAPI();
            
            const statusElement = document.getElementById('agent-status-display');
            if (!statusElement) return;
            
            let statusHTML = '';
            let statusClass = '';
            
            if (statusData.status === 'active') {{
                statusHTML = '🟢 <strong>Agent Active</strong> - Following signals';
                statusClass = 'status-active';
            }} else if (statusData.status === 'configuring') {{
                statusHTML = `⏳ <strong>Configuring</strong> - Ready in ${{statusData.ready_in_minutes}} min (${{statusData.setup_complete_percentage}}%)`;
                statusClass = 'status-configuring';
            }} else if (statusData.status === 'ready') {{
                statusHTML = '🟡 <strong>Ready</strong> - Agent configured';
                statusClass = 'status-ready';
            }} else if (statusData.status === 'not_configured') {{
                statusHTML = '🔴 <strong>Not Configured</strong> - <a href="/setup?key=' + currentApiKey + '" style="color: #dc2626;">Complete setup</a>';
                statusClass = 'status-error';
            }} else {{
                statusHTML = '⚠️ <strong>Unknown</strong> - ' + statusData.message;
                statusClass = 'status-unknown';
            }}
            
            statusElement.innerHTML = statusHTML;
            statusElement.className = 'agent-status ' + statusClass;
            
        }} catch (error) {{
            console.error('Error displaying agent status:', error);
        }}
    }}
    
    let agentStatusInterval = null;
    
    function startAgentStatusMonitoring() {{
        if (agentStatusInterval) {{
            clearInterval(agentStatusInterval);
        }}
        
        displayAgentStatus();
        
        agentStatusInterval = setInterval(() => {{
            displayAgentStatus();
        }}, 30000); // Update every 30 seconds
    }}
    
    function stopAgentStatusMonitoring() {{
        if (agentStatusInterval) {{
            clearInterval(agentStatusInterval);
            agentStatusInterval = null;
        }}
    }}
    
    // ═══════════════════════════════════════════════════════════════
    // Login and Authentication
    // ═══════════════════════════════════════════════════════════════
    
    function login() {{
        const apiKey = document.getElementById('api-key-input').value.trim();
        
        if (!apiKey) {{
            showError('login-error', 'Please enter your API key');
            return;
        }}
        
        if (!apiKey.startsWith('nk_')) {{
            showError('login-error', 'Invalid API key format. Should start with "nk_"');
            return;
        }}
        
        currentApiKey = apiKey;
        document.getElementById('login-error').style.display = 'none';
        
        checkPortfolioStatus();
    }}
    
    function logout() {{
        stopAgentStatusMonitoring();
        document.getElementById('dashboard').style.display = 'none';
        document.getElementById('login-screen').style.display = 'block';
        document.getElementById('setup-wizard').style.display = 'none';
        currentApiKey = '';
        document.getElementById('api-key-input').value = '';
    }}
    
    // ═══════════════════════════════════════════════════════════════
    // Portfolio Status Check (UPDATED WITH AGENT STATUS)
    // ═══════════════════════════════════════════════════════════════
    
    async function checkPortfolioStatus() {{
        try {{
            // STEP 1: Check agent status first
            const statusData = await checkAgentStatusAPI();
            
            if (statusData.status === 'configuring') {{
                showSetupWizard();
                return;
            }}
            
            if (statusData.status === 'not_configured') {{
                showSetupWizard();
                return;
            }}
            
            // STEP 2: Check portfolio status
            const response = await fetch(`/api/portfolio/balance-summary?key=${{currentApiKey}}`, {{
                headers: {{'X-API-Key': currentApiKey}}
            }});
            
            if (response.status === 401) {{
                showError('login-error', 'Invalid API key. Please check and try again.');
                return;
            }}
            
            if (!response.ok) {{
                console.error('Portfolio stats error:', response.status);
                showSetupWizard();
                return;
            }}
            
            const data = await response.json();
            
            if (data.status === 'success' || data.total_profit !== undefined) {{
                showDashboard(data);
                await loadBalanceSummary();
                await loadTransactionHistory();
                await displayAgentStatus();
            }} else if (data.status === 'not_initialized') {{
                showSetupWizard();
            }} else {{
                showSetupWizard();
            }}
            
        }} catch (error) {{
            console.error('Error:', error);
            showSetupWizard();
        }}
    }}
    
    // ═══════════════════════════════════════════════════════════════
    // Setup Wizard (UPDATED WITH CONFIGURING STATE)
    // ═══════════════════════════════════════════════════════════════
    
    async function showSetupWizard() {{
        const statusData = await checkAgentStatusAPI();
        
        document.getElementById('login-screen').style.display = 'none';
        document.getElementById('setup-wizard').style.display = 'block';
        document.getElementById('dashboard').style.display = 'none';
        
        const setupMessage = document.getElementById('setup-message');
        const startButton = document.querySelector('.btn');
        
        if (statusData.status === 'configuring') {{
            // Agent still being set up - show wait message
            if (setupMessage) {{
                setupMessage.innerHTML = `
                    ⏳ <strong>Your trading agent is being configured.</strong>
                    <br><br>
                    Please wait approximately <strong>${{statusData.ready_in_minutes}} minute(s)</strong> before initializing your portfolio.
                    <br><br>
                    <div style="background: #e0e7ff; padding: 10px; border-radius: 8px; margin-top: 12px;">
                        <small>Setup Progress: ${{statusData.setup_complete_percentage}}%</small>
                        <div style="background: #cbd5e1; height: 8px; border-radius: 4px; margin-top: 6px; overflow: hidden;">
                            <div style="background: #667eea; height: 100%; width: ${{statusData.setup_complete_percentage}}%; transition: width 0.5s;"></div>
                        </div>
                    </div>
                    <br>
                    <small style="color: #6b7280;">
                        <a href="javascript:location.reload()" style="color: #667eea; text-decoration: underline;">
                            Click here to refresh and check status
                        </a>
                    </small>
                `;
                setupMessage.className = 'info';
                setupMessage.style.display = 'block';
            }}
            
            if (startButton) {{
                startButton.disabled = true;
                startButton.textContent = `⏳ Agent Configuring (${{statusData.ready_in_minutes}} min remaining)`;
                startButton.style.cursor = 'not-allowed';
                startButton.style.background = '#9ca3af';
            }}
            
            // Auto-refresh after the remaining time + 10 seconds
            const refreshTime = (statusData.ready_in_minutes * 60 + 10) * 1000;
            setTimeout(() => {{
                location.reload();
            }}, refreshTime);
            
        }} else {{
            // Agent ready or not configured - enable button
            if (setupMessage) {{
                setupMessage.innerHTML = '';
                setupMessage.style.display = 'none';
            }}
            
            if (startButton) {{
                startButton.disabled = false;
                startButton.textContent = '🚀 Start Tracking';
                startButton.style.cursor = 'pointer';
                startButton.style.background = '#667eea';
            }}
        }}
    }}
    
    // ═══════════════════════════════════════════════════════════════
    // Portfolio Initialization (UPDATED WITH AGENT CHECK)
    // ═══════════════════════════════════════════════════════════════
    
    async function initializePortfolio() {{
        try {{
            // STEP 1: Check if agent is ready
            showSuccess('setup-message', '🔍 Checking agent status...');
            
            const statusData = await checkAgentStatusAPI();
            
            if (statusData.status === 'configuring') {{
                showError('setup-message', 
                    `⚠️ Your trading agent is not ready yet. ` +
                    `<br><br>Please wait <strong>${{statusData.ready_in_minutes}} more minute(s)</strong> and ` +
                    `<a href="javascript:location.reload()" style="color: #ffffff; text-decoration: underline;">refresh this page</a>.`);
                return;
            }}
            
            if (statusData.status === 'not_configured') {{
                showError('setup-message', 
                    'Please set up your trading agent first.<br><br>' +
                    '<a href="/setup?key=' + currentApiKey + '" ' +
                    'style="color: #ffffff; text-decoration: underline; font-weight: bold;">' +
                    '→ Go to Agent Setup</a>');
                return;
            }}
            
            if (statusData.status === 'error') {{
                showError('setup-message', 
                    '⚠️ Error checking agent status. Please try again or contact support.');
                return;
            }}
            
            // STEP 2: Proceed with portfolio initialization
            showSuccess('setup-message', '🔍 Detecting your Kraken balance...');
            
            const response = await fetch('/api/portfolio/initialize', {{
                method: 'POST',
                headers: {{
                    'X-API-Key': currentApiKey,
                    'Content-Type': 'application/json'
                }},
                body: JSON.stringify({{}})
            }});
            
            const data = await response.json();
            
            if (data.status === 'success') {{
                showSuccess('setup-message', 
                    `✅ Portfolio initialized with $${{data.initial_capital.toLocaleString()}} detected from your Kraken account!`);
                setTimeout(() => checkPortfolioStatus(), 2000);
            }} else if (data.status === 'already_initialized') {{
                showSuccess('setup-message', 'Portfolio already initialized! Loading dashboard...');
                setTimeout(() => checkPortfolioStatus(), 1000);
            }} else if (data.status === 'error') {{
                if (data.message.includes('set up your trading agent')) {{
                    showError('setup-message', 
                        data.message + '<br><br>' +
                        '<a href="/setup?key=' + currentApiKey + '" ' +
                        'style="color: #ffffff; text-decoration: underline; font-weight: bold;">' +
                        '→ Go to Agent Setup</a>');
                }} else {{
                    showError('setup-message', data.message);
                }}
            }} else {{
                showError('setup-message', data.message || 'Failed to initialize portfolio');
            }}
            
        }} catch (error) {{
            showError('setup-message', 'Error initializing portfolio: ' + error.message);
        }}
    }}
    
    // ═══════════════════════════════════════════════════════════════
    // Dashboard Display (UPDATED TO START MONITORING)
    // ═══════════════════════════════════════════════════════════════
    
    function showDashboard(stats) {{
        document.getElementById('login-screen').style.display = 'none';
        document.getElementById('setup-wizard').style.display = 'none';
        document.getElementById('dashboard').style.display = 'block';
        
        // Start monitoring agent status
        startAgentStatusMonitoring();
    }}
    
    // ═══════════════════════════════════════════════════════════════
    // Balance Summary
    // ═══════════════════════════════════════════════════════════════
    
    async function loadBalanceSummary() {{
        try {{
            const response = await fetch(`/api/portfolio/balance-summary?key=${{currentApiKey}}`);
            const data = await response.json();
            
            if (data.status === 'success') {{
                document.getElementById('initial-capital-display').textContent = `$${{data.initial_capital.toLocaleString()}}`;
                document.getElementById('current-value').textContent = `$${{data.current_value.toLocaleString()}}`;
                
                const netDeposits = data.total_deposits - data.total_withdrawals;
                document.getElementById('net-deposits').textContent = `$${{netDeposits.toLocaleString()}}`;
                document.getElementById('net-deposits').style.color = netDeposits >= 0 ? '#10b981' : '#ef4444';
                
                const totalProfit = data.current_value - data.initial_capital - netDeposits;
                document.getElementById('total-profit-overview').textContent = `$${{totalProfit.toLocaleString()}}`;
                document.getElementById('total-profit-overview').style.color = totalProfit >= 0 ? '#10b981' : '#ef4444';
                
                document.getElementById('total-deposits').textContent = `+$${{data.total_deposits.toLocaleString()}}`;
                document.getElementById('total-withdrawals').textContent = `-$${{data.total_withdrawals.toLocaleString()}}`;
            }}
        }} catch (error) {{
            console.error('Error loading balance summary:', error);
        }}
    }}
    
    // ═══════════════════════════════════════════════════════════════
    // Transaction History
    // ═══════════════════════════════════════════════════════════════
    
    async function loadTransactionHistory() {{
        try {{
            const response = await fetch(`/api/portfolio/transactions?key=${{currentApiKey}}&limit=50`);
            const data = await response.json();
            
            const container = document.getElementById('transaction-list');
            
            if (data.status === 'success' && data.transactions && data.transactions.length > 0) {{
                let html = '<div style="display: grid; gap: 12px;">';
                
                data.transactions.forEach(tx => {{
                    const date = new Date(tx.detected_at).toLocaleString();
                    const typeClass = tx.transaction_type === 'deposit' ? 'deposit' : 'withdrawal';
                    const typeIcon = tx.transaction_type === 'deposit' ? '⬆️' : '⬇️';
                    const amountColor = tx.transaction_type === 'deposit' ? '#10b981' : '#ef4444';
                    const amountPrefix = tx.transaction_type === 'deposit' ? '+' : '-';
                    
                    html += `
                        <div style="
                            background: white;
                            border: 1px solid #e5e7eb;
                            border-radius: 8px;
                            padding: 16px;
                            display: grid;
                            grid-template-columns: auto 1fr auto;
                            gap: 12px;
                            align-items: center;
                        ">
                            <div style="font-size: 24px;">${{typeIcon}}</div>
                            <div>
                                <div style="font-weight: 600; color: #1f2937; margin-bottom: 4px;">
                                    ${{tx.transaction_type.charAt(0).toUpperCase() + tx.transaction_type.slice(1)}}
                                </div>
                                <div style="font-size: 13px; color: #6b7280;">
                                    ${{date}}
                                </div>
                                ${{tx.notes ? `<div style="font-size: 12px; color: #9ca3af; margin-top: 4px;">${{tx.notes}}</div>` : ''}}
                            </div>
                            <div style="
                                font-size: 20px;
                                font-weight: 700;
                                color: ${{amountColor}};
                            ">
                                ${{amountPrefix}}$${{tx.amount.toLocaleString()}}
                            </div>
                        </div>
                    `;
                }});
                
                html += '</div>';
                container.innerHTML = html;
            }} else {{
                container.innerHTML = `
                    <div style="text-align: center; padding: 40px; color: #9ca3af;">
                        <div style="font-size: 48px; margin-bottom: 16px;">📊</div>
                        <div style="font-size: 16px; font-weight: 600; color: #6b7280;">No transactions yet</div>
                        <div style="font-size: 14px; margin-top: 8px;">Deposits and withdrawals will appear here</div>
                    </div>
                `;
            }}
        }} catch (error) {{
            console.error('Error loading transactions:', error);
            document.getElementById('transaction-list').innerHTML = `
                <div style="text-align: center; padding: 40px; color: #ef4444;">
                    Error loading transactions
                </div>
            `;
        }}
    }}
    
    // ═══════════════════════════════════════════════════════════════
    // Helper Functions
    // ═══════════════════════════════════════════════════════════════
    
    function showError(elementId, message) {{
        const el = document.getElementById(elementId);
        el.className = 'error';
        el.innerHTML = '❌ ' + message;  // Use innerHTML to render HTML tags
        el.style.display = 'block';
    }}
    
    function showSuccess(elementId, message) {{
        const el = document.getElementById(elementId);
        el.className = 'success';
        el.innerHTML = '✅ ' + message;  // Use innerHTML for formatting
        el.style.display = 'block';
    }}
</script>

// ═══════════════════════════════════════════════════════════════════════
// END OF COMPLETE JAVASCRIPT SECTION
// ═══════════════════════════════════════════════════════════════════════
</body>
</html>
    """
    
    return html

# Startup event - CRITICAL FIX HERE!
@app.on_event("startup")
async def startup_event():
    print("=" * 60)
    print("🚀 NIKE ROCKET FOLLOWER API STARTED")
    print("=" * 60)
    print("✅ Database connected")
    print("✅ Follower routes loaded")
    print("✅ Portfolio routes loaded")
    print("✅ Signup page available at /signup")
    print("✅ Setup page available at /setup")
    print("✅ Dashboard available at /dashboard")
    print("✅ Ready to receive signals")
    
    # Start balance checker for automatic deposit/withdrawal detection
    # CRITICAL FIX: WITH STARTUP DELAY TO PREVENT RACE CONDITION!
    if DATABASE_URL:
        try:
            db_pool = await asyncpg.create_pool(DATABASE_URL)
            
            # ═══════════════════════════════════════════════════════════
            # CRITICAL FIX: Added startup_delay_seconds parameter!
            # This prevents the "relation does not exist" error by waiting
            # for database tables to be created before starting balance checker
            # ═══════════════════════════════════════════════════════════
            scheduler = BalanceCheckerScheduler(
                db_pool, 
                check_interval_minutes=60,
                startup_delay_seconds=30  # ← CRITICAL FIX: Wait 30s!
            )
            
            asyncio.create_task(scheduler.start())
            print("⏳ Balance checker scheduled (starts in 30 seconds)")
            
        except Exception as e:
            print(f"⚠️ Balance checker failed to start: {e}")
    
    print("=" * 60)

# Run locally for testing
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
