"""Official-name alias crosswalk (model/cdm_crosswalk.yaml): every alias is real, reviewed and consistent.

The checks against the vendored official CDM files use the read-only source repo like the other repo-backed tests (the
`repo_path` fixture skips without it) and parse those files independently of the module under test. The apply_alias, validator,
vocabulary and layout tests need no repo. The row-level check (the alias column equals the base column after the declared
transform, for every silver row) belongs to the silver tests and is deliberately not here.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from contoso_onto import cdm_crosswalk as cx

KIT = Path(__file__).resolve().parent.parent
YAML_PATH = KIT / "model" / "cdm_crosswalk.yaml"

# The reviewed result. Changing the match list changes these numbers on purpose: update them, and the implementation notes in
# docs/MEDALLION_DESIGN.md, together with the YAML. Customer and Document are reviewed and carry no alias: the vendored
# Contact defines only a GUID, and CustomDocument only a loan-application request category and a description.
EXPECTED_COUNTS = {
    "Customer": 0,
    "Account": 4,
    "Card": 3,
    "Deposit": 3,
    "Loan": 6,
    "Document": 0,
}
ENTITY_SIZES = {
    "Contact": 1,
    "Relationship": 13,
    "FHAccount": 38,
    "FICard": 32,
    "FHSaving": 38,
    "FHLoan": 68,
    "CustomDocument": 11,
}
EXPECTED_MATCHES = {
    "exact": 1,
    "semantic": 6,
    "derived_major_units": 7,
    "type_narrowed": 2,
}

MATCHES = {"exact", "semantic", "derived_major_units", "type_narrowed"}
TRANSFORMS = {"identity", "minor_to_major_decimal", "date_keep"}
SILVER_TYPES = {"string", "int64", "bool", "date32", "timestamp_utc", "decimal(38,4)"}
ATTR_KEYS = [
    "profile",
    "official",
    "official_type",
    "match",
    "transform",
    "silver_type",
    "unit",
    "null_rule",
    "rationale",
    "evidence",
]
CATEGORIES = [
    "guid_or_lookup",
    "system_column",
    "option_set",
    "converted_currency_twin",
    "no_counterpart",
]
SYSTEM_COLUMNS = {
    "createdOn",
    "modifiedOn",
    "statecode",
    "statuscode",
    "importSequenceNumber",
    "overriddenCreatedOn",
    "timeZoneRuleVersionNumber",
    "UTCConversionTimeZoneCode",
}
TWIN_RE = re.compile(
    r"(_base|_default|default|exchangerate|defaultdisplayvalue)$", re.IGNORECASE
)
BASE_SILVER = {
    "string": "string",
    "integer": "int64",
    "boolean": "bool",
    "date": "date32",
    "datetime": "timestamp_utc",
    "minor": "int64",
    "decimal": "string",
}
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1


def expected_category(name: str, official_type: str) -> str:
    """The category an official attribute that is not aliased must be listed under (precedence as listed)."""
    if name in SYSTEM_COLUMNS:
        return "system_column"
    if official_type in {"entityId", "customer"}:
        return "guid_or_lookup"
    if official_type == "listLookup":
        return "option_set"
    if TWIN_RE.search(name):
        return "converted_currency_twin"
    return "no_counterpart"


def who(a: cx.Alias) -> str:
    return f"{a.profile_entity}.{a.profile_attribute} -> {a.official_attribute}"


def skipped_names(doc: dict) -> dict[str, dict[str, list[str]]]:
    """skipped.official_attributes as {official entity: {category: [attribute names]}}."""
    return {
        entity: {
            category: [i["attribute"] if isinstance(i, dict) else i for i in items]
            for category, items in categories.items()
        }
        for entity, categories in doc["skipped"]["official_attributes"].items()
    }


@pytest.fixture(scope="module")
def doc() -> dict:
    return yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def crosswalk() -> dict:
    return cx.load_crosswalk()


@pytest.fixture(scope="module")
def aliases(crosswalk) -> list:
    return [a for items in crosswalk.values() for a in items]


@pytest.fixture(scope="module")
def banking(repo_path) -> Path:
    return repo_path / "data" / "banking-model" / "v1"


@pytest.fixture(scope="module")
def profile(banking) -> dict:
    return json.loads((banking / "profile.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sources(banking) -> dict:
    return json.loads((banking / "sources.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def official(banking) -> dict:
    """An independent parse of the vendored official files: entity -> file and {attribute: {type, description}}."""
    out: dict[str, dict] = {}
    for file in sorted((banking / "vendor" / "cdm" / "references").glob("*.cdm.json")):
        for definition in json.loads(file.read_text(encoding="utf-8"))["definitions"]:
            if "entityName" not in definition:
                continue
            (group,) = definition["hasAttributes"]
            attributes = {}
            for member in group["attributeGroupReference"]["members"]:
                data_type = member["dataType"]
                attributes[member["name"]] = {
                    "type": data_type
                    if isinstance(data_type, str)
                    else data_type["dataTypeReference"],
                    "description": member["description"],
                }
            out[definition["entityName"]] = {"file": file, "attributes": attributes}
    return out


# --------------------------------------------------------------------------------------------------
# the committed match list (no repo needed)
# --------------------------------------------------------------------------------------------------
def test_the_reviewed_entities_and_alias_counts(crosswalk, aliases):
    assert list(crosswalk) == list(EXPECTED_COUNTS)
    assert {e: len(a) for e, a in crosswalk.items()} == EXPECTED_COUNTS
    assert crosswalk["Customer"] == [] and crosswalk["Document"] == []
    counts = {m: sum(1 for a in aliases if a.match == m) for m in EXPECTED_MATCHES}
    assert counts == EXPECTED_MATCHES


def test_every_alias_documents_itself(aliases):
    problems = [
        f"{who(a)}: empty {field}"
        for a in aliases
        for field in ("rationale", "evidence", "unit", "null_rule")
        if not isinstance(getattr(a, field), str) or not getattr(a, field).strip()
    ]
    assert problems == []


def test_vocabularies_are_closed_and_consistent(aliases):
    for a in aliases:
        assert a.match in MATCHES, who(a)
        assert a.transform in TRANSFORMS, who(a)
        assert a.silver_type in SILVER_TYPES, who(a)
        # derived_major_units <-> minor_to_major_decimal <-> official currency <-> decimal(38,4)
        derived = {
            a.match == "derived_major_units",
            a.transform == "minor_to_major_decimal",
            a.official_type == "currency",
            a.silver_type == "decimal(38,4)",
        }
        assert len(derived) == 1, who(a)
        # type_narrowed <-> date_keep <-> a plain date kept under an official dateTime
        narrowed = {
            a.match == "type_narrowed",
            a.transform == "date_keep",
            a.official_type == "dateTime" and a.silver_type == "date32",
        }
        assert len(narrowed) == 1, who(a)
        if a.transform == "identity":
            assert a.match in {"exact", "semantic"}, who(a)


def test_decimals_appear_only_for_official_currency(aliases):
    for a in aliases:
        assert a.silver_type.startswith("decimal") == (a.official_type == "currency"), (
            who(a)
        )
        if a.official_type == "currency":
            assert a.silver_type == "decimal(38,4)", who(a)


def test_a_date_only_source_stays_a_date(aliases):
    kept = [a for a in aliases if a.official_type == "dateTime"]
    assert {(a.profile_entity, a.profile_attribute) for a in kept} == {
        ("Deposit", "maturesOn"),
        ("Loan", "maturesOn"),
    }
    for a in kept:
        assert (a.silver_type, a.transform) == ("date32", "date_keep"), who(a)
        note = a.type_note or ""
        assert "date" in note and "timezone" in note, who(a)


def test_a_type_note_explains_every_representation_change(aliases):
    for a in aliases:
        changed = a.official_type == "decimal" or (
            a.official_type == "dateTime" and a.silver_type == "date32"
        )
        assert bool((a.type_note or "").strip()) == changed, who(a)
        if a.official_type == "decimal":
            assert "canonical decimal text" in a.type_note, who(a)


def test_no_profile_attribute_is_mapped_twice_and_no_official_attribute_targeted_twice(
    crosswalk,
):
    for entity, items in crosswalk.items():
        profile_names = [a.profile_attribute for a in items]
        official_names = [a.official_attribute.lower() for a in items]
        assert len(set(profile_names)) == len(profile_names), entity
        assert len(set(official_names)) == len(official_names), entity


def test_no_alias_targets_a_guid_lookup_option_set_twin_or_system_column(aliases, doc):
    skipped = skipped_names(doc)
    for a in aliases:
        assert a.official_type not in {"entityId", "customer", "listLookup"}, who(a)
        assert a.official_attribute not in SYSTEM_COLUMNS, who(a)
        assert not TWIN_RE.search(a.official_attribute), who(a)
        listed = {n for names in skipped[a.official_entity].values() for n in names}
        assert a.official_attribute not in listed, who(a)


def test_the_file_layout_is_deterministic(doc):
    # a Windows checkout may use CRLF (core.autocrlf); git stores LF
    raw = YAML_PATH.read_bytes().replace(b"\r\n", b"\n")
    assert b"\r" not in raw and b"\t" not in raw
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    assert list(doc) == ["crosswalk", "skipped"]
    for entry in doc["crosswalk"]:
        keys = ["profile_entity", "official_entity", "attributes"]
        assert list(entry) == keys + (["note"] if "note" in entry else []), entry
        assert entry["attributes"] or entry.get("note", "").strip(), entry["profile_entity"]
        for attr in entry["attributes"]:
            keys = ATTR_KEYS + (["type_note"] if "type_note" in attr else [])
            assert list(attr) == keys, (entry["profile_entity"], attr["profile"])
    assert list(doc["skipped"]) == ["categories", "entities", "official_attributes"]
    assert list(doc["skipped"]["categories"]) == CATEGORIES
    for entity, categories in doc["skipped"]["official_attributes"].items():
        assert list(categories) == [c for c in CATEGORIES if c in categories], entity


def test_skipped_entries_carry_reasons(doc):
    skipped = doc["skipped"]
    assert all(text.strip() for text in skipped["categories"].values())
    for entry in skipped["entities"]:
        assert list(entry) == ["profile_entity", "official_entity", "reason"]
        assert entry["reason"].strip()
    for categories in skipped["official_attributes"].values():
        for item in categories.get("no_counterpart", []):
            assert list(item) == ["attribute", "reason"] and item["reason"].strip()
        for category, items in categories.items():
            if category != "no_counterpart":
                assert items and all(isinstance(i, str) for i in items), category


def test_the_wording_never_claims_conformance_or_certification(doc):
    texts = list(doc["skipped"]["categories"].values())
    texts += [e["reason"] for e in doc["skipped"]["entities"]]
    for categories in doc["skipped"]["official_attributes"].values():
        texts += [i["reason"] for i in categories.get("no_counterpart", [])]
    for entry in doc["crosswalk"]:
        texts.append(entry.get("note", ""))
        for attr in entry["attributes"]:
            # `evidence` is the official text; everything else is ours
            texts += [attr[k] for k in ("unit", "null_rule", "rationale", "type_note") if k in attr]
    assert [t for t in texts if re.search(r"conform|certif", t, re.IGNORECASE)] == []


def test_the_crosswalk_keys_do_not_collide_with_the_merged_model(doc):
    # gold.load_model merges every model/*.yaml and model/gold/*.yaml, so the two top-level keys must be unique
    others: set[str] = set()
    for path in [*(KIT / "model").glob("*.yaml"), *(KIT / "model" / "gold").glob("*.yaml")]:
        if path != YAML_PATH:
            others |= set(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    assert not set(doc) & others


# --------------------------------------------------------------------------------------------------
# loader and index helpers (no repo needed)
# --------------------------------------------------------------------------------------------------
GOOD_ATTR = {
    "profile": "a",
    "official": "b",
    "official_type": "string",
    "match": "exact",
    "transform": "identity",
    "silver_type": "string",
    "null_rule": "n",
    "rationale": "r",
    "evidence": "e",
}


def entry(*attributes, entity="E"):
    return {
        "profile_entity": entity,
        "official_entity": "O",
        "attributes": list(attributes),
    }


def test_the_loader_defaults_the_optional_keys_and_keeps_empty_entities():
    parsed = cx._parse({"crosswalk": [entry(GOOD_ATTR), entry(entity="F")]}, "inline")
    assert list(parsed) == ["E", "F"] and parsed["F"] == []
    (alias,) = parsed["E"]
    assert (alias.unit, alias.type_note) == (None, None)
    assert (alias.profile_entity, alias.official_entity) == ("E", "O")


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        ({}, "top-level 'crosswalk' list"),
        ({"crosswalk": {}}, "top-level 'crosswalk' list"),
        ({"crosswalk": ["E"]}, "expected a mapping"),
        ({"crosswalk": [{"profile_entity": "E"}]}, "missing keys"),
        (
            {"crosswalk": [entry(GOOD_ATTR), entry(GOOD_ATTR)]},
            "E appears twice",
        ),
        (
            {"crosswalk": [entry({k: v for k, v in GOOD_ATTR.items() if k != "rationale"})]},
            r"missing keys \['rationale'\]",
        ),
        (
            {"crosswalk": [entry({**GOOD_ATTR, "extra": 1})]},
            r"unknown keys \['extra'\]",
        ),
        ({"crosswalk": [{**entry(GOOD_ATTR), "extra": 1}]}, "unknown keys"),
    ],
)
def test_the_loader_rejects_structural_mistakes(doc, message):
    with pytest.raises(ValueError, match=message):
        cx._parse(doc, "inline")


def test_attribute_groups_are_resolved_including_nested_ones():
    nested = [
        {
            "attributeGroupReference": {
                "attributeGroupName": "outer",
                "members": [
                    {"name": "a", "dataType": "string"},
                    {
                        "attributeGroupReference": {
                            "members": [{"name": "b", "dataType": "currency"}]
                        }
                    },
                ],
            }
        },
        {"name": "c", "dataType": {"dataTypeReference": "listLookup"}},
    ]
    names = [m["name"] for m in cx._members(nested, "inline")]
    assert names == ["a", "b", "c"]
    assert cx._type_name({"dataTypeReference": "listLookup", "appliedTraits": []}, "x") == (
        "listLookup"
    )


def test_unresolvable_shapes_are_errors_not_silent_gaps():
    with pytest.raises(ValueError, match="referenced, not defined inline"):
        list(cx._members([{"attributeGroupReference": "cdm:/core/other.cdm.json"}], "x"))
    with pytest.raises(ValueError, match="unsupported attribute shape"):
        list(cx._members([{"entity": {"entityReference": "Other"}}], "x"))
    with pytest.raises(ValueError, match="unsupported dataType"):
        cx._type_name({"dataTypeName": "inline"}, "x")


def test_a_folder_without_the_reference_files_is_a_clear_error():
    with pytest.raises(FileNotFoundError, match="official CDM reference files"):
        cx.official_index(KIT)


# --------------------------------------------------------------------------------------------------
# apply_alias (no repo needed)
# --------------------------------------------------------------------------------------------------
def make(**changes) -> cx.Alias:
    values = {
        "profile_entity": "Thing",
        "profile_attribute": "thingId",
        "official_entity": "FHThing",
        "official_attribute": "integrationKey",
        "official_type": "string",
        "match": "semantic",
        "transform": "identity",
        "silver_type": "string",
        "rationale": "the same business key",
        "evidence": "Used by the system integrator to uniquely define the data record.",
        "unit": "none (identifier text)",
        "null_rule": "null only when the base is null",
        "type_note": None,
    }
    values.update(changes)
    return cx.Alias(**values)


MINOR = make(
    profile_attribute="amountMinor",
    official_attribute="amount",
    official_type="currency",
    match="derived_major_units",
    transform="minor_to_major_decimal",
    silver_type="decimal(38,4)",
)
DATE_KEEP = make(
    profile_attribute="dueOn",
    official_attribute="dueDate",
    official_type="dateTime",
    match="type_narrowed",
    transform="date_keep",
    silver_type="date32",
    type_note="a date stays a date",
)


@pytest.mark.parametrize(
    ("paise", "rupees"),
    [
        (0, "0.00"),
        (1, "0.01"),
        (-1, "-0.01"),
        (99, "0.99"),
        (100, "1.00"),
        (100830000, "1008300.00"),
        (-4745400, "-47454.00"),
        (INT64_MAX, "92233720368547758.07"),
        (INT64_MIN, "-92233720368547758.08"),
    ],
)
def test_minor_units_become_exact_decimal_major_units(paise, rupees):
    got = cx.apply_alias(MINOR, paise)
    assert isinstance(got, Decimal) and str(got) == rupees and got == Decimal(rupees)
    sign, digits, exponent = got.as_tuple()
    assert exponent == -2
    assert (-1 if sign else 1) * int("".join(map(str, digits))) == paise


def test_the_conversion_is_exact_whatever_the_decimal_context_says():
    with decimal.localcontext() as ctx:
        ctx.prec = 3
        ctx.rounding = decimal.ROUND_DOWN
        assert str(cx.apply_alias(MINOR, INT64_MAX)) == "92233720368547758.07"
        assert str(cx.apply_alias(MINOR, INT64_MIN)) == "-92233720368547758.08"


def test_the_major_units_fit_the_declared_arrow_type_and_a_narrower_one_would_not():
    pa = pytest.importorskip("pyarrow")
    values = [INT64_MIN, -1, 0, 1, INT64_MAX]
    expected = [cx.apply_alias(MINOR, v) for v in values]
    assert pa.array(expected, type=pa.decimal128(38, 4)).to_pylist() == expected
    # int64 paise / 100 has 17 integral digits, so decimal(19,4) is too small (design doc section 5)
    with pytest.raises(pa.ArrowInvalid):
        pa.array(expected, type=pa.decimal128(19, 4))


@pytest.mark.parametrize(
    "bad",
    [True, False, 12.0, 12.5, float("nan"), "100", b"1", Decimal("1"), [1], (1,)],
)
def test_minor_units_refuse_anything_but_an_integer(bad):
    with pytest.raises(TypeError):
        cx.apply_alias(MINOR, bad)


@pytest.mark.parametrize("bad", [INT64_MAX + 1, INT64_MIN - 1, 2**100])
def test_minor_units_outside_int64_are_refused(bad):
    with pytest.raises(ValueError, match="signed 64-bit"):
        cx.apply_alias(MINOR, bad)


def test_integer_like_values_such_as_numpy_ints_are_accepted():
    class Paise:
        def __index__(self) -> int:
            return 1234

    assert cx.apply_alias(MINOR, Paise()) == Decimal("12.34")


def test_none_stays_none_for_every_transform_and_every_committed_alias(aliases):
    for a in [*aliases, MINOR, DATE_KEEP, make()]:
        assert cx.apply_alias(a, None) is None, who(a)


def test_identity_returns_the_value_unchanged_after_a_type_check():
    text = make()
    assert cx.apply_alias(text, "acc-rhea-primary") == "acc-rhea-primary"
    assert cx.apply_alias(text, "") == ""  # empty text is not null
    for bad in (5, True, b"x", None.__class__):
        with pytest.raises(TypeError):
            cx.apply_alias(text, bad)
    number = make(official_type="integer", silver_type="int64")
    assert cx.apply_alias(number, 7) == 7 and cx.apply_alias(number, INT64_MIN) == INT64_MIN
    with pytest.raises(TypeError):
        cx.apply_alias(number, True)
    with pytest.raises(ValueError):
        cx.apply_alias(number, INT64_MAX + 1)
    flag = make(official_type="boolean", silver_type="bool")
    assert cx.apply_alias(flag, False) is False
    with pytest.raises(TypeError):
        cx.apply_alias(flag, 1)
    instant = make(official_type="dateTime", silver_type="timestamp_utc")
    aware = dt.datetime(2026, 9, 30, 12, 0, tzinfo=dt.timezone.utc)
    assert cx.apply_alias(instant, aware) is aware
    with pytest.raises(TypeError):
        cx.apply_alias(instant, dt.datetime(2026, 9, 30, 12, 0))  # never assign a timezone


def test_identity_cannot_produce_a_type_it_does_not_keep():
    with pytest.raises(ValueError, match="cannot have silver_type date32"):
        cx.apply_alias(make(silver_type="date32"), "2026-09-30")


def test_date_keep_keeps_a_plain_date_and_refuses_anything_with_a_time():
    day = dt.date(2027, 2, 15)
    assert cx.apply_alias(DATE_KEEP, day) is day
    for bad in (
        dt.datetime(2027, 2, 15),
        dt.datetime(2027, 2, 15, tzinfo=dt.timezone.utc),
        "2027-02-15",
        20270215,
    ):
        with pytest.raises(TypeError, match="plain date"):
            cx.apply_alias(DATE_KEEP, bad)


def test_an_unknown_transform_is_an_error():
    with pytest.raises(ValueError, match="unknown transform 'cast'"):
        cx.apply_alias(make(transform="cast"), "x")


# --------------------------------------------------------------------------------------------------
# validate() on a small synthetic official entity: the validator is not vacuous (no repo needed)
# --------------------------------------------------------------------------------------------------
SYN_INDEX = {
    "FHThing": {
        "FH_ThingId": "entityId",
        "createdOn": "dateTime",
        "statecode": "listLookup",
        "Cardholder": "customer",
        "integrationKey": "string",
        "Label": "string",
        "bad-name": "string",
        "amount": "currency",
        "amount_Base": "currency",
        "amountDefault": "decimal",
        "amountExchangerate": "decimal",
        "dueDate": "dateTime",
        "interestrate": "decimal",
        "count": "integer",
    }
}


def syn_profile(exponent: int = 2) -> dict:
    return {
        "commonEntityColumns": ["id", "label", "state"],
        "money": {"exponent": exponent},
        "entityDefinitions": [
            {
                "name": "Thing",
                "cdmReference": {"entityName": "FHThing"},
                "attributes": [
                    {"name": "thingId", "type": "string"},
                    {"name": "amountMinor", "type": "minor"},
                    {"name": "dueOn", "type": "date"},
                    {"name": "ratePct", "type": "decimal"},
                    {"name": "quantity", "type": "integer"},
                ],
            },
            {
                "name": "Plain",
                "cdmReference": None,
                "attributes": [{"name": "x", "type": "string"}],
            },
        ],
    }


GOOD = [
    make(),
    make(
        profile_attribute="amountMinor",
        official_attribute="amount",
        official_type="currency",
        match="derived_major_units",
        transform="minor_to_major_decimal",
        silver_type="decimal(38,4)",
    ),
    make(
        profile_attribute="dueOn",
        official_attribute="dueDate",
        official_type="dateTime",
        match="type_narrowed",
        transform="date_keep",
        silver_type="date32",
        type_note="a date stays a date",
    ),
    make(
        profile_attribute="ratePct",
        official_attribute="interestrate",
        official_type="decimal",
        silver_type="string",
        type_note="canonical decimal text",
    ),
]


def check(crosswalk, exponent: int = 2) -> list[str]:
    return cx.validate(crosswalk, SYN_INDEX, syn_profile(exponent))


def mutated(index: int, **changes) -> dict:
    items = list(GOOD)
    items[index] = dataclasses.replace(items[index], **changes)
    return {"Thing": items}


def test_a_valid_crosswalk_has_no_errors_and_an_empty_entity_is_fine():
    assert check({"Thing": GOOD}) == []
    assert check({"Thing": [], "Plain": []}) == []


MUTATIONS = [
    (0, {"official_attribute": "nope"}, "defines no attribute nope"),
    (0, {"official_type": "integer"}, "vendored file says string"),
    (0, {"official_attribute": "FH_ThingId", "official_type": "entityId"}, "never aliased"),
    (0, {"official_attribute": "statecode", "official_type": "listLookup"}, "never aliased"),
    (0, {"official_attribute": "Cardholder", "official_type": "customer"}, "never aliased"),
    (0, {"official_attribute": "createdOn", "official_type": "dateTime"}, "system columns"),
    (0, {"official_attribute": "amount_Base", "official_type": "currency"}, "converted-currency"),
    (0, {"official_attribute": "amountDefault", "official_type": "decimal"}, "converted-currency"),
    (0, {"official_attribute": "amountExchangerate", "official_type": "decimal"}, "converted-currency"),
    (0, {"official_entity": "FHOther"}, "must be the profile's cdmReference.entityName FHThing"),
    (0, {"official_entity": "FHOther"}, "not in the official index"),
    (0, {"profile_attribute": "ghost"}, "no such attribute"),
    (0, {"rationale": ""}, "rationale must be a non-empty text"),
    (0, {"evidence": "   "}, "evidence must be a non-empty text"),
    (0, {"unit": None}, "unit must be a non-empty text"),
    (0, {"null_rule": ""}, "null_rule must be a non-empty text"),
    (0, {"match": "close enough"}, "match 'close enough' is not one of"),
    (0, {"transform": "cast"}, "transform 'cast' is not one of"),
    (0, {"silver_type": "decimal(19,4)"}, "silver_type 'decimal(19,4)' is not one of"),
    (0, {"official_attribute": "Label"}, "collides, ignoring case"),
    (0, {"official_attribute": "bad-name"}, "not a valid silver column name"),
    (0, {"match": "derived_major_units"}, "needs the minor_to_major_decimal transform"),
    (0, {"match": "type_narrowed"}, "identity keeps the value unchanged"),
    (0, {"official_type": "string", "silver_type": "int64"}, "identity needs the alias type"),
    (1, {"profile_attribute": "quantity"}, "needs a minor profile attribute"),
    (1, {"transform": "identity", "match": "semantic", "silver_type": "string"}, "only reachable through minor_to_major_decimal"),
    (1, {"silver_type": "string"}, "makes a currency stored as decimal(38,4)"),
    (2, {"type_note": None}, "type_note is required"),
    (2, {"profile_attribute": "thingId"}, "needs a date profile attribute"),
    (2, {"silver_type": "timestamp_utc"}, "keeps a date (date32)"),
    (3, {"type_note": None}, "type_note is required"),
    (3, {"silver_type": "decimal(38,4)"}, "decimal silver types are only for official currency"),
]


@pytest.mark.parametrize(("index", "changes", "message"), MUTATIONS)
def test_validate_reports_each_kind_of_mistake(index, changes, message):
    errors = check(mutated(index, **changes))
    assert any(message in e for e in errors), (message, errors)
    assert all(e.startswith("Thing.") for e in errors)


def test_validate_checks_the_money_exponent_the_transform_divides_by():
    errors = check({"Thing": GOOD}, exponent=3)
    assert [e for e in errors if "money exponent is 3" in e]


def test_validate_reports_duplicates_and_misfiled_aliases():
    twice = make(official_attribute="count", official_type="integer", silver_type="int64")
    assert any("mapped twice" in e for e in check({"Thing": [*GOOD, twice]}))
    again = make(profile_attribute="quantity", official_attribute="INTEGRATIONKEY")
    assert any("targeted twice" in e for e in check({"Thing": [*GOOD, again]}))
    assert any(
        "names profile entity Other" in e
        for e in check({"Thing": [make(profile_entity="Other")]})
    )


def test_validate_requires_a_profile_entity_with_a_cdm_reference():
    assert any("no cdmReference" in e for e in check({"Plain": [make(profile_entity="Plain")]}))
    assert any("not an entity of the profile" in e for e in check({"Nope": []}))


def test_validate_reports_every_problem_not_just_the_first():
    broken = mutated(0, official_attribute="nope", rationale="", match="close enough")
    assert len(check(broken)) >= 3


def test_validate_reports_non_text_values_instead_of_crashing():
    errors = check(mutated(0, official_attribute=123, rationale=None))
    assert any("defines no attribute 123" in e for e in errors)
    assert any("rationale must be a non-empty text" in e for e in errors)


# --------------------------------------------------------------------------------------------------
# against the vendored official files (repo-backed)
# --------------------------------------------------------------------------------------------------
def test_official_index_matches_an_independent_parse(repo_path, official):
    index = cx.official_index(repo_path)
    assert list(index) == list(official)
    for entity, parsed in official.items():
        assert list(index[entity].items()) == [
            (name, attr["type"]) for name, attr in parsed["attributes"].items()
        ], entity


def test_official_index_knows_the_shapes_the_crosswalk_relies_on(repo_path):
    index = cx.official_index(str(repo_path))
    assert index == cx.official_index(repo_path)
    assert {e: len(index[e]) for e in ENTITY_SIZES} == ENTITY_SIZES
    assert index["Contact"] == {"contactId": "entityId"}
    assert index["FHAccount"]["statecode"] == "listLookup"
    assert index["FHAccount"]["balance"] == "currency"
    assert index["FHAccount"]["balance_Base"] == "currency"
    assert index["FHAccount"]["balanceDefault"] == "decimal"
    assert index["FICard"]["Cardholder"] == "customer"
    assert index["FHSaving"]["maturitydate"] == "dateTime"


def test_every_alias_targets_an_attribute_that_exists_with_the_declared_type(
    aliases, official
):
    for a in aliases:
        attributes = official[a.official_entity]["attributes"]
        assert a.official_attribute in attributes, who(a)
        assert attributes[a.official_attribute]["type"] == a.official_type, who(a)


def test_evidence_is_the_official_description_verbatim(aliases, official):
    for a in aliases:
        official_text = official[a.official_entity]["attributes"][a.official_attribute]
        assert a.evidence == official_text["description"], who(a)


def test_official_entities_are_the_profile_references_and_the_files_the_pinned_originals(
    doc, profile, sources, banking, official
):
    refs = {
        e["name"]: e["cdmReference"]
        for e in profile["entityDefinitions"]
        if e.get("cdmReference")
    }
    claimed = [
        (e["profile_entity"], e["official_entity"])
        for e in [*doc["crosswalk"], *doc["skipped"]["entities"]]
    ]
    for profile_entity, official_entity in claimed:
        assert refs[profile_entity]["entityName"] == official_entity, profile_entity
    artifacts = {a["entityName"]: a for a in sources["artifacts"] if "entityName" in a}
    for official_entity in {o for _, o in claimed}:
        ref = next(r for r in refs.values() if r["entityName"] == official_entity)
        artifact = artifacts[official_entity]
        path = banking / artifact["path"]
        assert artifact["path"] == ref["artifact"], official_entity
        assert path == official[official_entity]["file"], official_entity
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == artifact["sha256"] == ref["sha256"], official_entity


def test_the_yaml_names_the_pinned_cdm_commit(sources):
    header = YAML_PATH.read_text(encoding="utf-8").split("\ncrosswalk:", 1)[0]
    assert sources["cdmCommit"] in header
    commits = {a["commit"] for a in sources["artifacts"] if "entityName" in a}
    assert commits == {sources["cdmCommit"]}


def test_every_profile_cdm_reference_is_reviewed_exactly_once(doc, profile):
    referencing = [e["name"] for e in profile["entityDefinitions"] if e.get("cdmReference")]
    reviewed = [e["profile_entity"] for e in doc["crosswalk"]]
    skipped = [e["profile_entity"] for e in doc["skipped"]["entities"]]
    assert len(referencing) == 8
    assert sorted(reviewed + skipped) == sorted(referencing)
    assert skipped == ["PartyRelationship", "EvidenceReading"]


def test_the_runtime_only_entities_have_no_seed_rows(repo_path):
    # skipped.entities says PartyRelationship and EvidenceReading are built at runtime and never seeded
    markers = ("verified_recipient", "recipientPartyId", "readId", "extractionMethod")
    for file in sorted((repo_path / "data" / "releases").rglob("*.json")):
        text = file.read_text(encoding="utf-8")
        assert not [m for m in markers if m in text], file.name


def test_the_file_follows_profile_order(doc, profile):
    order = [e["name"] for e in profile["entityDefinitions"]]
    names = [e["profile_entity"] for e in doc["crosswalk"]]
    assert names == sorted(names, key=order.index)
    skipped = [e["profile_entity"] for e in doc["skipped"]["entities"]]
    assert skipped == sorted(skipped, key=order.index)
    attributes = {
        e["name"]: [a["name"] for a in e["attributes"]] for e in profile["entityDefinitions"]
    }
    for item in doc["crosswalk"]:
        used = [a["profile"] for a in item["attributes"]]
        assert used == sorted(used, key=attributes[item["profile_entity"]].index)
    expected: list[str] = []
    for e in profile["entityDefinitions"]:
        reference = e.get("cdmReference")
        if reference and reference["entityName"] not in expected:
            expected.append(reference["entityName"])
    assert list(doc["skipped"]["official_attributes"]) == expected


def test_profile_attributes_exist_and_their_types_fit_the_transform(aliases, profile):
    assert profile["money"]["exponent"] == 2
    entities = {
        e["name"]: {a["name"]: a for a in e["attributes"]}
        for e in profile["entityDefinitions"]
    }
    for a in aliases:
        attr = entities[a.profile_entity][a.profile_attribute]
        if a.transform == "minor_to_major_decimal":
            assert attr["type"] == "minor" and "exponent 2" in attr["unit"], who(a)
        if a.transform == "date_keep":
            assert attr["type"] == "date" and "no implicit midnight" in attr["unit"], who(a)
        if a.transform == "identity":
            assert BASE_SILVER[attr["type"]] == a.silver_type, who(a)


def test_alias_names_are_valid_silver_names_that_do_not_collide_with_the_table(
    aliases, profile
):
    common = {c.lower() for c in profile["commonEntityColumns"]}
    attributes = {
        e["name"]: {a["name"].lower() for a in e["attributes"]}
        for e in profile["entityDefinitions"]
    }
    for a in aliases:
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", a.official_attribute), who(a)
        taken = attributes[a.profile_entity] | common
        assert a.official_attribute.lower() not in taken, who(a)


def test_validate_accepts_the_committed_crosswalk(crosswalk, repo_path, profile):
    assert cx.validate(crosswalk, cx.official_index(repo_path), profile) == []


def test_every_official_attribute_is_aliased_or_skipped_exactly_once(doc, official):
    aliased: dict[str, list[str]] = {}
    for item in doc["crosswalk"]:
        for attr in item["attributes"]:
            aliased.setdefault(item["official_entity"], []).append(attr["official"])
    used = {e["official_entity"] for e in [*doc["crosswalk"], *doc["skipped"]["entities"]]}
    sections = skipped_names(doc)
    assert set(sections) == used
    for entity, categories in sections.items():
        attributes = official[entity]["attributes"]
        listed: list[str] = []
        for category, names in categories.items():
            assert category in CATEGORIES and names, (entity, category)
            assert names == [n for n in attributes if n in names], (entity, category)
            for name in names:
                kind = expected_category(name, attributes[name]["type"])
                assert kind == category, (entity, name, category)
            listed += names
        mapped = aliased.get(entity, [])
        assert len(set(listed)) == len(listed), entity
        assert not set(listed) & set(mapped), entity
        assert sorted(listed + mapped) == sorted(attributes), entity
        for name in mapped:
            assert expected_category(name, attributes[name]["type"]) == "no_counterpart"
