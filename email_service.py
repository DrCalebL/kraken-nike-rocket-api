"""
Email Service - Resend Integration (Updated for Hosted Agents)
==============================================================

Sends emails using Resend API.
Free tier: 3,000 emails/month

Setup:
1. Sign up at https://resend.com
2. Get API key
3. Set RESEND_API_KEY environment variable

Updated: November 22, 2025 - Improved UX with prominent dashboard
"""

import os
import requests
from typing import Optional

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_API_URL = "https://api.resend.com/emails"
FROM_EMAIL = os.getenv("FROM_EMAIL", "$NIKEPIG's Massive Rocket <onboarding@resend.dev>")
BASE_URL = os.getenv("BASE_URL", "https://nike-rocket-api-production.up.railway.app")


def send_welcome_email(to_email: str, api_key: str) -> bool:
    """
    Send welcome email with API key and setup link (HOSTED AGENTS)
    
    This is the main email for new signups with hosted agents.
    User gets their API key and a link to /setup page.
    
    Args:
        to_email: User's email address
        api_key: User's unique API key
        
    Returns:
        True if sent successfully, False otherwise
    """
    if not RESEND_API_KEY:
        print("⚠️ RESEND_API_KEY not set - email not sent")
        print(f"🔗 Setup link (for testing): {BASE_URL}/setup?key={api_key}")
        return False
    
    setup_link = f"{BASE_URL}/setup?key={api_key}"
    dashboard_link = f"{BASE_URL}/dashboard?key={api_key}"
    login_link = f"{BASE_URL}/login"
    
    # Email HTML - IMPROVED VERSION
    html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Your $NIKEPIG's Massive Rocket API Key</title>
