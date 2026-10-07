"""
Affiliate channel — the two-tier partner network that introduces cooperatives.

A team member closes a cooperative; a team lead recruits and supports members.
This module covers recruitment, attribution, setup-fee commissions and
read-only affiliate statements. Payout processing remains separate.

Operator-only, like hq_billing: it lives on the HQ instance and is guarded by
the same hq_admin_required, so it never appears in a tenant's duty catalogue.
The two public routes (apply, accept appointment) are deliberately open.
"""
import re
import json
import hashlib
import secrets
from markupsafe import escape
from datetime import datetime
from functools import wraps

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for, current_app)
from flask_login import current_user

from database import get_db, last_insert_id
from extensions import csrf
from email_service import send_email
from blueprints.marketing import marketing_hq_enabled
from blueprints.hq_billing import (hq_admin_required, setup_paid, setup_charged,
                                   SETUP_ITEM_TYPE)
from utils import audit

affiliates_bp = Blueprint('affiliates', __name__)

TIER_MEMBER, TIER_LEAD = 'member', 'lead'

# applied -> approved (code issued, letter sent) -> active (MOU accepted).
# Only an active affiliate can be attributed a lead.
STATUS_APPLIED = 'applied'
STATUS_APPROVED = 'approved'
STATUS_ACTIVE = 'active'
STATUS_DECLINED = 'declined'
STATUS_SUSPENDED = 'suspended'
ATTRIBUTABLE = (STATUS_ACTIVE,)

# How many people a member must have introduced before they may apply to lead.
PROMOTION_THRESHOLD = 5


def _require_hq():
    if not marketing_hq_enabled():
        abort(404)


