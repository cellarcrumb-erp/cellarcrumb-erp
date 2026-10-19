from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from functools import wraps
from database import get_db, next_ref, post_journal, log_audit
from datetime import date

payments_bp = Blueprint('payments', __name__, url_prefix='/payments')

from auth import login_required, require_permission

@payments_bp.route('/')
@login_required
@require_permission('payments', 'read')
def index():
    conn = get_db()
    month = date.today().strftime('%Y-%m')
    total_received = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM payments WHERE payment_type='Incoming' AND status='Posted' AND strftime('%Y-%m',doc_date)=?", (month,)
    ).fetchone()[0] or 0
    total_paid = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM payments WHERE payment_type='Outgoing' AND status='Posted' AND strftime('%Y-%m',doc_date)=?", (month,)
    ).fetchone()[0] or 0
    unallocated = conn.execute(
        "SELECT COALESCE(SUM(unallocated),0) FROM payments WHERE status='Posted'"
    ).fetchone()[0] or 0
    recent_payments = conn.execute('''
        SELECT p.*, bp.name as partner_name, m.name as method_name, b.name as account_name
        FROM payments p 
        LEFT JOIN partners bp ON p.partner_id=bp.id 
        LEFT JOIN payment_methods m ON p.payment_method_id=m.id
        LEFT JOIN bank_accounts b ON p.bank_account_id=b.id
        ORDER BY p.id DESC LIMIT 10
    ''').fetchall()
    return render_template('payments/index.html', total_received=total_received,
                           total_paid=total_paid, unallocated=unallocated, recent_payments=recent_payments)

@payments_bp.route('/incoming')
@login_required
def incoming():
    conn = get_db()
    payments_list = conn.execute('''
        SELECT p.*, bp.name as partner_name, m.name as method_name, b.name as account_name
        FROM payments p 
        LEFT JOIN partners bp ON p.partner_id=bp.id 
        LEFT JOIN payment_methods m ON p.payment_method_id=m.id
        LEFT JOIN bank_accounts b ON p.bank_account_id=b.id
        WHERE p.payment_type = 'Incoming'
        ORDER BY p.doc_date DESC, p.id DESC
    ''').fetchall()
    return render_template('payments/payment_list.html', title='Incoming Payments', payments=payments_list,
                           new_url=url_for('payments.new_incoming'), ptype='Incoming')

incoming_list = incoming

@payments_bp.route('/incoming/new', methods=['GET', 'POST'])
@login_required
def new_incoming():
    conn = get_db()
    if request.method == 'POST':
        partner_id = request.form.get('partner_id')
        doc_date = request.form.get('doc_date', date.today().isoformat())
        amount = request.form.get('amount', 0, type=float)
        method_id = request.form.get('payment_method_id')
        bank_acc_id = request.form.get('bank_account_id')
        branch_id = request.form.get('branch_id', type=int) or session.get('active_branch_id') or None
        reference = request.form.get('reference', '')
        remarks = request.form.get('remarks', '')
        
        if not partner_id or amount <= 0:
            flash('Customer and a valid amount are required.', 'error')
        else:
            try:
                doc_ref = next_ref('Payments', 'Incoming')
                cursor = conn.execute('''
                    INSERT INTO payments (doc_ref, doc_date, payment_type, partner_id, bank_account_id, payment_method_id, amount, amount_allocated, unallocated, reference, remarks, status, branch_id, posted_by)
                    VALUES (?, ?, 'Incoming', ?, ?, ?, ?, 0, ?, ?, ?, 'Draft', ?, ?)
                ''', (doc_ref, doc_date, partner_id, bank_acc_id, method_id, amount, amount, reference, remarks, branch_id, session.get('user_id')))
                pay_id = cursor.lastrowid
                conn.commit()
                log_audit(conn, session.get('user_id'), 'Payments', 'Create', 'Incoming Payment', doc_ref, f'Created payment KES {amount:,.2f}')
                flash(f'Payment {doc_ref} created. You can now post and allocate it.', 'success')
                return redirect(url_for('payments.payment_detail', id=pay_id))
            except Exception as e:
                flash(f'Error creating payment: {e}', 'error')
    partners = conn.execute("SELECT * FROM partners WHERE partner_type IN ('Customer', 'Customer & Supplier') AND status='Active' ORDER BY name").fetchall()
    methods = conn.execute('SELECT * FROM payment_methods WHERE is_active=1').fetchall()
    accounts = conn.execute('SELECT * FROM bank_accounts WHERE is_active=1').fetchall()
    branches = conn.execute("SELECT * FROM branches WHERE is_active=1 ORDER BY is_main DESC, name ASC").fetchall()
    return render_template('payments/payment_form.html', payment_type='Incoming', title='New Customer Receipt',
                           partners=partners, payment_methods=methods, bank_accounts=accounts, coas=[], branches=branches)

