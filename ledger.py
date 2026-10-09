"""
ledger.py — double-entry general ledger: posting engine + reporting.

A journal entry is a set of lines whose debits equal its credits. Every
financial event (savings deposit, loan disbursement, repayment, fee, expense…)
posts one balanced entry, and all statements roll up from these entries.

Account codes are the stable interface (see the seeded chart of accounts in
database.py). Posting helpers never commit — they run inside the caller's
transaction so the journal entry is atomic with the operation it records.
"""

import secrets
from datetime import datetime

# Canonical account codes (mirror the seeded chart of accounts)
CASH            = '1000'
LOANS_RECEIVABLE = '1100'
CTAS_ADVANCES   = '1150'   # CTAS target-advance receivable (paid out, recovered via payroll)
INVESTMENTS     = '1200'
MEMBER_DEPOSITS = '2000'
INSURANCE_PAYABLE = '2110'   # loan insurance withheld on behalf of the insurer (pass-through)
ACCUM_SURPLUS   = '3000'
STATUTORY_RESERVE = '3100'
SHARE_CAPITAL   = '3200'
LOAN_INTEREST_INCOME = '4000'
FEE_INCOME      = '4100'
CTAS_ADMIN_FEE_INCOME = '4150'
INVESTMENT_INCOME = '4200'
OPERATING_EXPENSES = '5000'
HONORARIUM      = '5100'

OPERATIONAL_REVENUE_CATEGORIES = {
    'Late Fee',
    'Loan Insurance',
    'Loan Application Fee',
}


# ── Period close / books-lock date ────────────────────────────────────────────

class PeriodLockedError(ValueError):
    """Raised when a journal entry is dated on or before the books-lock date.

    Subclasses ValueError so existing `except ValueError` posting handlers show
    the message to the user instead of 500-ing.
    """


def _date_str(date):
    """Normalise a date/datetime/string/None to a 'YYYY-MM-DD' string."""
    if date is None:
        return datetime.now().strftime('%Y-%m-%d')
    if isinstance(date, datetime):
        return date.strftime('%Y-%m-%d')
    return str(date)[:10]


def opening_balance_date(db):
    """The date of the opening-balance entry ('YYYY-MM-DD'), or None if the
    books were never migrated.

    Anything dated on or before this is already represented inside the opening
    entry, because that entry IS the position at that date.
    """
    try:
        row = db.execute(
            "SELECT MAX(date) AS d FROM journal_entries WHERE source_module = 'opening'"
        ).fetchone()
        return _date_str(row['d']) if row and row['d'] else None
    except Exception:
        return None


def get_lock_date(db):
    """The date the books are locked through ('YYYY-MM-DD'), or None if unlocked."""
    try:
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'books_lock_date'").fetchone()
        v = (row['value'] if row else '') or ''
        return v.strip() or None
    except Exception:
        return None


def date_is_locked(db, date):
    """True if `date` falls on or before the current books-lock date."""
    lock = get_lock_date(db)
    return bool(lock) and _date_str(date) <= lock


def get_accounts(db, active_only=True):
    sql = 'SELECT code, name, type, normal_balance, parent_code, is_active FROM accounts'
    if active_only:
        sql += ' WHERE is_active = 1'
    sql += " ORDER BY code"
    return db.execute(sql).fetchall()


def get_default_cash_account(db):
    """Return the configured cash/bank posting account, falling back to 1000.

    The selected account must be an active asset account. This allows a coop to
    post receipts/disbursements to a bank detail account like 1010 while keeping
    legacy installs safe on the seeded Cash & Bank account.
    """
    try:
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'default_cash_account'"
        ).fetchone()
        code = (row['value'] if row else '') or CASH
        account = db.execute(
            "SELECT code FROM accounts WHERE code = ? AND type = 'asset' AND is_active = 1",
            (code,)
        ).fetchone()
        return account['code'] if account else CASH
    except Exception:
        return CASH


def get_cash_bank_accounts(db):
    """The accounts money moves through — banks, cash in hand, and control
    accounts that hold money on its way to a bank.

    Marked per account (`is_cash_account`) rather than inferred from the name.
    A cooperative's salary deductions sit in a control account until the
    employer remits them; nothing in the words "Cooperative Fund Account" says
    so, and a name rule would equally have offered a "Building Fund" that holds
    no money at all. The treasurer says which accounts these are.
    """
    rows = db.execute('''
        SELECT code, name, type, normal_balance, parent_code, is_active
        FROM accounts
        WHERE is_active = 1 AND is_cash_account = 1
        ORDER BY
          CASE WHEN parent_code IS NOT NULL AND parent_code != '' THEN 0 ELSE 1 END,
          code
    ''').fetchall()
    return [dict(r) for r in rows]


def get_postable_cash_accounts(db):
    """The cash/bank accounts an entry may actually be posted to.

    A header that has detail accounts under it is left out. The bank report adds
    a parent and its children into one total, so money posted on the parent gets
    counted twice — once on its own row and again inside the children's total.
    A cooperative that splits Cash & Bank into 1010 Cash, 1020 FCMB and 1030 UBA
    means 1000 to be a heading, not a destination.

    An account with nothing under it stays postable, which is how every install
    starts and how simple ones remain.
    """
    headers = {r['parent_code'] for r in db.execute(
        "SELECT DISTINCT parent_code FROM accounts "
        "WHERE is_active = 1 AND parent_code IS NOT NULL AND parent_code != ''"
    ).fetchall()}
    return [a for a in get_cash_bank_accounts(db) if a['code'] not in headers]


class UnknownCashAccountError(ValueError):
    """A posting was aimed at an account that is not a cash/bank account.

    Subclasses ValueError so existing `except ValueError` posting handlers keep
    working.
    """


