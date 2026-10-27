"""Uploaded documents that must never be handed to the public.

Anything under static/ is served to whoever asks for it, so a payout voucher
saved there was one guessed filename away from a stranger. Private files live
outside static/ and only leave through a view that has already checked who is
asking.

In production each client container mounts a Docker volume at PRIVATE_UPLOAD_DIR,
so these files survive a rebuild the same way static/uploads always did.
"""

import os
import secrets

DEFAULT_DIR = 'private_uploads'


def private_root():
    """The directory private uploads are stored under."""
    return os.environ.get('PRIVATE_UPLOAD_DIR', '').strip() or DEFAULT_DIR


def private_path(rel):
    """Absolute path for a stored file, or None if `rel` points outside the root.

    `rel` comes from the database, so it is not attacker-controlled today — but
    a traversal check costs nothing and keeps it that way if a future caller
    ever passes something from a request.
    """
    root = os.path.abspath(private_root())
    full = os.path.abspath(os.path.join(root, rel or ''))
    if full != root and not full.startswith(root + os.sep):
        return None
    return full


def save_private(f, subdir, allowed_exts, max_bytes):
    """Validate an upload and store it privately.

    Returns (True, relative_path) or (False, message). The stored name is
    random rather than derived from the clock: a timestamp plus a short random
    suffix is a small enough space to walk through.
    """
    name = (f.filename or '')
    if '.' not in name:
        return False, 'file must be one of: ' + ', '.join(sorted(allowed_exts)).upper()
    ext = name.rsplit('.', 1)[1].lower()
    if ext not in allowed_exts:
        return False, 'only ' + ', '.join(sorted(e.upper() for e in allowed_exts)) + ' files are allowed.'

    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(0)
    if size > max_bytes:
        return False, f'file is larger than {max_bytes // (1024 * 1024)} MB.'
    if size == 0:
        return False, 'file is empty.'

    rel = f'{subdir}/{secrets.token_hex(16)}.{ext}'
    disk = private_path(rel)
    os.makedirs(os.path.dirname(disk), exist_ok=True)
    f.save(disk)
    return True, rel


LEGACY_PAYOUT_DIR = os.path.join('static', 'uploads', 'payouts')


def migrate_legacy_payouts(db):
    """Move payout evidence out of static/ and repoint the rows that name it.

    Runs on every boot and does nothing once there is nothing left to move.
    Filenames are kept as they are: what made the old location unsafe was that
    static/ is public, not the name, and keeping it means a half-finished move
    can still be matched up by hand.
    """
    if not os.path.isdir(LEGACY_PAYOUT_DIR):
        return 0
    moved = 0
    for entry in sorted(os.listdir(LEGACY_PAYOUT_DIR)):
        source = os.path.join(LEGACY_PAYOUT_DIR, entry)
        if not os.path.isfile(source):
            continue
        rel = f'payouts/{entry}'
        target = private_path(rel)
        if target is None:
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            if os.path.exists(target):
                os.remove(source)          # already moved on an earlier boot
            else:
                os.replace(source, target)
        except OSError:
            continue                        # try again next boot
        db.execute('UPDATE savings SET evidence_path = ? WHERE evidence_path = ?',
                   (rel, f'uploads/payouts/{entry}'))
        moved += 1
    if moved:
        print(f'  [uploads] moved {moved} payout evidence file(s) out of static/')
    return moved
