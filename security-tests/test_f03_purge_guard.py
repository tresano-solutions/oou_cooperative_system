"""F-03 regression: purge is off by default, needs password, never deletes the audit log."""
import sys, os, unittest
sys.path.insert(0, os.path.dirname(__file__))
import _harness as H

app = H.boot_app()


def counts():
    from database import get_db
    with app.app_context():
        db = get_db()
        return (db.execute('SELECT COUNT(*) AS c FROM members').fetchone()['c'],
                db.execute('SELECT COUNT(*) AS c FROM audit_log').fetchone()['c'])


class PurgeGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        H.seed(app)

    def _post(self, **data):
        c = app.test_client(); H.login(c, 'admin', H.ADMIN_PW)
        return c.post('/migration/purge', data=dict({'confirm_phrase': 'PURGE ALL DATA'}, **data))

    def test_1_disabled_by_default(self):
        os.environ.pop('ENABLE_PURGE', None)
        m0, a0 = counts()
        self._post(password=H.ADMIN_PW)
        m1, a1 = counts()
        self.assertEqual(m1, m0)
        self.assertGreaterEqual(a1, a0)

    def test_2_wrong_password_refused_when_enabled(self):
        os.environ['ENABLE_PURGE'] = '1'
        m0, _ = counts()
        self._post(password='wrong')
        self.assertEqual(counts()[0], m0)
        self._post()
        self.assertEqual(counts()[0], m0)

    def test_3_enabled_with_password_keeps_audit_log(self):
        os.environ['ENABLE_PURGE'] = '1'
        _, a0 = counts()
        self._post(password=H.ADMIN_PW)
        m1, a1 = counts()
        self.assertEqual(m1, 0)
        self.assertGreater(a1, a0, 'audit log must survive and record the purge')
        from database import get_db
        with app.app_context():
            acts = [r['action'] for r in get_db().execute('SELECT action FROM audit_log').fetchall()]
        self.assertIn('PURGE_DATABASE', acts)
        os.environ.pop('ENABLE_PURGE', None)


if __name__ == '__main__':
    unittest.main()
