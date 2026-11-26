"""Accounting for the asset scheme: goods bought for a member are inventory until they are delivered.

    Supplier paid ............ Dr Inventory                      Cr Bank              (cost)
    Member confirms delivery . Dr Loans Receivable (price)        Cr Asset Scheme Sales (price)
                               Dr Cost of Asset Sales (cost)      Cr Inventory          (cost)
    Cancelled, returned ...... Dr Bank (refund)                   Cr Inventory          (cost)
                               Dr Operating Expenses (any unrecovered cost)
    Cancelled, kept .......... nothing: the cost stays in Inventory as stock held for resale

Orders bought before this treatment began were expensed when paid. They keep that treatment (so nothing
is counted twice at delivery) unless the one-off reclassification moves them to Inventory.
"""
from ledger import LOANS_RECEIVABLE, OPERATING_EXPENSES, post_journal_required

# role -> (setting that holds the account code, default code, account type it must have)
ACCOUNT_SETTINGS = {
    'inventory': ('asset_inventory_account', '1300', 'asset'),
    'cogs': ('asset_cogs_account', '5200', 'expense'),
    'sales': ('asset_sales_account', '4050', 'income'),
}
INVENTORY, EXPENSED = 'inventory', 'expensed'


def accounts(db, *roles):
    """The account codes for the requested roles (all three if none are named), each checked to exist
    and to be the right kind of account, so a mapping mistake stops the posting with a clear message."""
    codes = {}
    for role in (roles or tuple(ACCOUNT_SETTINGS)):
        setting, default, kind = ACCOUNT_SETTINGS[role]
        row = db.execute('SELECT value FROM settings WHERE key=?', (setting,)).fetchone()
        code = ((row['value'] if row and row['value'] else default) or default).strip()
        account = db.execute('SELECT type FROM accounts WHERE code=?', (code,)).fetchone()
        if not account or account['type'] != kind:
            raise ValueError(f'The asset scheme {role} account ({code}) is missing or is not a {kind} account. '
                             'Correct it under Settings, Asset scheme accounts.')
        codes[role] = code
    return codes


def treatment(order):
    """'inventory', 'expensed' (bought before inventory accounting), or None if not yet bought."""
    keys = order.keys()
    if 'procurement_treatment' in keys and order['procurement_treatment']:
        return order['procurement_treatment']
    return EXPENSED if order['procurement_journal_id'] else None


def post_purchase(db, order_id, order_number, cost, bank_account, reference, user_id):
    return post_journal_required(db, f'Asset procurement - {order_number}', [
        {'account': accounts(db, 'inventory')['inventory'], 'debit': cost, 'memo': order_number},
        {'account': bank_account, 'credit': cost, 'memo': reference},
    ], reference=reference, source_module='asset_procurement', source_id=order_id, created_by=user_id)


def delivery_lines(db, order, loan_number):
    """The journal lines when the member takes delivery: the sale, and (for stock) its cost."""
    price = order['selling_subtotal']
    lines = [
        {'account': LOANS_RECEIVABLE, 'debit': price, 'memo': loan_number},
        {'account': accounts(db, 'sales')['sales'], 'credit': price, 'memo': order['order_number']},
    ]
    if treatment(order) == INVENTORY:
        cost = round(float(order['procurement_cost'] or 0), 2)
        held = accounts(db, 'inventory', 'cogs')
        if cost > 0:
            lines += [{'account': held['cogs'], 'debit': cost, 'memo': order['order_number']},
                      {'account': held['inventory'], 'credit': cost, 'memo': order['order_number']}]
    return lines


def refund_lines(db, order, refund, refund_account, reference):
    """Lines for a supplier refund on a cancelled order. Stock leaves Inventory at cost, any part of
    the cost the supplier did not refund is written off; an order that was expensed is simply credited."""
    cost = round(float(order['procurement_cost'] or 0), 2)
    if treatment(order) != INVENTORY:
        return [{'account': refund_account, 'debit': refund, 'memo': reference},
                {'account': OPERATING_EXPENSES, 'credit': refund, 'memo': order['order_number']}]
    lines = [{'account': refund_account, 'debit': refund, 'memo': reference},
             {'account': accounts(db, 'inventory')['inventory'], 'credit': cost, 'memo': order['order_number']}]
    if round(cost - refund, 2) > 0:
        lines.insert(1, {'account': OPERATING_EXPENSES, 'debit': round(cost - refund, 2),
                         'memo': f'Unrecovered cost of cancelled purchase {order["order_number"]}'})
    return lines


def open_expensed_purchases(db):
    """Orders already bought and expensed whose goods have not yet been delivered: the ones the one-off
    reclassification can move to Inventory."""
    return db.execute("""SELECT o.*, m.first_name, m.last_name FROM asset_orders o JOIN members m ON m.id=o.member_id
        WHERE o.procurement_journal_id IS NOT NULL AND COALESCE(o.procurement_treatment,'expensed')='expensed'
        AND o.status IN ('procured','ready_for_receipt') ORDER BY o.id""").fetchall()


def reclassify(db, order, date, user_id):
    """Dr Inventory / Cr Operating Expenses for one order, then mark it as inventory."""
    cost = round(float(order['procurement_cost'] or 0), 2)
    if cost <= 0:
        raise ValueError(f'{order["order_number"]} has no recorded purchase cost to reclassify.')
    journal_id = post_journal_required(db, f'Reclassify asset purchase to inventory - {order["order_number"]}', [
        {'account': accounts(db, 'inventory')['inventory'], 'debit': cost, 'memo': order['order_number']},
        {'account': OPERATING_EXPENSES, 'credit': cost, 'memo': 'Purchase expensed when paid; now held as stock'},
    ], date=date, reference=f'RECLASS-{order["order_number"]}', source_module='asset_inventory_reclass',
       source_id=order['id'], created_by=user_id)
    changed = db.execute("""UPDATE asset_orders SET procurement_treatment='inventory',inventory_status='bought'
        WHERE id=? AND COALESCE(procurement_treatment,'expensed')='expensed'""", (order['id'],)).rowcount
    if changed != 1:
        raise ValueError(f'{order["order_number"]} was already reclassified.')
    return journal_id


def stock_on_hand(db):
    """Orders whose cost is in Inventory right now, and their total, for reconciling to the ledger."""
    rows = db.execute("""SELECT o.*, m.first_name, m.last_name FROM asset_orders o JOIN members m ON m.id=o.member_id
        WHERE o.procurement_treatment='inventory' AND o.inventory_status IN ('bought','held')
        ORDER BY o.inventory_status, o.id""").fetchall()
    total = round(sum(float(r['procurement_cost'] or 0) for r in rows), 2)
    return rows, total


def ledger_balance(db, code):
    row = db.execute("""SELECT COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0) FROM journal_lines l
        JOIN journal_entries e ON e.id=l.entry_id WHERE l.account_code=?""", (code,)).fetchone()
    return round(float(row[0] or 0), 2)
