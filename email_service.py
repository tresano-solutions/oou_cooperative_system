"""
email_service.py — Outgoing email, two back-ends supported:

  1. Resend API  (requires a verified domain — best for production)
  2. SMTP relay  (works with any email address — good for getting started fast)
     Recommended provider when you have no domain: Brevo (brevo.com, free,
     300 emails/day, verify sender email only — no domain needed).

Priority:  Resend is tried first if RESEND_API_KEY is set.
           SMTP is used if smtp_host + smtp_user + smtp_pass are configured.

All send_* helpers are fire-and-forget: they log failures but never raise,
so an email error never crashes the main request.
"""
import atexit
import base64
import os
import json
import logging
import smtplib
import ssl
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import parseaddr
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText

log = logging.getLogger(__name__)


# ── Background sender ───────────────────────────────────────────────────────────
#
# Sending email over HTTP/SMTP can take several seconds — or block on a timeout
# if a provider is slow. Doing that inside the web request ties up a gunicorn
# worker (we only run 2 per client) and can time out the user's browser, which
# is worst for bulk sends. We hand the actual delivery to a small thread pool so
# the request returns immediately. Each task re-enters a fresh Flask app context
# so get_db()/settings lookups keep working after the request has ended.

_MAX_EMAIL_WORKERS = int(os.environ.get('EMAIL_WORKERS', '2') or '2')
_executor = ThreadPoolExecutor(max_workers=_MAX_EMAIL_WORKERS,
                               thread_name_prefix='email')
atexit.register(lambda: _executor.shutdown(wait=False))


def run_in_background(fn, *args, **kwargs):
    """Run fn(*args, **kwargs) off the request thread on the email pool.

    If called within a Flask request/app context, the task re-enters a fresh
    app context so database and settings lookups keep working after the request
    ends. Exceptions are logged, never raised (fire-and-forget).

    In TESTING mode the task runs inline (synchronously) so tests stay
    deterministic.
    """
    app = None
    testing = False
    try:
        from flask import current_app, has_app_context
        if has_app_context():
            app = current_app._get_current_object()
            testing = bool(app.config.get('TESTING'))
    except Exception:
        app = None

    # In tests, run inline within the current app context so behaviour is
    # deterministic (no thread races) and errors surface immediately rather
    # than being swallowed. This reuses the request's database connection.
    if testing:
        return fn(*args, **kwargs)

    def _task():
        try:
            if app is not None:
                with app.app_context():
                    return fn(*args, **kwargs)
            return fn(*args, **kwargs)
        except Exception:
            log.exception('Background email task failed')

    try:
        _executor.submit(_task)
    except RuntimeError:
        # Pool already shut down (interpreter exiting) — run inline as a fallback.
        _task()


# ── Config helpers ─────────────────────────────────────────────────────────────

def _db_setting(key: str) -> str:
    """Read one value from the settings table (returns '' on any error)."""
    try:
        from database import get_db
        db  = get_db()
        row = db.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        return (row['value'] or '').strip() if row else ''
    except Exception:
        return ''


def _env_first(*names: str) -> str:
    """Return the first non-empty environment value from a list of aliases."""
    for name in names:
        value = os.environ.get(name, '').strip()
        if value:
            return value
    return ''


def _cfg(env_var: str, db_key: str, *aliases: str) -> str:
    """Env var takes precedence; falls back to DB setting."""
    return _env_first(env_var, *aliases) or _db_setting(db_key)


def _truthy(value: str) -> bool:
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def _is_enabled() -> bool:
    configured = _cfg('MAIL_ENABLED', 'mail_enabled', 'ENABLE_EMAIL_NOTIFICATIONS')
    return _truthy(configured)


def _delivery_result(ok: bool, provider: str, error: str = '') -> dict:
    return {
        'ok': ok,
        'provider': provider,
        'error': str(error or '')[:1000],
    }