</head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif; background: #f5f5f5;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background: #f5f5f5; padding: 40px 20px;">
        <tr>
            <td align="center">
                <table width="600" cellpadding="0" cellspacing="0" style="max-width: 600px; background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">
                    
                    <!-- Header -->
                    <tr>
                        <td style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 40px 30px; text-align: center;">
                            <h1 style="margin: 0; color: white; font-size: 32px; font-weight: bold;">
                                🚀 $NIKEPIG's Massive Rocket
                            </h1>
                            <p style="margin: 8px 0 0 0; color: rgba(255,255,255,0.9); font-size: 16px;">
                                Your API Key
                            </p>
                        </td>
                    </tr>
                    
                    <!-- Body -->
                    <tr>
                        <td style="padding: 40px 30px;">
                            
                            <!-- Your API Key Section -->
                            <h2 style="margin: 0 0 20px 0; color: #667eea; font-size: 20px;">
                                Your API Key
                            </h2>
                            <p style="margin: 0 0 15px 0; color: #374151; font-size: 14px;">
                                As requested, here's your $NIKEPIG's Massive Rocket API key:
                            </p>
                            
                            <div style="background: #f9fafb; border: 2px dashed #d1d5db; border-radius: 8px; padding: 20px; margin-bottom: 30px;">
                                <code style="font-family: 'Courier New', monospace; font-size: 14px; color: #667eea; word-break: break-all; display: block; text-align: center;">
                                    {api_key}
                                </code>
                            </div>
                            
                            <div style="background: #fef3c7; border-left: 4px solid #f59e0b; padding: 15px; border-radius: 6px; margin-bottom: 30px;">
                                <p style="margin: 0; color: #92400e; font-size: 13px;">
                                    🔒 <strong>Security Reminder:</strong> Never share your API key with anyone. If you believe your key has been compromised, contact support immediately.
                                </p>
                            </div>
                            
                            <!-- BIG DASHBOARD BUTTON (IMPROVED!) -->
                            <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom: 30px;">
                                <tr>
                                    <td style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 12px; padding: 30px; text-align: center; box-shadow: 0 4px 12px rgba(102, 126, 234, 0.3);">
                                        <p style="margin: 0 0 15px 0; color: white; font-size: 20px; font-weight: bold;">
                                            📊 View Your Dashboard
                                        </p>
                                        <a href="{dashboard_link}" style="display: inline-block; background: white; color: #667eea; padding: 18px 50px; border-radius: 10px; text-decoration: none; font-weight: bold; font-size: 18px; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
                                            📈 Open Dashboard Now
                                        </a>
                                        <p style="margin: 15px 0 0 0; color: rgba(255,255,255,0.95); font-size: 14px; font-weight: 600;">
                                            💡 <strong>Tip:</strong> Bookmark this link for easy access!
                                        </p>
                                    </td>
                                </tr>
                            </table>
                            
                            <!-- Quick Setup Notice -->
                            <div style="background: #d1fae5; border-left: 4px solid #10b981; padding: 20px; border-radius: 8px; margin-bottom: 30px;">
                                <p style="margin: 0 0 10px 0; color: #065f46; font-size: 16px; font-weight: bold;">
                                    ⚡ Quick Setup (2 minutes):
                                </p>
                                <p style="margin: 0 0 15px 0; color: #047857; font-size: 14px;">
                                    Click the button below to set up your automated trading agent. No technical skills required!
                                </p>
                                <a href="{setup_link}" style="display: inline-block; background: #10b981; color: white; padding: 14px 35px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 15px; box-shadow: 0 2px 8px rgba(16, 185, 129, 0.3);">
                                    🛠️ Setup Agent
                                </a>
                            </div>
                            
                            <!-- What Happens Next (KEPT!) -->
                            <h3 style="margin: 0 0 15px 0; color: #374151; font-size: 18px; font-weight: 600;">
                                What happens next:
                            </h3>
                            <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom: 30px;">
                                <tr>
                                    <td style="padding: 12px 0; border-bottom: 1px solid #e5e7eb;">
                                        <p style="margin: 0; color: #374151; font-size: 14px; line-height: 1.6;">
                                            <strong>1.</strong> Click the setup button above - Opens your personalized setup page
                                        </p>
                                    </td>
                                </tr>
                                <tr>
                                    <td style="padding: 12px 0; border-bottom: 1px solid #e5e7eb;">
                                        <p style="margin: 0; color: #374151; font-size: 14px; line-height: 1.6;">
                                            <strong>2.</strong> Enter your Kraken API credentials - Takes 30 seconds
                                        </p>
                                    </td>
                                </tr>
                                <tr>
                                    <td style="padding: 12px 0; border-bottom: 1px solid #e5e7eb;">
                                        <p style="margin: 0; color: #374151; font-size: 14px; line-height: 1.6;">
                                            <strong>3.</strong> Your agent starts automatically - Begins following trading signals
                                        </p>
                                    </td>
                                </tr>
                                <tr>
                                    <td style="padding: 12px 0;">
                                        <p style="margin: 0; color: #374151; font-size: 14px; line-height: 1.6;">
                                            <strong>4.</strong> Track performance on your dashboard - View profits in real-time
                                        </p>
                                    </td>
                                </tr>
                            </table>
                            
                            <!-- RETURNING USERS SECTION (NEW!) -->
                            <div style="background: #ede9fe; border-left: 4px solid #7c3aed; padding: 20px; border-radius: 8px;">
                                <p style="margin: 0 0 10px 0; color: #5b21b6; font-size: 16px; font-weight: bold;">
                                    🔖 For Future Access:
                                </p>
                                <p style="margin: 0 0 10px 0; color: #6b21a8; font-size: 14px; line-height: 1.6;">
                                    <strong>Bookmark your dashboard link above</strong>, or use our login page anytime:
                                </p>
                                <p style="margin: 0 0 15px 0; color: #6b21a8; font-size: 14px;">
                                    <a href="{login_link}" style="color: #7c3aed; text-decoration: none; font-weight: 600; font-size: 15px;">
                                        🔗 {login_link}
                                    </a>
                                </p>
                                <p style="margin: 0; color: #6b21a8; font-size: 13px;">
                                    💡 Just enter your API key to access your dashboard from any device!
                                </p>
                            </div>
                            
                        </td>
                    </tr>
                    
                    <!-- Footer -->
                    <tr>
                        <td style="padding: 30px; background: #f9fafb; text-align: center; border-top: 1px solid #e5e7eb;">
                            <p style="margin: 0 0 10px 0; color: #6b7280; font-size: 13px;">
                                Questions? Need help? Contact us anytime.
                            </p>
                            <p style="margin: 0 0 5px 0; color: #9ca3af; font-size: 12px;">
                                $NIKEPIG's Massive Rocket - Automated Kraken Futures Trading
                            </p>
                            <p style="margin: 0; color: #9ca3af; font-size: 12px;">
                                You're receiving this email because you signed up at {BASE_URL}
                            </p>
                        </td>
                    </tr>
                    
                </table>
            </td>
        </tr>
    </table>
