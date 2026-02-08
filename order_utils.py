"""
Order Utilities with Retry Logic & Email Notifications
=======================================================

Provides robust order execution with:
- Automatic retry on failures (3 attempts, exponential backoff)
- Email notifications via Resend on failures
- Detailed logging

Usage:
    from order_utils import place_order_with_retry, notify_admin

    # Place order with automatic retry
    order = await place_order_with_retry(
        exchange=exchange,
        symbol="PF_XBTUSD",
        order_type="limit",
        side="sell",
        amount=0.01,
        price=50000,
        params={'reduceOnly': True},
        order_description="Take-Profit",
        user_email="user@example.com"
    )

    # Manual notification
    await notify_admin("⚠️ Something happened", {"detail": "value"})
"""

import asyncio
import logging
import os
from typing import Any, Dict, Optional
from datetime import datetime

import aiohttp

# Setup logging
logger = logging.getLogger("ORDER_UTILS")

# Configuration
MAX_RETRIES = 3
INITIAL_BACKOFF = 1.0  # seconds
MAX_BACKOFF = 8.0  # seconds

# Resend email configuration
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_API_URL = "https://api.resend.com/emails"
FROM_EMAIL = os.getenv("FROM_EMAIL", "$NIKEPIG's Massive Rocket <onboarding@resend.dev>")
ADMIN_EMAIL = "calebws87@gmail.com"


