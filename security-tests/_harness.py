"""
Shared harness for the security tests.

SAFETY: this module refuses to run unless DATABASE_URL points at a loopback
Postgres (127.0.0.1 / localhost) and the database name starts with "coop_sectest".
It never makes network calls to anything but the in-process Flask test client.

Bring up the throwaway database first (see README.md), then:
    python -m unittest security-tests.test_race_conditions -v
"""
import os
import re
import sys
import threading
from urllib.parse import urlparse

PG_HOST = os.environ.get('SECTEST_PG_HOST', '127.0.0.1')
PG_PORT = os.environ.get('SECTEST_PG_PORT', '55432')
PG_USER = os.environ.get('SECTEST_PG_USER', 'postgres')
DB_NAME = os.environ.get('SECTEST_DB', 'coop_sectest')
ADMIN_PW = 'SecTest-Admin-1'
TREAS_PW = 'SecTest-Treas-1'
SECR_PW = 'SecTest-Secr-1'
MEMBER_PW = 'SecTest-Member-1'

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _assert_local(url):
    u = urlparse(url)
    if u.hostname not in ('127.0.0.1', 'localhost', '::1'):
        sys.exit(f'REFUSING TO RUN: DATABASE_URL host {u.hostname!r} is not loopback.')
    if not (u.path or '').lstrip('/').startswith('coop_sectest'):
        sys.exit('REFUSING TO RUN: database name must start with coop_sectest.')


def fresh_database():
    """Drop and recreate the throwaway test database (local instance only)."""
    import psycopg2
    admin_url = f'postgresql://{PG_USER}@{PG_HOST}:{PG_PORT}/postgres'
    _assert_local(f'postgresql://{PG_USER}@{PG_HOST}:{PG_PORT}/{DB_NAME}')
    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s', (DB_NAME,))
    cur.execute(f'DROP DATABASE IF EXISTS {DB_NAME}')
    cur.execute(f'CREATE DATABASE {DB_NAME}')
    conn.close()


def boot_app():
    """Create a clean DB, set the environment, import the real application."""
    url = f'postgresql://{PG_USER}@{PG_HOST}:{PG_PORT}/{DB_NAME}'
    _assert_local(url)
    fresh_database()
    os.environ['DATABASE_URL'] = url
    os.environ['SECRET_KEY'] = 'sectest-' + 'k' * 40
    os.environ['FIELD_ENCRYPTION_KEY'] = '05SmPJhNFMKwg9NysnBdQjKtqn3VwWDl1IiPIMAg2as='
    os.environ['ADMIN_PASSWORD'] = ADMIN_PW
    os.environ['TREASURER_PASSWORD'] = TREAS_PW
    os.environ['SECRETARY_PASSWORD'] = SECR_PW
    os.environ['FLASK_DEBUG'] = '1'       # plain-http cookies for the test client
    os.environ['TASK_RUNNER_TOKEN'] = 'sectest-task-token'
    os.environ['HQ_SYNC_TOKEN'] = 'sectest-hq-token'
    os.environ.pop('ENABLE_SUPPORT_ROUTES', None)
    os.environ.pop('RESEND_API_KEY', None)
    os.environ['MAIL_ENABLED'] = '0'
    sys.path.insert(0, REPO)
    os.chdir(REPO)
    import app as app_module
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, PROPAGATE_EXCEPTIONS=False)
    return app_module.app


def login(client, username, password):
    r = client.post('/login', data={'username': username, 'password': password})
    return r


def seed(app):
    """Two members with portal logins, one loan per member. Test data only."""
    from werkzeug.security import generate_password_hash
    from database import get_db
    with app.app_context():
        db = get_db()
        for i, (fn, ln) in enumerate([('Alice', 'Alpha'), ('Bob', 'Beta')], start=1):
            email = f'{fn.lower()}@sectest.invalid'
            db.execute('''INSERT INTO members (member_number, first_name, last_name, email, phone,
                          status, total_savings, monthly_savings)
                          VALUES (?, ?, ?, ?, ?, 'active', ?, 5000)''',
                       (f'SEC{i:03d}', fn, ln, email, f'0800000000{i}', 100000 * i))
            db.execute('''INSERT INTO users (username, password_hash, role, full_name, email, is_active)
                          VALUES (?, ?, 'member', ?, ?, 1)''',
                       (email, generate_password_hash(MEMBER_PW), f'{fn} {ln}', email))
        db.commit()
        members = {r['first_name']: dict(r) for r in db.execute('SELECT * FROM members').fetchall()}
        for fn, m in members.items():
            db.execute('''INSERT INTO loans (loan_number, member_id, amount, purpose, tenure,
                          interest_rate, total_repayment, status, approval_stage, balance,
                          payment_collateral_type, payment_collateral_status,
                          bank_statement_status, credit_check_status)
                          VALUES (?, ?, 50000, 'Regular', 12, 11, 55000, 'pending', 'president', 0,
                                  'standing_order', 'verified', 'received', 'completed')''',
                       (f'SEC-LOAN-{m["id"]}', m['id']))
        db.commit()
        loans = {r['member_id']: dict(r) for r in db.execute('SELECT * FROM loans').fetchall()}
        return members, loans


def parallel(n, fn):
    """Run fn(i) in n threads released simultaneously; return the results."""
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        try:
            results[i] = fn(i)
        except Exception as exc:   # noqa: BLE001
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results
