"""
CoopMS - Cooperative Management System
"""
import os
import re
import time
from datetime import datetime

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import LoginManager, current_user, logout_user

from database import init_db, get_db, close_db
from extensions import csrf
from crypto import encryption_enabled
from security import end_web_session, session_is_valid, log_audit, STAFF_ROLES, two_factor_enforced
from utils import User, member_for_user, new_submission_token

# ── App factory ──────────────────────────────────────────────────────────────

app = Flask(__name__)

_KNOWN_BAD_KEYS = {
    'change-this-in-production',
    'your-super-secret-key-change-this-in-production-2024',
    'secret',
    'dev',
    '',
}
_secret_key = os.environ.get('SECRET_KEY', '')
if not _secret_key or _secret_key in _KNOWN_BAD_KEYS:
    raise RuntimeError(
        "\n\n  *** STARTUP ABORTED ***\n"
        "  SECRET_KEY is not set or is using a known insecure default.\n"
        "  Generate a secure key and set it as an environment variable:\n\n"
        "      python -c \"import secrets; print(secrets.token_hex(32))\"\n\n"
        "  Then set:  SECRET_KEY=<generated-value>\n"
    )
app.config['SECRET_KEY'] = _secret_key
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10 MB

# ── Session / cookie hardening ────────────────────────────────────────────────
# config.py is not loaded via from_object, so these must be set on the live app.
# Secure cookies are enabled unless FLASK_DEBUG=1 (local http development).
_is_debug = os.environ.get('FLASK_DEBUG') == '1'
if not _is_debug and not encryption_enabled():
    raise RuntimeError(
        "\n\n  *** STARTUP ABORTED ***\n"
        "  FIELD_ENCRYPTION_KEY is required in production to encrypt sensitive PII.\n"
        "  Generate one with:\n\n"
        "      python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n\n"
        "  Then set: FIELD_ENCRYPTION_KEY=<generated-value>\n"
    )
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=not _is_debug,
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SAMESITE='Lax',
    REMEMBER_COOKIE_SECURE=not _is_debug,
)
app.config['IDLE_TIMEOUT_SECONDS'] = int(os.environ.get('IDLE_TIMEOUT_SECONDS', 15 * 60))
app.config['IDLE_WARNING_SECONDS'] = int(os.environ.get('IDLE_WARNING_SECONDS', 2 * 60))

# Behind Railway's HTTPS proxy, honor X-Forwarded-Proto/Host so that
# request.is_secure, Secure cookies, and url_for(_external=True) payment
# callbacks all resolve to https (not the internal http the app sees).
if not _is_debug:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config['PREFERRED_URL_SCHEME'] = 'https'

csrf.init_app(app)

# Close the request-scoped DB connection at the end of every request.
app.teardown_appcontext(close_db)

# ── Login manager ─────────────────────────────────────────────────────────────

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'auth.login'


@login_manager.user_loader
def load_user(user_id):
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if user and not session_is_valid(db, user, session):
        return None
    if user:
        keys = user.keys()
        return User(
            user['id'], user['username'], user['password_hash'], user['role'],
            user['email'] if 'email' in keys else '',
            user['must_change_password'] if 'must_change_password' in keys else 0,
            user['is_super_admin'] if 'is_super_admin' in keys else 0,
        )
    return None


# ── Database ──────────────────────────────────────────────────────────────────

init_db()

# ── Blueprints ────────────────────────────────────────────────────────────────

from blueprints.auth        import auth
from blueprints.main        import main
from blueprints.members     import members
from blueprints.savings     import savings
from blueprints.loans       import loans
from blueprints.investments import investments
from blueprints.reports     import reports
from blueprints.admin_panel import admin_panel
from blueprints.portal      import portal
from blueprints.cards       import cards
from blueprints.migration   import migration
from blueprints.payments_bp import payments_bp
from blueprints.help_bp     import help_bp
from blueprints.training    import training
from blueprints.accounting  import accounting
from blueprints.governance  import governance
from blueprints.communications import communications
from blueprints.security     import security_bp
from blueprints.feedback     import feedback_bp
from blueprints.marketing    import marketing
from blueprints.hq_billing   import hq_billing
from blueprints.affiliates  import affiliates_bp
from blueprints.ctas         import ctas as ctas_bp
from blueprints.virtual_accounts_bp import virtual_accounts_bp
from blueprints.member_receipts import member_receipts
from mobile_api             import mobile_api

