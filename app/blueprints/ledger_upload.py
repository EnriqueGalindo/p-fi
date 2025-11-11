# app/blueprints/ledger_upload.py
import csv
import io
import re
import hashlib
import datetime as dt
from typing import Tuple, Dict, Any, List
from flask import Blueprint, request, redirect, url_for, flash, current_app, render_template
from ..logic.ledger import apply_transaction
from ..services.utils import (
    user_prefix,
    current_user_identity,
    now_iso,
    normalize_entry,
)

COST_TYPES = {
    "health_fitness", "grocery", "entertainment", "utility",
    "pet", "clothes", "other"
}

bp = Blueprint("ledger_upload", __name__)

# --- helpers ---------------------------------------------------------------

DATE_FMTS = ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d")

def _norm_desc(s: str) -> str:
    # remove repeated spaces, upper for stable matching, strip punctuation spam
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s.upper()

def _parse_date_to_iso(date_str: str) -> str:
    raw = (date_str or "").strip()
    for fmt in DATE_FMTS:
        try:
            d = dt.datetime.strptime(raw, fmt).date()
            # midnight UTC with Z suffix to match your ledger style
            return dt.datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=dt.timezone.utc).isoformat().replace("+00:00", "Z")
        except Exception:
            continue
    # last resort: try dt.fromisoformat
    try:
        d = dt.date.fromisoformat(raw[:10])
        return dt.datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        raise ValueError(f"Bad date: {date_str!r}")

def _to_float(x) -> float:
    try:
        return float(str(x).replace(",", "").strip())
    except Exception:
        return 0.0

def _csv_cols(row: Dict[str, Any]) -> Dict[str, Any]:
    # make keys case-insensitive
    lowered = { (k or "").strip().lower(): v for k, v in row.items() }
    return {
        "date": lowered.get("date") or lowered.get("transaction date") or "",
        "description": lowered.get("description") or lowered.get("memo") or "",
        "category": lowered.get("category") or "",
        "amount": lowered.get("amount") or "",
        "split": lowered.get("split") or "",
        "tags": lowered.get("tags") or "",
    }

def _entry_id(ts_iso: str, amount: float, desc_norm: str) -> str:
    # stable id from core attributes so re-uploads are idempotent
    h = hashlib.sha1(f"{ts_iso[:10]}|{amount:.2f}|{desc_norm}".encode()).hexdigest()[:16]
    return f"{ts_iso[:10]}-{int(round(amount*100)):d}-{h}"

def _existing_key(ts_iso: str, amount: float) -> Tuple[str, float]:
    # we dedupe on DATE (yyyy-mm-dd), rounded amount, normalized description
    return (ts_iso[:10], round(amount, 2))

# --- routes ----------------------------------------------------------------

@bp.get("/ledger/upload")
def upload_form():
    return render_template("ledger_upload_form.html")

@bp.post("/ledger/upload")
def upload_ledger_csv():

    _, user_id = current_user_identity()
    pref = user_prefix(user_id)
    store = current_app.gcs

    file = request.files.get("file")
    if not file:
        flash("No file uploaded", "error")
        return redirect(url_for("ledger_upload.upload_form"))

    # read CSV
    data = io.StringIO(file.stream.read().decode("utf-8", errors="ignore"))
    reader = csv.DictReader(data)
    if not reader.fieldnames:
        flash("CSV missing header row.", "error")
        return redirect(url_for("ledger_upload.upload_form"))

    # load current index
    idx_path = f"{pref}ledger/review/index.json"
    index: List[dict] = store.read_json(idx_path) or []

    # build fast duplicate set from existing index
    existing = set()
    for r in index:
        try:
            ts = (r.get("ts") or "")
            amt = round(float(r.get("amount") or 0), 2)
            existing.add(_existing_key(ts, amt))
        except Exception:
            continue
    print(f"Existing keys: {existing}")
    inserted = 0
    skipped_dup = 0
    bad_rows = 0

    for raw in reader:
        cols = _csv_cols(raw)
        try:
            ts_iso = _parse_date_to_iso(cols["date"])
            amt = abs(round(_to_float(cols["amount"]), 2))
            note = (cols["description"] or "").strip()
            note_norm = _norm_desc(note)
            category = (cols["category"] or "").strip() or None
            split = (cols["split"] or "").strip() or None
            tags_raw = (cols["tags"] or "").strip()
            tags = [t.strip() for t in tags_raw.split(",") if t.strip()] or None
        except Exception:
            bad_rows += 1
            continue

        # duplicate check
        key = _existing_key(ts_iso, amt)
        print(f"Checking key: {key}")
        print(f"Existing keys: {existing}")
        if key in existing:
            skipped_dup += 1
            continue

        kind = "income" if amt > 0 else "expense"

        eid = _entry_id(ts_iso, amt, note_norm)
        entry = {
            "id": eid,
            "ts": ts_iso,
            "amount": abs(amt),
            "kind": kind,
            "note": note,
            "category": category, 
        }

        # write entry and update index
        store.write_json(f"{pref}ledger/review/{eid}.json", entry)
        index.append(entry)

        existing.add(key)
        inserted += 1

    # persist index
    store.write_json(idx_path, index)

    flash(f"Imported {inserted} new transactions. Skipped {skipped_dup} duplicates. Bad rows: {bad_rows}.", "success")
    return redirect(url_for("ledger_upload.review"))