def public_route(f):
    """Open to the world, but only on the HQ instance."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        _require_hq()
        return f(*args, **kwargs)
    return wrapper


# ── Codes ────────────────────────────────────────────────────────────────────

def _initials(full_name):
    parts = [p for p in re.split(r'\s+', (full_name or '').strip()) if p]
    letters = ''.join(p[0] for p in parts[:2]).upper()
    return re.sub(r'[^A-Z]', '', letters) or 'AF'


def generate_code(db, full_name, tier):
    """A short human-quotable code: CMA-<initials><4 digits>.

    Affiliates read these down the phone and write them on paper forms, so it
    avoids look-alike characters and stays short enough to dictate.
    """
    prefix = 'CML' if tier == TIER_LEAD else 'CMA'
    stem = _initials(full_name)
    for _ in range(40):
        code = f"{prefix}-{stem}{secrets.randbelow(9000) + 1000}"
        if not db.execute('SELECT 1 FROM affiliates WHERE code = ?', (code,)).fetchone():
            return code
    # Pathological fallback — still unique, just less pretty.
    return f"{prefix}-{secrets.token_hex(4).upper()}"


def resolve_code(db, code):
    """The affiliate a referral code belongs to, or None.

    Matching is case- and space-insensitive because the code is typed by the
    cooperative, not by the affiliate.
    """
    cleaned = re.sub(r'\s+', '', (code or '')).upper()
    if not cleaned:
        return None
    return db.execute(
        "SELECT * FROM affiliates WHERE UPPER(REPLACE(code, ' ', '')) = ?", (cleaned,)
    ).fetchone()


def attributable(affiliate):
    return bool(affiliate) and affiliate['status'] in ATTRIBUTABLE


# ── Tree ─────────────────────────────────────────────────────────────────────

def team_of(db, lead_id):
    return db.execute('SELECT * FROM affiliates WHERE parent_id = ? ORDER BY full_name',
                      (lead_id,)).fetchall()


def recruit_count(db, affiliate_id):
    """People this affiliate introduced to the programme and who made it in.

    Counted on recruited_by, which never moves, so a member does not lose credit
    for a recruit who is later reassigned to another team.
    """
    return db.execute(
        "SELECT COUNT(*) FROM affiliates WHERE recruited_by = ? AND status IN ('approved','active')",
        (affiliate_id,)).fetchone()[0]


def may_apply_for_promotion(db, affiliate):
    if not affiliate or affiliate['tier'] != TIER_MEMBER or affiliate['status'] != STATUS_ACTIVE:
        return False
    return recruit_count(db, affiliate['id']) >= PROMOTION_THRESHOLD


# ── Terms of reference (stated in the appointment letter) ────────────────────
# Deliberately explicit that earnings come only from cooperatives that sign and
# pay: an affiliate scheme that pays for recruitment is a pyramid scheme, and
# the cooperatives we sell to cannot be seen dealing with one.
MOU_TERMS = [
    ('What you do',
     'You introduce cooperative societies to CoopMS, explain the product honestly, and '
     'support them through onboarding. You are an independent contractor, not an employee, '
     'and you may not hold yourself out as one.'),
    ('How you are paid',
     'You earn a percentage of the one-off setup fee of each cooperative you introduce that '
     'signs and pays. Commission is calculated on money the cooperative has actually paid, so '
     'a part-paid setup fee earns its part and the balance is earned as it is collected.'),
    ('What you are not paid for',
     'Nothing is paid for recruiting other affiliates, and you are never asked to pay anything '
     'to join or to be promoted. Earnings come only from cooperatives that buy the product.'),
    ('Attribution',
     'A cooperative is credited to you when your affiliate code is recorded on their enquiry or '
     'onboarding form. Where two affiliates claim the same cooperative, the first recorded '
     'introduction stands.'),
    ('Teams',
     'If you are placed under a team lead, a share of your commission goes to that lead in '
     'return for their support and supervision. Your own rate is unchanged by whether you are '
     'in a team.'),
    ('Payment and deductions',
     'Earnings accumulate and are paid once they pass the payout threshold. Withholding tax and '
     'any other statutory deduction is applied at payment.'),
    ('Honest selling',
     'You may not misrepresent the product, its price, or its regulatory standing, promise '
     'features that do not exist, or register a cooperative without the knowledge and consent '
     'of its officers. Doing so ends your appointment and forfeits unpaid commission.'),
    ('Confidentiality and data',
     'Information about cooperatives, their members and their finances that you learn through '
     'this appointment is confidential, and you will handle personal data lawfully and only for '
     'the purpose of the introduction.'),
    ('Ending the appointment',
     'Either side may end this appointment in writing at any time. Commission already earned on '
     'cooperatives that have paid remains payable; commission is reversed where a cooperative '
     'is refunded.'),
]


# ── Public: apply to join ────────────────────────────────────────────────────

@affiliates_bp.route('/affiliates/apply', methods=['GET', 'POST'])
@public_route
@csrf.exempt
def apply():
    """Open application. Nothing here grants membership — compliance reviews and
    interviews every applicant, and only an accepted appointment activates them."""
    db = get_db()
    if request.method == 'GET':
        return render_template('affiliates/apply.html')

    full_name = (request.form.get('full_name') or '').strip()
    email = (request.form.get('email') or '').strip().lower()
    phone = (request.form.get('phone') or '').strip()
    referrer_code = (request.form.get('referrer_code') or '').strip()
    if not full_name or not email or '@' not in email:
        flash('Please give your full name and a valid email address.', 'danger')
        return render_template('affiliates/apply.html'), 400
    if db.execute('SELECT 1 FROM affiliates WHERE email = ?', (email,)).fetchone():
        flash('There is already an application under that email address. '
              'We will be in touch.', 'info')
        return redirect(url_for('affiliates.apply'))

    recruiter = resolve_code(db, referrer_code) if referrer_code else None
    db.execute(
        'INSERT INTO affiliates (full_name, email, phone, tier, status, recruited_by, '
        ' parent_id, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (full_name, email, phone, TIER_MEMBER, STATUS_APPLIED,
         recruiter['id'] if recruiter else None,
         # A member's recruit joins that member's own team lead until the
         # recruiter is promoted and forms a team of their own.
         (recruiter['id'] if recruiter and recruiter['tier'] == TIER_LEAD
          else (recruiter['parent_id'] if recruiter else None)),
         (request.form.get('about') or '').strip()))
    db.commit()
    flash('Thank you — your application has been received. Our compliance team will '
          'review it and contact you to arrange an interview.', 'success')
    return redirect(url_for('affiliates.apply'))


# ── Public: accept the appointment ───────────────────────────────────────────

@affiliates_bp.route('/affiliates/accept/<token>', methods=['GET', 'POST'])
@public_route
@csrf.exempt
def accept_appointment(token):
    """The link in the appointment letter. Acceptance of the terms of reference
    is what puts an approved affiliate into the programme."""
    db = get_db()
    aff = db.execute('SELECT * FROM affiliates WHERE accept_token = ?', (token,)).fetchone()
    if not aff:
        abort(404)
    if aff['status'] == STATUS_ACTIVE:
        return render_template('affiliates/accepted.html', affiliate=aff, already=True)
    if aff['status'] != STATUS_APPROVED:
        flash('This appointment is no longer open for acceptance.', 'warning')
        return render_template('affiliates/accepted.html', affiliate=aff, already=False,
                               closed=True)

    snapshot = json.dumps(MOU_TERMS, ensure_ascii=False)
    version = hashlib.sha256(snapshot.encode('utf-8')).hexdigest()

    if request.method == 'GET':
        return render_template('affiliates/accept.html', affiliate=aff, terms=MOU_TERMS, terms_version=version)

    if request.form.get('terms_version') != version:
        flash('Please review the current terms before signing.', 'warning')
        return render_template('affiliates/accept.html', affiliate=aff, terms=MOU_TERMS, terms_version=version), 400

    if not request.form.get('accept_terms'):
        flash('Please tick to confirm you accept the terms of reference.', 'danger')
        return render_template('affiliates/accept.html', affiliate=aff, terms=MOU_TERMS, terms_version=version), 400
    signature = (request.form.get('signature_name') or '').strip()
    if not signature or len(signature) > 200:
        flash('Type your full name to sign.', 'danger')
        return render_template('affiliates/accept.html', affiliate=aff, terms=MOU_TERMS, terms_version=version), 400

    db.execute("UPDATE affiliates SET status = ?, accepted_at = ?, accepted_ip = ?, "
               "signature_name = ?, accepted_terms = ?, accepted_terms_version = ?, "
               "updated_at = ? WHERE id = ? AND status = ?",
               (STATUS_ACTIVE, datetime.now(),
                (request.remote_addr or '')[:60], signature, snapshot, version,
                datetime.now(), aff['id'], STATUS_APPROVED))
    audit(db, 'AFFILIATE_ACCEPTED', 'affiliates',
          f"Affiliate {aff['id']} accepted terms {version}")
    db.commit()
    aff = db.execute('SELECT * FROM affiliates WHERE id = ?', (aff['id'],)).fetchone()
    return render_template('affiliates/accepted.html', affiliate=aff, already=False)


# ── Appointment letter ───────────────────────────────────────────────────────

def _base_url():
    from blueprints.marketing import _hq_base_url
    return _hq_base_url()


def _send_appointment(db, aff):
    """The congratulatory letter: states the code, sets out the MOU, and carries
    the acceptance link. Best-effort — approval still stands if mail is down, and
    the link can be copied from the affiliate's row."""
    link = f"{_base_url()}{url_for('affiliates.accept_appointment', token=aff['accept_token'])}"
    terms_html = ''.join(
        f"<p style='margin:0 0 10px'><strong>{title}</strong><br>{body}</p>"
        for title, body in MOU_TERMS)
    html = f"""
      <p>Dear {escape(aff['full_name'])},</p>
      <p>Congratulations — your application to join the CoopMS affiliate channel has been
         approved following compliance review.</p>
      <p style="font-size:18px"><strong>Your affiliate code is
         <span style="font-family:monospace">{aff['code']}</span></strong></p>
      <p>Quote this code on every cooperative enquiry and onboarding form you complete. It is
         how each introduction is credited to you.</p>
      <h3 style="margin-top:22px">Terms of reference</h3>
      {terms_html}
      <p style="margin-top:22px">
        <a href="{link}" style="background:#082b66;color:#fff;padding:12px 20px;
           border-radius:6px;text-decoration:none;display:inline-block">
           Accept the appointment</a></p>
      <p style="color:#475569;font-size:13px">You join the programme only once you accept.
         If the button does not work, open this link:<br>{link}</p>
      <p style="color:#475569;font-size:13px">Once you have accepted, you can see your
         introductions and earnings any time at
         <a href="{_base_url()}{url_for('affiliates.request_statement')}">your affiliate
         statement</a> — no password needed, we email you a link.</p>
    """
    try:
        return bool(send_email(aff['email'], 'Your CoopMS affiliate appointment', html))
    except Exception as exc:                      # pragma: no cover - network
        current_app.logger.warning('Affiliate appointment email failed for %s: %s',
                                   aff['email'], exc)
        return False


