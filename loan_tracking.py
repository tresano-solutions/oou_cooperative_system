"""Agreed repayment schedule versus what the member has actually paid.

Read-only. For one loan it lays the agreed instalments (from the schedule the member
accepted, else recomputed from the loan terms) beside real, un-reversed repayments, so an
admin can see exactly where a member fell behind. A payment received by the end of the
due month counts as on time, because salary deductions land at month-end.
"""
import calendar
import json
from datetime import datetime
from decimal import Decimal

from delinquency import _add_months, _parse
from repayment_adapter import money
from utils import compute_loan_schedule

TOLERANCE = Decimal('0.005')


def _dec(value):
    try:
        return money(value if value not in (None, '') else 0)
    except ValueError:
        return Decimal('0.00')


def _month_end(day):
    return day.replace(day=calendar.monthrange(day.year, day.month)[1], hour=23, minute=59, second=59)


def is_migrated(loan):
    return (str(loan['loan_number'] or '').startswith('TALLY/')
            or 'migrat' in str(loan['notes'] if 'notes' in loan.keys() else '').lower())


def agreed_schedule(loan):
    """(list of {month, payment, principal, interest}, source label) or (None, reason)."""
    try:
        tenure = int(loan['tenure'] or 0)
    except (TypeError, ValueError):
        tenure = 0
    if tenure <= 0:
        return None, 'No tenure recorded for this loan.'
    snapshot = {}
    try:
        snapshot = json.loads(loan['repayment_schedule_snapshot'] or '{}')
    except (TypeError, ValueError):
        pass
    saved = snapshot.get('schedule') or []
    if len(saved) == tenure:
        return [{'month': i + 1, 'payment': _dec(r.get('payment')), 'principal': _dec(r.get('principal')),
                 'interest': _dec(r.get('interest'))} for i, r in enumerate(saved)], 'Schedule accepted by the member at application'
    if is_migrated(loan):
        return None, 'Migrated loan: no agreed schedule is on record.'
    try:
        if float(loan['amount'] or 0) > 0 and loan['interest_rate'] is not None:
            _, _, rows = compute_loan_schedule(loan['amount'], loan['interest_rate'], tenure,
                                               loan['interest_method'] or 'reducing_annual')
            if rows:
                return [{'month': r['month'], 'payment': _dec(r['payment']), 'principal': _dec(r['principal']),
                         'interest': _dec(r['interest'])} for r in rows], 'Recomputed from the loan terms (no saved schedule)'
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    total = _dec(loan['total_repayment'])
    if total > 0:
        each = money(total / tenure)
        rows = [{'month': i, 'payment': each, 'principal': Decimal('0.00'), 'interest': Decimal('0.00')}
                for i in range(1, tenure + 1)]
        rows[-1]['payment'] = total - each * (tenure - 1)
        return rows, 'Total repayable divided equally over the tenure'
    return None, 'No repayment terms recorded for this loan.'


def first_due(loan):
    start = _parse(loan['first_payment_date'] if 'first_payment_date' in loan.keys() else None)
    if start:
        return start
    base = _parse(loan['disbursement_date'] if 'disbursement_date' in loan.keys() else None) \
        or _parse(loan['date_applied'] if 'date_applied' in loan.keys() else None)
    return _add_months(base, 1) if base else None


def track_loan(loan, repayments, as_of=None):
    """Compare the agreed schedule with real payments for one loan.

    `repayments`: iterable of rows with amount/date, already excluding reversed ones.
    """
    as_of = as_of or datetime.now()
    pays = []
    for r in repayments:
        when = _parse(r['date'])
        if when is not None and when <= as_of:
            pays.append((when, _dec(r['amount'])))
    pays.sort(key=lambda p: p[0])
    paid_total = sum((a for _, a in pays), Decimal(0))
    result = {
        'rows': [], 'source': '', 'has_schedule': False, 'paid_total': paid_total,
        'last_payment': pays[-1] if pays else None, 'agreed_total': _dec(loan['total_repayment']),
        'scheduled_to_date': Decimal('0.00'), 'gap': Decimal('0.00'), 'due_count': 0,
        'status': 'No schedule', 'monthly': Decimal('0.00'), 'next_due': None, 'balance': _dec(loan['balance']),
        'balance_gap': None,
    }
    schedule, source = agreed_schedule(loan)
    result['source'] = source
    start = first_due(loan)
    if not schedule or start is None:
        return result
    result['has_schedule'] = True
    result['monthly'] = schedule[0]['payment']
    cumulative, previous_end = Decimal(0), None
    for index, item in enumerate(schedule):
        due = _add_months(start, index)
        end = _month_end(due)
        cumulative += item['payment']
        in_period = sum((a for w, a in pays if (previous_end is None or w > previous_end) and w <= end), Decimal(0))
        paid_by_end = sum((a for w, a in pays if w <= end), Decimal(0))
        if paid_total + TOLERANCE >= cumulative:
            status = 'Paid' if paid_by_end + TOLERANCE >= cumulative else 'Paid late'
        elif due > as_of:
            status = 'Upcoming'
        elif end >= as_of:
            status = 'Due'
        elif paid_total > cumulative - item['payment'] + TOLERANCE:
            status = 'Part-paid'
        else:
            status = 'Missed'
        result['rows'].append({
            'month': item['month'], 'due': due, 'payment': item['payment'], 'principal': item['principal'],
            'interest': item['interest'], 'cumulative': cumulative, 'paid_in_period': in_period,
            'paid_by_end': paid_by_end, 'gap_at_end': max(Decimal(0), cumulative - paid_by_end),
            'status': status, 'due_passed': due <= as_of})
        if end < as_of:
            # Counted as owed only once its grace (the end of the due month) has passed.
            result['due_count'] += 1
            result['scheduled_to_date'] = cumulative
        elif result['next_due'] is None:
            result['next_due'] = due
        previous_end = end
    result['agreed_total'] = cumulative
    result['gap'] = result['scheduled_to_date'] - paid_total  # positive = behind, negative = ahead
    behind = result['gap'] > TOLERANCE
    if loan['status'] == 'completed' or result['balance'] <= TOLERANCE:
        result['status'] = 'Completed'
    elif result['due_count'] == 0:
        result['status'] = 'Not started'
    elif behind:
        result['status'] = 'Behind'
    elif result['gap'] < -TOLERANCE:
        result['status'] = 'Ahead'
    else:
        result['status'] = 'On track'
    expected_balance = cumulative - paid_total
    if abs(expected_balance - result['balance']) > Decimal('1.00'):
        result['balance_gap'] = result['balance'] - expected_balance
    return result


def portfolio(db, as_of=None):
    """Track every active loan; one query for loans, one for payments."""
    as_of = as_of or datetime.now()
    loans = db.execute('''SELECT l.*, m.first_name || ' ' || m.last_name AS member_name, m.member_number,
        m.employee_id FROM loans l JOIN members m ON m.id = l.member_id
        WHERE l.status = 'active' ORDER BY member_name, l.id''').fetchall()
    payments = {}
    for r in db.execute('''SELECT r.loan_id, r.amount, r.date FROM repayments r JOIN loans l ON l.id = r.loan_id
            WHERE l.status = 'active' AND r.reversed_at IS NULL''').fetchall():
        payments.setdefault(r['loan_id'], []).append(r)
    rows = []
    for loan in loans:
        tracked = track_loan(loan, payments.get(loan['id'], []), as_of)
        rows.append({'loan': dict(loan), **tracked})
    return rows
