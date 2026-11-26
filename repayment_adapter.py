"""Read-only payroll matching and allocation preparation; never posts money."""
import csv
import io
import json
import re
import unicodedata
import zipfile
from datetime import datetime
from collections import Counter
from difflib import SequenceMatcher
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


def month_repayments(db, member_id, month):
    """Repayments already recorded for the member this month. A member can legitimately pay more
    than once a month, so this informs the review; duplicates are stopped by receipt instead."""
    rows = db.execute('''SELECT r.amount, r.payment_method, r.receipt_number FROM repayments r
        JOIN loans l ON l.id=r.loan_id WHERE l.member_id=? AND r.reversed_at IS NULL
        AND SUBSTR(CAST(r.date AS TEXT),1,7)=?''', (member_id, month)).fetchall()
    payroll = [r for r in rows if str(r['payment_method'] or '').lower() == 'salary_deduction'
               or str(r['receipt_number'] or '').startswith('PAYROLL-LOAN')]
    total = lambda found: sum((money(r['amount']) for r in found), Decimal(0))
    return {'count': len(rows), 'total': total(rows), 'payroll_count': len(payroll), 'payroll_total': total(payroll)}


def month_savings(db, member_id, month):
    """What the member already has in this month. Several deposits a month are normal
    (salary deduction plus voluntary savings), so this informs the review, never blocks it."""
    rows = db.execute('''SELECT amount, COALESCE(share_capital,0) AS share_capital, payment_type, receipt_number
        FROM savings WHERE member_id=? AND reversed_at IS NULL
        AND (amount>0 OR COALESCE(share_capital,0)>0) AND month=?''', (member_id, month)).fetchall()
    salary = [r for r in rows if str(r['payment_type'] or '').lower() == 'salary']
    total = lambda found: sum((money(r['amount']) + money(r['share_capital']) for r in found), Decimal(0))
    return {'count': len(rows), 'total': total(rows), 'salary_count': len(salary), 'salary_total': total(salary)}


ALIASES = {
    'member_number': ('member_number', 'member_no', 'membership_number'),
    'employee_id': ('employee_id', 'staff_number', 'staff_no', 'employee_number', 'staff_id'),
    'name': ('name', 'full_name', 'member_name', 'employee_name', 'loan_name'),
    'amount': ('amount', 'deduction', 'loan_repayment', 'repayment_amount', 'total_deduction', 'cooperative_loan'),
    'savings_amount': ('cooperative_savings', 'savings', 'savings_amount'),
    'subscription_amount': ('cooperative_subscription', 'subscription', 'subscription_fee'),
}


# HR rounds deductions; a loan settled at its balance may be this much below the deduction.
ROUNDING_TOLERANCE = Decimal('1.00')


class PayrollRows(list):
    """Parsed HR rows; `control_verified` is True when a TOTAL line was reconciled."""
    control_verified = False
    control_warnings = ()
    columns = None  # {field: header text actually used}


class NeedsMapping(ValueError):
    """The headings were not recognised; carries what the user needs to map them."""

    def __init__(self, message, headers, guesses, sample):
        super().__init__(message)
        self.headers, self.guesses, self.sample = headers, guesses, sample


TOTAL_LABELS = ('TOTAL', 'GRAND TOTAL', 'SUB TOTAL', 'SUBTOTAL')
FIELD_LABELS = {
    'employee_id': 'Staff / Employee number', 'member_number': 'Member number', 'name': 'Full name',
    'amount': 'Loan deduction', 'savings_amount': 'Savings deduction', 'subscription_amount': 'Subscription (optional)',
}
_GUESS_PATTERNS = (
    ('name', r'name'),
    ('employee_id', r'staff|employee|emp_?(no|num|id)|payroll|ippis|file_?no'),
    ('member_number', r'member|membership|coop_?no'),
    ('amount', r'loan|repay'),
    ('savings_amount', r'sav'),
    ('subscription_amount', r'subscri|dues|levy'),
)


def guess_columns(headers):
    """Best-guess field -> column index from free-text headings (user confirms it)."""
    taken, guesses = set(), {}
    for field, pattern in _GUESS_PATTERNS:
        for i, header in enumerate(headers):
            key = re.sub(r'[^a-z0-9]+', '_', str(header or '').strip().lower()).strip('_')
            if i in taken or not key or not re.search(pattern, key):
                continue
            if field in ('employee_id', 'member_number') and 'name' in key:
                continue
            guesses[field] = i
            taken.add(i)
            break
    return guesses


