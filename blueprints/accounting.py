"""
Accounting blueprint — general-ledger views: chart of accounts, trial balance,
and the journal register. This is the auditable face of the double-entry ledger.
"""

import csv
import io
import secrets
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, make_response, jsonify
from flask_login import login_required, current_user

from database import get_db
from utils import role_required, audit
from ledger import (get_accounts, trial_balance, backfill_from_transactions,
                    ledger_reconciliation, account_ledger, journal_entry_detail,
                    get_lock_date, reverse_journal_entry, PeriodLockedError,
                    UnsupportedReversalError, reversal_support,
                    get_default_cash_account, get_cash_bank_accounts,
                    get_postable_cash_accounts)
from voucher_service import post_voucher, bank_accounts as voucher_bank_accounts, postable_accounts

accounting = Blueprint('accounting', __name__, url_prefix='/accounting')


@accounting.route('/vouchers', methods=['GET'])
@login_required
@role_required('admin', 'treasurer')
def vouchers():
    db = get_db()
    rows = db.execute('SELECT * FROM vouchers ORDER BY date DESC, id DESC LIMIT 100').fetchall()
    return render_template('accounting/vouchers.html', vouchers=rows)


@accounting.route('/vouchers/new', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'treasurer')
def new_voucher():
    db = get_db()
    if request.method == 'POST':
        try:
            count = min(int(request.form.get('line_count', '0') or 0), 100)
            lines = []
            for i in range(count):
                lines.append({k: request.form.get(f'{k}_{i}', '') for k in ('account','debit','credit','amount','member_id','loan_id','memo')})
            vid, created = post_voucher(db, request.form, lines, current_user)
            db.execute('INSERT INTO audit_log (user_id, action, entity_type, entity_id, details) VALUES (?, ?, ?, ?, ?)',
                       (current_user.id, 'VOUCHER_POSTED' if created else 'VOUCHER_DUPLICATE', 'voucher', vid, request.form.get('description','')))
            db.commit()
            flash('Voucher posted.' if created else 'This voucher was already posted; no duplicate was created.', 'success')
            return redirect(url_for('accounting.voucher_detail', voucher_id=vid))
        except Exception as exc:
            db.rollback()
            flash(str(exc), 'danger')
    members = db.execute('SELECT id, member_number, first_name, last_name FROM members WHERE status = ? ORDER BY member_number', ('active',)).fetchall()
    loans = db.execute("SELECT id, loan_number, member_id, amount, status FROM loans WHERE status = 'pending' ORDER BY id DESC").fetchall()
    return render_template('accounting/voucher_new.html', token=secrets.token_urlsafe(24), today=_today(),
                           accounts=postable_accounts(db), banks=voucher_bank_accounts(db), members=members, loans=loans)


@accounting.route('/vouchers/<int:voucher_id>')
@login_required
@role_required('admin', 'treasurer')
def voucher_detail(voucher_id):
    db = get_db()
    voucher = db.execute('SELECT * FROM vouchers WHERE id = ?', (voucher_id,)).fetchone()
    if not voucher:
        return 'Voucher not found', 404
    entry = journal_entry_detail(db, voucher['journal_entry_id']) if voucher['journal_entry_id'] else None
    return render_template('accounting/voucher_detail.html', voucher=voucher, entry=entry)


def _today():
    return datetime.now().strftime('%Y-%m-%d')


def _year_start():
    return datetime.now().replace(month=1, day=1).strftime('%Y-%m-%d')


def _bank_account_rows(db):
    """Return active cash/bank GL accounts used for bank-position reporting."""
    return get_cash_bank_accounts(db)


def _bank_positions(db, from_date, to_date):
    default_cash_account = get_default_cash_account(db)
    positions = []
    totals = {
        'opening_balance': 0.0,
        'cash_in': 0.0,
        'cash_out': 0.0,
        'closing_balance': 0.0,
        'entries': 0,
    }
    # A header with detail accounts under it is shown, so a stray balance on it
    # stays visible, but it is kept OUT of the totals: its children are listed
    # too, and adding both counts the same money twice.
    postable = {a['code'] for a in get_postable_cash_accounts(db)}
    for account in _bank_account_rows(db):
        data = account_ledger(db, account['code'], from_date, to_date)
        if not data:
            continue
        is_header = account['code'] not in postable
        row = {
            'code': account['code'],
            'name': account['name'],
            'parent_code': account.get('parent_code'),
            'is_header': is_header,
            'is_default': account['code'] == default_cash_account,
            'opening_balance': data['opening_balance'],
            'cash_in': data['total_debit'],
            'cash_out': data['total_credit'],
            'closing_balance': data['closing_balance'],
            'entries': data['count'],
        }
        positions.append(row)
        if is_header:
            continue
        totals['opening_balance'] += row['opening_balance']
        totals['cash_in'] += row['cash_in']
        totals['cash_out'] += row['cash_out']
        totals['closing_balance'] += row['closing_balance']
        totals['entries'] += row['entries']
    for key in ('opening_balance', 'cash_in', 'cash_out', 'closing_balance'):
        totals[key] = round(totals[key], 2)
    return positions, totals, default_cash_account


def _savings_bank_line_scope(from_account, from_date=None, to_date=None):
    where = [
        'jl.account_code = ?',
        "je.source_module IN ('savings_deposit', 'savings')",
    ]
    params = [from_account]
    if from_date:
        where.append('je.date >= ?')
        params.append(from_date)
    if to_date:
        where.append('je.date <= ?')
        params.append(f'{to_date} 23:59:59')
    return ' AND '.join(where), params


