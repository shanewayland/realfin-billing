from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment
import io
import math

app = Flask(__name__)
CORS(app)

currency_fmt = '_("$"* #,##0.00_);_("$"* \\(#,##0.00\\);_("$"* "-"??_);_(@_)'
pct_fmt = '0.00%'

def set_cell(ws, coord, value, bold=False, align=None, number_format=None):
    cell = ws[coord]
    cell.value = value
    cell.font = Font(name='Aptos Narrow', size=11, bold=bold)
    if align:
        cell.alignment = Alignment(horizontal=align, wrap_text=(align == 'wrap'))
    if number_format:
        cell.number_format = number_format

@app.route('/generate', methods=['POST'])
def generate():
    data = request.json
    loan = data.get('loan', {})
    activities = data.get('activities', [])

    # Sort activities by date
    from datetime import datetime, timedelta
    activities.sort(key=lambda x: x.get('d', '') or '')

    # Billing month setup
    if activities:
        first_date = datetime.strptime(activities[0]['d'], '%m/%d/%Y')
    else:
        first_date = datetime.strptime(loan.get('fd', '01/01/2026'), '%m/%d/%Y')

    billing_month_start = first_date.replace(day=1)
    next_month = billing_month_start.replace(month=billing_month_start.month % 12 + 1) if billing_month_start.month < 12 else billing_month_start.replace(year=billing_month_start.year + 1, month=1)
    billing_month_end = next_month - timedelta(days=1)
    statement_date = next_month

    # Calculate segments
    rows = []
    running_balance = float(loan.get('bp') or loan.get('bal') or 0)
    current_rate = float(loan.get('rate') or 0)
    segment_start = billing_month_start
    total_interest = 0
    int_reserve = float(loan.get('ir') or 0)

    for act in activities:
        if not act.get('d'):
            continue
        act_date = datetime.strptime(act['d'], '%m/%d/%Y')
        days = (act_date - segment_start).days
        if days > 0:
            interest = round(running_balance * current_rate / 360 * days, 2)
            total_interest += interest
            trans_amt = float(act.get('dis') or 0) or (min(float(act.get('ip') or 0), int_reserve) if act.get('ip') else 0)
            rows.append({
                'memo': act.get('t', 'Bal Fwd'),
                'type': '',
                'principal': running_balance,
                'trans': trans_amt,
                'dates': f"{segment_start.strftime('%m/%d/%Y')} - {act_date.strftime('%m/%d/%Y')}",
                'days': days,
                'rate': current_rate,
                'interest': interest
            })

        if act.get('dis'): running_balance += float(act['dis'])
        if act.get('pp'): running_balance -= float(act['pp'])
        if act.get('ip'):
            applied = min(float(act['ip']), int_reserve)
            int_reserve -= applied
            if applied > 0: running_balance += applied
        if act.get('pr'): current_rate = float(act['pr']) + float(loan.get('spread') or 0)
        segment_start = act_date

    # Final segment
    remaining_days = (billing_month_end - segment_start).days + 1
    final_interest = round(running_balance * current_rate / 360 * remaining_days, 2)
    total_interest += final_interest
    total_interest = round(total_interest, 2)

    # Build workbook
    wb = Workbook()
    ws = wb.active
    ws.title = 'Billing Statement'

    col_widths = {'A': 26.43, 'B': 18.71, 'C': 15.86, 'D': 14.29, 'E': 21.86, 'F': 13.43, 'G': 14.57, 'H': 12.29}
    for col, width in col_widths.items():
        ws.column_dimensions[col].width = width

    # Row 1 - Title
    ws.merge_cells('A1:H1')
    set_cell(ws, 'A1', 'LOAN BILLING STATEMENT', bold=True, align='center')

    # Row 2
    set_cell(ws, 'A2', loan.get('bn', ''))
    set_cell(ws, 'G2', 'As of Date:', align='right')
    set_cell(ws, 'H2', billing_month_end.strftime('%m/%d/%Y'))

    # Row 4
    set_cell(ws, 'A4', '1111 North Post Oak Road')
    set_cell(ws, 'G4', 'Statement Date:', align='right')
    set_cell(ws, 'H4', statement_date.strftime('%m/%d/%Y'))

    # Row 5
    set_cell(ws, 'A5', 'Houston, Texas 77055')

    # Row 9
    set_cell(ws, 'A9', 'Loan Number / Unit:', align='right')
    set_cell(ws, 'B9', loan.get('ln', ''))

    # Row 10
    set_cell(ws, 'A10', 'Address:', align='right')
    set_cell(ws, 'B10', loan.get('pa', ''))

    # Row 13
    set_cell(ws, 'B13', 'Loan Commitment:', bold=True)
    set_cell(ws, 'C13', float(loan.get('na') or 0), bold=True, number_format=currency_fmt)

    # Row 15 - Summary headers
    set_cell(ws, 'A15', 'Memo Description', bold=True)
    set_cell(ws, 'C15', 'Billing Date', bold=True)
    set_cell(ws, 'D15', 'Due Date', bold=True)
    set_cell(ws, 'E15', 'Amount Due', bold=True)

    # Row 16
    set_cell(ws, 'A16', 'INTEREST BILLING - PERIOD END')
    set_cell(ws, 'C16', billing_month_end.strftime('%m/%d/%Y'), align='left')
    set_cell(ws, 'D16', statement_date.strftime('%m/%d/%Y'), align='left')
    set_cell(ws, 'E16', total_interest, bold=True, number_format=currency_fmt)

    # Row 17
    set_cell(ws, 'D17', 'Total:')
    set_cell(ws, 'E17', total_interest, bold=True, number_format=currency_fmt)

    # Row 20 - Detail headers
    headers = [('A','Memo Description'),('B','Type'),('C','Principal Balance'),
               ('D','Transaction Amount'),('E','From / To Date'),('F','# of Days'),
               ('G','Rate'),('H','Interest Due')]
    for col, val in headers:
        set_cell(ws, f'{col}20', val, bold=True, align='center')

    # Activity rows starting row 21
    all_rows = rows + [{
        'memo': 'Loan Balance',
        'type': 'Bal Fwd',
        'principal': float(loan.get('na') or loan.get('bp') or loan.get('bal') or 0),
        'trans': 0,
        'dates': f"{segment_start.strftime('%m/%d/%Y')} - {billing_month_end.strftime('%m/%d/%Y')}",
        'days': remaining_days,
        'rate': None,
        'interest': final_interest
    }]

    for i, row in enumerate(all_rows):
        r = 21 + i
        set_cell(ws, f'A{r}', row['memo'])
        set_cell(ws, f'B{r}', row['type'])
        set_cell(ws, f'C{r}', row['principal'], number_format=currency_fmt)
        set_cell(ws, f'D{r}', row['trans'], number_format=currency_fmt)
        set_cell(ws, f'E{r}', row['dates'])
        set_cell(ws, f'F{r}', row['days'])
        if row['rate'] is not None:
            set_cell(ws, f'G{r}', row['rate'], number_format=pct_fmt)
        set_cell(ws, f'H{r}', row['interest'], number_format=currency_fmt)

    total_row = 21 + len(all_rows)
    set_cell(ws, f'B{total_row}', 'Total:', bold=True, align='right')
    set_cell(ws, f'C{total_row}', float(loan.get('na') or 0), bold=True, number_format=currency_fmt)
    set_cell(ws, f'D{total_row}', 0, bold=True, number_format=currency_fmt)
    set_cell(ws, f'G{total_row}', 'Total Interest for the Month:', bold=True, align='right')
    set_cell(ws, f'H{total_row}', total_interest, bold=True, number_format=currency_fmt)

    pay_row = total_row + 3
    set_cell(ws, f'G{pay_row}', 'PLEASE PAY THIS AMOUNT:', bold=True, align='right')
    set_cell(ws, f'H{pay_row}', total_interest, bold=True, number_format=currency_fmt)

    # Save to buffer
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    filename = f"{loan.get('ln', 'Loan')}_Billing_Statement.xlsx"
    return send_file(buffer, as_attachment=True, download_name=filename,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    app.run(debug=True)
