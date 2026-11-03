"""Unit tests for the gold engine: transforms, row access, templates, markdown tables, macros, and the silver source kind. No repo needed."""
import datetime as dt
import random
from decimal import Decimal

import pyarrow as pa
import pytest

from contoso_onto import gold, silver
from contoso_onto.gold import GoldError, Row, SilverFrames, apply_transform, build_rows, coerce, expand_columns, md_table, render_tpl
from contoso_onto.hooks import HOOKS

MONEY = {"amountMinor": 1008300000 // 1000, "currency": "INR"}   # 1,008,300 paise = INR 10,083.00
PREFIXES = {**gold.DEFAULT_REF_PREFIXES, "rhea": "customer"}


def t(spec, v, ctx=None):
    return apply_transform(spec, v, PREFIXES, Row(None, None, ctx or {}))


def test_money_transforms_are_exact():
    assert t("paise", {"amountMinor": 100830000, "currency": "INR"}) == 100830000
    assert t("inr", {"amountMinor": 100830000, "currency": "INR"}) == 1008300.0
    assert t("inr", {"amountMinor": 5, "currency": "INR"}) == 0.05
    assert t("inr", None) is None
    with pytest.raises(GoldError):
        t("inr", 5)


def test_rate_and_time_transforms():
    assert t("pct_from_bps", 850) == 8.5
    assert t("bps_from_pct", 7.75) == 775
    assert t("ts", "2026-04-01T09:00:00+05:30") == dt.datetime(2026, 4, 1, 3, 30, tzinfo=dt.timezone.utc)
    assert t("ist_date", "2026-07-31T23:30:00+00:00") == dt.date(2026, 8, 1)       # 05:00 IST next day
    assert t("ist_date", "2026-09-10T18:30:00+05:30") == dt.date(2026, 9, 10)
    with pytest.raises(GoldError):
        t("ts", "2026-04-01")


def test_text_transforms():
    assert t("join", ["a", None, "b"]) == "a; b"
    assert t("join", "x") == "x"
    assert t("join", []) is None
    assert t("len", [1, 2, 3]) == 3
    assert t("prefix:member-", "member-vivek") == "member-vivek"
    assert t("prefix:member-", "agent-rhea-001") is None
    assert t("prefix:doc-,DOC-", "DOC-N-PENSION") == "DOC-N-PENSION"
    assert t("re:^(L\\d+)", "L9: No sales during harm") == "L9"
    assert t("re:^L\\d+:\\s*(.*)$", "L9: No sales during harm") == "No sales during harm"
    assert t("re:^[^:]+:(.+)$", "transact") is None
    assert t("perm_class", "Yes, separate authenticated decision") == "yes"
    assert t("perm_class", "No") == "no"
    assert t("perm_class", "Not exposed") == "no"
    assert t("perm_class", "Interpret") == "qualified"


def test_ref_type_uses_prefixes():
    assert t("ref_type", "member-vivek") == "household_member"
    assert t("ref_type", "employer-northwind-india") == "employer"
    assert t("ref_type", "rhea") == "customer"
    assert t("ref_type", "zzz-1") == "other"
    assert t("ref_type", None) is None


def test_owner_namespacing_only_for_colliding_non_canonical_release():
    ctx = {"collisions": {"rhea"}, "canonical_release": "retail-frontier-v1", "release_short": "legacy", "release": "retail4-v1"}
    assert t("owner", "rhea", ctx) == "rhea~legacy"
    assert t("owner", "nivedita", ctx) == "nivedita"
    assert t("owner", "rhea", {**ctx, "release": "retail-frontier-v1"}) == "rhea"
    assert t("superseded_by", "rhea", ctx) == "rhea"
    assert t("superseded_by", "rhea", {**ctx, "release": "retail-frontier-v1"}) is None


def test_facts_text_never_leaks_paise():
    txt = t("facts_text", {"monthlyTuition": {"amountMinor": 1250000, "currency": "INR"}, "studentRef": "member-anika", "names": ["A", "B"]})
    assert "INR 12,500.00" in txt and "1250000" not in txt
    assert "names: A, B" in txt


def test_unknown_transform_fails_loudly():
    with pytest.raises(GoldError):
        t("nope", 1)


def test_coerce_rules():
    assert coerce(3.0, "int", "x") == 3
    with pytest.raises(GoldError):
        coerce(3.5, "int", "x")
    with pytest.raises(GoldError):
        coerce(True, "int", "x")
    with pytest.raises(GoldError):
        coerce({"a": 1}, "string", "x")
    assert coerce("2026-09-30", "date", "x") == dt.date(2026, 9, 30)
    assert coerce(1, "double", "x") == 1.0
    with pytest.raises(GoldError):
        coerce("yes", "bool", "x")


def test_row_paths_parents_and_templates():
    parent = Row({"loanId": "loan-1", "rows": [1]}, None, {"release": "r1"})
    child = Row({"n": 2}, parent, {"release": "r1", "index": 2})
    assert child.get("n") == 2 and child.get("^.loanId") == "loan-1" and child.get("$release") == "r1"
    assert child.get("missing.deep") is None
    assert render_tpl("{^.loanId}#{$index:02d}", child) == "loan-1#02"
    assert render_tpl("{^.nope}#1", child) is None
    leaf = Row("scope-x", parent, {})
    assert leaf.get("@") == "scope-x"


def test_money_macro_expands_to_rupees_and_hidden_paise():
    cols = expand_columns({"columns": [{"money": "posted_balance", "from": "postedBalance", "desc": "Ledger balance."}]})
    names = {c["col"]: c for c in cols}
    assert names["posted_balance_inr"]["type"] == "double" and not names["posted_balance_inr"].get("hidden")
    assert names["posted_balance_paise"]["type"] == "int" and names["posted_balance_paise"]["hidden"] is True
    assert names["posted_balance_paise"]["ontology"] is False


def test_rate_and_instant_macros():
    cols = {c["col"]: c for c in expand_columns({"columns": [
        {"rate_bps": "annual_rate", "from": "annualRateBps", "desc": "Rate."},
        {"instant": "posted_at", "date_col": "posted_date_ist", "from": "postedAt", "desc": "When."}]})}
    assert cols["annual_rate_pct"]["t"] == "pct_from_bps" and cols["annual_rate_bps"]["ontology"] is False
    assert cols["posted_at"]["type"] == "datetime" and cols["posted_date_ist"]["type"] == "date"
    assert "label" not in cols["posted_date_ist"] or cols["posted_date_ist"].get("label") != cols["posted_at"].get("label")


MD = """# Doc

## 5. Laws

Intro text.

| Law | Required behavior |
|---|---|
| **L1: No figure without authority** | A model-authored `balance` is rejected. |
| **L2: No invisible source use** | Documents must be [selected](http://x). |

## 6. Other
"""


def test_md_table_parsing_cleans_markup():
    rows = md_table(MD, "5. Laws")
    assert rows[0] == {"Law": "L1: No figure without authority", "Required behavior": "A model-authored balance is rejected."}
    assert rows[1]["Required behavior"] == "Documents must be selected."
    with pytest.raises(GoldError):
        md_table(MD, "9. Missing")


def test_load_model_merges_files(model):
    assert len(model["tables"]) == 51
    assert len(model["relationships"]) > 70 and len(model["measures"]) >= 40 and len(model["rules"]) >= 20
    assert model["canonical_release"] == "retail-frontier-v1"


# --------------------------------------------------------------------------------------------------
# the silver source kind
# --------------------------------------------------------------------------------------------------
V1_MONEY_SAMPLES = [0, 1, 5, 99, 100, 101, 12345, 100830000, 4_500_000_000_001, 10 ** 15 + 7, -1, -5, -100830000, 2 ** 63 - 1, -(2 ** 63)]


def test_minor_transforms_reproduce_the_v1_money_transforms_bit_for_bit():
    for m in V1_MONEY_SAMPLES:
        money = {"amountMinor": m, "currency": "INR"}
        assert t("minor_to_inr", m).hex() == t("inr", money).hex()
        assert t("minor_to_paise", m) == t("paise", money) == m and isinstance(t("minor_to_paise", m), int)
    assert t("minor_to_inr", None) is None and t("minor_to_paise", None) is None
    for bad in (1.5, True, "5", {"amountMinor": 5}):
        with pytest.raises(GoldError):
            t("minor_to_inr", bad)
        with pytest.raises(GoldError):
            t("minor_to_paise", bad)


def test_decimal_text_transforms_reproduce_the_v1_floats():
    """Silver holds a seed number as canonical decimal text; float(text) is the double the seed number was in v1."""
    rng = random.Random(7)
    seed_numbers = [8.5, 7.75, 0.1, 0.2 + 0.1, 1e-05, 5.0, 12.34, 1234567.891, 18.0, 99.99, 5, 0, 250000, 2.5e9] + [
        round(rng.uniform(0, 100000), rng.randint(0, 6)) for _ in range(500)]
    for v in seed_numbers:
        text = silver.canonical_decimal(v)
        assert t("dec_to_float", text) == float(v) and t("dec_to_float", text).hex() == float(v).hex(), (v, text)
    assert t("dec_to_float", 5) == 5.0 and t("dec_to_float", Decimal("1.5")) == 1.5 and t("dec_to_float", None) is None
    for bad in (True, 1.5, "abc", {"a": 1}):
        with pytest.raises(GoldError):
            t("dec_to_float", bad)


def test_percent_text_to_basis_points_is_exact():
    for bps in (0, 1, 25, 850, 775, 1000, 12345):
        pct = silver.canonical_decimal(Decimal(bps) / 100)
        assert t("dec_to_bps", pct) == bps and t("dec_to_float", pct) == t("pct_from_bps", bps)      # the same two columns the v1 rate macro made
    assert t("dec_to_bps", None) is None
    with pytest.raises(GoldError):
        t("dec_to_bps", "8.505")                  # half a basis point: refused, never rounded


def test_money_and_rate_macros_follow_the_source_kind():
    cols = {c["col"]: c for c in expand_columns({"silver": ["s"], "columns": [
        {"money": "posted", "from": "postedMinor"}, {"rate_bps": "annual", "from": "ratePct", "unit": "pct_text"},
        {"rate_bps": "penalty", "from": "penaltyBps"}, {"rate_pct": "interest", "from": "ratePct"}]})}
    assert (cols["posted_inr"]["t"], cols["posted_paise"]["t"]) == ("minor_to_inr", "minor_to_paise")
    assert (cols["annual_pct"]["t"], cols["annual_bps"]["t"]) == ("dec_to_float", "dec_to_bps")
    assert (cols["penalty_pct"]["t"], cols["penalty_bps"]["t"]) == ("pct_from_bps", "int")
    assert cols["interest_pct"]["t"] == "dec_to_float"
    raw = {c["col"]: c for c in expand_columns({"columns": [{"money": "posted", "from": "postedBalance"}, {"rate_pct": "interest", "from": "r"}]})}
    assert (raw["posted_inr"]["t"], raw["posted_paise"]["t"], raw["interest_pct"]["t"]) == ("inr", "paise", "float")
    with pytest.raises(GoldError):
        expand_columns({"silver": ["s"], "columns": [{"rate_bps": "x", "from": "y", "unit": "furlongs"}]})


PERSONAS = [
    {"ownerId": "rhea", "displayName": "Rhea", "_release_id": "fr", "_owner_id": "rhea"},
    {"ownerId": "rhea", "displayName": "Rhea (old)", "_release_id": "l4", "_owner_id": "rhea"},
    {"ownerId": "niv", "displayName": "Niv", "_release_id": "l4", "_owner_id": "niv"},
]
CASES = [
    {"caseId": "c1", "customerName": "Niv again", "_release_id": "l4", "_owner_id": "niv"},
    {"caseId": "c2", "customerName": "Zed", "_release_id": "l4", "_owner_id": "zed"},
    {"caseId": "c3", "customerName": "Zed again", "_release_id": "l4", "_owner_id": "zed"},
    {"caseId": "c4", "customerName": "Rhea case", "_release_id": "l4", "_owner_id": "rhea"},
]
LOANS = [{"loanId": "L1", "kind": "home", "_release_id": "fr"}, {"loanId": "L1", "kind": "old home", "_release_id": "l4"}]
INSTALMENTS = [{"loanId": "L1", "no": 1, "_release_id": "fr"}, {"loanId": "L1", "no": 1, "_release_id": "l4"},
               {"loanId": "L9", "no": 1, "_release_id": "fr"}]


def frames() -> SilverFrames:
    return SilverFrames({"silver_person": PERSONAS, "silver_case": CASES, "silver_loan": LOANS, "silver_inst": INSTALMENTS},
                        "fr", {"fr": "frontier", "l4": "legacy"}, gold.collisions_of(PERSONAS, "ownerId"))


CUSTOMER = {"table": "customer", "silver": ["silver_person", "silver_case"], "sources": [
    {"silver": "silver_person", "where": {"_release_id": "fr"}, "set": {"segment": "primary"}},
    {"silver": "silver_person", "where": {"_release_id": "l4"}},
    {"silver": "silver_case", "set": {"segment": "crm"}}],
    "columns": [{"col": "customer_id", "type": "string", "key": True, "from": "_owner_id", "t": "owner"},
                {"col": "name", "from": ["displayName", "customerName"]},
                {"col": "segment", "from": "$segment"},
                {"col": "superseded_by", "from": "_owner_id", "t": "superseded_by"},
                {"col": "release_id", "from": "_release_id"}]}


def test_collisions_are_the_personas_of_more_than_one_release():
    assert gold.collisions_of(PERSONAS, "ownerId") == {"rhea"}
    assert gold.collisions_of(PERSONAS[:1] + PERSONAS[2:], "ownerId") == set() and gold.collisions_of([], "ownerId") == set()


def test_a_silver_source_keeps_v1_semantics_namespacing_precedence_context_and_filters():
    res = build_rows(CUSTOMER, None, PREFIXES, {}, frames())
    assert res.errors == [] and res.sources == ["silver/silver_person", "silver/silver_person", "silver/silver_case"]
    by_id = {r["customer_id"]: r for r in res.rows}
    # rhea of the legacy release is namespaced exactly as v1 did (id collides with the canonical release); the CRM party with the same raw id too
    assert list(by_id) == ["rhea", "rhea~legacy", "niv", "zed"]
    assert by_id["rhea"]["superseded_by"] is None and by_id["rhea~legacy"]["superseded_by"] == "rhea" and by_id["niv"]["superseded_by"] is None
    # precedence by source order: the persona beats the CRM case of the same key, the first of two cases beats the second
    assert by_id["niv"]["name"] == "Niv" and by_id["zed"]["name"] == "Zed"
    assert res.rows_read == 7 and res.dropped_duplicates == 3                                  # c1, c3 and the Rhea CRM case
    # `set` constants and `where` filters are per source, release comes from the silver row
    assert (by_id["rhea"]["segment"], by_id["rhea~legacy"]["segment"], by_id["zed"]["segment"]) == ("primary", None, "crm")
    assert [by_id[k]["release_id"] for k in by_id] == ["fr", "l4", "l4", "l4"]


def test_a_release_less_silver_row_stays_release_less():
    rows = [{"productId": "P1", "_release_id": None}, {"productId": "P2", "_release_id": "fr"}]
    table = {"table": "p", "silver": ["silver_p"], "sources": [{"silver": "silver_p"}],
             "columns": [{"col": "product_id", "key": True, "from": "productId"}, {"col": "release_id", "from": ["$release"]}]}
    res = build_rows(table, None, PREFIXES, {}, SilverFrames({"silver_p": rows}, "fr"))
    assert [r["release_id"] for r in res.rows] == [None, "fr"]


def test_a_child_table_can_join_its_parent_within_the_same_release():
    table = {"table": "instalment", "silver": ["silver_inst"], "sources": [
        {"silver": "silver_inst", "join": {"silver": "silver_loan", "on": {"loanId": "loanId"}}}],
        "columns": [{"col": "id", "key": True, "tpl": "{loanId}#{no:02d}#{_release_id}"}, {"col": "loan_kind", "from": "^.kind"}]}
    res = build_rows(table, None, PREFIXES, {}, frames())
    kinds = {r["id"]: r["loan_kind"] for r in res.rows}
    assert kinds == {"L1#01#fr": "home", "L1#01#l4": "old home", "L9#01#fr": None}      # the parent is matched per release; a missing parent is null
    assert gold.check_silver_paths(table, frames()) == []


def test_the_where_require_and_absent_filters_apply_to_silver_rows():
    def read(**flt):
        table = {"table": "c", "sources": [{"silver": "silver_case", **flt}], "columns": [{"col": "case_id", "key": True, "from": "caseId"}]}
        return [r["case_id"] for r in build_rows(table, None, PREFIXES, {}, frames()).rows]
    assert read(where={"_owner_id": "zed"}) == ["c2", "c3"]
    assert read(require="customerName", where={"caseId": "c4"}) == ["c4"]
    assert read(absent="customerName") == []


def test_none_columns_are_null_and_const_columns_are_constant():
    table = {"table": "p", "sources": [{"silver": "silver_person"}], "columns": [
        {"col": "owner", "key": True, "from": "ownerId"}, {"col": "ends_on", "type": "date", "none": "no silver column carries it"},
        {"col": "synthetic", "type": "bool", "const": True}]}
    res = build_rows(table, None, PREFIXES, {}, frames())
    assert {r["ends_on"] for r in res.rows} == {None} and {r["synthetic"] for r in res.rows} == {True}


def test_post_hooks_still_work_on_silver_built_rows():
    table = {"table": "g", "sources": [{"silver": "silver_goal"}], "post": ["goal_progress"], "columns": [
        {"col": "goal_id", "key": True, "from": "goalId"}, {"money": "target", "from": "targetMinor"}, {"money": "current", "from": "currentMinor"},
        {"col": "target_frequency", "from": "frequency"}, {"col": "progress_pct", "type": "double", "const": None}]}
    table["silver"] = ["silver_goal"]
    rows = [{"goalId": "g1", "targetMinor": 20000, "currentMinor": 5000, "frequency": None, "_release_id": "fr"},
            {"goalId": "g2", "targetMinor": 20000, "currentMinor": 5000, "frequency": "monthly", "_release_id": "fr"}]
    res = build_rows(table, None, PREFIXES, HOOKS, SilverFrames({"silver_goal": rows}, "fr"))
    assert [r["progress_pct"] for r in res.rows] == [25.0, None] and res.rows[0]["target_inr"] == 200.0 and res.rows[0]["target_paise"] == 20000


def test_a_missing_frames_or_silver_table_is_an_error_not_an_empty_table():
    table = {"table": "p", "sources": [{"silver": "silver_nope"}], "columns": [{"col": "k", "key": True, "from": "ownerId"}]}
    with pytest.raises(GoldError, match="unknown silver table"):
        build_rows(table, None, PREFIXES, {}, frames())
    with pytest.raises(GoldError, match="no silver build"):
        build_rows(table, None, PREFIXES, {})
    with pytest.raises(GoldError, match="no repo"):
        build_rows({"table": "r", "sources": [{"doc": "x.md", "heading": "h"}], "columns": []}, None, PREFIXES, {})


def test_a_gold_column_naming_a_silver_column_that_does_not_exist_is_rejected():
    assert gold.check_silver_paths(CUSTOMER, frames()) == []              # displayName exists in one source, customerName in the other
    bad = {**CUSTOMER, "columns": CUSTOMER["columns"] + [{"col": "age", "from": "xAgeYears"}, {"col": "t", "tpl": "{caseId}-{nope}"},
                                                          {"col": "ctx", "from": "$unknown"}, {"col": "up", "from": "^.kind"}]}
    errors = gold.check_silver_paths(bad, frames())
    assert len(errors) == 4
    assert any("xAgeYears" in e for e in errors) and any("'nope'" in e for e in errors) and any("$unknown" in e for e in errors)
    assert any("^.kind" in e for e in errors)                              # a parent column without a join
    filtered = {**CUSTOMER, "sources": [{"silver": "silver_person", "where": {"_nope": "x"}}]}
    assert any("source filter reads '_nope'" in e for e in gold.check_silver_paths(filtered, frames()))
    assert any("unknown silver table" in e for e in gold.check_silver_paths({**CUSTOMER, "sources": [{"silver": "silver_x"}]}, frames()))
    reference = {"table": "law", "sources": [{"doc": "x.md", "heading": "h"}], "columns": [{"col": "law_id", "from": "Law"}]}
    assert gold.check_silver_paths(reference, frames()) == []             # a reference table reads docs/config, not silver


def test_frames_over_arrow_tables_carry_columns_rows_and_collisions():
    persona = pa.table({"ownerId": ["rhea", "rhea", "niv"], "_release_id": ["fr", "l4", "l4"],
                        "joined": pa.array([dt.date(2026, 1, 1)] * 3, pa.date32())})
    empty = pa.table({"x": pa.array([], pa.string())})
    fr = SilverFrames.from_tables({"silver_customer": persona, "silver_empty": empty}, "fr", {"fr": "frontier"},
                                  {"table": "silver_customer", "key": "ownerId"})
    assert fr.collisions == {"rhea"} and fr.columns["silver_empty"] == ["x"] and fr.rows["silver_empty"] == []
    assert fr.rows["silver_customer"][2] == {"ownerId": "niv", "_release_id": "l4", "joined": dt.date(2026, 1, 1)}
    assert SilverFrames.from_tables({"silver_empty": empty}).collisions == set()


def test_physical_names_are_one_registry_with_a_lookup_both_ways():
    model = {"tables": [{"table": "account", "physical": "dim_account"}, {"table": "plain"}]}
    assert gold.physical_names(model) == {"account": "dim_account", "plain": "plain"}
    assert gold.physical_name("account", model) == "dim_account" and gold.physical_name("plain", model) == "plain"
    assert gold.logical_name("dim_account", model) == "account"
    with pytest.raises(GoldError):
        gold.physical_name("nope", model)
    with pytest.raises(GoldError):
        gold.logical_name("dim_nope", model)
    assert gold.physical_name("account") == "dim_account" and gold.logical_name("fact_transaction") == "transaction"      # the committed model