# ── Attachments ────────────────────────────────────────────────────────────────
#
# An attachment is a plain dict so callers never need a provider-specific type:
#     {'filename': 'loan-application.pdf',
#      'content':  b'%PDF-1.4 ...',
#      'mimetype': 'application/pdf'}       # mimetype optional
# Every back-end below converts this into whatever the provider expects.

MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024   # provider limits are ~10MB after base64


def _clean_attachments(attachments) -> list:
    """Drop empty/oversized entries and normalise keys. Never raises."""
    cleaned = []
    for item in attachments or []:
        try:
            content = item.get('content')
            filename = (item.get('filename') or 'attachment').strip()
            if not content:
                continue
            if isinstance(content, str):
                content = content.encode('utf-8')
            if len(content) > MAX_ATTACHMENT_BYTES:
                log.warning('Attachment %s skipped: %d bytes exceeds limit', filename, len(content))
                continue
            cleaned.append({
                'filename': filename,
                'content': content,
                'mimetype': item.get('mimetype') or 'application/octet-stream',
            })
        except Exception:
            log.exception('Bad email attachment skipped')
    return cleaned


def _b64(content: bytes) -> str:
    return base64.b64encode(content).decode('ascii')


# ── Resend back-end ────────────────────────────────────────────────────────────

def _send_via_resend(to: str, subject: str, html: str, attachments=None) -> bool:
    api_key  = _cfg('RESEND_API_KEY', 'resend_api_key')
    from_addr = _cfg('MAIL_FROM',      'mail_from') or f'{_coop_name()} <noreply@cooperativems.com>'
    if not api_key:
        return False
    try:
        import resend
        resend.api_key = api_key
        payload = {
            'from':    from_addr,
            'to':      [to] if isinstance(to, str) else list(to),
            'subject': subject,
            'html':    html,
        }
        files = _clean_attachments(attachments)
        if files:
            payload['attachments'] = [
                {'filename': f['filename'], 'content': _b64(f['content'])}
                for f in files
            ]
        resend.Emails.send(payload)
        log.info('Resend OK: "%s" → %s', subject, to)
        return True
    except Exception as exc:
        log.error('Resend failed ("%s" → %s): %s', subject, to, exc)
        return False


# ── SMTP back-end (Gmail, Brevo, Outlook, any provider) ───────────────────────

def _sender_from_address(from_addr: str) -> dict:
    """Convert 'Name <email@example.com>' into Brevo's sender object."""
    name, email = parseaddr(from_addr)
    sender = {'email': email or from_addr}
    if name:
        sender['name'] = name
    return sender


def _recipient_list(to) -> list:
    recipients = [to] if isinstance(to, str) else list(to)
    return [{'email': parseaddr(recipient)[1] or recipient} for recipient in recipients]


