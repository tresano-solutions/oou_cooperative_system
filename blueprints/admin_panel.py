import os
import random
import re
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, render_template, redirect, url_for, request, flash
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename
from upload_history import decode_upload

from database import USE_POSTGRES, get_db, last_insert_id
from email_service import send_member_onboarding_email
from security import generate_account_setup_token, validate_password_strength
from utils import (finite_float, role_required, audit, validate_image, logo_data_uri,
                   member_savings_balance, reconcile_member_savings, notify,
                   coop_name as _coop_name)
from ledger import (post_journal_safe, get_default_cash_account, OPERATING_EXPENSES, FEE_INCOME,
                    HONORARIUM)
import permissions as perms
import loan_limits

admin_panel = Blueprint('admin_panel', __name__)


@admin_panel.route('/upload-history')
@login_required
@role_required('admin', 'treasurer', 'secretary')
def upload_history():
    page = max(1, request.args.get('page', 1, type=int))
    db = get_db()
    where = "action = 'UPLOAD_RESULT'"
    params = []
    if current_user.role == 'secretary':
        where += ' AND user_id = ?'
        params.append(current_user.id)
    logs = db.execute(
        f"SELECT * FROM audit_log WHERE {where} ORDER BY id DESC LIMIT 21 OFFSET ?",
        (*params, (page - 1) * 20)
    ).fetchall()
    return render_template('admin/upload-history.html',
                           logs=[decode_upload(r) for r in logs[:20]],
                           page=page, has_next=len(logs) > 20)

_DEFAULT_SETTINGS = {
    'mail_enabled':  '0',
    'resend_api_key': '',
    'brevo_api_key': '',
    'mail_from':     '',
    'coop_name': 'Your Cooperative',
    'reg_number': 'CMS/2005/001',
    'address': '',
    'phone': '',
    'email': '',
    'fy_start': '1',
    'currency': 'NGN',
    'date_format': 'Y-m-d',
    'session_timeout': '30',
    'password_min_length': '8',
    'password_require_upper': '1',
    'password_require_lower': '1',
    'password_require_number': '1',
    'password_require_special': '0',
    'maintenance_mode': '0',
    'min_savings': '5000',
    'share_capital_pct': '0',
    'savings_due_day': '10',
    'late_fee_percent': '10',
    'min_deposit_period': '90',
    'member_deposit_rate': '9',
    'nonmember_deposit_rate': '7',
    'dividend_rate': '50',
    'min_membership_months': '6',
    'min_savings_for_loan': '50000',
    'loan_multiplier': '2',
    'max_tenure_months': '18',
    'max_interest_rate': '11',
    'insurance_rate': '1',
    'guarantors_required': '2',
    'default_penalty_rate': '20',
    'interest_regular': '11',
    'interest_housing': '9',
    'interest_emergency': '10',
    'interest_asset': '10',
    'entrance_fee': '2000',
    'reentry_fee': '5000',
    'loan_application_fee': '1000',
    'statement_fee': '500',
    # Loan request alerts — see loan_alerts.py
    'loan_alert_enabled': '1',
    'loan_alert_attach_pdf': '1',
    'loan_alert_roles': 'admin,treasurer,secretary,exco',
    'loan_alert_extra_emails': '',
    'loan_alert_sla_hours': '24',
    'loan_alert_reminder_hours': '12',
    'loan_alert_escalate_hours': '48',
    'app_base_url': '',
}

_DEFAULT_SETTINGS.update(loan_limits.DEFAULTS)

_EDITABLE_SETTING_KEYS = set(_DEFAULT_SETTINGS) | {
    'coop_short_name',
    'member_prefix',
    'coop_logo',
    'active_gateway',
    'paystack_public_key',
    'flutterwave_public_key',
    'subscription_expiry',
    'subscription_per_user_fee',
    'subscription_email',
    'interest_method_regular',
    'interest_method_housing',
    'interest_method_emergency',
    'interest_method_asset',
    'interest_method_school_fees',
    'interest_school_fees',
    'support_phone',
    'support_email',
    'office_address',
    'whatsapp_number',
    'sms_enabled',
    'sms_provider',
    'sms_sender_id',
    'sms_username',
    'sms_country_code',
    'password_min_length',
    'password_require_upper',
    'password_require_lower',
    'password_require_number',
    'password_require_special',
}

_PROTECTED_SETTING_KEYS = {
    'csrf_token',
    'paystack_secret_key',
    'flutterwave_secret_key',
    'flutterwave_webhook_hash',
    'resend_api_key',
    'brevo_api_key',
    'smtp_pass',
    'sms_api_key',
}

_PASSWORD_POLICY_BOOLEAN_KEYS = {
    'password_require_upper',
    'password_require_lower',
    'password_require_number',
    'password_require_special',
}

# Switches on the Loan Settings tab. An unchecked box is not submitted at all,
# so they are written explicitly from the form group rather than the value loop.
_LOAN_ALERT_BOOLEAN_KEYS = {
    'loan_alert_enabled',
    'loan_alert_attach_pdf',
}

# Settings an admin must be able to blank out again (the generic loop skips
# empty values so a stray empty field cannot wipe a configured value).
_CLEARABLE_SETTING_KEYS = {
    'loan_alert_extra_emails',
    'app_base_url',
}


def _format_last_login(value):
    """Present a stored last_login (datetime on Postgres, ISO string on SQLite)
    as a clean 'YYYY-MM-DD HH:MM', or 'Never' if the user has not logged in."""
    if not value:
        return 'Never'
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M')
    try:
        return datetime.fromisoformat(str(value)).strftime('%Y-%m-%d %H:%M')
    except (TypeError, ValueError):
        return str(value)[:16]


def _sms_log_rows(db, limit=30):
    """The most recent SMS attempts, so an admin can see what credit bought."""
    try:
        return db.execute('''
            SELECT s.id, s.msisdn, s.purpose, s.body, s.status, s.error,
                   s.provider_ref, s.created_at,
                   m.member_number, m.first_name, m.last_name
            FROM sms_log s
            LEFT JOIN members m ON m.id = s.member_id
            ORDER BY s.id DESC
            LIMIT ?
        ''', (limit,)).fetchall()
    except Exception:
        return []


def _mobile_device_rows(db):
    rows = db.execute('''
        SELECT
            d.id,
            d.user_id,
            d.member_id,
            d.platform,
            d.push_token,
            d.device_name,
            d.enabled,
            d.last_seen_at,
            d.created_at,
            u.username,
            u.full_name AS user_full_name,
            u.email AS user_email,
            m.member_number,
            m.first_name,
            m.last_name
        FROM mobile_devices d
        LEFT JOIN users u ON u.id = d.user_id
        LEFT JOIN members m ON m.id = d.member_id
        ORDER BY COALESCE(d.last_seen_at, d.created_at) DESC, d.id DESC
    ''').fetchall()
    devices = []
    for row in rows:
        member_name = ''
        if row['first_name'] or row['last_name']:
            member_name = f"{row['first_name'] or ''} {row['last_name'] or ''}".strip()
        devices.append({
            'id': row['id'],
            'user_id': row['user_id'],
            'member_id': row['member_id'],
            'platform': row['platform'] or 'unknown',
            'push_token': row['push_token'] or '',
            'token_tail': (row['push_token'] or '')[-18:],
            'device_name': row['device_name'] or 'Mobile device',
            'enabled': row['enabled'] if row['enabled'] is not None else 1,
            'last_seen_at': _format_last_login(row['last_seen_at']),
            'created_at': _format_last_login(row['created_at']),
            'username': row['username'] or '',
            'user_full_name': row['user_full_name'] or row['username'] or '',
            'user_email': row['user_email'] or '',
            'member_number': row['member_number'] or '',
            'member_name': member_name,
        })
    return devices


