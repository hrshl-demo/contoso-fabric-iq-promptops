"""The naming standard (docs/NAMING_STANDARD.md), enforced. Silver names are `silver_` + snake_case of the entity; a gold lakehouse name is the role
prefix of its `kind` + snake_case of the ontology entity (the one exception is `dim_date`); an ontology entity that realises a profile entity carries
the profile entity's name (the one merge is Consent); no table or entity is named after an official Microsoft CDM entity or after a BIAN service domain
or control record; the generated tables in the document are current; every name meets the ontology limits; and a rename is recorded in the registry
(`renamed_from`), consistent with the frozen v1 specification. Every name is read from the specs: nothing here lists the names it checks."""
import importlib.util
import re
import sys
from pathlib import Path

import pytest

from contoso_onto import cdm_crosswalk, oac, registry, spec as S, standards
from contoso_onto.gold import KIND_PREFIX

KIT = Path(__file__).resolve().parent.parent
ROLE = {"dimension": "dim", "fact": "fact", "bridge": "bridge", "reference": "ref"}
EXCEPTIONS = {"calendar": "dim_date"}                      # logical id -> the lakehouse name outside role + entity
MERGES = {"FrontierConsent": "Consent"}                   # the one profile entity whose ontology entity has another name: Consent merges two
# the official Microsoft CDM entities vendored in the source repo (data/banking-model/v1/vendor/cdm/references); the test with the repo checks this list
OFFICIAL_CDM_ENTITIES = {"FHAccount", "FICard", "FHSaving", "FHLoan", "Contact", "CustomDocument", "Relationship", "FinancialHolding",
                         "CustomerFinancialHolding", "LoanApplication", "ApplicationDocument"}
LAYER_PREFIXES = ("silver_", "dim_", "fact_", "bridge_", "ref_")


def snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def flat(name: str) -> str:
    """A name compared without case, separators or a layer prefix: `silver_household_access_grant` and `HouseholdAccessGrant` are one name."""
    for prefix in LAYER_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
    return re.sub(r"[^a-z0-9]", "", name.lower())


def bian_names(model: dict) -> set:
    """Every BIAN service domain and control record name of the registry, as model/standards.yaml carries them."""
    return {n for e in model["standards"]["entities"].values() for b in e["bian"] for n in (b["domain"], b["control_record"])}


def clashes(names: set, official: set, bian: set) -> dict:
    """name -> what it is named after, for every name equal (ignoring case and separators) to an official CDM entity, a BIAN domain or a control record."""
    denied = {flat(n): f"the official CDM entity {n}" for n in official} | {flat(n): f"the BIAN name {n}" for n in bian}
    return {n: denied[flat(n)] for n in names if flat(n) in denied}


