import csv
import hmac
import os
import random
import math
import secrets
from upload_history import record_upload
import loan_limits
from datetime import datetime, timedelta
from io import StringIO, TextIOWrapper

from flask import (Blueprint, Response, render_template, redirect, url_for, request,
                   flash, make_response, jsonify)
from flask_login import login_required, current_user

from database import get_db, last_insert_id, USE_POSTGRES, for_update
from email_service import (send_loan_approval_email, send_loan_rejection_email,
                           send_loan_repayment_email, send_loan_stage_email,
                           send_guarantor_request_email)
from utils import (claim_submission, finite_float, role_required, audit, notify_member, compute_loan_schedule,
                   PURPOSE_SETTING_KEY, METHOD_LABELS, record_revenue, split_repayment,
                   member_savings_balance, member_for_user,
                   member_has_minimum_membership, has_unpaid_loan_of_type)
from ledger import (post_journal, post_journal_safe, get_default_cash_account, get_postable_cash_accounts,
                    get_loan_repayment_counter_accounts, resolve_loan_repayment_counter_account,
                    resolve_cash_bank_account, UnknownCashAccountError,
                    LOANS_RECEIVABLE, ACCUM_SURPLUS, FEE_INCOME,
                    LOAN_INTEREST_INCOME, INSURANCE_PAYABLE)
import loan_workflow as lw
import loan_alerts as la
import permissions as la_permissions
from loan_pdf import build_loan_application_pdf
from delinquency import portfolio_delinquency

loans = Blueprint('loans', __name__)


LOAN_CORRECTION_COLUMNS = {
    'review_status', 'correction_type', 'member_number', 'member_name',
    'correct_active_balance', 'current_coopms_balance', 'correction_amount',
    'adjustment_needed_correct_less_live', 'correct_active_loans',
    'current_coopms_active_loans', 'correct_loan_numbers',
    'current_coopms_loan_numbers', 'suggested_action',
}


def _loan_applicant_type(loan):
    return loan['loan_applicant_type'] if 'loan_applicant_type' in loan.keys() and loan['loan_applicant_type'] else 'non_staff'


def _due_diligence_checks(loan):
    """Return the required pre-disbursement checks for this loan."""
    applicant_type = _loan_applicant_type(loan)
    checks = []
    if applicant_type == 'staff':
        checks.append({
            'key': 'hr_affordability',
            'label': 'HR/payroll affordability confirmed',
            'status': loan['hr_affordability_status'] or 'pending',
            'done': (loan['hr_affordability_status'] or '') == 'confirmed',
        })
    else:
        checks.extend([
            {
                'key': 'bank_statement',
                'label': 'Bank statement received',
                'status': loan['bank_statement_status'] or 'requested',
                'done': (loan['bank_statement_status'] or '') == 'received',
            },
            {
                'key': 'credit_check',
                'label': 'Credit/affordability check completed',
                'status': loan['credit_check_status'] or 'pending',
                'done': (loan['credit_check_status'] or '') == 'completed',
            },
        ])
    checks.append({
        'key': 'payment_collateral',
        'label': 'Repayment collateral verified',
        'status': loan['payment_collateral_status'] or 'pending',
        'done': (loan['payment_collateral_status'] or '') == 'verified',
    })
    return checks


def _due_diligence_complete(loan):
    checks = _due_diligence_checks(loan)
    return all(c['done'] for c in checks), checks


def _acting_on_own_loan(db, loan):
    """True if the current user is the loan's applicant. Separation of duties:
    an officer must never approve or run due diligence on their own loan, even
    if they hold an approver role (e.g. an admin who is also a member)."""
    me = member_for_user(db)
    return bool(me and loan and me['id'] == loan['member_id'])


def _loan_fees(db, amount):
    """What is withheld at disbursement, read from settings rather than assumed.

    The two settings are different kinds of number and must not be confused:
      * ``insurance_rate``        — a percentage ("Insurance Premium Rate (%)")
      * ``loan_application_fee``  — a flat naira amount ("Loan Application Fee (₦)")

    Both used to be hard-coded at 1% of the loan. That silently charged members
    an application fee their cooperative had set to zero, so the configured
    value is the only thing trusted here.
    """
    amount = round(float(amount or 0), 2)

    def _setting(key, default):
        row = db.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        try:
            value = float(row['value']) if row and row['value'] not in (None, '') else default
        except (TypeError, ValueError):
            return default
        return value if value >= 0 else default

    insurance = round(amount * _setting('insurance_rate', 1.0) / 100.0, 2)
    application_fee = round(_setting('loan_application_fee', 0.0), 2)

    # A member must never be disbursed a negative amount because the fees were
    # set higher than the loan itself.
    if insurance + application_fee > amount:
        application_fee = max(0.0, round(amount - insurance, 2))
    return insurance, application_fee


def _disburse_loan(db, loan, cash_account, posting_date=None):
    """Final approval: book fees, disburse, post the GL entry, notify the member.

    `cash_account` is the account the money actually leaves — the approver picks
    it, because a cooperative holding several bank accounts does not pay every
    loan out of the same one, and crediting the wrong account leaves both that
    bank's reconciliation and the one that really paid wrong.

    Expects `loan` row; assumes caller commits."""
    insurance, application_fee = _loan_fees(db, loan['amount'])
    disbursed = round(loan['amount'] - insurance - application_fee, 2)
    posting_date = posting_date or datetime.now()
    # The balance is created here, not at application. Until this moment the
    # member has asked for a loan, not taken one, and a request must not read as
    # something owed on their account.
    db.execute('''
        UPDATE loans SET status = 'active', approval_stage = 'approved',
            approved_at = ?, approved_by = ?, insurance_premium = ?, application_fee = ?,
            disbursed_amount = ?, disbursement_date = ?, first_payment_date = ?,
            balance = ?
        WHERE id = ?
    ''', (datetime.now(), current_user.id, insurance, application_fee, disbursed,
          posting_date, posting_date + timedelta(days=30),
          loan['total_repayment'] or loan['amount'], loan['id']))
    # The application fee is the cooperative's income. The 1% insurance is withheld
    # on behalf of the insurer — a pass-through liability, not income — so it posts
    # to Insurance Payable and is NOT logged in the revenue table.
    if application_fee:
        record_revenue(db, 'Loan Application Fee', application_fee,
                       description=f"Application fee on loan {loan['loan_number']}",
                       source=f"Loan {loan['loan_number']}", received_by=current_user.id)
    # A fee set to zero is a real choice, not a missing value, so it contributes
    # no line at all rather than a ₦0.00 one cluttering the entry.
    _lines = [
        {'account': LOANS_RECEIVABLE, 'debit': loan['amount'], 'memo': loan['loan_number']},
        {'account': cash_account, 'credit': disbursed, 'memo': 'Net disbursed'},
    ]
    if application_fee:
        _lines.append({'account': FEE_INCOME, 'credit': application_fee, 'memo': 'Application fee'})
    if insurance:
        _lines.append({'account': INSURANCE_PAYABLE, 'credit': insurance,
                       'memo': 'Insurance premium held for insurer'})
    post_journal(db, f"Loan disbursement — {loan['loan_number']}", _lines,
                 date=posting_date, reference=loan['loan_number'],
                 source_module='loan_disbursement',
                 source_id=loan['id'], created_by=current_user.id)
    member = db.execute('SELECT * FROM members WHERE id = ?', (loan['member_id'],)).fetchone()
    if member and member['email']:
        send_loan_approval_email(member['email'], member, loan)
        notify_member(db, member['email'], 'Loan Approved',
                      f"Your loan of ₦{loan['amount']:,.2f} has been fully approved and "
                      f"₦{disbursed:,.2f} will be disbursed.", 'success', '/my-loans')


@loans.route('/loans/download-repayment-template')
@login_required
@role_required('admin', 'treasurer')
def download_repayment_template():
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['loan_number', 'amount', 'payment_date', 'payment_method', 'counter_account', 'receipt_number', 'notes'])
    writer.writerow(['LOAN/20250428/0001', '25000', '2025-04-28', 'transfer', '1000', 'RCPT-001', 'First repayment'])
    writer.writerow(['LOAN/20250428/0002', '50000', '2025-04-29', 'cash', '1000', '', 'Partial payment'])
    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = 'attachment; filename=loan_repayment_template.csv'
    return response


