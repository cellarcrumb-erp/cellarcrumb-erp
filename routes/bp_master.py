import csv
from io import StringIO
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, make_response, jsonify
from database import get_db, next_ref, log_audit
from auth import login_required, require_permission

bp_master_bp = Blueprint('bp_master', __name__, url_prefix='/bp')

@bp_master_bp.route('/')
@login_required
@require_permission('bp_master', 'read')
def index():
    search = request.args.get('search', '')
    p_type = request.args.get('type', '')
    conn = get_db()
    
    query = "SELECT * FROM partners WHERE (name LIKE ? OR code LIKE ? OR phone LIKE ?)"
    params = [f"%{search}%", f"%{search}%", f"%{search}%"]
    
    if p_type:
        query += " AND partner_type = ?"
        params.append(p_type)
        
    partners = conn.execute(query, params).fetchall()
    return render_template('bp_master/index.html', partners=partners, search=search, type=p_type)


# ── Partner type → code prefix mapping ────────────────────────────────────────
BP_PREFIX = {
    'Customer':            'CUS',
    'Walk-in Customer':    'WLK',
    'Supplier':            'SUP',
    'Customer & Supplier': 'BPS',
    'Employee':            'EMP',
    'Other':               'OTH',
}

def next_bp_code(conn, partner_type):
    """Generate the next sequential CardCode for a partner type.
    Scans existing codes matching the prefix and returns MAX+1, zero-padded to 4 digits.
    e.g.  SUP0001, SUP0002 … CUS0001 …
    """
    prefix = BP_PREFIX.get(partner_type, 'CUS' if 'Customer' in partner_type else 'SUP' if 'Supplier' in partner_type else 'BP')
    cursor = conn.cursor()
    rows = cursor.execute("SELECT code FROM partners WHERE code LIKE ?", (f"{prefix}%",)).fetchall()
    max_num = 0
    for r in rows:
        code_val = r['code'] if isinstance(r, dict) or hasattr(r, 'keys') else r[0]
        if code_val:
            num_part = ''.join(filter(str.isdigit, code_val[len(prefix):]))
            if num_part:
                try:
                    max_num = max(max_num, int(num_part))
                except ValueError:
                    pass
    next_num = max_num + 1
    return f"{prefix}{next_num:04d}"


@bp_master_bp.route('/next_code')
@login_required
def next_code_api():
    """JSON endpoint — returns the next auto-generated code for a given partner type.
    Called by the partner form via JS when the type dropdown changes.
    """
    from flask import jsonify
    p_type = request.args.get('type', 'Customer')
    conn = get_db()
    code = next_bp_code(conn, p_type)
    return jsonify({'code': code})


@bp_master_bp.route('/new', methods=['GET', 'POST'])
@login_required
def new_partner():
    if request.method == 'POST':
        code = request.form.get('code', '').strip()
        name = request.form.get('name', '').strip()
        p_type = request.form.get('partner_type', 'Customer')
        phone = request.form.get('phone', '').strip()
        email = request.form.get('email', '').strip()
        address = request.form.get('address', '').strip()
        tax_id = request.form.get('tax_id', '').strip()
        group_id = request.form.get('group_id', type=int) or None
        payment_term_id = request.form.get('payment_term_id', type=int) or None
        price_list_id = request.form.get('price_list_id', type=int) or None

        conn = get_db()
        try:
            # Always auto-generate if blank — never let a partner slip through without a code
            if not code:
                code = next_bp_code(conn, p_type)

            cur = conn.cursor()
            cur.execute('''
                INSERT INTO partners (code, name, partner_type, phone, email, address, tax_id, status, group_id, payment_term_id, price_list_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'Active', ?, ?, ?)
            ''', (code, name, p_type, phone, email, address, tax_id, group_id, payment_term_id, price_list_id))

            new_id = cur.lastrowid
            log_audit(conn, session['user_id'], 'BP Master', 'Create', 'Partner', new_id, f"Created partner {code}")
            conn.commit()
            flash(f'Partner {code} — {name} created successfully.', 'success')
            return redirect(url_for('bp_master.partner_detail', id=new_id))
        except Exception as e:
            conn.rollback()
            flash(f"Error: {str(e)}", 'danger')

    conn = get_db()
    groups = conn.execute("SELECT * FROM bp_groups ORDER BY name").fetchall()
    payment_terms = conn.execute("SELECT * FROM payment_terms WHERE is_active=1 ORDER BY name").fetchall()
    price_lists = conn.execute("SELECT * FROM price_lists WHERE is_active=1 ORDER BY name").fetchall()
    return render_template('bp_master/partner_form.html', partner=None, bp_prefix=BP_PREFIX,
                           groups=groups, payment_terms=payment_terms, price_lists=price_lists)

