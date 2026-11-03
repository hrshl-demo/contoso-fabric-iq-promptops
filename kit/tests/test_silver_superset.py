"""Silver is a superset of what gold needs (design doc 4.3.1): every seed field a v1 gold column reads through `from`, `tpl` or `t` is
exposed by the silver table(s) the gold table `serves`, in flat typed form, so gold never has to read bronze, and never has to decode the
common `state` / `label` columns: the raw lifecycle and display fields behind them are real columns too.

The comparison is data aware: a gold path counts only where some record of that source actually holds a value. v1 gold columns list
fallback paths for every source of a table (customer reads `[name, displayName, customerName]` from all three of its sources), and 26
of those paths exist in no record of the source they are tried on; a silver table cannot lose a value that does not exist.

"v1 gold" is the gold specification as it was before gold was built from silver: tests/fixtures/gold_v1/spec is model/gold/*.yaml at the v1
baseline commit (raw seed paths), frozen next to the v1 output. The live gold specs now name silver columns; they are checked by
test_gold_equivalence.py (same output as v1) and by the engine, which rejects a gold column that reads a silver column that does not exist."""
import copy

import pytest

from contoso_onto import silver
from contoso_onto.gold import Repo, expand_columns, source_rows

REFERENCE = {"calendar", "law", "authority_rule", "frontier_requirement", "bank_agent", "service_request_type", "journey", "journey_sop",
             "journey_product"}
# gold table -> {(source, level, seed field): why silver deliberately does not expose it}. Empty: silver exposes every seed field gold reads.
EXCEPTIONS: dict[str, dict[tuple[str, str, str], str]] = {}


@pytest.fixture(scope="module")
def repo(model, repo_path):
    return Repo(repo_path, model["canonical_release"], model.get("release_short"))


def serving(spec):
    out: dict[str, list[dict]] = {}
    for t in spec["tables"]:
        for g in t["serves"]:
            out.setdefault(g, []).append(t)
    return out


def exposed(tables):
    """(source, level, field) triples the profile and extension columns (plus the owner scope) of the tables read."""
    found: set = set()
    for t in tables:
        found |= silver.read_fields(t, [c for c in t["columns"] if c["origin"] in silver.ORIGINS])
        if t.get("owner"):
            found |= silver.read_fields(t, [{"from": t["owner"]}])
    return found


def needed(g, repo):
    """(source, level, field) triples a gold table's columns read where the seed holds a value."""
    found: set = set()
    for src in g["sources"]:
        rows = source_rows(src, repo)
        for c in expand_columns(g):
            for p in silver.source_paths(c):
                n = silver.normalise_path(p, src)
                if n and any(r.get(p) is not None for r in rows):
                    found.add((silver.source_key(src), *n))
    return found


def gaps(g, tables, repo):
    return needed(g, repo) - exposed(tables) - set(EXCEPTIONS.get(g["table"], {}))


def test_every_seed_field_gold_reads_is_exposed_by_silver(v1_model, silver_spec, repo):
    serves = serving(silver_spec)
    checked = 0
    for g in v1_model["tables"]:
        if g["table"] in REFERENCE:
            continue
        assert serves.get(g["table"]), g["table"]
        want = needed(g, repo)
        assert want, g["table"]
        assert not gaps(g, serves[g["table"]], repo), (g["table"], sorted(gaps(g, serves[g["table"]], repo)))
        checked += len(want)
    assert checked >= 400


def test_the_check_detects_a_dropped_column(v1_model, silver_spec, repo):
    gold_loan = next(g for g in v1_model["tables"] if g["table"] == "loan")
    spec = copy.deepcopy(silver_spec)
    table = next(t for t in spec["tables"] if t["table"] == "silver_loan")
    assert not gaps(gold_loan, serving(spec)["loan"], repo)
    table["columns"] = [c for c in table["columns"] if c["col"] != "xOpeningPrincipalMinor"]
    assert gaps(gold_loan, serving(spec)["loan"], repo) == {("retail-frontier-v1/loans.json", "", "openingPrincipal")}


# the seed fields that carry a record's lifecycle: gold needs their raw values, never the common `state` that silver derives from them
LIFECYCLE_FIELDS = {"status", "state", "stage", "eventType", "resolutionStatus", "revokedAt"}


def test_every_lifecycle_field_gold_reads_is_exposed_as_a_column(v1_model, silver_spec, repo):
    serves = serving(silver_spec)
    seen: set = set()
    for g in v1_model["tables"]:
        if g["table"] in REFERENCE:
            continue
        wanted = {k for k in needed(g, repo) if k[2].split(".")[-1] in LIFECYCLE_FIELDS}
        assert wanted <= exposed(serves[g["table"]]), (g["table"], sorted(wanted - exposed(serves[g["table"]])))
        seen |= {(g["table"], *k) for k in wanted}
    assert len(seen) >= 20


def test_every_seed_field_behind_a_common_column_rule_is_also_a_real_column(silver_spec, repo):
    """The state and label rules read seed fields; any of them that some record holds is a profile or extension column as well."""
    checked = 0
    for t in silver_spec["tables"]:
        rows = [r for src in t["sources"] for r in silver.silver_source_rows(src, repo)]
        exposed_paths = {p for c in t["columns"] if c["origin"] in silver.ORIGINS for p in silver.source_paths(c)}
        for p in silver.common_paths(t):
            if any(r.get(p) is not None for r in rows):
                assert p in exposed_paths, (t["table"], p)
                checked += 1
    assert checked >= 25


def test_the_exception_list_is_documented_and_not_stale(v1_model, silver_spec, repo):
    serves = serving(silver_spec)
    by_gold = {g["table"]: g for g in v1_model["tables"]}
    for table, items in EXCEPTIONS.items():
        for triple, reason in items.items():
            assert reason.strip(), (table, triple)
            assert triple in needed(by_gold[table], repo) - exposed(serves[table]), f"stale exception {table} {triple}"


def has_key(node, path):
    for seg in path.split("."):
        if isinstance(node, dict) and seg in node:
            node = node[seg]
        elif isinstance(node, list) and seg.isdigit() and int(seg) < len(node):
            node = node[int(seg)]
        else:
            return False
    return True


def test_every_silver_seed_path_exists_in_its_source(silver_spec, repo):
    """A typo in a `from` path would silently null a column: some path of every column must be a key of some record (a null value
    still counts, the seed declares the field; retail4 consents carry `revokedAt: null`)."""
    dead = []
    for t in silver_spec["tables"]:
        rows = []
        for src in t["sources"]:
            rows.extend(silver.silver_source_rows(src, repo))
        for c in t["columns"]:
            paths = [p for p in silver.source_paths(c) if p != "@" and not p.startswith(("$", "^"))]
            if paths and not any(has_key(r.node, p) for p in paths for r in rows):
                dead.append((t["table"], c["col"], paths))
    assert dead == [], dead
