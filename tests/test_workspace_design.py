"""Rendered-page and transaction regression checks for the shared workspace."""
import unittest
import uuid
import test_hardening_features as fixture
from database import get_db


class WorkspaceDesignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = fixture.app_module.app
        cls.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    def setUp(self):
        self.client = self.app.test_client()
        response = self.client.post('/login', data={'username': 'admin', 'password': 'TestAdmin123'})
        self.assertEqual(response.status_code, 302)

    def test_staff_pages_render_shared_workspace(self):
        for path in ['/dashboard', '/members', '/loans', '/accounting/vouchers',
                     '/accounting/vouchers/new', '/accounting/chart', '/accounting/journal',
                     '/accounting/reconciliation']:
            with self.subTest(path=path):
                response = self.client.get(path, follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b'workspace.js', response.data)
                self.assertIn(b'Skip to page content', response.data)
                self.assertIn(b'<main id="pageContent"', response.data)

    def test_receipt_posts_once_and_retains_invalid_form(self):
        with self.app.app_context():
            from voucher_service import bank_accounts
            bank = bank_accounts(get_db())[0]['code']
        token = uuid.uuid4().hex
        data = dict(submission_token=token, kind='receipt', date='2026-09-28',
                    party='Employer', description='September remittance', reference='REVIEW-TEST',
                    bank_account=bank, purpose='general', line_count='1',
                    account_0='4100', amount_0='1200.00')
        response = self.client.post('/accounting/vouchers/new', data=data)
        self.assertEqual(response.status_code, 302)
        detail = self.client.get(response.location)
        self.assertEqual(detail.status_code, 200)
        again = self.client.post('/accounting/vouchers/new', data=data)
        self.assertEqual(again.location, response.location)
        with self.app.app_context():
            db = get_db()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM vouchers WHERE submission_token=?', (token,)).fetchone()[0], 1)
            self.assertIsNotNone(db.execute("SELECT id FROM audit_log WHERE action='VOUCHER_POSTED'").fetchone())
        data.update(submission_token=uuid.uuid4().hex, amount_0='-1')
        invalid = self.client.post('/accounting/vouchers/new', data=data)
        self.assertEqual(invalid.status_code, 200)
        self.assertIn(b'value="September remittance"', invalid.data)
        self.assertIn(b'value="-1"', invalid.data)

    def test_unbalanced_journal_does_not_post(self):
        token = uuid.uuid4().hex
        response = self.client.post('/accounting/vouchers/new', data={
            'submission_token': token, 'kind': 'journal', 'date': '2026-09-28',
            'description': 'Unbalanced test', 'line_count': '2',
            'account_0': '4100', 'debit_0': '500', 'account_1': '4100', 'credit_1': '499',
        })
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            self.assertIsNone(get_db().execute('SELECT id FROM vouchers WHERE submission_token=?', (token,)).fetchone())

    def test_voucher_date_filters(self):
        response = self.client.get('/accounting/vouchers?from_date=2999-01-01&to_date=2999-12-31&page=999')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'No vouchers match these filters', response.data)
        self.assertIn(b'Page 1 of 1', response.data)
        invalid = self.client.get('/accounting/vouchers?from_date=invalid')
        self.assertEqual(invalid.status_code, 200)
        self.assertIn(b'Use a valid date filter', invalid.data)


if __name__ == '__main__':
    unittest.main()