def resolve_cash_bank_account(db, requested_code=None):
    """Validate a chosen cash/bank account, or fall back to the default when
    nothing was chosen.

    An empty choice means "use the default". A code that is not a cash/bank
    account raises, and is never quietly swapped for the default: this function
    decides which account the money is recorded as sitting in, so substituting a
    different one would post the entry somewhere the officer did not pick and
    leave that account's reconciliation wrong with nothing on screen to say so.
    A treasurer typing 1400 into a bulk upload should be told 1400 is not a bank
    account, not have the whole batch land in the default bank.
    """
    code = (requested_code or '').strip()
    if not code:
        return get_default_cash_account(db)
    if code not in {a['code'] for a in get_postable_cash_accounts(db)}:
        raise UnknownCashAccountError(
            f"'{code}' is not an account money can be posted to. Pick one of the "
            f"cash/bank accounts, or leave it blank to use the default. "
            f"(An account with detail accounts under it is a heading — post to "
            f"one of those instead.)"
        )
    return code


def get_payroll_counter_accounts(db):
    """Cash destinations plus the cooperative fund payroll control account."""
    accounts = get_postable_cash_accounts(db)
    control = db.execute('''
        SELECT code, name, type, normal_balance, parent_code, is_active
        FROM accounts WHERE code = '1400' AND type = 'asset' AND is_active = 1
        AND NOT EXISTS (
            SELECT 1 FROM accounts child
            WHERE child.parent_code = '1400' AND child.is_active = 1
        )
    ''').fetchone()
    if control and not any(a['code'] == '1400' for a in accounts):
        accounts.append(dict(control))
    return accounts


def resolve_payroll_counter_account(db, counter_account=None, bank_account=None):
    """An explicit counter account supports non-bank payroll deductions.

    Keep legacy bank_account validation intact; never mark the control account
    as cash or change receiving accounts in other transaction workflows.
    """
    code = (counter_account or '').strip()
    if not code:
        return resolve_cash_bank_account(db, bank_account)
    if code not in {a['code'] for a in get_payroll_counter_accounts(db)}:
        raise UnknownCashAccountError(
            f"'{code}' is not an active postable payroll counter account. "
            "Choose a cash/bank account or cooperative fund control account 1400."
        )
    return code


def get_loan_repayment_counter_accounts(db):
    return get_payroll_counter_accounts(db)


def resolve_loan_repayment_counter_account(db, counter_account=None, bank_account=None):
    return resolve_payroll_counter_account(db, counter_account, bank_account)


def account_exists(db, code):
    return db.execute('SELECT 1 FROM accounts WHERE code = ?', (code,)).fetchone() is not None


