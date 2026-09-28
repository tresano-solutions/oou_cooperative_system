from flask import Blueprint, render_template, redirect, url_for
from flask_login import login_required, current_user
from datetime import datetime

from database import get_db
from utils import member_for_user

main = Blueprint('main', __name__)

# Roles that may see organisation-wide data
_PRIVILEGED_ROLES = {'admin', 'treasurer', 'secretary', 'exco'}


@main.route('/dashboard')
@login_required
def dashboard():
    # Members must never see org-wide data — send them to their own portal
    if current_user.role not in _PRIVILEGED_ROLES:
        db = get_db()
        if not member_for_user(db):
            return redirect(url_for('portal.member_link_required'))
        return redirect(url_for('portal.member_portal'))

    db = get_db()

    members_count      = db.execute('SELECT COUNT(*) FROM members').fetchone()[0] or 0
    total_savings      = db.execute('SELECT SUM(amount) FROM savings').fetchone()[0] or 0
    total_loans        = db.execute("SELECT SUM(balance) FROM loans WHERE status = 'active'").fetchone()[0] or 0
    total_investments  = db.execute('SELECT SUM(amount) FROM investments').fetchone()[0] or 0

    recent_savings = db.execute("""
        SELECT s.*, m.first_name || ' ' || m.last_name as member_name
        FROM savings s JOIN members m ON s.member_id = m.id
        ORDER BY s.date DESC LIMIT 5
    """).fetchall()

    recent_loans = db.execute("""
        SELECT l.*, m.first_name || ' ' || m.last_name as member_name
        FROM loans l JOIN members m ON l.member_id = m.id
        ORDER BY l.date_applied DESC LIMIT 5
    """).fetchall()

    now = datetime.now()
    months = []
    for offset in range(5, -1, -1):
        year, month = divmod(now.year * 12 + now.month - 1 - offset, 12)
        months.append(f'{year:04d}-{month+1:02d}')
    savings_trend, loan_trend = [], []
    for month in months:
        savings_trend.append(float(db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM savings WHERE CAST(date AS TEXT) LIKE ?",
            (month + '%',)).fetchone()[0]))
        loan_trend.append(float(db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM loans WHERE CAST(disbursement_date AS TEXT) LIKE ?",
            (month + '%',)).fetchone()[0]))
    return render_template('dashboard.html',
                           trend_months=months, savings_trend=savings_trend, loan_trend=loan_trend,
                           members_count=members_count,
                           total_savings=total_savings,
                           total_loans=total_loans,
                           total_investments=total_investments,
                           recent_savings=recent_savings,
                           recent_loans=recent_loans)