app.register_blueprint(auth)
app.register_blueprint(main)
app.register_blueprint(members)
app.register_blueprint(savings)
app.register_blueprint(loans)
app.register_blueprint(investments)
app.register_blueprint(reports)
app.register_blueprint(admin_panel)
app.register_blueprint(portal)
app.register_blueprint(cards)
app.register_blueprint(migration)
app.register_blueprint(payments_bp)
app.register_blueprint(help_bp)
app.register_blueprint(training)
app.register_blueprint(accounting)
app.register_blueprint(governance)
app.register_blueprint(communications)
app.register_blueprint(security_bp)
app.register_blueprint(feedback_bp)
app.register_blueprint(marketing)
app.register_blueprint(hq_billing)
app.register_blueprint(affiliates_bp)
app.register_blueprint(ctas_bp)
app.register_blueprint(virtual_accounts_bp)
app.register_blueprint(member_receipts)
app.register_blueprint(mobile_api)

csrf.exempt(mobile_api)
csrf.exempt(app.view_functions['payments.paystack_webhook'])
csrf.exempt(app.view_functions['payments.flutterwave_webhook'])
# Scheduler-driven loan request sweep (authenticated with TASK_RUNNER_TOKEN).
csrf.exempt(app.view_functions['loans.loan_pipeline_sweep'])
# Scheduler-driven CTAS automatic contribution charges (same token guard).
csrf.exempt(app.view_functions['ctas.ctas_charge_due'])

# ── Context processor ─────────────────────────────────────────────────────────

@app.template_filter('fmtdt')
def _fmt_datetime(value, fmt='%d/%m/%Y %H:%M'):
    """Safely format a date/datetime for display in templates.

    Database date/datetime values reach templates as strings
    ('YYYY-MM-DD[ HH:MM:SS]') because _coerce normalises them (see database.py),
    so calling .strftime() on them raises AttributeError and 500s the page.
    This filter accepts a str, datetime, date, or None and never raises.
    """
    if not value:
        return ''
    if hasattr(value, 'strftime'):
        return value.strftime(fmt)
    from datetime import datetime as _dt
    text = str(value)
    for parse_fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return _dt.strptime(text[:19], parse_fmt).strftime(fmt)
        except ValueError:
            continue
    return text


@app.template_filter('logo_src')
def _logo_src(value):
    """Resolve a cooperative-logo setting to an <img> src.

    The logo is stored in the database as a data URI (so it survives container
    rebuilds). Legacy values are 'uploads/...' paths relative to static/ — those
    still resolve via url_for. Returns '' for a blank/None value.
    """
    if not value:
        return ''
    text = str(value)
    if text.startswith('data:') or text.startswith('http://') or text.startswith('https://'):
        return text
    return url_for('static', filename=text)


def _can_permission(permission):
    """Template helper: does the logged-in user hold this permission?"""
    try:
        from permissions import user_can
        return user_can(permission)
    except Exception:
        return False


def _va_flags():
    """Template helper: are member account numbers on, and is anything stuck?

    The badge counts transfers that need a human — money we could not tie to a
    member, and money the rule declined to apply.
    """
    try:
        from virtual_accounts import va_enabled
        db = get_db()
        if not va_enabled(db):
            return False, 0
        row = db.execute(
            "SELECT COUNT(*) AS n FROM virtual_account_receipts "
            "WHERE status IN ('unmatched', 'unallocated', 'part_allocated')").fetchone()
        return True, int(row['n'] or 0)
    except Exception:
        return False, 0


def _ctas_enabled_flag():
    """Template helper: is the optional CTAS module active for this cooperative?"""
    try:
        from blueprints.ctas import ctas_enabled
        return ctas_enabled()
    except Exception:
        return False


