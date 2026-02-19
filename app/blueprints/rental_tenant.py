# app/blueprints/rental_tenant.py
from __future__ import annotations
import os
import uuid
import stripe

import io
import datetime as dt
from flask import (
    Blueprint,
    current_app,
    render_template,
    session,
    redirect,
    url_for,
    flash,
    request,
    Response
)
from ..services.utils import (
    tenant_directory_path,
    parse_ymd,
    build_coverage_grid
)


bp = Blueprint("rental_tenant", __name__)

# ---- helpers ----------------------------------------------------

def _current_tenant_email() -> str | None:
    return (session.get("user_email") or "").strip().lower() or None

def _load_tenant_context_for_email(email: str):
    # 1) lookup global tenant directory entry
    dir_path = tenant_directory_path(email)
    entry = current_app.config_store.read_json(dir_path)

    if not entry or not entry.get("active"):
        return None, None, None, None, None, "Email not recognized as an active tenant."

    owner_user_id = entry.get("owner_user_id")
    tenant_id = entry.get("tenant_id")
    if not owner_user_id or not tenant_id:
        return None, None, None, None, None, "Tenant directory entry is missing owner_user_id or tenant_id."

    # 2) load owner’s rentals data
    tenants_path  = f"profiles/{owner_user_id}/rentals/tenants.json"
    props_path    = f"profiles/{owner_user_id}/rentals/properties.json"
    receipts_path = f"profiles/{owner_user_id}/rentals/receipts.json"

    tenants = current_app.gcs.read_json(tenants_path) or {}
    properties = current_app.gcs.read_json(props_path) or {}
    all_receipts = current_app.gcs.read_json(receipts_path) or {}

    tenant = tenants.get(tenant_id)
    if not tenant:
        return None, None, None, None, None, "Tenant record not found."

    # 3) filter receipts to tenant
    tenant_receipts = {
        rid: r for rid, r in all_receipts.items()
        if (r or {}).get("tenant_id") == tenant_id
    }

    # 4) coverage grid
    lease_months, coverage_map, coverage_grid = build_coverage_grid(tenant, tenant_receipts)

    return entry, tenant, properties, tenant_receipts, coverage_grid, None


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

def _money_to_cents(amount: float) -> int:
    return int(round(amount * 100))

def _cents_to_money(cents: int) -> float:
    return round(cents / 100.0, 2)

def _rent_amount_for_tenant(tenant: dict, properties: dict) -> float | None:
    prop_id = (tenant or {}).get("property_id")
    prop = (properties or {}).get(prop_id) or {}
    price = prop.get("price")
    try:
        return float(price) if price is not None else None
    except Exception:
        return None

def _next_due_month_ym(next_due: dt.date | None) -> str | None:
    if not next_due:
        return None
    return f"{next_due.year:04d}-{next_due.month:02d}"

def _payment_policy(now_local: dt.datetime, due_month_start: dt.datetime, rent: float) -> dict:
    """
    Rules:
    - If it's before the next month that needs to be paid => allow partial payments.
      (interpreted as: now < first day of due month)
    - If it's between the 1st and 5th => require full amount
    - If it's 6 days or later after the 1st => add late fee of 5% (and full required)
    """
    allow_partial = False
    require_full = True
    late_fee = 0.0

    if now_local < due_month_start:
        allow_partial = True
        require_full = False
    else:
        # we are in or after the due month
        if now_local.day <= 5:
            allow_partial = False
            require_full = True
        else:
            allow_partial = False
            require_full = True
            late_fee = round(rent * 0.05, 2)

    total_due = round(rent + late_fee, 2)
    return {
        "allow_partial": allow_partial,
        "require_full": require_full,
        "late_fee": late_fee,
        "total_due": total_due,
    }

def _app_base_url() -> str:
    base = (os.environ.get("APP_BASE_URL") or "").strip()
    if not base:
        raise RuntimeError("APP_BASE_URL is not set")
    if not (base.startswith("http://") or base.startswith("https://")):
        base = "https://" + base
    return base.rstrip("/")



# ---- routes ------------------------------------------------------

# =========================================================
# Tenant landing portal: view lease, receipts, coverage grid, and next payment due
# =========================================================

@bp.get("/tenant")
def tenant_portal():
    email = _current_tenant_email()
    if not email:
        flash("Please sign in.", "error")
        return redirect(url_for("auth.login_form", mode="tenant"))

    entry, tenant, properties, tenant_receipts, coverage_grid, err = _load_tenant_context_for_email(email)
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

