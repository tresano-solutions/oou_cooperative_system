import hmac
import os
from collections import defaultdict
from datetime import datetime, timedelta
import time

from flask import (Blueprint, current_app, flash, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_login import login_user, login_required, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from database import get_db
from email_service import send_password_reset_email
from security import (start_web_session, end_web_session, generate_account_setup_token, hash_account_setup_token, log_audit,
                      validate_password_strength, user_2fa_secret, verify_2fa_code)
from utils import (User, ACCOUNT_PAUSE, sign_in_pause_seconds, sign_in_tries_left, record_sign_in_failure,
                   clear_account_failures, pause_message, record_failed_login)

auth = Blueprint('auth', __name__)

_password_reset_attempts = defaultdict(list)
_PASSWORD_RESET_WINDOW = 900
_PASSWORD_RESET_MAX = 5


def _support_routes_enabled():
    return os.environ.get('ENABLE_SUPPORT_ROUTES') == '1'


def _reset_token_is_valid():
    expected_token = os.environ.get('RESET_TOKEN', '')
    provided_token = request.form.get('token') or request.headers.get('X-Reset-Token', '')
    return bool(expected_token and provided_token and hmac.compare_digest(provided_token, expected_token))


def _parse_db_datetime(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).split('.')[0].replace('T', ' '))


def _reset_request_limited(key):
    now = time.time()
    attempts = [t for t in _password_reset_attempts[key] if now - t < _PASSWORD_RESET_WINDOW]
    _password_reset_attempts[key] = attempts
    if len(attempts) >= _PASSWORD_RESET_MAX:
        return True
    attempts.append(now)
    return False


def _account_setup_token_row(db, token):
    token_hash = hash_account_setup_token(token)
    row = db.execute('''
        SELECT
            t.id AS token_id,
            t.user_id,
            t.expires_at,
            t.used_at,
            u.username,
            u.email,
            u.full_name,
            u.is_active
        FROM account_setup_tokens t
        JOIN users u ON u.id = t.user_id
        WHERE t.token_hash = ?
          AND COALESCE(t.purpose, 'member_onboarding') IN ('member_onboarding', 'admin_reissue')
    ''', (token_hash,)).fetchone()
    if not row or row['used_at'] or not row['is_active']:
        return None
    try:
        if _parse_db_datetime(row['expires_at']) <= datetime.now():
            return None
    except Exception:
        return None
    return row


def _password_reset_token_row(db, token):
    token_hash = hash_account_setup_token(token)
    row = db.execute('''
        SELECT
            t.id AS token_id,
            t.user_id,
            t.expires_at,
            t.used_at,
            u.username,
            u.email,
            u.full_name,
            u.is_active
        FROM account_setup_tokens t
        JOIN users u ON u.id = t.user_id
        WHERE t.token_hash = ?
          AND t.purpose = 'password_reset'
    ''', (token_hash,)).fetchone()
    if not row or row['used_at'] or not row['is_active']:
        return None
    try:
        if _parse_db_datetime(row['expires_at']) <= datetime.now():
            return None
    except Exception:
        return None
    return row


def _issue_password_reset_link(db, user):
    token, token_hash = generate_account_setup_token()
    db.execute('''
        UPDATE account_setup_tokens
        SET used_at = ?
        WHERE user_id = ?
          AND purpose = 'password_reset'
          AND used_at IS NULL
    ''', (datetime.now(), user['id']))
    db.execute('''
        INSERT INTO account_setup_tokens
            (user_id, token_hash, purpose, expires_at)
        VALUES (?, ?, 'password_reset', ?)
    ''', (user['id'], token_hash, datetime.now() + timedelta(hours=1)))
    return url_for('auth.reset_password', token=token, _external=True)


@auth.route('/')
def index():
    return render_template('index.html')


@auth.route('/manifest.webmanifest')
def web_manifest():
    """Lets a member add the portal to their phone's home screen as an app.

    This is how iPhone members get an app: Apple does not allow installing from
    a website, so the portal has to be the app for them. Generated per request
    rather than served as a static file so each society's home-screen icon
    carries its own name.
    """
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key = 'coop_name'").fetchone()
    name = (row['value'] if row else '') or 'CoopMS'
    short = db.execute(
        "SELECT value FROM settings WHERE key = 'coop_short_name'").fetchone()
    short_name = (short['value'] if short else '') or name

    icon = lambda f, size, purpose: {                      # noqa: E731
        'src': url_for('static', filename=f'icons/{f}'),
        'sizes': size, 'type': 'image/png', 'purpose': purpose,
    }
    return jsonify({
        'name': f'{name} — Member Portal',
        'short_name': short_name[:12],
        'description': 'Your savings, loans and contributions.',
        'start_url': '/dashboard',
        'scope': '/',
        'display': 'standalone',
        'orientation': 'portrait',
        'background_color': '#f4f6f9',
        'theme_color': '#1a3a6c',
        'icons': [
            icon('icon-192.png', '192x192', 'any'),
            icon('icon-512.png', '512x512', 'any'),
            icon('icon-maskable-512.png', '512x512', 'maskable'),
        ],
    })


