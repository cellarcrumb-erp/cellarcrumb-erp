"""
database.py — Dual-mode database layer
  • LOCAL  (no DATABASE_URL env var): uses SQLite at %LOCALAPPDATA%/CellarCrumbERP/erp.db
  • CLOUD  (DATABASE_URL set by Render/Heroku): uses PostgreSQL via psycopg2

All SQL is kept compatible via a small adapter:
  - Placeholder:  SQLite uses "?",  Postgres uses "%s"   → use DB_PLACEHOLDER
  - AUTOINCREMENT: handled per dialect in CREATE TABLE statements
  - UPSERT: INSERT OR IGNORE  →  INSERT ... ON CONFLICT DO NOTHING
  - PRAGMA: SQLite-only, skipped on Postgres
"""

import os
import re
import json
from pathlib import Path
from datetime import datetime
import bcrypt

# ─────────────────────────────────────────────────────────────────────────────
# Detect environment
# ─────────────────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get('DATABASE_URL', '')

# Render sometimes gives postgres:// which psycopg2 needs as postgresql://
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

USE_POSTGRES = bool(DATABASE_URL)

# For SQLite fallback
DB_PATH = Path(os.getenv('DB_PATH', '') or
               os.path.join(os.getenv('LOCALAPPDATA', '/tmp'), 'CellarCrumbERP', 'erp.db'))

# SQL placeholder token for parameterised queries
PH = '%s' if USE_POSTGRES else '?'


# ─────────────────────────────────────────────────────────────────────────────
# Connection helpers
# ─────────────────────────────────────────────────────────────────────────────
def get_db():
    """Return an open database connection (SQLite or PostgreSQL)."""
    if USE_POSTGRES:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        conn.autocommit = False
        return _PgWrapper(conn)
    else:
        import sqlite3
        os.makedirs(DB_PATH.parent, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA journal_mode = WAL')
        return conn


class _PgWrapper:
    """
    Thin wrapper around a psycopg2 connection that makes it behave
    like the sqlite3 connection our routes expect:
      - .execute(sql, params)  → returns a cursor-like object
      - .cursor()
      - .commit() / .close()
      - .row_factory  (ignored — RealDictCursor already returns dict-like rows)
    """
    def __init__(self, pg_conn):
        self._conn = pg_conn
        self.row_factory = None   # compatibility shim

    # Translate SQLite queries to PostgreSQL dialect on the fly
    @staticmethod
    def _fix(sql):
        if not sql:
            return sql
        s = sql.replace('?', '%s')
        # SQLite datetime('now') and date('now')
        s = re.sub(r"\bdatetime\('now'\)", "NOW()", s, flags=re.IGNORECASE)
        s = re.sub(r"\bdate\('now'\)", "CURRENT_DATE", s, flags=re.IGNORECASE)
        # SQLite IFNULL(a, b) -> COALESCE(a, b)
        s = re.sub(r'\bIFNULL\(', 'COALESCE(', s, flags=re.IGNORECASE)
        # SQLite strftime('%Y-%m', col) -> SUBSTR(col::text, 1, 7)
        s = re.sub(r"strftime\('%Y-%m',\s*([^)]+)\)", r"SUBSTR(\1::text, 1, 7)", s, flags=re.IGNORECASE)
        # INSERT OR IGNORE INTO -> INSERT INTO ... ON CONFLICT DO NOTHING
        if re.search(r'\bINSERT\s+OR\s+IGNORE\s+INTO\b', s, flags=re.IGNORECASE):
            s = re.sub(r'\bINSERT\s+OR\s+IGNORE\s+INTO\b', 'INSERT INTO', s, flags=re.IGNORECASE)
            if 'ON CONFLICT' not in s.upper():
                s = s.rstrip(';') + ' ON CONFLICT DO NOTHING'
        # Boolean columns: is_<col> = 1/0 -> is_<col> = TRUE/FALSE
        s = re.sub(r'\b(is_\w+)\s*=\s*CASE\s+WHEN\s+(is_\w+)\s*=\s*1\s+THEN\s+0\s+ELSE\s+1\s+END', r'\1 = NOT \2', s, flags=re.IGNORECASE)
        s = re.sub(r'\b(is_\w+)\s*=\s*1\b', r'\1 = TRUE', s, flags=re.IGNORECASE)
        s = re.sub(r'\b(is_\w+)\s*=\s*0\b', r'\1 = FALSE', s, flags=re.IGNORECASE)
        s = re.sub(r'\b(is_\w+)\s*!=\s*1\b', r'\1 != TRUE', s, flags=re.IGNORECASE)
        s = re.sub(r'\b(is_\w+)\s*!=\s*0\b', r'\1 != FALSE', s, flags=re.IGNORECASE)
        s = re.sub(r'\b(is_\w+)\s*<>\s*1\b', r'\1 <> TRUE', s, flags=re.IGNORECASE)
        s = re.sub(r'\b(is_\w+)\s*<>\s*0\b', r'\1 <> FALSE', s, flags=re.IGNORECASE)
        return s

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(self._fix(sql), params)
        return _PgCursor(cur)

    def cursor(self):
        raw = self._conn.cursor()
        return _PgCursorProxy(raw, self._fix)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if args[0] is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()


class _PgCursor:
    """Cursor returned by _PgWrapper.execute() — mimics sqlite3.Cursor."""
    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        return _DictRow(row)

    def fetchall(self):
        return [_DictRow(r) for r in self._cur.fetchall()]

    @property
    def lastrowid(self):
        return self._cur.fetchone()[0] if self._cur.description else None


class _PgCursorProxy:
    """Cursor returned by _PgWrapper.cursor() — used by routes that call conn.cursor()."""
    def __init__(self, cur, fix_fn):
        self._cur = cur
        self._fix = fix_fn

    def execute(self, sql, params=()):
        self._cur.execute(self._fix(sql), params)
        return self

    def executemany(self, sql, seq):
        self._cur.executemany(self._fix(sql), seq)

    def fetchone(self):
        row = self._cur.fetchone()
        return _DictRow(row) if row else None

    def fetchall(self):
        return [_DictRow(r) for r in self._cur.fetchall()]

    @property
    def lastrowid(self):
        # After INSERT ... RETURNING id we can get it; otherwise use currval trick
        try:
            row = self._cur.fetchone()
            if row:
                return list(row.values())[0]
        except Exception:
            pass
        return None

    @property
    def rowcount(self):
        return self._cur.rowcount


class _DictRow:
    """Makes psycopg2 RealDictRow subscriptable like sqlite3.Row (both key & index)."""
    def __init__(self, row):
        self._row = dict(row) if row else {}
        self._keys = list(self._row.keys())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._row[self._keys[key]]
        return self._row[key]

    def __contains__(self, key):
        return key in self._row

    def keys(self):
        return self._keys

    def get(self, key, default=None):
        return self._row.get(key, default)

    def __repr__(self):
        return repr(self._row)


# ─────────────────────────────────────────────────────────────────────────────
# Schema helpers: generate dialect-appropriate CREATE TABLE / INSERT statements
# ─────────────────────────────────────────────────────────────────────────────
def _pk(col='id'):
    """Primary key column definition."""
    if USE_POSTGRES:
        return f'{col} SERIAL PRIMARY KEY'
    return f'{col} INTEGER PRIMARY KEY'


def _ref(table, col='id'):
    """Foreign key reference fragment."""
    return f'REFERENCES {table}({col})'


def _upsert_ignore(table, cols, vals, conn):
    """INSERT OR IGNORE (SQLite) / INSERT ... ON CONFLICT DO NOTHING (Postgres)."""
    placeholders = ', '.join([PH] * len(vals))
    col_str = ', '.join(cols)
    if USE_POSTGRES:
        sql = f'INSERT INTO {table} ({col_str}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'
    else:
        sql = f'INSERT OR IGNORE INTO {table} ({col_str}) VALUES ({placeholders})'
    conn.execute(sql, vals)


def _insert_returning(cursor, table, cols, vals):
    """INSERT and return the new row id."""
    placeholders = ', '.join([PH] * len(vals))
    col_str = ', '.join(cols)
    if USE_POSTGRES:
        sql = f'INSERT INTO {table} ({col_str}) VALUES ({placeholders}) RETURNING id'
        cursor.execute(sql, vals)
        row = cursor.fetchone()
        return row['id'] if row else None
    else:
        sql = f'INSERT INTO {table} ({col_str}) VALUES ({placeholders})'
        cursor.execute(sql, vals)
        return cursor.lastrowid


# ─────────────────────────────────────────────────────────────────────────────
# Shared utility functions (unchanged API)
# ─────────────────────────────────────────────────────────────────────────────
def next_ref(module, doc_type, conn=None):
    close_at_end = False
    if conn is None:
        conn = get_db()
        close_at_end = True

    cursor = conn.cursor()
    cursor.execute(
        f"SELECT prefix, next_num FROM num_series WHERE module = {PH} AND doc_type = {PH}",
        (module, doc_type)
    )
    row = cursor.fetchone()
    if not row:
        prefix = doc_type[:2].upper()
        if USE_POSTGRES:
            cursor.execute(
                "INSERT INTO num_series (module, doc_type, prefix, next_num) VALUES (%s,%s,%s,2) ON CONFLICT DO NOTHING",
                (module, doc_type, prefix)
            )
        else:
            cursor.execute(
                "INSERT OR IGNORE INTO num_series (module, doc_type, prefix, next_num) VALUES (?,?,?,2)",
                (module, doc_type, prefix)
            )
        if close_at_end:
            conn.commit()
            conn.close()
        return f"{prefix}-000001"

    prefix, next_num = row['prefix'], row['next_num']

    while True:
        candidate_ref = f"{prefix}-{next_num:06d}"
        next_num += 1
        found = False
        for table in ['sales_docs', 'purchase_docs', 'goods_receipts', 'goods_issues', 'stock_transfers', 'inventory_counts']:
            try:
                exists = cursor.execute(
                    f"SELECT 1 FROM {table} WHERE doc_ref = {PH}", (candidate_ref,)
                ).fetchone()
                if exists:
                    found = True
                    break
            except Exception:
                pass
        if not found:
            break

    cursor.execute(
        f"UPDATE num_series SET next_num = {PH} WHERE module = {PH} AND doc_type = {PH}",
        (next_num, module, doc_type)
    )
    if close_at_end:
        conn.commit()
        conn.close()
    return candidate_ref


def post_journal(conn, doc_ref, doc_date, module, source_doc_type, source_doc_id,
                 description, lines, user_id=None, branch_id=None):
    parsed_lines = []
    for l in lines:
        if isinstance(l, (list, tuple)):
            acc_id = l[0]
            partner_id = l[1] if len(l) > 1 else None
            debit = float(l[2]) if len(l) > 2 else 0.0
            credit = float(l[3]) if len(l) > 3 else 0.0
            desc = l[4] if len(l) > 4 else description
        elif isinstance(l, dict):
            acc_id = l.get('account_id')
            partner_id = l.get('partner_id')
            debit = float(l.get('debit', 0.0))
            credit = float(l.get('credit', 0.0))
            desc = l.get('description', description)
        else:
            continue
        parsed_lines.append((acc_id, partner_id, debit, credit, desc))

    total_debit = sum(p[2] for p in parsed_lines)
    total_credit = sum(p[3] for p in parsed_lines)
    if round(total_debit, 2) != round(total_credit, 2):
        raise ValueError(f"Journal entry unbalanced: Debit {total_debit:.2f} != Credit {total_credit:.2f}")

    cursor = conn.cursor()
    je_cols = ['doc_ref', 'doc_date', 'module', 'source_doc_type', 'source_doc_id',
               'description', 'status', 'posted_by', 'created_at', 'branch_id']
    je_vals = (doc_ref, doc_date, module, source_doc_type, source_doc_id,
               description, 'Posted', user_id, datetime.now().isoformat(), branch_id)
    journal_id = _insert_returning(cursor, 'journal_entries', je_cols, je_vals)

    for acc_id, partner_id, debit, credit, desc in parsed_lines:
        cursor.execute(
            f"""INSERT INTO journal_lines (journal_id, account_id, partner_id, debit, credit, description)
                VALUES ({PH},{PH},{PH},{PH},{PH},{PH})""",
            (journal_id, acc_id, partner_id, debit, credit, desc)
        )
    return journal_id


def update_stock(conn, item_id, warehouse_id, qty_in, qty_out, doc_type, doc_id, doc_ref, unit_cost, user_id=None):
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT quantity FROM stock_balances WHERE item_id = {PH} AND warehouse_id = {PH}",
        (item_id, warehouse_id)
    )
    row = cursor.fetchone()
    current_qty = row['quantity'] if row else 0
    new_qty = current_qty + qty_in - qty_out

    if row:
        cursor.execute(
            f"UPDATE stock_balances SET quantity = {PH} WHERE item_id = {PH} AND warehouse_id = {PH}",
            (new_qty, item_id, warehouse_id)
        )
    else:
        cursor.execute(
            f"INSERT INTO stock_balances (item_id, warehouse_id, quantity) VALUES ({PH},{PH},{PH})",
            (item_id, warehouse_id, new_qty)
        )

    cursor.execute(
        f"""INSERT INTO stock_ledger
            (ts, item_id, warehouse_id, doc_type, doc_id, doc_ref, qty_in, qty_out, balance, unit_cost, posted_by)
            VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH})""",
        (datetime.now().isoformat(), item_id, warehouse_id, doc_type, doc_id, doc_ref,
         qty_in, qty_out, new_qty, unit_cost, user_id)
    )


