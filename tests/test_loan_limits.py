import sqlite3
import unittest

from loan_limits import application_error, limits, validate_settings, member_limits, eligible_amount


class LoanLimitTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)')

    def tearDown(self):
        self.db.close()

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', (key, str(value)))

    def test_default_preserves_amount_but_enforces_general_tenure(self):
        self.assertIsNone(application_error(self.db, 'Regular', 9000000, 18))
        self.assertIn('18 months', application_error(self.db, 'Regular', 1, 19))

    def test_amount_boundary_and_type_independence(self):
        self.put('max_loan_amount', '250000.50')
        for name in limits(self.db)['tenures']:
            self.assertIsNone(application_error(self.db, name, 250000.50, 12))
            self.assertIn('regardless of savings', application_error(self.db, name, 250000.51, 12))

    def test_type_tenure_cannot_exceed_general(self):
        self.put('max_tenure_school_fees', 6)
        self.put('max_tenure_housing', 60)
        self.assertIsNone(application_error(self.db, 'School Fees', 100, 6))
        self.assertIn('6 months', application_error(self.db, 'School Fees', 100, 7))
        self.assertEqual(limits(self.db)['tenures']['Housing'], 18)
        self.assertEqual(limits(self.db)['tenures']['Regular'], 18)

    def test_type_amounts_and_member_card_match_application(self):
        self.put('max_loan_amount_regular', 500000)
        self.put('max_loan_amount_school_fees', 150000)
        self.put('max_tenure_school_fees', 6)
        card = member_limits(self.db, 1000000)
        self.assertEqual(card['Regular']['eligible_amount'], 500000)
        self.assertEqual(card['School Fees']['eligible_amount'], 150000)
        self.assertEqual(card['School Fees']['max_tenure_months'], 6)
        self.assertEqual(card['Housing']['eligible_amount'], 2000000)
        self.assertEqual(eligible_amount(self.db, 10000, 'Regular'), 20000)
        for name, row in card.items():
            self.assertIsNone(application_error(self.db, name, row['eligible_amount'], row['max_tenure_months']))
            if row['max_amount']:
                self.assertIsNotNone(application_error(self.db, name, row['max_amount'] + 1, 1))
        self.put('max_loan_amount', 100000)
        self.assertEqual(member_limits(self.db, 1000000)['Regular']['eligible_amount'], 100000)

    def test_type_amount_setting_validation(self):
        validate_settings({'max_loan_amount_regular': '250000.50'})
        for value in ('nan', '-1', 'oops', '0.001'):
            with self.assertRaises(ValueError):
                validate_settings({'max_loan_amount_regular': value})

    def test_invalid_settings_and_nonfinite_applications(self):
        for value in ('nan', 'inf', '-1', 'abc', '0.001'):
            with self.assertRaises(ValueError):
                validate_settings({'max_loan_amount': value})
        for value in ('1.5', '61', '-1', 'nan'):
            with self.assertRaises(ValueError):
                validate_settings({'max_tenure_regular': value})
        with self.assertRaises(ValueError):
            validate_settings({'max_tenure_months': '0'})
        self.assertIsNotNone(application_error(self.db, 'Regular', float('nan'), 1))
        self.assertIsNotNone(application_error(self.db, 'Regular', float('inf'), 1))


class LoanFeeSettingsTests(unittest.TestCase):
    """Fees withheld at disbursement must come from settings.

    Both were hard-coded at 1% of the loan, so a cooperative that set its
    application fee to zero still had 1% taken off every member's disbursement.
    """

    def _fees(self, amount, fee, rate):
        from blueprints.loans import _loan_fees
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)')
        db.executemany('INSERT INTO settings (key, value) VALUES (?, ?)',
                       [('loan_application_fee', str(fee)), ('insurance_rate', str(rate))])
        return _loan_fees(db, amount)

    def test_zero_application_fee_deducts_nothing(self):
        insurance, fee = self._fees(500000, 0, 1)
        self.assertEqual(fee, 0)
        self.assertEqual(insurance, 5000)

    def test_application_fee_is_flat_not_a_percentage(self):
        # A flat 1000 stays 1000 whatever the loan is worth.
        self.assertEqual(self._fees(500000, 1000, 1)[1], 1000)
        self.assertEqual(self._fees(50000, 1000, 1)[1], 1000)

    def test_insurance_follows_its_configured_rate(self):
        self.assertEqual(self._fees(200000, 0, 2.5)[0], 5000)
        self.assertEqual(self._fees(200000, 0, 0)[0], 0)

    def test_fees_can_never_exceed_the_loan(self):
        # Otherwise the member is "disbursed" a negative amount.
        insurance, fee = self._fees(50000, 99999, 1)
        self.assertGreaterEqual(50000 - insurance - fee, 0)
