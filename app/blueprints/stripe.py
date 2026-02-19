# app/blueprints/stripe.py  (DROP-IN replacement for your webhook handler)
import os
import time
import uuid
import datetime as dt

import stripe
from flask import Blueprint, request, current_app

from ..logic import receipt as receipt_logic 
bp = Blueprint("stripe_webhook", __name__)


def _utcnow() -> dt.datetime:
    return dt.datetime.utcnow().replace(microsecond=0)


def _load_receipts(owner_user_id: str) -> dict:
    path = f"profiles/{owner_user_id}/rentals/receipts.json"
    return current_app.gcs.read_json(path) or {}


def _save_receipts(owner_user_id: str, receipts: dict) -> None:
    path = f"profiles/{owner_user_id}/rentals/receipts.json"
    current_app.gcs.write_json(path, receipts)


def _int_or_zero(v) -> int:
    try:
        if v is None:
            return 0
        if isinstance(v, bool):
            return 0
        return int(v)
    except Exception:
        try:
            return int(str(v).strip())
        except Exception:
            return 0


def _cents_to_money_str(cents: int) -> str:
    # send_rent_receipt currently expects strings (per your manual form usage)
    return f"{(max(0, cents) / 100.0):.2f}"


def _load_tenants(owner_user_id: str) -> dict:
    # If your app uses a different path, change it here.
    # This is the most common pattern with your receipts pathing.
    path = f"profiles/{owner_user_id}/rentals/tenants.json"
    return current_app.gcs.read_json(path) or {}


def _load_properties(owner_user_id: str) -> dict:
    # If your app uses a different path, change it here.
    path = f"profiles/{owner_user_id}/rentals/properties.json"
    return current_app.gcs.read_json(path) or {}


def _find_tenant_record(tenants: dict, tenant_id: str) -> dict | None:
    if not tenant_id:
        return None

    # Common shapes:
    # 1) tenants is dict keyed by tenant_id -> tenant dict
    t = tenants.get(tenant_id)
    if isinstance(t, dict):
        return t

    # 2) tenants is list-like under "tenants"
    arr = tenants.get("tenants") if isinstance(tenants, dict) else None
    if isinstance(arr, list):
        for item in arr:
            if isinstance(item, dict) and (item.get("tenant_id") == tenant_id or item.get("id") == tenant_id):
                return item

    # 3) tenants is itself a list
    if isinstance(tenants, list):
        for item in tenants:
            if isinstance(item, dict) and (item.get("tenant_id") == tenant_id or item.get("id") == tenant_id):
                return item

    return None


def _find_property_address(properties: dict, property_id: str) -> str:
    if not property_id:
        return ""

    p = properties.get(property_id)
    if isinstance(p, dict):
        # your UI uses prop.address
        return (p.get("address") or "").strip()

    arr = properties.get("properties") if isinstance(properties, dict) else None
    if isinstance(arr, list):
        for item in arr:
            if isinstance(item, dict) and (item.get("property_id") == property_id or item.get("id") == property_id):
                return (item.get("address") or "").strip()

    if isinstance(properties, list):
        for item in properties:
            if isinstance(item, dict) and (item.get("property_id") == property_id or item.get("id") == property_id):
                return (item.get("address") or "").strip()

    return ""


