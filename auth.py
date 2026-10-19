import bcrypt
import json
import secrets
import time
from functools import wraps
from flask import session, redirect, url_for, flash, Blueprint, render_template, request
from database import get_db
from datetime import datetime

auth_bp = Blueprint('auth', __name__)

LOGIN_ATTEMPTS = {}  # {key: {'count': int, 'locked_until': float}}
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = 300  # 5 minutes

def check_login_rate_limit(key):
    now = time.time()
    record = LOGIN_ATTEMPTS.get(key)
    if record:
        if record.get('locked_until', 0) > now:
            remaining = int(record['locked_until'] - now)
            return False, f"Too many failed login attempts. Account temporarily locked. Please try again in {remaining} seconds."
        elif record.get('locked_until', 0) <= now and record.get('locked_until', 0) > 0:
            LOGIN_ATTEMPTS.pop(key, None)
    return True, None

def record_failed_login(key):
    now = time.time()
    record = LOGIN_ATTEMPTS.get(key, {'count': 0, 'locked_until': 0})
    record['count'] += 1
    if record['count'] >= MAX_FAILED_ATTEMPTS:
        record['locked_until'] = now + LOCKOUT_DURATION
    LOGIN_ATTEMPTS[key] = record

def reset_login_attempts(key):
    LOGIN_ATTEMPTS.pop(key, None)

def generate_csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(24)
    return session['_csrf_token']

def validate_csrf_token(token):
    if not token or '_csrf_token' not in session:
        return False
    return secrets.compare_digest(session['_csrf_token'], token)

MODULE_DEFINITIONS = [
    {'key': 'overview', 'name': 'Overview & Dashboard', 'icon': 'grid', 'category': 'General'},
    {'key': 'inventory', 'name': 'Inventory & Stock', 'icon': 'package', 'category': 'Operations'},
    {'key': 'bp_master', 'name': 'Business Partner Master', 'icon': 'users', 'category': 'Operations'},
    {'key': 'sales', 'name': 'Sales & Invoicing', 'icon': 'shopping-cart', 'category': 'Operations'},
    {'key': 'purchases', 'name': 'Purchasing & AP', 'icon': 'truck', 'category': 'Operations'},
    {'key': 'production', 'name': 'Production & BOM', 'icon': 'cpu', 'category': 'Operations'},
    {'key': 'finance', 'name': 'Finance & GL Accounting', 'icon': 'bar-chart-2', 'category': 'Finance & HR'},
    {'key': 'hr', 'name': 'Human Resources & Staff', 'icon': 'user-check', 'category': 'Finance & HR'},
    {'key': 'banking', 'name': 'Banking & Cash Flow', 'icon': 'credit-card', 'category': 'Finance & HR'},
    {'key': 'payments', 'name': 'Payments & Allocations', 'icon': 'dollar-sign', 'category': 'Finance & HR'},
    {'key': 'settings', 'name': 'Administration & Settings', 'icon': 'settings', 'category': 'Administration'},
]

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated

def get_current_user():
    if 'user_id' in session:
        return session
    return None

def has_permission(module, action='read'):
    """
    Check if the current logged-in user has permission for a module.
    Levels:
      2 = Full Authorization (read and write)
      1 = Read Only (read allowed, write denied)
      0 = No Authorization (read and write denied)
    """
    if 'user_id' not in session:
        return False
        
    role = session.get('role', '')
    if role == 'Admin':
        return True
        
    permissions = session.get('permissions', {})
    if not isinstance(permissions, dict):
        permissions = {}
        
    # Default level is 0 if unspecified
    level = int(permissions.get(module, 0))
    
    if action == 'read':
        return level >= 1
    elif action == 'write':
        return level >= 2
    return False

def require_permission(module, action='read'):
    """Decorator to enforce permission checks on Flask view routes."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('auth.login'))
            if not has_permission(module, action):
                mod_name = module.replace('_', ' ').title()
                flash(f"Access Denied: You do not have {action.upper()} authorization for {mod_name}.", 'error')
                ref = request.referrer
                if ref and not ref.endswith('/login') and not ref.endswith('/logout'):
                    return redirect(ref)
                return redirect(url_for('overview.dashboard'))
            return f(*args, **kwargs)
        return decorated
    return decorator

def user_can(module, action='read'):
    """Helper for Jinja templates."""
    return has_permission(module, action)

def get_default_landing_endpoint():
    """Find the first authorized module for the current user's session."""
    if has_permission('overview', 'read'):
        return 'overview.dashboard'
    for mod_key, endpoint in [
        ('sales', 'sales.index'),
        ('purchases', 'purchases.index'),
        ('inventory', 'inventory.index'),
        ('bp_master', 'bp_master.index'),
        ('production', 'production.index'),
        ('finance', 'finance.index'),
        ('hr', 'hr.index'),
        ('banking', 'banking.index'),
        ('payments', 'payments.index'),
        ('settings', 'settings.index'),
    ]:
        if has_permission(mod_key, 'read'):
            return endpoint
    return 'overview.dashboard'