def _send_via_brevo(to: str, subject: str, html: str, text: str = '', attachments=None) -> bool:
    api_key = _cfg('BREVO_API_KEY', 'brevo_api_key', 'SENDINBLUE_API_KEY')
    from_addr = (
        _cfg('MAIL_FROM', 'mail_from', 'MAIL_DEFAULT_SENDER', 'COOP_EMAIL')
        or f'{_coop_name()} <noreply@cooperativems.com>'
    )
    if not api_key:
        return False

    payload = {
        'sender': _sender_from_address(from_addr),
        'to': _recipient_list(to),
        'subject': subject,
        'htmlContent': html,
    }
    if text:
        payload['textContent'] = text
    files = _clean_attachments(attachments)
    if files:
        payload['attachment'] = [
            {'name': f['filename'], 'content': _b64(f['content'])}
            for f in files
        ]

    request = urllib.request.Request(
        'https://api.brevo.com/v3/smtp/email',
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'accept': 'application/json',
            'api-key': api_key,
            'content-type': 'application/json',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if 200 <= response.status < 300:
                log.info('Brevo API OK: "%s" â†’ %s', subject, to)
                return True
            log.error('Brevo API failed ("%s" â†’ %s): HTTP %s',
                      subject, to, response.status)
            return False
    except urllib.error.HTTPError as exc:
        body = exc.read(500).decode('utf-8', errors='replace')
        log.error('Brevo API failed ("%s" â†’ %s): HTTP %s %s',
                  subject, to, exc.code, body)
        return False
    except Exception as exc:
        log.error('Brevo API failed ("%s" â†’ %s): %s', subject, to, exc)
        return False


def _send_via_smtp(to: str, subject: str, html: str, text: str = '', attachments=None) -> bool:
    host     = _cfg('SMTP_HOST',     'smtp_host', 'MAIL_SERVER')
    port_str = _cfg('SMTP_PORT',     'smtp_port', 'MAIL_PORT') or '587'
    user     = _cfg('SMTP_USER',     'smtp_user', 'MAIL_USERNAME')
    password = _cfg('SMTP_PASS',     'smtp_pass', 'MAIL_PASSWORD')
    from_addr = (
        _cfg('MAIL_FROM', 'mail_from', 'MAIL_DEFAULT_SENDER', 'COOP_EMAIL')
        or user
    )
    use_ssl = _truthy(_env_first('SMTP_USE_SSL', 'MAIL_USE_SSL'))
    use_tls = _truthy(_env_first('SMTP_USE_TLS', 'MAIL_USE_TLS'))

    if not (host and user and password):
        return False

    try:
        port = int(port_str)
        files = _clean_attachments(attachments)
        body = MIMEMultipart('alternative')
        if text:
            body.attach(MIMEText(text, 'plain'))
        body.attach(MIMEText(html, 'html'))

        if files:
            # Attachments require a mixed container with the text/html
            # alternative part nested inside it.
            msg = MIMEMultipart('mixed')
            msg.attach(body)
            for f in files:
                subtype = f['mimetype'].split('/')[-1] or 'octet-stream'
                part = MIMEApplication(f['content'], _subtype=subtype)
                part.add_header('Content-Disposition', 'attachment', filename=f['filename'])
                msg.attach(part)
        else:
            msg = body
        msg['Subject'] = subject
        msg['From']    = from_addr
        msg['To']      = to

        ctx = ssl.create_default_context()
        smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with smtp_cls(host, port, timeout=10) as server:
            server.ehlo()
            if use_tls and not use_ssl:
                server.starttls(context=ctx)
            server.login(user, password)
            server.sendmail(from_addr, [to], msg.as_string())
        log.info('SMTP OK: "%s" → %s', subject, to)
        return True
    except Exception as exc:
        log.error('SMTP failed ("%s" → %s): %s', subject, to, exc)
        return False


# ── Core send (tries Resend first, then SMTP) ─────────────────────────────────

# ── Branded email shell ────────────────────────────────────────────────────────

APP_NAME = 'CoopMS'   # the product; the cooperative's own name comes from settings


def _coop_name() -> str:
    """The client cooperative's own name for this deployment (from settings)."""
    return _db_setting('coop_name') or 'Your Cooperative'


def _system_cta_url() -> str:
    return _cfg('SYSTEM_CTA_URL', 'system_cta_url') or 'https://cooperativems.com'


def _wrap_email(inner_html: str) -> str:
    """Wrap message content in the branded shell: the client cooperative's own
    name in the header/footer (never hard-coded), plus a CoopMS advert + CTA at
    the base. Idempotent — safe to call on any content."""
    if not inner_html or 'data-coopms-email' in inner_html:
        return inner_html
    coop = _coop_name()
    year = datetime.now().year
    cta = _system_cta_url()
    advert = (f'Powered by <strong style="color:#ffffff;">{APP_NAME}</strong> — the all-in-one '
              f'platform to run a cooperative: members, savings, loans, investments and real '
              f'double-entry accounting in one place.')
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body data-coopms-email style="margin:0;padding:0;background:#f4f6f9;font-family:'Segoe UI',Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f9;padding:24px 12px;">
    <tr><td align="center">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:10px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.06);">
        <tr><td style="background:#082b66;padding:24px 30px;text-align:center;">
          <div style="color:#ffffff;font-size:22px;font-weight:bold;letter-spacing:.2px;">{coop}</div>
        </td></tr>
        <tr><td style="padding:32px 30px;color:#333333;font-size:16px;line-height:1.6;">
          {inner_html}
        </td></tr>
        <tr><td style="padding:18px 30px;background:#f8f9fa;text-align:center;color:#888888;font-size:13px;border-top:1px solid #eeeeee;">
          This is an automated message from {coop}. Please do not reply to this email.<br>
          &copy; {year} {coop}. All rights reserved.
        </td></tr>
        <tr><td style="padding:22px 30px;background:#061e4a;text-align:center;">
          <div style="color:#cbd5e1;font-size:13px;line-height:1.55;margin-bottom:16px;">{advert}</div>
          <table role="presentation" cellspacing="0" cellpadding="0" align="center" style="margin:0 auto;">
            <tr><td style="background:#f4b51c;border-radius:6px;padding:11px 24px;text-align:center;">
              <a href="{cta}" style="color:#082b66;text-decoration:none;font-weight:bold;font-size:13px;display:inline-block;">Digitize your cooperative with {APP_NAME} &rarr;</a>
            </td></tr>
          </table>
        </td></tr>
      </table>
      <div style="color:#aaaaaa;font-size:11px;margin-top:12px;">Delivered by {APP_NAME}</div>
    </td></tr>
  </table>
