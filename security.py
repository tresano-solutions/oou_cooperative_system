"""
Security utilities: audit logging, 2FA helpers.
"""

import io
import pyotp
import hashlib
import secrets
import string
from datetime import datetime, timedelta

import qrcode
import qrcode.image.svg


DEFAULT_PASSWORD_POLICY = {
    'password_min_length': '8',
    'password_require_upper': '0',
    'password_require_lower': '0',
    'password_require_number': '0',
    'password_require_special': '0',
}


def _setting(db, key, default=''):
    if not db:
        return default
    try:
        row = db.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        return str(row['value']) if row and row['value'] is not None else default
    except Exception:
        return default


def password_policy(db=None):
    policy = {}
    for key, default in DEFAULT_PASSWORD_POLICY.items():
        policy[key] = _setting(db, key, default)

    try:
        policy['password_min_length'] = str(max(6, min(128, int(policy['password_min_length']))))
    except (TypeError, ValueError):
        policy['password_min_length'] = DEFAULT_PASSWORD_POLICY['password_min_length']
    return policy


# Passwords people (and password-guessing tools) try first. The point of this list is
# not to make passwords harder to REMEMBER - it stops the handful that are cracked in
# seconds, and still allows any long, easy phrase such as "blue river market sunday".
_COMMON_PASSWORDS = {
    'password', 'passw0rd', 'p@ssword', 'p@ssw0rd', 'pass1234', 'password1', 'password12', 'password123',
    '12345678', '123456789', '1234567890', '123123123', '11111111', '00000000', '87654321', '12341234',
    'qwerty', 'qwerty12', 'qwerty123', 'qwertyui', 'qwertyuiop', 'asdfghjk', 'asdfghjkl', 'zxcvbnm',
    'abc12345', 'abcd1234', 'abcdefgh', 'iloveyou', 'letmein', 'welcome', 'welcome1', 'welcome12',
    'welcome123', 'admin', 'admin123', 'administrator', 'changeme', 'changeit', 'default', 'login',
    'monkey', 'dragon', 'master', 'sunshine', 'princess', 'football', 'baseball', 'superman', 'trustno1',
    'nigeria', 'nigeria1', 'nigeria123', 'lagos', 'lagos123', 'abuja', 'jesus', 'jesus123', 'godisgood',
    'godislove', 'blessing', 'blessed', 'ibadan', 'ogun', 'oou', 'smt',
    'coop', 'coop123', 'coop1234', 'coop12345', 'coopms', 'coopms123', 'cooperative', 'cooperative1',
    'cooperative123', 'member', 'member123', 'member1234', 'society', 'savings', 'savings123', 'treasurer',
    'secretary', 'president',
}
_COMMON_WORDS = ('password', 'passw0rd', 'qwerty', 'welcome', 'admin', 'letmein', 'coopms', 'iloveyou',
                 'cooperative', 'changeme')


def is_too_easy_to_guess(password, identifiers=()):
    """True for passwords that are guessed in seconds: well-known ones, a well-known word
    with digits tacked on, one repeated character, a simple run (12345678), or the person's
    own name/e-mail/phone. Long phrases pass."""
    import re
    raw = (password or '').strip().lower()
    if not raw:
        return True
    base = re.sub(r'[\d\W_]+$', '', raw)             # "Welcome2024!" -> "welcome"
    squeezed = re.sub(r'[\W_]+', '', raw)
    if raw in _COMMON_PASSWORDS or squeezed in _COMMON_PASSWORDS or base in _COMMON_PASSWORDS:
        return True
    if len(set(squeezed)) <= 2:                       # 11111111, abababab
        return True
    runs = '0123456789012345678909876543210abcdefghijklmnopqrstuvwxyzqwertyuiopasdfghjklzxcvbnm'
    if len(squeezed) >= 6 and squeezed in runs:
        return True
    if len(squeezed) < 14 and any(squeezed.startswith(w) or squeezed.endswith(w) for w in _COMMON_WORDS):
        return True
    for ident in identifiers:
        ident = re.sub(r'[\W_]+', '', (ident or '').split('@')[0].lower())
        if len(ident) >= 4 and (ident in squeezed or squeezed in ident):
            return True
    return False


def password_policy_description(db=None):
    policy = password_policy(db)
    parts = [f"at least {policy['password_min_length']} characters"]
    if policy['password_require_upper'] == '1':
        parts.append('an uppercase letter')
    if policy['password_require_lower'] == '1':
        parts.append('a lowercase letter')
    if policy['password_require_number'] == '1':
        parts.append('a number')
    if policy['password_require_special'] == '1':
        parts.append('a special character')
    return ('Password must contain ' + ', '.join(parts)
            + '. A short phrase of a few ordinary words (for example "blue river market sunday") is easy to '
              'remember and very safe. Avoid common passwords such as "password" or "12345678".')


