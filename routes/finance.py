from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, Response
from functools import wraps
from database import get_db, next_ref, post_journal, log_audit
import csv
from io import StringIO
from datetime import datetime, date, timedelta

finance_bp = Blueprint('finance', __name__, url_prefix='/finance')

from auth import login_required, require_permission

def get_branch_filter(req, sess):
    branch_id = req.args.get('branch_id')
    if branch_id and branch_id.isdigit():
        return int(branch_id)
    active_b = sess.get('active_branch_id')
    if active_b and int(active_b) > 0:
        return int(active_b)
    return None

@finance_bp.route('/')
@login_required
@require_permission('finance', 'read')
def index():
    db = get_db()
    today = date.today()
    this_year = f"{today.year}-01-01"
    
    # Quick KPI calculations
    # 1. Total Revenue YTD
    rev_row = db.execute("""
        SELECT COALESCE(SUM(jl.credit - jl.debit), 0) as rev
        FROM journal_lines jl
        JOIN journal_entries je ON jl.journal_id = je.id
        JOIN coa c ON jl.account_id = c.id
        WHERE je.status = 'Posted' AND c.account_type = 'Revenue' AND je.doc_date >= ?
    """, (this_year,)).fetchone()
    revenue_ytd = rev_row['rev'] if rev_row else 0.0

    # 2. Total Expense YTD
    exp_row = db.execute("""
        SELECT COALESCE(SUM(jl.debit - jl.credit), 0) as exp
        FROM journal_lines jl
        JOIN journal_entries je ON jl.journal_id = je.id
        JOIN coa c ON jl.account_id = c.id
        WHERE je.status = 'Posted' AND c.account_type = 'Expense' AND je.doc_date >= ?
    """, (this_year,)).fetchone()
    expenses_ytd = exp_row['exp'] if exp_row else 0.0
    net_profit_ytd = revenue_ytd - expenses_ytd

    # 3. Total AR Outstanding
    ar_row = db.execute("""
        SELECT COALESCE(SUM(balance_due), 0) as total_ar, COUNT(*) as cnt
        FROM sales_docs
        WHERE doc_type = 'Invoice' AND status = 'Posted' AND balance_due > 0
    """).fetchone()
    total_ar = ar_row['total_ar'] if ar_row else 0.0
    ar_count = ar_row['cnt'] if ar_row else 0

    # 4. Total AP Outstanding
    ap_row = db.execute("""
        SELECT COALESCE(SUM(balance_due), 0) as total_ap, COUNT(*) as cnt
        FROM purchase_docs
        WHERE doc_type = 'Invoice' AND status = 'Posted' AND balance_due > 0
    """).fetchone()
    total_ap = ap_row['total_ap'] if ap_row else 0.0
    ap_count = ap_row['cnt'] if ap_row else 0

    company = db.execute("SELECT currency FROM company LIMIT 1").fetchone()
    currency = company['currency'] if company and company['currency'] else 'KES'

    return render_template('finance/index.html',
                           revenue_ytd=revenue_ytd, expenses_ytd=expenses_ytd, net_profit_ytd=net_profit_ytd,
                           total_ar=total_ar, ar_count=ar_count,
                           total_ap=total_ap, ap_count=ap_count,
                           currency=currency)

# --- Chart of Accounts ---
@finance_bp.route('/coa')
@login_required
@require_permission('finance', 'read')
def coa_list():
    db = get_db()
    accounts = db.execute('SELECT * FROM coa ORDER BY code').fetchall()
    return render_template('finance/coa_list.html', accounts=accounts)

@finance_bp.route('/coa/new', methods=['GET', 'POST'])
@login_required
@require_permission('finance', 'write')
def coa_new():
    db = get_db()
    if request.method == 'POST':
        code = request.form.get('code')
        name = request.form.get('name')
        account_type = request.form.get('account_type')
        parent_id = request.form.get('parent_id')
        is_posting = 1 if request.form.get('is_posting') else 0
        opening_balance = float(request.form.get('opening_balance', 0))
        
        db.execute('''
            INSERT INTO coa (code, name, account_type, parent_id, is_posting, opening_balance, is_active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
        ''', (code, name, account_type, parent_id if parent_id else None, is_posting, opening_balance))
        db.commit()
        flash('Account created.', 'success')
        return redirect(url_for('finance.coa_list'))
        
    parents = db.execute('SELECT id, code, name FROM coa WHERE is_posting = 0').fetchall()
    return render_template('finance/coa_form.html', parents=parents, account=None)

@finance_bp.route('/coa/<int:id>')
@login_required
def coa_detail(id):
    db = get_db()
    account = db.execute('SELECT * FROM coa WHERE id = ?', (id,)).fetchone()
    lines = db.execute('''
        SELECT jl.*, je.doc_ref, je.doc_date, je.description as je_desc
        FROM journal_lines jl
        JOIN journal_entries je ON jl.journal_id = je.id
        WHERE jl.account_id = ?
        ORDER BY je.doc_date DESC, je.id DESC LIMIT 100
    ''', (id,)).fetchall()
    return render_template('finance/coa_detail.html', account=account, lines=lines)

