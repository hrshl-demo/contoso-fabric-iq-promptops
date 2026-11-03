"""The layer-aware pipeline: which tables a tier needs of each layer, the load plan and its fingerprints, the Parquet schema of every layer (read
back from what `build` writes), and the order, resumption and verification of the load against the in-process Fabric mock.
Offline: no Azure, Fabric or GitHub call. The mock Fabric and the fake `az` are the only counterparts, and nothing here names a gold or silver
table except through the specification and the manifests (a few generic tables of a synthetic output folder aside)."""
import base64
import datetime
import decimal
import hashlib
import itertools
import json
import re
import shutil
import sys
import threading
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import requests

from contoso_onto import cli, fabric_ops, layers, oac, pipeline, runner, silver, spec as spec_mod
from contoso_onto.gold import physical_names
from contoso_onto.layers import LAYERS, PlanError, bronze_target, load_plan
from mock_fabric import MockFabric, VirtualTime
from test_runner import TOKENS, World

KIT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True, scope="module")
def no_real_network():
    """Nothing here reaches Azure or Fabric. The build and diagnose call the live ontology MCP endpoint: it answers from this process. The
    delta-rs fallback writes to abfss://...onelake...: a test may use it only with a local-disk uri_base. Both hold in module scope, so the
    module-scoped build below is covered as well."""
    real = fabric_ops.Ops.load_layer_delta

    def guard(self, layer_dir, ws_id, lh_id, only=None, uri_base=None, storage_options=None, layer=None):
        assert uri_base, "a test would write to the real OneLake with delta-rs"
        return real(self, layer_dir, ws_id, lh_id, only, uri_base, storage_options, layer)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fabric_ops, "mcp_tools_list", lambda url, token, timeout=60: {"ok": True, "status": 200, "tools": ["ask_ontology"]})
        mp.setattr(fabric_ops.Ops, "load_layer_delta", guard)
        yield


# ---------------------------------------------------------------------------------------------- a small synthetic output folder
def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_table(folder: Path, name: str, rows: int, version: int) -> dict:
    """One tiny Parquet table and its manifest entry; the content (and so the hash) depends on `version`."""
    tbl = pa.table({"id": list(range(rows)), "note": [f"{name}-v{version}"] * rows})
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{name}.parquet"
    pq.write_table(tbl, p, compression="snappy", version="2.6")
    return {"physical": name, "rows": rows, "columns": 2, "bytes": p.stat().st_size, "sha256": sha(p.read_bytes()), "schema": pipeline.schema_of(tbl)}


def make_out(out: Path, *, files: int = 3, silver_tables: int = 3, gold_tables: int = 4, versions: dict | None = None) -> Path:
    """A layered output folder as `build` writes it (manifests, Parquet, staged seed files) with generic names: no real table is involved."""
    v = {"bronze": 1, "silver": 1, "gold": 1, **(versions or {})}
    rows = []
    for i in range(files):
        release = None if i == 0 else "rel-a" if i % 2 else "rel-b"
        path = f"data/relationship/f{i}.json" if release is None else f"data/releases/{release}/f{i}.json"
        data = json.dumps({"file": i, "version": v["bronze"]}).encode()
        dest = layers.staged_path(out, release, path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        rows.append({"release": release, "path": path, "sha256": sha(data), "bytes": len(data), "record_count": 1})
    manifest_table = write_table(out / "bronze", "bronze_file_manifest", files, v["bronze"])
    (out / "bronze" / "manifest.json").write_text(json.dumps({"layer": "bronze", "files": rows, "tables": {"bronze_file_manifest": manifest_table}}, sort_keys=True))
    silver_entries = {f"silver_t{i}": write_table(out / "silver", f"silver_t{i}", 2 + i, v["silver"]) for i in range(silver_tables)}
    (out / "silver" / "manifest.json").write_text(json.dumps({"layer": "silver", "tables": silver_entries}, sort_keys=True))
    gold_entries = {name: write_table(out / "gold", name, 3 + i, v["gold"]) for i, name in zip(range(gold_tables), (f"{'dim' if k % 2 == 0 else 'fact'}_t{k}" for k in itertools.count()))}
    (out / "manifest.json").write_text(json.dumps({"layer": "gold", "tables": gold_entries}, sort_keys=True))
    return out


def edit_manifest(path: Path, change) -> None:
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data, sort_keys=True))


@pytest.fixture()
def mock():
    m = MockFabric()
    yield m
    m.close()


@pytest.fixture()
def ops(mock):
    logs: list[str] = []
    fab = oac.Fabric(lambda s: TOKENS[s], api_base=f"{mock.base}/v1", pbi_base=f"{mock.base}/v1.0/myorg", log=logs.append, sleeper=lambda s: None)
    o = fabric_ops.Ops(fab, lambda s: TOKENS[s], logs.append, sleeper=lambda s: None, dfs_base=f"{mock.base}/dfs")
    o.logs = logs
    return o


@pytest.fixture()
def lakehouse(ops):
    ws = ops.ensure_workspace("w", "contosofabricf4")["workspaceId"]
    return ws, ops.ensure_lakehouse(ws, "L")["lakehouseId"]


def loaded(mock) -> list[str]:
    return [t["table"] for t in mock.table_loads]


def bronze_uploads(mock) -> list[str]:
    return sorted(r["path"] for r in mock.requests if r["method"] == "PUT" and "/Files/bronze/" in r["path"])


def files_tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): sha(p.read_bytes()) for p in sorted(root.rglob("*")) if p.is_file()}


# ================================================================================================ P1: what a tier needs of each layer
SILVER_SPEC = {"tables": [
    {"table": "silver_x", "sources": [{"release": "rel-a", "file": "x.json"}]},
    {"table": "silver_y", "sources": [{"release": "rel-a", "file": "y.json"}, {"release": "rel-b", "file": "y.json"}]},
    {"table": "silver_z", "sources": [{"release": "rel-b", "file": "z.json"}]},
    {"table": "silver_w", "sources": [{"data": "data/relationship/w.json"}]}]}


def test_the_layers_behind_gold_tables_are_exactly_the_silver_their_lineage_names_and_the_files_those_read():
    gold = [{"table": "a", "physical": "dim_a", "silver": ["silver_y", "silver_x"]}, {"table": "b", "physical": "fact_b", "silver": ["silver_y"]},
            {"table": "r", "physical": "ref_r", "reference": True}, {"table": "c"}]
    sel = pipeline.layers_for("demo", gold, SILVER_SPEC)
    assert sel.gold == ["a", "b", "r", "c"] and sel.gold_physical == ["dim_a", "fact_b", "ref_r", "c"]          # the physical name falls back to the logical id
    assert sel.silver == ["silver_x", "silver_y"]                                                              # silver spec order; z and w are not needed, reference tables bring none
    assert sel.bronze == ["data/releases/rel-a/release.json", "data/releases/rel-a/x.json", "data/releases/rel-a/y.json",
                          "data/releases/rel-b/release.json", "data/releases/rel-b/y.json"]                    # the files those two read, and their releases' release.json
    assert sel.silver_tables == ["silver_x", "silver_y", silver.ENTITY_CATALOG, silver.ATTRIBUTE_CATALOG]
    assert sel.counts() == {"bronzeFiles": 5, "silverTables": 4, "goldTables": 4} and sel.summary() == "5 bronze files, 4 silver tables, 4 gold tables"


def test_a_reference_only_selection_needs_no_silver_and_no_bronze():
    sel = pipeline.layers_for("demo", [{"table": "r", "reference": True}], SILVER_SPEC)
    assert sel.silver == [] and sel.bronze == [] and sel.silver_tables == [silver.ENTITY_CATALOG, silver.ATTRIBUTE_CATALOG]


def test_a_lineage_that_names_an_unknown_silver_table_is_an_error():
    with pytest.raises(ValueError, match="silver_ghost"):
        pipeline.layers_for("demo", [{"table": "a", "silver": ["silver_x", "silver_ghost"]}], SILVER_SPEC)


@pytest.fixture(scope="module")
def selections(model, silver_spec):
    return pipeline.tier_selections(model, silver_spec)


def test_each_tier_selects_its_own_gold_tables_and_the_silver_behind_them(selections, model, silver_spec):
    assert tuple(selections) == spec_mod.TIERS
    registry = physical_names(model)
    for tier, sel in selections.items():
        tables = spec_mod.make_spec(model, tier)["tables"]
        assert sel.gold == [t["table"] for t in tables] and sel.gold_physical == [registry[t["table"]] for t in tables]
        serves = {t["table"]: set(t["serves"]) for t in silver_spec["tables"]}
        assert sel.silver and all(serves[s] & set(sel.gold) for s in sel.silver), tier         # every selected silver table feeds a selected gold table
        for t in tables:                                                                           # and every gold table's silver is selected
            assert set(t.get("silver") or []) <= set(sel.silver)


def test_the_tiers_nest_and_a_smoke_build_is_much_smaller_than_a_full_one(selections, silver_spec):
    smoke, core, full = (selections[t] for t in spec_mod.TIERS)
    for small, big in ((smoke, core), (core, full)):
        assert set(small.gold) < set(big.gold) and set(small.silver) < set(big.silver) and set(small.bronze) < set(big.bronze)
    assert set(full.silver) == {t["table"] for t in silver_spec["tables"]}                          # the full tier needs every silver table
    assert len(smoke.gold) * 3 < len(full.gold) and len(smoke.silver_tables) * 2 < len(full.silver_tables) and len(smoke.bronze) * 2 < len(full.bronze)


def test_both_is_the_full_set(model, silver_spec, selections):
    both = pipeline.select_layers(model, silver_spec, "both")
    assert both.gold == selections["full"].gold and both.silver == selections["full"].silver and both.bronze == selections["full"].bronze


# ================================================================================================ P1: writing exactly that
@pytest.fixture(scope="module")
def written(model, built, repo_path, selections, tmp_path_factory):
    """Each tier written into its own folder from the session's build (no second build)."""
    out = {}
    for tier in spec_mod.TIERS:
        folder = tmp_path_factory.mktemp(f"layers_{tier}")
        out[tier] = (folder, pipeline.write_layers(model, built, folder, repo_path, KIT / "model", tier))
    return out


@pytest.mark.parametrize("tier", spec_mod.TIERS)
def test_a_tier_writes_only_what_it_needs_and_the_catalogs_describe_exactly_that(tier, written, selections, repo_path):
    folder, lo = written[tier]
    sel = selections[tier]
    assert lo.selection == sel
    gold_files = {p.stem for p in (folder / "gold").glob("*.parquet")}
    silver_files = {p.stem for p in (folder / "silver").glob("*.parquet")}
    assert gold_files == set(sel.gold_physical) == set(lo.gold["tables"])
    assert silver_files == set(sel.silver_tables) == set(lo.silver["tables"])
    assert {p.stem for p in (folder / "bronze").glob("*.parquet")} == {silver.BRONZE_MANIFEST_TABLE} == set(lo.bronze["tables"])
    # the catalogs and the bronze manifest describe the tables that are there, no more
    entity = pq.read_table(folder / "silver" / f"{silver.ENTITY_CATALOG}.parquet").to_pylist()
    attribute = pq.read_table(folder / "silver" / f"{silver.ATTRIBUTE_CATALOG}.parquet").to_pylist()
    assert {r["silver_table"] for r in entity} == set(sel.silver) and len(entity) == len(sel.silver)
    assert {r["silver_table"] for r in attribute} == set(sel.silver)
    for r in entity:                                                                              # the catalog's row count is the table's
        assert r["row_count"] == lo.silver["tables"][r["silver_table"]]["rows"]
    by_table = collections_by(attribute)
    for name in sel.silver:                                                                        # every column of a silver table has an attribute row
        assert by_table[name] == [f.name for f in pq.read_schema(folder / "silver" / f"{name}.parquet")]
    bronze_rows = pq.read_table(folder / "bronze" / f"{silver.BRONZE_MANIFEST_TABLE}.parquet").to_pylist()
    assert [r["path"] for r in lo.bronze["files"]] == sel.bronze == [r["path"] for r in bronze_rows]
    assert lo.bronze["files"] == bronze_rows
    # the seed files are staged verbatim, in the layout of OneLake's Files/bronze
    staged = files_tree(folder / "bronze" / "files")
    assert len(staged) == len(sel.bronze)
    for row in lo.bronze["files"]:
        rel = bronze_target(row["release"], row["path"])[len(layers.BRONZE_FOLDER) + 1:]
        assert staged[rel] == row["sha256"] == sha((repo_path / row["path"]).read_bytes())
    plan = load_plan(folder)
    assert plan.summary() == sel.summary() and plan.names("gold") == sorted(sel.gold_physical) and plan.names("silver") == sorted(sel.silver_tables)


COLUMN_FIELD = silver.ATTRIBUTE_CATALOG_COLUMNS[1]               # the catalog's column-name field; never spelled out here (the name may change)


