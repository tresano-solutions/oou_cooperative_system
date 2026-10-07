"""F-06 regression: concurrent repayments keep the balance exact; a double-submitted form posts once."""
import itertools, sys, os, threading, unittest
from datetime import datetime as real_dt, timedelta
from unittest import mock
sys.path.insert(0, os.path.dirname(__file__))
import _harness as H

app = H.boot_app()


class MoneyLocking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.members, cls.loans = H.seed(app)

    def _state(self, loan):
        from database import get_db
        with app.app_context():
            db = get_db()
            bal = db.execute('SELECT balance FROM loans WHERE id=?', (loan['id'],)).fetchone()['balance']
            r = db.execute('SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM repayments WHERE loan_id=?', (loan['id'],)).fetchone()
        return bal, r['c'], r['s']

    def test_concurrent_repayments_distinct_seconds(self):
        """Same scenario as RACE-03 (clock patched so the same-second reference collision cannot mask the race)."""
        import blueprints.loans as bl
        from database import get_db
        loan = self.loans[self.members['Bob']['id']]
        with app.app_context():
            db = get_db()
            db.execute("UPDATE loans SET status='active', approval_stage='approved', balance=55000 WHERE id=?", (loan['id'],))
            db.commit()
        tick = itertools.count(1); lock = threading.Lock(); base = real_dt.now()

        class FakeDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                with lock:
                    return base + timedelta(seconds=next(tick))

        N = 6
        clients = []
        for _ in range(N):
            c = app.test_client(); H.login(c, 'treasurer', H.TREAS_PW); clients.append(c)
        with mock.patch.object(bl, 'datetime', FakeDT):
            H.parallel(N, lambda i: clients[i].post(f"/loans/repay/{loan['id']}", data={'amount': '1000', 'method': 'cash'}))
        bal, cnt, total = self._state(loan)
        self.assertEqual(cnt, N)
        self.assertAlmostEqual(bal, 55000 - total, places=2)

    def test_double_submit_savings_posts_once(self):
        from database import get_db
        mid = self.members['Alice']['id']
        c = app.test_client(); H.login(c, 'treasurer', H.TREAS_PW)
        form = {'member_id': mid, 'amount': '10000', 'month': '2026-10', 'payment_type': 'voluntary',
                'payment_method': 'cash', 'submission_token': 'tok-savings-1'}
        with app.app_context():
            n0 = get_db().execute('SELECT COUNT(*) c FROM savings WHERE member_id=?', (mid,)).fetchone()['c']
        for _ in range(2):
            c.post('/savings/add', data=form)
        with app.app_context():
            n1 = get_db().execute('SELECT COUNT(*) c FROM savings WHERE member_id=?', (mid,)).fetchone()['c']
        self.assertEqual(n1 - n0, 1)

    def test_parallel_double_submit_savings_posts_once(self):
        from database import get_db
        mid = self.members['Bob']['id']
        N = 5
        clients = []
        for _ in range(N):
            c = app.test_client(); H.login(c, 'treasurer', H.TREAS_PW); clients.append(c)
        form = {'member_id': mid, 'amount': '10000', 'month': '2026-10', 'payment_type': 'voluntary',
                'payment_method': 'cash', 'submission_token': 'tok-savings-2'}
        with app.app_context():
            n0 = get_db().execute('SELECT COUNT(*) c FROM savings WHERE member_id=?', (mid,)).fetchone()['c']
        H.parallel(N, lambda i: clients[i].post('/savings/add', data=form))
        with app.app_context():
            n1 = get_db().execute('SELECT COUNT(*) c FROM savings WHERE member_id=?', (mid,)).fetchone()['c']
        self.assertEqual(n1 - n0, 1)

    def test_double_submit_repayment_posts_once(self):
        from database import get_db
        loan = self.loans[self.members['Alice']['id']]
        with app.app_context():
            db = get_db()
            db.execute("UPDATE loans SET status='active', approval_stage='approved', balance=55000 WHERE id=?", (loan['id'],))
            db.commit()
        c = app.test_client(); H.login(c, 'treasurer', H.TREAS_PW)
        for _ in range(2):
            c.post(f"/loans/repay/{loan['id']}", data={'amount': '2000', 'method': 'cash', 'submission_token': 'tok-rep-1'})
        bal, cnt, total = self._state(loan)
        self.assertEqual((cnt, bal), (1, 53000))


if __name__ == '__main__':
    unittest.main()
