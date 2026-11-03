"""Silver engine unit tests that need no repo: strict value conversion, the physical-name and spec validators, column order, the derived
profile rules, the lineage helpers, and a build of a tiny synthetic seed (ids, keys, currency, official-name aliases)."""
import datetime as dt
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest

from contoso_onto import cdm_crosswalk, silver
from contoso_onto.gold import Repo, Row
from contoso_onto.silver import SilverError

KIT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------------------------------------ values
def test_canonical_decimal_is_exact_plain_text():
    cases = {9.5: "9.5", 8.0: "8", 100: "100", "0.50": "0.5", -0.0: "0", "1E+2": "100", Decimal("7.750"): "7.75", "-0.10": "-0.1",
             "123456789012345678901234567890.125": "123456789012345678901234567890.125"}
    for raw, want in cases.items():
        assert silver.canonical_decimal(raw) == want, raw
    assert silver.canonical_decimal(None) is None
    for bad in (True, "abc", float("nan"), float("inf"), "1e999999999", Decimal("0." + "1" * 40)):
        with pytest.raises(SilverError):
            silver.canonical_decimal(bad)


def test_rupees_to_minor_is_exact_and_rejects_fractional_paise():
    assert silver.rupees_to_minor(499) == 49900
    assert silver.rupees_to_minor(12.5) == 1250
    assert silver.rupees_to_minor("0.07") == 7
    assert silver.rupees_to_minor(None) is None
    for bad in (0.001, "1.005", True, "x", 10 ** 17):
        with pytest.raises(SilverError):
            silver.rupees_to_minor(bad)


def test_rupees_to_minor_does_not_round_long_amounts_into_whole_paise():
    # 28 digits (the default decimal context) would have turned this into exactly 100 paise
    for long_amount in ("1.00000000000000000000000000001", "0.000000000000000000000000000001", "123456789012345678.123456789012345678"):
        with pytest.raises(SilverError):
            silver.rupees_to_minor(long_amount)
    assert silver.rupees_to_minor("92233720368547758.07") == 2 ** 63 - 1
    with pytest.raises(SilverError):
        silver.rupees_to_minor("92233720368547758.08")
    with pytest.raises(SilverError):
        silver.rupees_to_minor("sNaN")


def test_paise_accepts_only_an_integer_amount_in_a_money_object():
    assert silver.apply_t("paise", {"amountMinor": 12345, "currency": "INR"}) == 12345
    assert silver.apply_t("paise", {"amountMinor": -5, "currency": "INR"}) == -5
    assert silver.apply_t("paise", {"amountMinor": 2 ** 63 - 1}) == 2 ** 63 - 1
    assert silver.apply_t("paise", None) is None and silver.apply_t("paise", {"amountMinor": None, "currency": "INR"}) is None
    for bad in ({"amountMinor": 12345.7}, {"amountMinor": True}, {"amountMinor": "12345"}, {"amountMinor": "12345.5"}, {"amountMinor": 2 ** 63},
                {"amountMinor": -(2 ** 63) - 1}, {"currency": "INR"}, 5, "5", [5]):
        with pytest.raises(SilverError):
            silver.apply_t("paise", bad)


def test_rates_and_dates_and_versions():
    assert [silver._bps_to_pct_text(b) for b in (850, 775, 100, 0, 1, 4200)] == ["8.5", "7.75", "1", "0", "0.01", "42"]
    with pytest.raises(SilverError):
        silver._bps_to_pct_text(True)
    assert silver._iso_date("2026-09-30") == "2026-09-30"
    for bad in ("2026-09-30T00:00:00Z", "20260930", "2026-13-01", 20260930):
        with pytest.raises((SilverError, ValueError)):
            silver._iso_date(bad)
    assert silver._version_text(3) == "3" and silver._version_text("v1") == "v1" and silver._version_text(None) is None
    with pytest.raises(SilverError):
        silver._version_text(1.5)


