"""
Nike Rocket - Tax & Income Reports
==================================
Generate tax reports for Singapore/Dubai entity compliance.

Fee Model: 10% of user's monthly profits only
- Only charge on positive monthly P&L
- Calculate at month-end
- Track in follower_users.monthly_fee_due

Features:
- Monthly fee income breakdown
- Yearly income summary
- Per-user fee reports
- CSV exports (Xero-compatible)
- PDF summary reports

Author: Nike Rocket Team
Created: November 25, 2025
"""

import os
import psycopg2
from datetime import datetime, timedelta
from typing import List, Dict
import csv
import io

DATABASE_URL = os.getenv("DATABASE_URL")


def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def get_monthly_income(year: int, month: int) -> Dict:
    """
    Get actual historical fee data from trades table
    
    Calculates 10% fee on monthly profits (positive P&L only)
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Calculate fees per user from actual trade history
        cur.execute("""
            WITH monthly_user_profits AS (
                SELECT 
                    t.user_id,
                    fu.email,
                    SUM(t.profit_usd) as monthly_profit,
                    COUNT(t.id) as trade_count
                FROM trades t
                JOIN follower_users fu ON t.user_id = fu.id
                WHERE 
                    EXTRACT(YEAR FROM t.closed_at) = %s
                    AND EXTRACT(MONTH FROM t.closed_at) = %s
                    AND t.closed_at IS NOT NULL
                GROUP BY t.user_id, fu.email
                HAVING SUM(t.profit_usd) > 0
            )
            SELECT 
                user_id,
                email,
                monthly_profit,
                trade_count,
                monthly_profit * 0.10 as fee_due
            FROM monthly_user_profits
            ORDER BY email
        """, (year, month))
        
        users = cur.fetchall()
        
        total_fees = 0.0
        total_profit = 0.0
        total_trades = 0
        breakdown = []
        
        for user in users:
            user_id, email, monthly_profit, trade_count, fee_due = user
            
            profit = float(monthly_profit or 0)
            fee = float(fee_due or 0)
            
            total_fees += fee
            total_profit += profit
            total_trades += (trade_count or 0)
            
            breakdown.append({
                'user_id': user_id,
                'email': email,
                'monthly_profit': profit,
                'fee_due': fee,
                'trades_count': trade_count or 0,
                'fee_rate': '10%'
            })
        
        cur.close()
        conn.close()
        
        return {
            'year': year,
            'month': month,
            'month_name': datetime(year, month, 1).strftime('%B'),
            'total_fees': total_fees,
            'total_profit': total_profit,
            'total_trades': total_trades,
            'unique_users': len(users),
            'breakdown': breakdown,
            'avg_fee_per_user': total_fees / len(users) if users else 0
        }
        
    except Exception as e:
        print(f"Error getting historical monthly fees: {e}")
        return {
            'year': year,
            'month': month,
            'month_name': datetime(year, month, 1).strftime('%B'),
            'total_fees': 0,
            'total_profit': 0,
            'total_trades': 0,
            'unique_users': 0,
            'breakdown': [],
            'avg_fee_per_user': 0
        }


def get_yearly_income(year: int) -> Dict:
    """
    Get income summary for entire year
    
    Returns yearly totals and monthly breakdown
    """
    monthly_data = []
    yearly_total_fees = 0.0
    yearly_total_profit = 0.0
    yearly_trades = 0
    all_users = set()
    
    for month in range(1, 13):
        month_data = get_monthly_income(year, month)
        monthly_data.append(month_data)
        yearly_total_fees += month_data['total_fees']
        yearly_total_profit += month_data['total_profit']
        yearly_trades += month_data['total_trades']
        
        # Track unique users across all months
        for item in month_data['breakdown']:
            all_users.add(item['email'])
    
    return {
        'year': year,
        'total_fees': yearly_total_fees,
        'total_profit': yearly_total_profit,
        'total_trades': yearly_trades,
        'unique_users_year': len(all_users),
        'monthly_breakdown': monthly_data,
        'avg_fee_per_month': yearly_total_fees / 12,
        'avg_fee_per_user': yearly_total_fees / len(all_users) if all_users else 0
    }


def get_user_fees(start_date: str, end_date: str) -> List[Dict]:
    """
    Get per-user fee breakdown for date range
    
    Args:
        start_date: 'YYYY-MM-DD'
        end_date: 'YYYY-MM-DD'
    
    Returns list of users with their total fees (10% of profits)
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            WITH user_profits AS (
                SELECT 
                    t.user_id,
                    fu.email,
                    fu.api_key,
                    SUM(t.profit_usd) as total_profit,
                    COUNT(t.id) as trade_count,
                    MIN(t.closed_at) as first_trade,
                    MAX(t.closed_at) as last_trade
                FROM trades t
                JOIN follower_users fu ON t.user_id = fu.id
                WHERE 
                    t.closed_at BETWEEN %s AND %s
                    AND t.closed_at IS NOT NULL
                GROUP BY t.user_id, fu.email, fu.api_key
                HAVING SUM(t.profit_usd) > 0
            )
            SELECT 
                email,
                api_key,
                trade_count,
                total_profit,
                total_profit * 0.10 as total_fees,
                first_trade,
                last_trade
            FROM user_profits
            ORDER BY total_fees DESC
        """, (start_date, end_date))
        
        users = []
        for row in cur.fetchall():
            email, api_key, trade_count, total_profit, total_fees, first_trade, last_trade = row
            
            users.append({
                'email': email,
                'api_key': api_key[:20] + '...' if api_key else 'N/A',
                'trade_count': trade_count,
                'total_profit': float(total_profit),
                'total_fees': float(total_fees),
                'fee_rate': '10%',
                'first_trade': first_trade.strftime('%Y-%m-%d') if first_trade else 'N/A',
                'last_trade': last_trade.strftime('%Y-%m-%d') if last_trade else 'N/A',
                'avg_profit_per_trade': float(total_profit) / trade_count if trade_count > 0 else 0
            })
        
        cur.close()
        conn.close()
        
        return users
        
    except Exception as e:
        print(f"Error getting user fees: {e}")
        return []


def generate_monthly_csv(year: int, month: int) -> str:
    """Generate CSV for monthly income (Xero-compatible)"""
    data = get_monthly_income(year, month)
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Xero-compatible format
    writer.writerow([
        'Date',
        'Description',
        'Reference',
        'Amount (USD)',
        'Account'
    ])
    
    # One line per user
    month_end = datetime(year, month, 1)
    if month == 12:
        month_end = datetime(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = datetime(year, month + 1, 1) - timedelta(days=1)
    
    date_str = month_end.strftime('%Y-%m-%d')
    
    for item in data['breakdown']:
        writer.writerow([
            date_str,
            f"Trading fee - {item['email']}",
            f"{item['trades_count']} trades, profit ${item['monthly_profit']:.2f}",
            f"{item['fee_due']:.2f}",
            'Trading Fee Income'
        ])
    
    # Summary
    writer.writerow([])
    writer.writerow([
        'TOTAL',
        f"{data['month_name']} {year} Summary",
        f"{data['unique_users']} users, {data['total_trades']} trades",
        f"{data['total_fees']:.2f}",
        ''
    ])
    
    return output.getvalue()


def generate_yearly_csv(year: int) -> str:
    """Generate CSV for yearly income summary"""
    data = get_yearly_income(year)
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow([
        'Month',
        'Fees Collected (USD)',
        'User Profits (USD)',
        'Number of Trades',
        'Profitable Users',
        'Avg Fee/User (USD)'
    ])
    
    # Monthly data
    for month_data in data['monthly_breakdown']:
        avg_per_user = month_data['total_fees'] / month_data['unique_users'] if month_data['unique_users'] > 0 else 0
        writer.writerow([
            f"{month_data['month_name']} {month_data['year']}",
            f"{month_data['total_fees']:.2f}",
            f"{month_data['total_profit']:.2f}",
            month_data['total_trades'],
            month_data['unique_users'],
            f"{avg_per_user:.2f}"
        ])
    
    # Yearly summary
    writer.writerow([])
    writer.writerow([
        f'TOTAL {year}',
        f"{data['total_fees']:.2f}",
        f"{data['total_profit']:.2f}",
        data['total_trades'],
        data['unique_users_year'],
        f"{data['avg_fee_per_user']:.2f}"
    ])
    
    writer.writerow([])
    writer.writerow(['Average per month', f"{data['avg_fee_per_month']:.2f}",  '', '', '', ''])
    writer.writerow(['Fee Rate', '10% of monthly profits only', '', '', '', ''])
    
    return output.getvalue()


def generate_user_fees_csv(start_date: str, end_date: str) -> str:
    """Generate per-user fee breakdown CSV"""
    users = get_user_fees(start_date, end_date)
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow([
        'User Email',
        'Total Profit (USD)',
        'Fee Rate',
        'Fees Owed (USD)',
        'Number of Trades',
        'Avg Profit/Trade (USD)',
        'First Trade',
        'Last Trade',
        'Period'
    ])
    
    # User data
    total_fees = 0
    total_profit = 0
    total_trades = 0
    
    for user in users:
        writer.writerow([
            user['email'],
            f"{user['total_profit']:.2f}",
            user['fee_rate'],
            f"{user['total_fees']:.2f}",
            user['trade_count'],
            f"{user['avg_profit_per_trade']:.2f}",
            user['first_trade'],
            user['last_trade'],
            f"{start_date} to {end_date}"
        ])
        total_fees += user['total_fees']
        total_profit += user['total_profit']
        total_trades += user['trade_count']
    
    # Summary
    writer.writerow([])
    writer.writerow([
        f'TOTAL ({len(users)} users)',
        f"{total_profit:.2f}",
        '10%',
        f"{total_fees:.2f}",
        total_trades,
        f"{total_profit / total_trades:.2f}" if total_trades > 0 else '0.00',
        start_date,
        end_date,
        ''
    ])
    
    writer.writerow([])
    writer.writerow(['Note:', 'Fees calculated as 10% of positive monthly profits only', '', '', '', '', '', '', ''])
    
    return output.getvalue()