@payments_bp.route('/<int:id>')
@payments_bp.route('/incoming/<int:id>')
@payments_bp.route('/outgoing/<int:id>')
@login_required
def payment_detail(id):
    conn = get_db()
    payment = conn.execute('''
        SELECT p.*, bp.name as partner_name, m.name as method_name, m.name as payment_method_name,
               b.name as account_name, b.name as bank_account_name,
               br.name as branch_name, br.code as branch_code
        FROM payments p
        LEFT JOIN partners bp ON p.partner_id=bp.id
        LEFT JOIN payment_methods m ON p.payment_method_id=m.id
        LEFT JOIN bank_accounts b ON p.bank_account_id=b.id
        LEFT JOIN branches br ON p.branch_id=br.id
        WHERE p.id=?
    ''', (id,)).fetchone()
    
    if not payment:
        flash('Payment not found.', 'warning')
        return redirect(url_for('payments.index'))
    
    outstanding_docs = []
    if payment['partner_id']:
        if payment['payment_type'] == 'Incoming':
            outstanding_docs = conn.execute('''
                SELECT * FROM sales_docs 
                WHERE customer_id=? AND doc_type='Invoice' AND status IN ('Posted', 'Partial') AND balance_due > 0
                ORDER BY doc_date
            ''', (payment['partner_id'],)).fetchall()
        elif payment['payment_type'] == 'Outgoing':
            outstanding_docs = conn.execute('''
                SELECT * FROM purchase_docs 
                WHERE supplier_id=? AND doc_type IN ('Invoice', 'Bill') AND status IN ('Posted', 'Partial') AND balance_due > 0
                ORDER BY doc_date
            ''', (payment['partner_id'],)).fetchall()
        
    allocations = conn.execute('''
        SELECT a.*, 
               COALESCE(s.doc_ref, p.doc_ref) as invoice_ref,
               COALESCE(s.doc_date, p.doc_date) as invoice_date,
               COALESCE(s.total, p.total) as invoice_total
        FROM payment_allocations a
        LEFT JOIN sales_docs s ON a.doc_id=s.id AND a.doc_type='Invoice'
        LEFT JOIN purchase_docs p ON a.doc_id=p.id AND a.doc_type='Bill'
        WHERE a.payment_id=?
    ''', (id,)).fetchall()
    
    return render_template('payments/payment_detail.html', payment=payment, 
                           outstanding_docs=outstanding_docs, allocations=allocations)

view_incoming = payment_detail
view_outgoing = payment_detail

