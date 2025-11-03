# app/logic/ledger_stats.py
from __future__ import annotations
import datetime as dt
from collections import defaultdict
from typing import Dict, Any, List, Optional
from ..services.utils import (
    user_prefix, parse_iso, month_window, period_bounds
)


def compute_ledger_stats(
    store,
    user_id: str,
    period: str = "month",
    *,
    start_iso: Optional[str] = None,
    end_iso: Optional[str] = None,
) -> Dict[str, Any]:
    pref = user_prefix(user_id)
    idx_path = f"{pref}ledger/index.json"
    index: List[dict] = store.read_json(idx_path) or []

    # Window selection: custom beats period; if neither, default month.
    if start_iso and end_iso:
        start = parse_iso(start_iso)
        end   = parse_iso(end_iso)
        if not start or not end or start >= end:
            start, end = month_window()
        period_used = "custom"
    else:
        # if someone still passes period, support it; else default month
        now_utc = dt.datetime.utcnow().replace(microsecond=0)
        start, end = period_bounds(now_utc, period or "month")
        period_used = period or "month"

    def in_window(ts_str: str) -> bool:
        t = parse_iso(ts_str)
        return bool(t) and (start <= t < end)

    income_total = 0.0
    expenses_total = 0.0
    expenses_by_category = defaultdict(float)
    debts_agg: Dict[str, Dict[str, Any]] = {}

    window_rows = [r for r in index if in_window(r.get("ts"))]

    def read_entry(entry_id: str) -> dict:
        entry_path = f"{pref}ledger/entries/{entry_id.replace(':','-')}.json"
        return store.read_json(entry_path) or {}

    for r in window_rows:
        kind = (r.get("kind") or "").lower()
        amt = float(r.get("amount") or 0.0)

        if kind == "income":
            income_total += amt

        elif kind == "expense":
            expenses_total += amt
            cat = (r.get("category") or "other").lower()
            expenses_by_category[cat] += amt

        elif kind == "debt_payment":
            entry = read_entry(r.get("id", ""))
            principal = float(entry.get("principal_portion") or amt)
            debt_name = r.get("debt_name") or (entry.get("debt_name") or "unknown")
            balance_after = entry.get("balance_after")

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
            if r.get("ts") < d["first_ts"]:
                d["first_ts"] = r.get("ts")
                if balance_before is not None:
                    d["balance_before"] = balance_before
            if r.get("ts") > d["last_ts"]:
                d["last_ts"] = r.get("ts")
                if balance_after is not None:
                    d["balance_after"] = balance_after

    debt_paid_total = sum(d["total_paid"] for d in debts_agg.values())

    return {
        "period": period_used,
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
