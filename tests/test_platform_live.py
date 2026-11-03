"""The platform adapter in tools/platform.py against the kit's in-process Fabric mock.

No tenant is involved. It proves that the Fabric calls the platform makes (create the workspace on the capacity, grant roles, create the lakehouse,
probe the ontology gate and the permission to create workspaces, delete workspaces) have the shape the kit and the service expect, and that the
probes clean up after themselves. It needs the kit's requirements, so it is skipped where they are not installed; CI runs it in the offline-build job.
"""
import sys

import pytest

pytest.importorskip("pandas")
pytest.importorskip("pyarrow")
pytest.importorskip("requests")

from tools import cd, platform as pf  # noqa: E402
from tools.promptlib import ROOT  # noqa: E402

KIT = ROOT / "kit"
ADMIN = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
UPN = "admin@demo.test"
PIPE = "bbbbbbbb-1111-4111-8111-bbbbbbbbbbbb"
CONS = "cccccccc-1111-4111-8111-cccccccccccc"
ENV = {"CD_RESOURCE_GROUP": "rg-demo", "CD_CAPACITY": "contosofabricf4", "CD_WORKSPACE_DEV": "Demo-Dev", "CD_WORKSPACE_PROD": "Demo-Prod", "CD_TIER": "smoke",
       "CD_ADMIN_OBJECT_IDS": ADMIN, "CD_ADMIN_UPNS": UPN, "CD_PIPELINE_OBJECT_ID": PIPE, "CD_CONSUMER_OBJECT_IDS": CONS}


def settings(**over):
    return pf.load_platform_settings({**ENV, **over}, "dev", "smoke")


@pytest.fixture()
def world(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(KIT))
    monkeypatch.syspath_prepend(str(KIT / "tests"))
    from contoso_onto import fabric_ops, oac
    from mock_fabric import MockFabric
    tokens = {oac.FABRIC_SCOPE: "t", fabric_ops.STORAGE_SCOPE: "t", oac.PBI_SCOPE: "t", fabric_ops.SEARCH_SCOPE: "t"}
    mock = MockFabric()
    fab = oac.Fabric(lambda s: tokens[s], api_base=f"{mock.base}/v1", pbi_base=f"{mock.base}/v1.0/myorg", log=lambda m: None, sleeper=lambda s: None)
    ops = fabric_ops.Ops(fab, lambda s: tokens[s], lambda m: None, sleeper=lambda s: None, dfs_base=f"{mock.base}/dfs")
    live = pf.Live(state_dir=tmp_path / "state", sleeper=lambda s: None)
    cli, *_rest = live._kit_modules()
    monkeypatch.setattr(cli, "make_ops", lambda: ops)
    out = tmp_path / "out"
    out.mkdir()
    yield mock, live, ops, out, cli
    mock.close()


def fault(mock, match, method, status, count=1):
    mock.faults.append({"match": match, "method": method, "status": status, "count": count})


def ready(world):
    """A workspace and a lakehouse, as `build` leaves them before it checks the gate."""
    mock, live, ops, out, cli = world
    return live.workspace_and_lakehouse(settings(), out)


# ---- workspace and lakehouse ----------------------------------------------------------------------------------

def test_the_workspace_is_created_on_the_capacity_with_the_roles_and_a_lakehouse(world, monkeypatch):
    mock, live, ops, out, cli = world
    granted = []
    real = ops.add_members
    monkeypatch.setattr(ops, "add_members", lambda ws, members: (granted.append((ws, members)), real(ws, members))[1])
    first = live.workspace_and_lakehouse(settings(), out)
    assert first["created"] is True and first["capacityId"] == "cap-guid-1" and first["lakehouseName"] == "ContosoBankLH" and first["sqlDatabase"]
    ws = mock.workspaces[first["workspaceId"]]
    assert ws["displayName"] == "Demo-Dev" and ws["capacityId"] == "cap-guid-1" and live.capacity_fabric_id == "cap-guid-1"
    assert granted == [(first["workspaceId"], [{"id": ADMIN, "type": "User", "role": "Admin"}, {"id": CONS, "type": "ServicePrincipal", "role": "Contributor"}])]
    state = cli.read_state(out)
    assert state["workspaceId"] == first["workspaceId"] and state["workspaceName"] == "Demo-Dev" and state["lakehouseId"] == first["lakehouseId"]
    assert state["capacityName"] == "contosofabricf4" and state["resourceGroup"] == "rg-demo" and state["sqlEndpoint"]


