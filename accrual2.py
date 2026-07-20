"""
RealFin accrual engine — v2.

RULES (confirmed with Shane, 2026-07-20):
  1. Day count is ACTUAL days / 360.
  2. effective_rate = max(spread + prime, floor), COMPUTED not stored.
  3. A disbursement accrues interest on the day it lands, and reduces escrow holdback.
  4. A paydown reduces principal on the day it lands (symmetric with 3).
  5. An INTEREST PAYMENT is an input, entered on its own date. It capitalizes into
     principal on that date and draws down the interest reserve.
  6. A prime rate change entered on date D takes effect D+1.
  7. No bank/investor split. No EDPC.
  8. The schedule is not capped at maturity; it runs to whatever horizon is asked for.

ACCRUAL vs PAYMENT
  Accrued interest never touches principal on its own. Principal moves only when an
  event says so. Each month reports accrued, paid, and the variance between them, so a
  mismatch is visible instead of silently compounding.

PROJECTION MODE
  Past months use entered events only. For months after `actuals_through`, no payment
  has been entered yet, so the engine assumes the prior month's accrual capitalizes on
  the 1st. Set actuals_through=None to disable and use entered events throughout.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from calendar import monthrange
from typing import List, Optional


# ---------------------------------------------------------------- inputs

@dataclass(frozen=True)
class Disbursement:
    date: date
    amount: float
    memo: str = "Disbursement to Borrower"


@dataclass(frozen=True)
class Paydown:
    date: date
    amount: float
    memo: str = "Principal Paydown"


@dataclass(frozen=True)
class InterestPayment:
    date: date
    amount: float
    memo: str = "Interest Payment"


@dataclass(frozen=True)
class PrimeChange:
    date: date
    prime: float


@dataclass
class Loan:
    number: str
    borrower: str
    funding_date: date
    advanced_at_closing: float
    spread: float
    floor: float
    initial_prime: float
    commitment: float = 0.0
    escrow_holdback: float = 0.0
    interest_reserve: float = 0.0
    maturity_date: Optional[date] = None
    property_address: str = ""

    disbursements: List[Disbursement] = field(default_factory=list)
    paydowns: List[Paydown] = field(default_factory=list)
    interest_payments: List[InterestPayment] = field(default_factory=list)
    prime_changes: List[PrimeChange] = field(default_factory=list)

    def effective_rate(self, prime: float) -> float:
        return max(self.spread + prime, self.floor)


# ---------------------------------------------------------------- output

@dataclass
class DaySegment:
    start: date
    end: date
    balance: float
    rate: float
    memo: str
    trans: float

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def interest(self) -> float:
        return self.balance * self.rate / 360 * self.days


@dataclass
class MonthRow:
    month_number: int
    month_date: date
    period_start: date
    period_end: date
    beginning_balance: float
    interest_paid: float          # capitalized this month (entered or projected)
    disbursements: float
    paydowns: float
    days_accrued: int
    accrued_interest: float       # what actually accrued this month
    ending_balance: float
    cash_interest_due: float      # accrual the reserve could not fund
    escrow_remaining: float
    reserve_remaining: float
    projected: bool               # True if capitalization was assumed, not entered
    past_maturity: bool
    segments: List[DaySegment] = field(default_factory=list)

    @property
    def variance(self) -> float:
        """Prior month's accrual minus what was capitalized this month."""
        return round(self._prior_accrual - self.interest_paid, 2)

    _prior_accrual: float = 0.0


# ---------------------------------------------------------------- engine

def _last_day(d: date) -> date:
    return d.replace(day=monthrange(d.year, d.month)[1])


