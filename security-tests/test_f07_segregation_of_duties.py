"""F-07 regression. Separation of duties on loan approval is automatic:
strict when a cooperative has MORE than 3 active officers, permissive at 3 or fewer
(small societies), with ENFORCE_SEGREGATION_OF_DUTIES=1/0 as an operator override."""
import sys, os, unittest
sys.path.insert(0, os.path.dirname(__file__))
import _harness as H

app = H.boot_app()


class SoD(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.members, cls.loans = H.seed(app)

    def setUp(self):
        os.environ.pop('ENFORCE_SEGREGATION_OF_DUTIES', None)

    def _officers(self, n):
        """Make the number of active officers n (admin, treasurer, secretary seeded = 3)."""
        from database import get_db
        from werkzeug.security import generate_password_hash
        with app.app_context():
            db = get_db()
            db.execute("DELETE FROM users WHERE username LIKE 'exco_sec%'")
            for i in range(max(0, n - 3)):
                db.execute("INSERT INTO users (username, password_hash, role, is_active) VALUES (?, ?, 'exco', 1)",
                           (f'exco_sec{i}', generate_password_hash('x')))
            db.commit()

    def _loan(self, name, stage):
        from database import get_db
        loan = self.loans[self.members[name]['id']]
        with app.app_context():
            db = get_db()
            db.execute("UPDATE loans SET status='pending', approval_stage=?, due_diligence_updated_by=NULL WHERE id=?", (stage, loan['id']))
            db.execute("DELETE FROM loan_approvals WHERE loan_id=?", (loan['id'],))
            db.execute("DELETE FROM journal_lines WHERE entry_id IN (SELECT id FROM journal_entries WHERE reference=?)", (loan['loan_number'],))
            db.execute("DELETE FROM journal_entries WHERE reference=?", (loan['loan_number'],))
            db.execute("DELETE FROM audit_log WHERE action IN ('LOAN_SAME_OFFICER_MULTI_STAGE','LOAN_SOD_BLOCKED')")
            db.commit()
        return loan['id']

    def _state(self, lid):
        from database import get_db
        with app.app_context():
            r = get_db().execute('SELECT status, approval_stage FROM loans WHERE id=?', (lid,)).fetchone()
        return r['status'], r['approval_stage']

    def _audit_actions(self):
        from database import get_db
        with app.app_context():
            return [r['action'] for r in get_db().execute('SELECT action FROM audit_log').fetchall()]

    def _act(self, user, pw, lid):
        c = app.test_client(); H.login(c, user, pw)
        c.post(f'/loans/{lid}/act', data={'action': 'approve'})

    def test_three_officers_is_permissive_and_audited(self):
        self._officers(3)
        lid = self._loan('Alice', 'secretary')
        for _ in range(3):
            self._act('admin', H.ADMIN_PW, lid)
        self.assertEqual(self._state(lid), ('active', 'approved'))
        self.assertIn('LOAN_SAME_OFFICER_MULTI_STAGE', self._audit_actions())

    def test_four_officers_is_strict(self):
        self._officers(4)
        lid = self._loan('Bob', 'secretary')
        for _ in range(3):
            self._act('admin', H.ADMIN_PW, lid)
        self.assertEqual(self._state(lid), ('pending', 'treasurer'))
        self.assertIn('LOAN_SOD_BLOCKED', self._audit_actions())

    def test_four_officers_distinct_officers_complete_the_chain(self):
        self._officers(4)
        lid = self._loan('Alice', 'secretary')
        self._act('secretary', H.SECR_PW, lid)
        self._act('treasurer', H.TREAS_PW, lid)
        self._act('admin', H.ADMIN_PW, lid)
        self.assertEqual(self._state(lid), ('active', 'approved'))

    def test_strict_mode_due_diligence_officer_cannot_give_final_approval(self):
        from database import get_db
        self._officers(4)
        lid = self._loan('Bob', 'president')
        with app.app_context():
            db = get_db()
            aid = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()['id']
            db.execute('UPDATE loans SET due_diligence_updated_by=? WHERE id=?', (aid, lid)); db.commit()
        self._act('admin', H.ADMIN_PW, lid)
        self.assertEqual(self._state(lid), ('pending', 'president'))

    def test_inactive_officers_do_not_count(self):
        from database import get_db
        self._officers(4)
        with app.app_context():
            db = get_db(); db.execute("UPDATE users SET is_active=0 WHERE username='exco_sec0'"); db.commit()
        lid = self._loan('Alice', 'secretary')
        for _ in range(3):
            self._act('admin', H.ADMIN_PW, lid)
        self.assertEqual(self._state(lid), ('active', 'approved'))

    def test_operator_override_both_ways(self):
        self._officers(3)
        os.environ['ENFORCE_SEGREGATION_OF_DUTIES'] = '1'
        lid = self._loan('Bob', 'secretary')
        for _ in range(3):
            self._act('admin', H.ADMIN_PW, lid)
        self.assertEqual(self._state(lid), ('pending', 'treasurer'))
        self._officers(5)
        os.environ['ENFORCE_SEGREGATION_OF_DUTIES'] = '0'
        lid = self._loan('Alice', 'secretary')
        for _ in range(3):
            self._act('admin', H.ADMIN_PW, lid)
        self.assertEqual(self._state(lid), ('active', 'approved'))


if __name__ == '__main__':
    unittest.main()
