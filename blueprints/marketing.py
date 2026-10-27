"""
Marketing lead capture and admin lead inbox for CoopMS.
"""

import csv
from csv_safe import safe_writer
import io
import json
import os
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

from flask import Blueprint, Response, abort, current_app, jsonify, redirect, render_template, request, url_for, flash
from flask_login import current_user, login_required
from markupsafe import escape

from database import get_db, last_insert_id
from email_service import send_email_detailed
from extensions import csrf
from utils import audit, client_ip, role_required, throttle


marketing = Blueprint('marketing', __name__)

LEAD_STATUSES = ('new', 'contacted', 'demo_booked', 'demo_completed', 'proposal_sent', 'negotiation', 'won', 'lost')
ACTIVITY_TYPES = ('call', 'email', 'whatsapp', 'demo', 'proposal', 'note')
PROPOSAL_STATUSES = ('not_started', 'drafting', 'sent', 'accepted', 'rejected')


def marketing_hq_enabled() -> bool:
    return os.environ.get('MARKETING_HQ', '0') == '1'


def _require_marketing_hq():
    if not marketing_hq_enabled():
        abort(404)


def _client_ip() -> str:
    return client_ip()


def _rate_limited(ip: str) -> bool:
    return throttle(f'lead:{ip}', max_hits=8, window_seconds=15 * 60)


def _clean(value, limit=500):
    return (str(value or '').strip())[:limit]


def _parse_datetime(value):
    value = (value or '').strip()
    if not value:
        return None
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _parse_date(value):
    value = (value or '').strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date().isoformat()
    except ValueError:
        return None


def _parse_money(value):
    try:
        return round(float(str(value or '0').replace(',', '').strip()), 2)
    except (TypeError, ValueError):
        return 0


def _assignable_users(db):
    return db.execute('''
        SELECT id, username, full_name, email, role
          FROM users
         WHERE is_active = 1
           AND role IN ('admin', 'secretary')
         ORDER BY full_name, username
    ''').fetchall()


def _lead_flags(lead, now=None):
    now = now or datetime.now()
    next_due = _parse_datetime(lead.get('next_follow_up_at'))
    last_activity = _parse_datetime(lead.get('last_activity_at')) or _parse_datetime(lead.get('updated_at')) or _parse_datetime(lead.get('created_at'))
    open_status = lead.get('status') in ('new', 'contacted', 'demo_booked', 'demo_completed', 'proposal_sent', 'negotiation')
    return {
        'follow_up_due': bool(open_status and next_due and next_due <= now),
        'stale': bool(open_status and last_activity and now - last_activity > timedelta(days=3)),
    }


def _hq_base_url() -> str:
    return os.environ.get('MARKETING_HQ_BASE_URL', 'https://hq.cooperativems.com').rstrip('/')


def _public_site_url() -> str:
    return os.environ.get('MARKETING_SITE_URL', 'https://www.cooperativems.com').rstrip('/')


def _lead_detail_url(lead_id) -> str:
    return f"{_hq_base_url()}/marketing/leads/{lead_id}"


