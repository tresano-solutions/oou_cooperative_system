"""Read-only payroll matching and allocation preparation; never posts money."""
import csv
import io
import json
import re
import unicodedata
import zipfile
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def money(value):
    try:
        number = Decimal(str(value).replace(',', '').strip())
        if not number.is_finite() or number < 0:
            raise ValueError('Amounts must be finite and non-negative.')
        return number.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError):
        raise ValueError('Enter a valid amount.')


def name_key(value):
    return ' '.join(sorted(re.findall(r'\w+', unicodedata.normalize('NFKC', str(value or '')).casefold())))


def identifier(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value or '').strip()


def month_date(value):
    if not re.fullmatch(r'\d{4}-\d{2}', value or ''):
        raise ValueError('Select a payroll month.')
    return datetime.strptime(value, '%Y-%m')


def historical_payment(history, balance):
    """History suggests a deduction, never establishes contractual terms."""
    if not history:
        return None, 'No usable historical payments; review required'
    recent = sorted(history.items(), reverse=True)[:3]
    latest_month, latest_amount = recent[0]
    amounts = [amount for _, amount in recent]
    if len(recent) == 1:
        confidence = 'Only one month available; review required'
    elif len(set(amounts)) == 1:
        confidence = f'Consistent across {len(recent)} recorded months; confirm'
    else:
        confidence = 'Irregular history; latest month suggested; review required'
    evidence = '; '.join(f'{period}: {amount:,.2f}' for period, amount in recent)
    capped = '; capped at outstanding balance' if latest_amount > balance else ''
    return min(balance, latest_amount), f'Historical monthly total ({evidence}). {confidence}{capped}'


def expected_payment(loan, month, history=None):
    """Prefer saved schedules; use labelled historical suggestions as fallback."""
    period = month_date(month)
    balance = money(loan['balance'])
    first = loan.get('first_payment_date')
    if not first:
        return historical_payment(history, balance)
    try:
        start = datetime.fromisoformat(str(first))
        n = (period.year - start.year) * 12 + period.month - start.month
        tenure = int(loan.get('tenure') or 0)
        if n < 0:
            return Decimal('0.00'), 'Repayment starts ' + start.strftime('%Y-%m-%d')
        if n >= tenure or tenure <= 0:
            return None, 'Outside scheduled term; review outstanding balance'
        snapshot = json.loads(loan.get('repayment_schedule_snapshot') or '{}')
        schedule = snapshot.get('schedule', [])
        if schedule:
            amount = money(schedule[n]['payment'])
            return min(balance, amount), 'Accepted repayment schedule'
        if (str(loan.get('loan_number', '')).startswith('TALLY/')
                or 'migrat' in str(loan.get('notes') or '').lower()):
            return historical_payment(history, balance)
        total = money(loan['total_repayment'])
        amount = money(total / tenure)
        if n == tenure - 1:
            amount = total - amount * (tenure - 1)
        return min(balance, amount), 'Stored contract total / tenure'
    except (ValueError, TypeError, KeyError, IndexError, ArithmeticError):
        return historical_payment(history, balance)


def loan_book(db, month):
    period = month_date(month)
    rows = db.execute('''SELECT l.*, m.member_number, m.employee_id,
        m.first_name || ' ' || m.last_name AS member_name
        FROM loans l JOIN members m ON m.id=l.member_id
        WHERE l.status='active' AND l.balance>0 ORDER BY member_name,l.id''').fetchall()
    # Aggregate real, unreversed payments by loan/month. Do not infer repayments
    # from opening balances or corrections, or leak current/future payroll data.
    payments = db.execute('''SELECT r.loan_id,r.amount,r.date,r.notes
        FROM repayments r JOIN loans l ON l.id=r.loan_id
        WHERE l.status='active' AND l.balance>0 AND r.reversed_at IS NULL
        AND SUBSTR(CAST(r.date AS TEXT),1,7) >= ?
        AND SUBSTR(CAST(r.date AS TEXT),1,7) < ?''',
        (f'{period.year - 1:04d}-{period.month:02d}', month)).fetchall()
    histories = {}
    for payment in payments:
        note = str(payment['notes'] or '').lower()
        if any(term in note for term in ('pre-liquidation', 'preliquidation', 'settled in full', 'catch-up', 'catchup')):
            continue
        try:
            amount = money(payment['amount'])
        except ValueError:
            continue
        if amount <= 0:
            continue
        history = histories.setdefault(payment['loan_id'], {})
        payment_month = str(payment['date'])[:7]
        history[payment_month] = history.get(payment_month, Decimal(0)) + amount
    result = []
    for row in rows:
        loan = dict(row)
        expected, basis = expected_payment(loan, month, histories.get(loan['id']))
        result.append({**loan, 'expected': str(expected) if expected is not None else '', 'basis': basis})
    return result


