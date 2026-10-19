from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from functools import wraps
from database import get_db

overview_bp = Blueprint('overview', __name__, url_prefix='/overview')

from auth import login_required, require_permission

@overview_bp.route('/')
@login_required
@require_permission('overview', 'read')
def dashboard():
    conn = get_db()
    active_branch_id = session.get('active_branch_id', 0)
    
    branch_sales_clause = " AND branch_id = ?" if active_branch_id > 0 else ""
    branch_purch_clause = " AND branch_id = ?" if active_branch_id > 0 else ""
    b_params = [active_branch_id] if active_branch_id > 0 else []

    # Total Sales (Posted/Paid Invoices)
    r_sales = conn.execute(f"""
        SELECT COALESCE(SUM(total), 0) as val 
        FROM sales_docs 
        WHERE doc_type='Invoice' AND status IN ('Posted', 'Paid', 'Partial') {branch_sales_clause}
    """, b_params).fetchone()
    total_sales = r_sales['val'] if r_sales else 0

    # Total Purchases
    r_purch = conn.execute(f"""
        SELECT COALESCE(SUM(total), 0) as val 
        FROM purchase_docs 
        WHERE doc_type IN ('Invoice', 'Bill') AND status IN ('Posted', 'Paid', 'Partial') {branch_purch_clause}
    """, b_params).fetchone()
    total_purchases = r_purch['val'] if r_purch else 0

    # Open Receivables
    r_ar = conn.execute(f"""
        SELECT COALESCE(SUM(balance_due), 0) as val 
        FROM sales_docs 
        WHERE doc_type='Invoice' AND status IN ('Posted', 'Partial') {branch_sales_clause}
    """, b_params).fetchone()
    open_receivables = r_ar['val'] if r_ar else 0

    # Open Payables
    r_ap = conn.execute(f"""
        SELECT COALESCE(SUM(balance_due), 0) as val 
        FROM purchase_docs 
        WHERE doc_type IN ('Invoice', 'Bill') AND status IN ('Posted', 'Partial') {branch_purch_clause}
    """, b_params).fetchone()
    open_payables = r_ap['val'] if r_ap else 0

    # Stock Value
    if active_branch_id > 0:
        r_stock = conn.execute("""
            SELECT COALESCE(SUM(sb.quantity * i.cost), 0) as val
            FROM stock_balances sb
            JOIN items i ON sb.item_id = i.id
            JOIN warehouses w ON sb.warehouse_id = w.id
            WHERE w.branch_id = ?
        """, (active_branch_id,)).fetchone()
    else:
        r_stock = conn.execute("""
            SELECT COALESCE(SUM(sb.quantity * i.cost), 0) as val
            FROM stock_balances sb
            JOIN items i ON sb.item_id = i.id
        """).fetchone()
    stock_value = r_stock['val'] if r_stock else 0

    # Low Stock Items
    r_low = conn.execute("""
        SELECT COUNT(*) as cnt FROM items i
        WHERE i.is_stock_item = 1 AND i.is_active = 1
        AND (SELECT COALESCE(SUM(sb.quantity), 0) FROM stock_balances sb WHERE sb.item_id = i.id) <= i.reorder_level
    """).fetchone()
    low_stock_count = r_low['cnt'] if r_low else 0

    # Cash Balance
    r_cash = conn.execute("SELECT COALESCE(SUM(current_balance), 0) as val FROM bank_accounts WHERE is_active = 1").fetchone()
    cash_balance = r_cash['val'] if r_cash else 0

    # Pending Drafts Count
    r_pending_s = conn.execute(f"SELECT COUNT(*) as cnt FROM sales_docs WHERE status = 'Draft' {branch_sales_clause}", b_params).fetchone()
    r_pending_p = conn.execute(f"SELECT COUNT(*) as cnt FROM purchase_docs WHERE status = 'Draft' {branch_purch_clause}", b_params).fetchone()
    pending_count = (r_pending_s['cnt'] if r_pending_s else 0) + (r_pending_p['cnt'] if r_pending_p else 0)

    # Recent transactions
    recent_txns = conn.execute(f"""
        SELECT 'Sales Invoice' as type, doc_ref as ref, doc_date as date, total as amount, status 
        FROM sales_docs WHERE doc_type='Invoice' {branch_sales_clause}
        UNION ALL
        SELECT 'Customer Payment' as type, doc_ref as ref, doc_date as date, amount, status 
        FROM payments WHERE payment_type='Incoming' {branch_sales_clause}
        UNION ALL
        SELECT 'Supplier Bill' as type, doc_ref as ref, doc_date as date, total as amount, status 
        FROM purchase_docs WHERE doc_type IN ('Invoice', 'Bill') {branch_purch_clause}
        ORDER BY date DESC LIMIT 8
    """, b_params * 3 if active_branch_id > 0 else []).fetchall()

    # Low Stock Items table
    low_stock_items = conn.execute("""
        SELECT i.code, i.name, COALESCE(SUM(sb.quantity), 0) as stock, i.reorder_level
        FROM items i
        LEFT JOIN stock_balances sb ON i.id = sb.item_id
        WHERE i.is_stock_item = 1 AND i.is_active = 1
        GROUP BY i.id
        HAVING stock <= i.reorder_level
        LIMIT 6
    """).fetchall()

    # Chart data (Monthly sales vs purchases)
    chart_labels = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    chart_sales = [0] * 12
    chart_purchases = [0] * 12
    chart_sales[7] = round(total_sales, 2)  # Current month August
    chart_purchases[7] = round(total_purchases, 2)

    return render_template('overview/dashboard.html',
                           total_sales=total_sales,
                           total_purchases=total_purchases,
                           stock_value=stock_value,
                           low_stock_count=low_stock_count,
                           open_receivables=open_receivables,
                           open_payables=open_payables,
                           cash_balance=cash_balance,
                           pending_count=pending_count,
                           recent_transactions=recent_txns,
                           low_stock_items=low_stock_items,
                           chart_labels=chart_labels,
                           chart_sales=chart_sales,
                           chart_purchases=chart_purchases)

