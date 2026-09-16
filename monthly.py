"""
Monthly billing-statement email.

POST /monthly-statements   (header X-Cron-Key must equal env CRON_KEY)

On the 1st of each month a GitHub Actions schedule calls this endpoint. It:
  1. pulls every Closed loan from Bubble (backend workflow `statement_data`,
     admin-only, called with BUBBLE_API_TOKEN), then each loan's activity
     100 entries at a time, checked against Bubble's count,
  2. builds last month's statement for each loan with the same /statement
     code the Download Billing Statement button uses,
  3. emails all of them in one message from MAIL_FROM to MAIL_TO.

Query options (for testing):
  scheduled=1   only run if it is 8:00-8:59am on the 1st in Houston time
                (the schedule fires at 13:00 and 14:00 UTC to cover DST)
  dry_run=1     build everything, send nothing, return the summary
  to=<email>    send to this address instead of MAIL_TO
  bubble=test   read from the Bubble development database
  period=YYYY-MM-01   statement month (default: last month)

Env: BUBBLE_API_TOKEN, CRON_KEY, SMTP_HOST, SMTP_PORT, SMTP_USER (optional,
defaults to MAIL_FROM), SMTP_PASSWORD, MAIL_FROM, MAIL_TO, BUBBLE_BASE (optional).
"""

import hmac
import json
import os
import re
import smtplib
import urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, request

CENTRAL = ZoneInfo('America/Chicago')
XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

bp = Blueprint('monthly', __name__)


# ------------------------------------------------------------------ helpers

def _norm(k):
    return re.sub(r'[^a-z0-9]', '', str(k).lower())


def field(obj, *names, default=None):
    """Bubble returns fields under their display names; match loosely."""
    idx = {_norm(k): v for k, v in obj.items()}
    for n in names:
        v = idx.get(_norm(n))
        if v not in (None, ''):
            return v
    return default


def central_date(v):
    """Bubble dates arrive as UTC timestamps; the app shows them in Houston time."""
    if v in (None, ''):
        return None
    if isinstance(v, (int, float)):
        dt = datetime.fromtimestamp(v / 1000, tz=ZoneInfo('UTC'))
    else:
        s = str(v).strip().replace('Z', '+00:00')
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return s[:10]
        if dt.tzinfo is None:
            return dt.date().isoformat()
    return dt.astimezone(CENTRAL).date().isoformat()


def now_central():
    return datetime.now(CENTRAL)


def prior_month(today):
    first = today.replace(day=1)
    return (first - timedelta(days=1)).replace(day=1)


