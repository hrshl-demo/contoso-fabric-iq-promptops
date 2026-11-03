"""The arbiter of the gold-from-silver migration: gold built from silver must reproduce the frozen v1 gold (tests/fixtures/gold_v1, 51 tables
built straight from the seed) by logical table id: the same columns in the same order with the same Arrow types, the same rows, the same
values per key (floats within 1e-9; in practice bit for bit), and the same 29 golden answers. A difference is allowed only when
tests/fixtures/gold_v1/ALLOWED_DIFFS.yaml lists it with a reason (it lists none)."""
import datetime as dt
import gzip
import json
import math
from pathlib import Path
from typing import Optional

import pytest
import yaml

from contoso_onto import oracles
from contoso_onto import spec as S
from contoso_onto.gold import expand_columns

FIXTURES = Path(__file__).parent / "fixtures" / "gold_v1"
SUMMARY = json.loads((FIXTURES / "_summary.json").read_text(encoding="utf-8"))
ALLOWED = (yaml.safe_load((FIXTURES / "ALLOWED_DIFFS.yaml").read_text(encoding="utf-8")) or {}).get("allowed") or []
TOLERANCE = 1e-9
MAX_REPORTED = 8
# ontology / semantic-model counts of the v1 build: the medallion redesign must not move them (check_spec stats)
BASELINE_COUNTS = {
    "core": {"entities": 31, "properties": 417, "ontologyRelationships": 53, "measures": 39, "rules": 23},
    "full": {"entities": 50, "properties": 534, "ontologyRelationships": 78, "measures": 42, "rules": 23},
}
# The v1 semantic relationships (core 35, full 54) plus the semantic-only date-role relationships to the calendar that the design allows
# (docs/MEDALLION_DESIGN.md decision 3) and the naming phase added: ServiceCase (a core table) and ProactiveCandidate (extended tier), the two facts with a
# `date` column and no date relationship in v1. Nothing else about the semantic model moved: no ontology relationship, no measure.
V1_SEMANTIC_RELATIONSHIPS = {"core": 35, "full": 54}
DATE_ROLES_ADDED = {"core": 1, "full": 2}


def v1_rows(name: str) -> list[dict]:
    with gzip.open(FIXTURES / f"{name}.json.gz", "rt", encoding="utf-8") as f:
        return json.load(f)


def plain(v):
    """A built value the way the v1 fixture stores it: dates and instants as ISO text, everything else as it is."""
    return v.isoformat() if isinstance(v, (dt.datetime, dt.date)) else v


def built_rows(tables: dict, name: str) -> list[dict]:
    return [{k: plain(v) for k, v in r.items()} for r in tables[name].to_pylist()]


def same(a, b, exact: bool) -> bool:
    if a is None or b is None or isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, float) and isinstance(b, float):
        return a == b if exact else math.isclose(a, b, rel_tol=0.0, abs_tol=TOLERANCE)
    return type(a) is type(b) and a == b


def raw_diffs(name: str, tables: dict, key: str, exact: bool = False) -> list[tuple[Optional[str], object, str]]:
    """(column, key value, text) of every difference between the v1 rows and the rows built from silver."""
    got, want = {r[key]: r for r in built_rows(tables, name)}, {r[key]: r for r in v1_rows(name)}
    out: list[tuple[Optional[str], object, str]] = []
    out += [(None, k, f"{name}: key {k!r} is in v1 but not in gold from silver") for k in sorted(set(want) - set(got), key=str)]
    out += [(None, k, f"{name}: key {k!r} is in gold from silver but not in v1") for k in sorted(set(got) - set(want), key=str)]
    for k in sorted(set(got) & set(want), key=str):
        out += [(col, k, f"{name}[{k}].{col}: v1 {w!r}, from silver {got[k].get(col)!r}") for col, w in want[k].items()
                if not same(got[k].get(col), w, exact)]
    return out


def covers(entry: dict, table: str, column: Optional[str], key: object) -> bool:
    return entry["table"] == table and entry.get("column") in (None, column) and entry.get("key") in (None, key)


