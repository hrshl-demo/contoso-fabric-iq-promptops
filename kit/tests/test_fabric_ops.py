"""Fabric orchestration against the in-process mock: workspace, lakehouse, OneLake upload, Load Table, deploy, verify."""
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from contoso_onto import fabric_ops, gold, oac, spec as S
from mock_fabric import MockFabric, VirtualTime

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


def test_workspace_and_lakehouse_provisioning(mock, ops):
    ws = ops.ensure_workspace("Contoso-Bank-Ontology-Demo", "contosofabricf4", "demo")
    assert ws["created"] is True and mock.workspaces[ws["workspaceId"]]["capacityId"] == "cap-guid-1"
    again = ops.ensure_workspace("Contoso-Bank-Ontology-Demo", "contosofabricf4")
    assert again["workspaceId"] == ws["workspaceId"] and again["created"] is False and len(mock.workspaces) == 1           # idempotent
    lh = ops.ensure_lakehouse(ws["workspaceId"], "ContosoBankLH")
    assert lh["sqlEndpoint"].endswith("fabric.microsoft.com") and lh["created"] is True
    assert ops.ensure_lakehouse(ws["workspaceId"], "ContosoBankLH")["created"] is False
    created = next(r for r in mock.requests if r["method"] == "POST" and r["path"].endswith("/lakehouses"))
    assert "creationPayload" not in json.loads(created["body"])                                 # plain lakehouse: the API only takes enableSchemas=true or no payload
    assert all(r["auth"] == "Bearer tok-fabric" for r in mock.requests if r["path"].startswith("/v1/"))


def test_existing_workspace_is_adopted_on_our_capacity_or_when_unassigned_but_never_moved(mock, ops):
    mock.workspaces["w-1"] = {"id": "w-1", "displayName": "Mine", "capacityId": None}
    assert ops.ensure_workspace("Mine", "contosofabricf4")["created"] is False and mock.workspaces["w-1"]["capacityId"] == "cap-guid-1"      # was unassigned
    mock.workspaces["w-2"] = {"id": "w-2", "displayName": "Theirs", "capacityId": "some-other-capacity"}
    with pytest.raises(oac.FabricError, match="will not move it"):
        ops.ensure_workspace("Theirs", "contosofabricf4")
    assert mock.workspaces["w-2"]["capacityId"] == "some-other-capacity"


def test_delete_helpers_and_tenant_settings(mock, ops):
    ws = ops.ensure_workspace("w", "contosofabricf4")["workspaceId"]
    lh = ops.ensure_lakehouse(ws, "L")["lakehouseId"]
    ops.delete_item(ws, "lakehouses", lh)
    ops.delete_item(ws, "lakehouses", lh)                                                       # already gone: fine
    assert ops.fab.find(ws, "lakehouses", "L") is None
    assert ops.workspace_exists(ws)
    ops.delete_workspace(ws)
    assert not ops.workspace_exists(ws)
    assert ops.tenant_settings() is None                                                        # 403 for non-admins: reported as unknown
    mock.tenant_settings = [{"settingName": "OntologyItem", "title": "Ontology item (preview)", "enabled": True}, {"settingName": "Other", "title": "Unrelated", "enabled": True}]
    assert ops.tenant_settings() == [{"name": "OntologyItem", "title": "Ontology item (preview)", "enabled": True}]


def test_inactive_capacity_is_refused(ops, mock):
    mock.capacity_state = "Paused"
    with pytest.raises(oac.FabricError, match="resume"):
        ops.ensure_workspace("x", "contosofabricf4")