def _savings_bank_reclass_preview(db, from_account, from_date=None, to_date=None):
    where, params = _savings_bank_line_scope(from_account, from_date, to_date)
    row = db.execute(f'''
        SELECT COUNT(*) AS line_count,
               COALESCE(SUM(jl.debit), 0) AS debit_total,
               COALESCE(SUM(jl.credit), 0) AS credit_total
        FROM journal_lines jl
        JOIN journal_entries je ON je.id = jl.entry_id
        WHERE {where}
    ''', tuple(params)).fetchone()
    return {
        'line_count': int(row['line_count'] or 0) if row else 0,
        'debit_total': float(row['debit_total'] or 0) if row else 0.0,
        'credit_total': float(row['credit_total'] or 0) if row else 0.0,
    }


@accounting.route('/chart')
@login_required
@role_required('admin', 'treasurer')
def chart_of_accounts():
    db = get_db()
    accounts = get_accounts(db, active_only=False)
    default_cash_account = get_default_cash_account(db)
    account_list = [dict(a) for a in accounts]
    by_code = {a['code']: a for a in account_list}
    children = {}
    for a in account_list:
        if a.get('parent_code'):
            children.setdefault(a['parent_code'], []).append(a)
    for a in account_list:
        a['children'] = children.get(a['code'], [])
        a['is_parent'] = bool(a['children'])
    # Group by type for display
    groups = {}
    for a in account_list:
        groups.setdefault(a['type'], []).append(a)
    order = ['asset', 'liability', 'equity', 'income', 'expense']
    grouped = [(t, groups[t]) for t in order if t in groups]
    return render_template('accounting/chart.html', grouped=grouped,
                           all_accounts=account_list, accounts_by_code=by_code,
                           default_cash_account=default_cash_account)


ACCOUNT_TYPES = ('asset', 'liability', 'equity', 'income', 'expense')


