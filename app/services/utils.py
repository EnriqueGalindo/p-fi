# app/services/utils.py
import html
import json
import time
import logging
from typing import Any, Optional, Tuple
from google.cloud import storage
from google.api_core.exceptions import NotFound, Forbidden
from functools import wraps
from flask import session, redirect, url_for
import os
import requests

# very small in-process TTL cache
_cache: dict[Tuple[str, str], tuple[float, Any]] = {}

import hashlib
import base64
from typing import Tuple

def canonicalize_email(email: str) -> str:
    """
    Lower-case, trim, and (optionally) apply Gmail-style '+' stripping.
    Keep it conservative unless you only use Gmail.
    """
    e = (email or "").strip().lower()
    # Optional aggressive canonicalization for Gmail:
    # local, sep, domain = e.partition("@")
    # if domain in ("gmail.com", "googlemail.com"):
    #     local = local.split("+", 1)[0].replace(".", "")
    # e = f"{local}@{domain}"
    return e

def user_id_for_email(email: str, length: int = 20) -> str:
    """
    Return a URL-safe short id. We hash the canonical email so your GCS paths
    don't expose emails directly.
    """
    c = canonicalize_email(email)
    digest = hashlib.sha256(c.encode("utf-8")).digest()
    # Base32 is URL-safe and case-insensitive. Strip padding and shorten.
    b32 = base64.b32encode(digest).decode("ascii").rstrip("=")
    return b32[:length].lower()

from flask import session, abort

def current_user_identity() -> Tuple[str, str]:
    """
    Returns (user_email, user_id) from the session or 401 if not logged in.
    """
    email = session.get("user_email")
    uid   = session.get("user_id")
    if not email or not uid:
        abort(401)
    return email, uid

def user_prefix(user_id: str) -> str:
    # All your user data lives under this prefix
    return f"profiles/{user_id}/"


def get_json_from_gcs(
    bucket: str,
    path: str,
    default: Any = None,
    *,
    ttl: int = 0,
    client: Optional[storage.Client] = None,
) -> Any:
    """
    Read a JSON object from GCS: gs://<bucket>/<path>
    - Returns `default` if the object doesn't exist or can't be parsed.
    - Optional TTL (seconds) to cache reads in-process.
    - You may pass an existing google.cloud.storage.Client via `client`.
    """
    key = (bucket, path)
    now = time.time()

    if ttl and key in _cache:
        exp, val = _cache[key]
        if now < exp:
            return val

    if client is None:
        client = storage.Client()

    try:
        blob = client.bucket(bucket).blob(path)
        text = blob.download_as_text()  # utf-8
        val = json.loads(text)
    except NotFound:
        return default
    except Forbidden:
        logging.exception("Forbidden reading gs://%s/%s", bucket, path)
        raise
    except Exception:
        logging.exception("Failed reading/parsing gs://%s/%s", bucket, path)
        return default

    if ttl:
        _cache[key] = (now + ttl, val)
    return val

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_email"):
            return redirect(url_for("auth.login_form"))
        return fn(*args, **kwargs)
    return wrapper

def send_email(to, subject
               , text=None, html=None
               , *
               , from_addr=None, reply_to=None
               , tags=None):
    mode = os.getenv("EMAIL_MODE", "provider").lower()
    api_key = os.getenv("RESEND_API_KEY", "")
    from_addr = from_addr or os.getenv("MAIL_FROM", "gmoney.me <login@gmoney.me>")

    if isinstance(to, str):
        to = [to]

    if not (text or html):
        text = "(no body)"

    print(mode)
    if mode != "provider":
        # Dev: just print
        print("\n[EMAIL:console]")
        print("From:", from_addr)
        print("To  :", ", ".join(to))
        print("Subj:", subject)
        print("Text:", text or "")
        if html:
            print("HTML:", html[:400] + ("..." if len(html) > 400 else ""))
        print("[/EMAIL]\n")
        return True, None

    if not api_key:
        return False, "RESEND_API_KEY missing and EMAIL_MODE=provider"

    payload = {
        "from": from_addr,
        "to": to,
        "subject": subject,
    }
    if text:
        payload["text"] = text
    if html:
        payload["html"] = html
    if reply_to:
        payload["reply_to"] = reply_to
    if tags:
        payload["tags"] = [{"name": t} for t in tags]

    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
        if r.status_code >= 400:
            return False, f"Resend error {r.status_code}: {r.text}"
        return True, None
    except requests.RequestException as e:
        return False, str(e)