import hmac
import os
import random
from datetime import datetime, timedelta

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from asset_schemes import delivery_cleared, delivery_dispatch_allowed, finance_terms
from database import get_db, last_insert_id
from email_service import run_in_background
from ledger import (FEE_INCOME, LOANS_RECEIVABLE, OPERATING_EXPENSES,
                    get_postable_cash_accounts, post_journal_required)
import loan_workflow as lw
from utils import (audit, finite_float, member_for_user, notify_member,
                   product_image_data_uri, role_required, validate_image)


asset_schemes = Blueprint('asset_schemes', __name__)
STAGES = ('secretary', 'treasurer', 'president')
NEXT = {'secretary': 'treasurer', 'treasurer': 'president', 'president': 'approved'}
CATEGORY_REFERENCE_IMAGES = {
    'air conditioner': 'images/asset-categories/air-conditioner.jpg',
    'television': 'images/asset-categories/television.jpg',
    'refrigerator': 'images/asset-categories/refrigerator.jpg',
    'freezer': 'images/asset-categories/freezer.jpg',
    'washing machine': 'images/asset-categories/washing-machine.jpg',
    'cooker': 'images/asset-categories/cooker.jpg',
    'microwave': 'images/asset-categories/microwave.jpg',
    'air fryer': 'images/asset-categories/air-fryer.jpg',
    'blender': 'images/asset-categories/blender.jpg',
    'generator': 'images/asset-categories/generator.jpg',
    'inverter': 'images/asset-categories/inverter.jpg',
}


def _parse_campaign_date(value, end_of_day=False):
    value = (value or '').strip()
    if not value:
        return None
    parsed = datetime.strptime(value, '%Y-%m-%d')
    if end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return parsed


def _campaign_body(db, campaign):
    products = db.execute('''SELECT category, brand, size_spec, estimated_price_min,
        estimated_price_max FROM asset_products WHERE campaign_id=? AND status='active'
        ORDER BY category, brand, size_spec''', (campaign['id'],)).fetchall()
    choices = []
    for product in products[:12]:
        price_min = float(product['estimated_price_min'] or 0)
        price_max = float(product['estimated_price_max'] or 0)
        price = (f" (indicative range: ₦{price_min:,.0f}–₦{price_max:,.0f})"
                 if price_min or price_max else '')
        choices.append(f"• {product['category']} — {product['brand'] or 'approved brands'}"
                       f" {product['size_spec'] or ''}{price}".strip())
    choice_text = '\n'.join(choices) or '• Home appliances selected by your cooperative'
    intro = (campaign['newsletter_intro'] or '').strip() or (
        'Acquire the home appliance you have been planning for without paying the full cost at once. '
        'Choose what suits your household and budget, then let your cooperative source and review the final quote with you.'
    )
    return (
        'Dear {first_name},\n\n'
        f'{intro}\n\nAvailable choices include:\n{choice_text}\n\n'
        f"Why consider it now:\n• Convenient {campaign['tenure_months']}-month repayment plan\n"
        '• Cooperative-supported sourcing and approval\n'
        '• You review the final selling price before accepting\n'
        '• Select by appliance type, brand, size and expected budget\n\n'
        'Explore the campaign and submit your interest here: {portal_link}/asset-schemes\n\n'
        'Terms, eligibility review and final approval apply. Indicative prices may change after supplier quotation.\n\n'
        'Thank you,\nCooperative Administration'
    )


def _queue_newsletter(db, campaign, portal_url):
    from blueprints.communications import _process_campaign

    members = db.execute("SELECT id,email FROM members WHERE status='active' ORDER BY id").fetchall()
    subject = (campaign['newsletter_subject'] or '').strip() or f"{campaign['name']}: choose the appliance you need"
    db.execute('''INSERT INTO communication_campaigns
        (title,audience,channel,subject,body,status,recipient_count,created_by)
        VALUES (?,?,?,?,?,'sending',?,?)''',
        (f"Asset scheme: {campaign['name']}", 'active', 'email', subject,
         _campaign_body(db, campaign), len(members), campaign['created_by']))
    communication_id = last_insert_id(db)
    skipped = queued = 0
    for member in members:
        destination = (member['email'] or '').strip()
        status = 'queued' if destination else 'skipped'
        queued += status == 'queued'
        skipped += status == 'skipped'
        db.execute('''INSERT INTO communication_recipients
            (campaign_id,member_id,channel,destination,status,error)
            VALUES (?,?,'email',?,?,?)''',
            (communication_id, member['id'], destination, status,
             '' if destination else 'No email address'))
    now = datetime.now()
    next_send = now + timedelta(days=int(campaign['newsletter_frequency_days'] or 7))
    db.execute('UPDATE communication_campaigns SET skipped_count=? WHERE id=?',
               (skipped, communication_id))
    db.execute('''UPDATE asset_campaigns SET newsletter_last_sent_at=?,
        newsletter_next_send_at=? WHERE id=?''', (now, next_send, campaign['id']))
    db.commit()
    if queued:
        run_in_background(_process_campaign, communication_id, portal_url)
    return communication_id, queued, skipped


