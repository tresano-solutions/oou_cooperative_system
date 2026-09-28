import csv
from upload_history import record_upload
import os
import random
from datetime import datetime
from io import StringIO, TextIOWrapper

from flask import Blueprint, render_template, redirect, url_for, request, flash, make_response
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from database import get_db, last_insert_id
from email_service import send_payment_confirmation_email
from utils import (role_required, audit, notify_member, record_revenue, share_capital_split,
                   member_savings_balance)
from ledger import (post_journal_safe, get_default_cash_account, resolve_cash_bank_account,
                    reverse_journal_entry, UnknownCashAccountError,
                    get_postable_cash_accounts,
                    PeriodLockedError, MEMBER_DEPOSITS, FEE_INCOME, SHARE_CAPITAL,
                    get_accounts, account_exists, ACCUM_SURPLUS)

savings = Blueprint('savings', __name__)


def _parse_date(raw):
    raw = (raw or '').strip()
    if not raw:
        return datetime.now()
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y'):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(raw.split('.')[0].replace('T', ' '))
    except ValueError:
        return None


def _resolve_member(db, row):
    member_number = (row.get('member_number') or row.get('member_no') or '').strip()
    email = (row.get('email') or '').strip()
    employee_id = (row.get('employee_id') or '').strip()
    phone = (row.get('phone') or '').strip()
    if member_number:
        found = db.execute('SELECT * FROM members WHERE member_number = ?', (member_number,)).fetchone()
        if found:
            return found
    if employee_id:
        found = db.execute('SELECT * FROM members WHERE employee_id = ?', (employee_id,)).fetchone()
        if found:
            return found
    if email:
        found = db.execute('SELECT * FROM members WHERE email = ?', (email,)).fetchone()
        if found:
            return found
    if phone:
        return db.execute('SELECT * FROM members WHERE phone = ?', (phone,)).fetchone()
    return None


def _batch_ref(month):
    suffix = random.randint(1000, 9999)
    return f"SAL-SAV/{month.replace('-', '')}/{datetime.now().strftime('%H%M%S')}/{suffix}"


@savings.route('/savings')
@login_required
@role_required('admin', 'treasurer', 'secretary', 'exco')
def savings_list():
    db = get_db()
    # Hide reversal bookkeeping from the records list: the compensating negative
    # rows (payment_type='reversal') and the originals they cancelled
    # (reversed_at set). They net to zero, so the totals are unchanged, but the
    # list shows only live contributions. The full trail stays on the batch page.
    all_savings = db.execute('''
        SELECT s.*, m.first_name || ' ' || m.last_name as member_name
        FROM savings s
        JOIN members m ON s.member_id = m.id
        WHERE COALESCE(s.payment_type, '') != 'reversal' AND s.reversed_at IS NULL
        ORDER BY s.date DESC
    ''').fetchall()
    total_savings = db.execute(
        "SELECT SUM(amount) FROM savings "
        "WHERE COALESCE(payment_type, '') != 'reversal' AND reversed_at IS NULL"
    ).fetchone()[0] or 0
    batches = db.execute('''
        SELECT import_batch, source_file, MIN(date) AS first_date, MAX(date) AS last_date,
               COUNT(*) AS row_count,
               COALESCE(SUM(amount), 0) AS total_amount,
               COALESCE(SUM(late_fee), 0) AS total_late_fee
        FROM savings
        WHERE import_batch IS NOT NULL AND import_batch != ''
        GROUP BY import_batch, source_file
        ORDER BY MAX(date) DESC, import_batch DESC
        LIMIT 10
    ''').fetchall()
    return render_template('admin/savings.html',
                           savings=all_savings,
                           total_savings=total_savings,
                           batches=batches)


def _batch_rows(db, batch_ref):
    return db.execute('''
        SELECT s.*, m.member_number, m.employee_id,
               m.first_name || ' ' || m.last_name AS member_name,
               m.email, m.phone,
               CASE WHEN je.id IS NULL THEN 0 ELSE 1 END AS posted_to_gl,
               je.entry_number AS journal_entry_number,
               je.id AS journal_entry_id
        FROM savings s
        JOIN members m ON m.id = s.member_id
        LEFT JOIN journal_entries je ON je.reference = s.receipt_number
        WHERE s.import_batch = ?
        ORDER BY s.date ASC, s.id ASC
    ''', (batch_ref,)).fetchall()


