"""A small in-process mock of the Fabric REST API, OneLake DFS, an MCP endpoint and the Azure AI Search retrieve call."""
from __future__ import annotations

import base64
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class VirtualTime:
    """Time that passes only when something sleeps: a sleeper that records the wait and moves a monotonic clock by it. Hand `sleep` to the Fabric client and to
    Ops as their sleeper and `now` to the Fabric client as its clock, and retries, back-offs and deadlines run in no real time at all."""

    def __init__(self) -> None:
        self.now_s = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.now_s

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now_s += seconds


class MockFabric:
    def __init__(self, capacity_state: str = "Active", capacity_sku: str = "F4"):
        self.requests: list[dict] = []
        self.capacity_state = capacity_state
        self.capacity_sku = capacity_sku                                # the size Fabric reports for the capacity
        self.capacity_hidden_polls = 0                                  # a brand-new capacity is not listed by Fabric for this many calls
        self.capacity_assignment_fails = False                          # a workspace never ends up on the capacity it was created on / assigned to
        self.workspaces: dict[str, dict] = {}
        self.items: dict[tuple[str, str], dict[str, dict]] = {}       # (ws, collection) -> id -> item
        self.definitions: dict[str, dict] = {}
        self.tables: dict[str, list[str]] = {}                          # lakehouse id -> table names
        self.table_loads: list[dict] = []
        self.files: dict[str, bytes] = {}
        self.ops: dict[str, dict] = {}
        self.sql_ready_after = 0
        self.tenant_settings: list[dict] | None = None
        self.deleted_workspaces: dict[str, dict] = {}                  # workspace retention shells: id -> {displayName, itemsLeft}
        self.recycle: dict[str, dict[str, dict]] = {}                  # workspace id -> item id -> soft-deleted item
        self.hard_deleted: list[dict] = []                             # items permanently deleted
        self.purged: list[dict] = []                                   # items emptied from the recycle bin
        self.renames: list[tuple[str, str]] = []
        self.stuck_item_ids: set[str] = set()                          # deleting these answers 403
        self.soft_delete_disabled = False                              # tenant switch: item recovery off, hardDelete answers TenantSwitchDisabled
        self.ontology_forbidden = False                                # tenant setting 'Users can create ontology (preview) items' is off: creating one answers 403 FeatureNotAvailable
        self.forbidden_creates = 0                                     # ontology creates answered 403 so far (a test flips ontology_forbidden from a poll hook)
        self.on_forbidden_create = None                                # called after every refused create: lets a test open the gate after N polls
        self.graph_update_error: str | None = None                     # errorCode the graph model updateDefinition answers with (default: it works)
        self.graph_jobs: dict[str, list[dict]] = {}                    # graph model id -> job instances, oldest first
        self.result_error: str | None = None                           # error code the /result of a result-less operation answers with (default OperationHasNoResult)
        self.load_failures: dict[str, int] = {}                        # table -> Load Table calls that answer 409 before one succeeds
        self.load_delay = 0.0                                          # seconds each Load Table call takes (so that concurrent loads overlap)
        self.inflight_loads = 0
        self.max_inflight_loads = 0
        self.dfs_corrupt = False                                       # OneLake keeps one byte less than was appended (a truncated upload)
        self.dfs_read_status: int | None = None                        # status the read of a OneLake file answers with instead of the bytes (for example 403)
        self.dfs_list_status: int | None = None                        # status the listing of a OneLake folder answers with instead of the paths
        # A limit on the description line of an entity (ontology) or table (semantic model): the real service documents none, so this is a hook to rehearse a refusal.
        # The error body has the shape Fabric uses (requestId, errorCode, message, moreDetails); its wording is invented.
        self.max_description_chars: int | None = None                  # a definition with a longer description line is refused (default: no limit)
        self.description_collections = {"ontologies", "semanticModels"}   # the item types the limit applies to
        self.refuse_via_operation = False                              # refuse with 202 and an operation that ends Failed, instead of an immediate 400
        self.validate_before_gate = False                              # answer a refused definition before the 403 gate (default: the gate comes first)
        self.description_refusals: list[dict] = []                     # every definition refused for its descriptions: {collection, entity, characters}
        # Faults for the load pipeline (the first live run is where these happen): every knob is off by default.
        self.faults: list[dict] = []                                   # {match: regex on the path, method, status, retry_after, count, delay}: matching requests answer status first (after `delay` s)
        self.fault_hits = 0
        self.load_rejections: dict[str, str] = {}                      # table -> how the Load Table API refuses its file for good: "400" (HTTP 400), "failed" or "failed:<errorCode>" (an operation that ends Failed), "failed-nocode", "cancelled"
        self.page_size: int | None = None                              # listings of tables and items answer this many entries per page, with a continuation token that must come back intact
        self.sql_sync_polls: int | None = None                         # refreshMetadata answers 202 and its operation runs for this many polls (default: it answers 200 at once)
        self.sql_sync_status = "Succeeded"                             # how that operation ends
        self.list_lag = 0                                              # a table the Load Table API has just created stays out of the table listing for this many listings (live: the listing trails the load)
        self._unlisted: dict[tuple[str, str], int] = {}
        self.endpoint_lag_creates = 0                                  # the first N ontology creates answer 400: the SQL analytics endpoint does not know the loaded tables yet
        self.endpoint_lag_refusals = 0
        self._lock = threading.Lock()
        self._lh_polls: dict[str, int] = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401
                pass

            def _read(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _send(self, code: int, body=None, headers=None):
                raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _dispatch(self, method: str):
                raw = self._read()
                u = urlparse(self.path)
                outer.requests.append({"method": method, "path": u.path, "query": parse_qs(u.query), "raw_query": u.query, "auth": self.headers.get("Authorization"),
                                       "body": raw, "headers": dict(self.headers)})
                try:
                    code, body, hdrs = outer.handle(method, u.path, parse_qs(u.query), raw, f"http://{self.headers['Host']}")
                except KeyError as ex:
                    code, body, hdrs = 404, {"error": f"not found {ex}"}, {}
                self._send(code, body, hdrs)

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

            def do_PUT(self):
                self._dispatch("PUT")

            def do_PATCH(self):
                self._dispatch("PATCH")

            def do_DELETE(self):
                self._dispatch("DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

    # ---- helpers ----
    TYPES = {"lakehouses": "Lakehouse", "semanticModels": "SemanticModel", "ontologies": "Ontology", "graphModels": "GraphModel"}
    SOFT_DELETE_TYPES = {"Lakehouse", "Ontology"}                     # semantic models and graph models are irreversibly deleted by any DELETE

    def _job_status(self, job: dict) -> str:
        if job.get("fixed"):
            return job["fixed"]
        return "NotStarted" if job["polls"] < 1 else ("InProgress" if job["polls"] < 2 else "Completed")

    def _new_job(self, gm_id: str, fixed: str | None = None) -> dict:
        job = {"id": str(uuid.uuid4()), "polls": 0, "fixed": fixed, "startTimeUtc": f"2026-10-16T15:{len(self.graph_jobs.get(gm_id, [])):02d}:00"}
        self.graph_jobs.setdefault(gm_id, []).append(job)
        return job

    def _graph_configured(self, gm_id: str) -> bool:
        parts = {p["path"]: base64.b64decode(p["payload"]).decode() for p in self.definitions[gm_id]["parts"]}
        return bool(json.loads(parts.get("graphType.json", "{}")).get("nodeTypes"))

    def _graph_jobs(self, method: str, ws_id: str, rest: list[str], body, base: str):
        """items/{graph model}/jobs/instances[/{job}] and graphModels/{id}/jobs/refreshGraph/instances, as the live service answers them."""
        gm_id = rest[1]
        loc = f"{base}/v1/workspaces/{ws_id}/items/{gm_id}/jobs/instances"
        if method == "POST":                                           # refreshGraph
            if not self._graph_configured(gm_id):
                job = self._new_job(gm_id, fixed="Cancelled")           # live: a graph that was never configured cancels a REST refresh within seconds
            elif any(self._job_status(j) in ("NotStarted", "InProgress") for j in self.graph_jobs.get(gm_id, [])):
                job = self._new_job(gm_id, fixed="Deduped")             # live: JobInstanceStatusDeduped while a refresh is already running
            else:
                job = self._new_job(gm_id)
            return 202, None, {"Location": f"{loc}/{job['id']}", "Retry-After": "0"}
        if len(rest) == 4:                                             # list
            return 200, {"value": [{"id": j["id"], "status": self._job_status(j), "startTimeUtc": j["startTimeUtc"]} for j in self.graph_jobs.get(gm_id, [])]}, {}
        job = next(j for j in self.graph_jobs.get(gm_id, []) if j["id"] == rest[4])
        status = self._job_status(job)
        job["polls"] += 1
        out = {"id": job["id"], "status": status, "startTimeUtc": job["startTimeUtc"]}
        if status == "Deduped":
            out["failureReason"] = {"errorCode": "JobInstanceStatusDeduped", "message": "A job instance of the same job type is already running and this job instance is skipped"}
        return 200, out, {}

    def _graph_type(self, gm_id: str):
        done = any(self._job_status(j) == "Completed" for j in self.graph_jobs.get(gm_id, []))
        parts = {p["path"]: base64.b64decode(p["payload"]).decode() for p in self.definitions[gm_id]["parts"]}
        gt = json.loads(parts.get("graphType.json", "{}")) if done else {}
        return 200, {"nodeTypes": gt.get("nodeTypes", []), "edgeTypes": gt.get("edgeTypes", [])}, {}

    def _delete_item(self, ws_id: str, item_id: str, hard: bool):
        found = next(((coll, typ, self._items(ws_id, coll)[item_id]) for coll, typ in self.TYPES.items() if item_id in self._items(ws_id, coll)), None)
        if found is None:
            return 404, {"errorCode": "ItemNotFound"}, {}
        coll, typ, it = found
        if item_id in self.stuck_item_ids:
            return 403, {"errorCode": "InsufficientWorkspaceRole", "message": "not allowed"}, {}
        if hard and self.soft_delete_disabled:
            return 400, {"errorCode": "TenantSwitchDisabled", "message": "soft deletion is disabled by the tenant admin"}, {}
        if hard and typ not in self.SOFT_DELETE_TYPES:
            return 400, {"errorCode": "UnsupportedItemType", "message": "soft deletion is not supported for this item type"}, {}
        self._items(ws_id, coll).pop(item_id)
        if typ == "Lakehouse":                                          # its files and tables go with it, hard or soft deleted
            self._drop_lakehouse_data(ws_id, item_id)
        rec = {**it, "type": typ, "workspaceId": ws_id}
        if hard or typ not in self.SOFT_DELETE_TYPES or self.soft_delete_disabled:
            self.hard_deleted.append(rec)
        else:
            self.recycle.setdefault(ws_id, {})[item_id] = rec
        return 200, None, {}

    def _drop_lakehouse_data(self, ws_id: str, lh_id: str) -> None:
        self.tables.pop(lh_id, None)
        for key in [k for k in self.files if k.startswith(f"{ws_id}/{lh_id}/")]:
            self.files.pop(key)

    def _op(self, result: dict | None, base: str):
        oid = str(uuid.uuid4())
        self.ops[oid] = {"status": "Succeeded", "result": result}
        return 202, None, {"Location": f"{base}/v1/operations/{oid}", "Retry-After": "0"}

    def _description_refusal(self, coll: str, definition: dict | None, base: str):
        """The answer for a definition whose entity (ontology) or table (semantic model) description line is over `max_description_chars`; None when it is fine."""
        if not self.max_description_chars or coll not in self.description_collections or not definition:
            return None
        for part in definition.get("parts", []):
            if not ((coll == "ontologies" and part["path"].startswith("entities/")) or (coll == "semanticModels" and "/tables/" in part["path"])):
                continue
            first = base64.b64decode(part["payload"]).decode("utf-8").split("\n", 1)[0]
            if first.startswith("/// ") and len(first) - 4 > self.max_description_chars:
                entity, n = part["path"].rsplit("/", 1)[-1].removesuffix(".tmdl"), len(first) - 4
                self.description_refusals.append({"collection": coll, "entity": entity, "characters": n})
                err = {"requestId": "r-6", "errorCode": "InvalidDefinition", "message": f"The definition of the {self.TYPES[coll]} item is not valid.",
                       "moreDetails": [{"errorCode": "InvalidEntityDescription", "message": f"Entity '{entity}': the description is {n} characters long, the maximum is {self.max_description_chars}."}]}
                if not self.refuse_via_operation:
                    return 400, err, {}
                oid = str(uuid.uuid4())
                self.ops[oid] = {"status": "Failed", "error": err, "result": None}
                return 202, None, {"Location": f"{base}/v1/operations/{oid}", "Retry-After": "0"}
        return None

    def _items(self, ws, coll):
        return self.items.setdefault((ws, coll), {})

    def _page(self, entries: list, q: dict):
        """(the entries of this page, the token of the next one, an error reply or None). Without `page_size` everything is one page. The tokens have + / = in them
        on purpose and must come back byte for byte: a client that does not URL-encode the token gets the + turned into a space and a 400."""
        if not self.page_size:
            return entries, None, None
        token = (q.get("continuationToken") or [None])[0]
        start = 0
        if token is not None:
            m = re.fullmatch(r"tk\+(\d+)/x==", token)
            if not m:
                return None, None, (400, {"requestId": "r-9", "errorCode": "InvalidContinuationToken", "message": f"the continuation token {token!r} is not one this service issued"}, {})
            start = int(m.group(1))
        end = start + self.page_size
        return entries[start:end], (f"tk+{end}/x==" if end < len(entries) else None), None

    def _failed_op(self, error: dict, base: str, status: str = "Failed"):
        """202 and a long-running operation that ends Failed (or Cancelled)."""
        oid = str(uuid.uuid4())
        self.ops[oid] = {"status": status, "error": error, "result": None}
        return 202, None, {"Location": f"{base}/v1/operations/{oid}", "Retry-After": "0"}

    def _dfs(self, method: str, parts: list[str], q: dict, raw: bytes):
        """OneLake DFS: <workspace>/<lakehouse>/<path>. PUT creates, PATCH appends or flushes, GET reads (or lists a folder when the query says
        resource=filesystem: Path - List, directory=<lakehouse>/<folder>), as the live service answers them."""
        key = "/".join(parts)
        if method == "GET" and q.get("resource") == ["filesystem"]:
            if self.dfs_list_status:
                return self.dfs_list_status, {"error": {"code": "AuthorizationFailure"}}, {}
            prefix = f"{parts[0]}/{(q.get('directory') or [''])[0].strip('/')}/"
            paths = [{"name": k[len(parts[0]) + 1:], "contentLength": str(len(v)), "isDirectory": "false"} for k, v in sorted(self.files.items()) if k.startswith(prefix)]
            return (200, {"paths": paths}, {}) if paths else (404, {"error": {"code": "PathNotFound"}}, {})
        if method == "GET":
            if self.dfs_read_status:
                return self.dfs_read_status, {"error": {"code": "AuthorizationFailure"}}, {}
            return (200, self.files[key], {}) if key in self.files else (404, {"error": {"code": "PathNotFound"}}, {})
        if method == "PUT":
            self.files[key] = b""
        elif method == "PATCH" and q.get("action") == ["append"]:
            self.files[key] = self.files.get(key, b"") + (raw[:-1] if self.dfs_corrupt else raw)
        return 200, None, {}

    def handle(self, method, path, q, raw, base):
        hit = None
        with self._lock:
            for fault in self.faults:
                if fault["count"] > 0 and fault.get("method") in (None, method) and re.search(fault["match"], path):
                    fault["count"] -= 1
                    self.fault_hits += 1
                    hit = fault
                    break
        if hit is not None:
            if hit.get("delay"):                                                      # the failing request takes this long to answer (so that concurrent loads overlap)
                time.sleep(hit["delay"])
            return hit["status"], {"error": {"code": "Injected"}}, ({"Retry-After": str(hit["retry_after"])} if hit.get("retry_after") is not None else {})
        body = json.loads(raw) if raw and raw[:1] in (b"{", b"[") else None
        p = path.strip("/").split("/")
        # OneLake DFS
        if p[0] == "dfs":
            return self._dfs(method, p[1:], q, raw)
        if p[0] == "mcp":
            m = body.get("method")
            if m == "initialize":
                return 200, {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}, {"Mcp-Session-Id": "s1"}
            if m == "tools/list":
                return 200, {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "list_ontology_entity_types"}, {"name": "search_ontology"}]}}, {}
            return 202, None, {}
        if p[0] == "knowledgebases":
            return 200, {"response": [{"content": [{"type": "text", "text": "Rhea owes INR 42,000"}]}]}, {}
        if p[0] == "v1.0":                                    # Power BI refresh
            return 202, None, {}
        assert p[0] == "v1", path
        p = p[1:]
        if p[0] == "operations":
            op = self.ops[p[1]]
            if len(p) == 3:
                if op["result"] is not None:
                    return 200, op["result"], {}
                # the live API's answer for an operation that succeeded without producing a result (seen on the first real Load Table)
                return 400, {"requestId": "r-2", "errorCode": self.result_error or "OperationHasNoResult", "message": "The operation has no result",
                             "isRetriable": False}, {}
            if op.get("running", 0) > 0:                               # a long-running operation that is still going (the SQL endpoint refresh)
                op["running"] -= 1
                return 200, {"status": "Running"}, {}
            return 200, {"status": op["status"], **({"error": op["error"]} if "error" in op else {})}, {}
        if p == ["capacities"]:
            if self.capacity_hidden_polls > 0:
                self.capacity_hidden_polls -= 1
                return 200, {"value": []}, {}
            return 200, {"value": [{"id": "cap-guid-1", "displayName": "contosofabricf4", "sku": self.capacity_sku, "state": self.capacity_state}]}, {}
        if p == ["admin", "tenantsettings"]:
            if self.tenant_settings is None:
                return 403, {"errorCode": "InsufficientPrivileges"}, {}
            return 200, {"tenantSettings": self.tenant_settings}, {}
        if p == ["workspaces"]:
            if method == "GET":
                return 200, {"value": list(self.workspaces.values())}, {}
            names = {w["displayName"] for w in self.workspaces.values()} | {s["displayName"] for s in self.deleted_workspaces.values()}
            if body["displayName"] in names:                          # a workspace in retention still reserves its name
                return 409, {"errorCode": "WorkspaceNameAlreadyExists", "message": f"a workspace named {body['displayName']} exists"}, {}
            ws = {"id": str(uuid.uuid4()), "displayName": body["displayName"], "capacityId": None if self.capacity_assignment_fails else body.get("capacityId")}
            self.workspaces[ws["id"]] = ws
            return 201, ws, {}
        ws_id = p[1]
        rest = p[2:]
        if method == "DELETE":
            if not rest:                                              # workspace delete: the emptied workspace is kept as a retention shell
                ws = self.workspaces.pop(ws_id, None)
                if ws is not None:
                    left = sum(len(v) for k, v in self.items.items() if k[0] == ws_id) + len(self.recycle.get(ws_id, {}))
                    self.deleted_workspaces[ws_id] = {"displayName": ws["displayName"], "itemsLeft": left}
                    for lh_id in list(self._items(ws_id, "lakehouses")):
                        self._drop_lakehouse_data(ws_id, lh_id)
                    for k in [k for k in self.items if k[0] == ws_id]:
                        self.items.pop(k)
                    self.recycle.pop(ws_id, None)
                return 200, None, {}
            if rest[0] == "items" and len(rest) == 2:
                return self._delete_item(ws_id, rest[1], q.get("hardDelete") == ["true"])
            if rest[0] == "recoverableItems" and len(rest) == 2:
                it = self.recycle.get(ws_id, {}).pop(rest[1], None)
                if it is None:
                    return 404, {"errorCode": "ItemNotFound"}, {}
                self.purged.append(it)
                return 200, None, {}
            if len(rest) == 2:
                self._items(ws_id, rest[0]).pop(rest[1], None)
                return 200, None, {}
        if method == "PATCH" and not rest:                            # rename
            self.workspaces[ws_id]["displayName"] = body["displayName"]
            self.renames.append((ws_id, body["displayName"]))
            return 200, self.workspaces[ws_id], {}
        if rest and rest[0] == "items" and len(rest) >= 4 and rest[2] == "jobs" and rest[3] == "instances":
            return self._graph_jobs(method, ws_id, rest, body, base)
        if rest and rest[0].lower() == "graphmodels" and len(rest) == 3 and rest[2] == "getQueryableGraphType":
            return self._graph_type(rest[1])
        if rest and rest[0] == "graphModels" and len(rest) == 5 and rest[2:4] == ["jobs", "refreshGraph"] and method == "POST":
            return self._graph_jobs("POST", ws_id, rest, body, base)
        if rest == ["items"] and method == "GET":
            out = []
            for coll, typ in self.TYPES.items():
                for it in self._items(ws_id, coll).values():
                    out.append({"id": it["id"], "displayName": it["displayName"], "type": typ, "workspaceId": ws_id})
                    if typ == "Lakehouse":                            # every lakehouse has a SQL endpoint child
                        out.append({"id": "sql-" + it["id"], "displayName": it["displayName"], "type": "SQLEndpoint", "workspaceId": ws_id})
            return 200, {"value": out}, {}
        if rest == ["recoverableItems"] and method == "GET":
            return 200, {"value": [{"id": i["id"], "displayName": i["displayName"], "type": i["type"], "workspaceId": ws_id,
                                    "retentionExpirationDateTime": "2099-01-01T00:00:00Z"} for i in self.recycle.get(ws_id, {}).values()]}, {}
        if not rest:
            return 200, self.workspaces[ws_id], {}
        if rest == ["assignToCapacity"]:
            if not self.capacity_assignment_fails:
                self.workspaces[ws_id]["capacityId"] = body["capacityId"]
            return 202, None, {}
        if rest == ["roleAssignments"]:
            return 201, {}, {}
        coll = rest[0]
        if coll in ("lakehouses", "semanticModels", "ontologies", "graphModels"):
            items = self._items(ws_id, coll)
            if len(rest) == 1:
                if method == "GET":
                    page, token, bad = self._page(list(items.values()), q)
                    return bad or (200, {"value": page, **({"continuationToken": token} if token else {})}, {})
                if self.validate_before_gate:
                    refused = self._description_refusal(coll, (body or {}).get("definition"), base)
                    if refused:
                        return refused
                if coll == "ontologies" and self.ontology_forbidden:
                    self.forbidden_creates += 1
                    if self.on_forbidden_create:
                        self.on_forbidden_create(self)
                    return 403, {"requestId": "r-3", "errorCode": "FeatureNotAvailable", "message": "The feature is not available", "isRetriable": False}, {}   # live answer, tenant setting off
                if coll == "ontologies" and self.endpoint_lag_creates > 0:      # the ontology binds through the SQL analytics endpoint, which has not picked up the tables yet
                    self.endpoint_lag_creates -= 1
                    self.endpoint_lag_refusals += 1
                    return 400, {"requestId": "r-8", "errorCode": "InvalidDefinition", "message": "The data binding of the ontology refers to a table that was not found in the "
                                 "SQL analytics endpoint of the lakehouse."}, {}                                        # the wording is invented: the live text is not known
                if coll == "lakehouses" and "creationPayload" in body and (body["creationPayload"] or {}).get("enableSchemas") is not True:
                    return 400, {"requestId": "r-1", "errorCode": "InvalidLakehouseCreationPayload", "moreDetails": [{"errorCode": "InvalidLakehouseCreationPayload"}],
                                 "message": "The property 'enableSchemas' in the Lakehouse creation payload is either missing or has a value which is not supported. "
                                            "The only supported value for 'enableSchemas' when a creation payload is specified is 'true'. To create a Lakehouse "
                                            "without schemas enabled, do not specify a creation payload."}, {}       # the live API's answer, seen on the first real build
                refused = self._description_refusal(coll, (body or {}).get("definition"), base)
                if refused:
                    return refused
                item = {"id": str(uuid.uuid4()), "displayName": body["displayName"]}
                items[item["id"]] = item
                if "definition" in body:
                    self.definitions[item["id"]] = body["definition"]
                    item["_created_with"] = body
                if coll == "ontologies":                              # live: Fabric creates a GraphModel child next to every ontology (with an empty graph)
                    gm = {"id": str(uuid.uuid4()), "displayName": f"{body['displayName']}_graph_{item['id'].replace('-', '')}"}
                    self._items(ws_id, "graphModels")[gm["id"]] = gm
                    self.definitions[gm["id"]] = parts_b64({".platform": "{}", "graphType.json": json.dumps({"nodeTypes": [], "edgeTypes": []})})
                return self._op(item, base)
            item_id = rest[1]
            if len(rest) == 2:
                it = dict(items[item_id])
                if coll == "lakehouses":
                    n = self._lh_polls.get(item_id, 0)
                    self._lh_polls[item_id] = n + 1
                    ready = n >= self.sql_ready_after
                    it["properties"] = {"sqlEndpointProperties": {"provisioningStatus": "Success" if ready else "InProgress",
                                                                  "connectionString": "abc.datawarehouse.fabric.microsoft.com" if ready else None, "id": "sql-1"}}
                return 200, it, {}
            action = rest[2]
            if action == "getDefinition":
                return self._op({"definition": self.definitions[item_id]}, base)
            if action == "updateDefinition":
                if coll == "graphModels" and self.graph_update_error:
                    return 400, {"requestId": "r-4", "errorCode": self.graph_update_error, "message": "the graph model definition was refused"}, {}
                refused = self._description_refusal(coll, body.get("definition"), base)
                if refused:
                    return refused
                self.definitions[item_id] = body["definition"]
                if coll == "graphModels":                             # live: a definition update starts a refresh by itself
                    self._new_job(item_id)
                return self._op(None, base)
            if action == "tables" and len(rest) == 3:
                with self._lock:
                    shown = []
                    for t in self.tables.get(item_id, []):
                        left = self._unlisted.get((item_id, t), 0)
                        if left > 0:
                            self._unlisted[(item_id, t)] = left - 1
                        else:
                            shown.append(t)
                page, token, bad = self._page([{"name": t} for t in shown], q)
                return bad or (200, {"data": page, **({"continuationToken": token} if token else {})}, {})
            if action == "tables" and rest[-1] == "load":
                table = rest[3]
                with self._lock:
                    self.inflight_loads += 1
                    self.max_inflight_loads = max(self.max_inflight_loads, self.inflight_loads)
                try:
                    if self.load_delay:
                        time.sleep(self.load_delay)
                    with self._lock:
                        if self.load_failures.get(table, 0) > 0:
                            self.load_failures[table] -= 1
                            return 409, {"errorCode": "ItemOperationInProgress", "message": "another operation is in progress on this lakehouse"}, {}
                        if self.load_rejections.get(table) == "400":
                            return 400, {"requestId": "r-7", "errorCode": "InvalidInput", "message": f"The Parquet file of table {table} cannot be loaded"}, {}
                        mode = self.load_rejections.get(table) or ""
                        if mode == "cancelled":
                            return self._failed_op({"errorCode": "OperationCanceled", "message": "the operation was cancelled"}, base, status="Cancelled")
                        if mode == "failed-nocode":                                    # an operation that ends Failed and names no errorCode
                            return self._failed_op({"message": f"The load of table {table} failed"}, base)
                        if mode.startswith("failed"):                                  # "failed" (code OperationFailed) or "failed:<errorCode>"
                            return self._failed_op({"errorCode": mode.partition(":")[2] or "OperationFailed", "message": f"The Parquet file of table {table} cannot be loaded"}, base)
                        self.table_loads.append({"table": table, **body})
                        names = self.tables.setdefault(item_id, [])
                        if table not in names:
                            names.append(table)
                            if self.list_lag:
                                self._unlisted[(item_id, table)] = self.list_lag
                    return self._op(None, base)
                finally:
                    with self._lock:
                        self.inflight_loads -= 1
        if rest[0] == "sqlEndpoints":
            if rest[-1] == "refreshMetadata" and self.sql_sync_polls is not None:           # a long-running refresh that lists what the endpoint now knows
                oid = str(uuid.uuid4())
                names = sorted({t for v in self.tables.values() for t in v})
                self.ops[oid] = {"status": self.sql_sync_status, "running": self.sql_sync_polls,
                                 "result": {"value": [{"tableName": t, "status": "Success"} for t in names]},
                                 **({"error": {"errorCode": "RefreshFailed", "message": "the metadata refresh failed"}} if self.sql_sync_status == "Failed" else {})}
                return 202, None, {"Location": f"{base}/v1/operations/{oid}", "Retry-After": "0"}
            return 200, {}, {}
        raise KeyError(path)


def parts_b64(parts: dict[str, str]) -> dict:
    return {"parts": [{"path": k, "payload": base64.b64encode(v.encode()).decode(), "payloadType": "InlineBase64"} for k, v in parts.items()]}
