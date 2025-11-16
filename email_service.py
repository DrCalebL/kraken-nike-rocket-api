"""
Email Service - Resend Integration
===================================

Sends verification emails using Resend API.
Free tier: 3,000 emails/month

Setup:
1. Sign up at https://resend.com
2. Get API key
3. Set RESEND_API_KEY environment variable
"""

import os
import requests
from typing import Optional

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_API_URL = "https://api.resend.com/emails"
FROM_EMAIL = os.getenv("FROM_EMAIL", "Nike Rocket <onboarding@resend.dev>")
BASE_URL = os.getenv("BASE_URL", "https://nike-rocket-api-production.up.railway.app")


def send_verification_email(to_email: str, verification_token: str) -> bool:
    """
    Send verification email with API key
    
    Args:
        to_email: User's email address
        verification_token: Unique verification token
        
    Returns:
        True if sent successfully, False otherwise
    """
    if not RESEND_API_KEY:
        print("⚠️ RESEND_API_KEY not set - email not sent")
        print(f"🔗 Verification link (for testing): {BASE_URL}/verify/{verification_token}")
        return False
    
    verification_link = f"{BASE_URL}/verify/{verification_token}"
    
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
            }}
            .container {{
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                border-radius: 10px;
                padding: 40px;
                text-align: center;
            }}
            .content {{
                background: white;
                border-radius: 10px;
                padding: 30px;
                margin-top: 20px;
            }}
            .btn {{
                display: inline-block;
                padding: 15px 30px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                text-decoration: none;
                border-radius: 8px;
                font-weight: bold;
                margin: 20px 0;
            }}
            .footer {{
                margin-top: 30px;
                font-size: 12px;
                color: #666;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1 style="color: white; margin: 0;">🚀 Nike Rocket</h1>
            <p style="color: white; margin: 10px 0;">Automated Trading Signals</p>
        </div>
        
        <div class="content">
            <h2>Verify Your Email</h2>
            <p>Thanks for signing up! Click the button below to verify your email and get your API key.</p>
            
            <a href="{verification_link}" class="btn">Verify Email & Get API Key</a>
            
            <p style="margin-top: 30px; font-size: 14px; color: #666;">
                Or copy this link into your browser:<br>
                <code style="background: #f5f5f5; padding: 5px 10px; border-radius: 4px; display: inline-block; margin-top: 10px;">{verification_link}</code>
            </p>
            
            <div class="footer">
                <p>This link expires in 1 hour.</p>
                <p>If you didn't sign up for Nike Rocket, you can ignore this email.</p>
            </div>
        </div>
    </body>
    </html>
    """
    
    # Plain text version
    text_content = f"""
    Welcome to Nike Rocket!
    
    Thanks for signing up! Click the link below to verify your email and get your API key:
    
    {verification_link}
    
    This link expires in 1 hour.
    
    If you didn't sign up for Nike Rocket, you can ignore this email.
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
                "subject": "Verify your Nike Rocket account",
                "html": html_content,
                "text": text_content
            }
        )
        
        if response.status_code == 200:
            print(f"✅ Verification email sent to {to_email}")
            return True
        else:
            print(f"❌ Failed to send email: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ Error sending email: {e}")
        return False


def send_password_reset_email(to_email: str, reset_token: str) -> bool:
    """
    Send password reset email (future feature)
    
    Args:
        to_email: User's email address
        reset_token: Unique reset token
        
    Returns:
        True if sent successfully, False otherwise
    """
    # TODO: Implement when password feature is added
    pass