async def notify_admin(
    title: str,
    details: Dict[str, Any],
    level: str = "warning"  # "info", "warning", "error"
) -> bool:
    """
    Send notification to admin via Resend email.
    
    Args:
        title: Notification title (email subject)
        details: Dict of details to include
        level: Severity level (affects subject prefix)
        
    Returns:
        True if sent successfully
    """
    if not RESEND_API_KEY:
        logger.warning("⚠️ RESEND_API_KEY not set - notification skipped")
        return False
    
    # Subject prefix based on level
    prefixes = {
        "info": "ℹ️ INFO",
        "warning": "⚠️ WARNING", 
        "error": "🚨 CRITICAL",
        "success": "✅ SUCCESS",
    }
    prefix = prefixes.get(level, prefixes["warning"])
    
    subject = f"{prefix}: {title}"
    
    # Build HTML body
    html_rows = ""
    for key, value in details.items():
        html_rows += f"""
        <tr>
            <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold; background: #f5f5f5;">{key.replace('_', ' ').title()}</td>
            <td style="padding: 8px; border: 1px solid #ddd; word-break: break-all;">{value}</td>
        </tr>
        """
    
    # Color based on level
    colors = {
        "info": "#3498db",
        "warning": "#f39c12",
        "error": "#e74c3c",
        "success": "#2ecc71",
    }
    color = colors.get(level, colors["warning"])
    
    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; padding: 20px;">
        <div style="border-left: 4px solid {color}; padding-left: 15px; margin-bottom: 20px;">
            <h2 style="color: {color}; margin: 0;">{title}</h2>
            <p style="color: #666; margin: 5px 0;">Nike Rocket Alert - {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</p>
        </div>
        
        <table style="border-collapse: collapse; width: 100%; max-width: 600px;">
            {html_rows}
        </table>
        
        <p style="color: #999; font-size: 12px; margin-top: 20px;">
            This is an automated notification from Nike Rocket trading system.
        </p>
    </body>
    </html>
    """
    
    # Send via Resend
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                RESEND_API_URL,
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "from": FROM_EMAIL,
                    "to": [ADMIN_EMAIL],
                    "subject": subject,
                    "html": html_body
                },
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status in (200, 201):
                    logger.info(f"✅ Email notification sent: {title}")
                    return True
                else:
                    error_text = await resp.text()
                    logger.warning(f"⚠️ Resend API returned {resp.status}: {error_text}")
                    return False
                    
    except Exception as e:
        logger.error(f"❌ Failed to send email notification: {e}")
        return False


async def place_order_with_retry(
    exchange,
    symbol: str,
    order_type: str,
    side: str,
    amount: float,
    price: Optional[float] = None,
    params: Optional[Dict] = None,
    order_description: str = "Order",
    user_email: str = "unknown",
    user_api_key: str = "unknown",
    notify_on_failure: bool = True
) -> Optional[Dict]:
    """
    Place an order with automatic retry and admin notification on failure.
    
    Args:
        exchange: CCXT exchange instance
        symbol: Trading symbol (e.g., "PF_XBTUSD")
        order_type: Order type ("market", "limit", "stop", "stop_market")
        side: "buy" or "sell"
        amount: Order quantity
        price: Price for limit/stop orders (None for market)
        params: Additional CCXT params (e.g., {'reduceOnly': True})
        order_description: Human-readable description (e.g., "Take-Profit", "Stop-Loss")
        user_email: User email for notifications
        user_api_key: User API key for notifications
        notify_on_failure: Whether to send Discord notification on failure
        
    Returns:
        Order dict on success, None on failure (after all retries)
    """
    params = params or {}
    backoff = INITIAL_BACKOFF
    last_error = None
    attempts_log = []
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"📝 Placing {order_description} (attempt {attempt}/{MAX_RETRIES})...")
            
            if order_type == "market":
                order = exchange.create_market_order(symbol, side, amount, params=params)
            elif order_type == "limit":
                order = exchange.create_limit_order(symbol, side, amount, price, params=params)
            else:
                # stop, stop_market, etc.
                order = exchange.create_order(symbol, order_type, side, amount, price, params=params)
            
            logger.info(f"✅ {order_description} placed: {order['id']}")
            return order
            
        except Exception as e:
            last_error = e
            error_msg = str(e)[:200]
            attempts_log.append(f"Attempt {attempt}: {error_msg}")
            
            logger.warning(f"⚠️ {order_description} failed (attempt {attempt}/{MAX_RETRIES}): {error_msg}")
            
            if attempt < MAX_RETRIES:
                logger.info(f"⏳ Retrying in {backoff:.1f}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
    
    # All retries exhausted
    logger.error(f"❌ {order_description} FAILED after {MAX_RETRIES} attempts for {user_email}")
    
    # Send admin notification
    if notify_on_failure:
        await notify_admin(
            title=f"🚨 {order_description} FAILED",
            details={
                "User": user_email,
                "API Key": user_api_key[:20] + "..." if len(user_api_key) > 20 else user_api_key,
                "Symbol": symbol,
                "Side": side,
                "Amount": amount,
                "Price": price or "Market",
                "Order Type": order_type,
                "Attempts": "\n".join(attempts_log),
                "Final Error": str(last_error)[:500],
            },
            level="error"
        )
    
    return None


async def place_tp_order_with_retry(
    exchange,
    symbol: str,
    exit_side: str,
    quantity: float,
    tp_price: float,
    user_email: str = "unknown",
    user_api_key: str = "unknown"
) -> Optional[Dict]:
    """
    Place Take-Profit order with retry.
    
    Convenience wrapper for place_order_with_retry.
    """
    return await place_order_with_retry(
        exchange=exchange,
        symbol=symbol,
        order_type="limit",
        side=exit_side,
        amount=quantity,
        price=tp_price,
        params={'reduceOnly': True},
        order_description="Take-Profit",
        user_email=user_email,
        user_api_key=user_api_key
    )


async def place_sl_order_with_retry(
    exchange,
    symbol: str,
    exit_side: str,
    quantity: float,
    sl_price: float,
    user_email: str = "unknown",
    user_api_key: str = "unknown"
) -> Optional[Dict]:
    """
    Place Stop-Loss order with retry.
    
    Tries stop_market first, falls back to stop.
    """
    # Try stop_market first
    order = await place_order_with_retry(
        exchange=exchange,
        symbol=symbol,
        order_type="stop_market",
        side=exit_side,
        amount=quantity,
        price=None,
        params={'stopPrice': sl_price, 'reduceOnly': True},
        order_description="Stop-Loss (stop_market)",
        user_email=user_email,
        user_api_key=user_api_key,
        notify_on_failure=False  # Don't notify yet, we'll try fallback
    )
    
    if order:
        return order
    
    # Fallback to stop order type
    logger.info("🔄 Trying stop order type as fallback...")
    
    return await place_order_with_retry(
        exchange=exchange,
        symbol=symbol,
        order_type="stop",
        side=exit_side,
        amount=quantity,
        price=None,
        params={'triggerPrice': sl_price, 'reduceOnly': True},
        order_description="Stop-Loss (stop)",
        user_email=user_email,
        user_api_key=user_api_key,
        notify_on_failure=True  # Notify if this also fails
    )


async def place_entry_order_with_retry(
    exchange,
    symbol: str,
    side: str,
    quantity: float,
    user_email: str = "unknown",
    user_api_key: str = "unknown"
) -> Optional[Dict]:
    """
    Place Entry (market) order with retry.
    
    Convenience wrapper for place_order_with_retry.
    """
    return await place_order_with_retry(
        exchange=exchange,
        symbol=symbol,
        order_type="market",
        side=side,
        amount=quantity,
        price=None,
        params={},
        order_description="Entry",
        user_email=user_email,
        user_api_key=user_api_key
    )


# ==================== CRITICAL FAILURE NOTIFICATIONS ====================

async def notify_entry_failed(
    user_email: str,
    user_api_key: str,
    symbol: str,
    side: str,
    quantity: float,
    error: str
):
    """Notify admin when entry order fails - CRITICAL, position not opened."""
    await notify_admin(
        title="🚨 CRITICAL: Entry Order Failed",
        details={
            "Severity": "HIGH - Position NOT opened",
            "User": user_email,
            "API Key": user_api_key[:20] + "...",
            "Symbol": symbol,
            "Side": side,
            "Quantity": quantity,
            "Error": error[:500],
            "Action Required": "Check user account manually",
        },
        level="error"
    )


async def notify_bracket_incomplete(
    user_email: str,
    user_api_key: str,
    symbol: str,
    entry_order_id: str,
    tp_placed: bool,
    sl_placed: bool,
    error: str
):
    """Notify admin when TP/SL fails - DANGEROUS, position unprotected."""
    await notify_admin(
        title="🚨 DANGER: Bracket Order Incomplete",
        details={
            "Severity": "CRITICAL - Position UNPROTECTED",
            "User": user_email,
            "API Key": user_api_key[:20] + "...",
            "Symbol": symbol,
            "Entry Order": entry_order_id,
            "TP Placed": "✅ Yes" if tp_placed else "❌ NO",
            "SL Placed": "✅ Yes" if sl_placed else "❌ NO",
            "Error": error[:500],
            "Action Required": "IMMEDIATELY place missing orders manually!",
        },
        level="error"
    )


async def notify_signal_invalid(
    signal_id: str,
    symbol: str,
    action: str,
    missing_fields: list,
    reason: str
):
    """Notify admin when a signal is missing SL/TP - TRADE NOT TAKEN."""
    await notify_admin(
        title="🚫 SIGNAL REJECTED - Missing SL/TP",
        details={
            "Signal ID": signal_id or "unknown",
            "Symbol": symbol or "unknown",
            "Action": action or "unknown",
            "Missing Fields": ", ".join(missing_fields) if missing_fields else "none",
            "Reason": reason,
            "Impact": "Trade NOT executed for ANY user",
            "Action Required": "Check master algorithm signal generation",
        },
        level="error"
    )


async def notify_signal_invalid_values(
    signal_id: str,
    symbol: str,
    action: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    reason: str
):
    """Notify admin when a signal has invalid SL/TP values."""
    await notify_admin(
        title="🚫 SIGNAL REJECTED - Invalid SL/TP Values",
        details={
            "Signal ID": signal_id or "unknown",
            "Symbol": symbol or "unknown",
            "Action": action or "unknown",
            "Entry Price": entry_price,
            "Stop Loss": stop_loss,
            "Take Profit": take_profit,
            "Reason": reason,
            "Impact": "Trade NOT executed for ANY user",
            "Action Required": "Check master algorithm signal generation",
        },
        level="error"
    )


# ==================== NEW NOTIFICATION TYPES ====================

async def notify_api_failure(
    service: str,
    endpoint: str,
    error: str,
    status_code: int = None,
    user_api_key: str = None,
    impact: str = "Operation skipped"
):
    """Notify admin when external API fails (Kraken, etc)."""
    details = {
        "Service": service,
        "Endpoint": endpoint[:100] if endpoint else "unknown",
        "Error": str(error)[:500],
        "Impact": impact,
    }
    if status_code:
        details["Status Code"] = status_code
    if user_api_key:
        details["User API Key"] = user_api_key[:20] + "..."
    
    await notify_admin(
        title=f"🔌 API FAILURE: {service}",
        details=details,
        level="error"
    )


async def notify_database_error(
    operation: str,
    error: str,
    table: str = None,
    user_api_key: str = None,
    query_snippet: str = None
):
    """Notify admin when database operation fails."""
    details = {
        "Operation": operation,
        "Error": str(error)[:500],
        "Impact": "Data may not be saved/retrieved correctly",
    }
    if table:
        details["Table"] = table
    if user_api_key:
        details["User API Key"] = user_api_key[:20] + "..."
    if query_snippet:
        details["Query"] = query_snippet[:200]
    
    await notify_admin(
        title=f"🗄️ DATABASE ERROR: {operation}",
        details=details,
        level="error"
    )


async def notify_critical_error(
    error_type: str,
    error: str,
    location: str = None,
    user_api_key: str = None,
    context: dict = None
):
    """Notify admin for any critical/unhandled error."""
    details = {
        "Error Type": error_type,
        "Error": str(error)[:500],
        "Severity": "CRITICAL - Requires investigation",
    }
    if location:
        details["Location"] = location
    if user_api_key:
        details["User API Key"] = user_api_key[:20] + "..."
    if context:
        # Add context items (limit to avoid huge emails)
        for k, v in list(context.items())[:5]:
            details[k] = str(v)[:200]
    
    await notify_admin(
        title=f"🚨 CRITICAL ERROR: {error_type}",
        details=details,
        level="error"
    )


async def notify_security_alert(
    alert_type: str,
    details_dict: dict,
    ip_address: str = None,
    user_agent: str = None
):
    """Notify admin of potential security issues (SQL injection attempts, etc)."""
    details = {
        "Alert Type": alert_type,
        "Severity": "SECURITY - Potential attack detected",
        **{k: str(v)[:200] for k, v in details_dict.items()}
    }
    if ip_address:
        details["IP Address"] = ip_address
    if user_agent:
        details["User Agent"] = user_agent[:200]
    
    await notify_admin(
        title=f"🛡️ SECURITY ALERT: {alert_type}",
        details=details,
        level="error"
    )