def log_audit(conn, user_id, module, action, doc_type, doc_id, description):
    try:
        conn.execute(
            f"""INSERT INTO audit_log (ts, user_id, module, action, doc_type, doc_id, description)
                VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH})""",
            (datetime.now().isoformat(), user_id, module, action, doc_type, doc_id, description)
        )
        conn.commit()
    except Exception:
        pass


def get_account_id(conn, code):
    cursor = conn.cursor()
    cursor.execute(f"SELECT id FROM coa WHERE code = {PH}", (code,))
    row = cursor.fetchone()
    return row['id'] if row else None


# ─────────────────────────────────────────────────────────────────────────────
# Schema initialisation
# ─────────────────────────────────────────────────────────────────────────────
def init_db():
    conn = get_db()
    c = conn.cursor()

    if USE_POSTGRES:
        _init_postgres(c, conn)
    else:
        _init_sqlite(c, conn)

    conn.commit()
    conn.close()


# ---------- SQLite (original, unchanged logic) ----------
def _init_sqlite(c, conn):
    import sqlite3
    queries = [
        "CREATE TABLE IF NOT EXISTS company (id INTEGER PRIMARY KEY, name TEXT, address TEXT, phone TEXT, email TEXT, tax_id TEXT, currency TEXT, logo_path TEXT, financial_year_start TEXT)",
        "CREATE TABLE IF NOT EXISTS roles (id INTEGER PRIMARY KEY, name TEXT, description TEXT, permissions TEXT DEFAULT '{}')",
        "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, email TEXT, password_hash TEXT, role_id INTEGER REFERENCES roles(id), is_active BOOLEAN, created_at TEXT, last_login TEXT)",
        "CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY, ts TEXT, user_id INTEGER, module TEXT, action TEXT, doc_type TEXT, doc_id INTEGER, description TEXT)",
        "CREATE TABLE IF NOT EXISTS num_series (id INTEGER PRIMARY KEY, module TEXT, doc_type TEXT, prefix TEXT, next_num INTEGER, UNIQUE(module, doc_type))",
        "CREATE TABLE IF NOT EXISTS uom (id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT, base_uom TEXT)",
        "CREATE TABLE IF NOT EXISTS taxes (id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT, rate REAL, tax_type TEXT, is_active BOOLEAN)",
        "CREATE TABLE IF NOT EXISTS currencies (id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT, symbol TEXT, exchange_rate REAL)",
        "CREATE TABLE IF NOT EXISTS payment_methods (id INTEGER PRIMARY KEY, name TEXT UNIQUE, method_type TEXT, bank_account_id INTEGER, is_active BOOLEAN)",
        "CREATE TABLE IF NOT EXISTS approval_workflows (id INTEGER PRIMARY KEY, module TEXT, doc_type TEXT, condition_field TEXT, condition_op TEXT, condition_value TEXT, approver_role_id INTEGER, is_active BOOLEAN)",
        "CREATE TABLE IF NOT EXISTS partners (id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT, partner_type TEXT, phone TEXT, email TEXT, address TEXT, tax_id TEXT, payment_terms TEXT, credit_limit REAL, opening_balance REAL, currency TEXT, status TEXT, notes TEXT, created_at TEXT, balance REAL)",
        "CREATE TABLE IF NOT EXISTS branches (id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT, address TEXT, phone TEXT, email TEXT, tax_id TEXT, default_warehouse_id INTEGER, is_main BOOLEAN DEFAULT 0, is_active BOOLEAN DEFAULT 1, created_at TEXT)",
        "CREATE TABLE IF NOT EXISTS user_branches (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), branch_id INTEGER REFERENCES branches(id), is_default BOOLEAN DEFAULT 0, UNIQUE(user_id, branch_id))",
        "CREATE TABLE IF NOT EXISTS warehouses (id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT, location TEXT, is_active BOOLEAN, branch_id INTEGER REFERENCES branches(id))",
        "CREATE TABLE IF NOT EXISTS item_groups (id INTEGER PRIMARY KEY, name TEXT UNIQUE, parent_id INTEGER REFERENCES item_groups(id))",
        "CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT, description TEXT, group_id INTEGER REFERENCES item_groups(id), item_type TEXT, uom_id INTEGER REFERENCES uom(id), cost REAL, price REAL, reorder_level REAL, tax_id INTEGER REFERENCES taxes(id), is_active BOOLEAN, is_stock_item BOOLEAN, created_at TEXT)",
        "CREATE TABLE IF NOT EXISTS stock_balances (id INTEGER PRIMARY KEY, item_id INTEGER REFERENCES items(id), warehouse_id INTEGER REFERENCES warehouses(id), quantity REAL, UNIQUE(item_id, warehouse_id))",
        "CREATE TABLE IF NOT EXISTS stock_ledger (id INTEGER PRIMARY KEY, ts TEXT, item_id INTEGER REFERENCES items(id), warehouse_id INTEGER REFERENCES warehouses(id), doc_type TEXT, doc_id INTEGER, doc_ref TEXT, qty_in REAL, qty_out REAL, balance REAL, unit_cost REAL, posted_by INTEGER REFERENCES users(id))",
        "CREATE TABLE IF NOT EXISTS goods_receipts (id INTEGER PRIMARY KEY, doc_ref TEXT UNIQUE, doc_date TEXT, warehouse_id INTEGER REFERENCES warehouses(id), partner_id INTEGER REFERENCES partners(id), source_doc_type TEXT, source_doc_id INTEGER, status TEXT, remarks TEXT, total_value REAL, posted_by INTEGER REFERENCES users(id), posted_at TEXT, created_at TEXT, branch_id INTEGER REFERENCES branches(id), source_doc_ref TEXT)",
        "CREATE TABLE IF NOT EXISTS goods_receipt_lines (id INTEGER PRIMARY KEY, receipt_id INTEGER REFERENCES goods_receipts(id), item_id INTEGER REFERENCES items(id), quantity REAL, unit_cost REAL, total REAL, po_line_id INTEGER, copied_qty REAL DEFAULT 0, source_doc_type TEXT, source_doc_id INTEGER, source_line_id INTEGER)",
        "CREATE TABLE IF NOT EXISTS goods_issues (id INTEGER PRIMARY KEY, doc_ref TEXT UNIQUE, doc_date TEXT, warehouse_id INTEGER REFERENCES warehouses(id), issue_type TEXT, source_doc_type TEXT, source_doc_id INTEGER, status TEXT, remarks TEXT, total_value REAL, posted_by INTEGER REFERENCES users(id), posted_at TEXT, created_at TEXT, branch_id INTEGER REFERENCES branches(id), source_doc_ref TEXT)",
        "CREATE TABLE IF NOT EXISTS goods_issue_lines (id INTEGER PRIMARY KEY, issue_id INTEGER REFERENCES goods_issues(id), item_id INTEGER REFERENCES items(id), quantity REAL, unit_cost REAL, total REAL, copied_qty REAL DEFAULT 0, source_doc_type TEXT, source_doc_id INTEGER, source_line_id INTEGER)",
        "CREATE TABLE IF NOT EXISTS stock_transfers (id INTEGER PRIMARY KEY, doc_ref TEXT UNIQUE, doc_date TEXT, from_warehouse_id INTEGER REFERENCES warehouses(id), to_warehouse_id INTEGER REFERENCES warehouses(id), status TEXT, remarks TEXT, posted_by INTEGER REFERENCES users(id), posted_at TEXT, created_at TEXT, from_branch_id INTEGER REFERENCES branches(id), to_branch_id INTEGER REFERENCES branches(id), source_doc_id INTEGER, source_doc_type TEXT, source_doc_ref TEXT)",
        "CREATE TABLE IF NOT EXISTS stock_transfer_lines (id INTEGER PRIMARY KEY, transfer_id INTEGER REFERENCES stock_transfers(id), item_id INTEGER REFERENCES items(id), quantity REAL, unit_cost REAL, copied_qty REAL DEFAULT 0, source_doc_type TEXT, source_doc_id INTEGER, source_line_id INTEGER)",
        "CREATE TABLE IF NOT EXISTS inventory_counts (id INTEGER PRIMARY KEY, doc_ref TEXT UNIQUE, doc_date TEXT, warehouse_id INTEGER REFERENCES warehouses(id), status TEXT, remarks TEXT, posted_by INTEGER REFERENCES users(id), posted_at TEXT, created_at TEXT, branch_id INTEGER REFERENCES branches(id))",
        "CREATE TABLE IF NOT EXISTS inventory_count_lines (id INTEGER PRIMARY KEY, count_id INTEGER REFERENCES inventory_counts(id), item_id INTEGER REFERENCES items(id), system_qty REAL, counted_qty REAL, variance REAL, unit_cost REAL)",
        "CREATE TABLE IF NOT EXISTS sales_docs (id INTEGER PRIMARY KEY, doc_ref TEXT UNIQUE, doc_type TEXT, doc_date TEXT, due_date TEXT, customer_id INTEGER REFERENCES partners(id), warehouse_id INTEGER REFERENCES warehouses(id), status TEXT, source_doc_id INTEGER, source_doc_type TEXT, subtotal REAL, tax_total REAL, total REAL, amount_paid REAL, balance_due REAL, remarks TEXT, posted_by INTEGER REFERENCES users(id), posted_at TEXT, created_at TEXT, branch_id INTEGER REFERENCES branches(id), source_doc_ref TEXT)",
        "CREATE TABLE IF NOT EXISTS sales_lines (id INTEGER PRIMARY KEY, doc_id INTEGER REFERENCES sales_docs(id), item_id INTEGER REFERENCES items(id), description TEXT, quantity REAL, unit_price REAL, discount_pct REAL, tax_id INTEGER REFERENCES taxes(id), tax_amount REAL, line_total REAL, cost_price REAL, copied_qty REAL DEFAULT 0, source_doc_type TEXT, source_doc_id INTEGER, source_line_id INTEGER)",
        "CREATE TABLE IF NOT EXISTS purchase_docs (id INTEGER PRIMARY KEY, doc_ref TEXT UNIQUE, doc_type TEXT, doc_date TEXT, due_date TEXT, supplier_id INTEGER REFERENCES partners(id), warehouse_id INTEGER REFERENCES warehouses(id), status TEXT, source_doc_id INTEGER, source_doc_type TEXT, subtotal REAL, tax_total REAL, total REAL, amount_paid REAL, balance_due REAL, remarks TEXT, posted_by INTEGER REFERENCES users(id), posted_at TEXT, created_at TEXT, branch_id INTEGER REFERENCES branches(id), source_doc_ref TEXT)",
        "CREATE TABLE IF NOT EXISTS purchase_lines (id INTEGER PRIMARY KEY, doc_id INTEGER REFERENCES purchase_docs(id), item_id INTEGER REFERENCES items(id), description TEXT, quantity REAL, unit_cost REAL, discount_pct REAL, tax_id INTEGER REFERENCES taxes(id), tax_amount REAL, line_total REAL, copied_qty REAL DEFAULT 0, source_doc_type TEXT, source_doc_id INTEGER, source_line_id INTEGER)",
        "CREATE TABLE IF NOT EXISTS bom (id INTEGER PRIMARY KEY, finished_item_id INTEGER REFERENCES items(id), name TEXT, quantity_produced REAL, is_active BOOLEAN, notes TEXT, created_at TEXT, UNIQUE(finished_item_id, name))",
        "CREATE TABLE IF NOT EXISTS bom_lines (id INTEGER PRIMARY KEY, bom_id INTEGER REFERENCES bom(id), item_id INTEGER REFERENCES items(id), quantity REAL, uom_id INTEGER REFERENCES uom(id), notes TEXT)",
        "CREATE TABLE IF NOT EXISTS production_orders (id INTEGER PRIMARY KEY, doc_ref TEXT UNIQUE, doc_date TEXT, planned_date TEXT, bom_id INTEGER REFERENCES bom(id), warehouse_id INTEGER REFERENCES warehouses(id), planned_qty REAL, produced_qty REAL, wastage_qty REAL, status TEXT, batch_no TEXT, total_cost REAL, remarks TEXT, posted_by INTEGER REFERENCES users(id), created_at TEXT, branch_id INTEGER REFERENCES branches(id))",
        "CREATE TABLE IF NOT EXISTS production_material_issues (id INTEGER PRIMARY KEY, production_order_id INTEGER REFERENCES production_orders(id), item_id INTEGER REFERENCES items(id), planned_qty REAL, issued_qty REAL, unit_cost REAL, issue_date TEXT)",
        "CREATE TABLE IF NOT EXISTS production_wastage (id INTEGER PRIMARY KEY, production_order_id INTEGER REFERENCES production_orders(id), item_id INTEGER REFERENCES items(id), wastage_qty REAL, reason TEXT, doc_date TEXT)",
        "CREATE TABLE IF NOT EXISTS coa (id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT, account_type TEXT, parent_id INTEGER REFERENCES coa(id), is_posting BOOLEAN, is_active BOOLEAN, opening_balance REAL, notes TEXT)",
        "CREATE TABLE IF NOT EXISTS journal_entries (id INTEGER PRIMARY KEY, doc_ref TEXT UNIQUE, doc_date TEXT, module TEXT, source_doc_type TEXT, source_doc_id INTEGER, description TEXT, status TEXT, posted_by INTEGER REFERENCES users(id), created_at TEXT, branch_id INTEGER REFERENCES branches(id))",
        "CREATE TABLE IF NOT EXISTS journal_lines (id INTEGER PRIMARY KEY, journal_id INTEGER REFERENCES journal_entries(id), account_id INTEGER REFERENCES coa(id), partner_id INTEGER REFERENCES partners(id), debit REAL, credit REAL, description TEXT)",
        "CREATE TABLE IF NOT EXISTS financial_periods (id INTEGER PRIMARY KEY, name TEXT UNIQUE, start_date TEXT, end_date TEXT, status TEXT)",
        "CREATE TABLE IF NOT EXISTS departments (id INTEGER PRIMARY KEY, name TEXT UNIQUE, manager_id INTEGER, is_active BOOLEAN)",
        "CREATE TABLE IF NOT EXISTS job_titles (id INTEGER PRIMARY KEY, name TEXT UNIQUE, department_id INTEGER REFERENCES departments(id))",
        "CREATE TABLE IF NOT EXISTS employees (id INTEGER PRIMARY KEY, emp_no TEXT UNIQUE, full_name TEXT, email TEXT, phone TEXT, address TEXT, department_id INTEGER REFERENCES departments(id), job_title_id INTEGER REFERENCES job_titles(id), partner_id INTEGER REFERENCES partners(id), date_hired TEXT, date_terminated TEXT, employment_type TEXT, monthly_salary REAL, status TEXT, emergency_contact TEXT, emergency_phone TEXT, national_id TEXT, kra_pin TEXT, notes TEXT, created_at TEXT)",
        "CREATE TABLE IF NOT EXISTS leave_records (id INTEGER PRIMARY KEY, employee_id INTEGER REFERENCES employees(id), leave_type TEXT, start_date TEXT, end_date TEXT, days REAL, status TEXT, approved_by INTEGER REFERENCES users(id), remarks TEXT, created_at TEXT)",
        "CREATE TABLE IF NOT EXISTS bank_accounts (id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT, account_type TEXT, bank_name TEXT, account_number TEXT, currency TEXT, coa_id INTEGER REFERENCES coa(id), opening_balance REAL, current_balance REAL, is_active BOOLEAN, created_at TEXT)",
        "CREATE TABLE IF NOT EXISTS bank_transactions (id INTEGER PRIMARY KEY, doc_ref TEXT UNIQUE, doc_date TEXT, bank_account_id INTEGER REFERENCES bank_accounts(id), txn_type TEXT, amount REAL, description TEXT, reference TEXT, partner_id INTEGER REFERENCES partners(id), is_reconciled BOOLEAN, reconciled_at TEXT, journal_id INTEGER REFERENCES journal_entries(id), created_at TEXT)",
        "CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY, doc_ref TEXT UNIQUE, doc_date TEXT, payment_type TEXT, partner_id INTEGER REFERENCES partners(id), bank_account_id INTEGER REFERENCES bank_accounts(id), payment_method_id INTEGER REFERENCES payment_methods(id), amount REAL, amount_allocated REAL, unallocated REAL, reference TEXT, remarks TEXT, status TEXT, journal_id INTEGER REFERENCES journal_entries(id), posted_by INTEGER REFERENCES users(id), posted_at TEXT, created_at TEXT, branch_id INTEGER REFERENCES branches(id))",
        "CREATE TABLE IF NOT EXISTS payment_allocations (id INTEGER PRIMARY KEY, payment_id INTEGER REFERENCES payments(id), doc_type TEXT, doc_id INTEGER, amount REAL, allocation_date TEXT)",
    ]
    for q in queries:
        c.execute(q)

    # Seed data only on empty DB
    _seed_data(c, conn, sqlite_mode=True)

    conn.commit()


