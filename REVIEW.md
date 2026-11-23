# Coop-MS technical review
Reviewed HEAD 59b577e, 23 November 2026. Static source and Git inspection only. Confidence is highest for the cited code paths. Deployment and credential validity are unverified.

## Executive assessment
| Area | Score / 10 | Basis |
|---|---:|---|
| Architecture | 6 | Domain blueprints and payment adapters are identifiable, but importing the app mutates storage (R09). |
| Security | 3 | Tracked database credentials and public card storage undermine otherwise substantial access controls (R01–R04). |
| Database | 5 | Unique financial references and row locking coexist with approximate money types and silent schema failures (R08–R10). |
| Code quality | 6 | Financial regression tests exercise real invariants, while controllers mix SQL, business logic and file writes (R06). |
| DevOps | 4 | VPS recovery steps exist, but the root stack cannot build its workers as supplied (R07). |

The system manages cooperative members, savings, loans, investments and accounting. Staff use web workflows and reports. Members have a web portal and an Expo mobile client. The backend integrates payment providers and runs tenant-specific deployments (README.md:3–18, app.py:155–183, payments.py:195–211, deploy/vps/db_roles.py:58–63).

## Ranked risks and remediation
Each row gives the evidence, consequence, fix and estimated engineering effort. Severity reflects potential impact under the stated conditions. Ordering within a severity reflects reach and exposure.

| Rank / ID | Severity / area | Evidence | Why it matters | Recommended fix | Effort |
|---|---|---|---|---|---|
| 1 / R01 | Critical / secrets | Postgres Database ur.txt:1, Git commit 103a248; Dockerfile:22–24, deploy/vps/Dockerfile:19, .dockerignore:1–18 | Credential-bearing database URI [REDACTED] persists in history and falls inside the root image build context. Anyone with access to affected source or images may have received it. | Rotate the database credential first, review access logs, remove the file from source and build inputs, inventory affected images and coordinate history cleanup. | M |
| 2 / R02 | High / infrastructure | docker-compose.yml:18,56–60,79–84,101–105,254–260 | Root Compose combines fixed DB credentials [REDACTED], published data ports, disabled Elasticsearch security and a management service with Docker socket access. Host firewall and deployment status unverified. | Remove host publication for internal services, require injected credentials and isolate management behind a private control network. | S |
| 3 / R03 | High / access control | card_generator.py:73–74, blueprints/cards.py:33–46,53–68,78–100, nginx.conf:31–38, app.py:105–123 | Generated cards containing member details reside under public static storage despite checks on view/download handlers. Legacy payout files left after a failed move are also covered by the Nginx static alias. | Move cards outside static, serve them through the checked handler, deny legacy directories at the proxy and migrate existing files. | M |
| 4 / R04 | High / authentication | mobile_api.py:515–531, blueprints/auth.py:194, security.py:312–320 | An enabled MFA flag paired with a missing stored secret skips the mobile challenge. The code then issues a JWT. Occurrence of inconsistent records is unverified. | Require successful verification whenever the flag is enabled. Deny login on missing secret and direct the account to audited recovery. | S |
| 5 / R05 | High / key protection | security.py:295–302,322–333 | An encryption exception returns the original TOTP seed to the enrollment persistence path. A database copy can then contain recoverable second factors. | Fail enrollment on encryption error and verify production key availability before enabling enrollment. Inspect existing records without logging seeds. | S |
| 6 / R06 | Medium / card integrity | blueprints/cards.py:13–29,44–47; card_generator.py:170–174 | A GET rotates a verification token and commits before rendering succeeds. Concurrent renders share a temporary barcode filename, risking mixed output or failed generation. | Use a CSRF-protected POST, render the barcode in memory, stage each output privately, then publish the card reference and token together. | M |
| 7 / R07 | High / availability | docker-compose.yml:42–43,152–176; Dockerfile.worker, tasks.py and worker.py are zero bytes per wc -c; requirements.txt:1–23; nginx.conf:56–58 | The root worker build has no instructions and beat expects Celery. The app probe targets Flask, while the configured health response belongs to Nginx. Full-stack startup and readiness reporting are unreliable. | Remove unused services or supply a tested worker image and task entry point. Add distinct application readiness and proxy liveness probes. | M |
| 8 / R08 | Medium / schema integrity | database.py:216–249,477–492,144–150 | Column upgrades discard arbitrary errors as duplicates. SQLite connections do not explicitly enable foreign keys, leaving enforcement dependent on the library default. | Detect existing columns via metadata, propagate unexpected errors and enable and assert foreign keys on every SQLite connection. | M |
| 9 / R09 | Medium / deployment | app.py:103–116; database.py:288–295,2463; Procfile:1 | Every worker import enters DB initialization and attempts legacy file migration. The PostgreSQL advisory lock serializes schema work but couples startup to its duration and success. File migration runs separately. | Run a single versioned release migration, record completion and make worker startup check schema compatibility. | L |
| 10 / R10 | Medium / financial precision | database.py:203–211,477–497; blueprints/reports.py:356–357 | Savings amounts are binary floats in both supported databases and report totals explicitly sum floats. Fractional rounding differences can accumulate. Actual discrepancies unverified. | Define rounding per financial operation, reconcile historical balances, migrate to integer minor units or fixed-scale numeric, and use decimal arithmetic end to end. | L |

## Other findings
| ID | Severity / area | Evidence | Why it matters | Recommended fix | Effort |
|---|---|---|---|---|---|
| R11 | Medium / performance | member_financials.py:24–48; blueprints/reports.py:374–397 | Statements fetch full histories and execute two extra queries per loan. Portfolio reporting materializes its result set. | Batch repayments and adjustments by member, retain opening-balance semantics, stream large exports. | M |
| R12 | Medium / API contract | app.py:449–467,612–626; mobile_api.py:186–189 | Global gates and errors can send HTML to a JSON client. The 500 handler opens a DB connection even when the database caused the failure. | Select JSON for mobile paths and roll back only an existing connection with guarded cleanup. | S |
| R13 | Medium / container privilege | Dockerfile:2–35; deploy/vps/Dockerfile:3–26; deploy/vps/docker-entrypoint.sh:25 | Both web images inherit the base user and do not drop privileges. Root image also ships compilers and services alongside Gunicorn. | Build dependencies in a separate stage, set an unprivileged runtime user and grant write access only to upload/runtime directories. | M |
| R14 | Low / maintenance | app.py:455–459 | Local constant disables the maintenance branch for every request. | Wire a documented switch to this branch or remove the dead branch and document the actual maintenance procedure. | S |

## Repository and operations
- Git evidence: git ls-files returned 497 paths. git rev-list --count HEAD returned 367. git shortlog -sn HEAD attributed 365 commits to keshdel and two to Claude. Reachable HEAD history spans 7 March to 20 November 2026. Largest by changed file count were add4408 (147), e4b37b1 (72) and d60871b (59). Size alone gives no evidence of malicious changes. git count-objects -vH reported 18.66 MiB packed.
- Python 3.11.9 in runtime.txt:1; Flask 3.1.3, Flask-Login, Flask-WTF, psycopg2, Gunicorn and PyJWT pinned in requirements.txt:1–23. SQLAlchemy is listed but the inspected financial paths use direct SQL. HTML/Jinja templates, static JS and Bootstrap implement the web UI (README.md:14–18; app.py:612–626). Expo 51, React 18.2, React Native 0.74.5 and TypeScript use npm with package-lock.json (mobile/coopms-mobile/package.json:5–28).
- README.md:20–25 documents pip installation and local launch. Procfile:1 and render.yaml:4–15 define hosted startup. Mobile scripts include start, native builds and typecheck. docs/workspace-design.md:41 documents a pytest invocation. git ls-files showed no GitHub Actions workflow. External CI and protected-branch settings unverified.
- Modular monolith: one Flask app registers 27 blueprints (app.py:155–183). Domain helpers coexist with transaction orchestration in handlers. blueprints/ctas.py contains 2,381 lines and 60 functions. Database connections are request-scoped (database.py:153–186). Local mutable card files require shared storage across replicas (R03). Runtime import cycles unverified. Firebase has a commented example only (functions/main.py:14–21).
- SQLite development and PostgreSQL deployment share a SQL adapter (database.py:144–150,203–240). Isolation on VPS is database/role-based (deploy/vps/db_roles.py:58–63). Member-level filtering is separately required within each database (blueprints/cards.py:53–61, tests/test_member_financials.py:35–37). A row lock helper and partial unique references protect selected posting paths (database.py:237–240,2293–2305). Soft deletion is workflow-specific: active users and reversed postings have separate semantics (mobile_api.py:503, tests/test_member_financials.py:14–20). Schema uses both date and created_at timestamps; no uniform rollback chain appears in migrations/versions (only .gitkeep).
- Web cookies use HttpOnly, Lax and production Secure flags (app.py:44–60); authenticated responses have cache controls and production HSTS (app.py:595–609). Mobile JWTs have issuer, audience, expiry and revocation logic (mobile_api.py:163–181,205–263). Password creation hashes at database.py:2427–2428 and seeded account setup depends on supplied environment values (database.py:2413–2428). CSRF exceptions are explicit (app.py:185–192). Provider verification uses constant-time comparison (payments.py:195–208,302–310). Mobile login records failures (mobile_api.py:511–524). Endpoint-wide throttling, validation and authorization parity remain unverified.
- Private upload helper enforces size, extension, random storage names and lexical root containment (private_files.py:23–63). File-content validation is unverified. Sampled parameterized queries include blueprints/cards.py:18,57–61 and member_financials.py:24–48. No conclusion about all SQL sinks, mass assignment, XSS contexts, SSRF or unsafe deserialization. HQ builds outbound URLs from client records (blueprints/hq_billing.py:216–254); destination authorization, redirect policy and network egress controls require verification. No global CORS header found by source search for CORS and Access-Control-Allow-Origin in Python/Nginx. Effective proxy headers and TLS unverified.
- Backups use pg_dump and gzip integrity checks (deploy/vps/backup.sh:117–125). Recovery requires database, upload and key material (deploy/vps/README.md:138–164). Restore success, RPO/RTO and application/database rollback compatibility unverified. VPS uses exec Gunicorn with a 120-second timeout; provider requests use explicit timeouts (deploy/vps/docker-entrypoint.sh:25; payments.py:86,101). Retry safety, resource ceilings and failover in the deployed system are unverified. Root Compose declares Redis, Elasticsearch and monitoring services (docker-compose.yml:76–110,223–245); declarations alone do not establish application use or functioning telemetry.
- Tests: 45 tracked test modules across tests and security-tests. Statement tests assert dated reversals, opening/closing balances and member isolation (tests/test_member_financials.py:26–37). The locking suite checks count and balance after concurrent requests, and seeds and writes records (security-tests/test_f06_money_locking.py:8–14,24–49). No tests run, coverage percentage and flakiness unverified. Root README.md:14–25 describes only the local SQLite path. Production and mobile operators need links to the VPS runbook and mobile scripts.