@payments_bp.route('/<int:id>/post', methods=['POST'])
@payments_bp.route('/incoming/<int:id>/post', methods=['POST'])
@payments_bp.route('/outgoing/<int:id>/post', methods=['POST'])
@login_required
def post_payment(id):
    conn = get_db()
    payment = conn.execute('SELECT * FROM payments WHERE id=?', (id,)).fetchone()
    if not payment or payment['status'] == 'Posted':
        flash('Payment already posted or not found.', 'warning')
        return redirect(url_for('payments.payment_detail', id=id))
        
    try:
        bank_acc = conn.execute('SELECT * FROM bank_accounts WHERE id=?', (payment['bank_account_id'],)).fetchone()
        bank_coa_id = bank_acc['coa_id'] if bank_acc and bank_acc['coa_id'] else conn.execute("SELECT id FROM coa WHERE code='1110'").fetchone()[0]
        amount = payment['amount']
        
        if payment['payment_type'] == 'Incoming':
            ar_coa_id = conn.execute("SELECT id FROM coa WHERE code='1200'").fetchone()[0]
            lines = [
                (bank_coa_id, payment['partner_id'], amount, 0, f"Customer payment receipt {payment['doc_ref']}"),
                (ar_coa_id, payment['partner_id'], 0, amount, f"Receivable reduction {payment['doc_ref']}")
            ]
            je_id = post_journal(conn, payment['doc_ref'], payment['doc_date'], 'Payments', 'Incoming Payment', id, f"Payment from customer", lines, session.get('user_id'), branch_id=payment['branch_id'])
            
            if payment['bank_account_id']:
                conn.execute('UPDATE bank_accounts SET current_balance = current_balance + ? WHERE id=?', (amount, payment['bank_account_id']))
                conn.execute('''
                    INSERT INTO bank_transactions(doc_ref, doc_date, bank_account_id, txn_type, amount, description, reference, partner_id, journal_id)
                    VALUES(?,?,?,?,?,?,?,?,?)
                ''', (payment['doc_ref'], payment['doc_date'], payment['bank_account_id'], 'Deposit', amount, f"Customer Payment {payment['doc_ref']}", payment['reference'], payment['partner_id'], je_id))
            
            # Reduce partner's outstanding balance (AR) on receipt of payment
            if payment['partner_id']:
                conn.execute('UPDATE partners SET balance = balance - ? WHERE id=?', (amount, payment['partner_id']))
            
            conn.execute("UPDATE payments SET status='Posted', journal_id=?, posted_at=datetime('now') WHERE id=?", (je_id, id))
            conn.commit()
            log_audit(conn, session.get('user_id'), 'Payments', 'Post', 'Incoming Payment', payment['doc_ref'], f'Posted customer receipt KES {amount:,.2f}')
            flash('Payment posted successfully to ledger and bank balance.', 'success')
            
        elif payment['payment_type'] == 'Outgoing':
            ap_coa_id = conn.execute("SELECT id FROM coa WHERE code='2110'").fetchone()[0]
            lines = [
                (ap_coa_id, payment['partner_id'], amount, 0, f"Payable reduction {payment['doc_ref']}"),
                (bank_coa_id, payment['partner_id'], 0, amount, f"Payment to supplier {payment['doc_ref']}")
            ]
            je_id = post_journal(conn, payment['doc_ref'], payment['doc_date'], 'Payments', 'Outgoing Payment', id, "Supplier Payment", lines, session.get('user_id'), branch_id=payment['branch_id'])
            
            if payment['bank_account_id']:
                conn.execute('UPDATE bank_accounts SET current_balance = current_balance - ? WHERE id=?', (amount, payment['bank_account_id']))
                conn.execute('''
                    INSERT INTO bank_transactions(doc_ref, doc_date, bank_account_id, txn_type, amount, description, reference, partner_id, journal_id)
                    VALUES(?,?,?,?,?,?,?,?,?)
                ''', (payment['doc_ref'], payment['doc_date'], payment['bank_account_id'], 'Withdrawal', -amount, f"Supplier Payment {payment['doc_ref']}", payment['reference'], payment['partner_id'], je_id))
            
            # Reduce partner's outstanding payables balance on payment
            if payment['partner_id']:
                conn.execute('UPDATE partners SET balance = balance - ? WHERE id=?', (amount, payment['partner_id']))
                
            conn.execute("UPDATE payments SET status='Posted', journal_id=?, posted_at=datetime('now') WHERE id=?", (je_id, id))
            conn.commit()
            log_audit(conn, session.get('user_id'), 'Payments', 'Post', 'Outgoing Payment', payment['doc_ref'], f'Paid supplier KES {amount:,.2f}')
            flash('Payment voucher posted successfully.', 'success')
            
    except Exception as e:
        conn.rollback()
        flash(f'Error posting payment: {e}', 'error')
        
    return redirect(url_for('payments.payment_detail', id=id))