@auth.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = request.remote_addr or '0.0.0.0'
        username = request.form['username']
        password = request.form['password']
        ua       = request.user_agent.string if request.user_agent else ''

        # A short, friendly pause after many wrong passwords on THIS account (or a
        # flood from one network) - never a long lock-out. See utils.sign_in_pause_seconds.
        wait = sign_in_pause_seconds(ip, username)
        if wait:
            flash(pause_message(wait), 'lockout')
            return render_template('login.html')

        db   = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()

        if user and user['is_active'] == 0:
            record_sign_in_failure(ip, username)
            log_audit(db, user['id'], user['username'], 'FAILED_LOGIN', 'auth',
                      'Inactive user login attempt', ip, ua)
            db.commit()
            flash('Incorrect username or password. Please try again.', 'danger')
        elif user and check_password_hash(user['password_hash'], password):
            clear_account_failures(username)
            keys = user.keys()
            has_2fa = ('two_factor_enabled' in keys and user['two_factor_enabled']
                       and user_2fa_secret(db, user['id']))
            if has_2fa:
                # Password is correct but 2FA is on - defer the actual login
                # until the second factor is verified.
                session['pending_2fa_user'] = user['id']
                session['pending_2fa_at'] = time.time()
                log_audit(db, user['id'], user['username'], 'LOGIN_2FA_CHALLENGE',
                          'auth', '2FA challenge issued', ip, ua)
                db.commit()
                return redirect(url_for('auth.verify_2fa'))
            return _finalize_login(db, user, ip, ua)
        else:
            record_sign_in_failure(ip, username)
            log_audit(db, None, username, 'FAILED_LOGIN', 'auth',
                      f'Failed login attempt for username: {username}', ip, ua)
            db.commit()
            left = sign_in_tries_left(username)
            if left == 0:
                flash(pause_message(ACCOUNT_PAUSE), 'lockout')
            elif left <= 3:
                flash(f'Incorrect username or password. You have {left} '
                      f'{"try" if left == 1 else "tries"} left before a short pause. '
                      'Forgotten it? Tap "Forgot password?" below.', 'danger')
            else:
                flash('Incorrect username or password. Please try again.', 'danger')

    return render_template('login.html')


def _finalize_login(db, user, ip, ua):
    """Complete a login once all factors are satisfied. Shared by the direct
    (no-2FA) path and the 2FA verification path."""
    keys = user.keys()
    user_obj = User(
        user['id'], user['username'], user['password_hash'], user['role'],
        user['email'] if 'email' in keys else '',
        user['must_change_password'] if 'must_change_password' in keys else 0,
    )
    login_user(user_obj)
    start_web_session(db, user['id'], user['session_version'] if 'session_version' in keys else 0)
    session.pop('view_mode', None)
    session.pop('pending_2fa_user', None)
    session.pop('pending_2fa_at', None)
    db.execute('UPDATE users SET last_login = ? WHERE id = ?', (datetime.now(), user['id']))
    log_audit(db, user['id'], user['username'], 'LOGIN', 'auth', 'User logged in', ip, ua)
    db.commit()
    if user_obj.must_change_password:
        flash('Welcome! You must set a new password before you can continue.', 'warning')
        return redirect(url_for('portal.change_password'))
    flash('Login successful!', 'success')
    return redirect(url_for('main.dashboard'))


# How long a passed-password 2FA challenge stays valid before the user must
# re-enter their password (seconds).
_2FA_CHALLENGE_TTL = 300


