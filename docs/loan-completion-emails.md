# Loan completion emails

When a recorded repayment reduces a loan balance to zero and marks it completed,
CoopMS prepares separate branded emails for the member and active admin, exco,
secretary and treasurer accounts that have email addresses. Duplicate addresses
receive one message; a member who is also an officer receives the member version.

Supported posting paths: single repayments, bulk repayments, combined member
receipts and existing online payment confirmations. Historical migration imports
and balance corrections do not trigger these emails. Existing completed loans are
not automatically backfilled.

The message includes the loan number/type, original principal, final payment,
settlement date, repayment reference and zero outstanding balance. It explicitly
does not imply that the member has cleared other loans. Officers are reminded to
review payroll deduction changes through their normal approval process; no
salary deduction setting is changed automatically.

## Delivery and recovery

- Uses each tenant's existing branded email service and provider configuration.
- Outbox records commit with the repayment; sending starts after payment commit.
- `loan_completion_emails` records recipients, attempts, status and errors.
- `sent` means the provider accepted the message, not confirmed inbox delivery.
- Failed attempts retry during subsequent repayment dispatches or the existing
  protected loan pipeline sweep, up to three attempts. No new scheduler is created.
- Reversed/reopened loans and recipients who are no longer eligible are checked
  before dispatch, and pending messages are cancelled.
- A `sending` record after an exception/crash has an unknown delivery outcome.
  Check provider logs before manually recovering it; do not blindly requeue it.
- Missing email addresses are skipped. Provider failures cannot undo a committed
  repayment. Already sent emails cannot be recalled following a later reversal.

The schema is created by normal application database initialization. There is no
new credential required. Validate delivery with a controlled test member after
deployment; automated tests use a mocked provider and do not prove live delivery.
