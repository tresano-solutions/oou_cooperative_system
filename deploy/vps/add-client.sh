#!/usr/bin/env bash
# Add a client: one command sets up their database, secrets, app container, and
# HTTPS domain. Run from deploy/vps/ on the VPS.
#
#   ./add-client.sh <name> <domain>
#   ./add-client.sh client1 client1.example.com
#
# <name>  : lowercase letters/digits/dashes (used for the container + database).
# <domain>: the web address for this client (its DNS A record must point here).
set -euo pipefail
cd "$(dirname "$0")"

NAME="${1:-}"
DOMAIN="${2:-}"

if [[ -z "$NAME" || -z "$DOMAIN" ]]; then
  echo "Usage: ./add-client.sh <name> <domain>"
  echo "   e.g. ./add-client.sh client1 client1.example.com"
  exit 1
fi
if ! [[ "$NAME" =~ ^[a-z0-9][a-z0-9-]{1,30}$ ]]; then
  echo "Invalid name '$NAME'. Use lowercase letters, digits and dashes."
  exit 1
fi

# Root env holds shared secrets (Postgres password + the HQ sync token).
if [[ ! -f .env ]]; then
  echo "Creating deploy/vps/.env with a new Postgres password..."
  echo "POSTGRES_PASSWORD=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" > .env
fi
# Shared token that lets HQ read member counts and set access on every tenant.
# One value for all clients; injected into each app container via generate.py.
if ! grep -q '^HQ_SYNC_TOKEN=' .env 2>/dev/null; then
  echo "Adding a shared HQ_SYNC_TOKEN to deploy/vps/.env..."
  echo "HQ_SYNC_TOKEN=$(python3 -c 'import secrets;print(secrets.token_hex(32))')" >> .env
fi
# shellcheck disable=SC1091
set -a; source .env; set +a

mkdir -p clients
CLIENT_FILE="clients/${NAME}.env"
if [[ -f "$CLIENT_FILE" ]]; then
  echo "clients/${NAME}.env already exists — refusing to overwrite it."
  echo "Edit it by hand, or run ./remove-client.sh ${NAME} first."
  exit 1
fi

echo "==> Generating secrets for '${NAME}'"
SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
FIELD_ENCRYPTION_KEY=$(python3 -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())' 2>/dev/null || echo '')
ADMIN_PASSWORD=$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')

cat > "$CLIENT_FILE" <<EOF
# ── ${NAME} ──  Keep this file secret (it is gitignored).
DOMAIN=${DOMAIN}
SECRET_KEY=${SECRET_KEY}
FIELD_ENCRYPTION_KEY=${FIELD_ENCRYPTION_KEY}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
FLASK_DEBUG=0

# Email. Each tenant has its own sender configuration.
# Set MAIL_ENABLED=1 and configure exactly one provider.
MAIL_ENABLED=0
MAIL_FROM=

# Option A: Brevo API
# BREVO_API_KEY=

# Option B: SMTP relay (Brevo SMTP, Gmail app password, Outlook, etc.)
# SMTP_HOST=smtp-relay.brevo.com
# SMTP_PORT=587
# SMTP_USER=
# SMTP_PASS=
# SMTP_USE_TLS=1
# SMTP_USE_SSL=0

# Option C: Resend API. Requires verified sender/domain.
# RESEND_API_KEY=

# Payments and billing:
# PAYSTACK_SECRET=
# PAYSTACK_PUBLIC=
# SUBSCRIPTION_EXPIRY=2027-01-01
EOF
if [[ "$NAME" == "hq" ]]; then
  cat >> "$CLIENT_FILE" <<EOF

# CoopMS owner back office. Enables marketing lead inbox and public lead capture.
MARKETING_HQ=1
MARKETING_HQ_BASE_URL=https://${DOMAIN}
MARKETING_SITE_URL=https://www.cooperativems.com
# Where new website lead alerts should go. Leave blank to notify active admins.
MARKETING_LEAD_NOTIFY_EMAIL=
MARKETING_ALLOWED_ORIGINS=cooperativems.com,www.cooperativems.com
EOF
fi
echo "    wrote ${CLIENT_FILE}"

echo "==> Starting Postgres (if not already up)"
docker compose up -d postgres
# Wait until Postgres is ready.
for i in $(seq 1 30); do
  if docker compose exec -T postgres pg_isready -U postgres >/dev/null 2>&1; then break; fi
  sleep 2
done

DBNAME="coop_${NAME}"
echo "==> Creating database ${DBNAME} (if it does not exist)"
EXISTS=$(docker compose exec -T postgres psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='${DBNAME}'" || true)
if [[ "$EXISTS" != "1" ]]; then
  docker compose exec -T postgres createdb -U postgres "${DBNAME}"
  echo "    created ${DBNAME}"
else
  echo "    ${DBNAME} already exists — reusing it"
fi

echo "==> Giving ${NAME} its own least-privilege database login"
bash harden-db-roles.sh "$NAME" --no-backup

echo "==> Regenerating compose + Caddy config"
python3 generate.py

echo "==> Building/starting the app container"
docker compose up -d --build

# Caddy is already running from earlier clients, so `up -d` won't restart it and
# it won't see the new site block on its own. Reload it so it picks up the new
# domain and fetches its HTTPS certificate.
echo "==> Reloading Caddy to pick up ${DOMAIN}"
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile 2>/dev/null \
  || docker compose restart caddy

echo
echo "======================================================================"
echo " Client '${NAME}' is live at:  https://${DOMAIN}"
echo " Admin login:  username 'admin'"
echo " Admin password is in:  ${CLIENT_FILE}"
echo
echo " Next: point ${DOMAIN}'s DNS A record at this server's IP (if not already),"
echo " then open the URL. HTTPS is issued automatically within ~30s."
echo " Log in, change the admin password, then import members under Data Migration."
echo "======================================================================"
