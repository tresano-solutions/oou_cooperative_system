"""Affordability review for an asset request.

It never decides: it gathers what an approver would otherwise have to look up by hand (the member's
existing loan repayments, arrears, savings-based limit and other open requests) and says plainly what
needs a closer look. The committee still approves or declines.
"""
import delinquency
import loan_limits

AFFORDABILITY_LIMIT = 0.4          # of monthly salary, counting loans already being repaid
CLOSED_REQUESTS = ('cancelled', 'rejected', 'received')


def review(db, member, new_monthly, new_total, repayment_method='', exclude_order_id=None):
    """(status, reason): status is 'eligible' or 'review'; reason lists every point to look at."""
    flags = []
    salary = float(member['annual_salary'] or 0) / 12
    loans = db.execute("SELECT * FROM loans WHERE member_id=? AND status='active' AND balance>0",
                       (member['id'],)).fetchall()
    existing = sum(float(l['total_repayment'] or 0) / max(int(l['tenure'] or 1), 1) for l in loans)
    if salary <= 0:
        flags.append('No salary is recorded, so affordability cannot be checked.')
    else:
        ratio = (existing + float(new_monthly)) / salary
        if ratio > AFFORDABILITY_LIMIT:
            flags.append(f'Repayments would be {ratio:.0%} of monthly salary (limit {AFFORDABILITY_LIMIT:.0%}): '
                         f'₦{existing:,.2f} on existing loans plus ₦{float(new_monthly):,.2f} on this request.')
    arrears = sum(delinquency.loan_delinquency(l)['arrears'] for l in loans)
    if arrears > 0.005:
        flags.append(f'Existing loans are ₦{arrears:,.2f} in arrears.')
    outstanding = sum(float(l['balance'] or 0) for l in loans)
    savings_limit = loan_limits.eligible_amount(db, member['total_savings'] if 'total_savings' in member.keys() else 0)
    if outstanding + float(new_total) > savings_limit:
        flags.append(f'Owing ₦{outstanding:,.2f} now plus ₦{float(new_total):,.2f} here is above what the member\'s '
                     f'savings support (₦{savings_limit:,.2f}).')
    placeholders = ','.join('?' for _ in CLOSED_REQUESTS)
    others = db.execute(f'''SELECT COUNT(*) FROM asset_orders WHERE member_id=? AND status NOT IN ({placeholders})
        AND id <> ?''', (member['id'], *CLOSED_REQUESTS, exclude_order_id or 0)).fetchone()[0]
    if others:
        flags.append(f'{others} other asset request(s) are already open for this member.')
    if flags:
        return 'review', ' | '.join(flags)
    note = ('Within the affordability, arrears and savings checks.')
    if repayment_method == 'salary_deduction':
        note += ' Repayment is by salary deduction: confirm the deduction with HR before the loan starts.'
    return 'eligible', note
