"""Silver conformance to the superapp's CDM application profile and BIAN registry, plus the catalogs that say so. Every expectation is read
straight from the source repo's profile.json, banking.cdm.json, field-mappings.json, bian-registry.json and the app's own projection code
(not through silver.Profile or the spec), so the builder and these tests cannot share a misreading. The common columns sourceKind, state,
label and historical must equal what app/data_xray/projection.py derives. Wording rule: "official-name alias" and "semantic reference",
never certification."""
import json
import re
from collections import Counter
from decimal import Decimal

import pyarrow as pa
import pytest

from contoso_onto import cdm_crosswalk, silver, standards

COMMON = ["id", "label", "state", "sourceKind", "historical", "sourceRecordId", "sourceVersion"]
PROJECTION = "app/data_xray/projection.py"
CONTRACTS = "app/frontier/contracts.py"
FRONTIER_DATA = "app/frontier/data.py"
SOURCE_KIND_FILE = "app/data_xray/contracts.py"
ARROW = {"string": pa.string(), "integer": pa.int64(), "boolean": pa.bool_(), "date": pa.date32(), "datetime": pa.timestamp("us", tz="UTC"),
         "minor": pa.int64(), "decimal": pa.string()}
CDM_COMMON_ARROW = {"string": pa.string(), "contosoBanking.stringIdentifier": pa.string(), "boolean": pa.bool_()}
ALIAS_ARROW = {"string": pa.string(), "int64": pa.int64(), "bool": pa.bool_(), "date32": pa.date32(),
               "timestamp_utc": pa.timestamp("us", tz="UTC"), "decimal(38,4)": pa.decimal128(38, 4)}
ENTITY_CATALOG = ["entity", "silver_table", "profile_entity", "extension", "cdm_reference_entity", "cdm_reference_status", "bian_domains",
                  "source_files", "grain", "row_count"]
ATTRIBUTE_CATALOG = ["silver_table", "column_name", "origin", "profile_attribute", "cdm_type", "unit", "nullable", "privacy", "bian_domain",
                     "bian_control_record", "bian_mapping_status", "official_entity", "official_attribute", "match_type", "source_pointer",
                     "transform"]
CATALOGS = {silver.ENTITY_CATALOG, silver.ATTRIBUTE_CATALOG}
FORBIDDEN_WORDING = ("cdm conformance", "cdm compliant", "bian certified", "bian-certified", "certified by")


def read(repo, name):
    return json.loads((repo / "data" / "banking-model" / "v1" / name).read_text(encoding="utf-8-sig"))


@pytest.fixture(scope="module")
def prof(repo_path):
    return read(repo_path, "profile.json")


@pytest.fixture(scope="module")
def entities(prof):
    return {e["name"]: e for e in prof["entityDefinitions"]}


@pytest.fixture(scope="module")
def registry(repo_path):
    return read(repo_path, "bian-registry.json")


@pytest.fixture(scope="module")
def field_maps(repo_path):
    return {(f["entityType"], f["attribute"]): f for f in read(repo_path, "field-mappings.json")["fields"]}


@pytest.fixture(scope="module")
def cdm_defs(repo_path):
    return {d["entityName"]: d for d in read(repo_path, "banking.cdm.json")["definitions"]}


@pytest.fixture(scope="module")
def aligned(silver_spec):
    return {t["profile_entity"]: t for t in silver_spec["tables"] if t.get("profile_entity")}


@pytest.fixture(scope="module")
def catalog(silver_rows):
    return {(r["silver_table"], r["column_name"]): r for r in silver_rows[silver.ATTRIBUTE_CATALOG]}


def attributes(prof, entity):
    """The profile attributes silver emits as profile columns: all of them, minus the ones shared with a common column."""
    shared = {c for c, v in prof["sharedEntityColumns"].items() if entity["name"] in v["entities"]}
    return [a for a in entity["attributes"] if a["name"] not in shared]


def bian_summary(registry, entity):
    domains = {d["id"]: d for d in registry["domains"]}
    per: dict[str, dict] = {}
    for m in registry["mappings"]:
        if m["entityType"] == entity and m["mappingStatus"] != "unmapped" and m.get("domainId"):
            per.setdefault(m["domainId"], {"custom": 0, "provisional": 0})[m["mappingStatus"]] += 1
    return [{"domain": domains[k]["name"], "control_record": domains[k].get("controlRecord"), **v}
            for k, v in sorted(per.items(), key=lambda kv: domains[kv[0]]["name"])]


