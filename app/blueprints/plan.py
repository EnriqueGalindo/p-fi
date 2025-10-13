# app/blueprints/plan.py
from flask import Blueprint, current_app, jsonify, redirect, render_template, url_for
from ..logic.plan_engine import compute_plan
from ..services.utils import get_json_from_gcs
from math import ceil, log
from ..logic.plan_engine import MONTH_FACTORS  # reuse factors


STEPS_CFG_PATH = "config/plan_steps.json"
bp = Blueprint("plan", __name__, url_prefix="")

CASH_TYPES = {"cash","checking","savings"}

def _to_monthly(amount, interval):
    if amount is None:
        return 0.0
    return float(amount) * MONTH_FACTORS.get((interval or "monthly").lower(), 1)

def _months_to_payoff(balance: float, apr_percent: float, min_payment: float) -> int | None:
    """
    Months to payoff using a fixed minimum payment and APR.
    Returns None if payment is too small to ever amortize.
    """
    B = float(balance or 0)
    P = float(min_payment or 0)
    r = float(apr_percent or 0) / 100.0 / 12.0
    if B <= 0:
        return 0
    if P <= 0:
        return None
    if r == 0:
        return ceil(B / P)
    # If payment <= monthly interest, balance never decreases
    if P <= r * B:
        return None
    n = log(P / (P - r * B)) / log(1 + r)
    return int(ceil(n))

@bp.get("/overview")
def overview():
    user_id = current_app.config["USER_ID"]
    latest = current_app.gcs.read_json(f"profiles/{user_id}/latest.json")
    if not latest:
        return redirect(url_for("onboarding.onboarding_form"))

    # pull arrays
    incomes  = latest.get("income_streams", []) or []
    costs    = latest.get("recurring_costs", []) or []
    debts    = latest.get("debts", []) or []
    accounts = latest.get("accounts", []) or []
    hh       = int(latest.get("household_size") or 1)

    # cash_now (strict by type)
    cash_now = 0.0
    for a in accounts:
        if (a.get("type") or "").lower() in CASH_TYPES:
            cash_now += float(a.get("balance") or 0)

    # monthly expenses consistent with plan_engine
    costs_monthly = sum(_to_monthly(c.get("amount"), c.get("interval")) for c in costs)
    min_payments  = sum(float(d.get("min_payment") or 0) for d in debts)
    groceries     = 400 * hh
    monthly_required = costs_monthly + min_payments + groceries

    # figure out current step to pick EF target
    from ..logic.plan_engine import compute_plan
    plan = compute_plan(latest)

    # current-step EF target
    six_target = monthly_required * 6
    twelve_target = monthly_required * 12
    if plan["current_step"] <= 2:
        ef_target = 1000.0
    elif plan["current_step"] <= 5:
        ef_target = six_target
    elif plan["current_step"] <= 6:
        ef_target = twelve_target
    else:
        ef_target = twelve_target

    ef_now = max(0.0, cash_now - monthly_required)
    ef_now = min(ef_now, ef_target)

    # “Available” = cash after reserving 1-month costs AND the step EF
    available = max(0.0, cash_now - monthly_required - ef_now)

    # debts sorted by APR, with months to payoff using min payments
    debts_rows = []
    for d in debts:
        bal = float(d.get("balance") or 0)
        apr = float(d.get("apr") or 0)
        mp  = float(d.get("min_payment") or 0)
        months = _months_to_payoff(bal, apr, mp)
        debts_rows.append({
            "name": d.get("name") or "",
            "balance": round(bal, 2),
            "apr": round(apr, 2),
            "min_payment": round(mp, 2),
            "months_to_payoff": months,
        })
    debts_rows.sort(key=lambda x: x["apr"], reverse=True)

    # costs (include the new type)
    cost_rows = []
    for c in costs:
        cost_rows.append({
            "name": c.get("name") or "",
            "type": (c.get("type") or ""),
            "amount_monthly": round(_to_monthly(c.get("amount"), c.get("interval")), 2),
            "interval": (c.get("interval") or "monthly")
        })
    # optional: sort by type then amount
    cost_rows.sort(key=lambda x: (x["type"] or "zzz", -x["amount_monthly"]))

    summary = {
        "cash_now": round(cash_now, 2),
        "ef_now": round(ef_now, 2),
        "ef_target": round(ef_target, 2),
        "available": round(available, 2),
        "monthly_required": round(monthly_required, 2),
        "current_step": plan["current_step"],
    }

    return render_template("overview.html",
                           summary=summary,
                           debts=debts_rows,
                           costs=cost_rows)


@bp.get("/plan")
def view_plan():
    user_id = current_app.config["USER_ID"]
    latest = current_app.gcs.read_json(f"profiles/{user_id}/latest.json")
    if not latest:
        return redirect(url_for("onboarding.onboarding_form"))

    plan = compute_plan(latest)

    steps_cfg = get_json_from_gcs(
        current_app.config["GCS_BUCKET"],
        STEPS_CFG_PATH,
        default={},   # no local default, as you prefer
        ttl=0         # set to 300 later
    ) or {}

    # normalize keys so 2 and "2" both work
    steps_cfg = {str(k): v for k, v in steps_cfg.items()}

    step_copy = steps_cfg.get(str(plan["current_step"]), {})

    # (optional) log so you can see it
    current_app.logger.info("step_copy=%s", step_copy)

    ts = plan["generated_at"].replace(":", "-")
    current_app.gcs.write_json(f"profiles/{user_id}/plans/{ts}.json", plan)
    current_app.gcs.write_json(f"profiles/{user_id}/plan-latest.json", plan)

    return render_template("plan.html", plan=plan, step_copy=step_copy)
