# app/blueprints/auth.py
import os
import hmac
import json
import time
import secrets
import hashlib
import datetime as dt
from urllib.parse import urlencode

from flask import (
    Blueprint,
    current_app,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
)

from ..services.utils import (
    canonicalize_email,
    user_id_for_email,
    send_email,
    tenant_directory_path,
    tenant_email_key
)

bp = Blueprint("auth", __name__)

# --- Config ---
APP_BASE_URL       = os.getenv("APP_BASE_URL", "http://localhost:8080")
MAGIC_TOKEN_SECRET = os.getenv("MAGIC_TOKEN_SECRET", "dev-secret")
MAIL_FROM          = os.getenv("MAIL_FROM", "gmoney.me <login@gmoney.me>")

# =============================================================================
# Storage helpers (GLOBAL pending tokens; no session dependency)
# =============================================================================

def _pending_path(tid: str) -> str:
    return f"auth/pending/{tid}.json"

def _put_json(path: str, obj: dict):
    return current_app.gcs.write_json(path, obj)

def _get_json(path: str):
    return current_app.gcs.read_json(path)

# =============================================================================
# Token helpers
# =============================================================================

def _sign(payload: str) -> str:
    return hmac.new(MAGIC_TOKEN_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()

def create_magic_token(email: str, ttl_secs: int = 900) -> str:
    tid = secrets.token_urlsafe(16)
    exp = int(time.time()) + int(ttl_secs)
    payload = json.dumps({"tid": tid, "email": email, "exp": exp}, separators=(",", ":"))
    sig = _sign(payload)
    token = f"{payload}.{sig}"
    _put_json(_pending_path(tid), {"email": email, "exp": exp, "used": False})
    return token

def parse_and_validate(token: str):
    try:
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(payload)):
            return None, "bad-signature"

        data = json.loads(payload)
        now = int(time.time())
        if now > int(data["exp"]):
            return None, "expired"

        rec = _get_json(_pending_path(data["tid"])) or {}
        if not rec:
            return None, "missing"
        if rec.get("used"):
            return None, "already-used"
        if rec.get("email") != data["email"]:
            return None, "email-mismatch"

        return data, None
    except Exception:
        return None, "invalid-token"

def mark_used(tid: str):
    rec_path = _pending_path(tid)
    rec = _get_json(rec_path) or {}
    rec["used"] = True
    rec["used_at"] = int(time.time())
    _put_json(rec_path, rec)

# =============================================================================
# Email sender (via services.utils.send_email)
# =============================================================================

def send_login_link(to_email: str, token: str) -> bool:
    link = f"{APP_BASE_URL}/auth/magic?{urlencode({'token': token})}"
    subject = "Your gmoney.me sign-in link"
    text = f"Click to sign in (valid 15 minutes): {link}"
    html = f"""\
<p>Click to sign in (valid 15 minutes):</p>
<p><a href="{link}">{link}</a></p>
"""

    ok, err = send_email(
        to_email,
        subject,
        text=text,
        html=html,
        from_addr=MAIL_FROM,              # default can also come from env in send_email
        tags=["magic-link", "login"],
    )
    if not ok:
        raise RuntimeError(err or "Failed to send email")
    return True

# =============================================================================
# Views
# =============================================================================

@bp.get("/login")
def login_form():
    # Default tenant, allow switch via ?mode=owner
    mode = (request.args.get("mode") or "tenant").strip().lower()
    if mode not in ("tenant", "owner"):
        mode = "tenant"

    return render_template("login.html", mode=mode)




@bp.post("/login")
def login_submit():
    mode = (request.form.get("mode") or request.args.get("mode") or "tenant").strip().lower()
    if mode not in ("tenant", "owner"):
        mode = "tenant"

    email = (request.form.get("email") or "").strip().lower()
    if not email or "@" not in email:
        flash("Enter a valid email.", "error")
        return redirect(url_for("auth.login_form", mode=mode))

    # ----------------------------
    # TENANT MODE (public, no owner session needed)
    # ----------------------------
    if mode == "tenant":
        rec = current_app.config_store.read_json(tenant_directory_path(email)) or {}

        if not rec or rec.get("active") is not True:
            # Optional: add tiny delay to reduce email-enumeration
            time.sleep(0.35)
            flash("Email not found. Ask your landlord to add you as a tenant.", "error")
            return redirect(url_for("auth.login_form", mode="tenant"))

        # Create token and send link (we’ll add owner_user_id/tenant_id to pending token next)
        token = create_magic_token(email, ttl_secs=900)
        try:
            send_login_link(email, token)
        except Exception as e:
            current_app.logger.exception("Failed to send tenant login email")
            flash(f"Could not send email: {e}", "error")
            return redirect(url_for("auth.login_form", mode="tenant"))

        flash("We sent you a sign-in link. Please check your email.", "success")
        return redirect(url_for("auth.login_form", mode="tenant"))

    # ----------------------------
    # OWNER MODE (your original behavior)
    # ----------------------------
    token = create_magic_token(email, ttl_secs=900)
    try:
        send_login_link(email, token)
    except Exception as e:
        current_app.logger.exception("Failed to send login email")
        flash(f"Could not send email: {e}", "error")
        return redirect(url_for("auth.login_form", mode="owner"))

    flash("We sent you a sign-in link. Please check your email.", "success")
    return redirect(url_for("auth.login_form", mode="owner"))

@bp.get("/auth/magic")
def magic():
    token = request.args.get("token", "")
    data, err = parse_and_validate(token)
    if err:
        flash(f"Sign-in link invalid: {err}", "error")
        return redirect(url_for("auth.login_form"))

    # Mark single-use
    mark_used(data["tid"])

    # Create session + map email -> user_id
    email = canonicalize_email(data["email"])
    uid   = user_id_for_email(email)

    session.clear()
    session["user_email"] = email
    session["user_id"]    = uid
    session["auth_at"]    = int(time.time())

    # First-login scaffold + simple users registry
    pref = f"profiles/{uid}/"
    meta_path = f"{pref}meta.json"
    meta = current_app.gcs.read_json(meta_path)
    if not meta:
        meta = {
            "email": email,
            "created_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "version": 1,
        }
        current_app.gcs.write_json(meta_path, meta)

        # Optional global registry for admin tools
        users_root = "users"
        current_app.gcs.write_json(
            f"{users_root}/{uid}.json",
            {"user_id": uid, "email": email, "created_at": meta["created_at"]},
        )
        ekey = hashlib.sha256(email.encode()).hexdigest()[:16]
        current_app.gcs.write_json(
            f"{users_root}/by_email/{ekey}.json",
            {"user_id": uid, "email": email},
        )

    return redirect(url_for("plan.view_plan"))

@bp.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login_form"))