def status_text(b):
    return "custom, partly provisional" if b["custom"] and b["provisional"] else "provisional" if b["provisional"] else "custom"


def bian_attribute(registry, entity, attribute):
    """(domains, control records, statuses) of one attribute, '; ' joined in registry order; unmapped when the registry has no row."""
    domains = {d["id"]: d for d in registry["domains"]}
    rows = sorted((m for m in registry["mappings"] if m["entityType"] == entity and m["attribute"] == attribute),
                  key=lambda m: int(m["id"].rsplit(":", 1)[1]))
    if not rows:
        return None, None, "unmapped"
    names = [domains[m["domainId"]]["name"] for m in rows if m.get("domainId")]
    controls = [m["controlRecord"] for m in rows if m.get("controlRecord")]
    return "; ".join(names) or None, "; ".join(controls) or None, "; ".join(m["mappingStatus"] for m in rows)


def app_lines(repo, rel):
    return (repo / rel).read_text(encoding="utf-8").splitlines()


def app_vocabulary(repo):
    """(SourceKind literal, _INACTIVE) read from the pinned app source; the committed copies when the source repo is not at hand."""
    if repo is not None and (repo / SOURCE_KIND_FILE).is_file() and (repo / PROJECTION).is_file():
        kinds = re.search(r"SourceKind\s*=\s*Literal\[(.*?)\]", "\n".join(app_lines(repo, SOURCE_KIND_FILE)), re.S)
        inactive = re.search(r"_INACTIVE\s*=\s*frozenset\(\{(.*?)\}\)", "\n".join(app_lines(repo, PROJECTION)), re.S)
        return frozenset(re.findall(r'"(\w+)"', kinds.group(1))), frozenset(re.findall(r'"(\w+)"', inactive.group(1)))
    return silver.SOURCE_KINDS, silver.INACTIVE_STATES


def cited_text(repo, cites):
    """{app file: the cited lines} for citations such as app/data_xray/projection.py:516-523; every range must lie inside its file."""
    out: dict[str, str] = {}
    for cite in [cites] if isinstance(cites, str) else cites:
        file, _, span = cite.rpartition(":")
        first, _, last = span.partition("-")
        lines = app_lines(repo, file)
        assert 1 <= int(first) <= int(last or first) <= len(lines), cite
        out[file] = out.get(file, "") + "\n".join(lines[int(first) - 1:int(last or first)]) + "\n"
    return out


def normal(common):
    """A common block without its citation and with every path as a list, so the spec's spelling does not matter."""
    out = {k: v for k, v in common.items() if k != "cite"}
    out["state"] = {k: [v] if k in ("from", "when") and isinstance(v, str) else v for k, v in common["state"].items()}
    if "label" in out:
        out["label"] = {k: [v] if k == "from" and isinstance(v, str) else v for k, v in common["label"].items()}
    return out


# what app/data_xray/projection.py passes to put() for each profile entity (line numbers are the pinned commit's)
HOLDING = {"source_kind": "bank_record", "state": {"from": ["status"], "default": "recorded"},
           "historical": {"state_in": "inactive", "source_kind": "history"}}
FRONTIER_STATUS = {"source_kind": "frontier", "state": {"from": ["status"], "default": "recorded"}}
FRONTIER_NO_STATUS = {"source_kind": "frontier", "state": {"const": "recorded"}}
RULES = {
    "Customer": {"source_kind": "bank_record", "state": {"const": "customer"}, "label": {"from": ["displayName", "name"]}},
    "Product": {"source_kind": "catalogue", "state": {"from": ["status"], "default": "catalogue"}, "label": {"from": ["displayName"]}},
    "Account": HOLDING, "Card": HOLDING, "Deposit": HOLDING, "Loan": HOLDING, "ProtectionPolicy": HOLDING, "Transaction": HOLDING,
    "Goal": {**HOLDING, "label": {"from": ["title", "name"]}},
    "Preference": {"source_kind": "customer_stated", "state": {"from": ["status"], "default": "recorded"}, "label": {"from": ["field"]}},
    "Document": {"source_kind": "document_metadata", "state": {"const": "available"}, "label": {"from": ["title"], "default": "Source document"}},
    "Consent": {"source_kind": "consent", "state": {"when": ["revokedAt"], "then": "revoked", "default": "recorded"}},
    "ServiceCase": {"source_kind": "bank_record", "state": {"from": ["status"], "default": "pending"}},
    "HouseholdAccessGrant": FRONTIER_NO_STATUS, "FrontierConsent": FRONTIER_NO_STATUS, "RegisteredAgent": FRONTIER_STATUS,
    "FrontierMandate": FRONTIER_STATUS, "MandateAction": FRONTIER_STATUS, "ProactiveCandidate": FRONTIER_STATUS,
}
# (app file, text, must it appear in the cited lines): the put() arguments and contract fields each rule rests on
HOLDING_EVIDENCE = [(PROJECTION, 'state = row.get("status", "recorded")', True), (PROJECTION, "historical = closed or state in _INACTIVE", True),
                    (PROJECTION, 'source_kind="history" if historical else "bank_record"', True), (PROJECTION, "_INACTIVE = frozenset(", True)]
