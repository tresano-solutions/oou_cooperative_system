"""F-05 regression: NaN / Infinity / absurd amounts are refused by money-writing routes."""
import sys, os, unittest
sys.path.insert(0, os.path.dirname(__file__))
import _harness as H

app = H.boot_app()
BAD = ['nan', 'NaN', 'inf', '-inf', 'Infinity', '1e999', '1e13']


class FiniteAmounts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.members, cls.loans = H.seed(app)

    def _client(self, user, pw):
        c = app.test_client(); H.login(c, user, pw); return c

    def _scalar(self, sql, *a):
        from database import get_db
        with app.app_context():
            return list(get_db().execute(sql, a).fetchone().values())[0]

    def test_finite_float_helper(self):
        from utils import finite_float
        for b in BAD:
            with self.assertRaises(ValueError):
                finite_float(b)
        self.assertEqual(finite_float('12,500.50'), 12500.5)

    def test_savings_add_rejects(self):
        mid = self.members['Alice']['id']
        c = self._client('treasurer', H.TREAS_PW)
        before = self._scalar('SELECT COUNT(*) FROM savings WHERE member_id=?', mid)
        for b in BAD:
            c.post('/savings/add', data={'member_id': mid, 'amount': b, 'month': '2026-10',
                                         'payment_type': 'voluntary', 'payment_method': 'cash'})
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM savings WHERE member_id=?', mid), before)
        ts = self._scalar('SELECT total_savings FROM members WHERE id=?', mid)
        self.assertEqual(ts, ts)           # not NaN

    def test_repayment_rejects(self):
        from database import get_db
        loan = self.loans[self.members['Bob']['id']]
        with app.app_context():
            db = get_db()
            db.execute("UPDATE loans SET status='active', approval_stage='approved', balance=55000 WHERE id=?", (loan['id'],))
            db.commit()
        c = self._client('treasurer', H.TREAS_PW)
        for b in BAD:
            c.post(f"/loans/repay/{loan['id']}", data={'amount': b, 'method': 'cash'})
        self.assertEqual(self._scalar('SELECT balance FROM loans WHERE id=?', loan['id']), 55000)
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM repayments WHERE loan_id=?', loan['id']), 0)

    def test_expense_and_adjust_reject(self):
        c = self._client('admin', H.ADMIN_PW)
        n = self._scalar('SELECT COUNT(*) FROM expenses')
        for b in BAD:
            c.post('/expenses/add', data={'category': 'x', 'amount': b, 'description': 'x'})
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM expenses'), n)
        mid = self.members['Alice']['id']
        for b in BAD:
            c.post('/savings/adjust', data={'member_id': mid, 'amount': b, 'reason': 'x'})
        ts = self._scalar('SELECT total_savings FROM members WHERE id=?', mid)
        self.assertEqual(ts, ts)


if __name__ == '__main__':
    unittest.main()
