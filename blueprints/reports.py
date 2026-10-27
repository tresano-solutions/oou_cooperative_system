import csv
from csv_safe import safe_writer
import io
from datetime import datetime

from flask import Blueprint, flash, make_response, redirect, render_template, request, url_for
from flask_login import login_required

from database import get_db
from delinquency import loan_delinquency
from ledger import account_ledger, get_default_cash_account, trial_balance
from reports_engine import balance_sheet, cash_flow, income_statement, surplus_appropriation
from utils import role_required

reports = Blueprint('reports', __name__)


def _today():
    return datetime.now().strftime('%Y-%m-%d')


def _year_start():
    return datetime.now().replace(month=1, day=1).strftime('%Y-%m-%d')


def _get_val(db, query, params=()):
    row = db.execute(query, params).fetchone()
    return row[0] if row and row[0] is not None else 0


def _csv_response(filename, columns, rows):
    out = io.StringIO()
    writer = safe_writer(out)
    writer.writerow([c['label'] for c in columns])
    for row in rows:
        writer.writerow([row.get(c['key'], '') for c in columns])
    response = make_response(out.getvalue())
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    response.headers['Content-Disposition'] = f'attachment; filename={filename}'
    return response


def _money(value):
    return float(value or 0)


def _report_center_groups(year_start, today):
    return [
        {
            'name': 'Core Statements',
            'reports': [
                {
                    'title': 'Financial Statements',
                    'summary': 'Income statement, balance sheet, cash flow, and surplus appropriation.',
                    'icon': 'fas fa-chart-pie',
                    'view_url': url_for('reports.financial_report', from_date=year_start, to_date=today),
                    'export_url': url_for('reports.financial_report', from_date=year_start, to_date=today, format='xlsx'),
                    'status': 'Ready',
                    'roles': ['admin', 'treasurer', 'secretary', 'exco'],
                },
                {
                    'title': 'Trial Balance',
                    'summary': 'Debit and credit balances by chart-of-account line.',
                    'icon': 'fas fa-balance-scale',
                    'view_url': url_for('accounting.trial_balance_view', as_of=today),
                    'export_url': url_for('accounting.trial_balance_view', as_of=today, format='xlsx'),
                    'status': 'Ready',
                    'roles': ['admin', 'treasurer'],
                },
                {
                    'title': 'General Ledger Register',
                    'summary': 'Full journal-line export for external analysis and audit sampling.',
                    'icon': 'fas fa-book',
                    'view_url': url_for('accounting.journal_register'),
                    'export_url': url_for('accounting.journal_register_export'),
                    'status': 'Ready',
                    'roles': ['admin', 'treasurer'],
                },
            ],
        },
        {
            'name': 'Control Reports',
            'reports': [
                {
                    'title': 'Cashbook',
                    'summary': 'Cash/bank movements with running balance from the GL cash account.',
                    'icon': 'fas fa-wallet',
                    'view_url': url_for('reports.cashbook_report', from_date=year_start, to_date=today),
                    'export_url': url_for('reports.cashbook_report', from_date=year_start, to_date=today, format='csv'),
                    'status': 'Ready',
                    'roles': ['admin', 'treasurer'],
                },
                {
                    'title': 'Bank Accounts Position',
                    'summary': 'Separate bank/cash account balances, period movements, and reconciliation variance.',
                    'icon': 'fas fa-building-columns',
                    'view_url': url_for('accounting.bank_accounts', from_date=year_start, to_date=today),
                    'export_url': url_for('accounting.bank_accounts', from_date=year_start, to_date=today, format='csv'),
                    'status': 'Ready',
                    'roles': ['admin', 'treasurer'],
                },
                {
                    'title': 'Member Savings Control',
                    'summary': 'Member-level savings balances for reconciliation to member deposits.',
                    'icon': 'fas fa-piggy-bank',
                    'view_url': url_for('reports.member_savings_control', as_of=today),
                    'export_url': url_for('reports.member_savings_control', as_of=today, format='csv'),
                    'status': 'Ready',
                    'roles': ['admin', 'treasurer', 'secretary', 'exco'],
                },
                {
                    'title': 'Loan Portfolio and Aging',
                    'summary': 'Outstanding loan book, repayment totals, due dates, and aging buckets.',
                    'icon': 'fas fa-hand-holding-usd',
                    'view_url': url_for('reports.loan_portfolio_report', as_of=today),
                    'export_url': url_for('reports.loan_portfolio_report', as_of=today, format='csv'),
                    'status': 'Ready',
                    'roles': ['admin', 'treasurer', 'secretary', 'exco'],
                },
            ],
        },
        {
            'name': 'Audit Drill-Down',
            'reports': [
                {
                    'title': 'Chart of Accounts',
                    'summary': 'Open an account and drill into its full GL register.',
                    'icon': 'fas fa-list',
                    'view_url': url_for('accounting.chart_of_accounts'),
                    'export_url': '',
                    'status': 'Ready',
                    'roles': ['admin', 'treasurer'],
                },
                {
                    'title': 'Ledger Reconciliation',
                    'summary': 'Checks operational records against posted journal entries.',
                    'icon': 'fas fa-check-double',
                    'view_url': url_for('accounting.reconciliation'),
                    'export_url': '',
                    'status': 'Ready',
                    'roles': ['admin', 'treasurer'],
                },
                {
                    'title': 'Period Close',
                    'summary': 'Lock posted periods so historical reports remain stable.',
                    'icon': 'fas fa-lock',
                    'view_url': url_for('accounting.period_close'),
                    'export_url': '',
                    'status': 'Ready',
                    'roles': ['admin'],
                },
            ],
        },
    ]


