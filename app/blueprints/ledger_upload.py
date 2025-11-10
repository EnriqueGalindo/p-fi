# app/blueprints/ledger_upload.py
import csv
import io
import re
import hashlib
import datetime as dt
from typing import Tuple, Dict, Any, List
from flask import Blueprint, request, redirect, url_for, flash, current_app, render_template

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
    from .auth import current_user_identity
    from ..services.utils import user_prefix

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
    idx_path = f"{pref}ledger/index.json"
    index: List[dict] = store.read_json(idx_path) or []

    # build fast duplicate set from existing index
    existing = set()
    for r in index:
        try:
            ts = (r.get("ts") or "")
            amt = round(float(r.get("amount") or 0), 2)
            desc = _norm_desc(r.get("description") or "")
            existing.add(_existing_key(ts, amt, desc))
        except Exception:
            continue

    inserted = 0
    skipped_dup = 0
    bad_rows = 0

    for raw in reader:
        cols = _csv_cols(raw)
        try:
            ts_iso = _parse_date_to_iso(cols["date"])
            amt = round(_to_float(cols["amount"]), 2)
            desc = (cols["description"] or "").strip()
            desc_norm = _norm_desc(desc)
            category = (cols["category"] or "").strip() or None
            split = (cols["split"] or "").strip() or None
            tags_raw = (cols["tags"] or "").strip()
            tags = [t.strip() for t in tags_raw.split(",") if t.strip()] or None
        except Exception:
            bad_rows += 1
            continue

        # duplicate check
        key = _existing_key(ts_iso, amt)
        if key in existing:
            skipped_dup += 1
            continue

        kind = "income" if amt > 0 else "expense"

        eid = _entry_id(ts_iso, amt, desc_norm)
        entry = {
            "id": eid,
            "ts": ts_iso,
            "amount": amt,
            "kind": kind,
            "description": desc,
            "category": category,     # may be None -> review page
            "split": split,           # stored as provided
            "tags": tags,             # list[str] or None
        }

        # write entry and update index
        store.write_json(f"{pref}ledger/entries/{eid}.json", entry)
        index.append({
            "id": eid,
            "ts": ts_iso,
            "amount": amt,
            "kind": kind,
            "description": desc,
            "category": category,
            "tags": tags,
        })

        existing.add(key)
        inserted += 1

    # persist index
    store.write_json(idx_path, index)

    flash(f"Imported {inserted} new transactions. Skipped {skipped_dup} duplicates. Bad rows: {bad_rows}.", "success")
    return redirect(url_for("ledger_upload.review_uncategorized"))

@bp.get("/ledger/review")
def review_uncategorized():
    """Simple bulk review UI for uncategorized items."""
    from .auth import current_user_identity
    from ..services.utils import user_prefix

    _, user_id = current_user_identity()
    pref = user_prefix(user_id)
    store = current_app.gcs

    idx_path = f"{pref}ledger/index.json"
    index: List[dict] = store.read_json(idx_path) or []

    # newest first; show only uncategorized (or blank) entries
    rows = []
    for r in sorted(index, key=lambda x: x.get("ts",""), reverse=True):
        cat = r.get("category")
        if not cat or str(cat).strip() == "":
            rows.append(r)

    # lightweight category list: you can replace with your canonical set
    all_cats = sorted({ (r.get("category") or "").strip() for r in index if r.get("category") }) or [
        "Groceries", "Dining", "Rent", "Utilities", "Transportation", "Fuel",
        "Entertainment", "Health", "Subscriptions", "Other", "Income"
    ]

    return render_template("ledger_review.html", rows=rows[:300], categories=all_cats)

@bp.post("/ledger/review")
def save_review():
    """Bulk apply categories/tags to entries selected on the review page."""
    from .auth import current_user_identity
    from ..services.utils import user_prefix

    _, user_id = current_user_identity()
    pref = user_prefix(user_id)
    store = current_app.gcs

    idx_path = f"{pref}ledger/index.json"
    index: List[dict] = store.read_json(idx_path) or []

    # incoming form: fields like category-<id>, tags-<id>
    form = request.form
    changed = 0

    index_by_id = { r.get("id"): r for r in index }
    for eid, row in list(index_by_id.items()):
        cat_key = f"category-{eid}"
        tag_key = f"tags-{eid}"
        if cat_key in form or tag_key in form:
            new_cat = (form.get(cat_key) or "").strip() or None
            new_tags = [t.strip() for t in (form.get(tag_key) or "").split(",") if t.strip()] or None

            # update entry file
            epath = f"{pref}ledger/entries/{eid}.json"
            entry = store.read_json(epath) or {}
            if (entry.get("category") or None) != new_cat or (entry.get("tags") or None) != new_tags:
                entry["category"] = new_cat
                entry["tags"] = new_tags
                store.write_json(epath, entry)

                # mirror in index
                row["category"] = new_cat
                row["tags"] = new_tags
                changed += 1

    if changed:
        store.write_json(idx_path, list(index_by_id.values()))

    flash(f"Updated {changed} transaction(s).", "success")
    return redirect(url_for("ledger_upload.review_uncategorized"))
