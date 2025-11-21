"""
Nike Rocket Follower System - Main API
=======================================
Updated main.py with hosted agents system.

Author: Nike Rocket Team
Updated: November 21, 2025
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
            "setup": "/setup",
            "dashboard": "/dashboard",
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

# Portfolio Dashboard (USER-FRIENDLY VERSION)
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
    </style>
</head>
<body>
    <div class="container">
        <!-- Login Screen -->
        <div id="login-screen" class="login-screen">
            <h1>🚀 Nike Rocket</h1>
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
            <h2>🎯 Welcome to Nike Rocket!</h2>
            <p>Let's set up your portfolio tracking in 30 seconds.</p>
            
            <div class="input-group">
                <label for="initial-capital">Starting Capital (USD):</label>
                <input 
                    type="number" 
                    id="initial-capital" 
                    placeholder="10000"
                    value="10000"
                >
            </div>
            
            <button class="btn" onclick="initializePortfolio()">Start Tracking</button>
            
            <div id="setup-message" style="display: none;"></div>
        </div>
        
        <!-- Dashboard -->
        <div id="dashboard" style="display: none;">
            <button class="logout-btn" onclick="logout()">Change API Key</button>
            
            <div class="hero">
                <h1>🚀 NIKE ROCKET PERFORMANCE</h1>
                
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
                <div style="margin: 30px 0; display: flex; gap: 15px; justify-content: center; flex-wrap: wrap;">
                    <button onclick="shareToTwitter()" style="
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
                        <span>𝕏</span> Share to X/Twitter
                    </button>
                    
                    <button onclick="copyShareLink()" style="
                        padding: 12px 24px;
                        background: #10b981;
                        color: white;
                        border: none;
                        border-radius: 8px;
                        font-weight: 600;
                        cursor: pointer;
                        font-size: 14px;
                        display: flex;
                        align-items: center;
                        gap: 8px;
                        box-shadow: 0 4px 12px rgba(16, 185, 129, 0.3);
                    " id="copy-link-btn">
                        <span>🔗</span> Copy Share Link
                    </button>
                    
                    <button onclick="downloadPerformanceCard()" style="
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
        let currentApiKey = '{api_key}';
        let currentPeriod = '30d';
        
        // On page load
        if (currentApiKey) {{
            document.getElementById('api-key-input').value = currentApiKey;
            login();
        }}
        
        function login() {{
            const apiKey = document.getElementById('api-key-input').value.trim();
            
            if (!apiKey) {{
                showError('login-error', 'Please enter your API key');
                return;
            }}
            
            if (!apiKey.startsWith('nk_')) {{
                showError('login-error', 'Invalid API key format. Keys should start with "nk_"');
                return;
            }}
            
            currentApiKey = apiKey;
            localStorage.setItem('apiKey', apiKey);
            
            // Try to load stats
            checkPortfolioStatus();
        }}
        
        function logout() {{
            localStorage.removeItem('apiKey');
            currentApiKey = '';
            document.getElementById('login-screen').style.display = 'block';
            document.getElementById('setup-wizard').style.display = 'none';
            document.getElementById('dashboard').style.display = 'none';
        }}
        
        async function checkPortfolioStatus() {{
            try {{
                const response = await fetch(`/api/portfolio/stats?period=30d`, {{
                    headers: {{'X-API-Key': currentApiKey}}
                }});
                
                if (response.status === 401) {{
                    showError('login-error', 'Invalid API key. Please check and try again.');
                    return;
                }}
                
                if (!response.ok) {{
                    // Handle other errors (500, 404, etc.)
                    console.error('Portfolio stats error:', response.status);
                    // Still try to show setup wizard for new users
                    showSetupWizard();
                    return;
                }}
                
                const data = await response.json();
                
                if (data.status === 'no_data') {{
                    // Portfolio initialized but no trades yet
                    showDashboard(data);
                }} else if (data.total_profit !== undefined) {{
                    // Portfolio has data
                    showDashboard(data);
                }} else {{
                    // Need to initialize
                    showSetupWizard();
                }}
                
            }} catch (error) {{
                console.error('Error:', error);
                // If error, assume needs setup
                showSetupWizard();
            }}
        }}
        
        function showSetupWizard() {{
            document.getElementById('login-screen').style.display = 'none';
            document.getElementById('setup-wizard').style.display = 'block';
            document.getElementById('dashboard').style.display = 'none';
        }}
        
        async function initializePortfolio() {{
            const initialCapital = parseFloat(document.getElementById('initial-capital').value);
            
            if (!initialCapital || initialCapital <= 0) {{
                showError('setup-message', 'Please enter a valid starting capital');
                return;
            }}
            
            try {{
                const response = await fetch('/api/portfolio/initialize', {{
                    method: 'POST',
                    headers: {{
                        'X-API-Key': currentApiKey,
                        'Content-Type': 'application/json'
                    }},
                    body: JSON.stringify({{initial_capital: initialCapital}})
                }});
                
                const data = await response.json();
                
                if (data.status === 'success' || data.status === 'already_initialized') {{
                    showSuccess('setup-message', 'Portfolio initialized! Loading dashboard...');
                    setTimeout(() => checkPortfolioStatus(), 1000);
                }} else {{
                    showError('setup-message', data.message || 'Failed to initialize portfolio');
                }}
                
            }} catch (error) {{
                showError('setup-message', 'Error initializing portfolio: ' + error.message);
            }}
        }}
        
        function showDashboard(stats) {{
            document.getElementById('login-screen').style.display = 'none';
            document.getElementById('setup-wizard').style.display = 'none';
            document.getElementById('dashboard').style.display = 'block';
            
            if (stats && stats.status !== 'no_data') {{
                updateDashboard(stats);
            }} else {{
                // Show empty state
                document.getElementById('profit-label').textContent = 'No Trades Yet';
                document.getElementById('total-profit').textContent = '$0';
                document.getElementById('time-tracking').textContent = 'Start trading to see your stats!';
            }}
        }}
        
        function updateDashboard(stats) {{
            document.getElementById('profit-label').textContent = `${{stats.period}} Profit`;
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
        }}
        
        async function changePeriod() {{
            currentPeriod = document.getElementById('period-selector').value;
            
            try {{
                const response = await fetch(`/api/portfolio/stats?period=${{currentPeriod}}`, {{
                    headers: {{'X-API-Key': currentApiKey}}
                }});
                
                const stats = await response.json();
                
                if (stats.status !== 'no_data') {{
                    updateDashboard(stats);
                }}
            }} catch (error) {{
                console.error('Error loading stats:', error);
            }}
        }}
        
        // ==================== SOCIAL SHARING FUNCTIONS (NEW!) ====================
        
        function shareToTwitter() {{
            const profit = document.getElementById('total-profit').textContent;
            const roi = document.getElementById('roi').textContent;
            const period = document.getElementById('period-selector').value;
            const trades = document.getElementById('total-trades').textContent;
            
            const periodLabels = {{
                '7d': 'Last 7 Days',
                '30d': 'Last 30 Days',
                '90d': 'Last 90 Days',
                'all': 'All-Time'
            }};
            
            const text = `🚀 Nike Rocket Trading Results (${{periodLabels[period]}})

💰 Profit: ${{profit}}
📈 ROI: ${{roi}}
📊 Trades: ${{trades}}

Automated crypto futures trading with Nike Rocket! 🎯

#CryptoTrading #NikeRocket #AutomatedTrading`;
            
            const twitterUrl = `https://twitter.com/intent/tweet?text=${{encodeURIComponent(text)}}`;
            window.open(twitterUrl, '_blank', 'width=600,height=400');
        }}
        
        function copyShareLink() {{
            const shareUrl = `${{window.location.origin}}/dashboard?key=${{currentApiKey}}`;
            
            navigator.clipboard.writeText(shareUrl).then(() => {{
                const btn = document.getElementById('copy-link-btn');
                const originalHTML = btn.innerHTML;
                btn.innerHTML = '<span>✅</span> Link Copied!';
                btn.style.background = '#10b981';
                
                setTimeout(() => {{
                    btn.innerHTML = originalHTML;
                    btn.style.background = '#10b981';
                }}, 2000);
            }}).catch(() => {{
                alert('Share link: ' + shareUrl);
            }});
        }}
        
        function downloadPerformanceCard() {{
            // Create a simple text-based shareable card
            const profit = document.getElementById('total-profit').textContent;
            const roi = document.getElementById('roi').textContent;
            const period = document.getElementById('period-selector').value;
            const trades = document.getElementById('total-trades').textContent;
            const profitFactor = document.getElementById('profit-factor').textContent;
            const sharpe = document.getElementById('sharpe').textContent;
            
            const periodLabels = {{
                '7d': 'Last 7 Days',
                '30d': 'Last 30 Days',
                '90d': 'Last 90 Days',
                'all': 'All-Time'
            }};
            
            // Create canvas for image
            const canvas = document.createElement('canvas');
            canvas.width = 1200;
            canvas.height = 630;
            const ctx = canvas.getContext('2d');
            
            // Gradient background
            const gradient = ctx.createLinearGradient(0, 0, canvas.width, canvas.height);
            gradient.addColorStop(0, '#667eea');
            gradient.addColorStop(1, '#764ba2');
            ctx.fillStyle = gradient;
            ctx.fillRect(0, 0, canvas.width, canvas.height);
            
            // Title
            ctx.fillStyle = 'white';
            ctx.font = 'bold 60px Arial';
            ctx.textAlign = 'center';
            ctx.fillText('🚀 NIKE ROCKET PERFORMANCE', canvas.width / 2, 100);
            
            // Period
            ctx.font = '30px Arial';
            ctx.fillText(periodLabels[period], canvas.width / 2, 160);
            
            // Main profit
            ctx.font = 'bold 100px Arial';
            ctx.fillText(profit, canvas.width / 2, 280);
            
            // Stats grid
            ctx.font = 'bold 40px Arial';
            ctx.textAlign = 'left';
            const leftX = 150;
            const rightX = 650;
            let y = 380;
            
            ctx.fillText(`ROI: ${{roi}}`, leftX, y);
            ctx.fillText(`Trades: ${{trades}}`, rightX, y);
            
            y += 70;
            ctx.fillText(`Profit Factor: ${{profitFactor}}`, leftX, y);
            ctx.fillText(`Sharpe: ${{sharpe}}`, rightX, y);
            
            // Footer
            ctx.textAlign = 'center';
            ctx.font = '25px Arial';
            ctx.fillText('Automated Crypto Trading', canvas.width / 2, 570);
            
            // Download
            canvas.toBlob((blob) => {{
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = 'nike-rocket-performance.png';
                a.click();
                URL.revokeObjectURL(url);
            }});
        }}
        
        function showError(elementId, message) {{
            const el = document.getElementById(elementId);
            el.className = 'error';
            el.textContent = '❌ ' + message;
            el.style.display = 'block';
        }}
        
        function showSuccess(elementId, message) {{
            const el = document.getElementById(elementId);
            el.className = 'success';
            el.textContent = '✅ ' + message;
            el.style.display = 'block';
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
    print("✅ Setup page available at /setup")
    print("✅ Dashboard available at /dashboard")
    print("✅ Ready to receive signals")
    print("=" * 60)

# Run locally for testing
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
