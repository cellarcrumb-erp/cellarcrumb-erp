from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from database import get_db
from auth import login_required, require_permission
import datetime as _dt

overview_bp = Blueprint('overview', __name__, url_prefix='/overview')

@overview_bp.route('/')
@login_required
@require_permission('overview', 'read')
def dashboard():
    conn = get_db()
    try:
        active_branch_id = session.get('active_branch_id', 0)
        
        branch_sales_clause = " AND branch_id = ?" if active_branch_id > 0 else ""
        branch_purch_clause = " AND branch_id = ?" if active_branch_id > 0 else ""
        b_params = [active_branch_id] if active_branch_id > 0 else []

        # Total Sales (Posted/Paid Invoices)
        total_sales = 0.0
        try:
            r_sales = conn.execute(f"""
                SELECT COALESCE(SUM(total), 0) as val 
                FROM sales_docs 
                WHERE doc_type='Invoice' AND status IN ('Posted', 'Paid', 'Partial') {branch_sales_clause}
            """, b_params).fetchone()
            total_sales = float(r_sales['val']) if r_sales and r_sales['val'] is not None else 0.0
        except Exception as e:
            print(f"[Dashboard] Error calculating total_sales: {e}")

        # Total Purchases
        total_purchases = 0.0
        try:
            r_purch = conn.execute(f"""
                SELECT COALESCE(SUM(total), 0) as val 
                FROM purchase_docs 
                WHERE doc_type IN ('Invoice', 'Bill') AND status IN ('Posted', 'Paid', 'Partial') {branch_purch_clause}
            """, b_params).fetchone()
            total_purchases = float(r_purch['val']) if r_purch and r_purch['val'] is not None else 0.0
        except Exception as e:
            print(f"[Dashboard] Error calculating total_purchases: {e}")

        # Open Receivables
        open_receivables = 0.0
        try:
            r_ar = conn.execute(f"""
                SELECT COALESCE(SUM(balance_due), 0) as val 
                FROM sales_docs 
                WHERE doc_type='Invoice' AND status IN ('Posted', 'Partial') {branch_sales_clause}
            """, b_params).fetchone()
            open_receivables = float(r_ar['val']) if r_ar and r_ar['val'] is not None else 0.0
        except Exception as e:
            print(f"[Dashboard] Error calculating open_receivables: {e}")

        # Open Payables
        open_payables = 0.0
        try:
            r_ap = conn.execute(f"""
                SELECT COALESCE(SUM(balance_due), 0) as val 
                FROM purchase_docs 
                WHERE doc_type IN ('Invoice', 'Bill') AND status IN ('Posted', 'Partial') {branch_purch_clause}
            """, b_params).fetchone()
            open_payables = float(r_ap['val']) if r_ap and r_ap['val'] is not None else 0.0
        except Exception as e:
            print(f"[Dashboard] Error calculating open_payables: {e}")

        # Stock Value
        stock_value = 0.0
        try:
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
            stock_value = float(r_stock['val']) if r_stock and r_stock['val'] is not None else 0.0
        except Exception as e:
            print(f"[Dashboard] Error calculating stock_value: {e}")

        # Low Stock Items count
        low_stock_count = 0
        try:
            r_low = conn.execute("""
                SELECT COUNT(*) as cnt FROM items i
                WHERE i.is_stock_item = TRUE AND i.is_active = TRUE
                AND (SELECT COALESCE(SUM(sb.quantity), 0) FROM stock_balances sb WHERE sb.item_id = i.id) <= i.reorder_level
            """).fetchone()
            low_stock_count = int(r_low['cnt']) if r_low and r_low['cnt'] is not None else 0
        except Exception as e:
            print(f"[Dashboard] Error calculating low_stock_count: {e}")

        # Cash Balance
        cash_balance = 0.0
        try:
            r_cash = conn.execute("SELECT COALESCE(SUM(current_balance), 0) as val FROM bank_accounts WHERE is_active = TRUE").fetchone()
            cash_balance = float(r_cash['val']) if r_cash and r_cash['val'] is not None else 0.0
        except Exception as e:
            print(f"[Dashboard] Error calculating cash_balance: {e}")

        # Pending Drafts Count
        pending_count = 0
        try:
            r_pending_s = conn.execute(f"SELECT COUNT(*) as cnt FROM sales_docs WHERE status = 'Draft' {branch_sales_clause}", b_params).fetchone()
            r_pending_p = conn.execute(f"SELECT COUNT(*) as cnt FROM purchase_docs WHERE status = 'Draft' {branch_purch_clause}", b_params).fetchone()
            cnt_s = int(r_pending_s['cnt']) if r_pending_s and r_pending_s['cnt'] is not None else 0
            cnt_p = int(r_pending_p['cnt']) if r_pending_p and r_pending_p['cnt'] is not None else 0
            pending_count = cnt_s + cnt_p
        except Exception as e:
            print(f"[Dashboard] Error calculating pending_count: {e}")

        # Recent transactions with type casting for PostgreSQL UNION ALL compatibility
        recent_txns = []
        try:
            recent_txns = conn.execute(f"""
                SELECT 'Sales Invoice' as doc_type, CAST(doc_ref AS TEXT) as doc_ref, CAST(doc_date AS TEXT) as doc_date, CAST(total AS REAL) as total, CAST(status AS TEXT) as status 
                FROM sales_docs WHERE doc_type='Invoice' {branch_sales_clause}
                UNION ALL
                SELECT 'Customer Payment' as doc_type, CAST(doc_ref AS TEXT) as doc_ref, CAST(doc_date AS TEXT) as doc_date, CAST(amount AS REAL) as total, CAST(status AS TEXT) as status 
                FROM payments WHERE payment_type='Incoming' {branch_sales_clause}
                UNION ALL
                SELECT 'Supplier Bill' as doc_type, CAST(doc_ref AS TEXT) as doc_ref, CAST(doc_date AS TEXT) as doc_date, CAST(total AS REAL) as total, CAST(status AS TEXT) as status 
                FROM purchase_docs WHERE doc_type IN ('Invoice', 'Bill') {branch_purch_clause}
                ORDER BY doc_date DESC LIMIT 8
            """, b_params * 3 if active_branch_id > 0 else []).fetchall()
        except Exception as e:
            print(f"[Dashboard] Error fetching recent_txns (trying safe fallback): {e}")
            try:
                recent_txns = conn.execute("""
                    SELECT 'Sales Invoice' as doc_type, CAST(doc_ref AS TEXT) as doc_ref, CAST(doc_date AS TEXT) as doc_date, CAST(total AS REAL) as total, CAST(status AS TEXT) as status 
                    FROM sales_docs WHERE doc_type='Invoice'
                    UNION ALL
                    SELECT 'Supplier Bill' as doc_type, CAST(doc_ref AS TEXT) as doc_ref, CAST(doc_date AS TEXT) as doc_date, CAST(total AS REAL) as total, CAST(status AS TEXT) as status 
                    FROM purchase_docs WHERE doc_type IN ('Invoice', 'Bill')
                    ORDER BY doc_date DESC LIMIT 8
                """).fetchall()
            except Exception as e2:
                print(f"[Dashboard] Fallback recent_txns also failed: {e2}")
                recent_txns = []

        # Low Stock Items table
        low_stock_items = []
        try:
            low_stock_items = conn.execute("""
                SELECT i.code, i.name, COALESCE(SUM(sb.quantity), 0) as stock_qty, i.reorder_level
                FROM items i
                LEFT JOIN stock_balances sb ON i.id = sb.item_id
                WHERE i.is_stock_item = TRUE AND i.is_active = TRUE
                GROUP BY i.id, i.code, i.name, i.reorder_level
                HAVING COALESCE(SUM(sb.quantity), 0) <= i.reorder_level
                LIMIT 6
            """).fetchall()
        except Exception as e:
            print(f"[Dashboard] Error fetching low_stock_items: {e}")
            low_stock_items = []

        # Chart data (Monthly sales vs purchases — current year)
        cur_year = _dt.date.today().year
        chart_labels = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        chart_sales = [0.0] * 12
        chart_purchases = [0.0] * 12

        try:
            sales_rows = conn.execute(f"""
                SELECT doc_date, total
                FROM sales_docs
                WHERE doc_type='Invoice' AND status IN ('Posted','Paid','Partial')
                  AND CAST(doc_date AS TEXT) >= ? AND CAST(doc_date AS TEXT) <= ? {branch_sales_clause}
            """, [f"{cur_year}-01-01", f"{cur_year}-12-31"] + b_params).fetchall()
            for r in sales_rows:
                d = r['doc_date']
                m = (d.month - 1) if hasattr(d, 'month') else (int(str(d).split('-')[1]) - 1)
                if 0 <= m < 12:
                    chart_sales[m] = round(chart_sales[m] + float(r['total'] or 0), 2)
        except Exception as e:
            print(f"[Dashboard] Error compiling chart_sales: {e}")

        try:
            purch_rows = conn.execute(f"""
                SELECT doc_date, total
                FROM purchase_docs
                WHERE doc_type IN ('Invoice','Bill') AND status IN ('Posted','Paid','Partial')
                  AND CAST(doc_date AS TEXT) >= ? AND CAST(doc_date AS TEXT) <= ? {branch_purch_clause}
            """, [f"{cur_year}-01-01", f"{cur_year}-12-31"] + b_params).fetchall()
            for r in purch_rows:
                d = r['doc_date']
                m = (d.month - 1) if hasattr(d, 'month') else (int(str(d).split('-')[1]) - 1)
                if 0 <= m < 12:
                    chart_purchases[m] = round(chart_purchases[m] + float(r['total'] or 0), 2)
        except Exception as e:
            print(f"[Dashboard] Error compiling chart_purchases: {e}")

        return render_template('overview/dashboard.html',
                               total_sales=f"{total_sales:,.2f}",
                               total_purchases=f"{total_purchases:,.2f}",
                               stock_value=f"{stock_value:,.2f}",
                               low_stock_count=low_stock_count,
                               open_receivables=f"{open_receivables:,.2f}",
                               open_payables=f"{open_payables:,.2f}",
                               cash_balance=f"{cash_balance:,.2f}",
                               pending_count=pending_count,
                               recent_txns=recent_txns,
                               recent_transactions=recent_txns,
                               low_stock_items=low_stock_items,
                               chart_labels=chart_labels,
                               chart_sales=chart_sales,
                               chart_purchases=chart_purchases)
    finally:
        try:
            conn.close()
        except Exception:
            pass
