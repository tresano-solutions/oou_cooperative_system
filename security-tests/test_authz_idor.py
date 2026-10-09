"""
AUTHZ-01  unauthenticated sweep of every route
AUTHZ-02  member-role sweep of every route (GET and POST) — staff endpoints must refuse
AUTHZ-03  cross-member IDOR attempts on member-owned records
Run:  python -m unittest security-tests.test_authz_idor -v   (from repo root)
Output is a table; the unittest asserts only the hard failures (IDOR, staff-by-member).
"""
import re, sys, os, unittest
sys.path.insert(0, os.path.dirname(__file__))
import _harness as H

app = H.boot_app()

# Endpoints that are intentionally public (login, webhooks that verify a signature, marketing capture ...)
PUBLIC_OK = {
    'static', 'auth.index', 'auth.login', 'auth.verify_2fa', 'auth.forgot_password', 'auth.reset_password',
    'auth.setup_password', 'auth.web_manifest', 'auth.setup', 'auth.debug_auth', 'auth.emergency_reset',
    'cards.verify_card', 'payments.paystack_webhook', 'payments.flutterwave_webhook',
    'marketing.capture_lead', 'affiliates.apply', 'affiliates.request_statement',
    'mobile_api.mobile_login', 'mobile_api.mobile_forgot_password', 'mobile_api.mobile_tenant',
    'mobile_api.mobile_resolve_tenant', 'mobile_api.hq_member_count', 'mobile_api.hq_set_status',
    'mobile_api.hq_set_feature', 'loans.loan_pipeline_sweep', 'ctas.ctas_charge_due',
    'governance.run_reminders', 'hq_billing.pay_callback',
}


def _fill(rule, ids):
    url = rule.rule
    for arg in rule.arguments:
        conv = rule._converters[arg]
        val = ids.get(arg, '1') if conv.__class__.__name__ == 'IntegerConverter' else 'x'
        url = re.sub(r'<[^>]*\b%s>' % re.escape(arg), str(val), url)
    return url


def _denied(resp, client):
    if resp.status_code in (401, 403, 404, 405):
        return True
    if resp.status_code in (301, 302, 303, 307):
        loc = resp.headers.get('Location', '')
        if '/login' in loc:
            return True
        with client.session_transaction() as s:
            flashes = [m for _, m in s.get('_flashes', [])]
        s_txt = ' '.join(flashes).lower()
        return any(k in s_txt for k in ('access denied', 'insufficient', 'not assigned', 'denied', 'only admin'))
    return False


class AuthzTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.members, cls.loans = H.seed(app)
        cls.alice = cls.members['Alice']; cls.bob = cls.members['Bob']
        cls.bob_loan = cls.loans[cls.bob['id']]; cls.alice_loan = cls.loans[cls.alice['id']]

    def _ids(self):
        return {'member_id': self.bob['id'], 'loan_id': self.bob_loan['id'], 'saving_id': 1, 'user_id': 1,
                'gid': 1, 'notif_id': 1, 'card_id': 1}

    def test_authz01_unauthenticated_sweep(self):
        c = app.test_client()
        exposed = []
        for rule in app.url_map.iter_rules():
            if rule.endpoint in PUBLIC_OK or 'GET' not in rule.methods:
                continue
            r = c.get(_fill(rule, self._ids()))
            if r.status_code == 200:
                exposed.append((rule.rule, rule.endpoint))
        print('\nAUTHZ-01 unauthenticated GET returning 200 (not on the public allow-list):')
        for u, e in exposed:
            print('   ', u, e)
        self.assertEqual(exposed, [], 'routes reachable without login')

    def test_authz02_member_sweep(self):
        c = app.test_client()
        r = H.login(c, self.alice['email'], H.MEMBER_PW)
        self.assertIn(r.status_code, (302, 303), 'member login failed')
        allowed = []
        for rule in app.url_map.iter_rules():
            if rule.endpoint in PUBLIC_OK or rule.endpoint.startswith('static') or rule.endpoint == 'auth.logout':
                continue
            for method in sorted(rule.methods & {'GET', 'POST'}):
                url = _fill(rule, self._ids())
                resp = c.get(url) if method == 'GET' else c.post(url, data={})
                if resp.status_code in (301, 302, 303) and '/login' in resp.headers.get('Location', ''):
                    H.login(c, self.alice['email'], H.MEMBER_PW)     # session lost (idle/logout side effect)
                    resp = c.get(url) if method == 'GET' else c.post(url, data={})
                if resp.status_code >= 500:
                    allowed.append((method, rule.rule, rule.endpoint, f'HTTP {resp.status_code}'))
                elif not _denied(resp, c):
                    allowed.append((method, rule.rule, rule.endpoint, f'HTTP {resp.status_code}'))
        # Endpoints members legitimately use (portal, payments, mobile etc.) are reviewed by hand below.
        member_ok_prefixes = ('portal.', 'main.', 'auth.', 'payments.', 'virtual_accounts.', 'member_receipts.',
                              'ctas.my_', 'ctas.member_', 'feedback.', 'help.', 'help_bp.', 'training.', 'communications.my',
                              'mobile_api.', 'governance.')
        member_self_service = {'session_ping', 'security.index', 'security.setup_2fa',
                               'security.show_backup_codes', 'security.regenerate_backup_codes_route',
                               'security.disable_2fa'}
        suspicious = [a for a in allowed if not a[2].startswith(member_ok_prefixes)
                      and a[2] not in member_self_service]
        print('\nAUTHZ-02 routes a plain MEMBER session was NOT refused on (outside member areas):')
        for a in suspicious:
            print('   ', *a)
        print('AUTHZ-02 (informational) member-area routes that answered:', len(allowed) - len(suspicious))
        self.assertEqual(suspicious, [], 'staff routes reachable by a member session')

    def test_authz03_idor(self):
        c = app.test_client()
        H.login(c, self.alice['email'], H.MEMBER_PW)
        bob_loan = self.bob_loan['id']
        attempts = [
            ('GET',  f'/loan-detail/{bob_loan}'),
            ('GET',  f'/loan-detail/{bob_loan}/application.pdf'),
            ('POST', f'/loan-detail/{bob_loan}/withdraw'),
            ('GET',  f"/member/statement/{self.bob['id']}"),
            ('GET',  f"/members/{self.bob['id']}"),
            ('GET',  f"/api/member/{self.bob['id']}"),
            ('GET',  f"/member-card/{self.bob['id']}"),
            ('GET',  f"/cards/download/{self.bob['id']}"),
        ]
        leaked = []
        for m, u in attempts:
            r = c.get(u) if m == 'GET' else c.post(u, data={'withdrawal_reason': 'idor'})
            body = r.get_data(as_text=True)
            hit = r.status_code == 200 and ('Beta' in body or 'bob@sectest' in body or b'%PDF' in r.data[:5].__class__(r.data[:5]))
            print(f'AUTHZ-03 {m:4} {u:45} -> {r.status_code}  {"LEAK" if hit else "ok"}')
            if hit:
                leaked.append(u)
        from database import get_db
        with app.app_context():
            st = get_db().execute('SELECT status FROM loans WHERE id=?', (bob_loan,)).fetchone()['status']
        print('AUTHZ-03 Bob loan status after Alice tried to withdraw it:', st)
        self.assertEqual(st, 'pending')
        self.assertEqual(leaked, [])


if __name__ == '__main__':
    unittest.main()