# =========================================================
# Tenant payment portal: view and pay rent
# =========================================================

@bp.get("/tenant/pay")
def pay():
    email = _current_tenant_email()
    if not email:
        flash("Please sign in.", "error")
        return redirect(url_for("auth.login_form", mode="tenant"))

    entry, tenant, properties, tenant_receipts, coverage_grid, err = _load_tenant_context_for_email(email)
    if err:
        flash(err, "error")
        return redirect(url_for("auth.login_form", mode="tenant"))

    rent = _rent_amount_for_tenant(tenant, properties)
    if not rent or rent <= 0:
        flash("Rent amount is not configured for this property.", "error")
        return redirect(url_for("rental_tenant.tenant_portal"))

    next_due = compute_next_payment_due(tenant, tenant_receipts)
    due_ym = _next_due_month_ym(next_due)
    if not due_ym:
        flash("No payment due (lease may be fully covered or missing dates).", "info")
        return redirect(url_for("rental_tenant.tenant_portal"))

    due_month_start = dt.datetime(next_due.year, next_due.month, 1)
    now_local = dt.datetime.now()  # simplest for now; swap to a configured tz later if you want
    policy = _payment_policy(now_local, due_month_start, rent)

    return render_template(
        "rental_tenant/pay.html",
        tenant=tenant,
        properties=properties,
        next_payment_due=next_due,
        due_ym=due_ym,
        rent=rent,
        policy=policy,
    )


@bp.post("/tenant/pay/create-checkout-session")
def create_checkout_session():
    email = _current_tenant_email()
    if not email:
        flash("Please sign in.", "error")
        return redirect(url_for("auth.login_form", mode="tenant"))

    entry, tenant, properties, tenant_receipts, coverage_grid, err = _load_tenant_context_for_email(email)
    if err:
        flash(err, "error")
        return redirect(url_for("auth.login_form", mode="tenant"))

    rent = _rent_amount_for_tenant(tenant, properties)
    if not rent or rent <= 0:
        flash("Rent amount is not configured for this property.", "error")
        return redirect(url_for("rental_tenant.tenant_portal"))

    next_due = compute_next_payment_due(tenant, tenant_receipts)
    if not next_due:
        flash("No payment due.", "info")
        return redirect(url_for("rental_tenant.tenant_portal"))

    due_ym = _next_due_month_ym(next_due)
    due_month_start = dt.datetime(next_due.year, next_due.month, 1)
    now_local = dt.datetime.now()
    policy = _payment_policy(now_local, due_month_start, rent)

    # Amount selection:
    # - If allow_partial, read optional "amount" from form; else force full total_due
    amount = policy["total_due"]
    if policy["allow_partial"]:
        amt_raw = (request.form.get("amount") or "").strip()
        if amt_raw:
            try:
                amt = float(amt_raw)
            except Exception:
                amt = 0.0
            if amt <= 0:
                flash("Enter a valid payment amount.", "error")
                return redirect(url_for("rental_tenant.pay"))
            if amt > policy["total_due"]:
                flash(f"Amount can’t exceed ${policy['total_due']:.2f}.", "error")
                return redirect(url_for("rental_tenant.pay"))
            amount = round(amt, 2)

    if policy["require_full"] and abs(amount - policy["total_due"]) > 0.009:
        flash(f"Full payment of ${policy['total_due']:.2f} is required right now.", "error")
        return redirect(url_for("rental_tenant.pay"))

    # Stripe init
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    app_base = _app_base_url()

    owner_user_id = entry["owner_user_id"]
    tenant_id = entry["tenant_id"]
    property_id = (tenant or {}).get("property_id") or ""

    # Line items: rent (+ optional late fee) OR single "Rent payment" for partials
    line_items = []
    if policy["allow_partial"] and amount < policy["total_due"]:
        line_items = [{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": f"Rent payment (partial) — {due_ym}"},
                "unit_amount": _money_to_cents(amount),
            },
            "quantity": 1,
        }]
    else:
        line_items = [{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": f"Rent — {due_ym}"},
                "unit_amount": _money_to_cents(rent),
            },
            "quantity": 1,
        }]
        if policy["late_fee"] > 0:
            line_items.append({
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "Late fee (5%)"},
                    "unit_amount": _money_to_cents(policy["late_fee"]),
                },
                "quantity": 1,
            })

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
            "property_id": property_id,
            "covered_month": due_ym,
            "rent_cents": str(_money_to_cents(rent)),
            "late_fee_cents": str(_money_to_cents(policy["late_fee"])),
            "paid_cents": str(_money_to_cents(amount)),
            "policy_allow_partial": str(policy["allow_partial"]).lower(),
            "policy_require_full": str(policy["require_full"]).lower(),
        },
    )

    return redirect(checkout.url, code=303)