@bp.get("/ledger/review")
def review():

    _, user_id = current_user_identity()
    pref = user_prefix(user_id)
    store = current_app.gcs

    # Read the REVIEW inbox (not the live ledger)
    review_idx_path = f"{pref}ledger/review/index.json"
    index: list[dict] = store.read_json(review_idx_path) or []
    showing = "Showing all transactions in the review inbox (no filters)"

    # Newest first; no filtering
    rows = sorted(index, key=lambda x: x.get("ts", ""), reverse=True)

    # Select options
    types = ["expense", "income", "transfer", "debt_payment"]

    # Derive categories from inbox (or fall back to defaults)
    categories = COST_TYPES.copy()

    # Account/debt options from latest.json
    latest = store.read_json(f"{pref}latest.json") or {}
    account_options = sorted({
        (a.get("name") or "").strip()
        for a in (latest.get("accounts") or [])
        if (a.get("name") or "").strip()
    })
    debt_options = sorted({
        (d.get("name") or "").strip()
        for d in (latest.get("debts") or [])
        if (d.get("name") or "").strip()
    })

    return render_template(
        "ledger_review.html",
        rows=rows[:1000],  # adjust page size as you like
        types=types,
        categories=categories,
        account_options=account_options,
        debt_options=debt_options,
        showing=showing,
    )


@bp.post("/ledger/review")
def save_review():

    _, user_id = current_user_identity()
    pref = user_prefix(user_id)
    store = current_app.gcs

    form = request.form
    # --- review inbox ---
    review_idx_path = f"{pref}ledger/review/index.json"
    review_index = store.read_json(review_idx_path) or []
    review_by_id = {r.get("id"): r for r in review_index}

    # --- main ledger index (load once) ---
    main_idx_path = f"{pref}ledger/index.json"
    main_index = store.read_json(main_idx_path) or []

    # --- current state ---
    latest_path = f"{pref}latest.json"
    latest = store.read_json(latest_path) or {}

    allowed_types = {"expense", "income", "transfer", "debt_payment"}
    processed_ids = []
    new_index_rows = []   # collect index rows to append once
    changed_count = 0

    def _split_to_target(to_raw: str | None):
        if not to_raw:
            return None, None
        to_raw = to_raw.strip()
        if not to_raw:
            return None, None
        if to_raw.startswith("debt::"):
            return None, to_raw.split("debt::", 1)[1].strip() or None
        return to_raw, None

    # iterate only over rows present in the submitted form
    for rid, r in list(review_by_id.items()):
        k_type = f"type-{rid}"; k_cat = f"category-{rid}"; k_note = f"note-{rid}"
        k_from = f"from-{rid}"; k_to = f"to-{rid}"
        if not any(k in form for k in (k_type, k_cat, k_note, k_from, k_to)):
            continue

        base_kind = (r.get("kind") or "").lower()
        base_ts = r.get("ts") or now_iso()
        base_amount = float(r.get("amount") or 0.0)

        new_type = (form.get(k_type) or "").strip().lower() or base_kind
        if new_type not in allowed_types:
            new_type = base_kind

        new_cat = (form.get(k_cat) or "").strip() or None
        new_note = (form.get(k_note) or "").strip() or (r.get("note") or "")
        new_from = (form.get(k_from) or "").strip() or None
        to_raw = (form.get(k_to) or "").strip() or None
        new_to_account, new_debt_name = _split_to_target(to_raw)

        # if user targeted a debt and didn’t change type, coerce to debt_payment
        if new_debt_name and new_type == base_kind and new_type != "debt_payment":
            new_type = "debt_payment"

        tx = {
            "id": now_iso(),     # unique
            "ts": base_ts,
            "kind": new_type,
            "amount": base_amount,
            "note": new_note,
            "from_account": new_from,
            "to_account": new_to_account,
            "category": new_cat,
            "debt_name": new_debt_name,
            "principal_portion": None,
            "interest_portion": None,
            "income_subtype": None,
            "income_source": None,
        }

        updated, entry = apply_transaction(latest, tx)

        # write entry
        entry_path = f"{pref}ledger/entries/{entry['id'].replace(':','-')}.json"
        store.write_json(entry_path, entry)

        idx_path, d_entry = normalize_entry(user_id, entry)

        # collect index row (don’t write yet)
        new_index_rows.append(d_entry)

        # snapshot + latest
        snap_ts = entry["id"].replace(":", "-")
        store.write_json(f"{pref}snapshots/{snap_ts}.json", updated)
        store.write_json(f"{pref}latest.json", updated)
        latest = updated

        processed_ids.append(rid)
        changed_count += 1

    # single write to main index
    if new_index_rows:
        main_index.extend(new_index_rows)
        store.write_json(main_idx_path, main_index)

    # clear processed review files and rewrite review index once
    if processed_ids:
        remaining = []
        for r in review_index:
            if r.get("id") in processed_ids:
                # prefer actual delete if available
                if hasattr(store, "delete"):
                    try:
                        store.delete(f"{pref}ledger/review/{r['id']}.json")
                    except Exception:
                        pass
                else:
                    # fallback: overwrite with {} to reduce payload vs "null"
                    store.write_json(f"{pref}ledger/review/{r['id']}.json", {})
            else:
                remaining.append(r)
        store.write_json(review_idx_path, remaining)

    flash(f"Applied {changed_count} transaction(s) from review.", "success")
    if (form.get("batch") or "").strip():
        return redirect(url_for("ledger_upload.review", batch=form.get("batch")))
    return redirect(url_for("ledger_upload.review"))
