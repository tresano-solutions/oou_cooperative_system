"""Route audit: request every route as an anonymous visitor and report which ones answer.

Run from the repo root:  python scripts/audit_routes.py
Uses a throw-away SQLite database; nothing real is touched. Routes that are public on purpose
(login, password reset, marketing, health) are listed so you can confirm each is meant to be open.
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.getcwd())
_dir = tempfile.TemporaryDirectory(prefix='route-audit-')
os.environ['SQLITE_DB_PATH'] = os.path.join(_dir.name, 'audit.db')
os.environ.pop('DATABASE_URL', None)
os.environ['SECRET_KEY'] = 'route-audit-key-not-for-production'
os.environ['ADMIN_PASSWORD'] = 'RouteAudit123!'
os.environ['FLASK_DEBUG'] = '1'

from app import app  # noqa: E402

app.config['WTF_CSRF_ENABLED'] = False
client = app.test_client()
open_get, open_post, errors = [], [], []

for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
    if rule.endpoint == 'static':
        continue
    url = re.sub(r'<[^>]+>', '1', rule.rule)
    for method in sorted(rule.methods & {'GET', 'POST'}):
        try:
            resp = client.open(url, method=method, follow_redirects=False)
        except Exception as exc:  # a route that crashes anonymously is worth knowing about
            errors.append((method, rule.rule, repr(exc)[:80]))
            continue
        target = resp.headers.get('Location', '')
        sent_to_login = resp.status_code in (301, 302, 303, 307, 308) and 'login' in target
        if resp.status_code in (401, 403, 404, 405) or sent_to_login:
            continue
        row = (method, rule.rule, resp.status_code, rule.endpoint)
        (open_get if method == 'GET' else open_post).append(row)

for title, rows in (('GET answered without login', open_get), ('POST answered without login', open_post)):
    print(f'\n== {title} ({len(rows)}) ==')
    for method, path, status, endpoint in rows:
        print(f'  {status} {path}   [{endpoint}]')
print(f'\n== Crashed anonymously ({len(errors)}) ==')
for method, path, err in errors:
    print(f'  {method} {path}  {err}')
