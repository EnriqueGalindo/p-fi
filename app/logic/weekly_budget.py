# app/logic/weekly_budget.py

from typing import Dict, Any

WEEKS_PER_MONTH = 4.345  # average weeks per month

# Convert each interval to a weekly factor
WEEK_FACTORS = {
    "weekly": 1.0,
    "biweekly": 0.5,            # paid every 2 weeks
    "monthly": 1.0 / WEEKS_PER_MONTH,
    "annual": 1.0 / 52.0,
}

# Known cost types; anything else falls into "uncategorized"
KNOWN_TYPES = {
    "health_fitness",
    "grocery",
    "entertainment",
    "utility",
    "pet",
    "clothes",
}

def _to_weekly(amount, interval) -> float:
    if amount is None:
        return 0.0
    try:
        amt = float(amount)
    except Exception:
        return 0.0
    factor = WEEK_FACTORS.get((interval or "monthly").lower(), WEEK_FACTORS["monthly"])
    return amt * factor


def build_weekly_budget(snapshot: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a weekly budget roll-up.

    Returns:
      {
        "income_week": float,
        "required_week": float,
        "leftover_week": float,
        "groceries_week": float,
        "debt_min_week": float,
        "costs_by_type_week": {type: float, ...},
        "allocations": {
            "to_debt_week": float,
            "to_saving_week": float,
            "rationale": "debt" | "savings" | "none",
            "step": int,
        },
      }
    """
    hh = int(snapshot.get("household_size") or 1)

    incomes = snapshot.get("income_streams") or []
    costs   = snapshot.get("recurring_costs") or []
    debts   = snapshot.get("debts") or []

    # Income: after-tax streams only
    income_week = sum(
        _to_weekly(i.get("amount"), i.get("interval"))
        for i in incomes
        if (i.get("after_tax") is True)
    )

    # Costs by type → weekly
    by_type = {t: 0.0 for t in KNOWN_TYPES}
    by_type["uncategorized"] = 0.0

    for c in costs:
        w = _to_weekly(c.get("amount"), c.get("interval"))
        t = (c.get("type") or "uncategorized").lower()
        if t not in by_type:
            t = "uncategorized"
        by_type[t] += w

    # Groceries rule: $400 per person per month
    groceries_week = (400.0 * hh) / WEEKS_PER_MONTH

    # Debt minimums (sum of mins) → weekly
    debt_min_month = sum(float(d.get("min_payment") or 0.0) for d in debts)
    debt_min_week = debt_min_month / WEEKS_PER_MONTH

    # Required weekly spending & leftover
    required_week = sum(by_type.values()) + groceries_week + debt_min_week
    leftover_week = max(0.0, income_week - required_week)

    # Routing policy based on current step
    step = int(plan.get("current_step") or 0)
    saving_steps = {1, 3, 6}  # $1k EF, 6-mo EF, 12-mo EF
    debt_steps   = {2, 5}     # high-APR debts, medium-APR debts

    if step in debt_steps:
        to_debt_week = leftover_week
        to_saving_week = 0.0
        rationale = "debt"
    elif step in saving_steps:
        to_debt_week = 0.0
        to_saving_week = leftover_week
        rationale = "savings"
    else:
        to_debt_week = 0.0
        to_saving_week = 0.0
        rationale = "none"

    return {
        "income_week": round(income_week, 2),
        "required_week": round(required_week, 2),
        "leftover_week": round(leftover_week, 2),
        "groceries_week": round(groceries_week, 2),
        "debt_min_week": round(debt_min_week, 2),
        "costs_by_type_week": {k: round(v, 2) for k, v in by_type.items()},
        "allocations": {
            "to_debt_week": round(to_debt_week, 2),
            "to_saving_week": round(to_saving_week, 2),
            "rationale": rationale,
            "step": step,
        },
    }