def test_running_it_again_changes_nothing(world):
    mock, live, ops, out, cli = world
    first = live.workspace_and_lakehouse(settings(), out)
    again = live.workspace_and_lakehouse(settings(), out)
    assert again["created"] is False and again["workspaceId"] == first["workspaceId"] and again["lakehouseId"] == first["lakehouseId"]
    assert len(mock.workspaces) == 1 and len(mock.items[(first["workspaceId"], "lakehouses")]) == 1


def test_a_workspace_with_no_roles_to_grant_is_still_created(world, monkeypatch):
    mock, live, ops, out, cli = world
    monkeypatch.setattr(ops, "add_members", lambda ws, members: pytest.fail("no roles were configured"))
    s = pf.load_platform_settings({**ENV, "CD_ADMIN_OBJECT_IDS": ADMIN, "CD_CONSUMER_OBJECT_IDS": ""}, "dev", "smoke")
    monkeypatch.setattr(pf.PlatformSettings, "workspace_members", lambda self: [])
    assert live.workspace_and_lakehouse(s, out)["created"] is True


@pytest.mark.parametrize("fix, message", [
    (lambda mock: setattr(mock, "capacity_state", "Paused"), "is Paused; resume it first"),
    (lambda mock: setattr(mock, "capacity_sku", "F2"), "configured for F4"),
    (lambda mock: mock.workspaces.update({"w-1": {"id": "w-1", "displayName": "Demo-Dev", "capacityId": "some-other-capacity"}}), "different capacity"),
])
def test_it_refuses_a_capacity_that_is_not_the_one_that_will_be_paused(world, fix, message):
    mock, live, ops, out, cli = world
    fix(mock)
    with pytest.raises(Exception, match=message):
        live.workspace_and_lakehouse(settings(), out)
    assert not mock.items.get(("w-1", "lakehouses"))


@pytest.mark.parametrize("code", [401, 403])
def test_a_refused_workspace_creation_says_which_two_things_to_check(world, code):
    """Creating a workspace needs the tenant setting for the group AND the caller to administer the capacity; Fabric answers 403 for both, so the message names both."""
    mock, live, ops, out, cli = world
    fault(mock, r"/workspaces$", "POST", code)
    with pytest.raises(cd.CdError) as ex:
        live.workspace_and_lakehouse(settings(), out)
    text = str(ex.value)
    assert f"refused to create the workspace ({code})" in text                                              # the real status, not a fixed 403
    assert "Service principals can create workspaces, connections, and deployment pipelines" in text and "contoso-promptops-fabric-apps" in text
    assert "not yet an administrator of the capacity" in text and "The capacity is paused next" in text and "up to 15 minutes" in text
    assert not mock.workspaces


def test_another_failure_while_creating_the_workspace_is_not_blamed_on_the_tenant_setting(world):
    mock, live, ops, out, cli = world
    fault(mock, r"/workspaces$", "POST", 400)
    with pytest.raises(Exception) as ex:
        live.workspace_and_lakehouse(settings(), out)
    assert "Service principals can create workspaces" not in str(ex.value)


# ---- the ontology gate ----------------------------------------------------------------------------------------

def test_a_closed_gate_is_seen_as_closed_and_nothing_is_left_behind(world):
    mock, live, ops, out, cli = world
    ws = ready(world)["workspaceId"]
    mock.ontology_forbidden = True
    state = live.gate_state(settings(), out)
    assert state.status == "closed" and "403 FeatureNotAvailable" in state.detail
    assert not mock.items.get((ws, "ontologies")) and not mock.items.get((ws, "graphModels"))


