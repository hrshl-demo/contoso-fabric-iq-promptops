"""Fabric side of the demo: workspace, lakehouse, OneLake upload, Load Table API, semantic model + ontology deployment,
post-deployment verification. Every mutating function takes a Fabric client (oac.Fabric) so tests can run against a mock server.
The lakehouse holds three layers (see layers.py): the bronze seed files and manifest table, the silver tables, the gold tables; `load_layers`
puts a LoadPlan there in that order."""
from __future__ import annotations

import hashlib
import json
import random
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import oac
from .layers import BRONZE_FOLDER, LoadPlan, PlannedFile

ONELAKE_DFS = "https://onelake.dfs.fabric.microsoft.com"
STORAGE_SCOPE = "https://storage.azure.com/.default"
SEARCH_SCOPE = "https://search.azure.com/.default"

# What a failed request says. A reply about the moment (throttled, busy, down, a dropped connection) says nothing about the request: trying again later
# is the answer. Only the Load Table call itself being refused can mean the API cannot load that file, which is what the delta-rs fallback is for.
TRANSIENT_STATUS = (408, 425, 429, 500, 502, 503, 504)
REJECTION_STATUS = (400, 415, 422)                  # the answers to the load request itself that refuse the file
# the errorCode of a failed operation that is about the moment, not about the file: a service that is down or slow, a capacity that is short of room,
# throttling, an operation that was cancelled
TRANSIENT_CODE = re.compile(r"InternalError|Unavailable|Timeout|TimedOut|Cancel|Capacity|Throttl|TooManyRequests|ServerBusy", re.I)
SERIAL_TRIES = 5                  # tries in all for an item whose failure is retryable (the first, in the parallel round, counts); a refusal gets 2
DFS_TRIES = 4                     # tries in all for one OneLake request that is throttled or answered with a server error
RETRY_BUDGET_S = 360.0            # how long one table or file is retried in all, whatever the number of tries and waits: after that its failure is final
REGISTER_RETRY_S = 180.0          # how long a table listing that keeps failing is tried again before it is reported (one deadline, waits inside each call included)
REGISTER_ATTEMPTS = 40            # and the most it is tried, whatever the clock says
SYNC_WAIT_S = 600.0               # how long the refresh of the SQL analytics endpoint is followed after the last layer

# hard purge: errors that mean "soft delete does not apply here", after which a plain DELETE is already permanent
SOFT_DELETE_UNAVAILABLE = ("UnsupportedItemType", "FeatureNotAvailable", "TenantSwitchDisabled", "InvalidTargetItemStateForSoftDeletion")
CHILD_TYPES = {"SQLEndpoint"}                      # removed together with their lakehouse; not deletable on their own
# dependants go before the items they depend on (data agent -> ontology -> graph -> semantic model -> lakehouse)
DELETE_RANK = {"DataAgent": 0, "OperationsAgent": 0, "Ontology": 1, "GraphModel": 2, "GraphIndex": 2, "GraphQuerySet": 2, "Report": 3,
               "SemanticModel": 4, "Notebook": 5, "DataPipeline": 5, "Warehouse": 9, "Lakehouse": 9}
DEFAULT_RANK = 6
COLLECTION_TYPE = {"ontologies": "Ontology", "semanticModels": "SemanticModel", "lakehouses": "Lakehouse", "graphModels": "GraphModel"}


def operation_code(ex: BaseException) -> str:
    """The errorCode a failed long-running operation reports ('' when it names none)."""
    m = re.search(r'"errorCode":\s*"([^"]*)"', str(ex))
    return m.group(1) if m else ""


def _operation_is_about_the_moment(ex: BaseException) -> bool:
    return str(ex).startswith("operation Cancelled") or bool(TRANSIENT_CODE.search(operation_code(ex) or str(ex)))


def is_transient(ex: BaseException) -> bool:
    """A failure about the moment, not about the request: throttling, a server that is busy or down, a connection that dropped or timed out."""
    import requests
    if isinstance(ex, requests.RequestException):
        return True
    if isinstance(ex, oac.OperationFailed):
        return _operation_is_about_the_moment(ex)
    return getattr(ex, "status", None) in TRANSIENT_STATUS


def is_rejection(ex: BaseException) -> bool:
    """The Load Table call itself was refused, and nothing else: `Ops.load_table` marks what it raises (`from_load_call`), and only that counts. It is an
    HTTP 400, 415 or 422 answer to the load request, or an operation that ended Failed with an errorCode that is not about the moment (a transient code, a
    cancelled operation or no code at all is not a refusal). A failure of the staging upload, of a listing or of anything else never is."""
    if not getattr(ex, "from_load_call", False):
        return False
    if isinstance(ex, oac.OperationFailed):
        return str(ex).startswith("operation Failed") and bool(operation_code(ex)) and not _operation_is_about_the_moment(ex)
    return getattr(ex, "status", None) in REJECTION_STATUS


def is_retryable(ex: BaseException) -> bool:
    """Worth trying again after a back-off: a transient failure, or a conflict (another operation is running on the lakehouse)."""
    return is_transient(ex) or getattr(ex, "status", None) == 409


class FabricCapacitySkuMismatch(oac.FabricError):
    """Fabric lists the capacity with another size than this build is configured for (F4 unless deliberately overridden)."""


class LayerLoadError(oac.FabricError):
    """A layer could not be loaded. `table` is the first table that failed (after its retries; None when the layer's tables loaded but could not be
    verified), `loaded` the tables of the layer that did load, `remaining` the ones that did not (the failed ones and those never tried). `rejected` are
    the failed tables the Load Table API itself refused: the only ones a fallback loader may take over. `transient` says the load stopped for a reason
    that is not about the tables (throttling, a busy or unreachable service, a table list that could not be read): trying again later is the answer."""

    def __init__(self, layer: str, table: Optional[str], detail: str, loaded: Iterable[str] = (), remaining: Iterable[str] = (), rejected: Iterable[str] = (),
                 transient: bool = False):
        super().__init__(f"{layer} table {table}: {detail}" if table else f"{layer} layer: {detail}")
        self.layer, self.table, self.detail = layer, table, detail
        self.loaded, self.remaining, self.rejected, self.transient = sorted(loaded), list(remaining), list(rejected), transient


def _mmss(seconds: float) -> str:
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


def describe_layer(rep: dict) -> str:
    """One layer of `Ops.layer_report` as a line: tables registered, bronze files held in OneLake, and what is missing."""
    text = f"{rep['present']}/{rep['expected']} tables registered"
    files = rep.get("files")
    if files is not None:
        text += f", {files['present']}/{files['expected']} files in OneLake" if files["present"] is not None else f", {files['expected']} files planned (OneLake did not list them)"
    if rep["missing"]:
        text += f"; MISSING {', '.join(rep['missing'][:5])}" + (f" and {len(rep['missing']) - 5} more" if len(rep["missing"]) > 5 else "")
    return text