@app.context_processor
def utility_processor():
    db = get_db()
    va_on, va_attention = _va_flags()
    coop_name  = db.execute("SELECT value FROM settings WHERE key = 'coop_name'").fetchone()
    coop_logo  = db.execute("SELECT value FROM settings WHERE key = 'coop_logo'").fetchone()
    coop_short = db.execute("SELECT value FROM settings WHERE key = 'coop_short_name'").fetchone()

    unread_count = 0
    pending_savings_requests = 0
    linked_member_profile = False
    can_switch_to_member_view = False
    active_member_view = False
    show_feedback_nudge = False
    feedback_features = []
    feedback_improve_options = []
    if current_user.is_authenticated:
        role = getattr(current_user, 'role', '')
        try:
            from blueprints.feedback import feedback_due, FEATURE_OPTIONS, IMPROVE_OPTIONS
            show_feedback_nudge = feedback_due(db, current_user.id)
            feedback_features = FEATURE_OPTIONS
            feedback_improve_options = IMPROVE_OPTIONS
        except Exception:
            show_feedback_nudge = False
        try:
            linked_member_profile = bool(member_for_user(db))
        except Exception:
            linked_member_profile = False
        can_switch_to_member_view = role in ('admin', 'treasurer', 'secretary', 'exco') and linked_member_profile
        active_member_view = role == 'member' or (can_switch_to_member_view and session.get('view_mode') == 'member')
        try:
            row = db.execute(
                'SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0',
                (current_user.id,)
            ).fetchone()
            unread_count = row[0] if row else 0
        except Exception:
            pass
        if role in ('admin', 'secretary', 'treasurer'):
            try:
                row = db.execute(
                    "SELECT COUNT(*) FROM savings_change_requests WHERE status = 'pending'"
                ).fetchone()
                pending_savings_requests = row[0] if row else 0
            except Exception:
                pass

    _role = getattr(current_user, 'role', None) if current_user.is_authenticated else None
    return {
        'idle_timeout_seconds':     idle_timeout_for_role(_role),
        'idle_warning_seconds':     180 if _role == 'member' else int(app.config.get('IDLE_WARNING_SECONDS', 120)),
        'new_submission_token':     new_submission_token,
        'now':                      datetime.now,
        'coop_name':                coop_name['value']  if coop_name  else 'Your Cooperative',
        'coop_logo':                coop_logo['value']  if coop_logo  else '',
        'coop_short_name':          coop_short['value'] if coop_short else 'Coop',
        'unread_notifications_count': unread_count,
        'pending_savings_requests': pending_savings_requests,
        'linked_member_profile':    linked_member_profile,
        'can_switch_to_member_view': can_switch_to_member_view,
        'active_member_view':       active_member_view,
        'show_feedback_nudge':      show_feedback_nudge,
        'feedback_features':        feedback_features,
        'feedback_improve_options': feedback_improve_options,
        'marketing_hq_enabled':     os.environ.get('MARKETING_HQ', '0') == '1',
        'ctas_enabled':             _ctas_enabled_flag(),
        'va_enabled':               va_on,
        'va_attention':             va_attention,
        # Menus and buttons follow the officer's assigned permissions, not their
        # role — see permissions.py and Settings → Task Assignment.
        'can':                      _can_permission,
    }


# ── Before-request hook ───────────────────────────────────────────────────────

def _get_subscription_expiry():
    """
    Returns the subscription expiry date string (YYYY-MM-DD) or '' if not set.
    Checks the database settings first, then falls back to SUBSCRIPTION_EXPIRY env var.
    """
    try:
        from database import get_db
        db = get_db()
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'subscription_expiry'"
        ).fetchone()
        # Do NOT close here — the connection is request-scoped and shared;
        # teardown_appcontext(close_db) closes it at end of request.
        if row and row['value']:
            return row['value'].strip()
    except Exception:
        pass
    return os.environ.get('SUBSCRIPTION_EXPIRY', '').strip()


