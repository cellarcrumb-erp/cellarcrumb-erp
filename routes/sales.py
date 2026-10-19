import json
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from functools import wraps
from database import get_db, next_ref, update_stock, post_journal, log_audit
from datetime import datetime, date

sales_bp = Blueprint('sales', __name__, url_prefix='/sales')

from auth import login_required, require_permission

DOC_TYPES = {
    'quotations': 'Quotation',
    'orders': 'Sales Order',
    'deliveries': 'Delivery',
    'invoices': 'Invoice',
    'returns': 'Sales Return'
}

DOC_KIND_FROM_TYPE = {v: k for k, v in DOC_TYPES.items()}

# Allowed forward transitions: Current Doc Type -> Valid Target Doc Types
COPY_TO_MAP = {
    'Quotation': ['Sales Order'],
    'Sales Order': ['Delivery', 'Invoice'],
    'Delivery': ['Invoice'],
    'Invoice': ['Sales Return'],
    'Sales Return': []
}

# Allowed backward transitions: Current Doc Type -> Valid Source Doc Types
COPY_FROM_MAP = {
    'Quotation': [],
    'Sales Order': ['Quotation'],
    'Delivery': ['Sales Order'],
    'Invoice': ['Delivery', 'Sales Order'],
    'Sales Return': ['Invoice']
}

@sales_bp.route('/')
@login_required
@require_permission('sales', 'read')
def index():
    conn = get_db()
    counts = {}
    for kind, doc_type in DOC_TYPES.items():
        row = conn.execute("SELECT COUNT(*) as cnt, COALESCE(SUM(total), 0) as total FROM sales_docs WHERE doc_type = ?", (doc_type,)).fetchone()
        counts[kind] = {'count': row['cnt'], 'total': row['total']}
    conn.close()
    return render_template('sales/index.html', counts=counts)

# --- SAP COPY FROM / TO API ENDPOINTS ---

@sales_bp.route('/api/eligible-sources')
@login_required
def api_eligible_sources():
    target_type = request.args.get('target_type', '')
    customer_id = request.args.get('customer_id', type=int)
    
    valid_source_types = COPY_FROM_MAP.get(target_type, [])
    if not valid_source_types:
        return jsonify({'sources': []})
        
    conn = get_db()
    placeholders = ','.join(['?'] * len(valid_source_types))
    query = f'''
        SELECT d.id, d.doc_ref, d.doc_date, d.doc_type, d.customer_id, d.status, d.total,
               p.name as customer_name, w.name as warehouse_name,
               (SELECT COUNT(*) FROM sales_lines l WHERE l.doc_id = d.id AND (l.quantity - COALESCE(l.copied_qty, 0)) > 0) as open_lines_count,
               (SELECT SUM((l.quantity - COALESCE(l.copied_qty, 0)) * l.unit_price) FROM sales_lines l WHERE l.doc_id = d.id) as open_amount
        FROM sales_docs d
        LEFT JOIN partners p ON d.customer_id = p.id
        LEFT JOIN warehouses w ON d.warehouse_id = w.id
        WHERE d.doc_type IN ({placeholders})
          AND d.status NOT IN ('Cancelled', 'Closed')
    '''
    params = list(valid_source_types)
    if customer_id:
        query += ' AND d.customer_id = ?'
        params.append(customer_id)
        
    query += ' ORDER BY d.doc_date DESC, d.id DESC'
    
    rows = conn.execute(query, params).fetchall()
    sources = []
    for r in rows:
        if (r['open_lines_count'] or 0) > 0:
            sources.append({
                'id': r['id'],
                'doc_ref': r['doc_ref'],
                'doc_date': r['doc_date'],
                'doc_type': r['doc_type'],
                'doc_kind': DOC_KIND_FROM_TYPE.get(r['doc_type'], 'orders'),
                'customer_id': r['customer_id'],
                'customer_name': r['customer_name'] or '',
                'warehouse_name': r['warehouse_name'] or '',
                'status': r['status'],
                'total': float(r['total'] or 0),
                'open_amount': float(r['open_amount'] or 0),
                'open_lines_count': r['open_lines_count']
            })
    return jsonify({'sources': sources, 'valid_source_types': valid_source_types})

