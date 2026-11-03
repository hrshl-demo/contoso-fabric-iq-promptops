"""Repo-backed tests: the gold layer must reproduce the numbers the repo's own docs and release files state."""
import json
import re

import pandas as pd
import pytest

from contoso_onto import oracles
from contoso_onto.gold import expand_columns
from contoso_onto.pipeline import build_gold

FR, L4 = "retail-frontier-v1", "retail4-v1"


def rel_counts(repo, release):
    return json.loads((repo / "data" / "releases" / release / "release.json").read_text(encoding="utf-8-sig"))["counts"]


def test_every_table_builds_without_errors(built, model):
    assert built.errors == []
    assert set(built.tables) == {t["table"] for t in model["tables"]}


def test_row_counts_match_release_manifests(tables, repo_path):
    fr, l4 = rel_counts(repo_path, FR), rel_counts(repo_path, L4)
    by = lambda t, rel: int((tables[t].release_id == rel).sum())  # noqa: E731
    assert by("account", FR) == fr["accounts"] and by("account", L4) == l4["accounts"]
    assert by("card", FR) == fr["cards"] and by("card", L4) == l4["cards"]
    assert by("deposit", FR) == fr["deposits"] and by("deposit", L4) == l4["deposits"]
    assert by("loan", FR) == fr["loans"]
    assert by("goal", FR) == fr["goals"] and by("goal", L4) == l4["goals"]
    assert by("transaction", FR) == fr["transactions"] and by("transaction", L4) == l4["transactions"]
    assert by("document", FR) == fr["documents"] and by("document", L4) == l4["documents"]
    assert by("consent", FR) == fr["consents"] if "consents" in fr else True
    assert len(tables["household_member"]) == fr["householdMembers"]
    assert len(tables["access_grant"]) == fr["accessGrants"]
    assert len(tables["income_record"]) == fr["incomeRecords"]
    assert len(tables["scheduled_obligation"]) == fr["scheduledObligations"]
    assert len(tables["incident"]) == fr["incidents"]
    assert len(tables["service_record"]) == fr["serviceRecords"]
    assert len(tables["proactive_event"]) == fr["proactiveEvents"]
    assert len(tables["sop"]) == l4["sops"]
    assert len(tables["registered_agent"]) == fr["agents"]
    assert len(tables["mandate"]) == fr["mandates"]
    assert len(tables["investment"]) == fr["investments"]
    # insurance.json mixes policies and a protection-gap record: policies + gap = counts.insurancePolicies
    assert (tables["insurance_policy"].release_id == FR).sum() + len(tables["protection_gap"]) == fr["insurancePolicies"]


def test_money_is_exact_and_consistent(tables):
    for name, df in tables.items():
        for c in df.columns:
            if c.endswith("_paise"):
                inr = df[c[:-6] + "_inr"]
                mask = df[c].notna()
                assert (inr[mask] * 100).round().astype("int64").eq(df.loc[mask, c].astype("int64")).all(), f"{name}.{c}"
                assert str(df[c].dtype).startswith("Int64") or str(df[c].dtype).startswith("int64") or df[c].isna().any()


def test_all_foreign_keys_resolve(built, model):
    # validate_gold already raises on orphans; make the guarantee explicit and count what was checked
    assert not [e for e in built.errors if "orphan" in e]
    assert len(model["relationships"]) >= 70


def test_keys_are_unique_and_columns_are_flat(tables, model):
    for t in model["tables"]:
        key = next(c["col"] for c in expand_columns(t) if c.get("key"))
        assert tables[t["table"]][key].is_unique, t["table"]
    for name, df in tables.items():
        assert all(re.fullmatch(r"[a-z][a-z0-9_]*", c) for c in df.columns), name
        assert not any(str(d) == "object" and df[c].map(lambda v: isinstance(v, (dict, list))).any() for c, d in df.dtypes.items()), name


def test_identity_collision_is_namespaced(tables):
    c = tables["customer"].set_index("customer_id")
    assert {"rhea", "rhea~legacy"} <= set(c.index)
    assert c.loc["rhea", "release_id"] == FR and c.loc["rhea~legacy", "release_id"] == L4
    assert c.loc["rhea~legacy", "superseded_by_customer_id"] == "rhea"
    acc = tables["account"].set_index("account_id")
    assert acc.loc["acc-rhea-primary", "customer_id"] == "rhea" and acc.loc["acc-rhe-sal-01", "customer_id"] == "rhea~legacy"
    # nothing in the legacy release may still point at the raw frontier id
    for name in ("account", "card", "goal", "transaction", "document", "consent", "preference"):
        df = tables[name]
        assert not ((df.release_id == L4) & (df.customer_id == "rhea")).any(), name