def _init_postgres(c, conn):
    """Create all tables using PostgreSQL-compatible DDL."""
    tables = [
        """CREATE TABLE IF NOT EXISTS company (
            id SERIAL PRIMARY KEY, name TEXT, address TEXT, phone TEXT, email TEXT,
            tax_id TEXT, currency TEXT, logo_path TEXT, financial_year_start TEXT)""",
        """CREATE TABLE IF NOT EXISTS roles (
            id SERIAL PRIMARY KEY, name TEXT, description TEXT, permissions TEXT DEFAULT '{}')""",
        """CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY, username TEXT, full_name TEXT, email TEXT,
            password_hash TEXT, role_id INTEGER REFERENCES roles(id),
            is_active BOOLEAN, created_at TEXT, last_login TEXT)""",
        """CREATE TABLE IF NOT EXISTS audit_log (
            id SERIAL PRIMARY KEY, ts TEXT, user_id INTEGER, module TEXT,
            action TEXT, doc_type TEXT, doc_id INTEGER, description TEXT)""",
        """CREATE TABLE IF NOT EXISTS num_series (
            id SERIAL PRIMARY KEY, module TEXT, doc_type TEXT,
            prefix TEXT, next_num INTEGER, UNIQUE(module, doc_type))""",
        """CREATE TABLE IF NOT EXISTS uom (
            id SERIAL PRIMARY KEY, code TEXT UNIQUE, name TEXT, base_uom TEXT)""",
        """CREATE TABLE IF NOT EXISTS taxes (
            id SERIAL PRIMARY KEY, code TEXT UNIQUE, name TEXT,
            rate REAL, tax_type TEXT, is_active BOOLEAN)""",
        """CREATE TABLE IF NOT EXISTS currencies (
            id SERIAL PRIMARY KEY, code TEXT UNIQUE, name TEXT,
            symbol TEXT, exchange_rate REAL)""",
        """CREATE TABLE IF NOT EXISTS payment_methods (
            id SERIAL PRIMARY KEY, name TEXT UNIQUE, method_type TEXT,
            bank_account_id INTEGER, is_active BOOLEAN)""",
        """CREATE TABLE IF NOT EXISTS approval_workflows (
            id SERIAL PRIMARY KEY, module TEXT, doc_type TEXT, condition_field TEXT,
            condition_op TEXT, condition_value TEXT, approver_role_id INTEGER, is_active BOOLEAN)""",
        """CREATE TABLE IF NOT EXISTS partners (
            id SERIAL PRIMARY KEY, code TEXT UNIQUE, name TEXT, partner_type TEXT,
            phone TEXT, email TEXT, address TEXT, tax_id TEXT, payment_terms TEXT,
            credit_limit REAL, opening_balance REAL, currency TEXT, status TEXT,
            notes TEXT, created_at TEXT, balance REAL)""",
        """CREATE TABLE IF NOT EXISTS branches (
            id SERIAL PRIMARY KEY, code TEXT UNIQUE, name TEXT, address TEXT,
            phone TEXT, email TEXT, tax_id TEXT, default_warehouse_id INTEGER,
            is_main BOOLEAN DEFAULT FALSE, is_active BOOLEAN DEFAULT TRUE, created_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS user_branches (
            id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id),
            branch_id INTEGER REFERENCES branches(id),
            is_default BOOLEAN DEFAULT FALSE, UNIQUE(user_id, branch_id))""",
        """CREATE TABLE IF NOT EXISTS warehouses (
            id SERIAL PRIMARY KEY, code TEXT UNIQUE, name TEXT, location TEXT,
            is_active BOOLEAN, branch_id INTEGER REFERENCES branches(id))""",
        """CREATE TABLE IF NOT EXISTS item_groups (
            id SERIAL PRIMARY KEY, name TEXT UNIQUE, parent_id INTEGER REFERENCES item_groups(id))""",
        """CREATE TABLE IF NOT EXISTS items (
            id SERIAL PRIMARY KEY, code TEXT UNIQUE, name TEXT, description TEXT,
            group_id INTEGER REFERENCES item_groups(id), item_type TEXT,
            uom_id INTEGER REFERENCES uom(id), cost REAL, price REAL,
            reorder_level REAL, tax_id INTEGER REFERENCES taxes(id),
            is_active BOOLEAN, is_stock_item BOOLEAN, created_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS stock_balances (
            id SERIAL PRIMARY KEY, item_id INTEGER REFERENCES items(id),
            warehouse_id INTEGER REFERENCES warehouses(id),
            quantity REAL, UNIQUE(item_id, warehouse_id))""",
        """CREATE TABLE IF NOT EXISTS stock_ledger (
            id SERIAL PRIMARY KEY, ts TEXT, item_id INTEGER REFERENCES items(id),
            warehouse_id INTEGER REFERENCES warehouses(id), doc_type TEXT, doc_id INTEGER,
            doc_ref TEXT, qty_in REAL, qty_out REAL, balance REAL, unit_cost REAL,
            posted_by INTEGER REFERENCES users(id))""",
        """CREATE TABLE IF NOT EXISTS goods_receipts (
            id SERIAL PRIMARY KEY, doc_ref TEXT UNIQUE, doc_date TEXT,
            warehouse_id INTEGER REFERENCES warehouses(id),
            partner_id INTEGER REFERENCES partners(id), source_doc_type TEXT,
            source_doc_id INTEGER, status TEXT, remarks TEXT, total_value REAL,
            posted_by INTEGER REFERENCES users(id), posted_at TEXT, created_at TEXT,
            branch_id INTEGER REFERENCES branches(id), source_doc_ref TEXT)""",
        """CREATE TABLE IF NOT EXISTS goods_receipt_lines (
            id SERIAL PRIMARY KEY, receipt_id INTEGER REFERENCES goods_receipts(id),
            item_id INTEGER REFERENCES items(id), quantity REAL, unit_cost REAL,
            total REAL, po_line_id INTEGER, copied_qty REAL DEFAULT 0,
            source_doc_type TEXT, source_doc_id INTEGER, source_line_id INTEGER)""",
        """CREATE TABLE IF NOT EXISTS goods_issues (
            id SERIAL PRIMARY KEY, doc_ref TEXT UNIQUE, doc_date TEXT,
            warehouse_id INTEGER REFERENCES warehouses(id), issue_type TEXT,
            source_doc_type TEXT, source_doc_id INTEGER, status TEXT, remarks TEXT,
            total_value REAL, posted_by INTEGER REFERENCES users(id), posted_at TEXT,
            created_at TEXT, branch_id INTEGER REFERENCES branches(id), source_doc_ref TEXT)""",
        """CREATE TABLE IF NOT EXISTS goods_issue_lines (
            id SERIAL PRIMARY KEY, issue_id INTEGER REFERENCES goods_issues(id),
            item_id INTEGER REFERENCES items(id), quantity REAL, unit_cost REAL,
            total REAL, copied_qty REAL DEFAULT 0, source_doc_type TEXT,
            source_doc_id INTEGER, source_line_id INTEGER)""",
        """CREATE TABLE IF NOT EXISTS stock_transfers (
            id SERIAL PRIMARY KEY, doc_ref TEXT UNIQUE, doc_date TEXT,
            from_warehouse_id INTEGER REFERENCES warehouses(id),
            to_warehouse_id INTEGER REFERENCES warehouses(id),
            status TEXT, remarks TEXT, posted_by INTEGER REFERENCES users(id),
            posted_at TEXT, created_at TEXT,
            from_branch_id INTEGER REFERENCES branches(id),
            to_branch_id INTEGER REFERENCES branches(id),
            source_doc_id INTEGER, source_doc_type TEXT, source_doc_ref TEXT)""",
        """CREATE TABLE IF NOT EXISTS stock_transfer_lines (
            id SERIAL PRIMARY KEY, transfer_id INTEGER REFERENCES stock_transfers(id),
            item_id INTEGER REFERENCES items(id), quantity REAL, unit_cost REAL,
            copied_qty REAL DEFAULT 0, source_doc_type TEXT,
            source_doc_id INTEGER, source_line_id INTEGER)""",
        """CREATE TABLE IF NOT EXISTS inventory_counts (
            id SERIAL PRIMARY KEY, doc_ref TEXT UNIQUE, doc_date TEXT,
            warehouse_id INTEGER REFERENCES warehouses(id), status TEXT,
            remarks TEXT, posted_by INTEGER REFERENCES users(id),
            posted_at TEXT, created_at TEXT, branch_id INTEGER REFERENCES branches(id))""",
        """CREATE TABLE IF NOT EXISTS inventory_count_lines (
            id SERIAL PRIMARY KEY, count_id INTEGER REFERENCES inventory_counts(id),
            item_id INTEGER REFERENCES items(id), system_qty REAL, counted_qty REAL,
            variance REAL, unit_cost REAL)""",
        """CREATE TABLE IF NOT EXISTS sales_docs (
            id SERIAL PRIMARY KEY, doc_ref TEXT UNIQUE, doc_type TEXT, doc_date TEXT,
            due_date TEXT, customer_id INTEGER REFERENCES partners(id),
            warehouse_id INTEGER REFERENCES warehouses(id), status TEXT,
            source_doc_id INTEGER, source_doc_type TEXT, subtotal REAL, tax_total REAL,
            total REAL, amount_paid REAL, balance_due REAL, remarks TEXT,
            posted_by INTEGER REFERENCES users(id), posted_at TEXT, created_at TEXT,
            branch_id INTEGER REFERENCES branches(id), source_doc_ref TEXT)""",
        """CREATE TABLE IF NOT EXISTS sales_lines (
            id SERIAL PRIMARY KEY, doc_id INTEGER REFERENCES sales_docs(id),
            item_id INTEGER REFERENCES items(id), description TEXT,
            quantity REAL, unit_price REAL, discount_pct REAL,
            tax_id INTEGER REFERENCES taxes(id), tax_amount REAL, line_total REAL,
            cost_price REAL, copied_qty REAL DEFAULT 0, source_doc_type TEXT,
            source_doc_id INTEGER, source_line_id INTEGER)""",
        """CREATE TABLE IF NOT EXISTS purchase_docs (
            id SERIAL PRIMARY KEY, doc_ref TEXT UNIQUE, doc_type TEXT, doc_date TEXT,
            due_date TEXT, supplier_id INTEGER REFERENCES partners(id),
            warehouse_id INTEGER REFERENCES warehouses(id), status TEXT,
            source_doc_id INTEGER, source_doc_type TEXT, subtotal REAL, tax_total REAL,
            total REAL, amount_paid REAL, balance_due REAL, remarks TEXT,
            posted_by INTEGER REFERENCES users(id), posted_at TEXT, created_at TEXT,
            branch_id INTEGER REFERENCES branches(id), source_doc_ref TEXT)""",
        """CREATE TABLE IF NOT EXISTS purchase_lines (
            id SERIAL PRIMARY KEY, doc_id INTEGER REFERENCES purchase_docs(id),
            item_id INTEGER REFERENCES items(id), description TEXT,
            quantity REAL, unit_cost REAL, discount_pct REAL,
            tax_id INTEGER REFERENCES taxes(id), tax_amount REAL, line_total REAL,
            copied_qty REAL DEFAULT 0, source_doc_type TEXT,
            source_doc_id INTEGER, source_line_id INTEGER)""",
        """CREATE TABLE IF NOT EXISTS bom (
            id SERIAL PRIMARY KEY, finished_item_id INTEGER REFERENCES items(id),
            name TEXT, quantity_produced REAL, is_active BOOLEAN, notes TEXT,
            created_at TEXT, UNIQUE(finished_item_id, name))""",
        """CREATE TABLE IF NOT EXISTS bom_lines (
            id SERIAL PRIMARY KEY, bom_id INTEGER REFERENCES bom(id),
            item_id INTEGER REFERENCES items(id), quantity REAL,
            uom_id INTEGER REFERENCES uom(id), notes TEXT)""",
        """CREATE TABLE IF NOT EXISTS production_orders (
            id SERIAL PRIMARY KEY, doc_ref TEXT UNIQUE, doc_date TEXT, planned_date TEXT,
            bom_id INTEGER REFERENCES bom(id), warehouse_id INTEGER REFERENCES warehouses(id),
            planned_qty REAL, produced_qty REAL, wastage_qty REAL, status TEXT, batch_no TEXT,
            total_cost REAL, remarks TEXT, posted_by INTEGER REFERENCES users(id),
            created_at TEXT, branch_id INTEGER REFERENCES branches(id))""",
        """CREATE TABLE IF NOT EXISTS production_material_issues (
            id SERIAL PRIMARY KEY, production_order_id INTEGER REFERENCES production_orders(id),
            item_id INTEGER REFERENCES items(id), planned_qty REAL, issued_qty REAL,
            unit_cost REAL, issue_date TEXT)""",
        """CREATE TABLE IF NOT EXISTS production_wastage (
            id SERIAL PRIMARY KEY, production_order_id INTEGER REFERENCES production_orders(id),
            item_id INTEGER REFERENCES items(id), wastage_qty REAL, reason TEXT, doc_date TEXT)""",
        """CREATE TABLE IF NOT EXISTS coa (
            id SERIAL PRIMARY KEY, code TEXT UNIQUE, name TEXT, account_type TEXT,
            parent_id INTEGER REFERENCES coa(id), is_posting BOOLEAN, is_active BOOLEAN,
            opening_balance REAL, notes TEXT)""",
        """CREATE TABLE IF NOT EXISTS journal_entries (
            id SERIAL PRIMARY KEY, doc_ref TEXT UNIQUE, doc_date TEXT, module TEXT,
            source_doc_type TEXT, source_doc_id INTEGER, description TEXT, status TEXT,
            posted_by INTEGER REFERENCES users(id), created_at TEXT,
            branch_id INTEGER REFERENCES branches(id))""",
        """CREATE TABLE IF NOT EXISTS journal_lines (
            id SERIAL PRIMARY KEY, journal_id INTEGER REFERENCES journal_entries(id),
            account_id INTEGER REFERENCES coa(id), partner_id INTEGER REFERENCES partners(id),
            debit REAL, credit REAL, description TEXT)""",
        """CREATE TABLE IF NOT EXISTS financial_periods (
            id SERIAL PRIMARY KEY, name TEXT UNIQUE, start_date TEXT, end_date TEXT, status TEXT)""",
        """CREATE TABLE IF NOT EXISTS departments (
            id SERIAL PRIMARY KEY, name TEXT UNIQUE, manager_id INTEGER, is_active BOOLEAN)""",
        """CREATE TABLE IF NOT EXISTS job_titles (
            id SERIAL PRIMARY KEY, name TEXT UNIQUE,
            department_id INTEGER REFERENCES departments(id))""",
        """CREATE TABLE IF NOT EXISTS employees (
            id SERIAL PRIMARY KEY, emp_no TEXT UNIQUE, full_name TEXT, email TEXT,
            phone TEXT, address TEXT, department_id INTEGER REFERENCES departments(id),
            job_title_id INTEGER REFERENCES job_titles(id),
            partner_id INTEGER REFERENCES partners(id), date_hired TEXT,
            date_terminated TEXT, employment_type TEXT, monthly_salary REAL, status TEXT,
            emergency_contact TEXT, emergency_phone TEXT, national_id TEXT,
            kra_pin TEXT, notes TEXT, created_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS leave_records (
            id SERIAL PRIMARY KEY, employee_id INTEGER REFERENCES employees(id),
            leave_type TEXT, start_date TEXT, end_date TEXT, days REAL, status TEXT,
            approved_by INTEGER REFERENCES users(id), remarks TEXT, created_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS bank_accounts (
            id SERIAL PRIMARY KEY, code TEXT UNIQUE, name TEXT, account_type TEXT,
            bank_name TEXT, account_number TEXT, currency TEXT,
            coa_id INTEGER REFERENCES coa(id), opening_balance REAL, current_balance REAL,
            is_active BOOLEAN, created_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS bank_transactions (
            id SERIAL PRIMARY KEY, doc_ref TEXT UNIQUE, doc_date TEXT,
            bank_account_id INTEGER REFERENCES bank_accounts(id), txn_type TEXT,
            amount REAL, description TEXT, reference TEXT,
            partner_id INTEGER REFERENCES partners(id), is_reconciled BOOLEAN,
            reconciled_at TEXT, journal_id INTEGER REFERENCES journal_entries(id),
            created_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY, doc_ref TEXT UNIQUE, doc_date TEXT, payment_type TEXT,
            partner_id INTEGER REFERENCES partners(id),
            bank_account_id INTEGER REFERENCES bank_accounts(id),
            payment_method_id INTEGER REFERENCES payment_methods(id),
            amount REAL, amount_allocated REAL, unallocated REAL, reference TEXT,
            remarks TEXT, status TEXT, journal_id INTEGER REFERENCES journal_entries(id),
            posted_by INTEGER REFERENCES users(id), posted_at TEXT, created_at TEXT,
            branch_id INTEGER REFERENCES branches(id))""",
        """CREATE TABLE IF NOT EXISTS payment_allocations (
            id SERIAL PRIMARY KEY, payment_id INTEGER REFERENCES payments(id),
            doc_type TEXT, doc_id INTEGER, amount REAL, allocation_date TEXT)""",
    ]

    for q in tables:
        c.execute(q)

    conn.commit()
    _seed_data(c, conn, sqlite_mode=False)


