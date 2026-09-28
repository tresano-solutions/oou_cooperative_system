"""
Data Migration Blueprint
Handles bulk import/export for migrating from the old cooperative app.
All imports use atomic transactions — the whole file succeeds or rolls back.
"""
import csv
from upload_history import record_upload
import random
import secrets
from contextlib import contextmanager
from datetime import datetime
from io import StringIO, TextIOWrapper

from flask import Blueprint, render_template, redirect, url_for, request, flash, make_response, session
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash

from crypto import encrypt_field
from database import get_db
from utils import role_required, audit, member_prefix, share_capital_split
from ledger import get_accounts, post_journal, account_exists, ACCUM_SURPLUS

migration = Blueprint('migration', __name__, url_prefix='/migration')

# ── Helper ────────────────────────────────────────────────────────────────────

def _resolve_member(db, row):
    """Return member row from member_number or email column, or None."""
    member_number = (row.get('member_number') or '').strip()
    email = (row.get('email') or '').strip()
    if member_number:
        m = db.execute('SELECT * FROM members WHERE member_number = ?', (member_number,)).fetchone()
        if m:
            return m
    if email:
        m = db.execute('SELECT * FROM members WHERE email = ?', (email,)).fetchone()
        if m:
            return m
    return None


def _ref(prefix):
    return f"{prefix}/{datetime.now().strftime('%Y%m%d')}/{random.randint(1000, 9999)}"


class _SkipRow(Exception):
    """Raise inside an import row to skip it without recording an error
    (e.g. a duplicate that already exists)."""


@contextmanager
def _row_savepoint(db):
    """Wrap one import row in a SAVEPOINT so a single failing row rolls back on
    its own instead of aborting the whole batch. PostgreSQL aborts the entire
    transaction on any error ('current transaction is aborted, commands ignored
    until end of transaction block'), which otherwise turns one bad row into a
    failure for every row after it. Works on SQLite too."""
    db.execute('SAVEPOINT import_row')
    try:
        yield
    except Exception:
        db.execute('ROLLBACK TO SAVEPOINT import_row')
        db.execute('RELEASE SAVEPOINT import_row')
        raise
    else:
        db.execute('RELEASE SAVEPOINT import_row')


# Canonical loan purpose names (lower-case key → canonical value)
_PURPOSE_MAP = {
    'regular':        'Regular',
    'personal':       'Regular',
    'general':        'Regular',
    'standard':       'Regular',
    'consumer':       'Regular',
    'housing':        'Housing',
    'house':          'Housing',
    'mortgage':       'Housing',
    'home':           'Housing',
    'property':       'Housing',
    'emergency':      'Emergency',
    'urgent':         'Emergency',
    'asset':          'Asset Purchase',
    'asset purchase': 'Asset Purchase',
    'equipment':      'Asset Purchase',
    'vehicle':        'Asset Purchase',
    'car':            'Asset Purchase',
    'school fees':    'School Fees',
    'school':         'School Fees',
    'education':      'School Fees',
    'tuition':        'School Fees',
    'fees':           'School Fees',
}

_VALID_PURPOSES = {'Regular', 'Housing', 'Emergency', 'Asset Purchase', 'School Fees'}


def _normalize_purpose(raw):
    """Map free-text purpose to one of the 5 canonical loan types."""
    key = (raw or '').strip().lower()
    if key in _PURPOSE_MAP:
        return _PURPOSE_MAP[key]
    # Check if it's already canonical (case-insensitive)
    for canon in _VALID_PURPOSES:
        if key == canon.lower():
            return canon
    # Unknown — keep as-is (won't break anything, just won't match interest rate presets)
    return raw.strip() if raw.strip() else 'Regular'


# Canonical loan status aliases
_STATUS_MAP = {
    'pending':   'pending',
    'applied':   'pending',
    'new':       'pending',
    'approved':  'approved',
    'active':    'active',
    'disbursed': 'active',
    'running':   'active',
    'ongoing':   'active',
    'completed': 'completed',
    'cleared':   'completed',
    'repaid':    'completed',
    'paid':      'completed',
    'closed':    'completed',
    'settled':   'completed',
    'defaulted': 'defaulted',
    'default':   'defaulted',
    'overdue':   'defaulted',
    'rejected':  'rejected',
    'declined':  'rejected',
    'denied':    'rejected',
}


def _normalize_loan_status(raw, fallback='pending'):
    key = (raw or '').strip().lower()
    return _STATUS_MAP.get(key, fallback)


# ── Dashboard ─────────────────────────────────────────────────────────────────

@migration.route('/')
@login_required
@role_required('admin')
def index():
    db = get_db()
    counts = {
        'members':     db.execute('SELECT COUNT(*) FROM members').fetchone()[0],
        'savings':     db.execute('SELECT COUNT(*) FROM savings').fetchone()[0],
        'loans':       db.execute('SELECT COUNT(*) FROM loans').fetchone()[0],
        'repayments':  db.execute('SELECT COUNT(*) FROM repayments').fetchone()[0],
        'expenses':    db.execute('SELECT COUNT(*) FROM expenses').fetchone()[0],
        'revenue':     db.execute('SELECT COUNT(*) FROM revenue').fetchone()[0],
        'investments': db.execute('SELECT COUNT(*) FROM investments').fetchone()[0],
        'honorarium':  db.execute('SELECT COUNT(*) FROM honorarium').fetchone()[0],
    }
    # Pop temp credentials from session (show once, then gone)
    new_credentials = session.pop('new_member_credentials', None)
    return render_template('admin/migration/index.html',
                           counts=counts,
                           new_credentials=new_credentials)


# ═══════════════════════════════════════════════════════════════════════════════
# OPENING BALANCES (general-ledger opening entry for migrations)
# ═══════════════════════════════════════════════════════════════════════════════

@migration.route('/opening-balances/template')
@login_required
@role_required('admin')
def template_opening():
    """CSV pre-listing every ledger account for the admin to fill with amounts."""
    db = get_db()
    out = StringIO()
    w = csv.writer(out)
    w.writerow(['account_code', 'account_name', 'normal_balance', 'debit', 'credit'])
    for a in get_accounts(db, active_only=True):
        w.writerow([a['code'], a['name'], a['normal_balance'], '', ''])
    return _csv_response(out, 'opening_balances_template.csv')


