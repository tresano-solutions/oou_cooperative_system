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