def test_an_open_gate_creates_and_removes_the_probe_with_its_graph_model(world):
    mock, live, ops, out, cli = world
    ws = ready(world)["workspaceId"]
    state = live.gate_state(settings(), out)
    assert state.status == "open" and "created and deleted" in state.detail and not state.warning
    assert not mock.items.get((ws, "ontologies"))
    assert not mock.items.get((ws, "graphModels")), "the graph model Fabric creates next to an ontology must not be left behind"
    assert [i["displayName"] for i in mock.hard_deleted if i["type"] in ("Ontology", "GraphModel")][:1] == [pf.GATE_PROBE_ITEM]
    assert live.gate_state(settings(), out).status == "open"                                           # and it can be asked again and again


def test_a_leftover_probe_from_an_earlier_run_is_removed_before_the_next_one(world):
    """A leftover would make the next create answer 409 (name in use) and turn a clear gate answer into 'unknown'."""
    mock, live, ops, out, cli = world
    ws = ready(world)["workspaceId"]
    ops.fab.create(ws, "ontologies", {"displayName": pf.GATE_PROBE_ITEM})                              # the debris of a run that could not clean up
    assert mock.items.get((ws, "ontologies")) and mock.items.get((ws, "graphModels"))
    state = live.gate_state(settings(), out)
    assert state.status == "open" and not state.warning
    assert not mock.items.get((ws, "ontologies")) and not mock.items.get((ws, "graphModels"))


def test_a_closed_gate_still_cleans_up_an_old_leftover(world):
    mock, live, ops, out, cli = world
    ws = ready(world)["workspaceId"]
    ops.fab.create(ws, "ontologies", {"displayName": pf.GATE_PROBE_ITEM})
    mock.ontology_forbidden = True
    assert live.gate_state(settings(), out).status == "closed"
    assert not mock.items.get((ws, "ontologies"))


def test_a_gate_that_refuses_the_empty_definition_is_open(world):
    mock, live, ops, out, cli = world
    ready(world)
    mock.endpoint_lag_creates = 1                                                                      # the mock's 400 on an ontology create
    state = live.gate_state(settings(), out)
    assert state.status == "open" and "refused the empty definition" in state.detail


def test_any_other_answer_is_unknown_and_says_what_was_said(world):
    mock, live, ops, out, cli = world
    ready(world)
    fault(mock, r"/ontologies$", "POST", 403)
    state = live.gate_state(settings(), out)
    assert state.status == "unknown" and "Injected" in state.detail


def test_a_leftover_that_cannot_be_removed_stops_the_probe_and_is_reported(world, monkeypatch):
    """With the old debris still there the create would answer 409 and say nothing about the gate: the probe is not made, and the debris is reported."""
    mock, live, ops, out, cli = world
    ws = ready(world)["workspaceId"]
    ops.fab.create(ws, "ontologies", {"displayName": pf.GATE_PROBE_ITEM})
    posts, real_create = [], ops.fab.create
    monkeypatch.setattr(ops.fab, "create", lambda *a, **k: posts.append(a) or real_create(*a, **k))
    monkeypatch.setattr(ops, "hard_delete_item", lambda w, item: (_ for _ in ()).throw(cd.CdError("403 InsufficientWorkspaceRole")))
    state = live.gate_state(settings(), out)
    assert state.status == "unknown" and "was not probed" in state.detail
    assert pf.GATE_PROBE_ITEM in state.warning and "delete by hand" in state.warning and posts == []


def test_a_probe_that_cannot_be_cleaned_up_is_tried_three_times_then_reported(world, monkeypatch):
    mock, live, ops, out, cli = world
    ready(world)
    attempts = []

    def refuse(ws, item):
        attempts.append(item["displayName"])
        raise cd.CdError("403 InsufficientWorkspaceRole")
    monkeypatch.setattr(ops, "hard_delete_item", refuse)
    state = live.gate_state(settings(), out)
    assert state.status == "open" and "could not be fully removed" in state.detail                    # the gate answer itself is still true
    assert pf.GATE_PROBE_ITEM in state.warning and "delete by hand" in state.warning
    assert len(attempts) >= pf.PROBE_CLEANUP_TRIES