@migration.route('/opening-balances', methods=['GET', 'POST'])
@login_required
@role_required('admin')
def import_opening():
    """Post the cooperative's starting balances as one balanced opening journal
    entry. Any imbalance is plugged into Accumulated Surplus. Re-importing
    replaces the previous opening entry."""
    db = get_db()

    if request.method == 'POST':
        as_of = (request.form.get('as_of') or datetime.now().strftime('%Y-%m-%d')).strip()
        f = request.files.get('file')
        if not f or not f.filename:
            flash('Please choose a CSV file.', 'danger')
            return redirect(request.url)

        try:
            stream = TextIOWrapper(f.stream, encoding='utf-8-sig')
            reader = csv.DictReader(stream)
            fieldnames = set(reader.fieldnames or [])
            if 'account_code' not in fieldnames or not ({'debit', 'credit'} & fieldnames):
                flash('Missing columns: need account_code plus debit and/or credit.', 'danger')
                return redirect(request.url)

            lines, errors, net = [], [], 0.0
            for i, row in enumerate(reader, start=2):
                code = (row.get('account_code') or '').strip()
                if not code:
                    continue
                debit  = float((row.get('debit')  or '0').strip() or 0)
                credit = float((row.get('credit') or '0').strip() or 0)
                if debit == 0 and credit == 0:
                    continue
                if not account_exists(db, code):
                    errors.append(f"Row {i}: unknown account code '{code}'")
                    continue
                if debit and credit:
                    errors.append(f"Row {i}: account '{code}' has both a debit and a credit")
                    continue
                lines.append({'account': code, 'debit': debit, 'credit': credit,
                              'memo': 'Opening balance'})
                net += debit - credit

            if errors:
                for e in errors[:8]:
                    flash(e, 'danger')
                return redirect(request.url)
            if not lines:
                flash('No opening balances found in the file.', 'warning')
                return redirect(request.url)

            # Balance the entry: plug the difference into Accumulated Surplus,
            # unless the admin supplied that account explicitly (then require exact).
            if abs(net) > 0.01:
                if any(l['account'] == ACCUM_SURPLUS for l in lines):
                    flash(f'Opening balances are out of balance by ₦{abs(net):,.2f}. '
                          f'Adjust the figures so total debits equal total credits.', 'danger')
                    return redirect(request.url)
                if net > 0:
                    lines.append({'account': ACCUM_SURPLUS, 'credit': round(net, 2),
                                  'memo': 'Opening balancing figure'})
                else:
                    lines.append({'account': ACCUM_SURPLUS, 'debit': round(-net, 2),
                                  'memo': 'Opening balancing figure'})

            # Replace any prior opening entry so re-imports correct rather than stack.
            for e in db.execute("SELECT id FROM journal_entries WHERE source_module = 'opening'").fetchall():
                db.execute('DELETE FROM journal_lines WHERE entry_id = ?', (e['id'],))
                db.execute('DELETE FROM journal_entries WHERE id = ?', (e['id'],))

            post_journal(db, 'Opening balances', lines, date=as_of,
                         reference=f'OPENING-{as_of}', source_module='opening',
                         created_by=current_user.id)
            db.commit()
            audit(db, 'IMPORT_OPENING', 'migration',
                  f'Posted opening balances as of {as_of} ({len(lines)} lines)')
            flash(f'Opening balances posted as of {as_of}. The ledger now reflects your '
                  f'starting position.', 'success')
            return redirect(url_for('accounting.trial_balance_view'))

        except ValueError as e:
            db.rollback()
            flash(f'Could not post opening balances: {e}', 'danger')
            return redirect(request.url)
        except Exception as e:
            db.rollback()
            flash(f'File processing error: {e}', 'danger')
            return redirect(request.url)

    existing = db.execute(
        "SELECT COUNT(*) FROM journal_entries WHERE source_module = 'opening'"
    ).fetchone()[0]
    return render_template('admin/migration/opening_balances.html',
                           accounts=get_accounts(db, active_only=True),
                           today=datetime.now().strftime('%Y-%m-%d'),
                           already_set=bool(existing))


# ═══════════════════════════════════════════════════════════════════════════════
# MEMBERS
# ═══════════════════════════════════════════════════════════════════════════════

MEMBERS_COLUMNS = [
    'first_name', 'last_name', 'email', 'phone',
    'member_number', 'date_joined', 'monthly_savings',
    'address', 'occupation', 'date_of_birth', 'status',
    'nominee_name', 'nominee_relationship', 'nominee_phone',
    'bank_name', 'account_number', 'account_name',
    'emergency_contact_name', 'emergency_contact_phone',
    'exit_date', 'exit_reason', 'exit_note',
]

# Phone is optional so historical / former members (who often have no phone on
# file) can still be migrated for the archive.
MEMBERS_REQUIRED = {'first_name', 'last_name'}


