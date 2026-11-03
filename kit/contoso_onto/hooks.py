"""Post-extraction hooks for tables that need a derived column or a split the YAML mapping cannot express."""
from __future__ import annotations

from typing import Callable


def txn_signed(rows: list[dict]) -> list[dict]:
    for r in rows:
        a = r.get("amount_inr")
        r["signed_amount_inr"] = None if a is None else (a if r.get("direction") == "credit" else -a)
    return rows


def goal_progress(rows: list[dict]) -> list[dict]:
    for r in rows:
        t, c = r.get("target_inr"), r.get("current_inr")
        r["progress_pct"] = round(c / t * 100.0, 2) if t and c is not None and r.get("target_frequency") is None else None
    return rows


def consent_status(rows: list[dict]) -> list[dict]:
    for r in rows:
        if r.get("status") is None:
            r["status"] = "revoked" if r.get("revoked_at") is not None else "granted"
    return rows


def product_names(rows: list[dict]) -> list[dict]:
    for r in rows:
        if r.get("display_name") in (None, r.get("family")):
            r["display_name"] = f"{r['product_id']} ({str(r.get('family') or 'product').replace('_', ' ')}, reference only)"
    return rows


HOOKS: dict[str, Callable[[list[dict]], list[dict]]] = {
    "txn_signed": txn_signed, "goal_progress": goal_progress, "consent_status": consent_status,
    "product_names": product_names}