@reports.route('/reports')
@login_required
@role_required('admin', 'treasurer', 'secretary', 'exco')
def reports_list():
    db = get_db()
    today = _today()
    year_start = _year_start()
    try:
        inc = income_statement(db, year_start, today)
        bs = balance_sheet(db, as_of=today)
        cash_account = get_default_cash_account(db)
        stats = {
            'total_members': _get_val(db, 'SELECT COUNT(*) FROM members'),
            'active_members': _get_val(db, "SELECT COUNT(*) FROM members WHERE status = 'active'"),
            'active_loans': _get_val(db, "SELECT COUNT(*) FROM loans WHERE status = 'active'"),
            'member_deposits': _money(_get_val(db, 'SELECT COALESCE(SUM(amount), 0) FROM savings')),
            'loan_book': _money(_get_val(db, "SELECT COALESCE(SUM(COALESCE(balance, amount)), 0) FROM loans WHERE status = 'active'")),
            'cash_balance': _money(_get_val(db, '''
                SELECT COALESCE(SUM(debit), 0) - COALESCE(SUM(credit), 0)
                FROM journal_lines
                WHERE account_code = ?
            ''', (cash_account,))),
            'ytd_income': inc['total_income'],
            'ytd_expenses': inc['total_expenses'],
            'ytd_surplus': inc['net_surplus'],
            'balance_sheet_ok': bs['balances'],
        }
    except Exception as exc:
        stats = {}
        flash(f'Unable to load report center summary: {exc}', 'warning')

    return render_template(
        'admin/reports.html',
        today=today,
        year_start=year_start,
        stats=stats,
        report_groups=_report_center_groups(year_start, today),
    )


@reports.route('/reports/financial')
@login_required
@role_required('admin', 'treasurer', 'secretary', 'exco')
def financial_report():
    db = get_db()
    from_date = (
        request.args.get('from_date')
        or request.args.get('start_date')
        or _year_start()
    )
    to_date = (
        request.args.get('to_date')
        or request.args.get('end_date')
        or _today()
    )

    try:
        inc = income_statement(db, from_date, to_date)
        bs = balance_sheet(db, as_of=to_date)
        appr = surplus_appropriation(inc['net_surplus'])
        cf = cash_flow(db, from_date, to_date)

        fmt = request.args.get('format')
        if fmt:
            from report_export import report_response

            def _r(label, val, bold=False):
                return {'cells': [label, val], 'bold': bold}

            inc_rows = [_r(l['name'], l['amount']) for l in inc['income_lines']]
            inc_rows.append(_r('Total income', inc['total_income'], True))
            inc_rows += [_r(l['name'], -l['amount']) for l in inc['expense_lines']]
            inc_rows.append(_r('Total expenses', -inc['total_expenses'], True))
            inc_rows.append(_r('Net surplus / (deficit)', inc['net_surplus'], True))

            bs_rows = [_r(l['name'], l['amount']) for l in bs['asset_lines']]
            bs_rows.append(_r('Total assets', bs['total_assets'], True))
            bs_rows += [_r(l['name'], l['amount']) for l in bs['liability_lines']]
            bs_rows += [_r(l['name'], l['amount']) for l in bs['equity_lines']]
            bs_rows.append(_r('Total liabilities & equity', bs['total_liabilities'] + bs['total_equity'], True))

            cf_rows = [_r('Opening cash', cf['opening'])]
            for cat in ('operating', 'investing', 'financing'):
                for it in cf['groups'][cat]:
                    cf_rows.append(_r(f"{cat.title()}: {it['name']}", it['amount']))
            cf_rows += [_r('Net change in cash', cf['net_change'], True),
                        _r('Closing cash', cf['closing'], True)]

            report = {
                'title': 'Financial Statements',
                'subtitle': f'{from_date} to {to_date}',
                'sections': [
                    {'heading': 'Income Statement', 'columns': ['', 'Amount'], 'rows': inc_rows},
                    {'heading': f'Balance Sheet (as at {to_date})', 'columns': ['', 'Amount'], 'rows': bs_rows},
                    {'heading': 'Cash Flow Statement', 'columns': ['', 'Amount'], 'rows': cf_rows},
                ],
            }
            return report_response(report, fmt, redirect_url=url_for(
                'reports.financial_report', from_date=from_date, to_date=to_date
            ))

        return render_template('admin/financial-report.html',
                               from_date=from_date, to_date=to_date,
                               inc=inc, bs=bs, appr=appr, cf=cf)
    except Exception as e:
        flash(f'Error generating financial report: {str(e)}', 'danger')
        return redirect(url_for('reports.reports_list'))