def test_convert_is_strict_for_every_silver_type():
    c = silver.convert
    assert c("x", "string", "w") == "x" and c(5, "integer", "w") == 5 and c(7, "minor", "w") == 7 and c(True, "boolean", "w") is True
    assert c("2026-09-30", "date", "w") == dt.date(2026, 9, 30)
    assert c("2026-04-01T09:00:00+05:30", "datetime", "w") == dt.datetime(2026, 4, 1, 3, 30, tzinfo=dt.timezone.utc)
    assert c(9.5, "decimal", "w") == "9.5" and c(Decimal("12.5"), "decimal(38,4)", "w") == Decimal("12.5000")
    for v, typ in ((5, "string"), (True, "integer"), (5.0, "integer"), ("5", "integer"), (2 ** 63, "minor"), (1, "boolean"), ("yes", "boolean"),
                   ("2026-04-01", "datetime"), (dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), "date"), ("2026-9-3", "date"),
                   ({"a": 1}, "string"), (Decimal("0.00001"), "decimal(38,4)"), (Decimal("1" * 40), "decimal(38,4)")):
        with pytest.raises(SilverError):
            c(v, typ, "w")
    with pytest.raises(SilverError):
        c(1, "double", "w")


def test_transform_pipeline_runs_silver_then_gold_transforms():
    assert silver.apply_t("only_float|decimal_text", 1.0) == "1" and silver.apply_t("only_float|decimal_text", 3) is None
    assert silver.apply_t("join", ["a", None, "b"]) == "a; b" and silver.apply_t("len", [1, 2]) == 2
    assert silver.apply_t("paise", {"amountMinor": 5, "currency": "INR"}) == 5
    assert silver.apply_t("json_text", True) == "true" and silver.apply_t("json_text", 1.0) == "1.0" and silver.apply_t("json_text", ["a", "b"]) == "a; b"
    assert [silver.apply_t("value_kind", v) for v in (True, 1, 1.5, "s", ["x"], None)] == ["boolean", "integer", "decimal", "string", "list", None]
    assert silver.apply_t("only_int", True) is None and silver.apply_t("only_bool", 1) is None
    with pytest.raises(SilverError):
        silver.apply_t("paise", 5)
    with pytest.raises(SilverError):
        silver.apply_t("no_such_transform", 5)


def test_arrow_types_never_use_floats_and_decimals_only_when_asked():
    assert not any(pa.types.is_floating(t) for t in silver.ARROW_TYPES.values())
    assert silver.arrow_type("minor") == pa.int64() and silver.arrow_type("decimal") == pa.string()
    assert silver.arrow_type("datetime") == pa.timestamp("us", tz="UTC") and silver.arrow_type("date") == pa.date32()
    assert silver.arrow_type("decimal(38,4)") == pa.decimal128(38, 4)
    with pytest.raises(SilverError):
        silver.arrow_type("double")


# ------------------------------------------------------------------------------------------------ names and spec
def test_physical_names_allow_camel_case_but_no_special_characters():
    ok = [("table", "silver_account"), ("column", "postedBalanceMinor"), ("column", "xAgeYears"), ("technical", "_release_id")]
    assert all(silver.validate_name(n, k) is None for k, n in ok)
    bad = [("table", "Silver_account"), ("table", "silver-account"), ("column", "bad name"), ("column", "a;b"), ("column", "a.b"),
           ("column", "1abc"), ("column", "x{y}"), ("column", "x=y"), ("column", "a" * 61), ("technical", "_Release"), ("technical", "release")]
    assert all(silver.validate_name(n, k) for k, n in bad)


def test_physical_names_are_unique_case_insensitively():
    assert silver.validate_physical_names(["silver_a", "silver_b"], {"silver_a": ["id", "Balance", "xBalance"]}) == []
    errs = silver.validate_physical_names(["silver_a", "silver_a"], {"silver_a": ["id", "Balance", "balance"]})
    assert any("table 'silver_a'" in e for e in errs) and any("column 'balance'" in e for e in errs)
    assert silver.snake("FrontierMandate") == "frontier_mandate" and silver.snake("Account") == "account"


def table(**over):
    t = {"table": "silver_thing", "entity": "Thing", "profile": False, "domain": "Test", "serves": ["thing"], "grain": "One thing.",
         "sources": [{"release": "r1", "file": "things.json"}, {"release": "r2", "file": "things.json"}], "key": ["thingId"],
         "owner": "ownerId",
         "common": {"cite": "app/data_xray/projection.py:322-338", "source_kind": "bank_record",
                    "state": {"from": "status", "default": "recorded"}, "label": {"from": ["name"]}},
         "columns": [{"col": "thingId", "origin": "extension", "type": "string", "from": "thingId", "nullable": False},
                     {"col": "name", "origin": "extension", "type": "string", "from": "name"},
                     {"col": "amountMinor", "origin": "extension", "type": "minor", "from": "amount", "t": "paise"},
                     {"col": "currency", "origin": "extension", "type": "string", "from": "amount.currency"},
                     {"col": "rate", "origin": "extension", "type": "decimal", "from": "rate", "t": "decimal_text"},
                     {"col": "tags", "origin": "extension", "type": "string", "from": "tags", "t": "join"},
                     {"col": "openedOn", "origin": "extension", "type": "date", "from": "openedOn"},
                     {"col": "releaseBusinessDate", "origin": "extension", "type": "date", "from": "$business_date"}]}
    t.update(over)
    return t


