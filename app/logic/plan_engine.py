import datetime as dt

from flask import Blueprint, current_app, jsonify, redirect, render_template, url_for
from ..services.utils import get_json_from_gcs, current_user_identity, user_prefix


MONTH_FACTORS = {"monthly":1, "biweekly":26/12, "weekly":52/12, "annual":1/12}
CASH_TYPES = {"cash", "checking", "savings"}

bp = Blueprint("plan", __name__, url_prefix="")

@bp.get("/plan")
def view_plan():
    _, user_id = current_user_identity()
    pref = user_prefix(user_id)
    latest = current_app.gcs.read_json(f"{pref}latest.json") or {}
    if not latest:
        return redirect(url_for("onboarding.onboarding_form"))

    plan = compute_plan(latest)

    steps_cfg = get_json_from_gcs(
        "pfi-datalake",
        "config/plan_steps.json",
        ttl=0
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

# app/logic/plan_engine.py

def simulate_debt_payoff(
    debts, monthly_extra: float, lump_sum: float, order: str = "avalanche"
):
    """
    debts: list of dicts with keys: name, balance, apr, min_payment
    monthly_extra: extra amount available each month after all minimums are covered
    lump_sum: cash to throw at the top debt immediately (time 0)
    order: "avalanche" (highest APR first) or "snowball" (smallest balance first)

    Returns (schema designed for the template):
    {
      "method": <order>,
      "lump_applied": <float>,
      "monthly_extra": <float>,
      "months_total": <int>,
      "interest_total": <float>,
      "debts": [
         {"name","balance","apr","min_payment","months_to_zero","interest_paid"}
      ],
    }
    """

    # Copy & sanitize
    items = []
    for d in debts:
        bal = max(0.0, float(d.get("balance") or 0))
        apr = max(0.0, float(d.get("apr") or 0))
        mp  = max(0.0, float(d.get("min_payment") or 0))
        if bal <= 0:
            continue
        items.append({
            "name": d.get("name") or "",
            "balance": bal,
            "balance_temp": bal,
            "apr": apr,
            "min_payment": mp,
            "months_to_zero": None,
            "interest_paid": 0.0,
        })

    if not items:
        return {
            "method": order,
            "lump_applied": 0.0,
            "monthly_extra": float(monthly_extra or 0),
            "months_total": 0,
            "interest_total": 0.0,
            "debts": [],
        }

    # Sort per strategy
    if order == "snowball":
        items.sort(key=lambda x: (x["balance"], -x["apr"]))  # smallest balance first
    else:
        items.sort(key=lambda x: (-x["apr"], x["balance"]))  # highest APR first

    # Apply lump sum to the ordered list at time 0
    remaining_lump = max(0.0, float(lump_sum or 0))
    lump_applied = 0.0
    i = 0
    pay = 0
    while remaining_lump > 1e-8 and i < len(items):
        pay = min(items[i]["balance_temp"], remaining_lump)
        items[i]["balance_temp"] -= pay
        remaining_lump -= pay
        lump_applied += pay
        if items[i]["balance_temp"] <= 1e-8:
            items[i]["balance_temp"] = 0.0
            items[i]["months_to_zero"] = 0  # paid off immediately
            i += 1

    total_interest = 0.0
    months = 0

    def all_paid():
        return all(d["balance_temp"] <= 1e-8 for d in items)

    # Monthly loop (cap large to avoid infinite loops)
    while not all_paid() and months < 3600:
        months += 1

        # 1) accrue interest
        for d in items:
            if d["balance"] <= 0:
                continue
            if d.get('balance_temp') is None:
                d['balance_temp'] = d['balance']

            if d.get('balance_temp') <= 0:
                continue
            balance_work = d["balance_temp"]
            r = d["apr"] / 100.0 / 12.0
            interest = balance_work * r
            balance_work +=  interest
            d["interest_paid"] += interest
            total_interest += interest
            d['balance_temp'] = balance_work
        # 2) pay minimums
        for d in items:
            if d["balance"] <= 0:
                continue
            if d.get('balance_temp') is None:
                d['balance_temp'] = d['balance']
            if d.get('balance_temp') <= 0:
                continue
            balance_work = d["balance_temp"]
            p = min(d["min_payment"], balance_work)
            balance_work = d["balance"] - pay
            if balance_work <= 1e-8 and d["months_to_zero"] is None:
                d["months_to_zero"] = months
                balance_work = 0.0
            d['balance_temp'] = balance_work

        # 3) apply monthly extra to the current target (first unpaid in order)
        extra = max(0.0, float(monthly_extra or 0))
        for d in items:
            if extra <= 1e-8:
                break
            if d["balance"] <= 0:
                continue
            if d.get('balance_temp') is None:
                d['balance_temp'] = d['balance']
            if d.get('balance_temp') <= 0:
                continue
            balance_work = d["balance_temp"]

            pay = min(d["balance_temp"], extra)
            d["balance_temp"] = d["balance_temp"] - pay
            extra -= pay
            if d["balance_temp"] <= 1e-8 and d["months_to_zero"] is None:
                d["months_to_zero"] = months
                balance_work = 0.0
            d['balance_temp'] = balance_work

        # Re-sort each month if using snowball/avalanche (optional; keeps target fresh)
        if order == "snowball":
            items.sort(key=lambda x: (x["balance"], -x["apr"]))
        else:
            items.sort(key=lambda x: (-x["apr"], x["balance"]))

    # Fill months_to_zero if cleared on final month
    for d in items:
        if d["months_to_zero"] is None and d["balance_temp"] <= 1e-8:
            d["months_to_zero"] = months

    # Round for display
    for d in items:
        d["balance"] = round(max(0.0, d["balance"]), 2)
        d["interest_paid"] = round(d["interest_paid"], 2)

    return {
        "method": order,
        "lump_applied": round(lump_applied, 2),
        "monthly_extra": round(float(monthly_extra or 0), 2),
        "months_total": months if months < 3600 else None,
        "interest_total": round(total_interest, 2),
        "debts": items,
    }


def compute_plan(snapshot: dict):
    hh = int(snapshot.get("household_size") or 1)
    incomes = snapshot.get("income_streams", []) or []
    costs   = snapshot.get("recurring_costs", []) or []
    debts   = snapshot.get("debts", []) or []
    accounts= snapshot.get("accounts", []) or []
    has_match = bool(snapshot.get("has_employer_plan"))

    # Income after-tax monthly (assume 75% take-home for pre-tax streams)
    income_monthly = 0.0
    for inc in incomes:
        amt = _to_monthly(inc.get("amount"), inc.get("interval"))
        after_tax = inc.get("after_tax") in (True, "true", "True", "on")
        income_monthly += amt if after_tax else amt * 0.75

    # Monthly expenses = recurring + minimum debt payments + groceries
    costs_monthly = sum(_to_monthly(c.get("amount"), c.get("interval")) for c in costs)
    min_payments  = sum(float(d.get("min_payment") or 0) for d in debts)
    groceries     = 400 * hh
    monthly_required = costs_monthly + min_payments + groceries
    leftover      = income_monthly - monthly_required

    # Debts
    hi_debts  = [d for d in debts if float(d.get("apr") or 0) >= 10 and float(d.get("balance") or 0) > 0]
    med_debts = [d for d in debts if 4 < float(d.get("apr") or 0) < 10 and float(d.get("balance") or 0) > 0]

    # Liquid cash toward EF (strict by account type)
    cash_now = sum(float(a.get("balance") or 0) for a in accounts
                   if (a.get("type") or "").lower() in CASH_TYPES)

    # >>> NEW: EF progress is what's left after setting aside 1 month of required costs
    ef_now = max(0.0, cash_now - monthly_required)

    # Step targets
    starter_ef_target    = 1000.0
    six_month_target     = monthly_required * 6
    twelve_month_target  = monthly_required * 12

    # Decide current step (unchanged ordering, but compare against ef_now)
    if leftover < 0:
        current = 0
    elif ef_now + 1e-6 < starter_ef_target:
        current = 1
    elif hi_debts:
        current = 2
    elif ef_now + 1e-6 < six_month_target:
        current = 3
    elif has_match:
        current = 4
    elif med_debts:
        current = 5
    elif ef_now + 1e-6 < twelve_month_target:
        current = 6
    else:
        current = 7

    # High-interest list for Step 2 details
    hi_list = [{
        "name": (d.get("name") or ""),
        "balance": round(float(d.get("balance") or 0), 2),
        "apr": round(float(d.get("apr") or 0), 2),
        "min_payment": round(float(d.get("min_payment") or 0), 2),
    } for d in hi_debts]
    hi_total = round(sum(x["balance"] for x in hi_list), 2)

    # Helper for EF rows
    def ef_row(title, target):
        # Cap EF progress to this step's target so the table never shows more than the goal
        progress = min(ef_now, target)

        return {
            "title": title,
            "target_fund": round(target, 2),
            "ef_now": round(progress, 2),
            "monthly_to_target": round(max(0.0, min(leftover, target - progress)), 2),
            "status": (
                "progress" if progress < target and leftover >= 0
                else ("pass" if progress >= target else "blocked")
            ),
        }


    steps = [
        {"id":0,"title":"Budget / no deficit",
         "status":"block" if leftover<0 else "pass",
         "notes":[f"Leftover: ${leftover:,.2f}", f"Required monthly: ${monthly_required:,.2f}"]},

        dict({"id":1}, **ef_row("Starter EF $1,000", starter_ef_target)),

        {"id":2,"title":"Pay off high-interest debts (≥10% APR)",
         "status":"info" if hi_debts else "pass",
         "debts": hi_list, "debts_total": hi_total},

        dict({"id":3}, **ef_row("6-month emergency fund", six_month_target)),

        {"id":4,"title":"Capture employer match","status":"info" if has_match else "skip"},

        {"id":5,"title":"Pay down medium-interest debts (4%–<10%)",
         "status":"info" if med_debts else "pass"},

        dict({"id":6}, **ef_row("12-month emergency fund", twelve_month_target)),

        {"id":7,"title":"Contribute to an IRA","status":"info"},
        {"id":8,"title":"Save more for retirement","status":"info"},
        {"id":9,"title":"Save for other goals","status":"info"},
    ]

    return {
        "generated_at": dt.datetime.utcnow().isoformat(timespec="seconds")+"Z",
        "based_on_snapshot_at": snapshot.get("snapshot_at"),
        "household_size": hh,
        "monthly": {
            "income_after_tax": round(income_monthly, 2),
            "required_expenses": round(monthly_required, 2),
            "groceries_allowance": round(groceries, 2),
            "leftover": round(leftover, 2),
        },
        "cash_now": round(cash_now, 2),
        "ef_now": round(ef_now, 2),  # <-- expose for other pages if you want
        "current_step": current,
        "steps": steps
    }


def update_accounts_snapshot(
    user_id: str,
    *,
    action: str,                 # "add", "update", or "delete"
    target_name: str | None = None,
    target_type: str | None = None,
    changes: dict | None = None,     # for "update"
    new_account: dict | None = None  # for "add"
):
    """
    Load latest.json, apply an account mutation, then write a new snapshot
    and update latest.json.

    Matching is based on (name, type) for update/delete.
    """
    pref = user_prefix(user_id)
    latest = current_app.gcs.read_json(f"{pref}latest.json") or {}
    latest_path = f"{pref}latest.json"


    if not latest:
        raise RuntimeError("No latest snapshot found for user")

    accounts = latest.get("accounts", [])

    # --- perform the requested action on accounts ---------------------------

    def _matches(acct: dict) -> bool:
        return acct.get("name") == target_name and acct.get("type") == target_type

    if action == "update":
        if changes is None:
            changes = {}
        for acct in accounts:
            if _matches(acct):
                acct.update(changes)
                break
        else:
            raise ValueError(f"Account {target_name!r} ({target_type}) not found")

    elif action == "delete":
        accounts = [acct for acct in accounts if not _matches(acct)]

    elif action == "add":
        if not new_account:
            raise ValueError("new_account is required for action='add'")
        # you can normalize / validate here if you want
        accounts.append(new_account)

    else:
        raise ValueError(f"Unknown action: {action}")

    latest["accounts"] = accounts

    # --- create a new snapshot object --------------------------------------

    snapshot_at = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    # keep everything from latest, just bump snapshot_at (and version if you want)
    snapshot = {
        **latest,
        "snapshot_at": snapshot_at,
        "version": latest.get("version", 1),
    }

    ts = snapshot_at.replace(":", "-")
    snap_path = f"{pref}snapshots/{ts}.json"

    # --- write both snapshot and latest ------------------------------------
    current_app.gcs.write_json(snap_path, snapshot)
    current_app.gcs.write_json(latest_path, snapshot)

    return snapshot
