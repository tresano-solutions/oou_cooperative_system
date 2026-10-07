import random
from datetime import datetime

from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_required, current_user

from database import get_db
from utils import finite_float, role_required, audit
from ledger import post_journal_safe, get_default_cash_account, INVESTMENTS as ACCT_INVESTMENTS

investments = Blueprint('investments', __name__)


@investments.route('/investments')
@login_required
@role_required('admin', 'treasurer')
def investments_list():
    db = get_db()
    all_investments = db.execute('SELECT * FROM investments ORDER BY date DESC').fetchall()
    total_investments = db.execute('SELECT SUM(amount) FROM investments').fetchone()[0] or 0
    return render_template('admin/investments.html',
                           investments=all_investments,
                           total_investments=total_investments)


@investments.route('/investments/add', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'treasurer')
def add_investment():
    if request.method == 'POST':
        db = get_db()
        try:
            investment_number = (
                f"INV/{datetime.now().year}/{datetime.now().month:02d}/{random.randint(1000, 9999)}"
            )

            name = request.form.get('name', '').strip()
            if not name:
                flash('Investment name is required.', 'danger')
                return redirect(url_for('investments.add_investment'))

            amount = finite_float(request.form.get('amount', 0))
            if amount <= 0:
                flash('Amount must be greater than zero.', 'danger')
                return redirect(url_for('investments.add_investment'))

            investment_type = request.form.get('type', '')
            institution = request.form.get('institution', '')
            interest_rate_str = request.form.get('interest_rate')
            interest_rate = float(interest_rate_str) if interest_rate_str else None
            start_date = request.form.get('start_date')
            maturity_date = request.form.get('maturity_date')
            risk_level = request.form.get('risk_level', 'medium')
            description = request.form.get('description', '')

            db.execute('''
                INSERT INTO investments (
                    investment_number, name, amount, type, institution,
                    interest_rate, start_date, maturity_date, risk_level,
                    description, approval_status, created_by, date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                investment_number, name, amount, investment_type, institution,
                interest_rate, start_date, maturity_date, risk_level,
                description, 'approved', current_user.id, datetime.now()
            ))
            # Double-entry: cash converts into an investment asset.
            cash_account = get_default_cash_account(db)
            post_journal_safe(db, f"Investment — {name}", [
                {'account': ACCT_INVESTMENTS, 'debit': amount, 'memo': name},
                {'account': cash_account, 'credit': amount},
            ], reference=investment_number, source_module='investments',
               created_by=current_user.id)
            db.commit()
            audit(db, 'ADD_INVESTMENT', 'investments',
                  f"Added investment {investment_number} – {name} ₦{amount:,.2f}")
            flash(f'Investment "{name}" added successfully! Reference: {investment_number}', 'success')
            return redirect(url_for('investments.investments_list'))

        except Exception as e:
            db.rollback()
            flash(f'Error adding investment: {str(e)}', 'danger')
            return redirect(url_for('investments.add_investment'))

    return render_template('admin/add-investment.html')
