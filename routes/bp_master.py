import csv
from io import StringIO
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, make_response
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

        conn = get_db()
        try:
            # Always auto-generate if blank — never let a partner slip through without a code
            if not code:
                code = next_bp_code(conn, p_type)

            conn.execute('''
                INSERT INTO partners (code, name, partner_type, phone, email, address, tax_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'Active')
            ''', (code, name, p_type, phone, email, address, tax_id))

            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            log_audit(conn, session['user_id'], 'BP Master', 'Create', 'Partner', new_id, f"Created partner {code}")
            conn.commit()
            flash(f'Partner {code} — {name} created successfully.', 'success')
            return redirect(url_for('bp_master.partner_detail', id=new_id))
        except Exception as e:
            conn.rollback()
            flash(f"Error: {str(e)}", 'danger')

    return render_template('bp_master/partner_form.html', partner=None, bp_prefix=BP_PREFIX)

@bp_master_bp.route('/<int:id>')
@login_required
def partner_detail(id):
    conn = get_db()
    partner = conn.execute("SELECT * FROM partners WHERE id = ?", (id,)).fetchone()
    sales = conn.execute("SELECT * FROM sales_docs WHERE customer_id = ? ORDER BY id DESC LIMIT 10", (id,)).fetchall()
    purchases = conn.execute("SELECT * FROM purchase_docs WHERE supplier_id = ? ORDER BY id DESC LIMIT 10", (id,)).fetchall()
    payments = conn.execute("SELECT * FROM payments WHERE partner_id = ? ORDER BY id DESC LIMIT 10", (id,)).fetchall()
    
    return render_template('bp_master/partner_detail.html', partner=partner, sales=sales, purchases=purchases, payments=payments)

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
        
        try:
            conn.execute('''
                UPDATE partners SET code=?, name=?, partner_type=?, phone=?, email=?, address=?, tax_id=?
                WHERE id=?
            ''', (code, name, p_type, phone, email, address, tax_id, id))
            log_audit(conn, session['user_id'], 'BP Master', 'Edit', 'Partner', id, f"Updated partner {code}")
            conn.commit()
            flash('Partner updated successfully', 'success')
            return redirect(url_for('bp_master.partner_detail', id=id))
        except Exception as e:
            conn.rollback()
            flash(f"Error: {str(e)}", 'danger')
            
    return render_template('bp_master/partner_form.html', partner=partner, bp_prefix=BP_PREFIX)

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