def errs(t):
    return silver.validate_spec({"tables": [t]})


def profile_table(**over):
    base = {"profile": True, "profile_entity": "Account", "cdm": None, "bian": [], "key": ["accountId"], "columns": [
        {"col": "accountId", "origin": "profile", "type": "string", "from": "accountId"},
        {"col": "currency", "origin": "profile", "type": "string", "from": "currency"},
        {"col": "xAccountType", "origin": "extension", "type": "string", "from": "accountType"}]}
    return table(**{**base, **over})


def test_a_well_formed_spec_is_valid():
    assert errs(table()) == [] and errs(profile_table()) == []
    assert silver.validate_spec({"tables": []}) == ["silver spec has no tables"]


@pytest.mark.parametrize("change, needle", [
    ({"surprise": 1}, "unknown table key 'surprise'"),
    ({"label": ["name"]}, "unknown table key 'label'"),
    ({"state": "status"}, "unknown table key 'state'"),
    ({"common": None}, "missing `common`"),
    ({"grain": ""}, "missing grain"),
    ({"key": ["nope"]}, "key column 'nope' is not a column"),
    ({"profile_entity": "Account"}, "disagree"),
    ({"cdm": {"entity": "FHAccount"}}, "extension entity makes no CDM or BIAN claim"),
    ({"bian": [{"domain": "Savings Account"}]}, "extension entity makes no CDM or BIAN claim"),
    ({"table": "Silver-Thing"}, "table name 'Silver-Thing'"),
])
def test_spec_validator_table_rules(change, needle):
    assert any(needle in e for e in errs(table(**change))), errs(table(**change))


def with_column(col, **over):
    t = table()
    t["columns"] = [*t["columns"], {"origin": "extension", "type": "string", "from": "x", **col}]
    t.update(over)
    return t


@pytest.mark.parametrize("col, needle", [
    ({"col": "zip", "origin": "bogus"}, "origin must be one of"),
    ({"col": "zip", "type": "double"}, "type must be one of"),
    ({"col": "zip", "const": "a"}, "needs exactly one of from/tpl/const/derive/none"),
    ({"col": "zip", "none": "x"}, "needs exactly one of from/tpl/const/derive/none"),
    ({"col": "zip", "derive": "nope", "from": None}, "unknown derive"),
    ({"col": "zip", "t": "owner"}, "unknown transform 'owner'"),
    ({"col": "Zip"}, "plain camelCase"),
    ({"col": "zip", "color": 1}, "unknown column key 'color'"),
    ({"col": "zip", "origin": "profile"}, "profile column on an extension entity"),
    ({"col": "NAME"}, "collides case-insensitively"),
    ({"col": "label"}, "collides case-insensitively"),
    ({"col": "bad name"}, "must match"),
    ({"col": "zip", "shared": "common"}, "only the profile's sourceVersion"),
])
def test_spec_validator_column_rules(col, needle):
    cols = with_column(col)
    cols["columns"] = [{k: v for k, v in c.items() if v is not None} for c in cols["columns"]]
    assert any(needle in e for e in errs(cols)), errs(cols)


def test_spec_validator_profile_rules_and_money_needs_a_currency():
    assert any("must be x + PascalCase" in e or "x + PascalCase" in e for e in errs(profile_table(columns=[
        {"col": "accountId", "origin": "profile", "type": "string", "from": "accountId"},
        {"col": "accountType", "origin": "extension", "type": "string", "from": "accountType"}])))
    assert any("carries its cdm and bian summary" in e for e in errs(table(profile=True, profile_entity="Account")))
    no_currency = table()
    no_currency["columns"] = [c for c in no_currency["columns"] if c["col"] != "currency"]
    assert any("money columns need a 'currency' column" in e for e in errs(no_currency))
    dup = {"tables": [table(), table()]}
    assert any("duplicate silver table" in e for e in silver.validate_spec(dup))


