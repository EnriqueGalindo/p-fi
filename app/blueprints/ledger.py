# app/blueprints/ledger.py
from flask import Blueprint, current_app, render_template, request, redirect, url_for, abort
import datetime as dt
from ..logic.ledger import apply_transaction, reverse_transaction

bp = Blueprint("ledger", __name__, url_prefix="/ledger")

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
    user_id = current_app.config["USER_ID"]
    latest = current_app.gcs.read_json(f"{_prefix(user_id)}latest.json") or {}
    index  = current_app.gcs.read_json(f"{_prefix(user_id)}ledger/index.json") or []
    index = sorted(index, key=lambda x: x.get("ts",""), reverse=True)[:100]
    return render_template("ledger_list.html", profile=latest, ledger=index)

@bp.get("/new")
def new_entry_form():
    user_id = current_app.config["USER_ID"]
    latest = current_app.gcs.read_json(f"{_prefix(user_id)}latest.json") or {}
    accounts = latest.get("accounts", []) or []
    debts    = latest.get("debts", []) or []
    return render_template("ledger_new.html", accounts=accounts, debts=debts)

@bp.post("/new")
def create_entry():
    user_id = current_app.config["USER_ID"]
    store   = current_app.gcs
    latest  = store.read_json(f"{_prefix(user_id)}latest.json") or {}

    kind   = (request.form.get("kind") or "").lower()
    amount = float(request.form.get("amount") or 0)
    note   = request.form.get("note", "").strip()

    tx = {
        "id": _now_iso(),
        "ts": _now_iso(),
        "kind": kind,                # expense | debt_payment | transfer | income
        "amount": amount,
        "note": note,

        # shared names set by the visible section (thanks to the JS)
        "from_account": request.form.get("from_account") or None,
        "to_account": request.form.get("to_account") or None,
        "category": request.form.get("category") or None,
        "debt_name": request.form.get("debt_name") or None,

        # debt optional splits
        "principal_portion": request.form.get("principal_portion") or None,
        "interest_portion": request.form.get("interest_portion") or None,

        # income extras
        "income_subtype": request.form.get("income_subtype") or None,  # paystub | refund | other
        "income_source": request.form.get("income_source") or None,
    }

    for k in ("principal_portion", "interest_portion"):
        v = tx.get(k)
        if v not in (None, "",):
            try:
                tx[k] = float(v)
            except Exception:
                tx[k] = None

    updated, entry = apply_transaction(latest, tx)

    # persist immutable entry + new snapshot
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
    user_id = current_app.config["USER_ID"]
    store   = current_app.gcs

    idx_path = f"{_prefix(user_id)}ledger/index.json"
    latest_path = f"{_prefix(user_id)}latest.json"

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