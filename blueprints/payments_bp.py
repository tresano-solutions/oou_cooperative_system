"""
Online Payments Blueprint — CoopMS
Routes:
  POST  /admin/pay/savings              — initiate savings payment
  POST  /admin/pay/loan/<loan_id>       — initiate loan repayment
  GET   /admin/pay/callback/<gateway>   — gateway redirect after payment
  POST  /webhooks/paystack              — Paystack webhook (server-side confirmation)
  POST  /webhooks/flutterwave           — Flutterwave webhook
"""

import json
from datetime import datetime

from flask import (Blueprint, abort, current_app, flash, jsonify,
                   redirect, render_template, request, url_for)
from flask_login import current_user, login_required

from database import USE_POSTGRES, get_db, for_update
from email_service import send_loan_repayment_email
from payments import get_gateway, generate_reference
from security import log_audit
from utils import finite_float, audit, member_for_user, split_repayment
from ledger import (post_journal_safe, get_default_cash_account, MEMBER_DEPOSITS, LOANS_RECEIVABLE,
                    LOAN_INTEREST_INCOME)

payments_bp = Blueprint('payments', __name__)

# URL prefix strategy:
#   member-facing payment initiation & callback → /admin/pay/...
#   gateway webhooks (external URLs)            → /webhooks/...  (no prefix)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _select_pending_payment_for_processing(db, reference: str):
    """Fetch and lock one pending payment row for processing."""
    sql = 'SELECT * FROM pending_payments WHERE reference = ?'
    if USE_POSTGRES:
        sql += ' FOR UPDATE'
    return db.execute(sql, (reference,)).fetchone()


def _record_payment(db, reference: str) -> bool:
    """
    Idempotent: look up the pending_payments row for *reference*, verify with
    the gateway, and if successful commit the actual savings / repayment record.

    Returns True if payment was newly committed, False if already done or failed.
    """
    row = _select_pending_payment_for_processing(db, reference)
    if row is None:
        db.rollback()
        return False
    if row['status'] == 'completed':
        db.rollback()
        return False  # already processed (webhook beat the callback, or duplicate call)

    gateway_name = row['gateway']
    gw           = get_gateway(gateway_name)

    # ── Verify with gateway ────────────────────────────────────────────────────
    try:
        if gateway_name == 'paystack':
            resp   = gw.verify(reference)
            ok     = (resp.get('status') is True and
                      resp.get('data', {}).get('status') == 'success')
            gw_ref = resp.get('data', {}).get('id', '')
        else:  # flutterwave — reference is stored in gateway_ref after callback
            gw_ref = row['gateway_ref'] or reference
            resp   = gw.verify(str(gw_ref))
            ok     = (resp.get('status') == 'success' and
                      resp.get('data', {}).get('status') == 'successful')
            gw_ref = resp.get('data', {}).get('id', gw_ref)
    except Exception as exc:
        current_app.logger.error('Payment verify error ref=%s: %s', reference, exc)
        db.rollback()
        return False

    if not ok:
        db.execute(
            "UPDATE pending_payments SET status = 'failed', gateway_ref = ? WHERE reference = ?",
            (str(gw_ref), reference)
        )
        db.commit()
        return False

    # ── Mark pending row completed ─────────────────────────────────────────────
    db.execute(
        "UPDATE pending_payments SET status = 'completed', gateway_ref = ?, "
        "completed_at = ? WHERE reference = ?",
        (str(gw_ref), datetime.now(), reference)
    )

    # ── Persist the actual financial record ────────────────────────────────────
    ptype     = row['payment_type']
    member_id = row['member_id']
    amount    = row['amount']

    if ptype == 'savings':
        month = row['month']
        # Idempotency is per PAYMENT (reference), not per month — a member may
        # save more than once in a month (e.g. salary deduction + voluntary).
        exists = db.execute(
            'SELECT id FROM savings WHERE reference = ?', (reference,)
        ).fetchone()
        if not exists:
            db.execute(
                '''INSERT INTO savings
                   (member_id, amount, month, payment_type, payment_method, reference, date)
                   VALUES (?, ?, ?, 'monthly', 'online', ?, ?)''',
                (member_id, amount, month, reference, datetime.now())
            )
            db.execute(
                'UPDATE members SET total_savings = total_savings + ? WHERE id = ?',
                (amount, member_id)
            )
            cash_account = get_default_cash_account(db)
            post_journal_safe(db, f'Online savings deposit — {month}', [
                {'account': cash_account, 'debit': amount, 'memo': 'Online payment'},
                {'account': MEMBER_DEPOSITS, 'credit': amount, 'memo': f'Member {member_id}'},
            ], reference=reference, source_module='payments', source_id=member_id)

    elif ptype == 'loan_repayment':
        loan_id = row['related_id']
        loan    = db.execute(
            for_update('SELECT * FROM loans WHERE id = ? AND member_id = ?'),
            (loan_id, member_id)
        ).fetchone()
        if loan:
            existing_repayment = db.execute(
                'SELECT id FROM repayments WHERE reference = ?', (reference,)
            ).fetchone()
            if existing_repayment:
                db.commit()
                return False

            principal_paid, interest_paid = split_repayment(
                amount, loan['amount'], loan['total_repayment'])
            new_balance = max(loan['balance'] - amount, 0)

            rep_num = f"REP-{reference[-8:].upper()}"
            db.execute(
                '''INSERT INTO repayments
                   (repayment_number, loan_id, amount, principal_paid, interest_paid,
                    payment_method, reference, date)
                   VALUES (?, ?, ?, ?, ?, 'online', ?, ?)''',
                (rep_num, loan_id, amount, principal_paid, interest_paid,
                 reference, datetime.now())
            )
            if new_balance <= 0:
                db.execute(
                    "UPDATE loans SET balance = 0, status = 'completed', "
                    "completed_at = ? WHERE id = ?",
                    (datetime.now(), loan_id)
                )
            else:
                db.execute(
                    'UPDATE loans SET balance = ? WHERE id = ?',
                    (new_balance, loan_id)
                )
            cash_account = get_default_cash_account(db)
            post_journal_safe(db, f"Online loan repayment — {loan['loan_number']}", [
                {'account': cash_account, 'debit': amount, 'memo': 'Online payment'},
                {'account': LOANS_RECEIVABLE, 'credit': principal_paid, 'memo': loan['loan_number']},
                {'account': LOAN_INTEREST_INCOME, 'credit': interest_paid, 'memo': 'Interest earned'},
            ], reference=reference, source_module='payments', source_id=loan_id)
            member = db.execute('SELECT * FROM members WHERE id = ?', (member_id,)).fetchone()
            if member and member['email']:
                send_loan_repayment_email(
                    member['email'],
                    member,
                    loan,
                    {
                        'repayment_number': rep_num,
                        'reference': reference,
                        'amount': amount,
                        'principal_paid': principal_paid,
                        'interest_paid': interest_paid,
                        'balance': new_balance,
                        'date': datetime.now().strftime('%Y-%m-%d'),
                    },
                    url_for('portal.my_loans', _external=True),
                )

    db.commit()
    return True


