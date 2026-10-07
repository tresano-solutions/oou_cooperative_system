"""
loan_workflow.py — multi-level loan approval per the bye-laws.

Chain:  guarantors → secretary → treasurer → president → approved (disbursed)
Any stage can reject (with a reason). Guarantor consent must be complete before
the Secretary can review.
"""

from datetime import datetime

STAGE_GUARANTORS = 'guarantors'
STAGE_SECRETARY  = 'secretary'
STAGE_TREASURER  = 'treasurer'
STAGE_PRESIDENT  = 'president'
STAGE_APPROVED   = 'approved'
STAGE_REJECTED   = 'rejected'
STAGE_WITHDRAWN  = 'withdrawn'

STAGE_LABELS = {
    STAGE_GUARANTORS: 'Awaiting guarantor consent',
    STAGE_SECRETARY:  'Awaiting Secretary review',
    STAGE_TREASURER:  'Awaiting Treasurer verification',
    STAGE_PRESIDENT:  'Awaiting President approval',
    STAGE_APPROVED:   'Approved & disbursed',
    STAGE_REJECTED:   'Rejected',
    STAGE_WITHDRAWN:  'Withdrawn by applicant',
}

# Role that acts at each staff stage (admin may act at any stage)
STAGE_ROLE = {
    STAGE_SECRETARY: 'secretary',
    STAGE_TREASURER: 'treasurer',
    STAGE_PRESIDENT: 'admin',      # President = top authority (admin)
}
STAGE_ACTOR_LABEL = {
    STAGE_SECRETARY: 'Secretary',
    STAGE_TREASURER: 'Treasurer',
    STAGE_PRESIDENT: 'President',
}
NEXT_STAGE = {
    STAGE_GUARANTORS: STAGE_SECRETARY,
    STAGE_SECRETARY:  STAGE_TREASURER,
    STAGE_TREASURER:  STAGE_PRESIDENT,
    STAGE_PRESIDENT:  STAGE_APPROVED,
}


def can_act(role, stage):
    """True if a user with `role` may approve/reject a loan at `stage`."""
    if stage not in STAGE_ROLE:
        return False
    return role == 'admin' or role == STAGE_ROLE[stage]


STRICT_ABOVE_OFFICERS = 3      # strict when there are MORE active officers than this


def active_officer_count(db):
    row = db.execute(
        "SELECT COUNT(*) AS c FROM users WHERE role IN ('admin', 'treasurer', 'secretary', 'exco') "
        "AND COALESCE(is_active, 1) = 1").fetchone()
    return int(row['c'] or 0)


def segregation_enforced(db):
    """Is one-person-one-stage enforced for this cooperative?

    Automatic: strict once the cooperative has more than three active officers,
    permissive below that so a society with one or two officers can still run.
    The server operator can force it either way in the client .env:
    ENFORCE_SEGREGATION_OF_DUTIES=1 (always strict) or =0 (always permissive).
    """
    import os
    forced = os.environ.get('ENFORCE_SEGREGATION_OF_DUTIES')
    if forced == '1':
        return True
    if forced == '0':
        return False
    return active_officer_count(db) > STRICT_ABOVE_OFFICERS


def sod_conflict(db, loan, user_id, stage):
    """Why `user_id` may not act at `stage` of this loan, or '' if they may.

    Segregation of duties: nobody approves two stages of the same loan, and the
    officer who completed the due-diligence checks cannot give the final approval
    that releases the money.
    """
    if not segregation_enforced(db):
        return ''
    prior = db.execute(
        "SELECT stage FROM loan_approvals WHERE loan_id = ? AND acted_by = ? AND action = 'approved'",
        (loan['id'], user_id)).fetchone()
    if prior:
        return (f"You already approved this loan at the {STAGE_ACTOR_LABEL.get(prior['stage'], prior['stage'])} "
                f"stage. A different officer must approve each stage.")
    if (stage == STAGE_PRESIDENT and 'due_diligence_updated_by' in loan.keys()
            and loan['due_diligence_updated_by'] == user_id):
        return ('You completed the due-diligence checks on this loan, so a different officer '
                'must give the final approval.')
    return ''


def guarantors_required(db):
    row = db.execute("SELECT value FROM settings WHERE key = 'guarantors_required'").fetchone()
    try:
        return int(row['value']) if row and row['value'] else 2
    except (TypeError, ValueError):
        return 2


def record_action(db, loan_id, stage, action, acted_by=None, acted_by_name='', comment=''):
    """Append an entry to the loan's approval audit trail."""
    db.execute(
        '''INSERT INTO loan_approvals
           (loan_id, stage, action, acted_by, acted_by_name, acted_at, comment)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (loan_id, stage, action, acted_by, acted_by_name, datetime.now(), comment or '')
    )


def guarantor_progress(db, loan_id):
    """Return (accepted_count, required) for a loan's guarantors."""
    accepted = db.execute(
        "SELECT COUNT(*) FROM loan_guarantors WHERE loan_id = ? AND status = 'accepted'",
        (loan_id,)
    ).fetchone()[0]
    return accepted, guarantors_required(db)


def maybe_advance_from_guarantors(db, loan_id):
    """If enough guarantors have accepted, move the loan to Secretary review.
    Returns True if it advanced."""
    loan = db.execute('SELECT approval_stage FROM loans WHERE id = ?', (loan_id,)).fetchone()
    if not loan or loan['approval_stage'] != STAGE_GUARANTORS:
        return False
    accepted, required = guarantor_progress(db, loan_id)
    if accepted >= required:
        db.execute("UPDATE loans SET approval_stage = ? WHERE id = ?", (STAGE_SECRETARY, loan_id))
        return True
    return False
