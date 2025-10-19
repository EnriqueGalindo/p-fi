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

def reverse_transaction(snapshot: dict, entry: dict):
    """
    Undo a single persisted ledger entry against the snapshot (latest.json).
    Returns a *modified copy* of snapshot.
    """
    snap = {**snapshot}
    accounts = list(snap.get("accounts", []) or [])
    debts    = list(snap.get("debts", []) or [])

    kind   = (entry.get("kind") or "").lower()
    amount = float(entry.get("amount") or 0)

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
        # Expense originally: account -= amount
        acc = find_account(entry.get("from_account"))
        if acc:
            acc["balance"] = float(acc.get("balance") or 0) + amount

    elif kind == "transfer":
        # Transfer originally: src -= amount ; dst += amount
        src = find_account(entry.get("from_account"))
        dst = find_account(entry.get("to_account"))
        if src:
            src["balance"] = float(src.get("balance") or 0) + amount
        if dst:
            dst["balance"] = float(dst.get("balance") or 0) - amount

    elif kind == "debt_payment":
        # Debt payment originally:
        #   account -= amount
        #   debt_balance -= principal (interest portion didn't change balances except cash)
        acc  = find_account(entry.get("from_account"))
        debt = find_debt(entry.get("debt_name"))
        principal = float(entry.get("principal_portion") or entry.get("meta", {}).get("principal_portion") or 0.0)
        # If principal wasn't stored in entry, best-effort: assume full amount was principal
        if principal <= 0 or principal > amount:
            principal = amount

        if acc:
            acc["balance"] = float(acc.get("balance") or 0) + amount
        if debt:
            debt["balance"] = float(debt.get("balance") or 0) + principal

    elif kind == "income":
        # Income originally: to_account += amount
        acc = find_account(entry.get("to_account"))
        if acc:
            acc["balance"] = float(acc.get("balance") or 0) - amount

    else:
        # Unknown kinds are ignored (no-op)
        pass

    snap["accounts"] = accounts
    snap["debts"]    = debts
    return snap


# app/logic/ledger.py
def apply_transaction(snapshot: dict, tx: dict):
    """
    Returns (updated_snapshot, entry)
    entry will also include balance fields for display.

    Supported kinds:
      - expense        : from_account -amount
      - transfer       : from_account -amount  AND  to_account +amount
      - debt_payment   : from_account -amount  AND  debt balance -principal_portion
      - income         : to_account +amount  (subtype via income_subtype)
    """
    # shallow copy of top-level + list containers (items inside will be mutated)
    snap = {**snapshot}
    accounts = list(snap.get("accounts", []) or [])
    debts    = list(snap.get("debts", []) or [])

    kind   = (tx.get("kind") or "").lower()
    amount = float(tx.get("amount") or 0)

    entry = dict(tx)  # make a copy to persist immutably
    entry.setdefault("meta", {})

    # ------------- helpers -------------
    def _norm(s):
        return (s or "").strip()

    def find_account(name):
        n = _norm(name)
        for a in accounts:
            if _norm(a.get("name")) == n:
                return a
        return None

    def find_debt(name):
        n = _norm(name)
        for d in debts:
            if _norm(d.get("name")) == n:
                return d
        return None

    # ------------- kinds -------------
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
        # guardrail: never allocate more than amount
        if principal + interest > amount + 1e-9:
            principal = amount

        debt["balance"] = max(0.0, float(debt.get("balance") or 0) - principal)

        entry["balance_kind"]  = "debt"
        entry["balance_name"]  = debt.get("name")
        entry["balance_after"] = round(float(debt["balance"]), 2)
        entry["account_after"] = round(float(acc["balance"]), 2)  # from-account new bal for reference

    elif kind == "income":
        # Deposit into destination account
        dst = find_account(tx.get("to_account"))
        if not dst:
            raise ValueError("Account not found for income (to_account)")
        before = float(dst.get("balance") or 0)
        after  = before + amount
        dst["balance"] = after

        entry["balance_kind"]  = "account"
        entry["balance_name"]  = dst.get("name")
        entry["balance_after"] = round(after, 2)

        # For the ledger list, show the subtype in your Category column
        # (e.g., paystub, refund, other)
        subtype = (tx.get("income_subtype") or "other").lower()
        entry["category"] = subtype
        # Normalize fields that don't apply
        entry["from_account"] = None
        entry["debt_name"]    = None

    else:
        raise ValueError(f"Unsupported kind: {kind}")

    # write back mutated containers
    snap["accounts"] = accounts
    snap["debts"]    = debts
    return snap, entry