"""
Dedicated virtual accounts — a permanent bank account number per member.

Why this exists: the old way is a member transfers to the cooperative's account,
forgets the reference, sends a screenshot, and a treasurer posts it by hand. Here
each member gets their own NUBAN, so the account the money landed in *is* the
identity. Nothing is matched by hand and nothing depends on the member
remembering to quote anything.

Money arrives unannounced, which makes this different from every other payment
path in CoopMS. There is no pending row waiting for it and no member click that
says what it is for. So an inflow is handled in two steps:

  1. **Banked.**  Debit Cash & Bank, credit Unallocated Member Receipts (2010).
     The cooperative owes the member that money from the second it lands, and
     the books say so immediately — even though nobody has decided what it is
     for yet.
  2. **Applied.**  Debit 2010, credit savings or the member's loan. This is a
     separate decision, taken by the cooperative's allocation rule or by an
     officer, and it is reversible on its own.

Never guess a member. An inflow that cannot be matched to a member is parked as
``unmatched`` for an officer to look at, not silently absorbed.
"""

from datetime import datetime

from ledger import (CASH, LOANS_RECEIVABLE, LOAN_INTEREST_INCOME, MEMBER_DEPOSITS,
                    SHARE_CAPITAL, get_default_cash_account, post_journal_required)
from utils import share_capital_split, split_repayment

# Liability — money received from a member but not yet applied to anything.
VA_UNALLOCATED = '2010'

# How an unattended inflow is applied when the member has not said. A
# cooperative picks one.
ALLOCATION_RULES = {
    'savings':    'Put it all into savings',
    'loan_first': 'Clear what they owe on their loans first, rest to savings',
    'manual':     'Hold it and let an officer decide',
}
DEFAULT_RULE = 'savings'

# What a member can say their own transfers are for. Their choice beats the
# cooperative's rule, because they know what they are paying for. Each option
# spills the remainder into savings rather than leaving money in limbo.
MEMBER_PREFERENCES = {
    '':        "Let the cooperative decide",
    'savings': 'My savings',
    'loan':    'My loan repayment, then savings',
    'ctas':    'My Target Advance contribution, then savings',
}

_SETTING_KEYS = ('va_enabled', 'va_provider', 'va_preferred_bank', 'va_allocation_rule')


# ── Configuration ─────────────────────────────────────────────────────────────

def va_config(db) -> dict:
    cfg = {}
    try:
        rows = db.execute(
            'SELECT key, value FROM settings WHERE key IN (%s)'
            % ','.join('?' for _ in _SETTING_KEYS), _SETTING_KEYS).fetchall()
        cfg = {r['key']: (r['value'] or '') for r in rows}
    except Exception:
        pass
    cfg.setdefault('va_provider', 'paystack')
    cfg.setdefault('va_preferred_bank', 'wema-bank')
    rule = cfg.get('va_allocation_rule') or DEFAULT_RULE
    cfg['va_allocation_rule'] = rule if rule in ALLOCATION_RULES else DEFAULT_RULE
    return cfg


def va_enabled(db) -> bool:
    return str(va_config(db).get('va_enabled', '')) == '1'


def va_ensure_accounts(db) -> None:
    """Seed the holding account, only once a cooperative actually turns this on,
    so a coop that never uses virtual accounts keeps a clean chart of accounts."""
    try:
        db.execute(
            'INSERT INTO accounts (code, name, type, normal_balance, parent_code) '
            'VALUES (?, ?, ?, ?, NULL) ON CONFLICT(code) DO NOTHING',
            (VA_UNALLOCATED, 'Unallocated Member Receipts', 'liability', 'credit'))
    except Exception:
        pass


def set_setting(db, key, value) -> None:
    row = db.execute('SELECT id FROM settings WHERE key = ?', (key,)).fetchone()
    if row:
        db.execute('UPDATE settings SET value = ? WHERE key = ?', (value, key))
    else:
        db.execute('INSERT INTO settings (key, value, description) VALUES (?, ?, ?)',
                   (key, value, f'Virtual account setting: {key}'))