@accounting.route('/accounts/add', methods=['POST'])
@login_required
@role_required('admin')
def add_account():
    db = get_db()
    code   = request.form.get('code', '').strip()
    name   = request.form.get('name', '').strip()
    atype  = request.form.get('type', '').strip().lower()
    normal = request.form.get('normal_balance', '').strip().lower()
    parent = request.form.get('parent_code', '').strip() or None
    if parent:
        parent_row = db.execute(
            'SELECT code, type, normal_balance FROM accounts WHERE code = ? AND is_active = 1',
            (parent,)
        ).fetchone()
        if not parent_row:
            flash('Selected parent account does not exist or is inactive.', 'danger')
            return redirect(url_for('accounting.chart_of_accounts'))
        if not atype:
            atype = parent_row['type']
        if atype != parent_row['type']:
            flash('A detail account must use the same type as its parent account.', 'danger')
            return redirect(url_for('accounting.chart_of_accounts'))
        if not normal:
            normal = parent_row['normal_balance']
    if not code or not name or atype not in ACCOUNT_TYPES:
        flash('Account code, name, and a valid type are required.', 'danger')
        return redirect(url_for('accounting.chart_of_accounts'))
    # Normal balance defaults from type if not specified
    if normal not in ('debit', 'credit'):
        normal = 'debit' if atype in ('asset', 'expense') else 'credit'
    # Only something owned or owed can hold money; a detail account under a
    # parent can, its parent then becomes a heading.
    is_cash = 1 if (request.form.get('is_cash_account') and atype in ('asset', 'liability')) else 0
    try:
        db.execute(
            'INSERT INTO accounts (code, name, type, normal_balance, parent_code, is_active, '
            'is_cash_account) VALUES (?, ?, ?, ?, ?, 1, ?)',
            (code, name, atype, normal, parent, is_cash))
        db.commit()
        audit(db, 'ADD_ACCOUNT', 'accounting',
              f'Added account {code} — {name} ({atype})'
              + (' — money moves through it' if is_cash else ''))
        flash(f'Account {code} — {name} added.', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Could not add account (the code may already exist): {e}', 'danger')
    return redirect(url_for('accounting.chart_of_accounts'))


@accounting.route('/accounts/default-cash', methods=['POST'])
@login_required
@role_required('admin')
def set_default_cash_account():
    db = get_db()
    code = request.form.get('default_cash_account', '').strip()
    account = db.execute(
        "SELECT code, name FROM accounts WHERE code = ? AND type = 'asset' AND is_active = 1",
        (code,)
    ).fetchone()
    if not account:
        flash('Choose an active asset account for cash/bank posting.', 'danger')
        return redirect(url_for('accounting.chart_of_accounts'))
    # A blank choice anywhere else falls back to this account, so it must be one
    # money can actually sit in. A heading with detail accounts under it would
    # get counted twice in the cash position.
    if code not in {a['code'] for a in get_postable_cash_accounts(db)}:
        flash(f'{code} - {account["name"]} has detail accounts under it, so it is a '
              f'heading rather than a place money sits. Choose one of the accounts '
              f'beneath it instead.', 'danger')
        return redirect(url_for('accounting.chart_of_accounts'))
    try:
        existing = db.execute(
            "SELECT id FROM settings WHERE key = 'default_cash_account'"
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE settings SET value = ? WHERE key = 'default_cash_account'",
                (code,)
            )
        else:
            db.execute(
                "INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                ('default_cash_account', code, 'Default cash/bank GL account for receipts and disbursements')
            )
        db.commit()
        audit(db, 'SET_DEFAULT_CASH_ACCOUNT', 'accounting',
              f'Set default cash/bank posting account to {code} - {account["name"]}')
        flash(f'Default cash/bank posting account set to {code} - {account["name"]}.', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Could not update default cash/bank account: {e}', 'danger')
    return redirect(url_for('accounting.chart_of_accounts'))


@accounting.route('/accounts/<code>/toggle', methods=['POST'])
@login_required
@role_required('admin')
def toggle_account(code):
    db = get_db()
    a = db.execute('SELECT is_active FROM accounts WHERE code = ?', (code,)).fetchone()
    if not a:
        flash('Account not found.', 'danger')
        return redirect(url_for('accounting.chart_of_accounts'))
    new_val = 0 if a['is_active'] else 1
    db.execute('UPDATE accounts SET is_active = ? WHERE code = ?', (new_val, code))
    db.commit()
    audit(db, 'TOGGLE_ACCOUNT', 'accounting',
          f'Account {code} {"reactivated" if new_val else "deactivated"}')
    flash(f'Account {code} {"reactivated" if new_val else "deactivated"}.', 'success')
    return redirect(url_for('accounting.chart_of_accounts'))


@accounting.route('/accounts/<code>/cash-toggle', methods=['POST'])
@login_required
@role_required('admin')
def toggle_cash_account(code):
    """Mark whether money moves through this account.

    Marked accounts are what a treasurer picks when recording a contribution,
    repayment, payout or disbursement. Banks and cash in hand obviously belong;
    so does a control account like a Cooperative Fund Account, where salary
    deductions sit until the employer remits them and the receipt is
    Dr Bank / Cr Fund.
    """
    db = get_db()
    a = db.execute('SELECT name, type, is_cash_account FROM accounts WHERE code = ?',
                   (code,)).fetchone()
    if not a:
        flash('Account not found.', 'danger')
        return redirect(url_for('accounting.chart_of_accounts'))
    turning_on = not a['is_cash_account']
    # Money sits in something owned or owed. Letting an income or expense
    # account be the cash side of a receipt would book the same amount twice.
    if turning_on and a['type'] not in ('asset', 'liability'):
        flash(f'{code} — {a["name"]} is a {a["type"]} account. Only asset or liability '
              f'accounts can hold money.', 'danger')
        return redirect(url_for('accounting.chart_of_accounts'))
    if turning_on and code in {p['code'] for p in _header_codes(db)}:
        flash(f'{code} — {a["name"]} has detail accounts under it, so it is a heading. '
              f'Mark the accounts beneath it instead.', 'danger')
        return redirect(url_for('accounting.chart_of_accounts'))

    db.execute('UPDATE accounts SET is_cash_account = ? WHERE code = ?',
               (1 if turning_on else 0, code))
    db.commit()
    audit(db, 'TOGGLE_CASH_ACCOUNT', 'accounting',
          f'Account {code} {"marked as" if turning_on else "no longer"} an account money moves through')
    flash(f'{code} — {a["name"]} '
          f'{"can now be chosen when recording money in and out." if turning_on else "will no longer be offered."}',
          'success')
    return redirect(url_for('accounting.chart_of_accounts'))


def _header_codes(db):
    """Accounts that have active children, so are headings rather than places
    money sits."""
    return db.execute(
        "SELECT DISTINCT parent_code AS code FROM accounts "
        "WHERE is_active = 1 AND parent_code IS NOT NULL AND parent_code != ''"
    ).fetchall()


@accounting.route('/journal/new', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'treasurer')
def new_journal():
    db = get_db()
    if request.method == 'POST':
        from ledger import post_journal
        desc = request.form.get('description', '').strip()
        date = request.form.get('date') or None
        ref  = request.form.get('reference', '').strip()
        codes   = request.form.getlist('account')
        debits  = request.form.getlist('debit')
        credits = request.form.getlist('credit')
        memos   = request.form.getlist('memo')
        lines = []
        for c, d, cr, memo in zip(codes, debits, credits, memos):
            c = (c or '').strip()
            if not c:
                continue
            try:
                d_v = float(d or 0); c_v = float(cr or 0)
            except ValueError:
                continue
            if d_v == 0 and c_v == 0:
                continue
            lines.append({'account': c, 'debit': d_v, 'credit': c_v, 'memo': (memo or '').strip()})
        try:
            if not desc:
                raise ValueError('A description is required.')
            eid = post_journal(db, desc, lines, date=date, reference=ref,
                               source_module='manual', created_by=current_user.id)
            if eid is None:
                raise ValueError('Enter at least one debit and one credit line.')
            db.commit()
            audit(db, 'MANUAL_JOURNAL', 'accounting', f'Posted manual journal: {desc}')
            flash('Journal entry posted.', 'success')
            return redirect(url_for('accounting.journal_register'))
        except ValueError as e:
            db.rollback()
            flash(str(e), 'danger')
        except Exception as e:
            db.rollback()
            flash(f'Error posting entry: {e}', 'danger')
    return render_template('accounting/journal_new.html',
                           accounts=get_accounts(db, active_only=True),
                           today=datetime.now().strftime('%Y-%m-%d'))


@accounting.route('/trial-balance')
@login_required
@role_required('admin', 'treasurer')
def trial_balance_view():
    db = get_db()
    as_of = request.args.get('as_of', datetime.now().strftime('%Y-%m-%d'))
    tb = trial_balance(db, as_of=as_of)
    fmt = request.args.get('format')
    if fmt:
        from report_export import report_response
        rows = [{'cells': [r['code'], r['name'], r['type'].title(), r['debit'], r['credit']]}
                for r in tb['rows']]
        rows.append({'cells': ['', 'Totals', '', tb['total_debit'], tb['total_credit']], 'bold': True})
        report = {'title': 'Trial Balance', 'subtitle': f'As at {as_of}',
                  'sections': [{'columns': ['Code', 'Account', 'Type', 'Debit', 'Credit'], 'rows': rows}]}
        return report_response(report, fmt,
                               redirect_url=url_for('accounting.trial_balance_view', as_of=as_of))
    return render_template('accounting/trial_balance.html', tb=tb, as_of=as_of)


@accounting.route('/reconciliation')
@login_required
@role_required('admin', 'treasurer')
def reconciliation():
    db = get_db()
    rec = ledger_reconciliation(db)
    return render_template('accounting/reconciliation.html', rec=rec)


@accounting.route('/bank-accounts')
@login_required
@role_required('admin', 'treasurer')
def bank_accounts():
    db = get_db()
    from_date = request.args.get('from_date') or _year_start()
    to_date = request.args.get('to_date') or _today()
    positions, totals, default_cash_account = _bank_positions(db, from_date, to_date)
    bank_accounts_list = _bank_account_rows(db)
    for account in bank_accounts_list:
        account['is_default'] = account['code'] == default_cash_account
    reclass_from = request.args.get('reclass_from', '1000')
    reclass_preview = _savings_bank_reclass_preview(db, reclass_from, from_date, to_date)

    if request.args.get('format') == 'csv':
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow([
            'account_code', 'account_name', 'opening_balance', 'cash_in',
            'cash_out', 'closing_balance', 'entries', 'is_default',
            'from_date', 'to_date',
        ])
        for row in positions:
            writer.writerow([
                row['code'], row['name'], f"{row['opening_balance']:.2f}",
                f"{row['cash_in']:.2f}", f"{row['cash_out']:.2f}",
                f"{row['closing_balance']:.2f}", row['entries'],
                'yes' if row['is_default'] else 'no', from_date, to_date,
            ])
        writer.writerow([])
        writer.writerow([
            'TOTAL', '', f"{totals['opening_balance']:.2f}",
            f"{totals['cash_in']:.2f}", f"{totals['cash_out']:.2f}",
            f"{totals['closing_balance']:.2f}", totals['entries'], '', from_date, to_date,
        ])
        resp = make_response(out.getvalue())
        resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
        resp.headers['Content-Disposition'] = 'attachment; filename=bank_accounts_position.csv'
        return resp

    return render_template('accounting/bank-accounts.html',
                           positions=positions, totals=totals,
                           default_cash_account=default_cash_account,
                           bank_accounts=bank_accounts_list,
                           reclass_from=reclass_from,
                           reclass_preview=reclass_preview,
                           from_date=from_date, to_date=to_date,
                           generated_on=datetime.now())


@accounting.route('/bank-accounts/reclassify-savings', methods=['POST'])
@login_required
@role_required('admin')
def reclassify_savings_bank_account():
    db = get_db()
    from_account = request.form.get('from_account', '').strip()
    to_account = request.form.get('to_account', '').strip()
    from_date = request.form.get('from_date', '').strip() or None
    to_date = request.form.get('to_date', '').strip() or None

    if not from_account or not to_account or from_account == to_account:
        flash('Choose different source and target bank accounts.', 'danger')
        return redirect(url_for('accounting.bank_accounts', from_date=from_date or _year_start(), to_date=to_date or _today()))

    accounts = {
        r['code']: r for r in db.execute('''
            SELECT code, name, type, is_active
            FROM accounts
            WHERE code IN (?, ?) AND type = 'asset' AND is_active = 1
        ''', (from_account, to_account)).fetchall()
    }
    if from_account not in accounts or to_account not in accounts:
        flash('Both source and target must be active asset bank/cash accounts.', 'danger')
        return redirect(url_for('accounting.bank_accounts', from_date=from_date or _year_start(), to_date=to_date or _today()))

    preview = _savings_bank_reclass_preview(db, from_account, from_date, to_date)
    if preview['line_count'] == 0:
        flash('No savings bank-side journal lines found for the selected source account and period.', 'info')
        return redirect(url_for('accounting.bank_accounts', from_date=from_date or _year_start(), to_date=to_date or _today()))

    where, params = _savings_bank_line_scope(from_account, from_date, to_date)
    try:
        db.execute(f'''
            UPDATE journal_lines
               SET account_code = ?
             WHERE id IN (
                SELECT jl.id
                FROM journal_lines jl
                JOIN journal_entries je ON je.id = jl.entry_id
                WHERE {where}
             )
        ''', tuple([to_account] + params))
        db.commit()
        audit(db, 'RECLASSIFY_SAVINGS_BANK', 'accounting',
              f"Moved {preview['line_count']} savings bank line(s) from {from_account} to {to_account}"
              f" for {from_date or 'beginning'} to {to_date or 'today'}")
        flash(
            f"Moved {preview['line_count']} savings bank line(s) from "
            f"{from_account} - {accounts[from_account]['name']} to "
            f"{to_account} - {accounts[to_account]['name']}.",
            'success',
        )
    except Exception as e:
        db.rollback()
        flash(f'Could not reclassify savings bank lines: {e}', 'danger')
    return redirect(url_for('accounting.bank_accounts', from_date=from_date or _year_start(), to_date=to_date or _today()))


@accounting.route('/bank-accounts/<code>')
@login_required
@role_required('admin', 'treasurer')
def bank_account_detail(code):
    db = get_db()
    account = db.execute('''
        SELECT code FROM accounts
        WHERE code = ? AND is_active = 1 AND type = 'asset'
          AND (
                code = '1000'
             OR parent_code = '1000'
             OR LOWER(name) LIKE ?
             OR LOWER(name) LIKE ?
             OR LOWER(name) LIKE ?
          )
    ''', (code, '%bank%', '%cash%', '%wallet%')).fetchone()
    if not account:
        flash('Bank/cash account not found.', 'danger')
        return redirect(url_for('accounting.bank_accounts'))

    from_date = request.args.get('from_date') or _year_start()
    to_date = request.args.get('to_date') or _today()
    data = account_ledger(db, code, from_date, to_date)
    statement_balance_raw = request.args.get('statement_balance', '').strip()
    statement_balance = None
    variance = None
    variance_reconciled = None
    if statement_balance_raw:
        try:
            statement_balance = float(statement_balance_raw.replace(',', ''))
            variance = round(statement_balance - data['closing_balance'], 2)
            variance_reconciled = abs(variance) < 0.01
        except ValueError:
            flash('Statement balance must be a valid number.', 'warning')

    if request.args.get('format') == 'csv':
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(['account_code', data['account']['code']])
        writer.writerow(['account_name', data['account']['name']])
        writer.writerow(['from_date', from_date])
        writer.writerow(['to_date', to_date])
        writer.writerow(['opening_balance', f"{data['opening_balance']:.2f}"])
        writer.writerow(['cash_in', f"{data['total_debit']:.2f}"])
        writer.writerow(['cash_out', f"{data['total_credit']:.2f}"])
        writer.writerow(['gl_closing_balance', f"{data['closing_balance']:.2f}"])
        if statement_balance is not None:
            writer.writerow(['statement_balance', f"{statement_balance:.2f}"])
            writer.writerow(['variance', f"{variance:.2f}"])
        writer.writerow([])
        writer.writerow([
            'date', 'entry_number', 'description', 'reference', 'source_module',
            'memo', 'cash_in', 'cash_out', 'running_balance',
        ])
        for e in data['entries']:
            writer.writerow([
                str(e.get('date') or '')[:10],
                e.get('entry_number') or f"JE-{e.get('entry_id')}",
                e.get('description') or '',
                e.get('reference') or '',
                e.get('source_module') or '',
                e.get('memo') or '',
                f"{float(e.get('debit') or 0):.2f}",
                f"{float(e.get('credit') or 0):.2f}",
                f"{float(e.get('balance') or 0):.2f}",
            ])
        resp = make_response(out.getvalue())
        resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
        resp.headers['Content-Disposition'] = f'attachment; filename=bank_account_{code}.csv'
        return resp

    return render_template('accounting/bank-account-detail.html',
                           data=data, account=data['account'],
                           from_date=from_date, to_date=to_date,
                           statement_balance=statement_balance,
                           statement_balance_raw=statement_balance_raw,
                           variance=variance,
                           variance_reconciled=variance_reconciled,
                           generated_on=datetime.now())


def _pct(source, name, default):
    try:
        return float(source.get(name, default))
    except (TypeError, ValueError):
        return default


@accounting.route('/dividends')
@login_required
@role_required('admin', 'treasurer')
def dividends():
    from dividends import compute_dividend_schedule
    db = get_db()
    today = datetime.now()
    from_date = request.args.get('from_date', today.replace(month=1, day=1).strftime('%Y-%m-%d'))
    to_date   = request.args.get('to_date', today.strftime('%Y-%m-%d'))
    rates = {
        'dividend_pct':    _pct(request.args, 'dividend_pct', 50),
        'reserve_pct':     _pct(request.args, 'reserve_pct', 25),
        'honorarium_pct':  _pct(request.args, 'honorarium_pct', 10),
        'other_pct':       _pct(request.args, 'other_pct', 15),
        'patronage_split': _pct(request.args, 'patronage_split', 0),
    }
    sched = None
    if request.args.get('preview'):
        sched = compute_dividend_schedule(db, from_date, to_date, **rates)
    declarations = db.execute(
        'SELECT * FROM dividend_declarations ORDER BY declared_at DESC'
    ).fetchall()
    return render_template('accounting/dividends.html',
                           from_date=from_date, to_date=to_date,
                           sched=sched, declarations=declarations, **rates)


@accounting.route('/dividends/declare', methods=['POST'])
@login_required
@role_required('admin')
def declare_dividend():
    from dividends import declare_dividends
    db = get_db()
    try:
        from_date = request.form['from_date']
        to_date   = request.form['to_date']
        decl_id = declare_dividends(
            db, from_date, to_date,
            dividend_pct=_pct(request.form, 'dividend_pct', 50),
            reserve_pct=_pct(request.form, 'reserve_pct', 25),
            honorarium_pct=_pct(request.form, 'honorarium_pct', 10),
            other_pct=_pct(request.form, 'other_pct', 15),
            patronage_split=_pct(request.form, 'patronage_split', 0),
            declared_by=current_user.id,
        )
        db.commit()
        audit(db, 'DECLARE_DIVIDEND', 'accounting',
              f'Declared dividend #{decl_id} for {from_date} to {to_date}')
        flash('Dividend declared and credited to members\' savings.', 'success')
        return redirect(url_for('accounting.dividend_detail', decl_id=decl_id))
    except ValueError as e:
        db.rollback()
        flash(str(e), 'warning')
        return redirect(url_for('accounting.dividends'))
    except Exception as e:
        db.rollback()
        flash(f'Error declaring dividend: {e}', 'danger')
        return redirect(url_for('accounting.dividends'))


@accounting.route('/dividends/<int:decl_id>')
@login_required
@role_required('admin', 'treasurer')
def dividend_detail(decl_id):
    db = get_db()
    decl = db.execute('SELECT * FROM dividend_declarations WHERE id = ?', (decl_id,)).fetchone()
    if not decl:
        flash('Dividend declaration not found.', 'danger')
        return redirect(url_for('accounting.dividends'))
    allocs = db.execute('''
        SELECT da.*, m.member_number, m.first_name, m.last_name
        FROM dividend_allocations da JOIN members m ON m.id = da.member_id
        WHERE da.declaration_id = ? ORDER BY da.total DESC
    ''', (decl_id,)).fetchall()
    fmt = request.args.get('format')
    if fmt:
        from report_export import report_response
        appro = [
            {'cells': ['Net surplus', decl['net_surplus']], 'bold': True},
            {'cells': ['Statutory reserve', decl['reserve_amount']]},
            {'cells': ['Honorarium', decl['honorarium_amount']]},
            {'cells': ['Other', decl['other_amount']]},
            {'cells': ['Dividend pool', decl['dividend_pool']], 'bold': True},
        ]
        alloc_rows = [{'cells': [a['member_number'], f"{a['first_name']} {a['last_name']}",
                                 a['savings_base'], a['dividend_savings'],
                                 a['dividend_patronage'], a['total']]} for a in allocs]
        report = {
            'title': 'Dividend Declaration',
            'subtitle': f"{decl['period_from']} to {decl['period_to']}",
            'sections': [
                {'heading': 'Appropriation', 'columns': ['', 'Amount'], 'rows': appro},
                {'heading': 'Member Allocations',
                 'columns': ['Member No', 'Name', 'Savings', 'On savings', 'Patronage', 'Total'],
                 'rows': alloc_rows},
            ],
        }
        return report_response(report, fmt,
                               redirect_url=url_for('accounting.dividend_detail', decl_id=decl_id))
    return render_template('accounting/dividend_detail.html', decl=decl, allocs=allocs)


@accounting.route('/backfill', methods=['POST'])
@login_required
@role_required('admin')
def backfill():
    """Post journal entries for existing transactions not yet in the ledger."""
    db = get_db()
    try:
        n, skipped = backfill_from_transactions(db, created_by=current_user.id)
        db.commit()
        audit(db, 'GL_BACKFILL', 'accounting',
              f'Backfilled {n} transactions into the general ledger '
              f'({skipped} skipped as covered by the opening balance)')
        if n:
            flash(f'Posted {n} historical transaction(s) to the general ledger.', 'success')
        else:
            flash('The ledger is already up to date — nothing to backfill.', 'info')
        if skipped:
            # Migrated records are already inside the opening balance. Say so,
            # or the untouched count reads like a failure.
            flash(f'{skipped} record(s) dated on or before the opening balance were left '
                  f'alone — the opening entry already accounts for them.', 'info')
    except Exception as e:
        db.rollback()
        flash(f'Error backfilling ledger: {e}', 'danger')
    return redirect(url_for('accounting.journal_register'))


def _source_link(db, module, source_id):
    """Resolve a journal entry's originating record to (label, url) for drill-down.

    Returns (label, None) when the source is known but has no dedicated page.
    """
    module = (module or '').lower()
    if not source_id:
        return (module.title() or 'Manual entry', None)
    try:
        if module == 'savings_deposit':
            # Precise linkage: source_id is the savings row.
            row = db.execute(
                'SELECT s.member_id, m.first_name, m.last_name, m.member_number '
                'FROM savings s LEFT JOIN members m ON m.id = s.member_id WHERE s.id = ?',
                (source_id,)).fetchone()
            if row and row['member_id']:
                return (f"Savings — {row['first_name']} {row['last_name']} (#{row['member_number']})",
                        url_for('members.member_savings_statement', member_id=row['member_id']))
            return ('Savings contribution', None)
        if module in ('savings', 'payments'):
            # Legacy coarse linkage: source_id was the member id.
            m = db.execute('SELECT id, first_name, last_name, member_number FROM members WHERE id = ?',
                           (source_id,)).fetchone()
            if m:
                return (f"Savings/payment — {m['first_name']} {m['last_name']} (#{m['member_number']})",
                        url_for('members.member_savings_statement', member_id=m['id']))
            return ('Member transaction', None)
        if module == 'loan_repayment':
            # Precise linkage: source_id is the repayment row.
            rep = db.execute(
                'SELECT r.loan_id, l.loan_number FROM repayments r '
                'LEFT JOIN loans l ON l.id = r.loan_id WHERE r.id = ?', (source_id,)).fetchone()
            if rep and rep['loan_id']:
                return (f"Loan repayment — {rep['loan_number'] or rep['loan_id']}",
                        url_for('loans.loan_detail', loan_id=rep['loan_id']))
            return ('Loan repayment', None)
        if module in ('loans', 'loan_disbursement'):
            row = db.execute(
                'SELECT id, loan_number FROM loans WHERE id = ?', (source_id,)).fetchone()
            if row:
                return (f"Loan {row['loan_number'] or row['id']}",
                        url_for('loans.loan_detail', loan_id=row['id']))
            return ('Loan transaction', None)
        if module == 'investments':
            return ('Investment', url_for('investments.investments_list'))
        if module == 'dividend':
            return ('Dividend declaration',
                    url_for('accounting.dividend_detail', decl_id=source_id))
        if module == 'opening':
            return ('Opening balance import', None)
    except Exception:
        pass
    return (module.title() or 'Manual entry', None)


@accounting.route('/ledger/<code>')
@login_required
@role_required('admin', 'treasurer')
def account_ledger_view(code):
    """Audit drill-down: every journal line that hit one account."""
    db = get_db()
    from_date = request.args.get('from_date', '')
    to_date   = request.args.get('to_date', '')
    data = account_ledger(db, code, from_date or None, to_date or None)
    if not data:
        flash(f'Account {code} not found.', 'danger')
        return redirect(url_for('accounting.chart_of_accounts'))
    return render_template('accounting/account-ledger.html',
                           data=data, account=data['account'],
                           from_date=from_date, to_date=to_date,
                           generated_on=datetime.now())


@accounting.route('/ledger/<code>/export')
@login_required
@role_required('admin', 'treasurer')
def account_ledger_export(code):
    """Export one GL account register as CSV for audit and spreadsheet analysis."""
    db = get_db()
    from_date = request.args.get('from_date', '')
    to_date   = request.args.get('to_date', '')
    data = account_ledger(db, code, from_date or None, to_date or None)
    if not data:
        flash(f'Account {code} not found.', 'danger')
        return redirect(url_for('accounting.chart_of_accounts'))

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        'account_code', 'account_name', 'account_type', 'normal_balance',
        'from_date', 'to_date', 'opening_balance',
    ])
    writer.writerow([
        data['account']['code'], data['account']['name'], data['account']['type'],
        data['account']['normal_balance'], from_date, to_date, data['opening_balance'],
    ])
    writer.writerow([])
    writer.writerow([
        'date', 'entry_number', 'description', 'reference', 'source_module',
        'source_id', 'line_memo', 'debit', 'credit', 'running_balance',
    ])
    for e in data['entries']:
        writer.writerow([
            str(e.get('date') or '')[:10],
            e.get('entry_number') or f"JE-{e.get('entry_id')}",
            e.get('description') or '',
            e.get('reference') or '',
            e.get('source_module') or '',
            e.get('source_id') or '',
            e.get('memo') or '',
            f"{float(e.get('debit') or 0):.2f}",
            f"{float(e.get('credit') or 0):.2f}",
            f"{float(e.get('balance') or 0):.2f}",
        ])
    writer.writerow([])
    writer.writerow(['totals', '', '', '', '', '', '', f"{data['total_debit']:.2f}", f"{data['total_credit']:.2f}", f"{data['closing_balance']:.2f}"])

    resp = make_response(out.getvalue())
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename=gl_register_{code}.csv'
    return resp


