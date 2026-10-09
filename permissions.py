"""
permissions.py — what each officer is allowed to do.

Before this module the answer was hard-coded: every view carried
`@role_required('admin', 'treasurer')` and the only way to change who could do
what was to edit and redeploy the code. A cooperative that wanted its Treasurer
to look up member details, or its Exco to stay out of the savings book, had no
way to say so.

Now access is expressed as **permissions**, and a permission can be granted at
two levels:

  1. **Role defaults** — what a Treasurer, General Secretary or Exco member can
     do out of the box. Seeded from `PERMISSIONS[*]['default_roles']` below,
     which reproduce exactly the access the code used to hard-code, and
     editable afterwards in Settings → Task Assignment.
  2. **Per-officer overrides** — one named officer allowed or denied a single
     permission regardless of their role. Anything left on "inherit" follows
     the role.

Resolution order (first match wins):

    super admin ....................... everything
    role == 'admin' (President) ....... everything      (cannot be locked out)
    per-user override ................. allow / deny
    role default ...................... allow / deny
    otherwise ......................... denied

Views stay decorated with `role_required(...)`. That decorator now looks the
current endpoint up in `ENDPOINT_PERMISSIONS` (built from this catalogue) and
checks the permission when one is mapped, falling back to its literal role list
for anything unmapped — so a view that is added without being catalogued keeps
working under the old rules instead of failing open.
"""

import logging

log = logging.getLogger(__name__)

# ── Offices ───────────────────────────────────────────────────────────────────
#
# The bye-laws name the offices; the system stores them as user roles.

ROLE_LABELS = {
    'admin':     'President / Administrator',
    'treasurer': 'Treasurer',
    'secretary': 'General Secretary',
    'exco':      'Exco Member',
}

# The President holds every permission and is never editable — somebody must
# always be able to restore access after a mistake.
FULL_ACCESS_ROLE = 'admin'

# Offices whose permissions an admin may edit.
ASSIGNABLE_ROLES = ('treasurer', 'secretary', 'exco')


# ── Catalogue ─────────────────────────────────────────────────────────────────
#
# Each entry: key, label, group, description, default_roles, endpoints.
# `endpoints` are Flask endpoint names ('blueprint.view_function'); they are
# what the decorator matches on, and what the admin UI lists as "covers".
#
# The default_roles of every permission mirror the role lists the views used to
# carry, with one deliberate change, marked below: the Treasurer and Exco can
# now open the members list, not only a member's detail page.

