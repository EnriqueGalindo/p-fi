# app/blueprints/auth.py
import os, hmac, hashlib, time, json, secrets
from urllib.parse import urlencode
from flask import Blueprint, current_app, render_template, request, redirect, url_for, session, abort, flash
from ..services.utils import canonicalize_email, user_id_for_email
import datetime as dt
import hashlib

bp = Blueprint("auth", __name__)

APP_BASE_URL      = os.getenv("APP_BASE_URL", "http://localhost:8080")
EMAIL_MODE        = os.getenv("EMAIL_MODE", "console")  # console | provider
MAGIC_TOKEN_SECRET= os.getenv("MAGIC_TOKEN_SECRET", "dev-secret")
RESEND_API_KEY    = os.getenv("RESEND_API_KEY", "")

# ---------- Storage helpers (GCS) ----------
def _auth_prefix():
    uid = session.get("user_id")       
    if not uid:
        abort(401)
    return f"profiles/{uid}/auth/"

def _put_json(path, obj):
    return current_app.gcs.write_json(path, obj)

def _get_json(path):
    return current_app.gcs.read_json(path)

# ---------- Token helpers ----------
def _sign(payload: str) -> str:
    return hmac.new(MAGIC_TOKEN_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()

def create_magic_token(email: str, ttl_secs: int = 900) -> str:
    tid = secrets.token_urlsafe(16)                # token id (non-secret)
    exp = int(time.time()) + ttl_secs
    payload = json.dumps({"tid": tid, "email": email, "exp": exp}, separators=(",", ":"))
    sig = _sign(payload)
    token = f"{payload}.{sig}"                     # opaque token sent to user
    # store server-side record for single-use + auditing
    _put_json(f"{_auth_prefix()}pending/{tid}.json", {"email": email, "exp": exp, "used": False})
    return token

def parse_and_validate(token: str):
    try:
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(payload)):
            return None, "bad-signature"
        data = json.loads(payload)
        if int(time.time()) > int(data["exp"]):
            return None, "expired"
        rec = _get_json(f"{_auth_prefix()}pending/{data['tid']}.json") or {}
        if not rec or rec.get("used"):
            return None, "already-used-or-missing"
        if rec.get("email") != data["email"]:
            return None, "email-mismatch"
        return data, None
    except Exception:
        return None, "invalid-token"

def mark_used(tid: str):
    rec_path = f"{_auth_prefix()}pending/{tid}.json"
    rec = _get_json(rec_path) or {}
    rec["used"] = True
    rec["used_at"] = int(time.time())
    _put_json(rec_path, rec)

# ---------- Email sender ----------
def send_login_link(to_email: str, token: str):
    link = f"{APP_BASE_URL}/auth/magic?{urlencode({'token': token})}"
    subject = "Your gmoney.me sign-in link"
    text = f"Click to sign in (valid 15 minutes): {link}"

    if EMAIL_MODE == "console":
        print(f"[DEV] Login for {to_email}: {link}")
        return True

    import requests
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={
            "from": "gmoney.me <login@gmoney.me>",
            "to": [to_email],
            "subject": subject,
            "text": text,
        },
        timeout=10,
    )
    r.raise_for_status()
    return True

# ---------- Views ----------
@bp.get("/login")
def login_form():
    return render_template("login.html")

@bp.post("/login")
def login_submit():
    email = (request.form.get("email") or "").strip().lower()
    if not email or "@" not in email:
        flash("Enter a valid email.", "error")
        return redirect(url_for("auth.login_form"))

    # Simple rate-limit per email (optional)
    # (skip here for brevity; you can add a small counter in GCS)
    token = create_magic_token(email, ttl_secs=900)
    send_login_link(email, token)
    flash("We sent you a sign-in link. Please check your email.", "success")
    return redirect(url_for("auth.login_form"))

@bp.get("/auth/magic")
def magic():
    token = request.args.get("token", "")
    data, err = parse_and_validate(token)
    if err:
        flash(f"Sign-in link invalid: {err}", "error")
        return redirect(url_for("auth.login_form"))

    # Single-use enforcement
    mark_used(data["tid"])

    # Create session
    email = canonicalize_email(data["email"])
    uid   = user_id_for_email(email)

    session.clear()
    session["user_email"] = email
    session["user_id"]    = uid
    session["auth_at"]    = int(time.time())

    # ---------- NEW: first-login scaffold + simple users registry ----------
    pref = f"profiles/{uid}/"  # -> "profiles/{uid}/"
    meta_path = f"{pref}meta.json"
    meta = current_app.gcs.read_json(meta_path)
    if not meta:
        meta = {
            "email": email,
            "created_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "version": 1,
        }
        current_app.gcs.write_json(meta_path, meta)

        # Optional global registry (handy for admin tools)
        # Keep a lightweight master list and a by-email index
        users_root = "users"
        # Master list entry
        current_app.gcs.write_json(
            f"{users_root}/{uid}.json",
            {"user_id": uid, "email": email, "created_at": meta["created_at"]}
        )
        # By-email index (use a short hash for path safety)
        ekey = hashlib.sha256(email.encode()).hexdigest()[:16]
        current_app.gcs.write_json(
            f"{users_root}/by_email/{ekey}.json",
            {"user_id": uid, "email": email}
        )
    # ----------------------------------------------------------------------

    return redirect(url_for("plan.view_plan"))