# ─── Initiate savings payment ─────────────────────────────────────────────────

@payments_bp.route('/admin/pay/savings', methods=['POST'])
@login_required
def initiate_savings():
    db     = get_db()
    member = member_for_user(db, current_user.id)
    if not member:
        flash('Member record not found.', 'danger')
        return redirect(url_for('portal.my_savings'))

    try:
        amount = finite_float(request.form['amount'])
        month  = request.form['month']         # expected YYYY-MM
        if amount <= 0:
            raise ValueError('Amount must be positive')
    except (KeyError, ValueError) as exc:
        flash(f'Invalid payment details: {exc}', 'danger')
        return redirect(url_for('portal.my_savings'))

    # A member may save more than once in a month (salary deduction + voluntary),
    # so we do not block on month here. Each payment is a distinct transaction and
    # is de-duplicated by its gateway reference when confirmed.

    gateway_name = db.execute(
        "SELECT value FROM settings WHERE key = 'active_gateway'"
    ).fetchone()
    gateway_name = gateway_name['value'] if gateway_name else 'paystack'

    reference    = generate_reference('SAV')
    callback_url = url_for('payments.payment_callback',
                           gateway=gateway_name, _external=True)

    # Persist pending row BEFORE redirecting to gateway
    db.execute(
        '''INSERT INTO pending_payments
           (reference, member_id, payment_type, amount, month, gateway)
           VALUES (?, ?, 'savings', ?, ?, ?)''',
        (reference, member['id'], amount, month, gateway_name)
    )
    db.commit()

    email = member['email'] or current_user.email or ''
    name  = f"{member['first_name']} {member['last_name']}"
    desc  = f"Savings — {month}"

    try:
        gw = get_gateway(gateway_name)
        if gateway_name == 'paystack':
            resp = gw.initialize(email, amount, reference, callback_url,
                                 metadata={'member_id': member['id'], 'month': month})
            if resp.get('status'):
                return redirect(resp['data']['authorization_url'])
        else:  # flutterwave
            resp = gw.initialize(email, amount, reference, callback_url,
                                 name=name, description=desc)
            if resp.get('status') == 'success':
                return redirect(resp['data']['link'])
    except Exception as exc:
        current_app.logger.error('Payment init error: %s', exc)

    flash('Could not connect to payment gateway. Please try again or pay at the office.', 'danger')
    db.execute("UPDATE pending_payments SET status = 'failed' WHERE reference = ?", (reference,))
    db.commit()
    return redirect(url_for('portal.my_savings'))


