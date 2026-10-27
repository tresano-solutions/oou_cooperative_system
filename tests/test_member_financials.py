import sqlite3
import unittest
from decimal import Decimal
from member_financials import statement


class MemberStatementTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE savings (id INTEGER, member_id INTEGER, date TEXT, amount REAL, share_capital REAL);
            CREATE TABLE loans (id INTEGER, member_id INTEGER, loan_number TEXT, purpose TEXT, amount REAL, total_repayment REAL, balance REAL, status TEXT, date_applied TEXT, disbursement_date TEXT);
            CREATE TABLE repayments (id INTEGER, loan_id INTEGER, amount REAL, date TEXT, reversed_at TEXT);
            CREATE TABLE loan_adjustments (id INTEGER, loan_id INTEGER, amount REAL, direction TEXT, reason TEXT, created_at TEXT, reversed_at TEXT);
            INSERT INTO savings VALUES (1,1,'2026-01-01',95000,5000),(2,1,'2026-02-01',-95000,-5000),(3,2,'2026-01-01',999,0);
            INSERT INTO loans VALUES (1,1,'LN-1','Regular',500000,550000,540000,'active','2026-01-01','2026-01-01');
            INSERT INTO loans VALUES (2,1,'LN-2','Regular',100000,110000,0,'pending','2026-01-01',NULL);
            INSERT INTO repayments VALUES (1,1,50000,'2026-01-15','2026-02-15');
            INSERT INTO loan_adjustments VALUES (1,1,10000,'decrease','Correction','2026-01-20',NULL);
        ''')

    def tearDown(self):
        self.db.close()

    def test_period_balances_include_interest_and_dated_reversals(self):
        january = statement(self.db, 1, '2026-01-01', '2026-01-31')
        self.assertEqual(january['closing'], dict(savings=Decimal(95000), shares=Decimal(5000), loan=Decimal(490000)))
        self.assertEqual(len(january['loans']), 1)
        february = statement(self.db, 1, '2026-02-01', '2026-02-28')
        self.assertEqual(february['opening'], january['closing'])
        self.assertEqual(february['closing'], dict(savings=Decimal(0), shares=Decimal(0), loan=Decimal(540000)))
        self.assertEqual(february['loans'][0]['reconciliation_difference'], 0)

    def test_member_isolation_and_invalid_dates(self):
        self.assertEqual(statement(self.db, 2, '2026-01-01', '2026-12-31')['closing']['savings'], 999)
        with self.assertRaises(ValueError):
            statement(self.db, 1, '2026-02-01', '2026-01-01')
