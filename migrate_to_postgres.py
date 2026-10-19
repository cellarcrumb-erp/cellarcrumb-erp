"""
migrate_to_postgres.py
======================
Run this ONCE to migrate your local SQLite database to a Render PostgreSQL database.

Usage:
    set DATABASE_URL=postgresql://user:pass@host/dbname
    python migrate_to_postgres.py

Or on PowerShell:
    $env:DATABASE_URL = "postgresql://user:pass@host/dbname"
    python migrate_to_postgres.py
"""

import os
import sys
import sqlite3
from pathlib import Path

DATABASE_URL = os.environ.get('DATABASE_URL', '')
if not DATABASE_URL:
    print("ERROR: Set DATABASE_URL environment variable first.")
    print("Example:")
    print('  $env:DATABASE_URL = "postgresql://user:pass@host/dbname"')
    sys.exit(1)

if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

# Local SQLite path
sqlite_path = Path(os.getenv('LOCALAPPDATA', '')) / 'CellarCrumbERP' / 'erp.db'
if not sqlite_path.exists():
    print(f"ERROR: SQLite DB not found at {sqlite_path}")
    sys.exit(1)

print(f"Source: {sqlite_path}")
print(f"Target: {DATABASE_URL[:40]}...")
print()

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("Installing psycopg2-binary...")
    os.system(f"{sys.executable} -m pip install psycopg2-binary")
    import psycopg2
    import psycopg2.extras

# ── Connect ──────────────────────────────────────────────────────────────────
sqlite_conn = sqlite3.connect(str(sqlite_path))
sqlite_conn.row_factory = sqlite3.Row

pg_conn = psycopg2.connect(DATABASE_URL)
pg_conn.autocommit = False
pg_cur = pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

# ── Initialise schema on Postgres ────────────────────────────────────────────
print("Creating schema on PostgreSQL...")
os.environ['DATABASE_URL'] = DATABASE_URL
import database
database.init_db()
print("Schema ready.\n")

# ── Tables in dependency order (parents before children) ─────────────────────
TABLES = [
    'company', 'roles', 'users', 'audit_log', 'num_series',
    'uom', 'taxes', 'currencies', 'payment_methods', 'approval_workflows',
    'partners', 'branches',
    'warehouses', 'item_groups', 'items',
    'stock_balances', 'stock_ledger',
    'goods_receipts', 'goods_receipt_lines',
    'goods_issues', 'goods_issue_lines',
    'stock_transfers', 'stock_transfer_lines',
    'inventory_counts', 'inventory_count_lines',
    'sales_docs', 'sales_lines',
    'purchase_docs', 'purchase_lines',
    'bom', 'bom_lines',
    'production_orders', 'production_material_issues', 'production_wastage',
    'coa', 'journal_entries', 'journal_lines',
    'financial_periods', 'departments', 'job_titles', 'employees', 'leave_records',
    'bank_accounts', 'bank_transactions',
    'payments', 'payment_allocations',
    'user_branches',
]

for table in TABLES:
    rows = sqlite_conn.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        print(f"  {table}: empty, skipping")
        continue

    cols = list(rows[0].keys())
    col_str = ', '.join(cols)
    placeholders = ', '.join(['%s'] * len(cols))

    # Clear existing (schema seed might have added defaults)
    pg_cur.execute(f"DELETE FROM {table}")

    batch = [tuple(row[c] for c in cols) for row in rows]
    psycopg2.extras.execute_batch(
        pg_cur,
        f"INSERT INTO {table} ({col_str}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
        batch,
        page_size=500
    )
    print(f"  {table}: {len(batch)} rows migrated")

pg_conn.commit()

# ── Reset sequences so new inserts don't collide ──────────────────────────────
print("\nResetting PostgreSQL sequences...")
pg_cur.execute("""
    SELECT sequence_name FROM information_schema.sequences
    WHERE sequence_schema = 'public'
""")
sequences = [r['sequence_name'] for r in pg_cur.fetchall()]
for seq in sequences:
    table = seq.replace('_id_seq', '')
    try:
        pg_cur.execute(f"SELECT setval('{seq}', COALESCE((SELECT MAX(id) FROM {table}), 1))")
        print(f"  Reset {seq}")
    except Exception as e:
        print(f"  Skip {seq}: {e}")
        pg_conn.rollback()

pg_conn.commit()
pg_conn.close()
sqlite_conn.close()

print("\n✅ Migration complete! Your data is now on PostgreSQL.")
print("Set DATABASE_URL on Render and redeploy.")
