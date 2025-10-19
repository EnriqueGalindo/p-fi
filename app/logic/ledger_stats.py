# app/logic/ledger_stats.py
from __future__ import annotations
import datetime as dt
from collections import defaultdict
from typing import Dict, Any, List

# Helper: parse ISO with trailing 'Z'
def _parse_iso(ts: str) -> dt.datetime:
    # ts like 2025-10-16T13:02:04Z
    if not ts:
        return None
    if ts.endswith("Z"):
        ts = ts[:-1]
    # naive UTC
    return dt.datetime.fromisoformat(ts)

def _period_bounds(now_utc: dt.datetime, period: str):
    period = (period or "month").lower()
    if period == "week":
        # Start: beginning of ISO week (Monday 00:00 UTC)
        start = now_utc - dt.timedelta(days=now_utc.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        # End: start + 7 days
        end = start + dt.timedelta(days=7)
    elif period == "month":
        start = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # next month
        if start.month == 12:
            end = start.replace(year=start.year+1, month=1)
        else:
            end = start.replace(month=start.month+1)
    elif period == "year":
        start = now_utc.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year+1)
    else:  # "all"
        start = dt.datetime.min.replace(tzinfo=None)
        end = dt.datetime.max.replace(tzinfo=None)

    return start, end

def compute_ledger_stats(store, user_id: str, period: str = "month") -> Dict[str, Any]:
    """
    Reads profiles/{user_id}/ledger/index.json and entry files on-demand
    to compute dashboard aggregates for the selected period.

    Returns dict with:
      - window: {start, end}
      - period
      - totals: {income, expenses, expenses_by_category, debt_paid}
      - debts_impacted: [
            {name, total_paid, first_ts, last_ts, balance_before, balance_after}
        ]
    """
    idx_path = f"profiles/{user_id}/ledger/index.json"
    index: List[dict] = store.read_json(idx_path) or []

    now_utc = dt.datetime.utcnow().replace(microsecond=0)
    start, end = _period_bounds(now_utc, period)

    def in_window(ts_str: str) -> bool:
        t = _parse_iso(ts_str)
        if not t:
            return False
        return start <= t < end

    # Aggregates
    income_total = 0.0
    expenses_total = 0.0
    expenses_by_category = defaultdict(float)

    # Debt info keyed by debt name
    debts_agg: Dict[str, Dict[str, Any]] = {}

    # Filter index to window
    window_rows = [r for r in index if in_window(r.get("ts"))]

    # We may need entry files for debt principal_paid + precise balances
    def read_entry(entry_id: str) -> dict:
        # entries are stored with ':' replaced by '-'
        entry_path = f"profiles/{user_id}/ledger/entries/{entry_id.replace(':','-')}.json"
        return store.read_json(entry_path) or {}

    for r in window_rows:
        kind = (r.get("kind") or "").lower()
        amt = float(r.get("amount") or 0.0)

        if kind == "income":
            income_total += amt

        elif kind == "expense":
            expenses_total += amt
            cat = r.get("category") or "other"
            expenses_by_category[cat] += amt

        elif kind == "debt_payment":
            # Prefer principal_portion from entry; fallback to amount
            entry = read_entry(r.get("id", ""))
            principal = float(entry.get("principal_portion") or amt)
            debt_name = r.get("debt_name") or (entry.get("debt_name") or "unknown")
            balance_after = entry.get("balance_after")
            # approximate balance_before = after + principal (same tx)
            balance_before = None
            if balance_after is not None:
                try:
                    balance_before = float(balance_after) + float(principal)
                except Exception:
                    balance_before = None

            d = debts_agg.setdefault(debt_name, {
                "name": debt_name,
                "total_paid": 0.0,
                "first_ts": r.get("ts"),
                "last_ts": r.get("ts"),
                "balance_before": balance_before,
                "balance_after": balance_after,
            })
            d["total_paid"] += principal
            # earliest / latest in window
            if r.get("ts") < d["first_ts"]:
                d["first_ts"] = r.get("ts")
                # Prefer the earliest balance_before we saw
                if balance_before is not None:
                    d["balance_before"] = balance_before
            if r.get("ts") > d["last_ts"]:
                d["last_ts"] = r.get("ts")
                # Prefer the latest balance_after we saw
                if balance_after is not None:
                    d["balance_after"] = balance_after

        # Transfers are neutral in/out, so we don’t include in income/expenses.

    debt_paid_total = sum(d["total_paid"] for d in debts_agg.values())

    return {
        "period": period,
        "window": {
            "start": start.isoformat() + "Z",
            "end": end.isoformat() + "Z",
        },
        "totals": {
            "income": round(income_total, 2),
            "expenses": round(expenses_total, 2),
            "expenses_by_category": {k: round(v, 2) for k, v in expenses_by_category.items()},
            "debt_paid": round(debt_paid_total, 2),
        },
        "debts_impacted": [
            {
                "name": d["name"],
                "total_paid": round(d["total_paid"], 2),
                "first_ts": d["first_ts"],
                "last_ts": d["last_ts"],
                "balance_before": (None if d["balance_before"] is None else round(d["balance_before"], 2)),
                "balance_after": (None if d["balance_after"] is None else round(float(d["balance_after"]), 2)),
            }
            for d in sorted(debts_agg.values(), key=lambda x: x["name"] or "")
        ],
    }
