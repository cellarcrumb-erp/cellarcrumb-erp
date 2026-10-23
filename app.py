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
    
    # Secure Session Cookies and Template Auto-Reload
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        TEMPLATES_AUTO_RELOAD=True
    )
    
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
                
            return {
                'user_can': user_can,
                'all_accessible_branches': branches,
                'active_branch_id': active_branch_id,
                'active_branch': active_branch,
                'can_switch_all_branches': (role in ['Admin', 'Manager'])
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
