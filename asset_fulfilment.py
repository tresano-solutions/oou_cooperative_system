"""Controlled conversion of a delivered asset order into an active member loan."""

import json
import uuid
from datetime import datetime

from asset_documents import update_asset_documents
import asset_accounting
import repayment_start
from asset_schemes import delivery_cleared, schedule_snapshot
from database import last_insert_id
from ledger import post_journal_required


def activate_delivered_asset(db, order, actor_id, actor_name, channel, confirmation_note=''):
    if not order or order['status'] != 'ready_for_receipt':
        raise ValueError('This order is not ready for receipt confirmation.')
    if not delivery_cleared(order):
        raise ValueError('The delivery fee has not been cleared.')
    if order['linked_loan_id']:
        raise ValueError('Receipt was already confirmed.')
    now = datetime.now()
    # Claim the order first. Two clicks, or two officers, can reach this point together; only the
    # one whose update changes the row may go on to create the loan and post the receivable.
    claimed = db.execute('''UPDATE asset_orders SET status='received',received_at=?
        WHERE id=? AND status='ready_for_receipt' AND linked_loan_id IS NULL''',
                         (now, order['id'])).rowcount
    if claimed != 1:
        raise ValueError('Receipt was already confirmed.')

    # The schedule the member accepted when they took the quote. Orders accepted before schedules
    # were saved get theirs built now from the same agreed terms.
    snapshot = order['repayment_schedule_snapshot'] if 'repayment_schedule_snapshot' in order.keys() else None
    if not snapshot:
        snapshot = json.dumps(schedule_snapshot(order['selling_subtotal'], order['annual_interest_rate'],
                                                order['tenure_months']))
    db.execute('''INSERT INTO loans
        (loan_number,member_id,amount,purpose,description,tenure,interest_rate,interest_method,
         total_repayment,balance,status,approval_stage,disbursed_amount,disbursement_date,
         first_payment_date,repayment_schedule_snapshot,repayment_schedule_accepted,
         approved_at,terms_accepted,signature_name,date_applied,submission_channel)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (f'LOAN/AS/TMP/{uuid.uuid4().hex}', order['member_id'], order['selling_subtotal'], 'Asset Purchase',
         f"Asset scheme order {order['order_number']}", order['tenure_months'],
         order['annual_interest_rate'], 'flat', order['total_repayment'], order['total_repayment'],
         'active', 'approved', order['selling_subtotal'], now,
         repayment_start.first_repayment_date(now, repayment_start.cutoff_day(db)), snapshot, 1,
         order['approved_at'], 1, order['signature_name'], order['submitted_at'], channel))
    loan_id = last_insert_id(db)
    # Numbered from the loan's own id, so two loans on one day can never collide.
    loan_number = f"LOAN/AS/{now:%Y%m%d}/{loan_id:05d}"
    db.execute('UPDATE loans SET loan_number=? WHERE id=?', (loan_number, loan_id))
    lines = asset_accounting.delivery_lines(db, order, loan_number)
    post_journal_required(db, f"Asset delivered - {order['order_number']}", lines,
                          reference=order['order_number'], source_module='asset_scheme', source_id=order['id'],
                          created_by=actor_id)
    sold = ", inventory_status='sold'" if asset_accounting.treatment(order) == asset_accounting.INVENTORY else ''
    db.execute(f'UPDATE asset_orders SET linked_loan_id=?{sold} WHERE id=?', (loan_id, order['id']))
    update_asset_documents(db, order['id'], invoice_status='delivered_loan_active',
                           delivery_status='delivered', confirmed=True, confirmed_by=actor_id)
    comment = f'Loan {loan_number} activated'
    if confirmation_note:
        comment += f'. {confirmation_note}'
    db.execute('''INSERT INTO asset_order_events
        (order_id,stage,action,actor_id,actor_name,comment)
        VALUES (?,'receipt','confirmed',?,?,?)''',
        (order['id'], actor_id, actor_name, comment))
    return loan_id, loan_number