def _upsert_setting(db, key, value, description=None):
    existing = db.execute('SELECT id FROM settings WHERE key = ?', (key,)).fetchone()
    if existing:
        db.execute('UPDATE settings SET value = ? WHERE key = ?', (value, key))
    else:
        db.execute(
            'INSERT INTO settings (key, value, description) VALUES (?, ?, ?)',
            (key, value, description or f'Setting for {key}')
        )


def _issue_account_setup_link(db, user):
    token, token_hash = generate_account_setup_token()
    db.execute(
        'UPDATE account_setup_tokens SET used_at = ? WHERE user_id = ? AND used_at IS NULL',
        (datetime.now(), user['id'])
    )
    db.execute('''
        INSERT INTO account_setup_tokens
            (user_id, token_hash, purpose, expires_at)
        VALUES (?, ?, 'admin_reissue', ?)
    ''', (user['id'], token_hash, datetime.now() + timedelta(hours=24)))
    return url_for('auth.setup_password', token=token, _external=True)


def _setting_map(db):
    return {r['key']: r['value'] for r in db.execute('SELECT key, value FROM settings').fetchall()}


def _truthy(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _system_readiness(db):
    rows = _setting_map(db)
    checks = []

    try:
        db.execute('SELECT 1').fetchone()
        counts = {
            'members': db.execute('SELECT COUNT(*) FROM members').fetchone()[0],
            'users': db.execute('SELECT COUNT(*) FROM users').fetchone()[0],
            'savings': db.execute('SELECT COUNT(*) FROM savings').fetchone()[0],
            'loans': db.execute('SELECT COUNT(*) FROM loans').fetchone()[0],
            'journal_entries': db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0],
            'audit_log': db.execute('SELECT COUNT(*) FROM audit_log').fetchone()[0],
        }
        checks.append({
            'key': 'database',
            'label': 'Database',
            'status': 'ok',
            'detail': 'PostgreSQL' if USE_POSTGRES else 'SQLite',
            'meta': counts,
        })
    except Exception as exc:
        checks.append({
            'key': 'database',
            'label': 'Database',
            'status': 'fail',
            'detail': f'Connection/query failed: {exc}',
            'meta': {},
        })

    mail_enabled = _truthy(rows.get('mail_enabled'))
    has_brevo = bool(rows.get('brevo_api_key'))
    has_resend = bool(rows.get('resend_api_key'))
    has_smtp = bool(rows.get('smtp_host') and rows.get('smtp_user') and rows.get('smtp_pass'))
    mail_status = 'ok' if mail_enabled and (has_brevo or has_resend or has_smtp) else 'warn'
    checks.append({
        'key': 'email',
        'label': 'Outgoing Email',
        'status': mail_status,
        'detail': 'Configured' if mail_status == 'ok' else 'Enable mail and configure Brevo, Resend, or SMTP.',
        'meta': {
            'enabled': mail_enabled,
            'brevo_api': has_brevo,
            'resend_api': has_resend,
            'smtp': has_smtp,
        },
    })

    gateway = rows.get('active_gateway') or 'paystack'
    if gateway == 'flutterwave':
        payment_ok = bool(rows.get('flutterwave_public_key') and rows.get('flutterwave_secret_key'))
    else:
        payment_ok = bool(rows.get('paystack_public_key') and rows.get('paystack_secret_key'))
    checks.append({
        'key': 'payments',
        'label': 'Payments',
        'status': 'ok' if payment_ok else 'warn',
        'detail': f'{gateway.title()} configured' if payment_ok else f'{gateway.title()} keys are incomplete.',
        'meta': {'active_gateway': gateway},
    })

    checks.append({
        'key': 'backup',
        'label': 'Backup Posture',
        'status': 'ok' if USE_POSTGRES else 'warn',
        'detail': (
            'Railway PostgreSQL should be covered by provider backups plus periodic export drills.'
            if USE_POSTGRES else
            'Local SQLite is not production-safe; use Railway PostgreSQL for client data.'
        ),
        'meta': {'backend': 'postgres' if USE_POSTGRES else 'sqlite'},
    })

    overall = 'fail' if any(c['status'] == 'fail' for c in checks) else (
        'warn' if any(c['status'] == 'warn' for c in checks) else 'ok'
    )
    return {
        'overall': overall,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'checks': checks,
    }


@admin_panel.route('/settings')
@login_required
@role_required('admin')
def settings():
    db = get_db()
    try:
        settings_rows = db.execute('SELECT key, value FROM settings').fetchall()
        settings_dict = {row['key']: row['value'] for row in settings_rows}
        for key, default_value in _DEFAULT_SETTINGS.items():
            settings_dict.setdefault(key, default_value)

        users = db.execute(
            '''
            SELECT id, username, full_name, email, role, is_active, last_login,
                   is_super_admin, must_change_password, two_factor_enabled
            FROM users
            ORDER BY id
            '''
        ).fetchall()
        # Check if the currently logged-in user is a super admin
        me_row = db.execute('SELECT is_super_admin FROM users WHERE id = ?', (current_user.id,)).fetchone()
        current_is_super = bool(me_row and me_row['is_super_admin'])
        user_list = [
            {
                'id':             u['id'],
                'username':       u['username'],
                'full_name':      u['full_name'] or u['username'],
                'email':          u['email'] or '',
                'role':           u['role'],
                'is_active':      u['is_active'] if u['is_active'] is not None else 1,
                'last_login':     _format_last_login(u['last_login']),
                'is_super_admin': bool(u['is_super_admin'] if 'is_super_admin' in u.keys() else 0),
                'must_change_password': bool(u['must_change_password'] if 'must_change_password' in u.keys() else 0),
                'two_factor_enabled': bool(u['two_factor_enabled'] if 'two_factor_enabled' in u.keys() else 0),
            }
            for u in users
        ]

        audit_logs = db.execute(
            'SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT 100'
        ).fetchall()
        readiness = _system_readiness(db)
        mobile_devices = _mobile_device_rows(db)
        sms_log = _sms_log_rows(db)

        return render_template('admin/settings.html',
                               settings=settings_dict,
                               system_users=user_list,
                               mobile_devices=mobile_devices,
                               sms_log=sms_log,
                               current_is_super=current_is_super,
                               audit_logs=audit_logs,
                               backup_history=[],
                               readiness=readiness,
                               datetime=datetime)
    except Exception as e:
        flash(f'Error loading settings: {str(e)}', 'danger')
        return render_template('admin/settings.html',
                               settings=_DEFAULT_SETTINGS,
                               system_users=[],
                               mobile_devices=[],
                               sms_log=[],
                               current_is_super=False,
                               audit_logs=[],
                               backup_history=[],
                               readiness={'overall': 'fail', 'generated_at': '', 'checks': []},
                               datetime=datetime)