post_incoming = post_payment
post_outgoing = post_payment

@payments_bp.route('/<int:id>/allocate', methods=['POST'])
@payments_bp.route('/incoming/<int:id>/allocate', methods=['POST'])
@payments_bp.route('/outgoing/<int:id>/allocate', methods=['POST'])
@login_required
def allocate_payment(id):
    conn = get_db()
    payment = conn.execute('SELECT * FROM payments WHERE id=?', (id,)).fetchone()
    if not payment:
        flash('Payment not found.', 'error')
        return redirect(url_for('payments.index'))
        
    try:
        total_allocated = 0
        for key in request.form:
            if key.startswith('alloc_'):
                doc_id = key.replace('alloc_', '')
                alloc_amt = request.form.get(key, 0, type=float)
                if alloc_amt > 0:
                    if payment['payment_type'] == 'Incoming':
                        doc = conn.execute('SELECT * FROM sales_docs WHERE id=?', (doc_id,)).fetchone()
                        if doc:
                            apply_amt = min(alloc_amt, doc['balance_due'], payment['unallocated'] - total_allocated)
                            if apply_amt > 0:
                                conn.execute('''
                                    INSERT INTO payment_allocations(payment_id, doc_type, doc_id, amount)
                                    VALUES(?, 'Invoice', ?, ?)
                                ''', (id, doc_id, apply_amt))
                                
                                new_paid = (doc['amount_paid'] or 0.0) + apply_amt
                                new_due = max(0.0, (doc['balance_due'] or 0.0) - apply_amt)
                                new_status = 'Paid' if new_due <= 0.001 else 'Partial'
                                conn.execute('UPDATE sales_docs SET amount_paid=?, balance_due=?, status=? WHERE id=?',
                                             (new_paid, new_due, new_status, doc_id))
                                # NOTE: partners.balance was already reduced when the payment was posted.
                                # Allocation only marks which invoice the payment is applied against.
                                total_allocated += apply_amt
                    elif payment['payment_type'] == 'Outgoing':
                        doc = conn.execute('SELECT * FROM purchase_docs WHERE id=?', (doc_id,)).fetchone()
                        if doc:
                            apply_amt = min(alloc_amt, doc['balance_due'], payment['unallocated'] - total_allocated)
                            if apply_amt > 0:
                                conn.execute('''
                                    INSERT INTO payment_allocations(payment_id, doc_type, doc_id, amount)
                                    VALUES(?, 'Bill', ?, ?)
                                ''', (id, doc_id, apply_amt))
                                new_paid = (doc['amount_paid'] or 0.0) + apply_amt
                                new_due = max(0.0, (doc['balance_due'] or 0.0) - apply_amt)
                                new_status = 'Paid' if new_due <= 0.001 else 'Partial'
                                conn.execute('UPDATE purchase_docs SET amount_paid=?, balance_due=?, status=? WHERE id=?',
                                             (new_paid, new_due, new_status, doc_id))
                                # NOTE: partners.balance was already reduced when the payment was posted.
                                total_allocated += apply_amt
                                
        if total_allocated > 0:
            new_total_alloc = payment['amount_allocated'] + total_allocated
            new_unalloc = max(0, payment['amount'] - new_total_alloc)
            conn.execute('UPDATE payments SET amount_allocated=?, unallocated=? WHERE id=?', (new_total_alloc, new_unalloc, id))
            conn.commit()
            flash(f'Allocated KES {total_allocated:,.2f} to invoice(s)/bill(s).', 'success')
        else:
            flash('No allocations entered.', 'warning')
    except Exception as e:
        conn.rollback()
        flash(f'Error allocating payment: {e}', 'error')
    return redirect(url_for('payments.payment_detail', id=id))

