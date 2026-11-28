"""Security hardening: response headers, no raw-HTML flashes, no credential files. Run separately (isolated database)."""
import os
import re
import subprocess
import tempfile
import unittest

_directory = tempfile.TemporaryDirectory(prefix='sec-hardening-', dir=os.path.abspath('outputs'))
os.environ['SQLITE_DB_PATH'] = os.path.join(_directory.name, 'test.db')
os.environ.pop('DATABASE_URL', None)
os.environ['SECRET_KEY'] = 'sec-hardening-test-key'
os.environ['ADMIN_PASSWORD'] = 'TestAdmin123'
os.environ['FLASK_DEBUG'] = '1'

from app import app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Headers(unittest.TestCase):
    def test_login_page_carries_the_protective_headers(self):
        headers = app.test_client().get('/login').headers
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        csp = headers['Content-Security-Policy']
        for directive in ("frame-ancestors 'none'", "base-uri 'self'", "object-src 'none'"):
            self.assertIn(directive, csp)

    def test_cross_origin_requests_are_not_invited(self):
        headers = app.test_client().get('/login', headers={'Origin': 'https://evil.example'}).headers
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_a_post_without_a_csrf_token_is_refused(self):
        app.config['WTF_CSRF_ENABLED'] = True
        try:
            self.assertEqual(app.test_client().post('/login', data={'username': 'admin', 'password': 'x'}).status_code, 400)
        finally:
            app.config['WTF_CSRF_ENABLED'] = False


class Templates(unittest.TestCase):
    def test_flash_messages_are_never_rendered_as_raw_html(self):
        for name in ('templates/login.html', 'templates/auth/verify-2fa.html'):
            text = open(os.path.join(ROOT, name), encoding='utf-8').read()
            self.assertIsNone(re.search(r'message\s*\|\s*safe', text), name)


class Repository(unittest.TestCase):
    def test_no_credential_files_are_tracked(self):
        tracked = subprocess.run(['git', 'ls-files'], cwd=ROOT, capture_output=True, text=True).stdout.splitlines()
        bad = [f for f in tracked if re.search(r'(^|/)(\.env(\.[^/]*)?|Postgres Database[^/]*|[^/]*\.pem)$', f)
               and not f.endswith('.example')]
        self.assertEqual(bad, [])

    def test_the_default_demo_passwords_are_gone(self):
        for name in ('fix_database.py', 'config.py'):
            self.assertFalse(os.path.exists(os.path.join(ROOT, name)), name)


if __name__ == '__main__':
    unittest.main()
