"""Invoice and delivery-order documents for accepted asset-scheme quotes."""

import base64
import io
from datetime import datetime
from xml.sax.saxutils import escape

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


def _brand_settings(db):
    rows = db.execute("""SELECT key,value FROM settings WHERE key IN
        ('coop_name','coop_short_name','coop_logo','reg_number','address','phone','email')""").fetchall()
    values = {row['key']: (row['value'] or '').strip() for row in rows}
    return {
        'name': values.get('coop_name') or values.get('coop_short_name') or 'Cooperative',
        'logo': values.get('coop_logo', ''),
        'registration': values.get('reg_number', ''),
        'address': values.get('address', ''),
        'phone': values.get('phone', ''),
        'email': values.get('email', ''),
    }


def _logo_flowable(logo_uri, mm):
    if not logo_uri or not logo_uri.startswith('data:') or ',' not in logo_uri:
        return None
    try:
        from reportlab.platypus import Image as RLImage
        image = RLImage(io.BytesIO(base64.b64decode(logo_uri.split(',', 1)[1])))
        max_width = 38 * mm
        image.drawWidth = max_width
        image.drawHeight = max_width * (image.imageHeight / float(image.imageWidth))
        return image
    except Exception:
        return None


def render_asset_pdf(db, order_id, document_type):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

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
    brand = _brand_settings(db)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=16*mm, bottomMargin=16*mm,
                            leftMargin=16*mm, rightMargin=16*mm, title=number)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='DocTitle', parent=styles['Title'], textColor=colors.HexColor('#0B3475')))
    styles.add(ParagraphStyle(name='Small', parent=styles['BodyText'], fontSize=8.5, leading=11,
                              textColor=colors.HexColor('#4B5563')))
    logo = _logo_flowable(brand['logo'], mm)
    identity = [Paragraph(escape(brand['name']), styles['Heading2'])]
    identity_bits = []
    if brand['registration']:
        identity_bits.append(f'Registration: {escape(brand["registration"])}')
    if brand['address']:
        identity_bits.append(escape(brand['address']))
    contact = ' | '.join(value for value in (brand['phone'], brand['email']) if value)
    if contact:
        identity_bits.append(escape(contact))
    if identity_bits:
        identity.append(Paragraph('<br/>'.join(identity_bits), styles['Small']))
    header_left = logo if logo is not None else Paragraph(escape(brand['name'][:2].upper()), styles['Heading1'])
    header = Table([[header_left, identity]], colWidths=[44*mm, 123*mm])
    header.setStyle(TableStyle([('VALIGN', (0,0), (-1,-1), 'MIDDLE'), ('LEFTPADDING',(0,0),(-1,-1),0)]))
    story = [header, Spacer(1, 8), Paragraph(title, styles['DocTitle']),
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
    if document_type == 'invoice':
        item_data = [['Agreed item / specification', 'Brand / model', 'Warranty', 'Qty', 'Unit price', 'Line total']]
    else:
        item_data = [['Agreed item / specification', 'Brand / model', 'Warranty', 'Qty']]
    for item in items:
        name = item['agreed_product_name'] or item['product_name']
        specification = item['agreed_specification'] or item['size_spec'] or '-'
        brand_model = f'{item["agreed_brand"] or item["brand"] or ""} {item["agreed_model"] or item["model"] or ""}'.strip() or '-'
        warranty = item['agreed_warranty'] or '-'
        quantity = int(item['quantity'] or 1)
        unit_price = float(item['unit_selling_price'] or 0)
        description = Paragraph(f'<b>{escape(name)}</b><br/>{escape(specification)}', styles['Small'])
        if document_type == 'invoice':
            item_data.append([description, Paragraph(escape(brand_model), styles['Small']),
                              Paragraph(escape(warranty), styles['Small']), str(quantity),
                              f'NGN {unit_price:,.2f}', f'NGN {unit_price * quantity:,.2f}'])
        else:
            item_data.append([description, Paragraph(escape(brand_model), styles['Small']),
                              Paragraph(escape(warranty), styles['Small']), str(quantity)])
    widths = [58*mm, 32*mm, 24*mm, 10*mm, 22*mm, 26*mm] if document_type == 'invoice' else [82*mm, 46*mm, 29*mm, 10*mm]
    story.append(Table(item_data, colWidths=widths, repeatRows=1,
                       style=TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#0B3475')),
                                         ('TEXTCOLOR',(0,0),(-1,0),colors.white),
                                         ('GRID',(0,0),(-1,-1),.5,colors.HexColor('#D9E2EF')),
                                         ('VALIGN',(0,0),(-1,-1),'TOP'),
                                         ('ALIGN',(-3,1),(-1,-1),'RIGHT'),
                                         ('PADDING',(0,0),(-1,-1),5)])))
    story.append(Spacer(1, 12))
    if document_type == 'invoice':
        totals = [['Cooperative selling price', f'NGN {float(invoice["selling_subtotal"] or 0):,.2f}'],
                  ['Finance charge', f'NGN {float(invoice["finance_charge"] or 0):,.2f}'],
                  ['Total financed repayment', f'NGN {float(invoice["total_repayment"] or 0):,.2f}'],
                  ['Delivery fee (separate)', f'NGN {float(invoice["delivery_fee"] or 0):,.2f}']]
        story.append(Table(totals, colWidths=[105*mm, 62*mm], style=TableStyle([
            ('ALIGN',(1,0),(1,-1),'RIGHT'), ('LINEABOVE',(0,2),(-1,2),1,colors.HexColor('#0B3475')),
            ('FONTNAME',(0,2),(-1,2),'Helvetica-Bold'), ('PADDING',(0,0),(-1,-1),6)])))
        import json
        import repayment_start
        from asset_schemes import effective_annual_rate, schedule_snapshot
        snapshot = (json.loads(order['repayment_schedule_snapshot']) if order['repayment_schedule_snapshot']
                    else schedule_snapshot(order['selling_subtotal'], order['annual_interest_rate'], order['tenure_months']))
        rows = [['Instalment', 'Amount', 'Principal', 'Finance charge', 'Balance after']]
        for row in snapshot['schedule']:
            rows.append([str(row['month']), f'NGN {row["payment"]:,.2f}', f'NGN {row["principal"]:,.2f}',
                         f'NGN {row["interest"]:,.2f}', f'NGN {row["balance"]:,.2f}'])
        story.extend([Spacer(1, 10), Paragraph('Repayment schedule', styles['Heading3']),
                      Table(rows, colWidths=[24*mm, 35*mm, 35*mm, 35*mm, 38*mm], repeatRows=1, style=TableStyle([
                          ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#EEF4FF')),
                          ('GRID', (0,0), (-1,-1), .5, colors.HexColor('#D9E2EF')),
                          ('ALIGN', (1,1), (-1,-1), 'RIGHT'), ('PADDING', (0,0), (-1,-1), 4)])),
                      Spacer(1, 6), Paragraph(
            f'Instalments are deducted through the monthly payroll run once delivery is confirmed: a delivery confirmed '
            f'on or before the {repayment_start.cutoff_day(db)}th starts that month, a later one the next month. '
            f'Equivalent yearly rate on the balance you owe: <b>{effective_annual_rate(order["selling_subtotal"], order["annual_interest_rate"], order["tenure_months"])}%</b> '
            f'(the finance charge is flat, but you repay the principal month by month).', styles['Small'])])
        story.extend([Spacer(1, 10), Paragraph(
            '<b>Important:</b> This accepted invoice records the agreed commercial terms. '
            'The appliance descriptions above are the final items agreed between the cooperative and member. '
            'The asset loan and member receivable begin only when delivery is confirmed.', styles['BodyText'])])
    else:
        story.append(Paragraph(
            '<b>Member confirmation:</b> I confirm that I received the items listed above in satisfactory condition. '
            'My confirmation date becomes the commencement date of the asset loan and agreed repayment schedule.',
            styles['BodyText']))
        story.extend([Spacer(1, 28), KeepTogether(Table([
            ['Member / receiving officer signature', 'Date'],
            ['____________________________', '________________']], colWidths=[110*mm, 57*mm]))])
    doc.build(story)
    buf.seek(0)
    return buf, number