# ── HQ: the affiliate register ───────────────────────────────────────────────

@affiliates_bp.route('/hq/affiliates')
@hq_admin_required
def register():
    db = get_db()
    status = (request.args.get('status') or '').strip()
    where, params = '', []
    if status in (STATUS_APPLIED, STATUS_APPROVED, STATUS_ACTIVE, STATUS_DECLINED, STATUS_SUSPENDED):
        where, params = 'WHERE a.status = ?', [status]
    rows = db.execute(f'''
        SELECT a.*, p.full_name AS lead_name, p.code AS lead_code,
               r.full_name AS recruiter_name
        FROM affiliates a
        LEFT JOIN affiliates p ON p.id = a.parent_id
        LEFT JOIN affiliates r ON r.id = a.recruited_by
        {where}
        ORDER BY CASE a.status WHEN 'applied' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                 a.full_name
    ''', params).fetchall()
    counts = {r['status']: r['n'] for r in db.execute(
        'SELECT status, COUNT(*) AS n FROM affiliates GROUP BY status').fetchall()}
    leads = db.execute("SELECT * FROM affiliates WHERE tier = 'lead' AND status = 'active' "
                       'ORDER BY full_name').fetchall()
    promotable = {a['id'] for a in rows if may_apply_for_promotion(db, a)}
    recruits = {a['id']: recruit_count(db, a['id']) for a in rows}
    return render_template('affiliates/register.html', affiliates=rows, counts=counts,
                           leads=leads, active_status=status, promotable=promotable,
                           recruits=recruits, threshold=PROMOTION_THRESHOLD,
                           base_url=_base_url())


@affiliates_bp.route('/hq/affiliates/<int:aff_id>/review', methods=['POST'])
@hq_admin_required
def review(aff_id):
    """Compliance outcome. Approving issues the code and sends the appointment
    letter; the affiliate is still not in the programme until they accept."""
    db = get_db()
    aff = db.execute('SELECT * FROM affiliates WHERE id = ?', (aff_id,)).fetchone()
    if not aff:
        abort(404)
    action = (request.form.get('action') or '').strip()

    if action == 'decline':
        reason = (request.form.get('reason') or '').strip()
        db.execute('UPDATE affiliates SET status = ?, declined_reason = ?, reviewed_at = ?, '
                   'reviewed_by = ?, updated_at = ? WHERE id = ?',
                   (STATUS_DECLINED, reason, datetime.now(), current_user.id,
                    datetime.now(), aff_id))
        audit(db, 'AFFILIATE_DECLINED', 'affiliates', f"{aff['full_name']}: {reason}")
        db.commit()
        flash(f"{aff['full_name']} declined.", 'info')
        return redirect(url_for('affiliates.register'))

    if action != 'approve':
        flash('Choose approve or decline.', 'warning')
        return redirect(url_for('affiliates.register'))
    if aff['status'] not in (STATUS_APPLIED, STATUS_DECLINED):
        flash('Only an applicant can be approved.', 'warning')
        return redirect(url_for('affiliates.register'))

    tier = TIER_LEAD if (request.form.get('tier') or '') == TIER_LEAD else TIER_MEMBER
    parent_id = request.form.get('parent_id') or None
    if tier == TIER_LEAD:
        parent_id = None                     # a lead sits at the top of their own team
    code = aff['code'] or generate_code(db, aff['full_name'], tier)
    token = aff['accept_token'] or secrets.token_urlsafe(24)
    db.execute('UPDATE affiliates SET status = ?, tier = ?, parent_id = ?, code = ?, '
               'accept_token = ?, approved_at = ?, reviewed_at = ?, reviewed_by = ?, '
               'updated_at = ? WHERE id = ?',
               (STATUS_APPROVED, tier, parent_id, code, token, datetime.now(),
                datetime.now(), current_user.id, datetime.now(), aff_id))
    db.commit()
    aff = db.execute('SELECT * FROM affiliates WHERE id = ?', (aff_id,)).fetchone()
    sent = _send_appointment(db, aff)
    audit(db, 'AFFILIATE_APPROVED', 'affiliates',
          f"{aff['full_name']} approved as {tier}, code {code}")
    db.commit()
    flash(f"{aff['full_name']} approved — code {code}. "
          + ('Appointment letter sent.' if sent else
             'Could not send the letter; copy the acceptance link from their row.'),
          'success' if sent else 'warning')
    return redirect(url_for('affiliates.register'))


@affiliates_bp.route('/hq/affiliates/<int:aff_id>/resend', methods=['POST'])
@hq_admin_required
def resend_appointment(aff_id):
    db = get_db()
    aff = db.execute('SELECT * FROM affiliates WHERE id = ?', (aff_id,)).fetchone()
    if not aff or aff['status'] != STATUS_APPROVED:
        flash('Only an approved affiliate who has not yet accepted can be re-sent.', 'warning')
        return redirect(url_for('affiliates.register'))
    ok = _send_appointment(db, aff)
    audit(db, 'AFFILIATE_LETTER_RESENT', 'affiliates', aff['full_name'])
    db.commit()
    flash('Appointment letter re-sent.' if ok else 'Could not send the letter.',
          'success' if ok else 'danger')
    return redirect(url_for('affiliates.register'))


