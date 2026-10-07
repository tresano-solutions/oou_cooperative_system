"""
REST API for CoopMS mobile app integration.

Existing routes under /api/mobile remain available for backward compatibility.
New member-facing app routes use /api/mobile/v1.
"""

import hmac
import json
import os
import loan_limits
import random
import re
from datetime import datetime, timedelta, UTC
from functools import wraps

import jwt
from flask import Blueprint, current_app, g, jsonify, make_response, request
from werkzeug.security import check_password_hash, generate_password_hash

import loan_workflow as lw
import loan_alerts as la
from loan_pdf import build_loan_application_pdf
from crypto import encrypt_member_sensitive_fields, mask_member_sensitive_fields
from database import get_db, last_insert_id
from email_service import send_guarantor_request_email, send_password_reset_email
from security import generate_account_setup_token, validate_password_strength
from utils import (
    finite_float,
    audit,
    clear_login_attempts,
    compute_loan_schedule,
    is_rate_limited,
    lockout_seconds_remaining,
    member_for_user,
    member_has_minimum_membership,
    has_unpaid_loan_of_type,
    member_savings_balance,
    member_share_capital,
    notify,
    notify_member,
    record_failed_login,
)

mobile_api = Blueprint('mobile_api', __name__)

JWT_ISSUER = 'coopms'
JWT_AUDIENCE = 'coopms-mobile'
MOBILE_TOKEN_TTL_HOURS = 24
TENANT_CODE_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,30}$')

PROFILE_FIELDS = (
    'first_name', 'last_name', 'phone', 'address', 'city', 'state', 'country',
    'occupation', 'date_of_birth', 'emergency_contact_name',
    'emergency_contact_phone', 'next_of_kin', 'bank_name', 'account_name',
    'account_number', 'bvn', 'nin',
)
PROFILE_COMPLETION_FIELDS = (
    'first_name', 'last_name', 'email', 'phone', 'date_of_birth', 'address',
    'city', 'state', 'country', 'occupation', 'bank_name', 'account_name',
    'account_number', 'emergency_contact_name', 'emergency_contact_phone',
)


def _interest_rates(db):
    rows = db.execute("SELECT key, value FROM settings WHERE key LIKE 'interest_%'").fetchall()
    raw = {row['key']: row['value'] for row in rows}
    return {
        'Regular': float(raw.get('interest_regular', 11)),
        'Housing': float(raw.get('interest_housing', 9)),
        'Emergency': float(raw.get('interest_emergency', 10)),
        'Asset Purchase': float(raw.get('interest_asset', 10)),
        'School Fees': float(raw.get('interest_school_fees', 9)),
    }


def _interest_methods(db):
    rows = db.execute("SELECT key, value FROM settings WHERE key LIKE 'interest_method_%'").fetchall()
    raw = {row['key']: row['value'] for row in rows}
    return {
        'Regular': raw.get('interest_method_regular', 'reducing_annual'),
        'Housing': raw.get('interest_method_housing', 'reducing_annual'),
        'Emergency': raw.get('interest_method_emergency', 'reducing_annual'),
        'Asset Purchase': raw.get('interest_method_asset', 'reducing_annual'),
        'School Fees': raw.get('interest_method_school_fees', 'flat'),
    }


def _loan_purpose_options(db):
    rates = _interest_rates(db)
    methods = _interest_methods(db)
    return [
        {
            'value': purpose,
            'label': purpose,
            'interest_rate': float(rates[purpose]),
            'interest_method': methods.get(purpose, 'reducing_annual'),
        }
        for purpose in rates.keys()
    ]


def _collateral_options(is_staff_member):
    options = [
        {
            'value': 'standing_order',
            'label': 'Standing order / salary deduction',
            'description': 'Repayment is deducted automatically through payroll or a standing instruction.',
        },
    ]
    if not is_staff_member:
        options.append({
            'value': 'post_dated_cheques',
            'label': 'Post-dated cheques',
            'description': 'Member provides post-dated cheques as repayment collateral.',
        })
    return options


def _eligible_guarantors(db, applicant_id):
    rows = db.execute('''
        SELECT id, member_number, first_name, last_name, email, phone, total_savings
        FROM members
        WHERE status = 'active'
          AND id <> ?
        ORDER BY first_name, last_name, member_number
        LIMIT 250
    ''', (applicant_id,)).fetchall()
    return [
        {
            'id': row['id'],
            'member_number': row['member_number'],
            'full_name': f"{row['first_name']} {row['last_name']}",
            'email': row['email'] or '',
            'phone': row['phone'] or '',
            'total_savings': float(row['total_savings'] or 0),
        }
        for row in rows
    ]


def _mobile_member_is_staff(member):
    return bool((member['employee_id'] or '').strip()) if 'employee_id' in member.keys() else False


def _max_tenure(db):
    row = db.execute("SELECT value FROM settings WHERE key = 'max_tenure_months'").fetchone()
    try:
        return int(row['value']) if row and row['value'] else 18
    except (TypeError, ValueError):
        return 18


def _generate_token(user_id, username, role, session_version=0):
    now = datetime.now(UTC)
    payload = {
        'sv': int(session_version or 0),
        'user_id': user_id,
        'username': username,
        'role': role,
        'iat': now,
        'nbf': now,
        'exp': now + timedelta(hours=MOBILE_TOKEN_TTL_HOURS),
        'iss': JWT_ISSUER,
        'aud': JWT_AUDIENCE,
    }
    return jwt.encode(payload, current_app.config['SECRET_KEY'], algorithm='HS256')


def _mobile_login_key():
    return f"mobile:{request.remote_addr or '0.0.0.0'}"


def _json_error(message, status=400, code='error'):
    return jsonify({'success': False, 'error': message, 'code': code}), status


def _to_json_value(value):
    if isinstance(value, datetime):
        return value.isoformat(timespec='seconds')
    if hasattr(value, 'isoformat'):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return value


def _row_to_dict(row):
    return {k: _to_json_value(row[k]) for k in row.keys()} if row else None


