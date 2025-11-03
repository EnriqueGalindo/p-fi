# app/blueprints/ledger.py
from flask import Blueprint, current_app, render_template, request, redirect, url_for, abort, flash
import datetime as dt
from collections import defaultdict
from typing import Dict, Tuple

from ..logic.ledger import apply_transaction, reverse_transaction
from ..logic.ledger_stats import compute_ledger_stats
from ..services.utils import current_user_identity, user_prefix

bp = Blueprint("ledger", __name__, url_prefix="/ledger")


# ----------------------------
# Helpers
# ----------------------------

def _prefix(user_id: str) -> str:
    return f"profiles/{user_id}/"

def _now_iso() -> str:
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
        # balance display helpers
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


def _parse_ymd(s: str) -> dt.datetime:
    # Accept YYYY-MM-DD or full ISO; normalize to datetime (UTC, no tz info).
    s = (s or "").strip()
    if not s:
        raise ValueError("empty")
    if "T" in s:
        # strip trailing Z if present
        if s.endswith("Z"):
            s = s[:-1]
        return dt.datetime.fromisoformat(s).replace(microsecond=0)
    # date-only -> midnight UTC
    return dt.datetime.fromisoformat(s + "T00:00:00").replace(microsecond=0)


def _default_month_window(today_utc: dt.datetime) -> Tuple[dt.datetime, dt.datetime]:
    # First day of current month -> first day of next month
    start = today_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _window_from_query() -> Tuple[dt.datetime, dt.datetime]:
    """
    Returns (start_dt, end_dt) as naive UTC datetimes.
    - If ?start=YYYY-MM-DD&end=YYYY-MM-DD present, use [start, end) (end is exclusive).
    - Else default to current month.
    """
    now = dt.datetime.utcnow().replace(microsecond=0)
    q_start = request.args.get("start", "").strip()
    q_end   = request.args.get("end", "").strip()

    if q_start and q_end:
        try:
            start_dt = _parse_ymd(q_start)
            end_dt   = _parse_ymd(q_end)
            # Guarantee end > start; if equal or reversed, fallback to 1-day window
            if end_dt <= start_dt:
                end_dt = start_dt + dt.timedelta(days=1)
            return start_dt, end_dt
        except Exception:
            pass  # fall back to default month

    return _default_month_window(now)


def _within(ts_iso: str, start_dt: dt.datetime, end_dt: dt.datetime) -> bool:
    """
    True if ts_iso is within [start_dt, end_dt) (end exclusive).
    """
    if not ts_iso:
        return False
    s = ts_iso.rstrip("Z")
    try:
        t = dt.datetime.fromisoformat(s)
    except Exception:
        return False
    return (t >= start_dt) and (t < end_dt)


def _entries(store, user_id) -> list:
    idx = store.read_json(f"{_prefix(user_id)}ledger/index.json") or []
    return idx


def _actual_expenses_by_category(index: list, start_dt: dt.datetime, end_dt: dt.datetime) -> Dict[str, float]:
    by_cat = defaultdict(float)
    for e in index:
        if e.get("kind") != "expense":
            continue
        if not _within(e.get("ts"), start_dt, end_dt):
            continue
        cat = (e.get("category") or "uncategorized").lower()
        try:
            by_cat[cat] += float(e.get("amount") or 0.0)
        except Exception:
            pass
    return dict(by_cat)


def _range_days(start_dt: dt.datetime, end_dt: dt.datetime) -> float:
    return (end_dt - start_dt).total_seconds() / 86400.0


def _weekly_budget_by_category(store, user_id) -> Dict[str, float]:
    """
    Uses your weekly budget function and folds groceries into 'grocery'.
    Returns per-week expected spend by category.
    """
    from ..logic.weekly_budget import build_weekly_budget
    pref = user_prefix(user_id)

    snapshot = store.read_json(f"{pref}latest.json") or {}
    # If your plan path differs, adjust here:
    plan     = store.read_json(f"{pref}plans/current.json") or {}

    wb = build_weekly_budget(snapshot, plan) or {}
    by_type = dict(wb.get("costs_by_type_week", {}))

    # Fold groceries rule into grocery category
    g_week = float(wb.get("groceries_week") or 0.0)
    by_type["grocery"] = float(by_type.get("grocery", 0.0)) + g_week

    # Normalize case/floats
    return {k.lower(): float(v or 0.0) for k, v in by_type.items()}


def _budget_compare(store, user_id, index: list, start_dt: dt.datetime, end_dt: dt.datetime) -> dict:
    """
    Build a dict with expected vs actual by category for the date window.
    """
    days = _range_days(start_dt, end_dt)
    weeks = days / 7.0

    weekly = _weekly_budget_by_category(store, user_id)
    expected = {k: round(v * weeks, 2) for k, v in weekly.items()}

    actual = _actual_expenses_by_category(index, start_dt, end_dt)
    # include categories that exist only on one side
    cats = set(expected) | set(actual)

    rows = []
    total_expected = 0.0
    total_actual = 0.0
    for c in sorted(cats):
        a = round(float(actual.get(c, 0.0)), 2)
        e = round(float(expected.get(c, 0.0)), 2)
        rows.append({
            "category": c,
            "actual": a,
            "expected": e,
            "delta": round(a - e, 2),
        })
        total_actual += a
        total_expected += e

    return {
        "start_iso": start_dt.replace(microsecond=0).isoformat() + "Z",
        "end_iso":   end_dt.replace(microsecond=0).isoformat() + "Z",
        "days": round(days, 2),
        "expected_by_category": expected,
        "actual_by_category": actual,
        "rows": rows,
        "totals": {
            "actual": round(total_actual, 2),
            "expected": round(total_expected, 2),
            "delta": round(total_actual - total_expected, 2),
        }
    }


