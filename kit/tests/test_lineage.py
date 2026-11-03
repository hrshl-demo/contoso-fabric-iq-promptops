"""Gold lineage (design doc sections 4.3 and 4.4): every gold table traces to silver tables or is a documented reference table; every silver
table named exists and serves it; every gold column reads silver columns that exist; gold follows the silver rows it is given (not the seed);
and the survivorship accounting is explicit: silver rows minus gold rows per table is exactly the documented merges, nothing else is dropped."""
import csv
import dataclasses
import io
from pathlib import Path

import pyarrow as pa
import pytest

from contoso_onto import silver
from contoso_onto.gold import SilverFrames, check_silver_paths, expand_columns
from contoso_onto.pipeline import build_gold

KIT = Path(__file__).resolve().parent.parent
# the gold tables that keep reading curated docs/config: the generated calendar, the laws, authority matrix, requirements and bank agents of the PRD,
# the service request types, the journeys and the journey links (design section 4.3 point 4)
REFERENCE = {"calendar", "law", "authority_rule", "frontier_requirement", "bank_agent", "service_request_type", "journey", "journey_sop", "journey_product"}
# silver columns that silver derives (the app's projection rules) and that gold must never read: the raw lifecycle and display fields behind them are columns too
DERIVED_COMMON = {"id", "label", "state", "sourceKind", "historical", "sourceRecordId"}
# rows silver keeps (it is source-faithful per release) that gold merges by precedence: gold table -> how many silver rows are not in gold, and why
SURVIVORSHIP = {
    "customer": 6,           # CRM-case parties whose key is a persona's (rhea~legacy) or an earlier case's: the persona, then the first case, wins
    "product": 1,            # CB-SAV-SALARY is a catalogue product and a Frontier reference stub: the catalogue row wins by source order
}


def gold_paths(t: dict) -> set:
    return {p for c in expand_columns(t) for p in ([c["from"]] if isinstance(c.get("from"), str) else c.get("from") or [])}


@pytest.fixture(scope="module")
def frames(model, silver_built) -> SilverFrames:
    return SilverFrames.from_tables(silver_built.tables, model["canonical_release"], model["release_short"], model["identity"])


# --------------------------------------------------------------------------------------------------
# the spec: which silver tables feed which gold table
# --------------------------------------------------------------------------------------------------
def test_every_gold_table_has_silver_tables_or_is_a_reference_table(model):
    assert {t["table"] for t in model["tables"] if t.get("reference")} == REFERENCE
    for t in model["tables"]:
        if t["table"] in REFERENCE:
            assert t["reference"] is True and not t.get("silver") and not any("silver" in s for s in t["sources"]), t["table"]
        else:
            assert t["silver"] and not t.get("reference") and all(set(s) & {"silver"} for s in t["sources"]), t["table"]
            assert set(t["silver"]) == {s["silver"] for s in t["sources"]}, t["table"]
    assert sum(1 for t in model["tables"] if t.get("silver")) == 42


def test_every_named_silver_table_exists_and_serves_the_gold_table_and_the_reverse(model, silver_spec):
    by_silver = {t["table"]: t for t in silver_spec["tables"]}
    by_gold = {t["table"]: t for t in model["tables"]}
    for g in model["tables"]:
        for s in g.get("silver") or []:
            assert s in by_silver, f"{g['table']}: silver table {s} does not exist"
            assert g["table"] in by_silver[s]["serves"], f"{s} does not list {g['table']} under `serves`"
    for s in silver_spec["tables"]:
        for served in s["serves"]:
            assert served in by_gold and s["table"] in by_gold[served]["silver"], f"{s['table']} serves {served}, which does not read it"
    assert {s for g in model["tables"] for s in g.get("silver") or []} == set(by_silver)           # every silver data table feeds some gold table


def test_gold_columns_read_only_silver_columns_that_exist(model, frames):
    checked = 0
    for t in model["tables"]:
        assert check_silver_paths(t, frames) == [], t["table"]
        checked += len(expand_columns(t)) if t.get("silver") else 0
    assert checked > 400


def test_the_check_notices_a_silver_column_that_went_away(model, silver_built):
    tables = dict(silver_built.tables)
    tables["silver_loan"] = tables["silver_loan"].drop_columns(["xOpeningPrincipalMinor"])
    frames = SilverFrames.from_tables(tables, model["canonical_release"], model["release_short"], model["identity"])
    loan = next(t for t in model["tables"] if t["table"] == "loan")
    errors = check_silver_paths(loan, frames)
    assert errors and all("xOpeningPrincipalMinor" in e for e in errors) and {e.split(":")[0] for e in errors} == {"loan.opening_principal_inr", "loan.opening_principal_paise"}
    assert not build_gold(model, KIT, silver=dataclasses.replace(silver_built, tables=tables)).ok