def validate_password_strength(password, db=None, role=None, identifiers=()):
    policy = password_policy(db)
    password = password or ''
    errors = []
    min_length = int(policy['password_min_length'])

    if len(password) < min_length:
        errors.append(f'Password must be at least {min_length} characters.')
    if policy['password_require_upper'] == '1' and not any(c.isupper() for c in password):
        errors.append('Password must include an uppercase letter.')
    if policy['password_require_lower'] == '1' and not any(c.islower() for c in password):
        errors.append('Password must include a lowercase letter.')
    if policy['password_require_number'] == '1' and not any(c.isdigit() for c in password):
        errors.append('Password must include a number.')
    if policy['password_require_special'] == '1' and not any(c in string.punctuation for c in password):
        errors.append('Password must include a special character.')

    # Staff handle the cooperative's money, so they need a little more length.
    if role in STAFF_ROLES and len(password) < max(min_length, 10):
        errors.append('Officer passwords must be at least 10 characters. A short phrase of a few words works well.')

    if not errors and is_too_easy_to_guess(password, identifiers):
        errors.append('That password is too easy to guess. Try a short phrase of three or four ordinary '
                      'words you will remember, for example "blue river market sunday".')

    return not errors, errors


def generate_compliant_password(db=None, length=None):
    policy = password_policy(db)
    min_length = int(policy['password_min_length'])
    target_length = max(length or 14, min_length)
    chars = []
    if policy['password_require_upper'] == '1':
        chars.append(secrets.choice(string.ascii_uppercase))
    if policy['password_require_lower'] == '1':
        chars.append(secrets.choice(string.ascii_lowercase))
    if policy['password_require_number'] == '1':
        chars.append(secrets.choice(string.digits))
    if policy['password_require_special'] == '1':
        chars.append(secrets.choice('!@#$%^&*'))

    alphabet = string.ascii_letters + string.digits + '!@#$%^&*'
    while len(chars) < target_length:
        chars.append(secrets.choice(alphabet))

    secrets.SystemRandom().shuffle(chars)
    return ''.join(chars)


def generate_account_setup_token():
    """Return a plaintext token for email delivery and its database hash."""
    token = secrets.token_urlsafe(32)
    return token, hash_account_setup_token(token)


def hash_account_setup_token(token):
    return hashlib.sha256((token or '').encode('utf-8')).hexdigest()


# ── Audit logging ────────────────────────────────────────────────────────────