def _event(db, order_id, stage, action, comment=''):
    db.execute('''INSERT INTO asset_order_events
        (order_id, stage, action, actor_id, actor_name, comment)
        VALUES (?, ?, ?, ?, ?, ?)''',
        (order_id, stage, action, current_user.id, current_user.username, comment or ''))


def _order(db, order_id):
    return db.execute('''SELECT o.*, c.name AS campaign_name,
        m.first_name, m.last_name, m.member_number, m.email
        FROM asset_orders o JOIN asset_campaigns c ON c.id=o.campaign_id
        JOIN members m ON m.id=o.member_id WHERE o.id=?''', (order_id,)).fetchone()


def _notify_order(db, order, title, message):
    if order['email']:
        notify_member(db, order['email'], title, message, 'info',
                      f'/asset-schemes/orders/{order["id"]}')


@asset_schemes.route('/asset-schemes')
@login_required
def catalogue():
    db = get_db()
    member = member_for_user(db)
    if not member:
        if current_user.role in {'admin', 'treasurer', 'secretary', 'exco'}:
            return redirect(url_for('asset_schemes.manage'))
        flash('A member profile must be linked to use Asset Schemes.', 'warning')
        return redirect(url_for('main.dashboard'))
    now = datetime.now()
    campaigns = db.execute("""SELECT * FROM asset_campaigns WHERE status='open'
        AND (opens_at IS NULL OR opens_at <= ?) AND (closes_at IS NULL OR closes_at >= ?)
        ORDER BY id DESC""", (now, now)).fetchall()
    products = db.execute('''SELECT p.* FROM asset_products p JOIN asset_campaigns c ON c.id=p.campaign_id
        WHERE p.status='active' AND c.status='open'
        AND (c.opens_at IS NULL OR c.opens_at <= ?) AND (c.closes_at IS NULL OR c.closes_at >= ?)
        ORDER BY p.campaign_id DESC, p.name''', (now, now)).fetchall()
    orders = db.execute('SELECT * FROM asset_orders WHERE member_id=? ORDER BY id DESC', (member['id'],)).fetchall()
    return render_template('asset_schemes/catalogue.html', member=member, campaigns=campaigns,
                           products=products, orders=orders,
                           category_images=CATEGORY_REFERENCE_IMAGES)


@asset_schemes.route('/asset-schemes/apply/<int:campaign_id>', methods=['POST'])
@login_required
def apply(campaign_id):
    db = get_db()
    member = member_for_user(db)
    now = datetime.now()
    campaign = db.execute("""SELECT * FROM asset_campaigns WHERE id=? AND status='open'
        AND (opens_at IS NULL OR opens_at <= ?) AND (closes_at IS NULL OR closes_at >= ?)""",
        (campaign_id, now, now)).fetchone()
    if not member or not campaign:
        flash('This asset scheme is not available.', 'danger')
        return redirect(url_for('asset_schemes.catalogue'))
    if request.form.get('terms_accepted') != '1':
        flash('Accept the scheme terms before submitting.', 'danger')
        return redirect(url_for('asset_schemes.catalogue'))
    repayment_method = request.form.get('repayment_method', '').strip()
    allowed = {x.strip() for x in (campaign['repayment_methods'] or '').split(',') if x.strip()}
    if repayment_method not in allowed:
        flash('Choose an available repayment method.', 'danger')
        return redirect(url_for('asset_schemes.catalogue'))
    selected = []
    for product in db.execute("SELECT * FROM asset_products WHERE campaign_id=? AND status='active'", (campaign_id,)).fetchall():
        try:
            qty = int(request.form.get(f'qty_{product["id"]}', 0) or 0)
        except ValueError:
            qty = 0
        if qty < 0 or qty > 20:
            flash('Invalid product quantity.', 'danger')
            return redirect(url_for('asset_schemes.catalogue'))
        if qty:
            try:
                expected = finite_float(request.form.get(f'budget_{product["id"]}', 0))
            except ValueError:
                expected = 0
            if expected <= 0:
                flash(f'Enter your expected spend for {product["name"]}.', 'danger')
                return redirect(url_for('asset_schemes.catalogue'))
            selected.append((product, qty, expected))
    if not selected:
        flash('Select at least one appliance.', 'danger')
        return redirect(url_for('asset_schemes.catalogue'))
    subtotal = round(sum(expected * q for p, q, expected in selected), 2)
    delivery = 0.0
    interest, total, monthly = finance_terms(subtotal, campaign['annual_interest_rate'], campaign['tenure_months'])
    annual_salary = float(member['annual_salary'] or 0)
    eligible = annual_salary <= 0 or monthly <= annual_salary / 12 * 0.4
    reason = ('Monthly repayment is within 40% of recorded monthly salary.' if annual_salary and eligible
              else 'Monthly repayment exceeds 40% of recorded monthly salary; committee review required.' if annual_salary
              else 'No salary recorded; committee affordability review required.')
    order_number = f"AS/{datetime.now():%Y%m%d}/{random.randint(1000, 9999)}"
    db.execute('''INSERT INTO asset_orders
        (order_number,campaign_id,member_id,status,approval_stage,expected_spend,quote_status,
         selling_subtotal,annual_interest_rate,tenure_months,
         interest_amount,total_repayment,monthly_repayment,delivery_fee,repayment_method,
         eligibility_status,eligibility_reason,terms_accepted,signature_name,created_by)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (order_number, campaign_id, member['id'], 'sourcing_quote', 'quote', subtotal, 'not_quoted',
         subtotal, campaign['annual_interest_rate'],
         campaign['tenure_months'], interest, total, monthly, delivery, repayment_method,
         'eligible' if eligible else 'review', reason, 1,
         request.form.get('signature_name', '').strip(), current_user.id))
    order_id = last_insert_id(db)
    for product, qty, expected in selected:
        db.execute('''INSERT INTO asset_order_items
            (order_id,product_id,product_name,brand,model,quantity,unit_purchase_cost,
             unit_selling_price,unit_delivery_fee,expected_unit_spend) VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (order_id, product['id'], product['name'], product['brand'], product['model'], qty,
             product['purchase_cost'], 0, 0, expected))
    _event(db, order_id, 'quote', 'sourcing_requested',
           f'Member budget ₦{subtotal:,.2f}. {reason}')
    db.commit()
    audit(db, 'ASSET_ORDER_SUBMITTED', 'asset_schemes', f'{order_number} submitted')
    flash(f'Asset request {order_number} submitted. The cooperative will now source options and send you a final quote.', 'success')
    return redirect(url_for('asset_schemes.order_detail', order_id=order_id))