def collections_by(attribute_rows: list[dict]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in attribute_rows:
        out.setdefault(r["silver_table"], []).append(r[COLUMN_FIELD])
    return out


def test_a_smaller_tier_written_over_a_larger_one_leaves_nothing_of_it(model, built, repo_path, written, selections, tmp_path):
    folder, _ = written["full"]
    shutil.copytree(folder, tmp_path / "out")
    pipeline.write_layers(model, built, tmp_path / "out", repo_path, KIT / "model", "smoke")
    assert files_tree(tmp_path / "out" / "gold") == files_tree(written["smoke"][0] / "gold")
    assert files_tree(tmp_path / "out" / "silver") == files_tree(written["smoke"][0] / "silver")
    assert files_tree(tmp_path / "out" / "bronze") == files_tree(written["smoke"][0] / "bronze")
    assert not [p for p in (tmp_path / "out" / "bronze" / "files").rglob("*") if p.is_dir() and not any(p.iterdir())]      # no empty folders either
    assert load_plan(tmp_path / "out").summary() == selections["smoke"].summary()


def test_every_layer_writer_prunes_to_its_manifest_when_it_is_called_on_its_own(model, built, silver_built, repo_path, selections, tmp_path):
    """silver's write_bronze and write_silver, write_gold and the staged seed files: a larger earlier write must not survive a smaller one."""
    sel = selections["smoke"]
    silver.write_bronze(silver_built, tmp_path)
    silver.write_silver(silver_built, tmp_path, KIT / "model")
    pipeline.write_gold(model, built, tmp_path)
    pipeline.stage_bronze(tmp_path, silver_built.bronze, repo_path)
    assert len(list((tmp_path / "silver").glob("*.parquet"))) > len(sel.silver_tables) and len(list((tmp_path / "gold").glob("*.parquet"))) > len(sel.gold)
    sliced = pipeline.slice_silver(silver_built, sel.silver)
    bronze, silver_manifest = silver.write_bronze(sliced, tmp_path), silver.write_silver(sliced, tmp_path, KIT / "model")
    gold = pipeline.write_gold(model, built, tmp_path, only=sel.gold)
    pipeline.stage_bronze(tmp_path, sliced.bronze, repo_path)
    for layer, manifest in (("bronze", bronze), ("silver", silver_manifest), ("gold", gold)):
        assert {p.stem for p in (tmp_path / layer).glob("*.parquet")} == set(manifest["tables"]), layer
    assert set(gold["tables"]) == set(sel.gold_physical) and set(silver_manifest["tables"]) == set(sel.silver_tables)
    staged = files_tree(tmp_path / "bronze" / "files")
    assert sorted(staged) == sorted(bronze_target(r["release"], r["path"])[len(layers.BRONZE_FOLDER) + 1:] for r in bronze["files"]) and len(staged) == len(sel.bronze)


def test_the_layers_are_written_byte_for_byte_the_same_twice(model, built, repo_path, written, tmp_path):
    pipeline.write_layers(model, built, tmp_path, repo_path, KIT / "model", "core")
    assert files_tree(tmp_path) == files_tree(written["core"][0])


def test_write_gold_refuses_tables_that_were_not_built(model, built, tmp_path):
    with pytest.raises(ValueError, match="were not built"):
        pipeline.write_gold(model, built, tmp_path, only=["no_such_table"])


# ================================================================================================ P2: the load plan (no I/O but reading its manifests)
def test_a_plan_lists_bronze_then_silver_then_gold_with_posix_targets(tmp_path):
    plan = load_plan(make_out(tmp_path))
    assert plan.layers() == ["bronze", "silver", "gold"] == list(LAYERS)
    assert plan.all_names() == ["bronze_file_manifest", "silver_t0", "silver_t1", "silver_t2", "dim_t0", "dim_t2", "fact_t1", "fact_t3"]          # sorted inside a layer
    assert plan.summary() == "3 bronze files, 3 silver tables, 4 gold tables" and plan.counts()["tables"] == 8 and plan.counts()["bronzeTables"] == 1
    assert [f.target for f in plan.files] == ["Files/bronze/no-release/data/relationship/f0.json", "Files/bronze/rel-a/data/releases/rel-a/f1.json",
                                              "Files/bronze/rel-b/data/releases/rel-b/f2.json"]
    assert all("\\" not in f.target and f.local.is_file() for f in plan.files) and plan.folder("silver") == tmp_path / "silver"
    assert plan.table_bytes("silver") == sum(t.bytes for t in plan.tables["silver"]) and plan.table_bytes("bronze") > plan.tables["bronze"][0].bytes


def test_bronze_targets_are_posix_paths_under_the_release_and_never_escape_files_bronze():
    assert bronze_target("retail4-v1", "data/releases/retail4-v1/bank/crm-cases.json") == "Files/bronze/retail4-v1/data/releases/retail4-v1/bank/crm-cases.json"
    assert bronze_target(None, "data/relationship/products.json") == "Files/bronze/no-release/data/relationship/products.json"
    for release, path in ((None, "../x.json"), (None, "/abs/x.json"), (None, "a\\b.json"), (None, ""), ("a/b", "x.json"), ("..", "x.json")):
        with pytest.raises(PlanError):
            bronze_target(release, path)


def test_the_files_of_a_manifest_that_escape_the_bronze_folder_are_refused(tmp_path):
    make_out(tmp_path)
    edit_manifest(tmp_path / "bronze" / "manifest.json", lambda m: m["files"][0].update(path="../../etc/passwd"))
    with pytest.raises(PlanError, match="without '..'"):
        load_plan(tmp_path)


def test_a_table_name_may_not_appear_in_two_layers(tmp_path):
    make_out(tmp_path)
    edit_manifest(tmp_path / "manifest.json", lambda m: m["tables"].update(silver_t0=m["tables"].pop("dim_t0")))
    with pytest.raises(PlanError, match="unique across layers"):
        load_plan(tmp_path)


def test_a_plan_needs_all_three_layers_and_each_manifest_must_name_its_own_layer(tmp_path):
    make_out(tmp_path)
    (tmp_path / "silver" / "manifest.json").unlink()
    with pytest.raises(PlanError, match="no silver layer.*run `build` first"):
        load_plan(tmp_path)
    make_out(tmp_path)
    edit_manifest(tmp_path / "silver" / "manifest.json", lambda m: m.update(layer="gold"))
    with pytest.raises(PlanError, match="describes the gold layer"):
        load_plan(tmp_path)


def test_a_plan_can_be_cut_down_to_layers_and_tables_across_layers(tmp_path):
    plan = load_plan(make_out(tmp_path))
    only_gold = plan.select(["gold"])
    assert only_gold.layers() == ["gold"] and not only_gold.files and only_gold.names("gold") == plan.names("gold")
    assert plan.select(["gold", "bronze"]).layers() == ["bronze", "gold"]                     # load order is kept whatever order was asked for
    some = plan.select(only=["silver_t1", "fact_t1"])
    assert some.layers() == ["silver", "gold"] and some.names("silver") == ["silver_t1"] and some.names("gold") == ["fact_t1"] and not some.files
    assert plan.select(only=["bronze_file_manifest"]).files == plan.files                      # the seed files travel with the table that lists them
    with pytest.raises(PlanError, match="no table named dim_t0 in the silver layer"):
        plan.select(["silver"], ["dim_t0"])
    with pytest.raises(PlanError, match="unknown layer 'platinum'"):
        plan.select(["platinum"])


def test_fingerprints_change_with_a_layer_and_with_every_layer_after_it(tmp_path):
    base = load_plan(make_out(tmp_path / "base")).fingerprints()
    assert load_plan(make_out(tmp_path / "again")).fingerprints() == base                       # the same data, the same fingerprints
    changed = {layer: load_plan(make_out(tmp_path / layer, versions={layer: 2})).fingerprints() for layer in LAYERS}
    differs = {layer: {x for x in LAYERS if changed[layer][x] != base[x]} for layer in LAYERS}
    assert differs == {"bronze": {"bronze", "silver", "gold"},       # gold is built from silver and silver from bronze
                       "silver": {"silver", "gold"},
                       "gold": {"gold"}}
    shape = load_plan(make_out(tmp_path / "shape", silver_tables=4)).fingerprints()             # a table added to silver: silver and gold
    assert {x for x in LAYERS if shape[x] != base[x]} == {"silver", "gold"}
    plans = [load_plan(make_out(tmp_path / f"p{i}", versions=v)) for i, v in enumerate(({}, {"gold": 2}, {"silver": 2}, {"bronze": 2}))]
    assert len({p.fingerprint() for p in plans}) == 4                                          # the whole-plan fingerprint moves with any layer


def test_row_counts_and_schemas_are_part_of_the_fingerprint_even_when_the_file_hash_is_the_same(tmp_path):
    make_out(tmp_path / "a")
    base = load_plan(tmp_path / "a").fingerprints()
    for name, layer, manifest, change in (("rows", "gold", "manifest.json", lambda m: m["tables"]["dim_t0"].update(rows=99)),
                                          ("schema", "gold", "manifest.json", lambda m: m["tables"]["dim_t0"]["schema"][1].update(type="large_string")),
                                          ("silver_schema", "silver", "silver/manifest.json", lambda m: m["tables"]["silver_t0"]["schema"][1].update(type="large_string"))):
        shutil.copytree(tmp_path / "a", tmp_path / name)
        edit_manifest(tmp_path / name / manifest, change)
        fps = load_plan(tmp_path / name).fingerprints()
        assert {x for x in LAYERS if fps[x] != base[x]} == {x for x in LAYERS if LAYERS.index(x) >= LAYERS.index(layer)}, name


# ================================================================================================ P5: Parquet schemas of every layer (read back from `build`)
@pytest.fixture(scope="module")
def full_plan(built_out):
    return load_plan(built_out)


def test_the_build_writes_all_three_layers_for_the_default_both_tier(full_plan, selections):
    assert full_plan.summary() == selections["full"].summary() and full_plan.counts()["bronzeTables"] == 1
    assert [t.layer for t in itertools.chain.from_iterable(full_plan.tables[layer] for layer in LAYERS)] == (
        ["bronze"] * 1 + ["silver"] * len(selections["full"].silver_tables) + ["gold"] * len(selections["full"].gold))


def test_the_manifests_describe_the_schemas_of_the_parquet_files(full_plan):
    for layer in LAYERS:
        for t in full_plan.tables[layer]:
            real = [{"name": f.name, "type": str(f.type), "nullable": f.nullable} for f in pq.read_schema(t.file)]
            assert real == t.schema, (layer, t.name)
            assert pq.read_metadata(t.file).num_rows == t.rows and t.file.stat().st_size == t.bytes and sha(t.file.read_bytes()) == t.sha256


@pytest.fixture(scope="module")
def silver_catalog(full_plan):
    rows = pq.read_table(full_plan.folder("silver") / f"{silver.ATTRIBUTE_CATALOG}.parquet").to_pylist()
    return {(r["silver_table"], r[COLUMN_FIELD]): r for r in rows}


def silver_columns(plan):
    for t in plan.tables["silver"]:
        for f in pq.read_schema(t.file):
            yield t.name, f.name, f.type


def test_silver_money_is_int64_paise_and_exact_rates_are_text(full_plan, silver_catalog):
    seen = {"minor": 0, "exact": 0}
    for table, column, typ in silver_columns(full_plan):
        cdm = (silver_catalog.get((table, column)) or {}).get("cdm_type")
        if column.endswith("Minor"):
            assert typ == pa.int64(), (table, column, typ)                                      # paise, never renamed to major units and never a double
        if cdm == "contosoBanking.minorAmount":
            assert typ == pa.int64() and column.endswith("Minor"), (table, column, typ)
            seen["minor"] += 1
        if cdm in ("contosoBanking.exactDecimal", "decimal"):
            assert typ == pa.string(), (table, column, typ)                                     # exact decimal text, no float rounding
            seen["exact"] += 1
    assert seen["minor"] > 20 and seen["exact"] > 5


def test_silver_has_decimal_38_4_only_on_official_name_alias_columns_and_no_floats(full_plan, silver_catalog):
    decimals = {(t, c): typ for t, c, typ in silver_columns(full_plan) if pa.types.is_decimal(typ)}
    aliases = {k for k, r in silver_catalog.items() if r["origin"] == "cdm_official" and r["match_type"] == "derived_major_units"}
    assert decimals and set(decimals) == aliases, "decimal columns must be exactly the derived-major-units official aliases of the catalog"
    assert set(decimals.values()) == {pa.decimal128(38, 4)}
    assert all(silver_catalog[k]["cdm_type"] == "currency" and silver_catalog[k]["origin"] == "cdm_official" for k in decimals)
    assert not [(t, c, typ) for t, c, typ in silver_columns(full_plan) if pa.types.is_floating(typ)]          # no float money, no float anything


def test_silver_dates_are_date32_and_instants_are_utc_microseconds(full_plan, silver_catalog):
    for table, column, typ in silver_columns(full_plan):
        cdm = (silver_catalog.get((table, column)) or {}).get("cdm_type")
        if cdm in ("date", "dateTime"):                                                         # dateTime: an official alias kept a date (no invented time of day)
            assert typ == pa.date32(), (table, column, typ)
        if cdm == "dateTimeOffset":
            assert typ == pa.timestamp("us", tz="UTC"), (table, column, typ)
        assert not pa.types.is_date64(typ) and (not pa.types.is_timestamp(typ) or typ == pa.timestamp("us", tz="UTC")), (table, column, typ)


def test_gold_has_no_decimals_and_money_comes_as_int64_paise_with_double_rupee_twins(full_plan):
    paise = 0
    for t in full_plan.tables["gold"]:
        schema = pq.read_schema(t.file)
        names = {f.name for f in schema}
        for f in schema:
            assert not pa.types.is_decimal(f.type) and not pa.types.is_float32(f.type), (t.name, f.name, f.type)     # decimals come back null from Fabric Graph
            if f.name.endswith("_paise"):
                paise += 1
                assert f.type == pa.int64() and f"{f.name[:-6]}_inr" in names, (t.name, f.name)
            if f.name.endswith("_inr"):
                assert f.type == pa.float64(), (t.name, f.name, f.type)
            if pa.types.is_timestamp(f.type):
                assert f.type == pa.timestamp("us", tz="UTC"), (t.name, f.name)
    assert paise > 10


SPECIAL = oac.SPECIAL_COL_CHARS
TABLE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def test_column_names_are_unique_ignoring_case_and_free_of_special_characters_in_every_layer(full_plan):
    for layer in LAYERS:
        for t in full_plan.tables[layer]:
            names = [f.name for f in pq.read_schema(t.file)]
            assert len({n.lower() for n in names}) == len(names), (layer, t.name, "duplicate column names ignoring case")
            for n in names:
                assert n and n.isascii() and not (set(n) & SPECIAL), (layer, t.name, n)           # Fabric would turn on column mapping, which the graph does not support


def test_table_names_are_lowercase_snake_case_short_and_unique_across_layers(full_plan):
    names = [(layer, t.name) for layer in LAYERS for t in full_plan.tables[layer]]
    assert len(names) == full_plan.counts()["tables"]
    assert all(TABLE_NAME.match(n) and len(n) <= 128 for _, n in names), [x for x in names if not TABLE_NAME.match(x[1])]
    assert len({n for _, n in names}) == len(names) == len({n.lower() for _, n in names})
    prefixes = {"bronze": ("bronze_",), "silver": ("silver_",), "gold": ("dim_", "fact_", "bridge_", "ref_")}
    for layer, n in names:
        assert n.startswith(prefixes[layer]), (layer, n)


def squashed(name: str) -> str:
    return " ".join((KIT / name).read_text(encoding="utf-8").split())


def test_the_docs_state_the_sizes_the_specification_and_the_build_compute(selections, silver_catalog):
    readme, checklist = squashed("README.md"), squashed("docs/LIVE_CHECKLIST.md")
    for tier, sel in selections.items():
        assert sel.summary() in readme and sel.summary() in checklist, tier                          # "18 bronze files, 15 silver tables, 13 gold tables"
        total = len(sel.gold) + len(sel.silver_tables) + 1                                           # plus the bronze manifest table
        assert f"{total} tables" in checklist and (tier == "smoke" or f"{total} tables" in readme), tier
    aliases = [r for r in silver_catalog.values() if r["origin"] == "cdm_official"]
    decimals = [r for r in aliases if r["cdm_type"] == "currency"]
    assert f"{len(aliases)} official-name alias columns ({len(decimals)} of them `decimal(38,4)`" in readme


# ================================================================================================ P6: seed files and layers against the mock
def test_bronze_files_land_verbatim_under_files_bronze_by_release_and_are_read_back(mock, ops, lakehouse, tmp_path):
    ws, lh = lakehouse
    plan = load_plan(make_out(tmp_path))
    res = ops.upload_bronze(plan.files, ws, lh, workers=1)
    assert res == {"uploaded": sorted(f.target for f in plan.files), "verified": 3, "unverified": []}
    for f in plan.files:
        assert mock.files[f"{ws}/{lh}/{f.target}"] == f.local.read_bytes()                      # byte for byte
    assert sorted(k[len(f"{ws}/{lh}/"):] for k in mock.files) == sorted(f.target for f in plan.files) and all(k.endswith(".json") for k in mock.files)
    dfs = [r for r in mock.requests if r["path"].startswith("/dfs/")]
    assert {r["auth"] for r in dfs} == {"Bearer tok-storage"} and len([r for r in dfs if r["method"] == "GET"]) == 3      # the storage audience; every file read back once
    assert ops.list_files(ws, lh) == {f.target: f.bytes for f in plan.files}


def test_a_truncated_upload_is_an_error_and_a_read_back_that_cannot_be_answered_is_only_reported(mock, ops, lakehouse, tmp_path):
    ws, lh = lakehouse
    plan = load_plan(make_out(tmp_path))
    mock.dfs_corrupt = True                                                                      # OneLake keeps one byte less than was sent
    with pytest.raises(oac.FabricError, match=r"OneLake holds \d+ bytes at Files/bronze/.*not the \d+ bytes"):
        ops.upload_bronze(plan.files, ws, lh, workers=1)
    mock.dfs_corrupt, mock.dfs_read_status = False, 403                                           # the read-back is refused: the upload stands, unverified
    res = ops.upload_bronze(plan.files, ws, lh, workers=1)
    assert res["verified"] == 0 and res["unverified"] == sorted(f.target for f in plan.files) and any("could not read" in m for m in ops.logs)
    mock.dfs_read_status = None
    mock.requests.clear()
    assert ops.upload_bronze(plan.files, ws, lh, workers=1, verify=False)["unverified"] == sorted(f.target for f in plan.files)
    assert not [r for r in mock.requests if r["method"] == "GET"]                                 # verify=False never reads back


def test_an_upload_that_is_not_where_it_should_be_is_an_error(mock, ops, lakehouse, tmp_path, monkeypatch):
    ws, lh = lakehouse
    plan = load_plan(make_out(tmp_path))
    monkeypatch.setattr(ops, "upload", lambda *a, **k: None)                                      # the upload "succeeds" but nothing is there
    with pytest.raises(oac.FabricError, match="does not have a file at that path"):
        ops.upload_bronze(plan.files, ws, lh, workers=1)


def test_a_staged_copy_that_is_not_what_the_manifest_says_is_never_uploaded_and_a_missing_one_says_to_build_again(mock, ops, lakehouse, tmp_path):
    ws, lh = lakehouse
    plan = load_plan(make_out(tmp_path))
    edited = sorted(plan.files, key=lambda f: f.target)[1]
    edited.local.write_bytes(b"{}")                                                              # changed after the build
    with pytest.raises(oac.FabricError, match="not what the bronze manifest says"):
        ops.upload_bronze(plan.files, ws, lh, workers=1)
    assert f"{ws}/{lh}/{edited.target}" not in mock.files
    edited.local.unlink()
    with pytest.raises(oac.FabricError, match="is not staged at .*run `build` again"):
        ops.upload_bronze(plan.files, ws, lh, workers=1)


def test_files_and_tables_load_in_layer_order_into_one_flat_staging_folder(mock, ops, lakehouse, tmp_path):
    ws, lh = lakehouse
    plan = load_plan(make_out(tmp_path))
    res = ops.load_layers(plan, ws, lh, workers=1)                                               # serial: the request order is exact
    assert list(res) == ["bronze", "silver", "gold"] and all(res[layer]["missingAfterLoad"] == [] for layer in LAYERS)
    assert loaded(mock) == plan.all_names()                                                       # the bronze table, the silver tables, then the gold tables
    assert [t["relativePath"] for t in mock.table_loads] == [f"Files/staging/{n}.parquet" for n in plan.all_names()]
    assert res["bronze"]["uploaded"]["uploaded"] == sorted(f.target for f in plan.files) and res["silver"]["loaded"] == plan.names("silver")
    first_file = next(i for i, r in enumerate(mock.requests) if r["method"] == "PUT" and "/Files/bronze/" in r["path"])
    first_load = next(i for i, r in enumerate(mock.requests) if r["path"].endswith("/load"))
    assert first_file < first_load                                                                # the seed files are in OneLake before the first table


def test_a_table_that_collides_in_parallel_is_retried_on_its_own_inside_its_layer(mock, ops, lakehouse, tmp_path):
    ws, lh = lakehouse
    plan = load_plan(make_out(tmp_path))
    mock.load_failures = {"silver_t1": 1}                                                         # refused once, as if the lakehouse were busy
    res = ops.load_layers(plan, ws, lh)                                                           # four workers
    assert res["silver"]["loaded"] == plan.names("silver") and len([r for r in mock.requests if r["path"].endswith("/tables/silver_t1/load")]) == 2
    assert mock.max_inflight_loads <= 4                                                           # never more than the worker cap


def test_the_sql_endpoint_is_asked_to_sync_once_per_layer_after_its_tables_and_before_the_next_layer(mock, ops, lakehouse, tmp_path):
    ws, lh = lakehouse
    plan = load_plan(make_out(tmp_path))
    ops.load_layers(plan, ws, lh, workers=1, sql_endpoint_id="sql-1")
    syncs = [i for i, r in enumerate(mock.requests) if r["path"].endswith("/sqlEndpoints/sql-1/refreshMetadata")]
    loads = {layer: [i for i, r in enumerate(mock.requests) if r["path"].endswith("/load") and any(f"/tables/{n}/load" in r["path"] for n in plan.names(layer))] for layer in LAYERS}
    assert len(syncs) == 3
    for i, layer in enumerate(LAYERS):
        assert max(loads[layer]) < syncs[i] and (i == 2 or syncs[i] < min(loads[LAYERS[i + 1]])), layer


def test_a_failing_silver_table_stops_before_gold_and_names_the_first_failing_table(mock, ops, lakehouse, tmp_path):
    ws, lh = lakehouse
    plan = load_plan(make_out(tmp_path))
    mock.load_failures = {"silver_t1": 99, "silver_t2": 99}
    with pytest.raises(fabric_ops.LayerLoadError) as err:
        ops.load_layers(plan, ws, lh, workers=1, fallback=False)
    ex = err.value
    assert (ex.layer, ex.table, ex.loaded, ex.remaining) == ("silver", "silver_t1", ["silver_t0"], ["silver_t1", "silver_t2"])
    assert "silver table silver_t1" in str(ex) and "ItemOperationInProgress" in str(ex)
    assert loaded(mock) == ["bronze_file_manifest", "silver_t0"] and not set(loaded(mock)) & set(plan.names("gold"))      # nothing of gold was started


def test_tables_that_are_not_registered_after_a_layer_stop_the_load_at_that_layer(mock, ops, lakehouse, tmp_path, monkeypatch):
    ws, lh = lakehouse
    plan = load_plan(make_out(tmp_path))
    monkeypatch.setattr(ops, "load_table", lambda *a, **k: None)                                  # the load "succeeds" but nothing is registered
    with pytest.raises(fabric_ops.LayerLoadError, match="1 bronze tables are not registered in the lakehouse yet: \\['bronze_file_manifest'\\]; re-run build to resume") as err:
        ops.load_layers(plan, ws, lh, workers=1)
    assert err.value.layer == "bronze" and not mock.table_loads


def test_tables_the_api_loaded_but_the_listing_shows_late_are_waited_for(mock, ops, lakehouse, tmp_path):
    ws, lh = lakehouse
    plan = load_plan(make_out(tmp_path))
    mock.list_lag = 4                                                                             # live, first run: the load answered, the table list did not show the table yet
    res = ops.load_layers(plan, ws, lh, workers=1)
    assert all(res[layer]["missingAfterLoad"] == [] for layer in LAYERS)
    assert sorted(loaded(mock)) == sorted(plan.names("bronze") + plan.names("silver") + plan.names("gold"))


def test_a_table_that_never_shows_in_the_listing_stops_the_layer_after_the_wait(mock, ops, lakehouse, tmp_path):
    ws, lh = lakehouse
    plan = load_plan(make_out(tmp_path))
    mock.list_lag = 10_000
    sleeps: list[float] = []
    ops.sleep = sleeps.append
    with pytest.raises(fabric_ops.LayerLoadError, match="1 bronze tables are not registered in the lakehouse yet") as err:
        ops.load_layers(plan, ws, lh, workers=1, register_wait_s=30.0)
    assert err.value.layer == "bronze" and sum(sleeps) == 30.0 and not set(loaded(mock)) & set(plan.names("silver"))


def test_skipped_layers_are_left_alone_and_progress_is_reported_per_layer(mock, ops, lakehouse, tmp_path):
    ws, lh = lakehouse
    plan = load_plan(make_out(tmp_path))
    events: list[tuple[str, str]] = []
    res = ops.load_layers(plan, ws, lh, workers=1, skip=lambda layer: layer == "silver", started=lambda layer: events.append(("start", layer)),
                          finished=lambda layer, r: events.append(("done", layer)), clock=iter([10.0, 12.5, 20.0, 31.0]).__next__)
    assert events == [("start", "bronze"), ("done", "bronze"), ("start", "gold"), ("done", "gold")] and res["silver"] == {"skipped": True}
    assert (res["bronze"]["seconds"], res["gold"]["seconds"]) == (2.5, 11.0) and not set(loaded(mock)) & set(plan.names("silver"))


def forbid_delta(monkeypatch):
    """delta-rs must not be touched by the test: the failure is not about the tables, or no table was refused."""
    def boom(self, *a, **k):
        raise AssertionError("delta-rs was used")
    monkeypatch.setattr(fabric_ops.Ops, "load_layer_delta", boom)


@pytest.mark.parametrize("how", ["400", "failed"])
@pytest.mark.parametrize("refused", [["silver_t1"], ["silver_t0", "silver_t2"]])
def test_only_the_tables_the_load_api_refuses_are_written_with_delta_rs_and_the_rest_stay_on_the_api(mock, ops, lakehouse, tmp_path, monkeypatch, how, refused):
    ws, lh = lakehouse
    plan = load_plan(make_out(tmp_path))
    mock.load_rejections = {t: how for t in refused}                                              # an HTTP 400, or an operation that ends Failed
    calls = []

    def fake_delta(self, layer_dir, ws_id, lh_id, only=None, uri_base=None, storage_options=None, layer=None):
        calls.append((layer, list(only)))
        mock.tables.setdefault(lh_id, []).extend(n for n in only if n not in mock.tables.get(lh_id, []))      # the lakehouse picks the written tables up
        return {"loaded": sorted(only), "missingAfterLoad": []}
    monkeypatch.setattr(fabric_ops.Ops, "load_layer_delta", fake_delta)
    res = ops.load_layers(plan, ws, lh)                                                           # four workers
    assert calls == [("silver", refused)]                                                         # only the refused tables; bronze, gold and the other silver tables never went there
    assert res["silver"]["loaded"] == plan.names("silver") and f"the Load Table API refused {len(refused)} of the 3 silver tables" in "\n".join(ops.logs)
    assert set(loaded(mock)) == {*plan.names("bronze"), *(set(plan.names("silver")) - set(refused)), *plan.names("gold")}


def test_without_delta_rs_a_table_the_api_refused_is_final_and_says_how_to_install_it(mock, ops, lakehouse, tmp_path, monkeypatch):
    ws, lh = lakehouse
    plan = load_plan(make_out(tmp_path))
    mock.load_rejections = {"silver_t1": "failed"}
    monkeypatch.setitem(sys.modules, "deltalake", None)                                           # `import deltalake` fails
    with pytest.raises(fabric_ops.LayerLoadError, match="delta-rs fallback is not installed in this Python environment.*pip install -r requirements-optional.txt") as err:
        ops.load_layers(plan, ws, lh, workers=1)
    assert (err.value.layer, err.value.table, err.value.rejected) == ("silver", "silver_t1", ["silver_t1"]) and not err.value.transient


def test_the_delta_loader_writes_every_layer_with_delta_rs_and_never_calls_the_load_api(mock, ops, lakehouse, tmp_path, monkeypatch):
    ws, lh = lakehouse
    plan = load_plan(make_out(tmp_path))
    calls = []

    def fake_delta(self, layer_dir, ws_id, lh_id, only=None, uri_base=None, storage_options=None, layer=None):
        calls.append((layer, list(only), Path(layer_dir).name))
        mock.tables.setdefault(lh_id, []).extend(only)
        return {"loaded": sorted(only), "missingAfterLoad": []}
    monkeypatch.setattr(fabric_ops.Ops, "load_layer_delta", fake_delta)
    ops.load_layers(plan, ws, lh, loader="delta")
    assert calls == [(layer, plan.names(layer), layer) for layer in LAYERS] and not mock.table_loads
    assert bronze_uploads(mock) == sorted(f"/dfs/{ws}/{lh}/{f.target}" for f in plan.files)       # the seed files go through OneLake either way


def delta_type(t: pa.DataType) -> str:
    if pa.types.is_decimal(t):
        return f"decimal({t.precision},{t.scale})"
    return {pa.string(): "string", pa.int64(): "long", pa.bool_(): "boolean", pa.date32(): "date", pa.float64(): "double",
            pa.timestamp("us", tz="UTC"): "timestamp"}[t]


def test_the_delta_fallback_keeps_decimal128_columns_intact(ops, tmp_path):
    from deltalake import DeltaTable
    folder = tmp_path / "silver"
    folder.mkdir()
    tbl = pa.table({"id": ["a", "b", "c"], "balance": pa.array([decimal.Decimal("1234567890123456789012.5000"), decimal.Decimal("-0.0100"), None], type=pa.decimal128(38, 4)),
                    "on": pa.array([datetime.date(2026, 9, 30), None, datetime.date(2026, 1, 1)], type=pa.date32()),
                    "at": pa.array([datetime.datetime(2026, 9, 30, 12, tzinfo=datetime.timezone.utc), None, None], type=pa.timestamp("us", tz="UTC")),
                    "paise": pa.array([123, None, 7], type=pa.int64())})
    pq.write_table(tbl, folder / "silver_dec.parquet")
    res = ops.load_layer_delta(folder, "ws", "lh", uri_base=str(tmp_path / "Tables"), storage_options={}, layer="silver")
    assert res == {"loaded": ["silver_dec"], "missingAfterLoad": []}
    delta = DeltaTable(str(tmp_path / "Tables" / "silver_dec"))
    assert {f["name"]: f["type"] for f in json.loads(delta.schema().to_json())["fields"]} == {"id": "string", "balance": "decimal(38,4)", "on": "date", "at": "timestamp", "paise": "long"}
    back = delta.to_pyarrow_table()
    assert back.schema.field("balance").type == pa.decimal128(38, 4) and back.column("balance").to_pylist() == tbl.column("balance").to_pylist()


def test_every_real_table_of_every_layer_survives_the_delta_fallback_with_its_schema(full_plan, ops, tmp_path):
    from deltalake import DeltaTable
    base = tmp_path / "Tables"
    for layer in LAYERS:
        res = ops.load_layer_delta(full_plan.folder(layer), "ws", "lh", only=full_plan.names(layer), uri_base=str(base), storage_options={}, layer=layer)
        assert res["loaded"] == full_plan.names(layer)
    decimals = 0
    for layer in LAYERS:
        for t in full_plan.tables[layer]:
            delta = DeltaTable(str(base / t.name))
            original = pq.read_table(t.file)
            assert {f["name"]: f["type"] for f in json.loads(delta.schema().to_json())["fields"]} == {f.name: delta_type(f.type) for f in original.schema}, t.name
            back = delta.to_pyarrow_table()
            assert back.num_rows == t.rows, t.name
            for f in original.schema:
                if pa.types.is_decimal(f.type):
                    decimals += 1
                    assert back.schema.field(f.name).type == f.type and back.column(f.name).to_pylist() == original.column(f.name).to_pylist(), (t.name, f.name)
    assert decimals > 0


def test_the_local_disk_hook_reaches_the_delta_writer_of_every_layer(mock, ops, lakehouse, tmp_path, monkeypatch):
    from deltalake import DeltaTable
    ws, lh = lakehouse
    plan = load_plan(make_out(tmp_path / "out"))
    monkeypatch.setattr(ops, "wait_registered", lambda *a, **k: [])                               # the lakehouse registers delta-written tables by itself (not simulated here)
    res = ops.load_layers(plan, ws, lh, loader="delta", uri_base=str(tmp_path / "Tables"), storage_options={})
    for layer in LAYERS:
        assert res[layer]["loaded"] == plan.names(layer)
        for t in plan.tables[layer]:
            assert DeltaTable(str(tmp_path / "Tables" / t.name)).to_pyarrow_table().num_rows == t.rows
    assert not mock.table_loads


def test_the_layer_report_flags_missing_tables_and_files_by_layer(mock, ops, lakehouse, tmp_path):
    ws, lh = lakehouse
    plan = load_plan(make_out(tmp_path))
    assert ops.list_files(ws, lh) == {}                                                           # no bronze folder yet
    ops.load_layers(plan, ws, lh, workers=1)
    rep = ops.layer_report(plan, ws, lh)
    assert rep["tables"] == 8 and rep["other"] == [] and all(not rep["layers"][layer]["missing"] for layer in LAYERS)
    assert rep["layers"]["bronze"]["files"] == {"expected": 3, "present": 3} and fabric_ops.describe_layer(rep["layers"]["gold"]) == "4/4 tables registered"
    mock.tables[lh].remove("silver_t1")
    del mock.files[f"{ws}/{lh}/{plan.files[0].target}"]
    mock.tables[lh].append("dim_from_another_build")
    rep = ops.layer_report(plan, ws, lh)
    assert rep["layers"]["silver"]["missing"] == ["silver_t1"] and not rep["layers"]["gold"]["missing"] and rep["other"] == ["dim_from_another_build"]
    assert fabric_ops.describe_layer(rep["layers"]["silver"]) == "2/3 tables registered; MISSING silver_t1"
    assert fabric_ops.describe_layer(rep["layers"]["bronze"]) == "1/1 tables registered, 2/3 files in OneLake"
    mock.dfs_list_status = 403                                                                    # OneLake would not list the folder: reported, not guessed
    rep = ops.layer_report(plan, ws, lh)
    assert rep["layers"]["bronze"]["files"]["present"] is None and "3 files planned (OneLake did not list them)" in fabric_ops.describe_layer(rep["layers"]["bronze"])
    assert fabric_ops.describe_layer({"present": 0, "expected": 9, "missing": [f"t{i}" for i in range(9)]}).endswith("MISSING t0, t1, t2, t3, t4 and 4 more")


# ================================================================================================ P3/P6: which layers load again, and when
def run_stage(ops, lakehouse, plan, state, *, reload=False, loader="load-api", logs=None):
    ws, lh = lakehouse
    logs = [] if logs is None else logs
    runner.load_stage(ops, plan, {"workspaceId": ws}, {"lakehouseId": lh, "sqlDatabase": None}, state, "Files/staging", loader, runner.Pipeline(reload=reload), logs.append)
    return "\n".join(logs)


def layers_of(plan, names) -> set[str]:
    return {layer for layer in LAYERS if set(plan.names(layer)) & set(names)}


def test_the_first_load_records_every_layer_and_an_unchanged_rerun_loads_nothing_until_reload(mock, ops, lakehouse, tmp_path):
    plan = load_plan(make_out(tmp_path / "out"))
    state = tmp_path / "state"
    text = run_stage(ops, lakehouse, plan, state)
    recorded = cli.read_state(state)
    assert recorded["loadedLayers"] == plan.fingerprints() and recorded["loadedFingerprint"] == plan.fingerprint()
    assert sorted(loaded(mock)) == sorted(plan.all_names()) and sorted(f"/dfs/{lakehouse[0]}/{lakehouse[1]}/{f.target}" for f in plan.files) == bronze_uploads(mock)
    assert "load bronze: 3 seed files and 1 table (not loaded by this build yet)" in text and "load silver: 3 tables (not loaded by this build yet)" in text
    assert re.search(r"  bronze: 1 table registered, 3 seed files uploaded \(3 read back and checked\) in \dm\d\ds\n", text + "\n")      # the line docs/LIVE_CHECKLIST.md item 3 describes
    assert "load done: 8 tables and 3 seed files in the lakehouse (3 bronze files, 3 silver tables, 4 gold tables)" in text
    n, uploads = len(mock.table_loads), bronze_uploads(mock)
    again = run_stage(ops, lakehouse, plan, state)
    assert len(mock.table_loads) == n and bronze_uploads(mock) == uploads and "already holds these 8 tables and 3 bronze files (same data as the last load); skipped" in again
    forced = run_stage(ops, lakehouse, plan, state, reload=True)
    assert len(mock.table_loads) == 2 * n and len(bronze_uploads(mock)) == 2 * len(uploads) and forced.count("(--reload)") == 3


def test_a_changed_layer_is_loaded_again_together_with_every_layer_built_from_it(mock, ops, lakehouse, tmp_path):
    base = load_plan(make_out(tmp_path / "base"))
    state = tmp_path / "state"
    run_stage(ops, lakehouse, base, state)
    for changed, expect in (("gold", {"gold"}), ("silver", {"silver", "gold"}), ("bronze", {"bronze", "silver", "gold"})):
        before, uploads = len(mock.table_loads), len(bronze_uploads(mock))
        plan = load_plan(make_out(tmp_path / f"v_{changed}", versions={changed: 2}))
        text = run_stage(ops, lakehouse, plan, state)
        assert layers_of(plan, loaded(mock)[before:]) == expect, changed
        assert (len(bronze_uploads(mock)) > uploads) == ("bronze" in expect)
        assert all(f"load {layer}: " in text for layer in LAYERS) and ("unchanged since its last load" in text) == (expect != set(LAYERS))
        assert cli.read_state(state)["loadedLayers"] == plan.fingerprints()
        run_stage(ops, lakehouse, base, state)                                                    # back to the base data: the next round starts from it


def test_a_table_that_is_missing_from_the_lakehouse_makes_its_layer_load_again_and_only_that_one(mock, ops, lakehouse, tmp_path):
    plan = load_plan(make_out(tmp_path / "out"))
    state = tmp_path / "state"
    run_stage(ops, lakehouse, plan, state)
    for layer, victim in (("silver", "silver_t1"), ("gold", plan.names("gold")[0]), ("bronze", "bronze_file_manifest")):
        mock.tables[lakehouse[1]].remove(victim)
        before, uploads = len(mock.table_loads), len(bronze_uploads(mock))
        text = run_stage(ops, lakehouse, plan, state)
        assert layers_of(plan, loaded(mock)[before:]) == {layer}, layer
        assert f"1 of its tables are missing from the lakehouse: {victim}" in text and (len(bronze_uploads(mock)) > uploads) == (layer == "bronze")


def test_a_failure_in_silver_keeps_bronze_and_never_starts_gold_and_the_rerun_resumes(mock, ops, lakehouse, tmp_path, monkeypatch):
    plan = load_plan(make_out(tmp_path / "out"))
    state = tmp_path / "state"
    logs: list[str] = []
    mock.load_failures = {"silver_t1": 99}
    with monkeypatch.context() as mp, pytest.raises(fabric_ops.LayerLoadError):
        mp.setitem(sys.modules, "deltalake", None)                                                # no fallback: the failure is final
        run_stage(ops, lakehouse, plan, state, logs=logs)
    text = "\n".join(logs)
    assert "ERROR the silver layer stopped at table silver_t1" in text and "gold was not started" in text and "fix the cause and run build.sh again" in text
    assert not set(loaded(mock)) & set(plan.names("gold"))                                        # silver before gold: not one gold table was loaded
    recorded = cli.read_state(state)
    assert list(recorded["loadedLayers"]) == ["bronze"] and recorded["loadedLayers"]["bronze"] == plan.fingerprints()["bronze"] and recorded["loadedFingerprint"] == ""
    mock.load_failures = {}
    before, uploads = len(mock.table_loads), bronze_uploads(mock)
    text = run_stage(ops, lakehouse, plan, state)
    assert layers_of(plan, loaded(mock)[before:]) == {"silver", "gold"} and bronze_uploads(mock) == uploads        # bronze was not done again
    assert "load bronze: unchanged since its last load" in text and cli.read_state(state)["loadedLayers"] == plan.fingerprints()


def test_a_crash_in_the_middle_of_a_layer_unrecords_it_so_a_half_loaded_layer_is_never_taken_for_a_finished_one(mock, ops, lakehouse, tmp_path, monkeypatch):
    base = load_plan(make_out(tmp_path / "base"))
    state = tmp_path / "state"
    run_stage(ops, lakehouse, base, state)
    newer = load_plan(make_out(tmp_path / "newer", versions={"silver": 2}))
    mock.load_failures = {"silver_t2": 99}
    with monkeypatch.context() as mp, pytest.raises(fabric_ops.LayerLoadError):
        mp.setitem(sys.modules, "deltalake", None)
        run_stage(ops, lakehouse, newer, state)
    assert "silver" not in cli.read_state(state)["loadedLayers"] and cli.read_state(state)["loadedLayers"]["gold"] == base.fingerprints()["gold"]
    mock.load_failures = {}
    before = len(mock.table_loads)
    text = run_stage(ops, lakehouse, base, state)                                                 # even the OLD data again reloads the half-written silver ...
    assert layers_of(base, loaded(mock)[before:]) == {"silver"} and "load gold: unchanged since its last load" in text      # ... gold never started, so it still holds the old data


def test_loading_a_smaller_or_a_larger_set_loads_every_layer_whose_tables_changed_and_never_deletes_tables(mock, ops, lakehouse, tmp_path):
    big = load_plan(make_out(tmp_path / "big"))
    small = load_plan(make_out(tmp_path / "small", files=2, silver_tables=2, gold_tables=2))
    state = tmp_path / "state"
    run_stage(ops, lakehouse, big, state)
    before = len(mock.table_loads)
    run_stage(ops, lakehouse, small, state)                                                       # a smaller tier: the layers are reloaded with fewer tables
    assert sorted(loaded(mock)[before:]) == sorted(small.all_names()) and set(big.all_names()) <= set(mock.tables[lakehouse[1]])
    before = len(mock.table_loads)
    run_stage(ops, lakehouse, big, state)
    assert sorted(loaded(mock)[before:]) == sorted(big.all_names())


def test_the_delta_loader_is_used_for_every_layer_by_the_build_stage_when_asked_for(mock, ops, lakehouse, tmp_path, monkeypatch):
    plan = load_plan(make_out(tmp_path / "out"))
    calls = []

    def fake_delta(self, layer_dir, ws_id, lh_id, only=None, uri_base=None, storage_options=None, layer=None):
        calls.append(layer)
        mock.tables.setdefault(lh_id, []).extend(only)
        return {"loaded": sorted(only), "missingAfterLoad": []}
    monkeypatch.setattr(fabric_ops.Ops, "load_layer_delta", fake_delta)
    run_stage(ops, lakehouse, plan, tmp_path / "state", loader="delta")
    assert calls == list(LAYERS) and not mock.table_loads and cli.read_state(tmp_path / "state")["loadedLayers"] == plan.fingerprints()


# ================================================================================================ review fixes: failures that say nothing about the tables, waiting, Ctrl+C
@pytest.fixture()
def timed(mock):
    """An Ops whose time is virtual: every sleep is recorded instead of slept and moves a clock the retry deadlines read (`o.time`), and the jitter is 0 (the
    shortest wait of each step)."""
    vt, logs = VirtualTime(), []
    fab = oac.Fabric(lambda s: TOKENS[s], api_base=f"{mock.base}/v1", pbi_base=f"{mock.base}/v1.0/myorg", log=logs.append, sleeper=vt.sleep, rng=lambda: 0.0, clock=vt.now)
    o = fabric_ops.Ops(fab, lambda s: TOKENS[s], logs.append, sleeper=vt.sleep, dfs_base=f"{mock.base}/dfs", rng=lambda: 0.0)
    o.logs, o.slept, o.time = logs, vt.slept, vt
    return o


@pytest.fixture()
def timed_lakehouse(timed):
    ws = timed.ensure_workspace("w", "contosofabricf4")["workspaceId"]
    return ws, timed.ensure_lakehouse(ws, "L")["lakehouseId"]


def posts_to(mock, table: str) -> int:
    return len([r for r in mock.requests if r["method"] == "POST" and r["path"].endswith(f"/tables/{table}/load")])


def test_a_failed_table_listing_after_a_complete_api_load_is_retried_and_is_never_a_reason_for_delta_rs(mock, timed, timed_lakehouse, tmp_path, monkeypatch):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path))
    forbid_delta(monkeypatch)
    mock.faults = [{"match": r"/tables$", "method": "GET", "status": 503, "count": 8}]            # more than the 6 tries of one call
    res = timed.load_layers(plan, ws, lh, workers=1)
    assert mock.fault_hits == 8 and [res[layer]["loaded"] for layer in LAYERS] == [plan.names(layer) for layer in LAYERS]
    assert loaded(mock) == plan.all_names()                                                       # every table was loaded once: the API worked, only the listing failed
    text = "\n".join(timed.logs)
    assert "the lakehouse's table list could not be read" in text and "falling back" not in text and "refused" not in text