def test_fabric_must_report_the_capacity_as_f4_before_a_workspace_is_put_on_it(ops, mock):
    mock.capacity_sku = "F8"
    with pytest.raises(fabric_ops.FabricCapacitySkuMismatch, match="reports capacity contosofabricf4 as F8, but this build is configured for F4"):
        ops.ensure_workspace("w", "contosofabricf4", sku="F4")
    assert issubclass(fabric_ops.FabricCapacitySkuMismatch, oac.FabricError) and not mock.workspaces      # nothing was created on the wrong size
    mock.capacity_sku = "F4"
    ws = ops.ensure_workspace("w", "contosofabricf4", sku="F4")
    assert ws["capacitySku"] == "F4" and ws["capacityState"] == "Active"


def test_a_brand_new_capacity_that_fabric_lists_late_is_waited_for(ops, mock):
    mock.capacity_hidden_polls = 3                                                              # Azure created it; Fabric lists it a few polls later
    ws = ops.ensure_workspace("w", "contosofabricf4", sku="F4")
    assert ws["capacityId"] == "cap-guid-1" and mock.workspaces[ws["workspaceId"]]["capacityId"] == "cap-guid-1"
    assert len([r for r in mock.requests if r["path"] == "/v1/capacities"]) == 4                # 3 empty answers, then the capacity


def test_a_capacity_that_never_shows_up_fails_after_the_wait_and_other_lookups_do_not_wait(ops, mock):
    mock.capacity_hidden_polls = 10_000
    with pytest.raises(oac.FabricError, match="not visible to you"):
        ops.ensure_workspace("w", "contosofabricf4", sku="F4")
    assert 20 < len([r for r in mock.requests if r["path"] == "/v1/capacities"]) < 40           # a bounded wait: about 4 minutes of polling
    mock.requests.clear()
    with pytest.raises(oac.FabricError, match="not visible to you"):
        ops.find_capacity("contosofabricf4")                                                     # diagnose / developer lookups fail at once
    assert len(mock.requests) == 1


def test_a_workspace_that_does_not_end_up_on_the_capacity_is_an_error(ops, mock):
    mock.capacity_assignment_fails = True
    with pytest.raises(oac.FabricError, match="is on capacity none, not contosofabricf4"):
        ops.ensure_workspace("w", "contosofabricf4", sku="F4")
    mock.workspaces["w-1"] = {"id": "w-1", "displayName": "Mine", "capacityId": None}           # an unassigned workspace that cannot be assigned
    with pytest.raises(oac.FabricError, match="is on capacity none"):
        ops.ensure_workspace("Mine", "contosofabricf4", sku="F4")


def test_unknown_capacity_lists_what_is_visible(ops):
    with pytest.raises(oac.FabricError, match="contosofabricf4"):
        ops.find_capacity("nope")


def test_sql_endpoint_wait_loop(mock, ops):
    mock.sql_ready_after = 3
    ws = ops.ensure_workspace("w", "contosofabricf4")
    lh = ops.ensure_lakehouse(ws["workspaceId"], "L")
    assert lh["sqlEndpoint"] and mock._lh_polls


def test_load_gold_uploads_parquet_then_loads_managed_tables(mock, ops, tmp_path):
    for name in ("dim_customer", "dim_account"):                                       # the lakehouse (physical) names the gold files carry
        pq.write_table(pa.table({"id": ["a", "b"], "amount_inr": [1.5, 2.5]}), tmp_path / f"{name}.parquet")
    ws = ops.ensure_workspace("w", "contosofabricf4")["workspaceId"]
    lh = ops.ensure_lakehouse(ws, "L")["lakehouseId"]
    res = ops.load_gold(tmp_path, ws, lh, workers=1)                                  # serial: the request order below is deterministic
    assert res == {"loaded": ["dim_account", "dim_customer"], "missingAfterLoad": []}
    key = f"{ws}/{lh}/Files/staging/dim_customer.parquet"
    assert pq.read_table(__import__("io").BytesIO(mock.files[key])).num_rows == 2
    dfs = [r for r in mock.requests if r["path"].startswith("/dfs/")]
    assert {r["auth"] for r in dfs} == {"Bearer tok-storage"}                       # storage audience, not the Fabric one
    assert [r["method"] for r in dfs[:3]] == ["PUT", "PATCH", "PATCH"] and dfs[2]["query"]["action"] == ["flush"]
    assert mock.table_loads[0] == {"table": "dim_account", "relativePath": "Files/staging/dim_account.parquet", "pathType": "File", "mode": "Overwrite",
                                   "recursive": False, "formatOptions": {"format": "Parquet"}}