allocate_incoming = allocate_payment
allocate_outgoing = allocate_payment

@payments_bp.route('/outgoing')
@login_required
def outgoing():
    conn = get_db()
    payments_list = conn.execute('''
        SELECT p.*, bp.name as partner_name, m.name as method_name, b.name as account_name
        FROM payments p 
        LEFT JOIN partners bp ON p.partner_id=bp.id 
        LEFT JOIN payment_methods m ON p.payment_method_id=m.id
        LEFT JOIN bank_accounts b ON p.bank_account_id=b.id
        WHERE p.payment_type = 'Outgoing'
        ORDER BY p.doc_date DESC, p.id DESC
    ''').fetchall()
    return render_template('payments/payment_list.html', title='Outgoing Payments', payments=payments_list,
                           new_url=url_for('payments.new_outgoing'), ptype='Outgoing')

outgoing_list = outgoing

@payments_bp.route('/outgoing/new', methods=['GET', 'POST'])
@login_required
def new_outgoing():
    conn = get_db()
    if request.method == 'POST':
        partner_id = request.form.get('partner_id')
        doc_date = request.form.get('doc_date', date.today().isoformat())
        amount = request.form.get('amount', 0, type=float)
        method_id = request.form.get('payment_method_id')
        bank_acc_id = request.form.get('bank_account_id')
        branch_id = request.form.get('branch_id', type=int) or session.get('active_branch_id') or None
        reference = request.form.get('reference', '')
        remarks = request.form.get('remarks', '')
        
        if not partner_id or amount <= 0:
            flash('Supplier and valid amount required.', 'error')
        else:
            try:
                doc_ref = next_ref('Payments', 'Outgoing')
                cursor = conn.execute('''
                    INSERT INTO payments (doc_ref, doc_date, payment_type, partner_id, bank_account_id, payment_method_id, amount, amount_allocated, unallocated, reference, remarks, status, branch_id, posted_by)
                    VALUES (?, ?, 'Outgoing', ?, ?, ?, ?, 0, ?, ?, ?, 'Draft', ?, ?)
                ''', (doc_ref, doc_date, partner_id, bank_acc_id, method_id, amount, amount, reference, remarks, branch_id, session.get('user_id')))
                pay_id = cursor.lastrowid
                conn.commit()
                flash(f'Payment voucher {doc_ref} created.', 'success')
                return redirect(url_for('payments.view_outgoing', id=pay_id))
            except Exception as e:
                flash(f'Error creating outgoing payment: {e}', 'error')
    partners = conn.execute("SELECT * FROM partners WHERE partner_type IN ('Supplier', 'Customer & Supplier') AND status='Active' ORDER BY name").fetchall()
    methods = conn.execute('SELECT * FROM payment_methods WHERE is_active=1').fetchall()
    accounts = conn.execute('SELECT * FROM bank_accounts WHERE is_active=1').fetchall()
    branches = conn.execute("SELECT * FROM branches WHERE is_active=1 ORDER BY is_main DESC, name ASC").fetchall()
    return render_template('payments/payment_form.html', payment_type='Outgoing', title='New Supplier Payment',
                           partners=partners, payment_methods=methods, bank_accounts=accounts, coas=[], branches=branches)

