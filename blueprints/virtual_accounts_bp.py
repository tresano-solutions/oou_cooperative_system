"""
Virtual accounts (admin) — CoopMS

Give each member their own bank account number, then watch the money arrive and
decide what it is for. The heavy lifting lives in ``virtual_accounts.py``; this
blueprint is the screens around it.
"""

from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required

from database import get_db
from ledger import account_balance
from ledger import JournalPostingError
from utils import audit, role_required
from virtual_accounts import (ALLOCATION_RULES, MEMBER_PREFERENCES, VA_UNALLOCATED,
                              account_for_member, apply_plan, build_plan, ctas_due_rows,
                              member_loan_balances, members_without_accounts, pending_total,
                              provision_account, receipts, set_setting, set_va_enabled,
                              va_config, va_enabled)

virtual_accounts_bp = Blueprint('virtual_accounts', __name__, url_prefix='/admin/virtual-accounts')


def _require_enabled():
    if not va_enabled(get_db()):
        flash('Virtual accounts are switched off. Turn them on below first.', 'warning')


@virtual_accounts_bp.route('/')
@login_required
@role_required('admin')
def index():
    db = get_db()
    cfg = va_config(db)

    accounts = db.execute('''
        SELECT v.*, m.member_number, m.first_name, m.last_name, m.payment_preference
        FROM member_virtual_accounts v
        JOIN members m ON m.id = v.member_id
        ORDER BY m.member_number
    ''').fetchall()

    unmatched = receipts(db, status='unmatched', limit=50)
    waiting = [r for r in receipts(db, limit=200)
               if r['status'] in ('unallocated', 'part_allocated')]

    return render_template(
        'admin/virtual-accounts.html',
        config=cfg,
        enabled=va_enabled(db),
        rules=ALLOCATION_RULES,
        preferences=MEMBER_PREFERENCES,
        accounts=accounts,
        without=members_without_accounts(db, cfg['va_provider']),
        recent=receipts(db, limit=50),
        unmatched=unmatched,
        waiting=waiting,
        # The queue and the ledger are two views of the same money; showing both
        # means a mismatch is obvious rather than buried.
        pending_total=pending_total(db),
        # account_balance is debits - credits, so a liability comes back
        # negative. Flip it: officers want to see what is being held.
        holding_balance=-account_balance(db, VA_UNALLOCATED),
    )


@virtual_accounts_bp.route('/settings', methods=['POST'])
@login_required
@role_required('admin')
def save_settings():
    db = get_db()
    try:
        on = request.form.get('va_enabled') == '1'
        rule = request.form.get('va_allocation_rule', '')
        if rule not in ALLOCATION_RULES:
            rule = 'savings'
        set_va_enabled(db, on)
        set_setting(db, 'va_allocation_rule', rule)
        set_setting(db, 'va_preferred_bank',
                    request.form.get('va_preferred_bank', '').strip() or 'wema-bank')
        db.commit()
        audit(db, 'UPDATE_VA_SETTINGS', 'settings',
              f"Virtual accounts {'on' if on else 'off'}, rule {rule}")
        db.commit()
        flash('Virtual account settings saved.', 'success')
    except Exception as exc:
        db.rollback()
        flash(f'Could not save: {exc}', 'danger')
    return redirect(url_for('virtual_accounts.index'))


@virtual_accounts_bp.route('/provision/<int:member_id>', methods=['POST'])
@login_required
@role_required('admin')
def provision(member_id):
    db = get_db()
    _require_enabled()
    ok, message = provision_account(db, member_id, created_by=current_user.id)
    if ok:
        db.commit()
        audit(db, 'PROVISION_VIRTUAL_ACCOUNT', 'members',
              f'Member {member_id} given account {message}')
        db.commit()
        flash(f'Account created: {message}', 'success')
    else:
        db.rollback()
        flash(message, 'danger')
    return redirect(url_for('virtual_accounts.index'))


@virtual_accounts_bp.route('/provision-all', methods=['POST'])
@login_required
@role_required('admin')
def provision_all():
    """Give every active member without one an account number.

    Each member is committed on its own: one rejected member must not throw away
    the accounts already created in this run.
    """
    db = get_db()
    _require_enabled()
    made, failed = 0, []
    for member in members_without_accounts(db, va_config(db)['va_provider']):
        ok, message = provision_account(db, member['id'], created_by=current_user.id)
        if ok:
            db.commit()
            made += 1
        else:
            db.rollback()
            failed.append(f"{member['member_number']}: {message}")

    if made:
        audit(db, 'PROVISION_VIRTUAL_ACCOUNTS', 'members', f'{made} account(s) created')
        db.commit()
        flash(f'Created {made} account number(s).', 'success')
    if failed:
        shown = '; '.join(failed[:5])
        more = f' and {len(failed) - 5} more' if len(failed) > 5 else ''
        flash(f'Could not create {len(failed)}: {shown}{more}', 'warning')
    if not made and not failed:
        flash('Every active member already has an account number.', 'info')
    return redirect(url_for('virtual_accounts.index'))