def jwt_required(f):
    """Validate Bearer JWT and store payload in flask.g."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return jsonify({'success': False, 'error': 'Missing Authorization header'}), 401
        token = auth[7:]
        try:
            payload = jwt.decode(
                token,
                current_app.config['SECRET_KEY'],
                algorithms=['HS256'],
                issuer=JWT_ISSUER,
                audience=JWT_AUDIENCE,
            )
            g.user_id = payload['user_id']
            g.username = payload['username']
            g.role = payload['role']
            # Revocation: the account must still be active and the token must carry
            # the user's current session_version (bumped on password change, role
            # change, deactivation, 2FA reset).
            row = get_db().execute('SELECT is_active, session_version FROM users WHERE id = ?',
                                   (g.user_id,)).fetchone()
            if (not row or row['is_active'] == 0
                    or int(row['session_version'] or 0) != int(payload.get('sv', 0) or 0)):
                return _json_error('Session is no longer valid. Please sign in again.', 401, 'token_revoked')
        except jwt.ExpiredSignatureError:
            return _json_error('Token has expired', 401, 'token_expired')
        except jwt.InvalidTokenError:
            return _json_error('Invalid token', 401, 'invalid_token')
        return f(*args, **kwargs)
    return decorated


def _issue_mobile_password_reset_link(db, user):
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
    from flask import url_for
    return url_for('auth.reset_password', token=token, _external=True)


def _env_tenant_records():
    raw = os.environ.get('COOPMS_TENANTS_JSON', '').strip()
    if raw:
        try:
            records = json.loads(raw)
            if isinstance(records, list):
                return records
        except Exception:
            pass
    suffix = os.environ.get('COOPMS_MOBILE_DOMAIN_SUFFIX', 'cooperativems.com').strip().strip('.')
    defaults = []
    for code, name in (
        ('hq', 'CoopMS HQ'),
        ('ooucoop', 'OOU Coop'),
        ('smtcoop', 'SMT Coop'),
    ):
        defaults.append({
            'code': code,
            'name': name,
            'base_url': f'https://{code}.{suffix}',
            'logo_url': '',
            'is_active': 1,
        })
    return defaults


def _tenant_record_from_mapping(record, code):
    rec_code = str(record.get('code') or '').strip().lower()
    if rec_code != code:
        return None
    active = record.get('is_active', record.get('active', True))
    if str(active).lower() in ('0', 'false', 'no', 'inactive'):
        return None
    base_url = str(record.get('base_url') or record.get('url') or '').strip().rstrip('/')
    if not base_url:
        domain = str(record.get('domain') or '').strip().strip('/')
        base_url = f'https://{domain}' if domain else ''
    if not base_url:
        return None
    return {
        'code': rec_code,
        'coop_name': str(record.get('name') or record.get('coop_name') or rec_code),
        'base_url': base_url,
        'logo': str(record.get('logo_url') or record.get('logo') or ''),
    }


def member_required(f):
    @wraps(f)
    @jwt_required
    def decorated(*args, **kwargs):
        db = get_db()
        member = member_for_user(db, g.user_id)
        if not member:
            return jsonify({
                'success': False,
                'error': 'No member profile is linked to this account. Contact admin.',
            }), 404
        g.db = db
        g.member = member
        return f(*args, **kwargs)
    return decorated


def _profile_completion(member):
    missing = []
    completed = 0
    for field in PROFILE_COMPLETION_FIELDS:
        if (member[field] if field in member.keys() else None):
            completed += 1
        else:
            missing.append(field)
    percent = round((completed / len(PROFILE_COMPLETION_FIELDS)) * 100)
    return {
        'percent': percent,
        'missing_fields': missing,
        'certified_member': percent == 100,
    }


def _member_summary(member, db):
    savings_balance = member_savings_balance(db, member['id'])
    share_capital = member_share_capital(db, member['id'])
    data = _row_to_dict(member)
    data = mask_member_sensitive_fields(data)
    return {
        'id': member['id'],
        'member_number': member['member_number'],
        'full_name': f"{member['first_name']} {member['last_name']}",
        'first_name': member['first_name'],
        'last_name': member['last_name'],
        'email': member['email'] or '',
        'phone': member['phone'] or '',
        'status': member['status'],
        'date_joined': _to_json_value(member['date_joined']),
        'total_savings': float(savings_balance or 0),
        'share_capital': float(share_capital or 0),
        'loan_eligibility_amount': loan_limits.eligible_amount(db, savings_balance),
        'loan_type_limits': loan_limits.member_limits(db, savings_balance),
        'profile_completion': _profile_completion(member),
        'bank_name_masked': data.get('bank_name_masked', ''),
        'account_name_masked': data.get('account_name_masked', ''),
        'account_number_masked': data.get('account_number_masked', ''),
        'bvn_masked': data.get('bvn_masked', ''),
        'nin_masked': data.get('nin_masked', ''),
    }


def _loan_payload(loan, include_schedule=False):
    data = _row_to_dict(loan)
    is_disbursed = loan['status'] in ('active', 'completed') or bool(loan['disbursement_date'])
    data['is_disbursed'] = is_disbursed
    data['balance'] = float(loan['balance'] or 0) if is_disbursed else 0.0
    data['amount'] = float(loan['amount'] or 0)
    data['total_repayment'] = float(loan['total_repayment'] or 0)
    data['monthly_payment'] = 0.0
    if include_schedule:
        mp, _, schedule = compute_loan_schedule(
            float(loan['amount'] or 0),
            float(loan['interest_rate'] or 0),
            max(int(loan['tenure'] or 1), 1),
            loan['interest_method'] or 'reducing_annual',
        )
        data['monthly_payment'] = float(mp or 0)
        data['schedule'] = schedule
    return data


def _get_savings(db, member_id, limit=12):
    rows = db.execute(
        '''SELECT id, amount, month, late_fee, payment_type, payment_method,
                  receipt_number, date
           FROM savings WHERE member_id = ?
           ORDER BY date DESC LIMIT ?''',
        (member_id, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _get_loans(db, member_id):
    rows = db.execute(
        '''SELECT id, loan_number, amount, purpose, tenure, interest_rate,
                  interest_method, total_repayment, balance, status, approval_stage,
                  disbursement_date, date_applied, withdrawn_at, withdrawal_reason
           FROM loans WHERE member_id = ?
           ORDER BY date_applied DESC, id DESC''',
        (member_id,),
    ).fetchall()
    return [_loan_payload(r) for r in rows]


def _get_transactions(db, member_id, limit=10):
    savings = db.execute(
        '''SELECT 'saving' AS type, amount, date, receipt_number AS reference
           FROM savings WHERE member_id = ?
           ORDER BY date DESC LIMIT ?''',
        (member_id, limit),
    ).fetchall()
    repayments = db.execute(
        '''SELECT 'repayment' AS type, r.amount, r.date, r.repayment_number AS reference
           FROM repayments r
           JOIN loans l ON r.loan_id = l.id
           WHERE l.member_id = ?
           ORDER BY r.date DESC LIMIT ?''',
        (member_id, limit),
    ).fetchall()
    combined = [_row_to_dict(r) for r in savings] + [_row_to_dict(r) for r in repayments]
    combined.sort(key=lambda x: x.get('date') or '', reverse=True)
    return combined[:limit]


def _get_notifications(db, user_id, limit=20):
    rows = db.execute(
        '''SELECT id, title, message, notification_type, is_read, action_url,
                  created_at, read_at
           FROM notifications WHERE user_id = ?
           ORDER BY created_at DESC LIMIT ?''',
        (user_id, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _card_data(member, db):
    return {
        'member_number': member['member_number'],
        'full_name': f"{member['first_name']} {member['last_name']}",
        'status': member['status'],
        'card_number': member['card_number'],
        'card_status': member['card_status'],
        'date_joined': str(member['date_joined'])[:10] if member['date_joined'] else '',
        'total_savings': float(member_savings_balance(db, member['id']) or 0),
    }


@mobile_api.route('/api/mobile/login', methods=['POST'])
def mobile_login():
    """Authenticate and return a short-lived JWT token."""
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    if not username or not password:
        return jsonify({'success': False, 'error': 'username and password are required'}), 400

    login_key = _mobile_login_key()
    if is_rate_limited(login_key):
        return jsonify({
            'success': False,
            'error': 'Too many failed login attempts. Please try again later.',
            'retry_after_seconds': lockout_seconds_remaining(login_key),
        }), 429

    db = get_db()
    user = db.execute('''
        SELECT * FROM users
        WHERE is_active = 1
          AND (
            lower(username) = lower(?)
            OR lower(COALESCE(email, '')) = lower(?)
          )
        LIMIT 1
    ''', (username, username)).fetchone()

    if not user or not check_password_hash(user['password_hash'], password):
        record_failed_login(login_key)
        return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

    clear_login_attempts(login_key)
    db.execute('UPDATE users SET last_login = ? WHERE id = ?', (datetime.now(), user['id']))
    db.commit()
    token = _generate_token(user['id'], user['username'], user['role'],
                            user['session_version'] if 'session_version' in user.keys() else 0)
    return jsonify({
        'success': True,
        'token': token,
        'expires_in_seconds': MOBILE_TOKEN_TTL_HOURS * 3600,
        'user': {
            'id': user['id'],
            'username': user['username'],
            'role': user['role'],
            'email': user['email'] or '',
        },
    })


@mobile_api.route('/api/mobile/v1/auth/forgot-password', methods=['POST'])
def mobile_forgot_password():
    """Request a password-reset email from mobile.

    The response is deliberately generic so the endpoint cannot be used for
    account enumeration.
    """
    data = request.get_json(silent=True) or {}
    identifier = (data.get('identifier') or '').strip()
    ip = request.remote_addr or '0.0.0.0'
    ua = request.user_agent.string if request.user_agent else ''
    generic = {
        'success': True,
        'message': 'If an active account exists, a password reset link will be sent to the registered email address.',
    }

    if not identifier:
        return _json_error('Enter your username or email address.', 400, 'missing_identifier')

    reset_key = f"mobile-reset:{ip}:{identifier.lower()}"
    if is_rate_limited(reset_key):
        return jsonify({
            'success': False,
            'error': 'Too many reset requests. Please wait before trying again.',
            'retry_after_seconds': lockout_seconds_remaining(reset_key),
            'code': 'rate_limited',
        }), 429

    record_failed_login(reset_key, identifier)
    db = get_db()
    user = db.execute('''
        SELECT id, username, email, full_name, is_active
        FROM users
        WHERE lower(username) = lower(?)
           OR lower(COALESCE(email, '')) = lower(?)
        LIMIT 1
    ''', (identifier, identifier)).fetchone()

    if user and user['is_active'] and (user['email'] or '').strip():
        reset_url = _issue_mobile_password_reset_link(db, user)
        sent = send_password_reset_email(user['email'], dict(user), reset_url)
        from security import log_audit
        log_audit(
            db,
            user['id'],
            user['username'],
            'MOBILE_PASSWORD_RESET_REQUEST',
            'auth',
            'Mobile password reset link requested' + ('' if sent else ' but email delivery failed'),
            ip,
            ua,
        )
    else:
        from security import log_audit
        log_audit(
            db,
            None,
            identifier,
            'MOBILE_PASSWORD_RESET_REQUEST',
            'auth',
            'Mobile password reset requested for unknown, inactive, or email-less account',
            ip,
            ua,
        )
    db.commit()
    return jsonify(generic)


@mobile_api.route('/api/mobile/v1/auth/change-password', methods=['POST'])
@jwt_required
def mobile_change_password():
    data = request.get_json(silent=True) or {}
    current_password = data.get('current_password') or ''
    new_password = data.get('new_password') or ''
    confirm_password = data.get('confirm_password') or ''

    if not current_password or not new_password or not confirm_password:
        return _json_error('Current password, new password, and confirmation are required.', 400, 'missing_fields')
    if new_password != confirm_password:
        return _json_error('New passwords do not match.', 400, 'password_mismatch')

    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ? AND is_active = 1', (g.user_id,)).fetchone()
    if not user or not check_password_hash(user['password_hash'], current_password):
        record_failed_login(f"mobile-change-password:{request.remote_addr or '0.0.0.0'}", g.username)
        return _json_error('Current password is incorrect.', 401, 'invalid_current_password')

    ok, errors = validate_password_strength(new_password, db)
    if not ok:
        return _json_error(' '.join(errors), 400, 'weak_password')

    db.execute(
        'UPDATE users SET session_version = COALESCE(session_version, 0) + 1, password_hash = ?, must_change_password = 0 WHERE id = ?',
        (generate_password_hash(new_password), g.user_id),
    )
    audit(db, 'MOBILE_CHANGE_PASSWORD', 'auth', 'Mobile user changed password')
    db.commit()
    # Every older token is now revoked; hand this device a fresh one.
    fresh = db.execute('SELECT id, username, role, session_version FROM users WHERE id = ?', (g.user_id,)).fetchone()
    return jsonify({'success': True, 'message': 'Password changed successfully.',
                    'token': _generate_token(fresh['id'], fresh['username'], fresh['role'], fresh['session_version'])})


@mobile_api.route('/api/mobile/v1/me')
@member_required
def mobile_me():
    return jsonify({
        'success': True,
        'user': {
            'id': g.user_id,
            'username': g.username,
            'role': g.role,
        },
        'member': _member_summary(g.member, g.db),
    })


@mobile_api.route('/api/mobile/v1/profile', methods=['GET', 'PATCH'])
@member_required
def mobile_profile():
    db = g.db
    member = g.member
    if request.method == 'GET':
        data = _row_to_dict(member)
        masked = mask_member_sensitive_fields(data)
        profile = {
            field: data.get(field, '') for field in PROFILE_FIELDS
            if field not in {'bank_name', 'account_name', 'account_number', 'bvn', 'nin'}
        }
        profile.update({
            'email': member['email'] or '',
            'member_number': member['member_number'],
            'bank_name_masked': masked.get('bank_name_masked', ''),
            'account_name_masked': masked.get('account_name_masked', ''),
            'account_number_masked': masked.get('account_number_masked', ''),
            'bvn_masked': masked.get('bvn_masked', ''),
            'nin_masked': masked.get('nin_masked', ''),
        })
        return jsonify({
            'success': True,
            'profile': profile,
            'profile_completion': _profile_completion(member),
        })

    data = request.get_json(silent=True) or {}
    updates = {
        field: str(data[field]).strip()
        for field in PROFILE_FIELDS
        if field in data and data[field] is not None
    }
    if not updates:
        return jsonify({'success': False, 'error': 'No editable profile fields supplied'}), 400

    updates = encrypt_member_sensitive_fields(updates)
    set_clause = ', '.join(f'{field} = ?' for field in updates)
    db.execute(
        f'UPDATE members SET {set_clause} WHERE id = ?',
        (*updates.values(), member['id']),
    )
    audit(db, 'MOBILE_PROFILE_UPDATE', 'members',
          f"Member {member['id']} updated profile from mobile API")
    db.commit()
    refreshed = db.execute('SELECT * FROM members WHERE id = ?', (member['id'],)).fetchone()
    return jsonify({
        'success': True,
        'member': _member_summary(refreshed, db),
    })


@mobile_api.route('/api/mobile/v1/dashboard')
@member_required
def mobile_v1_dashboard():
    db = g.db
    member = g.member
    unread = db.execute(
        'SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0',
        (g.user_id,),
    ).fetchone()[0]
    active_balance = db.execute(
        "SELECT COALESCE(SUM(balance), 0) FROM loans WHERE member_id = ? AND status = 'active'",
        (member['id'],),
    ).fetchone()[0] or 0
    return jsonify({
        'success': True,
        'member': _member_summary(member, db),
        'summary': {
            'unread_notifications': int(unread or 0),
            'active_loan_balance': float(active_balance or 0),
        },
        'savings': _get_savings(db, member['id']),
        'loans': _get_loans(db, member['id']),
        'recent_transactions': _get_transactions(db, member['id']),
        'notifications': _get_notifications(db, g.user_id),
    })


@mobile_api.route('/api/mobile/dashboard')
@jwt_required
def mobile_dashboard():
    """Backward-compatible dashboard route."""
    db = get_db()
    member = member_for_user(db, g.user_id)
    if not member:
        return jsonify({
            'success': False,
            'error': 'No member profile is linked to this account. Contact admin.',
        }), 404
    return jsonify({
        'success': True,
        'member': _member_summary(member, db),
        'savings': _get_savings(db, member['id']),
        'loans': _get_loans(db, member['id']),
        'recent_transactions': _get_transactions(db, member['id']),
        'notifications': _get_notifications(db, g.user_id, limit=10),
    })


@mobile_api.route('/api/mobile/v1/savings')
@member_required
def mobile_savings():
    limit = min(max(int(request.args.get('limit', 50)), 1), 250)
    return jsonify({
        'success': True,
        'balance': float(member_savings_balance(g.db, g.member['id']) or 0),
        'rows': _get_savings(g.db, g.member['id'], limit=limit),
    })


@mobile_api.route('/api/mobile/v1/loans')
@member_required
def mobile_loans():
    return jsonify({'success': True, 'loans': _get_loans(g.db, g.member['id'])})


# ── Paying in: the member's own account number ─────────────────────────────────

def _in_target_advance(db, member_id):
    try:
        from blueprints.ctas import ctas_enabled
        if not ctas_enabled(db):
            return False
        return db.execute(
            "SELECT 1 FROM ctas_subscriptions WHERE member_id = ? "
            "AND status IN ('enrolled', 'scheduled', 'active_recovery') LIMIT 1",
            (member_id,)).fetchone() is not None
    except Exception:
        return False


@mobile_api.route('/api/mobile/v1/pay-in', methods=['GET', 'PATCH'])
@member_required
def mobile_pay_in():
    """The member's personal account number, and what their transfers pay for.

    GET returns ``enabled: false`` when the cooperative does not issue account
    numbers, so the app simply hides the section rather than showing an error.
    """
    from virtual_accounts import (MEMBER_PREFERENCES, account_for_member,
                                  member_preference, set_member_preference,
                                  va_config, va_enabled)

    if not va_enabled(g.db):
        return jsonify({'success': True, 'enabled': False, 'account': None,
                        'preference': '', 'choices': []})

    member_id = g.member['id']

    if request.method == 'PATCH':
        choice = (request.get_json(silent=True) or {}).get('preference', '')
        if choice == 'ctas' and not _in_target_advance(g.db, member_id):
            return jsonify({'success': False,
                            'error': 'You are not currently on a Target Advance cycle.'}), 400
        if not set_member_preference(g.db, member_id, choice):
            return jsonify({'success': False, 'error': 'That is not one of the options.'}), 400
        g.db.commit()

    row = account_for_member(g.db, member_id, va_config(g.db)['va_provider'])
    account = None
    if row and row['status'] == 'active':
        account = {
            'account_number': row['account_number'],
            'account_name': row['account_name'],
            'bank_name': row['bank_name'],
        }

    choices = [{'key': key, 'label': label} for key, label in MEMBER_PREFERENCES.items()
               if key != 'ctas' or _in_target_advance(g.db, member_id)]

    return jsonify({
        'success': True,
        'enabled': True,
        'account': account,
        'preference': member_preference(g.db, member_id),
        'choices': choices,
    })


# ── CTAS (Target Advance) — optional module, member-facing ──────────────────────

def _ctas_sub_json(s):
    target = float(s['target_amount'] or 0)
    recovered = float(s['total_recovered'] or 0)
    return {
        'id': s['id'],
        'cycle_name': s['cycle_name'],
        'target_amount': target,
        'tenure_months': s['tenure_months'],
        'monthly_deduction': float(s['monthly_deduction'] or 0),
        'status': s['status'],
        'payout_month': s['payout_month'],
        'total_recovered': recovered,
        'outstanding': round(target - recovered, 2),
        'arrears_amount': float(s['arrears_amount'] or 0) if 'arrears_amount' in s.keys() else 0.0,
        'progress': round((recovered / target * 100) if target else 0),
    }


@mobile_api.route('/api/mobile/v1/ctas')
@member_required
def mobile_ctas():
    """Member's CTAS view: enabled flag, their subscriptions, and open cycles."""
    from blueprints.ctas import ctas_enabled
    import ctas_engine as ce
    db, member = g.db, g.member
    if not ctas_enabled(db):
        return jsonify({'success': True, 'enabled': False, 'subscriptions': [], 'open_cycles': []})
    subs = db.execute('''SELECT s.*, c.name AS cycle_name FROM ctas_subscriptions s
                         JOIN ctas_cycles c ON c.id = s.cycle_id
                         WHERE s.member_id = ? ORDER BY s.applied_at DESC''', (member['id'],)).fetchall()
    open_cycles = db.execute('''SELECT id, name, duration_months, affordability_method, savings_multiple
                                FROM ctas_cycles WHERE status = 'open'
                                  AND id NOT IN (SELECT cycle_id FROM ctas_subscriptions WHERE member_id = ?)
                                ORDER BY name''', (member['id'],)).fetchall()
    return jsonify({
        'success': True,
        'enabled': True,
        'savings_balance': float(member['total_savings'] or 0) if 'total_savings' in member.keys() else 0.0,
        'has_active': ce.member_has_active_subscription(db, member['id']),
        'subscriptions': [_ctas_sub_json(s) for s in subs],
        'open_cycles': [{'id': c['id'], 'name': c['name'], 'duration_months': c['duration_months'],
                         'affordability_method': c['affordability_method'],
                         'savings_multiple': float(c['savings_multiple'] or 0)} for c in open_cycles],
    })


