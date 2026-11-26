"""Repayment start: payroll cut-off rule and President-only deferral. Run separately: isolated database."""
import os
import tempfile
import unittest
import uuid
from datetime import datetime
from unittest.mock import patch

_directory = tempfile.TemporaryDirectory(prefix='repayment-start-', dir=os.path.abspath('outputs'))
os.environ['SQLITE_DB_PATH'] = os.path.join(_directory.name, 'test.db')
os.environ.pop('DATABASE_URL', None)
os.environ['SECRET_KEY'] = 'repayment-start-test-key'
os.environ['ADMIN_PASSWORD'] = 'TestAdmin123'
os.environ['FLASK_DEBUG'] = '1'

from werkzeug.security import generate_password_hash

import repayment_start as rs
from app import app
from database import get_db, last_insert_id

PASSWORD = 'RepaymentStart123!'


class CutoffRule(unittest.TestCase):
    def test_issued_by_the_cutoff_starts_this_month_later_starts_next(self):
        first = rs.first_repayment_date
        self.assertEqual(first(datetime(2026, 11, 10), 15), datetime(2026, 11, 30))
        self.assertEqual(first(datetime(2026, 11, 15, 23, 59), 15), datetime(2026, 11, 30))   # the cut-off day itself
        self.assertEqual(first(datetime(2026, 11, 16), 15), datetime(2026, 12, 31))
        self.assertEqual(first(datetime(2026, 12, 20), 15), datetime(2027, 1, 31))            # across the year end
        self.assertEqual(first(datetime(2027, 1, 30), 28), datetime(2027, 2, 28))             # short month
        self.assertEqual(first('2026-11-10 08:00:00', 15), datetime(2026, 11, 30))            # as SQLite returns it

    def test_only_a_later_month_up_to_a_year_can_be_chosen(self):
        current = datetime(2026, 11, 30)
        self.assertEqual(rs.deferred_start(current, '2027-02'), datetime(2027, 2, 28))
        for bad in ('2026-11', '2026-10', '2028-01', 'January', ''):
            with self.assertRaises(ValueError, msg=bad):
                rs.deferred_start(current, bad)
        self.assertEqual(rs.deferred_start(current, '2027-11'), datetime(2027, 11, 30))       # exactly a year on

    def test_cutoff_setting_falls_back_when_missing_or_invalid(self):
        class Row(dict):
            pass

        class Db:
            def __init__(self, value):
                self.value = value

            def execute(self, *_):
                class Cursor:
                    def fetchone(inner):
                        return None if self.value is None else {'value': self.value}
                return Cursor()
        self.assertEqual(rs.cutoff_day(Db(None)), 15)
        self.assertEqual(rs.cutoff_day(Db('20')), 20)
        for bad in ('0', '29', 'abc', ''):
            self.assertEqual(rs.cutoff_day(Db(bad)), 15, bad)