@sales_bp.route('/api/source-lines')
@login_required
def api_source_lines():
    doc_id = request.args.get('doc_id', type=int)
    if not doc_id:
        return jsonify({'error': 'Missing doc_id'}), 400
        
    conn = get_db()
    doc = conn.execute('''
        SELECT d.*, p.name as customer_name, w.name as warehouse_name
        FROM sales_docs d
        LEFT JOIN partners p ON d.customer_id = p.id
        LEFT JOIN warehouses w ON d.warehouse_id = w.id
        WHERE d.id = ?
    ''', (doc_id,)).fetchone()
    
    if not doc:
        return jsonify({'error': 'Document not found'}), 404
        
    lines = conn.execute('''
        SELECT l.*, i.name as item_name, i.code as item_code,
               (l.quantity - COALESCE(l.copied_qty, 0)) as open_qty
        FROM sales_lines l
        LEFT JOIN items i ON l.item_id = i.id
        WHERE l.doc_id = ?
        ORDER BY l.id
    ''', (doc_id,)).fetchall()
    
    lines_data = []
    for l in lines:
        open_qty = max(0.0, float(l['open_qty'] or 0.0))
        lines_data.append({
            'id': l['id'],
            'item_id': l['item_id'],
            'item_code': l['item_code'] or '',
            'item_name': l['item_name'] or '',
            'description': l['description'] or '',
            'quantity': float(l['quantity'] or 0),
            'copied_qty': float(l['copied_qty'] or 0),
            'open_qty': open_qty,
            'unit_price': float(l['unit_price'] or 0),
            'discount_pct': float(l['discount_pct'] or 0),
            'tax_id': l['tax_id'],
            'tax_amount': float(l['tax_amount'] or 0),
            'line_total': float(l['line_total'] or 0)
        })
        
    return jsonify({
        'header': {
            'id': doc['id'],
            'doc_ref': doc['doc_ref'],
            'doc_type': doc['doc_type'],
            'doc_date': doc['doc_date'],
            'customer_id': doc['customer_id'],
            'customer_name': doc['customer_name'] or '',
            'warehouse_id': doc['warehouse_id'],
            'warehouse_name': doc['warehouse_name'] or '',
            'remarks': doc['remarks'] or ''
        },
        'lines': lines_data
    })

# --- DOCUMENT LIST & DETAILS ---

@sales_bp.route('/<doc_kind>')
@login_required
@require_permission('sales', 'read')
def doc_list(doc_kind):
    if doc_kind not in DOC_TYPES:
        return "Not found", 404
    doc_type = DOC_TYPES[doc_kind]
    
    conn = get_db()
    status = request.args.get('status', '')
    customer_id = request.args.get('customer_id', '')
    branch_id_param = request.args.get('branch_id', type=int)
    
    # Active branch filter
    active_branch_id = session.get('active_branch_id', 0)
    effective_branch_id = branch_id_param if branch_id_param is not None else (active_branch_id if active_branch_id > 0 else None)
    
    query = """
        SELECT d.*, p.name as customer_name, b.name as branch_name, b.code as branch_code,
               (SELECT COUNT(*) FROM sales_lines l WHERE l.doc_id = d.id AND (l.quantity - COALESCE(l.copied_qty, 0)) > 0) as open_lines_count
        FROM sales_docs d 
        LEFT JOIN partners p ON d.customer_id = p.id 
        LEFT JOIN branches b ON d.branch_id = b.id
        WHERE d.doc_type = ?
    """
    params = [doc_type]
    
    if status:
        query += " AND d.status = ?"
        params.append(status)
    if customer_id:
        query += " AND d.customer_id = ?"
        params.append(customer_id)
    if effective_branch_id:
        query += " AND d.branch_id = ?"
        params.append(effective_branch_id)
        
    query += " ORDER BY d.doc_date DESC, d.id DESC"
    docs = conn.execute(query, params).fetchall()
    customers = conn.execute("SELECT id, code, name FROM partners WHERE partner_type IN ('Customer', 'Customer & Supplier') ORDER BY name").fetchall()
    branches = conn.execute("SELECT id, code, name FROM branches WHERE is_active=1 ORDER BY is_main DESC, name ASC").fetchall()
    
    return render_template('sales/doc_list.html', docs=docs, doc_kind=doc_kind, doc_type=doc_type,
                           customers=customers, branches=branches, current_branch_id=effective_branch_id)