@mobile_api.route('/api/mobile/v1/ctas/apply', methods=['POST'])
@member_required
def mobile_ctas_apply():
    from blueprints.ctas import ctas_enabled
    import ctas_engine as ce
    db, member = g.db, g.member
    if not ctas_enabled(db):
        return jsonify({'success': False, 'error': 'Target Advance is not enabled.'}), 404
    data = request.get_json(silent=True) or {}
    cycle = db.execute("SELECT * FROM ctas_cycles WHERE id = ? AND status = 'open'",
                       (data.get('cycle_id'),)).fetchone()
    if not cycle:
        return jsonify({'success': False, 'error': 'That cycle is not open for applications.'}), 400
    try:
        target = finite_float(data.get('target_amount') or 0)
        tenure = int(data.get('tenure_months') or 0)
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'Enter a valid amount and tenure.'}), 400
    if not data.get('terms_accepted'):
        return jsonify({'success': False, 'error': 'You must accept the scheme terms.'}), 400
    signature = (data.get('signature_name') or '').strip()
    if not signature:
        return jsonify({'success': False, 'error': 'Type your full name as your signature.'}), 400
    res = ce.check_eligibility(db, member, cycle, target, tenure)
    hard = [r for r in res['reasons'] if 'exceeds' not in r and 'salary' not in r.lower()]
    if hard:
        return jsonify({'success': False, 'error': 'Cannot apply: ' + ' '.join(hard)}), 400
    db.execute('''INSERT INTO ctas_subscriptions
            (cycle_id, member_id, target_amount, tenure_months, monthly_deduction, admin_fee,
             status, outstanding, terms_accepted, signature_name, signed_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, 'submitted', ?, 1, ?, ?, ?)''',
        (cycle['id'], member['id'], target, tenure, res['monthly_deduction'],
         res['admin_fee'], target, signature, datetime.now(), g.user_id))
    audit(db, 'CTAS_APPLY', 'ctas', f"Mobile: member {member['id']} applied to cycle {cycle['name']}")
    db.commit()
    return jsonify({'success': True, 'monthly_deduction': res['monthly_deduction'],
                    'admin_fee': res['admin_fee'], 'eligible': res['eligible']})