def set_va_enabled(db, on: bool) -> None:
    set_setting(db, 'va_enabled', '1' if on else '0')
    if on:
        va_ensure_accounts(db)


# ── Provisioning ──────────────────────────────────────────────────────────────

def account_for_member(db, member_id, provider='paystack'):
    return db.execute(
        'SELECT * FROM member_virtual_accounts WHERE member_id = ? AND provider = ?',
        (member_id, provider)).fetchone()


def provision_account(db, member_id, created_by=None):
    """Give one member their own account number.

    Returns (ok, message). Idempotent: a member who already has one keeps it —
    reissuing would strand every payment already set up against the old number.
    """
    cfg = va_config(db)
    provider = cfg['va_provider']

    existing = account_for_member(db, member_id, provider)
    if existing and existing['status'] == 'active':
        return True, f"Already has {existing['account_number']} ({existing['bank_name']})."

    member = db.execute('SELECT * FROM members WHERE id = ?', (member_id,)).fetchone()
    if not member:
        return False, 'Member not found.'
    email = (member['email'] or '').strip()
    if not email:
        return False, 'This member has no email address, which the bank requires.'

    from payments import get_gateway
    gw = get_gateway(provider)
    if not hasattr(gw, 'create_dedicated_account'):
        return False, f'{provider.title()} does not support virtual accounts in CoopMS yet.'

    try:
        cust = gw.create_customer(email, member['first_name'] or '',
                                  member['last_name'] or '', member['phone'] or '')
        if not cust.get('status'):
            return False, f"Bank rejected the member details: {cust.get('message', 'unknown error')}"
        customer_code = cust.get('data', {}).get('customer_code', '')
        if not customer_code:
            return False, 'Bank did not return a customer reference.'

        resp = gw.create_dedicated_account(customer_code, cfg['va_preferred_bank'])
        if not resp.get('status'):
            return False, f"Could not create the account: {resp.get('message', 'unknown error')}"
        data = resp.get('data', {}) or {}
        bank = data.get('bank', {}) or {}
    except Exception as exc:
        return False, f'Could not reach the bank: {exc}'

    account_number = data.get('account_number', '')
    if not account_number:
        return False, 'Bank did not return an account number.'

    if existing:
        db.execute('''UPDATE member_virtual_accounts
                         SET customer_code = ?, account_number = ?, account_name = ?,
                             bank_name = ?, bank_slug = ?, provider_account_id = ?,
                             status = 'active', closed_at = NULL
                       WHERE id = ?''',
                   (customer_code, account_number, data.get('account_name', ''),
                    bank.get('name', ''), bank.get('slug', ''), str(data.get('id', '')),
                    existing['id']))
    else:
        db.execute('''INSERT INTO member_virtual_accounts
                          (member_id, provider, customer_code, account_number, account_name,
                           bank_name, bank_slug, provider_account_id, status, created_by)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)''',
                   (member_id, provider, customer_code, account_number,
                    data.get('account_name', ''), bank.get('name', ''), bank.get('slug', ''),
                    str(data.get('id', '')), created_by))

    return True, f"{account_number} ({bank.get('name', 'bank')})"


def members_without_accounts(db, provider='paystack'):
    """Active members who could be given an account number."""
    return db.execute('''
        SELECT m.* FROM members m
        LEFT JOIN member_virtual_accounts v
               ON v.member_id = m.id AND v.provider = ? AND v.status = 'active'
        WHERE m.status = 'active' AND v.id IS NULL
        ORDER BY m.member_number
    ''', (provider,)).fetchall()


# ── Receiving money ───────────────────────────────────────────────────────────

def find_member_for_inflow(db, account_number='', customer_code='', provider='paystack'):
    """Which member does this money belong to?

    The account number is the reliable signal — it is the whole point of the
    scheme. The customer code is a fallback for providers that omit the account
    on some events.
    """
    row = None
    if account_number:
        row = db.execute(
            'SELECT * FROM member_virtual_accounts '
            'WHERE account_number = ? AND provider = ?',
            (str(account_number), provider)).fetchone()
    if row is None and customer_code:
        row = db.execute(
            'SELECT * FROM member_virtual_accounts '
            'WHERE customer_code = ? AND provider = ?',
            (customer_code, provider)).fetchone()
    return row


