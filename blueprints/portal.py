import json
import os
import random
import loan_limits
import secrets
from datetime import datetime, timedelta
from io import BytesIO
from types import SimpleNamespace

from flask import Blueprint, render_template, redirect, url_for, request, flash, make_response, jsonify, session
from flask_login import login_required, current_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from database import get_db, last_insert_id
from crypto import decrypt_member_sensitive_fields, encrypt_field, mask_member_sensitive_fields
from security import validate_password_strength
from utils import (finite_float, audit, notify_member, notify, compute_loan_schedule, METHOD_LABELS,
                   member_for_user, member_savings_balance, member_share_capital,
                   validate_image, member_has_minimum_membership, has_unpaid_loan_of_type)
import loan_workflow as lw
import loan_alerts as la
from loan_pdf import build_loan_application_pdf

portal = Blueprint('portal', __name__)
STAFF_ROLES = {'admin', 'treasurer', 'secretary', 'exco'}


# ── Helpers ──────────────────────────────────────────────────────────────────────

def _member_extras(member, db):
    """Augment a member row with computed fields the templates need."""
    d = dict(member)
    d = mask_member_sensitive_fields(d)
    d['full_name'] = f"{member['first_name']} {member['last_name']}"
    # Show the ledger balance (source of truth), not the cached column, so the
    # member always sees a correct figure even if the cache has drifted.
    ledger_balance     = member_savings_balance(db, member['id'])
    d['total_savings'] = ledger_balance
    d['loan_eligibility_amount'] = loan_limits.eligible_amount(db, ledger_balance)
    d['loan_type_limits'] = loan_limits.member_limits(db, ledger_balance)
    # Share capital carved from contributions (see share_capital_split); shown
    # separately so members see the 5% wasn't lost, just reclassified to equity.
    d['share_capital'] = member_share_capital(db, member['id'])
    d['total_contributions'] = round(ledger_balance + d['share_capital'], 2)
    keys = member.keys() if hasattr(member, 'keys') else d.keys()
    d['shares']      = d.get('shares') or 0
    d['shares_value'] = d['shares'] * 100   # ₦100 per share unit
    dj = member['date_joined']
    if isinstance(dj, str):
        try:
            d['date_joined'] = datetime.fromisoformat(dj.split('.')[0])
        except Exception:
            d['date_joined'] = datetime.now()
    dob = d.get('date_of_birth')
    if isinstance(dob, str) and dob:
        try:
            d['date_of_birth'] = datetime.fromisoformat(dob.split()[0])
        except Exception:
            pass
    completion = _profile_completion(d)
    d['profile_completion'] = completion
    d['profile_completion_percent'] = completion['percent']
    d['is_certified_member'] = completion['percent'] == 100
    return d


def _profile_completion(member):
    """Return profile readiness score and missing requirements for self-service onboarding."""
    requirements = [
        ('first_name', 'First name', 'Identity'),
        ('last_name', 'Last name', 'Identity'),
        ('email', 'Email address', 'Contact'),
        ('phone', 'Phone number', 'Contact'),
        ('date_of_birth', 'Date of birth', 'Identity'),
        ('address', 'Residential address', 'Location'),
        ('city', 'City or town', 'Location'),
        ('state', 'State', 'Location'),
        ('country', 'Country', 'Location'),
        ('occupation', 'Occupation', 'Employment'),
        ('bank_name', 'Bank name', 'Banking'),
        ('account_name', 'Account name', 'Banking'),
        ('account_number', 'Account number', 'Banking'),
        ('emergency_contact_name', 'Emergency contact name', 'Emergency contact'),
        ('emergency_contact_phone', 'Emergency contact phone', 'Emergency contact'),
        ('nominee_name', 'Nominee name', 'Beneficiary'),
        ('nominee_relationship', 'Nominee relationship', 'Beneficiary'),
        ('nominee_phone', 'Nominee phone', 'Beneficiary'),
    ]
    completed = []
    missing = []
    sections = {}
    for field, label, section in requirements:
        value = member.get(field)
        is_done = value is not None and str(value).strip() != ''
        item = {'field': field, 'label': label, 'section': section, 'complete': is_done}
        sections.setdefault(section, {'total': 0, 'done': 0})
        sections[section]['total'] += 1
        if is_done:
            completed.append(item)
            sections[section]['done'] += 1
        else:
            missing.append(item)
    percent = round((len(completed) / len(requirements)) * 100) if requirements else 100
    for section in sections.values():
        section['percent'] = round((section['done'] / section['total']) * 100) if section['total'] else 100
    return {
        'percent': percent,
        'completed': completed,
        'missing': missing,
        'sections': sections,
        'total': len(requirements),
        'done': len(completed),
        'ready_to_transact': percent == 100,
    }


def _interest_rates(db):
    rows = db.execute("SELECT key, value FROM settings WHERE key LIKE 'interest_%'").fetchall()
    r = {row['key']: row['value'] for row in rows}
    return {
        'Regular':        float(r.get('interest_regular',    11)),
        'Housing':        float(r.get('interest_housing',     9)),
        'Emergency':      float(r.get('interest_emergency',  10)),
        'Asset Purchase': float(r.get('interest_asset',      10)),
        'School Fees':    float(r.get('interest_school_fees', 9)),
    }


def _interest_methods(db):
    rows = db.execute("SELECT key, value FROM settings WHERE key LIKE 'interest_method_%'").fetchall()
    r = {row['key']: row['value'] for row in rows}
    return {
        'Regular':        r.get('interest_method_regular',    'reducing_annual'),
        'Housing':        r.get('interest_method_housing',    'reducing_annual'),
        'Emergency':      r.get('interest_method_emergency',  'reducing_annual'),
        'Asset Purchase': r.get('interest_method_asset',      'reducing_annual'),
        'School Fees':    r.get('interest_method_school_fees','flat'),
    }


def _parse_dt(val):
    if val is None:
        return datetime.now()
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val).split('.')[0].replace('T', ' '))
    except Exception:
        return datetime.now()


# ── Member Portal Dashboard ───────────────────────────────────────────────────────

@portal.route('/member/view-as-member', methods=['POST'])
@login_required
def view_as_member():
    db = get_db()
    if current_user.role == 'member':
        return redirect(url_for('portal.member_portal'))
    if current_user.role not in STAFF_ROLES:
        flash('Member view is only available to staff users linked to a member profile.', 'warning')
        return redirect(url_for('main.dashboard'))
    member = member_for_user(db)
    if not member:
        flash('No member profile is linked to this login email.', 'warning')
        return redirect(url_for('main.dashboard'))
    session['view_mode'] = 'member'
    audit(db, 'ENTER_MEMBER_VIEW', 'portal',
          f"User {current_user.id} entered member view for member {member['id']}")
    db.commit()
    flash('You are now viewing your member portal.', 'info')
    return redirect(url_for('portal.member_portal'))


@portal.route('/member/back-to-admin', methods=['POST'])
@login_required
def back_to_admin():
    if current_user.role in STAFF_ROLES:
        session.pop('view_mode', None)
        flash('Returned to admin view.', 'info')
        return redirect(url_for('main.dashboard'))
    return redirect(url_for('portal.member_portal'))


@portal.route('/member/portal')
@login_required
def member_portal():
    db     = get_db()
    member = member_for_user(db)
    if not member:
        flash('Your account is not linked to a member profile. Please contact the administrator.', 'warning')
        if current_user.role == 'member':
            return redirect(url_for('portal.member_link_required'))
        return redirect(url_for('main.dashboard'))

    savings = db.execute(
        'SELECT * FROM savings WHERE member_id = ? ORDER BY date DESC', (member['id'],)
    ).fetchall()
    loans = db.execute(
        'SELECT * FROM loans WHERE member_id = ? ORDER BY date_applied DESC', (member['id'],)
    ).fetchall()

    active_loans_raw = [l for l in loans if l['status'] == 'active']

    def _aug_portal_loan(l):
        d = dict(l)
        amt = l['amount'] or 0
        bal = l['balance'] or 0
        d['progress_percentage'] = round((amt - bal) / amt * 100, 1) if amt > 0 else 0
        d['next_payment_date'] = None
        return SimpleNamespace(**d)

    active_loans = [_aug_portal_loan(l) for l in active_loans_raw]

    unread_count = db.execute(
        'SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0',
        (current_user.id,)
    ).fetchone()[0]

    # Augment recent_savings with datetime objects
    recent_savings = []
    for s in savings[:5]:
        d = dict(s)
        d['date'] = _parse_dt(s['date'])
        recent_savings.append(SimpleNamespace(**d))

    try:
        _today = datetime.now().strftime('%Y-%m-%d')
        events = db.execute(
            "SELECT * FROM events WHERE is_active = 1 AND event_date >= ? "
            "ORDER BY event_date ASC LIMIT 3", (_today,)).fetchall()
    except Exception:
        events = []

    return render_template('member/portal.html',
                           member=_member_extras(member, db),
                           active_loans_total=sum(l.amount for l in active_loans),
                           active_loans_count=len(active_loans),
                           active_loans=active_loans,
                           recent_savings=recent_savings,
                           recent_loans=loans[:5],
                           events=events,
                           unread_count=unread_count)


