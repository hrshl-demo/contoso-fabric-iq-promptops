"""Repo-backed silver build tests: spec coverage, row counts that reproduce gold, keys, flat typed columns, lineage back to the seed,
the bronze and silver manifests, and determinism. They skip without the source repo snapshot like the other repo-backed tests."""
import hashlib
import json
import re
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from contoso_onto import gold, silver

KIT = Path(__file__).resolve().parent.parent
FR, L4 = "retail-frontier-v1", "retail4-v1"
REFERENCE = {"calendar", "law", "authority_rule", "frontier_requirement", "bank_agent", "service_request_type", "journey", "journey_sop",
             "journey_product"}
# rows silver keeps (it is source-faithful per release) that the v1 gold merges by precedence: 6 CRM-case parties that repeat a persona id
# and the catalogue product CB-SAV-SALARY that both seed releases carry
SURVIVORSHIP = {"customer": 6, "product": 1}
ALLOWED_TYPES = [pa.string(), pa.int64(), pa.bool_(), pa.date32(), pa.timestamp("us", tz="UTC"), pa.decimal128(38, 4)]
# silver table -> the release.json counts key that sizes it, in each release that states the key
PER_RELEASE = {"silver_account": "accounts", "silver_card": "cards", "silver_deposit": "deposits", "silver_loan": "loans", "silver_goal": "goals",
               "silver_transaction": "transactions", "silver_document": "documents", "silver_product": "products", "silver_customer": "personas",
               "silver_household_member": "householdMembers", "silver_household_access_grant": "accessGrants",
               "silver_income_record": "incomeRecords", "silver_scheduled_obligation": "scheduledObligations", "silver_incident": "incidents",
               "silver_service_case": "serviceRecords", "silver_proactive_candidate": "proactiveEvents", "silver_sop": "sops",
               "silver_registered_agent": "agents", "silver_frontier_mandate": "mandates", "silver_investment": "investments"}
PLAIN_ONLY = {"explode", "where", "require", "absent", "keyed", "single", "data_glob", "doc"}


def raw_json(repo, rel):
    return json.loads((repo / rel).read_bytes().decode("utf-8-sig"))


def release_counts(repo, release):
    return raw_json(repo, f"data/releases/{release}/release.json")["counts"]


def dig(node, path):
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def count(rows, table, release):
    return sum(1 for r in rows[table] if r["_release_id"] == release)


def plain_files(t):
    """{repo path: list key} when every source of the table maps record for record onto one JSON file, else None."""
    out = {}
    for src in t["sources"]:
        if "custom" in src or set(src) & PLAIN_ONLY:
            return None
        path = silver.repo_path(src)
        if path in out:
            return None
        out[path] = src.get("list")
    return out


# ------------------------------------------------------------------------------------------------ spec and build
def test_spec_is_valid_and_serves_every_non_reference_gold_table(silver_spec, model):
    assert silver.validate_spec(silver_spec) == []
    gold_tables = {t["table"] for t in model["tables"]}
    served = {g for t in silver_spec["tables"] for g in t["serves"]}
    assert served <= gold_tables
    assert gold_tables - served == REFERENCE


def test_table_names_follow_the_entity_and_the_profile_extension_split(silver_spec):
    tables = silver_spec["tables"]
    assert all(t["table"] == "silver_" + silver.snake(t["entity"]) for t in tables)
    assert sum(1 for t in tables if t.get("profile_entity")) == 19
    assert sum(1 for t in tables if not t.get("profile_entity")) == 24
    assert all(t["profile"] is (t.get("profile_entity") is not None) for t in tables)


def test_build_is_clean_and_complete(silver_built, silver_spec):
    assert silver_built.errors == [] and silver_built.warnings == []
    assert set(silver_built.tables) == {t["table"] for t in silver_spec["tables"]} | {silver.ENTITY_CATALOG, silver.ATTRIBUTE_CATALOG}
    assert all(silver_built.tables[t["table"]].num_rows > 0 for t in silver_spec["tables"])


def test_physical_names_are_valid_and_unique_case_insensitively(silver_built):
    columns = {n: t.schema.names for n, t in silver_built.tables.items()}
    assert silver.validate_physical_names(list(columns), columns) == []
    assert not [n for n in columns if n.startswith(("dim_", "fact_", "bridge_", "ref_"))]