@payments_bp.route('/outgoing/<int:id>')
@login_required
def view_outgoing(id):
    conn = get_db()
    payment = conn.execute('''
        SELECT p.*, bp.name as partner_name, m.name as method_name, b.name as account_name,
               br.name as branch_name, br.code as branch_code
        FROM payments p
        LEFT JOIN partners bp ON p.partner_id=bp.id
        LEFT JOIN payment_methods m ON p.payment_method_id=m.id
        LEFT JOIN bank_accounts b ON p.bank_account_id=b.id
        LEFT JOIN branches br ON p.branch_id=br.id
        WHERE p.id=?
    ''', (id,)).fetchone()
    
    outstanding_docs = []
    if payment and payment['partner_id']:
        outstanding_docs = conn.execute('''
            SELECT * FROM purchase_docs 
            WHERE supplier_id=? AND doc_type IN ('Invoice', 'Bill') AND status IN ('Posted', 'Partial') AND balance_due > 0
            ORDER BY doc_date
        ''', (payment['partner_id'],)).fetchall()
        
    allocations = conn.execute('''
        SELECT a.*, p.doc_ref as invoice_ref, p.doc_date as invoice_date, p.total as invoice_total
        FROM payment_allocations a
        JOIN purchase_docs p ON a.doc_id=p.id
        WHERE a.payment_id=?
    ''', (id,)).fetchall()
    
    return render_template('payments/payment_detail.html', payment=payment, 
                           outstanding_docs=outstanding_docs, allocations=allocations)

@payments_bp.route('/outgoing/<int:id>/post', methods=['POST'])
@login_required
def post_outgoing(id):
    conn = get_db()
    payment = conn.execute('SELECT * FROM payments WHERE id=?', (id,)).fetchone()
    if not payment or payment['status'] == 'Posted':
        flash('Payment already posted.', 'warning')
        return redirect(url_for('payments.view_outgoing', id=id))
        
    try:
        bank_acc = conn.execute('SELECT * FROM bank_accounts WHERE id=?', (payment['bank_account_id'],)).fetchone()
        bank_coa_id = bank_acc['coa_id'] if bank_acc and bank_acc['coa_id'] else conn.execute("SELECT id FROM coa WHERE code='1110'").fetchone()[0]
        ap_coa_id = conn.execute("SELECT id FROM coa WHERE code='2110'").fetchone()[0]
        
        amount = payment['amount']
        lines = [
            (ap_coa_id, payment['partner_id'], amount, 0, f"Payable reduction {payment['doc_ref']}"),
            (bank_coa_id, payment['partner_id'], 0, amount, f"Payment to supplier {payment['doc_ref']}")
        ]
        je_id = post_journal(conn, payment['doc_ref'], payment['doc_date'], 'Payments', 'Outgoing Payment', id, "Supplier Payment", lines, session.get('user_id'), branch_id=payment['branch_id'])
        
        if payment['bank_account_id']:
            conn.execute('UPDATE bank_accounts SET current_balance = current_balance - ? WHERE id=?', (amount, payment['bank_account_id']))
            conn.execute('''
                INSERT INTO bank_transactions(doc_ref, doc_date, bank_account_id, txn_type, amount, description, reference, partner_id, journal_id)
                VALUES(?,?,?,?,?,?,?,?,?)
            ''', (payment['doc_ref'], payment['doc_date'], payment['bank_account_id'], 'Withdrawal', -amount, f"Supplier Payment {payment['doc_ref']}", payment['reference'], payment['partner_id'], je_id))
            
        conn.execute("UPDATE payments SET status='Posted', journal_id=?, posted_at=datetime('now') WHERE id=?", (je_id, id))
        conn.commit()
        log_audit(conn, session.get('user_id'), 'Payments', 'Post', 'Outgoing Payment', payment['doc_ref'], f'Paid supplier KES {amount:,.2f}')
        flash('Payment posted successfully.', 'success')
    except Exception as e:
        flash(f'Error posting: {e}', 'error')
    return redirect(url_for('payments.view_outgoing', id=id))