def test_a_table_listing_that_never_recovers_is_a_transient_stop_not_missing_tables(mock, timed, timed_lakehouse, tmp_path, monkeypatch):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path))
    forbid_delta(monkeypatch)
    mock.faults = [{"match": r"/tables$", "method": "GET", "status": 503, "count": 10**6}]
    timed.slept.clear()
    with pytest.raises(fabric_ops.LayerLoadError) as err:
        timed.load_layers(plan, ws, lh, workers=1)
    ex = err.value
    assert (ex.layer, ex.table, ex.transient, ex.rejected, ex.remaining) == ("bronze", None, True, [], []) and ex.loaded == plan.names("bronze")
    assert "could not be read for 180 s" in str(ex) and "503" in str(ex) and "nothing is known to be missing" in str(ex)
    assert sum(timed.slept) >= fabric_ops.REGISTER_RETRY_S and loaded(mock) == plan.names("bronze")      # it waited the three minutes; silver never started


@pytest.mark.parametrize("fault, transient", [
    ({"match": r"/tables/silver_t1/load$", "method": "POST", "status": 429, "retry_after": 1, "count": 10**6}, True),
    ({"match": r"/tables/silver_t1/load$", "method": "POST", "status": 503, "count": 10**6}, True),
    ({"match": r"/dfs/.*/Files/staging/silver_t1\.parquet", "status": 503, "count": 10**6}, True),               # OneLake throttles the upload of the file
    ({"match": r"/tables/silver_t1/load$", "method": "POST", "status": 403, "count": 10**6}, False),             # not allowed: no other loader changes that
    ({"match": r"/tables/silver_t1/load$", "method": "POST", "status": 404, "count": 10**6}, False)], ids=["429", "503", "onelake-503", "403", "404"])
