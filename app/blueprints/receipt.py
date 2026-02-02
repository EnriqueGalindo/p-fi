# app/blueprints/receipts.py
from flask import Blueprint, render_template, request, flash, redirect, url_for

from app.services.utils import send_rent_receipt  # where your send_email lives too

bp = Blueprint("receipts", __name__, url_prefix="/receipts")


@bp.get("/")
def index():
    # simple landing page (or redirect to new)
    return redirect(url_for("receipts.new_rent_receipt"))


@bp.route("/rent", methods=["GET", "POST"])
def new_rent_receipt():
    if request.method == "POST":
        renter_first = (request.form.get("renter_first") or "").strip()
        renter_last = (request.form.get("renter_last") or "").strip()
        renter_email = (request.form.get("renter_email") or "").strip()

        rental_address = (request.form.get("rental_address") or "").strip()
        date_paid = (request.form.get("date_paid") or "").strip()
        month_covered = (request.form.get("month_covered") or "").strip()

        amount_paid = (request.form.get("amount_paid") or "").strip()
        payment_status = (request.form.get("payment_status") or "").strip()
        payment_method = (request.form.get("payment_method") or "").strip()
        check_number = (request.form.get("check_number") or "").strip() or None

        # Basic validation (keep it minimal for now)
        missing = []
        for k, v in [
            ("First name", renter_first),
            ("Last name", renter_last),
            ("Renter email", renter_email),
            ("Rental address", rental_address),
            ("Date paid", date_paid),
            ("Month covered", month_covered),
            ("Amount paid", amount_paid),
            ("Payment method", payment_method),
            ("Payment status", payment_status),
        ]:
            if not v:
                missing.append(k)

        if missing:
            flash(f"Missing required fields: {', '.join(missing)}", "error")
            return render_template(
                "receipts/rent_form.html",
                form=request.form,
            )

        ok, err = send_rent_receipt(
            renter_first=renter_first,
            renter_last=renter_last,
            renter_email=renter_email,
            rental_address=rental_address,
            date_paid=date_paid,
            month_covered=month_covered,
            amount_paid=amount_paid,
            payment_status=payment_status,
            payment_method=payment_method,
            check_number=check_number,
        )

        if not ok:
            flash(err or "Failed to send receipt.", "error")
            return render_template("receipts/rent_form.html", form=request.form)

        flash("Receipt sent (tenant + records copy).", "success")
        return redirect(url_for("receipts.new_rent_receipt"))

    # GET
    return render_template("receipts/rent_form.html", form={})
