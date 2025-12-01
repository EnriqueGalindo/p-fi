# app/services/utils.py
import html
import resend
import json
import time
import logging
from typing import Any, Optional, Tuple
from google.cloud import storage
from google.api_core.exceptions import NotFound, Forbidden
from functools import wraps
from flask import session, redirect, url_for, current_app
import os
import requests
import datetime as dt

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
    mode = (os.getenv("EMAIL_MODE", "provider") or "provider").lower()
    api_key = os.getenv("RESEND_API_KEY", "")
    from_addr = from_addr or os.getenv("MAIL_FROM", "gmoney.me <login@gmoney.me>")

    # Normalize recipients
    if isinstance(to, str):
        to_list = [to]
    else:
        to_list = [x for x in (to or []) if x]

    if not to_list:
        return False, "Missing recipient"

    # Fallback body if neither provided
    if not (text or html):
        text = "(no body)"

    # Console/dev mode: just print and succeed
    if mode != "provider":
        print("\n[EMAIL:console]")
        print("From:", from_addr)
        print("To  :", ", ".join(to_list))
        print("Subj:", subject)
        if text:
            print("Text:", text)
        if html:
            preview = html if len(html) <= 400 else (html[:400] + "…")
            print("HTML:", preview)
        if reply_to:
            print("Reply-To:", reply_to)
        if tags:
            print("Tags:", list(tags))
        print("[/EMAIL]\n")
        return True, None

    if not api_key:
        return False, "RESEND_API_KEY not configured"

    try:
        resend.api_key = api_key

        # Resend supports: from, to, subject, html, text, reply_to, tags
        payload: dict = {
            "from": from_addr,
            "to": to_list,
            "subject": subject,
        }
        if text:
            payload["text"] = text
        if html:
            payload["html"] = html
        if reply_to:
            # Resend accepts str or list[str]
            payload["reply_to"] = reply_to
        if tags:
            # Accept either simple strings or full dicts.
            # If strings, convert to {"name": tag}. Add "value" if you prefer.
            normalized = []
            for t in tags:
                if isinstance(t, str):
                    normalized.append({"name": t})
                elif isinstance(t, dict):
                    normalized.append(t)
            if normalized:
                payload["tags"] = normalized

        result = resend.Emails.send(payload)  # raises on hard errors
        # Result shape typically has an "id" on success
        if not isinstance(result, dict) or not result.get("id"):
            return False, "Resend: no message ID returned"
        return True, None

    except Exception as e:
        # Keep this broad; Resend SDK can raise ResendError or generic exceptions
        return False, f"Resend error: {e}"
    
# ---------- Time helpers ----------
def now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def parse_iso(s: str) -> Optional[dt.datetime]:
    """Accept YYYY-MM-DD or full ISO; return naive UTC datetime or None."""
    if not s:
        return None
    s = s.strip()
    try:
        if "T" in s:
            if s.endswith("Z"):
                s = s[:-1]
            return dt.datetime.fromisoformat(s).replace(microsecond=0)
        return dt.datetime.fromisoformat(s + "T00:00:00").replace(microsecond=0)
    except Exception:
        return None

def parse_ymd(s: str) -> dt.datetime:
    """Strict YYYY-MM-DD (or ISO) → naive UTC datetime; raises on failure."""
    s = (s or "").strip()
    if not s:
        raise ValueError("empty")
    if "T" in s:
        if s.endswith("Z"):
            s = s[:-1]
        return dt.datetime.fromisoformat(s).replace(microsecond=0)
    return dt.datetime.fromisoformat(s + "T00:00:00").replace(microsecond=0)

# ---------- Window helpers ----------
def month_window(today_utc: Optional[dt.datetime] = None) -> Tuple[dt.datetime, dt.datetime]:
    """First of current month → first of next month."""
    if today_utc is None:
        today_utc = dt.datetime.utcnow().replace(microsecond=0)
    start = today_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end

def week_window(today_utc: Optional[dt.datetime] = None) -> Tuple[dt.datetime, dt.datetime]:
    """ISO week (Mon 00:00 → next Mon 00:00)."""
    if today_utc is None:
        today_utc = dt.datetime.utcnow().replace(microsecond=0)
    start = today_utc - dt.timedelta(days=today_utc.weekday())
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=7)
    return start, end

def year_window(today_utc: Optional[dt.datetime] = None) -> Tuple[dt.datetime, dt.datetime]:
    if today_utc is None:
        today_utc = dt.datetime.utcnow().replace(microsecond=0)
    start = today_utc.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1)
    return start, end

def period_bounds(now_utc: Optional[dt.datetime], period: str) -> Tuple[dt.datetime, dt.datetime]:
    """General fallback: week|month|year|all → [start, end)."""
    if now_utc is None:
        now_utc = dt.datetime.utcnow().replace(microsecond=0)
    p = (period or "month").lower()
    if p == "week":
        return week_window(now_utc)
    if p == "month":
        return month_window(now_utc)
    if p == "year":
        return year_window(now_utc)
    # all-time
    start = dt.datetime.min.replace(tzinfo=None)
    end   = dt.datetime.max.replace(tzinfo=None)
    return start, end

def window_from_strings(q_start: str, q_end: str
                        , fallback_today: Optional[dt.datetime] = None
                        ) -> Tuple[dt.datetime, dt.datetime]:
    """
    Convert ?start=YYYY-MM-DD&end=YYYY-MM-DD (or ISO) to [start, end).
    If invalid/missing, default to the current month.
    """
    if fallback_today is None:
        fallback_today = dt.datetime.utcnow().replace(microsecond=0)

    if q_start and q_end:
        try:
            start_dt = parse_ymd(q_start)
            end_dt   = parse_ymd(q_end)
            if end_dt <= start_dt:
                end_dt = start_dt + dt.timedelta(days=1)
            return start_dt, end_dt
        except Exception:
            pass
    return month_window(fallback_today)

def normalize_entry(user_id: str, entry: dict):
    idx_path = f"{user_prefix(user_id)}ledger/index.json"
    d_entry = {
        "id": entry.get("id"),
        "ts": entry.get("ts"),
        "kind": entry.get("kind"),
        "amount": entry.get("amount"),
        "from_account": entry.get("from_account"),
        "to_account": entry.get("to_account"),
        "debt_name": entry.get("debt_name"),
        "category": entry.get("category"),
        "note": entry.get("note"),
        # balance display helpers
        "balance_kind": entry.get("balance_kind"),
        "balance_name": entry.get("balance_name"),
        "balance_after": entry.get("balance_after"),
        "balance_name_from": entry.get("balance_name_from"),
        "balance_after_from": entry.get("balance_after_from"),
        "balance_name_to": entry.get("balance_name_to"),
        "balance_after_to": entry.get("balance_after_to"),
        "account_after": entry.get("account_after"),
    }
    return idx_path, d_entry

def get_valid_types(category: str) -> list[str]:
    """
    Return the valid_types list for a category in type_config.json.

    category: "account", "debt", or "costs".
    """
    cfg_path = current_app.config["TYPE_CONFIG_PATH"]      # e.g. "type_config.json"
    store = current_app.config_store                       # GcsStore for gmoneysys_admin

    cfg = store.read_json(cfg_path)

    if not cfg:
        # Fail loudly so we don't silently get an empty dropdown
        raise RuntimeError(f"type_config.json not found or empty at {cfg_path}")

    section = cfg.get(category)
    if not section:
        raise RuntimeError(f"Category {category!r} missing in type_config.json")

    types = section.get("valid_types", [])
    if not types:
        raise RuntimeError(f"No valid_types defined for {category!r} in type_config.json")

    return types