def test_throttling_a_busy_service_and_a_refusal_to_sign_in_are_never_a_reason_for_delta_rs(mock, timed, timed_lakehouse, tmp_path, monkeypatch, fault, transient):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path))
    forbid_delta(monkeypatch)
    mock.faults = [fault]
    with pytest.raises(fabric_ops.LayerLoadError) as err:
        timed.load_layers(plan, ws, lh, workers=1)
    ex = err.value
    assert (ex.layer, ex.table, ex.rejected, ex.transient) == ("silver", "silver_t1", [], transient)
    assert ex.loaded == ["silver_t0"] and ex.remaining == ["silver_t1", "silver_t2"] and set(loaded(mock)) == {*plan.names("bronze"), "silver_t0"}      # gold never started


def test_a_dropped_connection_is_tried_again_and_then_stops_the_layer_without_delta_rs(mock, timed, timed_lakehouse, tmp_path, monkeypatch):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path))
    forbid_delta(monkeypatch)
    real = timed.load_table

    def flaky(ws_id, lh_id, table, rel_path):
        if table == "silver_t1":
            raise requests.ConnectionError("connection reset by peer")
        return real(ws_id, lh_id, table, rel_path)
    monkeypatch.setattr(timed, "load_table", flaky)
    with pytest.raises(fabric_ops.LayerLoadError) as err:
        timed.load_layers(plan, ws, lh, workers=1)
    ex = err.value
    assert ex.transient and ex.rejected == [] and ex.table == "silver_t1" and "connection reset by peer" in str(ex)
    assert len([m for m in timed.logs if m.startswith("  silver_t1:") and "trying again in" in m]) == fabric_ops.SERIAL_TRIES - 1