@pytest.fixture(scope="module")
def naming_tool():
    path = KIT / "tools" / "naming_registry.py"
    spec = importlib.util.spec_from_file_location("naming_registry", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["naming_registry"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rows(model) -> list:
    return registry.entity_rows(model)


# --------------------------------------------------------------------------------------------------
# the rule per layer
# --------------------------------------------------------------------------------------------------
def test_silver_tables_are_silver_plus_snake_case_of_the_entity(silver_spec, model):
    tables = silver_spec["tables"]
    assert len(tables) == 43
    for t in tables:
        assert t["table"] == "silver_" + snake(t["entity"]), t["table"]
        assert registry.snake(t["entity"]) == snake(t["entity"])                  # the registry's rule is the rule written here
    gold_entity = {g["table"]: g["entity"] for g in model["tables"]}
    for t in tables:                                                            # the silver spec names the ontology entity its gold table carries
        assert t["ontology_entity"] in {gold_entity[served] for served in t["serves"]}, t["table"]


def test_gold_lakehouse_names_are_the_role_prefix_plus_snake_case_of_the_entity(model):
    assert {k: v for k, v in KIND_PREFIX.items()} == {k: v + "_" for k, v in ROLE.items()}
    off_rule = {}
    for t in model["tables"]:
        want = ROLE[t["kind"]] + "_" + snake(t["entity"])
        if t["physical"] != want:
            off_rule[t["table"]] = t["physical"]
        assert registry.expected_physical(t["entity"], t["kind"]) == (EXCEPTIONS.get(t["table"]) or want), t["table"]
    assert off_rule == EXCEPTIONS


def test_every_kind_has_a_role_and_the_counts_are_the_star_of_the_design(model, rows):
    kinds = {k: sum(1 for t in model["tables"] if t["kind"] == k) for k in ROLE}
    assert kinds == {"dimension": 26, "fact": 13, "bridge": 8, "reference": 4}
    assert len(rows) == 51 and len({r["physical"] for r in rows}) == 51 and len({r["entity"] for r in rows}) == 51
    assert sum(1 for r in rows if r["profile_entities"]) == 18 and sum(1 for r in rows if r["extension"]) == 33


def test_logical_ids_never_change_and_a_rename_is_recorded_against_the_frozen_v1_spec(model, v1_model):
    v1 = {t["table"]: t for t in v1_model["tables"]}
    assert {t["table"] for t in model["tables"]} == set(v1)                       # the v1 table names stay the logical ids
    renamed = 0
    for t in model["tables"]:
        old = t.get("renamed_from") or {}
        assert set(old) <= {"entity", "physical"}, t["table"]
        assert ("entity" in old) == (t["entity"] != v1[t["table"]]["entity"]), t["table"]
        if "entity" in old:
            assert old["entity"] == v1[t["table"]]["entity"] and old["entity"] != t["entity"]
            renamed += 1
        if "physical" in old:
            assert old["physical"] != t["physical"] and old["physical"].startswith(KIND_PREFIX[t["kind"]])     # the name the first star build gave it
    assert renamed >= 1


# --------------------------------------------------------------------------------------------------
# ontology entity names
# --------------------------------------------------------------------------------------------------
def test_an_ontology_entity_that_realises_a_profile_entity_carries_its_name(model, rows, silver_spec):
    for profile, onto in standards.ONTOLOGY_ENTITY.items():
        assert onto == profile or MERGES.get(profile) == onto, (profile, onto)
    merged = {r["entity"]: r["profile_entities"] for r in rows if len(r["profile_entities"]) > 1}
    assert merged == {"Consent": ["Consent", "FrontierConsent"]}
    for r in rows:
        if r["profile_entities"] and r["entity"] not in merged:
            assert r["profile_entities"] == [r["entity"]], r["entity"]
    aligned = {t["profile_entity"]: t["ontology_entity"] for t in silver_spec["tables"] if t.get("profile_entity")}
    assert aligned == standards.ONTOLOGY_ENTITY                                  # silver and the standards map say the same thing
    extension = {t["ontology_entity"] for t in silver_spec["tables"] if t["profile"] is False}
    assert not extension & set(standards.ONTOLOGY_ENTITY.values())               # an extension entity never claims an entity a profile entity realises


def test_no_name_is_an_official_cdm_entity_a_bian_service_domain_or_a_control_record(model, silver_spec, rows):
    bian = bian_names(model)
    assert len(bian) == 34 and "Savings Account" in bian and "SavingsAccountFacility" in bian
    names = {n for r in rows for n in (r["entity"], r["physical"], r["table"])}
    names |= {n for t in silver_spec["tables"] for n in (t["table"], t["entity"], t["ontology_entity"])}
    assert clashes(names, OFFICIAL_CDM_ENTITIES, bian) == {}
    # negative control: the check sees an official entity, a BIAN domain and a control record under every spelling a table could take
    bad = {"FHAccount", "dim_fh_account", "silver_FICard", "bridge_contact", "SavingsAccount", "dim_savings_account", "SavingsAccountFacility",
           "fact_credit_card", "TermDepositFacility", "ref_payment_order", "CustomDocument", "dim_custom_document"}
    assert set(clashes(bad, OFFICIAL_CDM_ENTITIES, bian)) == bad
    assert clashes({"Account", "dim_loan", "Card", "silver_goal", "Product", "ProtectionPolicy"}, OFFICIAL_CDM_ENTITIES, bian) == {}


def test_the_official_entity_deny_list_is_what_the_vendored_files_define(repo_path):
    assert set(cdm_crosswalk.official_index(repo_path)) == OFFICIAL_CDM_ENTITIES


def test_the_bian_names_are_what_the_registry_defines(repo_path, model):
    import json
    reg = json.loads((Path(repo_path) / standards.REGISTRY).read_text(encoding="utf-8-sig"))
    assert {n for d in reg["domains"] for n in (d["name"], d["controlRecord"])} == bian_names(model) and len(reg["domains"]) == 17


# --------------------------------------------------------------------------------------------------
# limits and the generated document
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["core", "full"])
def test_entity_and_property_names_meet_the_ontology_limits(model, tier):
    sp = S.make_spec(model, tier)
    rep = S.check_spec(sp)
    assert rep.ok and rep.warnings == [], (rep.errors, rep.warnings)               # S004 / S005 / S016 / S017: identifier and 1-26 characters
    for t in sp["tables"]:
        assert re.fullmatch(r"[A-Z][A-Za-z0-9]*", t["entity"]) and oac.NAME_RE.match(t["entity"]), t["entity"]
        if t["ontology"]:
            for c in t["columns"]:
                if c["ontology"]:
                    assert re.fullmatch(r"[A-Z][A-Za-z0-9]*", c["prop"]) and oac.NAME_RE.match(c["prop"]), (t["entity"], c["prop"])


def test_the_naming_document_carries_the_generated_tables(model, naming_tool):
    text = (KIT / "docs" / "NAMING_STANDARD.md").read_text(encoding="utf-8")
    blocks = naming_tool.render(model)
    assert set(blocks) == {"entities", "bian"}
    for name, body in blocks.items():
        assert naming_tool.block_of(text, name) == body, f"docs/NAMING_STANDARD.md is out of date: run python tools/naming_registry.py --write ({name})"
    assert len(blocks["entities"].splitlines()) == 2 + 51 and len(blocks["bian"].splitlines()) == 2 + 17
    assert naming_tool.replace_blocks(text, blocks) == text


def test_the_generator_notices_a_stale_document_and_rewrites_it(model, naming_tool):
    text = (KIT / "docs" / "NAMING_STANDARD.md").read_text(encoding="utf-8")
    fresh = naming_tool.render(model)
    stale = naming_tool.replace_blocks(text, {**fresh, "entities": fresh["entities"].replace("`dim_frontier_mandate`", "`dim_mandate`")})
    assert stale != text and naming_tool.block_of(stale, "entities") != fresh["entities"]
    assert naming_tool.replace_blocks(stale, fresh) == text


def test_the_document_names_every_rule_it_promises(model):
    text = (KIT / "docs" / "NAMING_STANDARD.md").read_text(encoding="utf-8")
    for needle in ("silver_<entity>", "dim_<entity>", "fact_<entity>", "bridge_<entity>", "ref_<entity>", "dim_date", "Consent",
                   "How to change a name", "renamed_from", "tools/naming_registry.py"):
        assert needle in text, needle
    assert "Contoso extension entity: no CDM profile entity" in text