</body>
</html>"""


def _deliver(to: str, subject: str, html: str, text: str = '', attachments=None) -> bool:
    """Try each configured provider in order (Resend → Brevo → SMTP).
    Expects `html` already wrapped in the branded shell."""
    if _cfg('RESEND_API_KEY', 'resend_api_key'):
        if _send_via_resend(to, subject, html, attachments):
            return True
        log.warning('Resend failed; trying next email provider for "%s"', subject)

    if _cfg('BREVO_API_KEY', 'brevo_api_key', 'SENDINBLUE_API_KEY'):
        if _send_via_brevo(to, subject, html, text, attachments):
            return True
        log.warning('Brevo API failed; trying SMTP fallback for "%s"', subject)

    if _cfg('SMTP_HOST', 'smtp_host', 'MAIL_SERVER'):
        return _send_via_smtp(to, subject, html, text, attachments)

    log.warning('No email provider configured — skipped: "%s"', subject)
    return False


def _deliver_detailed(to: str, subject: str, html: str, text: str = '') -> dict:
    """Try configured providers and return provider/status details."""
    attempted = []

    if _cfg('RESEND_API_KEY', 'resend_api_key'):
        attempted.append('resend')
        if _send_via_resend(to, subject, html):
            return _delivery_result(True, 'resend')
        log.warning('Resend failed; trying next email provider for "%s"', subject)

    if _cfg('BREVO_API_KEY', 'brevo_api_key', 'SENDINBLUE_API_KEY'):
        attempted.append('brevo')
        if _send_via_brevo(to, subject, html, text):
            return _delivery_result(True, 'brevo')
        log.warning('Brevo API failed; trying SMTP fallback for "%s"', subject)

    if _cfg('SMTP_HOST', 'smtp_host', 'MAIL_SERVER'):
        attempted.append('smtp')
        if _send_via_smtp(to, subject, html, text):
            return _delivery_result(True, 'smtp')

    if attempted:
        return _delivery_result(
            False,
            attempted[-1],
            'Provider returned failure. Check app logs or provider dashboard for the exact rejection reason.',
        )
    log.warning('No email provider configured - skipped: "%s"', subject)
    return _delivery_result(False, 'none', 'No email provider configured')


def send_email(to: str, subject: str, html: str, text: str = '',
               background: bool = False, attachments=None) -> bool:
    """
    Send one transactional email, wrapped in the branded shell.
    Tries Resend if configured, falls back to Brevo, then SMTP.

    When background=False (default) the send is synchronous and the return value
    reflects the real delivery result — use this when the caller needs to know
    (e.g. a "send test email" button, or a password-reset link).

    When background=True the branded HTML is built now (in the request, where the
    database is available) but the provider call is dispatched to a worker thread
    so the request returns immediately. Returns True to mean "queued" — the real
    outcome is logged. Use this for fire-and-forget notifications and bulk sends.

    `attachments` is a list of {'filename', 'content' (bytes), 'mimetype'} dicts;
    they are rendered for whichever provider ends up delivering the message.
    """
    if not _is_enabled():
        log.debug('Email disabled — skipped: "%s"', subject)
        return False

    html = _wrap_email(html)

    if background:
        run_in_background(_deliver, to, subject, html, text, attachments)
        return True
    return _deliver(to, subject, html, text, attachments)


def send_email_detailed(to: str, subject: str, html: str, text: str = '') -> dict:
    """Send synchronously and return actual provider delivery status."""
    if not _is_enabled():
        log.debug('Email disabled - skipped: "%s"', subject)
        return _delivery_result(False, 'none', 'Email is disabled')

    return _deliver_detailed(to, subject, _wrap_email(html), text)


# ── Public send helpers ────────────────────────────────────────────────────────

def send_welcome_email(recipient: str, member: dict) -> None:
    try:
        from flask import render_template
        html = render_template('emails/welcome.html', member=member, login_url='')
    except Exception:
        full_name = member.get('full_name', 'Member')
        num       = member.get('member_number', '')
        html = (
            f'<p>Dear {full_name},</p>'
            f'<p>Welcome to {_coop_name()}! Your member number is <strong>{num}</strong>.</p>'
            f'<p>Please log in to your member portal to view your account.</p>'
        )
    send_email(recipient, f'Welcome to {_coop_name()}!', html, background=True)


def send_member_onboarding_email(recipient: str, member: dict, username: str,
                                 setup_url: str, profile_url: str = '') -> None:
    full_name = member.get('full_name') or 'Member'
    member_number = member.get('member_number') or ''
    html = (
        f'<p>Dear {full_name},</p>'
        f'<p>Your cooperative portal profile has been created.</p>'
        f'<table cellpadding="6" cellspacing="0" style="border-collapse:collapse">'
        f'<tr><td><strong>Member number</strong></td><td>{member_number}</td></tr>'
        f'<tr><td><strong>Username</strong></td><td>{username}</td></tr>'
        f'</table>'
        f'<p>Use the secure link below to choose your password and activate your portal access.</p>'
        f'<p><a href="{setup_url}">Set up your password</a></p>'
        f'<p>This link can be used once and expires after 24 hours.</p>'
    )
    if profile_url:
        html += f'<p>After setting your password, review your profile here: <a href="{profile_url}">{profile_url}</a></p>'
    html += '<p>If any profile detail is wrong, contact the cooperative office.</p>'

    text = (
        f'Dear {full_name},\n\n'
        f'Your cooperative portal profile has been created.\n'
        f'Member number: {member_number}\n'
        f'Username: {username}\n'
        f'Set up your password: {setup_url}\n\n'
        f'This link can be used once and expires after 24 hours.\n'
    )
    send_email(recipient, 'Set up your Cooperative Portal Account', html, text,
               background=True)


def send_savings_change_request_email(recipient: str, member: dict,
                                      current_amount: float, requested_amount: float,
                                      reason: str = '', review_url: str = '') -> None:
    """Tell an officer a member wants to change their monthly savings.

    `review_url` is built by the caller, inside the request. Background email
    threads have no request context, so url_for(_external=True) cannot be called
    from in here.
    """
    name = member.get('full_name') or f"{member.get('first_name', '')} {member.get('last_name', '')}".strip()
    number = member.get('member_number', '')
    direction = 'an increase' if requested_amount > current_amount else 'a reduction'
    button = (f'<p style="margin:22px 0"><a href="{review_url}" '
              f'style="background:#1a3a6c;color:#fff;padding:11px 20px;border-radius:6px;'
              f'text-decoration:none;font-weight:600">Review this request</a></p>'
              if review_url else '')
    html = (
        f'<p>{name} ({number}) has requested <strong>{direction}</strong> '
        f'to their monthly savings.</p>'
        f'<table cellpadding="6" style="border-collapse:collapse;margin:14px 0">'
        f'<tr><td style="color:#5f6d86">Currently</td>'
        f'<td style="font-weight:600">&#8358;{current_amount:,.2f}</td></tr>'
        f'<tr><td style="color:#5f6d86">Requested</td>'
        f'<td style="font-weight:600">&#8358;{requested_amount:,.2f}</td></tr>'
        f'<tr><td style="color:#5f6d86">Reason</td><td>{reason or "—"}</td></tr>'
        f'</table>'
        f'{button}'
        f'<p style="color:#5f6d86;font-size:13px">The change does not take effect until '
        f'an officer approves it.</p>'
    )
    send_email(recipient, f'Savings change request — {name}', html, background=True)


def send_loan_approval_email(recipient: str, member: dict,
                              loan: dict, loan_url: str = '') -> None:
    try:
        from flask import render_template
        html = render_template('emails/loan-approval.html',
                               member=member, loan=loan, loan_url=loan_url)
    except Exception:
        amount = loan.get('amount', 0)
        html = (
            f'<p>Dear {member.get("full_name", "Member")},</p>'
            f'<p>Your loan application for <strong>&#8358;{amount:,.2f}</strong> '
            f'has been <strong>approved</strong>.</p>'
            f'<p>The funds will be disbursed shortly. Log in to your portal for details.</p>'
        )
    send_email(recipient, 'Your Loan Has Been Approved!', html, background=True)


def send_loan_rejection_email(recipient: str, member: dict,
                               rejection_reason: str = '',
                               contact_url: str = '') -> None:
    try:
        from flask import render_template
        html = render_template('emails/loan-rejection.html',
                               member=member,
                               rejection_reason=rejection_reason,
                               contact_url=contact_url)
    except Exception:
        reason_line = f'<p>Reason: {rejection_reason}</p>' if rejection_reason else ''
        html = (
            f'<p>Dear {member.get("full_name", "Member")},</p>'
            f'<p>We regret to inform you that your loan application could not be '
            f'approved at this time.</p>'
            f'{reason_line}'
            f'<p>Please contact us if you have any questions.</p>'
        )
    send_email(recipient, 'Update on Your Loan Application', html, background=True)


def send_payment_confirmation_email(recipient: str, member: dict,
                                     transaction: dict,
                                     transaction_url: str = '') -> None:
    try:
        from flask import render_template
        html = render_template('emails/payment-confirmation.html',
                               member=member, transaction=transaction,
                               transaction_url=transaction_url)
    except Exception:
        amount = transaction.get('amount', 0)
        html = (
            f'<p>Dear {member.get("full_name", "Member")},</p>'
            f'<p>Your savings payment of <strong>&#8358;{amount:,.2f}</strong> '
            f'has been recorded successfully.</p>'
            f'<p>Log in to your portal to view your updated balance.</p>'
        )
    send_email(recipient, f'Payment Confirmation - {_coop_name()}', html,
               background=True)


def send_loan_repayment_email(recipient: str, member: dict, loan: dict,
                               repayment: dict, repayment_url: str = '') -> None:
    """Notify a member that a loan repayment was recorded."""
    full_name = member.get('full_name') or (
        f"{member.get('first_name', '')} {member.get('last_name', '')}".strip()
    ) or 'Member'
    amount = float(repayment.get('amount') or 0)
    balance = float(repayment.get('balance') or repayment.get('new_balance') or 0)
    principal = float(repayment.get('principal_paid') or 0)
    interest = float(repayment.get('interest_paid') or 0)
    date = repayment.get('date') or ''
    reference = repayment.get('repayment_number') or repayment.get('reference') or ''
    loan_number = loan.get('loan_number') or ''

    html = (
        f'<p>Dear {full_name},</p>'
        f'<p>A loan repayment has been recorded on your cooperative account.</p>'
        f'<table cellpadding="6" cellspacing="0" style="border-collapse:collapse">'
        f'<tr><td><strong>Loan number</strong></td><td>{loan_number}</td></tr>'
        f'<tr><td><strong>Repayment reference</strong></td><td>{reference}</td></tr>'
        f'<tr><td><strong>Date</strong></td><td>{date}</td></tr>'
        f'<tr><td><strong>Amount paid</strong></td><td>&#8358;{amount:,.2f}</td></tr>'
        f'<tr><td><strong>Principal portion</strong></td><td>&#8358;{principal:,.2f}</td></tr>'
        f'<tr><td><strong>Interest portion</strong></td><td>&#8358;{interest:,.2f}</td></tr>'
        f'<tr><td><strong>Outstanding balance</strong></td><td>&#8358;{balance:,.2f}</td></tr>'
        f'</table>'
    )
    if repayment_url:
        html += f'<p><a href="{repayment_url}">View your loan details</a></p>'
    html += '<p>Please contact the cooperative office if this entry does not match your records.</p>'

    text = (
        f'Dear {full_name},\n\n'
        f'Loan repayment recorded.\n'
        f'Loan: {loan_number}\nReference: {reference}\nDate: {date}\n'
        f'Amount paid: NGN {amount:,.2f}\nPrincipal: NGN {principal:,.2f}\n'
        f'Interest: NGN {interest:,.2f}\nOutstanding balance: NGN {balance:,.2f}\n'
    )
    send_email(recipient, f'Loan Repayment Recorded - {_coop_name()}', html, text,
               background=True)


def send_guarantor_request_email(recipient: str, guarantor: dict, applicant: dict,
                                 loan_number: str, amount: float) -> None:
    """Ask a member to stand as guarantor for a loan."""
    full = (f"{guarantor.get('first_name', '')} {guarantor.get('last_name', '')}".strip()
            or 'Member')
    app_name = f"{applicant['first_name']} {applicant['last_name']}"
    html = (
        f'<p>Dear {full},</p>'
        f'<p><strong>{app_name}</strong> has requested you to stand as guarantor for a '
        f'loan of <strong>&#8358;{float(amount):,.2f}</strong> (ref {loan_number}).</p>'
        f'<p>Please log in to your member portal to <strong>accept or decline</strong> this request.</p>'
    )
    send_email(recipient, f'Guarantor Request - {_coop_name()}', html,
               background=True)


def send_loan_stage_email(recipient: str, member: dict, loan_number: str,
                          stage_label: str) -> None:
    """Notify a member their loan advanced to a new approval stage."""
    full = (f"{member.get('first_name', '')} {member.get('last_name', '')}".strip()
            or 'Member')
    html = (
        f'<p>Dear {full},</p>'
        f'<p>Your loan application (ref {loan_number}) has progressed: '
        f'<strong>{stage_label}</strong>.</p>'
        f'<p>Log in to your member portal for details.</p>'
    )
    send_email(recipient, f'Loan Application Update - {_coop_name()}', html,
               background=True)


def send_password_reset_email(recipient: str, user: dict, reset_url: str) -> bool:
    try:
        from flask import render_template
        html = render_template('emails/password-reset.html',
                               user=user, reset_url=reset_url)
    except Exception:
        html = (
            f'<p>Dear {user.get("full_name", user.get("username", "User"))},</p>'
            f'<p>Click the link below to reset your password (valid for 1 hour):</p>'
            f'<p><a href="{reset_url}">{reset_url}</a></p>'
            f'<p>If you did not request this, you can ignore this email.</p>'
        )
    return send_email(recipient, f'Reset Your Password - {_coop_name()}', html)
