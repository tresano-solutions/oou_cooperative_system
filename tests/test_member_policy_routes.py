"""Run separately: uses its own temporary database and Flask application."""
import os
import tempfile
import unittest
from unittest.mock import patch

_directory = tempfile.TemporaryDirectory(prefix='coopms-policy-', dir=os.path.abspath('outputs'))
os.environ['SQLITE_DB_PATH'] = os.path.join(_directory.name, 'test.db')
os.environ.pop('DATABASE_URL', None)
os.environ['SECRET_KEY'] = 'isolated-policy-route-test-secret'
os.environ['ADMIN_PASSWORD'] = 'TestAdmin123'
os.environ['FLASK_DEBUG'] = '1'

from app import app
from database import get_db, last_insert_id


class PolicyRouteTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = app.test_client()
        self.client.post('/login', data=dict(username='admin', password='TestAdmin123'))
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO members (first_name,last_name,phone,email,date_joined,monthly_savings,status) VALUES ('Test','Member','123',?,'2020-01-01',5000,'active')", (self._testMethodName + '@example.test',))
            self.member_id = last_insert_id(db)
            db.execute("INSERT INTO settings (key,value) VALUES ('loan_catalog','standard') ON CONFLICT(key) DO UPDATE SET value='standard'")
            db.commit()

    def test_smt_settings_preview_and_forms(self):
        response = self.client.post('/settings/update', data={'loan_catalog': 'smt'})
        self.assertEqual(response.status_code, 302)
        for months, total in [(12, 537500), (13, 545000), (24, 545000)]:
            response = self.client.get(f'/loan-schedule-preview?amount=500000&tenure={months}&purpose=Regular')
            self.assertEqual(response.json['total_repayment'], total)
        self.assertFalse(self.client.get('/loan-schedule-preview?amount=500000&tenure=25&purpose=Regular').json['ok'])
        for purpose in ('School Fees', 'Asset Purchase'):
            result = self.client.get('/loan-schedule-preview', query_string=dict(amount=500000, tenure=6, purpose=purpose)).json
            self.assertEqual(result['total_interest'], 50000)
            self.assertEqual(result['total_repayment'], 550000)
        self.assertFalse(self.client.get('/loan-schedule-preview?amount=500000&tenure=6&purpose=Housing').json['ok'])
        response = self.client.get('/loans/apply')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Back to school', response.data)
        self.assertNotIn(b'<option value="Housing">', response.data)
        self.assertEqual(self.client.get('/settings').status_code, 200)

    def test_statement_export_and_history(self):
        base = f'/members/{self.member_id}'
        self.assertEqual(self.client.get(base + '/financial-statement').status_code, 200)
        response = self.client.get(base + '/financial-statement?format=csv')
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response.headers['Content-Disposition'])
        self.assertIn(b'Loan movement', response.data)
        self.assertEqual(self.client.get(base + '/financial-statement?from_date=bad').status_code, 400)
        self.assertEqual(self.client.get(base + '/savings-history').status_code, 200)
        with patch('flask_login.utils._get_user') as user:
            user.return_value.is_authenticated = True
            user.return_value.is_anonymous = False
            user.return_value.role = 'member'
            user.return_value.id = -1
            user.return_value.is_super_admin = False
            self.assertNotEqual(self.client.get(base + '/financial-statement').status_code, 200)

    def test_annual_flat_preview_uses_settings(self):
        self.client.post('/settings/update', data=dict(interest_regular='20', interest_method_regular='flat'))
        response = self.client.get('/loan-schedule-preview?amount=500000&tenure=6&purpose=Regular')
        self.assertEqual(response.json['total_interest'], 50000)
        self.assertEqual(response.json['total_repayment'], 550000)

    def test_direct_savings_edit_is_audited(self):
        response = self.client.post(f'/members/edit/{self.member_id}', data=dict(first_name='Test', last_name='Member', phone='123', email='policy@example.test', monthly_savings='7000', status='active'))
        self.assertEqual(response.status_code, 302)
        response = self.client.get(f'/members/{self.member_id}/savings-history')
        self.assertIn(b'7000', response.data)
        self.assertIn(b'admin', response.data)
