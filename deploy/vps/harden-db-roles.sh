#!/usr/bin/env bash
# Give one client its own least-privilege Postgres login instead of the shared
# `postgres` superuser (security finding F-01). Idempotent; re-run to rotate the password.
#
#   ./harden-db-roles.sh <name>              # existing client: backup, create role, switch, restart
#   ./harden-db-roles.sh <name> --no-backup  # (used by add-client.sh for a brand-new empty DB)
#
# Roll back for one client: delete its DATABASE_URL= line from clients/<name>.env,
# then `python3 generate.py && docker compose up -d app-<name>` (it falls back to
# the superuser URL). Nothing in the database itself is changed destructively;
# only ownership and connect rights.
set -euo pipefail
cd "$(dirname "$0")"

NAME="${1:-}"; MODE="${2:-}"
if [[ -z "$NAME" ]]; then echo "Usage: ./harden-db-roles.sh <name> [--no-backup]"; exit 1; fi
ENV_FILE="clients/${NAME}.env"
[[ -f "$ENV_FILE" ]] || { echo "No $ENV_FILE"; exit 1; }
DB="coop_${NAME}"
ROLE="coop_${NAME//-/_}"

psql_super() { docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres "$@"; }

psql_super -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='${DB}'" | grep -q 1 \
  || { echo "Database ${DB} does not exist."; exit 1; }

if [[ "$MODE" != "--no-backup" ]]; then
  echo "==> Backing up first (bash backup.sh)"
  bash backup.sh
fi

PASSWORD=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')

echo "==> Creating/rotating role ${ROLE} and locking down ${DB}"
python3 db_roles.py stage1 "$NAME" "$PASSWORD" | psql_super -d postgres
python3 db_roles.py stage2 "$NAME"             | psql_super -d "$DB"

echo "==> Pointing ${NAME} at its own login"
cp "$ENV_FILE" "${ENV_FILE}.bak-$(date +%Y%m%d-%H%M%S)"
grep -v '^DATABASE_URL=' "$ENV_FILE" > "${ENV_FILE}.tmp" || true
echo "DATABASE_URL=postgresql://${ROLE}:${PASSWORD}@postgres:5432/${DB}" >> "${ENV_FILE}.tmp"
mv "${ENV_FILE}.tmp" "$ENV_FILE"
chmod 600 "$ENV_FILE"

python3 generate.py >/dev/null
if [[ "$MODE" != "--no-backup" ]]; then
  docker compose up -d "app-${NAME}"
  echo "==> Waiting for app-${NAME} to come up"
  sleep 8
  if docker compose logs --tail 40 "app-${NAME}" 2>&1 | grep -qiE "permission denied|password authentication failed|Traceback"; then
    echo "!! app-${NAME} reports an error - read: docker compose logs app-${NAME}"
    echo "   Roll back by deleting the DATABASE_URL= line in ${ENV_FILE}, then generate.py + up -d."
    exit 1
  fi
fi

echo "==> Role status"
psql_super -d postgres -tAc "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname='${ROLE}'"
echo "Done. ${NAME} now runs as ${ROLE}. The previous env file is kept as ${ENV_FILE}.bak-* (it holds the OLD config; delete once happy)."
