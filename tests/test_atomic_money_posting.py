"""A subledger row must never commit while its ledger entry does not.

Balances are read from the ledger, so a savings row with no journal entry is
not a small inconsistency — it is a member's money the books cannot see, found
during a reconciliation weeks later with nothing left to explain it.
"""

import os
import unittest
from unittest.mock import patch

TEST_DB = os.path.abspath('.test-atomic-money.db')
os.environ.setdefault('SECRET_KEY', 'test-secret-atomic')
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
import ledger                                                 # noqa: E402
from database import get_db, last_insert_id                   # noqa: E402
from ledger import (JournalPostingError, post_journal_required,  # noqa: E402
                    get_postable_cash_accounts)


class AtomicPostingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app_module.app
        cls.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    def setUp(self):
        self.client = self.app.test_client()
        self.client.post('/login', data={'username': 'admin', 'password': 'TestAdmin123'})
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO members (member_number, first_name, last_name, status, "
                "date_joined, email, monthly_savings) VALUES (?, 'Atom', 'Ic', 'active', "
                "'2024-01-01', ?, 5000)",
                (f'AT/{os.urandom(4).hex()}', f'at{os.urandom(4).hex()}@coop.test'))
            self.member_id = last_insert_id(db)
            db.commit()

    def savings_rows(self):
        with self.app.app_context():
            return get_db().execute(
                'SELECT COUNT(*) AS n FROM savings WHERE member_id = ?',
                (self.member_id,)).fetchone()['n']

    def member_total(self):
        with self.app.app_context():
            return float(get_db().execute(
                'SELECT total_savings FROM members WHERE id = ?',
                (self.member_id,)).fetchone()['total_savings'] or 0)

    # ── the helper itself ────────────────────────────────────────────────────

    def test_the_strict_poster_raises_instead_of_returning_none(self):
        with self.app.app_context():
            with patch.object(ledger, 'post_journal', side_effect=RuntimeError('ledger down')):
                with self.assertRaises(JournalPostingError):
                    post_journal_required(get_db(), 'x', [], reference='ATOMIC/1')

    def test_the_message_says_nothing_was_saved(self):
        with self.app.app_context():
            with patch.object(ledger, 'post_journal', side_effect=RuntimeError('boom')):
                try:
                    post_journal_required(get_db(), 'x', [], reference='ATOMIC/2')
                except JournalPostingError as exc:
                    self.assertIn('nothing was saved', str(exc))

    def test_a_poster_that_returns_nothing_is_also_a_failure(self):
        with self.app.app_context():
            with patch.object(ledger, 'post_journal', return_value=None):
                with self.assertRaises(JournalPostingError):
                    post_journal_required(get_db(), 'x', [], reference='ATOMIC/3')

    # ── the flow the review reproduced ───────────────────────────────────────

    def _post_contribution(self):
        return self.client.post('/savings/add', data={
            'member_id': str(self.member_id), 'amount': '5000',
            'month': '2026-10', 'payment_type': 'monthly',
            'payment_method': 'cash',
            'bank_account': get_postable_cash_accounts(get_db())[0]['code'],
        }, follow_redirects=True)

    def test_a_failed_journal_leaves_no_savings_row(self):
        before, before_total = self.savings_rows(), self.member_total()
        with self.app.app_context():
            with patch.object(ledger, 'post_journal', side_effect=RuntimeError('ledger down')):
                self._post_contribution()
        self.assertEqual(self.savings_rows(), before,
                         'the contribution must not survive its own accounting entry failing')
        self.assertEqual(self.member_total(), before_total,
                         'the running total must not move either')

    def test_the_officer_is_told_rather_than_shown_success(self):
        with self.app.app_context():
            with patch.object(ledger, 'post_journal', side_effect=RuntimeError('ledger down')):
                body = self._post_contribution().get_data(as_text=True)
        self.assertNotIn('Contribution of', body, 'a failure must not be reported as recorded')

    def test_the_same_contribution_still_works_normally(self):
        before = self.savings_rows()
        with self.app.app_context():
            self._post_contribution()
        self.assertEqual(self.savings_rows(), before + 1)
        with self.app.app_context():
            entry = get_db().execute(
                "SELECT COUNT(*) AS n FROM journal_entries WHERE source_module = 'savings_deposit'"
            ).fetchone()['n']
        self.assertGreater(entry, 0, 'the ledger entry should be there on the happy path')

    def test_no_money_flow_still_posts_best_effort(self):
        # The backfill re-posts history and counts what it could not place, so
        # it must keep swallowing failures rather than stopping at the first.
        import inspect
        source = inspect.getsource(ledger.backfill_from_transactions)
        self.assertIn('post_journal_safe', source,
                      'the backfill is deliberately best-effort')
        self.assertNotIn('post_journal_required', source)


if __name__ == '__main__':
    unittest.main()