@bp_master_bp.route('/<int:id>')
@login_required
def partner_detail(id):
    conn = get_db()
    partner = conn.execute("""
        SELECT p.*, g.name as group_name, pt.name as payment_term_name, pt.due_days, pl.name as price_list_name
        FROM partners p
        LEFT JOIN bp_groups g ON p.group_id = g.id
        LEFT JOIN payment_terms pt ON p.payment_term_id = pt.id
        LEFT JOIN price_lists pl ON p.price_list_id = pl.id
        WHERE p.id = ?
    """, (id,)).fetchone()
    sales = conn.execute("SELECT * FROM sales_docs WHERE customer_id = ? ORDER BY id DESC LIMIT 10", (id,)).fetchall()
    purchases = conn.execute("SELECT * FROM purchase_docs WHERE supplier_id = ? ORDER BY id DESC LIMIT 10", (id,)).fetchall()
    payments = conn.execute("SELECT * FROM payments WHERE partner_id = ? ORDER BY id DESC LIMIT 10", (id,)).fetchall()
    contacts = conn.execute("SELECT * FROM bp_contacts WHERE partner_id = ? ORDER BY is_default DESC, name ASC", (id,)).fetchall()
    
    return render_template('bp_master/partner_detail.html', partner=partner, sales=sales, purchases=purchases, payments=payments, contacts=contacts)

