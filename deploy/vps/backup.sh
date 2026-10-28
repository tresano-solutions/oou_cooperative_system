#!/usr/bin/env bash
# Back up every client database AND its uploaded documents, keep the last 14
# days locally, and — when off-site storage is configured — copy each archive
# somewhere the server cannot reach on its own.
#
# NOT included, deliberately: clients/<name>.env, which holds each cooperative's
# field encryption key. Without that key a restored database gives back every
# BVN, NIN and bank detail as unreadable ciphertext — so it must be kept, but
# NOT in the same place as the dumps, because the same file holds the database
# password. Use ./escrow-config.sh and store the result somewhere else entirely.
#
# Why off-site matters: a backup that lives on the machine it protects is not a
# backup. One deleted droplet, one ransomware run, one billing lapse, and the
# societies' records go with the server.
#
# Run nightly via cron (see README). Each client is dumped separately so one can
# be restored without touching the others.
#
# Off-site is optional and off until configured. In deploy/vps/.env:
#
#   OFFSITE_BUCKET=coopms-backups          # required to switch it on
#   OFFSITE_ENDPOINT=https://fra1.digitaloceanspaces.com
#   OFFSITE_REGION=fra1
#   OFFSITE_PREFIX=vps1                    # optional folder inside the bucket
#   AWS_ACCESS_KEY_ID=...                  # the Spaces/S3 key
#   AWS_SECRET_ACCESS_KEY=...
#   BACKUP_PASSPHRASE=...                  # optional; encrypts before upload
#
# The bucket must be private. These dumps contain members' names, savings and
# loan histories.
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck disable=SC1091
[[ -f .env ]] && { set -a; source .env; set +a; }

STAMP=$(date +%Y%m%d-%H%M%S)
OUT="backups"
RETAIN_DAYS="${BACKUP_RETAIN_DAYS:-14}"
mkdir -p "$OUT"

OFFSITE_BUCKET="${OFFSITE_BUCKET:-}"
OFFSITE_ENDPOINT="${OFFSITE_ENDPOINT:-}"
OFFSITE_REGION="${OFFSITE_REGION:-us-east-1}"
OFFSITE_PREFIX="${OFFSITE_PREFIX:-}"
BACKUP_PASSPHRASE="${BACKUP_PASSPHRASE:-}"
AWSCLI_IMAGE="${AWSCLI_IMAGE:-amazon/aws-cli:2.17.0}"

failures=0
uploaded=0
dumped=0
archived=0

offsite_enabled() { [[ -n "$OFFSITE_BUCKET" ]]; }

# Upload one file and prove it arrived. A backup job that keeps exiting 0 while
# silently uploading nothing is the failure mode this guards against.
upload() {
  local file="$1" key
  key="${OFFSITE_PREFIX:+$OFFSITE_PREFIX/}$(basename "$file")"

  local endpoint_arg=()
  [[ -n "$OFFSITE_ENDPOINT" ]] && endpoint_arg=(--endpoint-url "$OFFSITE_ENDPOINT")

  if ! docker run --rm \
        -v "$PWD/$OUT:/backups:ro" \
        -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
        -e "AWS_DEFAULT_REGION=$OFFSITE_REGION" \
        "$AWSCLI_IMAGE" "${endpoint_arg[@]}" \
        s3 cp "/backups/$(basename "$file")" "s3://$OFFSITE_BUCKET/$key" >/dev/null; then
    echo "  !! upload FAILED: $key" >&2
    return 1
  fi

  if ! docker run --rm \
        -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
        -e "AWS_DEFAULT_REGION=$OFFSITE_REGION" \
        "$AWSCLI_IMAGE" "${endpoint_arg[@]}" \
        s3api head-object --bucket "$OFFSITE_BUCKET" --key "$key" >/dev/null; then
    echo "  !! uploaded but NOT FOUND afterwards: $key" >&2
    return 1
  fi

  echo "  off-site: s3://$OFFSITE_BUCKET/$key"
  return 0
}

if offsite_enabled; then
  if [[ -z "${AWS_ACCESS_KEY_ID:-}" || -z "${AWS_SECRET_ACCESS_KEY:-}" ]]; then
    echo "OFFSITE_BUCKET is set but AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY are not." >&2
    exit 1
  fi
else
  echo "NOTE: off-site copying is off. Backups exist only on this server."
  echo "      Set OFFSITE_BUCKET in .env to change that — see the top of this script."
fi

# List coop_* databases and dump each.
DBS=$(docker compose exec -T postgres psql -U postgres -tAc \
  "SELECT datname FROM pg_database WHERE datname LIKE 'coop_%'")

# Finding nothing is a failure, not a quiet success. Postgres answering while
# no database matches means the databases were renamed, restored under another
# prefix, or this is pointing at the wrong stack — and a nightly job that keeps
# exiting 0 having backed up nothing is the worst way to discover that.
if [[ -z "${DBS//[[:space:]]/}" ]]; then
  echo "No coop_* databases found — nothing was backed up. Check the postgres" >&2
  echo "container and the database names before trusting this schedule again." >&2
  exit 1
