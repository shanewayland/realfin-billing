"""Statement regression: /statement must succeed (and tie to the engine) in months
with a prime change or a mid-month interest payment. Run: python testStatement.py"""
from service import app
c = app.test_client()
base = {"ln": "T", "bn": "T", "na": 5000000, "accrual_start": "2026-01-15",
        "advanced_at_closing": 800000, "spread": 3.5, "floor": 8.5, "prime": 6.75,
        "interest_reserve": 300000}
cases = [
    ("prime change mid-month", {"prime_changes": [{"d": "2026-03-10", "pr": 7.0}]}, "2026-03-01", 7177.78),
    ("prime change on last day", {"prime_changes": [{"d": "2026-02-28", "pr": 7.0}]}, "2026-02-01", 6377.78),
    ("prime drops below floor", {"prime_changes": [{"d": "2026-03-10", "pr": 4.0}]}, "2026-03-01", 6244.44),
    ("interest payment mid-month", {"interest_payments": [{"d": "2026-03-12", "ip": 4000}]}, "2026-03-01", 7083.89),
]
P = F = 0
for name, extra, period, want in cases:
    r = c.post('/statement', json={"loan": dict(base, **extra), "period": period,
                                   "actuals_through": "2026-09-01"})
    ok = r.status_code == 200
    # engine accrual for the same month must match the expected total
    s = c.post('/schedule', json={"loan": dict(base, **extra), "period": period,
                                  "actuals_through": "2026-09-01"}).json
    got = s['rows'][0]['accrued_interest']
    ok = ok and abs(got - want) < 0.01
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: status {r.status_code}, accrual {got}, want {want}")
    P, F = (P + 1, F) if ok else (P, F + 1)
print(f"\n  {P} passed, {F} failed")