def test_load_gold_flags_tables_that_did_not_register(mock, ops, tmp_path):
    pq.write_table(pa.table({"id": ["a"]}), tmp_path / "t.parquet")
    ws = ops.ensure_workspace("w", "contosofabricf4")["workspaceId"]
    lh = ops.ensure_lakehouse(ws, "L")["lakehouseId"]
    ops.load_table = lambda *a, **k: None                                           # load "succeeds" but nothing is registered
    assert ops.load_gold(tmp_path, ws, lh)["missingAfterLoad"] == ["t"]


def write_tables(folder, n):
    for i in range(n):
        pq.write_table(pa.table({"id": ["a", "b"]}), folder / f"t{i:02d}.parquet")
    return [f"t{i:02d}" for i in range(n)]


def test_an_operation_that_succeeds_without_a_result_is_not_an_error(mock, ops, tmp_path):
    """Load Table succeeds with no result: Fabric answers 400 OperationHasNoResult for it (first live build)."""
    names = write_tables(tmp_path, 2)
    ws = ops.ensure_workspace("w", "contosofabricf4")["workspaceId"]
    lh = ops.ensure_lakehouse(ws, "L")["lakehouseId"]
    assert ops.load_gold(tmp_path, ws, lh, workers=1) == {"loaded": names, "missingAfterLoad": []}
    mock.result_error = "SomethingElseWentWrong"                                      # any other 400 on /result is a real error
    with pytest.raises(oac.FabricError, match="SomethingElseWentWrong"):
        ops.load_table(ws, lh, "t00", "Files/staging/t00.parquet")


def test_tables_load_in_parallel_and_the_result_is_complete_and_ordered(mock, ops, tmp_path):
    names = write_tables(tmp_path, 12)
    mock.load_delay = 0.05                                                            # long enough for the loads to overlap
    ws = ops.ensure_workspace("w", "contosofabricf4")["workspaceId"]
    lh = ops.ensure_lakehouse(ws, "L")["lakehouseId"]
    res = ops.load_gold(tmp_path, ws, lh)
    assert res == {"loaded": names, "missingAfterLoad": []}
    assert 1 < mock.max_inflight_loads <= 4                                           # concurrent, and never more than the worker cap
    assert sorted(t["table"] for t in mock.table_loads) == names and len(mock.table_loads) == 12


def test_a_table_that_collides_while_others_load_is_retried_on_its_own(mock, ops, tmp_path):
    names = write_tables(tmp_path, 6)
    mock.load_failures = {"t03": 1}                                                   # the first attempt is refused, as if the lakehouse were busy
    ws = ops.ensure_workspace("w", "contosofabricf4")["workspaceId"]
    lh = ops.ensure_lakehouse(ws, "L")["lakehouseId"]
    res = ops.load_gold(tmp_path, ws, lh)
    assert res == {"loaded": names, "missingAfterLoad": []}
    assert len([r for r in mock.requests if r["path"].endswith("/tables/t03/load")]) == 2   # refused once, then loaded serially
    mock.load_failures = {"t01": 99}                                                  # a table that keeps failing: the serial retry raises with its own message
    with pytest.raises(oac.FabricError, match="ItemOperationInProgress"):
        ops.load_gold(tmp_path, ws, lh)