def test_a_refused_table_and_a_throttled_one_in_the_same_layer_raise_without_any_fallback(mock, timed, timed_lakehouse, tmp_path, monkeypatch):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path))
    forbid_delta(monkeypatch)
    mock.load_rejections = {"silver_t0": "400"}
    mock.faults = [{"match": r"/tables/silver_t2/load$", "method": "POST", "status": 429, "retry_after": 1, "count": 10**6}]
    with pytest.raises(fabric_ops.LayerLoadError) as err:
        timed.load_layers(plan, ws, lh, workers=1)
    ex = err.value
    assert ex.rejected == ["silver_t0"] and ex.remaining == ["silver_t0", "silver_t2"] and ex.transient and ex.loaded == ["silver_t1"]
    assert ex.table == "silver_t2" and "429" in str(ex)                                           # the failure that ended the run is the one named, not the refusal before it


def test_a_429_with_retry_after_is_waited_out_before_every_try_and_without_it_the_waits_grow_exponentially(mock, timed, timed_lakehouse, tmp_path):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path))
    timed.slept.clear()
    mock.faults = [{"match": r"/tables/silver_t0/load$", "method": "POST", "status": 429, "retry_after": 7, "count": 8}]
    res = timed.load_layers(plan, ws, lh, workers=1)
    assert res["silver"]["loaded"] == plan.names("silver") and mock.fault_hits == 8
    assert [s for s in timed.slept if s >= 7] == [7.0] * 8                                        # 5 waits inside the first call, 1 before the layer's own retry, 2 after it
    timed.logs.clear()
    mock.faults = [{"match": r"/tables/silver_t1/load$", "method": "POST", "status": 503, "count": 3}]
    timed.load_layers(plan, ws, lh, workers=1)
    assert re.findall(r"503 from POST \S+/silver_t1/load; retrying in (\d+)s", "\n".join(timed.logs)) == ["1", "2", "4"]            # 1-2 s, 2-4 s, 4-8 s: the shortest of each with no jitter


def test_a_conflict_is_tried_again_after_growing_waits_and_a_refusal_gets_one_more_try_only(mock, timed, timed_lakehouse, tmp_path, monkeypatch):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path))
    mock.load_failures = {"silver_t1": 3}                                                         # 409: another operation is running on the lakehouse, three times
    timed.load_layers(plan, ws, lh, workers=1)
    assert posts_to(mock, "silver_t1") == 4 and re.findall(r"silver_t1: .*; trying again in (\d+)s", "\n".join(timed.logs)) == ["1", "2", "4"]
    mock.load_rejections = {"silver_t0": "400"}
    written = []

    def fake_delta(self, layer_dir, ws_id, lh_id, only=None, uri_base=None, storage_options=None, layer=None):
        written.append(list(only))
        return {"loaded": sorted(only), "missingAfterLoad": []}
    monkeypatch.setattr(fabric_ops.Ops, "load_layer_delta", fake_delta)
    monkeypatch.setattr(timed, "wait_registered", lambda *a, **k: [])
    before = posts_to(mock, "silver_t0")
    timed.load_layers(plan, ws, lh, workers=1, skip=lambda layer: layer != "silver")
    assert posts_to(mock, "silver_t0") - before == 2 and written == [["silver_t0"]]               # the first try and exactly one more, then delta-rs


def test_a_throttled_onelake_request_is_tried_again_after_the_wait_it_asks_for_and_a_refusal_is_not(mock, timed, timed_lakehouse):
    ws, lh = timed_lakehouse
    timed.slept.clear()
    mock.faults = [{"match": r"/dfs/.*/Files/x\.json", "method": "PUT", "status": 429, "retry_after": 3, "count": 2}]
    timed.upload(ws, lh, "Files/x.json", b"{}")
    assert timed.slept == [3.0, 3.0] and mock.files[f"{ws}/{lh}/Files/x.json"] == b"{}" and any("OneLake create Files/x.json: 429; trying again in 3s" in m for m in timed.logs)
    hits = mock.fault_hits
    mock.faults = [{"match": r"/dfs/.*/Files/y\.json", "method": "PUT", "status": 403, "count": 5}]
    with pytest.raises(oac.FabricError, match="OneLake create Files/y.json: 403") as err:
        timed.upload(ws, lh, "Files/y.json", b"{}")
    assert err.value.status == 403 and mock.fault_hits == hits + 1                                # a refusal is final at once


def test_a_bronze_upload_that_stays_throttled_stops_the_bronze_layer_as_a_transient_failure(mock, timed, timed_lakehouse, tmp_path, monkeypatch):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path))
    forbid_delta(monkeypatch)
    mock.faults = [{"match": r"/dfs/.*/Files/bronze/", "method": "PUT", "status": 503, "count": 10**6}]
    with pytest.raises(fabric_ops.LayerLoadError) as err:
        timed.load_layers(plan, ws, lh, workers=1)
    ex = err.value
    assert (ex.layer, ex.table, ex.transient) == ("bronze", None, True) and "bronze file data/" in str(ex) and "503" in str(ex) and not mock.table_loads


def test_ctrl_c_stops_the_tasks_that_are_queued_and_lets_the_running_ones_finish(timed):
    started: list[int] = []
    second_running, release, lock = threading.Event(), threading.Event(), threading.Lock()

    def task(i: int) -> int:
        with lock:
            started.append(i)
        if i == 0:
            second_running.wait(5)
            raise KeyboardInterrupt                                                               # what Ctrl+C does: future.result() raises it again in the main thread
        if i == 1:
            second_running.set()
        release.wait(5)
        return i

    threading.Timer(1.0, release.set).start()
    with pytest.raises(KeyboardInterrupt):
        timed._run(list(range(12)), task, 2, key=str)
    release.set()
    time.sleep(0.5)                                                                               # long enough for a queued task to start, were it going to
    assert 2 <= len(started) <= 3 and {0, 1} <= set(started)                                      # the two that were running (and at most the next one), never all twelve
    assert any(re.fullmatch(r"  interrupted: waiting for [12] running loads to finish \(up to 15 min\); queued loads were cancelled", m) for m in timed.logs)