PERMISSIONS = [
    {
        'key': 'accounting.vouchers', 'label': 'View vouchers', 'group': 'Accounting',
        'description': 'View posted receipts, payments and journals.',
        'default_roles': ('admin', 'treasurer'),
        'endpoints': ('accounting.vouchers', 'accounting.voucher_detail'),
    },
    {
        'key': 'accounting.voucher_post', 'label': 'Post vouchers', 'group': 'Accounting',
        'description': 'Create receipts, payments and journals in the general ledger.',
        'default_roles': ('admin', 'treasurer'),
        'endpoints': ('accounting.new_voucher',),
    },
    # ── Members ──────────────────────────────────────────────────────────────
    {
        'key': 'members.view',
        'label': 'View members and their profiles',
        'group': 'Members',
        'description': 'Open the members list, a member profile, savings statement and ID card.',
        'default_roles': ('admin', 'secretary', 'treasurer', 'exco'),
        'endpoints': (
            'members.members_list',            # was admin/secretary only — see docs
            'members.member_details',
            'members.member_savings_statement',
            'members.member_card',
            'admin_panel.get_member_api',
        ),
    },
    {
        'key': 'members.manage',
        'label': 'Add, edit and remove members',
        'group': 'Members',
        'description': 'Register new members, edit records, mark former members and export the register.',
        'default_roles': ('admin', 'secretary'),
        'endpoints': (
            'members.add_member', 'members.edit_member', 'members.delete_member',
            'members.mark_former', 'members.reinstate_member',
            'members.bulk_upload_members', 'members.download_template',
            'members.export_members',
        ),
    },
    {
        'key': 'members.savings_requests',
        'label': 'Review savings-change requests',
        'group': 'Members',
        'description': 'Approve or decline a member\'s request to change their monthly savings.',
        'default_roles': ('admin', 'secretary', 'treasurer'),
        'endpoints': ('members.savings_requests', 'members.savings_request_act'),
    },
    {
        'key': 'members.cards',
        'label': 'Generate member ID cards',
        'group': 'Members',
        'description': 'Produce the printable membership card for a member.',
        'default_roles': ('admin', 'secretary'),
        'endpoints': ('cards.generate_member_card',),
    },

    # ── Savings ──────────────────────────────────────────────────────────────
    {
        'key': 'savings.view',
        'label': 'View the savings book',
        'group': 'Savings',
        'description': 'See savings contributions and salary-deduction batches.',
        'default_roles': ('admin', 'treasurer', 'secretary', 'exco'),
        'endpoints': ('savings.savings_list', 'savings.salary_batch_detail',
                      'savings.salary_batch_export'),
    },
    {
        'key': 'savings.manage',
        'label': 'Record savings and payouts',
        'group': 'Savings',
        'description': 'Post contributions, upload salary batches and record payouts.',
        'default_roles': ('admin', 'treasurer'),
        'endpoints': ('savings.add_saving', 'savings.record_payout',
                      'savings.salary_upload', 'savings.download_salary_template',
                      'member_receipts.member_payment', 'member_receipts.reverse_member_payment'),
    },
    {
        'key': 'payments.virtual_accounts',
        'label': 'Member account numbers and received transfers',
        'group': 'Savings',
        'description': 'Issue each member a bank account number, and decide what '
                       'incoming transfers are for.',
        'default_roles': ('admin', 'treasurer'),
        'endpoints': ('virtual_accounts.index', 'virtual_accounts.save_settings',
                      'virtual_accounts.provision', 'virtual_accounts.provision_all',
                      'virtual_accounts.match_receipt', 'virtual_accounts.apply_receipt',
                      'virtual_accounts.receipt_detail'),
    },

    # ── Loans ────────────────────────────────────────────────────────────────
    {
        'key': 'loans.view',
        'label': 'View loan requests and the loan book',
        'group': 'Loans',
        'description': 'Open the loans list, any loan and its application PDF.',
        'default_roles': ('admin', 'treasurer', 'secretary', 'exco'),
        'endpoints': ('loans.loans_list', 'loans.loan_detail', 'loans.loan_application_pdf'),
    },
    {
        'key': 'loans.apply',
        'label': 'Raise a loan application for a member',
        'group': 'Loans',
        'description': 'Capture a loan application at the office on a member\'s behalf.',
        'default_roles': ('admin', 'treasurer', 'secretary', 'exco'),
        'endpoints': ('loans.apply_loan',),
    },
    {
        'key': 'loans.approve',
        'label': 'Act on loan approvals and due diligence',
        'group': 'Loans',
        'description': ('Approve or reject at your stage, run due diligence and re-send exco '
                        'alerts. Which stage an officer may sign remains fixed by the bye-laws.'),
        'default_roles': ('admin', 'treasurer', 'secretary'),
        'endpoints': ('loans.loan_act', 'loans.update_due_diligence', 'loans.resend_loan_alert'),
    },
    {
        'key': 'loans.cancel',
        'label': 'Cancel a pending loan request',
        'group': 'Loans',
        'description': "Take a request out of the queue on the applicant's behalf - entered "
                       "twice, wrong details, or the member asked. Different from rejecting "
                       "it, which is a decision at a stage. The applicant can always "
                       "withdraw their own.",
        'default_roles': ('admin', 'treasurer', 'secretary', 'exco'),
        'endpoints': ('loans.cancel_loan_application',),
    },
    {
        'key': 'loans.corrections',
        'label': 'Correct a loan balance',
        'group': 'Loans',
        'description': 'Adjust a loan balance to fix a posting error. Separate from '
                       'recording repayments, so a cooperative can grant one without the other.',
        'default_roles': ('admin', 'treasurer'),
        'endpoints': ('loans.loan_corrections',),
    },
    {
        'key': 'loans.fee_refunds',
        'label': 'Refund wrongly charged loan fees',
        'group': 'Loans',
        'description': 'Review and pay back application fees that were withheld at '
                       'disbursement without being configured. Its own duty because the '
                       'money leaves the bank and the list carries member account numbers.',
        'default_roles': ('admin', 'treasurer'),
        'endpoints': ('loans.fee_refunds', 'loans.fee_refunds_export'),
    },
    {
        'key': 'loans.repayments',
        'label': 'Record loan repayments',
        'group': 'Loans',
        'description': 'Post repayments, run bulk repayment uploads and export the loan book.',
        'default_roles': ('admin', 'treasurer'),
        'endpoints': ('loans.repay_loan', 'loans.bulk_loan_repayments',
                      'loans.export_loans', 'loans.export_loan_statements',
                      'loans.download_repayment_template'),
    },

    # ── Investments ──────────────────────────────────────────────────────────
    {
        'key': 'investments.manage',
        'label': 'View and record investments',
        'group': 'Investments',
        'description': 'See the investment portfolio and add new placements.',
        'default_roles': ('admin', 'treasurer'),
        'endpoints': ('investments.investments_list', 'investments.add_investment'),
    },

    # ── Reports ──────────────────────────────────────────────────────────────
    {
        'key': 'reports.view',
        'label': 'View reports',
        'group': 'Reports',
        'description': 'Financial statements, savings control and loan portfolio reports.',
        'default_roles': ('admin', 'treasurer', 'secretary', 'exco'),
        'endpoints': ('reports.reports_list', 'reports.financial_report',
                      'reports.member_savings_control', 'reports.loan_portfolio_report'),
    },
    {
        'key': 'reports.cashbook',
        'label': 'View the cashbook',
        'group': 'Reports',
        'description': 'The cash receipts and payments book.',
        'default_roles': ('admin', 'treasurer'),
        'endpoints': ('reports.cashbook_report',),
    },

    # ── Accounting ───────────────────────────────────────────────────────────
    {
        'key': 'accounting.view',
        'label': 'View the ledgers',
        'group': 'Accounting',
        'description': 'Chart of accounts, trial balance, bank accounts, journals and dividends.',
        'default_roles': ('admin', 'treasurer'),
        'endpoints': ('accounting.chart_of_accounts', 'accounting.trial_balance_view',
                      'accounting.reconciliation', 'accounting.bank_accounts',
                      'accounting.bank_account_detail', 'accounting.dividends',
                      'accounting.dividend_detail', 'accounting.account_ledger_view',
                      'accounting.account_ledger_export', 'accounting.journal_entry_view',
                      'accounting.journal_entry_quick_view', 'accounting.journal_register',
                      'accounting.journal_register_export'),
    },
    {
        'key': 'accounting.post',
        'label': 'Post and reverse journal entries',
        'group': 'Accounting',
        'description': 'Raise journal entries, reverse them and close an accounting period.',
        'default_roles': ('admin', 'treasurer'),
        'endpoints': ('accounting.new_journal', 'accounting.reverse_entry',
                      'accounting.period_close', 'savings.salary_batch_reverse',
                      'savings.adjust_saving'),
    },
    {
        'key': 'accounting.admin',
        'label': 'Change the chart of accounts and declare dividends',
        'group': 'Accounting',
        'description': 'Add or disable accounts, set the default cash account, lock the books '
                       'and declare dividends.',
        'default_roles': ('admin',),
        'endpoints': ('accounting.add_account', 'accounting.set_default_cash_account',
                      'accounting.toggle_account', 'accounting.toggle_cash_account',
                      'accounting.reclassify_savings_bank_account',
                      'accounting.declare_dividend', 'accounting.backfill',
                      'accounting.set_lock_date'),
    },

    {
        'key': 'ctas.eligibility',
        'label': 'Confirm CTAS eligibility',
        'group': 'CTAS',
        'description': 'First approval gate: check the member qualifies for the target advance. '
                       'Give this to a different officer from finance and committee approval if '
                       'you want the stages separated.',
        'default_roles': ('admin', 'secretary'),
        'endpoints': (),
    },
    {
        'key': 'ctas.finance',
        'label': 'CTAS finance review',
        'group': 'CTAS',
        'description': 'Second approval gate: confirm the member can afford the contributions.',
        'default_roles': ('admin', 'treasurer'),
        'endpoints': (),
    },
    {
        'key': 'ctas.approve',
        'label': 'CTAS committee approval',
        'group': 'CTAS',
        'description': 'Final approval gate before a member is enrolled for the ballot.',
        'default_roles': ('admin',),
        'endpoints': (),
    },
    {
        'key': 'ctas.manage',
        'label': 'Manage the Target Advance Scheme (CTAS)',
        'group': 'CTAS',
        'description': 'Run CTAS cycles: enrol members, run the ballot and pay out advances. '
                       'Only visible when the CTAS add-on is enabled.',
        'default_roles': ('admin', 'treasurer'),
        'endpoints': ('ctas.dashboard', 'ctas.overview', 'ctas.plans', 'ctas.new_plan',
                      'ctas.new_cycle', 'ctas.cycle_detail',
                      'ctas.cycle_transition', 'ctas.delete_cycle', 'ctas.promote_cycle',
                      'ctas.add_subscription',
                      'ctas.subscription_act',
                      'ctas.run_ballot', 'ctas.payout',
                      'ctas.set_priority_fees', 'ctas.decide_priority', 'ctas.set_liquidity',
                      'ctas.set_security',
                      'ctas.record_terms', 'ctas.bulk_advance',
                      'ctas.payroll_export', 'ctas.payroll_import',
                      'ctas.exit_settle', 'ctas.exceptions', 'ctas.resolve_exception'),
    },
    {
        'key': 'ctas.guarantee_call',
        'label': 'Authorise calling on a CTAS guarantor',
        'group': 'CTAS',
        'description': 'Approve recovering an unpaid target advance from a guarantor\'s savings. '
                       'This takes money from a member who is not the one leaving, so it is held '
                       'apart from running the scheme and needs a committee decision on record.',
        'default_roles': ('admin',),
        # Checked inside the exit settlement rather than by a decorator, because
        # it only applies when the officer actually calls on a guarantor.
        'endpoints': (),
    },

    # ── Money in and out ─────────────────────────────────────────────────────
    {
        'key': 'finance.expenses',
        'label': 'Record expenses and other revenue',
        'group': 'Money in & out',
        'description': 'Capture cooperative expenses and non-loan revenue.',
        'default_roles': ('admin', 'treasurer'),
        'endpoints': ('admin_panel.expenses', 'admin_panel.add_expense',
                      'admin_panel.revenue', 'admin_panel.add_revenue'),
    },
    {
        'key': 'finance.honorarium',
        'label': 'Manage honorarium payments',
        'group': 'Money in & out',
        'description': 'Record honorarium paid to officers.',
        'default_roles': ('admin',),
        'endpoints': ('admin_panel.honorarium', 'admin_panel.add_honorarium'),
    },
    {
        'key': 'finance.subscription',
        'label': 'Manage the platform subscription',
        'group': 'Money in & out',
        'description': 'View and pay the cooperative\'s CoopMS subscription.',
        'default_roles': ('admin', 'treasurer'),
        'endpoints': ('admin_panel.subscription_page', 'admin_panel.subscription_callback'),
    },

    # ── Governance and outreach ──────────────────────────────────────────────
    {
        'key': 'governance.manage',
        'label': 'Manage events and minutes',
        'group': 'Governance',
        'description': 'Create meetings and events, take attendance and upload minutes.',
        'default_roles': ('admin', 'secretary'),
        'endpoints': ('governance.manage', 'governance.add_event', 'governance.toggle_event',
                      'governance.delete_event', 'governance.edit_event',
                      'governance.mark_attendance', 'governance.upload_minutes',
                      'governance.delete_minutes'),
    },
    {
        'key': 'communications.manage',
        'label': 'Send communications to members',
        'group': 'Governance',
        'description': 'Compose and send broadcast campaigns.',
        'default_roles': ('admin', 'secretary'),
        'endpoints': ('communications.index', 'communications.new_campaign',
                      'communications.campaign_detail', 'communications.retry_campaign'),
    },
    {
        'key': 'marketing.manage',
        'label': 'Work the leads inbox',
        'group': 'Governance',
        'description': 'Follow up prospective cooperatives captured by the marketing site.',
        'default_roles': ('admin', 'secretary'),
        'endpoints': ('marketing.leads_inbox', 'marketing.lead_detail',
                      'marketing.update_lead_status', 'marketing.update_lead_pipeline',
                      'marketing.update_lead_workflow', 'marketing.add_lead_activity',
                      'marketing.export_leads'),
    },
    {
        'key': 'feedback.manage',
        'label': 'Read member feedback',
        'group': 'Governance',
        'description': 'See in-app feedback and referral exports.',
        'default_roles': ('admin',),
        'endpoints': ('feedback.admin', 'feedback.export_referrals'),
    },

    # ── Training ─────────────────────────────────────────────────────────────
    {
        'key': 'training.facilitate',
        'label': 'Open the facilitator material',
        'group': 'Training',
        'description': "The lesson plan and facilitator notes for the officer course. "
                       "The course outline and the slides stay open to every member.",
        'default_roles': ('admin', 'secretary', 'treasurer', 'exco'),
        'endpoints': ('training.document',),
    },

    # ── Data migration ───────────────────────────────────────────────────────
    {
        'key': 'migration.members',
        'label': 'Import and export member data',
        'group': 'Data migration',
        'description': 'Bulk member import/export and templates.',
        'default_roles': ('admin', 'secretary'),
        'endpoints': ('migration.import_members', 'migration.template_members',
                      'migration.export_members'),
    },
    {
        'key': 'migration.finance',
        'label': 'Import and export financial data',
        'group': 'Data migration',
        'description': 'Bulk savings, loans, repayments, expenses, revenue and investment files.',
        'default_roles': ('admin', 'treasurer'),
        'endpoints': ('migration.import_savings', 'migration.template_savings',
                      'migration.export_savings', 'migration.import_loans',
                      'migration.template_loans', 'migration.export_loans',
                      'migration.import_repayments', 'migration.template_repayments',
                      'migration.export_repayments', 'migration.import_expenses',
                      'migration.template_expenses', 'migration.export_expenses',
                      'migration.import_revenue', 'migration.template_revenue',
                      'migration.export_revenue', 'migration.import_investments',
                      'migration.template_investments', 'migration.export_investments'),
    },
    {
        'key': 'migration.admin',
        'label': 'Opening balances and database tools',
        'group': 'Data migration',
        'description': 'Opening balances, honorarium migration, demo data and database purge.',
        'default_roles': ('admin',),
        'endpoints': ('migration.index', 'migration.template_opening', 'migration.import_opening',
                      'migration.import_honorarium', 'migration.template_honorarium',
                      'migration.export_honorarium', 'migration.load_demo',
                      'migration.purge_database'),
    },

    # ── System ───────────────────────────────────────────────────────────────
    {
        'key': 'system.settings',
        'label': 'Change system settings',
        'group': 'System',
        'description': 'Cooperative details, loan rules, email set-up and system health.',
        'default_roles': ('admin',),
        'endpoints': ('admin_panel.settings', 'admin_panel.update_settings',
                      'admin_panel.update_security_settings', 'admin_panel.update_mail_settings',
                      'admin_panel.test_mail', 'cards.test_email',
                      'admin_panel.update_sms_settings',
                      'admin_panel.test_sms', 'admin_panel.reconcile_savings',
                      'admin_panel.readiness_status', 'admin_panel.test_db'),
    },
    {
        'key': 'system.upload_history',
        'label': 'View upload history',
        'group': 'System',
        'description': 'See past data uploads and why rows failed. A General Secretary '
                       'sees only their own uploads; others see every one.',
        'default_roles': ('admin', 'treasurer', 'secretary'),
        'endpoints': ('admin_panel.upload_history',),
    },
    {
        'key': 'system.users',
        'label': 'Manage staff accounts',
        'group': 'System',
        'description': 'Create officers, reset passwords and 2FA, enable or disable accounts.',
        'default_roles': ('admin',),
        'endpoints': ('admin_panel.add_user', 'admin_panel.edit_user',
                      'admin_panel.reset_user_password', 'admin_panel.reset_user_2fa',
                      'admin_panel.resend_setup_link', 'admin_panel.bulk_send_setup_links',
                      'admin_panel.revoke_setup_links', 'admin_panel.toggle_super_admin',
                      'admin_panel.toggle_user', 'admin_panel.test_mobile_device_push',
                      'admin_panel.revoke_mobile_device'),
    },
    {
        'key': 'system.permissions',
        'label': 'Assign what each officer can do',
        'group': 'System',
        'description': 'This screen. Reserved to the President so access can always be restored.',
        'default_roles': ('admin',),
        'endpoints': ('admin_panel.task_assignment', 'admin_panel.update_role_permissions',
                      'admin_panel.user_permissions', 'admin_panel.update_user_permissions',
                      'admin_panel.reset_permissions', 'admin_panel.reset_user_permissions'),
    },
]

