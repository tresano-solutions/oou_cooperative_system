"""
AUTH-01 shared-IP lockout is a denial-of-service on staff logins
AUTH-02 mobile login skips 2FA
AUTH-03 mobile JWT survives account deactivation and password change
AUTH-04 web session cookie replay after logout / password change
AUTH-05 password policy accepts weak passwords
AUTH-06 purge endpoint needs only a typed phrase (no re-auth, no step-up)
"""
import sys, os, unittest, json
sys.path.insert(0, os.path.dirname(__file__))
import _harness as H

app = H.boot_app()


class AuthnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.members, cls.loans = H.seed(app)

    def test_auth01_lockout_dos(self):
        c = app.test_client()
        for i in range(6):
            c.post('/login', data={'username': f'nobody{i}', 'password': 'x'})
        r = c.post('/login', data={'username': 'admin', 'password': H.ADMIN_PW})
        locked = r.status_code == 200      # a successful login redirects (302)
        print(f'\nAUTH-01: after 6 failed logins for non-existent users, the REAL admin password '
              f'is {"REFUSED (locked out)" if locked else "accepted"} from the same IP')
        from utils import clear_login_attempts
        with app.app_context():
            clear_login_attempts('127.0.0.1')
        self.assertFalse(locked)

    def test_auth02_mobile_login_skips_2fa(self):
        import pyotp
        from database import get_db
        from security import encrypt_2fa_secret
        with app.app_context():
            db = get_db()
            db.execute('UPDATE users SET two_factor_secret=?, two_factor_enabled=1 WHERE username=?',
                       (encrypt_2fa_secret(pyotp.random_base32()), 'treasurer'))
            db.commit()
        web = app.test_client()
        r = web.post('/login', data={'username': 'treasurer', 'password': H.TREAS_PW})
        web_challenged = r.headers.get('Location', '').endswith('/login/verify')
        m = app.test_client().post('/api/mobile/login', json={'username': 'treasurer', 'password': H.TREAS_PW})
        got_token = bool(m.get_json().get('token'))
        print(f'\nAUTH-02: web login for 2FA-enabled treasurer asks for code: {web_challenged}; '
              f'mobile API issues a token with password only: {got_token}')
        self.__class__.treas_token = m.get_json().get('token')
        self.assertFalse(got_token and web_challenged, '2FA bypass via mobile API')

    def test_auth03_jwt_after_deactivation_and_pw_change(self):
        from database import get_db
        from werkzeug.security import generate_password_hash
        c = app.test_client()
        r = c.post('/api/mobile/login', json={'username': 'alice@sectest.invalid', 'password': H.MEMBER_PW})
        tok = r.get_json()['token']
        hdr = {'Authorization': f'Bearer {tok}'}
        before = c.get('/api/mobile/card', headers=hdr).status_code
        with app.app_context():
            db = get_db()
            db.execute("UPDATE users SET is_active=0, password_hash=? WHERE username='alice@sectest.invalid'",
                       (generate_password_hash('Changed-By-Admin-9'),))
            db.commit()
        after = c.get('/api/mobile/card', headers=hdr).status_code
        print(f'\nAUTH-03: mobile card endpoint before={before}; AFTER account deactivated + password changed={after} '
              f'(token lifetime 24h, no revocation)')
        with app.app_context():
            db = get_db()
            db.execute("UPDATE users SET is_active=1, password_hash=? WHERE username='alice@sectest.invalid'",
                       (generate_password_hash(H.MEMBER_PW),))
            db.commit()
        self.assertNotEqual(after, 200, 'JWT still honoured for a deactivated user')

    def test_auth04_cookie_replay(self):
        c = app.test_client()
        H.login(c, 'secretary', H.SECR_PW)
        cookie = c.get_cookie('session').value
        self.assertEqual(c.get('/loans').status_code, 200)
        c.get('/logout')
        replay = app.test_client()
        replay.set_cookie('session', cookie)
        code = replay.get('/loans').status_code
        print(f'\nAUTH-04: replaying the pre-logout session cookie after logout -> HTTP {code} '
              f'({"STILL LOGGED IN" if code == 200 else "rejected"})')
        self.assertNotEqual(code, 200)

    def test_auth08_web_session_survives_deactivation(self):
        """A logged-in user whose account an admin disables keeps working until their cookie lapses."""
        from database import get_db
        c = app.test_client(); H.login(c, 'secretary', H.SECR_PW)
        before = c.get('/loans').status_code
        with app.app_context():
            db = get_db(); db.execute("UPDATE users SET is_active=0 WHERE username='secretary'"); db.commit()
        after = c.get('/loans').status_code
        with app.app_context():
            db = get_db(); db.execute("UPDATE users SET is_active=1 WHERE username='secretary'"); db.commit()
        print(f'\nAUTH-08: /loans before deactivation={before}; after an admin disables the account={after}')
        self.assertNotEqual(after, 200, 'disabled user still has a working web session')

    def test_auth05_weak_passwords(self):
        from security import validate_password_strength
        weak = ['Password1', 'Welcome1', 'Qwerty123', 'Coop12345']
        accepted = [w for w in weak if validate_password_strength(w)[0]]
        print(f'\nAUTH-05: common passwords accepted by the policy: {accepted}')
        self.assertEqual(accepted, [])

    def test_auth06_purge_needs_only_typed_phrase(self):
        from database import get_db
        c = app.test_client(); H.login(c, 'admin', H.ADMIN_PW)
        with app.app_context():
            n0 = get_db().execute('SELECT COUNT(*) AS c FROM members').fetchone()['c']
            a0 = get_db().execute('SELECT COUNT(*) AS c FROM audit_log').fetchone()['c']
        r = c.post('/migration/purge', data={'confirm_phrase': 'PURGE ALL DATA'})
        with app.app_context():
            n1 = get_db().execute('SELECT COUNT(*) AS c FROM members').fetchone()['c']
            a1 = get_db().execute('SELECT COUNT(*) AS c FROM audit_log').fetchone()['c']
        print(f'\nAUTH-06: one admin POST with a fixed phrase: members {n0}->{n1}, audit_log rows {a0}->{a1}')
        self.assertEqual(n1, n0, 'whole membership + audit trail wiped by one request, no re-auth/backup')