def test_ctrl_c_during_a_layer_load_leaves_the_queued_tables_unloaded(mock, timed, timed_lakehouse, tmp_path, monkeypatch):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path, gold_tables=12))
    mock.load_delay = 0.2
    real, calls = timed.load_table, []

    def interrupted(ws_id, lh_id, table, rel_path):
        calls.append(table)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return real(ws_id, lh_id, table, rel_path)
    monkeypatch.setattr(timed, "load_table", interrupted)
    with pytest.raises(KeyboardInterrupt):
        timed.load_layer(plan.folder("gold"), ws, lh, only=plan.names("gold"), workers=2, layer="gold")
    time.sleep(0.8)
    assert len(calls) <= 3 and len(loaded(mock)) <= 2 < len(plan.names("gold"))


def test_a_sql_endpoint_refresh_that_times_out_is_logged_and_never_stops_the_load(mock, timed, timed_lakehouse, tmp_path, monkeypatch):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path))
    real = timed.fab.call

    def call(method, url, *a, **k):
        if url.endswith("/refreshMetadata"):
            raise requests.ReadTimeout("HTTPSConnectionPool: Read timed out. (read timeout=120)")      # a requests exception, not a FabricError
        return real(method, url, *a, **k)
    monkeypatch.setattr(timed.fab, "call", call)
    res = timed.load_layers(plan, ws, lh, workers=1, sql_endpoint_id="sql-1")
    assert all(res[layer]["missingAfterLoad"] == [] for layer in LAYERS) and "sqlSync" not in res["gold"]
    assert len([m for m in timed.logs if "metadata refresh skipped: HTTPSConnectionPool" in m]) == 3                        # once per layer; every time the load went on
    assert timed.sync_sql_endpoint(ws, "sql-1") is False and timed.sync_sql_endpoint(ws, None) is False


# ================================================================================================ round 3: one budget per table, a breaker in the parallel round, refusals only
def test_the_parallel_round_stops_after_workers_failures_in_a_row_that_are_not_about_the_tables(mock, timed, timed_lakehouse, tmp_path, monkeypatch):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path, gold_tables=16))
    forbid_delta(monkeypatch)
    mock.faults = [{"match": r"/tables/[^/]+/load$", "method": "POST", "status": 429, "retry_after": 30, "count": 10**6, "delay": 0.03}]
    with pytest.raises(fabric_ops.LayerLoadError) as err:
        timed.load_layer(plan.folder("gold"), ws, lh, only=plan.names("gold"), workers=4, layer="gold")
    ex = err.value
    tried = {r["path"].split("/")[-2] for r in mock.requests if r["method"] == "POST" and r["path"].endswith("/load")}
    assert ex.transient and ex.rejected == [] and ex.loaded == [] and sorted(ex.remaining) == sorted(plan.names("gold"))          # every table is accounted for ...
    assert 4 <= len(tried) <= 12 and len(tried) < len(plan.names("gold")) and ex.table in tried                                    # ... but only the first waves were tried
    assert mock.fault_hits <= 6 * 12                                                              # one ladder of 6 calls per table that ran, no serial rounds on top (480+ without the breaker)
    assert "loads in a row failed for a reason that is not about the tables" in "\n".join(timed.logs)


def test_conflicts_and_refusals_do_not_trip_the_breaker_and_a_success_resets_it(mock, timed, timed_lakehouse, tmp_path):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path, gold_tables=8))
    mock.load_failures = {name: 1 for name in plan.names("gold")}                                 # every table meets a conflict once (409): a busy lakehouse, not a broken service
    res = timed.load_layer(plan.folder("gold"), ws, lh, only=plan.names("gold"), workers=4, layer="gold")
    assert res["loaded"] == sorted(plan.names("gold")) and "in a row failed" not in "\n".join(timed.logs)


def test_a_table_that_keeps_failing_runs_out_of_its_retry_budget_and_its_failure_is_final(mock, timed, timed_lakehouse, tmp_path, monkeypatch):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path))
    forbid_delta(monkeypatch)
    assert timed.retry_budget_s == fabric_ops.RETRY_BUDGET_S == 360.0                             # six minutes by default
    timed.retry_budget_s = 60.0
    mock.faults = [{"match": r"/tables/silver_t1/load$", "method": "POST", "status": 429, "retry_after": 30, "count": 10**6}]
    before = timed.time.now_s
    with pytest.raises(fabric_ops.LayerLoadError) as err:
        timed.load_layers(plan, ws, lh, workers=1)
    assert (err.value.table, err.value.transient, err.value.loaded) == ("silver_t1", True, ["silver_t0"])
    assert posts_to(mock, "silver_t1") == 3 and timed.time.now_s - before <= 70.0                 # waits of 30 s at 0 s and at 30 s; none at 60 s, it would end past the budget


def test_the_retry_budget_covers_the_whole_ladder_of_a_table_in_the_parallel_round_too(mock, timed, timed_lakehouse, tmp_path, monkeypatch):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path))
    forbid_delta(monkeypatch)
    timed.retry_budget_s = 45.0
    mock.faults = [{"match": r"/tables/silver_t1/load$", "method": "POST", "status": 503, "retry_after": 20, "count": 10**6}]
    before = timed.time.now_s
    with pytest.raises(fabric_ops.LayerLoadError) as err:
        timed.load_layer(plan.folder("silver"), ws, lh, only=plan.names("silver"), workers=4, layer="silver")                    # three tables, so no breaker: the budget is what ends it
    assert err.value.table == "silver_t1" and err.value.loaded == ["silver_t0", "silver_t2"] and err.value.transient
    assert posts_to(mock, "silver_t1") == 3 and timed.time.now_s - before <= 60.0


@pytest.mark.parametrize("mode, delta", [
    ("400", True), ("failed", True), ("failed:InvalidInput", True), ("failed:UnsupportedDataType", True),
    ("cancelled", False), ("failed:InternalError", False), ("failed:ServiceUnavailable", False), ("failed:RequestTimeout", False), ("failed:CapacityLimitExceeded", False),
    ("failed:TooManyRequests", False), ("failed:OperationCanceled", False), ("failed:SparkJobThrottled", False), ("failed-nocode", False)])
def test_delta_rs_is_for_a_refused_load_call_and_for_nothing_else(mock, timed, timed_lakehouse, tmp_path, monkeypatch, mode, delta):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path))
    mock.load_rejections = {"silver_t1": mode}
    written = []

    def fake_delta(self, layer_dir, ws_id, lh_id, only=None, uri_base=None, storage_options=None, layer=None):
        written.append(list(only))
        mock.tables.setdefault(lh_id, []).extend(n for n in only if n not in mock.tables.get(lh_id, []))
        return {"loaded": sorted(only), "missingAfterLoad": []}
    monkeypatch.setattr(fabric_ops.Ops, "load_layer_delta", fake_delta)
    if delta:                                                                                     # the API said no to this file: an HTTP 400, or a failed operation with an errorCode about the file
        assert timed.load_layers(plan, ws, lh, workers=1)["silver"]["loaded"] == plan.names("silver") and written == [["silver_t1"]]
        return
    with pytest.raises(fabric_ops.LayerLoadError) as err:                                         # a capacity, a service that is down or slow, a cancelled operation, an error without a code
        timed.load_layers(plan, ws, lh, workers=1)
    assert not written and err.value.rejected == [] and err.value.table == "silver_t1"


@pytest.mark.parametrize("status", [415, 422])
def test_an_http_415_or_422_on_the_load_request_is_a_refusal_too(mock, timed, timed_lakehouse, tmp_path, monkeypatch, status):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path))
    mock.faults = [{"match": r"/tables/silver_t1/load$", "method": "POST", "status": status, "count": 10**6}]
    written = []

    def fake_delta(self, layer_dir, ws_id, lh_id, only=None, uri_base=None, storage_options=None, layer=None):
        written.append(list(only))
        mock.tables.setdefault(lh_id, []).extend(only)
        return {"loaded": sorted(only), "missingAfterLoad": []}
    monkeypatch.setattr(fabric_ops.Ops, "load_layer_delta", fake_delta)
    timed.load_layers(plan, ws, lh, workers=1)
    assert written == [["silver_t1"]] and posts_to(mock, "silver_t1") == 2                        # the first try and one more, then delta-rs for that table only


@pytest.mark.parametrize("status", [400, 415, 422, 403, 404])
def test_a_failed_staging_upload_never_falls_back_and_the_error_names_the_file(mock, timed, timed_lakehouse, tmp_path, monkeypatch, status):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path))
    forbid_delta(monkeypatch)
    mock.faults = [{"match": r"/dfs/.*/Files/staging/silver_t1\.parquet", "status": status, "count": 10**6}]
    with pytest.raises(fabric_ops.LayerLoadError) as err:
        timed.load_layers(plan, ws, lh, workers=1)
    ex = err.value
    assert (ex.table, ex.rejected, ex.transient) == ("silver_t1", [], False)
    assert f"the staging upload of silver_t1.parquet to Files/staging/silver_t1.parquet failed: OneLake create Files/staging/silver_t1.parquet: {status}" in str(ex)
    assert posts_to(mock, "silver_t1") == 0                                                       # the Load Table call was never made


@pytest.mark.parametrize("retry_after", [None, 60])
def test_the_table_listing_window_is_one_deadline_that_includes_the_back_off_inside_each_call(mock, timed, timed_lakehouse, retry_after):
    ws, lh = timed_lakehouse
    mock.faults = [{"match": r"/tables$", "method": "GET", "status": 503, "count": 10**6, **({"retry_after": retry_after} if retry_after else {})}]
    start = timed.time.now_s
    with pytest.raises(oac.FabricError, match="503"):
        timed.registered_tables(ws, lh)
    assert 120.0 <= timed.time.now_s - start <= fabric_ops.REGISTER_RETRY_S                       # 180 s in all (it was 738 s with 503 and 1552 s with Retry-After 60: only its own waits were counted)


def test_a_listing_that_comes_back_inside_the_window_is_used(mock, timed, timed_lakehouse):
    ws, lh = timed_lakehouse
    mock.tables[lh] = ["t1", "t2"]
    mock.faults = [{"match": r"/tables$", "method": "GET", "status": 503, "retry_after": 60, "count": 3}]        # three waits of 60 s end exactly at the deadline: the fourth try is allowed
    start = timed.time.now_s
    assert timed.registered_tables(ws, lh) == {"t1", "t2"} and timed.time.now_s - start == 180.0
    mock.faults = [{"match": r"/tables$", "method": "GET", "status": 503, "count": 3}]
    start = timed.time.now_s
    assert timed.registered_tables(ws, lh) == {"t1", "t2"} and timed.time.now_s - start == 7.0               # 1 + 2 + 4 s


def test_for_a_layer_written_with_delta_rs_the_endpoint_refresh_comes_after_its_tables_are_listed(mock, timed, timed_lakehouse, tmp_path, monkeypatch):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path))
    pending: list[tuple[float, list[str]]] = []                                                   # delta-rs tables become visible to the lakehouse listing 30 s after they are written

    def fake_delta(self, layer_dir, ws_id, lh_id, only=None, uri_base=None, storage_options=None, layer=None):
        pending.append((timed.time.now_s + 30.0, list(only)))
        return {"loaded": sorted(only), "missingAfterLoad": []}
    monkeypatch.setattr(fabric_ops.Ops, "load_layer_delta", fake_delta)
    first_seen: dict[str, int] = {}
    real_list = timed.list_tables

    def listing(ws_id, lh_id):
        for due, names in list(pending):
            if timed.time.now_s >= due:
                mock.tables.setdefault(lh_id, []).extend(n for n in names if n not in mock.tables.get(lh_id, []))
                pending.remove((due, names))
        out = real_list(ws_id, lh_id)
        present = {t["name"] for t in out}
        for layer in LAYERS:
            if layer not in first_seen and set(plan.names(layer)) <= present:
                first_seen[layer] = len(mock.requests)                                            # the request after which the layer's tables are listed
        return out
    monkeypatch.setattr(timed, "list_tables", listing)
    timed.load_layers(plan, ws, lh, loader="delta", sql_endpoint_id="sql-1")
    refreshes = [i for i, r in enumerate(mock.requests) if r["path"].endswith("/refreshMetadata")]
    assert len(refreshes) == 3 and set(first_seen) == set(LAYERS)
    assert all(refreshes[i] >= first_seen[layer] for i, layer in enumerate(LAYERS))               # never before the layer's tables are in the listing


def test_one_read_timeout_in_an_operation_poll_no_longer_aborts_the_load(mock, timed, timed_lakehouse, tmp_path, monkeypatch):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path))
    real, hit = requests.request, []

    def flaky(method, url, **kw):
        if method == "GET" and "/v1/operations/" in url and not hit:
            hit.append(url)
            raise requests.ReadTimeout("Read timed out. (read timeout=120)")
        return real(method, url, **kw)
    monkeypatch.setattr(requests, "request", flaky)
    res = timed.load_layers(plan, ws, lh, workers=1)
    assert hit and all(res[layer]["missingAfterLoad"] == [] for layer in LAYERS)
    assert any("ReadTimeout from GET" in m and "Read timed out. (read timeout=120)" in m for m in timed.logs)


