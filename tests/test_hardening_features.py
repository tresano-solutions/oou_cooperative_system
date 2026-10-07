import json
import os
import time
import unittest
from datetime import datetime
from io import BytesIO
from unittest.mock import patch
from urllib.parse import urlparse

import jwt
from werkzeug.security import check_password_hash, generate_password_hash

TEST_DB = os.path.abspath('.test-hardening-features.db')
os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-hardening-regression')
os.environ.setdefault('ADMIN_PASSWORD', 'TestAdmin123')
os.environ.setdefault('FLASK_DEBUG', '1')
os.environ.setdefault('FIELD_ENCRYPTION_KEY', '05SmPJhNFMKwg9NysnBdQjKtqn3VwWDl1IiPIMAg2as=')
os.environ.pop('DATABASE_URL', None)
os.environ['SQLITE_DB_PATH'] = TEST_DB

try:
    os.remove(TEST_DB)
except FileNotFoundError:
    pass

import app as app_module  # noqa: E402
from database import get_db  # noqa: E402
from crypto import decrypt_field, is_encrypted  # noqa: E402
from ledger import backfill_from_transactions, ledger_reconciliation  # noqa: E402
from utils import member_savings_balance  # noqa: E402
from mobile_api import JWT_AUDIENCE  # noqa: E402
from reports_engine import income_statement  # noqa: E402
from security import generate_compliant_password, validate_password_strength  # noqa: E402
from utils import clear_login_attempts, is_rate_limited, record_failed_login  # noqa: E402


class HardeningFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app_module.app
        cls.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    def setUp(self):
        self.client = self.app.test_client()

    def login_admin(self):
        response = self.client.post(
            '/login',
            data={'username': 'admin', 'password': 'TestAdmin123'},
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))

    def test_idle_session_timeout_logs_user_out_and_audits(self):
        self.login_admin()
        with self.client.session_transaction() as sess:
            sess['last_activity_at'] = time.time() - (self.app.config['IDLE_TIMEOUT_SECONDS'] + 5)

        response = self.client.get('/dashboard', follow_redirects=False)
        self.assertIn(response.status_code, (302, 303))
        self.assertIn('/login', response.headers.get('Location', ''))

        with self.app.app_context():
            db = get_db()
            row = db.execute(
                "SELECT action, module, description FROM audit_log WHERE action = 'SESSION_TIMEOUT' ORDER BY id DESC"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row['module'], 'auth')
            self.assertIn('inactivity', row['description'].lower())

    def test_edit_member_join_date_is_saved_validated_and_audited(self):
        self.login_admin()
        mid = self.create_member()
        with self.app.app_context():
            db = get_db()
            db.execute('UPDATE members SET date_joined = ? WHERE id = ?',
                       ('2026-09-01', mid))
            db.commit()
        page = self.client.get(f'/members/edit/{mid}')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'name="date_joined"', page.data)
        base = dict(first_name='Ada', last_name='Audit', phone='08000000001')
        response = self.client.post(f'/members/edit/{mid}',
                                    data={**base, 'date_joined': '2020-01-15'})
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            from utils import member_has_minimum_membership
            db = get_db()
            member = db.execute('SELECT * FROM members WHERE id = ?', (mid,)).fetchone()
            self.assertEqual(str(member['date_joined'])[:10], '2020-01-15')
            self.assertTrue(member_has_minimum_membership(member))
            record = db.execute("SELECT description FROM audit_log WHERE action = 'CHANGE_MEMBER_JOIN_DATE' ORDER BY id DESC").fetchone()
            self.assertIn('2026-09-01', record['description'])
            self.assertIn('2020-01-15', record['description'])
        for bad_date in ('', 'invalid', '2024-02-30', '2999-01-01'):
            self.client.post(f'/members/edit/{mid}', data={**base, 'date_joined': bad_date})
            with self.app.app_context():
                saved = get_db().execute('SELECT date_joined FROM members WHERE id = ?', (mid,)).fetchone()
                self.assertEqual(str(saved['date_joined'])[:10], '2020-01-15')
        self.client.post(f'/members/edit/{mid}', data=base)
        with self.app.app_context():
            saved = get_db().execute('SELECT date_joined FROM members WHERE id = ?', (mid,)).fetchone()
            self.assertEqual(str(saved['date_joined'])[:10], '2020-01-15')

    def test_authenticated_pages_have_security_headers(self):
        self.login_admin()
        response = self.client.get('/dashboard')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('X-Frame-Options'), 'DENY')
        self.assertEqual(response.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertIn('no-store', response.headers.get('Cache-Control', ''))

    def create_member(self):
        with self.app.app_context():
            db = get_db()
            existing = db.execute(
                "SELECT * FROM members WHERE member_number = 'OOU/TEST/0001'"
            ).fetchone()
            if existing:
                return existing['id']
            db.execute('''
                INSERT INTO members
                    (member_number, employee_id, first_name, last_name, email,
                     phone, status, monthly_savings, total_savings, date_joined)
                VALUES
                    ('OOU/TEST/0001', 'EMP001', 'Ada', 'Audit',
                     'ada.audit@example.com', '08000000001', 'active',
                     15000, 0, '2024-01-01')
            ''')
            db.commit()
            return db.execute(
                "SELECT id FROM members WHERE member_number = 'OOU/TEST/0001'"
            ).fetchone()['id']

    def create_member_user(self, member_id, email='ada.audit@example.com'):
        with self.app.app_context():
            db = get_db()
            db.execute('''
                INSERT INTO users (username, password_hash, role, full_name, email, is_active, must_change_password)
                VALUES (?, ?, 'member', 'Ada Audit', ?, 1, 0)
                ON CONFLICT(username) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    is_active = 1,
                    must_change_password = 0
            ''', (email, generate_password_hash('MemberPass1!'), email))
            db.commit()

    def create_non_staff_member(self):
        email = 'non.staff.loan@example.com'
        with self.app.app_context():
            db = get_db()
            existing = db.execute(
                "SELECT * FROM members WHERE member_number = 'OOU/TEST/N001'"
            ).fetchone()
            if existing:
                return existing['id'], email
            db.execute('''
                INSERT INTO members
                    (member_number, employee_id, first_name, last_name, email,
                     phone, status, monthly_savings, total_savings, date_joined)
                VALUES
                    ('OOU/TEST/N001', NULL, 'Nora', 'Nonstaff',
                     ?, '08000000011', 'active', 15000, 0, '2024-01-01')
            ''', (email,))
            db.commit()
            return db.execute(
                "SELECT id FROM members WHERE member_number = 'OOU/TEST/N001'"
            ).fetchone()['id'], email

    def create_guarantor_member(self, number, email, first_name):
        with self.app.app_context():
            db = get_db()
            existing = db.execute('SELECT id FROM members WHERE member_number = ?', (number,)).fetchone()
            if existing:
                return existing['id']
            db.execute('''
                INSERT INTO members
                    (member_number, employee_id, first_name, last_name, email,
                     phone, status, monthly_savings, total_savings, date_joined)
                VALUES (?, ?, ?, 'Guarantor', ?, '08000000991', 'active', 15000, 100000, '2024-01-01')
            ''', (number, number.replace('/', ''), first_name, email))
            db.commit()
            return db.execute('SELECT id FROM members WHERE member_number = ?', (number,)).fetchone()['id']

    def fund_member_savings(self, member_id, amount=100000):
        with self.app.app_context():
            db = get_db()
            receipt = f'SAV/LOANAPP/{member_id}'
            if db.execute('SELECT id FROM savings WHERE receipt_number = ?', (receipt,)).fetchone():
                backfill_from_transactions(db)
                db.commit()
                return
            db.execute('''
                INSERT INTO savings
                    (member_id, amount, month, payment_type, payment_method, receipt_number, date)
                VALUES (?, ?, '2026-07', 'monthly', 'cash', ?, '2026-07-01')
            ''', (member_id, amount, receipt))
            backfill_from_transactions(db)
            db.commit()

    def login_member(self, email='ada.audit@example.com'):
        response = self.client.post(
            '/login',
            data={'username': email, 'password': 'MemberPass1!'},
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))

    def test_migrated_member_join_date_is_used_for_loan_eligibility(self):
        suffix = int(time.time() * 1000)
        member_number = f'OOU/MIG/{suffix}'
        with self.app.app_context():
            db = get_db()
            db.execute('''
                INSERT INTO members
                    (member_number, employee_id, first_name, last_name, email,
                     phone, status, monthly_savings, total_savings, date_joined)
                VALUES (?, ?, 'Migrated', 'Member', ?, '08000000222',
                        'active', 15000, 0, '15/01/2024')
            ''', (member_number, f'EMP-MIG-{suffix}', f'migrated.{suffix}@example.com'))
            member_id = db.execute(
                'SELECT id FROM members WHERE member_number = ?', (member_number,)
            ).fetchone()['id']
            db.commit()

        self.fund_member_savings(member_id)
        self.login_admin()
        response = self.client.post(
            '/loans/apply',
            data={
                'member_id': str(member_id),
                'amount': '50000',
                'purpose': 'Regular',
                'tenure': '6',
            },
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            loan = db.execute(
                'SELECT * FROM loans WHERE member_id = ? ORDER BY id DESC',
                (member_id,),
            ).fetchone()
            self.assertIsNotNone(loan)
            self.assertEqual(loan['status'], 'pending')

    def test_bulk_member_upload_accepts_historical_join_date(self):
        suffix = int(time.time() * 1000)
        self.login_admin()
        body = (
            'first_name,last_name,email,phone,address,occupation,monthly_savings,date_joined\n'
            f'Bulk,Joined,bulk.joined.{suffix}@example.com,08000000333,Lagos,Teacher,10000,2024-02-01\n'
        )
        response = self.client.post(
            '/members/bulk-upload',
            data={'file': (BytesIO(body.encode('utf-8')), 'members.csv')},
            content_type='multipart/form-data',
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            member = db.execute(
                'SELECT * FROM members WHERE email = ?',
                (f'bulk.joined.{suffix}@example.com',),
            ).fetchone()
            self.assertIsNotNone(member)
            self.assertTrue(str(member['date_joined']).startswith('2024-02-01'))

    def test_support_routes_are_disabled_by_default(self):
        for path in ('/setup', '/debug-auth', '/emergency-reset'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404, path)

    def test_support_diagnostics_remain_disabled_when_flag_enabled(self):
        with patch.dict(os.environ, {'ENABLE_SUPPORT_ROUTES': '1', 'RESET_TOKEN': 'test-reset-token'}):
            for path in ('/setup', '/debug-auth'):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404, path)

    def test_emergency_reset_requires_post_and_non_url_token(self):
        support_env = {
            'ENABLE_SUPPORT_ROUTES': '1',
            'RESET_TOKEN': 'test-reset-token',
            'ADMIN_PASSWORD': 'TestAdmin123',
        }
        with patch.dict(os.environ, support_env):
            get_response = self.client.get('/emergency-reset?token=test-reset-token')
            self.assertEqual(get_response.status_code, 405)

            query_token_response = self.client.post('/emergency-reset?token=test-reset-token')
            self.assertEqual(query_token_response.status_code, 403)

            form_token_response = self.client.post(
                '/emergency-reset',
                data={'token': 'test-reset-token'},
            )
            self.assertEqual(form_token_response.status_code, 200)
            self.assertNotIn(b'TestAdmin123', form_token_response.data)

    def test_mobile_tenant_endpoint_is_public(self):
        """The mobile tenant endpoint returns the coop identity without auth so
        the app can target the right backend and brand its login screen."""
        resp = self.client.get('/api/mobile/v1/tenant')   # no Authorization header
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get('success'))
        self.assertIn('coop_name', data)
        self.assertIn('coop_short_name', data)
        self.assertIn('logo', data)

    def test_mobile_hq_tenant_resolver_uses_registry(self):
        with self.app.app_context():
            db = get_db()
            db.execute('''
                INSERT INTO coop_tenants (code, name, base_url, logo_url, is_active)
                VALUES ('demo', 'Demo Cooperative', 'https://demo.cooperativems.com/', '/logo.png', 1)
                ON CONFLICT(code) DO UPDATE SET
                    name = excluded.name,
                    base_url = excluded.base_url,
                    logo_url = excluded.logo_url,
                    is_active = excluded.is_active
            ''')
            db.commit()

        response = self.client.get('/api/mobile/v1/tenants/resolve?code=demo')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['tenant']['coop_name'], 'Demo Cooperative')
        self.assertEqual(payload['tenant']['base_url'], 'https://demo.cooperativems.com')

        bad = self.client.get('/api/mobile/v1/tenants/resolve?code=../admin')
        self.assertEqual(bad.status_code, 400)

    def test_mobile_repayment_is_fail_closed(self):
        clear_login_attempts('mobile:127.0.0.1')
        login = self.client.post(
            '/api/mobile/login',
            json={'username': 'admin', 'password': 'TestAdmin123'},
        )
        self.assertEqual(login.status_code, 200)
        token = login.get_json()['token']
        response = self.client.post(
            '/api/mobile/pay',
            json={'amount': 1000},
            headers={'Authorization': f'Bearer {token}'},
        )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()['success'])

    def test_mobile_login_is_rate_limited_and_cleared_on_success(self):
        login_key = 'mobile:203.0.113.10'
        clear_login_attempts(login_key)
        environ = {'REMOTE_ADDR': '203.0.113.10'}

        for _ in range(5):
            response = self.client.post(
                '/api/mobile/login',
                json={'username': 'admin', 'password': 'wrong-password'},
                environ_overrides=environ,
            )
            self.assertEqual(response.status_code, 401)

        blocked = self.client.post(
            '/api/mobile/login',
            json={'username': 'admin', 'password': 'TestAdmin123'},
            environ_overrides=environ,
        )
        self.assertEqual(blocked.status_code, 429)

        clear_login_attempts(login_key)
        success = self.client.post(
            '/api/mobile/login',
            json={'username': 'admin', 'password': 'TestAdmin123'},
            environ_overrides=environ,
        )
        self.assertEqual(success.status_code, 200)

    def test_mobile_login_accepts_email_case_insensitively(self):
        suffix = int(time.time() * 1000)
        email = f'mobile.email.login.{suffix}@example.com'
        member_id = self.create_member()
        self.create_member_user(member_id, email=email)
        clear_login_attempts('mobile:127.0.0.1')

        response = self.client.post(
            '/api/mobile/login',
            json={'username': email.upper(), 'password': 'MemberPass1!'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['user']['email'], email)

    def test_mobile_dashboard_links_member_email_case_insensitively(self):
        suffix = int(time.time() * 1000)
        user_email = f'hq.member.{suffix}@example.com'
        with self.app.app_context():
            db = get_db()
            db.execute('''
                INSERT INTO members
                    (member_number, first_name, last_name, email, phone, status,
                     monthly_savings, total_savings, date_joined)
                VALUES (?, 'HQ', 'Member', ?, '08000000991', 'active', 10000, 0, '2024-01-01')
            ''', (f'HQ/TEST/{suffix}', user_email.upper()))
            db.commit()
            member_id = db.execute(
                'SELECT id FROM members WHERE member_number = ?',
                (f'HQ/TEST/{suffix}',),
            ).fetchone()['id']
        self.create_member_user(member_id, email=user_email)
        clear_login_attempts('mobile:127.0.0.1')

        login_response = self.client.post(
            '/api/mobile/login',
            json={'username': user_email, 'password': 'MemberPass1!'},
        )
        self.assertEqual(login_response.status_code, 200)
        token = login_response.get_json()['token']

        dashboard_response = self.client.get(
            '/api/mobile/v1/dashboard',
            headers={'Authorization': f'Bearer {token}'},
        )
        self.assertEqual(dashboard_response.status_code, 200)
        payload = dashboard_response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['member']['email'], user_email.upper())

    def test_unlinked_member_login_does_not_redirect_loop(self):
        suffix = int(time.time() * 1000)
        username = f'unlinked.member.{suffix}@example.com'
        with self.app.app_context():
            db = get_db()
            db.execute('''
                INSERT INTO users
                    (username, password_hash, role, full_name, email, is_active, must_change_password)
                VALUES (?, ?, 'member', 'Unlinked Member', ?, 1, 0)
            ''', (username, generate_password_hash('MemberPass1!'), username))
            db.commit()

        response = self.client.post(
            '/login',
            data={'username': username, 'password': 'MemberPass1!'},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Member profile link required', response.data)
        self.assertLess(len(response.history), 5)

    def test_mobile_password_reset_request_is_generic_and_sends_email(self):
        suffix = int(time.time() * 1000)
        email = f'mobile.reset.{suffix}@example.com'
        member_id = self.create_member()
        self.create_member_user(member_id, email=email)
        clear_login_attempts(f'mobile-reset:127.0.0.1:{email}')
        with patch('mobile_api.send_password_reset_email') as send_reset:
            send_reset.return_value = True
            response = self.client.post(
                '/api/mobile/v1/auth/forgot-password',
                json={'identifier': email},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])
        send_reset.assert_called_once()

        with patch('mobile_api.send_password_reset_email') as send_reset_unknown:
            unknown = self.client.post(
                '/api/mobile/v1/auth/forgot-password',
                json={'identifier': 'unknown-user@example.com'},
            )
        self.assertEqual(unknown.status_code, 200)
        self.assertTrue(unknown.get_json()['success'])
        send_reset_unknown.assert_not_called()

    def test_mobile_token_requires_expected_audience(self):
        clear_login_attempts('mobile:127.0.0.1')
        response = self.client.post(
            '/api/mobile/login',
            json={'username': 'admin', 'password': 'TestAdmin123'},
        )
        self.assertEqual(response.status_code, 200)
        token = response.get_json()['token']

        with self.assertRaises(jwt.InvalidAudienceError):
            jwt.decode(token, self.app.config['SECRET_KEY'], algorithms=['HS256'])

        payload = jwt.decode(
            token,
            self.app.config['SECRET_KEY'],
            algorithms=['HS256'],
            audience=JWT_AUDIENCE,
        )
        self.assertEqual(payload['username'], 'admin')

    def test_mobile_change_password_requires_current_password_and_policy(self):
        suffix = int(time.time() * 1000)
        email = f'mobile.password.{suffix}@example.com'
        member_id = self.create_member()
        self.create_member_user(member_id, email=email)

        login = self.client.post(
            '/api/mobile/login',
            json={'username': email, 'password': 'MemberPass1!'},
        )
        self.assertEqual(login.status_code, 200)
        headers = {'Authorization': f"Bearer {login.get_json()['token']}"}

        wrong_current = self.client.post(
            '/api/mobile/v1/auth/change-password',
            json={
                'current_password': 'wrong',
                'new_password': 'NewMemberPass1!',
                'confirm_password': 'NewMemberPass1!',
            },
            headers=headers,
        )
        self.assertEqual(wrong_current.status_code, 401)

        weak = self.client.post(
            '/api/mobile/v1/auth/change-password',
            json={
                'current_password': 'MemberPass1!',
                'new_password': 'short',
                'confirm_password': 'short',
            },
            headers=headers,
        )
        self.assertEqual(weak.status_code, 400)

        changed = self.client.post(
            '/api/mobile/v1/auth/change-password',
            json={
                'current_password': 'MemberPass1!',
                'new_password': 'NewMemberPass1!',
                'confirm_password': 'NewMemberPass1!',
            },
            headers=headers,
        )
        self.assertEqual(changed.status_code, 200)

        relogin = self.client.post(
            '/api/mobile/login',
            json={'username': email, 'password': 'NewMemberPass1!'},
        )
        self.assertEqual(relogin.status_code, 200)

    def test_mobile_v1_member_profile_device_notifications_and_loan_withdrawal(self):
        suffix = int(time.time() * 1000)
        email = f'mobile.member.{suffix}@example.com'
        with self.app.app_context():
            db = get_db()
            db.execute('''
                INSERT INTO members
                    (member_number, employee_id, first_name, last_name, email,
                     phone, status, monthly_savings, total_savings, date_joined)
                VALUES (?, ?, 'Mobi', 'Member', ?, '08000000123',
                        'active', 15000, 0, '2024-01-01')
            ''', (f'OOU/TEST/MOB{suffix}', f'EMP-MOB{suffix}', email))
            member_id = db.execute('SELECT id FROM members WHERE email = ?', (email,)).fetchone()['id']
            db.execute('''
                INSERT INTO savings
                    (member_id, amount, month, payment_type, payment_method, receipt_number, date)
                VALUES (?, 100000, '2026-08', 'monthly', 'cash', ?, '2026-08-01')
            ''', (member_id, f'MOB-SAV-{suffix}'))
            db.execute('''
                INSERT INTO loans
                    (loan_number, member_id, amount, purpose, tenure, interest_rate,
                     interest_method, total_repayment, balance, status, approval_stage,
                     date_applied)
                VALUES (?, ?, 50000, 'Regular', 6, 11, 'reducing_annual',
                        52000, 52000, 'pending', 'secretary', '2026-08-02')
            ''', (f'LOAN/MOBILE/{suffix}', member_id))
            loan_id = db.execute(
                'SELECT id FROM loans WHERE member_id = ? ORDER BY id DESC', (member_id,)
            ).fetchone()['id']
            db.commit()
        self.create_member_user(member_id, email=email)

        login = self.client.post(
            '/api/mobile/login',
            json={'username': email, 'password': 'MemberPass1!'},
        )
        self.assertEqual(login.status_code, 200)
        token = login.get_json()['token']
        headers = {'Authorization': f'Bearer {token}'}

        options = self.client.get('/api/mobile/v1/loans/options', headers=headers)
        self.assertEqual(options.status_code, 200)
        options_payload = options.get_json()
        self.assertIn('Regular', [item['value'] for item in options_payload['purposes']])
        self.assertTrue(options_payload['collateral_options'])
        self.assertGreaterEqual(options_payload['guarantors_required'], 0)
        self.assertTrue(all(g['id'] != member_id for g in options_payload['eligible_guarantors']))

        profile_update = self.client.patch(
            '/api/mobile/v1/profile',
            json={
                'city': 'Abeokuta',
                'state': 'Ogun',
                'country': 'Nigeria',
                'date_of_birth': '1990-01-02',
                'address': '1 Mobile Street',
                'occupation': 'Teacher',
                'emergency_contact_name': 'Mobile Helper',
                'emergency_contact_phone': '08000000999',
                'bank_name': 'Zenith Bank',
                'account_name': 'Mobi Member',
                'account_number': '1234567890',
                'bvn': '22222222222',
                'nin': '33333333333',
            },
            headers=headers,
        )
        self.assertEqual(profile_update.status_code, 200)
        member_payload = profile_update.get_json()['member']
        self.assertIn('account_number_masked', member_payload)
        self.assertNotEqual(member_payload['account_number_masked'], '1234567890')

        dashboard = self.client.get('/api/mobile/v1/dashboard', headers=headers)
        self.assertEqual(dashboard.status_code, 200)
        data = dashboard.get_json()
        self.assertTrue(data['success'])
        self.assertGreaterEqual(data['member']['total_savings'], 100000)
        self.assertEqual(len(data['loans']), 1)

        device = self.client.post(
            '/api/mobile/v1/devices',
            json={'platform': 'android', 'push_token': f'ExpoPushToken[{suffix}]', 'device_name': 'Test Phone'},
            headers=headers,
        )
        self.assertEqual(device.status_code, 200)
        self.assertTrue(device.get_json()['device_id'])

        withdraw = self.client.post(
            f'/api/mobile/v1/loans/{loan_id}/withdraw',
            json={'reason': 'Applying later'},
            headers=headers,
        )
        self.assertEqual(withdraw.status_code, 200)
        self.assertEqual(withdraw.get_json()['loan']['status'], 'withdrawn')

        blocked = self.client.post(
            f'/api/mobile/v1/loans/{loan_id}/withdraw',
            json={'reason': 'Again'},
            headers=headers,
        )
        self.assertEqual(blocked.status_code, 409)

        with self.app.app_context():
            db = get_db()
            loan = db.execute('SELECT * FROM loans WHERE id = ?', (loan_id,)).fetchone()
            self.assertEqual(loan['status'], 'withdrawn')
            self.assertEqual(loan['withdrawal_reason'], 'Applying later')
            device_row = db.execute(
                'SELECT * FROM mobile_devices WHERE push_token = ?', (f'ExpoPushToken[{suffix}]',)
            ).fetchone()
            self.assertEqual(device_row['member_id'], member_id)

        guarantor_1 = self.create_guarantor_member(f'OOU/TEST/MG1{suffix}', f'mg1.{suffix}@example.com', 'MobileG1')
        guarantor_2 = self.create_guarantor_member(f'OOU/TEST/MG2{suffix}', f'mg2.{suffix}@example.com', 'MobileG2')
        preview = self.client.post(
            '/api/mobile/v1/loans/schedule-preview',
            json={'amount': 50000, 'purpose': 'Regular', 'tenure': 6},
            headers=headers,
        )
        self.assertEqual(preview.status_code, 200)
        self.assertGreater(preview.get_json()['monthly_payment'], 0)

        invalid_preview = self.client.post(
            '/api/mobile/v1/loans/schedule-preview',
            json={'amount': 50000, 'purpose': 'Free text purpose', 'tenure': 6},
            headers=headers,
        )
        self.assertEqual(invalid_preview.status_code, 400)

        apply = self.client.post(
            '/api/mobile/v1/loans/apply',
            json={
                'amount': 50000,
                'purpose': 'Regular',
                'tenure': 6,
                'payment_collateral_type': 'standing_order',
                'guarantor_ids': [guarantor_1, guarantor_2],
                'signature_name': 'Mobi Member',
                'accept_terms': True,
                'data_processing_consent': True,
                'repayment_schedule_accepted': True,
                'hr_affordability_consent': True,
            },
            headers=headers,
        )
        self.assertEqual(apply.status_code, 201)
        self.assertEqual(apply.get_json()['loan']['status'], 'pending')

        invalid_apply = self.client.post(
            '/api/mobile/v1/loans/apply',
            json={
                'amount': 50000,
                'purpose': 'Free text purpose',
                'tenure': 6,
                'payment_collateral_type': 'standing_order',
                'guarantor_ids': [member_id],
                'signature_name': 'Mobi Member',
                'accept_terms': True,
                'data_processing_consent': True,
                'repayment_schedule_accepted': True,
                'hr_affordability_consent': True,
            },
            headers=headers,
        )
        self.assertEqual(invalid_apply.status_code, 400)

        with self.app.app_context():
            db = get_db()
            new_loan = db.execute(
                "SELECT * FROM loans WHERE member_id = ? AND status = 'pending' ORDER BY id DESC",
                (member_id,),
            ).fetchone()
            self.assertIsNotNone(new_loan)
            self.assertEqual(new_loan['repayment_schedule_accepted'], 1)
            self.assertEqual(new_loan['loan_applicant_type'], 'staff')
            guarantor_count = db.execute(
                'SELECT COUNT(*) FROM loan_guarantors WHERE loan_id = ?',
                (new_loan['id'],),
            ).fetchone()[0]
            self.assertEqual(guarantor_count, 2)

    def test_registered_mobile_device_receives_push_when_notified(self):
        self.login_admin()
        with self.app.app_context():
            db = get_db()
            user = db.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
            db.execute('''
                INSERT INTO mobile_devices
                    (user_id, platform, push_token, device_name, enabled, last_seen_at)
                VALUES (?, 'android', 'ExpoPushToken[test-admin]', 'Admin Phone', 1, ?)
            ''', (user['id'], datetime.now()))
            db.commit()

        with patch.dict(os.environ, {'MOBILE_PUSH_SYNC': '1'}):
            with patch('mobile_push._post_expo_messages') as post_push:
                with self.app.app_context():
                    db = get_db()
                    from utils import notify
                    notify(db, user['id'], 'Mobile Alert', 'This is a push-enabled notification.', 'info', '/dashboard')
                    db.commit()
                post_push.assert_called_once()
                messages = post_push.call_args[0][0]
                self.assertEqual(messages[0]['to'], 'ExpoPushToken[test-admin]')
                self.assertEqual(messages[0]['title'], 'Mobile Alert')
                self.assertEqual(messages[0]['data']['action_url'], '/dashboard')

    def test_admin_can_view_test_and_revoke_mobile_device(self):
        self.login_admin()
        suffix = int(time.time() * 1000)
        email = f'mobile.device.admin.{suffix}@example.com'
        member_id = self.create_member()
        self.create_member_user(member_id, email=email)
        push_token = f'ExpoPushToken[admin-device-{suffix}]'
        with self.app.app_context():
            db = get_db()
            user = db.execute('SELECT * FROM users WHERE username = ?', (email,)).fetchone()
            db.execute('''
                INSERT INTO mobile_devices
                    (user_id, member_id, platform, push_token, device_name, enabled, last_seen_at)
                VALUES (?, ?, 'android', ?, 'Adeo Test Phone', 1, ?)
            ''', (user['id'], member_id, push_token, datetime.now()))
            device_id = db.execute(
                'SELECT id FROM mobile_devices WHERE push_token = ?', (push_token,)
            ).fetchone()['id']
            db.commit()

        settings = self.client.get('/settings')
        self.assertEqual(settings.status_code, 200)
        self.assertIn(b'Mobile Devices', settings.data)
        self.assertIn(b'Adeo Test Phone', settings.data)

        with patch.dict(os.environ, {'MOBILE_PUSH_SYNC': '1'}):
            with patch('mobile_push._post_expo_messages') as post_push:
                pushed = self.client.post(
                    f'/api/mobile_devices/{device_id}/test-push',
                    follow_redirects=False,
                )
                self.assertIn(pushed.status_code, (302, 303))
                post_push.assert_called_once()
                messages = post_push.call_args[0][0]
                self.assertEqual(messages[0]['to'], push_token)
                self.assertEqual(messages[0]['title'], 'CoopMS test notification')

        revoked = self.client.post(
            f'/api/mobile_devices/{device_id}/revoke',
            follow_redirects=False,
        )
        self.assertIn(revoked.status_code, (302, 303))
        with self.app.app_context():
            db = get_db()
            enabled = db.execute(
                'SELECT enabled FROM mobile_devices WHERE id = ?', (device_id,)
            ).fetchone()['enabled']
            self.assertEqual(enabled, 0)

    def test_admin_configured_password_policy_is_enforced_by_helper(self):
        with self.app.app_context():
            db = get_db()
            for key, value in (
                ('password_min_length', '10'),
                ('password_require_upper', '1'),
                ('password_require_lower', '1'),
                ('password_require_number', '1'),
                ('password_require_special', '1'),
            ):
                db.execute(
                    'INSERT INTO settings (key, value, description) VALUES (?, ?, ?) '
                    'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                    (key, value, f'Test {key}'),
                )
            db.commit()

            ok, errors = validate_password_strength('Password1', db)
            self.assertFalse(ok)
            self.assertIn('special character', ' '.join(errors))

            generated = generate_compliant_password(db)
            ok, errors = validate_password_strength(generated, db)
            self.assertTrue(ok, errors)

    def test_new_member_gets_portal_user_and_onboarding_email(self):
        self.login_admin()
        email = 'new.member.onboarding@example.com'
        with patch('blueprints.members.send_welcome_email') as welcome_email, \
                patch('blueprints.members.send_member_onboarding_email') as onboarding_email:
            response = self.client.post(
                '/members/add',
                data={
                    'first_name': 'New',
                    'last_name': 'Member',
                    'email': email,
                    'phone': '08000000999',
                    'monthly_savings': '12000',
                },
                follow_redirects=False,
            )
        self.assertIn(response.status_code, (302, 303))
        welcome_email.assert_called_once()
        onboarding_email.assert_called_once()

        with self.app.app_context():
            db = get_db()
            user = db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
            member = db.execute('SELECT * FROM members WHERE email = ?', (email,)).fetchone()
            self.assertIsNotNone(user)
            self.assertIsNotNone(member)
            self.assertEqual(user['role'], 'member')
            self.assertEqual(user['username'], email)
            self.assertEqual(user['must_change_password'], 1)
            setup_url = onboarding_email.call_args.args[3]
            self.assertIn('/setup-password/', setup_url)
            self.assertNotIn('password', setup_url.split('/setup-password/', 1)[-1].lower())

            token = urlparse(setup_url).path.rsplit('/', 1)[-1]
            token_row = db.execute('SELECT * FROM account_setup_tokens WHERE user_id = ?', (user['id'],)).fetchone()
            self.assertIsNotNone(token_row)
            self.assertIsNone(token_row['used_at'])

        setup = self.client.post(
            f'/setup-password/{token}',
            data={'new_password': 'SetupPass1!', 'confirm_password': 'SetupPass1!'},
            follow_redirects=False,
        )
        self.assertIn(setup.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            user = db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
            token_row = db.execute('SELECT * FROM account_setup_tokens WHERE user_id = ?', (user['id'],)).fetchone()
            self.assertEqual(user['must_change_password'], 0)
            self.assertTrue(check_password_hash(user['password_hash'], 'SetupPass1!'))
            self.assertIsNotNone(token_row['used_at'])

        reused = self.client.get(f'/setup-password/{token}', follow_redirects=False)
        self.assertIn(reused.status_code, (302, 303))

    def test_loan_type_restriction_across_application_channels(self):
        guarantors = [self.create_guarantor_member(f'TYPE-G{i}', f'type-g{i}@example.com', 'Guarantor')
                      for i in (1, 2)]
        for channel in ('admin', 'portal', 'mobile'):
            with self.subTest(channel=channel):
                email = f'type-{channel}@example.com'
                mid = self.create_guarantor_member(f'TYPE-{channel}', email, 'Applicant')
                self.create_member_user(mid, email)
                self.fund_member_savings(mid)
                with self.app.app_context():
                    db = get_db()
                    db.execute("""INSERT INTO loans
                        (loan_number, member_id, amount, purpose, tenure, interest_rate,
                         total_repayment, balance, status, date_applied)
                        VALUES (?, ?, 50000, ' regular ', 6, 11, 52000, 52000, 'active', '2024-01-01')""",
                        (f'TYPE-EXISTING-{channel}', mid))
                    db.commit()
                self.client = self.app.test_client()
                headers = {}
                if channel == 'admin':
                    self.login_admin()
                elif channel == 'portal':
                    self.login_member(email)
                else:
                    clear_login_attempts('mobile:127.0.0.1')
                    login = self.client.post('/api/mobile/login', json={'username': email, 'password': 'MemberPass1!'})
                    self.assertEqual(login.status_code, 200)
                    headers = {'Authorization': f"Bearer {login.get_json()['token']}"}
                payload = dict(member_id=str(mid), amount='50000', tenure='6',
                               payment_collateral_type='standing_order', signature_name='Applicant Guarantor',
                               accept_terms='1', data_processing_consent='1', repayment_schedule_accepted='1',
                               hr_affordability_consent='1', guarantors=[str(g) for g in guarantors],
                               guarantor_ids=guarantors)
                def submit(purpose):
                    data = {**payload, 'purpose': purpose}
                    if channel == 'mobile':
                        return self.client.post('/api/mobile/v1/loans/apply', json=data, headers=headers)
                    path = '/loans/apply' if channel == 'admin' else '/apply-loan-member'
                    return self.client.post(path, data=data)

                for purpose in ('Regular', 'Unrecognised product'):
                    submit(purpose)
                    with self.app.app_context():
                        self.assertEqual(get_db().execute('SELECT COUNT(*) FROM loans WHERE member_id = ?', (mid,)).fetchone()[0], 1)
                for purpose in ('Asset Purchase', 'School Fees'):
                    result = submit(purpose)
                    self.assertEqual(result.status_code, 201 if channel == 'mobile' else 302)
                    with self.app.app_context():
                        loan = get_db().execute('SELECT * FROM loans WHERE member_id = ? AND purpose = ?', (mid, purpose)).fetchone()
                        self.assertIsNotNone(loan)
                        self.assertEqual(loan['status'], 'pending')
                        self.assertEqual(float(loan['balance']), 0)
                        self.assertTrue(loan['approval_stage'])
                        self.assertFalse(loan['disbursement_date'])
                with self.app.app_context():
                    db = get_db()
                    db.execute('UPDATE loans SET balance = 0 WHERE loan_number = ?', (f'TYPE-EXISTING-{channel}',))
                    db.commit()
                submit('Regular')
                with self.app.app_context():
                    loan = get_db().execute("SELECT * FROM loans WHERE member_id = ? AND purpose = 'Regular'", (mid,)).fetchone()
                    self.assertIsNotNone(loan)
                    self.assertEqual(loan['status'], 'pending')

    def test_member_loan_application_requires_due_diligence_acknowledgements(self):
        member_id = self.create_member()
        self.create_member_user(member_id)
        self.fund_member_savings(member_id)
        guarantor_1 = self.create_guarantor_member('OOU/TEST/G001', 'g1@example.com', 'Grace')
        guarantor_2 = self.create_guarantor_member('OOU/TEST/G002', 'g2@example.com', 'George')
        self.login_member()

        response = self.client.post(
            '/apply-loan-member',
            data={
                'amount': '50000',
                'purpose': 'Regular',
                'tenure': '6',
                'payment_collateral_type': 'standing_order',
                'guarantors': [str(guarantor_1), str(guarantor_2)],
                'accept_terms': '1',
                'data_processing_consent': '1',
                'repayment_schedule_accepted': '1',
                'signature_name': 'Ada Audit',
            },
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            loan = db.execute(
                'SELECT * FROM loans WHERE member_id = ? ORDER BY id DESC',
                (member_id,),
            ).fetchone()
            self.assertIsNone(loan)

    def test_member_loan_application_stores_consent_and_schedule_snapshot(self):
        member_id = self.create_member()
        self.create_member_user(member_id)
        self.fund_member_savings(member_id)
        guarantor_1 = self.create_guarantor_member('OOU/TEST/G003', 'g3@example.com', 'Gina')
        guarantor_2 = self.create_guarantor_member('OOU/TEST/G004', 'g4@example.com', 'Gabriel')
        self.login_member()

        response = self.client.post(
            '/apply-loan-member',
            data={
                'amount': '50000',
                'purpose': 'Regular',
                'tenure': '6',
                'payment_collateral_type': 'standing_order',
                'guarantors': [str(guarantor_1), str(guarantor_2)],
                'hr_affordability_consent': '1',
                'data_processing_consent': '1',
                'repayment_schedule_accepted': '1',
                'accept_terms': '1',
                'signature_name': 'Ada Audit',
            },
            follow_redirects=False,
            environ_overrides={'REMOTE_ADDR': '203.0.113.44'},
        )
        self.assertIn(response.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            loan = db.execute(
                'SELECT * FROM loans WHERE member_id = ? ORDER BY id DESC',
                (member_id,),
            ).fetchone()
            self.assertIsNotNone(loan)
            self.assertEqual(loan['terms_accepted'], 1)
            self.assertEqual(loan['data_processing_consent'], 1)
            self.assertEqual(loan['credit_check_consent'], 0)
            self.assertEqual(loan['repayment_schedule_accepted'], 1)
            self.assertEqual(loan['bank_statement_status'], 'not_required')
            self.assertEqual(loan['payment_collateral_type'], 'standing_order')
            self.assertEqual(loan['payment_collateral_status'], 'pending')
            self.assertEqual(loan['consent_ip'], '203.0.113.44')
            self.assertEqual(loan['loan_applicant_type'], 'staff')
            self.assertEqual(loan['hr_affordability_consent'], 1)
            self.assertEqual(loan['hr_affordability_status'], 'pending')

            snapshot = json.loads(loan['repayment_schedule_snapshot'])
            self.assertEqual(snapshot['principal'], 50000)
            self.assertEqual(snapshot['purpose'], 'Regular')
            self.assertEqual(snapshot['tenure'], 6)
            self.assertEqual(len(snapshot['schedule']), 6)

    def test_member_can_withdraw_pending_loan_before_disbursement_only(self):
        suffix = int(time.time() * 1000)
        email = f'withdraw.{suffix}@example.com'
        loan_number = f"LOAN/WITHDRAW/{int(time.time() * 1000)}"
        active_loan_number = f"LOAN/WITHDRAW-ACTIVE/{int(time.time() * 1000)}"
        with self.app.app_context():
            db = get_db()
            db.execute('''
                INSERT INTO members
                    (member_number, employee_id, first_name, last_name, email,
                     phone, status, monthly_savings, total_savings, date_joined)
                VALUES (?, ?, 'Wendy', 'Withdraw', ?, '08000000088',
                        'active', 15000, 100000, '2024-01-01')
            ''', (f'OOU/TEST/W{suffix}', f'EMP-W{suffix}', email))
            member_id = db.execute(
                'SELECT id FROM members WHERE email = ?', (email,)
            ).fetchone()['id']
            db.execute('''
                INSERT INTO loans
                    (loan_number, member_id, amount, purpose, tenure, interest_rate,
                     interest_method, total_repayment, balance, status, approval_stage,
                     date_applied)
                VALUES (?, ?, 50000, 'Regular', 6, 11, 'reducing_annual',
                        52000, 52000, 'pending', 'secretary', '2026-07-20')
            ''', (loan_number, member_id))
            pending_loan_id = db.execute(
                'SELECT id FROM loans WHERE loan_number = ?', (loan_number,)
            ).fetchone()['id']
            db.execute(
                "INSERT INTO loan_guarantors (loan_id, member_id, status) VALUES (?, ?, 'pending')",
                (pending_loan_id, member_id)
            )
            db.execute('''
                INSERT INTO loans
                    (loan_number, member_id, amount, purpose, tenure, interest_rate,
                     interest_method, total_repayment, balance, status, approval_stage,
                     disbursement_date, date_applied)
                VALUES (?, ?, 50000, 'Regular', 6, 11, 'reducing_annual',
                        52000, 52000, 'active', 'approved', '2026-07-21', '2026-07-20')
            ''', (active_loan_number, member_id))
            active_loan_id = db.execute(
                'SELECT id FROM loans WHERE loan_number = ?', (active_loan_number,)
            ).fetchone()['id']
            db.commit()

        self.create_member_user(member_id, email=email)
        self.login_member(email=email)
        response = self.client.post(
            f'/loan-detail/{pending_loan_id}/withdraw',
            data={'withdrawal_reason': 'I want to revise the amount'},
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))

        blocked = self.client.post(
            f'/loan-detail/{active_loan_id}/withdraw',
            data={'withdrawal_reason': 'Too late'},
            follow_redirects=False,
        )
        self.assertIn(blocked.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            withdrawn = db.execute('SELECT * FROM loans WHERE id = ?', (pending_loan_id,)).fetchone()
            self.assertEqual(withdrawn['status'], 'withdrawn')
            self.assertEqual(withdrawn['approval_stage'], 'withdrawn')
            self.assertIsNotNone(withdrawn['withdrawn_at'])
            self.assertEqual(withdrawn['withdrawal_reason'], 'I want to revise the amount')
            trail = db.execute(
                "SELECT * FROM loan_approvals WHERE loan_id = ? AND action = 'withdrawn'",
                (pending_loan_id,)
            ).fetchone()
            self.assertIsNotNone(trail)
            guarantor = db.execute(
                'SELECT status FROM loan_guarantors WHERE loan_id = ?',
                (pending_loan_id,)
            ).fetchone()
            self.assertEqual(guarantor['status'], 'withdrawn')
            active = db.execute('SELECT * FROM loans WHERE id = ?', (active_loan_id,)).fetchone()
            self.assertEqual(active['status'], 'active')

    def test_member_can_request_monthly_savings_change_from_savings_page(self):
        suffix = int(time.time() * 1000)
        email = f'savings.change.{suffix}@example.com'
        member_number = f'OOU/TEST/SC{suffix}'
        with self.app.app_context():
            db = get_db()
            db.execute('''
                INSERT INTO members
                    (member_number, employee_id, first_name, last_name, email,
                     phone, status, monthly_savings, total_savings, date_joined)
                VALUES (?, ?, 'Sade', 'Savings', ?, '08000000089',
                        'active', 15000, 250000, '2024-01-01')
            ''', (member_number, f'EMP-SC{suffix}', email))
            db.commit()
            member_id = db.execute(
                'SELECT id FROM members WHERE member_number = ?', (member_number,)
            ).fetchone()['id']

        self.create_member_user(member_id, email=email)
        self.login_member(email=email)

        page = self.client.get('/my-savings')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Request Update', page.data)
        self.assertIn(b'Update salary deduction', page.data)

        response = self.client.post(
            '/change-savings-request',
            data={'new_amount': '25000', 'reason': 'Salary deduction increase'},
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))

        duplicate = self.client.post(
            '/change-savings-request',
            data={'new_amount': '30000', 'reason': 'Second request'},
            follow_redirects=True,
        )
        self.assertEqual(duplicate.status_code, 200)

        with self.app.app_context():
            db = get_db()
            rows = db.execute(
                'SELECT * FROM savings_change_requests WHERE member_id = ?',
                (member_id,),
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['status'], 'pending')
            self.assertAlmostEqual(float(rows[0]['current_amount']), 15000.0)
            self.assertAlmostEqual(float(rows[0]['requested_amount']), 25000.0)
            self.assertEqual(rows[0]['reason'], 'Salary deduction increase')
            audit_row = db.execute(
                "SELECT 1 FROM audit_log WHERE action = 'SAVINGS_CHANGE_REQUEST' "
                "AND description LIKE ?",
                (f'%{member_id}%',),
            ).fetchone()
            self.assertIsNotNone(audit_row)

    def test_non_staff_loan_application_still_requires_bank_and_credit_acknowledgements(self):
        member_id, email = self.create_non_staff_member()
        self.create_member_user(member_id, email=email)
        self.fund_member_savings(member_id)
        guarantor_1 = self.create_guarantor_member('OOU/TEST/G005', 'g5@example.com', 'Gideon')
        guarantor_2 = self.create_guarantor_member('OOU/TEST/G006', 'g6@example.com', 'Gloria')
        self.login_member(email=email)

        response = self.client.post(
            '/apply-loan-member',
            data={
                'amount': '50000',
                'purpose': 'Regular',
                'tenure': '6',
                'payment_collateral_type': 'post_dated_cheques',
                'guarantors': [str(guarantor_1), str(guarantor_2)],
                'data_processing_consent': '1',
                'repayment_schedule_accepted': '1',
                'accept_terms': '1',
                'signature_name': 'Nora Nonstaff',
            },
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            loan = db.execute(
                'SELECT * FROM loans WHERE member_id = ? ORDER BY id DESC',
                (member_id,),
            ).fetchone()
            self.assertIsNone(loan)

    def test_final_loan_approval_requires_completed_due_diligence(self):
        self.login_admin()
        with self.app.app_context():
            db = get_db()
            db.execute('''
                INSERT INTO members
                    (member_number, employee_id, first_name, last_name, email,
                     phone, status, monthly_savings, total_savings, date_joined)
                VALUES
                    ('OOU/TEST/DUE1', 'EMP-DUE1', 'Dara', 'Due',
                     'dara.due@example.com', '08000000021', 'active',
                     15000, 100000, '2024-01-01')
            ''')
            member_id = db.execute(
                "SELECT id FROM members WHERE member_number = 'OOU/TEST/DUE1'"
            ).fetchone()['id']
            db.execute('''
                INSERT INTO loans
                    (loan_number, member_id, amount, purpose, tenure, interest_rate,
                     interest_method, total_repayment, balance, status, approval_stage,
                     loan_applicant_type, hr_affordability_consent, hr_affordability_status,
                     payment_collateral_type, payment_collateral_status, date_applied)
                VALUES
                    ('LOAN/DUE/0001', ?, 50000, 'Regular', 6, 11,
                     'reducing_annual', 52000, 52000, 'pending', 'president',
                     'staff', 1, 'pending', 'standing_order', 'pending', '2026-07-20')
            ''', (member_id,))
            db.commit()
            loan_id = db.execute(
                "SELECT id FROM loans WHERE loan_number = 'LOAN/DUE/0001'"
            ).fetchone()['id']

        blocked = self.client.post(
            f'/loans/{loan_id}/act',
            data={'action': 'approve'},
            follow_redirects=False,
        )
        self.assertIn(blocked.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            loan = db.execute('SELECT * FROM loans WHERE id = ?', (loan_id,)).fetchone()
            self.assertEqual(loan['status'], 'pending')
            self.assertEqual(loan['approval_stage'], 'president')

        verified = self.client.post(
            f'/loans/{loan_id}/due-diligence',
            data={
                'hr_affordability_confirmed': '1',
                'payment_collateral_verified': '1',
                'comment': 'HR confirmed salary deduction capacity.',
            },
            follow_redirects=False,
        )
        self.assertIn(verified.status_code, (302, 303))

        approved = self.client.post(
            f'/loans/{loan_id}/act',
            data={'action': 'approve'},
            follow_redirects=False,
        )
        self.assertIn(approved.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            loan = db.execute('SELECT * FROM loans WHERE id = ?', (loan_id,)).fetchone()
            self.assertEqual(loan['hr_affordability_status'], 'confirmed')
            self.assertEqual(loan['payment_collateral_status'], 'verified')
            self.assertEqual(loan['status'], 'active')
            self.assertEqual(loan['approval_stage'], 'approved')
            trail = db.execute(
                "SELECT * FROM loan_approvals WHERE loan_id = ? AND stage = 'due_diligence'",
                (loan_id,),
            ).fetchone()
            self.assertIsNotNone(trail)

            journal = db.execute(
                "SELECT id FROM journal_entries WHERE reference = 'LOAN/DUE/0001'"
            ).fetchone()
            if journal:
                db.execute('DELETE FROM journal_lines WHERE entry_id = ?', (journal['id'],))
                db.execute('DELETE FROM journal_entries WHERE id = ?', (journal['id'],))
            db.execute("DELETE FROM revenue WHERE source = 'Loan LOAN/DUE/0001'")
            db.execute('DELETE FROM loan_approvals WHERE loan_id = ?', (loan_id,))
            db.execute('DELETE FROM loans WHERE id = ?', (loan_id,))
            db.execute('DELETE FROM members WHERE id = ?', (member_id,))
            db.commit()

    def test_loan_insurance_posts_to_payable_not_income(self):
        """The 1% loan insurance is money held for the insurer — a pass-through
        liability, not income. On disbursement it must credit Insurance Payable
        (2110), leave Fee Income to the application fee only, and never be logged
        in the revenue table."""
        from ledger import INSURANCE_PAYABLE, FEE_INCOME
        self.login_admin()
        with self.app.app_context():
            db = get_db()
            db.execute('''
                INSERT INTO members
                    (member_number, employee_id, first_name, last_name, email,
                     phone, status, monthly_savings, total_savings, date_joined)
                VALUES
                    ('OOU/TEST/INS1', 'EMP-INS1', 'Ivy', 'Insure',
                     'ivy.insure@example.com', '08000000031', 'active',
                     15000, 100000, '2024-01-01')
            ''')
            member_id = db.execute(
                "SELECT id FROM members WHERE member_number = 'OOU/TEST/INS1'"
            ).fetchone()['id']
            db.execute('''
                INSERT INTO loans
                    (loan_number, member_id, amount, purpose, tenure, interest_rate,
                     interest_method, total_repayment, balance, status, approval_stage,
                     loan_applicant_type, hr_affordability_consent, hr_affordability_status,
                     payment_collateral_type, payment_collateral_status, date_applied)
                VALUES
                    ('LOAN/INS/0001', ?, 50000, 'Regular', 6, 11,
                     'reducing_annual', 52000, 52000, 'pending', 'president',
                     'staff', 1, 'pending', 'standing_order', 'pending', '2026-07-20')
            ''', (member_id,))
            db.commit()
            loan_id = db.execute(
                "SELECT id FROM loans WHERE loan_number = 'LOAN/INS/0001'"
            ).fetchone()['id']

        self.client.post(
            f'/loans/{loan_id}/due-diligence',
            data={
                'hr_affordability_confirmed': '1',
                'payment_collateral_verified': '1',
                'comment': 'HR confirmed salary deduction capacity.',
            },
            follow_redirects=False,
        )
        approved = self.client.post(
            f'/loans/{loan_id}/act',
            data={'action': 'approve'},
            follow_redirects=False,
        )
        self.assertIn(approved.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            entry = db.execute(
                "SELECT id FROM journal_entries WHERE reference = 'LOAN/INS/0001'"
            ).fetchone()
            self.assertIsNotNone(entry, 'disbursement should post a GL entry')
            lines = db.execute(
                "SELECT account_code, debit, credit FROM journal_lines WHERE entry_id = ?",
                (entry['id'],)
            ).fetchall()
            by_code = {row['account_code']: row for row in lines}
            # Insurance (1% of 50000 = 500) is a payable, not income.
            self.assertIn(INSURANCE_PAYABLE, by_code)
            self.assertAlmostEqual(by_code[INSURANCE_PAYABLE]['credit'], 500, places=2)
            # Fee Income carries only the application fee (500), not insurance + fee.
            self.assertAlmostEqual(by_code[FEE_INCOME]['credit'], 500, places=2)
            # Insurance is not booked as revenue.
            rev = db.execute(
                "SELECT COUNT(*) AS c FROM revenue "
                "WHERE category = 'Loan Insurance' AND source = 'Loan LOAN/INS/0001'"
            ).fetchone()
            self.assertEqual(rev['c'], 0)
            # The whole entry still balances.
            self.assertAlmostEqual(sum(r['debit'] for r in lines),
                                   sum(r['credit'] for r in lines), places=2)

            db.execute('DELETE FROM journal_lines WHERE entry_id = ?', (entry['id'],))
            db.execute('DELETE FROM journal_entries WHERE id = ?', (entry['id'],))
            db.execute("DELETE FROM revenue WHERE source = 'Loan LOAN/INS/0001'")
            db.execute('DELETE FROM loan_approvals WHERE loan_id = ?', (loan_id,))
            db.execute('DELETE FROM loans WHERE id = ?', (loan_id,))
            db.execute('DELETE FROM members WHERE id = ?', (member_id,))
            db.commit()

    def test_retry_campaign_resumes_stranded_queued_recipients(self):
        """A worker restart/redeploy can strand a campaign's recipients at
        'queued' with the campaign stuck 'sending'. The Retry route must re-run
        the sender so those recipients leave 'queued' and the campaign reaches a
        terminal state — without re-sending anyone already marked 'sent'."""
        from datetime import datetime
        from database import last_insert_id
        self.login_admin()
        with self.app.app_context():
            db = get_db()
            db.execute('''
                INSERT INTO members
                    (member_number, employee_id, first_name, last_name, email,
                     phone, status, monthly_savings, total_savings, date_joined)
                VALUES
                    ('OOU/TEST/CMM1', 'EMP-CMM1', 'Cam', 'Paign',
                     'cam.paign@example.com', '08000000041', 'active',
                     15000, 100000, '2024-01-01')
            ''')
            member_id = db.execute(
                "SELECT id FROM members WHERE member_number = 'OOU/TEST/CMM1'"
            ).fetchone()['id']
            db.execute('''
                INSERT INTO communication_campaigns
                    (title, audience, channel, subject, body, status,
                     recipient_count, created_by, created_at)
                VALUES ('Stuck campaign', 'active', 'email', 'Hi {first_name}',
                        'Hello', 'sending', 2, 1, ?)
            ''', (datetime.now(),))
            campaign_id = last_insert_id(db)
            # One already delivered, one stranded at 'queued' by a crashed worker.
            db.execute('''
                INSERT INTO communication_recipients
                    (campaign_id, member_id, channel, destination, status, error, created_at)
                VALUES (?, ?, 'email', 'cam.paign@example.com', 'sent', '', ?)
            ''', (campaign_id, member_id, datetime.now()))
            db.execute('''
                INSERT INTO communication_recipients
                    (campaign_id, member_id, channel, destination, status, error, created_at)
                VALUES (?, ?, 'email', 'cam.paign@example.com', 'queued', '', ?)
            ''', (campaign_id, member_id, datetime.now()))
            db.commit()

        resp = self.client.post(f'/communications/{campaign_id}/retry',
                                follow_redirects=False)
        self.assertIn(resp.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            stuck = db.execute(
                "SELECT COUNT(*) AS c FROM communication_recipients "
                "WHERE campaign_id = ? AND status = 'queued'", (campaign_id,)
            ).fetchone()['c']
            self.assertEqual(stuck, 0, 'retry should drain all queued recipients')
            campaign = db.execute(
                "SELECT status FROM communication_campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
            self.assertNotEqual(campaign['status'], 'sending',
                                'campaign should reach a terminal state after retry')

            db.execute('DELETE FROM communication_recipients WHERE campaign_id = ?', (campaign_id,))
            db.execute('DELETE FROM communication_campaigns WHERE id = ?', (campaign_id,))
            db.execute('DELETE FROM members WHERE id = ?', (member_id,))
            db.commit()

    def test_portal_link_does_not_raise_without_request_context(self):
        """The background campaign sender runs with only an app context. Building
        an external URL there used to raise (no request context / SERVER_NAME),
        crashing the send loop and stranding recipients at 'queued'. _portal_link
        must degrade gracefully instead of raising."""
        from blueprints import communications as comm
        with self.app.app_context():   # app context only — no request context
            link = comm._portal_link()
            self.assertIsInstance(link, str)

    def test_notifications_page_renders_with_string_dates(self):
        """DB datetimes reach templates as strings (see database._coerce), so the
        notifications page calling .strftime on created_at 500'd once any
        notification existed. The page must render (200) with a notification
        present."""
        from datetime import datetime
        self.login_admin()
        with self.app.app_context():
            db = get_db()
            admin_id = db.execute(
                "SELECT id FROM users WHERE username = 'admin'"
            ).fetchone()['id']
            db.execute('''
                INSERT INTO notifications
                    (user_id, title, message, notification_type, is_read, action_url, created_at)
                VALUES (?, 'Test notice', 'Body text', 'info', 0, '/dashboard', ?)
            ''', (admin_id, datetime.now()))
            db.commit()

        resp = self.client.get('/notifications', follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Test notice', resp.data)

        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM notifications WHERE user_id = ? AND title = 'Test notice'",
                       (admin_id,))
            db.commit()

    def test_member_share_capital_sums_savings_rows(self):
        """member_share_capital totals the carved-out share_capital portion per
        member (the figure shown on the dashboard/statement), independent of the
        deposit balance."""
        from datetime import datetime
        from utils import member_share_capital, member_savings_balance
        with self.app.app_context():
            db = get_db()
            db.execute('''
                INSERT INTO members
                    (member_number, employee_id, first_name, last_name, email,
                     phone, status, monthly_savings, total_savings, date_joined)
                VALUES ('OOU/TEST/SCAP1', 'EMP-SCAP1', 'Sha', 'Capital',
                        'sha.capital@example.com', '08000000051', 'active',
                        15000, 0, '2024-01-01')
            ''')
            member_id = db.execute(
                "SELECT id FROM members WHERE member_number = 'OOU/TEST/SCAP1'"
            ).fetchone()['id']
            # Two contributions: deposit 95% in `amount`, 5% in `share_capital`.
            db.execute("INSERT INTO savings (member_id, amount, share_capital, month) "
                       "VALUES (?, 95000, 5000, '2026-01')", (member_id,))
            db.execute("INSERT INTO savings (member_id, amount, share_capital, month) "
                       "VALUES (?, 190000, 10000, '2026-02')", (member_id,))
            db.commit()

            self.assertAlmostEqual(member_share_capital(db, member_id), 15000, places=2)
            # Savings balance stays the deposit portion only.
            self.assertAlmostEqual(member_savings_balance(db, member_id), 285000, places=2)

            db.execute('DELETE FROM savings WHERE member_id = ?', (member_id,))
            db.execute('DELETE FROM members WHERE id = ?', (member_id,))
            db.commit()

    def test_member_dashboard_and_statement_render_share_capital(self):
        """The member dashboard and savings statement must render (200) and
        surface the share-capital figure so members see the 5% wasn't lost."""
        email = 'scap.render@example.com'
        with self.app.app_context():
            db = get_db()
            db.execute('''
                INSERT INTO members
                    (member_number, employee_id, first_name, last_name, email,
                     phone, status, monthly_savings, total_savings, date_joined)
                VALUES ('OOU/TEST/SCAP2', 'EMP-SCAP2', 'Ren', 'Der', ?,
                        '08000000052', 'active', 15000, 0, '2024-01-01')
            ''', (email,))
            member_id = db.execute(
                "SELECT id FROM members WHERE member_number = 'OOU/TEST/SCAP2'"
            ).fetchone()['id']
            db.execute("INSERT INTO savings (member_id, amount, share_capital, month) "
                       "VALUES (?, 95000, 5000, '2026-01')", (member_id,))
            db.commit()
        self.create_member_user(member_id, email=email)
        self.login_member(email=email)

        dash = self.client.get('/member/portal')
        self.assertEqual(dash.status_code, 200)
        self.assertIn(b'Share Capital', dash.data)

        stmt = self.client.get('/my-savings')
        self.assertEqual(stmt.status_code, 200)
        self.assertIn(b'Share Capital', stmt.data)

        with self.app.app_context():
            db = get_db()
            db.execute('DELETE FROM savings WHERE member_id = ?', (member_id,))
            db.execute('DELETE FROM users WHERE email = ?', (email,))
            db.execute('DELETE FROM members WHERE id = ?', (member_id,))
            db.commit()

    def test_cooperative_logo_persists_in_database_on_upload(self):
        """An uploaded logo is stored in the DB as a compact data URI so it
        survives container rebuilds (static/uploads is ephemeral). The logo_src
        filter renders data URIs directly and legacy paths via static."""
        from io import BytesIO
        from PIL import Image
        from app import _logo_src
        self.login_admin()

        buf = BytesIO()
        Image.new('RGB', (300, 120), (8, 43, 102)).save(buf, format='PNG')
        buf.seek(0)
        resp = self.client.post(
            '/settings/update',
            data={'coop_logo': (buf, 'logo.png')},
            content_type='multipart/form-data',
            follow_redirects=False,
        )
        self.assertIn(resp.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            row = db.execute("SELECT value FROM settings WHERE key = 'coop_logo'").fetchone()
            self.assertIsNotNone(row, 'coop_logo should be saved')
            self.assertTrue(
                str(row['value']).startswith('data:image/png;base64,'),
                'logo must be stored in the database as a data URI',
            )
            db.execute("DELETE FROM settings WHERE key = 'coop_logo'")
            db.commit()

        # Filter: data URIs pass through untouched; legacy paths resolve via static.
        self.assertTrue(_logo_src('data:image/png;base64,AAAA').startswith('data:'))
        self.assertEqual(_logo_src(''), '')
        with self.app.test_request_context():
            self.assertIn('uploads/x.png', _logo_src('uploads/x.png'))

    def test_upcoming_events_excludes_past_and_undated(self):
        """The members' announcements banner is date-driven: past and undated
        events never appear; only today-or-future dated events do."""
        from datetime import datetime, timedelta
        from blueprints.governance import upcoming_events
        with self.app.app_context():
            db = get_db()
            past = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
            future = (datetime.now() + timedelta(days=3)).strftime('%Y-%m-%d')
            db.execute("INSERT INTO events (title, event_type, event_date, is_active, created_by) "
                       "VALUES ('PAST EV', 'general', ?, 1, 1)", (past,))
            db.execute("INSERT INTO events (title, event_type, event_date, is_active, created_by) "
                       "VALUES ('UNDATED EV', 'announcement', NULL, 1, 1)")
            db.execute("INSERT INTO events (title, event_type, event_date, is_active, created_by) "
                       "VALUES ('FUTURE EV', 'general', ?, 1, 1)", (future,))
            db.commit()
            titles = {e['title'] for e in upcoming_events(db, limit=20)}
            self.assertIn('FUTURE EV', titles)
            self.assertNotIn('PAST EV', titles)
            self.assertNotIn('UNDATED EV', titles)
            db.execute("DELETE FROM events WHERE title IN ('PAST EV', 'UNDATED EV', 'FUTURE EV')")
            db.commit()

    def test_meeting_reminders_and_calendar(self):
        """A meeting due tomorrow gets a one-time reminder (idempotent), and the
        calendar view renders it."""
        from datetime import datetime, timedelta
        from database import last_insert_id
        self.login_admin()
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO events (title, event_type, event_date, is_active, created_by) "
                       "VALUES ('Reminder Test Mtg', 'general', ?, 1, 1)", (tomorrow,))
            event_id = last_insert_id(db)
            db.commit()

        resp = self.client.post('/governance/reminders/run', follow_redirects=False)
        self.assertIn(resp.status_code, (302, 303))
        with self.app.app_context():
            db = get_db()
            ev = db.execute("SELECT reminder_sent_at FROM events WHERE id = ?", (event_id,)).fetchone()
            self.assertIsNotNone(ev['reminder_sent_at'], 'reminder should mark the event')

        # A second run must not re-remind (reminder_sent_at is set).
        self.client.post('/governance/reminders/run', follow_redirects=False)

        cal = self.client.get(f'/events/calendar?year={tomorrow[:4]}&month={int(tomorrow[5:7])}')
        self.assertEqual(cal.status_code, 200)
        self.assertIn(b'Reminder Test Mtg', cal.data)

        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM events WHERE id = ?", (event_id,))
            db.commit()

    def test_meeting_create_rsvp_and_attendance(self):
        """A meeting is created with the new detail fields; a member RSVPs; the
        manager records attendance for the register (AGM quorum)."""
        self.login_admin()
        resp = self.client.post('/governance/events/add', data={
            'title': 'Test AGM 2026', 'event_type': 'agm', 'event_date': '2026-12-01',
            'start_time': '10:00', 'end_time': '12:00', 'location': 'Main Hall',
            'agenda': '1. Opening 2. Reports', 'description': 'Annual meeting',
        }, follow_redirects=False)   # no send_invite → no email dependency
        self.assertIn(resp.status_code, (302, 303))
        with self.app.app_context():
            db = get_db()
            ev = db.execute("SELECT * FROM events WHERE title = 'Test AGM 2026' ORDER BY id DESC").fetchone()
            self.assertIsNotNone(ev)
            event_id = ev['id']
            self.assertEqual(ev['start_time'], '10:00')
            self.assertEqual(ev['event_type'], 'agm')

        detail = self.client.get(f'/events/{event_id}')
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b'Test AGM 2026', detail.data)
        self.assertIn(b'Attendance register', detail.data)   # manager view

        member_id = self.create_member()
        self.create_member_user(member_id)
        self.login_member()
        r = self.client.post(f'/events/{event_id}/rsvp', data={'response': 'attending'},
                             follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        with self.app.app_context():
            db = get_db()
            row = db.execute("SELECT response FROM event_rsvps WHERE event_id = ? AND member_id = ?",
                             (event_id, member_id)).fetchone()
            self.assertEqual(row['response'], 'attending')

        self.login_admin()
        a = self.client.post(f'/governance/events/{event_id}/attendance',
                             data={'attended': [str(member_id)]}, follow_redirects=False)
        self.assertIn(a.status_code, (302, 303))
        with self.app.app_context():
            db = get_db()
            att = db.execute("SELECT attended FROM event_rsvps WHERE event_id = ? AND member_id = ?",
                             (event_id, member_id)).fetchone()
            self.assertEqual(att['attended'], 1)
            db.execute("DELETE FROM event_rsvps WHERE event_id = ?", (event_id,))
            db.execute("DELETE FROM events WHERE id = ?", (event_id,))
            db.commit()

    def test_officer_cannot_approve_own_loan(self):
        """Separation of duties: an officer (even an admin) who is the loan's
        applicant must not be able to approve/advance their own loan."""
        from datetime import datetime
        from database import last_insert_id
        email = 'self.approver@example.com'
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO users (username, password_hash, role, full_name, email, is_active, must_change_password) "
                "VALUES ('selfadmin', ?, 'admin', 'Self Admin', ?, 1, 0)",
                (generate_password_hash('SelfAdmin1!'), email))
            db.execute(
                "INSERT INTO members (member_number, first_name, last_name, email, phone, status, "
                "monthly_savings, total_savings, date_joined) "
                "VALUES ('OOU/TEST/SELF', 'Self', 'Approver', ?, '08000000061', 'active', 15000, 100000, '2020-01-01')",
                (email,))
            member_id = db.execute("SELECT id FROM members WHERE member_number = 'OOU/TEST/SELF'").fetchone()['id']
            db.execute(
                "INSERT INTO loans (loan_number, member_id, amount, purpose, tenure, interest_rate, "
                "interest_method, total_repayment, balance, status, approval_stage, date_applied) "
                "VALUES ('LOAN/SELF/0001', ?, 100000, 'Regular', 6, 11, 'reducing_annual', 104000, 104000, "
                "'pending', 'secretary', ?)", (member_id, datetime.now()))
            loan_id = db.execute("SELECT id FROM loans WHERE loan_number = 'LOAN/SELF/0001'").fetchone()['id']
            db.commit()

        self.client.post('/login', data={'username': 'selfadmin', 'password': 'SelfAdmin1!'},
                         follow_redirects=False)
        resp = self.client.post(f'/loans/{loan_id}/act', data={'action': 'approve'}, follow_redirects=True)
        self.assertIn(b'your own loan', resp.data)

        with self.app.app_context():
            db = get_db()
            loan = db.execute("SELECT status, approval_stage FROM loans WHERE id = ?", (loan_id,)).fetchone()
            self.assertEqual(loan['status'], 'pending')            # not activated
            self.assertEqual(loan['approval_stage'], 'secretary')  # stage did not advance
            db.execute("DELETE FROM loans WHERE id = ?", (loan_id,))
            db.execute("DELETE FROM members WHERE id = ?", (member_id,))
            db.execute("DELETE FROM users WHERE username = 'selfadmin'")
            db.commit()

    def test_open_notification_marks_read_and_redirects(self):
        """Clicking a notification (GET /notification/<id>) marks it read and
        redirects to its action target — the route was missing (404 before)."""
        from datetime import datetime
        from database import last_insert_id
        self.login_admin()
        with self.app.app_context():
            db = get_db()
            admin_id = db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()['id']
            db.execute("INSERT INTO notifications (user_id, title, message, notification_type, is_read, action_url, created_at) "
                       "VALUES (?, 'Go', 'Body', 'info', 0, '/dashboard', ?)", (admin_id, datetime.now()))
            notif_id = last_insert_id(db)
            db.execute("INSERT INTO notifications (user_id, title, message, notification_type, is_read, action_url, created_at) "
                       "VALUES (?, 'Evil', 'Body', 'info', 0, '//evil.example.com', ?)", (admin_id, datetime.now()))
            evil_id = last_insert_id(db)
            db.commit()

        resp = self.client.get(f'/notification/{notif_id}', follow_redirects=False)
        self.assertIn(resp.status_code, (302, 303))
        self.assertIn('/dashboard', resp.headers.get('Location', ''))

        # Open-redirect guard: a protocol-relative target falls back to the list.
        evil = self.client.get(f'/notification/{evil_id}', follow_redirects=False)
        self.assertNotIn('evil.example.com', evil.headers.get('Location', ''))

        with self.app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT is_read FROM notifications WHERE id = ?", (notif_id,)).fetchone()['is_read'], 1)
            db.execute("DELETE FROM notifications WHERE id IN (?, ?)", (notif_id, evil_id))
            db.commit()

    def test_admin_can_resend_and_revoke_setup_links(self):
        self.login_admin()
        email = 'resend.setup@example.com'
        with self.app.app_context():
            db = get_db()
            existing = db.execute('SELECT id FROM users WHERE username = ?', (email,)).fetchone()
            if existing:
                user_id = existing['id']
            else:
                db.execute('''
                    INSERT INTO users
                        (username, password_hash, role, full_name, email,
                         is_active, must_change_password, created_at)
                    VALUES (?, ?, 'member', 'Resend Setup', ?, 1, 1, CURRENT_TIMESTAMP)
                ''', (email, generate_password_hash('UnusedPass1!'), email))
                db.commit()
                user_id = db.execute('SELECT id FROM users WHERE username = ?', (email,)).fetchone()['id']
            db.execute('''
                INSERT INTO account_setup_tokens (user_id, token_hash, purpose, expires_at)
                VALUES (?, 'old-token-hash-for-resend-test', 'member_onboarding', '2099-01-01 00:00:00')
            ''', (user_id,))
            db.commit()

        with patch('blueprints.admin_panel.send_member_onboarding_email') as onboarding_email:
            response = self.client.post(f'/api/resend_setup_link/{user_id}', follow_redirects=False)
        self.assertIn(response.status_code, (302, 303))
        onboarding_email.assert_called_once()
        self.assertIn('/setup-password/', onboarding_email.call_args.args[3])

        with self.app.app_context():
            db = get_db()
            rows = db.execute(
                'SELECT * FROM account_setup_tokens WHERE user_id = ? ORDER BY id',
                (user_id,)
            ).fetchall()
            self.assertGreaterEqual(len(rows), 2)
            self.assertIsNotNone(rows[0]['used_at'])
            self.assertIsNone(rows[-1]['used_at'])

        revoke = self.client.post(f'/api/revoke_setup_links/{user_id}', follow_redirects=False)
        self.assertIn(revoke.status_code, (302, 303))
        with self.app.app_context():
            db = get_db()
            open_links = db.execute(
                'SELECT COUNT(*) FROM account_setup_tokens WHERE user_id = ? AND used_at IS NULL',
                (user_id,)
            ).fetchone()[0]
            self.assertEqual(open_links, 0)

    def test_admin_can_bulk_send_pending_setup_links(self):
        self.login_admin()
        users = [
            ('bulk.pending.1@example.com', 'Bulk Pending One', 'bulk.pending.1@example.com', 1, 1),
            ('bulk.pending.2@example.com', 'Bulk Pending Two', 'bulk.pending.2@example.com', 1, 1),
            ('bulk.completed@example.com', 'Bulk Completed', 'bulk.completed@example.com', 1, 0),
            ('bulk.inactive@example.com', 'Bulk Inactive', 'bulk.inactive@example.com', 0, 1),
            ('bulk.noemail@example.com', 'Bulk No Email', '', 1, 1),
        ]
        with self.app.app_context():
            db = get_db()
            for username, full_name, email, is_active, must_change in users:
                db.execute('DELETE FROM account_setup_tokens WHERE user_id IN (SELECT id FROM users WHERE username = ?)', (username,))
                db.execute('DELETE FROM users WHERE username = ?', (username,))
                db.execute('''
                    INSERT INTO users
                        (username, password_hash, role, full_name, email,
                         is_active, must_change_password, created_at)
                    VALUES (?, ?, 'member', ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', (
                    username,
                    generate_password_hash('UnusedPass1!'),
                    full_name,
                    email,
                    is_active,
                    must_change,
                ))
            db.commit()

        with patch('blueprints.admin_panel.send_member_onboarding_email') as onboarding_email:
            response = self.client.post('/api/bulk_send_setup_links', follow_redirects=False)

        self.assertIn(response.status_code, (302, 303))
        self.assertEqual(onboarding_email.call_count, 2)
        recipients = {call.args[0] for call in onboarding_email.call_args_list}
        self.assertEqual(recipients, {'bulk.pending.1@example.com', 'bulk.pending.2@example.com'})

        with self.app.app_context():
            db = get_db()
            token_counts = {
                row['username']: row['token_count']
                for row in db.execute('''
                    SELECT u.username, COUNT(t.id) AS token_count
                    FROM users u
                    LEFT JOIN account_setup_tokens t ON t.user_id = u.id AND t.used_at IS NULL
                    WHERE u.username LIKE 'bulk.%@example.com'
                    GROUP BY u.username
                ''').fetchall()
            }
            self.assertEqual(token_counts['bulk.pending.1@example.com'], 1)
            self.assertEqual(token_counts['bulk.pending.2@example.com'], 1)
            self.assertEqual(token_counts['bulk.completed@example.com'], 0)
            self.assertEqual(token_counts['bulk.inactive@example.com'], 0)
            self.assertEqual(token_counts['bulk.noemail@example.com'], 0)

    def test_user_can_request_and_complete_password_reset(self):
        email = 'reset.member@example.com'
        username = 'reset.member'
        with self.app.app_context():
            db = get_db()
            db.execute('DELETE FROM account_setup_tokens WHERE user_id IN (SELECT id FROM users WHERE username = ?)', (username,))
            db.execute('DELETE FROM users WHERE username = ?', (username,))
            db.execute('''
                INSERT INTO users
                    (username, password_hash, role, full_name, email,
                     is_active, must_change_password, created_at)
                VALUES (?, ?, 'member', 'Reset Member', ?, 1, 0, CURRENT_TIMESTAMP)
            ''', (username, generate_password_hash('OldPass123'), email))
            db.commit()

        with patch('blueprints.auth.send_password_reset_email', return_value=True) as reset_email:
            response = self.client.post(
                '/forgot-password',
                data={'identifier': email},
                follow_redirects=False,
            )

        self.assertIn(response.status_code, (302, 303))
        reset_email.assert_called_once()
        reset_url = reset_email.call_args.args[2]
        token = urlparse(reset_url).path.rsplit('/', 1)[-1]
        self.assertTrue(token)

        with self.app.app_context():
            db = get_db()
            token_row = db.execute('''
                SELECT t.*
                FROM account_setup_tokens t
                JOIN users u ON u.id = t.user_id
                WHERE u.username = ? AND t.purpose = 'password_reset'
            ''', (username,)).fetchone()
            self.assertIsNotNone(token_row)
            self.assertIsNone(token_row['used_at'])

        reset_response = self.client.post(
            f'/reset-password/{token}',
            data={'new_password': 'NewPass123!', 'confirm_password': 'NewPass123!'},
            follow_redirects=False,
        )
        self.assertIn(reset_response.status_code, (302, 303))
        self.assertIn('/login', reset_response.headers.get('Location', ''))

        login_response = self.client.post(
            '/login',
            data={'username': username, 'password': 'NewPass123!'},
            follow_redirects=False,
        )
        self.assertIn(login_response.status_code, (302, 303))

        reuse_response = self.client.get(f'/reset-password/{token}', follow_redirects=False)
        self.assertIn(reuse_response.status_code, (302, 303))
        self.assertIn('/forgot-password', reuse_response.headers.get('Location', ''))

    def test_password_reset_token_cannot_be_used_for_account_setup(self):
        email = 'reset.notsetup@example.com'
        username = 'reset.notsetup'
        with self.app.app_context():
            db = get_db()
            db.execute('DELETE FROM account_setup_tokens WHERE user_id IN (SELECT id FROM users WHERE username = ?)', (username,))
            db.execute('DELETE FROM users WHERE username = ?', (username,))
            db.execute('''
                INSERT INTO users
                    (username, password_hash, role, full_name, email,
                     is_active, must_change_password, created_at)
                VALUES (?, ?, 'member', 'Reset Not Setup', ?, 1, 1, CURRENT_TIMESTAMP)
            ''', (username, generate_password_hash('OldPass123'), email))
            db.commit()

        with patch('blueprints.auth.send_password_reset_email', return_value=True) as reset_email:
            self.client.post('/forgot-password', data={'identifier': username})

        token = urlparse(reset_email.call_args.args[2]).path.rsplit('/', 1)[-1]
        response = self.client.post(
            f'/setup-password/{token}',
            data={'new_password': 'WrongRoute123!', 'confirm_password': 'WrongRoute123!'},
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))
        self.assertIn('/login', response.headers.get('Location', ''))

        with self.app.app_context():
            db = get_db()
            user = db.execute('SELECT password_hash, must_change_password FROM users WHERE username = ?', (username,)).fetchone()
            self.assertFalse(check_password_hash(user['password_hash'], 'WrongRoute123!'))
            self.assertEqual(user['must_change_password'], 1)

    def test_admin_readiness_endpoint_reports_core_services(self):
        self.login_admin()
        response = self.client.get('/api/readiness')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn(payload['overall'], {'ok', 'warn', 'fail'})
        checks = {check['key']: check for check in payload['checks']}
        for key in ('database', 'email', 'payments', 'backup'):
            self.assertIn(key, checks)
            self.assertIn(checks[key]['status'], {'ok', 'warn', 'fail'})
        self.assertIn('members', checks['database']['meta'])

    def test_salary_upload_posts_savings_journal_and_batch_detail(self):
        self.login_admin()
        member_id = self.create_member()
        csv_body = (
            'member_number,employee_id,email,phone,amount,month,date,receipt_number,notes\n'
            'OOU/TEST/0001,EMP001,ada.audit@example.com,08000000001,15000,2026-07,2026-07-05,,July payroll\n'
        )
        response = self.client.post(
            '/savings/salary-upload',
            data={
                'month': '2026-07',
                'batch_ref': 'SAL-SAV/TEST/0001',
                'file': (BytesIO(csv_body.encode('utf-8')), 'salary.csv'),
            },
            content_type='multipart/form-data',
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))
        self.assertIn('/savings/batch/SAL-SAV/TEST/0001', response.headers['Location'])

        with self.app.app_context():
            db = get_db()
            saving = db.execute(
                'SELECT * FROM savings WHERE import_batch = ? AND member_id = ?',
                ('SAL-SAV/TEST/0001', member_id),
            ).fetchone()
            self.assertIsNotNone(saving)
            self.assertEqual(saving['payment_method'], 'salary_deduction')
            self.assertEqual(float(saving['amount']), 15000.0)
            journal = db.execute(
                'SELECT * FROM journal_entries WHERE reference = ?',
                (saving['receipt_number'],),
            ).fetchone()
            self.assertIsNotNone(journal)
            rec = ledger_reconciliation(db)
            savings_section = next(s for s in rec['sections'] if s['label'] == 'Savings deposits')
            self.assertEqual(savings_section['missing'], 0)

        detail = self.client.get('/savings/batch/SAL-SAV/TEST/0001')
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b'SAL-SAV/TEST/0001', detail.data)
        export = self.client.get('/savings/batch/SAL-SAV/TEST/0001/export')
        self.assertEqual(export.status_code, 200)
        self.assertIn(b'posted_to_gl', export.data)

    # ── Undoing a posted transaction ─────────────────────────────────────────

    def _post_savings_payout(self, member_id, amount, deposit_first=20000.0):
        """Give a member a balance, then pay some of it out through the real
        route. Returns (savings_row_id, journal_entry_id) for the payout."""
        import io, random
        from ledger import post_journal_safe, get_default_cash_account, MEMBER_DEPOSITS
        with self.app.app_context():
            db = get_db()
            receipt = f'DEP/TEST/{random.randint(100000, 999999)}'
            db.execute("INSERT INTO savings (member_id, amount, month, payment_type, "
                       " receipt_number, date) VALUES (?, ?, '2026-08', 'monthly', ?, '2026-08-05')",
                       (member_id, deposit_first, receipt))
            sid = db.execute('SELECT id FROM savings WHERE receipt_number = ?', (receipt,)).fetchone()['id']
            db.execute('UPDATE members SET total_savings = COALESCE(total_savings,0) + ? WHERE id = ?',
                       (deposit_first, member_id))
            # Post it to the ledger like the real deposit path, so the
            # reconciliation report does not see an unposted savings row.
            post_journal_safe(db, 'Savings deposit (test fixture)', [
                {'account': get_default_cash_account(db), 'debit': deposit_first, 'memo': 'deposit'},
                {'account': MEMBER_DEPOSITS, 'credit': deposit_first, 'memo': 'member'},
            ], reference=receipt, source_module='savings_deposit', source_id=sid)
            db.commit()
        rv = self.client.post('/savings/payout', data={
            'member_id': str(member_id), 'amount': str(amount),
            'reason': 'Member requested a partial withdrawal',
            'payment_method': 'bank',
            'evidence': (io.BytesIO(b'%PDF-1.4 test evidence'), 'evidence.pdf'),
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertNotIn(b'Payout blocked', rv.data)
        with self.app.app_context():
            db = get_db()
            row = db.execute("SELECT id FROM savings WHERE member_id = ? AND payment_type = 'withdrawal' "
                             "ORDER BY id DESC", (member_id,)).fetchone()
            self.assertIsNotNone(row, 'payout did not create a withdrawal row')
            je = db.execute("SELECT id FROM journal_entries WHERE source_module = 'savings_payout' "
                            "AND source_id = ?", (row['id'],)).fetchone()
            self.assertIsNotNone(je, 'payout did not post to the ledger')
            return row['id'], je['id']

    def test_reversing_a_savings_payout_puts_the_money_back(self):
        """Undoing a payout restores the member's balance, nets the ledger to
        zero and keeps both rows — nothing is deleted."""
        self.login_admin()
        member_id = self.create_member()
        with self.app.app_context():
            from utils import member_savings_balance
            base = member_savings_balance(get_db(), member_id)

        sav_id, entry_id = self._post_savings_payout(member_id, 5000.0, deposit_first=20000.0)
        with self.app.app_context():
            from utils import member_savings_balance
            db = get_db()
            self.assertAlmostEqual(member_savings_balance(db, member_id), base + 15000.0, places=2)

        rv = self.client.post(f'/accounting/journal/{entry_id}/reverse',
                              data={'reason': 'Paid out to the wrong member'},
                              follow_redirects=True)
        self.assertNotIn(b'cannot be undone', rv.data)
        with self.app.app_context():
            from utils import member_savings_balance
            db = get_db()
            # Balance back to the pre-payout figure (deposit kept, payout undone).
            self.assertAlmostEqual(member_savings_balance(db, member_id), base + 20000.0, places=2)
            # Original kept and marked, compensating row written.
            orig = db.execute('SELECT reversed_at, amount FROM savings WHERE id = ?', (sav_id,)).fetchone()
            self.assertIsNotNone(orig['reversed_at'])
            self.assertAlmostEqual(float(orig['amount']), -5000.0, places=2)
            comp = db.execute("SELECT amount FROM savings WHERE payment_type = 'reversal' "
                              "AND notes LIKE ?", (f'%payout #{sav_id}%',)).fetchone()
            self.assertIsNotNone(comp, 'no compensating row for this payout')
            self.assertAlmostEqual(float(comp['amount']), 5000.0, places=2)
            # Ledger: the payout entry and its reversal cancel out.
            net = db.execute(
                "SELECT COALESCE(SUM(jl.debit),0) - COALESCE(SUM(jl.credit),0) FROM journal_lines jl "
                "JOIN journal_entries je ON je.id = jl.entry_id "
                "WHERE (je.id = ? OR je.reversal_of = ?) AND jl.account_code = '2000'",
                (entry_id, entry_id)).fetchone()[0]
            self.assertAlmostEqual(float(net), 0.0, places=2)
            # The reason is kept on the reversal, and the undo is audited.
            rev = db.execute('SELECT reversal_reason FROM journal_entries WHERE reversal_of = ?',
                             (entry_id,)).fetchone()
            self.assertEqual(rev['reversal_reason'], 'Paid out to the wrong member')
            aud = db.execute("SELECT description FROM audit_log WHERE action = 'REVERSE_JOURNAL' "
                             "ORDER BY id DESC").fetchone()
            self.assertIsNotNone(aud, 'the reversal was not audited')
            self.assertIn('wrong member', aud['description'])

    def test_reversal_needs_a_reason(self):
        self.login_admin()
        member_id = self.create_member()
        _, entry_id = self._post_savings_payout(member_id, 3000.0)
        rv = self.client.post(f'/accounting/journal/{entry_id}/reverse',
                              data={'reason': '   '}, follow_redirects=True)
        self.assertIn(b'Give a reason', rv.data)
        with self.app.app_context():
            db = get_db()
            self.assertIsNone(db.execute('SELECT reversed_at FROM journal_entries WHERE id = ?',
                                         (entry_id,)).fetchone()['reversed_at'])

    def test_journal_refuses_to_undo_a_module_it_cannot_fully_undo(self):
        """A module with records behind it but no handler is refused outright,
        rather than correcting the ledger and leaving its own records saying the
        opposite."""
        self.login_admin()
        with self.app.app_context():
            db = get_db()
            from ledger import post_journal
            eid = post_journal(db, 'CTAS payout (test)', [
                {'account': '1150', 'debit': 50000, 'memo': 'advance'},
                {'account': '1000', 'credit': 50000, 'memo': 'cash'},
            ], source_module='ctas_payout', source_id=999)
            db.commit()
        rv = self.client.post(f'/accounting/journal/{eid}/reverse',
                              data={'reason': 'wrong amount'}, follow_redirects=True)
        self.assertIn(b'cannot be undone from the journal', rv.data)
        self.assertIn(b'CTAS cycle page', rv.data)          # says where to undo it instead
        with self.app.app_context():
            db = get_db()
            self.assertIsNone(db.execute('SELECT reversed_at FROM journal_entries WHERE id = ?',
                                         (eid,)).fetchone()['reversed_at'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM journal_entries WHERE reversal_of = ?',
                                        (eid,)).fetchone()[0], 0)

    def test_every_ledger_posting_module_is_classified(self):
        """Any module that posts to the GL must either have a reversal handler
        or be knowingly ledger-only — so a new one cannot quietly become
        un-undoable without someone deciding that."""
        import glob, re
        from ledger import REVERSAL_HANDLERS, LEDGER_ONLY_MODULES, REVERSAL_GUIDANCE
        found = set()
        for path in glob.glob('blueprints/*.py') + glob.glob('*.py'):
            with open(path, encoding='utf-8') as fh:
                found.update(re.findall(r"source_module\s*=\s*'([a-z_]+)'", fh.read()))
        found.discard('reversal')          # a reversal is never itself reversed
        unclassified = sorted(m for m in found
                              if m not in REVERSAL_HANDLERS
                              and m not in LEDGER_ONLY_MODULES
                              and m not in REVERSAL_GUIDANCE)
        self.assertEqual(unclassified, [],
                         f'these modules post to the ledger but no one has decided how they are '
                         f'undone: {unclassified}. Add a handler, mark them ledger-only, or give '
                         f'them guidance in REVERSAL_GUIDANCE.')

    def test_salary_batch_reverse_restores_savings_shares_and_ledger(self):
        """A mistaken upload can be reversed with a reason: member savings and
        shares are restored, the ledger gets a balancing entry, nothing is
        deleted, and a re-run is a no-op. Models the ooucoop 5%-share incident."""
        self.login_admin()
        member_id = self.create_member()
        batch = 'SAL-SAV/REVTEST/0007'
        month = '2026-09'
        base_sav = base_shr = 0.0
        deposit_receipt = None
        try:
            with self.app.app_context():
                db = get_db()
                m0 = db.execute('SELECT total_savings, shares_value FROM members WHERE id = ?',
                                (member_id,)).fetchone()
                base_sav = float(m0['total_savings'] or 0)
                base_shr = float((m0['shares_value'] if 'shares_value' in m0.keys() else 0) or 0)
                # Reproduce the incident: 5% of each contribution diverted to shares.
                db.execute("DELETE FROM settings WHERE key = 'share_capital_pct'")
                db.execute("INSERT INTO settings (key, value) VALUES ('share_capital_pct', '5')")
                db.commit()

            csv_body = (
                'member_number,employee_id,email,phone,amount,month,date,receipt_number,notes\n'
                f'OOU/TEST/0001,EMP001,ada.audit@example.com,08000000001,10000,{month},{month}-05,,Sept payroll\n'
            )
            up = self.client.post(
                '/savings/salary-upload',
                data={'month': month, 'batch_ref': batch,
                      'file': (BytesIO(csv_body.encode('utf-8')), 'salary.csv')},
                content_type='multipart/form-data', follow_redirects=False)
            self.assertIn(up.status_code, (302, 303))

            with self.app.app_context():
                db = get_db()
                sav = db.execute('SELECT * FROM savings WHERE import_batch = ? AND member_id = ?',
                                 (batch, member_id)).fetchone()
                self.assertIsNotNone(sav)
                sav_id = sav['id']
                deposit_receipt = sav['receipt_number']
                self.assertAlmostEqual(float(sav['amount']), 9500.0, places=2)            # 95% deposit
                self.assertAlmostEqual(float(sav['share_capital'] or 0), 500.0, places=2)  # 5% shares
                m1 = db.execute('SELECT total_savings, shares_value FROM members WHERE id = ?',
                                (member_id,)).fetchone()
                self.assertAlmostEqual(float(m1['total_savings']), base_sav + 9500.0, places=2)
                self.assertAlmostEqual(float(m1['shares_value'] or 0), base_shr + 500.0, places=2)

            # A reason is required — a blank one changes nothing.
            no_reason = self.client.post(f'/savings/batch/{batch}/reverse',
                                         data={'reason': '   '}, follow_redirects=False)
            self.assertIn(no_reason.status_code, (302, 303))
            with self.app.app_context():
                db = get_db()
                je = db.execute("SELECT reversed_at FROM journal_entries "
                                "WHERE source_module = 'savings_deposit' AND source_id = ?",
                                (sav_id,)).fetchone()
                self.assertIsNone(je['reversed_at'])

            # Reverse for real.
            rv = self.client.post(f'/savings/batch/{batch}/reverse',
                                  data={'reason': 'Share capital was 5% by mistake'},
                                  follow_redirects=False)
            self.assertIn(rv.status_code, (302, 303))
            with self.app.app_context():
                db = get_db()
                je = db.execute("SELECT id, reversed_at FROM journal_entries "
                                "WHERE source_module = 'savings_deposit' AND source_id = ?",
                                (sav_id,)).fetchone()
                self.assertIsNotNone(je['reversed_at'])                    # original marked reversed
                rev_count = db.execute("SELECT COUNT(*) FROM journal_entries WHERE reversal_of = ?",
                                       (je['id'],)).fetchone()[0]
                self.assertEqual(rev_count, 1)                             # one balancing entry posted
                m2 = db.execute('SELECT total_savings, shares_value FROM members WHERE id = ?',
                                (member_id,)).fetchone()
                self.assertAlmostEqual(float(m2['total_savings']), base_sav, places=2)   # restored
                self.assertAlmostEqual(float(m2['shares_value'] or 0), base_shr, places=2)
                aud = db.execute("SELECT description FROM audit_log "
                                 "WHERE action = 'REVERSE_SAVINGS_BATCH' ORDER BY id DESC").fetchone()
                self.assertIsNotNone(aud)
                self.assertIn('5%', aud['description'])                    # reason captured

            # Re-running is a no-op.
            again = self.client.post(f'/savings/batch/{batch}/reverse',
                                     data={'reason': 'again'}, follow_redirects=False)
            self.assertIn(again.status_code, (302, 303))
            with self.app.app_context():
                db = get_db()
                je = db.execute("SELECT id FROM journal_entries "
                                "WHERE source_module = 'savings_deposit' AND source_id = ?",
                                (sav_id,)).fetchone()
                rev_count = db.execute("SELECT COUNT(*) FROM journal_entries WHERE reversal_of = ?",
                                       (je['id'],)).fetchone()[0]
                self.assertEqual(rev_count, 1)                             # still just one
        finally:
            with self.app.app_context():
                db = get_db()
                db.execute("DELETE FROM settings WHERE key = 'share_capital_pct'")
                # Remove ledger + savings traces so shared-DB reconciliation tests stay clean.
                if deposit_receipt:
                    ids = [d['id'] for d in db.execute(
                        "SELECT id FROM journal_entries WHERE reference = ?", (deposit_receipt,)).fetchall()]
                    ids += [r['id'] for did in list(ids) for r in db.execute(
                        "SELECT id FROM journal_entries WHERE reversal_of = ?", (did,)).fetchall()]
                    for jid in ids:
                        db.execute("DELETE FROM journal_lines WHERE entry_id = ?", (jid,))
                        db.execute("DELETE FROM journal_entries WHERE id = ?", (jid,))
                db.execute("DELETE FROM savings WHERE member_id = ? AND month = ?", (member_id, month))
                db.execute("UPDATE members SET total_savings = ?, shares_value = ? WHERE id = ?",
                           (base_sav, base_shr, member_id))
                db.commit()

    def test_reupload_after_reversal_is_not_blocked_and_no_share_split_at_zero(self):
        """The ooucoop case: after a batch is reversed, the SAME data (same
        receipts, as from the batch export) must re-upload cleanly, and with
        share_capital_pct=0 nothing is diverted to shares."""
        self.login_admin()
        member_id = self.create_member()
        batch = 'SAL-SAV/REUP/0001'
        month = '2026-10'
        receipt = 'PAYROLL/REUP/0001/0001'
        base_sav = base_shr = 0.0
        try:
            with self.app.app_context():
                db = get_db()
                m0 = db.execute('SELECT total_savings, shares_value FROM members WHERE id = ?',
                                (member_id,)).fetchone()
                base_sav = float(m0['total_savings'] or 0)
                base_shr = float((m0['shares_value'] if 'shares_value' in m0.keys() else 0) or 0)
                db.execute("DELETE FROM settings WHERE key = 'share_capital_pct'")
                db.execute("INSERT INTO settings (key, value) VALUES ('share_capital_pct', '0')")
                db.commit()

            csv_body = (
                'member_number,employee_id,email,phone,amount,month,date,receipt_number,notes\n'
                f'OOU/TEST/0001,EMP001,ada.audit@example.com,08000000001,10000,{month},{month}-05,{receipt},Oct\n')

            def _upload(follow):
                return self.client.post('/savings/salary-upload',
                    data={'month': month, 'batch_ref': batch,
                          'file': (BytesIO(csv_body.encode('utf-8')), 's.csv')},
                    content_type='multipart/form-data', follow_redirects=follow)

            self.assertIn(_upload(False).status_code, (302, 303))
            with self.app.app_context():
                db = get_db()
                row = db.execute('SELECT * FROM savings WHERE receipt_number = ?', (receipt,)).fetchone()
                self.assertIsNotNone(row)
                self.assertAlmostEqual(float(row['amount']), 10000.0, places=2)          # no split
                self.assertAlmostEqual(float(row['share_capital'] or 0), 0.0, places=2)
                orig_id = row['id']

            # Reverse the batch.
            rv = self.client.post(f'/savings/batch/{batch}/reverse',
                                  data={'reason': 'wrong data'}, follow_redirects=False)
            self.assertIn(rv.status_code, (302, 303))
            with self.app.app_context():
                db = get_db()
                orig = db.execute('SELECT reversed_at, receipt_number FROM savings WHERE id = ?', (orig_id,)).fetchone()
                self.assertIsNotNone(orig['reversed_at'])          # marked reversed
                self.assertIn('~REV', orig['receipt_number'])      # receipt freed

            # Re-upload the SAME rows (same receipt) — previously blocked as duplicate.
            self.assertEqual(_upload(True).status_code, 200)
            with self.app.app_context():
                db = get_db()
                fresh = db.execute("SELECT * FROM savings WHERE receipt_number = ? AND reversed_at IS NULL",
                                   (receipt,)).fetchone()
                self.assertIsNotNone(fresh)                        # re-upload succeeded
                self.assertAlmostEqual(float(fresh['amount']), 10000.0, places=2)
                self.assertAlmostEqual(float(fresh['share_capital'] or 0), 0.0, places=2)
        finally:
            with self.app.app_context():
                db = get_db()
                db.execute("DELETE FROM settings WHERE key = 'share_capital_pct'")
                ids = [r['id'] for r in db.execute('SELECT id FROM savings WHERE member_id = ? AND month = ?',
                                                   (member_id, month)).fetchall()]
                for sid in ids:
                    deps = [d['id'] for d in db.execute(
                        "SELECT id FROM journal_entries WHERE source_module = 'savings_deposit' AND source_id = ?",
                        (sid,)).fetchall()]
                    alljes = list(deps)
                    for did in deps:
                        alljes += [r['id'] for r in db.execute(
                            'SELECT id FROM journal_entries WHERE reversal_of = ?', (did,)).fetchall()]
                    for jid in alljes:
                        db.execute('DELETE FROM journal_lines WHERE entry_id = ?', (jid,))
                        db.execute('DELETE FROM journal_entries WHERE id = ?', (jid,))
                db.execute('DELETE FROM savings WHERE member_id = ? AND month = ?', (member_id, month))
                db.execute('UPDATE members SET total_savings = ?, shares_value = ? WHERE id = ?',
                           (base_sav, base_shr, member_id))
                db.commit()

    def test_savings_adjustment_moves_both_the_member_and_the_ledger(self):
        """Correcting a savings balance has to write two places at once. A
        journal entry alone moves the books while the member's dashboard sits
        unchanged, which is the trap this exists to close. Reducing works too,
        the member's statement carries the reason, and the entry is reversible."""
        self.login_admin()
        member_id = self.create_member()
        try:
            with self.app.app_context():
                db = get_db()
                start = member_savings_balance(db, member_id)
                # The cached column is asserted on its own movement, not against
                # the row total: other tests insert savings rows directly and
                # leave the cache behind, and reconciling that is not this
                # feature's job.
                cached_start = float(db.execute(
                    'SELECT COALESCE(total_savings,0) AS t FROM members WHERE id = ?',
                    (member_id,)).fetchone()['t'] or 0)
                dep_start = db.execute(
                    "SELECT COALESCE(SUM(credit-debit),0) FROM journal_lines WHERE account_code='2000'"
                ).fetchone()[0] or 0

            r = self.client.post('/savings/adjust', data={
                'member_id': member_id, 'amount': '25000',
                'contra_account': '3000', 'date': '2026-07-31',
                'reason': 'Understated in the Tally opening balance'},
                follow_redirects=False)
            self.assertIn(r.status_code, (302, 303))

            with self.app.app_context():
                db = get_db()
                # The member moved...
                self.assertAlmostEqual(member_savings_balance(db, member_id), start + 25000, places=2)
                cached = db.execute('SELECT total_savings FROM members WHERE id = ?',
                                    (member_id,)).fetchone()['total_savings']
                self.assertAlmostEqual(float(cached or 0) - cached_start, 25000, places=2)
                # ...and so did the ledger, by the same amount.
                dep_now = db.execute(
                    "SELECT COALESCE(SUM(credit-debit),0) FROM journal_lines WHERE account_code='2000'"
                ).fetchone()[0] or 0
                self.assertAlmostEqual(float(dep_now) - float(dep_start), 25000, places=2)
                surplus = db.execute(
                    "SELECT COALESCE(SUM(debit-credit),0) FROM journal_lines l "
                    "JOIN journal_entries e ON e.id = l.entry_id "
                    "WHERE l.account_code='3000' AND e.source_module='savings_adjustment'"
                ).fetchone()[0] or 0
                self.assertAlmostEqual(float(surplus), 25000, places=2)
                # The reason is on the row, so the statement can explain itself.
                row = db.execute("SELECT * FROM savings WHERE member_id = ? AND payment_type='adjustment'",
                                 (member_id,)).fetchone()
                self.assertIn('Tally', row['notes'])

            # A reduction is the same flow with a negative amount.
            r2 = self.client.post('/savings/adjust', data={
                'member_id': member_id, 'amount': '-5000',
                'contra_account': '3000', 'reason': 'Overstated'}, follow_redirects=False)
            self.assertIn(r2.status_code, (302, 303))
            with self.app.app_context():
                db = get_db()
                self.assertAlmostEqual(member_savings_balance(db, member_id), start + 20000, places=2)

            # It cannot push a member below zero, and needs a reason.
            self.client.post('/savings/adjust', data={
                'member_id': member_id, 'amount': '-99999999',
                'reason': 'too much'}, follow_redirects=True)
            self.client.post('/savings/adjust', data={
                'member_id': member_id, 'amount': '1000', 'reason': '  '},
                follow_redirects=True)
            with self.app.app_context():
                db = get_db()
                self.assertAlmostEqual(member_savings_balance(db, member_id), start + 20000, places=2)
        finally:
            with self.app.app_context():
                db = get_db()
                ids = [r['id'] for r in db.execute(
                    "SELECT id FROM savings WHERE member_id = ? AND payment_type='adjustment'",
                    (member_id,)).fetchall()]
                for sid in ids:
                    for e in db.execute("SELECT id FROM journal_entries WHERE source_module='savings_adjustment' AND source_id = ?",
                                        (sid,)).fetchall():
                        db.execute('DELETE FROM journal_lines WHERE entry_id = ?', (e['id'],))
                        db.execute('DELETE FROM journal_entries WHERE id = ?', (e['id'],))
                db.execute("DELETE FROM savings WHERE member_id = ? AND payment_type='adjustment'",
                           (member_id,))
                db.execute('UPDATE members SET total_savings = 0 WHERE id = ?', (member_id,))
                db.commit()

    def test_backfill_skips_records_covered_by_the_opening_balance(self):
        """A migrated cooperative imports member balances as subledger rows with
        no journal entries, because the opening balance already states the
        position they add up to. Backfill must leave those alone -- posting them
        again doubles the ledger. Records dated after the cutover still post."""
        from ledger import backfill_from_transactions, post_journal, opening_balance_date
        self.login_admin()
        member_id = self.create_member()
        try:
            with self.app.app_context():
                db = get_db()
                # The opening balance: the cooperative's position at cutover.
                post_journal(db, 'Opening balances',
                             [{'account': '1000', 'debit': 500000},
                              {'account': '2000', 'credit': 500000}],
                             date='2026-07-31', reference='OPENING-2026-07-31',
                             source_module='opening', created_by=1)
                # One savings row from before the cutover (already in the opening
                # figure) and one from after (genuinely new activity).
                db.execute("INSERT INTO savings (member_id, amount, month, receipt_number, date) "
                           "VALUES (?, 250000, '2026-06', 'BACKFILL-PRE', '2026-06-30')", (member_id,))
                db.execute("INSERT INTO savings (member_id, amount, month, receipt_number, date) "
                           "VALUES (?, 40000, '2026-08', 'BACKFILL-POST', '2026-08-15')", (member_id,))
                db.commit()

                self.assertEqual(opening_balance_date(db), '2026-07-31')

                posted, skipped = backfill_from_transactions(db, created_by=1)
                db.commit()

                self.assertGreaterEqual(skipped, 1)
                pre = db.execute("SELECT 1 FROM journal_entries WHERE reference = 'BACKFILL-PRE'").fetchone()
                post = db.execute("SELECT 1 FROM journal_entries WHERE reference = 'BACKFILL-POST'").fetchone()
                self.assertIsNone(pre, 'pre-cutover savings must NOT be posted again')
                self.assertIsNotNone(post, 'post-cutover savings must still be posted')

                # Re-running changes nothing further.
                again, _ = backfill_from_transactions(db, created_by=1)
                db.commit()
                self.assertEqual(again, 0)
        finally:
            with self.app.app_context():
                db = get_db()
                for ref in ('OPENING-2026-07-31', 'BACKFILL-POST'):
                    for e in db.execute('SELECT id FROM journal_entries WHERE reference = ?', (ref,)).fetchall():
                        db.execute('DELETE FROM journal_lines WHERE entry_id = ?', (e['id'],))
                        db.execute('DELETE FROM journal_entries WHERE id = ?', (e['id'],))
                db.execute("DELETE FROM savings WHERE receipt_number IN ('BACKFILL-PRE','BACKFILL-POST')")
                db.commit()

    def test_migration_savings_import_applies_share_capital_split(self):
        """A bulk savings import must carve out share capital exactly like a
        manual entry or a salary upload. It used to store the gross amount as
        deposit with share_capital 0, so a migrated cooperative's shares silently
        read zero and members' deposits looked inflated. The export then has to
        hand back the GROSS figure, or an export/re-import round trip splits the
        already-split amount a second time and shrinks every balance."""
        self.login_admin()
        member_id = self.create_member()
        month = '2026-11'
        receipt = 'RCPT/MIGSPLIT/0001'
        base_sav = base_shr = 0.0
        try:
            with self.app.app_context():
                db = get_db()
                m0 = db.execute('SELECT total_savings, shares_value FROM members WHERE id = ?',
                                (member_id,)).fetchone()
                base_sav = float(m0['total_savings'] or 0)
                base_shr = float((m0['shares_value'] if 'shares_value' in m0.keys() else 0) or 0)
                db.execute("DELETE FROM settings WHERE key = 'share_capital_pct'")
                db.execute("INSERT INTO settings (key, value) VALUES ('share_capital_pct', '5')")
                db.commit()

            csv_body = ('member_number,email,amount,month,payment_type,receipt_number,date\n'
                        f'OOU/TEST/0001,ada.audit@example.com,10000,{month},monthly,{receipt},{month}-05\n')
            up = self.client.post('/migration/savings',
                data={'file': (BytesIO(csv_body.encode('utf-8')), 'savings.csv')},
                content_type='multipart/form-data', follow_redirects=False)
            self.assertIn(up.status_code, (302, 303))

            with self.app.app_context():
                db = get_db()
                row = db.execute('SELECT * FROM savings WHERE receipt_number = ?', (receipt,)).fetchone()
                self.assertIsNotNone(row)
                self.assertAlmostEqual(float(row['amount']), 9500.0, places=2)             # 95% deposit
                self.assertAlmostEqual(float(row['share_capital'] or 0), 500.0, places=2)  # 5% shares
                m1 = db.execute('SELECT total_savings, shares_value FROM members WHERE id = ?',
                                (member_id,)).fetchone()
                self.assertAlmostEqual(float(m1['total_savings']), base_sav + 9500.0, places=2)
                self.assertAlmostEqual(float(m1['shares_value'] or 0), base_shr + 500.0, places=2)

            # The export hands back the gross contribution, so re-importing the
            # file it produces reproduces the same split rather than shrinking it.
            ex = self.client.get('/migration/savings/export')
            self.assertEqual(ex.status_code, 200)
            line = [l for l in ex.data.decode('utf-8').splitlines() if receipt in l]
            self.assertTrue(line, 'exported savings row not found')
            self.assertIn('10000', line[0])
        finally:
            with self.app.app_context():
                db = get_db()
                db.execute("DELETE FROM settings WHERE key = 'share_capital_pct'")
                db.execute('DELETE FROM savings WHERE member_id = ? AND month = ?', (member_id, month))
                db.execute('UPDATE members SET total_savings = ?, shares_value = ? WHERE id = ?',
                           (base_sav, base_shr, member_id))
                db.commit()

    def test_member_can_have_two_savings_in_one_month(self):
        """Salary deduction + voluntary savings in the same month must both
        import — month is not a uniqueness criterion."""
        self.login_admin()
        member_id = self.create_member()
        batch = 'SAL-SAV/MULTI/0001'
        month = '2026-11'
        try:
            csv_body = (
                'member_number,employee_id,email,phone,amount,month,date,receipt_number,notes\n'
                f'OOU/TEST/0001,EMP001,ada.audit@example.com,08000000001,8000,{month},{month}-05,,Salary deduction\n'
                f'OOU/TEST/0001,EMP001,ada.audit@example.com,08000000001,5000,{month},{month}-20,,Voluntary savings\n')
            r = self.client.post('/savings/salary-upload',
                data={'month': month, 'batch_ref': batch,
                      'file': (BytesIO(csv_body.encode('utf-8')), 's.csv')},
                content_type='multipart/form-data', follow_redirects=False)
            self.assertIn(r.status_code, (302, 303))
            with self.app.app_context():
                n = get_db().execute('SELECT COUNT(*) FROM savings WHERE import_batch = ? AND member_id = ?',
                                     (batch, member_id)).fetchone()[0]
                self.assertEqual(n, 2)   # both rows imported, same month
        finally:
            with self.app.app_context():
                db = get_db()
                ids = [r['id'] for r in db.execute('SELECT id FROM savings WHERE member_id = ? AND month = ?',
                                                   (member_id, month)).fetchall()]
                for sid in ids:
                    for j in db.execute("SELECT id FROM journal_entries WHERE source_module = 'savings_deposit' AND source_id = ?",
                                        (sid,)).fetchall():
                        db.execute('DELETE FROM journal_lines WHERE entry_id = ?', (j['id'],))
                        db.execute('DELETE FROM journal_entries WHERE id = ?', (j['id'],))
                db.execute('DELETE FROM savings WHERE member_id = ? AND month = ?', (member_id, month))
                db.execute('UPDATE members SET total_savings = 0, shares_value = 0 WHERE id = ?', (member_id,))
                db.commit()

    def test_accounting_exports_journal_and_gl_register_csv(self):
        self.login_admin()
        with self.app.app_context():
            from ledger import CASH, OPERATING_EXPENSES, post_journal
            db = get_db()
            existing = db.execute(
                "SELECT id FROM journal_entries WHERE reference = 'TEST/GL/EXPORT'"
            ).fetchone()
            if not existing:
                post_journal(
                    db,
                    'CSV export smoke test',
                    [
                        {'account': OPERATING_EXPENSES, 'debit': 1234.56, 'memo': 'Export debit'},
                        {'account': CASH, 'credit': 1234.56, 'memo': 'Export credit'},
                    ],
                    date='2026-07-21',
                    reference='TEST/GL/EXPORT',
                    source_module='manual',
                )
                db.commit()

        journal_export = self.client.get('/accounting/journal/export')
        self.assertEqual(journal_export.status_code, 200)
        self.assertIn(b'entry_number,date,description,reference', journal_export.data)
        self.assertIn(b'TEST/GL/EXPORT', journal_export.data)

        gl_export = self.client.get('/accounting/ledger/1000/export')
        self.assertEqual(gl_export.status_code, 200)
        self.assertIn(b'account_code,account_name,account_type,normal_balance', gl_export.data)
        self.assertIn(b'TEST/GL/EXPORT', gl_export.data)

        with self.app.app_context():
            db = get_db()
            journal = db.execute(
                "SELECT id FROM journal_entries WHERE reference = 'TEST/GL/EXPORT'"
            ).fetchone()
            if journal:
                db.execute('DELETE FROM journal_lines WHERE entry_id = ?', (journal['id'],))
                db.execute('DELETE FROM journal_entries WHERE id = ?', (journal['id'],))
                db.commit()

    def test_bank_accounts_position_and_reconciliation_exports(self):
        self.login_admin()
        with open(os.path.join(os.getcwd(), 'blueprints', 'accounting.py'), encoding='utf-8') as f:
            self.assertNotIn("LIKE '%", f.read())
        with self.app.app_context():
            from ledger import OPERATING_EXPENSES, post_journal
            db = get_db()
            db.execute("DELETE FROM accounts WHERE code = '1096'")
            db.execute('''
                INSERT INTO accounts (code, name, type, normal_balance, parent_code, is_active, is_cash_account)
                VALUES ('1096', 'Test Reconciliation Bank', 'asset', 'debit', '1000', 1, 1)
            ''')
            entry_ids = []
            entry_ids.append(post_journal(
                db,
                'Opening bank test movement',
                [
                    {'account': '1096', 'debit': 1000, 'memo': 'Opening bank'},
                    {'account': OPERATING_EXPENSES, 'credit': 1000, 'memo': 'Offset'},
                ],
                date='2025-12-31',
                reference='TEST/BANK/OPEN',
                source_module='manual',
            ))
            entry_ids.append(post_journal(
                db,
                'Period bank inflow',
                [
                    {'account': '1096', 'debit': 2500, 'memo': 'Inflow'},
                    {'account': OPERATING_EXPENSES, 'credit': 2500, 'memo': 'Offset'},
                ],
                date='2026-07-10',
                reference='TEST/BANK/IN',
                source_module='manual',
            ))
            entry_ids.append(post_journal(
                db,
                'Period bank outflow',
                [
                    {'account': OPERATING_EXPENSES, 'debit': 400, 'memo': 'Expense'},
                    {'account': '1096', 'credit': 400, 'memo': 'Outflow'},
                ],
                date='2026-07-12',
                reference='TEST/BANK/OUT',
                source_module='manual',
            ))
            db.commit()

        page = self.client.get('/accounting/bank-accounts?from_date=2026-01-01&to_date=2026-07-31')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Test Reconciliation Bank', page.data)
        self.assertIn(b'Bank Position', page.data)

        csv_page = self.client.get('/accounting/bank-accounts?from_date=2026-01-01&to_date=2026-07-31&format=csv')
        self.assertEqual(csv_page.status_code, 200)
        self.assertIn(b'account_code,account_name,opening_balance,cash_in', csv_page.data)
        self.assertIn(b'1096,Test Reconciliation Bank,1000.00,2500.00,400.00,3100.00', csv_page.data)

        detail = self.client.get(
            '/accounting/bank-accounts/1096?from_date=2026-01-01&to_date=2026-07-31&statement_balance=3100'
        )
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b'Bank Reconciliation', detail.data)
        self.assertIn(b'Variance', detail.data)
        self.assertIn(b'TEST/BANK/IN', detail.data)

        detail_csv = self.client.get(
            '/accounting/bank-accounts/1096?from_date=2026-01-01&to_date=2026-07-31&statement_balance=3100&format=csv'
        )
        self.assertEqual(detail_csv.status_code, 200)
        self.assertIn(b'gl_closing_balance,3100.00', detail_csv.data)
        self.assertIn(b'TEST/BANK/OUT', detail_csv.data)

        cash_header = self.client.get('/accounting/bank-accounts/1000?from_date=2026-01-01&to_date=2026-07-31')
        self.assertEqual(cash_header.status_code, 200)
        self.assertIn(b'Bank Reconciliation', cash_header.data)
        self.assertIn(b'Cash &amp; Bank', cash_header.data)

        with self.app.app_context():
            db = get_db()
            for entry_id in entry_ids:
                db.execute('DELETE FROM journal_lines WHERE entry_id = ?', (entry_id,))
                db.execute('DELETE FROM journal_entries WHERE id = ?', (entry_id,))
            db.execute("DELETE FROM accounts WHERE code = '1096'")
            db.commit()

    def test_admin_can_reclassify_savings_bank_lines_to_detail_bank(self):
        self.login_admin()
        with self.app.app_context():
            from ledger import MEMBER_DEPOSITS, post_journal
            db = get_db()
            db.execute("DELETE FROM accounts WHERE code = '1095'")
            db.execute('''
                INSERT INTO accounts (code, name, type, normal_balance, parent_code, is_active, is_cash_account)
                VALUES ('1095', 'Zenith Test Bank', 'asset', 'debit', '1000', 1, 1)
            ''')
            entry_id = post_journal(
                db,
                'Savings posted to header account',
                [
                    {'account': '1000', 'debit': 7500, 'memo': 'Savings cash side'},
                    {'account': MEMBER_DEPOSITS, 'credit': 7500, 'memo': 'Member savings'},
                ],
                date='2026-07-15',
                reference='TEST/SAV/RECLASS',
                source_module='savings_deposit',
            )
            db.commit()

        response = self.client.post(
            '/accounting/bank-accounts/reclassify-savings',
            data={
                'from_account': '1000',
                'to_account': '1095',
                'from_date': '2026-07-01',
                'to_date': '2026-07-31',
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Moved 1 savings bank line', response.data)

        with self.app.app_context():
            db = get_db()
            moved = db.execute('''
                SELECT account_code, debit, credit
                FROM journal_lines
                WHERE entry_id = ? AND debit > 0
            ''', (entry_id,)).fetchone()
            liability = db.execute('''
                SELECT account_code, debit, credit
                FROM journal_lines
                WHERE entry_id = ? AND credit > 0
            ''', (entry_id,)).fetchone()
            self.assertEqual(moved['account_code'], '1095')
            self.assertEqual(liability['account_code'], MEMBER_DEPOSITS)
            db.execute('DELETE FROM journal_lines WHERE entry_id = ?', (entry_id,))
            db.execute('DELETE FROM journal_entries WHERE id = ?', (entry_id,))
            db.execute("DELETE FROM accounts WHERE code = '1095'")
            db.commit()

    def test_admin_can_send_member_email_campaign_with_logs(self):
        member_id = self.create_member()
        self.login_admin()
        composer = self.client.get('/communications/new')
        self.assertEqual(composer.status_code, 200)
        self.assertIn(b'Monthly savings reminder', composer.data)
        self.assertIn(b'Loan repayment reminder', composer.data)

        sent_messages = []

        def fake_send(to, subject, html, text=''):
            sent_messages.append((to, subject, html))
            return True

        with patch('blueprints.communications.send_email', side_effect=fake_send):
            response = self.client.post(
                '/communications/new',
                data={
                    'title': 'Profile reminder',
                    'audience': 'selected',
                    'channel': 'email',
                    'member_ids': [str(member_id)],
                    'subject': 'Hello {first_name}',
                    'body': 'Dear {first_name}, your balance is {savings_balance}. Portal: {portal_link}',
                },
                follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Campaign queued', response.data)
        self.assertEqual(len(sent_messages), 1)
        self.assertEqual(sent_messages[0][0], 'ada.audit@example.com')
        self.assertIn('Hello Ada', sent_messages[0][1])
        self.assertIn('Dear Ada', sent_messages[0][2])
        self.assertIn('NGN', sent_messages[0][2])
        self.assertIn('CoopMS Member Communication', sent_messages[0][2])
        # The email heading reflects the campaign title, not a hardcoded string.
        self.assertIn('Profile reminder', sent_messages[0][2])

        with self.app.app_context():
            db = get_db()
            campaign = db.execute(
                "SELECT * FROM communication_campaigns WHERE title = 'Profile reminder'"
            ).fetchone()
            self.assertIsNotNone(campaign)
            self.assertEqual(campaign['sent_count'], 1)
            recipient = db.execute(
                'SELECT * FROM communication_recipients WHERE campaign_id = ?',
                (campaign['id'],),
            ).fetchone()
            self.assertEqual(recipient['status'], 'sent')
            db.execute('DELETE FROM communication_recipients WHERE campaign_id = ?', (campaign['id'],))
            db.execute('DELETE FROM communication_campaigns WHERE id = ?', (campaign['id'],))
            db.commit()

    def test_journal_quick_view_drawer_endpoint_and_register_link(self):
        self.login_admin()
        with self.app.app_context():
            from ledger import CASH, OPERATING_EXPENSES, post_journal
            db = get_db()
            existing = db.execute(
                "SELECT id FROM journal_entries WHERE reference = 'TEST/JOURNAL/DRAWER'"
            ).fetchone()
            if existing:
                entry_id = existing['id']
            else:
                entry_id = post_journal(
                    db,
                    'Drawer quick view smoke test',
                    [
                        {'account': OPERATING_EXPENSES, 'debit': 500, 'memo': 'Drawer debit'},
                        {'account': CASH, 'credit': 500, 'memo': 'Drawer credit'},
                    ],
                    date='2026-07-22',
                    reference='TEST/JOURNAL/DRAWER',
                    source_module='manual',
                )
                db.commit()

        quick_view = self.client.get(f'/accounting/journal/{entry_id}/quick-view')
        self.assertEqual(quick_view.status_code, 200)
        payload = quick_view.get_json()
        self.assertTrue(payload['ok'])
        self.assertIn('TEST/JOURNAL/DRAWER', payload['html'])
        self.assertIn('Debit (left side)', payload['html'])
        self.assertIn('Open full page', payload['html'])

        register = self.client.get('/accounting/journal')
        self.assertEqual(register.status_code, 200)
        self.assertIn(b'data-journal-quick-view', register.data)
        self.assertIn(f'/accounting/journal/{entry_id}/quick-view'.encode(), register.data)

        with self.app.app_context():
            db = get_db()
            db.execute('DELETE FROM journal_lines WHERE entry_id = ?', (entry_id,))
            db.execute('DELETE FROM journal_entries WHERE id = ?', (entry_id,))
            db.commit()

    def test_member_profile_completion_and_certified_badge(self):
        member_id = self.create_member()
        self.create_member_user(member_id)
        self.login_member()

        incomplete = self.client.get('/profile')
        self.assertEqual(incomplete.status_code, 200)
        self.assertIn(b'Profile In Progress', incomplete.data)
        self.assertIn(b'Readiness to transact', incomplete.data)

        response = self.client.post(
            '/edit-profile',
            data={
                'first_name': 'Ada',
                'last_name': 'Audit',
                'email': 'ada.audit@example.com',
                'phone': '08000000001',
                'date_of_birth': '1990-01-02',
                'occupation': 'Accountant',
                'address': '12 Cooperative Road',
                'city': 'Ago-Iwoye',
                'state': 'Ogun',
                'country': 'Nigeria',
                'bank_name': 'Test Bank',
                'account_name': 'Ada Audit',
                'account_number': '1234567890',
                'emergency_contact_name': 'Bola Audit',
                'emergency_contact_phone': '08000000002',
                'nominee_name': 'Tunde Audit',
                'nominee_relationship': 'Brother',
                'nominee_phone': '08000000003',
                'nominee_email': 'tunde.audit@example.com',
                'nominee_address': '13 Cooperative Road',
                'bvn': '12345678901',
                'nin': '10987654321',
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Certified Member', response.data)
        self.assertIn(b'100%', response.data)
        self.assertNotIn(b'1234567890', response.data)
        self.assertIn(b'******7890', response.data)

        with self.app.app_context():
            db = get_db()
            member = db.execute('SELECT * FROM members WHERE id = ?', (member_id,)).fetchone()
            self.assertEqual(member['city'], 'Ago-Iwoye')
            self.assertEqual(member['state'], 'Ogun')
            self.assertTrue(is_encrypted(member['bank_name']))
            self.assertTrue(is_encrypted(member['account_number']))
            self.assertTrue(is_encrypted(member['bvn']))
            self.assertEqual(decrypt_field(member['bank_name']), 'Test Bank')
            self.assertEqual(decrypt_field(member['account_number']), '1234567890')
            self.assertEqual(decrypt_field(member['bvn']), '12345678901')
            user = db.execute('SELECT * FROM users WHERE email = ?', ('ada.audit@example.com',)).fetchone()
            self.assertEqual(user['phone'], '08000000001')

        bad_reveal = self.client.post('/profile/reveal-sensitive', data={'password': 'wrong'})
        self.assertEqual(bad_reveal.status_code, 403)
        reveal = self.client.post('/profile/reveal-sensitive', data={'password': 'MemberPass1!'})
        self.assertEqual(reveal.status_code, 200)
        fields = reveal.get_json()['fields']
        self.assertEqual(fields['account_number'], '1234567890')
        self.assertEqual(fields['bvn'], '12345678901')
        self.assertEqual(fields['nin'], '10987654321')

    def test_staff_user_can_switch_between_admin_and_member_views(self):
        member_id = self.create_member()
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE users SET email = ?, phone = ? WHERE username = 'admin'",
                ('ada.audit@example.com', '08000000001')
            )
            db.commit()

        self.login_admin()
        admin_page = self.client.get('/dashboard')
        self.assertEqual(admin_page.status_code, 200)
        self.assertIn(b'My Member Portal', admin_page.data)
        self.assertIn(b'Dashboard', admin_page.data)

        member_view = self.client.post('/member/view-as-member', follow_redirects=True)
        self.assertEqual(member_view.status_code, 200)
        self.assertIn(b'My Profile', member_view.data)
        self.assertIn(b'Back to Admin', member_view.data)
        self.assertNotIn(b'Data Migration', member_view.data)

        protected_admin = self.client.get('/members')
        self.assertEqual(protected_admin.status_code, 200)

        admin_view = self.client.post('/member/back-to-admin', follow_redirects=True)
        self.assertEqual(admin_view.status_code, 200)
        self.assertIn(b'Data Migration', admin_view.data)

    def test_chart_of_accounts_creates_detail_account_under_parent(self):
        self.login_admin()
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM accounts WHERE code = '1099'")
            db.commit()

        response = self.client.post(
            '/accounting/accounts/add',
            data={
                'code': '1099',
                'name': 'Test Detail Bank',
                'type': '',
                'normal_balance': '',
                'parent_code': '1000',
            },
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))
        with self.app.app_context():
            db = get_db()
            account = db.execute("SELECT * FROM accounts WHERE code = '1099'").fetchone()
            self.assertIsNotNone(account)
            self.assertEqual(account['parent_code'], '1000')
            self.assertEqual(account['type'], 'asset')
            self.assertEqual(account['normal_balance'], 'debit')

            db.execute("DELETE FROM accounts WHERE code = '1099'")
            db.commit()

    def test_savings_post_to_configured_default_cash_detail_account(self):
        self.login_admin()
        member_id = self.create_member()
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM journal_lines WHERE account_code = '1098'")
            db.execute("DELETE FROM accounts WHERE code = '1098'")
            db.execute("DELETE FROM settings WHERE key = 'default_cash_account'")
            db.execute('''
                INSERT INTO accounts (code, name, type, normal_balance, parent_code, is_active, is_cash_account)
                VALUES ('1098', 'Test Main Bank', 'asset', 'debit', '1000', 1, 1)
            ''')
            db.execute(
                "INSERT INTO settings (key, value, description) VALUES ('default_cash_account', '1098', 'test')"
            )
            db.commit()

        response = self.client.post('/savings/add', data={
            'member_id': member_id,
            'amount': '5000',
            'month': '2026-07',
            'payment_type': 'monthly',
            'payment_method': 'bank_transfer',
            'notes': 'Default cash account test',
        }, follow_redirects=False)
        self.assertIn(response.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            line = db.execute('''
                SELECT jl.*
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.entry_id
                WHERE jl.account_code = '1098'
                  AND je.source_module = 'savings_deposit'
                ORDER BY jl.id DESC
            ''').fetchone()
            self.assertIsNotNone(line)
            self.assertGreater(float(line['debit'] or 0), 0)
            db.execute("DELETE FROM settings WHERE key = 'default_cash_account'")
            db.execute("DELETE FROM journal_lines WHERE account_code = '1098'")
            db.execute("DELETE FROM accounts WHERE code = '1098'")
            db.commit()

    def test_savings_post_to_selected_receiving_bank_account(self):
        self.login_admin()
        member_id = self.create_member()
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM journal_lines WHERE account_code = '1097'")
            db.execute("DELETE FROM accounts WHERE code = '1097'")
            db.execute('''
                INSERT INTO accounts (code, name, type, normal_balance, parent_code, is_active, is_cash_account)
                VALUES ('1097', 'Test Zenith Bank', 'asset', 'debit', '1000', 1, 1)
            ''')
            db.commit()

        response = self.client.post('/savings/add', data={
            'member_id': member_id,
            'amount': '5000',
            'month': '2026-08',
            'payment_type': 'voluntary',
            'payment_method': 'bank_transfer',
            'bank_account': '1097',
            'notes': 'Selected bank posting test',
        }, follow_redirects=False)
        self.assertIn(response.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            line = db.execute('''
                SELECT jl.*
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.entry_id
                WHERE jl.account_code = '1097'
                  AND je.source_module = 'savings_deposit'
                ORDER BY jl.id DESC
            ''').fetchone()
            self.assertIsNotNone(line)
            self.assertAlmostEqual(float(line['debit'] or 0), 5000.0, places=2)
            self.assertIn('bank_transfer', line['memo'])

            for entry in db.execute('''
                SELECT DISTINCT je.id
                FROM journal_entries je
                JOIN journal_lines jl ON jl.entry_id = je.id
                WHERE jl.account_code = '1097'
            ''').fetchall():
                db.execute('DELETE FROM journal_lines WHERE entry_id = ?', (entry['id'],))
                db.execute('DELETE FROM journal_entries WHERE id = ?', (entry['id'],))
            db.execute("DELETE FROM accounts WHERE code = '1097'")
            db.commit()

    def test_loan_prepayment_posts_to_selected_receiving_bank_account(self):
        self.login_admin()
        member_id = self.create_member()
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM journal_lines WHERE account_code = '1096'")
            db.execute("DELETE FROM accounts WHERE code = '1096'")
            db.execute("DELETE FROM loans WHERE loan_number = 'LOAN/SEL/BANK/001'")
            db.execute('''
                INSERT INTO accounts (code, name, type, normal_balance, parent_code, is_active, is_cash_account)
                VALUES ('1096', 'Test Access Bank', 'asset', 'debit', '1000', 1, 1)
            ''')
            db.execute('''
                INSERT INTO loans
                    (loan_number, member_id, amount, purpose, tenure, interest_rate,
                     interest_method, total_repayment, balance, status, approval_stage,
                     date_applied)
                VALUES
                    ('LOAN/SEL/BANK/001', ?, 100000, 'Emergency', 6, 20,
                     'flat', 120000, 120000, 'active', 'approved',
                     '2026-08-01')
            ''', (member_id,))
            loan_id = db.execute(
                "SELECT id FROM loans WHERE loan_number = 'LOAN/SEL/BANK/001'"
            ).fetchone()['id']
            db.commit()

        response = self.client.post(f'/loans/repay/{loan_id}', data={
            'amount': '200000',
            'method': 'transfer',
            'bank_account': '1096',
        }, follow_redirects=False)
        self.assertIn(response.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            loan = db.execute(
                "SELECT balance, status FROM loans WHERE id = ?", (loan_id,)
            ).fetchone()
            self.assertAlmostEqual(float(loan['balance'] or 0), 0.0, places=2)
            self.assertEqual(loan['status'], 'completed')
            line = db.execute('''
                SELECT jl.*
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.entry_id
                WHERE jl.account_code = '1096'
                  AND je.source_module = 'loan_repayment'
                ORDER BY jl.id DESC
            ''').fetchone()
            self.assertIsNotNone(line)
            self.assertAlmostEqual(float(line['debit'] or 0), 120000.0, places=2)
            self.assertIn('transfer', line['memo'])

            for entry in db.execute('''
                SELECT DISTINCT je.id
                FROM journal_entries je
                JOIN journal_lines jl ON jl.entry_id = je.id
                WHERE jl.account_code = '1096'
            ''').fetchall():
                db.execute('DELETE FROM journal_lines WHERE entry_id = ?', (entry['id'],))
                db.execute('DELETE FROM journal_entries WHERE id = ?', (entry['id'],))
            db.execute("DELETE FROM repayments WHERE loan_id = ?", (loan_id,))
            db.execute("DELETE FROM loans WHERE id = ?", (loan_id,))
            db.execute("DELETE FROM accounts WHERE code = '1096'")
            db.commit()

    def test_admin_can_export_all_loan_statements_for_reconciliation(self):
        self.login_admin()
        member_id = self.create_member()
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM repayments WHERE loan_id IN "
                       "(SELECT id FROM loans WHERE loan_number = 'LOAN/STMT/001')")
            db.execute("DELETE FROM journal_lines WHERE entry_id IN "
                       "(SELECT id FROM journal_entries WHERE reference = 'REP/STMT/001')")
            db.execute("DELETE FROM journal_entries WHERE reference = 'REP/STMT/001'")
            db.execute("DELETE FROM loans WHERE loan_number = 'LOAN/STMT/001'")
            db.execute('''
                INSERT INTO loans
                    (loan_number, member_id, amount, purpose, tenure, interest_rate,
                     interest_method, total_repayment, balance, status, approval_stage,
                     disbursement_date, date_applied)
                VALUES
                    ('LOAN/STMT/001', ?, 100000, 'Business', 6, 20,
                     'flat', 120000, 90000, 'active', 'approved',
                     '2026-08-01', '2026-07-25')
            ''', (member_id,))
            loan_id = db.execute(
                "SELECT id FROM loans WHERE loan_number = 'LOAN/STMT/001'"
            ).fetchone()['id']
            db.execute('''
                INSERT INTO repayments
                    (repayment_number, loan_id, amount, principal_paid, interest_paid,
                     payment_method, reference, receipt_number, notes, date)
                VALUES
                    ('REP/STMT/001', ?, 30000, 25000, 5000, 'transfer',
                     'BANK/STMT/001', 'RCPT/STMT/001', 'first payment', '2026-08-31')
            ''', (loan_id,))
            db.commit()

        detail = self.client.get(f'/loans/{loan_id}')
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b'Loan Statement &amp; Corrections', detail.data)
        self.assertIn(b'REP/STMT/001', detail.data)

        response = self.client.get('/loans/export-statements')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('loan_statements_export.csv',
                      response.headers.get('Content-Disposition', ''))
        self.assertIn('member_number,member_name,member_email,loan_number', body)
        self.assertIn('LOAN/STMT/001', body)
        self.assertIn('APPLICATION', body)
        self.assertIn('LOAN_OPENED', body)
        self.assertIn('REPAYMENT', body)
        self.assertIn('REP/STMT/001', body)
        self.assertIn('90000.00', body)

        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM repayments WHERE loan_id = ?", (loan_id,))
            db.execute("DELETE FROM loans WHERE id = ?", (loan_id,))
            db.commit()

    def test_admin_can_import_approved_loan_balance_corrections(self):
        self.login_admin()
        member_id = self.create_member()
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM loan_adjustments WHERE loan_id IN "
                       "(SELECT id FROM loans WHERE loan_number = 'LOAN/CORR/001')")
            db.execute("DELETE FROM journal_lines WHERE entry_id IN "
                       "(SELECT id FROM journal_entries WHERE reference LIKE 'LOAN-CORR-OOU/TEST/0001-%')")
            db.execute("DELETE FROM journal_entries WHERE reference LIKE 'LOAN-CORR-OOU/TEST/0001-%'")
            db.execute("DELETE FROM loans WHERE loan_number = 'LOAN/CORR/001'")
            db.execute('''
                INSERT INTO loans
                    (loan_number, member_id, amount, purpose, tenure, interest_rate,
                     interest_method, total_repayment, balance, status, approval_stage,
                     disbursement_date, date_applied)
                VALUES
                    ('LOAN/CORR/001', ?, 100000, 'Regular', 6, 20,
                     'flat', 120000, 90000, 'active', 'approved',
                     '2026-08-01', '2026-07-25')
            ''', (member_id,))
            db.commit()

        csv_body = (
            'review_status,correction_type,member_number,member_name,correct_active_balance,'
            'current_coopms_balance,correction_amount,adjustment_needed_correct_less_live,'
            'correct_active_loans,current_coopms_active_loans,correct_loan_numbers,'
            'current_coopms_loan_numbers,suggested_action,officer_note\n'
            'approved_for_correction,reduce_balance_or_close,OOU/TEST/0001,Ada Audit,60000,'
            '90000,30000,-30000,1,1,SMT-CORRECT-001,LOAN/CORR/001,'
            'reduce CoopMS loan balance after approval,Reviewed by treasurer\n'
        )
        response = self.client.post(
            '/loans/corrections',
            data={'file': (BytesIO(csv_body.encode('utf-8')), 'approved_corrections.csv')},
            content_type='multipart/form-data',
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            loan = db.execute("SELECT id, balance, status FROM loans WHERE loan_number = 'LOAN/CORR/001'").fetchone()
            self.assertAlmostEqual(float(loan['balance']), 60000.0, places=2)
            self.assertEqual(loan['status'], 'active')
            adjustment = db.execute(
                "SELECT * FROM loan_adjustments WHERE loan_id = ?", (loan['id'],)
            ).fetchone()
            self.assertIsNotNone(adjustment)
            self.assertEqual(adjustment['direction'], 'decrease')
            self.assertAlmostEqual(float(adjustment['amount']), 30000.0, places=2)
            journal = db.execute(
                "SELECT * FROM journal_entries WHERE source_module = 'loan_adjustment' "
                "AND source_id = ?", (adjustment['id'],)
            ).fetchone()
            self.assertIsNotNone(journal)
            totals = db.execute(
                "SELECT COALESCE(SUM(debit),0) AS debit, COALESCE(SUM(credit),0) AS credit "
                "FROM journal_lines WHERE entry_id = ?", (journal['id'],)
            ).fetchone()
            self.assertAlmostEqual(float(totals['debit']), float(totals['credit']), places=2)

        export = self.client.get('/loans/export-statements')
        self.assertEqual(export.status_code, 200)
        body = export.get_data(as_text=True)
        self.assertIn('ADJUSTMENT', body)
        self.assertIn('LOAN/CORR/001', body)

        with self.app.app_context():
            db = get_db()
            loan = db.execute("SELECT id FROM loans WHERE loan_number = 'LOAN/CORR/001'").fetchone()
            if loan:
                adj_ids = [r['id'] for r in db.execute(
                    "SELECT id FROM loan_adjustments WHERE loan_id = ?", (loan['id'],)
                ).fetchall()]
                for adj_id in adj_ids:
                    journal_ids = [r['id'] for r in db.execute(
                        "SELECT id FROM journal_entries WHERE source_module = 'loan_adjustment' AND source_id = ?",
                        (adj_id,)
                    ).fetchall()]
                    for journal_id in journal_ids:
                        db.execute('DELETE FROM journal_lines WHERE entry_id = ?', (journal_id,))
                        db.execute('DELETE FROM journal_entries WHERE id = ?', (journal_id,))
                db.execute("DELETE FROM loan_adjustments WHERE loan_id = ?", (loan['id'],))
                db.execute("DELETE FROM loans WHERE id = ?", (loan['id'],))
                db.commit()

    def test_member_receipt_allocates_one_bank_payment_to_savings_and_loan(self):
        self.login_admin()
        member_id = self.create_member()
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM journal_lines WHERE account_code = '1095'")
            db.execute("DELETE FROM accounts WHERE code = '1095'")
            db.execute("DELETE FROM loans WHERE loan_number = 'LOAN/MR/ALLOC/001'")
            db.execute("DELETE FROM settings WHERE key = 'default_cash_account'")
            db.execute('''
                INSERT INTO accounts (code, name, type, normal_balance, parent_code, is_active, is_cash_account)
                VALUES ('1095', 'Test FCMB Bank', 'asset', 'debit', '1000', 1, 1)
            ''')
            db.execute(
                "INSERT INTO settings (key, value, description) VALUES ('default_cash_account', '1095', 'test')"
            )
            db.execute('''
                INSERT INTO loans
                    (loan_number, member_id, amount, purpose, tenure, interest_rate,
                     interest_method, total_repayment, balance, status, approval_stage,
                     date_applied)
                VALUES
                    ('LOAN/MR/ALLOC/001', ?, 500000, 'Business', 12, 10,
                     'flat', 550000, 220000, 'active', 'approved',
                     '2026-08-01')
            ''', (member_id,))
            loan_id = db.execute(
                "SELECT id FROM loans WHERE loan_number = 'LOAN/MR/ALLOC/001'"
            ).fetchone()['id']
            db.commit()

        response = self.client.post('/receipts/member-payment', data={
            'member_id': member_id,
            'bank_account': '1095',
            'amount': '360000',
            'date': '2026-09-08',
            'payment_method': 'transfer',
            'bank_reference': 'FCMB/TEST/360',
            'savings_amount': '250000',
            f'loan_amount_{loan_id}': '110000',
        }, follow_redirects=False)
        self.assertIn(response.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            receipt = db.execute(
                "SELECT * FROM member_receipts WHERE bank_reference = 'FCMB/TEST/360'"
            ).fetchone()
            self.assertIsNotNone(receipt)
            self.assertAlmostEqual(float(receipt['amount']), 360000.0, places=2)
            self.assertAlmostEqual(float(receipt['allocated_savings']), 250000.0, places=2)
            self.assertAlmostEqual(float(receipt['allocated_loans']), 110000.0, places=2)

            journal = db.execute(
                "SELECT * FROM journal_entries WHERE source_module = 'member_receipt' "
                "AND source_id = ?",
                (receipt['id'],)
            ).fetchone()
            self.assertIsNotNone(journal)
            bank_lines = db.execute(
                "SELECT * FROM journal_lines WHERE entry_id = ? AND account_code = '1095'",
                (journal['id'],)
            ).fetchall()
            self.assertEqual(len(bank_lines), 1)
            self.assertAlmostEqual(float(bank_lines[0]['debit'] or 0), 360000.0, places=2)

            saving = db.execute(
                "SELECT * FROM savings WHERE receipt_number = ?",
                (receipt['receipt_number'] + '-SAV',)
            ).fetchone()
            self.assertIsNotNone(saving)
            self.assertAlmostEqual(float(saving['amount'] or 0), 250000.0, places=2)

            repayment = db.execute(
                "SELECT * FROM repayments WHERE repayment_number = ?",
                (receipt['receipt_number'] + f'-L{loan_id}',)
            ).fetchone()
            self.assertIsNotNone(repayment)
            self.assertAlmostEqual(float(repayment['amount'] or 0), 110000.0, places=2)
            loan = db.execute("SELECT balance, status FROM loans WHERE id = ?", (loan_id,)).fetchone()
            self.assertAlmostEqual(float(loan['balance'] or 0), 110000.0, places=2)
            self.assertEqual(loan['status'], 'active')

            db.execute('DELETE FROM member_receipt_allocations WHERE receipt_id = ?', (receipt['id'],))
            db.execute('DELETE FROM member_receipts WHERE id = ?', (receipt['id'],))
            db.execute('DELETE FROM repayments WHERE loan_id = ?', (loan_id,))
            db.execute('DELETE FROM savings WHERE receipt_number = ?', (receipt['receipt_number'] + '-SAV',))
            db.execute('DELETE FROM journal_lines WHERE entry_id = ?', (journal['id'],))
            db.execute('DELETE FROM journal_entries WHERE id = ?', (journal['id'],))
            db.execute('DELETE FROM loans WHERE id = ?', (loan_id,))
            db.execute("DELETE FROM settings WHERE key = 'default_cash_account'")
            db.execute("DELETE FROM accounts WHERE code = '1095'")
            db.commit()

    def test_member_receipt_reversal_unwinds_bank_savings_and_loan(self):
        self.login_admin()
        member_id = self.create_member()
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM journal_lines WHERE account_code = '1094'")
            db.execute("DELETE FROM accounts WHERE code = '1094'")
            db.execute("DELETE FROM loans WHERE loan_number = 'LOAN/MR/REV/001'")
            db.execute("DELETE FROM member_receipt_allocations WHERE receipt_id IN "
                       "(SELECT id FROM member_receipts WHERE bank_reference = 'FCMB/TEST/REV360')")
            db.execute("DELETE FROM member_receipts WHERE bank_reference = 'FCMB/TEST/REV360'")
            db.execute("DELETE FROM settings WHERE key = 'default_cash_account'")
            db.execute("UPDATE members SET total_savings = 0, shares_value = 0 WHERE id = ?", (member_id,))
            db.execute('''
                INSERT INTO accounts (code, name, type, normal_balance, parent_code, is_active, is_cash_account)
                VALUES ('1094', 'Test Reversal Bank', 'asset', 'debit', '1000', 1, 1)
            ''')
            db.execute(
                "INSERT INTO settings (key, value, description) VALUES ('default_cash_account', '1094', 'test')"
            )
            db.execute('''
                INSERT INTO loans
                    (loan_number, member_id, amount, purpose, tenure, interest_rate,
                     interest_method, total_repayment, balance, status, approval_stage,
                     date_applied)
                VALUES
                    ('LOAN/MR/REV/001', ?, 500000, 'Business', 12, 10,
                     'flat', 550000, 110000, 'active', 'approved',
                     '2026-08-01')
            ''', (member_id,))
            loan_id = db.execute(
                "SELECT id FROM loans WHERE loan_number = 'LOAN/MR/REV/001'"
            ).fetchone()['id']
            db.commit()

        response = self.client.post('/receipts/member-payment', data={
            'member_id': member_id,
            'bank_account': '1094',
            'amount': '360000',
            'date': '2026-09-08',
            'payment_method': 'transfer',
            'bank_reference': 'FCMB/TEST/REV360',
            'savings_amount': '250000',
            f'loan_amount_{loan_id}': '110000',
        }, follow_redirects=False)
        self.assertIn(response.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            receipt = db.execute(
                "SELECT * FROM member_receipts WHERE bank_reference = 'FCMB/TEST/REV360'"
            ).fetchone()
            self.assertIsNotNone(receipt)
            original_entry_id = receipt['journal_entry_id']
            self.assertIsNotNone(original_entry_id)
            self.assertAlmostEqual(float(db.execute(
                "SELECT total_savings FROM members WHERE id = ?", (member_id,)
            ).fetchone()['total_savings'] or 0), 250000.0, places=2)
            self.assertEqual(db.execute(
                "SELECT status FROM loans WHERE id = ?", (loan_id,)
            ).fetchone()['status'], 'completed')

        reverse = self.client.post(
            f'/receipts/{receipt["id"]}/reverse',
            data={'reason': 'Bank receipt was allocated to the wrong member.'},
            follow_redirects=False,
        )
        self.assertIn(reverse.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            receipt = db.execute('SELECT * FROM member_receipts WHERE id = ?', (receipt['id'],)).fetchone()
            self.assertIsNotNone(receipt['reversed_at'])
            original = db.execute(
                'SELECT reversed_at FROM journal_entries WHERE id = ?', (original_entry_id,)
            ).fetchone()
            self.assertIsNotNone(original['reversed_at'])
            reversal = db.execute(
                'SELECT * FROM journal_entries WHERE reversal_of = ?', (original_entry_id,)
            ).fetchone()
            self.assertIsNotNone(reversal)
            bank_reversal = db.execute(
                "SELECT * FROM journal_lines WHERE entry_id = ? AND account_code = '1094'",
                (reversal['id'],)
            ).fetchone()
            self.assertIsNotNone(bank_reversal)
            self.assertAlmostEqual(float(bank_reversal['credit'] or 0), 360000.0, places=2)

            member = db.execute(
                "SELECT total_savings FROM members WHERE id = ?", (member_id,)
            ).fetchone()
            self.assertAlmostEqual(float(member['total_savings'] or 0), 0.0, places=2)
            loan = db.execute("SELECT balance, status FROM loans WHERE id = ?", (loan_id,)).fetchone()
            self.assertAlmostEqual(float(loan['balance'] or 0), 110000.0, places=2)
            self.assertEqual(loan['status'], 'active')
            repayment = db.execute(
                "SELECT reversed_at FROM repayments WHERE reference = ?",
                (receipt['receipt_number'],)
            ).fetchone()
            self.assertIsNotNone(repayment['reversed_at'])
            savings_rows = db.execute(
                "SELECT amount, payment_type FROM savings WHERE member_id = ?",
                (member_id,)
            ).fetchall()
            self.assertTrue(any(float(row['amount'] or 0) == -250000.0 and row['payment_type'] == 'reversal'
                                for row in savings_rows))

            db.execute('DELETE FROM member_receipt_allocations WHERE receipt_id = ?', (receipt['id'],))
            db.execute('DELETE FROM member_receipts WHERE id = ?', (receipt['id'],))
            db.execute('DELETE FROM repayments WHERE loan_id = ?', (loan_id,))
            db.execute('DELETE FROM savings WHERE member_id = ?', (member_id,))
            db.execute('DELETE FROM journal_lines WHERE entry_id IN (?, ?)', (original_entry_id, reversal['id']))
            db.execute('DELETE FROM journal_entries WHERE id IN (?, ?)', (original_entry_id, reversal['id']))
            db.execute('DELETE FROM loans WHERE id = ?', (loan_id,))
            db.execute("DELETE FROM settings WHERE key = 'default_cash_account'")
            db.execute("DELETE FROM accounts WHERE code = '1094'")
            db.commit()

    def test_unknown_bank_account_is_refused_not_silently_redirected(self):
        """A code that is not a cash/bank account must be rejected outright.

        Falling back to the default would post the money to a bank the officer
        did not choose and leave that account's reconciliation wrong with
        nothing on screen to say so -- the whole point of letting them pick.
        """
        from ledger import (resolve_cash_bank_account, UnknownCashAccountError,
                            get_default_cash_account)
        self.login_admin()
        member_id = self.create_member()
        with self.app.app_context():
            db = get_db()
            # 3000 Accumulated Surplus exists but is not a cash/bank account.
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM accounts WHERE code = '3000'").fetchone())
            with self.assertRaises(UnknownCashAccountError):
                resolve_cash_bank_account(db, '3000')
            with self.assertRaises(UnknownCashAccountError):
                resolve_cash_bank_account(db, '9999')          # no such account
            # Blank still means "use the default".
            self.assertEqual(resolve_cash_bank_account(db, ''),
                             get_default_cash_account(db))
            self.assertEqual(resolve_cash_bank_account(db, None),
                             get_default_cash_account(db))
            before = db.execute(
                'SELECT COUNT(*) AS n FROM savings WHERE member_id = ?',
                (member_id,)).fetchone()['n']

        r = self.client.post('/savings/add', data={
            'member_id': member_id, 'amount': '5000', 'month': '2026-09',
            'payment_type': 'voluntary', 'payment_method': 'transfer',
            'bank_account': '3000', 'notes': 'should be refused',
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'not an account money can be posted to', r.data)

        with self.app.app_context():
            db = get_db()
            # Refused before anything was written: no savings row, no entry.
            after = db.execute(
                'SELECT COUNT(*) AS n FROM savings WHERE member_id = ?',
                (member_id,)).fetchone()['n']
            self.assertEqual(after, before)
            self.assertIsNone(db.execute(
                "SELECT 1 FROM journal_lines WHERE account_code = '3000' "
                "AND memo LIKE '%should be refused%'").fetchone())

    def test_bulk_repayment_retry_skips_existing_and_imports_only_new_rows(self):
        self.login_admin()
        mid = self.create_member()
        with self.app.app_context():
            db = get_db()
            db.execute('UPDATE members SET email = NULL WHERE id = ?', (mid,))
            db.execute("INSERT INTO loans (loan_number, member_id, amount, total_repayment, balance, status) VALUES ('LOAN/RETRY/001', ?, 1000, 1000, 900, 'active')", (mid,))
            loan_id = db.execute("SELECT id FROM loans WHERE loan_number = 'LOAN/RETRY/001'").fetchone()['id']
            # A repayment posted before duplicate protection was installed.
            db.execute("INSERT INTO repayments (repayment_number, loan_id, amount, receipt_number, date) VALUES ('REP/LEGACY', ?, 100, 'RETRY-OLD', '2026-09-30')", (loan_id,))
            db.commit()

        def upload(rows):
            body = 'loan_number,amount,payment_date,bank_account,receipt_number\n' + rows
            response = self.client.post('/loans/bulk-repayments', data={
                'file': (BytesIO(body.encode()), 'retry.csv')}, content_type='multipart/form-data')
            self.assertEqual(response.status_code, 302)
            with self.app.app_context():
                db = get_db()
                return json.loads(db.execute("SELECT data FROM audit_log WHERE action = 'UPLOAD_RESULT' ORDER BY id DESC LIMIT 1").fetchone()['data'])

        old = 'LOAN/RETRY/001,100,2026-09-30,1000,RETRY-OLD\n'
        new = 'LOAN/RETRY/001,200,2026-09-30,1000,RETRY-NEW\n'
        failed = 'MISSING-RETRY,50,2026-09-30,1000,RETRY-FIX\n'
        try:
            result = upload(old + new + failed)
            self.assertEqual((result['success'], result['skipped'], len(result['errors'])), (1, 1, 1))
            self.assertEqual(result['rows'][0]['status'], 'Skipped')
            self.assertIn('Already imported', result['rows'][0]['reason'])
            # Fix only the rejected row and resend the complete file.
            result = upload(old + new + failed.replace('MISSING-RETRY', 'LOAN/RETRY/001'))
            self.assertEqual((result['success'], result['skipped'], len(result['errors'])), (1, 2, 0))
            with self.app.app_context():
                db = get_db()
                self.assertEqual(db.execute('SELECT balance FROM loans WHERE id = ?', (loan_id,)).fetchone()['balance'], 650)
                self.assertEqual(db.execute('SELECT COUNT(*) AS n FROM repayments WHERE loan_id = ?', (loan_id,)).fetchone()['n'], 3)
                self.assertEqual(db.execute("SELECT COUNT(*) AS n FROM journal_entries WHERE source_module = 'loan_repayment' AND source_id IN (SELECT id FROM repayments WHERE loan_id = ?)", (loan_id,)).fetchone()['n'], 2)
            # Repeated rows within one file, blank receipts and later periods.
            blank = 'LOAN/RETRY/001,40,2026-09-30,1000,\n'
            result = upload(blank + blank)
            self.assertEqual((result['success'], result['skipped']), (1, 1))
            result = upload(blank)
            self.assertEqual((result['success'], result['skipped']), (0, 1))
            result = upload(blank.replace('2026-09-30', '2026-10-31'))
            self.assertEqual((result['success'], result['skipped']), (1, 0))
            # Legitimate separate payments with different nonblank receipts.
            result = upload('LOAN/RETRY/001,30,2026-09-30,1000,RETRY-A\nLOAN/RETRY/001,30,2026-09-30,1000,RETRY-B\n')
            self.assertEqual((result['success'], result['skipped']), (2, 0))
            # A changed amount under the same receipt is never posted again.
            result = upload(old.replace(',100,', ',101,'))
            self.assertEqual((result['success'], result['skipped']), (0, 1))
            self.assertIn('different amount or date', result['rows'][0]['reason'])
        finally:
            with self.app.app_context():
                db = get_db()
                db.execute("DELETE FROM journal_lines WHERE entry_id IN (SELECT id FROM journal_entries WHERE source_module = 'loan_repayment' AND source_id IN (SELECT id FROM repayments WHERE loan_id = ?))", (loan_id,))
                db.execute("DELETE FROM journal_entries WHERE source_module = 'loan_repayment' AND source_id IN (SELECT id FROM repayments WHERE loan_id = ?)", (loan_id,))
                db.execute('DELETE FROM repayments WHERE loan_id = ?', (loan_id,))
                db.execute('DELETE FROM loans WHERE id = ?', (loan_id,))
                db.commit()

    def test_bulk_repayment_duplicate_receipt_on_completed_or_other_loan(self):
        self.login_admin()
        mid = self.create_member()
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO loans (loan_number, member_id, amount, total_repayment, balance, status) VALUES ('LOAN/RETRY/DONE', ?, 100, 100, 0, 'completed')", (mid,))
            loan_id = db.execute("SELECT id FROM loans WHERE loan_number = 'LOAN/RETRY/DONE'").fetchone()['id']
            db.execute("INSERT INTO repayments (repayment_number, loan_id, amount, receipt_number, date) VALUES ('REP/CAPPED/OLD', ?, 100, 'RETRY-CAPPED', '2026-09-30')", (loan_id,))
            db.execute("INSERT INTO loans (loan_number, member_id, amount, total_repayment, balance, status) VALUES ('LOAN/RETRY/OTHER', ?, 100, 100, 100, 'active')", (mid,))
            db.commit()
        try:
            body = ('loan_number,amount,payment_date,bank_account,receipt_number\n'
                    'LOAN/RETRY/DONE,120,2026-09-30,1000,RETRY-CAPPED\n'
                    'LOAN/RETRY/OTHER,100,2026-09-30,1000,RETRY-CAPPED\n')
            self.client.post('/loans/bulk-repayments', data={'file': (BytesIO(body.encode()), 'completed.csv')}, content_type='multipart/form-data')
            with self.app.app_context():
                db = get_db()
                result = json.loads(db.execute("SELECT data FROM audit_log WHERE action = 'UPLOAD_RESULT' ORDER BY id DESC LIMIT 1").fetchone()['data'])
                self.assertEqual((result['success'], result['skipped'], len(result['errors'])), (0, 1, 1))
                self.assertIn('another loan', result['errors'][0])
                self.assertEqual(db.execute("SELECT balance FROM loans WHERE loan_number = 'LOAN/RETRY/OTHER'").fetchone()['balance'], 100)
        finally:
            with self.app.app_context():
                db = get_db()
                db.execute('DELETE FROM repayments WHERE loan_id = ?', (loan_id,))
                db.execute("DELETE FROM loans WHERE loan_number IN ('LOAN/RETRY/DONE', 'LOAN/RETRY/OTHER')")
                db.commit()

    def test_upload_history_keeps_all_row_errors_after_redirect(self):
        self.login_admin()
        body = 'loan_number,amount,payment_date,bank_account\n' + ''.join(
            f'MISSING-{i},10,2026-09-30,1000\n' for i in range(9))
        response = self.client.post('/loans/bulk-repayments', data={
            'file': (BytesIO(body.encode()), '<script>upload.csv')},
            content_type='multipart/form-data')
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            row = get_db().execute("SELECT * FROM audit_log WHERE action = 'UPLOAD_RESULT' ORDER BY id DESC LIMIT 1").fetchone()
            result = json.loads(row['data'])
            self.assertEqual(result['success'], 0)
            self.assertEqual(len(result['errors']), 9)
            self.assertEqual(len(result['rows']), 9)
            self.assertEqual(result['rows'][-1]['identifier'], 'MISSING-8')
        page = self.client.get('/upload-history')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'MISSING-8', page.data)
        self.assertIn(b'&lt;script&gt;upload.csv', page.data)
        self.assertNotIn(b'<script>upload.csv', page.data)
        anonymous = self.app.test_client().get('/upload-history')
        self.assertEqual(anonymous.status_code, 302)
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET role = 'member' WHERE username = 'admin'")
            db.commit()
        try:
            self.assertEqual(self.client.get('/upload-history').status_code, 302)
        finally:
            with self.app.app_context():
                db = get_db()
                db.execute("UPDATE users SET role = 'admin' WHERE username = 'admin'")
                db.commit()

    def test_upload_history_records_rejected_file(self):
        self.login_admin()
        self.client.post('/loans/bulk-repayments', data={
            'file': (BytesIO(b'wrong,headers\n1,2\n'), 'bad.csv')},
            content_type='multipart/form-data')
        page = self.client.get('/upload-history')
        self.assertIn(b'Missing columns:', page.data)
        self.assertIn(b'bad.csv', page.data)

    def test_upload_history_paginates_and_limits_secretary_to_own_uploads(self):
        self.login_admin()
        with self.app.app_context():
            db = get_db()
            admin_id = db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()['id']
            for i in range(22):
                db.execute("INSERT INTO audit_log (user_id, username, action, module, description, data) VALUES (?, 'admin', 'UPLOAD_RESULT', 'test_history', 'test', ?)",
                           (admin_id if i == 0 else None, json.dumps(dict(filename=f'page-test-{i}.csv', success=0, errors=[]))))
            db.commit()
        try:
            first = self.client.get('/upload-history')
            self.assertIn(b'page-test-21.csv', first.data)
            self.assertNotIn(b'page-test-0.csv', first.data)
            second = self.client.get('/upload-history?page=2')
            self.assertIn(b'page-test-0.csv', second.data)
            with self.app.app_context():
                db = get_db()
                db.execute("UPDATE users SET role = 'secretary' WHERE id = ?", (admin_id,))
                db.commit()
            own = self.client.get('/upload-history')
            self.assertEqual(own.status_code, 200)
            self.assertIn(b'page-test-0.csv', own.data)
            self.assertNotIn(b'page-test-21.csv', own.data)
        finally:
            with self.app.app_context():
                db = get_db()
                db.execute("UPDATE users SET role = 'admin' WHERE id = ?", (admin_id,))
                db.execute("DELETE FROM audit_log WHERE module = 'test_history'")
                db.commit()

    def test_failed_repayment_journal_rolls_back_row_and_is_logged(self):
        self.login_admin()
        mid = self.create_member()
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO loans (loan_number, member_id, amount, total_repayment, balance, status) VALUES ('LOAN/LOG/FAIL', ?, 100, 100, 100, 'active')", (mid,))
            db.commit()
        body = 'loan_number,amount,payment_date,bank_account\nLOAN/LOG/FAIL,20,2026-09-30,1000\n'
        with patch('blueprints.loans.post_journal', side_effect=ValueError('Test journal rejection')):
            self.client.post('/loans/bulk-repayments', data={
                'file': (BytesIO(body.encode()), 'journal-fail.csv')}, content_type='multipart/form-data')
        with self.app.app_context():
            db = get_db()
            loan = db.execute("SELECT * FROM loans WHERE loan_number = 'LOAN/LOG/FAIL'").fetchone()
            self.assertEqual(loan['balance'], 100)
            self.assertIsNone(db.execute('SELECT id FROM repayments WHERE loan_id = ?', (loan['id'],)).fetchone())
            result = json.loads(db.execute("SELECT data FROM audit_log WHERE action = 'UPLOAD_RESULT' ORDER BY id DESC LIMIT 1").fetchone()['data'])
            self.assertEqual(result['success'], 0)
            self.assertIn('Test journal rejection', result['errors'][0])
            db.execute('DELETE FROM loans WHERE id = ?', (loan['id'],))
            db.commit()

    def test_bulk_repayment_control_account_debits_fund(self):
        self.login_admin()
        member_id = self.create_member()
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO accounts (code, name, type, normal_balance, is_active, is_cash_account) VALUES ('1400', 'Cooperative Fund', 'asset', 'debit', 1, 0)")
            db.execute("UPDATE members SET email = NULL WHERE id = ?", (member_id,))
            db.execute("INSERT INTO loans (loan_number, member_id, amount, total_repayment, balance, status) VALUES ('LOAN/CONTROL/001', ?, 100000, 120000, 120000, 'active')", (member_id,))
            db.commit()
        try:
            body = ('loan_number,amount,payment_date,payment_method,counter_account,receipt_number\n'
                    'LOAN/CONTROL/001,20000,2026-09-30,salary_deduction,1400,CONTROL-TEST\n')
            response = self.client.post('/loans/bulk-repayments', data={
                'bank_account': '1000', 'file': (BytesIO(body.encode()), 'repayments.csv')},
                content_type='multipart/form-data', follow_redirects=False)
            self.assertEqual(response.status_code, 302)
            with self.client.session_transaction() as session:
                self.assertTrue(any('Successfully recorded 1 loan repayments' in message
                                    for category, message in session.get('_flashes', [])))
            with self.app.app_context():
                db = get_db()
                rep = db.execute("SELECT * FROM repayments WHERE receipt_number = 'CONTROL-TEST'").fetchone()
                self.assertIsNotNone(rep)
                lines = db.execute("SELECT jl.* FROM journal_lines jl JOIN journal_entries je ON je.id = jl.entry_id WHERE je.source_module = 'loan_repayment' AND je.source_id = ?", (rep['id'],)).fetchall()
                self.assertAlmostEqual(sum(float(r['debit']) for r in lines), 20000)
                self.assertAlmostEqual(sum(float(r['credit']) for r in lines), 20000)
                self.assertEqual({r['account_code'] for r in lines if r['debit']}, {'1400'})
                self.assertEqual(db.execute("SELECT is_cash_account FROM accounts WHERE code = '1400'").fetchone()['is_cash_account'], 0)
                self.assertAlmostEqual(db.execute("SELECT balance FROM loans WHERE loan_number = 'LOAN/CONTROL/001'").fetchone()['balance'], 100000)
                result = json.loads(db.execute("SELECT data FROM audit_log WHERE action = 'UPLOAD_RESULT' ORDER BY id DESC LIMIT 1").fetchone()['data'])
                self.assertEqual(result['success'], 1)
                self.assertEqual(result['errors'], [])
                self.assertEqual(result['rows'][0]['status'], 'Imported')
        finally:
            with self.app.app_context():
                db = get_db()
                db.execute("DELETE FROM journal_lines WHERE entry_id IN (SELECT id FROM journal_entries WHERE source_module = 'loan_repayment' AND source_id IN (SELECT id FROM repayments WHERE receipt_number = 'CONTROL-TEST'))")
                db.execute("DELETE FROM journal_entries WHERE source_module = 'loan_repayment' AND source_id IN (SELECT id FROM repayments WHERE receipt_number = 'CONTROL-TEST')")
                db.execute("DELETE FROM repayments WHERE receipt_number = 'CONTROL-TEST'")
                db.execute("DELETE FROM loans WHERE loan_number = 'LOAN/CONTROL/001'")
                db.execute("DELETE FROM accounts WHERE code = '1400'")
                db.commit()

    def test_bulk_repayment_row_with_bad_bank_account_writes_nothing(self):
        """The bulk loop shares one transaction and commits after the last row,
        with no savepoint per row. So a bad account has to be caught before the
        row writes anything, or the repayment and the balance change would be
        committed while their journal entry never posted."""
        self.login_admin()
        member_id = self.create_member()
        try:
            with self.app.app_context():
                db = get_db()
                db.execute('''
                    INSERT INTO loans
                        (loan_number, member_id, amount, purpose, tenure, interest_rate,
                         interest_method, total_repayment, balance, status, approval_stage,
                         date_applied)
                    VALUES ('LOAN/BADBANK/001', ?, 100000, 'Emergency', 6, 20,
                            'flat', 120000, 120000, 'active', 'approved', '2026-08-01')
                ''', (member_id,))
                db.commit()

            csv_body = ('loan_number,amount,payment_date,payment_method,bank_account,receipt_number,notes\n'
                        'LOAN/BADBANK/001,20000,2026-09-01,transfer,1400,RCPT-BAD,should be refused\n')
            r = self.client.post('/loans/bulk-repayments', data={
                'bank_account': '1400',
                'file': (BytesIO(csv_body.encode('utf-8')), 'reps.csv')},
                content_type='multipart/form-data', follow_redirects=True)
            self.assertEqual(r.status_code, 200)
            self.assertIn(b'not an account money can be posted to', r.data)

            with self.app.app_context():
                db = get_db()
                loan = db.execute(
                    "SELECT balance, status FROM loans WHERE loan_number = 'LOAN/BADBANK/001'"
                ).fetchone()
                self.assertAlmostEqual(float(loan['balance']), 120000.0, places=2)
                self.assertEqual(loan['status'], 'active')
                self.assertIsNone(db.execute(
                    "SELECT 1 FROM repayments WHERE receipt_number = 'RCPT-BAD'").fetchone())
        finally:
            with self.app.app_context():
                db = get_db()
                row = db.execute(
                    "SELECT id FROM loans WHERE loan_number = 'LOAN/BADBANK/001'").fetchone()
                if row:
                    db.execute('DELETE FROM repayments WHERE loan_id = ?', (row['id'],))
                    db.execute('DELETE FROM loans WHERE id = ?', (row['id'],))
                db.commit()

    def test_header_cash_account_is_not_postable_and_not_double_counted(self):
        """1000 Cash & Bank is a heading once detail accounts sit under it.

        The bank report lists a parent and its children, so money posted on the
        parent would be counted twice in the cash position. It must not be
        offered as a destination, must be refused if asked for, and must stay
        out of the report's total.
        """
        from ledger import (get_cash_bank_accounts, get_postable_cash_accounts,
                            resolve_cash_bank_account, UnknownCashAccountError)
        self.login_admin()
        try:
            with self.app.app_context():
                db = get_db()
                db.execute('''
                    INSERT INTO accounts (code, name, type, normal_balance, parent_code, is_active, is_cash_account)
                    VALUES ('1095', 'Test GTB Current', 'asset', 'debit', '1000', 1, 1)
                ''')
                db.commit()

                reportable = {a['code'] for a in get_cash_bank_accounts(db)}
                postable = {a['code'] for a in get_postable_cash_accounts(db)}
                # The report still needs the parent; the selector must not have it.
                self.assertIn('1000', reportable)
                self.assertNotIn('1000', postable)
                self.assertIn('1095', postable)
                with self.assertRaises(UnknownCashAccountError):
                    resolve_cash_bank_account(db, '1000')
                self.assertEqual(resolve_cash_bank_account(db, '1095'), '1095')

            # The selector on the member page must not offer it either.
            member_id = self.create_member()
            page = self.client.get(f'/members/{member_id}')
            self.assertEqual(page.status_code, 200)
            self.assertIn(b'1095', page.data)
            self.assertNotIn(b'>1000 - Cash &amp; Bank', page.data)
        finally:
            with self.app.app_context():
                db = get_db()
                db.execute("DELETE FROM accounts WHERE code = '1095'")
                db.commit()

    def test_salary_upload_control_account_and_direct_bank_override(self):
        self.login_admin()
        mid = self.create_member()
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO accounts (code, name, type, normal_balance, is_active, is_cash_account) VALUES ('1400', 'Cooperative Fund', 'asset', 'debit', 1, 0)")
            db.commit()
        body = ('member_number,amount,month,date,counter_account,bank_account,receipt_number\n'
                'OOU/TEST/0001,10000,2026-09,2026-09-25,,,SAL-CONTROL-DEFAULT\n'
                'OOU/TEST/0001,5000,2026-09,2026-09-25,,1000,SAL-DIRECT-BANK\n'
                'OOU/TEST/0001,2000,2026-09,2026-09-25,1400,1000,SAL-CONTROL-ROW\n'
                'OOU/TEST/0001,1000,2026-09,2026-09-25,9999,,SAL-CONTROL-BAD\n')
        try:
            page = self.client.get('/savings/salary-upload')
            self.assertIn(b'name="counter_account"', page.data)
            self.assertIn(b'value="1400" selected', page.data)
            for attempt in range(2):
                response = self.client.post('/savings/salary-upload', data={
                    'month': '2026-09', 'batch_ref': 'SAL/CONTROL/TEST',
                    'file': (BytesIO(body.encode()), 'control.csv')}, content_type='multipart/form-data')
                self.assertEqual(response.status_code, 302)
                with self.app.app_context():
                    db = get_db()
                    result = json.loads(db.execute("SELECT data FROM audit_log WHERE action = 'UPLOAD_RESULT' ORDER BY id DESC LIMIT 1").fetchone()['data'])
                    self.assertEqual((result['success'], result['skipped'], len(result['errors'])), (3, 0, 1) if attempt == 0 else (0, 3, 1))
            with self.app.app_context():
                db = get_db()
                rows = db.execute("SELECT jl.* FROM journal_lines jl JOIN journal_entries je ON je.id = jl.entry_id WHERE je.reference IN ('SAL-CONTROL-DEFAULT', 'SAL-DIRECT-BANK', 'SAL-CONTROL-ROW')").fetchall()
                self.assertEqual(sum(float(r['debit']) for r in rows if r['account_code'] == '1400'), 12000)
                self.assertEqual(sum(float(r['debit']) for r in rows if r['account_code'] == '1000'), 5000)
                self.assertEqual(sum(float(r['credit']) for r in rows), 17000)
                self.assertEqual(db.execute("SELECT is_cash_account FROM accounts WHERE code = '1400'").fetchone()['is_cash_account'], 0)
                self.assertIsNone(db.execute("SELECT id FROM savings WHERE receipt_number = 'SAL-CONTROL-BAD'").fetchone())
        finally:
            with self.app.app_context():
                db = get_db()
                sums = db.execute("SELECT COALESCE(SUM(amount), 0) AS amount, COALESCE(SUM(share_capital), 0) AS shares FROM savings WHERE import_batch = 'SAL/CONTROL/TEST'").fetchone()
                db.execute('UPDATE members SET total_savings = total_savings - ?, shares_value = shares_value - ? WHERE id = ?', (sums['amount'], sums['shares'], mid))
                db.execute("DELETE FROM journal_lines WHERE entry_id IN (SELECT id FROM journal_entries WHERE reference IN ('SAL-CONTROL-DEFAULT', 'SAL-DIRECT-BANK', 'SAL-CONTROL-ROW'))")
                db.execute("DELETE FROM journal_entries WHERE reference IN ('SAL-CONTROL-DEFAULT', 'SAL-DIRECT-BANK', 'SAL-CONTROL-ROW')")
                db.execute("DELETE FROM savings WHERE import_batch = 'SAL/CONTROL/TEST'")
                db.execute("DELETE FROM accounts WHERE code = '1400'")
                db.commit()

    def test_salary_upload_posts_to_the_selected_receiving_account(self):
        """The payroll batch is the largest recurring flow, so it has to honour
        the chosen account too — and a row may name its own."""
        self.login_admin()
        member_id = self.create_member()
        month = '2026-10'
        try:
            with self.app.app_context():
                db = get_db()
                for code, name in (('1094', 'Test Batch Bank'), ('1093', 'Test Row Bank')):
                    db.execute('''
                        INSERT INTO accounts (code, name, type, normal_balance, parent_code, is_active, is_cash_account)
                        VALUES (?, ?, 'asset', 'debit', '1000', 1, 1)
                    ''', (code, name))
                member = db.execute('SELECT member_number FROM members WHERE id = ?',
                                    (member_id,)).fetchone()
                db.commit()
            num = member['member_number']

            csv_body = ('member_number,amount,month,date,bank_account,receipt_number,notes\n'
                        f'{num},10000,{month},{month}-28,,SALBANK-1,batch account\n'
                        f'{num},7000,{month},{month}-28,1093,SALBANK-2,row override\n')
            r = self.client.post('/savings/salary-upload', data={
                'month': month, 'batch_ref': 'SAL-SAV/BANKSEL/0001',
                'bank_account': '1094',
                'file': (BytesIO(csv_body.encode('utf-8')), 'payroll.csv')},
                content_type='multipart/form-data', follow_redirects=True)
            self.assertEqual(r.status_code, 200)

            with self.app.app_context():
                db = get_db()
                batch = db.execute(
                    "SELECT COALESCE(SUM(debit),0) AS d FROM journal_lines jl "
                    "JOIN journal_entries je ON je.id = jl.entry_id "
                    "WHERE jl.account_code = '1094' AND je.reference = 'SALBANK-1'"
                ).fetchone()['d']
                row = db.execute(
                    "SELECT COALESCE(SUM(debit),0) AS d FROM journal_lines jl "
                    "JOIN journal_entries je ON je.id = jl.entry_id "
                    "WHERE jl.account_code = '1093' AND je.reference = 'SALBANK-2'"
                ).fetchone()['d']
                self.assertAlmostEqual(float(batch), 10000.0, places=2)
                self.assertAlmostEqual(float(row), 7000.0, places=2)

            # A row naming a non-bank account is skipped, not redirected.
            bad = ('member_number,amount,month,date,bank_account,receipt_number\n'
                   f'{num},4000,{month},{month}-28,3000,SALBANK-3\n')
            r2 = self.client.post('/savings/salary-upload', data={
                'month': month, 'batch_ref': 'SAL-SAV/BANKSEL/0002',
                'bank_account': '1094',
                'file': (BytesIO(bad.encode('utf-8')), 'payroll2.csv')},
                content_type='multipart/form-data', follow_redirects=True)
            self.assertEqual(r2.status_code, 200)
            with self.app.app_context():
                db = get_db()
                self.assertIsNone(db.execute(
                    "SELECT 1 FROM savings WHERE receipt_number = 'SALBANK-3'").fetchone())
        finally:
            with self.app.app_context():
                db = get_db()
                for ref in ('SALBANK-1', 'SALBANK-2', 'SALBANK-3'):
                    for e in db.execute('SELECT id FROM journal_entries WHERE reference = ?',
                                        (ref,)).fetchall():
                        db.execute('DELETE FROM journal_lines WHERE entry_id = ?', (e['id'],))
                        db.execute('DELETE FROM journal_entries WHERE id = ?', (e['id'],))
                db.execute('DELETE FROM savings WHERE member_id = ? AND month = ?',
                           (member_id, month))
                db.execute("DELETE FROM accounts WHERE code IN ('1093','1094')")
                db.execute('UPDATE members SET total_savings = 0, shares_value = 0 WHERE id = ?',
                           (member_id,))
                db.commit()

    def test_disbursement_credits_the_chosen_bank_not_the_default(self):
        """Final approval pays the money out, so the approver chooses the bank.

        A cooperative running several accounts does not pay every loan from the
        same one; crediting the default would leave that bank short in the books
        and the bank that really paid untouched, so neither reconciles.
        """
        self.login_admin()
        member_id = self.create_member()
        loan_id = None
        try:
            with self.app.app_context():
                db = get_db()
                db.execute('''
                    INSERT INTO accounts (code, name, type, normal_balance, parent_code, is_active, is_cash_account)
                    VALUES ('1092', 'Test Disbursing Bank', 'asset', 'debit', '1000', 1, 1)
                ''')
                # Sitting at final approval with due diligence already done.
                db.execute('''
                    INSERT INTO loans
                        (loan_number, member_id, amount, purpose, tenure, interest_rate,
                         interest_method, total_repayment, balance, status, approval_stage,
                         date_applied, loan_applicant_type, hr_affordability_status,
                         payment_collateral_status)
                    VALUES ('LOAN/DISB/BANK/001', ?, 100000, 'Emergency', 6, 20, 'flat',
                            120000, 0, 'pending', 'president', '2026-08-01',
                            'staff', 'confirmed', 'verified')
                ''', (member_id,))
                loan_id = db.execute(
                    "SELECT id FROM loans WHERE loan_number = 'LOAN/DISB/BANK/001'"
                ).fetchone()['id']
                db.commit()

            # The approver is offered the choice on the page itself.
            page = self.client.get(f'/loans/{loan_id}')
            self.assertEqual(page.status_code, 200)
            self.assertIn(b'Disburse From', page.data)
            self.assertIn(b'1092', page.data)

            # A non-bank account must stop the approval outright.
            bad = self.client.post(f'/loans/{loan_id}/act',
                                   data={'action': 'approve', 'bank_account': '3000'},
                                   follow_redirects=True)
            self.assertEqual(bad.status_code, 200)
            with self.app.app_context():
                db = get_db()
                still = db.execute('SELECT status, approval_stage FROM loans WHERE id = ?',
                                   (loan_id,)).fetchone()
                self.assertEqual(still['status'], 'pending')
                self.assertEqual(still['approval_stage'], 'president')

            r = self.client.post(f'/loans/{loan_id}/act',
                                 data={'action': 'approve', 'bank_account': '1092'},
                                 follow_redirects=True)
            self.assertEqual(r.status_code, 200)

            with self.app.app_context():
                db = get_db()
                loan = db.execute('SELECT status, balance FROM loans WHERE id = ?',
                                  (loan_id,)).fetchone()
                self.assertEqual(loan['status'], 'active')
                # The debt is created at disbursement, not at application: the
                # request carried a zero balance until the money actually went.
                self.assertAlmostEqual(float(loan['balance'] or 0), 120000.0, places=2)
                # 100,000 less 1% insurance and 1% application fee.
                credited = db.execute('''
                    SELECT COALESCE(SUM(jl.credit), 0) AS c
                    FROM journal_lines jl
                    JOIN journal_entries je ON je.id = jl.entry_id
                    WHERE jl.account_code = '1092'
                      AND je.source_module = 'loan_disbursement'
                ''').fetchone()['c']
                self.assertAlmostEqual(float(credited), 98000.0, places=2)
                # ...and nothing landed on the default account for this loan.
                from ledger import get_default_cash_account
                default_code = get_default_cash_account(db)
                if default_code != '1092':
                    on_default = db.execute('''
                        SELECT COALESCE(SUM(jl.credit), 0) AS c
                        FROM journal_lines jl
                        JOIN journal_entries je ON je.id = jl.entry_id
                        WHERE jl.account_code = ? AND je.reference = 'LOAN/DISB/BANK/001'
                    ''', (default_code,)).fetchone()['c']
                    self.assertAlmostEqual(float(on_default), 0.0, places=2)
        finally:
            with self.app.app_context():
                db = get_db()
                if loan_id:
                    for e in db.execute(
                        "SELECT id FROM journal_entries WHERE reference = 'LOAN/DISB/BANK/001'"
                    ).fetchall():
                        db.execute('DELETE FROM journal_lines WHERE entry_id = ?', (e['id'],))
                        db.execute('DELETE FROM journal_entries WHERE id = ?', (e['id'],))
                    db.execute('DELETE FROM loan_approvals WHERE loan_id = ?', (loan_id,))
                    db.execute('DELETE FROM loan_request_events WHERE loan_id = ?', (loan_id,))
                    db.execute('DELETE FROM loans WHERE id = ?', (loan_id,))
                db.execute("DELETE FROM revenue WHERE source = 'Loan LOAN/DISB/BANK/001'")
                db.execute("DELETE FROM accounts WHERE code = '1092'")
                db.commit()

    def test_a_control_account_can_be_marked_as_one_money_moves_through(self):
        """A Cooperative Fund Account holds salary deductions the employer has
        withheld but not yet remitted, so contributions land there and the
        remittance is later Dr Bank / Cr Fund. Nothing in its name says that, so
        the treasurer marks it — the old name-matching rule could never have.
        """
        from ledger import get_cash_bank_accounts, resolve_cash_bank_account, UnknownCashAccountError
        self.login_admin()
        member_id = self.create_member()
        try:
            with self.app.app_context():
                db = get_db()
                db.execute('''
                    INSERT INTO accounts (code, name, type, normal_balance, is_active, is_cash_account)
                    VALUES ('1450', 'Test Cooperative Fund Account', 'asset', 'debit', 1, 0)
                ''')
                db.commit()
                # Unmarked, it is invisible to every money-movement screen.
                self.assertNotIn('1450', {a['code'] for a in get_cash_bank_accounts(db)})
                with self.assertRaises(UnknownCashAccountError):
                    resolve_cash_bank_account(db, '1450')

            r = self.client.post('/accounting/accounts/1450/cash-toggle', follow_redirects=True)
            self.assertEqual(r.status_code, 200)

            with self.app.app_context():
                db = get_db()
                self.assertIn('1450', {a['code'] for a in get_cash_bank_accounts(db)})
                self.assertEqual(resolve_cash_bank_account(db, '1450'), '1450')

            # And a contribution can now be recorded against it.
            self.client.post('/savings/add', data={
                'member_id': member_id, 'amount': '9000', 'month': '2026-11',
                'payment_type': 'voluntary', 'payment_method': 'salary_deduction',
                'bank_account': '1450', 'notes': 'deduction held by employer',
            }, follow_redirects=True)
            with self.app.app_context():
                db = get_db()
                posted = db.execute('''
                    SELECT COALESCE(SUM(jl.debit), 0) AS d
                    FROM journal_lines jl
                    JOIN journal_entries je ON je.id = jl.entry_id
                    WHERE jl.account_code = '1450' AND je.source_module = 'savings_deposit'
                ''').fetchone()['d']
                self.assertAlmostEqual(float(posted), 9000.0, places=2)

            # Unmarking withdraws it again.
            self.client.post('/accounting/accounts/1450/cash-toggle', follow_redirects=True)
            with self.app.app_context():
                db = get_db()
                self.assertNotIn('1450', {a['code'] for a in get_cash_bank_accounts(db)})

            # Money cannot sit in income or expense, so those cannot be marked.
            with self.app.app_context():
                db = get_db()
                db.execute('''
                    INSERT INTO accounts (code, name, type, normal_balance, is_active, is_cash_account)
                    VALUES ('4450', 'Test Some Income', 'income', 'credit', 1, 0)
                ''')
                db.commit()
            bad = self.client.post('/accounting/accounts/4450/cash-toggle', follow_redirects=True)
            self.assertIn(b'Only asset or liability accounts can hold money', bad.data)
            with self.app.app_context():
                db = get_db()
                self.assertEqual(db.execute(
                    "SELECT is_cash_account FROM accounts WHERE code = '4450'"
                ).fetchone()['is_cash_account'], 0)
        finally:
            with self.app.app_context():
                db = get_db()
                for e in db.execute(
                    "SELECT DISTINCT entry_id AS id FROM journal_lines WHERE account_code = '1450'"
                ).fetchall():
                    db.execute('DELETE FROM journal_lines WHERE entry_id = ?', (e['id'],))
                    db.execute('DELETE FROM journal_entries WHERE id = ?', (e['id'],))
                db.execute('DELETE FROM savings WHERE member_id = ? AND month = ?',
                           (member_id, '2026-11'))
                db.execute("DELETE FROM accounts WHERE code IN ('1450', '4450')")
                db.execute('UPDATE members SET total_savings = 0, shares_value = 0 WHERE id = ?',
                           (member_id,))
                db.commit()

    def test_adding_an_officer_invites_them_instead_of_setting_their_password(self):
        """An officer sets their own password from an invitation, and nobody
        else ever knows it. Every action is recorded against a name, and that is
        only worth something if a name means one person — an admin who chose the
        password could have done anything the officer is credited with.
        """
        self.login_admin()
        try:
            with self.app.app_context():
                db = get_db()
                db.execute("DELETE FROM users WHERE username IN ('newtreasurer', 'nomailofficer')")
                db.commit()

            r = self.client.post('/api/add_user', data={
                'username': 'newtreasurer', 'full_name': 'New Treasurer',
                'email': 'new.treasurer@example.com', 'role': 'treasurer',
            }, follow_redirects=True)
            self.assertEqual(r.status_code, 200)

            with self.app.app_context():
                db = get_db()
                u = db.execute(
                    'SELECT id, must_change_password FROM users WHERE username = ?',
                    ('newtreasurer',)).fetchone()
                self.assertIsNotNone(u, 'the officer was not created without a password')
                # They must set their own before they can do anything.
                self.assertEqual(u['must_change_password'], 1)
                # An invitation is waiting, whether or not the email got through.
                token = db.execute(
                    'SELECT id FROM account_setup_tokens WHERE user_id = ? AND used_at IS NULL',
                    (u['id'],)).fetchone()
                self.assertIsNotNone(token, 'no setup link was issued')

            # An officer with no email cannot be invited, so a password is handed
            # over instead — and still has to be changed at first login.
            r2 = self.client.post('/api/add_user', data={
                'username': 'nomailofficer', 'full_name': 'No Mail Officer',
                'email': '', 'role': 'exco', 'password': 'HandOver123!',
            }, follow_redirects=True)
            self.assertEqual(r2.status_code, 200)
            with self.app.app_context():
                db = get_db()
                u2 = db.execute(
                    'SELECT must_change_password FROM users WHERE username = ?',
                    ('nomailofficer',)).fetchone()
                self.assertIsNotNone(u2)
                self.assertEqual(u2['must_change_password'], 1)

            # Neither an email nor a password is refused, not half-created.
            r3 = self.client.post('/api/add_user', data={
                'username': 'nothingofficer', 'full_name': 'Nothing', 'role': 'exco',
            }, follow_redirects=True)
            self.assertEqual(r3.status_code, 200)
            with self.app.app_context():
                db = get_db()
                self.assertIsNone(db.execute(
                    "SELECT 1 FROM users WHERE username = 'nothingofficer'").fetchone())
        finally:
            with self.app.app_context():
                db = get_db()
                for name in ('newtreasurer', 'nomailofficer', 'nothingofficer'):
                    row = db.execute('SELECT id FROM users WHERE username = ?', (name,)).fetchone()
                    if row:
                        db.execute('DELETE FROM account_setup_tokens WHERE user_id = ?', (row['id'],))
                        db.execute('DELETE FROM users WHERE id = ?', (row['id'],))
                db.commit()

    def test_a_pending_request_is_not_a_debt_and_can_be_cancelled(self):
        """A loan request must not read as money owed until it is paid out, and
        an officer must be able to take it out of the queue.

        Cancelling is not rejecting: rejecting records a decision the committee
        made at a stage, cancelling withdraws the request. Recording one as the
        other misreads the member's record for as long as it is kept.
        """
        self.login_admin()
        member_id = self.create_member()
        loan_id = None
        try:
            with self.app.app_context():
                db = get_db()
                db.execute("DELETE FROM loans WHERE loan_number = 'LOAN/CANCEL/001'")
                db.execute('''
                    INSERT INTO loans
                        (loan_number, member_id, amount, purpose, tenure, interest_rate,
                         interest_method, total_repayment, balance, status, approval_stage,
                         date_applied)
                    VALUES ('LOAN/CANCEL/001', ?, 100000, 'Emergency', 6, 20, 'flat',
                            120000, 0, 'pending', 'secretary', '2026-09-01')
                ''', (member_id,))
                loan_id = db.execute(
                    "SELECT id FROM loans WHERE loan_number = 'LOAN/CANCEL/001'").fetchone()['id']
                db.execute(
                    "INSERT INTO loan_guarantors (loan_id, member_id, status) VALUES (?, ?, 'pending')",
                    (loan_id, member_id))
                db.commit()

                # A request carries no balance, so nothing it does can read as
                # owed on the member's account.
                owed = db.execute(
                    "SELECT COALESCE(SUM(balance), 0) AS b FROM loans WHERE member_id = ?",
                    (member_id,)).fetchone()['b']
                self.assertAlmostEqual(float(owed), 0.0, places=2)

            # A reason is required — a blank one changes nothing.
            self.client.post(f'/loans/{loan_id}/cancel', data={'reason': '   '},
                             follow_redirects=True)
            with self.app.app_context():
                db = get_db()
                self.assertEqual(db.execute(
                    'SELECT status FROM loans WHERE id = ?', (loan_id,)).fetchone()['status'],
                    'pending')

            r = self.client.post(f'/loans/{loan_id}/cancel',
                                 data={'reason': 'Entered twice by mistake'},
                                 follow_redirects=True)
            self.assertEqual(r.status_code, 200)

            with self.app.app_context():
                db = get_db()
                loan = db.execute(
                    'SELECT status, approval_stage, withdrawal_reason, balance '
                    'FROM loans WHERE id = ?', (loan_id,)).fetchone()
                self.assertEqual(loan['status'], 'withdrawn')
                self.assertEqual(loan['approval_stage'], 'withdrawn')
                self.assertIn('Entered twice', loan['withdrawal_reason'])
                self.assertAlmostEqual(float(loan['balance'] or 0), 0.0, places=2)
                # Guarantors are released — they stood for a request that is gone.
                self.assertIsNone(db.execute(
                    "SELECT 1 FROM loan_guarantors WHERE loan_id = ? AND status = 'pending'",
                    (loan_id,)).fetchone())
                # Nothing was posted to the books.
                self.assertIsNone(db.execute(
                    "SELECT 1 FROM journal_entries WHERE reference = 'LOAN/CANCEL/001'").fetchone())

            # A loan that has been paid out is not cancellable — it is corrected.
            with self.app.app_context():
                db = get_db()
                db.execute("UPDATE loans SET status = 'active', balance = 120000 WHERE id = ?",
                           (loan_id,))
                db.commit()
            self.client.post(f'/loans/{loan_id}/cancel', data={'reason': 'too late'},
                             follow_redirects=True)
            with self.app.app_context():
                db = get_db()
                self.assertEqual(db.execute(
                    'SELECT status FROM loans WHERE id = ?', (loan_id,)).fetchone()['status'],
                    'active')
        finally:
            with self.app.app_context():
                db = get_db()
                if loan_id:
                    db.execute('DELETE FROM loan_guarantors WHERE loan_id = ?', (loan_id,))
                    db.execute('DELETE FROM loan_approvals WHERE loan_id = ?', (loan_id,))
                    db.execute('DELETE FROM loan_request_events WHERE loan_id = ?', (loan_id,))
                    db.execute('DELETE FROM loans WHERE id = ?', (loan_id,))
                db.commit()

    def test_financial_reporting_center_and_control_exports_render(self):
        self.login_admin()
        member_id = self.create_member()
        with self.app.app_context():
            db = get_db()
            db.execute(
                "DELETE FROM savings WHERE receipt_number = 'REPORT/SAV/0001'"
            )
            db.execute('''
                INSERT INTO savings
                    (member_id, amount, month, payment_type, payment_method,
                     receipt_number, date, share_capital)
                VALUES (?, 5000, '2026-07', 'monthly', 'cash',
                        'REPORT/SAV/0001', '2026-07-21', 0)
            ''', (member_id,))
            db.commit()

        page = self.client.get('/reports')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Financial Reporting', page.data)
        self.assertIn(b'Member Savings Control', page.data)

        for url, marker in (
            ('/reports/cashbook?format=csv', b'Date,Entry #,Description'),
            ('/reports/member-savings-control?format=csv', b'Member #,Member Name,Email'),
            ('/reports/loan-portfolio?format=csv', b'Loan #,Member #,Member Name'),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn(marker, response.data)

    def test_financial_report_uses_legacy_income_fallback(self):
        with self.app.app_context():
            db = get_db()
            db.execute('''
                INSERT INTO revenue (revenue_number, category, amount, description, source, date)
                VALUES ('REV/TEST/0001', 'Other Income', 2500, 'Legacy revenue', 'Test', '2026-07-05')
            ''')
            db.execute('''
                INSERT INTO expenses (expense_number, category, amount, description, date)
                VALUES ('EXP/TEST/0001', 'Office', 700, 'Legacy expense', '2026-07-06')
            ''')
            db.commit()
            inc = income_statement(db, '2026-07-01', '2026-07-31')
            self.assertEqual(inc['total_income'], 2500.0)
            self.assertEqual(inc['total_expenses'], 700.0)
            self.assertEqual(inc['net_surplus'], 1800.0)

    def test_email_service_accepts_flask_mail_env_names(self):
        import email_service

        original_env = os.environ.copy()
        original_smtp = email_service.smtplib.SMTP

        class FakeSMTP:
            sent = []
            started_tls = False

            def __init__(self, host, port, timeout=10):
                self.host = host
                self.port = port
                self.timeout = timeout

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def ehlo(self):
                pass

            def starttls(self, context=None):
                FakeSMTP.started_tls = True

            def login(self, user, password):
                self.user = user
                self.password = password

            def sendmail(self, from_addr, recipients, message):
                FakeSMTP.sent.append((from_addr, recipients, message))

        try:
            for key in (
                'MAIL_ENABLED', 'SMTP_HOST', 'SMTP_PORT', 'SMTP_USER',
                'SMTP_PASS', 'MAIL_FROM', 'RESEND_API_KEY',
            ):
                os.environ.pop(key, None)
            os.environ.update({
                'ENABLE_EMAIL_NOTIFICATIONS': 'true',
                'MAIL_SERVER': 'smtp.example.test',
                'MAIL_PORT': '587',
                'MAIL_USERNAME': 'coop@example.test',
                'MAIL_PASSWORD': 'app-password',
                'MAIL_DEFAULT_SENDER': 'OOU Coop <coop@example.test>',
                'MAIL_USE_TLS': 'true',
            })
            email_service.smtplib.SMTP = FakeSMTP

            ok = email_service.send_email(
                'member@example.test',
                'SMTP compatibility test',
                '<p>Hello</p>',
                'Hello',
            )

            self.assertTrue(ok)
            self.assertTrue(FakeSMTP.started_tls)
            self.assertEqual(len(FakeSMTP.sent), 1)
            self.assertEqual(FakeSMTP.sent[0][0], 'OOU Coop <coop@example.test>')
            self.assertEqual(FakeSMTP.sent[0][1], ['member@example.test'])
        finally:
            os.environ.clear()
            os.environ.update(original_env)
            email_service.smtplib.SMTP = original_smtp

    def test_email_service_background_send_dispatches_wrapped_delivery(self):
        import email_service

        original_env = os.environ.copy()
        original_deliver = email_service._deliver
        calls = []

        def fake_deliver(to, subject, html, text='', attachments=None):
            calls.append((to, subject, html, text))
            return True

        try:
            for key in ('MAIL_ENABLED', 'RESEND_API_KEY', 'BREVO_API_KEY', 'SMTP_HOST'):
                os.environ.pop(key, None)
            os.environ['ENABLE_EMAIL_NOTIFICATIONS'] = 'true'
            email_service._deliver = fake_deliver

            with self.app.app_context():
                result = email_service.send_email(
                    'member@example.test', 'Async subject', '<p>Async body</p>',
                    background=True,
                )

            # A background send returns True immediately (queued). In TESTING it
            # runs inline, so delivery has already happened once by now.
            self.assertTrue(result)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], 'member@example.test')
            self.assertEqual(calls[0][1], 'Async subject')
            # The branded shell is built in-request, before dispatch.
            self.assertIn('data-coopms-email', calls[0][2])
        finally:
            os.environ.clear()
            os.environ.update(original_env)
            email_service._deliver = original_deliver

    def test_email_service_falls_back_to_smtp_when_resend_fails(self):
        import email_service

        original_env = os.environ.copy()
        original_resend = email_service._send_via_resend
        original_smtp = email_service.smtplib.SMTP

        class FakeSMTP:
            sent = []

            def __init__(self, host, port, timeout=10):
                self.host = host
                self.port = port
                self.timeout = timeout

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def ehlo(self):
                pass

            def starttls(self, context=None):
                pass

            def login(self, user, password):
                self.user = user
                self.password = password

            def sendmail(self, from_addr, recipients, message):
                FakeSMTP.sent.append((from_addr, recipients, message))

        try:
            for key in (
                'MAIL_ENABLED', 'ENABLE_EMAIL_NOTIFICATIONS', 'SMTP_HOST',
                'SMTP_PORT', 'SMTP_USER', 'SMTP_PASS', 'MAIL_FROM',
                'RESEND_API_KEY',
            ):
                os.environ.pop(key, None)
            os.environ.update({
                'MAIL_ENABLED': '1',
                'RESEND_API_KEY': 're_test_key',
                'SMTP_HOST': 'smtp.example.test',
                'SMTP_PORT': '587',
                'SMTP_USER': 'coop@example.test',
                'SMTP_PASS': 'app-password',
                'MAIL_FROM': 'OOU Coop <coop@example.test>',
                'SMTP_USE_TLS': 'true',
            })
            email_service._send_via_resend = lambda to, subject, html, attachments=None: False
            email_service.smtplib.SMTP = FakeSMTP

            ok = email_service.send_email(
                'member@example.test',
                'Fallback test',
                '<p>Hello</p>',
            )

            self.assertTrue(ok)
            self.assertEqual(len(FakeSMTP.sent), 1)
        finally:
            os.environ.clear()
            os.environ.update(original_env)
            email_service._send_via_resend = original_resend
            email_service.smtplib.SMTP = original_smtp

    def test_email_service_sends_via_brevo_api(self):
        import json
        import email_service

        original_env = os.environ.copy()
        original_urlopen = email_service.urllib.request.urlopen
        captured = {}

        class FakeResponse:
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(request, timeout=15):
            captured['url'] = request.full_url
            captured['timeout'] = timeout
            captured['headers'] = dict(request.header_items())
            captured['payload'] = json.loads(request.data.decode('utf-8'))
            return FakeResponse()

        try:
            for key in (
                'MAIL_ENABLED', 'ENABLE_EMAIL_NOTIFICATIONS', 'RESEND_API_KEY',
                'BREVO_API_KEY', 'SENDINBLUE_API_KEY', 'MAIL_FROM',
                'SMTP_HOST', 'MAIL_SERVER',
            ):
                os.environ.pop(key, None)
            os.environ.update({
                'MAIL_ENABLED': '1',
                'BREVO_API_KEY': 'xkeysib-test',
                'MAIL_FROM': 'OOU Coop <coop@example.test>',
            })
            email_service.urllib.request.urlopen = fake_urlopen

            ok = email_service.send_email(
                'member@example.test',
                'Brevo API test',
                '<p>Hello</p>',
                'Hello',
            )

            self.assertTrue(ok)
            self.assertEqual(captured['url'], 'https://api.brevo.com/v3/smtp/email')
            self.assertEqual(captured['timeout'], 15)
            self.assertEqual(captured['headers']['Api-key'], 'xkeysib-test')
            self.assertEqual(captured['payload']['sender']['email'], 'coop@example.test')
            self.assertEqual(captured['payload']['sender']['name'], 'OOU Coop')
            self.assertEqual(captured['payload']['to'], [{'email': 'member@example.test'}])
            self.assertEqual(captured['payload']['subject'], 'Brevo API test')
            self.assertEqual(captured['payload']['textContent'], 'Hello')
        finally:
            os.environ.clear()
            os.environ.update(original_env)
            email_service.urllib.request.urlopen = original_urlopen

    def test_payment_processing_uses_postgres_row_lock(self):
        from blueprints import payments_bp as payments_module

        original_flag = payments_module.USE_POSTGRES

        class FakeCursor:
            def fetchone(self):
                return {'reference': 'PAY-LOCK', 'status': 'pending'}

        class FakeDb:
            sql = ''
            params = ()

            def execute(self, sql, params=()):
                self.sql = sql
                self.params = params
                return FakeCursor()

        try:
            payments_module.USE_POSTGRES = True
            db = FakeDb()
            row = payments_module._select_pending_payment_for_processing(db, 'PAY-LOCK')
            self.assertEqual(row['reference'], 'PAY-LOCK')
            self.assertIn('FOR UPDATE', db.sql)
            self.assertEqual(db.params, ('PAY-LOCK',))
        finally:
            payments_module.USE_POSTGRES = original_flag

    def test_completed_payment_releases_lock_without_reposting(self):
        from blueprints import payments_bp as payments_module

        class FakeCursor:
            def fetchone(self):
                return {'reference': 'PAY-DONE', 'status': 'completed'}

        class FakeDb:
            rolled_back = False

            def execute(self, sql, params=()):
                return FakeCursor()

            def rollback(self):
                self.rolled_back = True

        db = FakeDb()
        processed = payments_module._record_payment(db, 'PAY-DONE')
        self.assertFalse(processed)
        self.assertTrue(db.rolled_back)

    def test_audit_log_does_not_commit_caller_transaction(self):
        from security import log_audit

        class FakeDb:
            committed = False
            executed = False

            def execute(self, sql, params=()):
                self.executed = True

            def commit(self):
                self.committed = True

        db = FakeDb()
        log_audit(db, 1, 'admin', 'TEST', 'security', 'audit test')
        self.assertTrue(db.executed)
        self.assertFalse(db.committed)

    def test_financial_references_are_unique_when_present(self):
        member_id = self.create_member()
        with self.app.app_context():
            db = get_db()

            db.execute('''
                INSERT INTO savings
                    (member_id, amount, month, payment_type, payment_method,
                     receipt_number, date)
                VALUES (?, 1000, '2026-08', 'monthly', 'cash', 'RCPT/UNIQUE/1', '2026-08-01')
            ''', (member_id,))
            with self.assertRaises(Exception):
                db.execute('''
                    INSERT INTO savings
                        (member_id, amount, month, payment_type, payment_method,
                         receipt_number, date)
                    VALUES (?, 1000, '2026-08', 'monthly', 'cash', 'RCPT/UNIQUE/1', '2026-08-01')
                ''', (member_id,))
            db.rollback()

            loan_number = 'LOAN/UNIQUE/1'
            db.execute('''
                INSERT INTO loans
                    (loan_number, member_id, amount, purpose, tenure, interest_rate,
                     total_repayment, balance, status, date_applied)
                VALUES (?, ?, 10000, 'Regular', 6, 10, 10500, 10500, 'active', '2026-08-01')
            ''', (loan_number, member_id))
            loan_id = db.execute(
                'SELECT id FROM loans WHERE loan_number = ?', (loan_number,)
            ).fetchone()['id']
            db.execute('''
                INSERT INTO repayments
                    (repayment_number, loan_id, amount, reference, date)
                VALUES ('REP/UNIQUE/1', ?, 1000, 'PAY-UNIQUE-1', '2026-08-02')
            ''', (loan_id,))
            with self.assertRaises(Exception):
                db.execute('''
                    INSERT INTO repayments
                        (repayment_number, loan_id, amount, reference, date)
                    VALUES ('REP/UNIQUE/2', ?, 1000, 'PAY-UNIQUE-1', '2026-08-02')
                ''', (loan_id,))
            db.rollback()

            db.execute('''
                INSERT INTO journal_entries
                    (entry_number, date, description, reference)
                VALUES ('JE-UNIQUE-1', '2026-08-03', 'Unique ref test', 'JREF-UNIQUE-1')
            ''')
            with self.assertRaises(Exception):
                db.execute('''
                    INSERT INTO journal_entries
                        (entry_number, date, description, reference)
                    VALUES ('JE-UNIQUE-2', '2026-08-03', 'Unique ref duplicate', 'JREF-UNIQUE-1')
            ''')
            db.rollback()

    def test_operational_fee_revenue_is_not_backfilled_twice(self):
        with self.app.app_context():
            db = get_db()
            db.execute('''
                INSERT INTO revenue
                    (revenue_number, category, amount, description, source, date)
                VALUES
                    ('REV/OPERATIONAL/MEMO/1', 'Late Fee', 500,
                     'Late fee already posted with savings journal', 'Savings', '2026-09-01')
            ''')

            posted, _ = backfill_from_transactions(db, created_by=1)
            duplicate = db.execute(
                "SELECT id FROM journal_entries WHERE reference = 'REV/OPERATIONAL/MEMO/1'"
            ).fetchone()
            rec = ledger_reconciliation(db, sample_limit=1000)
            revenue_section = next(s for s in rec['sections'] if s['label'] == 'Revenue')
            sample_refs = {r['ref'] for r in revenue_section['samples']}

            self.assertIsNone(duplicate)
            self.assertNotIn('REV/OPERATIONAL/MEMO/1', sample_refs)
            self.assertGreaterEqual(posted, 0)
            db.rollback()


    # ── Two-factor authentication + DB-backed rate limiting ──────────────────

    def _reset_2fa_state(self):
        """2FA tests share one DB file — start each from a known clean state."""
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET two_factor_secret = NULL, two_factor_enabled = 0 "
                       "WHERE username = 'admin'")
            db.execute("DELETE FROM user_backup_codes")
            db.execute("UPDATE settings SET value = '0' WHERE key = 'require_2fa'")
            db.execute("DELETE FROM login_attempts")
            db.commit()

    def _enable_admin_2fa(self):
        """Enable 2FA for the admin via the real setup route and return the
        TOTP secret plus the issued backup codes."""
        import pyotp
        self.login_admin()
        self.client.get('/security/2fa/setup')
        with self.client.session_transaction() as sess:
            secret = sess['2fa_setup_secret']
        resp = self.client.post('/security/2fa/setup',
                                data={'code': pyotp.TOTP(secret).now()},
                                follow_redirects=False)
        self.assertIn(resp.status_code, (302, 303))
        with self.app.app_context():
            db = get_db()
            row = db.execute("SELECT two_factor_enabled FROM users WHERE username = 'admin'").fetchone()
            self.assertEqual(row['two_factor_enabled'], 1)
            codes = db.execute(
                "SELECT COUNT(*) AS c FROM user_backup_codes b "
                "JOIN users u ON u.id = b.user_id WHERE u.username = 'admin'"
            ).fetchone()['c']
            self.assertEqual(codes, 10)
        return secret

    def test_login_rate_limit_is_shared_via_database(self):
        self._reset_2fa_state()
        ip = '198.51.100.77'
        with self.app.app_context():
            for _ in range(5):
                record_failed_login(ip, 'attacker')
            self.assertTrue(is_rate_limited(ip))
            rows = get_db().execute(
                'SELECT COUNT(*) AS c FROM login_attempts WHERE ip = ?', (ip,)
            ).fetchone()['c']
            self.assertEqual(rows, 5)  # persisted, not held in process memory

            clear_login_attempts(ip)
            self.assertFalse(is_rate_limited(ip))
            rows = get_db().execute(
                'SELECT COUNT(*) AS c FROM login_attempts WHERE ip = ?', (ip,)
            ).fetchone()['c']
            self.assertEqual(rows, 0)

    def test_two_factor_setup_then_login_requires_code(self):
        import pyotp
        self._reset_2fa_state()
        secret = self._enable_admin_2fa()
        self.client.get('/logout')

        # Correct password alone must NOT log in — it should defer to the code.
        resp = self.client.post('/login',
                                data={'username': 'admin', 'password': 'TestAdmin123'},
                                follow_redirects=False)
        self.assertIn(resp.status_code, (302, 303))
        self.assertIn('/login/verify', resp.headers.get('Location', ''))

        # Not actually logged in yet.
        dash = self.client.get('/dashboard', follow_redirects=False)
        self.assertIn(dash.status_code, (302, 303))
        self.assertIn('/login', dash.headers.get('Location', ''))

        # A wrong code is rejected.
        bad = self.client.post('/login/verify', data={'code': '000000'},
                               follow_redirects=False)
        self.assertEqual(bad.status_code, 200)

        # The current TOTP code completes the login.
        good = self.client.post('/login/verify', data={'code': pyotp.TOTP(secret).now()},
                                follow_redirects=False)
        self.assertIn(good.status_code, (302, 303))
        self.assertIn('/dashboard', good.headers.get('Location', ''))
        self.assertEqual(self.client.get('/dashboard').status_code, 200)
        self._reset_2fa_state()

    def test_backup_code_can_be_used_once_at_login(self):
        self._reset_2fa_state()
        self._enable_admin_2fa()
        # Grab a real backup code hash and craft a known code by regenerating
        # through the helper so we know the plaintext.
        from security import regenerate_backup_codes
        with self.app.app_context():
            db = get_db()
            uid = db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()['id']
            codes = regenerate_backup_codes(db, uid)
            db.commit()
        self.client.get('/logout')

        self.client.post('/login',
                         data={'username': 'admin', 'password': 'TestAdmin123'},
                         follow_redirects=False)
        # First use of a backup code works.
        first = self.client.post('/login/verify', data={'code': codes[0]},
                                 follow_redirects=False)
        self.assertIn('/dashboard', first.headers.get('Location', ''))

        # The same code cannot be reused.
        self.client.get('/logout')
        self.client.post('/login',
                         data={'username': 'admin', 'password': 'TestAdmin123'},
                         follow_redirects=False)
        reuse = self.client.post('/login/verify', data={'code': codes[0]},
                                 follow_redirects=False)
        self.assertEqual(reuse.status_code, 200)  # rejected, stays on verify page
        self._reset_2fa_state()

    def test_2fa_enforcement_redirects_staff_without_2fa_to_setup(self):
        self._reset_2fa_state()
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE settings SET value = '1' WHERE key = 'require_2fa'")
            db.commit()
        self.login_admin()
        resp = self.client.get('/dashboard', follow_redirects=False)
        self.assertIn(resp.status_code, (302, 303))
        self.assertIn('/security/2fa/setup', resp.headers.get('Location', ''))
        # The setup page itself must stay reachable while enforced.
        self.assertEqual(self.client.get('/security/2fa/setup').status_code, 200)
        self._reset_2fa_state()


    def test_login_records_last_login_timestamp(self):
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET last_login = NULL WHERE username = 'admin'")
            db.commit()
        self.login_admin()
        with self.app.app_context():
            row = get_db().execute(
                "SELECT last_login FROM users WHERE username = 'admin'"
            ).fetchone()
            self.assertIsNotNone(row['last_login'])


    # ── Feedback / NPS survey ────────────────────────────────────────────────

    def _reset_feedback_state(self):
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM feedback_responses")
            db.execute("UPDATE users SET feedback_dismissed_at = NULL WHERE username = 'admin'")
            db.commit()

    def test_feedback_nudge_shows_until_submitted_then_hidden(self):
        from blueprints.feedback import feedback_due
        self._reset_feedback_state()
        self.login_admin()
        with self.app.app_context():
            db = get_db()
            uid = db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()['id']
            self.assertTrue(feedback_due(db, uid))  # never asked -> due

        # The nudge card renders on the dashboard.
        page = self.client.get('/dashboard')
        self.assertIn(b'Share feedback', page.data)

        # Submit the survey with a referral opt-in.
        resp = self.client.post('/feedback/', data={
            'overall_experience': '5',
            'most_loved_feature': 'Loans',
            'improve_feature': 'Speed / performance',
            'recommend_score': '9',
            'comments': 'Great tool',
            'referral_optin': '1',
            'referral_name': 'Ada Referrer',
            'referral_email': 'ada.ref@example.com',
        }, follow_redirects=False)
        self.assertIn(resp.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            uid = db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()['id']
            row = db.execute(
                "SELECT * FROM feedback_responses WHERE user_id = ? ORDER BY id DESC", (uid,)
            ).fetchone()
            self.assertEqual(row['overall_experience'], 5)
            self.assertEqual(row['recommend_score'], 9)
            self.assertEqual(row['most_loved_feature'], 'Loans')
            self.assertEqual(row['referral_optin'], 1)
            self.assertEqual(row['referral_email'], 'ada.ref@example.com')
            self.assertFalse(feedback_due(db, uid))  # submitted -> not due

        # Nudge no longer rendered after submitting.
        page = self.client.get('/dashboard')
        self.assertNotIn(b'Share feedback', page.data)
        self._reset_feedback_state()

    def test_feedback_dismiss_snoozes_the_nudge(self):
        from blueprints.feedback import feedback_due
        self._reset_feedback_state()
        self.login_admin()
        resp = self.client.post('/feedback/dismiss', follow_redirects=False)
        self.assertIn(resp.status_code, (302, 303))
        with self.app.app_context():
            db = get_db()
            uid = db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()['id']
            self.assertFalse(feedback_due(db, uid))
        self._reset_feedback_state()

    def test_feedback_referral_not_captured_without_optin(self):
        self._reset_feedback_state()
        self.login_admin()
        self.client.post('/feedback/', data={
            'overall_experience': '4',
            'recommend_score': '7',
            'referral_name': 'Should Ignore',
            'referral_email': 'ignore@example.com',
        }, follow_redirects=False)
        with self.app.app_context():
            db = get_db()
            row = db.execute(
                "SELECT referral_optin, referral_email FROM feedback_responses ORDER BY id DESC"
            ).fetchone()
            self.assertEqual(row['referral_optin'], 0)
            self.assertEqual(row['referral_email'] or '', '')  # dropped when not opted in
        self._reset_feedback_state()

    def test_feedback_admin_and_referral_csv_export(self):
        self._reset_feedback_state()
        self.login_admin()
        self.client.post('/feedback/', data={
            'overall_experience': '5', 'recommend_score': '10',
            'referral_optin': '1', 'referral_name': 'Ref Person',
            'referral_email': 'ref@example.com',
        })
        admin_page = self.client.get('/feedback/admin')
        self.assertEqual(admin_page.status_code, 200)
        self.assertIn(b'Net Promoter Score', admin_page.data)

        csv_resp = self.client.get('/feedback/admin/referrals.csv')
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn('text/csv', csv_resp.headers.get('Content-Type', ''))
        self.assertIn(b'ref@example.com', csv_resp.data)
        self._reset_feedback_state()


    def test_loan_import_keeps_explicit_zero_balance(self):
        """A blank balance defaults to the full amount, but an explicit 0 must
        stick — needed to migrate fully-repaid (closed) loans."""
        self.login_admin()
        self.create_member()
        csv_body = (
            'member_number,loan_number,amount,purpose,tenure,total_repayment,balance,status\n'
            'OOU/TEST/0001,LN-ZERO-1,500000,Regular,12,560000,0,completed\n'
            'OOU/TEST/0001,LN-BLANK-1,500000,Regular,12,560000,,active\n'
        )
        resp = self.client.post(
            '/migration/loans',
            data={'file': (BytesIO(csv_body.encode('utf-8')), 'loans.csv')},
            content_type='multipart/form-data',
            follow_redirects=False,
        )
        self.assertIn(resp.status_code, (302, 303))
        with self.app.app_context():
            db = get_db()
            z = db.execute(
                "SELECT balance, status FROM loans WHERE loan_number = 'LN-ZERO-1'"
            ).fetchone()
            self.assertIsNotNone(z)
            self.assertEqual(float(z['balance']), 0.0)       # the fix: explicit 0 kept
            self.assertEqual(z['status'], 'completed')
            b = db.execute(
                "SELECT balance FROM loans WHERE loan_number = 'LN-BLANK-1'"
            ).fetchone()
            self.assertEqual(float(b['balance']), 560000.0)  # blank still defaults to total
            db.execute("DELETE FROM loans WHERE loan_number IN ('LN-ZERO-1','LN-BLANK-1')")
            db.commit()


    def test_mark_member_former_and_reinstate(self):
        self.login_admin()
        mid = self.create_member()
        resp = self.client.post(
            f'/members/{mid}/mark-former',
            data={'exit_reason': 'Resigned', 'exit_date': '2026-06-30',
                  'exit_note': 'left for a new job'},
            follow_redirects=False,
        )
        self.assertIn(resp.status_code, (302, 303))
        with self.app.app_context():
            m = get_db().execute(
                'SELECT status, exit_reason, exit_note FROM members WHERE id = ?', (mid,)
            ).fetchone()
            self.assertEqual(m['status'], 'former')
            self.assertEqual(m['exit_reason'], 'Resigned')
            self.assertEqual(m['exit_note'], 'left for a new job')

        resp = self.client.post(f'/members/{mid}/reinstate', follow_redirects=False)
        self.assertIn(resp.status_code, (302, 303))
        with self.app.app_context():
            m = get_db().execute(
                'SELECT status, exit_reason FROM members WHERE id = ?', (mid,)
            ).fetchone()
            self.assertEqual(m['status'], 'active')
            self.assertIsNone(m['exit_reason'])

    def test_member_import_allows_former_without_phone_and_skips_login(self):
        self.login_admin()
        csv_body = (
            'first_name,last_name,email,member_number,status,exit_reason,exit_date\n'
            'Gone,Member,gone.member@example.com,SMT/FORMER/1,former,Deceased,2023-01-15\n'
        )
        resp = self.client.post(
            '/migration/members',
            data={'file': (BytesIO(csv_body.encode('utf-8')), 'm.csv')},
            content_type='multipart/form-data',
            follow_redirects=False,
        )
        self.assertIn(resp.status_code, (302, 303))
        with self.app.app_context():
            db = get_db()
            m = db.execute(
                "SELECT * FROM members WHERE member_number = 'SMT/FORMER/1'"
            ).fetchone()
            self.assertIsNotNone(m)                          # imported with no phone
            self.assertEqual(m['status'], 'former')
            self.assertEqual(m['exit_reason'], 'Deceased')
            u = db.execute(
                "SELECT id FROM users WHERE email = 'gone.member@example.com'"
            ).fetchone()
            self.assertIsNone(u)                             # no login account for a former member
            db.execute("DELETE FROM members WHERE member_number = 'SMT/FORMER/1'")
            db.commit()


    def test_savings_import_duplicate_receipt_does_not_abort_rest(self):
        """A duplicate receipt_number must be skipped, and rows AFTER it must
        still import — regression for the PostgreSQL 'current transaction is
        aborted' cascade where one bad row failed every following row."""
        self.login_admin()
        self.create_member()  # OOU/TEST/0001

        def imp(body):
            return self.client.post(
                '/migration/savings',
                data={'file': (BytesIO(body.encode('utf-8')), 's.csv')},
                content_type='multipart/form-data', follow_redirects=True)

        imp('member_number,amount,month,receipt_number\n'
            'OOU/TEST/0001,1000,2026-06,SP-RCPT-A\n')
        # Re-import: the duplicate first, then a brand-new row that MUST still land.
        imp('member_number,amount,month,receipt_number\n'
            'OOU/TEST/0001,1000,2026-06,SP-RCPT-A\n'
            'OOU/TEST/0001,2500,2026-07,SP-RCPT-B\n')

        with self.app.app_context():
            db = get_db()
            b = db.execute("SELECT amount FROM savings WHERE receipt_number = 'SP-RCPT-B'").fetchone()
            self.assertIsNotNone(b)                       # row after the duplicate imported
            self.assertEqual(float(b['amount']), 2500.0)
            a = db.execute("SELECT COUNT(*) AS c FROM savings WHERE receipt_number = 'SP-RCPT-A'").fetchone()['c']
            self.assertEqual(a, 1)                         # duplicate not double-inserted
            db.execute("DELETE FROM savings WHERE receipt_number IN ('SP-RCPT-A','SP-RCPT-B')")
            db.execute(
                "UPDATE members SET total_savings = COALESCE("
                "(SELECT SUM(amount) FROM savings WHERE member_id = "
                "(SELECT id FROM members WHERE member_number = 'OOU/TEST/0001')), 0) "
                "WHERE member_number = 'OOU/TEST/0001'")
            db.commit()


    def _seed_savings(self, member_id, amount):
        with self.app.app_context():
            db = get_db()
            # Start clean — other tests share this member and may leave savings/loans.
            db.execute("DELETE FROM savings WHERE member_id = ?", (member_id,))
            db.execute("DELETE FROM loans WHERE member_id = ?", (member_id,))
            db.execute("INSERT INTO savings (member_id, amount, month, payment_type, "
                       "receipt_number, date) VALUES (?, ?, '2026-06', 'opening', ?, '2026-06-15')",
                       (member_id, amount, f'TST-OPEN-{member_id}'))
            db.execute("UPDATE members SET total_savings = ? WHERE id = ?", (amount, member_id))
            db.commit()

    def _cleanup_member_financials(self, member_id):
        with self.app.app_context():
            db = get_db()
            for je in db.execute("SELECT id FROM journal_entries WHERE source_module = 'savings_payout'").fetchall():
                db.execute("DELETE FROM journal_lines WHERE entry_id = ?", (je['id'],))
                db.execute("DELETE FROM journal_entries WHERE id = ?", (je['id'],))
            db.execute("DELETE FROM savings WHERE member_id = ?", (member_id,))
            db.execute("DELETE FROM loans WHERE member_id = ?", (member_id,))
            db.execute("UPDATE members SET total_savings = 0 WHERE id = ?", (member_id,))
            db.commit()

    def test_savings_payout_reduces_balance_and_posts_ledger(self):
        self.login_admin()
        mid = self.create_member()
        self._seed_savings(mid, 100000)
        resp = self.client.post('/savings/payout', data={
            'member_id': str(mid), 'amount': '40000', 'payment_method': 'bank',
            'reason': 'approved partial withdrawal',
            'evidence': (BytesIO(b'%PDF-1.4 test voucher'), 'voucher.pdf'),
        }, content_type='multipart/form-data', follow_redirects=False)
        self.assertIn(resp.status_code, (302, 303))
        with self.app.app_context():
            db = get_db()
            bal = db.execute("SELECT COALESCE(SUM(amount),0) AS b FROM savings WHERE member_id = ?", (mid,)).fetchone()['b']
            self.assertAlmostEqual(float(bal), 60000.0)          # 100k - 40k
            je = db.execute("SELECT id FROM journal_entries WHERE source_module = 'savings_payout' ORDER BY id DESC").fetchone()
            self.assertIsNotNone(je)                              # ledger entry posted
            w = db.execute("SELECT evidence_path FROM savings WHERE payment_type = 'withdrawal' AND member_id = ?", (mid,)).fetchone()
            self.assertTrue(w['evidence_path'])                  # evidence stored
        self._cleanup_member_financials(mid)

    def test_savings_payout_blocked_with_outstanding_loan(self):
        self.login_admin()
        mid = self.create_member()
        self._seed_savings(mid, 50000)
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO loans (loan_number, member_id, amount, total_repayment, "
                       "balance, status, tenure, date_applied) VALUES ('TST-LN-PAY', ?, 100000, "
                       "110000, 50000, 'active', 12, '2026-01-01')", (mid,))
            db.commit()
        resp = self.client.post('/savings/payout', data={
            'member_id': str(mid), 'amount': '10000', 'reason': 'x',
            'evidence': (BytesIO(b'%PDF-1.4'), 'v.pdf'),
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn(b'outstanding loan', resp.data)
        with self.app.app_context():
            db = get_db()
            bal = db.execute("SELECT COALESCE(SUM(amount),0) AS b FROM savings WHERE member_id = ?", (mid,)).fetchone()['b']
            self.assertAlmostEqual(float(bal), 50000.0)          # unchanged — payout blocked
        self._cleanup_member_financials(mid)

    def test_savings_payout_requires_evidence(self):
        self.login_admin()
        mid = self.create_member()
        self._seed_savings(mid, 30000)
        resp = self.client.post('/savings/payout', data={
            'member_id': str(mid), 'amount': '5000', 'reason': 'test',
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn(b'evidence', resp.data.lower())
        with self.app.app_context():
            db = get_db()
            bal = db.execute("SELECT COALESCE(SUM(amount),0) AS b FROM savings WHERE member_id = ?", (mid,)).fetchone()['b']
            self.assertAlmostEqual(float(bal), 30000.0)          # unchanged
        self._cleanup_member_financials(mid)


    def test_edit_member_blank_date_of_birth_saved_as_null(self):
        """A blank date of birth must be stored as NULL, not '' — PostgreSQL
        rejects '' for a date column (broke editing migrated members)."""
        self.login_admin()
        mid = self.create_member()
        resp = self.client.post(f'/members/edit/{mid}', data={
            'first_name': 'Ada', 'last_name': 'Audit',
            'email': 'ada.audit@example.com', 'phone': '08000000001',
            'date_of_birth': '', 'monthly_savings': '15000', 'status': 'active',
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'Error updating member', resp.data)
        with self.app.app_context():
            m = get_db().execute(
                "SELECT date_of_birth FROM members WHERE id = ?", (mid,)).fetchone()
            self.assertIsNone(m['date_of_birth'])   # blank -> NULL, not ''


    def test_edit_member_can_change_number_and_rejects_duplicate(self):
        self.login_admin()
        # Use dedicated members so the shared test member isn't disturbed.
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM members WHERE member_number IN ('NUMEDIT/1','STAFF-DUP','STAFF-999')")
            db.execute("INSERT INTO members (member_number, first_name, last_name, phone, status, "
                       "date_joined) VALUES ('NUMEDIT/1','Num','Edit','08000000009','active','2024-01-01')")
            db.execute("INSERT INTO members (member_number, first_name, last_name, status, "
                       "date_joined) VALUES ('STAFF-DUP','X','Y','active','2024-01-01')")
            db.commit()
            mid = db.execute("SELECT id FROM members WHERE member_number = 'NUMEDIT/1'").fetchone()['id']
        base = {'first_name': 'Num', 'last_name': 'Edit', 'phone': '08000000009',
                'monthly_savings': '15000', 'status': 'active'}
        # a number already in use is rejected
        r1 = self.client.post(f'/members/edit/{mid}', data={**base, 'member_number': 'STAFF-DUP'},
                              follow_redirects=True)
        self.assertIn(b'already used', r1.data)
        # a unique number is accepted
        r2 = self.client.post(f'/members/edit/{mid}', data={**base, 'member_number': 'STAFF-999'},
                              follow_redirects=True)
        self.assertNotIn(b'already used', r2.data)
        with self.app.app_context():
            db = get_db()
            m = db.execute("SELECT member_number FROM members WHERE id = ?", (mid,)).fetchone()
            self.assertEqual(m['member_number'], 'STAFF-999')
            db.execute("DELETE FROM members WHERE member_number IN ('STAFF-DUP','STAFF-999','NUMEDIT/1')")
            db.commit()


if __name__ == '__main__':
    unittest.main()
