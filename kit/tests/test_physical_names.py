"""Design decision 7: every gold table has a stable logical id (`account`: what relationships, measures, rules, tiers, oracles and the
equivalence fixtures use) and a physical lakehouse name (`dim_account`). The physical name is used only at output boundaries (Parquet file
names, the gold manifest, TMDL partitions, the graph's Delta paths, the data dictionary, the load stage), and no v1 logical name is used as
a lakehouse object name in any generated artifact."""
import base64
import json
import re

import pytest

from contoso_onto import agents, oac, spec as S
from contoso_onto.gold import GoldError, logical_name, physical_name, physical_names
from contoso_onto.pipeline import validate_star, write_gold

# docs/MEDALLION_DESIGN.md section 6 (the v1 table -> the v2 lakehouse table), with the seven names the naming standard changed so that every
# lakehouse name is <role>_<snake(entity)> (docs/NAMING_STANDARD.md): insurance_policy, income_record, access_grant, mandate, mandate_event,
# service_record and proactive_event
DESIGN = {
    "customer": "dim_customer", "household": "dim_household", "household_member": "dim_household_member", "account": "dim_account",
    "card": "dim_card", "deposit": "dim_deposit", "loan": "dim_loan", "loan_installment": "fact_loan_installment",
    "insurance_policy": "dim_protection_policy", "protection_gap": "fact_protection_gap", "investment": "dim_investment",
    "investment_goal": "bridge_investment_goal", "transaction": "fact_transaction", "transaction_evidence": "bridge_transaction_evidence",
    "scheduled_obligation": "fact_scheduled_obligation", "income_record": "fact_income_record", "goal": "dim_goal", "preference": "dim_preference",
    "consent": "dim_consent", "consent_resource": "bridge_consent_resource", "access_grant": "dim_household_access_grant",
    "grant_permission": "bridge_grant_permission", "registered_agent": "dim_registered_agent", "agent_scope": "bridge_agent_scope",
    "mandate": "dim_frontier_mandate", "mandate_event": "fact_mandate_action", "incident": "fact_incident", "service_record": "fact_service_case",
    "proactive_event": "fact_proactive_candidate", "service_request_type": "dim_service_request_type", "document": "dim_document",
    "crm_case": "fact_crm_case", "product": "dim_product", "product_parameter": "dim_product_parameter", "journey": "dim_journey",
    "sop": "dim_sop", "journey_sop": "bridge_journey_sop", "journey_product": "bridge_journey_product", "law": "ref_law",
    "authority_rule": "ref_authority_rule", "frontier_requirement": "ref_frontier_requirement", "bank_agent": "ref_bank_agent",
    "business_unit": "dim_business_unit", "region": "dim_region", "relationship_manager": "dim_relationship_manager",
    "campaign": "dim_campaign", "monthly_metric": "fact_monthly_metric", "risk_alert": "fact_risk_alert", "fraud_signal": "fact_fraud_signal",
    "product_business_unit": "bridge_product_business_unit", "calendar": "dim_date",
}
# where a lakehouse object name can stand in a generated text: a Delta path, a partition's entityName, dbo.<table>, FROM/JOIN inside a code fence
OBJECT_NAME = re.compile(r"/Tables/(\w+)|entityName:\s*(\w+)|\bdbo\.(\w+)")
FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
SQL_TABLE = re.compile(r"\b(?:FROM|JOIN)\s+(?:\[?dbo\]?\.)?\[?(\w+)\]?", re.I)
HEADING = re.compile(r"^### .*?`(\w+)`", re.M)


def object_names(text: str) -> set[str]:
    """The lakehouse object names a generated text uses: Delta paths, partition entityNames, dbo.<table>, FROM/JOIN in fenced SQL, and the
    backticked table of a data dictionary heading."""
    found = {g for m in OBJECT_NAME.finditer(text) for g in m.groups() if g}
    found |= {m.group(1) for block in FENCE.findall(text) for m in SQL_TABLE.finditer(block)}
    return found | set(HEADING.findall(text))


def out_files(out) -> dict:
    """Every generated text of one build: path -> text, with the base64 definition parts of the REST payloads decoded."""
    texts = {}
    for p in sorted(out.rglob("*")):
        if not p.is_file() or p.suffix == ".parquet" or "bronze" in p.parts or "silver" in p.parts:
            continue
        text = p.read_text(encoding="utf-8")
        texts[p.relative_to(out).as_posix()] = text
        if p.parent.name == "payloads":
            for part in json.loads(text)["definition"]["parts"]:
                texts[f"{p.relative_to(out).as_posix()}::{part['path']}"] = base64.b64decode(part["payload"]).decode("utf-8")
    return texts


