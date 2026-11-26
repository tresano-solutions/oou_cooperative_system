"""Agreed schedule vs payments. Run separately: isolated database."""
import csv
import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal

_directory = tempfile.TemporaryDirectory(prefix='loan-tracking-', dir=os.path.abspath('outputs'))
os.environ['SQLITE_DB_PATH'] = os.path.join(_directory.name, 'test.db')
os.environ.pop('DATABASE_URL', None)
os.environ['SECRET_KEY'] = 'loan-tracking-test-key'
os.environ['ADMIN_PASSWORD'] = 'TestAdmin123'
os.environ['FLASK_DEBUG'] = '1'

import loan_tracking as lt
from app import app
from database import get_db, last_insert_id
from delinquency import _add_months


def loan(**kw):
    schedule = [{'month': i, 'payment': 100, 'principal': 90, 'interest': 10, 'balance': 0} for i in range(1, 7)]
    base = dict(loan_number='LN-1', tenure=6, amount=540, interest_rate=10, interest_method='flat', total_repayment=600,
                balance=600, first_payment_date='2026-05-10', status='active', notes='', disbursement_date='2026-04-10',
                date_applied='2026-04-01', repayment_schedule_snapshot=json.dumps({'schedule': schedule}))
    base.update(kw)
    return base


def pay(date, amount):
    return {'date': date, 'amount': amount}


AS_OF = datetime(2026, 9, 20)


class TrackingLogic(unittest.TestCase):
    def test_paying_the_current_instalment_early_is_ahead(self):
        t = lt.track_loan(loan(), [pay('2026-05-27', 100), pay('2026-06-27', 100), pay('2026-07-27', 100),
                                   pay('2026-08-27', 100), pay('2026-09-12', 100)], AS_OF)
        self.assertEqual((t['status'], t['due_count'], t['gap']), ('Ahead', 4, Decimal('-100.00')))
        self.assertEqual([r['status'] for r in t['rows']], ['Paid'] * 5 + ['Upcoming'])

    def test_behind_shows_arrears_and_missed_instalments(self):
        t = lt.track_loan(loan(), [pay('2026-05-27', 100), pay('2026-06-27', 100), pay('2026-07-27', 100)], AS_OF)
        self.assertEqual((t['status'], t['gap']), ('Behind', Decimal('100.00')))
        self.assertEqual([r['status'] for r in t['rows']][:5], ['Paid', 'Paid', 'Paid', 'Missed', 'Due'])

    def test_this_months_instalment_is_not_arrears_until_month_end(self):
        # Due 10 Sep, today 20 Sep: Sep payroll has not landed yet, so nobody is "behind".
        t = lt.track_loan(loan(), [pay('2026-05-27', 100), pay('2026-06-27', 100), pay('2026-07-27', 100),
                                   pay('2026-08-27', 100)], AS_OF)
        self.assertEqual((t['status'], t['gap'], t['due_count']), ('On track', Decimal('0.00'), 4))
        self.assertEqual(t['rows'][4]['status'], 'Due')

    def test_part_payment_and_late_payment(self):
        t = lt.track_loan(loan(), [pay('2026-05-27', 100), pay('2026-06-27', 50)], AS_OF)
        self.assertEqual(t['rows'][1]['status'], 'Part-paid')
        late = lt.track_loan(loan(), [pay('2026-07-05', 100)], datetime(2026, 7, 20))
        self.assertEqual(late['rows'][0]['status'], 'Paid late')

    def test_ahead_and_future_payments_ignored(self):
        t = lt.track_loan(loan(), [pay('2026-05-27', 700), pay('2027-01-01', 999)], AS_OF)
        self.assertEqual(t['status'], 'Ahead')
        self.assertEqual(t['paid_total'], Decimal('700.00'))

    def test_migrated_loan_has_no_schedule_but_keeps_payments(self):
        t = lt.track_loan(loan(loan_number='TALLY/X', repayment_schedule_snapshot=None), [pay('2026-05-27', 50)], AS_OF)
        self.assertFalse(t['has_schedule'])
        self.assertEqual((t['status'], t['paid_total']), ('No schedule', Decimal('50.00')))
        self.assertIn('Migrated', t['source'])

    def test_recomputed_when_no_snapshot(self):
        t = lt.track_loan(loan(repayment_schedule_snapshot=None), [], AS_OF)
        self.assertTrue(t['has_schedule'])
        self.assertIn('Recomputed', t['source'])
        self.assertEqual(len(t['rows']), 6)

    def test_balance_difference_is_reported(self):
        t = lt.track_loan(loan(balance=250), [pay('2026-05-27', 100)], AS_OF)
        self.assertEqual(t['balance_gap'], Decimal('-250.00'))


