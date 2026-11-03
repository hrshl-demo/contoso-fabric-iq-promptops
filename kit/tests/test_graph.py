"""GraphModel definition generated from the spec: the child item Fabric creates next to the ontology.
tests/fixtures/graph_core is what the portal's Configure graph wizard wrote for the core tier (captured live; the workspace and lakehouse
ids are replaced by the make_ctx() placeholders). The capture was made with the names of the first build: lakehouse tables still had their v1
names (`account`) and five core entities had other names than today (`Mandate`, `AccessGrant`, ...; the naming standard, docs/NAMING_STANDARD.md,
gave them their CDM profile names). The reference below is therefore that capture with two mechanical mappings: every Delta table path goes
through the physical-name registry (`dim_account`), and every entity name and entity alias (the lineage tag, a uuid5 of the entity name) the
registry says was renamed (`renamed_from`) is swapped for the current one. Both variants are derived in the test; the captured files stay as
the portal wrote them."""
import base64
import collections
import json
import re
import uuid
from pathlib import Path

import pytest

from contoso_onto import oac, spec as S
from contoso_onto.gold import GoldError, logical_name, physical_name, physical_names

FIXTURE = Path(__file__).parent / "fixtures" / "graph_core"
IDS = dict(workspace_id="11111111-1111-4111-8111-111111111111", lakehouse_id="22222222-2222-4222-8222-222222222222")
GRAPH_OF_TMDL = {"string": "STRING", "int64": "INT", "double": "FLOAT", "boolean": "BOOLEAN", "dateTime": "ZONED DATETIME"}
# The portal lists node types, edge types, data sources and tables in the order the ontology service stored them. The capture environment was
# deployed smoke tier first and then core, so the reference starts with the 12 smoke entities and relationships: that order is history, not
# content. The five collections are therefore compared by key; everything inside an item (property order included) is compared exactly, and
# the order we emit (the spec's) is pinned by test_generated_order_follows_the_spec.
KEYS = {"nodeTypes": "alias", "edgeTypes": "alias", "dataSources": "name", "nodeTables": "id", "edgeTables": "id"}


@pytest.fixture(scope="module", params=["smoke", "core", "full"])
def tier(request):
    return request.param


@pytest.fixture(scope="module")
def spec_for(model):
    return lambda tier: S.make_spec(model, tier)


@pytest.fixture(scope="module")
def made(spec_for, tier):
    sp = spec_for(tier)
    return sp, oac.build_ontology(sp, oac.make_ctx()), oac.build_graph(sp, oac.make_ctx())


@pytest.fixture(scope="module")
def core(spec_for):
    return spec_for("core")


@pytest.fixture(scope="module")
def core_parts(core):
    return oac.build_graph(core, oac.make_ctx())


def docs(parts: dict) -> tuple:
    return tuple(json.loads(parts[n]) for n in ("graphType.json", "dataSources.json", "graphDefinition.json"))


def by_key(doc: dict) -> dict:
    return {k: sorted(v, key=lambda i: i[KEYS[k]]) if k in KEYS else v for k, v in doc.items()}


def physical_variant(name: str, doc: dict) -> dict:
    """The portal capture with each Delta table path mapped from the v1 lakehouse name to the physical name (a path that already names a
    physical table, as a later capture would, is left alone)."""
    if name == "dataSources.json":
        for source in doc["dataSources"]:
            head, _, table = source["properties"]["path"].rpartition("/")
            try:
                logical_name(table)
            except GoldError:
                source["properties"]["path"] = f"{head}/{physical_name(table)}"
    return doc


def entity_renames(sp: dict) -> dict:
    """old entity name -> current entity name, read from the registry's `renamed_from` of the spec's tables."""
    return {t["registry"]["renamed_from"]["entity"]: t["entity"] for t in sp["tables"] if (t["registry"]["renamed_from"] or {}).get("entity")}