def test_emitted_columns_are_ordered_common_profile_alias_extension_technical():
    alias = cdm_crosswalk.Alias("Account", "postedBalanceMinor", "FHAccount", "balance", "currency", "derived_major_units",
                                "minor_to_major_decimal", "decimal(38,4)", "r", "e", "major units", "null stays null", None)
    t = profile_table(columns=[{"col": "accountId", "origin": "profile", "type": "string", "from": "accountId"},
                               {"col": "postedBalanceMinor", "origin": "profile", "type": "minor", "from": "p", "t": "paise"},
                               {"col": "currency", "origin": "profile", "type": "string", "from": "currency"},
                               {"col": "xAccountType", "origin": "extension", "type": "string", "from": "accountType"}])
    cols = silver.emitted_columns(t, [alias])
    assert [c["col"] for c in cols] == [*silver.COMMON_COLUMNS, "accountId", "postedBalanceMinor", "currency", "balance", "xAccountType",
                                        "_release_id", "_source_file", "_source_row", "_owner_id"]
    assert [c["origin"] for c in cols][7:] == ["profile", "profile", "profile", "cdm_official", "extension", "technical", "technical",
                                              "technical", "technical"]
    assert {c["col"]: c["nullable"] for c in cols if c["origin"] in ("common", "technical")} == {
        "id": False, "label": False, "state": False, "sourceKind": False, "historical": False, "sourceRecordId": False, "sourceVersion": True,
        "_release_id": True, "_source_file": False, "_source_row": False, "_owner_id": True}
    assert next(c for c in cols if c["col"] == "balance")["type"] == "decimal(38,4)"


def test_a_shared_profile_column_takes_the_common_slot_once():
    t = profile_table(columns=[{"col": "accountId", "origin": "profile", "type": "string", "from": "accountId"},
                               {"col": "sourceVersion", "origin": "profile", "type": "string", "from": "version", "t": "version_text",
                                "shared": "common"}])
    names = [c["col"] for c in silver.emitted_columns(t)]
    assert names.count("sourceVersion") == 1 and names.index("sourceVersion") == 6
    assert next(c for c in silver.emitted_columns(t) if c["col"] == "sourceVersion")["profile_attribute"] == "sourceVersion"


# ------------------------------------------------------------------------------------------------ the app's common-column rules
def common(**over):
    return {"cite": "app/data_xray/projection.py:322-338", "source_kind": "bank_record", "state": {"const": "recorded"}, **over}


@pytest.mark.parametrize("over, needle", [
    ({"cite": None}, "common.cite must name the app source lines"),
    ({"cite": "projection.py 516-523"}, "common.cite must name the app source lines"),
    ({"cite": ["app/data_xray/projection.py:516-523", "nope"]}, "common.cite must name the app source lines"),
    ({"source_kind": "seed"}, "not in the app's SourceKind vocabulary"),
    ({"source_kind": "bank-record"}, "not in the app's SourceKind vocabulary"),
    ({"state": {"const": "derived:recorded"}}, "retired placeholder"),
    ({"state": {"from": "status", "default": "seed"}}, "retired placeholder"),
    ({"state": {"const": ""}}, "must be non-empty text"),
    ({"state": {"from": "status"}}, "so a state is never empty"),
    ({"state": {"when": "revokedAt", "then": "revoked"}}, "so a state is never empty"),
    ({"state": "status"}, "so a state is never empty"),
    ({"state": {"from": [], "default": "recorded"}}, "needs a seed path or a list of seed paths"),
    ({"label": {"default": "x"}}, "common.label is {from[, default]}"),
    ({"label": {"from": ["name"], "extra": 1}}, "common.label is {from[, default]}"),
    ({"historical": {"state_in": "active", "source_kind": "history"}}, "common.historical is"),
    ({"historical": {"state_in": "inactive", "source_kind": "past"}}, "not in the app's SourceKind vocabulary"),
    ({"surprise": 1}, "unknown common key 'surprise'"),
])
def test_common_rule_validator(over, needle):
    t = table(common=common(**over))
    assert any(needle in e for e in errs(t)), errs(t)


