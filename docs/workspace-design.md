# Workspace design implementation

The design skill's token architecture guides this implementation: existing navy,
gold and neutral primitives feed shared semantic tokens and component styles.
Bootstrap remains the layout system. Tenant branding and server permissions remain
authoritative.

## Shared staff and member workspace

- Collapsible task groups preserve the server-rendered, permission-filtered links.
- Exact-page navigation highlighting, keyboard-operable groups and Escape dismissal.
- Skip-to-content landmark, unrestricted browser zoom and visible focus indicators.
- Consistent cards, fields, headers, monetary digits and mobile touch targets.
- Persistent error messages, field validation descriptions and reduced-motion styles.
- Read-only tables with at least five loaded rows gain labelled local search and
  column sorting. These controls explicitly describe their loaded-page scope;
  existing server pagination and exports remain in place.
- Narrow tables scroll inside keyboard-focusable regions instead of forcing the
  entire page horizontally. Print styles hide navigation and local table controls.

## Dashboard and vouchers

- Permission-aware finance shortcuts.
- Real six-month savings/disbursement series replaces hard-coded sample values.
- Outstanding-loan metric uses remaining balance rather than original principal.
- Voucher entry retains invalid submitted values and uses explicit associated labels.
- Receipt/payment bank selection is mandatory and starts with a blank option.
- General lines support member savings/shares and linked loan records.
- Loan disbursement hides/disables unrelated lines and previews gross, fee,
  insurance and net payment using the existing 1%/1% policy.
- Journal balancing is checked in cents; server posting validation remains authoritative.
- Review dialog displays selected details and lines before the final post action.
- Voucher detail exposes totals, creator, creation time, posted/reversed state,
  printing and the existing journal/reversal history.
- Corrected the voucher audit call to use the application's real audit schema.
- Server-error page no longer references a missing illustration or claims that
  someone has been notified without evidence.

## Verification

Run `pytest tests/test_workspace_design.py` in the test virtual environment.
It verifies rendered routes, receipt posting, duplicate-submit protection and
preserved values following invalid input.

`scripts/preview_workspace.py` is an isolated localhost preview using the existing
test fixture. It recreates only the fixture's test database. Do not run it at the
same time as tests sharing that database.

`scripts/check_workspace_ui.cjs` exercises navigation, journal balancing,
confirmation, loan controls, mobile reflow and Escape handling, and saves screenshots
to `outputs/design-review`. It accepts PLAYWRIGHT_MODULE and BROWSER_CHANNEL.

## Boundaries

These shared improvements apply to pages extending the base template. Standalone
print, card and email layouts retain their specialized designs. This is not a WCAG
conformance certification: complete screen-reader, contrast and all-role audits
are still required. New bank-feed matching, a draft-voucher approval lifecycle,
and replacement of all legacy page-specific components are separate functional
changes, not claimed as part of this implementation.