def renamed_variant(doc, sp: dict):
    """The capture with every entity name and every entity alias swapped for the current one. Aliases are the ontology lineage tags, a uuid5 of
    the entity name, so the old alias of each renamed entity is recomputed from its old name. Only a string equal to one of those is touched:
    relationship names, relationship aliases and property names are not renamed."""
    names = entity_renames(sp)
    swap = {**names, **{oac.lineage_tag(sp, "entity", old): oac.lineage_tag(sp, "entity", new) for old, new in names.items()}}
    if isinstance(doc, dict):
        return {k: renamed_variant(v, sp) for k, v in doc.items()}
    if isinstance(doc, list):
        return [renamed_variant(v, sp) for v in doc]
    return swap.get(doc, doc) if isinstance(doc, str) else doc


def codes(rep: oac.Report) -> set:
    return {e.split(":")[0] for e in rep.errors}


def mutated(parts: dict, name: str, change) -> dict:
    doc = json.loads(parts[name])
    change(doc)
    return {**parts, name: json.dumps(doc, indent=2) + "\n"}


def node_table(doc: dict, entity: str) -> dict:
    return next(t for t in doc["nodeTables"] if t["dataSourceName"] == entity)


def read_ontology(ont: dict) -> dict:
    """The generated ontology TMDL read back: entities, table columns, Delta table names and relationships."""
    def objs(path: str, kind: str) -> list:
        return [n for n in oac.parse_tmdl(ont[path]) if n.kind == kind]

    out: dict = {"entities": {}, "columns": {}, "delta": {}, "rels": {}}
    for path in ont:
        if path.startswith("entities/"):
            e = objs(path, "entity")[0]
            out["entities"][e.name] = {"tag": e.props["lineageTag"], "table": oac.unquote(e.props["backingTable"]),
                                       "key": oac.unquote(e.props["keyProperty"]), "props": [p.name for p in e.kids("property")]}
        elif path.startswith("tables/"):
            t = objs(path, "table")[0]
            out["columns"][t.name] = {c.name: (c.props["dataType"], c.props["sourceColumn"]) for c in t.kids("column")}
            out["delta"][t.name] = t.kids("partition")[0].kids("source")[0].props["entityName"]
    fks = {r.name: oac._split_ref(r.props["fromColumn"]) for r in objs("relationships.tmdl", "relationship")}
    for er in objs("entityRelationships.tmdl", "entityRelationship"):
        out["rels"][er.name] = {"tag": er.props["lineageTag"], "from": er.props["fromEntity"], "to": er.props["toEntity"],
                                "fk": fks[er.kids("backingConfiguration")[0].props["relationship"]]}
    return out


# --------------------------------------------------------------------------------------------------
# generator
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", oac.GRAPH_PARTS)
def test_core_graph_equals_the_portal_reference(core, core_parts, name):
    got = json.loads(core_parts[name])
    want = renamed_variant(physical_variant(name, json.loads((FIXTURE / name).read_text(encoding="utf-8"))), core)
    assert by_key(got) == by_key(want)


def test_the_capture_is_renamed_by_the_registry_and_by_nothing_else(core):
    """The mapping is not vacuous (the capture holds every renamed core entity under its old name and alias) and it is exactly the registry's:
    after it no old name or old alias is left in the capture, and every renamed entity is there under the current name and alias."""
    renames = entity_renames(core)
    assert set(renames) <= {t["registry"]["renamed_from"]["entity"] for t in core["tables"] if t["registry"]["renamed_from"].get("entity")}
    raw = {n: (FIXTURE / n).read_text(encoding="utf-8") for n in oac.GRAPH_PARTS}
    for old, new in renames.items():
        assert f'"{old}"' in raw["graphType.json"] and f'"{new}"' not in raw["graphType.json"]
        assert oac.lineage_tag(core, "entity", old) in raw["graphType.json"] and oac.lineage_tag(core, "entity", new) not in raw["graphType.json"]
    renamed = {n: renamed_variant(json.loads(raw[n]), core) for n in ("graphType.json", "dataSources.json", "graphDefinition.json")}
    text = json.dumps(renamed)
    assert all(f'"{old}"' not in text and oac.lineage_tag(core, "entity", old) not in text for old in renames)
    assert all(f'"{new}"' in text and oac.lineage_tag(core, "entity", new) in text for new in renames.values())


