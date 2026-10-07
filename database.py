"""
database.py — dual-backend database layer
  • PostgreSQL in production (DATABASE_URL env var set by Railway add-on)
  • SQLite for local development (no DATABASE_URL)

All application code uses the same API:
    db = get_db()
    db.execute(sql, params)   # uses ? placeholders everywhere
    row['column']  or  row[0] # both work
    db.commit() / db.rollback() / db.close()
"""

import os
import re
import secrets
from datetime import datetime
from werkzeug.security import generate_password_hash

# ── Backend detection ──────────────────────────────────────────────────────────

DATABASE_URL = os.environ.get('DATABASE_URL', '')
# Railway injects postgres:// URLs; psycopg2 requires postgresql://
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

USE_POSTGRES = bool(DATABASE_URL and DATABASE_URL.startswith('postgresql'))

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3
    _SQLITE_DB = os.environ.get('SQLITE_DB_PATH', 'cooperative.db')


# ── Row wrapper ────────────────────────────────────────────────────────────────

from datetime import date as _date, datetime as _datetime
from decimal import Decimal as _Decimal


def _coerce(v):
    """Make PostgreSQL values match what SQLite (and the app) expect: dates as
    'YYYY-MM-DD[ HH:MM:SS]' strings, and Decimal as float. No-op for values that
    are already strings/floats (SQLite), so it is safe on both backends."""
    if isinstance(v, _datetime):
        return v.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(v, _date):
        return v.strftime('%Y-%m-%d')
    if isinstance(v, _Decimal):
        return float(v)
    return v


