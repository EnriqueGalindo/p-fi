# import os
# import json
# import datetime as dt
# from flask import Flask, request, render_template, redirect

# from .services.gcs import GcsStore

# BUCKET = os.environ.get("GCS_BUCKET")
# USER_ID = os.environ.get("USER_ID", "default")
# assert BUCKET, "GCS_BUCKET env var is required"

# app = Flask(__name__)
# store = GcsStore(BUCKET)

# def _profile_prefix():
#     return f"profiles/{USER_ID}/"

# def _snapshot_path(ts_iso: str):
#     return f"{_profile_prefix()}snapshots/{ts_iso}.json"

# def _latest_path():
#     return f"{_profile_prefix()}latest.json"

# @app.get("/")
# def root():
#     # Minimal: just show the questionnaire
#     return render_template("onboarding.html")

# @app.post("/onboarding")
# def onboarding_submit():
#     def collect_group(prefix, fields, cast_map=None, skip_if_all_empty=True):
#         cast_map = cast_map or {}
#         lists = {k: request.form.getlist(f"{prefix}-{k}[]") for k in fields}
#         max_len = max((len(v) for v in lists.values()), default=0)
#         items = []
#         for i in range(max_len):
#             item = {}
#             empty = True
#             for k in fields:
#                 vals = lists.get(k, [])
#                 v = vals[i].strip() if i < len(vals) else ""
#                 if v != "":
#                     empty = False
#                 func = cast_map.get(k)
#                 if func:
#                     try:
#                         v = func(v) if v != "" else None
#                     except Exception:
#                         v = None
#                 item[k] = v
#             if not (skip_if_all_empty and empty):
#                 items.append(item)
#         return items

#     currency = (request.form.get("currency") or "USD").upper()
#     notes = request.form.get("notes", "").strip()

#     incomes = collect_group(
#         "Incomes - recurring sources of income",
#         fields=["name", "amount", "interval", "after_tax"],
#         cast_map={"amount": float, "after_tax": lambda x: x.lower() == "true"},
#     )
#     costs = collect_group(
#         "Costs - recurring expenses",
#         fields=["name", "amount", "interval", "category"],
#         cast_map={"amount": float},
#     )
#     debts = collect_group(
#         "Debts - outstanding debts",
#         fields=["name", "balance", "apr", "min_payment", "due_day"],
#         cast_map={"balance": float, "apr": float, "min_payment": float, "due_day": int},
#     )
#     accounts = collect_group(
#         "Accounts - bank accounts and other assets",
#         fields=["name", "balance"],
#         cast_map={"balance": float},
#     )

#     snapshot = {
#         "user_id": USER_ID,
#         "snapshot_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
#         "currency": currency,
#         "income_streams": incomes,
#         "recurring_costs": costs,
#         "debts": debts,
#         "accounts": accounts,
#         "notes": notes,
#         "version": 1,
#     }

#     ts = snapshot["snapshot_at"].replace(":", "-")
#     snap_path = _snapshot_path(ts)
#     latest_path = _latest_path()

#     store.write_json(snap_path, snapshot)
#     store.write_json(latest_path, snapshot)

#     # Minimal confirmation page (no extra endpoints)
#     return f"""
# <!doctype html>
# <meta charset="utf-8">
# <title>Saved</title>
# <body style="font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin:2rem;">
#   <h1>Snapshot saved</h1>
#   <p>Saved to:</p>
#   <pre>gs://{BUCKET}/{snap_path}</pre>
#   <p>Latest pointer:</p>
#   <pre>gs://{BUCKET}/{latest_path}</pre>
#   <p><a href="/">Back to form</a></p>
# </body>
# """