def test_the_delta_paths_are_the_physical_lakehouse_tables_and_never_a_logical_id(model, core, core_parts):
    sources = json.loads(core_parts["dataSources.json"])["dataSources"]
    paths = {s["name"]: s["properties"]["path"].rsplit("/", 1)[-1] for s in sources}
    assert paths == {t["entity"]: t["physical"] for t in core["tables"] if t["ontology"]}
    assert paths["Account"] == "dim_account" and paths["Transaction"] == "fact_transaction" and paths["Law"] == "ref_law"
    registry = physical_names(model)
    assert set(paths.values()) <= set(registry.values()) and not set(paths.values()) & set(registry)       # physical names, no logical id


def test_fixture_holds_placeholders_not_real_ids():
    ctx = oac.make_ctx()
    text = (FIXTURE / "dataSources.json").read_text(encoding="utf-8")
    assert text.count(f"abfss://{ctx['workspace_id']}@onelake.pbidedicated.windows.net/{ctx['lakehouse_id']}/Tables/") == 31
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text, re.I)              # no workspace or lakehouse GUID left


def test_core_graph_has_31_nodes_and_53_edges(core_parts):
    gt, ds, gd = docs(core_parts)
    assert len(gt["nodeTypes"]) == 31 and len(gt["edgeTypes"]) == 53
    assert len(ds["dataSources"]) == 31 and len(gd["nodeTables"]) == 31 and len(gd["edgeTables"]) == 53
    kinds = collections.Counter(p["type"] for n in gt["nodeTypes"] for p in n["properties"])
    assert kinds == {"STRING": 282, "FLOAT": 65, "ZONED DATETIME": 38, "INT": 24, "BOOLEAN": 8}


def test_generated_order_follows_the_spec(core, core_parts):
    gt, ds, gd = docs(core_parts)
    ont = [t for t in core["tables"] if t["ontology"]]
    assert [n["labels"][0] for n in gt["nodeTypes"]] == [t["entity"] for t in ont]
    assert [[p["name"] for p in n["properties"]] for n in gt["nodeTypes"]] == [[c["prop"] for c in t["columns"] if c["ontology"]] for t in ont]
    assert [e["labels"][0] for e in gt["edgeTypes"]] == [r["name"].replace(" ", "_") for r in core["relationships"] if r["ontology"]]
    assert [d["name"] for d in ds["dataSources"]] == [t["entity"] for t in ont]
    assert [t["nodeTypeAlias"] for t in gd["nodeTables"]] == [n["alias"] for n in gt["nodeTypes"]]
    assert [t["edgeTypeAlias"] for t in gd["edgeTables"]] == [e["alias"] for e in gt["edgeTypes"]]


def test_only_what_the_ontology_uses_reaches_the_graph(core, core_parts):
    gt, _, gd = docs(core_parts)
    nodes, edges = {n["labels"][0]: n for n in gt["nodeTypes"]}, {e["labels"][0] for e in gt["edgeTypes"]}
    hidden = [(t["entity"], c) for t in core["tables"] if t["ontology"] for c in t["columns"] if not c["ontology"]]
    assert {"DateOfBirth", "KycAddress", "Email", "PostedBalancePaise", "PolicyNumber", "KyaKeyFingerprint"} <= {c["prop"] for _, c in hidden}
    for en, c in hidden:
        assert c["prop"] not in {p["name"] for p in nodes[en]["properties"]}
        assert c["col"] not in {m["sourceColumn"] for m in node_table(gd, en)["propertyMappings"]}
    assert [t["entity"] for t in core["tables"] if not t["ontology"]] == ["Calendar"] and "Calendar" not in nodes      # semantic-model-only table
    skipped = [r["name"].replace(" ", "_") for r in core["relationships"] if not r["ontology"]]
    assert "Transaction_Posted_On_Date" in skipped and edges.isdisjoint(skipped)