@accounting.route('/journal/<int:entry_id>')
@login_required
@role_required('admin', 'treasurer')
def journal_entry_view(entry_id):
    """Audit drill-down: one journal entry, its lines, and its source document."""
    db = get_db()
    data = journal_entry_detail(db, entry_id)
    if not data:
        flash('Journal entry not found.', 'danger')
        return redirect(url_for('accounting.journal_register'))
    src_label, src_url = _source_link(db, data['entry'].get('source_module'),
                                      data['entry'].get('source_id'))
    lock = get_lock_date(db)
    entry_locked = bool(lock) and str(data['entry'].get('date') or '')[:10] <= lock
    can_reverse, reverse_block = reversal_support(data['entry'].get('source_module'))
    return render_template('accounting/journal-entry.html',
                           data=data, entry=data['entry'], lines=data['lines'],
                           source_label=src_label, source_url=src_url,
                           lock_date=lock, entry_locked=entry_locked,
                           can_reverse=can_reverse, reverse_block=reverse_block)


@accounting.route('/journal/<int:entry_id>/quick-view')
@login_required
@role_required('admin', 'treasurer')
def journal_entry_quick_view(entry_id):
    """Compact journal detail for the in-app audit drawer."""
    db = get_db()
    data = journal_entry_detail(db, entry_id)
    if not data:
        return jsonify({'ok': False, 'message': 'Journal entry not found.'}), 404
    src_label, src_url = _source_link(db, data['entry'].get('source_module'),
                                      data['entry'].get('source_id'))
    lock = get_lock_date(db)
    entry_locked = bool(lock) and str(data['entry'].get('date') or '')[:10] <= lock
    can_reverse, reverse_block = reversal_support(data['entry'].get('source_module'))
    html = render_template('accounting/_journal_quick_view.html',
                           data=data, entry=data['entry'], lines=data['lines'],
                           source_label=src_label, source_url=src_url,
                           lock_date=lock, entry_locked=entry_locked,
                           can_reverse=can_reverse, reverse_block=reverse_block)
    return jsonify({
        'ok': True,
        'title': data['entry'].get('entry_number') or f"JE-{entry_id}",
        'html': html,
    })


