# Task feedback and advertising enquiries

## Web feedback

The optional inline prompt asks "How was this task?" with Easy, Okay, Difficult
and Skip. It does not block work or require a written review. It appears below
the page content, not as a modal. The same task is prompted at most once per
user per ten minutes in the current browser session.

Coverage is explicitly listed in `task_feedback.py`: member balances, savings,
loans and statements; successful loan applications, withdrawal, guarantor review,
savings requests and profile updates; staff loan reviews, receipts, savings,
uploads, member updates, journals and lock dates. Only successful POST redirects
with a new success message qualify for write-task feedback. Mixed/error uploads
do not qualify. Read prompts mean the page was viewed, not proof that a task was
finished. This is not tracking every click.

Administrators see aggregates by task and role under Administration > User
Feedback. Task feedback is separate from the existing quarterly survey/NPS.
Records contain internal user ID, role, task, rating and timestamp, not loan
amounts, balances or form contents. This is identified feedback, not anonymous.
Each tenant retains its own records. There is no cross-tenant HQ collection.

The submission route requires authentication, CSRF and a user-bound signed
one-hour token. A unique event key prevents duplicate submissions. Failure to
save a rating does not undo the user's completed financial transaction.

Native mobile screens are not included in this web rollout. Add their task
events and API integration separately before claiming mobile coverage.

## Advertising and onboarding

Approved planning limit: NGN 50,000 per month. No campaign has been launched
and no ad spend has been authorized through this implementation.

Public contact: Adeolu Keshinro, adeolu.keshinro@tresanofinance.com,
WhatsApp +2348071090281. Direct contact links are alongside the demo form.
WhatsApp/email conversations do not automatically create HQ CRM leads; staff
must record those enquiries. Demo form submissions already create HQ leads.

Before launching ads:

1. Set MARKETING_LEAD_NOTIFY_EMAIL=adeolu.keshinro@tresanofinance.com in HQ's
   private deployment environment (do not commit its environment file).
2. Confirm HQ Resend delivery to this address with an approved test lead.
3. Confirm the prospect acknowledgement and internal notification both arrive.
4. Verify a conversion fires only after successful demo form submission.
5. Confirm Google Ads account, billing and budget before publishing a campaign.
6. Track qualified leads, demos attended, paid onboarding and spend weekly.

Deployment creates the task feedback table through the normal schema startup.
Deploy the application and public landing files together. No production settings
or mail delivery are changed merely by editing these files.