def test_aliases_are_the_lineage_tags_of_the_ontology(made):
    sp, ont, parts = made
    o, (gt, _, _) = read_ontology(ont), docs(parts)
    nodes, edges = {n["labels"][0]: n["alias"] for n in gt["nodeTypes"]}, {e["labels"][0]: e["alias"] for e in gt["edgeTypes"]}
    assert len(nodes) == len(gt["nodeTypes"]) and len(edges) == len(gt["edgeTypes"])
    assert nodes == {en: e["tag"] for en, e in o["entities"].items()}
    assert edges == {name.replace(" ", "_"): r["tag"] for name, r in o["rels"].items()}
    assert all(nodes[en] == oac.lineage_tag(sp, "entity", en) for en in o["entities"])
    assert all(edges[name.replace(" ", "_")] == oac.lineage_tag(sp, "entityRelationship", name) for name in o["rels"])


def test_graph_is_consistent_with_the_ontology_tmdl(made):
    sp, ont, parts = made
    o, (gt, ds, gd) = read_ontology(ont), docs(parts)
    nodes, edges = {n["labels"][0]: n for n in gt["nodeTypes"]}, {e["labels"][0]: e for e in gt["edgeTypes"]}
    paths = {d["name"]: d["properties"]["path"] for d in ds["dataSources"]}
    node_tables, edge_tables = {t["nodeTypeAlias"]: t for t in gd["nodeTables"]}, {t["edgeTypeAlias"]: t for t in gd["edgeTables"]}
    assert set(nodes) == set(o["entities"]) == set(paths) and len(edges) == len(o["rels"])
    for en, e in o["entities"].items():
        n, cols = nodes[en], o["columns"][e["table"]]
        t = node_tables[n["alias"]]
        assert n["primaryKeyProperties"] == [e["key"]] and [p["name"] for p in n["properties"]] == e["props"]
        assert [p["type"] for p in n["properties"]] == [GRAPH_OF_TMDL[cols[p][0]] for p in e["props"]]
        assert t["dataSourceName"] == en and [(m["propertyName"], m["sourceColumn"]) for m in t["propertyMappings"]] == [(p, cols[p][1]) for p in e["props"]]
        assert paths[en].endswith("/Tables/" + o["delta"][e["table"]])
    for name, r in o["rels"].items():
        e, src = edges[name.replace(" ", "_")], o["entities"][r["from"]]
        fk_table, fk_prop = r["fk"]
        t, cols = edge_tables[e["alias"]], o["columns"][src["table"]]
        assert fk_table == src["table"]
        assert e["sourceNodeType"]["alias"] == src["tag"] and e["destinationNodeType"]["alias"] == o["entities"][r["to"]]["tag"]
        assert t["dataSourceName"] == r["from"] and t["edgeIdMapping"] == [] and t["propertyMappings"] == [] and e["properties"] == []
        assert t["sourceNodeKeyColumns"] == [cols[src["key"]][1]] and t["destinationNodeKeyColumns"] == [cols[fk_prop][1]]


def test_generation_is_deterministic(spec_for, tier):
    sp = spec_for(tier)
    before, first = json.dumps(sp, sort_keys=True), oac.build_graph(sp, oac.make_ctx())
    assert oac.build_graph(sp, oac.make_ctx()) == first
    assert oac.build_graph(spec_for(tier), oac.make_ctx()) == first                  # a freshly built spec gives the same text
    assert json.dumps(sp, sort_keys=True) == before                                  # the spec is not modified
    assert tuple(first) == oac.GRAPH_PARTS
    assert all(text == json.dumps(json.loads(text), indent=2) + "\n" for text in first.values())


def test_ids_only_change_the_data_source_paths(core, core_parts):
    other = oac.build_graph(core, oac.make_ctx(**IDS))
    assert {n for n in core_parts if core_parts[n] != other[n]} == {"dataSources.json"}
    assert other["dataSources.json"].count(f"abfss://{IDS['workspace_id']}@onelake.pbidedicated.windows.net/{IDS['lakehouse_id']}/Tables/") == 31