def _load_matrix(raw, filename):
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
                if len(matrix) >= 2021:
                    raise ValueError('Maximum 2,000 payroll rows.')
                matrix.append(row)
        finally:
            book.close()
    elif filename.lower().endswith('.csv'):
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('cp1252', errors='replace')  # Excel "CSV" on Windows
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t')
        except csv.Error:
            dialect = csv.excel
        matrix = list(csv.reader(io.StringIO(text), dialect))
    else:
        raise ValueError('Upload an Excel .xlsx or CSV file.')
    header_index = next(
        (i for i, row in enumerate(matrix[:21])
         if any(value is not None and str(value).strip() for value in row)),
        None,
    )
    if header_index is None or len(matrix) - header_index > 2001:
        raise ValueError('Upload between 1 and 2,000 payroll rows.')
    return matrix, header_index


def read_payroll(raw, filename, mapping=None):
    """Parse the HR file. `mapping` ({field: column index}) overrides heading detection."""
    matrix, header_index = _load_matrix(raw, filename)
    shown = [str(v).strip() if v is not None else '' for v in matrix[header_index]]
    headers = [re.sub(r'[^a-z0-9]+', '_', h.lower()).strip('_') for h in shown]
    columns = {}
    if mapping:
        for key, index in mapping.items():
            if key not in FIELD_LABELS or index in (None, ''):
                continue
            index = int(index)
            if not 0 <= index < len(headers):
                raise ValueError('Choose a column from the list.')
            columns[key] = index
        if len(set(columns.values())) != len(columns):
            raise ValueError('Each column can be used for only one field.')
    else:
        for key, aliases in ALIASES.items():
            indices = [i for i, h in enumerate(headers) if h in aliases]
            if len(indices) > 1:
                raise ValueError('Duplicate column for ' + key)
            if indices:
                columns[key] = indices[0]
    if (not set(columns).intersection(('amount', 'savings_amount'))
            or not set(columns).intersection(('member_number', 'employee_id', 'name'))):
        sample = [[str(v if v is not None else '')[:40] for v in row[:len(headers)]]
                  for row in matrix[header_index + 1:header_index + 4]]
        raise NeedsMapping('Some columns were not recognised. Tell us which column is which.',
                           shown, {**guess_columns(shown), **columns}, sample)
    rows = []
    for number, values in enumerate(matrix[header_index + 1:], header_index + 2):
        if not any(v not in (None, '') for v in values):
            continue
        row = {key: identifier(values[i]) if i < len(values) else '' for key, i in columns.items()}
        row['row'] = number
        rows.append(row)
    details, warnings, verified = remove_verified_totals(rows)
    result = PayrollRows(details)
    result.control_verified = bool(verified)
    result.control_warnings = warnings
    result.columns = {key: (shown[i] or f'Column {i + 1}') for key, i in columns.items()}
    return result


