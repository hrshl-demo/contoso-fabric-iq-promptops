"""Azure side of the demo: Fabric capacity (F4) lifecycle with a dry-run default.

Everything that changes Azure goes through Az.mutate() / Az.rest_mutate(), which only record the command unless
execute=True. Read-only calls (account show, capacity GET, name check, what-if, public retail prices) always run.

The capacity is managed with plain ARM REST through `az rest` (stable API 2023-11-01) and a compiled ARM template, so a
control host needs nothing beyond a signed-in Azure CLI: no `fabric` CLI extension and no Bicep download.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

Runner = Callable[[list[str]], tuple[int, str, str]]
INFRA = Path(__file__).resolve().parent.parent / "infra"
BICEP = str(INFRA / "fabric-capacity.bicep")
TEMPLATE = str(INFRA / "fabric-capacity.json")           # compiled from the Bicep (az bicep build); this is what the scripts deploy
ARM = "https://management.azure.com"
CAP_API = "2023-11-01"
CAPACITY_NAME_RE = re.compile(r"^[a-z][a-z0-9]{2,62}$")
SKUS = {"F2": 2, "F4": 4, "F8": 8, "F16": 16, "F32": 32, "F64": 64}
DEMO_SKU = "F4"                                           # the size this demo is built, tested and priced for (4 capacity units)
ALLOW_OTHER_SKU_ENV = "CONTOSO_ALLOW_OTHER_SKU"           # =1 builds on another size deliberately; without it anything but F4 is refused
PRICES_URL = "https://prices.azure.com/api/retail/prices"


class AzError(RuntimeError):
    @property
    def not_found(self) -> bool:
        s = str(self)
        return any(x in s for x in ("ResourceNotFound", "ResourceGroupNotFound", "NotFound", "was not found", "could not be found", "(404)"))


def default_runner(args: list[str]) -> tuple[int, str, str]:
    az = shutil.which("az") or shutil.which("az.cmd")
    if not az:
        return 127, "", "az CLI not found on PATH"
    p = subprocess.run([az, *args, "--only-show-errors"], capture_output=True, text=True, timeout=1800)
    return p.returncode, p.stdout, p.stderr


@dataclass
class Az:
    runner: Runner = default_runner
    execute: bool = False
    log: list[str] = field(default_factory=list)
    _who: Optional[dict] = None

    def _run(self, args: list[str], as_json: bool) -> Any:
        rc, out, err = self.runner(args)
        if rc != 0:
            raise AzError(f"az {' '.join(args[:3])} failed ({rc}): {err.strip()[:400]}")
        if not out.strip():
            return None if as_json else ""
        return json.loads(out) if as_json else out.strip()

    def read(self, args: list[str], as_json: bool = True) -> Any:
        self.log.append("az " + " ".join(args))
        return self._run(args, as_json)

    def mutate(self, args: list[str], as_json: bool = True) -> Any:
        self.log.append(("az " if self.execute else "[dry-run] az ") + " ".join(args))
        if not self.execute:
            return None
        return self._run(args, as_json)

    def _rest(self, method: str, url: str, body: Optional[dict], mutating: bool) -> Any:
        shown = f"rest --method {method} --url {url}" + (f" --body {json.dumps(body, separators=(',', ':'))}" if body is not None else "")
        if mutating and not self.execute:
            self.log.append("[dry-run] az " + shown)
            return None
        self.log.append("az " + shown)
        args = ["rest", "--method", method, "--url", url]
        tmp = None
        try:
            if body is not None:                                       # by file: JSON in argv is mangled by cmd.exe on Windows
                fd, tmp = tempfile.mkstemp(suffix=".json")
                os.write(fd, json.dumps(body).encode("utf-8"))
                os.close(fd)
                args += ["--body", f"@{tmp}"]
            return self._run(args, True)
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def rest_read(self, method: str, url: str, body: Optional[dict] = None) -> Any:
        return self._rest(method, url, body, mutating=False)

    def rest_mutate(self, method: str, url: str, body: Optional[dict] = None) -> Any:
        return self._rest(method, url, body, mutating=True)

    def who(self) -> dict:
        if self._who is None:
            acct = self.read(["account", "show", "-o", "json"])
            self._who = {"subscriptionId": acct["id"], "subscriptionName": acct["name"], "tenantId": acct["tenantId"],
                         "user": acct["user"]["name"], "userType": acct["user"]["type"]}
        return self._who


def validate_config(cfg: dict) -> list[str]:
    errs = []
    cap = cfg["capacity"]
    if not CAPACITY_NAME_RE.match(cap["name"]):
        errs.append(f"capacity name '{cap['name']}' must be 3-63 lowercase letters/digits, starting with a letter")
    if cap["sku"] not in SKUS:
        errs.append(f"capacity sku '{cap['sku']}' must be one of {sorted(SKUS)}")
    elif cap["sku"] != DEMO_SKU and not cap.get("allowOtherSku"):
        errs.append(f"capacity sku '{cap['sku']}' is not the {DEMO_SKU} this demo is built for: unset CONTOSO_CAPACITY_SKU (and capacity.sku in "
                    f"config/local.json) to use {DEMO_SKU}, or set {ALLOW_OTHER_SKU_ENV}=1 to build on {cap['sku']} deliberately")
    if not re.match(r"^[a-z]+[a-z0-9]*$", cfg["location"]):
        errs.append(f"location '{cfg['location']}' must be an Azure region name such as southindia")
    return errs


class CapacitySkuMismatch(RuntimeError):
    """The capacity that exists is not the size this build is configured for (F4 unless deliberately overridden). Never resized silently."""


def sku_mismatch(cfg: dict, live_sku: Optional[str]) -> Optional[str]:
    """None when the live capacity has the configured size; else why not, and how to put it right."""
    want = cfg["capacity"]["sku"]
    if not live_sku or live_sku == want:
        return None
    return (f"capacity {cfg['capacity']['name']} is {live_sku}, but this build is configured for {want}. Resize it back with "
            f"`bash build.sh --helper capacity scale --sku {want} --execute`, or keep {live_sku} deliberately with "
            f"{ALLOW_OTHER_SKU_ENV}=1 CONTOSO_CAPACITY_SKU={live_sku}. This kit never resizes a capacity by itself")


def signed_in(az: Az) -> dict:
    return az.who()


def capacity_admins(cfg: dict, who: dict) -> list[str]:
    return cfg["capacity"].get("admins") or ([who["user"]] if who["userType"] == "user" else [])


def arm_parameters(cfg: dict, admins: list[str]) -> dict:
    """ARM parameter file content (a file avoids shell-quoting of JSON arguments)."""
    p = {"capacityName": cfg["capacity"]["name"], "location": cfg["location"], "sku": cfg["capacity"]["sku"],
         "adminMembers": admins, "tags": cfg.get("tags", {})}
    return {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#", "contentVersion": "1.0.0.0",
            "parameters": {k: {"value": v} for k, v in p.items()}}


def write_param_file(cfg: dict, admins: list[str], out_dir: Path) -> Path:
    d = Path(out_dir) / ".infra"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{cfg['capacity']['name']}.parameters.json"
    p.write_text(json.dumps(arm_parameters(cfg, admins), indent=2), encoding="utf-8")
    return p


# --------------------------------------------------------------------------------------------------
# capacity via ARM REST
# --------------------------------------------------------------------------------------------------
def cap_url(az: Az, cfg: dict, suffix: str = "") -> str:
    try:
        sub = az.who()["subscriptionId"]
    except AzError:
        if az.execute:
            raise
        sub = "<subscription-id>"                                   # a dry run must work on a machine that is not signed in
    return (f"{ARM}/subscriptions/{sub}/resourceGroups/{cfg['resourceGroup']}/providers/Microsoft.Fabric/capacities/"
            f"{cfg['capacity']['name']}{suffix}?api-version={CAP_API}")


def capacity_get(cfg: dict, az: Az) -> Optional[dict]:
    try:
        return az.rest_read("get", cap_url(az, cfg))
    except AzError as ex:
        if ex.not_found:
            return None
        raise


def _summ(c: Optional[dict]) -> dict:
    if not c:
        return {"exists": False}
    p = c.get("properties") or {}
    return {"exists": True, "name": c.get("name"), "id": c.get("id"), "sku": (c.get("sku") or {}).get("name"), "state": p.get("state"),
            "provisioningState": p.get("provisioningState"), "location": c.get("location"),
            "admins": (p.get("administration") or {}).get("members")}


def capacity_state(cfg: dict, az: Az) -> dict:
    return _summ(capacity_get(cfg, az))


def wait_capacity(cfg: dict, az: Az, done: Callable[[dict], bool], what: str, timeout: int = 1200, interval: int = 15,
                  sleeper: Callable[[float], None] = time.sleep) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        s = capacity_state(cfg, az)
        if done(s):
            return s
        if time.monotonic() > deadline:
            raise TimeoutError(f"capacity {cfg['capacity']['name']}: timed out waiting for {what} (last state: {s})")
        sleeper(interval)


def lifecycle(cfg: dict, az: Az, action: str, sku: Optional[str] = None, sleeper: Callable[[float], None] = time.sleep) -> Optional[dict]:
    """suspend | resume | scale | delete a capacity: REST call, then poll until the state is reached. Idempotent."""
    name = cfg["capacity"]["name"]
    if action not in ("suspend", "resume", "scale", "delete"):
        raise ValueError(f"unknown action {action}")
    if action == "scale" and sku not in SKUS:
        raise ValueError(f"sku must be one of {sorted(SKUS)}")
    if not az.execute:                                               # record exactly what would run; read nothing about the live state
        if action in ("suspend", "resume"):
            az.rest_mutate("post", cap_url(az, cfg, f"/{action}"))
        elif action == "scale":
            az.rest_mutate("patch", cap_url(az, cfg), {"sku": {"name": sku, "tier": "Fabric"}})
        else:
            az.rest_mutate("delete", cap_url(az, cfg))
        return None
    cur = capacity_state(cfg, az)
    if not cur["exists"]:
        if action == "delete":
            return cur
        raise AzError(f"capacity {name} does not exist in resource group {cfg['resourceGroup']}")
    if action == "suspend":
        if cur["state"] != "Paused":
            az.rest_mutate("post", cap_url(az, cfg, "/suspend"))
            cur = wait_capacity(cfg, az, lambda s: s.get("state") == "Paused", "Paused", sleeper=sleeper)
    elif action == "resume":
        if cur["state"] != "Active":
            az.rest_mutate("post", cap_url(az, cfg, "/resume"))
            cur = wait_capacity(cfg, az, lambda s: s.get("state") == "Active", "Active", sleeper=sleeper)
    elif action == "scale":
        if cur["sku"] != sku:
            az.rest_mutate("patch", cap_url(az, cfg), {"sku": {"name": sku, "tier": "Fabric"}})
            cur = wait_capacity(cfg, az, lambda s: s.get("sku") == sku and s.get("state") in ("Active", "Paused"), f"sku {sku}", sleeper=sleeper)
    else:
        az.rest_mutate("delete", cap_url(az, cfg))
        cur = wait_capacity(cfg, az, lambda s: not s["exists"], "deletion", sleeper=sleeper)
    return cur


# --------------------------------------------------------------------------------------------------
# pricing
# --------------------------------------------------------------------------------------------------
def retail_rate_per_cu_hour(region: str, fetch: Optional[Callable[[str], dict]] = None) -> Optional[float]:
    """Indicative pay-as-you-go price of one capacity unit hour from the public Azure Retail Prices API (no auth)."""
    import requests
    url = f"{PRICES_URL}?$filter=serviceName eq 'Microsoft Fabric' and armRegionName eq '{region}'&currencyCode='USD'"
    items: list[dict] = []
    try:
        while url:
            page = fetch(url) if fetch else requests.get(url, timeout=30).json()
            items += page.get("Items", [])
            url = page.get("NextPageLink")
    except Exception:  # noqa: BLE001 - pricing is advisory; never block the plan
        return None
    rates = [i["retailPrice"] for i in items if i.get("type") == "Consumption" and str(i.get("meterName", "")).endswith("Capacity Usage CU")
             and i.get("unitOfMeasure") == "1 Hour"]
    if not rates:
        return None
    return max(set(rates), key=rates.count)


def cost_estimate(cfg: dict, rate: Optional[float]) -> dict:
    cu = SKUS[cfg["capacity"]["sku"]]
    if rate is None:
        return {"cu": cu, "note": "price lookup unavailable; use the Azure pricing calculator"}
    hourly = round(cu * rate, 3)
    return {"cu": cu, "usdPerCuHour": rate, "usdPerHour": hourly, "usdPer8hDemoDay": round(hourly * 8, 2),
            "usdPerMonthIfNeverPaused": round(hourly * 730, 2),
            "note": "Indicative, from the public Azure Retail Prices API. Paused capacity is not billed for compute; OneLake storage still is. "
                    "Ontology meters (Modeling, Discovery, Logic and Operations, AI) bill against the same capacity."}


# --------------------------------------------------------------------------------------------------
# plan / provision
# --------------------------------------------------------------------------------------------------
OWNER_TAG = ("managedBy", "contoso-bank-ontology-demo")        # on the resource group this kit creates
RUN_TAG = "contosoRun"                                           # unique per build; the same value is in the ledger


class ExistingResourceGroup(RuntimeError):
    """The resource group exists and this kit did not create it (or it is no longer the group the ledger recorded)."""


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def not_ours_message(rg: str) -> str:
    return (f"resource group '{rg}' already exists and this kit did not create it. build.sh only builds into a brand-new resource group, "
            f"so that wipe.sh can delete the whole group and leave none behind. Choose another name (CONTOSO_RESOURCE_GROUP), or delete "
            f"that group yourself first")


def resource_group_exists(az: Az, rg: str) -> bool:
    return str(az.read(["group", "exists", "-n", rg], as_json=False)).lower() == "true"


def owns_resource_group(ledger: Any, rg: str) -> Optional[dict]:
    """The ledger entry when this kit created (and still owns) the resource group, else None."""
    e = ledger.get("resourceGroup", rg) if ledger is not None else None
    return e if e and e["owned"] and e["state"] in ("pending", "present") else None


def resource_group_tags(az: Az, rg: str) -> dict:
    body = az.read(["group", "show", "-n", rg, "-o", "json"]) or {}
    return body.get("tags") or {}


def resource_group_resources(az: Az, rg: str) -> list[dict]:
    """[{name, type}] of everything in the group (read only)."""
    return [{"name": r.get("name"), "type": r.get("type")} for r in (az.read(["resource", "list", "-g", rg, "-o", "json"]) or [])]


def list_fabric_capacities(az: Az) -> list[dict]:
    """[{name, resourceGroup, sku, location}] of every Fabric capacity in the current subscription (read only): the wipe audit."""
    rows = az.read(["resource", "list", "--resource-type", "Microsoft.Fabric/capacities", "-o", "json"]) or []
    return [{"name": r.get("name"), "resourceGroup": r.get("resourceGroup"), "sku": (r.get("sku") or {}).get("name"), "location": r.get("location")} for r in rows]


def verify_run_tag(az: Az, rg: str, entry: Optional[dict]) -> Optional[str]:
    """None when the live group carries the run tag recorded in the ledger (it is the group this kit created); else the reason."""
    want = ((entry or {}).get("details") or {}).get("runId")
    if not want:
        return None                                              # a ledger written before run tags existed
    have = resource_group_tags(az, rg).get(RUN_TAG)
    if have == want:
        return None
    return (f"resource group '{rg}' carries {RUN_TAG}={have!r} but the ledger recorded {want!r}: it is not the group this kit created "
            f"(someone replaced it), so it is left alone")


def delete_resource_group(az: Az, rg: str) -> None:
    """Hard delete. Azure has no soft delete for resource groups or for Fabric capacities, so nothing can be recovered afterwards.
    Blocks until the group is gone."""
    az.mutate(["group", "delete", "-n", rg, "--yes"], as_json=False)


def plan(cfg: dict, az: Az, price_fetch: Optional[Callable[[str], dict]] = None, offline: bool = False, out_dir: Optional[Path] = None,
         ledger: Any = None) -> dict:
    errs = validate_config(cfg)
    out: dict[str, Any] = {"errors": errs, "config": {k: cfg[k] for k in ("resourceGroup", "location", "capacity", "workspace", "lakehouse")}}
    if errs:
        return out
    if offline:
        out["steps"] = ["(offline) resolve signed-in user", "az group create (a brand-new resource group)", "az deployment group create (ARM template)",
                        "az rest GET capacity"]
        out["cost"] = cost_estimate(cfg, None)
        return out
    who = az.who()
    out["signedIn"] = who
    admins = capacity_admins(cfg, who)
    out["capacityAdmins"] = admins
    if not admins:
        out["errors"].append("no capacity admin: you are signed in as a service principal / managed identity; set CONTOSO_CAPACITY_ADMINS "
                             "(comma separated UPNs) or capacity.admins in config/local.json")
    rg = cfg["resourceGroup"]
    out["resourceGroupExists"] = resource_group_exists(az, rg)
    own = owns_resource_group(ledger, rg)
    out["resourceGroupOwned"] = own is not None
    if out["resourceGroupExists"]:
        if own is None:
            out["errors"].append(not_ours_message(rg))
        else:
            why = verify_run_tag(az, rg, own)
            if why:
                out["errors"].append(why)
    cap = capacity_get(cfg, az) if out["resourceGroupExists"] else None
    out["capacity"] = _summ(cap)
    if cap is None:
        try:
            chk = az.rest_read("post", f"{ARM}/subscriptions/{who['subscriptionId']}/providers/Microsoft.Fabric/locations/{cfg['location']}"
                                       f"/checkNameAvailability?api-version={CAP_API}",
                               {"name": cfg["capacity"]["name"], "type": "Microsoft.Fabric/capacities"})
            out["nameAvailable"] = bool((chk or {}).get("nameAvailable"))
            if out["nameAvailable"] is False:
                out["errors"].append(f"capacity name '{cfg['capacity']['name']}' is not available: {(chk or {}).get('message', '')}")
        except AzError as ex:
            out["nameAvailable"] = None
            out["nameCheckNote"] = str(ex)[:200]
    out["cost"] = cost_estimate(cfg, retail_rate_per_cu_hour(cfg["location"], price_fetch))
    params = write_param_file(cfg, admins, out_dir or Path("out")) if admins else Path("<parameters.json>")
    if out["resourceGroupExists"] and admins:
        try:
            wi = az.read(["deployment", "group", "what-if", "-g", rg, "-f", TEMPLATE, "-p", f"@{params}", "--no-pretty-print", "-o", "json"])
            out["whatIf"] = [f"{c['changeType']} {c['resourceId'].split('/providers/')[-1]}" for c in (wi or {}).get("changes", [])
                             if c.get("changeType") != "Ignore"]
        except AzError as ex:
            out["whatIfNote"] = str(ex)[:300]
    else:
        out["whatIfNote"] = "what-if needs the resource group; `provision` validates the template right after creating it and stops before the capacity if ARM or Azure Policy objects"
    out["steps"] = [
        f"az group create -n {rg} -l {cfg['location']} --tags {OWNER_TAG[0]}={OWNER_TAG[1]} {RUN_TAG}=<run id>   "
        + ("(resuming the group this kit created)" if own else "(a brand-new group: wipe.sh deletes it, with everything in it)"),
        f"az deployment group validate -g {rg} -f {TEMPLATE} -p @{params}   (preflight: ARM + Azure Policy)",
        f"az deployment group create -g {rg} -f {TEMPLATE} -p @{params}" + (" (capacity exists: converges, no change)" if cap else ""),
        f"Fabric REST: workspace '{cfg['workspace']['name']}' assigned to the capacity, lakehouse '{cfg['lakehouse']['name']}'",
    ]
    return out


def provision(cfg: dict, az: Az, out_dir: Optional[Path] = None, ledger: Any = None) -> dict:
    """A brand-new resource group + the capacity in it. The group must not exist unless this kit created it (resume); every object
    is recorded in the ledger BEFORE it is created (write-ahead) and is always owned, so wipe can delete the whole group and a crash
    can never leave billable resources that wipe does not know about. The group is tagged with a unique run id; wipe refuses to
    delete a group whose tag differs (someone else's group that merely reuses the name)."""
    errs = validate_config(cfg)
    if errs:
        raise ValueError("; ".join(errs))
    who = az.who()
    admins = capacity_admins(cfg, who)
    if not admins:
        raise ValueError("no capacity admin: set CONTOSO_CAPACITY_ADMINS (comma separated UPNs) or capacity.admins in config/local.json")
    rg, loc = cfg["resourceGroup"], cfg["location"]
    params = write_param_file(cfg, admins, out_dir or Path("out"))
    exists = resource_group_exists(az, rg)
    own = owns_resource_group(ledger, rg)
    if exists:
        if own is None:
            raise ExistingResourceGroup(not_ours_message(rg))
        why = verify_run_tag(az, rg, own)
        if why:
            raise ExistingResourceGroup(why)
    run_id = ((own or {}).get("details") or {}).get("runId") or new_run_id()
    tags = [f"{k}={v}" for k, v in cfg.get("tags", {}).items()] + [f"{OWNER_TAG[0]}={OWNER_TAG[1]}", f"{RUN_TAG}={run_id}"]
    cap_exists = exists and capacity_get(cfg, az) is not None
    if ledger is not None and az.execute:
        ledger.record("resourceGroup", rg, owned=True, state="pending", subscriptionId=who["subscriptionId"], location=loc, runId=run_id)
        ledger.record("fabricCapacity", cfg["capacity"]["name"], owned=True, state="pending", resourceGroup=rg,
                      subscriptionId=who["subscriptionId"], sku=cfg["capacity"]["sku"])
    if exists:
        az.log.append(f"resource group {rg}: created by this kit earlier, resuming")
    else:
        az.mutate(["group", "create", "-n", rg, "-l", loc, "--tags", *tags])
    dep = ["-g", rg, "-f", TEMPLATE, "-p", f"@{params}"]
    if cap_exists:
        az.log.append(f"capacity {cfg['capacity']['name']} exists (created by this kit earlier): no deployment, sku and state untouched")
        if not az.execute:
            return {"dryRun": True, "commands": list(az.log)}
        state = capacity_state(cfg, az)
        if ledger is not None:
            ledger.record("resourceGroup", rg, owned=True, state="present")
            ledger.record("fabricCapacity", cfg["capacity"]["name"], owned=True, state="present", id=state.get("id"))
        why = sku_mismatch(cfg, state.get("sku"))
        if why:
            raise CapacitySkuMismatch(why)
        return state
    if az.execute:
        az.read(["deployment", "group", "validate", *dep, "-o", "none"], as_json=False)        # raises before anything billable exists
    else:
        az.log.append(f"[dry-run] az deployment group validate {' '.join(dep)}")
    az.mutate(["deployment", "group", "create", *dep[:2], "-n", f"fabric-capacity-{cfg['capacity']['name']}", *dep[2:], "-o", "none"], as_json=False)
    if not az.execute:
        return {"dryRun": True, "commands": list(az.log)}
    state = wait_capacity(cfg, az, lambda s: s["exists"] and s.get("state") in ("Active", "Paused"), "provisioning")
    if ledger is not None:
        ledger.record("resourceGroup", rg, owned=True, state="present")
        ledger.record("fabricCapacity", cfg["capacity"]["name"], owned=True, state="present", id=state.get("id"))
    why = sku_mismatch(cfg, state.get("sku"))                                                    # recorded first: wipe must know about it
    if why:
        raise CapacitySkuMismatch(why)
    return state
