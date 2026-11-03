"""The star metadata is honest (design doc section 6): every gold table declares a kind, a one-sentence grain and a lakehouse name; a fact's
grain key is unique; a dimension's business key is unique; the foreign keys of facts and bridges resolve; gold has no decimal column.
Documented exceptions (a fact with no dimension key, a link between two facts) are listed here with their reason, so a new one fails
the test until somebody decides it."""
import collections
import re

import pyarrow as pa
import pytest

from contoso_onto import oac, spec as S
from contoso_onto.gold import ARROW, KIND_PREFIX, KINDS, expand_columns
from contoso_onto.pipeline import validate_star

KIND_COUNTS = {"dimension": 26, "fact": 13, "bridge": 8, "reference": 4}                      # design section 6: 51 tables, 1:1 with v1
# facts that have no relationship to any dimension in relationships.yaml, and why
NO_DIMENSION_KEY = {
    "proactive_event": "a fixture of the seed: evidence references only, no customer or account key in the record",
    "fraud_signal": "a bank-level signal type with 30-day counts; it is not about one customer, business unit or product",
}
# fact -> fact and bridge -> fact links (everything else a fact or bridge points at is a dimension), and why
FACT_TO_FACT = {
    ("transaction", "incident"): "a disputed transaction names the incident that is about it",
    ("service_record", "incident"): "a servicing contact names the incident case it handles",
    ("transaction_evidence", "transaction"): "the evidence link is a bridge between a posting and its evidence",
    ("transaction_evidence", "incident"): "the evidence of a posting can be an incident case",
}
# the `date` typed columns of each fact (what a semantic-only date-role relationship to dim_date can hang on) and the facts with none
FACT_DATE_COLUMNS = {
    "loan_installment": ["due_on"], "transaction": ["posted_date_ist", "value_date"], "mandate_event": ["occurred_date_ist"],
    "incident": ["detected_date_ist"], "service_record": ["opened_date_ist"], "proactive_event": ["as_of_date"], "crm_case": ["opened_date_ist"],
}
UNDATED_FACTS = {
    "protection_gap": "a snapshot at the business date: the seed gives it no date column",
    "scheduled_obligation": "recurs on a day of the month; it has no calendar date",
    "income_record": "carries a period as YYYY-MM text, not a day",
    "monthly_metric": "carries a month as YYYY-MM text, not a day",
    "risk_alert": "carries an instant (raised_at) and no IST date twin",
    "fraud_signal": "a 30-day count with no date",
}
# the one date column each dated fact hangs its date-role relationship to the calendar on: the main business date. A fact with two date columns keeps its
# posting date (a transaction's posted_date_ist, not its value_date); every other dated fact has one.
DATE_ROLE = {
    "loan_installment": "due_on", "transaction": "posted_date_ist", "mandate_event": "occurred_date_ist", "incident": "detected_date_ist",
    "crm_case": "opened_date_ist", "service_record": "opened_date_ist", "proactive_event": "as_of_date",
}
NEW_DATE_ROLES = {"Service Case Opened On Date": "service_record.opened_date_ist", "Proactive Candidate Evaluated On Date": "proactive_event.as_of_date"}


@pytest.fixture(scope="module")
def by_table(model) -> dict:
    return {t["table"]: t for t in model["tables"]}


def key_of(t: dict) -> str:
    return next(c["col"] for c in expand_columns(t) if c.get("key"))


def test_every_table_declares_a_kind_a_grain_and_a_lakehouse_name(model, silver_spec):
    assert validate_star(model, silver_spec) == []
    assert collections.Counter(t["kind"] for t in model["tables"]) == KIND_COUNTS
    for t in model["tables"]:
        assert t["kind"] in KINDS and t["physical"].startswith(KIND_PREFIX[t["kind"]]), t["table"]
        assert t["grain"].strip().endswith(".") and t["grain"].count(". ") == 0, f"{t['table']}: the grain is one sentence"


def test_the_checks_reject_a_wrong_kind_a_missing_grain_and_a_clashing_physical_name(model, silver_spec):
    bad = [dict(t) for t in model["tables"]]
    bad[0].update(kind="cube")
    bad[1].update(grain="")
    bad[2].update(physical=bad[3]["physical"])
    bad[4].update(physical="fact_account")
    bad[5].update(silver=None, reference=None)
    errors = validate_star({**model, "tables": bad}, silver_spec)
    assert len(errors) >= 5 and any("kind must be" in e for e in errors) and any("grain" in e for e in errors) and any("also used by" in e for e in errors)
    assert any("is called dim_" in e for e in errors) and any("exactly one of" in e for e in errors)