def test_frontier_mandate_matches_the_prd(tables):
    m = tables["mandate"].iloc[0]
    assert (m.per_action_limit_inr, m.monthly_limit_inr, m.minimum_source_balance_inr) == (20000.0, 40000.0, 750000.0)
    assert m.status == "revoked" and m.source_account_id == "acc-rhea-primary" and m.destination_account_id == "acc-rhea-reserve"
    ev = tables["mandate_event"].set_index("event_id")
    assert ev.loc["mandate-event-002", "amount_inr"] == 15000.0 and ev.loc["mandate-event-002", "actor_type"] == "agent"
    assert ev.loc["mandate-event-004", "event_type"] == "revoked" and str(ev.loc["mandate-event-004", "occurred_date_ist"]) == "2026-09-10"


def test_policy_tables_are_parsed_from_the_prd(tables):
    assert list(tables["law"].law_id) == [f"L{i}" for i in range(1, 13)]
    assert tables["law"].set_index("law_id").loc["L9", "name"] == "No sales during harm"
    assert len(tables["authority_rule"]) == 70 and set(tables["authority_rule"].permission_class) == {"yes", "no", "qualified"}
    appr = tables["authority_rule"].query("capability == 'Approve customer action' and permission_class == 'yes'")
    assert list(appr.actor) == ["Customer"]
    j = tables["journey"].set_index("journey_id")
    assert list(j.index[-6:]) == ["F01", "F02", "F03", "F04", "F05", "F06"] and j.loc["F05", "autonomy_ceiling"] == 6
    assert j.loc["F01", "title"].startswith("Financial Health")
    assert len(tables["frontier_requirement"]) == 14 and len(tables["bank_agent"]) == 8


def test_product_catalogue_precedence_and_units(tables):
    p = tables["product"].set_index("product_id")
    assert len(p) == 36
    assert p.loc["CB-SAV-SALARY", "data_role"] == "catalogue"          # catalogue beats the Frontier stub
    assert p.loc["CB-CARD-FRONTIER", "data_role"] == "reference_only" and pd.isna(p.loc["CB-CARD-FRONTIER", "annual_fee_inr"])
    assert p.loc["CB-CARD-CLASSIC", "annual_fee_inr"] == 499.0          # whole rupees, not paise
    assert p.loc["CB-FD-SENIOR-INCOME", "interest_rate_pct"] == 7.75


def test_pii_and_restricted_columns_are_flagged(model):
    cols = {(t["table"], c["col"]): c for t in model["tables"] for c in expand_columns(t)}
    for key in (("customer", "date_of_birth"), ("customer", "kyc_address"), ("customer", "email"),
                ("insurance_policy", "policy_number"), ("registered_agent", "kya_key_fingerprint")):
        assert cols[key].get("ontology") is False, key


def test_goal_progress_and_net_position(tables):
    g = tables["goal"].set_index("goal_id")
    assert g.loc["goal-emergency", "progress_pct"] == 48.33
    assert pd.isna(g.loc["goal-niv-01", "progress_pct"])               # monthly-frequency target has no lump-sum progress
    assert oracles.ORACLES["net_position"](tables)["net_inr"] < 0      # home loan principal exceeds liquid assets


def test_calendar_covers_all_transaction_dates(tables):
    cal = set(tables["calendar"].date)
    assert set(tables["transaction"].posted_date_ist.dropna()) <= cal
    assert set(tables["loan_installment"].due_on.dropna()) <= cal


def test_golden_oracles_all_resolve(tables, model):
    res = oracles.run_oracles(tables, model["golden"])
    assert len(res) == len(model["golden"]) and not [r for r in res if r.get("error")]
    exp = {r["id"]: r["expected"] for r in res}
    assert exp["G01"] == 20000.0 and exp["G10"] == 133250.0 and exp["G14"]["goal"] == "Retirement corpus"
    assert exp["G22"] == ["rhea", "rhea~legacy"] and exp["G21"] == ["Customer"]
    assert exp["G26"] == pytest.approx(24177.88, abs=0.01)


def test_gold_build_is_deterministic(model, repo_path, tmp_path):
    from contoso_onto.pipeline import write_gold
    m1 = write_gold(model, build_gold(model, repo_path), tmp_path / "a")
    m2 = write_gold(model, build_gold(model, repo_path), tmp_path / "b")
    assert {k: v["sha256"] for k, v in m1["tables"].items()} == {k: v["sha256"] for k, v in m2["tables"].items()}