def send_api_key_email(to_email: str, api_key: str) -> bool:
    """
    Send API key via email (for secure delivery)
    
    Args:
        to_email: User's email address
        api_key: User's API key
        
    Returns:
        True if sent successfully, False otherwise
    """
    if not RESEND_API_KEY:
        print("⚠️ RESEND_API_KEY not set - email not sent")
        return False
    
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
            }}
            .container {{
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                border-radius: 10px;
                padding: 40px;
                text-align: center;
            }}
            .content {{
                background: white;
                border-radius: 10px;
                padding: 30px;
                margin-top: 20px;
            }}
            .api-key {{
                background: #f0f7ff;
                border: 2px dashed #667eea;
                padding: 15px;
                border-radius: 8px;
                font-family: 'Courier New', monospace;
                font-size: 16px;
                word-break: break-all;
                margin: 20px 0;
            }}
            .warning {{
                background: #fee2e2;
                border-left: 4px solid #ef4444;
                padding: 15px;
                border-radius: 4px;
                margin: 20px 0;
                text-align: left;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1 style="color: white; margin: 0;">🚀 Nike Rocket</h1>
            <p style="color: white; margin: 10px 0;">Your API Key</p>
        </div>
        
        <div class="content">
            <h2>Welcome to Nike Rocket!</h2>
            <p>Your account has been verified. Here's your API key:</p>
            
            <div class="api-key">{api_key}</div>
            
            <div class="warning">
                <strong>⚠️ IMPORTANT:</strong> Save this key securely! You won't be able to retrieve it later.
                We recommend using a password manager like 1Password, LastPass, or Bitwarden.
            </div>
            
            <div style="background: #fef3c7; border-left: 4px solid #f59e0b; padding: 15px; border-radius: 4px; margin: 20px 0; text-align: left;">
                <strong style="color: #92400e;">⚠️ BEFORE YOU DEPLOY:</strong>
                <p style="margin: 10px 0 5px 0; color: #92400e;">Complete these steps first (see signup page for details):</p>
                <ol style="margin: 5px 0 0 20px; color: #92400e; line-height: 1.8;">
                    <li><strong>Activate Kraken Futures</strong> (one-time setup)</li>
                    <li><strong>Create Futures API Keys</strong> (with correct permissions)</li>
                    <li><strong>Fund Your Futures Wallet</strong> ($500+ recommended)</li>
                </ol>
                <p style="margin: 10px 0 0 0; color: #92400e; font-size: 14px;">
                    📖 Need help? Visit the signup page for step-by-step instructions.
                </p>
            </div>
            
            <h3>Next Steps:</h3>
            <ol style="text-align: left;">
                <li>Save your API key in a secure location ✅</li>
                <li>Complete Kraken setup (Steps 1-3 on <a href="{BASE_URL}/signup" style="color: #667eea;">signup page</a>)</li>
                <li>Click "Deploy to Render" button below</li>
                <li>Enter your Nike Rocket API key when prompted</li>
                <li>Enter your Kraken API credentials</li>
                <li>Start receiving trading signals! 🚀</li>
            </ol>
            
            <p style="margin-top: 30px; text-align: center;">
                <a href="https://render.com/deploy?repo=https://github.com/DrCalebL/kraken-follower-agent" 
                   style="display: inline-block; padding: 15px 30px; background: linear-gradient(135deg, #10b981 0%, #059669 100%); color: white; text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 16px;">
                    🚀 Deploy to Render
                </a>
            </p>
            
            <p style="margin-top: 20px; text-align: center;">
                <a href="{BASE_URL}/signup" style="color: #667eea; text-decoration: none; font-size: 14px;">
                    Back to Nike Rocket →
                </a>
            </p>
        </div>
    </body>
    </html>
    """
    
    text_content = f"""
    Welcome to Nike Rocket!
    
    Your account has been verified. Here's your API key:
    
    {api_key}
    
    IMPORTANT: Save this key securely! You won't be able to retrieve it later.
    
    ⚠️ BEFORE YOU DEPLOY:
    Complete these steps first (see signup page for details):
    1. Activate Kraken Futures (one-time setup)
    2. Create Futures API Keys (with correct permissions)
    3. Fund Your Futures Wallet ($500+ recommended)
    
    Next Steps:
    1. Save your API key ✅
    2. Complete Kraken setup (Steps 1-3 on signup page: {BASE_URL}/signup)
    3. Deploy to Render: https://render.com/deploy?repo=https://github.com/DrCalebL/kraken-follower-agent
    4. Enter your Nike Rocket API key
    5. Enter your Kraken API credentials
    6. Start receiving signals! 🚀
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
            print(f"✅ API key email sent to {to_email}")
            return True
        else:
            print(f"❌ Failed to send email: {response.status_code}")
            return False
            
    except Exception as e:
        print(f"❌ Error sending email: {e}")
        return False