def has_month_payment(db, member_id, month):
    return db.execute('''SELECT r.id FROM repayments r JOIN loans l ON l.id=r.loan_id
        WHERE l.member_id=? AND r.reversed_at IS NULL
        AND SUBSTR(CAST(r.date AS TEXT),1,7)=?''', (member_id, month)).fetchone() is not None


def has_month_savings(db, member_id, month):
    return db.execute('''SELECT id FROM savings WHERE member_id=? AND reversed_at IS NULL
        AND (amount>0 OR COALESCE(share_capital,0)>0) AND month=?''', (member_id, month)).fetchone() is not None


ALIASES = {
    'member_number': ('member_number', 'member_no', 'membership_number'),
    'employee_id': ('employee_id', 'staff_number', 'staff_no', 'employee_number', 'staff_id'),
    'name': ('name', 'full_name', 'member_name', 'employee_name', 'loan_name'),
    'amount': ('amount', 'deduction', 'loan_repayment', 'repayment_amount', 'total_deduction', 'cooperative_loan'),
    'savings_amount': ('cooperative_savings', 'savings', 'savings_amount'),
    'subscription_amount': ('cooperative_subscription', 'subscription', 'subscription_fee'),
}


def read_payroll(raw, filename):
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError('Maximum file size is 5 MB.')
    if filename.lower().endswith('.xlsx'):
        import openpyxl
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if sum(i.file_size for i in archive.infolist()) > 30 * 1024 * 1024:
                raise ValueError('Workbook is too large when expanded.')
        book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        try:
            if len(book.worksheets) != 1:
                raise ValueError('Upload a workbook with one payroll sheet.')
            matrix = []
            for row in book.active.iter_rows(values_only=True):
                if len(matrix) >= 2002:
                    raise ValueError('Maximum 2,000 payroll rows.')
                matrix.append(row)
        finally:
            book.close()
    elif filename.lower().endswith('.csv'):
        text = raw.decode('utf-8-sig')
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t')
        except csv.Error:
            dialect = csv.excel
        matrix = list(csv.reader(io.StringIO(text), dialect))
    else:
        raise ValueError('Upload an Excel .xlsx or CSV file.')
    if not matrix or len(matrix) > 2001:
        raise ValueError('Upload between 1 and 2,000 payroll rows.')
    headers = [re.sub(r'[^a-z0-9]+', '_', str(v or '').strip().lower()).strip('_') for v in matrix[0]]
    columns = {}
    for key, aliases in ALIASES.items():
        indices = [i for i, h in enumerate(headers) if h in aliases]
        if len(indices) > 1:
            raise ValueError('Duplicate column for ' + key)
        if indices:
            columns[key] = indices[0]
    if not set(columns).intersection(('amount', 'savings_amount')) or not set(columns).intersection(('member_number', 'employee_id', 'name')):
        raise ValueError('Include loan/savings deductions and member_number, Employee Number or Full name columns.')
    rows = []
    for number, values in enumerate(matrix[1:], 2):
        if not any(v not in (None, '') for v in values):
            continue
        row = {key: identifier(values[i]) if i < len(values) else '' for key, i in columns.items()}
        row['row'] = number
        rows.append(row)
    return remove_verified_totals(rows)


def remove_verified_totals(rows):
    """Recognize only trailing reconciled HR control totals, never member rows."""
    details = list(rows)
    footers = []
    while details:
        row = details[-1]
        if any(row.get(k) for k in ('member_number', 'employee_id', 'name')):
            break
        footers.insert(0, details.pop())
    if not footers:
        return rows
    if len(details) < 2:
        return rows
    totals = {k: sum((money(r.get(k) or '0') for r in details), Decimal(0))
              for k in ('amount', 'savings_amount', 'subscription_amount')}
    for row in footers:
        label_keys = [k for k in totals if str(row.get(k, '')).strip().upper() in ('TOTAL', 'GRAND TOTAL')]
        if label_keys:
            numbers = [money(row[k]) for k in totals if row.get(k) and k not in label_keys]
            if len(numbers) != 1 or numbers[0] != sum(totals.values()):
                raise ValueError(f'Row {row["row"]}: HR grand total does not reconcile to employee deductions.')
        else:
            if not any(row.get(k) for k in totals) or any(money(row.get(k) or '0') != totals[k] for k in totals):
                raise ValueError(f'Row {row["row"]}: missing employee identity or incorrect HR total.')
    return details


