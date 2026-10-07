# security-tests

Dynamic security tests written for `SECURITY_AUDIT.md`. They drive the **real Flask application**
in-process (Flask test client) against a **throwaway local PostgreSQL** that holds only seeded
test data. They never touch production, staging or any live domain, and make no outbound calls
(mail/SMS are disabled).

A **FAIL is a confirmed finding** (the test asserts the safe behaviour). A PASS is a confirmed strength.

## Safety guard
`_harness.py` aborts unless `DATABASE_URL` is loopback and the database name starts with
`coop_sectest`. It drops/recreates that database on every run.

## One-time setup
```bash
# 1. a scratch Postgres 16 cluster on 127.0.0.1:55432 (any local Postgres works; Docker also fine)
initdb -D /tmp/sectest-pg -U postgres --auth=trust -E UTF8
pg_ctl -D /tmp/sectest-pg -o "-p 55432 -c listen_addresses=127.0.0.1" -l /tmp/sectest-pg.log start

# 2. a virtualenv with the app's pinned requirements
python -m venv /tmp/sv && /tmp/sv/bin/pip install -r requirements.txt     # Windows: Scripts\python.exe
```
Override host/port/user with `SECTEST_PG_HOST`, `SECTEST_PG_PORT`, `SECTEST_PG_USER`.

## Run
```bash
PY=/tmp/sv/bin/python bash security-tests/run_all.sh          # everything
/tmp/sv/bin/python -m unittest security-tests.test_race_conditions -v   # one module
```

## Modules and the finding IDs they prove
| Module | Tests |
|---|---|
| `test_authz_idor.py` | AUTHZ-01 unauthenticated sweep, AUTHZ-02 member-role sweep of every route (GET+POST), AUTHZ-03 cross-member IDOR |
| `test_authn.py` | AUTH-01 lockout DoS, AUTH-02 mobile login skips 2FA, AUTH-03 JWT after deactivation, AUTH-04 cookie replay after logout, AUTH-05 weak passwords, AUTH-06 one-request purge, AUTH-07 e-mail identity link, AUTH-08 disabled user keeps web session, CSRF-01 |
| `test_financial_integrity.py` | FIN-01 NaN amounts, FIN-02 double-submit, FIN-03 single admin all approval stages, FIN-04 float money |
| `test_race_conditions.py` | RACE-01 double disbursement, RACE-02/03 concurrent repayments (lost update) |
| `test_audit_trail.py` | AUD-01/02 audit rows silently lost |
| `test_input_handling.py` | INJ-01..03 CSV injection / rate-limit bypass, XSS-01, UPL-01 public uploads, TEN-01 DB role / blast radius, MASS-01 |

Nothing here modifies application code. The only files written outside this folder are a
temporary file under `static/uploads/payouts/` (UPL-01, deleted in a `finally`).
