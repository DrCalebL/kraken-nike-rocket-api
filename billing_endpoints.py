"""
Nike Rocket Billing Endpoints
==============================
Admin endpoints for billing operations:
- Manual invoice generation
- Payment reminders
- Billing summary
- Manual suspend/restore

Add these to main.py or create separate router
"""

from fastapi import APIRouter, HTTPException, Depends, Header
from datetime import datetime
import os

# Import billing service
from billing_service import BillingService

router = APIRouter(tags=["billing"])

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme123")


def verify_admin(x_admin_password: str = Header(None)):
    """Verify admin password"""
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    return True


# =============================================================================
# ADMIN BILLING ENDPOINTS
# =============================================================================

@router.post("/api/admin/billing/process-monthly")
async def admin_process_monthly_billing(
    admin: bool = Depends(verify_admin),
    db_pool = None  # Inject from app state
):
    """
    Manually trigger monthly billing process
    
    This will:
    1. Send invoices to all users with fees due
    2. Reset monthly counters for new month
    
    Auth: Admin password required
    """
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database pool not available")
    
    billing = BillingService(db_pool)
    result = await billing.process_monthly_billing()
    
    return {
        "status": "success",
        "message": f"Processed monthly billing",
        **result
    }


@router.post("/api/admin/billing/send-reminders")
async def admin_send_reminders(
    admin: bool = Depends(verify_admin),
    db_pool = None
):
    """
    Manually trigger payment reminder emails
    
    Auth: Admin password required
    """
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database pool not available")
    
    billing = BillingService(db_pool)
    result = await billing.send_payment_reminders()
    
    return {
        "status": "success",
        "message": "Payment reminders sent",
        **result
    }


@router.post("/api/admin/billing/process-suspensions")
async def admin_process_suspensions(
    admin: bool = Depends(verify_admin),
    db_pool = None
):
    """
    Manually trigger auto-suspension check
    
    Auth: Admin password required
    """
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database pool not available")
    
    billing = BillingService(db_pool)
    result = await billing.process_auto_suspensions()
    
    return {
        "status": "success",
        "message": "Suspension check complete",
        **result
    }


@router.get("/api/admin/billing/summary")
async def admin_billing_summary(
    admin: bool = Depends(verify_admin),
    db_pool = None
):
    """
    Get billing summary
    
    Returns:
    - Unpaid invoices count/amount
    - Paid invoices count
    - Total collected
    - Users by tier
    - Suspended for non-payment
    
    Auth: Admin password required
    """
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database pool not available")
    
    billing = BillingService(db_pool)
    summary = await billing.get_billing_summary()
    
    return {
        "status": "success",
        "billing_summary": summary
    }


@router.post("/api/admin/billing/send-invoice/{user_id}")
async def admin_send_invoice(
    user_id: int,
    admin: bool = Depends(verify_admin),
    db_pool = None
):
    """
    Manually send invoice to specific user
    
    Auth: Admin password required
    """
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database pool not available")
    
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("""
            SELECT id, email, api_key, fee_tier, monthly_profit, monthly_fee_due
            FROM follower_users
            WHERE id = $1
        """, user_id)
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        if user['monthly_fee_due'] <= 0:
            return {
                "status": "skipped",
                "message": "No fees due for this user"
            }
        
        billing = BillingService(db_pool)
        email_sent = billing.send_invoice_email(
            to_email=user['email'],
            api_key=user['api_key'],
            amount=float(user['monthly_fee_due']),
            profit=float(user['monthly_profit']),
            fee_tier=user['fee_tier'] or 'standard',
            for_month=datetime.utcnow().strftime("%Y-%m")
        )
        
        if email_sent:
            # Update last_fee_calculation
            await conn.execute("""
                UPDATE follower_users
                SET last_fee_calculation = CURRENT_TIMESTAMP
                WHERE id = $1
            """, user_id)
        
        return {
            "status": "success" if email_sent else "failed",
            "user": user['email'],
            "amount": float(user['monthly_fee_due'])
        }


@router.post("/api/admin/billing/waive-fees/{user_id}")
async def admin_waive_fees(
    user_id: int,
    admin: bool = Depends(verify_admin),
    db_pool = None
):
    """
    Waive current fees for a user (mark as paid without payment)
    
    Auth: Admin password required
    """
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database pool not available")
    
    async with db_pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE follower_users
            SET 
                monthly_fee_paid = true,
                monthly_fee_due = 0
            WHERE id = $1
        """, user_id)
        
        if result == "UPDATE 0":
            raise HTTPException(status_code=404, detail="User not found")
        
        return {
            "status": "success",
            "message": f"Fees waived for user {user_id}"
        }


@router.post("/api/admin/billing/restore-access/{user_id}")
async def admin_restore_access(
    user_id: int,
    admin: bool = Depends(verify_admin),
    db_pool = None
):
    """
    Manually restore access for suspended user
    
    Auth: Admin password required
    """
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database pool not available")
    
    async with db_pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE follower_users
            SET 
                access_granted = true,
                suspended_at = NULL,
                suspension_reason = NULL
            WHERE id = $1
        """, user_id)
        
        if result == "UPDATE 0":
            raise HTTPException(status_code=404, detail="User not found")
        
        return {
            "status": "success",
            "message": f"Access restored for user {user_id}"
        }