FRONTIER_EVIDENCE = [(PROJECTION, 'state=str(getattr(row, "status", "recorded"))', True), (PROJECTION, 'source_kind="frontier"', True)]
EVIDENCE = {
    "Customer": [(PROJECTION, 'label=persona.get("displayName"), state="customer"', True),
                 (FRONTIER_DATA, 'record.setdefault("displayName", record.get("name"))', True)],
    "Product": [(PROJECTION, 'label=row.get("displayName")', True), (PROJECTION, 'state=row.get("status", "catalogue"), source_kind="catalogue"', True)],
    "Account": HOLDING_EVIDENCE, "Card": HOLDING_EVIDENCE, "Deposit": HOLDING_EVIDENCE, "Loan": HOLDING_EVIDENCE,
    "ProtectionPolicy": HOLDING_EVIDENCE, "Transaction": HOLDING_EVIDENCE,
    "Goal": [*HOLDING_EVIDENCE, (PROJECTION, 'label=row.get("title") if entity == "Goal" else key', True)],
    "Preference": [(PROJECTION, 'label=row.get("field"), state=row.get("status", "recorded"), source_kind="customer_stated"', True)],
    "Document": [(PROJECTION, '"title": self.text(row.get("title")) or "Source document"', True),
                 (PROJECTION, 'state="retained_source" if historical else "available"', True),
                 (PROJECTION, 'source_kind="catalogue" if catalogue else "document_metadata"', True)],
    "Consent": [(PROJECTION, 'state="revoked" if raw.get("revokedAt") else "recorded", source_kind="consent"', True)],
    "ServiceCase": [(PROJECTION, 'raw.get("status", "pending")', True),
                    (PROJECTION, 'source_kind="application" if legacy else "bank_record"', True)],
    "HouseholdAccessGrant": [*FRONTIER_EVIDENCE, (CONTRACTS, "status:", False)],
    "FrontierConsent": [*FRONTIER_EVIDENCE, (CONTRACTS, "status:", False)],
    "RegisteredAgent": [*FRONTIER_EVIDENCE, (CONTRACTS, 'status: Literal["active", "suspended", "revoked"]', True)],
    "FrontierMandate": [*FRONTIER_EVIDENCE, (CONTRACTS, 'status: Literal["draft", "active", "paused", "expired", "revoked"]', True)],
    "MandateAction": [*FRONTIER_EVIDENCE, (CONTRACTS, 'status: Literal["executed", "refused", "referred"]', True)],
    "ProactiveCandidate": [*FRONTIER_EVIDENCE, (CONTRACTS, 'status: Literal["active", "suppressed", "dismissed", "completed"]', True)],
}
# extension entities: the app's put() defaults, `catalogue` for catalogue and reference data, the seed's own lifecycle field where it has one
CATALOGUE = {"ProductParameter", "Sop", "BusinessUnit", "Region", "RelationshipManager", "Campaign", "ProductBusinessUnit"}
LIFECYCLE = {"LoanInstallment": "status", "ScheduledObligation": "status", "Incident": "status", "CrmCase": "stage", "Campaign": "status"}


# ------------------------------------------------------------------------------------------------ which entities, which names
def test_profile_aligned_tables_are_the_standards_mapped_entities(silver_spec, aligned, entities):
    assert len(aligned) == sum(1 for t in silver_spec["tables"] if t.get("profile_entity"))
    assert set(aligned) <= set(entities)
    assert set(aligned) == set(standards.ONTOLOGY_ENTITY)
    for e, t in aligned.items():
        assert t["entity"] == e and t["table"] == "silver_" + silver.snake(e) and t["profile"] is True
        assert t["ontology_entity"] == standards.ONTOLOGY_ENTITY[e]