@migration.route('/members', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'secretary')
def import_members():
    if request.method == 'POST':
        f = request.files.get('file')
        if not f or not f.filename:
            flash('No file selected.', 'danger')
            return redirect(request.url)
        if not f.filename.lower().endswith('.csv'):
            flash('Please upload a CSV file.', 'danger')
            return redirect(request.url)

        db = get_db()
        try:
            stream = TextIOWrapper(f.stream, encoding='utf-8-sig')
            reader = csv.DictReader(stream)
            missing_req = MEMBERS_REQUIRED - set(reader.fieldnames or [])
            if missing_req:
                flash(f'Missing required columns: {", ".join(sorted(missing_req))}', 'danger')
                return redirect(request.url)

            success, skipped, errors = 0, 0, []
            new_credentials = []  # {name, email, username, password} for auto-created accounts

            for row_num, row in enumerate(reader, start=2):
                try:
                    first_name = row.get('first_name', '').strip()
                    last_name  = row.get('last_name', '').strip()
                    phone      = row.get('phone', '').strip() or None
                    if not all([first_name, last_name]):
                        errors.append(f"Row {row_num}: first_name and last_name are required.")
                        continue

                    email = row.get('email', '').strip() or None
                    if email:
                        dup = db.execute('SELECT id FROM members WHERE email = ?', (email,)).fetchone()
                        if dup:
                            skipped += 1
                            continue  # silently skip duplicate email

                    member_number = row.get('member_number', '').strip() or None
                    if member_number:
                        dup = db.execute(
                            'SELECT id FROM members WHERE member_number = ?', (member_number,)
                        ).fetchone()
                        if dup:
                            skipped += 1
                            continue

                    date_joined_raw = row.get('date_joined', '').strip()
                    date_joined = None
                    if date_joined_raw:
                        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y'):
                            try:
                                date_joined = datetime.strptime(date_joined_raw, fmt)
                                break
                            except ValueError:
                                pass
                        if date_joined is None:
                            errors.append(f"Row {row_num}: unrecognised date_joined '{date_joined_raw}'.")
                            continue

                    monthly_savings_raw = row.get('monthly_savings', '').strip()
                    monthly_savings = float(monthly_savings_raw) if monthly_savings_raw else 5000.0

                    # Auto-generate member_number BEFORE the INSERT so we never
                    # need last_insert_rowid() (SQLite-only; breaks PostgreSQL).
                    if not member_number:
                        seq  = db.execute('SELECT COUNT(*) FROM members').fetchone()[0] + 1
                        year = (date_joined or datetime.now()).year
                        member_number = f"{member_prefix(db)}/{year}/{seq:04d}"

                    status_value = row.get('status', 'active').strip() or 'active'
                    exit_date = _parse_date(row.get('exit_date', ''))
                    exit_reason = row.get('exit_reason', '').strip() or None
                    exit_note = row.get('exit_note', '').strip() or None

                    db.execute('''
                        INSERT INTO members (
                            member_number, first_name, last_name, email, phone,
                            address, occupation, date_of_birth, date_joined, monthly_savings,
                            status, nominee_name, nominee_relationship, nominee_phone,
                            bank_name, account_number, account_name,
                            emergency_contact_name, emergency_contact_phone,
                            exit_date, exit_reason, exit_note
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        member_number,
                        first_name,
                        last_name,
                        email,
                        phone,
                        row.get('address', '').strip() or None,
                        row.get('occupation', '').strip() or None,
                        row.get('date_of_birth', '').strip() or None,
                        date_joined or datetime.now(),
                        monthly_savings,
                        status_value,
                        row.get('nominee_name', '').strip() or None,
                        row.get('nominee_relationship', '').strip() or None,
                        row.get('nominee_phone', '').strip() or None,
                        encrypt_field(row.get('bank_name', '').strip()) or None,
                        encrypt_field(row.get('account_number', '').strip()) or None,
                        encrypt_field(row.get('account_name', '').strip()) or None,
                        row.get('emergency_contact_name', '').strip() or None,
                        row.get('emergency_contact_phone', '').strip() or None,
                        exit_date, exit_reason, exit_note,
                    ))

                    # Auto-create a member portal user account if email is available.
                    # Never create login accounts for former/archived members.
                    if email and status_value != 'former':
                        existing_user = db.execute(
                            'SELECT id FROM users WHERE email = ?', (email,)
                        ).fetchone()
                        if not existing_user:
                            # Generate a cryptographically random temporary password.
                            # must_change_password=1 forces the member to set their own
                            # password on first login — the temp password is shown to the
                            # admin after import so they can communicate it to the member.
                            temp_pw   = secrets.token_urlsafe(12)
                            pw_hash   = generate_password_hash(temp_pw)
                            full_name = f"{first_name} {last_name}"
                            db.execute('''
                                INSERT INTO users
                                    (username, password_hash, role, full_name, email,
                                     is_active, must_change_password, created_at)
                                VALUES (?, ?, 'member', ?, ?, 1, 1, ?)
                            ''', (email, pw_hash, full_name, email, datetime.now()))
                            new_credentials.append({
                                'name':     full_name,
                                'email':    email,
                                'username': email,
                                'password': temp_pw,
                            })

                    success += 1
                except Exception as e:
                    errors.append(f"Row {row_num}: {e}")

            db.commit()
            audit(db, 'IMPORT_MEMBERS', 'migration',
                  f"Imported {success} members, skipped {skipped} duplicates, {len(errors)} errors")

            # Stash temp credentials in the session so the index page can display them once.
            if new_credentials:
                session['new_member_credentials'] = new_credentials

        except Exception as e:
            db.rollback()
            flash(f'File processing error: {e}', 'danger')
            return redirect(request.url)

        _flash_result(success, skipped, errors, 'member')
        return redirect(url_for('migration.index'))

    return render_template('admin/migration/import.html',
                           entity='members',
                           title='Import Members',
                           required_cols=sorted(MEMBERS_REQUIRED),
                           optional_cols=[c for c in MEMBERS_COLUMNS if c not in MEMBERS_REQUIRED],
                           template_url=url_for('migration.template_members'),
                           back_url=url_for('migration.index'))


@migration.route('/members/template')
@login_required
@role_required('admin', 'secretary')
def template_members():
    out = StringIO()
    w = csv.writer(out)
    w.writerow(MEMBERS_COLUMNS)
    w.writerow(['John', 'Doe', 'john@example.com', '08012345678',
                'MEM/2024/0001', '2020-01-15', '5000',
                'Lagos', 'Teacher', '1985-06-20', 'active',
                'Mary Doe', 'Spouse', '08099991111',
                'GTBank', '0123456789', 'John Doe',
                'James Doe', '08011112222'])
    w.writerow(['Jane', 'Smith', 'jane@example.com', '08087654321',
                '', '2021-03-01', '10000',
                'Ibadan', 'Engineer', '1990-11-05', 'active',
                '', '', '',
                'Access Bank', '9876543210', 'Jane Smith',
                '', ''])
    return _csv_response(out, 'members_import_template.csv')


# ── Members export ────────────────────────────────────────────────────────────

@migration.route('/members/export')
@login_required
@role_required('admin', 'secretary')
def export_members():
    db = get_db()
    rows = db.execute('''
        SELECT member_number, first_name, last_name, email, phone,
               address, occupation, date_of_birth, date_joined, monthly_savings,
               total_savings, status, nominee_name, nominee_relationship, nominee_phone,
               bank_name, account_number, account_name,
               emergency_contact_name, emergency_contact_phone
        FROM members ORDER BY member_number
    ''').fetchall()
    out = StringIO()
    w = csv.writer(out)
    w.writerow([
        'member_number', 'first_name', 'last_name', 'email', 'phone',
        'address', 'occupation', 'date_of_birth', 'date_joined', 'monthly_savings',
        'total_savings', 'status', 'nominee_name', 'nominee_relationship', 'nominee_phone',
        'bank_name', 'account_number', 'account_name',
        'emergency_contact_name', 'emergency_contact_phone',
    ])
    for r in rows:
        w.writerow(list(r))
    return _csv_response(out, 'members_export.csv')


# ═══════════════════════════════════════════════════════════════════════════════
# SAVINGS
# ═══════════════════════════════════════════════════════════════════════════════

SAVINGS_COLUMNS = [
    'member_number', 'email', 'amount', 'month', 'payment_type',
    'late_fee', 'payment_method', 'receipt_number', 'date', 'notes',
]
SAVINGS_REQUIRED = {'amount', 'month'}


@migration.route('/savings', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'treasurer')
def import_savings():
    if request.method == 'POST':
        f = request.files.get('file')
        if not f or not f.filename:
            flash('No file selected.', 'danger')
            return redirect(request.url)
        if not f.filename.lower().endswith('.csv'):
            flash('Please upload a CSV file.', 'danger')
            return redirect(request.url)

        db = get_db()
        try:
            stream = TextIOWrapper(f.stream, encoding='utf-8-sig')
            reader = csv.DictReader(stream)
            fieldnames = set(reader.fieldnames or [])
            if not SAVINGS_REQUIRED.issubset(fieldnames):
                flash(f'Missing columns: {", ".join(sorted(SAVINGS_REQUIRED - fieldnames))}', 'danger')
                return redirect(request.url)
            if 'member_number' not in fieldnames and 'email' not in fieldnames:
                flash('CSV must have at least one of: member_number, email', 'danger')
                return redirect(request.url)

            success, skipped, errors = 0, 0, []

            for row_num, row in enumerate(reader, start=2):
                try:
                    with _row_savepoint(db):
                        member = _resolve_member(db, row)
                        if not member:
                            raise ValueError(
                                f"member not found (member_number="
                                f"{row.get('member_number','')!r}, email={row.get('email','')!r})")

                        amount_raw = row.get('amount', '').strip()
                        if not amount_raw:
                            raise ValueError("amount is required.")
                        amount = float(amount_raw)

                        month = row.get('month', '').strip()
                        if not month:
                            raise ValueError("month is required (format YYYY-MM).")

                        # Multiple savings per month are allowed (salary + personal etc.)
                        payment_type   = row.get('payment_type', 'monthly').strip() or 'monthly'
                        late_fee       = float(row.get('late_fee', '0').strip() or 0)
                        payment_method = row.get('payment_method', 'cash').strip() or 'cash'
                        given_receipt  = row.get('receipt_number', '').strip()
                        receipt_number = given_receipt or _ref('RCPT')
                        # Idempotent: skip a receipt we already have so re-running
                        # the same file doesn't error or double-count.
                        if given_receipt and db.execute(
                            'SELECT id FROM savings WHERE receipt_number = ?', (receipt_number,)
                        ).fetchone():
                            raise _SkipRow()
                        notes          = row.get('notes', '').strip() or None

                        date_raw = row.get('date', '').strip()
                        date = _parse_date(date_raw) or datetime.now()

                        # The CSV amount is the gross contribution, exactly as
                        # typed on the savings form, so a bulk import must carve
                        # out the same share-capital portion a manual entry does.
                        # Without this the share split silently comes out zero and
                        # members' balances look short of what they contributed.
                        deposit_amount, share_amount = share_capital_split(db, amount)

                        db.execute('''
                            INSERT INTO savings
                                (member_id, amount, share_capital, month, payment_type, late_fee,
                                 payment_method, receipt_number, notes, date)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (member['id'], deposit_amount, share_amount, month, payment_type,
                              late_fee, payment_method, receipt_number, notes, date))

                        db.execute(
                            'UPDATE members SET total_savings = total_savings + ?, '
                            'shares_value = COALESCE(shares_value, 0) + ? WHERE id = ?',
                            (deposit_amount, share_amount, member['id'])
                        )
                    success += 1
                except _SkipRow:
                    skipped += 1
                except Exception as e:
                    errors.append(f"Row {row_num}: {e}")

            db.commit()
            audit(db, 'IMPORT_SAVINGS', 'migration',
                  f"Imported {success} savings records, {skipped} skipped, {len(errors)} errors")

        except Exception as e:
            db.rollback()
            flash(f'File processing error: {e}', 'danger')
            return redirect(request.url)

        _flash_result(success, skipped, errors, 'savings record')
        return redirect(url_for('migration.index'))

    return render_template('admin/migration/import.html',
                           entity='savings',
                           title='Import Savings History',
                           required_cols=sorted(SAVINGS_REQUIRED) + ['member_number or email'],
                           optional_cols=[c for c in SAVINGS_COLUMNS
                                          if c not in SAVINGS_REQUIRED
                                          and c not in ('member_number', 'email')],
                           template_url=url_for('migration.template_savings'),
                           back_url=url_for('migration.index'))


