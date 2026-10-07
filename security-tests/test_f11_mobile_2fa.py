"""F-11 regression: the mobile login demands the 2FA code for accounts that have 2FA on."""
import sys, os, unittest
sys.path.insert(0, os.path.dirname(__file__))
import _harness as H

app = H.boot_app()


class MobileTwoFactor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import pyotp
        from database import get_db
        from security import enable_user_2fa
        H.seed(app)
        cls.secret = pyotp.random_base32()
        with app.app_context():
            db = get_db()
            uid = db.execute("SELECT id FROM users WHERE username='treasurer'").fetchone()['id']
            cls.backup = enable_user_2fa(db, uid, cls.secret)
            db.commit()

    def setUp(self):
        from utils import clear_login_attempts
        with app.app_context():
            clear_login_attempts('mobile:127.0.0.1')

    def _login(self, **extra):
        return app.test_client().post('/api/mobile/login',
                                      json=dict({'username': 'treasurer', 'password': H.TREAS_PW}, **extra))

    def test_password_alone_is_not_enough(self):
        r = self._login()
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()['code'], 'otp_required')
        self.assertNotIn('token', r.get_json())

    def test_wrong_code_refused_and_counted(self):
        r = self._login(otp='000000')
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()['code'], 'otp_invalid')

    def test_valid_totp_logs_in(self):
        import pyotp
        r = self._login(otp=pyotp.TOTP(self.secret).now())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['token'])

    def test_backup_code_works_once(self):
        code = self.backup[0]
        self.assertEqual(self._login(otp=code).status_code, 200)
        self.assertEqual(self._login(otp=code).status_code, 401)

    def test_account_without_2fa_unchanged(self):
        r = app.test_client().post('/api/mobile/login', json={'username': 'alice@sectest.invalid', 'password': H.MEMBER_PW})
        self.assertEqual(r.status_code, 200)

    def test_brute_forcing_codes_is_rate_limited(self):
        c = app.test_client()
        codes = []
        for i in range(12):   # 8 tries per account, then a short pause
            codes.append(c.post('/api/mobile/login', json={'username': 'treasurer', 'password': H.TREAS_PW,
                                                           'otp': f'{i:06d}'}).status_code)
        self.assertIn(429, codes)


if __name__ == '__main__':
    unittest.main()