def _call_bubble(version, payload):
    base = os.environ.get('BUBBLE_BASE', 'https://realfin.elevateebs.com').rstrip('/')
    path = '/version-test' if version == 'test' else ''
    req = urllib.request.Request(
        f'{base}{path}/api/1.1/wf/statement_data', data=json.dumps(payload).encode(),
        method='POST',
        headers={'Authorization': f"Bearer {os.environ['BUBBLE_API_TOKEN']}",
                 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.loads(r.read().decode())
    return body.get('response', body)


PAGE = 100


def fetch_loans(version):
    return _call_bubble(version, {}).get('loans') or []


def fetch_activity(version, loan_id):
    """All activity for one loan, 100 at a time. Returns (items, expected_count)."""
    items, expected, offset = [], None, 0
    while True:
        resp = _call_bubble(version, {'loan': loan_id, 'offset': offset})
        page = resp.get('activities') or []
        if expected is None:
            expected = int(resp.get('activity_count') or 0)
        items.extend(page)
        offset += PAGE
        if len(page) < PAGE or len(items) >= expected:
            return items, expected


def loan_payload(loan, acts):
    """Same shape the Monthly Schedule page sends to /statement."""
    disb, pays, ints, primes = [], [], [], []
    for a in acts:
        d = central_date(field(a, 'activity_date'))
        if not d:
            continue
        dis = field(a, 'disbursement_to_borrower')
        pp = field(a, 'principal_paydown')
        ip = field(a, 'interest_payment')
        pr = field(a, 'prime_rate', 'prime rate')
        if dis:
            disb.append({'d': d, 'dis': dis})
        if pp:
            pays.append({'d': d, 'pp': pp})
        if ip:
            ints.append({'d': d, 'ip': ip})
        if pr is not None:
            primes.append({'d': d, 'pr': pr})
    return {
        'ln': field(loan, 'loan_number', default=''),
        'bn': field(loan, 'borrower_name', default=''),
        'na': field(loan, 'loan_amount', 'note_loan_amount', default=0),
        'pa': field(loan, 'property_address', default=''),
        'accrual_start': central_date(field(loan, 'accrual start date', 'accrual_start_date'))
                         or central_date(field(loan, 'funding_date')),
        'advanced_at_closing': field(loan, 'advanced_at_closing', default=0),
        'spread': field(loan, 'loan_spread_rate', default=0),
        'floor': field(loan, 'floor_rate', default=0),
        'prime': field(loan, 'initial_prime_rate', default=0),
        'escrow_holdback': field(loan, 'escrow_holdback', default=0),
        'interest_reserve': field(loan, 'interest_reserve', default=0),
        'maturity_date': central_date(field(loan, 'maturity_date')),
        'disbursements': disb, 'paydowns': pays,
        'interest_payments': ints, 'prime_changes': primes,
    }


def send_mail(to_addr, subject, text, attachments):
    sender = os.environ['MAIL_FROM']
    msg = EmailMessage()
    msg['From'] = sender
    msg['To'] = to_addr
    msg['Subject'] = subject
    msg.set_content(text)
    for name, data in attachments:
        maintype, subtype = XLSX.split('/', 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    host = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
    port = int(os.environ.get('SMTP_PORT', '587'))
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.starttls()
        s.login(os.environ.get('SMTP_USER') or sender, os.environ['SMTP_PASSWORD'])
        s.send_message(msg)


# ------------------------------------------------------------------ endpoint

@bp.route('/monthly-statements', methods=['POST'])
def monthly_statements():
    key = os.environ.get('CRON_KEY', '')
    if not key or not hmac.compare_digest(request.headers.get('X-Cron-Key', ''), key):
        return jsonify({'error': 'unauthorized'}), 401

    now = now_central()
    if request.args.get('scheduled') and not (now.day == 1 and now.hour == 8):
        return jsonify({'skipped': f'not 8am on the 1st in Houston (it is {now:%Y-%m-%d %H:%M})'})

    today = now.date()
    period = request.args.get('period')
    period = date.fromisoformat(period).replace(day=1) if period else prior_month(today)
    dry = bool(request.args.get('dry_run'))
    to_addr = request.args.get('to') or os.environ.get('MAIL_TO', '')

    version = request.args.get('bubble', 'live')
    loans = fetch_loans(version)
    loans.sort(key=lambda l: str(field(l, 'loan_number', default='')))

    from service import app  # the same /statement code the page uses
    client = app.test_client()
    files, skipped, counts = [], [], {}
    for loan in loans:
        acts, expected = fetch_activity(version, loan.get('_id'))
        body = loan_payload(loan, acts)
        label = f"{body['ln']} {body['bn']}".strip()
        counts[body['ln'] or label] = len(acts)
        if len(acts) != expected:
            skipped.append(f"{label}: only {len(acts)} of {expected} activity entries came back from RealFin")
            continue
        r = client.post('/statement', json={'loan': body, 'period': period.isoformat(),
                                            'actuals_through': today.isoformat()})
        if r.status_code == 200:
            files.append((f"{body['ln'] or 'Loan'}_{period:%Y-%m}_Billing_Statement.xlsx", r.data))
        else:
            skipped.append(f"{label}: {(r.get_json(silent=True) or {}).get('error', r.status_code)}")

    summary = {'period': period.strftime('%B %Y'), 'to': to_addr, 'statements': [f for f, _ in files],
               'skipped': skipped, 'activity_counts': counts, 'sent': False}
    if dry:
        return jsonify(summary)
    if not files and not skipped:
        return jsonify(summary)

    lines = [f"Billing statements for {period:%B %Y} — {len(files)} attached.", '']
    lines += [f"  • {name}" for name, _ in files]
    if skipped:
        lines += ['', 'Not included (check these loans):'] + [f"  • {s}" for s in skipped]
    lines += ['', 'Sent automatically by RealFin on the 1st of the month.']
    send_mail(to_addr, f"RealFin billing statements — {period:%B %Y}", '\n'.join(lines), files)
    summary['sent'] = True
    return jsonify(summary)
