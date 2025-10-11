# app/logic/plan_engine.py
import datetime as dt

MONTH_FACTORS = {"monthly":1, "biweekly":26/12, "weekly":52/12, "annual":1/12}

def _to_monthly(amount, interval):
    if amount is None: return 0.0
    return float(amount) * MONTH_FACTORS.get((interval or "monthly").lower(), 1)

def compute_plan(snapshot: dict):
    hh = int(snapshot.get("household_size") or 1)
    incomes = snapshot.get("income_streams", [])
    costs   = snapshot.get("recurring_costs", [])
    debts   = snapshot.get("debts", [])
    accounts= snapshot.get("accounts", [])
    has_match = bool(snapshot.get("has_employer_plan"))

    # income after-tax monthly (assume 75% take-home for pre-tax)
    income_monthly = 0.0
    for inc in incomes:
        amt = _to_monthly(inc.get("amount"), inc.get("interval"))
        after_tax = inc.get("after_tax") in (True, "true", "True", "on")
        income_monthly += amt if after_tax else amt * 0.75

    # monthly expenses
    costs_monthly = sum(_to_monthly(c.get("amount"), c.get("interval")) for c in costs)
    min_payments  = sum(float(d.get("min_payment") or 0) for d in debts)
    groceries     = 400 * hh
    required      = costs_monthly + min_payments + groceries
    leftover      = income_monthly - required

    # debt buckets
    hi_debts  = [d for d in debts if float(d.get("apr") or 0) >= 10]
    med_debts = [d for d in debts if 4 < float(d.get("apr") or 0) < 10]

    monthly_expenses = costs_monthly + min_payments + groceries
    # emergency fund target
    if hi_debts:
        ef_target = max(1000.0, monthly_expenses)      # $1k or 1 month while high APR exists
    else:
        ef_target = monthly_expenses * 3               # default 3 months

    cash_now = sum(float(a.get("balance") or 0) for a in accounts
                   if str(a.get("name","")).lower() in ("cash","checking","savings"))
    ef_gap = max(0.0, ef_target - cash_now)

    # which step are we on?
    if leftover < 0:
        current = 0
    elif ef_gap > 0.01:
        current = 1
    elif has_match:
        current = 2 if (hi_debts or med_debts) else 4
    elif (hi_debts or med_debts):
        current = 3
    else:
        current = 4

    return {
        "generated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "based_on_snapshot_at": snapshot.get("snapshot_at"),
        "household_size": hh,
        "monthly": {
            "income_after_tax": round(income_monthly, 2),
            "required_expenses": round(required, 2),
            "groceries_allowance": round(groceries, 2),
            "leftover": round(leftover, 2),
        },
        "current_step": current,
        "steps": [
            {"id":0,"title":"Outline costs","status": "block" if leftover<0 else "pass",
             "notes":[f"Leftover: ${leftover:,.2f}", f"Required monthly: ${required:,.2f}"]},
            {"id":1,"title":"Emergency fund","target_fund": round(ef_target,2),
             "cash_now": round(cash_now,2),
             "monthly_to_target": round(max(0.0, min(leftover, ef_gap)),2),
             "status":"progress" if ef_gap>0 and leftover>=0 else ("pass" if ef_gap<=0 else "blocked")},
            {"id":2,"title":"Employer match","status":"info" if has_match else "skip"},
            {"id":3,"title":"Pay down high-interest debts",
             "status":"info" if (hi_debts or med_debts) else "skip"},
            {"id":4,"title":"Contribute to an IRA","status":"info"},
            {"id":5,"title":"Save more for retirement","status":"info"},
            {"id":6,"title":"Save for other goals","status":"info"},
        ]
    }