def _seed_data(c, conn, sqlite_mode=True):
    """Seed default data — runs on both SQLite and Postgres."""
    # Check if already seeded
    c.execute("SELECT COUNT(*) as count FROM company")
    row = c.fetchone()
    if row and (row['count'] if hasattr(row, '__getitem__') else list(row.values())[0]) > 0:
        # Still run branch migration on existing DBs
        _ensure_main_branch(c, conn, sqlite_mode)
        _seed_role_permissions(c, conn)
        return

    ph = '?' if sqlite_mode else '%s'

    def ex(sql, params=()):
        sql = sql.replace('?', ph)
        c.execute(sql, params)

    def ex_ri(table, cols, vals):
        """Insert and return id."""
        col_str = ', '.join(cols)
        placeholders = ', '.join([ph] * len(vals))
        if sqlite_mode:
            ex(f'INSERT INTO {table} ({col_str}) VALUES ({placeholders})', vals)
            c.execute('SELECT last_insert_rowid() as id')
        else:
            ex(f'INSERT INTO {table} ({col_str}) VALUES ({placeholders}) RETURNING id', vals)
        row = c.fetchone()
        return row['id'] if row else None

    ex("INSERT INTO company (name, address, phone, email, tax_id, currency, financial_year_start) VALUES (?,?,?,?,?,?,?)",
       ('Cellar & Crumb Operations', 'Nairobi, Kenya', '+254 700 000 000',
        'info@cellarncrumb.co.ke', 'P051234567A', 'KES', '2026-01-01'))

    roles = ['Admin', 'Manager', 'Sales', 'Purchasing', 'Store', 'Accountant', 'HR', 'Viewer']
    role_ids = {}
    for r in roles:
        rid = ex_ri('roles', ['name', 'description'], (r, f'{r} role'))
        role_ids[r] = rid

    admin_hash = bcrypt.hashpw(b'admin123', bcrypt.gensalt()).decode('utf-8')
    manager_hash = bcrypt.hashpw(b'manager123', bcrypt.gensalt()).decode('utf-8')
    ex("INSERT INTO users (username, full_name, email, password_hash, role_id, is_active, created_at) VALUES (?,?,?,?,?,TRUE,?)",
       ('admin', 'System Administrator', 'admin@cellarncrumb.co.ke', admin_hash, role_ids['Admin'], datetime.now().isoformat()))
    ex("INSERT INTO users (username, full_name, email, password_hash, role_id, is_active, created_at) VALUES (?,?,?,?,?,TRUE,?)",
       ('manager', 'General Manager', 'manager@cellarncrumb.co.ke', manager_hash, role_ids['Manager'], datetime.now().isoformat()))

    num_series = [
        ('Sales', 'Quotation', 'SQ'), ('Sales', 'Order', 'SO'), ('Sales', 'Delivery', 'DN'),
        ('Sales', 'Invoice', 'SI'), ('Sales', 'Return', 'SCN'),
        ('Purchases', 'Request', 'PR'), ('Purchases', 'Order', 'PO'), ('Purchases', 'Receipt', 'GR'),
        ('Purchases', 'Invoice', 'PI'), ('Purchases', 'Return', 'PDN'),
        ('Inventory', 'GoodsReceipt', 'IGR'), ('Inventory', 'GoodsIssue', 'IGI'),
        ('Inventory', 'Transfer', 'ITR'), ('Inventory', 'Count', 'IC'),
        ('Inventory', 'TransferRequest', 'ITR'),
        ('Production', 'Order', 'WO'), ('Finance', 'Journal', 'JE'),
        ('Banking', 'Deposit', 'DEP'), ('Banking', 'Withdrawal', 'WDR'), ('Banking', 'Transfer', 'BTR'),
        ('Payments', 'Incoming', 'RCV'), ('Payments', 'Outgoing', 'PMT'), ('Payments', 'Expense', 'EXP'),
    ]
    for ns in num_series:
        col_str = 'module, doc_type, prefix, next_num'
        vals = ns + (1,)
        if sqlite_mode:
            ex(f'INSERT OR IGNORE INTO num_series ({col_str}) VALUES (?,?,?,?)', vals)
        else:
            ex(f'INSERT INTO num_series ({col_str}) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING', vals)

    uoms = ['PCS', 'KG', 'G', 'LTR', 'ML', 'BTL', 'CRT', 'BOX', 'BAG', 'MTR', 'PKT']
    uom_ids = {}
    for u in uoms:
        uid = ex_ri('uom', ['code', 'name', 'base_uom'], (u, u, u))
        uom_ids[u] = uid

    ex("INSERT INTO taxes (code, name, rate, tax_type, is_active) VALUES (?,?,16,'Sales',TRUE)", ('VAT16', 'VAT 16%'))
    ex("INSERT INTO taxes (code, name, rate, tax_type, is_active) VALUES (?,?,0,'Sales',TRUE)", ('NONE', 'No Tax'))

    currencies = [('KES', 'Kenyan Shilling', 'KES', 1), ('USD', 'US Dollar', '$', 130), ('EUR', 'Euro', '€', 140)]
    for cur in currencies:
        ex("INSERT INTO currencies (code, name, symbol, exchange_rate) VALUES (?,?,?,?)", cur)

    payment_methods = ['Cash', 'Bank Transfer', 'M-Pesa', 'Card', 'Cheque']
    for pm in payment_methods:
        ex("INSERT INTO payment_methods (name, method_type, is_active) VALUES (?,?,TRUE)", (pm, pm.split()[0]))

    # Branch first (warehouses reference it)
    branch_id = ex_ri('branches', ['code', 'name', 'address', 'phone', 'email', 'tax_id', 'is_main', 'is_active', 'created_at'],
                      ('HQ', 'Main Bakery & HQ', 'Nairobi, Kenya', '+254 700 000 000',
                       'info@cellarncrumb.co.ke', 'P051234567A', True, True, datetime.now().isoformat()))

    warehouses = [('MAIN', 'Main Warehouse', 'HQ'), ('BAKERY', 'Bakery Store', 'Kitchen'), ('BAR', 'Bar Store', 'Bar')]
    wh_ids = {}
    for code, name, loc in warehouses:
        wid = ex_ri('warehouses', ['code', 'name', 'location', 'is_active', 'branch_id'], (code, name, loc, True, branch_id))
        wh_ids[code] = wid

    # Update branch with default warehouse
    ex("UPDATE branches SET default_warehouse_id = ? WHERE id = ?", (wh_ids['MAIN'], branch_id))

    item_groups = ['Raw Materials', 'Finished Goods', 'Wines & Spirits', 'Packaging', 'Services']
    ig_ids = {}
    for ig in item_groups:
        igid = ex_ri('item_groups', ['name'], (ig,))
        ig_ids[ig] = igid

    coas = [
        ('1000', 'Assets', 'Asset', None, 0),
        ('1100', 'Current Assets', 'Asset', '1000', 0),
        ('1110', 'Cash on Hand', 'Asset', '1100', 1),
        ('1120', 'Bank Account', 'Asset', '1100', 1),
        ('1130', 'Mobile Money', 'Asset', '1100', 1),
        ('1200', 'Accounts Receivable', 'Asset', '1100', 1),
        ('1300', 'Inventory', 'Asset', '1100', 0),
        ('1310', 'Raw Material Inventory', 'Asset', '1300', 1),
        ('1320', 'Finished Goods Inventory', 'Asset', '1300', 1),
        ('1400', 'Fixed Assets', 'Asset', '1000', 0),
        ('1410', 'Equipment', 'Asset', '1400', 1),
        ('2000', 'Liabilities', 'Liability', None, 0),
        ('2100', 'Current Liabilities', 'Liability', '2000', 0),
        ('2110', 'Accounts Payable', 'Liability', '2100', 1),
        ('2200', 'Tax Payable', 'Liability', '2100', 0),
        ('2210', 'VAT Payable', 'Liability', '2200', 1),
        ('3000', 'Equity', 'Equity', None, 0),
        ('3100', 'Owner Capital', 'Equity', '3000', 1),
        ('3200', 'Retained Earnings', 'Equity', '3000', 1),
        ('4000', 'Revenue', 'Revenue', None, 0),
        ('4100', 'Sales Revenue', 'Revenue', '4000', 1),
        ('4200', 'Other Income', 'Revenue', '4000', 1),
        ('5000', 'Expenses', 'Expense', None, 0),
        ('5100', 'Cost of Goods Sold', 'Expense', '5000', 1),
        ('5200', 'Operating Expenses', 'Expense', '5000', 0),
        ('5210', 'Salaries & Wages', 'Expense', '5200', 1),
        ('5220', 'Rent', 'Expense', '5200', 1),
        ('5230', 'Utilities', 'Expense', '5200', 1),
        ('5240', 'Production Cost', 'Expense', '5200', 1),
        ('5250', 'Packaging Cost', 'Expense', '5200', 1),
        ('5260', 'Wastage & Losses', 'Expense', '5200', 1),
        ('5300', 'Finance Charges', 'Expense', '5000', 1),
    ]
    coa_map = {}
    for code, name, acc_type, parent_code, is_posting in coas:
        parent_id = coa_map.get(parent_code)
        cid = ex_ri('coa', ['code', 'name', 'account_type', 'parent_id', 'is_posting', 'is_active', 'opening_balance'],
                    (code, name, acc_type, parent_id, bool(is_posting), True, 0))
        coa_map[code] = cid

    ex("INSERT INTO bank_accounts (code, name, account_type, bank_name, currency, coa_id, current_balance, is_active) VALUES (?,?,?,?,?,?,0,TRUE)",
       ('CASH-MAIN', 'Cash Register', 'Cash', 'Cash', 'KES', coa_map['1110']))
    ex("INSERT INTO bank_accounts (code, name, account_type, bank_name, account_number, currency, coa_id, current_balance, is_active) VALUES (?,?,?,?,?,?,?,0,TRUE)",
       ('KCB-001', 'KCB Current Account', 'Bank', 'KCB Bank', '001', 'KES', coa_map['1120']))
    ex("INSERT INTO bank_accounts (code, name, account_type, bank_name, currency, coa_id, current_balance, is_active) VALUES (?,?,?,?,?,?,0,TRUE)",
       ('MPESA-001', 'M-Pesa Till', 'Mobile', 'M-Pesa', 'KES', coa_map['1130']))

    departments = ['Administration', 'Production', 'Sales', 'Purchasing', 'Store', 'Finance', 'Human Resources']
    dept_ids = {}
    for dept in departments:
        did = ex_ri('departments', ['name', 'is_active'], (dept, True))
        dept_ids[dept] = did

    job_titles = [
        ('General Manager', 'Administration'), ('Head Baker', 'Production'), ('Pastry Chef', 'Production'),
        ('Sales Manager', 'Sales'), ('Sales Rep', 'Sales'), ('Purchasing Officer', 'Purchasing'),
        ('Store Keeper', 'Store'), ('Accountant', 'Finance'), ('HR Officer', 'Human Resources'),
    ]
    jt_ids = {}
    for title, dept in job_titles:
        jtid = ex_ri('job_titles', ['name', 'department_id'], (title, dept_ids[dept]))
        jt_ids[title] = jtid

    partners = [
        ('CUS0001', 'Walk-in Customer', 'Customer', 0),
        ('CUS0002', 'Events Co', 'Customer', 50000),
        ('SUP0001', 'Milling Best Supplies', 'Supplier', 0),
        ('SUP0002', 'Wines Distributor', 'Supplier', 0),
        ('BPS0001', 'Garden View Hotel', 'Customer & Supplier', 100000),
    ]
    for p_code, p_name, p_type, credit in partners:
        ex("INSERT INTO partners (code, name, partner_type, credit_limit, currency, status, balance) VALUES (?,?,?,?,?,?,0)",
           (p_code, p_name, p_type, credit, 'KES', 'Active'))

    items = [
        ('RM-FLOUR', 'Wheat Flour', 'Raw Materials', 'RM', 'KG', 120, 0, 250),
        ('RM-SUGAR', 'Sugar', 'Raw Materials', 'RM', 'KG', 160, 0, 80),
        ('RM-YEAST', 'Yeast', 'Raw Materials', 'RM', 'KG', 800, 0, 15),
        ('RM-BUTTER', 'Butter', 'Raw Materials', 'RM', 'KG', 600, 0, 30),
        ('FG-BREAD', 'White Bread Loaf', 'Finished Goods', 'FG', 'PCS', 55, 90, 35),
        ('FG-CAKE', 'Vanilla Cake', 'Finished Goods', 'FG', 'PCS', 650, 950, 8),
        ('FG-ROLL', 'Dinner Roll', 'Finished Goods', 'FG', 'PCS', 80, 140, 20),
        ('WS-WHISKY', 'House Whisky 750ml', 'Wines & Spirits', 'WS', 'BTL', 1200, 1650, 24),
        ('WS-WINE-RED', 'Cabernet 750ml', 'Wines & Spirits', 'WS', 'BTL', 850, 1250, 30),
        ('WS-WINE-WHT', 'Chardonnay 750ml', 'Wines & Spirits', 'WS', 'BTL', 800, 1200, 24),
        ('WS-GIN', 'Dry Gin 750ml', 'Wines & Spirits', 'WS', 'BTL', 950, 1450, 18),
        ('WS-BEER', 'Premium Lager 500ml', 'Wines & Spirits', 'WS', 'BTL', 180, 280, 120),
    ]
    item_ids = {}
    for i_code, i_name, i_group, i_type, i_uom, i_cost, i_price, i_stock in items:
        iid = ex_ri('items', ['code', 'name', 'group_id', 'item_type', 'uom_id', 'cost', 'price', 'is_active', 'is_stock_item'],
                    (i_code, i_name, ig_ids[i_group], i_type, uom_ids[i_uom], i_cost, i_price, True, True))
        item_ids[i_code] = iid
        ex("INSERT INTO stock_balances (item_id, warehouse_id, quantity) VALUES (?,?,?)",
           (iid, wh_ids['MAIN'], i_stock))

    # BOMs
    bom1 = ex_ri('bom', ['finished_item_id', 'name', 'quantity_produced', 'is_active'],
                 (item_ids['FG-BREAD'], 'Standard Bread Recipe', 10, True))
    for bom_item, qty in [('RM-FLOUR', 2.5), ('RM-SUGAR', 0.2), ('RM-YEAST', 0.05)]:
        ex("INSERT INTO bom_lines (bom_id, item_id, quantity, uom_id) VALUES (?,?,?,?)",
           (bom1, item_ids[bom_item], qty, uom_ids['KG']))

    bom2 = ex_ri('bom', ['finished_item_id', 'name', 'quantity_produced', 'is_active'],
                 (item_ids['FG-CAKE'], 'Vanilla Cake Recipe', 1, True))
    for bom_item, qty in [('RM-FLOUR', 0.5), ('RM-SUGAR', 0.3), ('RM-BUTTER', 0.2)]:
        ex("INSERT INTO bom_lines (bom_id, item_id, quantity, uom_id) VALUES (?,?,?,?)",
           (bom2, item_ids[bom_item], qty, uom_ids['KG']))

    ex("INSERT INTO financial_periods (name, start_date, end_date, status) VALUES (?,?,?,?)",
       ('Jan-Dec 2026', '2026-01-01', '2026-12-31', 'Open'))

    ex("INSERT INTO employees (emp_no, full_name, email, department_id, job_title_id, status) VALUES (?,?,?,?,?,?)",
       ('EMP-001', 'John Doe', 'johndoe@test.com', dept_ids['Administration'], jt_ids['General Manager'], 'Active'))

    # Assign admin & manager users to main branch
    c.execute("SELECT id FROM users")
    user_rows = c.fetchall()
    for u in user_rows:
        uid = u['id']
        if sqlite_mode:
            ex("INSERT OR IGNORE INTO user_branches (user_id, branch_id, is_default) VALUES (?,?,1)", (uid, branch_id))
        else:
            ex("INSERT INTO user_branches (user_id, branch_id, is_default) VALUES (%s,%s,true) ON CONFLICT DO NOTHING", (uid, branch_id))

    conn.commit()
    _seed_role_permissions(c, conn)


