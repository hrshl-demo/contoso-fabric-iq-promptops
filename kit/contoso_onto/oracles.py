"""Golden questions: ground-truth answers computed from the gold tables, plus an answer comparer.

The expected values never come from a model: each oracle is a few lines of pandas over the gold layer, so a wrong answer
from the data agent, the ontology MCP endpoint, Foundry IQ or a voice agent is detectable.
"""
from __future__ import annotations

import re
from typing import Any, Callable

import pandas as pd

Tables = dict[str, pd.DataFrame]
FR = "retail-frontier-v1"


def _frontier(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["release_id"] == FR]


def _one(df: pd.DataFrame, **where: Any) -> pd.Series:
    for k, v in where.items():
        df = df[df[k] == v]
    if len(df) != 1:
        raise LookupError(f"expected one row for {where}, got {len(df)}")
    return df.iloc[0]


def _monthly_obligations(T: Tables) -> float:
    o = T["scheduled_obligation"]
    return float(o[(o.cadence == "monthly") & (o.status == "active")].amount_inr.sum())


def _net_position(T: Tables) -> dict:
    accounts = float(_frontier(T["account"]).posted_balance_inr.sum())
    invest = float(_frontier(T["investment"]).current_value_inr.sum())
    loans = float(_frontier(T["loan"]).current_principal_inr.sum())
    card = float(_frontier(T["card"]).outstanding_balance_inr.sum())
    return {"assets_inr": round(accounts + invest, 2), "liabilities_inr": round(loans + card, 2), "net_inr": round(accounts + invest - loans - card, 2)}


STOP_WORDS = {"after", "before", "until", "while"}


def _status_words(status: str) -> list[str]:
    """A status code such as temporarily_locked_after_dispute as the words an answer uses (locked, dispute): nobody speaks the code itself."""
    return [w for w in str(status).split("_") if len(w) > 4 and w not in STOP_WORDS and not w.endswith("ly")]


def _code_words(code: str) -> str:
    """A permission code such as view:household_budget as the words an answer uses (household budget)."""
    return code.split(":")[-1].replace("_", " ")


ORACLES: dict[str, Callable[[Tables], Any]] = {
    "mandate_per_action": lambda T: float(_one(T["mandate"], mandate_id="mandate-surplus-001").per_action_limit_inr),
    "mandate_monthly": lambda T: float(_one(T["mandate"], mandate_id="mandate-surplus-001").monthly_limit_inr),
    "mandate_min_balance": lambda T: float(_one(T["mandate"], mandate_id="mandate-surplus-001").minimum_source_balance_inr),
    "mandate_status": lambda T: {"status": _one(T["mandate"], mandate_id="mandate-surplus-001").status,
                                 "revoked_on": str(_one(T["mandate_event"], mandate_id="mandate-surplus-001", event_type="revoked").occurred_date_ist)},
    "mandate_executed": lambda T: float(T["mandate_event"].query("event_type == 'action_executed'").amount_inr.sum()),
    "primary_balance": lambda T: {"balance_inr": float(_one(T["account"], account_id="acc-rhea-primary").posted_balance_inr),
                                  "as_on": str(_one(T["account"], account_id="acc-rhea-primary").balance_as_on)},
    "rhea_total_balance": lambda T: float(_frontier(T["account"]).posted_balance_inr.sum()),
    "card_state": lambda T: (lambda c: {"outstanding_inr": float(c.outstanding_balance_inr), "limit_inr": float(c.credit_limit_inr),
                                        "status": _status_words(c.status)})(
        _one(T["card"], card_id="card-rhea-frontier")),
    "home_loan": lambda T: (lambda l: {"emi_inr": float(l.emi_inr), "principal_inr": float(l.current_principal_inr), "rate_pct": float(l.annual_rate_pct)})(
        _one(T["loan"], loan_id="loan-home-001")),
    "monthly_obligations": _monthly_obligations,
    "emergency_progress": lambda T: (lambda g: {"current_inr": float(g.current_inr), "target_inr": float(g.target_inr), "progress_pct": float(g.progress_pct)})(
        _one(T["goal"], goal_id="goal-emergency")),
    "frontier_txn_counts": lambda T: (lambda t: {"count": int(len(t)), "debits": int((t.direction == "debit").sum()),
                                                 "debit_total_inr": float(t[t.direction == "debit"].amount_inr.sum()),
                                                 "credit_total_inr": float(t[t.direction == "credit"].amount_inr.sum())})(_frontier(T["transaction"])),
    "scam_incident": lambda T: (lambda i: {"amount_inr": float(i.amount_inr), "recovered_inr": float(i.recovery_amount_inr)})(
        _one(T["incident"], incident_id="case-scam-001")),
    "largest_goal": lambda T: (lambda g: {"goal": g["name"], "target_inr": float(g["target_inr"])})(
        _frontier(T["goal"]).sort_values("target_inr", ascending=False).iloc[0]),
    "household_members": lambda T: sorted(T["household_member"].display_name),
    "vivek_grant": lambda T: (lambda g: {"permissions": sorted(_code_words(p) for p in g.permissions.split("; ")),
                                         "denied": sorted(_code_words(p) for p in g.denied_permissions.split("; "))})(
        _one(T["access_grant"], grant_id="grant-vivek-view")),
    "agent_scopes": lambda T: (lambda a: {"scopes": sorted(a.scopes.split("; ")), "denied": sorted(a.denied_scopes.split("; "))})(
        _one(T["registered_agent"], agent_id="agent-rhea-001")),
    "highest_fd_rate": lambda T: (lambda p: {"product": p.product_id, "rate_pct": float(p.interest_rate_pct)})(
        T["product"].query("family == 'fixed_deposit'").sort_values("interest_rate_pct", ascending=False).iloc[0]),
    "journey_f05": lambda T: (lambda j: {"autonomy_ceiling": int(j.autonomy_ceiling), "operations": sorted(j.allowed_prepared_operations.replace(" ", "").split(","))})(
        _one(T["journey"], journey_id="F05")),
    "law_l9": lambda T: _one(T["law"], law_id="L9")["name"],
    "who_approves": lambda T: sorted(T["authority_rule"].query("capability == 'Approve customer action' and permission_class == 'yes'").actor),
    "named_rhea": lambda T: sorted(T["customer"].query("display_name == 'Rhea Bansal'").customer_id),
    "classic_card_fee": lambda T: float(_one(T["product"], product_id="CB-CARD-CLASSIC").annual_fee_inr),
    "j03_sops": lambda T: sorted(T["journey_sop"].query("journey_id == 'J03'").sop_id),
    "freeze_never_claim": lambda T: sorted(p.split()[-1] for p in _one(T["service_request_type"], service_type_id="SR-CARD-FREEZE").never_claim.split("; ")),   # each phrase ends in its verb: blocked, reversed, contacted
    "net_position": _net_position,
    "interest_paid": lambda T: round(float(T["loan_installment"].query("loan_id == 'loan-home-001' and status == 'paid'").interest_paid_inr.sum()), 2),
    "open_crm_cases": lambda T: int(T["crm_case"].query("stage in ['new','in_progress','awaiting_customer']").shape[0]),
    "legacy_note": lambda T: {"superseded_by": _one(T["customer"], customer_id="rhea~legacy").superseded_by_customer_id},
}


