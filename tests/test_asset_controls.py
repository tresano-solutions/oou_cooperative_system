"""Asset scheme controls, cancellation and loan set-up. Run separately: isolated database."""
import json
import os
import re
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

_directory = tempfile.TemporaryDirectory(prefix='asset-controls-', dir=os.path.abspath('outputs'))
os.environ['SQLITE_DB_PATH'] = os.path.join(_directory.name, 'test.db')
os.environ.pop('DATABASE_URL', None)
os.environ['SECRET_KEY'] = 'asset-controls-test-key'
os.environ['ADMIN_PASSWORD'] = 'TestAdmin123'
os.environ['FLASK_DEBUG'] = '1'

from werkzeug.security import generate_password_hash

import repayment_adapter
import repayment_start
from app import app
from asset_documents import ensure_asset_documents
from database import get_db, last_insert_id
from ledger import get_postable_cash_accounts

PASSWORD = 'AssetControls123!'


class AssetControlTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = app.test_client()
        self.tag = uuid.uuid4().hex[:8]
        # The room for this test: a member (with a login), a treasurer and an admin.
        with app.app_context():
            db = get_db()
            self.member_email = f'member-{self.tag}@example.test'
            self.users = {}
            for role in ('member', 'treasurer', 'secretary'):
                username = f'{role}-{self.tag}@example.test'
                db.execute("INSERT INTO users (username,email,password_hash,role,is_active) VALUES (?,?,?,?,1)",
                           (username, self.member_email if role == 'member' else username,
                            generate_password_hash(PASSWORD), role))
                self.users[role] = (username, last_insert_id(db))
            db.execute("""INSERT INTO members (member_number,first_name,last_name,email,status,annual_salary)
                VALUES (?,?,?,?,'active',3600000)""", (f'AC-{self.tag}', 'Asset', 'Member', self.member_email))
            self.member_id = last_insert_id(db)
            db.execute("INSERT INTO asset_campaigns (name,status,tenure_months,annual_interest_rate,closes_at,repayment_methods) "
                       "VALUES (?, 'open', 6, 20, ?, 'salary_deduction,cash')",
                       (f'Campaign {self.tag}', datetime.now() + timedelta(days=30)))
            self.campaign_id = last_insert_id(db)
            db.execute("INSERT INTO asset_products (campaign_id,name,category,brand,size_spec,selling_price,estimated_price_min,estimated_price_max) "
                       "VALUES (?, 'Smart TV','Television','Any','55 inch',0,300000,600000)", (self.campaign_id,))
            self.product_id = last_insert_id(db)
            cash = get_postable_cash_accounts(db)
            self.cash_account = cash[0]['code']
            db.commit()

    # -- helpers -------------------------------------------------------------------------------
    def login(self, who):
        self.client.get('/logout')
        if who == 'admin':
            self.client.post('/login', data={'username': 'admin', 'password': 'TestAdmin123'})
        else:
            self.client.post('/login', data={'username': self.users[who][0], 'password': PASSWORD})

    def make_order(self, status, stage='approved', member_id=None, accepted=True, **columns):
        values = dict(selling_subtotal=500000, annual_interest_rate=20, tenure_months=6, interest_amount=50000,
                      total_repayment=550000, monthly_repayment=91666.67, repayment_method='salary_deduction',
                      expected_spend=500000, procurement_cost=450000, quote_status='accepted' if accepted else 'quoted',
                      delivery_fee=0, delivery_fee_status='unpaid', signature_name='Asset Member', terms_accepted=1)
        values.update(columns)
        with app.app_context():
            db = get_db()
            number = f'AS/T/{uuid.uuid4().hex[:10]}'
            db.execute(f"""INSERT INTO asset_orders (order_number,campaign_id,member_id,status,approval_stage,
                {','.join(values)}) VALUES (?,?,?,?,?,{','.join('?' for _ in values)})""",
                       (number, self.campaign_id, member_id or self.member_id, status, stage, *values.values()))
            order_id = last_insert_id(db)
            db.execute("""INSERT INTO asset_order_items (order_id,product_id,product_name,quantity,unit_selling_price,
                expected_unit_spend) VALUES (?,?,?,?,?,?)""", (order_id, self.product_id, 'Smart TV', 1, 500000, 500000))
            if accepted:
                ensure_asset_documents(db, order_id)
            db.commit()
        return order_id

    def order(self, order_id):
        with app.app_context():
            return dict(get_db().execute('SELECT * FROM asset_orders WHERE id=?', (order_id,)).fetchone())

    def act(self, order_id, **data):
        return self.client.post(f'/asset-schemes/orders/{order_id}/action', data=data)

    def journals(self, module, order_id):
        with app.app_context():
            return get_db().execute('SELECT COUNT(*) FROM journal_entries WHERE source_module=? AND source_id=?',
                                    (module, order_id)).fetchone()[0]

    def add_event(self, order_id, action, user_key):
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO asset_order_events (order_id,stage,action,actor_id,actor_name) VALUES (?,?,?,?,?)",
                       (order_id, 'delivery', action, self.users[user_key][1], user_key))
            db.commit()

    # -- an officer cannot act on their own request ----------------------------------------------
    def test_officer_cannot_act_on_their_own_request(self):
        with app.app_context():
            db = get_db()
            admin = db.execute("SELECT email FROM users WHERE username='admin'").fetchone()
            email = admin['email'] or f'admin-{self.tag}@example.test'
            db.execute("UPDATE users SET email=? WHERE username='admin'", (email,))
            db.execute("""INSERT INTO members (member_number,first_name,last_name,email,status)
                VALUES (?,?,?,?,'active')""", (f'OWN-{self.tag}', 'Own', 'Order', email))
            own_member = last_insert_id(db)
            db.commit()
        order_id = self.make_order('approved_for_procurement', member_id=own_member)
        self.login('admin')
        self.act(order_id, action='procured', procurement_account=self.cash_account, procurement_reference='SUP-1')
        self.assertEqual(self.order(order_id)['status'], 'approved_for_procurement')
        self.assertEqual(self.journals('asset_procurement', order_id), 0)
        self.act(order_id, action='cancel', comment='Cancelling my own request')
        self.assertEqual(self.order(order_id)['status'], 'approved_for_procurement')
        with app.app_context():
            get_db().execute("UPDATE users SET email=? WHERE username='admin'", (f'admin-{self.tag}@elsewhere.test',))
            get_db().commit()

    def test_officer_cannot_confirm_delivery_of_their_own_request(self):
        with app.app_context():
            db = get_db()
            email = self.users['treasurer'][0]
            db.execute("""INSERT INTO members (member_number,first_name,last_name,email,status)
                VALUES (?,?,?,?,'active')""", (f'OWN2-{self.tag}', 'Treasurer', 'Member', email))
            own_member = last_insert_id(db)
            db.commit()
        order_id = self.make_order('ready_for_receipt', member_id=own_member,
                                   dispatched_at=datetime.now() - timedelta(days=10))
        self.login('treasurer')
        self.client.post(f'/asset-schemes/orders/{order_id}/confirm-receipt-officer',
                         data={'confirmation_note': 'Collected from the office yesterday'})
        self.assertEqual(self.order(order_id)['status'], 'ready_for_receipt')

    # -- money steps happen once -----------------------------------------------------------------
    def test_procurement_is_posted_once_and_not_by_the_secretary(self):
        order_id = self.make_order('approved_for_procurement')
        data = dict(action='procured', procurement_account=self.cash_account, procurement_reference='SUP-100')
        self.login('secretary')
        self.act(order_id, **data)
        self.assertEqual(self.order(order_id)['status'], 'approved_for_procurement')
        self.login('treasurer')
        self.act(order_id, **data)
        self.act(order_id, **data)          # a second click, or a second officer
        self.assertEqual(self.order(order_id)['status'], 'procured')
        self.assertEqual(self.journals('asset_procurement', order_id), 1)

    def test_delivery_fee_is_recorded_once(self):
        order_id = self.make_order('procured', delivery_fee=15000)
        self.login('treasurer')
        data = dict(action='delivery_fee', delivery_fee_status='paid', delivery_fee_account=self.cash_account,
                    delivery_fee_reference='FEE-1')
        self.act(order_id, **data)
        self.act(order_id, **data)
        self.assertEqual(self.journals('asset_delivery_fee', order_id), 1)

    # -- confirming delivery on a member's behalf ------------------------------------------------
    def confirm_for_member(self, order_id, user='treasurer', note='Collected from the office; signed delivery note 55'):
        self.login(user)
        return self.client.post(f'/asset-schemes/orders/{order_id}/confirm-receipt-officer',
                                data={'confirmation_note': note})

    def test_officer_confirmation_waits_for_the_member_to_have_a_chance(self):
        order_id = self.make_order('ready_for_receipt', dispatched_at=datetime.now() - timedelta(hours=5))
        with patch.dict(os.environ, {'ENFORCE_SEGREGATION_OF_DUTIES': '0'}):
            self.confirm_for_member(order_id)
            self.assertEqual(self.order(order_id)['status'], 'ready_for_receipt')
            self.assertIsNone(self.order(order_id)['linked_loan_id'])
            with app.app_context():
                get_db().execute('UPDATE asset_orders SET dispatched_at=? WHERE id=?',
                                 (datetime.now() - timedelta(days=4), order_id))
                get_db().commit()
            self.confirm_for_member(order_id, note='too short')      # evidence note must be real
            self.assertEqual(self.order(order_id)['status'], 'ready_for_receipt')
            self.confirm_for_member(order_id)
            self.assertEqual(self.order(order_id)['status'], 'received')
            self.assertIsNotNone(self.order(order_id)['linked_loan_id'])

    def test_officer_who_dispatched_cannot_also_confirm_for_the_member(self):
        order_id = self.make_order('ready_for_receipt', dispatched_at=datetime.now() - timedelta(days=10))
        self.add_event(order_id, 'dispatched', 'treasurer')
        with patch.dict(os.environ, {'ENFORCE_SEGREGATION_OF_DUTIES': '1'}):
            self.confirm_for_member(order_id, user='treasurer')
            self.assertEqual(self.order(order_id)['status'], 'ready_for_receipt')
            self.confirm_for_member(order_id, user='secretary')
            self.assertEqual(self.order(order_id)['status'], 'received')

    # -- the loan is set up so every other tool can follow it ------------------------------------
    def test_activated_loan_has_first_payment_date_schedule_and_tidy_number(self):
        snapshot = json.dumps(__import__('asset_schemes').schedule_snapshot(500000, 20, 6))
        order_id = self.make_order('ready_for_receipt', dispatched_at=datetime.now() - timedelta(days=10),
                                   repayment_schedule_snapshot=snapshot)
        with patch.dict(os.environ, {'ENFORCE_SEGREGATION_OF_DUTIES': '0'}):
            self.confirm_for_member(order_id)
        with app.app_context():
            loan = dict(get_db().execute('SELECT l.* FROM loans l JOIN asset_orders o ON o.linked_loan_id=l.id '
                                         'WHERE o.id=?', (order_id,)).fetchone())
        self.assertRegex(loan['loan_number'], r'^LOAN/AS/\d{8}/\d{5}$')
        first = datetime.fromisoformat(str(loan['first_payment_date'])[:19])
        # Tied to the monthly payroll run: the last day of the first payroll month, not "30 days on".
        with app.app_context():
            cutoff = repayment_start.cutoff_day(get_db())
        expected = repayment_start.first_repayment_date(datetime.now(), cutoff)
        self.assertEqual(first.date(), expected.date())
        saved = json.loads(loan['repayment_schedule_snapshot'])
        self.assertEqual(len(saved['schedule']), 6)
        self.assertAlmostEqual(sum(r['payment'] for r in saved['schedule']), 550000, places=2)
        self.assertEqual(loan['repayment_schedule_accepted'], 1)
        # The payroll adapter can now suggest the instalment for the month it falls due.
        due_month = f'{first:%Y-%m}'
        expected, basis = repayment_adapter.expected_payment(loan, due_month)
        self.assertEqual(str(expected), '91666.67')
        self.assertEqual(basis, 'Accepted repayment schedule')

    def test_orders_accepted_before_schedules_were_saved_still_get_one(self):
        order_id = self.make_order('ready_for_receipt', dispatched_at=datetime.now() - timedelta(days=10))
        with patch.dict(os.environ, {'ENFORCE_SEGREGATION_OF_DUTIES': '0'}):
            self.confirm_for_member(order_id)
        with app.app_context():
            loan = get_db().execute('SELECT l.repayment_schedule_snapshot s FROM loans l JOIN asset_orders o '
                                    'ON o.linked_loan_id=l.id WHERE o.id=?', (order_id,)).fetchone()
        self.assertEqual(len(json.loads(loan['s'])['schedule']), 6)

    def test_accepting_a_quote_saves_the_agreed_schedule(self):
        order_id = self.make_order('awaiting_member_quote', stage='quote', accepted=False)
        self.login('member')
        self.client.post(f'/asset-schemes/orders/{order_id}/accept-quote', data={'accept_quote': '1'})
        order = self.order(order_id)
        self.assertEqual(order['status'], 'submitted')
        saved = json.loads(order['repayment_schedule_snapshot'])
        self.assertEqual((saved['tenure'], len(saved['schedule'])), (6, 6))
        self.assertAlmostEqual(sum(r['payment'] for r in saved['schedule']), order['total_repayment'], places=2)

    # -- cancelling --------------------------------------------------------------------------------
    def test_member_can_cancel_until_the_supplier_is_paid(self):
        for status, stage in (('sourcing_quote', 'quote'), ('awaiting_member_quote', 'quote'),
                              ('submitted', 'treasurer'), ('approved_for_procurement', 'approved')):
            order_id = self.make_order(status, stage=stage, accepted=status in ('submitted', 'approved_for_procurement'))
            self.login('member')
            self.client.post(f'/asset-schemes/orders/{order_id}/cancel', data={'reason': 'I no longer need it'})
            self.assertEqual(self.order(order_id)['status'], 'cancelled', status)
            with app.app_context():
                invoice = get_db().execute('SELECT status FROM asset_invoices WHERE order_id=?', (order_id,)).fetchone()
            if status in ('submitted', 'approved_for_procurement'):
                self.assertEqual(invoice['status'], 'cancelled')

    def test_member_cannot_cancel_once_the_supplier_is_paid(self):
        for status in ('procured', 'ready_for_receipt', 'received'):
            order_id = self.make_order(status)
            self.login('member')
            self.client.post(f'/asset-schemes/orders/{order_id}/cancel', data={'reason': 'Changed my mind late'})
            self.assertEqual(self.order(order_id)['status'], status)

    def test_cooperative_can_cancel_before_purchase_with_a_reason(self):
        order_id = self.make_order('sourcing_quote', stage='quote', accepted=False)
        self.login('treasurer')
        self.act(order_id, action='cancel', comment='')
        self.assertEqual(self.order(order_id)['status'], 'sourcing_quote')          # reason required
        self.act(order_id, action='cancel', comment='Supplier cannot supply this model')
        order = self.order(order_id)
        self.assertEqual((order['status'], order['quote_status']), ('cancelled', 'cancelled_by_cooperative'))
        with app.app_context():
            event = get_db().execute("SELECT comment FROM asset_order_events WHERE order_id=? AND action='cancelled_by_cooperative'",
                                     (order_id,)).fetchone()
        self.assertIn('cannot supply', event['comment'])

    def test_secretary_cannot_cancel_and_cancel_is_closed_after_purchase(self):
        order_id = self.make_order('submitted', stage='secretary')
        self.login('secretary')
        self.act(order_id, action='cancel', comment='Secretary trying to cancel')
        self.assertEqual(self.order(order_id)['status'], 'submitted')
        paid = self.make_order('procured')
        self.login('treasurer')
        self.act(paid, action='cancel', comment='Trying the simple cancel after buying')
        self.assertEqual(self.order(paid)['status'], 'procured')
        self.act(paid, action='cancel_after_purchase', comment='Treasurer cannot do this one', purchase_outcome='retained')
        self.assertEqual(self.order(paid)['status'], 'procured')

    def test_cancel_after_purchase_returned_posts_the_refund(self):
        order_id = self.make_order('procured')
        self.login('admin')
        data = dict(action='cancel_after_purchase', comment='Member left the cooperative before delivery',
                    purchase_outcome='returned', refund_amount='450000', refund_account=self.cash_account,
                    refund_reference='REFUND-9')
        self.act(order_id, **{**data, 'refund_amount': '999999'})                   # more than was paid
        self.assertEqual(self.order(order_id)['status'], 'procured')
        self.act(order_id, **data)
        self.assertEqual(self.order(order_id)['status'], 'cancelled')
        self.assertEqual(self.journals('asset_procurement_refund', order_id), 1)
        self.act(order_id, **data)                                                  # a second click
        self.assertEqual(self.journals('asset_procurement_refund', order_id), 1)

    def test_cancel_after_purchase_retained_posts_nothing_and_needs_a_clear_reason(self):
        order_id = self.make_order('ready_for_receipt')
        self.login('admin')
        self.act(order_id, action='cancel_after_purchase', comment='short', purchase_outcome='retained')
        self.assertEqual(self.order(order_id)['status'], 'ready_for_receipt')
        self.act(order_id, action='cancel_after_purchase', comment='Member withdrew; cooperative keeps the stock',
                 purchase_outcome='retained')
        self.assertEqual(self.order(order_id)['status'], 'cancelled')
        self.assertEqual(self.journals('asset_procurement_refund', order_id), 0)

    # -- the order page offers the right controls -------------------------------------------------
    def test_order_page_shows_the_right_cancel_and_confirm_controls(self):
        submitted = self.make_order('submitted', stage='treasurer')
        procured = self.make_order('procured')
        early = self.make_order('ready_for_receipt', dispatched_at=datetime.now() - timedelta(hours=2))
        self.login('treasurer')
        page = self.client.get(f'/asset-schemes/orders/{submitted}').text
        self.assertIn('Cancel this request', page)
        self.assertNotIn('Cancel after purchase', page)
        self.login('admin')
        page = self.client.get(f'/asset-schemes/orders/{procured}').text
        self.assertIn('Cancel after purchase', page)
        self.assertNotIn('Cancel this request', page)
        self.login('secretary')
        page = self.client.get(f'/asset-schemes/orders/{submitted}').text
        self.assertNotIn('Cancel this request', page)
        with patch.dict(os.environ, {'ENFORCE_SEGREGATION_OF_DUTIES': '0'}):
            self.login('treasurer')
            page = self.client.get(f'/asset-schemes/orders/{early}').text
        self.assertIn('has not had long enough to confirm', page)
        self.assertNotIn('Confirm receipt on member', page)
        self.login('member')
        self.assertIn('Changed your mind?', self.client.get(f'/asset-schemes/orders/{submitted}').text)
        self.assertNotIn('Changed your mind?', self.client.get(f'/asset-schemes/orders/{procured}').text)

    # -- requests are numbered safely and submitted once --------------------------------------------
    def test_requests_get_distinct_numbers_and_a_double_click_makes_one(self):
        self.login('member')
        form = {f'qty_{self.product_id}': '1', f'budget_{self.product_id}': '500000', 'repayment_method': 'salary_deduction',
                'terms_accepted': '1', 'signature_name': 'Asset Member'}
        url = f'/asset-schemes/apply/{self.campaign_id}'
        self.client.post(url, data={**form, 'submission_token': f'tok-{self.tag}-1'})
        self.client.post(url, data={**form, 'submission_token': f'tok-{self.tag}-1'})      # same token: a double click
        self.client.post(url, data={**form, 'submission_token': f'tok-{self.tag}-2'})
        with app.app_context():
            numbers = [r['order_number'] for r in get_db().execute(
                'SELECT order_number FROM asset_orders WHERE member_id=? ORDER BY id', (self.member_id,)).fetchall()]
        self.assertEqual(len(numbers), 2)
        self.assertEqual(len(set(numbers)), 2)
        for number in numbers:
            self.assertRegex(number, r'^AS/\d{8}/\d{5}$')


if __name__ == '__main__':
    unittest.main()