@migration.route('/savings/template')
@login_required
@role_required('admin', 'treasurer')
def template_savings():
    out = StringIO()
    w = csv.writer(out)
    w.writerow(SAVINGS_COLUMNS)
    w.writerow(['MEM/2024/0001', 'john@example.com', '5000', '2024-01', 'monthly',
                '0', 'salary_deduction', 'RCPT/20240115/0001', '2024-01-10', ''])
    w.writerow(['MEM/2024/0001', 'john@example.com', '2000', '2024-01', 'personal',
                '0', 'cash', 'RCPT/20240115/0002', '2024-01-15', 'Personal top-up'])
    w.writerow(['MEM/2024/0002', 'jane@example.com', '10000', '2024-01', 'monthly',
                '1000', 'transfer', 'RCPT/20240118/0003', '2024-01-18', 'Late payment'])
    return _csv_response(out, 'savings_import_template.csv')


@migration.route('/savings/export')
@login_required
@role_required('admin', 'treasurer')
def export_savings():
    db = get_db()
    rows = db.execute('''
        SELECT m.member_number, m.email, s.amount, s.share_capital, s.month, s.late_fee,
               s.payment_method, s.receipt_number, s.date, s.notes
        FROM savings s JOIN members m ON s.member_id = m.id
        ORDER BY m.member_number, s.month
    ''').fetchall()
    out = StringIO()
    w = csv.writer(out)
    w.writerow(['member_number', 'email', 'amount', 'month', 'late_fee',
                'payment_method', 'receipt_number', 'date', 'notes'])
    for r in rows:
        # Export the gross contribution (deposit + share capital), which is what
        # the importer expects and re-splits. Exporting the net deposit would
        # shrink balances a little more on every export/re-import round trip.
        gross = round(float(r['amount'] or 0) + float(r['share_capital'] or 0), 2)
        w.writerow([r['member_number'], r['email'], gross, r['month'], r['late_fee'],
                    r['payment_method'], r['receipt_number'], r['date'], r['notes']])
    return _csv_response(out, 'savings_export.csv')


# ═══════════════════════════════════════════════════════════════════════════════
# LOANS
# ═══════════════════════════════════════════════════════════════════════════════

LOANS_COLUMNS = [
    'member_number', 'email', 'loan_number', 'amount', 'purpose',
    'tenure', 'interest_rate', 'total_repayment', 'balance', 'status',
    'date_applied', 'date_approved', 'disbursement_date', 'disbursed_amount', 'notes',
]
LOANS_REQUIRED = {'amount', 'purpose'}


