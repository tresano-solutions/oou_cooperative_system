"""
AUD-01 audit rows written AFTER db.commit() are silently discarded (repayment, member delete)
AUD-02 the application (and any admin) can delete the audit trail
"""
import sys, os, unittest
sys.path.insert(0, os.path.dirname(__file__))
import _harness as H

app = H.boot_app()


class AuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.members, cls.loans = H.seed(app)

    def _actions(self):
        from database import get_db
        with app.app_context():
            return [r['action'] for r in get_db().execute('SELECT action FROM audit_log ORDER BY id').fetchall()]

    def test_aud01_repayment_and_delete_audit(self):
        from database import get_db
        loan = self.loans[self.members['Alice']['id']]
        with app.app_context():
            db = get_db()
            db.execute("UPDATE loans SET status='active', approval_stage='approved', balance=55000 WHERE id=?", (loan['id'],))
            db.execute("""INSERT INTO members (member_number, first_name, last_name, email, status)
                          VALUES ('SEC999','Temp','Person','temp@sectest.invalid','active')""")
            tmp = db.execute("SELECT id FROM members WHERE member_number='SEC999'").fetchone()['id']
            db.commit()
        t = app.test_client(); H.login(t, 'treasurer', H.TREAS_PW)
        t.post(f"/loans/repay/{loan['id']}", data={'amount': '5000', 'method': 'cash'})
        a = app.test_client(); H.login(a, 'admin', H.ADMIN_PW)
        a.post(f'/members/delete/{tmp}')
        acts = self._actions()
        from database import get_db as g
        with app.app_context():
            reps = g().execute('SELECT COUNT(*) AS c FROM repayments').fetchone()['c']
            mem = g().execute("SELECT COUNT(*) AS c FROM members WHERE member_number='SEC999'").fetchone()['c']
        print(f"\nAUD-01: repayments recorded={reps}, temp member still exists={bool(mem)}")
        print('AUD-01: audit actions present:', acts)
        self.assertIn('LOAN_REPAYMENT', acts, 'repayment happened but has no audit row')
        self.assertIn('DELETE_MEMBER', acts, 'member deletion happened but has no audit row')

    def test_aud02_sensitive_admin_actions(self):
        from database import get_db
        with app.app_context():
            uid = get_db().execute("SELECT id FROM users WHERE username='secretary'").fetchone()['id']
        a = app.test_client(); H.login(a, 'admin', H.ADMIN_PW)
        before = len(self._actions())
        a.post(f'/api/reset_user_password/{uid}', data={'new_password': 'Rotated-By-Admin-9', 'force_change': '0'})
        a.post(f'/api/toggle_user/{uid}')
        a.post(f'/api/toggle_user/{uid}')
        a.post(f'/api/edit_user/{uid}', data={'full_name': 'Sec', 'email': 'sec@sectest.invalid', 'role': 'treasurer'})
        t = app.test_client(); H.login(t, 'treasurer', H.TREAS_PW)
        mid = self.members['Bob']['id']
        t.post('/savings/add', data={'member_id': mid, 'amount': '20000', 'month': '2026-10',
                                     'payment_type': 'voluntary', 'payment_method': 'cash'})
        new = self._actions()[before:]
        print('\nAUD-02: password reset + disable/enable user + role change + savings deposit produced audit rows:', new)
        self.assertTrue({'UPDATE'} & set(new), 'no audit row for admin user-management actions')


if __name__ == '__main__':
    unittest.main()