@payments_bp.route('/outgoing/<int:id>/allocate', methods=['POST'])
@login_required
def allocate_outgoing(id):
    conn = get_db()
    payment = conn.execute('SELECT * FROM payments WHERE id=?', (id,)).fetchone()
    if not payment:
        return redirect(url_for('payments.outgoing'))
    try:
        total_allocated = 0
        for key in request.form:
            if key.startswith('alloc_'):
                doc_id = key.replace('alloc_', '')
                alloc_amt = request.form.get(key, 0, type=float)
                if alloc_amt > 0:
                    doc = conn.execute('SELECT * FROM purchase_docs WHERE id=?', (doc_id,)).fetchone()
                    if doc:
                        apply_amt = min(alloc_amt, doc['balance_due'], payment['unallocated'] - total_allocated)
                        if apply_amt > 0:
                            conn.execute('''
                                INSERT INTO payment_allocations(payment_id, doc_type, doc_id, amount)
                                VALUES(?, 'Bill', ?, ?)
                            ''', (id, doc_id, apply_amt))
                            new_paid = (doc['amount_paid'] or 0.0) + apply_amt
                            new_due = max(0.0, (doc['balance_due'] or 0.0) - apply_amt)
                            new_status = 'Paid' if new_due <= 0.001 else 'Partial'
                            conn.execute('UPDATE purchase_docs SET amount_paid=?, balance_due=?, status=? WHERE id=?',
                                         (new_paid, new_due, new_status, doc_id))
                            conn.execute('UPDATE partners SET balance = balance - ? WHERE id=?', (apply_amt, payment['partner_id']))
                            total_allocated += apply_amt
        if total_allocated > 0:
            new_total_alloc = payment['amount_allocated'] + total_allocated
            new_unalloc = max(0, payment['amount'] - new_total_alloc)
            conn.execute('UPDATE payments SET amount_allocated=?, unallocated=? WHERE id=?', (new_total_alloc, new_unalloc, id))
            conn.commit()
            flash(f'Allocated KES {total_allocated:,.2f} to bills.', 'success')
    except Exception as e:
        flash(f'Error allocating: {e}', 'error')
    return redirect(url_for('payments.view_outgoing', id=id))

@payments_bp.route('/expenses')
@login_required
def expenses():
    conn = get_db()
    payments_list = conn.execute('''
        SELECT p.*, bp.name as partner_name, m.name as method_name, b.name as account_name
        FROM payments p 
        LEFT JOIN partners bp ON p.partner_id=bp.id 
        LEFT JOIN payment_methods m ON p.payment_method_id=m.id
        LEFT JOIN bank_accounts b ON p.bank_account_id=b.id
        WHERE p.payment_type = 'Expense'
        ORDER BY p.doc_date DESC, p.id DESC
    ''').fetchall()
    return render_template('payments/payment_list.html', title='Expense Payments', payments=payments_list,
                           new_url=url_for('payments.new_expense'), ptype='Expense')

expense_list = expenses

