"""Controlled conversion of a delivered asset order into an active member loan."""

import random
from datetime import datetime

from asset_documents import update_asset_documents
from asset_schemes import delivery_cleared
from database import last_insert_id
from ledger import LOANS_RECEIVABLE, post_journal_required


def activate_delivered_asset(db, order, actor_id, actor_name, channel, confirmation_note=''):
    if not order or order['status'] != 'ready_for_receipt':
        raise ValueError('This order is not ready for receipt confirmation.')
    if not delivery_cleared(order):
        raise ValueError('The delivery fee has not been cleared.')
    if order['linked_loan_id']:
        raise ValueError('Receipt was already confirmed.')
    loan_number = f"LOAN/AS/{datetime.now():%Y%m%d}/{random.randint(1000,9999)}"
    db.execute('''INSERT INTO loans
        (loan_number,member_id,amount,purpose,description,tenure,interest_rate,interest_method,
         total_repayment,balance,status,approval_stage,disbursed_amount,disbursement_date,
         approved_at,terms_accepted,signature_name,date_applied,submission_channel)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (loan_number, order['member_id'], order['selling_subtotal'], 'Asset Purchase',
         f"Asset scheme order {order['order_number']}", order['tenure_months'],
         order['annual_interest_rate'], 'flat', order['total_repayment'], order['total_repayment'],
         'active', 'approved', order['selling_subtotal'], datetime.now(), order['approved_at'], 1,
         order['signature_name'], order['submitted_at'], channel))
    loan_id = last_insert_id(db)
    post_journal_required(db, f"Asset delivered - {order['order_number']}", [
        {'account': LOANS_RECEIVABLE, 'debit': order['selling_subtotal'], 'memo': loan_number},
        {'account': '4050', 'credit': order['selling_subtotal'], 'memo': order['order_number']},
    ], reference=order['order_number'], source_module='asset_scheme', source_id=order['id'],
       created_by=actor_id)
    changed = db.execute('''UPDATE asset_orders SET status='received',received_at=?,linked_loan_id=?
        WHERE id=? AND linked_loan_id IS NULL''', (datetime.now(), loan_id, order['id'])).rowcount
    if changed != 1:
        raise ValueError('Receipt was already confirmed.')
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
