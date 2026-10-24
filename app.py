import os
import secrets
from datetime import timedelta
from flask import Flask, redirect, url_for, session
from database import init_db, DB_PATH
from auth import auth_bp, user_can, generate_csrf_token

def get_or_create_secret_key():
    """Retrieve secret key from environment or persist a strong random 256-bit key on disk."""
    env_key = os.environ.get('SECRET_KEY')
    if env_key:
        return env_key
    key_file = DB_PATH.parent / '.secret_key'
    try:
        if key_file.exists():
            return key_file.read_text(encoding='utf-8').strip()
        else:
            key_file.parent.mkdir(parents=True, exist_ok=True)
            new_key = secrets.token_hex(32)
            key_file.write_text(new_key, encoding='utf-8')
            return new_key
    except Exception:
        return secrets.token_hex(32)

def create_app():
    app = Flask(__name__)
    app.secret_key = get_or_create_secret_key()
    
    # Secure Session Cookies, Upload Limits, and Template Auto-Reload
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        TEMPLATES_AUTO_RELOAD=True,
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=16 * 1024 * 1024,
    )
    app.request_class.max_form_memory_size = 16 * 1024 * 1024
    
    init_db()
    
    # Register Jinja global helpers
    app.jinja_env.globals['user_can'] = user_can
    app.jinja_env.globals['csrf_token'] = generate_csrf_token
    
    app.register_blueprint(auth_bp)
    for name, prefix, mod in [
        ('overview_bp', '/overview', 'routes.overview'),
        ('inventory_bp', '/inventory', 'routes.inventory'),
        ('bp_master_bp', '/bp', 'routes.bp_master'),
        ('sales_bp', '/sales', 'routes.sales'),
        ('purchases_bp', '/purchases', 'routes.purchases'),
        ('production_bp', '/production', 'routes.production'),
        ('finance_bp', '/finance', 'routes.finance'),
        ('hr_bp', '/hr', 'routes.hr'),
        ('banking_bp', '/banking', 'routes.banking'),
        ('payments_bp', '/payments', 'routes.payments'),
        ('settings_bp', '/settings', 'routes.settings'),
    ]:
        try:
            import importlib
            m = importlib.import_module(mod)
            app.register_blueprint(getattr(m, name), url_prefix=prefix)
        except Exception as e:
            print(f'Blueprint {name} error: {e}')

    @app.context_processor
    def inject_global_context():
        from auth import user_can
        if 'user_id' not in session:
            return {'user_can': user_can}
        from database import get_db, PH
        conn = get_db()
        try:
            user_id = session.get('user_id')
            role = session.get('role', '')
            
            # If Admin or Manager, can access all active branches; otherwise allowed user_branches
            branches = []
            try:
                if role in ['Admin', 'Manager']:
                    branches = conn.execute("SELECT * FROM branches WHERE is_active = TRUE ORDER BY is_main DESC, name ASC").fetchall()
                else:
                    branches = conn.execute(f"""
                        SELECT b.* FROM user_branches ub 
                        JOIN branches b ON ub.branch_id = b.id 
                        WHERE ub.user_id = {PH} AND b.is_active = TRUE 
                        ORDER BY ub.is_default DESC, b.name ASC
                    """, (user_id,)).fetchall()
            except Exception as e:
                print(f"[GlobalContext] Error loading branches: {e}")
                branches = []
                
            active_branch_id = session.get('active_branch_id', 0)
            active_branch = None
            if active_branch_id and active_branch_id > 0:
                try:
                    active_branch = conn.execute(f"SELECT * FROM branches WHERE id = {PH}", (active_branch_id,)).fetchone()
                except Exception as e:
                    print(f"[GlobalContext] Error loading active_branch: {e}")
                    active_branch = None

            company_row = None
            try:
                company_row = conn.execute("SELECT name, currency FROM company WHERE id = 1").fetchone()
            except Exception:
                pass

            return {
                'user_can': user_can,
                'all_accessible_branches': branches,
                'active_branch_id': active_branch_id,
                'active_branch': active_branch,
                'can_switch_all_branches': (role in ['Admin', 'Manager']),
                'global_company_name': (company_row['name'] if company_row and company_row['name'] else 'Cellar & Crumb Operations'),
            }
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @app.errorhandler(500)
    def handle_500(e):
        import traceback
        tb = traceback.format_exc()
        print("500 INTERNAL SERVER ERROR:\n" + tb)
        return f"""<!DOCTYPE html>
<html>
<head><title>500 - Application Error</title>
<style>
body {{ background: #0f172a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 2rem; }}
.card {{ background: #1e293b; border: 1px solid #ef4444; border-radius: 8px; padding: 1.5rem; max-width: 900px; margin: 0 auto; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
h2 {{ color: #ef4444; margin-top: 0; }}
pre {{ background: #0b1325; border: 1px solid rgba(255,255,255,0.1); padding: 1rem; border-radius: 6px; overflow-x: auto; color: #fbbf24; font-family: monospace; font-size: 13px; line-height: 1.5; }}
a {{ color: #38bdf8; text-decoration: underline; margin-right: 15px; font-weight: 500; }}
</style>
</head>
<body>
<div class="card">
  <h2>500 - Application Error</h2>
  <p>The server encountered an error while processing your request:</p>
  <pre>{tb}</pre>
  <div style="margin-top:1.5rem;">
    <a href="/logout">Logout &amp; Reset Session</a>
    <a href="/purchases/quotations">Purchases Quotations</a>
    <a href="/sales/quotations">Sales Quotations</a>
  </div>
</div>
</body>
</html>""", 500

    @app.route('/api/sap-nav')
    def api_sap_nav():
        """SAP Business One Data Record Navigation (First / Previous / Next / Last) & Quick Find."""
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        from flask import request, jsonify
        from database import get_db, PH
        bp = request.args.get('bp', '')
        path = request.args.get('path', '')
        dir_action = request.args.get('dir', 'next')  # first, prev, next, last
        conn = get_db()
        try:
            parts = [p for p in path.strip('/').split('/') if p]
            target_url = None
            find_url = None
            new_url = None

            def _resolve_id(table, current_id, where_sql="", params=()):
                w_base = f"WHERE {where_sql}" if where_sql else ""
                w_and = f"AND ({where_sql})" if where_sql else ""
                if dir_action == 'first':
                    r = conn.execute(f"SELECT id FROM {table} {w_base} ORDER BY id ASC LIMIT 1", params).fetchone()
                elif dir_action == 'last':
                    r = conn.execute(f"SELECT id FROM {table} {w_base} ORDER BY id DESC LIMIT 1", params).fetchone()
                elif dir_action == 'prev':
                    if current_id:
                        r = conn.execute(f"SELECT id FROM {table} WHERE id < {PH} {w_and} ORDER BY id DESC LIMIT 1", (current_id,) + tuple(params)).fetchone()
                        if not r:
                            r = conn.execute(f"SELECT id FROM {table} {w_base} ORDER BY id ASC LIMIT 1", params).fetchone()
                    else:
                        r = conn.execute(f"SELECT id FROM {table} {w_base} ORDER BY id DESC LIMIT 1", params).fetchone()
                else:  # next
                    if current_id:
                        r = conn.execute(f"SELECT id FROM {table} WHERE id > {PH} {w_and} ORDER BY id ASC LIMIT 1", (current_id,) + tuple(params)).fetchone()
                        if not r:
                            r = conn.execute(f"SELECT id FROM {table} {w_base} ORDER BY id DESC LIMIT 1", params).fetchone()
                    else:
                        r = conn.execute(f"SELECT id FROM {table} {w_base} ORDER BY id ASC LIMIT 1", params).fetchone()
                return r['id'] if r else None

            # 1. Sales Documents (/sales/<doc_kind>/<id> or /sales/<doc_kind>/new or /sales/<doc_kind>)
            if bp == 'sales' and len(parts) >= 2:
                from routes.sales import DOC_TYPES
                doc_kind = parts[1]
                if doc_kind in DOC_TYPES:
                    dtype = DOC_TYPES[doc_kind]
                    cur_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
                    nid = _resolve_id('sales_docs', cur_id, f"doc_type = {PH}", (dtype,))
                    find_url = f"/sales/{doc_kind}"
                    new_url = f"/sales/{doc_kind}/new"
                    target_url = f"/sales/{doc_kind}/{nid}" if nid else find_url

            # 2. Purchase Documents (/purchases/<doc_kind>/<id>)
            elif bp == 'purchases' and len(parts) >= 2:
                from routes.purchases import DOC_TYPES as P_DOC_TYPES
                doc_kind = parts[1]
                if doc_kind in P_DOC_TYPES:
                    dtype = P_DOC_TYPES[doc_kind]
                    cur_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
                    nid = _resolve_id('purchase_docs', cur_id, f"doc_type = {PH}", (dtype,))
                    find_url = f"/purchases/{doc_kind}"
                    new_url = f"/purchases/{doc_kind}/new"
                    target_url = f"/purchases/{doc_kind}/{nid}" if nid else find_url

            # 3. Inventory (items, warehouses, receipts, issues, transfers, counts)
            elif bp == 'inventory' and len(parts) >= 2:
                sub = parts[1]
                cur_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
                inv_map = {
                    'items': ('items', '/inventory/items', '/inventory/items/new'),
                    'warehouses': ('warehouses', '/inventory/warehouses', '/inventory/warehouses/new'),
                    'receipts': ('goods_receipts', '/inventory/receipts', '/inventory/receipts/new'),
                    'issues': ('goods_issues', '/inventory/issues', '/inventory/issues/new'),
                    'transfers': ('stock_transfers', '/inventory/transfers', '/inventory/transfers/new'),
                    'counts': ('inventory_counts', '/inventory/counts', '/inventory/counts/new'),
                }
                if sub in inv_map:
                    tbl, f_url, n_url = inv_map[sub]
                    nid = _resolve_id(tbl, cur_id)
                    find_url, new_url = f_url, n_url
                    target_url = f"/inventory/{sub}/{nid}" if nid else f_url

            # 4. Business Partners (/bp/<id> or /bp/)
            elif bp == 'bp_master':
                cur_id = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
                nid = _resolve_id('partners', cur_id)
                find_url = '/bp/'
                new_url = '/bp/new'
                target_url = f"/bp/{nid}" if nid else find_url

            # 5. Payments (/payments/<id> or /payments/incoming or /payments/outgoing)
            elif bp == 'payments':
                cur_id = None
                for p in parts[1:]:
                    if p.isdigit():
                        cur_id = int(p)
                        break
                nid = _resolve_id('payments', cur_id)
                find_url = '/payments/incoming'
                new_url = '/payments/incoming/new'
                target_url = f"/payments/{nid}" if nid else find_url

            # 6. Production (/production/orders/<id> or /production/bom/<id>)
            elif bp == 'production' and len(parts) >= 2:
                sub = parts[1]
                cur_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
                if sub == 'bom':
                    nid = _resolve_id('bom', cur_id)
                    find_url, new_url = '/production/bom', '/production/bom/new'
                    target_url = f"/production/bom/{nid}" if nid else find_url
                else:
                    nid = _resolve_id('production_orders', cur_id)
                    find_url, new_url = '/production/orders', '/production/orders/new'
                    target_url = f"/production/orders/{nid}" if nid else find_url

            # 7. Finance (/finance/journals/<id> or /finance/coa/<id>)
            elif bp == 'finance' and len(parts) >= 2:
                sub = parts[1]
                cur_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
                if sub == 'coa':
                    nid = _resolve_id('coa', cur_id)
                    find_url, new_url = '/finance/coa', '/finance/coa/new'
                    target_url = f"/finance/coa/{nid}" if nid else find_url
                else:
                    nid = _resolve_id('journal_entries', cur_id)
                    find_url, new_url = '/finance/journals', '/finance/journals/new'
                    target_url = f"/finance/journals/{nid}" if nid else find_url

            # 8. HR (/hr/employees/<id>)
            elif bp == 'hr':
                cur_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
                nid = _resolve_id('employees', cur_id)
                find_url, new_url = '/hr/employees', '/hr/employees/new'
                target_url = f"/hr/employees/{nid}" if nid else find_url

            # Default fallback: Sales Orders
            if not target_url:
                nid = _resolve_id('sales_docs', None, f"doc_type = {PH}", ('Sales Order',))
                find_url = '/sales/orders'
                new_url = '/sales/orders/new'
                target_url = f"/sales/orders/{nid}" if nid else find_url

            return jsonify({'url': target_url, 'find_url': find_url, 'new_url': new_url})
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @app.route('/')
    def index():
        if 'user_id' in session:
            try:
                from auth import get_default_landing_endpoint
                return redirect(url_for(get_default_landing_endpoint()))
            except Exception:
                pass
        return redirect(url_for('auth.login'))
    return app

# ── WSGI entry-point for gunicorn (Render cloud) ──────────────────────────
wsgi_app = create_app()

if __name__ == '__main__':
    app = create_app()
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() in ['true', '1']
    app.run(debug=debug_mode, host='0.0.0.0', port=5000, use_reloader=False)