def _check_billing_status():
    """
    Returns True if subscription is active (or billing not configured).
    Reads expiry from DB settings first, then SUBSCRIPTION_EXPIRY env var.
    """
    expiry_str = _get_subscription_expiry()
    if not expiry_str:
        return True  # no billing configured — allow access
    try:
        from datetime import datetime as _dt
        expiry = _dt.strptime(expiry_str, '%Y-%m-%d')
        return _dt.now() < expiry
    except Exception:
        return True  # malformed date — fail open


# Endpoints accessible even when subscription is expired
_BILLING_EXEMPT = {
    'auth.login', 'auth.logout', 'auth.setup_password', 'auth.verify_2fa',
    'auth.forgot_password', 'auth.reset_password', 'static',
    'admin_panel.subscription_page',
    'admin_panel.subscription_callback',
    'help_bp.knowledge_base',
    'help_bp.article',
    'help_bp.panel_api',
    'marketing.capture_lead',
}

_IDLE_EXEMPT = {
    'auth.login',
    'auth.logout',
    'auth.setup_password',
    'auth.forgot_password',
    'auth.reset_password',
    'auth.verify_2fa',
    'static',
}

# When 2FA enforcement is on, a staff member who has not set up 2FA yet is
# redirected to the setup page. These endpoints must stay reachable so they can
# actually complete setup (or log out).
_2FA_SETUP_EXEMPT = {
    'auth.login', 'auth.logout', 'auth.verify_2fa', 'static',
    'security.index', 'security.setup_2fa', 'security.show_backup_codes',
    'portal.change_password',
    'help_bp.knowledge_base', 'help_bp.article', 'help_bp.panel_api',
}


# When the provider suspends a tenant from HQ, only these stay reachable so the
# notice shows, users can sign out, and HQ can reach the token-guarded control
# endpoints (crucially, to reactivate).
_SUSPEND_EXEMPT = {'auth.login', 'auth.logout', 'auth.verify_2fa', 'static',
                   'mobile_api.hq_set_status', 'mobile_api.hq_member_count'}


def _is_tenant_suspended():
    """True if HQ has suspended this tenant (settings.tenant_suspended = '1')."""
    try:
        from database import get_db
        row = get_db().execute(
            "SELECT value FROM settings WHERE key = 'tenant_suspended'").fetchone()
        return bool(row and str(row['value']) == '1')
    except Exception:
        return False


@app.before_request
def check_maintenance():
    # Provider suspension locks the whole tenant — admins included — until it is
    # reactivated from HQ. Checked before the admin bypass on purpose.
    if request.endpoint not in _SUSPEND_EXEMPT and _is_tenant_suspended():
        return render_template('errors/tenant_suspended.html'), 403
    if current_user.is_authenticated and current_user.role == 'admin':
        return
    maintenance = False
    if maintenance and request.endpoint not in ['auth.login', 'static']:
        return render_template('errors/maintenance.html'), 503

    # Billing subscription check — admin always gets through
    if request.endpoint not in _BILLING_EXEMPT:
        if not _check_billing_status():
            return render_template(
                'errors/subscription_expired.html',
                expiry=_get_subscription_expiry()
            ), 402


def idle_timeout_for_role(role):
    """Staff handle money, so they time out sooner; members get longer (and a longer
    on-screen warning) because many are older and read and type more slowly."""
    if role == 'member':
        return int(os.environ.get('MEMBER_IDLE_TIMEOUT_SECONDS', 30 * 60))
    return int(app.config.get('IDLE_TIMEOUT_SECONDS', 15 * 60))