def test_commitment_is_a_documented_extension_entity(silver_spec, aligned, entities, field_maps):
    """The profile's Commitment is a mandate or repayment-schedule row, not the seed's scheduled obligation: standards.ONTOLOGY_ENTITY does not
    map it, and ScheduledObligation is an extension entity that claims no profile or BIAN alignment."""
    t = next(t for t in silver_spec["tables"] if t["table"] == "silver_scheduled_obligation")
    assert t["profile"] is False and not t.get("profile_entity") and "Commitment" not in aligned
    assert "Commitment" not in standards.ONTOLOGY_ENTITY and "ScheduledObligation" not in standards.ONTOLOGY_ENTITY.values()
    assert t["ontology_entity"] == "ScheduledObligation" and "Commitment" in t["notes"]
    datasets = {s["dataset"] for (e, _), f in field_maps.items() if e == "Commitment" for s in f["sources"]}
    assert datasets == {"liabilities", "liabilities.repaymentSchedule", "mandates"}
    assert "repayment-schedule" in entities["Commitment"]["description"]
    assert all("scheduled" not in d.lower() for d in datasets)


def test_profile_attributes_have_exact_names_types_and_nullability(silver_built, aligned, entities, prof):
    total = 0
    for e, t in aligned.items():
        schema = silver_built.tables[t["table"]].schema
        attrs = attributes(prof, entities[e])
        assert schema.names[:7] == COMMON, e
        assert schema.names[7:7 + len(attrs)] == [a["name"] for a in attrs], e
        for a in attrs:
            f = schema.field(a["name"])
            assert f.type.equals(ARROW[a["type"]]) and f.nullable == a["nullable"], (e, a["name"], f.type)
        total += len(attrs)
    assert total >= 200


def test_units_and_types_are_recorded_in_the_attribute_catalog(aligned, entities, prof, catalog):
    for e, t in aligned.items():
        for a in attributes(prof, entities[e]):
            row = catalog[(t["table"], a["name"])]
            assert (row["origin"], row["profile_attribute"], row["cdm_type"], row["unit"], row["nullable"]) == (
                "profile", a["name"], a["cdmType"], a["unit"], a["nullable"]), (e, a["name"])
            if a["type"] == "minor":
                assert a["unit"] == "currency minor units; INR paise, exponent 2" and row["unit"] == a["unit"]


def test_attributes_without_a_seed_counterpart_stay_null_with_a_reason(silver_built, aligned):
    nothing = 0
    for t in aligned.values():
        tbl = silver_built.tables[t["table"]]
        for c in t["columns"]:
            if c["origin"] == "profile" and "none" in c:
                assert str(c["none"]).strip(), (t["table"], c["col"])
                assert tbl.column(c["col"]).null_count == tbl.num_rows, (t["table"], c["col"])
                nothing += 1
    assert nothing >= 60


def test_the_shared_source_version_is_emitted_once_in_the_common_position(silver_built, silver_rows, prof, registry, catalog):
    schema = silver_built.tables["silver_document"].schema
    assert "Document" in prof["sharedEntityColumns"]["sourceVersion"]["entities"]
    assert schema.names.count("sourceVersion") == 1 and schema.names.index("sourceVersion") == 6 and "xSourceVersion" not in schema.names
    row = catalog[("silver_document", "sourceVersion")]
    assert (row["origin"], row["profile_attribute"], row["nullable"]) == ("common", "sourceVersion", True)
    assert (row["bian_domain"], row["bian_control_record"], row["bian_mapping_status"]) == bian_attribute(registry, "Document", "sourceVersion")
    assert {"1", "2"} <= {r["sourceVersion"] for r in silver_rows["silver_document"]}


# ------------------------------------------------------------------------------------------------ common columns
def test_common_columns_match_the_cdm_definitions(silver_built, aligned, cdm_defs):
    for e, t in aligned.items():
        defined = {a["name"]: a for a in cdm_defs[e]["hasAttributes"]}
        schema = silver_built.tables[t["table"]].schema
        for name in COMMON:
            f = schema.field(name)
            assert f.type.equals(CDM_COMMON_ARROW[defined[name]["dataType"]]), (e, name)
            assert f.nullable == defined[name]["isNullable"], (e, name)


