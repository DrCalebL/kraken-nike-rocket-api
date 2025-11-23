# UPDATED PORTFOLIO API - AUTO-DETECT INITIAL CAPITAL
# ====================================================
# FULLY CORRECTED VERSION - Proper column names for all tables

from fastapi import APIRouter, Request, HTTPException
from datetime import datetime
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
    """
    Get user's Kraken API credentials from database
    
    CORRECTED: Queries follower_users table with encrypted credentials
    """
    DATABASE_URL = os.getenv("DATABASE_URL")
    if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    
    conn = await asyncpg.connect(DATABASE_URL)
    
    # CORRECTED QUERY: Use follower_users instead of follower_agents
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
    
    # Decrypt credentials
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
        
        # Initialize Kraken API
        kraken = krakenex.API(key=kraken_key, secret=kraken_secret)
        k = KrakenAPI(kraken)
        
        # Get balance
        balance = k.get_account_balance()
        
        # Get USDT balance (or ZUSD depending on setup)
        usdt_balance = 0
        for currency in ['USDT', 'ZUSD', 'USD']:
            if currency in balance.index:
                usdt_balance = float(balance.loc[currency]['vol'])
                break
        
        return Decimal(str(usdt_balance))
        
    except Exception as e:
        print(f"Error getting Kraken balance: {e}")
        return None


# NEW: Auto-detect initialize
@router.post("/api/portfolio/initialize")
async def initialize_portfolio_autodetect(request: Request):
    """
    Initialize portfolio tracking - AUTO-DETECTS initial capital from Kraken
    
    FULLY CORRECTED with proper column names
    """
    api_key = request.headers.get("X-API-Key")
    
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")
    
    try:
        DATABASE_URL = os.getenv("DATABASE_URL")
        if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
            DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        
        conn = await asyncpg.connect(DATABASE_URL)
        
        # CORRECTED: Use api_key column
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
        
        # Get user's Kraken credentials (CORRECTED)
        credentials = await get_kraken_credentials(api_key)
        
        if not credentials:
            await conn.close()
            return {
                "status": "error",
                "message": "No trading agent found. Please set up your agent first at /setup"
            }
        
        # Get current Kraken balance (AUTO-DETECT!)
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
        
        # Check for zero or negative balance
        if kraken_balance <= 0:
            await conn.close()
            return {
                "status": "error",
                "message": f"Your Kraken balance is $0. Please deposit funds to your Kraken account first."
            }
        
        # Check minimum balance ($10 to avoid tracking dust amounts)
        MINIMUM_BALANCE = 10
        if kraken_balance < MINIMUM_BALANCE:
            await conn.close()
            return {
                "status": "error",
                "message": f"Minimum balance required: ${MINIMUM_BALANCE}. Your current balance: ${float(kraken_balance):.2f}"
            }
        
        # Use current balance as initial capital!
        initial_capital = float(kraken_balance)
        
        # CORRECTED: Use api_key column
        await conn.execute("""
            INSERT INTO portfolio_users (api_key, initial_capital, created_at, last_known_balance)
            VALUES ($1, $2, CURRENT_TIMESTAMP, $2)
        """, api_key, initial_capital)
        
        # Create initial transaction (user_id in portfolio_transactions is api_key string)
        await conn.execute("""
            INSERT INTO portfolio_transactions (
                user_id, transaction_type, amount, detection_method, notes
            ) VALUES ($1, 'initial', $2, 'automatic', $3)
        """, api_key, initial_capital, 
            f'Auto-detected from Kraken balance: ${initial_capital:,.2f}')
        
        await conn.close()
        
        return {
            "status": "success",
            "message": f"Portfolio initialized with ${initial_capital:,.2f} from your Kraken account",
            "initial_capital": initial_capital,
            "detected_from": "kraken_balance"
        }
        
    except Exception as e:
        print(f"Error initializing portfolio: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# Balance Summary endpoint
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


# Transaction History endpoint
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
