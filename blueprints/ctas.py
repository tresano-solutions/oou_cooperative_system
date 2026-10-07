"""
CTAS — Cooperative Target Advance Scheme.

A balloted rotating-contribution (ajo/esusu) module. Members subscribe to a
cycle and contribute a fixed amount every period; a seeded ballot assigns each a
payout position; on that position they collect the full target. Their own pooled
contributions fund part of the payout and the cooperative advances the gap,
which their remaining contributions repay. Money posts to the real double-entry
GL (CTAS Contribution Pool, CTAS Advances Receivable, CTAS Admin Fee Income,
CTAS Write-offs) and reuses CoopMS members.

**Optional feature.** Off by default and leaves no footprint (no menu, no
routes, no GL accounts) until a cooperative is activated — on request — either by
the operator from HQ (POST /api/hq/set-feature) or by a super admin here. Gate
every CTAS view with @ctas_required.
"""

import csv
import hmac
import os
import secrets
from datetime import date, datetime, timedelta
from io import StringIO, TextIOWrapper
from functools import wraps

from flask import (Blueprint, abort, current_app, flash, jsonify, make_response,
                   redirect, render_template, request, url_for)
from flask_login import current_user, login_required

from database import get_db, last_insert_id
from utils import finite_float, audit, role_required, member_for_user, notify_member
from ledger import post_journal_safe, get_default_cash_account, MEMBER_DEPOSITS, SHARE_CAPITAL
from payments import get_gateway, generate_reference
import permissions as perms
import ctas_engine as ce

ctas = Blueprint('ctas', __name__)

CTAS_WRITEOFF = '5150'   # expense — outstanding CTAS advance written off on member exit
CTAS_POOL = '2050'       # liability — members' contributions held in the rotating pool
CTAS_PRIORITY_FEE_INCOME = '4160'   # income — fee for an early payout position

# What a member agrees to when they join. Covers the contribution obligation,
# how the cooperative may recover it, credit assessment and the processing of
# personal data — the consents a cooperative needs on record.
CTAS_TERMS = [
    ('Your contributions',
     'I will contribute the fixed amount every period for the full duration of the cycle, '
     'whether or not I have already received my payout.'),
    ('Receiving your target',
     'I understand my payout position is decided by ballot, that I receive the target amount '
     'on that position, and that my obligation to contribute continues until the cycle ends.'),
    ('How the cooperative recovers contributions',
     'I authorise the cooperative to collect my contributions by the method I have chosen — '
     'salary deduction, an authorised card or account debit, or direct payment — and to apply '
     'any payment I make to amounts I owe under this scheme.'),
    ('If I leave or fall into arrears',
     'If I exit the cooperative or fail to contribute, I authorise the cooperative to recover '
     'what I still owe from my savings, share capital, dividends and any terminal benefits, '
     'in that order, in line with the bye-laws.'),
    ('Credit and affordability checks',
     'I consent to the cooperative assessing my creditworthiness and affordability for this '
     'scheme, including reviewing my savings, contribution and loan repayment history, and '
     'where applicable confirming my employment and salary deduction capacity.'),
    ('Processing my personal information',
     'I consent to the cooperative collecting and processing my personal and financial '
     'information for the purpose of operating this scheme, including assessing my application, '
     'collecting contributions, making payouts and meeting its record-keeping obligations. '
     'My information is kept secure, is not sold, and is shared only where necessary to operate '
     'the scheme or where required by law. I may withdraw this consent by ending my '
     'participation, subject to settling what I owe.'),
]


def _liquidity(db, cycle):
    """Liquidity projection for a cycle, using the real ballot result once it
    exists and the worst case (full capacity) before that."""
    members = ce.participant_count(db, cycle['id'])
    rows = db.execute("SELECT payout_month, COUNT(*) AS n FROM ctas_subscriptions "
                      "WHERE cycle_id = ? AND payout_month IS NOT NULL GROUP BY payout_month",
                      (cycle['id'],)).fetchall()
    by_period = {int(r['payout_month']): int(r['n']) for r in rows} or None
    return ce.liquidity_projection(cycle, members, payouts_by_period=by_period)


def _priority_tiers(db, cycle_id=None, plan_id=None):
    """{position: fee} for a cycle (or a plan template)."""
    if cycle_id:
        rows = db.execute('SELECT position, fee FROM ctas_priority_fees WHERE cycle_id = ? '
                          'ORDER BY position', (cycle_id,)).fetchall()
    else:
        rows = db.execute('SELECT position, fee FROM ctas_priority_fees WHERE plan_id = ? '
                          'ORDER BY position', (plan_id,)).fetchall()
    return {int(r['position']): float(r['fee'] or 0) for r in rows}


def _notify_member(db, member_id, title, message, action_url='/my-ctas'):
    """Best-effort in-app + push notification to a member by id."""
    row = db.execute('SELECT email FROM members WHERE id = ?', (member_id,)).fetchone()
    if row and row['email']:
        notify_member(db, row['email'], title, message, 'info', action_url)


def _ensure_schedule(db, sub, cycle):
    """Create this subscription's contribution schedule (one row per period) if it
    does not exist yet. Idempotent."""
    existing = db.execute('SELECT COUNT(*) FROM ctas_schedule WHERE subscription_id = ?',
                          (sub['id'],)).fetchone()[0]
    if existing:
        return 0
    rows = ce.build_schedule(cycle)
    # Cycles created before the pool model (or left blank) carry no contribution
    # amount — fall back to the member's own per-period figure so the schedule is
    # still meaningful.
    fallback = round(float(sub['monthly_deduction'] or 0), 2)
    for period, due, amount in rows:
        db.execute('''INSERT INTO ctas_schedule
                        (subscription_id, cycle_id, period_number, due_date, expected_amount, status)
                      VALUES (?, ?, ?, ?, ?, 'pending')''',
                   (sub['id'], cycle['id'], period,
                    due.isoformat() if due else None, amount or fallback))
    return len(rows)


def _refresh_schedule_statuses(db, cycle_id):
    """Recompute due/grace/late for a cycle's unpaid schedule rows."""
    cycle = db.execute('SELECT grace_days FROM ctas_cycles WHERE id = ?', (cycle_id,)).fetchone()
    grace = int((cycle['grace_days'] if cycle and cycle['grace_days'] is not None else 7) or 0)
    rows = db.execute("SELECT * FROM ctas_schedule WHERE cycle_id = ? AND status != 'paid'",
                      (cycle_id,)).fetchall()
    for r in rows:
        status = ce.schedule_status(r['due_date'], r['expected_amount'], r['paid_amount'], grace)
        if status != r['status']:
            db.execute('UPDATE ctas_schedule SET status = ? WHERE id = ?', (status, r['id']))


def _apply_to_schedule(db, subscription_id, period, amount, grace_days=7):
    """Record a contribution against its schedule row."""
    row = db.execute('SELECT * FROM ctas_schedule WHERE subscription_id = ? AND period_number = ?',
                     (subscription_id, period)).fetchone()
    if not row:
        return
    paid = round(float(row['paid_amount'] or 0) + float(amount or 0), 2)
    status = ce.schedule_status(row['due_date'], row['expected_amount'], paid, grace_days)
    db.execute('UPDATE ctas_schedule SET paid_amount = ?, status = ?, paid_at = ? WHERE id = ?',
               (paid, status, datetime.now() if status == ce.SCH_PAID else row['paid_at'], row['id']))


def _post_contribution(db, sub, cycle, period, amount, created_by=None, ref_suffix='',
                       debit_account=None):
    """Record one contribution: post it to the GL (pool before payout, advance
    repayment after), update the subscription, and tick off its schedule row.
    Returns the amount actually posted. The single path used by the CSV import,
    automatic debits, and money transferred to a member's own account number.

    ``debit_account`` overrides the cash side. A transfer that already landed in
    the bank is held in Unallocated Member Receipts, so debiting cash again
    would count the same money twice."""
    ctas_ensure_accounts(db)
    cash = debit_account or get_default_cash_account(db)
    amount = round(float(amount or 0), 2)
    contributed = round(float(sub['contributed_total'] or 0) + amount, 2)

    if sub['status'] == ce.SUB_ACTIVE_RECOVERY:
        # Already paid out — this repays the cooperative's advance.
        post_amt = round(min(amount, float(sub['advance_balance'] or 0)), 2)
        credit_acct = CTAS_ADVANCES
        new_adv = round(max(0.0, float(sub['advance_balance'] or 0) - post_amt), 2)
        recovered = round(float(sub['total_recovered'] or 0) + post_amt, 2)
        status = 'completed' if new_adv <= 0 else 'active_recovery'
    else:
        # Not yet paid out — funds the rotating pool.
        post_amt = amount
        credit_acct = CTAS_POOL
        new_adv = float(sub['advance_balance'] or 0)
        recovered = float(sub['total_recovered'] or 0)
        status = sub['status']

    if post_amt > 0:
        post_journal_safe(
            db, f"CTAS contribution period {period} - subscription {sub['id']}",
            [{'account': cash, 'debit': post_amt, 'memo': f'CTAS contribution p{period}'},
             {'account': credit_acct, 'credit': post_amt, 'memo': f"CTAS contribution sub {sub['id']}"}],
            date=datetime.now(), reference=f"CTAS-CN-{sub['id']}-M{period}{ref_suffix}",
            source_module='ctas_contribution', source_id=sub['id'], created_by=created_by)

    db.execute("UPDATE ctas_subscriptions SET contributed_total = ?, advance_balance = ?, "
               "total_recovered = ?, outstanding = ?, status = ?, completed_at = ? WHERE id = ?",
               (contributed, new_adv, recovered,
                max(0.0, round(float(sub['target_amount']) - contributed, 2)), status,
                datetime.now() if status == 'completed' else None, sub['id']))
    _apply_to_schedule(db, sub['id'], period, amount,
                       int(cycle['grace_days'] if cycle['grace_days'] is not None else 7))
    return post_amt


def _raise_exception(db, subscription_id, case_type, month, amount, description):
    """Log a CTAS exception case (missed deduction, exit recovery, override)."""
    db.execute("INSERT INTO ctas_exceptions (subscription_id, case_type, status, month_number, "
               "amount, description) VALUES (?, ?, 'open', ?, ?, ?)",
               (subscription_id, case_type, month, amount, description))

CTAS_ADVANCES = '1150'            # asset — advances paid out, recovered via payroll
CTAS_ADMIN_FEE_INCOME = '4150'   # income — admin fee charged on subscription


def ctas_enabled(db=None) -> bool:
    """True if this cooperative has the CTAS add-on switched on."""
    try:
        db = db or get_db()
        row = db.execute("SELECT value FROM settings WHERE key = 'ctas_enabled'").fetchone()
        return bool(row and str(row['value']) == '1')
    except Exception:
        return False


def _member_reachable_value(db, member_id) -> float:
    """Savings plus share capital the cooperative can actually reach for a
    member — the ceiling on what a guarantee can be called on for."""
    try:
        from utils import member_savings_balance, member_share_capital
        return round(float(member_savings_balance(db, member_id))
                     + float(member_share_capital(db, member_id)), 2)
    except Exception:
        return 0.0


def _ctas_max_exposure(db) -> float:
    """Coop-wide ceiling on advances outstanding. 0 = uncapped (the default)."""
    try:
        row = db.execute("SELECT value FROM settings WHERE key = 'ctas_max_net_exposure'").fetchone()
        return max(0.0, float(row['value'])) if row and row['value'] else 0.0
    except Exception:
        return 0.0


def _ctas_advances_outstanding(db) -> float:
    """Advances paid out and not yet recovered, from the ledger (GL 1150)."""
    try:
        r = db.execute("SELECT COALESCE(SUM(debit),0) AS d, COALESCE(SUM(credit),0) AS c "
                       "FROM journal_lines WHERE account_code = ?", (CTAS_ADVANCES,)).fetchone()
        return round(float(r['d'] or 0) - float(r['c'] or 0), 2)
    except Exception:
        return 0.0


def ctas_ensure_accounts(db) -> None:
    """Seed the two CTAS GL accounts — only when the feature is turned on, so a
    coop that never uses CTAS keeps a clean chart of accounts."""
    for code, name, atype, normal in (
        (CTAS_ADVANCES, 'CTAS Advances Receivable', 'asset', 'debit'),
        (CTAS_POOL, 'CTAS Contribution Pool', 'liability', 'credit'),
        (CTAS_ADMIN_FEE_INCOME, 'CTAS Admin Fee Income', 'income', 'credit'),
        (CTAS_PRIORITY_FEE_INCOME, 'CTAS Priority Fee Income', 'income', 'credit'),
        (CTAS_WRITEOFF, 'CTAS Write-offs', 'expense', 'debit'),
    ):
        try:
            db.execute(
                "INSERT INTO accounts (code, name, type, normal_balance, parent_code) "
                "VALUES (?, ?, ?, ?, NULL) ON CONFLICT(code) DO NOTHING",
                (code, name, atype, normal))
        except Exception:
            pass


def set_ctas_enabled(db, on: bool) -> None:
    """Turn the CTAS add-on on/off for this cooperative (seeds accounts on-on)."""
    db.execute("DELETE FROM settings WHERE key = 'ctas_enabled'")
    db.execute("INSERT INTO settings (key, value) VALUES ('ctas_enabled', ?)", ('1' if on else '0',))
    if on:
        ctas_ensure_accounts(db)