@sales_bp.route('/<doc_kind>/new', methods=['GET', 'POST'])
@login_required
@require_permission('sales', 'write')
def doc_new(doc_kind):
    if doc_kind not in DOC_TYPES:
        return "Not found", 404
    doc_type = DOC_TYPES[doc_kind]
    conn = get_db()
    
    if request.method == 'POST':
        customer_id = request.form.get('customer_id')
        doc_date = request.form.get('doc_date', date.today().isoformat())
        due_date = request.form.get('due_date') or None
        warehouse_id = request.form.get('warehouse_id') or None
        branch_id = request.form.get('branch_id', type=int) or session.get('active_branch_id') or None
        remarks = request.form.get('remarks', '')
        source_doc_id = request.form.get('source_doc_id', type=int) or None
        source_doc_type = request.form.get('source_doc_type') or None
        source_doc_ref = request.form.get('source_doc_ref') or None
        
        item_ids = request.form.getlist('item_id[]')
        quantities = request.form.getlist('quantity[]')
        prices = request.form.getlist('unit_price[]')
        discounts = request.form.getlist('discount_pct[]')
        tax_ids = request.form.getlist('tax_id[]')
        descriptions = request.form.getlist('description[]')
        source_line_ids = request.form.getlist('source_line_id[]')
        
        if not customer_id or not item_ids:
            flash('Customer and at least one item line are required.', 'error')
            return redirect(request.url)
            
        doc_ref = next_ref('Sales', doc_type, conn)
        cursor = conn.cursor()
        
        # Validate copied quantities against source lines open balance
        for i in range(len(item_ids)):
            if not item_ids[i]: continue
            s_line_id = source_line_ids[i] if i < len(source_line_ids) and source_line_ids[i] else None
            if s_line_id:
                s_line = conn.execute('SELECT quantity, copied_qty FROM sales_lines WHERE id = ?', (s_line_id,)).fetchone()
                if s_line:
                    avail_open = max(0.0, s_line['quantity'] - (s_line['copied_qty'] or 0.0))
                    req_qty = float(quantities[i]) if quantities[i] else 0.0
                    if req_qty > (avail_open + 0.0001):
                        flash(f'Cannot copy quantity {req_qty:,.2f} exceeding remaining open quantity {avail_open:,.2f}.', 'error')
                        return redirect(request.url)
                        
        subtotal = 0.0
        tax_total = 0.0
        grand_total = 0.0
        
        cursor.execute("""
            INSERT INTO sales_docs 
            (doc_ref, doc_type, doc_date, due_date, customer_id, warehouse_id, branch_id, status, subtotal, tax_total, total, balance_due, remarks, source_doc_id, source_doc_type, source_doc_ref, posted_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'Draft', 0, 0, 0, 0, ?, ?, ?, ?, ?)
        """, (doc_ref, doc_type, doc_date, due_date, customer_id, warehouse_id, branch_id, remarks, source_doc_id, source_doc_type, source_doc_ref, session.get('user_id')))
        doc_id = cursor.lastrowid
        
        for i in range(len(item_ids)):
            if not item_ids[i]: continue
            qty = float(quantities[i]) if quantities[i] else 0.0
            price = float(prices[i]) if prices[i] else 0.0
            disc = float(discounts[i]) if i < len(discounts) and discounts[i] else 0.0
            tax = int(tax_ids[i]) if i < len(tax_ids) and tax_ids[i] else None
            desc = descriptions[i] if i < len(descriptions) else ''
            s_line_id = int(source_line_ids[i]) if i < len(source_line_ids) and source_line_ids[i] else None
            
            line_net = qty * price * (1.0 - disc / 100.0)
            subtotal += line_net
            tax_amt = 0.0
            
            cursor.execute("""
                INSERT INTO sales_lines
                (doc_id, item_id, description, quantity, unit_price, discount_pct, tax_id, tax_amount, line_total, cost_price, copied_qty, source_doc_type, source_doc_id, source_line_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
            """, (doc_id, item_ids[i], desc, qty, price, disc, tax, tax_amt, line_net + tax_amt, source_doc_type, source_doc_id, s_line_id))
            
            # Atomically increment copied_qty on the source line
            if s_line_id:
                cursor.execute('UPDATE sales_lines SET copied_qty = COALESCE(copied_qty, 0) + ? WHERE id = ?', (qty, s_line_id))
                
            grand_total += (line_net + tax_amt)
            
        cursor.execute("""
            UPDATE sales_docs 
            SET subtotal = ?, tax_total = ?, total = ?, balance_due = ?
            WHERE id = ?
        """, (subtotal, tax_total, grand_total, grand_total, doc_id))
        
        conn.commit()
        log_audit(conn, session.get('user_id'), 'Sales', 'Create', doc_type, doc_id, f"Created {doc_type} {doc_ref}" + (f" copied from {source_doc_type} {source_doc_ref}" if source_doc_ref else ""))
        flash(f"{doc_type} {doc_ref} created successfully.", "success")
        return redirect(url_for('sales.doc_detail', doc_kind=doc_kind, id=doc_id))
        
    customers = conn.execute("SELECT id, code, name FROM partners WHERE partner_type IN ('Customer', 'Customer & Supplier') ORDER BY name").fetchall()
    warehouses = conn.execute("SELECT id, name FROM warehouses WHERE is_active=1").fetchall()
    items = conn.execute("SELECT id, code, name, price, cost FROM items WHERE is_active=1").fetchall()
    items_json = {row['id']: dict(row) for row in items}
    
    # Check pre-population from Copy To query params
    copy_from_id = request.args.get('copy_from_id', type=int)
    copy_from_type = request.args.get('copy_from_type', '')
    prefilled_doc = None
    prefilled_lines = []
    
    if copy_from_id and copy_from_type:
        src = conn.execute('SELECT * FROM sales_docs WHERE id = ?', (copy_from_id,)).fetchone()
        if src:
            prefilled_doc = dict(src)
            src_lines = conn.execute('''
                SELECT l.*, i.name as item_name, i.code as item_code,
                       (l.quantity - COALESCE(l.copied_qty, 0)) as open_qty
                FROM sales_lines l
                LEFT JOIN items i ON l.item_id = i.id
                WHERE l.doc_id = ? AND (l.quantity - COALESCE(l.copied_qty, 0)) > 0
            ''', (copy_from_id,)).fetchall()
            prefilled_lines = [dict(sl) for sl in src_lines]
            
    valid_source_types = COPY_FROM_MAP.get(doc_type, [])
    branches = conn.execute("SELECT * FROM branches WHERE is_active=1 ORDER BY is_main DESC, name ASC").fetchall()
    
    return render_template('sales/doc_form.html', doc_kind=doc_kind, doc_type=doc_type, 
                           customers=customers, warehouses=warehouses, items=items,
                           items_json=json.dumps(items_json), doc=prefilled_doc,
                           prefilled_lines=prefilled_lines, copy_from_id=copy_from_id,
                           copy_from_type=copy_from_type, valid_source_types=valid_source_types,
                           branches=branches)

