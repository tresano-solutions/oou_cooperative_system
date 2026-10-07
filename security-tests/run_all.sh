#!/usr/bin/env bash
# Run every security test module against the throwaway local Postgres (see README.md).
# Each module rebuilds the coop_sectest database from scratch.
# Usage (from repo root):  PY=/path/to/venv/python bash security-tests/run_all.sh
set -u
PY="${PY:-python}"
cd "$(dirname "$0")/.."
for m in test_authz_idor test_authn test_financial_integrity test_race_conditions test_audit_trail test_input_handling; do
  echo "================ $m ================"
  "$PY" -W ignore -m unittest "security-tests.$m" 2>&1 \
    | grep -E "^(AUTHZ|AUTH|CSRF|FIN|RACE|AUD|INJ|XSS|UPL|TEN|MASS)|^(FAIL|ERROR):|^Ran|^OK|^FAILED"
done
