import time
import uuid

# routing / rendering
from flask import (
    Blueprint,
    current_app,
    render_template,
    redirect,
    url_for,
    flash,
)

# request / response handling
from flask import (
    request,
    session,
    Response,
    send_file,
)

import io

from ..services.utils import current_user_identity, user_prefix, tenant_directory_path, tenant_email_key, parse_ymd

bp = Blueprint("rental_admin", __name__, url_prefix="/rental-admin")

import datetime as dt

def _month_range(start_date: str, end_date: str) -> list[str]:
    """
    Inclusive month range from start_date to end_date.
    start_date/end_date are YYYY-MM-DD.
    Returns ["YYYY-MM", ...]
    """
    s = dt.date.fromisoformat(start_date)
    e = dt.date.fromisoformat(end_date)

    # normalize to first of month
    cur = dt.date(s.year, s.month, 1)
    last = dt.date(e.year, e.month, 1)

    out = []
    while cur <= last:
        out.append(f"{cur.year:04d}-{cur.month:02d}")
        # increment month
        if cur.month == 12:
            cur = dt.date(cur.year + 1, 1, 1)
        else:
            cur = dt.date(cur.year, cur.month + 1, 1)
    return out

def _month_label(ym: str) -> str:
    # ym = "YYYY-MM"
    y, m = ym.split("-", 1)
    names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    return f"{names[int(m)-1]} {y}"



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

def _load_tenants():
    data = current_app.gcs.read_json(_tenants_path()) or {}
    for t in data.values():
        t.setdefault("email", None)
        t.setdefault("lease", {})
    return data

def _save_tenants(tenants: dict) -> None:
    current_app.gcs.write_json(_tenants_path(), tenants)