# ----------------------------
# Routes
# ----------------------------

@bp.get("/")
def list_entries():
    # identity + data
    _, user_id = current_user_identity()
    pref = user_prefix(user_id)
    store = current_app.gcs
    latest = store.read_json(f"{pref}latest.json") or {}

    # list view table
    index = _entries(store, user_id)
    index = sorted(index, key=lambda x: x.get("ts", ""), reverse=True)[:100]

    # legacy "period" still supported for your existing stats renderer
    period = (request.args.get("period") or "").lower().strip()
    if period not in {"week", "month", "year", "all"}:
        # ignore if you pass start/end; we won't use it for budget
        period = "month"
    stats = compute_ledger_stats(store, user_id, period)

    # new date-range window + budget comparison
    start_dt, end_dt = _window_from_query()
    budget = _budget_compare(store, user_id, index=_entries(store, user_id), start_dt=start_dt, end_dt=end_dt)

    return render_template(
        "ledger_list.html",
        profile=latest,
        ledger=index,
        stats=stats,          # existing data your template already uses
        budget=budget,        # NEW block for Actual vs Expected UI
        start=budget["start_iso"],
        end=budget["end_iso"],
    )


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

    # Build a timestamp for sorting/reporting
    ts_now = _now_iso()
    ts = ts_now
    if ts_date:
        if "T" in ts_date:
            ts = ts_date if ts_date.endswith("Z") else f"{ts_date}Z"
        else:
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
def delete_entry(entry_id: str):
    # who/where
    _, user_id = current_user_identity()
    pref  = user_prefix(user_id)
    store = current_app.gcs

    # paths
    latest_path = f"{pref}latest.json"
    idx_path    = f"{pref}ledger/index.json"
    entry_path  = _entry_path(user_id, entry_id)

    # load index and find summary (optional but used as fallback)
    index = store.read_json(idx_path) or []
    match = next((e for e in index if e.get("id") == entry_id), None)

    # load full entry payload
    entry = store.read_json(entry_path) or match
    if not entry:
        abort(404, description="Entry not found")

    # reverse the entry's effect on latest.json
    latest = store.read_json(latest_path) or {}
    latest = reverse_transaction(latest, entry)
    store.write_json(latest_path, latest)

    # remove from index
    new_index = [e for e in index if e.get("id") != entry_id]
    store.write_json(idx_path, new_index)

    # archive deleted entry (optional)
    store.write_json(f"{pref}ledger/deleted/{entry_id}.json", entry)

    return redirect(url_for("ledger.list_entries"))


# --- History / Revert ---

def _snap_path(user_id: str, snap_id: str) -> str:
    # snap_id is like "2025-10-19T05-36-51Z" (entry["id"] with ":" -> "-")
    return f"{_prefix(user_id)}snapshots/{snap_id}.json"


@bp.get("/history")
def history():
    """List recent revert points (taken from the ledger index)."""
    _, user_id = current_user_identity()
    pref = user_prefix(user_id)

    index = current_app.gcs.read_json(f"{pref}ledger/index.json") or []
    index = sorted(index, key=lambda x: x.get("ts", ""), reverse=True)[:200]

    rows = []
    for t in index:
        entry_id = (t.get("id") or "").replace(":", "-")
        rows.append({
            "snap_id": entry_id,
            "ts":      t.get("ts"),
            "kind":    t.get("kind"),
            "amount":  t.get("amount"),
            "note":    t.get("note") or "",
        })

    return render_template("ledger_history.html", rows=rows)


@bp.post("/revert")
def revert_to_snapshot():
    """Restore latest.json to a selected snapshot; first back up current latest."""
    _, user_id = current_user_identity()
    store = current_app.gcs
    pref = user_prefix(user_id)

    snap_id = (request.form.get("snap_id") or "").strip()
    if not snap_id:
        flash("Missing snapshot id.", "error")
        return redirect(url_for("ledger.history"))

    snap_path = f"{pref}snapshots/{snap_id}.json"
    snap = store.read_json(snap_path)
    if not snap:
        flash("Snapshot not found.", "error")
        return redirect(url_for("ledger.history"))

    # 1) back up current latest.json so this revert is itself undoable
    now_iso = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    backup_id = f"manual-backup-{now_iso.replace(':','-')}"
    latest_path = f"{pref}latest.json"
    latest = store.read_json(latest_path) or {}
    store.write_json(f"{pref}snapshots/{backup_id}.json", latest)

    # 2) write the chosen snapshot into latest.json
    store.write_json(latest_path, snap)

    flash(f"Restored profile to snapshot: {snap_id}", "success")
    return redirect(url_for("ledger.list_entries"))