def _score_lead(lead):
    score = 20
    reasons = []
    member_count = (lead.get('member_count') or '').lower()
    priority = (lead.get('priority') or '').lower()
    current_system = (lead.get('current_system') or '').lower()
    message = (lead.get('message') or '').lower()
    society_type = (lead.get('society_type') or '').lower()

    if '1,000' in member_count or '1000' in member_count:
        score += 35
        reasons.append('large cooperative size')
    elif '500' in member_count:
        score += 28
        reasons.append('mid-large cooperative size')
    elif '200' in member_count:
        score += 20
        reasons.append('meaningful member base')
    elif '50' in member_count:
        score += 10
        reasons.append('small but qualified society')

    if any(term in priority for term in ('migration', 'reconciliation')):
        score += 25
        reasons.append('migration/reconciliation priority')
    if 'demo' in priority:
        score += 18
        reasons.append('demo requested')
    if any(term in priority for term in ('loan', 'portal', 'audit', 'compliance')):
        score += 15
        reasons.append('specific product pain identified')
    if 'pricing' in priority:
        score += 8
        reasons.append('pricing interest')

    if any(term in current_system for term in ('excel', 'manual', 'spreadsheets')):
        score += 15
        reasons.append('manual/spreadsheet process')
    if 'accounting software plus spreadsheets' in current_system:
        score += 12
        reasons.append('fragmented accounting workflow')

    if lead.get('phone'):
        score += 10
        reasons.append('phone/WhatsApp provided')
    if lead.get('email'):
        score += 5
        reasons.append('email provided')
    if any(term in society_type for term in ('staff', 'multipurpose', 'federation', 'union')):
        score += 8
        reasons.append('strong target segment')
    if len(message) > 40:
        score += 7
        reasons.append('detailed request')

    score = max(0, min(score, 100))
    if score >= 75:
        temperature = 'hot'
    elif score >= 50:
        temperature = 'warm'
    else:
        temperature = 'cold'
    return score, temperature, '; '.join(reasons) or 'basic enquiry'


def _origin_allowed() -> bool:
    origin = request.headers.get('Origin') or request.headers.get('Referer') or ''
    if not origin:
        return True
    origin_host = urlparse(origin).netloc.lower()
    request_host = (request.host or '').lower()
    allowed = {h.strip().lower() for h in os.environ.get('MARKETING_ALLOWED_ORIGINS', '').split(',') if h.strip()}
    allowed.update({request_host, 'cooperativems.com', 'www.cooperativems.com'})
    return origin_host in allowed


def _notification_recipients(db):
    configured = os.environ.get('MARKETING_LEAD_NOTIFY_EMAIL', '').strip()
    if configured:
        return [x.strip() for x in configured.split(',') if x.strip()]
    settings = {
        row['key']: row['value']
        for row in db.execute(
            "SELECT key, value FROM settings WHERE key IN ('subscription_email', 'email')"
        ).fetchall()
    }
    candidates = [settings.get('subscription_email', ''), settings.get('email', '')]
    admins = db.execute(
        "SELECT email FROM users WHERE role = 'admin' AND is_active = 1 AND email IS NOT NULL AND email != ''"
    ).fetchall()
    candidates.extend([row['email'] for row in admins])
    seen = set()
    recipients = []
    for email in candidates:
        email = (email or '').strip()
        if email and email.lower() not in seen:
            recipients.append(email)
            seen.add(email.lower())
    return recipients[:5]


