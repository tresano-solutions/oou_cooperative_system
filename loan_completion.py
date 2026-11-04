"""Transactional completion-email outbox; dispatch only after payment commit."""
import logging
from datetime import datetime

from flask import render_template
from email_service import send_email_detailed

log = logging.getLogger(__name__)


def queue_completion(db, loan_id, repayment_id):
    loan = db.execute('SELECT * FROM loans WHERE id = ?', (loan_id,)).fetchone()
    repayment = db.execute('SELECT * FROM repayments WHERE id = ? AND loan_id = ?',
                           (repayment_id, loan_id)).fetchone()
    if (not loan or loan['status'] != 'completed' or float(loan['balance'] or 0) != 0
            or not repayment or repayment['reversed_at'] or float(repayment['amount'] or 0) <= 0):
        return
    member = db.execute('SELECT email FROM members WHERE id = ?', (loan['member_id'],)).fetchone()
    recipients = {}
    if member and (member['email'] or '').strip():
        recipients[member['email'].strip().lower()] = 'member'
    # Committee officers receive separate messages, never a shared CC list.
    for officer in db.execute("SELECT email FROM users WHERE is_active = 1 AND role IN ('admin','exco','secretary','treasurer')").fetchall():
        email = (officer['email'] or '').strip().lower()
        if email:
            recipients.setdefault(email, 'exco')
    for email, audience in recipients.items():
        db.execute('''INSERT INTO loan_completion_emails
                      (loan_id, repayment_id, recipient, audience) VALUES (?, ?, ?, ?)
                      ON CONFLICT (loan_id, repayment_id, recipient) DO NOTHING''',
                   (loan_id, repayment_id, email, audience))


def dispatch_completions(db, limit=30):
    """Called after commit, or by the existing protected loan-pipeline sweep.

    A durable claim prevents parallel workers sending the same email. Interrupted
    'sending' rows require review, not blind retries (provider outcome is unknown).
    """
    try:
        rows = db.execute("SELECT * FROM loan_completion_emails WHERE status IN ('pending','failed') AND attempts < 3 ORDER BY id LIMIT ?", (limit,)).fetchall()
        for row in rows:
            loan = db.execute('SELECT * FROM loans WHERE id = ?', (row['loan_id'],)).fetchone()
            rep = db.execute('SELECT * FROM repayments WHERE id = ?', (row['repayment_id'],)).fetchone()
            if not loan or loan['status'] != 'completed' or float(loan['balance'] or 0) != 0 or not rep or rep['reversed_at']:
                db.execute("UPDATE loan_completion_emails SET status='cancelled' WHERE id=? AND status IN ('pending','failed')", (row['id'],))
                db.commit()
                continue
            member = db.execute('SELECT * FROM members WHERE id = ?', (loan['member_id'],)).fetchone()
            # Re-check address/role before a delayed retry.
            if row['audience'] == 'member':
                eligible = member and (member['email'] or '').strip().lower() == row['recipient']
            else:
                eligible = db.execute("SELECT id FROM users WHERE lower(email)=? AND is_active=1 AND role IN ('admin','exco','secretary','treasurer')", (row['recipient'],)).fetchone()
            if not eligible:
                db.execute("UPDATE loan_completion_emails SET status='cancelled' WHERE id=? AND status IN ('pending','failed')", (row['id'],))
                db.commit()
                continue
            claimed = db.execute("UPDATE loan_completion_emails SET status='sending', attempts=attempts+1 WHERE id=? AND status IN ('pending','failed') AND attempts < 3", (row['id'],))
            db.commit()
            if claimed.rowcount != 1:
                continue
            title = ('Congratulations - your loan is fully repaid' if row['audience'] == 'member'
                     else 'Member loan fully repaid')
            try:
                html = render_template('emails/loan-completion.html', member=member, loan=loan,
                                       repayment=rep, audience=row['audience'])
                text = (f"{title}\nLoan: {loan['loan_number']}\n"
                        f"Member: {member['first_name']} {member['last_name']}\n"
                        f"Final payment: NGN {float(rep['amount']):,.2f}\n"
                        f"Settlement date: {str(rep['date'])[:10]}\nOutstanding balance: NGN 0.00\n"
                        'This confirmation applies to this loan only. Sign in to review your statement.')
                result = send_email_detailed(row['recipient'], f"{title} - {loan['loan_number']}", html, text)
                ok = bool(result.get('ok'))
                db.execute('UPDATE loan_completion_emails SET status=?, sent_at=?, last_error=? WHERE id=?',
                           ('sent' if ok else 'failed', datetime.now() if ok else None,
                            '' if ok else 'Provider did not accept email; check email configuration and provider logs.', row['id']))
                db.commit()
            except Exception:
                # Unknown provider outcome: do not auto-retry and risk duplicates.
                log.exception('Completion email outcome requires review: outbox %s', row['id'])
                db.rollback()
        return len(rows)
    except Exception:
        db.rollback()
        log.exception('Completion email dispatch failed; payment remains committed')
        return 0
