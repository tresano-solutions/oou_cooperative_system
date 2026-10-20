"""
Regression tests for the loan request alert pipeline.

The failure these cover: a member submitted a loan request through the app and
no exco member was told until the member phoned the office. Every path that
creates a loan request must now log the request and alert the officers with the
application attached.
"""

import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

TEST_DB = os.path.abspath('.test-loan-request-alerts.db')
os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-loan-alerts')
os.environ.setdefault('ADMIN_PASSWORD', 'TestAdmin123')
os.environ.setdefault('TREASURER_PASSWORD', 'TestTreasurer123')
os.environ.setdefault('SECRETARY_PASSWORD', 'TestSecretary123')
os.environ.setdefault('FLASK_DEBUG', '1')
os.environ.setdefault('FIELD_ENCRYPTION_KEY', '05SmPJhNFMKwg9NysnBdQjKtqn3VwWDl1IiPIMAg2as=')
os.environ.pop('DATABASE_URL', None)
os.environ['SQLITE_DB_PATH'] = TEST_DB

try:
    os.remove(TEST_DB)
except FileNotFoundError:
    pass

import app as app_module  # noqa: E402
import loan_alerts as la  # noqa: E402
import loan_workflow as lw  # noqa: E402
from database import get_db, last_insert_id  # noqa: E402
from loan_pdf import build_loan_application_pdf  # noqa: E402


class LoanRequestAlertTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app_module.app
        cls.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    OFFICERS = (('treasurer', 'treasurer'), ('secretary', 'secretary'), ('excomember', 'exco'))

    def setUp(self):
        self.client = self.app.test_client()
        with self.app.app_context():
            db = get_db()
            # Seed the offices this suite needs. They are only created from
            # environment passwords at first boot, and this file may share a
            # database with other test modules.
            from werkzeug.security import generate_password_hash
            existing = {row['username'] for row in db.execute('SELECT username FROM users').fetchall()}
            for username, role in self.OFFICERS:
                if username not in existing:
                    db.execute(
                        'INSERT INTO users (username, password_hash, role, email, is_active) '
                        'VALUES (?, ?, ?, ?, 1)',
                        (username, generate_password_hash('TestOfficer123'), role,
                         f'{username}@coop.test'))
                else:
                    db.execute("UPDATE users SET role = ?, is_active = 1 WHERE username = ?",
                               (role, username))
            # Officers must have addresses, otherwise there is nothing to email.
            db.execute("UPDATE users SET email = username || '@coop.test' WHERE COALESCE(email, '') = ''")
            db.execute("UPDATE settings SET value = '1' WHERE key = 'loan_alert_enabled'")
            db.execute("UPDATE settings SET value = '1' WHERE key = 'loan_alert_attach_pdf'")
            db.execute("UPDATE settings SET value = '' WHERE key = 'loan_alert_extra_emails'")
            db.commit()

    # ── fixtures ─────────────────────────────────────────────────────────────

    def _member(self, suffix='0001', email=None):
        """An eligible member: joined a year ago, well past the savings floor."""
        email = email or f'member{suffix}@coop.test'
        with self.app.app_context():
            db = get_db()
            existing = db.execute('SELECT * FROM members WHERE member_number = ?',
                                  (f'TEST/LOAN/{suffix}',)).fetchone()
            if existing:
                return existing['id']
            joined = (datetime.now() - timedelta(days=400)).strftime('%Y-%m-%d %H:%M:%S')
            db.execute('''
                INSERT INTO members (member_number, first_name, last_name, email, phone,
                                     status, monthly_savings, total_savings, date_joined)
                VALUES (?, ?, ?, ?, '08000000000', 'active', 10000, 400000, ?)
            ''', (f'TEST/LOAN/{suffix}', 'Test', f'Member{suffix}', email, joined))
            member_id = last_insert_id(db)
            db.execute('''
                INSERT INTO savings (member_id, amount, month, date, payment_method)
                VALUES (?, 400000, '2025-01', ?, 'transfer')
            ''', (member_id, joined))
            db.commit()
            return member_id

    def _loan(self, member_id, stage=lw.STAGE_GUARANTORS, amount=200000, guarantors=0):
        # Guarantor members are created first: nesting a second app context (and
        # so a second SQLite connection) inside an open write transaction
        # deadlocks the test database.
        guarantor_ids = [self._member(suffix=f'G{i}') for i in range(guarantors)]
        with self.app.app_context():
            db = get_db()
            ref = f"LOAN/TEST/{datetime.now().strftime('%H%M%S%f')}"
            db.execute('''
                INSERT INTO loans (loan_number, member_id, amount, purpose, tenure,
                                   interest_rate, interest_method, total_repayment, balance,
                                   status, approval_stage, signature_name, terms_accepted,
                                   date_applied)
                VALUES (?, ?, ?, 'Regular', 12, 11, 'reducing_annual', ?, ?, 'pending', ?,
                        'Test Member', 1, ?)
            ''', (ref, member_id, amount, amount * 1.11, amount * 1.11, stage, datetime.now()))
            loan_id = last_insert_id(db)
            for gid in guarantor_ids:
                db.execute("INSERT INTO loan_guarantors (loan_id, member_id, status) "
                           "VALUES (?, ?, 'pending')", (loan_id, gid))
            db.commit()
            return loan_id

    # ── recipients ───────────────────────────────────────────────────────────

    def test_alert_recipients_cover_president_treasurer_and_secretary(self):
        with self.app.app_context():
            roles = {r['role'] for r in la.alert_recipients(get_db())}
        self.assertIn('admin', roles)        # President
        self.assertIn('treasurer', roles)
        self.assertIn('secretary', roles)    # General Secretary

    def test_extra_addresses_are_copied_on_alerts(self):
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE settings SET value = 'chairman@coop.test' "
                       "WHERE key = 'loan_alert_extra_emails'")
            db.commit()
            emails = {r['email'] for r in la.alert_recipients(db)}
        self.assertIn('chairman@coop.test', emails)

    # ── the core regression ──────────────────────────────────────────────────

    def test_submission_alerts_every_officer_even_while_guarantors_pending(self):
        """The bug: a request sitting at the guarantor stage told nobody."""
        member_id = self._member('0002')
        loan_id = self._loan(member_id, stage=lw.STAGE_GUARANTORS, guarantors=2)
        with self.app.app_context():
            db = get_db()
            with patch('email_service.send_email', return_value=True) as send_email:
                alerted = la.notify_loan_submitted(db, loan_id, channel='member_portal')

            self.assertGreaterEqual(alerted, 3)
            officer_notifications = db.execute('''
                SELECT COUNT(*) FROM notifications n JOIN users u ON u.id = n.user_id
                WHERE u.role IN ('admin', 'treasurer', 'secretary')
                  AND n.title LIKE 'New Loan Request%'
            ''').fetchone()[0]
            self.assertGreaterEqual(officer_notifications, 3)
            self.assertTrue(send_email.called)

    def test_submission_alert_email_carries_the_application_pdf(self):
        member_id = self._member('0003')
        loan_id = self._loan(member_id)
        with self.app.app_context():
            db = get_db()
            with patch('email_service.send_email', return_value=True) as send_email:
                la.notify_loan_submitted(db, loan_id, channel='mobile')

        officer_calls = [c for c in send_email.call_args_list
                         if 'New Loan Request' in (c.args[1] if len(c.args) > 1 else '')]
        self.assertTrue(officer_calls, 'no officer alert email was sent')
        for call in officer_calls:
            attachments = call.kwargs.get('attachments') or []
            self.assertEqual(len(attachments), 1)
            self.assertTrue(attachments[0]['filename'].endswith('.pdf'))
            self.assertTrue(attachments[0]['content'].startswith(b'%PDF'))
            self.assertEqual(attachments[0]['mimetype'], 'application/pdf')

    def test_submission_is_logged_with_recipients_and_channel(self):
        member_id = self._member('0004')
        loan_id = self._loan(member_id)
        with self.app.app_context():
            db = get_db()
            with patch('email_service.send_email', return_value=True):
                la.notify_loan_submitted(db, loan_id, channel='mobile')
            events = db.execute(
                'SELECT event_type, channel, delivery, status, recipient_role '
                'FROM loan_request_events WHERE loan_id = ?', (loan_id,)).fetchall()
            loan = db.execute('SELECT alert_count, first_alert_at, submission_channel '
                              'FROM loans WHERE id = ?', (loan_id,)).fetchone()

        types = {e['event_type'] for e in events}
        self.assertIn(la.EVENT_SUBMITTED, types)
        self.assertIn(la.EVENT_ALERT, types)
        self.assertIn('email', {e['delivery'] for e in events})
        self.assertIn('inapp', {e['delivery'] for e in events})
        self.assertEqual(loan['submission_channel'], 'mobile')
        self.assertGreaterEqual(loan['alert_count'], 1)
        self.assertIsNotNone(loan['first_alert_at'])

    def test_applicant_gets_an_acknowledgement(self):
        member_id = self._member('0005', email='ack@coop.test')
        loan_id = self._loan(member_id)
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO users (username, password_hash, role, email) "
                       "VALUES ('ackmember', 'x', 'member', 'ack@coop.test')")
            db.commit()
            with patch('email_service.send_email', return_value=True):
                la.notify_loan_submitted(db, loan_id, channel='member_portal')
            received = db.execute('''
                SELECT COUNT(*) FROM notifications n JOIN users u ON u.id = n.user_id
                WHERE u.username = 'ackmember' AND n.title = 'Loan Request Received'
            ''').fetchone()[0]
        self.assertEqual(received, 1)

    def test_alerts_can_be_switched_off(self):
        member_id = self._member('0006')
        loan_id = self._loan(member_id)
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE settings SET value = '0' WHERE key = 'loan_alert_enabled'")
            db.commit()
            with patch('email_service.send_email', return_value=True) as send_email:
                alerted = la.notify_loan_submitted(db, loan_id, channel='mobile')
            skipped = db.execute(
                "SELECT status FROM loan_request_events WHERE loan_id = ? AND event_type = 'submitted'",
                (loan_id,)).fetchone()
        self.assertEqual(alerted, 0)
        self.assertFalse(send_email.called)
        self.assertEqual(skipped['status'], 'skipped')

    # ── handover between offices ─────────────────────────────────────────────

    def test_stage_advance_alerts_the_officer_who_owns_the_stage(self):
        member_id = self._member('0007')
        loan_id = self._loan(member_id, stage=lw.STAGE_SECRETARY)
        with self.app.app_context():
            db = get_db()
            with patch('email_service.send_email', return_value=True):
                la.notify_stage_advanced(db, loan_id, lw.STAGE_TREASURER, actor_name='secretary')
                db.commit()
            treasurer_alerts = db.execute('''
                SELECT COUNT(*) FROM notifications n JOIN users u ON u.id = n.user_id
                WHERE u.role = 'treasurer' AND n.title = 'Loan Request Awaiting Your Approval'
            ''').fetchone()[0]
            stage_entered = db.execute('SELECT stage_entered_at FROM loans WHERE id = ?',
                                       (loan_id,)).fetchone()['stage_entered_at']
        self.assertGreaterEqual(treasurer_alerts, 1)
        self.assertIsNotNone(stage_entered)

    # ── chasing what goes quiet ──────────────────────────────────────────────

    def test_sweep_reminds_then_escalates_an_untouched_request(self):
        member_id = self._member('0008')
        loan_id = self._loan(member_id, stage=lw.STAGE_SECRETARY)
        with self.app.app_context():
            db = get_db()
            stale = datetime.now() - timedelta(hours=30)
            db.execute('UPDATE loans SET stage_entered_at = ? WHERE id = ?', (stale, loan_id))
            db.commit()
            with patch('email_service.send_email', return_value=True):
                summary = la.run_pipeline_sweep(db)
            self.assertGreaterEqual(summary['reminded'], 1)
            reminders = db.execute(
                "SELECT COUNT(*) FROM loan_request_events WHERE loan_id = ? AND event_type = ?",
                (loan_id, la.EVENT_REMINDER)).fetchone()[0]
            self.assertGreaterEqual(reminders, 1)

            # Throttled: a second sweep straight away must not chase again.
            with patch('email_service.send_email', return_value=True):
                again = la.run_pipeline_sweep(db)
            self.assertEqual(again['reminded'], 0)

            # Old enough, and past the reminder gap → escalation to the exco.
            db.execute('UPDATE loans SET stage_entered_at = ?, last_reminder_at = ? WHERE id = ?',
                       (datetime.now() - timedelta(hours=72),
                        datetime.now() - timedelta(hours=20), loan_id))
            db.commit()
            with patch('email_service.send_email', return_value=True):
                escalation = la.run_pipeline_sweep(db)
            self.assertGreaterEqual(escalation['escalated'], 1)
            escalated_at = db.execute('SELECT escalated_at FROM loans WHERE id = ?',
                                      (loan_id,)).fetchone()['escalated_at']
        self.assertIsNotNone(escalated_at)

    def test_sweep_chases_silent_guarantors(self):
        member_id = self._member('0009')
        loan_id = self._loan(member_id, stage=lw.STAGE_GUARANTORS, guarantors=2)
        with self.app.app_context():
            db = get_db()
            db.execute('UPDATE loans SET stage_entered_at = ? WHERE id = ?',
                       (datetime.now() - timedelta(hours=40), loan_id))
            db.commit()
            with patch('email_service.send_email', return_value=True):
                summary = la.run_pipeline_sweep(db)
            guarantor_events = db.execute(
                "SELECT COUNT(*) FROM loan_request_events "
                "WHERE loan_id = ? AND recipient_role = 'guarantor'", (loan_id,)).fetchone()[0]
        self.assertGreaterEqual(summary['guarantors_chased'], 1)
        self.assertGreaterEqual(guarantor_events, 2)

    def test_fresh_requests_are_not_chased(self):
        member_id = self._member('0010')
        self._loan(member_id, stage=lw.STAGE_SECRETARY)
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE loans SET stage_entered_at = ? WHERE status = 'pending'",
                       (datetime.now(),))
            db.commit()
            with patch('email_service.send_email', return_value=True):
                summary = la.run_pipeline_sweep(db)
        self.assertEqual(summary['reminded'], 0)
        self.assertEqual(summary['escalated'], 0)

    def test_pipeline_snapshot_counts_overdue_requests(self):
        member_id = self._member('0011')
        loan_id = self._loan(member_id, stage=lw.STAGE_SECRETARY)
        with self.app.app_context():
            db = get_db()
            db.execute('UPDATE loans SET stage_entered_at = ? WHERE id = ?',
                       (datetime.now() - timedelta(hours=50), loan_id))
            db.commit()
            snapshot = la.pipeline_snapshot(db)
        self.assertGreaterEqual(snapshot['pending'], 1)
        self.assertGreaterEqual(snapshot['overdue'], 1)
        self.assertGreaterEqual(snapshot['oldest_hours'], 50)

    # ── the PDF itself ───────────────────────────────────────────────────────

    def test_application_pdf_is_a_real_pdf_named_after_the_loan(self):
        member_id = self._member('0012')
        loan_id = self._loan(member_id, guarantors=1)
        with self.app.app_context():
            pdf_bytes, filename = build_loan_application_pdf(get_db(), loan_id)
        self.assertTrue(pdf_bytes.startswith(b'%PDF'))
        self.assertGreater(len(pdf_bytes), 2000)
        self.assertTrue(filename.startswith('loan-application-'))
        self.assertTrue(filename.endswith('.pdf'))
        self.assertNotIn('/', filename)

    def test_pdf_for_a_missing_loan_returns_nothing_instead_of_raising(self):
        with self.app.app_context():
            pdf_bytes, filename = build_loan_application_pdf(get_db(), 99999999)
        self.assertIsNone(pdf_bytes)
        self.assertEqual(filename, '')

    # ── email plumbing ───────────────────────────────────────────────────────

    def test_smtp_backend_builds_a_multipart_message_with_the_attachment(self):
        import email_service

        captured = {}

        class _FakeSMTP:
            def __init__(self, host, port, timeout=10):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def ehlo(self):
                pass

            def starttls(self, context=None):
                pass

            def login(self, user, password):
                pass

            def sendmail(self, sender, recipients, message):
                captured['message'] = message

        with patch.dict(os.environ, {'SMTP_HOST': 'smtp.test', 'SMTP_USER': 'u@test',
                                     'SMTP_PASS': 'p', 'MAIL_FROM': 'coop@test'}):
            with patch.object(email_service.smtplib, 'SMTP', _FakeSMTP):
                ok = email_service._send_via_smtp(
                    'exco@test', 'New Loan Request', '<p>hi</p>', 'hi',
                    attachments=[{'filename': 'loan-application-x.pdf',
                                  'content': b'%PDF-1.4 test',
                                  'mimetype': 'application/pdf'}])

        self.assertTrue(ok)
        self.assertIn('loan-application-x.pdf', captured['message'])
        self.assertIn('multipart/mixed', captured['message'])

    def test_oversized_attachments_are_dropped_not_sent(self):
        import email_service
        cleaned = email_service._clean_attachments([
            {'filename': 'huge.pdf', 'content': b'x' * (email_service.MAX_ATTACHMENT_BYTES + 1)},
            {'filename': 'empty.pdf', 'content': b''},
            {'filename': 'fine.pdf', 'content': b'%PDF', 'mimetype': 'application/pdf'},
        ])
        self.assertEqual([a['filename'] for a in cleaned], ['fine.pdf'])

    # ── routes ───────────────────────────────────────────────────────────────

    def login(self, username='admin', password='TestAdmin123'):
        response = self.client.post('/login', data={'username': username, 'password': password},
                                    follow_redirects=False)
        self.assertIn(response.status_code, (302, 303))

    def test_officer_can_download_the_application_pdf(self):
        member_id = self._member('0013')
        loan_id = self._loan(member_id)
        self.login()
        response = self.client.get(f'/loans/{loan_id}/application.pdf')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Content-Type'], 'application/pdf')
        self.assertTrue(response.data.startswith(b'%PDF'))

    def test_officer_can_resend_a_missed_alert(self):
        member_id = self._member('0014')
        loan_id = self._loan(member_id)
        self.login()
        with patch('email_service.send_email', return_value=True):
            response = self.client.post(f'/loans/{loan_id}/resend-alert', follow_redirects=False)
        self.assertIn(response.status_code, (302, 303))
        with self.app.app_context():
            resent = get_db().execute(
                "SELECT COUNT(*) FROM loan_request_events WHERE loan_id = ? AND channel = 'resend'",
                (loan_id,)).fetchone()[0]
        self.assertGreaterEqual(resent, 1)

    def test_opening_a_pending_request_starts_the_response_clock(self):
        member_id = self._member('0015')
        loan_id = self._loan(member_id)
        self.login()
        self.client.get(f'/loans/{loan_id}')
        with self.app.app_context():
            row = get_db().execute('SELECT first_response_at FROM loans WHERE id = ?',
                                   (loan_id,)).fetchone()
        self.assertIsNotNone(row['first_response_at'])

    def _member_user(self, email, username, password='MemberPass1!'):
        from werkzeug.security import generate_password_hash
        with self.app.app_context():
            db = get_db()
            if not db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone():
                db.execute('INSERT INTO users (username, password_hash, role, email, is_active) '
                           "VALUES (?, ?, 'member', ?, 1)",
                           (username, generate_password_hash(password), email))
                db.commit()
        return username, password

    def test_application_limits_block_all_submission_channels(self):
        import loan_limits
        member_id = self._member('0098', email='limits@coop.test')
        username, password = self._member_user('limits@coop.test', 'limitsmember')
        policy = {'max_amount': 0, 'amounts': {'Regular': 100000}, 'multiplier': 2, 'tenures': {'Regular': 6}}
        payloads = [({'amount': 100001, 'tenure': 6, 'purpose': 'Regular'}, b'Maximum loan amount'),
                    ({'amount': 100000, 'tenure': 7, 'purpose': 'Regular'}, b'Maximum tenure')]
        with patch.object(loan_limits, 'limits', return_value=policy):
            self.login()
            for payload, message in payloads:
                response = self.client.post('/loans/apply', data=dict(payload, member_id=member_id), follow_redirects=True)
                self.assertIn(message, response.data)
            self.client.get('/logout')
            self.login(username, password)
            for payload, message in payloads:
                response = self.client.post('/apply-loan-member', data=payload, follow_redirects=True)
                self.assertIn(message, response.data)
            login = self.client.post('/api/mobile/login', json={'username': 'limits@coop.test', 'password': password})
            self.assertEqual(login.status_code, 200)
            headers = {'Authorization': f"Bearer {login.get_json()['token']}"}
            for payload, message in payloads:
                for route in ('apply', 'schedule-preview'):
                    response = self.client.post('/api/mobile/v1/loans/' + route, headers=headers, json=payload)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn(message.decode(), response.get_json()['error'])
        with self.app.app_context():
            self.assertEqual(get_db().execute('SELECT COUNT(*) FROM loans WHERE member_id = ?', (member_id,)).fetchone()[0], 0)

    def test_member_portal_application_alerts_the_committee_end_to_end(self):
        member_id = self._member('0018', email='portal.applicant@coop.test')
        guarantors = [self._member('0019'), self._member('0020')]
        username, password = self._member_user('portal.applicant@coop.test', 'portalapplicant')
        self.login(username, password)

        with patch('email_service.send_email', return_value=True) as send_email:
            response = self.client.post('/apply-loan-member', data={
                'amount': '150000',
                'purpose': 'Regular',
                'tenure': '12',
                'signature_name': 'Test Member0018',
                'payment_collateral_type': 'standing_order',
                'accept_terms': '1',
                'data_processing_consent': '1',
                'repayment_schedule_accepted': '1',
                'credit_check_consent': '1',
                'bank_statement_ack': '1',
                'guarantors': [str(g) for g in guarantors],
            }, follow_redirects=False)
        self.assertIn(response.status_code, (302, 303))

        with self.app.app_context():
            db = get_db()
            loan = db.execute('SELECT id, submission_channel, alert_count FROM loans '
                              'WHERE member_id = ? ORDER BY id DESC', (member_id,)).fetchone()
            self.assertIsNotNone(loan, 'the application was not created')
            alerted_roles = {row['recipient_role'] for row in db.execute(
                "SELECT recipient_role FROM loan_request_events "
                "WHERE loan_id = ? AND event_type = 'alert_sent'", (loan['id'],)).fetchall()}
        self.assertEqual(loan['submission_channel'], 'member_portal')
        self.assertGreaterEqual(loan['alert_count'], 1)
        self.assertTrue({'admin', 'treasurer', 'secretary'} <= alerted_roles)
        attached = [c for c in send_email.call_args_list if c.kwargs.get('attachments')]
        self.assertTrue(attached, 'no alert email carried the application PDF')

    def test_mobile_application_alerts_the_committee_end_to_end(self):
        self._member('0021', email='mobile.applicant@coop.test')
        guarantors = [self._member('0022'), self._member('0023')]
        self._member_user('mobile.applicant@coop.test', 'mobileapplicant')

        login = self.client.post('/api/mobile/login',
                                 json={'username': 'mobile.applicant@coop.test',
                                       'password': 'MemberPass1!'})
        self.assertEqual(login.status_code, 200)
        headers = {'Authorization': f"Bearer {login.get_json()['token']}"}

        with patch('email_service.send_email', return_value=True) as send_email:
            response = self.client.post('/api/mobile/v1/loans/apply', headers=headers, json={
                'amount': 150000,
                'tenure': 12,
                'purpose': 'Regular',
                'signature_name': 'Test Member0021',
                'payment_collateral_type': 'standing_order',
                'accept_terms': True,
                'data_processing_consent': True,
                'repayment_schedule_accepted': True,
                'credit_check_consent': True,
                'bank_statement_ack': True,
                'guarantor_ids': guarantors,
            })
        self.assertEqual(response.status_code, 201, response.get_json())
        loan_id = response.get_json()['loan']['id']

        with self.app.app_context():
            db = get_db()
            events = db.execute(
                "SELECT event_type, channel FROM loan_request_events WHERE loan_id = ?",
                (loan_id,)).fetchall()
        self.assertIn('submitted', {e['event_type'] for e in events})
        self.assertIn('alert_sent', {e['event_type'] for e in events})
        self.assertEqual({e['channel'] for e in events if e['channel']}, {'mobile'})
        self.assertTrue([c for c in send_email.call_args_list if c.kwargs.get('attachments')])

        pdf = self.client.get(f'/api/mobile/v1/loans/{loan_id}/application.pdf', headers=headers)
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b'%PDF'))

    def test_loans_pages_render_the_request_queue_and_notification_log(self):
        member_id = self._member('0024')
        loan_id = self._loan(member_id)
        with self.app.app_context():
            db = get_db()
            with patch('email_service.send_email', return_value=True):
                la.notify_loan_submitted(db, loan_id, channel='admin')
        self.login()
        listing = self.client.get('/loans')
        self.assertEqual(listing.status_code, 200)
        self.assertIn(b'awaiting the committee', listing.data)
        detail = self.client.get(f'/loans/{loan_id}')
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b'Notification Log', detail.data)
        self.assertIn(b'Application PDF', detail.data)

    def test_settings_page_saves_and_clears_the_alert_switches(self):
        self.login()
        page = self.client.get('/settings')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Loan Request Alerts', page.data)

        # Unchecked switches are not submitted at all — the group marker is what
        # lets the server write them back as off.
        self.client.post('/settings/update', data={
            '_settings_group': 'loan_alerts',
            'loan_alert_roles': 'admin,secretary',
            'loan_alert_extra_emails': '',
            'loan_alert_sla_hours': '6',
        }, follow_redirects=False)
        with self.app.app_context():
            db = get_db()
            self.assertFalse(la.alerts_enabled(db))
            self.assertEqual(la.alert_roles(db), ('admin', 'secretary'))
            self.assertEqual(la._hours(db, 'loan_alert_sla_hours', 24), 6)

        self.client.post('/settings/update', data={
            '_settings_group': 'loan_alerts',
            'loan_alert_enabled': '1',
            'loan_alert_attach_pdf': '1',
            'loan_alert_roles': 'admin,treasurer,secretary,exco',
            'loan_alert_sla_hours': '24',
        }, follow_redirects=False)
        with self.app.app_context():
            self.assertTrue(la.alerts_enabled(get_db()))

    def test_sweep_endpoint_rejects_callers_without_a_token(self):
        response = self.client.post('/tasks/loans/pipeline-sweep')
        self.assertEqual(response.status_code, 403)

    def test_sweep_endpoint_accepts_the_scheduler_token(self):
        with patch.dict(os.environ, {'TASK_RUNNER_TOKEN': 'sweep-token'}):
            with patch('email_service.send_email', return_value=True):
                response = self.client.post('/tasks/loans/pipeline-sweep',
                                            headers={'X-Task-Token': 'sweep-token'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])

    def test_member_can_download_their_own_application_but_not_another_member_s(self):
        mine = self._member('0016', email='mine@coop.test')
        other = self._member('0017', email='other@coop.test')
        my_loan = self._loan(mine)
        other_loan = self._loan(other)
        with self.app.app_context():
            db = get_db()
            from werkzeug.security import generate_password_hash
            db.execute("INSERT INTO users (username, password_hash, role, email, is_active) "
                       "VALUES ('mineuser', ?, 'member', 'mine@coop.test', 1)",
                       (generate_password_hash('MinePass123'),))
            db.commit()
        self.login('mineuser', 'MinePass123')
        ok = self.client.get(f'/loan-detail/{my_loan}/application.pdf')
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.data.startswith(b'%PDF'))
        denied = self.client.get(f'/loan-detail/{other_loan}/application.pdf',
                                 follow_redirects=False)
        self.assertIn(denied.status_code, (302, 303))


