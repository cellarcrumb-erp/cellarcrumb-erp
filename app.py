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
                'ui_theme': session.get('ui_theme', 'midnight'),
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

    @app.route('/api/theme', methods=['POST'])
    def api_set_theme():
        from flask import request, jsonify
        data = request.get_json(silent=True) or {}
        theme = data.get('theme') or request.form.get('theme') or 'midnight'
        if theme not in ('midnight', 'slate', 'light'):
            theme = 'midnight'
        session['ui_theme'] = theme
        return jsonify({'ok': True, 'theme': theme})

    @app.route('/api/sap-search')
    def api_sap_search():
        """Universal & Module Quick Search API (multi-word, case-insensitive, singular/plural tolerant)."""
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        from flask import request, jsonify
        from database import get_db, smart_search_sql
        q = (request.args.get('q') or '').strip()
        bp = (request.args.get('bp') or '').strip()
        conn = get_db()
        results = []
        try:
            # 1. Business Partners
            w_bp, p_bp = smart_search_sql(q, ['name', 'code', 'phone', 'email', 'partner_type'])
            bp_rows = conn.execute(f"SELECT id, code, name, partner_type, phone, balance FROM partners WHERE {w_bp} ORDER BY id ASC LIMIT 8", p_bp).fetchall()
            for r in bp_rows:
                results.append({
                    'module': 'bp_master',
                    'category': 'Business Partner',
                    'code': r['code'] or f"BP-{r['id']}",
                    'title': r['name'],
                    'subtitle': f"{r['partner_type']} • {r['phone'] or 'No phone'}",
                    'badge': f"KES {(r['balance'] or 0):,.2f}",
                    'url': f"/bp/{r['id']}"
                })

            # 2. Inventory Items
            w_it, p_it = smart_search_sql(q, ['i.name', 'i.code', 'i.item_type'])
            it_rows = conn.execute(f"""
                SELECT i.id, i.code, i.name, i.item_type, i.price, COALESCE(u.code, 'PCS') as uom_code
                FROM items i LEFT JOIN uom u ON i.uom_id = u.id
                WHERE {w_it} ORDER BY i.code ASC LIMIT 8
            """, p_it).fetchall()
            for r in it_rows:
                results.append({
                    'module': 'inventory',
                    'category': 'Item Master',
                    'code': r['code'],
                    'title': r['name'],
                    'subtitle': f"Type: {r['item_type']} • UOM: {r['uom_code']}",
                    'badge': f"KES {(r['price'] or 0):,.2f}",
                    'url': f"/inventory/items/{r['id']}"
                })

            # 3. Sales Documents
            from routes.sales import DOC_TYPES as S_DOC_TYPES
            s_kind_by_type = {v: k for k, v in S_DOC_TYPES.items()}
            w_sd, p_sd = smart_search_sql(q, ['d.doc_ref', 'd.doc_type', 'p.name', 'p.code', 'd.status', 'd.remarks'])
            sd_rows = conn.execute(f"""
                SELECT d.id, d.doc_ref, d.doc_type, d.doc_date, d.total, d.status, p.name as partner_name
                FROM sales_docs d LEFT JOIN partners p ON d.customer_id = p.id
                WHERE {w_sd} ORDER BY d.id DESC LIMIT 8
            """, p_sd).fetchall()
            for r in sd_rows:
                dk = s_kind_by_type.get(r['doc_type'], 'orders')
                results.append({
                    'module': 'sales',
                    'category': f"Sales • {r['doc_type']}",
                    'code': r['doc_ref'],
                    'title': r['partner_name'] or 'Customer',
                    'subtitle': f"{r['doc_date']} • Status: {r['status']}",
                    'badge': f"KES {(r['total'] or 0):,.2f}",
                    'url': f"/sales/{dk}/{r['id']}"
                })

            # 4. Purchase Documents
            from routes.purchases import DOC_TYPES as P_DOC_TYPES
            p_kind_by_type = {v: k for k, v in P_DOC_TYPES.items()}
            w_pd, p_pd = smart_search_sql(q, ['d.doc_ref', 'd.doc_type', 'p.name', 'p.code', 'd.status', 'd.remarks'])
            pd_rows = conn.execute(f"""
                SELECT d.id, d.doc_ref, d.doc_type, d.doc_date, d.total, d.status, p.name as partner_name
                FROM purchase_docs d LEFT JOIN partners p ON d.supplier_id = p.id
                WHERE {w_pd} ORDER BY d.id DESC LIMIT 8
            """, p_pd).fetchall()
            for r in pd_rows:
                dk = p_kind_by_type.get(r['doc_type'], 'orders')
                results.append({
                    'module': 'purchases',
                    'category': f"Purchasing • {r['doc_type']}",
                    'code': r['doc_ref'],
                    'title': r['partner_name'] or 'Supplier',
                    'subtitle': f"{r['doc_date']} • Status: {r['status']}",
                    'badge': f"KES {(r['total'] or 0):,.2f}",
                    'url': f"/purchases/{dk}/{r['id']}"
                })

            # 5. Payments
            w_pay, p_pay = smart_search_sql(q, ['p.doc_ref', 'p.payment_type', 'bp.name', 'p.reference'])
            pay_rows = conn.execute(f"""
                SELECT p.id, p.doc_ref, p.payment_type, p.doc_date, p.amount, p.status, bp.name as partner_name
                FROM payments p LEFT JOIN partners bp ON p.partner_id = bp.id
                WHERE {w_pay} ORDER BY p.id DESC LIMIT 5
            """, p_pay).fetchall()
            for r in pay_rows:
                results.append({
                    'module': 'payments',
                    'category': f"Payment • {r['payment_type']}",
                    'code': r['doc_ref'],
                    'title': r['partner_name'] or r['payment_type'],
                    'subtitle': f"{r['doc_date']} • {r['status']}",
                    'badge': f"KES {(r['amount'] or 0):,.2f}",
                    'url': f"/payments/{r['id']}"
                })

            # 6. Finance COA & Journals
            w_coa, p_coa = smart_search_sql(q, ['code', 'name', 'account_type'])
            coa_rows = conn.execute(f"SELECT id, code, name, account_type FROM coa WHERE {w_coa} ORDER BY code ASC LIMIT 5", p_coa).fetchall()
            for r in coa_rows:
                results.append({
                    'module': 'finance',
                    'category': 'Chart of Accounts',
                    'code': r['code'],
                    'title': r['name'],
                    'subtitle': r['account_type'],
                    'badge': 'G/L Account',
                    'url': f"/finance/coa/{r['id']}"
                })

            # 7. Employees
            w_emp, p_emp = smart_search_sql(q, ['emp_no', 'full_name', 'email'])
            emp_rows = conn.execute(f"SELECT id, emp_no, full_name, email, status FROM employees WHERE {w_emp} ORDER BY id DESC LIMIT 5", p_emp).fetchall()
            for r in emp_rows:
                results.append({
                    'module': 'hr',
                    'category': 'Employee Master',
                    'code': r['emp_no'],
                    'title': r['full_name'],
                    'subtitle': r['email'] or 'HR Record',
                    'badge': r['status'],
                    'url': f"/hr/employees/{r['id']}"
                })

            # Prioritize active blueprint results at the top
            if bp:
                results.sort(key=lambda x: (0 if x.get('module') == bp else 1))

            return jsonify({'results': results[:25], 'query': q})
        finally:
            try:
                conn.close()
            except Exception:
                pass

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
            cur_id = None
            nid = None

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

            # 1. Sales Documents (/sales/<doc_kind>/<id> or /sales/<doc_kind>/new or /sales/<doc_kind> or /sales)
            if bp == 'sales':
                from routes.sales import DOC_TYPES
                doc_kind = parts[1] if len(parts) >= 2 and parts[1] in DOC_TYPES else 'orders'
                dtype = DOC_TYPES[doc_kind]
                cur_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
                nid = _resolve_id('sales_docs', cur_id, f"doc_type = {PH}", (dtype,))
                find_url = f"/sales/{doc_kind}"
                new_url = f"/sales/{doc_kind}/new"
                target_url = f"/sales/{doc_kind}/{nid}" if nid else find_url

            # 2. Purchase Documents (/purchases/<doc_kind>/<id> or /purchases)
            elif bp == 'purchases':
                from routes.purchases import DOC_TYPES as P_DOC_TYPES
                doc_kind = parts[1] if len(parts) >= 2 and parts[1] in P_DOC_TYPES else 'orders'
                dtype = P_DOC_TYPES[doc_kind]
                cur_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
                nid = _resolve_id('purchase_docs', cur_id, f"doc_type = {PH}", (dtype,))
                find_url = f"/purchases/{doc_kind}"
                new_url = f"/purchases/{doc_kind}/new"
                target_url = f"/purchases/{doc_kind}/{nid}" if nid else find_url

            # 3. Inventory (items, warehouses, receipts, issues, transfers, counts)
            elif bp == 'inventory':
                sub = parts[1] if len(parts) >= 2 else 'items'
                cur_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
                inv_map = {
                    'items': ('items', '/inventory/items', '/inventory/items/new'),
                    'warehouses': ('warehouses', '/inventory/warehouses', '/inventory/warehouses/new'),
                    'receipts': ('goods_receipts', '/inventory/receipts', '/inventory/receipts/new'),
                    'issues': ('goods_issues', '/inventory/issues', '/inventory/issues/new'),
                    'transfers': ('stock_transfers', '/inventory/transfers', '/inventory/transfers/new'),
                    'counts': ('inventory_counts', '/inventory/counts', '/inventory/counts/new'),
                }
                if sub not in inv_map:
                    sub = 'items'
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
                for p in parts[1:]:
                    if p.isdigit():
                        cur_id = int(p)
                        break
                nid = _resolve_id('payments', cur_id)
                find_url = '/payments/incoming'
                new_url = '/payments/incoming/new'
                target_url = f"/payments/{nid}" if nid else find_url

            # 6. Banking (/banking/accounts/<id> or /banking/deposits/<id> etc.)
            elif bp == 'banking':
                sub = parts[1] if len(parts) >= 2 else 'accounts'
                cur_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
                if sub in ('deposits', 'withdrawals', 'transfers'):
                    nid = _resolve_id('bank_transactions', cur_id)
                    find_url, new_url = f"/banking/{sub}", f"/banking/{sub}/new"
                    target_url = f"/banking/{sub}/{nid}" if nid else find_url
                else:
                    nid = _resolve_id('bank_accounts', cur_id)
                    find_url, new_url = '/banking/accounts', '/banking/accounts/new'
                    target_url = f"/banking/accounts/{nid}" if nid else find_url

            # 7. Production (/production/orders/<id> or /production/bom/<id>)
            elif bp == 'production':
                sub = parts[1] if len(parts) >= 2 else 'orders'
                cur_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
                if sub == 'bom':
                    nid = _resolve_id('bom', cur_id)
                    find_url, new_url = '/production/bom', '/production/bom/new'
                    target_url = f"/production/bom/{nid}" if nid else find_url
                else:
                    nid = _resolve_id('production_orders', cur_id)
                    find_url, new_url = '/production/orders', '/production/orders/new'
                    target_url = f"/production/orders/{nid}" if nid else find_url

            # 8. Finance (/finance/journals/<id> or /finance/coa/<id>)
            elif bp == 'finance':
                sub = parts[1] if len(parts) >= 2 else 'journals'
                cur_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
                if sub == 'coa':
                    nid = _resolve_id('coa', cur_id)
                    find_url, new_url = '/finance/coa', '/finance/coa/new'
                    target_url = f"/finance/coa/{nid}" if nid else find_url
                else:
                    nid = _resolve_id('journal_entries', cur_id)
                    find_url, new_url = '/finance/journals', '/finance/journals/new'
                    target_url = f"/finance/journals/{nid}" if nid else find_url

            # 9. HR (/hr/employees/<id>)
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

            same_record = bool(cur_id and nid and int(cur_id) == int(nid))
            return jsonify({
                'url': target_url,
                'find_url': find_url,
                'new_url': new_url,
                'current_id': cur_id,
                'target_id': nid,
                'same_record': same_record,
                'has_records': bool(nid is not None)
            })
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