@admin_panel.route('/settings/update', methods=['POST'])
@login_required
@role_required('admin')
def update_settings():
    db = get_db()

    if 'coop_logo' in request.files:
        logo = request.files['coop_logo']
        if logo and logo.filename:
            ok, err = validate_image(logo)
            if not ok:
                flash(f'Logo not saved: {err}', 'warning')
            else:
                # Store the logo in the database as a compact data URI so it
                # persists across container rebuilds (static/uploads is ephemeral).
                try:
                    _upsert_setting(db, 'coop_logo', logo_data_uri(logo))
                except Exception as exc:
                    flash(f'Logo not saved: could not process image ({exc}).', 'warning')

    try:
        loan_limits.validate_settings(request.form)
        updated = 0
        ignored = []
        settings_group = request.form.get('_settings_group', '')
        if settings_group == 'password_policy':
            for key in _PASSWORD_POLICY_BOOLEAN_KEYS:
                _upsert_setting(db, key, '1' if request.form.get(key) == '1' else '0')
                updated += 1
        if settings_group == 'loan_alerts':
            for key in _LOAN_ALERT_BOOLEAN_KEYS:
                _upsert_setting(db, key, '1' if request.form.get(key) == '1' else '0')
                updated += 1
        for key, value in request.form.items():
            if key == '_settings_group':
                continue
            if key in _PROTECTED_SETTING_KEYS:
                continue
            if key in _PASSWORD_POLICY_BOOLEAN_KEYS or key in _LOAN_ALERT_BOOLEAN_KEYS:
                continue
            if key not in _EDITABLE_SETTING_KEYS:
                ignored.append(key)
                continue
            if not value and key not in _CLEARABLE_SETTING_KEYS:
                continue
            _upsert_setting(db, key, value)
            updated += 1
        db.commit()
        audit(db, 'UPDATE_SETTINGS', 'settings', f'System settings updated ({updated} keys)')
        if ignored:
            flash(f'Ignored unsupported setting keys: {", ".join(sorted(set(ignored)))}', 'warning')
        flash('Settings saved successfully!', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error saving settings: {str(e)}', 'danger')

    return redirect(url_for('admin_panel.settings'))


@admin_panel.route('/settings/security', methods=['POST'])
@login_required
@role_required('admin')
def update_security_settings():
    db = get_db()
    require = '1' if request.form.get('require_2fa') == '1' else '0'
    _upsert_setting(db, 'require_2fa', require)
    audit(db, 'UPDATE_SECURITY_SETTINGS', 'settings',
          f'2FA enforcement turned {"on" if require == "1" else "off"}')
    db.commit()
    if require == '1':
        flash('Two-factor authentication is now required for all staff (admin, '
              'treasurer, secretary, exco). They will be asked to set it up on '
              'their next page load.', 'success')
    else:
        flash('Two-factor authentication is now optional for staff.', 'success')
    return redirect(url_for('admin_panel.settings'))


@admin_panel.route('/settings/reconcile-savings', methods=['POST'])
@login_required
@role_required('admin')
def reconcile_savings():
    """Resync every member's cached total_savings with the savings ledger."""
    db = get_db()
    try:
        corrected = reconcile_member_savings(db)
        db.commit()
        audit(db, 'RECONCILE_SAVINGS', 'members',
              f'Reconciled member savings balances; {corrected} corrected')
        if corrected:
            flash(f'Savings balances reconciled — {corrected} member(s) corrected.', 'success')
        else:
            flash('Savings balances reconciled — all members were already in sync.', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error reconciling savings: {e}', 'danger')
    return redirect(url_for('admin_panel.settings') + '#system')


@admin_panel.route('/api/readiness')
@login_required
@role_required('admin')
def readiness_status():
    db = get_db()
    return jsonify(_system_readiness(db))


@admin_panel.route('/expenses')
@login_required
@role_required('admin', 'treasurer')
def expenses():
    db = get_db()
    all_expenses = db.execute('SELECT * FROM expenses ORDER BY date DESC').fetchall()
    total_expenses = db.execute('SELECT SUM(amount) FROM expenses').fetchone()[0] or 0
    return render_template('admin/expenses.html',
                           expenses=all_expenses,
                           total_expenses=total_expenses)


@admin_panel.route('/expenses/add', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'treasurer')
def add_expense():
    if request.method == 'POST':
        db = get_db()
        try:
            expense_number = f"EXP/{datetime.now().strftime('%Y%m%d')}/{random.randint(1000, 9999)}"
            db.execute('''
                INSERT INTO expenses (
                    expense_number, category, amount, description, vendor,
                    payment_method, date, recorded_by, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                expense_number,
                request.form['category'],
                finite_float(request.form['amount']),
                request.form['description'],
                request.form.get('vendor', ''),
                request.form['payment_method'],
                request.form.get('date', datetime.now()),
                current_user.id,
                request.form.get('notes', ''),
            ))
            cash_account = get_default_cash_account(db)
            post_journal_safe(db, f"Expense — {request.form['category']}", [
                {'account': OPERATING_EXPENSES, 'debit': finite_float(request.form['amount']),
                 'memo': request.form.get('description', '')},
                {'account': cash_account, 'credit': finite_float(request.form['amount'])},
            ], reference=expense_number, source_module='expenses', created_by=current_user.id)
            db.commit()
            audit(db, 'ADD_EXPENSE', 'expenses',
                  f"Recorded expense {expense_number} – ₦{finite_float(request.form['amount']):,.2f}")
            flash('Expense recorded successfully!', 'success')
        except Exception as e:
            db.rollback()
            flash(f'Error recording expense: {str(e)}', 'danger')
        return redirect(url_for('admin_panel.expenses'))
    return render_template('admin/add-expense.html')


@admin_panel.route('/revenue')
@login_required
@role_required('admin', 'treasurer')
def revenue():
    db = get_db()
    revenues = db.execute('SELECT * FROM revenue ORDER BY date DESC').fetchall()
    total_revenue = db.execute('SELECT SUM(amount) FROM revenue').fetchone()[0] or 0
    return render_template('admin/revenue.html', revenues=revenues, total_revenue=total_revenue)


@admin_panel.route('/revenue/add', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'treasurer')
def add_revenue():
    if request.method == 'POST':
        db = get_db()
        try:
            revenue_number = f"REV/{datetime.now().strftime('%Y%m%d')}/{random.randint(1000, 9999)}"
            db.execute('''
                INSERT INTO revenue (
                    revenue_number, category, amount, description, source,
                    date, received_by, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                revenue_number,
                request.form['category'],
                finite_float(request.form['amount']),
                request.form['description'],
                request.form.get('source', ''),
                request.form.get('date', datetime.now()),
                current_user.id,
                request.form.get('notes', ''),
            ))
            cash_account = get_default_cash_account(db)
            post_journal_safe(db, f"Revenue — {request.form['category']}", [
                {'account': cash_account, 'debit': finite_float(request.form['amount'])},
                {'account': FEE_INCOME, 'credit': finite_float(request.form['amount']),
                 'memo': request.form.get('description', '')},
            ], reference=revenue_number, source_module='revenue', created_by=current_user.id)
            db.commit()
            audit(db, 'ADD_REVENUE', 'revenue',
                  f"Recorded revenue {revenue_number} – ₦{finite_float(request.form['amount']):,.2f}")
            flash('Revenue recorded successfully!', 'success')
        except Exception as e:
            db.rollback()
            flash(f'Error recording revenue: {str(e)}', 'danger')
        return redirect(url_for('admin_panel.revenue'))
    return render_template('admin/add-revenue.html')


@admin_panel.route('/honorarium')
@login_required
@role_required('admin')
def honorarium():
    db = get_db()
    honorariums = db.execute('''
        SELECT h.*, u.username as paid_by_name
        FROM honorarium h
        LEFT JOIN users u ON h.paid_by = u.id
        ORDER BY h.date DESC
    ''').fetchall()
    return render_template('admin/honorarium.html', honorariums=honorariums)


@admin_panel.route('/honorarium/add', methods=['POST'])
@login_required
@role_required('admin')
def add_honorarium():
    db = get_db()
    try:
        db.execute('''
            INSERT INTO honorarium (
                recipient_id, recipient_name, amount, description, month, paid_by
            ) VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            request.form.get('recipient_id'),
            request.form['recipient_name'],
            finite_float(request.form['amount']),
            request.form['description'],
            request.form['month'],
            current_user.id,
        ))
        _hid = last_insert_id(db)
        cash_account = get_default_cash_account(db)
        post_journal_safe(db, f"Honorarium — {request.form.get('recipient_name', '')}", [
            {'account': HONORARIUM, 'debit': finite_float(request.form['amount']),
             'memo': request.form.get('recipient_name', '')},
            {'account': cash_account, 'credit': finite_float(request.form['amount'])},
        ], source_module='honorarium', source_id=_hid, created_by=current_user.id)
        db.commit()
        flash('Honorarium recorded successfully!', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error recording honorarium: {str(e)}', 'danger')
    return redirect(url_for('admin_panel.honorarium'))


@admin_panel.route('/api/member/<int:member_id>')
@login_required
@role_required('admin', 'treasurer', 'secretary', 'exco')
def get_member_api(member_id):
    from flask import jsonify
    db = get_db()
    member = db.execute(
        'SELECT id, first_name, last_name, member_number FROM members WHERE id = ?',
        (member_id,)
    ).fetchone()
    if member:
        # Use the savings ledger (source of truth) for the eligibility figures.
        savings_balance = member_savings_balance(db, member_id)
        return jsonify({
            'id': member['id'],
            'first_name': member['first_name'],
            'last_name': member['last_name'],
            'member_number': member['member_number'],
            'total_savings': savings_balance,
            'max_loan': loan_limits.eligible_amount(db, savings_balance),
            'loan_type_limits': loan_limits.member_limits(db, savings_balance),
            'max_loan_note': loan_limits.eligibility_note(db, savings_balance),
        })
    return jsonify({'error': 'Member not found'}), 404


@admin_panel.route('/api/add_user', methods=['POST'])
@login_required
@role_required('admin')
def add_user():
    db = get_db()
    try:
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        role = request.form.get('role', 'member').strip()
        full_name = request.form.get('full_name', username)
        email = request.form.get('email', '')

        if not username:
            flash('Username is required', 'danger')
            return redirect(url_for('admin_panel.settings') + '#users')

        # An officer sets their own password from an invitation, and nobody else
        # ever knows it. Every action is recorded against a name, and that is
        # only worth something if a name means one person — an admin who chose
        # the password could have done anything the officer is credited with.
        #
        # Without an email there is no way to invite them, so a password is
        # handed over instead and must be changed at first login.
        invite = bool(email)
        if invite:
            password = secrets.token_urlsafe(24)     # never shown to anybody
        else:
            if not password:
                flash('Give this user an email address so they can be invited, or set a '
                      'password to hand over in person.', 'danger')
                return redirect(url_for('admin_panel.settings') + '#users')
            ok, errors = validate_password_strength(password, db)
            if not ok:
                flash(' '.join(errors), 'danger')
                return redirect(url_for('admin_panel.settings') + '#users')

        existing = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
        if existing:
            flash(f'Username "{username}" already exists', 'danger')
            return redirect(url_for('admin_panel.settings') + '#users')

        password_hash = generate_password_hash(password)
        db.execute('''
            INSERT INTO users (username, password_hash, role, full_name, email, created_at,
                               must_change_password)
            VALUES (?, ?, ?, ?, ?, ?, 1)
        ''', (username, password_hash, role, full_name, email, datetime.now()))
        new_id = last_insert_id(db)
        audit(db, 'ADD_USER', 'users',
              f'Created {role} account "{username}"'
              + (' and invited them by email' if invite else ' with a handover password'))

        if not invite:
            db.commit()
            flash(f'User "{username}" created. Give them this password in person — they '
                  f'must change it the first time they log in.', 'success')
            return redirect(url_for('admin_panel.settings') + '#users')

        # The link is stored before the email is attempted, so a mail failure
        # leaves a working invitation the officer can resend rather than a user
        # nobody can get into.
        setup_url = _issue_account_setup_link(
            db, {'id': new_id, 'username': username, 'full_name': full_name, 'email': email})
        db.commit()
        try:
            send_member_onboarding_email(
                email,
                {'full_name': full_name or username, 'member_number': ''},
                username,
                setup_url,
                url_for('portal.profile', _external=True),
            )
            flash(f'User "{username}" created and invited at {email}. They set their own '
                  f'password from the link.', 'success')
        except Exception as mail_error:
            flash(f'User "{username}" was created, but the invitation email could not be '
                  f'sent ({mail_error}). Use "Send setup link" on their row to try again.',
                  'warning')
    except Exception as e:
        db.rollback()
        flash(f'Error creating user: {str(e)}', 'danger')
    return redirect(url_for('admin_panel.settings'))


@admin_panel.route('/api/edit_user/<int:user_id>', methods=['POST'])
@login_required
@role_required('admin')
def edit_user(user_id):
    db = get_db()
    try:
        full_name = request.form.get('full_name', '').strip()
        email     = request.form.get('email', '').strip()
        role      = request.form.get('role', '').strip()

        if not role:
            flash('Role is required.', 'danger')
            return redirect(url_for('admin_panel.settings') + '#users')

        # Prevent admin from removing their own admin role
        if user_id == current_user.id and role != 'admin':
            flash('You cannot change your own role.', 'danger')
            return redirect(url_for('admin_panel.settings') + '#users')

        # Super-admin protection: only a super admin can edit another super admin
        target = db.execute('SELECT is_super_admin FROM users WHERE id = ?', (user_id,)).fetchone()
        if target and target['is_super_admin']:
            me = db.execute('SELECT is_super_admin FROM users WHERE id = ?', (current_user.id,)).fetchone()
            if not (me and me['is_super_admin']):
                flash('Only a super admin can modify a super admin account.', 'danger')
                return redirect(url_for('admin_panel.settings') + '#users')

        db.execute(
            'UPDATE users SET session_version = COALESCE(session_version, 0) + 1, full_name = ?, email = ?, role = ? WHERE id = ?',
            (full_name, email, role, user_id)
        )
        db.commit()
        audit(db, 'UPDATE', 'users', f'Updated user id={user_id} role={role}')
        flash('User updated successfully.', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error updating user: {e}', 'danger')
    return redirect(url_for('admin_panel.settings') + '#users')


@admin_panel.route('/api/reset_user_password/<int:user_id>', methods=['POST'])
@login_required
@role_required('admin')
def reset_user_password(user_id):
    db = get_db()
    try:
        new_password = request.form.get('new_password', '').strip()
        force_change  = request.form.get('force_change', '0') == '1'

        ok, errors = validate_password_strength(new_password, db)
        if not ok:
            flash(' '.join(errors), 'danger')
            return redirect(url_for('admin_panel.settings') + '#users')

        db.execute(
            'UPDATE users SET session_version = COALESCE(session_version, 0) + 1, password_hash = ?, must_change_password = ? WHERE id = ?',
            (generate_password_hash(new_password), 1 if force_change else 0, user_id)
        )
        db.commit()
        user = db.execute('SELECT username FROM users WHERE id = ?', (user_id,)).fetchone()
        uname = user['username'] if user else str(user_id)
        audit(db, 'UPDATE', 'users', f'Admin reset password for user {uname}')
        flash(f'Password for "{uname}" has been reset successfully.', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error resetting password: {e}', 'danger')
    return redirect(url_for('admin_panel.settings') + '#users')


@admin_panel.route('/api/reset_user_2fa/<int:user_id>', methods=['POST'])
@login_required
@role_required('admin')
def reset_user_2fa(user_id):
    """Clear a user's two-factor setup so they can enrol again from scratch —
    the recovery path when someone loses their authenticator and backup codes."""
    from security import disable_user_2fa
    db = get_db()
    try:
        user = db.execute('SELECT username FROM users WHERE id = ?', (user_id,)).fetchone()
        uname = user['username'] if user else str(user_id)
        disable_user_2fa(db, user_id)
        db.commit()
        audit(db, 'RESET_2FA', 'users', f'Admin reset two-factor authentication for user {uname}')
        flash(f'Two-factor authentication for "{uname}" has been reset. '
              f'They will be asked to set it up again on their next sign-in '
              f'(if it is required for their role).', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error resetting two-factor authentication: {e}', 'danger')
    return redirect(url_for('admin_panel.settings') + '#users')


@admin_panel.route('/api/resend_setup_link/<int:user_id>', methods=['POST'])
@login_required
@role_required('admin')
def resend_setup_link(user_id):
    db = get_db()
    try:
        user = db.execute(
            'SELECT id, username, full_name, email, is_active FROM users WHERE id = ?',
            (user_id,)
        ).fetchone()
        if not user:
            flash('User not found.', 'danger')
            return redirect(url_for('admin_panel.settings') + '#users')
        if not user['email']:
            flash('Cannot send setup link because this user has no email address.', 'danger')
            return redirect(url_for('admin_panel.settings') + '#users')
        if not user['is_active']:
            flash('Cannot send setup link to an inactive user.', 'danger')
            return redirect(url_for('admin_panel.settings') + '#users')

        setup_url = _issue_account_setup_link(db, user)
        db.execute('UPDATE users SET must_change_password = 1 WHERE id = ?', (user_id,))
        audit(db, 'RESEND_SETUP_LINK', 'users', f'Reissued setup link for {user["username"]}')
        db.commit()
        send_member_onboarding_email(
            user['email'],
            {'full_name': user['full_name'] or user['username'], 'member_number': ''},
            user['username'],
            setup_url,
            url_for('portal.profile', _external=True),
        )
        flash(f'Setup link sent to "{user["username"]}".', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error sending setup link: {e}', 'danger')
    return redirect(url_for('admin_panel.settings') + '#users')


@admin_panel.route('/api/bulk_send_setup_links', methods=['POST'])
@login_required
@role_required('admin')
def bulk_send_setup_links():
    db = get_db()
    sent = 0
    failed = []

    try:
        users = db.execute('''
            SELECT id, username, full_name, email, is_active, must_change_password
            FROM users
            WHERE COALESCE(is_active, 1) = 1
              AND COALESCE(must_change_password, 0) = 1
              AND COALESCE(email, '') <> ''
            ORDER BY id
        ''').fetchall()

        if not users:
            flash('No active users are waiting for account setup links.', 'info')
            return redirect(url_for('admin_panel.settings') + '#users')

        for user in users:
            try:
                setup_url = _issue_account_setup_link(db, user)
                audit(db, 'BULK_SEND_SETUP_LINK', 'users', f'Reissued setup link for {user["username"]}')
                db.commit()
                send_member_onboarding_email(
                    user['email'],
                    {'full_name': user['full_name'] or user['username'], 'member_number': ''},
                    user['username'],
                    setup_url,
                    url_for('portal.profile', _external=True),
                )
                sent += 1
            except Exception as exc:
                db.rollback()
                failed.append(f'{user["username"]}: {exc}')

        if sent:
            flash(f'Setup links sent to {sent} user{"s" if sent != 1 else ""}.', 'success')
        if failed:
            flash(f'{len(failed)} setup link{"s" if len(failed) != 1 else ""} failed. Check logs/email settings.', 'warning')
    except Exception as e:
        db.rollback()
        flash(f'Error sending setup links: {e}', 'danger')

    return redirect(url_for('admin_panel.settings') + '#users')


@admin_panel.route('/api/revoke_setup_links/<int:user_id>', methods=['POST'])
@login_required
@role_required('admin')
def revoke_setup_links(user_id):
    db = get_db()
    try:
        user = db.execute('SELECT username FROM users WHERE id = ?', (user_id,)).fetchone()
        if not user:
            flash('User not found.', 'danger')
            return redirect(url_for('admin_panel.settings') + '#users')

        db.execute(
            'UPDATE account_setup_tokens SET used_at = ? WHERE user_id = ? AND used_at IS NULL',
            (datetime.now(), user_id)
        )
        audit(db, 'REVOKE_SETUP_LINKS', 'users', f'Revoked setup links for {user["username"]}')
        db.commit()
        flash(f'Outstanding setup links for "{user["username"]}" have been revoked.', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error revoking setup links: {e}', 'danger')
    return redirect(url_for('admin_panel.settings') + '#users')


@admin_panel.route('/api/toggle_super_admin/<int:user_id>', methods=['POST'])
@login_required
@role_required('admin')
def toggle_super_admin(user_id):
    """Grant or revoke super-admin status.  Only a super admin can do this."""
    db = get_db()
    me = db.execute('SELECT is_super_admin FROM users WHERE id = ?', (current_user.id,)).fetchone()
    if not (me and me['is_super_admin']):
        flash('Only a super admin can grant or revoke super admin status.', 'danger')
        return redirect(url_for('admin_panel.settings') + '#users')
    if user_id == current_user.id:
        flash('You cannot revoke your own super admin status.', 'danger')
        return redirect(url_for('admin_panel.settings') + '#users')
    try:
        target = db.execute('SELECT username, is_super_admin FROM users WHERE id = ?', (user_id,)).fetchone()
        if not target:
            flash('User not found.', 'danger')
            return redirect(url_for('admin_panel.settings') + '#users')
        new_val = 0 if target['is_super_admin'] else 1
        db.execute('UPDATE users SET session_version = COALESCE(session_version, 0) + 1, is_super_admin = ? WHERE id = ?', (new_val, user_id))
        db.commit()
        status = 'granted' if new_val else 'revoked'
        audit(db, 'UPDATE', 'users', f'Super admin status {status} for {target["username"]}')
        flash(f'Super admin status {status} for "{target["username"]}".', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error updating super admin: {e}', 'danger')
    return redirect(url_for('admin_panel.settings') + '#users')


@admin_panel.route('/api/toggle_user/<int:user_id>', methods=['POST'])
@login_required
@role_required('admin')
def toggle_user(user_id):
    if user_id == current_user.id:
        flash('You cannot disable your own account.', 'danger')
        return redirect(url_for('admin_panel.settings') + '#users')
    db = get_db()
    try:
        user = db.execute('SELECT username, is_active FROM users WHERE id = ?', (user_id,)).fetchone()
        if not user:
            flash('User not found.', 'danger')
            return redirect(url_for('admin_panel.settings') + '#users')
        new_status = 0 if user['is_active'] else 1
        db.execute('UPDATE users SET session_version = COALESCE(session_version, 0) + 1, is_active = ? WHERE id = ?', (new_status, user_id))
        db.commit()
        action = 'enabled' if new_status else 'disabled'
        audit(db, 'UPDATE', 'users', f'Admin {action} user {user["username"]}')
        flash(f'User "{user["username"]}" has been {action}.', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error toggling user: {e}', 'danger')
    return redirect(url_for('admin_panel.settings') + '#users')


@admin_panel.route('/api/mobile_devices/<int:device_id>/test-push', methods=['POST'])
@login_required
@role_required('admin')
def test_mobile_device_push(device_id):
    db = get_db()
    try:
        device = db.execute('''
            SELECT d.*, u.username
            FROM mobile_devices d
            LEFT JOIN users u ON u.id = d.user_id
            WHERE d.id = ?
        ''', (device_id,)).fetchone()
        if not device:
            flash('Mobile device not found.', 'danger')
            return redirect(url_for('admin_panel.settings') + '#mobile-devices')
        if not (device['enabled'] if device['enabled'] is not None else 1):
            flash('This mobile device is revoked. Enable/register it again before testing push delivery.', 'warning')
            return redirect(url_for('admin_panel.settings') + '#mobile-devices')

        title = 'CoopMS test notification'
        message = 'If you received this, mobile push delivery is working for this device.'
        notify(db, device['user_id'], title, message, 'info', '/notifications')
        audit(db, 'TEST_MOBILE_PUSH', 'mobile', f"Sent test push to device #{device_id} ({device['username'] or device['user_id']})")
        db.commit()
        flash('Test push queued for the selected mobile device/user.', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error sending test push: {e}', 'danger')
    return redirect(url_for('admin_panel.settings') + '#mobile-devices')


@admin_panel.route('/api/mobile_devices/<int:device_id>/revoke', methods=['POST'])
@login_required
@role_required('admin')
def revoke_mobile_device(device_id):
    db = get_db()
    try:
        device = db.execute('''
            SELECT d.*, u.username
            FROM mobile_devices d
            LEFT JOIN users u ON u.id = d.user_id
            WHERE d.id = ?
        ''', (device_id,)).fetchone()
        if not device:
            flash('Mobile device not found.', 'danger')
            return redirect(url_for('admin_panel.settings') + '#mobile-devices')
        db.execute('UPDATE mobile_devices SET enabled = 0 WHERE id = ?', (device_id,))
        audit(db, 'REVOKE_MOBILE_DEVICE', 'mobile', f"Revoked mobile device #{device_id} ({device['username'] or device['user_id']})")
        db.commit()
        flash('Mobile device revoked. It will no longer receive push notifications.', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error revoking mobile device: {e}', 'danger')
    return redirect(url_for('admin_panel.settings') + '#mobile-devices')


@admin_panel.route('/api/test_db')
@login_required
@role_required('admin')
def test_db():
    from flask import jsonify
    try:
        db = get_db()
        db.execute('SELECT 1').fetchone()
        return jsonify({'success': True, 'message': 'Database connection successful'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@admin_panel.route('/settings/update-mail', methods=['POST'])
@login_required
@role_required('admin')
def update_mail_settings():
    """Save email settings (Resend API or SMTP) to the DB."""
    db = get_db()
    try:
        mail_enabled   = '1' if request.form.get('mail_enabled') else '0'
        mail_from      = request.form.get('mail_from', '').strip()
        resend_api_key = request.form.get('resend_api_key', '').strip()
        brevo_api_key  = request.form.get('brevo_api_key', '').strip()
        smtp_host      = request.form.get('smtp_host', '').strip()
        smtp_port      = request.form.get('smtp_port', '587').strip() or '587'
        smtp_user      = request.form.get('smtp_user', '').strip()
        smtp_pass      = request.form.get('smtp_pass', '').strip()

        updates = {
            'mail_enabled': mail_enabled,
            'mail_from':    mail_from,
            'smtp_host':    smtp_host,
            'smtp_port':    smtp_port,
            'smtp_user':    smtp_user,
        }
        if resend_api_key:
            updates['resend_api_key'] = resend_api_key
        if brevo_api_key:
            updates['brevo_api_key'] = brevo_api_key
        if smtp_pass:
            updates['smtp_pass'] = smtp_pass  # blank → keep existing

        for key, val in updates.items():
            _upsert_setting(db, key, val, f'Email setting: {key}')

        db.commit()
        audit(db, 'UPDATE_MAIL_SETTINGS', 'settings', 'Email settings updated')
        flash('Email settings saved successfully!', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error saving email settings: {str(e)}', 'danger')
    return redirect(url_for('admin_panel.settings') + '#mail')


@admin_panel.route('/settings/test-mail', methods=['POST'])
@login_required
@role_required('admin')
def test_mail():
    from flask import jsonify
    from email_service import send_email

    recipient = request.form.get('recipient', '').strip()
    if not recipient:
        return jsonify({'success': False, 'error': 'Recipient email is required'})

    db = get_db()
    enabled = db.execute("SELECT value FROM settings WHERE key = 'mail_enabled'").fetchone()
    if not enabled or enabled['value'] != '1':
        return jsonify({'success': False,
                        'error': 'Email is disabled. Enable it and save first.'})

    html = (
        '<h2 style="color:#1a3a6c">Test Email</h2>'
        '<p>This is a test email from your cooperative management system, powered by CoopMS.</p>'
        '<p>If you received this, your outgoing email provider is configured correctly.</p>'
        f'<hr><small>Sent at {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</small>'
    )
    ok = send_email(recipient, 'Test Email', html)
    if ok:
        audit(db, 'TEST_MAIL', 'settings', f'Test email sent to {recipient}')
        return jsonify({'success': True, 'message': f'Test email sent to {recipient}'})
    return jsonify({'success': False,
                    'error': 'Send failed. Check Resend sender verification or SMTP host, username, password/app password, and TLS/SSL settings.'})


@admin_panel.route('/settings/update-sms', methods=['POST'])
@login_required
@role_required('admin')
def update_sms_settings():
    """Save this cooperative's own SMS provider credentials.

    Each society buys and pays for its own provider account, so the key lives in
    this tenant's settings and is never shared. A blank key keeps the saved one.
    """
    from sms import PROVIDERS

    db = get_db()
    try:
        provider = (request.form.get('sms_provider', '') or 'termii').strip().lower()
        if provider not in PROVIDERS:
            flash(f'Unknown SMS provider "{provider}".', 'danger')
            return redirect(url_for('admin_panel.settings') + '#sms-settings')

        updates = {
            'sms_enabled':      '1' if request.form.get('sms_enabled') else '0',
            'sms_provider':     provider,
            'sms_sender_id':    request.form.get('sms_sender_id', '').strip(),
            'sms_username':     request.form.get('sms_username', '').strip(),
            'sms_country_code': re.sub(r'\D', '', request.form.get('sms_country_code', '')) or '234',
        }
        api_key = request.form.get('sms_api_key', '').strip()
        if api_key:
            updates['sms_api_key'] = api_key      # blank → keep existing

        for key, val in updates.items():
            _upsert_setting(db, key, val, f'SMS setting: {key}')
        db.commit()
        audit(db, 'UPDATE_SMS_SETTINGS', 'settings',
              f'SMS settings updated (provider {provider}, '
              f'{"on" if updates["sms_enabled"] == "1" else "off"})')
        flash('SMS settings saved successfully!', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error saving SMS settings: {str(e)}', 'danger')
    return redirect(url_for('admin_panel.settings') + '#sms-settings')


@admin_panel.route('/settings/test-sms', methods=['POST'])
@login_required
@role_required('admin')
def test_sms():
    """Send one real message so an admin can prove the credentials work."""
    from flask import jsonify
    from sms import send_sms, sms_config, get_provider, normalise_msisdn, looks_sendable

    recipient = request.form.get('recipient', '').strip()
    if not recipient:
        return jsonify({'success': False, 'error': 'A phone number is required'})

    db = get_db()
    cfg = sms_config(db)
    if str(cfg.get('sms_enabled', '')) != '1':
        return jsonify({'success': False,
                        'error': 'SMS is disabled. Turn it on and save first.'})
    provider = get_provider(cfg)
    if not provider or not provider.configured():
        return jsonify({'success': False,
                        'error': 'No API key saved for this provider. Save one first.'})

    msisdn = normalise_msisdn(recipient, cfg.get('sms_country_code'))
    if not looks_sendable(msisdn):
        return jsonify({'success': False, 'error': f'"{recipient}" is not a usable phone number'})

    text = (f'Test message from {_coop_name(db)} on CoopMS. '
            f'Your SMS setup is working ({datetime.now().strftime("%H:%M")}).')
    ok = send_sms(db, msisdn, text, purpose='test')
    if ok:
        audit(db, 'TEST_SMS', 'settings', f'Test SMS sent to {msisdn}')
        db.commit()
        return jsonify({'success': True, 'message': f'Test SMS sent to +{msisdn}'})
    row = db.execute(
        "SELECT error FROM sms_log WHERE purpose = 'test' ORDER BY id DESC LIMIT 1").fetchone()
    detail = (row['error'] if row and row['error'] else '') or ''
    return jsonify({'success': False,
                    'error': ('Send failed. Check the API key, sender ID and your provider '
                              'credit balance.' + (f' Provider said: {detail}' if detail else ''))})


# ── Subscription billing ──────────────────────────────────────────────────────

@admin_panel.route('/subscription')
@login_required
@role_required('admin', 'treasurer')
def subscription_page():
    from datetime import datetime, timedelta
    db = get_db()
    rows = {r['key']: r['value'] for r in db.execute('SELECT key, value FROM settings').fetchall()}

    expiry_str   = rows.get('subscription_expiry', '').strip()
    per_user_fee = int(rows.get('subscription_per_user_fee', '5000') or 5000)
    coop_email   = rows.get('subscription_email') or rows.get('email', '')
    coop_name    = rows.get('coop_name', 'Cooperative')
    pk           = rows.get('paystack_public_key', '')

    # Count active members to compute per-user fee
    try:
        member_count = db.execute(
            "SELECT COUNT(*) FROM members WHERE status = 'active'"
        ).fetchone()[0] or 0
    except Exception:
        member_count = 0

    # Total fee = active members × per_user_fee (minimum 1 member to avoid ₦0)
    total_fee = max(member_count, 1) * per_user_fee

    expiry_date = None
    days_left   = None
    is_active   = False

    if expiry_str:
        try:
            expiry_date = datetime.strptime(expiry_str, '%Y-%m-%d')
            days_left   = (expiry_date - datetime.now()).days
            is_active   = days_left > 0
        except Exception:
            pass
    else:
        is_active = True  # no billing configured

    # Pre-compute new expiry so the template never needs now() or timedelta
    base = expiry_date if (is_active and expiry_date) else datetime.now()
    new_expiry_display = (base + timedelta(days=365)).strftime('%d %b %Y')

    # Safe days_left — never negative for display
    days_left_safe = max(days_left, 0) if days_left is not None else None

    return render_template(
        'subscription.html',
        expiry_date=expiry_date,
        days_left=days_left,
        days_left_safe=days_left_safe,
        is_active=is_active,
        fee=total_fee,
        per_user_fee=per_user_fee,
        member_count=member_count,
        coop_email=coop_email,
        coop_name=coop_name,
        paystack_public_key=pk,
        new_expiry_display=new_expiry_display,
    )


@admin_panel.route('/subscription/callback')
@login_required
@role_required('admin', 'treasurer')
def subscription_callback():
    """Paystack redirects here after a subscription payment."""
    reference = request.args.get('reference', '').strip()
    if not reference:
        flash('Invalid payment reference.', 'danger')
        return redirect(url_for('admin_panel.subscription_page'))

    # Verify with Paystack
    db  = get_db()
    sk  = (db.execute("SELECT value FROM settings WHERE key='paystack_secret_key'").fetchone() or {}).get('value', '')
    if not sk:
        sk = os.environ.get('PAYSTACK_SECRET_KEY', '')

    verified = False
    amount_paid = 0
    try:
        import urllib.request as _ur, json as _json, ssl as _ssl
        req = _ur.Request(
            f'https://api.paystack.co/transaction/verify/{reference}',
            headers={'Authorization': f'Bearer {sk}', 'Accept': 'application/json'}
        )
        ctx = _ssl.create_default_context()
        with _ur.urlopen(req, context=ctx, timeout=10) as resp:
            data = _json.loads(resp.read())
        if data.get('status') and data['data'].get('status') == 'success':
            verified    = True
            amount_paid = data['data']['amount'] // 100  # kobo → naira
    except Exception as e:
        flash(f'Could not verify payment with Paystack: {e}', 'danger')
        return redirect(url_for('admin_panel.subscription_page'))

    if not verified:
        flash('Payment could not be verified. Please contact support.', 'danger')
        return redirect(url_for('admin_panel.subscription_page'))

    # ── Replay protection: each Paystack reference may only be applied once ──
    already = db.execute(
        "SELECT id FROM audit_log WHERE action = 'SUBSCRIPTION_RENEWED' AND data LIKE ?",
        (f'%{reference}%',)
    ).fetchone()
    if already:
        flash('This payment reference has already been applied to your subscription.', 'info')
        return redirect(url_for('admin_panel.subscription_page'))

    # ── Amount check: the payment must cover the fee actually due ──
    _rows = {r['key']: r['value'] for r in db.execute('SELECT key, value FROM settings').fetchall()}
    _per_user_fee = int(_rows.get('subscription_per_user_fee', '5000') or 5000)
    try:
        _member_count = db.execute(
            "SELECT COUNT(*) FROM members WHERE status = 'active'"
        ).fetchone()[0] or 0
    except Exception:
        _member_count = 0
    _expected_fee = max(_member_count, 1) * _per_user_fee
    if amount_paid < _expected_fee:
        flash(
            f'Payment of ₦{amount_paid:,} is less than the amount due '
            f'(₦{_expected_fee:,}). Subscription was not extended — please contact support.',
            'danger'
        )
        return redirect(url_for('admin_panel.subscription_page'))

    # Extend subscription by 1 year from today (or from current expiry if still active)
    from datetime import datetime, timedelta
    current_str = (db.execute("SELECT value FROM settings WHERE key='subscription_expiry'").fetchone() or {}).get('value', '')
    try:
        current_expiry = datetime.strptime(current_str, '%Y-%m-%d') if current_str else datetime.now()
        base = max(current_expiry, datetime.now())
    except Exception:
        base = datetime.now()

    new_expiry = (base + timedelta(days=365)).strftime('%Y-%m-%d')

    db.execute(
        "UPDATE settings SET value = ? WHERE key = 'subscription_expiry'",
        (new_expiry,)
    )
    # Also log as revenue
    from security import log_audit
    log_audit(db, current_user.id, current_user.username,
              'SUBSCRIPTION_RENEWED', 'billing',
              f'Subscription renewed via Paystack ref {reference}. '
              f'Amount: ₦{amount_paid:,}. New expiry: {new_expiry}',
              request.remote_addr, '')
    db.commit()

    flash(f'✅ Subscription renewed successfully! Active until {new_expiry}.', 'success')
    return redirect(url_for('admin_panel.subscription_page'))


# ── Task assignment: who may do what ──────────────────────────────────────────
#
# The offices are fixed by the bye-laws, but what each office may do in the app
# is not. These screens let the President reassign any permission to a role, or
# to one named officer, without a code change. See permissions.py.

def _officer_rows(db):
    """Staff accounts that can be assigned work, newest office first."""
    rows = db.execute(
        '''SELECT id, username, full_name, email, role, is_active, is_super_admin
           FROM users WHERE role IN ('admin', 'treasurer', 'secretary', 'exco')
           ORDER BY CASE role WHEN 'admin' THEN 0 WHEN 'treasurer' THEN 1
                             WHEN 'secretary' THEN 2 ELSE 3 END, username'''
    ).fetchall()
    customised = perms.officers_with_custom_access(db)
    officers = []
    for row in rows:
        officers.append({
            'id': row['id'],
            'username': row['username'],
            'full_name': row['full_name'] or row['username'],
            'email': row['email'] or '',
            'role': row['role'],
            'role_label': perms.ROLE_LABELS.get(row['role'], row['role'].title()),
            'is_active': bool(row['is_active'] if row['is_active'] is not None else 1),
            'is_super_admin': bool(row['is_super_admin']),
            'custom_count': customised.get(row['id'], 0),
        })
    return officers


@admin_panel.route('/task-assignment')
@login_required
@role_required('admin')
def task_assignment():
    """The role matrix: what a Treasurer, General Secretary or Exco member may do."""
    db = get_db()
    overrides = perms.role_permission_overrides(db)
    grid = {}
    for permission in perms.PERMISSIONS:
        key = permission['key']
        grid[key] = {}
        for role in perms.ASSIGNABLE_ROLES:
            default = perms.default_allowed(key, role)
            allowed = overrides.get((role, key), default)
            grid[key][role] = {
                'allowed': bool(allowed) and perms.assignable(key, role),
                'default': bool(default),
                'changed': (role, key) in overrides and bool(allowed) != default,
                'assignable': perms.assignable(key, role),
            }
    return render_template('admin/task-assignment.html',
                           groups=perms.permission_groups(),
                           roles=perms.ASSIGNABLE_ROLES,
                           role_labels=perms.ROLE_LABELS,
                           grid=grid,
                           officers=_officer_rows(db),
                           changed_count=len(overrides))


@admin_panel.route('/task-assignment/roles', methods=['POST'])
@login_required
@role_required('admin')
def update_role_permissions():
    """Save the role matrix. A box that is not ticked is a withdrawn permission,
    so every assignable cell is written, not only the ticked ones."""
    db = get_db()
    ticked = set(request.form.getlist('permission'))   # values are "role:key"
    changes = []
    try:
        for permission in perms.PERMISSIONS:
            key = permission['key']
            for role in perms.ASSIGNABLE_ROLES:
                if not perms.assignable(key, role):
                    continue
                allowed = f'{role}:{key}' in ticked
                if perms.role_allows(db, role, key) != allowed:
                    changes.append(f"{perms.ROLE_LABELS.get(role, role)} "
                                   f"{'granted' if allowed else 'lost'} {key}")
                perms.set_role_permission(db, role, key, allowed)
        audit(db, 'UPDATE_ROLE_PERMISSIONS', 'permissions',
              f"Role permissions updated ({len(changes)} change(s)): "
              f"{'; '.join(changes[:12]) or 'no change'}")
        db.commit()
        flash(f'Task assignment saved. {len(changes)} change(s) applied.'
              if changes else 'Task assignment saved — nothing changed.', 'success')
    except Exception as exc:
        db.rollback()
        flash(f'Could not save task assignment: {exc}', 'danger')
    return redirect(url_for('admin_panel.task_assignment'))


@admin_panel.route('/task-assignment/reset', methods=['POST'])
@login_required
@role_required('admin')
def reset_permissions():
    """Put every role back to the built-in defaults (per-officer overrides stay)."""
    db = get_db()
    try:
        perms.reset_role_permissions(db)
        audit(db, 'RESET_ROLE_PERMISSIONS', 'permissions',
              'Role permissions reset to the built-in defaults')
        db.commit()
        flash('All roles restored to their default duties.', 'success')
    except Exception as exc:
        db.rollback()
        flash(f'Could not reset task assignment: {exc}', 'danger')
    return redirect(url_for('admin_panel.task_assignment'))


@admin_panel.route('/task-assignment/officer/<int:user_id>')
@login_required
@role_required('admin')
def user_permissions(user_id):
    """One officer's duties: inherit from their office, or allow/deny by name."""
    db = get_db()
    user = db.execute(
        'SELECT id, username, full_name, email, role, is_active, is_super_admin '
        'FROM users WHERE id = ?', (user_id,)
    ).fetchone()
    if not user:
        flash('Staff account not found.', 'danger')
        return redirect(url_for('admin_panel.task_assignment'))
    matrix = perms.permission_matrix(db, user_id, user['role'])
    return render_template('admin/officer-permissions.html',
                           officer=user,
                           role_label=perms.ROLE_LABELS.get(user['role'], (user['role'] or '').title()),
                           groups=perms.permission_groups(),
                           matrix=matrix,
                           full_access=(user['role'] == perms.FULL_ACCESS_ROLE
                                        or bool(user['is_super_admin'])),
                           override_count=sum(1 for m in matrix.values() if m['override'] is not None))


@admin_panel.route('/task-assignment/officer/<int:user_id>', methods=['POST'])
@login_required
@role_required('admin')
def update_user_permissions(user_id):
    """Save one officer's overrides. Each permission posts inherit/allow/deny."""
    db = get_db()
    user = db.execute('SELECT id, username, role FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        flash('Staff account not found.', 'danger')
        return redirect(url_for('admin_panel.task_assignment'))
    try:
        changed = 0
        for permission in perms.PERMISSIONS:
            key = permission['key']
            choice = request.form.get(f'perm__{key}', 'inherit')
            if not perms.assignable(key, user['role']):
                continue
            allowed = {'allow': True, 'deny': False}.get(choice, None)
            before = perms.permission_matrix(db, user_id, user['role'])[key]['override']
            if before != allowed:
                changed += 1
            perms.set_user_permission(db, user_id, key, allowed, granted_by=current_user.id)
        audit(db, 'UPDATE_USER_PERMISSIONS', 'permissions',
              f"Duties updated for {user['username']} ({changed} change(s))")
        db.commit()
        flash(f"Duties saved for {user['username']}. {changed} change(s) applied."
              if changed else f"No change to {user['username']}'s duties.", 'success')
    except Exception as exc:
        db.rollback()
        flash(f'Could not save duties: {exc}', 'danger')
    return redirect(url_for('admin_panel.user_permissions', user_id=user_id))


@admin_panel.route('/task-assignment/officer/<int:user_id>/reset', methods=['POST'])
@login_required
@role_required('admin')
def reset_user_permissions(user_id):
    """Drop this officer's personal overrides — back to whatever their office allows."""
    db = get_db()
    user = db.execute('SELECT id, username FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        flash('Staff account not found.', 'danger')
        return redirect(url_for('admin_panel.task_assignment'))
    try:
        perms.clear_user_permissions(db, user_id)
        audit(db, 'RESET_USER_PERMISSIONS', 'permissions',
              f"Personal duty overrides cleared for {user['username']}")
        db.commit()
        flash(f"{user['username']} now follows their office's duties exactly.", 'success')
    except Exception as exc:
        db.rollback()
        flash(f'Could not reset duties: {exc}', 'danger')
    return redirect(url_for('admin_panel.user_permissions', user_id=user_id))
