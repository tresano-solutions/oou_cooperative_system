"""F-07 regression: no one approves two stages of one loan; DD officer cannot give final approval."""
import sys, os, unittest
sys.path.insert(0, os.path.dirname(__file__))
import _harness as H

app = H.boot_app()


class SoD(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.members, cls.loans = H.seed(app)

    def _loan(self, name, stage):
        from database import get_db
        loan = self.loans[self.members[name]['id']]
        with app.app_context():
            db = get_db()
            db.execute("UPDATE loans SET status='pending', approval_stage=?, due_diligence_updated_by=NULL WHERE id=?", (stage, loan['id']))
            db.execute("DELETE FROM loan_approvals WHERE loan_id=?", (loan['id'],))
            # a previous test may have disbursed this loan; clear its ledger entry (unique reference)
            db.execute("DELETE FROM journal_lines WHERE entry_id IN (SELECT id FROM journal_entries WHERE reference=?)", (loan['loan_number'],))
            db.execute("DELETE FROM journal_entries WHERE reference=?", (loan['loan_number'],))
            db.commit()
        return loan['id']

    def _state(self, lid):
        from database import get_db
        with app.app_context():
            r = get_db().execute('SELECT status, approval_stage FROM loans WHERE id=?', (lid,)).fetchone()
        return r['status'], r['approval_stage']

    def _act(self, user, pw, lid):
        c = app.test_client(); H.login(c, user, pw)
        c.post(f'/loans/{lid}/act', data={'action': 'approve'})

    def test_same_user_cannot_approve_two_stages(self):
        os.environ.pop('ALLOW_SAME_USER_APPROVALS', None)
        lid = self._loan('Alice', 'secretary')
        c = app.test_client(); H.login(c, 'admin', H.ADMIN_PW)
        for _ in range(3):
            c.post(f'/loans/{lid}/act', data={'action': 'approve'})
        self.assertEqual(self._state(lid), ('pending', 'treasurer'))

    def test_three_different_officers_complete_the_chain(self):
        os.environ.pop('ALLOW_SAME_USER_APPROVALS', None)
        lid = self._loan('Bob', 'secretary')
        self._act('secretary', H.SECR_PW, lid)
        self._act('treasurer', H.TREAS_PW, lid)
        self._act('admin', H.ADMIN_PW, lid)
        self.assertEqual(self._state(lid), ('active', 'approved'))

    def test_due_diligence_officer_cannot_give_final_approval(self):
        from database import get_db
        os.environ.pop('ALLOW_SAME_USER_APPROVALS', None)
        lid = self._loan('Alice', 'president')
        with app.app_context():
            db = get_db()
            aid = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()['id']
            db.execute('UPDATE loans SET due_diligence_updated_by=? WHERE id=?', (aid, lid)); db.commit()
        self._act('admin', H.ADMIN_PW, lid)
        self.assertEqual(self._state(lid), ('pending', 'president'))

    def test_operator_override(self):
        os.environ['ALLOW_SAME_USER_APPROVALS'] = '1'
        try:
            lid = self._loan('Bob', 'secretary')
            for _ in range(3):
                self._act('admin', H.ADMIN_PW, lid)
            self.assertEqual(self._state(lid), ('active', 'approved'))
        finally:
            os.environ.pop('ALLOW_SAME_USER_APPROVALS', None)


if __name__ == '__main__':
    unittest.main()