@pytest.fixture(scope="module")
def logical(model) -> set:
    return set(physical_names(model))


# --------------------------------------------------------------------------------------------------
# the registry
# --------------------------------------------------------------------------------------------------
def test_the_registry_is_the_design_table_one_distinct_lakehouse_name_per_logical_id(model, logical):
    assert physical_names(model) == DESIGN and len(set(DESIGN.values())) == 51
    assert validate_star(model) == []                      # kind prefix, uniqueness and shape of every name
    assert not logical & set(DESIGN.values())              # no table keeps its v1 name as its lakehouse name
    for table in model["tables"]:
        assert physical_name(table["table"]) == physical_name(table["table"], model) == table["physical"]
        assert logical_name(table["physical"]) == table["table"]


def test_logical_ids_still_key_everything_that_is_not_a_lakehouse_boundary(model, logical):
    """Relationships, measures, rules' tables, tiers and oracles keep using logical ids; semantic-model objects and DAX use entity names."""
    ends = {end.split(".")[0] for r in model["relationships"] for end in (r["from"], r["to"])}
    assert ends <= logical and {m["table"] for m in model["measures"]} <= logical and S.SMOKE_TABLES <= logical
    physical = set(DESIGN.values())
    for m in model["measures"]:
        assert not set(re.findall(r"\w+", m["expr"])) & physical, m["name"]            # DAX names entities (Account[...]), not lakehouse tables
    sp = S.make_spec(model, "full")
    sm = oac.build_semantic_model(sp, oac.make_ctx())
    for path, text in sm.items():
        if path.startswith("definition/tables/"):
            entity = path.rsplit("/", 1)[-1][:-5]
            assert f"table {entity}" in text and f"partition {entity} = entity" in text            # semantic-model tables stay entity-named
    assert not set(re.findall(r"\w+", sm["definition/relationships.tmdl"] + sm["definition/model.tmdl"])) & physical


def test_an_unknown_name_is_an_error_not_a_guess(model):
    with pytest.raises(GoldError):
        physical_name("dim_account")                        # a physical name is not a logical id
    with pytest.raises(GoldError):
        logical_name("account")                             # nor the other way round


# --------------------------------------------------------------------------------------------------
# the output boundaries
# --------------------------------------------------------------------------------------------------
def test_parquet_files_the_manifest_and_the_dq_report_carry_the_physical_names(built_out, model, logical):
    stems = {p.stem for p in (built_out / "gold").glob("*.parquet")}
    manifest = json.loads((built_out / "manifest.json").read_text(encoding="utf-8"))
    dq = json.loads((built_out / "dq_report.json").read_text(encoding="utf-8"))
    assert stems == set(manifest["tables"]) == set(dq["tables"]) == set(DESIGN.values()) and not stems & logical
    assert manifest["layer"] == "gold"
    for phys, entry in manifest["tables"].items():
        assert entry["physical"] == phys and entry["table"] == logical_name(phys) and DESIGN[entry["table"]] == phys
        assert entry["kind"] in ("dimension", "fact", "bridge", "reference") and entry["grain"] and bool(entry.get("silver")) != bool(entry.get("reference"))
        assert [f["name"] for f in entry["schema"]] and len(entry["schema"]) == entry["columns"] and dq["tables"][phys]["table"] == entry["table"]


def test_write_gold_removes_a_parquet_file_a_v1_build_left_behind(built, model, tmp_path):
    (tmp_path / "gold").mkdir()
    (tmp_path / "gold" / "account.parquet").write_bytes(b"left by a v1 build")
    (tmp_path / "gold" / "notes.txt").write_text("not a table", encoding="utf-8")
    write_gold(model, built, tmp_path)
    assert {p.stem for p in (tmp_path / "gold").glob("*.parquet")} == set(DESIGN.values()) and (tmp_path / "gold" / "notes.txt").exists()


@pytest.mark.parametrize("tier", ["core", "full"])
def test_tmdl_partitions_bind_the_physical_table_of_the_entity(built_out, model, tier):
    sp = S.make_spec(model, tier)
    for folder, want in (("semantic_model/definition/tables", sp["tables"]), ("ontology/tables", [t for t in sp["tables"] if t["ontology"]])):
        for t in want:
            text = (built_out / tier / folder / f"{t['entity']}.tmdl").read_text(encoding="utf-8")
            assert re.findall(r"entityName: (\w+)", text) == [DESIGN[t["table"]]], (folder, t["entity"])
            assert f"partition {t['entity']} = entity" in text
    ont = (built_out / tier / "ontology" / "entities" / "Account.tmdl").read_text(encoding="utf-8")
    assert "backingTable: Account" in ont                  # the ontology entity binds the ontology's table (named by entity), which binds the lakehouse


