"""One-time, opt-in migration of a COPY of appointments.db to PostgreSQL.
Run only after app.py has initialized PostgreSQL schema; makes no changes to SQLite.
"""
import os,sys,sqlite3
from dotenv import load_dotenv
import psycopg
load_dotenv()
source=sys.argv[1] if len(sys.argv)>1 else 'appointments.db'
if not os.path.isfile(source):raise SystemExit('SQLite source not found: '+source)
if not os.getenv('DATABASE_URL'):raise SystemExit('Set DATABASE_URL in .env')
with sqlite3.connect('file:'+os.path.abspath(source)+'?mode=ro',uri=True) as old,psycopg.connect(os.environ['DATABASE_URL']) as new:
    old.row_factory=sqlite3.Row
    tables=['appointments','call_sessions','human_handoffs']
    for table in tables:
        if not old.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():continue
        rows=old.execute('SELECT * FROM '+table).fetchall()
        for r in rows:
            cols=list(r.keys()); values=[r[k] for k in cols]
            sql='INSERT INTO '+table+' ('+','.join(cols)+') VALUES ('+','.join(['%s']*len(cols))+') ON CONFLICT DO NOTHING'
            new.execute(sql,values)
        print(f'{table}: checked {len(rows)} rows')
    for table in ('appointments','human_handoffs'):
        new.execute("SELECT setval(pg_get_serial_sequence(%s,'id'),GREATEST(COALESCE((SELECT MAX(id) FROM "+table+"),0),1),true)",(table,))
print('Migration finished. Verify all rows and appointment flows before production use.')