@finance_bp.route('/coa/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@require_permission('finance', 'write')
def coa_edit(id):
    db = get_db()
    account = db.execute('SELECT * FROM coa WHERE id = ?', (id,)).fetchone()
    if request.method == 'POST':
        name = request.form.get('name')
        db.execute('UPDATE coa SET name = ? WHERE id = ?', (name, id))
        db.commit()
        flash('Account updated.', 'success')
        return redirect(url_for('finance.coa_detail', id=id))
    parents = db.execute('SELECT id, code, name FROM coa WHERE is_posting = 0').fetchall()
    return render_template('finance/coa_form.html', account=account, parents=parents)

# --- Journal Entries ---
@finance_bp.route('/journals')
@login_required
def journal_list():
    db = get_db()
    status = request.args.get('status', '')
    query = 'SELECT * FROM journal_entries'
    params = []
    if status:
        query += ' WHERE status = ?'
        params.append(status)
    query += ' ORDER BY doc_date DESC, id DESC'
    journals = db.execute(query, params).fetchall()
    return render_template('finance/journal_list.html', journals=journals, status=status)

@finance_bp.route('/journals/new', methods=['GET', 'POST'])
@login_required
@require_permission('finance', 'write')
def journal_new():
    db = get_db()
    if request.method == 'POST':
        doc_date = request.form.get('doc_date')
        description = request.form.get('description')
        
        account_ids = request.form.getlist('account_id[]')
        debits = request.form.getlist('debit[]')
        credits = request.form.getlist('credit[]')
        line_descs = request.form.getlist('line_description[]')
        
        total_dr = sum(float(d or 0) for d in debits)
        total_cr = sum(float(c or 0) for c in credits)
        
        if abs(total_dr - total_cr) > 0.01:
            flash('Journal must balance.', 'error')
            return redirect(request.url)
            
        doc_ref = next_ref('Finance', 'Journal', db)
        
        lines = []
        for i in range(len(account_ids)):
            if account_ids[i] and (float(debits[i] or 0) > 0 or float(credits[i] or 0) > 0):
                lines.append({
                    'account_id': account_ids[i],
                    'debit': float(debits[i] or 0),
                    'credit': float(credits[i] or 0),
                    'partner_id': None,
                    'description': line_descs[i] if line_descs[i] else description
                })
                
        active_branch = session.get('active_branch_id')
        post_journal(db, doc_ref, doc_date, 'Finance', 'ManualJournal', None, description, lines, session['user_id'], branch_id=active_branch)
        db.commit()
        flash('Journal entry posted.', 'success')
        return redirect(url_for('finance.journal_list'))
        
    accounts = db.execute('SELECT id, code, name FROM coa WHERE is_posting = 1 ORDER BY code').fetchall()
    return render_template('finance/journal_form.html', accounts=accounts)

@finance_bp.route('/journals/<int:id>')
@login_required
def journal_detail(id):
    db = get_db()
    journal = db.execute('SELECT * FROM journal_entries WHERE id = ?', (id,)).fetchone()
    lines = db.execute('''
        SELECT jl.*, c.code, c.name as account_name
        FROM journal_lines jl
        JOIN coa c ON jl.account_id = c.id
        WHERE jl.journal_id = ?
    ''', (id,)).fetchall()
    return render_template('finance/journal_detail.html', journal=journal, lines=lines)

# --- 1. PROFIT AND LOSS (INCOME STATEMENT) ---
@finance_bp.route('/profit-loss')
@login_required
@require_permission('finance', 'read')
def profit_loss():
    db = get_db()
    today = date.today()
    start_date = request.args.get('start_date') or f"{today.year}-01-01"
    end_date = request.args.get('end_date') or today.isoformat()
    branch_id = get_branch_filter(request, session)
    
    branch_clause = ""
    params = [start_date, end_date]
    if branch_id:
        branch_clause = " AND je.branch_id = ?"
        params.append(branch_id)
        
    query = f"""
        SELECT c.id, c.code, c.name, c.account_type,
               COALESCE(SUM(jl.debit), 0) as total_debit,
               COALESCE(SUM(jl.credit), 0) as total_credit
        FROM coa c
        JOIN journal_lines jl ON c.id = jl.account_id
        JOIN journal_entries je ON jl.journal_id = je.id
        WHERE je.status = 'Posted'
          AND je.doc_date >= ? AND je.doc_date <= ?
          {branch_clause}
          AND c.account_type IN ('Revenue', 'Expense')
        GROUP BY c.id, c.code, c.name, c.account_type
        ORDER BY c.code ASC
    """
    rows = db.execute(query, params).fetchall()
    
    revenues = []
    cogs = []
    operating_expenses = []
    
    total_revenue = 0.0
    total_cogs = 0.0
    total_operating_expenses = 0.0
    
    for r in rows:
        acc_type = r['account_type']
        code = str(r['code'])
        name = r['name']
        debit = float(r['total_debit'])
        credit = float(r['total_credit'])
        
        if acc_type == 'Revenue':
            amount = credit - debit
            if abs(amount) > 0.001:
                revenues.append({'code': code, 'name': name, 'amount': amount})
                total_revenue += amount
        elif acc_type == 'Expense':
            amount = debit - credit
            if abs(amount) > 0.001:
                if code.startswith('51') or 'cost of goods' in name.lower() or 'cogs' in name.lower():
                    cogs.append({'code': code, 'name': name, 'amount': amount})
                    total_cogs += amount
                else:
                    operating_expenses.append({'code': code, 'name': name, 'amount': amount})
                    total_operating_expenses += amount
                    
    gross_profit = total_revenue - total_cogs
    gross_margin = (gross_profit / total_revenue * 100.0) if total_revenue > 0 else 0.0
    net_profit = gross_profit - total_operating_expenses
    net_margin = (net_profit / total_revenue * 100.0) if total_revenue > 0 else 0.0
    
    branches = db.execute("SELECT id, name, code FROM branches WHERE is_active = 1 ORDER BY id").fetchall()
    company = db.execute("SELECT * FROM company LIMIT 1").fetchone()
    currency = company['currency'] if company and company['currency'] else 'KES'
    
    if request.args.get('export') == 'csv':
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow([f"Profit and Loss Statement - {company['name'] if company else 'Cellar & Crumb'}"])
        writer.writerow([f"Period: {start_date} to {end_date} | Currency: {currency}"])
        writer.writerow([])
        writer.writerow(["Category", "Code", "Account Name", "Amount"])
        for r in revenues:
            writer.writerow(["Operating Revenue", r['code'], r['name'], f"{r['amount']:.2f}"])
        writer.writerow(["Total Revenue", "", "", f"{total_revenue:.2f}"])
        writer.writerow([])
        for c in cogs:
            writer.writerow(["Cost of Goods Sold", c['code'], c['name'], f"{c['amount']:.2f}"])
        writer.writerow(["Total COGS", "", "", f"{total_cogs:.2f}"])
        writer.writerow(["Gross Profit", "", "", f"{gross_profit:.2f}"])
        writer.writerow([])
        for e in operating_expenses:
            writer.writerow(["Operating Expense", e['code'], e['name'], f"{e['amount']:.2f}"])
        writer.writerow(["Total Operating Expenses", "", "", f"{total_operating_expenses:.2f}"])
        writer.writerow(["NET PROFIT / (LOSS)", "", "", f"{net_profit:.2f}"])
        output.seek(0)
        return Response(output.getvalue(), mimetype='text/csv',
                        headers={"Content-Disposition": f"attachment;filename=Profit_Loss_{start_date}_{end_date}.csv"})
        
    return render_template('finance/profit_loss.html',
                           revenues=revenues, total_revenue=total_revenue,
                           cogs=cogs, total_cogs=total_cogs, gross_profit=gross_profit, gross_margin=gross_margin,
                           operating_expenses=operating_expenses, total_operating_expenses=total_operating_expenses,
                           net_profit=net_profit, net_margin=net_margin,
                           start_date=start_date, end_date=end_date,
                           branches=branches, selected_branch=branch_id, currency=currency)

# --- 2. BALANCE SHEET (STATEMENT OF FINANCIAL POSITION) ---
@finance_bp.route('/balance-sheet')
@login_required
@require_permission('finance', 'read')
def balance_sheet():
    db = get_db()
    today = date.today()
    as_of_date = request.args.get('as_of_date') or today.isoformat()
    branch_id = get_branch_filter(request, session)
    
    branch_clause = ""
    params = [as_of_date]
    if branch_id:
        branch_clause = " AND je.branch_id = ?"
        params.append(branch_id)
        
    query = f"""
        SELECT c.id, c.code, c.name, c.account_type,
               COALESCE(c.opening_balance, 0) as opening_balance,
               COALESCE(SUM(jl.debit), 0) as total_debit,
               COALESCE(SUM(jl.credit), 0) as total_credit
        FROM coa c
        LEFT JOIN journal_lines jl ON c.id = jl.account_id
        LEFT JOIN journal_entries je ON jl.journal_id = je.id AND je.status = 'Posted' AND je.doc_date <= ? {branch_clause}
        GROUP BY c.id, c.code, c.name, c.account_type
        ORDER BY c.code ASC
    """
    rows = db.execute(query, params).fetchall()
    
    current_assets = []
    fixed_assets = []
    current_liabilities = []
    long_term_liabilities = []
    equity_items = []
    
    total_current_assets = 0.0
    total_fixed_assets = 0.0
    total_current_liabilities = 0.0
    total_long_term_liabilities = 0.0
    total_equity_accounts = 0.0
    
    total_rev_cum = 0.0
    total_exp_cum = 0.0
    
    for r in rows:
        acc_type = r['account_type']
        code = str(r['code'])
        name = r['name']
        debit = float(r['total_debit'])
        credit = float(r['total_credit'])
        op_bal = float(r['opening_balance'])
        
        if acc_type == 'Asset':
            bal = op_bal + (debit - credit)
            if abs(bal) > 0.001:
                item = {'code': code, 'name': name, 'amount': bal}
                if code.startswith('14') or 'fixed' in name.lower() or 'equipment' in name.lower():
                    fixed_assets.append(item)
                    total_fixed_assets += bal
                else:
                    current_assets.append(item)
                    total_current_assets += bal
        elif acc_type == 'Liability':
            bal = op_bal + (credit - debit)
            if abs(bal) > 0.001:
                item = {'code': code, 'name': name, 'amount': bal}
                if code.startswith('25') or 'long term' in name.lower() or 'borrowing' in name.lower():
                    long_term_liabilities.append(item)
                    total_long_term_liabilities += bal
                else:
                    current_liabilities.append(item)
                    total_current_liabilities += bal
        elif acc_type == 'Equity':
            bal = op_bal + (credit - debit)
            if abs(bal) > 0.001:
                equity_items.append({'code': code, 'name': name, 'amount': bal})
                total_equity_accounts += bal
        elif acc_type == 'Revenue':
            total_rev_cum += (credit - debit)
        elif acc_type == 'Expense':
            total_exp_cum += (debit - credit)
            
    current_earnings = total_rev_cum - total_exp_cum
    
    total_assets = total_current_assets + total_fixed_assets
    total_liabilities = total_current_liabilities + total_long_term_liabilities
    total_equity = total_equity_accounts + current_earnings
    total_liabilities_and_equity = total_liabilities + total_equity
    
    diff = abs(total_assets - total_liabilities_and_equity)
    is_balanced = diff < 0.05
    
    branches = db.execute("SELECT id, name, code FROM branches WHERE is_active = 1 ORDER BY id").fetchall()
    company = db.execute("SELECT * FROM company LIMIT 1").fetchone()
    currency = company['currency'] if company and company['currency'] else 'KES'
    
    if request.args.get('export') == 'csv':
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow([f"Balance Sheet - {company['name'] if company else 'Cellar & Crumb'}"])
        writer.writerow([f"As of: {as_of_date} | Currency: {currency}"])
        writer.writerow([])
        writer.writerow(["Section", "Code", "Account Name", "Amount"])
        for a in current_assets:
            writer.writerow(["Current Assets", a['code'], a['name'], f"{a['amount']:.2f}"])
        writer.writerow(["Total Current Assets", "", "", f"{total_current_assets:.2f}"])
        for a in fixed_assets:
            writer.writerow(["Fixed Assets", a['code'], a['name'], f"{a['amount']:.2f}"])
        writer.writerow(["Total Fixed Assets", "", "", f"{total_fixed_assets:.2f}"])
        writer.writerow(["TOTAL ASSETS", "", "", f"{total_assets:.2f}"])
        writer.writerow([])
        for l in current_liabilities:
            writer.writerow(["Current Liabilities", l['code'], l['name'], f"{l['amount']:.2f}"])
        writer.writerow(["Total Current Liabilities", "", "", f"{total_current_liabilities:.2f}"])
        for l in long_term_liabilities:
            writer.writerow(["Long-Term Liabilities", l['code'], l['name'], f"{l['amount']:.2f}"])
        writer.writerow(["TOTAL LIABILITIES", "", "", f"{total_liabilities:.2f}"])
        writer.writerow([])
        for e in equity_items:
            writer.writerow(["Equity", e['code'], e['name'], f"{e['amount']:.2f}"])
        writer.writerow(["Current Earnings", "", "Retained Profit for Period", f"{current_earnings:.2f}"])
        writer.writerow(["TOTAL EQUITY", "", "", f"{total_equity:.2f}"])
        writer.writerow(["TOTAL LIABILITIES & EQUITY", "", "", f"{total_liabilities_and_equity:.2f}"])
        output.seek(0)
        return Response(output.getvalue(), mimetype='text/csv',
                        headers={"Content-Disposition": f"attachment;filename=Balance_Sheet_{as_of_date}.csv"})
        
    return render_template('finance/balance_sheet.html',
                           current_assets=current_assets, total_current_assets=total_current_assets,
                           fixed_assets=fixed_assets, total_fixed_assets=total_fixed_assets,
                           total_assets=total_assets,
                           current_liabilities=current_liabilities, total_current_liabilities=total_current_liabilities,
                           long_term_liabilities=long_term_liabilities, total_long_term_liabilities=total_long_term_liabilities,
                           total_liabilities=total_liabilities,
                           equity_items=equity_items, total_equity_accounts=total_equity_accounts,
                           current_earnings=current_earnings, total_equity=total_equity,
                           total_liabilities_and_equity=total_liabilities_and_equity,
                           is_balanced=is_balanced, diff=diff,
                           as_of_date=as_of_date, branches=branches, selected_branch=branch_id,
                           currency=currency)

# --- 3. TRIAL BALANCE ---
@finance_bp.route('/trial-balance')
@login_required
@require_permission('finance', 'read')
def trial_balance():
    db = get_db()
    today = date.today()
    start_date = request.args.get('start_date') or f"{today.year}-01-01"
    end_date = request.args.get('end_date') or today.isoformat()
    branch_id = get_branch_filter(request, session)
    
    branch_clause = ""
    params_period = [start_date, end_date]
    params_prior = [start_date]
    if branch_id:
        branch_clause = " AND je.branch_id = ?"
        params_period.append(branch_id)
        params_prior.append(branch_id)
        
    # Get all active posting accounts
    accounts = db.execute("SELECT id, code, name, account_type, COALESCE(opening_balance, 0) as init_bal FROM coa WHERE is_posting = 1 ORDER BY code").fetchall()
    
    # Movements during the period
    period_rows = db.execute(f"""
        SELECT jl.account_id,
               COALESCE(SUM(jl.debit), 0) as dr,
               COALESCE(SUM(jl.credit), 0) as cr
        FROM journal_lines jl
        JOIN journal_entries je ON jl.journal_id = je.id
        WHERE je.status = 'Posted' AND je.doc_date >= ? AND je.doc_date <= ? {branch_clause}
        GROUP BY jl.account_id
    """, params_period).fetchall()
    period_map = {r['account_id']: (float(r['dr']), float(r['cr'])) for r in period_rows}

    # Movements prior to period (to add to opening balance)
    prior_rows = db.execute(f"""
        SELECT jl.account_id,
               COALESCE(SUM(jl.debit), 0) as dr,
               COALESCE(SUM(jl.credit), 0) as cr
        FROM journal_lines jl
        JOIN journal_entries je ON jl.journal_id = je.id
        WHERE je.status = 'Posted' AND je.doc_date < ? {branch_clause}
        GROUP BY jl.account_id
    """, params_prior).fetchall()
    prior_map = {r['account_id']: (float(r['dr']), float(r['cr'])) for r in prior_rows}

    tb_rows = []
    tot_op_dr = 0.0
    tot_op_cr = 0.0
    tot_dr = 0.0
    tot_cr = 0.0
    tot_cl_dr = 0.0
    tot_cl_cr = 0.0

    for a in accounts:
        aid = a['id']
        acc_type = a['account_type']
        p_dr, p_cr = prior_map.get(aid, (0.0, 0.0))
        m_dr, m_cr = period_map.get(aid, (0.0, 0.0))
        init = float(a['init_bal'])
        
        # Calculate opening net
        if acc_type in ('Asset', 'Expense'):
            op_net = init + (p_dr - p_cr)
            cl_net = op_net + (m_dr - m_cr)
        else:
            op_net = init + (p_cr - p_dr)
            cl_net = op_net + (m_cr - m_dr)
            
        op_debit = op_net if (acc_type in ('Asset', 'Expense') and op_net > 0) or (acc_type not in ('Asset', 'Expense') and op_net < 0) else 0.0
        op_credit = op_net if (acc_type not in ('Asset', 'Expense') and op_net > 0) or (acc_type in ('Asset', 'Expense') and op_net < 0) else 0.0
        if op_debit < 0: op_debit = abs(op_debit)
        if op_credit < 0: op_credit = abs(op_credit)

        cl_debit = cl_net if (acc_type in ('Asset', 'Expense') and cl_net > 0) or (acc_type not in ('Asset', 'Expense') and cl_net < 0) else 0.0
        cl_credit = cl_net if (acc_type not in ('Asset', 'Expense') and cl_net > 0) or (acc_type in ('Asset', 'Expense') and cl_net < 0) else 0.0
        if cl_debit < 0: cl_debit = abs(cl_debit)
        if cl_credit < 0: cl_credit = abs(cl_credit)

        if abs(op_net) > 0.001 or abs(m_dr) > 0.001 or abs(m_cr) > 0.001 or abs(cl_net) > 0.001:
            tb_rows.append({
                'id': aid,
                'code': a['code'],
                'name': a['name'],
                'account_type': acc_type,
                'period_debit': m_dr,
                'period_credit': m_cr,
                'closing_debit': cl_debit,
                'closing_credit': cl_credit
            })
            tot_dr += m_dr
            tot_cr += m_cr
            tot_cl_dr += cl_debit
            tot_cl_cr += cl_credit

    branches = db.execute("SELECT id, name, code FROM branches WHERE is_active = 1 ORDER BY id").fetchall()
    company = db.execute("SELECT * FROM company LIMIT 1").fetchone()
    currency = company['currency'] if company and company['currency'] else 'KES'

    if request.args.get('export') == 'csv':
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow([f"Trial Balance - {company['name'] if company else 'Cellar & Crumb'}"])
        writer.writerow([f"Period: {start_date} to {end_date} | Currency: {currency}"])
        writer.writerow([])
        writer.writerow(["Code", "Account Name", "Type", "Period Debit", "Period Credit", "Closing Debit", "Closing Credit"])
        for r in tb_rows:
            writer.writerow([r['code'], r['name'], r['account_type'], f"{r['period_debit']:.2f}", f"{r['period_credit']:.2f}", f"{r['closing_debit']:.2f}", f"{r['closing_credit']:.2f}"])
        writer.writerow(["TOTALS", "", "", f"{tot_dr:.2f}", f"{tot_cr:.2f}", f"{tot_cl_dr:.2f}", f"{tot_cl_cr:.2f}"])
        output.seek(0)
        return Response(output.getvalue(), mimetype='text/csv',
                        headers={"Content-Disposition": f"attachment;filename=Trial_Balance_{start_date}_{end_date}.csv"})

    return render_template('finance/trial_balance.html',
                           tb_rows=tb_rows,
                           tot_dr=tot_dr, tot_cr=tot_cr,
                           tot_cl_dr=tot_cl_dr, tot_cl_cr=tot_cl_cr,
                           start_date=start_date, end_date=end_date,
                           branches=branches, selected_branch=branch_id, currency=currency)

# --- 4. GENERAL LEDGER (GL) DRILL-DOWN ---
@finance_bp.route('/gl')
@login_required
@require_permission('finance', 'read')
def gl_list():
    db = get_db()
    today = date.today()
    start_date = request.args.get('start_date') or f"{today.year}-01-01"
    end_date = request.args.get('end_date') or today.isoformat()
    account_id = request.args.get('account_id')
    branch_id = get_branch_filter(request, session)
    
    accounts = db.execute("SELECT id, code, name, account_type FROM coa WHERE is_posting = 1 ORDER BY code").fetchall()
    
    selected_account = None
    lines = []
    opening_balance = 0.0
    closing_balance = 0.0
    total_debit = 0.0
    total_credit = 0.0

    if account_id and account_id.isdigit():
        aid = int(account_id)
        selected_account = db.execute("SELECT * FROM coa WHERE id = ?", (aid,)).fetchone()
        
        branch_clause = ""
        params_prior = [aid, start_date]
        params_period = [aid, start_date, end_date]
        if branch_id:
            branch_clause = " AND je.branch_id = ?"
            params_prior.append(branch_id)
            params_period.append(branch_id)

        # Prior movement
        prior = db.execute(f"""
            SELECT COALESCE(SUM(jl.debit), 0) as dr, COALESCE(SUM(jl.credit), 0) as cr
            FROM journal_lines jl
            JOIN journal_entries je ON jl.journal_id = je.id
            WHERE jl.account_id = ? AND je.status = 'Posted' AND je.doc_date < ? {branch_clause}
        """, params_prior).fetchone()

        init = float(selected_account['opening_balance'] or 0)
        p_dr, p_cr = float(prior['dr']), float(prior['cr'])
        is_dr_normal = selected_account['account_type'] in ('Asset', 'Expense')
        
        opening_balance = (init + p_dr - p_cr) if is_dr_normal else (init + p_cr - p_dr)

        # Period lines
        raw_lines = db.execute(f"""
            SELECT jl.id, jl.debit, jl.credit, jl.description,
                   je.id as journal_id, je.doc_ref, je.doc_date, je.module, je.source_doc_type, je.source_doc_id,
                   p.name as partner_name
            FROM journal_lines jl
            JOIN journal_entries je ON jl.journal_id = je.id
            LEFT JOIN partners p ON jl.partner_id = p.id
            WHERE jl.account_id = ? AND je.status = 'Posted' AND je.doc_date >= ? AND je.doc_date <= ? {branch_clause}
            ORDER BY je.doc_date ASC, je.id ASC
        """, params_period).fetchall()

        running_bal = opening_balance
        for l in raw_lines:
            dr = float(l['debit'])
            cr = float(l['credit'])
            total_debit += dr
            total_credit += cr
            if is_dr_normal:
                running_bal += (dr - cr)
            else:
                running_bal += (cr - dr)
            lines.append({
                'id': l['id'],
                'doc_date': l['doc_date'],
                'doc_ref': l['doc_ref'],
                'journal_id': l['journal_id'],
                'source_doc_type': l['source_doc_type'],
                'source_doc_id': l['source_doc_id'],
                'partner_name': l['partner_name'],
                'description': l['description'],
                'debit': dr,
                'credit': cr,
                'balance': running_bal
            })
        closing_balance = running_bal

    branches = db.execute("SELECT id, name, code FROM branches WHERE is_active = 1 ORDER BY id").fetchall()
    company = db.execute("SELECT * FROM company LIMIT 1").fetchone()
    currency = company['currency'] if company and company['currency'] else 'KES'

    return render_template('finance/gl.html',
                           accounts=accounts, selected_account=selected_account,
                           lines=lines, opening_balance=opening_balance, closing_balance=closing_balance,
                           total_debit=total_debit, total_credit=total_credit,
                           start_date=start_date, end_date=end_date,
                           branches=branches, selected_branch=branch_id, currency=currency)

# --- 5. ACCOUNTS RECEIVABLE (AR) AGING ---
@finance_bp.route('/ar')
@login_required
@require_permission('finance', 'read')
def ar_list():
    db = get_db()
    today = date.today()
    as_of_date = request.args.get('as_of_date') or today.isoformat()
    branch_id = get_branch_filter(request, session)
    
    branch_clause = ""
    params = [as_of_date]
    if branch_id:
        branch_clause = " AND s.branch_id = ?"
        params.append(branch_id)
        
    query = f"""
        SELECT s.id, s.doc_ref, s.doc_date, s.due_date, s.total, s.amount_paid, s.balance_due,
               p.id as partner_id, p.name as customer_name, p.phone as customer_phone, p.email as customer_email
        FROM sales_docs s
        JOIN partners p ON s.customer_id = p.id
        WHERE s.doc_type = 'Invoice' AND s.status = 'Posted'
          AND s.balance_due > 0 AND s.doc_date <= ?
          {branch_clause}
        ORDER BY p.name ASC, s.due_date ASC
    """
    invoices = db.execute(query, params).fetchall()
    
    customers = {}
    try:
        as_of_dt = datetime.strptime(as_of_date, '%Y-%m-%d').date()
    except Exception:
        as_of_dt = today
        
    totals = {'current': 0.0, 'b1_30': 0.0, 'b31_60': 0.0, 'b61_90': 0.0, 'b90_plus': 0.0, 'total': 0.0}
    
    for inv in invoices:
        cust_id = inv['partner_id']
        if cust_id not in customers:
            customers[cust_id] = {
                'id': cust_id,
                'name': inv['customer_name'],
                'phone': inv['customer_phone'],
                'email': inv['customer_email'],
                'current': 0.0,
                'b1_30': 0.0,
                'b31_60': 0.0,
                'b61_90': 0.0,
                'b90_plus': 0.0,
                'total': 0.0,
                'invoices': []
            }
            
        due_str = inv['due_date'] or inv['doc_date']
        try:
            due_dt = datetime.strptime(due_str, '%Y-%m-%d').date()
        except Exception:
            due_dt = as_of_dt
            
        overdue_days = (as_of_dt - due_dt).days
        bal = float(inv['balance_due'])
        
        customers[cust_id]['total'] += bal
        totals['total'] += bal
        
        inv_dict = dict(inv)
        inv_dict['overdue_days'] = overdue_days
        customers[cust_id]['invoices'].append(inv_dict)
        
        if overdue_days <= 0:
            customers[cust_id]['current'] += bal
            totals['current'] += bal
        elif overdue_days <= 30:
            customers[cust_id]['b1_30'] += bal
            totals['b1_30'] += bal
        elif overdue_days <= 60:
            customers[cust_id]['b31_60'] += bal
            totals['b31_60'] += bal
        elif overdue_days <= 90:
            customers[cust_id]['b61_90'] += bal
            totals['b61_90'] += bal
        else:
            customers[cust_id]['b90_plus'] += bal
            totals['b90_plus'] += bal
            
    customer_list = sorted(customers.values(), key=lambda x: x['total'], reverse=True)
    branches = db.execute("SELECT id, name, code FROM branches WHERE is_active = 1 ORDER BY id").fetchall()
    company = db.execute("SELECT * FROM company LIMIT 1").fetchone()
    currency = company['currency'] if company and company['currency'] else 'KES'
    
    if request.args.get('export') == 'csv':
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow([f"Accounts Receivable Aging Report - {company['name'] if company else 'Cellar & Crumb'}"])
        writer.writerow([f"As of: {as_of_date} | Currency: {currency}"])
        writer.writerow([])
        writer.writerow(["Customer Name", "Current (Not Due)", "1-30 Days", "31-60 Days", "61-90 Days", "90+ Days", "Total Outstanding"])
        for c in customer_list:
            writer.writerow([c['name'], f"{c['current']:.2f}", f"{c['b1_30']:.2f}", f"{c['b31_60']:.2f}", f"{c['b61_90']:.2f}", f"{c['b90_plus']:.2f}", f"{c['total']:.2f}"])
        writer.writerow(["TOTALS", f"{totals['current']:.2f}", f"{totals['b1_30']:.2f}", f"{totals['b31_60']:.2f}", f"{totals['b61_90']:.2f}", f"{totals['b90_plus']:.2f}", f"{totals['total']:.2f}"])
        output.seek(0)
        return Response(output.getvalue(), mimetype='text/csv',
                        headers={"Content-Disposition": f"attachment;filename=AR_Aging_{as_of_date}.csv"})
        
    return render_template('finance/ar.html',
                           customers=customer_list, totals=totals,
                           as_of_date=as_of_date, branches=branches, selected_branch=branch_id, currency=currency)

# --- 6. ACCOUNTS PAYABLE (AP) AGING ---
@finance_bp.route('/ap')
@login_required
@require_permission('finance', 'read')
def ap_list():
    db = get_db()
    today = date.today()
    as_of_date = request.args.get('as_of_date') or today.isoformat()
    branch_id = get_branch_filter(request, session)
    
    branch_clause = ""
    params = [as_of_date]
    if branch_id:
        branch_clause = " AND p_doc.branch_id = ?"
        params.append(branch_id)
        
    query = f"""
        SELECT p_doc.id, p_doc.doc_ref, p_doc.doc_date, p_doc.due_date, p_doc.total, p_doc.amount_paid, p_doc.balance_due,
               part.id as partner_id, part.name as supplier_name, part.phone as supplier_phone, part.email as supplier_email
        FROM purchase_docs p_doc
        JOIN partners part ON p_doc.supplier_id = part.id
        WHERE p_doc.doc_type = 'Invoice' AND p_doc.status = 'Posted'
          AND p_doc.balance_due > 0 AND p_doc.doc_date <= ?
          {branch_clause}
        ORDER BY part.name ASC, p_doc.due_date ASC
    """
    bills = db.execute(query, params).fetchall()
    
    suppliers = {}
    try:
        as_of_dt = datetime.strptime(as_of_date, '%Y-%m-%d').date()
    except Exception:
        as_of_dt = today
        
    totals = {'current': 0.0, 'b1_30': 0.0, 'b31_60': 0.0, 'b61_90': 0.0, 'b90_plus': 0.0, 'total': 0.0}
    
    for bill in bills:
        supp_id = bill['partner_id']
        if supp_id not in suppliers:
            suppliers[supp_id] = {
                'id': supp_id,
                'name': bill['supplier_name'],
                'phone': bill['supplier_phone'],
                'email': bill['supplier_email'],
                'current': 0.0,
                'b1_30': 0.0,
                'b31_60': 0.0,
                'b61_90': 0.0,
                'b90_plus': 0.0,
                'total': 0.0,
                'bills': []
            }
            
        due_str = bill['due_date'] or bill['doc_date']
        try:
            due_dt = datetime.strptime(due_str, '%Y-%m-%d').date()
        except Exception:
            due_dt = as_of_dt
            
        overdue_days = (as_of_dt - due_dt).days
        bal = float(bill['balance_due'])
        
        suppliers[supp_id]['total'] += bal
        totals['total'] += bal
        
        bill_dict = dict(bill)
        bill_dict['overdue_days'] = overdue_days
        suppliers[supp_id]['bills'].append(bill_dict)
        
        if overdue_days <= 0:
            suppliers[supp_id]['current'] += bal
            totals['current'] += bal
        elif overdue_days <= 30:
            suppliers[supp_id]['b1_30'] += bal
            totals['b1_30'] += bal
        elif overdue_days <= 60:
            suppliers[supp_id]['b31_60'] += bal
            totals['b31_60'] += bal
        elif overdue_days <= 90:
            suppliers[supp_id]['b61_90'] += bal
            totals['b61_90'] += bal
        else:
            suppliers[supp_id]['b90_plus'] += bal
            totals['b90_plus'] += bal
            
    supplier_list = sorted(suppliers.values(), key=lambda x: x['total'], reverse=True)
    branches = db.execute("SELECT id, name, code FROM branches WHERE is_active = 1 ORDER BY id").fetchall()
    company = db.execute("SELECT * FROM company LIMIT 1").fetchone()
    currency = company['currency'] if company and company['currency'] else 'KES'
    
    if request.args.get('export') == 'csv':
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow([f"Accounts Payable Aging Report - {company['name'] if company else 'Cellar & Crumb'}"])
        writer.writerow([f"As of: {as_of_date} | Currency: {currency}"])
        writer.writerow([])
        writer.writerow(["Supplier Name", "Current (Not Due)", "1-30 Days", "31-60 Days", "61-90 Days", "90+ Days", "Total Outstanding"])
        for s in supplier_list:
            writer.writerow([s['name'], f"{s['current']:.2f}", f"{s['b1_30']:.2f}", f"{s['b31_60']:.2f}", f"{s['b61_90']:.2f}", f"{s['b90_plus']:.2f}", f"{s['total']:.2f}"])
        writer.writerow(["TOTALS", f"{totals['current']:.2f}", f"{totals['b1_30']:.2f}", f"{totals['b31_60']:.2f}", f"{totals['b61_90']:.2f}", f"{totals['b90_plus']:.2f}", f"{totals['total']:.2f}"])
        output.seek(0)
        return Response(output.getvalue(), mimetype='text/csv',
                        headers={"Content-Disposition": f"attachment;filename=AP_Aging_{as_of_date}.csv"})
        
    return render_template('finance/ap.html',
                           suppliers=supplier_list, totals=totals,
                           as_of_date=as_of_date, branches=branches, selected_branch=branch_id, currency=currency)

# --- 7. TAX REPORT ---
@finance_bp.route('/tax-report')
@login_required
@require_permission('finance', 'read')
def tax_report():
    db = get_db()
    today = date.today()
    start_date = request.args.get('start_date') or f"{today.year}-01-01"
    end_date = request.args.get('end_date') or today.isoformat()
    branch_id = get_branch_filter(request, session)
    
    branch_clause_s = ""
    branch_clause_p = ""
    params_s = [start_date, end_date]
    params_p = [start_date, end_date]
    if branch_id:
        branch_clause_s = " AND branch_id = ?"
        branch_clause_p = " AND branch_id = ?"
        params_s.append(branch_id)
        params_p.append(branch_id)

    # Output VAT (Sales Invoices)
    sales_vat = db.execute(f"""
        SELECT COUNT(*) as doc_cnt, COALESCE(SUM(subtotal), 0) as taxable_sales, COALESCE(SUM(tax_total), 0) as output_vat
        FROM sales_docs
        WHERE doc_type = 'Invoice' AND status = 'Posted' AND doc_date >= ? AND doc_date <= ? {branch_clause_s}
    """, params_s).fetchone()

    # Input VAT (Supplier Bills)
    purch_vat = db.execute(f"""
        SELECT COUNT(*) as doc_cnt, COALESCE(SUM(subtotal), 0) as taxable_purchases, COALESCE(SUM(tax_total), 0) as input_vat
        FROM purchase_docs
        WHERE doc_type = 'Invoice' AND status = 'Posted' AND doc_date >= ? AND doc_date <= ? {branch_clause_p}
    """, params_p).fetchone()

    output_vat = float(sales_vat['output_vat'] if sales_vat else 0.0)
    input_vat = float(purch_vat['input_vat'] if purch_vat else 0.0)
    net_vat_payable = output_vat - input_vat

    branches = db.execute("SELECT id, name, code FROM branches WHERE is_active = 1 ORDER BY id").fetchall()
    company = db.execute("SELECT * FROM company LIMIT 1").fetchone()
    currency = company['currency'] if company and company['currency'] else 'KES'

    return render_template('finance/tax_report.html',
                           sales_vat=sales_vat, purch_vat=purch_vat,
                           output_vat=output_vat, input_vat=input_vat, net_vat_payable=net_vat_payable,
                           start_date=start_date, end_date=end_date,
                           branches=branches, selected_branch=branch_id, currency=currency)

# --- 8. FINANCIAL PERIODS ---
@finance_bp.route('/periods')
@login_required
def periods():
    db = get_db()
    periods_list = db.execute("SELECT * FROM financial_periods ORDER BY start_date DESC").fetchall()
    return render_template('finance/periods.html', periods=periods_list)
