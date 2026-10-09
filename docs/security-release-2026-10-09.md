# Consolidated security release, 9 October 2026

Baseline: `41bc7bd` (affiliate reliability). Release branch: `codex/security-release`.

## Included

Consolidates the existing F-01 through F-13 security branches: restricted database
roles, purge safeguards, audit persistence, finite money input checks, concurrent
posting controls, approval separation, Python dependency updates, tenant-specific
HQ credentials, revocable sessions, mobile two-factor authentication, sign-in
throttling and password rules. F-02 is a credential-rotation runbook, not evidence
that external credentials have been rotated or repository visibility changed.

Integration fixes disable sending mail from the legacy GET `/test-email` route
(administrators are redirected to Settings), prevent generated tenant credentials
from entering the shared Docker image, explicitly clear the HQ master token in
non-HQ containers, handle quoted configuration values, and restrict the generated
Compose file's permissions. Test fixtures preserve member identity and avoid
duplicate seeding; access tests recognise legitimate self-service security pages.

## Validation

Security regression modules run individually against disposable local PostgreSQL
16 with `SECTEST_RESTRICTED=1`. Each module recreates its own test database. The
release checks cover audit persistence, authentication and authorisation, database
roles, purge protection, financial validation and concurrency, approval rules,
HQ credentials, session revocation, mobile 2FA and sign-in policy.

Application regressions cover hardening workflows, affiliates, HQ billing, loan
limits and repayment controls. Mobile TypeScript checking, affiliate referral
JavaScript tests and Python dependency consistency are also checked. Raw results
are local under `outputs/security-release/` and are not shipped in the image.

## Unresolved audit findings

The original diagnostic suites are retained without suppressing their failures.
They still report seven assertions across `test_financial_integrity` and
`test_input_handling`:

- Tokenless legacy deposit submissions can be duplicated; the new form-token
  regression passes for the updated forms.
- Societies with three or fewer active officers permit one officer at multiple
  approval stages, with auditing; strict separation applies above that threshold.
- Money columns still use floating-point types (54 columns in the test schema).
- Lead and member CSV exports do not consistently neutralise spreadsheet formulas.
- The public lead rate limiter can be bypassed with forwarded-IP values in the
  direct application test. Proxy trust needs a separate end-to-end review.
- Uploaded payout evidence is accessible through the static file route.

These pre-existing findings are not resolved by consolidating the existing
branches. This release is not a clean bill of security.

## Deployment

Take an encrypted offsite backup before deployment and retain the previous image.
Regenerate the VPS configuration, build the shared app image and restart all three
tenant services. Migrate database roles one client at a time using
`harden-db-roles.sh`, checking HTTP and database connectivity between clients.
Confirm no tenant retains the HQ master secret and that its own token is accepted.

The mobile source includes the OTP prompt, but a VPS rebuild cannot update installed
mobile binaries. Distribute a compatible mobile update for users with 2FA enabled.
Existing 2FA accounts must never receive a password-only bypass for old clients.

The F-02 runbook and unresolved audit findings remain follow-up work.
