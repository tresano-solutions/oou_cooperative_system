"""Refunding the application fee that disbursement charged without being told to.

The money leaves the bank, so the things worth pinning are that it leaves once,
lands in the right accounts, and can be put back.
"""

import os
import unittest

TEST_DB = os.path.abspath('.test-fee-refunds.db')
os.environ.setdefault('SECRET_KEY', 'test-secret-refunds')
os.environ.setdefault('ADMIN_PASSWORD', 'TestAdmin123')
os.environ.setdefault('FLASK_DEBUG', '1')
os.environ.setdefault('FIELD_ENCRYPTION_KEY', '05SmPJhNFMKwg9NysnBdQjKtqn3VwWDl1IiPIMAg2as=')
os.environ.pop('DATABASE_URL', None)
os.environ['SQLITE_DB_PATH'] = TEST_DB
try:
    os.remove(TEST_DB)
except FileNotFoundError:
    pass

import app as app_module                                   # noqa: E402
from database import get_db, last_insert_id                # noqa: E402
from crypto import encrypt_field                           # noqa: E402
from ledger import (FEE_INCOME, account_balance,           # noqa: E402
                    get_postable_cash_accounts, reverse_journal_entry,
                    reversal_support)
from blueprints.loans import _fee_refund_rows              # noqa: E402

_SEQ = 0


class FeeRefundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app_module.app
        cls.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    def setUp(self):
        self.client = self.app.test_client()
        self.client.post('/login', data={'username': 'admin', 'password': 'TestAdmin123'})

    def tearDown(self):
        with self.app.app_context():
            db = get_db()
            db.execute('DELETE FROM loan_fee_refunds')
            db.execute("DELETE FROM revenue WHERE revenue_number LIKE 'REV/REFUND/%'")
            db.execute("DELETE FROM loans WHERE loan_number LIKE 'LN/FR/%'")
            db.execute("DELETE FROM members WHERE member_number LIKE 'FR/%'")
            db.commit()

    def _loan(self, fee=5000.0, amount=500000.0, status='active'):
        global _SEQ
        _SEQ += 1
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO members (member_number, first_name, last_name, status, "
                "date_joined, email, bank_name, account_name, account_number) VALUES "
                "(?, 'Fee', 'Refund', 'active', '2024-01-01', ?, ?, ?, ?)",
                (f'FR/{_SEQ}', f'fr{_SEQ}@example.test', encrypt_field('GTBank'),
                 encrypt_field('Fee Refund'), encrypt_field('0123456789')))
            mid = last_insert_id(db)
            db.execute(
                "INSERT INTO loans (loan_number, member_id, amount, purpose, tenure, "
                "interest_rate, total_repayment, balance, status, application_fee, "
                "insurance_premium, disbursed_amount, disbursement_date, date_applied) "
                "VALUES (?, ?, ?, 'Regular', 12, 11, ?, ?, ?, ?, 5000, ?, "
                "'2026-06-01', '2026-05-01')",
                (f'LN/FR/{_SEQ}', mid, amount, amount * 1.11, amount * 1.11, status,
                 fee, amount - fee - 5000))
            loan_id = last_insert_id(db)
            db.commit()
            return loan_id

    def _bank(self):
        with self.app.app_context():
            return get_postable_cash_accounts(get_db())[0]['code']

    def _pay(self, loan_id, bank=None):
        return self.client.post('/loans/fee-refunds/pay',
                                data={'cash_account': bank or self._bank(),
                                      'loan_ids': [str(loan_id)]},
                                follow_redirects=True)

    # ── What the refund does to the books ────────────────────────────────────

    def test_refund_reverses_income_and_takes_cash_out(self):
        loan_id = self._loan(fee=5000)
        bank = self._bank()
        with self.app.app_context():
            db = get_db()
            fee_before, cash_before = account_balance(db, FEE_INCOME), account_balance(db, bank)

        self._pay(loan_id, bank)

        with self.app.app_context():
            db = get_db()
            # Debit to Fee Income: the cooperative gives back income never due.
            self.assertAlmostEqual(account_balance(db, FEE_INCOME) - fee_before, 5000, places=2)
            # Credit to the bank: the money leaves.
            self.assertAlmostEqual(account_balance(db, bank) - cash_before, -5000, places=2)

    def test_the_original_income_row_is_offset_not_deleted(self):
        loan_id = self._loan(fee=5000)
        self._pay(loan_id)
        with self.app.app_context():
            db = get_db()
            row = db.execute('SELECT amount FROM revenue WHERE revenue_number = ?',
                             (f'REV/REFUND/{loan_id}',)).fetchone()
            self.assertIsNotNone(row, 'a contra revenue row should net off the original')
            self.assertAlmostEqual(float(row['amount']), -5000, places=2)

    def test_the_bank_it_was_paid_from_is_recorded(self):
        loan_id = self._loan()
        bank = self._bank()
        self._pay(loan_id, bank)
        with self.app.app_context():
            row = get_db().execute('SELECT bank_account FROM loan_fee_refunds WHERE loan_id = ?',
                                   (loan_id,)).fetchone()
            self.assertEqual(row['bank_account'], bank)

    # ── Paying twice ─────────────────────────────────────────────────────────

    def test_a_member_cannot_be_refunded_twice(self):
        loan_id = self._loan(fee=5000)
        bank = self._bank()
        self._pay(loan_id, bank)
        with self.app.app_context():
            cash_after_first = account_balance(get_db(), bank)

        self._pay(loan_id, bank)

        with self.app.app_context():
            db = get_db()
            rows = db.execute('SELECT COUNT(*) AS n FROM loan_fee_refunds WHERE loan_id = ?',
                              (loan_id,)).fetchone()['n']
            self.assertEqual(rows, 1, 'a second payout must not be recorded')
            self.assertAlmostEqual(account_balance(db, bank), cash_after_first, places=2,
                                   msg='no further money may leave the bank')

    def test_a_refunded_loan_drops_off_the_outstanding_list(self):
        loan_id = self._loan()
        self._pay(loan_id)
        with self.app.app_context():
            listed = [r['id'] for r in _fee_refund_rows(get_db())]
            self.assertNotIn(loan_id, listed)

    # ── Refusals ─────────────────────────────────────────────────────────────

    def test_an_unknown_bank_account_refunds_nothing(self):
        loan_id = self._loan()
        self.client.post('/loans/fee-refunds/pay',
                         data={'cash_account': '9999', 'loan_ids': [str(loan_id)]},
                         follow_redirects=True)
        with self.app.app_context():
            n = get_db().execute('SELECT COUNT(*) AS n FROM loan_fee_refunds').fetchone()['n']
            self.assertEqual(n, 0)

    def test_selecting_nobody_refunds_nothing(self):
        self._loan()
        self.client.post('/loans/fee-refunds/pay',
                         data={'cash_account': self._bank()}, follow_redirects=True)
        with self.app.app_context():
            n = get_db().execute('SELECT COUNT(*) AS n FROM loan_fee_refunds').fetchone()['n']
            self.assertEqual(n, 0)

    # ── Undoing one ──────────────────────────────────────────────────────────

    def test_a_refund_can_be_undone_and_becomes_owed_again(self):
        loan_id = self._loan(fee=5000)
        bank = self._bank()
        self._pay(loan_id, bank)

        self.assertEqual(reversal_support('loan_fee_refund')[0], True)

        with self.app.app_context():
            db = get_db()
            entry = db.execute(
                "SELECT id FROM journal_entries WHERE source_module = 'loan_fee_refund' "
                "AND source_id = ?", (loan_id,)).fetchone()
            cash_before = account_balance(db, bank)
            reverse_journal_entry(db, entry['id'], reason='Paid the wrong member')
            db.commit()

            # The money comes back.
            self.assertAlmostEqual(account_balance(db, bank) - cash_before, 5000, places=2)
            # The refund is marked, never deleted.
            row = db.execute('SELECT status, reversed_at FROM loan_fee_refunds '
                             'WHERE loan_id = ?', (loan_id,)).fetchone()
            self.assertEqual(row['status'], 'reversed')
            self.assertIsNotNone(row['reversed_at'])
            # And the fee is owed again.
            self.assertIn(loan_id, [r['id'] for r in _fee_refund_rows(db)])


if __name__ == '__main__':
    unittest.main()
