"""Period statements from member subledgers, including dated reversals."""
from datetime import date
from decimal import Decimal


def money(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.01'))


def statement(db, member_id, start, end):
    start = date.fromisoformat(start)
    end = date.fromisoformat(end)
    if start > end:
        raise ValueError('From date must not be after the to date.')
    events = []

    def add(when, category, reference, description, savings=0, shares=0, loan=0):
        if not when:
            return
        events.append(dict(date=str(when)[:10], category=category,
                           reference=reference or '', description=description or '',
                           savings=money(savings), shares=money(shares), loan=money(loan)))

    for raw in db.execute('SELECT * FROM savings WHERE member_id = ? ORDER BY date, id', (member_id,)).fetchall():
        row = dict(raw)
        add(row['date'], 'Savings', row.get('reference') or row.get('receipt_number'),
            row.get('notes') or row.get('payment_type') or 'Contribution',
            row['amount'], row.get('share_capital'))

    loans = []
    for raw in db.execute('SELECT * FROM loans WHERE member_id = ? ORDER BY date_applied, id', (member_id,)).fetchall():
        row = dict(raw)
        if not row.get('disbursement_date') and row['status'] not in ('active', 'completed'):
            continue
        opened = row.get('disbursement_date') or row['date_applied']
        ref = row['loan_number']
        opening = money(row.get('total_repayment') or row['amount'])
        add(opened, 'Loan opened', ref, row['purpose'], loan=opening)
        loan_events = [(str(opened)[:10], opening)]
        for raw_rep in db.execute('SELECT * FROM repayments WHERE loan_id = ? ORDER BY date, id', (row['id'],)).fetchall():
            rep = dict(raw_rep)
            amount = money(rep['amount'])
            add(rep['date'], 'Repayment', ref, rep.get('reference') or rep.get('notes'), loan=-amount)
            loan_events.append((str(rep['date'])[:10], -amount))
            if rep.get('reversed_at'):
                add(rep['reversed_at'], 'Repayment reversed', ref, rep.get('notes'), loan=amount)
                loan_events.append((str(rep['reversed_at'])[:10], amount))
        for raw_adj in db.execute('SELECT * FROM loan_adjustments WHERE loan_id = ? ORDER BY created_at, id', (row['id'],)).fetchall():
            adj = dict(raw_adj)
            amount = money(adj['amount']) * (1 if adj['direction'] == 'increase' else -1)
            add(adj['created_at'], 'Loan adjustment', ref, adj['reason'], loan=amount)
            loan_events.append((str(adj['created_at'])[:10], amount))
            if adj.get('reversed_at'):
                add(adj['reversed_at'], 'Adjustment reversed', ref, adj['reason'], loan=-amount)
                loan_events.append((str(adj['reversed_at'])[:10], -amount))
        if str(opened)[:10] <= end.isoformat():
            loans.append(dict(number=ref, purpose=row['purpose'], principal=money(row['amount']),
                              agreed_total=opening,
                              period_balance=sum((v for d, v in loan_events if d <= end.isoformat()), Decimal(0)),
                              current_balance=money(row['balance']),
                              reconciliation_difference=money(row['balance']) - sum((v for _, v in loan_events), Decimal(0))))

    events.sort(key=lambda e: e['date'])
    opening = dict(savings=Decimal(0), shares=Decimal(0), loan=Decimal(0))
    balances = opening.copy()
    rows = []
    for event in events:
        if event['date'] > end.isoformat():
            continue
        for key in balances:
            balances[key] += event[key]
        if event['date'] < start.isoformat():
            opening = balances.copy()
        else:
            rows.append(dict(event, savings_balance=balances['savings'],
                             shares_balance=balances['shares'], loan_balance=balances['loan']))
    return dict(rows=rows, opening=opening, closing=balances, loans=loans,
                net_position=balances['savings'] + balances['shares'] - balances['loan'],
                from_date=start.isoformat(), to_date=end.isoformat())