def test_silver_row_counts_reproduce_gold_except_the_survivorship_list(silver_built, silver_spec, built):
    feeds: dict[str, int] = {}
    for t in silver_spec["tables"]:
        for g in t["serves"]:
            feeds[g] = feeds.get(g, 0) + silver_built.tables[t["table"]].num_rows
    for g, n in feeds.items():
        assert n == built.tables[g].num_rows + SURVIVORSHIP.get(g, 0), g
    reported = {m.group(1): int(m.group(2)) for w in built.warnings if (m := re.match(r"(\w+): (\d+) duplicate key rows merged by precedence", w))}
    assert reported == SURVIVORSHIP


def test_release_row_counts_match_the_release_manifests(silver_rows, repo_path):
    fr, l4 = release_counts(repo_path, FR), release_counts(repo_path, L4)
    for table, key in PER_RELEASE.items():
        for release, counts in ((FR, fr), (L4, l4)):
            if key in counts:
                assert count(silver_rows, table, release) == counts[key], (table, release)
    # insurance.json mixes policies and one protection-gap record: policies + gap = counts.insurancePolicies
    assert count(silver_rows, "silver_protection_policy", FR) + count(silver_rows, "silver_protection_gap", FR) == fr["insurancePolicies"]
    assert count(silver_rows, "silver_protection_policy", L4) == l4["policies"]
    assert sum(1 for r in silver_rows["silver_product"] if r["_release_id"] is None) == 5


def test_keys_are_unique_per_release_and_ids_are_unique(silver_spec, silver_rows):
    for t in silver_spec["tables"]:
        rows = silver_rows[t["table"]]
        keys = [(r["_release_id"], *[r[k] for k in t["key"]]) for r in rows]
        assert len(set(keys)) == len(keys), t["table"]
        assert all(v is not None for k in keys for v in k[1:]), t["table"]
        ids = [r["id"] for r in rows]
        assert len(set(ids)) == len(ids), t["table"]
        assert all(i.startswith(f"{t['entity']}:") for i in ids), t["table"]


def test_a_key_repeated_in_a_later_release_is_namespaced_in_the_opaque_id_only(silver_rows):
    namespaced = {(n, r["id"]) for n, rows in silver_rows.items() if "id" in rows[0] for r in rows if "~" in r["id"]}
    assert namespaced == {("silver_customer", "Customer:rhea~legacy"), ("silver_product", "Product:CB-SAV-SALARY~legacy")}
    rhea = {r["id"]: r for r in silver_rows["silver_customer"]}
    assert rhea["Customer:rhea"]["_release_id"] == FR and rhea["Customer:rhea~legacy"]["_release_id"] == L4
    assert rhea["Customer:rhea~legacy"]["ownerId"] == "rhea" and rhea["Customer:rhea~legacy"]["sourceRecordId"] == "rhea"
    salary = [r for r in silver_rows["silver_product"] if r["productId"] == "CB-SAV-SALARY"]
    assert sorted(r["_release_id"] for r in salary) == [FR, L4]


# ------------------------------------------------------------------------------------------------ shapes and types
def test_every_column_is_flat_and_typed_without_floating_point(silver_built):
    for name, tbl in silver_built.tables.items():
        for f in tbl.schema:
            assert not pa.types.is_nested(f.type) and not pa.types.is_floating(f.type), (name, f.name, f.type)
            assert any(f.type.equals(t) for t in ALLOWED_TYPES), (name, f.name, f.type)


def test_decimal_columns_exist_only_as_official_currency_aliases(silver_built, silver_rows):
    official = {(r["silver_table"], r["column_name"]) for r in silver_rows[silver.ATTRIBUTE_CATALOG]
                if r["origin"] == "cdm_official" and r["cdm_type"] == "currency"}
    decimals = {(n, f.name) for n, t in silver_built.tables.items() for f in t.schema if pa.types.is_decimal(f.type)}
    assert decimals == official and decimals


def test_money_is_int64_paise_beside_a_currency(silver_built, silver_spec, silver_rows):
    seen = 0
    for t in silver_spec["tables"]:
        for c in t["columns"]:
            if c["type"] != "minor":
                continue
            assert silver_built.tables[t["table"]].schema.field(c["col"]).type == pa.int64()
            assert all(r["currency"] == "INR" for r in silver_rows[t["table"]] if r[c["col"]] is not None), (t["table"], c["col"])
            seen += 1
    assert seen >= 40