@auth.route('/login/verify', methods=['GET', 'POST'])
def verify_2fa():
    user_id = session.get('pending_2fa_user')
    if not user_id:
        return redirect(url_for('auth.login'))

    # Expire the challenge so a stale pending session can't linger.
    if time.time() - session.get('pending_2fa_at', 0) > _2FA_CHALLENGE_TTL:
        session.pop('pending_2fa_user', None)
        session.pop('pending_2fa_at', None)
        flash('Your verification window expired. Please sign in again.', 'warning')
        return redirect(url_for('auth.login'))

    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        session.pop('pending_2fa_user', None)
        session.pop('pending_2fa_at', None)
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        ip = request.remote_addr or '0.0.0.0'
        ua = request.user_agent.string if request.user_agent else ''

        wait = sign_in_pause_seconds(ip, user['username'])
        if wait:
            flash(pause_message(wait), 'lockout')
            return render_template('auth/verify-2fa.html', username=user['username'])

        code = request.form.get('code', '')
        if verify_2fa_code(db, user_id, code):
            clear_account_failures(user['username'])
            return _finalize_login(db, user, ip, ua)

        record_sign_in_failure(ip, user['username'])
        log_audit(db, user_id, user['username'], 'FAILED_2FA', 'auth',
                  'Invalid 2FA code at login', ip, ua)
        db.commit()
        flash('That code was not valid. Enter the current 6-digit code from your '
              'authenticator app, or one of your backup codes.', 'danger')

    return render_template('auth/verify-2fa.html', username=user['username'])


@auth.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        ip = request.remote_addr or '0.0.0.0'
        ua = request.user_agent.string if request.user_agent else ''
        generic_message = (
            'If an active account exists for those details, a password reset link '
            'will be sent to the registered email address.'
        )

        if not identifier:
            flash('Enter your username or email address.', 'danger')
            return redirect(url_for('auth.forgot_password'))

        reset_key = f'{ip}:{identifier.lower()}'
        if _reset_request_limited(reset_key):
            flash('Too many reset requests. Please wait a few minutes before trying again.', 'warning')
            return redirect(url_for('auth.forgot_password'))

        db = get_db()
        user = db.execute('''
            SELECT id, username, email, full_name, is_active
            FROM users
            WHERE lower(username) = lower(?)
               OR lower(COALESCE(email, '')) = lower(?)
            LIMIT 1
        ''', (identifier, identifier)).fetchone()

        if user and user['is_active'] and (user['email'] or '').strip():
            reset_url = _issue_password_reset_link(db, user)
            sent = send_password_reset_email(user['email'], dict(user), reset_url)
            log_audit(
                db,
                user['id'],
                user['username'],
                'PASSWORD_RESET_REQUEST',
                'auth',
                'Password reset link requested' + ('' if sent else ' but email delivery failed'),
                ip,
                ua,
            )
        else:
            log_audit(
                db,
                None,
                identifier,
                'PASSWORD_RESET_REQUEST',
                'auth',
                'Password reset requested for unknown, inactive, or email-less account',
                ip,
                ua,
            )
        db.commit()
        flash(generic_message, 'success')
        return redirect(url_for('auth.login'))

    return render_template('forgot-password.html')


@auth.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    db = get_db()
    reset_row = _password_reset_token_row(db, token)
    if not reset_row:
        flash('This password reset link is invalid, expired, or has already been used.', 'danger')
        return redirect(url_for('auth.forgot_password'))

    if request.method == 'POST':
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not new_password or not confirm_password:
            flash('Please enter and confirm your new password.', 'danger')
            return redirect(url_for('auth.reset_password', token=token))
        if new_password != confirm_password:
            flash('New passwords do not match.', 'danger')
            return redirect(url_for('auth.reset_password', token=token))

        ok, errors = validate_password_strength(new_password, db)
        if not ok:
            flash(' '.join(errors), 'danger')
            return redirect(url_for('auth.reset_password', token=token))

        try:
            db.execute(
                'UPDATE users SET session_version = COALESCE(session_version, 0) + 1, password_hash = ?, must_change_password = 0 WHERE id = ?',
                (generate_password_hash(new_password), reset_row['user_id'])
            )
            db.execute(
                'UPDATE account_setup_tokens SET used_at = ? WHERE id = ?',
                (datetime.now(), reset_row['token_id'])
            )
            log_audit(
                db,
                reset_row['user_id'],
                reset_row['username'],
                'PASSWORD_RESET_COMPLETE',
                'auth',
                'User reset password using emailed reset link',
                request.remote_addr or '',
                request.user_agent.string if request.user_agent else '',
            )
            db.commit()
            flash('Password reset successfully. Please sign in with your new password.', 'success')
            return redirect(url_for('auth.login'))
        except Exception:
            db.rollback()
            flash('Unable to reset password. Please request a new reset link.', 'danger')
            return redirect(url_for('auth.forgot_password'))

    return render_template('reset-password.html', token=token, user=reset_row)