@loans.route('/loans')
@login_required
@role_required('admin', 'treasurer', 'secretary', 'exco')
def loans_list():
    db = get_db()
    all_loans = db.execute('''
        SELECT l.*, m.first_name || ' ' || m.last_name as member_name
        FROM loans l
        JOIN members m ON l.member_id = m.id
        ORDER BY l.date_applied DESC
    ''').fetchall()
    active_loans = db.execute("SELECT SUM(amount) FROM loans WHERE status = 'active'").fetchone()[0] or 0
    # Total outstanding = what members still owe on active loans (sum of balances),
    # as opposed to the principal originally disbursed.
    summary = db.execute(
        "SELECT COUNT(*) AS c, COALESCE(SUM(balance), 0) AS outstanding "
        "FROM loans WHERE status = 'active'"
    ).fetchone()
    active_count = summary['c']
    total_outstanding = summary['outstanding']
    booked_interest = db.execute('''
        SELECT COALESCE(SUM(COALESCE(total_repayment, 0) - COALESCE(amount, 0)), 0)
          FROM loans
         WHERE status IN ('active', 'completed')
    ''').fetchone()[0] or 0

    # Real delinquency: compare each active loan's expected repayments-to-date
    # against what has actually been repaid, and age the shortfall.
    ageing = portfolio_delinquency(db)

    # Loan requests waiting on the committee — the queue that must never go quiet.
    pipeline = la.pipeline_snapshot(db)
    bank_accounts = get_postable_cash_accounts(db)
    default_cash_account = get_default_cash_account(db)

    return render_template('admin/loans.html', loans=all_loans, active_loans=active_loans,
                           active_count=active_count, total_outstanding=total_outstanding,
                           booked_interest=booked_interest,
                           overdue_loans=ageing['loans'], ageing=ageing,
                           pipeline=pipeline,
                           bank_accounts=bank_accounts,
                           default_cash_account=default_cash_account)


def _money(raw):
    try:
        return round(float(str(raw or '0').replace(',', '').strip()), 2)
    except (TypeError, ValueError):
        return 0.0


def _active_loans_for_member(db, member_number):
    return db.execute('''
        SELECT l.*, m.member_number, m.first_name || ' ' || m.last_name AS member_name
        FROM loans l
        JOIN members m ON m.id = l.member_id
        WHERE m.member_number = ? AND l.status = 'active' AND COALESCE(l.balance, 0) > 0
        ORDER BY COALESCE(l.disbursement_date, l.date_applied) DESC, l.id DESC
    ''', (member_number,)).fetchall()


def _find_active_loan(db, loan_number):
    return db.execute('''
        SELECT l.*, m.member_number, m.first_name || ' ' || m.last_name AS member_name
        FROM loans l
        JOIN members m ON m.id = l.member_id
        WHERE l.loan_number = ? AND l.status = 'active' AND COALESCE(l.balance, 0) > 0
    ''', (loan_number,)).fetchone()