def ctas_required(f):
    """Feature gate: 404 unless CTAS is enabled here; then require login."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not ctas_enabled():
            abort(404)
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login', next=request.path))
        return f(*args, **kwargs)
    return wrapper


def _is_operator() -> bool:
    return current_user.is_authenticated and getattr(current_user, 'is_super_admin', False)


@ctas.route('/ctas/enable', methods=['POST'])
@login_required
def enable_ctas():
    """Activate CTAS for this cooperative — operator/super-admin only."""
    if not _is_operator():
        abort(403)
    db = get_db()
    set_ctas_enabled(db, True)
    audit(db, 'CTAS_ENABLED', 'ctas', 'Target Advance Scheme enabled')
    db.commit()
    flash('CTAS (Target Advance Scheme) is now enabled.', 'success')
    return redirect(request.referrer or url_for('main.dashboard'))


@ctas.route('/ctas/disable', methods=['POST'])
@login_required
def disable_ctas():
    if not _is_operator():
        abort(403)
    db = get_db()
    set_ctas_enabled(db, False)
    audit(db, 'CTAS_DISABLED', 'ctas', 'Target Advance Scheme disabled')
    db.commit()
    flash('CTAS has been disabled.', 'info')
    return redirect(request.referrer or url_for('main.dashboard'))


# ── Admin: cycles ─────────────────────────────────────────────────────────────

@ctas.route('/ctas')
@ctas_required
@role_required('admin', 'treasurer')
def dashboard():
    db = get_db()
    cycles = db.execute('''
        SELECT c.*,
               (SELECT COUNT(*) FROM ctas_subscriptions s WHERE s.cycle_id = c.id) AS sub_count,
               (SELECT COUNT(*) FROM ctas_subscriptions s WHERE s.cycle_id = c.id AND s.status = 'enrolled') AS enrolled_count
        FROM ctas_cycles c ORDER BY c.created_at DESC, c.id DESC
    ''').fetchall()
    active_plans = db.execute("SELECT * FROM ctas_plans WHERE status = 'active' ORDER BY name").fetchall()
    return render_template('ctas/cycles.html', cycles=cycles, plans=active_plans, ce=ce)


@ctas.route('/ctas/cycles', methods=['POST'])
@ctas_required
@role_required('admin', 'treasurer')
def new_cycle():
    db = get_db()
    name = (request.form.get('name') or '').strip()
    if not name:
        flash('Cycle name is required.', 'danger')
        return redirect(url_for('ctas.dashboard'))

    # A cycle may be created from a reusable Plan (which supplies the product
    # terms), with the form overriding only the schedule/dates.
    plan = None
    plan_id = request.form.get('plan_id')
    if plan_id:
        plan = db.execute('SELECT * FROM ctas_plans WHERE id = ?', (plan_id,)).fetchone()

    def _num(key, default):
        try:
            return float(request.form.get(key) or default)
        except ValueError:
            return float(default)

    def _pn(key, default):
        if plan is not None and key in plan.keys() and plan[key] is not None:
            try:
                return float(plan[key])
            except (TypeError, ValueError):
                return float(default)
        return _num(key, default)

    frequency = (request.form.get('frequency') or (plan['frequency'] if plan else 'monthly') or 'monthly').strip()
    periods = int(_pn('periods', 12))
    contribution = _pn('contribution_amount', 0)

    db.execute('''INSERT INTO ctas_cycles
            (name, status, start_date, end_date, duration_months, fixed_monthly_amount,
             frequency, periods, contribution_amount, plan_id,
             monthly_capacity, earliest_payout_month, max_participants,
             admin_fee_flat, admin_fee_percentage, admin_fee_cap, admin_fee_threshold,
             ballot_date, affordability_method, affordability_ratio, savings_multiple,
             grace_days, coverage_ratio, created_by)
            VALUES (?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (name, (request.form.get('start_date') or '').strip() or None,
         (request.form.get('end_date') or '').strip() or None,
         periods, contribution,   # duration_months = periods (back-compat), fixed_monthly_amount = contribution
         frequency, periods, contribution, (plan['id'] if plan else None),
         int(_pn('monthly_capacity', 1)), int(_pn('earliest_payout_month', 1)),
         int(_num('max_participants', 0)),
         _pn('admin_fee_flat', 0), _pn('admin_fee_percentage', 0),
         _pn('admin_fee_cap', 0), _pn('admin_fee_threshold', 0),
         (request.form.get('ballot_date') or '').strip() or None,
         (plan['affordability_method'] if plan else (request.form.get('affordability_method') or 'savings')).strip(),
         _pn('affordability_ratio', 0.5), _pn('savings_multiple', 3),
         int(_pn('grace_days', 7)),
         # Inherited from the plan when there is one, so the cooperative sets the
         # cover it thinks appropriate once; still overridable per cycle.
         _pn('coverage_ratio', 0), current_user.id))
    cycle_id = last_insert_id(db)   # capture before the audit insert changes last-rowid
    audit(db, 'CTAS_CYCLE_CREATE', 'ctas', f"Created cycle {name}")
    db.commit()
    flash(f'Cycle "{name}" created (draft). Open it to start enrolment.', 'success')
    return redirect(url_for('ctas.cycle_detail', cycle_id=cycle_id))


@ctas.route('/ctas/overview')
@ctas_required
@role_required('admin', 'treasurer')
def overview():
    """Management view: is CTAS profitable, funded, and within risk appetite?
    Money figures come from the general ledger (authoritative); operational
    figures from the CTAS tables. Anything the system does not yet track is
    reported as not tracked rather than guessed at."""
    db = get_db()

    def one(sql, params=()):
        row = db.execute(sql, params).fetchone()
        return (row[0] if row and row[0] is not None else 0)

    def gl_balance(code, normal='credit'):
        """Net movement on an account (income/liability net credit; asset net debit)."""
        r = db.execute("SELECT COALESCE(SUM(jl.debit),0) AS d, COALESCE(SUM(jl.credit),0) AS c "
                       "FROM journal_lines jl WHERE jl.account_code = ?", (code,)).fetchone()
        d, c = float(r['d'] or 0), float(r['c'] or 0)
        return round(c - d, 2) if normal == 'credit' else round(d - c, 2)

    # ── Membership ──
    membership = {
        'applications': one("SELECT COUNT(*) FROM ctas_subscriptions"),
        'awaiting': one("SELECT COUNT(*) FROM ctas_subscriptions WHERE status IN "
                        "('submitted','eligible','finance_reviewed','approved')"),
        'enrolled': one("SELECT COUNT(*) FROM ctas_subscriptions WHERE status = 'enrolled'"),
        'active': one("SELECT COUNT(*) FROM ctas_subscriptions WHERE status IN "
                      "('scheduled','active_recovery')"),
        'completed': one("SELECT COUNT(*) FROM ctas_subscriptions WHERE status = 'completed'"),
        'rejected': one("SELECT COUNT(*) FROM ctas_subscriptions WHERE status = 'rejected'"),
        'cycles_running': one("SELECT COUNT(*) FROM ctas_cycles WHERE status IN "
                              "('open','closed','ready_for_ballot','balloted','active')"),
    }

    # ── Contributions (from the due-date schedule) ──
    expected = float(one("SELECT COALESCE(SUM(expected_amount),0) FROM ctas_schedule "
                         "WHERE status != 'pending'"))
    received = float(one("SELECT COALESCE(SUM(paid_amount),0) FROM ctas_schedule"))
    contributions = {
        'expected_to_date': round(expected, 2),
        'received': round(received, 2),
        'collection_rate': round((received / expected * 100), 1) if expected else None,
        'late_rows': one("SELECT COUNT(*) FROM ctas_schedule WHERE status = 'late'"),
        'due_rows': one("SELECT COUNT(*) FROM ctas_schedule WHERE status IN ('due','grace')"),
        'arrears_total': round(float(one("SELECT COALESCE(SUM(arrears_amount),0) FROM ctas_subscriptions")), 2),
        'members_in_arrears': one("SELECT COUNT(*) FROM ctas_subscriptions WHERE arrears_amount > 0"),
    }

    # ── Payouts ──
    payouts = {
        'completed': one("SELECT COUNT(*) FROM ctas_subscriptions WHERE paid_out_at IS NOT NULL"),
        'paid_value': round(float(one("SELECT COALESCE(SUM(target_amount),0) FROM ctas_subscriptions "
                                      "WHERE paid_out_at IS NOT NULL")), 2),
        'upcoming': one("SELECT COUNT(*) FROM ctas_subscriptions WHERE status = 'scheduled'"),
        'upcoming_value': round(float(one("SELECT COALESCE(SUM(target_amount),0) FROM ctas_subscriptions "
                                          "WHERE status = 'scheduled'")), 2),
        'priority_allocated': one("SELECT COUNT(*) FROM ctas_subscriptions WHERE is_priority = 1 "
                                  "AND priority_status = 'granted' AND payout_month IS NOT NULL"),
        'priority_requested': one("SELECT COUNT(*) FROM ctas_subscriptions WHERE priority_status = 'requested'"),
    }

    # ── Risk ──
    risk = {
        'gross_exposure': gl_balance(CTAS_ADVANCES, 'debit'),        # advances still owed
        'members_owing': one("SELECT COUNT(*) FROM ctas_subscriptions WHERE advance_balance > 0"),
        'in_arrears': contributions['members_in_arrears'],
        'open_exceptions': one("SELECT COUNT(*) FROM ctas_exceptions WHERE status = 'open'"),
        'write_offs': gl_balance(CTAS_WRITEOFF, 'debit'),
        'suspended_mandates': one("SELECT COUNT(*) FROM ctas_mandates WHERE status IN ('suspended','expired')"),
    }
    risk['write_off_rate'] = (round(risk['write_offs'] / payouts['paid_value'] * 100, 2)
                              if payouts['paid_value'] else None)

    # ── Liquidity (aggregated across cycles still running) ──
    running = db.execute("SELECT * FROM ctas_cycles WHERE status IN "
                         "('open','closed','ready_for_ballot','balloted','active')").fetchall()
    liq_rows = []
    agg = {'reserve': 0.0, 'support': 0.0, 'shortfall': 0.0, 'future_payouts': 0.0}
    for cyc in running:
        rows, s = _liquidity(db, cyc)
        liq_rows.append({'cycle': cyc, 'summary': s})
        agg['reserve'] += s['reserve']
        agg['support'] += s['support']
        agg['shortfall'] += s['shortfall']
        agg['future_payouts'] += s['total_payouts']
    agg = {k: round(v, 2) for k, v in agg.items()}
    agg['pool_held'] = gl_balance(CTAS_POOL, 'credit')      # members' money currently in the pool

    # ── Profitability (straight from the ledger) ──
    admin_fees = gl_balance(CTAS_ADMIN_FEE_INCOME, 'credit')
    priority_fees = gl_balance(CTAS_PRIORITY_FEE_INCOME, 'credit')
    profit = {
        'admin_fees': admin_fees,
        'priority_fees': priority_fees,
        'total_income': round(admin_fees + priority_fees, 2),
        'write_offs': risk['write_offs'],
        'net': round(admin_fees + priority_fees - risk['write_offs'], 2),
    }

    # Honest about what is not modelled yet.
    not_tracked = ['Secured exposure (deposits, pledged savings, guarantors)',
                   'Guarantor exposure', 'Late-payment fee income',
                   'Investment income on the CTAS float', 'Bad-debt provision']

    return render_template('ctas/overview.html', membership=membership,
                           contributions=contributions, payouts=payouts, risk=risk,
                           liq=agg, liq_rows=liq_rows, profit=profit, not_tracked=not_tracked)


# ── Admin: plans (reusable product definitions) ───────────────────────────────

@ctas.route('/ctas/plans')
@ctas_required
@role_required('admin', 'treasurer')
def plans():
    db = get_db()
    rows = db.execute('''SELECT p.*, (SELECT COUNT(*) FROM ctas_cycles c WHERE c.plan_id = p.id) AS cycle_count
                         FROM ctas_plans p ORDER BY p.status = 'active' DESC, p.name''').fetchall()
    return render_template('ctas/plans.html', plans=rows)


@ctas.route('/ctas/plans', methods=['POST'])
@ctas_required
@role_required('admin', 'treasurer')
def new_plan():
    db = get_db()
    name = (request.form.get('name') or '').strip()
    if not name:
        flash('Plan name is required.', 'danger')
        return redirect(url_for('ctas.plans'))

    def _num(key, default):
        try:
            return float(request.form.get(key) or default)
        except ValueError:
            return float(default)

    frequency = (request.form.get('frequency') or 'monthly').strip()
    periods = int(_num('periods', 12))
    contribution = _num('contribution_amount', 0)
    target = ce.compute_target(contribution, periods)
    db.execute('''INSERT INTO ctas_plans
            (name, description, contribution_amount, frequency, periods, target_amount,
             monthly_capacity, earliest_payout_month, admin_fee_flat, admin_fee_percentage,
             admin_fee_cap, admin_fee_threshold, affordability_method, affordability_ratio,
             savings_multiple, grace_days, coverage_ratio, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (name, (request.form.get('description') or '').strip(), contribution, frequency, periods, target,
         int(_num('monthly_capacity', 1)), int(_num('earliest_payout_month', 1)),
         _num('admin_fee_flat', 0), _num('admin_fee_percentage', 0),
         _num('admin_fee_cap', 0), _num('admin_fee_threshold', 0),
         (request.form.get('affordability_method') or 'savings').strip(),
         _num('affordability_ratio', 0.5), _num('savings_multiple', 3),
         int(_num('grace_days', 7)),
         # Held as a fraction; the form asks for a percentage the coop chooses.
         max(0.0, min(200.0, _num('coverage_pct', 0))) / 100.0, current_user.id))
    audit(db, 'CTAS_PLAN_CREATE', 'ctas',
          f"Created plan {name} ({contribution:g} x {periods} {frequency}) target {target:g}")
    db.commit()
    flash(f'Plan "{name}" created — target ₦{target:,.2f}.', 'success')
    return redirect(url_for('ctas.plans'))


@ctas.route('/ctas/cycles/<int:cycle_id>')
@ctas_required
@role_required('admin', 'treasurer')
def cycle_detail(cycle_id):
    db = get_db()
    cycle = db.execute('SELECT * FROM ctas_cycles WHERE id = ?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    subs = db.execute('''
        SELECT s.*, m.first_name || ' ' || m.last_name AS member_name, m.member_number,
               m.total_savings, m.annual_salary
        FROM ctas_subscriptions s JOIN members m ON m.id = s.member_id
        WHERE s.cycle_id = ? ORDER BY s.payout_month, s.id
    ''', (cycle_id,)).fetchall()
    # Live eligibility note for still-submitted rows.
    elig = {}
    for s in subs:
        if s['status'] == ce.SUB_SUBMITTED:
            member = db.execute('SELECT * FROM members WHERE id = ?', (s['member_id'],)).fetchone()
            elig[s['id']] = ce.check_eligibility(db, member, cycle, s['target_amount'],
                                                 s['tenure_months'],
                                                 exclude_cycle_id=s['cycle_id'],
                                                 exclude_subscription_id=s['id'],
                                                 subscription_id=s['id'])
    # Members available to add (active, not already in this cycle).
    members = db.execute('''
        SELECT id, first_name || ' ' || last_name AS name, member_number, total_savings, annual_salary
        FROM members WHERE status = 'active'
          AND id NOT IN (SELECT member_id FROM ctas_subscriptions WHERE cycle_id = ?)
        ORDER BY first_name''', (cycle_id,)).fetchall()
    _refresh_schedule_statuses(db, cycle_id)
    db.commit()
    due_soon = db.execute('''
        SELECT sc.*, m.first_name || ' ' || m.last_name AS member_name, m.member_number
        FROM ctas_schedule sc
        JOIN ctas_subscriptions s ON s.id = sc.subscription_id
        JOIN members m ON m.id = s.member_id
        WHERE sc.cycle_id = ? AND sc.status IN ('due', 'grace', 'late', 'partial')
        ORDER BY sc.due_date, m.member_number''', (cycle_id,)).fetchall()
    summary = {
        'capacity': ce.total_capacity(cycle),
        'participants': ce.participant_count(db, cycle_id),
        'enrolled': sum(1 for s in subs if s['status'] == ce.SUB_ENROLLED),
        'outstanding_count': len(due_soon),
        'outstanding_amount': sum(float(r['expected_amount'] or 0) - float(r['paid_amount'] or 0)
                                  for r in due_soon),
    }
    liq_rows, liq = _liquidity(db, cycle)

    # What needs attention on this cycle right now — shown before anything else
    # so an officer does not have to read the whole page to find the blockers.
    pending_stages = [s for s in subs if s['status'] in ce.APPROVAL_ORDER[:-1]]
    attention = {
        'awaiting': len(pending_stages),
        'no_terms': sum(1 for s in subs if s['status'] in ce.APPROVAL_ORDER and not s['terms_accepted']),
        'priority_requests': sum(1 for s in subs if s['priority_status'] == 'requested'),
        'late': sum(1 for r in due_soon if r['status'] == 'late'),
        'ready_to_pay': sum(1 for s in subs if s['status'] == ce.SUB_SCHEDULED),
        'shortfall': liq['shortfall'],
    }
    attention['any'] = any([attention['awaiting'], attention['no_terms'],
                            attention['priority_requests'], attention['late'],
                            attention['ready_to_pay'], attention['shortfall'] > 0])
    # Members sitting at the same gate can be advanced together.
    next_gate = pending_stages[0]['status'] if pending_stages else None
    bulk_ready = [s['id'] for s in pending_stages
                  if s['status'] == next_gate and s['terms_accepted']] if next_gate else []

    # Who the security cover would hold back, worked out before the ballot runs
    # rather than discovered when it fails.
    security = _security_preview(db, cycle, subs)
    attention['under_covered'] = security['held_back']
    attention['any'] = attention['any'] or security['held_back'] > 0

    tab = (request.args.get('tab') or 'members').strip()
    if tab not in ('members', 'contributions', 'payouts', 'setup'):
        tab = 'members'
    return render_template('ctas/cycle_detail.html', cycle=cycle, subs=subs, elig=elig,
                           members=members, summary=summary, due_soon=due_soon, ce=ce,
                           priority_tiers=_priority_tiers(db, cycle_id=cycle_id),
                           liq_rows=liq_rows, liq=liq, ctas_terms=CTAS_TERMS,
                           active_tab=tab, attention=attention, bulk_ready=bulk_ready,
                           next_gate=next_gate, security=security)


def _security_preview(db, cycle, subs):
    """Per-member security cover for a cycle, plus whether the ballot can seat
    everyone. Read-only — the ballot recomputes and records the real figures."""
    ratio = ce.coverage_ratio_for(cycle)
    earliest = int(cycle['earliest_payout_month'] or 1)
    rows, held_back = [], 0
    for sub in subs:
        if sub['status'] not in (ce.SUB_ENROLLED, ce.SUB_SCHEDULED):
            continue
        member = db.execute('SELECT * FROM members WHERE id = ?', (sub['member_id'],)).fetchone()
        if not member:
            continue
        parts = ce.member_security(db, member, sub['id'])
        floor = ce.min_safe_position(cycle, parts['total'], sub['target_amount'])
        if floor > earliest:
            held_back += 1
        rows.append({'sub_id': sub['id'], 'name': f"{member['first_name']} {member['last_name']}",
                     'security': parts, 'floor': floor,
                     'exposure': ce.exposure_at_position(cycle, floor, sub['target_amount']),
                     'exposure_earliest': ce.exposure_at_position(cycle, earliest, sub['target_amount'])})

    # The same seating check the ballot applies, so the officer sees it early.
    infeasible = None
    if ratio > 0 and rows:
        cap = int(cycle['monthly_capacity'] or 1)
        slots = [p for p in range(earliest, ce.cycle_periods(cycle) + 1) for _ in range(cap)]
        for f in sorted({r['floor'] for r in rows}, reverse=True):
            need = sum(1 for r in rows if r['floor'] >= f)
            have = sum(1 for p in slots if p >= f)
            if need > have:
                infeasible = (f'{need} members need position {f} or later to meet the cover, '
                              f'but only {have} such positions exist.')
                break
    return {'ratio': ratio, 'rows': rows, 'held_back': held_back,
            'infeasible': infeasible, 'earliest': earliest}


def _advert_for_cycle(db, cycle):
    """Build a persuasive, numbers-accurate advert for an open cycle. Uses the
    communications engine's {placeholders}, which are filled per member."""
    contribution = float(cycle['contribution_amount'] or 0)
    periods = ce.cycle_periods(cycle)
    target = ce.compute_target(contribution, periods)
    word = ce.period_word(cycle)
    every = {'weekly': 'every week', 'fortnightly': 'every two weeks',
             'monthly': 'every month'}.get(cycle['frequency'] or 'monthly', f'every {word}')
    coop = (db.execute("SELECT value FROM settings WHERE key = 'coop_name'").fetchone() or {}).get('value', 'your cooperative')
    fee_bits = []
    if float(cycle['admin_fee_flat'] or 0):
        fee_bits.append(f"₦{float(cycle['admin_fee_flat']):,.0f} administrative fee")
    fee_line = (' A one-off ' + ' / '.join(fee_bits) + ' applies.') if fee_bits else ''
    ballot = f"\nBallot date: {cycle['ballot_date']}" if cycle['ballot_date'] else ''
    closes = f"\nEnrolment closes: {cycle['end_date']}" if cycle['end_date'] else ''

    subject = f"Get ₦{target:,.0f} — join {cycle['name']} (Target Advance)"
    body = (
        'Dear {first_name},\n\n'
        f"Saving up for something big takes time — {coop} can help you get there sooner.\n\n"
        f"*{cycle['name']}* is now open. Contribute *₦{contribution:,.0f} {every}* for "
        f"*{periods} {word}s*, and receive your full *₦{target:,.0f}* in a single lump sum on "
        f"your allocated {word} — which could be long before you have finished contributing.\n\n"
        "Why members like it:\n"
        f"• You get the full ₦{target:,.0f} at once — for school fees, rent, business stock, "
        "equipment or a family project.\n"
        "• No interest. You contribute the same amount you receive.\n"
        f"• You do not need to find other contributors — {coop} runs and backs the scheme.\n"
        "• Your payout position is decided by a transparent, recorded ballot — everyone has a fair chance.\n"
        "• Contributions are automatic and every transaction is on your member record.\n\n"
        f"How it works: contribute ₦{contribution:,.0f} {every} → the ballot assigns your payout "
        f"{word} → you collect ₦{target:,.0f} → you keep contributing until the cycle ends."
        f"{fee_line}{ballot}{closes}\n\n"
        'Places are limited and allocated by ballot, so apply before enrolment closes.\n\n'
        'Apply in your member portal: {portal_link}\n\n'
        'Member number: {member_number}\n'
        'Your savings balance: {savings_balance}\n\n'
        'Warm regards,\n'
        f'{coop}'
    )
    return subject, body