def test_graph_types_follow_the_tmdl_types():
    assert set(oac.GRAPH_TYPE) == set(oac.TMDL_TYPE)
    assert oac.GRAPH_TYPE == {k: GRAPH_OF_TMDL[v] for k, v in oac.TMDL_TYPE.items()}
    assert oac.GRAPH_TYPE["date"] == oac.GRAPH_TYPE["datetime"] == "ZONED DATETIME"


def test_lineage_tag_is_a_stable_uuid5_of_spec_kind_and_name(core):
    want = str(uuid.uuid5(uuid.NAMESPACE_URL, "urn:ontology-as-code:ContosoBank:entity:Customer"))
    assert oac.lineage_tag(core, "entity", "Customer") == want == "c4375f7f-077f-5d51-9944-2605a9d7ab52"      # the node alias the portal kept
    assert len({oac.lineage_tag(core, k, "Customer") for k in ("entity", "table", "column")}) == 3
    assert oac.lineage_tag({**core, "name": "Other"}, "entity", "Customer") != want


def test_parts_make_the_update_definition_payload(made):
    payload = oac.to_payload(made[2])
    assert [p["path"] for p in payload["parts"]] == sorted(oac.GRAPH_PARTS) and "format" not in payload
    assert {p["path"]: base64.b64decode(p["payload"]).decode("utf-8") for p in payload["parts"]} == made[2]
    assert len(json.dumps(payload)) < 1_000_000


# --------------------------------------------------------------------------------------------------
# validator
# --------------------------------------------------------------------------------------------------
def test_every_tier_builds_a_valid_graph(made):
    sp, _, parts = made
    rep = oac.validate_graph(parts, sp)
    assert rep.ok, rep.errors
    assert {w.split(":")[0] for w in rep.warnings} == {"G003"}                       # only the unresolved make_ctx() placeholders
    resolved = oac.validate_graph(oac.build_graph(sp, oac.make_ctx(**IDS)), sp, strict=True)
    assert resolved.ok and not resolved.warnings, resolved.errors + resolved.warnings
    ont = [t for t in sp["tables"] if t["ontology"]]
    assert rep.stats == {"parts": 5, "nodeTypes": len(ont), "dataSources": len(ont), "properties": sum(1 for t in ont for c in t["columns"] if c["ontology"]),
                         "edgeTypes": sum(1 for r in sp["relationships"] if r["ontology"])}


def test_validator_catches_a_duplicate_alias(core, core_parts):
    bad = mutated(core_parts, "graphType.json", lambda d: d["nodeTypes"][1].update(alias=d["nodeTypes"][0]["alias"]))
    rep = oac.validate_graph(bad, core)
    assert not rep.ok and "G010" in codes(rep)


def test_validator_catches_an_edge_to_an_unknown_node(core, core_parts):
    bad = mutated(core_parts, "graphType.json", lambda d: d["edgeTypes"][0]["destinationNodeType"].update(alias=str(uuid.uuid4())))
    rep = oac.validate_graph(bad, core)
    assert not rep.ok and "G021" in codes(rep)
    bad = mutated(core_parts, "graphType.json", lambda d: d["edgeTypes"][0]["sourceNodeType"].update(alias=str(uuid.uuid4())))
    assert "G021" in codes(oac.validate_graph(bad, core))


@pytest.mark.parametrize("name", oac.GRAPH_PARTS)
def test_validator_catches_bad_json_and_missing_parts(core, core_parts, name):
    for text in ("{not json", "[]", ""):
        rep = oac.validate_graph({**core_parts, name: text}, core)
        assert not rep.ok and "G002" in codes(rep), text
    rep = oac.validate_graph({k: v for k, v in core_parts.items() if k != name}, core)
    assert not rep.ok and "G001" in codes(rep)


def test_validator_catches_leftover_placeholders_only_in_strict_mode(core, core_parts):
    lax, strict = oac.validate_graph(core_parts, core), oac.validate_graph(core_parts, core, strict=True)
    assert lax.ok and {w.split(":")[0] for w in lax.warnings} == {"G003"}
    assert not strict.ok and codes(strict) == {"G003"} and not strict.warnings
    assert any("<WORKSPACE_ID>" in e for e in strict.errors) and any("<LAKEHOUSE_ID>" in e for e in strict.errors)
    assert oac.validate_graph(oac.build_graph(core, oac.make_ctx(**IDS)), core, strict=True).ok