def record_receipt(db, provider_reference, amount, account_number='', customer_code='',
                   sender_name='', sender_bank='', narration='', provider='paystack',
                   received_at=None):
    """Bank one inflow. Idempotent on (provider, provider_reference), because a
    gateway may deliver the same webhook more than once.

    Returns (receipt_id, is_new).
    """
    existing = db.execute(
        'SELECT id FROM virtual_account_receipts '
        'WHERE provider = ? AND provider_reference = ?',
        (provider, str(provider_reference))).fetchone()
    if existing:
        return existing['id'], False

    amount = round(float(amount or 0), 2)
    va = find_member_for_inflow(db, account_number, customer_code, provider)

    db.execute('''INSERT INTO virtual_account_receipts
                      (member_id, virtual_account_id, provider, provider_reference, amount,
                       account_number, sender_name, sender_bank, narration, status, received_at)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
               (va['member_id'] if va else None, va['id'] if va else None, provider,
                str(provider_reference), amount, str(account_number or ''),
                sender_name, sender_bank, narration,
                'unallocated' if va else 'unmatched',
                received_at or datetime.now()))
    from database import last_insert_id
    receipt_id = last_insert_id(db)

    # Bank it straight away. The cooperative holds the member's money from now,
    # whether or not anyone has decided what it is for.
    cash_account = get_default_cash_account(db) or CASH
    who = f"member {va['member_id']}" if va else 'unmatched sender'
    post_journal_required(db, f'Transfer received — {who}', [
        {'account': cash_account, 'debit': amount, 'memo': f'To {account_number}'},
        {'account': VA_UNALLOCATED, 'credit': amount, 'memo': sender_name or who},
    ], reference=f'VA-{provider_reference}', source_module='va_receipt',
        source_id=receipt_id)

    return receipt_id, True


# ── Deciding what it is for ───────────────────────────────────────────────────

def member_loan_balances(db, member_id):
    """Active loans, oldest first — the order arrears are cleared in."""
    return db.execute(
        "SELECT * FROM loans WHERE member_id = ? AND status = 'active' AND balance > 0 "
        "ORDER BY id", (member_id,)).fetchall()


def member_preference(db, member_id):
    """What this member said their transfers are for. '' means they have not
    said, so the cooperative's rule applies."""
    try:
        row = db.execute('SELECT payment_preference FROM members WHERE id = ?',
                         (member_id,)).fetchone()
        pref = (row['payment_preference'] or '') if row else ''
    except Exception:
        return ''
    return pref if pref in MEMBER_PREFERENCES else ''


def set_member_preference(db, member_id, preference):
    if preference not in MEMBER_PREFERENCES:
        return False
    db.execute('UPDATE members SET payment_preference = ? WHERE id = ?',
               (preference, member_id))
    return True


def ctas_due_rows(db, member_id):
    """This member's unpaid Target Advance contributions, oldest first.

    Empty when the module is off, they never joined, or they are up to date —
    all of which simply mean there is nothing to pay towards.
    """
    try:
        from blueprints.ctas import ctas_enabled
        if not ctas_enabled(db):
            return []
        return db.execute('''
            SELECT sc.*, s.member_id
            FROM ctas_schedule sc
            JOIN ctas_subscriptions s ON s.id = sc.subscription_id
            WHERE s.member_id = ?
              AND s.status IN ('enrolled', 'scheduled', 'active_recovery')
              AND sc.status IN ('due', 'grace', 'late', 'partial')
              AND COALESCE(sc.expected_amount, 0) > COALESCE(sc.paid_amount, 0)
            ORDER BY sc.due_date, sc.period_number
        ''', (member_id,)).fetchall()
    except Exception:
        return []


def _loan_legs(db, member_id, remaining):
    legs = []
    for loan in member_loan_balances(db, member_id):
        if remaining <= 0:
            break
        part = round(min(remaining, float(loan['balance'] or 0)), 2)
        if part > 0:
            legs.append({'target': 'loan', 'loan_id': loan['id'], 'amount': part})
            remaining = round(remaining - part, 2)
    return legs, remaining


def _ctas_legs(db, member_id, remaining):
    legs = []
    for row in ctas_due_rows(db, member_id):
        if remaining <= 0:
            break
        owed = round(float(row['expected_amount'] or 0) - float(row['paid_amount'] or 0), 2)
        part = round(min(remaining, owed), 2)
        if part > 0:
            legs.append({'target': 'ctas', 'subscription_id': row['subscription_id'],
                         'period': row['period_number'], 'amount': part})
            remaining = round(remaining - part, 2)
    return legs, remaining


def build_plan(db, member_id, amount, rule=None):
    """Work out where an inflow should go, without applying anything.

    The member's own instruction wins: they know what they are paying for, and
    a member who has said "this is my Target Advance" should not have it swept
    into savings by a cooperative-wide default. Only when they have not said
    does the cooperative's rule decide.

    Returns a list of legs. An empty list means hold it — either an officer
    decides, or there is nothing to apply it to.
    """
    amount = round(float(amount or 0), 2)
    if amount <= 0:
        return []

    plan = []
    remaining = amount
    pref = member_preference(db, member_id)

    if pref == 'ctas':
        plan, remaining = _ctas_legs(db, member_id, remaining)
    elif pref == 'loan':
        plan, remaining = _loan_legs(db, member_id, remaining)
    elif pref == 'savings':
        pass                                    # straight to savings below
    else:
        rule = rule or va_config(db)['va_allocation_rule']
        if rule == 'manual':
            return []
        if rule == 'loan_first':
            plan, remaining = _loan_legs(db, member_id, remaining)

    # Whatever is left becomes savings. A member who names a target and sends
    # more than it needs is saving the difference, not leaving it unallocated.
    if remaining > 0:
        plan.append({'target': 'savings', 'loan_id': None, 'amount': remaining})
    return plan


def apply_plan(db, receipt_id, plan, created_by=None):
    """Apply a receipt. Returns (ok, message).

    Refuses to apply more than arrived — the holding account must never go
    negative, or the books would claim the cooperative received money it did not.
    """
    receipt = db.execute('SELECT * FROM virtual_account_receipts WHERE id = ?',
                         (receipt_id,)).fetchone()
    if not receipt:
        return False, 'Receipt not found.'
    if not receipt['member_id']:
        return False, 'This money is not matched to a member yet.'

    already = round(float(receipt['allocated_amount'] or 0), 2)
    total = round(sum(float(p['amount'] or 0) for p in plan), 2)
    available = round(float(receipt['amount'] or 0) - already, 2)
    if total <= 0:
        return False, 'Nothing to apply.'
    if total > available + 0.005:
        return False, (f'Only ₦{available:,.2f} of this transfer is left to apply.')

    member_id = receipt['member_id']
    notes = []
    for part in plan:
        amt = round(float(part['amount'] or 0), 2)
        if amt <= 0:
            continue
        if part['target'] == 'loan':
            note = _apply_to_loan(db, receipt, member_id, part.get('loan_id'), amt, created_by)
        elif part['target'] == 'ctas':
            note = _apply_to_ctas(db, receipt, member_id, part.get('subscription_id'),
                                  part.get('period'), amt, created_by)
        else:
            note = _apply_to_savings(db, receipt, member_id, amt, created_by)
        if note:
            notes.append(note)

    applied = round(already + total, 2)
    status = 'allocated' if applied >= round(float(receipt['amount']), 2) - 0.005 else 'part_allocated'
    db.execute('''UPDATE virtual_account_receipts
                     SET allocated_amount = ?, status = ?, allocated_at = ?, allocated_by = ?
                   WHERE id = ?''',
               (applied, status, datetime.now(), created_by, receipt_id))
    return True, '; '.join(notes) or 'Applied.'


def _record_allocation(db, receipt_id, target, target_id, loan_id, amount, created_by):
    db.execute('''INSERT INTO virtual_account_allocations
                      (receipt_id, target, target_id, loan_id, amount, created_by)
                  VALUES (?, ?, ?, ?, ?, ?)''',
               (receipt_id, target, target_id, loan_id, amount, created_by))
    from database import last_insert_id
    return last_insert_id(db)


def _apply_to_savings(db, receipt, member_id, amount, created_by):
    """Turn part of a transfer into a savings deposit.

    A late fee is deliberately not charged here: this is money the member chose
    to send, not a deduction that arrived behind schedule.
    """
    from database import last_insert_id

    month = datetime.now().strftime('%Y-%m')
    deposit, share = share_capital_split(db, amount)

    # The receipt number is derived from the row's own id rather than a random
    # number: journal_entries.reference is unique, and a collision there loses
    # the posting silently.
    db.execute('''INSERT INTO savings
                      (member_id, amount, share_capital, month, payment_type, late_fee,
                       payment_method, receipt_number, notes, date, created_by)
                  VALUES (?, ?, ?, ?, 'monthly', 0, 'virtual_account', '', ?, ?, ?)''',
               (member_id, deposit, share, month,
                f"Bank transfer to {receipt['account_number']}", datetime.now(), created_by))
    sav_id = last_insert_id(db)
    receipt_number = f'VA/{sav_id}'
    db.execute('UPDATE savings SET receipt_number = ? WHERE id = ?', (receipt_number, sav_id))

    db.execute('UPDATE members SET total_savings = COALESCE(total_savings, 0) + ?, '
               'shares_value = COALESCE(shares_value, 0) + ? WHERE id = ?',
               (deposit, share, member_id))

    alloc_id = _record_allocation(db, receipt['id'], 'savings', sav_id, None, amount, created_by)

    lines = [{'account': VA_UNALLOCATED, 'debit': amount, 'memo': f'Member {member_id}'},
             {'account': MEMBER_DEPOSITS, 'credit': deposit, 'memo': f'Savings {month}'}]
    if share:
        lines.append({'account': SHARE_CAPITAL, 'credit': share, 'memo': 'Share capital'})
    post_journal_required(db, f'Transfer applied to savings — {month}', lines,
                      reference=receipt_number, source_module='va_savings',
                      source_id=alloc_id, created_by=created_by)

    if share:
        return (f'₦{deposit:,.2f} to savings, ₦{share:,.2f} to share capital')
    return f'₦{deposit:,.2f} to savings'


def _apply_to_loan(db, receipt, member_id, loan_id, amount, created_by):
    from database import last_insert_id

    loan = db.execute('SELECT * FROM loans WHERE id = ? AND member_id = ?',
                      (loan_id, member_id)).fetchone()
    if not loan:
        return None
    amount = round(min(amount, float(loan['balance'] or 0)), 2)
    if amount <= 0:
        return None

    principal, interest = split_repayment(amount, loan['amount'], loan['total_repayment'])
    new_balance = round(max(float(loan['balance'] or 0) - amount, 0), 2)

    db.execute('''INSERT INTO repayments
                      (repayment_number, loan_id, amount, principal_paid, interest_paid,
                       payment_method, reference, date)
                  VALUES ('', ?, ?, ?, ?, 'virtual_account', ?, ?)''',
               (loan_id, amount, principal, interest,
                receipt['provider_reference'], datetime.now()))
    rep_id = last_insert_id(db)
    rep_num = f'VAR/{rep_id}'          # unique by construction — see _apply_to_savings
    db.execute('UPDATE repayments SET repayment_number = ? WHERE id = ?', (rep_num, rep_id))

    if new_balance <= 0:
        db.execute("UPDATE loans SET balance = 0, status = 'completed', completed_at = ? "
                   'WHERE id = ?', (datetime.now(), loan_id))
    else:
        db.execute('UPDATE loans SET balance = ? WHERE id = ?', (new_balance, loan_id))

    alloc_id = _record_allocation(db, receipt['id'], 'loan', rep_id, loan_id, amount, created_by)

    post_journal_required(db, f"Transfer applied to loan — {loan['loan_number']}", [
        {'account': VA_UNALLOCATED, 'debit': amount, 'memo': f'Member {member_id}'},
        {'account': LOANS_RECEIVABLE, 'credit': principal, 'memo': loan['loan_number']},
        {'account': LOAN_INTEREST_INCOME, 'credit': interest, 'memo': 'Interest earned'},
    ], reference=rep_num, source_module='va_loan', source_id=alloc_id, created_by=created_by)

    return f"₦{amount:,.2f} to loan {loan['loan_number']}"


def _apply_to_ctas(db, receipt, member_id, subscription_id, period, amount, created_by):
    """Put part of a transfer towards the member's Target Advance contribution.

    The CTAS module owns this bookkeeping — whether the money funds the pool or
    repays an advance already paid out, and how the schedule is ticked off — so
    this hands over to its posting path rather than reproducing the rules. Only
    the account the money comes from differs: it is already banked, sitting in
    Unallocated Member Receipts, not arriving as fresh cash.
    """
    from blueprints.ctas import _post_contribution

    sub = db.execute('SELECT * FROM ctas_subscriptions WHERE id = ? AND member_id = ?',
                     (subscription_id, member_id)).fetchone()
    if not sub:
        return _apply_to_savings(db, receipt, member_id, amount, created_by)
    cycle = db.execute('SELECT * FROM ctas_cycles WHERE id = ?', (sub['cycle_id'],)).fetchone()
    if not cycle:
        return _apply_to_savings(db, receipt, member_id, amount, created_by)

    alloc_id = _record_allocation(db, receipt['id'], 'ctas', subscription_id, None,
                                  amount, created_by)
    posted = _post_contribution(db, sub, cycle, period, amount, created_by=created_by,
                                ref_suffix=f'-VA{alloc_id}', debit_account=VA_UNALLOCATED)
    posted = round(float(posted or 0), 2)

    # A member in recovery cannot repay more than they owe; the difference is
    # theirs, so it is saved rather than left sitting in the holding account.
    leftover = round(amount - posted, 2)
    if leftover > 0.005:
        db.execute('UPDATE virtual_account_allocations SET amount = ? WHERE id = ?',
                   (posted, alloc_id))
        extra = _apply_to_savings(db, receipt, member_id, leftover, created_by)
        return f'₦{posted:,.2f} to Target Advance, {extra}' if posted > 0 else extra

    return f'₦{posted:,.2f} to Target Advance (period {period})'


def auto_allocate(db, receipt_id):
    """Apply a freshly banked receipt using the cooperative's rule.

    Returns (applied, message). Nothing to apply is a normal outcome, not a
    failure: under the 'manual' rule everything waits for an officer.
    """
    receipt = db.execute('SELECT * FROM virtual_account_receipts WHERE id = ?',
                         (receipt_id,)).fetchone()
    if not receipt or receipt['status'] not in ('unallocated', 'part_allocated'):
        return False, ''
    if not receipt['member_id']:
        return False, ''

    outstanding = round(float(receipt['amount'] or 0) -
                        float(receipt['allocated_amount'] or 0), 2)
    plan = build_plan(db, receipt['member_id'], outstanding)
    if not plan:
        return False, ''
    return apply_plan(db, receipt_id, plan)


# ── Reading ───────────────────────────────────────────────────────────────────

def receipts(db, status=None, limit=100):
    sql = '''SELECT r.*, m.member_number, m.first_name, m.last_name
             FROM virtual_account_receipts r
             LEFT JOIN members m ON m.id = r.member_id'''
    args = []
    if status:
        sql += ' WHERE r.status = ?'
        args.append(status)
    sql += ' ORDER BY r.id DESC LIMIT ?'
    args.append(limit)
    return db.execute(sql, tuple(args)).fetchall()


def pending_total(db):
    """Money received but not yet applied — should agree with account 2010."""
    row = db.execute(
        "SELECT COALESCE(SUM(amount - COALESCE(allocated_amount, 0)), 0) AS t "
        "FROM virtual_account_receipts WHERE status IN ('unallocated', 'part_allocated', 'unmatched')"
    ).fetchone()
    return round(float(row['t'] or 0), 2)
