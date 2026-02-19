# app/blueprints/rental_tenant.py
import os
import time
import uuid
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP

import stripe
from flask import Blueprint, current_app, session, redirect, url_for, flash, request

bp = Blueprint("rental_tenant", __name__)

def _utcnow():
    return dt.datetime.utcnow().replace(microsecond=0)

def _to_cents(x: float) -> int:
    return int((Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100))

def _from_cents(cents: int) -> float:
    return float(Decimal(cents) / Decimal(100))

def _month_range(start_ymd: str, end_ymd: str) -> list[str]:
    # expects YYYY-MM-DD
    s = dt.datetime.fromisoformat(start_ymd + "T00:00:00")
    e = dt.datetime.fromisoformat(end_ymd + "T00:00:00")
    out = []
    cur = dt.datetime(s.year, s.month, 1)
    endm = dt.datetime(e.year, e.month, 1)
    while cur <= endm:
        out.append(f"{cur.year:04d}-{cur.month:02d}")
        # add one month
        if cur.month == 12:
            cur = dt.datetime(cur.year + 1, 1, 1)
        else:
            cur = dt.datetime(cur.year, cur.month + 1, 1)
    return out

def _next_unpaid_month(tenant: dict, tenant_receipts: dict) -> str | None:
    lease = tenant.get("lease") or {}
    s = (lease.get("start_date") or "").strip()
    e = (lease.get("end_date") or "").strip()
    if not s or not e:
        return None

    months = _month_range(s, e)
    covered = set()
    for r in (tenant_receipts or {}).values():
        if not isinstance(r, dict):
            continue
        if r.get("tenant_id") != tenant.get("tenant_id"):
            continue
        if (r.get("status") or "") == "NSF / returned":
            continue
        m = (r.get("covered_month") or "").strip()
        if m:
            # treat any non-NSF receipt as coverage (you can tighten later)
            covered.add(m)

    for m in months:
        if m not in covered:
            return m
    return None

def _policy_for_due_month(now_utc: dt.datetime, due_ym: str, base_rent: float) -> dict:
    """
    Rules:
    - If before the next month that needs to be paid => allow partial payments.
      (interpretation: now < first-of-due-month)
    - If between 1st and 5th of due month => require full amount.
    - If day >= 6 => add 5% late fee (and require full).
    """
    y, m = map(int, due_ym.split("-"))
    due_start = dt.datetime(y, m, 1)

    # Default: full rent, no fee
    allow_partial = False
    require_full = True
    late_fee = 0.0

    if now_utc < due_start:
        allow_partial = True
        require_full = False

    elif now_utc.year == y and now_utc.month == m:
        if 1 <= now_utc.day <= 5:
            allow_partial = False
            require_full = True
        else:
            allow_partial = False
            require_full = True
            late_fee = round(base_rent * 0.05, 2)

    else:
        # If they’re paying after the due month started (and not the same month case above),
        # treat as late by default
        allow_partial = False
        require_full = True
        late_fee = round(base_rent * 0.05, 2)

    total = round(base_rent + late_fee, 2)
    return {
        "due_ym": due_ym,
        "due_start": due_start,
        "base_rent": base_rent,
        "late_fee": late_fee,
        "total": total,
        "allow_partial": allow_partial,
        "require_full": require_full,
    }

def _load_tenants_for_owner(owner_user_id: str) -> dict:
    path = f"profiles/{owner_user_id}/rentals/tenants.json"
    return current_app.gcs.read_json(path) or {}

def _load_properties_for_owner(owner_user_id: str) -> dict:
    path = f"profiles/{owner_user_id}/rentals/properties.json"
    return current_app.gcs.read_json(path) or {}

def _load_receipts_for_owner(owner_user_id: str) -> dict:
    path = f"profiles/{owner_user_id}/rentals/receipts.json"
    return current_app.gcs.read_json(path) or {}

def tenant_directory_path(email: str) -> str:
    # you already have this helper somewhere; keep using yours
    raise NotImplementedError