def test_a_cleanup_that_works_on_the_second_try_leaves_no_warning(world, monkeypatch):
    mock, live, ops, out, cli = world
    ready(world)
    real, calls = ops.hard_delete_item, []

    def flaky(ws, item):
        calls.append(item["id"])
        if len(calls) == 1:
            raise cd.CdError("503 briefly")
        return real(ws, item)
    monkeypatch.setattr(ops, "hard_delete_item", flaky)
    state = live.gate_state(settings(), out)
    assert state.status == "open" and not state.warning and "created and deleted" in state.detail


# ---- the two doors that can be checked before a capacity exists ----------------------------------------------

def probe_names(mock):
    return [w["displayName"] for w in mock.workspaces.values()]


def test_both_doors_are_checked_and_the_probe_workspace_is_created_and_deleted(world):
    """The second tenant setting is proven by creating an empty workspace without a capacity and deleting it at once."""
    mock, live, ops, out, cli = world
    api, create = live.probe_fabric(settings())
    assert api.status == "ok" and create.status == "ok" and "created and deleted" in create.detail
    assert not mock.workspaces                                                                            # nothing is left in Fabric
    assert [s["displayName"] for s in mock.deleted_workspaces.values()][0].startswith(pf.PROBE_WORKSPACE_PREFIX)
    assert all(w.get("capacityId") is None for w in mock.workspaces.values())                              # no capacity was involved, so nothing could bill


@pytest.mark.parametrize("code", [401, 403])
def test_a_refused_probe_workspace_means_the_second_tenant_setting_is_off(world, code):
    """The first live run: Fabric answered 401 to the pipeline's create-workspace call at step 8, with the F4 already running."""
    mock, live, ops, out, cli = world
    fault(mock, r"/workspaces$", "POST", code)
    api, create = live.probe_fabric(settings())
    assert api.status == "ok" and create.status == "closed" and str(code) in create.detail
    assert "Service principals can create workspaces, connections, and deployment pipelines" in create.guide and "contoso-promptops-fabric-apps" in create.guide
    assert not mock.workspaces


@pytest.mark.parametrize("code", [400, 404, 500])
def test_a_probe_that_fails_for_another_reason_is_not_called_closed(world, code):
    mock, live, ops, out, cli = world
    fault(mock, r"/workspaces$", "POST", code)
    _api, create = live.probe_fabric(settings())
    assert create.status == "skipped" and "could not be proven" in create.detail and "step 8" in create.detail


def test_a_probe_workspace_that_cannot_be_deleted_is_reported_by_name(world):
    mock, live, ops, out, cli = world
    fault(mock, r"/workspaces/[^/]+$", "DELETE", 500, count=9)
    _api, create = live.probe_fabric(settings())
    assert create.status == "error" and pf.PROBE_WORKSPACE_PREFIX in create.detail and "delete it by hand" in create.detail


def test_a_leftover_probe_workspace_from_an_earlier_run_is_swept_and_other_workspaces_are_left_alone(world):
    mock, live, ops, out, cli = world
    for name in (pf.PROBE_WORKSPACE_PREFIX + "deadbeef", "Contoso-PromptOps-Dev"):
        ops.fab.call("POST", f"{ops.fab.api}/workspaces", json={"displayName": name})
    _api, create = live.probe_fabric(settings())
    assert create.status == "ok" and probe_names(mock) == ["Contoso-PromptOps-Dev"]


@pytest.mark.parametrize("code, status", [(401, "closed"), (403, "closed"), (500, "error"), (404, "error")])
def test_a_refused_listing_means_the_api_setting_is_off(world, code, status):
    mock, live, ops, out, cli = world
    fault(mock, r"/workspaces$", "GET", code)
    checks = live.probe_fabric(settings())
    assert len(checks) == 1 and checks[0].status == status                                                # without API access the creation is not even tried
    assert ("Service principals can call Fabric public APIs" in checks[0].guide) is (status == "closed")
    assert not mock.workspaces


# ---- destroying -----------------------------------------------------------------------------------------------