PERMISSION_BY_KEY = {p['key']: p for p in PERMISSIONS}
PERMISSION_KEYS = tuple(p['key'] for p in PERMISSIONS)

# Permissions no officer other than the President may hold: handing these out
# would let an officer grant themselves anything else.
RESERVED_PERMISSIONS = frozenset({'system.permissions'})

ENDPOINT_PERMISSIONS = {}
for _perm in PERMISSIONS:
    for _endpoint in _perm['endpoints']:
        if _endpoint in ENDPOINT_PERMISSIONS:      # pragma: no cover - catalogue typo guard
            raise RuntimeError(f'endpoint {_endpoint} is mapped to two permissions')
        ENDPOINT_PERMISSIONS[_endpoint] = _perm['key']


def permission_groups():
    """The catalogue grouped for display, in catalogue order."""
    groups, order = {}, []
    for perm in PERMISSIONS:
        if perm['group'] not in groups:
            groups[perm['group']] = []
            order.append(perm['group'])
        groups[perm['group']].append(perm)
    return [(name, groups[name]) for name in order]


def endpoint_permission(endpoint):
    """The permission guarding a Flask endpoint, or None if it is not catalogued."""
    return ENDPOINT_PERMISSIONS.get(endpoint or '')


def default_allowed(permission, role) -> bool:
    perm = PERMISSION_BY_KEY.get(permission)
    return bool(perm and role in perm['default_roles'])