def _ensure_main_branch(c, conn, sqlite_mode=True):
    """Idempotent: create main branch if missing and migrate nulls."""
    ph = '?' if sqlite_mode else '%s'

    c.execute("SELECT COUNT(*) as count FROM branches")
    row = c.fetchone()
    if not row or (row['count'] if isinstance(row['count'], int) else list(row.values())[0]) == 0:
        if sqlite_mode:
            c.execute("""INSERT INTO branches (code, name, address, phone, email, tax_id, is_main, is_active, created_at)
                         VALUES (?,?,?,?,?,?,1,1,datetime('now'))""",
                      ('HQ', 'Main Bakery & HQ', 'Nairobi, Kenya', '+254 700 000 000',
                       'info@cellarncrumb.co.ke', 'P051234567A'))
        else:
            c.execute("""INSERT INTO branches (code, name, address, phone, email, tax_id, is_main, is_active, created_at)
                         VALUES (%s,%s,%s,%s,%s,%s,true,true,now()) ON CONFLICT DO NOTHING""",
                      ('HQ', 'Main Bakery & HQ', 'Nairobi, Kenya', '+254 700 000 000',
                       'info@cellarncrumb.co.ke', 'P051234567A'))

    c.execute("SELECT id FROM branches WHERE is_main = true")
    main_b = c.fetchone()
    if not main_b:
        c.execute("SELECT id FROM branches LIMIT 1")
        main_b = c.fetchone()

    if main_b:
        hq_id = main_b['id']
        for table, col in [
            ('warehouses', 'branch_id'), ('sales_docs', 'branch_id'),
            ('purchase_docs', 'branch_id'), ('goods_receipts', 'branch_id'),
            ('goods_issues', 'branch_id'), ('payments', 'branch_id'),
            ('journal_entries', 'branch_id'), ('production_orders', 'branch_id'),
        ]:
            try:
                c.execute(f"UPDATE {table} SET {col} = {ph} WHERE {col} IS NULL", (hq_id,))
            except Exception:
                pass
        for col in ['from_branch_id', 'to_branch_id']:
            try:
                c.execute(f"UPDATE stock_transfers SET {col} = {ph} WHERE {col} IS NULL", (hq_id,))
            except Exception:
                pass

        # Ensure users are in branch
        c.execute("SELECT id FROM users")
        for u in c.fetchall():
            uid = u['id']
            if sqlite_mode:
                c.execute("INSERT OR IGNORE INTO user_branches (user_id, branch_id, is_default) VALUES (?,?,1)", (uid, hq_id))
            else:
                c.execute("INSERT INTO user_branches (user_id, branch_id, is_default) VALUES (%s,%s,true) ON CONFLICT DO NOTHING", (uid, hq_id))

    conn.commit()