@app.before_request
def enforce_idle_timeout():
    if not current_user.is_authenticated:
        session.pop('last_activity_at', None)
        return
    if request.endpoint in _IDLE_EXEMPT:
        return

    now_ts = time.time()
    timeout = idle_timeout_for_role(getattr(current_user, 'role', None))
    last_activity = session.get('last_activity_at')

    try:
        last_activity = float(last_activity) if last_activity is not None else None
    except (TypeError, ValueError):
        last_activity = None

    if last_activity is not None and now_ts - last_activity > timeout:
        db = get_db()
        user_id = getattr(current_user, 'id', None)
        username = getattr(current_user, 'username', '')
        log_audit(
            db, user_id, username, 'SESSION_TIMEOUT', 'auth',
            f'User logged out after {timeout // 60} minutes of inactivity',
            request.remote_addr or '',
            request.headers.get('User-Agent', ''),
        )
        db.commit()
        session.pop('view_mode', None)
        session.pop('last_activity_at', None)
        end_web_session(db, session.get('sid'))
        db.commit()
        logout_user()
        flash(f'You were logged out after {timeout // 60} minutes of inactivity.', 'warning')
        if request.path.startswith('/api/') or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'error': 'session_timeout'}), 401
        return redirect(url_for('auth.login'))

    session['last_activity_at'] = now_ts


# ── Forced password-change gate ───────────────────────────────────────────────

_ALLOWED_WHILE_FORCED = {
    'portal.change_password',
    'auth.logout',
    'static',
    # payment callbacks must be reachable so gateway redirects don't loop
    'payments.payment_callback',
    'payments.paystack_webhook',
    'payments.flutterwave_webhook',
}

@app.before_request
def enforce_password_change():
    """Redirect any user with must_change_password=True to the change-password
    page until they set a new password.  Static assets and the change/logout
    endpoints are always permitted so the page can actually render."""
    if not current_user.is_authenticated:
        return
    if not getattr(current_user, 'must_change_password', False):
        return
    if request.endpoint in _ALLOWED_WHILE_FORCED:
        return
    from flask import redirect, url_for, flash
    flash('You must set a new password before continuing.', 'warning')
    return redirect(url_for('portal.change_password'))


# ── Two-factor enforcement gate ───────────────────────────────────────────────

@app.before_request
def enforce_2fa_setup():
    """When the cooperative requires 2FA, a staff member who hasn't set it up
    yet is redirected to the setup page until they do."""
    if not current_user.is_authenticated:
        return
    if request.endpoint in _2FA_SETUP_EXEMPT:
        return
    if getattr(current_user, 'role', None) not in STAFF_ROLES:
        return
    db = get_db()
    if not two_factor_enforced(db):
        return
    row = db.execute(
        'SELECT two_factor_enabled FROM users WHERE id = ?', (current_user.id,)
    ).fetchone()
    if row and row['two_factor_enabled']:
        return
    flash('Your cooperative now requires two-factor authentication. '
          'Please set it up to continue.', 'warning')
    return redirect(url_for('security.setup_2fa'))


# ── Error handlers ────────────────────────────────────────────────────────────

@app.route('/session/ping')
def session_ping():
    if not current_user.is_authenticated:
        return jsonify({'ok': False, 'error': 'unauthenticated'}), 401
    session['last_activity_at'] = time.time()
    return jsonify({'ok': True})


@app.after_request
def persist_audit_rows(response):
    """Commit audit rows written after a handler's own commit (see security.log_audit).
    Skipped on server errors, where the whole request is being abandoned."""
    from flask import g
    if getattr(g, '_audit_dirty', False) and response.status_code < 500:
        try:
            get_db().commit()
        except Exception as exc:  # never turn a finished request into an error
            print(f"[audit] failed to commit audit rows: {exc}")
    return response


@app.after_request
def apply_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    if not _is_debug:
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    if current_user.is_authenticated and request.endpoint != 'static':
        response.headers.setdefault('Cache-Control', 'no-store, no-cache, must-revalidate, private')
        response.headers.setdefault('Pragma', 'no-cache')
        response.headers.setdefault('Expires', '0')
    return response


@app.errorhandler(404)
def not_found_error(error):
    return render_template('errors/404.html'), 404


@app.errorhandler(403)
def forbidden_error(error):
    return render_template('errors/403.html'), 403


@app.errorhandler(500)
def internal_error(error):
    db = get_db()
    db.rollback()
    return render_template('errors/500.html'), 500


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(
        debug=os.environ.get('FLASK_DEBUG') == '1',
        host=os.environ.get('FLASK_HOST', '127.0.0.1'),
        port=int(os.environ.get('PORT', 5000)),
    )