def test_columns_follow_the_documented_order(silver_built):
    rank = {"common": 0, "profile": 1, "cdm_official": 2, "extension": 3, "technical": 4}
    for name, res in silver_built.results.items():
        ranks = [rank[c["origin"]] for c in res.columns]
        assert ranks == sorted(ranks), name
        assert [c["col"] for c in res.columns] == silver_built.tables[name].schema.names
        assert [c["col"] for c in res.columns[:7]] == list(silver.COMMON_COLUMNS)
        assert [c["col"] for c in res.columns[-4:]] == ["_release_id", "_source_file", "_source_row", "_owner_id"]


def test_technical_columns_name_the_release_file_row_and_owner(silver_spec, silver_rows, repo_path):
    for t in silver_spec["tables"]:
        rows = silver_rows[t["table"]]
        for r in rows:
            assert r["_release_id"] in (FR, L4) or (r["_release_id"] is None and r["_source_file"] == "data/relationship/products.json")
            assert (repo_path / r["_source_file"]).is_file() and r["_source_row"] >= 1
            if r["_release_id"]:
                assert r["_source_file"].startswith(f"data/releases/{r['_release_id']}/")
        owners = [r["_owner_id"] for r in rows if r["_owner_id"] is not None]
        assert bool(owners) == bool(t.get("owner")), t["table"]
    assert {r["_owner_id"] for r in silver_rows["silver_customer"]} == {"rhea", "nivedita", "ravi"}


def test_restricted_and_pii_seed_fields_stay_flagged_in_silver(silver_spec, v1_model):
    by_serves: dict[str, list[dict]] = {}
    for t in silver_spec["tables"]:
        for g in t["serves"]:
            by_serves.setdefault(g, []).append(t)
    checked = 0
    for g in v1_model["tables"]:
        for c in g["columns"]:
            flag = "restricted" if c.get("ontology") is False else "pii" if c.get("pii") else None
            for p in silver.source_paths(c) if flag else []:
                readers = [s for t in by_serves.get(g["table"], []) for s in t["columns"] if p in silver.source_paths(s)]
                assert readers, (g["table"], p)
                assert all(s.get(flag) or s.get("restricted") for s in readers), (g["table"], p, flag)
                checked += 1
    assert checked >= 10


# ------------------------------------------------------------------------------------------------ lineage to the seed
def test_rows_trace_back_to_the_seed_record_they_came_from(silver_spec, silver_rows, repo_path):
    tables = money = 0
    for t in silver_spec["tables"]:
        files = plain_files(t)
        if files is None:
            continue
        by_col = {c["col"]: c for c in t["columns"]}
        for path, key in files.items():
            raw = raw_json(repo_path, path)
            records = raw[key] if key else raw
            rows = [r for r in silver_rows[t["table"]] if r["_source_file"] == path]
            assert [r["_source_row"] for r in rows] == list(range(1, len(records) + 1)), t["table"]
            for row, rec in zip(rows, records):
                for k in t["key"]:
                    c = by_col[k]
                    if isinstance(c.get("from"), str) and not c.get("t"):
                        assert row[k] == dig(rec, c["from"]), (t["table"], k)
                for c in t["columns"]:
                    if c.get("t") == "paise" and isinstance(c.get("from"), str):
                        obj = dig(rec, c["from"])
                        assert row[c["col"]] == (None if obj is None else obj["amountMinor"]), (t["table"], c["col"])
                        money += 1
        tables += 1
    assert tables >= 20 and money >= 100


@pytest.mark.parametrize("name", sorted(silver.SILVER_CUSTOM))
def test_silver_flatteners_reproduce_the_gold_ones(name, silver_spec, v1_model, repo_path):
    """The flatteners the v1 gold engine still carries (gold.CUSTOM_SOURCES, the independent reference implementation) and the silver ones
    give the same rows; the v1 gold spec says which gold table reads which flattener."""
    repo = gold.Repo(repo_path, v1_model["canonical_release"], v1_model.get("release_short"))
    flat = lambda v: "; ".join(str(x) for x in v) if isinstance(v, list) else v  # noqa: E731
    silver_nodes, gold_nodes = [], []
    served = set()
    for t in silver_spec["tables"]:
        for src in t["sources"]:
            if src.get("custom") == name:
                served.update(t["serves"])
                silver_nodes.extend(node for _, _, _, node in silver.SILVER_CUSTOM[name](repo, src))
    assert served
    for g in v1_model["tables"]:
        for src in g["sources"]:
            if src.get("custom") == name and g["table"] in served:
                gold_nodes.extend(gold.CUSTOM_SOURCES[name](repo, src))
    assert silver_nodes and len(silver_nodes) == len(gold_nodes)
    assert [{k: flat(v) for k, v in n.items()} for n in silver_nodes] == gold_nodes