@ctas.route('/ctas/cycles/<int:cycle_id>/promote', methods=['GET', 'POST'])
@ctas_required
@role_required('admin', 'treasurer')
def promote_cycle(cycle_id):
    """Advertise an open cycle to members. GET previews the generated advert;
    POST creates a communications campaign (email now, WhatsApp when enabled)."""
    db = get_db()
    cycle = db.execute('SELECT * FROM ctas_cycles WHERE id = ?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    subject, body = _advert_for_cycle(db, cycle)
    if request.method == 'GET':
        return render_template('ctas/promote.html', cycle=cycle, subject=subject, body=body)

    subject = (request.form.get('subject') or subject).strip()
    body = (request.form.get('body') or body).strip()
    channel = (request.form.get('channel') or 'email').strip()
    audience = (request.form.get('audience') or 'active').strip()
    if channel != 'email':
        flash('Only email sending is enabled for now. WhatsApp will send once its '
              'consent/template setup is completed in Communications.', 'warning')
        return redirect(url_for('ctas.promote_cycle', cycle_id=cycle_id))

    from blueprints.communications import _members_for_audience, _process_campaign
    members = _members_for_audience(db, audience, None)
    db.execute('''INSERT INTO communication_campaigns
            (title, audience, channel, subject, body, status, recipient_count, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?)''',
        (f"CTAS advert — {cycle['name']}", audience, channel, subject, body,
         len(members), current_user.id, datetime.now()))
    campaign_id = last_insert_id(db)
    for m in members:
        db.execute('''INSERT INTO communication_recipients
                (campaign_id, member_id, channel, destination, status)
                VALUES (?, ?, ?, ?, 'pending')''',
            (campaign_id, m['id'], channel, (m['email'] if 'email' in m.keys() else '') or ''))
    audit(db, 'CTAS_PROMOTE', 'ctas',
          f"Advertised cycle {cycle['name']} to {len(members)} member(s) by {channel}")
    db.commit()
    try:
        _process_campaign(campaign_id)
    except Exception as exc:   # pragma: no cover - delivery is best-effort
        current_app.logger.warning('CTAS advert send failed: %s', exc)
    flash(f'Advert queued to {len(members)} member(s). Track delivery under Communications.', 'success')
    return redirect(url_for('communications.campaign_detail', campaign_id=campaign_id))


@ctas.route('/ctas/cycles/<int:cycle_id>/delete', methods=['POST'])
@ctas_required
@role_required('admin', 'treasurer')
def delete_cycle(cycle_id):
    """Delete a cycle and its subscriptions — for clearing test/abandoned cycles.
    Refused once money has moved (any payout or contribution posted to the GL),
    because those journals must stay for audit; void/settle those instead."""
    db = get_db()
    cycle = db.execute('SELECT * FROM ctas_cycles WHERE id = ?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    sub_ids = [r['id'] for r in db.execute(
        'SELECT id FROM ctas_subscriptions WHERE cycle_id = ?', (cycle_id,)).fetchall()]
    if sub_ids:
        placeholders = ','.join('?' for _ in sub_ids)
        posted = db.execute(
            f"SELECT COUNT(*) FROM journal_entries WHERE source_module IN "
            f"('ctas_payout','ctas_contribution','ctas_recovery','ctas_exit') "
            f"AND source_id IN ({placeholders})", sub_ids).fetchone()[0]
        if posted:
            flash('This cycle has money posted to the ledger and cannot be deleted. '
                  'Settle or complete its members instead.', 'danger')
            return redirect(url_for('ctas.cycle_detail', cycle_id=cycle_id))
        db.execute(f"DELETE FROM ctas_exceptions WHERE subscription_id IN ({placeholders})", sub_ids)
        db.execute(f"DELETE FROM ctas_payroll_lines WHERE subscription_id IN ({placeholders})", sub_ids)
    db.execute('DELETE FROM ctas_payroll_batches WHERE cycle_id = ?', (cycle_id,))
    db.execute('DELETE FROM ctas_ballot_runs WHERE cycle_id = ?', (cycle_id,))
    db.execute('DELETE FROM ctas_subscriptions WHERE cycle_id = ?', (cycle_id,))
    db.execute('DELETE FROM ctas_cycles WHERE id = ?', (cycle_id,))
    audit(db, 'CTAS_CYCLE_DELETE', 'ctas',
          f"Deleted cycle {cycle['name']} ({len(sub_ids)} subscription(s))")
    db.commit()
    flash(f'Cycle "{cycle["name"]}" deleted.', 'info')
    return redirect(url_for('ctas.dashboard'))