@payments_bp.route('/expenses/new', methods=['GET', 'POST'])
@login_required
def new_expense():
    conn = get_db()
    if request.method == 'POST':
        exp_account_id = request.form.get('expense_account_id')
        partner_id = request.form.get('partner_id') or None
        doc_date = request.form.get('doc_date', date.today().isoformat())
        amount = request.form.get('amount', 0, type=float)
        method_id = request.form.get('payment_method_id')
        bank_acc_id = request.form.get('bank_account_id')
        branch_id = request.form.get('branch_id', type=int) or session.get('active_branch_id') or None
        desc = request.form.get('description', '')
        ref = request.form.get('reference', '')
        
        if not exp_account_id or amount <= 0:
            flash('Expense account and valid amount are required.', 'error')
        else:
            try:
                doc_ref = next_ref('Payments', 'Expense')
                bank_acc = conn.execute('SELECT * FROM bank_accounts WHERE id=?', (bank_acc_id,)).fetchone()
                bank_coa_id = bank_acc['coa_id'] if bank_acc and bank_acc['coa_id'] else conn.execute("SELECT id FROM coa WHERE code='1110'").fetchone()[0]
                
                lines = [
                    (exp_account_id, partner_id, amount, 0, f"Expense: {desc}"),
                    (bank_coa_id, partner_id, 0, amount, f"Payment via {bank_acc['name'] if bank_acc else 'Cash'}")
                ]
                je_id = post_journal(conn, doc_ref, doc_date, 'Payments', 'Expense Payment', None, desc, lines, session.get('user_id'), branch_id=branch_id)
                
                if bank_acc_id:
                    conn.execute('UPDATE bank_accounts SET current_balance = current_balance - ? WHERE id=?', (amount, bank_acc_id))
                    conn.execute('''
                        INSERT INTO bank_transactions(doc_ref, doc_date, bank_account_id, txn_type, amount, description, reference, partner_id, journal_id)
                        VALUES(?,?,?,?,?,?,?,?,?)
                    ''', (doc_ref, doc_date, bank_acc_id, 'Withdrawal', -amount, f"Expense: {desc}", ref, partner_id, je_id))
                    
                conn.execute('''
                    INSERT INTO payments (doc_ref, doc_date, payment_type, partner_id, bank_account_id, payment_method_id, amount, amount_allocated, unallocated, reference, remarks, status, branch_id, journal_id, posted_by, posted_at)
                    VALUES (?, ?, 'Expense', ?, ?, ?, ?, ?, 0, ?, ?, 'Posted', ?, ?, ?, datetime('now'))
                ''', (doc_ref, doc_date, partner_id, bank_acc_id, method_id, amount, amount, ref, desc, branch_id, je_id, session.get('user_id')))
                conn.commit()
                log_audit(conn, session.get('user_id'), 'Payments', 'Post', 'Expense', doc_ref, f'Paid expense KES {amount:,.2f}')
                flash(f'Expense payment {doc_ref} posted successfully.', 'success')
                return redirect(url_for('payments.expenses'))
            except Exception as e:
                flash(f'Error recording expense: {e}', 'error')
    coas = conn.execute("SELECT * FROM coa WHERE is_posting=1 AND account_type='Expense' ORDER BY code").fetchall()
    partners = conn.execute("SELECT * FROM partners WHERE status='Active' ORDER BY name").fetchall()
    methods = conn.execute('SELECT * FROM payment_methods WHERE is_active=1').fetchall()
    accounts = conn.execute('SELECT * FROM bank_accounts WHERE is_active=1').fetchall()
    branches = conn.execute("SELECT * FROM branches WHERE is_active=1 ORDER BY is_main DESC, name ASC").fetchall()
    return render_template('payments/payment_form.html', payment_type='Expense', title='Record Expense Payment',
                           coas=coas, partners=partners, payment_methods=methods, bank_accounts=accounts, branches=branches)

@payments_bp.route('/<int:id>/print')
@login_required
def print_payment(id):
    conn = get_db()
    payment = conn.execute('''
        SELECT p.*, bp.name as partner_name, bp.address as partner_address, bp.phone as partner_phone,
               m.name as method_name, b.name as account_name, u.full_name as created_by_name
        FROM payments p
        LEFT JOIN partners bp ON p.partner_id=bp.id
        LEFT JOIN payment_methods m ON p.payment_method_id=m.id
        LEFT JOIN bank_accounts b ON p.bank_account_id=b.id
        LEFT JOIN users u ON p.posted_by=u.id
        WHERE p.id=?
    ''', (id,)).fetchone()
    company = conn.execute('SELECT * FROM company WHERE id=1').fetchone()
    allocations = conn.execute('''
        SELECT a.*, COALESCE(s.doc_ref, p.doc_ref) as invoice_ref, COALESCE(s.total, p.total) as invoice_total
        FROM payment_allocations a
        LEFT JOIN sales_docs s ON a.doc_id=s.id AND a.doc_type='Invoice'
        LEFT JOIN purchase_docs p ON a.doc_id=p.id AND a.doc_type='Bill'
        WHERE a.payment_id=?
    ''', (id,)).fetchall()
    return render_template('payments/payment_print.html', payment=payment, company=company, allocations=allocations)