# ------------------------------------------------------------------------------------------------ manifests
@pytest.fixture(scope="module")
def written(silver_built, tmp_path_factory):
    out = tmp_path_factory.mktemp("medallion")
    return out, silver.write_bronze(silver_built, out), silver.write_silver(silver_built, out, KIT / "model")


def test_bronze_manifest_lists_every_seed_file_with_its_hash(written, silver_built, silver_spec, silver_rows, repo_path):
    out, manifest, _ = written
    files = manifest["files"]
    assert files == silver_built.bronze
    paths = [f["path"] for f in files]
    assert paths == sorted(paths) and len(set(paths)) == len(paths) and all(p.endswith(".json") for p in paths)
    assert {r["_source_file"] for rows in silver_rows.values() for r in rows if "_source_file" in r} <= set(paths)
    assert {f"data/releases/{rel}/release.json" for rel in (FR, L4)} | {"data/relationship/products.json"} <= set(paths)
    lists: dict[str, set] = {}
    for t in silver_spec["tables"]:
        for src in t["sources"]:
            if "custom" not in src and ("file" in src or "data" in src):
                lists.setdefault(silver.repo_path(src), set()).update([src["list"]] if src.get("list") else [])
    for f in files:
        data = (repo_path / f["path"]).read_bytes()
        raw = raw_json(repo_path, f["path"])
        keys = lists.get(f["path"])
        want = sum(len(raw[k]) for k in keys) if keys else len(raw) if isinstance(raw, list) else 1
        assert f["sha256"] == hashlib.sha256(data).hexdigest() and f["bytes"] == len(data) and f["record_count"] == want, f["path"]
        assert f["release"] == (re.match(r"data/releases/([^/]+)/", f["path"]) or [None, None])[1]
    on_disk = json.loads((out / "bronze" / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk == manifest and manifest["layer"] == "bronze"
    table = pq.read_table(out / "bronze" / f"{silver.BRONZE_MANIFEST_TABLE}.parquet")
    assert table.to_pylist() == files
    assert table.schema.names == ["release", "path", "sha256", "bytes", "record_count"]


def test_silver_manifest_describes_every_parquet_file(written, silver_built, silver_spec):
    out, _, manifest = written
    names = {t["table"] for t in silver_spec["tables"]} | {silver.ENTITY_CATALOG, silver.ATTRIBUTE_CATALOG}
    folder = out / "silver"
    assert set(manifest["tables"]) == names == {p.stem for p in folder.glob("*.parquet")}
    assert manifest["layer"] == "silver" and json.loads((folder / "manifest.json").read_text(encoding="utf-8")) == manifest
    serves = {t["table"]: t["serves"] for t in silver_spec["tables"]}
    for name, m in manifest["tables"].items():
        p = folder / f"{name}.parquet"
        stored = pq.read_table(p)
        assert m["physical"] == name and m["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest() and m["bytes"] == p.stat().st_size
        assert m["rows"] == stored.num_rows == silver_built.tables[name].num_rows and m["columns"] == stored.num_columns
        assert m["schema"] == [{"name": f.name, "type": str(f.type), "nullable": f.nullable} for f in stored.schema]
        assert stored.schema.equals(silver_built.tables[name].schema)
        assert m.get("serves") == serves.get(name)
    assert set(manifest["specFiles"]) == {p.name for p in (KIT / "model" / "silver").glob("*.yaml")}
    assert manifest["profile"]["bianRelease"] == "13.0.0"


def test_two_builds_are_byte_identical(written, model, silver_spec, repo_path, tmp_path):
    again = silver.build_silver(model, silver_spec, repo_path)
    assert again.ok, again.errors
    silver.write_bronze(again, tmp_path)
    silver.write_silver(again, tmp_path, KIT / "model")
    first = written[0]
    files = sorted(p.relative_to(first) for p in first.rglob("*") if p.is_file())
    assert files == sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    assert len(files) == 43 + 2 + 1 + 1 + 1
    for f in files:
        assert (first / f).read_bytes() == (tmp_path / f).read_bytes(), f
