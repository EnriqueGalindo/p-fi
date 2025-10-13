import datetime as dt

from flask import Blueprint, current_app, jsonify, redirect, render_template, url_for
from ..logic.plan_engine import compute_plan
from ..services.utils import get_json_from_gcs

MONTH_FACTORS = {"monthly":1, "biweekly":26/12, "weekly":52/12, "annual":1/12}
CASH_TYPES = {"cash", "checking", "savings"}

STEPS_CFG_PATH = "config/plan_steps.json"  # you can change via env later if you want

bp = Blueprint("plan", __name__, url_prefix="")

@bp.get("/plan")
def view_plan():
    user_id = current_app.config["USER_ID"]
    latest = current_app.gcs.read_json(f"profiles/{user_id}/latest.json")
    if not latest:
        return redirect(url_for("onboarding.onboarding_form"))

    plan = compute_plan(latest)

    # load step copy from GCS (with 5-minute TTL cache); fall back to LOCAL_DEFAULT_STEPS
    steps_cfg = get_json_from_gcs(
        current_app.config["GCS_BUCKET"],
        STEPS_CFG_PATH,
        ttl=300
    )

    step_copy = steps_cfg.get(str(plan["current_step"]), {"name": f"Step {plan['current_step']}", "description": "", "requirements": []})

    # persist history (unchanged)
    ts = plan["generated_at"].replace(":", "-")
    current_app.gcs.write_json(f"profiles/{user_id}/plans/{ts}.json", plan)
    current_app.gcs.write_json(f"profiles/{user_id}/plan-latest.json", plan)

    return render_template("plan.html", plan=plan, step_copy=step_copy)

def _to_monthly(amount, interval):
    if amount is None: return 0.0
    return float(amount) * MONTH_FACTORS.get((interval or "monthly").lower(), 1)

def compute_plan(snapshot: dict):
    hh = int(snapshot.get("household_size") or 1)
    incomes = snapshot.get("income_streams", []) or []
    costs   = snapshot.get("recurring_costs", []) or []
    debts   = snapshot.get("debts", []) or []
    accounts= snapshot.get("accounts", []) or []
    has_match = bool(snapshot.get("has_employer_plan"))

    # Income after-tax monthly (assume 75% take-home for pre-tax)
    income_monthly = 0.0
    for inc in incomes:
        amt = _to_monthly(inc.get("amount"), inc.get("interval"))
        after_tax = inc.get("after_tax") in (True, "true", "True", "on")
        income_monthly += amt if after_tax else amt * 0.75

    # Monthly expenses: recurring + minimum debt payments + groceries
    costs_monthly = sum(_to_monthly(c.get("amount"), c.get("interval")) for c in costs)
    min_payments  = sum(float(d.get("min_payment") or 0) for d in debts)
    groceries     = 400 * hh
    required      = costs_monthly + min_payments + groceries
    leftover      = income_monthly - required

    # Debt buckets
    hi_debts   = [d for d in debts if float(d.get("apr") or 0) >= 10 and float(d.get("balance") or 0) > 0]
    med_debts  = [d for d in debts if 4 < float(d.get("apr") or 0) < 10 and float(d.get("balance") or 0) > 0]

    # EF targets
    monthly_expenses = required  # includes min payments + groceries, consistent with UI
    starter_ef_target = 1000.0
    six_month_target  = monthly_expenses * 6
    twelve_month_target = monthly_expenses * 12

    # Liquid cash toward EF
    cash_now = 0.0
    for a in accounts:
        atype = (a.get("type") or "").lower()
        if atype in CASH_TYPES:
            cash_now += float(a.get("balance") or 0)

    # Decide current step in the new order
    if leftover < 0:
        current = 0
    elif cash_now + 1e-6 < starter_ef_target:
        current = 1
    elif hi_debts:
        current = 2
    elif cash_now + 1e-6 < six_month_target:
        current = 3
    elif has_match:
        current = 4
    elif med_debts:
        current = 5
    elif cash_now + 1e-6 < twelve_month_target:
        current = 6
    else:
        current = 7  # on to IRA and beyond

    # Build steps array for the table
    steps = [
        {"id":0,"title":"Budget / no deficit",
         "status": "block" if leftover<0 else "pass",
         "notes":[f"Leftover: ${leftover:,.2f}", f"Required monthly: ${required:,.2f}"]},

        {"id":1,"title":"Starter EF $1,000",
         "target_fund": round(starter_ef_target,2),
         "cash_now": round(cash_now,2),
         "monthly_to_target": round(max(0.0, min(leftover, starter_ef_target - cash_now)),2),
         "status":"progress" if cash_now < starter_ef_target and leftover>=0 else ("pass" if cash_now >= starter_ef_target else "blocked")},

        {"id":2,"title":"Pay off high-interest debts (≥10% APR)",
         "status": "info" if hi_debts else "pass"},

        {"id":3,"title":"6-month emergency fund",
         "target_fund": round(six_month_target,2),
         "cash_now": round(cash_now,2),
         "monthly_to_target": round(max(0.0, min(leftover, six_month_target - cash_now)),2),
         "status":"progress" if cash_now < six_month_target and leftover>=0 else ("pass" if cash_now >= six_month_target else "blocked")},

        {"id":4,"title":"Capture employer match",
         "status":"info" if has_match else "skip"},

        {"id":5,"title":"Pay down medium-interest debts (4%–<10%)",
         "status":"info" if med_debts else "pass"},

        {"id":6,"title":"12-month emergency fund",
         "target_fund": round(twelve_month_target,2),
         "cash_now": round(cash_now,2),
         "monthly_to_target": round(max(0.0, min(leftover, twelve_month_target - cash_now)),2),
         "status":"progress" if cash_now < twelve_month_target and leftover>=0 else ("pass" if cash_now >= twelve_month_target else "blocked")},

        {"id":7,"title":"Contribute to an IRA","status":"info"},
        {"id":8,"title":"Save more for retirement","status":"info"},
        {"id":9,"title":"Save for other goals","status":"info"}
    ]

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
        "steps": steps
    }