## Remediation sequence
Within one working day, rotate the exposed credential, check image distribution, restrict Compose port publication, reject missing MFA secrets, and deny public access to sensitive file directories while arranging an authenticated delivery path. Start with R01–R05. Strategic work should separate release migrations from worker boot, reconcile money before changing representation, and exercise the supported deployment and restore paths in isolation.

## Verification limits
All registered blueprint source files, mobile_api.py and app.py were parsed without importing the application. The route list covers explicit decorators plus the framework static rule. Handler bodies, template escaping, dependency graphs, payment flows and infrastructure were sampled. This is not a complete clearance of business authorization, injection, sensitive response fields, request validation, accounting correctness or production operations. No external services, database writes, deployment commands or probes were used. No pip-audit, safety or bandit executable on PATH; no online npm audit or advisory lookup was performed. Current CVEs, transitive Python resolution and production package versions are unverified. Only the cited secret path was traced into history; full historical secret coverage and actual use are unverified.

## Route inventory
Global hooks include payout protection, tenant suspension/billing, idle timeout, forced password change and staff MFA (app.py:119,449,478,531,549). Flask-WTF exemptions are listed above. GET also admits HEAD, and Flask ordinarily supplies OPTIONS. Decorators are shown as written. Inline/public status marked for review means no auth decorator was found, not a determination that access is unrestricted. Receipt, card, gateway and mobile endpoints can impose additional in-handler conditions.