def assignable(permission, role) -> bool:
    """False for combinations the UI must not offer (reserved permissions)."""
    return not (permission in RESERVED_PERMISSIONS and role != FULL_ACCESS_ROLE)


# ── Stored overlays ───────────────────────────────────────────────────────────

def role_permission_overrides(db, role=None):
    """{(role, permission): bool} of role defaults an admin has changed."""
    try:
        if role:
            rows = db.execute(
                'SELECT role, permission, allowed FROM role_permissions WHERE role = ?', (role,)
            ).fetchall()
        else:
            rows = db.execute('SELECT role, permission, allowed FROM role_permissions').fetchall()
    except Exception:
        log.exception('Could not read role permissions')
        return {}
    return {(r['role'], r['permission']): bool(r['allowed']) for r in rows}


def user_permission_overrides(db, user_id):
    """{permission: bool} of per-officer allow/deny overrides."""
    if not user_id:
        return {}
    try:
        rows = db.execute(
            'SELECT permission, allowed FROM user_permissions WHERE user_id = ?', (user_id,)
        ).fetchall()
    except Exception:
        log.exception('Could not read user permissions for %s', user_id)
        return {}
    return {r['permission']: bool(r['allowed']) for r in rows}


def role_allows(db, role, permission) -> bool:
    """What the role grants, taking any stored change to the default into account."""
    if role == FULL_ACCESS_ROLE:
        return True
    if not assignable(permission, role):
        return False
    overrides = role_permission_overrides(db, role)
    if (role, permission) in overrides:
        return overrides[(role, permission)]
    return default_allowed(permission, role)


