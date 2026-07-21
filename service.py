"""
RealFin loan service.

  POST /schedule    -> JSON monthly schedule from the accrual engine
  POST /statement   -> XLSX billing statement, balances derived BY the engine
  POST /generate    -> legacy statement endpoint (Bubble supplies bp). Unchanged,
                       kept alive for the parallel run. Retire once /statement is trusted.
  GET  /health

WHY /statement EXISTS
  /generate trusts whatever opening balance Bubble hands it. That is exactly how the
  June statement came out wrong while being internally consistent. /statement takes the
  loan's raw inputs, runs the engine, and derives the opening balance itself, so a bad
  stored balance cannot reach a borrower.
"""

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from datetime import datetime, date
import io
import re
import traceback
from openpyxl import load_workbook

from accrual2 import (Loan, Disbursement, Paydown, InterestPayment,
                      PrimeChange, build_schedule)
import app as legacy   # existing statement writer

application = app = Flask(__name__)
CORS(app)


# ---------------------------------------------------------------- parsing

def parse_date(s):
    if not s:
        return None
    if isinstance(s, (date, datetime)):
        return s.date() if isinstance(s, datetime) else s
    s = re.split(r'\s+\d{1,2}:\d{2}', str(s).strip())[0].strip()
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y', '%b %d, %Y', '%B %d, %Y'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def num(v, default=0.0):
    if v in (None, ''):
        return default
    if isinstance(v, str):
        v = v.replace(',', '').replace('$', '').replace('%', '').strip()
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def rate(v):
    """Accept 11.5 or 0.115; both mean 11.5%."""
    x = num(v)
    return x / 100.0 if x > 1 else x


def build_loan(d):
    """Assemble a Loan from the request body. Raises ValueError on missing essentials."""
    start = parse_date(d.get('accrual_start') or d.get('fd') or d.get('funding_date'))
    if start is None:
        raise ValueError("accrual_start (or funding_date) is required and must be a valid date")

    advance = num(d.get('advanced_at_closing', d.get('ac')))
    if advance <= 0:
        raise ValueError("advanced_at_closing must be greater than zero")

    def events(key, cls, amount_key):
        out = []
        for e in d.get(key) or []:
            dt = parse_date(e.get('d') or e.get('date'))
            amt = num(e.get(amount_key, e.get('amount')))
            note = str(e.get('n', e.get('notes', '')) or '').strip()
            if dt and amt:
                out.append(cls(dt, amt, notes=note))
        return out

    primes = []
    for e in d.get('prime_changes') or []:
        dt = parse_date(e.get('d') or e.get('date'))
        pr = e.get('pr', e.get('prime'))
        if dt and pr not in (None, ''):
            note = str(e.get('n', e.get('notes', '')) or '').strip()
            primes.append(PrimeChange(dt, rate(pr), notes=note))

    return Loan(
        number=str(d.get('ln', d.get('loan_number', ''))).strip(),
        borrower=str(d.get('bn', d.get('borrower', ''))).strip(),
        funding_date=start,
        advanced_at_closing=advance,
        spread=rate(d.get('spread', d.get('loan_spread_rate'))),
        floor=rate(d.get('floor', d.get('floor_rate'))),
        initial_prime=rate(d.get('prime', d.get('initial_prime_rate'))),
        commitment=num(d.get('na', d.get('loan_amount'))),
        escrow_holdback=num(d.get('escrow_holdback')),
        interest_reserve=num(d.get('interest_reserve')),
        maturity_date=parse_date(d.get('maturity_date')),
        property_address=str(d.get('pa', d.get('property_address', ''))).strip(),
        disbursements=events('disbursements', Disbursement, 'dis'),
        paydowns=events('paydowns', Paydown, 'pp'),
        interest_payments=events('interest_payments', InterestPayment, 'ip'),
        prime_changes=primes,
    )


def row_json(loan, r, include_segments=False):
    out = {
        'month_number': r.month_number,
        'month_date': r.month_date.isoformat(),
        'period_start': r.period_start.isoformat(),
        'period_end': r.period_end.isoformat(),
        'beginning_balance': round(r.beginning_balance, 2),
        'interest_capitalized': round(r.interest_paid, 2),
        'cash_interest_due': round(r.cash_interest_due, 2),
        'disbursements': round(r.disbursements, 2),
        'paydowns': round(r.paydowns, 2),
        'days_accrued': r.days_accrued,
        'accrued_interest': round(r.accrued_interest, 2),
        'ending_balance': round(r.ending_balance, 2),
        'effective_rate': loan.effective_rate(loan.initial_prime),
        'escrow_remaining': round(r.escrow_remaining, 2),
        'reserve_remaining': round(r.reserve_remaining, 2),
        'projected': r.projected,
        'past_maturity': r.past_maturity,
    }
    if include_segments:
        out['segments'] = [seg_json(s) for s in r.segments]
    return out


