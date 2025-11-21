"""
Email Service - Resend Integration (Updated for Hosted Agents)
==============================================================

Sends emails using Resend API.
Free tier: 3,000 emails/month

Setup:
1. Sign up at https://resend.com
2. Get API key
3. Set RESEND_API_KEY environment variable

Updated: November 21, 2025 - Hosted Agents Flow
"""

import os
import requests
from typing import Optional

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_API_URL = "https://api.resend.com/emails"
FROM_EMAIL = os.getenv("FROM_EMAIL", "Nike Rocket <onboarding@resend.dev>")
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
    
    # Email HTML
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
                background: #f5f5f5;
            }}
            .container {{
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                border-radius: 12px;
                padding: 40px 20px;
                text-align: center;
            }}
            .content {{
                background: white;
                border-radius: 12px;
                padding: 30px;
                margin-top: 20px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.1);
            }}
            .api-key {{
                background: #f0f7ff;
                border: 2px dashed #667eea;
                padding: 15px;
                border-radius: 8px;
                font-family: 'Courier New', monospace;
                font-size: 14px;
                word-break: break-all;
                margin: 20px 0;
                color: #333;
            }}
            .btn {{
                display: inline-block;
                padding: 16px 32px;
                background: linear-gradient(135deg, #10b981 0%, #059669 100%);
                color: white !important;
                text-decoration: none;
                border-radius: 10px;
                font-weight: bold;
                font-size: 16px;
                margin: 25px 0;
                box-shadow: 0 4px 12px rgba(16, 185, 129, 0.3);
            }}
            .info-box {{
                background: #fef3c7;
                border-left: 4px solid #f59e0b;
                padding: 15px;
                border-radius: 8px;
                margin: 20px 0;
                text-align: left;
            }}
            .info-box p {{
                margin: 5px 0;
                color: #92400e;
            }}
            .steps {{
                text-align: left;
                margin: 20px 0;
            }}
            .steps ol {{
                padding-left: 20px;
            }}
            .steps li {{
                margin: 10px 0;
                line-height: 1.8;
            }}
            .footer {{
                margin-top: 30px;
                padding-top: 20px;
                border-top: 1px solid #e5e7eb;
                font-size: 12px;
                color: #666;
                text-align: center;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1 style="color: white; margin: 0; font-size: 36px;">🚀 Nike Rocket</h1>
            <p style="color: white; margin: 10px 0; font-size: 16px;">Welcome to Automated Trading!</p>
        </div>
        
        <div class="content">
            <h2 style="color: #667eea;">Your Account is Ready!</h2>
            <p>Thanks for signing up! Your Nike Rocket account has been created.</p>
            
            <div style="background: #f9fafb; padding: 15px; border-radius: 8px; margin: 20px 0;">
                <p style="margin: 0 0 10px 0;"><strong>Your API Key:</strong></p>
                <div class="api-key">{api_key}</div>
                <p style="font-size: 12px; color: #666; margin: 10px 0 0 0;">
                    💡 Keep this key secure - you'll need it to access your dashboard and trading agent.
                </p>
            </div>
            
            <div class="info-box">
                <p><strong>⚡ Quick Setup (2 minutes):</strong></p>
                <p style="margin-top: 10px;">Click the button below to set up your automated trading agent. No technical skills required!</p>
            </div>
            
            <a href="{setup_link}" class="btn">
                🚀 Setup Your Trading Agent
            </a>
            
            <div class="steps">
                <h3 style="color: #333; font-size: 18px;">What happens next:</h3>
                <ol>
                    <li><strong>Click the setup button above</strong> - Opens your personalized setup page</li>
                    <li><strong>Enter your Kraken API credentials</strong> - Takes 30 seconds</li>
                    <li><strong>Your agent starts automatically</strong> - Begins following trading signals</li>
                    <li><strong>Track performance on your dashboard</strong> - View profits in real-time</li>
                </ol>
            </div>
            
            <div style="background: #f0f7ff; padding: 15px; border-radius: 8px; margin: 20px 0; text-align: left;">
                <p style="margin: 0 0 10px 0;"><strong style="color: #667eea;">📊 View Your Dashboard:</strong></p>
                <p style="margin: 0; font-size: 14px; color: #666;">
                    <a href="{BASE_URL}/dashboard?key={api_key}" style="color: #667eea; text-decoration: none;">
                        {BASE_URL}/dashboard?key={api_key}
                    </a>
                </p>
            </div>
            
            <div class="footer">
                <p><strong>Need help?</strong> Visit our <a href="{BASE_URL}/signup" style="color: #667eea;">signup page</a> for detailed setup instructions.</p>
                <p style="margin-top: 10px;">Having issues? Reply to this email for support.</p>
            </div>
        </div>
        
        <div style="text-align: center; margin-top: 20px; font-size: 12px; color: #999;">
            <p>Nike Rocket - Automated Kraken Futures Trading</p>
            <p>You're receiving this email because you signed up at {BASE_URL}</p>
        </div>
    </body>
    </html>
    """
    
    # Plain text version
    text_content = f"""
Welcome to Nike Rocket!

Your account is ready! Here's your API key:

{api_key}

💡 Keep this key secure - you'll need it to access your dashboard and trading agent.

⚡ QUICK SETUP (2 minutes):
Click this link to set up your trading agent:
{setup_link}

What happens next:
1. Click the setup link above
2. Enter your Kraken API credentials (30 seconds)
3. Your agent starts automatically
4. Track performance on your dashboard

View Your Dashboard:
{BASE_URL}/dashboard?key={api_key}

Need help? Visit {BASE_URL}/signup for detailed instructions.

---
Nike Rocket - Automated Kraken Futures Trading
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
                "subject": "🚀 Welcome to Nike Rocket - Your API Key Inside",
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
    
    setup_link = f"{{BASE_URL}}/setup?key={{api_key}}"
    dashboard_link = f"{{BASE_URL}}/dashboard?key={{api_key}}"
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
                background: #f5f5f5;
            }}
            .container {{
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                border-radius: 12px;
                padding: 40px 20px;
                text-align: center;
            }}
            .content {{
                background: white;
                border-radius: 12px;
                padding: 30px;
                margin-top: 20px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.1);
            }}
            .api-key {{
                background: #f0f7ff;
                border: 2px dashed #667eea;
                padding: 15px;
                border-radius: 8px;
                font-family: 'Courier New', monospace;
                font-size: 14px;
                word-break: break-all;
                margin: 20px 0;
                color: #333;
            }}
            .btn {{
                display: inline-block;
                padding: 16px 32px;
                background: linear-gradient(135deg, #667eea 0%, #5568d3 100%);
                color: white !important;
                text-decoration: none;
                border-radius: 10px;
                font-weight: bold;
                font-size: 16px;
                margin: 20px 10px;
                box-shadow: 0 4px 12px rgba(102, 126, 234, 0.3);
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1 style="color: white; margin: 0; font-size: 36px;">🚀 Nike Rocket</h1>
            <p style="color: white; margin: 10px 0;">Your API Key</p>
        </div>
        
        <div class="content">
            <h2 style="color: #667eea;">Your API Key</h2>
            <p>As requested, here's your Nike Rocket API key:</p>
            
            <div class="api-key">{{api_key}}</div>
            
            <div style="background: #fef3c7; border-left: 4px solid #f59e0b; padding: 15px; border-radius: 8px; margin: 20px 0; text-align: left;">
                <p style="margin: 0; color: #92400e;">
                    <strong>🔒 Security Reminder:</strong> Never share your API key with anyone. If you believe your key has been compromised, contact support immediately.
                </p>
            </div>
            
            <div style="margin: 30px 0;">
                <a href="{{dashboard_link}}" class="btn">
                    📊 View Dashboard
                </a>
                <a href="{{setup_link}}" class="btn" style="background: linear-gradient(135deg, #10b981 0%, #059669 100%);">
                    ⚙️ Setup Agent
                </a>
            </div>
            
            <div style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #e5e7eb; font-size: 12px; color: #666; text-align: center;">
                <p>If you didn't request this email, please ignore it or contact support.</p>
            </div>
        </div>
    </body>
    </html>
    """
    
    text_content = f"""
Your Nike Rocket API Key

As requested, here's your API key:

{{api_key}}

🔒 Security Reminder: Never share your API key with anyone.

View Dashboard: {{dashboard_link}}
Setup Agent: {{setup_link}}

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
                "subject": "Your Nike Rocket API Key",
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