@portal.route('/member/link-required')
@login_required
def member_link_required():
    db = get_db()
    member = member_for_user(db)
    if member:
        return redirect(url_for('portal.member_portal'))
    return render_template('member/link-required.html')


def _in_target_advance(db, member_id):
    """True if this member has a live Target Advance subscription."""
    try:
        from blueprints.ctas import ctas_enabled
        if not ctas_enabled(db):
            return False
        row = db.execute(
            "SELECT 1 FROM ctas_subscriptions WHERE member_id = ? "
            "AND status IN ('enrolled', 'scheduled', 'active_recovery') LIMIT 1",
            (member_id,)).fetchone()
        return row is not None
    except Exception:
        return False


@portal.route('/payment-preference', methods=['POST'])
@login_required
def set_payment_preference():
    """The member says what money sent to their account number is for."""
    from virtual_accounts import MEMBER_PREFERENCES, set_member_preference

    db = get_db()
    member = member_for_user(db)
    if not member:
        flash('Member profile not found.', 'warning')
        return redirect(url_for('main.dashboard'))

    choice = request.form.get('payment_preference', '')
    if choice == 'ctas' and not _in_target_advance(db, member['id']):
        flash('You are not currently on a Target Advance cycle.', 'warning')
        return redirect(url_for('portal.my_savings'))
    if not set_member_preference(db, member['id'], choice):
        flash('That is not one of the options.', 'danger')
        return redirect(url_for('portal.my_savings'))

    db.commit()
    audit(db, 'SET_PAYMENT_PREFERENCE', 'members',
          f"Member {member['id']} set transfers to pay for: {choice or 'cooperative default'}")
    db.commit()
    flash(f'Saved. Your transfers will go to: {MEMBER_PREFERENCES[choice]}.', 'success')
    return redirect(url_for('portal.my_savings'))


# ── My Savings ────────────────────────────────────────────────────────────────────

