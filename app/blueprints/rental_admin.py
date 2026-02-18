# app/blueprints/rental_admin.py
import time
import uuid
from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash, session

bp = Blueprint("rental_admin", __name__, url_prefix="/rental-admin")

UNITS_PATH = "rentals/units.json"


def _load_units() -> dict:
    data = current_app.config_store.read_json(UNITS_PATH) or {}
    # Ensure stable shape
    if not isinstance(data, dict):
        return {}
    return data


def _save_units(units: dict) -> None:
    current_app.config_store.write_json(UNITS_PATH, units)


def _is_owner() -> bool:
    # Simple gate for now. Later you can enforce real roles.
    # If AUTH_DISABLED, you probably want access anyway.
    if current_app.config.get("AUTH_DISABLED"):
        return True
    return bool(session.get("user_id"))  # tighten later to role=="owner"


@bp.before_request
def _guard():
    if not _is_owner():
        flash("Unauthorized.", "error")
        return redirect(url_for("auth.login_form"))


@bp.get("/")
def index():
    return redirect(url_for("rental_admin.property_list"))


@bp.route("/units", methods=["GET", "POST"])
def property_list():
    units = _load_units()

    if request.method == "POST":
        address = (request.form.get("address") or "").strip()
        price = (request.form.get("price") or "").strip()
        tenant = (request.form.get("tenant") or "").strip() or "Vacant"

        missing = []
        if not address:
            missing.append("address")
        if not price:
            missing.append("price")

        if missing:
            flash(f"Missing: {', '.join(missing)}", "error")
            return render_template("rental_admin/property_list.html", units=units, form=request.form)

        unit_id = f"u_{uuid.uuid4().hex[:10]}"
        units[unit_id] = {
            "unit_id": unit_id,
            "address": address,
            "price": price,
            "tenant": tenant,
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
        }
        _save_units(units)
        flash("Unit added.", "success")
        return redirect(url_for("rental_admin.property_list"))

    return render_template("rental_admin/property_list.html", units=units, form={})


@bp.route("/units/<unit_id>", methods=["GET", "POST"])
def property_edit(unit_id: str):
    units = _load_units()
    unit = units.get(unit_id)
    if not unit:
        flash("Unit not found.", "error")
        return redirect(url_for("rental_admin.property_list"))

    if request.method == "POST":
        address = (request.form.get("address") or "").strip()
        price = (request.form.get("price") or "").strip()
        tenant = (request.form.get("tenant") or "").strip() or "Vacant"

        if not address or not price:
            flash("Address and price are required.", "error")
            return render_template("rental_admin/property_edit.html", unit=unit)

        unit["address"] = address
        unit["price"] = price
        unit["tenant"] = tenant
        unit["updated_at"] = int(time.time())

        units[unit_id] = unit
        _save_units(units)

        flash("Unit updated.", "success")
        return redirect(url_for("rental_admin.property_list"))

    return render_template("rental_admin/property_edit.html", unit=unit)