class Ops:
    def __init__(self, fab: oac.Fabric, token_fn: Callable[[str], str], log: Callable[[str], None] = print,
                 sleeper: Optional[Callable[[float], None]] = None, dfs_base: str = ONELAKE_DFS, rng: Callable[[], float] = random.random):
        self.fab, self.token_fn, self.log = fab, token_fn, log
        self.sleep = sleeper or time.sleep
        self.dfs = dfs_base.rstrip("/")
        self.rng = rng                                                                # jitter of the back-off between tries (a test passes a constant)
        self.retry_budget_s = RETRY_BUDGET_S                                          # one table's (or file's) retries in all end here, tries and waits alike

    def clock(self) -> float:
        """Monotonic time for deadlines: the Fabric client's clock, so that a virtual clock injected there serves both."""
        return self.fab.clock() if hasattr(self.fab, "clock") else time.monotonic()

    # ---- capacity / workspace -------------------------------------------------------------------
    def find_capacity(self, display_name: str, wait_s: float = 0.0, step_s: float = 10.0) -> dict:
        """The capacity as Fabric lists it. A capacity Azure created a moment ago can take a few minutes to show up in Fabric, so a
        build passes wait_s; everything else fails at once."""
        waited, said = 0.0, False
        while True:
            body = self.fab._json(self.fab.call("GET", f"{self.fab.api}/capacities")) or {}
            for c in body.get("value", []):
                if c.get("displayName", "").lower() == display_name.lower():
                    return c
            if waited >= wait_s:
                raise oac.FabricError(f"capacity '{display_name}' is not visible to you (needs capacity admin or contributor); found "
                                      f"{[c.get('displayName') for c in body.get('value', [])]}")
            if not said:
                self.log(f"  capacity {display_name} is not listed by Fabric yet (a new capacity can take a few minutes); waiting")
                said = True
            self.sleep(step_s)
            waited += step_s

    def find_workspace(self, name: str) -> Optional[dict]:
        body = self.fab._json(self.fab.call("GET", f"{self.fab.api}/workspaces")) or {}
        return next((w for w in body.get("value", []) if w.get("displayName") == name), None)

    def ensure_workspace(self, name: str, capacity_name: str, description: str = "", sku: Optional[str] = None) -> dict:
        """The workspace, on the named capacity. When `sku` is given Fabric itself must report that size for the capacity (F4 for this
        demo), and once the workspace is created or assigned Fabric must report it on that capacity: the semantic model, ontology and
        graph bill to the capacity their workspace is on."""
        cap = self.find_capacity(capacity_name, wait_s=240 if sku else 0)                       # a build (sku given) waits for a brand-new capacity to be listed
        if cap.get("state") not in (None, "Active"):
            raise oac.FabricError(f"capacity {capacity_name} is {cap.get('state')}; resume it first")
        if sku and cap.get("sku") and cap["sku"] != sku:
            raise FabricCapacitySkuMismatch(f"Fabric reports capacity {capacity_name} as {cap['sku']}, but this build is configured for {sku}. "
                                            f"Resize it back with `bash build.sh --helper capacity scale --sku {sku} --execute`; this kit never resizes a capacity by itself")
        ws = self.find_workspace(name)
        created = ws is None
        if ws:
            on = ws.get("capacityId")
            if on and on != cap["id"]:
                raise oac.FabricError(f"workspace '{name}' already exists on a different capacity ({on}); this kit will not move it. "
                                      f"Pick another workspace name (CONTOSO_WORKSPACE_NAME) or move the workspace yourself")
            if not on:
                self.fab.call("POST", f"{self.fab.api}/workspaces/{ws['id']}/assignToCapacity", json={"capacityId": cap["id"]})
                self.log(f"  assigned workspace '{name}' to capacity {capacity_name}")
        else:
            resp = self.fab.call("POST", f"{self.fab.api}/workspaces", json={"displayName": name, "description": description[:256], "capacityId": cap["id"]})
            ws = self.fab._json(resp) or {}
            self.log(f"  created workspace '{name}' ({ws.get('id')})")
        self.confirm_on_capacity(ws["id"], cap)
        return {"workspaceId": ws["id"], "workspaceName": name, "capacityId": cap["id"], "created": created,
                "capacitySku": cap.get("sku"), "capacityState": cap.get("state")}

    def confirm_on_capacity(self, ws_id: str, cap: dict, wait_s: float = 90.0, step_s: float = 3.0) -> None:
        """Fabric must report the workspace on this capacity (an assignment completes a moment after the call returns)."""
        waited = 0.0
        while True:
            seen = (self.fab._json(self.fab.call("GET", f"{self.fab.api}/workspaces/{ws_id}")) or {}).get("capacityId")
            if seen == cap["id"]:
                return
            if waited >= wait_s:
                raise oac.FabricError(f"workspace {ws_id} is on capacity {seen or 'none'}, not {cap.get('displayName')} ({cap['id']}), "
                                      f"{int(wait_s)} s after it was assigned")
            self.sleep(step_s)
            waited += step_s

    def add_members(self, ws_id: str, members: list[dict]) -> None:
        for m in members:
            body = {"principal": {"id": m["id"], "type": m.get("type", "User")}, "role": m.get("role", "Contributor")}
            self.fab.call("POST", f"{self.fab.api}/workspaces/{ws_id}/roleAssignments", json=body, ok=(200, 201, 409))
            self.log(f"  role {body['role']} for {m['id']}")

    def delete_workspace(self, ws_id: str) -> None:
        self.fab.call("DELETE", f"{self.fab.api}/workspaces/{ws_id}", ok=(200, 202, 204, 404))

    # ---- hard purge -------------------------------------------------------------------------------
    def list_items(self, ws_id: str) -> list[dict]:
        return self.fab.list_items(ws_id, "items")

    def list_recoverable(self, ws_id: str) -> list[dict]:
        """Soft-deleted items still held in the workspace recycle bin. An error means the feature is unavailable: nothing to purge."""
        try:
            return self.fab.list_items(ws_id, "recoverableItems")
        except oac.FabricError as ex:
            self.log(f"  recycle bin not listed ({str(ex)[:120]}); treating it as empty")
            return []

    def hard_delete_item(self, ws_id: str, item: dict) -> str:
        """Permanently delete one item: ?hardDelete=true skips the recycle bin for item types that support soft delete. Types that
        do not support it (or tenants with item recovery off) are irreversibly deleted by a plain DELETE already. Needs workspace Admin."""
        url = f"{self.fab.api}/workspaces/{ws_id}/items/{item['id']}"
        mode = "hard"
        try:
            resp = self.fab.call("DELETE", url + "?hardDelete=true", ok=(200, 202, 204, 404))
        except oac.FabricError as ex:
            if not any(code in str(ex) for code in SOFT_DELETE_UNAVAILABLE):
                raise
            resp = self.fab.call("DELETE", url, ok=(200, 202, 204, 404))
            mode = "plain"
        if resp.status_code == 202:
            self.fab.lro(resp)
        return "gone" if resp.status_code == 404 else mode

    def purge_recoverable(self, ws_id: str, item_id: str) -> None:
        self.fab.call("DELETE", f"{self.fab.api}/workspaces/{ws_id}/recoverableItems/{item_id}", ok=(200, 202, 204, 404))

    def rename_workspace(self, ws_id: str, name: str) -> None:
        self.fab.call("PATCH", f"{self.fab.api}/workspaces/{ws_id}", json={"displayName": name})

    def purge_workspace(self, ws_id: str, passes: int = 4) -> dict:
        """Hard-delete everything in the workspace (dependants first, several passes), empty its recycle bin, rename the workspace so the
        name is free for the next build, then delete it. Fabric has no API that permanently deletes a *workspace*: the empty, renamed
        shell stays 'Deleted' for the tenant's retention period (7 days minimum) unless a tenant admin purges it in the admin portal.
        Nothing recoverable is left inside it; OneLake keeps the bytes of hard-deleted items for 7 more days, unrecoverable."""
        rep: dict[str, Any] = {"itemsDeleted": [], "recoverablePurged": [], "itemsLeft": [], "recoverableLeft": [], "errors": [],
                               "renamedTo": None, "workspaceDeleted": False}
        errors: dict[str, str] = {}
        for _ in range(passes):
            items = [i for i in self.list_items(ws_id) if i.get("type") not in CHILD_TYPES]
            if not items:
                break
            items.sort(key=lambda i: (DELETE_RANK.get(i.get("type"), DEFAULT_RANK), i.get("displayName", "")))
            done = 0
            for it in items:
                try:
                    mode = self.hard_delete_item(ws_id, it)
                    rep["itemsDeleted"].append(f"{it.get('type')}:{it.get('displayName')} ({mode})")
                    errors.pop(it["id"], None)
                    done += 1
                except oac.FabricError as ex:
                    errors[it["id"]] = f"{it.get('type')}:{it.get('displayName')}: {str(ex)[:160]}"
            if not done:
                break
            self.sleep(3)                                                         # let the listing catch up before the next pass
        for r in self.list_recoverable(ws_id):
            try:
                self.purge_recoverable(ws_id, r["id"])
                rep["recoverablePurged"].append(f"{r.get('type')}:{r.get('displayName')}")
            except oac.FabricError as ex:
                errors[r["id"]] = f"recycle bin {r.get('type')}:{r.get('displayName')}: {str(ex)[:160]}"
        rep["itemsLeft"] = [f"{i.get('type')}:{i.get('displayName')}" for i in self.list_items(ws_id) if i.get("type") not in CHILD_TYPES]
        rep["recoverableLeft"] = [f"{r.get('type')}:{r.get('displayName')}" for r in self.list_recoverable(ws_id)]
        tomb = f"zz-wiped-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-{ws_id[:8]}"
        try:
            self.rename_workspace(ws_id, tomb)
            rep["renamedTo"] = tomb
        except oac.FabricError as ex:
            errors["rename"] = f"could not rename the workspace before deleting it ({str(ex)[:120]}); its name may stay reserved during retention"
        self.delete_workspace(ws_id)
        rep["workspaceDeleted"] = not self.workspace_exists(ws_id)
        rep["errors"] = list(errors.values())
        return rep

    def delete_item(self, ws_id: str, collection: str, item_id: str) -> None:
        """Permanently delete one item of an adopted workspace (type from the collection name)."""
        self.hard_delete_item(ws_id, {"id": item_id, "type": COLLECTION_TYPE.get(collection, collection)})

    def workspace_exists(self, ws_id: str) -> bool:
        return any(w.get("id") == ws_id for w in (self.fab._json(self.fab.call("GET", f"{self.fab.api}/workspaces")) or {}).get("value", []))

    def tenant_settings(self, keywords: tuple[str, ...] = ("ontology", "create fabric items", "data agent", "azure openai", "copilot")) -> Optional[list[dict]]:
        """Best effort read of the Fabric tenant settings relevant to the ontology. Needs a Fabric admin; None when not readable."""
        try:
            body = self.fab._json(self.fab.call("GET", f"{self.fab.api}/admin/tenantsettings")) or {}
        except oac.FabricError:
            return None
        out = []
        for s in body.get("tenantSettings", []):
            hay = f"{s.get('settingName', '')} {s.get('title', '')}".lower()
            if any(k in hay for k in keywords):
                out.append({"name": s.get("settingName"), "title": s.get("title"), "enabled": s.get("enabled")})
        return out

    # ---- lakehouse ------------------------------------------------------------------------------
    def ensure_lakehouse(self, ws_id: str, name: str, wait_sql: bool = True) -> dict:
        lh = self.fab.find(ws_id, "lakehouses", name)
        created = lh is None
        if created:
            # a plain (schema-less) lakehouse: the API accepts enableSchemas=true or no creation payload at all, never enableSchemas=false
            lh_id = self.fab.create(ws_id, "lakehouses", {"displayName": name, "description": "Contoso bank gold layer (ontology demo)"})
            self.log(f"  created lakehouse '{name}' ({lh_id})")
        else:
            lh_id = lh["id"]
        for _ in range(60 if wait_sql else 1):
            detail = self.fab._json(self.fab.call("GET", f"{self.fab.api}/workspaces/{ws_id}/lakehouses/{lh_id}")) or {}
            sql = (detail.get("properties") or {}).get("sqlEndpointProperties") or {}
            if sql.get("provisioningStatus") in (None, "Success") and (sql.get("connectionString") or not wait_sql):
                return {"lakehouseId": lh_id, "lakehouseName": name, "sqlEndpoint": sql.get("connectionString"), "sqlDatabase": sql.get("id"), "created": created}
            self.sleep(10)
        raise oac.FabricError("SQL analytics endpoint did not finish provisioning")

    # ---- OneLake upload + Load Table API --------------------------------------------------------
    def upload(self, ws_id: str, lh_id: str, rel_path: str, data: bytes) -> None:
        import requests
        url = f"{self.dfs}/{ws_id}/{lh_id}/{rel_path.lstrip('/')}"
        h = {"Authorization": f"Bearer {self.token_fn(STORAGE_SCOPE)}", "x-ms-version": "2023-11-03"}
        for step, req in (("create", lambda: requests.put(f"{url}?resource=file", headers={**h, "Content-Length": "0"}, timeout=120)),
                          ("append", lambda: requests.patch(f"{url}?action=append&position=0", headers={**h, "Content-Type": "application/octet-stream"},
                                                            data=data, timeout=300)),
                          ("flush", lambda: requests.patch(f"{url}?action=flush&position={len(data)}", headers=h, timeout=120))):
            self._dfs_call(req, f"{step} {rel_path}")

    def _dfs_call(self, send: Callable[[], Any], what: str, ok: tuple[int, ...] = (200, 201, 202)) -> Any:
        """One OneLake request. A throttled or server-error answer is tried again, up to DFS_TRIES in all, after a back-off that honours its Retry-After; any
        other status outside `ok` is an error carrying it. A dropped connection is not retried here: the item that made the request is, from its start."""
        for attempt in range(DFS_TRIES):
            r = send()
            asked = oac.retry_after_seconds(r.headers.get("Retry-After"))
            if r.status_code in (*oac.RETRY_STATUS, 500) and attempt < DFS_TRIES - 1:
                wait = oac.backoff_delay(attempt, asked, rng=self.rng)
                if not self.fab.out_of_time(wait):                                 # the retry budget of the item that is making this request
                    self.log(f"  OneLake {what}: {r.status_code}; trying again in {wait:.0f}s")
                    self.sleep(wait)
                    continue
            if r.status_code not in ok:
                raise oac.FabricError(f"OneLake {what}: {r.status_code} {r.text[:300]}", status=r.status_code, retry_after=asked)
            return r
        raise oac.FabricError(f"OneLake {what}: too many retries")

    def load_table(self, ws_id: str, lh_id: str, table: str, rel_path: str) -> None:
        body = {"relativePath": rel_path, "pathType": "File", "mode": "Overwrite", "recursive": False, "formatOptions": {"format": "Parquet"}}
        try:
            resp = self.fab.call("POST", f"{self.fab.api}/workspaces/{ws_id}/lakehouses/{lh_id}/tables/{table}/load", json=body)
        except oac.FabricError as ex:
            ex.from_load_call = True                                               # the load request itself was answered: see is_rejection
            raise
        try:
            self.fab.lro(resp)
        except oac.OperationFailed as ex:
            ex.from_load_call = True                                               # the operation the load started ended Failed or Cancelled
            raise

    def list_tables(self, ws_id: str, lh_id: str) -> list[dict]:
        out, token = [], None
        while True:
            url = f"{self.fab.api}/workspaces/{ws_id}/lakehouses/{lh_id}/tables" + (f"?continuationToken={urllib.parse.quote(token, safe='')}" if token else "")
            body = self.fab._json(self.fab.call("GET", url)) or {}
            out += body.get("data", body.get("value", []))
            token = body.get("continuationToken")
            if not token:
                return out

    def _run(self, items: list, fn: Callable[[Any], Any], workers: int, key: Callable[[Any], str]) -> tuple[list, list[tuple[Any, Exception]], list]:
        """Run fn over items, a few at a time, and return (results, failures, untried). An item that fails while others run is tried again on its own
        afterwards, one at a time (concurrent calls can collide, and a retry surfaces the real error with its own message): a failure worth waiting for
        (throttling, a busy or unreachable service, a conflict) is tried again after a back-off, up to SERIAL_TRIES in all, any other failure once. Every
        item has ONE retry budget (self.retry_budget_s from its first try, tries, requests and waits alike): when it is spent the failure is final. An item
        that still fails is in `failures` with its error. A failure that is not the work being refused (see is_rejection) ends the run, since the items
        after it would fare no better: they are `untried`. In the parallel round that is `workers` such failures in a row (a conflict says nothing either
        way): the queued tasks are cancelled, the running ones finish, and nothing is tried again. Ctrl+C (any BaseException) cancels what is queued, lets
        what is running finish, and is raised."""
        done: list = []
        budgets: dict[str, float] = {}                                         # item key -> the time its retries end

        def attempt(it: Any) -> Any:
            with self.fab.retry_until(budgets.setdefault(key(it), self.clock() + self.retry_budget_s)):
                return fn(it)

        retry: list[tuple[Any, Optional[Exception]]] = [(it, None) for it in items]
        untried: list = []
        if workers > 1 and len(items) > 1:
            self.token_fn(STORAGE_SCOPE)                                       # warm the token cache once instead of once per thread
            self.token_fn(oac.FABRIC_SCOPE)
            retry = []
            pool = ThreadPoolExecutor(max_workers=min(workers, len(items)))
            futures: dict = {}
            streak, tripped = 0, False

            def collect(fut: Any) -> None:
                nonlocal streak, tripped
                it = futures[fut]
                try:
                    done.append(fut.result())
                    streak = 0
                except Exception as ex:  # noqa: BLE001 - concurrent calls can collide; each failure gets its serial tries below
                    retry.append((it, ex))
                    streak = 0 if is_rejection(ex) else streak + (getattr(ex, "status", None) != 409)
                    trips = streak >= workers and not tripped
                    self.log(f"  {key(it)}: {str(ex)[:160]}" + ("" if tripped or trips else "; retrying on its own"))
                    if trips:
                        tripped = True
                        self.log(f"  {streak} loads in a row failed for a reason that is not about the tables: the queued ones are cancelled, nothing more is started or tried again")
                        pool.shutdown(wait=False, cancel_futures=True)

            try:
                futures.update({pool.submit(attempt, it): it for it in items})
                seen: set = set()
                for fut in as_completed(futures):
                    seen.add(fut)
                    collect(fut)
                    if tripped:
                        break
                if tripped:
                    untried = [futures[f] for f in futures if f.cancelled()]
                    for fut in as_completed([f for f in futures if f not in seen and not f.cancelled()]):           # the ones that were running finish
                        collect(fut)
            except BaseException as ex:
                pool.shutdown(wait=False, cancel_futures=True)                 # leaving a `with` block would wait for every queued task to run first
                if isinstance(ex, KeyboardInterrupt):
                    running = sum(1 for f in futures if f.running())
                    self.log("  interrupted: " + (f"waiting for {running} running loads to finish (up to {max(1, round(self.fab.max_wait / 60))} min); " if running else "")
                             + "queued loads were cancelled")
                raise
            pool.shutdown()
            if tripped:
                return done, sorted(((it, ex) for it, ex in retry if ex is not None), key=lambda r: key(r[0])), sorted(untried, key=key)
        failures: list[tuple[Any, Exception]] = []
        ordered = sorted(retry, key=lambda r: key(r[0]))
        for i, (it, err) in enumerate(ordered):
            try:
                done.append(self._again(it, attempt, key, err, budgets))
            except Exception as ex:  # noqa: BLE001 - reported by the caller with the item's name
                failures.append((it, ex))
                if not is_rejection(ex):
                    untried = [r[0] for r in ordered[i + 1:]]
                    break
        return done, failures, untried

    def _again(self, it: Any, fn: Callable[[Any], Any], key: Callable[[Any], str], err: Optional[Exception], budgets: dict[str, float]) -> Any:
        """fn(it) on its own. `err` is its failure in the parallel round, if it had one; that counts as the first try. A retryable failure is tried
        again after a back-off (exponential with jitter, never shorter than the service's Retry-After), up to SERIAL_TRIES in all and as long as the
        item's retry budget lasts; any other failure gets one more try at once and is then raised."""
        tries = 0 if err is None else 1
        while True:
            if err is not None:
                retryable = is_retryable(err)
                if tries >= (SERIAL_TRIES if retryable else 2):
                    raise err
                if retryable:
                    wait = oac.backoff_delay(tries - 1, getattr(err, "retry_after", None), rng=self.rng)
                    if self.clock() + wait > budgets.get(key(it), float("inf")):
                        raise err                                              # this wait would end past the item's retry budget: its failure is final
                    self.log(f"  {key(it)}: {str(err)[:120]}; trying again in {wait:.0f}s")
                    self.sleep(wait)
            try:
                return fn(it)
            except Exception as ex:  # noqa: BLE001 - classified at the top of the loop
                err, tries = ex, tries + 1

    def load_layer(self, layer_dir: Path, ws_id: str, lh_id: str, staging: str = "Files/staging", only: Optional[Iterable[str]] = None, workers: int = 4,
                   layer: Optional[str] = None) -> dict:
        """Upload each Parquet file of one layer's folder to OneLake and load it as a managed Delta table (the file stem is the table name; `only`
        limits which). Tables are independent, so a few load at once (each Load Table call is a long-running operation); a table that fails while
        others run is tried again on its own (see _run), and one that still fails raises LayerLoadError naming it, with the tables the API itself
        refused in `rejected`. Mode is Overwrite, so a re-run is safe. The result lists the loaded tables and those the lakehouse did not list
        afterwards; a listing that fails for a moment is tried again for a few minutes (see wait_registered), it never turns into "missing"."""
        label = layer or Path(layer_dir).name
        wanted = set(only) if only else None
        files = [f for f in sorted(Path(layer_dir).glob("*.parquet")) if wanted is None or f.stem in wanted]

        def one(f: Path) -> str:
            rel = f"{staging}/{f.name}"
            try:
                self.upload(ws_id, lh_id, rel, f.read_bytes())
            except oac.FabricError as ex:                                      # the load request was never made: never a reason for delta-rs
                raise oac.FabricError(f"the staging upload of {f.name} to {rel} failed: {ex}", status=ex.status, retry_after=ex.retry_after) from ex
            self.load_table(ws_id, lh_id, f.stem, rel)
            self.log(f"  loaded {f.stem} ({f.stat().st_size:,} bytes)")
            return f.stem

        done, failures, untried = self._run(files, one, workers, key=lambda f: f.stem)
        if failures:
            stopper = next(((f, e) for f, e in failures if not is_rejection(e)), None)             # a failure that is not a refusal ended the run: it is the one to name
            first, ex = stopper or failures[0]
            raise LayerLoadError(label, first.stem, str(ex)[:400], loaded=done, remaining=[f.stem for f, _ in failures] + [f.stem for f in untried],
                                 rejected=[f.stem for f, e in failures if is_rejection(e)], transient=stopper is not None and is_transient(ex)) from ex
        done.sort()
        return {"loaded": done, "missingAfterLoad": self._verified(label, done, ws_id, lh_id, 0.0)}

    def load_gold(self, gold_dir: Path, ws_id: str, lh_id: str, staging: str = "Files/staging", only: Optional[list[str]] = None,
                  workers: int = 4) -> dict:
        """The gold layer alone (the star schema of out/gold): load_layer."""
        return self.load_layer(gold_dir, ws_id, lh_id, staging, only, workers, layer="gold")

    def load_layer_delta(self, layer_dir: Path, ws_id: str, lh_id: str, only: Optional[Iterable[str]] = None, uri_base: Optional[str] = None,
                         storage_options: Optional[dict] = None, layer: Optional[str] = None) -> dict:
        """Fallback loader: write each table of a layer as Delta straight into the lakehouse Tables folder with delta-rs. Use it if the Load
        Table API rejects a file. The Arrow table goes in exactly as the Parquet file holds it, so decimal128 columns (silver's official-name
        aliases) keep their precision and scale. uri_base/storage_options are overridable so it can be tested on local disk."""
        import pyarrow.parquet as pq
        from deltalake import write_deltalake
        label = layer or Path(layer_dir).name
        base = uri_base or f"abfss://{ws_id}@onelake.dfs.fabric.microsoft.com/{lh_id}/Tables"
        opts = storage_options if storage_options is not None else {"bearer_token": self.token_fn(STORAGE_SCOPE), "use_fabric_endpoint": "true"}
        wanted = set(only) if only else None
        files = [f for f in sorted(Path(layer_dir).glob("*.parquet")) if wanted is None or f.stem in wanted]
        done: list[str] = []
        for i, f in enumerate(files):
            try:
                write_deltalake(f"{base}/{f.stem}", pq.read_table(f), mode="overwrite", storage_options=opts or None)
            except Exception as ex:  # noqa: BLE001 - named for the caller; the tables after it were not tried
                raise LayerLoadError(label, f.stem, str(ex)[:400], loaded=done, remaining=[g.stem for g in files[i:]]) from ex
            self.log(f"  wrote delta table {f.stem}")
            done.append(f.stem)
        return {"loaded": done, "missingAfterLoad": []}

    def load_gold_delta(self, gold_dir: Path, ws_id: str, lh_id: str, only: Optional[list[str]] = None,
                        uri_base: Optional[str] = None, storage_options: Optional[dict] = None) -> dict:
        """The gold layer alone, through the delta-rs fallback: load_layer_delta."""
        return self.load_layer_delta(gold_dir, ws_id, lh_id, only, uri_base, storage_options, layer="gold")

    # ---- bronze: the seed files, verbatim ---------------------------------------------------------
    def _dfs_headers(self) -> dict:
        return {"Authorization": "Bearer " + self.token_fn(STORAGE_SCOPE), "x-ms-version": "2023-11-03"}

    def read_file(self, ws_id: str, lh_id: str, rel_path: str) -> Optional[bytes]:
        """The bytes of a OneLake file, or None when there is no file at that path."""
        import requests
        r = self._dfs_call(lambda: requests.get(f"{self.dfs}/{ws_id}/{lh_id}/{rel_path.lstrip('/')}", headers=self._dfs_headers(), timeout=300), f"read {rel_path}", ok=(200, 404))
        return None if r.status_code == 404 else r.content

    def list_files(self, ws_id: str, lh_id: str, folder: str = BRONZE_FOLDER) -> Optional[dict[str, int]]:
        """{path relative to the lakehouse root: size in bytes} of every file under a OneLake folder; {} when the folder does not exist yet; None
        when OneLake would not list it (permissions, an API difference): callers then report the files as not checked."""
        import requests
        out: dict[str, int] = {}
        token = None
        try:
            while True:
                params = {"resource": "filesystem", "directory": f"{lh_id}/{folder.strip('/')}", "recursive": "true", **({"continuation": token} if token else {})}
                r = self._dfs_call(lambda: requests.get(f"{self.dfs}/{ws_id}", params=params, headers=self._dfs_headers(), timeout=120), f"list {folder}", ok=(200, 404))
                if r.status_code == 404:
                    return out
                for p in r.json().get("paths", []):
                    if str(p.get("isDirectory", "false")).lower() != "true":
                        name = p["name"]
                        out[name[len(lh_id) + 1:] if name.startswith(lh_id + "/") else name] = int(p.get("contentLength") or 0)
                token = r.headers.get("x-ms-continuation")
                if not token:
                    return out
        except oac.FabricError as ex:
            self.log(f"  OneLake did not list {folder}: {str(ex).split(': ', 1)[-1][:160]}")
            return None
        except (requests.RequestException, ValueError, KeyError) as ex:
            self.log(f"  OneLake did not list {folder}: {str(ex)[:160]}")
            return None

    def upload_bronze(self, files: Iterable[PlannedFile], ws_id: str, lh_id: str, workers: int = 4, verify: bool = True) -> dict:
        """Upload the seed JSON files verbatim to Files/bronze/<release>/<repo path> (PlannedFile.target). The staged copy is checked against the
        bronze manifest first (size and sha256), and with `verify` each upload is read back from OneLake and checked the same way: a file that
        is missing or different there is an error, a read-back OneLake cannot answer (permissions) is logged and reported as unverified. Files
        are independent, so a few go at once; one that fails is retried on its own, then raises. Returns {"uploaded": [targets], "verified": n,
        "unverified": [targets]}."""
        todo = sorted(files, key=lambda f: f.target)

        def one(f: PlannedFile) -> tuple[str, bool]:
            if not f.local.is_file():
                raise oac.FabricError(f"bronze file {f.path} is not staged at {f.local}; run `build` again (it copies the seed files the manifest names)")
            data = f.local.read_bytes()
            if len(data) != f.bytes or hashlib.sha256(data).hexdigest() != f.sha256:
                raise oac.FabricError(f"the staged copy of {f.path} is not what the bronze manifest says ({len(data)} bytes, expected {f.bytes}); run `build` again")
            self.upload(ws_id, lh_id, f.target, data)
            if not verify:
                return f.target, False
            try:
                back = self.read_file(ws_id, lh_id, f.target)
            except Exception as ex:  # noqa: BLE001 - the read-back is a check on top of the upload, not part of it
                self.log(f"  could not read {f.target} back to verify it: {str(ex)[:160]}")
                return f.target, False
            if back is None:
                raise oac.FabricError(f"uploaded {f.target} but OneLake does not have a file at that path")
            if len(back) != f.bytes or hashlib.sha256(back).hexdigest() != f.sha256:
                raise oac.FabricError(f"OneLake holds {len(back)} bytes at {f.target}, not the {f.bytes} bytes (sha256 {f.sha256[:12]}...) of the manifest")
            return f.target, True

        done, failures, _ = self._run(todo, one, workers, key=lambda f: f.target)
        if failures:
            f, ex = failures[0]
            raise oac.FabricError(f"bronze file {f.path}: {str(ex)[:400]}", status=getattr(ex, "status", None), retry_after=getattr(ex, "retry_after", None)) from ex
        done.sort()
        self.log(f"  uploaded {len(done)} bronze files ({sum(f.bytes for f in todo):,} bytes) to {BRONZE_FOLDER}/" + ("; each read back and checked against the manifest" if verify else ""))
        return {"uploaded": [t for t, _ in done], "verified": sum(1 for _, ok in done if ok), "unverified": [t for t, ok in done if not ok]}

    def registered_tables(self, ws_id: str, lh_id: str, retry_s: float = REGISTER_RETRY_S) -> set[str]:
        """The names of the lakehouse's tables. A listing that fails for a moment (throttling, a busy server, a dropped connection) says nothing about the
        tables: it is tried again, with back-off, before its error is raised, so a failed listing is never mistaken for missing tables. ONE deadline,
        retry_s from now on the Fabric client's monotonic clock, bounds all of it: the waits between listings and the back-off inside each listing call
        alike (and at most REGISTER_ATTEMPTS listings, whatever the clock says)."""
        until, attempt = self.clock() + retry_s, 0
        while True:
            try:
                with self.fab.retry_until(until):
                    return {t.get("name") for t in self.list_tables(ws_id, lh_id)}
            except Exception as ex:  # noqa: BLE001 - only a failure about the moment is tried again; any other is the caller's to report
                if not is_transient(ex) or self.clock() >= until or attempt + 1 >= REGISTER_ATTEMPTS:
                    raise
                wait = min(oac.backoff_delay(attempt, getattr(ex, "retry_after", None), cap=30.0, rng=self.rng), until - self.clock())
                attempt += 1
                self.log(f"  the lakehouse's table list could not be read ({str(ex)[:120]}); trying again in {wait:.0f}s")
                self.sleep(wait)

    def wait_registered(self, ws_id: str, lh_id: str, names: Iterable[str], timeout_s: float = 0.0, step_s: float = 10.0, retry_s: float = REGISTER_RETRY_S) -> list[str]:
        """The names the lakehouse still does not list as tables, after waiting up to timeout_s for them to appear (0: look once). The listing trails the
        load: tables written straight as Delta, by the fallback loader, take a minute or two to be picked up, and a table the Load Table API has just
        reported as loaded can be missing from the listing for a few seconds (seen live on the very first table). A listing that keeps failing for
        retry_s raises its error (see registered_tables)."""
        wanted, waited = list(names), 0.0
        while True:
            present = self.registered_tables(ws_id, lh_id, retry_s)
            missing = [n for n in wanted if n not in present]
            if not missing or waited >= timeout_s:
                return missing
            self.sleep(step_s)
            waited += step_s

    def _verified(self, layer: str, names: list[str], ws_id: str, lh_id: str, lag_s: float) -> list[str]:
        """The tables of a layer the lakehouse does not list, waiting up to lag_s for them; a listing that cannot be read is a LayerLoadError, not a verdict."""
        import requests
        try:
            return self.wait_registered(ws_id, lh_id, names, lag_s)
        except (oac.FabricError, requests.RequestException) as ex:
            raise LayerLoadError(layer, None, f"the {len(names)} tables were loaded, but the lakehouse's table list could not be read for {int(REGISTER_RETRY_S)} s "
                                 f"({str(ex)[:200]}); nothing is known to be missing, and loading again verifies them", loaded=names, transient=is_transient(ex)) from ex

    def load_layers(self, plan: LoadPlan, ws_id: str, lh_id: str, *, staging: str = "Files/staging", loader: str = "load-api", sql_endpoint_id: Optional[str] = None,
                    workers: int = 4, skip: Optional[Callable[[str], bool]] = None, started: Optional[Callable[[str], None]] = None,
                    finished: Optional[Callable[[str, dict], None]] = None, clock: Callable[[], float] = time.monotonic, register_wait_s: float = 180.0,
                    fallback: bool = True, uri_base: Optional[str] = None, storage_options: Optional[dict] = None, sync_wait_s: float = SYNC_WAIT_S) -> dict[str, dict]:
        """Put a LoadPlan in the lakehouse in layer order: bronze (the seed files, then the manifest table), silver, gold. Inside a layer the
        tables load side by side; the SQL analytics endpoint is asked to refresh once per layer; and every table of the layer must be registered
        before the next layer starts. The first layer that cannot be loaded raises LayerLoadError naming its first failing table, so nothing
        after it is started (silver before gold exists to bisect failures). The Load Table API is the default; with `fallback`, the tables it
        refuses itself (not the ones it could not be asked for) are written with delta-rs instead; `loader="delta"` uses delta-rs from the start.
        After the last layer that is loaded the refresh of the endpoint is followed for up to `sync_wait_s` (see await_sync): the ontology binds to the
        tables through that endpoint. `skip(layer)` leaves a layer alone, `started(layer)` / `finished(layer, result)` let the caller keep track
        of progress (a layer is finished once its tables are verified, before the endpoint is awaited)."""
        results: dict[str, dict] = {}
        todo = [layer for layer in plan.layers() if not (skip is not None and skip(layer))]
        for layer in plan.layers():
            if layer not in todo:
                results[layer] = {"skipped": True}
                continue
            if started is not None:
                started(layer)
            t0 = clock()
            res: dict[str, Any] = {}
            if layer == "bronze" and plan.files:
                try:
                    res["uploaded"] = self.upload_bronze(plan.files, ws_id, lh_id, workers=workers)
                except oac.FabricError as ex:
                    raise LayerLoadError(layer, None, str(ex), remaining=plan.names(layer), transient=is_transient(ex.__cause__ or ex) or is_transient(ex)) from ex
            loaded, _ = self._load_tables(plan, layer, ws_id, lh_id, staging, loader, workers, fallback, uri_base, storage_options)
            res.update(loaded)
            missing = self._verified(layer, plan.names(layer), ws_id, lh_id, register_wait_s)           # the API's tables show late too (live, first run: a single table, not listed yet)
            if missing:
                raise LayerLoadError(layer, missing[0], f"{len(missing)} {layer} tables are not registered in the lakehouse yet: {missing[:5]}; re-run build to resume",
                                     loaded=[n for n in plan.names(layer) if n not in missing], remaining=missing)
            refresh = self.refresh_sql_endpoint(ws_id, sql_endpoint_id)                # only now: the endpoint can pick up what the lakehouse lists, and a delta-rs table is listed late
            res.update(missingAfterLoad=[], seconds=round(clock() - t0, 1))
            results[layer] = res
            if finished is not None:
                finished(layer, res)
            if layer == todo[-1] and refresh is not None and sync_wait_s > 0:
                res["sqlSync"] = self.await_sync(refresh, sync_wait_s, expect=[n for lay in todo for n in plan.names(lay)])
        return results

    def _load_tables(self, plan: LoadPlan, layer: str, ws_id: str, lh_id: str, staging: str, loader: str, workers: int, fallback: bool, uri_base: Optional[str],
                     storage_options: Optional[dict]) -> tuple[dict, bool]:
        """The tables of one layer through the Load Table API, or delta-rs; the flag says whether delta-rs wrote any of them. delta-rs takes over only
        tables whose Load Table call the API itself refused (HTTP 400/415/422, or an operation that ended Failed with an error code that is not about
        the moment): a layer that stopped for any other reason (throttling, a busy or unreachable service, a capacity that is short of room, a staging
        upload that failed, a table list that could not be read, a sign-in that does not work) is not a reason to write tables another way, so that raises."""
        names, folder = plan.names(layer), plan.folder(layer)
        if not names:
            return {"loaded": []}, False
        if loader == "delta":
            return self.load_layer_delta(folder, ws_id, lh_id, names, uri_base, storage_options, layer=layer), True
        try:
            return self.load_layer(folder, ws_id, lh_id, staging, names, workers, layer=layer), False
        except LayerLoadError as ex:
            if not fallback or not ex.rejected or sorted(ex.rejected) != sorted(ex.remaining):
                raise
            left = ex.rejected
            self.log(f"  the Load Table API refused {len(left)} of the {len(names)} {layer} tables ({', '.join(left[:5])}{'...' if len(left) > 5 else ''}): "
                     f"{ex.detail[:200]}; writing only those with delta-rs, the other {len(ex.loaded)} stay as the API loaded them")
            try:
                import deltalake  # noqa: F401
            except ImportError:
                raise LayerLoadError(layer, left[0], f"the Load Table API refused it ({ex.detail[:160]}) and the delta-rs fallback is not installed in this Python environment; "
                                     f"install it with: pip install -r requirements-optional.txt (every build.sh run does that in its own environment), then run build.sh again",
                                     loaded=ex.loaded, remaining=left, rejected=left) from ex
            res = self.load_layer_delta(folder, ws_id, lh_id, left, uri_base, storage_options, layer=layer)
            return {"loaded": sorted({*ex.loaded, *res["loaded"]}), "missingAfterLoad": []}, True

    def layer_report(self, plan: LoadPlan, ws_id: str, lh_id: str) -> dict:
        """What the lakehouse holds of a plan, per layer: tables listed and missing, and for bronze how many of the planned files OneLake holds
        (None: OneLake did not list them). `tables` is the number of tables in the lakehouse, `other` those that are in no layer of the plan."""
        present = {t.get("name") for t in self.list_tables(ws_id, lh_id)}
        layers: dict[str, dict] = {}
        for layer in plan.layers():
            names = plan.names(layer)
            missing = [n for n in names if n not in present]
            rep: dict[str, Any] = {"expected": len(names), "present": len(names) - len(missing), "missing": missing}
            if layer == "bronze":
                found = self.list_files(ws_id, lh_id)
                rep["files"] = {"expected": len(plan.files), "present": None if found is None else sum(1 for f in plan.files if found.get(f.target) == f.bytes)}
            layers[layer] = rep
        return {"tables": len(present), "other": sorted(present - set(plan.all_names())), "layers": layers}

    def refresh_sql_endpoint(self, ws_id: str, sql_endpoint_id: Optional[str]) -> Optional[Any]:
        """Ask the SQL analytics endpoint to pick up newly loaded tables (preview API); the reply, or None when nothing was asked or it did not work.
        Best effort: a refusal, throttling or a timeout (requests raises those as exceptions that are not FabricErrors) never stops a load that worked."""
        if not sql_endpoint_id:
            return None
        try:
            return self.fab.call("POST", f"{self.fab.api}/workspaces/{ws_id}/sqlEndpoints/{sql_endpoint_id}/refreshMetadata", json={}, ok=(200, 202))
        except Exception as ex:  # noqa: BLE001 - best effort, see above
            self.log(f"  metadata refresh skipped: {str(ex)[:160]}")
            return None

    def await_sync(self, resp: Any, timeout_s: float = SYNC_WAIT_S, step_s: float = 10.0, expect: Iterable[str] = ()) -> str:
        """Follow the long-running operation a metadata refresh started (the Location of its 202 reply) for at most timeout_s, logging progress about
        once a minute, and say how it went: 'synced', 'failed', 'timeout' or 'unknown' (the refresh could not be followed). The ontology reads the lakehouse's
        tables through the SQL analytics endpoint (the semantic model reads OneLake directly), so the endpoint is where a freshly loaded table shows up
        last. Best effort: nothing in here raises. A reply that is already the answer (200) counts as 'synced'."""
        location = resp.headers.get("Location")
        if not location and resp.headers.get("x-ms-operation-id"):                    # a reply that names the operation by its id only
            location = f"{self.fab.api}/operations/{resp.headers['x-ms-operation-id']}"
        if resp.status_code != 202 or not location:
            self.log("  SQL endpoint sync: the refresh answered at once")
            return "synced"
        asked = oac.retry_after_seconds(resp.headers.get("Retry-After"))
        step = min(max(asked, 5.0), 30.0) if asked else step_s
        waited, note_at = 0.0, 60.0
        while True:
            try:
                state = self.fab._json(self.fab.call("GET", location)) or {}
            except Exception as ex:  # noqa: BLE001 - best effort
                self.log(f"  SQL endpoint sync: could not follow the refresh ({str(ex)[:140]}); the tables can take a few minutes more to show up there")
                return "unknown"
            status = state.get("status")
            if status == "Succeeded":
                self._sync_result(location, list(expect), waited)
                return "synced"
            if status in ("Failed", "Cancelled"):
                self.log(f"  SQL endpoint sync: the refresh ended {status}: {json.dumps(state.get('error', state))[:300]}; the tables can take a few minutes more to show up there")
                return "failed"
            if waited >= timeout_s:
                self.log(f"  SQL endpoint sync: still running after {_mmss(waited)}; carrying on. The ontology reads the tables through that endpoint: if creating it "
                         f"fails, wait a few minutes and run build.sh again (the load is not repeated)")
                return "timeout"
            self.sleep(step)
            waited += step
            if waited >= note_at:
                self.log(f"  SQL endpoint sync: the refresh is still running after {_mmss(waited)}")
                note_at += 60.0

    def _sync_result(self, location: str, expect: list[str], seconds: float) -> None:
        """Log what a finished metadata refresh reports per table (best effort; the shape of the preview API's result is not verified live)."""
        rows: Any = None
        try:
            reply = self.fab.call("GET", location.rstrip("/") + "/result", ok=(200, 204, 400, 404))
            rows = self.fab._json(reply) if reply.status_code == 200 else None
        except Exception:  # noqa: BLE001 - best effort
            rows = None
        rows = rows.get("value") if isinstance(rows, dict) else rows
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows) or not rows:
            self.log(f"  SQL endpoint sync: the refresh finished after {_mmss(seconds)}")
            return
        bad = [str(r.get("tableName") or r.get("name")) for r in rows if str(r.get("status", "Success")).lower() not in ("success", "succeeded", "notrun")]
        seen = {str(r.get("tableName") or r.get("name")) for r in rows}
        absent = [n for n in expect if n not in seen] if any(r.get("tableName") or r.get("name") for r in rows) else []
        self.log(f"  SQL endpoint sync: the refresh finished after {_mmss(seconds)}; it reports {len(rows)} tables"
                 + (f", {len(bad)} failed ({', '.join(bad[:5])})" if bad else "")
                 + (f", and does not list {len(absent)} of the planned tables yet ({', '.join(absent[:3])}{'...' if len(absent) > 3 else ''})" if absent else ""))

    def sync_sql_endpoint(self, ws_id: str, sql_endpoint_id: Optional[str], wait_s: float = 0.0) -> bool:
        """Best effort: ask the SQL analytics endpoint to pick up newly loaded tables (preview API) and, with wait_s, follow the refresh that long.
        True when the request was accepted. Never raises."""
        resp = self.refresh_sql_endpoint(ws_id, sql_endpoint_id)
        if resp is not None and wait_s > 0:
            self.await_sync(resp, wait_s)
        return resp is not None

    # ---- semantic model + ontology --------------------------------------------------------------
    def deploy_items(self, ws_id: str, spec: dict, sm_parts: dict, ont_parts: dict, replace: bool = False, refresh: bool = False,
                     backup_dir: Optional[Path] = None) -> dict:
        state: dict[str, str] = {"workspaceId": ws_id}
        sm_def = oac.to_payload(sm_parts, "TMDL")
        sm = self.fab.find(ws_id, "semanticModels", spec["semanticModel"]["name"])
        if not sm:
            sm_id = self.fab.create(ws_id, "semanticModels", {"displayName": spec["semanticModel"]["name"], "definition": sm_def})
            self.log(f"  created semantic model {sm_id}")
        else:
            sm_id = sm["id"]
            if replace:
                self.fab.update_definition(ws_id, "semanticModels", sm_id, sm_def)
                self.log(f"  replaced semantic model definition {sm_id}")
            else:
                self.log(f"  semantic model exists ({sm_id}); skipped (use --replace)")
        state["semanticModelId"] = sm_id
        if refresh:
            self.fab.refresh_model(ws_id, sm_id)
            self.log("  refresh requested")
        ont_def = oac.to_payload(ont_parts)
        ont = self.fab.find(ws_id, "ontologies", spec["ontology"]["name"])
        if not ont:
            ont_id = self.fab.create(ws_id, "ontologies", {"displayName": spec["ontology"]["name"],
                                                           "description": spec["ontology"].get("description", "")[:256], "definition": ont_def})
            self.log(f"  created ontology {ont_id}")
        else:
            ont_id = ont["id"]
            if replace:
                if backup_dir:
                    oac.write_parts(self.fab.get_definition(ws_id, "ontologies", ont_id), backup_dir)
                    self.log(f"  backed up live ontology definition to {backup_dir}")
                self.fab.update_definition(ws_id, "ontologies", ont_id, ont_def)
                self.log(f"  replaced ontology definition {ont_id}")
            else:
                self.log(f"  ontology exists ({ont_id}); skipped (use --replace; portal-authored content would be overwritten)")
        state["ontologyId"] = ont_id
        return state

    # ---- verification ---------------------------------------------------------------------------
    def verify_ontology(self, ws_id: str, ont_id: str, spec: dict) -> dict:
        parts = self.fab.get_definition(ws_id, "ontologies", ont_id)
        rep = oac.validate_parts(parts)
        live_entities = sorted(p.split("/")[1][:-5] for p in parts if p.startswith("entities/"))
        want = sorted(t["entity"] for t in spec["tables"] if t["ontology"])
        annotations = any("ONT_ItemId" in t for p, t in parts.items() if p.startswith("tables/"))
        return {"valid": rep.ok, "errors": rep.errors, "warnings": rep.warnings, "entitiesLive": len(live_entities),
                "entitiesExpected": len(want), "missingEntities": sorted(set(want) - set(live_entities)),
                "extraEntities": sorted(set(live_entities) - set(want)),
                "relationshipsLive": sum(1 for p in parts if p == "entityRelationships.tmdl" for ln in parts[p].splitlines() if ln.startswith("entityRelationship ")),
                "rulesLive": sum(1 for p in parts if p.startswith("rules/")),
                "graphAnnotationsPresent": annotations,
                "graphHint": "" if annotations else "No ONT_* partition annotations: every entity will show as graph-ineligible in the portal. Rebuild without --no-ont-annotations, or seed once in the portal (Generate ontology), export, then rebuild with --seed."}

    # ---- graph model: the child item Fabric creates with every ontology (live-verified 16 Oct 2026) --------
    def find_graph_model(self, ws_id: str, ontology_name: str, wait_s: float = 0.0, step_s: float = 10.0) -> Optional[dict]:
        """The GraphModel Fabric auto-creates next to an ontology (named <ontology>_graph_<id>); it can show up a little after the ontology."""
        prefix, waited = f"{ontology_name}_graph_", 0.0
        while True:
            gm = next((i for i in self.list_items(ws_id) if i.get("type") == "GraphModel" and str(i.get("displayName", "")).startswith(prefix)), None)
            if gm or waited >= wait_s:
                return gm
            self.sleep(step_s)
            waited += step_s

    def graph_jobs(self, ws_id: str, gm_id: str) -> list[dict]:
        body = self.fab._json(self.fab.call("GET", f"{self.fab.api}/workspaces/{ws_id}/items/{gm_id}/jobs/instances")) or {}
        return sorted(body.get("value", []), key=lambda j: j.get("startTimeUtc") or "")

    def apply_graph(self, ws_id: str, gm_id: str, parts: dict[str, str]) -> None:
        """Write the graph definition the portal's Configure graph wizard would write. Fabric then starts a refresh on its own."""
        self.fab.update_definition(ws_id, "graphModels", gm_id, oac.to_payload(parts, None))

    def refresh_graph(self, ws_id: str, gm_id: str, timeout_s: float = 900.0, step_s: float = 10.0) -> dict:
        """Refresh the graph and wait until it is done. A definition update already starts a refresh, so a second request is answered
        'Deduped'; the instance that is really running is followed instead."""
        resp = self.fab.call("POST", f"{self.fab.api}/workspaces/{ws_id}/graphModels/{gm_id}/jobs/refreshGraph/instances")
        job_id = (resp.headers.get("Location") or "").rstrip("/").rsplit("/", 1)[-1]
        waited = 0.0
        while waited <= timeout_s:
            job = (self.fab._json(self.fab.call("GET", f"{self.fab.api}/workspaces/{ws_id}/items/{gm_id}/jobs/instances/{job_id}")) if job_id else None) or {}
            status = job.get("status")
            if status == "Completed":
                return job
            if status in ("Failed", "Cancelled"):
                raise oac.FabricError(f"graph refresh {status}: {json.dumps(job.get('failureReason') or {})[:300]}")
            if status == "Deduped" or not job_id:
                live = [j for j in self.graph_jobs(ws_id, gm_id) if j.get("status") != "Deduped"]
                if live:
                    job_id = live[-1]["id"]
                    continue
            self.sleep(step_s)
            waited += step_s
        raise oac.FabricError(f"graph refresh did not finish within {int(timeout_s)} s; it may still be running (check the graph in the portal)")

    def graph_counts(self, ws_id: str, gm_id: str) -> dict:
        body = self.fab._json(self.fab.call("GET", f"{self.fab.api}/workspaces/{ws_id}/GraphModels/{gm_id}/getQueryableGraphType?preview=true")) or {}
        return {"nodeTypes": len(body.get("nodeTypes") or []), "edgeTypes": len(body.get("edgeTypes") or [])}