def effective_permissions(db, user_id, role, is_super_admin=False):
    """Every permission the user actually holds, as a set of keys."""
    if is_super_admin or role == FULL_ACCESS_ROLE:
        return set(PERMISSION_KEYS)
    if role not in ASSIGNABLE_ROLES:
        return set()
    role_overrides = role_permission_overrides(db, role)
    user_overrides = user_permission_overrides(db, user_id)
    allowed = set()
    for key in PERMISSION_KEYS:
        if not assignable(key, role):
            continue
        if key in user_overrides:
            granted = user_overrides[key]
        elif (role, key) in role_overrides:
            granted = role_overrides[(role, key)]
        else:
            granted = default_allowed(key, role)
        if granted:
            allowed.add(key)
    return allowed


def permission_matrix(db, user_id, role):
    """Per-permission detail for the officer screen:
    {key: {'role_default', 'override' (None/True/False), 'effective'}}."""
    role_overrides = role_permission_overrides(db, role)
    user_overrides = user_permission_overrides(db, user_id)
    matrix = {}
    for key in PERMISSION_KEYS:
        role_grant = (role_overrides.get((role, key))
                      if (role, key) in role_overrides else default_allowed(key, role))
        if not assignable(key, role):
            role_grant = False
        override = user_overrides.get(key)
        matrix[key] = {
            'role_default': bool(role_grant),
            'override': override,
            'effective': bool(role_grant if override is None else override),
            'assignable': assignable(key, role),
        }
    return matrix


