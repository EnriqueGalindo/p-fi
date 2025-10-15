# app/blueprints/ledger.py
from flask import Blueprint, current_app, render_template, request, redirect, url_for
import datetime as dt
from ..logic.ledger import apply_transaction

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
        "kind": kind,                # expense | debt_payment | transfer
        "amount": amount,
        "note": note,
        "from_account": request.form.get("from_account") or None,
        "to_account": request.form.get("to_account") or None,
        "category": request.form.get("category") or None,
        "debt_name": request.form.get("debt_name") or None,
        "principal_portion": request.form.get("principal_portion") or None,
        "interest_portion": request.form.get("interest_portion") or None,
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
