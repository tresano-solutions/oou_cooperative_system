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
            db.execute("""INSERT INTO members (member_number,first_name,last_name,email,status,annual_salary,total_savings)
                VALUES (?,?,?,?,'active',3600000,500000)""", (f'AC-{self.tag}', 'Asset', 'Member', self.member_email))
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

    # -- the money steps post the right journals ---------------------------------------------------
    def lines(self, module, order_id):
        with app.app_context():
            rows = get_db().execute("""SELECT l.account_code, l.debit, l.credit FROM journal_lines l
                JOIN journal_entries e ON e.id=l.entry_id WHERE e.source_module=? AND e.source_id=?""",
                                    (module, order_id)).fetchall()
        return [(r['account_code'], round(r['debit'] or 0, 2), round(r['credit'] or 0, 2)) for r in rows]

    def stock_order(self, status='procured', **columns):
        """An order bought under inventory accounting."""
        return self.make_order(status, procurement_treatment='inventory', inventory_status='bought',
                               procurement_journal_id=999001, **columns)

    def legacy_order(self, status='procured', **columns):
        """An order bought (and expensed) before inventory accounting began."""
        return self.make_order(status, procurement_journal_id=999002, **columns)

    def stock_status(self, order_id):
        return self.order(order_id)['inventory_status']

    def test_purchase_goes_to_inventory_and_the_fee_to_income(self):
        procured = self.make_order('approved_for_procurement')
        self.login('treasurer')
        self.act(procured, action='procured', procurement_account=self.cash_account, procurement_reference='SUP-J1')
        self.assertEqual(sorted(self.lines('asset_procurement', procured)),
                         sorted([('1300', 450000.0, 0.0), (self.cash_account, 0.0, 450000.0)]))
        order = self.order(procured)
        self.assertEqual((order['procurement_treatment'], order['inventory_status']), ('inventory', 'bought'))
        fee = self.make_order('procured', delivery_fee=15000)
        self.act(fee, action='delivery_fee', delivery_fee_status='paid', delivery_fee_account=self.cash_account,
                 delivery_fee_reference='FEE-J1')
        self.assertEqual(sorted(self.lines('asset_delivery_fee', fee)),
                         sorted([(self.cash_account, 15000.0, 0.0), ('4100', 0.0, 15000.0)]))

    def test_delivery_books_the_sale_and_moves_the_cost_from_inventory_to_cost_of_sales(self):
        order_id = self.stock_order('ready_for_receipt', dispatched_at=datetime.now() - timedelta(days=9))
        with patch.dict(os.environ, {'ENFORCE_SEGREGATION_OF_DUTIES': '0'}):
            self.confirm_for_member(order_id)
        self.assertEqual(sorted(self.lines('asset_scheme', order_id)),
                         sorted([('1100', 500000.0, 0.0), ('4050', 0.0, 500000.0),
                                 ('5200', 450000.0, 0.0), ('1300', 0.0, 450000.0)]))
        self.assertEqual(self.stock_status(order_id), 'sold')

    def test_an_order_bought_before_inventory_accounting_adds_no_second_cost_at_delivery(self):
        order_id = self.legacy_order('ready_for_receipt', dispatched_at=datetime.now() - timedelta(days=9))
        with patch.dict(os.environ, {'ENFORCE_SEGREGATION_OF_DUTIES': '0'}):
            self.confirm_for_member(order_id)
        self.assertEqual(sorted(self.lines('asset_scheme', order_id)),
                         sorted([('1100', 500000.0, 0.0), ('4050', 0.0, 500000.0)]))

    def test_returned_stock_leaves_inventory_and_unrecovered_cost_is_written_off(self):
        full = self.stock_order()
        partial = self.stock_order()
        legacy = self.legacy_order()
        self.login('admin')
        base = dict(action='cancel_after_purchase', comment='Member left before delivery', purchase_outcome='returned',
                    refund_account=self.cash_account)
        self.act(full, refund_amount='450000', refund_reference='R-FULL', **base)
        self.assertEqual(sorted(self.lines('asset_procurement_refund', full)),
                         sorted([(self.cash_account, 450000.0, 0.0), ('1300', 0.0, 450000.0)]))
        self.assertEqual(self.stock_status(full), 'returned')
        self.act(partial, refund_amount='400000', refund_reference='R-PART', **base)
        self.assertEqual(sorted(self.lines('asset_procurement_refund', partial)),
                         sorted([(self.cash_account, 400000.0, 0.0), ('5000', 50000.0, 0.0), ('1300', 0.0, 450000.0)]))
        self.act(legacy, refund_amount='450000', refund_reference='R-OLD', **base)    # was expensed: credit expenses
        self.assertEqual(sorted(self.lines('asset_procurement_refund', legacy)),
                         sorted([(self.cash_account, 450000.0, 0.0), ('5000', 0.0, 450000.0)]))

    def test_stock_the_cooperative_keeps_stays_in_inventory(self):
        kept = self.stock_order('ready_for_receipt')
        old = self.legacy_order('ready_for_receipt')
        self.login('admin')
        for order_id in (kept, old):
            self.act(order_id, action='cancel_after_purchase', comment='Member withdrew; cooperative keeps it',
                     purchase_outcome='retained')
            self.assertEqual(self.order(order_id)['status'], 'cancelled')
            self.assertEqual(self.journals('asset_procurement_refund', order_id), 0)
        self.assertEqual(self.stock_status(kept), 'held')
        self.assertIsNone(self.stock_status(old))

    # -- the one-off move of earlier purchases into inventory --------------------------------------
    def test_earlier_purchases_can_be_reclassified_once(self):
        waiting = self.legacy_order('procured')
        delivered = self.legacy_order('received')
        self.login('treasurer')
        page = self.client.get('/asset-schemes/inventory').text
        self.assertIn(self.order(waiting)['order_number'], page)
        self.assertNotIn(self.order(delivered)['order_number'], page)          # already sold: left as it was
        self.client.post('/asset-schemes/inventory/reclassify',
                         data={'order_ids': [str(waiting), str(delivered)], 'posting_date': datetime.now().strftime('%Y-%m-%d')})
        self.assertEqual(sorted(self.lines('asset_inventory_reclass', waiting)),
                         sorted([('1300', 450000.0, 0.0), ('5000', 0.0, 450000.0)]))
        self.assertEqual(self.journals('asset_inventory_reclass', delivered), 0)
        self.assertEqual((self.order(waiting)['procurement_treatment'], self.stock_status(waiting)), ('inventory', 'bought'))
        self.client.post('/asset-schemes/inventory/reclassify',
                         data={'order_ids': [str(waiting)], 'posting_date': datetime.now().strftime('%Y-%m-%d')})
        self.assertEqual(self.journals('asset_inventory_reclass', waiting), 1)   # a second post does nothing
        # after the move, delivery carries the cost to cost of sales
        with app.app_context():
            get_db().execute('UPDATE asset_orders SET status=?,dispatched_at=? WHERE id=?',
                             ('ready_for_receipt', datetime.now() - timedelta(days=9), waiting))
            get_db().commit()
        with patch.dict(os.environ, {'ENFORCE_SEGREGATION_OF_DUTIES': '0'}):
            self.confirm_for_member(waiting)
        self.assertIn(('5200', 450000.0, 0.0), self.lines('asset_scheme', waiting))

    def test_inventory_page_reconciles_stock_to_the_ledger_and_is_for_treasury_only(self):
        import asset_accounting
        with app.app_context():
            db = get_db()
            before = asset_accounting.ledger_balance(db, '1300') - asset_accounting.stock_on_hand(db)[1]
        order_id = self.make_order('approved_for_procurement')
        self.login('treasurer')
        self.act(order_id, action='procured', procurement_account=self.cash_account, procurement_reference='SUP-INV')
        with app.app_context():
            db = get_db()
            after = asset_accounting.ledger_balance(db, '1300') - asset_accounting.stock_on_hand(db)[1]
        self.assertAlmostEqual(before, after, places=2)           # a purchase adds the same to stock list and ledger
        page = self.client.get('/asset-schemes/inventory')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Ledger balance', page.text)
        self.login('secretary')
        self.assertNotEqual(self.client.get('/asset-schemes/inventory').status_code, 200)

    def test_a_wrong_account_mapping_stops_the_purchase_with_a_clear_reason(self):
        order_id = self.make_order('approved_for_procurement')
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO settings (key,value) VALUES ('asset_inventory_account','9999') "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            db.commit()
        try:
            self.login('treasurer')
            self.act(order_id, action='procured', procurement_account=self.cash_account, procurement_reference='SUP-BAD')
            self.assertEqual(self.order(order_id)['status'], 'approved_for_procurement')
            self.assertEqual(self.journals('asset_procurement', order_id), 0)
        finally:
            with app.app_context():
                get_db().execute("UPDATE settings SET value='1300' WHERE key='asset_inventory_account'")
                get_db().commit()

    # -- affordability is looked at, and again on the real price -----------------------------------
    def test_eligibility_review_lists_every_point_to_check(self):
        import asset_eligibility
        with app.app_context():
            db = get_db()
            member = db.execute('SELECT * FROM members WHERE id=?', (self.member_id,)).fetchone()
            status, reason = asset_eligibility.review(db, member, 20000, 120000)
            self.assertEqual(status, 'eligible')                    # 500,000 savings x 2 covers 120,000; 20,000 is well within salary
            db.execute("""INSERT INTO loans (loan_number,member_id,amount,total_repayment,balance,tenure,status,
                purpose,first_payment_date,disbursement_date) VALUES (?,?,900000,900000,900000,6,'active','Regular',?,?)""",
                       (f'EL-{self.tag}', self.member_id, datetime.now() - timedelta(days=100),
                        datetime.now() - timedelta(days=130)))
            db.commit()
            member = db.execute('SELECT * FROM members WHERE id=?', (self.member_id,)).fetchone()
            status, reason = asset_eligibility.review(db, member, 20000, 120000)
        self.assertEqual(status, 'review')
        self.assertIn('of monthly salary', reason)                  # 150,000 + 20,000 on 300,000 a month
        self.assertIn('in arrears', reason)
        self.assertIn('savings support', reason)

    def test_a_clean_member_is_eligible_with_an_hr_reminder_for_salary_deduction(self):
        import asset_eligibility
        with app.app_context():
            db = get_db()
            member = db.execute('SELECT * FROM members WHERE id=?', (self.member_id,)).fetchone()
            status, reason = asset_eligibility.review(db, member, 20000, 120000, 'salary_deduction')
        self.assertEqual(status, 'eligible')
        self.assertIn('confirm the deduction with HR', reason)

    def test_the_quote_is_assessed_again_on_the_real_price(self):
        order_id = self.make_order('sourcing_quote', stage='quote', accepted=False, eligibility_status='eligible',
                                   eligibility_reason='Looked fine on the budget')
        with app.app_context():
            item = get_db().execute('SELECT id FROM asset_order_items WHERE order_id=?', (order_id,)).fetchone()['id']
        self.login('treasurer')
        self.act(order_id, action='quote', quoted_supplier_cost='900000', quoted_delivery_fee='0',
                 **{f'item_unit_price_{item}': '3000000', f'agreed_product_name_{item}': 'Big TV',
                    f'agreed_brand_{item}': 'LG', f'agreed_specification_{item}': '85 inch'})
        order = self.order(order_id)
        self.assertEqual(order['status'], 'awaiting_member_quote')
        self.assertEqual(order['eligibility_status'], 'review')
        self.assertIn('of monthly salary', order['eligibility_reason'])
        self.assertIsNotNone(order['quote_valid_until'])

    # -- quotes expire ------------------------------------------------------------------------------
    def test_an_expired_quote_cannot_be_accepted_but_a_revision_can_be_asked_for(self):
        order_id = self.make_order('awaiting_member_quote', stage='quote', accepted=False,
                                   quote_valid_until=datetime.now() - timedelta(days=1))
        self.login('member')
        page = self.client.get(f'/asset-schemes/orders/{order_id}').text
        self.assertIn('This quote has expired', page)
        self.assertNotIn('Accept and submit for approval', page)
        self.client.post(f'/asset-schemes/orders/{order_id}/accept-quote', data={'accept_quote': '1'})
        self.assertEqual(self.order(order_id)['status'], 'awaiting_member_quote')
        self.client.post(f'/asset-schemes/orders/{order_id}/request-revision', data={'reason': 'Please refresh the price'})
        self.assertEqual(self.order(order_id)['status'], 'sourcing_quote')

    def test_a_current_quote_shows_its_expiry_and_the_yearly_rate(self):
        order_id = self.make_order('awaiting_member_quote', stage='quote', accepted=False,
                                   quote_valid_until=datetime.now() + timedelta(days=5))
        self.login('member')
        page = self.client.get(f'/asset-schemes/orders/{order_id}').text
        self.assertIn('Quote valid until', page)
        self.assertIn('Equivalent yearly rate', page)
        self.assertIn('39.2%', page)
        self.assertIn('Accept and submit for approval', page)

    def test_effective_yearly_rate_of_a_flat_charge(self):
        from asset_schemes import effective_annual_rate
        self.assertEqual(effective_annual_rate(500000, 20, 6), 39.2)
        self.assertEqual(effective_annual_rate(500000, 20, 12), 41.3)

    def test_invoice_pdf_carries_the_schedule(self):
        order_id = self.make_order('submitted', stage='secretary')
        self.login('member')
        result = self.client.get(f'/asset-schemes/orders/{order_id}/invoice.pdf')
        self.assertEqual((result.status_code, result.mimetype), (200, 'application/pdf'))
        self.assertGreater(len(result.data), 2000)

    # -- web and mobile apply by the same rules -------------------------------------------------------
    def test_apply_needs_a_signature_and_a_sensible_budget_on_the_web_too(self):
        self.login('member')
        form = {f'qty_{self.product_id}': '1', 'repayment_method': 'salary_deduction', 'terms_accepted': '1'}
        url = f'/asset-schemes/apply/{self.campaign_id}'
        def count():
            with app.app_context():
                return get_db().execute('SELECT COUNT(*) FROM asset_orders WHERE member_id=?', (self.member_id,)).fetchone()[0]
        before = count()
        self.client.post(url, data={**form, f'budget_{self.product_id}': '500000', 'signature_name': ''})
        self.client.post(url, data={**form, f'budget_{self.product_id}': '500', 'signature_name': 'Asset Member'})
        self.assertEqual(count(), before)
        self.client.post(url, data={**form, f'budget_{self.product_id}': '500000', 'signature_name': 'Asset Member'})
        self.assertEqual(count(), before + 1)

    # -- housekeeping ---------------------------------------------------------------------------------
    def test_the_newsletter_task_ignores_a_token_in_the_address(self):
        with patch.dict(os.environ, {'TASK_RUNNER_TOKEN': 'secret-task-token'}):
            self.client.get('/logout')
            self.assertEqual(self.client.post('/tasks/asset-schemes/newsletters?token=secret-task-token').status_code, 403)
            self.assertEqual(self.client.post('/tasks/asset-schemes/newsletters',
                                              headers={'X-Task-Token': 'secret-task-token'}).status_code, 200)

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