def _post_loan_balance_adjustment(db, loan, amount, direction, reason, reference, source_file):
    if amount <= 0:
        raise ValueError('Correction amount must be greater than zero.')
    if direction not in ('increase', 'decrease'):
        raise ValueError('Correction direction must be increase or decrease.')
    if reference and db.execute(
        'SELECT 1 FROM loan_adjustments WHERE reference = ?', (reference,)
    ).fetchone():
        return None

    previous = _money(loan['balance'])
    if direction == 'decrease':
        amount = min(amount, previous)
        new_balance = round(previous - amount, 2)
        lines = [
            {'account': ACCUM_SURPLUS, 'debit': amount, 'memo': reason[:120]},
            {'account': LOANS_RECEIVABLE, 'credit': amount, 'memo': loan['loan_number']},
        ]
    else:
        new_balance = round(previous + amount, 2)
        lines = [
            {'account': LOANS_RECEIVABLE, 'debit': amount, 'memo': loan['loan_number']},
            {'account': ACCUM_SURPLUS, 'credit': amount, 'memo': reason[:120]},
        ]

    adjustment_number = f"LADJ/{datetime.now().strftime('%Y%m%d%H%M%S')}/{loan['id']}"
    db.execute('''
        INSERT INTO loan_adjustments
            (adjustment_number, loan_id, member_id, amount, direction,
             previous_balance, new_balance, reason, reference, source_file, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (adjustment_number, loan['id'], loan['member_id'], amount, direction,
          previous, new_balance, reason, reference, source_file, current_user.id))
    adjustment_id = last_insert_id(db)
    journal_id = post_journal(
        db,
        f"Loan balance correction - {loan['loan_number']}",
        lines,
        date=datetime.now(),
        reference=reference or adjustment_number,
        source_module='loan_adjustment',
        source_id=adjustment_id,
        created_by=current_user.id,
    )
    new_status = 'completed' if new_balance <= 0 else 'active'
    completed_at = datetime.now() if new_status == 'completed' else None
    db.execute(
        'UPDATE loans SET balance = ?, status = ?, completed_at = ? WHERE id = ?',
        (new_balance, new_status, completed_at, loan['id'])
    )
    db.execute(
        'UPDATE loan_adjustments SET journal_entry_id = ? WHERE id = ?',
        (journal_id, adjustment_id)
    )
    return {
        'adjustment_number': adjustment_number,
        'loan_number': loan['loan_number'],
        'member_number': loan['member_number'],
        'amount': amount,
        'direction': direction,
        'previous_balance': previous,
        'new_balance': new_balance,
    }


@loans.route('/loans/corrections', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'treasurer')
def loan_corrections():
    if request.method == 'POST':
        uploaded = request.files.get('file')
        if not uploaded or not uploaded.filename:
            flash('Choose the approved loan correction CSV first.', 'danger')
            return redirect(url_for('loans.loan_corrections'))
        if not uploaded.filename.lower().endswith('.csv'):
            flash('Loan corrections must be uploaded as a CSV file.', 'danger')
            return redirect(url_for('loans.loan_corrections'))

        db = get_db()
        posted, skipped, errors = [], 0, []
        try:
            reader = csv.DictReader(TextIOWrapper(uploaded.stream, encoding='utf-8-sig'))
            missing = LOAN_CORRECTION_COLUMNS - set(reader.fieldnames or [])
            if missing:
                flash(f'Missing columns: {", ".join(sorted(missing))}', 'danger')
                return redirect(url_for('loans.loan_corrections'))

            for row_num, row in enumerate(reader, start=2):
                try:
                    if (row.get('review_status') or '').strip().lower() != 'approved_for_correction':
                        skipped += 1
                        continue
                    correction = _money(row.get('adjustment_needed_correct_less_live'))
                    if abs(correction) < 0.01:
                        skipped += 1
                        continue
                    direction = 'increase' if correction > 0 else 'decrease'
                    remaining = abs(correction)
                    member_number = (row.get('member_number') or '').strip()
                    live_numbers = [
                        n.strip() for n in (row.get('current_coopms_loan_numbers') or '').split(';')
                        if n.strip()
                    ]
                    target_loans = []
                    for loan_number in live_numbers:
                        loan = _find_active_loan(db, loan_number)
                        if loan:
                            target_loans.append(loan)
                    if not target_loans and member_number:
                        target_loans = list(_active_loans_for_member(db, member_number))
                    if not target_loans:
                        errors.append(
                            f"Row {row_num}: no active CoopMS loan found for member {member_number}; "
                            "create/import missing loans separately."
                        )
                        continue

                    reason = (
                        f"Approved SMT loan reconciliation. Correct balance "
                        f"{row.get('correct_active_balance')}; previous CoopMS balance "
                        f"{row.get('current_coopms_balance')}. {row.get('suggested_action') or ''}"
                    ).strip()
                    for loan in target_loans:
                        if remaining <= 0.005:
                            break
                        amount = remaining
                        if direction == 'decrease':
                            amount = min(remaining, _money(loan['balance']))
                        reference = (
                            f"LOAN-CORR-{member_number or loan['member_number']}-"
                            f"{loan['id']}-{datetime.now().strftime('%Y%m%d')}"
                        )
                        result = _post_loan_balance_adjustment(
                            db, loan, amount, direction, reason, reference, uploaded.filename
                        )
                        if result:
                            posted.append(result)
                        else:
                            skipped += 1
                        remaining = round(remaining - amount, 2)
                    if remaining > 0.005:
                        errors.append(
                            f"Row {row_num}: ₦{remaining:,.2f} could not be applied; "
                            "available live loan balance was lower than the requested reduction."
                        )
                except Exception as exc:
                    errors.append(f"Row {row_num}: {exc}")

            if errors:
                db.rollback()
                flash('No corrections were posted because the file has errors. Fix the rows and upload again.', 'danger')
                for err in errors[:10]:
                    flash(err, 'warning')
            else:
                audit(db, 'LOAN_CORRECTION_IMPORT', 'loans',
                      f"Posted {len(posted)} loan corrections from {uploaded.filename}; {skipped} skipped")
                db.commit()
                flash(f'Posted {len(posted)} loan correction entries. {skipped} rows skipped.', 'success')
                return redirect(url_for('loans.loans_list'))
        except Exception as exc:
            db.rollback()
            flash(f'Could not process correction file: {exc}', 'danger')

    return render_template('admin/loan-corrections.html')


@loans.route('/loans/apply', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'treasurer', 'secretary', 'exco')
def apply_loan():
    db = get_db()

    max_tenure_row = db.execute("SELECT value FROM settings WHERE key = 'max_tenure_months'").fetchone()
    max_tenure = int(max_tenure_row['value']) if max_tenure_row else 18

    # Load all interest_* settings in one query
    interest_rows = db.execute("SELECT key, value FROM settings WHERE key LIKE 'interest_%'").fetchall()
    s_raw = {row['key']: row['value'] for row in interest_rows}

    interest_rates = {
        'Regular':        float(s_raw.get('interest_regular',    11)),
        'Housing':        float(s_raw.get('interest_housing',     9)),
        'Emergency':      float(s_raw.get('interest_emergency',  10)),
        'Asset Purchase': float(s_raw.get('interest_asset',      10)),
        'School Fees':    float(s_raw.get('interest_school_fees', 9)),
    }
    interest_methods = {
        'Regular':        s_raw.get('interest_method_regular',    'reducing_annual'),
        'Housing':        s_raw.get('interest_method_housing',    'reducing_annual'),
        'Emergency':      s_raw.get('interest_method_emergency',  'reducing_annual'),
        'Asset Purchase': s_raw.get('interest_method_asset',      'reducing_annual'),
        'School Fees':    s_raw.get('interest_method_school_fees','flat'),
    }

    if request.method == 'POST':
        member_id = request.form.get('member_id')
        try:
            amount = finite_float(request.form.get('amount', 0))
        except ValueError:
            flash('Enter a valid loan amount.', 'danger')
            return redirect(url_for('loans.apply_loan'))
        purpose   = request.form.get('purpose', '').strip()
        tenure    = int(request.form.get('tenure', 0))

        if not member_id or amount <= 0 or not purpose or tenure <= 0:
            flash('All fields are required and must be valid.', 'danger')
            return redirect(url_for('loans.apply_loan'))

        limit_error = loan_limits.application_error(db, purpose, amount, tenure)
        if limit_error:
            flash(limit_error, 'danger')
            return redirect(url_for('loans.apply_loan'))

        if purpose not in interest_rates:
            flash('Select a valid loan type.', 'danger')
            return redirect(url_for('loans.apply_loan'))

        try:
            member = db.execute('SELECT * FROM members WHERE id = ?', (member_id,)).fetchone()
            if not member:
                flash('Member not found.', 'danger')
                return redirect(url_for('loans.apply_loan'))

            eligible_age, joined_at, _days_as_member = member_has_minimum_membership(member, 6)
            if not joined_at:
                flash('Member join date is missing. Please contact admin.', 'danger')
                return redirect(url_for('members.member_details', member_id=member_id))
            if not eligible_age:
                flash('Member must be registered for at least 6 months.', 'danger')
                return redirect(url_for('members.member_details', member_id=member_id))

            # Eligibility uses the savings ledger (source of truth), not the
            # cached members.total_savings column, which can drift.
            savings_balance = member_savings_balance(db, member_id)
            if savings_balance < 50000:
                flash(f'Minimum savings of ₦50,000 required (current: ₦{savings_balance:,.2f}).', 'danger')
                return redirect(url_for('members.member_details', member_id=member_id))

            if has_unpaid_loan_of_type(db, member_id, purpose):
                flash(f'Member has an unpaid {purpose} loan. Repay it fully before applying for another {purpose} loan. Other loan types can be submitted for approval.', 'danger')
                return redirect(url_for('members.member_details', member_id=member_id))

            max_loan = loan_limits.eligible_amount(db, savings_balance, purpose)
            if amount > max_loan:
                flash(f'Maximum loan amount is ₦{max_loan:,.2f} — '
                      f'{loan_limits.eligibility_note(db, savings_balance)}', 'danger')
                return redirect(url_for('members.member_details', member_id=member_id))

            # Look up rate and method for the chosen purpose
            interest_rate   = interest_rates.get(purpose, interest_rates.get('Regular', 11))
            interest_method = interest_methods.get(purpose, 'reducing_annual')

            monthly_payment, total_repayment, _ = compute_loan_schedule(
                amount, interest_rate, tenure, interest_method
            )

            loan_number = f"LOAN/{datetime.now().strftime('%Y%m%d')}/{random.randint(1000, 9999)}"

            guarantor_ids = [g for g in request.form.getlist('guarantors')
                             if g and g != str(member_id)]
            required_g = lw.guarantors_required(db)
            initial_stage = lw.STAGE_GUARANTORS if required_g > 0 else lw.STAGE_SECRETARY

            db.execute('''
                INSERT INTO loans (
                    loan_number, member_id, amount, purpose, tenure, interest_rate,
                    interest_method, total_repayment, balance, status, approval_stage, date_applied
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            ''', (loan_number, member_id, amount, purpose, tenure, interest_rate,
                  interest_method, total_repayment, 0, initial_stage, datetime.now()))
            loan_id = last_insert_id(db)
            lw.record_action(db, loan_id, 'submitted', 'submitted',
                             acted_by=current_user.id, acted_by_name=current_user.username,
                             comment=f'Application submitted (min {required_g} guarantor(s))')

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
            audit(db, 'APPLY_LOAN', 'loans', f"Loan {loan_number} applied for member {member_id}")
            # Log the request and alert the President, Treasurer, General Secretary
            # and exco immediately — with the full application attached.
            la.notify_loan_submitted(db, loan_id, channel='admin')
            g_msg = f" {len(guarantor_ids)} guarantor(s) notified." if guarantor_ids else ""
            flash(f'Loan application submitted!{g_msg} The approval workflow has started.', 'success')
            return redirect(url_for('loans.loan_detail', loan_id=loan_id))

        except Exception as e:
            db.rollback()
            flash(f'Error applying for loan: {str(e)}', 'danger')
            return redirect(url_for('loans.apply_loan'))

    all_members = db.execute(
        "SELECT id, first_name, last_name FROM members WHERE status = 'active'"
    ).fetchall()
    return render_template('admin/apply-loan.html',
                           members=all_members,
                           max_tenure=max_tenure,
                           loan_limits=loan_limits.limits(db),
                           interest_rates=interest_rates,
                           interest_methods=interest_methods,
                           method_labels=METHOD_LABELS)


@loans.route('/loans/<int:loan_id>')
@login_required
@role_required('admin', 'treasurer', 'secretary', 'exco')
def loan_detail(loan_id):
    db = get_db()
    loan = db.execute('''
        SELECT l.*, m.first_name, m.last_name, m.member_number, m.email
        FROM loans l JOIN members m ON m.id = l.member_id WHERE l.id = ?
    ''', (loan_id,)).fetchone()
    if not loan:
        flash('Loan not found.', 'danger')
        return redirect(url_for('loans.loans_list'))
    guarantors = db.execute('''
        SELECT lg.*, m.first_name, m.last_name, m.member_number
        FROM loan_guarantors lg JOIN members m ON m.id = lg.member_id
        WHERE lg.loan_id = ? ORDER BY lg.id
    ''', (loan_id,)).fetchall()
    history = db.execute('SELECT * FROM loan_approvals WHERE loan_id = ? ORDER BY id', (loan_id,)).fetchall()
    repayments = db.execute('''
        SELECT r.*, je.id AS journal_entry_id, je.entry_number
        FROM repayments r
        LEFT JOIN journal_entries je
          ON je.source_module = 'loan_repayment' AND je.source_id = r.id
        WHERE r.loan_id = ?
        ORDER BY r.date DESC, r.id DESC
    ''', (loan_id,)).fetchall()
    accepted, required = lw.guarantor_progress(db, loan_id)
    stage = loan['approval_stage'] or 'secretary'
    can_act = lw.can_act(current_user.role, stage) and loan['status'] == 'pending'
    due_diligence_complete, due_diligence_checks = _due_diligence_complete(loan)
    # First time an officer opens a pending request, start the response clock.
    if loan['status'] == 'pending':
        la.mark_first_response(db, loan_id, current_user.id, current_user.username, current_user.role)
    # Only the final stage pays money out, so only it needs to choose a bank.
    is_disbursing_stage = lw.NEXT_STAGE.get(stage) == lw.STAGE_APPROVED

    return render_template('admin/loan-detail.html',
                           bank_accounts=get_postable_cash_accounts(db),
                           default_cash_account=get_default_cash_account(db),
                           is_disbursing_stage=is_disbursing_stage,
                           alert_events=la.loan_events(db, loan_id, limit=40),
                           loan=loan, guarantors=guarantors, history=history,
                           repayments=repayments,
                           stage=stage, stage_label=lw.STAGE_LABELS.get(stage, stage),
                           accepted=accepted, required=required, can_act=can_act,
                           due_diligence_complete=due_diligence_complete,
                           due_diligence_checks=due_diligence_checks,
                           stage_actor=lw.STAGE_ACTOR_LABEL.get(stage, ''),
                           stage_labels=lw.STAGE_LABELS)


@loans.route('/loans/<int:loan_id>/due-diligence', methods=['POST'])
@login_required
@role_required('admin', 'treasurer', 'secretary')
def update_due_diligence(loan_id):
    db = get_db()
    loan = db.execute('SELECT * FROM loans WHERE id = ?', (loan_id,)).fetchone()
    if not loan:
        flash('Loan not found.', 'danger')
        return redirect(url_for('loans.loans_list'))
    if loan['status'] != 'pending':
        flash('Due diligence can only be updated on pending loan applications.', 'warning')
        return redirect(url_for('loans.loan_detail', loan_id=loan_id))
    if _acting_on_own_loan(db, loan):
        flash('You cannot run due diligence on your own loan application. '
              'A different officer must handle it.', 'danger')
        return redirect(url_for('loans.loan_detail', loan_id=loan_id))

    applicant_type = _loan_applicant_type(loan)
    payment_collateral_status = 'verified' if request.form.get('payment_collateral_verified') else 'pending'
    comment = request.form.get('comment', '').strip()

    if applicant_type == 'staff':
        hr_status = 'confirmed' if request.form.get('hr_affordability_confirmed') else 'pending'
        db.execute('''
            UPDATE loans
               SET hr_affordability_status = ?,
                   payment_collateral_status = ?,
                   due_diligence_updated_by = ?,
                   due_diligence_updated_at = ?
             WHERE id = ?
        ''', (hr_status, payment_collateral_status, current_user.id, datetime.now(), loan_id))
    else:
        bank_status = 'received' if request.form.get('bank_statement_received') else 'requested'
        credit_status = 'completed' if request.form.get('credit_check_completed') else 'pending'
        db.execute('''
            UPDATE loans
               SET bank_statement_status = ?,
                   credit_check_status = ?,
                   payment_collateral_status = ?,
                   due_diligence_updated_by = ?,
                   due_diligence_updated_at = ?
             WHERE id = ?
        ''', (bank_status, credit_status, payment_collateral_status,
              current_user.id, datetime.now(), loan_id))

    updated = db.execute('SELECT * FROM loans WHERE id = ?', (loan_id,)).fetchone()
    complete, checks = _due_diligence_complete(updated)
    action = 'verified' if complete else 'updated'
    summary = ', '.join(f"{c['label']}: {c['status']}" for c in checks)
    lw.record_action(db, loan_id, 'due_diligence', action, current_user.id,
                     current_user.username, comment or summary)
    audit(db, 'LOAN_DUE_DILIGENCE_UPDATE', 'loans',
          f"Loan {loan['loan_number']} due diligence {action}: {summary}")
    db.commit()
    flash('Due diligence complete. This loan can now reach final approval/disbursement.'
          if complete else 'Due diligence checklist updated.', 'success')
    return redirect(url_for('loans.loan_detail', loan_id=loan_id))


@loans.route('/loans/<int:loan_id>/application.pdf')
@login_required
@role_required('admin', 'treasurer', 'secretary', 'exco')
def loan_application_pdf(loan_id):
    """The member's full application as a PDF — the same file attached to the
    alert emails, so an officer can always re-download it."""
    db = get_db()
    pdf_bytes, filename = build_loan_application_pdf(db, loan_id)
    if not pdf_bytes:
        flash('Could not build the application PDF for this loan.', 'danger')
        return redirect(url_for('loans.loan_detail', loan_id=loan_id))
    audit(db, 'LOAN_APPLICATION_PDF', 'loans', f'Downloaded application PDF for loan {loan_id}')
    db.commit()
    response = make_response(pdf_bytes)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'inline; filename={filename}'
    return response


@loans.route('/loans/<int:loan_id>/resend-alert', methods=['POST'])
@login_required
@role_required('admin', 'treasurer', 'secretary')
def resend_loan_alert(loan_id):
    """Re-send the committee alert for a request that was missed."""
    db = get_db()
    loan = db.execute('SELECT id, loan_number, approval_stage FROM loans WHERE id = ?',
                      (loan_id,)).fetchone()
    if not loan:
        flash('Loan not found.', 'danger')
        return redirect(url_for('loans.loans_list'))
    sent = la.notify_loan_submitted(db, loan_id, channel='resend')
    audit(db, 'LOAN_ALERT_RESEND', 'loans',
          f"Re-sent loan request alert for {loan['loan_number']} to {sent} recipient(s)")
    db.commit()
    flash(f'Loan request alert re-sent to {sent} officer(s), with the application attached.'
          if sent else 'No alert was sent — check that loan request alerts are enabled in Settings.',
          'success' if sent else 'warning')
    return redirect(url_for('loans.loan_detail', loan_id=loan_id))


@loans.route('/tasks/loans/pipeline-sweep', methods=['GET', 'POST'])
def loan_pipeline_sweep():
    """Chase every loan request that has gone quiet: reminders past the SLA,
    escalation to the President and exco past the escalation window.

    Two ways in, so a cooperative can run it either way:
      * a scheduler (cron, Render/Railway job, uptime pinger) calling this with
        ``X-Task-Token``/``?token=`` matching the TASK_RUNNER_TOKEN env var;
      * a logged-in officer pressing "Chase pending requests" in the UI.
    """
    db = get_db()
    token = (os.environ.get('TASK_RUNNER_TOKEN', '') or '').strip()
    provided = (request.headers.get('X-Task-Token', '') or request.args.get('token', '')).strip()
    by_token = bool(token) and hmac.compare_digest(provided, token)
    by_user = current_user.is_authenticated and la_permissions.user_can('loans.approve')
    if not (by_token or by_user):
        return jsonify({'success': False, 'error': 'Unauthorised'}), 403

    summary = la.run_pipeline_sweep(db)
    if by_user and not by_token:
        audit(db, 'LOAN_PIPELINE_SWEEP', 'loans',
              f"Manual chase: {summary['reminded']} reminded, {summary['escalated']} escalated")
        db.commit()
        flash(f"Checked {summary['checked']} pending request(s): {summary['reminded']} reminder(s), "
              f"{summary['escalated']} escalation(s), {summary['guarantors_chased']} guarantor chase(s).",
              'info')
        return redirect(url_for('loans.loans_list'))
    return jsonify({'success': True, **summary})


@loans.route('/loans/<int:loan_id>/act', methods=['POST'])
@login_required
@role_required('admin', 'treasurer', 'secretary')
def loan_act(loan_id):
    db = get_db()
    action  = request.form.get('action', '')
    comment = request.form.get('comment', '').strip()
    try:
        loan = db.execute(for_update('SELECT * FROM loans WHERE id = ?'), (loan_id,)).fetchone()
        if not loan or loan['status'] != 'pending':
            flash('Loan not found or already processed.', 'danger')
            return redirect(url_for('loans.loans_list'))
        stage = loan['approval_stage'] or 'secretary'
        if not lw.can_act(current_user.role, stage):
            flash(f'Only the {lw.STAGE_ACTOR_LABEL.get(stage, "authorised approver")} '
                  f'(or an admin) can act at this stage.', 'danger')
            return redirect(url_for('loans.loan_detail', loan_id=loan_id))
        if _acting_on_own_loan(db, loan):
            flash('You cannot approve or reject your own loan application. '
                  'A different officer must review it.', 'danger')
            return redirect(url_for('loans.loan_detail', loan_id=loan_id))
        conflict = lw.sod_conflict(db, loan, current_user.id, stage) if action == 'approve' else ''
        if action == 'approve' and not conflict and not lw.segregation_enforced(db):
            # Permissive (small cooperative): allowed, but leave a trail when one
            # person is covering more than one stage of the same loan.
            earlier = db.execute(
                "SELECT stage FROM loan_approvals WHERE loan_id = ? AND acted_by = ? AND action = 'approved'",
                (loan['id'], current_user.id)).fetchone()
            if earlier:
                audit(db, 'LOAN_SAME_OFFICER_MULTI_STAGE', 'loans',
                      f"Loan {loan['loan_number']}: {current_user.username} approved {earlier['stage']} "
                      f"and now {stage} (separation of duties not enforced: "
                      f"{lw.active_officer_count(db)} active officer(s))")
        if conflict:
            audit(db, 'LOAN_SOD_BLOCKED', 'loans', f"Loan {loan['loan_number']} stage {stage}: {conflict}")
            db.commit()
            flash(conflict, 'danger')
            return redirect(url_for('loans.loan_detail', loan_id=loan_id))
        member = db.execute('SELECT * FROM members WHERE id = ?', (loan['member_id'],)).fetchone()

        if action == 'reject':
            lw.record_action(db, loan_id, stage, 'rejected', current_user.id, current_user.username, comment)
            db.execute("UPDATE loans SET status='rejected', approval_stage='rejected', "
                       "rejection_reason=? WHERE id=?", (comment or 'Rejected', loan_id))
            if member and member['email']:
                send_loan_rejection_email(member['email'], member, comment or 'Not stated')
                notify_member(db, member['email'], 'Loan Application Update',
                              f"Your loan was declined at the {lw.STAGE_ACTOR_LABEL.get(stage,'')} "
                              f"stage. Reason: {comment or 'Not stated'}.", 'warning', '/my-loans')
            la.log_event(db, loan_id, 'decided', stage, 'workflow',
                         {'user_id': current_user.id, 'name': current_user.username,
                          'role': current_user.role},
                         'system', 'sent', f'Rejected at {stage}: {comment or "no reason given"}')
            audit(db, 'LOAN_REJECT', 'loans', f"Loan {loan['loan_number']} rejected at {stage}: {comment}")
            db.commit()
            flash('Loan rejected. The member has been notified.', 'info')
            return redirect(url_for('loans.loan_detail', loan_id=loan_id))

        # ── approve ──
        nxt = lw.NEXT_STAGE.get(stage)
        disbursing_account = None
        if nxt == lw.STAGE_APPROVED:
            complete, checks = _due_diligence_complete(loan)
            if not complete:
                pending = ', '.join(c['label'] for c in checks if not c['done'])
                flash(f'Due diligence is incomplete. Complete before final approval/disbursement: {pending}.',
                      'danger')
                return redirect(url_for('loans.loan_detail', loan_id=loan_id))
            # Settled before the approval is recorded: this step pays money out,
            # and a rejected account code must stop the approval rather than
            # send the cash from whichever bank happens to be the default.
            try:
                disbursing_account = resolve_cash_bank_account(
                    db, request.form.get('bank_account', '').strip())
            except UnknownCashAccountError as e:
                flash(str(e), 'danger')
                return redirect(url_for('loans.loan_detail', loan_id=loan_id))
        lw.record_action(db, loan_id, stage, 'approved', current_user.id, current_user.username, comment)
        if nxt == lw.STAGE_APPROVED:
            _disburse_loan(db, loan, disbursing_account)
            la.log_event(db, loan_id, 'decided', stage, 'workflow',
                         {'user_id': current_user.id, 'name': current_user.username,
                          'role': current_user.role},
                         'system', 'sent', 'Final approval — loan disbursed')
            audit(db, 'LOAN_APPROVE_FINAL', 'loans',
                  f"Loan {loan['loan_number']} approved & disbursed from account {disbursing_account}")
            db.commit()
            flash('Loan fully approved and disbursed. The member has been notified.', 'success')
        else:
            db.execute('UPDATE loans SET approval_stage = ? WHERE id = ?', (nxt, loan_id))
            if member and member['email']:
                notify_member(db, member['email'], 'Loan Application Progress',
                              f"Your loan passed the {lw.STAGE_ACTOR_LABEL.get(stage,'')} stage — "
                              f"now {lw.STAGE_LABELS.get(nxt,'')}.", 'info', '/my-loans')
            la.notify_stage_advanced(db, loan_id, nxt, actor_name=current_user.username)
            audit(db, 'LOAN_APPROVE_STAGE', 'loans', f"Loan {loan['loan_number']} {stage} -> {nxt}")
            db.commit()
            flash(f'Approved at {lw.STAGE_ACTOR_LABEL.get(stage,"")} stage. '
                  f'Now {lw.STAGE_LABELS.get(nxt,"")}.', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error processing loan action: {e}', 'danger')
    return redirect(url_for('loans.loan_detail', loan_id=loan_id))


@loans.route('/loans/<int:loan_id>/cancel', methods=['POST'])
@login_required
@role_required('admin', 'treasurer', 'secretary', 'exco')
def cancel_loan_application(loan_id):
    """Take a pending request out of the queue.

    Separate from rejecting it. A rejection is a decision at a stage — the
    committee considered it and said no, and that belongs in the loan's history
    as a decision. A cancellation is the request being withdrawn: the member
    asked, it was entered twice, or the details were wrong. Recording one as the
    other misreads the member's record for as long as it is kept.

    The applicant can already withdraw their own from the member portal. This is
    the same act by an officer, on their behalf.
    """
    db = get_db()
    loan = db.execute('''
        SELECT l.*, m.first_name, m.last_name, m.email
        FROM loans l JOIN members m ON m.id = l.member_id
        WHERE l.id = ?
    ''', (loan_id,)).fetchone()
    if not loan:
        flash('Loan application not found.', 'danger')
        return redirect(url_for('loans.loans_list'))

    if loan['status'] != 'pending':
        flash('Only a pending request can be cancelled. A loan that has been paid out is '
              'corrected from the loan record instead.', 'warning')
        return redirect(url_for('loans.loan_detail', loan_id=loan_id))

    reason = (request.form.get('reason') or '').strip()
    if not reason:
        flash('Give a reason for cancelling this request. It stays on the record.', 'danger')
        return redirect(url_for('loans.loan_detail', loan_id=loan_id))

    now = datetime.now()
    try:
        db.execute('''
            UPDATE loans
               SET status = 'withdrawn', approval_stage = 'withdrawn',
                   withdrawn_at = ?, withdrawn_by = ?, withdrawal_reason = ?
             WHERE id = ?
        ''', (now, current_user.id, reason, loan_id))
        # Guarantors were asked to stand for a request that no longer exists.
        db.execute(
            "UPDATE loan_guarantors SET status = 'withdrawn', responded_at = ? "
            "WHERE loan_id = ? AND status = 'pending'", (now, loan_id))
        lw.record_action(db, loan_id, 'withdrawn', 'withdrawn',
                         acted_by=current_user.id, acted_by_name=current_user.username,
                         comment=f'Cancelled by {current_user.username}: {reason}')
        audit(db, 'LOAN_APPLICATION_CANCELLED', 'loans',
              f"Cancelled loan request {loan['loan_number']} for "
              f"{loan['first_name']} {loan['last_name']}: {reason}")
        if loan['email']:
            notify_member(db, loan['email'], 'Loan Application Cancelled',
                          f"Your loan application {loan['loan_number']} has been cancelled. "
                          f"Reason: {reason}. No loan was created and nothing was posted to "
                          f"your account.", 'info', '/my-loans')
        db.commit()
        flash(f"Request {loan['loan_number']} cancelled. No loan account was created and "
              f"nothing was posted.", 'success')
    except Exception as e:
        db.rollback()
        flash(f'Could not cancel the request: {e}', 'danger')
    return redirect(url_for('loans.loan_detail', loan_id=loan_id))


@loans.route('/loans/bulk-repayments', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'treasurer')
def bulk_loan_repayments():
    if request.method == 'POST':
        db = get_db()
        if 'file' not in request.files or request.files['file'].filename == '':
            flash('No file selected', 'danger')
            return redirect(request.url)

        file = request.files['file']
        if not file.filename.lower().endswith('.csv'):
            record_upload(db, 'loan_repayments', 0, ['Please upload a CSV file'])
            db.commit()
            flash('Please upload a CSV file', 'danger')
            return redirect(request.url)

        try:
            stream = TextIOWrapper(file.stream, encoding='utf-8')
            reader = csv.DictReader(stream)

            required = {'loan_number', 'amount', 'payment_date'}
            if not required.issubset(reader.fieldnames or []):
                missing = required - set(reader.fieldnames or [])
                record_upload(db, 'loan_repayments', 0, [f'Missing columns: {", ".join(sorted(missing))}'])
                db.commit()
                flash(f'Missing columns: {", ".join(missing)}', 'danger')
                return redirect(request.url)

            db = get_db()
            success = 0
            skipped = 0
            errors = []
            warnings = []
            outcomes = []
            batch_token = secrets.token_hex(4)
            # Keep row savepoints inside one transaction on SQLite as well as PostgreSQL.
            db.execute('SAVEPOINT repayment_upload')
            # Serialize bulk uploads through their duplicate check and commit.
            # Existing data needs no unique-index migration or cleanup.
            if USE_POSTGRES:
                db.execute('SELECT pg_advisory_xact_lock(7319041)')
            else:
                db.execute('UPDATE repayments SET id = id WHERE 1 = 0')

            for row_num, row in enumerate(reader, start=2):
                error_start = len(errors)
                processed = False
                duplicate_reason = ''
                loan_number = str(row.get('loan_number') or '').strip()
                db.execute('SAVEPOINT repayment_row')
                try:
                    loan_number = row.get('loan_number', '').strip()
                    amount = finite_float(row.get('amount', 0))
                    payment_date_str = row.get('payment_date', '').strip()
                    payment_method = row.get('payment_method', 'cash').strip().lower()
                    bank_account = row.get('bank_account', '').strip() or request.form.get('bank_account', '').strip()
                    # A row-specific legacy bank code takes precedence over the
                    # form default; explicit counter_account is authoritative.
                    counter_account = row.get('counter_account', '').strip()
                    if not counter_account and not row.get('bank_account', '').strip():
                        counter_account = request.form.get('counter_account', '').strip()
                    receipt_number = row.get('receipt_number', '').strip()
                    notes = row.get('notes', '').strip()

                    if not loan_number or not math.isfinite(amount) or amount <= 0:
                        errors.append(f"Row {row_num}: Invalid loan number or amount")
                        continue

                    # Validate the account before writing this row.
                    try:
                        cash_account = resolve_loan_repayment_counter_account(db, counter_account, bank_account)
                    except UnknownCashAccountError as e:
                        errors.append(f"Row {row_num}: {e}")
                        continue

                    try:
                        payment_date = datetime.strptime(payment_date_str, '%Y-%m-%d')
                    except ValueError:
                        errors.append(f"Row {row_num}: Invalid date format (use YYYY-MM-DD)")
                        continue

                    loan = db.execute(for_update('''
                        SELECT l.*, m.first_name, m.last_name, m.email
                        FROM loans l
                        JOIN members m ON m.id = l.member_id
                        WHERE l.loan_number = ?
                    ''', 'l'), (loan_number,)).fetchone()
                    if not loan:
                        errors.append(f"Row {row_num}: Loan number {loan_number} not found")
                        continue
                    existing = None
                    if receipt_number:
                        existing = db.execute('''
                            SELECT * FROM repayments
                            WHERE TRIM(receipt_number) = ? AND reversed_at IS NULL
                            ORDER BY id LIMIT 1
                        ''', (receipt_number,)).fetchone()
                        if existing and existing['loan_id'] != loan['id']:
                            errors.append(f"Row {row_num}: Receipt {receipt_number} already belongs to another loan; review the receipt number")
                            continue
                    if not existing:
                        # With no receipt on either side, conservatively treat
                        # the same loan/date/amount as already posted. Distinct
                        # nonblank receipts still permit legitimate instalments.
                        existing = db.execute('''
                            SELECT * FROM repayments WHERE loan_id = ?
                            AND CAST(date AS DATE) = CAST(? AS DATE)
                            AND ABS(amount - ?) < 0.005 AND reversed_at IS NULL
                            AND (? = '' OR COALESCE(TRIM(receipt_number), '') = '')
                            ORDER BY id LIMIT 1
                        ''' if USE_POSTGRES else '''
                            SELECT * FROM repayments WHERE loan_id = ?
                            AND date(date) = date(?)
                            AND ABS(amount - ?) < 0.005 AND reversed_at IS NULL
                            AND (? = '' OR COALESCE(TRIM(receipt_number), '') = '')
                            ORDER BY id LIMIT 1
                        ''', (loan['id'], payment_date_str, amount, receipt_number)).fetchone()
                    if existing:
                        duplicate_reason = f"Already imported: {existing['repayment_number']}"
                        if (abs(float(existing['amount']) - amount) >= 0.005
                                or str(existing['date'])[:10] != payment_date_str):
                            duplicate_reason += '; receipt exists with a different amount or date; review the existing repayment'
                        skipped += 1
                        continue
                    if loan['status'] != 'active' or loan['balance'] <= 0:
                        errors.append(f"Row {row_num}: Loan {loan_number} is not active or has no outstanding balance")
                        continue

                    # Pre-liquidation: if amount >= balance, settle in full
                    is_pre_liquidation = amount >= loan['balance']
                    settled_amount = loan['balance'] if is_pre_liquidation else amount

                    repayment_number = f"REP/{datetime.now().strftime('%Y%m%d')}/{batch_token}/{row_num:04d}"
                    repayment_notes = notes
                    if is_pre_liquidation:
                        repayment_notes = ('Pre-liquidation – loan settled in full. ' + (notes or '')).strip()

                    principal_paid, interest_paid = split_repayment(
                        settled_amount, loan['amount'], loan['total_repayment'])

                    db.execute('''
                        INSERT INTO repayments (
                            repayment_number, loan_id, amount, principal_paid, interest_paid,
                            payment_method, receipt_number, notes, date
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (repayment_number, loan['id'], settled_amount, principal_paid, interest_paid,
                          payment_method, receipt_number, repayment_notes, payment_date))
                    rep_id = last_insert_id(db)

                    new_balance = loan['balance'] - settled_amount
                    status = 'completed' if new_balance <= 0 else 'active'
                    completed_at = payment_date if status == 'completed' else None
                    db.execute(
                        'UPDATE loans SET balance = ?, status = ?, completed_at = ? WHERE id = ?',
                        (new_balance, status, completed_at, loan['id'])
                    )
                    post_journal(db, f"Loan repayment — {loan_number}", [
                        {'account': cash_account, 'debit': settled_amount,
                         'memo': f'Repayment via {payment_method}'},
                        {'account': LOANS_RECEIVABLE, 'credit': principal_paid, 'memo': loan_number},
                        {'account': LOAN_INTEREST_INCOME, 'credit': interest_paid, 'memo': 'Interest earned'},
                    ], date=payment_date, reference=repayment_number, source_module='loan_repayment',
                       source_id=rep_id, created_by=current_user.id)
                    processed = True
                    success += 1
                    if amount > settled_amount:
                        warnings.append(
                            f"Row {row_num}: Loan {loan_number}: entered {amount:,.2f}; "
                            f"recorded outstanding balance {settled_amount:,.2f}. Loan completed."
                        )
                    if loan['email']:
                        send_loan_repayment_email(
                            loan['email'],
                            {'first_name': loan['first_name'], 'last_name': loan['last_name']},
                            loan,
                            {
                                'repayment_number': repayment_number,
                                'amount': settled_amount,
                                'principal_paid': principal_paid,
                                'interest_paid': interest_paid,
                                'balance': max(new_balance, 0),
                                'date': payment_date.strftime('%Y-%m-%d'),
                            },
                            url_for('portal.my_loans', _external=True),
                        )
                except Exception as e:
                    if processed:
                        warnings.append(f"Row {row_num}: Repayment recorded; email notification failed: {e}")
                    else:
                        errors.append(f"Row {row_num}: {str(e)}")
                finally:
                    if not processed:
                        db.execute('ROLLBACK TO SAVEPOINT repayment_row')
                    db.execute('RELEASE SAVEPOINT repayment_row')
                    outcomes.append(dict(row=row_num, identifier=loan_number,
                                         status='Skipped' if duplicate_reason else ('Imported' if processed else 'Failed'),
                                         reason=duplicate_reason or ('; '.join(errors[error_start:]) if not processed else 'Repayment recorded')))

            record_upload(db, 'loan_repayments', success, errors, skipped=skipped, warnings=warnings, rows=outcomes)
            db.commit()
            flash('Full results saved in Upload History.', 'info')
            if skipped:
                flash(f'Skipped {skipped} already imported repayment(s). See Upload History for details.', 'info')
            if errors:
                flash(f'Processed {success} repayments. {len(errors)} errors:', 'warning')
                for err in errors[:5]:
                    flash(err, 'danger')
            else:
                flash(f'Successfully recorded {success} loan repayments!', 'success')

        except Exception as e:
            db.rollback()
            record_upload(db, 'loan_repayments', 0, [f'File processing failed; this upload was rolled back: {e}'])
            db.commit()
            flash(f'Error processing file: {str(e)}', 'danger')

        return redirect(url_for('loans.loans_list'))

    db = get_db()
    return render_template('admin/bulk-repayments.html',
                           bank_accounts=get_loan_repayment_counter_accounts(db),
                           default_cash_account=get_default_cash_account(db))


@loans.route('/loans/export')
@login_required
@role_required('admin', 'treasurer')
def export_loans():
    db = get_db()
    all_loans = db.execute('''
        SELECT l.loan_number, m.first_name || ' ' || m.last_name AS member_name,
               l.amount, l.balance, l.status, l.date_applied
        FROM loans l
        JOIN members m ON l.member_id = m.id
        ORDER BY l.date_applied DESC
    ''').fetchall()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Loan Number', 'Member Name', 'Original Amount',
                     'Outstanding Balance', 'Status', 'Date Applied'])
    for loan in all_loans:
        writer.writerow([
            loan['loan_number'],
            loan['member_name'],
            f"₦{loan['amount']:,.2f}",
            f"₦{loan['balance']:,.2f}",
            loan['status'],
            loan['date_applied'][:10] if loan['date_applied'] else '',
        ])

    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = 'attachment; filename=loans_export.csv'
    return response


