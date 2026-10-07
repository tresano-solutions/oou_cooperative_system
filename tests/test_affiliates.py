import os
import unittest
import json
import re
from urllib.parse import urlparse
from unittest.mock import patch

TEST_DB = os.path.abspath('.test-affiliates.db')
os.environ.setdefault('SECRET_KEY', 'test-secret-affiliates')
os.environ.setdefault('ADMIN_PASSWORD', 'TestAdmin123')
os.environ.setdefault('FLASK_DEBUG', '1')
os.environ.setdefault('FIELD_ENCRYPTION_KEY', '05SmPJhNFMKwg9NysnBdQjKtqn3VwWDl1IiPIMAg2as=')
pg_test_url = os.environ.get('AFFILIATE_TEST_DATABASE_URL')
if pg_test_url:
    parsed = urlparse(pg_test_url)
    if parsed.hostname not in ('localhost', '127.0.0.1') or parsed.path != '/coopms_affiliate_test':
        raise RuntimeError('Affiliate PostgreSQL tests require an isolated local coopms_affiliate_test database.')
    os.environ['DATABASE_URL'] = pg_test_url
else:
    os.environ.pop('DATABASE_URL', None)
    os.environ['SQLITE_DB_PATH'] = TEST_DB
try:
    os.remove(TEST_DB)
except FileNotFoundError:
    pass

import app as app_module  # noqa: E402
from database import get_db  # noqa: E402


class AffiliateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app_module.app
        cls.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    def setUp(self):
        os.environ['MARKETING_HQ'] = '1'
        self.client = self.app.test_client()
        # The public lead API allows 8 submissions per IP per 15 minutes. That is
        # right in production and wrong for a test suite that files many
        # enquiries from one address, so the window is cleared per test rather
        # than the limit loosened.
        import blueprints.marketing as mk
        mk._RECENT_SUBMISSIONS.clear()

    def tearDown(self):
        os.environ.pop('MARKETING_HQ', None)

    def login_admin(self):
        r = self.client.post('/login', data={'username': 'admin', 'password': 'TestAdmin123'})
        self.assertIn(r.status_code, (302, 303))

    # ── helpers ──────────────────────────────────────────────────────────────

    def _apply(self, name, email, referrer_code=''):
        self.client.post('/affiliates/apply', data={
            'full_name': name, 'email': email, 'phone': '08000000000',
            'referrer_code': referrer_code}, follow_redirects=True)
        with self.app.app_context():
            return get_db().execute('SELECT * FROM affiliates WHERE email = ?', (email,)).fetchone()

    def _approve(self, aff_id, tier='member', parent_id=''):
        self.client.post(f'/hq/affiliates/{aff_id}/review', data={
            'action': 'approve', 'tier': tier, 'parent_id': str(parent_id)},
            follow_redirects=True)
        with self.app.app_context():
            return get_db().execute('SELECT * FROM affiliates WHERE id = ?', (aff_id,)).fetchone()

    def _accept(self, aff):
        page = self.client.get(f"/affiliates/accept/{aff['accept_token']}")
        version = re.search(rb'name="terms_version" value="([^"]+)"', page.data)
        return self.client.post(f"/affiliates/accept/{aff['accept_token']}", data={
            'terms_version': version[1].decode() if version else '',
            'accept_terms': '1', 'signature_name': aff['full_name']}, follow_redirects=True)

    def _onboard(self, name, email, tier='member', parent_id=''):
        aff = self._apply(name, email)
        aff = self._approve(aff['id'], tier=tier, parent_id=parent_id)
        self._accept(aff)
        with self.app.app_context():
            return get_db().execute('SELECT * FROM affiliates WHERE id = ?', (aff['id'],)).fetchone()

    # ── recruitment ──────────────────────────────────────────────────────────

    def test_acceptance_keeps_signature_and_exact_terms(self):
        import hashlib
        from blueprints.affiliates import MOU_TERMS
        self.login_admin()
        aff = self._onboard('Evidence Partner', 'evidence@example.test')
        self.assertEqual(aff['signature_name'], 'Evidence Partner')
        self.assertEqual(json.loads(aff['accepted_terms']), [list(t) for t in MOU_TERMS])
        self.assertEqual(aff['accepted_terms_version'], hashlib.sha256(aff['accepted_terms'].encode()).hexdigest())
        original_time = aff['accepted_at']
        self._accept(aff)
        with self.app.app_context():
            saved = get_db().execute('SELECT * FROM affiliates WHERE id = ?', (aff['id'],)).fetchone()
            self.assertEqual(saved['accepted_at'], original_time)

    def test_stale_terms_cannot_be_accepted(self):
        self.login_admin()
        aff = self._approve(self._apply('Stale Terms', 'stale@example.test')['id'])
        response = self.client.post(f"/affiliates/accept/{aff['accept_token']}", data={
            'accept_terms': '1', 'signature_name': 'Stale Terms', 'terms_version': 'old'})
        self.assertEqual(response.status_code, 400)

    def test_email_failures_are_not_reported_as_success_and_names_are_escaped(self):
        from blueprints.affiliates import _send_appointment, _send_statement_link
        self.login_admin()
        aff = self._approve(self._apply('<b>Partner</b>', 'mailfailure@example.test')['id'])
        with self.app.test_request_context('/'), patch('blueprints.affiliates.send_email', return_value=False) as send:
            db = get_db()
            self.assertFalse(_send_appointment(db, aff))
            self.assertIn('&lt;b&gt;Partner&lt;/b&gt;', send.call_args.args[2])
            self.assertFalse(_send_statement_link(db, aff, 'test-token'))

    def test_failed_commission_rolls_back_partial_work_and_retries_original_rates(self):
        from blueprints.affiliates import accrue_for_invoice
        self.login_admin()
        aff = self._onboard('Retry Partner', 'retry@example.test')
        _, invoice = self._client_with_setup('Retry Cooperative', aff, pay=False)
        def fail_after_writing(db, *args, **kwargs):
            accrue_for_invoice(db, *args, **kwargs)
            db.execute('SELECT * FROM deliberately_missing_commission_table')
        with patch('blueprints.affiliates.accrue_for_invoice', side_effect=fail_after_writing):
            response = self.client.post(f'/hq/invoices/{invoice}/mark-paid')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._commissions(invoice), [])
        with self.app.app_context():
            db = get_db()
            self.assertEqual(db.execute('SELECT status FROM hq_invoices WHERE id = ?', (invoice,)).fetchone()['status'], 'paid')
            job = db.execute("SELECT * FROM affiliate_commission_jobs WHERE invoice_id = ? AND operation = 'accrue'", (invoice,)).fetchone()
            self.assertEqual(job['status'], 'pending')
            expected = sum(r['basis'] * r['rate'] / 100 for r in json.loads(job['payload'])['plan'])
        page = self.client.get('/hq/affiliates/commissions')
        self.assertIn(b'Pending commission processing', page.data)
        with patch('blueprints.affiliates._earner_split', side_effect=AssertionError('Do not recalculate rates')):
            response = self.client.post(f"/hq/affiliates/commission-jobs/{job['id']}/retry")
        self.assertEqual(response.status_code, 302)
        self.assertAlmostEqual(sum(r['amount'] for r in self._commissions(invoice)), expected)
        self.client.post(f"/hq/affiliates/commission-jobs/{job['id']}/retry")
        self.assertAlmostEqual(sum(r['amount'] for r in self._commissions(invoice)), expected)

    def test_failed_reversal_can_be_retried_after_invoice_deletion(self):
        self.login_admin()
        aff = self._onboard('Reverse Retry', 'reverse-retry@example.test')
        _, invoice = self._client_with_setup('Reverse Retry Cooperative', aff)
        with patch('blueprints.affiliates.reverse_for_invoice', side_effect=RuntimeError('temporary')):
            self.client.post(f'/hq/invoices/{invoice}/delete')
        with self.app.app_context():
            db = get_db()
            self.assertIsNone(db.execute('SELECT id FROM hq_invoices WHERE id = ?', (invoice,)).fetchone())
            job = db.execute("SELECT id FROM affiliate_commission_jobs WHERE invoice_id = ? AND operation = 'reverse'", (invoice,)).fetchone()
        self.client.post(f"/hq/affiliates/commission-jobs/{job['id']}/retry")
        self.assertAlmostEqual(sum(r['amount'] for r in self._commissions(invoice)), 0)

    def test_an_applicant_is_not_in_the_programme_until_they_accept(self):
        self.login_admin()
        aff = self._apply('Tunde Bakare', 'tunde@example.test')
        self.assertEqual(aff['status'], 'applied')
        self.assertIsNone(aff['code'])           # no code until approved

        aff = self._approve(aff['id'])
        self.assertEqual(aff['status'], 'approved')
        self.assertTrue(aff['code'], 'approval must issue a code')
        self.assertTrue(aff['accept_token'])
        # Approved is not yet attributable — acceptance is the gate.
        from blueprints.affiliates import attributable
        self.assertFalse(attributable(aff))

        self._accept(aff)
        with self.app.app_context():
            aff = get_db().execute('SELECT * FROM affiliates WHERE id = ?', (aff['id'],)).fetchone()
        self.assertEqual(aff['status'], 'active')
        self.assertIsNotNone(aff['accepted_at'])
        self.assertTrue(attributable(aff))

    def test_a_declined_applicant_gets_no_code(self):
        self.login_admin()
        aff = self._apply('Rejected Person', 'rejected@example.test')
        self.client.post(f"/hq/affiliates/{aff['id']}/review", data={
            'action': 'decline', 'reason': 'Failed interview'}, follow_redirects=True)
        with self.app.app_context():
            aff = get_db().execute('SELECT * FROM affiliates WHERE id = ?', (aff['id'],)).fetchone()
        self.assertEqual(aff['status'], 'declined')
        self.assertIsNone(aff['code'])
        self.assertEqual(aff['declined_reason'], 'Failed interview')

    def test_acceptance_link_is_single_use_and_unguessable(self):
        self.login_admin()
        aff = self._onboard('Once Only', 'once@example.test')
        # A second visit reports the existing acceptance rather than re-accepting.
        r = self.client.get(f"/affiliates/accept/{aff['accept_token']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Welcome aboard', r.data)
        self.assertEqual(self.client.get('/affiliates/accept/not-a-real-token').status_code, 404)

    # ── attribution ──────────────────────────────────────────────────────────

    def _capture_lead(self, society, code=None):
        payload = {'full_name': 'Sec Gen', 'email': f'{society.lower()}@example.test',
                   'society_name': society, 'consent_accepted': '1'}
        if code is not None:
            payload['affiliate_code'] = code
        r = self.client.post('/api/marketing/leads', json=payload)
        self.assertEqual(r.status_code, 200, f'lead capture refused: {r.data[:200]}')
        with self.app.app_context():
            lead = get_db().execute('SELECT * FROM marketing_leads WHERE society_name = ?',
                                    (society,)).fetchone()
        self.assertIsNotNone(lead, f'no lead row created for {society}')
        return lead

    def test_a_referral_code_on_an_enquiry_credits_the_affiliate(self):
        self.login_admin()
        aff = self._onboard('Grace Eze', 'grace@example.test')
        lead = self._capture_lead('Sunrise Coop', aff['code'])
        self.assertEqual(lead['affiliate_code'], aff['code'])
        self.assertEqual(lead['affiliate_id'], aff['id'])

    def test_codes_are_matched_however_the_cooperative_types_them(self):
        self.login_admin()
        aff = self._onboard('Case Test', 'case@example.test')
        lead = self._capture_lead('Lowercase Coop', aff['code'].lower())
        self.assertEqual(lead['affiliate_id'], aff['id'])
        lead2 = self._capture_lead('Spaced Coop', f" {aff['code'][:4]} {aff['code'][4:]} ")
        self.assertEqual(lead2['affiliate_id'], aff['id'])

    def test_an_unknown_code_is_kept_for_correction_not_dropped(self):
        self.login_admin()
        lead = self._capture_lead('Typo Coop', 'CMA-ZZ9999')
        self.assertEqual(lead['affiliate_code'], 'CMA-ZZ9999')
        self.assertIsNone(lead['affiliate_id'], 'an unknown code must not credit anyone')
        # It surfaces on the attribution screen so it can be fixed.
        r = self.client.get('/hq/affiliates/attribution')
        self.assertIn(b'CMA-ZZ9999', r.data)
        # And an officer can correct it to a real affiliate.
        aff = self._onboard('Fixer Upper', 'fixer@example.test')
        self.client.post(f"/hq/affiliates/leads/{lead['id']}/code",
                         data={'affiliate_code': aff['code']}, follow_redirects=True)
        with self.app.app_context():
            lead = get_db().execute('SELECT * FROM marketing_leads WHERE id = ?',
                                    (lead['id'],)).fetchone()
        self.assertEqual(lead['affiliate_id'], aff['id'])

    def test_a_code_belonging_to_a_suspended_affiliate_credits_nobody(self):
        self.login_admin()
        aff = self._onboard('Suspended Sam', 'sam@example.test')
        self.client.post(f"/hq/affiliates/{aff['id']}/team",
                         data={'action': 'suspend'}, follow_redirects=True)
        lead = self._capture_lead('Late Coop', aff['code'])
        self.assertEqual(lead['affiliate_code'], aff['code'])
        self.assertIsNone(lead['affiliate_id'])

    def test_linking_a_client_to_its_enquiry_carries_the_introduction_through(self):
        from blueprints.hq_billing import setup_paid
        self.login_admin()
        aff = self._onboard('Chioma Obi', 'chioma@example.test')
        lead = self._capture_lead('Bridge Coop', aff['code'])
        self.client.post('/hq/clients', data={
            'name': 'Bridge Coop', 'code': 'bridge', 'billing_email': 'b@x.com',
            'user_count': '150', 'rate_per_user': '5000', 'billing_cycle': 'annual'},
            follow_redirects=True)
        with self.app.app_context():
            cid = get_db().execute("SELECT id FROM hq_clients WHERE name = 'Bridge Coop'").fetchone()['id']
        self.client.post(f'/hq/affiliates/clients/{cid}/link',
                         data={'lead_id': str(lead['id'])}, follow_redirects=True)
        with self.app.app_context():
            db = get_db()
            client = db.execute('SELECT * FROM hq_clients WHERE id = ?', (cid,)).fetchone()
            self.assertEqual(client['lead_id'], lead['id'])
            self.assertEqual(client['affiliate_id'], aff['id'])
            self.assertIsNotNone(client['attributed_at'])

        # Nothing is earned until the setup fee is actually collected.
        self.client.post('/hq/invoices/new', data={
            'client_id': cid, 'sub_mode': 'none', 'setup_amount': '300000'},
            follow_redirects=True)
        with self.app.app_context():
            db = get_db()
            self.assertAlmostEqual(setup_paid(db, cid), 0.0, places=2)
            inv = db.execute('SELECT id FROM hq_invoices WHERE client_id = ?', (cid,)).fetchone()['id']
        self.client.post(f'/hq/invoices/{inv}/mark-paid', data={'paid_method': 'transfer'},
                         follow_redirects=True)
        with self.app.app_context():
            self.assertAlmostEqual(setup_paid(get_db(), cid), 300000.0, places=2)

    def test_first_introduction_wins_when_a_client_is_relinked(self):
        self.login_admin()
        first = self._onboard('First Finder', 'first@example.test')
        second = self._onboard('Second Claimer', 'second@example.test')
        lead_a = self._capture_lead('Contested Coop', first['code'])
        lead_b = self._capture_lead('Contested Coop Two', second['code'])
        self.client.post('/hq/clients', data={
            'name': 'Contested Coop', 'code': 'contested', 'billing_email': 'c@x.com',
            'user_count': '80', 'rate_per_user': '5000', 'billing_cycle': 'annual'},
            follow_redirects=True)
        with self.app.app_context():
            cid = get_db().execute("SELECT id FROM hq_clients WHERE name = 'Contested Coop'").fetchone()['id']
        self.client.post(f'/hq/affiliates/clients/{cid}/link',
                         data={'lead_id': str(lead_a['id'])}, follow_redirects=True)
        self.client.post(f'/hq/affiliates/clients/{cid}/link',
                         data={'lead_id': str(lead_b['id'])}, follow_redirects=True)
        with self.app.app_context():
            client = get_db().execute('SELECT * FROM hq_clients WHERE id = ?', (cid,)).fetchone()
        self.assertEqual(client['affiliate_id'], first['id'],
                         'a later link must not move an existing attribution')

    # ── teams and promotion ──────────────────────────────────────────────────

    def test_a_recruit_joins_their_recruiters_team_lead(self):
        self.login_admin()
        lead = self._onboard('Team Lead', 'lead@example.test', tier='lead')
        member = self._onboard('Team Member', 'member@example.test', parent_id=lead['id'])
        recruit = self._apply('New Recruit', 'recruit@example.test', referrer_code=member['code'])
        self.assertEqual(recruit['recruited_by'], member['id'])
        # The recruiter is not yet a lead, so the recruit sits under their lead.
        self.assertEqual(recruit['parent_id'], lead['id'])

    def test_promotion_needs_five_recruits_and_takes_the_team_along(self):
        from blueprints.affiliates import recruit_count, may_apply_for_promotion
        self.login_admin()
        boss = self._onboard('Old Boss', 'boss@example.test', tier='lead')
        climber = self._onboard('Climber', 'climber@example.test', parent_id=boss['id'])

        for i in range(4):
            r = self._apply(f'Recruit {i}', f'r{i}@example.test', referrer_code=climber['code'])
            self._approve(r['id'], parent_id=boss['id'])
        with self.app.app_context():
            db = get_db()
            self.assertEqual(recruit_count(db, climber['id']), 4)
            c = db.execute('SELECT * FROM affiliates WHERE id = ?', (climber['id'],)).fetchone()
            self.assertFalse(may_apply_for_promotion(db, c), 'four recruits is not enough')

        # Below threshold the promotion is refused.
        self.client.post(f"/hq/affiliates/{climber['id']}/team",
                         data={'action': 'promote'}, follow_redirects=True)
        with self.app.app_context():
            c = get_db().execute('SELECT tier FROM affiliates WHERE id = ?', (climber['id'],)).fetchone()
        self.assertEqual(c['tier'], 'member')

        fifth = self._apply('Recruit 5', 'r5@example.test', referrer_code=climber['code'])
        self._approve(fifth['id'], parent_id=boss['id'])
        self.client.post(f"/hq/affiliates/{climber['id']}/team",
                         data={'action': 'promote'}, follow_redirects=True)
        with self.app.app_context():
            db = get_db()
            c = db.execute('SELECT * FROM affiliates WHERE id = ?', (climber['id'],)).fetchone()
            self.assertEqual(c['tier'], 'lead')
            self.assertIsNone(c['parent_id'], 'a promoted member leaves their former lead')
            self.assertIsNotNone(c['promoted_at'])
            # Their own recruits follow them into the new team.
            moved = db.execute('SELECT COUNT(*) FROM affiliates WHERE parent_id = ?',
                               (climber['id'],)).fetchone()[0]
            self.assertEqual(moved, 5)

    def test_affiliate_pages_are_operator_only(self):
        self.login_admin()
        os.environ.pop('MARKETING_HQ', None)
        self.assertEqual(self.client.get('/hq/affiliates').status_code, 404)
        self.assertEqual(self.client.get('/affiliates/apply').status_code, 404)

    # ── commission ───────────────────────────────────────────────────────────

    def _client_with_setup(self, name, aff, setup=300000, users=200, pay=True):
        """A cooperative introduced by `aff`, billed a setup fee, optionally paid."""
        lead = self._capture_lead(name, aff['code'])
        self.client.post('/hq/clients', data={
            'name': name, 'code': name.lower().replace(' ', ''), 'billing_email': 'x@y.com',
            'user_count': str(users), 'rate_per_user': '5000', 'billing_cycle': 'annual'},
            follow_redirects=True)
        with self.app.app_context():
            cid = get_db().execute('SELECT id FROM hq_clients WHERE name = ?', (name,)).fetchone()['id']
        self.client.post(f'/hq/affiliates/clients/{cid}/link',
                         data={'lead_id': str(lead['id'])}, follow_redirects=True)
        self.client.post('/hq/invoices/new', data={
            'client_id': cid, 'sub_mode': 'none', 'setup_amount': str(setup)},
            follow_redirects=True)
        with self.app.app_context():
            inv = get_db().execute('SELECT id FROM hq_invoices WHERE client_id = ? '
                                   'ORDER BY id DESC', (cid,)).fetchone()['id']
        if pay:
            self.client.post(f'/hq/invoices/{inv}/mark-paid',
                             data={'paid_method': 'transfer'}, follow_redirects=True)
        return cid, inv

    def _commissions(self, invoice_id):
        with self.app.app_context():
            return get_db().execute(
                'SELECT c.*, a.full_name FROM affiliate_commissions c '
                'JOIN affiliates a ON a.id = c.affiliate_id WHERE c.invoice_id = ? '
                'ORDER BY c.role, c.id', (invoice_id,)).fetchall()

    def test_member_and_lead_split_the_pool_out_of_one_setup_fee(self):
        self.login_admin()
        boss = self._onboard('Split Lead', 'splitlead@example.test', tier='lead')
        member = self._onboard('Split Member', 'splitmember@example.test', parent_id=boss['id'])
        _, inv = self._client_with_setup('Split Coop', member, setup=300000)

        rows = self._commissions(inv)
        self.assertEqual(len(rows), 2)
        by_role = {r['role']: r for r in rows}
        # 15% to the member who closed it, 5% to their lead, out of the same 20%.
        self.assertAlmostEqual(float(by_role['direct']['amount']), 45000.0, places=2)
        self.assertEqual(by_role['direct']['affiliate_id'], member['id'])
        self.assertAlmostEqual(float(by_role['override']['amount']), 15000.0, places=2)
        self.assertEqual(by_role['override']['affiliate_id'], boss['id'])
        self.assertEqual(by_role['override']['source_affiliate_id'], member['id'])
        # Total cost to the business is capped at the pool.
        self.assertAlmostEqual(sum(float(r['amount']) for r in rows), 60000.0, places=2)

    def test_a_lead_who_closes_it_himself_takes_the_whole_pool(self):
        from blueprints.affiliates import affiliate_balance
        self.login_admin()
        boss = self._onboard('Solo Lead', 'sololead@example.test', tier='lead')
        _, inv = self._client_with_setup('Solo Coop', boss, setup=300000)
        rows = self._commissions(inv)
        self.assertEqual(len(rows), 1, 'there is nobody above a lead to override')
        self.assertEqual(rows[0]['role'], 'direct')
        self.assertAlmostEqual(float(rows[0]['amount']), 60000.0, places=2)
        with self.app.app_context():
            self.assertAlmostEqual(affiliate_balance(get_db(), boss['id']), 60000.0, places=2)

    def test_a_member_with_no_lead_earns_their_own_rate_and_the_rest_is_kept(self):
        self.login_admin()
        orphan = self._onboard('No Team', 'noteam@example.test')   # no parent
        _, inv = self._client_with_setup('Orphan Coop', orphan, setup=300000)
        rows = self._commissions(inv)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(float(rows[0]['amount']), 45000.0, places=2)
        # The 5% override is simply not paid — it does not roll up to the member.
        self.assertAlmostEqual(sum(float(r['amount']) for r in rows), 45000.0, places=2)

    def test_nothing_is_earned_until_the_setup_fee_is_actually_paid(self):
        self.login_admin()
        aff = self._onboard('Patient Seller', 'patient@example.test')
        cid, inv = self._client_with_setup('Unpaid Coop', aff, setup=300000, pay=False)
        self.assertEqual(len(self._commissions(inv)), 0)
        self.client.post(f'/hq/invoices/{inv}/mark-paid',
                         data={'paid_method': 'transfer'}, follow_redirects=True)
        self.assertEqual(len(self._commissions(inv)), 1)

    def test_commission_follows_cash_so_a_deposit_earns_only_its_part(self):
        from blueprints.affiliates import affiliate_balance
        self.login_admin()
        aff = self._onboard('Chaser', 'chaser@example.test')
        # 100,000 deposit invoice paid; 200,000 balance invoice still outstanding.
        cid, first = self._client_with_setup('Instalment Coop', aff, setup=100000)
        self.client.post('/hq/invoices/new', data={
            'client_id': cid, 'sub_mode': 'none', 'setup_amount': '200000',
            'setup_again': '1'}, follow_redirects=True)
        with self.app.app_context():
            db = get_db()
            self.assertAlmostEqual(affiliate_balance(db, aff['id']), 15000.0, places=2)
            second = db.execute('SELECT id FROM hq_invoices WHERE client_id = ? ORDER BY id DESC',
                                (cid,)).fetchone()['id']
        # Chasing the balance earns the rest.
        self.client.post(f'/hq/invoices/{second}/mark-paid',
                         data={'paid_method': 'transfer'}, follow_redirects=True)
        with self.app.app_context():
            self.assertAlmostEqual(affiliate_balance(get_db(), aff['id']), 45000.0, places=2)

    def test_accrual_is_idempotent_so_a_replayed_payment_cannot_pay_twice(self):
        from blueprints.affiliates import accrue_for_invoice, affiliate_balance
        self.login_admin()
        aff = self._onboard('Once Paid', 'oncepaid@example.test')
        _, inv = self._client_with_setup('Replay Coop', aff, setup=300000)
        with self.app.app_context():
            db = get_db()
            before = affiliate_balance(db, aff['id'])
            # Simulate the gateway replaying its callback.
            accrue_for_invoice(db, inv)
            accrue_for_invoice(db, inv)
            db.commit()
            self.assertAlmostEqual(affiliate_balance(db, aff['id']), before, places=2)
        self.assertEqual(len(self._commissions(inv)), 1)

    def test_only_the_setup_line_earns_commission(self):
        from blueprints.affiliates import affiliate_balance
        self.login_admin()
        aff = self._onboard('Subs Only', 'subsonly@example.test')
        lead = self._capture_lead('Subs Coop', aff['code'])
        self.client.post('/hq/clients', data={
            'name': 'Subs Coop', 'code': 'subscoop', 'billing_email': 'x@y.com',
            'user_count': '100', 'rate_per_user': '5000', 'billing_cycle': 'annual'},
            follow_redirects=True)
        with self.app.app_context():
            cid = get_db().execute("SELECT id FROM hq_clients WHERE name = 'Subs Coop'").fetchone()['id']
        self.client.post(f'/hq/affiliates/clients/{cid}/link',
                         data={'lead_id': str(lead['id'])}, follow_redirects=True)
        # A subscription plus a service fee, and no setup line at all.
        self.client.post('/hq/invoices/new', data={
            'client_id': cid, 'sub_mode': 'full', 'sub_qty': '100', 'sub_unit': '5000',
            'service_type': 'training', 'service_desc': 'onboarding day',
            'service_amount': '50000'}, follow_redirects=True)
        with self.app.app_context():
            inv = get_db().execute('SELECT id FROM hq_invoices WHERE client_id = ?',
                                   (cid,)).fetchone()['id']
        self.client.post(f'/hq/invoices/{inv}/mark-paid',
                         data={'paid_method': 'transfer'}, follow_redirects=True)
        with self.app.app_context():
            self.assertAlmostEqual(affiliate_balance(get_db(), aff['id']), 0.0, places=2)

    def test_an_unattributed_cooperative_earns_nobody_anything(self):
        self.login_admin()
        self.client.post('/hq/clients', data={
            'name': 'Walk In Coop', 'code': 'walkin', 'billing_email': 'x@y.com',
            'user_count': '90', 'rate_per_user': '5000', 'billing_cycle': 'annual'},
            follow_redirects=True)
        with self.app.app_context():
            cid = get_db().execute("SELECT id FROM hq_clients WHERE name = 'Walk In Coop'").fetchone()['id']
        self.client.post('/hq/invoices/new', data={
            'client_id': cid, 'sub_mode': 'none', 'setup_amount': '300000'},
            follow_redirects=True)
        with self.app.app_context():
            inv = get_db().execute('SELECT id FROM hq_invoices WHERE client_id = ?',
                                   (cid,)).fetchone()['id']
        self.client.post(f'/hq/invoices/{inv}/mark-paid',
                         data={'paid_method': 'transfer'}, follow_redirects=True)
        self.assertEqual(len(self._commissions(inv)), 0)

    def test_deleting_a_paid_invoice_claws_the_commission_back(self):
        from blueprints.affiliates import affiliate_balance
        self.login_admin()
        boss = self._onboard('Clawback Lead', 'cblead@example.test', tier='lead')
        member = self._onboard('Clawback Member', 'cbmember@example.test', parent_id=boss['id'])
        _, inv = self._client_with_setup('Refund Coop', member, setup=300000)
        with self.app.app_context():
            db = get_db()
            self.assertAlmostEqual(affiliate_balance(db, member['id']), 45000.0, places=2)
            self.assertAlmostEqual(affiliate_balance(db, boss['id']), 15000.0, places=2)

        self.client.post(f'/hq/invoices/{inv}/delete', follow_redirects=True)
        with self.app.app_context():
            db = get_db()
            # Both the member and the override go back to zero...
            self.assertAlmostEqual(affiliate_balance(db, member['id']), 0.0, places=2)
            self.assertAlmostEqual(affiliate_balance(db, boss['id']), 0.0, places=2)
            # ...but the history stays, as compensating rows, not deletions.
            rows = db.execute('SELECT * FROM affiliate_commissions WHERE invoice_id = ? '
                              'ORDER BY id', (inv,)).fetchall()
            self.assertEqual(len(rows), 4, 'two earnings and two reversals')
            # The originals are marked reversed and stamped...
            originals = [r for r in rows if r['status'] == 'reversed']
            self.assertEqual(len(originals), 2)
            self.assertTrue(all(r['reversed_at'] for r in originals))
            # ...and each is cancelled by its own compensating row.
            clawbacks = [r for r in rows if r['status'] == 'clawback']
            self.assertEqual(len(clawbacks), 2)
            self.assertTrue(all(r['amount'] < 0 and r['reversal_of'] for r in clawbacks))
            self.assertEqual({r['reversal_of'] for r in clawbacks},
                             {r['id'] for r in originals})

    def test_rates_are_configurable_and_only_affect_later_earnings(self):
        from blueprints.affiliates import commission_rates
        self.login_admin()
        aff = self._onboard('Rate Change', 'ratechange@example.test')
        _, first = self._client_with_setup('Old Rate Coop', aff, setup=300000)
        self.assertAlmostEqual(float(self._commissions(first)[0]['amount']), 45000.0, places=2)

        self.client.post('/hq/affiliates/rates', data={
            'affiliate_member_rate': '10', 'affiliate_lead_rate': '5'}, follow_redirects=True)
        with self.app.app_context():
            r = commission_rates(get_db())
            self.assertEqual(r['member'], 10.0)
            self.assertEqual(r['pool'], 15.0, 'the pool is the sum of the two shares')

        _, second = self._client_with_setup('New Rate Coop', aff, setup=300000)
        self.assertAlmostEqual(float(self._commissions(second)[0]['amount']), 30000.0, places=2)
        # The earlier earning is untouched.
        self.assertAlmostEqual(float(self._commissions(first)[0]['amount']), 45000.0, places=2)

    # ── statement portal ─────────────────────────────────────────────────────

    def _token_for(self, aff):
        with self.app.app_context():
            db = get_db()
            from blueprints.affiliates import issue_portal_token
            t = issue_portal_token(db, aff['id'])
            db.commit()
        return t

    def test_requesting_a_link_never_reveals_who_is_an_affiliate(self):
        self.login_admin()
        aff = self._onboard('Quiet One', 'quiet@example.test')
        real = self.client.post('/affiliates/statement', data={'email': aff['email']})
        fake = self.client.post('/affiliates/statement', data={'email': 'nobody@example.test'})
        self.assertEqual(real.status_code, fake.status_code)
        self.assertEqual(real.data, fake.data, 'the reply must not differ for a real address')
        self.assertIn(b'Check your email', real.data)
        # A link was still only created for the real one.
        with self.app.app_context():
            n = get_db().execute('SELECT COUNT(*) FROM affiliate_portal_tokens '
                                 'WHERE affiliate_id = ?', (aff['id'],)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_a_statement_link_shows_only_that_affiliates_own_figures(self):
        self.login_admin()
        mine = self._onboard('Mine Only', 'mineonly@example.test')
        other = self._onboard('Someone Else', 'someoneelse@example.test')
        self._client_with_setup('My Coop', mine, setup=300000)
        self._client_with_setup('Their Coop', other, setup=300000)

        r = self.client.get(f'/affiliates/statement/{self._token_for(mine)}')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'My Coop', r.data)
        self.assertNotIn(b'Their Coop', r.data,
                         "one affiliate must not see another's clients")
        self.assertIn(mine['code'].encode(), r.data)

    def test_earned_and_still_to_come_are_shown_separately(self):
        self.login_admin()
        aff = self._onboard('Pipeline Watcher', 'pipeline@example.test')
        # One paid setup fee, one billed but unpaid.
        self._client_with_setup('Paid Coop', aff, setup=300000)
        self._client_with_setup('Unpaid Coop', aff, setup=200000, pay=False)
        with self.app.app_context():
            from blueprints.affiliates import statement_context
            db = get_db()
            a = db.execute('SELECT * FROM affiliates WHERE id = ?', (aff['id'],)).fetchone()
            ctx = statement_context(db, a)
        self.assertAlmostEqual(ctx['balance'], 45000.0, places=2)      # 15% of 300,000 collected
        self.assertAlmostEqual(ctx['pipeline'], 30000.0, places=2)     # 15% of 200,000 not yet paid
        r = self.client.get(f'/affiliates/statement/{self._token_for(aff)}')
        self.assertIn(b'Not yet earned', r.data, 'the pipeline must be labelled as unearned')

    def test_a_lead_sees_their_override_across_the_team(self):
        self.login_admin()
        boss = self._onboard('Portal Lead', 'portallead@example.test', tier='lead')
        member = self._onboard('Portal Member', 'portalmember@example.test', parent_id=boss['id'])
        self._client_with_setup('Team Coop', member, setup=300000)
        with self.app.app_context():
            from blueprints.affiliates import statement_context
            db = get_db()
            b = db.execute('SELECT * FROM affiliates WHERE id = ?', (boss['id'],)).fetchone()
            ctx = statement_context(db, b)
        self.assertEqual(len(ctx['team']), 1)
        self.assertAlmostEqual(ctx['team'][0]['earned'], 15000.0, places=2)
        self.assertAlmostEqual(ctx['balance'], 15000.0, places=2)
        r = self.client.get(f'/affiliates/statement/{self._token_for(boss)}')
        self.assertIn(b'Portal Member', r.data)

    def test_an_expired_link_is_refused_and_offers_a_new_one(self):
        from datetime import datetime, timedelta
        self.login_admin()
        aff = self._onboard('Expired Link', 'expired@example.test')
        token = self._token_for(aff)
        with self.app.app_context():
            db = get_db()
            db.execute('UPDATE affiliate_portal_tokens SET expires_at = ? WHERE token = ?',
                       (datetime.now() - timedelta(minutes=1), token))
            db.commit()
        r = self.client.get(f'/affiliates/statement/{token}')
        self.assertEqual(r.status_code, 410)
        self.assertIn(b'expired', r.data.lower())
        self.assertEqual(self.client.get('/affiliates/statement/made-up-token').status_code, 404)

    def test_a_link_stops_working_once_the_appointment_is_closed(self):
        self.login_admin()
        aff = self._onboard('Gone Away', 'goneaway@example.test')
        token = self._token_for(aff)
        self.assertEqual(self.client.get(f'/affiliates/statement/{token}').status_code, 200)
        # Declining ends the appointment; the live link must stop working.
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE affiliates SET status = 'declined' WHERE id = ?", (aff['id'],))
            db.commit()
        self.assertEqual(self.client.get(f'/affiliates/statement/{token}').status_code, 404)

    def test_the_statement_is_reachable_for_the_operator_too(self):
        self.login_admin()
        aff = self._onboard('Support Call', 'support@example.test')
        self._client_with_setup('Support Coop', aff, setup=300000)
        r = self.client.get(f"/hq/affiliates/{aff['id']}/statement")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Support Coop', r.data)
        self.assertIn(b'as an operator', r.data)


if __name__ == '__main__':
    unittest.main()