@savings.route('/savings/batch/<path:batch_ref>')
@login_required
@role_required('admin', 'treasurer', 'secretary', 'exco')
def salary_batch_detail(batch_ref):
    db = get_db()
    rows = _batch_rows(db, batch_ref)
    if not rows:
        flash('Savings batch not found.', 'warning')
        return redirect(url_for('savings.savings_list'))

    # How many of this batch's ledger entries are still active (reversible) vs
    # already reversed — drives the Reverse-upload control.
    sav_ids = [r['id'] for r in rows]
    active_entries = reversed_entries = 0
    if sav_ids:
        placeholders = ','.join('?' for _ in sav_ids)
        active_entries = db.execute(
            f"SELECT COUNT(*) FROM journal_entries WHERE source_module = 'savings_deposit' "
            f"AND source_id IN ({placeholders}) AND reversed_at IS NULL", sav_ids).fetchone()[0]
        reversed_entries = db.execute(
            f"SELECT COUNT(*) FROM journal_entries WHERE source_module = 'savings_deposit' "
            f"AND source_id IN ({placeholders}) AND reversed_at IS NOT NULL", sav_ids).fetchone()[0]

    summary = {
        'batch_ref': batch_ref,
        'source_file': rows[0]['source_file'] or '',
        'row_count': len(rows),
        'total_amount': sum(float(r['amount'] or 0) for r in rows),
        'total_late_fee': sum(float(r['late_fee'] or 0) for r in rows),
        'posted_count': sum(1 for r in rows if r['posted_to_gl']),
        'active_entries': active_entries,
        'reversed_entries': reversed_entries,
        'first_date': rows[0]['date'],
        'last_date': rows[-1]['date'],
    }
    return render_template('admin/salary-savings-batch.html',
                           batch=summary,
                           rows=rows)


