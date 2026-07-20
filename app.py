from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment
from datetime import datetime, timedelta
from collections import OrderedDict
import io
import re

app = Flask(__name__)
CORS(app)

currency_fmt = '_("$"* #,##0.00_);_("$"* \\(#,##0.00\\);_("$"* "-"??_);_(@_)'
pct_fmt = '0.00%'


def parse_date(s):
    """Parse a date string. Strips any trailing time component.
    Returns None if unparseable (caller decides fallback) — never silently returns today."""
    if not s:
        return None
    s = str(s).strip()
    s_date = re.split(r'\s+\d{1,2}:\d{2}', s)[0].strip()
    for fmt in ('%m/%d/%Y', '%m/%d/%y', '%Y-%m-%d', '%b %d, %Y', '%B %d, %Y', '%b %d %Y'):
        try:
            return datetime.strptime(s_date, fmt)
        except:
            pass
    return None


def month_bounds(anchor):
    """Return (first_day, last_day) of the month containing anchor."""
    start = anchor.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        nxt = start.replace(year=start.year + 1, month=1)
    else:
        nxt = start.replace(month=start.month + 1)
    return start, nxt - timedelta(days=1)


def set_cell(ws, coord, value, bold=False, align=None, number_format=None):
    cell = ws[coord]
    cell.value = value
    cell.font = Font(name='Aptos Narrow', size=11, bold=bold)
    if align:
        cell.alignment = Alignment(horizontal=align, wrap_text=(align == 'wrap'))
    if number_format:
        cell.number_format = number_format


def apply_group(acts, bal, rate, spread, floor):
    """Apply every activity that shares one date. Returns (memo, net_trans, bal, rate)."""
    memos = []
    net = 0.0
    for act in acts:
        dis = float(act.get('dis') or 0)
        pp = float(act.get('pp') or 0)
        ip = float(act.get('ip') or 0)
        pr = act.get('pr')

        if dis:
            bal += dis
            net += dis
        if pp:
            bal -= pp
            net -= pp
        if ip:
            bal += ip
            net += ip
        if pr is not None and pr != '':
            rate = max(spread + float(pr), floor)

        t = (act.get('t') or '').strip()
        if t and t not in memos:
            memos.append(t)

    return ' / '.join(memos), net, bal, rate


