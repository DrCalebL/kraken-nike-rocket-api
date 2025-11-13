"""
Nike Rocket Follower System - Main API
=======================================

Updated main.py that includes follower system endpoints.

Add this to your existing Railway API project.

Author: Nike Rocket Team
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine
import os

# Import follower system
from follower_models import init_db
from follower_endpoints import router as follower_router

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
    print("✅ Database initialized")
else:
    print("⚠️ DATABASE_URL not set - database features disabled")


# Include follower routes
app.include_router(follower_router, tags=["follower"])


# Health check
@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Nike Rocket Follower API",
        "version": "1.0.0",
        "endpoints": {
            "broadcast": "/api/broadcast-signal",
            "latest_signal": "/api/latest-signal",
            "report_pnl": "/api/report-pnl",
            "register": "/api/users/register",
            "verify": "/api/users/verify",
            "stats": "/api/users/stats",
            "pay": "/api/pay/{api_key}",
            "webhook": "/api/payments/webhook",
            "admin": "/api/admin/stats"
        }
    }


@app.get("/health")
async def health():
    return {"status": "healthy"}


# Startup event
@app.on_event("startup")
async def startup_event():
    print("=" * 60)
    print("🚀 NIKE ROCKET FOLLOWER API STARTED")
    print("=" * 60)
    print("✅ Database connected")
    print("✅ Follower routes loaded")
    print("✅ Ready to receive signals")
    print("=" * 60)


# Run locally for testing
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