def test_a_valid_common_block_in_every_form_passes():
    for state in ({"const": "customer"}, {"from": ["status", "state"], "default": "recorded"},
                  {"when": "revokedAt", "then": "revoked", "default": "recorded"}):
        t = table(common=common(state=state, label={"from": "title", "default": "Source document"},
                                historical={"state_in": "inactive", "source_kind": "history"}))
        assert errs(t) == []
    assert errs(table(common=common(cite=["app/data_xray/projection.py:66", "app/frontier/contracts.py:72-93"]))) == []


def test_the_app_vocabulary_copies_are_pinned():
    assert len(silver.SOURCE_KINDS) == 15 and "seed" not in silver.SOURCE_KINDS
    assert {"bank_record", "catalogue", "document_metadata", "customer_stated", "consent", "frontier", "history"} <= silver.SOURCE_KINDS
    assert silver.INACTIVE_STATES == {"closed", "matured", "renewed", "superseded", "cancelled", "expired", "withdrawn"}
    assert silver.RETIRED_LITERALS == {"seed", "derived:recorded"}


def test_state_rules_follow_the_apps_put_arguments():
    node = lambda n: Row(n, None, {})  # noqa: E731
    assert silver.common_state({"const": "customer"}, node({"status": "active"})) == "customer"
    from_status = {"from": "status", "default": "recorded"}
    assert silver.common_state(from_status, node({"status": "closed"})) == "closed"
    assert [silver.common_state(from_status, node(n)) for n in ({}, {"status": None}, {"status": ""})] == ["recorded"] * 3
    revoked = {"when": "revokedAt", "then": "revoked", "default": "recorded"}
    assert silver.common_state(revoked, node({"revokedAt": "2026-01-01T00:00:00+05:30"})) == "revoked"
    assert [silver.common_state(revoked, node(n)) for n in ({}, {"revokedAt": None}, {"revokedAt": ""})] == ["recorded"] * 3
    with pytest.raises(SilverError, match="state is not text"):
        silver.common_state(from_status, node({"status": 7}))


def test_label_rules_prefer_the_display_field_then_the_literal_then_the_key():
    node = lambda n: Row(n, None, {})  # noqa: E731
    rule = {"from": ["title", "name"], "default": "Source document"}
    assert silver.common_label(rule, node({"title": "A", "name": "B"}), "k") == "A"
    assert silver.common_label(rule, node({"name": "B"}), "k") == "B"
    assert silver.common_label(rule, node({"title": ""}), "k") == "Source document"
    assert silver.common_label({"from": ["field"]}, node({}), "pref-1") == "pref-1"
    assert silver.common_label(None, node({"title": "x"}), "k") == "k"
    with pytest.raises(SilverError, match="label is not text"):
        silver.common_label(rule, node({"title": 3}), "k")


def test_an_inactive_state_is_retained_history_with_the_history_kind():
    rule = {"source_kind": "bank_record", "historical": {"state_in": "inactive", "source_kind": "history"}}
    got = {s: silver.common_kind(rule, s) for s in (*sorted(silver.INACTIVE_STATES), "active", "reversed", "recorded")}
    assert all(v == (True, "history") for k, v in got.items() if k in silver.INACTIVE_STATES)
    assert got["active"] == got["reversed"] == got["recorded"] == (False, "bank_record")
    assert silver.common_kind({"source_kind": "catalogue"}, "closed") == (False, "catalogue")


def test_common_paths_and_pointers_state_the_rule_and_its_source():
    t = table(common=common(state={"when": "revokedAt", "then": "revoked", "default": "recorded"}, label={"from": ["title", "name"]}))
    assert silver.common_paths(t) == ["revokedAt", "title", "name"] and silver.common_paths(table(common=common())) == []
    rich = table(common=common(state={"from": "status", "default": "recorded"}, label={"from": ["title"], "default": "Source document"},
                               historical={"state_in": "inactive", "source_kind": "history"}))
    pointer = lambda tbl, name: silver._common_pointer(tbl, name, None)  # noqa: E731
    assert pointer(rich, "state") == "/status else 'recorded'" and pointer(rich, "label") == "/title else 'Source document'"
    assert pointer(rich, "sourceKind") == "history when historical, else bank_record"
    assert pointer(rich, "historical").startswith("state in {cancelled, closed, expired")
    assert pointer(t, "state") == "'revoked' when /revokedAt is set, else 'recorded'" and pointer(t, "historical") == "const: false"
    assert pointer(table(common=common()), "sourceKind") == "const: bank_record" and pointer(table(common=common()), "label") == "sourceRecordId"
    assert silver._cites(rich) == "app/data_xray/projection.py:322-338"