def sync_polls(mock) -> list[int]:
    """How often each refresh operation (the ones the mock keeps running for a while) was asked about, in the order they were started."""
    return [len([r for r in mock.requests if r["method"] == "GET" and r["path"].endswith(f"/operations/{oid}")]) for oid, op in mock.ops.items() if "running" in op]


def test_only_the_refresh_after_the_last_layer_is_followed_until_it_finishes(mock, timed, timed_lakehouse, tmp_path):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path))
    mock.sql_sync_polls = 3                                                                       # the refresh runs for three polls
    res = timed.load_layers(plan, ws, lh, workers=1, sql_endpoint_id="sql-1")
    assert res["gold"]["sqlSync"] == "synced" and "sqlSync" not in res["bronze"] and "sqlSync" not in res["silver"]
    assert sync_polls(mock) == [0, 0, 4]                                                          # one refresh per layer, only the last one asked about: three Running, then Succeeded
    text = "\n".join(timed.logs)
    assert f"the refresh finished after 0m30s; it reports {len(plan.all_names())} tables" in text and "does not list" not in text
    res = timed.load_layers(plan, ws, lh, workers=1, sql_endpoint_id="sql-1", skip=lambda layer: layer == "gold")        # gold left alone: silver is the last layer that loads
    assert res["silver"]["sqlSync"] == "synced" and "sqlSync" not in res["bronze"] and res["gold"] == {"skipped": True} and sync_polls(mock)[3:] == [0, 4]
    mock.requests.clear()
    mock.ops.clear()
    timed.load_layers(plan, ws, lh, workers=1, sql_endpoint_id="sql-1", sync_wait_s=0)           # not awaited when sync_wait_s is 0
    assert sync_polls(mock) == [0, 0, 0] and len([r for r in mock.requests if r["path"].endswith("/refreshMetadata")]) == 3


def test_a_refresh_that_runs_long_reports_progress_each_minute_then_stops_waiting_and_carries_on(mock, timed, timed_lakehouse, tmp_path):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path))
    mock.sql_sync_polls = 13                                                                      # 13 polls of 10 s: a little over two minutes
    res = timed.load_layers(plan, ws, lh, workers=1, sql_endpoint_id="sql-1")
    text = "\n".join(timed.logs)
    assert res["gold"]["sqlSync"] == "synced" and "the refresh is still running after 1m00s" in text and "the refresh is still running after 2m00s" in text
    assert "3m00s" not in text
    mock.sql_sync_polls = 10**6                                                                   # never finishes
    timed.logs.clear()
    res = timed.load_layers(plan, ws, lh, workers=1, sql_endpoint_id="sql-1", sync_wait_s=25)
    assert res["gold"]["sqlSync"] == "timeout" and all(res[layer]["missingAfterLoad"] == [] for layer in LAYERS)
    assert "still running after 0m30s; carrying on" in "\n".join(timed.logs) and "wait a few minutes and run build.sh again" in "\n".join(timed.logs)


def test_a_refresh_that_fails_or_cannot_be_followed_is_reported_and_never_fails_the_load(mock, timed, timed_lakehouse, tmp_path):
    ws, lh = timed_lakehouse
    plan = load_plan(make_out(tmp_path))
    mock.sql_sync_polls, mock.sql_sync_status = 1, "Failed"
    res = timed.load_layers(plan, ws, lh, workers=1, sql_endpoint_id="sql-1")
    assert res["gold"]["sqlSync"] == "failed" and "the refresh ended Failed" in "\n".join(timed.logs)

    class Reply:                                                                                  # what Fabric answered to the refresh request
        status_code, headers = 202, {"Location": f"{mock.base}/v1/operations/op-x", "Retry-After": "0"}
    mock.faults = [{"match": r"/v1/operations/op-x$", "method": "GET", "status": 503, "count": 10**6}]
    assert timed.await_sync(Reply, 60) == "unknown" and any("could not follow the refresh" in m for m in timed.logs)
    Reply.status_code = 200
    assert timed.await_sync(Reply, 60) == "synced"                                                # an answer that is already the result


def test_a_refresh_reply_that_names_the_operation_by_id_only_is_followed_too(mock, timed):
    mock.ops["op-by-id"] = {"status": "Succeeded", "running": 2, "result": {"value": [{"tableName": "a", "status": "Success"}]}}

    class Reply:
        status_code, headers = 202, {"x-ms-operation-id": "op-by-id", "Retry-After": "0"}
    assert timed.await_sync(Reply, 60, expect=["a"]) == "synced"
    assert len([r for r in mock.requests if r["path"].endswith("/operations/op-by-id")]) == 3     # two Running, then Succeeded
    assert "the refresh finished after 0m20s; it reports 1 tables" in "\n".join(timed.logs)


def test_the_sync_result_names_failed_tables_and_planned_tables_the_endpoint_does_not_list_yet(timed, monkeypatch):
    rows = {"value": [{"tableName": "a", "status": "Success"}, {"tableName": "b", "status": "Failure"}, {"tableName": "c", "status": "Success"}]}
    monkeypatch.setattr(timed.fab, "call", lambda method, url, *a, **k: type("Reply", (), {"status_code": 200})())
    monkeypatch.setattr(timed.fab, "_json", lambda resp: rows)
    timed._sync_result("http://x/v1/operations/o", ["a", "b", "c", "d", "e"], 95.0)
    assert "the refresh finished after 1m35s; it reports 3 tables, 1 failed (b), and does not list 2 of the planned tables yet (d, e)" in "\n".join(timed.logs)
    timed.logs.clear()
    rows = {"unexpected": "shape"}
    timed._sync_result("http://x/v1/operations/o", ["a"], 5.0)
    assert timed.logs == ["  SQL endpoint sync: the refresh finished after 0m05s"]                # the preview API's result shape is not verified: nothing is guessed


def test_the_end_of_the_last_load_is_kept_in_the_state_and_an_unchanged_rerun_keeps_it(mock, ops, lakehouse, tmp_path):
    ws, lh = lakehouse
    plan = load_plan(make_out(tmp_path / "out"))
    state = tmp_path / "state"
    pipe = runner.Pipeline(wall=lambda: 1_800_000_000.0)
    args = (ops, plan, {"workspaceId": ws}, {"lakehouseId": lh, "sqlDatabase": None}, state, "Files/staging", "load-api", pipe, [].append)
    runner.load_stage(*args)
    assert cli.read_state(state)["lastLoadAt"] == datetime.datetime.fromtimestamp(1_800_000_000, datetime.timezone.utc).strftime(runner.STAMP_FORMAT)
    runner.load_stage(*args[:7], runner.Pipeline(wall=lambda: 1_800_005_000.0), [].append)       # nothing to load: the time of the load that did happen stays
    assert cli.read_state(state)["lastLoadAt"].startswith("2027-01-15T0")


