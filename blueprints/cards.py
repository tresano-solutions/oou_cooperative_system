import os
import uuid

from flask import Blueprint, render_template, redirect, url_for, request, flash, send_file
from flask_login import login_required, current_user

from database import get_db
from utils import role_required, can_access_member

cards = Blueprint('cards', __name__)


@cards.route('/member/generate-card/<int:member_id>')
@login_required
@role_required('admin', 'secretary')
def generate_member_card(member_id):
    db = get_db()
    member = db.execute('SELECT * FROM members WHERE id = ?', (member_id,)).fetchone()
    if not member:
        flash('Member not found', 'danger')
        return redirect(url_for('members.members_list'))

    token = str(uuid.uuid4())
    db.execute('UPDATE members SET card_token = ? WHERE id = ?', (token, member_id))
    db.commit()
    verification_url = url_for('cards.verify_card', token=token, _external=True)

    from card_generator import MemberCardGenerator
    card_gen = MemberCardGenerator()

    member_dict = dict(member)
    _coop = db.execute("SELECT value FROM settings WHERE key = 'coop_name'").fetchone()
    member_data = {
        'coop_name':     (_coop['value'] if _coop else '') or 'Cooperative',
        'member_number': member_dict.get('member_number', f"MEM/{member_id:04d}"),
        'full_name':     f"{member_dict['first_name']} {member_dict['last_name']}",
        'join_date':     (member_dict.get('date_joined', '')[:10]
                          if member_dict.get('date_joined') else ''),
        'membership_type': 'Full Member',
        'photo_path':    member_dict.get('photo_path'),
        'qr_data':       verification_url,
    }

    card_path = card_gen.generate_member_card(member_data)
    card_filename = os.path.basename(card_path)
    db.execute('UPDATE members SET card_path = ? WHERE id = ?', (card_filename, member_id))
    db.commit()

    flash('Member card generated successfully!', 'success')
    return redirect(url_for('members.member_details', member_id=member_id))


@cards.route('/member/view-card/<int:member_id>')
@login_required
def view_member_card(member_id):
    db = get_db()
    if not can_access_member(db, member_id):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    member = db.execute('SELECT * FROM members WHERE id = ?', (member_id,)).fetchone()
    if not member:
        flash('Member not found', 'danger')
        return redirect(url_for('members.members_list'))

    card_filename = member['card_path']
    full_card_path = os.path.join('static/cards', card_filename) if card_filename else None

    if not card_filename or not os.path.exists(full_card_path):
        if current_user.role not in ('admin', 'secretary'):
            flash('Your member card is not available yet. Please contact the administrator.', 'warning')
            return redirect(url_for('portal.member_portal'))
        return redirect(url_for('cards.generate_member_card', member_id=member_id))

    return render_template('member/view-card.html', member=member)


@cards.route('/member/download-card/<int:member_id>')
@login_required
def download_member_card(member_id):
    db = get_db()
    if not can_access_member(db, member_id):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    member = db.execute('SELECT * FROM members WHERE id = ?', (member_id,)).fetchone()
    if not member or not member['card_path']:
        flash('Card not found', 'danger')
        if current_user.role in ('admin', 'secretary', 'treasurer', 'exco'):
            return redirect(url_for('members.member_details', member_id=member_id))
        return redirect(url_for('portal.member_portal'))

    full_path = os.path.join('static/cards', member['card_path'])
    if not os.path.exists(full_path):
        flash('Card file missing. Please regenerate.', 'danger')
        if current_user.role not in ('admin', 'secretary'):
            return redirect(url_for('portal.member_portal'))
        return redirect(url_for('cards.generate_member_card', member_id=member_id))

    return send_file(full_path, as_attachment=True, download_name=member['card_path'])


@cards.route('/verify-card/<token>')
def verify_card(token):
    db = get_db()
    member = db.execute('SELECT * FROM members WHERE card_token = ?', (token,)).fetchone()
    if not member:
        return render_template('errors/404.html'), 404
    return render_template('public/verify-card.html', member=member)


@cards.route('/test-email')
@login_required
@role_required('admin')
def test_email():
    return redirect(url_for('admin_panel.settings'))