def test_common_columns_are_never_null_and_keyed_on_the_record(silver_built, silver_spec, silver_rows):
    for t in silver_spec["tables"]:
        schema = silver_built.tables[t["table"]].schema
        rows = silver_rows[t["table"]]
        for name in COMMON[:-1]:
            assert not schema.field(name).nullable and all(r[name] is not None and r[name] != "" for r in rows), (t["table"], name)
        assert schema.field("sourceVersion").nullable
        for r in rows:
            assert r["id"].split("~")[0] == f"{t['entity']}:{r['sourceRecordId']}"
            if not t.get("record_id"):
                assert r["sourceRecordId"] == "#".join(str(r[k]) for k in t["key"]), t["table"]


# ------------------------------------------------------------------------------------------------ sourceKind, state, label, historical: the app's rules
def test_declared_source_kinds_are_in_the_apps_vocabulary(silver_spec, repo_or_none):
    kinds, _ = app_vocabulary(repo_or_none)
    declared = {t["common"]["source_kind"] for t in silver_spec["tables"]}
    declared |= {t["common"]["historical"]["source_kind"] for t in silver_spec["tables"] if "historical" in t["common"]}
    assert declared and declared <= kinds and "seed" not in kinds


def test_the_committed_vocabulary_copies_equal_the_pinned_app_source(repo_path):
    kinds, inactive = app_vocabulary(repo_path)
    assert kinds == silver.SOURCE_KINDS and len(kinds) == 15
    assert inactive == silver.INACTIVE_STATES and len(inactive) == 7


def test_every_row_has_an_app_source_kind_and_a_state(silver_rows, repo_or_none):
    kinds, _ = app_vocabulary(repo_or_none)
    used = Counter()
    for name, rows in silver_rows.items():
        for r in (rows if name not in CATALOGS else []):
            assert r["sourceKind"] in kinds and r["sourceKind"] != "seed", (name, r["sourceKind"])
            assert isinstance(r["state"], str) and r["state"].strip() and r["state"] not in silver.RETIRED_LITERALS, (name, r["state"])
            used[r["sourceKind"]] += 1
    # `history` appears only for a seed record in an inactive state, and none of the seed's statuses is one
    assert set(used) == {"bank_record", "catalogue", "consent", "customer_stated", "document_metadata", "frontier"}


def test_the_spec_rules_equal_the_apps_projection_rules(aligned, repo_path):
    assert set(RULES) == set(aligned) == set(EVIDENCE)
    for e, t in aligned.items():
        assert normal(t["common"]) == RULES[e], e
        text = cited_text(repo_path, t["common"]["cite"])
        for file, snippet, present in EVIDENCE[e]:
            assert file in text and (snippet in text[file]) == present, (e, file, snippet)


def test_extension_entities_use_the_apps_default_vocabulary(silver_spec, repo_path):
    extension = [t for t in silver_spec["tables"] if not t.get("profile_entity")]
    assert {t["entity"] for t in extension} >= CATALOGUE | set(LIFECYCLE)
    for t in extension:
        e, common = t["entity"], t["common"]
        assert common["source_kind"] == ("catalogue" if e in CATALOGUE else "bank_record"), e
        assert normal(common)["state"] == ({"from": [LIFECYCLE[e]], "default": "recorded"} if e in LIFECYCLE else {"const": "recorded"}), e
        assert "historical" not in common, e
        text = cited_text(repo_path, common["cite"])[PROJECTION]
        assert 'state: str = "recorded", source_kind: str = "bank_record"' in text, e
        assert ('source_kind="catalogue"' in text) == (e in CATALOGUE), e


def test_state_label_kind_and_history_follow_the_rules_from_exposed_columns(silver_spec, silver_rows, repo_or_none):
    """Gold never has to decode `state`: the raw field behind every rule is a real column, and the common values are recomputable from it."""
    _, inactive = app_vocabulary(repo_or_none)
    checked = 0
    for t in silver_spec["tables"]:
        rule = normal(t["common"])
        cols = [c for c in t["columns"] if c["origin"] in silver.ORIGINS and not c.get("t")]

        def value(row, path):
            return next((row[c["col"]] for c in cols if path in silver.source_paths(c) and row[c["col"]] is not None), None)

        for r in silver_rows[t["table"]]:
            state = rule["state"]
            if "const" in state:
                want_state = state["const"]
            elif "from" in state:
                want_state = next((v for p in state["from"] if (v := value(r, p)) is not None), state["default"])
            else:
                want_state = state["then"] if any(value(r, p) for p in state["when"]) else state["default"]
            label = rule.get("label")
            want_label = r["sourceRecordId"]
            if label:
                want_label = next((v for p in label["from"] if (v := value(r, p)) not in (None, "")), label.get("default") or want_label)
            historical = "historical" in rule and want_state in inactive
            want_kind = rule["historical"]["source_kind"] if historical else rule["source_kind"]
            assert (r["state"], r["label"], r["historical"], r["sourceKind"]) == (want_state, want_label, historical, want_kind), (t["table"], r["id"])
            checked += 1
    assert checked == sum(len(silver_rows[t["table"]]) for t in silver_spec["tables"]) == 1440


