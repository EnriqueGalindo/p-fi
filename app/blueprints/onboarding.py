# app/blueprints/onboarding.py
import datetime as dt
from flask import Blueprint, current_app, render_template, request, redirect, url_for
from flask import session, redirect, url_for
from ..services.utils import current_user_identity, user_prefix

bp = Blueprint("onboarding", __name__)

# @bp.before_request
# def require_login_for_blueprint():
#     if not session.get("user_email"):
#         return redirect(url_for("auth.login_form"))

def _profile_prefix(user_id: str) -> str:
    return f"profiles/{user_id}/"

@bp.get("/onboarding")
def onboarding_form():
    _, user_id = current_user_identity()
    pref = user_prefix(user_id)
    latest = current_app.gcs.read_json(f"{pref}latest.json") or {}
    return render_template("onboarding.html", profile=latest)

@bp.post("/onboarding")
def onboarding_submit():
    def collect_group(prefix, fields, cast_map=None):
        cast_map = cast_map or {}
        lists = {k: request.form.getlist(f"{prefix}-{k}[]") for k in fields}
        max_len = max((len(v) for v in lists.values()), default=0)
        items = []
        for i in range(max_len):
            row = {}
            empty = True
            for k in fields:
                vals = lists.get(k, [])
                v = vals[i].strip() if i < len(vals) else ""
                if v != "":
                    empty = False
                if k in (cast_map or {}):
                    try:
                        v = cast_map[k](v) if v != "" else None
                    except Exception:
                        v = None
                row[k] = v
            if not empty:
                items.append(row)
        return items

    _, user_id = current_user_identity()
    currency = (request.form.get("currency") or "USD").upper()
    notes = request.form.get("notes", "").strip()
    household_size = int(request.form.get("household_size") or 1)
    has_employer_plan = (request.form.get("has_employer_plan") == "on")
    employer_match_pct_on_salary = float(request.form.get("employer_match_pct_on_salary") or 0)
    employer_match_rate = float(request.form.get("employer_match_rate") or 0)

    incomes = collect_group("incomes",
                            ["name","amount","interval","after_tax"],
                            {"amount": float, "after_tax": lambda x: x.lower()=="true"})
    costs   = collect_group("costs",
                            ["name","amount","interval","type"],
                            {"amount": float})
    debts = collect_group("debts",
                            ["name","balance","apr","min_payment","due_day","type",
                            "escrow","interest_portion","principal_portion"],
                            {"balance": float,
                                "apr": float,
                                "min_payment": float,
                                "due_day": int,
                                "escrow": float,
                                "interest_portion": float,
                                "principal_portion": float})

    accounts = collect_group("accounts",
                            ["name", "balance", "type"],
                            {"balance": float})
    
    VALID_TYPES = {"cash","checking","savings","investment","retirement"}
    errors = []
    for i, a in enumerate(accounts, start=1):
        has_any = any(a.get(k) not in (None, "",) for k in ("name","balance","type"))
        if not has_any:
            continue  # completely empty row, drop it
        if not a.get("type"):
            errors.append(f"Accounts row {i}: type is required.")
        elif str(a["type"]).lower() not in VALID_TYPES:
            errors.append(f"Accounts row {i}: invalid type '{a['type']}'.")
        else:
            a["type"] = str(a["type"]).lower()

    if errors:
        # Simple error return; you can render the template with errors if you prefer
        return ("Form error:\n" + "\n".join(errors), 400)

    snapshot = {
        "user_id": user_id,
        "snapshot_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "currency": currency,
        "household_size": household_size,
        "has_employer_plan": has_employer_plan,
        "employer_match_pct_on_salary": employer_match_pct_on_salary,
        "employer_match_rate": employer_match_rate,
        "income_streams": incomes,
        "recurring_costs": costs,
        "debts": debts,
        "accounts": accounts,
        "notes": notes,
        "version": 1,
    }
    ts = snapshot["snapshot_at"].replace(":", "-")
    prefix = _profile_prefix(user_id)
    snap_path = f"{prefix}snapshots/{ts}.json"
    latest_path = f"{prefix}latest.json"

    current_app.gcs.write_json(snap_path, snapshot)
    current_app.gcs.write_json(latest_path, snapshot)

    return redirect(url_for("plan.view_plan"))