@ctas.route('/ctas/cycles/<int:cycle_id>/transition', methods=['POST'])
@ctas_required
@role_required('admin', 'treasurer')
def cycle_transition(cycle_id):
    db = get_db()
    cycle = db.execute('SELECT * FROM ctas_cycles WHERE id = ?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    to = (request.form.get('to') or '').strip()
    try:
        if not ce.can_transition(cycle['status'], to):
            raise ValueError(f'Cannot move a {cycle["status"]} cycle to {to}.')
        if to == ce.CYCLE_READY_FOR_BALLOT:
            ce.assert_ready_for_ballot(db, cycle)
            # Liquidity gate (spec section 14): do not commit to payouts the
            # cooperative cannot fund, unless an officer knowingly overrides.
            _, liq = _liquidity(db, cycle)
            if liq['shortfall'] > 0 and not request.form.get('override_liquidity'):
                raise ValueError(
                    f"Projected shortfall of ₦{liq['shortfall']:,.2f}: contributions plus the "
                    f"₦{liq['reserve']:,.2f} reserve and ₦{liq['support']:,.2f} approved support do not "
                    f"cover the scheduled payouts. Increase support, reduce payouts per period, "
                    f"or enrol more members — or tick 'proceed anyway' to accept the gap.")
            if liq['shortfall'] > 0:
                audit(db, 'CTAS_LIQUIDITY_OVERRIDE', 'ctas',
                      f"Cycle {cycle['name']}: proceeded to ballot with a projected shortfall of "
                      f"₦{liq['shortfall']:,.2f}")
        db.execute('UPDATE ctas_cycles SET status = ?, updated_at = ? WHERE id = ?',
                   (to, datetime.now(), cycle_id))
        audit(db, 'CTAS_CYCLE_TRANSITION', 'ctas', f"Cycle {cycle['name']}: {cycle['status']} -> {to}")
        db.commit()
        flash(f'Cycle moved to {to.replace("_", " ")}.', 'success')
    except ValueError as e:
        flash(str(e), 'danger')
    return redirect(url_for('ctas.cycle_detail', cycle_id=cycle_id))


# ── Admin: subscriptions ──────────────────────────────────────────────────────

@ctas.route('/ctas/cycles/<int:cycle_id>/subscriptions', methods=['POST'])
@ctas_required
@role_required('admin', 'treasurer')
def add_subscription(cycle_id):
    db = get_db()
    cycle = db.execute('SELECT * FROM ctas_cycles WHERE id = ?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    if cycle['status'] != ce.CYCLE_OPEN:
        flash('Members can only be added while the cycle is open.', 'warning')
        return redirect(url_for('ctas.cycle_detail', cycle_id=cycle_id))
    member = db.execute('SELECT * FROM members WHERE id = ?', (request.form.get('member_id'),)).fetchone()
    if not member:
        flash('Choose a member.', 'danger')
        return redirect(url_for('ctas.cycle_detail', cycle_id=cycle_id))
    try:
        target = finite_float(request.form.get('target_amount') or 0)
        tenure = int(request.form.get('tenure_months') or 0)
    except ValueError:
        flash('Enter a valid target amount and tenure.', 'danger')
        return redirect(url_for('ctas.cycle_detail', cycle_id=cycle_id))

    result = ce.check_eligibility(db, member, cycle, target, tenure)
    # Hard rules (dup subscription, bad numbers, capacity) block; affordability is advisory here.
    hard = [r for r in result['reasons'] if 'exceeds' not in r and 'salary' not in r.lower()]
    if hard:
        flash('Cannot add: ' + ' '.join(hard), 'danger')
        return redirect(url_for('ctas.cycle_detail', cycle_id=cycle_id))

    terms = 1 if request.form.get('terms') else 0
    signature = (request.form.get('signature_name') or '').strip()
    db.execute('''INSERT INTO ctas_subscriptions
            (cycle_id, member_id, target_amount, tenure_months, monthly_deduction, admin_fee,
             status, outstanding, terms_accepted, signature_name, signed_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, 'submitted', ?, ?, ?, ?, ?)''',
        (cycle_id, member['id'], target, tenure, result['monthly_deduction'],
         result['admin_fee'], target, terms, signature or None,
         datetime.now() if terms else None, current_user.id))
    audit(db, 'CTAS_SUBSCRIPTION_ADD', 'ctas',
          f"{member['first_name']} {member['last_name']} joined cycle {cycle['name']} (target {target})")
    db.commit()
    note = '' if result['eligible'] else ' (affordability flagged — review before enrolling)'
    flash(f'Subscription added for {member["first_name"]} {member["last_name"]}.{note}', 'success')
    return redirect(url_for('ctas.cycle_detail', cycle_id=cycle_id))


@ctas.route('/ctas/subscriptions/<int:sub_id>/act', methods=['POST'])
@ctas_required
@role_required('admin', 'treasurer')
def subscription_act(sub_id):
    db = get_db()
    sub = db.execute('SELECT * FROM ctas_subscriptions WHERE id = ?', (sub_id,)).fetchone()
    if not sub:
        abort(404)
    action = (request.form.get('action') or '').strip()
    now = datetime.now()
    detail = url_for('ctas.cycle_detail', cycle_id=sub['cycle_id'])

    # Officers who are also members may act on their own application, but it is
    # flagged and recorded — separation of duties is a judgement for the coop.
    own = member_for_user(db, current_user.id)
    acting_on_own = bool(own and own['id'] == sub['member_id'])
    if acting_on_own and action in ('advance', 'reject'):
        flash('Note: this is your own target-advance application. The action has been '
              'recorded for audit — where possible another officer should review it.', 'warning')
        audit(db, 'CTAS_SELF_REVIEW', 'ctas',
              f"{current_user.username} acted ({action}) on their own subscription #{sub_id}")

    if action == 'reject' and sub['status'] in ce.APPROVAL_ORDER:
        reason = (request.form.get('reason') or '').strip()
        db.execute("UPDATE ctas_subscriptions SET status = 'rejected', rejected_reason = ? WHERE id = ?",
                   (reason, sub_id))
        _notify_member(db, sub['member_id'], 'Target Advance — application declined',
                       'Your target-advance application was not approved.'
                       + (f' Reason: {reason}' if reason else ''))
        flash('Application declined.', 'info')

    elif action == 'back' and sub['status'] in ce.PREV_STAGE:
        db.execute("UPDATE ctas_subscriptions SET status = ? WHERE id = ?",
                   (ce.PREV_STAGE[sub['status']], sub_id))
        flash('Moved back one step.', 'info')

    elif action == 'advance':
        nxt = ce.next_stage(sub['status'])
        if not nxt:
            flash('This application is already fully approved and enrolled.', 'warning')
            return redirect(detail)
        # Each gate needs its own duty, so the stages can be split between officers.
        needed = ce.STAGE_PERMISSION.get(nxt, 'ctas.manage')
        if not perms.user_can(needed):
            flash(f"You do not have permission to {ce.STAGE_DUTY_LABEL.get(nxt, 'do this')}. "
                  f"An officer holding that duty must act — see Settings → Task Assignment.", 'danger')
            return redirect(detail)
        # A member must have accepted the scheme terms before they are enrolled —
        # the terms carry the contribution, recovery, credit and data consents.
        if nxt == ce.SUB_ENROLLED and not sub['terms_accepted']:
            flash('This member has not accepted the scheme terms yet, so they cannot be enrolled. '
                  'Ask them to accept in their member portal, or record their signed acceptance '
                  'using "Record terms acceptance" on this page.', 'danger')
            return redirect(detail)
        # Re-run eligibility at the eligibility gate and again at enrolment.
        if nxt in (ce.SUB_ELIGIBLE, ce.SUB_ENROLLED):
            cycle = db.execute('SELECT * FROM ctas_cycles WHERE id = ?', (sub['cycle_id'],)).fetchone()
            member = db.execute('SELECT * FROM members WHERE id = ?', (sub['member_id'],)).fetchone()
            res = ce.check_eligibility(db, member, cycle, sub['target_amount'], sub['tenure_months'],
                                       exclude_cycle_id=sub['cycle_id'],
                                       exclude_subscription_id=sub['id'],
                                       subscription_id=sub['id'])
            hard = [r for r in res['reasons'] if 'exceeds' not in r and 'salary' not in r.lower()]
            if hard:
                flash('Cannot advance: ' + ' '.join(hard), 'danger')
                return redirect(detail)
        if nxt == ce.SUB_ELIGIBLE:
            db.execute("UPDATE ctas_subscriptions SET status = ?, eligibility_at = ?, eligibility_by = ? "
                       "WHERE id = ?", (nxt, now, current_user.id, sub_id))
        elif nxt == ce.SUB_FINANCE_REVIEWED:
            db.execute("UPDATE ctas_subscriptions SET status = ?, finance_reviewed_at = ?, "
                       "finance_reviewed_by = ? WHERE id = ?", (nxt, now, current_user.id, sub_id))
        elif nxt == ce.SUB_APPROVED:
            db.execute("UPDATE ctas_subscriptions SET status = ?, approved_at = ?, approved_by = ? "
                       "WHERE id = ?", (nxt, now, current_user.id, sub_id))
            _notify_member(db, sub['member_id'], 'Target Advance — approved',
                           'Your application is approved. You will be enrolled for the ballot.')
        elif nxt == ce.SUB_ENROLLED:
            db.execute("UPDATE ctas_subscriptions SET status = ?, enrolled_at = ? WHERE id = ?",
                       (nxt, now, sub_id))
            # Enrolment commits the member to contribute every period — lay out
            # their due dates now so arrears and reminders have something to work from.
            cyc = db.execute('SELECT * FROM ctas_cycles WHERE id = ?', (sub['cycle_id'],)).fetchone()
            _ensure_schedule(db, sub, cyc)
            _notify_member(db, sub['member_id'], 'Target Advance — enrolled',
                           'You are enrolled. The ballot will assign your payout month.')
        flash(f'Advanced to {nxt.replace("_", " ")}.', 'success')

    else:
        flash('That action is not allowed for this application right now.', 'warning')
        return redirect(detail)

    audit(db, 'CTAS_SUBSCRIPTION_ACT', 'ctas', f"Subscription #{sub_id}: {action}")
    db.commit()
    return redirect(detail)


# ── Priority payout: fee tiers, member requests, officer decisions ────────────

@ctas.route('/ctas/cycles/<int:cycle_id>/liquidity', methods=['POST'])
@ctas_required
@role_required('admin', 'treasurer')
def set_liquidity(cycle_id):
    """Record what the cooperative has reserved for this cycle and how much it
    has approved to lend the pool (the guarantee)."""
    db = get_db()
    if not db.execute('SELECT 1 FROM ctas_cycles WHERE id = ?', (cycle_id,)).fetchone():
        abort(404)

    def _num(key):
        try:
            return max(0.0, float(request.form.get(key) or 0))
        except ValueError:
            return 0.0

    db.execute('UPDATE ctas_cycles SET liquidity_reserve = ?, liquidity_support = ?, '
               'liquidity_buffer = ? WHERE id = ?',
               (_num('liquidity_reserve'), _num('liquidity_support'), _num('liquidity_buffer'), cycle_id))
    audit(db, 'CTAS_LIQUIDITY', 'ctas',
          f"Cycle #{cycle_id}: reserve {_num('liquidity_reserve')}, support {_num('liquidity_support')}")
    db.commit()
    flash('Liquidity settings saved.', 'success')
    return redirect(url_for('ctas.cycle_detail', cycle_id=cycle_id, tab='setup'))


@ctas.route('/ctas/cycles/<int:cycle_id>/security', methods=['POST'])
@ctas_required
@role_required('admin', 'treasurer')
def set_security(cycle_id):
    """Set how much of a member's exposure must be backed by value the
    cooperative can reach. Applied when the ballot runs, so it can be changed
    right up to that point."""
    db = get_db()
    cycle = db.execute('SELECT * FROM ctas_cycles WHERE id = ?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    if cycle['status'] in (ce.CYCLE_BALLOTED, ce.CYCLE_COMPLETED):
        flash('The ballot has already run — the cover requirement no longer applies.', 'warning')
        return redirect(url_for('ctas.cycle_detail', cycle_id=cycle_id, tab='setup'))
    try:
        pct = max(0.0, min(200.0, float(request.form.get('coverage_pct') or 0)))
    except ValueError:
        pct = 0.0
    db.execute('UPDATE ctas_cycles SET coverage_ratio = ? WHERE id = ?', (pct / 100.0, cycle_id))
    audit(db, 'CTAS_SECURITY', 'ctas', f"Cycle #{cycle_id}: security cover set to {pct:g}%")
    db.commit()
    flash('Security cover requirement saved. It is applied when the ballot runs.'
          if pct > 0 else 'Security cover turned off for this cycle.', 'success')
    return redirect(url_for('ctas.cycle_detail', cycle_id=cycle_id, tab='setup'))


@ctas.route('/ctas/cycles/<int:cycle_id>/priority-fees', methods=['POST'])
@ctas_required
@role_required('admin', 'treasurer')
def set_priority_fees(cycle_id):
    """Price each early payout position (earlier positions normally cost more)."""
    db = get_db()
    cycle = db.execute('SELECT * FROM ctas_cycles WHERE id = ?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    enabled = 1 if request.form.get('priority_enabled') else 0
    db.execute('UPDATE ctas_cycles SET priority_enabled = ? WHERE id = ?', (enabled, cycle_id))
    db.execute('DELETE FROM ctas_priority_fees WHERE cycle_id = ?', (cycle_id,))
    positions = request.form.getlist('position')
    fees = request.form.getlist('fee')
    saved = 0
    for i, pos in enumerate(positions):
        try:
            p, f = int(pos), float(fees[i] if i < len(fees) else 0)
        except (ValueError, IndexError):
            continue
        if p <= 0 or f <= 0:
            continue
        db.execute('INSERT INTO ctas_priority_fees (cycle_id, position, fee) VALUES (?, ?, ?)',
                   (cycle_id, p, f))
        saved += 1
    audit(db, 'CTAS_PRIORITY_FEES', 'ctas',
          f"Cycle {cycle['name']}: priority {'on' if enabled else 'off'}, {saved} priced position(s)")
    db.commit()
    flash(f"Priority payout {'enabled' if enabled else 'disabled'} — {saved} position(s) priced.", 'success')
    return redirect(url_for('ctas.cycle_detail', cycle_id=cycle_id, tab='setup'))


@ctas.route('/my-ctas/<int:sub_id>/priority', methods=['POST'])
@ctas_required
@login_required
def request_priority(sub_id):
    """A member asks for an earlier payout position, accepting the fee."""
    db = get_db()
    member = member_for_user(db, current_user.id)
    sub = db.execute('SELECT * FROM ctas_subscriptions WHERE id = ?', (sub_id,)).fetchone()
    if not sub or not member or sub['member_id'] != member['id']:
        abort(403)
    cycle = db.execute('SELECT * FROM ctas_cycles WHERE id = ?', (sub['cycle_id'],)).fetchone()
    if not cycle['priority_enabled']:
        flash('Priority payout is not offered on this cycle.', 'warning')
        return redirect(url_for('ctas.my_ctas'))
    if sub['payout_month']:
        flash('The ballot has already run, so positions can no longer be requested.', 'warning')
        return redirect(url_for('ctas.my_ctas'))
    if sub['status'] not in (ce.SUB_SUBMITTED, ce.SUB_ELIGIBLE, ce.SUB_FINANCE_REVIEWED,
                             ce.SUB_APPROVED, ce.SUB_ENROLLED):
        flash('Priority can only be requested before the ballot.', 'warning')
        return redirect(url_for('ctas.my_ctas'))
    try:
        position = int(request.form.get('position') or 0)
    except ValueError:
        position = 0
    tiers = _priority_tiers(db, cycle_id=cycle['id'])
    if position not in tiers:
        flash('Choose one of the priority positions offered.', 'danger')
        return redirect(url_for('ctas.my_ctas'))
    if not request.form.get('accept_fee'):
        flash('Please accept the priority fee to continue.', 'danger')
        return redirect(url_for('ctas.my_ctas'))

    fee = ce.priority_fee_for(tiers, position)
    db.execute("UPDATE ctas_subscriptions SET is_priority = 1, requested_payout_month = ?, "
               "priority_fee = ?, priority_status = 'requested', priority_decided_at = NULL, "
               "priority_note = NULL WHERE id = ?", (position, fee, sub_id))
    audit(db, 'CTAS_PRIORITY_REQUEST', 'ctas',
          f"Subscription #{sub_id} requested position {position} (fee {fee})")
    db.commit()
    flash(f'Priority request submitted for position {position} (fee ₦{fee:,.2f}). '
          f'The cooperative will review it before the ballot.', 'success')
    return redirect(url_for('ctas.my_ctas'))


@ctas.route('/ctas/cycles/<int:cycle_id>/bulk-advance', methods=['POST'])
@ctas_required
@role_required('admin', 'treasurer')
def bulk_advance(cycle_id):
    """Advance several members through the same gate in one go. Each is still
    checked individually — the duty, the terms and eligibility all apply — so
    this saves clicks without weakening any control."""
    db = get_db()
    cycle = db.execute('SELECT * FROM ctas_cycles WHERE id = ?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    ids = [i for i in request.form.getlist('sub_ids') if str(i).isdigit()]
    if not ids:
        flash('Select at least one member to advance.', 'warning')
        return redirect(url_for('ctas.cycle_detail', cycle_id=cycle_id, tab='members'))

    advanced, blocked = 0, []
    now = datetime.now()
    for sub_id in ids:
        sub = db.execute('SELECT * FROM ctas_subscriptions WHERE id = ? AND cycle_id = ?',
                         (sub_id, cycle_id)).fetchone()
        if not sub:
            continue
        nxt = ce.next_stage(sub['status'])
        member = db.execute('SELECT * FROM members WHERE id = ?', (sub['member_id'],)).fetchone()
        name = f"{member['first_name']} {member['last_name']}" if member else f"#{sub_id}"
        if not nxt:
            blocked.append(f'{name}: already enrolled')
            continue
        if not perms.user_can(ce.STAGE_PERMISSION.get(nxt, 'ctas.manage')):
            blocked.append(f"{name}: you cannot {ce.STAGE_DUTY_LABEL.get(nxt, 'act')}")
            continue
        if nxt == ce.SUB_ENROLLED and not sub['terms_accepted']:
            blocked.append(f'{name}: has not accepted the terms')
            continue
        if nxt in (ce.SUB_ELIGIBLE, ce.SUB_ENROLLED):
            res = ce.check_eligibility(db, member, cycle, sub['target_amount'], sub['tenure_months'],
                                       exclude_cycle_id=cycle_id, exclude_subscription_id=sub['id'],
                                       subscription_id=sub['id'])
            hard = [r for r in res['reasons'] if 'exceeds' not in r and 'salary' not in r.lower()]
            if hard:
                blocked.append(f"{name}: {' '.join(hard)}")
                continue
        if nxt == ce.SUB_ELIGIBLE:
            db.execute("UPDATE ctas_subscriptions SET status = ?, eligibility_at = ?, "
                       "eligibility_by = ? WHERE id = ?", (nxt, now, current_user.id, sub_id))
        elif nxt == ce.SUB_FINANCE_REVIEWED:
            db.execute("UPDATE ctas_subscriptions SET status = ?, finance_reviewed_at = ?, "
                       "finance_reviewed_by = ? WHERE id = ?", (nxt, now, current_user.id, sub_id))
        elif nxt == ce.SUB_APPROVED:
            db.execute("UPDATE ctas_subscriptions SET status = ?, approved_at = ?, approved_by = ? "
                       "WHERE id = ?", (nxt, now, current_user.id, sub_id))
            _notify_member(db, sub['member_id'], 'Target Advance — approved',
                           'Your application is approved. You will be enrolled for the ballot.')
        elif nxt == ce.SUB_ENROLLED:
            db.execute("UPDATE ctas_subscriptions SET status = ?, enrolled_at = ? WHERE id = ?",
                       (nxt, now, sub_id))
            _ensure_schedule(db, sub, cycle)
            _notify_member(db, sub['member_id'], 'Target Advance — enrolled',
                           'You are enrolled. The ballot will assign your payout month.')
        advanced += 1

    audit(db, 'CTAS_BULK_ADVANCE', 'ctas',
          f"Cycle {cycle['name']}: advanced {advanced}, skipped {len(blocked)}")
    db.commit()
    if advanced:
        flash(f'Advanced {advanced} member(s).', 'success')
    for msg in blocked[:5]:
        flash(f'Skipped — {msg}', 'warning')
    if len(blocked) > 5:
        flash(f'{len(blocked) - 5} more were skipped.', 'warning')
    return redirect(url_for('ctas.cycle_detail', cycle_id=cycle_id, tab='members'))


@ctas.route('/ctas/subscriptions/<int:sub_id>/terms', methods=['POST'])
@ctas_required
@role_required('admin', 'treasurer')
def record_terms(sub_id):
    """Record that a member accepted the scheme terms offline (signed form).
    The officer attests to it and their attestation is audited — this is not the
    officer consenting on the member's behalf."""
    db = get_db()
    sub = db.execute('SELECT * FROM ctas_subscriptions WHERE id = ?', (sub_id,)).fetchone()
    if not sub:
        abort(404)
    signature = (request.form.get('signature_name') or '').strip()
    if not signature or not request.form.get('attest'):
        flash('Enter the name the member signed with and confirm you hold their signed acceptance.',
              'danger')
        return redirect(url_for('ctas.cycle_detail', cycle_id=sub['cycle_id']))
    db.execute("UPDATE ctas_subscriptions SET terms_accepted = 1, signature_name = ?, signed_at = ? "
               "WHERE id = ?", (signature, datetime.now(), sub_id))
    audit(db, 'CTAS_TERMS_RECORDED', 'ctas',
          f"{current_user.username} recorded signed terms acceptance for subscription #{sub_id} "
          f"(signed: {signature})")
    db.commit()
    flash(f'Recorded terms acceptance signed by {signature}.', 'success')
    return redirect(url_for('ctas.cycle_detail', cycle_id=sub['cycle_id']))


@ctas.route('/ctas/subscriptions/<int:sub_id>/priority', methods=['POST'])
@ctas_required
@role_required('admin', 'treasurer')
def decide_priority(sub_id):
    """Grant or decline a priority request. Granting does not bypass the rules —
    the member must already be eligible, and if several want the same position
    the ballot decides between them."""
    db = get_db()
    sub = db.execute('SELECT * FROM ctas_subscriptions WHERE id = ?', (sub_id,)).fetchone()
    if not sub:
        abort(404)
    action = (request.form.get('action') or '').strip()
    note = (request.form.get('note') or '').strip()
    if sub['payout_month']:
        flash('The ballot has already run for this member.', 'warning')
        return redirect(url_for('ctas.cycle_detail', cycle_id=sub['cycle_id']))

    if action == 'grant':
        db.execute("UPDATE ctas_subscriptions SET priority_status = 'granted', priority_decided_at = ?, "
                   "priority_note = ? WHERE id = ?", (datetime.now(), note, sub_id))
        _notify_member(db, sub['member_id'], 'Target Advance — priority approved',
                       f"Your request for payout position {sub['requested_payout_month']} was approved. "
                       f"A fee of ₦{float(sub['priority_fee'] or 0):,.2f} applies, deducted from your payout. "
                       f"If more members want the same position, the ballot decides between you.")
        flash('Priority request granted.', 'success')
    elif action == 'decline':
        db.execute("UPDATE ctas_subscriptions SET priority_status = 'declined', is_priority = 0, "
                   "priority_fee = 0, priority_decided_at = ?, priority_note = ? WHERE id = ?",
                   (datetime.now(), note, sub_id))
        _notify_member(db, sub['member_id'], 'Target Advance — priority not approved',
                       'Your priority request was not approved, so you join the normal ballot. '
                       'No priority fee will be charged.' + (f' {note}' if note else ''))
        flash('Priority request declined — the member joins the normal ballot.', 'info')
    else:
        flash('Unknown action.', 'warning')
    audit(db, 'CTAS_PRIORITY_DECIDE', 'ctas', f"Subscription #{sub_id}: priority {action}")
    db.commit()
    return redirect(url_for('ctas.cycle_detail', cycle_id=sub['cycle_id']))


# ── Admin: ballot + payout ────────────────────────────────────────────────────

@ctas.route('/ctas/cycles/<int:cycle_id>/ballot', methods=['POST'])
@ctas_required
@role_required('admin', 'treasurer')
def run_ballot(cycle_id):
    db = get_db()
    cycle = db.execute('SELECT * FROM ctas_cycles WHERE id = ?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    try:
        if cycle['status'] != ce.CYCLE_READY_FOR_BALLOT:
            raise ValueError('Move the cycle to "ready for ballot" first.')
        enrolled = db.execute("SELECT id FROM ctas_subscriptions WHERE cycle_id = ? AND status = 'enrolled'",
                              (cycle_id,)).fetchall()
        ids = [r['id'] for r in enrolled]
        seed = secrets.token_hex(8)
        # Granted priority requests are placed first; oversubscribed positions
        # are decided by the same seeded ballot.
        granted = {r['id']: int(r['requested_payout_month'])
                   for r in db.execute(
                       "SELECT id, requested_payout_month FROM ctas_subscriptions "
                       "WHERE cycle_id = ? AND status = 'enrolled' AND priority_status = 'granted' "
                       "AND requested_payout_month IS NOT NULL", (cycle_id,)).fetchall()}
        # Security cover: a member whose recoverable value is thin for the
        # earliest positions is held back to one the cover reaches. Recorded on
        # the subscription so the ballot outcome can be explained afterwards.
        floors = {}
        for sub in db.execute(
                "SELECT s.id, s.target_amount, s.member_id FROM ctas_subscriptions s "
                "WHERE s.cycle_id = ? AND s.status = 'enrolled'", (cycle_id,)).fetchall():
            member = db.execute('SELECT * FROM members WHERE id = ?', (sub['member_id'],)).fetchone()
            if not member:
                continue
            security = ce.member_security(db, member, sub['id'])
            floor = ce.min_safe_position(cycle, security['total'], sub['target_amount'])
            floors[sub['id']] = floor
            db.execute("UPDATE ctas_subscriptions SET security_value = ?, min_payout_position = ? "
                       "WHERE id = ?", (security['total'], floor, sub['id']))

        assignments = ce.assign_payout_months(ids, cycle, seed, priority=granted, floors=floors)
        for sub_id, month in assignments.items():
            db.execute("UPDATE ctas_subscriptions SET status = 'scheduled', payout_month = ?, "
                       "ballot_assigned_at = ? WHERE id = ?", (month, datetime.now(), sub_id))
            # A member who asked for (and was granted) priority but did not win
            # that position pays no priority fee — they were balloted normally.
            if sub_id in granted and granted[sub_id] != month:
                db.execute("UPDATE ctas_subscriptions SET is_priority = 0, priority_fee = 0, "
                           "priority_status = 'not_allocated' WHERE id = ?", (sub_id,))
            row = db.execute('SELECT member_id FROM ctas_subscriptions WHERE id = ?', (sub_id,)).fetchone()
            extra = ''
            if sub_id in granted:
                extra = (' Your priority position was allocated.' if granted[sub_id] == month
                         else ' Your priority position was oversubscribed, so you were balloted '
                              'normally and no priority fee is charged.')
            _notify_member(db, row['member_id'], 'Target Advance — ballot result',
                           f'The ballot is complete. Your advance is scheduled for month {month} '
                           f'of the cycle.{extra}')
        summary = f"{len(ids)} member(s) assigned payout months (seed {seed})."
        db.execute("INSERT INTO ctas_ballot_runs (cycle_id, seed, summary, executed_by) VALUES (?, ?, ?, ?)",
                   (cycle_id, seed, summary, current_user.id))
        db.execute("UPDATE ctas_cycles SET status = 'balloted', updated_at = ? WHERE id = ?",
                   (datetime.now(), cycle_id))
        audit(db, 'CTAS_BALLOT', 'ctas', f"Cycle {cycle['name']}: {summary}")
        db.commit()
        flash(f'Ballot complete — {summary}', 'success')
    except ValueError as e:
        db.rollback()
        flash(str(e), 'danger')
    return redirect(url_for('ctas.cycle_detail', cycle_id=cycle_id))


@ctas.route('/ctas/subscriptions/<int:sub_id>/payout', methods=['POST'])
@ctas_required
@role_required('admin', 'treasurer')
def payout(sub_id):
    db = get_db()
    sub = db.execute('SELECT * FROM ctas_subscriptions WHERE id = ?', (sub_id,)).fetchone()
    if not sub:
        abort(404)
    if sub['status'] != ce.SUB_SCHEDULED:
        flash('Only a scheduled (balloted) subscription can be paid out.', 'warning')
        return redirect(url_for('ctas.cycle_detail', cycle_id=sub['cycle_id']))
    member = db.execute('SELECT * FROM members WHERE id = ?', (sub['member_id'],)).fetchone()
    ctas_ensure_accounts(db)
    target = float(sub['target_amount'])
    admin_fee = float(sub['admin_fee'] or 0)

    # Coop-wide ceiling on money out on advance at any one time. Set
    # `ctas_max_net_exposure` in settings; 0 or unset leaves it uncapped.
    cap = _ctas_max_exposure(db)
    if cap > 0:
        outstanding = _ctas_advances_outstanding(db)
        adding = round(max(0.0, target - float(sub['contributed_total'] or 0)), 2)
        if round(outstanding + adding, 2) > cap:
            flash(f'This payout would take advances outstanding to '
                  f'₦{outstanding + adding:,.2f}, above the cooperative’s limit of '
                  f'₦{cap:,.2f}. Collect contributions, or raise the limit in settings, '
                  f'before paying out.', 'danger')
            return redirect(url_for('ctas.cycle_detail', cycle_id=sub['cycle_id'], tab='payouts'))
    prio_fee = float(sub['priority_fee'] or 0) if sub['is_priority'] else 0.0
    fee = round(admin_fee + prio_fee, 2)
    contributed = float(sub['contributed_total'] or 0)
    # Their own pooled contributions cover part of the payout; the co-op advances
    # the rest (recovered from their remaining contributions).
    pool_portion = round(min(contributed, target), 2)
    advance_portion = round(target - pool_portion, 2)
    cash = get_default_cash_account(db)
    lines = []
    if pool_portion > 0:
        lines.append({'account': CTAS_POOL, 'debit': pool_portion, 'memo': "CTAS payout from member's pool"})
    if advance_portion > 0:
        lines.append({'account': CTAS_ADVANCES, 'debit': advance_portion,
                      'memo': f"CTAS advance to {member['first_name']} {member['last_name']}"})
    lines.append({'account': cash, 'credit': round(target - fee, 2), 'memo': 'CTAS payout disbursed'})
    if admin_fee:
        lines.append({'account': CTAS_ADMIN_FEE_INCOME, 'credit': admin_fee, 'memo': 'CTAS admin fee'})
    if prio_fee:
        lines.append({'account': CTAS_PRIORITY_FEE_INCOME, 'credit': prio_fee,
                      'memo': f"CTAS priority fee (position {sub['payout_month']})"})
    try:
        post_journal_safe(db, f"CTAS payout - subscription {sub_id}", lines,
                          date=datetime.now(), reference=f"CTAS-PO-{sub_id}",
                          source_module='ctas_payout', source_id=sub_id, created_by=current_user.id)
        db.execute("UPDATE ctas_subscriptions SET status = 'active_recovery', paid_out_at = ?, "
                   "payout_date = ?, advance_balance = ? WHERE id = ?",
                   (datetime.now(), datetime.now().date(), advance_portion, sub_id))
        _notify_member(db, sub['member_id'], 'Target Advance — paid out',
                       f"Your target of ₦{target - fee:,.2f} has been disbursed. Keep contributing "
                       f"₦{float(sub['monthly_deduction'] or 0):,.2f} each period until the cycle completes.")
        audit(db, 'CTAS_PAYOUT', 'ctas',
              f"Paid out ₦{target:,.2f} to {member['first_name']} {member['last_name']} (sub #{sub_id})")
        db.commit()
        flash(f'Paid out ₦{target - fee:,.2f} (net of ₦{fee:,.2f} admin fee). Recovery has started.', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Could not post the payout: {e}', 'danger')
    return redirect(url_for('ctas.cycle_detail', cycle_id=sub['cycle_id'], tab='payouts'))


# ── Payroll recovery ──────────────────────────────────────────────────────────

@ctas.route('/ctas/cycles/<int:cycle_id>/payroll/export')
@ctas_required
@role_required('admin', 'treasurer')
def payroll_export(cycle_id):
    """Download the contribution schedule for a period. In the rotating pool
    EVERY active member contributes the fixed amount each period — whether or not
    they have already had their payout."""
    db = get_db()
    cycle = db.execute('SELECT * FROM ctas_cycles WHERE id = ?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    try:
        month = max(1, int(request.args.get('month') or 1))
    except ValueError:
        month = 1
    contribution = float(cycle['contribution_amount'] or 0)
    subs = db.execute('''
        SELECT s.id, s.monthly_deduction, s.status, m.member_number, m.employee_id,
               m.first_name || ' ' || m.last_name AS name
        FROM ctas_subscriptions s JOIN members m ON m.id = s.member_id
        WHERE s.cycle_id = ? AND s.status IN ('enrolled', 'scheduled', 'active_recovery')
        ORDER BY m.member_number''', (cycle_id,)).fetchall()
    out = StringIO()
    w = csv.writer(out)
    w.writerow(['subscription_id', 'member_number', 'employee_id', 'name', 'status', 'period',
                'expected_amount', 'actual_amount'])
    for s in subs:
        exp = contribution or float(s['monthly_deduction'] or 0)
        w.writerow([s['id'], s['member_number'] or '', s['employee_id'] or '', s['name'],
                    s['status'], month, f"{exp:.2f}", ''])
    if not subs:
        # Still hand back a usable file, with an example row showing the format.
        w.writerow(['', 'MEM/2026/0001', 'EMP001', 'Example Member', 'enrolled', month,
                    f"{contribution:.2f}", ''])
    db.execute("INSERT INTO ctas_payroll_batches (cycle_id, month_number, kind, created_by) "
               "VALUES (?, ?, 'export', ?)", (cycle_id, month, current_user.id))
    db.commit()
    resp = make_response(out.getvalue())
    resp.headers['Content-Type'] = 'text/csv'
    resp.headers['Content-Disposition'] = f'attachment; filename=ctas_contributions_{cycle_id}_period{month}.csv'
    return resp


@ctas.route('/ctas/cycles/<int:cycle_id>/payroll/import', methods=['POST'])
@ctas_required
@role_required('admin', 'treasurer')
def payroll_import(cycle_id):
    """Import confirmed contributions for a period. In the rotating pool, a
    not-yet-paid member's contribution funds the pool (CR CTAS Pool); a paid-out
    member's contribution repays the co-op's advance (CR CTAS Advances).
    Idempotent per subscription+period."""
    db = get_db()
    cycle = db.execute('SELECT * FROM ctas_cycles WHERE id = ?', (cycle_id,)).fetchone()
    if not cycle:
        abort(404)
    try:
        month = max(1, int(request.form.get('month') or 1))
    except ValueError:
        month = 1
    file = request.files.get('file')
    if not file or not file.filename:
        flash('Choose a contributions CSV to import.', 'danger')
        return redirect(url_for('ctas.cycle_detail', cycle_id=cycle_id))

    ctas_ensure_accounts(db)
    cash = get_default_cash_account(db)
    contribution = float(cycle['contribution_amount'] or 0)
    db.execute("INSERT INTO ctas_payroll_batches (cycle_id, month_number, kind, file_name, processed, created_by) "
               "VALUES (?, ?, 'import', ?, 1, ?)", (cycle_id, month, file.filename, current_user.id))
    batch_id = last_insert_id(db)

    posted = missed = skipped = 0
    try:
        reader = csv.DictReader(TextIOWrapper(file, encoding='utf-8-sig'))
        for row in reader:
            sub = None
            sid = (row.get('subscription_id') or '').strip()
            if sid.isdigit():
                sub = db.execute("SELECT * FROM ctas_subscriptions WHERE id = ? AND cycle_id = ?",
                                 (int(sid), cycle_id)).fetchone()
            if not sub:
                mn = (row.get('member_number') or '').strip()
                if mn:
                    sub = db.execute(
                        "SELECT s.* FROM ctas_subscriptions s JOIN members m ON m.id = s.member_id "
                        "WHERE m.member_number = ? AND s.cycle_id = ? AND s.status IN ('enrolled','scheduled','active_recovery')",
                        (mn, cycle_id)).fetchone()
            if not sub or sub['status'] not in ('enrolled', 'scheduled', 'active_recovery'):
                skipped += 1
                continue
            # Idempotent per subscription+period.
            dup = db.execute(
                "SELECT 1 FROM ctas_payroll_lines l JOIN ctas_payroll_batches b ON b.id = l.batch_id "
                "WHERE l.subscription_id = ? AND b.month_number = ? AND b.kind = 'import' "
                "AND l.status IN ('deducted','partial')", (sub['id'], month)).fetchone()
            if dup:
                skipped += 1
                continue

            expected = contribution or float(sub['monthly_deduction'] or 0)
            try:
                actual = float((row.get('actual_amount') or row.get('amount') or 0) or 0)
            except ValueError:
                actual = 0.0
            if actual <= 0:
                db.execute("INSERT INTO ctas_payroll_lines (batch_id, subscription_id, expected_amount, "
                           "actual_amount, status) VALUES (?, ?, ?, 0, 'missed')",
                           (batch_id, sub['id'], expected))
                db.execute("UPDATE ctas_subscriptions SET arrears_amount = COALESCE(arrears_amount, 0) + ? "
                           "WHERE id = ?", (expected, sub['id']))
                _raise_exception(db, sub['id'], 'missed_deduction', month, expected,
                                 f'Missed contribution (period {month}): ₦{expected:,.2f} not received.')
                _notify_member(db, sub['member_id'], 'Target Advance — missed contribution',
                               f'Your period {month} contribution of ₦{expected:,.2f} was not received. '
                               f'Please pay to avoid arrears.')
                missed += 1
                continue

            shortfall = round(max(0.0, expected - actual), 2)
            over = round(max(0.0, actual - expected), 2)
            new_arrears = round(max(0.0, float(sub['arrears_amount'] or 0) + shortfall - over), 2)

            _post_contribution(db, sub, cycle, month, actual, created_by=current_user.id)
            db.execute("UPDATE ctas_subscriptions SET arrears_amount = ? WHERE id = ?",
                       (new_arrears, sub['id']))
            db.execute("INSERT INTO ctas_payroll_lines (batch_id, subscription_id, expected_amount, "
                       "actual_amount, status) VALUES (?, ?, ?, ?, ?)",
                       (batch_id, sub['id'], expected, actual, 'partial' if shortfall > 0 else 'deducted'))
            if shortfall > 0:
                _raise_exception(db, sub['id'], 'missed_deduction', month, shortfall,
                                 f'Partial contribution (period {month}): short by ₦{shortfall:,.2f}.')
                _notify_member(db, sub['member_id'], 'Target Advance — partial contribution',
                               f'Only ₦{actual:,.2f} of your ₦{expected:,.2f} period {month} contribution '
                               f'was received.')
            posted += 1

        audit(db, 'CTAS_CONTRIBUTION_IMPORT', 'ctas',
              f"Cycle {cycle_id} period {month}: posted {posted}, missed {missed}, skipped {skipped}")
        db.commit()
        flash(f'Contributions period {month}: posted {posted}, missed {missed}, skipped {skipped}.', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Could not process the contributions file: {e}', 'danger')
    return redirect(url_for('ctas.cycle_detail', cycle_id=cycle_id, tab='contributions'))


# ── Member portal ─────────────────────────────────────────────────────────────

@ctas.route('/my-ctas')
@ctas_required
@login_required
def my_ctas():
    db = get_db()
    member = member_for_user(db, current_user.id)
    if not member:
        flash('Your account is not linked to a member record yet.', 'warning')
        return redirect(url_for('portal.member_portal'))
    subs = db.execute('''
        SELECT s.*, c.name AS cycle_name, c.duration_months, c.affordability_method
        FROM ctas_subscriptions s JOIN ctas_cycles c ON c.id = s.cycle_id
        WHERE s.member_id = ? ORDER BY s.applied_at DESC''', (member['id'],)).fetchall()
    open_cycles = db.execute('''
        SELECT * FROM ctas_cycles WHERE status = 'open'
          AND id NOT IN (SELECT cycle_id FROM ctas_subscriptions WHERE member_id = ?)
        ORDER BY name''', (member['id'],)).fetchall()
    has_active = ce.member_has_active_subscription(db, member['id'])
    # This member's upcoming/outstanding contributions across their subscriptions.
    schedule = []
    if subs:
        sub_ids = [s['id'] for s in subs]
        placeholders = ','.join('?' for _ in sub_ids)
        for cid in {s['cycle_id'] for s in subs}:
            _refresh_schedule_statuses(db, cid)
        db.commit()
        schedule = db.execute(
            f"SELECT sc.*, c.name AS cycle_name FROM ctas_schedule sc "
            f"JOIN ctas_cycles c ON c.id = sc.cycle_id "
            f"WHERE sc.subscription_id IN ({placeholders}) "
            f"ORDER BY sc.due_date, sc.period_number", sub_ids).fetchall()
    mandates = {}
    for s in subs:
        m = _active_mandate(db, s['id'])
        if m:
            mandates[s['id']] = m
    # Priority positions still on offer for each of the member's cycles.
    prio = {}
    for s in subs:
        cyc = db.execute('SELECT priority_enabled FROM ctas_cycles WHERE id = ?', (s['cycle_id'],)).fetchone()
        if cyc and cyc['priority_enabled'] and not s['payout_month']:
            tiers = _priority_tiers(db, cycle_id=s['cycle_id'])
            if tiers:
                prio[s['id']] = tiers
    # Guarantors this member has asked for, and the cover each cycle expects.
    guarantors, cover_need = {}, {}
    for s in subs:
        guarantors[s['id']] = db.execute(
            "SELECT g.*, m.first_name || ' ' || m.last_name AS name, m.member_number "
            "FROM ctas_guarantors g JOIN members m ON m.id = g.member_id "
            "WHERE g.subscription_id = ? ORDER BY g.id", (s['id'],)).fetchall()
        cyc = db.execute('SELECT * FROM ctas_cycles WHERE id = ?', (s['cycle_id'],)).fetchone()
        if cyc and ce.coverage_ratio_for(cyc) > 0 and not s['payout_month']:
            sec = ce.member_security(db, member, s['id'])
            cover_need[s['id']] = {
                'ratio': ce.coverage_ratio_for(cyc),
                'security': sec,
                'floor': ce.min_safe_position(cyc, sec['total'], s['target_amount']),
                'earliest': int(cyc['earliest_payout_month'] or 1),
                'period_word': ce.period_word(cyc),
            }
    # Members who could be asked to guarantee (not the applicant, not already asked).
    candidates = db.execute(
        "SELECT id, first_name, last_name, member_number FROM members "
        "WHERE status = 'active' AND id <> ? ORDER BY first_name", (member['id'],)).fetchall()
    return render_template('member/my-ctas.html', member=member, subs=subs,
                           open_cycles=open_cycles, has_active=has_active, schedule=schedule,
                           mandates=mandates, priority_offers=prio, ctas_terms=CTAS_TERMS,
                           guarantors=guarantors, cover_need=cover_need, candidates=candidates)


@ctas.route('/my-ctas/<int:sub_id>/guarantors', methods=['POST'])
@ctas_required
@login_required
def request_guarantor(sub_id):
    """Ask another member to back this subscription. The pledge is worth nothing
    until they consent — it is recorded as pending and they are notified."""
    db = get_db()
    member = member_for_user(db, current_user.id)
    sub = db.execute('SELECT * FROM ctas_subscriptions WHERE id = ?', (sub_id,)).fetchone()
    if not member or not sub or sub['member_id'] != member['id']:
        abort(403)
    if sub['payout_month']:
        flash('The ballot has run for this cycle, so guarantors can no longer be added.', 'warning')
        return redirect(url_for('ctas.my_ctas'))
    try:
        guarantor_id = int(request.form.get('guarantor_id') or 0)
        amount = round(max(0.0, finite_float(request.form.get('amount') or 0)), 2)
    except ValueError:
        flash('Enter a valid guarantor and amount.', 'danger')
        return redirect(url_for('ctas.my_ctas'))

    guarantor = db.execute("SELECT * FROM members WHERE id = ? AND status = 'active'",
                           (guarantor_id,)).fetchone()
    if not guarantor or guarantor_id == member['id']:
        flash('Choose another active member as your guarantor.', 'danger')
        return redirect(url_for('ctas.my_ctas'))
    if amount <= 0:
        flash('Enter how much they are backing you for.', 'danger')
        return redirect(url_for('ctas.my_ctas'))
    if db.execute('SELECT 1 FROM ctas_guarantors WHERE subscription_id = ? AND member_id = ?',
                  (sub_id, guarantor_id)).fetchone():
        flash('That member has already been asked.', 'warning')
        return redirect(url_for('ctas.my_ctas'))

    capacity = ce.guarantor_capacity(db, guarantor_id)
    if amount > capacity:
        flash(f"{guarantor['first_name']} can back at most ₦{capacity:,.2f} right now — that is "
              f"their savings and share capital less what they already guarantee or owe.", 'danger')
        return redirect(url_for('ctas.my_ctas'))

    db.execute("INSERT INTO ctas_guarantors (subscription_id, member_id, amount, status) "
               "VALUES (?, ?, ?, 'pending')", (sub_id, guarantor_id, amount))
    _notify_member(db, guarantor_id, 'Target Advance — guarantee request',
                   f"{member['first_name']} {member['last_name']} has asked you to guarantee "
                   f"₦{amount:,.2f} of their target advance. Review it under Guarantor Requests.")
    audit(db, 'CTAS_GUARANTOR_REQUEST', 'ctas',
          f"Sub #{sub_id}: asked member {guarantor_id} to guarantee ₦{amount:,.2f}")
    db.commit()
    flash(f"Request sent to {guarantor['first_name']} {guarantor['last_name']}. "
          f"It only counts once they accept.", 'success')
    return redirect(url_for('ctas.my_ctas'))


@ctas.route('/my-ctas/guarantors/<int:gid>/withdraw', methods=['POST'])
@ctas_required
@login_required
def withdraw_guarantor(gid):
    """The applicant cancels a request they made."""
    db = get_db()
    member = member_for_user(db, current_user.id)
    row = db.execute('SELECT g.*, s.member_id AS applicant_id, s.payout_month FROM ctas_guarantors g '
                     'JOIN ctas_subscriptions s ON s.id = g.subscription_id WHERE g.id = ?',
                     (gid,)).fetchone()
    if not member or not row or row['applicant_id'] != member['id']:
        abort(403)
    if row['payout_month']:
        flash('The ballot has run — this guarantee stands until the advance is recovered.', 'warning')
        return redirect(url_for('ctas.my_ctas'))
    db.execute("UPDATE ctas_guarantors SET status = 'withdrawn', responded_at = ? WHERE id = ?",
               (datetime.now(), gid))
    audit(db, 'CTAS_GUARANTOR_WITHDRAWN', 'ctas', f"Guarantee #{gid} withdrawn by the applicant")
    db.commit()
    flash('Guarantee request withdrawn.', 'info')
    return redirect(url_for('ctas.my_ctas'))


@ctas.route('/my-ctas/guarantees/<int:gid>/<action>', methods=['POST'])
@ctas_required
@login_required
def respond_ctas_guarantee(gid, action):
    """The guarantor accepts or declines. Accepting is a commitment of their own
    savings, so it needs a typed signature and is capacity-checked again here."""
    db = get_db()
    member = member_for_user(db, current_user.id)
    g = db.execute('SELECT * FROM ctas_guarantors WHERE id = ? AND member_id = ?',
                   (gid, member['id'] if member else 0)).fetchone()
    if not member or not g:
        abort(403)
    if g['status'] != 'pending':
        flash('That request has already been answered.', 'warning')
        return redirect(url_for('ctas.my_guarantees'))

    sub = db.execute('SELECT * FROM ctas_subscriptions WHERE id = ?', (g['subscription_id'],)).fetchone()
    applicant = db.execute('SELECT * FROM members WHERE id = ?', (sub['member_id'],)).fetchone()

    if action == 'accept':
        signature = (request.form.get('signature_name') or '').strip()
        if not signature:
            flash('Type your full name to confirm you accept this guarantee.', 'danger')
            return redirect(url_for('ctas.my_guarantees'))
        capacity = ce.guarantor_capacity(db, member['id'], exclude_guarantee_id=gid)
        if float(g['amount'] or 0) > capacity:
            flash(f'You can back at most ₦{capacity:,.2f} right now, so this guarantee of '
                  f'₦{float(g["amount"] or 0):,.2f} cannot be accepted.', 'danger')
            return redirect(url_for('ctas.my_guarantees'))
        db.execute("UPDATE ctas_guarantors SET status = 'accepted', responded_at = ?, "
                   "signature_name = ? WHERE id = ?", (datetime.now(), signature, gid))
        _notify_member(db, sub['member_id'], 'Target Advance — guarantee accepted',
                       f"{member['first_name']} {member['last_name']} has accepted your guarantee "
                       f"request for ₦{float(g['amount'] or 0):,.2f}.")
        msg = 'Guarantee accepted. It now counts towards their security cover.'
    else:
        db.execute("UPDATE ctas_guarantors SET status = 'declined', responded_at = ?, "
                   "comment = ? WHERE id = ?",
                   (datetime.now(), (request.form.get('comment') or '').strip(), gid))
        _notify_member(db, sub['member_id'], 'Target Advance — guarantee declined',
                       f"{member['first_name']} {member['last_name']} declined your guarantee request.")
        msg = 'Guarantee declined.'
    audit(db, 'CTAS_GUARANTOR_RESPONSE', 'ctas',
          f"Guarantee #{gid} {action}ed by member {member['id']} "
          f"for {applicant['first_name'] if applicant else '?'}")
    db.commit()
    flash(msg, 'success')
    return redirect(url_for('ctas.my_guarantees'))


@ctas.route('/my-ctas/guarantees')
@ctas_required
@login_required
def my_guarantees():
    """The member's inbox of CTAS guarantee requests made of them."""
    db = get_db()
    member = member_for_user(db, current_user.id)
    if not member:
        flash('Your account is not linked to a member record yet.', 'warning')
        return redirect(url_for('portal.member_portal'))
    rows = db.execute('''
        SELECT g.*, s.target_amount, s.payout_month, s.status AS sub_status,
               c.name AS cycle_name,
               m.first_name || ' ' || m.last_name AS applicant_name, m.member_number
        FROM ctas_guarantors g
        JOIN ctas_subscriptions s ON s.id = g.subscription_id
        JOIN ctas_cycles c ON c.id = s.cycle_id
        JOIN members m ON m.id = s.member_id
        WHERE g.member_id = ? ORDER BY g.id DESC''', (member['id'],)).fetchall()
    return render_template('member/my-ctas-guarantees.html', requests=rows,
                           capacity=ce.guarantor_capacity(db, member['id']))


@ctas.route('/my-ctas/apply', methods=['POST'])
@ctas_required
@login_required
def my_ctas_apply():
    db = get_db()
    member = member_for_user(db, current_user.id)
    if not member:
        abort(403)
    cycle = db.execute("SELECT * FROM ctas_cycles WHERE id = ? AND status = 'open'",
                       (request.form.get('cycle_id'),)).fetchone()
    if not cycle:
        flash('That cycle is not open for applications.', 'warning')
        return redirect(url_for('ctas.my_ctas'))
    try:
        target = finite_float(request.form.get('target_amount') or 0)
        tenure = int(request.form.get('tenure_months') or 0)
    except ValueError:
        flash('Enter a valid amount and tenure.', 'danger')
        return redirect(url_for('ctas.my_ctas'))

    if not request.form.get('terms'):
        flash('You must accept the scheme terms to apply.', 'danger')
        return redirect(url_for('ctas.my_ctas'))
    signature = (request.form.get('signature_name') or '').strip()
    if not signature:
        flash('Please type your full name as your signature.', 'danger')
        return redirect(url_for('ctas.my_ctas'))

    result = ce.check_eligibility(db, member, cycle, target, tenure)
    hard = [r for r in result['reasons'] if 'exceeds' not in r and 'salary' not in r.lower()]
    if hard:
        flash('Cannot apply: ' + ' '.join(hard), 'danger')
        return redirect(url_for('ctas.my_ctas'))

    db.execute('''INSERT INTO ctas_subscriptions
            (cycle_id, member_id, target_amount, tenure_months, monthly_deduction, admin_fee,
             status, outstanding, terms_accepted, signature_name, signed_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, 'submitted', ?, 1, ?, ?, ?)''',
        (cycle['id'], member['id'], target, tenure, result['monthly_deduction'],
         result['admin_fee'], target, signature, datetime.now(), current_user.id))
    audit(db, 'CTAS_APPLY', 'ctas', f"Member {member['id']} applied to cycle {cycle['name']} (target {target})")
    db.commit()
    note = '' if result['eligible'] else ' Affordability will be reviewed by the committee.'
    flash('Your target-advance application has been submitted for review.' + note, 'success')
    return redirect(url_for('ctas.my_ctas'))


# ── Automatic contributions (card-on-file mandate) ────────────────────────────

def _next_unpaid_period(db, subscription_id):
    """The member's earliest unpaid schedule row, or None."""
    return db.execute(
        "SELECT * FROM ctas_schedule WHERE subscription_id = ? AND status != 'paid' "
        "ORDER BY period_number LIMIT 1", (subscription_id,)).fetchone()


def _active_mandate(db, subscription_id):
    return db.execute("SELECT * FROM ctas_mandates WHERE subscription_id = ? AND status = 'active' "
                      "ORDER BY id DESC LIMIT 1", (subscription_id,)).fetchone()


def _store_token(token):
    """Encrypt the payment token at rest, like other sensitive member data."""
    try:
        from crypto import encrypt_field, encryption_enabled
        return encrypt_field(token) if (token and encryption_enabled()) else token
    except Exception:
        return token


def _read_token(stored):
    """Decrypt a stored payment token for use with the gateway."""
    try:
        from crypto import decrypt_field
        return decrypt_field(stored) if stored else ''
    except Exception:
        return stored or ''


def _consent_text(cycle, amount):
    word = ce.period_word(cycle)
    return (f"I authorise {cycle['name']} contributions of ₦{amount:,.2f} to be charged to this card "
            f"every {word} on the due date until my subscription completes or I cancel. "
            f"I may cancel at any time from my member portal.")


@ctas.route('/my-ctas/autopay/<int:sub_id>', methods=['GET', 'POST'])
@ctas_required
@login_required
def autopay_setup(sub_id):
    """Set up automatic contributions. The member authorises by paying their next
    due contribution with a card; the gateway returns a reusable token which is
    stored as the mandate. No card details ever reach this system."""
    db = get_db()
    member = member_for_user(db, current_user.id)
    sub = db.execute('SELECT * FROM ctas_subscriptions WHERE id = ?', (sub_id,)).fetchone()
    if not sub or not member or sub['member_id'] != member['id']:
        abort(403)
    cycle = db.execute('SELECT * FROM ctas_cycles WHERE id = ?', (sub['cycle_id'],)).fetchone()
    due = _next_unpaid_period(db, sub_id)
    amount = round(float(due['expected_amount'] or 0) - float(due['paid_amount'] or 0), 2) if due else 0.0
    mandate = _active_mandate(db, sub_id)

    if request.method == 'GET':
        # Show a stopped mandate (suspended after repeated failures, or expired
        # card) so the member understands why and can set it up again.
        stopped = None if mandate else db.execute(
            "SELECT * FROM ctas_mandates WHERE subscription_id = ? AND status IN ('suspended','expired') "
            "ORDER BY id DESC LIMIT 1", (sub_id,)).fetchone()
        return render_template('member/ctas-autopay.html', sub=sub, cycle=cycle, due=due,
                               amount=amount, mandate=mandate, stopped=stopped,
                               consent=_consent_text(cycle, amount) if due else '')

    if mandate:
        flash('Automatic payment is already set up for this subscription.', 'info')
        return redirect(url_for('ctas.my_ctas'))
    if not due or amount <= 0:
        flash('You have no outstanding contribution to authorise with right now.', 'warning')
        return redirect(url_for('ctas.my_ctas'))
    if not request.form.get('consent'):
        flash('Please tick the authorisation box to continue.', 'danger')
        return redirect(url_for('ctas.autopay_setup', sub_id=sub_id))
    signature = (request.form.get('signature_name') or '').strip()
    if not signature:
        flash('Type your full name to authorise.', 'danger')
        return redirect(url_for('ctas.autopay_setup', sub_id=sub_id))

    settings = {r['key']: r['value'] for r in db.execute('SELECT key, value FROM settings').fetchall()}
    if (settings.get('active_gateway') or 'paystack') != 'paystack' or not settings.get('paystack_secret_key'):
        flash('Online payment is not configured for this cooperative yet. Please contact the office.', 'warning')
        return redirect(url_for('ctas.my_ctas'))

    email = (member['email'] or current_user.email or '').strip()
    if not email:
        flash('Add an email address to your profile before setting up automatic payment.', 'danger')
        return redirect(url_for('ctas.my_ctas'))

    reference = generate_reference('CTASMD')
    db.execute('''INSERT INTO ctas_mandates
            (member_id, subscription_id, provider, mandate_type, email, amount_cap, status,
             consent_text, consent_signature, consent_ip, consented_at, setup_reference)
            VALUES (?, ?, 'paystack', 'card', ?, ?, 'pending', ?, ?, ?, ?, ?)''',
        (member['id'], sub_id, email, amount, _consent_text(cycle, amount), signature,
         (request.headers.get('X-Forwarded-For', '') or request.remote_addr or '')[:80],
         datetime.now(), reference))
    audit(db, 'CTAS_MANDATE_START', 'ctas', f"Autopay authorisation started for subscription #{sub_id}")
    db.commit()

    try:
        gw = get_gateway('paystack')
        resp = gw.initialize(email, amount, reference,
                             url_for('ctas.autopay_callback', _external=True),
                             metadata={'ctas_subscription': sub_id, 'ctas_period': due['period_number'],
                                       'purpose': 'ctas_mandate'})
        if resp.get('status'):
            return redirect(resp['data']['authorization_url'])
    except Exception as exc:   # pragma: no cover - network
        current_app.logger.error('CTAS mandate init failed: %s', exc)
    flash('Could not reach the payment gateway. Please try again shortly.', 'danger')
    return redirect(url_for('ctas.my_ctas'))


@ctas.route('/my-ctas/autopay/callback')
@ctas_required
@login_required
def autopay_callback():
    """Gateway return: verify the payment, store the reusable token as the
    mandate, and post the contribution that authorised it."""
    db = get_db()
    member = member_for_user(db, current_user.id)
    reference = (request.args.get('reference') or '').strip()
    mandate = db.execute("SELECT * FROM ctas_mandates WHERE setup_reference = ? AND status = 'pending'",
                         (reference,)).fetchone()
    if not reference or not mandate:
        flash('Payment reference not recognised.', 'danger')
        return redirect(url_for('ctas.my_ctas'))
    # Only the member who started this authorisation may complete it.
    if not member or mandate['member_id'] != member['id']:
        abort(403)
    try:
        resp = get_gateway('paystack').verify(reference)
    except Exception as exc:   # pragma: no cover - network
        current_app.logger.error('CTAS mandate verify failed: %s', exc)
        flash('We could not confirm the payment yet. If you were charged, contact the office.', 'warning')
        return redirect(url_for('ctas.my_ctas'))

    data = resp.get('data') or {}
    if not resp.get('status') or data.get('status') != 'success':
        db.execute("UPDATE ctas_mandates SET status = 'failed', last_error = ? WHERE id = ?",
                   ('Authorisation payment was not completed.', mandate['id']))
        db.commit()
        flash('Payment was not completed, so automatic payment was not set up.', 'warning')
        return redirect(url_for('ctas.my_ctas'))

    auth = data.get('authorization') or {}
    token = auth.get('authorization_code') or ''
    reusable = bool(auth.get('reusable'))
    paid = round(float(data.get('amount', 0)) / 100.0, 2)

    sub = db.execute('SELECT * FROM ctas_subscriptions WHERE id = ?', (mandate['subscription_id'],)).fetchone()
    cycle = db.execute('SELECT * FROM ctas_cycles WHERE id = ?', (sub['cycle_id'],)).fetchone()
    period = int((data.get('metadata') or {}).get('ctas_period') or 0) or None
    if not period:
        nxt = _next_unpaid_period(db, sub['id'])
        period = nxt['period_number'] if nxt else 1

    # Post the contribution that authorised the mandate (idempotent by reference).
    already = db.execute("SELECT 1 FROM journal_entries WHERE reference = ?",
                         (f"CTAS-CN-{sub['id']}-M{period}-{reference}",)).fetchone()
    if not already and paid > 0:
        _post_contribution(db, sub, cycle, period, paid, created_by=current_user.id,
                           ref_suffix=f'-{reference}')

    if token and reusable:
        db.execute("UPDATE ctas_mandates SET status = 'active', authorization_code = ?, "
                   "masked_label = ?, bank_name = ?, card_type = ?, exp_month = ?, exp_year = ?, "
                   "last_charged_at = ? WHERE id = ?",
                   (_store_token(token), f"**** {auth.get('last4', '')}".strip(), auth.get('bank') or '',
                    auth.get('card_type') or '', auth.get('exp_month') or '',
                    auth.get('exp_year') or '', datetime.now(), mandate['id']))
        audit(db, 'CTAS_MANDATE_ACTIVE', 'ctas',
              f"Autopay authorised for subscription #{sub['id']} ({auth.get('card_type','card')} ****{auth.get('last4','')})")
        _notify_member(db, sub['member_id'], 'Target Advance — automatic payment active',
                       'Your contribution was received and automatic payment is now set up. '
                       'You can cancel any time from your member portal.')
        flash('Payment received and automatic contributions are now set up.', 'success')
    else:
        db.execute("UPDATE ctas_mandates SET status = 'failed', "
                   "last_error = 'Card cannot be reused for automatic charges.' WHERE id = ?",
                   (mandate['id'],))
        flash('Your payment was received, but this card cannot be saved for automatic charges. '
              'You can still pay each period manually.', 'warning')
    db.commit()
    return redirect(url_for('ctas.my_ctas'))


@ctas.route('/my-ctas/autopay/<int:mandate_id>/cancel', methods=['POST'])
@ctas_required
@login_required
def autopay_cancel(mandate_id):
    """Members can withdraw their authorisation at any time (required by the
    consent they gave)."""
    db = get_db()
    member = member_for_user(db, current_user.id)
    mandate = db.execute('SELECT * FROM ctas_mandates WHERE id = ?', (mandate_id,)).fetchone()
    if not mandate or not member or mandate['member_id'] != member['id']:
        abort(403)
    db.execute("UPDATE ctas_mandates SET status = 'cancelled', cancelled_at = ? WHERE id = ?",
               (datetime.now(), mandate_id))
    audit(db, 'CTAS_MANDATE_CANCEL', 'ctas', f"Autopay cancelled for mandate #{mandate_id}")
    db.commit()
    flash('Automatic payment cancelled. You will need to pay each contribution yourself.', 'info')
    return redirect(url_for('ctas.my_ctas'))


@ctas.route('/tasks/ctas/charge-due', methods=['GET', 'POST'])
def ctas_charge_due():
    """Charge every due contribution that has an active mandate.

    Two ways in, like the loan sweep: a scheduler calling with
    ``X-Task-Token``/``?token=`` matching TASK_RUNNER_TOKEN, or a logged-in
    officer holding the CTAS duty.
    """
    db = get_db()
    if not ctas_enabled(db):
        abort(404)
    token = (os.environ.get('TASK_RUNNER_TOKEN', '') or '').strip()
    provided = (request.headers.get('X-Task-Token', '') or request.args.get('token', '')).strip()
    by_token = bool(token) and hmac.compare_digest(provided, token)
    by_user = current_user.is_authenticated and perms.user_can('ctas.manage')
    if not (by_token or by_user):
        return jsonify({'success': False, 'error': 'Unauthorised'}), 403

    today = date.today()
    today_s = today.isoformat()
    rows = db.execute('''
        SELECT sc.*, mn.id AS mandate_id, mn.authorization_code, mn.email,
               mn.exp_month, mn.exp_year, s.member_id
        FROM ctas_schedule sc
        JOIN ctas_subscriptions s ON s.id = sc.subscription_id
        JOIN ctas_mandates mn ON mn.subscription_id = s.id AND mn.status = 'active'
        WHERE sc.status IN ('due', 'grace', 'late', 'partial') AND sc.due_date <= ?
          AND (sc.next_retry_at IS NULL OR sc.next_retry_at <= ?)
          AND s.status IN ('enrolled', 'scheduled', 'active_recovery')
        ORDER BY sc.due_date''', (today_s, today_s)).fetchall()

    charged = failed = skipped = suspended = 0
    for r in rows:
        amount = round(float(r['expected_amount'] or 0) - float(r['paid_amount'] or 0), 2)
        if amount <= 0:
            continue
        sub = db.execute('SELECT * FROM ctas_subscriptions WHERE id = ?', (r['subscription_id'],)).fetchone()
        cycle = db.execute('SELECT * FROM ctas_cycles WHERE id = ?', (sub['cycle_id'],)).fetchone()
        retry_days = int(cycle['retry_days'] if cycle['retry_days'] is not None else 3)
        max_attempts = int(cycle['max_charge_attempts'] if cycle['max_charge_attempts'] is not None else 3)
        attempts = int(r['charge_attempts'] or 0)

        # An expired card can never succeed — stop trying and tell the member.
        if ce.card_expired(r['exp_month'], r['exp_year'], today):
            db.execute("UPDATE ctas_mandates SET status = 'expired', "
                       "last_error = 'Saved card has expired.' WHERE id = ?", (r['mandate_id'],))
            _raise_exception(db, sub['id'], 'missed_deduction', r['period_number'], amount,
                             'Automatic payment stopped: the saved card has expired.')
            _notify_member(db, r['member_id'], 'Target Advance — your saved card has expired',
                           'Your card on file has expired, so automatic payment has stopped. '
                           'Please set it up again with a current card.')
            skipped += 1
            continue

        reference = generate_reference('CTASAP')
        try:
            resp = get_gateway('paystack').charge_authorization(
                r['email'], amount, reference, _read_token(r['authorization_code']),
                metadata={'ctas_subscription': sub['id'], 'ctas_period': r['period_number']})
            ok = bool(resp.get('status')) and ((resp.get('data') or {}).get('status') == 'success')
        except Exception as exc:   # pragma: no cover - network
            current_app.logger.warning('CTAS auto-charge error: %s', exc)
            ok, resp = False, {'message': str(exc)}

        if ok:
            _post_contribution(db, sub, cycle, r['period_number'], amount, ref_suffix=f'-{reference}')
            db.execute("UPDATE ctas_schedule SET charge_attempts = ?, last_attempt_at = ?, "
                       "next_retry_at = NULL WHERE id = ?", (attempts + 1, datetime.now(), r['id']))
            db.execute("UPDATE ctas_mandates SET last_charged_at = ?, fail_count = 0, last_error = NULL "
                       "WHERE id = ?", (datetime.now(), r['mandate_id']))
            _notify_member(db, r['member_id'], 'Target Advance — contribution paid',
                           f"₦{amount:,.2f} for period {r['period_number']} was charged to your saved card.")
            charged += 1
            continue

        # Failed: step the ladder rather than retrying every run.
        attempts += 1
        reason = str(resp.get('message') or 'declined')[:200]
        give_up = attempts >= max_attempts
        next_retry = None if give_up else (today + timedelta(days=retry_days)).isoformat()
        db.execute("UPDATE ctas_schedule SET charge_attempts = ?, last_attempt_at = ?, next_retry_at = ? "
                   "WHERE id = ?", (attempts, datetime.now(), next_retry, r['id']))
        db.execute("UPDATE ctas_mandates SET fail_count = ?, last_error = ? WHERE id = ?",
                   (attempts, reason, r['mandate_id']))
        if give_up:
            # Stop charging; hand it to the officers as an exception to work.
            db.execute("UPDATE ctas_mandates SET status = 'suspended' WHERE id = ?", (r['mandate_id'],))
            _raise_exception(db, sub['id'], 'missed_deduction', r['period_number'], amount,
                             f"Automatic payment suspended after {attempts} failed attempts: {reason}")
            _notify_member(db, r['member_id'], 'Target Advance — automatic payment stopped',
                           f"We could not charge ₦{amount:,.2f} for period {r['period_number']} after "
                           f"{attempts} attempts, so automatic payment has been paused. Please pay this "
                           f"contribution and set up your card again.")
            suspended += 1
        else:
            _notify_member(db, r['member_id'], 'Target Advance — payment failed',
                           f"We could not charge ₦{amount:,.2f} for period {r['period_number']} "
                           f"({reason}). We will try again in {retry_days} day(s).")
        failed += 1

    audit(db, 'CTAS_AUTO_CHARGE', 'ctas',
          f"Auto-charge run: {charged} charged, {failed} failed, {suspended} suspended, {skipped} skipped")
    db.commit()
    if by_user and not by_token:
        flash(f'Automatic charges: {charged} succeeded, {failed} failed'
              + (f', {suspended} suspended' if suspended else '')
              + (f', {skipped} skipped (expired card)' if skipped else '') + '.', 'info')
        return redirect(url_for('ctas.dashboard'))
    return jsonify({'success': True, 'charged': charged, 'failed': failed,
                    'suspended': suspended, 'skipped': skipped})


# ── Exceptions: member exit (net-off waterfall) + arrears dashboard ────────────

@ctas.route('/ctas/subscriptions/<int:sub_id>/exit', methods=['GET', 'POST'])
@ctas_required
@role_required('admin', 'treasurer')
def exit_settle(sub_id):
    """Settle a member's outstanding advance on exit/resignation via the net-off
    waterfall: savings -> share capital -> other recoveries -> write-off."""
    db = get_db()
    sub = db.execute('''SELECT s.*, m.first_name || ' ' || m.last_name AS member_name,
                               m.total_savings, m.shares_value
                        FROM ctas_subscriptions s JOIN members m ON m.id = s.member_id
                        WHERE s.id = ?''', (sub_id,)).fetchone()
    if not sub:
        abort(404)
    if sub['status'] != ce.SUB_ACTIVE_RECOVERY:
        flash('Only a subscription in recovery can be settled on exit.', 'warning')
        return redirect(url_for('ctas.cycle_detail', cycle_id=sub['cycle_id']))
    # Outstanding = the co-op's advance still owed by this paid-out member.
    outstanding = round(float(sub['advance_balance'] or 0), 2)
    savings = float(sub['total_savings'] or 0)
    shares = float(sub['shares_value'] or 0)

    # Accepted guarantees, and what each guarantor can actually be called on for
    # today. A pledge is capped by the guarantor's own savings and share capital.
    guarantors = []
    for g in db.execute(
            "SELECT g.*, m.first_name || ' ' || m.last_name AS name, m.id AS gid "
            "FROM ctas_guarantors g JOIN members m ON m.id = g.member_id "
            "WHERE g.subscription_id = ? AND g.status = 'accepted' ORDER BY g.id",
            (sub_id,)).fetchall():
        reachable = _member_reachable_value(db, g['gid'])
        guarantors.append({
            'id': g['id'], 'member_id': g['gid'], 'name': g['name'],
            'pledged': round(float(g['amount'] or 0), 2),
            'available': round(min(float(g['amount'] or 0), reachable), 2),
        })

    may_call = perms.user_can('ctas.guarantee_call')

    if request.method == 'GET':
        wf = ce.net_off_waterfall(outstanding, savings, shares, 0,
                                  sum(g['available'] for g in guarantors) if may_call else 0)
        return render_template('ctas/exit.html', sub=sub, outstanding=outstanding,
                               savings=savings, shares=shares, wf=wf, guarantors=guarantors,
                               may_call=may_call)

    try:
        other = float(request.form.get('other_recovery') or 0)
    except ValueError:
        other = 0.0
    reason = (request.form.get('reason') or '').strip()

    # The officer decides how much to call from each guarantor; nothing is taken
    # automatically, and never more than that guarantor pledged or actually has.
    called = []
    for g in guarantors:
        try:
            amt = round(max(0.0, float(request.form.get(f"guarantor_{g['id']}") or 0)), 2)
        except ValueError:
            amt = 0.0
        amt = min(amt, g['available'])
        if amt > 0:
            called.append({**g, 'called': amt})

    # Calling on a guarantor takes money from a member who is not the one
    # leaving, so it needs its own duty and a committee decision on record.
    # Settling with no guarantor call is unaffected.
    committee_ref = (request.form.get('committee_reference') or '').strip()
    if called:
        if not may_call:
            flash('You do not have permission to call on a guarantor. An officer holding the '
                  '"Authorise calling on a CTAS guarantor" duty must do this — see '
                  'Settings → Task Assignment. You can still settle without calling on them.',
                  'danger')
            return redirect(url_for('ctas.exit_settle', sub_id=sub_id))
        if not committee_ref:
            flash('Record the committee decision authorising this call — for example the minute '
                  'or resolution reference. It is kept against each guarantee.', 'danger')
            return redirect(url_for('ctas.exit_settle', sub_id=sub_id))

    from_guarantors_available = round(sum(g['called'] for g in called), 2)
    wf = ce.net_off_waterfall(outstanding, savings, shares, other, from_guarantors_available)
    # If the earlier steps covered more than expected, only take what was needed.
    if wf['from_guarantors'] < from_guarantors_available:
        short = round(from_guarantors_available - wf['from_guarantors'], 2)
        for g in reversed(called):
            take = min(short, g['called'])
            g['called'] = round(g['called'] - take, 2)
            short = round(short - take, 2)
            if short <= 0:
                break
        called = [g for g in called if g['called'] > 0]
    ctas_ensure_accounts(db)
    cash = get_default_cash_account(db)
    try:
        lines = []
        if wf['from_savings'] > 0:
            lines.append({'account': MEMBER_DEPOSITS, 'debit': wf['from_savings'], 'memo': 'CTAS exit: from savings'})
        if wf['from_shares'] > 0:
            lines.append({'account': SHARE_CAPITAL, 'debit': wf['from_shares'], 'memo': 'CTAS exit: from share capital'})
        if wf['from_other'] > 0:
            lines.append({'account': cash, 'debit': wf['from_other'], 'memo': 'CTAS exit: other recoveries'})
        for g in called:
            lines.append({'account': MEMBER_DEPOSITS, 'debit': g['called'],
                          'memo': f"CTAS exit: called on guarantor {g['name']}"})
        if wf['write_off'] > 0:
            lines.append({'account': CTAS_WRITEOFF, 'debit': wf['write_off'], 'memo': 'CTAS exit: write-off'})
        if outstanding > 0:
            lines.append({'account': CTAS_ADVANCES, 'credit': outstanding,
                          'memo': f'CTAS exit settlement sub {sub_id}'})
        if lines:
            post_journal_safe(db, f"CTAS exit settlement - subscription {sub_id}", lines,
                              date=datetime.now(), reference=f"CTAS-EXIT-{sub_id}",
                              source_module='ctas_exit', source_id=sub_id, created_by=current_user.id)
        db.execute("UPDATE members SET total_savings = COALESCE(total_savings, 0) - ?, "
                   "shares_value = COALESCE(shares_value, 0) - ? WHERE id = ?",
                   (wf['from_savings'], wf['from_shares'], sub['member_id']))
        # Guarantors: reduce the savings actually called on, record it against
        # the guarantee, and tell them — this is their money leaving.
        for g in called:
            # Record it in the savings ledger, not just the cached column: the
            # ledger is the source of truth for a member's balance, and a
            # guarantor stays an active member whose balance must stay right
            # (and must not be callable twice for the same money).
            db.execute(
                "INSERT INTO savings (member_id, amount, month, payment_type, payment_method, "
                " notes, date, created_by) "
                "VALUES (?, ?, ?, 'withdrawal', 'ctas_guarantee', ?, ?, ?)",
                (g['member_id'], -g['called'], datetime.now().strftime('%Y-%m'),
                 f"CTAS guarantee called for {sub['member_name']} "
                 f"(subscription {sub_id}), authorised {committee_ref}",
                 datetime.now(), current_user.id))
            db.execute("UPDATE members SET total_savings = COALESCE(total_savings, 0) - ? WHERE id = ?",
                       (g['called'], g['member_id']))
            db.execute("UPDATE ctas_guarantors SET recovered_amount = COALESCE(recovered_amount, 0) + ?, "
                       "status = 'called', called_by = ?, called_at = ?, called_reference = ? "
                       "WHERE id = ?",
                       (g['called'], current_user.id, datetime.now(), committee_ref, g['id']))
            _notify_member(db, g['member_id'], 'Target Advance — your guarantee was called',
                           f"₦{g['called']:,.2f} was recovered from your savings under the guarantee "
                           f"you gave for {sub['member_name']}'s target advance. "
                           f"Authorised by the committee ({committee_ref}).")
            audit(db, 'CTAS_GUARANTOR_CALLED', 'ctas',
                  f"Sub #{sub_id}: called ₦{g['called']:,.2f} on guarantor {g['name']} "
                  f"(member {g['member_id']}) under committee authority {committee_ref}")
        # Any guarantee not called is discharged when the subscription closes.
        db.execute("UPDATE ctas_guarantors SET status = 'released' "
                   "WHERE subscription_id = ? AND status = 'accepted'", (sub_id,))
        db.execute("UPDATE ctas_subscriptions SET total_recovered = target_amount, outstanding = 0, "
                   "advance_balance = 0, arrears_amount = 0, status = 'completed', completed_at = ? WHERE id = ?",
                   (datetime.now(), sub_id))
        db.execute("INSERT INTO ctas_exceptions (subscription_id, case_type, status, amount, description, "
                   "resolution_note, resolved_at, resolved_by) "
                   "VALUES (?, 'exit_recovery', 'resolved', ?, ?, ?, ?, ?)",
                   (sub_id, outstanding, f"Member exit; outstanding ₦{outstanding:,.2f}. {reason}",
                    f"savings ₦{wf['from_savings']:,.2f}, shares ₦{wf['from_shares']:,.2f}, "
                    f"other ₦{wf['from_other']:,.2f}, guarantors ₦{wf['from_guarantors']:,.2f}, "
                    f"write-off ₦{wf['write_off']:,.2f}",
                    datetime.now(), current_user.id))
        audit(db, 'CTAS_EXIT', 'ctas',
              f"Exit settle sub #{sub_id}: outstanding {outstanding}, write-off {wf['write_off']}")
        _notify_member(db, sub['member_id'], 'Target Advance — settled on exit',
                       f"Your target advance was settled. Outstanding ₦{outstanding:,.2f} recovered from your "
                       f"balances" + (f", ₦{wf['write_off']:,.2f} written off." if wf['write_off'] else "."))
        db.commit()
        flash(f"Exit settled — savings ₦{wf['from_savings']:,.2f}, shares ₦{wf['from_shares']:,.2f}"
              + (f", other ₦{wf['from_other']:,.2f}" if wf['from_other'] else "")
              + (f", written off ₦{wf['write_off']:,.2f}" if wf['write_off'] else "") + ".", 'success')
    except Exception as e:
        db.rollback()
        flash(f'Could not settle the exit: {e}', 'danger')
    return redirect(url_for('ctas.cycle_detail', cycle_id=sub['cycle_id']))


@ctas.route('/ctas/exceptions')
@ctas_required
@role_required('admin', 'treasurer')
def exceptions():
    db = get_db()
    show = (request.args.get('status') or 'open').strip()
    where = "WHERE e.status = 'open'" if show == 'open' else ''
    rows = db.execute(f'''
        SELECT e.*, s.cycle_id, m.first_name || ' ' || m.last_name AS member_name, m.member_number
        FROM ctas_exceptions e
        JOIN ctas_subscriptions s ON s.id = e.subscription_id
        JOIN members m ON m.id = s.member_id
        {where}
        ORDER BY e.created_at DESC''').fetchall()
    return render_template('ctas/exceptions.html', rows=rows, show=show)


@ctas.route('/ctas/exceptions/<int:case_id>/resolve', methods=['POST'])
@ctas_required
@role_required('admin', 'treasurer')
def resolve_exception(case_id):
    db = get_db()
    case = db.execute('SELECT * FROM ctas_exceptions WHERE id = ?', (case_id,)).fetchone()
    if not case:
        abort(404)
    note = (request.form.get('resolution_note') or '').strip()
    db.execute("UPDATE ctas_exceptions SET status = 'resolved', resolution_note = ?, resolved_at = ?, "
               "resolved_by = ? WHERE id = ?", (note, datetime.now(), current_user.id, case_id))
    audit(db, 'CTAS_EXCEPTION_RESOLVE', 'ctas', f"Resolved exception #{case_id}")
    db.commit()
    flash('Exception marked resolved.', 'success')
    return redirect(url_for('ctas.exceptions'))