fi

for DB in $DBS; do
  DB=$(echo "$DB" | tr -d '[:space:]')
  [[ -z "$DB" ]] && continue
  FILE="${OUT}/${DB}-${STAMP}.sql.gz"

  if ! docker compose exec -T postgres pg_dump -U postgres "$DB" | gzip > "$FILE"; then
    echo "  !! dump FAILED: $DB" >&2
    rm -f "$FILE"
    failures=$((failures + 1))
    continue
  fi

  # A truncated dump is worse than none, because it looks like a backup.
  if ! gzip -t "$FILE" 2>/dev/null; then
    echo "  !! dump is not a readable archive, discarding: $FILE" >&2
    rm -f "$FILE"
    failures=$((failures + 1))
    continue
  fi

  # A passphrase means these dumps must not leave the server readable. If the
  # encryption step fails the plaintext stays here — where the live database it
  # was taken from already sits — but it is NOT uploaded. Sending it off-site
  # anyway would put every member's name, savings and loan history into the
  # bucket in clear text, and the run's non-zero exit comes far too late to
  # recall it.
  may_upload=1
  if [[ -n "$BACKUP_PASSPHRASE" ]]; then
    if openssl enc -aes-256-cbc -pbkdf2 -salt \
         -pass env:BACKUP_PASSPHRASE -in "$FILE" -out "${FILE}.enc"; then
      rm -f "$FILE"
      FILE="${FILE}.enc"
    else
      rm -f "${FILE}.enc"        # a partial file openssl may have left behind
      echo "  !! encryption FAILED for $FILE — kept on this server only, NOT uploaded" >&2
      failures=$((failures + 1))
      may_upload=0
    fi
  fi

  dumped=$((dumped + 1))
  echo "backed up ${DB} -> ${FILE}"

  if offsite_enabled && (( may_upload )); then
    if upload "$FILE"; then
      uploaded=$((uploaded + 1))
    else
      failures=$((failures + 1))
    fi
  fi
done

# Uploaded documents live in Docker volumes, not in the database, so a restore
# from the dumps alone comes back with every payout voucher and member photo
# missing. Each volume is archived the same way a dump is: encrypted if a
# passphrase is set, then uploaded and checked.
for vol in $(docker volume ls -q | grep -E "_(uploads|private)-" || true); do
  FILE="${OUT}/${vol}-${STAMP}.tar.gz"
  if ! docker run --rm -v "${vol}:/src:ro" -v "$PWD/$OUT:/out" alpine:3.20 tar czf "/out/$(basename "$FILE")" -C /src . 2>/dev/null; then
    echo "  !! archive FAILED: ${vol}" >&2
    rm -f "$FILE"
    failures=$((failures + 1))
    continue
  fi

  may_upload=1
  if [[ -n "$BACKUP_PASSPHRASE" ]]; then
    if openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_PASSPHRASE -in "$FILE" -out "${FILE}.enc"; then
      rm -f "$FILE"; FILE="${FILE}.enc"
    else
      rm -f "${FILE}.enc"
      echo "  !! encryption FAILED for $FILE - kept on this server only, NOT uploaded" >&2
      failures=$((failures + 1)); may_upload=0
    fi
  fi

  archived=$((archived + 1))
  echo "archived ${vol} -> ${FILE}"
  if offsite_enabled && (( may_upload )); then
    if upload "$FILE"; then uploaded=$((uploaded + 1)); else failures=$((failures + 1)); fi
  fi
done

# The field encryption key is NOT in any of this, on purpose: it lives beside
# the database password in clients/<name>.env, and a bucket holding both the
# dumps and the key that opens them is a bucket where field encryption has
# stopped meaning anything. Escrow it separately with ./escrow-config.sh.
for f in clients/*.env; do
  [[ -e "$f" ]] || continue
  if ! grep -qE "^FIELD_ENCRYPTION_KEY=.+" "$f"; then
    echo "  !! $(basename "${f%.env}") has no FIELD_ENCRYPTION_KEY - a restore would not open its BVN/NIN/bank fields" >&2
    failures=$((failures + 1))
  fi
done

# Keep N days of local backups. Off-site retention belongs in the bucket's own
# lifecycle rule, so a compromised server cannot delete its own history.
find "$OUT" -type f \( -name '*.sql.gz' -o -name '*.sql.gz.enc' \) \
     -mtime "+${RETAIN_DAYS}" -delete 2>/dev/null || true
echo "Local backups older than ${RETAIN_DAYS} days pruned."

echo "Summary: ${dumped} database(s) dumped, ${archived} document archive(s), ${uploaded} copied off-site, ${failures} failure(s)."

if (( failures > 0 )); then
  echo "Backup run finished WITH FAILURES — read the lines marked !! above." >&2
  exit 1
fi

if offsite_enabled && (( uploaded < dumped + archived )); then
  echo "Not every dump reached off-site storage." >&2
  exit 1
fi