def seg_json(s):
    """One activity line. The transaction is split across the columns the
    monthly page already shows, so each maps 1:1 with no Bubble-side logic."""
    memo = (s.memo or '').lower()
    disb = pay = intp = 0.0
    if 'disbursement' in memo:
        disb = round(s.trans, 2)
    elif 'paydown' in memo:
        pay = round(abs(s.trans), 2)
    elif 'interest' in memo:
        intp = round(s.trans, 2)
    return {
        'memo': s.memo,
        'activity_date': s.start.isoformat(),
        'start': s.start.isoformat(),
        'end': s.end.isoformat(),
        'days': s.days,
        'principal_balance': round(s.balance, 2),
        'transaction': round(s.trans, 2),
        'interest_payment': intp,
        'disbursement': disb,
        'paydown': pay,
        'rate': s.rate,
        'prime_rate': s.prime,
        'notes': s.notes or '',
        'interest': round(s.interest, 2),
    }


# ---------------------------------------------------------------- endpoints

@app.route('/schedule', methods=['POST'])
def schedule():
    try:
        body = request.json or {}
        loan = build_loan(body.get('loan', body))
        months = int(num(body.get('months'), 60))
        through = parse_date(body.get('actuals_through')) or date.today()
        segs = bool(body.get('include_segments'))
        rows = build_schedule(loan, months=months, actuals_through=through)

        # Optional: narrow to one month. Bubble sends the Month Selector's date.
        period = parse_date(body.get('period'))
        if period:
            rows = [r for r in rows
                    if r.month_date.year == period.year
                    and r.month_date.month == period.month]
            if not rows:
                return jsonify({'error': f'{period:%B %Y} is outside the schedule'}), 400
        return jsonify({
            'loan_number': loan.number,
            'effective_rate': loan.effective_rate(loan.initial_prime),
            'accrual_start': loan.funding_date.isoformat(),
            'months': len(rows),
            'rows': [row_json(loan, r, segs) for r in rows],
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'{type(e).__name__}: {e}',
                        'trace': traceback.format_exc()[-800:]}), 500


@app.route('/statement', methods=['POST'])
def statement():
    """XLSX for one month, with the opening balance derived by the engine."""
    try:
        body = request.json or {}
        loan = build_loan(body.get('loan', body))
        target = parse_date(body.get('period')) or date.today()
        months = int(num(body.get('months'), 120))
        through = parse_date(body.get('actuals_through')) or date.today()

        rows = build_schedule(loan, months=months, actuals_through=through)
        row = next((r for r in rows
                    if r.month_date.year == target.year
                    and r.month_date.month == target.month), None)
        if row is None:
            return jsonify({'error': f'{target:%B %Y} is outside the schedule; '
                                     f'it runs {rows[0].month_date:%b %Y} to '
                                     f'{rows[-1].month_date:%b %Y}'}), 400

        acts = []
        if row.interest_paid:
            acts.append({'d': row.month_date.isoformat(), 't': 'Interest Payment',
                         'ip': row.interest_paid})
        for x in loan.disbursements:
            if row.period_start <= x.date <= row.period_end:
                acts.append({'d': x.date.isoformat(), 't': x.memo, 'dis': x.amount})
        for x in loan.paydowns:
            if row.period_start <= x.date <= row.period_end:
                acts.append({'d': x.date.isoformat(), 't': x.memo, 'pp': x.amount})
        for c in loan.prime_changes:
            if row.period_start <= c.date <= row.period_end:
                acts.append({'d': c.date.isoformat(), 't': 'Prime Rate Change',
                             'pr': c.prime})

        payload = {
            'loan': {
                'ln': loan.number, 'bn': loan.borrower, 'na': loan.commitment,
                'pa': loan.property_address,
                'bp': row.beginning_balance,
                'rate': loan.effective_rate(loan.initial_prime),
                'spread': loan.spread, 'floor': loan.floor,
                'fd': loan.funding_date.isoformat(),
                'period_start': row.period_start.isoformat(),
                'period_end': row.period_end.isoformat(),
            },
            'activities': acts,
        }

        with legacy.app.test_request_context(json=payload):
            resp = legacy.generate()

        resp.direct_passthrough = False
        data = resp.get_data()

        # Cross-check: the statement's total must equal the engine's accrual.
        chk = load_workbook(io.BytesIO(data)).active
        i = 21
        while chk[f'A{i}'].value or chk[f'E{i}'].value:
            i += 1
        total = chk[f'H{i}'].value or 0
        if abs(total - row.accrued_interest) > 0.02:
            return jsonify({'error': 'statement total does not match engine accrual',
                            'statement_total': total,
                            'engine_accrual': row.accrued_interest}), 500

        return send_file(
            io.BytesIO(data), as_attachment=True,
            download_name=f"{loan.number}_{row.month_date:%Y-%m}_Billing_Statement.xlsx",
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'{type(e).__name__}: {e}',
                        'trace': traceback.format_exc()[-800:]}), 500


@app.route('/generate', methods=['POST'])
def generate_passthrough():
    with legacy.app.test_request_context(json=request.json):
        return legacy.generate()


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'engine': 'accrual2'})


if __name__ == '__main__':
    app.run(debug=True)
