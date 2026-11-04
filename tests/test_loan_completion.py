import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from loan_completion import dispatch_completions, queue_completion


class CompletionEmailTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE members (id INTEGER PRIMARY KEY, email TEXT,
                first_name TEXT, last_name TEXT, member_number TEXT);
            CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, role TEXT, is_active INTEGER);
            CREATE TABLE loans (id INTEGER PRIMARY KEY, member_id INTEGER,
                status TEXT, balance REAL, loan_number TEXT, amount REAL, purpose TEXT);
            CREATE TABLE repayments (id INTEGER PRIMARY KEY, loan_id INTEGER,
                reversed_at TEXT, amount REAL, date TEXT, repayment_number TEXT);
            CREATE TABLE loan_completion_emails (id INTEGER PRIMARY KEY,
                loan_id INTEGER, repayment_id INTEGER, recipient TEXT, audience TEXT,
                status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
                last_error TEXT, sent_at TEXT,
                UNIQUE(loan_id,repayment_id,recipient));
            INSERT INTO members VALUES (1,'member@example.test','Ada <script>','Test','M001');
            INSERT INTO users VALUES (1,'exco@example.test','exco',1),
                (2,'MEMBER@example.test','admin',1),(3,'inactive@example.test','exco',0);
            INSERT INTO loans VALUES (1,1,'completed',0,'LN001',500000,'Regular');
            INSERT INTO repayments VALUES (1,1,NULL,25000,'2026-11-04','REP001');
        ''')
        app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / 'templates'))
        self.context = app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.addCleanup(self.db.close)
        self.mock = patch('loan_completion.send_email_detailed', return_value={'ok': True})
        self.send = self.mock.start()
        self.addCleanup(self.mock.stop)

    def queue(self):
        queue_completion(self.db, 1, 1)
        self.db.commit()

    def test_completion_deduplicates_and_escapes_content(self):
        self.queue()
        self.queue()
        self.send.assert_not_called()
        dispatch_completions(self.db)
        dispatch_completions(self.db)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual({c.args[0] for c in self.send.call_args_list},
                         {'member@example.test', 'exco@example.test'})
        html = self.send.call_args_list[0].args[2]
        self.assertIn('Ada &lt;script&gt;', html)
        self.assertIn('NGN 0.00', html)
        self.assertIn('2026-11-04', html)

    def test_partial_payment_does_not_queue(self):
        self.db.execute("UPDATE loans SET status='active', balance=100")
        self.queue()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM loan_completion_emails').fetchone()[0], 0)

    def test_rollback_removes_outbox(self):
        queue_completion(self.db, 1, 1)
        self.db.rollback()
        dispatch_completions(self.db)
        self.send.assert_not_called()

    def test_provider_failure_retries_without_changing_payment(self):
        self.queue()
        self.send.return_value = {'ok': False}
        dispatch_completions(self.db)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM loan_completion_emails WHERE status='failed'").fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT balance FROM loans').fetchone()[0], 0)
        self.send.return_value = {'ok': True}
        dispatch_completions(self.db)
        dispatch_completions(self.db)
        self.assertEqual(self.send.call_count, 4)

    def test_reversed_payment_cancels_pending_notifications(self):
        self.queue()
        self.db.execute("UPDATE repayments SET reversed_at='2026-11-04'")
        self.db.commit()
        dispatch_completions(self.db)
        self.send.assert_not_called()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM loan_completion_emails WHERE status='cancelled'").fetchone()[0], 2)

    def test_changed_recipients_are_not_sent_private_details(self):
        self.queue()
        self.db.execute("UPDATE members SET email='new@example.test'")
        self.db.execute('UPDATE users SET is_active=0')
        self.db.commit()
        dispatch_completions(self.db)
        self.send.assert_not_called()

    def test_unknown_delivery_outcome_is_not_blindly_retried(self):
        self.queue()
        self.send.side_effect = RuntimeError('unknown outcome')
        with self.assertLogs('loan_completion', level='ERROR'):
            dispatch_completions(self.db)
        dispatch_completions(self.db)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM loan_completion_emails WHERE status='sending'").fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