@reports.route('/reports/cashbook')
@login_required
@role_required('admin', 'treasurer')
def cashbook_report():
    db = get_db()
    from_date = request.args.get('from_date') or _year_start()
    to_date = request.args.get('to_date') or _today()
    cash_account = get_default_cash_account(db)
    data = account_ledger(db, cash_account, from_date, to_date)
    if not data:
        flash('Cash account is not available in the chart of accounts.', 'danger')
        return redirect(url_for('reports.reports_list'))

    columns = [
        {'key': 'date', 'label': 'Date'},
        {'key': 'entry_number', 'label': 'Entry #'},
        {'key': 'description', 'label': 'Description'},
        {'key': 'reference', 'label': 'Reference'},
        {'key': 'source_module', 'label': 'Source'},
        {'key': 'debit', 'label': 'Cash In'},
        {'key': 'credit', 'label': 'Cash Out'},
        {'key': 'balance', 'label': 'Running Balance'},
    ]
    rows = [{
        'date': str(e.get('date') or '')[:10],
        'entry_number': e.get('entry_number') or '',
        'description': e.get('description') or '',
        'reference': e.get('reference') or '',
        'source_module': e.get('source_module') or 'manual',
        'debit': f"{float(e.get('debit') or 0):.2f}",
        'credit': f"{float(e.get('credit') or 0):.2f}",
        'balance': f"{float(e.get('balance') or 0):.2f}",
    } for e in data['entries']]

    if request.args.get('format') == 'csv':
        return _csv_response('cashbook_report.csv', columns, rows)

    return render_template('admin/report-table.html',
                           title='Cashbook',
                           subtitle=f'{from_date} to {to_date}',
                           columns=columns, rows=rows,
                           totals=[
                               ('Opening cash', data['opening_balance']),
                               ('Cash in', data['total_debit']),
                               ('Cash out', data['total_credit']),
                               ('Closing cash', data['closing_balance']),
                           ],
                           export_url=url_for('reports.cashbook_report', from_date=from_date, to_date=to_date, format='csv'),
                           back_url=url_for('reports.reports_list'))


@reports.route('/reports/member-savings-control')
@login_required
@role_required('admin', 'treasurer', 'secretary', 'exco')
def member_savings_control():
    db = get_db()
    as_of = request.args.get('as_of') or _today()
    hi = f'{as_of} 23:59:59'
    rows_db = db.execute('''
        SELECT m.member_number, m.first_name, m.last_name, m.email, m.status,
               COALESCE(SUM(CASE WHEN s.date <= ? THEN s.amount ELSE 0 END), 0) AS savings_balance,
               COALESCE(SUM(CASE WHEN s.date <= ? THEN s.share_capital ELSE 0 END), 0) AS share_capital,
               MAX(s.date) AS last_savings_date
        FROM members m
        LEFT JOIN savings s ON s.member_id = m.id
        GROUP BY m.id, m.member_number, m.first_name, m.last_name, m.email, m.status
        ORDER BY m.member_number, m.last_name, m.first_name
    ''', (hi, hi)).fetchall()
    columns = [
        {'key': 'member_number', 'label': 'Member #'},
        {'key': 'member_name', 'label': 'Member Name'},
        {'key': 'email', 'label': 'Email'},
        {'key': 'status', 'label': 'Status'},
        {'key': 'savings_balance', 'label': 'Savings Balance'},
        {'key': 'share_capital', 'label': 'Share Capital'},
        {'key': 'last_savings_date', 'label': 'Last Savings Date'},
    ]
    rows = [{
        'member_number': r['member_number'] or '',
        'member_name': f"{r['first_name']} {r['last_name']}",
        'email': r['email'] or '',
        'status': r['status'] or '',
        'savings_balance': f"{float(r['savings_balance'] or 0):.2f}",
        'share_capital': f"{float(r['share_capital'] or 0):.2f}",
        'last_savings_date': str(r['last_savings_date'] or '')[:10],
    } for r in rows_db]

    if request.args.get('format') == 'csv':
        return _csv_response('member_savings_control.csv', columns, rows)

    total_savings = sum(float(r['savings_balance']) for r in rows)
    total_shares = sum(float(r['share_capital']) for r in rows)
    return render_template('admin/report-table.html',
                           title='Member Savings Control',
                           subtitle=f'As at {as_of}',
                           columns=columns, rows=rows,
                           totals=[('Members', str(len(rows))), ('Savings balance', total_savings), ('Share capital', total_shares)],
                           export_url=url_for('reports.member_savings_control', as_of=as_of, format='csv'),
                           back_url=url_for('reports.reports_list'))


