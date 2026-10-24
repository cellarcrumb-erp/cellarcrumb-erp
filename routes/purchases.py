import json
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, abort
from functools import wraps
from database import get_db, smart_search_sql, next_ref, update_stock, post_journal, log_audit
from datetime import datetime, date

purchases_bp = Blueprint('purchases', __name__, url_prefix='/purchases')

from auth import login_required, require_permission

DOC_TYPES = {
    'requests': 'Request',
    'quotations': 'Purchase Quotation',
    'orders': 'Purchase Order',
    'receipts': 'Receipt',
    'bills': 'Invoice',
    'down_payments': 'Down Payment Invoice',
    'returns': 'Purchase Return'
}

DOC_KIND_FROM_TYPE = {v: k for k, v in DOC_TYPES.items()}

# Allowed forward transitions: Current Doc Type -> Valid Target Doc Types
COPY_TO_MAP = {
    'Request': ['Purchase Quotation', 'Purchase Order'],
    'Purchase Quotation': ['Purchase Order'],
    'Purchase Order': ['Receipt', 'Invoice', 'Down Payment Invoice'],
    'Receipt': ['Invoice'],
    'Down Payment Invoice': ['Invoice'],
    'Invoice': ['Purchase Return'],
    'Purchase Return': []
}

# Allowed backward transitions: Current Doc Type -> Valid Source Doc Types
COPY_FROM_MAP = {
    'Request': [],
    'Purchase Quotation': ['Request'],
    'Purchase Order': ['Purchase Quotation', 'Request'],
    'Receipt': ['Purchase Order'],
    'Down Payment Invoice': ['Purchase Order'],
    'Invoice': ['Receipt', 'Purchase Order', 'Down Payment Invoice'],
    'Purchase Return': ['Invoice']
}

@purchases_bp.route('/')
@login_required
@require_permission('purchases', 'read')
def index():
    conn = get_db()
    counts = {}
    for kind, doc_type in DOC_TYPES.items():
        row = conn.execute("SELECT COUNT(*) as cnt, COALESCE(SUM(total), 0) as total FROM purchase_docs WHERE doc_type = ?", (doc_type,)).fetchone()
        counts[kind] = {'count': row['cnt'], 'total': row['total']}
    conn.close()
    return render_template('purchases/index.html', counts=counts)

# --- SAP COPY FROM / TO API ENDPOINTS ---

@purchases_bp.route('/api/eligible-sources')
@login_required
def api_eligible_sources():
    target_type = request.args.get('target_type', '')
    supplier_id = request.args.get('supplier_id', type=int)
    
    valid_source_types = COPY_FROM_MAP.get(target_type, [])
    if not valid_source_types:
        return jsonify({'sources': []})
        
    conn = get_db()
    placeholders = ','.join(['?'] * len(valid_source_types))
    query = f'''
        SELECT d.id, d.doc_ref, d.doc_date, d.doc_type, d.supplier_id, d.status, d.total,
               p.name as supplier_name, w.name as warehouse_name,
               (SELECT COUNT(*) FROM purchase_lines l WHERE l.doc_id = d.id AND (l.quantity - COALESCE(l.copied_qty, 0)) > 0) as open_lines_count,
               (SELECT SUM((l.quantity - COALESCE(l.copied_qty, 0)) * l.unit_cost) FROM purchase_lines l WHERE l.doc_id = d.id) as open_amount
        FROM purchase_docs d
        LEFT JOIN partners p ON d.supplier_id = p.id
        LEFT JOIN warehouses w ON d.warehouse_id = w.id
        WHERE d.doc_type IN ({placeholders})
          AND d.status NOT IN ('Cancelled', 'Closed')
    '''
    params = list(valid_source_types)
    if supplier_id:
        query += ' AND d.supplier_id = ?'
        params.append(supplier_id)
        
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
                'supplier_id': r['supplier_id'],
                'supplier_name': r['supplier_name'] or '',
                'warehouse_name': r['warehouse_name'] or '',
                'status': r['status'],
                'total': float(r['total'] or 0),
                'open_amount': float(r['open_amount'] or 0),
                'open_lines_count': r['open_lines_count']
            })
    return jsonify({'sources': sources, 'valid_source_types': valid_source_types})