# ------------------------------------------------------------------------------------------------ derived profile rules
def row(node):
    return Row(node, None, {})


def test_product_rules_follow_the_bank_policy_and_the_profile():
    d = silver.DERIVED
    assert d["product_currency"](row({"parameters": {"currency": "USD"}})) == "USD"
    assert d["product_currency"](row({"parameters": {}})) == "INR" and d["product_currency"](row({"productType": "card"})) is None
    assert d["product_max_term_months"](row({"parameters": {"maximumTermMonths": 60}, "family": "fixed_deposit"})) == 60
    assert d["product_max_term_months"](row({"parameters": {"maximumTenureYears": 20}, "family": "home_loan"})) == 240
    assert d["product_max_term_months"](row({"parameters": {"maximumTenureYears": 20}, "family": "personal_loan"})) is None
    assert d["product_requires_decision"](row({"family": "credit_card"})) is True
    assert d["product_requires_decision"](row({"family": "debit_card"})) is False
    assert d["product_requires_decision"](row({"family": "health_cover"})) is True
    assert d["product_requires_decision"](row({})) is None
    with pytest.raises(SilverError):
        d["product_requires_decision"](row({"family": "crypto"}))
    assert silver.DECISION_FAMILIES < silver.KNOWN_FAMILIES


def test_document_rules_use_an_asset_only_when_it_is_unambiguous():
    d = silver.DERIVED
    one = row({"assets": [{"mimeType": "application/pdf", "sha256": "ab"}], "pageCount": 2})
    two = row({"assets": [{"mimeType": "application/pdf", "sha256": "ab"}, {"mimeType": "image/png", "sha256": "cd"}]})
    same = row({"assets": [{"mimeType": "application/pdf", "sha256": "ab"}, {"mimeType": "application/pdf", "sha256": "ab"}]})
    assert d["document_content_type"](one) == "application/pdf" and d["document_source_sha256"](one) == "ab"
    assert d["document_content_type"](two) is None and d["document_source_sha256"](two) is None
    assert d["document_content_type"](same) == "application/pdf"
    assert d["document_content_type"](row({"contentType": "text/plain"})) == "text/plain" and d["document_content_type"](row({})) is None
    assert d["document_page_count_basis"](one) == "declared_metadata" and d["document_page_count_basis"](two) is None


# ------------------------------------------------------------------------------------------------ lineage helpers
def test_paths_normalise_to_source_level_and_field():
    plain, child = {"release": "r", "file": "f.json"}, {"release": "r", "file": "f.json", "explode": "items"}
    n = silver.normalise_path
    assert n("a.b", plain) == ("", "a.b") and n("a.b", child) == ("items", "a.b")
    assert n("^.parentId", child) == ("", "parentId") and n("@", child) == ("items", "@") and n("^", child) == ("", "@")
    assert n("$release", plain) is None and n("$index", child) is None and n("$business_date", plain) == ("", "$business_date")
    assert silver.source_key({"custom": "monthly_metric", "release": "r"}) == "custom:monthly_metric"
    assert silver.source_key(plain) == "r/f.json" and silver.source_key({"data": "relationship/products.json"}) == "data/relationship/products.json"


def test_read_fields_covers_from_tpl_and_ignores_derive_reads():
    t = {"sources": [{"release": "r", "file": "f.json"}]}
    cols = [{"from": ["a", "b.c"]}, {"tpl": "{x}-{^.y}"}, {"derive": "product_currency", "reads": ["secret"]}, {"none": "n"}, {"from": "$release"}]
    assert silver.read_fields(t, cols) == {("r/f.json", "", p) for p in ("a", "b.c", "x", "y")}


def test_release_of_and_repo_path():
    assert silver.release_of("data/releases/retail4-v1/bank/risk.json") == "retail4-v1" and silver.release_of("data/relationship/products.json") is None
    assert silver.repo_path({"release": "r", "file": "bank/x.json"}) == "data/releases/r/bank/x.json"
    assert silver.repo_path({"data": "relationship/products.json"}) == "data/relationship/products.json"
    with pytest.raises(SilverError):
        silver.repo_path({"custom": "x"})


