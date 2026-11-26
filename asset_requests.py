"""Creating an asset request. The web form and the mobile app both come through here, so the rules
cannot drift apart (they had: one required a signature and a minimum spend, the other did not)."""
import uuid
from datetime import datetime

import asset_eligibility
from asset_notifications import notify_asset_member, notify_asset_roles
from asset_schemes import finance_terms
from database import last_insert_id

MAX_QUANTITY = 20
MIN_BUDGET = 1000                    # per item, naira
PREFERENCE_LIMITS = {'requested_brand': 100, 'requested_model': 100,
                     'requested_size_spec': 160, 'requested_notes': 500}


class AssetRequestError(ValueError):
    """A request the member can fix; `code` lets the mobile app tell the cases apart."""

    def __init__(self, message, code='invalid_request'):
        super().__init__(message)
        self.code = code


def check_terms(campaign, repayment_method, terms_accepted):
    if not terms_accepted:
        raise AssetRequestError('Accept the scheme terms before submitting.', 'terms_required')
    allowed = {x.strip() for x in (campaign['repayment_methods'] or '').split(',') if x.strip()}
    if (repayment_method or '').strip() not in allowed:
        raise AssetRequestError('Choose an available repayment method.', 'invalid_repayment_method')
    return repayment_method.strip()


def clean_preferences(get):
    """The member's brand/model/size/notes, from `get(field)` (a form or a JSON item)."""
    values = {}
    for key, limit in PREFERENCE_LIMITS.items():
        value = str(get(key) or '').strip()
        if len(value) > limit:
            raise AssetRequestError('One of the appliance preference fields is too long.', 'invalid_items')
        values[key] = value
    return values


def check_item(product, quantity, expected):
    if quantity < 1 or quantity > MAX_QUANTITY or expected < MIN_BUDGET:
        raise AssetRequestError(
            f'Check the quantity (1 to {MAX_QUANTITY}) and expected spend (at least ₦{MIN_BUDGET:,}) '
            f'for {product["name"]}.', 'invalid_items')


def create_request(db, *, campaign, member, selected, repayment_method, signature, user_id, username, portal_url):
    """Store the request, its items and its first event, and tell the member and the sourcing officers.

    `selected` is a list of (product row, quantity, expected spend per item, preferences)."""
    if not selected:
        raise AssetRequestError('Select at least one appliance.', 'items_required')
    signature = (signature or '').strip()
    if not signature:
        raise AssetRequestError('Type your full name as confirmation.', 'signature_required')
    subtotal = round(sum(expected * quantity for _, quantity, expected, _ in selected), 2)
    interest, total, monthly = finance_terms(subtotal, campaign['annual_interest_rate'], campaign['tenure_months'])
    status, reason = asset_eligibility.review(db, member, monthly, total, repayment_method)
    db.execute('''INSERT INTO asset_orders
        (order_number,campaign_id,member_id,status,approval_stage,expected_spend,quote_status,
         selling_subtotal,annual_interest_rate,tenure_months,interest_amount,total_repayment,
         monthly_repayment,delivery_fee,repayment_method,eligibility_status,eligibility_reason,
         terms_accepted,signature_name,created_by)
        VALUES (?,?,?,'sourcing_quote','quote',?,'not_quoted',?,?,?,?,?,?,0,?,?,?,1,?,?)''',
               (f'AS/TMP/{uuid.uuid4().hex}', campaign['id'], member['id'], subtotal, subtotal,
                campaign['annual_interest_rate'], campaign['tenure_months'], interest, total, monthly,
                repayment_method, status, reason, signature, user_id))
    order_id = last_insert_id(db)
    # Numbered from the order's own id, so two requests on one day can never collide.
    order_number = f'AS/{datetime.now():%Y%m%d}/{order_id:05d}'
    db.execute('UPDATE asset_orders SET order_number=? WHERE id=?', (order_number, order_id))
    summary = []
    for product, quantity, expected, preferences in selected:
        db.execute('''INSERT INTO asset_order_items
            (order_id,product_id,product_name,brand,model,size_spec,quantity,unit_purchase_cost,
             unit_selling_price,unit_delivery_fee,expected_unit_spend,requested_brand,requested_model,
             requested_size_spec,requested_notes) VALUES (?,?,?,?,?,?,?,?,0,0,?,?,?,?,?)''',
                   (order_id, product['id'], product['name'], product['brand'], product['model'],
                    product['size_spec'], quantity, product['purchase_cost'], expected,
                    preferences['requested_brand'], preferences['requested_model'],
                    preferences['requested_size_spec'], preferences['requested_notes']))
        details = ', '.join(v for v in (preferences['requested_brand'], preferences['requested_model'],
                                        preferences['requested_size_spec']) if v)
        summary.append(f'{product["name"]}: {details or "open specification"}')
    db.execute('''INSERT INTO asset_order_events (order_id,stage,action,actor_id,actor_name,comment)
        VALUES (?,'quote','sourcing_requested',?,?,?)''',
               (order_id, user_id, username,
                f'Member budget ₦{subtotal:,.2f}. Requested: {"; ".join(summary)}. {reason}'))
    order = db.execute('''SELECT o.*,c.name campaign_name,m.first_name,m.last_name,m.email
        FROM asset_orders o JOIN asset_campaigns c ON c.id=o.campaign_id
        JOIN members m ON m.id=o.member_id WHERE o.id=?''', (order_id,)).fetchone()
    notify_asset_member(db, order, 'Asset request received',
                        'Your interest has been recorded. The cooperative is sourcing suitable options and will '
                        'send you a final quote.', 'success', portal_url)
    notify_asset_roles(db, order, ('admin', 'treasurer'), 'New asset request to source',
                       f'{member["first_name"]} {member["last_name"]} submitted an asset request requiring a '
                       'supplier quote.', 'info', portal_url)
    return order_id, order_number
