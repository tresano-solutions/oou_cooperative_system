"""Business rules for cooperative-financed asset sales."""

from decimal import Decimal, ROUND_HALF_UP


MONEY = Decimal('0.01')


def money(value):
    return Decimal(str(value or 0)).quantize(MONEY, rounding=ROUND_HALF_UP)


def finance_terms(selling_subtotal, annual_rate, tenure_months):
    """Return interest, total and monthly payment using annual flat rate.

    Delivery is deliberately absent: it is member-paid but never financed or
    interest-bearing. A 20% annual rate over six months therefore charges 10%.
    """
    principal = money(selling_subtotal)
    rate = Decimal(str(annual_rate or 0)) / Decimal('100')
    months = int(tenure_months or 0)
    if principal <= 0 or months <= 0 or months > 60 or rate < 0:
        raise ValueError('Invalid asset finance terms.')
    interest = money(principal * rate * Decimal(months) / Decimal('12'))
    total = money(principal + interest)
    monthly = money(total / Decimal(months))
    return float(interest), float(total), float(monthly)


def delivery_cleared(order):
    return (order['delivery_fee'] or 0) <= 0 or order['delivery_fee_status'] in {'paid', 'waived'}


def delivery_dispatch_allowed(order):
    return delivery_cleared(order) or order['delivery_fee_status'] == 'pay_on_delivery'


# --- Cancellation ---------------------------------------------------------------------------
# Until the cooperative has paid the supplier, a request can be closed without any accounting.
# After that the money is out, so cancelling needs a decision about the goods (see the blueprint).
CANCELLABLE_BEFORE_PURCHASE = ('sourcing_quote', 'awaiting_member_quote', 'submitted', 'approved_for_procurement')
CANCELLABLE_AFTER_PURCHASE = ('procured', 'ready_for_receipt')

# An officer may confirm delivery for a member only after the items have been out for this long,
# so the member has had a real chance to confirm for themselves.
OFFICER_CONFIRM_WAIT_DAYS = 3


def member_can_cancel(order):
    return order['status'] in CANCELLABLE_BEFORE_PURCHASE and not order['linked_loan_id']


def schedule_snapshot(selling_subtotal, annual_rate, tenure_months, purpose='Asset Purchase'):
    """The repayment schedule the member accepted, in the same shape loans store theirs.

    The last instalment absorbs rounding so the schedule always adds up to exactly the total the
    member agreed to repay."""
    from utils import compute_loan_schedule
    _, total, _ = finance_terms(selling_subtotal, annual_rate, tenure_months)
    monthly, _, rows = compute_loan_schedule(selling_subtotal, annual_rate, tenure_months, 'flat')
    schedule = [dict(r) for r in rows]
    if schedule:
        earlier = round(sum(r['payment'] for r in schedule[:-1]), 2)
        schedule[-1]['payment'] = round(total - earlier, 2)
    return {
        'principal': round(float(selling_subtotal), 2), 'purpose': purpose, 'tenure': int(tenure_months),
        'interest_rate': float(annual_rate), 'interest_method': 'flat',
        'monthly_payment': round(float(monthly), 2), 'total_repayment': total, 'schedule': schedule,
    }