def test_load_silver_spec_merges_files_in_name_order(tmp_path):
    (tmp_path / "silver").mkdir()
    (tmp_path / "silver" / "20_b.yaml").write_text("silver:\n  - {table: silver_b}\n", encoding="utf-8")
    (tmp_path / "silver" / "10_a.yaml").write_text("silver:\n  - {table: silver_a}\n  - {table: silver_c}\n", encoding="utf-8")
    assert [t["table"] for t in silver.load_silver_spec(tmp_path)["tables"]] == ["silver_a", "silver_c", "silver_b"]
    assert silver.load_silver_spec(tmp_path / "missing") == {"tables": []}


# ------------------------------------------------------------------------------------------------ a tiny synthetic seed
def write_seed(root: Path) -> Repo:
    def put(rel, data):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")

    money = lambda n, c="INR": {"amountMinor": n, "currency": c}  # noqa: E731
    put("data/releases/r1/release.json", {"releaseId": "r1", "businessDate": "2026-01-31"})
    put("data/releases/r2/release.json", {"releaseId": "r2", "businessDate": "2026-02-28"})
    put("data/releases/r1/things.json", [
        {"thingId": "t1", "ownerId": "o1", "name": "Alpha", "amount": money(12345), "rate": 8.0, "tags": ["a", "b"], "openedOn": "2026-01-02",
         "status": "active"},
        {"thingId": "t2", "ownerId": "o2", "amount": money(-5), "rate": 7.75, "tags": [], "openedOn": None}])
    put("data/releases/r2/things.json", [{"thingId": "t1", "ownerId": "o1", "name": "Alpha again", "amount": None, "rate": 1, "tags": ["z"]}])
    return Repo(root, "r1", {"r1": "frontier", "r2": "legacy"})


def test_a_tiny_seed_builds_exact_typed_rows_with_namespaced_ids(tmp_path):
    repo = write_seed(tmp_path)
    res = silver._build_table(table(), repo, [])
    assert res.errors == [] and len(res.rows) == 3
    a, b, c = res.rows
    assert (a["id"], b["id"], c["id"]) == ("Thing:t1", "Thing:t2", "Thing:t1~legacy")
    assert (a["sourceRecordId"], c["sourceRecordId"]) == ("t1", "t1")
    assert a["label"] == "Alpha" and b["label"] == "t2" and c["label"] == "Alpha again"
    assert (a["state"], b["state"]) == ("active", "recorded") and a["sourceKind"] == "bank_record" and a["historical"] is False
    assert a["amountMinor"] == 12345 and a["currency"] == "INR" and b["amountMinor"] == -5 and c["amountMinor"] is None and c["currency"] is None
    assert (a["rate"], b["rate"], c["rate"]) == ("8", "7.75", "1")
    assert (a["tags"], b["tags"], c["tags"]) == ("a; b", None, "z")
    assert a["openedOn"] == dt.date(2026, 1, 2) and b["openedOn"] is None
    assert (a["releaseBusinessDate"], c["releaseBusinessDate"]) == (dt.date(2026, 1, 31), dt.date(2026, 2, 28))
    assert [r["_release_id"] for r in res.rows] == ["r1", "r1", "r2"] and [r["_source_row"] for r in res.rows] == [1, 2, 1]
    assert {r["_source_file"] for r in res.rows} == {"data/releases/r1/things.json", "data/releases/r2/things.json"} == set(res.files)
    assert [r["_owner_id"] for r in res.rows] == ["o1", "o2", "o1"]
    tbl = silver.to_arrow(res)
    assert tbl.schema.field("amountMinor").type == pa.int64() and tbl.schema.field("thingId").nullable is False
    assert tbl.schema.field("rate").type == pa.string() and tbl.schema.field("openedOn").type == pa.date32()


def test_silver_never_drops_or_fills_instead_of_failing(tmp_path):
    repo = write_seed(tmp_path)
    twice = table(sources=[{"release": "r1", "file": "things.json"}, {"release": "r1", "file": "things.json"}])
    assert any("share the business key" in e for e in silver._build_table(twice, repo, []).errors)
    no_key = table(key=["name"])                                          # t2 has no name: the business key cannot be derived
    assert any("null business key" in e or "cannot be derived" in e for e in silver._build_table(no_key, repo, []).errors)
    wrong = table()
    wrong["columns"] = [c if c["col"] != "currency" else {**c, "from": "ownerId"} for c in wrong["columns"]]
    assert any("disagree with the currency column" in e for e in silver._build_table(wrong, repo, []).errors)
    text_amount = table()
    text_amount["columns"] = [*text_amount["columns"], {"col": "tagCount", "origin": "extension", "type": "integer", "from": "name"}]
    assert any("not a whole number" in e for e in silver._build_table(text_amount, repo, []).errors)