@purchases_bp.route('/api/source-lines')
@login_required
def api_source_lines():
    doc_id = request.args.get('doc_id', type=int)
    if not doc_id:
        return jsonify({'error': 'Missing doc_id'}), 400
        
    conn = get_db()
    doc = conn.execute('''
        SELECT d.*, p.name as supplier_name, w.name as warehouse_name
        FROM purchase_docs d
        LEFT JOIN partners p ON d.supplier_id = p.id
        LEFT JOIN warehouses w ON d.warehouse_id = w.id
        WHERE d.id = ?
    ''', (doc_id,)).fetchone()
    
    if not doc:
        return jsonify({'error': 'Document not found'}), 404
        
    lines = conn.execute('''
        SELECT l.*, i.name as item_name, i.code as item_code,
               COALESCE(u.code, u.name, 'PCS') as uom_name,
               (l.quantity - COALESCE(l.copied_qty, 0)) as open_qty
        FROM purchase_lines l
        LEFT JOIN items i ON l.item_id = i.id
        LEFT JOIN uom u ON i.uom_id = u.id
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
            'uom_name': l['uom_name'] or 'PCS',
            'description': l['description'] or '',
            'quantity': float(l['quantity'] or 0),
            'copied_qty': float(l['copied_qty'] or 0),
            'open_qty': open_qty,
            'unit_cost': float(l['unit_cost'] or 0),
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
            'supplier_id': doc['supplier_id'],
            'supplier_name': doc['supplier_name'] or '',
            'warehouse_id': doc['warehouse_id'],
            'warehouse_name': doc['warehouse_name'] or '',
            'remarks': doc['remarks'] or ''
        },
        'lines': lines_data
    })

# --- DOCUMENT LIST & DETAILS ---

@purchases_bp.route('/<doc_kind>')
@login_required
@require_permission('purchases', 'read')
def doc_list(doc_kind):
    if doc_kind not in DOC_TYPES:
        return "Not found", 404
    doc_type = DOC_TYPES[doc_kind]
    
    conn = get_db()
    search = request.args.get('search', '').strip()
    status = request.args.get('status', '')
    supplier_id = request.args.get('supplier_id', '')
    branch_id_param = request.args.get('branch_id', type=int)
    
    # Active branch filter
    active_branch_id = session.get('active_branch_id', 0)
    effective_branch_id = branch_id_param if branch_id_param is not None else (active_branch_id if active_branch_id > 0 else None)
    
    query = """
        SELECT d.*, p.name as supplier_name, b.name as branch_name, b.code as branch_code,
               (SELECT COUNT(*) FROM purchase_lines l WHERE l.doc_id = d.id AND (l.quantity - COALESCE(l.copied_qty, 0)) > 0) as open_lines_count
        FROM purchase_docs d 
        LEFT JOIN partners p ON d.supplier_id = p.id 
        LEFT JOIN branches b ON d.branch_id = b.id
        WHERE d.doc_type = ?
    """
    params = [doc_type]
    
    if status:
        query += " AND d.status = ?"
        params.append(status)
    if supplier_id:
        query += " AND d.supplier_id = ?"
        params.append(supplier_id)
    if effective_branch_id:
        query += " AND d.branch_id = ?"
        params.append(effective_branch_id)
    if search:
        s_where, s_params = smart_search_sql(search, ['d.doc_ref', 'p.name', 'p.code', 'd.remarks', 'd.status', 'd.doc_date'])
        query += f" AND {s_where}"
        params.extend(s_params)
        
    query += " ORDER BY d.doc_date DESC, d.id DESC"
    docs = conn.execute(query, params).fetchall()
    suppliers = conn.execute("SELECT id, code, name FROM partners WHERE partner_type IN ('Supplier', 'Customer & Supplier') ORDER BY name").fetchall()
    branches = conn.execute("SELECT id, code, name FROM branches WHERE is_active=1 ORDER BY is_main DESC, name ASC").fetchall()
    
    return render_template('purchases/doc_list.html', docs=docs, doc_kind=doc_kind, doc_type=doc_type,
                           suppliers=suppliers, branches=branches, current_branch_id=effective_branch_id, search=search)

@purchases_bp.route('/<doc_kind>/new', methods=['GET', 'POST'])
@login_required
@require_permission('purchases', 'write')
def doc_new(doc_kind):
    if doc_kind not in DOC_TYPES:
        return "Not found", 404
    doc_type = DOC_TYPES[doc_kind]
    conn = get_db()
    
    if request.method == 'POST':
        supplier_id = request.form.get('supplier_id')
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
        costs = request.form.getlist('unit_cost[]')
        discounts = request.form.getlist('discount_pct[]')
        tax_ids = request.form.getlist('tax_id[]')
        descriptions = request.form.getlist('description[]')
        source_line_ids = request.form.getlist('source_line_id[]')
        
        if not supplier_id or not item_ids:
            flash('Supplier and at least one item line are required.', 'error')
            return redirect(request.url)
            
        doc_ref = next_ref('Purchases', doc_type, conn)
        cursor = conn.cursor()
        
        # Validate copied quantities against source lines open balance
        for i in range(len(item_ids)):
            if not item_ids[i]: continue
            s_line_id = source_line_ids[i] if i < len(source_line_ids) and source_line_ids[i] else None
            if s_line_id:
                s_line = conn.execute('SELECT quantity, copied_qty FROM purchase_lines WHERE id = ?', (s_line_id,)).fetchone()
                if s_line:
                    avail_open = max(0.0, s_line['quantity'] - (s_line['copied_qty'] or 0.0))
                    req_qty = float(quantities[i]) if quantities[i] else 0.0
                    if req_qty > (avail_open + 0.0001):
                        flash(f'Cannot copy quantity {req_qty:,.2f} exceeding open quantity {avail_open:,.2f}.', 'error')
                        return redirect(request.url)
                        
        subtotal = 0.0
        tax_total = 0.0
        grand_total = 0.0
        
        contact_id = request.form.get('contact_id', type=int) or None
        initial_status = 'Draft'
        cursor.execute("""
            INSERT INTO purchase_docs 
            (doc_ref, doc_type, doc_date, due_date, supplier_id, warehouse_id, branch_id, status, subtotal, tax_total, total, balance_due, remarks, source_doc_id, source_doc_type, source_doc_ref, posted_by, contact_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?, ?, ?, ?, ?)
        """, (doc_ref, doc_type, doc_date, due_date, supplier_id, warehouse_id, branch_id, initial_status, remarks, source_doc_id, source_doc_type, source_doc_ref, session.get('user_id'), contact_id))
        doc_id = cursor.lastrowid
        
        for i in range(len(item_ids)):
            if not item_ids[i]: continue
            qty = float(quantities[i]) if quantities[i] else 0.0
            unit_cost = float(costs[i]) if costs[i] else 0.0
            disc = float(discounts[i]) if i < len(discounts) and discounts[i] else 0.0
            tax = int(tax_ids[i]) if i < len(tax_ids) and tax_ids[i] else None
            desc = descriptions[i] if i < len(descriptions) else ''
            s_line_id = int(source_line_ids[i]) if i < len(source_line_ids) and source_line_ids[i] else None
            
            line_net = qty * unit_cost * (1.0 - disc / 100.0)
            subtotal += line_net
            tax_amt = 0.0
            
            cursor.execute("""
                INSERT INTO purchase_lines
                (doc_id, item_id, description, quantity, unit_cost, discount_pct, tax_id, tax_amount, line_total, copied_qty, source_doc_type, source_doc_id, source_line_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """, (doc_id, item_ids[i], desc, qty, unit_cost, disc, tax, tax_amt, line_net + tax_amt, source_doc_type, source_doc_id, s_line_id))
            
            # Atomically increment copied_qty on the source line
            if s_line_id:
                cursor.execute('UPDATE purchase_lines SET copied_qty = COALESCE(copied_qty, 0) + ? WHERE id = ?', (qty, s_line_id))
                
            grand_total += (line_net + tax_amt)
            
        cursor.execute("""
            UPDATE purchase_docs 
            SET subtotal = ?, tax_total = ?, total = ?, balance_due = ?
            WHERE id = ?
        """, (subtotal, tax_total, grand_total, grand_total, doc_id))
        
        conn.commit()
        log_audit(conn, session.get('user_id'), 'Purchases', 'Create', doc_type, doc_id, f"Created {doc_type} {doc_ref}" + (f" copied from {source_doc_type} {source_doc_ref}" if source_doc_ref else ""))
        flash(f"{doc_type} {doc_ref} created successfully.", "success")
        return redirect(url_for('purchases.doc_detail', doc_kind=doc_kind, id=doc_id))
        
    suppliers = conn.execute("SELECT id, code, name FROM partners WHERE partner_type IN ('Supplier', 'Customer & Supplier') ORDER BY name").fetchall()
    warehouses = conn.execute("SELECT id, name FROM warehouses WHERE is_active=1").fetchall()
    items = conn.execute("""
        SELECT i.id, i.code, i.name, i.price, i.cost, COALESCE(u.code, u.name, 'PCS') as uom_name
        FROM items i
        LEFT JOIN uom u ON i.uom_id = u.id
        WHERE i.is_active=1
    """).fetchall()
    items_json = {row['id']: dict(row) for row in items}
    
    # Check pre-population from Copy To query params
    copy_from_id = request.args.get('copy_from_id', type=int)
    copy_from_type = request.args.get('copy_from_type', '')
    prefilled_doc = None
    prefilled_lines = []
    
    if copy_from_id and copy_from_type:
        src = conn.execute('SELECT * FROM purchase_docs WHERE id = ?', (copy_from_id,)).fetchone()
        if src:
            prefilled_doc = dict(src)
            src_lines = conn.execute('''
                SELECT l.*, i.name as item_name, i.code as item_code,
                       COALESCE(u.code, u.name, 'PCS') as uom_name,
                       (l.quantity - COALESCE(l.copied_qty, 0)) as open_qty
                FROM purchase_lines l
                LEFT JOIN items i ON l.item_id = i.id
                LEFT JOIN uom u ON i.uom_id = u.id
                WHERE l.doc_id = ? AND (l.quantity - COALESCE(l.copied_qty, 0)) > 0
            ''', (copy_from_id,)).fetchall()
            prefilled_lines = [dict(sl) for sl in src_lines]
            
    valid_source_types = COPY_FROM_MAP.get(doc_type, [])
    branches = conn.execute("SELECT * FROM branches WHERE is_active=1 ORDER BY is_main DESC, name ASC").fetchall()
    
    return render_template('purchases/doc_form.html', doc_kind=doc_kind, doc_type=doc_type, 
                           suppliers=suppliers, warehouses=warehouses, items=items,
                           items_json=json.dumps(items_json), doc=prefilled_doc,
                           prefilled_lines=prefilled_lines, copy_from_id=copy_from_id,
                           copy_from_type=copy_from_type, valid_source_types=valid_source_types,
                           branches=branches)

@purchases_bp.route('/<doc_kind>/<int:id>')
@login_required
@require_permission('purchases', 'read')
def doc_detail(doc_kind, id):
    if doc_kind not in DOC_TYPES:
        return "Not found", 404
    doc_type = DOC_TYPES[doc_kind]
    conn = get_db()
    doc = conn.execute("""
        SELECT d.*, p.name as supplier_name, w.name as warehouse_name,
               b.name as branch_name, b.code as branch_code
        FROM purchase_docs d
        LEFT JOIN partners p ON d.supplier_id = p.id
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
            return redirect(url_for('purchases.doc_list', doc_kind=doc_kind))
        
    lines = conn.execute("""
        SELECT l.*, i.name as item_name, i.code as item_code,
               COALESCE(u.code, u.name, 'PCS') as uom_name,
               (l.quantity - COALESCE(l.copied_qty, 0)) as open_qty
        FROM purchase_lines l
        LEFT JOIN items i ON l.item_id = i.id
        LEFT JOIN uom u ON i.uom_id = u.id
        WHERE l.doc_id = ?
    """, (id,)).fetchall()
    
    # 1. Base Documents (Upstream - Copied From)
    base_docs = []
    if doc['source_doc_id']:
        b = conn.execute('''
            SELECT id, doc_ref, doc_date, doc_type, status, total 
            FROM purchase_docs WHERE id = ?
        ''', (doc['source_doc_id'],)).fetchone()
        if b:
            base_docs.append(dict(b))
            
    # 2. Target Documents (Downstream - Copied To)
    target_docs = conn.execute('''
        SELECT DISTINCT d.id, d.doc_ref, d.doc_date, d.doc_type, d.status, d.total
        FROM purchase_docs d
        JOIN purchase_lines l ON l.doc_id = d.id
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
    
    return render_template('purchases/doc_detail.html', doc=doc, lines=lines, doc_kind=doc_kind,
                           base_docs=base_docs, target_docs=target_docs,
                           valid_copy_to=valid_copy_to, total_open_qty=total_open_qty,
                           is_fully_copied=is_fully_copied, doc_kind_map=DOC_KIND_FROM_TYPE)

@purchases_bp.route('/<doc_kind>/<int:id>/post', methods=['POST'])
@login_required
def doc_post(doc_kind, id):
    conn = get_db()
    doc = conn.execute("SELECT * FROM purchase_docs WHERE id = ?", (id,)).fetchone()
    if not doc or doc['status'] != 'Draft':
        flash("Can only post Draft documents", "error")
        return redirect(url_for('purchases.doc_detail', doc_kind=doc_kind, id=id))
        
    doc_type = doc['doc_type']
    lines = conn.execute("SELECT * FROM purchase_lines WHERE doc_id = ?", (id,)).fetchall()
    
    if doc_type == 'Receipt':
        # Add stock for received items
        for line in lines:
            update_stock(conn, line['item_id'], doc['warehouse_id'], line['quantity'], 0, doc_type, id, doc['doc_ref'], line['unit_cost'], session.get('user_id'))
    
    elif doc_type == 'Invoice':
        ap_acc = conn.execute("SELECT id FROM coa WHERE code='2110'").fetchone()
        inv_acc = conn.execute("SELECT id FROM coa WHERE code='1300'").fetchone()
        tax_acc = conn.execute("SELECT id FROM coa WHERE code='2210'").fetchone()
        
        ap_id = ap_acc['id'] if ap_acc else 4
        inv_id = inv_acc['id'] if inv_acc else 5
        tax_id = tax_acc['id'] if tax_acc else 3
        
        j_lines = [
            (inv_id, doc['supplier_id'], doc['subtotal'], 0, f"Purchase Bill {doc['doc_ref']}"),
            (ap_id, doc['supplier_id'], 0, doc['total'], f"Accounts Payable {doc['doc_ref']}")
        ]
        if doc['tax_total'] > 0:
            j_lines.append((tax_id, None, doc['tax_total'], 0, f"VAT Claimable {doc['doc_ref']}"))
        
        post_journal(conn, doc['doc_ref'], doc['doc_date'], 'Purchases', doc_type, id, f"Supplier Bill {doc['doc_ref']}", j_lines, session.get('user_id'), branch_id=doc['branch_id'])
        conn.execute("UPDATE partners SET balance = balance + ? WHERE id = ?", (doc['total'], doc['supplier_id']))
        
    elif doc_type == 'Purchase Return':
        # Deduct stock and debit AP
        for line in lines:
            update_stock(conn, line['item_id'], doc['warehouse_id'], 0, line['quantity'], doc_type, id, doc['doc_ref'], line['unit_cost'], session.get('user_id'))
            
        ap_acc = conn.execute("SELECT id FROM coa WHERE code='2110'").fetchone()
        inv_acc = conn.execute("SELECT id FROM coa WHERE code='1300'").fetchone()
        ap_id = ap_acc['id'] if ap_acc else 4
        inv_id = inv_acc['id'] if inv_acc else 5
        
        j_lines = [
            (ap_id, doc['supplier_id'], doc['total'], 0, f"Debit Note AP {doc['doc_ref']}"),
            (inv_id, doc['supplier_id'], 0, doc['subtotal'], f"Inventory Reduction {doc['doc_ref']}")
        ]
        post_journal(conn, doc['doc_ref'], doc['doc_date'], 'Purchases', doc_type, id, f"Purchase Return {doc['doc_ref']}", j_lines, session.get('user_id'), branch_id=doc['branch_id'])
        conn.execute("UPDATE partners SET balance = balance - ? WHERE id = ?", (doc['total'], doc['supplier_id']))
        
    elif doc_type == 'Down Payment Invoice':
        # AP Down Payment: Debits Vendor Advances (1210), Credits Accounts Payable (2110)
        ap_acc = conn.execute("SELECT id FROM coa WHERE code='2110'").fetchone()
        dp_acc = conn.execute("SELECT id FROM coa WHERE code='1210'").fetchone()
        tax_acc = conn.execute("SELECT id FROM coa WHERE code='2210'").fetchone()
        
        ap_id = ap_acc['id'] if ap_acc else 4
        dp_id = dp_acc['id'] if dp_acc else 5
        tax_id = tax_acc['id'] if tax_acc else 3
        
        j_lines = [
            (dp_id, doc['supplier_id'], doc['subtotal'], 0, f"Vendor Advance {doc['doc_ref']}"),
            (ap_id, doc['supplier_id'], 0, doc['total'], f"AP Down Payment {doc['doc_ref']}")
        ]
        if doc['tax_total'] > 0:
            j_lines.append((tax_id, None, doc['tax_total'], 0, f"VAT Claimable {doc['doc_ref']}"))
        post_journal(conn, doc['doc_ref'], doc['doc_date'], 'Purchases', doc_type, id, f"Down Payment {doc['doc_ref']}", j_lines, session.get('user_id'), branch_id=doc['branch_id'])
        conn.execute("UPDATE partners SET balance = balance + ? WHERE id = ?", (doc['total'], doc['supplier_id']))
        
    elif doc_type in ('Request', 'Purchase Quotation', 'Purchase Order'):
        pass
        
    conn.execute("UPDATE purchase_docs SET status = 'Posted', posted_at = datetime('now') WHERE id = ?", (id,))
    conn.commit()
    log_audit(conn, session.get('user_id'), 'Purchases', 'Post', doc_type, id, f"Posted {doc_type} {doc['doc_ref']}")
    flash(f"{doc_type} posted successfully.", "success")
    return redirect(url_for('purchases.doc_detail', doc_kind=doc_kind, id=id))

@purchases_bp.route('/<doc_kind>/<int:id>/print')
@login_required
def doc_print(doc_kind, id):
    from database import get_print_settings
    conn = get_db()
    doc = conn.execute("""
        SELECT d.*, p.name as supplier_name, p.code as supplier_code, p.address as supplier_address,
               p.phone as supplier_phone, p.email as supplier_email, p.tax_id as supplier_tax_id,
               b.name as branch_name, w.name as warehouse_name
        FROM purchase_docs d
        LEFT JOIN partners p ON d.supplier_id = p.id
        LEFT JOIN branches b ON d.branch_id = b.id
        LEFT JOIN warehouses w ON d.warehouse_id = w.id
        WHERE d.id = ?
    """, (id,)).fetchone()
    if not doc:
        abort(404)
    
    lines = conn.execute("""
        SELECT l.*, i.name as item_name, i.code as item_code,
               COALESCE(u.code, u.name, 'PCS') as uom_name
        FROM purchase_lines l
        LEFT JOIN items i ON l.item_id = i.id
        LEFT JOIN uom u ON i.uom_id = u.id
        WHERE l.doc_id = ?
    """, (id,)).fetchall()
    
    company = conn.execute("SELECT * FROM company WHERE id = 1").fetchone()
    print_cfg = get_print_settings(conn)
    return render_template('purchases/bill_print.html', doc=doc, lines=lines, company=company, print_cfg=print_cfg)