def lease_is_active(tenant: dict, now_utc: Optional[dt.datetime] = None) -> bool:
    """
    Lease is active if now is within [start_date, end_date).

    Behavior choices:
    - missing end_date → treat as active (safer default)
    - missing start_date → treat as started
    """

    if now_utc is None:
        now_utc = dt.datetime.utcnow().replace(microsecond=0)

    lease = (tenant or {}).get("lease") or {}

    start_dt = None
    end_dt = None

    try:
        if lease.get("start_date"):
            start_dt = parse_ymd(lease["start_date"])
    except Exception:
        pass

    try:
        if lease.get("end_date"):
            end_dt = parse_ymd(lease["end_date"])
    except Exception:
        pass

    # No end date → safest assumption is still active
    if end_dt is None:
        return True

    # If start exists and hasn't begun → not active
    if start_dt is not None and now_utc < start_dt:
        return False

    # Active until end (exclusive)
    return now_utc < end_dt


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
        email = (request.form.get("email") or "").strip().lower()
        property_id = (request.form.get("property_id") or "").strip()

        lease_start = (request.form.get("lease_start") or "").strip()  # YYYY-MM-DD
        lease_end = (request.form.get("lease_end") or "").strip()

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
            "email": email or None,
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
        # REGISTER TENANT IN GLOBAL TENANT DIRECTORY
        if email:
            _, owner_user_id = current_user_identity()
            now = int(time.time())

            current_app.config_store.write_json(
                tenant_directory_path(email),
                {
                    "owner_user_id": owner_user_id,
                    "tenant_id": tenant_id,
                    "active": True,
                    "updated_at": now,
                }
            )
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

    # Ensure lease dict exists (so t["lease"]["..."] won't crash)
    if not isinstance(t.get("lease"), dict):
        t["lease"] = {}

    if request.method == "POST":
        first_name = (request.form.get("first_name") or "").strip()
        last_name = (request.form.get("last_name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        property_id = (request.form.get("property_id") or "").strip()

        lease_start = (request.form.get("lease_start") or "").strip()
        lease_end = (request.form.get("lease_end") or "").strip()


        if not first_name or not last_name or not property_id:
            flash("First name, last name, and property are required.", "error")
            return render_template(
                "rental_admin/tenant_edit.html",
                tenant=t,
                properties=properties,
            )

        else:
            # Update tenant fields
            t["first_name"] = first_name
            t["last_name"] = last_name
            t["email"] = email
            t["property_id"] = property_id
            t["lease"]["start_date"] = lease_start
            t["lease"]["end_date"] = lease_end
            t["updated_at"] = int(time.time())

            # Optional lease PDF upload
            file = request.files.get("lease_file")
            if file and file.filename:
                safe_name = file.filename.replace("/", "_")
                content_type = file.content_type or "application/pdf"
                data = file.read()

                lease_path = f"{_leases_prefix()}{tenant_id}/{safe_name}"
                current_app.gcs.write_bytes(lease_path, data, content_type=content_type)

                t["lease"]["file_path"] = lease_path
                t["lease"]["file_name"] = safe_name
                t["lease"]["content_type"] = content_type
                t["lease"]["uploaded_at"] = int(time.time())

            tenants[tenant_id] = t
            _save_tenants(tenants)

            flash("Tenant updated.", "success")
            # stay on edit page so you can immediately upload receipts / see coverage
            return redirect(url_for("rental_admin.tenant_edit", tenant_id=tenant_id))

    # Receipts + Lease Coverage context for GET (and POST errors)
    all_receipts = _load_receipts() or {}

    tenant_receipts = {
        rid: r for rid, r in all_receipts.items()
        if isinstance(r, dict) and r.get("tenant_id") == tenant_id
    }

    # Sort newest-first by covered_month then date_paid (both strings)
    tenant_receipts = dict(
        sorted(
            tenant_receipts.items(),
            key=lambda kv: ((kv[1].get("covered_month") or ""), (kv[1].get("date_paid") or "")),
            reverse=True,
        )
    )

    lease = t.get("lease") or {}
    start = lease.get("start_date")
    end = lease.get("end_date")

    lease_months = []
    coverage_map = {}

    if start and end:
        lease_months = _month_range(start, end)  # expects YYYY-MM-DD strings
        for _, r in tenant_receipts.items():
            m = (r.get("covered_month") or "").strip()
            if not m:
                continue
            if (r.get("status") or "") == "NSF / returned":
                continue
            coverage_map.setdefault(m, r)
    
    coverage_grid = []
    for ym in lease_months:
        r = coverage_map.get(ym)
        paid = bool(r)
        coverage_grid.append({
            "ym": ym,
            "label": _month_label(ym),
            "paid": paid,
            "status": (r.get("status") if r else "Not paid"),
            "receipt_id": (r.get("receipt_id") if r else None),
            "date_paid": (r.get("date_paid") if r else None),
            "amount": (r.get("amount") if r else None),
        })


    return render_template(
        "rental_admin/tenant_edit.html",
        tenant=t,
        properties=properties,
        tenant_receipts=tenant_receipts,
        lease_months=lease_months,
        coverage_map=coverage_map,
        coverage_grid=coverage_grid,
    )

# =========================================================
# Tenant Delete
# =========================================================
@bp.route("/tenants/<tenant_id>/delete", methods=["POST"])
def delete_tenant(tenant_id):
    tenants = _load_tenants()
    properties = _load_properties()

    tenant = tenants.get(tenant_id)
    if not tenant:
        flash("Tenant not found.", "error")
        return redirect(url_for("rental_admin.tenant_list"))

    now = int(time.time())

    if lease_is_active(tenant):
        flash("Cannot delete tenant while lease is active.", "error")
        return redirect(url_for("rental_admin.tenant_list"))


    # Unlink property
    property_id = tenant.get("property_id")
    if property_id and property_id in properties:
        prop = properties[property_id]

        if prop.get("tenant_id") == tenant_id:
            prop["tenant_id"] = None
            prop["tenant"] = None
            prop["updated_at"] = now
            properties[property_id] = prop

        _save_properties(properties)

    # Delete lease file (if exists)
    lease = tenant.get("lease", {})
    file_path = lease.get("file_path")

    if file_path:
        try:
            current_app.storage.delete(file_path)
        except Exception:
            current_app.logger.warning("Failed to delete lease file", exc_info=True)

    # Remove from global tenant directory
    email = tenant.get("email")
    if email:
        try:
            current_app.config_store.delete(tenant_directory_path(email))
        except Exception:
            current_app.logger.warning("Failed to delete tenant directory entry", exc_info=True)

    # Remove tenant record
    tenants.pop(tenant_id)
    _save_tenants(tenants)

    flash("Tenant deleted.", "success")
    return redirect(url_for("rental_admin.tenant_list"))


# =========================================================
# Tenant Lease View 
# =========================================================

@bp.get("/tenants/<tenant_id>/lease/view")
def tenant_lease_view(tenant_id: str):
    tenants = _load_tenants()
    t = tenants.get(tenant_id)
    if not t:
        flash("Tenant not found.", "error")
        return redirect(url_for("rental_admin.tenant_list"))

    lease = (t.get("lease") or {})
    path = lease.get("file_path")
    filename = lease.get("file_name") or "lease.pdf"
    content_type = lease.get("content_type") or "application/pdf"

    if not path:
        flash("No lease file uploaded for this tenant.", "error")
        return redirect(url_for("rental_admin.tenant_edit", tenant_id=tenant_id))

    data = current_app.gcs.read_bytes(path)
    if not data:
        flash("Lease file missing in storage.", "error")
        return redirect(url_for("rental_admin.tenant_edit", tenant_id=tenant_id))

    # inline=True opens in-browser for PDFs
    return send_file(
        io.BytesIO(data),
        mimetype=content_type,
        as_attachment=False,
        download_name=filename,
        max_age=0,
    )

# =========================================================
# Tenant Lease Download
# =========================================================

@bp.get("/tenants/<tenant_id>/lease/download")
def tenant_lease_download(tenant_id: str):
    tenants = _load_tenants()
    t = tenants.get(tenant_id)
    if not t:
        flash("Tenant not found.", "error")
        return redirect(url_for("rental_admin.tenant_list"))

    lease = (t.get("lease") or {})
    path = lease.get("file_path")
    filename = lease.get("file_name") or "lease.pdf"
    content_type = lease.get("content_type") or "application/pdf"

    if not path:
        flash("No lease file uploaded for this tenant.", "error")
        return redirect(url_for("rental_admin.tenant_edit", tenant_id=tenant_id))

    data = current_app.gcs.read_bytes(path)
    if not data:
        flash("Lease file missing in storage.", "error")
        return redirect(url_for("rental_admin.tenant_edit", tenant_id=tenant_id))

    # as_attachment=True forces download
    return send_file(
        io.BytesIO(data),
        mimetype=content_type,
        as_attachment=True,
        download_name=filename,
        max_age=0,
    )

# =========================================================
# Receipts
# =========================================================

RECEIPTS_PATH = "rentals/receipts.json"

def _receipts_path() -> str:
    _, user_id = current_user_identity()
    return f"{user_prefix(user_id)}{RECEIPTS_PATH}"

def _receipts_prefix() -> str:
    _, user_id = current_user_identity()
    return f"{user_prefix(user_id)}rentals/receipts/"

def _load_receipts() -> dict:
    data = current_app.gcs.read_json(_receipts_path()) or {}
    return data if isinstance(data, dict) else {}

def _save_receipts(receipts: dict) -> None:
    current_app.gcs.write_json(_receipts_path(), receipts)

# =========================================================
# Receipt Upload + View + Download
# =========================================================

@bp.post("/tenants/<tenant_id>/receipts")
def tenant_receipt_upload(tenant_id: str):
    tenants = _load_tenants()
    t = tenants.get(tenant_id)
    if not t:
        flash("Tenant not found.", "error")
        return redirect(url_for("rental_admin.tenant_list"))

    # Required fields
    covered_month = (request.form.get("covered_month") or "").strip()  # expect YYYY-MM
    date_paid = (request.form.get("date_paid") or "").strip()          # YYYY-MM-DD
    amount_raw = (request.form.get("amount") or "").strip()
    payment_method = (request.form.get("payment_method") or "").strip()
    status = (request.form.get("status") or "").strip()
    check_number = (request.form.get("check_number") or "").strip() or None
    notes = (request.form.get("notes") or "").strip() or None

    # Parse amount
    try:
        amount = float(amount_raw) if amount_raw != "" else None
    except ValueError:
        amount = None

    missing = []
    if not covered_month: missing.append("covered_month")
    if not date_paid: missing.append("date_paid")
    if amount is None: missing.append("amount")
    if not payment_method: missing.append("payment_method")
    if not status: missing.append("status")

    file = request.files.get("receipt_file")
    if not file or not file.filename:
        missing.append("receipt_file")

    if missing:
        flash(f"Missing or invalid: {', '.join(missing)}", "error")
        return redirect(url_for("rental_admin.tenant_edit", tenant_id=tenant_id))

    # Upload file
    receipt_id = f"r_{uuid.uuid4().hex[:10]}"
    safe_name = file.filename.replace("/", "_")
    content_type = file.content_type or "application/pdf"
    data = file.read()

    receipt_path = f"{_receipts_prefix()}{tenant_id}/{receipt_id}/{safe_name}"
    current_app.gcs.write_bytes(receipt_path, data, content_type=content_type)

    now = int(time.time())
    receipts = _load_receipts()
    receipts[receipt_id] = {
        "receipt_id": receipt_id,
        "tenant_id": tenant_id,
        "property_id": t.get("property_id"),
        "covered_month": covered_month,
        "date_paid": date_paid,
        "amount": amount,
        "payment_method": payment_method,
        "check_number": check_number,
        "status": status,
        "notes": notes,
        "file": {
            "file_path": receipt_path,
            "file_name": safe_name,
            "content_type": content_type,
            "uploaded_at": now,
        },
        "created_at": now,
        "updated_at": now,
    }
    _save_receipts(receipts)

    flash("Receipt uploaded.", "success")
    return redirect(url_for("rental_admin.tenant_edit", tenant_id=tenant_id))

@bp.get("/receipts/<receipt_id>/view")
def receipt_view(receipt_id: str):
    receipts = _load_receipts()
    r = receipts.get(receipt_id)
    if not r:
        flash("Receipt not found.", "error")
        return redirect(url_for("rental_admin.tenant_list"))

    f = r.get("file") or {}
    path = f.get("file_path")
    name = f.get("file_name") or "receipt"
    ctype = f.get("content_type") or "application/octet-stream"

    data = current_app.gcs.read_bytes(path) if path else None
    if not data:
        flash("Receipt file missing in storage.", "error")
        return redirect(url_for("rental_admin.tenant_edit", tenant_id=r.get("tenant_id")))

    import io
    return send_file(io.BytesIO(data), mimetype=ctype, as_attachment=False, download_name=name, max_age=0)


@bp.get("/receipts/<receipt_id>/download")
def receipt_download(receipt_id: str):
    receipts = _load_receipts()
    r = receipts.get(receipt_id)
    if not r:
        flash("Receipt not found.", "error")
        return redirect(url_for("rental_admin.tenant_list"))

    f = r.get("file") or {}
    path = f.get("file_path")
    name = f.get("file_name") or "receipt"
    ctype = f.get("content_type") or "application/octet-stream"

    data = current_app.gcs.read_bytes(path) if path else None
    if not data:
        flash("Receipt file missing in storage.", "error")
        return redirect(url_for("rental_admin.tenant_edit", tenant_id=r.get("tenant_id")))

    import io
    return send_file(io.BytesIO(data), mimetype=ctype, as_attachment=True, download_name=name, max_age=0)

@bp.get("/receipts/<receipt_id>")
def receipt_detail(receipt_id: str):
    receipts = _load_receipts() or {}
    r = receipts.get(receipt_id)
    if not r:
        flash("Receipt not found.", "error")
        return redirect(url_for("rental_admin.tenant_list"))

    return render_template("rental_admin/receipt_detail.html", receipt=r)

