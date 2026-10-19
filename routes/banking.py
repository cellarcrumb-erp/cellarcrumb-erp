from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from functools import wraps
from database import get_db, next_ref, post_journal, log_audit
from datetime import date

banking_bp = Blueprint('banking', __name__, url_prefix='/banking')

from auth import login_required, require_permission

@banking_bp.route('/')
@login_required
@require_permission('banking', 'read')
def index():
    conn = get_db()
    accounts = conn.execute('SELECT * FROM bank_accounts WHERE is_active=1').fetchall()
    return render_template('banking/index.html', accounts=accounts)

@banking_bp.route('/accounts')
@login_required
def accounts():
    conn = get_db()
    accounts_list = conn.execute('SELECT b.*, c.name as coa_name FROM bank_accounts b LEFT JOIN coa c ON b.coa_id=c.id').fetchall()
    return render_template('banking/accounts.html', accounts=accounts_list)

@banking_bp.route('/accounts/new', methods=['GET', 'POST'])
@login_required
def new_account():
    conn = get_db()
    if request.method == 'POST':
        code = request.form.get('code')
        name = request.form.get('name')
        acc_type = request.form.get('account_type', 'Bank')
        bank_name = request.form.get('bank_name', '')
        acc_num = request.form.get('account_number', '')
        currency = request.form.get('currency', 'KES')
        coa_id = request.form.get('coa_id')
        opening = request.form.get('opening_balance', 0, type=float)
        
        try:
            conn.execute('''INSERT INTO bank_accounts 
                (code, name, account_type, bank_name, account_number, currency, coa_id, opening_balance, current_balance, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)''', 
                (code, name, acc_type, bank_name, acc_num, currency, coa_id, opening, opening))
            conn.commit()
            log_audit(conn, session.get('user_id'), 'Banking', 'Create', 'Account', code, f'Created account {name}')
            flash('Bank account created successfully.', 'success')
            return redirect(url_for('banking.accounts'))
        except Exception as e:
            flash(f'Error creating account: {e}', 'error')
    coas = conn.execute("SELECT * FROM coa WHERE is_posting=1 AND account_type='Asset'").fetchall()
    curs = conn.execute("SELECT * FROM currencies").fetchall()
    return render_template('banking/account_form.html', account=None, coas=coas, currencies=curs)

@banking_bp.route('/accounts/<int:id>')
@login_required
def view_account(id):
    conn = get_db()
    acc = conn.execute('SELECT b.*, c.name as coa_name FROM bank_accounts b LEFT JOIN coa c ON b.coa_id=c.id WHERE b.id = ?', (id,)).fetchone()
    txns = conn.execute('SELECT * FROM bank_transactions WHERE bank_account_id = ? ORDER BY doc_date DESC, id DESC', (id,)).fetchall()
    return render_template('banking/account_detail.html', account=acc, transactions=txns)

account_detail = view_account