@portal.route('/my-savings')
@login_required
def my_savings():
    from collections import defaultdict
    db     = get_db()
    member = member_for_user(db)
    if not member:
        flash('Member profile not found.', 'warning')
        return redirect(url_for('main.dashboard'))

    # All savings oldest-first (needed for running balance)
    all_savings = db.execute(
        'SELECT * FROM savings WHERE member_id = ? ORDER BY date ASC',
        (member['id'],)
    ).fetchall()

    # ── Aggregate stats (always over full history) ─────────────────────────
    total_savings   = sum(float(s['amount'] or 0) for s in all_savings)
    total_late_fees = sum(float(s['late_fee'] or 0) for s in all_savings)
    total_principal = total_savings - total_late_fees
    current_year    = str(datetime.now().year)
    current_month   = datetime.now().strftime('%Y-%m')
    year_savings    = sum(float(s['amount'] or 0) for s in all_savings
                          if str(s['month']).startswith(current_year))
    month_savings   = sum(float(s['amount'] or 0) for s in all_savings
                          if s['month'] == current_month)
    year_pct   = round(year_savings  / total_savings * 100 if total_savings > 0 else 0, 1)
    mo_target  = float(member['monthly_savings'] or 5000)
    mo_pct     = round(month_savings / mo_target * 100 if mo_target > 0 else 0, 1)

    # Yearly breakdown table
    by_year = defaultdict(lambda: {'total': 0.0, 'late_fees': 0.0, 'count': 0})
    for s in all_savings:
        yr = str(s['month'])[:4]
        by_year[yr]['total']     += float(s['amount'] or 0)
        by_year[yr]['late_fees'] += float(s['late_fee'] or 0)
        by_year[yr]['count']     += 1
    yearly_summaries = [
        SimpleNamespace(year=yr, total=v['total'], late_fees=v['late_fees'],
                        net=v['total'] - v['late_fees'], count=v['count'])
        for yr, v in sorted(by_year.items(), reverse=True)
    ]

    # Milestones
    milestones   = [500_000, 1_000_000, 2_000_000, 5_000_000, 10_000_000]
    next_target  = next((m for m in milestones if m > total_savings), milestones[-1])
    next_milestone = SimpleNamespace(target=next_target, needed=max(0, next_target - total_savings))

    # Chart — last 12 months
    monthly_totals = defaultdict(float)
    for s in all_savings:
        monthly_totals[str(s['month'])[:7]] += float(s['amount'] or 0)
    sorted_months = sorted(monthly_totals.keys())[-12:]
    chart_labels  = sorted_months
    chart_data    = [round(monthly_totals[m], 2) for m in sorted_months]

    # ── Date-range & status filters ────────────────────────────────────────
    from_date_str  = request.args.get('from_date', '')
    to_date_str    = request.args.get('to_date',   '')
    status_filter  = request.args.get('status', '')
    page           = max(1, request.args.get('page', 1, type=int))

    filtered = list(all_savings)   # still oldest-first at this point

    if from_date_str:
        try:
            fd = datetime.strptime(from_date_str, '%Y-%m-%d')
            filtered = [s for s in filtered if _parse_dt(s['date']) >= fd]
        except ValueError:
            pass
    if to_date_str:
        try:
            td = datetime.strptime(to_date_str, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
            filtered = [s for s in filtered if _parse_dt(s['date']) <= td]
        except ValueError:
            pass
    if status_filter == 'late':
        filtered = [s for s in filtered if (s['late_fee'] or 0) > 0]
    elif status_filter == 'ontime':
        filtered = [s for s in filtered if (s['late_fee'] or 0) == 0]

    # ── Running balance (cumulative over full history up to each row) ──────
    # First pass: build running balance for ALL savings (unfiltered, oldest first)
    cumulative = 0.0
    balance_map = {}   # savings.id → running balance at that point
    for s in all_savings:
        cumulative += float(s['amount'] or 0)
        balance_map[s['id']] = round(cumulative, 2)

    # Augment filtered rows and reverse for newest-first display
    augmented = []
    for s in reversed(filtered):   # newest first
        d = dict(s)
        d['running_balance'] = balance_map[s['id']]
        d['date_parsed']     = _parse_dt(s['date'])
        augmented.append(SimpleNamespace(**d))

    # ── Filtered totals (for footer row) ──────────────────────────────────
    filt_principal  = sum(float(s['amount'] or 0) - float(s['late_fee'] or 0) for s in filtered)
    filt_late_fees  = sum(float(s['late_fee'] or 0) for s in filtered)
    filt_total      = filt_principal + filt_late_fees
    # Share capital: per-row portion is on each savings row; totals for the
    # statement footer (filtered) and the full-history figure.
    filt_share_capital  = sum(float(s['share_capital'] or 0) for s in filtered)
    total_share_capital = sum(float(s['share_capital'] or 0) for s in all_savings)

    # ── Pagination over filtered set ───────────────────────────────────────
    per_page    = 20
    total_rows  = len(augmented)
    total_pages = max(1, (total_rows + per_page - 1) // per_page)
    page        = min(page, total_pages)
    savings_paged = augmented[(page - 1) * per_page : page * per_page]

    # The member's own account number, and what they said their transfers pay for.
    virtual_account = None
    payment_choices = {}
    payment_preference = ''
    try:
        from virtual_accounts import (MEMBER_PREFERENCES, account_for_member,
                                      member_preference, va_config, va_enabled)
        if va_enabled(db):
            row = account_for_member(db, member['id'], va_config(db)['va_provider'])
            if row and row['status'] == 'active':
                virtual_account = row
                payment_preference = member_preference(db, member['id'])
                payment_choices = dict(MEMBER_PREFERENCES)
                # Only offer Target Advance to a member who is actually in it.
                if not _in_target_advance(db, member['id']):
                    payment_choices.pop('ctas', None)
    except Exception:
        pass

    pending_savings_change = db.execute('''
        SELECT id, current_amount, requested_amount, reason, requested_at
        FROM savings_change_requests
        WHERE member_id = ? AND status = 'pending'
        ORDER BY requested_at DESC, id DESC
        LIMIT 1
    ''', (member['id'],)).fetchone()

    return render_template('member/my-savings.html',
                           member=_member_extras(member, db),
                           virtual_account=virtual_account,
                           payment_choices=payment_choices,
                           payment_preference=payment_preference,
                           pending_savings_change=pending_savings_change,
                           savings=savings_paged,
                           total_savings=total_savings,
                           year_savings=year_savings,
                           year_percentage=year_pct,
                           month_savings=month_savings,
                           month_percentage=min(mo_pct, 100),
                           dividend_earned=0,
                           total_principal=filt_principal,
                           total_late_fees=filt_late_fees,
                           total_amount=filt_total,
                           filt_share_capital=filt_share_capital,
                           total_share_capital=total_share_capital,
                           yearly_summaries=yearly_summaries,
                           next_milestone=next_milestone,
                           from_date=from_date_str,
                           to_date=to_date_str,
                           status=status_filter,
                           page=page,
                           total_pages=total_pages,
                           total_rows=total_rows,
                           chart_labels=chart_labels,
                           chart_data=chart_data)


@portal.route('/saving-detail/<int:saving_id>')
@login_required
def saving_detail(saving_id):
    db      = get_db()
    member  = member_for_user(db)
    saving  = db.execute('SELECT * FROM savings WHERE id = ?', (saving_id,)).fetchone()
    if not saving or not member or saving['member_id'] != member['id']:
        flash('Record not found.', 'danger')
        return redirect(url_for('portal.my_savings'))
    return render_template('member/saving-detail.html',
                           saving=saving, member=_member_extras(member, db))


# ── My Loans ──────────────────────────────────────────────────────────────────────

@portal.route('/my-loans')
@login_required
def my_loans():
    db     = get_db()
    member = member_for_user(db)
    if not member:
        flash('Member profile not found.', 'warning')
        return redirect(url_for('main.dashboard'))

    loans_raw = db.execute(
        'SELECT * FROM loans WHERE member_id = ? ORDER BY date_applied DESC',
        (member['id'],)
    ).fetchall()

    # Count repayments per loan in a single query
    repayment_counts = {}
    try:
        for row in db.execute('''
            SELECT loan_id, COUNT(*) as cnt FROM repayments
            WHERE loan_id IN (SELECT id FROM loans WHERE member_id = ?)
            GROUP BY loan_id
        ''', (member['id'],)).fetchall():
            repayment_counts[row['loan_id']] = row['cnt']
    except Exception:
        pass

    today = datetime.now()

    def _aug(loan):
        d = dict(loan)
        tenure = max(loan['tenure'] or 1, 1)
        amt    = loan['amount']   or 0
        bal    = loan['balance']  or 0
        is_disbursed = bool(loan['status'] in ('active', 'completed') or loan['disbursement_date'])

        d['monthly_payment']    = round((loan['total_repayment'] or 0) / tenure, 2)
        d['account_balance']     = bal if is_disbursed else 0
        d['display_balance']     = d['account_balance']
        d['is_disbursed']        = is_disbursed
        d['progress_percentage'] = round((amt - bal) / amt * 100, 1) if is_disbursed and amt > 0 else 0
        d['payments_made']       = repayment_counts.get(loan['id'], 0)

        # Parse date fields to datetime objects for strftime in template
        d['date_applied']      = _parse_dt(d.get('date_applied'))      if d.get('date_applied')      else None
        d['date_approved']     = _parse_dt(d.get('date_approved'))      if d.get('date_approved')     else None
        d['completed_at']      = _parse_dt(d.get('completed_at'))       if d.get('completed_at')      else None
        d['disbursement_date'] = _parse_dt(d.get('disbursement_date'))  if d.get('disbursement_date') else None

        # Next expected payment date
        if d['disbursement_date']:
            paid = d['payments_made']
            d['next_payment_date'] = d['disbursement_date'] + timedelta(days=30 * (paid + 1))
            overdue_days = (today - d['next_payment_date']).days
            d['is_overdue']   = overdue_days > 0 and loan['status'] == 'active'
            d['days_overdue'] = max(0, overdue_days) if d['is_overdue'] else 0
        else:
            d['next_payment_date'] = None
            d['is_overdue']   = False
            d['days_overdue'] = 0

        return SimpleNamespace(**d)

    all_loans       = [_aug(l) for l in loans_raw]
    active_loans    = [l for l in all_loans if l.status == 'active']
    pending_loans   = [l for l in all_loans if l.status == 'pending']
    completed_loans = [l for l in all_loans if l.status == 'completed']
    booked_loans    = active_loans + completed_loans

    total_loans_taken   = sum(l.amount for l in booked_loans)
    outstanding_balance = sum(l.account_balance for l in active_loans)
    total_repaid        = total_loans_taken - outstanding_balance
    # Headroom left after what they already owe, against the same ceiling the
    # application will apply — never a figure the form would then reject.
    available_credit    = max(0, loan_limits.eligible_amount(db, member_savings_balance(db, member['id']))
                                 - outstanding_balance)
    repayment_pct       = round((total_repaid / total_loans_taken * 100) if total_loans_taken > 0 else 0, 1)

    return render_template('member/my-loans.html',
                           member=_member_extras(member, db),
                           all_loans=all_loans,
                           active_loans=active_loans,
                           pending_loans=pending_loans,
                           completed_loans=completed_loans,
                           total_loans_taken=total_loans_taken,
                           total_loans_count=len(all_loans),
                           outstanding_balance=outstanding_balance,
                           active_loans_count=len(active_loans),
                           pending_loans_count=len(pending_loans),
                           completed_loans_count=len(completed_loans),
                           total_repaid=total_repaid,
                           repayment_percentage=repayment_pct,
                           available_credit=available_credit)


@portal.route('/loan-detail/<int:loan_id>')
@login_required
def loan_detail(loan_id):
    db     = get_db()
    member = member_for_user(db)
    loan   = db.execute('SELECT * FROM loans WHERE id = ?', (loan_id,)).fetchone()
    if not loan or not member or loan['member_id'] != member['id']:
        flash('Loan not found.', 'danger')
        return redirect(url_for('portal.my_loans'))

    repayments_raw = db.execute(
        'SELECT * FROM repayments WHERE loan_id = ? ORDER BY date ASC', (loan_id,)
    ).fetchall()

    d = dict(loan)
    tenure = max(d.get('tenure') or 1, 1)
    amt    = d.get('amount') or 0
    rate   = d.get('interest_rate') or 0
    method = d.get('interest_method') or 'reducing_annual'
    is_disbursed = bool(d.get('status') in ('active', 'completed') or d.get('disbursement_date'))

    # Parse dates (DB column is approved_at, alias to date_approved for templates)
    d['date_applied']      = _parse_dt(d.get('date_applied'))      if d.get('date_applied')      else None
    d['date_approved']     = _parse_dt(d.get('approved_at'))       if d.get('approved_at')       else None
    d['disbursement_date'] = _parse_dt(d.get('disbursement_date'))  if d.get('disbursement_date') else None

    # Ensure numeric fields have safe defaults
    d['is_disbursed']       = is_disbursed
    d['disbursed_amount']   = (d.get('disbursed_amount') or 0) if is_disbursed else 0
    d['application_fee']    = d.get('application_fee')    or 0
    d['insurance_premium']  = d.get('insurance_premium')  or 0
    d['total_repayment']    = d.get('total_repayment')    or 0
    d['account_balance']    = (d.get('balance') or 0) if is_disbursed else 0
    d['balance']            = d['account_balance']

    # Compute schedule using the stored interest method
    mp, total_rep, raw_schedule = compute_loan_schedule(amt, rate, tenure, method)
    d['monthly_payment']  = mp
    d['interest_method']  = method
    d['method_label']     = METHOD_LABELS.get(method, method)

    # Build schedule with due dates and paid/overdue flags
    schedule   = []
    start_date = d['disbursement_date'] or datetime.now()
    today      = datetime.now()
    paid_count = len(repayments_raw) if is_disbursed else 0

    for row in raw_schedule:
        i   = row['month']
        due = start_date + timedelta(days=30 * i)
        is_paid    = is_disbursed and i <= paid_count
        is_overdue = is_disbursed and not is_paid and due < today
        schedule.append(SimpleNamespace(
            month=i, due_date=due,
            payment=row['payment'],
            principal=row['principal'],
            interest=row['interest'],
            balance=row['balance'],
            paid=is_paid, overdue=is_overdue,
        ))

    # Next payment date
    next_unpaid = next((s for s in schedule if not s.paid), None)
    d['next_payment_date']   = next_unpaid.due_date if is_disbursed and next_unpaid else None
    d['progress_percentage'] = round(paid_count / tenure * 100, 1) if is_disbursed else 0

    # Payments with datetime objects
    payments = []
    for r in repayments_raw:
        rd = dict(r)
        rd['date']          = _parse_dt(rd.get('date')) if rd.get('date') else datetime.now()
        rd['principal_paid'] = rd.get('principal_paid') or 0
        rd['interest_paid']  = rd.get('interest_paid')  or 0
        rd['penalty_paid']   = rd.get('penalty_paid')   or 0
        payments.append(SimpleNamespace(**rd))

    return render_template('member/loan-detail.html',
                           loan=d, schedule=schedule,
                           payments=payments, repayments=payments,
                           member=_member_extras(member, db))


# ── Guarantor Requests ────────────────────────────────────────────────────────────

@portal.route('/loan-detail/<int:loan_id>/application.pdf')
@login_required
def my_loan_application_pdf(loan_id):
    """Let a member download their own signed application — the same document
    the management committee receives."""
    db     = get_db()
    member = member_for_user(db)
    loan   = db.execute('SELECT id, member_id FROM loans WHERE id = ?', (loan_id,)).fetchone()
    if not loan or not member or loan['member_id'] != member['id']:
        flash('Loan not found.', 'danger')
        return redirect(url_for('portal.my_loans'))
    pdf_bytes, filename = build_loan_application_pdf(db, loan_id)
    if not pdf_bytes:
        flash('Could not build your application PDF. Please contact the office.', 'danger')
        return redirect(url_for('portal.loan_detail', loan_id=loan_id))
    response = make_response(pdf_bytes)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'inline; filename={filename}'
    return response


@portal.route('/loan-detail/<int:loan_id>/withdraw', methods=['POST'])
@login_required
def withdraw_loan_application(loan_id):
    db = get_db()
    member = member_for_user(db)
    if not member:
        flash('Member profile not found.', 'warning')
        return redirect(url_for('main.dashboard'))

    reason = request.form.get('withdrawal_reason', '').strip()
    loan = db.execute(
        'SELECT * FROM loans WHERE id = ? AND member_id = ?',
        (loan_id, member['id'])
    ).fetchone()
    if not loan:
        flash('Loan application not found.', 'danger')
        return redirect(url_for('portal.my_loans'))
    if loan['status'] != 'pending':
        flash('Only pending loan applications can be withdrawn before final approval or disbursement.', 'warning')
        return redirect(url_for('portal.loan_detail', loan_id=loan_id))

    now = datetime.now()
    try:
        db.execute('''
            UPDATE loans
               SET status = 'withdrawn',
                   approval_stage = 'withdrawn',
                   withdrawn_at = ?,
                   withdrawn_by = ?,
                   withdrawal_reason = ?
             WHERE id = ?
        ''', (now, current_user.id, reason or 'Withdrawn by applicant', loan_id))
        db.execute(
            "UPDATE loan_guarantors SET status = 'withdrawn', responded_at = ? "
            "WHERE loan_id = ? AND status = 'pending'",
            (now, loan_id)
        )
        lw.record_action(db, loan_id, 'withdrawn', 'withdrawn',
                         acted_by=current_user.id,
                         acted_by_name=current_user.username,
                         comment=reason or 'Applicant withdrew the pending application')
        audit(db, 'LOAN_APPLICATION_WITHDRAWN', 'loans',
              f"Member {member['id']} withdrew loan {loan['loan_number']}: {reason or 'No reason supplied'}")
        for u in db.execute(
            "SELECT id FROM users WHERE role IN ('admin', 'treasurer', 'secretary') "
            "AND COALESCE(is_active, 1) = 1"
        ).fetchall():
            notify(db, u['id'], 'Loan Application Withdrawn',
                   f"{member['first_name']} {member['last_name']} withdrew loan application "
                   f"{loan['loan_number']}.", 'info', url_for('loans.loan_detail', loan_id=loan_id))
        db.commit()
        flash('Your loan application has been withdrawn. No loan account was created and no ledger entry was posted.', 'success')
    except Exception as exc:
        db.rollback()
        flash(f'Unable to withdraw application: {exc}', 'danger')
    return redirect(url_for('portal.loan_detail', loan_id=loan_id))


@portal.route('/my-guarantor-requests')
@login_required
def my_guarantor_requests():
    db = get_db()
    member = member_for_user(db)
    if not member:
        flash('Member profile not found.', 'warning')
        return redirect(url_for('main.dashboard'))
    reqs = db.execute('''
        SELECT lg.id AS gid, lg.status, lg.requested_at, lg.responded_at,
               l.id AS loan_id, l.loan_number, l.amount, l.purpose, l.status AS loan_status,
               am.first_name AS app_first, am.last_name AS app_last, am.member_number AS app_number
        FROM loan_guarantors lg
        JOIN loans l ON l.id = lg.loan_id
        JOIN members am ON am.id = l.member_id
        WHERE lg.member_id = ? ORDER BY lg.id DESC
    ''', (member['id'],)).fetchall()
    return render_template('member/guarantor-requests.html', requests=reqs)


@portal.route('/my-guarantor-requests/<int:gid>/<action>', methods=['POST'])
@login_required
def respond_guarantor(gid, action):
    db = get_db()
    member = member_for_user(db)
    if not member:
        return redirect(url_for('main.dashboard'))
    g = db.execute('SELECT * FROM loan_guarantors WHERE id = ? AND member_id = ?',
                   (gid, member['id'])).fetchone()
    if not g or g['status'] != 'pending':
        flash('Request not found or already answered.', 'warning')
        return redirect(url_for('portal.my_guarantor_requests'))
    new_status = 'accepted' if action == 'accept' else 'declined'
    db.execute("UPDATE loan_guarantors SET status = ?, responded_at = ? WHERE id = ?",
               (new_status, datetime.now(), gid))
    loan = db.execute('SELECT * FROM loans WHERE id = ?', (g['loan_id'],)).fetchone()
    applicant = db.execute('SELECT * FROM members WHERE id = ?', (loan['member_id'],)).fetchone()
    if new_status == 'accepted':
        if lw.maybe_advance_from_guarantors(db, g['loan_id']):
            if applicant and applicant['email']:
                notify_member(db, applicant['email'], 'Guarantors Complete',
                              f"All guarantors accepted for loan {loan['loan_number']}. "
                              f"It is now awaiting Secretary review.", 'success', '/my-loans')
            la.notify_guarantors_complete(db, g['loan_id'])
    else:
        if applicant and applicant['email']:
            notify_member(db, applicant['email'], 'Guarantor Declined',
                          f"A guarantor declined your loan {loan['loan_number']}. "
                          f"Please arrange another guarantor or contact the office.", 'warning', '/my-loans')
    audit(db, 'GUARANTOR_RESPONSE', 'loans', f"Member {member['id']} {new_status} guarantor request {gid}")
    db.commit()
    flash(f'You have {new_status} the guarantor request.', 'success')
    return redirect(url_for('portal.my_guarantor_requests'))


# ── Loan Application ──────────────────────────────────────────────────────────────

@portal.route('/loan-schedule-preview')
@login_required
def loan_schedule_preview():
    """Live repayment schedule so a member can compare options before applying."""
    db = get_db()
    try:
        amount  = finite_float(request.args.get('amount', 0))
        tenure  = int(request.args.get('tenure', 0))
        purpose = request.args.get('purpose', 'Regular')
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Invalid input'})
    if amount <= 0 or tenure <= 0:
        return jsonify({'ok': False, 'error': 'Enter an amount and tenure.'})
    limit_error = loan_limits.application_error(db, purpose, amount, tenure)
    if limit_error:
        return jsonify({'ok': False, 'error': limit_error})
    rates   = _interest_rates(db)
    methods = _interest_methods(db)
    rate    = rates.get(purpose, rates['Regular'])
    method  = methods.get(purpose, 'reducing_annual')
    mp, total, schedule = compute_loan_schedule(amount, rate, tenure, method)
    return jsonify({
        'ok': True, 'monthly_payment': mp, 'total_repayment': total,
        'total_interest': round(total - amount, 2), 'rate': rate,
        'method': METHOD_LABELS.get(method, method), 'schedule': schedule,
    })


@portal.route('/apply-loan-member', methods=['GET', 'POST'])
@login_required
def apply_loan_member():
    db     = get_db()
    member = member_for_user(db)
    if not member:
        flash('Member profile not found. Please ensure your email is registered.', 'danger')
        return redirect(url_for('main.dashboard'))

    max_tenure_row = db.execute("SELECT value FROM settings WHERE key = 'max_tenure_months'").fetchone()
    max_tenure     = int(max_tenure_row['value']) if max_tenure_row else 18
    rates          = _interest_rates(db)
    methods        = _interest_methods(db)
    is_staff_member = bool((member['employee_id'] or '').strip()) if 'employee_id' in member.keys() else False

    if request.method == 'POST':
        amount  = finite_float(request.form.get('amount', 0))
        purpose = request.form.get('purpose', '').strip()
        tenure  = int(request.form.get('tenure', 0))

        if amount <= 0 or not purpose or tenure <= 0:
            flash('All fields are required and must be valid.', 'danger')
            return redirect(url_for('portal.apply_loan_member'))

        limit_error = loan_limits.application_error(db, purpose, amount, tenure)
        if limit_error:
            flash(limit_error, 'danger')
            return redirect(url_for('portal.apply_loan_member'))

        if purpose not in rates:
            flash('Select a valid loan type.', 'danger')
            return redirect(url_for('portal.apply_loan_member'))

        signature = request.form.get('signature_name', '').strip()
        payment_collateral_type = request.form.get('payment_collateral_type', '').strip()
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
        missing_ack = [message for field, message in required_acknowledgements.items()
                       if not request.form.get(field)]
        if missing_ack or not signature:
            flash(missing_ack[0] if missing_ack else 'Type your full name to sign this application.', 'danger')
            return redirect(url_for('portal.apply_loan_member'))
        if payment_collateral_type not in {'standing_order', 'post_dated_cheques'}:
            flash('Select the repayment collateral you will provide before the loan is issued.', 'danger')
            return redirect(url_for('portal.apply_loan_member'))
        if is_staff_member and payment_collateral_type != 'standing_order':
            flash('Staff cooperative loans must use a salary deduction or standing order mandate.', 'danger')
            return redirect(url_for('portal.apply_loan_member'))

        try:
            eligible_age, joined_at, _days_as_member = member_has_minimum_membership(member, 6)
            if not joined_at:
                flash('Your join date is missing. Please contact admin.', 'danger')
                return redirect(url_for('portal.apply_loan_member'))
            if not eligible_age:
                flash('You must be a member for at least 6 months to apply for a loan.', 'danger')
                return redirect(url_for('portal.apply_loan_member'))

            # Eligibility uses the savings ledger (source of truth).
            savings_balance = member_savings_balance(db, member['id'])
            if savings_balance < 50000:
                flash(f'Minimum savings of ₦50,000 required (yours: ₦{savings_balance:,.2f}).', 'danger')
                return redirect(url_for('portal.apply_loan_member'))

            if has_unpaid_loan_of_type(db, member['id'], purpose):
                flash(f'You have an unpaid {purpose} loan. Repay it fully before applying for another {purpose} loan. Other loan types can be submitted for approval.', 'danger')
                return redirect(url_for('portal.my_loans'))

            max_loan = loan_limits.eligible_amount(db, savings_balance, purpose)
            if amount > max_loan:
                flash(f'Maximum loan amount is ₦{max_loan:,.2f} — '
                      f'{loan_limits.eligibility_note(db, savings_balance)}', 'danger')
                return redirect(url_for('portal.apply_loan_member'))

            rate   = rates.get(purpose, rates['Regular'])
            method = methods.get(purpose, 'reducing_annual')
            mp, total_repayment, schedule = compute_loan_schedule(amount, rate, tenure, method)
            schedule_snapshot = json.dumps({
                'principal': round(amount, 2),
                'purpose': purpose,
                'tenure': tenure,
                'interest_rate': rate,
                'interest_method': method,
                'monthly_payment': round(mp, 2),
                'total_repayment': round(total_repayment, 2),
                'schedule': schedule,
                'accepted_at': datetime.now().isoformat(timespec='seconds'),
            }, separators=(',', ':'))

            required_g = lw.guarantors_required(db)
            guarantor_ids = list(dict.fromkeys(
                g for g in request.form.getlist('guarantors') if g and g != str(member['id'])
            ))
            if len(guarantor_ids) < required_g:
                flash(f'Select {required_g} guarantor(s) who are not the applicant.', 'danger')
                return redirect(url_for('portal.apply_loan_member'))
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
            lw.record_action(db, loan_id, 'submitted', 'submitted', acted_by=current_user.id,
                             acted_by_name=current_user.username, comment='Member self-application')

            from email_service import send_guarantor_request_email
            for gid in guarantor_ids:
                g = db.execute('SELECT * FROM members WHERE id = ?', (gid,)).fetchone()
                if not g:
                    continue
                db.execute("INSERT INTO loan_guarantors (loan_id, member_id, status) VALUES (?, ?, 'pending')",
                           (loan_id, gid))
                if g['email']:
                    notify_member(db, g['email'], 'Guarantor Request',
                                  f"{member['first_name']} {member['last_name']} asked you to guarantee "
                                  f"a ₦{amount:,.2f} loan. Please review and respond.",
                                  'warning', '/my-guarantor-requests')
                    send_guarantor_request_email(g['email'], g, member, loan_number, amount)

            db.commit()
            audit(db, 'MEMBER_LOAN_APPLICATION', 'loans',
                  f"Member {member['id']} applied for ₦{amount:,.2f} {purpose} loan – {loan_number}")
            # Log the request and alert the President, Treasurer, General Secretary
            # and exco right now — with the full application attached as a PDF.
            la.notify_loan_submitted(db, loan_id, channel='member_portal')
            flash(f'Loan application submitted! Reference: {loan_number}. The approval workflow has started.', 'success')
            return redirect(url_for('portal.my_loans'))

        except Exception as e:
            db.rollback()
            flash(f'Error submitting application: {str(e)}', 'danger')
            return redirect(url_for('portal.apply_loan_member'))

    all_members = db.execute(
        "SELECT id, first_name, last_name, member_number FROM members "
        "WHERE status = 'active' AND id != ? ORDER BY first_name", (member['id'],)
    ).fetchall()
    return render_template('member/apply_loan.html',
                           member=_member_extras(member, db),
                           max_tenure=max_tenure,
                           loan_limits=loan_limits.limits(db),
                           interest_rates=rates,
                           interest_methods=methods,
                           method_labels=METHOD_LABELS,
                           loan_types=list(rates.keys()),
                           is_staff_member=is_staff_member,
                           all_members=all_members)


# ── Change Savings Amount Request ─────────────────────────────────────────────────

@portal.route('/change-savings-request', methods=['GET', 'POST'])
@login_required
def change_savings_request():
    db     = get_db()
    member = member_for_user(db)
    if not member:
        flash('Member profile not found.', 'warning')
        return redirect(url_for('main.dashboard'))

    if request.method == 'POST':
        new_amount = request.form.get('new_amount', '').strip()
        reason     = request.form.get('reason', '').strip()

        try:
            new_amount_val = float(new_amount)
            if new_amount_val < 5000:
                flash('Minimum monthly savings is ₦5,000 (bye-laws 8.2.2).', 'danger')
                return redirect(url_for('portal.change_savings_request'))

            current_amt = float(member['monthly_savings'] or 0)

            # Block a duplicate request while one is still pending.
            pending = db.execute(
                "SELECT id FROM savings_change_requests WHERE member_id = ? AND status = 'pending'",
                (member['id'],)).fetchone()
            if pending:
                flash('You already have a pending savings-change request awaiting review.', 'warning')
                return redirect(url_for('portal.change_savings_request'))

            # Record a formal request for staff to approve/reject.
            db.execute('''INSERT INTO savings_change_requests
                          (member_id, current_amount, requested_amount, reason, status)
                          VALUES (?, ?, ?, ?, 'pending')''',
                       (member['id'], current_amt, new_amount_val, reason))
            db.commit()

            # Notify the officers who can actually act on it — resolved from the
            # duty rather than a fixed role list, so the alert follows the
            # Task Assignment settings instead of drifting out of step with them.
            import permissions as perms
            from email_service import send_savings_change_request_email

            officers = perms.users_with_permission(db, 'members.savings_requests')
            # Built here, in the request: the email goes out on a background
            # thread, which has no request context to derive a host from.
            review_url = url_for('members.savings_requests', _external=True)
            member_name = f"{member['first_name']} {member['last_name']}"
            summary = (f"{member_name} (#{member['member_number']}) "
                       f"requests to change monthly savings from "
                       f"₦{current_amt:,.2f} to ₦{new_amount_val:,.2f}. "
                       f"Reason: {reason or '—'}")

            for officer in officers:
                notify(db, officer['id'], 'Savings Amount Change Request', summary,
                       notification_type='info',
                       action_url=url_for('members.savings_requests'))
                if officer['email']:
                    send_savings_change_request_email(
                        officer['email'],
                        {'full_name': member_name,
                         'member_number': member['member_number']},
                        current_amt, new_amount_val, reason, review_url)

            audit(db, 'SAVINGS_CHANGE_REQUEST', 'members',
                  f"Member {member['id']} requested savings change to ₦{new_amount_val:,.2f}")
            db.commit()
            flash('Your request has been submitted and will be reviewed by the cooperative office.', 'success')
            return redirect(url_for('portal.member_portal'))

        except ValueError:
            flash('Please enter a valid amount.', 'danger')

    return render_template('member/change-savings-request.html',
                           member=_member_extras(member, db))


# ── Loan Calculator ───────────────────────────────────────────────────────────────

@portal.route('/loan-calculator')
@login_required
def loan_calculator():
    db     = get_db()
    member = member_for_user(db)
    rates  = _interest_rates(db)
    return render_template('member/loan-calculator.html',
                           member=_member_extras(member, db) if member else None,
                           interest_rates=rates,
                           policy=loan_limits.limits(db))


# ── My Member Card ────────────────────────────────────────────────────────────────

@portal.route('/my-cards')
@login_required
def my_cards():
    db     = get_db()
    member = member_for_user(db)
    if not member:
        flash('Member profile not found.', 'warning')
        return redirect(url_for('main.dashboard'))
    return render_template('member/my-cards.html', member=_member_extras(member, db))


# ── Profile ───────────────────────────────────────────────────────────────────────

@portal.route('/profile')
@login_required
def profile():
    db     = get_db()
    member = member_for_user(db)
    if not member:
        flash('Member profile not found.', 'warning')
        return redirect(url_for('main.dashboard'))
    return render_template('member/profile.html', member=_member_extras(member, db))


@portal.route('/profile/reveal-sensitive', methods=['POST'])
@login_required
def reveal_sensitive_profile_fields():
    db = get_db()
    member = member_for_user(db)
    if not member:
        return jsonify({'ok': False, 'error': 'member_not_found'}), 404
    password = request.form.get('password', '')
    user = db.execute('SELECT password_hash FROM users WHERE id = ?', (current_user.id,)).fetchone()
    if not user or not check_password_hash(user['password_hash'], password):
        audit(db, 'SENSITIVE_FIELD_REVEAL_DENIED', 'members', f"Invalid reveal password for member {member['id']}")
        db.commit()
        return jsonify({'ok': False, 'error': 'invalid_password'}), 403

    data = decrypt_member_sensitive_fields({
        'bank_name': member.get('bank_name') or '',
        'account_name': member.get('account_name') or '',
        'account_number': member.get('account_number') or '',
        'bvn': member.get('bvn') or '',
        'nin': member.get('nin') or '',
    })
    audit(db, 'SENSITIVE_FIELD_REVEAL', 'members', f"Member {member['id']} revealed own bank/ID fields")
    db.commit()
    return jsonify({'ok': True, 'fields': data})


@portal.route('/edit-profile', methods=['GET', 'POST'])
@login_required
def edit_profile():
    db     = get_db()
    member = member_for_user(db)
    if not member:
        flash('Member profile not found.', 'warning')
        return redirect(url_for('main.dashboard'))

    if request.method == 'POST':
        first_name = request.form.get('first_name', '').strip()
        last_name = request.form.get('last_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone', '').strip()
        address = request.form.get('address', '').strip()
        city = request.form.get('city', '').strip()
        state = request.form.get('state', '').strip()
        country = request.form.get('country', '').strip() or 'Nigeria'
        occupation = request.form.get('occupation', '').strip()
        date_of_birth = request.form.get('date_of_birth', '').strip() or None
        bank_name = request.form.get('bank_name', '').strip()
        account_name = request.form.get('account_name', '').strip()
        account_number = request.form.get('account_number', '').strip()
        emergency_contact_name = request.form.get('emergency_contact_name', '').strip()
        emergency_contact_phone = request.form.get('emergency_contact_phone', '').strip()
        nominee_name = request.form.get('nominee_name', '').strip()
        nominee_relationship = request.form.get('nominee_relationship', '').strip()
        nominee_phone = request.form.get('nominee_phone', '').strip()
        nominee_email = request.form.get('nominee_email', '').strip()
        nominee_address = request.form.get('nominee_address', '').strip()
        bvn = request.form.get('bvn', '').strip()
        nin = request.form.get('nin', '').strip()

        if not all([first_name, last_name, email, phone]):
            flash('First name, last name, email, and phone are required.', 'danger')
            return redirect(url_for('portal.edit_profile'))
        duplicate_member = db.execute(
            'SELECT id FROM members WHERE email = ? AND id <> ?',
            (email, member['id'])
        ).fetchone()
        duplicate_user = db.execute(
            'SELECT id FROM users WHERE email = ? AND id <> ?',
            (email, current_user.id)
        ).fetchone()
        if duplicate_member or duplicate_user:
            flash('That email address is already assigned to another account.', 'danger')
            return redirect(url_for('portal.edit_profile'))
        if date_of_birth:
            try:
                datetime.strptime(date_of_birth, '%Y-%m-%d')
            except ValueError:
                flash('Date of birth must be a valid date.', 'danger')
                return redirect(url_for('portal.edit_profile'))
        if account_number and (not account_number.isdigit() or len(account_number) != 10):
            flash('Account number must be 10 digits.', 'danger')
            return redirect(url_for('portal.edit_profile'))
        if bvn and (not bvn.isdigit() or len(bvn) != 11):
            flash('BVN must be 11 digits.', 'danger')
            return redirect(url_for('portal.edit_profile'))
        if nin and (not nin.isdigit() or len(nin) != 11):
            flash('NIN must be 11 digits.', 'danger')
            return redirect(url_for('portal.edit_profile'))

        photo_path = member['photo_path']
        if 'photo' in request.files:
            photo = request.files['photo']
            if photo and photo.filename:
                ok, err = validate_image(photo)
                if not ok:
                    flash(err, 'danger')
                    return redirect(url_for('portal.edit_profile'))
                ext = secure_filename(photo.filename).rsplit('.', 1)[1].lower()
                unique_name = f"{member['id']}_{secrets.token_hex(8)}.{ext}"
                photo_path = os.path.join('static/uploads/member-photos', unique_name)
                os.makedirs('static/uploads/member-photos', exist_ok=True)
                photo.save(photo_path)
        db.execute('''
            UPDATE members SET
                first_name = ?, last_name = ?, email = ?, phone = ?,
                address = ?, city = ?, state = ?, country = ?,
                occupation = ?, date_of_birth = ?,
                bank_name = ?, account_name = ?, account_number = ?,
                emergency_contact_name = ?, emergency_contact_phone = ?,
                nominee_name = ?, nominee_relationship = ?, nominee_phone = ?,
                nominee_email = ?, nominee_address = ?,
                bvn = ?, nin = ?, photo_path = ?, sms_optout = ?
            WHERE id = ?
        ''', (
            first_name, last_name, email, phone,
            address, city, state, country,
            occupation, date_of_birth,
            encrypt_field(bank_name), encrypt_field(account_name), encrypt_field(account_number),
            emergency_contact_name, emergency_contact_phone,
            nominee_name, nominee_relationship, nominee_phone,
            nominee_email, nominee_address,
            encrypt_field(bvn), encrypt_field(nin), photo_path,
            0 if request.form.get('sms_optin') else 1,
            member['id'],
        ))
        db.execute(
            'UPDATE users SET username = ?, email = ?, full_name = ?, phone = ? WHERE id = ?',
            (email, email, f'{first_name} {last_name}', phone, current_user.id)
        )
        db.commit()
        current_user.email = email
        current_user.username = email
        audit(db, 'MEMBER_PROFILE_UPDATE', 'members', f"Member {member['id']} updated profile")
        flash('Profile updated successfully!', 'success')
        return redirect(url_for('portal.profile'))

    return render_template('member/edit-profile.html',
                           member=decrypt_member_sensitive_fields(_member_extras(member, db)))


# ── Change Password ───────────────────────────────────────────────────────────────

@portal.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_pw  = request.form.get('current_password')
        new_pw      = request.form.get('new_password')
        confirm_pw  = request.form.get('confirm_password')

        if not all([current_pw, new_pw, confirm_pw]):
            flash('All fields are required.', 'danger')
            return redirect(url_for('portal.change_password'))
        if new_pw != confirm_pw:
            flash('New passwords do not match.', 'danger')
            return redirect(url_for('portal.change_password'))
        db   = get_db()
        ok, errors = validate_password_strength(new_pw, db, role=current_user.role, identifiers=(current_user.username, current_user.email))
        if not ok:
            flash(' '.join(errors), 'danger')
            return redirect(url_for('portal.change_password'))

        user = db.execute('SELECT * FROM users WHERE id = ?', (current_user.id,)).fetchone()
        if not check_password_hash(user['password_hash'], current_pw):
            flash('Current password is incorrect.', 'danger')
            return redirect(url_for('portal.change_password'))

        db.execute(
            'UPDATE users SET session_version = COALESCE(session_version, 0) + 1, password_hash = ?, must_change_password = 0 WHERE id = ?',
            (generate_password_hash(new_pw), current_user.id)
        )
        db.commit()
        flash('Password changed successfully! Please log in again.', 'success')
        logout_user()
        return redirect(url_for('auth.login'))

    return render_template('change-password.html')


# ── Nominee ───────────────────────────────────────────────────────────────────────

@portal.route('/nominee', methods=['GET', 'POST'])
@login_required
def nominee():
    db     = get_db()
    member = member_for_user(db)
    if not member:
        return redirect(url_for('main.dashboard'))
    return render_template('member/nominee.html', member=_member_extras(member, db))


# ── Transactions (SOA / running statement) ────────────────────────────────────────

@portal.route('/transactions')
@login_required
def transactions():
    db     = get_db()
    member = member_for_user(db)
    if not member:
        flash('Member profile not found.', 'warning')
        return redirect(url_for('main.dashboard'))

    from_date_str = request.args.get('from_date', '')
    to_date_str   = request.args.get('to_date',   '')
    tx_type       = request.args.get('type', '')

    txs = []

    # Savings contributions (CREDIT)
    for s in db.execute(
        'SELECT * FROM savings WHERE member_id = ? ORDER BY date', (member['id'],)
    ).fetchall():
        txs.append({
            'id': s['id'], 'date': _parse_dt(s['date']),
            'transaction_number': f"SAV-{s['id']:06d}",
            'description': f"Savings Contribution – {s['month']}",
            'transaction_type': 'Savings', 'type': 'credit', 'type_color': 'success',
            'amount': s['amount'], 'status': 'completed',
            'receipt_number': f"RCPT-SAV-{s['id']:06d}",
        })

    # Loan repayments (CREDIT on loan)
    try:
        for r in db.execute('''
            SELECT r.*, l.loan_number, l.purpose
            FROM repayments r JOIN loans l ON r.loan_id = l.id
            WHERE l.member_id = ? ORDER BY r.date
        ''', (member['id'],)).fetchall():
            txs.append({
                'id': f"R{r['id']}", 'date': _parse_dt(r['date']),
                'transaction_number': r['repayment_number'],
                'description': f"Loan Repayment – {r['loan_number']} ({r['purpose']})",
                'transaction_type': 'Repayment', 'type': 'credit', 'type_color': 'info',
                'amount': r['amount'], 'status': 'completed',
                'receipt_number': r['receipt_number'] if r['receipt_number'] else f"RCPT-{r['repayment_number']}",
            })
    except Exception:
        pass

    # Loan disbursements (DEBIT – money received)
    for l in db.execute(
        "SELECT * FROM loans WHERE member_id = ? AND disbursement_date IS NOT NULL ORDER BY disbursement_date",
        (member['id'],)
    ).fetchall():
        txs.append({
            'id': f"L{l['id']}", 'date': _parse_dt(l['disbursement_date']),
            'transaction_number': l['loan_number'],
            'description': f"Loan Disbursed – {l['purpose']}",
            'transaction_type': 'Loan', 'type': 'debit', 'type_color': 'primary',
            'amount': l['disbursed_amount'] or l['amount'],
            'status': 'completed',
            'receipt_number': f"RCPT-{l['loan_number']}",
        })

    # Sort oldest first for running balance
    txs.sort(key=lambda x: x['date'])

    # Compute running savings balance
    running = 0.0
    for t in txs:
        if t['transaction_type'] == 'Savings':
            running += t['amount']
        t['running_balance'] = round(running, 2)

    # Reverse so newest first for display
    txs.reverse()

    # Apply date/type filters
    if from_date_str:
        try:
            fd = datetime.strptime(from_date_str, '%Y-%m-%d')
            txs = [t for t in txs if t['date'] >= fd]
        except ValueError:
            pass
    if to_date_str:
        try:
            td = datetime.strptime(to_date_str, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
            txs = [t for t in txs if t['date'] <= td]
        except ValueError:
            pass
    if tx_type:
        txs = [t for t in txs if t['transaction_type'].lower() == tx_type.lower()]

    tx_objs = [SimpleNamespace(**t) for t in txs]

    # Simple pagination
    per_page = 50
    page     = max(1, request.args.get('page', 1, type=int))
    total    = len(tx_objs)
    pages    = max(1, (total + per_page - 1) // per_page)
    page     = min(page, pages)
    paginated = tx_objs[(page - 1) * per_page : page * per_page]

    return render_template('member/transactions.html',
                           transactions=paginated,
                           from_date=from_date_str,
                           to_date=to_date_str,
                           type=tx_type,
                           page=page,
                           pages=pages,
                           total=total,
                           member=_member_extras(member, db))


# ── Statement of Account (HTML printable / certifiable) ───────────────────────────

@portal.route('/statements')
@login_required
def statements():
    db     = get_db()
    member = member_for_user(db)
    if not member:
        flash('Member profile not found.', 'warning')
        return redirect(url_for('main.dashboard'))

    # ── Date range from query params (default: beginning of current year → today) ──
    default_from = datetime(datetime.now().year, 1, 1).strftime('%Y-%m-%d')
    default_to   = datetime.now().strftime('%Y-%m-%d')
    from_date_str = request.args.get('from_date', default_from)
    to_date_str   = request.args.get('to_date',   default_to)

    try:
        from_date = datetime.strptime(from_date_str, '%Y-%m-%d')
    except ValueError:
        from_date = datetime(datetime.now().year, 1, 1)
        from_date_str = from_date.strftime('%Y-%m-%d')
    try:
        to_date = datetime.strptime(to_date_str, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
    except ValueError:
        to_date = datetime.now()
        to_date_str = to_date.strftime('%Y-%m-%d')

    # ── All savings (full history, for running balance) ─────────────────────
    all_savings = db.execute(
        'SELECT * FROM savings WHERE member_id = ? ORDER BY date ASC',
        (member['id'],)
    ).fetchall()

    # ── All loans and repayments ────────────────────────────────────────────
    all_loans = db.execute(
        'SELECT * FROM loans WHERE member_id = ? ORDER BY date_applied ASC',
        (member['id'],)
    ).fetchall()

    all_repayments = []
    try:
        all_repayments = db.execute('''
            SELECT r.*, l.loan_number, l.purpose
            FROM repayments r JOIN loans l ON r.loan_id = l.id
            WHERE l.member_id = ? ORDER BY r.date ASC
        ''', (member['id'],)).fetchall()
    except Exception:
        pass

    # ── Build unified ledger (oldest first) ────────────────────────────────
    entries = []

    for s in all_savings:
        entries.append({
            'date':        _parse_dt(s['date']),
            'ref':         s['receipt_number'] or f"SAV-{s['id']:06d}",
            'description': f"Savings Deposit — {s['month']}",
            'entry_type':  'savings',
            'debit':       0.0,
            'credit':      float(s['amount'] or 0),
            'late_fee':    float(s['late_fee'] or 0),
            'method':      (s['payment_method'] or 'cash').title(),
        })

    for l in all_loans:
        if l['disbursement_date']:
            entries.append({
                'date':        _parse_dt(l['disbursement_date']),
                'ref':         l['loan_number'],
                'description': f"Loan Disbursed — {l['purpose']} ({l['tenure']} months)",
                'entry_type':  'loan_disbursement',
                'debit':       float(l['disbursed_amount'] or l['amount'] or 0),
                'credit':      0.0,
                'late_fee':    0.0,
                'method':      'Bank Transfer',
            })

    for r in all_repayments:
        entries.append({
            'date':        _parse_dt(r['date']),
            'ref':         r['repayment_number'] or r['receipt_number'] or f"REP-{r['id']:06d}",
            'description': f"Loan Repayment — {r['loan_number']} ({r['purpose']})",
            'entry_type':  'repayment',
            'debit':       0.0,
            'credit':      float(r['amount'] or 0),
            'late_fee':    0.0,
            'method':      (r['payment_method'] or 'cash').title(),
        })

    entries.sort(key=lambda e: e['date'])

    # ── Compute running balances (savings account & loan balance) ───────────
    run_savings = 0.0
    run_loan    = 0.0
    for e in entries:
        if e['entry_type'] == 'savings':
            run_savings += e['credit']
        elif e['entry_type'] == 'loan_disbursement':
            run_loan += e['debit']
        elif e['entry_type'] == 'repayment':
            principal = float(0)
            # try to get principal_paid from repayment if available
            run_loan = max(0.0, run_loan - e['credit'])
        e['run_savings'] = round(run_savings, 2)
        e['run_loan']    = round(run_loan, 2)
        e['net']         = round(run_savings - run_loan, 2)

    # ── Filter to requested date range ─────────────────────────────────────
    period_entries = [e for e in entries
                      if from_date <= e['date'] <= to_date]

    # Opening balances (totals before from_date)
    before = [e for e in entries if e['date'] < from_date]
    ob_savings = round(sum(e['credit'] for e in before if e['entry_type'] == 'savings'), 2)
    ob_loan    = round(sum(e['debit']  for e in before if e['entry_type'] == 'loan_disbursement')
                     - sum(e['credit'] for e in before if e['entry_type'] == 'repayment'), 2)
    ob_loan    = max(0.0, ob_loan)

    # Re-run running balances for the period starting from opening balances
    rs = ob_savings
    rl = ob_loan
    ledger = []
    for e in period_entries:
        if e['entry_type'] == 'savings':
            rs += e['credit']
        elif e['entry_type'] == 'loan_disbursement':
            rl += e['debit']
        elif e['entry_type'] == 'repayment':
            rl = max(0.0, rl - e['credit'])
        row = dict(e)
        row['run_savings'] = round(rs, 2)
        row['run_loan']    = round(rl, 2)
        row['net']         = round(rs - rl, 2)
        ledger.append(SimpleNamespace(**row))

    # ── Period summary figures ─────────────────────────────────────────────
    period_savings_in   = sum(e['credit'] for e in period_entries if e['entry_type'] == 'savings')
    period_late_fees    = sum(e['late_fee'] for e in period_entries if e['entry_type'] == 'savings')
    period_loans_out    = sum(e['debit']  for e in period_entries if e['entry_type'] == 'loan_disbursement')
    period_repaid       = sum(e['credit'] for e in period_entries if e['entry_type'] == 'repayment')

    # Closing balances
    cb_savings = rs
    cb_loan    = rl
    cb_net     = round(cb_savings - cb_loan, 2)

    # Full-history totals for summary cards
    total_savings_ever = sum(float(s['amount'] or 0) for s in all_savings)
    active_loans_list  = [l for l in all_loans if l['status'] == 'active']
    total_outstanding  = sum(float(l['balance'] or 0) for l in active_loans_list)

    return render_template('member/statements.html',
                           member=_member_extras(member, db),
                           ledger=ledger,
                           from_date=from_date_str,
                           to_date=to_date_str,
                           ob_savings=ob_savings,
                           ob_loan=ob_loan,
                           cb_savings=cb_savings,
                           cb_loan=cb_loan,
                           cb_net=cb_net,
                           period_savings_in=period_savings_in,
                           period_late_fees=period_late_fees,
                           period_loans_out=period_loans_out,
                           period_repaid=period_repaid,
                           total_savings_ever=total_savings_ever,
                           total_outstanding=total_outstanding,
                           generated_on=datetime.now())


# ── Notifications ─────────────────────────────────────────────────────────────────

@portal.route('/notifications')
@login_required
def notifications():
    db           = get_db()
    active_filter = request.args.get('filter', 'all')
    page         = max(1, request.args.get('page', 1, type=int))
    per_page     = 20

    base_query = 'SELECT * FROM notifications WHERE user_id = ?'
    params     = [current_user.id]
    if active_filter == 'unread':
        base_query += ' AND is_read = 0'
    elif active_filter == 'important':
        base_query += " AND notification_type IN ('warning','danger')"

    total  = db.execute(base_query.replace('SELECT *', 'SELECT COUNT(*)'), params).fetchone()[0]
    notifs = db.execute(
        base_query + ' ORDER BY created_at DESC LIMIT ? OFFSET ?',
        params + [per_page, (page - 1) * per_page]
    ).fetchall()

    pages = max(1, (total + per_page - 1) // per_page)
    return render_template('member/notifications.html',
                           notifications=notifs,
                           filter=active_filter,
                           page=page, pages=pages, total=total)


@portal.route('/notifications/mark-read/<int:notif_id>', methods=['POST'])
@login_required
def mark_notification_read(notif_id):
    db = get_db()
    db.execute('UPDATE notifications SET is_read = 1, read_at = ? WHERE id = ? AND user_id = ?',
               (datetime.now(), notif_id, current_user.id))
    db.commit()
    return jsonify({'ok': True})


@portal.route('/notifications/mark-all-read', methods=['POST'])
@login_required
def mark_all_notifications_read():
    db = get_db()
    db.execute('UPDATE notifications SET is_read = 1, read_at = ? WHERE user_id = ? AND is_read = 0',
               (datetime.now(), current_user.id))
    db.commit()
    return jsonify({'ok': True})


@portal.route('/notification/<int:notif_id>')
@login_required
def open_notification(notif_id):
    """Open a notification: mark it read, then go to its action target (or back
    to the list). This backs the card click on the notifications page."""
    db = get_db()
    n = db.execute('SELECT * FROM notifications WHERE id = ? AND user_id = ?',
                   (notif_id, current_user.id)).fetchone()
    if not n:
        flash('That notification could not be found.', 'warning')
        return redirect(url_for('portal.notifications'))
    db.execute('UPDATE notifications SET is_read = 1, read_at = ? WHERE id = ? AND user_id = ?',
               (datetime.now(), notif_id, current_user.id))
    db.commit()
    target = (n['action_url'] or '').strip()
    # Only follow same-site relative paths — never an external/open redirect.
    if target.startswith('/') and not target.startswith('//'):
        return redirect(target)
    return redirect(url_for('portal.notifications'))


# ── Support ───────────────────────────────────────────────────────────────────────

@portal.route('/support', methods=['GET', 'POST'])
@login_required
def support():
    db     = get_db()
    member = member_for_user(db)
    if request.method == 'POST':
        subject = request.form.get('subject', '').strip()
        message = request.form.get('message', '').strip()
        if subject and message and member:
            admin_users = db.execute("SELECT email FROM users WHERE role = 'admin'").fetchall()
            for admin in admin_users:
                notify_member(db, admin['email'],
                              f'Support Request: {subject}',
                              f"From: {member['first_name']} {member['last_name']} "
                              f"({current_user.email})\n\n{message}",
                              notification_type='info',
                              action_url='/members')
            flash('Your support request has been sent. We will get back to you shortly.', 'success')
            return redirect(url_for('portal.support'))
        else:
            flash('Subject and message are required.', 'danger')

    def _setting(key, default=''):
        row = db.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        return row['value'] if row and row['value'] else default

    wa_raw    = _setting('whatsapp_number', '')
    wa_digits = ''.join(c for c in wa_raw if c.isdigit())
    wa_link   = f"https://wa.me/{wa_digits}" if wa_digits else None

    return render_template('member/support.html',
                           member=_member_extras(member, db) if member else None,
                           whatsapp_number=wa_raw,
                           whatsapp_link=wa_link,
                           support_phone=_setting('support_phone', wa_raw),
                           support_email=_setting('support_email', _setting('email', '')),
                           office_address=_setting('office_address', 'Main Office'))


# ── PDF Statement (admin/member download) ─────────────────────────────────────────

@portal.route('/member/statement/<int:member_id>')
@login_required
def member_statement(member_id):
    # Only admin or the member themselves
    db     = get_db()
    member = db.execute('SELECT * FROM members WHERE id = ?', (member_id,)).fetchone()
    if not member:
        flash('Member not found.', 'danger')
        return redirect(url_for('members.members_list'))

    if current_user.role == 'member':
        self_member = member_for_user(db)
        if not self_member or self_member['id'] != member_id:
            flash('Access denied.', 'danger')
            return redirect(url_for('portal.statements'))

    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle, Paragraph

        savings_rows = db.execute(
            'SELECT date, month, amount, late_fee FROM savings WHERE member_id = ? ORDER BY date DESC',
            (member_id,)
        ).fetchall()
        loan_rows = db.execute(
            'SELECT loan_number, purpose, amount, balance, status, date_applied FROM loans WHERE member_id = ?',
            (member_id,)
        ).fetchall()

        buffer = BytesIO()
        doc    = SimpleDocTemplate(buffer, pagesize=A4)
        styles = getSampleStyleSheet()
        elems  = []

        coop_name = db.execute("SELECT value FROM settings WHERE key='coop_name'").fetchone()
        coop_name = coop_name['value'] if coop_name else 'Your Cooperative'

        elems.append(Paragraph(f'{coop_name} – Member Statement of Account', styles['Title']))
        elems.append(Spacer(1, 0.2 * inch))
        elems.append(Paragraph(f"<b>Member:</b> {member['first_name']} {member['last_name']}", styles['Normal']))
        elems.append(Paragraph(f"<b>Member #:</b> {member['member_number'] or 'N/A'}", styles['Normal']))
        elems.append(Paragraph(f"<b>Email:</b> {member['email'] or ''}", styles['Normal']))
        elems.append(Paragraph(f"<b>Date Generated:</b> {datetime.now().strftime('%d %B %Y %H:%M')}", styles['Normal']))
        elems.append(Spacer(1, 0.3 * inch))

        total_savings = sum(s['amount'] for s in savings_rows)
        active_loans  = [l for l in loan_rows if l['status'] == 'active']
        total_loans   = sum(l['balance'] for l in active_loans)

        summary = Table([
            ['Description', 'Amount (₦)'],
            ['Total Savings',      f"{total_savings:,.2f}"],
            ['Active Loan Balance', f"{total_loans:,.2f}"],
            ['Net Position',       f"{total_savings - total_loans:,.2f}"],
        ])
        summary.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#4f46e5')),
            ('TEXTCOLOR',  (0,0), (-1,0), colors.whitesmoke),
            ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
            ('ALIGN',      (0,0), (-1,-1), 'CENTER'),
            ('GRID',       (0,0), (-1,-1), 0.5, colors.grey),
            ('BACKGROUND', (0,1), (-1,-1), colors.HexColor('#f5f5f5')),
        ]))
        elems.append(summary)
        elems.append(Spacer(1, 0.3 * inch))

        # Savings section
        elems.append(Paragraph('<b>Savings History</b>', styles['Heading2']))
        s_data = [['Date', 'Month', 'Base Amount', 'Late Fee', 'Total Paid']]
        for s in savings_rows:
            base = s['amount'] - s['late_fee']
            s_data.append([
                s['date'][:10] if s['date'] else '',
                s['month'],
                f"₦{base:,.2f}",
                f"₦{s['late_fee']:,.2f}" if s['late_fee'] else '-',
                f"₦{s['amount']:,.2f}",
            ])
        if len(s_data) > 1:
            t = Table(s_data, colWidths=[1.2*inch]*5)
            t.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.grey),
                ('TEXTCOLOR',  (0,0), (-1,0), colors.whitesmoke),
                ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
                ('ALIGN',      (0,0), (-1,-1), 'CENTER'),
                ('GRID',       (0,0), (-1,-1), 0.5, colors.black),
                ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f9f9f9')]),
            ]))
            elems.append(t)
        else:
            elems.append(Paragraph('No savings records.', styles['Normal']))

        # Loans section
        elems.append(Spacer(1, 0.3 * inch))
        elems.append(Paragraph('<b>Loan History</b>', styles['Heading2']))
        l_data = [['Loan #', 'Purpose', 'Amount', 'Balance', 'Status']]
        for l in loan_rows:
            l_data.append([
                l['loan_number'], l['purpose'] or '-',
                f"₦{l['amount']:,.2f}", f"₦{l['balance']:,.2f}", l['status'].title()
            ])
        if len(l_data) > 1:
            t2 = Table(l_data, colWidths=[1.5*inch, 1.2*inch, 1.2*inch, 1.2*inch, 1.0*inch])
            t2.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.grey),
                ('TEXTCOLOR',  (0,0), (-1,0), colors.whitesmoke),
                ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
                ('ALIGN',      (0,0), (-1,-1), 'CENTER'),
                ('GRID',       (0,0), (-1,-1), 0.5, colors.black),
                ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f9f9f9')]),
            ]))
            elems.append(t2)
        else:
            elems.append(Paragraph('No loan records.', styles['Normal']))

        doc.build(elems)
        pdf = buffer.getvalue()
        buffer.close()

        response = make_response(pdf)
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = \
            f'attachment; filename=SOA_{member["member_number"] or member_id}_{datetime.now().strftime("%Y%m%d")}.pdf'
        return response

    except ImportError:
        flash('PDF generation requires reportlab. Run: pip install reportlab', 'warning')
        return redirect(url_for('portal.statements'))
    except Exception as e:
        flash(f'Error generating PDF: {str(e)}', 'danger')
        return redirect(url_for('portal.statements'))