def run_oracles(T: Tables, golden: list[dict], tier: str = "full") -> list[dict]:
    out = []
    for g in golden:
        if tier == "core" and g.get("tier", "core") != "core":
            continue
        row = {k: g[k] for k in ("id", "question", "tier", "surfaces", "tags", "judge", "must_include", "must_not_include") if k in g}
        try:
            row["expected"] = ORACLES[g["oracle"]](T)
        except (KeyError, LookupError) as ex:
            row["error"] = f"{g['oracle']}: {ex!r}"
        out.append(row)
    return out


# --------------------------------------------------------------------------------------------------
# comparing a surface's answer text with the expected value
# --------------------------------------------------------------------------------------------------
NUM_RE = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(lakh|lakhs|crore|crores|k)?", re.I)
UNIT = {"lakh": 1e5, "lakhs": 1e5, "crore": 1e7, "crores": 1e7, "k": 1e3}


def numbers_in(text: str) -> list[float]:
    """All numbers in a text, with Indian grouping (4,85,000), thousands commas, lakh/crore and k suffixes."""
    vals = []
    for m in NUM_RE.finditer(text.replace("₹", " ").replace("INR", " ").replace("Rs.", " ").replace("Rs", " ")):
        v = float(m.group(1).replace(",", ""))
        if m.group(2):
            v *= UNIT[m.group(2).lower()]
        vals.append(v)
    return vals


def _flatten(expected: Any) -> list[Any]:
    if isinstance(expected, dict):
        return [x for v in expected.values() for x in _flatten(v)]
    if isinstance(expected, (list, tuple, set)):
        return [x for v in expected for x in _flatten(v)]
    return [expected]


def compare(expected: Any, answer: str, rel_tol: float = 0.005) -> dict:
    """Every expected number must appear (within 0.5%), every expected string must appear (case-insensitive)."""
    nums = numbers_in(answer)
    low = answer.lower()
    missing = []
    for x in _flatten(expected):
        if isinstance(x, bool) or x is None:
            continue
        if isinstance(x, (int, float)):
            target = abs(x)                                           # numbers_in drops the sign, so a deficit (negative expected value) is matched by its magnitude
            if not any(abs(n - target) <= max(target * rel_tol, 0.5 if target > 100 else 0.005) for n in nums):
                missing.append(x)
        elif isinstance(x, str) and x.strip():
            token = x.strip().lower().replace("_", " ")
            if token not in low.replace("_", " "):
                missing.append(x)
    return {"pass": not missing, "missing": missing}