def remove_verified_totals(rows):
    """Split off a trailing HR TOTAL line. Returns (member rows, warnings, verified).

    A total that disagrees with the member rows is reported, never refused. A trailing line
    that has no identity and is not labelled as a total stays a row, so it cannot vanish."""
    details = list(rows)
    footers = []
    while details:
        row = details[-1]
        label = str(row.get('name', '')).strip().upper()
        if (row.get('member_number') or row.get('employee_id')
                or (label and label not in TOTAL_LABELS)):
            break
        footers.insert(0, details.pop())
    if not footers or len(details) < 2:
        return list(rows), [], False
    keys = ('amount', 'savings_amount', 'subscription_amount')
    totals = {k: sum((_amount(r.get(k)) for r in details), Decimal(0)) for k in keys}
    kept, warnings, verified = [], [], True
    for row in footers:
        labelled = str(row.get('name', '')).strip().upper() in TOTAL_LABELS
        label_keys = [k for k in keys if str(row.get(k, '')).strip().upper() in TOTAL_LABELS]
        if label_keys:   # "TOTAL" sits in a deduction column with the grand total beside it
            numbers = [_amount(row[k]) for k in keys if row.get(k) and k not in label_keys]
            grand = sum(totals.values())
            if len(numbers) != 1 or numbers[0] != grand:
                verified = False
                warnings.append(f'Row {row["row"]}: the HR grand total'
                                f'{f" ({numbers[0]:,.2f})" if len(numbers) == 1 else ""} differs from the member rows ({grand:,.2f}).')
        elif any(row.get(k) for k in keys) and all(_amount(row.get(k)) == totals[k] for k in keys):
            continue     # a verified control total
        elif labelled:
            verified = False
            theirs = {k: _amount(row.get(k)) for k in keys if row.get(k)}
            diffs = [f'{k.replace("_", " ")}: HR {v:,.2f} vs rows {totals[k]:,.2f}' for k, v in theirs.items() if v != totals[k]]
            warnings.append(f'Row {row["row"]}: the HR TOTAL line differs from the member rows ({"; ".join(diffs)}).')
        else:
            kept.append(row)   # no identity, not a total: keep it so it is visible as an unmatched row
            verified = False
            warnings.append(f'Row {row["row"]} has amounts but no staff number or name, and is not a matching total. '
                            'It is kept as an unmatched row so nothing disappears.')
    dropped = len(footers) - len(kept)
    return details + kept, warnings, verified and dropped > 0


def suggest_members(name, members, limit=3):
    """Ranked look-alike members for an unmatched HR name (never auto-applied)."""
    key = name_key(name)
    if not key:
        return []
    tokens = set(key.split())
    scored = []
    for m in members:
        other = name_key(m['member_name'])
        if not other:
            continue
        other_tokens = set(other.split())
        shared = len(tokens & other_tokens)
        if not shared:
            continue
        overlap = shared / min(len(tokens), len(other_tokens))
        ratio = SequenceMatcher(None, key, other).ratio()
        score = max(ratio, overlap * 0.85 if shared >= 2 else 0)
        if score >= 0.6:
            scored.append((score, m))
    scored.sort(key=lambda pair: -pair[0])
    return [{'id': m['id'], 'member_name': m['member_name'], 'member_number': m.get('member_number'),
             'employee_id': m.get('employee_id'), 'score': round(score * 100)}
            for score, m in scored[:limit]]


def _allocate(item, book):
    """Suggest a loan split and compare the HR deduction with what was expected."""
    amount = money(item['amount'])
    loans = [dict(l) for l in book if l['member_id'] == item['member_id']]
    if not loans:
        item['loan_error'] = 'No active loan with an outstanding balance.'
        return
    for loan in loans:
        loan['balance'] = str(money(loan['balance']))
        loan['allocation'] = ''
    balances = sum((money(l['balance']) for l in loans), Decimal(0))
    known = all(l['expected'] != '' for l in loans)
    expected = sum((money(l['expected']) for l in loans), Decimal(0)) if known else None
    rounding = Decimal(0) < amount - balances <= ROUNDING_TOLERANCE
    if len(loans) == 1 and amount <= money(loans[0]['balance']):
        loans[0]['allocation'] = str(amount)
    elif balances == amount or rounding:
        for loan in loans:
            loan['allocation'] = loan['balance']
    elif known and expected == amount:
        for loan in loans:
            loan['allocation'] = loan['expected']
    item['loan_expected'] = str(expected) if expected is not None else ''
    if expected is None:
        item['loan_flag'], item['loan_variance'] = 'unknown', ''
    else:
        variance = amount - expected
        item['loan_variance'] = str(variance)
        item['loan_flag'] = 'match' if abs(variance) < Decimal('0.01') else ('short' if variance < 0 else 'over')
    prefilled = all(l['allocation'] != '' for l in loans)
    if rounding:
        item['loan_flag'] = 'match'
        note = (f'HR deducted {amount:,.2f}, which is {amount - balances:,.2f} more than the outstanding balance of '
                f'{balances:,.2f}. The loan is settled at its balance and the difference appears as a rounding gap.')
    elif amount > balances:
        item['loan_flag'] = 'over'
        note = 'HR deducted more than the total outstanding. Verify the excess with HR.'
    elif item['loan_flag'] == 'short':
        note = (f'HR deducted {amount:,.2f}, which is {abs(Decimal(item["loan_variance"])):,.2f} '
                f'below the expected {expected:,.2f}. The shortfall stays as arrears.')
    elif item['loan_flag'] == 'over':
        note = (f'HR deducted {amount:,.2f}, which is {Decimal(item["loan_variance"]):,.2f} '
                f'above the expected {expected:,.2f}. Verify with HR.')
    elif prefilled:
        note = 'Suggested allocations match the HR deduction. Review before confirming.'
    elif not known:
        note = 'At least one expected payment is unknown. Verify the split with HR.'
    else:
        note = 'Enter how the HR deduction splits across the loans.'
    if item['loan_flag'] in ('short', 'over') and prefilled and not rounding:
        note += ' Allocations are pre-filled from the HR amount; review before confirming.'
    item['allocation_note'] = note
    item['loans'] = loans


