"""Optional task feedback, isolated from financial posting and the NPS survey."""
import time
import uuid

from flask import current_app, g, request, session
from flask_login import current_user
from itsdangerous import URLSafeTimedSerializer


WRITE_TASKS = {
    'portal.apply_loan_member': 'Loan application',
    'portal.withdraw_loan_application': 'Withdraw loan application',
    'portal.change_savings_request': 'Savings deduction request',
    'portal.edit_profile': 'Update profile',
    'portal.respond_guarantor': 'Guarantor review',
    'loans.apply_loan': 'Record loan application',
    'loans.loan_act': 'Loan approval review',
    'loans.cancel_loan_application': 'Cancel loan application',
    'loans.bulk_loan_repayments': 'Upload loan repayments',
    'savings.add_saving': 'Record savings',
    'savings.salary_upload': 'Upload salary savings',
    'member_receipts.member_payment': 'Record member receipt',
    'member_receipts.reverse_member_payment': 'Reverse member receipt',
    'members.edit_member': 'Update member',
    'accounting.new_journal': 'Post journal',
    'accounting.set_lock_date': 'Set accounting lock date',
}
READ_TASKS = {
    'portal.member_portal': 'Check balances',
    'portal.my_savings': 'View savings',
    'portal.my_loans': 'View loans',
    'portal.statements': 'View statement',
    'members.financial_statement': 'Review member financial statement',
    'accounting.trial_balance_view': 'Review trial balance',
    'accounting.bank_accounts': 'Review bank position',
}


def signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt='task-feedback-v1')


def install_task_feedback(app):
    @app.before_request
    def remember_flash_count():
        g.task_flash_count = len(session.get('_flashes', []))

    @app.after_request
    def capture_completed_task(response):
        # Only a confirmed, successful POST redirect qualifies as a completed write.
        task = WRITE_TASKS.get(request.endpoint)
        new_flashes = session.get('_flashes', [])[getattr(g, 'task_flash_count', 0):]
        if (task and current_user.is_authenticated and request.method == 'POST'
                and response.status_code in (302, 303)
                and any(category == 'success' for category, _ in new_flashes)
                and not any(category in ('danger', 'error', 'warning') for category, _ in new_flashes)):
            session['task_feedback_pending'] = {
                'task': task, 'user_id': str(current_user.id), 'at': time.time()}
        return response

    @app.context_processor
    def task_prompt():
        if not current_user.is_authenticated or request.method != 'GET':
            return {'task_feedback_prompt': None}
        pending = session.pop('task_feedback_pending', None)
        task = None
        if (pending and pending.get('user_id') == str(current_user.id)
                and time.time() - pending.get('at', 0) < 600):
            task = pending['task']
        task = task or READ_TASKS.get(request.endpoint)
        if not task:
            return {'task_feedback_prompt': None}
        # Refreshes and repeated balance checks must not repeatedly solicit ratings.
        seen = session.get('task_feedback_seen', {})
        key = str(current_user.id) + ':' + task
        if time.time() - seen.get(key, 0) < 600:
            return {'task_feedback_prompt': None}
        seen = {k: v for k, v in seen.items() if time.time() - v < 600}
        seen[key] = time.time()
        session['task_feedback_seen'] = seen
        token = signer().dumps({'user_id': str(current_user.id), 'task': task,
                                'nonce': uuid.uuid4().hex})
        return {'task_feedback_prompt': {'task': task, 'token': token}}