@pytest.fixture()
def parts(model):
    sp = S.make_spec(model, "core")
    ctx = oac.make_ctx(workspace_id="w1", lakehouse_id="l1", workspace_name="W", sql_endpoint="x.datawarehouse.fabric.microsoft.com", sql_db="sql-1", lakehouse_name="ContosoBankLH")
    return sp, oac.build_semantic_model(sp, ctx), oac.build_ontology(sp, ctx)


def test_deploy_creates_then_skips_then_replaces_with_backup(mock, ops, parts, tmp_path):
    sp, sm, ont = parts
    ws = ops.ensure_workspace("w", "contosofabricf4")["workspaceId"]
    st = ops.deploy_items(ws, sp, sm, ont, refresh=True)
    created = mock.definitions[st["ontologyId"]]
    assert len(created["parts"]) == len(ont) and created.get("format") is None
    assert mock.definitions[st["semanticModelId"]]["format"] == "TMDL"
    assert any("refreshes" in r["path"] and r["auth"] == "Bearer tok-pbi" for r in mock.requests)
    st2 = ops.deploy_items(ws, sp, sm, ont)                                           # exists -> skipped, nothing overwritten
    assert st2 == st and not [r for r in mock.requests if r["path"].endswith("updateDefinition")]
    ops.deploy_items(ws, sp, sm, ont, replace=True, backup_dir=tmp_path / "bk")
    assert [r for r in mock.requests if r["path"].endswith("updateDefinition") and r["query"]["updateMetadata"] == ["false"]]
    assert (tmp_path / "bk" / "model.tmdl").exists()                                 # live definition backed up before overwrite


def test_verify_ontology_reports_graph_eligibility_hint(mock, ops, parts):
    sp, sm, ont = parts
    ws = ops.ensure_workspace("w", "contosofabricf4")["workspaceId"]
    st = ops.deploy_items(ws, sp, sm, ont)
    v = ops.verify_ontology(ws, st["ontologyId"], sp)
    assert v["valid"] and v["entitiesLive"] == v["entitiesExpected"] == 31 and not v["missingEntities"]
    assert v["relationshipsLive"] == 53 and v["rulesLive"] == 23
    assert v["graphAnnotationsPresent"] is False and "portal" in v["graphHint"]
    # a seeded definition carries the portal's ONT_* annotations
    seeded = oac.build_ontology(sp, oac.make_ctx(), None, ont_annotations=True)
    mock.definitions[st["ontologyId"]] = __import__("mock_fabric").parts_b64(seeded)
    assert ops.verify_ontology(ws, st["ontologyId"], sp)["graphAnnotationsPresent"] is True


def test_mcp_and_foundry_iq_helpers(mock):
    r = fabric_ops.mcp_tools_list(f"{mock.base}/mcp", "tok")
    assert r["ok"] and "search_ontology" in r["tools"]
    assert any(q["headers"].get("Mcp-Session-Id") == "s1" for q in mock.requests if q["path"] == "/mcp" and b"tools/list" in q["body"])
    txt = fabric_ops.ask_foundry_iq(mock.base, "kb1", "How much does Rhea owe?", "user-token")
    assert "42,000" in txt
    sent = [q for q in mock.requests if q["path"].startswith("/knowledgebases")][0]
    assert sent["headers"]["x-ms-query-source-authorization"] == "user-token" and sent["query"]["api-version"] == ["2026-08-01-preview"]


def test_mcp_failure_is_reported_not_raised():
    r = fabric_ops.mcp_tools_list("http://127.0.0.1:9/none", "t", timeout=2)
    assert r["ok"] is False and "manually" in r["note"]