def users_with_permission(db, permission):
    """Active staff who actually hold `permission`.

    Resolved the same way a live request resolves it — role defaults, role
    overrides and per-officer overrides — so an alert reaches whoever can really
    act on it. Addressing a fixed role instead would go silently wrong the
    moment a cooperative reassigns the duty.
    """
    if permission not in PERMISSION_BY_KEY:
        return []
    try:
        rows = db.execute(
            "SELECT id, username, email, full_name, role, "
            "       COALESCE(is_super_admin, 0) AS is_super_admin "
            "FROM users WHERE COALESCE(is_active, 1) = 1 AND role <> 'member'"
        ).fetchall()
    except Exception:
        log.exception('Could not list users for permission %s', permission)
        return []

    holders = []
    for row in rows:
        try:
            allowed = effective_permissions(db, row['id'], row['role'] or '',
                                            bool(row['is_super_admin']))
        except Exception:
            allowed = {k for k in PERMISSION_KEYS if default_allowed(k, row['role'] or '')}
        if permission in allowed:
            holders.append(row)
    return holders


# ── Runtime checks ────────────────────────────────────────────────────────────

def _current_user_permissions():
    """Permissions of the logged-in user, cached for the duration of the request."""
    from flask import g, has_request_context
    from flask_login import current_user
    from database import get_db

    if not getattr(current_user, 'is_authenticated', False):
        return set()
    role = getattr(current_user, 'role', '') or ''
    is_super = bool(getattr(current_user, 'is_super_admin', False))
    if is_super or role == FULL_ACCESS_ROLE:
        return set(PERMISSION_KEYS)

    cache_key = f'_permissions_{getattr(current_user, "id", "anon")}'
    if has_request_context():
        cached = getattr(g, cache_key, None)
        if cached is not None:
            return cached
    try:
        allowed = effective_permissions(get_db(), current_user.id, role, is_super)
    except Exception:
        log.exception('Could not resolve permissions; falling back to role defaults')
        allowed = {k for k in PERMISSION_KEYS if default_allowed(k, role)}
    if has_request_context():
        setattr(g, cache_key, allowed)
    return allowed