def log_audit(db, user_id, username, action, module, description,
              ip_address='', user_agent='', data=''):
    """Insert a row into audit_log. Never raises — audit must not crash main flow."""
    try:
        db.execute(
            '''INSERT INTO audit_log
               (user_id, username, action, module, description, ip_address, user_agent, data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (user_id, username, action, module, description, ip_address, user_agent, data)
        )
    except Exception as exc:
        print(f"[audit] failed to write log: {exc}")


# ── Revocable web sessions ───────────────────────────────────────────────────

def start_web_session(db, user_id, session_version):
    """Register this login server-side and stamp the cookie with its id and the
    user's current session_version. Call right after login_user()."""
    from flask import session
    sid = secrets.token_urlsafe(24)
    db.execute('INSERT INTO user_sessions (sid, user_id) VALUES (?, ?)', (sid, user_id))
    session['sid'] = sid
    session['sv'] = int(session_version or 0)


def end_web_session(db, sid):
    if sid:
        db.execute('UPDATE user_sessions SET revoked_at = ? WHERE sid = ? AND revoked_at IS NULL',
                   (datetime.now(), sid))


def session_is_valid(db, user_row, session_obj):
    """False if the account is disabled, its session_version moved on (password
    change, role change, deactivation, 2FA reset), or this session was logged out.
    Cookies issued before this feature (no sv/sid) are honoured until the next bump."""
    if not user_row or user_row['is_active'] == 0:
        return False
    current = int(user_row['session_version'] or 0) if 'session_version' in user_row.keys() else 0
    if int(session_obj.get('sv', 0) or 0) != current:
        return False
    sid = session_obj.get('sid')
    if sid:
        row = db.execute('SELECT revoked_at FROM user_sessions WHERE sid = ?', (sid,)).fetchone()
        if row is None or row['revoked_at']:
            return False
    return True


# ── 2FA helpers ──────────────────────────────────────────────────────────────

class SecurityManager:
    def generate_2fa_secret(self):
        """Return a fresh TOTP secret for a user."""
        return pyotp.random_base32()

    def verify_2fa(self, secret, token):
        """Return True if the TOTP token is valid for the given secret."""
        return pyotp.TOTP(secret).verify(token)

    def generate_backup_codes(self, count=10):
        """Return a list of one-time backup codes (plaintext + sha-256 hash)."""
        codes = []
        for _ in range(count):
            code = secrets.token_hex(4).upper()
            codes.append({
                'code': code,
                'hashed': hashlib.sha256(code.encode()).hexdigest(),
                'used': False,
            })
        return codes

    def get_totp_uri(self, secret, username, issuer='CoopMS'):
        """Return an otpauth:// URI for QR-code provisioning."""
        return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


security_manager = SecurityManager()


# ── 2FA persistence + enforcement ────────────────────────────────────────────
#
# The staff roles that can be forced to use 2FA (gated by the require_2fa
# setting). Ordinary members are never forced.
STAFF_ROLES = ('admin', 'treasurer', 'secretary', 'exco')


def two_factor_enforced(db) -> bool:
    """True when the require_2fa setting is on for this cooperative."""
    return _setting(db, 'require_2fa', '0') == '1'


def role_requires_2fa(role, db) -> bool:
    """True when a user of this role must have 2FA set up (enforcement on)."""
    return bool(role) and role in STAFF_ROLES and two_factor_enforced(db)


def totp_qr_svg(uri) -> str:
    """Render an otpauth URI as an inline SVG QR code (no Pillow needed)."""
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage,
                      box_size=9, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode('utf-8')


def _normalize_backup_code(code) -> str:
    """Backup codes are shown grouped/spaced; compare on the bare characters."""
    return ''.join((code or '').split()).replace('-', '').strip().upper()


def _hash_backup_code(code) -> str:
    return hashlib.sha256(_normalize_backup_code(code).encode('utf-8')).hexdigest()


def encrypt_2fa_secret(secret) -> str:
    """Encrypt the TOTP secret at rest (no-op if no encryption key configured)."""
    try:
        from crypto import encrypt_field
        return encrypt_field(secret or '')
    except Exception:
        return secret or ''


def decrypt_2fa_secret(stored) -> str:
    try:
        from crypto import decrypt_field
        return decrypt_field(stored or '')
    except Exception:
        return stored or ''


def user_2fa_secret(db, user_id) -> str:
    """Return the decrypted TOTP secret for a user, or '' if 2FA is not set up."""
    row = db.execute(
        'SELECT two_factor_secret FROM users WHERE id = ?', (user_id,)
    ).fetchone()
    if not row or not row['two_factor_secret']:
        return ''
    return decrypt_2fa_secret(row['two_factor_secret'])


def enable_user_2fa(db, user_id, secret) -> list:
    """Persist a confirmed TOTP secret (encrypted), mark 2FA enabled, and issue a
    fresh set of one-time backup codes. Returns the plaintext backup codes to
    show the user once."""
    db.execute(
        'UPDATE users SET two_factor_secret = ?, two_factor_enabled = 1 WHERE id = ?',
        (encrypt_2fa_secret(secret), user_id),
    )
    return regenerate_backup_codes(db, user_id)


def disable_user_2fa(db, user_id) -> None:
    """Turn off 2FA for a user and destroy their secret + backup codes."""
    db.execute(
        'UPDATE users SET session_version = COALESCE(session_version, 0) + 1, two_factor_secret = NULL, two_factor_enabled = 0 WHERE id = ?',
        (user_id,),
    )
    db.execute('DELETE FROM user_backup_codes WHERE user_id = ?', (user_id,))


def regenerate_backup_codes(db, user_id, count=10) -> list:
    """Replace a user's backup codes with a fresh set; return the plaintext codes."""
    db.execute('DELETE FROM user_backup_codes WHERE user_id = ?', (user_id,))
    codes = []
    for _ in range(count):
        raw = secrets.token_hex(5).upper()           # 10 hex chars
        code = f'{raw[:5]}-{raw[5:]}'                 # e.g. A1B2C-3D4E5
        codes.append(code)
        db.execute(
            'INSERT INTO user_backup_codes (user_id, code_hash, created_at) VALUES (?, ?, ?)',
            (user_id, _hash_backup_code(code), datetime.now()),
        )
    return codes


def count_unused_backup_codes(db, user_id) -> int:
    row = db.execute(
        'SELECT COUNT(*) AS c FROM user_backup_codes WHERE user_id = ? AND used_at IS NULL',
        (user_id,),
    ).fetchone()
    return int(row['c']) if row else 0


def consume_backup_code(db, user_id, code) -> bool:
    """If `code` matches an unused backup code, mark it used and return True."""
    code_hash = _hash_backup_code(code)
    row = db.execute(
        'SELECT id FROM user_backup_codes '
        'WHERE user_id = ? AND code_hash = ? AND used_at IS NULL',
        (user_id, code_hash),
    ).fetchone()
    if not row:
        return False
    db.execute('UPDATE user_backup_codes SET used_at = ? WHERE id = ?',
               (datetime.now(), row['id']))
    return True


def verify_2fa_code(db, user_id, code) -> bool:
    """Verify a login 2FA challenge: accept a valid TOTP code, or fall back to a
    one-time backup code (which is then consumed). Returns True on success."""
    code = (code or '').strip()
    if not code:
        return False
    secret = user_2fa_secret(db, user_id)
    if secret and pyotp.TOTP(secret).verify(code, valid_window=1):
        return True
    return consume_backup_code(db, user_id, code)
