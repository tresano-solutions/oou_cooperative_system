# Intelligent Loan Adapter

Open Loans > Bulk Repayments > Intelligent Loan Adapter (admin/treasurer with repayment permission).

1. Choose a month and Export Loan Book. This lists active outstanding loans with member/staff numbers, names, recorded loan amounts, balances, expected instalments and the schedule basis. Migrated opening balances are not necessarily original disbursement amounts. Where schedules are missing or migrated, the latest historical monthly payment total is suggested, capped at the outstanding balance. The most recent three recorded months from the preceding 12 months are shown as evidence. One-month and irregular history are explicitly flagged for review. No usable history means an unconfirmed amount.
2. Upload a single-sheet XLSX or UTF-8 CSV. Use `amount` plus `member_number`, `staff_number` or `member_name`. The importer also accepts employee_id, employee_name and common header variants. Remove summary totals and give every identifier column a heading. IDs with leading zeros should be Excel text.
3. Choose the payroll month, actual payment date and debit account once. Select 1400 only when it is the cooperative's configured payroll control account.
4. Review identity and loan allocations. Unique names can match regardless of name order, but spelling differences, conflicting IDs/names and ambiguous names require correction in the source. IDs never silently fall back to names. Multiple loans are prefilled only when all known expected instalments equal the deduction. Otherwise enter the split explicitly. Future instalments are shown as zero; explicit early repayment remains possible.
5. Confirm each included row or explicitly exclude it. All amounts must reconcile to that member's deduction. Download Approved Repayments, then upload that CSV through Bulk Repayments without resaving it in Excel.

Preparation never posts money. Preview tokens expire after one hour, are signed and tied to the preparing user. Current balances/status are checked again at export. Existing repayments in the payroll month block that member in the adapter, including legacy receipts; investigate those via the existing repayment workflow. Stable per-month/member/loan receipts protect retries of adapter-generated files. The posting importer still performs its own validation and receipt checks.

Export audit history records the preparing user, approved allocation amounts, account, date, receipts and excluded source rows. Corrections to prior postings are separate controlled actions. A reviewed file is not proof of a successful posting: check Upload History after import.

Limits: 5 MB files, 2,000 input rows, one worksheet; no fuzzy name auto-allocation. The adapter does not correct historic rates or migrated schedules and does not include arrears in the scheduled monthly instalment automatically.

Historical suggestions exclude reversed, non-positive and explicitly labelled pre-liquidation, settlement or catch-up payments. Current and future payroll months are excluded. Unlabelled catch-up or combined payments can still distort history, so historical totals are suggestions, not contractual instalments; the treasurer must confirm them. This is transparent rule-based matching, not an external AI service: member data is not sent to a third party.
