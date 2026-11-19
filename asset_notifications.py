"""Transactional notifications for the asset-scheme workflow."""

from html import escape

from email_service import send_email
from utils import notify, notify_member


def _order_path(order):
    return f'/asset-schemes/orders/{order["id"]}'


def _email_html(name, title, message, order, portal_url=''):
    link = f'{portal_url.rstrip("/")}{_order_path(order)}' if portal_url else ''
    button = (f'<p><a href="{escape(link)}" style="display:inline-block;padding:12px 18px;'
              'background:#0b3475;color:#fff;text-decoration:none;border-radius:6px">'
              'View asset request</a></p>') if link else ''
    return (
        f'<p>Dear {escape(name or "Member")},</p>'
        f'<h2>{escape(title)}</h2><p>{escape(message)}</p>'
        f'<p><strong>Request:</strong> {escape(order["order_number"])}</p>{button}'
        '<p>Thank you,<br>Cooperative Administration</p>'
    )


def notify_asset_member(db, order, title, message, notification_type='info', portal_url=''):
    """Notify the member by in-app alert, push and email."""
    email = (order['email'] or '').strip()
    if not email:
        return
    notify_member(db, email, title, message, notification_type, _order_path(order))
    name = f'{order["first_name"] or ""} {order["last_name"] or ""}'.strip() or 'Member'
    send_email(email, f'{title} - {order["order_number"]}',
               _email_html(name, title, message, order, portal_url), background=True)


def notify_asset_roles(db, order, roles, title, message, notification_type='info', portal_url=''):
    """Notify active officers in the roles responsible for the next action."""
    roles = tuple(dict.fromkeys(role for role in roles if role))
    if not roles:
        return
    placeholders = ','.join('?' for _ in roles)
    users = db.execute(f'''SELECT id,username,email,full_name FROM users
        WHERE role IN ({placeholders}) AND COALESCE(is_active,1)=1''', roles).fetchall()
    for user in users:
        notify(db, user['id'], title, message, notification_type, _order_path(order))
        email = (user['email'] or user['username'] or '').strip()
        if '@' in email:
            send_email(email, f'{title} - {order["order_number"]}',
                       _email_html(user['full_name'] or 'Officer', title, message, order, portal_url),
                       background=True)

