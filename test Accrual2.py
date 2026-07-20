from datetime import date
from accrual2 import *
P=F=0
def check(l,g,w,tol=0.005):
    global P,F
    ok = abs(g-w)<=tol if isinstance(w,float) else g==w
    print(f"  {'PASS' if ok else 'FAIL'}  {l}: got {g}, want {w}")
    globals().__setitem__('P',P+1) if ok else globals().__setitem__('F',F+1)
def base(**kw):
    d=dict(number="T",borrower="T",funding_date=date(2026,6,1),advanced_at_closing=100000.0,
           spread=0.12,floor=0.0,initial_prime=0.0)
    d.update(kw); return Loan(**d)

print("\n[1] actual/360 full month")
check("interest", build_schedule(base(),1)[0].accrued_interest, 1000.00)

print("\n[2] floor overrides spread+prime")
l=base(spread=0.045,initial_prime=0.0675,floor=0.115)
check("rate", l.effective_rate(l.initial_prime), 0.115)

print("\n[3] spread+prime overrides a lower floor")
l=base(spread=0.045,initial_prime=0.09,floor=0.115)
check("rate", l.effective_rate(l.initial_prime), 0.135)

print("\n[4] interest payment is an input: capitalizes and draws reserve")
l=base(interest_reserve=10000.0,interest_payments=[InterestPayment(date(2026,6,1),500.0)])
r=build_schedule(l,1)[0]
check("ending", r.ending_balance, 100500.00)
check("reserve", r.reserve_remaining, 9500.00)
check("interest", r.accrued_interest, 1005.00)   # 100,500 x12%/360x30

print("\n[5] accrual alone never moves principal")
r=build_schedule(base(),2)[1]
check("month2 beginning", r.beginning_balance, 100000.00)
check("month2 ending", r.ending_balance, 100000.00)

print("\n[6] disbursement accrues day-of and draws escrow")
l=base(escrow_holdback=500000.0,disbursements=[Disbursement(date(2026,6,16),100000.0)])
r=build_schedule(l,1)[0]
check("interest", r.accrued_interest, 1500.00)
check("escrow", r.escrow_remaining, 400000.00)

print("\n[7] paydown reduces day-of")
r=build_schedule(base(paydowns=[Paydown(date(2026,6,16),50000.0)]),1)[0]
check("interest", r.accrued_interest, 750.00)

print("\n[8] prime change entered 6/15 effective 6/16")
l=base(advanced_at_closing=360000.0,spread=0.05,initial_prime=0.05,
       prime_changes=[PrimeChange(date(2026,6,15),0.06)])
check("interest", build_schedule(l,1)[0].accrued_interest, 3150.00)

print("\n[9] projection capitalizes prior accrual when reserve allows")
l=base(interest_reserve=10000.0)
rows=build_schedule(l,2,actuals_through=date(2026,6,30))
check("m2 capitalized", rows[1].interest_paid, 1000.00)
check("m2 beginning", rows[1].beginning_balance, 100000.00)
check("m2 ending", rows[1].ending_balance, 101000.00)
check("m2 cash due", rows[1].cash_interest_due, 0.00)

print("\n[10] reserve exhaustion: partial capitalize, remainder cash")
l=base(interest_reserve=400.0)
rows=build_schedule(l,2,actuals_through=date(2026,6,30))
check("capitalized", rows[1].interest_paid, 400.00)
check("cash due", rows[1].cash_interest_due, 600.00)
check("reserve", rows[1].reserve_remaining, 0.00)

print("\n[11] after exhaustion principal is flat")
l=base(interest_reserve=0.0)
rows=build_schedule(l,4,actuals_through=date(2026,6,30))
check("flat", len({r.ending_balance for r in rows}), 1)
check("all cash due", all(r.cash_interest_due>0 for r in rows[1:]), True)

print("\n[12] ledger identity every month")
l=base(funding_date=date(2026,1,17),advanced_at_closing=250000.0,spread=0.0375,
       initial_prime=0.07,floor=0.06,escrow_holdback=900000.0,interest_reserve=50000.0,
       disbursements=[Disbursement(date(2026,2,3),80000.0),Disbursement(date(2026,2,3),1500.0),
                      Disbursement(date(2026,5,20),300000.0)],
       paydowns=[Paydown(date(2026,4,9),45000.0)],
       interest_payments=[InterestPayment(date(2026,3,1),900.0)],
       prime_changes=[PrimeChange(date(2026,3,18),0.0625)])
rows=build_schedule(l,24,actuals_through=date(2026,6,30))
bad=sum(1 for r in rows if abs(r.ending_balance-round(r.beginning_balance+r.interest_paid+r.disbursements-r.paydowns,2))>0.005)
check("failures", bad, 0)
print("\n[13] chain integrity")
check("breaks", sum(1 for a,b in zip(rows,rows[1:]) if abs(b.beginning_balance-a.ending_balance)>0.005), 0)
print("\n[14] segments tile each month exactly")
bad=0
for r in rows:
    if r.segments[0].start!=r.period_start or r.segments[-1].end!=r.period_end: bad+=1
    if sum(s.days for s in r.segments)!=r.days_accrued: bad+=1
    bad+=sum(1 for a,b in zip(r.segments,r.segments[1:]) if (b.start-a.end).days!=1)
check("defects", bad, 0)
print("\n[15] segment interest sums to month accrual")
check("mismatches", sum(1 for r in rows if abs(round(sum(s.interest for s in r.segments),2)-r.accrued_interest)>0.02), 0)
print("\n[16] escrow never silently goes negative unnoticed")
l=base(escrow_holdback=1000.0,disbursements=[Disbursement(date(2026,6,10),5000.0)])
check("negative escrow flagged", build_schedule(l,1)[0].escrow_remaining, -4000.00)
print("\n[17] leap february")
check("days", build_schedule(base(funding_date=date(2028,2,1)),1)[0].days_accrued, 29)
print("\n[18] december rolls to january")
check("m2", build_schedule(base(funding_date=date(2026,12,1)),2)[1].month_date, date(2027,1,1))
print("\n[19] past-maturity months are flagged not truncated")
l=base(maturity_date=date(2026,8,31))
rows=build_schedule(l,6)
check("rows", len(rows), 6)
check("flagged", sum(1 for r in rows if r.past_maturity), 3)
print("\n[20] LOAN 001 REGRESSION: engine accrual == entered payments")
l=Loan(number="001",borrower="SMH",funding_date=date(2026,3,30),advanced_at_closing=69003.96,
       spread=0.045,floor=0.115,initial_prime=0.0675,escrow_holdback=2890997.0,
       interest_reserve=233580.69,
       disbursements=[Disbursement(date(2026,5,27),374217.62)],
       interest_payments=[InterestPayment(date(2026,4,1),198.39),InterestPayment(date(2026,5,1),663.19),
                          InterestPayment(date(2026,6,1),1289.57),InterestPayment(date(2026,7,1),4268.16)])
rows=build_schedule(l,6,actuals_through=date(2026,7,20))
check("Apr accrual == May payment", rows[1].accrued_interest, 663.19)
check("May accrual == Jun payment", rows[2].accrued_interest, 1289.57)
check("Jun accrual == Jul payment", rows[3].accrued_interest, 4268.16)
check("escrow after draw", rows[2].escrow_remaining, 2516779.38)
print(f"\n{'='*52}\n  {P} passed, {F} failed\n{'='*52}")
