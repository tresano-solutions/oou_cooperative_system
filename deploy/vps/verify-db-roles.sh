#!/usr/bin/env bash
# Prove that each client really is confined to its own database (finding F-01/V01).
#
#   ./verify-db-roles.sh            # check every client
#   ./verify-db-roles.sh ooucoop    # check one
#
# Read-only: it connects, asks questions and disconnects. It changes nothing, so
# it is safe to run against production at any time. Run it after migrating, and
# again whenever a client is added.
#
# For each client it checks that the app's own login:
#   1. exists and is NOT a superuser, and cannot create databases or roles,
#   2. can reach its own database,
#   3. CANNOT reach any other client's database, nor postgres/template1,
#   4. cannot read a file or run a program through the database server.
set -uo pipefail
cd "$(dirname "$0")"

fail=0
pass_line() { printf '    ok    %s\n' "$1"; }
fail_line() { printf '    FAIL  %s\n' "$1"; fail=$((fail + 1)); }

# Ask a question as the client's own role. Prints nothing; returns non-zero if
# the connection or the statement was refused.
as_role() {
  local url="$1" sql="$2"
  docker compose exec -T -e PGCONNECT_TIMEOUT=5 postgres \
    psql -v ON_ERROR_STOP=1 -tAq "$url" -c "$sql" >/dev/null 2>&1
}

clients=()
if [[ $# -gt 0 ]]; then
  clients=("$@")
else
  for f in clients/*.env; do
    [[ -e "$f" ]] || continue
    clients+=("$(basename "${f%.env}")")
  done
fi
[[ ${#clients[@]} -gt 0 ]] || { echo "No clients found in clients/."; exit 1; }

# Stop early if the database is not reachable, rather than reporting every
# check as a failure because docker could not be asked.
if ! docker compose exec -T postgres psql -U postgres -tAq -d postgres -c "SELECT 1" >/dev/null 2>&1; then
  echo "Cannot reach the postgres container from $(pwd)."
  echo "Run this from deploy/vps on the server, with the stack up."
  exit 1
fi

# Every client database, so each role can be tried against all the others.
all_dbs=$(docker compose exec -T postgres psql -U postgres -tAq -d postgres \
  -c "SELECT datname FROM pg_database WHERE datname LIKE 'coop\\_%' ORDER BY 1" | tr -d '\r')

for name in "${clients[@]}"; do
  echo "== ${name}"
  env_file="clients/${name}.env"
  [[ -f "$env_file" ]] || { fail_line "no ${env_file}"; continue; }

  url=$(grep -m1 '^DATABASE_URL=' "$env_file" | cut -d= -f2- | tr -d '\r')
  if [[ -z "$url" ]]; then
    fail_line "still on the shared postgres SUPERUSER - run ./harden-db-roles.sh ${name}"
    continue
  fi

  role="coop_${name//-/_}"
  own_db="coop_${name}"

  # 1. The role is not privileged.
  attrs=$(docker compose exec -T postgres psql -U postgres -tAq -d postgres -c \
    "SELECT rolsuper::int||','||rolcreatedb::int||','||rolcreaterole::int FROM pg_roles WHERE rolname='${role}'" \
    | tr -d '\r')
  # Cast to int in the query: concatenating a boolean renders it as "false",
  # while psql prints a bare boolean column as "f". 0/1 reads the same either way.
  case "$attrs" in
    "")      fail_line "role ${role} does not exist" ;;
    "0,0,0") pass_line "${role} is not a superuser and cannot create databases or roles" ;;
    *)       fail_line "${role} has privileges it should not (super,createdb,createrole = ${attrs})" ;;
  esac

  # 2. It can do its own job.
  if as_role "$url" 'SELECT 1'; then
    pass_line "reaches its own database (${own_db})"
  else
    fail_line "cannot reach its own database (${own_db}) - the app will not start"
  fi

  # 3. It cannot reach anyone else's.
  for db in $all_dbs postgres template1; do
    [[ "$db" == "$own_db" ]] && continue
    other_url="${url%/*}/${db}"
    if as_role "$other_url" 'SELECT 1'; then
      fail_line "CAN connect to ${db} - this is the finding, not the fix"
    else
      pass_line "refused by ${db}"
    fi
  done

  # 4. It cannot use the database server to reach the host.
  for sql in "COPY (SELECT 1) TO PROGRAM 'id'" "SELECT pg_read_file('/etc/passwd')"; do
    if as_role "$url" "$sql"; then
      fail_line "allowed: ${sql}"
    else
      pass_line "refused: ${sql%% *}..."
    fi
  done
done

echo
if [[ $fail -eq 0 ]]; then
  echo "All checks passed - every client is confined to its own database."
else
  echo "${fail} check(s) FAILED - read the FAIL lines above before treating this as done."
  exit 1
fi
