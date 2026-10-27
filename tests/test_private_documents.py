"""Payout evidence must not be readable by whoever guesses a URL.

These files were written under static/uploads/payouts/, which Flask serves to
anyone who asks, under a name built from the clock plus four random digits.
"""

import os
import unittest
from io import BytesIO

from werkzeug.datastructures import FileStorage

TEST_DB = os.path.abspath('.test-private-docs.db')
os.environ.setdefault('SECRET_KEY', 'test-secret-private-docs')
os.environ.setdefault('ADMIN_PASSWORD', 'TestAdmin123')
os.environ.setdefault('FLASK_DEBUG', '1')
os.environ.setdefault('FIELD_ENCRYPTION_KEY', '05SmPJhNFMKwg9NysnBdQjKtqn3VwWDl1IiPIMAg2as=')
os.environ.pop('DATABASE_URL', None)
os.environ['SQLITE_DB_PATH'] = TEST_DB
try:
    os.remove(TEST_DB)
except FileNotFoundError:
    pass

import app as app_module                                      # noqa: E402
from database import get_db, last_insert_id                   # noqa: E402
import private_files as pf                                    # noqa: E402


def upload(data=b'%PDF-1.4 x', filename='voucher.pdf'):
    """What the browser hands the view: a stream with a name."""
    return FileStorage(stream=BytesIO(data), filename=filename)



class PrivateDocumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app_module.app
        cls.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    def setUp(self):
        self.client = self.app.test_client()

    def login_admin(self):
        self.client.post('/login', data={'username': 'admin', 'password': 'TestAdmin123'})

    def _payout_row(self, evidence_path='payouts/deadbeef.pdf'):
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO members (member_number, first_name, last_name, status, "
                "date_joined, email) VALUES (?, 'Priv', 'Doc', 'active', '2024-01-01', ?)",
                (f'PD/{os.urandom(4).hex()}', f'pd{os.urandom(4).hex()}@coop.test'))
            mid = last_insert_id(db)
            db.execute(
                "INSERT INTO savings (member_id, amount, month, payment_type, "
                "receipt_number, evidence_path, date) "
                "VALUES (?, -5000, '2026-10', 'withdrawal', ?, ?, '2026-10-01')",
                (mid, f'PAYOUT/{os.urandom(3).hex()}', evidence_path))
            sid = last_insert_id(db)
            db.commit()
            return sid

    # ── where the file goes ──────────────────────────────────────────────────

    def test_evidence_is_not_written_under_static(self):
        ok, rel = pf.save_private(upload(), 'payouts', {'pdf'}, 8 * 1024 * 1024)
        self.fail_if_not(ok, rel)
        disk = pf.private_path(rel)
        self.addCleanup(lambda: os.path.exists(disk) and os.remove(disk))
        self.assertNotIn(f'static{os.sep}', disk)
        self.assertTrue(os.path.exists(disk))

    def fail_if_not(self, ok, msg):
        if not ok:
            self.fail(f'upload rejected: {msg}')

    def test_the_stored_name_is_not_guessable_from_the_clock(self):
        ok, rel = pf.save_private(upload(), 'payouts', {'pdf'}, 8 * 1024 * 1024)
        self.fail_if_not(ok, rel)
        self.addCleanup(lambda: os.remove(pf.private_path(rel)))
        stem = os.path.splitext(os.path.basename(rel))[0]
        self.assertEqual(len(stem), 32, 'expected a 128-bit random name')
        self.assertTrue(all(c in '0123456789abcdef' for c in stem))

    def test_a_path_that_escapes_the_root_is_refused(self):
        for bad in ('../../etc/passwd', '..', '/etc/passwd', 'payouts/../../x'):
            self.assertIsNone(pf.private_path(bad), f'{bad} should be refused')

    def test_oversized_and_wrong_type_uploads_are_refused(self):
        ok, _ = pf.save_private(upload(b'x' * 50), 'payouts', {'pdf'}, 10)
        self.assertFalse(ok, 'a file over the limit must be refused')
        ok, _ = pf.save_private(upload(b'MZ', 'payload.exe'), 'payouts', {'pdf'}, 8 * 1024 * 1024)
        self.assertTrue(ok is False)

    # ── who may read it ──────────────────────────────────────────────────────

    def test_the_old_public_url_is_refused(self):
        legacy = os.path.join('static', 'uploads', 'payouts')
        os.makedirs(legacy, exist_ok=True)
        probe = os.path.join(legacy, 'payout_PROBE.pdf')
        with open(probe, 'wb') as fh:
            fh.write(b'%PDF-1.4 probe')
        self.addCleanup(lambda: os.path.exists(probe) and os.remove(probe))
        r = self.client.get('/static/uploads/payouts/payout_PROBE.pdf')
        self.assertEqual(r.status_code, 404, 'static/ must not serve payout evidence')

    def test_a_stranger_cannot_download_evidence(self):
        sid = self._payout_row()
        r = self.client.get(f'/savings/{sid}/evidence')
        self.assertNotEqual(r.status_code, 200)

    def test_an_officer_gets_the_file(self):
        ok, rel = pf.save_private(upload(b'%PDF-1.4 real'), 'payouts', {'pdf'}, 8 * 1024 * 1024)
        self.fail_if_not(ok, rel)
        self.addCleanup(lambda: os.remove(pf.private_path(rel)))
        sid = self._payout_row(evidence_path=rel)
        self.login_admin()
        r = self.client.get(f'/savings/{sid}/evidence')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'%PDF-1.4 real', r.data)

    def test_a_missing_file_is_a_404_not_a_crash(self):
        sid = self._payout_row(evidence_path='payouts/nothing-here.pdf')
        self.login_admin()
        self.assertEqual(self.client.get(f'/savings/{sid}/evidence').status_code, 404)

    # ── moving what is already on disk ───────────────────────────────────────

    def test_legacy_files_are_moved_and_the_row_follows(self):
        legacy = os.path.join('static', 'uploads', 'payouts')
        os.makedirs(legacy, exist_ok=True)
        name = f'payout_LEGACY_{os.urandom(3).hex()}.pdf'
        with open(os.path.join(legacy, name), 'wb') as fh:
            fh.write(b'%PDF-1.4 legacy')
        sid = self._payout_row(evidence_path=f'uploads/payouts/{name}')
        with self.app.app_context():
            db = get_db()
            pf.migrate_legacy_payouts(db)
            db.commit()
            row = db.execute('SELECT evidence_path FROM savings WHERE id = ?', (sid,)).fetchone()
        self.assertEqual(row['evidence_path'], f'payouts/{name}')
        moved = pf.private_path(f'payouts/{name}')
        self.addCleanup(lambda: os.path.exists(moved) and os.remove(moved))
        self.assertTrue(os.path.exists(moved))
        self.assertFalse(os.path.exists(os.path.join(legacy, name)), 'the public copy must go')


if __name__ == '__main__':
    unittest.main()