def _seed_role_permissions(c, conn):
    """Ensure role permissions JSON is populated."""
    DEFAULT_ROLE_PERMISSIONS = {
        'Admin':      {'overview': 2, 'inventory': 2, 'bp_master': 2, 'sales': 2, 'purchases': 2, 'production': 2, 'finance': 2, 'hr': 2, 'banking': 2, 'payments': 2, 'settings': 2},
        'Manager':    {'overview': 2, 'inventory': 2, 'bp_master': 2, 'sales': 2, 'purchases': 2, 'production': 2, 'finance': 2, 'hr': 2, 'banking': 2, 'payments': 2, 'settings': 1},
        'Sales':      {'overview': 1, 'inventory': 1, 'bp_master': 2, 'sales': 2, 'purchases': 0, 'production': 0, 'finance': 0, 'hr': 0, 'banking': 0, 'payments': 1, 'settings': 0},
        'Purchasing': {'overview': 1, 'inventory': 2, 'bp_master': 2, 'sales': 0, 'purchases': 2, 'production': 0, 'finance': 0, 'hr': 0, 'banking': 0, 'payments': 1, 'settings': 0},
        'Store':      {'overview': 1, 'inventory': 2, 'bp_master': 1, 'sales': 0, 'purchases': 1, 'production': 1, 'finance': 0, 'hr': 0, 'banking': 0, 'payments': 0, 'settings': 0},
        'Accountant': {'overview': 2, 'inventory': 1, 'bp_master': 2, 'sales': 1, 'purchases': 1, 'production': 1, 'finance': 2, 'hr': 1, 'banking': 2, 'payments': 2, 'settings': 0},
        'HR':         {'overview': 1, 'inventory': 0, 'bp_master': 0, 'sales': 0, 'purchases': 0, 'production': 0, 'finance': 0, 'hr': 2, 'banking': 0, 'payments': 0, 'settings': 0},
        'Viewer':     {'overview': 1, 'inventory': 1, 'bp_master': 1, 'sales': 1, 'purchases': 1, 'production': 1, 'finance': 1, 'hr': 0, 'banking': 1, 'payments': 1, 'settings': 0},
    }
    ph = '%s' if USE_POSTGRES else '?'
    for r_name, r_perms in DEFAULT_ROLE_PERMISSIONS.items():
        perms_json = json.dumps(r_perms)
        c.execute(
            f"UPDATE roles SET permissions = {ph} WHERE name = {ph} AND (permissions IS NULL OR permissions = '' OR permissions = '{{}}')",
            (perms_json, r_name)
        )
    conn.commit()