@bp.post("/stripe/webhook")
def stripe_webhook():
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    whsec = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not whsec:
        current_app.logger.error("Missing STRIPE_WEBHOOK_SECRET")
        return ("", 500)

    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, whsec)
    except Exception as e:
        current_app.logger.warning(f"Stripe webhook signature verification failed: {e}")
        return ("", 400)

    # Idempotency by event id: also covers "email immediately after payment"
    event_id = event.get("id")
    if event_id:
        marker_path = f"webhooks/stripe/processed/{event_id}.json"
        if current_app.gcs.read_json(marker_path):
            return ("", 200)

    etype = event.get("type")

    if etype == "checkout.session.completed":
        sess = event["data"]["object"]
        md = (sess.get("metadata") or {})

        if (md.get("kind") or "") == "rent":
            owner_user_id = (md.get("owner_user_id") or "").strip()
            tenant_id = (md.get("tenant_id") or "").strip()
            property_id = (md.get("property_id") or "").strip() or None
            covered_month = (md.get("covered_month") or "").strip()

            payment_kind = (md.get("payment_kind") or "").strip().lower()  # "half" | "full" | ""

            paid_cents = _int_or_zero(md.get("paid_cents"))
            if paid_cents <= 0:
                paid_cents = _int_or_zero(sess.get("amount_total"))

            total_cents = _int_or_zero(md.get("total_cents"))
            if total_cents <= 0:
                total_cents = paid_cents

            client_ref = (sess.get("client_reference_id") or "").strip()
            if not client_ref:
                client_ref = f"{tenant_id}:{covered_month}" if (tenant_id and covered_month) else ""

            if not (owner_user_id and tenant_id and covered_month and paid_cents > 0):
                current_app.logger.warning(
                    "Stripe rent webhook missing required fields: "
                    f"owner_user_id={owner_user_id!r}, tenant_id={tenant_id!r}, "
                    f"covered_month={covered_month!r}, paid_cents={paid_cents}"
                )
            else:
                receipts = _load_receipts(owner_user_id)

                # Upsert: prefer updating existing Stripe receipt for same tenant/month
                existing_id = None
                for rid, r in receipts.items():
                    if not isinstance(r, dict):
                        continue
                    if (r.get("tenant_id") == tenant_id) and (r.get("covered_month") == covered_month):
                        if (r.get("payment_method") or "") == "Stripe":
                            existing_id = rid
                            break

                now = int(time.time())
                receipt_id = existing_id or f"rcpt_{uuid.uuid4().hex[:10]}"

                prev_paid_cents = 0
                if existing_id:
                    prev_paid_cents = _int_or_zero(((receipts.get(existing_id) or {}).get("stripe") or {}).get("paid_cents"))
                new_paid_cents_total = prev_paid_cents + paid_cents if existing_id else paid_cents

                is_full_total = (total_cents > 0 and new_paid_cents_total >= total_cents)
                status_total = "Paid in full" if is_full_total else "Partial"

                # Keep last email attempt in receipt["email"], and keep a history list too
                prev_email = (receipts.get(receipt_id) or {}).get("email") or {"sent": False, "sent_at": None, "error": None}
                email_history = (receipts.get(receipt_id) or {}).get("email_history")
                if not isinstance(email_history, list):
                    email_history = []

                # Write receipt first (so even if email fails, you still have the receipt)
                receipts[receipt_id] = {
                    "receipt_id": receipt_id,
                    "tenant_id": tenant_id,
                    "property_id": property_id,
                    "covered_month": covered_month,
                    "date_paid": _utcnow().date().isoformat(),
                    # amount stored reflects TOTAL paid toward this month (accumulating)
                    "amount": round(new_paid_cents_total / 100.0, 2),
                    "payment_method": "Stripe",
                    "status": status_total,
                    "check_number": None,
                    "email": prev_email,
                    "email_history": email_history,
                    "stripe": {
                        "event_id": event_id,
                        "checkout_session_id": sess.get("id"),
                        "payment_intent": sess.get("payment_intent"),
                        "client_reference_id": client_ref,
                        "payment_kind": payment_kind,
                        "paid_cents": new_paid_cents_total,     # TOTAL paid so far
                        "total_cents": total_cents,
                        "last_payment_cents": paid_cents,       # THIS payment
                    },
                    "created_at": (receipts.get(receipt_id) or {}).get("created_at") or now,
                    "updated_at": now,
                }
                _save_receipts(owner_user_id, receipts)

                # ---- Send email immediately after payment (half or full) ----
                # Uses tenant + property info to populate your existing send_rent_receipt API.
                sent_ok = False
                sent_err = None
                sent_at = None

                try:
                    tenants = _load_tenants(owner_user_id)
                    props = _load_properties(owner_user_id)
                    t = _find_tenant_record(tenants, tenant_id) or {}

                    renter_first = (t.get("first_name") or "").strip()
                    renter_last = (t.get("last_name") or "").strip()
                    renter_email = (t.get("email") or "").strip()

                    rental_address = _find_property_address(props, property_id or "") or ""

                    # What to show in the email:
                    # - Amount paid: THIS payment amount (so you get an email after each payment)
                    # - Payment status: "Partial" or "Paid in full" based on accumulated total
                    amount_paid_str = _cents_to_money_str(paid_cents)
                    payment_status = status_total  # status after applying this payment

                    # (Optional) If you prefer the email to show TOTAL paid so far instead:
                    # amount_paid_str = _cents_to_money_str(new_paid_cents_total)

                    if not (renter_first and renter_last and renter_email and rental_address):
                        missing = []
                        if not renter_first: missing.append("tenant.first_name")
                        if not renter_last: missing.append("tenant.last_name")
                        if not renter_email: missing.append("tenant.email")
                        if not rental_address: missing.append("property.address")
                        raise RuntimeError(f"Cannot send receipt email; missing: {', '.join(missing)}")

                    ok, err = receipt_logic.send_rent_receipt(
                        renter_first=renter_first,
                        renter_last=renter_last,
                        renter_email=renter_email,
                        rental_address=rental_address,
                        date_paid=_utcnow().date().isoformat(),
                        month_covered=covered_month,
                        amount_paid=amount_paid_str,
                        payment_status=payment_status,
                        payment_method="Stripe",
                        check_number=None,
                    )
                    sent_ok = bool(ok)
                    sent_err = err if not ok else None
                    sent_at = _utcnow().isoformat() + "Z"

                except Exception as e:
                    sent_ok = False
                    sent_err = str(e)
                    sent_at = _utcnow().isoformat() + "Z"
                    current_app.logger.exception(f"Failed sending Stripe receipt email: {e}")

                # Persist email result (last attempt + history entry)
                try:
                    receipts = _load_receipts(owner_user_id)
                    r = receipts.get(receipt_id) if isinstance(receipts, dict) else None
                    if isinstance(r, dict):
                        r["email"] = {
                            "sent": sent_ok,
                            "sent_at": sent_at,
                            "error": sent_err,
                            "event_id": event_id,
                            "checkout_session_id": sess.get("id"),
                        }
                        hist = r.get("email_history")
                        if not isinstance(hist, list):
                            hist = []
                        hist.append({
                            "sent": sent_ok,
                            "sent_at": sent_at,
                            "error": sent_err,
                            "event_id": event_id,
                            "checkout_session_id": sess.get("id"),
                            "covered_month": covered_month,
                            "last_payment_cents": paid_cents,
                            "status_after_payment": status_total,
                        })
                        r["email_history"] = hist
                        r["updated_at"] = int(time.time())
                        receipts[receipt_id] = r
                        _save_receipts(owner_user_id, receipts)
                except Exception as e:
                    current_app.logger.exception(f"Failed persisting email status on receipt {receipt_id}: {e}")

    # Mark processed (after success)
    if event_id:
        current_app.gcs.write_json(
            f"webhooks/stripe/processed/{event_id}.json",
            {"event_id": event_id, "processed_at": _utcnow().isoformat() + "Z"},
        )

    return ("", 200)