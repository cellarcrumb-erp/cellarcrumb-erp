import sys
sys.path.insert(0, '.')
from database import get_db, init_db, USE_POSTGRES, DB_PATH
print('USE_POSTGRES:', USE_POSTGRES)
print('DB_PATH:', DB_PATH)
init_db()
conn = get_db()
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = [r[0] for r in cur.fetchall()]
print('Tables:', len(tables))
print('Sample:', tables[:5])
conn.close()
print('OK - local SQLite mode works correctly')