@sales_bp.route('/<doc_kind>/<int:id>')
@login_required
@require_permission('sales', 'read')
def doc_detail(doc_kind, id):
    if doc_kind not in DOC_TYPES:
        return "Not found", 404
    doc_type = DOC_TYPES[doc_kind]
    conn = get_db()
    doc = conn.execute("""
        SELECT d.*, p.name as customer_name, w.name as warehouse_name,
               b.name as branch_name, b.code as branch_code
        FROM sales_docs d
        LEFT JOIN partners p ON d.customer_id = p.id
        LEFT JOIN warehouses w ON d.warehouse_id = w.id
        LEFT JOIN branches b ON d.branch_id = b.id
        WHERE d.id = ?
    """, (id,)).fetchone()
    
    if not doc:
        return "Not found", 404
        
    active_branch_id = session.get('active_branch_id', 0)
    role = session.get('role', '')
    if active_branch_id > 0 and doc['branch_id'] and doc['branch_id'] != active_branch_id:
        if role not in ['Admin', 'Manager']:
            flash('Access Denied: This document belongs to a different branch.', 'error')
            return redirect(url_for('sales.doc_list', doc_kind=doc_kind))
        
    lines = conn.execute("""
        SELECT l.*, i.name as item_name, i.code as item_code,
               (l.quantity - COALESCE(l.copied_qty, 0)) as open_qty
        FROM sales_lines l
        LEFT JOIN items i ON l.item_id = i.id
        WHERE l.doc_id = ?
    """, (id,)).fetchall()
    
    # 1. Base Documents (Upstream - Copied From)
    base_docs = []
    if doc['source_doc_id']:
        b = conn.execute('''
            SELECT id, doc_ref, doc_date, doc_type, status, total 
            FROM sales_docs WHERE id = ?
        ''', (doc['source_doc_id'],)).fetchone()
        if b:
            base_docs.append(dict(b))
            
    # 2. Target Documents (Downstream - Copied To)
    target_docs = conn.execute('''
        SELECT DISTINCT d.id, d.doc_ref, d.doc_date, d.doc_type, d.status, d.total
        FROM sales_docs d
        JOIN sales_lines l ON l.doc_id = d.id
        WHERE l.source_doc_id = ? OR d.source_doc_id = ?
        ORDER BY d.doc_date, d.id
    ''', (id, id)).fetchall()
    
    # Valid Copy To next document options
    valid_copy_to_types = COPY_TO_MAP.get(doc['doc_type'], [])
    valid_copy_to = []
    for ct in valid_copy_to_types:
        target_kind = DOC_KIND_FROM_TYPE.get(ct)
        if target_kind:
            valid_copy_to.append({'type': ct, 'kind': target_kind})
            
    total_open_qty = sum(max(0.0, l['open_qty']) for l in lines)
    is_fully_copied = (total_open_qty <= 0.0001 and len(lines) > 0)

    # For Invoice detail: pass payment options so quick-receive modal can render
    payment_methods = []
    bank_accounts   = []
    if doc_type == 'Invoice':
        payment_methods = conn.execute('SELECT * FROM payment_methods WHERE is_active=1').fetchall()
        bank_accounts   = conn.execute('SELECT * FROM bank_accounts WHERE is_active=1').fetchall()

    return render_template('sales/doc_detail.html', doc=doc, lines=lines, doc_kind=doc_kind,
                           base_docs=base_docs, target_docs=target_docs,
                           valid_copy_to=valid_copy_to, total_open_qty=total_open_qty,
                           is_fully_copied=is_fully_copied, doc_kind_map=DOC_KIND_FROM_TYPE,
                           payment_methods=payment_methods, bank_accounts=bank_accounts)

