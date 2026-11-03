"""What an entity description shows (A5 of the naming and ontology phase): the business meaning first, then the standards sentence, then one
architecture clause built from the registry (star role, grain, lakehouse lineage, official-name aliases); every description inside its budget; the data
dictionary carrying the same facts as data; and the generated agent texts naming only real entities and, in a query, only lakehouse tables."""
import json
import re

import pytest

from contoso_onto import agents, oac, registry, spec as S, standards

ONTOLOGY_TIERS = ("core", "full")
FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
SQL_TABLE = re.compile(r"\b(?:FROM|JOIN)\s+(?:\[?dbo\]?\.)?\[?([A-Za-z_]\w*)\]?", re.I)
INLINE_SQL_TABLE = re.compile(r"\b(?:FROM|JOIN)\s+(?:\[?dbo\]?\.)?\[?([A-Za-z_]\w*)\]?")             # outside a fence only the upper-case keyword: prose says "from the data"
ENTITY_PROPERTY = re.compile(r"\b([A-Z][A-Za-z]+)\.([A-Z][A-Za-z]+)\b")


@pytest.fixture(scope="module", params=ONTOLOGY_TIERS)
def tier(request):
    return request.param


@pytest.fixture(scope="module")
def spec_of(model):
    return lambda tier: S.make_spec(model, tier)


@pytest.fixture(scope="module")
def originals(model) -> dict:
    return {t["table"]: (t.get("description") or "").rstrip() for t in model["tables"]}


def sql_tables(text: str) -> set:
    """The table names a generated text queries: FROM / JOIN targets in a code fence, and upper-case FROM / JOIN targets anywhere else."""
    fenced = {m.group(1) for block in FENCE.findall(text) for m in SQL_TABLE.finditer(block)}
    return fenced | {m.group(1) for m in INLINE_SQL_TABLE.finditer(FENCE.sub("", text))}


# --------------------------------------------------------------------------------------------------
# the budget
# --------------------------------------------------------------------------------------------------
def test_no_description_exceeds_its_budget(spec_of, tier):
    sp = spec_of(tier)
    assert len(sp["ontology"]["description"]) <= S.ONTOLOGY_DESCRIPTION_MAX == 256               # Fabric keeps 256 of an item description and cuts the rest
    for t in sp["tables"]:
        assert len(t["description"]) <= S.ENTITY_DESCRIPTION_MAX, t["entity"]
        for c in t["columns"]:
            assert len(c["desc"]) <= S.COLUMN_DESCRIPTION_MAX == 200, (t["entity"], c["col"])    # the 200 characters Copilot reads
    assert all(len(m.get("desc", "")) <= S.COLUMN_DESCRIPTION_MAX for m in sp["measures"])
    assert S.check_spec(sp).ok


def test_the_budget_is_checked_at_build_time(spec_of):
    sp = spec_of("core")
    sp["tables"][0]["description"] = "x" * (S.ENTITY_DESCRIPTION_MAX + 1)
    sp["tables"][1]["columns"][0]["desc"] = "y" * (S.COLUMN_DESCRIPTION_MAX + 1)
    sp["measures"][0]["desc"] = "z" * (S.COLUMN_DESCRIPTION_MAX + 1)
    sp["ontology"]["description"] = "w" * (S.ONTOLOGY_DESCRIPTION_MAX + 1)
    errors = S.check_spec(sp).errors
    assert {e.split(":")[0] for e in errors} == {"S073", "S074"} and len(errors) == 4


def test_the_longest_description_is_inside_the_budget_with_room_to_spare(spec_of):
    longest = max(len(t["description"]) for t in spec_of("full")["tables"])
    assert 497 < longest <= S.ENTITY_DESCRIPTION_MAX                                # the longest one proven live was 497; the clause adds to it, within the budget


