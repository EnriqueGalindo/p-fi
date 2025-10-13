# app/blueprints/plan.py
from flask import Blueprint, current_app, jsonify, redirect, render_template, url_for
from ..logic.plan_engine import compute_plan
from ..services.utils import get_json_from_gcs

STEPS_CFG_PATH = "config/plan_steps.json"
bp = Blueprint("plan", __name__, url_prefix="")

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
