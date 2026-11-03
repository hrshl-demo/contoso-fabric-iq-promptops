"""The adapter in tools/cd.py against the kit's in-process Fabric mock.

No tenant is involved: this proves that the calls into the kit (find the workspace, create the lakehouse, compare the lakehouse with the load plan,
build the graph) have the shape the kit expects. It needs the kit's requirements, so it is skipped where they are not installed; CI runs it in the
offline-build job.
"""
import json
import shutil
import sys

import pytest

pytest.importorskip("pandas")
pytest.importorskip("pyarrow")
pytest.importorskip("requests")

from tools import cd  # noqa: E402
from tools.promptlib import ROOT  # noqa: E402

KIT = ROOT / "kit"
ENV = {"CD_RESOURCE_GROUP": "rg-demo", "CD_CAPACITY": "contosofabricf4", "CD_WORKSPACE_DEV": "Demo-Dev", "CD_TIER": "smoke"}


def settings():
    return cd.load_settings(ENV, "dev", "smoke")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """One offline smoke build, shared by the tests (a copy is made for each)."""
    out = tmp_path_factory.mktemp("cd-built") / "out"
    cd.Live(state_dir=tmp_path_factory.mktemp("cd-state")).build_offline(settings(), out)
    return out


@pytest.fixture()
def world(monkeypatch, tmp_path, built):
    monkeypatch.syspath_prepend(str(KIT))
    monkeypatch.syspath_prepend(str(KIT / "tests"))
    from contoso_onto import fabric_ops, oac
    from mock_fabric import MockFabric
    tokens = {oac.FABRIC_SCOPE: "t", fabric_ops.STORAGE_SCOPE: "t", oac.PBI_SCOPE: "t", fabric_ops.SEARCH_SCOPE: "t"}
    mock = MockFabric()
    fab = oac.Fabric(lambda s: tokens[s], api_base=f"{mock.base}/v1", pbi_base=f"{mock.base}/v1.0/myorg", log=lambda m: None, sleeper=lambda s: None)
    ops = fabric_ops.Ops(fab, lambda s: tokens[s], lambda m: None, sleeper=lambda s: None, dfs_base=f"{mock.base}/dfs")
    live = cd.Live(state_dir=tmp_path / "state")
    cli, layers, runner, spec, load_model = live._kit_modules()
    monkeypatch.setattr(cli, "make_ops", lambda: ops)
    out = tmp_path / "out"
    shutil.copytree(built, out)
    yield mock, live, out, (cli, layers, runner, spec)
    mock.close()


def test_bootstrap_finds_the_workspace_and_creates_the_lakehouse_once(world):
    mock, live, out, (cli, *_rest) = world
    mock.workspaces["w-1"] = {"id": "w-1", "displayName": "Demo-Dev", "capacityId": "cap-guid-1"}
    first = live.bootstrap(settings(), out)
    assert first["workspaceId"] == "w-1" and first["lakehouseName"] == "ContosoBankLH" and first["sqlDatabase"]
    state = cli.read_state(out)
    assert state["workspaceId"] == "w-1" and state["workspaceName"] == "Demo-Dev" and state["lakehouseId"] == first["lakehouseId"]
    assert state["capacityName"] == "contosofabricf4" and state["resourceGroup"] == "rg-demo" and state["sqlEndpoint"]
    assert live.bootstrap(settings(), out)["lakehouseId"] == first["lakehouseId"]
    assert len(mock.items[("w-1", "lakehouses")]) == 1


def test_bootstrap_says_what_to_fix(world):
    mock, live, out, _ = world
    with pytest.raises(cd.CdError, match="was not found.*add the identity as Admin"):
        live.bootstrap(settings(), out)
    mock.workspaces["w-1"] = {"id": "w-1", "displayName": "Demo-Dev", "capacityId": None}
    with pytest.raises(cd.CdError, match="not assigned to a capacity"):
        live.bootstrap(settings(), out)


def test_bootstrap_refuses_a_workspace_that_bills_to_another_capacity(world):
    """The pipeline pauses only the configured capacity; a workspace on another one would keep billing unseen."""
    mock, live, out, _ = world
    mock.workspaces["w-1"] = {"id": "w-1", "displayName": "Demo-Dev", "capacityId": "some-other-capacity-guid"}
    with pytest.raises(cd.CdError, match="not on capacity 'contosofabricf4'.*would leave the capacity that really bills running"):
        live.bootstrap(settings(), out)
    assert not mock.items.get(("w-1", "lakehouses"))                                              # nothing was created in it


