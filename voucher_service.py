"""Atomic voucher posting and explicitly linked member accounting records."""
import re
import secrets
from datetime import datetime
from decimal import Decimal, InvalidOperation

from database import USE_POSTGRES, last_insert_id
from ledger import (post_journal, get_postable_cash_accounts, MEMBER_DEPOSITS,
                    SHARE_CAPITAL, LOANS_RECEIVABLE, LOAN_INTEREST_INCOME)


def money(value):
    try:
        value = Decimal(str(value or '0'))
        if not value.is_finite() or value < 0 or value > Decimal('1000000000000'):
            raise ValueError()
        if value != value.quantize(Decimal('0.01')):
            raise ValueError()
        return float(value)
    except (InvalidOperation, ValueError):
        raise ValueError('Amounts must be non-negative numbers with at most two decimal places.')


def bank_accounts(db):
    return [a for a in get_postable_cash_accounts(db) if a['code'] != '1400']


def postable_accounts(db):
    rows = [dict(r) for r in db.execute('SELECT * FROM accounts WHERE is_active = 1').fetchall()]
    parents = {r['parent_code'] for r in rows if r['parent_code']}
    return [r for r in rows if r['code'] not in parents]


def _control(db, code):
    seen = set()
    while code and code not in seen:
        if code in (MEMBER_DEPOSITS, SHARE_CAPITAL, LOANS_RECEIVABLE):
            return code
        seen.add(code)
        row = db.execute('SELECT parent_code FROM accounts WHERE code = ?', (code,)).fetchone()
        code = row['parent_code'] if row else None
    return None


def _locked(db, table, record_id):
    # Table names are internal constants, never submitted input.
    return db.execute(f'SELECT * FROM {table} WHERE id = ?' + (' FOR UPDATE' if USE_POSTGRES else ''),
                      (record_id,)).fetchone()