@migration.route('/loans', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'treasurer')
def import_loans():
    if request.method == 'POST':
        f = request.files.get('file')
        if not f or not f.filename:
            flash('No file selected.', 'danger')
            return redirect(request.url)
        if not f.filename.lower().endswith('.csv'):
            flash('Please upload a CSV file.', 'danger')
            return redirect(request.url)

        db = get_db()
        try:
            stream = TextIOWrapper(f.stream, encoding='utf-8-sig')
            reader = csv.DictReader(stream)
            fieldnames = set(reader.fieldnames or [])
            if not LOANS_REQUIRED.issubset(fieldnames):
                flash(f'Missing columns: {", ".join(sorted(LOANS_REQUIRED - fieldnames))}', 'danger')
                return redirect(request.url)
            if 'member_number' not in fieldnames and 'email' not in fieldnames:
                flash('CSV must have at least one of: member_number, email', 'danger')
                return redirect(request.url)

            success, skipped, errors = 0, 0, []

            for row_num, row in enumerate(reader, start=2):
                try:
                    with _row_savepoint(db):
                        member = _resolve_member(db, row)
                        if not member:
                            raise ValueError("member not found.")

                        loan_number = row.get('loan_number', '').strip() or _ref('LOAN')
                        dup = db.execute(
                            'SELECT id FROM loans WHERE loan_number = ?', (loan_number,)
                        ).fetchone()
                        if dup:
                            raise _SkipRow()

                        amount = float(row.get('amount', 0))
                        if amount <= 0:
                            raise ValueError("amount must be > 0.")

                        purpose    = _normalize_purpose(row.get('purpose', ''))
                        tenure     = int(row.get('tenure', '12').strip() or 12)
                        int_rate   = float(row.get('interest_rate', '11').strip() or 11)
                        total_rep  = float(row.get('total_repayment', '0').strip() or 0) or amount
                        # A blank balance defaults to the full amount owed, but an
                        # explicit 0 must stick (e.g. migrating a fully-repaid loan).
                        bal_raw    = row.get('balance', '').strip()
                        balance    = float(bal_raw) if bal_raw else total_rep
                        status     = _normalize_loan_status(row.get('status', ''), fallback='pending')
                        notes      = row.get('notes', '').strip() or None

                        date_applied      = _parse_date(row.get('date_applied', '')) or datetime.now()
                        date_approved     = _parse_date(row.get('date_approved', ''))
                        disbursement_date = _parse_date(row.get('disbursement_date', ''))
                        disbursed_raw     = row.get('disbursed_amount', '').strip()
                        disbursed_amount  = float(disbursed_raw) if disbursed_raw else None

                        # If disbursement_date given but no disbursed_amount, default to loan amount
                        if disbursement_date and disbursed_amount is None:
                            disbursed_amount = amount

                        db.execute('''
                            INSERT INTO loans
                                (loan_number, member_id, amount, purpose, tenure, interest_rate,
                                 total_repayment, balance, status, notes, date_applied, approved_at,
                                 disbursement_date, disbursed_amount)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (loan_number, member['id'], amount, purpose, tenure, int_rate,
                              total_rep, balance, status, notes, date_applied, date_approved,
                              disbursement_date, disbursed_amount))
                    success += 1
                except _SkipRow:
                    skipped += 1
                except Exception as e:
                    errors.append(f"Row {row_num}: {e}")

            db.commit()
            audit(db, 'IMPORT_LOANS', 'migration',
                  f"Imported {success} loans, skipped {skipped}, {len(errors)} errors")

        except Exception as e:
            db.rollback()
            flash(f'File processing error: {e}', 'danger')
            return redirect(request.url)

        _flash_result(success, skipped, errors, 'loan')
        return redirect(url_for('migration.index'))

    return render_template('admin/migration/import.html',
                           entity='loans',
                           title='Import Loan Records',
                           required_cols=sorted(LOANS_REQUIRED) + ['member_number or email'],
                           optional_cols=[c for c in LOANS_COLUMNS
                                          if c not in LOANS_REQUIRED
                                          and c not in ('member_number', 'email')],
                           template_url=url_for('migration.template_loans'),
                           back_url=url_for('migration.index'))


@migration.route('/loans/template')
@login_required
@role_required('admin', 'treasurer')
def template_loans():
    out = StringIO()
    w = csv.writer(out)
    w.writerow(LOANS_COLUMNS)
    # Active loan — already disbursed, partially repaid
    w.writerow(['MEM/2024/0001', 'john@example.com', 'LOAN/20240201/0001',
                '200000', 'Regular', '12', '11', '224000', '100000',
                'active', '2024-02-01', '2024-02-15', '2024-02-20', '200000', ''])
    # Pending loan — not yet disbursed
    w.writerow(['MEM/2024/0002', 'jane@example.com', '',
                '100000', 'Emergency', '6', '10', '105000', '105000',
                'pending', '2024-03-10', '', '', '', 'Awaiting guarantor'])
    # Completed loan — fully repaid
    w.writerow(['MEM/2024/0003', '', 'LOAN/20230601/0042',
                '50000', 'School Fees', '6', '9', '52250', '0',
                'completed', '2023-06-01', '2023-06-05', '2023-06-10', '50000', ''])
    return _csv_response(out, 'loans_import_template.csv')


@migration.route('/loans/export')
@login_required
@role_required('admin', 'treasurer')
def export_loans():
    db = get_db()
    rows = db.execute('''
        SELECT m.member_number, m.email, l.loan_number, l.amount, l.purpose,
               l.tenure, l.interest_rate, l.total_repayment, l.balance, l.status,
               l.date_applied, l.approved_at, l.disbursement_date, l.disbursed_amount, l.notes
        FROM loans l JOIN members m ON l.member_id = m.id
        ORDER BY l.date_applied DESC
    ''').fetchall()
    out = StringIO()
    w = csv.writer(out)
    w.writerow(['member_number', 'email', 'loan_number', 'amount', 'purpose',
                'tenure', 'interest_rate', 'total_repayment', 'balance', 'status',
                'date_applied', 'date_approved', 'disbursement_date', 'disbursed_amount', 'notes'])
    for r in rows:
        w.writerow(list(r))
    return _csv_response(out, 'loans_export.csv')


# ═══════════════════════════════════════════════════════════════════════════════
# REPAYMENTS
# ═══════════════════════════════════════════════════════════════════════════════

REPAYMENTS_COLUMNS = [
    'loan_number', 'member_number', 'email',
    'amount', 'principal_paid', 'interest_paid', 'penalty_paid',
    'payment_method', 'receipt_number', 'date', 'notes',
]
REPAYMENTS_REQUIRED = {'amount', 'date'}


@migration.route('/repayments', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'treasurer')
def import_repayments():
    if request.method == 'POST':
        f = request.files.get('file')
        if not f or not f.filename:
            flash('No file selected.', 'danger')
            return redirect(request.url)
        if not f.filename.lower().endswith('.csv'):
            flash('Please upload a CSV file.', 'danger')
            return redirect(request.url)

        db = get_db()
        try:
            stream = TextIOWrapper(f.stream, encoding='utf-8-sig')
            reader = csv.DictReader(stream)
            fieldnames = set(reader.fieldnames or [])
            if not REPAYMENTS_REQUIRED.issubset(fieldnames):
                flash(f'Missing columns: {", ".join(sorted(REPAYMENTS_REQUIRED - fieldnames))}',
                      'danger')
                return redirect(request.url)
            has_loan_ref = 'loan_number' in fieldnames
            has_member_ref = 'member_number' in fieldnames or 'email' in fieldnames
            if not has_loan_ref and not has_member_ref:
                flash('CSV must have at least one of: loan_number, member_number, email', 'danger')
                return redirect(request.url)

            success, skipped, errors = 0, 0, []

            for row_num, row in enumerate(reader, start=2):
                try:
                    # ── Resolve loan ──────────────────────────────────────────
                    loan = None
                    loan_number = row.get('loan_number', '').strip()
                    if loan_number:
                        loan = db.execute(
                            'SELECT * FROM loans WHERE loan_number = ?', (loan_number,)
                        ).fetchone()
                        if not loan:
                            errors.append(f"Row {row_num}: loan_number '{loan_number}' not found.")
                            continue
                    else:
                        # Fall back to member's most recent active/approved loan
                        member = _resolve_member(db, row)
                        if not member:
                            errors.append(f"Row {row_num}: member not found and no loan_number given.")
                            continue
                        loan = db.execute(
                            '''SELECT * FROM loans
                               WHERE member_id = ? AND status IN ('active','approved')
                               ORDER BY date_applied DESC LIMIT 1''',
                            (member['id'],)
                        ).fetchone()
                        if not loan:
                            errors.append(
                                f"Row {row_num}: no active/approved loan for member "
                                f"'{row.get('member_number','') or row.get('email','')}'. "
                                f"Provide loan_number explicitly."
                            )
                            continue

                    # ── Parse amounts ─────────────────────────────────────────
                    amount_raw = row.get('amount', '').strip()
                    if not amount_raw:
                        errors.append(f"Row {row_num}: amount is required.")
                        continue
                    amount = float(amount_raw)
                    if amount <= 0:
                        errors.append(f"Row {row_num}: amount must be > 0.")
                        continue

                    principal_raw  = row.get('principal_paid', '').strip()
                    interest_raw   = row.get('interest_paid', '').strip()
                    penalty_raw    = row.get('penalty_paid', '').strip()

                    interest_paid  = float(interest_raw)  if interest_raw  else 0.0
                    penalty_paid   = float(penalty_raw)   if penalty_raw   else 0.0
                    # If principal not given, derive it: amount − interest − penalty
                    principal_paid = (float(principal_raw) if principal_raw
                                      else max(0.0, amount - interest_paid - penalty_paid))

                    payment_method = row.get('payment_method', 'cash').strip() or 'cash'
                    receipt_number = row.get('receipt_number', '').strip() or _ref('RCPT')
                    notes          = row.get('notes', '').strip() or None
                    date           = _parse_date(row.get('date', ''))
                    if not date:
                        errors.append(f"Row {row_num}: unrecognised or missing date.")
                        continue

                    repayment_number = _ref('REP')

                    db.execute('''
                        INSERT INTO repayments
                            (repayment_number, loan_id, amount, principal_paid,
                             interest_paid, penalty_paid, payment_method,
                             receipt_number, notes, date)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (repayment_number, loan['id'], amount, principal_paid,
                          interest_paid, penalty_paid, payment_method,
                          receipt_number, notes, date))

                    # ── Update loan balance ───────────────────────────────────
                    new_balance = max(0.0, (loan['balance'] or 0) - principal_paid)
                    if new_balance <= 0:
                        db.execute(
                            '''UPDATE loans
                               SET balance = 0, status = 'completed', completed_at = ?
                               WHERE id = ?''',
                            (datetime.now(), loan['id'])
                        )
                    else:
                        db.execute('UPDATE loans SET balance = ? WHERE id = ?',
                                   (new_balance, loan['id']))

                    # Refresh loan row for subsequent repayments in same file
                    loan = db.execute('SELECT * FROM loans WHERE id = ?',
                                      (loan['id'],)).fetchone()
                    success += 1

                except Exception as e:
                    errors.append(f"Row {row_num}: {e}")

            db.commit()
            audit(db, 'IMPORT_REPAYMENTS', 'migration',
                  f"Imported {success} repayments, skipped {skipped}, {len(errors)} errors")

        except Exception as e:
            db.rollback()
            flash(f'File processing error: {e}', 'danger')
            return redirect(request.url)

        _flash_result(success, skipped, errors, 'repayment')
        return redirect(url_for('migration.index'))

    return render_template('admin/migration/import.html',
                           entity='repayments',
                           title='Import Loan Repayments',
                           required_cols=sorted(REPAYMENTS_REQUIRED) + ['loan_number (or member_number/email)'],
                           optional_cols=[c for c in REPAYMENTS_COLUMNS
                                          if c not in REPAYMENTS_REQUIRED
                                          and c not in ('loan_number', 'member_number', 'email')],
                           template_url=url_for('migration.template_repayments'),
                           back_url=url_for('migration.index'))