@mobile_api.route('/api/mobile/v1/loans/options')
@member_required
def mobile_loan_options():
    db = g.db
    member = g.member
    is_staff_member = _mobile_member_is_staff(member)
    return jsonify({
        'success': True,
        'purposes': _loan_purpose_options(db),
        'collateral_options': _collateral_options(is_staff_member),
        'guarantors_required': lw.guarantors_required(db),
        'eligible_guarantors': _eligible_guarantors(db, member['id']),
        'max_tenure_months': _max_tenure(db),
        'loan_limits': loan_limits.limits(db),
        'loan_eligibility_amount': loan_limits.eligible_amount(db, member_savings_balance(db, member['id'])),
        'staff_member': bool(is_staff_member),
    })


@mobile_api.route('/api/mobile/v1/loans/schedule-preview', methods=['POST'])
@member_required
def mobile_loan_schedule_preview():
    data = request.get_json(silent=True) or {}
    try:
        amount = finite_float(data.get('amount') or 0)
        tenure = int(data.get('tenure') or 0)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid amount or tenure'}), 400
    purpose = (data.get('purpose') or 'Regular').strip()
    if amount <= 0 or tenure <= 0:
        return jsonify({'success': False, 'error': 'Enter an amount and tenure.'}), 400
    limit_error = loan_limits.application_error(g.db, purpose, amount, tenure)
    if limit_error:
        return jsonify({'success': False, 'error': limit_error}), 400

    rates = _interest_rates(g.db)
    if purpose not in rates:
        return jsonify({'success': False, 'error': 'Select a valid loan purpose.'}), 400
    methods = _interest_methods(g.db)
    rate = rates.get(purpose, rates['Regular'])
    method = methods.get(purpose, 'reducing_annual')
    monthly_payment, total_repayment, schedule = compute_loan_schedule(amount, rate, tenure, method)
    return jsonify({
        'success': True,
        'amount': amount,
        'purpose': purpose,
        'tenure': tenure,
        'interest_rate': rate,
        'interest_method': method,
        'monthly_payment': monthly_payment,
        'total_repayment': total_repayment,
        'total_interest': round(total_repayment - amount, 2),
        'schedule': schedule,
    })


