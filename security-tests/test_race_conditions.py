"""
RACE-01: concurrent final approvals of the same loan must disburse it once.
Run:  python -m unittest security-tests.test_race_conditions -v   (from repo root)
"""
import unittest
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import _harness as H

app = H.boot_app()


class RaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.members, cls.loans = H.seed(app)

    def test_race01_double_disbursement(self):
        from database import get_db
        loan = self.loans[self.members['Alice']['id']]
        N = 8

        clients = []
        for _ in range(N):                 # log in first so the POSTs truly collide
            c = app.test_client()
            H.login(c, 'admin', H.ADMIN_PW)
            clients.append(c)

        def attempt(i):
            return clients[i].post(f"/loans/{loan['id']}/act",
                                   data={'action': 'approve', 'comment': f'r{i}'})

        results = H.parallel(N, attempt)
        with app.app_context():
            db = get_db()
            je = db.execute("SELECT COUNT(*) AS c FROM journal_entries WHERE source_module='loan_disbursement' "
                            "AND source_id = ?", (loan['id'],)).fetchone()['c']
            appr = db.execute("SELECT COUNT(*) AS c FROM loan_approvals WHERE loan_id=? AND action='approved'",
                              (loan['id'],)).fetchone()['c']
            fee = db.execute("SELECT COUNT(*) AS c FROM revenue WHERE source LIKE ?",
                             (f"Loan {loan['loan_number']}%",)).fetchone()['c']
        print(f'\nRACE-01: {N} parallel approvals -> disbursement journal entries={je}, '
              f'approval rows={appr}, fee revenue rows={fee}')
        self.assertEqual(je, 1, f'loan disbursed {je} times (expected exactly 1)')

    # ---- helpers for repayment races -------------------------------------------------
    def _activate(self, member_name, balance=55000.0):
        from database import get_db
        loan = self.loans[self.members[member_name]['id']]
        with app.app_context():
            db = get_db()
            db.execute("UPDATE loans SET status='active', approval_stage='approved', balance=? WHERE id=?",
                       (balance, loan['id']))
            db.commit()
        return loan

    def _repay_state(self, loan):
        from database import get_db
        with app.app_context():
            db = get_db()
            bal = db.execute('SELECT balance FROM loans WHERE id=?', (loan['id'],)).fetchone()['balance']
            n = db.execute('SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM repayments WHERE loan_id=?',
                           (loan['id'],)).fetchone()
        return bal, n['c'], n['s']

    def test_race02_concurrent_repayments_as_shipped(self):
        loan = self._activate('Alice')
        N = 6
        clients = []
        for _ in range(N):
            c = app.test_client(); H.login(c, 'treasurer', H.TREAS_PW); clients.append(c)
        H.parallel(N, lambda i: clients[i].post(f"/loans/repay/{loan['id']}",
                                                data={'amount': '1000', 'method': 'cash'}))
        bal, cnt, total = self._repay_state(loan)
        print(f'\nRACE-02 (as shipped): {N} parallel repayments of 1000 -> rows={cnt}, '
              f'sum recorded={total}, balance={bal} (expected balance {55000 - total})')
        self.assertAlmostEqual(bal, 55000 - total, places=2,
                               msg='loan balance does not match recorded repayments (lost update)')

    def test_race03_repayment_lost_update_without_accidental_guard(self):
        """The repayment reference is REP/<timestamp-to-the-second>/<loan>, and journal
        references are unique, so two repayments in the same second collide and one is
        rejected. That is an accident, not a lock. Give each request a distinct second
        (what happens when two requests straddle a second boundary) and look at the balance."""
        import itertools, threading
        from datetime import datetime as real_dt, timedelta
        from unittest import mock
        import blueprints.loans as bl
        loan = self._activate('Bob')
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
            H.parallel(N, lambda i: clients[i].post(f"/loans/repay/{loan['id']}",
                                                    data={'amount': '1000', 'method': 'cash'}))
        bal, cnt, total = self._repay_state(loan)
        print(f'\nRACE-03 (distinct seconds): {N} parallel repayments of 1000 -> rows={cnt}, '
              f'sum recorded={total}, balance={bal} (expected balance {55000 - total})')
        self.assertAlmostEqual(bal, 55000 - total, places=2,
                               msg='LOST UPDATE: repayments recorded but balance not reduced by all of them')


if __name__ == '__main__':
    unittest.main()