@accounting.route('/journal/<int:entry_id>/reverse', methods=['POST'])
@login_required
@role_required('admin', 'treasurer')
def reverse_entry(entry_id):
    db = get_db()
    reason = (request.form.get('reason') or '').strip()
    try:
        new_id, source_note = reverse_journal_entry(db, entry_id, created_by=current_user.id,
                                                    reason=reason)
        # Audited BEFORE the commit, so the log lands in the same transaction as
        # the reversal it describes.
        audit(db, 'REVERSE_JOURNAL', 'accounting',
              f'Reversed journal entry {entry_id} with new entry {new_id}. Reason: {reason}'
              + (f' ({source_note})' if source_note else ''))
        db.commit()
        msg = 'Entry reversed — a balanced offsetting entry has been posted.'
        if source_note:
            msg += ' ' + source_note
        flash(msg, 'success')
        return redirect(url_for('accounting.journal_entry_view', entry_id=new_id))
    except PeriodLockedError as e:
        db.rollback()
        flash(str(e), 'warning')
    except UnsupportedReversalError as e:
        db.rollback()
        flash(str(e), 'warning')
    except ValueError as e:
        db.rollback()
        flash(str(e), 'danger')
    except Exception as e:
        db.rollback()
        flash(f'Could not reverse entry: {e}', 'danger')
    return redirect(url_for('accounting.journal_entry_view', entry_id=entry_id))