@affiliates_bp.route('/hq/affiliates/<int:aff_id>/team', methods=['POST'])
@hq_admin_required
def set_team(aff_id):
    """Move a member to another lead, promote, suspend, or restore."""
    db = get_db()
    aff = db.execute('SELECT * FROM affiliates WHERE id = ?', (aff_id,)).fetchone()
    if not aff:
        abort(404)
    action = (request.form.get('action') or 'move').strip()

    if action in ('suspend', 'restore'):
        new_status = STATUS_SUSPENDED if action == 'suspend' else STATUS_ACTIVE
        if action == 'restore' and not aff['accepted_at']:
            flash('They have not accepted their appointment, so there is nothing to restore.',
                  'warning')
            return redirect(url_for('affiliates.register'))
        db.execute('UPDATE affiliates SET status = ?, updated_at = ? WHERE id = ?',
                   (new_status, datetime.now(), aff_id))
        audit(db, 'AFFILIATE_STATUS', 'affiliates', f"{aff['full_name']} -> {new_status}")
        db.commit()
        flash(f"{aff['full_name']} {'suspended' if action == 'suspend' else 'restored'}.", 'info')
        return redirect(url_for('affiliates.register'))

    if action == 'promote':
        if aff['tier'] == TIER_LEAD:
            flash('Already a team lead.', 'info')
            return redirect(url_for('affiliates.register'))
        n = recruit_count(db, aff['id'])
        if n < PROMOTION_THRESHOLD and not request.form.get('force'):
            flash(f"{aff['full_name']} has introduced {n} affiliate(s); "
                  f"{PROMOTION_THRESHOLD} are needed. Tick override to promote anyway.",
                  'warning')
            return redirect(url_for('affiliates.register'))
        # They leave their old lead, and the people they introduced follow them
        # into the new team.
        db.execute('UPDATE affiliates SET tier = ?, parent_id = NULL, promoted_at = ?, '
                   'updated_at = ? WHERE id = ?',
                   (TIER_LEAD, datetime.now(), datetime.now(), aff_id))
        moved = db.execute('SELECT COUNT(*) FROM affiliates WHERE recruited_by = ?',
                           (aff_id,)).fetchone()[0]
        db.execute('UPDATE affiliates SET parent_id = ?, updated_at = ? WHERE recruited_by = ?',
                   (aff_id, datetime.now(), aff_id))
        audit(db, 'AFFILIATE_PROMOTED', 'affiliates',
              f"{aff['full_name']} promoted to team lead; {moved} recruit(s) moved across")
        db.commit()
        flash(f"{aff['full_name']} is now a team lead. {moved} recruit(s) moved into their team.",
              'success')
        return redirect(url_for('affiliates.register'))

    parent_id = request.form.get('parent_id') or None
    if parent_id and int(parent_id) == aff_id:
        flash('An affiliate cannot lead themselves.', 'danger')
        return redirect(url_for('affiliates.register'))
    db.execute('UPDATE affiliates SET parent_id = ?, updated_at = ? WHERE id = ?',
               (parent_id, datetime.now(), aff_id))
    audit(db, 'AFFILIATE_TEAM', 'affiliates', f"{aff['full_name']} moved team")
    db.commit()
    flash('Team updated.', 'success')
    return redirect(url_for('affiliates.register'))


# ── Attribution ──────────────────────────────────────────────────────────────

def attach_code_to_lead(db, lead_id, code):
    """Record the referral code on a lead and resolve it to an affiliate.

    The code is stored exactly as it arrived even when it resolves to nobody, so
    a mistyped or retired code shows up as unresolved and can be corrected,
    rather than the introduction silently going unpaid.
    """
    raw = (code or '').strip()
    if not raw:
        return None
    aff = resolve_code(db, raw)
    db.execute('UPDATE marketing_leads SET affiliate_code = ?, affiliate_id = ? WHERE id = ?',
               (raw, aff['id'] if attributable(aff) else None, lead_id))
    return aff


def link_lead_to_client(db, lead_id, client_id):
    """Tie a CRM lead to the billing client it became, and snapshot who
    introduced it. Attribution is copied onto the client so that later edits to
    the lead cannot move a commission after the fact."""
    lead = db.execute('SELECT * FROM marketing_leads WHERE id = ?', (lead_id,)).fetchone()
    client = db.execute('SELECT * FROM hq_clients WHERE id = ?', (client_id,)).fetchone()
    if not lead or not client:
        return None
    db.execute('UPDATE marketing_leads SET client_id = ? WHERE id = ?', (client_id, lead_id))
    # First introduction wins: never overwrite an attribution already on a client.
    if not client['affiliate_id']:
        db.execute('UPDATE hq_clients SET lead_id = ?, affiliate_id = ?, attributed_at = ? '
                   'WHERE id = ?',
                   (lead_id, lead['affiliate_id'], datetime.now(), client_id))
    else:
        db.execute('UPDATE hq_clients SET lead_id = COALESCE(lead_id, ?) WHERE id = ?',
                   (lead_id, client_id))
    return lead['affiliate_id']


def attribution_rows(db):
    """Per-affiliate introductions and what they turned into.

    Money comes from the ledger side (hq_billing.setup_paid) rather than from
    anything stored here, so this report cannot drift from what was invoiced.
    """
    rows = []
    for aff in db.execute(
            "SELECT a.*, p.full_name AS lead_name FROM affiliates a "
            'LEFT JOIN affiliates p ON p.id = a.parent_id '
            "WHERE a.status IN ('active','suspended') ORDER BY a.tier DESC, a.full_name"
    ).fetchall():
        leads = db.execute('SELECT COUNT(*) FROM marketing_leads WHERE affiliate_id = ?',
                           (aff['id'],)).fetchone()[0]
        clients = db.execute('SELECT * FROM hq_clients WHERE affiliate_id = ?',
                             (aff['id'],)).fetchall()
        charged = sum(setup_charged(db, c['id']) for c in clients)
        paid = sum(setup_paid(db, c['id']) for c in clients)
        rows.append({
            'affiliate': aff,
            'leads': leads,
            'clients': clients,
            'client_count': len(clients),
            'setup_charged': round(charged, 2),
            'setup_paid': round(paid, 2),
            'outstanding': round(charged - paid, 2),
        })
    return rows