if __name__ == '__main__':
    unittest.main()


class StageRoleTests(unittest.TestCase):
    """Each approval stage is signed by its own officer, and only that officer.

    The President used to be able to act at every stage, which collapsed a
    three-officer control into one signature carrying a loan from application
    all the way to disbursement.
    """

    def test_each_stage_has_exactly_one_approving_role(self):
        roles = ('admin', 'secretary', 'treasurer', 'exco', 'member')
        for stage in (lw.STAGE_SECRETARY, lw.STAGE_TREASURER, lw.STAGE_PRESIDENT):
            allowed = [r for r in roles if lw.can_act(r, stage)]
            self.assertEqual(len(allowed), 1,
                             f'{stage} should be signable by exactly one role, got {allowed}')

    def test_the_president_cannot_sign_the_other_stages(self):
        self.assertFalse(lw.can_act('admin', lw.STAGE_SECRETARY))
        self.assertFalse(lw.can_act('admin', lw.STAGE_TREASURER))
        # But still signs their own.
        self.assertTrue(lw.can_act('admin', lw.STAGE_PRESIDENT))

    def test_officers_sign_only_their_own_stage(self):
        self.assertTrue(lw.can_act('secretary', lw.STAGE_SECRETARY))
        self.assertFalse(lw.can_act('secretary', lw.STAGE_TREASURER))
        self.assertFalse(lw.can_act('secretary', lw.STAGE_PRESIDENT))
        self.assertTrue(lw.can_act('treasurer', lw.STAGE_TREASURER))
        self.assertFalse(lw.can_act('treasurer', lw.STAGE_SECRETARY))

    def test_a_stage_nobody_signs_is_refused(self):
        # Guarantor consent is not an officer approval.
        for role in ('admin', 'secretary', 'treasurer'):
            self.assertFalse(lw.can_act(role, lw.STAGE_GUARANTORS))
            self.assertFalse(lw.can_act(role, lw.STAGE_APPROVED))
