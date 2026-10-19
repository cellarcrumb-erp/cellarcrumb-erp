import os
import csv
import io
import json
import sqlite3
import shutil
from datetime import datetime
from pathlib import Path
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, make_response, jsonify, send_from_directory
from functools import wraps
from werkzeug.utils import secure_filename
from database import get_db, log_audit, DB_PATH, update_stock
from auth import login_required, require_permission, MODULE_DEFINITIONS, user_can
import bcrypt

settings_bp = Blueprint('settings', __name__, url_prefix='/settings')


@settings_bp.route('/')
@login_required
@require_permission('settings', 'read')
def index():
    return render_template('settings/index.html')

@settings_bp.route('/company', methods=['GET', 'POST'])
@login_required
def company():
    conn = get_db()
    if request.method == 'POST':
        name = request.form.get('name', '')
        address = request.form.get('address', '')
        phone = request.form.get('phone', '')
        email = request.form.get('email', '')
        tax_id = request.form.get('tax_id', '')
        currency = request.form.get('currency', 'KES')
        conn.execute('UPDATE company SET name=?,address=?,phone=?,email=?,tax_id=?,currency=? WHERE id=1',
                     (name, address, phone, email, tax_id, currency))
        conn.commit()
        log_audit(conn, session.get('user_id'), 'Settings', 'Update', 'Company', 1, 'Updated company profile')
        flash('Company profile updated.', 'success')
        return redirect(url_for('settings.company'))
    co = conn.execute('SELECT * FROM company WHERE id=1').fetchone()
    currencies = conn.execute('SELECT * FROM currencies').fetchall()
    return render_template('settings/company.html', company=co, currencies=currencies)

@settings_bp.route('/users')
@login_required
def users():
    conn = get_db()
    users_list = conn.execute('SELECT u.*, r.name as role_name FROM users u LEFT JOIN roles r ON u.role_id=r.id ORDER BY u.id').fetchall()
    return render_template('settings/users.html', users=users_list)

@settings_bp.route('/users/new', methods=['GET', 'POST'])
@login_required
def new_user():
    conn = get_db()
    if request.method == 'POST':
        username = request.form.get('username')
        full_name = request.form.get('full_name')
        email = request.form.get('email')
        role_id = request.form.get('role_id')
        password = request.form.get('password')
        if not username or not full_name or not password:
            flash('Username, Full Name, and Password are required.', 'error')
        else:
            try:
                pw_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode()
                conn.execute('INSERT INTO users(username, full_name, email, password_hash, role_id, is_active) VALUES(?,?,?,?,?,1)',
                             (username, full_name, email, pw_hash, role_id))
                conn.commit()
                flash('User created successfully.', 'success')
                return redirect(url_for('settings.users'))
            except Exception as e:
                flash(f'Error creating user: {e}', 'error')
    roles_list = conn.execute('SELECT * FROM roles').fetchall()
    return render_template('settings/user_form.html', user=None, roles=roles_list)

@settings_bp.route('/users/<int:id>')
@login_required
def view_user(id):
    conn = get_db()
    u = conn.execute('SELECT u.*, r.name as role_name FROM users u LEFT JOIN roles r ON u.role_id=r.id WHERE u.id=?', (id,)).fetchone()
    return render_template('settings/user_detail.html', user=u)

user_detail = view_user