def hr_key(row):
    """Stable identity of an HR line: its staff/member number, else its normalised name."""
    number = identifier(row.get('employee_id')) or identifier(row.get('member_number'))
    if number:
        return 'id:' + number
    key = name_key(row.get('name'))
    return 'name:' + key if key else ''


def odd_staff_numbers(rows):
    """Rows whose staff number length differs from the clear majority (typos, truncation)."""
    ids = [(r['row'], identifier(r.get('employee_id'))) for r in rows if identifier(r.get('employee_id'))]
    if len(ids) < 5:
        return {}, 0
    length, count = Counter(len(i) for _, i in ids).most_common(1)[0]
    if count / len(ids) < 0.8:
        return {}, 0
    return {n: i for n, i in ids if len(i) != length}, length


def load_remembered(db):
    return {r['hr_key']: r['member_id'] for r in
            db.execute('SELECT hr_key, member_id FROM payroll_identity_map').fetchall()}


def remember_matches(db, items, choices, user_id):
    """Save corrections the user made and then confirmed, so next month needs none."""
    saved = 0
    for item in items:
        row = item['row']
        if (not item.get('manual') or not item.get('member_id') or not item.get('source_key')
                or not choices.get(f'confirm_{row}') or choices.get(f'exclude_{row}')):
            continue
        db.execute('''INSERT INTO payroll_identity_map (hr_key, member_id, hr_label, created_by)
            VALUES (?,?,?,?) ON CONFLICT(hr_key) DO UPDATE SET member_id=excluded.member_id,
            hr_label=excluded.hr_label, updated_at=CURRENT_TIMESTAMP''',
                   (item['source_key'], item['member_id'], str(item['source_name'])[:120], user_id))
        saved += 1
    return saved


def match_payroll(rows, members, book, remembered=None):
    result, by_member = [], {}
    remembered = remembered or {}
    odd_ids, id_length = odd_staff_numbers(rows)
    for row in rows:
        item = {'row': row['row'], 'name': row.get('name', ''), 'amount': row.get('amount', ''),
                'source_name': row.get('name', ''), 'source_staff': row.get('employee_id', ''),
                'savings_amount': row.get('savings_amount', '0'),
                'subscription_amount': row.get('subscription_amount', '0'),
                'member_id': None, 'member_number': '', 'employee_id': '',
                'loans': [], 'error': '', 'loan_error': '', 'savings_error': '', 'savings_note': '', 'loan_note': '', 'match': '',
                'merged_rows': [], 'suggestions': [], 'loan_expected': '', 'loan_variance': '',
                'loan_flag': '', 'allocation_note': '', 'merge_note': '', 'state': 'attention',
                'source_key': hr_key(row), 'manual': bool(row.get('selected_member_id')), 'remembered': False,
                'id_warning': ''}
        if row['row'] in odd_ids:
            item['id_warning'] = (f'Staff number {odd_ids[row["row"]]} has {len(odd_ids[row["row"]])} digits but most have '
                                  f'{id_length}. Check it is correct before confirming.')
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
            remembered_id = remembered.get(item['source_key']) if not manual else None
            used_memory = False
            if len(candidates) != 1 and remembered_id:
                candidates = [m for m in members if m['id'] == remembered_id]
                used_memory = len(candidates) == 1
            if len(candidates) != 1:
                item['suggestions'] = suggest_members(row.get('name'), members) if row.get('name') else []
                raise ValueError('No unique member match. Select the correct member for review.')
            member = candidates[0]
            if (not manual and not used_memory and remembered_id and member['id'] != remembered_id
                    and any(m['id'] == remembered_id for m in members)):
                member = next(m for m in members if m['id'] == remembered_id)
                used_memory = True
            if used_memory:
                item['match'] = 'Remembered from a previous month; confirm identity'
                item['remembered'] = True
            if not manual and not used_memory and row.get('name') and name_key(member['member_name']) != name_key(row['name']):
                item['identity_candidate'] = {key: member.get(key) for key in
                                              ('id', 'member_name', 'member_number', 'employee_id')}
                raise ValueError('Name and identifier disagree. Select the verified member for review.')
            if member['id'] in by_member:
                # HR often lists a member twice (e.g. one line per loan): add them up.
                first = by_member[member['id']]
                for key, value in (('amount', amount), ('savings_amount', savings),
                                   ('subscription_amount', subscription)):
                    first[key] = str(money(first[key]) + value)
                first['merged_rows'].append(row['row'])
                continue
            item.update(member_id=member['id'], name=member['member_name'],
                        member_number=identifier(member.get('member_number')),
                        employee_id=identifier(member.get('employee_id')))
            by_member[member['id']] = item
            if savings and not (item['member_number'] or item['employee_id']):
                item['savings_error'] = 'Member has no member/staff number for savings import; update the member record.'
        except ValueError as exc:
            item['error'] = str(exc)
        result.append(item)
    for item in result:
        if item['member_id'] and not item['error']:
            if item['merged_rows']:
                rows_text = ', '.join(str(n) for n in [item['row']] + item['merged_rows'])
                item['merge_note'] = f'Rows {rows_text} are the same member and were added together.'
            if money(item['amount']):
                _allocate(item, book)
    return result