def unexplained(name: str, tables: dict, key: str, exact: bool = False) -> list[str]:
    return [text for col, k, text in raw_diffs(name, tables, key, exact) if not any(covers(a, name, col, k) for a in ALLOWED)]


@pytest.fixture(scope="module")
def key_of(model) -> dict:
    return {t["table"]: next(c["col"] for c in expand_columns(t) if c.get("key")) for t in model["tables"]}


@pytest.fixture(scope="module")
def arrow(built) -> dict:
    return built.tables


def test_the_baseline_has_the_51_tables_and_2501_rows():
    assert len(SUMMARY["tables"]) == 51 and sum(t["rows"] for t in SUMMARY["tables"].values()) == 2501
    assert SUMMARY["baseline_commit"] == "c421458"


def test_every_v1_table_is_built_and_nothing_else(built, model):
    assert set(built.tables) == set(SUMMARY["tables"]) == {t["table"] for t in model["tables"]}


@pytest.mark.parametrize("name", sorted(SUMMARY["tables"]))
def test_column_names_order_types_and_row_counts_equal_v1(arrow, name):
    want, got = SUMMARY["tables"][name], arrow[name]
    assert [(f.name, str(f.type)) for f in got.schema] == [(c["name"], c["type"]) for c in want["columns"]]
    assert got.num_rows == want["rows"] == len(v1_rows(name))


@pytest.mark.parametrize("name", sorted(SUMMARY["tables"]))
def test_every_value_equals_v1_by_key(arrow, key_of, name):
    diffs = unexplained(name, arrow, key_of[name])
    assert not diffs, f"{len(diffs)} difference(s) to v1:\n" + "\n".join(diffs[:MAX_REPORTED])


@pytest.mark.parametrize("name", sorted(SUMMARY["tables"]))
def test_floats_are_bit_for_bit_the_v1_floats(arrow, key_of, name):
    """The contract allows 1e-9; the migration achieves more: rupees are minor/100.0 and percentages are float(decimal text), the same
    correctly rounded doubles v1 made from the seed numbers."""
    diffs = unexplained(name, arrow, key_of[name], exact=True)
    assert not diffs, f"{len(diffs)} float difference(s):\n" + "\n".join(diffs[:MAX_REPORTED])


def equal_answers(a, b) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equal_answers(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equal_answers(x, y) for x, y in zip(a, b))
    return same(a, b, exact=False)


def test_the_29_golden_answers_equal_the_v1_answers(arrow, model):
    tables = {k: v.to_pandas() for k, v in arrow.items()}
    got = {r["id"]: r for r in oracles.run_oracles(tables, model["golden"])}
    want = json.loads((FIXTURES / "golden_expected_full.json").read_text(encoding="utf-8"))
    assert len(want) == len(got) == 29 and not [g for g in got.values() if g.get("error")]
    for w in want:
        g = json.loads(json.dumps(got[w["id"]]["expected"], default=str))
        assert equal_answers(g, w["expected"]), f"{w['id']}: v1 {w['expected']!r}, from silver {g!r}"


@pytest.mark.parametrize("tier", ["core", "full"])
def test_ontology_and_semantic_counts_equal_the_baseline(model, tier):
    rep = S.check_spec(S.make_spec(model, tier))
    assert rep.ok, rep.errors
    assert {k: rep.stats[k] for k in BASELINE_COUNTS[tier]} == BASELINE_COUNTS[tier]
    assert rep.stats["semanticRelationships"] == V1_SEMANTIC_RELATIONSHIPS[tier] + DATE_ROLES_ADDED[tier] == {"core": 36, "full": 56}[tier]


def test_allowed_diffs_are_justified_and_all_used(arrow, key_of):
    assert all(isinstance(a.get("reason"), str) and a["reason"].strip() and a.get("table") in SUMMARY["tables"] for a in ALLOWED)
    for a in ALLOWED:        # an exemption that covers no difference is stale and would hide a later regression
        found = raw_diffs(a["table"], arrow, key_of[a["table"]])
        assert any(covers(a, a["table"], col, k) for col, k, _ in found), f"allowed diff {a} covers no difference"