def test_delta_fallback_loader_writes_valid_delta_with_fabric_friendly_types(repo_path, model, tmp_path):
    """The gold schema must survive a round trip through a real Delta writer: timestamp/date/bool/int64/double/string only. The Delta
    tables carry the lakehouse (physical) names of the Parquet files."""
    from deltalake import DeltaTable
    from contoso_onto.gold import logical_name
    from contoso_onto.pipeline import build_gold, write_gold
    built = build_gold(model, repo_path)
    write_gold(model, built, tmp_path / "out")
    ops = fabric_ops.Ops(oac.Fabric(lambda s: "t"), lambda s: "t", lambda m: None)
    res = ops.load_gold_delta(tmp_path / "out" / "gold", "ws", "lh", uri_base=str(tmp_path / "Tables"), storage_options={})
    assert len(res["loaded"]) == 51 and set(res["loaded"]) == set(gold.physical_names(model).values())
    seen = set()
    for t in res["loaded"]:
        dt = DeltaTable(str(tmp_path / "Tables" / t))
        assert dt.to_pyarrow_table().num_rows == built.tables[logical_name(t, model)].num_rows
        seen |= {f["type"] for f in json.loads(dt.schema().to_json())["fields"]}
    assert seen == {"string", "long", "double", "boolean", "date", "timestamp"}, seen       # no decimal, no ntz, no nested types


# ---------------------------------------------------------------------------------------------- waiting between tries
def test_retry_after_is_read_as_seconds_or_as_an_http_date_and_anything_else_is_ignored():
    import datetime
    now = datetime.datetime(2026, 10, 16, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
    assert oac.retry_after_seconds("7") == 7.0 and oac.retry_after_seconds(" 2.5 ") == 2.5 and oac.retry_after_seconds(0) == 0.0 and oac.retry_after_seconds("-5") == 0.0
    assert oac.retry_after_seconds("Fri, 16 Oct 2026 12:00:30 GMT", now=lambda: now) == 30.0
    assert oac.retry_after_seconds("Fri, 16 Oct 2026 11:00:00 GMT", now=lambda: now) == 0.0          # a date in the past: no wait
    assert [oac.retry_after_seconds(v) for v in (None, "", "soon", "nan", "inf")] == [None] * 5


def test_the_back_off_never_waits_less_than_retry_after_and_otherwise_doubles_with_jitter():
    assert [oac.backoff_delay(a, rng=lambda: 0.0) for a in range(6)] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]        # half of a ceiling that doubles up to the cap of 60 s ...
    assert [oac.backoff_delay(a, rng=lambda: 1.0) for a in range(6)] == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0]       # ... and the other half is random
    assert oac.backoff_delay(0, 7, rng=lambda: 0.0) == 7.0 and oac.backoff_delay(9, 7, rng=lambda: 1.0) == 8.75   # Retry-After is a floor, plus up to a quarter more; the attempt does not matter
    assert oac.backoff_delay(0, 900, rng=lambda: 0.0) == oac.MAX_RETRY_AFTER_S                                   # an absurd ask is cut at two minutes
    assert oac.backoff_delay(0, 0, rng=lambda: 0.0) == 1.0                                                       # Retry-After: 0 is no hint
    waits = {oac.backoff_delay(3) for _ in range(30)}
    assert len(waits) > 1 and all(8.0 <= w <= 16.0 for w in waits)                                               # real jitter: callers that fail together do not come back together


@pytest.fixture()
def timed_fab(mock):
    """A Fabric client whose waits are recorded instead of slept and whose jitter is zero."""
    fab = oac.Fabric(lambda s: TOKENS[s], api_base=f"{mock.base}/v1", pbi_base=f"{mock.base}/v1.0/myorg", sleeper=lambda s: fab.slept.append(s), log=lambda m: fab.logs.append(m),
                     rng=lambda: 0.0)
    fab.slept, fab.logs = [], []
    return fab


