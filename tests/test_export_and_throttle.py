"""Two small holes: exported spreadsheets, and who the throttle thinks you are."""

import os
import unittest
from io import StringIO

TEST_DB = os.path.abspath('.test-export-throttle.db')
os.environ.setdefault('SECRET_KEY', 'test-secret-export')
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
from csv_safe import safe_writer, neutralise                  # noqa: E402
from utils import throttle                                    # noqa: E402

FORMULA = '=HYPERLINK("http://attacker.example","Click me")'


class CsvInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app_module.app
        cls.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    def test_a_formula_is_defused(self):
        for payload in (FORMULA, '+1+1', '@SUM(A1)', '\t=cmd|calc', '\r=1'):
            self.assertTrue(neutralise(payload).startswith("'"),
                            f'{payload!r} should not reach a spreadsheet as a formula')

    def test_figures_are_left_alone(self):
        # An accountant has to be able to read a negative number as a number.
        for figure in ('-5,000.00', '-5000', '-0.5'):
            self.assertEqual(neutralise(figure), figure)

    def test_ordinary_text_and_non_strings_pass_through(self):
        for value in ('GTBank', '2026-10-28', '08012345678', '', None, 42, 3.5):
            self.assertEqual(neutralise(value), value)

    def test_the_writer_defuses_every_cell(self):
        out = StringIO()
        safe_writer(out).writerow(['ok', FORMULA, '-1,000.00'])
        line = out.getvalue()
        self.assertIn("'=HYPERLINK", line)
        self.assertNotIn(',=HYPERLINK', line)

    def test_a_members_export_carries_no_live_formula(self):
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO members (member_number, first_name, last_name, status, "
                "date_joined, email, address) VALUES (?, ?, 'Exporter', 'active', "
                "'2024-01-01', ?, ?)",
                (f'CSV/{os.urandom(3).hex()}', FORMULA,
                 f'csv{os.urandom(3).hex()}@coop.test', FORMULA))
            db.commit()
        client = self.app.test_client()
        client.post('/login', data={'username': 'admin', 'password': 'TestAdmin123'})
        body = client.get('/members/export').get_data(as_text=True)
        self.assertIn('HYPERLINK', body, 'the row should still be exported')
        for line in body.splitlines():
            for cell in line.split(','):
                self.assertFalse(cell.startswith('=HYPERLINK'),
                                 'a cell still opens with a formula')


class ThrottleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app_module.app
        cls.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    def test_the_allowance_runs_out(self):
        bucket = f'test:{os.urandom(4).hex()}'
        with self.app.app_context():
            allowed = [throttle(bucket, max_hits=3, window_seconds=900) for _ in range(5)]
        self.assertEqual(allowed, [False, False, False, True, True])

    def test_buckets_do_not_share_an_allowance(self):
        a, b = f'test:{os.urandom(4).hex()}', f'test:{os.urandom(4).hex()}'
        with self.app.app_context():
            for _ in range(3):
                throttle(a, max_hits=3, window_seconds=900)
            self.assertTrue(throttle(a, max_hits=3, window_seconds=900))
            self.assertFalse(throttle(b, max_hits=3, window_seconds=900))

    def test_the_count_is_shared_not_per_process(self):
        # The point of moving this into the database: a second worker sees the
        # same tally rather than starting its own.
        bucket = f'test:{os.urandom(4).hex()}'
        with self.app.app_context():
            for _ in range(3):
                throttle(bucket, max_hits=3, window_seconds=900)
            rows = get_db().execute(
                'SELECT COUNT(*) AS n FROM throttle_hits WHERE bucket = ?', (bucket,)
            ).fetchone()['n']
        self.assertEqual(rows, 3, 'hits must be recorded where every worker can see them')

    def test_expired_hits_stop_counting(self):
        bucket = f'test:{os.urandom(4).hex()}'
        with self.app.app_context():
            for _ in range(3):
                throttle(bucket, max_hits=3, window_seconds=900)
            self.assertTrue(throttle(bucket, max_hits=3, window_seconds=900))
            # A zero-length window means everything already recorded has expired.
            self.assertFalse(throttle(bucket, max_hits=3, window_seconds=0))

    def test_the_lead_form_buckets_on_the_address_the_proxy_reported(self):
        # A caller must not be able to choose their own bucket with a header.
        # ProxyFix resolves remote_addr from the entry Caddy appended, which is
        # the only one the caller cannot write.
        os.environ['MARKETING_HQ'] = '1'
        self.addCleanup(os.environ.pop, 'MARKETING_HQ', None)
        client = self.app.test_client()
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM throttle_hits WHERE bucket LIKE 'lead:%'")
            db.commit()
        for i in range(3):
            r = client.post('/api/marketing/leads',
                            json={'name': 'Spoofer', 'email': f'spoof{i}@example.test',
                                  'cooperative': 'Test Coop', 'phone': '08000000000'},
                            headers={'X-Forwarded-For': f'9.9.9.{i}'})
            self.assertNotEqual(r.status_code, 404, 'the lead route should be reachable here')
        with self.app.app_context():
            seen = {r['bucket'] for r in get_db().execute(
                "SELECT DISTINCT bucket FROM throttle_hits WHERE bucket LIKE 'lead:%'").fetchall()}
        self.assertEqual(len(seen), 1,
                         f'three forged headers must share one bucket, got {seen}')
        self.assertNotIn('lead:9.9.9.0', seen, 'the forged address was trusted')


if __name__ == '__main__':
    unittest.main()