@bp.get("/tenant/pay/success")
def pay_success():
    flash("Payment submitted. It may take a moment to appear in receipts.", "success")
    return redirect(url_for("rental_tenant.tenant_portal"))

@bp.get("/tenant/pay/cancel")
def pay_cancel():
    flash("Payment canceled.", "info")
    return redirect(url_for("rental_tenant.tenant_portal"))

# =========================================================
# Tenant view and download receipts
# =========================================================

def _load_receipt_for_tenant(entry: dict, receipt_id: str):
    owner_user_id = entry.get("owner_user_id")
    tenant_id = entry.get("tenant_id")
    if not owner_user_id or not tenant_id:
        return None, "Missing tenant directory info."

    path = f"profiles/{owner_user_id}/rentals/receipts.json"
    receipts = current_app.gcs.read_json(path) or {}

    r = receipts.get(receipt_id)
    if not r:
        # fallback: search by embedded receipt_id
        for rid, rr in receipts.items():
            if isinstance(rr, dict) and rr.get("receipt_id") == receipt_id:
                r = rr
                receipt_id = rid
                break

    if not r or not isinstance(r, dict):
        return None, "Receipt not found."

    if (r.get("tenant_id") or "") != tenant_id:
        return None, "Receipt not found."

    # normalize id for templates
    r = dict(r)
    r.setdefault("receipt_id", receipt_id)
    return r, None



@bp.get("/tenant/receipts/<receipt_id>")
def receipt_view(receipt_id: str):
    email = _current_tenant_email()
    if not email:
        flash("Please sign in.", "error")
        return redirect(url_for("auth.login_form", mode="tenant"))

    entry, tenant, properties, tenant_receipts, coverage_grid, err = _load_tenant_context_for_email(email)
    if err:
        flash(err, "error")
        return redirect(url_for("auth.login_form", mode="tenant"))

    receipt, rerr = _load_receipt_for_tenant(entry, receipt_id)
    if rerr:
        flash(rerr, "error")
        return redirect(url_for("rental_tenant.tenant_portal"))

    return render_template(
        "rental_tenant/receipt_detail.html",
        tenant=tenant,
        properties=properties,
        receipt=receipt,
    )


@bp.get("/tenant/receipts/<receipt_id>/download")
def receipt_download(receipt_id: str):
    email = _current_tenant_email()
    if not email:
        flash("Please sign in.", "error")
        return redirect(url_for("auth.login_form", mode="tenant"))

    entry, tenant, properties, tenant_receipts, coverage_grid, err = _load_tenant_context_for_email(email)
    if err:
        flash(err, "error")
        return redirect(url_for("auth.login_form", mode="tenant"))

    receipt, rerr = _load_receipt_for_tenant(entry, receipt_id)
    if rerr:
        flash(rerr, "error")
        return redirect(url_for("rental_tenant.tenant_portal"))

    prop = (properties or {}).get((tenant or {}).get("property_id")) or {}

    html = render_template(
        "receipts/rent_receipt.html",
        renter_first=(tenant or {}).get("first_name", ""),
        renter_last=(tenant or {}).get("last_name", ""),
        renter_email=(tenant or {}).get("email", ""),
        rental_address=(prop or {}).get("address", "—"),
        date_paid=(receipt or {}).get("date_paid", "—"),
        month_covered=(receipt or {}).get("covered_month", "—"),
        amount_paid=f"${float((receipt or {}).get('amount') or 0):.2f}",
        payment_status=(receipt or {}).get("status", "—"),
        payment_method=(receipt or {}).get("payment_method", "—"),
        check_number=(receipt or {}).get("check_number"),
    )

    filename = f"rent_receipt_{(receipt or {}).get('covered_month','')}_{receipt_id}.html".replace("/", "_")
    return Response(
        html,
        headers={
            "Content-Type": "text/html; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
