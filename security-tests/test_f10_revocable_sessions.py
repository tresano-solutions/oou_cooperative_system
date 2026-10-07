"""F-10 regression: sessions and mobile tokens can be revoked."""
import sys, os, unittest
sys.path.insert(0, os.path.dirname(__file__))
import _harness as H

app = H.boot_app()


class Revocation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.members, cls.loans = H.seed(app)

    def _sql(self, sql, *a):
        from database import get_db
        with app.app_context():
            db = get_db(); db.execute(sql, a); db.commit()

    def test_logout_kills_a_copied_cookie(self):
        c = app.test_client(); H.login(c, 'secretary', H.SECR_PW)
        cookie = c.get_cookie('session').value
        self.assertEqual(c.get('/loans').status_code, 200)
        c.get('/logout')
        r = app.test_client(); r.set_cookie('session', cookie)
        self.assertNotEqual(r.get('/loans').status_code, 200)

    def test_second_device_survives_other_devices_logout(self):
        a = app.test_client(); H.login(a, 'treasurer', H.TREAS_PW)
        b = app.test_client(); H.login(b, 'treasurer', H.TREAS_PW)
        a.get('/logout')
        self.assertEqual(b.get('/loans').status_code, 200)

    def test_deactivation_cuts_web_session(self):
        c = app.test_client(); H.login(c, 'secretary', H.SECR_PW)
        self.assertEqual(c.get('/loans').status_code, 200)
        self._sql("UPDATE users SET is_active=0 WHERE username='secretary'")
        self.assertNotEqual(c.get('/loans').status_code, 200)
        self._sql("UPDATE users SET is_active=1 WHERE username='secretary'")

    def test_admin_password_reset_revokes_sessions_and_tokens(self):
        from database import get_db
        v = app.test_client(); H.login(v, 'secretary', H.SECR_PW)
        tok = app.test_client().post('/api/mobile/login', json={'username': 'alice@sectest.invalid', 'password': H.MEMBER_PW}).get_json()['token']
        hdr = {'Authorization': f'Bearer {tok}'}
        self.assertEqual(app.test_client().get('/api/mobile/card', headers=hdr).status_code, 200)
        with app.app_context():
            uid = get_db().execute("SELECT id FROM users WHERE username='secretary'").fetchone()['id']
            aid = get_db().execute("SELECT id FROM users WHERE username='alice@sectest.invalid'").fetchone()['id']
        adm = app.test_client(); H.login(adm, 'admin', H.ADMIN_PW)
        adm.post(f'/api/reset_user_password/{uid}', data={'new_password': 'Rotated-Pass-9x', 'force_change': '0'})
        adm.post(f'/api/reset_user_password/{aid}', data={'new_password': 'Rotated-Pass-9y', 'force_change': '0'})
        self.assertNotEqual(v.get('/loans').status_code, 200)
        r = app.test_client().get('/api/mobile/card', headers=hdr)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()['code'], 'token_revoked')
        from werkzeug.security import generate_password_hash
        self._sql('UPDATE users SET password_hash=? WHERE id=?', generate_password_hash(H.SECR_PW), uid)

    def test_mobile_token_dies_on_deactivation(self):
        tok = app.test_client().post('/api/mobile/login', json={'username': 'bob@sectest.invalid', 'password': H.MEMBER_PW}).get_json()['token']
        hdr = {'Authorization': f'Bearer {tok}'}
        self.assertEqual(app.test_client().get('/api/mobile/card', headers=hdr).status_code, 200)
        self._sql("UPDATE users SET is_active=0 WHERE username='bob@sectest.invalid'")
        self.assertEqual(app.test_client().get('/api/mobile/card', headers=hdr).status_code, 401)
        self._sql("UPDATE users SET is_active=1 WHERE username='bob@sectest.invalid'")

    def test_role_change_revokes(self):
        from database import get_db
        v = app.test_client(); H.login(v, 'treasurer', H.TREAS_PW)
        with app.app_context():
            uid = get_db().execute("SELECT id FROM users WHERE username='treasurer'").fetchone()['id']
        adm = app.test_client(); H.login(adm, 'admin', H.ADMIN_PW)
        adm.post(f'/api/edit_user/{uid}', data={'full_name': 'T', 'email': 't@sectest.invalid', 'role': 'treasurer'})
        self.assertNotEqual(v.get('/loans').status_code, 200)

    def test_fresh_login_still_works(self):
        c = app.test_client(); H.login(c, 'admin', H.ADMIN_PW)
        self.assertEqual(c.get('/loans').status_code, 200)


if __name__ == '__main__':
    unittest.main()