@loans.route('/loans/export-statements')
@login_required
@role_required('admin', 'treasurer')
def export_loan_statements():
    """Export every loan as a statement-style movement file for audit matching.

    The ordinary loan export is a summary. This report is intentionally flat:
    one row per loan application/opening/repayment movement, with raw numeric
    values that can be compared against an external loan register in Excel.
    """
    db = get_db()
    loans_rows = db.execute('''
        SELECT l.*, m.member_number,
               m.first_name || ' ' || m.last_name AS member_name,
               m.email AS member_email
        FROM loans l
        JOIN members m ON l.member_id = m.id
        ORDER BY m.member_number, l.date_applied, l.id
    ''').fetchall()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'member_number', 'member_name', 'member_email', 'loan_number',
        'loan_status', 'movement_date', 'movement_type', 'movement_reference',
        'description', 'debit_increase', 'credit_decrease', 'principal_paid',
        'interest_paid', 'penalty_paid', 'running_loan_balance',
        'loan_principal', 'total_repayable', 'current_system_balance',
        'payment_method', 'receipt_number', 'bank_reference', 'journal_entry',
        'source_module', 'source_id', 'reversed_at', 'notes'
    ])

    for loan in loans_rows:
        running_balance = 0.0
        total_repayable = float(loan['total_repayment'] or loan['amount'] or 0)
        principal = float(loan['amount'] or 0)

        writer.writerow([
            loan['member_number'], loan['member_name'], loan['member_email'],
            loan['loan_number'], loan['status'],
            (str(loan['date_applied'])[:10] if loan['date_applied'] else ''),
            'APPLICATION', loan['loan_number'], loan['purpose'] or '',
            '', '', '', '', '', '',
            f'{principal:.2f}', f'{total_repayable:.2f}',
            f"{float(loan['balance'] or 0):.2f}", '', '', '', '',
            'loans', loan['id'], '', loan['notes'] or ''
        ])

        if loan['disbursement_date'] or loan['status'] in ('active', 'completed'):
            running_balance = total_repayable
            writer.writerow([
                loan['member_number'], loan['member_name'], loan['member_email'],
                loan['loan_number'], loan['status'],
                (str(loan['disbursement_date'])[:10] if loan['disbursement_date'] else ''),
                'LOAN_OPENED', loan['loan_number'],
                'Loan approved/disbursed and member loan account opened',
                f'{total_repayable:.2f}', '', '', '', '',
                f'{running_balance:.2f}', f'{principal:.2f}',
                f'{total_repayable:.2f}', f"{float(loan['balance'] or 0):.2f}",
                '', '', '', '', 'loan_disbursement', loan['id'], '',
                loan['notes'] or ''
            ])

        repayments = db.execute('''
            SELECT r.*, je.id AS journal_entry_id, je.entry_number
            FROM repayments r
            LEFT JOIN journal_entries je
              ON je.source_module = 'loan_repayment' AND je.source_id = r.id
            WHERE r.loan_id = ?
            ORDER BY r.date, r.id
        ''', (loan['id'],)).fetchall()

        for rep in repayments:
            amount = float(rep['amount'] or 0)
            effective_credit = 0.0 if rep['reversed_at'] else amount
            running_balance = max(0.0, running_balance - effective_credit)
            writer.writerow([
                loan['member_number'], loan['member_name'], loan['member_email'],
                loan['loan_number'], loan['status'],
                (str(rep['date'])[:10] if rep['date'] else ''),
                'REPAYMENT', rep['repayment_number'] or rep['reference'] or rep['receipt_number'] or '',
                'Loan repayment' + (' (reversed)' if rep['reversed_at'] else ''),
                '', f'{amount:.2f}',
                f"{float(rep['principal_paid'] or 0):.2f}",
                f"{float(rep['interest_paid'] or 0):.2f}",
                f"{float(rep['penalty_paid'] or 0):.2f}",
                f'{running_balance:.2f}', f'{principal:.2f}',
                f'{total_repayable:.2f}', f"{float(loan['balance'] or 0):.2f}",
                rep['payment_method'] or '', rep['receipt_number'] or '',
                rep['reference'] or rep['transaction_id'] or '',
                rep['entry_number'] or '', 'loan_repayment', rep['id'],
                (str(rep['reversed_at'])[:19] if rep['reversed_at'] else ''),
                rep['notes'] or ''
            ])

        adjustments = db.execute('''
            SELECT a.*, je.entry_number
            FROM loan_adjustments a
            LEFT JOIN journal_entries je ON je.id = a.journal_entry_id
            WHERE a.loan_id = ?
            ORDER BY a.created_at, a.id
        ''', (loan['id'],)).fetchall()
        for adj in adjustments:
            amount = float(adj['amount'] or 0)
            effective_amount = 0.0 if adj['reversed_at'] else amount
            if adj['direction'] == 'increase':
                running_balance += effective_amount
                debit = amount
                credit = ''
            else:
                running_balance = max(0.0, running_balance - effective_amount)
                debit = ''
                credit = amount
            writer.writerow([
                loan['member_number'], loan['member_name'], loan['member_email'],
                loan['loan_number'], loan['status'],
                (str(adj['created_at'])[:10] if adj['created_at'] else ''),
                'ADJUSTMENT', adj['adjustment_number'] or adj['reference'] or '',
                (adj['reason'] or 'Loan balance correction') + (' (reversed)' if adj['reversed_at'] else ''),
                f'{debit:.2f}' if debit != '' else '',
                f'{credit:.2f}' if credit != '' else '',
                '', '', '', f'{running_balance:.2f}', f'{principal:.2f}',
                f'{total_repayable:.2f}', f"{float(loan['balance'] or 0):.2f}",
                '', '', adj['reference'] or '', adj['entry_number'] or '',
                'loan_adjustment', adj['id'],
                (str(adj['reversed_at'])[:19] if adj['reversed_at'] else ''),
                adj['reason'] or ''
            ])

    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = 'attachment; filename=loan_statements_export.csv'
    return response


