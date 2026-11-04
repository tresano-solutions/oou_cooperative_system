import random
from loan_completion import queue_completion, dispatch_completions
from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from database import get_db, last_insert_id
from ledger import (LOAN_INTEREST_INCOME, LOANS_RECEIVABLE, MEMBER_DEPOSITS,
                    SHARE_CAPITAL, UnknownCashAccountError,
                    get_default_cash_account, get_postable_cash_accounts,
                    post_journal, resolve_cash_bank_account, reverse_journal_entry,
                    PeriodLockedError)
from utils import audit, notify_member, role_required, share_capital_split, split_repayment


member_receipts = Blueprint('member_receipts', __name__, url_prefix='/receipts')


def _money(value):
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _receipt_number():
    return f"MR/{datetime.now().strftime('%Y%m%d%H%M%S')}/{random.randint(100, 999)}"


@member_receipts.route('/member-payment', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'treasurer')
def member_payment():
    db = get_db()

    if request.method == 'POST':
        member_id = request.form.get('member_id', type=int)
        receipt_amount = _money(request.form.get('amount'))
        savings_amount = _money(request.form.get('savings_amount'))
        payment_method = (request.form.get('payment_method') or 'transfer').strip()
        bank_reference = (request.form.get('bank_reference') or '').strip()
        notes = (request.form.get('notes') or '').strip()
        bank_account_code = (request.form.get('bank_account') or '').strip()
        receipt_date_raw = (request.form.get('date') or '').strip()

        member = db.execute('SELECT * FROM members WHERE id = ?', (member_id,)).fetchone()
        if not member:
            flash('Select a valid member before posting the receipt.', 'danger')
            return redirect(url_for('member_receipts.member_payment'))
        if receipt_amount <= 0:
            flash('Receipt amount must be greater than zero.', 'danger')
            return redirect(url_for('member_receipts.member_payment'))

        try:
            receipt_date = datetime.strptime(receipt_date_raw, '%Y-%m-%d') if receipt_date_raw else datetime.now()
        except ValueError:
            flash('Receipt date must use YYYY-MM-DD format.', 'danger')
            return redirect(url_for('member_receipts.member_payment'))

        try:
            bank_account = resolve_cash_bank_account(db, bank_account_code)
        except UnknownCashAccountError as e:
            flash(str(e), 'danger')
            return redirect(url_for('member_receipts.member_payment'))

        active_loans = db.execute(
            "SELECT * FROM loans WHERE member_id = ? AND status = 'active' AND balance > 0 ORDER BY id",
            (member_id,),
        ).fetchall()
        loan_allocations = []
        for loan in active_loans:
            amount = _money(request.form.get(f'loan_amount_{loan["id"]}'))
            if amount <= 0:
                continue
            balance = _money(loan['balance'])
            if amount > balance + 0.005:
                flash(f'Loan allocation for {loan["loan_number"]} exceeds its outstanding balance.', 'danger')
                return redirect(url_for('member_receipts.member_payment'))
            principal, interest = split_repayment(amount, loan['amount'], loan['total_repayment'])
            loan_allocations.append({
                'loan': loan,
                'amount': amount,
                'principal': principal,
                'interest': interest,
            })

        total_loans = round(sum(item['amount'] for item in loan_allocations), 2)
        total_allocated = round(savings_amount + total_loans, 2)
        if total_allocated <= 0:
            flash('Allocate the receipt to savings, loan repayment, or both.', 'danger')
            return redirect(url_for('member_receipts.member_payment'))
        if abs(total_allocated - receipt_amount) > 0.005:
            flash('Total allocations must equal the bank receipt amount before posting.', 'danger')
            return redirect(url_for('member_receipts.member_payment'))

        receipt_no = _receipt_number()
        try:
            db.execute('''
                INSERT INTO member_receipts
                    (receipt_number, member_id, amount, allocated_savings, allocated_loans,
                     bank_account, payment_method, bank_reference, notes, date, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (receipt_no, member_id, receipt_amount, savings_amount, total_loans,
                  bank_account, payment_method, bank_reference, notes, receipt_date, current_user.id))
            receipt_id = last_insert_id(db)

            lines = [{
                'account': bank_account,
                'debit': receipt_amount,
                'memo': bank_reference or f'Member receipt {receipt_no}',
            }]
            allocation_summary = []

            if savings_amount > 0:
                deposit, share = share_capital_split(db, savings_amount)
                month = receipt_date.strftime('%Y-%m')
                sav_receipt = f'{receipt_no}-SAV'
                db.execute('''
                    INSERT INTO savings
                        (member_id, amount, share_capital, month, payment_type, late_fee,
                         payment_method, receipt_number, notes, date, created_by)
                    VALUES (?, ?, ?, ?, 'personal', 0, ?, ?, ?, ?, ?)
                ''', (member_id, deposit, share, month, payment_method, sav_receipt,
                      f'Member receipt {receipt_no}. {notes}'.strip(), receipt_date, current_user.id))
                sav_id = last_insert_id(db)
                db.execute(
                    'UPDATE members SET total_savings = COALESCE(total_savings, 0) + ?, '
                    'shares_value = COALESCE(shares_value, 0) + ? WHERE id = ?',
                    (deposit, share, member_id),
                )
                db.execute('''
                    INSERT INTO member_receipt_allocations
                        (receipt_id, target, target_id, amount)
                    VALUES (?, 'savings', ?, ?)
                ''', (receipt_id, sav_id, savings_amount))
                lines.append({'account': MEMBER_DEPOSITS, 'credit': deposit, 'memo': f'Savings for member {member_id}'})
                if share:
                    lines.append({'account': SHARE_CAPITAL, 'credit': share, 'memo': 'Share capital split'})
                    allocation_summary.append(f'₦{deposit:,.2f} savings, ₦{share:,.2f} share capital')
                else:
                    allocation_summary.append(f'₦{deposit:,.2f} savings')

            for item in loan_allocations:
                loan = item['loan']
                repayment_no = f'{receipt_no}-L{loan["id"]}'
                repayment_notes = f'Member receipt {receipt_no}. {notes}'.strip()
                new_balance = round(_money(loan['balance']) - item['amount'], 2)
                if new_balance <= 0:
                    repayment_notes = f'Pre-liquidation - loan settled in full. {repayment_notes}'.strip()

                db.execute('''
                    INSERT INTO repayments
                        (repayment_number, loan_id, amount, principal_paid, interest_paid,
                         payment_method, receipt_number, reference, notes, date, received_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (repayment_no, loan['id'], item['amount'], item['principal'], item['interest'],
                      payment_method, bank_reference, repayment_no, repayment_notes, receipt_date, current_user.id))
                rep_id = last_insert_id(db)

                if new_balance <= 0:
                    db.execute(
                        "UPDATE loans SET balance = 0, status = 'completed', completed_at = ? WHERE id = ?",
                        (receipt_date, loan['id']),
                    )
                else:
                    db.execute('UPDATE loans SET balance = ? WHERE id = ?', (new_balance, loan['id']))

                if new_balance <= 0:
                    queue_completion(db, loan['id'], rep_id)

                db.execute('''
                    INSERT INTO member_receipt_allocations
                        (receipt_id, target, target_id, loan_id, amount, principal_paid, interest_paid)
                    VALUES (?, 'loan', ?, ?, ?, ?, ?)
                ''', (receipt_id, rep_id, loan['id'], item['amount'], item['principal'], item['interest']))
                lines.append({'account': LOANS_RECEIVABLE, 'credit': item['principal'], 'memo': loan['loan_number']})
                if item['interest']:
                    lines.append({'account': LOAN_INTEREST_INCOME, 'credit': item['interest'], 'memo': 'Interest earned'})
                allocation_summary.append(f'₦{item["amount"]:,.2f} to {loan["loan_number"]}')

            journal_id = post_journal(
                db,
                f'Member receipt allocation — {receipt_no}',
                lines,
                date=receipt_date,
                reference=receipt_no,
                source_module='member_receipt',
                source_id=receipt_id,
                created_by=current_user.id,
            )
            db.execute('UPDATE member_receipts SET journal_entry_id = ? WHERE id = ?', (journal_id, receipt_id))

            if member['email']:
                notify_member(
                    db,
                    member['email'],
                    'Payment Received and Allocated',
                    f'₦{receipt_amount:,.2f} received and allocated: {", ".join(allocation_summary)}.',
                    notification_type='info',
                    action_url='/statements',
                )

            audit(db, 'MEMBER_RECEIPT_ALLOCATED', 'member_receipts',
                  f'{receipt_no}: ₦{receipt_amount:,.2f} received in {bank_account}; '
                  f'allocated {", ".join(allocation_summary)}')
            db.commit()
            dispatch_completions(db)
            flash(f'Receipt {receipt_no} posted and allocated successfully.', 'success')
            return redirect(url_for('member_receipts.member_payment'))
        except Exception as e:
            db.rollback()
            flash(f'Error posting member receipt: {e}', 'danger')
            return redirect(url_for('member_receipts.member_payment'))

    members = db.execute('''
        SELECT id, member_number, first_name, last_name, email
        FROM members
        WHERE status = 'active'
        ORDER BY first_name, last_name
    ''').fetchall()
    loans = db.execute('''
        SELECT l.id, l.member_id, l.loan_number, l.amount, l.total_repayment, l.balance,
               m.first_name, m.last_name
        FROM loans l
        JOIN members m ON m.id = l.member_id
        WHERE l.status = 'active' AND l.balance > 0
        ORDER BY m.first_name, m.last_name, l.id
    ''').fetchall()
    recent_receipts = db.execute('''
        SELECT r.*, m.member_number, m.first_name, m.last_name, a.name AS bank_name
        FROM member_receipts r
        JOIN members m ON m.id = r.member_id
        LEFT JOIN accounts a ON a.code = r.bank_account
        ORDER BY r.id DESC
        LIMIT 10
    ''').fetchall()
    return render_template(
        'admin/member-payment.html',
        members=members,
        loans=loans,
        recent_receipts=recent_receipts,
        bank_accounts=get_postable_cash_accounts(db),
        default_cash_account=get_default_cash_account(db),
        today=datetime.now().strftime('%Y-%m-%d'),
    )


@member_receipts.route('/<int:receipt_id>/reverse', methods=['POST'])
@login_required
@role_required('admin', 'treasurer')
def reverse_member_payment(receipt_id):
    db = get_db()
    reason = (request.form.get('reason') or '').strip()
    receipt = db.execute('SELECT * FROM member_receipts WHERE id = ?', (receipt_id,)).fetchone()
    if not receipt:
        flash('Manual receipt not found.', 'danger')
        return redirect(url_for('member_receipts.member_payment'))
    if receipt['reversed_at']:
        flash('This manual receipt has already been reversed.', 'warning')
        return redirect(url_for('member_receipts.member_payment'))
    if not reason:
        flash('Give a reason before reversing this manual receipt.', 'danger')
        return redirect(url_for('member_receipts.member_payment'))
    if not receipt['journal_entry_id']:
        flash('This manual receipt has no linked journal entry to reverse.', 'danger')
        return redirect(url_for('member_receipts.member_payment'))

    try:
        new_id, source_note = reverse_journal_entry(
            db,
            receipt['journal_entry_id'],
            created_by=current_user.id,
            reason=reason,
        )
        audit(db, 'REVERSE_MEMBER_RECEIPT', 'member_receipts',
              f'Reversed manual receipt {receipt["receipt_number"]} with journal {new_id}. '
              f'Reason: {reason}' + (f' ({source_note})' if source_note else ''))
        db.commit()
        flash('Manual receipt reversed. Bank, savings and loan balances were offset together.', 'success')
    except PeriodLockedError as e:
        db.rollback()
        flash(str(e), 'warning')
    except Exception as e:
        db.rollback()
        flash(f'Could not reverse manual receipt: {e}', 'danger')

    return redirect(url_for('member_receipts.member_payment'))
