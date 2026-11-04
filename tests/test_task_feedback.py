import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask, flash, redirect, render_template_string
from flask_login import LoginManager, UserMixin
from flask_wtf.csrf import CSRFProtect

from blueprints.feedback import feedback_bp
from task_feedback import install_task_feedback, signer


class User(UserMixin):
    role = 'member'
    username = 'test-member'

    def __init__(self, user_id):
        self.id = user_id


class TaskFeedbackTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.app = Flask(__name__, template_folder=str(root / 'templates'),
                         static_folder=str(root / 'static'))
        self.app.config.update(SECRET_KEY='test-only', TESTING=True, WTF_CSRF_ENABLED=False)
        CSRFProtect(self.app)
        login = LoginManager(self.app)
        login.user_loader(lambda user_id: User(user_id))
        self.app.register_blueprint(feedback_bp)
        install_task_feedback(self.app)

        def balance():
            return render_template_string("{% include '_task_feedback.html' %}")

        def complete():
            flash('Application submitted', 'success')
            return redirect('/balance')

        def failed():
            flash('Validation failed', 'danger')
            return redirect('/balance')

        self.app.add_url_rule('/balance', 'portal.member_portal', balance)
        self.app.add_url_rule('/apply', 'portal.apply_loan_member', complete, methods=['POST'])
        self.app.add_url_rule('/fail', 'loans.apply_loan', failed, methods=['POST'])
        self.db = sqlite3.connect(':memory:')
        self.db.execute('''CREATE TABLE task_feedback_responses (
            event_key TEXT UNIQUE, user_id INTEGER, role TEXT, task TEXT, rating TEXT)''')
        self.addCleanup(self.db.close)
        self.db_patch = patch('blueprints.feedback.get_db', return_value=self.db)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['_user_id'] = '1'
            session['_fresh'] = True

    def token(self, user_id='1'):
        with self.app.app_context():
            return signer().dumps({'user_id': user_id, 'task': 'Check balances', 'nonce': 'event-1'})

    def test_prompt_is_optional_and_refresh_does_not_repeat(self):
        html = self.client.get('/balance').get_data(as_text=True)
        for label in ('Easy', 'Okay', 'Difficult', 'Skip', 'csrf_token'):
            self.assertIn(label, html)
        self.assertNotIn('task-feedback-form', self.client.get('/balance').get_data(as_text=True))

    def test_successful_write_names_task_after_redirect(self):
        html = self.client.post('/apply', follow_redirects=True).get_data(as_text=True)
        self.assertIn('Loan application: How was this task?', html)

    def test_failed_write_does_not_claim_completion(self):
        html = self.client.post('/fail', follow_redirects=True).get_data(as_text=True)
        self.assertNotIn('Record loan application:', html)

    def test_submission_is_idempotent_and_role_is_server_assigned(self):
        data = {'token': self.token(), 'rating': 'easy', 'role': 'admin'}
        self.assertEqual(self.client.post('/feedback/task', json=data).status_code, 200)
        self.assertEqual(self.client.post('/feedback/task', json=data).status_code, 200)
        rows = self.db.execute('SELECT role,rating FROM task_feedback_responses').fetchall()
        self.assertEqual(rows, [('member', 'easy')])

    def test_rejects_invalid_rating_tampering_and_other_user(self):
        for data in ({'rating': 'bad', 'token': self.token()},
                     {'rating': 'easy', 'token': 'forged'},
                     {'rating': 'easy', 'token': self.token('2')}, ['easy']):
            self.assertEqual(self.client.post('/feedback/task', json=data).status_code, 400)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM task_feedback_responses').fetchone()[0], 0)

    def test_login_and_csrf_required(self):
        self.assertEqual(self.app.test_client().post('/feedback/task', json={}).status_code, 401)
        self.app.config['WTF_CSRF_ENABLED'] = True
        self.assertEqual(self.client.post('/feedback/task', json={
            'rating': 'easy', 'token': self.token()}).status_code, 400)

    def test_public_contact_details(self):
        html = (Path(__file__).resolve().parents[1] / 'deploy/vps/landing/index.html').read_text(encoding='utf-8')
        self.assertIn('https://wa.me/2348071090281?', html)
        self.assertIn('mailto:adeolu.keshinro@tresanofinance.com?subject=', html)


if __name__ == '__main__':
    unittest.main()