@affiliates_bp.route('/hq/affiliates/attribution')
@hq_admin_required
def attribution():
    """Who introduced what, and how much of it has actually been collected.
    No commission is accrued or owed from this screen — it reports only."""
    db = get_db()
    rows = attribution_rows(db)
    unresolved = db.execute(
        "SELECT id, society_name, full_name, affiliate_code, created_at FROM marketing_leads "
        "WHERE affiliate_code IS NOT NULL AND affiliate_code != '' AND affiliate_id IS NULL "
        'ORDER BY created_at DESC').fetchall()
    unlinked = db.execute(
        'SELECT * FROM hq_clients WHERE lead_id IS NULL ORDER BY name').fetchall()
    return render_template('affiliates/attribution.html', rows=rows, unresolved=unresolved,
                           unlinked=unlinked,
                           totals={
                               'leads': sum(r['leads'] for r in rows),
                               'clients': sum(r['client_count'] for r in rows),
                               'paid': round(sum(r['setup_paid'] for r in rows), 2),
                               'outstanding': round(sum(r['outstanding'] for r in rows), 2),
                           })


@affiliates_bp.route('/hq/affiliates/leads/<int:lead_id>/code', methods=['POST'])
@hq_admin_required
def fix_lead_code(lead_id):
    """Correct or add the referral code on a lead — for codes given verbally,
    mistyped by the cooperative, or added after the enquiry arrived."""
    db = get_db()
    if not db.execute('SELECT 1 FROM marketing_leads WHERE id = ?', (lead_id,)).fetchone():
        abort(404)
    code = (request.form.get('affiliate_code') or '').strip()
    if not code:
        db.execute('UPDATE marketing_leads SET affiliate_code = NULL, affiliate_id = NULL '
                   'WHERE id = ?', (lead_id,))
        audit(db, 'LEAD_ATTRIBUTION_CLEARED', 'affiliates', f'Lead #{lead_id}')
        db.commit()
        flash('Attribution cleared for that lead.', 'info')
        return redirect(url_for('affiliates.attribution'))
    aff = attach_code_to_lead(db, lead_id, code)
    audit(db, 'LEAD_ATTRIBUTION_SET', 'affiliates',
          f"Lead #{lead_id} -> {code} ({'resolved' if attributable(aff) else 'unresolved'})")
    db.commit()
    if attributable(aff):
        flash(f"Lead credited to {aff['full_name']} ({aff['code']}).", 'success')
    elif aff:
        flash(f"{aff['code']} belongs to {aff['full_name']}, who is {aff['status']} — "
              f'the lead is recorded but not credited.', 'warning')
    else:
        flash(f'No active affiliate has the code {code}. It is recorded so you can correct it.',
              'warning')
    return redirect(url_for('affiliates.attribution'))


@affiliates_bp.route('/hq/affiliates/clients/<int:client_id>/link', methods=['POST'])
@hq_admin_required
def link_client(client_id):
    """Say which enquiry a billing client came from. This is what joins the CRM
    to billing, and therefore what makes an introduction payable."""
    db = get_db()
    client = db.execute('SELECT * FROM hq_clients WHERE id = ?', (client_id,)).fetchone()
    if not client:
        abort(404)
    lead_id = request.form.get('lead_id')
    if not lead_id:
        db.execute('UPDATE hq_clients SET lead_id = NULL, affiliate_id = NULL, '
                   'attributed_at = NULL WHERE id = ?', (client_id,))
        audit(db, 'CLIENT_ATTRIBUTION_CLEARED', 'affiliates', client['name'])
        db.commit()
        flash(f"Attribution cleared for {client['name']}.", 'info')
        return redirect(url_for('affiliates.attribution'))
    aff_id = link_lead_to_client(db, int(lead_id), client_id)
    audit(db, 'CLIENT_LINKED_TO_LEAD', 'affiliates',
          f"{client['name']} linked to lead #{lead_id}")
    db.commit()
    flash(f"{client['name']} linked." + ('' if aff_id else
          ' That enquiry has no affiliate code, so nothing is credited.'),
          'success' if aff_id else 'warning')
    return redirect(url_for('affiliates.attribution'))


# ── Commission ───────────────────────────────────────────────────────────────
# The pool is a fixed share of the setup fee and is split between at most two
# people, so the cost of the channel is capped at the pool rate however the tree
# is shaped. A member with no team lead still earns only their own rate — the
# lead's share is simply not paid — because paying an unattached member the
# whole pool would make leaving a team the profitable move.

DEFAULT_POOL_RATE = 20.0
DEFAULT_MEMBER_RATE = 15.0
DEFAULT_LEAD_RATE = 5.0

ROLE_DIRECT = 'direct'
ROLE_OVERRIDE = 'override'
STATUS_EARNED = 'earned'
# The original earning, once cancelled.
STATUS_REVERSED = 'reversed'
# The compensating negative row that cancelled it. A distinct status because it
# is not itself a reversed earning, and because the uniqueness guard on earned
# rows must not see it.
STATUS_CLAWBACK = 'clawback'


def commission_rates(db):
    """Configured rates as percentages of the setup fee.

    member + lead should equal the pool; if a setting has been edited to break
    that, the pool is treated as the sum so the split can never quietly cost
    more than it claims to.
    """
    def _get(key, default):
        row = db.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        try:
            return max(0.0, float(row['value'])) if row and row['value'] not in (None, '') else default
        except (TypeError, ValueError):
            return default

    member = _get('affiliate_member_rate', DEFAULT_MEMBER_RATE)
    lead = _get('affiliate_lead_rate', DEFAULT_LEAD_RATE)
    pool = _get('affiliate_pool_rate', DEFAULT_POOL_RATE)
    if round(member + lead, 6) != round(pool, 6):
        pool = round(member + lead, 2)
    return {'pool': pool, 'member': member, 'lead': lead}


def _earner_split(db, affiliate):
    """Who earns what on one introduction, as (affiliate_id, role, rate) rows.

    A team lead who closed the deal himself takes the whole pool with no
    override, since there is nobody above him to share with.
    """
    rates = commission_rates(db)
    if affiliate['tier'] == TIER_LEAD:
        return [(affiliate['id'], ROLE_DIRECT, rates['pool'], None)]

    rows = [(affiliate['id'], ROLE_DIRECT, rates['member'], None)]
    parent_id = affiliate['parent_id']
    if parent_id:
        parent = db.execute("SELECT * FROM affiliates WHERE id = ? AND status = 'active'",
                            (parent_id,)).fetchone()
        if parent:
            rows.append((parent['id'], ROLE_OVERRIDE, rates['lead'], affiliate['id']))
    # No lead, or a lead who is no longer active: their share is simply not paid.
    return rows