</body>
</html>
    """
    
    # Plain text version
    text_content = f"""
🚀 Your $NIKEPIG's Massive Rocket API Key

Your API Key:
{api_key}

🔒 Security Reminder: Never share your API key with anyone.

📊 VIEW YOUR DASHBOARD:
{dashboard_link}

💡 Bookmark this link for easy access!

⚡ Quick Setup (2 minutes):
{setup_link}

What happens next:
1. Click the setup button above - Opens your personalized setup page
2. Enter your Kraken API credentials - Takes 30 seconds
3. Your agent starts automatically - Begins following trading signals
4. Track performance on your dashboard - View profits in real-time

🔖 FOR FUTURE ACCESS:
Bookmark your dashboard link above, or use our login page:
{login_link}

Just enter your API key to access your dashboard from any device!

Questions? Need help? Contact us anytime.

---
$NIKEPIG's Massive Rocket - Automated Kraken Futures Trading
You're receiving this email because you signed up at {BASE_URL}
    """
    
    try:
        response = requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "from": FROM_EMAIL,
                "to": [to_email],
                "subject": "🚀 Your $NIKEPIG's Massive Rocket API Key",
                "html": html_content,
                "text": text_content
            }
        )
        
        if response.status_code == 200:
            print(f"✅ Welcome email sent to {to_email}")
            return True
        else:
            print(f"❌ Failed to send email: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ Error sending email: {e}")
        return False


def send_api_key_resend_email(to_email: str, api_key: str) -> bool:
    """
    Resend API key to existing user (for "forgot key" scenarios)
    
    Args:
        to_email: User's email address
        api_key: User's existing API key
        
    Returns:
        True if sent successfully, False otherwise
    """
    if not RESEND_API_KEY:
        print("⚠️ RESEND_API_KEY not set - email not sent")
        return False
    
    setup_link = f"{BASE_URL}/setup?key={api_key}"
    dashboard_link = f"{BASE_URL}/dashboard?key={api_key}"
    login_link = f"{BASE_URL}/login"
    
    html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif; background: #f5f5f5;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background: #f5f5f5; padding: 40px 20px;">
        <tr>
            <td align="center">
                <table width="600" cellpadding="0" cellspacing="0" style="max-width: 600px; background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">
                    
                    <tr>
                        <td style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 40px 30px; text-align: center;">
                            <h1 style="margin: 0; color: white; font-size: 32px;">🚀 $NIKEPIG's Massive Rocket</h1>
                            <p style="margin: 8px 0 0 0; color: rgba(255,255,255,0.9);">Your API Key</p>
                        </td>
                    </tr>
                    
                    <tr>
                        <td style="padding: 40px 30px;">
                            <h2 style="color: #667eea; margin: 0 0 15px 0;">Your API Key</h2>
                            <p style="color: #374151; font-size: 14px; margin: 0 0 20px 0;">
                                As requested, here's your $NIKEPIG's Massive Rocket API key:
                            </p>
                            
                            <div style="background: #f9fafb; border: 2px dashed #d1d5db; border-radius: 8px; padding: 20px; margin-bottom: 25px;">
                                <code style="font-family: 'Courier New', monospace; font-size: 14px; color: #667eea; word-break: break-all; display: block; text-align: center;">
                                    {api_key}
                                </code>
                            </div>
                            
                            <div style="background: #fef3c7; border-left: 4px solid #f59e0b; padding: 15px; border-radius: 6px; margin-bottom: 30px;">
                                <p style="margin: 0; color: #92400e; font-size: 13px;">
                                    🔒 <strong>Security Reminder:</strong> Never share your API key with anyone. If you believe your key has been compromised, contact support immediately.
                                </p>
                            </div>
                            
                            <!-- Dashboard Button -->
                            <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom: 20px;">
                                <tr>
                                    <td style="text-align: center;">
                                        <a href="{dashboard_link}" style="display: inline-block; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 16px 40px; border-radius: 10px; text-decoration: none; font-weight: bold; font-size: 16px; box-shadow: 0 4px 12px rgba(102, 126, 234, 0.3); margin: 10px;">
                                            📊 View Dashboard
                                        </a>
                                        <a href="{setup_link}" style="display: inline-block; background: linear-gradient(135deg, #10b981 0%, #059669 100%); color: white; padding: 16px 40px; border-radius: 10px; text-decoration: none; font-weight: bold; font-size: 16px; box-shadow: 0 4px 12px rgba(16, 185, 129, 0.3); margin: 10px;">
                                            ⚙️ Setup Agent
                                        </a>
                                    </td>
                                </tr>
                            </table>
                            
                            <!-- Login Info -->
                            <div style="background: #ede9fe; border-left: 4px solid #7c3aed; padding: 15px; border-radius: 8px; margin-top: 25px;">
                                <p style="margin: 0 0 8px 0; color: #5b21b6; font-size: 14px; font-weight: 600;">
                                    🔖 Access anytime at:
                                </p>
                                <p style="margin: 0; color: #6b21a8; font-size: 13px;">
                                    <a href="{login_link}" style="color: #7c3aed; text-decoration: none; font-weight: 600;">
                                        {login_link}
                                    </a>
                                </p>
                            </div>
                        </td>
                    </tr>
                    
                    <tr>
                        <td style="padding: 20px 30px; background: #f9fafb; text-align: center; border-top: 1px solid #e5e7eb;">
                            <p style="margin: 0; color: #6b7280; font-size: 12px;">
                                If you didn't request this email, please ignore it or contact support.
                            </p>
                        </td>
                    </tr>
                    
                </table>
            </td>
        </tr>
    </table>
</body>
</html>
    """
    
    text_content = f"""
Your Nike Rocket API Key

As requested, here's your API key:

{api_key}

🔒 Security Reminder: Never share your API key with anyone.

View Dashboard: {dashboard_link}
Setup Agent: {setup_link}

🔖 Access anytime at: {login_link}

If you didn't request this email, please ignore it or contact support.
    """
    
    try:
        response = requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "from": FROM_EMAIL,
                "to": [to_email],
                "subject": "Your $NIKEPIG's Massive Rocket API Key",
                "html": html_content,
                "text": text_content
            }
        )
        
        if response.status_code == 200:
            print(f"✅ API key resend email sent to {to_email}")
            return True
        else:
            print(f"❌ Failed to send email: {response.status_code}")
            return False
            
    except Exception as e:
        print(f"❌ Error sending email: {e}")
        return False


# Keep old functions for backward compatibility but mark as deprecated
def send_verification_email(to_email: str, verification_token: str) -> bool:
    """
    DEPRECATED: Use send_welcome_email() instead for hosted agents flow
    """
    print("⚠️ send_verification_email() is deprecated - use send_welcome_email() instead")
    return False


def send_api_key_email(to_email: str, api_key: str) -> bool:
    """
    DEPRECATED: Use send_welcome_email() instead for hosted agents flow
    """
    print("⚠️ send_api_key_email() is deprecated - use send_welcome_email() instead")
    return send_welcome_email(to_email, api_key)


def send_password_reset_email(to_email: str, reset_token: str) -> bool:
    """
    DEPRECATED: Not needed for current flow
    """
    print("⚠️ send_password_reset_email() is deprecated")
    return False
