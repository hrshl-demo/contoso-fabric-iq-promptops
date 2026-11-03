import importlib.util
import os
import sys
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))

import yaml  # noqa: E402

from contoso_onto import cli, silver  # noqa: E402
from contoso_onto.gold import load_model  # noqa: E402
from contoso_onto.pipeline import build_gold  # noqa: E402

V1_SPEC = KIT / "tests" / "fixtures" / "gold_v1" / "spec"


def _repo() -> Path | None:
    for cand in (os.environ.get("CONTOSO_REPO"), KIT.parent / "_snapshot" / "contoso-retail-superapp4"):
        if cand and (Path(cand) / "data" / "releases" / "retail-frontier-v1").is_dir():
            return Path(cand)
    return None


SLOW_FILES = {"test_runner.py", "test_layer_load.py", "test_scripts.py"}


def pytest_collection_modifyitems(config, items):
    """The orchestration tests (fake Azure and Fabric, 5 to 20 seconds each) are marked slow: CI runs them when the engine changes and on release."""
    for item in items:
        if Path(str(item.fspath)).name in SLOW_FILES:
            item.add_marker(pytest.mark.slow)


@pytest.fixture(scope="session")
def repo_path() -> Path:
    p = _repo()
    if p is None:
        msg = "set CONTOSO_REPO to a checkout of contoso-retail-superapp4 (read only) to run the repo-backed tests"
        if os.environ.get("CI") or os.environ.get("CONTOSO_REQUIRE_REPO"):
            pytest.fail(msg)                          # a CI run without the repo must not be green: most of the data tests would be skipped
        pytest.skip(msg)
    return p


@pytest.fixture(scope="session")
def repo_or_none() -> Path | None:
    """The source repo when it is available; tests that have an offline fallback use this instead of skipping."""
    return _repo()


@pytest.fixture(scope="session")
def model() -> dict:
    return load_model(KIT / "model")


@pytest.fixture(scope="session")
def v1_model(model) -> dict:
    """The gold specification as it was before gold was built from silver (tests/fixtures/gold_v1/spec, frozen next to the v1 output):
    51 tables with raw seed sources and seed paths. Tests about the seed-to-silver step and the raw engine path read this."""
    tables = []
    for f in sorted(V1_SPEC.glob("*.yaml")):
        tables.extend(yaml.safe_load(f.read_text(encoding="utf-8"))["tables"])
    assert len(tables) == 51
    return {"tables": tables, "canonical_release": model["canonical_release"], "release_short": model.get("release_short")}


@pytest.fixture(scope="session")
def built(model, silver_built, repo_path):
    """Gold built from the session's silver build (the same silver layer the silver tests read)."""
    b = build_gold(model, repo_path, silver=silver_built)
    assert b.ok, b.errors
    return b


@pytest.fixture(scope="session")
def tables(built) -> dict:
    return {k: v.to_pandas() for k, v in built.tables.items()}


@pytest.fixture(scope="session")
def silver_spec() -> dict:
    return silver.load_silver_spec(KIT / "model")


@pytest.fixture(scope="session")
def built_out(repo_path, tmp_path_factory):
    """The output folder of one offline `build` (both tiers), shared by the tests that read generated artifacts."""
    out = tmp_path_factory.mktemp("out")
    assert cli.main(["build", "--repo", str(repo_path), "--out", str(out)]) == 0        # pinned commit: no --allow-drift needed
    return out


@pytest.fixture(scope="session")
def migration_tool():
    """tools/migrate_gold_to_silver.py as a module (the one-off gold migration; it is not part of the package)."""
    path = KIT / "tools" / "migrate_gold_to_silver.py"
    spec = importlib.util.spec_from_file_location("migrate_gold_to_silver", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["migrate_gold_to_silver"] = module               # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def silver_built(model, silver_spec, repo_path):
    b = silver.build_silver(model, silver_spec, repo_path)
    assert b.ok, b.errors
    return b


@pytest.fixture(scope="session")
def silver_rows(silver_built) -> dict:
    """Every silver table (and catalog) as a list of python rows, read back from the Arrow tables that get written."""
    return {name: tbl.to_pylist() for name, tbl in silver_built.tables.items()}
