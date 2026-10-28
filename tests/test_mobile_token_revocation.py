"""Signing out of the mobile app must actually retire the token.

Logging out cleared the phone and nothing else. The JWT stayed valid until it
expired — up to thirty days — so a copy taken from a lost, sold or shared
device still opened the member's account.
"""

import os
import unittest

TEST_DB = os.path.abspath('.test-mobile-revocation.db')
os.environ.setdefault('SECRET_KEY', 'test-secret-revocation')
os.environ.setdefault('ADMIN_PASSWORD', 'TestAdmin123')
os.environ.setdefault('FLASK_DEBUG', '1')
os.environ.setdefault('FIELD_ENCRYPTION_KEY', '05SmPJhNFMKwg9NysnBdQjKtqn3VwWDl1IiPIMAg2as=')
os.environ.pop('DATABASE_URL', None)
os.environ['SQLITE_DB_PATH'] = TEST_DB
try:
    os.remove(TEST_DB)
except FileNotFoundError:
    pass

import jwt                                                    # noqa: E402
import app as app_module                                      # noqa: E402
from database import get_db, last_insert_id                   # noqa: E402
from werkzeug.security import generate_password_hash          # noqa: E402

PASSWORD = 'MobileMember123!'


class MobileTokenRevocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app_module.app
        cls.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    def setUp(self):
        self.client = self.app.test_client()
        tag = os.urandom(4).hex()
        self.email = f'mob{tag}@coop.test'
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO members (member_number, first_name, last_name, status, "
                "date_joined, email, monthly_savings) VALUES (?, 'Mob', 'Member', 'active', "
                "'2024-01-01', ?, 5000)", (f'MB/{tag}', self.email))
            last_insert_id(db)
            db.execute(
                "INSERT INTO users (username, password_hash, role, email, full_name, is_active) "
                "VALUES (?, ?, 'member', ?, 'Mob Member', 1)",
                (self.email, generate_password_hash(PASSWORD), self.email))
            db.commit()

    def token(self):
        r = self.client.post('/api/mobile/login',
                             json={'username': self.email, 'password': PASSWORD})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()['token']

    def me(self, token):
        return self.client.get('/api/mobile/v1/me',
                               headers={'Authorization': f'Bearer {token}'})

    def sign_out(self, token):
        return self.client.post('/api/mobile/v1/auth/logout',
                                headers={'Authorization': f'Bearer {token}'})

    # ── the token carries an identity ────────────────────────────────────────

    def test_a_token_can_be_named(self):
        payload = jwt.decode(self.token(), self.app.config['SECRET_KEY'],
                             algorithms=['HS256'], audience='coopms-mobile',
                             options={'verify_iss': False})
        self.assertIn('jti', payload)
        self.assertEqual(len(payload['jti']), 32)

    def test_two_sign_ins_get_different_tokens(self):
        a = jwt.decode(self.token(), self.app.config['SECRET_KEY'], algorithms=['HS256'],
                       audience='coopms-mobile', options={'verify_iss': False})
        b = jwt.decode(self.token(), self.app.config['SECRET_KEY'], algorithms=['HS256'],
                       audience='coopms-mobile', options={'verify_iss': False})
        self.assertNotEqual(a['jti'], b['jti'])

    # ── signing out ──────────────────────────────────────────────────────────

    def test_the_token_works_until_it_is_handed_back(self):
        token = self.token()
        self.assertEqual(self.me(token).status_code, 200)
        self.assertEqual(self.sign_out(token).status_code, 200)
        self.assertEqual(self.me(token).status_code, 401,
                         'a copied token must stop working the moment the member signs out')

    def test_the_refusal_says_to_sign_in_again(self):
        token = self.token()
        self.sign_out(token)
        self.assertEqual(self.me(token).get_json()['code'], 'token_revoked')

    def test_signing_out_twice_is_not_an_error(self):
        token = self.token()
        self.sign_out(token)
        # The second attempt arrives with a token that is already refused.
        self.assertEqual(self.sign_out(token).status_code, 401)

    def test_the_other_device_keeps_working(self):
        phone, tablet = self.token(), self.token()
        self.sign_out(phone)
        self.assertEqual(self.me(phone).status_code, 401)
        self.assertEqual(self.me(tablet).status_code, 200,
                         'signing out of one device must not turn the member out of the other')

    def test_a_stranger_cannot_sign_out_without_a_token(self):
        self.assertEqual(self.client.post('/api/mobile/v1/auth/logout').status_code, 401)

    # ── housekeeping ─────────────────────────────────────────────────────────

    def test_the_table_does_not_grow_past_the_tokens_still_alive(self):
        from datetime import datetime, timedelta, UTC
        token = self.token()
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO revoked_tokens (jti, user_id, expires_at) "
                       "VALUES ('stale-entry', 1, ?)",
                       (datetime.now(UTC) - timedelta(days=1),))
            db.commit()
        self.sign_out(token)
        with self.app.app_context():
            left = get_db().execute(
                "SELECT COUNT(*) AS n FROM revoked_tokens WHERE jti = 'stale-entry'"
            ).fetchone()['n']
        self.assertEqual(left, 0, 'an entry past its expiry has nothing left to revoke')


if __name__ == '__main__':
    unittest.main()