def test_the_seed_rows_get_the_values_the_apps_rules_give(silver_rows):
    count = lambda table, col: dict(Counter(r[col] for r in silver_rows[table]))  # noqa: E731
    states = {"silver_customer": {"customer": 4}, "silver_account": {"active": 5}, "silver_card": {"active": 3, "temporarily_locked_after_dispute": 1},
              "silver_deposit": {"active": 2, "recorded": 1}, "silver_loan": {"recorded": 2}, "silver_protection_policy": {"active": 3},
              "silver_transaction": {"posted": 633, "reversed": 1}, "silver_goal": {"active": 7, "exploring": 1, "recorded": 6},
              "silver_preference": {"recorded": 9}, "silver_consent": {"recorded": 3}, "silver_service_case": {"pending": 3},
              "silver_document": {"available": 53}, "silver_product": {"active": 24, "proposed": 5, "catalogue": 8},
              "silver_frontier_consent": {"recorded": 4}, "silver_household_access_grant": {"recorded": 4},
              "silver_registered_agent": {"registered": 1}, "silver_frontier_mandate": {"revoked": 1},
              "silver_mandate_action": {"recorded": 4}, "silver_proactive_candidate": {"recorded": 5}}
    kinds = {"silver_customer": "bank_record", "silver_account": "bank_record", "silver_transaction": "bank_record", "silver_goal": "bank_record",
             "silver_preference": "customer_stated", "silver_consent": "consent", "silver_service_case": "bank_record",
             "silver_document": "document_metadata", "silver_product": "catalogue", "silver_frontier_consent": "frontier",
             "silver_household_access_grant": "frontier", "silver_registered_agent": "frontier", "silver_frontier_mandate": "frontier",
             "silver_mandate_action": "frontier", "silver_proactive_candidate": "frontier"}
    for table, want in states.items():
        assert count(table, "state") == want, table
    for table, kind in kinds.items():
        assert count(table, "sourceKind") == {kind: len(silver_rows[table])}, table
    assert not any(r["historical"] for name, rows in silver_rows.items() if name not in CATALOGS for r in rows)
    assert {r["label"] for r in silver_rows["silver_customer"]} >= {"Rhea Bansal", "Nivedita Rao"}
    assert sum(1 for r in silver_rows["silver_document"] if r["label"] == "Source document") == 12
    assert sum(1 for r in silver_rows["silver_product"] if r["label"] == r["sourceRecordId"]) == 8
    assert all(r["label"] == r["sourceRecordId"] for r in silver_rows["silver_transaction"] + silver_rows["silver_account"])
    # the seed's own lifecycle values stay in real columns: the Frontier seed's status is not the app's state
    assert count("silver_frontier_consent", "xStatus") == {"granted": 3, "revoked": 1}
    assert count("silver_household_access_grant", "xStatus") == {"active": 3, "inactive_minor_no_access": 1}
    assert set(count("silver_mandate_action", "xEventType")) == {"created", "action_executed", "action_skipped", "revoked"}


# ------------------------------------------------------------------------------------------------ CDM reference and BIAN lineage
def test_cdm_and_bian_summaries_equal_the_profile_and_the_registry(aligned, entities, registry, model, repo_path):
    generated = standards.generate(repo_path)["entities"]
    for e, t in aligned.items():
        ref = entities[e].get("cdmReference")
        want_cdm = {"entity": ref["entityName"], "status": ref["mappingStatus"], "reference": ref["referenceStatus"]} if ref else None
        want_bian = bian_summary(registry, e)
        assert t["cdm"] == want_cdm == generated[e].get("cdm") == model["standards"]["entities"][e].get("cdm"), e
        assert t["bian"] == want_bian == generated[e]["bian"] == model["standards"]["entities"][e]["bian"], e
        assert all(set(b) == {"domain", "control_record", "custom", "provisional"} for b in t["bian"])