def _amount(value):
    """A non-negative amount, or zero when the text is not a usable number."""
    try:
        return money(value if value not in (None, '') else '0')
    except ValueError:
        return Decimal('0.00')


def allocation_lines(item, amounts):
    """(loan, amount) for every loan the user gave a positive allocation. No judgement here:
    the Bulk Repayments importer decides what to do with balances, status and duplicates."""
    lines = []
    for loan in item['loans']:
        amount = _amount(amounts.get(str(loan['id']), ''))
        if amount > 0:
            lines.append((loan, amount))
    return lines


LOAN_HEADERS = ['loan_number', 'amount', 'payment_date', 'payment_method', 'counter_account', 'receipt_number', 'notes']
SAVINGS_HEADERS = ['member_number', 'employee_id', 'amount', 'month', 'date', 'counter_account', 'receipt_number', 'notes']
REVIEW_HEADERS = ['source_row', 'hr_name', 'hr_staff_number', 'matched_member_number', 'loan_deduction',
                  'savings_deduction', 'subscription_deduction', 'loan_exported', 'savings_exported',
                  'loan_status', 'savings_status', 'subscription_status', 'loan_gap', 'savings_gap', 'warnings']


def prepare_review(db, rows, month):
    members = [dict(m) for m in db.execute("SELECT id,member_number,employee_id,first_name || ' ' || last_name AS member_name FROM members ORDER BY first_name,last_name").fetchall()]
    items = match_payroll(rows, members, loan_book(db, month), load_remembered(db))
    for item in items:
        item['state'] = 'attention'
        item['loans'] = [{k: loan[k] for k in ('id', 'loan_number', 'purpose', 'balance', 'expected', 'basis', 'allocation')} for loan in item['loans']]
        if item['member_id']:
            if money(item['amount']) > 0:
                existing = month_repayments(db, item['member_id'], month)
                if existing['payroll_count']:
                    item['loan_note'] = (f"A salary-deduction repayment of {existing['payroll_total']:,.2f} is already recorded for {month}. "
                                         'Confirm this payroll has not been posted before adding another.')
                elif existing['count']:
                    item['loan_note'] = (f"Member already has {existing['count']} repayment(s) totalling {existing['total']:,.2f} in {month}. "
                                         'This payroll deduction will be added as a further repayment.')
            if money(item['savings_amount']) > 0:
                existing = month_savings(db, item['member_id'], month)
                if existing['salary_count']:
                    item['savings_note'] = (f"A salary deduction of {existing['salary_total']:,.2f} is already recorded for {month}. "
                                            'Confirm this payroll has not been posted before adding another.')
                elif existing['count']:
                    item['savings_note'] = (f"Member already has {existing['count']} saving(s) totalling {existing['total']:,.2f} in {month}. "
                                            'This payroll deduction will be added as a further deposit.')
        if item['error'] or item['loan_error'] or item['savings_error']:
            item['state'] = 'attention'
        elif (item.get('loan_flag') in ('short', 'over', 'unknown') or item['merged_rows'] or item['id_warning']
              or item['savings_note'] or item['loan_note']):
            item['state'] = 'check'
        else:
            item['state'] = 'ready'
    return items, members