@migration.route('/repayments/template')
@login_required
@role_required('admin', 'treasurer')
def template_repayments():
    out = StringIO()
    w = csv.writer(out)
    w.writerow(REPAYMENTS_COLUMNS)
    # Full repayment breakdown known
    w.writerow(['LOAN/20240201/0001', 'MEM/2024/0001', 'john@example.com',
                '18667', '15000', '3667', '0',
                'salary_deduction', 'RCPT/20240310/0001', '2024-03-10', ''])
    # Only amount known — principal will be derived as amount − interest − penalty
    w.writerow(['LOAN/20240201/0001', '', '',
                '18667', '', '', '',
                'transfer', '', '2024-04-10', 'April instalment'])
    # Resolved by member (latest active loan used)
    w.writerow(['', 'MEM/2024/0002', 'jane@example.com',
                '17500', '14000', '3500', '0',
                'cash', 'RCPT/20240315/0003', '2024-03-15', ''])
    return _csv_response(out, 'repayments_import_template.csv')


@migration.route('/repayments/export')
@login_required
@role_required('admin', 'treasurer')
def export_repayments():
    db = get_db()
    rows = db.execute('''
        SELECT l.loan_number, m.member_number, m.email,
               r.amount, r.principal_paid, r.interest_paid, r.penalty_paid,
               r.payment_method, r.receipt_number, r.date, r.notes
        FROM repayments r
        JOIN loans l ON r.loan_id = l.id
        JOIN members m ON l.member_id = m.id
        ORDER BY r.date DESC
    ''').fetchall()
    out = StringIO()
    w = csv.writer(out)
    w.writerow(['loan_number', 'member_number', 'email',
                'amount', 'principal_paid', 'interest_paid', 'penalty_paid',
                'payment_method', 'receipt_number', 'date', 'notes'])
    for r in rows:
        w.writerow(list(r))
    return _csv_response(out, 'repayments_export.csv')


# ═══════════════════════════════════════════════════════════════════════════════
# EXPENSES
# ═══════════════════════════════════════════════════════════════════════════════

EXPENSES_COLUMNS = ['category', 'amount', 'description', 'vendor',
                    'payment_method', 'date', 'notes']
EXPENSES_REQUIRED = {'category', 'amount'}


@migration.route('/expenses', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'treasurer')
def import_expenses():
    if request.method == 'POST':
        f = request.files.get('file')
        if not f or not f.filename:
            flash('No file selected.', 'danger')
            return redirect(request.url)
        if not f.filename.lower().endswith('.csv'):
            flash('Please upload a CSV file.', 'danger')
            return redirect(request.url)

        db = get_db()
        try:
            stream = TextIOWrapper(f.stream, encoding='utf-8-sig')
            reader = csv.DictReader(stream)
            missing = EXPENSES_REQUIRED - set(reader.fieldnames or [])
            if missing:
                flash(f'Missing columns: {", ".join(sorted(missing))}', 'danger')
                return redirect(request.url)

            success, errors = 0, []

            for row_num, row in enumerate(reader, start=2):
                try:
                    category = row.get('category', '').strip()
                    amount_raw = row.get('amount', '').strip()
                    if not category or not amount_raw:
                        errors.append(f"Row {row_num}: category and amount are required.")
                        continue
                    amount = float(amount_raw)
                    date = _parse_date(row.get('date', '')) or datetime.now()
                    expense_number = _ref('EXP')
                    db.execute('''
                        INSERT INTO expenses
                            (expense_number, category, amount, description, vendor,
                             payment_method, date, notes)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (expense_number, category, amount,
                          row.get('description', '').strip() or None,
                          row.get('vendor', '').strip() or None,
                          row.get('payment_method', 'cash').strip() or 'cash',
                          date,
                          row.get('notes', '').strip() or None))
                    success += 1
                except Exception as e:
                    errors.append(f"Row {row_num}: {e}")

            db.commit()
            audit(db, 'IMPORT_EXPENSES', 'migration',
                  f"Imported {success} expenses, {len(errors)} errors")

        except Exception as e:
            db.rollback()
            flash(f'File processing error: {e}', 'danger')
            return redirect(request.url)

        _flash_result(success, 0, errors, 'expense')
        return redirect(url_for('migration.index'))

    return render_template('admin/migration/import.html',
                           entity='expenses',
                           title='Import Expenses',
                           required_cols=sorted(EXPENSES_REQUIRED),
                           optional_cols=[c for c in EXPENSES_COLUMNS if c not in EXPENSES_REQUIRED],
                           template_url=url_for('migration.template_expenses'),
                           back_url=url_for('migration.index'))


@migration.route('/expenses/template')
@login_required
@role_required('admin', 'treasurer')
def template_expenses():
    out = StringIO()
    w = csv.writer(out)
    w.writerow(EXPENSES_COLUMNS)
    w.writerow(['Stationery', '5000', 'Office supplies', 'Balogun Market',
                'cash', '2024-01-10', ''])
    w.writerow(['Utilities', '15000', 'January electricity bill', 'IBEDC',
                'transfer', '2024-01-20', 'Account: 9876543'])
    return _csv_response(out, 'expenses_import_template.csv')


@migration.route('/expenses/export')
@login_required
@role_required('admin', 'treasurer')
def export_expenses():
    db = get_db()
    rows = db.execute('''
        SELECT expense_number, category, amount, description, vendor,
               payment_method, date, notes
        FROM expenses ORDER BY date DESC
    ''').fetchall()
    out = StringIO()
    w = csv.writer(out)
    w.writerow(['expense_number', 'category', 'amount', 'description',
                'vendor', 'payment_method', 'date', 'notes'])
    for r in rows:
        w.writerow(list(r))
    return _csv_response(out, 'expenses_export.csv')


# ═══════════════════════════════════════════════════════════════════════════════
# REVENUE
# ═══════════════════════════════════════════════════════════════════════════════

REVENUE_COLUMNS = ['category', 'amount', 'description', 'source', 'date', 'notes']
REVENUE_REQUIRED = {'category', 'amount'}


@migration.route('/revenue', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'treasurer')
def import_revenue():
    if request.method == 'POST':
        f = request.files.get('file')
        if not f or not f.filename:
            flash('No file selected.', 'danger')
            return redirect(request.url)
        if not f.filename.lower().endswith('.csv'):
            flash('Please upload a CSV file.', 'danger')
            return redirect(request.url)

        db = get_db()
        try:
            stream = TextIOWrapper(f.stream, encoding='utf-8-sig')
            reader = csv.DictReader(stream)
            missing = REVENUE_REQUIRED - set(reader.fieldnames or [])
            if missing:
                flash(f'Missing columns: {", ".join(sorted(missing))}', 'danger')
                return redirect(request.url)

            success, errors = 0, []
            for row_num, row in enumerate(reader, start=2):
                try:
                    category = row.get('category', '').strip()
                    amount_raw = row.get('amount', '').strip()
                    if not category or not amount_raw:
                        errors.append(f"Row {row_num}: category and amount are required.")
                        continue
                    date = _parse_date(row.get('date', '')) or datetime.now()
                    db.execute('''
                        INSERT INTO revenue
                            (revenue_number, category, amount, description, source, date, notes)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (_ref('REV'), category, float(amount_raw),
                          row.get('description', '').strip() or None,
                          row.get('source', '').strip() or None,
                          date,
                          row.get('notes', '').strip() or None))
                    success += 1
                except Exception as e:
                    errors.append(f"Row {row_num}: {e}")

            db.commit()
            audit(db, 'IMPORT_REVENUE', 'migration',
                  f"Imported {success} revenue records, {len(errors)} errors")

        except Exception as e:
            db.rollback()
            flash(f'File processing error: {e}', 'danger')
            return redirect(request.url)

        _flash_result(success, 0, errors, 'revenue record')
        return redirect(url_for('migration.index'))

    return render_template('admin/migration/import.html',
                           entity='revenue',
                           title='Import Revenue Records',
                           required_cols=sorted(REVENUE_REQUIRED),
                           optional_cols=[c for c in REVENUE_COLUMNS if c not in REVENUE_REQUIRED],
                           template_url=url_for('migration.template_revenue'),
                           back_url=url_for('migration.index'))