def _send_lead_alert(db, lead):
    recipients = _notification_recipients(db)
    if not recipients:
        return {'ok': False, 'provider': 'none', 'error': 'No internal alert recipients configured'}
    subject = f"{str(lead.get('lead_temperature') or 'new').upper()} lead ({lead.get('lead_score', 0)}/100) - {lead['society_name']}"
    safe = {k: escape(lead.get(k) or '') for k in lead.keys()}
    lead_url = _lead_detail_url(lead['id'])
    html = f"""
    <h2>New CoopMS demo request</h2>
    <p>A new lead has been captured from the public CoopMS website.</p>
    <div style="background:#eef5ff;border:1px solid #cfe0ff;border-radius:8px;padding:14px 16px;margin:18px 0;">
      <div style="font-size:13px;color:#475569;text-transform:uppercase;font-weight:bold;">Lead score</div>
      <div style="font-size:28px;color:#082b66;font-weight:800;">{safe['lead_score']}/100 &middot; {safe['lead_temperature']}</div>
      <div style="color:#475569;">{safe['score_reason']}</div>
    </div>
    <table cellpadding="6" cellspacing="0" style="border-collapse:collapse;">
      <tr><td><strong>Name</strong></td><td>{safe['full_name']}</td></tr>
      <tr><td><strong>Email</strong></td><td>{safe['email']}</td></tr>
      <tr><td><strong>Phone</strong></td><td>{safe['phone']}</td></tr>
      <tr><td><strong>Cooperative</strong></td><td>{safe['society_name']}</td></tr>
      <tr><td><strong>Type</strong></td><td>{safe['society_type']}</td></tr>
      <tr><td><strong>Members</strong></td><td>{safe['member_count']}</td></tr>
      <tr><td><strong>Priority</strong></td><td>{safe['priority']}</td></tr>
      <tr><td><strong>Source</strong></td><td>{safe['utm_source'] or 'direct'}</td></tr>
    </table>
    <p><strong>Message</strong><br>{safe['message'].replace(chr(10), '<br>')}</p>
    <p><strong>Suggested next action:</strong> respond within one business day, confirm the society size and book a live demo.</p>
    <p>
      <a href="{lead_url}" style="background:#f4b51c;color:#082b66;text-decoration:none;padding:12px 22px;border-radius:6px;font-weight:bold;display:inline-block;">Open lead in HQ</a>
    </p>
    """
    results = []
    for recipient in recipients:
        results.append(send_email_detailed(recipient, subject, html))
    ok = any(result.get('ok') for result in results)
    provider = next((r.get('provider') for r in results if r.get('ok')), results[-1].get('provider', 'none'))
    errors = '; '.join(r.get('error', '') for r in results if not r.get('ok') and r.get('error'))
    return {'ok': ok, 'provider': provider, 'error': errors[:1000]}


def _send_prospect_confirmation(lead):
    public_site = _public_site_url()
    html = f"""
    <h2>Thank you for requesting a CoopMS demo</h2>
    <p>Hello {escape(lead['full_name'])},</p>
    <p>Thank you for contacting CoopMS. We have received your request for <strong>{escape(lead['society_name'])}</strong>.</p>
    <p>Our team will review your cooperative's needs and contact you to arrange a short discovery call or live demo.</p>
    <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:14px 16px;margin:18px 0;">
      <p style="margin:0 0 8px;"><strong>Your request summary</strong></p>
      <p style="margin:0;">Priority: {escape(lead.get('priority') or 'Product demo')}</p>
      <p style="margin:0;">Approx. members: {escape(lead.get('member_count') or 'Not provided')}</p>
      <p style="margin:0;">Current records: {escape(lead.get('current_system') or 'Not provided')}</p>
    </div>
    <p>Before the demo, it helps to have a rough idea of your member count, savings process, loan workflow, and current records.</p>
    <p>
      <a href="{public_site}" style="background:#082b66;color:#ffffff;text-decoration:none;padding:12px 22px;border-radius:6px;font-weight:bold;display:inline-block;">Visit CoopMS</a>
    </p>
    <p>Regards,<br><strong>The CoopMS Team</strong></p>
    """
    text = (
        f"Hello {lead['full_name']},\n\n"
        f"Thank you for requesting a CoopMS demo for {lead['society_name']}.\n"
        "Our team will review your request and contact you shortly.\n\n"
        f"Visit: {public_site}\n\nThe CoopMS Team"
    )
    return send_email_detailed(lead['email'], 'We received your CoopMS demo request', html, text)