@banking_bp.route('/accounts/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_account(id):
    conn = get_db()
    if request.method == 'POST':
        name = request.form.get('name')
        bank_name = request.form.get('bank_name')
        acc_num = request.form.get('account_number')
        coa_id = request.form.get('coa_id')
        is_active = 1 if request.form.get('is_active') else 0
        conn.execute('UPDATE bank_accounts SET name=?, bank_name=?, account_number=?, coa_id=?, is_active=? WHERE id=?',
                     (name, bank_name, acc_num, coa_id, is_active, id))
        conn.commit()
        flash('Account updated successfully.', 'success')
        return redirect(url_for('banking.accounts'))
    acc = conn.execute('SELECT * FROM bank_accounts WHERE id=?', (id,)).fetchone()
    coas = conn.execute("SELECT * FROM coa WHERE is_posting=1 AND account_type='Asset'").fetchall()
    curs = conn.execute("SELECT * FROM currencies").fetchall()
    return render_template('banking/account_form.html', account=acc, coas=coas, currencies=curs)

@banking_bp.route('/deposits')
@login_required
def deposits():
    conn = get_db()
    txns = conn.execute('''
        SELECT t.*, b.name as account_name, bp.name as partner_name 
        FROM bank_transactions t 
        JOIN bank_accounts b ON t.bank_account_id=b.id 
        LEFT JOIN partners bp ON t.partner_id=bp.id
        WHERE t.txn_type = 'Deposit' 
        ORDER BY t.doc_date DESC, t.id DESC
    ''').fetchall()
    return render_template('banking/txn_list.html', title='Bank Deposits', transactions=txns, txn_type='Deposit')

@banking_bp.route('/deposits/new', methods=['GET', 'POST'])
@login_required
def new_deposit():
    conn = get_db()
    if request.method == 'POST':
        bank_acc_id = request.form.get('bank_account_id')
        doc_date = request.form.get('doc_date', date.today().isoformat())
        amount = request.form.get('amount', 0, type=float)
        desc = request.form.get('description', '')
        ref = request.form.get('reference', '')
        partner_id = request.form.get('partner_id') or None
        
        if not bank_acc_id or amount <= 0:
            flash('Please select an account and specify a valid deposit amount.', 'error')
        else:
            try:
                doc_ref = next_ref('Banking', 'Deposit')
                bank_acc = conn.execute('SELECT * FROM bank_accounts WHERE id=?', (bank_acc_id,)).fetchone()
                cash_coa_id = conn.execute("SELECT id FROM coa WHERE code='1110'").fetchone()[0]
                bank_coa_id = bank_acc['coa_id'] or conn.execute("SELECT id FROM coa WHERE code='1120'").fetchone()[0]
                
                # Journal: Dr Bank Account / Cr Cash on Hand
                lines = [
                    (bank_coa_id, partner_id, amount, 0, f'Deposit to {bank_acc["name"]} - {desc}'),
                    (cash_coa_id, partner_id, 0, amount, f'Cash deposit - {desc}')
                ]
                je_id = post_journal(conn, doc_ref, doc_date, 'Banking', 'Deposit', None, f'Deposit: {desc}', lines, session.get('user_id'))
                
                # Update bank balance
                conn.execute('UPDATE bank_accounts SET current_balance = current_balance + ? WHERE id=?', (amount, bank_acc_id))
                
                # Insert bank txn
                conn.execute('''
                    INSERT INTO bank_transactions(doc_ref, doc_date, bank_account_id, txn_type, amount, description, reference, partner_id, journal_id)
                    VALUES(?,?,?,?,?,?,?,?,?)
                ''', (doc_ref, doc_date, bank_acc_id, 'Deposit', amount, desc, ref, partner_id, je_id))
                conn.commit()
                log_audit(conn, session.get('user_id'), 'Banking', 'Post', 'Deposit', doc_ref, f'Deposited KES {amount:,.2f}')
                flash(f'Deposit {doc_ref} of KES {amount:,.2f} recorded successfully.', 'success')
                return redirect(url_for('banking.deposits'))
            except Exception as e:
                flash(f'Error recording deposit: {e}', 'error')
    accs = conn.execute('SELECT * FROM bank_accounts WHERE is_active=1').fetchall()
    partners = conn.execute('SELECT * FROM partners WHERE status="Active" ORDER BY name').fetchall()
    return render_template('banking/deposit_form.html', accounts=accs, partners=partners)

@banking_bp.route('/deposits/<int:id>')
@login_required
def view_deposit(id):
    conn = get_db()
    txn = conn.execute('''
        SELECT t.*, b.name as account_name, bp.name as partner_name, j.doc_ref as journal_ref
        FROM bank_transactions t
        JOIN bank_accounts b ON t.bank_account_id=b.id
        LEFT JOIN partners bp ON t.partner_id=bp.id
        LEFT JOIN journal_entries j ON t.journal_id=j.id
        WHERE t.id=?
    ''', (id,)).fetchone()
    return redirect(url_for('banking.deposits'))

deposit_detail = view_deposit

@banking_bp.route('/withdrawals')
@login_required
def withdrawals():
    conn = get_db()
    txns = conn.execute('''
        SELECT t.*, b.name as account_name, bp.name as partner_name 
        FROM bank_transactions t 
        JOIN bank_accounts b ON t.bank_account_id=b.id 
        LEFT JOIN partners bp ON t.partner_id=bp.id
        WHERE t.txn_type = 'Withdrawal' 
        ORDER BY t.doc_date DESC, t.id DESC
    ''').fetchall()
    return render_template('banking/txn_list.html', title='Bank Withdrawals', transactions=txns, txn_type='Withdrawal')

@banking_bp.route('/withdrawals/new', methods=['GET', 'POST'])
@login_required
def new_withdrawal():
    conn = get_db()
    if request.method == 'POST':
        bank_acc_id = request.form.get('bank_account_id')
        doc_date = request.form.get('doc_date', date.today().isoformat())
        amount = request.form.get('amount', 0, type=float)
        desc = request.form.get('description', '')
        ref = request.form.get('reference', '')
        partner_id = request.form.get('partner_id') or None
        
        if not bank_acc_id or amount <= 0:
            flash('Please select an account and valid withdrawal amount.', 'error')
        else:
            try:
                doc_ref = next_ref('Banking', 'Withdrawal')
                bank_acc = conn.execute('SELECT * FROM bank_accounts WHERE id=?', (bank_acc_id,)).fetchone()
                cash_coa_id = conn.execute("SELECT id FROM coa WHERE code='1110'").fetchone()[0]
                bank_coa_id = bank_acc['coa_id'] or conn.execute("SELECT id FROM coa WHERE code='1120'").fetchone()[0]
                
                # Journal: Dr Cash on Hand / Cr Bank Account
                lines = [
                    (cash_coa_id, partner_id, amount, 0, f'Cash withdrawal from {bank_acc["name"]} - {desc}'),
                    (bank_coa_id, partner_id, 0, amount, f'Withdrawal - {desc}')
                ]
                je_id = post_journal(conn, doc_ref, doc_date, 'Banking', 'Withdrawal', None, f'Withdrawal: {desc}', lines, session.get('user_id'))
                
                # Update bank balance
                conn.execute('UPDATE bank_accounts SET current_balance = current_balance - ? WHERE id=?', (amount, bank_acc_id))
                
                # Insert bank txn
                conn.execute('''
                    INSERT INTO bank_transactions(doc_ref, doc_date, bank_account_id, txn_type, amount, description, reference, partner_id, journal_id)
                    VALUES(?,?,?,?,?,?,?,?,?)
                ''', (doc_ref, doc_date, bank_acc_id, 'Withdrawal', amount, desc, ref, partner_id, je_id))
                conn.commit()
                log_audit(conn, session.get('user_id'), 'Banking', 'Post', 'Withdrawal', doc_ref, f'Withdrew KES {amount:,.2f}')
                flash(f'Withdrawal {doc_ref} of KES {amount:,.2f} recorded.', 'success')
                return redirect(url_for('banking.withdrawals'))
            except Exception as e:
                flash(f'Error recording withdrawal: {e}', 'error')
    accs = conn.execute('SELECT * FROM bank_accounts WHERE is_active=1').fetchall()
    partners = conn.execute('SELECT * FROM partners WHERE status="Active" ORDER BY name').fetchall()
    return render_template('banking/withdrawal_form.html', accounts=accs, partners=partners)

@banking_bp.route('/withdrawals/<int:id>')
@login_required
def view_withdrawal(id):
    return redirect(url_for('banking.withdrawals'))

withdrawal_detail = view_withdrawal

@banking_bp.route('/transfers')
@login_required
def transfers():
    conn = get_db()
    txns = conn.execute('''
        SELECT t.*, b.name as account_name 
        FROM bank_transactions t 
        JOIN bank_accounts b ON t.bank_account_id=b.id 
        WHERE t.txn_type = 'Transfer' 
        ORDER BY t.doc_date DESC, t.id DESC
    ''').fetchall()
    return render_template('banking/txn_list.html', title='Account Transfers', transactions=txns, txn_type='Transfer')

@banking_bp.route('/transfers/new', methods=['GET', 'POST'])
@login_required
def new_transfer():
    conn = get_db()
    if request.method == 'POST':
        from_id = request.form.get('from_account_id')
        to_id = request.form.get('to_account_id')
        doc_date = request.form.get('doc_date', date.today().isoformat())
        amount = request.form.get('amount', 0, type=float)
        desc = request.form.get('description', 'Inter-account transfer')
        ref = request.form.get('reference', '')
        
        if from_id == to_id:
            flash('Source and destination accounts must be different.', 'error')
        elif amount <= 0:
            flash('Transfer amount must be greater than zero.', 'error')
        else:
            try:
                from_acc = conn.execute('SELECT * FROM bank_accounts WHERE id=?', (from_id,)).fetchone()
                to_acc = conn.execute('SELECT * FROM bank_accounts WHERE id=?', (to_id,)).fetchone()
                
                doc_ref = next_ref('Banking', 'Transfer')
                lines = [
                    (to_acc['coa_id'], None, amount, 0, f'Transfer from {from_acc["name"]} to {to_acc["name"]}'),
                    (from_acc['coa_id'], None, 0, amount, f'Transfer from {from_acc["name"]} to {to_acc["name"]}')
                ]
                je_id = post_journal(conn, doc_ref, doc_date, 'Banking', 'Transfer', None, f'Transfer: {from_acc["name"]} -> {to_acc["name"]}', lines, session.get('user_id'))
                
                conn.execute('UPDATE bank_accounts SET current_balance = current_balance - ? WHERE id=?', (amount, from_id))
                conn.execute('UPDATE bank_accounts SET current_balance = current_balance + ? WHERE id=?', (amount, to_id))
                
                conn.execute('''
                    INSERT INTO bank_transactions(doc_ref, doc_date, bank_account_id, txn_type, amount, description, reference, journal_id)
                    VALUES(?,?,?,?,?,?,?,?)
                ''', (doc_ref, doc_date, from_id, 'Transfer', -amount, f'Transfer to {to_acc["name"]}', ref, je_id))
                
                conn.execute('''
                    INSERT INTO bank_transactions(doc_ref, doc_date, bank_account_id, txn_type, amount, description, reference, journal_id)
                    VALUES(?,?,?,?,?,?,?,?)
                ''', (doc_ref, doc_date, to_id, 'Transfer', amount, f'Transfer from {from_acc["name"]}', ref, je_id))
                
                conn.commit()
                log_audit(conn, session.get('user_id'), 'Banking', 'Post', 'Transfer', doc_ref, f'Transferred KES {amount:,.2f} from {from_acc["name"]} to {to_acc["name"]}')
                flash(f'Transfer {doc_ref} completed successfully.', 'success')
                return redirect(url_for('banking.transfers'))
            except Exception as e:
                flash(f'Error executing transfer: {e}', 'error')
    accs = conn.execute('SELECT * FROM bank_accounts WHERE is_active=1').fetchall()
    return render_template('banking/transfer_form.html', accounts=accs)

@banking_bp.route('/transfers/<int:id>')
@login_required
def view_transfer(id):
    return redirect(url_for('banking.transfers'))

transfer_detail = view_transfer

@banking_bp.route('/reconciliation')
@login_required
def reconciliation():
    conn = get_db()
    acc_id = request.args.get('account_id', type=int)
    accs = conn.execute('SELECT * FROM bank_accounts WHERE is_active=1').fetchall()
    selected_acc = None
    unreconciled = []
    reconciled_count = 0
    statement_balance = 0
    if acc_id:
        selected_acc = conn.execute('SELECT * FROM bank_accounts WHERE id=?', (acc_id,)).fetchone()
        unreconciled = conn.execute('SELECT * FROM bank_transactions WHERE bank_account_id=? AND is_reconciled=0 ORDER BY doc_date', (acc_id,)).fetchall()
        reconciled_count = conn.execute('SELECT COUNT(*) FROM bank_transactions WHERE bank_account_id=? AND is_reconciled=1', (acc_id,)).fetchone()[0]
        statement_balance = selected_acc['current_balance'] if selected_acc else 0
    return render_template('banking/reconciliation.html', accounts=accs, account=selected_acc, 
                           unreconciled=unreconciled, reconciled_count=reconciled_count, statement_balance=statement_balance)

@banking_bp.route('/reconciliation/mark', methods=['POST'])
@login_required
def mark_reconciled():
    conn = get_db()
    acc_id = request.form.get('account_id')
    txn_ids = request.form.getlist('txn_ids')
    if txn_ids:
        placeholders = ','.join(['?'] * len(txn_ids))
        now = date.today().isoformat()
        conn.execute(f'UPDATE bank_transactions SET is_reconciled=1, reconciled_at=? WHERE id IN ({placeholders})', [now] + txn_ids)
        conn.commit()
        flash(f'Marked {len(txn_ids)} transaction(s) as reconciled.', 'success')
    return redirect(url_for('banking.reconciliation', account_id=acc_id))

@banking_bp.route('/cashbook')
@login_required
def cashbook():
    conn = get_db()
    acc_id = request.args.get('account_id', type=int)
    from_date = request.args.get('from_date', date.today().replace(day=1).isoformat())
    to_date = request.args.get('to_date', date.today().isoformat())
    
    accs = conn.execute('SELECT * FROM bank_accounts WHERE is_active=1').fetchall()
    selected_acc = None
    transactions = []
    opening_bal = 0
    closing_bal = 0
    
    if acc_id:
        selected_acc = conn.execute('SELECT * FROM bank_accounts WHERE id=?', (acc_id,)).fetchone()
        txns = conn.execute('''
            SELECT * FROM bank_transactions 
            WHERE bank_account_id=? AND doc_date BETWEEN ? AND ? 
            ORDER BY doc_date, id
        ''', (acc_id, from_date, to_date)).fetchall()
        
        opening_bal = selected_acc['opening_balance']
        running = opening_bal
        for t in txns:
            amount = t['amount']
            is_receipt = amount > 0
            running += amount
            transactions.append({
                'doc_date': t['doc_date'],
                'doc_ref': t['doc_ref'],
                'description': t['description'],
                'receipt': amount if is_receipt else 0,
                'payment': abs(amount) if not is_receipt else 0,
                'balance': running
            })
        closing_bal = running
        
    return render_template('banking/cashbook.html', accounts=accs, account=selected_acc, 
                           transactions=transactions, opening_balance=opening_bal, closing_balance=closing_bal,
                           from_date=from_date, to_date=to_date)
