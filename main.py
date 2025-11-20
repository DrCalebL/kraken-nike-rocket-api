"""
Nike Rocket Follower System - Main API
=======================================
Updated main.py that includes follower system, portfolio tracking, and dashboard.
Add this to your Railway API project.

Author: Nike Rocket Team
Updated: November 20, 2025
"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy import create_engine
import os

# Import follower system
from follower_models import init_db
from follower_endpoints import router as follower_router

# Import portfolio system
from portfolio_models import init_portfolio_db
from portfolio_api import router as portfolio_router

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
        "service": "Nike Rocket Follower API",
        "version": "1.0.0",
        "endpoints": {
            "signup": "/signup",
            "dashboard": "/dashboard",
            "broadcast": "/api/broadcast-signal",
            "latest_signal": "/api/latest-signal",
            "report_pnl": "/api/report-pnl",
            "register": "/api/users/register",
            "verify": "/api/users/verify",
            "stats": "/api/users/stats",
            "portfolio_stats": "/api/portfolio/stats",
            "portfolio_trades": "/api/portfolio/trades",
            "portfolio_deposit": "/api/portfolio/deposit",
            "portfolio_withdraw": "/api/portfolio/withdraw",
            "pay": "/api/pay/{api_key}",
            "webhook": "/api/payments/webhook",
            "admin": "/api/admin/stats"
        }
    }

@app.get("/health")
async def health():
    return {"status": "healthy"}

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

# Portfolio Dashboard
@app.get("/dashboard", response_class=HTMLResponse)
async def portfolio_dashboard(request: Request):
    """Portfolio tracking dashboard"""
    
    # Get API key from query parameter or prompt user
    api_key = request.query_params.get('key', '')
    
    html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Nike Rocket - Portfolio Dashboard</title>
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
        
        #loading {{
            text-align: center;
            color: white;
            padding: 100px 20px;
            font-size: 24px;
        }}
        
        .error {{
            background: #fee2e2;
            color: #991b1b;
            padding: 20px;
            border-radius: 12px;
            margin: 40px 20px;
            text-align: center;
        }}
        
        .error button {{
            margin-top: 20px;
            padding: 12px 24px;
            font-size: 16px;
            background: #667eea;
            color: white;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-weight: 600;
        }}
        
        .error button:hover {{
            background: #5568d3;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div id="loading">
            <h2>⏳ Loading your portfolio...</h2>
        </div>
        
        <div id="dashboard" style="display: none;">
            <div class="hero">
                <h1>🚀 NIKE ROCKET PERFORMANCE</h1>
                
                <!-- Period Selector -->
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
            </div>
            
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-label">ROI on Initial Capital</div>
                    <div class="stat-value" id="roi">0%</div>
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
        </div>
    </div>
    
    <script>
        const API_URL = '';  // Same origin
        let API_KEY = '{api_key}';
        let currentPeriod = '30d';
        
        // If no API key in URL, prompt for it
        if (!API_KEY) {{
            API_KEY = prompt('Enter your API key:');
            if (!API_KEY) {{
                document.getElementById('loading').innerHTML = '<div class="error"><h2>❌ API Key Required</h2><p>Please provide your API key to view the dashboard.</p></div>';
            }}
        }}
        
        function changePeriod() {{
            currentPeriod = document.getElementById('period-selector').value;
            document.getElementById('loading').style.display = 'block';
            document.getElementById('dashboard').style.display = 'none';
            loadStats();
        }}
        
        async function loadStats() {{
            try {{
                const response = await fetch(`${{API_URL}}/api/portfolio/stats?period=${{currentPeriod}}`, {{
                    headers: {{'X-API-Key': API_KEY}}
                }});
                
                if (!response.ok) {{
                    if (response.status === 401) {{
                        throw new Error('Invalid API key. Please check your key and try again.');
                    }}
                    throw new Error(`HTTP ${{response.status}}`);
                }}
                
                const stats = await response.json();
                
                if (stats.status === 'no_data') {{
                    throw new Error('No trades recorded yet. Start trading to see your stats!');
                }}
                
                // Update period label
                document.getElementById('profit-label').textContent = `${{stats.period}} Profit`;
                
                // Update DOM
                document.getElementById('total-profit').textContent = `$${{stats.total_profit.toLocaleString()}}`;
                document.getElementById('roi').textContent = `+${{stats.roi_on_initial}}%`;
                document.getElementById('profit-factor').textContent = `${{stats.profit_factor}}x`;
                document.getElementById('best-trade').textContent = `$${{stats.best_trade.toLocaleString()}}`;
                document.getElementById('monthly-avg').textContent = `$${{stats.avg_monthly_profit.toLocaleString()}}`;
                document.getElementById('total-trades').textContent = stats.total_trades;
                document.getElementById('max-dd').textContent = `-${{stats.max_drawdown}}%`;
                document.getElementById('dd-recovery').textContent = `+${{stats.recovery_from_dd.toFixed(0)}}% recovered`;
                document.getElementById('sharpe').textContent = stats.sharpe_ratio.toFixed(1);
                document.getElementById('days-active').textContent = stats.days_active;
                
                document.getElementById('pf-detail').textContent = 
                    `$${{stats.gross_wins.toLocaleString()}} wins / $${{stats.gross_losses.toLocaleString()}} losses`;
                
                if (stats.started_tracking) {{
                    const startDate = new Date(stats.started_tracking);
                    document.getElementById('time-tracking').textContent = 
                        `Trading since ${{startDate.toLocaleDateString()}} • ${{stats.period}}`;
                }}
                
                document.getElementById('loading').style.display = 'none';
                document.getElementById('dashboard').style.display = 'block';
                
            }} catch (error) {{
                console.error('Error loading stats:', error);
                document.getElementById('loading').innerHTML = `
                    <div class="error">
                        <h2>❌ Error Loading Data</h2>
                        <p>${{error.message}}</p>
                        <button onclick="location.reload()">Retry</button>
                    </div>
                `;
            }}
        }}
        
        // Load stats on page load
        if (API_KEY) {{
            loadStats();
        }}
    </script>
</body>
</html>
    """
    
    return html

# Startup event
@app.on_event("startup")
async def startup_event():
    print("=" * 60)
    print("🚀 NIKE ROCKET FOLLOWER API STARTED")
    print("=" * 60)
    print("✅ Database connected")
    print("✅ Follower routes loaded")
    print("✅ Portfolio routes loaded")
    print("✅ Signup page available at /signup")
    print("✅ Dashboard available at /dashboard")
    print("✅ Ready to receive signals")
    print("=" * 60)

# Run locally for testing
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