class TrackingRoutes(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = app.test_client()
        self.client.post('/login', data={'username': 'admin', 'password': 'TestAdmin123'})
        now = datetime.now()
        start = _add_months(now, -3).strftime('%Y-%m-%d')
        schedule = [{'month': i, 'payment': 100, 'principal': 90, 'interest': 10, 'balance': 0} for i in range(1, 7)]
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO members (first_name,last_name,member_number,status) VALUES ('Track','Member',?,'active')",
                       (self._testMethodName,))
            self.member = last_insert_id(db)
            db.execute('''INSERT INTO loans (member_id,loan_number,amount,total_repayment,balance,tenure,first_payment_date,
                status,purpose,interest_rate,interest_method,repayment_schedule_snapshot,disbursement_date)
                VALUES (?,?,540,600,500,6,?,'active','Regular',10,'flat',?,?)''',
                       (self.member, self._testMethodName, start, json.dumps({'schedule': schedule}), start))
            self.loan = last_insert_id(db)
            db.execute("INSERT INTO repayments (loan_id,repayment_number,amount,date) VALUES (?,?,100,?)",
                       (self.loan, 'r1-' + self._testMethodName, start))
            db.execute("INSERT INTO repayments (loan_id,repayment_number,amount,date,reversed_at) VALUES (?,?,900,?,?)",
                       (self.loan, 'r2-' + self._testMethodName, start, start))
            db.commit()

    def test_tracking_page_lists_loan_as_behind_and_ignores_reversed(self):
        page = self.client.get('/loans/tracking?q=' + self._testMethodName)
        self.assertEqual(page.status_code, 200)
        self.assertIn('Repayment Tracking', page.text)
        self.assertIn('Behind', page.text)
        self.assertNotIn('900.00', page.text)
        self.assertIn('Behind 200.00', page.text)

    def test_filters_and_csv_export(self):
        self.assertIn(self._testMethodName, self.client.get('/loans/tracking?status=behind').text)
        self.assertNotIn(self._testMethodName, self.client.get('/loans/tracking?status=noschedule').text)
        result = self.client.get(f'/loans/tracking?export=1&q={self._testMethodName}')
        self.assertIn('attachment', result.headers.get('Content-Disposition', ''))
        rows = list(csv.DictReader(io.StringIO(result.text)))
        self.assertEqual((rows[0]['status'], rows[0]['paid_to_date'], rows[0]['arrears_positive_ahead_negative']),
                         ('Behind', '100.00', '200.00'))

    def test_loan_page_shows_schedule_against_payments(self):
        page = self.client.get(f'/loans/{self.loan}')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Agreed Schedule vs Payments', page.text)
        self.assertIn('Missed', page.text)
        self.assertIn('Schedule accepted by the member', page.text)

    def test_member_cannot_open_tracking(self):
        from unittest.mock import patch
        with patch('flask_login.utils._get_user') as user:
            user.return_value.is_authenticated = True
            user.return_value.is_anonymous = False
            user.return_value.role = 'member'
            user.return_value.id = -1
            user.return_value.is_super_admin = False
            self.assertNotEqual(self.client.get('/loans/tracking').status_code, 200)

    def test_tracking_is_covered_by_the_loans_view_permission(self):
        import permissions
        self.assertEqual(permissions.ENDPOINT_PERMISSIONS.get('loans.repayment_tracking'), 'loans.view')


if __name__ == '__main__':
    unittest.main()