@accounting.route('/period-close', methods=['GET'])
@login_required
@role_required('admin', 'treasurer')
def period_close():
    db = get_db()
    lock = get_lock_date(db)
    # Recent lock-date changes for an audit trail.
    try:
        history = db.execute(
            "SELECT username, description, timestamp FROM audit_log "
            "WHERE action = 'PERIOD_LOCK' ORDER BY id DESC LIMIT 10").fetchall()
    except Exception:
        history = []
    return render_template('accounting/period-close.html',
                           lock_date=lock, history=history,
                           today=datetime.now().strftime('%Y-%m-%d'))


@accounting.route('/period-close/set', methods=['POST'])
@login_required
@role_required('admin')
def set_lock_date():
    db = get_db()
    new_date = request.form.get('lock_date', '').strip()
    action = request.form.get('action', 'set')

    if action == 'clear':
        new_date = ''
    elif new_date:
        # Validate the date format.
        try:
            datetime.strptime(new_date, '%Y-%m-%d')
        except ValueError:
            flash('Enter a valid date (YYYY-MM-DD).', 'danger')
            return redirect(url_for('accounting.period_close'))

    row = db.execute("SELECT value FROM settings WHERE key = 'books_lock_date'").fetchone()
    if row is None:
        db.execute("INSERT INTO settings (key, value, description) VALUES (?, ?, ?)",
                   ('books_lock_date', new_date, 'Books locked through this date'))
    else:
        db.execute("UPDATE settings SET value = ? WHERE key = 'books_lock_date'", (new_date,))
    db.commit()

    if new_date:
        audit(db, 'PERIOD_LOCK', 'accounting', f'Books locked through {new_date}')
        flash(f'Books are now locked through {new_date}. Entries on or before that date are blocked.', 'success')
    else:
        audit(db, 'PERIOD_LOCK', 'accounting', 'Books unlocked (lock date cleared)')
        flash('Books unlocked — no period lock is in effect.', 'info')
    return redirect(url_for('accounting.period_close'))


