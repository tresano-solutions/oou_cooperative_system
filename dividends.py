"""
dividends.py — cooperative dividend & patronage engine.

At period end the net surplus (from the general ledger) is appropriated per
bye-law percentages, then the dividend pool is distributed to members:
  * a dividend-on-savings portion, proportional to each member's savings, and
  * an optional patronage rebate, proportional to the loan interest each member
    paid in the period (rewards members who used the co-op's credit).

Declaring a dividend credits each member's savings and posts one balanced
journal entry (Dr Accumulated Surplus / Cr Member Deposits + Cr Statutory
Reserve), so the books stay consistent.
"""

from datetime import datetime

from ledger import (post_journal_required, ACCUM_SURPLUS, STATUTORY_RESERVE,
                    MEMBER_DEPOSITS)


def compute_dividend_schedule(db, from_date, to_date, dividend_pct=50,
                              reserve_pct=25, honorarium_pct=10, other_pct=15,
                              patronage_split=0):
    """Compute the surplus appropriation and the per-member dividend schedule.
    Read-only — nothing is written."""
    from reports_engine import income_statement
    inc = income_statement(db, from_date, to_date)
    surplus = inc['net_surplus']
    base = surplus if surplus > 0 else 0.0

    reserve       = round(base * reserve_pct / 100, 2)
    honorarium    = round(base * honorarium_pct / 100, 2)
    other         = round(base * other_pct / 100, 2)
    dividend_pool = round(base * dividend_pct / 100, 2)
    savings_pool  = round(dividend_pool * (100 - patronage_split) / 100, 2)
    patronage_pool = round(dividend_pool - savings_pool, 2)

    hi = f"{to_date} 23:59:59"
    # Each member's savings base (closing balance, excluding prior dividend credits)
    sav = {r['member_id']: float(r['bal'] or 0) for r in db.execute(
        "SELECT member_id, SUM(amount) AS bal FROM savings "
        "WHERE date <= ? AND COALESCE(payment_type, '') != 'dividend' "
        "GROUP BY member_id", (hi,)).fetchall()}
    # Each member's patronage base (loan interest paid in the period)
    pat = {r['member_id']: float(r['ip'] or 0) for r in db.execute(
        "SELECT l.member_id, SUM(r.interest_paid) AS ip FROM repayments r "
        "JOIN loans l ON l.id = r.loan_id WHERE r.date BETWEEN ? AND ? "
        "GROUP BY l.member_id", (from_date, hi)).fetchall()}

    members = db.execute(
        "SELECT id, member_number, first_name, last_name FROM members "
        "WHERE status = 'active' ORDER BY member_number"
    ).fetchall()
    total_savings   = sum(sav.get(m['id'], 0) for m in members)
    total_patronage = sum(pat.get(m['id'], 0) for m in members)

    allocations = []
    for m in members:
        sb = sav.get(m['id'], 0)
        pb = pat.get(m['id'], 0)
        ds = round(savings_pool * sb / total_savings, 2) if total_savings > 0 else 0.0
        dp = round(patronage_pool * pb / total_patronage, 2) if total_patronage > 0 else 0.0
        total = round(ds + dp, 2)
        if sb == 0 and pb == 0 and total == 0:
            continue
        allocations.append({
            'member_id': m['id'], 'member_number': m['member_number'],
            'name': f"{m['first_name']} {m['last_name']}",
            'savings_base': sb, 'patronage_base': pb,
            'dividend_savings': ds, 'dividend_patronage': dp, 'total': total,
        })

    return {
        'from_date': from_date, 'to_date': to_date,
        'net_surplus': surplus,
        'reserve': reserve, 'honorarium': honorarium, 'other': other,
        'dividend_pool': dividend_pool,
        'savings_pool': savings_pool, 'patronage_pool': patronage_pool,
        'patronage_split': patronage_split,
        'total_savings': total_savings, 'total_patronage': total_patronage,
        'allocations': allocations,
        'allocated': round(sum(a['total'] for a in allocations), 2),
        'distributable': surplus > 0,
        'rates': {'dividend_pct': dividend_pct, 'reserve_pct': reserve_pct,
                  'honorarium_pct': honorarium_pct, 'other_pct': other_pct},
    }


def declare_dividends(db, from_date, to_date, dividend_pct=50, reserve_pct=25,
                      honorarium_pct=10, other_pct=15, patronage_split=0,
                      declared_by=None):
    """Record a dividend declaration: store it, credit each member's savings, and
    post the appropriation to the ledger. Does NOT commit. Returns declaration id.
    Raises ValueError if there is no surplus to distribute."""
    from database import last_insert_id
    sched = compute_dividend_schedule(db, from_date, to_date, dividend_pct,
                                      reserve_pct, honorarium_pct, other_pct,
                                      patronage_split)
    if not sched['distributable']:
        raise ValueError('There is no net surplus to distribute for this period.')

    db.execute('''
        INSERT INTO dividend_declarations
            (period_from, period_to, net_surplus, reserve_amount, honorarium_amount,
             other_amount, dividend_pool, patronage_split, status, declared_by, declared_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'declared', ?, ?)
    ''', (from_date, to_date, sched['net_surplus'], sched['reserve'],
          sched['honorarium'], sched['other'], sched['dividend_pool'],
          patronage_split, declared_by, datetime.now()))
    decl_id = last_insert_id(db)

    year = str(to_date)[:4]
    allocated = sched['allocated']
    for a in sched['allocations']:
        db.execute('''
            INSERT INTO dividend_allocations
                (declaration_id, member_id, savings_base, patronage_base,
                 dividend_savings, dividend_patronage, total)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (decl_id, a['member_id'], a['savings_base'], a['patronage_base'],
              a['dividend_savings'], a['dividend_patronage'], a['total']))
        if a['total'] > 0:
            # Credit the dividend to the member's savings. payment_type='dividend'
            # so the ledger backfill skips it (the aggregate entry below covers it).
            db.execute('''
                INSERT INTO savings
                    (member_id, amount, month, payment_type, payment_method,
                     receipt_number, notes, date)
                VALUES (?, ?, ?, 'dividend', 'dividend', ?, ?, ?)
            ''', (a['member_id'], a['total'], f"DIV-{year}",
                  f"DIV/{year}/{a['member_id']}",
                  f"Dividend for {from_date} to {to_date}", datetime.now()))
            db.execute('UPDATE members SET total_savings = total_savings + ? WHERE id = ?',
                       (a['total'], a['member_id']))

    lines = []
    if allocated > 0:
        lines.append({'account': ACCUM_SURPLUS, 'debit': allocated, 'memo': 'Dividend to members'})
        lines.append({'account': MEMBER_DEPOSITS, 'credit': allocated, 'memo': 'Dividend credited to savings'})
    if sched['reserve'] > 0:
        lines.append({'account': ACCUM_SURPLUS, 'debit': sched['reserve'], 'memo': 'Transfer to statutory reserve'})
        lines.append({'account': STATUTORY_RESERVE, 'credit': sched['reserve'], 'memo': 'Statutory reserve'})

    je_id = None
    if lines:
        je_id = post_journal_required(db, f"Dividend declaration {from_date} to {to_date}",
                                  lines, reference=f"DIV-{decl_id}",
                                  source_module='dividend', source_id=decl_id,
                                  created_by=declared_by)
    db.execute('UPDATE dividend_declarations SET journal_entry_id = ? WHERE id = ?',
               (je_id, decl_id))
    return decl_id
