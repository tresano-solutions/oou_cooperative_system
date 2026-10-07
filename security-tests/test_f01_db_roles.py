"""F-01 regression: per-client Postgres role is least-privilege and the app runs on it.

Applies deploy/vps/db_roles.py to two scratch client databases on the LOCAL test
instance and checks what the role can and cannot do. (The app itself is exercised
under the restricted role by running the other modules with SECTEST_RESTRICTED=1.)
"""
import sys, os, unittest
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'deploy', 'vps'))
import psycopg2
import db_roles
import _harness as H

PW = 'f01_test_password_abcdefghijklmnop'


def admin(db='postgres'):
    c = psycopg2.connect(f'postgresql://{H.PG_USER}@{H.PG_HOST}:{H.PG_PORT}/{db}')
    c.autocommit = True
    return c


def as_role(slug, db):
    return psycopg2.connect(f'postgresql://{db_roles.role_name(slug)}:{PW}@{H.PG_HOST}:{H.PG_PORT}/{db}')


class DbRoles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        a = admin(); cur = a.cursor()
        for slug in ('sectesta', 'sectestb'):
            cur.execute(f'DROP DATABASE IF EXISTS coop_{slug}')
            cur.execute(f'CREATE DATABASE coop_{slug}')
            d = admin(f'coop_{slug}')
            d.cursor().execute("CREATE TABLE members (id serial primary key, name text); INSERT INTO members (name) VALUES ('legacy-owned-by-postgres')")
            d.close()
        # same sequence the script runs, for client A and B
        for slug in ('sectesta', 'sectestb'):
            cur.execute(db_roles.stage1(slug, PW))
            d = admin(f'coop_{slug}'); d.cursor().execute(db_roles.stage2(slug)); d.close()
        a.close()

    def test_role_is_not_privileged(self):
        c = as_role('sectesta', 'coop_sectesta'); cur = c.cursor()
        cur.execute('SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = current_user')
        self.assertEqual(cur.fetchone(), (False, False, False, False, False))

    def test_can_run_app_ddl_in_own_database(self):
        c = as_role('sectesta', 'coop_sectesta'); c.autocommit = True; cur = c.cursor()
        cur.execute('ALTER TABLE members ADD COLUMN IF NOT EXISTS extra text')       # pre-existing table now owned by role
        cur.execute('CREATE TABLE IF NOT EXISTS t_new (id int)')
        cur.execute('CREATE INDEX IF NOT EXISTS i_new ON t_new(id)')
        cur.execute('SELECT pg_advisory_lock(2026072301)'); cur.execute('SELECT pg_advisory_unlock(2026072301)')
        cur.execute("SELECT name FROM members"); self.assertEqual(cur.fetchone()[0], 'legacy-owned-by-postgres')

    def test_cannot_connect_to_another_clients_database(self):
        for target in ('coop_sectestb', 'postgres', 'template1', 'coop_sectest'):
            with self.assertRaises(psycopg2.OperationalError, msg=target):
                as_role('sectesta', target)
        with self.assertRaises(psycopg2.OperationalError):
            as_role('sectestb', 'coop_sectesta')

    def test_cannot_escalate(self):
        c = as_role('sectesta', 'coop_sectesta'); c.autocommit = True; cur = c.cursor()
        for sql in ("CREATE ROLE evil SUPERUSER", "CREATE DATABASE evil",
                    "COPY (SELECT 1) TO PROGRAM 'id'", "SELECT pg_read_file('/etc/passwd')",
                    "ALTER ROLE coop_sectesta SUPERUSER", "CREATE EXTENSION IF NOT EXISTS plpython3u"):
            with self.assertRaises(psycopg2.Error, msg=sql):
                cur.execute(sql)

    def test_cannot_read_other_client_tables_via_cross_db(self):
        c = as_role('sectesta', 'coop_sectesta'); cur = c.cursor()
        with self.assertRaises(psycopg2.Error):
            cur.execute('SELECT * FROM coop_sectestb.public.members')

    def test_script_is_idempotent_and_rotates(self):
        # The local test cluster uses trust auth, so compare the stored password hash instead.
        a = admin(); cur = a.cursor()
        cur.execute("SELECT rolpassword FROM pg_authid WHERE rolname='coop_sectesta'"); before = cur.fetchone()[0]
        cur.execute(db_roles.stage1('sectesta', 'rotated_password_abcdefghijklmnop12'))
        cur.execute("SELECT rolpassword FROM pg_authid WHERE rolname='coop_sectesta'"); mid = cur.fetchone()[0]
        self.assertNotEqual(before, mid)
        cur.execute(db_roles.stage1('sectesta', PW))          # re-running is harmless
        as_role('sectesta', 'coop_sectesta').close()

    def test_rejects_unsafe_input(self):
        for slug, pw in (("a'; DROP", PW), ('ok-name', "x'; DROP ROLE postgres; --" + 'a' * 20), ('ok-name', 'short')):
            with self.assertRaises(ValueError):
                db_roles.stage1(slug, pw)


if __name__ == '__main__':
    unittest.main()