@loans.route('/loans/repay/<int:loan_id>', methods=['POST'])
@login_required
@role_required('admin', 'treasurer')
def repay_loan(loan_id):
    db = get_db()
    try:
        # Lock the loan so two concurrent repayments cannot both read the same
        # balance and overwrite each other's reduction.
        loan = db.execute(for_update('SELECT * FROM loans WHERE id = ?'), (loan_id,)).fetchone()
        if not loan:
            flash('Loan not found.', 'danger')
            return redirect(url_for('loans.loans_list'))

        if loan['status'] != 'active':
            flash('Only active loans can receive repayments.', 'warning')
            return redirect(url_for('loans.loans_list'))

        if not claim_submission(db, request.form.get('submission_token')):
            db.rollback()
            flash('This repayment form was already submitted. Check the loan before entering it again.', 'warning')
            return redirect(url_for('loans.loans_list'))

        amount = finite_float(request.form.get('amount', 0))
        method = request.form.get('method', 'cash')
        bank_account = request.form.get('bank_account', '').strip()

        if amount <= 0:
            flash('Payment amount must be greater than zero.', 'danger')
            return redirect(url_for('loans.loans_list'))

        # Settle the receiving account before the repayment is written, so a bad
        # code is refused rather than redirected to a different bank.
        try:
            cash_account = resolve_cash_bank_account(db, bank_account)
        except UnknownCashAccountError as e:
            flash(str(e), 'danger')
            return redirect(url_for('loans.loans_list'))

        # Cap payment at outstanding balance (pre-liquidation)
        is_pre_liq = amount >= loan['balance']
        settled    = loan['balance'] if is_pre_liq else amount

        repayment_number = f"REP/{datetime.now().strftime('%Y%m%d%H%M%S')}/{loan_id}"
        notes = 'Pre-liquidation – loan settled in full.' if is_pre_liq else ''
        principal_paid, interest_paid = split_repayment(
            settled, loan['amount'], loan['total_repayment'])

        db.execute('''
            INSERT INTO repayments
                (repayment_number, loan_id, amount, principal_paid, interest_paid,
                 payment_method, notes, date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (repayment_number, loan_id, settled, principal_paid, interest_paid,
              method, notes, datetime.now()))
        rep_id = last_insert_id(db)

        new_balance = loan['balance'] - settled
        new_status  = 'completed' if new_balance <= 0 else 'active'
        completed_at = datetime.now() if new_status == 'completed' else None

        db.execute(
            'UPDATE loans SET balance = ?, status = ?, completed_at = ? WHERE id = ?',
            (new_balance, new_status, completed_at, loan_id)
        )

        # Double-entry: cash in; principal reduces the receivable; interest is income.
        post_journal_safe(db, f"Loan repayment — {loan['loan_number']}", [
            {'account': cash_account, 'debit': settled, 'memo': f'Repayment via {method}'},
            {'account': LOANS_RECEIVABLE, 'credit': principal_paid, 'memo': loan['loan_number']},
            {'account': LOAN_INTEREST_INCOME, 'credit': interest_paid, 'memo': 'Interest earned'},
        ], reference=repayment_number, source_module='loan_repayment',
           source_id=rep_id, created_by=current_user.id)
        db.commit()

        member = db.execute('SELECT * FROM members WHERE id = ?', (loan['member_id'],)).fetchone()
        if member and member['email']:
            send_loan_repayment_email(
                member['email'],
                member,
                loan,
                {
                    'repayment_number': repayment_number,
                    'amount': settled,
                    'principal_paid': principal_paid,
                    'interest_paid': interest_paid,
                    'balance': max(new_balance, 0),
                    'date': datetime.now().strftime('%Y-%m-%d'),
                },
                url_for('portal.my_loans', _external=True),
            )
            notify_member(db, member['email'],
                          'Loan Repayment Recorded',
                          f"A repayment of ₦{settled:,.2f} has been recorded on your loan. "
                          f"Outstanding balance: ₦{new_balance:,.2f}.",
                          notification_type='info',
                          action_url='/my-loans')

        audit(db, 'LOAN_REPAYMENT', 'loans',
              f"Recorded repayment ₦{settled:,.2f} for loan ID {loan_id} "
              f"to bank account {cash_account} – balance now ₦{new_balance:,.2f}")

        if is_pre_liq:
            flash(f'Loan fully settled! ₦{settled:,.2f} recorded.', 'success')
        else:
            flash(f'Repayment of ₦{settled:,.2f} recorded. Balance: ₦{new_balance:,.2f}', 'success')

    except Exception as e:
        db.rollback()
        flash(f'Error recording repayment: {str(e)}', 'danger')

    return redirect(url_for('loans.loans_list'))


# ── Application-fee refunds ───────────────────────────────────────────────────
#
# Disbursement used to withhold 1% of every loan as an "application fee",
# ignoring the configured amount. Cooperatives running with the fee set to zero
# still had it taken off, so the money has to go back. This is the review side:
# it reads, totals and exports, and posts nothing.

def _fee_refund_bank(row):
    """(masked, full) bank details. Members' account numbers are encrypted at
    rest, so they are decrypted only where an officer genuinely needs them —
    masked on screen, in full only in the payment file they act on."""
    from crypto import decrypt_field, mask_field
    account = decrypt_field(row['account_number'] or '')
    return {
        'bank_name': decrypt_field(row['bank_name'] or ''),
        'account_name': decrypt_field(row['account_name'] or ''),
        'account_number': account,
        'account_masked': mask_field(account) if account else '',
    }


def _fee_refund_rows(db):
    """Loans still carrying a wrongly charged application fee.

    A loan already refunded drops out via the LEFT JOIN, so the list is always
    what is still owed rather than what was ever owed.
    """
    return db.execute('''
        SELECT l.id, l.loan_number, l.amount, l.application_fee, l.disbursed_amount,
               l.disbursement_date, l.status,
               m.id AS member_id, m.member_number, m.first_name, m.last_name,
               m.bank_name, m.account_name, m.account_number
        FROM loans l
        JOIN members m ON m.id = l.member_id
        LEFT JOIN loan_fee_refunds r ON r.loan_id = l.id AND r.reversed_at IS NULL
        WHERE COALESCE(l.application_fee, 0) > 0
          AND l.status IN ('active', 'completed')
          AND r.id IS NULL
        ORDER BY l.disbursement_date DESC, l.id DESC
    ''').fetchall()


def _fee_refunds_done(db):
    return db.execute('''
        SELECT r.*, l.loan_number, m.member_number, m.first_name, m.last_name
        FROM loan_fee_refunds r
        JOIN loans l ON l.id = r.loan_id
        JOIN members m ON m.id = r.member_id
        WHERE r.reversed_at IS NULL
        ORDER BY r.id DESC
    ''').fetchall()


@loans.route('/loans/fee-refunds')
@login_required
@role_required('admin', 'treasurer')
def fee_refunds():
    """Read-only: who is owed an application fee back, and how much."""
    db = get_db()
    rows = _fee_refund_rows(db)
    pending = []
    for r in rows:
        bank = _fee_refund_bank(r)
        fee = round(float(r['application_fee'] or 0), 2)
        disbursed = round(float(r['disbursed_amount'] or 0), 2)
        pending.append({
            'loan_id': r['id'],
            'loan_number': r['loan_number'],
            'member_number': r['member_number'],
            'member_name': f"{r['first_name']} {r['last_name']}",
            'loan_amount': round(float(r['amount'] or 0), 2),
            'fee': fee,
            'disbursed': disbursed,
            'should_have_been': round(disbursed + fee, 2),
            'disbursed_on': r['disbursement_date'],
            'status': r['status'],
            'bank_name': bank['bank_name'],
            'account_masked': bank['account_masked'],
            'has_bank': bool(bank['account_number']),
        })
    done = _fee_refunds_done(db)
    return render_template(
        'loans/fee-refunds.html',
        pending=pending,
        done=done,
        total_pending=round(sum(p['fee'] for p in pending), 2),
        total_done=round(sum(float(d['amount'] or 0) for d in done), 2),
        missing_bank=sum(1 for p in pending if not p['has_bank']),
    )


@loans.route('/loans/fee-refunds/export.csv')
@login_required
@role_required('admin', 'treasurer')
def fee_refunds_export():
    """The same list as a CSV, to check against your own records before paying."""
    db = get_db()
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(['member_number', 'member_name', 'loan_number', 'loan_amount',
                     'fee_charged', 'amount_disbursed', 'should_have_been_disbursed',
                     'disbursed_on', 'loan_status', 'bank_name', 'account_name',
                     'account_number'])
    for r in _fee_refund_rows(db):
        fee = float(r['application_fee'] or 0)
        disbursed = float(r['disbursed_amount'] or 0)
        bank = _fee_refund_bank(r)
        writer.writerow([
            r['member_number'], f"{r['first_name']} {r['last_name']}", r['loan_number'],
            f"{float(r['amount'] or 0):.2f}", f"{fee:.2f}", f"{disbursed:.2f}",
            f"{disbursed + fee:.2f}",
            str(r['disbursement_date'] or '')[:10], r['status'],
            bank['bank_name'], bank['account_name'], bank['account_number'],
        ])
    audit(db, 'EXPORT_FEE_REFUNDS', 'loans',
          'Exported the application-fee refund list, including bank details')
    db.commit()
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition':
                 f'attachment; filename=application-fee-refunds-'
                 f'{datetime.now().strftime("%Y%m%d")}.csv'})