@savings.route('/savings/batch/<path:batch_ref>/export')
@login_required
@role_required('admin', 'treasurer', 'secretary', 'exco')
def salary_batch_export(batch_ref):
    db = get_db()
    rows = _batch_rows(db, batch_ref)
    if not rows:
        flash('Savings batch not found.', 'warning')
        return redirect(url_for('savings.savings_list'))

    out = StringIO()
    writer = csv.writer(out)
    writer.writerow([
        'batch_ref', 'member_number', 'employee_id', 'name', 'email',
        'month', 'date', 'receipt_number', 'amount', 'late_fee',
        'total_paid', 'posted_to_gl', 'journal_entry',
    ])
    for r in rows:
        writer.writerow([
            batch_ref, r['member_number'], r['employee_id'], r['member_name'],
            r['email'], r['month'], str(r['date'])[:10], r['receipt_number'],
            f"{float(r['amount'] or 0):.2f}", f"{float(r['late_fee'] or 0):.2f}",
            f"{float(r['amount'] or 0) + float(r['late_fee'] or 0):.2f}",
            'yes' if r['posted_to_gl'] else 'no', r['journal_entry_number'] or '',
        ])
    response = make_response(out.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    safe_name = batch_ref.replace('/', '_').replace('\\', '_')
    response.headers['Content-Disposition'] = f'attachment; filename=savings_batch_{safe_name}.csv'
    return response


@savings.route('/savings/batch/<path:batch_ref>/reverse', methods=['POST'])
@login_required
@role_required('admin', 'treasurer')
def salary_batch_reverse(batch_ref):
    """Reverse an entire savings upload after a mistake (e.g. a wrong share
    split). Reversal, not deletion: for each row it posts a balancing journal
    entry, restores the member's savings + shares, and leaves a compensating
    record — the original rows and their reason stay for audit. Re-run safe:
    rows already reversed are skipped."""
    db = get_db()
    reason = (request.form.get('reason') or '').strip()
    if not reason:
        flash('Please give a reason for reversing this upload.', 'danger')
        return redirect(url_for('savings.salary_batch_detail', batch_ref=batch_ref))

    rows = db.execute('SELECT id FROM savings WHERE import_batch = ?', (batch_ref,)).fetchall()
    if not rows:
        flash('Savings batch not found.', 'warning')
        return redirect(url_for('savings.savings_list'))

    reversed_n = skipped_n = 0
    errors = []
    try:
        for s in rows:
            entry = db.execute(
                "SELECT id FROM journal_entries "
                "WHERE source_module = 'savings_deposit' AND source_id = ? AND reversed_at IS NULL",
                (s['id'],)).fetchone()
            if not entry:
                skipped_n += 1          # already reversed, or never posted to GL
                continue
            try:
                # The handler marks the original row reversed and frees its
                # receipt number, so the corrected batch can be re-uploaded.
                reverse_journal_entry(db, entry['id'], created_by=current_user.id,
                                      reason=f'Batch {batch_ref} reversed: {reason}')
                reversed_n += 1
            except PeriodLockedError as ple:
                errors.append(str(ple))
            except ValueError:
                skipped_n += 1          # itself a reversal / already reversed / no lines
        audit(db, 'REVERSE_SAVINGS_BATCH', 'savings',
              f"Batch {batch_ref}: reversed {reversed_n}, skipped {skipped_n}. Reason: {reason}")
        db.commit()
    except Exception as e:
        db.rollback()
        flash(f'Could not reverse the upload: {e}', 'danger')
        return redirect(url_for('savings.salary_batch_detail', batch_ref=batch_ref))

    if reversed_n:
        msg = (f'Reversed {reversed_n} row(s) from this upload. Member savings, shares and '
               f'the ledger have been restored.')
        if skipped_n:
            msg += f' {skipped_n} row(s) were already reversed or had no ledger entry.'
        flash(msg, 'success')
    elif errors:
        flash('Nothing was reversed — see the messages below.', 'warning')
    else:
        flash('Nothing to reverse — every row in this upload was already reversed.', 'info')
    for err in errors[:5]:
        flash(err, 'warning')
    return redirect(url_for('savings.salary_batch_detail', batch_ref=batch_ref))


@savings.route('/savings/salary-template')
@login_required
@role_required('admin', 'treasurer')
def download_salary_template():
    out = StringIO()
    writer = csv.writer(out)
    writer.writerow([
        'member_number', 'employee_id', 'email', 'phone', 'amount',
        'month', 'date', 'bank_account', 'receipt_number', 'notes',
    ])
    writer.writerow([
        'MEM/2025/0001', 'EMP001', 'member@example.com', '08012345678',
        '15000', datetime.now().strftime('%Y-%m'), datetime.now().strftime('%Y-%m-%d'),
        '', '', 'July payroll deduction',
    ])
    response = make_response(out.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = 'attachment; filename=salary_savings_template.csv'
    return response


@savings.route('/savings/salary-upload', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'treasurer')
def salary_upload():
    if request.method == 'POST':
        file = request.files.get('file')
        month = request.form.get('month', '').strip()
        batch_ref = request.form.get('batch_ref', '').strip() or _batch_ref(month or datetime.now().strftime('%Y-%m'))
        apply_late_fee = bool(request.form.get('apply_late_fee'))
        batch_bank_account = request.form.get('bank_account', '').strip()

        if not month:
            flash('Payroll month is required.', 'danger')
            return redirect(request.url)
        if not file or not file.filename:
            flash('No CSV file selected.', 'danger')
            return redirect(request.url)
        if not file.filename.lower().endswith('.csv'):
            flash('Please upload a CSV file.', 'danger')
            return redirect(request.url)

        db = get_db()

        # Settled before the file is opened: a whole payroll batch posting to
        # the wrong bank is exactly the misposting the selector exists to stop.
        try:
            batch_cash_account = resolve_cash_bank_account(db, batch_bank_account)
        except UnknownCashAccountError as e:
            flash(str(e), 'danger')
            return redirect(request.url)

        success = 0
        skipped = 0
        errors = []
        try:
            stream = TextIOWrapper(file.stream, encoding='utf-8-sig')
            reader = csv.DictReader(stream)
            fields = set(reader.fieldnames or [])
            if 'amount' not in fields:
                flash('CSV must include an amount column.', 'danger')
                return redirect(request.url)
            if not fields.intersection({'member_number', 'employee_id', 'email', 'phone'}):
                flash('CSV must include at least one member identifier: member_number, employee_id, email, or phone.', 'danger')
                return redirect(request.url)

            for row_num, row in enumerate(reader, start=2):
                try:
                    member = _resolve_member(db, row)
                    if not member:
                        errors.append(f"Row {row_num}: member not found.")
                        continue

                    amount = float((row.get('amount') or '').replace(',', '').strip())
                    if amount <= 0:
                        errors.append(f"Row {row_num}: amount must be greater than zero.")
                        continue

                    payment_date = _parse_date(row.get('date', '')) or datetime.now()
                    row_month = (row.get('month') or month).strip() or month
                    notes = (row.get('notes') or '').strip() or f'Savings batch {batch_ref}'
                    receipt_number = (row.get('receipt_number') or '').strip()
                    if not receipt_number:
                        receipt_number = f"PAYROLL/{row_month.replace('-', '')}/{batch_ref.split('/')[-1]}/{row_num:04d}"

                    # Duplicate = the same receipt already imported (makes re-running
                    # a batch idempotent). A member may legitimately have several
                    # savings in one month — salary deduction plus voluntary savings —
                    # so month is NOT a uniqueness criterion; only the receipt is.
                    exists = db.execute(
                        'SELECT id FROM savings WHERE reversed_at IS NULL AND receipt_number = ?',
                        (receipt_number,),
                    ).fetchone()
                    if exists:
                        skipped += 1
                        continue

                    # A row may name its own account; resolved here, before the
                    # row writes, so a bad code skips the row rather than
                    # committing a deduction whose journal entry never posted.
                    row_bank = (row.get('bank_account') or '').strip()
                    cash_account = (resolve_cash_bank_account(db, row_bank)
                                    if row_bank else batch_cash_account)

                    late_fee = 0.0
                    if apply_late_fee and payment_date.day > 10:
                        late_fee = round(amount * 0.10, 2)

                    deposit_amount, share_amount = share_capital_split(db, amount)

                    db.execute('''
                        INSERT INTO savings
                            (member_id, amount, share_capital, month, payment_type, late_fee,
                             payment_method, receipt_number, notes, date,
                             created_by, import_batch, source_file)
                        VALUES (?, ?, ?, ?, 'salary', ?, 'salary_deduction',
                                ?, ?, ?, ?, ?, ?)
                    ''', (
                        member['id'], deposit_amount, share_amount, row_month, late_fee,
                        receipt_number, notes, payment_date, current_user.id,
                        batch_ref, file.filename,
                    ))
                    sav_id = last_insert_id(db)   # before any revenue/GL INSERT
                    db.execute(
                        'UPDATE members SET total_savings = total_savings + ?, '
                        'shares_value = COALESCE(shares_value, 0) + ? WHERE id = ?',
                        (deposit_amount, share_amount, member['id']),
                    )

                    if late_fee:
                        record_revenue(
                            db, 'Late Fee', late_fee,
                            description=f'Late salary deduction fee for {row_month}',
                            source=f"{member['first_name']} {member['last_name']}",
                            received_by=current_user.id,
                            notes=f'Receipt {receipt_number}; batch {batch_ref}',
                        )

                    lines = [
                        {'account': cash_account, 'debit': amount + late_fee,
                         'memo': f'Salary savings {row_month}'},
                        {'account': MEMBER_DEPOSITS, 'credit': deposit_amount, 'memo': f"Member {member['id']}"},
                    ]
                    if share_amount:
                        lines.append({'account': SHARE_CAPITAL, 'credit': share_amount, 'memo': 'Share capital'})
                    if late_fee:
                        lines.append({'account': FEE_INCOME, 'credit': late_fee, 'memo': 'Late fee'})
                    post_journal_safe(
                        db, f'Salary savings deduction - {row_month}', lines,
                        date=payment_date, reference=receipt_number,
                        source_module='savings_deposit', source_id=sav_id,
                        created_by=current_user.id,
                    )

                    if member['email']:
                        notify_member(
                            db, member['email'], 'Salary Savings Recorded',
                            f"Salary savings of ₦{amount:,.2f} was recorded for {row_month}. "
                            f"Receipt: {receipt_number}.",
                            notification_type='info', action_url='/my-savings',
                        )
                    success += 1
                except Exception as row_error:
                    errors.append(f"Row {row_num}: {row_error}")

            record_upload(db, 'salary_savings', success, errors, skipped=skipped)
            audit(db, 'IMPORT_SALARY_SAVINGS', 'savings',
                  f"Batch {batch_ref}: imported {success}, skipped {skipped}, errors {len(errors)}")
            db.commit()
            flash(f'Batch {batch_ref}: imported {success} savings record(s), skipped {skipped}.', 'success')
            for error in errors[:8]:
                flash(error, 'warning')
            if len(errors) > 8:
                flash(f'{len(errors) - 8} additional row error(s) not shown.', 'warning')
            return redirect(url_for('savings.salary_batch_detail', batch_ref=batch_ref))
        except Exception as e:
            db.rollback()
            flash(f'Error processing salary deduction file: {e}', 'danger')
            return redirect(request.url)

    db = get_db()
    return render_template('admin/salary-savings-upload.html',
                           default_month=datetime.now().strftime('%Y-%m'),
                           default_batch=_batch_ref(datetime.now().strftime('%Y-%m')),
                           bank_accounts=get_postable_cash_accounts(db),
                           default_cash_account=get_default_cash_account(db))


@savings.route('/savings/add', methods=['POST'])
@login_required
@role_required('admin', 'treasurer')
def add_saving():
    member_id     = request.form['member_id']
    amount        = float(request.form['amount'])
    month         = request.form['month']
    payment_type  = request.form.get('payment_type', 'monthly').strip() or 'monthly'
    payment_method = request.form.get('payment_method', 'cash').strip() or 'cash'
    bank_account = request.form.get('bank_account', '').strip()
    notes         = request.form.get('notes', '').strip() or None

    if amount < 5000:
        flash(f'Minimum savings amount is ₦5,000. You entered ₦{amount:,.2f}', 'danger')
        return redirect(url_for('members.member_details', member_id=member_id))

    db = get_db()

    # Settle where the money is being recorded BEFORE anything is written, so a
    # bad account code is rejected outright instead of surfacing as a rollback
    # part-way through recording the contribution.
    try:
        cash_account = resolve_cash_bank_account(db, bank_account)
    except UnknownCashAccountError as e:
        flash(str(e), 'danger')
        return redirect(url_for('members.member_details', member_id=member_id))

    try:
        today = datetime.now()
        # Late fee applies only to monthly/salary savings recorded after the 10th.
        # The fee is cooperative INCOME — it is recorded separately and must NOT
        # inflate the member's savings balance.
        if payment_type in ('monthly', 'salary') and today.day > 10:
            late_fee = round(amount * 0.10, 2)
            flash(f'Late payment: 10% fee of ₦{late_fee:,.2f} applied.', 'info')
        else:
            late_fee = 0

        receipt_number = f"RCPT/{today.strftime('%Y%m%d')}/{random.randint(1000, 9999)}"

        # Allocate a configurable portion of the contribution to share capital.
        deposit_amount, share_amount = share_capital_split(db, amount)

        # savings.amount is the deposit portion; share_capital records the split.
        db.execute('''
            INSERT INTO savings
                (member_id, amount, share_capital, month, payment_type, late_fee,
                 payment_method, receipt_number, notes, date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (member_id, deposit_amount, share_amount, month, payment_type, late_fee,
              payment_method, receipt_number, notes, today))
        sav_id = last_insert_id(db)   # captured before any other INSERT (revenue/GL)

        # Deposits grow by the deposit portion; share capital by the share portion.
        db.execute(
            'UPDATE members SET total_savings = total_savings + ?, '
            'shares_value = COALESCE(shares_value, 0) + ? WHERE id = ?',
            (deposit_amount, share_amount, member_id))

        # Book the late fee as cooperative income.
        if late_fee:
            member_row = db.execute('SELECT first_name, last_name FROM members WHERE id = ?',
                                    (member_id,)).fetchone()
            member_name = (f"{member_row['first_name']} {member_row['last_name']}"
                           if member_row else f"member {member_id}")
            record_revenue(db, 'Late Fee', late_fee,
                           description=f'Late savings fee for {month}',
                           source=member_name, received_by=current_user.id,
                           notes=f'Receipt {receipt_number}')

        # Double-entry: cash in; deposit liability + share capital up; fee is income.
        _lines = [
            {'account': cash_account, 'debit': amount + late_fee,
             'memo': f'Savings {month} via {payment_method}'},
            {'account': MEMBER_DEPOSITS, 'credit': deposit_amount, 'memo': f'Member {member_id}'},
        ]
        if share_amount:
            _lines.append({'account': SHARE_CAPITAL, 'credit': share_amount, 'memo': 'Share capital'})
        if late_fee:
            _lines.append({'account': FEE_INCOME, 'credit': late_fee, 'memo': 'Late fee'})
        post_journal_safe(db, f'Savings deposit — {month}', _lines,
                          reference=receipt_number, source_module='savings_deposit',
                          source_id=sav_id, created_by=current_user.id)

        db.commit()

        saving_id = sav_id   # the savings row id (captured above, before GL posting)
        member    = db.execute('SELECT * FROM members WHERE id = ?', (member_id,)).fetchone()
        new_saving = db.execute('SELECT * FROM savings WHERE id = ?', (saving_id,)).fetchone()
        share_note = f" (₦{deposit_amount:,.2f} to savings, ₦{share_amount:,.2f} to share capital)" if share_amount else ""
        if member and member['email']:
            send_payment_confirmation_email(member['email'], member, new_saving)
            fee_note = f" (plus ₦{late_fee:,.2f} late fee)" if late_fee else ""
            notify_member(db, member['email'],
                          'Savings Payment Confirmed',
                          f"₦{amount:,.2f} {payment_type} contribution recorded for "
                          f"{month}{share_note}{fee_note}. Receipt: {receipt_number}.",
                          notification_type='info',
                          action_url='/my-savings')

        audit(db, 'ADD_SAVING', 'savings',
              f"Recorded ₦{amount:,.2f} {payment_type} contribution for member ID {member_id}, "
              f"receipt {receipt_number}; bank account {cash_account}{share_note}")
        flash(f'Contribution of ₦{amount:,.2f} recorded{share_note}. Receipt: {receipt_number}', 'success')

    except Exception as e:
        db.rollback()
        flash(f'Error recording savings: {str(e)}', 'danger')

    return redirect(url_for('members.member_details', member_id=member_id))


_PAYOUT_EVIDENCE_EXTS = {'pdf', 'png', 'jpg', 'jpeg'}


def _save_payout_evidence(f):
    """Validate + save a payout evidence file under static/uploads/payouts/.
    Returns (True, db_path) or (False, error_message)."""
    name = secure_filename(f.filename or '')
    if '.' not in name:
        return False, 'file must be a PDF or image (PDF, PNG, JPG).'
    ext = name.rsplit('.', 1)[1].lower()
    if ext not in _PAYOUT_EVIDENCE_EXTS:
        return False, 'only PDF, PNG or JPG files are allowed.'
    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(0)
    if size > 8 * 1024 * 1024:
        return False, 'file is larger than 8 MB.'
    unique = f"payout_{datetime.now().strftime('%Y%m%d%H%M%S')}_{random.randint(1000, 9999)}.{ext}"
    rel = f"uploads/payouts/{unique}"
    disk = os.path.join('static', 'uploads', 'payouts', unique)
    os.makedirs(os.path.dirname(disk), exist_ok=True)
    f.save(disk)
    return True, rel



@savings.route('/savings/adjust', methods=['POST'])
@login_required
@role_required('admin', 'treasurer')
def adjust_saving():
    """Correct a member's savings balance without pretending money moved.

    A journal entry alone cannot do this: a member's balance is the sum of their
    savings rows, and the ledger is a separate record, so posting to Member
    Deposits moves the books while the member's dashboard stays exactly as it
    was. This writes both halves together -- a correcting savings row the member
    can see on their statement, and the matching journal entry -- so the two
    can never drift apart.

    Unlike a payout this does not require evidence and is not blocked by an
    outstanding loan, because nothing is being paid to anybody. It is for fixing
    a figure that was recorded wrongly, most often a migrated opening balance.
    """
    db = get_db()
    member_id = request.form.get('member_id')
    member = db.execute('SELECT * FROM members WHERE id = ?', (member_id,)).fetchone()
    if not member:
        flash('Member not found.', 'danger')
        return redirect(url_for('members.members_list'))
    back = redirect(url_for('members.member_details', member_id=member_id))

    try:
        amount = float(request.form.get('amount') or 0)
    except ValueError:
        flash('Enter a valid adjustment amount.', 'danger')
        return back
    amount = round(amount, 2)
    if amount == 0:
        flash('An adjustment cannot be zero. Use a negative amount to reduce the balance.', 'danger')
        return back

    reason = (request.form.get('reason') or '').strip()
    if not reason:
        flash('A reason for the adjustment is required.', 'danger')
        return back

    balance = member_savings_balance(db, member_id)
    if balance + amount < -0.005:
        flash(f'That adjustment would take {member["first_name"]} {member["last_name"]} to '
              f'a negative balance (₦{balance:,.2f} + ₦{amount:,.2f}). Reduce the amount.', 'danger')
        return back

    # The other side of the correction. The app cannot know which account was
    # wrong, so the officer chooses; Accumulated Surplus is the usual home for
    # a prior-period correction.
    contra = (request.form.get('contra_account') or ACCUM_SURPLUS).strip()
    if not account_exists(db, contra):
        flash(f'Account {contra} does not exist.', 'danger')
        return back
    if contra == MEMBER_DEPOSITS:
        flash('The offset account cannot also be Member Deposits — that entry would do nothing.',
              'danger')
        return back

    date = _parse_date(request.form.get('date', '')) or datetime.now()
    receipt = f"ADJ/{datetime.now().strftime('%Y%m%d')}/{random.randint(1000, 9999)}"

    try:
        # Recorded as its own savings row, positive or negative, so the member's
        # statement shows the correction and its reason rather than a balance
        # that silently changed.
        db.execute('''
            INSERT INTO savings
                (member_id, amount, month, payment_type, payment_method,
                 receipt_number, notes, date, created_by)
            VALUES (?, ?, ?, 'adjustment', 'adjustment', ?, ?, ?, ?)
        ''', (member_id, amount, date.strftime('%Y-%m'), receipt, reason, date,
              current_user.id))
        sav_id = last_insert_id(db)

        db.execute('UPDATE members SET total_savings = COALESCE(total_savings, 0) + ? WHERE id = ?',
                   (amount, member_id))

        # An increase raises what the cooperative owes the member; a decrease
        # lowers it. The offset carries the opposite side either way.
        if amount > 0:
            lines = [{'account': contra, 'debit': amount, 'memo': reason},
                     {'account': MEMBER_DEPOSITS, 'credit': amount,
                      'memo': f'Adjustment for {member["member_number"]}'}]
        else:
            lines = [{'account': MEMBER_DEPOSITS, 'debit': -amount,
                      'memo': f'Adjustment for {member["member_number"]}'},
                     {'account': contra, 'credit': -amount, 'memo': reason}]
        post_journal_safe(db, f'Savings adjustment — {member["first_name"]} {member["last_name"]}',
                          lines, reference=receipt, source_module='savings_adjustment',
                          source_id=sav_id, created_by=current_user.id, date=date)

        db.commit()
        audit(db, 'SAVINGS_ADJUSTMENT', 'savings',
              f'Adjusted savings for {member["member_number"]} {member["first_name"]} '
              f'{member["last_name"]} by ₦{amount:,.2f} ({reason}); receipt {receipt}')
        flash(f'Savings adjusted by ₦{amount:,.2f} for {member["first_name"]} '
              f'{member["last_name"]}. New balance: ₦{balance + amount:,.2f}. '
              f'Reference: {receipt}', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Could not adjust savings: {e}', 'danger')
    return back

@savings.route('/savings/payout', methods=['POST'])
@login_required
@role_required('admin', 'treasurer')
def record_payout():
    member_id = request.form.get('member_id')
    db = get_db()
    member = db.execute('SELECT * FROM members WHERE id = ?', (member_id,)).fetchone()
    if not member:
        flash('Member not found.', 'danger')
        return redirect(url_for('members.members_list'))
    back = redirect(url_for('members.member_details', member_id=member_id))

    # Hard rule: no payout while any loan is still outstanding.
    outstanding = db.execute(
        "SELECT COALESCE(SUM(balance), 0) FROM loans WHERE member_id = ? AND status = 'active'",
        (member_id,)).fetchone()[0] or 0
    if float(outstanding) > 0.005:
        flash(f'Payout blocked: {member["first_name"]} {member["last_name"]} still has an '
              f'outstanding loan balance of ₦{float(outstanding):,.2f}. The loan must be '
              f'cleared before any savings can be paid out.', 'danger')
        return back

    balance = member_savings_balance(db, member_id)
    full = request.form.get('full_payout') == '1'
    try:
        amount = balance if full else float(request.form.get('amount') or 0)
    except ValueError:
        flash('Enter a valid payout amount.', 'danger')
        return back
    if amount <= 0:
        flash('Payout amount must be greater than zero.', 'danger')
        return back
    if amount > balance + 0.005:
        flash(f'Payout (₦{amount:,.2f}) exceeds the savings balance (₦{balance:,.2f}).', 'danger')
        return back

    reason = request.form.get('reason', '').strip()
    if not reason:
        flash('A reason for the payout is required.', 'danger')
        return back

    # Which account the money actually left. Settled before anything is written,
    # and before the evidence file is saved to disk.
    try:
        cash_account = resolve_cash_bank_account(db, request.form.get('bank_account', '').strip())
    except UnknownCashAccountError as e:
        flash(str(e), 'danger')
        return back

    evidence = request.files.get('evidence')
    if not evidence or not evidence.filename:
        flash('Payout evidence (PDF or image) is required.', 'danger')
        return back
    ok, res = _save_payout_evidence(evidence)
    if not ok:
        flash(f'Evidence not accepted: {res}', 'danger')
        return back
    evidence_path = res

    method = request.form.get('payment_method', 'bank').strip() or 'bank'
    date = _parse_date(request.form.get('date', '')) or datetime.now()
    try:
        receipt = f"PAYOUT/{datetime.now().strftime('%Y%m%d')}/{random.randint(1000, 9999)}"
        # Withdrawal recorded as a negative savings row so the balance (SUM) drops.
        db.execute('''
            INSERT INTO savings
                (member_id, amount, month, payment_type, payment_method,
                 receipt_number, notes, evidence_path, date, created_by)
            VALUES (?, ?, ?, 'withdrawal', ?, ?, ?, ?, ?, ?)
        ''', (member_id, -amount, date.strftime('%Y-%m'), method, receipt,
              reason, evidence_path, date, current_user.id))
        sav_id = last_insert_id(db)
        db.execute('UPDATE members SET total_savings = total_savings - ? WHERE id = ?',
                   (amount, member_id))

        # Double-entry: the deposit liability falls and cash goes out.
        post_journal_safe(db, f'Savings payout — {member["first_name"]} {member["last_name"]}',
                          [{'account': MEMBER_DEPOSITS, 'debit': amount, 'memo': reason},
                           {'account': cash_account, 'credit': amount,
                            'memo': f'Payout to member {member["member_number"]}'}],
                          reference=receipt, source_module='savings_payout',
                          source_id=sav_id, created_by=current_user.id, date=date)

        # A full payout can double as an exit — archive the member.
        if full and request.form.get('mark_former') == '1':
            db.execute(
                "UPDATE members SET status = 'former', exit_date = ?, exit_reason = ?, "
                "exit_note = ? WHERE id = ?",
                (date, (request.form.get('exit_reason') or 'Resigned').strip(),
                 f'Savings paid out in full: {reason}', member_id))

        db.commit()
        audit(db, 'SAVINGS_PAYOUT', 'savings',
              f'Paid out ₦{amount:,.2f} to {member["member_number"]} '
              f'{member["first_name"]} {member["last_name"]} ({reason}); receipt {receipt}')
        flash(f'Payout of ₦{amount:,.2f} recorded for {member["first_name"]} '
              f'{member["last_name"]}. Receipt: {receipt}', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Could not record payout: {e}', 'danger')
    return back