def test_extension_entities_make_no_cdm_or_bian_claim(silver_spec, silver_rows):
    ext = [t for t in silver_spec["tables"] if not t.get("profile_entity")]
    assert len(ext) == 24 and all(not t.get("cdm") and not t.get("bian") for t in ext)
    rows = {r["silver_table"]: r for r in silver_rows[silver.ENTITY_CATALOG]}
    for t in ext:
        r = rows[t["table"]]
        assert r["extension"] is True and r["profile_entity"] is None and r["cdm_reference_entity"] is None and r["bian_domains"] is None


def test_attribute_statuses_are_never_upgraded(aligned, entities, prof, registry, catalog):
    seen = set()
    for e, t in aligned.items():
        for a in attributes(prof, entities[e]):
            row = catalog[(t["table"], a["name"])]
            assert (row["bian_domain"], row["bian_control_record"], row["bian_mapping_status"]) == bian_attribute(registry, e, a["name"]), (e, a["name"])
            seen.update(row["bian_mapping_status"].split("; "))
    assert seen == {"custom", "provisional", "unmapped"}
    assert {m["mappingStatus"] for m in registry["mappings"]} == seen


def test_privacy_text_comes_from_the_profile_field_mappings(aligned, entities, prof, field_maps, catalog):
    for e, t in aligned.items():
        for a in attributes(prof, entities[e]):
            assert catalog[(t["table"], a["name"])]["privacy"].endswith(field_maps[(e, a["name"])]["privacy"]), (e, a["name"])


# ------------------------------------------------------------------------------------------------ the catalogs equal the spec
def test_entity_catalog_equals_the_spec_and_the_tables(silver_built, silver_spec, silver_rows, registry):
    assert silver_built.tables[silver.ENTITY_CATALOG].schema.names == ENTITY_CATALOG
    rows = silver_rows[silver.ENTITY_CATALOG]
    assert [r["silver_table"] for r in rows] == [t["table"] for t in silver_spec["tables"]]
    for r, t in zip(rows, silver_spec["tables"]):
        own = silver_rows[t["table"]]
        cdm = t.get("cdm") or {}
        domains = "; ".join(f"{b['domain']} ({status_text(b)})" for b in bian_summary(registry, t["profile_entity"])) if t.get("profile_entity") else ""
        assert r == {"entity": t["entity"], "silver_table": t["table"], "profile_entity": t.get("profile_entity"),
                     "extension": not t.get("profile_entity"), "cdm_reference_entity": cdm.get("entity"), "cdm_reference_status": cdm.get("status"),
                     "bian_domains": domains or None, "source_files": "; ".join(dict.fromkeys(x["_source_file"] for x in own)),
                     "grain": t["grain"], "row_count": len(own)}, t["table"]


def test_attribute_catalog_has_one_row_per_physical_column(silver_built, silver_rows):
    assert silver_built.tables[silver.ATTRIBUTE_CATALOG].schema.names == ATTRIBUTE_CATALOG
    rows = silver_rows[silver.ATTRIBUTE_CATALOG]
    keys = [(r["silver_table"], r["column_name"]) for r in rows]
    physical = [(n, c) for n, t in silver_built.tables.items() if n not in CATALOGS for c in t.schema.names]
    assert len(set(keys)) == len(keys) and sorted(keys) == sorted(physical)
    for r in rows:
        f = silver_built.tables[r["silver_table"]].schema.field(r["column_name"])
        assert r["nullable"] == f.nullable
        if r["origin"] == "technical":
            assert r["column_name"].startswith("_") and r["bian_mapping_status"] is None and r["official_entity"] is None
        if r["origin"] == "common":
            assert r["column_name"] in COMMON and r["official_entity"] is None
            assert (r["profile_attribute"] is not None) == (r["column_name"] == "sourceVersion" and r["silver_table"] == "silver_document")
            assert r["profile_attribute"] or r["bian_mapping_status"] is None
        if r["origin"] == "extension":
            assert r["bian_mapping_status"] == "unmapped" and r["profile_attribute"] is None and r["source_pointer"] and r["transform"] is not None
        assert r["origin"] in ("profile", "common", "cdm_official", "extension", "technical")