@marketing.route('/marketing/leads')
@login_required
@role_required('admin', 'secretary')
def leads_inbox():
    _require_marketing_hq()
    db = get_db()
    status = request.args.get('status', '').strip()
    focus = request.args.get('focus', '').strip()
    q = request.args.get('q', '').strip()
    clauses = []
    params = []
    if status in LEAD_STATUSES:
        clauses.append('ml.status = ?')
        params.append(status)
    if q:
        like = f'%{q}%'
        clauses.append('(ml.full_name LIKE ? OR ml.email LIKE ? OR ml.society_name LIKE ? OR ml.phone LIKE ?)')
        params.extend([like, like, like, like])
    if focus == 'hot':
        clauses.append("ml.lead_temperature = 'hot'")
    elif focus == 'due':
        clauses.append("ml.next_follow_up_at IS NOT NULL AND ml.next_follow_up_at <= ?")
        params.append(datetime.now())
        clauses.append("ml.status IN ('new', 'contacted', 'demo_booked', 'demo_completed', 'proposal_sent', 'negotiation')")
    elif focus == 'unassigned':
        clauses.append('ml.assigned_to IS NULL')
    where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
    leads = db.execute(f'''
        SELECT ml.*, u.full_name AS owner_full_name, u.username AS owner_username
        FROM marketing_leads ml
        LEFT JOIN users u ON u.id = ml.assigned_to
        {where}
        ORDER BY ml.created_at DESC
        LIMIT 500
    ''', params).fetchall()
    stats = db.execute('''
        SELECT status, COUNT(*) AS count
        FROM marketing_leads
        GROUP BY status
    ''').fetchall()
    status_counts = {row['status']: row['count'] for row in stats}
    now = datetime.now()
    enriched_leads = []
    for lead in leads:
        item = dict(lead)
        item.update(_lead_flags(item, now))
        enriched_leads.append(item)
    dashboard = {
        'hot': db.execute("SELECT COUNT(*) FROM marketing_leads WHERE lead_temperature = 'hot'").fetchone()[0],
        'due': db.execute(
            "SELECT COUNT(*) FROM marketing_leads WHERE next_follow_up_at IS NOT NULL AND next_follow_up_at <= ? AND status IN ('new', 'contacted', 'demo_booked', 'demo_completed', 'proposal_sent', 'negotiation')",
            (now,),
        ).fetchone()[0],
        'unassigned': db.execute('SELECT COUNT(*) FROM marketing_leads WHERE assigned_to IS NULL').fetchone()[0],
    }
    return render_template(
        'marketing/leads.html',
        leads=enriched_leads,
        statuses=LEAD_STATUSES,
        status_counts=status_counts,
        current_status=status,
        focus=focus,
        dashboard=dashboard,
        q=q,
    )


@marketing.route('/marketing/leads/<int:lead_id>/status', methods=['POST'])
@login_required
@role_required('admin', 'secretary')
def update_lead_status(lead_id):
    _require_marketing_hq()
    db = get_db()
    status = request.form.get('status', '').strip()
    notes = _clean(request.form.get('notes'), 2000)
    if status not in LEAD_STATUSES:
        flash('Invalid lead status.', 'danger')
        return redirect(url_for('marketing.leads_inbox'))
    lead = db.execute('SELECT * FROM marketing_leads WHERE id = ?', (lead_id,)).fetchone()
    if not lead:
        flash('Lead not found.', 'danger')
        return redirect(url_for('marketing.leads_inbox'))
    db.execute('''
        UPDATE marketing_leads
           SET status = ?, notes = ?, last_activity_at = ?, updated_at = ?
         WHERE id = ?
    ''', (status, notes, datetime.now(), datetime.now(), lead_id))
    db.execute('''
        INSERT INTO marketing_lead_events
            (lead_id, event_type, description, actor_user_id, actor_username, data)
        VALUES (?, 'status_update', ?, ?, ?, ?)
    ''', (
        lead_id,
        f"Status changed to {status}",
        current_user.id,
        current_user.username,
        json.dumps({'status': status, 'notes': notes}),
    ))
    audit(db, 'UPDATE_MARKETING_LEAD', 'marketing', f'Updated lead #{lead_id} to {status}')
    db.commit()
    flash('Lead updated.', 'success')
    return redirect(url_for('marketing.lead_detail', lead_id=lead_id))


