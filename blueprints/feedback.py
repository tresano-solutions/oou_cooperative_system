"""
In-app feedback / NPS survey.

Shown as a dismissible sidebar nudge to every active user, at most once every
~3 months. Answers are closed-ended (experience, most-loved feature, area to
improve, likelihood to recommend). The survey also invites users to join the
referral / sales-partner programme (opt-in): if they say yes we capture a name
and email, which an admin can export as CSV to feed the external commission
platform.
"""
import csv
from csv_safe import safe_writer
import io
from datetime import datetime, timedelta

from flask import (Blueprint, Response, flash, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required

from database import get_db
from utils import audit, role_required

feedback_bp = Blueprint('feedback', __name__, url_prefix='/feedback')

# How often a given user may be prompted (days).
CADENCE_DAYS = 90

# Closed-ended options — generic product features (white-label safe).
FEATURE_OPTIONS = [
    'Member management',
    'Savings & contributions',
    'Loans',
    'Member portal (self-service)',
    'Reports & accounting',
    'Communications & notices',
]
IMPROVE_OPTIONS = FEATURE_OPTIONS + ['Speed / performance', 'Nothing — it works well']


def _as_dt(value):
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def feedback_due(db, user_id) -> bool:
    """True when this user is due to be asked for feedback again (no submission
    and no dismissal within the last CADENCE_DAYS)."""
    if not user_id:
        return False
    try:
        urow = db.execute(
            'SELECT feedback_dismissed_at FROM users WHERE id = ?', (user_id,)
        ).fetchone()
        dismissed = _as_dt(urow['feedback_dismissed_at']) if urow and urow['feedback_dismissed_at'] else None

        srow = db.execute(
            'SELECT MAX(created_at) AS m FROM feedback_responses WHERE user_id = ?',
            (user_id,),
        ).fetchone()
        submitted = _as_dt(srow['m']) if srow and srow['m'] else None

        marks = [d for d in (dismissed, submitted) if d]
        if not marks:
            return True
        return (datetime.now() - max(marks)) > timedelta(days=CADENCE_DAYS)
    except Exception:
        return False


def _clamp_int(value, low, high):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return None


@feedback_bp.route('/', methods=['GET'])
@login_required
def form():
    """Standalone survey page (the sidebar card usually opens it as a modal;
    this is the accessible fallback / direct link)."""
    return render_template('feedback/form.html',
                           features=FEATURE_OPTIONS,
                           improve_options=IMPROVE_OPTIONS,
                           default_email=getattr(current_user, 'email', '') or '')


@feedback_bp.route('/', methods=['POST'])
@login_required
def submit():
    db = get_db()
    experience = _clamp_int(request.form.get('overall_experience'), 1, 5)
    recommend = _clamp_int(request.form.get('recommend_score'), 0, 10)
    most_loved = request.form.get('most_loved_feature', '').strip()
    improve = request.form.get('improve_feature', '').strip()
    comments = request.form.get('comments', '').strip()[:1000]

    if most_loved and most_loved not in FEATURE_OPTIONS:
        most_loved = ''
    if improve and improve not in IMPROVE_OPTIONS:
        improve = ''

    optin = 1 if request.form.get('referral_optin') in ('1', 'on', 'yes') else 0
    ref_name = request.form.get('referral_name', '').strip()[:120] if optin else ''
    ref_email = request.form.get('referral_email', '').strip()[:200] if optin else ''

    if experience is None and recommend is None and not most_loved:
        flash('Please answer at least one question before submitting.', 'warning')
        return redirect(request.referrer or url_for('main.dashboard'))

    try:
        db.execute('''
            INSERT INTO feedback_responses
                (user_id, username, role, overall_experience, most_loved_feature,
                 improve_feature, recommend_score, comments,
                 referral_optin, referral_name, referral_email, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            current_user.id, current_user.username, getattr(current_user, 'role', ''),
            experience, most_loved, improve, recommend, comments,
            optin, ref_name, ref_email, datetime.now(),
        ))
        db.commit()
        audit(db, 'SUBMIT_FEEDBACK', 'feedback',
              f'Feedback submitted (experience={experience}, recommend={recommend}, referral={"yes" if optin else "no"})')
    except Exception as e:
        db.rollback()
        flash(f'Sorry, we could not save your feedback: {e}', 'danger')
        return redirect(request.referrer or url_for('main.dashboard'))

    if optin:
        flash('Thank you! Your feedback is in, and we\'ll be in touch about the '
              'referral programme.', 'success')
    else:
        flash('Thank you for your feedback!', 'success')
    return redirect(url_for('main.dashboard'))


@feedback_bp.route('/dismiss', methods=['POST'])
@login_required
def dismiss():
    """'Not now' — snooze the nudge for this user for CADENCE_DAYS."""
    db = get_db()
    try:
        db.execute('UPDATE users SET feedback_dismissed_at = ? WHERE id = ?',
                   (datetime.now(), current_user.id))
        db.commit()
    except Exception:
        db.rollback()
    return redirect(request.referrer or url_for('main.dashboard'))


@feedback_bp.route('/admin')
@login_required
@role_required('admin')
def admin():
    db = get_db()
    responses = db.execute(
        'SELECT * FROM feedback_responses ORDER BY created_at DESC LIMIT 500'
    ).fetchall()

    stats = db.execute('''
        SELECT COUNT(*) AS total,
               AVG(overall_experience) AS avg_experience,
               AVG(recommend_score)    AS avg_recommend,
               SUM(CASE WHEN referral_optin = 1 THEN 1 ELSE 0 END) AS referrals
        FROM feedback_responses
    ''').fetchone()

    # Net Promoter Score = %promoters (9-10) - %detractors (0-6).
    nps = None
    scored = db.execute(
        'SELECT recommend_score FROM feedback_responses WHERE recommend_score IS NOT NULL'
    ).fetchall()
    if scored:
        promoters = sum(1 for r in scored if r['recommend_score'] >= 9)
        detractors = sum(1 for r in scored if r['recommend_score'] <= 6)
        nps = round((promoters - detractors) * 100.0 / len(scored))

    loved = db.execute('''
        SELECT most_loved_feature AS feature, COUNT(*) AS c
        FROM feedback_responses
        WHERE most_loved_feature IS NOT NULL AND most_loved_feature != ''
        GROUP BY most_loved_feature ORDER BY c DESC
    ''').fetchall()
    improve = db.execute('''
        SELECT improve_feature AS feature, COUNT(*) AS c
        FROM feedback_responses
        WHERE improve_feature IS NOT NULL AND improve_feature != ''
        GROUP BY improve_feature ORDER BY c DESC
    ''').fetchall()

    return render_template('feedback/admin.html',
                           responses=responses, stats=stats, nps=nps,
                           loved=loved, improve=improve)


@feedback_bp.route('/admin/referrals.csv')
@login_required
@role_required('admin')
def export_referrals():
    """Export referral opt-ins for the external commission platform."""
    db = get_db()
    rows = db.execute('''
        SELECT created_at, username, role, referral_name, referral_email,
               recommend_score
        FROM feedback_responses
        WHERE referral_optin = 1
        ORDER BY created_at DESC
    ''').fetchall()

    buf = io.StringIO()
    writer = safe_writer(buf)
    writer.writerow(['submitted_at', 'app_username', 'role',
                     'referral_name', 'referral_email', 'recommend_score'])
    for r in rows:
        writer.writerow([r['created_at'], r['username'], r['role'],
                         r['referral_name'], r['referral_email'], r['recommend_score']])

    audit(db, 'EXPORT_REFERRALS', 'feedback', f'Exported {len(rows)} referral opt-ins')
    db.commit()
    return Response(
        buf.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=referral_optins.csv'},
    )