def test_a_money_amount_without_any_currency_fails_the_build(tmp_path):
    write_seed(tmp_path)
    seed = tmp_path / "data/releases/r1/things.json"
    seed.write_text(json.dumps([{"thingId": "n1", "ownerId": "o1", "amount": {"amountMinor": 500}}]), encoding="utf-8")   # an amount, no currency
    res = silver._build_table(table(sources=[{"release": "r1", "file": "things.json"}]), Repo(tmp_path, "r1", {"r1": "frontier"}), [])
    assert any("no currency is recorded" in e for e in res.errors)
    seed.write_text(json.dumps([{"thingId": "n2", "ownerId": "o1", "amount": {"amountMinor": None}}, {"thingId": "n3", "ownerId": "o1", "amount": None}]),
                    encoding="utf-8")                                           # a new Repo: the first one caches the files it has read
    none = silver._build_table(table(sources=[{"release": "r1", "file": "things.json"}]), Repo(tmp_path, "r1", {"r1": "frontier"}), [])
    assert none.errors == [] and [r["amountMinor"] for r in none.rows] == [None, None]     # no amount: no currency needed


def test_a_closed_record_is_historical_with_the_history_kind(tmp_path):
    repo = write_seed(tmp_path)
    statuses = ("closed", "matured", "active")
    (tmp_path / "data/releases/r1/things.json").write_text(json.dumps(
        [{"thingId": f"c{i}", "ownerId": "o1", "status": s, "amount": None} for i, s in enumerate(statuses, start=1)]), encoding="utf-8")
    t = table(sources=[{"release": "r1", "file": "things.json"}],
              common=common(state={"from": "status", "default": "recorded"}, historical={"state_in": "inactive", "source_kind": "history"}))
    res = silver._build_table(t, repo, [])
    assert res.errors == []
    assert [(r["state"], r["historical"], r["sourceKind"]) for r in res.rows] == [
        ("closed", True, "history"), ("matured", True, "history"), ("active", False, "bank_record")]


def test_a_state_that_is_not_text_fails_the_build_instead_of_being_guessed(tmp_path):
    repo = write_seed(tmp_path)
    (tmp_path / "data/releases/r1/things.json").write_text(json.dumps([{"thingId": "c1", "ownerId": "o1", "status": 7, "amount": None}]),
                                                          encoding="utf-8")
    res = silver._build_table(table(sources=[{"release": "r1", "file": "things.json"}]), repo, [])
    assert any("state is not text" in e for e in res.errors)


def test_official_name_alias_columns_follow_apply_alias_row_by_row(tmp_path):
    repo = write_seed(tmp_path)
    alias = cdm_crosswalk.Alias("Thing", "amountMinor", "Ref", "balance", "currency", "derived_major_units", "minor_to_major_decimal",
                                "decimal(38,4)", "same amount in rupees", "official description", "INR major units", "null stays null", None)
    res = silver._build_table(table(), repo, [alias])
    names = [c["col"] for c in res.columns]
    assert res.errors == [] and names[7:9] == ["balance", "thingId"]
    assert [r["balance"] for r in res.rows] == [Decimal("123.4500"), Decimal("-0.0500"), None]
    assert [r["balance"] for r in res.rows] == [cdm_crosswalk.apply_alias(alias, r["amountMinor"]) for r in res.rows]
    tbl = silver.to_arrow(res)
    assert tbl.schema.field("balance").type == pa.decimal128(38, 4) and tbl.schema.field("balance").nullable
    broken = cdm_crosswalk.Alias("Thing", "name", "Ref", "label2", "string", "exact", "minor_to_major_decimal", "string", "r", "e", None, "n", None)
    assert any("alias label2" in e for e in silver._build_table(table(), repo, [broken]).errors)


def test_the_drafting_tool_refuses_to_overwrite_the_curated_specs(monkeypatch):
    spec = importlib.util.spec_from_file_location("draft_silver_spec", KIT / "tools" / "draft_silver_spec.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    monkeypatch.setattr(sys, "argv", ["draft_silver_spec.py", "--repo", str(KIT), "--out", str(KIT / "model" / "silver")])
    with pytest.raises(SystemExit, match="curated by hand"):
        tool.main()
