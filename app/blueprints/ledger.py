# app/blueprints/ledger.py
from flask import Blueprint, current_app, render_template, request, redirect, url_for, abort
import datetime as dt
from ..logic.ledger import apply_transaction, reverse_transaction
from ..logic.ledger_stats import compute_ledger_stats
from flask import session, redirect, url_for
from ..services.utils import current_user_identity, user_prefix



bp = Blueprint("ledger", __name__, url_prefix="/ledger")

# @bp.before_request
# def require_login_for_blueprint():
#     if not session.get("user_email"):
#         return redirect(url_for("auth.login_form"))

def _prefix(user_id: str) -> str:
    return f"profiles/{user_id}/"

def _now_iso():
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def _append_index(user_id: str, entry: dict):
    store = current_app.gcs
    idx_path = f"{_prefix(user_id)}ledger/index.json"
    idx = store.read_json(idx_path) or []
    idx.append({
        "id": entry.get("id"),
        "ts": entry.get("ts"),
        "kind": entry.get("kind"),
        "amount": entry.get("amount"),
        "from_account": entry.get("from_account"),
        "to_account": entry.get("to_account"),
        "debt_name": entry.get("debt_name"),
        "category": entry.get("category"),
        "note": entry.get("note"),
        # NEW balance display helpers
        "balance_kind": entry.get("balance_kind"),
        "balance_name": entry.get("balance_name"),
        "balance_after": entry.get("balance_after"),
        "balance_name_from": entry.get("balance_name_from"),
        "balance_after_from": entry.get("balance_after_from"),
        "balance_name_to": entry.get("balance_name_to"),
        "balance_after_to": entry.get("balance_after_to"),
        "account_after": entry.get("account_after"),
    })
    store.write_json(idx_path, idx)


@bp.get("/")
def list_entries():
    _, user_id = current_user_identity()
    pref = user_prefix(user_id)
    latest = current_app.gcs.read_json(f"{pref}latest.json") or {}
    store   = current_app.gcs
    index   = store.read_json(f"{_prefix(user_id)}ledger/index.json") or []
    index   = sorted(index, key=lambda x: x.get("ts",""), reverse=True)[:100]

    period = (request.args.get("period") or "month").lower()  # week|month|year|all
    stats = compute_ledger_stats(store, user_id, period)

    return render_template("ledger_list.html",
                           profile=latest,
                           ledger=index,
                           stats=stats)

@bp.get("/new")
def new_entry_form():
    _, user_id = current_user_identity()
    pref = user_prefix(user_id)
    latest = current_app.gcs.read_json(f"{pref}latest.json") or {}
    accounts = latest.get("accounts", []) or []
    debts    = latest.get("debts", []) or []
    today = dt.datetime.utcnow().date().isoformat()  # YYYY-MM-DD
    return render_template("ledger_new.html", accounts=accounts, debts=debts, today=today)

@bp.post("/new")
def create_entry():
    _, user_id = current_user_identity()
    pref = user_prefix(user_id)
    latest = current_app.gcs.read_json(f"{pref}latest.json") or {}
    store   = current_app.gcs

    # basic fields
    kind   = (request.form.get("kind") or "").lower()
    amount = float(request.form.get("amount") or 0)
    note   = (request.form.get("note") or "").strip()

    # optional date coming from the form (hidden canon_date)
    ts_date = (request.form.get("ts_date") or "").strip()

    # Build a timestamp for sorting/reporting:
    # - if user gave a date like "2025-10-19", use midnight UTC of that day
    # - if they provided a full ISO with time, respect it (append Z if missing)
    ts_now = _now_iso()
    ts = ts_now
    if ts_date:
        if "T" in ts_date:
            ts = ts_date if ts_date.endswith("Z") else f"{ts_date}Z"
        else:
            # date-only -> midnight UTC
            ts = f"{ts_date}T00:00:00Z"

    tx = {
        "id": ts_now,      # unique id (keep as 'now' so it doesn't collide)
        "ts": ts,          # user-selected timestamp (or now if none)
        "kind": kind,      # expense | debt_payment | transfer | income
        "amount": amount,
        "note": note,

        # shared names set by the visible section (thanks to the JS)
        "from_account": (request.form.get("from_account") or None),
        "to_account": (request.form.get("to_account") or None),
        "category": (request.form.get("category") or None),
        "debt_name": (request.form.get("debt_name") or None),

        # debt optional splits
        "principal_portion": request.form.get("principal_portion") or None,
        "interest_portion": request.form.get("interest_portion") or None,

        # income extras
        "income_subtype": request.form.get("income_subtype") or None,  # paystub | refund | other
        "income_source": request.form.get("income_source") or None,
    }

    # coerce optional numeric splits
    for k in ("principal_portion", "interest_portion"):
        v = tx.get(k)
        if v not in (None, "",):
            try:
                tx[k] = float(v)
            except Exception:
                tx[k] = None

    # apply + persist
    updated, entry = apply_transaction(latest, tx)

    entry_path = f"{_prefix(user_id)}ledger/entries/{entry['id'].replace(':','-')}.json"
    store.write_json(entry_path, entry)
    _append_index(user_id, entry)

    snap_ts = entry["id"].replace(":", "-")
    store.write_json(f"{_prefix(user_id)}snapshots/{snap_ts}.json", updated)
    store.write_json(f"{_prefix(user_id)}latest.json", updated)

    return redirect(url_for("ledger.list_entries"))