def post_voucher(db, form, raw_lines, actor):
    token = (form.get('submission_token') or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,80}', token):
        raise ValueError('This form has expired. Open a new voucher and try again.')
    db.execute('SAVEPOINT voucher_post')
    if USE_POSTGRES:
        db.execute('SELECT pg_advisory_xact_lock(7319042)')
    else:
        db.execute('UPDATE vouchers SET id = id WHERE 1 = 0')
    existing = db.execute('SELECT id FROM vouchers WHERE submission_token = ?', (token,)).fetchone()
    if existing:
        return existing['id'], False
    kind = form.get('kind')
    if kind not in ('receipt', 'payment', 'journal'):
        raise ValueError('Choose Receipt, Payment or Journal.')
    description = (form.get('description') or '').strip()
    party = (form.get('party') or '').strip()
    if not description or (kind != 'journal' and not party):
        raise ValueError('Enter a description and the payer/payee for receipts and payments.')
    try:
        date = datetime.strptime(form.get('date', ''), '%Y-%m-%d')
    except ValueError:
        raise ValueError('Enter a valid voucher date.')
    bank = (form.get('bank_account') or '').strip()
    if kind != 'journal' and bank not in {a['code'] for a in bank_accounts(db)}:
        raise ValueError('Choose an active bank/cash account, not the payroll control account.')
    purpose = form.get('purpose', 'general')
    if purpose not in ('general', 'loan_disbursement') or (purpose == 'loan_disbursement' and kind != 'payment'):
        raise ValueError('Loan disbursement is only available on a Payment voucher.')
    number = f"{dict(receipt='RV', payment='PV', journal='JV')[kind]}/{date:%Y%m%d}/{secrets.token_hex(4).upper()}"
    db.execute('''INSERT INTO vouchers
        (voucher_number, submission_token, kind, party, description, reference, date, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (number, token, kind, party, description, (form.get('reference') or '').strip(), date, actor.id))
    vid = last_insert_id(db)
    if purpose == 'loan_disbursement':
        from blueprints.loans import _disburse_loan, _due_diligence_complete, _acting_on_own_loan
        import loan_workflow as lw
        import permissions
        loan = _locked(db, 'loans', form.get('disbursement_loan_id'))
        if (not permissions.user_can('loans.approve') or not loan or loan['status'] != 'pending'
                or lw.NEXT_STAGE.get(loan['approval_stage']) != lw.STAGE_APPROVED
                or not lw.can_act(actor.role, loan['approval_stage'])):
            raise ValueError('Select a loan awaiting final approval; you must be authorised to approve it.')
        if _acting_on_own_loan(db, loan):
            raise ValueError('You cannot approve or disburse your own loan.')
        if not _due_diligence_complete(loan)[0]:
            raise ValueError('Complete this loan’s due-diligence checks before disbursement.')
        if db.execute("SELECT id FROM journal_entries WHERE source_module = 'loan_disbursement' AND source_id = ?", (loan['id'],)).fetchone():
            raise ValueError('This loan already has a disbursement entry.')
        lw.record_action(db, loan['id'], loan['approval_stage'], 'approved', actor.id, actor.username, f'Payment voucher {number}')
        _disburse_loan(db, loan, bank, posting_date=date)
        journal = db.execute("SELECT id FROM journal_entries WHERE source_module = 'loan_disbursement' AND source_id = ? ORDER BY id DESC LIMIT 1", (loan['id'],)).fetchone()['id']
    else:
        allowed = {a['code'] for a in postable_accounts(db)}
        if not raw_lines or len(raw_lines) > 100:
            raise ValueError('Enter between 1 and 100 voucher lines.')
        lines = []
        for index, raw in enumerate(raw_lines, 1):
            code = str(raw.get('account') or '').strip()
            debit, credit = money(raw.get('debit')), money(raw.get('credit'))
            if not code and not debit and not credit and not raw.get('amount'):
                continue
            if code not in allowed:
                raise ValueError(f'Line {index}: choose an active detail account; headings cannot be posted to.')
            if kind != 'journal':
                amount = money(raw.get('amount'))
                debit, credit = (amount, 0) if kind == 'payment' else (0, amount)
                if code == bank:
                    raise ValueError('The counter account cannot be the selected bank account.')
            if (debit > 0) == (credit > 0):
                raise ValueError(f'Line {index}: enter a positive amount on exactly one side.')
            member_id, loan_id = raw.get('member_id') or None, raw.get('loan_id') or None
            memo = str(raw.get('memo') or '').strip()
            control = _control(db, code)
            subref = f'{number}-{index}'
            target, target_id = None, None
            if control in (MEMBER_DEPOSITS, SHARE_CAPITAL):
                member = _locked(db, 'members', member_id)
                if not member:
                    raise ValueError(f'Line {index}: select the member whose savings or shares will change.')
                delta = round(credit - debit, 2)
                column = 'amount' if control == MEMBER_DEPOSITS else 'share_capital'
                balance = db.execute(f'SELECT COALESCE(SUM({column}), 0) AS balance FROM savings WHERE member_id = ?', (member['id'],)).fetchone()['balance']
                if round(float(balance) + delta, 2) < 0:
                    raise ValueError(f'Line {index}: this would make the member’s savings or shares negative.')
                deposit, share = (delta, 0) if control == MEMBER_DEPOSITS else (0, delta)
                db.execute('''INSERT INTO savings (member_id, amount, share_capital, month,
                    payment_type, payment_method, receipt_number, reference, notes, date, created_by)
                    VALUES (?, ?, ?, ?, 'voucher', ?, ?, ?, ?, ?, ?)''',
                    (member['id'], deposit, share, date.strftime('%Y-%m'), kind, subref, number, description, date, actor.id))
                target, target_id = 'savings', last_insert_id(db)
                db.execute('UPDATE members SET total_savings = COALESCE(total_savings,0) + ?, shares_value = COALESCE(shares_value,0) + ? WHERE id = ?', (deposit, share, member['id']))
                memo = f"{member['member_number']}: {memo}"
            elif control == LOANS_RECEIVABLE:
                if kind == 'payment':
                    raise ValueError('Use Payment → Loan disbursement to pay a member loan through its approval workflow.')
                loan = _locked(db, 'loans', loan_id)
                if not loan or loan['status'] not in ('active', 'completed'):
                    raise ValueError(f'Line {index}: select an active or completed loan.')
                if member_id and str(member_id) != str(loan['member_id']):
                    raise ValueError(f'Line {index}: the loan does not belong to the selected member.')
                old_balance = float(loan['balance'])
                new_balance = round(old_balance + debit - credit, 2)
                if new_balance < 0:
                    raise ValueError(f'Line {index}: credit exceeds the loan’s outstanding balance.')
                if kind == 'receipt':
                    from utils import split_repayment
                    principal, interest = split_repayment(credit, loan['amount'], loan['total_repayment'])
                    db.execute('''INSERT INTO repayments (repayment_number, loan_id, amount, principal_paid,
                        interest_paid, payment_method, receipt_number, reference, notes, date, received_by)
                        VALUES (?, ?, ?, ?, ?, 'voucher', ?, ?, ?, ?, ?)''',
                        (subref, loan['id'], credit, principal, interest, subref, number, description, date, actor.id))
                    target, target_id = 'repayment', last_insert_id(db)
                    credit = principal
                    if interest:
                        lines.append(dict(account=LOAN_INTEREST_INCOME, credit=interest, memo=f'{loan["loan_number"]} interest'))
                else:
                    delta = round(debit - credit, 2)
                    db.execute('''INSERT INTO loan_adjustments (adjustment_number, loan_id, member_id, amount,
                        direction, previous_balance, new_balance, reason, reference, created_by, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (subref, loan['id'], loan['member_id'], abs(delta), 'increase' if delta > 0 else 'decrease',
                         old_balance, new_balance, description, number, actor.id, date))
                    target, target_id = 'loan_adjustment', last_insert_id(db)
                db.execute('UPDATE loans SET balance = ?, status = ?, completed_at = ? WHERE id = ?',
                           (new_balance, 'completed' if new_balance == 0 else 'active', date if new_balance == 0 else None, loan['id']))
                memo = f'{loan["loan_number"]}: {memo}'
            if target:
                db.execute('INSERT INTO voucher_allocations (voucher_id, target, target_id) VALUES (?, ?, ?)', (vid, target, target_id))
            lines.append(dict(account=code, debit=debit, credit=credit, memo=memo))
        if not lines:
            raise ValueError('Enter at least one voucher line.')
        if kind != 'journal':
            total = round(sum(l.get('credit', 0) if kind == 'receipt' else l.get('debit', 0) for l in lines), 2)
            lines.append(dict(account=bank, debit=total if kind == 'receipt' else 0,
                              credit=total if kind == 'payment' else 0, memo=party))
        journal = post_journal(db, description, lines, date=date, reference=number,
                               source_module='voucher', source_id=vid, created_by=actor.id)
        if journal is None:
            raise ValueError('Enter a nonzero balanced voucher.')
    db.execute('UPDATE vouchers SET journal_entry_id = ? WHERE id = ?', (journal, vid))
    db.execute('UPDATE loan_adjustments SET journal_entry_id = ? WHERE id IN (SELECT target_id FROM voucher_allocations WHERE voucher_id = ? AND target = ?)', (journal, vid, 'loan_adjustment'))
    return vid, True


def reverse_voucher_records(db, entry, voucher_id):
    from ledger import _reverse_savings_deposit, _reverse_loan_repayment, _reverse_loan_adjustment
    handlers = {'savings': _reverse_savings_deposit, 'repayment': _reverse_loan_repayment,
                'loan_adjustment': _reverse_loan_adjustment}
    links = db.execute('SELECT * FROM voucher_allocations WHERE voucher_id = ? ORDER BY id DESC', (voucher_id,)).fetchall()
    for link in links:
        if link['target'] == 'savings':
            sav = db.execute('SELECT * FROM savings WHERE id = ?', (link['target_id'],)).fetchone()
            member = _locked(db, 'members', sav['member_id'])
            balances = db.execute('SELECT COALESCE(SUM(amount),0) AS savings, COALESCE(SUM(share_capital),0) AS shares FROM savings WHERE member_id = ?', (member['id'],)).fetchone()
            if float(balances['savings']) < float(sav['amount']) or float(balances['shares']) < float(sav['share_capital'] or 0):
                raise ValueError('Cannot reverse: this member has already used the credited savings or shares.')
        handlers[link['target']](db, entry, link['target_id'])
    return 'Voucher and linked member records reversed.'
