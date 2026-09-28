import sqlite3
import unittest

from ledger import (get_loan_repayment_counter_accounts,
                    resolve_loan_repayment_counter_account, UnknownCashAccountError)


class RepaymentControlTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE accounts (code TEXT, name TEXT, type TEXT, normal_balance TEXT, parent_code TEXT, is_active INTEGER, is_cash_account INTEGER)')
        self.db.executemany('INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?, ?)', [
            ('1000', 'Bank', 'asset', 'debit', None, 1, 1),
            ('1400', 'Cooperative Fund', 'asset', 'debit', None, 1, 0),
            ('3000', 'Surplus', 'equity', 'credit', None, 1, 0)])

    def tearDown(self):
        self.db.close()

    def test_control_is_explicit_and_does_not_become_a_bank(self):
        self.assertEqual(resolve_loan_repayment_counter_account(self.db, '1400', '1000'), '1400')
        self.assertEqual(resolve_loan_repayment_counter_account(self.db, '', '1000'), '1000')
        with self.assertRaises(UnknownCashAccountError):
            resolve_loan_repayment_counter_account(self.db, '', '1400')
        self.assertEqual(self.db.execute("SELECT is_cash_account FROM accounts WHERE code='1400'").fetchone()[0], 0)

    def test_rejects_invalid_inactive_and_heading_accounts(self):
        for code in ('9999', '3000'):
            with self.assertRaises(UnknownCashAccountError):
                resolve_loan_repayment_counter_account(self.db, code)
        self.db.execute("UPDATE accounts SET is_active=0 WHERE code='1400'")
        with self.assertRaises(UnknownCashAccountError):
            resolve_loan_repayment_counter_account(self.db, '1400')
        self.db.execute("UPDATE accounts SET is_active=1 WHERE code='1400'")
        self.db.execute("INSERT INTO accounts VALUES ('1401', 'Detail', 'asset', 'debit', '1400', 1, 0)")
        with self.assertRaises(UnknownCashAccountError):
            resolve_loan_repayment_counter_account(self.db, '1400')
        self.assertNotIn('1400', {a['code'] for a in get_loan_repayment_counter_accounts(self.db)})