def _entry_path(user_id: str, entry_id: str) -> str:
    return f"{_prefix(user_id)}ledger/entries/{entry_id.replace(':','-')}.json"

@bp.post("/delete/<entry_id>")
def delete_entry(entry_id):
    _, user_id = current_user_identity()
    pref = user_prefix(user_id)
    latest = current_app.gcs.read_json(f"{pref}latest.json") or {}
    store   = current_app.gcs

    idx_path = f"{pref}ledger/index.json"

    # Load index and find entry summary
    index = store.read_json(idx_path) or []
    match = next((e for e in index if e.get("id") == entry_id), None)

    # Load the full entry payload if present
    entry = store.read_json(_entry_path(user_id, entry_id)) or match
    if not entry:
        abort(404, description="Entry not found")

    # Reverse the entry effect on latest.json
    latest = store.read_json(latest_path) or {}
    latest = reverse_transaction(latest, entry)

    # Persist updated latest
    store.write_json(latest_path, latest)

    # Remove from index
    index = [e for e in index if e.get("id") != entry_id]
    store.write_json(idx_path, index)

    # Optionally delete the entry file (only if your GCS helper supports delete)
    try:
        # Some implementations may not have delete(); ignore if not present.
        if hasattr(store, "delete"):
            store.delete(_entry_path(user_id, entry_id))
    except Exception:
        pass  # safe to ignore

    return redirect(url_for("ledger.list_entries"))

# --- History / Revert ---

def _snap_path(user_id: str, snap_id: str) -> str:
    # snap_id is like "2025-10-19T05-36-51Z" (your entry["id"] with ":" replaced by "-")
    return f"{_prefix(user_id)}snapshots/{snap_id}.json"

@bp.get("/history")
def history():
    """List recent revert points (taken from the ledger index)."""
    user_id = current_app.config["USER_ID"]
    # We use the ledger index as our list of restore points (1 per entry).
    index = current_app.gcs.read_json(f"{_prefix(user_id)}ledger/index.json") or []
    # newest first, show more if you like
    index = sorted(index, key=lambda x: x.get("ts", ""), reverse=True)[:200]

    # Each entry’s snapshot id used when it was created:
    # snap_id = entry["id"].replace(":", "-")
    rows = []
    for t in index:
        entry_id = (t.get("id") or "").replace(":", "-")
        rows.append({
            "snap_id": entry_id,
            "ts": t.get("ts"),
            "kind": t.get("kind"),
            "amount": t.get("amount"),
            "note": t.get("note") or "",
        })
    return render_template("ledger_history.html", rows=rows)

@bp.post("/revert")
def revert_to_snapshot():
    """Restore latest.json to a selected snapshot; first back up current latest."""
    user_id = current_app.config["USER_ID"]
    store   = current_app.gcs

    snap_id = (request.form.get("snap_id") or "").strip()
    if not snap_id:
        flash("Missing snapshot id.", "error")
        return redirect(url_for("ledger.history"))

    snap = store.read_json(_snap_path(user_id, snap_id))
    if not snap:
        flash("Snapshot not found.", "error")
        return redirect(url_for("ledger.history"))

    # 1) Back up current latest so the revert can itself be undone
    now_iso = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    # Use a clear prefix so you can see it in GCS if needed
    backup_id = f"manual-backup-{now_iso.replace(':','-')}"
    latest = store.read_json(f"{_prefix(user_id)}latest.json") or {}
    store.write_json(_snap_path(user_id, backup_id), latest)

    # 2) Write selected snapshot to latest.json
    store.write_json(f"{_prefix(user_id)}latest.json", snap)

    flash(f"Restored profile to snapshot: {snap_id}", "success")
    return redirect(url_for("ledger.list_entries"))
