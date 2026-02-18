import time
import uuid
from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash, session
from ..services.utils import current_user_identity, user_prefix

bp = Blueprint("rental_admin", __name__, url_prefix="/rental-admin")

PROPERTIES_PATH = "rentals/properties.json"


def _properties_path() -> str:
    _, user_id = current_user_identity()
    pref = user_prefix(user_id)
    return f"{pref}{PROPERTIES_PATH}"


def _load_properties() -> dict:
    data = current_app.gcs.read_json(_properties_path()) or {}
    return data if isinstance(data, dict) else {}


def _save_properties(properties: dict) -> None:
    current_app.gcs.write_json(_properties_path(), properties)


def _is_owner() -> bool:
    if current_app.config.get("AUTH_DISABLED"):
        return True
    return bool(session.get("user_id"))


@bp.before_request
def _guard():
    if not _is_owner():
        flash("Unauthorized.", "error")
        return redirect(url_for("auth.login_form"))


@bp.get("/")
def index():
    return redirect(url_for("rental_admin.property_list"))


# =========================================================
# PROPERTY LIST + CREATE
# =========================================================
@bp.route("/properties", methods=["GET", "POST"])
def property_list():
    properties = _load_properties()

    if request.method == "POST":
        address = (request.form.get("address") or "").strip()

        price_raw = (request.form.get("price") or "").strip()
        try:
            price = float(price_raw) if price_raw != "" else None
        except ValueError:
            price = None

        tenant = (request.form.get("tenant") or "").strip() or "Vacant"

        missing = []
        if not address:
            missing.append("address")
        if price is None:
            missing.append("price")

        if missing:
            flash(f"Missing or invalid: {', '.join(missing)}", "error")
            return render_template("rental_admin/property_list.html", properties=properties, form=request.form)

        property_id = f"p_{uuid.uuid4().hex[:10]}"

        properties[property_id] = {
            "property_id": property_id,
            "address": address,
            "price": price,
            "tenant": tenant,
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
        }

        _save_properties(properties)
        flash("Property added.", "success")
        return redirect(url_for("rental_admin.property_list"))

    return render_template("rental_admin/property_list.html", properties=properties, form={})


# =========================================================
# PROPERTY EDIT
# =========================================================
@bp.route("/properties/<property_id>", methods=["GET", "POST"])
def property_edit(property_id: str):
    properties = _load_properties()
    prop = properties.get(property_id)

    if not prop:
        flash("Property not found.", "error")
        return redirect(url_for("rental_admin.property_list"))

    if request.method == "POST":
        address = (request.form.get("address") or "").strip()

        price_raw = (request.form.get("price") or "").strip()
        try:
            price = float(price_raw) if price_raw != "" else None
        except ValueError:
            price = None

        tenant = (request.form.get("tenant") or "").strip() or "Vacant"

        if not address or price is None:
            flash("Address and valid price are required.", "error")
            return render_template("rental_admin/property_edit.html", property=prop)

        prop["address"] = address
        prop["price"] = price
        prop["tenant"] = tenant
        prop["updated_at"] = int(time.time())

        properties[property_id] = prop
        _save_properties(properties)

        flash("Property updated.", "success")
        return redirect(url_for("rental_admin.property_list"))

    return render_template("rental_admin/property_edit.html", property=prop)
