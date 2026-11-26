"""When a loan starts repaying.

Loans are repaid through the monthly payroll run, whatever day they were issued. A loan issued on
or before the payroll cut-off day is in that month's run; one issued after it starts the next month.
Only the President can move the start later. `first_payment_date` is stored as the last day of the
first payroll month, so a month's instalment is owed once the month has ended.
"""
import calendar
from datetime import datetime

CUTOFF_SETTING = 'loan_repayment_cutoff_day'
DEFAULT_CUTOFF_DAY = 15
MAX_DEFERRAL_MONTHS = 12


def parse_dt(value):
    """A datetime from what the database returns (a datetime on PostgreSQL, text on SQLite)."""
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).replace('T', ' ').split('+')[0].split('.')[0].strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def add_months(day, months):
    """`day` moved forward by whole months, keeping the day where the month allows it."""
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def month_end(day):
    return day.replace(day=calendar.monthrange(day.year, day.month)[1], hour=0, minute=0, second=0, microsecond=0)


def cutoff_day(db):
    """The payroll cut-off day (1-28) from Settings; falls back to the default if unset or invalid."""
    row = db.execute('SELECT value FROM settings WHERE key=?', (CUTOFF_SETTING,)).fetchone()
    try:
        day = int(float(row['value'])) if row and row['value'] not in (None, '') else DEFAULT_CUTOFF_DAY
    except (TypeError, ValueError):
        day = DEFAULT_CUTOFF_DAY
    return day if 1 <= day <= 28 else DEFAULT_CUTOFF_DAY


def first_repayment_date(issued_on, cutoff):
    """Month-end of the first payroll month for a loan issued on `issued_on`."""
    issued_on = parse_dt(issued_on) or datetime.now()
    first_month = issued_on if issued_on.day <= cutoff else add_months(issued_on.replace(day=1), 1)
    return month_end(first_month)


def deferred_start(current_first, target_month):
    """The new first-payment date for a deferral to `target_month` ('YYYY-MM'), or ValueError.

    It must be a later month than the current start and no more than a year after it."""
    try:
        target = datetime.strptime((target_month or '').strip(), '%Y-%m')
    except ValueError:
        raise ValueError('Choose the month repayment should start, for example 2027-01.')
    current_first = parse_dt(current_first)
    if (target.year, target.month) <= (current_first.year, current_first.month):
        raise ValueError(f'The new start must be later than the current start ({current_first:%B %Y}). '
                         'Only a later start can be set; repayment cannot be brought forward.')
    if (target.year, target.month) > (add_months(current_first, MAX_DEFERRAL_MONTHS).year,
                                      add_months(current_first, MAX_DEFERRAL_MONTHS).month):
        raise ValueError(f'Repayment can be deferred by at most {MAX_DEFERRAL_MONTHS} months.')
    return month_end(target)