def match_payroll(rows, members, book):
    result, seen = [], set()
    for row in rows:
        item = {'row': row['row'], 'name': row.get('name', ''), 'amount': row.get('amount', ''),
                'source_name': row.get('name', ''), 'source_staff': row.get('employee_id', ''),
                'savings_amount': row.get('savings_amount', '0'),
                'subscription_amount': row.get('subscription_amount', '0'),
                'member_id': None, 'member_number': '', 'employee_id': '',
                'loans': [], 'error': '', 'loan_error': '', 'savings_error': '', 'match': ''}
        try:
            amount = money(row.get('amount') or '0')
            savings = money(row.get('savings_amount') or '0')
            subscription = money(row.get('subscription_amount') or '0')
            if amount + savings + subscription <= 0:
                raise ValueError('No positive deductions on this row.')
            item['amount'] = str(amount)
            item['savings_amount'] = str(savings)
            item['subscription_amount'] = str(subscription)
            supplied = [(key, row.get(key)) for key in ('member_number', 'employee_id') if row.get(key)]
            candidates = list(members)
            manual = row.get('selected_member_id')
            if manual:
                candidates = [m for m in members if str(m['id']) == str(manual)]
                item['match'] = 'Manually selected member; confirm identity'
            elif supplied:
                for key, value in supplied:
                    # Some migrated cooperatives stored HR numbers as member numbers.
                    candidates = [m for m in candidates if identifier(m.get(key)) == value
                                  or (key == 'employee_id' and identifier(m.get('member_number')) == value)]
                item['match'] = 'Member / staff number'
            else:
                candidates = [m for m in candidates if name_key(m['member_name']) == name_key(row.get('name')) and name_key(row.get('name'))]
                item['match'] = 'Exact name (review identity)'
            if len(candidates) != 1:
                raise ValueError('No unique member match. Select the correct member for review.')
            member = candidates[0]
            if not manual and row.get('name') and name_key(member['member_name']) != name_key(row['name']):
                raise ValueError('Name and identifier disagree. Select the verified member for review.')
            if member['id'] in seen:
                raise ValueError('Duplicate member row; consolidate the deduction before matching.')
            seen.add(member['id'])
            item.update(member_id=member['id'], name=member['member_name'],
                        member_number=identifier(member.get('member_number')),
                        employee_id=identifier(member.get('employee_id')))
            if savings and not (item['member_number'] or item['employee_id']):
                item['savings_error'] = 'Member has no member/staff number for savings import; update the member record.'
            if not amount:
                result.append(item)
                continue
            loans = [dict(l) for l in book if l['member_id'] == member['id']]
            if not loans:
                item['loan_error'] = 'No active loan with an outstanding balance.'
                result.append(item)
                continue
            for loan in loans:
                loan['allocation'] = ''
            if len(loans) == 1 and amount <= money(loans[0]['balance']):
                loans[0]['allocation'] = str(amount)
            elif all(l['expected'] != '' for l in loans) and sum((money(l['expected']) for l in loans), Decimal(0)) == amount:
                for loan in loans:
                    loan['allocation'] = loan['expected']
            item['loans'] = loans
        except ValueError as exc:
            item['error'] = str(exc)
        result.append(item)
    return result


def validate_allocations(item, amounts, current_loans):
    if item['error'] or item.get('loan_error') or not item['member_id']:
        raise ValueError('Unresolved member row cannot be exported.')
    result = []
    for loan in item['loans']:
        amount = money(amounts.get(str(loan['id']), '') or '0')
        if amount == 0:
            continue
        current = current_loans.get(loan['id'])
        if not current or current['member_id'] != item['member_id'] or amount > money(current['balance']):
            raise ValueError('Loan balance or status changed; preview the payroll again.')
        result.append((current, amount))
    if sum((amount for _, amount in result), Decimal(0)) != money(item['amount']):
        raise ValueError('Allocated amounts must equal the member deduction.')
    return result


LOAN_HEADERS = ['loan_number', 'amount', 'payment_date', 'payment_method', 'counter_account', 'receipt_number', 'notes']
SAVINGS_HEADERS = ['member_number', 'employee_id', 'amount', 'month', 'date', 'counter_account', 'receipt_number', 'notes']
REVIEW_HEADERS = ['source_row', 'hr_name', 'hr_staff_number', 'matched_member_number', 'loan_deduction',
                  'savings_deduction', 'subscription_deduction', 'loan_exported', 'savings_exported',
                  'loan_status', 'savings_status', 'subscription_status']