def test_a_throttled_call_waits_as_long_as_retry_after_asks_whatever_form_it_takes(mock, timed_fab):
    mock.faults = [{"match": r"/v1/workspaces$", "method": "GET", "status": 429, "retry_after": 7, "count": 2}]
    assert timed_fab.call("GET", f"{timed_fab.api}/workspaces").status_code == 200
    assert timed_fab.slept == [7.0, 7.0] and mock.fault_hits == 2 and all(m.endswith("retrying in 7s") and m.startswith("  429 from GET ") for m in timed_fab.logs)
    timed_fab.slept.clear()
    mock.faults = [{"match": r"/v1/workspaces$", "method": "GET", "status": 503, "retry_after": "Fri, 01 Jan 2100 00:00:00 GMT", "count": 1}]      # an HTTP date: int() used to crash on it
    assert timed_fab.call("GET", f"{timed_fab.api}/workspaces").status_code == 200 and timed_fab.slept == [oac.MAX_RETRY_AFTER_S]
    timed_fab.slept.clear()
    mock.faults = [{"match": r"/v1/workspaces$", "method": "GET", "status": 502, "retry_after": "soon", "count": 1}]
    assert timed_fab.call("GET", f"{timed_fab.api}/workspaces").status_code == 200 and timed_fab.slept == [1.0]               # unreadable: the exponential wait


def test_without_retry_after_the_waits_double_and_the_error_that_ends_it_carries_status_and_retry_after(mock, timed_fab):
    mock.faults = [{"match": r"/v1/workspaces$", "method": "GET", "status": 503, "count": 99}]
    with pytest.raises(oac.FabricError, match="-> 503") as err:
        timed_fab.call("GET", f"{timed_fab.api}/workspaces")
    assert timed_fab.slept == [1.0, 2.0, 4.0, 8.0, 16.0] and mock.fault_hits == 6                  # six tries, five waits
    assert (err.value.status, err.value.retry_after) == (503, None)
    timed_fab.slept.clear()
    mock.faults = [{"match": r"/v1/workspaces$", "method": "GET", "status": 429, "retry_after": 30, "count": 99}]
    with pytest.raises(oac.FabricError) as err:
        timed_fab.call("GET", f"{timed_fab.api}/workspaces")
    assert (err.value.status, err.value.retry_after) == (429, 30.0) and timed_fab.slept == [30.0] * 5
    timed_fab.slept.clear()
    mock.faults = [{"match": r"/v1/workspaces$", "method": "GET", "status": 404, "count": 99}]
    with pytest.raises(oac.FabricError) as err:                                                    # a real answer is not waited for
        timed_fab.call("GET", f"{timed_fab.api}/workspaces")
    assert err.value.status == 404 and not timed_fab.slept


def test_an_operation_that_ends_failed_is_an_operation_failed_error_without_an_http_status(mock, timed_fab):
    oid = "op-failed"
    mock.ops[oid] = {"status": "Failed", "error": {"errorCode": "Boom"}, "result": None}

    class Accepted:
        status_code, headers = 202, {"Location": f"{mock.base}/v1/operations/{oid}", "Retry-After": "soon"}
    with pytest.raises(oac.OperationFailed, match='operation Failed: {"errorCode": "Boom"}') as err:
        timed_fab.lro(Accepted)
    assert isinstance(err.value, oac.FabricError) and err.value.status is None


# ---------------------------------------------------------------------------------------------- transport errors
class Reply:
    """What the fake `requests.request` answers."""

    def __init__(self, status: int = 200, headers: dict | None = None, text: str = "{}"):
        self.status_code, self.headers, self.text, self.content = status, headers or {}, text, text.encode()

    def json(self):
        return json.loads(self.text)


@pytest.fixture()
def flaky(monkeypatch):
    """A Fabric client on a virtual clock whose requests are scripted: an exception in `script` is raised, a Reply is answered, and once the script is
    used up every request is answered 200."""
    import requests

    class Flaky:
        def __init__(self):
            self.vt, self.logs, self.calls, self.script = VirtualTime(), [], [], []
            self.fab = oac.Fabric(lambda s: "tok", api_base="http://fabric.invalid/v1", pbi_base="http://fabric.invalid/v1.0/myorg", sleeper=self.vt.sleep,
                                  clock=self.vt.now, rng=lambda: 0.0, log=self.logs.append)

        def request(self, method, url, **kw):
            self.calls.append((method, url))
            step = self.script.pop(0) if self.script else Reply()
            if isinstance(step, BaseException):
                raise step
            return step

    f = Flaky()
    monkeypatch.setattr(requests, "request", f.request)
    return f


