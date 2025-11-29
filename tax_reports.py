"""
Nike Rocket - Tax & Income Reports
==================================
Generate tax reports for Singapore/Dubai entity compliance.

IMPORTANT: Reports only include ACTUALLY RECEIVED income, not pending/due fees.
This ensures accurate tax reporting - unpaid invoices are not counted as income.

Fee Model: 10% of user's monthly profits (30-day rolling cycle)
- Fees are invoiced via Coinbase Commerce
- Only PAID invoices are counted as income
- Unpaid/expired invoices are excluded from reports

Features:
- Monthly fee income breakdown (paid only)
- Yearly income summary (paid only)
- Per-user fee reports (paid only)
- CSV exports (Xero-compatible)

Author: Nike Rocket Team
Created: November 25, 2025
Updated: November 29, 2025 - Changed to only report actually received payments
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
    Get ACTUAL PAID fee income from billing_invoices table
    
    Only counts invoices where status = 'paid' and paid_at is in the given month.
    This ensures we only report income actually received, not pending/unpaid fees.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get paid invoices for this month
        cur.execute("""
            SELECT 
                bi.user_id,
                fu.email,
                bi.amount_usd as fee_paid,
                bi.cycle_profit as user_profit,
                bi.paid_at,
                bi.coinbase_charge_id
            FROM billing_invoices bi
            JOIN follower_users fu ON fu.id = bi.user_id
            WHERE 
                bi.status = 'paid'
                AND EXTRACT(YEAR FROM bi.paid_at) = %s
                AND EXTRACT(MONTH FROM bi.paid_at) = %s
            ORDER BY fu.email, bi.paid_at
        """, (year, month))
        
        invoices = cur.fetchall()
        
        total_fees = 0.0
        total_profit = 0.0
        breakdown = []
        user_totals = {}  # Aggregate by user in case of multiple payments in month
        
        for invoice in invoices:
            user_id, email, fee_paid, user_profit, paid_at, charge_id = invoice
            
            fee = float(fee_paid or 0)
            profit = float(user_profit or 0)
            
            if email not in user_totals:
                user_totals[email] = {
                    'user_id': user_id,
                    'email': email,
                    'total_fee_paid': 0.0,
                    'total_profit': 0.0,
                    'payment_count': 0,
                    'fee_rate': '10%'
                }
            
            user_totals[email]['total_fee_paid'] += fee
            user_totals[email]['total_profit'] += profit
            user_totals[email]['payment_count'] += 1
            
            total_fees += fee
            total_profit += profit
        
        # Convert to list
        for email, data in user_totals.items():
            breakdown.append({
                'user_id': data['user_id'],
                'email': data['email'],
                'monthly_profit': data['total_profit'],
                'fee_paid': data['total_fee_paid'],
                'payment_count': data['payment_count'],
                'fee_rate': data['fee_rate']
            })
        
        cur.close()
        conn.close()
        
        return {
            'year': year,
            'month': month,
            'month_name': datetime(year, month, 1).strftime('%B'),
            'total_fees_received': total_fees,
            'total_user_profits': total_profit,
            'total_payments': len(invoices),
            'unique_users': len(user_totals),
            'breakdown': breakdown,
            'avg_fee_per_user': total_fees / len(user_totals) if user_totals else 0
        }
        
    except Exception as e:
        print(f"Error getting monthly paid fees: {e}")
        return {
            'year': year,
            'month': month,
            'month_name': datetime(year, month, 1).strftime('%B'),
            'total_fees_received': 0,
            'total_user_profits': 0,
            'total_payments': 0,
            'unique_users': 0,
            'breakdown': [],
            'avg_fee_per_user': 0
        }


def get_yearly_income(year: int) -> Dict:
    """
    Get ACTUAL PAID income summary for entire year
    
    Only counts payments actually received via billing_invoices.
    Returns yearly totals and monthly breakdown.
    """
    monthly_data = []
    yearly_total_fees = 0.0
    yearly_total_profit = 0.0
    yearly_payments = 0
    all_users = set()
    
    for month in range(1, 13):
        month_data = get_monthly_income(year, month)
        monthly_data.append(month_data)
        yearly_total_fees += month_data['total_fees_received']
        yearly_total_profit += month_data['total_user_profits']
        yearly_payments += month_data['total_payments']
        
        # Track unique users across all months
        for item in month_data['breakdown']:
            all_users.add(item['email'])
    
    return {
        'year': year,
        'total_fees_received': yearly_total_fees,
        'total_user_profits': yearly_total_profit,
        'total_payments': yearly_payments,
        'unique_users_year': len(all_users),
        'monthly_breakdown': monthly_data,
        'avg_fee_per_month': yearly_total_fees / 12,
        'avg_fee_per_user': yearly_total_fees / len(all_users) if all_users else 0
    }


def get_user_fees(start_date: str, end_date: str) -> List[Dict]:
    """
    Get per-user PAID fee breakdown for date range
    
    Only counts invoices where status = 'paid'.
    
    Args:
        start_date: 'YYYY-MM-DD'
        end_date: 'YYYY-MM-DD'
    
    Returns list of users with their total fees actually received
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT 
                fu.email,
                fu.api_key,
                fu.fee_tier,
                COUNT(bi.id) as payment_count,
                SUM(bi.cycle_profit) as total_profit,
                SUM(bi.amount_usd) as total_fees_paid,
                MIN(bi.paid_at) as first_payment,
                MAX(bi.paid_at) as last_payment
            FROM billing_invoices bi
            JOIN follower_users fu ON fu.id = bi.user_id
            WHERE 
                bi.status = 'paid'
                AND bi.paid_at BETWEEN %s AND %s
            GROUP BY fu.id, fu.email, fu.api_key, fu.fee_tier
            ORDER BY total_fees_paid DESC
        """, (start_date, end_date))
        
        users = []
        for row in cur.fetchall():
            email, api_key, fee_tier, payment_count, total_profit, total_fees, first_payment, last_payment = row
            
            # Determine fee rate from tier
            fee_rate = '10%'  # default/standard
            if fee_tier == 'team':
                fee_rate = '0%'
            elif fee_tier == 'vip':
                fee_rate = '5%'
            
            users.append({
                'email': email,
                'api_key': api_key[:20] + '...' if api_key else 'N/A',
                'fee_tier': fee_tier or 'standard',
                'payment_count': payment_count,
                'total_profit': float(total_profit or 0),
                'total_fees_paid': float(total_fees or 0),
                'fee_rate': fee_rate,
                'first_payment': first_payment.strftime('%Y-%m-%d') if first_payment else 'N/A',
                'last_payment': last_payment.strftime('%Y-%m-%d') if last_payment else 'N/A',
                'avg_fee_per_payment': float(total_fees or 0) / payment_count if payment_count > 0 else 0
            })
        
        cur.close()
        conn.close()
        
        return users
        
    except Exception as e:
        print(f"Error getting user paid fees: {e}")
        return []


def get_earliest_payment_year() -> int:
    """Get the year of the earliest paid invoice"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT MIN(EXTRACT(YEAR FROM paid_at))
            FROM billing_invoices
            WHERE status = 'paid' AND paid_at IS NOT NULL
        """)
        
        result = cur.fetchone()
        cur.close()
        conn.close()
        
        if result and result[0]:
            return int(result[0])
        else:
            # Default to current year if no payments yet
            from datetime import datetime
            return datetime.now().year
            
    except Exception as e:
        print(f"Error getting earliest payment year: {e}")
        from datetime import datetime
        return datetime.now().year


# Keep old function name as alias for backwards compatibility
def get_earliest_trade_year() -> int:
    """Alias for get_earliest_payment_year (backwards compatibility)"""
    return get_earliest_payment_year()


def generate_monthly_csv(year: int, month: int) -> str:
    """Generate CSV for monthly PAID income (Xero-compatible)"""
    data = get_monthly_income(year, month)
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Xero-compatible format
    writer.writerow([
        'Date',
        'Description',
        'Reference',
        'Amount Received (USD)',
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
            f"Trading fee received - {item['email']}",
            f"{item['payment_count']} payment(s), user profit ${item['monthly_profit']:.2f}",
            f"{item['fee_paid']:.2f}",
            'Trading Fee Income'
        ])
    
    # Summary
    writer.writerow([])
    writer.writerow([
        'TOTAL RECEIVED',
        f"{data['month_name']} {year} Summary",
        f"{data['unique_users']} paying users, {data['total_payments']} payments",
        f"{data['total_fees_received']:.2f}",
        ''
    ])
    writer.writerow([])
    writer.writerow(['Note:', 'Only includes actually received payments (paid invoices)', '', '', ''])
    
    return output.getvalue()


def generate_yearly_csv(year: int) -> str:
    """Generate CSV for yearly PAID income summary"""
    data = get_yearly_income(year)
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow([
        'Month',
        'Fees Received (USD)',
        'User Profits (USD)',
        'Number of Payments',
        'Paying Users',
        'Avg Fee/User (USD)'
    ])
    
    # Monthly data
    for month_data in data['monthly_breakdown']:
        avg_per_user = month_data['total_fees_received'] / month_data['unique_users'] if month_data['unique_users'] > 0 else 0
        writer.writerow([
            f"{month_data['month_name']} {month_data['year']}",
            f"{month_data['total_fees_received']:.2f}",
            f"{month_data['total_user_profits']:.2f}",
            month_data['total_payments'],
            month_data['unique_users'],
            f"{avg_per_user:.2f}"
        ])
    
    # Yearly summary
    writer.writerow([])
    writer.writerow([
        f'TOTAL {year}',
        f"{data['total_fees_received']:.2f}",
        f"{data['total_user_profits']:.2f}",
        data['total_payments'],
        data['unique_users_year'],
        f"{data['avg_fee_per_user']:.2f}"
    ])
    
    writer.writerow([])
    writer.writerow(['Average per month', f"{data['avg_fee_per_month']:.2f}",  '', '', '', ''])
    writer.writerow(['Fee Rate', '10% of 30-day cycle profits', '', '', '', ''])
    writer.writerow([])
    writer.writerow(['Note:', 'Only includes actually received payments (paid invoices)', '', '', '', ''])
    
    return output.getvalue()


def generate_user_fees_csv(start_date: str, end_date: str) -> str:
    """Generate per-user PAID fee breakdown CSV"""
    users = get_user_fees(start_date, end_date)
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow([
        'User Email',
        'Fee Tier',
        'Total Profit (USD)',
        'Fee Rate',
        'Fees Received (USD)',
        'Number of Payments',
        'Avg Fee/Payment (USD)',
        'First Payment',
        'Last Payment',
        'Period'
    ])
    
    # User data
    total_fees = 0
    total_profit = 0
    total_payments = 0
    
    for user in users:
        writer.writerow([
            user['email'],
            user['fee_tier'],
            f"{user['total_profit']:.2f}",
            user['fee_rate'],
            f"{user['total_fees_paid']:.2f}",
            user['payment_count'],
            f"{user['avg_fee_per_payment']:.2f}",
            user['first_payment'],
            user['last_payment'],
            f"{start_date} to {end_date}"
        ])
        total_fees += user['total_fees_paid']
        total_profit += user['total_profit']
        total_payments += user['payment_count']
    
    # Summary
    writer.writerow([])
    writer.writerow([
        f'TOTAL ({len(users)} users)',
        '',
        f"{total_profit:.2f}",
        '',
        f"{total_fees:.2f}",
        total_payments,
        f"{total_fees / total_payments:.2f}" if total_payments > 0 else '0.00',
        start_date,
        end_date,
        ''
    ])
    
    writer.writerow([])
    writer.writerow(['Note:', 'Only includes actually received payments (paid invoices). Unpaid/expired invoices excluded.', '', '', '', '', '', '', '', ''])
    
    return output.getvalue()