@pytest.mark.parametrize("tier", ["core", "full"])
def test_the_graph_definition_binds_the_physical_delta_tables(model, tier):
    sp = S.make_spec(model, tier)
    parts = oac.build_graph(sp, oac.make_ctx())
    paths = {s["name"]: s["properties"]["path"] for s in json.loads(parts["dataSources.json"])["dataSources"]}
    assert paths and all(path.endswith("/Tables/" + DESIGN[t["table"]]) for t in sp["tables"] if t["ontology"] for path in [paths[t["entity"]]])


@pytest.mark.parametrize("tier", ["core", "full"])
def test_the_data_dictionary_names_lakehouse_tables_physically(built_out, model, logical, tier):
    pack = built_out / tier / "agents" / "dictionary"
    dd = json.loads((pack / "data_dictionary.json").read_text(encoding="utf-8"))
    sp = S.make_spec(model, tier)
    assert {e["logicalId"]: e["table"] for e in dd["entities"]} == {t["table"]: DESIGN[t["table"]] for t in sp["tables"]}
    assert all(e["table"] not in logical for e in dd["entities"])
    ends = {end.split(".")[0] for r in dd["relationships"] for end in (r["from"], r["to"])}
    assert ends <= set(DESIGN.values()) and {m["table"] for m in dd["measures"]} <= set(DESIGN.values())
    assert {m["entity"] for m in dd["measures"]} <= {t["entity"] for t in sp["tables"]}            # measures live on entity-named semantic tables
    md = (pack / "data_dictionary.md").read_text(encoding="utf-8")
    assert set(HEADING.findall(md)) == {DESIGN[t["table"]] for t in sp["tables"]} and "`dim_account`" in md


def test_no_logical_id_is_a_lakehouse_object_name_in_any_generated_artifact(built_out, logical):
    """Everything one build writes (Parquet names, manifest, DQ report, TMDL, REST payloads, agent pack, dictionary, graph text): not one
    lakehouse object name is a v1 logical id. (The logical id is still a legitimate key `table` / `logicalId` in manifest and dictionary.)"""
    texts = out_files(built_out)
    assert len(texts) > 200
    leaks = {path: names & logical for path, text in texts.items() if (names := object_names(text)) & logical}
    assert leaks == {}
    used = set().union(*(object_names(t) for t in texts.values()))
    assert used >= set(DESIGN.values())                      # the scan sees the physical names in the same positions
    names = {p.stem for p in (built_out / "gold").glob("*.parquet")} | set(json.loads((built_out / "manifest.json").read_text(encoding="utf-8"))["tables"])
    assert not names & logical


def test_the_scan_does_catch_a_boundary_that_forgets_the_registry(model, logical):
    """Negative control: with every physical name reset to the logical id the generators leak, and the scan reports it for each boundary."""
    sp = S.make_spec(model, "core")
    for t in sp["tables"]:
        t["physical"] = t["table"]
    ctx = oac.make_ctx()
    semantic = "\n".join(oac.build_semantic_model(sp, ctx).values())
    ontology = "\n".join(oac.build_ontology(sp, ctx).values())
    graph = "\n".join(oac.build_graph(sp, ctx).values())
    dictionary = agents.data_dictionary_md(agents.data_dictionary(sp))
    for text in (semantic, ontology, graph, dictionary):
        assert len(object_names(text) & logical) >= 30
    fence = "```sql\nSELECT * FROM dbo.account a JOIN [customer] c ON c.customer_id = a.customer_id\n```"
    assert object_names(fence) >= {"account", "customer"}


def test_validate_graph_rejects_a_delta_path_that_is_not_the_entitys_physical_table(model):
    sp = S.make_spec(model, "smoke")
    parts = oac.build_graph(sp, oac.make_ctx())
    assert oac.validate_graph(parts, sp).ok
    doc = json.loads(parts["dataSources.json"])
    doc["dataSources"][0]["properties"]["path"] = doc["dataSources"][0]["properties"]["path"].rsplit("/", 1)[0] + "/customer"
    rep = oac.validate_graph({**parts, "dataSources.json": json.dumps(doc)}, sp)
    assert not rep.ok and any(e.startswith("G032") for e in rep.errors)


def test_the_spec_checks_reject_two_tables_on_one_lakehouse_name_and_a_bad_name(model):
    sp = json.loads(json.dumps(S.make_spec(model, "core")))
    sp["tables"][1]["physical"] = sp["tables"][0]["physical"]
    sp["tables"][2]["physical"] = "Not A Name"
    codes = {e.split(":")[0] for e in S.check_spec(sp).errors}
    assert {"S071", "S072"} <= codes