@bp_master_bp.route('/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_partner(id):
    conn = get_db()
    partner = conn.execute("SELECT * FROM partners WHERE id = ?", (id,)).fetchone()
    
    if request.method == 'POST':
        code = request.form.get('code', '').strip() or partner['code']
        name = request.form.get('name', '').strip()
        p_type = request.form.get('partner_type')
        phone = request.form.get('phone', '').strip()
        email = request.form.get('email', '').strip()
        address = request.form.get('address', '').strip()
        tax_id = request.form.get('tax_id', '').strip()
        group_id = request.form.get('group_id', type=int) or None
        payment_term_id = request.form.get('payment_term_id', type=int) or None
        price_list_id = request.form.get('price_list_id', type=int) or None
        
        try:
            conn.execute('''
                UPDATE partners SET code=?, name=?, partner_type=?, phone=?, email=?, address=?, tax_id=?,
                                    group_id=?, payment_term_id=?, price_list_id=?
                WHERE id=?
            ''', (code, name, p_type, phone, email, address, tax_id, group_id, payment_term_id, price_list_id, id))
            log_audit(conn, session['user_id'], 'BP Master', 'Edit', 'Partner', id, f"Updated partner {code}")
            conn.commit()
            flash('Partner updated successfully', 'success')
            return redirect(url_for('bp_master.partner_detail', id=id))
        except Exception as e:
            conn.rollback()
            flash(f"Error: {str(e)}", 'danger')
            
    groups = conn.execute("SELECT * FROM bp_groups ORDER BY name").fetchall()
    payment_terms = conn.execute("SELECT * FROM payment_terms WHERE is_active=1 ORDER BY name").fetchall()
    price_lists = conn.execute("SELECT * FROM price_lists WHERE is_active=1 ORDER BY name").fetchall()
    return render_template('bp_master/partner_form.html', partner=partner, bp_prefix=BP_PREFIX,
                           groups=groups, payment_terms=payment_terms, price_lists=price_lists)

@bp_master_bp.route('/<int:id>/archive', methods=['POST'])
@login_required
def archive_partner(id):
    conn = get_db()
    try:
        partner = conn.execute("SELECT status FROM partners WHERE id = ?", (id,)).fetchone()
        new_status = 'Archived' if partner['status'] == 'Active' else 'Active'
        conn.execute("UPDATE partners SET status = ? WHERE id = ?", (new_status, id))
        conn.commit()
        flash(f'Partner {new_status.lower()} successfully', 'success')
    except Exception as e:
        conn.rollback()
        flash(f"Error: {str(e)}", 'danger')
    return redirect(url_for('bp_master.index'))

@bp_master_bp.route('/<int:id>/statement')
@login_required
def partner_statement(id):
    conn = get_db()
    partner = conn.execute("SELECT * FROM partners WHERE id = ?", (id,)).fetchone()
    if not partner:
        flash('Partner not found.', 'error')
        return redirect(url_for('bp_master.index'))

    date_from = request.args.get('date_from', '')
    date_to   = request.args.get('date_to', '')

    rows = []

    # ── Sales Invoices (debit = charge to customer) ────────────────────────
    q = """
        SELECT doc_date as txn_date, doc_ref, 'Invoice' as txn_type,
               total as debit, 0.0 as credit, status
        FROM sales_docs
        WHERE customer_id = ? AND doc_type = 'Invoice' AND status != 'Cancelled'
    """
    params = [id]
    if date_from: q += " AND doc_date >= ?"; params.append(date_from)
    if date_to:   q += " AND doc_date <= ?"; params.append(date_to)
    rows += conn.execute(q, params).fetchall()

    # ── Sales Returns (credit = reduce AR) ────────────────────────────────
    q = """
        SELECT doc_date as txn_date, doc_ref, 'Credit Note' as txn_type,
               0.0 as debit, total as credit, status
        FROM sales_docs
        WHERE customer_id = ? AND doc_type = 'Sales Return' AND status != 'Cancelled'
    """
    params = [id]
    if date_from: q += " AND doc_date >= ?"; params.append(date_from)
    if date_to:   q += " AND doc_date <= ?"; params.append(date_to)
    rows += conn.execute(q, params).fetchall()

    # ── Payments received from customer (credit = reduce AR) ───────────────
    q = """
        SELECT doc_date as txn_date, doc_ref, 'Payment' as txn_type,
               0.0 as debit, amount as credit, status
        FROM payments
        WHERE partner_id = ? AND payment_type = 'Incoming' AND status = 'Posted'
    """
    params = [id]
    if date_from: q += " AND doc_date >= ?"; params.append(date_from)
    if date_to:   q += " AND doc_date <= ?"; params.append(date_to)
    rows += conn.execute(q, params).fetchall()

    # Sort by date, then by doc_ref
    transactions = sorted(rows, key=lambda r: (r['txn_date'], r['doc_ref']))

    # Compute running balance (opening = 0, positive = owed by customer)
    balance = 0.0
    ledger = []
    for t in transactions:
        balance += float(t['debit'] or 0) - float(t['credit'] or 0)
        ledger.append({
            'date':     t['txn_date'],
            'ref':      t['doc_ref'],
            'type':     t['txn_type'],
            'debit':    float(t['debit'] or 0),
            'credit':   float(t['credit'] or 0),
            'balance':  balance,
            'status':   t['status'],
        })

    return render_template('bp_master/statement.html',
                           partner=partner,
                           ledger=ledger,
                           closing_balance=balance,
                           date_from=date_from,
                           date_to=date_to)

@bp_master_bp.route('/export')
@login_required
def export_csv():
    conn = get_db()
    partners = conn.execute("SELECT * FROM partners").fetchall()
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(['Code', 'Name', 'Type', 'Phone', 'Email', 'Balance', 'Status'])
    for p in partners:
        cw.writerow([p['code'], p['name'], p['partner_type'], p['phone'], p['email'], p['balance'], p['status']])
        
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=partners.csv"
    output.headers["Content-type"] = "text/csv"
    return output

# ── Contacts & BP Groups (SAP OCRG, OCPR) ────────────────────────────────────
@bp_master_bp.route('/<int:id>/contacts/new', methods=['POST'])
@login_required
@require_permission('bp_master', 'write')
def new_contact(id):
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip()
    phone = request.form.get('phone', '').strip()
    mobile = request.form.get('mobile', '').strip()
    position = request.form.get('position', '').strip()
    is_default = 1 if request.form.get('is_default') == 'on' else 0
    if not name:
        flash('Contact name is required.', 'error')
        return redirect(url_for('bp_master.partner_detail', id=id))

    conn = get_db()
    try:
        if is_default:
            conn.execute("UPDATE bp_contacts SET is_default = 0 WHERE partner_id = ?", (id,))
        conn.execute("""
            INSERT INTO bp_contacts (partner_id, name, email, phone, mobile, position, is_default)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (id, name, email, phone, mobile, position, is_default))
        conn.commit()
        log_audit(conn, session['user_id'], 'BP Master', 'Create', 'Contact', None, f"Created contact {name} for partner {id}")
        flash(f"Contact {name} added.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", 'error')
    return redirect(url_for('bp_master.partner_detail', id=id))


@bp_master_bp.route('/contacts/<int:cid>/delete', methods=['POST'])
@login_required
@require_permission('bp_master', 'write')
def delete_contact(cid):
    conn = get_db()
    contact = conn.execute("SELECT partner_id, name FROM bp_contacts WHERE id = ?", (cid,)).fetchone()
    if contact:
        try:
            conn.execute("DELETE FROM bp_contacts WHERE id = ?", (cid,))
            conn.commit()
            log_audit(conn, session['user_id'], 'BP Master', 'Delete', 'Contact', cid, f"Deleted contact {contact['name']}")
            flash('Contact removed.', 'success')
            return redirect(url_for('bp_master.partner_detail', id=contact['partner_id']))
        except Exception as e:
            conn.rollback()
            flash(f"Error: {e}", 'error')
    return redirect(url_for('bp_master.index'))


@bp_master_bp.route('/api/partner-info/<int:id>')
@login_required
def partner_info_api(id):
    conn = get_db()
    p = conn.execute("SELECT * FROM partners WHERE id = ?", (id,)).fetchone()
    if not p:
        return jsonify({'error': 'partner not found'}), 404
    pt = None
    if p['payment_term_id']:
        pt = conn.execute("SELECT * FROM payment_terms WHERE id = ?", (p['payment_term_id'],)).fetchone()
    contacts = conn.execute("SELECT id, name, email, phone, position, is_default FROM bp_contacts WHERE partner_id = ? ORDER BY is_default DESC, name ASC", (id,)).fetchall()
    return jsonify({
        'id': p['id'],
        'code': p['code'],
        'name': p['name'],
        'price_list_id': p['price_list_id'],
        'payment_term_id': p['payment_term_id'],
        'due_days': pt['due_days'] if pt else 30,
        'contacts': [dict(c) for c in contacts]
    })


@bp_master_bp.route('/groups', methods=['GET', 'POST'])
@login_required
@require_permission('bp_master', 'read')
def bp_groups():
    conn = get_db()
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        group_type = request.form.get('group_type', 'Customer')
        if not name:
            flash('Group name is required.', 'error')
            return redirect(url_for('bp_master.bp_groups'))
        try:
            conn.execute("INSERT INTO bp_groups (name, group_type, created_at) VALUES (?, ?, datetime('now'))",
                         (name, group_type))
            conn.commit()
            log_audit(conn, session['user_id'], 'BP Master', 'Create', 'BPGroup', None, f"Created BP Group {name}")
            flash(f"Group '{name}' created.", "success")
            return redirect(url_for('bp_master.bp_groups'))
        except Exception as e:
            conn.rollback()
            flash(f"Error: {e}", 'error')

    groups = conn.execute("""
        SELECT g.*, (SELECT COUNT(*) FROM partners p WHERE p.group_id = g.id) as partner_count
        FROM bp_groups g ORDER BY g.group_type, g.name
    """).fetchall()
    return render_template('bp_master/groups.html', groups=groups)

