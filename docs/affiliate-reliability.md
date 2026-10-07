# Affiliate reliability update

The HQ affiliate model remains compatible with the current marketing leads and
setup-fee billing flow. Payout processing is not part of this update.

## Changes

- Referral lookup uses PostgreSQL-compatible string literals.
- Public demo forms carry `?ref=CODE` or `?affiliate_code=CODE` into the lead
  record. The browser session preserves the code across landing-page navigation.
- Acceptance stores the typed signature, timestamp, request IP, exact terms and
  a SHA-256 version. A stale terms form must be reviewed again before signing.
- Email helpers honour the provider result and escape affiliate names in HTML.
- Payment-time commission rates and attribution are snapshotted into durable
  jobs. Savepoints roll back partial commission writes without losing the
  invoice payment. Snapshot/queue persistence failures fail the outer transaction
  rather than silently losing commission information.
- HQ > Affiliate Commission lists failed jobs with a Retry action. Retries use
  the original rates, are serialized on PostgreSQL, and do not duplicate earnings.
- Reversals remain retryable after invoice deletion. An accrual retried after
  deletion creates no earnings, since the invoice is no longer paid/present.
- Form conversion tracking now runs after successful capture and excludes the
  submitted contact details and referral code from its custom event payload.

## Deployment and operation

Normal database initialization adds three nullable acceptance-evidence columns
and `affiliate_commission_jobs`. Existing acceptances are not backfilled with
invented signatures or terms. Existing commission records are unchanged.

Journal headers now initialize before voucher foreign keys, allowing a fresh
PostgreSQL database to initialize without a missing-table error.

After deployment, make a test affiliate introduction, accept its appointment,
submit a referral enquiry, and check the HQ attribution. Complete a test billing
cycle only in a separate test environment. Review pending commission processing
before relying on payable totals. Email provider acceptance is not proof of
delivery; check Resend delivery events when investigating missing mail.

## Local tests

```powershell
.\.test-venv\Scripts\python.exe -m pytest tests/test_affiliates.py tests/test_hq_billing.py -q
node tests/test_affiliate_referral.cjs
node tests/test_landing_motion.cjs
```

For PostgreSQL, provision a fresh, disposable local database named
`coopms_affiliate_test`, set `AFFILIATE_TEST_DATABASE_URL` to its localhost URL,
and run only `tests/test_affiliates.py` in a separate process. The test module
refuses non-local hosts and other database names. Never target a production DB.

## Follow-up scope

Payout approval/payment evidence, appointment-link expiry, shared public-form
rate limiting, and stricter team hierarchy validation remain separate work.