def test_a_fact_has_a_unique_grain_key_and_so_does_every_bridge(built, by_table):
    """The grain key is the table key, or the declared `grain_key` columns where the grain names a composite (instalment of a loan, gap per
    customer and cover type, metric per month and business unit, a link between two things)."""
    checked = collections.Counter()
    for name, t in by_table.items():
        if t["kind"] not in ("fact", "bridge"):
            continue
        df = built.tables[name].to_pandas()
        grain = t.get("grain_key") or [key_of(t)]
        assert not df[grain].isna().any().any(), (name, grain)
        assert not df.duplicated(subset=grain).any(), f"{name}: grain key {grain} is not unique"
        assert df[key_of(t)].is_unique, name
        if t["kind"] == "bridge":
            assert len(grain) >= 2, f"{name}: a bridge's grain is the combination of what it links"
        checked[t["kind"]] += 1
    assert checked == {"fact": 13, "bridge": 8}


def test_a_dimension_and_a_reference_table_have_a_unique_never_null_business_key(built, by_table):
    for name, t in by_table.items():
        if t["kind"] in ("dimension", "reference"):
            key = built.tables[name].column(key_of(t)).to_pylist()
            assert None not in key and len(set(key)) == len(key), name
            assert built.tables[name].schema.field(key_of(t)).nullable is False, name


def relationships_from(model, by_table, kinds):
    return [r for r in model["relationships"] if by_table[r["from"].split(".")[0]]["kind"] in kinds]


def test_the_foreign_keys_of_facts_and_bridges_resolve_to_dimensions(built, model, by_table):
    checked = 0
    for r in relationships_from(model, by_table, ("fact", "bridge")):
        (ft, fc), (tt, tc) = r["from"].split("."), r["to"].split(".")
        fk = [v for v in built.tables[ft].column(fc).to_pylist() if v is not None]
        keys = set(built.tables[tt].column(tc).to_pylist())
        assert set(fk) <= keys, f"{r['name']}: {sorted(set(fk) - keys)[:3]} resolve to no {tt}.{tc}"
        assert tc == key_of(by_table[tt]), f"{r['name']}: a foreign key points at the key of the other table"
        assert by_table[tt]["kind"] == "dimension" or (ft, tt) in FACT_TO_FACT, f"{r['name']}: {ft} -> {tt} ({by_table[tt]['kind']}) is not documented"
        checked += 1
    assert checked >= 25


def test_fact_to_fact_links_are_exactly_the_documented_ones(model, by_table):
    found = {(r["from"].split(".")[0], r["to"].split(".")[0]) for r in relationships_from(model, by_table, ("fact", "bridge"))
             if by_table[r["to"].split(".")[0]]["kind"] in ("fact", "bridge")}
    assert found == set(FACT_TO_FACT) and all(reason.strip() for reason in FACT_TO_FACT.values())


def test_every_fact_points_at_a_dimension_except_the_documented_two(model, by_table):
    """The date-role relationships to the calendar are semantic-model plumbing, not a business key, so they do not count here."""
    with_dimension = {r["from"].split(".")[0] for r in relationships_from(model, by_table, ("fact",))
                      if by_table[r["to"].split(".")[0]]["kind"] == "dimension" and r["to"].split(".")[0] != "calendar"}
    facts = {n for n, t in by_table.items() if t["kind"] == "fact"}
    assert facts - with_dimension == set(NO_DIMENSION_KEY) and all(NO_DIMENSION_KEY.values())
    bridges = {n for n, t in by_table.items() if t["kind"] == "bridge"}
    assert bridges <= {r["from"].split(".")[0] for r in relationships_from(model, by_table, ("bridge",)) if by_table[r["to"].split(".")[0]]["kind"] == "dimension"}


def test_gold_has_no_decimal_column(built, model):
    for name, tbl in built.tables.items():
        assert not [f.name for f in tbl.schema if pa.types.is_decimal(f.type)], name
    assert "decimal" not in ARROW                                                                  # the engine has no decimal type to give a column
    assert all(c["type"] in ARROW for t in model["tables"] for c in expand_columns(t))
    assert "decimal" not in oac.TMDL_TYPE                                                          # and the TMDL generator has none either (spec check S014)