BREAKS = [          # code, part, what is broken
    ("G011", "graphType.json", "primary key that is not a property", lambda d: d["nodeTypes"][0].update(primaryKeyProperties=["NoSuchProperty"])),
    ("G011", "graphType.json", "no primary key", lambda d: d["nodeTypes"][0].update(primaryKeyProperties=[])),
    ("G012", "graphType.json", "unknown property type", lambda d: d["nodeTypes"][0]["properties"][0].update(type="DECIMAL")),
    ("G020", "graphType.json", "edge alias used twice", lambda d: d["edgeTypes"][1].update(alias=d["edgeTypes"][0]["alias"])),
    ("G020", "graphType.json", "edge alias equal to a node alias", lambda d: d["edgeTypes"][0].update(alias=d["nodeTypes"][0]["alias"])),
    ("G030", "dataSources.json", "data source twice", lambda d: d["dataSources"].append(dict(d["dataSources"][0]))),
    ("G031", "dataSources.json", "data source that is no entity", lambda d: d["dataSources"].append({"name": "Calendar", "type": "DeltaTable", "properties": {"path": "x"}})),
    ("G032", "dataSources.json", "data source on a lakehouse table that is not the entity's (a v1 logical id)",
     lambda d: d["dataSources"][0]["properties"].update(path=d["dataSources"][0]["properties"]["path"].rsplit("/", 1)[0] + "/customer")),
    ("G040", "graphDefinition.json", "node table missing", lambda d: d["nodeTables"].pop(0)),
    ("G040", "graphDefinition.json", "node table twice", lambda d: d["nodeTables"].append(dict(d["nodeTables"][0]))),
    ("G041", "graphDefinition.json", "node table of an unknown node type", lambda d: d["nodeTables"][0].update(nodeTypeAlias="no-such-alias")),
    ("G042", "graphDefinition.json", "node table without its data source", lambda d: d["nodeTables"][0].update(dataSourceName="NoSuchSource")),
    ("G043", "graphDefinition.json", "mapping of an unknown property", lambda d: node_table(d, "Customer")["propertyMappings"][0].update(propertyName="NoSuchProperty")),
    ("G044", "graphDefinition.json", "mapping of a column the ontology hides", lambda d: node_table(d, "Customer")["propertyMappings"][1].update(sourceColumn="date_of_birth")),
    ("G044", "graphDefinition.json", "mapping of a missing column", lambda d: node_table(d, "Customer")["propertyMappings"][1].update(sourceColumn="no_such_column")),
    ("G045", "graphDefinition.json", "property without a mapping", lambda d: node_table(d, "Customer")["propertyMappings"].pop()),
    ("G050", "graphDefinition.json", "edge table missing", lambda d: d["edgeTables"].pop(0)),
    ("G051", "graphDefinition.json", "edge table of an unknown edge type", lambda d: d["edgeTables"][0].update(edgeTypeAlias="no-such-alias")),
    ("G052", "graphDefinition.json", "edge table without its data source", lambda d: d["edgeTables"][0].update(dataSourceName="NoSuchSource")),
    ("G053", "graphDefinition.json", "foreign key column that is not in the table", lambda d: d["edgeTables"][0].update(destinationNodeKeyColumns=["no_such_column"])),
    ("G053", "graphDefinition.json", "key column that is not in the table", lambda d: d["edgeTables"][0].update(sourceNodeKeyColumns=["no_such_column"])),
    ("G053", "graphDefinition.json", "no source key column", lambda d: d["edgeTables"][0].update(sourceNodeKeyColumns=[])),
]


@pytest.mark.parametrize("code,part,change", [pytest.param(c, p, f, id=f"{c}-{w}") for c, p, w, f in BREAKS])
def test_validator_catches_every_kind_of_inconsistency(core, core_parts, code, part, change):
    rep = oac.validate_graph(mutated(core_parts, part, change), core)
    assert not rep.ok and code in codes(rep), rep.errors