# ─── Initiate loan repayment ──────────────────────────────────────────────────

@payments_bp.route('/admin/pay/loan/<int:loan_id>', methods=['POST'])
@login_required
def initiate_loan_repayment(loan_id):
    db     = get_db()
    member = member_for_user(db, current_user.id)
    if not member:
        flash('Member record not found.', 'danger')
        return redirect(url_for('portal.my_loans'))

    loan = db.execute(
        "SELECT * FROM loans WHERE id = ? AND member_id = ? AND status = 'active'",
        (loan_id, member['id'])
    ).fetchone()
    if not loan:
        flash('Loan not found or not active.', 'danger')
        return redirect(url_for('portal.my_loans'))

    try:
        amount = finite_float(request.form['amount'])
        if amount <= 0:
            raise ValueError('Amount must be positive')
        if amount > loan['balance']:
            amount = loan['balance']   # cap at outstanding balance
    except (KeyError, ValueError) as exc:
        flash(f'Invalid amount: {exc}', 'danger')
        return redirect(url_for('portal.loan_detail', loan_id=loan_id))

    gateway_name = db.execute(
        "SELECT value FROM settings WHERE key = 'active_gateway'"
    ).fetchone()
    gateway_name = gateway_name['value'] if gateway_name else 'paystack'

    reference    = generate_reference('LOAN')
    callback_url = url_for('payments.payment_callback',
                           gateway=gateway_name, _external=True)

    db.execute(
        '''INSERT INTO pending_payments
           (reference, member_id, payment_type, related_id, amount, gateway)
           VALUES (?, ?, 'loan_repayment', ?, ?, ?)''',
        (reference, member['id'], loan_id, amount, gateway_name)
    )
    db.commit()

    email = member['email'] or current_user.email or ''
    name  = f"{member['first_name']} {member['last_name']}"
    desc  = f"Loan repayment — {loan['loan_number']}"

    try:
        gw = get_gateway(gateway_name)
        if gateway_name == 'paystack':
            resp = gw.initialize(email, amount, reference, callback_url,
                                 metadata={'member_id': member['id'], 'loan_id': loan_id})
            if resp.get('status'):
                return redirect(resp['data']['authorization_url'])
        else:
            resp = gw.initialize(email, amount, reference, callback_url,
                                 name=name, description=desc)
            if resp.get('status') == 'success':
                return redirect(resp['data']['link'])
    except Exception as exc:
        current_app.logger.error('Loan payment init error: %s', exc)

    flash('Could not connect to payment gateway. Please try again or pay at the office.', 'danger')
    db.execute("UPDATE pending_payments SET status = 'failed' WHERE reference = ?", (reference,))
    db.commit()
    return redirect(url_for('portal.loan_detail', loan_id=loan_id))


# ─── Payment callback (gateway redirect) ──────────────────────────────────────

@payments_bp.route('/admin/pay/callback/<gateway>', methods=['GET'])
@login_required
def payment_callback(gateway):
    """
    Gateway redirects the user here after payment attempt.
    Paystack:     ?reference=...&trxref=...
    Flutterwave:  ?tx_ref=...&transaction_id=...&status=...
    """
    if gateway not in {'paystack', 'flutterwave'}:
        abort(404)

    db = get_db()

    if gateway == 'paystack':
        reference = request.args.get('reference') or request.args.get('trxref', '')
    else:  # flutterwave
        reference      = request.args.get('tx_ref', '')
        transaction_id = request.args.get('transaction_id', '')

    if not reference:
        flash('Payment reference missing. Contact support if your account was debited.', 'danger')
        return redirect(url_for('portal.my_savings'))

    row = db.execute(
        'SELECT * FROM pending_payments WHERE reference = ?', (reference,)
    ).fetchone()
    if not row:
        flash('Unknown payment reference.', 'danger')
        return redirect(url_for('portal.my_savings'))

    member = member_for_user(db, current_user.id)
    if not member or row['member_id'] != member['id']:
        abort(403)

    if gateway == 'flutterwave' and transaction_id:
        db.execute(
            'UPDATE pending_payments SET gateway_ref = ? WHERE reference = ? AND member_id = ?',
            (transaction_id, reference, member['id'])
        )
        db.commit()

    success = _record_payment(db, reference)

    if success:
        flash('Payment successful! Your records have been updated.', 'success')
    else:
        status = db.execute(
            'SELECT status FROM pending_payments WHERE reference = ?', (reference,)
        ).fetchone()
        if status and status['status'] == 'completed':
            flash('This payment was already recorded.', 'info')
        else:
            flash('Payment could not be verified. Contact support if you were charged.', 'warning')

    # Redirect to appropriate page
    row = db.execute(
        'SELECT * FROM pending_payments WHERE reference = ?', (reference,)
    ).fetchone()
    if row and row['payment_type'] == 'loan_repayment' and row['related_id']:
        return redirect(url_for('portal.loan_detail', loan_id=row['related_id']))
    return redirect(url_for('portal.my_savings'))


