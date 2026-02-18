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

# =========================================================
# Tenants
# =========================================================

TENANTS_PATH = "rentals/tenants.json"

def _tenants_path() -> str:
    _, user_id = current_user_identity()
    pref = user_prefix(user_id)
    return f"{pref}{TENANTS_PATH}"

def _leases_prefix() -> str:
    _, user_id = current_user_identity()
    pref = user_prefix(user_id)
    return f"{pref}rentals/leases/"  # folder-like prefix

def _load_tenants() -> dict:
    data = current_app.gcs.read_json(_tenants_path()) or {}
    return data if isinstance(data, dict) else {}

def _save_tenants(tenants: dict) -> None:
    current_app.gcs.write_json(_tenants_path(), tenants)

# =========================================================
# Tenant List
# =========================================================

@bp.route("/tenants", methods=["GET", "POST"])
def tenant_list():
    tenants = _load_tenants()
    properties = _load_properties()  # from your existing code

    if request.method == "POST":
        first_name = (request.form.get("first_name") or "").strip()
        last_name  = (request.form.get("last_name") or "").strip()
        property_id = (request.form.get("property_id") or "").strip()

        lease_start = (request.form.get("lease_start") or "").strip()  # YYYY-MM-DD
        lease_end   = (request.form.get("lease_end") or "").strip()

        missing = []
        if not first_name: missing.append("first_name")
        if not last_name: missing.append("last_name")
        if not property_id: missing.append("property")

        if missing:
            flash(f"Missing: {', '.join(missing)}", "error")
            return render_template(
                "rental_admin/tenant_list.html",
                tenants=tenants,
                properties=properties,
                form=request.form,
            )

        tenant_id = f"t_{uuid.uuid4().hex[:10]}"
        now = int(time.time())

        tenants[tenant_id] = {
            "tenant_id": tenant_id,
            "first_name": first_name,
            "last_name": last_name,
            "property_id": property_id,
            "lease": {
                "start_date": lease_start or None,
                "end_date": lease_end or None,
                "file_path": None,
                "file_name": None,
                "content_type": None,
                "uploaded_at": None,
            },
            "created_at": now,
            "updated_at": now,
        }

        # Link property -> tenant_id (single-tenant model for now)
        prop = properties.get(property_id)
        if prop:
            prop["tenant_id"] = tenant_id
            prop["tenant"] = f"{first_name} {last_name}"  # optional display field
            prop["updated_at"] = now
            properties[property_id] = prop
            _save_properties(properties)

        _save_tenants(tenants)
        flash("Tenant added.", "success")
        return redirect(url_for("rental_admin.tenant_list"))

    return render_template(
        "rental_admin/tenant_list.html",
        tenants=tenants,
        properties=properties,
        form={},
    )

# =========================================================
# Tenant Edit
# =========================================================

@bp.route("/tenants/<tenant_id>", methods=["GET", "POST"])
def tenant_edit(tenant_id: str):
    tenants = _load_tenants()
    properties = _load_properties()
    t = tenants.get(tenant_id)

    if not t:
        flash("Tenant not found.", "error")
        return redirect(url_for("rental_admin.tenant_list"))

    if request.method == "POST":
        first_name = (request.form.get("first_name") or "").strip()
        last_name  = (request.form.get("last_name") or "").strip()
        property_id = (request.form.get("property_id") or "").strip()

        lease_start = (request.form.get("lease_start") or "").strip()
        lease_end   = (request.form.get("lease_end") or "").strip()

        # Handle optional file upload
        file = request.files.get("lease_file")
        if file and file.filename:
            content_type = file.content_type or "application/pdf"
            data = file.read()

            safe_name = file.filename.replace("/", "_")
            lease_path = f"{_leases_prefix()}{tenant_id}/{safe_name}"

            # Requires a bytes-write method on your GCS store:
            current_app.gcs.write_bytes(lease_path, data, content_type=content_type)

            t["lease"]["file_path"] = lease_path
            t["lease"]["file_name"] = safe_name
            t["lease"]["content_type"] = content_type
            t["lease"]["uploaded_at"] = int(time.time())

        if not first_name or not last_name or not property_id:
            flash("First name, last name, and property are required.", "error")
            return render_template(
                "rental_admin/tenant_edit.html",
                tenant=t,
                properties=properties,
            )

        t["first_name"] = first_name
        t["last_name"] = last_name
        t["property_id"] = property_id
        t["lease"]["start_date"] = lease_start or None
        t["lease"]["end_date"] = lease_end or None
        t["updated_at"] = int(time.time())

        tenants[tenant_id] = t
        _save_tenants(tenants)

        flash("Tenant updated.", "success")
        return redirect(url_for("rental_admin.tenant_list"))

    return render_template(
        "rental_admin/tenant_edit.html",
        tenant=t,
        properties=properties,
    )
