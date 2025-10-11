# app/blueprints/plan.py
from flask import Blueprint, current_app, jsonify, redirect, render_template, url_for
from ..logic.plan_engine import compute_plan

bp = Blueprint("plan", __name__, url_prefix="")

@bp.get("/plan")
def view_plan():
    user_id = current_app.config["USER_ID"]
    latest = current_app.gcs.read_json(f"profiles/{user_id}/latest.json")
    if not latest:
        return redirect(url_for("onboarding.onboarding_form"))
    plan = compute_plan(latest)
    # persist history
    ts = plan["generated_at"].replace(":", "-")
    current_app.gcs.write_json(f"profiles/{user_id}/plans/{ts}.json", plan)
    current_app.gcs.write_json(f"profiles/{user_id}/plan-latest.json", plan)
    return render_template("plan.html", plan=plan)

@bp.get("/plan.json")
def plan_json():
    user_id = current_app.config["USER_ID"]
    data = current_app.gcs.read_json(f"profiles/{user_id}/plan-latest.json")
    if not data:
        latest = current_app.gcs.read_json(f"profiles/{user_id}/latest.json") or {}
        data = compute_plan(latest)
    return jsonify(data)