@asset_schemes.route('/asset-schemes/orders/<int:order_id>')
@login_required
def order_detail(order_id):
    db = get_db()
    order = _order(db, order_id)
    member = member_for_user(db)
    staff = current_user.role in {'admin', 'treasurer', 'secretary', 'exco'}
    if not order or (not staff and (not member or order['member_id'] != member['id'])):
        flash('Asset request not found.', 'danger')
        return redirect(url_for('asset_schemes.catalogue'))
    items = db.execute('SELECT * FROM asset_order_items WHERE order_id=? ORDER BY id', (order_id,)).fetchall()
    events = db.execute('SELECT * FROM asset_order_events WHERE order_id=? ORDER BY id DESC', (order_id,)).fetchall()
    cash_accounts = get_postable_cash_accounts(db) if staff else []
    return render_template('asset_schemes/order.html', order=order, items=items, events=events,
                           staff=staff, delivery_cleared=delivery_cleared(order), cash_accounts=cash_accounts)


@asset_schemes.route('/asset-schemes/manage', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'treasurer', 'secretary')
def manage():
    db = get_db()
    if request.method == 'POST':
        if current_user.role not in {'admin', 'treasurer'}:
            flash('Only the President/Admin or Treasurer may create asset campaigns.', 'danger')
            return redirect(url_for('asset_schemes.manage'))
        try:
            name = request.form['name'].strip()
            tenure = int(request.form.get('tenure_months', 6))
            rate = finite_float(request.form.get('annual_interest_rate', 20))
            if not name or tenure < 1 or tenure > 60 or rate < 0:
                raise ValueError('Enter valid scheme terms.')
            methods = ','.join(request.form.getlist('repayment_methods'))
            status = request.form.get('status', 'draft')
            if status not in {'draft', 'open'}:
                raise ValueError('Choose a valid campaign status.')
            opens_at = _parse_campaign_date(request.form.get('opens_at'))
            closes_at = _parse_campaign_date(request.form.get('closes_at'), end_of_day=True)
            if status == 'open' and not closes_at:
                raise ValueError('Enter a campaign end date before opening it to members.')
            if opens_at and closes_at and closes_at <= opens_at:
                raise ValueError('Campaign end date must be after its opening date.')
            newsletter_enabled = 1 if request.form.get('newsletter_enabled') == '1' else 0
            frequency = int(request.form.get('newsletter_frequency_days', 7))
            if frequency < 3 or frequency > 90:
                raise ValueError('Newsletter frequency must be between 3 and 90 days.')
            first_send = datetime.now() if status == 'open' and newsletter_enabled else None
            db.execute('''INSERT INTO asset_campaigns
                (name,description,status,opens_at,closes_at,tenure_months,annual_interest_rate,
                 repayment_methods,banner_enabled,newsletter_enabled,newsletter_frequency_days,
                 newsletter_subject,newsletter_intro,newsletter_next_send_at,created_by)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (name, request.form.get('description','').strip(), status, opens_at, closes_at,
                 tenure, rate, methods, 1 if request.form.get('banner_enabled') == '1' else 0,
                 newsletter_enabled, frequency, request.form.get('newsletter_subject','').strip(),
                 request.form.get('newsletter_intro','').strip(), first_send, current_user.id))
            db.commit()
            flash('Asset scheme created.', 'success')
        except Exception as exc:
            db.rollback(); flash(str(exc), 'danger')
        return redirect(url_for('asset_schemes.manage'))
    campaigns = db.execute('SELECT * FROM asset_campaigns ORDER BY id DESC').fetchall()
    library = db.execute('''SELECT * FROM asset_catalogue
        ORDER BY is_active DESC, category, brand, size_spec, name''').fetchall()
    campaign_products = db.execute('''SELECT campaign_id, category, name, brand, size_spec
        FROM asset_products WHERE status='active' ORDER BY campaign_id, category, brand, size_spec''').fetchall()
    orders = db.execute('''SELECT o.*,m.first_name,m.last_name,c.name campaign_name FROM asset_orders o
        JOIN members m ON m.id=o.member_id JOIN asset_campaigns c ON c.id=o.campaign_id ORDER BY o.id DESC''').fetchall()
    return render_template('asset_schemes/manage.html', campaigns=campaigns, orders=orders,
                           library=library, campaign_products=campaign_products,
                           category_images=CATEGORY_REFERENCE_IMAGES)


@asset_schemes.route('/asset-schemes/campaigns/<int:campaign_id>/settings', methods=['POST'])
@login_required
@role_required('admin', 'treasurer')
def update_campaign(campaign_id):
    db = get_db()
    campaign = db.execute('SELECT * FROM asset_campaigns WHERE id=?', (campaign_id,)).fetchone()
    if not campaign:
        flash('Campaign not found.', 'danger')
        return redirect(url_for('asset_schemes.manage'))
    try:
        status = request.form.get('status', campaign['status'])
        if status not in {'draft', 'open', 'closed'}:
            raise ValueError('Choose a valid campaign status.')
        opens_at = _parse_campaign_date(request.form.get('opens_at'))
        closes_at = _parse_campaign_date(request.form.get('closes_at'), end_of_day=True)
        if status == 'open' and not closes_at:
            raise ValueError('An open campaign must have an end date.')
        if opens_at and closes_at and closes_at <= opens_at:
            raise ValueError('Campaign end date must be after its opening date.')
        frequency = int(request.form.get('newsletter_frequency_days', 7))
        if frequency < 3 or frequency > 90:
            raise ValueError('Newsletter frequency must be between 3 and 90 days.')
        enabled = 1 if request.form.get('newsletter_enabled') == '1' else 0
        next_send = campaign['newsletter_next_send_at']
        if status == 'open' and enabled and not next_send:
            next_send = datetime.now()
        if status != 'open' or not enabled:
            next_send = None
        db.execute('''UPDATE asset_campaigns SET status=?,opens_at=?,closes_at=?,banner_enabled=?,
            newsletter_enabled=?,newsletter_frequency_days=?,newsletter_subject=?,newsletter_intro=?,
            newsletter_next_send_at=? WHERE id=?''',
            (status, opens_at, closes_at, 1 if request.form.get('banner_enabled') == '1' else 0,
             enabled, frequency, request.form.get('newsletter_subject','').strip(),
             request.form.get('newsletter_intro','').strip(), next_send, campaign_id))
        db.commit()
        audit(db, 'ASSET_CAMPAIGN_SETTINGS', 'asset_schemes',
              f'Campaign {campaign_id} status={status}, newsletter={enabled}, frequency={frequency}')
        flash('Campaign visibility and newsletter schedule updated.', 'success')
    except Exception as exc:
        db.rollback()
        flash(str(exc), 'danger')
    return redirect(url_for('asset_schemes.manage'))


@asset_schemes.route('/tasks/asset-schemes/newsletters', methods=['GET', 'POST'])
def asset_campaign_newsletters():
    db = get_db()
    token = (os.environ.get('TASK_RUNNER_TOKEN', '') or '').strip()
    provided = (request.headers.get('X-Task-Token', '') or request.args.get('token', '')).strip()
    by_token = bool(token) and hmac.compare_digest(provided, token)
    by_user = current_user.is_authenticated and current_user.role in {'admin', 'treasurer'}
    if not (by_token or by_user):
        return jsonify({'success': False, 'error': 'Unauthorised'}), 403
    now = datetime.now()
    due = db.execute('''SELECT * FROM asset_campaigns WHERE status='open'
        AND newsletter_enabled=1 AND newsletter_next_send_at IS NOT NULL
        AND newsletter_next_send_at <= ? AND (opens_at IS NULL OR opens_at <= ?)
        AND (closes_at IS NULL OR closes_at >= ?) ORDER BY id''', (now, now, now)).fetchall()
    base_url = request.url_root.rstrip('/')
    results = []
    for campaign in due:
        communication_id, queued, skipped = _queue_newsletter(db, campaign, base_url)
        results.append({'campaign_id': campaign['id'], 'communication_id': communication_id,
                        'queued': queued, 'skipped': skipped})
    expired = db.execute("""SELECT COUNT(*) FROM asset_campaigns
        WHERE status='open' AND closes_at IS NOT NULL AND closes_at < ?""", (now,)).fetchone()[0]
    db.execute("""UPDATE asset_campaigns SET status='closed',newsletter_next_send_at=NULL
        WHERE status='open' AND closes_at IS NOT NULL AND closes_at < ?""", (now,))
    db.commit()
    if by_user and not by_token:
        queued_total = sum(item['queued'] for item in results)
        flash(f'Processed {len(results)} due campaign(s); {queued_total} member email(s) queued.',
              'success' if results else 'info')
        return redirect(url_for('asset_schemes.manage'))
    return jsonify({'success': True, 'campaigns_processed': len(results),
                    'expired_campaigns_closed': expired, 'results': results})


@asset_schemes.route('/asset-schemes/campaigns/<int:campaign_id>/library', methods=['POST'])
@login_required
@role_required('admin', 'treasurer')
def add_from_library(campaign_id):
    db = get_db()
    campaign = db.execute('SELECT id FROM asset_campaigns WHERE id=?', (campaign_id,)).fetchone()
    if not campaign:
        flash('Campaign not found.', 'danger')
        return redirect(url_for('asset_schemes.manage'))
    selected = []
    for raw_id in request.form.getlist('library_ids'):
        try:
            selected.append(int(raw_id))
        except (TypeError, ValueError):
            continue
    if not selected:
        flash('Select at least one appliance from the library.', 'warning')
        return redirect(url_for('asset_schemes.manage'))
    added = skipped = 0
    for library_id in selected:
        item = db.execute('SELECT * FROM asset_catalogue WHERE id=? AND is_active=1', (library_id,)).fetchone()
        if not item:
            continue
        exists = db.execute('''SELECT id FROM asset_products WHERE campaign_id=? AND category=?
            AND name=? AND brand=? AND size_spec=? AND status='active' LIMIT 1''',
            (campaign_id, item['category'], item['name'], item['brand'], item['size_spec'])).fetchone()
        if exists:
            skipped += 1
            continue
        db.execute('''INSERT INTO asset_products
            (campaign_id,name,brand,category,size_spec,description,image_url,purchase_cost,
             selling_price,estimated_price_min,estimated_price_max,delivery_fee,stock_limit,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'active')''',
            (campaign_id, item['name'], item['brand'], item['category'], item['size_spec'],
             item['description'], item['image_url'], 0, 0, item['estimated_price_min'],
             item['estimated_price_max'], 0, 0))
        added += 1
    db.commit()
    audit(db, 'ASSET_LIBRARY_ADD', 'asset_schemes',
          f'Added {added} library items to campaign {campaign_id}; skipped {skipped}')
    flash(f'{added} appliance option(s) added to the campaign.' +
          (f' {skipped} duplicate(s) skipped.' if skipped else ''), 'success')
    return redirect(url_for('asset_schemes.manage'))


@asset_schemes.route('/asset-schemes/library/<int:item_id>', methods=['POST'])
@login_required
@role_required('admin', 'treasurer')
def update_library_item(item_id):
    db = get_db()
    item = db.execute('SELECT * FROM asset_catalogue WHERE id=?', (item_id,)).fetchone()
    if not item:
        flash('Library item not found.', 'danger')
        return redirect(url_for('asset_schemes.manage'))
    try:
        price_min = finite_float(request.form.get('estimated_price_min'))
        price_max = finite_float(request.form.get('estimated_price_max'))
        category = request.form.get('category','').strip()
        name = request.form.get('name','').strip()
        brand = request.form.get('brand','').strip()
        size_spec = request.form.get('size_spec','').strip()
        if not all((category, name, brand, size_spec)):
            raise ValueError('Category, appliance name, brand and size are required.')
        if price_min <= 0 or price_max < price_min:
            raise ValueError('Enter a valid indicative price range.')
        current_image = item['image_url'] or ''
        supplied_url = request.form.get('image_url', '').strip()
        upload = request.files.get('image_file')
        if upload and upload.filename:
            valid, error = validate_image(upload)
            if not valid:
                raise ValueError(error)
            image_value = product_image_data_uri(upload)
        elif request.form.get('remove_image') == '1':
            image_value = ''
        elif supplied_url:
            image_value = supplied_url
        else:
            image_value = current_image
        db.execute('''UPDATE asset_catalogue SET category=?,name=?,brand=?,size_spec=?,description=?,
            image_url=?,estimated_price_min=?,estimated_price_max=?,is_active=?,updated_at=? WHERE id=?''',
            (category, name, brand, size_spec, request.form.get('description','').strip(),
             image_value, price_min, price_max,
             1 if request.form.get('is_active') == '1' else 0, datetime.now(), item_id))
        db.execute('''UPDATE asset_products SET category=?,name=?,brand=?,size_spec=?,description=?,
            image_url=?,estimated_price_min=?,estimated_price_max=?
            WHERE category=? AND name=? AND brand=? AND size_spec=?''',
            (category, name, brand, size_spec, request.form.get('description','').strip(),
             image_value, price_min, price_max, item['category'], item['name'],
             item['brand'], item['size_spec']))
        db.execute('''UPDATE asset_products SET category=?,name=?,brand=?,size_spec=?,description=?,
            image_url=?,estimated_price_min=?,estimated_price_max=?
            WHERE category=? AND name=? AND brand=? AND size_spec=?''',
            (category, name, brand, size_spec, request.form.get('description','').strip(),
             image_value, price_min, price_max, item['category'], item['name'],
             item['brand'], item['size_spec']))
        db.commit()
        flash('Appliance library item updated.', 'success')
    except Exception as exc:
        db.rollback(); flash(str(exc), 'danger')
    return redirect(url_for('asset_schemes.manage'))


@asset_schemes.route('/asset-schemes/library', methods=['POST'])
@login_required
@role_required('admin', 'treasurer')
def create_library_item():
    db = get_db()
    try:
        category = request.form.get('category', '').strip()
        name = request.form.get('name', '').strip()
        brand = request.form.get('brand', '').strip()
        size_spec = request.form.get('size_spec', '').strip()
        description = request.form.get('description', '').strip()
        image_url = request.form.get('image_url', '').strip()
        upload = request.files.get('image_file')
        if upload and upload.filename:
            valid, error = validate_image(upload)
            if not valid:
                raise ValueError(error)
            image_url = product_image_data_uri(upload)
        price_min = finite_float(request.form.get('estimated_price_min'))
        price_max = finite_float(request.form.get('estimated_price_max'))
        if not all((category, name, brand, size_spec)):
            raise ValueError('Category, appliance name, brand and size are required.')
        if price_min <= 0 or price_max < price_min:
            raise ValueError('The maximum indicative price must be at least the minimum price.')
        duplicate = db.execute('''SELECT id FROM asset_catalogue
            WHERE LOWER(category)=LOWER(?) AND LOWER(name)=LOWER(?)
              AND LOWER(brand)=LOWER(?) AND LOWER(size_spec)=LOWER(?) LIMIT 1''',
            (category, name, brand, size_spec)).fetchone()
        if duplicate:
            raise ValueError('This appliance configuration already exists in the master library.')
        db.execute('''INSERT INTO asset_catalogue
            (category,name,brand,size_spec,description,image_url,estimated_price_min,
             estimated_price_max,is_active,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (category, name, brand, size_spec, description, image_url, price_min, price_max,
             1 if request.form.get('is_active') == '1' else 0, datetime.now()))
        db.commit()
        audit(db, 'ASSET_LIBRARY_CREATE', 'asset_schemes',
              f'Created {category}: {name}, {brand}, {size_spec}')
        db.commit()
        flash('New appliance added to the master library.', 'success')
    except Exception as exc:
        db.rollback()
        flash(str(exc), 'danger')
    return redirect(url_for('asset_schemes.manage'))


@asset_schemes.route('/asset-schemes/campaigns/<int:campaign_id>/products', methods=['POST'])
@login_required
@role_required('admin', 'treasurer')
def add_product(campaign_id):
    db = get_db()
    try:
        price_min = finite_float(request.form.get('estimated_price_min') or 0)
        price_max = finite_float(request.form.get('estimated_price_max') or 0)
        if not request.form['name'].strip() or price_min <= 0 or price_max < price_min:
            raise ValueError('Enter a valid appliance type and indicative price range.')
        db.execute('''INSERT INTO asset_products
            (campaign_id,name,brand,model,category,size_spec,description,image_url,
             purchase_cost,selling_price,estimated_price_min,estimated_price_max,
             delivery_fee,stock_limit,warranty) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (campaign_id, request.form['name'].strip(), request.form.get('brand','').strip(),
             request.form.get('model','').strip(), request.form.get('category','').strip(),
             request.form.get('size_spec','').strip(), request.form.get('description','').strip(),
             request.form.get('image_url','').strip(), 0, 0, price_min, price_max, 0, 0,
             request.form.get('warranty','').strip()))
        db.commit(); flash('Appliance added to the catalogue.', 'success')
    except Exception as exc:
        db.rollback(); flash(str(exc), 'danger')
    return redirect(url_for('asset_schemes.manage'))


@asset_schemes.route('/asset-schemes/orders/<int:order_id>/action', methods=['POST'])
@login_required
@role_required('admin', 'treasurer', 'secretary')
def staff_action(order_id):
    db = get_db(); order = _order(db, order_id)
    action = request.form.get('action'); comment = request.form.get('comment','').strip()
    if not order:
        flash('Asset request not found.', 'danger'); return redirect(url_for('asset_schemes.manage'))
    try:
        if action == 'quote':
            if order['status'] != 'sourcing_quote':
                raise ValueError('This request is not awaiting a quote.')
            selling = finite_float(request.form.get('quoted_selling_price'))
            supplier_cost = finite_float(request.form.get('quoted_supplier_cost'))
            delivery = finite_float(request.form.get('quoted_delivery_fee') or 0)
            if selling <= 0 or supplier_cost <= 0 or selling < supplier_cost or delivery < 0:
                raise ValueError('Enter valid supplier cost, selling price and delivery fee.')
            interest, total, monthly = finance_terms(selling, order['annual_interest_rate'], order['tenure_months'])
            db.execute('''UPDATE asset_orders SET status='awaiting_member_quote',quote_status='quoted',
                selling_subtotal=?,interest_amount=?,total_repayment=?,monthly_repayment=?,
                delivery_fee=?,procurement_cost=?,quote_note=?,quoted_at=? WHERE id=?''',
                (selling, interest, total, monthly, delivery, supplier_cost,
                 request.form.get('quote_note','').strip(), datetime.now(), order_id))
            _event(db, order_id, 'quote', 'quoted',
                   f'Final selling price ₦{selling:,.2f}; delivery ₦{delivery:,.2f}')
        elif action in {'approve', 'reject'}:
            stage = order['approval_stage']
            if stage not in STAGES or not lw.can_act(current_user.role, stage):
                raise ValueError('You are not the officer assigned to this approval stage.')
            prior = db.execute("SELECT 1 FROM asset_order_events WHERE order_id=? AND actor_id=? AND action='approved'",
                               (order_id, current_user.id)).fetchone()
            if lw.segregation_enforced(db) and prior:
                raise ValueError('A different officer must approve each stage.')
            if action == 'reject':
                if not comment: raise ValueError('A rejection reason is required.')
                db.execute("UPDATE asset_orders SET status='rejected',approval_stage='rejected' WHERE id=?", (order_id,))
                _event(db, order_id, stage, 'rejected', comment)
            else:
                nxt = NEXT[stage]
                status = 'approved_for_procurement' if nxt == 'approved' else 'submitted'
                db.execute('UPDATE asset_orders SET approval_stage=?,status=?,approved_at=CASE WHEN ?=\'approved\' THEN ? ELSE approved_at END WHERE id=?',
                           (nxt, status, nxt, datetime.now(), order_id))
                _event(db, order_id, stage, 'approved', comment)
        elif action == 'procured':
            if order['status'] != 'approved_for_procurement': raise ValueError('Final approval is required first.')
            if order['procurement_journal_id']: raise ValueError('Procurement was already posted.')
            cost = round(float(order['procurement_cost'] or 0), 2)
            account = request.form.get('procurement_account','').strip()
            reference = request.form.get('procurement_reference','').strip()
            valid = {r['code'] for r in get_postable_cash_accounts(db)}
            if cost <= 0: raise ValueError('Add supplier purchase costs to the catalogue before recording procurement.')
            if account not in valid: raise ValueError('Select the bank or cash account used to purchase the items.')
            if not reference: raise ValueError('Enter the supplier invoice or payment reference.')
            journal_id = post_journal_required(db, f"Asset procurement - {order['order_number']}", [
                {'account': OPERATING_EXPENSES, 'debit': cost, 'memo': order['order_number']},
                {'account': account, 'credit': cost, 'memo': reference},
            ], reference=reference, source_module='asset_procurement', source_id=order_id,
               created_by=current_user.id)
            db.execute('''UPDATE asset_orders SET status='procured',procured_at=?,procurement_cost=?,
                procurement_account=?,procurement_reference=?,procurement_journal_id=? WHERE id=?''',
                (datetime.now(), cost, account, reference, journal_id, order_id))
            _event(db, order_id, 'procurement', 'procured', comment)
        elif action == 'dispatch':
            if order['status'] != 'procured': raise ValueError('Record procurement first.')
            if not delivery_dispatch_allowed(order): raise ValueError('Delivery fee must be paid, waived, or marked pay-on-delivery.')
            db.execute("UPDATE asset_orders SET status='ready_for_receipt',dispatched_at=? WHERE id=?", (datetime.now(), order_id))
            _event(db, order_id, 'delivery', 'dispatched', comment)
        elif action == 'delivery_fee':
            fee_status = request.form.get('delivery_fee_status')
            if fee_status not in {'paid','pay_on_delivery','waived'}: raise ValueError('Invalid delivery fee status.')
            if fee_status == 'waived' and current_user.role != 'admin': raise ValueError('Only the President/Admin may waive delivery fees.')
            reference = request.form.get('delivery_fee_reference','').strip()
            account = request.form.get('delivery_fee_account','').strip()
            journal_id = order['delivery_fee_journal_id']
            if fee_status == 'paid' and float(order['delivery_fee'] or 0) > 0:
                if journal_id: raise ValueError('This delivery fee has already been posted.')
                valid = {r['code'] for r in get_postable_cash_accounts(db)}
                if account not in valid: raise ValueError('Select the bank or cash account that received the delivery fee.')
                if not reference: raise ValueError('Enter the delivery fee payment reference.')
                journal_id = post_journal_required(db, f"Delivery fee - {order['order_number']}", [
                    {'account': account, 'debit': order['delivery_fee'], 'memo': order['order_number']},
                    {'account': FEE_INCOME, 'credit': order['delivery_fee'], 'memo': 'Asset delivery recovery'},
                ], reference=reference, source_module='asset_delivery_fee', source_id=order_id,
                   created_by=current_user.id)
            db.execute('''UPDATE asset_orders SET delivery_fee_status=?,delivery_fee_reference=?,
                delivery_fee_account=?,delivery_fee_journal_id=? WHERE id=?''',
                       (fee_status, reference, account or None, journal_id, order_id))
            _event(db, order_id, 'delivery_fee', fee_status, comment)
        else:
            raise ValueError('Unknown action.')
        updated = _order(db, order_id)
        _notify_order(db, updated, 'Asset scheme request updated',
                      f'Your request {updated["order_number"]} is now {updated["status"].replace("_", " ")}.')
        db.commit(); flash('Asset request updated.', 'success')
    except Exception as exc:
        db.rollback(); flash(str(exc), 'danger')
    return redirect(url_for('asset_schemes.order_detail', order_id=order_id))


@asset_schemes.route('/asset-schemes/orders/<int:order_id>/accept-quote', methods=['POST'])
@login_required
def accept_quote(order_id):
    db = get_db(); order = _order(db, order_id); member = member_for_user(db)
    try:
        if not order or not member or order['member_id'] != member['id']:
            raise ValueError('You cannot accept this quote.')
        if order['status'] != 'awaiting_member_quote' or order['quote_status'] != 'quoted':
            raise ValueError('There is no final quote awaiting acceptance.')
        if request.form.get('accept_quote') != '1':
            raise ValueError('Confirm that you accept the final quote and repayment schedule.')
        db.execute("""UPDATE asset_orders SET status='submitted',approval_stage='secretary',
            quote_status='accepted',quote_accepted_at=? WHERE id=?""", (datetime.now(), order_id))
        _event(db, order_id, 'quote', 'accepted', 'Member accepted final selling price and repayment schedule')
        db.commit(); audit(db, 'ASSET_QUOTE_ACCEPTED', 'asset_schemes', order['order_number'])
        flash('Final quote accepted. Your request has entered the approval workflow.', 'success')
    except Exception as exc:
        db.rollback(); flash(str(exc), 'danger')
    return redirect(url_for('asset_schemes.order_detail', order_id=order_id))


@asset_schemes.route('/asset-schemes/orders/<int:order_id>/confirm-receipt', methods=['POST'])
@login_required
def confirm_receipt(order_id):
    db = get_db(); order = _order(db, order_id); member = member_for_user(db)
    try:
        if not order or not member or order['member_id'] != member['id']:
            raise ValueError('You cannot confirm this order.')
        if order['status'] != 'ready_for_receipt': raise ValueError('This order is not ready for receipt confirmation.')
        if not delivery_cleared(order): raise ValueError('The delivery fee has not been cleared.')
        if order['linked_loan_id']: raise ValueError('Receipt was already confirmed.')
        loan_number = f"LOAN/AS/{datetime.now():%Y%m%d}/{random.randint(1000,9999)}"
        db.execute('''INSERT INTO loans
            (loan_number,member_id,amount,purpose,description,tenure,interest_rate,interest_method,
             total_repayment,balance,status,approval_stage,disbursed_amount,disbursement_date,
             approved_at,terms_accepted,signature_name,date_applied,submission_channel)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (loan_number, member['id'], order['selling_subtotal'], 'Asset Purchase',
             f"Asset scheme order {order['order_number']}", order['tenure_months'],
             order['annual_interest_rate'], 'flat', order['total_repayment'],
             order['total_repayment'], 'active', 'approved', order['selling_subtotal'],
             datetime.now(), order['approved_at'], 1, order['signature_name'], order['submitted_at'], 'asset_scheme'))
        loan_id = last_insert_id(db)
        # Selling price becomes the member receivable; interest remains
        # unearned and is recognised by the normal repayment splitter.
        post_journal_required(db, f"Asset delivered - {order['order_number']}", [
            {'account': LOANS_RECEIVABLE, 'debit': order['selling_subtotal'], 'memo': loan_number},
            {'account': '4050', 'credit': order['selling_subtotal'], 'memo': order['order_number']},
        ], reference=order['order_number'], source_module='asset_scheme', source_id=order_id,
           created_by=current_user.id)
        db.execute("UPDATE asset_orders SET status='received',received_at=?,linked_loan_id=? WHERE id=? AND linked_loan_id IS NULL",
                   (datetime.now(), loan_id, order_id))
        _event(db, order_id, 'receipt', 'confirmed', f'Loan {loan_number} activated')
        db.commit(); audit(db, 'ASSET_RECEIPT_CONFIRMED', 'asset_schemes', f'{order["order_number"]} -> {loan_number}')
        flash('Receipt confirmed. Your repayment plan is now active.', 'success')
    except Exception as exc:
        db.rollback(); flash(str(exc), 'danger')
    return redirect(url_for('asset_schemes.order_detail', order_id=order_id))
