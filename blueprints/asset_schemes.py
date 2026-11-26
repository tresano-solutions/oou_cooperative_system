import hmac
import json
import os
from datetime import datetime, timedelta

from flask import Blueprint, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from asset_schemes import (CANCELLABLE_AFTER_PURCHASE, CANCELLABLE_BEFORE_PURCHASE, OFFICER_CONFIRM_WAIT_DAYS,
                           delivery_cleared, delivery_dispatch_allowed, effective_annual_rate, finance_terms,
                           member_can_cancel, quote_expired, quote_expiry, schedule_snapshot)
from asset_notifications import notify_asset_member, notify_asset_roles
from asset_requests import AssetRequestError, check_item, check_terms, clean_preferences, create_request
from asset_documents import (document_rows, ensure_asset_documents, render_asset_pdf,
                             update_asset_documents)
from asset_fulfilment import activate_delivered_asset
from database import for_update, get_db, last_insert_id
from email_service import run_in_background
from ledger import (FEE_INCOME, OPERATING_EXPENSES,
                    get_postable_cash_accounts, post_journal_required)
import asset_accounting
import asset_eligibility
import loan_workflow as lw
from utils import (audit, claim_submission, finite_float, member_for_user,
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
    products = db.execute('''SELECT category, brand, size_spec
        FROM asset_products WHERE campaign_id=? AND status='active'
        ORDER BY category, brand, size_spec''', (campaign['id'],)).fetchall()
    choices = []
    for product in products[:12]:
        choices.append(f"• {product['category']} — {product['brand'] or 'approved brands'}"
                       f" {product['size_spec'] or ''}".strip())
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
        'Terms, eligibility review and final approval apply.\n\n'
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


def _order(db, order_id, lock=False):
    """The order with its member and campaign. `lock=True` holds the order row until the
    transaction ends (PostgreSQL), so two officers acting at once are handled one after another."""
    sql = '''SELECT o.*, c.name AS campaign_name,
        m.first_name, m.last_name, m.member_number, m.email
        FROM asset_orders o JOIN asset_campaigns c ON c.id=o.campaign_id
        JOIN members m ON m.id=o.member_id WHERE o.id=?'''
    return db.execute(for_update(sql, 'o') if lock else sql, (order_id,)).fetchone()


def _acting_on_own_order(db, order):
    """True if the current user is the member who placed this request. An officer who is also a
    member must never quote, approve, buy, deliver or cancel their own order (same rule as loans)."""
    me = member_for_user(db)
    return bool(me and order and me['id'] == order['member_id'])


def _close_order(db, order_id, quote_status):
    """Mark a request cancelled and cancel its invoice and delivery order with it."""
    db.execute("UPDATE asset_orders SET status='cancelled',approval_stage='closed',quote_status=? WHERE id=?",
               (quote_status, order_id))
    update_asset_documents(db, order_id, invoice_status='cancelled', delivery_status='cancelled')


def _setting_days(db, key, default):
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    try:
        return max(0, int(float(row['value']))) if row and row['value'] not in (None, '') else default
    except (TypeError, ValueError):
        return default


def _officer_confirm_rules(db, order):
    """(available_from, reason) for confirming delivery on a member's behalf; reason is '' if allowed.

    Confirming creates the member's debt, so the officer route is a last resort: the items must have
    been out for a few days, and the officer who bought or dispatched them cannot be the one who
    confirms receipt for the member (when separation of duties is in force)."""
    dispatched = lw._as_datetime(order['dispatched_at'])
    wait = _setting_days(db, 'asset_officer_confirm_days', OFFICER_CONFIRM_WAIT_DAYS)
    available = dispatched + timedelta(days=wait) if dispatched else None
    if available and datetime.now() < available:
        return available, (f'The member has not had long enough to confirm for themselves. An officer can confirm '
                           f'on their behalf from {available:%d %b %Y}.')
    if lw.segregation_enforced(db):
        involved = db.execute("""SELECT 1 FROM asset_order_events WHERE order_id=? AND actor_id=?
            AND action IN ('procured','dispatched')""", (order['id'], current_user.id)).fetchone()
        if involved:
            return available, ('A different officer must confirm delivery for the member than the one who '
                               'purchased or dispatched the items.')
    return available, ''


def _portal_url():
    return request.url_root.rstrip('/')


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
    try:
        repayment_method = check_terms(campaign, request.form.get('repayment_method', ''),
                                       request.form.get('terms_accepted') == '1')
        selected = []
        for product in db.execute("SELECT * FROM asset_products WHERE campaign_id=? AND status='active'",
                                  (campaign_id,)).fetchall():
            try:
                qty = int(request.form.get(f'qty_{product["id"]}', 0) or 0)
            except ValueError:
                qty = 0
            if not qty:
                continue
            try:
                expected = finite_float(request.form.get(f'budget_{product["id"]}', 0))
            except ValueError:
                expected = 0
            check_item(product, qty, expected)
            fields = {'requested_brand': 'brand', 'requested_model': 'model',
                      'requested_size_spec': 'size', 'requested_notes': 'notes'}
            preferences = clean_preferences(
                lambda key, pid=product['id']: request.form.get(f'{fields[key]}_{pid}'))
            selected.append((product, qty, expected, preferences))
        if selected and not claim_submission(db, request.form.get('submission_token')):
            db.rollback()
            flash('That request was already submitted. Check "My requests" below.', 'info')
            return redirect(url_for('asset_schemes.catalogue'))
        order_id, order_number = create_request(
            db, campaign=campaign, member=member, selected=selected, repayment_method=repayment_method,
            signature=request.form.get('signature_name', ''), user_id=current_user.id,
            username=current_user.username, portal_url=_portal_url())
        db.commit()
    except AssetRequestError as exc:
        db.rollback()
        flash(str(exc), 'danger')
        return redirect(url_for('asset_schemes.catalogue'))
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
    can_manage_supplier_cost = current_user.role in {'admin', 'treasurer'}
    if not order or (not staff and (not member or order['member_id'] != member['id'])):
        flash('Asset request not found.', 'danger')
        return redirect(url_for('asset_schemes.catalogue'))
    items = db.execute('SELECT * FROM asset_order_items WHERE order_id=? ORDER BY id', (order_id,)).fetchall()
    events = db.execute('SELECT * FROM asset_order_events WHERE order_id=? ORDER BY id DESC', (order_id,)).fetchall()
    cash_accounts = get_postable_cash_accounts(db) if staff else []
    invoice, delivery_order = document_rows(db, order_id)
    template_staff = staff and (order['status'] != 'sourcing_quote' or can_manage_supplier_cost)
    own_order = staff and _acting_on_own_order(db, order)
    officer_confirm_from, officer_confirm_block = ((None, '') if order['status'] != 'ready_for_receipt'
                                                   else _officer_confirm_rules(db, order))
    return render_template('asset_schemes/order.html', order=order, items=items, events=events,
                           staff=template_staff and not own_order, own_order=own_order,
                           can_cancel_staff=(not own_order and current_user.role in {'admin', 'treasurer'}
                                             and order['status'] in CANCELLABLE_BEFORE_PURCHASE),
                           can_cancel_after_purchase=(not own_order and current_user.role == 'admin'
                                                      and order['status'] in CANCELLABLE_AFTER_PURCHASE),
                           member_can_cancel=bool(member and order['member_id'] == member['id']
                                                  and member_can_cancel(order)),
                           officer_confirm_from=officer_confirm_from, officer_confirm_block=officer_confirm_block,
                           quote_valid_until=quote_expiry(order), quote_expired=quote_expired(order),
                           effective_rate=(effective_annual_rate(order['selling_subtotal'], order['annual_interest_rate'],
                                                                 order['tenure_months'])
                                           if order['quote_status'] in ('quoted', 'accepted') else None),
                           can_manage_supplier_cost=can_manage_supplier_cost,
                           delivery_cleared=delivery_cleared(order), cash_accounts=cash_accounts,
                           invoice=invoice, delivery_order=delivery_order)


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
    provided = (request.headers.get('X-Task-Token', '')).strip()
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
    db = get_db(); order = _order(db, order_id, lock=True)
    action = request.form.get('action'); comment = request.form.get('comment','').strip()
    if not order:
        flash('Asset request not found.', 'danger'); return redirect(url_for('asset_schemes.manage'))
    try:
        if _acting_on_own_order(db, order):
            raise ValueError('You cannot act on your own asset request. A different officer must handle it.')
        if action in {'procured', 'delivery_fee'} and current_user.role not in {'admin', 'treasurer'}:
            raise ValueError('Only the President/Admin or Treasurer may record supplier payments and delivery-fee receipts.')
        if action == 'quote':
            if current_user.role not in {'admin', 'treasurer'}:
                raise ValueError('Only the President/Admin or Treasurer may record supplier costs and issue a quote.')
            if order['status'] != 'sourcing_quote':
                raise ValueError('This request is not awaiting a quote.')
            supplier_cost = finite_float(request.form.get('quoted_supplier_cost'))
            delivery = finite_float(request.form.get('quoted_delivery_fee') or 0)
            quote_items = db.execute(
                'SELECT * FROM asset_order_items WHERE order_id=? ORDER BY id', (order_id,)
            ).fetchall()
            if not quote_items:
                raise ValueError('This request has no appliance items to quote.')
            item_updates = []
            selling = 0.0
            legacy_total = request.form.get('quoted_selling_price')
            for item in quote_items:
                item_id = item['id']
                name = request.form.get(f'agreed_product_name_{item_id}', '').strip()
                brand = request.form.get(f'agreed_brand_{item_id}', '').strip()
                model = request.form.get(f'agreed_model_{item_id}', '').strip()
                specification = request.form.get(f'agreed_specification_{item_id}', '').strip()
                warranty = request.form.get(f'agreed_warranty_{item_id}', '').strip()
                unit_value = request.form.get(f'item_unit_price_{item_id}')
                # Keep existing single-item API/tests compatible while moving the UI
                # to line-level agreed products and prices.
                if not unit_value and legacy_total and len(quote_items) == 1:
                    unit_value = finite_float(legacy_total) / max(int(item['quantity'] or 1), 1)
                unit_price = finite_float(unit_value)
                name = name or item['product_name']
                brand = brand or item['requested_brand'] or item['brand'] or 'To be confirmed'
                model = model or item['requested_model'] or item['model'] or ''
                specification = specification or item['requested_size_spec'] or item['size_spec'] or item['product_name']
                if item['requested_notes'] and item['requested_notes'] not in specification:
                    specification = f'{specification} — {item["requested_notes"]}'
                if not name or not brand or not specification or unit_price <= 0:
                    raise ValueError('Complete the final item name, brand, specification and selling price for every appliance.')
                quantity = max(int(item['quantity'] or 1), 1)
                selling += unit_price * quantity
                item_updates.append((name, brand, model, specification, warranty, unit_price, item_id))
            selling = round(selling, 2)
            if selling <= 0 or supplier_cost <= 0 or selling < supplier_cost or delivery < 0:
                raise ValueError('Enter valid supplier cost, selling price and delivery fee.')
            for values in item_updates:
                db.execute('''UPDATE asset_order_items SET agreed_product_name=?,agreed_brand=?,
                    agreed_model=?,agreed_specification=?,agreed_warranty=?,unit_selling_price=?
                    WHERE id=?''', values)
            interest, total, monthly = finance_terms(selling, order['annual_interest_rate'], order['tenure_months'])
            valid_until = datetime.now() + timedelta(days=_setting_days(db, 'asset_quote_valid_days', 7))
            moved = db.execute('''UPDATE asset_orders SET status='awaiting_member_quote',quote_status='quoted',
                selling_subtotal=?,interest_amount=?,total_repayment=?,monthly_repayment=?,
                delivery_fee=?,procurement_cost=?,quote_note=?,quoted_at=?,quote_valid_until=? WHERE id=? AND status=?''',
                (selling, interest, total, monthly, delivery, supplier_cost,
                 request.form.get('quote_note','').strip(), datetime.now(), valid_until, order_id, 'sourcing_quote')).rowcount
            if moved != 1:
                raise ValueError('This request is no longer awaiting a quote.')
            # The request was assessed on the member's budget; now the real price is known, assess it again.
            borrower = db.execute('SELECT * FROM members WHERE id=?', (order['member_id'],)).fetchone()
            eligibility, eligibility_reason = asset_eligibility.review(
                db, borrower, monthly, total, order['repayment_method'], exclude_order_id=order_id)
            db.execute('UPDATE asset_orders SET eligibility_status=?,eligibility_reason=? WHERE id=?',
                       (eligibility, eligibility_reason, order_id))
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
                moved = db.execute("UPDATE asset_orders SET status='rejected',approval_stage='rejected' "
                                   "WHERE id=? AND approval_stage=?", (order_id, stage)).rowcount
                if moved != 1:
                    raise ValueError('This request has already moved on.')
                update_asset_documents(db, order_id, invoice_status='rejected', delivery_status='cancelled')
                _event(db, order_id, stage, 'rejected', comment)
            else:
                nxt = NEXT[stage]
                status = 'approved_for_procurement' if nxt == 'approved' else 'submitted'
                moved = db.execute('UPDATE asset_orders SET approval_stage=?,status=?,approved_at=CASE WHEN ?=\'approved\' THEN ? ELSE approved_at END '
                                   'WHERE id=? AND approval_stage=?',
                                   (nxt, status, nxt, datetime.now(), order_id, stage)).rowcount
                if moved != 1:
                    raise ValueError('This request has already moved on.')
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
            claimed = db.execute('''UPDATE asset_orders SET status='procured',procured_at=?,procurement_cost=?,
                procurement_account=?,procurement_reference=?,procurement_treatment='inventory',
                inventory_status='bought' WHERE id=? AND status='approved_for_procurement'
                AND procurement_journal_id IS NULL''',
                (datetime.now(), cost, account, reference, order_id)).rowcount
            if claimed != 1:
                raise ValueError('Procurement was already posted for this request.')
            journal_id = asset_accounting.post_purchase(db, order_id, order['order_number'], cost, account,
                                                        reference, current_user.id)
            db.execute('UPDATE asset_orders SET procurement_journal_id=? WHERE id=?', (journal_id, order_id))
            update_asset_documents(db, order_id, delivery_status='procured')
            _event(db, order_id, 'procurement', 'procured', comment)
        elif action == 'dispatch':
            if order['status'] != 'procured': raise ValueError('Record procurement first.')
            if not delivery_dispatch_allowed(order): raise ValueError('Delivery fee must be paid, waived, or marked pay-on-delivery.')
            moved = db.execute("UPDATE asset_orders SET status='ready_for_receipt',dispatched_at=? "
                               "WHERE id=? AND status='procured'", (datetime.now(), order_id)).rowcount
            if moved != 1:
                raise ValueError('This request has already been dispatched.')
            update_asset_documents(db, order_id, delivery_status='ready_for_delivery', ready=True)
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
                # Claim the fee before posting, so a second click cannot record the same money twice.
                claimed = db.execute("UPDATE asset_orders SET delivery_fee_status='paid' WHERE id=? "
                                     "AND delivery_fee_journal_id IS NULL AND delivery_fee_status <> 'paid'",
                                     (order_id,)).rowcount
                if claimed != 1:
                    raise ValueError('This delivery fee has already been recorded.')
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
        elif action == 'cancel':
            if current_user.role not in {'admin', 'treasurer'}:
                raise ValueError('Only the President/Admin or Treasurer may cancel a request.')
            if order['status'] not in CANCELLABLE_BEFORE_PURCHASE:
                raise ValueError('The supplier has already been paid for this request, so it needs the '
                                 '"cancel after purchase" step, which records what happens to the items.')
            if len(comment) < 5:
                raise ValueError('Give the reason for cancelling, so the member can be told.')
            _close_order(db, order_id, 'cancelled_by_cooperative')
            _event(db, order_id, 'cancellation', 'cancelled_by_cooperative', comment)
        elif action == 'cancel_after_purchase':
            if current_user.role != 'admin':
                raise ValueError('Only the President/Admin may cancel a request after the supplier has been paid.')
            if order['status'] not in CANCELLABLE_AFTER_PURCHASE:
                raise ValueError('This request has not reached purchase, or is already complete.')
            if len(comment) < 10:
                raise ValueError('Explain why the request is being cancelled after purchase.')
            outcome = request.form.get('purchase_outcome')
            if outcome not in {'returned', 'retained'}:
                raise ValueError('Say whether the items were returned to the supplier or kept by the cooperative.')
            claimed = db.execute("UPDATE asset_orders SET status='cancelled',approval_stage='closed',"
                                 "quote_status='cancelled_by_cooperative' WHERE id=? AND status IN ('procured','ready_for_receipt')",
                                 (order_id,)).rowcount
            if claimed != 1:
                raise ValueError('This request has already been cancelled or completed.')
            update_asset_documents(db, order_id, invoice_status='cancelled', delivery_status='cancelled')
            detail = comment
            new_stock_status = None
            if outcome == 'returned':
                refund = finite_float(request.form.get('refund_amount'))
                refund_account = request.form.get('refund_account', '').strip()
                refund_reference = request.form.get('refund_reference', '').strip()
                cost = round(float(order['procurement_cost'] or 0), 2)
                if refund <= 0 or refund > cost:
                    raise ValueError(f'The refund must be more than zero and no more than the purchase cost (₦{cost:,.2f}).')
                if refund_account not in {r['code'] for r in get_postable_cash_accounts(db)}:
                    raise ValueError('Select the bank or cash account that received the supplier refund.')
                if not refund_reference:
                    raise ValueError('Enter the supplier refund reference.')
                refund = round(refund, 2)
                post_journal_required(db, f"Asset purchase refunded - {order['order_number']}",
                                      asset_accounting.refund_lines(db, order, refund, refund_account, refund_reference),
                                      reference=refund_reference, source_module='asset_procurement_refund',
                                      source_id=order_id, created_by=current_user.id)
                unrecovered = round(cost - refund, 2)
                detail = f'{comment} | Returned to supplier; refund ₦{refund:,.2f} ({refund_reference}).'
                if unrecovered > 0 and asset_accounting.treatment(order) == asset_accounting.INVENTORY:
                    detail += f' Unrecovered cost ₦{unrecovered:,.2f} written off to expenses.'
                new_stock_status = 'returned'
            else:
                if asset_accounting.treatment(order) == asset_accounting.INVENTORY:
                    detail = f'{comment} | Items kept by the cooperative; the cost stays in Inventory as stock held for resale.'
                    new_stock_status = 'held'
                else:
                    detail = f'{comment} | Items kept by the cooperative; the purchase was expensed when paid.'
                    new_stock_status = None
            if asset_accounting.treatment(order) == asset_accounting.INVENTORY:
                db.execute('UPDATE asset_orders SET inventory_status=? WHERE id=?', (new_stock_status, order_id))
            _event(db, order_id, 'cancellation', f'cancelled_after_purchase_{outcome}', detail)
        else:
            raise ValueError('Unknown action.')
        updated = _order(db, order_id)
        if action == 'quote':
            notify_asset_member(db, updated, 'Your final asset quote is ready',
                                'Review the cooperative selling price, delivery fee and repayment schedule. Accept, decline, or request a revision.',
                                'action', _portal_url())
        elif action == 'reject':
            notify_asset_member(db, updated, 'Asset request not approved',
                                f'Your request was not approved. Reason: {comment}', 'warning', _portal_url())
        elif action == 'approve':
            if updated['approval_stage'] == 'treasurer':
                next_roles = ('treasurer',); next_label = 'Treasurer verification'
            elif updated['approval_stage'] == 'president':
                next_roles = ('admin',); next_label = 'President approval'
            else:
                next_roles = ('admin', 'treasurer'); next_label = 'procurement'
            notify_asset_member(db, updated, 'Asset request moved forward',
                                f'Your request passed {stage.title()} review and is now awaiting {next_label}.',
                                'success', _portal_url())
            notify_asset_roles(db, updated, next_roles, 'Asset request requires your action',
                               f'{updated["order_number"]} is awaiting {next_label}.', 'action', _portal_url())
        elif action in {'cancel', 'cancel_after_purchase'}:
            notify_asset_member(db, updated, 'Your asset request was cancelled',
                                f'The cooperative has cancelled this request. Reason: {comment}. '
                                'No loan was created and you owe nothing for it.', 'warning', _portal_url())
            audit(db, 'ASSET_REQUEST_CANCELLED_BY_COOPERATIVE', 'asset_schemes',
                  f'{order["order_number"]} ({action}): {comment}')
        elif action == 'procured':
            notify_asset_member(db, updated, 'Your appliances have been procured',
                                'The cooperative has purchased your approved items and is preparing them for delivery.',
                                'success', _portal_url())
        elif action == 'dispatch':
            notify_asset_member(db, updated, 'Your appliances are ready',
                                'Your items are ready for collection or delivery. Confirm receipt in CoopMS only after you receive them.',
                                'action', _portal_url())
        elif action == 'delivery_fee':
            notify_asset_member(db, updated, 'Delivery fee status updated',
                                f'Your delivery fee is now recorded as {updated["delivery_fee_status"].replace("_", " ")}.',
                                'info', _portal_url())
        db.commit(); flash('Asset request updated.', 'success')
    except Exception as exc:
        db.rollback(); flash(str(exc), 'danger')
    return redirect(url_for('asset_schemes.order_detail', order_id=order_id))


@asset_schemes.route('/asset-schemes/inventory')
@login_required
@role_required('admin', 'treasurer')
def inventory():
    """Stock held for asset requests, reconciled to the ledger, and earlier purchases awaiting reclassification."""
    db = get_db()
    try:
        held_account = asset_accounting.accounts(db, 'inventory')['inventory']
        problem = ''
    except ValueError as exc:
        held_account, problem = None, str(exc)
    stock, stock_total = asset_accounting.stock_on_hand(db)
    ledger_total = asset_accounting.ledger_balance(db, held_account) if held_account else 0.0
    to_reclassify = asset_accounting.open_expensed_purchases(db)
    return render_template('asset_schemes/inventory.html', stock=stock, stock_total=stock_total,
                           ledger_total=ledger_total, difference=round(ledger_total - stock_total, 2),
                           held_account=held_account, problem=problem, to_reclassify=to_reclassify,
                           reclass_total=round(sum(float(o['procurement_cost'] or 0) for o in to_reclassify), 2),
                           today=datetime.now().strftime('%Y-%m-%d'))


@asset_schemes.route('/asset-schemes/inventory/reclassify', methods=['POST'])
@login_required
@role_required('admin', 'treasurer')
def reclassify_inventory():
    """One-off: move purchases that were expensed when paid, and are still undelivered, into Inventory."""
    db = get_db()
    try:
        date = datetime.strptime(request.form.get('posting_date', ''), '%Y-%m-%d')
        wanted = {int(x) for x in request.form.getlist('order_ids') if x.isdigit()}
        orders = [o for o in asset_accounting.open_expensed_purchases(db) if o['id'] in wanted]
        if not orders:
            raise ValueError('Select at least one purchase to reclassify.')
        for order in orders:
            asset_accounting.reclassify(db, order, date, current_user.id)
        audit(db, 'ASSET_INVENTORY_RECLASSIFIED', 'asset_schemes',
              f'{len(orders)} purchase(s) moved from expenses to inventory as at {date:%Y-%m-%d}: '
              + ', '.join(o['order_number'] for o in orders))
        db.commit()
        flash(f'{len(orders)} purchase(s) moved from Operating Expenses to Inventory.', 'success')
    except Exception as exc:
        db.rollback()
        flash(str(exc), 'danger')
    return redirect(url_for('asset_schemes.inventory'))


@asset_schemes.route('/asset-schemes/orders/<int:order_id>/cancel', methods=['POST'])
@login_required
def cancel_request(order_id):
    db = get_db(); order = _order(db, order_id, lock=True); member = member_for_user(db)
    try:
        if not order or not member or order['member_id'] != member['id']:
            raise ValueError('You cannot cancel this request.')
        if not member_can_cancel(order):
            raise ValueError('The cooperative has already bought these items, so this request can no longer be '
                             'cancelled online. Please contact the cooperative office.')
        reason = request.form.get('reason', '').strip()
        if len(reason) < 5:
            raise ValueError('Tell the cooperative why you are cancelling this request.')
        placeholders = ','.join('?' for _ in CANCELLABLE_BEFORE_PURCHASE)
        changed = db.execute(f"""UPDATE asset_orders SET status='cancelled',approval_stage='closed',
            quote_status='cancelled_by_member' WHERE id=? AND status IN ({placeholders})""",
                             (order_id, *CANCELLABLE_BEFORE_PURCHASE)).rowcount
        if changed != 1:
            raise ValueError('This request can no longer be cancelled.')
        update_asset_documents(db, order_id, invoice_status='cancelled', delivery_status='cancelled')
        _event(db, order_id, 'member', 'cancelled', reason)
        updated = _order(db, order_id)
        notify_asset_member(db, updated, 'Asset request cancelled',
                            'Your request has been closed. No loan was created and nothing is owed for it.',
                            'info', _portal_url())
        notify_asset_roles(db, updated, ('admin', 'treasurer', 'secretary'), 'Member cancelled asset request',
                           f'{updated["order_number"]} was cancelled by the member before purchase. Reason: {reason}',
                           'info', _portal_url())
        db.commit(); audit(db, 'ASSET_REQUEST_CANCELLED_BY_MEMBER', 'asset_schemes',
                           f'{order["order_number"]}: {reason}')
        flash('Asset request cancelled. No loan was created.', 'success')
    except Exception as exc:
        db.rollback(); flash(str(exc), 'danger')
    return redirect(url_for('asset_schemes.order_detail', order_id=order_id))


@asset_schemes.route('/asset-schemes/orders/<int:order_id>/accept-quote', methods=['POST'])
@login_required
def accept_quote(order_id):
    db = get_db(); order = _order(db, order_id, lock=True); member = member_for_user(db)
    try:
        if not order or not member or order['member_id'] != member['id']:
            raise ValueError('You cannot accept this quote.')
        if order['status'] != 'awaiting_member_quote' or order['quote_status'] != 'quoted':
            raise ValueError('There is no final quote awaiting acceptance.')
        if request.form.get('accept_quote') != '1':
            raise ValueError('Confirm that you accept the final quote and repayment schedule.')
        if quote_expired(order):
            raise ValueError(f'This quote expired on {quote_expiry(order):%d %b %Y}. Ask for a revised quote and the '
                             'cooperative will refresh the price.')
        snapshot = json.dumps(schedule_snapshot(order['selling_subtotal'], order['annual_interest_rate'],
                                                order['tenure_months']))
        accepted = db.execute("""UPDATE asset_orders SET status='submitted',approval_stage='secretary',
            quote_status='accepted',quote_accepted_at=?,repayment_schedule_snapshot=?
            WHERE id=? AND status='awaiting_member_quote'""", (datetime.now(), snapshot, order_id)).rowcount
        if accepted != 1:
            raise ValueError('This quote is no longer awaiting acceptance.')
        invoice, delivery_order = ensure_asset_documents(db, order_id)
        _event(db, order_id, 'quote', 'accepted', 'Member accepted final selling price and repayment schedule')
        _event(db, order_id, 'documents', 'invoice_generated', invoice['invoice_number'])
        _event(db, order_id, 'documents', 'delivery_order_generated', delivery_order['delivery_number'])
        updated = _order(db, order_id)
        notify_asset_member(db, updated, 'Final quote accepted',
                            f'Invoice {invoice["invoice_number"]} and delivery order {delivery_order["delivery_number"]} are ready. Your request is awaiting Secretary review; the loan begins only after you confirm delivery.',
                            'success', _portal_url())
        notify_asset_roles(db, updated, ('secretary',), 'Asset request awaiting Secretary review',
                           f'{updated["order_number"]} has been accepted by the member and requires your review.',
                           'action', _portal_url())
        db.commit(); audit(db, 'ASSET_QUOTE_ACCEPTED', 'asset_schemes', order['order_number'])
        flash('Final quote accepted. Your request has entered the approval workflow.', 'success')
    except Exception as exc:
        db.rollback(); flash(str(exc), 'danger')
    return redirect(url_for('asset_schemes.order_detail', order_id=order_id))


@asset_schemes.route('/asset-schemes/orders/<int:order_id>/decline-quote', methods=['POST'])
@login_required
def decline_quote(order_id):
    db = get_db(); order = _order(db, order_id); member = member_for_user(db)
    try:
        if not order or not member or order['member_id'] != member['id']:
            raise ValueError('You cannot decline this quote.')
        if order['status'] != 'awaiting_member_quote' or order['quote_status'] != 'quoted':
            raise ValueError('There is no final quote awaiting your response.')
        reason = request.form.get('reason', '').strip() or 'Member declined the final quote.'
        db.execute("""UPDATE asset_orders SET status='cancelled',approval_stage='closed',
            quote_status='declined' WHERE id=?""", (order_id,))
        _event(db, order_id, 'quote', 'declined', reason)
        updated = _order(db, order_id)
        notify_asset_member(db, updated, 'Asset quote declined',
                            'Your request has been closed and no loan or accounting entry was created.',
                            'info', _portal_url())
        notify_asset_roles(db, updated, ('admin', 'treasurer'), 'Member declined asset quote',
                           f'{updated["order_number"]} was declined. Reason: {reason}',
                           'info', _portal_url())
        db.commit(); audit(db, 'ASSET_QUOTE_DECLINED', 'asset_schemes', f'{order["order_number"]}: {reason}')
        flash('Quote declined. The request is closed and no loan was created.', 'success')
    except Exception as exc:
        db.rollback(); flash(str(exc), 'danger')
    return redirect(url_for('asset_schemes.order_detail', order_id=order_id))


@asset_schemes.route('/asset-schemes/orders/<int:order_id>/request-revision', methods=['POST'])
@login_required
def request_quote_revision(order_id):
    db = get_db(); order = _order(db, order_id); member = member_for_user(db)
    try:
        if not order or not member or order['member_id'] != member['id']:
            raise ValueError('You cannot request a revision for this quote.')
        if order['status'] != 'awaiting_member_quote' or order['quote_status'] != 'quoted':
            raise ValueError('There is no final quote awaiting your response.')
        reason = request.form.get('reason', '').strip()
        if len(reason) < 5:
            raise ValueError('Tell the cooperative what should be changed in the quote.')
        db.execute("""UPDATE asset_orders SET status='sourcing_quote',approval_stage='quote',
            quote_status='revision_requested' WHERE id=?""", (order_id,))
        _event(db, order_id, 'quote', 'revision_requested', reason)
        updated = _order(db, order_id)
        notify_asset_member(db, updated, 'Quote revision requested',
                            'Your request has returned to the sourcing team. You will be notified when a revised quote is ready.',
                            'success', _portal_url())
        notify_asset_roles(db, updated, ('admin', 'treasurer'), 'Asset quote revision requested',
                           f'{updated["order_number"]} needs a revised quote. Member note: {reason}',
                           'action', _portal_url())
        db.commit(); audit(db, 'ASSET_QUOTE_REVISION_REQUESTED', 'asset_schemes', f'{order["order_number"]}: {reason}')
        flash('Revision requested. The sourcing team has been notified.', 'success')
    except Exception as exc:
        db.rollback(); flash(str(exc), 'danger')
    return redirect(url_for('asset_schemes.order_detail', order_id=order_id))


@asset_schemes.route('/asset-schemes/orders/<int:order_id>/<document_type>.pdf')
@login_required
def asset_document_pdf(order_id, document_type):
    if document_type not in {'invoice', 'delivery-order'}:
        return jsonify({'error': 'Document not found.'}), 404
    db = get_db(); order = _order(db, order_id); member = member_for_user(db)
    staff = current_user.role in {'admin', 'treasurer', 'secretary', 'exco'}
    if not order or (not staff and (not member or order['member_id'] != member['id'])):
        return jsonify({'error': 'Document not found.'}), 404
    try:
        pdf_type = 'invoice' if document_type == 'invoice' else 'delivery_order'
        buffer, number = render_asset_pdf(db, order_id, pdf_type)
        filename = f'{document_type}_{number.replace("/", "_")}.pdf'
        return send_file(buffer, mimetype='application/pdf', as_attachment=True, download_name=filename)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 404


@asset_schemes.route('/asset-schemes/orders/<int:order_id>/confirm-receipt', methods=['POST'])
@login_required
def confirm_receipt(order_id):
    db = get_db(); order = _order(db, order_id, lock=True); member = member_for_user(db)
    try:
        if not order or not member or order['member_id'] != member['id']:
            raise ValueError('You cannot confirm this order.')
        loan_id, loan_number = activate_delivered_asset(
            db, order, current_user.id, current_user.username, 'asset_scheme_member')
        updated = _order(db, order_id)
        notify_asset_member(db, updated, 'Receipt confirmed and repayment activated',
                            f'Thank you for confirming delivery. Loan {loan_number} is now active and will follow the agreed repayment schedule.',
                            'success', _portal_url())
        notify_asset_roles(db, updated, ('admin', 'secretary', 'treasurer', 'exco'),
                           'Member confirmed asset receipt',
                           f'{updated["order_number"]} was received and {loan_number} is now active.',
                           'success', _portal_url())
        db.commit(); audit(db, 'ASSET_RECEIPT_CONFIRMED', 'asset_schemes', f'{order["order_number"]} -> {loan_number}')
        flash('Receipt confirmed. Your repayment plan is now active.', 'success')
    except Exception as exc:
        db.rollback(); flash(str(exc), 'danger')
    return redirect(url_for('asset_schemes.order_detail', order_id=order_id))


@asset_schemes.route('/asset-schemes/orders/<int:order_id>/confirm-receipt-officer', methods=['POST'])
@login_required
@role_required('admin', 'treasurer', 'secretary')
def confirm_receipt_officer(order_id):
    db = get_db(); order = _order(db, order_id, lock=True)
    try:
        if not order:
            raise ValueError('Asset request not found.')
        if _acting_on_own_order(db, order):
            raise ValueError('You cannot confirm delivery of your own asset request.')
        _, blocked = _officer_confirm_rules(db, order)
        if blocked:
            raise ValueError(blocked)
        note = request.form.get('confirmation_note', '').strip()
        if len(note) < 10:
            raise ValueError('Enter a delivery note or evidence reference (at least 10 characters) before '
                             'confirming for the member.')
        loan_id, loan_number = activate_delivered_asset(
            db, order, current_user.id, current_user.username, 'asset_scheme_officer',
            f'Officer confirmed on behalf of member: {note}')
        updated = _order(db, order_id)
        notify_asset_member(db, updated, 'Delivery confirmed by cooperative officer',
                            f'{current_user.username} confirmed delivery on your behalf. Loan {loan_number} is now active. Contact the cooperative immediately if you have not received the items.',
                            'warning', _portal_url())
        notify_asset_roles(db, updated, ('admin', 'secretary', 'treasurer', 'exco'),
                           'Officer confirmed asset delivery',
                           f'{current_user.username} confirmed {updated["order_number"]} on behalf of the member. {loan_number} is now active.',
                           'warning', _portal_url())
        db.commit(); audit(db, 'ASSET_RECEIPT_CONFIRMED_BY_OFFICER', 'asset_schemes',
                           f'{order["order_number"]} -> {loan_number}; {note}')
        flash('Delivery confirmed for the member. The asset loan is now active.', 'success')
    except Exception as exc:
        db.rollback(); flash(str(exc), 'danger')
    return redirect(url_for('asset_schemes.order_detail', order_id=order_id))