def test_dates_are_typed_dates_and_the_facts_with_a_date_column_are_the_documented_ones(built, by_table):
    for name, tbl in built.tables.items():
        for f in tbl.schema:
            if f.name.endswith(("_on", "_date", "_date_ist")):
                assert pa.types.is_date32(f.type), f"{name}.{f.name} is a date by name and must be typed date"
    found = {n: [f.name for f in built.tables[n].schema if pa.types.is_date32(f.type)] for n, t in by_table.items() if t["kind"] == "fact"}
    assert {n: sorted(c) for n, c in found.items() if c} == {n: sorted(c) for n, c in FACT_DATE_COLUMNS.items()}
    assert {n for n, c in found.items() if not c} == set(UNDATED_FACTS) and all(UNDATED_FACTS.values())


def test_the_calendar_is_the_generated_date_dimension(built, by_table):
    cal = by_table["calendar"]
    assert cal["physical"] == "dim_date" and cal["kind"] == "dimension" and cal.get("reference") is True
    assert built.tables["calendar"].num_rows == 822 and key_of(cal) == "date"
    for name in FACT_DATE_COLUMNS:                                      # every date a fact carries falls inside the generated calendar
        dates = set(built.tables["calendar"].column("date").to_pylist())
        for col in FACT_DATE_COLUMNS[name]:
            assert {v for v in built.tables[name].column(col).to_pylist() if v is not None} <= dates, (name, col)


def date_roles(model) -> dict:
    return {r["from"].split(".")[0]: r for r in model["relationships"] if r["to"] == "calendar.date"}


def test_every_fact_with_a_date_column_has_exactly_one_date_role_relationship_to_the_calendar(model, by_table):
    roles = date_roles(model)
    assert len(roles) == len([r for r in model["relationships"] if r["to"] == "calendar.date"]) == 7           # one role per fact: no second filter path
    assert set(roles) == set(FACT_DATE_COLUMNS) == set(DATE_ROLE) and not set(roles) & set(UNDATED_FACTS)
    for fact, r in roles.items():
        assert by_table[fact]["kind"] == "fact" and r["from"] == f"{fact}.{DATE_ROLE[fact]}" and DATE_ROLE[fact] in FACT_DATE_COLUMNS[fact], fact
        assert r["from"].split(".")[1] in {c["col"] for c in expand_columns(by_table[fact]) if c["type"] == "date"}
        assert r["name"].endswith(" On Date") and r["desc"].strip()


def test_the_new_date_roles_are_semantic_only_target_the_calendar_and_document_their_choice(model, tier_specs):
    roles = {r["name"]: r for r in model["relationships"] if r["to"] == "calendar.date"}
    for name, source in NEW_DATE_ROLES.items():
        r = roles[name]
        assert r["from"] == source and r["to"] == "calendar.date" and r.get("ontology") is False and r.get("semantic", True) is True
        assert "Date role: " in r["desc"] and source.split(".")[1] in r["desc"] and "no other date column" in r["desc"]
    for tier, sp in tier_specs.items():
        assert S.check_spec(sp).ok                                                       # S060 (no ambiguous filter path) and S061 (no cycle) still pass
        ontology = {r["name"] for r in sp["relationships"] if r["ontology"]}
        semantic = {r["name"] for r in sp["relationships"] if r["semantic"]}
        present = set(NEW_DATE_ROLES) & {r["name"] for r in sp["relationships"]}
        assert present <= semantic and not present & ontology
        assert present == (set(NEW_DATE_ROLES) if tier == "full" else {"Service Case Opened On Date"})      # ProactiveCandidate is an extended-tier table
        ont = oac.build_ontology(sp, oac.make_ctx())["entityRelationships.tmdl"]
        sm = oac.build_semantic_model(sp, oac.make_ctx())["definition/relationships.tmdl"]
        for name in present:
            assert name not in ont and f"rel_{oac.snake(name)}" in sm


def test_no_measure_filters_on_the_calendar_so_the_date_roles_cannot_move_a_result(model):
    assert not [m["name"] for m in model["measures"] if re.search(r"\bCalendar\b|\[Date\]|'Calendar'", m["expr"])]


@pytest.fixture(scope="module")
def tier_specs(model) -> dict:
    return {tier: S.make_spec(model, tier) for tier in ("core", "full")}