def _gap(deducted, exported):
    """HR deduction minus what the files carry; unreadable amounts count as zero."""
    return _amount(deducted) - exported


def _warnings(item):
    """Everything the review flagged for this row, kept with the reconciliation for the record."""
    parts = [item.get(k) for k in ('error', 'loan_error', 'savings_error', 'id_warning', 'merge_note',
                                   'loan_note', 'savings_note', 'allocation_note')]
    return ' | '.join(str(p) for p in parts if p)


def prepare_exports(db, items, choices, month, payment_date, account, username):
    """Turn the confirmed review into upload files. This never refuses a row: duplicates, balances,
    loan status and identifiers are the importers' job. Rows that cannot be turned into a line
    (no member, not confirmed, excluded) are left out and reported in the reconciliation."""
    loans, savings, review = [], [], []
    for item in items:
        row = item['row']
        exclude_all = choices.get(f'exclude_{row}')
        exclude_loan = exclude_all or choices.get(f'exclude_loan_{row}')
        exclude_savings = exclude_all or choices.get(f'exclude_savings_{row}')
        hr_loan, hr_savings = _amount(item['amount']), _amount(item['savings_amount'])
        loan_status = savings_status = 'Excluded / zero deduction'
        loan_exported, savings_exported = Decimal(0), Decimal(0)
        member = None
        if not exclude_all and (hr_loan > 0 or hr_savings > 0):
            if item.get('error') or not item.get('member_id'):
                loan_status = savings_status = 'Unmatched - NOT exported'
            elif not choices.get(f'confirm_{row}'):
                loan_status = savings_status = 'Not confirmed - NOT exported'
            else:
                member = db.execute('SELECT id,member_number,employee_id FROM members WHERE id=?',
                                    (item['member_id'],)).fetchone()
                if not member:
                    loan_status = savings_status = 'Member no longer exists - NOT exported'
        if member and hr_loan > 0 and not exclude_loan:
            amounts = {str(l['id']): choices.get(f'amount_{row}_{l["id"]}', '') for l in item['loans']}
            for loan in item['loans']:
                loan['allocation'] = amounts[str(loan['id'])]
            for loan, amount in allocation_lines(item, amounts):
                receipt = f'PAYROLL-LOAN/{month}/{item["member_id"]}/{loan["id"]}'
                loans.append([loan['loan_number'], str(amount), payment_date, 'salary_deduction', account,
                              receipt, f'Payroll {month}; approved by {username}'])
                loan_exported += amount
            loan_status = 'Exported' if loan_exported else 'No loan allocation - NOT exported'
        elif member and hr_loan > 0:
            loan_status = 'Excluded / zero deduction'
        if member and hr_savings > 0 and not exclude_savings:
            number, staff = identifier(member['member_number']), identifier(member['employee_id'])
            receipt = f'PAYROLL-SAV/{month}/{member["id"]}'
            savings.append([number, staff, str(hr_savings), month, payment_date, account,
                            receipt, f'Payroll {month}; approved by {username}'])
            savings_exported = hr_savings
            savings_status = 'Exported'
        elif member and hr_savings > 0:
            savings_status = 'Excluded / zero deduction'
        review.append([row, item['source_name'], item['source_staff'],
                       identifier(member['member_number']) if member else item.get('member_number', ''),
                       item['amount'], item['savings_amount'], item['subscription_amount'],
                       str(loan_exported), str(savings_exported), loan_status, savings_status,
                       'Separate fee - NOT included in loan/savings imports',
                       str(_gap(item['amount'], loan_exported)), str(_gap(item['savings_amount'], savings_exported)),
                       _warnings(item)])
    return loans, savings, review