@marketing.route('/marketing/leads/<int:lead_id>')
@login_required
@role_required('admin', 'secretary')
def lead_detail(lead_id):
    _require_marketing_hq()
    db = get_db()
    lead = db.execute('SELECT * FROM marketing_leads WHERE id = ?', (lead_id,)).fetchone()
    if not lead:
        flash('Lead not found.', 'danger')
        return redirect(url_for('marketing.leads_inbox'))
    events = db.execute(
        'SELECT * FROM marketing_lead_events WHERE lead_id = ? ORDER BY created_at DESC',
        (lead_id,),
    ).fetchall()
    return render_template(
        'marketing/lead_detail.html',
        lead=lead,
        events=events,
        statuses=LEAD_STATUSES,
        activity_types=ACTIVITY_TYPES,
        proposal_statuses=PROPOSAL_STATUSES,
        assignable_users=_assignable_users(db),
        flags=_lead_flags(dict(lead)),
    )


@marketing.route('/marketing/leads/<int:lead_id>/pipeline', methods=['POST'])
@login_required
@role_required('admin', 'secretary')
def update_lead_pipeline(lead_id):
    _require_marketing_hq()
    db = get_db()
    lead = db.execute('SELECT * FROM marketing_leads WHERE id = ?', (lead_id,)).fetchone()
    if not lead:
        flash('Lead not found.', 'danger')
        return redirect(url_for('marketing.leads_inbox'))

    demo_scheduled_at = _parse_datetime(request.form.get('demo_scheduled_at'))
    demo_meeting_link = _clean(request.form.get('demo_meeting_link'), 500)
    demo_presenter = _clean(request.form.get('demo_presenter'), 120)
    demo_outcome = _clean(request.form.get('demo_outcome'), 2000)
    proposed_plan = _clean(request.form.get('proposed_plan'), 120)
    proposal_status = request.form.get('proposal_status', '').strip()
    if proposal_status not in PROPOSAL_STATUSES:
        proposal_status = 'not_started'
    setup_fee = _parse_money(request.form.get('setup_fee'))
    monthly_subscription = _parse_money(request.form.get('monthly_subscription'))
    expected_close_date = _parse_date(request.form.get('expected_close_date'))
    decision_reason = _clean(request.form.get('decision_reason'), 1000)
    now = datetime.now()

    db.execute('''
        UPDATE marketing_leads
           SET demo_scheduled_at = ?,
               demo_meeting_link = ?,
               demo_presenter = ?,
               demo_outcome = ?,
               proposed_plan = ?,
               proposal_status = ?,
               setup_fee = ?,
               monthly_subscription = ?,
               expected_close_date = ?,
               decision_reason = ?,
               updated_at = ?
         WHERE id = ?
    ''', (
        demo_scheduled_at,
        demo_meeting_link,
        demo_presenter,
        demo_outcome,
        proposed_plan,
        proposal_status,
        setup_fee,
        monthly_subscription,
        expected_close_date,
        decision_reason,
        now,
        lead_id,
    ))
    db.execute('''
        INSERT INTO marketing_lead_events
            (lead_id, event_type, description, actor_user_id, actor_username, data)
        VALUES (?, 'pipeline_update', 'Sales pipeline details updated', ?, ?, ?)
    ''', (
        lead_id,
        current_user.id,
        current_user.username,
        json.dumps({
            'demo_scheduled_at': str(demo_scheduled_at or ''),
            'proposal_status': proposal_status,
            'proposed_plan': proposed_plan,
            'expected_close_date': expected_close_date or '',
        }),
    ))
    audit(db, 'UPDATE_MARKETING_PIPELINE', 'marketing', f'Updated sales pipeline for lead #{lead_id}')
    db.commit()
    flash('Sales pipeline updated.', 'success')
    return redirect(url_for('marketing.lead_detail', lead_id=lead_id))