class _DictRow(dict):
    """
    Dict subclass that also supports integer index access (like sqlite3.Row).
    Allows row[0] and row['column'] to both work, so existing code needs
    no changes when switching from SQLite. Values are coerced so PostgreSQL
    dates/decimals look like SQLite's strings/floats.
    """
    def __init__(self, mapping):
        coerced = {k: _coerce(v) for k, v in mapping.items()}
        super().__init__(coerced)
        self._vals = list(coerced.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        return super().__getitem__(key)

    def keys(self):
        return super().keys()


# ── PostgreSQL cursor wrapper ──────────────────────────────────────────────────

class _PGCursor:
    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        row = self._cur.fetchone()
        return _DictRow(row) if row is not None else None

    def fetchall(self):
        return [_DictRow(r) for r in (self._cur.fetchall() or [])]

    def __iter__(self):
        return iter(self.fetchall())


# ── PostgreSQL connection wrapper ──────────────────────────────────────────────

class _PGConn:
    """
    Wraps a psycopg2 connection so it looks like sqlite3 to the rest of the app:
    - Accepts ? placeholders (converts to %s for psycopg2)
    - Returns _DictRow objects that support both dict and index access
    """
    def __init__(self, raw):
        self._conn = raw

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        pg_sql = sql.replace('?', '%s')
        cur.execute(pg_sql, params if params else None)
        return _PGCursor(cur)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def __del__(self):
        self.close()


# ── Public API ─────────────────────────────────────────────────────────────────

def _sqlite_row_factory(cursor, row):
    """Return SQLite rows as _DictRow, matching the PostgreSQL backend exactly.

    Using sqlite3.Row here caused SQLite-only crashes: sqlite3.Row supports
    row['col'] and row[0] but NOT row.get(), while the PostgreSQL _DictRow
    (a dict subclass) does — so code paths that call .get() on a row (e.g.
    email_service) worked in production but raised on SQLite. Unifying the row
    type removes that dev/prod drift.
    """
    return _DictRow({col[0]: row[idx] for idx, col in enumerate(cursor.description)})


def _open_connection():
    """Open a brand-new raw database connection."""
    if USE_POSTGRES:
        return _PGConn(psycopg2.connect(DATABASE_URL))
    db = sqlite3.connect(_SQLITE_DB)
    db.row_factory = _sqlite_row_factory
    return db


def get_db():
    """Return a database connection (PostgreSQL or SQLite depending on env).

    Inside a Flask request/app context the connection is cached on ``flask.g``
    and reused for the rest of the request, then closed by the
    ``teardown_appcontext`` handler registered in app.py.  This prevents the
    connection leak that occurred when every call opened a fresh connection
    that was never closed (which exhausts PostgreSQL's connection pool).

    Outside an app context (CLI scripts, ``init_db`` at import time) a plain
    connection is returned; the caller is responsible for closing it.
    """
    try:
        from flask import g, has_app_context
        if has_app_context():
            db = getattr(g, '_database', None)
            if db is None:
                db = g._database = _open_connection()
            return db
    except Exception:
        pass
    return _open_connection()


def close_db(exception=None):
    """Close the request-scoped connection, if any. Registered as a Flask
    teardown_appcontext handler in app.py."""
    try:
        from flask import g
        db = getattr(g, '_database', None)
        if db is not None:
            g._database = None
            db.close()
    except Exception:
        pass


def last_insert_id(db):
    """Return the ID generated by the most recent INSERT.

    SQLite  : SELECT last_insert_rowid()
    PostgreSQL: SELECT lastval()  — works after any INSERT into a SERIAL column
    """
    if USE_POSTGRES:
        return db.execute('SELECT lastval()').fetchone()[0]
    return db.execute('SELECT last_insert_rowid()').fetchone()[0]


# ── DDL helpers ────────────────────────────────────────────────────────────────

def _adapt(sql):
    """Convert SQLite DDL to PostgreSQL-compatible DDL."""
    if not USE_POSTGRES:
        return sql
    # AUTOINCREMENT → SERIAL (PostgreSQL sequences)
    sql = sql.replace('INTEGER PRIMARY KEY AUTOINCREMENT', 'SERIAL PRIMARY KEY')
    # SQLite REAL = 8-byte float; PostgreSQL REAL = 4-byte; use DOUBLE PRECISION
    sql = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', sql)
    # SQLite BLOB → PostgreSQL BYTEA
    sql = re.sub(r'\bBLOB\b', 'BYTEA', sql)
    return sql


def _add_col(db, table, column, col_def):
    """
    ALTER TABLE … ADD COLUMN — safe for both databases.
    Uses SAVEPOINTs for PostgreSQL so a duplicate-column error doesn't
    abort the whole transaction.
    """
    if USE_POSTGRES:
        sp = f"sp_{table}_{column}"
        db.execute(f"SAVEPOINT {sp}")
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
            db.execute(f"RELEASE SAVEPOINT {sp}")
        except Exception:
            db.execute(f"ROLLBACK TO SAVEPOINT {sp}")
    else:
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
        except Exception:
            pass  # column already exists


def _exec_ignore(db, sql):
    """Run best-effort DDL that is safe to skip on existing databases."""
    try:
        db.execute(_adapt(sql))
    except Exception as exc:
        print(f"[schema] skipped optional DDL: {exc}")


def _encrypt_existing_member_sensitive_fields(db):
    """Encrypt existing plaintext member PII once FIELD_ENCRYPTION_KEY is set.

    Wrapped in a SAVEPOINT on PostgreSQL so any failure here (e.g. a missing
    column) cannot abort the surrounding init transaction and crash boot.
    """
    if USE_POSTGRES:
        db.execute('SAVEPOINT enc_backfill')
    try:
        from crypto import SENSITIVE_MEMBER_FIELDS, encrypt_field, encryption_enabled, is_encrypted
        if not encryption_enabled():
            if USE_POSTGRES:
                db.execute('RELEASE SAVEPOINT enc_backfill')
            return
        rows = db.execute(
            'SELECT id, bank_name, account_name, account_number, bvn, nin FROM members'
        ).fetchall()
        for row in rows:
            updates = {}
            for field in SENSITIVE_MEMBER_FIELDS:
                value = row.get(field)
                if value and not is_encrypted(str(value)):
                    updates[field] = encrypt_field(str(value))
            if updates:
                assignments = ', '.join(f'{field} = ?' for field in updates)
                params = list(updates.values()) + [row['id']]
                db.execute(f'UPDATE members SET {assignments} WHERE id = ?', params)
        if USE_POSTGRES:
            db.execute('RELEASE SAVEPOINT enc_backfill')
    except Exception as exc:
        if USE_POSTGRES:
            db.execute('ROLLBACK TO SAVEPOINT enc_backfill')
        print(f"[security] skipped sensitive field encryption pass: {exc}")


# ── Schema ─────────────────────────────────────────────────────────────────────

def init_db():
    db = get_db()
    if USE_POSTGRES:
        db.execute('SELECT pg_advisory_lock(2026072301)')

    # Users table
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            full_name TEXT,
            email TEXT,
            phone TEXT,
            is_active INTEGER DEFAULT 1,
            must_change_password INTEGER DEFAULT 0,
            two_factor_secret TEXT,
            last_login TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    '''))
    _add_col(db, 'users', 'must_change_password', 'INTEGER DEFAULT 0')
    _add_col(db, 'users', 'is_super_admin',       'INTEGER DEFAULT 0')
    _add_col(db, 'users', 'two_factor_enabled',   'INTEGER DEFAULT 0')

    # One-time backup codes for 2FA recovery (stored hashed, never in clear).
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS user_backup_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            code_hash TEXT NOT NULL,
            used_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    '''))

    # Failed-login throttle — shared across app workers and persistent across
    # restarts (replaces the old per-process in-memory counter).
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            username TEXT,
            attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    '''))

    # In-app feedback / NPS survey (also recruits referral / sales partners).
    _add_col(db, 'users', 'feedback_dismissed_at', 'TIMESTAMP')
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS feedback_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            role TEXT,
            overall_experience INTEGER,
            most_loved_feature TEXT,
            improve_feature TEXT,
            recommend_score INTEGER,
            comments TEXT,
            referral_optin INTEGER DEFAULT 0,
            referral_name TEXT,
            referral_email TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    '''))

    # Members table
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_number TEXT UNIQUE,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            email TEXT UNIQUE,
            phone TEXT,
            address TEXT,
            occupation TEXT,
            date_of_birth DATE,
            nominee_name TEXT,
            nominee_relationship TEXT,
            nominee_phone TEXT,
            nominee_email TEXT,
            nominee_address TEXT,
            alt_nominee_name TEXT,
            alt_nominee_relationship TEXT,
            monthly_savings REAL DEFAULT 5000,
            total_savings REAL DEFAULT 0,
            shares INTEGER DEFAULT 0,
            shares_value REAL DEFAULT 0,
            status TEXT DEFAULT 'active',
            date_joined TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            photo_path TEXT,
            card_number TEXT,
            card_status TEXT DEFAULT 'active',
            card_issued_date TIMESTAMP,
            card_expiry_date TIMESTAMP,
            emergency_contact_name TEXT,
            emergency_contact_phone TEXT,
            next_of_kin TEXT,
            bank_name TEXT,
            account_number TEXT,
            account_name TEXT,
            bvn TEXT,
            nin TEXT
        )
    '''))
    _add_col(db, 'members', 'card_token', 'TEXT')
    _add_col(db, 'members', 'card_path',  'TEXT')
    _add_col(db, 'members', 'employee_id', 'TEXT')
    # A member can decline SMS without losing in-app and push notifications.
    _add_col(db, 'members', 'sms_optout', 'INTEGER DEFAULT 0')
    # What a member says their transfers are for: '' follows the cooperative's
    # rule, otherwise 'savings', 'loan' or 'ctas'.
    _add_col(db, 'members', 'payment_preference', 'TEXT')
    # Optional annual salary — only used for salary-based CTAS affordability; 0
    # when the cooperative is not salary/staff-based (the default is savings-based).
    _add_col(db, 'members', 'annual_salary', 'REAL DEFAULT 0')
    _add_col(db, 'members', 'city', 'TEXT')
    _add_col(db, 'members', 'state', 'TEXT')
    _add_col(db, 'members', 'country', "TEXT DEFAULT 'Nigeria'")
    # Sensitive ID fields (encrypted at rest). These are written by the profile
    # form and read by the encryption backfill; without the columns, PostgreSQL
    # aborts the init transaction and the app fails to boot.
    _add_col(db, 'members', 'bvn', 'TEXT')
    _add_col(db, 'members', 'nin', 'TEXT')
    # Member-departure archive: status='former' plus who/when/why they left,
    # kept for audit & reconciliation (never deleted).
    _add_col(db, 'members', 'exit_date', 'DATE')
    _add_col(db, 'members', 'exit_reason', 'TEXT')
    _add_col(db, 'members', 'exit_note', 'TEXT')
    _encrypt_existing_member_sensitive_fields(db)

    # Savings table
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS savings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            month TEXT NOT NULL,
            payment_type TEXT DEFAULT 'monthly',
            late_fee REAL DEFAULT 0,
            payment_method TEXT DEFAULT 'cash',
            reference TEXT,
            receipt_number TEXT,
            notes TEXT,
            created_by INTEGER,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            verified_by INTEGER,
            verified_at TIMESTAMP,
            FOREIGN KEY (member_id) REFERENCES members (id)
        )
    '''))
    _add_col(db, 'savings', 'payment_type', "TEXT DEFAULT 'monthly'")
    # Portion of a contribution allocated to share capital (audit trail per row)
    _add_col(db, 'savings', 'share_capital', 'REAL DEFAULT 0')
    _add_col(db, 'savings', 'reference', 'TEXT')
    _add_col(db, 'savings', 'receipt_number', 'TEXT')
    _add_col(db, 'savings', 'notes', 'TEXT')
    _add_col(db, 'savings', 'created_by', 'INTEGER')
    # Supporting document for a payout/withdrawal (path under static/).
    _add_col(db, 'savings', 'evidence_path', 'TEXT')
    _add_col(db, 'savings', 'verified_by', 'INTEGER')
    _add_col(db, 'savings', 'verified_at', 'TIMESTAMP')
    _add_col(db, 'savings', 'import_batch', 'TEXT')
    _add_col(db, 'savings', 'source_file', 'TEXT')
    # Set when an upload batch is reversed, so the row no longer blocks a
    # corrected re-upload (excluded from the duplicate check; receipt freed).
    _add_col(db, 'savings', 'reversed_at', 'TIMESTAMP')

    # Loans table
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS loans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_number TEXT UNIQUE,
            member_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            purpose TEXT,
            description TEXT,
            tenure INTEGER,
            interest_rate REAL,
            interest_method TEXT DEFAULT 'reducing_annual',
            total_repayment REAL,
            balance REAL,
            status TEXT DEFAULT 'pending',
            application_fee REAL DEFAULT 0,
            insurance_premium REAL DEFAULT 0,
            disbursed_amount REAL,
            disbursement_date TIMESTAMP,
            first_payment_date TIMESTAMP,
            next_payment_date TIMESTAMP,
            approved_by INTEGER,
            approved_at TIMESTAMP,
            rejection_reason TEXT,
            completed_at TIMESTAMP,
            defaulted INTEGER DEFAULT 0,
            notes TEXT,
            date_applied TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (member_id) REFERENCES members (id)
        )
    '''))
    _add_col(db, 'loans', 'loan_number', 'TEXT')
    _add_col(db, 'loans', 'interest_method', "TEXT DEFAULT 'reducing_annual'")
    # Loan approval workflow stage: guarantors -> secretary -> treasurer -> president -> approved/rejected
    _add_col(db, 'loans', 'approval_stage', "TEXT DEFAULT 'secretary'")
    # Applicant terms-and-conditions consent (typed-name signature + date)
    _add_col(db, 'loans', 'terms_accepted', 'INTEGER DEFAULT 0')
    _add_col(db, 'loans', 'signature_name', 'TEXT')
    _add_col(db, 'loans', 'signed_at', 'TIMESTAMP')
    _add_col(db, 'loans', 'data_processing_consent', 'INTEGER DEFAULT 0')
    _add_col(db, 'loans', 'credit_check_consent', 'INTEGER DEFAULT 0')
    _add_col(db, 'loans', 'repayment_schedule_accepted', 'INTEGER DEFAULT 0')
    _add_col(db, 'loans', 'bank_statement_status', "TEXT DEFAULT 'requested'")
    _add_col(db, 'loans', 'payment_collateral_type', 'TEXT')
    _add_col(db, 'loans', 'payment_collateral_status', "TEXT DEFAULT 'pending'")
    _add_col(db, 'loans', 'repayment_schedule_snapshot', 'TEXT')
    _add_col(db, 'loans', 'consent_ip', 'TEXT')
    _add_col(db, 'loans', 'loan_applicant_type', "TEXT DEFAULT 'non_staff'")
    _add_col(db, 'loans', 'hr_affordability_consent', 'INTEGER DEFAULT 0')
    _add_col(db, 'loans', 'hr_affordability_status', "TEXT DEFAULT 'not_required'")
    _add_col(db, 'loans', 'credit_check_status', "TEXT DEFAULT 'not_required'")
    _add_col(db, 'loans', 'due_diligence_updated_by', 'INTEGER')
    _add_col(db, 'loans', 'due_diligence_updated_at', 'TIMESTAMP')
    _add_col(db, 'loans', 'disbursed_amount', 'REAL')
    _add_col(db, 'loans', 'disbursement_date', 'TIMESTAMP')
    _add_col(db, 'loans', 'first_payment_date', 'TIMESTAMP')
    _add_col(db, 'loans', 'next_payment_date', 'TIMESTAMP')
    _add_col(db, 'loans', 'approved_by', 'INTEGER')
    _add_col(db, 'loans', 'approved_at', 'TIMESTAMP')
    _add_col(db, 'loans', 'rejection_reason', 'TEXT')
    _add_col(db, 'loans', 'withdrawn_at', 'TIMESTAMP')
    _add_col(db, 'loans', 'withdrawn_by', 'INTEGER')
    _add_col(db, 'loans', 'withdrawal_reason', 'TEXT')
    # Loan request alert pipeline (see loan_alerts.py)
    _add_col(db, 'loans', 'submission_channel', 'TEXT')
    _add_col(db, 'loans', 'stage_entered_at', 'TIMESTAMP')
    _add_col(db, 'loans', 'alert_count', 'INTEGER DEFAULT 0')
    _add_col(db, 'loans', 'first_alert_at', 'TIMESTAMP')
    _add_col(db, 'loans', 'last_alert_at', 'TIMESTAMP')
    _add_col(db, 'loans', 'last_reminder_at', 'TIMESTAMP')
    _add_col(db, 'loans', 'escalated_at', 'TIMESTAMP')
    _add_col(db, 'loans', 'first_response_at', 'TIMESTAMP')
    _add_col(db, 'loans', 'completed_at', 'TIMESTAMP')
    _add_col(db, 'loans', 'defaulted', 'INTEGER DEFAULT 0')
    _add_col(db, 'loans', 'notes', 'TEXT')

    # Repayments table
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS repayments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repayment_number TEXT UNIQUE,
            loan_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            principal_paid REAL DEFAULT 0,
            interest_paid REAL DEFAULT 0,
            penalty_paid REAL DEFAULT 0,
            payment_method TEXT DEFAULT 'cash',
            reference TEXT,
            receipt_number TEXT,
            transaction_id TEXT,
            notes TEXT,
            received_by INTEGER,
            verified_by INTEGER,
            verified_at TIMESTAMP,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (loan_id) REFERENCES loans (id)
        )
    '''))
    # Ensure the principal/interest split columns exist on databases created
    # from an older schema (safe no-op if they already exist).
    _add_col(db, 'repayments', 'repayment_number', 'TEXT')
    _add_col(db, 'repayments', 'principal_paid', 'REAL DEFAULT 0')
    _add_col(db, 'repayments', 'interest_paid',  'REAL DEFAULT 0')
    _add_col(db, 'repayments', 'penalty_paid',   'REAL DEFAULT 0')
    _add_col(db, 'repayments', 'reference', 'TEXT')
    _add_col(db, 'repayments', 'receipt_number', 'TEXT')
    _add_col(db, 'repayments', 'transaction_id', 'TEXT')
    _add_col(db, 'repayments', 'notes', 'TEXT')
    _add_col(db, 'repayments', 'received_by', 'INTEGER')
    _add_col(db, 'repayments', 'verified_by', 'INTEGER')
    _add_col(db, 'repayments', 'reversed_at', 'TIMESTAMP')
    _add_col(db, 'repayments', 'verified_at', 'TIMESTAMP')

    # Controlled loan balance corrections used during migration/reconciliation.
    # These are not cash repayments: they move the loan subledger and GL
    # receivable together against accumulated surplus, with an audit reason.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS loan_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            adjustment_number TEXT UNIQUE,
            loan_id INTEGER NOT NULL,
            member_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            direction TEXT NOT NULL,
            previous_balance REAL NOT NULL,
            new_balance REAL NOT NULL,
            reason TEXT NOT NULL,
            reference TEXT,
            source_file TEXT,
            journal_entry_id INTEGER,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reversed_at TIMESTAMP,
            FOREIGN KEY (loan_id) REFERENCES loans (id),
            FOREIGN KEY (member_id) REFERENCES members (id)
        )
    '''))
    _add_col(db, 'loan_adjustments', 'reversed_at', 'TIMESTAMP')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_loan_adjustments_loan ON loan_adjustments(loan_id)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_loan_adjustments_member ON loan_adjustments(member_id)')
    _exec_ignore(db, "CREATE UNIQUE INDEX IF NOT EXISTS uq_loan_adjustments_reference ON loan_adjustments(reference) WHERE reference IS NOT NULL AND reference != ''")

    # Manual member bank receipts: one bank inflow can be allocated across
    # savings and one or more loan repayments while staying traceable as one
    # receipt for bank reconciliation.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS member_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_number TEXT UNIQUE NOT NULL,
            member_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            allocated_savings REAL DEFAULT 0,
            allocated_loans REAL DEFAULT 0,
            bank_account TEXT NOT NULL,
            payment_method TEXT DEFAULT 'transfer',
            bank_reference TEXT,
            notes TEXT,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            journal_entry_id INTEGER,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reversed_at TIMESTAMP,
            FOREIGN KEY (member_id) REFERENCES members (id)
        )
    '''))
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS member_receipt_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER NOT NULL,
            target TEXT NOT NULL,
            target_id INTEGER,
            loan_id INTEGER,
            amount REAL NOT NULL,
            principal_paid REAL DEFAULT 0,
            interest_paid REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (receipt_id) REFERENCES member_receipts (id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_member_receipts_member ON member_receipts(member_id)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_member_receipts_date ON member_receipts(date)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_member_receipt_alloc_receipt ON member_receipt_allocations(receipt_id)')

    # Loan guarantors — members who back a loan and must consent
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS loan_guarantors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_id INTEGER NOT NULL,
            member_id INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            responded_at TIMESTAMP,
            comment TEXT,
            FOREIGN KEY (loan_id) REFERENCES loans (id),
            FOREIGN KEY (member_id) REFERENCES members (id)
        )
    '''))
    # Loan approval audit trail — one row per stage action
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS loan_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_id INTEGER NOT NULL,
            stage TEXT NOT NULL,
            action TEXT NOT NULL,
            acted_by INTEGER,
            acted_by_name TEXT,
            acted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            comment TEXT,
            FOREIGN KEY (loan_id) REFERENCES loans (id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_loan_guarantors_loan ON loan_guarantors(loan_id)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_loan_guarantors_member ON loan_guarantors(member_id)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_loan_approvals_loan ON loan_approvals(loan_id)')

    # Loan request pipeline events — every alert, reminder, escalation and view
    # raised for a loan application is logged here. A loan request is treated
    # like a sales lead: it must be acknowledged fast, and the cooperative must
    # be able to prove who was told what, and when.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS loan_request_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            stage TEXT,
            channel TEXT,
            recipient_user_id INTEGER,
            recipient_name TEXT,
            recipient_role TEXT,
            recipient_email TEXT,
            delivery TEXT,
            status TEXT DEFAULT 'sent',
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (loan_id) REFERENCES loans (id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_loan_request_events_loan ON loan_request_events(loan_id)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_loan_request_events_type ON loan_request_events(event_type)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_loan_request_events_created ON loan_request_events(created_at)')

    # Investments table
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS investments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            investment_number TEXT UNIQUE,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            type TEXT NOT NULL,
            description TEXT,
            institution TEXT,
            interest_rate REAL,
            return_rate REAL,
            risk_level TEXT DEFAULT 'medium',
            start_date TIMESTAMP,
            maturity_date TIMESTAMP,
            duration_days INTEGER,
            expected_return REAL,
            actual_return REAL,
            current_value REAL,
            approval_status TEXT DEFAULT 'pending',
            approved_by INTEGER,
            approved_at TIMESTAMP,
            documents TEXT,
            notes TEXT,
            created_by INTEGER,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (approved_by) REFERENCES users (id),
            FOREIGN KEY (created_by) REFERENCES users (id)
        )
    '''))

    # Honorarium table
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS honorarium (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_id INTEGER,
            recipient_name TEXT,
            amount REAL NOT NULL,
            description TEXT,
            month TEXT,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            paid_by INTEGER,
            FOREIGN KEY (recipient_id) REFERENCES members (id),
            FOREIGN KEY (paid_by) REFERENCES users (id)
        )
    '''))
    _add_col(db, 'honorarium', 'recipient_id', 'INTEGER')
    _add_col(db, 'honorarium', 'recipient_name', 'TEXT')
    _add_col(db, 'honorarium', 'description', 'TEXT')
    _add_col(db, 'honorarium', 'month', 'TEXT')
    _add_col(db, 'honorarium', 'paid_by', 'INTEGER')

    # Expenses table
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expense_number TEXT UNIQUE,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            description TEXT,
            vendor TEXT,
            receipt_number TEXT,
            paid_to TEXT,
            payment_method TEXT,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            approved_by INTEGER,
            recorded_by INTEGER,
            notes TEXT,
            FOREIGN KEY (approved_by) REFERENCES users (id),
            FOREIGN KEY (recorded_by) REFERENCES users (id)
        )
    '''))

    # Revenue table
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS revenue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            revenue_number TEXT UNIQUE,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            description TEXT,
            source TEXT,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            received_by INTEGER,
            notes TEXT,
            FOREIGN KEY (received_by) REFERENCES users (id)
        )
    '''))

    # Settings table
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            value TEXT NOT NULL,
            description TEXT
        )
    '''))

    # Notifications table
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            notification_type TEXT DEFAULT 'info',
            is_read INTEGER DEFAULT 0,
            action_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            read_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    '''))
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS mobile_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            member_id INTEGER,
            platform TEXT,
            push_token TEXT UNIQUE NOT NULL,
            device_name TEXT,
            enabled INTEGER DEFAULT 1,
            last_seen_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (member_id) REFERENCES members (id)
        )
    '''))
    _add_col(db, 'mobile_devices', 'member_id', 'INTEGER')
    _add_col(db, 'mobile_devices', 'platform', 'TEXT')
    _add_col(db, 'mobile_devices', 'device_name', 'TEXT')
    _add_col(db, 'mobile_devices', 'enabled', 'INTEGER DEFAULT 1')
    _add_col(db, 'mobile_devices', 'last_seen_at', 'TIMESTAMP')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_mobile_devices_user ON mobile_devices(user_id, enabled)')

    # Central mobile tenant registry. In production this is primarily used by
    # the HQ tenant so the mobile app can resolve short codes like "ooucoop"
    # without guessing client domains.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS coop_tenants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            logo_url TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    '''))
    _add_col(db, 'coop_tenants', 'logo_url', 'TEXT')
    _add_col(db, 'coop_tenants', 'is_active', 'INTEGER DEFAULT 1')
    _add_col(db, 'coop_tenants', 'updated_at', 'TIMESTAMP')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_coop_tenants_code ON coop_tenants(code, is_active)')

    # ── Dedicated virtual accounts ────────────────────────────────────────────
    # A permanent bank account number per member. Money transferred to it is
    # identified by the account it landed in, so nothing is matched by hand.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS member_virtual_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            provider TEXT DEFAULT 'paystack',
            customer_code TEXT,
            account_number TEXT,
            account_name TEXT,
            bank_name TEXT,
            bank_slug TEXT,
            provider_account_id TEXT,
            status TEXT DEFAULT 'active',
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closed_at TIMESTAMP,
            FOREIGN KEY (member_id) REFERENCES members (id)
        )
    '''))
    _exec_ignore(db, 'CREATE UNIQUE INDEX IF NOT EXISTS idx_mva_member_provider '
                     'ON member_virtual_accounts(member_id, provider)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_mva_account_number '
                     'ON member_virtual_accounts(account_number)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_mva_customer '
                     'ON member_virtual_accounts(customer_code)')

    # Money arrives unannounced, so every inflow is banked first and applied
    # second. provider_reference is the idempotency key — a webhook redelivery
    # must never create a second receipt.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS virtual_account_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER,
            virtual_account_id INTEGER,
            provider TEXT DEFAULT 'paystack',
            provider_reference TEXT,
            amount REAL NOT NULL,
            account_number TEXT,
            sender_name TEXT,
            sender_bank TEXT,
            narration TEXT,
            status TEXT DEFAULT 'unallocated',
            allocated_amount REAL DEFAULT 0,
            received_at TIMESTAMP,
            allocated_at TIMESTAMP,
            allocated_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (member_id) REFERENCES members (id)
        )
    '''))
    _exec_ignore(db, 'CREATE UNIQUE INDEX IF NOT EXISTS idx_var_provider_ref '
                     'ON virtual_account_receipts(provider, provider_reference)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_var_status '
                     'ON virtual_account_receipts(status, id)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_var_member '
                     'ON virtual_account_receipts(member_id, id)')

    # What each receipt was applied to, so one leg can be undone on its own.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS virtual_account_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER NOT NULL,
            target TEXT NOT NULL,
            target_id INTEGER,
            loan_id INTEGER,
            amount REAL NOT NULL,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reversed_at TIMESTAMP,
            FOREIGN KEY (receipt_id) REFERENCES virtual_account_receipts (id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_vaa_receipt '
                     'ON virtual_account_allocations(receipt_id)')

    # Every SMS attempt, so a cooperative can see what its credit was spent on.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS sms_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER,
            msisdn TEXT,
            purpose TEXT,
            body TEXT,
            status TEXT DEFAULT 'sent',
            error TEXT,
            provider_ref TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (member_id) REFERENCES members (id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_sms_log_created ON sms_log(created_at)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_sms_log_member ON sms_log(member_id, status)')

    # ── HQ billing (operator-side): client registry, invoices, line items ──
    # Only used by the HQ instance (MARKETING_HQ=1). Each client is a tenant
    # cooperative the operator bills; per-user subscription plus service fees.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS hq_clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            code TEXT,
            billing_email TEXT,
            phone TEXT,
            user_count INTEGER DEFAULT 0,
            billed_user_count INTEGER DEFAULT 0,
            rate_per_user REAL DEFAULT 5000,
            billing_cycle TEXT DEFAULT 'annual',
            period_start DATE,
            period_end DATE,
            status TEXT DEFAULT 'active',
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    '''))
    _add_col(db, 'hq_clients', 'access_state', "TEXT DEFAULT 'active'")
    _add_col(db, 'hq_clients', 'ctas_enabled', 'INTEGER DEFAULT 0')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_hq_clients_status ON hq_clients(status)')

    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS hq_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number TEXT UNIQUE,
            client_id INTEGER NOT NULL,
            period_label TEXT,
            issue_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            due_date DATE,
            amount REAL NOT NULL DEFAULT 0,
            currency TEXT DEFAULT 'NGN',
            status TEXT DEFAULT 'draft',
            payment_reference TEXT,
            paid_at TIMESTAMP,
            paid_method TEXT,
            pay_token TEXT,
            sent_at TIMESTAMP,
            notes TEXT,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES hq_clients (id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_hq_invoices_client ON hq_invoices(client_id, status)')

    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS hq_invoice_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL,
            item_type TEXT DEFAULT 'service',
            description TEXT,
            quantity REAL DEFAULT 1,
            unit_price REAL DEFAULT 0,
            amount REAL DEFAULT 0,
            FOREIGN KEY (invoice_id) REFERENCES hq_invoices (id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_hq_invoice_items_invoice ON hq_invoice_items(invoice_id)')

    # One-off setup fee, priced by how many members the cooperative has. Kept as
    # its own line type (not a service fee) because it is the only line the
    # affiliate commission is calculated on, so it has to be identifiable
    # without reading a description string.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS hq_setup_bands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            min_members INTEGER NOT NULL DEFAULT 0,
            max_members INTEGER,
            amount REAL NOT NULL DEFAULT 0,
            label TEXT,
            active INTEGER DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_hq_setup_bands_min ON hq_setup_bands(min_members)')
    # A negotiated fee for one client; 0/NULL means use the band for their size.
    _add_col(db, 'hq_clients', 'setup_fee', 'REAL DEFAULT 0')

    # ── Affiliate channel ─────────────────────────────────────────────────────
    # Two tiers: a team member closes cooperatives, a team lead recruits and
    # supports members. `parent_id` is the lead a member currently sits under
    # and moves when they are promoted; `recruited_by` records who actually
    # introduced them and never changes, which is what the promotion threshold
    # is counted on.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS affiliates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            tier TEXT DEFAULT 'member',
            parent_id INTEGER,
            recruited_by INTEGER,
            status TEXT DEFAULT 'applied',
            bank_name TEXT,
            account_number TEXT,
            account_name TEXT,
            notes TEXT,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TIMESTAMP,
            reviewed_by INTEGER,
            approved_at TIMESTAMP,
            accept_token TEXT,
            accepted_at TIMESTAMP,
            accepted_ip TEXT,
            declined_reason TEXT,
            promoted_at TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (parent_id) REFERENCES affiliates (id),
            FOREIGN KEY (recruited_by) REFERENCES affiliates (id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_affiliates_status ON affiliates(status)')
    _add_col(db, 'affiliates', 'signature_name', 'TEXT')
    _add_col(db, 'affiliates', 'accepted_terms', 'TEXT')
    _add_col(db, 'affiliates', 'accepted_terms_version', 'TEXT')
    db.execute(_adapt('''CREATE TABLE IF NOT EXISTS affiliate_commission_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL,
        operation TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 1,
        last_error TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(invoice_id, operation)
    )'''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_affiliates_parent ON affiliates(parent_id)')
    _exec_ignore(db, "CREATE UNIQUE INDEX IF NOT EXISTS uq_affiliates_email ON affiliates(email) WHERE email IS NOT NULL AND email != ''")

    # One row per earning event, tied to the paid invoice that produced it.
    # Commission is never recomputed from a running balance: an affiliate must
    # be able to see which cooperative, which invoice and which rate produced
    # every naira, because disputes over commission are inevitable. A reversal
    # is a compensating negative row pointing at the original, never a delete.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS affiliate_commissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            affiliate_id INTEGER NOT NULL,
            client_id INTEGER,
            invoice_id INTEGER,
            source_affiliate_id INTEGER,
            role TEXT DEFAULT 'direct',
            basis_amount REAL DEFAULT 0,
            rate REAL DEFAULT 0,
            amount REAL DEFAULT 0,
            status TEXT DEFAULT 'earned',
            note TEXT,
            reversal_of INTEGER,
            reversed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_by INTEGER,
            FOREIGN KEY (affiliate_id) REFERENCES affiliates (id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_aff_comm_affiliate ON affiliate_commissions(affiliate_id, status)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_aff_comm_invoice ON affiliate_commissions(invoice_id)')
    # Accrual runs once per invoice per earner; the guard makes a repeated
    # mark-paid or a replayed gateway callback harmless.
    _exec_ignore(db, 'CREATE UNIQUE INDEX IF NOT EXISTS uq_aff_comm_earned '
                     'ON affiliate_commissions(invoice_id, affiliate_id, role) '
                     "WHERE status = 'earned'")

    # Magic-link access to an affiliate's own statement. Affiliates are external
    # contractors who sign in rarely, and HQ also runs billing and tenant
    # suspension — so they get a short-lived emailed link rather than an account
    # on that instance. Reusable within its window because mail scanners follow
    # links, which would burn a single-use token before the affiliate clicked it.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS affiliate_portal_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            affiliate_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_ip TEXT,
            last_used_at TIMESTAMP,
            FOREIGN KEY (affiliate_id) REFERENCES affiliates (id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_aff_portal_tokens_aff '
                     'ON affiliate_portal_tokens(affiliate_id)')

    # Attribution is snapshotted on the client when it is linked, so later edits
    # to the lead cannot silently move a commission.
    _add_col(db, 'hq_clients', 'lead_id', 'INTEGER')
    _add_col(db, 'hq_clients', 'affiliate_id', 'INTEGER')
    _add_col(db, 'hq_clients', 'attributed_at', 'TIMESTAMP')

    # ── CTAS: Cooperative Target Advance Scheme (ajo/esusu with balloted payout
    # order + payroll recovery). Reuses members + the double-entry GL. ──
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS ctas_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            status TEXT DEFAULT 'draft',
            start_date DATE,
            end_date DATE,
            duration_months INTEGER DEFAULT 6,
            fixed_monthly_amount REAL DEFAULT 0,
            monthly_capacity INTEGER DEFAULT 1,
            earliest_payout_month INTEGER DEFAULT 1,
            max_participants INTEGER DEFAULT 0,
            admin_fee_flat REAL DEFAULT 0,
            admin_fee_percentage REAL DEFAULT 0,
            admin_fee_cap REAL DEFAULT 0,
            admin_fee_threshold REAL DEFAULT 0,
            ballot_date DATE,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_ctas_cycles_status ON ctas_cycles(status)')
    # Affordability basis for a cycle — cooperatives are not always salary-based:
    #   'savings' (default): target <= savings_multiple x member savings balance
    #   'salary' : monthly deduction <= affordability_ratio x monthly salary
    #   'manual' : no automatic test; finance/committee assesses at approval
    _add_col(db, 'ctas_cycles', 'affordability_method', "TEXT DEFAULT 'savings'")
    _add_col(db, 'ctas_cycles', 'affordability_ratio', 'REAL DEFAULT 0.5')
    _add_col(db, 'ctas_cycles', 'savings_multiple', 'REAL DEFAULT 3')
    # Generic contribution period (a period is not always a month): frequency +
    # number of periods + the fixed contribution per period. `periods` supersedes
    # `duration_months` (which stays for older rows / monthly cycles).
    _add_col(db, 'ctas_cycles', 'plan_id', 'INTEGER')
    _add_col(db, 'ctas_cycles', 'frequency', "TEXT DEFAULT 'monthly'")
    _add_col(db, 'ctas_cycles', 'periods', 'INTEGER')
    _add_col(db, 'ctas_cycles', 'contribution_amount', 'REAL DEFAULT 0')

    # Reusable CTAS product definitions. A Plan is created once and executed as
    # many Cycles. Target = contribution_amount x periods.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS ctas_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            contribution_amount REAL NOT NULL DEFAULT 0,
            frequency TEXT DEFAULT 'monthly',
            periods INTEGER NOT NULL DEFAULT 12,
            target_amount REAL NOT NULL DEFAULT 0,
            monthly_capacity INTEGER DEFAULT 1,
            earliest_payout_month INTEGER DEFAULT 1,
            admin_fee_flat REAL DEFAULT 0,
            admin_fee_percentage REAL DEFAULT 0,
            admin_fee_cap REAL DEFAULT 0,
            admin_fee_threshold REAL DEFAULT 0,
            affordability_method TEXT DEFAULT 'savings',
            affordability_ratio REAL DEFAULT 0.5,
            savings_multiple REAL DEFAULT 3,
            status TEXT DEFAULT 'active',
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_ctas_plans_status ON ctas_plans(status)')

    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS ctas_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER NOT NULL,
            member_id INTEGER NOT NULL,
            target_amount REAL NOT NULL,
            tenure_months INTEGER NOT NULL,
            monthly_deduction REAL DEFAULT 0,
            admin_fee REAL DEFAULT 0,
            requested_payout_month INTEGER,
            status TEXT DEFAULT 'submitted',
            payout_month INTEGER,
            payout_date DATE,
            total_recovered REAL DEFAULT 0,
            outstanding REAL DEFAULT 0,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            approved_at TIMESTAMP,
            approved_by INTEGER,
            enrolled_at TIMESTAMP,
            ballot_assigned_at TIMESTAMP,
            paid_out_at TIMESTAMP,
            completed_at TIMESTAMP,
            rejected_reason TEXT,
            created_by INTEGER,
            FOREIGN KEY (cycle_id) REFERENCES ctas_cycles (id),
            FOREIGN KEY (member_id) REFERENCES members (id)
        )
    '''))
    _exec_ignore(db, 'CREATE UNIQUE INDEX IF NOT EXISTS uq_ctas_sub_member_cycle ON ctas_subscriptions(member_id, cycle_id)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_ctas_sub_cycle_status ON ctas_subscriptions(cycle_id, status)')
    # Terms acceptance at application + the multi-step approval chain
    # (submitted -> eligible -> finance_reviewed -> approved -> enrolled).
    _add_col(db, 'ctas_subscriptions', 'terms_accepted', 'INTEGER DEFAULT 0')
    _add_col(db, 'ctas_subscriptions', 'signature_name', 'TEXT')
    _add_col(db, 'ctas_subscriptions', 'signed_at', 'TIMESTAMP')
    _add_col(db, 'ctas_subscriptions', 'eligibility_at', 'TIMESTAMP')
    _add_col(db, 'ctas_subscriptions', 'eligibility_by', 'INTEGER')
    _add_col(db, 'ctas_subscriptions', 'finance_reviewed_at', 'TIMESTAMP')
    _add_col(db, 'ctas_subscriptions', 'finance_reviewed_by', 'INTEGER')
    # Rotating-pool model: total contributions across the whole cycle, and the
    # co-op's outstanding advance to a member after their payout.
    _add_col(db, 'ctas_subscriptions', 'contributed_total', 'REAL DEFAULT 0')
    _add_col(db, 'ctas_subscriptions', 'advance_balance', 'REAL DEFAULT 0')

    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS ctas_ballot_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER NOT NULL,
            seed TEXT,
            summary TEXT,
            executed_by INTEGER,
            run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (cycle_id) REFERENCES ctas_cycles (id)
        )
    '''))

    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS ctas_payroll_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER NOT NULL,
            month_number INTEGER NOT NULL,
            kind TEXT DEFAULT 'export',
            file_name TEXT,
            processed INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (cycle_id) REFERENCES ctas_cycles (id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_ctas_payroll_cycle ON ctas_payroll_batches(cycle_id, month_number)')

    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS ctas_payroll_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            subscription_id INTEGER NOT NULL,
            expected_amount REAL DEFAULT 0,
            actual_amount REAL DEFAULT 0,
            status TEXT DEFAULT 'deducted',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (batch_id) REFERENCES ctas_payroll_batches (id),
            FOREIGN KEY (subscription_id) REFERENCES ctas_subscriptions (id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_ctas_payroll_lines_sub ON ctas_payroll_lines(subscription_id)')
    _add_col(db, 'ctas_subscriptions', 'arrears_amount', 'REAL DEFAULT 0')
    # Days after a due date before a contribution is treated as late.
    _add_col(db, 'ctas_cycles', 'grace_days', 'INTEGER DEFAULT 7')
    _add_col(db, 'ctas_plans', 'grace_days', 'INTEGER DEFAULT 7')

    # One row per member per contribution period: what is owed, when, and whether
    # it has been paid. Drives arrears, reminders and (later) automatic debits.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS ctas_schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL,
            cycle_id INTEGER NOT NULL,
            period_number INTEGER NOT NULL,
            due_date DATE,
            expected_amount REAL DEFAULT 0,
            paid_amount REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',
            paid_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (subscription_id) REFERENCES ctas_subscriptions (id),
            FOREIGN KEY (cycle_id) REFERENCES ctas_cycles (id)
        )
    '''))
    _exec_ignore(db, 'CREATE UNIQUE INDEX IF NOT EXISTS uq_ctas_schedule_sub_period '
                     'ON ctas_schedule(subscription_id, period_number)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_ctas_schedule_due ON ctas_schedule(due_date, status)')
    # Automatic-charge retry tracking, so a declined card is retried on a sane
    # ladder instead of being hammered every run.
    _add_col(db, 'ctas_schedule', 'charge_attempts', 'INTEGER DEFAULT 0')
    _add_col(db, 'ctas_schedule', 'last_attempt_at', 'TIMESTAMP')
    _add_col(db, 'ctas_schedule', 'next_retry_at', 'DATE')
    _add_col(db, 'ctas_cycles', 'retry_days', 'INTEGER DEFAULT 3')
    _add_col(db, 'ctas_cycles', 'max_charge_attempts', 'INTEGER DEFAULT 3')
    _add_col(db, 'ctas_plans', 'retry_days', 'INTEGER DEFAULT 3')
    _add_col(db, 'ctas_plans', 'max_charge_attempts', 'INTEGER DEFAULT 3')

    # Liquidity: what the cooperative has set aside for this cycle and how much
    # it has approved to lend the pool when contributions fall short of a payout.
    _add_col(db, 'ctas_cycles', 'liquidity_reserve', 'REAL DEFAULT 0')
    _add_col(db, 'ctas_cycles', 'liquidity_support', 'REAL DEFAULT 0')
    _add_col(db, 'ctas_cycles', 'liquidity_buffer', 'REAL DEFAULT 0')
    _add_col(db, 'ctas_plans', 'liquidity_buffer', 'REAL DEFAULT 0')

    # Priority payout: a member may request an earlier position for a fee.
    _add_col(db, 'ctas_cycles', 'priority_enabled', 'INTEGER DEFAULT 0')
    _add_col(db, 'ctas_plans', 'priority_enabled', 'INTEGER DEFAULT 0')
    _add_col(db, 'ctas_subscriptions', 'is_priority', 'INTEGER DEFAULT 0')
    _add_col(db, 'ctas_subscriptions', 'priority_fee', 'REAL DEFAULT 0')
    _add_col(db, 'ctas_subscriptions', 'priority_status', 'TEXT')
    _add_col(db, 'ctas_subscriptions', 'priority_decided_at', 'TIMESTAMP')
    _add_col(db, 'ctas_subscriptions', 'priority_note', 'TEXT')

    # Security cover: how much of what a member would still owe, if they stopped
    # contributing right after collecting, must be backed by value the
    # cooperative can actually reach. 0 = off (the default, so nothing changes
    # for a cooperative that has not opted in). Enforced at the ballot by
    # holding under-covered members back to later, lower-exposure positions.
    _add_col(db, 'ctas_cycles', 'coverage_ratio', 'REAL DEFAULT 0')
    _add_col(db, 'ctas_plans', 'coverage_ratio', 'REAL DEFAULT 0')
    _add_col(db, 'ctas_subscriptions', 'security_value', 'REAL DEFAULT 0')
    _add_col(db, 'ctas_subscriptions', 'min_payout_position', 'INTEGER')

    # Another member backing a CTAS subscription, so a member whose own savings
    # are thin can still be balloted early. Mirrors loan_guarantors: the
    # guarantor must consent before the pledge counts for anything.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS ctas_guarantors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL,
            member_id INTEGER NOT NULL,
            amount REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            responded_at TIMESTAMP,
            comment TEXT,
            signature_name TEXT,
            recovered_amount REAL DEFAULT 0,
            FOREIGN KEY (subscription_id) REFERENCES ctas_subscriptions (id),
            FOREIGN KEY (member_id) REFERENCES members (id)
        )
    '''))
    _exec_ignore(db, 'CREATE UNIQUE INDEX IF NOT EXISTS uq_ctas_guarantor '
                     'ON ctas_guarantors(subscription_id, member_id)')
    # Who authorised calling on the guarantee, and the committee decision behind
    # it — recovering from a guarantor takes money from a member who is not the
    # one leaving, so the authority for it is kept on the record.
    _add_col(db, 'ctas_guarantors', 'called_by', 'INTEGER')
    _add_col(db, 'ctas_guarantors', 'called_at', 'TIMESTAMP')
    _add_col(db, 'ctas_guarantors', 'called_reference', 'TEXT')

    # Fee charged for each early payout position (earlier positions cost more).
    # A row belongs to either a plan (the template) or a cycle (the live copy).
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS ctas_priority_fees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER,
            cycle_id INTEGER,
            position INTEGER NOT NULL,
            fee REAL DEFAULT 0
        )
    '''))
    _exec_ignore(db, 'CREATE UNIQUE INDEX IF NOT EXISTS uq_ctas_prio_cycle ON ctas_priority_fees(cycle_id, position)')
    _exec_ignore(db, 'CREATE UNIQUE INDEX IF NOT EXISTS uq_ctas_prio_plan ON ctas_priority_fees(plan_id, position)')

    # Recurring-payment mandates. Stores ONLY the payment provider's reusable
    # token plus masked display details — never card numbers (PCI). The consent
    # fields are the member's authorisation record.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS ctas_mandates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            subscription_id INTEGER,
            provider TEXT DEFAULT 'paystack',
            mandate_type TEXT DEFAULT 'card',
            authorization_code TEXT,
            masked_label TEXT,
            bank_name TEXT,
            card_type TEXT,
            exp_month TEXT,
            exp_year TEXT,
            email TEXT,
            amount_cap REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',
            consent_text TEXT,
            consent_signature TEXT,
            consent_ip TEXT,
            consented_at TIMESTAMP,
            cancelled_at TIMESTAMP,
            last_charged_at TIMESTAMP,
            fail_count INTEGER DEFAULT 0,
            last_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (member_id) REFERENCES members (id),
            FOREIGN KEY (subscription_id) REFERENCES ctas_subscriptions (id)
        )
    '''))
    # The gateway reference used during setup is kept separate from the stored
    # token, so the token column can hold an ENCRYPTED value (see crypto.py).
    _add_col(db, 'ctas_mandates', 'setup_reference', 'TEXT')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_ctas_mandates_ref ON ctas_mandates(setup_reference)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_ctas_mandates_member ON ctas_mandates(member_id, status)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_ctas_mandates_sub ON ctas_mandates(subscription_id, status)')

    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS ctas_exceptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL,
            case_type TEXT DEFAULT 'missed_deduction',
            status TEXT DEFAULT 'open',
            month_number INTEGER,
            amount REAL DEFAULT 0,
            description TEXT,
            resolution_note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP,
            resolved_by INTEGER,
            FOREIGN KEY (subscription_id) REFERENCES ctas_subscriptions (id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_ctas_exceptions_status ON ctas_exceptions(status, case_type)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_ctas_exceptions_sub ON ctas_exceptions(subscription_id)')

    # Member communication campaigns and delivery attempts.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS communication_campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            audience TEXT NOT NULL,
            channel TEXT DEFAULT 'email',
            subject TEXT,
            body TEXT NOT NULL,
            status TEXT DEFAULT 'draft',
            recipient_count INTEGER DEFAULT 0,
            sent_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            skipped_count INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP,
            FOREIGN KEY (created_by) REFERENCES users (id)
        )
    '''))
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS communication_recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            member_id INTEGER,
            channel TEXT DEFAULT 'email',
            destination TEXT,
            status TEXT DEFAULT 'pending',
            error TEXT,
            sent_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (campaign_id) REFERENCES communication_campaigns (id),
            FOREIGN KEY (member_id) REFERENCES members (id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_comm_recip_campaign ON communication_recipients(campaign_id)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_comm_campaign_created ON communication_campaigns(created_at)')

    # Product marketing leads captured from the public CoopMS website.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS marketing_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            society_name TEXT NOT NULL,
            society_type TEXT,
            member_count TEXT,
            current_system TEXT,
            priority TEXT,
            message TEXT,
            status TEXT DEFAULT 'new',
            consent_accepted INTEGER DEFAULT 0,
            utm_source TEXT,
            utm_medium TEXT,
            utm_campaign TEXT,
            utm_term TEXT,
            utm_content TEXT,
            referrer TEXT,
            landing_page TEXT,
            ip_address TEXT,
            user_agent TEXT,
            crm_sync_status TEXT DEFAULT 'not_synced',
            crm_external_id TEXT,
            lead_score INTEGER DEFAULT 0,
            lead_temperature TEXT DEFAULT 'cold',
            score_reason TEXT,
            confirmation_sent_at TIMESTAMP,
            confirmation_status TEXT DEFAULT 'not_attempted',
            confirmation_provider TEXT,
            confirmation_error TEXT,
            internal_alert_sent_at TIMESTAMP,
            internal_alert_status TEXT DEFAULT 'not_attempted',
            internal_alert_provider TEXT,
            internal_alert_error TEXT,
            notes TEXT,
            assigned_to INTEGER,
            next_follow_up_at TIMESTAMP,
            last_activity_at TIMESTAMP,
            demo_scheduled_at TIMESTAMP,
            demo_meeting_link TEXT,
            demo_presenter TEXT,
            demo_outcome TEXT,
            proposed_plan TEXT,
            proposal_status TEXT,
            setup_fee REAL DEFAULT 0,
            monthly_subscription REAL DEFAULT 0,
            expected_close_date DATE,
            decision_reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (assigned_to) REFERENCES users (id)
        )
    '''))
    _add_col(db, 'marketing_leads', 'lead_score', 'INTEGER DEFAULT 0')
    # Who introduced a lead. The code is kept exactly as it arrived as well as
    # the affiliate it resolved to, so a mistyped or unknown code can be seen
    # and fixed rather than silently dropped.
    _add_col(db, 'marketing_leads', 'affiliate_code', 'TEXT')
    _add_col(db, 'marketing_leads', 'affiliate_id', 'INTEGER')
    # The billing client this lead became — the missing link between the CRM and
    # billing, and what ties "who introduced this coop" to "this coop paid".
    _add_col(db, 'marketing_leads', 'client_id', 'INTEGER')
    _add_col(db, 'marketing_leads', 'lead_temperature', "TEXT DEFAULT 'cold'")
    _add_col(db, 'marketing_leads', 'score_reason', 'TEXT')
    _add_col(db, 'marketing_leads', 'confirmation_sent_at', 'TIMESTAMP')
    _add_col(db, 'marketing_leads', 'confirmation_status', "TEXT DEFAULT 'not_attempted'")
    _add_col(db, 'marketing_leads', 'confirmation_provider', 'TEXT')
    _add_col(db, 'marketing_leads', 'confirmation_error', 'TEXT')
    _add_col(db, 'marketing_leads', 'internal_alert_sent_at', 'TIMESTAMP')
    _add_col(db, 'marketing_leads', 'internal_alert_status', "TEXT DEFAULT 'not_attempted'")
    _add_col(db, 'marketing_leads', 'internal_alert_provider', 'TEXT')
    _add_col(db, 'marketing_leads', 'internal_alert_error', 'TEXT')
    _add_col(db, 'marketing_leads', 'assigned_to', 'INTEGER')
    _add_col(db, 'marketing_leads', 'next_follow_up_at', 'TIMESTAMP')
    _add_col(db, 'marketing_leads', 'last_activity_at', 'TIMESTAMP')
    _add_col(db, 'marketing_leads', 'demo_scheduled_at', 'TIMESTAMP')
    _add_col(db, 'marketing_leads', 'demo_meeting_link', 'TEXT')
    _add_col(db, 'marketing_leads', 'demo_presenter', 'TEXT')
    _add_col(db, 'marketing_leads', 'demo_outcome', 'TEXT')
    _add_col(db, 'marketing_leads', 'proposed_plan', 'TEXT')
    _add_col(db, 'marketing_leads', 'proposal_status', 'TEXT')
    _add_col(db, 'marketing_leads', 'setup_fee', 'REAL DEFAULT 0')
    _add_col(db, 'marketing_leads', 'monthly_subscription', 'REAL DEFAULT 0')
    _add_col(db, 'marketing_leads', 'expected_close_date', 'DATE')
    _add_col(db, 'marketing_leads', 'decision_reason', 'TEXT')
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS marketing_lead_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            description TEXT,
            actor_user_id INTEGER,
            actor_username TEXT,
            data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (lead_id) REFERENCES marketing_leads (id),
            FOREIGN KEY (actor_user_id) REFERENCES users (id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_marketing_leads_status ON marketing_leads(status)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_marketing_leads_created ON marketing_leads(created_at)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_marketing_leads_email ON marketing_leads(email)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_marketing_lead_events_lead ON marketing_lead_events(lead_id)')

    # Pending payments table
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS pending_payments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            reference       TEXT UNIQUE NOT NULL,
            member_id       INTEGER NOT NULL,
            payment_type    TEXT NOT NULL,
            related_id      INTEGER,
            amount          REAL NOT NULL,
            month           TEXT,
            gateway         TEXT NOT NULL,
            status          TEXT DEFAULT 'pending',
            gateway_ref     TEXT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at    TIMESTAMP,
            FOREIGN KEY (member_id) REFERENCES members (id)
        )
    '''))

    # ── Task assignment (who may do what) ─────────────────────────────────────
    # Role defaults an admin has changed, and per-officer overrides on top of
    # them. Anything absent falls back to permissions.PERMISSIONS defaults, so
    # a fresh database behaves exactly like the old hard-coded role checks.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS role_permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            permission TEXT NOT NULL,
            allowed INTEGER DEFAULT 1,
            updated_by INTEGER,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    '''))
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS user_permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            permission TEXT NOT NULL,
            allowed INTEGER DEFAULT 1,
            granted_by INTEGER,
            granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    '''))
    _exec_ignore(db, 'CREATE UNIQUE INDEX IF NOT EXISTS uq_role_permissions ON role_permissions(role, permission)')
    _exec_ignore(db, 'CREATE UNIQUE INDEX IF NOT EXISTS uq_user_permissions ON user_permissions(user_id, permission)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_user_permissions_user ON user_permissions(user_id)')

    # Audit log table
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            action TEXT NOT NULL,
            module TEXT,
            description TEXT,
            ip_address TEXT,
            user_agent TEXT,
            data TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    '''))

    # Create journal headers before vouchers, which reference them on PostgreSQL.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS journal_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_number TEXT UNIQUE,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            description TEXT,
            reference TEXT,
            source_module TEXT,
            source_id INTEGER,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    '''))

    # Unified receipt, payment and journal vouchers. A submission token makes
    # retrying the same form safe without duplicating its financial effects.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS vouchers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_number TEXT UNIQUE NOT NULL,
            submission_token TEXT UNIQUE NOT NULL,
            kind TEXT NOT NULL,
            party TEXT,
            description TEXT NOT NULL,
            reference TEXT,
            date TIMESTAMP NOT NULL,
            journal_entry_id INTEGER,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (journal_entry_id) REFERENCES journal_entries(id),
            FOREIGN KEY (created_by) REFERENCES users(id)
        )
    '''))
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS voucher_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_id INTEGER NOT NULL,
            target TEXT NOT NULL,
            target_id INTEGER NOT NULL,
            FOREIGN KEY (voucher_id) REFERENCES vouchers(id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_voucher_targets ON voucher_allocations(target, target_id)')

    # One-time account setup / onboarding tokens.
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS account_setup_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT UNIQUE NOT NULL,
            purpose TEXT DEFAULT 'member_onboarding',
            expires_at TIMESTAMP NOT NULL,
            used_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_account_setup_tokens_user ON account_setup_tokens(user_id)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_account_setup_tokens_hash ON account_setup_tokens(token_hash)')

    # Events / announcements (AGM, meetings) shown on the members' banner
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            event_type TEXT DEFAULT 'announcement',
            event_date TIMESTAMP,
            location TEXT,
            meeting_link TEXT,
            description TEXT,
            is_active INTEGER DEFAULT 1,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    '''))
    # Virtual-meeting link (guard for databases created before this column existed)
    _add_col(db, 'events', 'meeting_link', 'TEXT')
    # Minutes of meeting repository — file stored in the DB so it survives
    # redeploys on platforms with an ephemeral filesystem (e.g. Railway).
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS meeting_minutes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            meeting_type TEXT DEFAULT 'general',
            meeting_date DATE,
            file_name TEXT,
            file_mime TEXT,
            file_data BLOB,
            notes TEXT,
            uploaded_by INTEGER,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    '''))
    # Meeting details + RSVP/attendance (added incrementally; guarded for old DBs).
    _add_col(db, 'events', 'start_time', 'TEXT')
    _add_col(db, 'events', 'end_time', 'TEXT')
    _add_col(db, 'events', 'agenda', 'TEXT')
    _add_col(db, 'events', 'reminder_sent_at', 'TIMESTAMP')
    _add_col(db, 'meeting_minutes', 'event_id', 'INTEGER')
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS event_rsvps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            member_id INTEGER NOT NULL,
            response TEXT DEFAULT 'attending',
            attended INTEGER DEFAULT 0,
            responded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (event_id) REFERENCES events (id),
            FOREIGN KEY (member_id) REFERENCES members (id)
        )
    '''))
    _exec_ignore(db, 'CREATE UNIQUE INDEX IF NOT EXISTS uq_event_rsvp ON event_rsvps(event_id, member_id)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_event_rsvps_event ON event_rsvps(event_id)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date)')

    # Member requests to change their monthly savings amount (staff-approved)
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS savings_change_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            current_amount REAL DEFAULT 0,
            requested_amount REAL NOT NULL,
            reason TEXT,
            status TEXT DEFAULT 'pending',
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_by INTEGER,
            reviewed_by_name TEXT,
            reviewed_at TIMESTAMP,
            review_comment TEXT,
            FOREIGN KEY (member_id) REFERENCES members (id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_savings_change_status ON savings_change_requests(status)')

    # ── Double-entry general ledger ────────────────────────────────────────────
    # Chart of accounts
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL,             -- asset | liability | equity | income | expense
            normal_balance TEXT NOT NULL,   -- debit | credit
            parent_code TEXT,
            is_active INTEGER DEFAULT 1,
            description TEXT
        )
    '''))
    # Journal entry lines (debits and credits)
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS journal_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL,
            account_code TEXT NOT NULL,
            debit REAL DEFAULT 0,
            credit REAL DEFAULT 0,
            memo TEXT,
            FOREIGN KEY (entry_id) REFERENCES journal_entries (id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_journal_lines_entry ON journal_lines(entry_id)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_journal_lines_account ON journal_lines(account_code)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_journal_entries_date ON journal_entries(date)')
    # Reversal linkage: reversal_of points from a reversal entry to the original;
    # reversed_at is set on an original once it has been reversed.
    _add_col(db, 'journal_entries', 'reversal_of', 'INTEGER')
    _add_col(db, 'journal_entries', 'reversed_at', 'TIMESTAMP')
    # Why the undo happened, kept on the reversal entry.
    _add_col(db, 'journal_entries', 'reversal_reason', 'TEXT')

    # Dividend declarations (year-end surplus distribution)
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS dividend_declarations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_from TEXT NOT NULL,
            period_to TEXT NOT NULL,
            net_surplus REAL NOT NULL,
            reserve_amount REAL DEFAULT 0,
            honorarium_amount REAL DEFAULT 0,
            other_amount REAL DEFAULT 0,
            dividend_pool REAL DEFAULT 0,
            patronage_split REAL DEFAULT 0,
            status TEXT DEFAULT 'declared',
            journal_entry_id INTEGER,
            declared_by INTEGER,
            declared_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    '''))
    # Per-member dividend allocations for a declaration
    db.execute(_adapt('''
        CREATE TABLE IF NOT EXISTS dividend_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            declaration_id INTEGER NOT NULL,
            member_id INTEGER NOT NULL,
            savings_base REAL DEFAULT 0,
            patronage_base REAL DEFAULT 0,
            dividend_savings REAL DEFAULT 0,
            dividend_patronage REAL DEFAULT 0,
            total REAL DEFAULT 0,
            FOREIGN KEY (declaration_id) REFERENCES dividend_declarations (id),
            FOREIGN KEY (member_id) REFERENCES members (id)
        )
    '''))
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_div_alloc_declaration ON dividend_allocations(declaration_id)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_div_alloc_member ON dividend_allocations(member_id)')

    # Seed the default cooperative chart of accounts (idempotent)
    default_accounts = [
        ('1000', 'Cash & Bank',                'asset',     'debit',  None),
        ('1100', 'Loans Receivable',           'asset',     'debit',  None),
        ('1200', 'Investments',                'asset',     'debit',  None),
        ('2000', 'Member Deposits (Savings)',  'liability', 'credit', None),
        ('2110', 'Insurance Payable',          'liability', 'credit', None),
        ('3000', 'Accumulated Surplus',        'equity',    'credit', None),
        ('3100', 'Statutory Reserve',          'equity',    'credit', None),
        ('3200', 'Member Share Capital',       'equity',    'credit', None),
        ('4000', 'Loan Interest Income',       'income',    'credit', None),
        ('4100', 'Fee Income',                 'income',    'credit', None),
        ('4200', 'Investment Income',          'income',    'credit', None),
        ('5000', 'Operating Expenses',         'expense',   'debit',  None),
        ('5100', 'Honorarium',                 'expense',   'debit',  None),
    ]
    for code, name, atype, normal, parent in default_accounts:
        try:
            db.execute('''
                INSERT INTO accounts (code, name, type, normal_balance, parent_code)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(code) DO NOTHING
            ''', (code, name, atype, normal, parent))
        except Exception as e:
            print(f"Error seeding account {code}: {e}")

    # Which accounts money actually moves through, so receipts and payments can
    # be recorded against the right one. Guessing this from an account's name
    # got it wrong both ways: it missed control accounts like a Cooperative Fund
    # Account (salary deductions the employer holds before remitting), and it
    # would have offered anything a coop happened to call a "Building Fund".
    _add_col(db, 'accounts', 'is_cash_account', 'INTEGER DEFAULT 0')

    # Backfill once, from the old name-matching rule, so existing installs keep
    # exactly the accounts they had. Guarded by a settings flag rather than
    # re-run on every boot: init_db runs on every container start, and without
    # the guard an account the treasurer deliberately unticked would come back.
    already = db.execute(
        "SELECT value FROM settings WHERE key = 'accounts_cash_flag_backfilled'"
    ).fetchone()
    if not already:
        db.execute('''
            UPDATE accounts SET is_cash_account = 1
            WHERE type = 'asset'
              AND (code = '1000' OR parent_code = '1000'
                   OR LOWER(name) LIKE '%bank%'
                   OR LOWER(name) LIKE '%cash%'
                   OR LOWER(name) LIKE '%wallet%')
        ''')
        # ON CONFLICT because init_db runs at import in every gunicorn worker:
        # two starting together would otherwise race on this unique key.
        db.execute('''
            INSERT INTO settings (key, value, description) VALUES (?, ?, ?)
            ON CONFLICT(key) DO NOTHING
        ''', ('accounts_cash_flag_backfilled', '1',
              'Set once when is_cash_account was seeded from the old name-matching rule'))

    # Lookup indexes for the most frequent auth, member, ledger, and payment paths.
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_members_email ON members(email)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_savings_member_month ON savings(member_id, month)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_loans_member_status ON loans(member_id, status)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_repayments_loan ON repayments(loan_id)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_repayments_reference ON repayments(reference)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_pending_payments_member_status ON pending_payments(member_id, status)')
    _exec_ignore(db, 'CREATE INDEX IF NOT EXISTS idx_notifications_user_read ON notifications(user_id, is_read)')
    # Idempotency guards for financial document references. Blank/NULL values
    # remain allowed for legacy rows, but any real business reference can only
    # appear once.
    _exec_ignore(db, "CREATE UNIQUE INDEX IF NOT EXISTS uq_savings_receipt_number ON savings(receipt_number) WHERE receipt_number IS NOT NULL AND receipt_number != ''")
    _exec_ignore(db, "CREATE UNIQUE INDEX IF NOT EXISTS uq_savings_reference ON savings(reference) WHERE reference IS NOT NULL AND reference != ''")
    _exec_ignore(db, "CREATE UNIQUE INDEX IF NOT EXISTS uq_repayments_reference ON repayments(reference) WHERE reference IS NOT NULL AND reference != ''")
    _exec_ignore(db, "CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_entries_reference ON journal_entries(reference) WHERE reference IS NOT NULL AND reference != ''")

    # ── Default settings ───────────────────────────────────────────────────────
    default_settings = [
        ('coop_name',       'Your Cooperative', 'Cooperative full name'),
        ('coop_short_name', 'Coop',             'Short name shown in sidebar and reports'),
        ('coop_logo',       '',                 'Logo path relative to static/ (e.g. uploads/logo.png)'),
        ('member_prefix',   'MEM',              'Prefix for auto-generated member numbers, e.g. MEM/2026/0001'),
        ('reg_number', 'CMS/2005/001', 'Registration number'),
        ('address', '', 'Cooperative address'),
        ('phone', '', 'Contact phone'),
        ('email', '', 'Contact email'),
        ('fy_start', '1', 'Financial year start month'),
        ('currency', 'NGN', 'Currency'),
        ('date_format', 'Y-m-d', 'Date format'),
        ('session_timeout', '30', 'Session timeout in minutes'),
        ('password_min_length', '8', 'Minimum password length'),
        ('password_require_upper', '1', 'Require uppercase letters in passwords'),
        ('password_require_lower', '1', 'Require lowercase letters in passwords'),
        ('password_require_number', '1', 'Require numbers in passwords'),
        ('password_require_special', '0', 'Require special characters in passwords'),
        ('maintenance_mode', '0', 'Maintenance mode'),
        ('require_2fa', '0', 'Require staff (admin, treasurer, secretary, exco) to set up two-factor authentication before using the system'),
        ('min_savings', '5000', 'Minimum monthly savings'),
        ('share_capital_pct', '0', 'Percent of each savings contribution allocated to member share capital (0 = off)'),
        ('savings_due_day', '10', 'Savings due day of month'),
        ('late_fee_percent', '10', 'Late fee percentage'),
        ('min_deposit_period', '90', 'Minimum deposit period in days'),
        ('member_deposit_rate', '9', 'Member deposit interest rate'),
        ('nonmember_deposit_rate', '7', 'Non-member deposit interest rate'),
        ('dividend_rate', '50', 'Dividend rate percentage'),
        ('min_membership_months', '6', 'Minimum membership months for loan'),
        ('min_savings_for_loan', '50000', 'Minimum savings for loan'),
        ('loan_multiplier', '2', 'Loan multiplier of savings'),
        ('max_tenure_months', '18', 'Maximum loan tenure'),
        ('max_interest_rate', '11', 'Maximum loan interest rate'),
        ('insurance_rate', '1', 'Loan insurance premium rate'),
        ('guarantors_required', '2', 'Number of guarantors required'),
        ('default_penalty_rate', '20', 'Default penalty rate'),
        ('books_lock_date', '', 'Books locked through this date (YYYY-MM-DD); entries on/before are blocked'),
        ('interest_regular', '11', 'Regular loan interest rate'),
        ('interest_housing', '9', 'Housing loan interest rate'),
        ('interest_emergency', '10', 'Emergency loan interest rate'),
        ('interest_asset', '10', 'Asset loan interest rate'),
        ('interest_school_fees', '9', 'School Fees loan interest rate'),
        ('interest_method_regular', 'reducing_annual', 'Regular loan computation method'),
        ('interest_method_housing', 'reducing_annual', 'Housing loan computation method'),
        ('interest_method_emergency', 'reducing_annual', 'Emergency loan computation method'),
        ('interest_method_asset', 'reducing_annual', 'Asset loan computation method'),
        ('interest_method_school_fees', 'flat', 'School Fees loan computation method'),
        ('entrance_fee', '2000', 'Entrance fee'),
        ('reentry_fee', '5000', 'Re-entry fee'),
        ('loan_application_fee', '1000', 'Loan application fee'),
        ('statement_fee', '500', 'Statement request fee'),
        ('active_gateway',          'paystack',  'Active payment gateway: paystack or flutterwave'),
        ('paystack_public_key',     '',          'Paystack publishable key (pk_...)'),
        ('paystack_secret_key',     '',          'Paystack secret key (sk_...)'),
        ('flutterwave_public_key',  '',          'Flutterwave public key (FLWPUBK_...)'),
        ('flutterwave_secret_key',  '',          'Flutterwave secret key (FLWSECK_...)'),
        ('flutterwave_webhook_hash','',          'Flutterwave webhook verification hash'),
        # ── Subscription billing ──────────────────────────────────────────────
        ('subscription_expiry',       '',      'Subscription expiry date YYYY-MM-DD (blank = no billing)'),
        ('subscription_per_user_fee', '5000', 'Per-member annual subscription fee in Naira'),
        ('subscription_email',        '',      'Billing contact email for payment receipts'),
        # ── Email ─────────────────────────────────────────────────────────────
        ('mail_enabled',   '0',  'Enable outgoing email (1=yes, 0=no)'),
        ('resend_api_key', '',   'Resend API key (re_...) — leave blank to use SMTP instead'),
        ('mail_from',      '',   'Sender address shown in inbox, e.g. "Your Coop <noreply@yourdomain.com>"'),
        ('smtp_host',      '',   'SMTP server hostname, e.g. smtp-relay.brevo.com or smtp.gmail.com'),
        ('smtp_port',      '587','SMTP port (587 for TLS, 465 for SSL)'),
        ('smtp_user',      '',   'SMTP login username (your email address)'),
        ('smtp_pass',      '',   'SMTP login password or app password'),
        # ── Loan request alerts (see loan_alerts.py) ──────────────────────────
        ('loan_alert_enabled',        '1', 'Alert the exco automatically the moment a member submits a loan request'),
        ('loan_alert_attach_pdf',     '1', 'Attach the full loan application as a PDF to exco alert emails'),
        ('loan_alert_roles',          'admin,treasurer,secretary,exco',
         'Roles alerted on every new loan request (President=admin, Treasurer, General Secretary=secretary, Exco)'),
        ('loan_alert_extra_emails',   '',  'Extra addresses copied on every loan request alert (comma separated)'),
        ('loan_alert_sla_hours',      '24', 'Hours an approval stage may sit untouched before it is treated as overdue'),
        ('loan_alert_reminder_hours', '12', 'Minimum hours between repeat reminders on the same loan'),
        ('loan_alert_escalate_hours', '48', 'Hours before an untouched loan request is escalated to the President and all exco'),
        ('app_base_url',             '',   'Public URL of this system, e.g. https://coop.example.org — used for links in alert emails'),
    ]

    for key, value, desc in default_settings:
        try:
            db.execute('''
                INSERT INTO settings (key, value, description)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO NOTHING
            ''', (key, value, desc))
        except Exception as e:
            print(f"Error inserting setting {key}: {e}")

    # ── Seed / refresh default staff accounts ─────────────────────────────────
    existing_users = {
        row['username']
        for row in db.execute('SELECT username FROM users').fetchall()
    }

    seed_users = [
        ('admin',     os.environ.get('ADMIN_PASSWORD'),     'admin'),
        ('treasurer', os.environ.get('TREASURER_PASSWORD'), 'treasurer'),
        ('secretary', os.environ.get('SECRETARY_PASSWORD'), 'secretary'),
    ]

    for username, password, role in seed_users:
        if username in existing_users:
            continue
        if not password:
            print(f"  [auth] Skipped creating '{username}': {username.upper()}_PASSWORD is not set.")
            continue

        try:
            db.execute(
                'INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)',
                (username, generate_password_hash(password), role, datetime.now())
            )
            print(f"  [auth] Created user '{username}' with role '{role}'.")
        except Exception as e:
            print(f"Error creating user {username}: {e}")

    # Heal any savings rows whose deposit was reversed BEFORE reversed_at existed:
    # mark them reversed and free their receipt number, so a corrected batch can
    # be re-uploaded without tripping the duplicate check or the unique receipt
    # index. Idempotent (skips rows already suffixed).
    try:
        db.execute('''
            UPDATE savings SET reversed_at = CURRENT_TIMESTAMP,
                receipt_number = CASE
                    WHEN receipt_number IS NOT NULL AND receipt_number != ''
                    THEN receipt_number || '~REV' || CAST(id AS TEXT)
                    ELSE receipt_number END
            WHERE reversed_at IS NULL
              AND (receipt_number IS NULL OR receipt_number NOT LIKE '%~REV%')
              AND id IN (
                SELECT source_id FROM journal_entries
                WHERE source_module = 'savings_deposit' AND source_id IS NOT NULL
                  AND reversed_at IS NOT NULL)
        ''')
    except Exception as exc:
        print(f"[schema] skipped reversed-savings backfill: {exc}")

    backend = 'PostgreSQL' if USE_POSTGRES else 'SQLite'
    print(f"\n{'=' * 60}")
    print(f"  Backend    : {backend}")
    print("  auth       : default users are create-only; passwords are never printed")
    print(f"{'=' * 60}\n")

    db.commit()
    if USE_POSTGRES:
        db.execute('SELECT pg_advisory_unlock(2026072301)')
        db.commit()
    db.close()
    print(f"Database ({backend}) initialised successfully!")


if __name__ == '__main__':
    init_db()