@mobile_api.route('/api/mobile/v1/loans/apply', methods=['POST'])
@member_required
def mobile_apply_loan():
    db = g.db
    member = g.member
    data = request.get_json(silent=True) or {}
    try:
        amount = finite_float(data.get('amount') or 0)
        tenure = int(data.get('tenure') or 0)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid amount or tenure'}), 400
    purpose = (data.get('purpose') or '').strip()
    signature = (data.get('signature_name') or '').strip()
    payment_collateral_type = (data.get('payment_collateral_type') or '').strip()
    raw_guarantor_ids = [str(gid).strip() for gid in data.get('guarantor_ids', []) if str(gid).strip()]
    guarantor_ids = [gid for gid in raw_guarantor_ids if gid != str(member['id'])]
    guarantor_ids = list(dict.fromkeys(guarantor_ids))

    if amount <= 0 or tenure <= 0 or not purpose:
        return jsonify({'success': False, 'error': 'amount, tenure and purpose are required'}), 400
    limit_error = loan_limits.application_error(db, purpose, amount, tenure)
    if limit_error:
        return jsonify({'success': False, 'error': limit_error}), 400
    rates = _interest_rates(db)
    if purpose not in rates:
        return jsonify({'success': False, 'error': 'Select a valid loan purpose.'}), 400

    is_staff_member = _mobile_member_is_staff(member)
    required_acknowledgements = {
        'accept_terms': 'You must accept the loan terms and conditions.',
        'data_processing_consent': 'You must permit the cooperative to process your personal information for this loan application.',
        'repayment_schedule_accepted': 'You must accept the calculated repayment schedule before submitting.',
    }
    if is_staff_member:
        required_acknowledgements['hr_affordability_consent'] = (
            'You must permit HR/payroll affordability confirmation for this staff cooperative loan.'
        )
    else:
        required_acknowledgements.update({
            'credit_check_consent': 'You must permit the cooperative to perform affordability and credit checks.',
            'bank_statement_ack': 'You must acknowledge that a bank statement is required before disbursement.',
        })
    for field, message in required_acknowledgements.items():
        if not data.get(field):
            return jsonify({'success': False, 'error': message}), 400
    if not signature:
        return jsonify({'success': False, 'error': 'Type your full name to sign this application.'}), 400
    if payment_collateral_type not in {'standing_order', 'post_dated_cheques'}:
        return jsonify({'success': False, 'error': 'Select a valid repayment collateral option.'}), 400
    if is_staff_member and payment_collateral_type != 'standing_order':
        return jsonify({'success': False, 'error': 'Staff cooperative loans must use standing order/salary deduction.'}), 400
    if str(member['id']) in raw_guarantor_ids:
        return jsonify({'success': False, 'error': 'You cannot select yourself as guarantor.'}), 400

    try:
        eligible_age, joined_at, _days_as_member = member_has_minimum_membership(member, 6)
        if not joined_at:
            return jsonify({'success': False, 'error': 'Your join date is missing. Contact admin.'}), 400
        if not eligible_age:
            return jsonify({'success': False, 'error': 'You must be a member for at least 6 months to apply.'}), 400

        savings_balance = member_savings_balance(db, member['id'])
        if savings_balance < 50000:
            return jsonify({'success': False, 'error': 'Minimum savings of NGN 50,000 required.'}), 400
        if has_unpaid_loan_of_type(db, member['id'], purpose):
            return jsonify({'success': False, 'error': f'You have an unpaid {purpose} loan. Repay it fully before applying for another {purpose} loan. Other loan types can be submitted for approval.'}), 409
        max_loan = loan_limits.eligible_amount(db, savings_balance, purpose)
        if amount > max_loan:
            return jsonify({'success': False,
                            'error': f'Maximum loan amount is NGN {max_loan:,.2f}.'}), 400

        required_g = lw.guarantors_required(db)
        if len(guarantor_ids) < required_g:
            return jsonify({'success': False, 'error': f'Select {required_g} guarantor(s).'}), 400
        if guarantor_ids:
            placeholders = ','.join(['?'] * len(guarantor_ids))
            active_guarantors = db.execute(
                f"SELECT id FROM members WHERE status = 'active' AND id IN ({placeholders})",
                guarantor_ids,
            ).fetchall()
            active_ids = {str(row['id']) for row in active_guarantors}
            if any(gid not in active_ids for gid in guarantor_ids):
                return jsonify({'success': False, 'error': 'Select guarantors from active cooperative members.'}), 400
        methods = _interest_methods(db)
        rate = rates.get(purpose, rates['Regular'])
        method = methods.get(purpose, 'reducing_annual')
        monthly_payment, total_repayment, schedule = compute_loan_schedule(amount, rate, tenure, method)
        schedule_snapshot = json.dumps({
            'principal': round(amount, 2),
            'purpose': purpose,
            'tenure': tenure,
            'interest_rate': rate,
            'interest_method': method,
            'monthly_payment': round(monthly_payment, 2),
            'total_repayment': round(total_repayment, 2),
            'schedule': schedule,
            'accepted_at': datetime.now().isoformat(timespec='seconds'),
            'accepted_channel': 'mobile',
        }, separators=(',', ':'))

        initial_stage = lw.STAGE_GUARANTORS if required_g > 0 else lw.STAGE_SECRETARY
        loan_number = f"LOAN/{datetime.now().strftime('%Y%m%d')}/{random.randint(1000, 9999)}"
        db.execute('''
            INSERT INTO loans (loan_number, member_id, amount, purpose, tenure, interest_rate,
                               interest_method, total_repayment, balance, status, approval_stage,
                               terms_accepted, data_processing_consent, credit_check_consent,
                               credit_check_status, repayment_schedule_accepted, bank_statement_status,
                               payment_collateral_type, payment_collateral_status,
                               repayment_schedule_snapshot, consent_ip, loan_applicant_type,
                               hr_affordability_consent, hr_affordability_status, signature_name,
                               signed_at, date_applied)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, 1, 1, ?, ?, 1, ?,
                    ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (loan_number, member['id'], amount, purpose, tenure, rate,
              method, total_repayment, 0, initial_stage,
              0 if is_staff_member else 1,
              'not_required' if is_staff_member else 'pending',
              'not_required' if is_staff_member else 'requested',
              payment_collateral_type, schedule_snapshot, request.remote_addr,
              'staff' if is_staff_member else 'non_staff',
              1 if is_staff_member else 0,
              'pending' if is_staff_member else 'not_required',
              signature, datetime.now(), datetime.now()))
        loan_id = last_insert_id(db)
        lw.record_action(db, loan_id, 'submitted', 'submitted',
                         acted_by=g.user_id, acted_by_name=g.username,
                         comment='Member mobile self-application')

        for gid in guarantor_ids:
            guarantor = db.execute('SELECT * FROM members WHERE id = ?', (gid,)).fetchone()
            if not guarantor:
                continue
            db.execute(
                "INSERT INTO loan_guarantors (loan_id, member_id, status) VALUES (?, ?, 'pending')",
                (loan_id, gid),
            )
            if guarantor['email']:
                notify_member(db, guarantor['email'], 'Guarantor Request',
                              f"{member['first_name']} {member['last_name']} asked you to guarantee "
                              f"a NGN {amount:,.2f} loan. Please review and respond.",
                              'warning', '/my-guarantor-requests')
                send_guarantor_request_email(guarantor['email'], guarantor, member, loan_number, amount)

        audit(db, 'MOBILE_LOAN_APPLICATION', 'loans',
              f"Member {member['id']} applied for NGN {amount:,.2f} {purpose} loan - {loan_number}")
        db.commit()
        # Log the request and alert the President, Treasurer, General Secretary
        # and exco right now — with the full application attached as a PDF.
        la.notify_loan_submitted(db, loan_id, channel='mobile')
        loan = db.execute('SELECT * FROM loans WHERE id = ?', (loan_id,)).fetchone()
        return jsonify({'success': True, 'loan': _loan_payload(loan, include_schedule=True)}), 201
    except Exception as exc:
        db.rollback()
        return jsonify({'success': False, 'error': f'Error submitting application: {exc}'}), 500


@mobile_api.route('/api/mobile/v1/loans/<int:loan_id>')
@member_required
def mobile_loan_detail(loan_id):
    loan = g.db.execute(
        'SELECT * FROM loans WHERE id = ? AND member_id = ?',
        (loan_id, g.member['id']),
    ).fetchone()
    if not loan:
        return jsonify({'success': False, 'error': 'Loan not found'}), 404
    return jsonify({'success': True, 'loan': _loan_payload(loan, include_schedule=True)})


@mobile_api.route('/api/mobile/v1/loans/<int:loan_id>/application.pdf')
@member_required
def mobile_loan_application_pdf(loan_id):
    """Download your own loan application as a PDF from the mobile app."""
    loan = g.db.execute(
        'SELECT id FROM loans WHERE id = ? AND member_id = ?',
        (loan_id, g.member['id']),
    ).fetchone()
    if not loan:
        return jsonify({'success': False, 'error': 'Loan not found'}), 404
    pdf_bytes, filename = build_loan_application_pdf(g.db, loan_id)
    if not pdf_bytes:
        return jsonify({'success': False, 'error': 'Could not build the application PDF'}), 500
    response = make_response(pdf_bytes)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'inline; filename={filename}'
    return response


@mobile_api.route('/api/mobile/v1/loans/<int:loan_id>/withdraw', methods=['POST'])
@member_required
def mobile_withdraw_loan(loan_id):
    data = request.get_json(silent=True) or {}
    reason = (data.get('reason') or '').strip()
    db = g.db
    loan = db.execute(
        'SELECT * FROM loans WHERE id = ? AND member_id = ?',
        (loan_id, g.member['id']),
    ).fetchone()
    if not loan:
        return jsonify({'success': False, 'error': 'Loan not found'}), 404
    if loan['status'] != 'pending':
        return jsonify({
            'success': False,
            'error': 'Only pending loan applications can be withdrawn before final approval or disbursement.',
        }), 409

    now = datetime.now()
    db.execute('''
        UPDATE loans
           SET status = 'withdrawn',
               approval_stage = 'withdrawn',
               withdrawn_at = ?,
               withdrawn_by = ?,
               withdrawal_reason = ?
         WHERE id = ?
    ''', (now, g.user_id, reason or 'Withdrawn by applicant via mobile app', loan_id))
    db.execute(
        "UPDATE loan_guarantors SET status = 'withdrawn', responded_at = ? "
        "WHERE loan_id = ? AND status = 'pending'",
        (now, loan_id),
    )
    lw.record_action(db, loan_id, 'withdrawn', 'withdrawn',
                     acted_by=g.user_id, acted_by_name=g.username,
                     comment=reason or 'Applicant withdrew the pending application via mobile app')
    audit(db, 'MOBILE_LOAN_APPLICATION_WITHDRAWN', 'loans',
          f"Member {g.member['id']} withdrew loan {loan['loan_number']}: {reason or 'No reason supplied'}")
    for u in db.execute(
        "SELECT id FROM users WHERE role IN ('admin', 'treasurer', 'secretary') "
        "AND COALESCE(is_active, 1) = 1"
    ).fetchall():
        notify(db, u['id'], 'Loan Application Withdrawn',
               f"{g.member['first_name']} {g.member['last_name']} withdrew loan application "
               f"{loan['loan_number']} from the mobile app.", 'info', f"/loans/{loan_id}")
    db.commit()
    updated = db.execute('SELECT * FROM loans WHERE id = ?', (loan_id,)).fetchone()
    return jsonify({'success': True, 'loan': _loan_payload(updated, include_schedule=True)})


@mobile_api.route('/api/mobile/v1/notifications')
@member_required
def mobile_notifications():
    limit = min(max(int(request.args.get('limit', 50)), 1), 250)
    return jsonify({
        'success': True,
        'notifications': _get_notifications(g.db, g.user_id, limit=limit),
    })


@mobile_api.route('/api/mobile/v1/notifications/<int:notification_id>/read', methods=['POST'])
@jwt_required
def mobile_mark_notification_read(notification_id):
    db = get_db()
    db.execute(
        'UPDATE notifications SET is_read = 1, read_at = ? WHERE id = ? AND user_id = ?',
        (datetime.now(), notification_id, g.user_id),
    )
    db.commit()
    return jsonify({'success': True})


@mobile_api.route('/api/mobile/v1/notifications/mark-all-read', methods=['POST'])
@jwt_required
def mobile_mark_all_notifications_read():
    db = get_db()
    db.execute(
        'UPDATE notifications SET is_read = 1, read_at = ? WHERE user_id = ? AND is_read = 0',
        (datetime.now(), g.user_id),
    )
    db.commit()
    return jsonify({'success': True})


@mobile_api.route('/api/mobile/v1/devices', methods=['POST'])
@member_required
def mobile_register_device():
    data = request.get_json(silent=True) or {}
    push_token = (data.get('push_token') or '').strip()
    if not push_token:
        return jsonify({'success': False, 'error': 'push_token is required'}), 400
    platform = (data.get('platform') or '').strip().lower()[:30]
    device_name = (data.get('device_name') or '').strip()[:120]
    now = datetime.now()

    existing = g.db.execute(
        'SELECT id FROM mobile_devices WHERE push_token = ?', (push_token,),
    ).fetchone()
    if existing:
        g.db.execute('''
            UPDATE mobile_devices
               SET user_id = ?, member_id = ?, platform = ?, device_name = ?,
                   enabled = 1, last_seen_at = ?
             WHERE id = ?
        ''', (g.user_id, g.member['id'], platform, device_name, now, existing['id']))
        device_id = existing['id']
    else:
        g.db.execute('''
            INSERT INTO mobile_devices
                (user_id, member_id, platform, push_token, device_name, enabled, last_seen_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
        ''', (g.user_id, g.member['id'], platform, push_token, device_name, now))
        device_id = g.db.execute(
            'SELECT id FROM mobile_devices WHERE push_token = ?', (push_token,),
        ).fetchone()['id']
    g.db.commit()
    return jsonify({'success': True, 'device_id': device_id})


@mobile_api.route('/api/mobile/v1/tenant', methods=['GET'])
def mobile_tenant():
    """Public tenant identity — lets the app confirm which cooperative a member
    is signing in to (name + logo) before login. No authentication required."""
    db = get_db()

    def _setting(key, default=''):
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return (row['value'] if row and row['value'] else default)

    coop_name = _setting('coop_name', 'Cooperative')
    return jsonify({
        'success': True,
        'coop_name': coop_name,
        'coop_short_name': _setting('coop_short_name', coop_name),
        'logo': _setting('coop_logo', ''),
    })


@mobile_api.route('/api/hq/member-count', methods=['GET'])
def hq_member_count():
    """Report this cooperative's active-member count to HQ for billing.
    Guarded by the shared HQ_SYNC_TOKEN (header X-HQ-Token); not public."""
    token = (os.environ.get('HQ_SYNC_TOKEN') or '').strip()
    provided = (request.headers.get('X-HQ-Token') or '').strip()
    if not token or not hmac.compare_digest(provided, token):
        return jsonify({'success': False, 'error': 'Unauthorised'}), 403
    db = get_db()
    n = db.execute("SELECT COUNT(*) FROM members WHERE status = 'active'").fetchone()[0] or 0
    return jsonify({'success': True, 'active_members': int(n)})


@mobile_api.route('/api/hq/set-status', methods=['POST'])
def hq_set_status():
    """Let HQ suspend or reactivate this cooperative's access. Guarded by the
    shared HQ_SYNC_TOKEN. Sets settings.tenant_suspended, which the app's
    before-request gate enforces."""
    token = (os.environ.get('HQ_SYNC_TOKEN') or '').strip()
    provided = (request.headers.get('X-HQ-Token') or '').strip()
    if not token or not hmac.compare_digest(provided, token):
        return jsonify({'success': False, 'error': 'Unauthorised'}), 403
    data = request.get_json(silent=True) or {}
    suspended = bool(data.get('suspended'))
    db = get_db()
    db.execute("DELETE FROM settings WHERE key = 'tenant_suspended'")
    db.execute("INSERT INTO settings (key, value) VALUES ('tenant_suspended', ?)",
               ('1' if suspended else '0',))
    db.commit()
    return jsonify({'success': True, 'suspended': suspended})


@mobile_api.route('/api/hq/set-feature', methods=['POST'])
def hq_set_feature():
    """Let HQ turn an optional add-on on/off for this cooperative (on request).
    Guarded by the shared HQ_SYNC_TOKEN. Currently supports feature 'ctas'."""
    token = (os.environ.get('HQ_SYNC_TOKEN') or '').strip()
    provided = (request.headers.get('X-HQ-Token') or '').strip()
    if not token or not hmac.compare_digest(provided, token):
        return jsonify({'success': False, 'error': 'Unauthorised'}), 403
    data = request.get_json(silent=True) or {}
    feature = (data.get('feature') or '').strip()
    enabled = bool(data.get('enabled'))
    if feature == 'ctas':
        from blueprints.ctas import set_ctas_enabled
        db = get_db()
        set_ctas_enabled(db, enabled)
        db.commit()
        return jsonify({'success': True, 'feature': 'ctas', 'enabled': enabled})
    return jsonify({'success': False, 'error': 'Unknown feature'}), 400


@mobile_api.route('/api/mobile/v1/tenants/resolve', methods=['GET'])
def mobile_resolve_tenant():
    """Resolve a short cooperative code to a tenant API base URL.

    Intended to be served by the HQ tenant. The mobile app calls this before it
    knows which cooperative backend to use.
    """
    code = (request.args.get('code') or '').strip().lower()
    if not TENANT_CODE_RE.match(code or ''):
        return _json_error('Enter a valid cooperative code.', 400, 'invalid_tenant_code')

    db = get_db()
    row = db.execute('''
        SELECT code, name, base_url, logo_url, is_active
        FROM coop_tenants
        WHERE lower(code) = lower(?)
        LIMIT 1
    ''', (code,)).fetchone()
    if row and row['is_active']:
        return jsonify({
            'success': True,
            'tenant': {
                'code': row['code'],
                'coop_name': row['name'],
                'base_url': (row['base_url'] or '').rstrip('/'),
                'logo': row['logo_url'] or '',
            },
        })
    if row and not row['is_active']:
        return _json_error('This cooperative is not active on mobile.', 404, 'tenant_inactive')

    for record in _env_tenant_records():
        tenant = _tenant_record_from_mapping(record, code)
        if tenant:
            return jsonify({'success': True, 'tenant': tenant})

    return _json_error('Cooperative not found — check the code with your society.', 404, 'tenant_not_found')


@mobile_api.route('/api/mobile/card')
@jwt_required
def mobile_card():
    """Return digital membership card data."""
    db = get_db()
    member = member_for_user(db, g.user_id)
    if not member:
        return jsonify({'success': False, 'error': 'Member profile not found'}), 404
    return jsonify({'success': True, 'card': _card_data(member, db)})


@mobile_api.route('/api/mobile/pay', methods=['POST'])
@jwt_required
def mobile_payment():
    """Mobile repayments must go through verified gateway flows."""
    return jsonify({
        'success': False,
        'error': (
            'Mobile repayments are temporarily disabled. '
            'Please use the web payment flow or contact the cooperative office.'
        ),
    }), 503