# --------------------------------------------------------------------------------------------------
# order and content
# --------------------------------------------------------------------------------------------------
def test_a_description_is_the_business_meaning_then_the_standards_sentence_then_the_architecture_clause(model, spec_of, originals, tier):
    notes = standards.notes(model)
    rows = {r["table"]: r for r in registry.entity_rows(model)}
    for t in spec_of(tier)["tables"]:
        want = " ".join(p for p in (originals[t["table"]], notes[t["entity"]], registry.architecture_clause(rows[t["table"]])) if p)
        assert t["description"] == want, t["entity"]
        assert t["description"].startswith(originals[t["table"]]) and t["description"][:200] == want[:200]     # what Copilot reads is the business meaning
        sentence = "Demo mapping, not certification." if t["registry"]["profile_entities"] else standards.EXTENSION_NOTE
        assert t["description"].index(originals[t["table"]]) < t["description"].index(sentence) < t["description"].index("Star role: "), t["entity"]


def test_the_clause_is_built_from_the_spec_not_typed(model, spec_of):
    by_entity = {t["entity"]: t["description"] for t in spec_of("full")["tables"]}
    assert by_entity["Account"].endswith(
        "Star role: dimension; grain: one account, with its balances as at the release business date. Lakehouse: dim_account <- silver_account <- accounts.json. "
        "Official CDM name aliases: FHAccount.integrationKey, FHAccount.balance, FHAccount.availableBalance, FHAccount.blockedAmount (semantic reference only).")
    assert by_entity["Law"].endswith("Star role: reference; grain: one design law. Lakehouse: ref_law <- Curated reference data.")
    assert by_entity["Calendar"].endswith("Lakehouse: dim_date <- Curated reference data.")
    assert "Lakehouse: dim_customer <- silver_customer + silver_crm_case <- personas.json, bank/crm-cases.json." in by_entity["Customer"]
    assert "Official CDM name aliases" not in by_entity["Customer"] and "Official CDM name aliases" not in by_entity["Transaction"]
    silver_tables = {s["table"]: s for s in model["silver_tables"]}
    for r in registry.entity_rows(model):
        assert registry.architecture_clause(r) in by_entity[r["entity"]]
        assert r["physical"] in by_entity[r["entity"]] and f"Star role: {r['kind']};" in by_entity[r["entity"]]
        assert r["reference"] == ("Curated reference data" in by_entity[r["entity"]])
        for s in r["silver"]:
            assert s in silver_tables and s in by_entity[r["entity"]]                # every silver table named exists in the silver spec
        for f in r["bronze_files"]:
            assert any(f in json.dumps(src) for s in r["silver"] for src in silver_tables[s]["sources"]), (r["entity"], f)      # a seed file the silver spec reads


def test_the_aliases_in_a_description_are_the_crosswalk_s_aliases(model, spec_of):
    by_entity = {t["entity"]: t["description"] for t in spec_of("core")["tables"]}
    aliased = {e["profile_entity"]: [f"{e['official_entity']}.{a['official']}" for a in e["attributes"]] for e in model["crosswalk"] if e["attributes"]}
    assert set(aliased) == {"Account", "Card", "Deposit", "Loan"} and sum(len(v) for v in aliased.values()) == 16
    for profile, names in aliased.items():
        for name in names:
            assert name in by_entity[standards.ONTOLOGY_ENTITY[profile]], name
    clause = ", ".join(aliased["Loan"]) + " (semantic reference only)."
    assert clause in by_entity["Loan"]


def test_the_renamed_entities_keep_their_old_business_words_findable(spec_of):
    sp = spec_of("full")
    renamed = [t for t in sp["tables"] if t["registry"]["renamed_from"].get("entity")]
    assert {t["entity"] for t in renamed} == {"ProtectionPolicy", "ServiceCase", "HouseholdAccessGrant", "FrontierMandate", "MandateAction", "ProactiveCandidate"}
    for t in renamed:
        words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", t["registry"]["renamed_from"]["entity"]).lower()
        assert words in [s.lower() for s in t["synonyms"]], (t["entity"], words)
        assert words.split()[-1] in t["description"].lower(), (t["entity"], words)
    wanted = {"FrontierMandate": {"mandate", "delegated mandate", "surplus-to-goal mandate", "standing authority"},
              "ProtectionPolicy": {"insurance policy", "cover", "policy"}, "ServiceCase": {"service record", "service request", "servicing contact"},
              "HouseholdAccessGrant": {"access grant", "household access", "permission grant"},
              "MandateAction": {"mandate event", "mandate action", "executed or skipped action"}, "ProactiveCandidate": {"proactive event", "proactive nudge"}}
    synonyms = {t["entity"]: {s.lower() for s in t["synonyms"]} for t in renamed}
    for entity, words in wanted.items():
        assert words <= synonyms[entity], (entity, words - synonyms[entity])