def test_only_the_named_workspaces_are_found(world):
    mock, live, ops, out, cli = world
    for name in ("Demo-Dev", "Demo-Prod", "Somebody-Elses"):
        ops.fab.call("POST", f"{ops.fab.api}/workspaces", json={"displayName": name})
    found = live.find_workspaces(settings(), ["Demo-Dev", "Demo-Prod"])
    assert sorted(n for _i, n in found) == ["Demo-Dev", "Demo-Prod"] and live.find_workspaces(settings(), ["Nope"]) == []


def test_a_purge_deletes_the_items_frees_the_name_and_leaves_nothing_recoverable(world):
    """The kit's own purge, proven live by its wipe: a bare delete would leave the items recoverable and the name reserved for the retention period."""
    mock, live, ops, out, cli = world
    ready_ws = ready(world)
    ws = ready_ws["workspaceId"]
    rep = live.purge_workspace(settings(), ws)
    assert rep["workspaceDeleted"] is True and not rep["errors"] and not rep["itemsLeft"] and not rep["recoverableLeft"]
    assert any("ContosoBankLH" in x for x in rep["itemsDeleted"])
    assert ws not in mock.workspaces and not mock.items.get((ws, "lakehouses"))
    assert rep["renamedTo"] and rep["renamedTo"].startswith("zz-wiped-")                                 # the name is free: the next build can use it again
    assert all(w["displayName"] != "Demo-Dev" for w in mock.workspaces.values())
    again = live.workspace_and_lakehouse(settings(), out)                                                # and a rebuild with the same name works
    assert again["created"] is True and mock.workspaces[again["workspaceId"]]["displayName"] == "Demo-Dev"


# ---- inventory and geography ------------------------------------------------------------------------------------

def test_the_inventory_reports_what_exists(world, monkeypatch):
    mock, live, ops, out, cli = world
    monkeypatch.setattr(live, "storage_exists", lambda s, name: True)
    monkeypatch.setattr(live, "capacity", lambda base, action, timeout=None: {"exists": True, "state": "Paused", "sku": "F4"})
    assert live.inventory(settings(), "stx") == {"capacity": {"exists": True, "state": "Paused", "sku": "F4"}, "storage": True, "workspace": False, "lakehouse": None}
    ready(world)
    assert live.inventory(settings(), "stx")["workspace"] is True and live.inventory(settings(), "stx")["lakehouse"] is True
    monkeypatch.setattr(live, "capacity", lambda base, action, timeout=None: {"exists": False})
    info = live.inventory(settings(), None)
    assert info["storage"] is None and info["workspace"] is None and info["capacity"]["exists"] is False


def test_a_region_outside_the_us_and_eu_needs_the_third_setting(world):
    mock, live, ops, out, cli = world
    assert live.needs_cross_geo(settings()) is False
    assert live.needs_cross_geo(settings(CD_LOCATION="southindia")) is True


def test_the_kit_commands_the_platform_names_exist():
    """The platform calls these kit sub-commands with these flags; fail early if the vendored kit renames one."""
    sys.path.insert(0, str(KIT))
    try:
        import io
        from contextlib import redirect_stdout, redirect_stderr
        from contoso_onto import cli, fabric_ops, runner
        for name in ("ensure_workspace", "add_members", "ensure_lakehouse", "hard_delete_item", "list_items", "find_workspace", "find_capacity", "purge_workspace"):
            assert callable(getattr(fabric_ops.Ops, name)), name
        for name in ("ontology_gate", "service_error_text", "needs_cross_geo"):
            assert callable(getattr(runner, name)), name
        for name in ("write_state", "read_state", "load_config", "make_ops"):
            assert callable(getattr(cli, name)), name
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf), pytest.raises(SystemExit) as ex:
            cli.main(["capacity", "--help"])
        assert ex.value.code == 0 and "delete" in buf.getvalue()
        buf = io.StringIO()
        with redirect_stdout(buf), pytest.raises(SystemExit):
            cli.main(["load", "--help"])
        assert "--execute" in buf.getvalue() and "--out" in buf.getvalue()
    finally:
        sys.path.remove(str(KIT))