def mcp_tools_list(url: str, token: str, timeout: int = 60) -> dict:
    """Best-effort JSON-RPC tools/list against the ontology MCP endpoint (streamable HTTP). Falls back to a manual check."""
    import requests
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    try:
        init = requests.post(url, headers=h, timeout=timeout, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "contoso-onto-verify", "version": "1"}}})
        sid = init.headers.get("Mcp-Session-Id")
        if sid:
            h["Mcp-Session-Id"] = sid
        requests.post(url, headers=h, timeout=timeout, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = requests.post(url, headers=h, timeout=timeout, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        text = r.text
        if "data:" in text and not text.lstrip().startswith("{"):
            text = next((ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")), "{}")
        body = json.loads(text)
        tools = [t.get("name") for t in (body.get("result") or {}).get("tools", [])]
        return {"ok": r.status_code == 200 and bool(tools), "status": r.status_code, "tools": tools}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": str(ex)[:300], "note": "check manually in VS Code (MCP: Add Server) or Copilot Studio"}


def ask_foundry_iq(search_endpoint: str, kb: str, question: str, user_token: str, api_version: str = "2026-08-01-preview", timeout: int = 120) -> str:
    """Query the ontology knowledge base. The end user's Search token goes in x-ms-query-source-authorization (never stored)."""
    import requests
    r = requests.post(f"{search_endpoint.rstrip('/')}/knowledgebases/{kb}/retrieve?api-version={api_version}", timeout=timeout,
                      headers={"Content-Type": "application/json", "x-ms-query-source-authorization": user_token,
                               "Authorization": f"Bearer {user_token}"},
                      json={"messages": [{"role": "user", "content": [{"type": "text", "text": question}]}]})
    r.raise_for_status()
    texts: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            for k, v in n.items():
                if k in ("text", "fabricAnswer") and isinstance(v, str):
                    texts.append(v)
                else:
                    walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(r.json())
    return "\n".join(texts)
