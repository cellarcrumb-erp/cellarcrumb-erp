from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from database import get_db, sync_document_statuses_and_balances
from auth import login_required, require_permission
import datetime as _dt

overview_bp = Blueprint('overview', __name__, url_prefix='/overview')

@overview_bp.route('/')
@login_required
@require_permission('overview', 'read')
def dashboard():
    conn = get_db()
    try:
        sync_document_statuses_and_balances(conn)
        active_branch_id = session.get('active_branch_id', 0)
        
        branch_sales_clause = " AND branch_id = ?" if active_branch_id > 0 else ""
        branch_purch_clause = " AND branch_id = ?" if active_branch_id > 0 else ""
        b_params = [active_branch_id] if active_branch_id > 0 else []

        # 1. Total Net Sales (Posted/Paid/Partial Invoices minus Posted Sales Returns)
        total_sales = 0.0
        try:
            r_sales = conn.execute(f"""
                SELECT COALESCE(SUM(total), 0) as val 
                FROM sales_docs 
                WHERE doc_type='Invoice' AND status IN ('Posted', 'Paid', 'Partial') {branch_sales_clause}
            """, b_params).fetchone()
            r_sret = conn.execute(f"""
                SELECT COALESCE(SUM(total), 0) as val 
                FROM sales_docs 
                WHERE doc_type='Sales Return' AND status IN ('Posted', 'Paid', 'Closed') {branch_sales_clause}
            """, b_params).fetchone()
            gross_sales = float(r_sales['val']) if r_sales and r_sales['val'] is not None else 0.0
            sales_returns = float(r_sret['val']) if r_sret and r_sret['val'] is not None else 0.0
            total_sales = max(0.0, gross_sales - sales_returns)
        except Exception as e:
            print(f"[Dashboard] Error calculating total_sales: {e}")

        # 2. Total Net Purchases (Posted/Paid/Partial Bills minus Posted Purchase Returns)
        total_purchases = 0.0
        try:
            r_purch = conn.execute(f"""
                SELECT COALESCE(SUM(total), 0) as val 
                FROM purchase_docs 
                WHERE doc_type IN ('Invoice', 'Bill') AND status IN ('Posted', 'Paid', 'Partial') {branch_purch_clause}
            """, b_params).fetchone()
            r_pret = conn.execute(f"""
                SELECT COALESCE(SUM(total), 0) as val 
                FROM purchase_docs 
                WHERE doc_type='Purchase Return' AND status IN ('Posted', 'Paid', 'Closed') {branch_purch_clause}
            """, b_params).fetchone()
            gross_purch = float(r_purch['val']) if r_purch and r_purch['val'] is not None else 0.0
            purch_returns = float(r_pret['val']) if r_pret and r_pret['val'] is not None else 0.0
            total_purchases = max(0.0, gross_purch - purch_returns)
        except Exception as e:
            print(f"[Dashboard] Error calculating total_purchases: {e}")

        # 3. Open Receivables (Tallies with Sales Cockpit & Finance AR)
        open_receivables = 0.0
        try:
            r_ar = conn.execute(f"""
                SELECT COALESCE(SUM(balance_due), 0) as val 
                FROM sales_docs 
                WHERE doc_type IN ('Invoice', 'Down Payment Invoice')
                  AND status IN ('Posted', 'Partial')
                  AND COALESCE(balance_due, 0) > 0.005
                  {branch_sales_clause}
            """, b_params).fetchone()
            open_receivables = float(r_ar['val']) if r_ar and r_ar['val'] is not None else 0.0
        except Exception as e:
            print(f"[Dashboard] Error calculating open_receivables: {e}")

        # 4. Open Payables (Tallies with Purchases Cockpit & Finance AP)
        open_payables = 0.0
        try:
            r_ap = conn.execute(f"""
                SELECT COALESCE(SUM(balance_due), 0) as val 
                FROM purchase_docs 
                WHERE doc_type IN ('Invoice', 'Bill', 'Down Payment Invoice')
                  AND status IN ('Posted', 'Partial')
                  AND COALESCE(balance_due, 0) > 0.005
                  {branch_purch_clause}
            """, b_params).fetchone()
            open_payables = float(r_ap['val']) if r_ap and r_ap['val'] is not None else 0.0
        except Exception as e:
            print(f"[Dashboard] Error calculating open_payables: {e}")

        # 5. Stock Value (Tallies with Inventory Dashboard)
        stock_value = 0.0
        try:
            if active_branch_id > 0:
                r_stock = conn.execute("""
                    SELECT COALESCE(SUM(sb.quantity * i.cost), 0) as val
                    FROM stock_balances sb
                    JOIN items i ON sb.item_id = i.id
                    LEFT JOIN warehouses w ON sb.warehouse_id = w.id
                    WHERE i.is_stock_item = TRUE AND i.is_active = TRUE
                      AND (w.branch_id = ? OR w.branch_id IS NULL)
                """, (active_branch_id,)).fetchone()
            else:
                r_stock = conn.execute("""
                    SELECT COALESCE(SUM(sb.quantity * i.cost), 0) as val
                    FROM stock_balances sb
                    JOIN items i ON sb.item_id = i.id
                    WHERE i.is_stock_item = TRUE AND i.is_active = TRUE
                """).fetchone()
            stock_value = float(r_stock['val']) if r_stock and r_stock['val'] is not None else 0.0
        except Exception as e:
            print(f"[Dashboard] Error calculating stock_value: {e}")

        # 6. Low Stock Items count (Tallies with Inventory Dashboard)
        low_stock_count = 0
        try:
            r_low = conn.execute("""
                SELECT COUNT(*) as cnt FROM items i
                WHERE i.is_stock_item = TRUE AND i.is_active = TRUE
                  AND COALESCE(i.reorder_level, 0) > 0
                  AND (SELECT COALESCE(SUM(sb.quantity), 0) FROM stock_balances sb WHERE sb.item_id = i.id) <= i.reorder_level
            """).fetchone()
            low_stock_count = int(r_low['cnt']) if r_low and r_low['cnt'] is not None else 0
        except Exception as e:
            print(f"[Dashboard] Error calculating low_stock_count: {e}")

        # 7. Cash & Bank Balance (Tallies with Banking Accounts)
        cash_balance = 0.0
        try:
            r_cash = conn.execute("SELECT COALESCE(SUM(current_balance), 0) as val FROM bank_accounts WHERE is_active = TRUE").fetchone()
            cash_balance = float(r_cash['val']) if r_cash and r_cash['val'] is not None else 0.0
        except Exception as e:
            print(f"[Dashboard] Error calculating cash_balance: {e}")

        # 8. Pending Approvals / Open Drafts Count & Action Items
        pending_count = 0
        draft_action_items = []
        try:
            r_pending_s = conn.execute(f"SELECT COUNT(*) as cnt FROM sales_docs WHERE status = 'Draft' {branch_sales_clause}", b_params).fetchone()
            r_pending_p = conn.execute(f"SELECT COUNT(*) as cnt FROM purchase_docs WHERE status = 'Draft' {branch_purch_clause}", b_params).fetchone()
            cnt_s = int(r_pending_s['cnt']) if r_pending_s and r_pending_s['cnt'] is not None else 0
            cnt_p = int(r_pending_p['cnt']) if r_pending_p and r_pending_p['cnt'] is not None else 0
            pending_count = cnt_s + cnt_p

            draft_sales = conn.execute(f"""
                SELECT 'sales' as module, id, doc_type, doc_ref, doc_date, total,
                       (SELECT name FROM partners WHERE id = sales_docs.customer_id) as party_name
                FROM sales_docs 
                WHERE status = 'Draft' {branch_sales_clause}
                ORDER BY id DESC LIMIT 4
            """, b_params).fetchall()
            draft_purch = conn.execute(f"""
                SELECT 'purchases' as module, id, doc_type, doc_ref, doc_date, total,
                       (SELECT name FROM partners WHERE id = purchase_docs.supplier_id) as party_name
                FROM purchase_docs 
                WHERE status = 'Draft' {branch_purch_clause}
                ORDER BY id DESC LIMIT 4
            """, b_params).fetchall()
            draft_action_items = [dict(d) for d in draft_sales] + [dict(d) for d in draft_purch]
        except Exception as e:
            print(f"[Dashboard] Error calculating pending_count: {e}")

        # 9. ERPNext & SAP B1 Document Workflow Pipeline Stages
        pipeline = {
            'quotations': 0,
            'orders': 0,
            'deliveries': 0,
            'invoices': 0,
            'payments': 0
        }
        try:
            p_q = conn.execute(f"SELECT COUNT(*) as cnt FROM sales_docs WHERE doc_type='Quotation' AND status NOT IN ('Cancelled', 'Closed') {branch_sales_clause}", b_params).fetchone()
            p_o = conn.execute(f"SELECT COUNT(*) as cnt FROM sales_docs WHERE doc_type='Sales Order' AND status NOT IN ('Cancelled', 'Closed') {branch_sales_clause}", b_params).fetchone()
            p_d = conn.execute(f"SELECT COUNT(*) as cnt FROM sales_docs WHERE doc_type='Delivery' AND status NOT IN ('Cancelled', 'Closed') {branch_sales_clause}", b_params).fetchone()
            p_i = conn.execute(f"SELECT COUNT(*) as cnt FROM sales_docs WHERE doc_type='Invoice' AND status IN ('Posted', 'Partial') {branch_sales_clause}", b_params).fetchone()
            p_p = conn.execute(f"SELECT COUNT(*) as cnt FROM payments WHERE status NOT IN ('Cancelled', 'Void') {branch_sales_clause}", b_params).fetchone()
            pipeline['quotations'] = int(p_q['cnt'] or 0) if p_q else 0
            pipeline['orders'] = int(p_o['cnt'] or 0) if p_o else 0
            pipeline['deliveries'] = int(p_d['cnt'] or 0) if p_d else 0
            pipeline['invoices'] = int(p_i['cnt'] or 0) if p_i else 0
            pipeline['payments'] = int(p_p['cnt'] or 0) if p_p else 0
        except Exception as e:
            print(f"[Dashboard] Error calculating pipeline: {e}")

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
                WHERE i.is_stock_item = TRUE AND i.is_active = TRUE AND COALESCE(i.reorder_level, 0) > 0
                GROUP BY i.id, i.code, i.name, i.reorder_level
                HAVING COALESCE(SUM(sb.quantity), 0) <= i.reorder_level
                LIMIT 6
            """).fetchall()
        except Exception as e:
            print(f"[Dashboard] Error fetching low_stock_items: {e}")
            low_stock_items = []

        # Chart data (Monthly net sales vs net purchases — current year)
        cur_year = _dt.date.today().year
        chart_labels = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        chart_sales = [0.0] * 12
        chart_purchases = [0.0] * 12

        try:
            sales_rows = conn.execute(f"""
                SELECT doc_date, doc_type, total
                FROM sales_docs
                WHERE ((doc_type='Invoice' AND status IN ('Posted','Paid','Partial'))
                    OR (doc_type='Sales Return' AND status IN ('Posted','Paid','Closed')))
                  AND CAST(doc_date AS TEXT) >= ? AND CAST(doc_date AS TEXT) <= ? {branch_sales_clause}
            """, [f"{cur_year}-01-01", f"{cur_year}-12-31"] + b_params).fetchall()
            for r in sales_rows:
                d = r['doc_date']
                m = (d.month - 1) if hasattr(d, 'month') else (int(str(d).split('-')[1]) - 1)
                if 0 <= m < 12:
                    sign = -1.0 if r['doc_type'] == 'Sales Return' else 1.0
                    chart_sales[m] = round(chart_sales[m] + sign * float(r['total'] or 0), 2)
        except Exception as e:
            print(f"[Dashboard] Error compiling chart_sales: {e}")

        try:
            purch_rows = conn.execute(f"""
                SELECT doc_date, doc_type, total
                FROM purchase_docs
                WHERE ((doc_type IN ('Invoice','Bill') AND status IN ('Posted','Paid','Partial'))
                    OR (doc_type='Purchase Return' AND status IN ('Posted','Paid','Closed')))
                  AND CAST(doc_date AS TEXT) >= ? AND CAST(doc_date AS TEXT) <= ? {branch_purch_clause}
            """, [f"{cur_year}-01-01", f"{cur_year}-12-31"] + b_params).fetchall()
            for r in purch_rows:
                d = r['doc_date']
                m = (d.month - 1) if hasattr(d, 'month') else (int(str(d).split('-')[1]) - 1)
                if 0 <= m < 12:
                    sign = -1.0 if r['doc_type'] == 'Purchase Return' else 1.0
                    chart_purchases[m] = round(chart_purchases[m] + sign * float(r['total'] or 0), 2)
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
                               chart_purchases=chart_purchases,
                               pipeline=pipeline,
                               draft_action_items=draft_action_items)
    finally:
        try:
            conn.close()
        except Exception:
            pass