def test_gold_never_reads_the_columns_silver_derives(model):
    reads = {t["table"]: gold_paths(t) & DERIVED_COMMON for t in model["tables"] if t.get("silver")}
    assert {k: v for k, v in reads.items() if v} == {}


def test_the_collisions_gold_namespaces_are_the_ones_the_seed_has(model, frames, repo_path):
    from contoso_onto.gold import Repo
    repo = Repo(repo_path, model["canonical_release"], model["release_short"])
    assert frames.collisions == repo.collisions() == {"rhea"}


# --------------------------------------------------------------------------------------------------
# gold follows silver
# --------------------------------------------------------------------------------------------------
def with_column(tbl: pa.Table, name: str, values: list) -> pa.Table:
    return tbl.set_column(tbl.schema.get_field_index(name), tbl.schema.field(name), pa.array(values, tbl.schema.field(name).type))


def test_gold_follows_the_silver_rows_it_is_given_not_the_seed(model, silver_built, built, repo_path):
    """Change one silver amount, one silver name and one silver release: only the gold values built from them move, by the same amount."""
    tables = dict(silver_built.tables)
    acc = tables["silver_account"]
    ids = acc.column("accountId").to_pylist()
    i = ids.index("acc-rhea-primary")
    minor = acc.column("postedBalanceMinor").to_pylist()
    minor[i] += 12345
    legacy = ids.index("acc-rhe-sal-01")
    releases = acc.column("_release_id").to_pylist()
    releases[legacy] = model["canonical_release"]                      # the legacy Rhea account claims to come from the Frontier release
    tables["silver_account"] = with_column(with_column(acc, "postedBalanceMinor", minor), "_release_id", releases)
    cust = tables["silver_customer"]
    names = [("Nivedita R." if n == "Nivedita Rao" else n) for n in cust.column("displayName").to_pylist()]
    tables["silver_customer"] = with_column(cust, "displayName", names)

    tampered = build_gold(model, repo_path, silver=dataclasses.replace(silver_built, tables=tables))
    assert tampered.ok, tampered.errors
    before, after = built.tables["account"].to_pylist(), tampered.tables["account"].to_pylist()
    by_id = {r["account_id"]: (r, a) for r, a in zip(before, after)}
    r, a = by_id["acc-rhea-primary"]
    assert a["posted_balance_paise"] == r["posted_balance_paise"] + 12345 and a["posted_balance_inr"] == a["posted_balance_paise"] / 100.0
    r, a = by_id["acc-rhe-sal-01"]
    assert (r["customer_id"], a["customer_id"]) == ("rhea~legacy", "rhea") and a["release_id"] == model["canonical_release"]      # namespacing follows silver's release
    assert all(r == a for k, (r, a) in by_id.items() if k not in ("acc-rhea-primary", "acc-rhe-sal-01"))
    changed = {r["customer_id"] for r, a in zip(built.tables["customer"].to_pylist(), tampered.tables["customer"].to_pylist()) if r != a}
    assert changed == {"nivedita"}
    assert all(built.tables[n].equals(tampered.tables[n]) for n in built.tables if n not in ("account", "customer"))


# --------------------------------------------------------------------------------------------------
# survivorship: nothing is dropped except the documented merges
# --------------------------------------------------------------------------------------------------
def test_silver_rows_minus_gold_rows_is_exactly_the_documented_survivorship(model, silver_built, built):
    dropped = {}
    for t in model["tables"]:
        if t.get("reference"):
            continue
        silver_in = sum(silver_built.tables[s].num_rows for s in t["silver"])
        gold_rows = built.tables[t["table"]].num_rows
        assert silver_in >= gold_rows, t["table"]
        if silver_in != gold_rows:
            dropped[t["table"]] = silver_in - gold_rows
    assert dropped == SURVIVORSHIP
    assert {t["table"]: t["expected_merged"] for t in model["tables"] if t.get("expected_merged") is not None} == SURVIVORSHIP
    assert {n: r.dropped_duplicates for n, r in built.results.items() if r.dropped_duplicates} == SURVIVORSHIP         # what the build reports (v1: 6 and 1)
    assert sum(r.rows_read for n, r in built.results.items() if not model_table(model, n).get("reference")) - sum(
        built.tables[n].num_rows for n in built.tables if not model_table(model, n).get("reference")) == sum(SURVIVORSHIP.values())