@pytest.mark.parametrize("method", ["GET", "POST", "PUT"])
@pytest.mark.parametrize("error", ["ConnectionError", "ReadTimeout", "ConnectTimeout", "ChunkedEncodingError"])
def test_a_dropped_or_timed_out_request_is_tried_again_like_a_503_whatever_the_method(flaky, method, error):
    import requests
    cls = getattr(requests, error, None) or getattr(requests.exceptions, error)
    flaky.script = [cls("Read timed out. (read timeout=120)"), cls("Read timed out. (read timeout=120)")]
    assert flaky.fab.call(method, "http://fabric.invalid/v1/workspaces?x=1").status_code == 200
    assert len(flaky.calls) == 3 and {m for m, _ in flaky.calls} == {method} and flaky.vt.slept == [1.0, 2.0]
    assert flaky.logs[0] == f"  {error} from {method} http://fabric.invalid/v1/workspaces: Read timed out. (read timeout=120); retrying in 1s"


def test_a_request_that_never_gets_through_ends_with_the_original_error_after_the_same_six_tries_as_a_503(flaky):
    import requests
    flaky.script = [requests.ReadTimeout(f"HTTPSConnectionPool(host='api.fabric.microsoft.com'): Read timed out. {i}") for i in range(9)]
    with pytest.raises(requests.ReadTimeout, match=r"Read timed out\. 5$"):                        # not wrapped: the text of the last one, exactly as requests said it
        flaky.fab.call("POST", "http://fabric.invalid/v1/workspaces/w/items")
    assert len(flaky.calls) == 6 and flaky.vt.slept == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert fabric_ops.is_transient(requests.ReadTimeout("x")) and len(flaky.script) == 3          # and the layers above treat it as the moment, not the request


def test_transport_errors_and_throttled_replies_share_one_ladder(flaky):
    import requests
    flaky.script = [Reply(503), requests.ReadTimeout("t"), Reply(429, {"Retry-After": "7"}), requests.ConnectionError("c")]
    assert flaky.fab.call("GET", "http://fabric.invalid/v1/workspaces").status_code == 200
    assert len(flaky.calls) == 5 and flaky.vt.slept == [1.0, 2.0, 7.0, 8.0]                         # the attempt number counts every kind of failure


def test_a_retry_deadline_cuts_the_transport_error_ladder_short_and_raises_the_original_error(flaky):
    import requests
    flaky.script = [requests.ReadTimeout(f"t{i}") for i in range(6)]
    with flaky.fab.retry_until(flaky.vt.now() + 2.5), pytest.raises(requests.ReadTimeout, match="t1"):
        flaky.fab.call("GET", "http://fabric.invalid/v1/workspaces")
    assert len(flaky.calls) == 2 and flaky.vt.slept == [1.0]                                       # the 2 s wait would have ended at 3 s: not taken
    assert flaky.fab.out_of_time(10**6) is False                                                   # and the deadline ends with its block


def test_only_transport_errors_are_retried_not_the_other_ways_a_request_can_fail(flaky):
    import requests
    flaky.script = [requests.exceptions.InvalidURL("no host")]
    with pytest.raises(requests.exceptions.InvalidURL):
        flaky.fab.call("GET", "http://fabric.invalid/v1/workspaces")
    assert len(flaky.calls) == 1 and not flaky.vt.slept


