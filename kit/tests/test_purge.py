"""Hard purge of a Fabric workspace against the in-process mock: items are permanently deleted, the recycle bin is emptied, the
unavoidable retention shell is renamed so its name is free, and anything that could not be purged is reported."""
import pytest

from contoso_onto import fabric_ops, oac
from mock_fabric import MockFabric

TOKENS = {oac.FABRIC_SCOPE: "tok-fabric", fabric_ops.STORAGE_SCOPE: "tok-storage", oac.PBI_SCOPE: "tok-pbi", fabric_ops.SEARCH_SCOPE: "tok-search"}


@pytest.fixture()
def mock():
    m = MockFabric()
    yield m
    m.close()


@pytest.fixture()
def ops(mock):
    logs = []
    fab = oac.Fabric(lambda s: TOKENS[s], api_base=f"{mock.base}/v1", pbi_base=f"{mock.base}/v1.0/myorg", log=logs.append, sleeper=lambda s: None)
    return fabric_ops.Ops(fab, lambda s: TOKENS[s], logs.append, sleeper=lambda s: None, dfs_base=f"{mock.base}/dfs")


def seeded(ops, name="w"):
    ws = ops.ensure_workspace(name, "contosofabricf4")["workspaceId"]
    lh = ops.ensure_lakehouse(ws, "L")["lakehouseId"]
    sm = ops.fab.create(ws, "semanticModels", {"displayName": "M", "definition": {"parts": []}})
    ont = ops.fab.create(ws, "ontologies", {"displayName": "O", "definition": {"parts": []}})
    return ws, lh, sm, ont


def test_hard_delete_uses_the_hard_flag_and_falls_back_where_soft_delete_does_not_apply(mock, ops):
    ws, lh, sm, ont = seeded(ops)
    assert ops.hard_delete_item(ws, {"id": lh, "type": "Lakehouse"}) == "hard"
    assert ops.hard_delete_item(ws, {"id": sm, "type": "SemanticModel"}) == "plain"                  # UnsupportedItemType: a plain DELETE is already permanent
    assert ops.hard_delete_item(ws, {"id": "no-such-item", "type": "Lakehouse"}) == "gone"          # already deleted: fine
    assert {h["type"] for h in mock.hard_deleted} == {"Lakehouse", "SemanticModel"} and not mock.recycle.get(ws)
    first = [r for r in mock.requests if r["method"] == "DELETE" and f"/items/{lh}" in r["path"]]
    assert first[0]["query"] == {"hardDelete": ["true"]}


def test_a_tenant_with_item_recovery_off_is_handled(mock, ops):
    mock.soft_delete_disabled = True
    ws, lh, sm, ont = seeded(ops)
    assert ops.hard_delete_item(ws, {"id": ont, "type": "Ontology"}) == "plain"                      # TenantSwitchDisabled: nothing is retained anyway
    assert mock.hard_deleted[-1]["type"] == "Ontology" and not mock.recycle.get(ws)


def test_a_plain_delete_lands_in_the_recycle_bin_and_can_be_purged_from_it(mock, ops):
    ws, lh, sm, ont = seeded(ops)
    ops.fab.call("DELETE", f"{ops.fab.api}/workspaces/{ws}/items/{ont}", ok=(200,))                  # what a portal delete would do
    rec = ops.list_recoverable(ws)
    assert [r["displayName"] for r in rec] == ["O"] and rec[0]["type"] == "Ontology"
    ops.purge_recoverable(ws, rec[0]["id"])
    assert ops.list_recoverable(ws) == [] and [i["displayName"] for i in mock.purged] == ["O"]
    ops.purge_recoverable(ws, rec[0]["id"])                                                           # already gone: fine


def test_purge_workspace_empties_everything_renames_and_deletes(mock, ops):
    ws, lh, sm, ont = seeded(ops)
    mock.recycle[ws] = {"old": {"id": "old", "displayName": "Earlier", "type": "Lakehouse", "workspaceId": ws}}
    rep = ops.purge_workspace(ws)
    assert rep["workspaceDeleted"] and rep["itemsLeft"] == [] and rep["recoverableLeft"] == [] and rep["errors"] == []
    assert [x.split(":")[0] for x in rep["itemsDeleted"]] == ["Ontology", "GraphModel", "SemanticModel", "Lakehouse"]      # dependants first (the ontology's graph model too); the SQL endpoint goes with its lakehouse
    assert rep["recoverablePurged"] == ["Lakehouse:Earlier"]
    assert rep["renamedTo"].startswith("zz-wiped-") and rep["renamedTo"].endswith(ws[:8])
    assert ws not in mock.workspaces and mock.deleted_workspaces[ws] == {"displayName": rep["renamedTo"], "itemsLeft": 0}


def test_a_deleted_workspace_reserves_its_name_unless_it_was_renamed(mock, ops):
    ws = ops.ensure_workspace("Demo", "contosofabricf4")["workspaceId"]
    ops.delete_workspace(ws)                                                                          # a plain delete: the shell keeps its name during retention
    with pytest.raises(oac.FabricError, match="WorkspaceNameAlreadyExists"):
        ops.ensure_workspace("Demo", "contosofabricf4")
    ws2 = ops.ensure_workspace("Demo2", "contosofabricf4")["workspaceId"]
    ops.purge_workspace(ws2)                                                                          # renamed first: the name is free again
    assert ops.ensure_workspace("Demo2", "contosofabricf4")["workspaceId"] != ws2


def test_what_cannot_be_purged_is_reported_and_the_workspace_is_still_deleted(mock, ops):
    ws, lh, sm, ont = seeded(ops)
    mock.stuck_item_ids.add(ont)
    rep = ops.purge_workspace(ws)
    assert rep["itemsLeft"] == ["Ontology:O"] and any("Ontology:O" in e and "403" in e for e in rep["errors"])
    assert rep["workspaceDeleted"] is True and mock.deleted_workspaces[ws]["itemsLeft"] == 1         # reported so wipe can say the content is not fully gone
    assert {h["type"] for h in mock.hard_deleted} == {"GraphModel", "SemanticModel", "Lakehouse"}      # everything else was still purged (the ontology's graph model too)


def test_a_failed_rename_is_reported_but_does_not_stop_the_purge(mock, ops, monkeypatch):
    ws, lh, sm, ont = seeded(ops)

    def no_rename(self, ws_id, name):
        raise oac.FabricError("PATCH -> 403")
    monkeypatch.setattr(fabric_ops.Ops, "rename_workspace", no_rename)
    rep = ops.purge_workspace(ws)
    assert rep["workspaceDeleted"] and rep["renamedTo"] is None and any("could not rename" in e for e in rep["errors"])


def test_an_unlistable_recycle_bin_counts_as_empty(mock, ops, monkeypatch):
    ws, lh, sm, ont = seeded(ops)

    def boom(self, ws_id, collection):
        if collection == "recoverableItems":
            raise oac.FabricError("GET -> 404")
        return oac.Fabric.list_items(self, ws_id, collection)
    monkeypatch.setattr(oac.Fabric, "list_items", boom)
    assert ops.list_recoverable(ws) == []