def _add_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def build_schedule(loan: Loan, months: int = 60,
                   actuals_through: Optional[date] = None) -> List[MonthRow]:

    by_date = {}

    def put(d, kind, amount, memo):
        by_date.setdefault(d, []).append((kind, amount, memo))

    for x in loan.disbursements:
        put(x.date, "disb", x.amount, x.memo)
    for x in loan.paydowns:
        put(x.date, "pay", x.amount, x.memo)
    for x in loan.interest_payments:
        put(x.date, "int", x.amount, x.memo)

    primes = sorted(loan.prime_changes, key=lambda c: c.date)

    def prime_on(day: date) -> float:
        cur = loan.initial_prime
        for c in primes:
            if c.date < day:
                cur = c.prime
            else:
                break
        return cur

    balance = round(loan.advanced_at_closing, 2)
    escrow = round(loan.escrow_holdback, 2)
    reserve = round(loan.interest_reserve, 2)
    rows: List[MonthRow] = []

    month_start = loan.funding_date.replace(day=1)
    prior_accrual = 0.0

    for m in range(1, months + 1):
        period_end = _last_day(month_start)
        period_start = loan.funding_date if m == 1 else month_start

        is_projected = bool(actuals_through and period_start > actuals_through)

        # Projection: prior month's accrual capitalizes to the extent the interest
        # reserve can fund it; anything beyond that is cash due from the borrower.
        cash_due = 0.0
        if is_projected and m > 1 and prior_accrual:
            fundable = max(0.0, min(round(prior_accrual, 2), reserve))
            if fundable:
                put(period_start, "int", fundable, "Interest Payment")
            cash_due = round(round(prior_accrual, 2) - fundable, 2)

        beginning_balance = balance
        month_disb = month_pays = month_int = 0.0
        accrued = 0.0

        segments: List[DaySegment] = []
        seg_start = period_start
        seg_balance = balance
        seg_rate = loan.effective_rate(prime_on(period_start))
        seg_memo = "Balance Forward"
        seg_trans = 0.0

        day = period_start
        while day <= period_end:
            memos, trans = [], 0.0
            for kind, amount, memo in by_date.get(day, []):
                if kind == "disb":
                    balance = round(balance + amount, 2)
                    escrow = round(escrow - amount, 2)
                    month_disb += amount
                    trans += amount
                elif kind == "pay":
                    balance = round(balance - amount, 2)
                    month_pays += amount
                    trans -= amount
                elif kind == "int":
                    balance = round(balance + amount, 2)
                    reserve = round(reserve - amount, 2)
                    month_int += amount
                    trans += amount
                if memo not in memos:
                    memos.append(memo)

            rate = loan.effective_rate(prime_on(day))

            if (balance != seg_balance or rate != seg_rate) and day > seg_start:
                segments.append(DaySegment(seg_start, day - timedelta(days=1),
                                           seg_balance, seg_rate, seg_memo, seg_trans))
                seg_start = day
                seg_memo = " / ".join(memos) if memos else "Rate Change"
                seg_trans = trans
            elif day == seg_start and memos:
                seg_memo = " / ".join(memos)
                seg_trans += trans
            seg_balance, seg_rate = balance, rate

            accrued += balance * rate / 360
            day += timedelta(days=1)

        segments.append(DaySegment(seg_start, period_end, seg_balance, seg_rate,
                                   seg_memo, seg_trans))
        accrued = round(accrued, 2)

        row = MonthRow(
            month_number=m, month_date=month_start,
            period_start=period_start, period_end=period_end,
            beginning_balance=beginning_balance,
            interest_paid=round(month_int, 2),
            disbursements=round(month_disb, 2),
            paydowns=round(month_pays, 2),
            days_accrued=(period_end - period_start).days + 1,
            accrued_interest=accrued,
            ending_balance=balance,
            cash_interest_due=cash_due,
            escrow_remaining=escrow,
            reserve_remaining=reserve,
            projected=is_projected,
            past_maturity=bool(loan.maturity_date and period_start > loan.maturity_date),
            segments=segments,
        )
        row._prior_accrual = prior_accrual
        rows.append(row)

        prior_accrual = accrued
        month_start = _add_month(month_start)

    return rows
