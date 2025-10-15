# app/logic/ledger.py
from copy import deepcopy
import datetime as dt

COST_TYPES = {
    "health_fitness", "grocery", "entertainment", "utility",
    "pet", "clothes", "other"
}
CASH_TYPES = {"cash", "checking", "savings"}  # for visibility only

def _now_iso():
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def _find_by_name(items, name):
    for x in items or []:
        if (x.get("name") or "").strip() == (name or "").strip():
            return x
    return None

# app/logic/ledger.py
def apply_transaction(snapshot: dict, tx: dict):
    """
    Returns (updated_snapshot, entry)
    entry will also include balance fields for display.
    """
    snap = {**snapshot}
    accounts = list(snap.get("accounts", []) or [])
    debts    = list(snap.get("debts", []) or [])

    kind   = (tx.get("kind") or "").lower()
    amount = float(tx.get("amount") or 0)

    entry = dict(tx)  # make a copy to persist immutably
    entry.setdefault("meta", {})

    def find_account(name):
        for a in accounts:
            if (a.get("name") or "") == (name or ""):
                return a
        return None

    def find_debt(name):
        for d in debts:
            if (d.get("name") or "") == (name or ""):
                return d
        return None

    if kind == "expense":
        acc = find_account(tx.get("from_account"))
        if not acc:
            raise ValueError("Account not found for expense")
        bal_before = float(acc.get("balance") or 0)
        acc["balance"] = bal_before - amount
        entry["balance_kind"]  = "account"
        entry["balance_name"]  = acc.get("name")
        entry["balance_after"] = round(float(acc["balance"]), 2)

    elif kind == "transfer":
        src = find_account(tx.get("from_account"))
        dst = find_account(tx.get("to_account"))
        if not src or not dst:
            raise ValueError("Accounts not found for transfer")
        src["balance"] = float(src.get("balance") or 0) - amount
        dst["balance"] = float(dst.get("balance") or 0) + amount
        entry["balance_kind"]        = "transfer"
        entry["balance_name_from"]   = src.get("name")
        entry["balance_after_from"]  = round(float(src["balance"]), 2)
        entry["balance_name_to"]     = dst.get("name")
        entry["balance_after_to"]    = round(float(dst["balance"]), 2)

    elif kind == "debt_payment":
        acc  = find_account(tx.get("from_account"))
        debt = find_debt(tx.get("debt_name"))
        if not acc or not debt:
            raise ValueError("Account or debt not found for debt payment")

        # withdraw from account
        acc["balance"] = float(acc.get("balance") or 0) - amount

        # apply to debt (default: all principal)
        principal = float(tx.get("principal_portion") or amount)
        interest  = float(tx.get("interest_portion") or 0.0)
        if principal + interest > amount + 1e-9:
            principal = amount  # guardrail

        debt["balance"] = max(0.0, float(debt.get("balance") or 0) - principal)

        entry["balance_kind"]  = "debt"
        entry["balance_name"]  = debt.get("name")
        entry["balance_after"] = round(float(debt["balance"]), 2)
        entry["account_after"] = round(float(acc["balance"]), 2)  # optional: from-account new bal for reference

    else:
        raise ValueError(f"Unsupported kind: {kind}")

    # write back
    snap["accounts"] = accounts
    snap["debts"]    = debts
    return snap, entry