# ─── Paystack Webhook ─────────────────────────────────────────────────────────

@payments_bp.route('/webhooks/paystack', methods=['POST'])
def paystack_webhook():
    payload   = request.get_data()
    signature = request.headers.get('X-Paystack-Signature', '')

    gw = get_gateway('paystack')
    if not gw.validate_webhook(payload, signature):
        abort(400)

    try:
        event = json.loads(payload)
    except ValueError:
        abort(400)

    if event.get('event') == 'charge.success':
        data = event.get('data', {}) or {}
        # A transfer into a member's own account number arrives on the same
        # event, but there is no pending row waiting for it — it is money nobody
        # announced, so it takes the virtual-account path instead.
        if data.get('channel') == 'dedicated_nuban':
            _record_virtual_account_inflow(get_db(), data)
        elif data.get('reference'):
            _record_payment(get_db(), data['reference'])

    return jsonify({'status': 'ok'}), 200


def _record_virtual_account_inflow(db, data):
    """Bank a transfer that landed in a member's dedicated account, then apply it
    using the cooperative's rule.

    Never raises: a webhook that 500s gets retried, and a retry that fails the
    same way just repeats. Anything that goes wrong is logged and the money is
    left for an officer to look at.
    """
    from virtual_accounts import auto_allocate, record_receipt, va_enabled

    try:
        if not va_enabled(db):
            return
        reference = data.get('reference', '')
        if not reference:
            return

        metadata = data.get('metadata') or {}
        auth = data.get('authorization') or {}
        customer = data.get('customer') or {}
        account_number = (metadata.get('receiver_account_number')
                          or auth.get('receiver_bank_account_number') or '')

        receipt_id, is_new = record_receipt(
            db,
            provider_reference=reference,
            amount=float(data.get('amount', 0)) / 100.0,      # Paystack sends kobo
            account_number=account_number,
            customer_code=customer.get('customer_code', ''),
            sender_name=auth.get('account_name') or auth.get('sender_name') or '',
            sender_bank=auth.get('sender_bank') or auth.get('bank') or '',
            narration=data.get('narration') or metadata.get('narration') or '',
            provider='paystack',
        )
        if not is_new:
            db.rollback()      # already banked; a redelivery must change nothing
            return

        auto_allocate(db, receipt_id)
        db.commit()
        _notify_member_of_receipt(db, receipt_id)
    except Exception as exc:
        db.rollback()
        current_app.logger.error('Virtual account inflow failed: %s', exc)


def _notify_member_of_receipt(db, receipt_id):
    """Tell the member their money landed. Best effort — the money is already
    banked, so a failed alert must not undo anything."""
    try:
        from utils import notify
        row = db.execute('''SELECT r.*, u.id AS user_id
                            FROM virtual_account_receipts r
                            JOIN members m ON m.id = r.member_id
                            JOIN users u ON lower(u.email) = lower(m.email)
                            WHERE r.id = ?''', (receipt_id,)).fetchone()
        if not row:
            return
        amount = float(row['amount'] or 0)
        if row['status'] == 'allocated':
            message = f'We received your transfer of ₦{amount:,.2f} and applied it to your account.'
        else:
            message = (f'We received your transfer of ₦{amount:,.2f}. '
                       'It will be applied to your account shortly.')
        notify(db, row['user_id'], 'Payment received', message,
               notification_type='success')
        db.commit()
    except Exception:
        db.rollback()


# ─── Flutterwave Webhook ──────────────────────────────────────────────────────

@payments_bp.route('/webhooks/flutterwave', methods=['POST'])
def flutterwave_webhook():
    signature = request.headers.get('verif-hash', '')

    gw = get_gateway('flutterwave')
    if not gw.validate_webhook(signature):
        abort(400)

    try:
        event = request.get_json(force=True) or {}
    except Exception:
        abort(400)

    if event.get('event') == 'charge.completed':
        data      = event.get('data', {})
        reference = data.get('tx_ref', '')
        gw_id     = str(data.get('id', ''))
        if reference:
            db = get_db()
            if gw_id:
                db.execute(
                    'UPDATE pending_payments SET gateway_ref = ? WHERE reference = ? AND gateway_ref IS NULL',
                    (gw_id, reference)
                )
                db.commit()
            _record_payment(db, reference)

    return jsonify({'status': 'ok'}), 200