@accounting.route('/journal')
@login_required
@role_required('admin', 'treasurer')
def journal_register():
    db = get_db()
    entries = db.execute('''
        SELECT id, entry_number, date, description, reference, source_module
        FROM journal_entries
        ORDER BY date DESC, id DESC
        LIMIT 200
    ''').fetchall()
    # Fetch lines for the listed entries
    lines_by_entry = {}
    for e in entries:
        rows = db.execute('''
            SELECT jl.account_code, a.name AS account_name, jl.debit, jl.credit, jl.memo
            FROM journal_lines jl
            LEFT JOIN accounts a ON a.code = jl.account_code
            WHERE jl.entry_id = ?
            ORDER BY jl.debit DESC, jl.id
        ''', (e['id'],)).fetchall()
        lines_by_entry[e['id']] = rows
    return render_template('accounting/journal.html',
                           entries=entries, lines_by_entry=lines_by_entry)


@accounting.route('/journal/export')
@login_required
@role_required('admin', 'treasurer')
def journal_register_export():
    """Export the journal register line-by-line as CSV."""
    db = get_db()
    from_date = request.args.get('from_date', '')
    to_date = request.args.get('to_date', '')
    where = []
    params = []
    if from_date:
        where.append('je.date >= ?')
        params.append(from_date)
    if to_date:
        where.append('je.date <= ?')
        params.append(f'{to_date} 23:59:59')
    where_sql = 'WHERE ' + ' AND '.join(where) if where else ''

    rows = db.execute(f'''
        SELECT je.entry_number, je.date, je.description, je.reference,
               je.source_module, je.source_id,
               jl.account_code, a.name AS account_name, jl.memo, jl.debit, jl.credit
        FROM journal_entries je
        JOIN journal_lines jl ON jl.entry_id = je.id
        LEFT JOIN accounts a ON a.code = jl.account_code
        {where_sql}
        ORDER BY je.date DESC, je.id DESC, jl.id ASC
    ''', tuple(params)).fetchall()

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        'entry_number', 'date', 'description', 'reference', 'source_module',
        'source_id', 'account_code', 'account_name', 'line_memo', 'debit', 'credit',
    ])
    for r in rows:
        writer.writerow([
            r['entry_number'], str(r['date'] or '')[:10], r['description'] or '',
            r['reference'] or '', r['source_module'] or '', r['source_id'] or '',
            r['account_code'], r['account_name'] or '', r['memo'] or '',
            f"{float(r['debit'] or 0):.2f}", f"{float(r['credit'] or 0):.2f}",
        ])

    resp = make_response(out.getvalue())
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = 'attachment; filename=journal_register.csv'
    return resp