def test_descriptions_reach_the_ontology_and_the_semantic_model_and_validate(spec_of, tier):
    sp = spec_of(tier)
    ctx = oac.make_ctx()
    ont, sm = oac.build_ontology(sp, ctx), oac.build_semantic_model(sp, ctx)
    for t in sp["tables"]:
        doc = "\n".join(ln for ln in sm[f"definition/tables/{t['entity']}.tmdl"].splitlines() if ln.startswith("///"))
        assert doc == "/// " + t["description"], t["entity"]
        if t["ontology"]:
            assert "Star role: " in ont[f"entities/{t['entity']}.tmdl"] and t["description"] in ont[f"entities/{t['entity']}.tmdl"]
    for parts in (ont, sm):
        rep = oac.validate_parts(parts)
        assert rep.ok, rep.errors


# --------------------------------------------------------------------------------------------------
# the data dictionary
# --------------------------------------------------------------------------------------------------
def test_the_data_dictionary_carries_the_architecture_per_entity(model, spec_of, tier):
    sp = spec_of(tier)
    dd = agents.data_dictionary(sp)
    rows = {r["table"]: r for r in registry.entity_rows(model)}
    assert len(dd["entities"]) == len(sp["tables"])
    for e in dd["entities"]:
        r = rows[e["logicalId"]]
        assert e["kind"] == r["kind"] and e["grain"] == r["grain"] and e["silver"] == r["silver"] and e["table"] == r["physical"]
        assert e["profileEntity"] == r["profile_entities"] and e["extension"] == (not r["profile_entities"]) and e["family"] == r["family"]
        assert [c["entity"] for c in e["cdmReference"]] == [c["entity"] for c in r["cdm"]] and all(c["status"] == "semantic reference only" for c in e["cdmReference"])
        assert [(b["domain"], b["controlRecord"], b["status"]) for b in e["bianDomains"]] == [(b["domain"], b["control_record"], b["status"]) for b in r["bian"]]
        assert (e["renamedFrom"] or {}) == r["renamed_from"]
    by_entity = {e["entity"]: e for e in dd["entities"]}
    assert by_entity["Account"]["silver"] == ["silver_account"] and by_entity["Account"]["profileEntity"] == ["Account"]
    assert by_entity["Account"]["cdmReference"][0]["nameAliases"][0] == "FHAccount.integrationKey"
    assert [b["domain"] for b in by_entity["Account"]["bianDomains"]] == ["Customer Product and Service Directory", "Savings Account"]
    assert by_entity["Household"]["profileEntity"] == [] and by_entity["Household"]["extension"] is True and by_entity["Household"]["bianDomains"] == []
    assert by_entity["Consent"]["profileEntity"] == ["Consent", "FrontierConsent"] and by_entity["Law"]["silver"] == []


def test_the_data_dictionary_markdown_shows_them(spec_of, tier):
    md = agents.data_dictionary_md(agents.data_dictionary(spec_of(tier)))
    assert "- **Star role:** dimension. **Grain:** One account" in md
    assert "**Lineage:** `dim_account` <- `silver_account` <- accounts.json. **Profile entity:** Account. **Official CDM reference:** FHAccount (semantic reference only)" in md
    assert "**BIAN domains:** Customer Product and Service Directory (custom), Savings Account (custom)." in md
    assert "**Profile entity:** none (Contoso extension entity)" in md and "**BIAN domains:** none verified" in md and "curated reference data" in md
    assert md.count("**Star role:**") == len(spec_of(tier)["tables"])


