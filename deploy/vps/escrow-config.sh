#!/usr/bin/env bash
# Package the secrets that backup.sh deliberately leaves out, so a restore can
# actually read what it restores.
#
#   ./escrow-config.sh
#
# backup.sh dumps the databases and the uploaded documents. It does NOT include
# clients/<name>.env, and that is on purpose: those files hold each
# cooperative's FIELD_ENCRYPTION_KEY. Put the key in the same bucket as the
# dumps and field encryption has stopped meaning anything — whoever takes the
# bucket takes both. But lose the key and a restored database hands back every
# BVN, NIN and bank detail as ciphertext nobody can open, permanently.
#
# So they are kept, separately, by hand. This writes ONE encrypted archive for
# you to download and store somewhere that is not this server and not the
# backup bucket: a password manager, or an account the server has no keys for.
#
# It never uploads anything. That is the point.
#
# To read it back:
#   openssl enc -d -aes-256-cbc -pbkdf2 -in coopms-config-<stamp>.tar.gz.enc | tar xz
set -euo pipefail
cd "$(dirname "$0")"

STAMP=$(date +%Y%m%d-%H%M%S)
OUT="escrow"
ARCHIVE="${OUT}/coopms-config-${STAMP}.tar.gz"
mkdir -p "$OUT"
chmod 700 "$OUT"

shopt -s nullglob
files=(clients/*.env)
[[ -f .env ]] && files+=(.env)
shopt -u nullglob
if [[ ${#files[@]} -eq 0 ]]; then
  echo "Nothing to escrow: no clients/*.env found." >&2
  exit 1
fi

echo "==> Including:"
for f in "${files[@]}"; do
  key="no FIELD_ENCRYPTION_KEY"
  grep -qE '^FIELD_ENCRYPTION_KEY=.+' "$f" && key="has the field encryption key"
  printf '    %-28s %s\n' "$f" "$key"
done

# A passphrase of its own. Reusing BACKUP_PASSPHRASE would put the key archive
# and the dumps behind one secret, which is the arrangement this exists to avoid.
read -r -s -p "Passphrase for this archive (not the backup passphrase): " ESCROW_PASSPHRASE
echo
read -r -s -p "Again: " CONFIRM
echo
[[ -n "$ESCROW_PASSPHRASE" ]] || { echo "A passphrase is required." >&2; exit 1; }
[[ "$ESCROW_PASSPHRASE" == "$CONFIRM" ]] || { echo "They do not match." >&2; exit 1; }
if [[ ${#ESCROW_PASSPHRASE} -lt 12 ]]; then
  echo "Use at least 12 characters — this opens every cooperative's keys." >&2
  exit 1
fi
export ESCROW_PASSPHRASE

tar czf "$ARCHIVE" "${files[@]}"
if ! openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:ESCROW_PASSPHRASE \
     -in "$ARCHIVE" -out "${ARCHIVE}.enc"; then
  rm -f "$ARCHIVE" "${ARCHIVE}.enc"
  echo "Encryption failed — nothing was written." >&2
  exit 1
fi
rm -f "$ARCHIVE"
chmod 600 "${ARCHIVE}.enc"

cat <<MSG

==> Written: ${ARCHIVE}.enc

Now, and this is the part that matters:

  1. Copy it OFF this server    (scp root@<server>:$(pwd)/${ARCHIVE}.enc .)
  2. Store it somewhere that is NOT the backup bucket
  3. Store the passphrase somewhere separate again
  4. Delete the copy left here:  rm ${ARCHIVE}.enc

Left on the server it protects nothing: it is sitting beside the files it is a
copy of. Re-run this whenever a client is added or a key is rotated.
MSG
