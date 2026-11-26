import io


def test_hr_workbook_with_blank_rows_and_index_column():
    import openpyxl
    from repayment_adapter import read_payroll

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append([None, None, 'Employee Number', 'Full name', 'COOPERATIVE LOAN', 'COOPERATIVE SAVINGS'])
    sheet.insert_rows(1, 4)
    sheet.append([None, 1, '00123', 'Sample Member', 15000, 10000])
    data = io.BytesIO()
    workbook.save(data)
    assert read_payroll(data.getvalue(), 'hr.xlsx') == [{
        'row': 6, 'employee_id': '00123', 'name': 'Sample Member',
        'amount': '15000', 'savings_amount': '10000',
    }]
import unittest
from decimal import Decimal
import repayment_adapter as adapter


class AdapterTests(unittest.TestCase):
    def loan(self, **kw):
        return dict(id=1, member_id=1, loan_number='LN1', balance=550000,
                    first_payment_date='2026-10-28', tenure=6, total_repayment=550000,
                    repayment_schedule_snapshot=None, **kw)

    def test_expected_contract_and_future(self):
        loan = self.loan()
        self.assertEqual(adapter.expected_payment(loan, '2026-10')[0], Decimal('91666.67'))
        self.assertEqual(adapter.expected_payment(loan, '2026-09')[0], 0)
        self.assertEqual(adapter.expected_payment(loan, '2027-03')[0], Decimal('91666.65'))
        loan['first_payment_date'] = None
        self.assertIsNone(adapter.expected_payment(loan, '2026-10')[0])

    def test_migrated_not_recomputed(self):
        loan = self.loan()
        loan['loan_number'] = 'TALLY/X'
        self.assertIsNone(adapter.expected_payment(loan, '2026-10')[0])

    def test_migrated_history_consistent_irregular_and_cap(self):
        loan = self.loan()
        loan.update(loan_number='TALLY/X', first_payment_date=None)
        history = {'2026-08': Decimal('60000'), '2026-09': Decimal('60000')}
        expected, basis = adapter.expected_payment(loan, '2026-10', history)
        self.assertEqual(expected, Decimal('60000'))
        self.assertIn('Consistent', basis)
        self.assertIn('2026-09', basis)
        history['2026-09'] = Decimal('70000')
        expected, basis = adapter.expected_payment(loan, '2026-10', history)
        self.assertEqual(expected, Decimal('70000'))
        self.assertIn('Irregular', basis)
        loan['balance'] = 50000
        self.assertEqual(adapter.expected_payment(loan, '2026-10', history)[0], Decimal('50000'))
        self.assertIn('Only one month', adapter.expected_payment(loan, '2026-10', {'2026-09': Decimal('100')})[1])

    def test_csv_bom_semicolon_and_headers(self):
        rows = adapter.read_payroll('\ufeffStaff Number;Member Name;Amount\n001;Jane Doe;1000\n'.encode(), 'pay.csv')
        self.assertEqual(rows[0]['employee_id'], '001')
        self.assertEqual(rows[0]['amount'], '1000')
        with self.assertRaises(ValueError):
            adapter.read_payroll(b'amount,amount,name\n1,2,Joe', 'pay.csv')

    def test_xlsx(self):
        import openpyxl
        w = openpyxl.Workbook()
        w.active.append(['member_number', 'amount'])
        w.active.append([123, 1000])
        f = io.BytesIO()
        w.save(f)
        self.assertEqual(adapter.read_payroll(f.getvalue(), 'pay.xlsx')[0]['member_number'], '123')

    def test_matching_and_unknown_id_no_name_fallback(self):
        members = [dict(id=1, member_number='001', employee_id='S1', member_name='Jane Doe')]
        loan = {**self.loan(), 'expected': '100.00'}
        rows = [dict(row=2, member_number='001', amount='100'),
                dict(row=4, member_number='wrong', name='Jane Doe', amount='100')]
        result = adapter.match_payroll(rows, members, [loan])
        self.assertEqual(result[0]['loans'][0]['allocation'], '100.00')
        self.assertTrue(result[1]['error'])
        self.assertFalse(adapter.match_payroll([dict(row=2, name='Doe Jane', amount='100')], members, [loan])[0]['error'])

    def test_multiple_only_suggest_exact_sum(self):
        members = [dict(id=1, member_number='001', member_name='Jane Doe')]
        loans = [{**self.loan(), 'expected': '100.00'}, {**self.loan(), 'id': 2, 'expected': '50.00'}]
        item = adapter.match_payroll([dict(row=2, member_number='001', amount='150')], members, loans)[0]
        self.assertEqual([l['allocation'] for l in item['loans']], ['100.00', '50.00'])
        self.assertEqual(len(adapter.validate_allocations(item, {'1': '100', '2': '50'}, {l['id']:l for l in loans})), 2)
        with self.assertRaises(ValueError):
            adapter.validate_allocations(item, {'1': '150', '2': '50'}, {l['id']:l for l in loans})
        with self.assertRaises(ValueError):
            adapter.validate_allocations(item, {'1': '100', '2': '50'}, {})
        loans[0]['expected'] = ''
        item = adapter.match_payroll([dict(row=2, member_number='001', amount='150')], members, loans)[0]
        self.assertEqual([l['allocation'] for l in item['loans']], ['', ''])

    def test_nonfinite_rejected(self):
        for value in ['NaN', 'Infinity', '-1', 'bad']:
            with self.assertRaises(ValueError):
                adapter.money(value)

    def test_combined_hr_headers_and_verified_totals(self):
        raw = ('Employee Number,Full name,COOPERATIVE LOAN,COOPERATIVE SAVINGS,COOPERATIVE SUBSCRIPTION\n'
               '001,Jane Doe,150,1000,20\n002,John Doe,0,500,\n'
               ',,150,1500,20\n,,TOTAL,1670,\n').encode()
        rows = adapter.read_payroll(raw, 'hr.csv')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['employee_id'], '001')
        self.assertEqual(rows[0]['savings_amount'], '1000')
        members = [dict(id=1, member_number='001', employee_id='', member_name='Jane Doe'),
                   dict(id=2, member_number='002', employee_id='', member_name='John Doe')]
        result = adapter.match_payroll(rows, members, [])
        self.assertEqual(result[0]['member_id'], 1)
        self.assertTrue(result[0]['loan_error'])
        self.assertFalse(result[1]['error'])
        self.assertFalse(result[1]['loan_error'])
        self.assertEqual(result[1]['savings_amount'], '500.00')
        with self.assertRaises(ValueError):
            adapter.read_payroll(raw.replace(b',,150,1500,20', b',,150,1600,20'), 'hr.csv')

    def test_manual_match_conflicts_and_duplicate_members(self):
        members = [dict(id=1, member_number='001', employee_id='', member_name='Jane Doe')]
        rows = [dict(row=2, employee_id='wrong', name='Different name', amount='0', savings_amount='100')]
        self.assertTrue(adapter.match_payroll(rows, members, [])[0]['error'])
        rows[0]['selected_member_id'] = '1'
        result = adapter.match_payroll(rows, members, [])
        self.assertFalse(result[0]['error'])
        self.assertIn('Manually selected', result[0]['match'])
        merged = adapter.match_payroll(rows + [{**rows[0], 'row': 3}], members, [])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['savings_amount'], '200.00')
        self.assertEqual(merged[0]['merged_rows'], [3])

    def test_duplicate_member_lines_are_added_together(self):
        members = [dict(id=1, member_number='001', employee_id='', member_name='Jane Doe')]
        loans = [{**self.loan(), 'expected': '5000.00'}]
        rows = [dict(row=2, member_number='001', amount='3000'), dict(row=9, member_number='001', amount='2000')]
        result = adapter.match_payroll(rows, members, loans)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['amount'], '5000.00')
        self.assertEqual(result[0]['loans'][0]['allocation'], '5000.00')
        self.assertEqual(result[0]['loan_flag'], 'match')
        self.assertIn('Rows 2, 9', result[0]['merge_note'])

    def test_shortfall_and_excess_are_flagged(self):
        members = [dict(id=1, member_number='001', employee_id='', member_name='Jane Doe')]
        loans = [{**self.loan(), 'expected': '5000.00'}]
        short = adapter.match_payroll([dict(row=2, member_number='001', amount='3000')], members, loans)[0]
        self.assertEqual((short['loan_flag'], short['loan_variance']), ('short', '-2000.00'))
        self.assertIn('arrears', short['allocation_note'])
        over = adapter.match_payroll([dict(row=2, member_number='001', amount='6000')], members, loans)[0]
        self.assertEqual((over['loan_flag'], over['loan_variance']), ('over', '1000.00'))

    def test_name_suggestions_for_unmatched_rows(self):
        members = [dict(id=1, member_number='001', employee_id='', member_name='Adeolu Keshinro'),
                   dict(id=2, member_number='002', employee_id='', member_name='Someone Else')]
        row = dict(row=2, name='ADEOLU K. KESHINRO', amount='100')
        item = adapter.match_payroll([row], members, [])[0]
        self.assertTrue(item['error'])
        self.assertEqual([s['id'] for s in item['suggestions']], [1])

    def test_excel_cp1252_csv_is_readable(self):
        raw = ('Staff Number,Full name,Loan Repayment\n1001,Adé B,5000\n').encode('cp1252')
        self.assertEqual(adapter.read_payroll(raw, 'hr.csv')[0]['amount'], '5000')

    def test_total_row_labelled_in_name_column(self):
        raw = ('S/N,Employee Number,Full name,COOPERATIVE LOAN,COOPERATIVE SAVINGS\n'
               '1,1001,ADE ONE,100.10,10\n2,1002,ADE TWO,0,20\n,,TOTAL,100.1,30\n').encode()
        rows = adapter.read_payroll(raw, 'hr.csv')
        self.assertEqual([r['employee_id'] for r in rows], ['1001', '1002'])
        self.assertTrue(rows.control_verified)
        with self.assertRaises(ValueError):
            adapter.read_payroll(raw.replace(b',,TOTAL,100.1,30', b',,TOTAL,100.1,31'), 'hr.csv')

    def test_unknown_headings_need_mapping_then_work(self):
        nl = chr(10)
        raw = nl.join(['Emp No.,Staff Name,Loan Ded,Sav Ded', '1001,Jane Doe,500,100', '']).encode()
        with self.assertRaises(adapter.NeedsMapping) as caught:
            adapter.read_payroll(raw, 'hr.csv')
        guesses = caught.exception.guesses
        self.assertEqual((guesses['employee_id'], guesses['name'], guesses['amount'], guesses['savings_amount']), (0, 1, 2, 3))
        rows = adapter.read_payroll(raw, 'hr.csv', {k: v for k, v in guesses.items()})
        self.assertEqual((rows[0]['employee_id'], rows[0]['amount'], rows[0]['savings_amount']), ('1001', '500', '100'))
        self.assertEqual(rows.columns['amount'], 'Loan Ded')
        with self.assertRaises(ValueError):
            adapter.read_payroll(raw, 'hr.csv', {'amount': 2, 'savings_amount': 2, 'employee_id': 0})
        with self.assertRaises(ValueError):
            adapter.read_payroll(raw, 'hr.csv', {'amount': 9, 'employee_id': 0})

    def test_odd_staff_number_is_flagged(self):
        members = [dict(id=i, member_number=f'M{i}', employee_id=f'100{i:04d}', member_name=f'Person {i}') for i in range(1, 7)]
        members.append(dict(id=7, member_number='M7', employee_id='7', member_name='Short Id'))
        rows = [dict(row=i + 1, employee_id=m['employee_id'], name=m['member_name'], amount='100') for i, m in enumerate(members)]
        items = adapter.match_payroll(rows, members, [])
        self.assertIn('digits', items[6]['id_warning'])
        self.assertEqual(items[0]['id_warning'], '')

    def test_remembered_match_resolves_a_known_mismatch(self):
        members = [dict(id=1, member_number='001', employee_id='5001', member_name='Jane Doe')]
        row = dict(row=2, employee_id='9999', name='J. DOE', amount='100')
        self.assertTrue(adapter.match_payroll([row], members, [])[0]['error'])
        item = adapter.match_payroll([row], members, [], {adapter.hr_key(row): 1})[0]
        self.assertEqual(item['error'], '')
        self.assertTrue(item['remembered'])
        self.assertIn('Remembered', item['match'])


if __name__ == '__main__':
    unittest.main()