@auth.route('/setup-password/<token>', methods=['GET', 'POST'])
def setup_password(token):
    db = get_db()
    setup_row = _account_setup_token_row(db, token)
    if not setup_row:
        flash('This setup link is invalid, expired, or has already been used.', 'danger')
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not new_password or not confirm_password:
            flash('Please enter and confirm your new password.', 'danger')
            return redirect(url_for('auth.setup_password', token=token))
        if new_password != confirm_password:
            flash('New passwords do not match.', 'danger')
            return redirect(url_for('auth.setup_password', token=token))

        ok, errors = validate_password_strength(new_password, db)
        if not ok:
            flash(' '.join(errors), 'danger')
            return redirect(url_for('auth.setup_password', token=token))

        try:
            db.execute(
                'UPDATE users SET session_version = COALESCE(session_version, 0) + 1, password_hash = ?, must_change_password = 0 WHERE id = ?',
                (generate_password_hash(new_password), setup_row['user_id'])
            )
            db.execute(
                'UPDATE account_setup_tokens SET used_at = ? WHERE id = ?',
                (datetime.now(), setup_row['token_id'])
            )
            log_audit(
                db,
                setup_row['user_id'],
                setup_row['username'],
                'ACCOUNT_SETUP',
                'auth',
                'User completed account setup',
                request.remote_addr or '',
                request.user_agent.string if request.user_agent else '',
            )
            db.commit()
            flash('Password set successfully. Please sign in with your new password.', 'success')
            return redirect(url_for('auth.login'))
        except Exception:
            db.rollback()
            flash('Unable to complete account setup. Please request a new setup link.', 'danger')
            return redirect(url_for('auth.login'))

    return render_template('setup-password.html', token=token, user=setup_row)


@auth.route('/logout')
@login_required
def logout():
    from flask_login import current_user
    db = get_db()
    from utils import audit
    if request.args.get('reason') == 'timeout':
        audit(db, 'SESSION_TIMEOUT', 'auth', 'User logged out after inactivity timeout')
    else:
        audit(db, 'LOGOUT', 'auth', 'User logged out')
    db.commit()
    session.pop('view_mode', None)
    session.pop('last_activity_at', None)
    end_web_session(db, session.get('sid'))
    db.commit()
    logout_user()
    if request.args.get('reason') == 'timeout':
        timeout_minutes = int(current_app.config.get('IDLE_TIMEOUT_SECONDS', 15 * 60)) // 60
        flash(f'You were logged out after {timeout_minutes} minutes of inactivity.', 'warning')
    else:
        flash('You have been logged out', 'info')
    return redirect(url_for('auth.index'))


@auth.route('/setup')
def setup():
    return '<h2>Not available</h2>', 404


@auth.route('/debug-auth')
def debug_auth():
    return '<h2>Not available</h2>', 404


@auth.route('/emergency-reset', methods=['GET', 'POST'])
def emergency_reset():
    """
    Emergency admin password reset.
    Enable with ENABLE_SUPPORT_ROUTES=1, then send RESET_TOKEN by POST body or
    X-Reset-Token header. After resetting, delete the support variables.
    """
    if not _support_routes_enabled():
        return '<h2>Not available</h2>', 404

    if request.method != 'POST':
        return '<h2>Reset requires POST.</h2>', 405

    if not _reset_token_is_valid():
        return '<h2>Invalid token.</h2>', 403

    new_password = os.environ.get('ADMIN_PASSWORD', '')
    if not new_password:
        return '<h2>Reset not available.</h2>', 400

    try:
        db = get_db()
        db.execute(
            'UPDATE users SET session_version = COALESCE(session_version, 0) + 1, password_hash = ? WHERE username = ?',
            (generate_password_hash(new_password), 'admin')
        )
        db.commit()
        rows = db.execute('SELECT id, username, role FROM users WHERE username = ?', ('admin',)).fetchone()
        if rows:
            return '''
            <h2 style="color:green">&#10003; Admin password reset successfully.</h2>
            <p>Username: <strong>admin</strong></p>
            <p><a href="/login">Go to Login</a></p>
            <hr>
            <p style="color:red"><strong>Security:</strong> Remove support reset variables now.</p>
            ''', 200
        else:
            return '<h2>Admin user not found.</h2><p>No user with username "admin" exists in the database.</p>', 404
    except Exception:
        return '<h2>Reset failed.</h2>', 500