| Method | Path | Handler | Local decorators / access | Evidence |
|---|---|---|---|---|
| GET | `/session/ping` | session_ping | Inline/public status requires handler review | app.py:574 |
| POST | `/api/mobile/login` | mobile_login | Inline/public status requires handler review | mobile_api.py:480 |
| POST | `/api/mobile/v1/auth/forgot-password` | mobile_forgot_password | Inline/public status requires handler review | mobile_api.py:544 |
| POST | `/api/mobile/v1/auth/change-password` | mobile_change_password | jwt_required | mobile_api.py:612 |
| POST | `/api/mobile/v1/auth/logout` | mobile_logout | jwt_required | mobile_api.py:647 |
| GET | `/api/mobile/v1/me` | mobile_me | member_required | mobile_api.py:675 |
| GET, PATCH | `/api/mobile/v1/profile` | mobile_profile | member_required | mobile_api.py:689 |
| GET | `/api/mobile/v1/dashboard` | mobile_v1_dashboard | member_required | mobile_api.py:768 |
| GET | `/api/mobile/dashboard` | mobile_dashboard | jwt_required | mobile_api.py:797 |
| GET | `/api/mobile/v1/savings` | mobile_savings | member_required | mobile_api.py:818 |
| GET | `/api/mobile/v1/loans` | mobile_loans | member_required | mobile_api.py:829 |
| GET | `/api/mobile/v1/assets` | mobile_assets | member_required | mobile_api.py:882 |
| POST | `/api/mobile/v1/assets/apply` | mobile_assets_apply | member_required | mobile_api.py:913 |
| POST | `/api/mobile/v1/assets/orders/<int:order_id>/cancel` | mobile_assets_cancel | member_required | mobile_api.py:1003 |
| POST | `/api/mobile/v1/assets/orders/<int:order_id>/accept-quote` | mobile_assets_accept_quote | member_required | mobile_api.py:1045 |
| POST | `/api/mobile/v1/assets/orders/<int:order_id>/decline-quote` | mobile_assets_decline_quote | member_required | mobile_api.py:1085 |
| POST | `/api/mobile/v1/assets/orders/<int:order_id>/request-revision` | mobile_assets_request_revision | member_required | mobile_api.py:1112 |
| POST | `/api/mobile/v1/assets/orders/<int:order_id>/confirm-receipt` | mobile_assets_confirm_receipt | member_required | mobile_api.py:1141 |
| GET, PATCH | `/api/mobile/v1/pay-in` | mobile_pay_in | member_required | mobile_api.py:1184 |
| GET | `/api/mobile/v1/ctas` | mobile_ctas | member_required | mobile_api.py:1252 |
| POST | `/api/mobile/v1/ctas/apply` | mobile_ctas_apply | member_required | mobile_api.py:1280 |
| GET | `/api/mobile/v1/loans/options` | mobile_loan_options | member_required | mobile_api.py:1319 |
| POST | `/api/mobile/v1/loans/schedule-preview` | mobile_loan_schedule_preview | member_required | mobile_api.py:1338 |
| POST | `/api/mobile/v1/loans/apply` | mobile_apply_loan | member_required | mobile_api.py:1374 |
| GET | `/api/mobile/v1/loans/<int:loan_id>` | mobile_loan_detail | member_required | mobile_api.py:1529 |
| GET | `/api/mobile/v1/loans/<int:loan_id>/application.pdf` | mobile_loan_application_pdf | member_required | mobile_api.py:1541 |
| POST | `/api/mobile/v1/loans/<int:loan_id>/withdraw` | mobile_withdraw_loan | member_required | mobile_api.py:1560 |
| GET | `/api/mobile/v1/notifications` | mobile_notifications | member_required | mobile_api.py:1610 |
| POST | `/api/mobile/v1/notifications/<int:notification_id>/read` | mobile_mark_notification_read | jwt_required | mobile_api.py:1620 |
| POST | `/api/mobile/v1/notifications/mark-all-read` | mobile_mark_all_notifications_read | jwt_required | mobile_api.py:1632 |
| POST | `/api/mobile/v1/devices` | mobile_register_device | member_required | mobile_api.py:1644 |
| GET | `/api/mobile/v1/tenant` | mobile_tenant | Inline/public status requires handler review | mobile_api.py:1679 |
| GET | `/api/hq/member-count` | hq_member_count | Inline/public status requires handler review | mobile_api.py:1698 |
| POST | `/api/hq/set-status` | hq_set_status | Inline/public status requires handler review | mobile_api.py:1709 |
| POST | `/api/hq/set-feature` | hq_set_feature | Inline/public status requires handler review | mobile_api.py:1726 |
| GET | `/api/mobile/v1/tenants/resolve` | mobile_resolve_tenant | Inline/public status requires handler review | mobile_api.py:1744 |
| GET | `/api/mobile/card` | mobile_card | jwt_required | mobile_api.py:1783 |
| POST | `/api/mobile/pay` | mobile_payment | jwt_required | mobile_api.py:1794 |
| GET | `/accounting/vouchers` | vouchers | login_required, role_required('admin', 'treasurer') | blueprints/accounting.py:28 |
| GET, POST | `/accounting/vouchers/new` | new_voucher | login_required, role_required('admin', 'treasurer') | blueprints/accounting.py:61 |
| GET | `/accounting/vouchers/<int:voucher_id>` | voucher_detail | login_required, role_required('admin', 'treasurer') | blueprints/accounting.py:88 |
| GET | `/accounting/chart` | chart_of_accounts | login_required, role_required('admin', 'treasurer') | blueprints/accounting.py:189 |
| POST | `/accounting/accounts/add` | add_account | login_required, role_required('admin') | blueprints/accounting.py:219 |
| POST | `/accounting/accounts/default-cash` | set_default_cash_account | login_required, role_required('admin') | blueprints/accounting.py:269 |
| POST | `/accounting/accounts/<code>/toggle` | toggle_account | login_required, role_required('admin') | blueprints/accounting.py:314 |
| POST | `/accounting/accounts/<code>/cash-toggle` | toggle_cash_account | login_required, role_required('admin') | blueprints/accounting.py:332 |
| GET, POST | `/accounting/journal/new` | new_journal | login_required, role_required('admin', 'treasurer') | blueprints/accounting.py:382 |
| GET | `/accounting/trial-balance` | trial_balance_view | login_required, role_required('admin', 'treasurer') | blueprints/accounting.py:430 |
| GET | `/accounting/reconciliation` | reconciliation | login_required, role_required('admin', 'treasurer') | blueprints/accounting.py:450 |
| GET | `/accounting/bank-accounts` | bank_accounts | login_required, role_required('admin', 'treasurer') | blueprints/accounting.py:459 |
| POST | `/accounting/bank-accounts/reclassify-savings` | reclassify_savings_bank_account | login_required, role_required('admin') | blueprints/accounting.py:509 |
| GET | `/accounting/bank-accounts/<code>` | bank_account_detail | login_required, role_required('admin', 'treasurer') | blueprints/accounting.py:567 |
| GET | `/accounting/dividends` | dividends | login_required, role_required('admin', 'treasurer') | blueprints/accounting.py:655 |
| POST | `/accounting/dividends/declare` | declare_dividend | login_required, role_required('admin') | blueprints/accounting.py:682 |
| GET | `/accounting/dividends/<int:decl_id>` | dividend_detail | login_required, role_required('admin', 'treasurer') | blueprints/accounting.py:715 |
| POST | `/accounting/backfill` | backfill | login_required, role_required('admin') | blueprints/accounting.py:757 |
| GET | `/accounting/ledger/<code>` | account_ledger_view | login_required, role_required('admin', 'treasurer') | blueprints/accounting.py:839 |
| GET | `/accounting/ledger/<code>/export` | account_ledger_export | login_required, role_required('admin', 'treasurer') | blueprints/accounting.py:857 |
| GET | `/accounting/journal/<int:entry_id>` | journal_entry_view | login_required, role_required('admin', 'treasurer') | blueprints/accounting.py:907 |
| GET | `/accounting/journal/<int:entry_id>/quick-view` | journal_entry_quick_view | login_required, role_required('admin', 'treasurer') | blueprints/accounting.py:929 |
| POST | `/accounting/journal/<int:entry_id>/reverse` | reverse_entry | login_required, role_required('admin', 'treasurer') | blueprints/accounting.py:955 |
| GET | `/accounting/period-close` | period_close | login_required, role_required('admin', 'treasurer') | blueprints/accounting.py:990 |
| POST | `/accounting/period-close/set` | set_lock_date | login_required, role_required('admin') | blueprints/accounting.py:1008 |
| GET | `/accounting/journal` | journal_register | login_required, role_required('admin', 'treasurer') | blueprints/accounting.py:1043 |
| GET | `/accounting/journal/export` | journal_register_export | login_required, role_required('admin', 'treasurer') | blueprints/accounting.py:1069 |
| GET | `/upload-history` | upload_history | login_required, role_required('admin', 'treasurer', 'secretary') | blueprints/admin_panel.py:27 |
| GET | `/settings` | settings | login_required, role_required('admin') | blueprints/admin_panel.py:361 |
| POST | `/settings/update` | update_settings | login_required, role_required('admin') | blueprints/admin_panel.py:430 |
| POST | `/settings/security` | update_security_settings | login_required, role_required('admin') | blueprints/admin_panel.py:489 |
| POST | `/settings/reconcile-savings` | reconcile_savings | login_required, role_required('admin') | blueprints/admin_panel.py:508 |
| GET | `/api/readiness` | readiness_status | login_required, role_required('admin') | blueprints/admin_panel.py:529 |
| GET | `/expenses` | expenses | login_required, role_required('admin', 'treasurer') | blueprints/admin_panel.py:537 |
| GET, POST | `/expenses/add` | add_expense | login_required, role_required('admin', 'treasurer') | blueprints/admin_panel.py:549 |
| GET | `/revenue` | revenue | login_required, role_required('admin', 'treasurer') | blueprints/admin_panel.py:590 |
| GET, POST | `/revenue/add` | add_revenue | login_required, role_required('admin', 'treasurer') | blueprints/admin_panel.py:600 |
| GET | `/honorarium` | honorarium | login_required, role_required('admin') | blueprints/admin_panel.py:640 |
| POST | `/honorarium/add` | add_honorarium | login_required, role_required('admin') | blueprints/admin_panel.py:654 |
| GET | `/api/member/<int:member_id>` | get_member_api | login_required, role_required('admin', 'treasurer', 'secretary', 'exco') | blueprints/admin_panel.py:687 |
| POST | `/api/add_user` | add_user | login_required, role_required('admin') | blueprints/admin_panel.py:713 |
| POST | `/api/edit_user/<int:user_id>` | edit_user | login_required, role_required('admin') | blueprints/admin_panel.py:797 |
| POST | `/api/reset_user_password/<int:user_id>` | reset_user_password | login_required, role_required('admin') | blueprints/admin_panel.py:837 |
| POST | `/api/reset_user_2fa/<int:user_id>` | reset_user_2fa | login_required, role_required('admin') | blueprints/admin_panel.py:866 |
| POST | `/api/resend_setup_link/<int:user_id>` | resend_setup_link | login_required, role_required('admin') | blueprints/admin_panel.py:889 |
| POST | `/api/bulk_send_setup_links` | bulk_send_setup_links | login_required, role_required('admin') | blueprints/admin_panel.py:927 |
| POST | `/api/revoke_setup_links/<int:user_id>` | revoke_setup_links | login_required, role_required('admin') | blueprints/admin_panel.py:977 |
| POST | `/api/toggle_super_admin/<int:user_id>` | toggle_super_admin | login_required, role_required('admin') | blueprints/admin_panel.py:1001 |
| POST | `/api/toggle_user/<int:user_id>` | toggle_user | login_required, role_required('admin') | blueprints/admin_panel.py:1031 |
| POST | `/api/mobile_devices/<int:device_id>/test-push` | test_mobile_device_push | login_required, role_required('admin') | blueprints/admin_panel.py:1056 |
| POST | `/api/mobile_devices/<int:device_id>/revoke` | revoke_mobile_device | login_required, role_required('admin') | blueprints/admin_panel.py:1087 |
| GET | `/api/test_db` | test_db | login_required, role_required('admin') | blueprints/admin_panel.py:1112 |
| POST | `/settings/update-mail` | update_mail_settings | login_required, role_required('admin') | blueprints/admin_panel.py:1125 |
| POST | `/settings/test-mail` | test_mail | login_required, role_required('admin') | blueprints/admin_panel.py:1167 |
| POST | `/settings/update-sms` | update_sms_settings | login_required, role_required('admin') | blueprints/admin_panel.py:1198 |
| POST | `/settings/test-sms` | test_sms | login_required, role_required('admin') | blueprints/admin_panel.py:1240 |
| GET | `/subscription` | subscription_page | login_required, role_required('admin', 'treasurer') | blueprints/admin_panel.py:1283 |
| GET | `/subscription/callback` | subscription_callback | login_required, role_required('admin', 'treasurer') | blueprints/admin_panel.py:1345 |
| GET | `/task-assignment` | task_assignment | login_required, role_required('admin') | blueprints/admin_panel.py:1469 |
| POST | `/task-assignment/roles` | update_role_permissions | login_required, role_required('admin') | blueprints/admin_panel.py:1498 |
| POST | `/task-assignment/reset` | reset_permissions | login_required, role_required('admin') | blueprints/admin_panel.py:1530 |
| GET | `/task-assignment/officer/<int:user_id>` | user_permissions | login_required, role_required('admin') | blueprints/admin_panel.py:1548 |
| POST | `/task-assignment/officer/<int:user_id>` | update_user_permissions | login_required, role_required('admin') | blueprints/admin_panel.py:1572 |
| POST | `/task-assignment/officer/<int:user_id>/reset` | reset_user_permissions | login_required, role_required('admin') | blueprints/admin_panel.py:1605 |
| GET, POST | `/affiliates/apply` | apply | public_route, csrf.exempt | blueprints/affiliates.py:173 |
| GET, POST | `/affiliates/accept/<token>` | accept_appointment | public_route, csrf.exempt | blueprints/affiliates.py:214 |
| GET | `/hq/affiliates` | register | hq_admin_required | blueprints/affiliates.py:308 |
| POST | `/hq/affiliates/<int:aff_id>/review` | review | hq_admin_required | blueprints/affiliates.py:338 |
| POST | `/hq/affiliates/<int:aff_id>/resend` | resend_appointment | hq_admin_required | blueprints/affiliates.py:391 |
| POST | `/hq/affiliates/<int:aff_id>/team` | set_team | hq_admin_required | blueprints/affiliates.py:407 |
| GET | `/hq/affiliates/attribution` | attribution | hq_admin_required | blueprints/affiliates.py:536 |
| POST | `/hq/affiliates/leads/<int:lead_id>/code` | fix_lead_code | hq_admin_required | blueprints/affiliates.py:559 |
| POST | `/hq/affiliates/clients/<int:client_id>/link` | link_client | hq_admin_required | blueprints/affiliates.py:590 |
| GET | `/hq/affiliates/commissions` | commissions | hq_admin_required | blueprints/affiliates.py:784 |
| POST | `/hq/affiliates/commission-jobs/<int:job_id>/retry` | retry_commission | hq_admin_required | blueprints/affiliates.py:816 |
| POST | `/hq/affiliates/rates` | save_rates | hq_admin_required | blueprints/affiliates.py:831 |
| GET, POST | `/affiliates/statement` | request_statement | public_route, csrf.exempt | blueprints/affiliates.py:1014 |
| GET | `/affiliates/statement/<token>` | statement | public_route | blueprints/affiliates.py:1048 |
| GET | `/hq/affiliates/<int:aff_id>/statement` | statement_as_admin | hq_admin_required | blueprints/affiliates.py:1061 |
| GET | `/asset-schemes` | catalogue | login_required | blueprints/asset_schemes.py:145 |
| POST | `/asset-schemes/apply/<int:campaign_id>` | apply | login_required | blueprints/asset_schemes.py:169 |
| GET | `/asset-schemes/orders/<int:order_id>` | order_detail | login_required | blueprints/asset_schemes.py:265 |
| GET, POST | `/asset-schemes/manage` | manage | login_required, role_required('admin', 'treasurer', 'secretary') | blueprints/asset_schemes.py:287 |
| POST | `/asset-schemes/campaigns/<int:campaign_id>/settings` | update_campaign | login_required, role_required('admin', 'treasurer') | blueprints/asset_schemes.py:343 |
| GET, POST | `/tasks/asset-schemes/newsletters` | asset_campaign_newsletters | Inline/public status requires handler review | blueprints/asset_schemes.py:387 |
| POST | `/asset-schemes/campaigns/<int:campaign_id>/library` | add_from_library | login_required, role_required('admin', 'treasurer') | blueprints/asset_schemes.py:421 |
| POST | `/asset-schemes/library/<int:item_id>` | update_library_item | login_required, role_required('admin', 'treasurer') | blueprints/asset_schemes.py:466 |
| POST | `/asset-schemes/library` | create_library_item | login_required, role_required('admin', 'treasurer') | blueprints/asset_schemes.py:524 |
| POST | `/asset-schemes/campaigns/<int:campaign_id>/products` | add_product | login_required, role_required('admin', 'treasurer') | blueprints/asset_schemes.py:570 |
| POST | `/asset-schemes/orders/<int:order_id>/action` | staff_action | login_required, role_required('admin', 'treasurer', 'secretary') | blueprints/asset_schemes.py:595 |
| POST | `/asset-schemes/orders/<int:order_id>/cancel` | cancel_request | login_required | blueprints/asset_schemes.py:764 |
| POST | `/asset-schemes/orders/<int:order_id>/accept-quote` | accept_quote | login_required | blueprints/asset_schemes.py:800 |
| POST | `/asset-schemes/orders/<int:order_id>/decline-quote` | decline_quote | login_required | blueprints/asset_schemes.py:831 |
| POST | `/asset-schemes/orders/<int:order_id>/request-revision` | request_quote_revision | login_required | blueprints/asset_schemes.py:858 |
| GET | `/asset-schemes/orders/<int:order_id>/<document_type>.pdf` | asset_document_pdf | login_required | blueprints/asset_schemes.py:887 |
| POST | `/asset-schemes/orders/<int:order_id>/confirm-receipt` | confirm_receipt | login_required | blueprints/asset_schemes.py:905 |
| POST | `/asset-schemes/orders/<int:order_id>/confirm-receipt-officer` | confirm_receipt_officer | login_required, role_required('admin', 'treasurer', 'secretary') | blueprints/asset_schemes.py:929 |
| GET | `/` | index | Inline/public status requires handler review | blueprints/auth.py:123 |
| GET | `/manifest.webmanifest` | web_manifest | Inline/public status requires handler review | blueprints/auth.py:128 |
| GET, POST | `/login` | login | Inline/public status requires handler review | blueprints/auth.py:166 |
| GET, POST | `/login/verify` | verify_2fa | Inline/public status requires handler review | blueprints/auth.py:252 |
| GET, POST | `/forgot-password` | forgot_password | Inline/public status requires handler review | blueprints/auth.py:296 |
| GET, POST | `/reset-password/<token>` | reset_password | Inline/public status requires handler review | blueprints/auth.py:356 |
| GET, POST | `/setup-password/<token>` | setup_password | Inline/public status requires handler review | blueprints/auth.py:410 |
| GET | `/logout` | logout | login_required | blueprints/auth.py:464 |
| GET | `/setup` | setup | Inline/public status requires handler review | blueprints/auth.py:488 |
| GET | `/debug-auth` | debug_auth | Inline/public status requires handler review | blueprints/auth.py:493 |
| GET, POST | `/emergency-reset` | emergency_reset | Inline/public status requires handler review | blueprints/auth.py:498 |
| GET | `/member/generate-card/<int:member_id>` | generate_member_card | login_required, role_required('admin', 'secretary') | blueprints/cards.py:13 |
| GET | `/member/view-card/<int:member_id>` | view_member_card | login_required | blueprints/cards.py:53 |
| GET | `/member/download-card/<int:member_id>` | download_member_card | login_required | blueprints/cards.py:78 |
| GET | `/verify-card/<token>` | verify_card | Inline/public status requires handler review | blueprints/cards.py:103 |
| GET | `/test-email` | test_email | login_required, role_required('admin') | blueprints/cards.py:112 |
| GET | `/communications/` | index | login_required, role_required('admin', 'secretary') | blueprints/communications.py:336 |
| GET, POST | `/communications/new` | new_campaign | login_required, role_required('admin', 'secretary') | blueprints/communications.py:433 |
| GET | `/communications/<int:campaign_id>` | campaign_detail | login_required, role_required('admin', 'secretary') | blueprints/communications.py:522 |
| POST | `/communications/<int:campaign_id>/retry` | retry_campaign | login_required, role_required('admin', 'secretary') | blueprints/communications.py:545 |
| POST | `/ctas/enable` | enable_ctas | login_required | blueprints/ctas.py:292 |
| POST | `/ctas/disable` | disable_ctas | login_required | blueprints/ctas.py:306 |
| GET | `/ctas` | dashboard | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:321 |
| POST | `/ctas/cycles` | new_cycle | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:336 |
| GET | `/ctas/overview` | overview | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:401 |
| GET | `/ctas/plans` | plans | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:513 |
| POST | `/ctas/plans` | new_plan | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:523 |
| GET | `/ctas/cycles/<int:cycle_id>` | cycle_detail | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:565 |
| GET, POST | `/ctas/cycles/<int:cycle_id>/promote` | promote_cycle | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:731 |
| POST | `/ctas/cycles/<int:cycle_id>/delete` | delete_cycle | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:778 |
| POST | `/ctas/cycles/<int:cycle_id>/transition` | cycle_transition | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:814 |
| POST | `/ctas/cycles/<int:cycle_id>/subscriptions` | add_subscription | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:853 |
| POST | `/ctas/subscriptions/<int:sub_id>/act` | subscription_act | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:899 |
| POST | `/ctas/cycles/<int:cycle_id>/liquidity` | set_liquidity | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:998 |
| POST | `/ctas/cycles/<int:cycle_id>/security` | set_security | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:1024 |
| POST | `/ctas/cycles/<int:cycle_id>/priority-fees` | set_priority_fees | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:1050 |
| POST | `/my-ctas/<int:sub_id>/priority` | request_priority | ctas_required, login_required | blueprints/ctas.py:1082 |
| POST | `/ctas/cycles/<int:cycle_id>/bulk-advance` | bulk_advance | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:1127 |
| POST | `/ctas/subscriptions/<int:sub_id>/terms` | record_terms | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:1201 |
| POST | `/ctas/subscriptions/<int:sub_id>/priority` | decide_priority | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:1227 |
| POST | `/ctas/cycles/<int:cycle_id>/ballot` | run_ballot | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:1269 |
| POST | `/ctas/subscriptions/<int:sub_id>/payout` | payout | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:1339 |
| GET | `/ctas/cycles/<int:cycle_id>/payroll/export` | payroll_export | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:1409 |
| POST | `/ctas/cycles/<int:cycle_id>/payroll/import` | payroll_import | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:1452 |
| GET | `/my-ctas` | my_ctas | ctas_required, login_required | blueprints/ctas.py:1557 |
| POST | `/my-ctas/<int:sub_id>/guarantors` | request_guarantor | ctas_required, login_required | blueprints/ctas.py:1628 |
| POST | `/my-ctas/guarantors/<int:gid>/withdraw` | withdraw_guarantor | ctas_required, login_required | blueprints/ctas.py:1681 |
| POST | `/my-ctas/guarantees/<int:gid>/<action>` | respond_ctas_guarantee | ctas_required, login_required | blueprints/ctas.py:1704 |
| GET | `/my-ctas/guarantees` | my_guarantees | ctas_required, login_required | blueprints/ctas.py:1754 |
| POST | `/my-ctas/apply` | my_ctas_apply | ctas_required, login_required | blueprints/ctas.py:1777 |
| GET, POST | `/my-ctas/autopay/<int:sub_id>` | autopay_setup | ctas_required, login_required | blueprints/ctas.py:1863 |
| GET | `/my-ctas/autopay/callback` | autopay_callback | ctas_required, login_required | blueprints/ctas.py:1939 |
| POST | `/my-ctas/autopay/<int:mandate_id>/cancel` | autopay_cancel | ctas_required, login_required | blueprints/ctas.py:2027 |
| GET, POST | `/tasks/ctas/charge-due` | ctas_charge_due | Inline/public status requires handler review | blueprints/ctas.py:2046 |
| GET, POST | `/ctas/subscriptions/<int:sub_id>/exit` | exit_settle | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:2177 |
| GET | `/ctas/exceptions` | exceptions | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:2350 |
| POST | `/ctas/exceptions/<int:case_id>/resolve` | resolve_exception | ctas_required, role_required('admin', 'treasurer') | blueprints/ctas.py:2367 |
| GET | `/feedback/` | form | login_required | blueprints/feedback.py:81 |
| POST | `/feedback/` | submit | login_required | blueprints/feedback.py:92 |
| POST | `/feedback/dismiss` | dismiss | login_required | blueprints/feedback.py:143 |
| GET | `/feedback/admin` | admin | login_required, role_required('admin') | blueprints/feedback.py:157 |
| POST | `/feedback/task` | submit_task | login_required | blueprints/feedback.py:209 |
| GET | `/feedback/admin/referrals.csv` | export_referrals | login_required, role_required('admin') | blueprints/feedback.py:238 |
| GET | `/events` | events_list | login_required | blueprints/governance.py:175 |
| GET | `/minutes` | minutes_list | login_required | blueprints/governance.py:193 |
| GET | `/minutes/<int:minute_id>/download` | minutes_download | login_required | blueprints/governance.py:203 |
| GET | `/governance` | manage | login_required, role_required('admin', 'secretary') | blueprints/governance.py:218 |
| POST | `/governance/events/add` | add_event | login_required, role_required('admin', 'secretary') | blueprints/governance.py:239 |
| POST | `/governance/events/<int:event_id>/toggle` | toggle_event | login_required, role_required('admin', 'secretary') | blueprints/governance.py:279 |
| POST | `/governance/events/<int:event_id>/delete` | delete_event | login_required, role_required('admin', 'secretary') | blueprints/governance.py:292 |
| GET | `/events/<int:event_id>` | event_detail | login_required | blueprints/governance.py:306 |
| POST | `/events/<int:event_id>/rsvp` | rsvp | login_required | blueprints/governance.py:341 |
| GET, POST | `/governance/events/<int:event_id>/edit` | edit_event | login_required, role_required('admin', 'secretary') | blueprints/governance.py:365 |
| POST | `/governance/events/<int:event_id>/attendance` | mark_attendance | login_required, role_required('admin', 'secretary') | blueprints/governance.py:400 |
| GET | `/events/calendar` | calendar_view | login_required | blueprints/governance.py:424 |
| GET, POST | `/governance/reminders/run` | run_reminders | Inline/public status requires handler review | blueprints/governance.py:456 |
| POST | `/governance/minutes/upload` | upload_minutes | login_required, role_required('admin', 'secretary') | blueprints/governance.py:474 |
| POST | `/governance/minutes/<int:minute_id>/delete` | delete_minutes | login_required, role_required('admin', 'secretary') | blueprints/governance.py:511 |
| GET | `/help/` | knowledge_base | login_required | blueprints/help_bp.py:1584 |
| GET | `/help/article/<slug>` | article | login_required | blueprints/help_bp.py:1601 |
| GET | `/help/api/panel` | panel_api | login_required | blueprints/help_bp.py:1619 |
| GET | `/hq/clients` | clients | hq_admin_required | blueprints/hq_billing.py:345 |
| POST | `/hq/clients` | add_client | hq_admin_required | blueprints/hq_billing.py:362 |
| POST | `/hq/clients/<int:client_id>/edit` | edit_client | hq_admin_required | blueprints/hq_billing.py:396 |
| POST | `/hq/billing-settings` | billing_settings | hq_admin_required | blueprints/hq_billing.py:437 |
| POST | `/hq/setup-bands` | save_setup_bands | hq_admin_required | blueprints/hq_billing.py:454 |
| POST | `/hq/clients/<int:client_id>/set-access` | set_client_access | hq_admin_required | blueprints/hq_billing.py:511 |
| POST | `/hq/clients/<int:client_id>/feature` | set_client_feature | hq_admin_required | blueprints/hq_billing.py:537 |
| POST | `/hq/clients/sync-members` | sync_members | hq_admin_required | blueprints/hq_billing.py:565 |
| GET | `/hq/invoices` | invoices | hq_admin_required | blueprints/hq_billing.py:602 |
| POST | `/hq/invoices/new` | new_invoice | hq_admin_required | blueprints/hq_billing.py:689 |
| POST | `/hq/invoices/bulk-subscription` | bulk_subscription | hq_admin_required | blueprints/hq_billing.py:767 |
| GET | `/hq/invoices/<int:invoice_id>` | invoice_detail | hq_admin_required | blueprints/hq_billing.py:791 |
| POST | `/hq/invoices/<int:invoice_id>/edit` | edit_invoice | hq_admin_required | blueprints/hq_billing.py:801 |
| POST | `/hq/invoices/<int:invoice_id>/duplicate` | duplicate_invoice | hq_admin_required | blueprints/hq_billing.py:877 |
| POST | `/hq/invoices/<int:invoice_id>/mark-paid` | mark_paid | hq_admin_required | blueprints/hq_billing.py:921 |
| POST | `/hq/invoices/<int:invoice_id>/void` | void_invoice | hq_admin_required | blueprints/hq_billing.py:946 |
| POST | `/hq/invoices/<int:invoice_id>/delete` | delete_invoice | hq_admin_required | blueprints/hq_billing.py:964 |
| GET | `/hq/invoices/<int:invoice_id>.pdf` | invoice_pdf | hq_admin_required | blueprints/hq_billing.py:990 |
| POST | `/hq/invoices/<int:invoice_id>/send` | send_invoice | hq_admin_required | blueprints/hq_billing.py:1005 |
| GET | `/hq/pay/<path:invoice_number>/<token>` | pay_invoice | Inline/public status requires handler review | blueprints/hq_billing.py:1044 |
| GET | `/hq/pay/callback` | pay_callback | Inline/public status requires handler review | blueprints/hq_billing.py:1081 |
| GET | `/investments` | investments_list | login_required, role_required('admin', 'treasurer') | blueprints/investments.py:14 |
| GET, POST | `/investments/add` | add_investment | login_required, role_required('admin', 'treasurer') | blueprints/investments.py:26 |
| GET | `/loans/payroll-book` | payroll_loan_book | login_required, role_required('admin', 'treasurer') | blueprints/loans.py:55 |
| GET, POST | `/loans/payroll-adapter` | payroll_repayment_adapter | login_required, role_required('admin', 'treasurer') | blueprints/loans.py:77 |
| GET | `/loans/download-repayment-template` | download_repayment_template | login_required, role_required('admin', 'treasurer') | blueprints/loans.py:325 |
| GET | `/loans` | loans_list | login_required, role_required('admin', 'treasurer', 'secretary', 'exco') | blueprints/loans.py:340 |
| GET, POST | `/loans/corrections` | loan_corrections | login_required, role_required('admin', 'treasurer') | blueprints/loans.py:475 |
| GET, POST | `/loans/apply` | apply_loan | login_required, role_required('admin', 'treasurer', 'secretary', 'exco') | blueprints/loans.py:576 |
| GET | `/loans/<int:loan_id>` | loan_detail | login_required, role_required('admin', 'treasurer', 'secretary', 'exco') | blueprints/loans.py:709 |
| POST | `/loans/<int:loan_id>/due-diligence` | update_due_diligence | login_required, role_required('admin', 'treasurer', 'secretary') | blueprints/loans.py:760 |
| GET | `/loans/<int:loan_id>/application.pdf` | loan_application_pdf | login_required, role_required('admin', 'treasurer', 'secretary', 'exco') | blueprints/loans.py:819 |
| POST | `/loans/<int:loan_id>/resend-alert` | resend_loan_alert | login_required, role_required('admin', 'treasurer', 'secretary') | blueprints/loans.py:838 |
| GET, POST | `/tasks/loans/pipeline-sweep` | loan_pipeline_sweep | Inline/public status requires handler review | blueprints/loans.py:859 |
| POST | `/loans/<int:loan_id>/act` | loan_act | login_required, role_required('admin', 'treasurer', 'secretary') | blueprints/loans.py:891 |
| POST | `/loans/<int:loan_id>/cancel` | cancel_loan_application | login_required, role_required('admin', 'treasurer', 'secretary', 'exco') | blueprints/loans.py:1004 |
| GET, POST | `/loans/bulk-repayments` | bulk_loan_repayments | login_required, role_required('admin', 'treasurer') | blueprints/loans.py:1071 |
| GET | `/loans/export` | export_loans | login_required, role_required('admin', 'treasurer') | blueprints/loans.py:1299 |
| GET | `/loans/export-statements` | export_loan_statements | login_required, role_required('admin', 'treasurer') | blueprints/loans.py:1332 |
| POST | `/loans/repay/<int:loan_id>` | repay_loan | login_required, role_required('admin', 'treasurer') | blueprints/loans.py:1467 |
| GET | `/loans/fee-refunds` | fee_refunds | login_required, role_required('admin', 'treasurer') | blueprints/loans.py:1636 |
| GET | `/loans/fee-refunds/export.csv` | fee_refunds_export | login_required, role_required('admin', 'treasurer') | blueprints/loans.py:1675 |
| POST | `/loans/fee-refunds/pay` | fee_refunds_pay | login_required, role_required('admin', 'treasurer') | blueprints/loans.py:1767 |
| GET | `/dashboard` | dashboard | login_required | blueprints/main.py:14 |
| GET | `/marketing/leads` | leads_inbox | login_required, role_required('admin', 'secretary') | blueprints/marketing.py:283 |
| POST | `/marketing/leads/<int:lead_id>/status` | update_lead_status | login_required, role_required('admin', 'secretary') | blueprints/marketing.py:350 |
| GET | `/marketing/leads/<int:lead_id>` | lead_detail | login_required, role_required('admin', 'secretary') | blueprints/marketing.py:387 |
| POST | `/marketing/leads/<int:lead_id>/pipeline` | update_lead_pipeline | login_required, role_required('admin', 'secretary') | blueprints/marketing.py:413 |
| POST | `/marketing/leads/<int:lead_id>/workflow` | update_lead_workflow | login_required, role_required('admin', 'secretary') | blueprints/marketing.py:487 |
| POST | `/marketing/leads/<int:lead_id>/activity` | add_lead_activity | login_required, role_required('admin', 'secretary') | blueprints/marketing.py:521 |
| GET | `/marketing/leads/export.csv` | export_leads | login_required, role_required('admin', 'secretary') | blueprints/marketing.py:563 |
| POST | `/api/marketing/leads` | capture_lead | csrf.exempt | blueprints/marketing.py:598 |
| GET, POST | `/receipts/member-payment` | member_payment | login_required, role_required('admin', 'treasurer') | blueprints/member_receipts.py:31 |
| POST | `/receipts/<int:receipt_id>/reverse` | reverse_member_payment | login_required, role_required('admin', 'treasurer') | blueprints/member_receipts.py:249 |
| GET | `/members/<int:member_id>/financial-statement` | financial_statement | login_required, role_required('admin', 'secretary', 'treasurer', 'exco') | blueprints/members.py:25 |
| GET | `/members/<int:member_id>/savings-history` | savings_history | login_required, role_required('admin', 'secretary', 'treasurer', 'exco') | blueprints/members.py:66 |
| GET | `/members` | members_list | login_required, role_required('admin', 'secretary') | blueprints/members.py:94 |
| GET | `/members/<int:member_id>` | member_details | login_required, role_required('admin', 'secretary', 'treasurer', 'exco') | blueprints/members.py:112 |
| GET | `/members/<int:member_id>/savings-statement` | member_savings_statement | login_required, role_required('admin', 'secretary', 'treasurer', 'exco') | blueprints/members.py:149 |
| GET, POST | `/members/add` | add_member | login_required, role_required('admin', 'secretary') | blueprints/members.py:222 |
| GET, POST | `/members/edit/<int:member_id>` | edit_member | login_required, role_required('admin', 'secretary') | blueprints/members.py:338 |
| POST | `/members/delete/<int:member_id>` | delete_member | login_required, role_required('admin', 'secretary') | blueprints/members.py:444 |
| POST | `/members/<int:member_id>/mark-former` | mark_former | login_required, role_required('admin', 'secretary') | blueprints/members.py:479 |
| POST | `/members/<int:member_id>/reinstate` | reinstate_member | login_required, role_required('admin', 'secretary') | blueprints/members.py:511 |
| GET, POST | `/members/bulk-upload` | bulk_upload_members | login_required, role_required('admin', 'secretary') | blueprints/members.py:536 |
| GET | `/members/download-template` | download_template | login_required, role_required('admin', 'secretary') | blueprints/members.py:617 |
| GET | `/members/export` | export_members | login_required, role_required('admin', 'secretary') | blueprints/members.py:632 |
| GET | `/members/<int:member_id>/card` | member_card | login_required, role_required('admin', 'secretary', 'treasurer', 'exco') | blueprints/members.py:670 |
| GET | `/savings-requests` | savings_requests | login_required, role_required('admin', 'secretary', 'treasurer') | blueprints/members.py:702 |
| POST | `/savings-requests/<int:req_id>/act` | savings_request_act | login_required, role_required('admin', 'secretary', 'treasurer') | blueprints/members.py:721 |
| GET | `/migration/` | index | login_required, role_required('admin') | blueprints/migration.py:145 |
| GET | `/migration/opening-balances/template` | template_opening | login_required, role_required('admin') | blueprints/migration.py:171 |
| GET, POST | `/migration/opening-balances` | import_opening | login_required, role_required('admin') | blueprints/migration.py:185 |
| GET, POST | `/migration/members` | import_members | login_required, role_required('admin', 'secretary') | blueprints/migration.py:302 |
| GET | `/migration/members/template` | template_members | login_required, role_required('admin', 'secretary') | blueprints/migration.py:468 |
| GET | `/migration/members/export` | export_members | login_required, role_required('admin', 'secretary') | blueprints/migration.py:492 |
| GET, POST | `/migration/savings` | import_savings | login_required, role_required('admin', 'treasurer') | blueprints/migration.py:530 |
| GET | `/migration/savings/template` | template_savings | login_required, role_required('admin', 'treasurer') | blueprints/migration.py:641 |
| GET | `/migration/savings/export` | export_savings | login_required, role_required('admin', 'treasurer') | blueprints/migration.py:657 |
| GET, POST | `/migration/loans` | import_loans | login_required, role_required('admin', 'treasurer') | blueprints/migration.py:694 |
| GET | `/migration/loans/template` | template_loans | login_required, role_required('admin', 'treasurer') | blueprints/migration.py:798 |
| GET | `/migration/loans/export` | export_loans | login_required, role_required('admin', 'treasurer') | blueprints/migration.py:820 |
| GET, POST | `/migration/repayments` | import_repayments | login_required, role_required('admin', 'treasurer') | blueprints/migration.py:854 |
| GET | `/migration/repayments/template` | template_repayments | login_required, role_required('admin', 'treasurer') | blueprints/migration.py:1000 |
| GET | `/migration/repayments/export` | export_repayments | login_required, role_required('admin', 'treasurer') | blueprints/migration.py:1022 |
| GET, POST | `/migration/expenses` | import_expenses | login_required, role_required('admin', 'treasurer') | blueprints/migration.py:1055 |
| GET | `/migration/expenses/template` | template_expenses | login_required, role_required('admin', 'treasurer') | blueprints/migration.py:1125 |
| GET | `/migration/expenses/export` | export_expenses | login_required, role_required('admin', 'treasurer') | blueprints/migration.py:1139 |
| GET, POST | `/migration/revenue` | import_revenue | login_required, role_required('admin', 'treasurer') | blueprints/migration.py:1166 |
| GET | `/migration/revenue/template` | template_revenue | login_required, role_required('admin', 'treasurer') | blueprints/migration.py:1231 |
| GET | `/migration/revenue/export` | export_revenue | login_required, role_required('admin', 'treasurer') | blueprints/migration.py:1245 |
| GET, POST | `/migration/investments` | import_investments | login_required, role_required('admin', 'treasurer') | blueprints/migration.py:1273 |
| GET | `/migration/investments/template` | template_investments | login_required, role_required('admin', 'treasurer') | blueprints/migration.py:1350 |
| GET | `/migration/investments/export` | export_investments | login_required, role_required('admin', 'treasurer') | blueprints/migration.py:1366 |
| GET, POST | `/migration/honorarium` | import_honorarium | login_required, role_required('admin') | blueprints/migration.py:1395 |
| GET | `/migration/honorarium/template` | template_honorarium | login_required, role_required('admin') | blueprints/migration.py:1464 |
| GET | `/migration/honorarium/export` | export_honorarium | login_required, role_required('admin') | blueprints/migration.py:1478 |
| POST | `/migration/load-demo` | load_demo | login_required, role_required('admin') | blueprints/migration.py:1515 |
| POST | `/migration/purge` | purge_database | login_required, role_required('admin') | blueprints/migration.py:1541 |
| POST | `/admin/pay/savings` | initiate_savings | login_required | blueprints/payments_bp.py:196 |
| POST | `/admin/pay/loan/<int:loan_id>` | initiate_loan_repayment | login_required | blueprints/payments_bp.py:263 |
| GET | `/admin/pay/callback/<gateway>` | payment_callback | login_required | blueprints/payments_bp.py:334 |
| POST | `/webhooks/paystack` | paystack_webhook | Inline/public status requires handler review | blueprints/payments_bp.py:399 |
| POST | `/webhooks/flutterwave` | flutterwave_webhook | Inline/public status requires handler review | blueprints/payments_bp.py:499 |
| POST | `/member/view-as-member` | view_as_member | login_required | blueprints/portal.py:140 |
| POST | `/member/back-to-admin` | back_to_admin | login_required | blueprints/portal.py:161 |
| GET | `/member/portal` | member_portal | login_required | blueprints/portal.py:171 |
| GET | `/member/link-required` | member_link_required | login_required | blueprints/portal.py:232 |
| POST | `/payment-preference` | set_payment_preference | login_required | blueprints/portal.py:257 |
| GET | `/my-savings` | my_savings | login_required | blueprints/portal.py:287 |
| GET | `/saving-detail/<int:saving_id>` | saving_detail | login_required | blueprints/portal.py:457 |
| GET | `/my-loans` | my_loans | login_required | blueprints/portal.py:472 |
| GET | `/loan-detail/<int:loan_id>` | loan_detail | login_required | blueprints/portal.py:566 |
| GET | `/loan-detail/<int:loan_id>/application.pdf` | my_loan_application_pdf | login_required | blueprints/portal.py:650 |
| POST | `/loan-detail/<int:loan_id>/withdraw` | withdraw_loan_application | login_required | blueprints/portal.py:671 |
| GET | `/my-guarantor-requests` | my_guarantor_requests | login_required | blueprints/portal.py:729 |
| POST | `/my-guarantor-requests/<int:gid>/<action>` | respond_guarantor | login_required | blueprints/portal.py:749 |
| GET | `/loan-schedule-preview` | loan_schedule_preview | login_required | blueprints/portal.py:786 |
| GET, POST | `/apply-loan-member` | apply_loan_member | login_required | blueprints/portal.py:816 |
| GET, POST | `/change-savings-request` | change_savings_request | login_required | blueprints/portal.py:998 |
| GET | `/loan-calculator` | loan_calculator | login_required | blueprints/portal.py:1076 |
| GET | `/my-cards` | my_cards | login_required | blueprints/portal.py:1090 |
| GET | `/profile` | profile | login_required | blueprints/portal.py:1103 |
| POST | `/profile/reveal-sensitive` | reveal_sensitive_profile_fields | login_required | blueprints/portal.py:1114 |
| GET, POST | `/edit-profile` | edit_profile | login_required | blueprints/portal.py:1166 |
| GET, POST | `/change-password` | change_password | login_required | blueprints/portal.py:1284 |
| GET, POST | `/nominee` | nominee | login_required | blueprints/portal.py:1323 |
| GET | `/transactions` | transactions | login_required | blueprints/portal.py:1335 |
| GET | `/statements` | statements | login_required | blueprints/portal.py:1448 |
| GET | `/notifications` | notifications | login_required | blueprints/portal.py:1619 |
| POST | `/notifications/mark-read/<int:notif_id>` | mark_notification_read | login_required | blueprints/portal.py:1647 |
| POST | `/notifications/mark-all-read` | mark_all_notifications_read | login_required | blueprints/portal.py:1657 |
| GET | `/notification/<int:notif_id>` | open_notification | login_required | blueprints/portal.py:1667 |
| GET, POST | `/support` | support | login_required | blueprints/portal.py:1690 |
| GET | `/member/statement/<int:member_id>` | member_statement | login_required | blueprints/portal.py:1731 |
| GET | `/reports` | reports_list | login_required, role_required('admin', 'treasurer', 'secretary', 'exco') | blueprints/reports.py:157 |
| GET | `/reports/financial` | financial_report | login_required, role_required('admin', 'treasurer', 'secretary', 'exco') | blueprints/reports.py:197 |
| GET | `/reports/cashbook` | cashbook_report | login_required, role_required('admin', 'treasurer') | blueprints/reports.py:266 |
| GET | `/reports/member-savings-control` | member_savings_control | login_required, role_required('admin', 'treasurer', 'secretary', 'exco') | blueprints/reports.py:317 |
| GET | `/reports/loan-portfolio` | loan_portfolio_report | login_required, role_required('admin', 'treasurer', 'secretary', 'exco') | blueprints/reports.py:367 |
| GET | `/savings` | savings_list | login_required, role_required('admin', 'treasurer', 'secretary', 'exco') | blueprints/savings.py:71 |
| GET | `/savings/batch/<path:batch_ref>` | salary_batch_detail | login_required, role_required('admin', 'treasurer', 'secretary', 'exco') | blueprints/savings.py:124 |
| GET | `/savings/batch/<path:batch_ref>/export` | salary_batch_export | login_required, role_required('admin', 'treasurer', 'secretary', 'exco') | blueprints/savings.py:164 |
| POST | `/savings/batch/<path:batch_ref>/reverse` | salary_batch_reverse | login_required, role_required('admin', 'treasurer') | blueprints/savings.py:196 |
| GET | `/savings/salary-template` | download_salary_template | login_required, role_required('admin', 'treasurer') | blueprints/savings.py:260 |
| GET, POST | `/savings/salary-upload` | salary_upload | login_required, role_required('admin', 'treasurer') | blueprints/savings.py:281 |
| POST | `/savings/add` | add_saving | login_required, role_required('admin', 'treasurer') | blueprints/savings.py:455 |
| GET | `/savings/<int:saving_id>/evidence` | payout_evidence | login_required, role_required('admin', 'treasurer', 'secretary', 'exco') | blueprints/savings.py:590 |
| POST | `/savings/adjust` | adjust_saving | login_required, role_required('admin', 'treasurer') | blueprints/savings.py:610 |
| POST | `/savings/payout` | record_payout | login_required, role_required('admin', 'treasurer') | blueprints/savings.py:713 |
| GET | `/security/` | index | login_required | blueprints/security.py:37 |
| GET, POST | `/security/2fa/setup` | setup_2fa | login_required | blueprints/security.py:50 |
| GET | `/security/2fa/backup-codes` | show_backup_codes | login_required | blueprints/security.py:84 |
| POST | `/security/2fa/backup-codes/regenerate` | regenerate_backup_codes_route | login_required | blueprints/security.py:94 |
| POST | `/security/2fa/disable` | disable_2fa | login_required | blueprints/security.py:109 |
| GET | `/training/` | index | login_required | blueprints/training.py:170 |
| GET | `/training/slides` | slides | login_required | blueprints/training.py:181 |
| GET | `/training/<slug>` | document | login_required, role_required('admin', 'secretary', 'treasurer', 'exco') | blueprints/training.py:191 |
| GET | `/admin/virtual-accounts/` | index | login_required, role_required('admin') | blueprints/virtual_accounts_bp.py:31 |
| POST | `/admin/virtual-accounts/settings` | save_settings | login_required, role_required('admin') | blueprints/virtual_accounts_bp.py:69 |
| POST | `/admin/virtual-accounts/provision/<int:member_id>` | provision | login_required, role_required('admin') | blueprints/virtual_accounts_bp.py:94 |
| POST | `/admin/virtual-accounts/provision-all` | provision_all | login_required, role_required('admin') | blueprints/virtual_accounts_bp.py:113 |
| POST | `/admin/virtual-accounts/receipt/<int:receipt_id>/match` | match_receipt | login_required, role_required('admin') | blueprints/virtual_accounts_bp.py:147 |
| POST | `/admin/virtual-accounts/receipt/<int:receipt_id>/apply` | apply_receipt | login_required, role_required('admin') | blueprints/virtual_accounts_bp.py:177 |
| GET | `/admin/virtual-accounts/receipt/<int:receipt_id>` | receipt_detail | login_required, role_required('admin') | blueprints/virtual_accounts_bp.py:250 |
| GET | `/static/<path:filename>` | static | Framework static serving; global hooks apply | app.py:20,119–123 |

