from decimal import Decimal

import pytest

from asset_schemes import delivery_cleared, delivery_dispatch_allowed, finance_terms, money


def test_six_month_twenty_percent_finance_charge_is_ten_percent():
    interest, total, monthly = finance_terms(500_000, 20, 6)
    assert interest == 50_000
    assert total == 550_000
    assert monthly == 91_666.67


def test_delivery_is_not_part_of_financed_amount():
    # Changing delivery has no input into the finance calculation by design.
    assert finance_terms(500_000, 20, 6) == finance_terms(500_000, 20, 6)
    assert money(12_500) == Decimal('12500.00')


@pytest.mark.parametrize('status', ['paid', 'waived'])
def test_delivery_can_be_cleared_by_supported_treatment(status):
    assert delivery_cleared({'delivery_fee': 10_000, 'delivery_fee_status': status})


def test_unpaid_delivery_blocks_dispatch():
    assert not delivery_cleared({'delivery_fee': 10_000, 'delivery_fee_status': 'unpaid'})
    assert delivery_cleared({'delivery_fee': 0, 'delivery_fee_status': 'unpaid'})


def test_pay_on_delivery_allows_dispatch_but_not_receipt_activation():
    order = {'delivery_fee': 10_000, 'delivery_fee_status': 'pay_on_delivery'}
    assert delivery_dispatch_allowed(order)
    assert not delivery_cleared(order)


@pytest.mark.parametrize('principal,rate,months', [(0, 20, 6), (1, -1, 6), (1, 20, 0), (1, 20, 61)])
def test_invalid_finance_terms_are_rejected(principal, rate, months):
    with pytest.raises(ValueError):
        finance_terms(principal, rate, months)


def test_request_quote_acceptance_gate():
    import os
    from werkzeug.security import generate_password_hash
    os.environ.setdefault('SECRET_KEY', 'asset-scheme-test-secret')
    os.environ.setdefault('ADMIN_PASSWORD', 'TestAdmin123')
    os.environ.setdefault('FLASK_DEBUG', '1')
    os.environ.pop('DATABASE_URL', None)
    os.environ.setdefault('SQLITE_DB_PATH', os.path.abspath('.test-asset-schemes.db'))
    import app as app_module
    from database import get_db, last_insert_id

    app = app_module.app
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    email = 'asset-flow@example.test'
    with app.app_context():
        db = get_db()
        db.execute('DELETE FROM asset_order_events')
        db.execute('DELETE FROM asset_order_items')
        db.execute('DELETE FROM asset_orders')
        db.execute('DELETE FROM asset_products')
        db.execute('DELETE FROM asset_campaigns')
        db.execute('DELETE FROM members WHERE email=?', (email,))
        db.execute('DELETE FROM users WHERE username=?', (email,))
        db.execute("INSERT INTO users (username,email,password_hash,role,is_active) VALUES (?,?,?,'member',1)",
                   (email, email, generate_password_hash('MemberPass123!')))
        db.execute("""INSERT INTO members
            (member_number,first_name,last_name,email,status,annual_salary)
            VALUES ('ASSET-TEST','Asset','Member',?,'active',2400000)""", (email,))
        db.execute("""INSERT INTO asset_campaigns
            (name,status,tenure_months,annual_interest_rate,repayment_methods)
            VALUES ('Year End','open',6,20,'salary_deduction')""")
        campaign_id = last_insert_id(db)
        db.execute("""INSERT INTO asset_products
            (campaign_id,name,category,brand,size_spec,selling_price,estimated_price_min,estimated_price_max)
            VALUES (?,'Smart TV','Television','Any','55 inch',0,350000,500000)""", (campaign_id,))
        product_id = last_insert_id(db)
        db.commit()

    client = app.test_client()
    assert client.post('/login', data={'username': email, 'password': 'MemberPass123!'}).status_code in (302, 303)
    catalogue = client.get('/asset-schemes')
    assert catalogue.status_code == 200
    assert b'What would you like us to source?' in catalogue.data
    response = client.post(f'/asset-schemes/apply/{campaign_id}', data={
        f'qty_{product_id}': '1', f'budget_{product_id}': '500000',
        'repayment_method': 'salary_deduction', 'signature_name': 'Asset Member',
        'terms_accepted': '1',
    })
    assert response.status_code in (302, 303)
    with app.app_context():
        db = get_db()
        order = db.execute('SELECT * FROM asset_orders ORDER BY id DESC LIMIT 1').fetchone()
        order_id = order['id']
        assert order['status'] == 'sourcing_quote'
        assert order['approval_stage'] == 'quote'
        assert order['expected_spend'] == 500000

    client.get('/logout')
    assert client.post('/login', data={'username': 'admin', 'password': 'TestAdmin123'}).status_code in (302, 303)
    manage_page = client.get('/asset-schemes/manage')
    assert manage_page.status_code == 200
    assert b'Requests, quotations and fulfilment' in manage_page.data
    assert b'Master appliance library' in manage_page.data
    with app.app_context():
        db = get_db()
        library_item = db.execute("SELECT id FROM asset_catalogue WHERE category='Refrigerator' ORDER BY id LIMIT 1").fetchone()
        assert library_item is not None
        library_id = library_item['id']
    added = client.post(f'/asset-schemes/campaigns/{campaign_id}/library',
                        data={'library_ids': str(library_id)})
    assert added.status_code in (302, 303)
    with app.app_context():
        db = get_db()
        count = db.execute('SELECT COUNT(*) FROM asset_products WHERE campaign_id=?', (campaign_id,)).fetchone()[0]
        assert count == 2
    client.post(f'/asset-schemes/campaigns/{campaign_id}/library',
                data={'library_ids': str(library_id)})
    with app.app_context():
        count = get_db().execute('SELECT COUNT(*) FROM asset_products WHERE campaign_id=?', (campaign_id,)).fetchone()[0]
        assert count == 2
    response = client.post(f'/asset-schemes/orders/{order_id}/action', data={
        'action': 'quote', 'quoted_supplier_cost': '450000',
        'quoted_selling_price': '500000', 'quoted_delivery_fee': '15000',
        'quote_note': '55 inch television with one-year warranty',
    })
    assert response.status_code in (302, 303)
    with app.app_context():
        order = get_db().execute('SELECT * FROM asset_orders WHERE id=?', (order_id,)).fetchone()
        assert order['status'] == 'awaiting_member_quote'
        assert order['total_repayment'] == 550000
        assert order['delivery_fee'] == 15000

    assert client.get(f'/asset-schemes/orders/{order_id}').status_code == 200

    client.get('/logout')
    client.post('/login', data={'username': email, 'password': 'MemberPass123!'})
    response = client.post(f'/asset-schemes/orders/{order_id}/accept-quote', data={'accept_quote': '1'})
    assert response.status_code in (302, 303)
    with app.app_context():
        order = get_db().execute('SELECT * FROM asset_orders WHERE id=?', (order_id,)).fetchone()
        assert order['status'] == 'submitted'
        assert order['approval_stage'] == 'secretary'
        assert order['quote_status'] == 'accepted'