def prepare_review(db, rows, month):
    members = [dict(m) for m in db.execute("SELECT id,member_number,employee_id,first_name || ' ' || last_name AS member_name FROM members ORDER BY first_name,last_name").fetchall()]
    items = match_payroll(rows, members, loan_book(db, month))
    for item in items:
        item['loans'] = [{k: loan[k] for k in ('id', 'loan_number', 'purpose', 'balance', 'expected', 'basis', 'allocation')} for loan in item['loans']]
        if item['member_id']:
            if money(item['amount']) > 0 and has_month_payment(db, item['member_id'], month):
                item['loan_error'] = 'Repayment already exists in this month; exclude loan deduction and review history.'
            if money(item['savings_amount']) > 0 and has_month_savings(db, item['member_id'], month):
                item['savings_error'] = 'Savings already exist in this month; exclude savings deduction and review history.'
    return items, members


def prepare_exports(db, items, choices, month, payment_date, account, username):
    current = {loan['id']: loan for loan in loan_book(db, month)}
    loans, savings, review = [], [], []
    for item in items:
        row = item['row']
        exclude_all = choices.get(f'exclude_{row}')
        exclude_loan = exclude_all or choices.get(f'exclude_loan_{row}')
        exclude_savings = exclude_all or choices.get(f'exclude_savings_{row}')
        loan_total, savings_total = Decimal(0), Decimal(0)
        if not exclude_all:
            loan_total, savings_total = money(item['amount']), money(item['savings_amount'])
        include_loan = loan_total > 0 and not exclude_loan
        include_savings = savings_total > 0 and not exclude_savings
        member = None
        if include_loan or include_savings:
            if not choices.get(f'confirm_{row}'):
                raise ValueError(f'Row {row}: confirm the member and allocation or exclude the deduction.')
            if item['error'] or not item['member_id']:
                raise ValueError(f'Row {row}: resolve the member match first.')
            member = db.execute('SELECT id,member_number,employee_id FROM members WHERE id=?', (item['member_id'],)).fetchone()
            if not member:
                raise ValueError(f'Row {row}: member no longer exists; review again.')
        loan_exported, savings_exported = Decimal(0), Decimal(0)
        if include_loan:
            if has_month_payment(db, item['member_id'], month):
                raise ValueError(f'Row {row}: repayment already exists in this month; review history.')
            amounts = {str(l['id']): choices.get(f'amount_{row}_{l["id"]}', '') for l in item['loans']}
            for loan in item['loans']:
                loan['allocation'] = amounts[str(loan['id'])]
            for loan, amount in validate_allocations(item, amounts, current):
                receipt = f'PAYROLL-LOAN/{month}/{item["member_id"]}/{loan["id"]}'
                if db.execute('SELECT id FROM repayments WHERE receipt_number=? AND reversed_at IS NULL', (receipt,)).fetchone():
                    raise ValueError(f'Row {row}: payroll loan receipt already posted.')
                loans.append([loan['loan_number'], str(amount), payment_date, 'salary_deduction', account,
                              receipt, f'Payroll {month}; approved by {username}'])
                loan_exported += amount
        if include_savings:
            if item['savings_error'] or has_month_savings(db, item['member_id'], month):
                raise ValueError(f'Row {row}: savings already posted or member identifier requires review.')
            number, staff = identifier(member['member_number']), identifier(member['employee_id'])
            if not number and not staff:
                raise ValueError(f'Row {row}: add a member or staff number before savings export.')
            # Ensure the importer will resolve exactly this member, not a duplicate ID.
            field, key = ('member_number', number) if number else ('employee_id', staff)
            matches = db.execute(f'SELECT id FROM members WHERE {field}=?', (key,)).fetchall()
            if len(matches) != 1 or matches[0]['id'] != member['id']:
                raise ValueError(f'Row {row}: savings import identifier is not unique.')
            receipt = f'PAYROLL-SAV/{month}/{member["id"]}'
            if db.execute('SELECT id FROM savings WHERE receipt_number=? AND reversed_at IS NULL', (receipt,)).fetchone():
                raise ValueError(f'Row {row}: payroll savings receipt already posted.')
            savings.append([number, staff, str(savings_total), month, payment_date, account,
                            receipt, f'Payroll {month}; approved by {username}'])
            savings_exported = savings_total
        review.append([row, item['source_name'], item['source_staff'],
                       identifier(member['member_number']) if member else item['member_number'],
                       item['amount'], item['savings_amount'], item['subscription_amount'],
                       str(loan_exported), str(savings_exported),
                       'Exported' if include_loan else 'Excluded / zero deduction',
                       'Exported' if include_savings else 'Excluded / zero deduction',
                       'Separate fee - NOT included in loan/savings imports'])
    if not loans and not savings:
        raise ValueError('No approved loan or savings deductions to export.')
    return loans, savings, review