Nginx also serves static files, cards and its own health response (nginx.conf:31–43,56–58).

## Base schema inventory
Column names and referenced tables are extracted from base CREATE statements. Later additive columns and indexes are separate, e.g. database.py:495–505,2293–2305. This is source evidence, not a live catalog.

| Table | Base columns | Referenced tables | Evidence |
|---|---|---|---|
| users | id, username, password_hash, role, full_name, email, phone, is_active, must_change_password, two_factor_secret, last_login, created_at |  | database.py:295 |
| user_sessions | sid, user_id, created_at, revoked_at |  | database.py:320 |
| user_backup_codes | id, user_id, code_hash, used_at, created_at |  | database.py:330 |
| login_attempts | id, ip, username, attempted_at |  | database.py:342 |
| throttle_hits | id, bucket, hit_at |  | database.py:354 |
| revoked_tokens | jti, user_id, expires_at, revoked_at |  | database.py:367 |
| feedback_responses | id, user_id, username, role, overall_experience, most_loved_feature, improve_feature, recommend_score, comments, referral_optin, referral_name, referral_email, created_at |  | database.py:380 |
| task_feedback_responses | id, event_key, user_id, role, task, rating, created_at |  | database.py:398 |
| members | id, member_number, first_name, last_name, email, phone, address, occupation, date_of_birth, nominee_name, nominee_relationship, nominee_phone, nominee_email, nominee_address, alt_nominee_name, alt_nominee_relationship, monthly_savings, total_savings, shares, shares_value, status, date_joined, photo_path, card_number, card_status, card_issued_date, card_expiry_date, emergency_contact_name, emergency_contact_phone, next_of_kin, bank_name, account_number, account_name, bvn, nin |  | database.py:411 |
| savings | id, member_id, amount, month, payment_type, late_fee, payment_method, reference, receipt_number, notes, created_by, date, verified_by, verified_at | members | database.py:477 |
| loans | id, loan_number, member_id, amount, purpose, description, tenure, interest_rate, interest_method, total_repayment, balance, status, application_fee, insurance_premium, disbursed_amount, disbursement_date, first_payment_date, next_payment_date, approved_by, approved_at, rejection_reason, completed_at, defaulted, notes, date_applied | members | database.py:514 |
| repayments | id, repayment_number, loan_id, amount, principal_paid, interest_paid, penalty_paid, payment_method, reference, receipt_number, transaction_id, notes, received_by, verified_by, verified_at, date | loans | database.py:590 |
| loan_adjustments | id, adjustment_number, loan_id, member_id, amount, direction, previous_balance, new_balance, reason, reference, source_file, journal_entry_id, created_by, created_at, reversed_at | loans, members | database.py:629 |
| member_receipts | id, receipt_number, member_id, amount, allocated_savings, allocated_loans, bank_account, payment_method, bank_reference, notes, date, journal_entry_id, created_by, created_at, reversed_at | members | database.py:658 |
| member_receipt_allocations | id, receipt_id, target, target_id, loan_id, amount, principal_paid, interest_paid, created_at | member_receipts | database.py:678 |
| loan_guarantors | id, loan_id, member_id, status, requested_at, responded_at, comment | loans, members | database.py:697 |
| loan_approvals | id, loan_id, stage, action, acted_by, acted_by_name, acted_at, comment | loans | database.py:711 |
| loan_request_events | id, loan_id, event_type, stage, channel, recipient_user_id, recipient_name, recipient_role, recipient_email, delivery, status, detail, created_at | loans | database.py:732 |
| asset_catalogue | id, category, name, brand, size_spec, description, image_url, estimated_price_min, estimated_price_max, is_active, updated_at |  | database.py:756 |
| asset_campaigns | id, name, description, status, opens_at, closes_at, tenure_months, annual_interest_rate, repayment_methods, banner_enabled, newsletter_enabled, newsletter_frequency_days, newsletter_subject, newsletter_intro, newsletter_last_sent_at, newsletter_next_send_at, created_by, created_at |  | database.py:811 |
| asset_products | id, campaign_id, name, brand, model, category, size_spec, description, image_url, vendor, purchase_cost, selling_price, estimated_price_min, estimated_price_max, delivery_fee, stock_limit, stock_reserved, warranty, status, created_at | asset_campaigns | database.py:840 |
| asset_orders | id, order_number, campaign_id, member_id, status, approval_stage, expected_spend, quote_status, quote_note, quoted_at, quote_accepted_at, selling_subtotal, annual_interest_rate, tenure_months, interest_amount, total_repayment, monthly_repayment, delivery_fee, delivery_fee_status, delivery_fee_reference, delivery_fee_account, delivery_fee_journal_id, repayment_method, eligibility_status, eligibility_reason, terms_accepted, signature_name, submitted_at, approved_at, procurement_cost, procurement_account, procurement_reference, procurement_journal_id, procured_at, dispatched_at, received_at, linked_loan_id, created_by | asset_campaigns, members, loans | database.py:869 |
| asset_order_items | id, order_id, product_id, product_name, brand, model, quantity, unit_purchase_cost, unit_selling_price, unit_delivery_fee, expected_unit_spend | asset_orders, asset_products | database.py:925 |
| asset_order_events | id, order_id, stage, action, actor_id, actor_name, comment, created_at | asset_orders | database.py:953 |
| asset_invoices | id, invoice_number, order_id, member_id, campaign_id, issue_date, selling_subtotal, finance_charge, total_repayment, delivery_fee, status, created_at | asset_orders, members, asset_campaigns | database.py:966 |
| asset_delivery_orders | id, delivery_number, order_id, invoice_id, status, issued_at, ready_at, confirmed_at, confirmed_by | asset_orders, asset_invoices, users | database.py:985 |
| investments | id, investment_number, name, amount, type, description, institution, interest_rate, return_rate, risk_level, start_date, maturity_date, duration_days, expected_return, actual_return, current_value, approval_status, approved_by, approved_at, documents, notes, created_by, date | users, users | database.py:1009 |
| honorarium | id, recipient_id, recipient_name, amount, description, month, date, paid_by | members, users | database.py:1040 |
| expenses | id, expense_number, category, amount, description, vendor, receipt_number, paid_to, payment_method, date, approved_by, recorded_by, notes | users, users | database.py:1061 |
| revenue | id, revenue_number, category, amount, description, source, date, received_by, notes | users | database.py:1082 |
| settings | id, key, value, description |  | database.py:1098 |
| notifications | id, user_id, title, message, notification_type, is_read, action_url, created_at, read_at | users | database.py:1108 |
| mobile_devices | id, user_id, member_id, platform, push_token, device_name, enabled, last_seen_at, created_at | users, members | database.py:1122 |
| coop_tenants | id, code, name, base_url, logo_url, is_active, created_at, updated_at |  | database.py:1147 |
| member_virtual_accounts | id, member_id, provider, customer_code, account_number, account_name, bank_name, bank_slug, provider_account_id, status, created_by, created_at, closed_at | members | database.py:1167 |
| virtual_account_receipts | id, member_id, virtual_account_id, provider, provider_reference, amount, account_number, sender_name, sender_bank, narration, status, allocated_amount, received_at, allocated_at, allocated_by, created_at | members | database.py:1195 |
| virtual_account_allocations | id, receipt_id, target, target_id, loan_id, amount, created_by, created_at, reversed_at | virtual_account_receipts | database.py:1224 |
| loan_fee_refunds | id, loan_id, member_id, amount, bank_account, journal_entry_id, status, note, created_by, created_at, reversed_at | loans, members | database.py:1246 |
| sms_log | id, member_id, msisdn, purpose, body, status, error, provider_ref, created_at | members | database.py:1268 |
| hq_clients | id, name, code, billing_email, phone, user_count, billed_user_count, rate_per_user, billing_cycle, period_start, period_end, status, notes, created_at, updated_at |  | database.py:1288 |
| hq_invoices | id, invoice_number, client_id, period_label, issue_date, due_date, amount, currency, status, payment_reference, paid_at, paid_method, pay_token, sent_at, notes, created_by, created_at | hq_clients | database.py:1311 |
| hq_invoice_items | id, invoice_id, item_type, description, quantity, unit_price, amount | hq_invoices | database.py:1335 |
| hq_setup_bands | id, min_members, max_members, amount, label, active, updated_at |  | database.py:1353 |
| affiliates | id, code, full_name, email, phone, tier, parent_id, recruited_by, status, bank_name, account_number, account_name, notes, applied_at, reviewed_at, reviewed_by, approved_at, accept_token, accepted_at, accepted_ip, declined_reason, promoted_at, updated_at | affiliates, affiliates | database.py:1374 |
| affiliate_commission_jobs | id, invoice_id, operation, payload, status, attempts, last_error, updated_at |  | database.py:1406 |
| affiliate_commissions | id, affiliate_id, client_id, invoice_id, source_affiliate_id, role, basis_amount, rate, amount, status, note, reversal_of, reversed_at, created_at, created_by | affiliates | database.py:1426 |
| affiliate_portal_tokens | id, affiliate_id, token, expires_at, created_at, created_ip, last_used_at | affiliates | database.py:1459 |
| ctas_cycles | id, name, status, start_date, end_date, duration_months, fixed_monthly_amount, monthly_capacity, earliest_payout_month, max_participants, admin_fee_flat, admin_fee_percentage, admin_fee_cap, admin_fee_threshold, ballot_date, created_by, created_at, updated_at |  | database.py:1482 |
| ctas_plans | id, name, description, contribution_amount, frequency, periods, target_amount, monthly_capacity, earliest_payout_month, admin_fee_flat, admin_fee_percentage, admin_fee_cap, admin_fee_threshold, affordability_method, affordability_ratio, savings_multiple, status, created_by, created_at |  | database.py:1522 |
| ctas_subscriptions | id, cycle_id, member_id, target_amount, tenure_months, monthly_deduction, admin_fee, requested_payout_month, status, payout_month, payout_date, total_recovered, outstanding, applied_at, approved_at, approved_by, enrolled_at, ballot_assigned_at, paid_out_at, completed_at, rejected_reason, created_by | ctas_cycles, members | database.py:1547 |
| ctas_ballot_runs | id, cycle_id, seed, summary, executed_by, run_at | ctas_cycles | database.py:1591 |
| ctas_payroll_batches | id, cycle_id, month_number, kind, file_name, processed, created_by, created_at | ctas_cycles | database.py:1603 |
| ctas_payroll_lines | id, batch_id, subscription_id, expected_amount, actual_amount, status, created_at | ctas_payroll_batches, ctas_subscriptions | database.py:1618 |
| ctas_schedule | id, subscription_id, cycle_id, period_number, due_date, expected_amount, paid_amount, status, paid_at, created_at | ctas_subscriptions, ctas_cycles | database.py:1639 |
| ctas_guarantors | id, subscription_id, member_id, amount, status, requested_at, responded_at, comment, signature_name, recovered_amount | ctas_subscriptions, members | database.py:1697 |
| ctas_priority_fees | id, plan_id, cycle_id, position, fee |  | database.py:1724 |
| ctas_mandates | id, member_id, subscription_id, provider, mandate_type, authorization_code, masked_label, bank_name, card_type, exp_month, exp_year, email, amount_cap, status, consent_text, consent_signature, consent_ip, consented_at, cancelled_at, last_charged_at, fail_count, last_error, created_at | members, ctas_subscriptions | database.py:1739 |
| ctas_exceptions | id, subscription_id, case_type, status, month_number, amount, description, resolution_note, created_at, resolved_at, resolved_by | ctas_subscriptions | database.py:1775 |
| communication_campaigns | id, title, audience, channel, subject, body, status, recipient_count, sent_count, failed_count, skipped_count, created_by, created_at, sent_at | users | database.py:1795 |
| communication_recipients | id, campaign_id, member_id, channel, destination, status, error, sent_at, created_at | communication_campaigns, members | database.py:1814 |
| marketing_leads | id, full_name, email, phone, society_name, society_type, member_count, current_system, priority, message, status, consent_accepted, utm_source, utm_medium, utm_campaign, utm_term, utm_content, referrer, landing_page, ip_address, user_agent, crm_sync_status, crm_external_id, lead_score, lead_temperature, score_reason, confirmation_sent_at, confirmation_status, confirmation_provider, confirmation_error, internal_alert_sent_at, internal_alert_status, internal_alert_provider, internal_alert_error, notes, assigned_to, next_follow_up_at, last_activity_at, demo_scheduled_at, demo_meeting_link, demo_presenter, demo_outcome, proposed_plan, proposal_status, setup_fee, monthly_subscription, expected_close_date, decision_reason, created_at, updated_at | users | database.py:1833 |
| marketing_lead_events | id, lead_id, event_type, description, actor_user_id, actor_username, data, created_at | marketing_leads, users | database.py:1920 |
| pending_payments | id, reference, member_id, payment_type, related_id, amount, month, gateway, status, gateway_ref, created_at, completed_at | members | database.py:1940 |
| loan_completion_emails | id, loan_id, repayment_id, recipient, audience, status, attempts, last_error, created_at, sent_at |  | database.py:1958 |
| role_permissions | id, role, permission, allowed, updated_by, updated_at |  | database.py:1978 |
| user_permissions | id, user_id, permission, allowed, granted_by, granted_at | users | database.py:1988 |
| audit_log | id, user_id, username, action, module, description, ip_address, user_agent, data, timestamp | users | database.py:2004 |
| journal_entries | id, entry_number, date, description, reference, source_module, source_id, created_by, created_at |  | database.py:2021 |
| form_submissions | token, created_at |  | database.py:2037 |
| vouchers | id, voucher_number, submission_token, kind, party, description, reference, date, journal_entry_id, created_by, created_at | journal_entries, users | database.py:2043 |
| voucher_allocations | id, voucher_id, target, target_id | vouchers | database.py:2060 |
| account_setup_tokens | id, user_id, token_hash, purpose, expires_at, used_at, created_at | users | database.py:2072 |
| events | id, title, event_type, event_date, location, meeting_link, description, is_active, created_by, created_at |  | database.py:2088 |
| meeting_minutes | id, title, meeting_type, meeting_date, file_name, file_mime, file_data, notes, uploaded_by, uploaded_at |  | database.py:2106 |
| event_rsvps | id, event_id, member_id, response, attended, responded_at | events, members | database.py:2126 |
| savings_change_requests | id, member_id, current_amount, requested_amount, reason, status, requested_at, reviewed_by, reviewed_by_name, reviewed_at, review_comment | members | database.py:2143 |
| accounts | id, code, name, type, normal_balance, parent_code, is_active, description |  | database.py:2163 |
| journal_lines | id, entry_id, account_code, debit, credit, memo | journal_entries | database.py:2176 |
| dividend_declarations | id, period_from, period_to, net_surplus, reserve_amount, honorarium_amount, other_amount, dividend_pool, patronage_split, status, journal_entry_id, declared_by, declared_at |  | database.py:2198 |
| dividend_allocations | id, declaration_id, member_id, savings_base, patronage_base, dividend_savings, dividend_patronage, total | dividend_declarations, members | database.py:2216 |
