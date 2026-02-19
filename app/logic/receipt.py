from flask import render_template
from decimal import Decimal, InvalidOperation
from ..services import utils

RECORDS_EMAIL = "egalindo@proton.me"

def _format_currency(amount) -> str:
    if amount is None:
        return ""
    try:
        dec = Decimal(str(amount)).quantize(Decimal("0.01"))
        return f"${dec:,.2f}"
    except (InvalidOperation, ValueError):
        return str(amount)

def normalize_payment_status(status: str | None, *, strict: bool = False) -> str:
    allowed = {
        "paid in full": "Paid in full",
        "partial payment": "Partial payment",
        "overpayment / credit": "Overpayment / credit",
        "late fee included": "Late fee included",
        "nsf / returned": "NSF / returned",
    }

    if not status or not str(status).strip():
        if strict:
            raise ValueError("payment_status is required (strict=True)")
        return "Paid in full"

    s = str(status).strip().lower()

    alias_map = {
        "paid": "Paid in full",
        "full": "Paid in full",
        "paid full": "Paid in full",
        "complete": "Paid in full",

        "partial": "Partial payment",
        "partially paid": "Partial payment",
        "paid in full": "Paid in full",

        "partial": "Partial payment",
        "partial payment": "Partial payment",

        "overpaid": "Overpayment / credit",
        "overpayment": "Overpayment / credit",
        "credit": "Overpayment / credit",

        "late": "Late fee included",
        "late fee": "Late fee included",

        "nsf": "NSF / returned",
        "returned": "NSF / returned",
        "bounced": "NSF / returned",
        "returned check": "NSF / returned",
        "bounced check": "NSF / returned",
    }

    if s in allowed:
        return allowed[s]
    if s in alias_map:
        return alias_map[s]

    # lightweight keyword fallback
    if "late" in s:
        return "Late fee included"
    if "partial" in s:
        return "Partial payment"
    if "credit" in s or "over" in s:
        return "Overpayment / credit"
    if "nsf" in s or "return" in s or "bounce" in s:
        return "NSF / returned"
    if "paid" in s or "full" in s:
        return "Paid in full"

    if strict:
        raise ValueError(f"Unknown payment_status '{status}'. Allowed: {list(allowed.values())}")

    return str(status).strip().title()

def send_rent_receipt(
    *,
    renter_first: str,
    renter_last: str,
    renter_email: str,
    rental_address: str,
    date_paid: str,
    month_covered: str,
    amount_paid,
    payment_status: str,
    payment_method: str,
    check_number: str | None = None,
):
    payment_status_norm = normalize_payment_status(payment_status, strict=False)
    amount_str = _format_currency(amount_paid)
    method = (payment_method or "").strip()

    subject = f"Rent Receipt — {month_covered}"

    html = render_template(
        "receipts/rent_receipt.html",
        renter_first=renter_first,
        renter_last=renter_last,
        renter_email=renter_email,
        rental_address=rental_address,
        date_paid=date_paid,
        month_covered=month_covered,
        amount_paid=amount_str,
        payment_status=payment_status_norm,
        payment_method=method,
        check_number=check_number,
    )

    text = (
        "Rent Receipt\n\n"
        f"Renter: {renter_first} {renter_last}\n"
        f"Renter Email: {renter_email}\n"
        f"Rental Address: {rental_address}\n"
        f"Date Paid: {date_paid}\n"
        f"Month Covered: {month_covered}\n"
        f"Amount Paid: {amount_str}\n"
        f"Payment Status: {payment_status_norm}\n"
        f"Payment Method: {method}\n"
        + (f"Check Number: {check_number or '(not provided)'}\n" if method.lower() == "check" else "")
    )

    ok1, err1 = utils.send_email(
        renter_email,
        subject,
        text=text,
        html=html,
        tags=["rent-receipt", "tenant"],
    )
    if not ok1:
        return False, f"Failed to send to renter: {err1}"

    ok2, err2 = utils.send_email(
        RECORDS_EMAIL,
        f"[COPY] {subject} — {renter_last}",
        text=text,
        html=html,
        tags=["rent-receipt", "records"],
    )
    if not ok2:
        return False, f"Sent to renter, but failed to send records copy: {err2}"

    return True, None