@settings_bp.route('/users/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_user(id):
    conn = get_db()
    if request.method == 'POST':
        full_name = request.form.get('full_name')
        email = request.form.get('email')
        role_id = request.form.get('role_id')
        is_active = 1 if request.form.get('is_active') else 0
        conn.execute('UPDATE users SET full_name=?, email=?, role_id=?, is_active=? WHERE id=?',
                     (full_name, email, role_id, is_active, id))
        conn.commit()
        flash('User updated successfully.', 'success')
        return redirect(url_for('settings.users'))
    u = conn.execute('SELECT * FROM users WHERE id=?', (id,)).fetchone()
    roles_list = conn.execute('SELECT * FROM roles').fetchall()
    return render_template('settings/user_form.html', user=u, roles=roles_list)

@settings_bp.route('/users/<int:id>/reset-password', methods=['POST'])
@login_required
def reset_password(id):
    new_pw = request.form.get('new_password')
    if new_pw:
        conn = get_db()
        pw_hash = bcrypt.hashpw(new_pw.encode('utf-8'), bcrypt.gensalt()).decode()
        conn.execute('UPDATE users SET password_hash=? WHERE id=?', (pw_hash, id))
        conn.commit()
        flash('Password reset successfully.', 'success')
    return redirect(url_for('settings.view_user', id=id))

@settings_bp.route('/users/<int:id>/toggle-active', methods=['POST'])
@login_required
def toggle_user(id):
    conn = get_db()
    conn.execute('UPDATE users SET is_active = CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?', (id,))
    conn.commit()
    flash('User status changed.', 'success')
    return redirect(url_for('settings.users'))

@settings_bp.route('/roles')
@login_required
def roles():
    conn = get_db()
    roles_list = conn.execute('''
        SELECT r.*, COUNT(u.id) as user_count 
        FROM roles r 
        LEFT JOIN users u ON r.id = u.role_id 
        GROUP BY r.id
    ''').fetchall()
    return render_template('settings/roles.html', roles=roles_list)

@settings_bp.route('/roles/new', methods=['GET', 'POST'])
@login_required
def new_role():
    conn = get_db()
    if request.method == 'POST':
        name = request.form.get('name')
        description = request.form.get('description', '')
        if name:
            conn.execute('INSERT INTO roles(name, description) VALUES(?,?)', (name, description))
            conn.commit()
            flash('Role created.', 'success')
            return redirect(url_for('settings.roles'))
    return render_template('settings/role_form.html', role=None)

@settings_bp.route('/roles/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_role(id):
    conn = get_db()
    if request.method == 'POST':
        name = request.form.get('name')
        description = request.form.get('description', '')
        conn.execute('UPDATE roles SET name=?, description=? WHERE id=?', (name, description, id))
        conn.commit()
        flash('Role updated.', 'success')
        return redirect(url_for('settings.roles'))
    r = conn.execute('SELECT * FROM roles WHERE id=?', (id,)).fetchone()
    return render_template('settings/role_form.html', role=r)

@settings_bp.route('/num-series', methods=['GET', 'POST'])
@login_required
def num_series():
    conn = get_db()
    if request.method == 'POST':
        for key in request.form:
            if key.startswith('prefix_'):
                sid = key.replace('prefix_', '')
                prefix = request.form.get(key)
                next_num = request.form.get(f'next_num_{sid}', 1, type=int)
                conn.execute('UPDATE num_series SET prefix=?, next_num=? WHERE id=?', (prefix, next_num, sid))
        conn.commit()
        flash('Numbering series updated.', 'success')
        return redirect(url_for('settings.num_series'))
    series = conn.execute('SELECT * FROM num_series ORDER BY module, doc_type').fetchall()
    return render_template('settings/num_series.html', series=series)

@settings_bp.route('/taxes')
@login_required
def taxes():
    conn = get_db()
    taxes_list = conn.execute('SELECT * FROM taxes').fetchall()
    return render_template('settings/taxes.html', taxes=taxes_list)

@settings_bp.route('/taxes/new', methods=['GET', 'POST'])
@login_required
def new_tax():
    conn = get_db()
    if request.method == 'POST':
        code = request.form.get('code')
        name = request.form.get('name')
        rate = request.form.get('rate', 0, type=float)
        tax_type = request.form.get('tax_type', 'VAT')
        conn.execute('INSERT INTO taxes(code, name, rate, tax_type) VALUES(?,?,?,?)', (code, name, rate, tax_type))
        conn.commit()
        flash('Tax created.', 'success')
        return redirect(url_for('settings.taxes'))
    return render_template('settings/tax_form.html', tax=None)

@settings_bp.route('/taxes/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_tax(id):
    conn = get_db()
    if request.method == 'POST':
        name = request.form.get('name')
        rate = request.form.get('rate', 0, type=float)
        is_active = 1 if request.form.get('is_active') else 0
        conn.execute('UPDATE taxes SET name=?, rate=?, is_active=? WHERE id=?', (name, rate, is_active, id))
        conn.commit()
        flash('Tax updated.', 'success')
        return redirect(url_for('settings.taxes'))
    t = conn.execute('SELECT * FROM taxes WHERE id=?', (id,)).fetchone()
    return render_template('settings/tax_form.html', tax=t)

@settings_bp.route('/payment-methods')
@login_required
def payment_methods():
    conn = get_db()
    methods = conn.execute('SELECT * FROM payment_methods').fetchall()
    return render_template('settings/payment_methods.html', payment_methods=methods)

@settings_bp.route('/payment-methods/new', methods=['GET', 'POST'])
@login_required
def new_payment_method():
    conn = get_db()
    if request.method == 'POST':
        name = request.form.get('name')
        method_type = request.form.get('method_type', 'Cash')
        conn.execute('INSERT INTO payment_methods(name, method_type) VALUES(?,?)', (name, method_type))
        conn.commit()
        flash('Payment method created.', 'success')
        return redirect(url_for('settings.payment_methods'))
    return render_template('settings/payment_method_form.html', method=None)

@settings_bp.route('/payment-methods/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_payment_method(id):
    conn = get_db()
    if request.method == 'POST':
        name = request.form.get('name')
        method_type = request.form.get('method_type', 'Cash')
        is_active = 1 if request.form.get('is_active') else 0
        conn.execute('UPDATE payment_methods SET name=?, method_type=?, is_active=? WHERE id=?', (name, method_type, is_active, id))
        conn.commit()
        flash('Payment method updated.', 'success')
        return redirect(url_for('settings.payment_methods'))
    m = conn.execute('SELECT * FROM payment_methods WHERE id=?', (id,)).fetchone()
    return render_template('settings/payment_method_form.html', method=m)

@settings_bp.route('/currencies')
@login_required
def currencies():
    conn = get_db()
    curs = conn.execute('SELECT * FROM currencies').fetchall()
    return render_template('settings/currencies.html', currencies=curs)

@settings_bp.route('/currencies/new', methods=['GET', 'POST'])
@login_required
def new_currency():
    conn = get_db()
    if request.method == 'POST':
        code = request.form.get('code')
        name = request.form.get('name')
        symbol = request.form.get('symbol')
        rate = request.form.get('exchange_rate', 1, type=float)
        conn.execute('INSERT INTO currencies(code, name, symbol, exchange_rate) VALUES(?,?,?,?)', (code, name, symbol, rate))
        conn.commit()
        flash('Currency created.', 'success')
        return redirect(url_for('settings.currencies'))
    return render_template('settings/currency_form.html', currency=None)

@settings_bp.route('/currencies/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_currency(id):
    conn = get_db()
    if request.method == 'POST':
        name = request.form.get('name')
        symbol = request.form.get('symbol')
        rate = request.form.get('exchange_rate', 1, type=float)
        conn.execute('UPDATE currencies SET name=?, symbol=?, exchange_rate=? WHERE id=?', (name, symbol, rate, id))
        conn.commit()
        flash('Currency updated.', 'success')
        return redirect(url_for('settings.currencies'))
    c = conn.execute('SELECT * FROM currencies WHERE id=?', (id,)).fetchone()
    return render_template('settings/currency_form.html', currency=c)

@settings_bp.route('/uom')
@login_required
def uom():
    conn = get_db()
    uoms = conn.execute('SELECT * FROM uom').fetchall()
    return render_template('settings/uom.html', uoms=uoms)

@settings_bp.route('/uom/new', methods=['GET', 'POST'])
@login_required
def new_uom():
    conn = get_db()
    if request.method == 'POST':
        code = request.form.get('code')
        name = request.form.get('name')
        base_uom = request.form.get('base_uom')
        conn.execute('INSERT INTO uom(code, name, base_uom) VALUES(?,?,?)', (code, name, base_uom))
        conn.commit()
        flash('UOM created.', 'success')
        return redirect(url_for('settings.uom'))
    uoms = conn.execute('SELECT * FROM uom').fetchall()
    return render_template('settings/uom_form.html', uom=None, uoms=uoms)

@settings_bp.route('/uom/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_uom(id):
    conn = get_db()
    if request.method == 'POST':
        name = request.form.get('name')
        base_uom = request.form.get('base_uom')
        conn.execute('UPDATE uom SET name=?, base_uom=? WHERE id=?', (name, base_uom, id))
        conn.commit()
        flash('UOM updated.', 'success')
        return redirect(url_for('settings.uom'))
    u = conn.execute('SELECT * FROM uom WHERE id=?', (id,)).fetchone()
    uoms = conn.execute('SELECT * FROM uom WHERE id != ?', (id,)).fetchall()
    return render_template('settings/uom_form.html', uom=u, uoms=uoms)

@settings_bp.route('/approvals')
@login_required
def approvals():
    conn = get_db()
    wfs = conn.execute('''
        SELECT a.*, r.name as role_name 
        FROM approval_workflows a 
        LEFT JOIN roles r ON a.approver_role_id = r.id
    ''').fetchall()
    return render_template('settings/approvals.html', workflows=wfs)

@settings_bp.route('/approvals/new', methods=['GET', 'POST'])
@login_required
def new_approval():
    conn = get_db()
    if request.method == 'POST':
        module = request.form.get('module')
        doc_type = request.form.get('doc_type')
        condition_field = request.form.get('condition_field')
        condition_op = request.form.get('condition_op')
        condition_val = request.form.get('condition_value', 0, type=float)
        role_id = request.form.get('approver_role_id')
        conn.execute('''
            INSERT INTO approval_workflows(module, doc_type, condition_field, condition_op, condition_value, approver_role_id)
            VALUES(?,?,?,?,?,?)
        ''', (module, doc_type, condition_field, condition_op, condition_val, role_id))
        conn.commit()
        flash('Approval workflow created.', 'success')
        return redirect(url_for('settings.approvals'))
    roles_list = conn.execute('SELECT * FROM roles').fetchall()
    return render_template('settings/approval_form.html', roles=roles_list)

@settings_bp.route('/approvals/<int:id>/toggle', methods=['POST'])
@login_required
def toggle_approval(id):
    conn = get_db()
    conn.execute('UPDATE approval_workflows SET is_active = CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?', (id,))
    conn.commit()
    flash('Approval workflow toggled.', 'success')
    return redirect(url_for('settings.approvals'))

@settings_bp.route('/audit-log')
@login_required
def audit_log():
    conn = get_db()
    module = request.args.get('module')
    user_id = request.args.get('user_id')
    query = 'SELECT a.*, u.username FROM audit_log a LEFT JOIN users u ON a.user_id=u.id WHERE 1=1'
    params = []
    if module:
        query += ' AND a.module = ?'
        params.append(module)
    if user_id:
        query += ' AND a.user_id = ?'
        params.append(user_id)
    query += ' ORDER BY a.id DESC LIMIT 100'
    logs = conn.execute(query, params).fetchall()
    users_list = conn.execute('SELECT * FROM users').fetchall()
    return render_template('settings/audit_log.html', audit_entries=logs, users=users_list)


# ── Multiple Branches (SAP BPL Management) ───────────────────────────────────

@settings_bp.route('/switch_branch/<int:branch_id>', methods=['GET', 'POST'])
@login_required
def switch_branch(branch_id):
    """Switch active working branch in user session."""
    conn = get_db()
    role = session.get('role', '')
    user_id = session.get('user_id')
    
    if branch_id == 0:
        if role in ['Admin', 'Manager']:
            session['active_branch_id'] = 0
            session['active_branch_name'] = 'All Branches'
            session['active_branch_code'] = 'ALL'
            flash('Switched to All Branches view.', 'info')
        else:
            flash('Unauthorized to view All Branches.', 'error')
    else:
        if role in ['Admin', 'Manager']:
            branch = conn.execute("SELECT * FROM branches WHERE id = ? AND is_active = 1", (branch_id,)).fetchone()
        else:
            branch = conn.execute("""
                SELECT b.* FROM user_branches ub 
                JOIN branches b ON ub.branch_id = b.id 
                WHERE ub.user_id = ? AND b.id = ? AND b.is_active = 1
            """, (user_id, branch_id)).fetchone()
            
        if branch:
            session['active_branch_id'] = branch['id']
            session['active_branch_name'] = branch['name']
            session['active_branch_code'] = branch['code']
            flash(f"Switched active branch to {branch['name']}.", 'success')
        else:
            flash('Branch not found or access denied.', 'error')
            
    ref = request.referrer
    if ref and not ref.endswith('/login') and not ref.endswith('/logout'):
        return redirect(ref)
    return redirect(url_for('overview.dashboard'))


@settings_bp.route('/branches')
@login_required
def branches():
    conn = get_db()
    branch_list = conn.execute('''
        SELECT b.*, w.name as default_warehouse_name,
               (SELECT COUNT(*) FROM user_branches ub WHERE ub.branch_id = b.id) as user_count,
               (SELECT COUNT(*) FROM warehouses wh WHERE wh.branch_id = b.id) as warehouse_count
        FROM branches b
        LEFT JOIN warehouses w ON b.default_warehouse_id = w.id
        ORDER BY b.is_main DESC, b.name ASC
    ''').fetchall()
    return render_template('settings/branches.html', branches=branch_list)


@settings_bp.route('/branches/new', methods=['GET', 'POST'])
@login_required
def new_branch():
    conn = get_db()
    if request.method == 'POST':
        code = request.form.get('code', '').strip().upper()
        name = request.form.get('name', '').strip()
        address = request.form.get('address', '').strip()
        phone = request.form.get('phone', '').strip()
        email = request.form.get('email', '').strip()
        tax_id = request.form.get('tax_id', '').strip()
        default_wh_id = request.form.get('default_warehouse_id', type=int) or None
        is_main = 1 if request.form.get('is_main') == '1' else 0
        
        if not code or not name:
            flash('Branch Code and Branch Name are required.', 'error')
        else:
            try:
                if is_main:
                    conn.execute("UPDATE branches SET is_main = 0")
                cursor = conn.execute('''
                    INSERT INTO branches (code, name, address, phone, email, tax_id, default_warehouse_id, is_main, is_active, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now'))
                ''', (code, name, address, phone, email, tax_id, default_wh_id, is_main))
                new_id = cursor.lastrowid
                
                # Auto assign all admins & managers to new branch
                admins = conn.execute("SELECT u.id FROM users u JOIN roles r ON u.role_id=r.id WHERE r.name IN ('Admin', 'Manager')").fetchall()
                for adm in admins:
                    conn.execute("INSERT OR IGNORE INTO user_branches (user_id, branch_id, is_default) VALUES (?, ?, 0)",
                                 (adm['id'], new_id))
                conn.commit()
                log_audit(conn, session.get('user_id'), 'Settings', 'Create', 'Branch', new_id, f"Created branch {code} - {name}")
                flash(f"Branch {name} ({code}) created successfully.", 'success')
                return redirect(url_for('settings.branches'))
            except Exception as e:
                conn.rollback()
                flash(f"Error creating branch: {e}", 'error')
                
    warehouses = conn.execute("SELECT * FROM warehouses WHERE is_active=1 ORDER BY name").fetchall()
    return render_template('settings/branch_form.html', branch=None, warehouses=warehouses)


@settings_bp.route('/branches/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_branch(id):
    conn = get_db()
    branch = conn.execute("SELECT * FROM branches WHERE id = ?", (id,)).fetchone()
    if not branch:
        flash('Branch not found.', 'error')
        return redirect(url_for('settings.branches'))
        
    if request.method == 'POST':
        code = request.form.get('code', '').strip().upper()
        name = request.form.get('name', '').strip()
        address = request.form.get('address', '').strip()
        phone = request.form.get('phone', '').strip()
        email = request.form.get('email', '').strip()
        tax_id = request.form.get('tax_id', '').strip()
        default_wh_id = request.form.get('default_warehouse_id', type=int) or None
        is_main = 1 if request.form.get('is_main') == '1' else 0
        is_active = 1 if request.form.get('is_active') == '1' else 0
        
        try:
            if is_main and not branch['is_main']:
                conn.execute("UPDATE branches SET is_main = 0")
            conn.execute('''
                UPDATE branches 
                SET code=?, name=?, address=?, phone=?, email=?, tax_id=?, default_warehouse_id=?, is_main=?, is_active=?
                WHERE id=?
            ''', (code, name, address, phone, email, tax_id, default_wh_id, is_main, is_active, id))
            conn.commit()
            log_audit(conn, session.get('user_id'), 'Settings', 'Edit', 'Branch', id, f"Updated branch {code}")
            flash('Branch updated successfully.', 'success')
            return redirect(url_for('settings.branches'))
        except Exception as e:
            conn.rollback()
            flash(f"Error updating branch: {e}", 'error')
            
    warehouses = conn.execute("SELECT * FROM warehouses WHERE is_active=1 ORDER BY name").fetchall()
    return render_template('settings/branch_form.html', branch=branch, warehouses=warehouses)


# ── User Branch Assignments ───────────────────────────────────────────────────

@settings_bp.route('/users/<int:id>/branches', methods=['GET', 'POST'])
@login_required
@require_permission('settings', 'write')
def user_branches(id):
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id=?', (id,)).fetchone()
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('settings.users'))
        
    if request.method == 'POST':
        selected_branches = request.form.getlist('branch_ids')
        default_branch = request.form.get('default_branch', type=int)
        
        conn.execute('DELETE FROM user_branches WHERE user_id=?', (id,))
        for b_id in selected_branches:
            b_id = int(b_id)
            is_def = 1 if b_id == default_branch else 0
            conn.execute('INSERT INTO user_branches (user_id, branch_id, is_default) VALUES (?, ?, ?)',
                         (id, b_id, is_def))
        conn.commit()
        log_audit(conn, session.get('user_id'), 'Settings', 'Update', 'User Branches', id, f"Updated branch access for {user['username']}")
        flash(f"Branch permissions updated for user {user['full_name']}.", 'success')
        return redirect(url_for('settings.users'))
        
    all_branches = conn.execute('SELECT * FROM branches WHERE is_active=1 ORDER BY is_main DESC, name ASC').fetchall()
    assigned = conn.execute('SELECT branch_id, is_default FROM user_branches WHERE user_id=?', (id,)).fetchall()
    assigned_map = {r['branch_id']: r['is_default'] for r in assigned}
    
    return render_template('settings/user_branches.html', user=user, branches=all_branches, assigned_map=assigned_map)


# ── SAP-Style General Authorizations Matrix ───────────────────────────────────

@settings_bp.route('/authorizations')
@login_required
@require_permission('settings', 'read')
def authorizations():
    conn = get_db()
    roles_list = conn.execute('SELECT * FROM roles ORDER BY id').fetchall()
    role_id = request.args.get('role_id', type=int)
    if not role_id and roles_list:
        role_id = roles_list[0]['id']
        
    selected_role = None
    if role_id:
        selected_role = conn.execute('SELECT * FROM roles WHERE id=?', (role_id,)).fetchone()
        
    role_perms = {}
    if selected_role and selected_role['permissions']:
        try:
            role_perms = json.loads(selected_role['permissions'])
        except Exception:
            role_perms = {}
            
    return render_template('settings/authorizations.html', 
                           roles=roles_list, 
                           selected_role=selected_role,
                           role_perms=role_perms,
                           module_definitions=MODULE_DEFINITIONS)


@settings_bp.route('/authorizations/<int:role_id>/save', methods=['POST'])
@login_required
@require_permission('settings', 'write')
def save_authorizations(role_id):
    conn = get_db()
    role = conn.execute('SELECT * FROM roles WHERE id=?', (role_id,)).fetchone()
    if not role:
        flash('Role not found.', 'error')
        return redirect(url_for('settings.authorizations'))
        
    perms = {}
    for mod in MODULE_DEFINITIONS:
        k = mod['key']
        level = request.form.get(f'perm_{k}', type=int)
        if level is not None:
            perms[k] = level
        else:
            perms[k] = 0
            
    conn.execute('UPDATE roles SET permissions = ? WHERE id = ?', (json.dumps(perms), role_id))
    conn.commit()
    log_audit(conn, session.get('user_id'), 'Settings', 'Update', 'Authorizations', role_id, f"Updated authorizations for role {role['name']}")
    flash(f"Authorizations successfully updated for role '{role['name']}'.", 'success')
    return redirect(url_for('settings.authorizations', role_id=role_id))


# ── Bulk Data Import Workbench (SAP DTW / Excel Import) ───────────────────────

@settings_bp.route('/import')
@login_required
@require_permission('settings', 'read')
def data_import():
    active_tab = request.args.get('tab', 'items')
    return render_template('settings/import.html', active_tab=active_tab)


@settings_bp.route('/import/template/<import_type>')
@login_required
def download_import_template(import_type):
    output = io.StringIO()
    writer = csv.writer(output)
    
    if import_type == 'items':
        writer.writerow(['Item Code*', 'Item Name*', 'Item Group*', 'Item Type*', 'UOM Code*', 'Cost Price', 'Selling Price', 'Reorder Level', 'Is Stock Item (1/0)'])
        writer.writerow(['FG-CROISSANT', 'Butter Croissant', 'Finished Goods', 'Finished Good', 'PCS', '45.00', '80.00', '15', '1'])
        writer.writerow(['RM-CHOCOLATE', 'Dark Chocolate Chips', 'Raw Materials', 'Raw Material', 'KG', '450.00', '0.00', '10', '1'])
        filename = 'Items_Import_Template.csv'
        
    elif import_type == 'partners':
        writer.writerow(['CardCode*', 'CardName*', 'Partner Type*', 'Phone', 'Email', 'Address', 'Tax ID / PIN', 'Credit Limit', 'Opening Balance'])
        writer.writerow(['CUS0100', 'Acme Cafe & Bistro', 'Customer', '+254 712 345 678', 'orders@acmecafe.co.ke', 'Westlands, Nairobi', 'P051999888Z', '50000', '0'])
        writer.writerow(['SUP0100', 'Kenya Grain Millers', 'Supplier', '+254 722 987 654', 'sales@grainmillers.co.ke', 'Industrial Area, Nairobi', 'P051777666Y', '0', '0'])
        filename = 'Business_Partners_Template.csv'
        
    elif import_type == 'stock':
        writer.writerow(['Item Code*', 'Warehouse Code*', 'Quantity*', 'Unit Cost*'])
        writer.writerow(['FG-BREAD', 'MAIN', '50', '55.00'])
        writer.writerow(['RM-FLOUR', 'MAIN', '200', '120.00'])
        filename = 'Opening_Stock_Template.csv'
        
    elif import_type == 'coa':
        writer.writerow(['Account Code*', 'Account Name*', 'Account Type*', 'Parent Code', 'Is Posting (1/0)', 'Opening Balance'])
        writer.writerow(['1140', 'Petty Cash - Westlands', 'Asset', '1100', '1', '10000.00'])
        writer.writerow(['5270', 'Marketing & Promotion', 'Expense', '5200', '1', '0.00'])
        filename = 'Chart_of_Accounts_Template.csv'
    else:
        flash('Invalid template requested.', 'error')
        return redirect(url_for('settings.data_import'))
        
    response = make_response(output.getvalue())
    response.headers['Content-Disposition'] = f'attachment; filename={filename}'
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    return response


@settings_bp.route('/import/upload', methods=['POST'])
@login_required
@require_permission('settings', 'write')
def upload_import_file():
    import_type = request.form.get('import_type')
    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({'success': False, 'error': 'No file uploaded.'})
        
    try:
        content = file.read().decode('utf-8-sig', errors='ignore')
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
        
        if not rows:
            return jsonify({'success': False, 'error': 'CSV file contains no data rows.'})
            
        conn = get_db()
        preview_rows = []
        valid_count = 0
        error_count = 0
        
        if import_type == 'items':
            existing_codes = {r['code'].upper() for r in conn.execute('SELECT code FROM items').fetchall()}
            uoms = {r['code'].upper(): r['id'] for r in conn.execute('SELECT code, id FROM uom').fetchall()}
            
            for idx, r in enumerate(rows, 1):
                code = (r.get('Item Code*') or r.get('Item Code') or r.get('code') or '').strip().upper()
                name = (r.get('Item Name*') or r.get('Item Name') or r.get('name') or '').strip()
                group_name = (r.get('Item Group*') or r.get('Item Group') or r.get('group') or 'Finished Goods').strip()
                item_type = (r.get('Item Type*') or r.get('Item Type') or r.get('type') or 'Finished Good').strip()
                uom_code = (r.get('UOM Code*') or r.get('UOM Code') or r.get('uom') or 'PCS').strip().upper()
                
                try: cost = float(r.get('Cost Price') or r.get('cost') or 0.0)
                except: cost = 0.0
                try: price = float(r.get('Selling Price') or r.get('price') or 0.0)
                except: price = 0.0
                try: reorder = float(r.get('Reorder Level') or r.get('reorder') or 0.0)
                except: reorder = 0.0
                is_stock = 1 if str(r.get('Is Stock Item (1/0)') or r.get('is_stock') or '1').strip() in ['1', 'true', 'True'] else 0
                
                errors = []
                if not code: errors.append('Item Code is required')
                if not name: errors.append('Item Name is required')
                if uom_code not in uoms: errors.append(f"UOM '{uom_code}' not found")
                
                status = 'Error' if errors else ('Update' if code in existing_codes else 'New')
                if errors: error_count += 1
                else: valid_count += 1
                
                preview_rows.append({
                    'row': idx, 'code': code, 'name': name, 'group': group_name,
                    'type': item_type, 'uom': uom_code, 'cost': cost, 'price': price,
                    'reorder': reorder, 'is_stock': is_stock, 'status': status,
                    'message': '; '.join(errors) if errors else 'Ready to import'
                })

        elif import_type == 'partners':
            existing_codes = {r['code'].upper() for r in conn.execute('SELECT code FROM partners').fetchall()}
            for idx, r in enumerate(rows, 1):
                code = (r.get('CardCode*') or r.get('CardCode') or r.get('code') or '').strip().upper()
                name = (r.get('CardName*') or r.get('CardName') or r.get('name') or '').strip()
                ptype = (r.get('Partner Type*') or r.get('Partner Type') or r.get('type') or 'Customer').strip()
                phone = (r.get('Phone') or '').strip()
                email = (r.get('Email') or '').strip()
                address = (r.get('Address') or '').strip()
                tax_id = (r.get('Tax ID / PIN') or '').strip()
                try: credit_limit = float(r.get('Credit Limit') or 0.0)
                except: credit_limit = 0.0
                try: opening_bal = float(r.get('Opening Balance') or 0.0)
                except: opening_bal = 0.0
                
                errors = []
                if not code: errors.append('CardCode is required')
                if not name: errors.append('CardName is required')
                if ptype not in ['Customer', 'Supplier', 'Customer & Supplier', 'Employee', 'Other']:
                    errors.append(f"Invalid Partner Type '{ptype}'")
                    
                status = 'Error' if errors else ('Update' if code in existing_codes else 'New')
                if errors: error_count += 1
                else: valid_count += 1
                
                preview_rows.append({
                    'row': idx, 'code': code, 'name': name, 'type': ptype,
                    'phone': phone, 'email': email, 'address': address, 'tax_id': tax_id,
                    'credit_limit': credit_limit, 'opening_balance': opening_bal,
                    'status': status, 'message': '; '.join(errors) if errors else 'Ready to import'
                })

        elif import_type == 'stock':
            items = {r['code'].upper(): (r['id'], r['cost']) for r in conn.execute('SELECT id, code, cost FROM items').fetchall()}
            warehouses = {r['code'].upper(): (r['id'], r['name']) for r in conn.execute('SELECT id, code, name FROM warehouses').fetchall()}
            
            for idx, r in enumerate(rows, 1):
                icode = (r.get('Item Code*') or r.get('Item Code') or r.get('code') or '').strip().upper()
                wcode = (r.get('Warehouse Code*') or r.get('Warehouse Code') or r.get('warehouse') or '').strip().upper()
                try: qty = float(r.get('Quantity*') or r.get('quantity') or 0.0)
                except: qty = 0.0
                try: cost = float(r.get('Unit Cost*') or r.get('cost') or 0.0)
                except: cost = 0.0
                
                errors = []
                if icode not in items: errors.append(f"Item '{icode}' not found")
                if wcode not in warehouses: errors.append(f"Warehouse '{wcode}' not found")
                if qty <= 0: errors.append('Quantity must be > 0')
                
                status = 'Error' if errors else 'Valid'
                if errors: error_count += 1
                else: valid_count += 1
                
                preview_rows.append({
                    'row': idx, 'item_code': icode, 'warehouse_code': wcode,
                    'quantity': qty, 'unit_cost': cost,
                    'status': status, 'message': '; '.join(errors) if errors else 'Ready to import'
                })

        elif import_type == 'coa':
            existing_codes = {r['code'] for r in conn.execute('SELECT code FROM coa').fetchall()}
            for idx, r in enumerate(rows, 1):
                code = (r.get('Account Code*') or r.get('code') or '').strip()
                name = (r.get('Account Name*') or r.get('name') or '').strip()
                atype = (r.get('Account Type*') or r.get('type') or 'Asset').strip()
                pcode = (r.get('Parent Code') or '').strip() or None
                is_posting = 1 if str(r.get('Is Posting (1/0)') or '1').strip() in ['1', 'true'] else 0
                try: op_bal = float(r.get('Opening Balance') or 0.0)
                except: op_bal = 0.0
                
                errors = []
                if not code: errors.append('Account Code is required')
                if not name: errors.append('Account Name is required')
                
                status = 'Error' if errors else ('Update' if code in existing_codes else 'New')
                if errors: error_count += 1
                else: valid_count += 1
                
                preview_rows.append({
                    'row': idx, 'code': code, 'name': name, 'type': atype,
                    'parent_code': pcode, 'is_posting': is_posting, 'opening_balance': op_bal,
                    'status': status, 'message': '; '.join(errors) if errors else 'Ready to import'
                })

        return jsonify({
            'success': True,
            'import_type': import_type,
            'total_rows': len(rows),
            'valid_count': valid_count,
            'error_count': error_count,
            'rows': preview_rows
        })
    except Exception as e:
        return jsonify({'success': False, 'error': f"Failed to parse CSV: {str(e)}"})


@settings_bp.route('/import/execute', methods=['POST'])
@login_required
@require_permission('settings', 'write')
def execute_import():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data payload received.'})
        
    import_type = data.get('import_type')
    rows = data.get('rows', [])
    
    if not rows:
        return jsonify({'success': False, 'error': 'No valid records to import.'})
        
    conn = get_db()
    created_count = 0
    updated_count = 0
    
    try:
        if import_type == 'items':
            uoms = {r['code'].upper(): r['id'] for r in conn.execute('SELECT code, id FROM uom').fetchall()}
            groups = {r['name'].upper(): r['id'] for r in conn.execute('SELECT name, id FROM item_groups').fetchall()}
            
            for r in rows:
                if r.get('status') == 'Error':
                    continue
                code = r['code'].strip().upper()
                name = r['name'].strip()
                group_name = r.get('group', 'Finished Goods').strip().upper()
                group_id = groups.get(group_name)
                if not group_id:
                    cur = conn.execute("INSERT INTO item_groups (name) VALUES (?)", (r.get('group', 'Finished Goods'),))
                    group_id = cur.lastrowid
                    groups[group_name] = group_id
                    
                uom_id = uoms.get(r.get('uom', 'PCS').strip().upper(), 1)
                cost = float(r.get('cost') or 0.0)
                price = float(r.get('price') or 0.0)
                reorder = float(r.get('reorder') or 0.0)
                is_stock = int(r.get('is_stock', 1))
                itype = r.get('type', 'Finished Good')
                
                existing = conn.execute("SELECT id FROM items WHERE code = ?", (code,)).fetchone()
                if existing:
                    conn.execute("""
                        UPDATE items 
                        SET name=?, group_id=?, item_type=?, uom_id=?, cost=?, price=?, reorder_level=?, is_stock_item=?
                        WHERE id=?
                    """, (name, group_id, itype, uom_id, cost, price, reorder, is_stock, existing['id']))
                    updated_count += 1
                else:
                    conn.execute("""
                        INSERT INTO items (code, name, group_id, item_type, uom_id, cost, price, reorder_level, is_stock_item, is_active, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now'))
                    """, (code, name, group_id, itype, uom_id, cost, price, reorder, is_stock))
                    created_count += 1

        elif import_type == 'partners':
            for r in rows:
                if r.get('status') == 'Error':
                    continue
                code = r['code'].strip().upper()
                name = r['name'].strip()
                ptype = r.get('type', 'Customer')
                phone = r.get('phone', '')
                email = r.get('email', '')
                address = r.get('address', '')
                tax_id = r.get('tax_id', '')
                credit = float(r.get('credit_limit') or 0.0)
                bal = float(r.get('opening_balance') or 0.0)
                
                existing = conn.execute("SELECT id FROM partners WHERE code = ?", (code,)).fetchone()
                if existing:
                    conn.execute("""
                        UPDATE partners 
                        SET name=?, partner_type=?, phone=?, email=?, address=?, tax_id=?, credit_limit=?
                        WHERE id=?
                    """, (name, ptype, phone, email, address, tax_id, credit, existing['id']))
                    updated_count += 1
                else:
                    conn.execute("""
                        INSERT INTO partners (code, name, partner_type, phone, email, address, tax_id, credit_limit, opening_balance, balance, status, currency, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Active', 'KES', datetime('now'))
                    """, (code, name, ptype, phone, email, address, tax_id, credit, bal, bal))
                    created_count += 1

        elif import_type == 'stock':
            items = {r['code'].upper(): (r['id'], r['cost']) for r in conn.execute('SELECT id, code, cost FROM items').fetchall()}
            warehouses = {r['code'].upper(): r['id'] for r in conn.execute('SELECT id, code FROM warehouses').fetchall()}
            user_id = session.get('user_id')
            
            for r in rows:
                if r.get('status') == 'Error':
                    continue
                icode = r['item_code'].strip().upper()
                wcode = r['warehouse_code'].strip().upper()
                qty = float(r['quantity'])
                cost = float(r['unit_cost']) if float(r.get('unit_cost') or 0) > 0 else items[icode][1]
                
                item_id = items[icode][0]
                wh_id = warehouses[wcode]
                
                # Update stock balance & ledger
                update_stock(conn, item_id, wh_id, qty, 0, 'OpeningStock', None, 'OPENING-STOCK', cost, user_id)
                created_count += 1

        elif import_type == 'coa':
            for r in rows:
                if r.get('status') == 'Error':
                    continue
                code = r['code'].strip()
                name = r['name'].strip()
                atype = r.get('type', 'Asset')
                pcode = r.get('parent_code')
                is_post = int(r.get('is_posting', 1))
                bal = float(r.get('opening_balance') or 0.0)
                
                existing = conn.execute("SELECT id FROM coa WHERE code = ?", (code,)).fetchone()
                if existing:
                    conn.execute("UPDATE coa SET name=?, account_type=?, parent_id=(SELECT id FROM coa WHERE code=?), is_posting=? WHERE id=?",
                                 (name, atype, pcode, is_post, existing['id']))
                    updated_count += 1
                else:
                    conn.execute("INSERT INTO coa (code, name, account_type, parent_id, is_posting, is_active, opening_balance) VALUES (?, ?, ?, (SELECT id FROM coa WHERE code=?), ?, 1, ?)",
                                 (code, name, atype, pcode, is_post, bal))
                    created_count += 1
                    
        conn.commit()
        log_audit(conn, session.get('user_id'), 'Settings', 'Import', import_type.title(), None, f"Imported {created_count} new, {updated_count} updated")
        return jsonify({
            'success': True,
            'message': f"Import completed successfully! Created: {created_count}, Updated: {updated_count}."
        })
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': f"Import failed: {str(e)}"})


# ── Database Backup & Restore System ──────────────────────────────────────────

BACKUPS_DIR = DB_PATH.parent / 'backups'

@settings_bp.route('/backup')
@login_required
@require_permission('settings', 'read')
def backups():
    os.makedirs(BACKUPS_DIR, exist_ok=True)
    backup_files = []
    for f in BACKUPS_DIR.glob('*.db'):
        stat = f.stat()
        size_kb = stat.st_size / 1024
        size_str = f"{size_kb / 1024:.2f} MB" if size_kb > 1024 else f"{size_kb:.1f} KB"
        dt = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
        backup_files.append({
            'filename': f.name,
            'size': size_str,
            'timestamp': dt,
            'path': str(f)
        })
    backup_files.sort(key=lambda x: x['timestamp'], reverse=True)
    return render_template('settings/backup.html', backups=backup_files)


@settings_bp.route('/backup/create', methods=['POST'])
@login_required
@require_permission('settings', 'write')
def create_backup():
    os.makedirs(BACKUPS_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_filename = f"backup_{ts}.db"
    dest_path = BACKUPS_DIR / backup_filename
    
    try:
        source_conn = get_db()
        dest_conn = sqlite3.connect(str(dest_path))
        source_conn.backup(dest_conn)
        dest_conn.close()
        source_conn.close()
        
        conn = get_db()
        log_audit(conn, session.get('user_id'), 'Settings', 'Backup', 'Database', None, f"Created backup {backup_filename}")
        conn.close()
        
        flash(f"Database backup '{backup_filename}' created successfully.", 'success')
    except Exception as e:
        flash(f"Error creating backup: {e}", 'error')
        
    return redirect(url_for('settings.backups'))


@settings_bp.route('/backup/download/<filename>')
@login_required
@require_permission('settings', 'read')
def download_backup(filename):
    safe_name = secure_filename(filename)
    os.makedirs(BACKUPS_DIR, exist_ok=True)
    return send_from_directory(str(BACKUPS_DIR), safe_name, as_attachment=True)


@settings_bp.route('/backup/restore/<filename>', methods=['POST'])
@login_required
@require_permission('settings', 'write')
def restore_backup(filename):
    safe_name = secure_filename(filename)
    source_file = BACKUPS_DIR / safe_name
    if not source_file.exists() or not source_file.resolve().is_relative_to(BACKUPS_DIR.resolve()):
        flash('Backup file not found or invalid path.', 'error')
        return redirect(url_for('settings.backups'))
        
    try:
        # Create an emergency backup of current state before restoring
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        emergency_backup = BACKUPS_DIR / f"pre_restore_backup_{ts}.db"
        shutil.copy2(str(DB_PATH), str(emergency_backup))
        
        # Restore selected backup
        shutil.copy2(str(source_file), str(DB_PATH))
        
        conn = get_db()
        log_audit(conn, session.get('user_id'), 'Settings', 'Restore', 'Database', None, f"Restored database from {safe_name}")
        conn.close()
        
        flash(f"Database successfully restored from '{safe_name}'. Pre-restore safety backup saved.", 'success')
    except Exception as e:
        flash(f"Error restoring backup: {e}", 'error')
        
    return redirect(url_for('settings.backups'))


@settings_bp.route('/backup/upload-restore', methods=['POST'])
@login_required
@require_permission('settings', 'write')
def upload_restore_backup():
    file = request.files.get('file')
    if not file or not file.filename:
        flash('No backup file selected.', 'error')
        return redirect(url_for('settings.backups'))
        
    safe_input_name = secure_filename(file.filename)
    if not safe_input_name.endswith('.db'):
        flash('Invalid file format. Please upload a valid SQLite .db file.', 'error')
        return redirect(url_for('settings.backups'))
        
    try:
        os.makedirs(BACKUPS_DIR, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        save_filename = f"uploaded_{ts}_{safe_input_name}"
        dest_path = BACKUPS_DIR / save_filename
        file.save(str(dest_path))
        
        # Verify valid sqlite database
        test_conn = sqlite3.connect(str(dest_path))
        test_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        test_conn.close()
        
        # Safety backup current DB
        emergency_backup = BACKUPS_DIR / f"pre_restore_backup_{ts}.db"
        shutil.copy2(str(DB_PATH), str(emergency_backup))
        
        # Replace current DB
        shutil.copy2(str(dest_path), str(DB_PATH))
        
        conn = get_db()
        log_audit(conn, session.get('user_id'), 'Settings', 'Restore', 'Database', None, f"Restored from uploaded file {safe_input_name}")
        conn.close()
        
        flash(f"Database successfully restored from uploaded file '{safe_input_name}'.", 'success')
    except Exception as e:
        flash(f"Failed to restore database from uploaded file: {e}", 'error')
        
    return redirect(url_for('settings.backups'))


@settings_bp.route('/backup/delete/<filename>', methods=['POST'])
@login_required
@require_permission('settings', 'write')
def delete_backup(filename):
    safe_name = secure_filename(filename)
    file_path = BACKUPS_DIR / safe_name
    if file_path.exists() and file_path.resolve().is_relative_to(BACKUPS_DIR.resolve()):
        try:
            file_path.unlink()
            flash(f"Backup '{safe_name}' deleted.", 'info')
        except Exception as e:
            flash(f"Error deleting backup: {e}", 'error')
    else:
        flash('Backup file not found.', 'error')
    return redirect(url_for('settings.backups'))


