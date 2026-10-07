"""
Shared helpers used across all blueprints.
Import from here — never from app.py — to avoid circular imports.
"""

from datetime import datetime, timedelta
from functools import wraps
from io import BytesIO

from flask import flash, redirect, url_for, request
from flask_login import UserMixin, current_user
from PIL import Image
from werkzeug.utils import secure_filename

from security import log_audit


# ── User model ───────────────────────────────────────────────────────────────

class User(UserMixin):
    def __init__(self, id, username, password_hash, role, email='',
                 must_change_password=0, is_super_admin=0):
        self.id                  = id
        self.username            = username
        self.password_hash       = password_hash
        self.role                = role
        self.email               = email
        self.must_change_password = bool(must_change_password)
        self.is_super_admin      = bool(is_super_admin)


# ── Role-based access control ─────────────────────────────────────────────────

def role_required(*roles):
    """Guard a view.

    Access is decided by the permission catalogue when the endpoint is listed
    in it (see permissions.py), so an admin can reassign who does what without
    a code change. The literal `roles` remain the fallback for any endpoint
    that is not catalogued — a new view is never left unguarded.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            import permissions as perms
            permission = perms.endpoint_permission(request.endpoint)
            if permission:
                if not perms.user_can(permission):
                    flash(f'Access denied: you are not assigned "{perms.describe(permission)}". '
                          f'Ask the President to assign it to you.', 'danger')
                    return redirect(url_for('main.dashboard'))
                return f(*args, **kwargs)
            if current_user.role not in roles:
                flash('Access denied. Insufficient privileges.', 'danger')
                return redirect(url_for('main.dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator





def new_submission_token():
    """A fresh one-time token to embed in a money-posting form."""
    import uuid
    return uuid.uuid4().hex


def claim_submission(db, token):
    """Record a form's one-time token inside the current transaction.

    Returns False if this token was already used (a double-click, a retry, a
    replayed request) so the caller must not post the money again. The row lives
    in the same transaction as the posting: if the posting rolls back, the token
    is released and the form can be resubmitted. A form without a token (old
    client) is allowed through.
    """
    token = (token or '').strip()
    if not token:
        return True
    cur = db.execute(
        'INSERT INTO form_submissions (token) VALUES (?) ON CONFLICT (token) DO NOTHING',
        (token[:64],))
    return cur.rowcount == 1


def finite_float(value):
    """float() that refuses NaN, Infinity and absurd magnitudes.

    Python's float() happily parses 'nan' and 'inf'; every comparison with NaN is
    False, so checks like ``amount <= 0`` pass and a NaN balance is then stored
    permanently. Raises ValueError (callers already handle that) instead.
    """
    import math
    if isinstance(value, str):
        value = value.strip().replace(',', '')
    number = float(value)
    if not math.isfinite(number) or abs(number) > 1e12:
        raise ValueError('amount must be a finite number')
    return number


STAFF_ROLES = {'admin', 'treasurer', 'secretary', 'exco'}


def has_unpaid_loan_of_type(db, member_id, purpose):
    """An unpaid active loan blocks another application for the same product."""
    return db.execute(
        """SELECT id FROM loans WHERE member_id = ? AND status = 'active'
           AND balance > 0 AND LOWER(TRIM(purpose)) = LOWER(TRIM(?)) LIMIT 1""",
        (member_id, purpose),
    ).fetchone() is not None


def is_staff_user() -> bool:
    return getattr(current_user, 'role', None) in STAFF_ROLES


def member_for_user(db, user_id=None):
    """Return the members row linked to a user by matching email, or None.

    This is the single source of truth for the user↔member email link used by
    the member portal, the online-payments blueprint, and the mobile API.

    With no ``user_id`` it uses the current logged-in user's email; with a
    ``user_id`` it looks up that user's email first.
    """
    if user_id is None:
        email = getattr(current_user, 'email', '') or ''
    else:
        urow  = db.execute('SELECT email FROM users WHERE id = ?', (user_id,)).fetchone()
        email = (urow['email'] if urow else '') or ''
    if not email:
        return None
    return db.execute(
        'SELECT * FROM members WHERE lower(COALESCE(email, \'\')) = lower(?)',
        (email,),
    ).fetchone()


def current_member_id(db):
    """Return the member id linked to the logged-in user by email."""
    member = member_for_user(db)
    return member['id'] if member else None


def parse_member_joined(value):
    """Return a member join date from common DB/CSV formats, or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    raw = raw.replace('Z', '+00:00').split('+')[0]
    raw = raw.split('.')[0]
    for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y', '%m/%d/%Y'):
        try:
            return datetime.strptime(raw[:19], fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def member_has_minimum_membership(member, min_months=6, as_of=None):
    """Check loan membership-age eligibility using the stored date_joined."""
    joined = parse_member_joined(member['date_joined'] if member and 'date_joined' in member.keys() else None)
    if not joined:
        return False, None, 0
    today = as_of or datetime.now()
    days = max(0, (today - joined).days)
    return days >= min_months * 30, joined, days


def can_access_member(db, member_id: int) -> bool:
    """Staff can access any member; members can access only their own profile."""
    if is_staff_user():
        return True
    own_id = current_member_id(db)
    return own_id == member_id


# ── Login rate limiting ───────────────────────────────────────────────────────

_RATE_MAX   = 5     # failures before block
_RATE_BLOCK = 900   # window / block duration in seconds (15 min)

# Failed logins are tracked in the login_attempts table (not process memory), so
# the limit is shared across all gunicorn workers and survives a restart. The
# old per-process dict let an attacker get N attempts per worker and reset the
# counter with any redeploy.


def _to_dt(value) -> datetime:
    """Normalise a stored timestamp (datetime on Postgres, ISO string on SQLite)."""
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return datetime.now()


def _recent_attempts(ip: str) -> list:
    """Timestamps of failed attempts for `ip` still within the block window."""
    from database import get_db
    db = get_db()
    cutoff = datetime.now() - timedelta(seconds=_RATE_BLOCK)
    try:
        rows = db.execute(
            'SELECT attempted_at FROM login_attempts '
            'WHERE ip = ? AND attempted_at >= ? ORDER BY attempted_at',
            (ip, cutoff),
        ).fetchall()
        return [_to_dt(r['attempted_at']) for r in rows]
    except Exception:
        # Never let a throttle-store hiccup block a legitimate login.
        return []


def is_rate_limited(ip: str) -> bool:
    return len(_recent_attempts(ip)) >= _RATE_MAX


def lockout_seconds_remaining(ip: str) -> int:
    """Return seconds until the oldest blocking attempt expires (0 if not locked)."""
    attempts = _recent_attempts(ip)
    if len(attempts) < _RATE_MAX:
        return 0
    oldest = min(attempts)
    remaining = int(_RATE_BLOCK - (datetime.now() - oldest).total_seconds())
    return max(remaining, 0)


def record_failed_login(ip: str, username: str = '') -> None:
    from database import get_db
    db = get_db()
    try:
        db.execute(
            'INSERT INTO login_attempts (ip, username, attempted_at) VALUES (?, ?, ?)',
            (ip, username, datetime.now()),
        )
        # Prune expired rows on the write path so reads stay lock-free.
        db.execute('DELETE FROM login_attempts WHERE attempted_at < ?',
                   (datetime.now() - timedelta(seconds=_RATE_BLOCK),))
        db.commit()
    except Exception:
        pass


def clear_login_attempts(ip: str) -> None:
    from database import get_db
    db = get_db()
    try:
        db.execute('DELETE FROM login_attempts WHERE ip = ?', (ip,))
        db.commit()
    except Exception:
        pass


# ── Sign-in throttling (finding F-12) ─────────────────────────────────────────
#
# Designed so an honest, slightly forgetful member is rarely stopped, while
# guessing is still impractical:
#   * the limit follows the ACCOUNT, not the shared network: a whole office or a
#     village sharing one Wi-Fi is never locked out by one person's typos;
#   * a generous number of tries first, then a SHORT pause (5 min), not a lock-out;
#   * a separate, much higher ceiling per network stops one machine from trying
#     many different accounts (password spraying);
#   * one person signing in successfully never resets the network ceiling (that
#     would let an attacker clear it by logging into their own account).
# The pause only affects typing a password; "Forgot password?" by email always works.

ACCOUNT_MAX = 8          # wrong passwords per account within the window
ACCOUNT_PAUSE = 300      # seconds to wait after the last wrong try (5 minutes)
NETWORK_MAX = 40         # wrong passwords, any accounts, from one network address
NETWORK_PAUSE = 600      # seconds (10 minutes)
THROTTLE_WINDOW = 900    # failures older than this are forgotten (15 minutes)


def _norm_account(username) -> str:
    return (username or '').strip().lower()


def _failures(column: str, value: str) -> list:
    from database import get_db
    cutoff = datetime.now() - timedelta(seconds=THROTTLE_WINDOW)
    try:
        rows = get_db().execute(
            f'SELECT attempted_at FROM login_attempts WHERE {column} = ? AND attempted_at >= ? '
            'ORDER BY attempted_at', (value, cutoff)).fetchall()      # column is a constant below
        return [_to_dt(r['attempted_at']) for r in rows]
    except Exception:
        return []        # a throttle-store hiccup must never lock honest people out


def _pause_left(times: list, limit: int, pause: int) -> int:
    if len(times) < limit:
        return 0
    return max(0, int(pause - (datetime.now() - max(times)).total_seconds()))


def sign_in_pause_seconds(network_key: str, username: str) -> int:
    """Seconds this sign-in must wait (0 = go ahead)."""
    account = _pause_left(_failures('username', _norm_account(username)), ACCOUNT_MAX, ACCOUNT_PAUSE) \
        if _norm_account(username) else 0
    network = _pause_left(_failures('ip', network_key), NETWORK_MAX, NETWORK_PAUSE)
    return max(account, network)


def sign_in_tries_left(username: str) -> int:
    return max(0, ACCOUNT_MAX - len(_failures('username', _norm_account(username))))


def record_sign_in_failure(network_key: str, username: str) -> None:
    record_failed_login(network_key, _norm_account(username))


def clear_account_failures(username: str) -> None:
    """After a successful sign-in forget THIS account's wrong tries (not the network's)."""
    from database import get_db
    db = get_db()
    try:
        db.execute('DELETE FROM login_attempts WHERE username = ?', (_norm_account(username),))
        db.commit()
    except Exception:
        pass


def pause_message(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    return (f"Let's try again in {minutes} minute{'s' if minutes != 1 else ''}. "
            "If you have forgotten your password, tap \"Forgot password?\" and we will email you a link.")


# ── Loan interest computation ─────────────────────────────────────────────────

# Maps canonical purpose names → settings key suffix
PURPOSE_SETTING_KEY = {
    'Regular':        'regular',
    'Housing':        'housing',
    'Emergency':      'emergency',
    'Asset Purchase': 'asset',
    'School Fees':    'school_fees',
}

METHOD_LABELS = {
    'flat':             'Flat Rate',
    'reducing_monthly': 'Declining Balance (Monthly Rate)',
    'reducing_annual':  'Declining Balance (Annual Rate)',
}


def compute_loan_schedule(principal, rate, tenure, method='reducing_annual'):
    """
    Compute loan repayment schedule using one of three interest methods.

    Args:
        principal : Loan amount (float, ₦)
        rate      : Interest rate as a percentage, e.g. 11 for 11%
        tenure    : Loan duration in months (int)
        method    : One of:
            'flat'             — Flat rate on original principal.
                                 Interest = P × (rate/100) × (tenure/12).
                                 Equal monthly instalments; rate is annual.
            'reducing_monthly' — Declining balance; rate IS the monthly rate
                                 (e.g. 2 means 2% per month).  PMT formula.
            'reducing_annual'  — Declining balance; rate is annual, divided
                                 by 12 for each month.  Standard amortisation.

    Returns:
        (monthly_payment: float, total_repayment: float, schedule: list[dict])
        Each schedule dict has: month, payment, principal, interest, balance
    """
    P = float(principal)
    r_pct = float(rate)
    n = int(tenure)

    if n <= 0 or P <= 0:
        return 0.0, 0.0, []

    if method == 'flat':
        total_interest  = P * (r_pct / 100) * (n / 12)
        total_repayment = P + total_interest
        mp       = total_repayment / n          # equal monthly payment
        prin_pm  = P / n
        int_pm   = total_interest / n

        balance  = P
        schedule = []
        for i in range(1, n + 1):
            balance = round(max(0.0, balance - prin_pm), 2)
            schedule.append({
                'month':     i,
                'payment':   round(mp, 2),
                'principal': round(prin_pm, 2),
                'interest':  round(int_pm, 2),
                'balance':   balance,
            })

    elif method == 'reducing_monthly':
        r = r_pct / 100                         # monthly rate as decimal
        if r > 0:
            mp = P * r * (1 + r) ** n / ((1 + r) ** n - 1)
        else:
            mp = P / n
        total_repayment = mp * n

        balance  = P
        schedule = []
        for i in range(1, n + 1):
            interest_p  = round(balance * r, 2)
            principal_p = round(mp - interest_p, 2)
            balance     = round(max(0.0, balance - principal_p), 2)
            if i == n:
                balance = 0.0
            schedule.append({
                'month':     i,
                'payment':   round(mp, 2),
                'principal': principal_p,
                'interest':  interest_p,
                'balance':   balance,
            })

    else:  # 'reducing_annual' — standard amortisation (default)
        r = (r_pct / 100) / 12                  # monthly rate from annual
        if r > 0:
            mp = P * r * (1 + r) ** n / ((1 + r) ** n - 1)
        else:
            mp = P / n
        total_repayment = mp * n

        balance  = P
        schedule = []
        for i in range(1, n + 1):
            interest_p  = round(balance * r, 2)
            principal_p = round(mp - interest_p, 2)
            balance     = round(max(0.0, balance - principal_p), 2)
            if i == n:
                balance = 0.0
            schedule.append({
                'month':     i,
                'payment':   round(mp, 2),
                'principal': principal_p,
                'interest':  interest_p,
                'balance':   balance,
            })

    return round(mp, 2), round(total_repayment, 2), schedule


# ── Financial helpers ─────────────────────────────────────────────────────────

def record_revenue(db, category, amount, description='', source='',
                   received_by=None, notes=''):
    """Insert a revenue (income) row, e.g. for late fees or loan fees.

    Does NOT commit — it runs inside the caller's transaction so the income
    is booked atomically with the operation that generated it. Skips zero /
    non-positive amounts. Never raises (income logging must not break the
    main flow).
    """
    try:
        amount = float(amount or 0)
    except (TypeError, ValueError):
        return
    if amount <= 0:
        return
    import secrets
    from datetime import datetime
    from database import USE_POSTGRES
    revenue_number = f"REV/{datetime.now().strftime('%Y%m%d%H%M%S')}/{secrets.token_hex(3).upper()}"
    sql = ('''INSERT INTO revenue
              (revenue_number, category, amount, description, source, date, received_by, notes)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?)''')
    params = (revenue_number, category, amount, description, source,
              datetime.now(), received_by, notes)
    # On PostgreSQL a failed statement aborts the entire transaction, which would
    # roll back the savings/loan record this revenue accompanies. Wrap the insert
    # in a SAVEPOINT so a failure here can never poison the caller's transaction.
    if USE_POSTGRES:
        try:
            db.execute('SAVEPOINT sp_revenue')
            db.execute(sql, params)
            db.execute('RELEASE SAVEPOINT sp_revenue')
        except Exception as exc:
            try:
                db.execute('ROLLBACK TO SAVEPOINT sp_revenue')
            except Exception:
                pass
            print(f"[revenue] failed to record {category} {amount}: {exc}")
    else:
        try:
            db.execute(sql, params)
        except Exception as exc:
            print(f"[revenue] failed to record {category} {amount}: {exc}")


def coop_name(db):
    """This client cooperative's own name (from settings); neutral fallback."""
    try:
        row = db.execute("SELECT value FROM settings WHERE key = 'coop_name'").fetchone()
        return (row['value'] if row else '') or 'Your Cooperative'
    except Exception:
        return 'Your Cooperative'


def member_prefix(db):
    """The configurable prefix for auto-generated member numbers (per-client).

    Falls back to 'MEM' so nothing is hard-coded to any one cooperative.
    """
    try:
        row = db.execute("SELECT value FROM settings WHERE key = 'member_prefix'").fetchone()
        v = (row['value'] if row else '') or ''
        return (v.strip() or 'MEM').upper()
    except Exception:
        return 'MEM'


def member_savings_balance(db, member_id):
    """Authoritative savings balance from the savings ledger (source of truth).

    Use this for financial decisions such as loan eligibility rather than the
    cached members.total_savings column, which can drift over time.
    """
    row = db.execute(
        'SELECT COALESCE(SUM(amount), 0) FROM savings WHERE member_id = ?',
        (member_id,)
    ).fetchone()
    return float(row[0] or 0) if row else 0.0


def member_share_capital(db, member_id):
    """The member's total share-capital contribution, from the savings ledger.

    Each contribution is split (see share_capital_split): `amount` holds the
    deposit portion and `share_capital` holds the carved-out share-capital
    portion. Summing `share_capital` gives the member's equity stake, and the
    total across all members reconciles to COA 3200 (Member Share Capital).
    """
    row = db.execute(
        'SELECT COALESCE(SUM(share_capital), 0) FROM savings WHERE member_id = ?',
        (member_id,)
    ).fetchone()
    return float(row[0] or 0) if row else 0.0


def reconcile_member_savings(db, member_id=None):
    """Recompute members.total_savings from the savings ledger.

    Reconciles a single member when member_id is given, otherwise every member.
    Does NOT commit — the caller owns the transaction. Returns the number of
    members whose cached balance was corrected.
    """
    if member_id is not None:
        ids = [member_id]
    else:
        ids = [r['id'] for r in db.execute('SELECT id FROM members').fetchall()]
    corrected = 0
    for mid in ids:
        ledger = member_savings_balance(db, mid)
        cur    = db.execute('SELECT total_savings FROM members WHERE id = ?', (mid,)).fetchone()
        cur_val = float(cur['total_savings'] or 0) if cur else 0.0
        if abs(cur_val - ledger) > 0.005:
            db.execute('UPDATE members SET total_savings = ? WHERE id = ?', (ledger, mid))
            corrected += 1
    return corrected


def share_capital_split(db, amount):
    """Split a savings contribution into (deposit_portion, share_capital_portion)
    per the 'share_capital_pct' setting. With the setting at 0 (default) the whole
    amount stays as a deposit and the share portion is 0, so behaviour is
    unchanged unless a cooperative opts in."""
    try:
        row = db.execute("SELECT value FROM settings WHERE key = 'share_capital_pct'").fetchone()
        pct = float(row['value']) if row and row['value'] else 0.0
    except Exception:
        pct = 0.0
    pct = max(0.0, min(pct, 100.0))
    amount = float(amount or 0)
    if pct <= 0 or amount <= 0:
        return round(amount, 2), 0.0
    share = round(amount * pct / 100.0, 2)
    return round(amount - share, 2), share


def split_repayment(amount, principal, total_repayment):
    """Split a loan repayment into (principal_part, interest_part).

    The loan's ``balance`` is stored as total_repayment (principal + all
    interest combined), so a payment must be apportioned. We split each
    payment in the loan's principal:interest ratio. This is a proportional
    (not per-period amortising) allocation, but it reconciles exactly over the
    life of the loan: the principal parts sum to the original principal and the
    interest parts sum to the total interest.

    Returns (principal_part, interest_part), each rounded to 2 dp.
    """
    try:
        amount          = float(amount or 0)
        principal       = float(principal or 0)
        total_repayment = float(total_repayment or 0)
    except (TypeError, ValueError):
        return round(float(amount or 0), 2), 0.0
    if total_repayment <= 0 or amount <= 0:
        return round(amount, 2), 0.0
    interest_total    = max(total_repayment - principal, 0.0)
    interest_fraction = interest_total / total_repayment
    interest_part     = round(amount * interest_fraction, 2)
    principal_part    = round(amount - interest_part, 2)
    return principal_part, interest_part


# ── File upload validation ────────────────────────────────────────────────────

_ALLOWED_IMAGE_EXTS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
_MAX_UPLOAD_BYTES   = 5 * 1024 * 1024  # 5 MB


def validate_image(file) -> tuple[bool, str]:
    """Check extension, file size, and real image content via Pillow.
    Seeks stream back to 0 on success so the caller can still save it."""
    filename = secure_filename(file.filename or '')
    if '.' not in filename:
        return False, 'File has no extension.'
    ext = filename.rsplit('.', 1)[1].lower()
    if ext not in _ALLOWED_IMAGE_EXTS:
        return False, f'File type .{ext} is not allowed. Use PNG, JPG, GIF, or WEBP.'
    data = file.read(_MAX_UPLOAD_BYTES + 1)
    if len(data) > _MAX_UPLOAD_BYTES:
        return False, 'File too large. Maximum size is 5 MB.'
    try:
        img = Image.open(BytesIO(data))
        img.verify()
    except Exception:
        return False, 'File does not appear to be a valid image.'
    file.stream.seek(0)
    return True, ''


def logo_data_uri(file, max_px: int = 240) -> str:
    """Downscale an uploaded logo and return a compact
    ``data:image/png;base64,...`` string.

    Stored directly in the settings table so the logo lives in the database and
    survives container rebuilds — unlike files under static/uploads, which are
    ephemeral in the per-client Docker deployment. Downscaling keeps the value
    small (a few KB) since it is loaded on every page via the context processor.
    """
    import base64
    file.stream.seek(0)
    img = Image.open(file.stream)
    img = img.convert('RGBA') if img.mode in ('P', 'RGBA', 'LA') else img.convert('RGB')
    img.thumbnail((max_px, max_px))
    buf = BytesIO()
    img.save(buf, format='PNG', optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode('ascii')
    return f'data:image/png;base64,{b64}'


# ── In-app notification helper ────────────────────────────────────────────────

def notify(db, user_id: int, title: str, message: str,
           notification_type: str = 'info', action_url: str = '') -> None:
    """Insert a notification record for a specific user. Never raises."""
    if not user_id:
        return
    try:
        from datetime import datetime
        db.execute('''
            INSERT INTO notifications
                (user_id, title, message, notification_type, is_read, action_url, created_at)
            VALUES (?, ?, ?, ?, 0, ?, ?)
        ''', (user_id, title, message, notification_type, action_url or '', datetime.now()))
        try:
            from mobile_push import send_mobile_pushes
            send_mobile_pushes(db, user_id, title, message, notification_type, action_url)
        except Exception:
            pass
        _sms_fallback(db, user_id, title, message)
    except Exception:
        pass  # notifications are non-critical — never break the main flow


def _has_mobile_device(db, user_id) -> bool:
    """True if this user has a registered device, so push already reached them."""
    try:
        row = db.execute(
            "SELECT 1 FROM mobile_devices WHERE user_id = ? "
            "AND COALESCE(enabled, 1) = 1 LIMIT 1", (user_id,)).fetchone()
        return row is not None
    except Exception:
        return False


def _sms_fallback(db, user_id, title, message) -> None:
    """Text the member only when push could not reach them — a member with the
    app installed costs nothing, so we do not pay to tell them twice."""
    try:
        from sms import sms_enabled, send_sms
        if not sms_enabled(db):
            return
        if _has_mobile_device(db, user_id):
            return
        member = member_for_user(db, user_id)
        phone = (member['phone'] if member and 'phone' in member.keys() else '') or ''
        if not phone:
            return
        body = f"{title}: {message}".strip()
        from email_service import run_in_background
        run_in_background(_send_sms_task, phone, body, member['id'])
    except Exception:
        pass


def _send_sms_task(phone, body, member_id):
    """Runs off the request thread with its own app context and connection."""
    try:
        from database import get_db
        from sms import send_sms
        send_sms(get_db(), phone, body, member_id=member_id, purpose='notification')
    except Exception:
        pass


def notify_member(db, member_email: str, title: str, message: str,
                  notification_type: str = 'info', action_url: str = '') -> None:
    """Find the user account matching member_email and create a notification."""
    if not member_email:
        return
    try:
        user = db.execute('SELECT id FROM users WHERE email = ?', (member_email,)).fetchone()
        if user:
            notify(db, user['id'], title, message, notification_type, action_url)
    except Exception:
        pass


# ── Audit helper ──────────────────────────────────────────────────────────────

def audit(db, action: str, module: str, description: str, data: str = '') -> None:
    """Write an audit log entry pre-filled from the current request/user."""
    uid   = current_user.id       if not current_user.is_anonymous else None
    uname = current_user.username if not current_user.is_anonymous else 'anonymous'
    ip    = request.remote_addr or ''
    ua    = request.user_agent.string if request.user_agent else ''
    log_audit(db, uid, uname, action, module, description, ip, ua, data)