@marketing.route('/marketing/leads/<int:lead_id>/workflow', methods=['POST'])
@login_required
@role_required('admin', 'secretary')
def update_lead_workflow(lead_id):
    _require_marketing_hq()
    db = get_db()
    lead = db.execute('SELECT * FROM marketing_leads WHERE id = ?', (lead_id,)).fetchone()
    if not lead:
        flash('Lead not found.', 'danger')
        return redirect(url_for('marketing.leads_inbox'))
    assigned_to = request.form.get('assigned_to', '').strip()
    next_follow_up = _parse_datetime(request.form.get('next_follow_up_at'))
    assigned_value = int(assigned_to) if assigned_to.isdigit() else None
    db.execute('''
        UPDATE marketing_leads
           SET assigned_to = ?, next_follow_up_at = ?, updated_at = ?
         WHERE id = ?
    ''', (assigned_value, next_follow_up, datetime.now(), lead_id))
    db.execute('''
        INSERT INTO marketing_lead_events
            (lead_id, event_type, description, actor_user_id, actor_username, data)
        VALUES (?, 'workflow_update', 'Owner or follow-up updated', ?, ?, ?)
    ''', (
        lead_id,
        current_user.id,
        current_user.username,
        json.dumps({'assigned_to': assigned_value, 'next_follow_up_at': str(next_follow_up or '')}),
    ))
    audit(db, 'UPDATE_MARKETING_WORKFLOW', 'marketing', f'Updated workflow for lead #{lead_id}')
    db.commit()
    flash('Lead workflow updated.', 'success')
    return redirect(url_for('marketing.lead_detail', lead_id=lead_id))


