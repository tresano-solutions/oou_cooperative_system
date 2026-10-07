"""
FIN-01 NaN / Infinity accepted as an amount (savings, repayment)
FIN-02 double-submit of the same savings deposit posts twice (no idempotency token)
FIN-03 one admin can push a loan through every approval stage alone (no segregation of duties)
FIN-04 money columns are binary floating point
FIN-05 savings reversal / deposit by a single user, no second approver
"""
import sys, os, unittest
sys.path.insert(0, os.path.dirname(__file__))
import _harness as H

app = H.boot_app()


class FinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.members, cls.loans = H.seed(app)

    def _treasurer(self):
        c = app.test_client(); H.login(c, 'treasurer', H.TREAS_PW); return c

    def test_fin01_nan_amounts(self):
        from database import get_db
        mid = self.members['Alice']['id']
        c = self._treasurer()
        c.post('/savings/add', data={'member_id': mid, 'amount': 'nan', 'month': '2026-10', 'payment_type': 'voluntary',
                                     'payment_method': 'cash'})
        with app.app_context():
            ts = get_db().execute('SELECT total_savings FROM members WHERE id=?', (mid,)).fetchone()['total_savings']
            n = get_db().execute("SELECT COUNT(*) AS c FROM savings WHERE member_id=?", (mid,)).fetchone()['c']
        print(f"\nFIN-01a: POST /savings/add amount='nan' -> savings rows={n}, member total_savings={ts}")
        bad = ts != ts            # NaN != NaN
        self.assertFalse(bad, 'member balance is now NaN')

    def test_fin01b_nan_repayment(self):
        from database import get_db
        loan = self.loans[self.members['Bob']['id']]
        with app.app_context():
            db = get_db()
            db.execute("UPDATE loans SET status='active', approval_stage='approved', balance=55000 WHERE id=?", (loan['id'],))
            db.commit()
        c = self._treasurer()
        c.post(f"/loans/repay/{loan['id']}", data={'amount': 'nan', 'method': 'cash'})
        with app.app_context():
            bal = get_db().execute('SELECT balance FROM loans WHERE id=?', (loan['id'],)).fetchone()['balance']
        print(f'FIN-01b: POST /loans/repay amount=nan -> loan balance={bal}')
        self.assertFalse(bal != bal, 'loan balance is now NaN')

    def test_fin02_double_submit(self):
        from database import get_db
        mid = self.members['Bob']['id']
        with app.app_context():
            n0 = get_db().execute('SELECT COUNT(*) AS c FROM savings WHERE member_id=?', (mid,)).fetchone()['c']
        c = self._treasurer()
        form = {'member_id': mid, 'amount': '10000', 'month': '2026-10', 'payment_type': 'voluntary', 'payment_method': 'cash'}
        for _ in range(2):
            c.post('/savings/add', data=form)
        with app.app_context():
            n1 = get_db().execute('SELECT COUNT(*) AS c FROM savings WHERE member_id=?', (mid,)).fetchone()['c']
        print(f'FIN-02: identical savings form submitted twice -> {n1 - n0} deposit rows created')
        self.assertEqual(n1 - n0, 1)

    def test_fin03_single_admin_all_stages(self):
        from database import get_db
        loan = self.loans[self.members['Alice']['id']]
        with app.app_context():
            db = get_db()
            db.execute("UPDATE loans SET status='pending', approval_stage='secretary' WHERE id=?", (loan['id'],))
            db.commit()
        c = app.test_client(); H.login(c, 'admin', H.ADMIN_PW)
        for _ in range(3):
            c.post(f"/loans/{loan['id']}/act", data={'action': 'approve'})
        with app.app_context():
            db = get_db()
            row = db.execute('SELECT status, approval_stage FROM loans WHERE id=?', (loan['id'],)).fetchone()
            who = db.execute("SELECT stage, acted_by_name FROM loan_approvals WHERE loan_id=? AND action='approved' ORDER BY id",
                             (loan['id'],)).fetchall()
        print(f"FIN-03: status={row['status']} stage={row['approval_stage']}; approvals recorded by: "
              f"{[(w['stage'], w['acted_by_name']) for w in who]}")
        self.assertFalse(row['status'] == 'active' and len({w['acted_by_name'] for w in who}) == 1,
                         'a single user approved every stage and the loan was disbursed')

    def test_fin04_float_columns(self):
        from database import get_db
        with app.app_context():
            rows = get_db().execute("""SELECT table_name, column_name, data_type FROM information_schema.columns
                WHERE table_schema='public' AND data_type IN ('double precision','real')
                AND (column_name LIKE '%amount%' OR column_name LIKE '%balance%' OR column_name LIKE '%savings%'
                     OR column_name IN ('debit','credit','total_repayment','late_fee')) ORDER BY 1,2""").fetchall()
        print(f'FIN-04: {len(rows)} money columns stored as binary floating point, e.g.',
              [(r['table_name'], r['column_name']) for r in rows[:8]])
        self.assertEqual(len(rows), 0)
        # 0.1+0.2 style drift on a running balance
        bal = 0.0
        for _ in range(1000):
            bal += 0.1
        print('FIN-04: 1000 additions of 0.10 =', repr(bal), '(should be 100.0)')


if __name__ == '__main__':
    unittest.main()
