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


ALIASES = {
    'member_number': ('member_number', 'member_no', 'membership_number'),
    'employee_id': ('employee_id', 'staff_number', 'staff_no', 'employee_number', 'staff_id'),
    'name': ('name', 'member_name', 'employee_name', 'loan_name'),
    'amount': ('amount', 'deduction', 'loan_repayment', 'repayment_amount', 'total_deduction'),
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
                if any(v is not None for v in row):
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
    if 'amount' not in columns or not set(columns).intersection(('member_number', 'employee_id', 'name')):
        raise ValueError('Include amount and member_number, staff_number or member_name columns.')
    rows = []
    for number, values in enumerate(matrix[1:], 2):
        if not any(v not in (None, '') for v in values):
            continue
        row = {key: identifier(values[i]) if i < len(values) else '' for key, i in columns.items()}
        # Totals are deliberately rejected, not silently credited to a member.
        row['row'] = number
        rows.append(row)
    return rows


def match_payroll(rows, members, book):
    result, seen = [], set()
    for row in rows:
        item = {'row': row['row'], 'name': row.get('name', ''), 'amount': row.get('amount', ''),
                'member_id': None, 'loans': [], 'error': '', 'match': ''}
        try:
            amount = money(row.get('amount', ''))
            if amount <= 0:
                raise ValueError('Deduction must be greater than zero.')
            item['amount'] = str(amount)
            supplied = [(key, row.get(key)) for key in ('member_number', 'employee_id') if row.get(key)]
            candidates = list(members)
            if supplied:
                for key, value in supplied:
                    candidates = [m for m in candidates if identifier(m.get(key)) == value]
                item['match'] = 'Member / staff number'
            else:
                candidates = [m for m in candidates if name_key(m['member_name']) == name_key(row.get('name')) and name_key(row.get('name'))]
                item['match'] = 'Exact name (review identity)'
            if len(candidates) != 1:
                raise ValueError('No unique member match. Correct the identifier in the source file.')
            member = candidates[0]
            if row.get('name') and name_key(member['member_name']) != name_key(row['name']):
                raise ValueError('Name and identifier disagree. Verify the member.')
            if member['id'] in seen:
                raise ValueError('Duplicate member row; consolidate the deduction before matching.')
            seen.add(member['id'])
            item.update(member_id=member['id'], name=member['member_name'])
            loans = [dict(l) for l in book if l['member_id'] == member['id']]
            if not loans:
                raise ValueError('No active loan with an outstanding balance.')
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
    if item['error'] or not item['member_id']:
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
