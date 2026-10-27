"""Run separately: isolated database, no production connections."""
import csv
import io
import os
import re
import tempfile
import unittest

_directory = tempfile.TemporaryDirectory(prefix='payroll-adapter-', dir=os.path.abspath('outputs'))
os.environ['SQLITE_DB_PATH'] = os.path.join(_directory.name, 'test.db')
os.environ.pop('DATABASE_URL', None)
os.environ['SECRET_KEY'] = 'payroll-adapter-test-key'
os.environ['ADMIN_PASSWORD'] = 'TestAdmin123'
os.environ['FLASK_DEBUG'] = '1'

from app import app
from database import get_db, last_insert_id


class PayrollRoutes(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = app.test_client()
        self.client.post('/login', data={'username': 'admin', 'password': 'TestAdmin123'})
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO accounts (code,name,type,normal_balance,is_active,is_cash_account) VALUES ('1400','Cooperative Fund','asset','debit',1,0) ON CONFLICT(code) DO NOTHING")
            db.execute("INSERT INTO members (first_name,last_name,member_number,status) VALUES ('Payroll','Member',?,'active')", (self._testMethodName,))
            self.member = last_insert_id(db)
            db.execute("INSERT INTO loans (member_id,loan_number,amount,total_repayment,balance,tenure,first_payment_date,status,purpose) VALUES (?,?,500000,550000,550000,6,'2026-10-01','active','School Fees')", (self.member, self._testMethodName))
            self.loan = last_insert_id(db)
            db.commit()

    def preview(self):
        response = self.client.post('/loans/payroll-adapter', data={
            'month': '2026-10', 'payment_date': '2026-10-27', 'counter_account': '1400',
            'file': (io.BytesIO(f'member_number,amount\n{self._testMethodName},91666.67\n'.encode()), 'pay.csv')})
        messages = re.findall(r'<div class="alert.*?</div>', response.text, re.S)
        self.assertTrue('name="review_token"' in response.text, str(messages))
        return response

    def test_preview_export_and_no_posting(self):
        response = self.preview()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'No repayments posted', response.data)
        token = re.search(r'name="review_token" value="([^"]+)"', response.text).group(1)
        data = dict(action='export', review_token=token, confirm_2='on')
        data[f'amount_2_{self.loan}'] = '91666.67'
        result = self.client.post('/loans/payroll-adapter', data=data)
        self.assertIn('attachment', result.headers.get('Content-Disposition', ''))
        rows = list(csv.DictReader(io.StringIO(result.text)))
        self.assertEqual(rows[0]['payment_date'], '2026-10-27')
        self.assertEqual(rows[0]['counter_account'], '1400')
        with app.app_context():
            self.assertEqual(get_db().execute('SELECT COUNT(*) FROM repayments WHERE loan_id=?', (self.loan,)).fetchone()[0], 0)
        data[f'amount_2_{self.loan}'] = '100000'
        self.assertIn(b'Allocated amounts must equal', self.client.post('/loans/payroll-adapter', data=data).data)

    def test_book_and_bad_token(self):
        self.assertEqual(self.client.get('/loans/payroll-book?month=bad').status_code, 400)
        response = self.client.get('/loans/payroll-book?month=2026-10')
        self.assertIn(b'expected_month_payment', response.data)
        self.assertIn(b'91666.67', response.data)
        response = self.client.post('/loans/payroll-adapter', data=dict(action='export', review_token='tampered'))
        self.assertNotIn('attachment', response.headers.get('Content-Disposition', ''))
        self.client.get('/logout')
        self.assertEqual(self.client.get('/loans/payroll-book').status_code, 302)

    def test_existing_month_blocks(self):
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO repayments (loan_id,repayment_number,amount,date) VALUES (?,'prior',1,'2026-10-05')", (self.loan,))
            db.commit()
        self.assertIn(b'Repayment already exists', self.preview().data)

    def test_historical_suggestions_exclude_reversed_current_and_settlement(self):
        import repayment_adapter as adapter
        with app.app_context():
            db = get_db()
            db.execute('UPDATE loans SET first_payment_date=NULL WHERE id=?', (self.loan,))
            for number, amount, date, notes, reversed_at in [
                ('hist1', 40000, '2026-08-27', '', None),
                ('hist2', 40000, '2026-09-27', '', None),
                ('reverse', 90000, '2026-09-28', '', '2026-09-29'),
                ('settled', 50000, '2026-09-29', 'Pre-liquidation - loan settled in full.', None),
                ('current', 80000, '2026-10-01', '', None),
                ('old', 10000, '2025-09-01', '', None),
            ]:
                db.execute('INSERT INTO repayments (loan_id,repayment_number,amount,date,notes,reversed_at) VALUES (?,?,?,?,?,?)',
                           (self.loan, number, amount, date, notes, reversed_at))
            db.commit()
            loan = next(l for l in adapter.loan_book(db, '2026-10') if l['id'] == self.loan)
            self.assertEqual(loan['expected'], '40000.00')
            self.assertIn('Consistent across 2', loan['basis'])
            self.assertNotIn('2026-10:', loan['basis'])

    def test_changed_balance_blocks_export(self):
        response = self.preview()
        token = re.search(r'name="review_token" value="([^"]+)"', response.text).group(1)
        with app.app_context():
            db = get_db()
            db.execute('UPDATE loans SET balance=1 WHERE id=?', (self.loan,))
            db.commit()
        data = dict(action='export', review_token=token, confirm_2='on')
        data[f'amount_2_{self.loan}'] = '91666.67'
        response = self.client.post('/loans/payroll-adapter', data=data)
        self.assertIn(b'Loan balance or status changed', response.data)
        self.assertNotIn('attachment', response.headers.get('Content-Disposition', ''))

    def test_member_denied(self):
        from unittest.mock import patch
        with patch('flask_login.utils._get_user') as user:
            user.return_value.is_authenticated = True
            user.return_value.is_anonymous = False
            user.return_value.role = 'member'
            user.return_value.id = -1
            user.return_value.is_super_admin = False
            self.assertNotEqual(self.client.get('/loans/payroll-book').status_code, 200)
            self.assertNotEqual(self.client.get('/loans/payroll-adapter').status_code, 200)


if __name__ == '__main__':
    unittest.main()