@marketing.route('/marketing/leads/<int:lead_id>/activity', methods=['POST'])
@login_required
@role_required('admin', 'secretary')
def add_lead_activity(lead_id):
    _require_marketing_hq()
    db = get_db()
    lead = db.execute('SELECT * FROM marketing_leads WHERE id = ?', (lead_id,)).fetchone()
    if not lead:
        flash('Lead not found.', 'danger')
        return redirect(url_for('marketing.leads_inbox'))
    activity_type = request.form.get('activity_type', 'note').strip()
    if activity_type not in ACTIVITY_TYPES:
        activity_type = 'note'
    description = _clean(request.form.get('description'), 2000)
    if not description:
        flash('Activity note is required.', 'danger')
        return redirect(url_for('marketing.lead_detail', lead_id=lead_id))
    next_follow_up = _parse_datetime(request.form.get('next_follow_up_at'))
    now = datetime.now()
    db.execute('''
        INSERT INTO marketing_lead_events
            (lead_id, event_type, description, actor_user_id, actor_username, data)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        lead_id,
        f'activity_{activity_type}',
        description,
        current_user.id,
        current_user.username,
        json.dumps({'next_follow_up_at': str(next_follow_up or '')}),
    ))
    db.execute('''
        UPDATE marketing_leads
           SET last_activity_at = ?, next_follow_up_at = COALESCE(?, next_follow_up_at), updated_at = ?
         WHERE id = ?
    ''', (now, next_follow_up, now, lead_id))
    audit(db, 'ADD_MARKETING_ACTIVITY', 'marketing', f'Added {activity_type} activity to lead #{lead_id}')
    db.commit()
    flash('Activity added.', 'success')
    return redirect(url_for('marketing.lead_detail', lead_id=lead_id))


@marketing.route('/marketing/leads/export.csv')
@login_required
@role_required('admin', 'secretary')
def export_leads():
    _require_marketing_hq()
    db = get_db()
    rows = db.execute('SELECT * FROM marketing_leads ORDER BY created_at DESC').fetchall()
    buf = io.StringIO()
    writer = safe_writer(buf)
    headers = [
        'created_at', 'status', 'lead_score', 'lead_temperature', 'score_reason',
        'full_name', 'email', 'phone', 'society_name',
        'society_type', 'member_count', 'current_system', 'priority', 'message',
        'assigned_to', 'next_follow_up_at', 'last_activity_at',
        'demo_scheduled_at', 'demo_meeting_link', 'demo_presenter', 'demo_outcome',
        'proposed_plan', 'proposal_status', 'setup_fee', 'monthly_subscription',
        'expected_close_date', 'decision_reason',
        'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
        'referrer', 'landing_page', 'consent_accepted', 'confirmation_sent_at',
        'confirmation_status', 'confirmation_provider', 'confirmation_error',
        'internal_alert_sent_at', 'internal_alert_status', 'internal_alert_provider',
        'internal_alert_error', 'crm_sync_status', 'notes'
    ]
    writer.writerow(headers)
    for row in rows:
        writer.writerow([row.get(h, '') for h in headers])
    audit(db, 'EXPORT_MARKETING_LEADS', 'marketing', f'Exported {len(rows)} marketing leads')
    db.commit()
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=coopms_marketing_leads.csv'},
    )


@marketing.route('/api/marketing/leads', methods=['POST'])
@csrf.exempt
def capture_lead():
    _require_marketing_hq()
    if not _origin_allowed():
        return jsonify({'ok': False, 'error': 'Origin not allowed'}), 403
    ip = _client_ip()
    if _rate_limited(ip):
        return jsonify({'ok': False, 'error': 'Too many submissions. Please try again later.'}), 429
    payload = request.get_json(silent=True) or request.form.to_dict()
    if _clean(payload.get('company_website'), 200):
        return jsonify({'ok': True})
    full_name = _clean(payload.get('full_name'), 160)
    email = _clean(payload.get('email'), 200).lower()
    society_name = _clean(payload.get('society_name'), 200)
    consent = str(payload.get('consent_accepted', '')).lower() in {'1', 'true', 'yes', 'on'}
    if not full_name or not email or '@' not in email or not society_name:
        return jsonify({'ok': False, 'error': 'Please provide name, email, and cooperative name.'}), 400
    if not consent:
        return jsonify({'ok': False, 'error': 'Consent is required before we can contact you.'}), 400

    db = get_db()
    now = datetime.now()
    lead_values = {
        'full_name': full_name,
        'email': email,
        'phone': _clean(payload.get('phone'), 80),
        'society_name': society_name,
        'society_type': _clean(payload.get('society_type'), 160),
        'member_count': _clean(payload.get('member_count'), 80),
        'current_system': _clean(payload.get('current_system'), 160),
        'priority': _clean(payload.get('priority'), 160),
        'message': _clean(payload.get('message'), 3000),
        'consent_accepted': 1,
        'utm_source': _clean(payload.get('utm_source'), 200),
        'utm_medium': _clean(payload.get('utm_medium'), 200),
        'utm_campaign': _clean(payload.get('utm_campaign'), 200),
        'utm_term': _clean(payload.get('utm_term'), 200),
        'utm_content': _clean(payload.get('utm_content'), 200),
        'referrer': _clean(payload.get('referrer'), 1000),
        'landing_page': _clean(payload.get('landing_page'), 1000),
        'ip_address': ip,
        'user_agent': _clean(request.headers.get('User-Agent'), 500),
        'created_at': now,
        'updated_at': now,
    }
    lead_score, lead_temperature, score_reason = _score_lead(lead_values)
    lead_values['lead_score'] = lead_score
    lead_values['lead_temperature'] = lead_temperature
    lead_values['score_reason'] = score_reason
    db.execute('''
        INSERT INTO marketing_leads (
            full_name, email, phone, society_name, society_type, member_count,
            current_system, priority, message, consent_accepted, utm_source,
            utm_medium, utm_campaign, utm_term, utm_content, referrer,
            landing_page, ip_address, user_agent, lead_score, lead_temperature,
            score_reason, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', tuple(lead_values[k] for k in [
        'full_name', 'email', 'phone', 'society_name', 'society_type', 'member_count',
        'current_system', 'priority', 'message', 'consent_accepted', 'utm_source',
        'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'referrer',
        'landing_page', 'ip_address', 'user_agent', 'lead_score', 'lead_temperature',
        'score_reason', 'created_at', 'updated_at'
    ]))
    lead_id = last_insert_id(db)
    # Referral attribution: either the code typed on the form, or a ?ref= the
    # landing page carried through. Recorded even when it resolves to nobody so
    # a mistyped code can be corrected rather than lost.
    from blueprints.affiliates import attach_code_to_lead
    referral_code = _clean(payload.get('affiliate_code') or payload.get('ref'), 40)
    if referral_code:
        attach_code_to_lead(db, lead_id, referral_code)
    db.execute('''
        INSERT INTO marketing_lead_events
            (lead_id, event_type, description, data)
        VALUES (?, 'created', 'Lead captured from public website', ?)
    ''', (lead_id, json.dumps({k: lead_values.get(k, '') for k in ('utm_source', 'utm_campaign', 'landing_page')})))
    lead = dict(lead_values)
    lead['id'] = lead_id
    db.execute('''
        INSERT INTO marketing_lead_events
            (lead_id, event_type, description, data)
        VALUES (?, 'scored', ?, ?)
    ''', (
        lead_id,
        f"Lead scored {lead_score}/100 ({lead_temperature})",
        json.dumps({'score': lead_score, 'temperature': lead_temperature, 'reason': score_reason}),
    ))
    db.commit()

    try:
        confirmation = _send_prospect_confirmation(lead)
        confirmation_status = 'sent' if confirmation.get('ok') else 'failed'
        db.execute('''
            UPDATE marketing_leads
               SET confirmation_sent_at = ?,
                   confirmation_status = ?,
                   confirmation_provider = ?,
                   confirmation_error = ?,
                   updated_at = ?
             WHERE id = ?
        ''', (
            datetime.now() if confirmation.get('ok') else None,
            confirmation_status,
            confirmation.get('provider', ''),
            confirmation.get('error', ''),
            datetime.now(),
            lead_id,
        ))
        db.execute('''
            INSERT INTO marketing_lead_events
                (lead_id, event_type, description, data)
            VALUES (?, ?, ?, ?)
        ''', (
            lead_id,
            'email_sent' if confirmation.get('ok') else 'email_failed',
            f"Prospect confirmation email {confirmation_status}",
            json.dumps(confirmation),
        ))
    except Exception as exc:
        current_app.logger.warning('Marketing lead confirmation failed: %s', exc)
        db.execute('''
            UPDATE marketing_leads
               SET confirmation_status = 'failed', confirmation_error = ?, updated_at = ?
             WHERE id = ?
        ''', (str(exc)[:1000], datetime.now(), lead_id))
    try:
        alert = _send_lead_alert(db, lead)
        alert_status = 'sent' if alert.get('ok') else 'failed'
        db.execute('''
            UPDATE marketing_leads
               SET internal_alert_sent_at = ?,
                   internal_alert_status = ?,
                   internal_alert_provider = ?,
                   internal_alert_error = ?,
                   updated_at = ?
             WHERE id = ?
        ''', (
            datetime.now() if alert.get('ok') else None,
            alert_status,
            alert.get('provider', ''),
            alert.get('error', ''),
            datetime.now(),
            lead_id,
        ))
        db.execute('''
            INSERT INTO marketing_lead_events
                (lead_id, event_type, description, data)
            VALUES (?, ?, ?, ?)
        ''', (
            lead_id,
            'email_sent' if alert.get('ok') else 'email_failed',
            f"Internal sales alert {alert_status}",
            json.dumps(alert),
        ))
    except Exception as exc:
        current_app.logger.warning('Marketing lead alert failed: %s', exc)
        db.execute('''
            UPDATE marketing_leads
               SET internal_alert_status = 'failed', internal_alert_error = ?, updated_at = ?
             WHERE id = ?
        ''', (str(exc)[:1000], datetime.now(), lead_id))
    db.commit()
    return jsonify({'ok': True, 'lead_id': lead_id})
