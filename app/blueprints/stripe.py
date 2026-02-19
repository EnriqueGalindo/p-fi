# app/blueprints/stripe_webhook.py
import os
import time
import uuid
import datetime as dt

import stripe
from flask import Blueprint, request, current_app

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

    # Idempotency: record event.id as processed (so retries don’t duplicate receipts)
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

            # NEW: keep track of whether this was "half" or "full" (from your updated create_checkout_session)
            payment_kind = (md.get("payment_kind") or "").strip().lower()  # "half" | "full" | ""

            # --- cents resolution -------------------------------------------------
            # paid_cents should represent what THIS checkout session paid.
            # total_cents should represent what it takes to fully cover that month.
            #
            # Preferred:
            #   paid_cents  = metadata.paid_cents
            #   total_cents = metadata.total_cents
            #
            # Fallbacks:
            #   paid_cents  -> sess.amount_total
            #   total_cents -> same as paid_cents (treat as Paid in full)
            paid_cents = _int_or_zero(md.get("paid_cents"))
            if paid_cents <= 0:
                paid_cents = _int_or_zero(sess.get("amount_total"))

            total_cents = _int_or_zero(md.get("total_cents"))
            if total_cents <= 0:
                total_cents = paid_cents

            # NEW: put a receipt in a predictable bucket so "half" can update/replace instead of duplicating
            # We use Stripe client_reference_id if present (tenant_id:YYYY-MM), else fall back to tenant_id:covered_month.
            # NOTE: checkout.session.completed includes client_reference_id.
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
                # Decide status
                is_full = (total_cents > 0 and paid_cents >= total_cents)
                status = "Paid in full" if is_full else "Partial"

                # Load + upsert (so multiple payments for same tenant/month don’t spam receipts)
                receipts = _load_receipts(owner_user_id)

                # Try to find an existing receipt for this tenant + covered_month to update
                # (this is what makes "half then full" behave nicely)
                existing_id = None
                for rid, r in receipts.items():
                    if not isinstance(r, dict):
                        continue
                    if (r.get("tenant_id") == tenant_id) and (r.get("covered_month") == covered_month):
                        # Prefer updating Stripe-origin receipts (so manual uploads don’t get clobbered)
                        if (r.get("payment_method") or "") == "Stripe":
                            existing_id = rid
                            break

                now = int(time.time())
                receipt_id = existing_id or f"rcpt_{uuid.uuid4().hex[:10]}"

                # If updating an existing partial receipt, accumulate paid_cents (so half+half -> full)
                prev_paid_cents = 0
                if existing_id:
                    prev_paid_cents = _int_or_zero(((receipts.get(existing_id) or {}).get("stripe") or {}).get("paid_cents"))
                new_paid_cents_total = prev_paid_cents + paid_cents if existing_id else paid_cents

                # Recompute status based on accumulated payments
                is_full_total = (total_cents > 0 and new_paid_cents_total >= total_cents)
                status_total = "Paid in full" if is_full_total else "Partial"

                receipts[receipt_id] = {
                    "receipt_id": receipt_id,
                    "tenant_id": tenant_id,
                    "property_id": property_id,
                    "covered_month": covered_month,
                    "date_paid": _utcnow().date().isoformat(),
                    # amount stored reflects TOTAL paid toward this month if we are accumulating
                    "amount": round(new_paid_cents_total / 100.0, 2),
                    "payment_method": "Stripe",
                    "status": status_total,
                    "check_number": None,
                    "stripe": {
                        "event_id": event_id,
                        "checkout_session_id": sess.get("id"),
                        "payment_intent": sess.get("payment_intent"),
                        "client_reference_id": client_ref,
                        "payment_kind": payment_kind,  # "half"/"full" (nice for debugging/UI)
                        "paid_cents": new_paid_cents_total,  # TOTAL paid so far for this month
                        "total_cents": total_cents,
                        "last_payment_cents": paid_cents,    # amount from this checkout
                    },
                    "created_at": (receipts.get(receipt_id) or {}).get("created_at") or now,
                    "updated_at": now,
                }

                _save_receipts(owner_user_id, receipts)

    # Mark processed (after success)
    if event_id:
        current_app.gcs.write_json(
            f"webhooks/stripe/processed/{event_id}.json",
            {"event_id": event_id, "processed_at": _utcnow().isoformat() + "Z"},
        )

    return ("", 200)
