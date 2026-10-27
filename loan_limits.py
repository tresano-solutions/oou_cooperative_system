"""Application limits shared by officer, member portal and mobile routes."""
from decimal import Decimal, InvalidOperation

LOAN_TYPES = {
    'Regular': 'regular', 'Housing': 'housing', 'Emergency': 'emergency',
    'Asset Purchase': 'asset', 'School Fees': 'school_fees',
}
DEFAULTS = {'max_loan_amount': '0', **{
    f'max_tenure_{suffix}': '0' for suffix in LOAN_TYPES.values()
}, **{
    f'max_loan_amount_{suffix}': '0' for suffix in LOAN_TYPES.values()
}}
DEFAULTS.update(loan_catalog='standard')


def products(db):
    values = {r['key']: r['value'] for r in db.execute('SELECT key, value FROM settings').fetchall()}
    smt = values.get('loan_catalog') == 'smt'
    defaults = dict(regular=11, housing=9, emergency=10, asset=10, school_fees=9)
    result = {}
    for name, suffix in LOAN_TYPES.items():
        if smt and suffix in ('housing', 'emergency'):
            continue
        result[name] = {
            'label': {'asset': 'Asset loan', 'school_fees': 'Back to school'}.get(suffix, name) if smt else name,
            'rate': float(values.get('interest_' + suffix, defaults[suffix])),
            'method': values.get('interest_method_' + suffix, 'flat' if suffix == 'school_fees' else 'reducing_annual'),
        }
    if smt:
        result['Regular'].update(rate=7.5, method='flat_term', tiers=[
            {'max_months': 12, 'rate': 7.5}, {'max_months': 24, 'rate': 9.0}])
        result = {name: result[name] for name in ('Regular', 'School Fees', 'Asset Purchase')}
    return result


def terms(db, purpose, tenure):
    product = products(db).get(purpose)
    if product is None:
        raise ValueError('Select an available loan type.')
    rate = product['rate']
    if product.get('tiers'):
        tier = next((t for t in product['tiers'] if tenure <= t['max_months']), None)
        if tier is None:
            raise ValueError('Maximum tenure for Regular is 24 months.')
        rate = tier['rate']
    return rate, product['method']


def validate_settings(values):
    if values.get('loan_catalog', 'standard') not in ('standard', 'smt'):
        raise ValueError('Select a valid loan catalogue.')
    for key in (set(DEFAULTS) - {'loan_catalog'}) | {'max_tenure_months'}:
        if key not in values:
            continue
        try:
            value = Decimal(str(values[key]))
        except (InvalidOperation, ValueError):
            raise ValueError(f'Invalid loan limit: {key}.')
        if not value.is_finite() or value < 0:
            raise ValueError('Loan limits must be finite, non-negative numbers.')
        if not key.startswith('max_loan_amount'):
            minimum = 1 if key == 'max_tenure_months' else 0
            if value != value.to_integral_value() or not minimum <= value <= 60:
                raise ValueError('Loan tenure must be whole months, up to 60.')
        elif value != value.quantize(Decimal('0.01')):
            raise ValueError('Maximum loan amount accepts at most two decimal places.')


def multiplier(db):
    """How many times their savings a member may borrow.

    Read from the 'loan_multiplier' setting so a cooperative can change it;
    defaults to 2, which is what the code used to hard-code.
    """
    try:
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'loan_multiplier'").fetchone()
        value = float(row['value']) if row and row['value'] else 2.0
    except (TypeError, ValueError, AttributeError):
        return 2.0
    return value if value > 0 else 2.0


def limits(db):
    values = dict(DEFAULTS, max_tenure_months='18')
    for row in db.execute('SELECT key, value FROM settings').fetchall():
        if row['key'] in values:
            values[row['key']] = row['value']
    validate_settings(values)
    general = int(values['max_tenure_months'])
    general_cap = float(values['max_loan_amount'])
    amounts = {}
    available = products(db)
    for name in available:
        suffix = LOAN_TYPES[name]
        type_cap = float(values[f'max_loan_amount_{suffix}'])
        caps = [cap for cap in (general_cap, type_cap) if cap > 0]
        amounts[name] = min(caps) if caps else 0
    return {
        'max_amount': float(values['max_loan_amount']),
        'amounts': amounts,
        'multiplier': multiplier(db),
        'products': available,
        'tenures': {name: (24 if values['loan_catalog'] == 'smt' and name == 'Regular'
                          else min(general, int(values[f'max_tenure_{LOAN_TYPES[name]}']) or general))
                    for name in available},
    }


def application_error(db, purpose, amount, tenure):
    policy = limits(db)
    if purpose not in policy['tenures']:
        return 'Select a valid loan type.'
    if not Decimal(str(amount)).is_finite() or amount <= 0 or tenure <= 0:
        return 'Enter a valid positive loan amount and tenure.'
    cap = policy.get('amounts', {}).get(purpose, policy['max_amount'])
    if cap and amount > cap:
        return f"Maximum loan amount for {purpose} is {cap:,.2f}, regardless of savings."
    maximum = policy['tenures'][purpose]
    if tenure > maximum:
        return f'Maximum tenure for {purpose} is {maximum} months.'
    return None


def eligible_amount(db, savings_balance, purpose=None):
    """The most this member may borrow — the single answer used everywhere.

    Two ceilings apply and the lower one wins: what their own savings support,
    and the cooperative's absolute cap, which exists so one large loan cannot
    drain the cash other members are relying on. A cap of 0 means no cap.

    Every screen that tells a member what they can borrow must come through
    here. Showing a figure the application would then reject is worse than
    showing nothing.
    """
    savings_limit = round(float(savings_balance or 0) * multiplier(db), 2)
    policy = limits(db)
    if purpose is None:
        return max(row['eligible_amount'] for row in member_limits(db, savings_balance).values())
    cap = policy['amounts'][purpose]
    return min(savings_limit, cap) if cap else savings_limit


def member_limits(db, savings_balance):
    policy = limits(db)
    savings_limit = max(0, round(float(savings_balance or 0) * policy['multiplier'], 2))
    return {name: {
        'label': policy['products'][name]['label'],
        'rate': policy['products'][name]['rate'],
        'method': policy['products'][name]['method'],
        'tiers': policy['products'][name].get('tiers', []),
        'max_amount': cap,
        'eligible_amount': min(savings_limit, cap) if cap else savings_limit,
        'max_tenure_months': policy['tenures'][name],
    } for name, cap in policy['amounts'].items()}


def eligibility_note(db, savings_balance):
    """Plain wording for why the figure is what it is, so an officer asked
    'why can they only get this much?' has the answer on screen."""
    cap = limits(db)['max_amount']
    times = multiplier(db)
    times_text = f'{times:g}× savings'
    if cap and round(float(savings_balance or 0) * times, 2) > cap:
        return f'Capped at ₦{cap:,.2f} by the cooperative, which is below {times_text}.'
    return f'{times_text}.'
