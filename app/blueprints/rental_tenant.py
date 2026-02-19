# app/blueprints/rental_tenant.py
from __future__ import annotations

import datetime as dt
from flask import Blueprint, current_app, render_template, session, redirect, url_for, flash
from ..services.utils import tenant_directory_path, parse_ymd, build_coverage_grid

bp = Blueprint("rental_tenant", __name__)

# ---- helpers ----------------------------------------------------

def _current_tenant_email() -> str | None:
    return (session.get("user_email") or "").strip().lower() or None

def _load_tenant_context_for_email(email: str):
    """
    Resolve tenant via your global tenant directory, then load:
      - tenant record (from owner's rentals/tenants.json)
      - properties.json (same owner)
      - receipts for that tenant (however you already do it in rental_admin)
      - coverage_grid (reuse your existing generator)
    """
    # 1) lookup global tenant directory entry (you already write this on tenant add)
    dir_path = tenant_directory_path(email)  # <-- you already have this helper
    entry = current_app.config_store.read_json(dir_path)
    if not entry or not entry.get("active"):
        return None, None, None, None, "Email not recognized as an active tenant."

    owner_user_id = entry.get("owner_user_id")
    tenant_id = entry.get("tenant_id")
    if not owner_user_id or not tenant_id:
        return None, None, None, None, "Tenant directory entry is missing owner_user_id or tenant_id."

    # 2) load owner’s rentals data
    # These should match whatever your rental_admin uses internally.
    # If you already have helpers like _load_tenants_for_user(owner_user_id) use them instead.
    tenants_path = f"profiles/{owner_user_id}/rentals/tenants.json"
    props_path   = f"profiles/{owner_user_id}/rentals/properties.json"
    receipts_path = f"profiles/{owner_user_id}/rentals/receipts.json"  # adjust if different

    tenants = current_app.gcs.read_json(tenants_path) or {}
    properties = current_app.gcs.read_json(props_path) or {}
    all_receipts = current_app.gcs.read_json(receipts_path) or {}

    tenant = tenants.get(tenant_id)
    if not tenant:
        return None, None, None, None, "Tenant record not found."

    # 3) filter receipts to tenant
    tenant_receipts = {
        rid: r for rid, r in all_receipts.items()
        if (r or {}).get("tenant_id") == tenant_id
    }

    # 4) coverage grid (reuse your existing function if you have one)
    # If you already generate coverage_grid in rental_admin.tenant_edit, call that same helper.
    lease_months, coverage_map, coverage_grid = build_coverage_grid(tenant, tenant_receipts)

    return tenant, properties, tenant_receipts, coverage_grid, None


def compute_next_payment_due(tenant: dict, tenant_receipts: dict, now_utc: dt.datetime | None = None) -> dt.date | None:
    """
    Next unpaid covered month within lease window.
    Due date returned as YYYY-MM-01.
    """
    if now_utc is None:
        now_utc = dt.datetime.utcnow().replace(microsecond=0)

    lease = (tenant or {}).get("lease") or {}
    if not lease.get("start_date") or not lease.get("end_date"):
        return None

    try:
        start_dt = parse_ymd(lease["start_date"])
        end_dt   = parse_ymd(lease["end_date"])
    except Exception:
        return None

    if now_utc >= end_dt:
        return None

    covered = set()
    for _, r in (tenant_receipts or {}).items():
        cm = (r or {}).get("covered_month")
        if cm:
            covered.add(cm)

    def ym(d: dt.datetime) -> str:
        return d.strftime("%Y-%m")

    def iter_months(start: dt.datetime, end: dt.datetime):
        cur = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        endm = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while cur < endm:
            yield cur
            y, m = cur.year, cur.month
            cur = cur.replace(year=y + (m // 12), month=(m % 12) + 1)

    search_start = max(now_utc.replace(day=1), start_dt.replace(day=1))
    for m_start in iter_months(search_start, end_dt):
        if ym(m_start) not in covered:
            return m_start.date()

    return None

# ---- routes ------------------------------------------------------

@bp.get("/tenant")
def tenant_portal():
    email = _current_tenant_email()
    if not email:
        flash("Please sign in.", "error")
        return redirect(url_for("auth.login_form", mode="tenant"))

    tenant, properties, tenant_receipts, coverage_grid, err = _load_tenant_context_for_email(email)
    if err:
        flash(err, "error")
        return redirect(url_for("auth.login_form", mode="tenant"))

    next_due = compute_next_payment_due(tenant, tenant_receipts)

    return render_template(
        "rental_tenant/tenant_portal.html",
        tenant=tenant,
        properties=properties,
        tenant_receipts=tenant_receipts,
        coverage_grid=coverage_grid,
        next_payment_due=next_due,
    )
