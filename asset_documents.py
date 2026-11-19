"""Invoice and delivery-order documents for accepted asset-scheme quotes."""

import io
from datetime import datetime

from database import last_insert_id


def ensure_asset_documents(db, order_id):
    order = db.execute('SELECT * FROM asset_orders WHERE id=?', (order_id,)).fetchone()
    if not order:
        raise ValueError('Asset request not found.')
    invoice = db.execute('SELECT * FROM asset_invoices WHERE order_id=?', (order_id,)).fetchone()
    now = datetime.now()
    if not invoice:
        number = f'INV/AS/{now:%Y%m%d}/{order_id:05d}'
        db.execute('''INSERT INTO asset_invoices
            (invoice_number,order_id,member_id,campaign_id,issue_date,selling_subtotal,
             finance_charge,total_repayment,delivery_fee,status)
            VALUES (?,?,?,?,?,?,?,?,?,'accepted_pending_delivery')''',
            (number, order_id, order['member_id'], order['campaign_id'], now,
             order['selling_subtotal'], order['interest_amount'], order['total_repayment'],
             order['delivery_fee']))
        invoice = db.execute('SELECT * FROM asset_invoices WHERE id=?',
                             (last_insert_id(db),)).fetchone()
    delivery = db.execute('SELECT * FROM asset_delivery_orders WHERE order_id=?', (order_id,)).fetchone()
    if not delivery:
        number = f'DO/AS/{now:%Y%m%d}/{order_id:05d}'
        db.execute('''INSERT INTO asset_delivery_orders
            (delivery_number,order_id,invoice_id,status,issued_at)
            VALUES (?,?,?,'pending_approval',?)''',
            (number, order_id, invoice['id'], now))
        delivery = db.execute('SELECT * FROM asset_delivery_orders WHERE id=?',
                              (last_insert_id(db),)).fetchone()
    return invoice, delivery


def update_asset_documents(db, order_id, *, invoice_status=None, delivery_status=None,
                           ready=False, confirmed=False, confirmed_by=None):
    if invoice_status:
        db.execute('UPDATE asset_invoices SET status=? WHERE order_id=?',
                   (invoice_status, order_id))
    if delivery_status:
        fields = ['status=?']; values = [delivery_status]
        if ready:
            fields.append('ready_at=?'); values.append(datetime.now())
        if confirmed:
            fields.extend(['confirmed_at=?', 'confirmed_by=?'])
            values.extend([datetime.now(), confirmed_by])
        values.append(order_id)
        db.execute(f'UPDATE asset_delivery_orders SET {", ".join(fields)} WHERE order_id=?', values)


def document_rows(db, order_id):
    invoice = db.execute('SELECT * FROM asset_invoices WHERE order_id=?', (order_id,)).fetchone()
    delivery = db.execute('SELECT * FROM asset_delivery_orders WHERE order_id=?', (order_id,)).fetchone()
    return invoice, delivery


def render_asset_pdf(db, order_id, document_type):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    order = db.execute('''SELECT o.*,c.name campaign_name,m.first_name,m.last_name,m.member_number,
        m.email,m.phone FROM asset_orders o JOIN asset_campaigns c ON c.id=o.campaign_id
        JOIN members m ON m.id=o.member_id WHERE o.id=?''', (order_id,)).fetchone()
    invoice, delivery = document_rows(db, order_id)
    document = invoice if document_type == 'invoice' else delivery
    if not order or not document:
        raise ValueError('Document not found.')
    items = db.execute('SELECT * FROM asset_order_items WHERE order_id=? ORDER BY id', (order_id,)).fetchall()
    number = invoice['invoice_number'] if document_type == 'invoice' else delivery['delivery_number']
    title = 'ASSET SCHEME INVOICE' if document_type == 'invoice' else 'DELIVERY ORDER'
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=16*mm, bottomMargin=16*mm,
                            leftMargin=16*mm, rightMargin=16*mm, title=number)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='DocTitle', parent=styles['Title'], textColor=colors.HexColor('#0B3475')))
    story = [Paragraph('COOPMS', styles['Heading2']), Paragraph(title, styles['DocTitle']),
             Paragraph(number, styles['Heading3']), Spacer(1, 8)]
    member_name = f'{order["first_name"]} {order["last_name"]}'
    story.append(Table([
        ['Member', member_name], ['Member number', order['member_number'] or '-'],
        ['Campaign', order['campaign_name']], ['Request', order['order_number']],
        ['Status', document['status'].replace('_', ' ').title()],
    ], colWidths=[42*mm, 125*mm], style=TableStyle([
        ('GRID', (0,0), (-1,-1), .5, colors.HexColor('#D9E2EF')),
        ('BACKGROUND', (0,0), (0,-1), colors.HexColor('#EEF4FF')),
        ('FONTNAME', (0,0), (0,-1), 'Helvetica-Bold'), ('PADDING', (0,0), (-1,-1), 6),
    ])))
    story.extend([Spacer(1, 12), Paragraph('Items', styles['Heading3'])])
    item_data = [['Description', 'Brand / model', 'Qty']]
    for item in items:
        item_data.append([item['product_name'], f'{item["brand"] or ""} {item["model"] or ""}'.strip() or '-', str(item['quantity'])])
    story.append(Table(item_data, colWidths=[85*mm, 62*mm, 20*mm], repeatRows=1,
                       style=TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#0B3475')),
                                         ('TEXTCOLOR',(0,0),(-1,0),colors.white),
                                         ('GRID',(0,0),(-1,-1),.5,colors.HexColor('#D9E2EF')),
                                         ('PADDING',(0,0),(-1,-1),6)])))
    story.append(Spacer(1, 12))
    if document_type == 'invoice':
        totals = [['Cooperative selling price', f'NGN {float(invoice["selling_subtotal"] or 0):,.2f}'],
                  ['Finance charge', f'NGN {float(invoice["finance_charge"] or 0):,.2f}'],
                  ['Total financed repayment', f'NGN {float(invoice["total_repayment"] or 0):,.2f}'],
                  ['Delivery fee (separate)', f'NGN {float(invoice["delivery_fee"] or 0):,.2f}']]
        story.append(Table(totals, colWidths=[105*mm, 62*mm], style=TableStyle([
            ('ALIGN',(1,0),(1,-1),'RIGHT'), ('LINEABOVE',(0,2),(-1,2),1,colors.HexColor('#0B3475')),
            ('FONTNAME',(0,2),(-1,2),'Helvetica-Bold'), ('PADDING',(0,0),(-1,-1),6)])))
        story.extend([Spacer(1, 10), Paragraph(
            '<b>Important:</b> This accepted invoice records the agreed commercial terms. '
            'The asset loan and member receivable begin only when delivery is confirmed.', styles['BodyText'])])
    else:
        story.append(Paragraph(
            '<b>Member confirmation:</b> I confirm that I received the items listed above in satisfactory condition. '
            'My confirmation date becomes the commencement date of the asset loan and agreed repayment schedule.',
            styles['BodyText']))
        story.extend([Spacer(1, 28), Table([['Member signature', 'Date'], ['____________________________', '________________']],
                                               colWidths=[110*mm, 57*mm])])
    doc.build(story)
    buf.seek(0)
    return buf, number