def user_can(permission) -> bool:
    """True if the logged-in user holds `permission`."""
    if not permission:
        return False
    return permission in _current_user_permissions()


def can(permission) -> bool:
    """Template-friendly alias of user_can."""
    return user_can(permission)


def describe(permission) -> str:
    perm = PERMISSION_BY_KEY.get(permission)
    return perm['label'] if perm else permission


# ── Writes ────────────────────────────────────────────────────────────────────

def set_role_permission(db, role, permission, allowed):
    """Store a role default. Delete-then-insert keeps it backend-neutral."""
    if role == FULL_ACCESS_ROLE or permission not in PERMISSION_BY_KEY:
        return False
    if not assignable(permission, role):
        return False
    from datetime import datetime
    db.execute('DELETE FROM role_permissions WHERE role = ? AND permission = ?',
               (role, permission))
    db.execute('INSERT INTO role_permissions (role, permission, allowed, updated_at) '
               'VALUES (?, ?, ?, ?)', (role, permission, 1 if allowed else 0, datetime.now()))
    return True


def set_user_permission(db, user_id, permission, allowed, granted_by=None):
    """allowed=True grant, False deny, None clear the override (inherit role)."""
    if permission not in PERMISSION_BY_KEY:
        return False
    from datetime import datetime
    db.execute('DELETE FROM user_permissions WHERE user_id = ? AND permission = ?',
               (user_id, permission))
    if allowed is None:
        return True
    db.execute('INSERT INTO user_permissions (user_id, permission, allowed, granted_by, granted_at) '
               'VALUES (?, ?, ?, ?, ?)',
               (user_id, permission, 1 if allowed else 0, granted_by, datetime.now()))
    return True


def reset_role_permissions(db, role=None):
    """Drop stored role defaults so the built-in ones apply again."""
    if role:
        db.execute('DELETE FROM role_permissions WHERE role = ?', (role,))
    else:
        db.execute('DELETE FROM role_permissions')


def clear_user_permissions(db, user_id):
    db.execute('DELETE FROM user_permissions WHERE user_id = ?', (user_id,))


def officers_with_custom_access(db):
    """{user_id: count} of officers carrying per-user overrides — the admin
    screen flags these so a customised officer is never a surprise."""
    try:
        rows = db.execute(
            'SELECT user_id, COUNT(*) AS c FROM user_permissions GROUP BY user_id'
        ).fetchall()
    except Exception:
        return {}
    return {r['user_id']: r['c'] for r in rows}