class RepaymentStartRoutes(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = app.test_client()
        self.tag = uuid.uuid4().hex[:8]
        with app.app_context():
            db = get_db()
            self.member_email = f'borrower-{self.tag}@example.test'
            for role in ('treasurer', 'member'):
                username = f'{role}-{self.tag}@example.test'
                db.execute("INSERT INTO users (username,email,password_hash,role,is_active) VALUES (?,?,?,?,1)",
                           (username, self.member_email if role == 'member' else username,
                            generate_password_hash(PASSWORD), role))
            db.execute("""INSERT INTO members (member_number,first_name,last_name,email,status,annual_salary,
                monthly_savings,total_savings,date_joined) VALUES (?,?,?,?,'active',3600000,15000,500000,'2024-01-01')""",
                       (f'RS-{self.tag}', 'Rita', 'Borrower', self.member_email))
            self.member_id = last_insert_id(db)
            db.commit()

    def login(self, who):
        self.client.get('/logout')
        if who == 'admin':
            self.client.post('/login', data={'username': 'admin', 'password': 'TestAdmin123'})
        else:
            self.client.post('/login', data={'username': f'{who}-{self.tag}@example.test', 'password': PASSWORD})

    def set_cutoff(self, day):
        with app.app_context():
            get_db().execute("INSERT INTO settings (key,value) VALUES ('loan_repayment_cutoff_day',?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(day),))
            get_db().commit()

    def make_loan(self, status='pending', stage='president', member_id=None):
        with app.app_context():
            db = get_db()
            db.execute("""INSERT INTO loans (loan_number,member_id,amount,purpose,tenure,interest_rate,interest_method,
                total_repayment,balance,status,approval_stage,loan_applicant_type,hr_affordability_consent,
                hr_affordability_status,payment_collateral_type,payment_collateral_status,date_applied,disbursement_date)
                VALUES (?,?,50000,'Regular',6,11,'reducing_annual',52000,52000,?,?,'staff',1,'confirmed',
                'standing_order','verified','2026-07-20',?)""",
                       (f'RS/{uuid.uuid4().hex[:8]}', member_id or self.member_id, status, stage,
                        datetime.now() if status == 'active' else None))
            loan_id = last_insert_id(db)
            if status == 'active':
                db.execute("UPDATE loans SET first_payment_date=? WHERE id=?", (rs.first_repayment_date(datetime.now(), 15), loan_id))
            db.commit()
        return loan_id

    def loan(self, loan_id):
        with app.app_context():
            return dict(get_db().execute('SELECT * FROM loans WHERE id=?', (loan_id,)).fetchone())

    def approve(self, loan_id, **extra):
        with patch.dict(os.environ, {'ENFORCE_SEGREGATION_OF_DUTIES': '0'}):
            return self.client.post(f'/loans/{loan_id}/act', data={'action': 'approve', **extra})

    # -- disbursement applies the rule ---------------------------------------------------------
    def test_final_approval_sets_the_first_payroll_month_from_the_cutoff(self):
        for cutoff in (1, 28):
            self.set_cutoff(cutoff)
            loan_id = self.make_loan()
            self.login('admin')
            self.approve(loan_id)
            loan = self.loan(loan_id)
            self.assertEqual(loan['status'], 'active', cutoff)
            expected = rs.first_repayment_date(datetime.now(), cutoff)
            self.assertEqual(str(loan['first_payment_date'])[:10], f'{expected:%Y-%m-%d}', cutoff)
            self.assertEqual(expected.day, rs.month_end(expected).day)               # always a month end

    def test_president_can_start_repayment_later_at_final_approval(self):
        self.set_cutoff(28)
        loan_id = self.make_loan()
        self.login('admin')
        base = rs.first_repayment_date(datetime.now(), 28)
        target = rs.add_months(base.replace(day=1), 2)
        self.approve(loan_id, defer_repayment_to=f'{target:%Y-%m}')
        loan = self.loan(loan_id)
        self.assertEqual(loan['status'], 'active')
        self.assertEqual(str(loan['first_payment_date'])[:10], f'{rs.month_end(target):%Y-%m-%d}')

    def test_an_earlier_or_invalid_deferral_stops_the_approval(self):
        self.set_cutoff(28)
        loan_id = self.make_loan()
        self.login('admin')
        self.approve(loan_id, defer_repayment_to='2020-01')
        loan = self.loan(loan_id)
        self.assertEqual((loan['status'], loan['approval_stage']), ('pending', 'president'))

    # -- the deferral action ---------------------------------------------------------------------
    def defer(self, loan_id, month, reason='Member on unpaid leave until the new year'):
        return self.client.post(f'/loans/{loan_id}/defer-repayment', data={'start_month': month, 'reason': reason})

    def test_only_the_president_can_defer(self):
        loan_id = self.make_loan('active', 'approved')
        before = self.loan(loan_id)['first_payment_date']
        target = f'{rs.add_months(datetime.now(), 3):%Y-%m}'
        self.login('treasurer')
        self.defer(loan_id, target)
        self.assertEqual(self.loan(loan_id)['first_payment_date'], before)
        self.login('admin')
        self.defer(loan_id, target)
        self.assertEqual(str(self.loan(loan_id)['first_payment_date'])[:7], target)
        with app.app_context():
            trail = get_db().execute("SELECT comment FROM loan_approvals WHERE loan_id=? AND stage='repayment_start'",
                                     (loan_id,)).fetchone()
        self.assertIn('unpaid leave', trail['comment'])

    def test_deferral_rules(self):
        loan_id = self.make_loan('active', 'approved')
        self.login('admin')
        original = str(self.loan(loan_id)['first_payment_date'])
        this_month = f'{datetime.now():%Y-%m}'
        self.defer(loan_id, this_month)                                             # not later
        self.defer(loan_id, f'{rs.add_months(datetime.now(), 30):%Y-%m}')           # more than a year on
        self.defer(loan_id, f'{rs.add_months(datetime.now(), 3):%Y-%m}', reason='no')   # reason too short
        self.assertEqual(str(self.loan(loan_id)['first_payment_date']), original)

    def test_no_deferral_once_a_repayment_has_been_made(self):
        loan_id = self.make_loan('active', 'approved')
        with app.app_context():
            get_db().execute("INSERT INTO repayments (loan_id,repayment_number,amount,date) VALUES (?,?,8000,?)",
                             (loan_id, f'REP-{uuid.uuid4().hex[:6]}', datetime.now()))
            get_db().commit()
        original = str(self.loan(loan_id)['first_payment_date'])
        self.login('admin')
        self.defer(loan_id, f'{rs.add_months(datetime.now(), 3):%Y-%m}')
        self.assertEqual(str(self.loan(loan_id)['first_payment_date']), original)

    def test_president_cannot_defer_their_own_loan(self):
        with app.app_context():
            db = get_db()
            email = db.execute("SELECT email FROM users WHERE username='admin'").fetchone()['email'] or f'admin-{self.tag}@example.test'
            db.execute("UPDATE users SET email=? WHERE username='admin'", (email,))
            db.execute("INSERT INTO members (member_number,first_name,last_name,email,status) VALUES (?,?,?,?,'active')",
                       (f'RSP-{self.tag}', 'Pres', 'Ident', email))
            own = last_insert_id(db)
            db.commit()
        loan_id = self.make_loan('active', 'approved', member_id=own)
        original = str(self.loan(loan_id)['first_payment_date'])
        self.login('admin')
        self.defer(loan_id, f'{rs.add_months(datetime.now(), 3):%Y-%m}')
        self.assertEqual(str(self.loan(loan_id)['first_payment_date']), original)
        with app.app_context():
            get_db().execute("UPDATE users SET email=? WHERE username='admin'", (f'admin-{self.tag}@elsewhere.test',))
            get_db().commit()

    # -- what people see ---------------------------------------------------------------------------
    def test_pages_show_the_payroll_start_month_and_who_can_change_it(self):
        loan_id = self.make_loan('active', 'approved')
        self.login('admin')
        page = self.client.get(f'/loans/{loan_id}').text
        self.assertIn('Repayment starts', page)
        self.assertIn('Defer Repayment Start', page)
        self.login('treasurer')
        page = self.client.get(f'/loans/{loan_id}').text
        self.assertIn('Repayment starts', page)
        self.assertNotIn('Defer Repayment Start', page)
        self.login('member')
        self.assertEqual(self.client.get('/my-loans').status_code, 200)
        self.assertEqual(self.client.get(f'/loan-detail/{loan_id}').status_code, 200)

    def test_final_approval_form_explains_the_rule_and_offers_the_president_a_later_month(self):
        loan_id = self.make_loan()
        self.login('admin')
        page = self.client.get(f'/loans/{loan_id}').text
        self.assertIn('name="defer_repayment_to"', page)
        self.assertIn('payroll', page)


if __name__ == '__main__':
    unittest.main()