def test_bootstrap_needs_the_identity_to_see_the_capacity(world):
    mock, live, out, _ = world
    mock.workspaces["w-1"] = {"id": "w-1", "displayName": "Demo-Dev", "capacityId": "cap-guid-1"}
    other = cd.load_settings({**ENV, "CD_CAPACITY": "someothercap"}, "dev", "smoke")
    with pytest.raises(cd.CdError, match="cannot see capacity 'someothercap'.*contributor of the capacity.*SETUP.md step 3"):
        live.bootstrap(other, out)


def test_missing_tables_compares_the_lakehouse_with_the_plan(world):
    mock, live, out, (cli, layers, *_rest) = world
    mock.workspaces["w-1"] = {"id": "w-1", "displayName": "Demo-Dev", "capacityId": "cap-guid-1"}
    lakehouse_id = live.bootstrap(settings(), out)["lakehouseId"]
    wanted = layers.load_plan(out).all_names()
    assert wanted and sorted(live.missing_tables(out)) == sorted(wanted)
    mock.tables[lakehouse_id] = list(wanted[:-1])
    assert live.missing_tables(out) == [wanted[-1]]
    mock.tables[lakehouse_id] = list(wanted)
    assert live.missing_tables(out) == []


def test_graph_hands_the_kit_a_valid_spec_and_state(world, monkeypatch):
    mock, live, out, (cli, layers, runner, spec) = world
    mock.workspaces["w-1"] = {"id": "w-1", "displayName": "Demo-Dev", "capacityId": "cap-guid-1"}
    live.bootstrap(settings(), out)
    seen = {}

    def fake(ops, sp, ws, lh, out_dir, say, wait_s=None, timeout_s=None):
        seen.update(sp=sp, ws=ws, lh=lh, out=out_dir, wait_s=wait_s, timeout_s=timeout_s)
        return {"action": "applied"}
    monkeypatch.setattr(runner, "materialize_graph", fake)
    assert live.graph(settings(), out) == {"action": "applied"}
    assert seen["sp"]["ontology"]["name"] == "ContosoBankOntology" and seen["sp"]["lakehouse"]["name"] == "ContosoBankLH" and seen["sp"]["tables"]
    assert seen["ws"] == {"workspaceId": "w-1", "workspaceName": "Demo-Dev"} and seen["lh"]["lakehouseId"] and seen["out"] == out
    assert json.loads((out / ".state.json").read_text(encoding="utf-8"))["workspaceId"] == "w-1"
    assert (seen["wait_s"], seen["timeout_s"]) == (300.0, 900.0)                                  # no deadline: the kit's own defaults


@pytest.mark.parametrize("budget, wait_s, timeout_s", [(5000, 300.0, 900.0), (1200, 300.0, 840.0), (100, 25.0, 70.0), (60, 15.0, 42.0), (10, 10.0, 30.0)])
def test_the_graph_waits_are_cut_to_the_time_left_before_the_deadline(world, monkeypatch, budget, wait_s, timeout_s):
    mock, live, out, (cli, layers, runner, spec) = world
    mock.workspaces["w-1"] = {"id": "w-1", "displayName": "Demo-Dev", "capacityId": "cap-guid-1"}
    live.bootstrap(settings(), out)
    seen = {}
    monkeypatch.setattr(runner, "materialize_graph", lambda *a, **kw: seen.update(kw) or {})
    live.graph(settings(), out, budget=budget)
    assert (seen["wait_s"], seen["timeout_s"]) == (wait_s, timeout_s)


def test_the_kit_commands_named_by_the_orchestrator_exist():
    """deploy() calls these kit sub-commands with these flags; fail early if the vendored kit renames one."""
    sys.path.insert(0, str(KIT))
    try:
        import io
        from contextlib import redirect_stdout, redirect_stderr
        from contoso_onto import cli
        for args in (["capacity", "status", "--help"], ["load", "--help"], ["deploy", "--help"], ["verify", "--help"]):
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(buf), pytest.raises(SystemExit) as ex:
                cli.main(args)
            assert ex.value.code == 0, args
        buf = io.StringIO()
        with redirect_stdout(buf), pytest.raises(SystemExit):
            cli.main(["deploy", "--help"])
        for flag in ("--execute", "--replace", "--tier", "--out"):
            assert flag in buf.getvalue(), flag
        buf = io.StringIO()
        with redirect_stdout(buf), pytest.raises(SystemExit):
            cli.main(["capacity", "--help"])
        assert "suspend" in buf.getvalue() and "resume" in buf.getvalue()
    finally:
        sys.path.remove(str(KIT))