def test_extension_columns_carry_the_x_prefix_on_profile_entities_only(silver_spec, catalog):
    for t in silver_spec["tables"]:
        for c in t["columns"]:
            if c["origin"] != "extension":
                continue
            assert catalog[(t["table"], c["col"])]["origin"] == "extension"
            if t.get("profile_entity"):
                assert c["col"].startswith("x") and c["col"][1].isupper(), (t["table"], c["col"])
            else:
                assert c["col"][0].islower(), (t["table"], c["col"])


def test_catalog_documents_the_common_rules_and_their_app_citations(silver_spec, catalog):
    for t in silver_spec["tables"]:
        c = t["common"]
        cites = [c["cite"]] if isinstance(c["cite"], str) else c["cite"]
        for name in ("label", "state", "sourceKind", "historical"):
            assert catalog[(t["table"], name)]["transform"] == "app rule: " + "; ".join(cites), (t["table"], name)
        state, kind = (catalog[(t["table"], n)]["source_pointer"] for n in ("state", "sourceKind"))
        assert c["source_kind"] in kind and ("history" in kind) == ("historical" in c), t["table"]
        assert state == f"const: {c['state']['const']}" if "const" in c["state"] else f"'{c['state']['default']}'" in state, t["table"]
        assert (catalog[(t["table"], "historical")]["source_pointer"] != "const: false") == ("historical" in c), t["table"]
        assert catalog[(t["table"], "id")]["transform"] is None and catalog[(t["table"], "sourceRecordId")]["transform"] is None


def test_catalog_wording_never_claims_certification_or_uses_the_retired_placeholders(silver_rows):
    for name in CATALOGS:
        for row in silver_rows[name]:
            for v in row.values():
                assert not (isinstance(v, str) and any(w in v.lower() for w in FORBIDDEN_WORDING)), (name, v)
                assert not (isinstance(v, str) and (v in silver.RETIRED_LITERALS or "const: seed" in v or "derived:recorded" in v)), (name, v)


# ------------------------------------------------------------------------------------------------ official-name aliases (design doc 5)
def test_official_alias_columns_follow_the_crosswalk(silver_built, aligned, entities, prof, catalog):
    crosswalk = cdm_crosswalk.load_crosswalk()
    total = 0
    for entity, aliases in crosswalk.items():
        schema = silver_built.tables[aligned[entity]["table"]].schema
        start = len(COMMON) + len(attributes(prof, entities[entity]))
        assert schema.names[start:start + len(aliases)] == [a.official_attribute for a in aliases], entity
        for a in aliases:
            f = schema.field(a.official_attribute)
            assert f.type.equals(ALIAS_ARROW[a.silver_type]) and f.nullable, (entity, a.official_attribute)
            assert pa.types.is_decimal(f.type) == (a.official_type == "currency"), a.official_attribute
            row = catalog[(aligned[entity]["table"], a.official_attribute)]
            assert (row["origin"], row["profile_attribute"], row["official_entity"], row["official_attribute"], row["match_type"], row["cdm_type"],
                    row["unit"], row["transform"]) == ("cdm_official", a.profile_attribute, a.official_entity, a.official_attribute, a.match,
                                                       a.official_type, a.unit, a.transform)
            assert row["bian_mapping_status"] is None
            total += 1
        assert [c for (tb, c), r in catalog.items() if tb == aligned[entity]["table"] and r["origin"] == "cdm_official"] == [
            a.official_attribute for a in aliases]
    assert total == sum(1 for r in catalog.values() if r["origin"] == "cdm_official") >= 16
    assert not [k for k, r in catalog.items() if r["origin"] == "cdm_official" and k[0] not in {aligned[e]["table"] for e in crosswalk}]


def test_alias_values_equal_the_declared_transform_of_the_base_column_for_every_row(aligned, silver_rows):
    checked = 0
    for entity, aliases in cdm_crosswalk.load_crosswalk().items():
        for row in silver_rows[aligned[entity]["table"]]:
            for a in aliases:
                base = row[a.profile_attribute]
                got = row[a.official_attribute]
                assert got == cdm_crosswalk.apply_alias(a, base), (entity, a.official_attribute, row["id"])
                if base is None:
                    assert got is None
                elif a.transform == "minor_to_major_decimal":
                    assert got == Decimal(base) / 100 and got.as_tuple().exponent == -4
                else:
                    assert got == base
                checked += 1
    assert checked >= 16 * 2