@migration.route('/revenue/template')
@login_required
@role_required('admin', 'treasurer')
def template_revenue():
    out = StringIO()
    w = csv.writer(out)
    w.writerow(REVENUE_COLUMNS)
    w.writerow(['Entrance Fee', '2000', 'New member entrance fee', 'Member payments',
                '2024-01-05', ''])
    w.writerow(['Loan Interest', '45000', 'Q1 loan interest income', 'Active loans',
                '2024-03-31', 'Aggregated for quarter'])
    return _csv_response(out, 'revenue_import_template.csv')


@migration.route('/revenue/export')
@login_required
@role_required('admin', 'treasurer')
def export_revenue():
    db = get_db()
    rows = db.execute(
        'SELECT revenue_number, category, amount, description, source, date, notes '
        'FROM revenue ORDER BY date DESC'
    ).fetchall()
    out = StringIO()
    w = csv.writer(out)
    w.writerow(['revenue_number', 'category', 'amount', 'description', 'source', 'date', 'notes'])
    for r in rows:
        w.writerow(list(r))
    return _csv_response(out, 'revenue_export.csv')


# ═══════════════════════════════════════════════════════════════════════════════
# INVESTMENTS
# ═══════════════════════════════════════════════════════════════════════════════

INVESTMENTS_COLUMNS = [
    'name', 'type', 'amount', 'institution', 'interest_rate',
    'risk_level', 'start_date', 'maturity_date', 'description', 'notes',
]
INVESTMENTS_REQUIRED = {'name', 'type', 'amount'}


@migration.route('/investments', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'treasurer')
def import_investments():
    if request.method == 'POST':
        f = request.files.get('file')
        if not f or not f.filename:
            flash('No file selected.', 'danger')
            return redirect(request.url)
        if not f.filename.lower().endswith('.csv'):
            flash('Please upload a CSV file.', 'danger')
            return redirect(request.url)

        db = get_db()
        try:
            stream = TextIOWrapper(f.stream, encoding='utf-8-sig')
            reader = csv.DictReader(stream)
            missing = INVESTMENTS_REQUIRED - set(reader.fieldnames or [])
            if missing:
                flash(f'Missing columns: {", ".join(sorted(missing))}', 'danger')
                return redirect(request.url)

            success, errors = 0, []
            for row_num, row in enumerate(reader, start=2):
                try:
                    name = row.get('name', '').strip()
                    inv_type = row.get('type', '').strip()
                    amount_raw = row.get('amount', '').strip()
                    if not name or not inv_type or not amount_raw:
                        errors.append(f"Row {row_num}: name, type, amount are required.")
                        continue

                    int_rate_raw = row.get('interest_rate', '').strip()
                    int_rate = float(int_rate_raw) if int_rate_raw else None
                    start_date = _parse_date(row.get('start_date', ''))
                    maturity_date = _parse_date(row.get('maturity_date', ''))

                    db.execute('''
                        INSERT INTO investments
                            (investment_number, name, amount, type, institution,
                             interest_rate, risk_level, start_date, maturity_date,
                             description, notes, approval_status, date)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (_ref('INV'), name, float(amount_raw), inv_type,
                          row.get('institution', '').strip() or None,
                          int_rate,
                          row.get('risk_level', 'medium').strip() or 'medium',
                          start_date, maturity_date,
                          row.get('description', '').strip() or None,
                          row.get('notes', '').strip() or None,
                          'approved', datetime.now()))
                    success += 1
                except Exception as e:
                    errors.append(f"Row {row_num}: {e}")

            db.commit()
            audit(db, 'IMPORT_INVESTMENTS', 'migration',
                  f"Imported {success} investments, {len(errors)} errors")

        except Exception as e:
            db.rollback()
            flash(f'File processing error: {e}', 'danger')
            return redirect(request.url)

        _flash_result(success, 0, errors, 'investment')
        return redirect(url_for('migration.index'))

    return render_template('admin/migration/import.html',
                           entity='investments',
                           title='Import Investments',
                           required_cols=sorted(INVESTMENTS_REQUIRED),
                           optional_cols=[c for c in INVESTMENTS_COLUMNS
                                          if c not in INVESTMENTS_REQUIRED],
                           template_url=url_for('migration.template_investments'),
                           back_url=url_for('migration.index'))


@migration.route('/investments/template')
@login_required
@role_required('admin', 'treasurer')
def template_investments():
    out = StringIO()
    w = csv.writer(out)
    w.writerow(INVESTMENTS_COLUMNS)
    w.writerow(['GTBank Fixed Deposit', 'Fixed Deposit', '500000', 'GTBank',
                '9', 'low', '2024-01-01', '2024-07-01',
                '6-month FD at 9%', ''])
    w.writerow(['LASG Bond Series 3', 'Government Bond', '250000', 'Lagos State',
                '11.5', 'low', '2023-06-01', '2025-06-01',
                '2-year bond', 'Held in custody at Stanbic'])
    return _csv_response(out, 'investments_import_template.csv')


@migration.route('/investments/export')
@login_required
@role_required('admin', 'treasurer')
def export_investments():
    db = get_db()
    rows = db.execute('''
        SELECT investment_number, name, type, amount, institution, interest_rate,
               risk_level, start_date, maturity_date, description, approval_status, date
        FROM investments ORDER BY date DESC
    ''').fetchall()
    out = StringIO()
    w = csv.writer(out)
    w.writerow(['investment_number', 'name', 'type', 'amount', 'institution',
                'interest_rate', 'risk_level', 'start_date', 'maturity_date',
                'description', 'approval_status', 'date'])
    for r in rows:
        w.writerow(list(r))
    return _csv_response(out, 'investments_export.csv')


# ═══════════════════════════════════════════════════════════════════════════════
# HONORARIUM
# ═══════════════════════════════════════════════════════════════════════════════

HONORARIUM_COLUMNS = ['recipient_name', 'member_number', 'email',
                      'amount', 'description', 'month', 'date']
HONORARIUM_REQUIRED = {'recipient_name', 'amount', 'month'}


@migration.route('/honorarium', methods=['GET', 'POST'])
@login_required
@role_required('admin')
def import_honorarium():
    if request.method == 'POST':
        f = request.files.get('file')
        if not f or not f.filename:
            flash('No file selected.', 'danger')
            return redirect(request.url)
        if not f.filename.lower().endswith('.csv'):
            flash('Please upload a CSV file.', 'danger')
            return redirect(request.url)

        db = get_db()
        try:
            stream = TextIOWrapper(f.stream, encoding='utf-8-sig')
            reader = csv.DictReader(stream)
            missing = HONORARIUM_REQUIRED - set(reader.fieldnames or [])
            if missing:
                flash(f'Missing columns: {", ".join(sorted(missing))}', 'danger')
                return redirect(request.url)

            success, errors = 0, []
            for row_num, row in enumerate(reader, start=2):
                try:
                    recipient_name = row.get('recipient_name', '').strip()
                    amount_raw = row.get('amount', '').strip()
                    month = row.get('month', '').strip()
                    if not recipient_name or not amount_raw or not month:
                        errors.append(f"Row {row_num}: recipient_name, amount, month required.")
                        continue

                    member = _resolve_member(db, row)
                    recipient_id = member['id'] if member else None
                    date = _parse_date(row.get('date', '')) or datetime.now()

                    db.execute('''
                        INSERT INTO honorarium
                            (recipient_id, recipient_name, amount, description, month, date)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (recipient_id, recipient_name, float(amount_raw),
                          row.get('description', '').strip() or None,
                          month, date))
                    success += 1
                except Exception as e:
                    errors.append(f"Row {row_num}: {e}")

            db.commit()
            audit(db, 'IMPORT_HONORARIUM', 'migration',
                  f"Imported {success} honorarium records, {len(errors)} errors")

        except Exception as e:
            db.rollback()
            flash(f'File processing error: {e}', 'danger')
            return redirect(request.url)

        _flash_result(success, 0, errors, 'honorarium record')
        return redirect(url_for('migration.index'))

    return render_template('admin/migration/import.html',
                           entity='honorarium',
                           title='Import Honorarium Records',
                           required_cols=sorted(HONORARIUM_REQUIRED),
                           optional_cols=[c for c in HONORARIUM_COLUMNS
                                          if c not in HONORARIUM_REQUIRED],
                           template_url=url_for('migration.template_honorarium'),
                           back_url=url_for('migration.index'))