def post_journal(db, description, lines, date=None, reference='',
                 source_module='', source_id=None, created_by=None):
    """Post a balanced double-entry journal entry.

    lines: iterable of dicts, e.g.
        [{'account': '1000', 'debit': 5000, 'memo': 'cash in'},
         {'account': '2000', 'credit': 5000}]

    Validates that debits == credits, that no line is both debit and credit,
    and that amounts are non-negative. Does NOT commit. Returns the new
    journal_entries.id, or None if there was nothing to post.
    Raises ValueError on an unbalanced or invalid entry.
    """
    norm = []
    total_debit = total_credit = 0.0
    for ln in lines:
        code   = ln['account']
        debit  = round(float(ln.get('debit', 0) or 0), 2)
        credit = round(float(ln.get('credit', 0) or 0), 2)
        if debit < 0 or credit < 0:
            raise ValueError('journal line debit/credit cannot be negative')
        if debit and credit:
            raise ValueError('a journal line cannot have both a debit and a credit')
        if not debit and not credit:
            continue  # skip zero lines
        norm.append((code, debit, credit, ln.get('memo', '')))
        total_debit  += debit
        total_credit += credit

    if not norm:
        return None
    if round(total_debit, 2) != round(total_credit, 2):
        raise ValueError(
            f'unbalanced journal entry: debits {total_debit:.2f} != credits {total_credit:.2f}'
        )

    # Period close: refuse to post into a locked period.
    lock = get_lock_date(db)
    if lock and _date_str(date) <= lock:
        raise PeriodLockedError(
            f'The books are locked through {lock}. Choose a later date, '
            f'or an admin can move the lock date under Accounting → Period Close.'
        )

    entry_number = f"JE-{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"
    db.execute(
        '''INSERT INTO journal_entries
           (entry_number, date, description, reference, source_module, source_id, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (entry_number, date or datetime.now(), description, reference,
         source_module, source_id, created_by, datetime.now())
    )
    from database import last_insert_id
    entry_id = last_insert_id(db)
    for code, debit, credit, memo in norm:
        db.execute(
            'INSERT INTO journal_lines (entry_id, account_code, debit, credit, memo) VALUES (?, ?, ?, ?, ?)',
            (entry_id, code, debit, credit, memo)
        )
    return entry_id


def post_journal_safe(db, *args, **kwargs):
    """Best-effort post_journal that never raises and cannot poison the caller's
    transaction. Use from within member-facing flows so a ledger problem can't
    break recording a payment. Returns the entry id, or None on failure.

    On PostgreSQL the work is wrapped in a SAVEPOINT so a failed insert does not
    abort the outer transaction (mirrors utils.record_revenue).
    """
    from database import USE_POSTGRES
    if USE_POSTGRES:
        try:
            db.execute('SAVEPOINT sp_journal')
            entry_id = post_journal(db, *args, **kwargs)
            db.execute('RELEASE SAVEPOINT sp_journal')
            return entry_id
        except Exception as exc:
            try:
                db.execute('ROLLBACK TO SAVEPOINT sp_journal')
            except Exception:
                pass
            print(f"[ledger] failed to post journal: {exc}")
            return None
    else:
        try:
            return post_journal(db, *args, **kwargs)
        except Exception as exc:
            print(f"[ledger] failed to post journal: {exc}")
            return None


def _je_exists_ref(db, reference):
    """True if a journal entry already exists for this (non-empty) reference."""
    if not reference:
        return False
    return db.execute(
        'SELECT 1 FROM journal_entries WHERE reference = ?', (reference,)
    ).fetchone() is not None


def _sample_missing_by_ref(db, label, sql, sample_limit):
    rows = db.execute(sql).fetchall()
    missing = []
    for r in rows:
        ref = r['ref']
        if not _je_exists_ref(db, ref):
            missing.append(r)
    return {
        'label': label,
        'total': len(rows),
        'missing': len(missing),
        'samples': missing[:sample_limit],
    }


def ledger_reconciliation(db, sample_limit=10):
    """Return operational records that have not yet been posted to the ledger.

    This is intentionally conservative: a record is considered posted when its
    stable business reference appears on a journal entry. Honorarium has no
    business reference, so it is matched by source_module/source_id.
    """
    sample_limit = max(1, int(sample_limit or 10))
    sections = []

    sections.append(_sample_missing_by_ref(db, 'Savings deposits', '''
        SELECT s.id, s.member_id, m.member_number,
               m.first_name || ' ' || m.last_name AS member_name,
               COALESCE(NULLIF(s.receipt_number, ''), NULLIF(s.reference, ''), 'SAV-' || CAST(s.id AS TEXT)) AS ref,
               s.date, s.amount, s.month
        FROM savings s
        JOIN members m ON m.id = s.member_id
        WHERE COALESCE(s.payment_type, '') NOT IN ('dividend', 'reversal')
          AND s.reversed_at IS NULL
        ORDER BY s.date DESC, s.id DESC
    ''', sample_limit))
    # 'reversal' rows and rows already reversed are excluded deliberately: their
    # ledger effect is the reversal entry, which is linked to the ORIGINAL entry
    # rather than carrying a reference of its own. Counting them here reported a
    # correctly-posted reversal as an unposted savings deposit.

    sections.append(_sample_missing_by_ref(db, 'Loan disbursements', '''
        SELECT l.id, l.member_id, m.member_number,
               m.first_name || ' ' || m.last_name AS member_name,
               l.loan_number AS ref, COALESCE(l.disbursement_date, l.date_applied) AS date,
               l.amount, l.purpose AS month
        FROM loans l
        JOIN members m ON m.id = l.member_id
        WHERE l.status IN ('active', 'completed') AND l.loan_number IS NOT NULL
        ORDER BY COALESCE(l.disbursement_date, l.date_applied) DESC, l.id DESC
    ''', sample_limit))

    sections.append(_sample_missing_by_ref(db, 'Loan repayments', '''
        SELECT r.id, l.member_id, m.member_number,
               m.first_name || ' ' || m.last_name AS member_name,
               COALESCE(NULLIF(r.repayment_number, ''), NULLIF(r.reference, ''), 'REP-' || CAST(r.id AS TEXT)) AS ref,
               r.date, r.amount, l.loan_number AS month
        FROM repayments r
        JOIN loans l ON l.id = r.loan_id
        JOIN members m ON m.id = l.member_id
        ORDER BY r.date DESC, r.id DESC
    ''', sample_limit))

    sections.append(_sample_missing_by_ref(db, 'Expenses', '''
        SELECT id, NULL AS member_id, '' AS member_number, category AS member_name,
               COALESCE(NULLIF(expense_number, ''), 'EXP-' || CAST(id AS TEXT)) AS ref,
               date, amount, payment_method AS month
        FROM expenses
        ORDER BY date DESC, id DESC
    ''', sample_limit))

    sections.append(_sample_missing_by_ref(db, 'Revenue', '''
        SELECT id, NULL AS member_id, '' AS member_number, category AS member_name,
               COALESCE(NULLIF(revenue_number, ''), 'REV-' || CAST(id AS TEXT)) AS ref,
               date, amount, source AS month
        FROM revenue
        WHERE COALESCE(category, '') NOT IN ('Late Fee', 'Loan Insurance', 'Loan Application Fee')
        ORDER BY date DESC, id DESC
    ''', sample_limit))

    sections.append(_sample_missing_by_ref(db, 'Investments', '''
        SELECT id, NULL AS member_id, '' AS member_number, name AS member_name,
               COALESCE(NULLIF(investment_number, ''), 'INV-' || CAST(id AS TEXT)) AS ref,
               date, amount, type AS month
        FROM investments
        ORDER BY date DESC, id DESC
    ''', sample_limit))

    honorarium_rows = db.execute('''
        SELECT id, NULL AS member_id, '' AS member_number,
               COALESCE(recipient_name, '') AS member_name,
               'HON-' || CAST(id AS TEXT) AS ref, date, amount, month
        FROM honorarium
        ORDER BY date DESC, id DESC
    ''').fetchall()
    honorarium_missing = []
    for h in honorarium_rows:
        exists = db.execute(
            "SELECT 1 FROM journal_entries WHERE source_module = 'honorarium' AND source_id = ?",
            (h['id'],)
        ).fetchone()
        if not exists:
            honorarium_missing.append(h)
    sections.append({
        'label': 'Honorarium',
        'total': len(honorarium_rows),
        'missing': len(honorarium_missing),
        'samples': honorarium_missing[:sample_limit],
    })

    total_records = sum(s['total'] for s in sections)
    total_missing = sum(s['missing'] for s in sections)
    posted_entries = db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0] or 0
    posted_lines = db.execute('SELECT COUNT(*) FROM journal_lines').fetchone()[0] or 0
    return {
        'sections': sections,
        'total_records': total_records,
        'total_missing': total_missing,
        'posted_entries': posted_entries,
        'posted_lines': posted_lines,
        'complete': total_missing == 0,
    }


def backfill_from_transactions(db, created_by=None):
    """Post journal entries for existing transactions that don't have one yet.

    Idempotent: transactions already in the ledger (by their unique reference,
    or by source for honorarium) are skipped, so this is safe to run more than
    once and safe to run alongside live posting. Does NOT commit.

    Records dated on or before the opening-balance entry are skipped. A migrated
    cooperative imports its members' balances as subledger rows with NO journal
    entries, because the opening balance already states the position those rows
    add up to. Posting them again would count the same money twice and double
    the ledger, so migrated history is left alone and only activity after the
    cutover is posted.

    Returns (posted, skipped_pre_cutover).
    """
    from utils import split_repayment
    posted = 0
    skipped_pre_cutover = 0
    cash_account = get_default_cash_account(db)
    cutoff = opening_balance_date(db)

    def _covered_by_opening(date):
        """True if this record predates the cutover and is already in the books."""
        nonlocal skipped_pre_cutover
        if cutoff and _date_str(date) <= cutoff:
            skipped_pre_cutover += 1
            return True
        return False

    # Savings deposits
    for s in db.execute('SELECT * FROM savings').fetchall():
        # Dividend credits are posted as one aggregate entry by the dividend
        # engine — don't double-post them here.
        if (s['payment_type'] or '') == 'dividend':
            continue
        ref = s['receipt_number'] or f"SAV-{s['id']}"
        if _je_exists_ref(db, ref) or _covered_by_opening(s['date']):
            continue
        amount = float(s['amount'] or 0)
        late   = float(s['late_fee'] or 0)
        if amount + late <= 0:
            continue
        lines = [
            {'account': cash_account, 'debit': amount + late, 'memo': f"Savings {s['month']}"},
            {'account': MEMBER_DEPOSITS, 'credit': amount, 'memo': f"Member {s['member_id']}"},
        ]
        if late:
            lines.append({'account': FEE_INCOME, 'credit': late, 'memo': 'Late fee'})
        if post_journal_safe(db, f"Savings deposit — {s['month']}", lines,
                             date=s['date'], reference=ref, source_module='savings',
                             source_id=s['member_id'], created_by=created_by):
            posted += 1

    # Loan disbursements (active or completed loans)
    for l in db.execute("SELECT * FROM loans WHERE status IN ('active','completed')").fetchall():
        ref = l['loan_number']
        if _je_exists_ref(db, ref) or _covered_by_opening(
                l['disbursement_date'] or l['date_applied']):
            continue
        principal = float(l['amount'] or 0)
        if principal <= 0:
            continue
        ins  = float(l['insurance_premium'] or 0)
        appf = float(l['application_fee'] or 0)
        fees = ins + appf
        disbursed = l['disbursed_amount']
        disbursed = float(disbursed) if disbursed is not None else (principal - fees)
        lines = [{'account': LOANS_RECEIVABLE, 'debit': principal, 'memo': l['loan_number']}]
        if disbursed:
            lines.append({'account': cash_account, 'credit': disbursed, 'memo': 'Net disbursed'})
        if fees:
            lines.append({'account': FEE_INCOME, 'credit': fees, 'memo': 'Loan fees'})
        if post_journal_safe(db, f"Loan disbursement — {ref}", lines,
                             date=l['disbursement_date'] or l['date_applied'], reference=ref,
                             source_module='loans', source_id=l['id'], created_by=created_by):
            posted += 1

    # Loan repayments
    for r in db.execute('''SELECT r.*, l.amount AS principal, l.total_repayment, l.loan_number
                           FROM repayments r JOIN loans l ON l.id = r.loan_id''').fetchall():
        ref = r['repayment_number'] or f"REP-{r['id']}"
        if _je_exists_ref(db, ref) or _covered_by_opening(r['date']):
            continue
        amount = float(r['amount'] or 0)
        if amount <= 0:
            continue
        pp = float(r['principal_paid'] or 0)
        ip = float(r['interest_paid'] or 0)
        if pp == 0 and ip == 0:
            pp, ip = split_repayment(amount, r['principal'], r['total_repayment'])
        if post_journal_safe(db, f"Loan repayment — {r['loan_number']}", [
            {'account': cash_account, 'debit': amount, 'memo': 'Repayment'},
            {'account': LOANS_RECEIVABLE, 'credit': pp, 'memo': r['loan_number']},
            {'account': LOAN_INTEREST_INCOME, 'credit': ip, 'memo': 'Interest earned'},
        ], date=r['date'], reference=ref, source_module='loans',
           source_id=r['loan_id'], created_by=created_by):
            posted += 1

    # Expenses
    for e in db.execute('SELECT * FROM expenses').fetchall():
        ref = e['expense_number'] or f"EXP-{e['id']}"
        if _je_exists_ref(db, ref) or _covered_by_opening(e['date']):
            continue
        amt = float(e['amount'] or 0)
        if amt <= 0:
            continue
        if post_journal_safe(db, f"Expense — {e['category']}", [
            {'account': OPERATING_EXPENSES, 'debit': amt, 'memo': e['description'] or ''},
            {'account': cash_account, 'credit': amt},
        ], date=e['date'], reference=ref, source_module='expenses', created_by=created_by):
            posted += 1

    # Revenue
    for rv in db.execute('SELECT * FROM revenue').fetchall():
        if rv['category'] in OPERATIONAL_REVENUE_CATEGORIES:
            continue
        ref = rv['revenue_number'] or f"REV-{rv['id']}"
        if _je_exists_ref(db, ref) or _covered_by_opening(rv['date']):
            continue
        amt = float(rv['amount'] or 0)
        if amt <= 0:
            continue
        if post_journal_safe(db, f"Revenue — {rv['category']}", [
            {'account': cash_account, 'debit': amt},
            {'account': FEE_INCOME, 'credit': amt, 'memo': rv['description'] or ''},
        ], date=rv['date'], reference=ref, source_module='revenue', created_by=created_by):
            posted += 1

    # Honorarium (no unique reference — identify by source)
    for h in db.execute('SELECT * FROM honorarium').fetchall():
        exists = db.execute(
            "SELECT 1 FROM journal_entries WHERE source_module = 'honorarium' AND source_id = ?",
            (h['id'],)
        ).fetchone()
        if exists or _covered_by_opening(h['date']):
            continue
        amt = float(h['amount'] or 0)
        if amt <= 0:
            continue
        if post_journal_safe(db, f"Honorarium — {h['recipient_name'] or ''}", [
            {'account': HONORARIUM, 'debit': amt, 'memo': h['recipient_name'] or ''},
            {'account': cash_account, 'credit': amt},
        ], date=h['date'], source_module='honorarium', source_id=h['id'], created_by=created_by):
            posted += 1

    # Investments
    for iv in db.execute('SELECT * FROM investments').fetchall():
        ref = iv['investment_number'] or f"INV-{iv['id']}"
        if _je_exists_ref(db, ref) or _covered_by_opening(iv['date']):
            continue
        amt = float(iv['amount'] or 0)
        if amt <= 0:
            continue
        if post_journal_safe(db, f"Investment — {iv['name']}", [
            {'account': INVESTMENTS, 'debit': amt, 'memo': iv['name']},
            {'account': cash_account, 'credit': amt},
        ], date=iv['date'], reference=ref, source_module='investments', created_by=created_by):
            posted += 1

    return posted, skipped_pre_cutover


def trial_balance(db, as_of=None):
    """Return the trial balance as of a date (or all-time).

    Each account's net balance (debits - credits) is placed on the side where
    it is positive, so total debits always equal total credits.
    """
    date_filter = ''
    params = ()
    if as_of:
        date_filter = 'WHERE je.date <= ?'
        params = (f"{as_of} 23:59:59",)

    rows = db.execute(f'''
        SELECT a.code, a.name, a.type,
               COALESCE(SUM(x.debit), 0)  AS d,
               COALESCE(SUM(x.credit), 0) AS c
        FROM accounts a
        LEFT JOIN (
            SELECT jl.account_code, jl.debit, jl.credit
            FROM journal_lines jl
            JOIN journal_entries je ON je.id = jl.entry_id
            {date_filter}
        ) x ON x.account_code = a.code
        WHERE a.is_active = 1
        GROUP BY a.code, a.name, a.type
        ORDER BY a.code
    ''', params).fetchall()

    result = []
    total_debit = total_credit = 0.0
    for r in rows:
        net    = float(r['d']) - float(r['c'])
        debit  = net if net > 0 else 0.0
        credit = -net if net < 0 else 0.0
        total_debit  += debit
        total_credit += credit
        result.append({
            'code': r['code'], 'name': r['name'], 'type': r['type'],
            'debit': round(debit, 2), 'credit': round(credit, 2),
        })
    return {
        'rows': result,
        'total_debit': round(total_debit, 2),
        'total_credit': round(total_credit, 2),
        'balanced': abs(total_debit - total_credit) < 0.01,
    }


def account_ledger(db, code, from_date=None, to_date=None):
    """Full audit trail for one account: every journal line that touched it.

    Returns the opening balance (everything before *from_date*), each line in
    date order with a running balance, and the closing balance. Balances are
    signed to the account's normal side, so an asset/expense reads positive on
    debits and a liability/equity/income reads positive on credits.
    """
    acct = db.execute(
        'SELECT code, name, type, normal_balance FROM accounts WHERE code = ?', (code,)
    ).fetchone()
    if not acct:
        return None

    sign = -1.0 if (acct['normal_balance'] or '').lower() == 'credit' else 1.0

    # Opening balance = net movement strictly before the period start.
    opening = 0.0
    if from_date:
        row = db.execute('''
            SELECT COALESCE(SUM(jl.debit), 0) - COALESCE(SUM(jl.credit), 0) AS bal
            FROM journal_lines jl
            JOIN journal_entries je ON je.id = jl.entry_id
            WHERE jl.account_code = ? AND je.date < ?
        ''', (code, from_date)).fetchone()
        opening = float(row['bal'] or 0) if row else 0.0

    where = ['jl.account_code = ?']
    params = [code]
    if from_date:
        where.append('je.date >= ?')
        params.append(from_date)
    if to_date:
        where.append('je.date <= ?')
        params.append(f"{to_date} 23:59:59")

    rows = db.execute(f'''
        SELECT jl.id AS line_id, jl.debit, jl.credit, jl.memo,
               je.id AS entry_id, je.entry_number, je.date, je.description,
               je.reference, je.source_module, je.source_id
        FROM journal_lines jl
        JOIN journal_entries je ON je.id = jl.entry_id
        WHERE {' AND '.join(where)}
        ORDER BY je.date ASC, je.id ASC, jl.id ASC
    ''', tuple(params)).fetchall()

    running = opening
    entries = []
    total_debit = total_credit = 0.0
    for r in rows:
        debit  = float(r['debit'] or 0)
        credit = float(r['credit'] or 0)
        running += debit - credit
        total_debit  += debit
        total_credit += credit
        d = dict(r)
        d['debit']   = round(debit, 2)
        d['credit']  = round(credit, 2)
        d['balance'] = round(running * sign, 2)
        entries.append(d)

    return {
        'account': dict(acct),
        'entries': entries,
        'opening_balance': round(opening * sign, 2),
        'closing_balance': round(running * sign, 2),
        'total_debit': round(total_debit, 2),
        'total_credit': round(total_credit, 2),
        'count': len(entries),
    }


def journal_entry_detail(db, entry_id):
    """One journal entry with all of its debit/credit lines, for audit drill-down."""
    entry = db.execute('SELECT * FROM journal_entries WHERE id = ?', (entry_id,)).fetchone()
    if not entry:
        return None
    lines = db.execute('''
        SELECT jl.*, a.name AS account_name, a.type AS account_type
        FROM journal_lines jl
        LEFT JOIN accounts a ON a.code = jl.account_code
        WHERE jl.entry_id = ?
        ORDER BY jl.debit DESC, jl.id ASC
    ''', (entry_id,)).fetchall()

    total_debit  = sum(float(l['debit'] or 0) for l in lines)
    total_credit = sum(float(l['credit'] or 0) for l in lines)

    entry_d = dict(entry)
    # The reversal entry that cancelled this one (if any).
    rev = db.execute(
        'SELECT id, entry_number, date FROM journal_entries WHERE reversal_of = ?',
        (entry_id,)).fetchone()
    reversed_by = dict(rev) if rev else None
    # If this entry is itself a reversal, the original it cancelled.
    original = None
    if entry_d.get('reversal_of'):
        o = db.execute('SELECT id, entry_number, date FROM journal_entries WHERE id = ?',
                       (entry_d['reversal_of'],)).fetchone()
        original = dict(o) if o else None

    return {
        'entry': entry_d,
        'lines': [dict(l) for l in lines],
        'total_debit': round(total_debit, 2),
        'total_credit': round(total_credit, 2),
        'balanced': abs(total_debit - total_credit) < 0.01,
        'reversed_by': reversed_by,
        'original': original,
    }


class UnsupportedReversalError(ValueError):
    """The books could be corrected, but the records behind the entry could not.

    Raised rather than posting a half-undo: an entry whose operational record
    still says the opposite of the ledger is worse than one that was never
    reversed, because nothing on screen shows the two disagree.
    """


# Modules whose journal entry IS the whole record — there is no operational row
# behind it, so offsetting the ledger is a complete undo.
LEDGER_ONLY_MODULES = frozenset({'', 'manual', 'opening', 'adjustment'})

# Where the ones the journal cannot undo are undone instead, so a refusal tells
# the officer what to do rather than just saying no.
REVERSAL_GUIDANCE = {
    'ctas_payout':       'Settle or correct it from the CTAS cycle page.',
    'ctas_contribution': 'Correct it from the CTAS cycle page.',
    'ctas_recovery':     'Correct it from the CTAS cycle page.',
    'ctas_exit':         'Reopen it from the CTAS exceptions page.',
    'loan_disbursement': 'Correct it from the loan record.',
    'loans':             'Correct it from the loan record.',
    'dividend':          'Cancel the declaration from the dividends page.',
    'payments':          'Correct it from the payment record.',
    'savings':           'This came from the historical backfill, which links to a '
                         'member rather than one savings row, so it cannot be undone here.',
    'member_receipt':    'Reverse it from the member receipt screen.',
    'revenue':           'Delete or correct the revenue record itself.',
    'expenses':          'Delete or correct the expense record itself.',
    'honorarium':        'Correct it from the honorarium record.',
    'investments':       'Correct it from the investment record.',
    'va_receipt':        'This is money that genuinely arrived in the bank, so it cannot be '
                         'undone. Undo how it was applied instead, from the received '
                         'transfers page.',
}


def _reverse_savings_deposit(db, e, sid):
    """Undo a member's savings deposit: a compensating negative row, the cached
    balances put back, and the original marked reversed so it drops out of the
    records list and frees its receipt number for a corrected re-upload."""
    sav = db.execute('SELECT * FROM savings WHERE id = ?', (sid,)).fetchone()
    if not sav:
        return None
    amt = float(sav['amount'] or 0)
    keys = sav.keys()
    shr = float(sav['share_capital'] or 0) if 'share_capital' in keys else 0.0
    # Compensating negative deposit so SUM(amount) nets to zero — nothing deleted.
    db.execute('''INSERT INTO savings
                      (member_id, amount, share_capital, month, payment_type,
                       late_fee, payment_method, receipt_number, notes, date)
                  VALUES (?, ?, ?, ?, 'reversal', 0, 'reversal', ?, ?, ?)''',
               (sav['member_id'], -amt, -shr, sav['month'],
                f"REV-{sav['receipt_number'] or sav['id']}",
                f"Reversal of savings deposit #{sav['id']}", datetime.now()))
    db.execute('''UPDATE members
                      SET total_savings = COALESCE(total_savings, 0) - ?,
                          shares_value  = COALESCE(shares_value, 0) - ?
                  WHERE id = ?''', (amt, shr, sav['member_id']))
    _mark_savings_row_reversed(db, sid)
    return f"Member savings reduced by ₦{amt:,.2f}."


def _reverse_savings_payout(db, e, sid):
    """Undo a savings payout/withdrawal. The original row is negative, so the
    compensating row is positive and the member's balance goes back up."""
    sav = db.execute('SELECT * FROM savings WHERE id = ?', (sid,)).fetchone()
    if not sav:
        return None
    if 'reversed_at' in sav.keys() and sav['reversed_at']:
        return None
    paid = abs(float(sav['amount'] or 0))       # stored negative on the payout
    db.execute('''INSERT INTO savings
                      (member_id, amount, share_capital, month, payment_type,
                       late_fee, payment_method, receipt_number, notes, date)
                  VALUES (?, ?, 0, ?, 'reversal', 0, 'reversal', ?, ?, ?)''',
               (sav['member_id'], paid, sav['month'],
                f"REV-{sav['receipt_number'] or sav['id']}",
                f"Reversal of savings payout #{sav['id']}", datetime.now()))
    db.execute('UPDATE members SET total_savings = COALESCE(total_savings, 0) + ? WHERE id = ?',
               (paid, sav['member_id']))
    _mark_savings_row_reversed(db, sid)

    note = f"Member savings restored by ₦{paid:,.2f}."
    # A full payout can double as an exit (see the savings payout handler), and
    # reinstating a member is a membership decision, not a bookkeeping one — so
    # say so instead of quietly flipping their status back.
    member = db.execute("SELECT status FROM members WHERE id = ?", (sav['member_id'],)).fetchone()
    if member and member['status'] == 'former':
        note += (' This member is still archived as former — reinstate them from their '
                 'profile if the exit is also being undone.')
    return note


def _mark_savings_row_reversed(db, sid):
    """Mark a savings row reversed and free its receipt number, so the records
    list hides it and a corrected row can reuse the reference."""
    db.execute(
        "UPDATE savings SET reversed_at = ?, "
        "receipt_number = CASE WHEN receipt_number IS NOT NULL AND receipt_number != '' "
        "THEN receipt_number || '~REV' || CAST(id AS TEXT) ELSE receipt_number END "
        "WHERE id = ? AND reversed_at IS NULL", (datetime.now(), sid))


def _reverse_loan_repayment(db, e, sid):
    """Undo a loan repayment: put the balance back and reopen the loan."""
    rep = db.execute('SELECT * FROM repayments WHERE id = ?', (sid,)).fetchone()
    if not rep:
        return None
    if 'reversed_at' in rep.keys() and rep['reversed_at']:
        return None
    loan = db.execute('SELECT * FROM loans WHERE id = ?', (rep['loan_id'],)).fetchone()
    if not loan:
        return None
    restored = round(float(loan['balance'] or 0) + float(rep['amount'] or 0), 2)
    new_status = 'active' if (loan['status'] == 'completed') else loan['status']
    db.execute('UPDATE loans SET balance = ?, status = ?, completed_at = NULL WHERE id = ?',
               (restored, new_status, loan['id']))
    db.execute('UPDATE repayments SET reversed_at = ? WHERE id = ?', (datetime.now(), rep['id']))
    return f"Loan {loan['loan_number'] or loan['id']} balance restored by ₦{float(rep['amount'] or 0):,.2f}."


def _reverse_loan_adjustment(db, e, sid):
    """Undo a controlled loan balance correction."""
    adj = db.execute('SELECT * FROM loan_adjustments WHERE id = ?', (sid,)).fetchone()
    if not adj:
        return None
    if 'reversed_at' in adj.keys() and adj['reversed_at']:
        return None
    loan = db.execute('SELECT * FROM loans WHERE id = ?', (adj['loan_id'],)).fetchone()
    if not loan:
        return None

    amount = round(float(adj['amount'] or 0), 2)
    balance = round(float(loan['balance'] or 0), 2)
    if adj['direction'] == 'decrease':
        restored = round(balance + amount, 2)
    else:
        if balance < amount:
            raise ValueError(
                f"Cannot reverse this loan adjustment because loan {loan['loan_number'] or loan['id']} "
                f"now has only ₦{balance:,.2f} outstanding."
            )
        restored = round(balance - amount, 2)

    status = 'completed' if restored <= 0 else 'active'
    completed_at = datetime.now() if status == 'completed' else None
    db.execute('UPDATE loans SET balance = ?, status = ?, completed_at = ? WHERE id = ?',
               (max(restored, 0), status, completed_at, loan['id']))
    db.execute('UPDATE loan_adjustments SET reversed_at = ? WHERE id = ?',
               (datetime.now(), adj['id']))
    return f"Loan {loan['loan_number'] or loan['id']} correction reversed by ₦{amount:,.2f}."


def _unapply_receipt(db, alloc):
    """Put an allocated amount back into its receipt, so the money returns to
    the unallocated pool and can be applied somewhere else."""
    db.execute('UPDATE virtual_account_allocations SET reversed_at = ? WHERE id = ?',
               (datetime.now(), alloc['id']))
    receipt = db.execute('SELECT * FROM virtual_account_receipts WHERE id = ?',
                         (alloc['receipt_id'],)).fetchone()
    if not receipt:
        return
    applied = round(float(receipt['allocated_amount'] or 0) - float(alloc['amount'] or 0), 2)
    applied = max(applied, 0.0)
    status = 'part_allocated' if applied > 0 else 'unallocated'
    db.execute('UPDATE virtual_account_receipts SET allocated_amount = ?, status = ? '
               'WHERE id = ?', (applied, status, receipt['id']))


def _reverse_va_savings(db, e, alloc_id):
    """Undo a transfer that was applied to savings. The money is not returned to
    the member — it goes back to unallocated, because it really did arrive."""
    alloc = db.execute('SELECT * FROM virtual_account_allocations WHERE id = ?',
                       (alloc_id,)).fetchone()
    if not alloc or alloc['reversed_at']:
        return None
    note = _reverse_savings_deposit(db, e, alloc['target_id'])
    _unapply_receipt(db, alloc)
    amt = float(alloc['amount'] or 0)
    return ((note or '') + f" ₦{amt:,.2f} is unallocated again and can be applied elsewhere.").strip()


def _reverse_va_loan(db, e, alloc_id):
    """Undo a transfer that was applied to a loan."""
    alloc = db.execute('SELECT * FROM virtual_account_allocations WHERE id = ?',
                       (alloc_id,)).fetchone()
    if not alloc or alloc['reversed_at']:
        return None
    note = _reverse_loan_repayment(db, e, alloc['target_id'])
    _unapply_receipt(db, alloc)
    amt = float(alloc['amount'] or 0)
    return ((note or '') + f" ₦{amt:,.2f} is unallocated again and can be applied elsewhere.").strip()


def _reverse_member_receipt(db, e, receipt_id):
    """Undo a manual member receipt as one controlled workflow.

    The original receipt posted a single bank debit plus allocations into
    savings and/or loans. Reversal posts the opposite journal, reverses the
    linked subledger rows, restores loan balances, and marks the receipt so it
    cannot be reversed twice.
    """
    receipt = db.execute('SELECT * FROM member_receipts WHERE id = ?', (receipt_id,)).fetchone()
    if not receipt:
        return None
    if 'reversed_at' in receipt.keys() and receipt['reversed_at']:
        return None

    notes = []
    allocations = db.execute(
        'SELECT * FROM member_receipt_allocations WHERE receipt_id = ? ORDER BY id',
        (receipt_id,)
    ).fetchall()
    for alloc in allocations:
        target = alloc['target']
        if target == 'savings':
            note = _reverse_savings_deposit(db, e, alloc['target_id'])
            if note:
                notes.append(note)
        elif target == 'loan':
            note = _reverse_loan_repayment(db, e, alloc['target_id'])
            if note:
                notes.append(note)

    db.execute('UPDATE member_receipts SET reversed_at = ? WHERE id = ?',
               (datetime.now(), receipt_id))
    amount = float(receipt['amount'] or 0)
    notes.append(f"Manual receipt {receipt['receipt_number']} reversed for ₦{amount:,.2f}.")
    return ' '.join(notes)


# source_module -> the function that undoes its operational record. A module
# posting to the GL that appears in neither this registry nor
# LEDGER_ONLY_MODULES cannot be reversed from the journal.
def _reverse_loan_fee_refund(db, e, loan_id):
    """Undo an application-fee refund: the money comes back and the loan
    returns to the list of fees still owed."""
    row = db.execute(
        'SELECT * FROM loan_fee_refunds WHERE loan_id = ? AND reversed_at IS NULL',
        (loan_id,)).fetchone()
    if not row:
        return None
    db.execute('UPDATE loan_fee_refunds SET reversed_at = ?, status = ? WHERE id = ?',
               (datetime.now(), 'reversed', row['id']))
    # Cancel the contra revenue row so the income report nets back to the original.
    db.execute("DELETE FROM revenue WHERE revenue_number = ?", (f'REV/REFUND/{loan_id}',))
    amt = float(row['amount'] or 0)
    return (f"₦{amt:,.2f} refund undone; the loan is owed the fee again.")


REVERSAL_HANDLERS = {
    'savings_deposit': _reverse_savings_deposit,
    # An adjustment is a savings row like any other, so the deposit handler
    # undoes it correctly -- including a negative one, which it flips back.
    'savings_adjustment': _reverse_savings_deposit,
    'savings_payout':  _reverse_savings_payout,
    'loan_repayment':  _reverse_loan_repayment,
    'loan_fee_refund': _reverse_loan_fee_refund,
    'loan_adjustment': _reverse_loan_adjustment,
    'va_savings':      _reverse_va_savings,
    'va_loan':         _reverse_va_loan,
    'member_receipt':  _reverse_member_receipt,
    'voucher':         lambda db, entry, source_id: __import__('voucher_service', fromlist=['reverse_voucher_records']).reverse_voucher_records(db, entry, source_id),
}


def reversal_support(module):
    """(can_reverse, why_not) for a source module — used by the journal screens
    so the button is only offered where the undo is actually complete."""
    module = (module or '')
    if module in REVERSAL_HANDLERS or module in LEDGER_ONLY_MODULES:
        return True, None
    guidance = REVERSAL_GUIDANCE.get(module)
    return False, (
        f"Entries from '{module}' cannot be undone from the journal, because the ledger "
        f"would be corrected while its own records still said the opposite. "
        + (guidance or 'Undo it where it was created.'))


def _reverse_source_effect(db, e):
    """Undo the operational record behind an entry via the registered handler.
    Returns a short human note, or None when there is nothing to undo."""
    handler = REVERSAL_HANDLERS.get(e.get('source_module') or '')
    sid = e.get('source_id')
    if not handler or not sid:
        return None
    return handler(db, e, sid)


def reverse_journal_entry(db, entry_id, created_by=None, reason=None):
    """Reverse a journal entry by posting a balanced offsetting entry and undoing
    the operational record behind it. Everything happens in the caller's
    transaction.

    Never deletes: the original stays and is linked to its reversal. Refuses to
    reverse an entry that is in a locked period, that is itself a reversal, that
    has already been reversed, or whose module has no registered handler — see
    REVERSAL_HANDLERS. `reason` is required and kept on the reversal entry, so
    every undo carries why it happened. The reversal is dated today (the open
    period). Does NOT commit. Returns (new_entry_id, source_note-or-None).
    """
    entry = db.execute('SELECT * FROM journal_entries WHERE id = ?', (entry_id,)).fetchone()
    if not entry:
        raise ValueError('Journal entry not found.')
    e = dict(entry)
    if e.get('reversal_of'):
        raise ValueError('This entry is itself a reversal and cannot be reversed.')
    if e.get('reversed_at'):
        raise ValueError('This entry has already been reversed.')

    reason = (reason or '').strip()
    if not reason:
        raise ValueError('Give a reason for reversing this entry — it is kept on the record.')

    # Refuse rather than half-undo: correcting the ledger while the module's own
    # records still say the opposite leaves nothing on screen showing the two
    # disagree.
    ok, why_not = reversal_support(e.get('source_module'))
    if not ok:
        raise UnsupportedReversalError(why_not)

    # Only entries in the OPEN period may be reversed.
    lock = get_lock_date(db)
    if lock and _date_str(e['date']) <= lock:
        raise PeriodLockedError(
            f'This entry is dated {_date_str(e["date"])}, within the locked period '
            f'(books locked through {lock}). Move the lock date forward to reverse it.'
        )

    lines = db.execute(
        'SELECT account_code, debit, credit, memo FROM journal_lines WHERE entry_id = ?',
        (entry_id,)).fetchall()
    # Swap debit and credit to offset the original.
    offset = [{'account': l['account_code'],
               'debit': float(l['credit'] or 0),
               'credit': float(l['debit'] or 0),
               'memo': (l['memo'] or '')} for l in lines]

    new_id = post_journal(
        db, f"Reversal of {e['entry_number']} — {reason}", offset,
        date=datetime.now(), reference=e['entry_number'],
        source_module='reversal', source_id=entry_id, created_by=created_by)
    if new_id is None:
        raise ValueError('Nothing to reverse — the original entry has no lines.')

    now = datetime.now()
    db.execute('UPDATE journal_entries SET reversal_of = ?, reversal_reason = ? WHERE id = ?',
               (entry_id, reason, new_id))
    db.execute('UPDATE journal_entries SET reversed_at = ? WHERE id = ?', (now, entry_id))

    # Undo the subledger for precisely-linked operational entries.
    source_note = _reverse_source_effect(db, e)
    return new_id, source_note


def account_balance(db, code, as_of=None):
    """Signed balance (debits - credits) for one account."""
    date_filter = ''
    params = [code]
    if as_of:
        date_filter = 'AND je.date <= ?'
        params.append(f"{as_of} 23:59:59")
    row = db.execute(f'''
        SELECT COALESCE(SUM(jl.debit), 0) - COALESCE(SUM(jl.credit), 0) AS bal
        FROM journal_lines jl
        JOIN journal_entries je ON je.id = jl.entry_id
        WHERE jl.account_code = ? {date_filter}
    ''', tuple(params)).fetchone()
    return float(row['bal'] or 0) if row else 0.0