class IdentityLinkTests(unittest.TestCase):
    """AUTH-07: members are linked to logins by e-mail, compared case-insensitively on read
    but case-sensitively on the uniqueness check. A member can edit their own e-mail."""
    @classmethod
    def setUpClass(cls):
        import _harness
        cls.members, cls.loans = _harness.seed(app)

    def test_auth07_email_case_takeover(self):
        from database import get_db
        with app.app_context():
            db = get_db()
            db.execute("UPDATE members SET email='Bob@sectest.invalid' WHERE first_name='Bob'")
            db.execute("UPDATE users SET email='Bob@sectest.invalid' WHERE username='bob@sectest.invalid'")
            db.commit()
        c = app.test_client(); H.login(c, 'alice@sectest.invalid', H.MEMBER_PW)
        r = c.post('/edit-profile', data={'first_name': 'Alice', 'last_name': 'Alpha',
                                          'email': 'bob@sectest.invalid', 'phone': '08000000001'})
        with app.app_context():
            who = get_db().execute("SELECT first_name FROM members WHERE lower(email)=lower('bob@sectest.invalid') ORDER BY id").fetchall()
        body = c.get('/profile').get_data(as_text=True)
        sees_bob = 'Beta' in body
        print(f"\nAUTH-07: after Alice set her e-mail to a case-variant of Bob's, members matching that e-mail: "
              f"{[w['first_name'] for w in who]}; Alice's /profile now shows Bob's record: {sees_bob}")
        self.assertFalse(sees_bob)


class CsrfTests(unittest.TestCase):
    def test_csrf01_enforced_on_state_changing_post(self):
        app.config['WTF_CSRF_ENABLED'] = True
        try:
            c = app.test_client()
            r1 = c.post('/login', data={'username': 'admin', 'password': H.ADMIN_PW})
            print(f'\nCSRF-01: POST /login without a CSRF token -> HTTP {r1.status_code} (400 = enforced)')
            self.assertEqual(r1.status_code, 400)
        finally:
            app.config['WTF_CSRF_ENABLED'] = False


if __name__ == '__main__':
    unittest.main()