def test_the_sql_sync_hint_is_only_for_an_ontology_error_soon_after_a_load(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    loaded_at = datetime.datetime(2026, 10, 17, 10, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
    cli.write_state(out, lastLoadAt="2026-10-17T10:00:00Z")
    refused = oac.FabricError("POST https://api.fabric.microsoft.com/v1/workspaces/w/ontologies -> 400: {\"errorCode\": \"InvalidDefinition\", \"message\": \"table not found\"}")
    hint = runner.sql_sync_hint(out, refused, loaded_at + 270)
    assert "loaded 4 min 30 s ago" in hint and "SQL analytics endpoint" in hint and "run bash build.sh again" in hint and "does not load the tables again" in hint
    assert "the semantic model reads OneLake directly" in hint                                    # the semantic model is not the one that lags
    assert runner.sql_sync_hint(out, refused, loaded_at + runner.SYNC_WINDOW_S + 1) == ""        # too long ago: the endpoint is not the explanation
    assert runner.sql_sync_hint(out, refused, loaded_at - 5) == ""
    assert runner.sql_sync_hint(out, oac.FabricError("POST https://x/v1/workspaces/w/semanticModels -> 400: bad"), loaded_at + 10) == ""
    assert runner.sql_sync_hint(out, oac.FabricError("operation Failed: {\"errorCode\": \"EntityNotFound\", \"message\": \"ontology item not found\"}"), loaded_at + 10)
    assert runner.sql_sync_hint(out, oac.FabricError("POST .../ontologies -> 403: {\"errorCode\": \"FeatureNotAvailable\"}"), loaded_at + 10) == ""     # the gate is not this
    assert runner.sql_sync_hint(out, ValueError("ontologies -> 400"), loaded_at + 10) == ""
    assert runner.sql_sync_hint(tmp_path / "no-state", refused, loaded_at + 10) == ""


def test_a_transient_stop_and_a_refusal_without_delta_rs_are_explained_differently_by_the_stage(mock, ops, lakehouse, tmp_path, monkeypatch):
    plan = load_plan(make_out(tmp_path / "out"))
    logs: list[str] = []
    mock.faults = [{"match": r"/tables/silver_t1/load$", "method": "POST", "status": 429, "retry_after": 1, "count": 10**6}]
    with pytest.raises(fabric_ops.LayerLoadError):
        run_stage(ops, lakehouse, plan, tmp_path / "s1", logs=logs)
    text = "\n".join(logs)
    assert "ERROR the silver layer stopped at table silver_t1" in text and "the cause is the moment (throttling" in text and "no table was written another way" in text
    mock.faults.clear()
    mock.load_rejections = {"silver_t1": "failed"}
    logs.clear()
    monkeypatch.setitem(sys.modules, "deltalake", None)
    with pytest.raises(fabric_ops.LayerLoadError):
        run_stage(ops, lakehouse, plan, tmp_path / "s2", logs=logs)
    text = "\n".join(logs)
    assert "ERROR the silver layer stopped at table silver_t1: the Load Table API refused it" in text and "delta-rs fallback is not installed" in text and "the cause is the moment" not in text
    mock.load_rejections.clear()
    logs.clear()
    started = len(mock.table_loads)
    mock.faults = [{"match": r"/tables$", "method": "GET", "status": 503, "count": 10**6}]
    with pytest.raises(oac.FabricError, match="503"):                                             # the stage's own first listing: the error is the listing's, not "missing tables"
        run_stage(ops, lakehouse, plan, tmp_path / "s3", logs=logs)
    assert len(mock.table_loads) == started and not logs                                          # nothing was loaded and nothing was said about tables


# ================================================================================================ P4: the load command
def run(*args):
    return cli.main([str(a) for a in args])


def test_load_is_a_dry_run_that_lists_the_layers_in_load_order(tmp_path, capsys):
    out = make_out(tmp_path / "out")
    assert run("load", "--out", out, "--state-dir", tmp_path / "s") == 0
    text = capsys.readouterr().out
    assert "3 bronze files, 3 silver tables, 4 gold tables" in text and "loaded in this order: bronze -> silver -> gold" in text and "DRY RUN" in text
    assert text.index("  bronze: 3 seed files, 1 tables") < text.index("  silver: 3 tables") < text.index("  gold: 4 tables")
    assert run("load", "--out", out, "--state-dir", tmp_path / "s", "--layers", "gold, silver") == 0
    text = capsys.readouterr().out
    assert "0 bronze files, 3 silver tables, 4 gold tables" in text and "loaded in this order: silver -> gold" in text           # load order, whatever the order asked for
    assert run("load", "--out", out, "--state-dir", tmp_path / "s", "--only", "silver_t1;dim_t0") == 0
    assert "0 bronze files, 1 silver tables, 1 gold tables" in capsys.readouterr().out


def test_load_refuses_a_bad_selection_and_a_folder_without_layers(tmp_path, capsys):
    out = make_out(tmp_path / "out")
    assert run("load", "--out", out, "--state-dir", tmp_path / "s", "--layers", "platinum") == 2
    assert run("load", "--out", out, "--state-dir", tmp_path / "s", "--only", "no_such_table") == 2
    assert "BAD SELECTION" in capsys.readouterr().out
    assert run("load", "--out", tmp_path / "empty", "--state-dir", tmp_path / "s") == 1
    assert "run `build` first" in capsys.readouterr().out
    assert run("load", "--execute", "--out", out, "--state-dir", tmp_path / "s") == 1                                          # no workspace in the state
    assert "provision --execute" in capsys.readouterr().out


@pytest.fixture()
def cli_world(tmp_path, mock, ops, lakehouse, monkeypatch):
    out = make_out(tmp_path / "out")
    ws, lh = lakehouse
    (out / ".state.json").write_text(json.dumps({"workspaceId": ws, "lakehouseId": lh, "sqlDatabase": "sql-1"}))
    monkeypatch.setattr(cli, "make_ops", lambda: ops)
    return out, load_plan(out), tmp_path / "s"


def layer_positions(mock, plan) -> dict[str, tuple[int, int]]:
    names = loaded(mock)
    return {layer: (min(names.index(n) for n in plan.names(layer)), max(names.index(n) for n in plan.names(layer))) for layer in plan.layers()}


def test_load_executes_the_layers_in_order_and_layers_and_only_pick_across_them(cli_world, mock, capsys):
    out, plan, state = cli_world
    assert run("load", "--execute", "--out", out, "--state-dir", state) == 0
    pos = layer_positions(mock, plan)
    assert pos["bronze"][1] < pos["silver"][0] and pos["silver"][1] < pos["gold"][0] and sorted(loaded(mock)) == sorted(plan.all_names())
    assert len(bronze_uploads(mock)) == 3 and '"missingAfterLoad": []' in capsys.readouterr().out
    mock.table_loads.clear()
    mock.requests.clear()
    assert run("load", "--execute", "--out", out, "--state-dir", state, "--layers", "silver") == 0
    assert sorted(loaded(mock)) == sorted(plan.names("silver")) and not bronze_uploads(mock)
    mock.table_loads.clear()
    assert run("load", "--execute", "--out", out, "--state-dir", state, "--only", "fact_t1,silver_t2,bronze_file_manifest") == 0
    assert sorted(loaded(mock)) == ["bronze_file_manifest", "fact_t1", "silver_t2"] and len(bronze_uploads(mock)) == 3     # the manifest table brings the seed files


def test_a_failing_layer_stops_load_with_exit_1_and_names_the_table(cli_world, mock, capsys):
    out, plan, state = cli_world
    mock.load_failures = {"silver_t2": 99}
    assert run("load", "--execute", "--out", out, "--state-dir", state) == 1                                                   # no delta-rs fallback in `load` (use --loader delta)
    text = capsys.readouterr().out
    assert "load stopped in the silver layer at table silver_t2" in text and "the layers after it were not started" in text
    assert not set(loaded(mock)) & set(plan.names("gold"))


def test_load_with_the_delta_loader_writes_through_delta_rs(cli_world, mock, monkeypatch):
    out, plan, state = cli_world
    calls = []

    def fake_delta(self, layer_dir, ws_id, lh_id, only=None, uri_base=None, storage_options=None, layer=None):
        calls.append((layer, list(only)))
        mock.tables.setdefault(lh_id, []).extend(only)
        return {"loaded": sorted(only), "missingAfterLoad": []}
    monkeypatch.setattr(fabric_ops.Ops, "load_layer_delta", fake_delta)
    assert run("load", "--execute", "--out", out, "--state-dir", state, "--loader", "delta", "--layers", "gold") == 0
    assert calls == [("gold", plan.names("gold"))] and not mock.table_loads


def test_a_smoke_build_over_the_folder_of_a_full_build_leaves_only_the_smoke_tables_and_files_in_each_layer(built_out, repo_path, tmp_path, selections):
    out = tmp_path / "out"
    shutil.copytree(built_out, out)                                                                # the output folder of the default (full) build
    full, tiers_before = load_plan(out), {t: files_tree(out / t) for t in ("core", "full")}
    assert full.summary() == selections["full"].summary()
    assert run("build", "--repo", repo_path, "--out", out, "--tier", "smoke") == 0
    sel, plan = selections["smoke"], load_plan(out)
    assert plan.summary() == sel.summary() and (out / "smoke").is_dir()
    for layer, expected in (("bronze", [silver.BRONZE_MANIFEST_TABLE]), ("silver", sel.silver_tables), ("gold", sel.gold_physical)):
        assert sorted(p.stem for p in plan.folder(layer).glob("*.parquet")) == sorted(expected) == plan.names(layer)        # what a glob-based load would find
        assert not [n for n in full.names(layer) if n not in expected and (plan.folder(layer) / f"{n}.parquet").exists()]
    staged = sorted(files_tree(out / "bronze" / "files"))
    assert staged == sorted(f.target[len(layers.BRONZE_FOLDER) + 1:] for f in plan.files) and len(staged) == len(sel.bronze) < len(full.files)
    assert {t: files_tree(out / t) for t in ("core", "full")} == tiers_before                       # the smoke build did not touch the other tiers' ontology folders
    assert len(json.loads((out / "eval" / "golden_expected.json").read_text())) == 29              # the answer keys are computed from all the data


# ================================================================================================ P3/P6: the whole build against the mock
@pytest.fixture(scope="module")
def built_world(tmp_path_factory, repo_path):
    """One real first build (core tier), shared by the read-only assertions below; the wipe test at the end uses it up."""
    w = World(tmp_path_factory.mktemp("w"), repo_path)
    w.rc = w.up()
    yield w
    w.close()


def lakehouse_id(w) -> str:
    return w.ledger.get("fabricLakehouse", w.cfg["lakehouse"]["name"])["id"]


def definition_text(w, collection: str, name: str) -> dict[str, str]:
    ws = next(iter(w.mock.workspaces))
    item = next(i for i in w.mock.items[(ws, collection)].values() if i["displayName"].startswith(name))
    return {p["path"]: base64.b64decode(p["payload"]).decode("utf-8") for p in w.mock.definitions[item["id"]]["parts"]}


def test_the_first_build_loads_bronze_then_silver_then_gold_and_records_every_layer(built_world, selections):
    w = built_world
    assert w.rc == 0, w.text[-2500:]
    plan = load_plan(w.out)
    sel = selections["core"]
    assert plan.summary() == sel.summary() and set(plan.names("gold")) == set(sel.gold_physical)
    names = loaded(w.mock)
    assert sorted(names) == sorted(plan.all_names())                                              # one load per planned table: a core build is no longer all 51 gold tables
    pos = layer_positions(w.mock, plan)
    assert pos["bronze"][1] < pos["silver"][0] and pos["silver"][1] < pos["gold"][0]
    ws, lh = next(iter(w.mock.workspaces)), lakehouse_id(w)
    in_onelake = {k[len(f"{ws}/{lh}/"):]: len(v) for k, v in w.mock.files.items() if "/Files/bronze/" in k}
    assert in_onelake == {f.target: f.bytes for f in plan.files} and all(k.endswith(".json") for k in in_onelake)             # only seed JSON, by release
    state = json.loads((w.out / ".state.json").read_text())
    assert state["loadedLayers"] == plan.fingerprints() and state["loadedFingerprint"] == plan.fingerprint()
    for needle in ("layers for the core tier", "bind to gold only", "load bronze:", "load silver:", "load gold:", "load done:", "seed files uploaded"):
        assert needle in w.text, needle
    steps = (w.out / "NEXT_STEPS.md").read_text()
    assert f"bronze = {len(plan.files)} seed JSON files" in steps and "silver = " in steps and "gold = " in steps and "bind to gold only" in steps


def test_the_semantic_model_the_ontology_and_the_graph_bind_to_gold_only(built_world):
    w = built_world
    gold = set(load_plan(w.out).names("gold"))
    sm_names = {n for text in definition_text(w, "semanticModels", "ContosoBankModel").values() for n in re.findall(r"entityName: (\S+)", text)}
    ont_names = {n for text in definition_text(w, "ontologies", "ContosoBankOntology").values() for n in re.findall(r"entityName: (\S+)", text)}
    graph = " ".join(definition_text(w, "graphModels", "ContosoBankOntology_graph_").values())
    graph_names = set(re.findall(r'/Tables/([A-Za-z0-9_]+)"', graph))
    assert sm_names == gold and ont_names and ont_names <= gold and graph_names and graph_names <= gold
    assert not [n for n in sm_names | ont_names | graph_names if n.startswith(("silver_", "bronze_"))]


def test_diagnose_reports_each_layer_and_flags_a_missing_table_by_layer(built_world):
    w = built_world
    plan, lh = load_plan(w.out), lakehouse_id(w)
    w.logs.clear()
    assert w.diagnose() == 0, w.text
    n = len(plan.files)
    assert f"{plan.counts()['tables']} tables; planned: {plan.summary()}" in w.text
    assert f"[PASS] layer bronze: 1/1 tables registered, {n}/{n} files in OneLake" in w.text
    assert f"[PASS] layer silver: {len(plan.names('silver'))}/{len(plan.names('silver'))} tables registered" in w.text and "[PASS] layer gold:" in w.text
    victim = plan.names("silver")[0]
    w.mock.tables[lh].remove(victim)
    try:
        w.logs.clear()
        assert w.diagnose() == 0                                                                  # a missing table is a warning, not a failure
        assert f"[WARN] layer silver: {len(plan.names('silver')) - 1}/{len(plan.names('silver'))} tables registered; MISSING {victim}" in w.text
        assert "[PASS] layer gold:" in w.text and "[WARN] lakehouse" in w.text
    finally:
        w.mock.tables[lh].append(victim)


def test_verify_reports_each_layer_and_fails_when_a_table_of_any_layer_is_missing(built_world, monkeypatch, capsys):
    w = built_world
    plan, lh = load_plan(w.out), lakehouse_id(w)
    monkeypatch.setattr(cli, "make_ops", w.make_ops)
    args = ("verify", "--out", w.out, "--state-dir", w.state)
    assert run(*args) == 0
    text = capsys.readouterr().out
    assert f"lakehouse tables: {plan.counts()['tables']} present ({plan.summary()} planned)" in text
    assert f"  bronze: 1/1 tables registered, {len(plan.files)}/{len(plan.files)} files in OneLake" in text and "  silver: " in text and "  gold: " in text
    victim = plan.names("gold")[0]
    w.mock.tables[lh].remove(victim)
    try:
        assert run(*args) == 1
        assert f"MISSING {victim}" in capsys.readouterr().out
    finally:
        w.mock.tables[lh].append(victim)


def test_a_smoke_build_loads_only_the_smoke_layers_and_a_bigger_tier_loads_every_layer_again(tmp_path, repo_path, selections):
    w = World(tmp_path, repo_path)
    try:
        assert w.up(tier="smoke", pipeline=w.pipeline(until="build")) == 0, w.text[-2000:]
        small, core = selections["smoke"], selections["core"]
        plan = load_plan(w.out)
        assert plan.summary() == small.summary() and sorted(loaded(w.mock)) == sorted(plan.all_names()) and len(loaded(w.mock)) < len(core.gold) + len(core.silver_tables)
        assert set(plan.names("gold")) == set(small.gold_physical) and "layers for the smoke tier" in w.text
        before = len(w.mock.table_loads)
        w.logs.clear()
        assert w.up(tier="core", pipeline=w.pipeline(until="build")) == 0, w.text[-2000:]
        bigger = load_plan(w.out)
        assert bigger.summary() == core.summary() and sorted(loaded(w.mock)[before:]) == sorted(bigger.all_names())             # a changed tier reloads all three layers
        assert w.text.count("changed since its last load") == 3
    finally:
        w.close()


def test_a_silver_failure_stops_the_build_before_gold_and_the_next_run_resumes_without_redoing_bronze(tmp_path, repo_path, selections, monkeypatch):
    w = World(tmp_path, repo_path)
    try:
        victim = selections["core"].silver[1]
        w.mock.load_failures = {victim: 99}
        with monkeypatch.context() as mp:
            mp.setitem(sys.modules, "deltalake", None)                                            # no fallback available: the failure is final
            assert w.up() == runner.EXIT_PARTIAL
        assert f"ERROR the silver layer stopped at table {victim}" in w.text and "gold was not started" in w.text and "PARTIAL" in w.text
        plan = load_plan(w.out)
        assert not set(loaded(w.mock)) & set(plan.names("gold")) and list(cli.read_state(w.out)["loadedLayers"]) == ["bronze"]
        assert {e["kind"] for e in w.ledger.owned()} == {"resourceGroup", "fabricCapacity", "fabricWorkspace", "fabricLakehouse"}       # nothing leaked
        w.mock.load_failures = {}
        before, uploads = len(w.mock.table_loads), bronze_uploads(w.mock)
        w.logs.clear()
        assert w.up() == 0, w.text[-2000:]
        assert layers_of(plan, loaded(w.mock)[before:]) == {"silver", "gold"} and bronze_uploads(w.mock) == uploads
        assert "load bronze: unchanged since its last load" in w.text and "BUILD DONE" in w.text
    finally:
        w.close()


def test_an_ontology_refused_while_the_sql_endpoint_catches_up_says_so_and_the_rerun_resumes_without_loading_again(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    try:
        w.mock.endpoint_lag_creates = 2                                                           # the endpoint does not know the loaded tables yet: the create is refused (twice)
        w.mock.sql_sync_polls = 2                                                                 # and the refresh after the last layer takes a few polls
        assert w.up(tier="smoke") == runner.EXIT_PARTIAL
        assert "PARTIAL" in w.text and w.mock.endpoint_lag_refusals >= 1
        assert "SQL endpoint sync: the refresh finished" in w.text                                # the build waited for the refresh before it went on to the ontology
        assert "note: the lakehouse tables were loaded" in w.text and "probably still syncing" in w.text and "run bash build.sh again" in w.text
        assert "the semantic model reads OneLake directly" in w.text
        stamp = cli.read_state(w.out)["lastLoadAt"]
        assert stamp.endswith("Z")
        w.mock.endpoint_lag_creates = 0                                                           # a few minutes later the endpoint knows them
        before = len(w.mock.table_loads)
        w.logs.clear()
        assert w.up(tier="smoke") == 0, w.text[-2000:]
        assert len(w.mock.table_loads) == before and "already holds these" in w.text and "BUILD DONE" in w.text      # resumed at the ontology: nothing was loaded again
        assert cli.read_state(w.out)["lastLoadAt"] == stamp and "note: the lakehouse tables were loaded" not in w.text
    finally:
        w.close()


def test_a_wipe_after_a_layered_build_verifies_clean_and_does_not_claim_that_only_gold_was_loaded(built_world):
    w = built_world
    ws, lh = next(iter(w.mock.workspaces)), lakehouse_id(w)
    assert w.mock.tables.get(lh) and any("/Files/bronze/" in k for k in w.mock.files)             # all three layers are there before the wipe
    w.logs.clear()
    assert w.down(plan_only=True) == 0 and "bronze files" in w.text and "silver and gold tables" in w.text
    w.logs.clear()
    assert w.down() == 0, w.text
    assert "VERIFIED" in w.text and not w.mock.workspaces and w.fake.cap is None and w.fake.rg_exists is False
    assert not w.mock.files and not w.mock.tables and w.mock.deleted_workspaces[ws]["itemsLeft"] == 0         # nothing of any layer is left in OneLake
    assert "only gold" not in w.text.lower() and "bronze files" in w.text                          # the wipe text covers every layer