@sales_bp.route('/<doc_kind>/<int:id>/post', methods=['POST'])
@login_required
def doc_post(doc_kind, id):
    conn = get_db()
    doc = conn.execute("SELECT * FROM sales_docs WHERE id = ?", (id,)).fetchone()
    if not doc or doc['status'] != 'Draft':
        flash("Can only post Draft documents", "error")
        return redirect(url_for('sales.doc_detail', doc_kind=doc_kind, id=id))
        
    doc_type = doc['doc_type']
    lines = conn.execute("SELECT * FROM sales_lines WHERE doc_id = ?", (id,)).fetchall()
    
    if doc_type == 'Delivery':
        # Stock verification & reduction
        for line in lines:
            bal = conn.execute('SELECT quantity FROM stock_balances WHERE item_id=? AND warehouse_id=?',
                               (line['item_id'], doc['warehouse_id'])).fetchone()
            curr_stock = bal['quantity'] if bal else 0
            if curr_stock < line['quantity']:
                item = conn.execute('SELECT name FROM items WHERE id=?', (line['item_id'],)).fetchone()
                flash(f"Insufficient stock for '{item['name']}' in selected warehouse. Available: {curr_stock}, Required: {line['quantity']}", 'error')
                return redirect(url_for('sales.doc_detail', doc_kind=doc_kind, id=id))
                
        for line in lines:
            item = conn.execute('SELECT cost FROM items WHERE id=?', (line['item_id'],)).fetchone()
            cost = item['cost'] if item else 0
            update_stock(conn, line['item_id'], doc['warehouse_id'], 0, line['quantity'], doc_type, id, doc['doc_ref'], cost, session.get('user_id'))
    
    elif doc_type == 'Invoice':
        ar_acc = conn.execute("SELECT id FROM coa WHERE code='1200'").fetchone()
        rev_acc = conn.execute("SELECT id FROM coa WHERE code='4100'").fetchone()
        tax_acc = conn.execute("SELECT id FROM coa WHERE code='2210'").fetchone()
        cogs_acc = conn.execute("SELECT id FROM coa WHERE code='5100'").fetchone()
        inv_acc = conn.execute("SELECT id FROM coa WHERE code='1300'").fetchone()

        ar_id = ar_acc['id'] if ar_acc else 1
        rev_id = rev_acc['id'] if rev_acc else 2
        tax_id = tax_acc['id'] if tax_acc else 3
        
        j_lines = [
            (ar_id, doc['customer_id'], doc['total'], 0, f"Sales Invoice {doc['doc_ref']}"),
            (rev_id, doc['customer_id'], 0, doc['subtotal'], f"Revenue {doc['doc_ref']}")
        ]
        if doc['tax_total'] > 0:
            j_lines.append((tax_id, None, 0, doc['tax_total'], f"Tax {doc['doc_ref']}"))
        
        post_journal(conn, doc['doc_ref'], doc['doc_date'], 'Sales', doc_type, id, f"Invoice {doc['doc_ref']}", j_lines, session.get('user_id'), branch_id=doc['branch_id'])
        conn.execute("UPDATE partners SET balance = balance + ? WHERE id = ?", (doc['total'], doc['customer_id']))

        # --- Stock movement: only if invoice was NOT copied from a Delivery note ---
        # If a Delivery (DN) was already posted, stock was moved then. Avoid double-deduction.
        already_delivered = False
        if doc['source_doc_id'] and doc['source_doc_type'] == 'Delivery':
            src = conn.execute("SELECT status FROM sales_docs WHERE id=?", (doc['source_doc_id'],)).fetchone()
            if src and src['status'] == 'Posted':
                already_delivered = True
        
        if not already_delivered and doc['warehouse_id']:
            # Verify stock first
            for line in lines:
                item_row = conn.execute('SELECT * FROM items WHERE id=?', (line['item_id'],)).fetchone()
                if not item_row or not item_row['is_stock_item']:
                    continue  # Skip non-stock items (services, etc.)
                bal = conn.execute('SELECT quantity FROM stock_balances WHERE item_id=? AND warehouse_id=?',
                                   (line['item_id'], doc['warehouse_id'])).fetchone()
                curr_stock = bal['quantity'] if bal else 0
                if curr_stock < line['quantity']:
                    flash(f"Insufficient stock for '{item_row['name']}'. Available: {curr_stock}, Required: {line['quantity']}. "
                          f"Please create a Delivery Note first, or receive stock.", 'error')
                    return redirect(url_for('sales.doc_detail', doc_kind=doc_kind, id=id))
            
            # Deduct stock
            for line in lines:
                item_row = conn.execute('SELECT * FROM items WHERE id=?', (line['item_id'],)).fetchone()
                if not item_row or not item_row['is_stock_item']:
                    continue
                cost = item_row['cost'] if item_row else 0
                update_stock(conn, line['item_id'], doc['warehouse_id'], 0, line['quantity'],
                             'Invoice', id, doc['doc_ref'], cost, session.get('user_id'))
                # Post COGS journal entry
                if cogs_acc and inv_acc:
                    total_cost = line['quantity'] * cost
                    cogs_lines = [
                        (cogs_acc['id'], None, total_cost, 0, f"COGS {doc['doc_ref']} - {item_row['name']}"),
                        (inv_acc['id'], None, 0, total_cost, f"Inventory reduction {doc['doc_ref']}")
                    ]
                    post_journal(conn, doc['doc_ref'] + '-COGS', doc['doc_date'], 'Sales', 'Invoice-COGS', id,
                                 f"COGS {doc['doc_ref']}", cogs_lines, session.get('user_id'), branch_id=doc['branch_id'])
        
    elif doc_type == 'Sales Return':
        # Re-stock items and credit AR
        for line in lines:
            item = conn.execute('SELECT cost FROM items WHERE id=?', (line['item_id'],)).fetchone()
            cost = item['cost'] if item else 0
            update_stock(conn, line['item_id'], doc['warehouse_id'], line['quantity'], 0, doc_type, id, doc['doc_ref'], cost, session.get('user_id'))
            
        ar_acc = conn.execute("SELECT id FROM coa WHERE code='1200'").fetchone()
        rev_acc = conn.execute("SELECT id FROM coa WHERE code='4100'").fetchone()
        ar_id = ar_acc['id'] if ar_acc else 1
        rev_id = rev_acc['id'] if rev_acc else 2
        
        j_lines = [
            (rev_id, doc['customer_id'], doc['subtotal'], 0, f"Sales Return Reversal {doc['doc_ref']}"),
            (ar_id, doc['customer_id'], 0, doc['total'], f"Credit AR {doc['doc_ref']}")
        ]
        post_journal(conn, doc['doc_ref'], doc['doc_date'], 'Sales', doc_type, id, f"Sales Return {doc['doc_ref']}", j_lines, session.get('user_id'), branch_id=doc['branch_id'])
        conn.execute("UPDATE partners SET balance = balance - ? WHERE id = ?", (doc['total'], doc['customer_id']))
        
    conn.execute("UPDATE sales_docs SET status = 'Posted', posted_at = datetime('now') WHERE id = ?", (id,))
    conn.commit()
    log_audit(conn, session.get('user_id'), 'Sales', 'Post', doc_type, id, f"Posted {doc_type} {doc['doc_ref']}")
    flash(f"{doc_type} posted successfully.", "success")
    return redirect(url_for('sales.doc_detail', doc_kind=doc_kind, id=id))

