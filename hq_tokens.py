"""
Per-tenant HQ credentials (security finding F-09).

Before: one HQ_SYNC_TOKEN, identical in every cooperative's container. Anyone who
read it from any one cooperative could suspend or reconfigure all of them.

Now: the HQ operator holds a master secret (HQ_SYNC_TOKEN, only in the HQ app).
Each cooperative receives only HQ_TENANT_TOKEN = HMAC-SHA256(master, "hq-sync:<name>"),
which is valid for that cooperative alone and reveals nothing about the master or
about any other cooperative's token.

deploy/vps/generate.py injects HQ_TENANT_TOKEN into each tenant; the HQ app derives
the same value from the tenant's code when it calls the tenant.
"""
import hashlib
import hmac
import os
import re
from urllib.parse import urlparse


def derive_tenant_token(master, name):
    return hmac.new(master.encode('utf-8'), f'hq-sync:{name}'.encode('utf-8'),
                    hashlib.sha256).hexdigest()


def tenant_name_from_code(code):
    """hq_clients.code is a name ('ooucoop'), a domain, or a URL. The tenant's name is
    its first host label, which is how add-client.sh names client and subdomain."""
    code = (code or '').strip().lower()
    if not code:
        return ''
    host = urlparse(code).hostname if '://' in code else code
    name = (host or '').split('.')[0]
    return name if re.match(r'^[a-z0-9][a-z0-9-]{1,30}$', name) else ''


def sender_token_for(client_code):
    """Token HQ should present to this tenant, or '' if HQ has no master secret."""
    master = (os.environ.get('HQ_SYNC_TOKEN') or '').strip()
    name = tenant_name_from_code(client_code)
    if not master or not name:
        return ''
    return derive_tenant_token(master, name)


def request_is_authorised(provided):
    """Tenant side. A tenant that has its own HQ_TENANT_TOKEN accepts only that.
    Without one (not yet migrated, or the HQ app itself) it falls back to the legacy
    shared HQ_SYNC_TOKEN so nothing breaks mid-rollout."""
    provided = (provided or '').strip()
    own = (os.environ.get('HQ_TENANT_TOKEN') or '').strip()
    if own:
        return bool(provided) and hmac.compare_digest(provided, own)
    legacy = (os.environ.get('HQ_SYNC_TOKEN') or '').strip()
    return bool(legacy and provided) and hmac.compare_digest(provided, legacy)
