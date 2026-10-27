# SMT loan policy and member statements

## Activation after deployment

In the SMT portal only, open Settings > Interest & Fees, select the SMT loan
catalogue and save. This is stored in that cooperative's own settings database.
Other cooperatives retain the Standard catalogue unless explicitly changed.

SMT applications offer Regular, Back to school and Asset loan. Regular uses
a single charge on the principal: 7.5% for 1-12 months and 9% for 13-24 months.
The preset overrides the Regular rate and tenure fields. Asset and school loans
use 20% annual flat interest, prorated by tenure: a six-month 500,000 loan has
50,000 interest and 550,000 total repayment. Their configured amount and tenure
limits still apply. The preset overrides their rate and method fields.

Existing internal purpose keys remain unchanged so historical loan accounts and
same-type borrowing checks continue to match. Existing contracts and balances
are not recalculated. Mobile uses the same backend policy; updated mobile builds
also display the friendly category labels and rate bands on the member card.

Annual flat interest is a separate method (Flat Rate in the existing settings):
500,000 * 20% * 6/12 = 50,000. Reducing annual interest uses the outstanding
principal, and therefore produces a different result. Configure the intended
method for each product before accepting applications.

## Statements and savings history

Open Members > member details > Financial Statement. Select the period, then
download CSV or use Print / Save PDF. Savings, share capital, loan obligations,
repayments, dated reversals and balance corrections are included. Loan account
balances are reconciled against current recorded balances; discrepancies are
shown, not silently corrected. Migrated accounts require complete dated history
for a complete historical position.

Members > member details > Savings History shows contribution entries and their
recording users, member change requests with review decisions, and new direct or
approved monthly-deduction changes with before/after values and the acting user.
Historical entries without actor data are explicitly labelled as unrecorded or
migrated. These views use the existing members.view permission.

## Verification

Run separately with the test Python environment:

```
python -m unittest tests.test_loan_limits tests.test_member_financials
python -m unittest tests.test_member_policy_routes
python -m unittest tests.test_hardening_features
```

The route suite creates an isolated temporary database under outputs; it does not
connect to production. From mobile/coopms-mobile, run `npx tsc --noEmit`.