# ---------------------------------------------------------------------------------------------- what is a refusal of the load, what is the moment
def test_only_a_refused_load_call_is_a_rejection_and_nothing_about_the_moment_is_one():
    import requests

    def marked(ex):
        ex.from_load_call = True
        return ex

    def operation(code, status="Failed"):
        return oac.OperationFailed(f"operation {status}: " + json.dumps({"errorCode": code} if code else {}))

    for status in (400, 415, 422):
        assert fabric_ops.is_rejection(marked(oac.FabricError("x", status=status))) and not fabric_ops.is_transient(oac.FabricError("x", status=status))
    for status in (401, 403, 404, 409, 429, 500, 502, 503, 504):
        assert not fabric_ops.is_rejection(marked(oac.FabricError("x", status=status))), status
    assert not fabric_ops.is_rejection(oac.FabricError("x", status=400))                           # the same 400 from a staging upload or a listing: not marked, never a refusal
    assert not fabric_ops.is_rejection(marked(requests.ConnectionError("x"))) and not fabric_ops.is_rejection(RuntimeError("x"))
    assert fabric_ops.is_rejection(marked(operation("InvalidFile"))) and fabric_ops.is_rejection(marked(operation("UnsupportedFormat")))
    for code in ("InternalError", "ServiceUnavailable", "RequestTimeout", "OperationCanceled", "CapacityLimitExceeded", "ThrottledRequest", "TooManyRequests", "ServerBusy", ""):
        assert not fabric_ops.is_rejection(marked(operation(code))), code                           # a capacity, a service or no word at all: not the file's fault
    assert not fabric_ops.is_rejection(marked(operation("InvalidFile", "Cancelled")))
    assert [fabric_ops.is_transient(operation(c)) for c in ("InternalError", "CapacityLimitExceeded", "Throttled", "InvalidFile", "")] == [True, True, True, False, False]
    assert fabric_ops.is_transient(operation("InvalidFile", "Cancelled")) and fabric_ops.is_retryable(oac.FabricError("x", status=409))
    assert fabric_ops.operation_code(operation("InvalidFile")) == "InvalidFile" and fabric_ops.operation_code(RuntimeError("no code")) == ""


# ---------------------------------------------------------------------------------------------- continuation tokens
def test_continuation_tokens_are_url_encoded_so_that_plus_slash_and_equals_survive(mock, ops):
    mock.page_size = 2                                                                              # the mock's tokens are tk+<n>/x== and must come back exactly
    ws = ops.ensure_workspace("w", "contosofabricf4")["workspaceId"]
    ids = [ops.fab.create(ws, "lakehouses", {"displayName": f"L{i}"}) for i in range(5)]
    mock.requests.clear()
    assert sorted(i["id"] for i in ops.fab.list_items(ws, "lakehouses")) == sorted(ids)             # three pages, nothing lost, nothing refused
    raw = [r["raw_query"] for r in mock.requests if "continuationToken" in r["raw_query"]]
    assert raw == ["continuationToken=tk%2B2%2Fx%3D%3D", "continuationToken=tk%2B4%2Fx%3D%3D"]
    lh = ids[0]
    mock.tables[lh] = [f"t{i}" for i in range(5)]
    mock.requests.clear()
    assert [t["name"] for t in ops.list_tables(ws, lh)] == mock.tables[lh]
    assert [r["raw_query"] for r in mock.requests if "continuationToken" in r["raw_query"]] == raw


def test_a_client_that_did_not_encode_the_token_would_be_refused_by_the_mock(mock, ops):
    import requests
    mock.page_size = 1
    ws = ops.ensure_workspace("w", "contosofabricf4")["workspaceId"]
    for i in range(2):
        ops.fab.create(ws, "lakehouses", {"displayName": f"L{i}"})
    r = requests.get(f"{ops.fab.api}/workspaces/{ws}/lakehouses?continuationToken=tk+1/x==", headers={"Authorization": "Bearer x"}, timeout=10)
    assert r.status_code == 400 and r.json()["errorCode"] == "InvalidContinuationToken"            # '+' arrives as a space: the proof that the encoding matters