def model_table(model, name):
    return next(t for t in model["tables"] if t["table"] == name)


def test_the_survivors_are_the_ones_the_precedence_rule_names(model, silver_built, built, frames):
    # customers: every persona survives; the dropped rows are CRM-case parties whose key (after legacy namespacing) is already taken
    personas = silver_built.tables["silver_customer"].column("ownerId").to_pylist()
    assert built.tables["customer"].column("source_kind").to_pylist().count("persona") == len(personas) == 4
    crm = silver_built.tables["silver_crm_case"]

    def key(owner, release):
        return f"{owner}~{model['release_short'][release]}" if owner in frames.collisions and release != model["canonical_release"] else owner
    taken = {key(o, r) for o, r in zip(silver_built.tables["silver_customer"].column("ownerId").to_pylist(),
                                       silver_built.tables["silver_customer"].column("_release_id").to_pylist())}
    parties = [key(o, r) for o, r in zip(crm.column("_owner_id").to_pylist(), crm.column("_release_id").to_pylist())]
    new = list(dict.fromkeys(p for p in parties if p not in taken))
    assert len(parties) - len(new) == SURVIVORSHIP["customer"] and built.tables["customer"].column("source_kind").to_pylist().count("crm_case_party") == len(new)
    # products: one catalogue id appears twice and the catalogue row (the first source) is the survivor
    ids = silver_built.tables["silver_product"].column("productId").to_pylist()
    twice = [p for p in set(ids) if ids.count(p) > 1]
    assert twice == ["CB-SAV-SALARY"] and len(ids) - len(set(ids)) == SURVIVORSHIP["product"]
    row = next(r for r in built.tables["product"].to_pylist() if r["product_id"] == "CB-SAV-SALARY")
    assert row["data_role"] == "catalogue" and row["release_id"] == "retail4-v1"


def test_a_merge_the_spec_does_not_expect_fails_the_build(model, silver_built, repo_path):
    wrong = {**model, "tables": [dict(t, expected_merged=5) if t["table"] == "customer" else t for t in model["tables"]]}
    b = build_gold(wrong, repo_path, silver=silver_built)
    assert not b.ok and any("merged by precedence, the spec documents 5" in e for e in b.errors)
    undeclared = {**model, "tables": [{k: v for k, v in t.items() if k != "allow_duplicates"} if t["table"] == "product" else t for t in model["tables"]]}
    b = build_gold(undeclared, repo_path, silver=silver_built)
    assert not b.ok and any("duplicate key rows were dropped" in e for e in b.errors)


# --------------------------------------------------------------------------------------------------
# docs/GOLD_LINEAGE.csv
# --------------------------------------------------------------------------------------------------
def test_the_lineage_csv_is_current_and_covers_every_gold_column(model, silver_spec, migration_tool):
    text = (KIT / "docs" / "GOLD_LINEAGE.csv").read_text(encoding="utf-8")
    assert text == migration_tool.lineage_csv(migration_tool.lineage_rows(model, silver_spec)), "run: python tools/migrate_gold_to_silver.py --lineage-only"
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0]) == ["gold_table", "gold_column", "silver_table", "silver_column", "transform"]
    assert {(r["gold_table"], r["gold_column"]) for r in rows} == {(t["table"], c["col"]) for t in model["tables"] for c in expand_columns(t)}
    assert "UNRESOLVED" not in {r["transform"] for r in rows}
    silver_cols = {t["table"]: {c["col"] for c in silver.emitted_columns(t)} for t in silver_spec["tables"]}
    named = set()
    for r in rows:
        if r["silver_table"].startswith("silver_"):
            assert r["silver_column"] in silver_cols[r["silver_table"]], r
            assert r["silver_table"] in model_table(model, r["gold_table"])["silver"], r
            named.add(r["silver_table"])
        elif model_table(model, r["gold_table"]).get("reference"):
            assert r["silver_table"].startswith("reference:") or not r["silver_table"], r
        else:
            assert r["transform"].startswith(("const:", "none:", "context ")) or r["silver_table"], r              # a constant, a context value or a hook
    assert named == set(silver_cols)                                                                                  # the CSV names every silver table