def setup_paid_on_invoice(db, invoice_id):
    """Setup-fee money on one paid invoice. Commission follows cash, so this is
    what an earning event is measured on."""
    row = db.execute(
        "SELECT COALESCE(SUM(it.amount), 0) FROM hq_invoice_items it "
        "JOIN hq_invoices i ON i.id = it.invoice_id "
        "WHERE it.invoice_id = ? AND it.item_type = ? AND i.status = 'paid'",
        (invoice_id, SETUP_ITEM_TYPE)).fetchone()
    return round(float(row[0] or 0), 2)


def commission_plan(db, invoice_id):
    """Earn commission on the setup-fee cash of a newly paid invoice.

    Idempotent: a repeated mark-paid, or a gateway callback replayed by the
    provider, must not pay twice. Returns the rows written.
    """
    basis = setup_paid_on_invoice(db, invoice_id)
    if basis <= 0:
        return []
    inv = db.execute('SELECT * FROM hq_invoices WHERE id = ?', (invoice_id,)).fetchone()
    if not inv:
        return []
    client = db.execute('SELECT * FROM hq_clients WHERE id = ?', (inv['client_id'],)).fetchone()
    if not client or not client['affiliate_id']:
        return []                      # nobody introduced this cooperative
    affiliate = db.execute('SELECT * FROM affiliates WHERE id = ?',
                           (client['affiliate_id'],)).fetchone()
    if not affiliate or affiliate['status'] not in (STATUS_ACTIVE, STATUS_SUSPENDED):
        return []

    return [dict(affiliate_id=aff_id, role=role, rate=rate, source_id=source_id,
                 basis=basis, client_id=client['id'], invoice_number=inv['invoice_number'])
            for aff_id, role, rate, source_id in _earner_split(db, affiliate)]


def accrue_for_invoice(db, invoice_id, created_by=None, plan=None):
    # A retry uses the original payment-time rates and attribution, not today's.
    plan = commission_plan(db, invoice_id) if plan is None else plan
    if setup_paid_on_invoice(db, invoice_id) <= 0:
        return []
    written = []
    for item in plan:
        aff_id, role, rate, source_id = (item[k] for k in ('affiliate_id', 'role', 'rate', 'source_id'))
        basis = item['basis']
        existing = db.execute(
            'SELECT 1 FROM affiliate_commissions WHERE invoice_id = ? AND affiliate_id = ? '
            "AND role = ? AND status = 'earned'", (invoice_id, aff_id, role)).fetchone()
        if existing:
            continue
        amount = round(basis * rate / 100.0, 2)
        if amount <= 0:
            continue
        db.execute(
            'INSERT INTO affiliate_commissions (affiliate_id, client_id, invoice_id, '
            ' source_affiliate_id, role, basis_amount, rate, amount, status, note, created_by) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (aff_id, item['client_id'], invoice_id, source_id, role, basis, rate, amount,
             STATUS_EARNED,
             f"{rate:g}% of setup fee collected on {item['invoice_number']}", created_by))
        written.append({'affiliate_id': aff_id, 'role': role, 'rate': rate, 'amount': amount})
    return written


def reverse_for_invoice(db, invoice_id, reason='', created_by=None):
    """Claw back commission when the money behind it goes away.

    Follows the ledger's reversal convention: a compensating negative row
    pointing at the original, so an affiliate's statement shows what was earned,
    what was taken back and why, instead of the earning silently vanishing.
    """
    rows = db.execute(
        "SELECT * FROM affiliate_commissions WHERE invoice_id = ? AND status = 'earned'",
        (invoice_id,)).fetchall()
    reversed_rows = []
    for r in rows:
        db.execute(
            'INSERT INTO affiliate_commissions (affiliate_id, client_id, invoice_id, '
            ' source_affiliate_id, role, basis_amount, rate, amount, status, note, '
            ' reversal_of, created_by) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (r['affiliate_id'], r['client_id'], invoice_id, r['source_affiliate_id'],
             r['role'], -float(r['basis_amount'] or 0), r['rate'], -float(r['amount'] or 0),
             STATUS_CLAWBACK, f"Reversal: {reason}" if reason else 'Reversal', r['id'],
             created_by))
        db.execute("UPDATE affiliate_commissions SET status = 'reversed', reversed_at = ? "
                   'WHERE id = ?', (datetime.now(), r['id']))
        reversed_rows.append(r)
    return reversed_rows


def affiliate_balance(db, affiliate_id):
    """What this affiliate has earned net of reversals. Nothing here is paid —
    payout batches are a later slice."""
    row = db.execute(
        'SELECT COALESCE(SUM(amount), 0) FROM affiliate_commissions WHERE affiliate_id = ?',
        (affiliate_id,)).fetchone()
    return round(float(row[0] or 0), 2)


# ── Statements ───────────────────────────────────────────────────────────────

@affiliates_bp.route('/hq/affiliates/commissions')
@hq_admin_required
def commissions():
    """What every affiliate has earned, and on what."""
    db = get_db()
    rates = commission_rates(db)
    rows = db.execute('''
        SELECT c.*, a.full_name, a.code, a.tier, cl.name AS client_name,
               i.invoice_number, s.full_name AS source_name
        FROM affiliate_commissions c
        JOIN affiliates a ON a.id = c.affiliate_id
        LEFT JOIN hq_clients cl ON cl.id = c.client_id
        LEFT JOIN hq_invoices i ON i.id = c.invoice_id
        LEFT JOIN affiliates s ON s.id = c.source_affiliate_id
        ORDER BY c.created_at DESC, c.id DESC
    ''').fetchall()
    totals = db.execute('''
        SELECT a.id, a.full_name, a.code, a.tier,
               COALESCE(SUM(c.amount), 0) AS balance,
               COALESCE(SUM(CASE WHEN c.role = 'direct' AND c.amount > 0 THEN c.amount END), 0) AS direct,
               COALESCE(SUM(CASE WHEN c.role = 'override' AND c.amount > 0 THEN c.amount END), 0) AS override_earned
        FROM affiliates a JOIN affiliate_commissions c ON c.affiliate_id = a.id
        GROUP BY a.id, a.full_name, a.code, a.tier
        HAVING COALESCE(SUM(c.amount), 0) <> 0 OR COUNT(c.id) > 0
        ORDER BY balance DESC
    ''').fetchall()
    return render_template('affiliates/commissions.html', rows=rows, totals=totals,
                           failures=db.execute("SELECT * FROM affiliate_commission_jobs WHERE status = 'pending' ORDER BY id").fetchall(),
                           rates=rates,
                           grand=round(sum(float(t['balance'] or 0) for t in totals), 2))


