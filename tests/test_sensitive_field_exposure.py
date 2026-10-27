"""A logged-in session must not be enough to read a member's BVN, NIN or account.

The profile page hides these behind a password prompt. Edit Profile used to put
the full values into the form's value= attributes, so anyone at an unattended
signed-in browser could read them from the page without the password ever being
asked for.
"""

import os
import unittest

TEST_DB = os.path.abspath('.test-sensitive-fields.db')
os.environ.setdefault('SECRET_KEY', 'test-secret-sensitive')
os.environ.setdefault('ADMIN_PASSWORD', 'TestAdmin123')
os.environ.setdefault('FLASK_DEBUG', '1')
os.environ.setdefault('FIELD_ENCRYPTION_KEY', '05SmPJhNFMKwg9NysnBdQjKtqn3VwWDl1IiPIMAg2as=')
os.environ.pop('DATABASE_URL', None)
os.environ['SQLITE_DB_PATH'] = TEST_DB
try:
    os.remove(TEST_DB)
except FileNotFoundError:
    pass

import app as app_module                                      # noqa: E402
from database import get_db, last_insert_id                   # noqa: E402
from crypto import encrypt_field, decrypt_field               # noqa: E402
from werkzeug.security import generate_password_hash          # noqa: E402

BVN = '22233344455'
NIN = '99988877766'
ACCOUNT = '0123456789'
PASSWORD = 'MemberPass123!'


class SensitiveFieldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app_module.app
        cls.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    def setUp(self):
        self.client = self.app.test_client()
        self.email = f'sens{os.urandom(4).hex()}@coop.test'
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO members (member_number, first_name, last_name, status, "
                "date_joined, email, phone, bank_name, account_name, account_number, bvn, nin) "
                "VALUES (?, 'Sens', 'Member', 'active', '2024-01-01', ?, '08000000000', "
                "?, ?, ?, ?, ?)",
                (f'SF/{os.urandom(4).hex()}', self.email,
                 encrypt_field('GTBank'), encrypt_field('Sens Member'),
                 encrypt_field(ACCOUNT), encrypt_field(BVN), encrypt_field(NIN)))
            self.member_id = last_insert_id(db)
            db.execute(
                "INSERT INTO users (username, password_hash, role, email, full_name, is_active) "
                "VALUES (?, ?, 'member', ?, 'Sens Member', 1)",
                (self.email, generate_password_hash(PASSWORD), self.email))
            db.commit()

    def login(self):
        r = self.client.post('/login', data={'username': self.email, 'password': PASSWORD})
        self.assertIn(r.status_code, (302, 303))

    def stored(self):
        with self.app.app_context():
            row = get_db().execute(
                'SELECT bank_name, account_name, account_number, bvn, nin FROM members WHERE id = ?',
                (self.member_id,)).fetchone()
        return {k: decrypt_field(row[k] or '') for k in row.keys()}

    # ── what the page gives away ─────────────────────────────────────────────

    def test_edit_profile_does_not_contain_the_full_values(self):
        self.login()
        body = self.client.get('/edit-profile').get_data(as_text=True)
        for secret, name in ((BVN, 'BVN'), (NIN, 'NIN'), (ACCOUNT, 'account number')):
            self.assertNotIn(secret, body, f'the full {name} must not reach the page')

    def test_edit_profile_shows_a_mask_so_the_member_knows_it_is_on_file(self):
        self.login()
        body = self.client.get('/edit-profile').get_data(as_text=True)
        self.assertIn('*******' + BVN[-4:], body)

    def test_the_password_prompt_still_returns_the_real_values(self):
        self.login()
        r = self.client.post('/profile/reveal-sensitive', data={'password': PASSWORD})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['fields']['bvn'], BVN)

    def test_a_wrong_password_reveals_nothing(self):
        self.login()
        r = self.client.post('/profile/reveal-sensitive', data={'password': 'not-it'})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn(BVN, r.get_data(as_text=True))

    # ── saving the form must not erase what it no longer carries ─────────────

    def _submit(self, **overrides):
        data = {'first_name': 'Sens', 'last_name': 'Member', 'email': self.email,
                'phone': '08000000000', 'bank_name': '', 'account_name': '',
                'account_number': '', 'bvn': '', 'nin': ''}
        data.update(overrides)
        return self.client.post('/edit-profile', data=data, follow_redirects=True)

    def test_saving_with_the_boxes_empty_keeps_what_is_on_file(self):
        self.login()
        self._submit()
        kept = self.stored()
        self.assertEqual(kept['bvn'], BVN, 'an empty box must mean "leave it alone"')
        self.assertEqual(kept['nin'], NIN)
        self.assertEqual(kept['account_number'], ACCOUNT)
        self.assertEqual(kept['bank_name'], 'GTBank')

    def test_typing_a_new_value_replaces_the_old_one(self):
        self.login()
        self._submit(bvn='11122233344', bank_name='Zenith')
        kept = self.stored()
        self.assertEqual(kept['bvn'], '11122233344')
        self.assertEqual(kept['bank_name'], 'Zenith')
        self.assertEqual(kept['nin'], NIN, 'the fields left blank stay as they were')

    def test_a_badly_formed_value_is_still_refused(self):
        self.login()
        self._submit(bvn='123')
        self.assertEqual(self.stored()['bvn'], BVN, 'a rejected edit must change nothing')


if __name__ == '__main__':
    unittest.main()