@migration.route('/honorarium/template')
@login_required
@role_required('admin')
def template_honorarium():
    out = StringIO()
    w = csv.writer(out)
    w.writerow(HONORARIUM_COLUMNS)
    w.writerow(['Ade Afolabi', 'MEM/2024/0001', 'ade@example.com',
                '15000', 'Executive allowance', '2024-01', '2024-01-31'])
    w.writerow(['Bola Tinubu', '', 'bola@example.com',
                '10000', 'Secretary allowance', '2024-01', '2024-01-31'])
    return _csv_response(out, 'honorarium_import_template.csv')


@migration.route('/honorarium/export')
@login_required
@role_required('admin')
def export_honorarium():
    db = get_db()
    rows = db.execute('''
        SELECT h.recipient_name, m.member_number, m.email,
               h.amount, h.description, h.month, h.date
        FROM honorarium h
        LEFT JOIN members m ON h.recipient_id = m.id
        ORDER BY h.date DESC
    ''').fetchall()
    out = StringIO()
    w = csv.writer(out)
    w.writerow(['recipient_name', 'member_number', 'email',
                'amount', 'description', 'month', 'date'])
    for r in rows:
        w.writerow(list(r))
    return _csv_response(out, 'honorarium_export.csv')


# ═══════════════════════════════════════════════════════════════════════════════
# PURGE
# ═══════════════════════════════════════════════════════════════════════════════

_PURGEABLE_TABLES = [
    # general ledger + dividends (children before parents for FK safety)
    'dividend_allocations', 'dividend_declarations',
    'journal_lines', 'journal_entries',
    # Loan children before loans, or Postgres rejects the delete on the FK.
    'repayments', 'loan_request_events', 'loan_approvals', 'loan_guarantors',
    'loans', 'savings',
    'honorarium', 'expenses', 'revenue', 'investments',
    'notifications', 'audit_log', 'members',
]

@migration.route('/load-demo', methods=['POST'])
@login_required
@role_required('admin')
def load_demo():
    """Load a coherent demo dataset and post it to the ledger (for evaluation)."""
    from demo_data import load_demo_data
    db = get_db()
    try:
        result = load_demo_data(db, created_by=current_user.id)
        if result.get('skipped'):
            db.rollback()
            flash('Demo data is already loaded. Purge first if you want to reload it.', 'info')
        else:
            db.commit()
            audit(db, 'LOAD_DEMO', 'migration',
                  f"Loaded demo data: {result['members']} members, "
                  f"{result['journal_entries']} journal entries")
            flash(f"Demo data loaded — {result['members']} members, {result['loans']} loans, "
                  f"and {result['journal_entries']} ledger entries posted. Explore Accounting, "
                  f"Reports and Dividends, then purge when finished.", 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error loading demo data: {e}', 'danger')
    return redirect(url_for('migration.index'))


@migration.route('/purge', methods=['POST'])
@login_required
@role_required('admin')
def purge_database():
    confirm = request.form.get('confirm_phrase', '').strip()
    if confirm != 'PURGE ALL DATA':
        flash('Confirmation phrase did not match. Purge cancelled.', 'danger')
        return redirect(url_for('migration.index'))

    db = get_db()
    try:
        for table in _PURGEABLE_TABLES:
            db.execute(f'DELETE FROM {table}')
        # Remove member portal logins created during import; keep staff accounts.
        db.execute("DELETE FROM users WHERE role = 'member'")
        db.commit()
        flash('All transactional data (including the ledger and dividends) has been '
              'purged. Staff users and settings were kept.', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Purge failed: {str(e)}', 'danger')

    return redirect(url_for('migration.index'))


# ── Shared utilities ──────────────────────────────────────────────────────────

def _parse_date(raw):
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass
    return None


def _csv_response(out, filename):
    response = make_response(out.getvalue())
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    response.headers['Content-Disposition'] = f'attachment; filename={filename}'
    return response


def _flash_result(success, skipped, errors, entity_name):
    db = get_db()
    record_upload(db, f'migration_{entity_name}', success, errors, skipped=skipped)
    db.commit()
    flash('Full results saved in Upload History.', 'info')
    if errors:
        flash(f'Imported {success} {entity_name}(s). '
              f'{skipped} duplicate(s) skipped. '
              f'{len(errors)} error(s):', 'warning')
        for err in errors[:10]:
            flash(err, 'danger')
        if len(errors) > 10:
            flash(f'… and {len(errors) - 10} more errors. Fix the CSV and re-upload.', 'danger')
    elif skipped:
        flash(f'Imported {success} {entity_name}(s). '
              f'{skipped} duplicate(s) were skipped.', 'success')
    else:
        flash(f'Successfully imported {success} {entity_name}(s)!', 'success')