@sales_bp.route('/<doc_kind>/<int:id>/print')
@login_required
def doc_print(doc_kind, id):
    conn = get_db()
    doc = conn.execute("""
        SELECT d.*, p.name as customer_name, p.address as customer_address, p.phone as customer_phone
        FROM sales_docs d
        LEFT JOIN partners p ON d.customer_id = p.id
        WHERE d.id = ?
    """, (id,)).fetchone()
    
    lines = conn.execute("""
        SELECT l.*, i.name as item_name, i.code as item_code
        FROM sales_lines l
        LEFT JOIN items i ON l.item_id = i.id
        WHERE l.doc_id = ?
    """, (id,)).fetchall()
    
    company = conn.execute("SELECT * FROM company WHERE id = 1").fetchone()
    return render_template('sales/invoice_print.html', doc=doc, lines=lines, company=company)


# ── Invoice + Payment: quick receive payment directly from invoice detail ──
@sales_bp.route('/invoices/<int:id>/receive_payment', methods=['POST'])
@login_required
def receive_payment(id):
    """Create, post, and allocate an incoming payment for a posted invoice in one step."""
    conn = get_db()
    doc = conn.execute("SELECT * FROM sales_docs WHERE id=?", (id,)).fetchone()
    if not doc or doc['doc_type'] != 'Invoice' or doc['status'] not in ('Posted', 'Partial'):
        flash('Payment can only be received for a Posted or Partial invoice.', 'error')
        return redirect(url_for('sales.doc_detail', doc_kind='invoices', id=id))

    amount      = request.form.get('amount', 0, type=float)
    method_id   = request.form.get('payment_method_id')
    bank_acc_id = request.form.get('bank_account_id')
    pay_date    = request.form.get('doc_date', date.today().isoformat())
    reference   = request.form.get('reference', '')

    if amount <= 0:
        flash('Please enter a valid payment amount.', 'error')
        return redirect(url_for('sales.doc_detail', doc_kind='invoices', id=id))

    max_receivable = doc['balance_due']
    if amount > max_receivable + 0.001:
        flash(f'Amount exceeds balance due (KES {max_receivable:,.2f}).', 'error')
        return redirect(url_for('sales.doc_detail', doc_kind='invoices', id=id))

    try:
        # ── 1. Create payment record ─────────────────────────────────────────
        pay_ref = next_ref('Payments', 'Incoming', conn)
        cursor = conn.execute('''
            INSERT INTO payments (doc_ref, doc_date, payment_type, partner_id, bank_account_id,
                                  payment_method_id, amount, amount_allocated, unallocated,
                                  reference, remarks, status, branch_id, posted_by)
            VALUES (?, ?, 'Incoming', ?, ?, ?, ?, 0, ?, ?, ?, 'Draft', ?, ?)
        ''', (pay_ref, pay_date, doc['customer_id'], bank_acc_id, method_id,
              amount, amount,
              reference,
              f"Payment for invoice {doc['doc_ref']}",
              doc['branch_id'],
              session.get('user_id')))
        pay_id = cursor.lastrowid

        # ── 2. Post payment — journal + bank balance + partner balance ────────
        bank_acc = conn.execute('SELECT * FROM bank_accounts WHERE id=?', (bank_acc_id,)).fetchone()
        bank_coa_id = (bank_acc['coa_id'] if bank_acc and bank_acc['coa_id']
                       else conn.execute("SELECT id FROM coa WHERE code='1110'").fetchone()[0])
        ar_coa_id = conn.execute("SELECT id FROM coa WHERE code='1200'").fetchone()[0]

        jlines = [
            (bank_coa_id, doc['customer_id'], amount, 0,      f"Customer receipt {pay_ref}"),
            (ar_coa_id,   doc['customer_id'], 0,      amount, f"AR reduction {pay_ref}"),
        ]
        je_id = post_journal(conn, pay_ref, pay_date, 'Payments', 'Incoming Payment', pay_id,
                             f"Payment for {doc['doc_ref']}", jlines, session.get('user_id'), branch_id=doc['branch_id'])

        if bank_acc_id:
            conn.execute('UPDATE bank_accounts SET current_balance = current_balance + ? WHERE id=?',
                         (amount, bank_acc_id))
            conn.execute('''
                INSERT INTO bank_transactions
                  (doc_ref, doc_date, bank_account_id, txn_type, amount, description, reference, partner_id, journal_id)
                VALUES (?,?,?,?,?,?,?,?,?)
            ''', (pay_ref, pay_date, bank_acc_id, 'Deposit', amount,
                  f"Customer Payment {pay_ref}", reference, doc['customer_id'], je_id))

        # Reduce partner AR balance
        conn.execute('UPDATE partners SET balance = balance - ? WHERE id=?',
                     (amount, doc['customer_id']))

        conn.execute("UPDATE payments SET status='Posted', journal_id=?, posted_at=datetime('now') WHERE id=?",
                     (je_id, pay_id))

        # ── 3. Allocate payment against this invoice ──────────────────────────
        apply_amt = min(amount, doc['balance_due'])
        conn.execute('''
            INSERT INTO payment_allocations (payment_id, doc_type, doc_id, amount)
            VALUES (?, 'Invoice', ?, ?)
        ''', (pay_id, id, apply_amt))

        new_paid   = (doc['amount_paid'] or 0.0) + apply_amt
        new_due    = max(0.0, (doc['balance_due'] or 0.0) - apply_amt)
        new_status = 'Paid' if new_due <= 0.001 else 'Partial'
        conn.execute('UPDATE sales_docs SET amount_paid=?, balance_due=?, status=? WHERE id=?',
                     (new_paid, new_due, new_status, id))

        conn.execute('UPDATE payments SET amount_allocated=?, unallocated=? WHERE id=?',
                     (apply_amt, max(0, amount - apply_amt), pay_id))

        conn.commit()
        log_audit(conn, session.get('user_id'), 'Sales', 'Receive Payment',
                  'Invoice', id, f"Received KES {amount:,.2f} via {pay_ref}")
        flash(f'Payment {pay_ref} of KES {amount:,.2f} received and allocated. Invoice status: {new_status}.', 'success')

    except Exception as e:
        conn.rollback()
        flash(f'Error processing payment: {e}', 'error')

    return redirect(url_for('sales.doc_detail', doc_kind='invoices', id=id))