# --------------------------------------------------------------------------------------------------
# the agent texts
# --------------------------------------------------------------------------------------------------
def test_the_data_agent_instructions_name_only_real_entities_and_properties_within_the_limit(spec_of, tier):
    sp = spec_of(tier)
    text = agents.data_agent_instructions(sp)
    assert len(text) <= 10_000                                                       # the documented limit for AI instructions
    props = {f"{t['entity']}.{c['prop']}" for t in sp["tables"] if t["ontology"] for c in t["columns"] if c["ontology"]}
    entities = {t["entity"] for t in sp["tables"] if t["ontology"]}
    for entity, prop in ENTITY_PROPERTY.findall(text):
        if entity in entities or entity in {t["registry"]["renamed_from"].get("entity") for t in sp["tables"]}:
            assert f"{entity}.{prop}" in props, f"the instructions name {entity}.{prop}, which is not an ontology property"
    for words, entity in agents._former_words(sp):
        assert f"{words} = {entity}" in text and entity in entities
    assert "HouseholdAccessGrant.Permissions" in text and "AccessGrant.Permissions" not in text.replace("HouseholdAccessGrant.Permissions", "")


def test_no_generated_text_names_an_entity_by_the_name_it_was_renamed_from(model, spec_of):
    """Rule statements, DAX and the agent instructions use the current entity names; the old ones live on only as the registry's `renamed_from`, as
    synonyms and as business words (a relationship or a measure keeps its wording on purpose)."""
    sp = spec_of("full")
    old = [t["registry"]["renamed_from"]["entity"] for t in sp["tables"] if t["registry"]["renamed_from"].get("entity")]
    texts = {r["name"]: r["statement"] for r in sp["rules"]} | {m["name"]: m["expr"] for m in sp["measures"]} | {"instructions": agents.data_agent_instructions(sp)}
    for name, text in texts.items():
        for o in old:
            assert not re.search(rf"\b{o}\.[A-Z]|\b{o}\[|\({o}\)", text), (name, o)
            if re.search(r"[a-z][A-Z]", o):
                assert not re.search(rf"\b{o}\b", text), (name, o)                     # a CamelCase name such as AccessGrant is never an English word
    for r in sp["rules"]:
        assert not {p.split(".")[0] for p in r["properties"]} & set(old)


def test_a_generated_query_names_only_lakehouse_tables(model, spec_of, tier):
    sp = spec_of(tier)
    physical = {oac.physical_of(t) for t in sp["tables"]}
    logical = {t["table"] for t in sp["tables"]} - physical
    pack = agents.build_pack(sp, [], oac.build_brief(sp))
    found = {name: sql_tables(text) for name, text in pack.items()}
    used = set().union(*found.values())
    assert used and used <= physical and not used & logical, {n: t - physical for n, t in found.items() if t - physical}      # the dictionary's example is non-vacuous
    md = pack["dictionary/data_dictionary.md"]
    assert "```sql" in md and sql_tables(md) <= physical
    # negative control: the scan sees a v1 logical id, an entity name and a table that does not exist, and ignores the prose "from the data"
    assert sql_tables("SELECT * FROM account a JOIN [Account] b ON 1 = 1 JOIN dbo.dim_account c") == {"account", "Account", "dim_account"}
    assert sql_tables("```sql\nselect 1 from fact_transaction t join mandate m on 1 = 1\n```") == {"fact_transaction", "mandate"}
    assert sql_tables("Quote a figure only if you read it from the data.") == set()
    assert {"account", "mandate"} <= logical and not {"account", "mandate"} & physical


def test_the_pack_is_json_and_markdown_that_round_trips(spec_of):
    sp = spec_of("core")
    pack = agents.build_pack(sp, [], oac.build_brief(sp))
    dd = json.loads(pack["dictionary/data_dictionary.json"])
    assert dd["entities"][0]["kind"] and "bianDomains" in dd["entities"][0] and pack["dictionary/data_dictionary.md"].endswith("```\n")