@app.route('/generate', methods=['POST'])
def generate():
    data = request.json
    loan = data.get('loan', {})
    activities = data.get('activities', [])

    activities = [a for a in activities
                  if a.get('d') and parse_date(a.get('d')) is not None
                  and a.get('t') not in ('EDPC', 'Notes')]
    activities.sort(key=lambda x: parse_date(x.get('d')))

    # ---- Statement period: driven by the client's selected month ----
    period_start = parse_date(loan.get('period_start'))
    period_end = parse_date(loan.get('period_end'))
    if period_start is None or period_end is None:
        if activities:
            anchor = parse_date(activities[-1]['d'])      # most recent, not oldest
        else:
            anchor = parse_date(loan.get('fd')) or datetime.now()
        period_start, period_end = month_bounds(anchor)
    else:
        period_start, _ = month_bounds(period_start)
        _, period_end = month_bounds(period_end)
    statement_date = period_end + timedelta(days=1)

    funding_date = parse_date(loan.get('fd'))
    if funding_date is None:
        funding_date = parse_date(activities[0]['d']) if activities else period_start

    spread = float(loan.get('spread') or 0)
    floor = float(loan.get('floor') or 0)
    bal = float(loan.get('bp') or loan.get('bal') or 0)
    rate = float(loan.get('rate') or 0)

    # ---- Group activities by date, drop anything after the period ----
    grouped = OrderedDict()
    for act in activities:
        d = parse_date(act['d'])
        if d > period_end:
            continue
        grouped.setdefault(d, []).append(act)

    # ---- Replay pre-period activity into the opening balance (no rows emitted) ----
    events = []
    for d in sorted(grouped.keys()):
        memo, net, bal, rate = apply_group(grouped[d], bal, rate, spread, floor)
        if d < period_start:
            continue
        events.append({'date': d, 'memo': memo or 'Activity',
                       'trans': net, 'balance': bal, 'rate': rate})

    # Opening state = balance/rate after all pre-period replay, before in-period events.
    # Recomputed from scratch so the opening row is unambiguous.
    o_bal = float(loan.get('bp') or loan.get('bal') or 0)
    o_rate = float(loan.get('rate') or 0)
    for d in sorted(grouped.keys()):
        if d < period_start:
            _, _, o_bal, o_rate = apply_group(grouped[d], o_bal, o_rate, spread, floor)

    # ---- Boundaries: opening row clipped to the period, then in-period events ----
    opening_start = max(period_start, funding_date)
    boundaries = [{'date': opening_start, 'memo': 'Balance Forward',
                   'trans': 0, 'balance': o_bal, 'rate': o_rate}]
    boundaries.extend(e for e in events if e['date'] >= opening_start)

    # ---- Segment the period ----
    rows = []
    total_interest = 0.0
    for i, seg in enumerate(boundaries):
        start = seg['date']
        if start > period_end:
            continue
        if i < len(boundaries) - 1:
            nxt = boundaries[i + 1]['date']
            days = (nxt - start).days
            to_date = nxt - timedelta(days=1)
        else:
            days = (period_end - start).days + 1
            to_date = period_end

        if days <= 0 and seg['trans'] == 0:
            continue  # opening row superseded by an event on the same date

        interest = round(seg['balance'] * seg['rate'] / 360 * days, 2) if days > 0 else 0
        total_interest += interest

        rows.append({
            'memo': seg['memo'],
            'type': '',
            'principal': seg['balance'],
            'trans': seg['trans'],
            'dates': f"{start.strftime('%m/%d/%Y')} - {to_date.strftime('%m/%d/%Y')}",
            'days': days,
            'rate': seg['rate'] if seg['rate'] else None,
            'interest': interest
        })

    total_interest = round(total_interest, 2)
    all_rows = rows
    closing_balance = boundaries[-1]['balance'] if boundaries else o_bal

    wb = Workbook()
    ws = wb.active
    ws.title = 'Billing Statement'

    col_widths = {'A': 35, 'B': 20, 'C': 18, 'D': 20, 'E': 28, 'F': 12, 'G': 30, 'H': 16}
    for col, width in col_widths.items():
        ws.column_dimensions[col].width = width

    ws.merge_cells('A1:H1')
    set_cell(ws, 'A1', 'LOAN BILLING STATEMENT', bold=True, align='center')

    set_cell(ws, 'A2', loan.get('bn', ''))
    set_cell(ws, 'G2', 'As of Date:', align='right')
    set_cell(ws, 'H2', period_end.strftime('%m/%d/%Y'))

    set_cell(ws, 'A4', '1111 North Post Oak Road')
    set_cell(ws, 'G4', 'Statement Date:', align='right')
    set_cell(ws, 'H4', statement_date.strftime('%m/%d/%Y'))

    set_cell(ws, 'A5', 'Houston, Texas 77055')

    set_cell(ws, 'A9', 'Loan Number / Unit:', align='right')
    set_cell(ws, 'B9', loan.get('ln', ''))

    set_cell(ws, 'A10', 'Address:', align='right')
    set_cell(ws, 'B10', loan.get('pa', ''))

    set_cell(ws, 'B13', 'Loan Commitment:', bold=True)
    set_cell(ws, 'C13', float(loan.get('na') or 0), bold=True, number_format=currency_fmt)

    set_cell(ws, 'A15', 'Memo Description', bold=True)
    set_cell(ws, 'C15', 'Billing Date', bold=True)
    set_cell(ws, 'D15', 'Due Date', bold=True)
    set_cell(ws, 'E15', 'Amount Due', bold=True)

    set_cell(ws, 'A16', 'INTEREST BILLING - PERIOD END')
    set_cell(ws, 'C16', period_end.strftime('%m/%d/%Y'), align='left')
    set_cell(ws, 'D16', statement_date.strftime('%m/%d/%Y'), align='left')
    set_cell(ws, 'E16', total_interest, bold=True, number_format=currency_fmt)

    set_cell(ws, 'D17', 'Total:')
    set_cell(ws, 'E17', total_interest, bold=True, number_format=currency_fmt)

    headers = [('A', 'Memo Description'), ('C', 'Principal Balance'),
               ('D', 'Transaction Amount'), ('E', 'From / To Date'), ('F', '# of Days'),
               ('G', 'Rate'), ('H', 'Interest Due')]
    for col, val in headers:
        set_cell(ws, f'{col}20', val, bold=True, align='center')

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
    set_cell(ws, f'C{total_row}', closing_balance, bold=True, number_format=currency_fmt)
    set_cell(ws, f'D{total_row}', sum(r['trans'] for r in all_rows), bold=True, number_format=currency_fmt)
    set_cell(ws, f'G{total_row}', 'Total Interest for the Month:', bold=True, align='right')
    set_cell(ws, f'H{total_row}', total_interest, bold=True, number_format=currency_fmt)

    pay_row = total_row + 3
    set_cell(ws, f'G{pay_row}', 'PLEASE PAY THIS AMOUNT:', bold=True, align='right')
    set_cell(ws, f'H{pay_row}', total_interest, bold=True, number_format=currency_fmt)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    filename = f"{loan.get('ln', 'Loan')}_{period_end.strftime('%Y-%m')}_Billing_Statement.xlsx"
    return send_file(buffer, as_attachment=True, download_name=filename,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    app.run(debug=True)