def require_permission(module, action='read'):
    """Decorator to enforce permission checks on Flask view routes."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('auth.login'))
            if not has_permission(module, action):
                mod_name = module.replace('_', ' ').title()
                flash(f"Access Denied: You do not have {action.upper()} authorization for {mod_name}.", 'error')
                ref = request.referrer
                if ref and not ref.endswith('/login') and not ref.endswith('/logout'):
                    return redirect(ref)
                landing = get_default_landing_endpoint()
                if landing != f"{module}.index" and landing != f"{module}.dashboard":
                    return redirect(url_for(landing))
                return redirect(url_for('auth.login'))
            return f(*args, **kwargs)
        return decorated
    return decorator

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    conn = get_db()
    cursor = conn.cursor()
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        client_ip = request.remote_addr or '127.0.0.1'
        rate_key = f"{client_ip}_{username.lower()}"
        is_allowed, lock_msg = check_login_rate_limit(rate_key)
        if not is_allowed:
            flash(lock_msg, 'error')
            conn.close()
            return render_template('auth/login.html')
        
        cursor.execute("""
            SELECT u.id, u.username, u.full_name, u.password_hash, r.name as role_name, r.permissions 
            FROM users u JOIN roles r ON u.role_id = r.id 
            WHERE u.username = ? AND u.is_active = TRUE
        """, (username,))
        user = cursor.fetchone()
        
        if user and bcrypt.checkpw(password.encode('utf-8'), user['password_hash'].encode('utf-8')):
            reset_login_attempts(rate_key)
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['full_name'] = user['full_name']
            session['role'] = user['role_name']
            
            # Load user role permissions into session
            perms = {}
            if user['permissions']:
                try:
                    perms = json.loads(user['permissions'])
                except Exception:
                    perms = {}
            session['permissions'] = perms
            
            # Strict Branch Assignment:
            # 1. Check user assigned branches from user_branches
            user_assigned_branches = cursor.execute("""
                SELECT b.id, b.name, b.code, ub.is_default
                FROM user_branches ub 
                JOIN branches b ON ub.branch_id = b.id 
                WHERE ub.user_id = ? AND b.is_active = TRUE 
                ORDER BY ub.is_default DESC, b.id ASC
            """, (user['id'],)).fetchall()
            
            if user_assigned_branches:
                # Lock user to their assigned default branch
                def_b = user_assigned_branches[0]
                session['active_branch_id'] = def_b['id']
                session['active_branch_name'] = def_b['name']
                session['active_branch_code'] = def_b['code']
            elif user['role_name'] in ['Admin', 'Manager']:
                # Admins/Managers default to Main HQ or All Branches
                main_b = cursor.execute("SELECT id, name, code FROM branches WHERE is_main = TRUE AND is_active = TRUE LIMIT 1").fetchone()
                if main_b:
                    session['active_branch_id'] = main_b['id']
                    session['active_branch_name'] = main_b['name']
                    session['active_branch_code'] = main_b['code']
                else:
                    session['active_branch_id'] = 0
                    session['active_branch_name'] = 'All Branches'
                    session['active_branch_code'] = 'ALL'
            else:
                # Fallback to main branch
                main_b = cursor.execute("SELECT id, name, code FROM branches WHERE is_main = TRUE AND is_active = TRUE LIMIT 1").fetchone()
                if main_b:
                    session['active_branch_id'] = main_b['id']
                    session['active_branch_name'] = main_b['name']
                    session['active_branch_code'] = main_b['code']
                else:
                    session['active_branch_id'] = 0
                    session['active_branch_name'] = 'All Branches'
                    session['active_branch_code'] = 'ALL'
            
            cursor.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.now().isoformat(), user['id']))
            conn.commit()
            conn.close()
            
            flash(f"Welcome back, {user['full_name']}! Active Branch: {session.get('active_branch_name')}", 'success')
            landing = get_default_landing_endpoint()
            return redirect(url_for(landing))
        else:
            record_failed_login(rate_key)
            flash('Invalid username or password', 'error')
            
    conn.close()
    return render_template('auth/login.html')

@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))