@virtual_accounts_bp.route('/receipt/<int:receipt_id>/match', methods=['POST'])
@login_required
@role_required('admin')
def match_receipt(receipt_id):
    """Attach an unmatched inflow to a member by hand."""
    db = get_db()
    member_id = request.form.get('member_id', type=int)
    if not member_id:
        flash('Pick a member first.', 'warning')
        return redirect(url_for('virtual_accounts.index'))

    receipt = db.execute('SELECT * FROM virtual_account_receipts WHERE id = ?',
                         (receipt_id,)).fetchone()
    if not receipt:
        abort(404)
    if receipt['status'] != 'unmatched':
        flash('That transfer is already matched to a member.', 'info')
        return redirect(url_for('virtual_accounts.index'))

    va = account_for_member(db, member_id, receipt['provider'])
    db.execute("UPDATE virtual_account_receipts SET member_id = ?, virtual_account_id = ?, "
               "status = 'unallocated' WHERE id = ?",
               (member_id, va['id'] if va else None, receipt_id))
    audit(db, 'MATCH_VA_RECEIPT', 'virtual_account_receipts',
          f'Receipt {receipt_id} matched to member {member_id}')
    db.commit()
    flash('Transfer matched. It is now waiting to be applied.', 'success')
    return redirect(url_for('virtual_accounts.index'))


@virtual_accounts_bp.route('/receipt/<int:receipt_id>/apply', methods=['POST'])
@login_required
@role_required('admin')
def apply_receipt(receipt_id):
    """Apply an inflow — either by the cooperative's rule, or split by hand."""
    db = get_db()
    receipt = db.execute('SELECT * FROM virtual_account_receipts WHERE id = ?',
                         (receipt_id,)).fetchone()
    if not receipt:
        abort(404)
    if not receipt['member_id']:
        flash('Match this transfer to a member first.', 'warning')
        return redirect(url_for('virtual_accounts.index'))

    outstanding = round(float(receipt['amount'] or 0) -
                        float(receipt['allocated_amount'] or 0), 2)

    if request.form.get('mode') == 'rule':
        plan = build_plan(db, receipt['member_id'], outstanding, rule=None)
        if not plan:
            flash('The current rule leaves this for an officer to decide. '
                  'Choose where it goes below.', 'info')
            return redirect(url_for('virtual_accounts.index'))
    else:
        savings = request.form.get('savings_amount', type=float) or 0.0
        loan_id = request.form.get('loan_id', type=int)
        loan_amount = request.form.get('loan_amount', type=float) or 0.0
        ctas_amount = request.form.get('ctas_amount', type=float) or 0.0
        plan = []
        if ctas_amount > 0:
            # Oldest unpaid period first, same order the member would pay in.
            due = ctas_due_rows(db, receipt['member_id'])
            remaining = ctas_amount
            for row in due:
                if remaining <= 0:
                    break
                owed = round(float(row['expected_amount'] or 0) -
                             float(row['paid_amount'] or 0), 2)
                part = round(min(remaining, owed), 2)
                if part > 0:
                    plan.append({'target': 'ctas', 'subscription_id': row['subscription_id'],
                                 'period': row['period_number'], 'amount': part})
                    remaining = round(remaining - part, 2)
            if remaining > 0.005:
                flash(f'₦{remaining:,.2f} of the Target Advance amount is more than is owed — '
                      'it was not applied.', 'warning')
        if loan_id and loan_amount > 0:
            plan.append({'target': 'loan', 'loan_id': loan_id, 'amount': loan_amount})
        if savings > 0:
            plan.append({'target': 'savings', 'loan_id': None, 'amount': savings})
        if not plan:
            flash('Enter an amount to apply.', 'warning')
            return redirect(url_for('virtual_accounts.index'))

    try:
        ok, message = apply_plan(db, receipt_id, plan, created_by=current_user.id)
    except JournalPostingError as exc:
        # The money is already banked to the unallocated account; only the
        # decision about where it goes failed, and that can be made again.
        db.rollback()
        flash(str(exc), 'danger')
        return redirect(url_for('virtual_accounts.index'))
    if ok:
        audit(db, 'APPLY_VA_RECEIPT', 'virtual_account_receipts',
              f'Receipt {receipt_id}: {message}')
        db.commit()
        flash(f'Applied — {message}', 'success')
    else:
        db.rollback()
        flash(message, 'danger')
    return redirect(url_for('virtual_accounts.index'))


@virtual_accounts_bp.route('/receipt/<int:receipt_id>')
@login_required
@role_required('admin')
def receipt_detail(receipt_id):
    db = get_db()
    receipt = db.execute('''
        SELECT r.*, m.member_number, m.first_name, m.last_name
        FROM virtual_account_receipts r
        LEFT JOIN members m ON m.id = r.member_id
        WHERE r.id = ?''', (receipt_id,)).fetchone()
    if not receipt:
        abort(404)
    allocations = db.execute(
        'SELECT * FROM virtual_account_allocations WHERE receipt_id = ? ORDER BY id',
        (receipt_id,)).fetchall()
    loans = member_loan_balances(db, receipt['member_id']) if receipt['member_id'] else []
    ctas_due = ctas_due_rows(db, receipt['member_id']) if receipt['member_id'] else []
    return render_template('admin/virtual-account-receipt.html',
                           receipt=receipt, allocations=allocations, loans=loans,
                           ctas_due=ctas_due, preferences=MEMBER_PREFERENCES,
                           members=db.execute(
                               "SELECT id, member_number, first_name, last_name FROM members "
                               "WHERE status = 'active' ORDER BY member_number").fetchall())
