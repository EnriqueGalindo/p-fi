# app/blueprints/stripe_webhook.py
import os
import time
import uuid
import datetime as dt
import stripe
from flask import Blueprint, request, current_app, abort

bp = Blueprint("stripe_webhook", __name__)

def _utcnow():
    return dt.datetime.utcnow().replace(microsecond=0)

def _load_receipts(owner_user_id: str) -> dict:
    path = f"profiles/{owner_user_id}/rentals/receipts.json"
    return current_app.gcs.read_json(path) or {}

def _save_receipts(owner_user_id: str, receipts: dict) -> None:
    path = f"profiles/{owner_user_id}/rentals/receipts.json"
    current_app.gcs.write_json(path, receipts)

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
        if md.get("kind") == "rent":
            owner_user_id = md.get("owner_user_id")
            tenant_id = md.get("tenant_id")
            property_id = md.get("property_id")
            covered_month = md.get("covered_month")
            total_cents = int(sess.get("amount_total") or 0)
            if total_cents <= 0:
                total_cents = int(md.get("paid_cents") or "0")

            if owner_user_id and tenant_id and covered_month and total_cents > 0:
                receipts = _load_receipts(owner_user_id)

                receipt_id = f"rcpt_{uuid.uuid4().hex[:10]}"
                now = int(time.time())

                receipts[receipt_id] = {
                    "receipt_id": receipt_id,
                    "tenant_id": tenant_id,
                    "property_id": property_id,
                    "covered_month": covered_month,
                    "date_paid": _utcnow().date().isoformat(),
                    "amount": round(total_cents / 100.0, 2),
                    "payment_method": "Stripe",
                    "status": "Paid in full",  # MVP: treat as full; refine when partials exist
                    "check_number": None,
                    "stripe": {
                        "event_id": event_id,
                        "checkout_session_id": sess.get("id"),
                        "payment_intent": sess.get("payment_intent"),
                    },
                    "created_at": now,
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