@bp.post("/tenant/pay/create-checkout-session")
def create_checkout_session():
    # Must be logged in as tenant
    email = (session.get("user_email") or "").strip().lower()
    if not email:
        flash("Please sign in again.", "error")
        return redirect(url_for("auth.login_form", mode="tenant"))

    # Directory record written when owner adds tenant:
    rec = current_app.config_store.read_json(tenant_directory_path(email)) or {}
    if not rec or rec.get("active") is not True:
        flash("Tenant access not active.", "error")
        return redirect(url_for("auth.login_form", mode="tenant"))

    owner_user_id = rec.get("owner_user_id")
    tenant_id = rec.get("tenant_id")
    if not owner_user_id or not tenant_id:
        flash("Tenant directory record is incomplete.", "error")
        return redirect(url_for("auth.login_form", mode="tenant"))

    tenants = _load_tenants_for_owner(owner_user_id)
    tenant = tenants.get(tenant_id) or {}
    if not tenant:
        flash("Tenant not found.", "error")
        return redirect(url_for("auth.login_form", mode="tenant"))

    properties = _load_properties_for_owner(owner_user_id)
    prop = properties.get(tenant.get("property_id")) or {}
    base_rent = float(prop.get("price") or 0.0)
    if base_rent <= 0:
        flash("Rent amount not configured for this property.", "error")
        return redirect(url_for("rental_tenant.tenant_portal"))

    receipts = _load_receipts_for_owner(owner_user_id)
    due_ym = _next_unpaid_month(tenant, receipts)
    if not due_ym:
        flash("No payment due (lease may be fully covered or missing dates).", "info")
        return redirect(url_for("rental_tenant.tenant_portal"))

    now = _utcnow()
    policy = _policy_for_due_month(now, due_ym, base_rent)

    # For MVP, we won't accept user-entered partial amount yet.
    # If you want partial entry, add a form input and validate here when allow_partial=True.
    amount = policy["total"]

    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    app_base = os.environ.get("APP_BASE_URL", "").rstrip("/")
    if not app_base:
        raise RuntimeError("APP_BASE_URL is not set")

    # Build line items (rent + optional late fee)
    line_items = [{
        "price_data": {
            "currency": "usd",
            "product_data": {"name": f"Rent — {due_ym}"},
            "unit_amount": _to_cents(policy["base_rent"]),
        },
        "quantity": 1,
    }]
    if policy["late_fee"] > 0:
        line_items.append({
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Late fee (5%)"},
                "unit_amount": _to_cents(policy["late_fee"]),
            },
            "quantity": 1,
        })

    session_id = f"rent_{uuid.uuid4().hex[:12]}"

    checkout = stripe.checkout.Session.create(
        mode="payment",
        payment_method_types=["card"],
        line_items=line_items,
        success_url=f"{app_base}{url_for('rental_tenant.pay_success')}?sid={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{app_base}{url_for('rental_tenant.pay_cancel')}",
        metadata={
            "kind": "rent",
            "owner_user_id": owner_user_id,
            "tenant_id": tenant_id,
            "property_id": tenant.get("property_id") or "",
            "covered_month": due_ym,
            "base_rent_cents": str(_to_cents(policy["base_rent"])),
            "late_fee_cents": str(_to_cents(policy["late_fee"])),
            "total_cents": str(_to_cents(amount)),
            "policy_allow_partial": str(policy["allow_partial"]).lower(),
            "policy_require_full": str(policy["require_full"]).lower(),
            "app_session_id": session_id,
        },
    )

    return redirect(checkout.url, code=303)

@bp.get("/tenant/pay/success")
def pay_success():
    flash("Payment submitted. It may take a moment to appear in your receipts.", "success")
    return redirect(url_for("rental_tenant.tenant_portal"))

@bp.get("/tenant/pay/cancel")
def pay_cancel():
    flash("Payment canceled.", "info")
    return redirect(url_for("rental_tenant.tenant_portal"))