@affiliates_bp.route('/hq/affiliates/commission-jobs/<int:job_id>/retry', methods=['POST'])
@hq_admin_required
def retry_commission(job_id):
    from blueprints.hq_billing import _run_commission_job
    db = get_db()
    if not db.execute('SELECT id FROM affiliate_commission_jobs WHERE id = ?', (job_id,)).fetchone():
        abort(404)
    _run_commission_job(db, job_id)
    status = db.execute('SELECT status FROM affiliate_commission_jobs WHERE id = ?', (job_id,)).fetchone()['status']
    db.commit()
    flash('Commission processing completed.' if status == 'complete' else 'Processing still failed; the job remains pending.',
          'success' if status == 'complete' else 'warning')
    return redirect(url_for('affiliates.commissions'))


@affiliates_bp.route('/hq/affiliates/rates', methods=['POST'])
@hq_admin_required
def save_rates():
    """Back-office commission rates, as percentages of the setup fee."""
    db = get_db()

    def _num(key, default):
        try:
            return max(0.0, min(100.0, float(request.form.get(key) or default)))
        except (TypeError, ValueError):
            return default

    member = _num('affiliate_member_rate', DEFAULT_MEMBER_RATE)
    lead = _num('affiliate_lead_rate', DEFAULT_LEAD_RATE)
    pool = round(member + lead, 2)
    if pool > 100:
        flash('The member and lead shares cannot exceed 100% of the setup fee.', 'danger')
        return redirect(url_for('affiliates.commissions'))
    for key, val in (('affiliate_member_rate', member), ('affiliate_lead_rate', lead),
                     ('affiliate_pool_rate', pool)):
        db.execute('DELETE FROM settings WHERE key = ?', (key,))
        db.execute('INSERT INTO settings (key, value) VALUES (?, ?)', (key, str(val)))
    audit(db, 'AFFILIATE_RATES', 'affiliates',
          f'Member {member:g}%, lead {lead:g}%, pool {pool:g}% of setup fee')
    db.commit()
    flash(f'Rates saved — member {member:g}%, team lead {lead:g}%, '
          f'{pool:g}% of each setup fee in total. Existing commission is unchanged.', 'success')
    return redirect(url_for('affiliates.commissions'))


# ── Affiliate statement ──────────────────────────────────────────────────────
# Read-only, and reached by a short-lived emailed link rather than an account,
# because affiliates are external contractors and this instance also runs
# billing and tenant suspension.

PORTAL_TOKEN_MINUTES = 45
_STATEMENT_REQUESTS = {}       # ip -> [timestamps], to blunt enumeration attempts


def _statement_rate_limited(ip):
    import time
    now, window, cap = time.time(), 15 * 60, 6
    hits = [t for t in _STATEMENT_REQUESTS.get(ip, []) if now - t < window]
    if len(hits) >= cap:
        _STATEMENT_REQUESTS[ip] = hits
        return True
    hits.append(now)
    _STATEMENT_REQUESTS[ip] = hits
    return False


def issue_portal_token(db, affiliate_id, ip=''):
    from datetime import timedelta
    token = secrets.token_urlsafe(32)
    db.execute('INSERT INTO affiliate_portal_tokens (affiliate_id, token, expires_at, created_ip) '
               'VALUES (?, ?, ?, ?)',
               (affiliate_id, token,
                datetime.now() + timedelta(minutes=PORTAL_TOKEN_MINUTES), (ip or '')[:60]))
    return token


def _affiliate_for_token(db, token):
    row = db.execute('SELECT * FROM affiliate_portal_tokens WHERE token = ?', (token,)).fetchone()
    if not row:
        return None, 'unknown'
    expires = row['expires_at']
    if not hasattr(expires, 'year'):
        try:
            expires = datetime.fromisoformat(str(expires).replace('T', ' ').split('.')[0])
        except ValueError:
            return None, 'unknown'
    if expires < datetime.now():
        return None, 'expired'
    aff = db.execute('SELECT * FROM affiliates WHERE id = ?', (row['affiliate_id'],)).fetchone()
    if not aff or aff['status'] not in (STATUS_ACTIVE, STATUS_SUSPENDED):
        return None, 'closed'
    db.execute('UPDATE affiliate_portal_tokens SET last_used_at = ? WHERE id = ?',
               (datetime.now(), row['id']))
    return aff, 'ok'


def _direct_rate_for(db, affiliate):
    """What this affiliate earns on a cooperative they introduced themselves."""
    rates = commission_rates(db)
    return rates['pool'] if affiliate['tier'] == TIER_LEAD else rates['member']


