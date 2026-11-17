import random
from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from asset_schemes import delivery_cleared, delivery_dispatch_allowed, finance_terms
from database import get_db, last_insert_id
from ledger import (FEE_INCOME, LOANS_RECEIVABLE, OPERATING_EXPENSES,
                    get_postable_cash_accounts, post_journal_required)
import loan_workflow as lw
from utils import audit, finite_float, member_for_user, notify_member, role_required


asset_schemes = Blueprint('asset_schemes', __name__)
STAGES = ('secretary', 'treasurer', 'president')
NEXT = {'secretary': 'treasurer', 'treasurer': 'president', 'president': 'approved'}


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
    campaigns = db.execute("SELECT * FROM asset_campaigns WHERE status='open' ORDER BY id DESC").fetchall()
    products = db.execute('''SELECT p.* FROM asset_products p JOIN asset_campaigns c ON c.id=p.campaign_id
        WHERE p.status='active' AND c.status='open' ORDER BY p.campaign_id DESC, p.name''').fetchall()
    orders = db.execute('SELECT * FROM asset_orders WHERE member_id=? ORDER BY id DESC', (member['id'],)).fetchall()
    return render_template('asset_schemes/catalogue.html', member=member, campaigns=campaigns,
                           products=products, orders=orders)


@asset_schemes.route('/asset-schemes/apply/<int:campaign_id>', methods=['POST'])
@login_required
def apply(campaign_id):
    db = get_db()
    member = member_for_user(db)
    campaign = db.execute("SELECT * FROM asset_campaigns WHERE id=? AND status='open'", (campaign_id,)).fetchone()
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
            db.execute('''INSERT INTO asset_campaigns
                (name,description,status,tenure_months,annual_interest_rate,repayment_methods,created_by)
                VALUES (?,?,?,?,?,?,?)''', (name, request.form.get('description','').strip(),
                request.form.get('status','draft'), tenure, rate, methods, current_user.id))
            db.commit()
            flash('Asset scheme created.', 'success')
        except Exception as exc:
            db.rollback(); flash(str(exc), 'danger')
        return redirect(url_for('asset_schemes.manage'))
    campaigns = db.execute('SELECT * FROM asset_campaigns ORDER BY id DESC').fetchall()
    orders = db.execute('''SELECT o.*,m.first_name,m.last_name,c.name campaign_name FROM asset_orders o
        JOIN members m ON m.id=o.member_id JOIN asset_campaigns c ON c.id=o.campaign_id ORDER BY o.id DESC''').fetchall()
    return render_template('asset_schemes/manage.html', campaigns=campaigns, orders=orders)


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