@reports.route('/reports/loan-portfolio')
@login_required
@role_required('admin', 'treasurer', 'secretary', 'exco')
def loan_portfolio_report():
    db = get_db()
    as_of = request.args.get('as_of') or _today()
    rows_db = db.execute('''
        SELECT l.loan_number, l.amount, l.total_repayment, l.balance, l.status,
               l.interest_rate, l.tenure, l.next_payment_date, l.date_applied,
               l.disbursement_date,
               m.member_number, m.first_name, m.last_name
        FROM loans l
        JOIN members m ON m.id = l.member_id
        ORDER BY l.status, l.next_payment_date, l.loan_number
    ''').fetchall()

    columns = [
        {'key': 'loan_number', 'label': 'Loan #'},
        {'key': 'member_number', 'label': 'Member #'},
        {'key': 'member_name', 'label': 'Member Name'},
        {'key': 'status', 'label': 'Status'},
        {'key': 'amount', 'label': 'Principal'},
        {'key': 'total_repayment', 'label': 'Total Repayable'},
        {'key': 'total_repaid', 'label': 'Total Repaid'},
        {'key': 'balance', 'label': 'Outstanding'},
        {'key': 'next_payment_date', 'label': 'Next Due Date'},
        {'key': 'aging_bucket', 'label': 'Aging'},
    ]
    rows = []
    as_of_dt = datetime.strptime(as_of, '%Y-%m-%d')
    for r in rows_db:
        total_repayment = float(r['total_repayment'] or 0)
        balance = float((r['balance'] if r['balance'] is not None else r['amount']) or 0)
        # Total repaid = what's been cleared off the balance. Derived so it's
        # correct for migrated loans too (which have no per-repayment rows).
        total_repaid = max(0.0, total_repayment - balance)

        if r['status'] == 'active':
            d = loan_delinquency(r, as_of=as_of_dt)
            due_text = str(d['next_due_date'] or '')[:10] if d['next_due_date'] else \
                str(r['next_payment_date'] or '')[:10]
            bucket = d['bucket']
        else:
            due_text = ''
            bucket = '—'

        rows.append({
            'loan_number': r['loan_number'] or '',
            'member_number': r['member_number'] or '',
            'member_name': f"{r['first_name']} {r['last_name']}",
            'status': r['status'] or '',
            'amount': f"{float(r['amount'] or 0):.2f}",
            'total_repayment': f"{total_repayment:.2f}",
            'total_repaid': f"{total_repaid:.2f}",
            'balance': f"{balance:.2f}",
            'next_payment_date': due_text,
            'aging_bucket': bucket,
        })

    if request.args.get('format') == 'csv':
        return _csv_response('loan_portfolio_aging.csv', columns, rows)

    return render_template('admin/report-table.html',
                           title='Loan Portfolio and Aging',
                           subtitle=f'As at {as_of}',
                           columns=columns, rows=rows,
                           totals=[
                               ('Loans', str(len(rows))),
                               ('Principal', sum(float(r['amount']) for r in rows)),
                               ('Repaid', sum(float(r['total_repaid']) for r in rows)),
                               ('Outstanding', sum(float(r['balance']) for r in rows)),
                           ],
                           export_url=url_for('reports.loan_portfolio_report', as_of=as_of, format='csv'),
                           back_url=url_for('reports.reports_list'))