def statement_context(db, affiliate):
    """Everything an affiliate should see about their own introductions.

    Earned and pipeline are kept strictly apart: earned is money the cooperative
    has paid, pipeline is what a still-unpaid setup fee would produce. Showing
    them as one number is how commission disputes start.
    """
    rates = commission_rates(db)
    my_rate = _direct_rate_for(db, affiliate)

    # Cooperatives this affiliate introduced.
    own = []
    pipeline_total = earned_total = 0.0
    for client in db.execute('SELECT * FROM hq_clients WHERE affiliate_id = ? ORDER BY name',
                             (affiliate['id'],)).fetchall():
        billed = setup_charged(db, client['id'])
        paid = setup_paid(db, client['id'])
        outstanding = round(billed - paid, 2)
        earned = round(float(db.execute(
            'SELECT COALESCE(SUM(amount), 0) FROM affiliate_commissions '
            "WHERE affiliate_id = ? AND client_id = ? AND role = 'direct'",
            (affiliate['id'], client['id'])).fetchone()[0] or 0), 2)
        potential = round(max(0.0, outstanding) * my_rate / 100.0, 2)
        earned_total += earned
        pipeline_total += potential
        own.append({'client': client, 'billed': billed, 'paid': paid,
                    'outstanding': outstanding, 'earned': earned, 'potential': potential})

    # For a team lead: the same again across their team, at the override rate.
    team = []
    team_earned = team_pipeline = 0.0
    if affiliate['tier'] == TIER_LEAD:
        for member in team_of(db, affiliate['id']):
            m_billed = m_paid = 0.0
            for client in db.execute('SELECT * FROM hq_clients WHERE affiliate_id = ?',
                                     (member['id'],)).fetchall():
                m_billed += setup_charged(db, client['id'])
                m_paid += setup_paid(db, client['id'])
            m_earned = round(float(db.execute(
                'SELECT COALESCE(SUM(amount), 0) FROM affiliate_commissions '
                "WHERE affiliate_id = ? AND source_affiliate_id = ? AND role = 'override'",
                (affiliate['id'], member['id'])).fetchone()[0] or 0), 2)
            m_potential = round(max(0.0, m_billed - m_paid) * rates['lead'] / 100.0, 2)
            team_earned += m_earned
            team_pipeline += m_potential
            team.append({'member': member, 'billed': round(m_billed, 2),
                         'paid': round(m_paid, 2), 'earned': m_earned,
                         'potential': m_potential})

    entries = db.execute('''
        SELECT c.*, cl.name AS client_name, i.invoice_number, s.full_name AS source_name
        FROM affiliate_commissions c
        LEFT JOIN hq_clients cl ON cl.id = c.client_id
        LEFT JOIN hq_invoices i ON i.id = c.invoice_id
        LEFT JOIN affiliates s ON s.id = c.source_affiliate_id
        WHERE c.affiliate_id = ? ORDER BY c.created_at DESC, c.id DESC''',
        (affiliate['id'],)).fetchall()

    return {
        'affiliate': affiliate,
        'rates': rates,
        'my_rate': my_rate,
        'own': own,
        'team': team,
        'entries': entries,
        'balance': affiliate_balance(db, affiliate['id']),
        'earned_own': round(earned_total, 2),
        'earned_team': round(team_earned, 2),
        'pipeline': round(pipeline_total + team_pipeline, 2),
        'clawed_back': round(abs(float(db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM affiliate_commissions "
            "WHERE affiliate_id = ? AND status = 'clawback'",
            (affiliate['id'],)).fetchone()[0] or 0)), 2),
    }


def _send_statement_link(db, aff, token):
    link = f"{_base_url()}{url_for('affiliates.statement', token=token)}"
    html = f"""
      <p>Hello {escape(aff['full_name'])},</p>
      <p>Here is the link to your CoopMS affiliate statement. It works for the next
         {PORTAL_TOKEN_MINUTES} minutes; request another whenever you need one.</p>
      <p style="margin:22px 0">
        <a href="{link}" style="background:#082b66;color:#fff;padding:12px 20px;
           border-radius:6px;text-decoration:none;display:inline-block">View my statement</a></p>
      <p style="color:#475569;font-size:13px">If the button does not work, open this link:<br>{link}</p>
      <p style="color:#475569;font-size:13px">If you did not ask for this, you can ignore it —
         the link only shows your own introductions and earnings.</p>
    """
    try:
        return bool(send_email(aff['email'], 'Your CoopMS affiliate statement', html))
    except Exception as exc:                      # pragma: no cover - network
        current_app.logger.warning('Statement link email failed for %s: %s', aff['email'], exc)
        return False


@affiliates_bp.route('/affiliates/statement', methods=['GET', 'POST'])
@public_route
@csrf.exempt
def request_statement():
    """Ask for a link to your own statement."""
    if request.method == 'GET':
        return render_template('affiliates/statement-request.html',
                               minutes=PORTAL_TOKEN_MINUTES)

    db = get_db()
    ip = (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()
    # The same reply either way, so this page cannot be used to find out who is
    # an affiliate.
    done = render_template('affiliates/statement-request.html',
                           minutes=PORTAL_TOKEN_MINUTES, sent=True)
    if _statement_rate_limited(ip):
        flash('Too many requests from this connection. Please try again shortly.', 'warning')
        return render_template('affiliates/statement-request.html',
                               minutes=PORTAL_TOKEN_MINUTES), 429

    email = (request.form.get('email') or '').strip().lower()
    if not email or '@' not in email:
        flash('Enter the email address on your affiliate appointment.', 'danger')
        return render_template('affiliates/statement-request.html',
                               minutes=PORTAL_TOKEN_MINUTES), 400
    aff = db.execute("SELECT * FROM affiliates WHERE LOWER(email) = ? AND status IN "
                     "('active', 'suspended')", (email,)).fetchone()
    if aff:
        token = issue_portal_token(db, aff['id'], ip)
        db.commit()
        _send_statement_link(db, aff, token)
    return done


@affiliates_bp.route('/affiliates/statement/<token>')
@public_route
def statement(token):
    db = get_db()
    aff, why = _affiliate_for_token(db, token)
    if not aff:
        db.commit()
        return render_template('affiliates/statement-expired.html', why=why), 410 if why == 'expired' else 404
    ctx = statement_context(db, aff)
    db.commit()
    return render_template('affiliates/statement.html', **ctx)


@affiliates_bp.route('/hq/affiliates/<int:aff_id>/statement')
@hq_admin_required
def statement_as_admin(aff_id):
    """The same statement an affiliate sees — for answering their questions
    without asking them to forward their link."""
    db = get_db()
    aff = db.execute('SELECT * FROM affiliates WHERE id = ?', (aff_id,)).fetchone()
    if not aff:
        abort(404)
    return render_template('affiliates/statement.html', admin_view=True,
                           **statement_context(db, aff))
