# app/blueprints/mail.py
from flask import Blueprint, render_template, request, redirect, url_for, flash
from ..services.utils import send_email

bp = Blueprint("mail", __name__, url_prefix="/mail")

@bp.get("/test")
def test_form():
    return render_template("mail_test.html")

@bp.post("/test")
def test_send():
    to = (request.form.get("to") or "").strip()
    subject = (request.form.get("subject") or "Hello from gmoney.me").strip()
    body = (request.form.get("body") or "This is a test email.").strip()

    if not to or "@" not in to:
        flash("Enter a valid email address.", "error")
        return redirect(url_for("mail.test_form"))

    ok, err = send_email(to=to, subject=subject, text=body)
    if ok:
        flash("Email sent (or printed to console).", "success")
    else:
        flash(f"Failed to send email: {err}", "error")

    return redirect(url_for("mail.test_form"))
