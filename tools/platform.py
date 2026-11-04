"""Build the platform from blank, from a pipeline: the data source, the Fabric capacity, a workspace, a lakehouse and the loaded data.

DRAFT. It drives the kit's documented commands and Fabric REST calls and is tested with fakes and the kit's Fabric mock; it has not run against a live
tenant yet. The one-time set-up it needs (an identity for the pipeline, two Fabric tenant settings, two capacity settings) is in docs/BOOTSTRAP.md.

  python -m tools.platform plan      --env dev    the settings and the steps; calls nothing
  python -m tools.platform preflight --env dev    free and read-only: sign-in, tokens, may the identity call Fabric, and the plan for the infrastructure
  python -m tools.platform build     --env dev    data source, upload, build, capacity, workspace, lakehouse, load, ontology gate, outputs, pause
  python -m tools.platform status    --env dev    what exists and what state it is in (read-only)
  python -m tools.platform rescue    --env dev    the last line of defence: settle a capacity deployment that may still be running, then pause the capacity
  python -m tools.platform destroy   --env dev [--everything]    purge the workspace (and with --everything the other workspace, the capacity and the storage account)

What `build` does, in order (the capacity does not exist, so nothing bills by the hour, until step 6):
  1 sign-in and tokens   2 find the subscription   3 storage account (the data source)   4 upload the seed data to it
  5 download it again and build the bronze, silver and gold layers from that copy
  (steps 3 and 4 are skipped, and 5 builds from the seed data in the repository, unless CD_DATA_SOURCE is `storage`: many subscriptions have a policy that
  turns off the storage account's public network access, and then GitHub's runners cannot reach it)
  6 create the Fabric capacity, or keep the one that exists (never resized, never loses an administrator)   7 make sure it is running
  8 create the workspace and the lakehouse   9 check the ontology gate, and ask the admin to open it now if it is closed   10 load the layers
  11 wait for the gate if it is still closed   12 write the outputs   13 pause the capacity, also when a step failed.
The admin's click needs 7 to 11 minutes to apply, about as long as the load takes, so asking at step 9 lets the two overlap.
Every step is bounded by the deadline (CD_DEADLINE_MIN, see tools/cd.py), so the pause always runs; a capacity deployment that is still running when the
pause is owed is cancelled and waited for first, so that it cannot create a running capacity behind the pause.

Settings are the repository variables of tools/cd.py (CD_RESOURCE_GROUP, CD_CAPACITY, CD_WORKSPACE_DEV, CD_WORKSPACE_PROD, CD_LOCATION, CD_TIER,
CD_DEADLINE_MIN), CD_DATA_SOURCE (`repository`, the default, or `storage`) and four lists that `python -m tools.bootstrap` sets: CD_ADMIN_OBJECT_IDS (people,
by object id: workspace Admins), CD_ADMIN_UPNS (the same
people, by user principal name: capacity administrators, because Fabric takes a UPN for a user), CD_PIPELINE_OBJECT_ID (this pipeline's identity) and
CD_CONSUMER_OBJECT_IDS (content repositories, by object id: capacity administrators and workspace Contributors).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from . import cd, export_fabric
from .cd import CdError, StepFailed
from .promptlib import ROOT

EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_GATE, EXIT_STILL_RUNNING = cd.EXIT_OK, cd.EXIT_FAILED, cd.EXIT_CONFIG, 3, cd.EXIT_STILL_RUNNING
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
SOURCE_CONTAINER = "source"
DATA_SOURCES = ("repository", "storage")
DATA_SOURCE_DEFAULT = "repository"                        # a storage account is reachable from GitHub's runners only where the subscription's policies leave public network access on
GATE_PROBE_ITEM = "PromptOpsGateProbe"
PROBE_WORKSPACE_PREFIX = "PromptOpsProbe-"
DEFAULT_GATE_WAIT_MIN = 20
GATE_POLL_S = 60.0
SP_GROUP_DEFAULT = "contoso-promptops-fabric-apps"
TAGS = {"project": "contoso-fabric-iq-promptops", "purpose": "demo", "autoPause": "true", "managedBy": "contoso-fabric-iq-promptops"}
INFRA_TIMEOUT_S = 20 * 60
SETTLE_POLL_S = 15.0
SETTLE_TIMEOUT_S = 240.0
SETTLE_TOTAL_S = 300.0                                    # the cancel, the polling and the calls around them
PROVISION_POLL_S = 15.0
PROVISION_WAIT_S = 300.0                                  # a capacity that is still being provisioned cannot be paused yet: wait for it, this long at most
LATE_APPEARANCE_S = 90.0                                  # after a cancel, a capacity may still appear: look for it this long
PAUSE_PREAMBLE_MAX_S = SETTLE_TOTAL_S + max(PROVISION_WAIT_S, LATE_APPEARANCE_S) + 2 * (cd.STATUS_TIMEOUT_S + cd.GRACE_S)     # everything before the pause itself, worst case
PROVISIONED = {None, "Succeeded", "Failed", "Canceled", "Cancelled", "Deleting", "Deleted"}
PROVISIONING_STATES = {"Provisioning", "Preparing", "Updating", "UpdatingSku", "Creating"}
MIN_CAPACITY_STEP_S = 15 * 60                             # creating the capacity is not started with less than this left: its pause needs room
STORAGE_DEPLOYMENT = "platform-storage"
CAPACITY_DEPLOYMENT = "platform-capacity"
DEPLOYMENT_ENDED = {"Succeeded", "Failed", "Canceled", "Cancelled", "NotFound"}
CANCELLED_STATES = {"Canceled", "Cancelled"}
CANCEL_DOUBT_S = 30 * 60                                  # a deployment cancelled less than this long ago (by anyone) may still be followed by a capacity
DEPLOYMENT_MISSING_RE = re.compile(r"DeploymentNotFound|ResourceGroupNotFound|ResourceNotFound")
TRANSFER_TIMEOUT_S = 15 * 60
PROBE_CLEANUP_TRIES = 3
PLATFORM_SKU = "F4"
UPN_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
TENANT_SETTINGS_URL = "https://app.fabric.microsoft.com/admin-portal/tenantSettings"
CAPACITY_SETTINGS_URL = "https://app.fabric.microsoft.com/admin-portal/capacities/{capacity_id}/delegatedSettings?experience=power-bi"


# ---------------------------------------------------------------------------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------------------------------------------------------------------------
def parse_ids(value: str, what: str) -> tuple[str, ...]:
    ids = tuple(dict.fromkeys(x.strip().lower() for x in (value or "").replace(";", ",").split(",") if x.strip()))
    for x in ids:
        if not GUID_RE.match(x):
            raise CdError(f"{what} must be a comma separated list of object ids (GUIDs); '{x[:12]}...' is not one (docs/BOOTSTRAP.md)")
    return ids


def parse_upns(value: str, what: str) -> tuple[str, ...]:
    upns = tuple(dict.fromkeys(x.strip() for x in (value or "").replace(";", ",").split(",") if x.strip()))
    for x in upns:
        if not UPN_RE.match(x):
            raise CdError(f"{what} must be a comma separated list of user principal names (name@domain); '{x[:12]}...' is not one (docs/BOOTSTRAP.md)")
    return upns


@dataclass(frozen=True)
class PlatformSettings:
    base: cd.Settings
    admins: tuple[str, ...]                        # object ids of the people: workspace Admins
    admin_upns: tuple[str, ...]                    # their user principal names: capacity administrators (Fabric takes a UPN for a user)
    pipeline: str
    consumers: tuple[str, ...] = ()
    other_workspaces: tuple[str, ...] = ()
    group_name: str = SP_GROUP_DEFAULT
    data_source: str = DATA_SOURCE_DEFAULT         # 'repository': the seed data in this repository is loaded straight into the lakehouse; 'storage': through a storage account

    @property
    def capacity_admins(self) -> tuple[str, ...]:
        """Everyone who may use the capacity: the people (by user principal name), this pipeline and the content repositories (by object id: Fabric's
        documented formats for a user and for a service principal). A service principal must administer the capacity to create a workspace on it
        and to see it through the Fabric API."""
        return tuple(dict.fromkeys((*self.admin_upns, self.pipeline, *self.consumers)))

    def workspace_members(self) -> list[dict]:
        members = [{"id": oid, "type": "User", "role": "Admin"} for oid in self.admins]
        members += [{"id": oid, "type": "ServicePrincipal", "role": "Contributor"} for oid in self.consumers]
        return members


def load_platform_settings(environ: Mapping[str, str], env_name: Optional[str], default_tier: str, need_pipeline: bool = True, need_people: bool = True) -> PlatformSettings:
    """`need_people` is False for the commands that never use the lists of administrators (status, destroy, rescue): the last line of defence in particular
    must not fail because a variable it does not use is missing."""
    base = cd.load_settings(environ, env_name, default_tier)
    admins = parse_ids(environ.get("CD_ADMIN_OBJECT_IDS", ""), "CD_ADMIN_OBJECT_IDS") if need_people else ()
    if need_people and not admins:
        raise CdError("CD_ADMIN_OBJECT_IDS is empty: it lists the people who administer the workspaces; `python -m tools.bootstrap apply` sets it (docs/BOOTSTRAP.md)")
    upns = parse_upns(environ.get("CD_ADMIN_UPNS", ""), "CD_ADMIN_UPNS") if need_people else ()
    if need_people and not upns:
        raise CdError("CD_ADMIN_UPNS is empty: it lists the user principal names of the people who administer the capacity (Fabric takes a UPN for a user, not an "
                      "object id); `python -m tools.bootstrap apply` sets it (docs/BOOTSTRAP.md)")
    pipeline = parse_ids(environ.get("CD_PIPELINE_OBJECT_ID", ""), "CD_PIPELINE_OBJECT_ID") if need_people else ()
    if need_people and need_pipeline and len(pipeline) != 1:
        raise CdError("CD_PIPELINE_OBJECT_ID must hold exactly one object id, the pipeline's identity; `python -m tools.bootstrap apply` sets it (docs/BOOTSTRAP.md)")
    consumers = parse_ids(environ.get("CD_CONSUMER_OBJECT_IDS", ""), "CD_CONSUMER_OBJECT_IDS") if need_people else ()
    others = tuple(n for n in ((environ.get(var) or "").strip() for key, var in cd.WORKSPACE_VARS.items() if key != env_name) if n)
    group = (environ.get("CD_SP_GROUP_NAME") or SP_GROUP_DEFAULT).strip() or SP_GROUP_DEFAULT
    data_source = (environ.get("CD_DATA_SOURCE") or "").strip().lower() or DATA_SOURCE_DEFAULT
    if data_source not in DATA_SOURCES:
        raise CdError(f"CD_DATA_SOURCE must be one of {', '.join(DATA_SOURCES)}; '{data_source[:20]}' is not (docs/PLATFORM.md)")
    return PlatformSettings(base, admins, upns, pipeline[0] if pipeline else "", consumers, others, group, data_source)


def http_status(ex: Exception) -> Optional[int]:
    """The HTTP status behind a Fabric error: the kit's FabricError carries it, and its text reads 'METHOD url -> 403: body'."""
    code = getattr(ex, "status", None)
    if isinstance(code, int):
        return code
    m = re.search(r"-> (\d{3})\b", str(ex))
    return int(m.group(1)) if m else None


def storage_account_name(subscription_id: str, resource_group: str) -> str:
    """A storage account name is global, 3 to 24 lowercase letters and digits: 'stpromptops' and 13 hex digits of a hash of the subscription and the group."""
    digest = hashlib.sha256(f"{subscription_id}/{resource_group}".lower().encode("utf-8")).hexdigest()
    return f"stpromptops{digest[:13]}"


def arm_parameters(s: PlatformSettings, storage_name: str, deploy_capacity: bool, admins: Optional[Sequence[str]] = None) -> dict:
    values = {"storageAccountName": storage_name, "sourceContainerName": SOURCE_CONTAINER, "capacityName": s.base.capacity, "location": s.base.location,
              "capacitySku": PLATFORM_SKU, "capacityAdmins": list(admins if admins is not None else s.capacity_admins), "deployCapacity": deploy_capacity,
              "deployStorage": s.data_source == "storage", "tags": TAGS}
    return {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#", "contentVersion": "1.0.0.0",
            "parameters": {k: {"value": v} for k, v in values.items()}}


def capacity_action(s: PlatformSettings, facts: dict) -> tuple[str, str, tuple[str, ...]]:
    """What to do about the capacity, from what Azure says exists: ('create' | 'keep' | 'update' | 'refuse', why, the administrators to deploy).

    An ARM deployment of a capacity that exists is an update: it would set the size to F4 and replace the administrators with the declared list. So an
    existing capacity is never resized (the kit's rule too: a resize changes the bill) and never loses an administrator; only the missing ones are added."""
    if not facts.get("exists"):
        return "create", "the capacity does not exist yet", s.capacity_admins
    sku = facts.get("sku")
    if sku != PLATFORM_SKU:
        if not sku:
            return "refuse", (f"capacity {s.base.capacity} exists but its size could not be read. The platform will not deploy over it: a deployment sets the size to "
                              f"{PLATFORM_SKU}, which would change the bill if it is another size. Look at the capacity in the Azure portal, then run again"), ()
        return "refuse", (f"capacity {s.base.capacity} is {sku}, but the platform is built for {PLATFORM_SKU}. It never resizes a capacity by itself, because a resize "
                          f"changes the bill: resize it yourself (the kit's `capacity scale`) or point CD_CAPACITY at another name"), ()
    if facts.get("admins") is None:
        return "refuse", (f"capacity {s.base.capacity} exists but its list of administrators could not be read. The platform will not change it: an update would "
                          "replace administrators it cannot see. Look at the capacity in the Azure portal, then run again"), ()
    have = [str(x) for x in facts["admins"]]
    lower = {x.lower() for x in have}
    missing = [x for x in s.capacity_admins if x.lower() not in lower]
    if not missing:
        return "keep", "the capacity exists and every configured administrator is on it", tuple(have)
    return "update", "administrators are missing: " + ", ".join(missing), tuple([*have, *missing])


def describe_plan(s: PlatformSettings) -> str:
    b = s.base
    if s.data_source == "storage":
        source_line = f"Data source:  a storage account in it (derived name), container '{SOURCE_CONTAINER}'"
        data_steps = ["  3. create the storage account (the data source; cents a month)",
                      "  4. upload the seed data to it",
                      "  5. download it again and build the bronze, silver and gold layers from that copy (offline)"]
    else:
        source_line = "Data source:  the seed data in this repository (seed/), loaded into the lakehouse as managed Delta tables; no storage account"
        data_steps = ["  3. (no storage account: CD_DATA_SOURCE=storage creates one, where the subscription lets GitHub's runners reach it)",
                      "  4. (nothing to upload)",
                      "  5. build the bronze, silver and gold layers from the seed data in the repository (offline)"]
    return "\n".join([
        f"Environment: {b.env_name}",
        f"Resource group: {b.resource_group} ({b.location}), empty at the start, created by the bootstrap",
        source_line,
        f"Capacity:     {b.capacity} (F4); administrators: {len(s.admin_upns)} people, this pipeline, {len(s.consumers)} content repositories",
        f"Workspace:    {b.workspace}; lakehouse from the kit's configuration",
        f"Tier:         {b.tier}",
        "Steps:",
        "  1. check the sign-in and fetch a token for each service",
        "  2. find the subscription",
        *data_steps,
        f"  6. create the Fabric capacity (bills about USD {b.usd_per_hour:.2f} an hour from here until step 13). A capacity that already exists is never resized "
        "and never loses an administrator: only missing administrators are added",
        "  7. make sure the capacity is running",
        "  8. create the workspace and the lakehouse, and give the people and the content repositories their roles",
        "  9. check the ontology gate; if it is closed, show the admin what to click, now, while the data loads",
        " 10. load the layers when tables are missing, or with --reload-data",
        " 11. wait for the ontology gate if it is still closed (up to --gate-wait-min minutes)",
        " 12. write the outputs (names and ids the content repository needs)",
        " 13. pause the capacity, also when a step failed",
        f"Deadline:    steps 1 to 12 are bounded to {b.deadline_min} minutes so that step 13 always runs",
    ])


# ---------------------------------------------------------------------------------------------------------------------------------------------
# what a check, a gate and a human request look like
# ---------------------------------------------------------------------------------------------------------------------------------------------
@dataclass
class Check:
    name: str
    status: str                       # ok | closed | unknown | error | skipped
    detail: str = ""
    guide: str = ""


@dataclass
class GateState:
    status: str                       # open | closed | unknown
    detail: str = ""
    request: str = ""                 # what the admin has to do, when the gate is closed
    warning: str = ""                 # something the probe left behind and could not remove


def tenant_settings_guide(group: str) -> dict[str, str]:
    return {
        "api": (f"Fabric admin portal ({TENANT_SETTINGS_URL}) > Tenant settings > Developer settings > **Service principals can call Fabric public APIs**: "
                f"Enabled, *Specific security groups*, add the group `{group}`, Apply."),
        "workspaces": (f"Same page > **Service principals can create workspaces, connections, and deployment pipelines**: Enabled, *Specific security groups*, "
                       f"add the group `{group}`, Apply."),
    }


def approval_request(s: PlatformSettings, capacity_fabric_id: Optional[str], wait_min: float, cross_geo: bool = False) -> str:
    url = CAPACITY_SETTINGS_URL.format(capacity_id=capacity_fabric_id or "<capacity id from the Fabric admin portal>")
    settings = ["Users can create Ontology (preview) items          (without it, creating an ontology answers 403)",
                "Users can use Copilot and other features powered by Azure OpenAI          (needed by the ontology agent)"]
    if cross_geo:
        settings.append("Data sent to Azure OpenAI can be processed outside this capacity's geographic region, compliance boundary, or national cloud instance"
                        f"          (needed too: {s.base.location} is outside the US and EU)")
    cost = f"The capacity bills while the run waits (about USD {s.base.usd_per_hour:.2f} an hour)."
    if wait_min <= 0:
        return "\n".join([
            f"ACTION NEEDED LATER from a capacity admin of {s.base.capacity} (about 2 minutes, no tenant admin needed). Not now: this run does not wait "
            "(gate_wait_min is 0), so it pauses the capacity as soon as the data is loaded, and the settings page is greyed out while the capacity is paused.",
            f"  1. Run build again with a wait (gate_wait_min, default {DEFAULT_GATE_WAIT_MIN:g}): it resumes the capacity, skips the data that is already loaded and waits for the gate.",
            f"  2. While that run is going, open {url}",
            f"     (Fabric admin portal > Capacity settings > {s.base.capacity} > Delegated tenant settings)",
            "  3. For EACH of these: tick 'Override tenant admin selection', switch it On, press Apply:",
            *[f"       - {x}" for x in settings],
            "The settings need 7 to 11 minutes to apply. " + cost,
        ])
    return "\n".join([
        f"ACTION NEEDED from a capacity admin of {s.base.capacity} (about 2 minutes, no tenant admin needed):",
        f"  1. Open {url}",
        f"     (Fabric admin portal > Capacity settings > {s.base.capacity} > Delegated tenant settings; the capacity is running now, a paused one is greyed out)",
        "  2. For EACH of these: tick 'Override tenant admin selection', switch it On, press Apply:",
        *[f"       - {x}" for x in settings],
        f"  3. Nothing else: the run goes on loading the data, then checks every minute for up to {wait_min:g} minutes and carries on by itself. The settings need 7 to 11 minutes to apply.",
        cost,
    ])


# ---------------------------------------------------------------------------------------------------------------------------------------------
# the real thing
# ---------------------------------------------------------------------------------------------------------------------------------------------
class Live(cd.Live):
    """`az` for Azure, the kit's Python API for Fabric, the kit's command line for the data."""

    def __init__(self, *a: Any, sleeper: Callable[[float], None] = time.sleep, utcnow: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc), **kw: Any):
        super().__init__(*a, **kw)
        self.sleeper = sleeper
        self.utcnow = utcnow
        self.subscription: Optional[str] = None
        self.capacity_fabric_id: Optional[str] = None
        self.deployment_cancelled = False

    # ---- az ------------------------------------------------------------------------------------------------------------------------------------
    def _az(self, args: list[str], timeout: float, check: bool = True) -> Any:
        az = self.az or shutil.which("az") or shutil.which("az.cmd")
        if not az:
            raise CdError("the az command line is not installed")
        try:
            r = self.runner([az, *args, "--only-show-errors"], text=True, encoding="utf-8", capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as ex:
            raise CdError(f"az {' '.join(args[:3])} did not finish within {int(timeout)} s") from ex
        if check and r.returncode != 0:
            raise CdError(f"az {' '.join(args[:3])} failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-500:]}")
        return r

    def identity(self, s: PlatformSettings) -> dict:
        r = self._az(["account", "show", "-o", "json"], 90)
        acct = json.loads(r.stdout)
        self.subscription = acct["id"]
        return {"subscriptionId": acct["id"], "user": (acct.get("user") or {}).get("name"), "type": (acct.get("user") or {}).get("type")}

    def _deploy(self, s: PlatformSettings, storage: str, deploy_capacity: bool, verb: str, timeout: float, admins: Optional[Sequence[str]] = None) -> Any:
        params = Path(tempfile.mkdtemp(prefix="platform-arm-")) / "parameters.json"
        params.write_text(json.dumps(arm_parameters(s, storage, deploy_capacity, admins)), encoding="utf-8")
        args = ["deployment", "group", verb, "-g", s.base.resource_group, "-f", str(self.root / "infra" / "main.bicep"), "-p", f"@{params}"]
        if verb == "create":
            args += ["-n", CAPACITY_DEPLOYMENT if deploy_capacity else STORAGE_DEPLOYMENT]
        else:
            args += ["--no-pretty-print"]
        return json.loads(self._az([*args, "-o", "json"], timeout).stdout or "{}")

    def infra(self, s: PlatformSettings, storage: str, deploy_capacity: bool, admins: Optional[Sequence[str]] = None, timeout: Optional[float] = None) -> dict:
        res = self._deploy(s, storage, deploy_capacity, "create", INFRA_TIMEOUT_S if timeout is None else timeout, admins)
        out = (res.get("properties") or {}).get("outputs") or {}
        return {k: v.get("value") for k, v in out.items()}

    def cancelled_recently(self, s: PlatformSettings) -> bool:
        """Did the capacity deployment end, cancelled, less than CANCEL_DOUBT_S ago? An unreadable time counts as yes: a cancel is not an undo, so a capacity
        can still appear for a while after somebody (this run, an earlier run, a person in the portal) cancelled the deployment, and only the age says whether
        that window is over."""
        try:
            r = self._az(["deployment", "group", "show", "-g", s.base.resource_group, "-n", CAPACITY_DEPLOYMENT, "--query", "properties.timestamp", "-o", "tsv"],
                         120, check=False)
            raw = re.sub(r"(\.\d{6})\d+", r"\1", (r.stdout or "").strip().replace("Z", "+00:00"))        # .NET ticks have seven digits
            when = dt.datetime.fromisoformat(raw) if r.returncode == 0 and raw else None
        except (CdError, ValueError):
            when = None
        if when is None:
            return True
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        return (self.utcnow() - when).total_seconds() < CANCEL_DOUBT_S

    def settle_capacity_deployment(self, s: PlatformSettings) -> str:
        """A capacity deployment that was abandoned (a deadline, an interrupt, a cancelled run) is still running on Azure's side and can create an Active
        capacity after a pause was tried. Cancel it and wait until it has ended, so that the pause that follows sees what really exists. Returns the last
        state seen; anything that is not an ended state means it could not be settled. `deployment_cancelled` says that the deployment was cancelled (by this
        call, or by somebody else in the last CANCEL_DOUBT_S): a cancelled deployment does not undo what the capacity service had already started, so the
        capacity may still appear."""
        self.deployment_cancelled = False
        show = ["deployment", "group", "show", "-g", s.base.resource_group, "-n", CAPACITY_DEPLOYMENT, "--query", "properties.provisioningState", "-o", "tsv"]

        def state() -> str:
            try:
                r = self._az(show, 120, check=False)
            except CdError:
                return "Unknown"
            if r.returncode == 0:
                return (r.stdout or "").strip() or "Unknown"
            # only Azure's own "there is no such deployment" counts as ended: a sign-in or network failure says nothing about the deployment
            return "NotFound" if DEPLOYMENT_MISSING_RE.search(f"{r.stderr or ''} {r.stdout or ''}") else "Unknown"
        now = state()
        if now in DEPLOYMENT_ENDED:
            if now in CANCELLED_STATES and self.cancelled_recently(s):
                self.deployment_cancelled = True
            return now
        self.deployment_cancelled = True
        try:
            self._az(["deployment", "group", "cancel", "-g", s.base.resource_group, "-n", CAPACITY_DEPLOYMENT], 60, check=False)
        except CdError:
            pass                                          # a cancel that hangs changes nothing about what the polling below can still find out
        waited = 0.0
        while waited < SETTLE_TIMEOUT_S:
            self.sleeper(SETTLE_POLL_S)
            waited += SETTLE_POLL_S
            now = state()
            if now in DEPLOYMENT_ENDED:
                break
        return now

    def what_if(self, s: PlatformSettings, storage: str) -> dict:
        res = self._deploy(s, storage, True, "what-if", 600)
        counts: dict[str, int] = {}
        for change in res.get("changes") or []:
            counts[change.get("changeType", "?")] = counts.get(change.get("changeType", "?"), 0) + 1
        return counts

    def _transfer(self, args: list[str]) -> None:
        """A storage account that was created a moment ago can refuse its first data-plane calls for a short while: try three times."""
        for attempt in range(3):
            try:
                self._az(args, TRANSFER_TIMEOUT_S)
                return
            except CdError as ex:
                if attempt == 2:
                    if "network rules" in str(ex):
                        raise CdError(f"{str(ex)[:400]} A policy in this subscription may have turned off public network access on the storage account (`az storage account show "
                                      "--query publicNetworkAccess` says Disabled), so GitHub's runners cannot reach it. Set the repository variable CD_DATA_SOURCE to "
                                      "repository to load the seed data straight into the lakehouse (docs/PLATFORM.md).") from ex
                    raise
                self.sleeper(30.0 * (attempt + 1))

    def upload_source(self, s: PlatformSettings, storage: str, seed: Path) -> None:
        self._transfer(["storage", "blob", "upload-batch", "--account-name", storage, "--destination", SOURCE_CONTAINER, "--source", str(seed),
                        "--auth-mode", "login", "--overwrite", "true", "-o", "none"])

    def download_source(self, s: PlatformSettings, storage: str, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        self._transfer(["storage", "blob", "download-batch", "--account-name", storage, "--source", SOURCE_CONTAINER, "--destination", str(dest),
                        "--auth-mode", "login", "--overwrite", "true", "-o", "none"])
        if not (dest / "data" / "releases").is_dir():
            raise CdError("the data source does not hold the seed data (data/releases is missing after the download)")
        return dest

    def build_from_source(self, s: PlatformSettings, source: Path, out: Path) -> Path:
        try:
            return export_fabric.run_build(self.root, s.base.tier, out, repo=source)
        except export_fabric.ExportError as ex:
            raise CdError(str(ex)) from ex

    def delete_storage(self, s: PlatformSettings, storage: str) -> None:
        self._az(["storage", "account", "delete", "-n", storage, "-g", s.base.resource_group, "--yes"], 600)

    def resource_count(self, s: PlatformSettings) -> int:
        return len(json.loads(self._az(["resource", "list", "-g", s.base.resource_group, "-o", "json"], 120).stdout or "[]"))

    def storage_exists(self, s: PlatformSettings, storage: str) -> bool:
        return self._az(["storage", "account", "show", "-n", storage, "-g", s.base.resource_group, "-o", "none"], 120, check=False).returncode == 0

    # ---- Fabric ---------------------------------------------------------------------------------------------------------------------------------
    def _ops(self, s: PlatformSettings) -> tuple[Any, Any]:
        cli, *_rest = self._kit_modules()
        return cli, cli.make_ops()

    def workspace_and_lakehouse(self, s: PlatformSettings, out: Path) -> dict:
        cli, ops = self._ops(s)
        _c, _l, _r, _s, _m = self._kit_modules()
        from contoso_onto import oac
        cfg = cli.load_config(None, env={**os.environ, **s.base.kit_env(self.state_dir, self.root)}, state_dir=self.state_dir)
        try:
            ws = ops.ensure_workspace(s.base.workspace, s.base.capacity, f"Contoso PromptOps platform, {s.base.env_name} (synthetic data only).", sku=PLATFORM_SKU)
        except oac.FabricError as ex:
            status = http_status(ex)
            if status in (401, 403):
                guide = tenant_settings_guide(s.group_name)
                raise CdError(f"Fabric refused to create the workspace ({status}). Two things can cause it. (1) The tenant setting is not on for the group, or not effective yet "
                              f"(a change can take up to 15 minutes): {guide['workspaces']} (2) The pipeline's identity is not yet an administrator of the capacity: the platform "
                              f"adds it when it creates the capacity, so check CD_PIPELINE_OBJECT_ID and the capacity's administrators. The capacity is paused next. {str(ex)[:600]}") from ex
            raise
        self.capacity_fabric_id = ws["capacityId"]
        members = s.workspace_members()
        if members:
            ops.add_members(ws["workspaceId"], members)
        lh = ops.ensure_lakehouse(ws["workspaceId"], cfg["lakehouse"]["name"])
        keep = {k: lh[k] for k in ("lakehouseId", "lakehouseName", "sqlEndpoint", "sqlDatabase")}
        cli.write_state(out, workspaceId=ws["workspaceId"], workspaceName=s.base.workspace, capacityName=s.base.capacity, capacityId=ws["capacityId"],
                        resourceGroup=s.base.resource_group, **keep)
        return {"workspaceId": ws["workspaceId"], "created": ws["created"], "capacityId": ws["capacityId"], **keep}

    def _remove_gate_probe(self, ops: Any, ws: str) -> list[str]:
        """Delete the empty ontology of an earlier probe and the graph model Fabric creates next to every ontology (the kit's wipe removes it too).
        Three tries; returns the names that are still there."""
        left: list[str] = []
        for attempt in range(PROBE_CLEANUP_TRIES):
            try:
                left = [i for i in ops.list_items(ws) if (i.get("type") == "Ontology" and i.get("displayName") == GATE_PROBE_ITEM)
                        or (i.get("type") == "GraphModel" and str(i.get("displayName", "")).startswith(f"{GATE_PROBE_ITEM}_graph_"))]
                for item in left:
                    ops.hard_delete_item(ws, item)
                if not left:
                    return []
                left = [i for i in ops.list_items(ws) if i["id"] in {x["id"] for x in left}]
            except Exception:  # noqa: BLE001 - tried again, and reported if it never works
                left = left or [{"displayName": GATE_PROBE_ITEM}]
            if not left:
                return []
            if attempt < PROBE_CLEANUP_TRIES - 1:
                self.sleeper(5.0)
        return [str(i.get("displayName")) for i in left]

    def gate_state(self, s: PlatformSettings, out: Path) -> GateState:
        """Is the ontology gate open? Creating an ontology is the probe: Fabric checks the gate before it looks at the definition, so a 403
        FeatureNotAvailable is a closed gate and a refusal of the empty definition (400) is an open one. The probe removes what it creates, and what an
        earlier probe left behind (a leftover would make the next create answer 409 instead of telling the gate)."""
        cli, ops = self._ops(s)
        _c, _l, runner, _s, _m = self._kit_modules()
        from contoso_onto import oac
        ws = cli.read_state(out)["workspaceId"]
        stuck = self._remove_gate_probe(ops, ws)
        if stuck:
            return GateState("unknown", "an earlier gate probe left items that could not be removed, so the gate was not probed (a leftover makes the create answer 409)",
                             warning=f"left behind in the workspace, delete by hand: {', '.join(stuck)}")
        try:
            ops.fab.create(ws, "ontologies", {"displayName": GATE_PROBE_ITEM})
        except oac.FabricError as ex:
            if runner.ontology_gate(ex):
                return GateState("closed", "creating an ontology answers 403 FeatureNotAvailable")
            if http_status(ex) == 400:
                return GateState("open", "the gate is open: Fabric refused the empty definition, which it checks after the gate")
            return GateState("unknown", runner.service_error_text(str(ex))[:240])
        left = self._remove_gate_probe(ops, ws)
        if left:
            return GateState("open", "an empty ontology was created and could not be fully removed", warning=f"left behind in the workspace, delete by hand: {', '.join(left)}")
        return GateState("open", "an empty ontology was created and deleted")

    def needs_cross_geo(self, s: PlatformSettings) -> bool:
        _c, _l, runner, _s, _m = self._kit_modules()
        return bool(runner.needs_cross_geo(s.base.location))

    def probe_fabric(self, s: PlatformSettings) -> list[Check]:
        """The two doors that can be proven before a capacity exists: may the pipeline's identity call Fabric at all (the first tenant setting), and may it create
        a workspace (the second)? The second is proven by creating an empty workspace without a capacity and deleting it at once: Fabric answers 401 when the
        tenant setting is not on for the identity (the first live run, 4 Nov 2026, found this at step 8, with the F4 already running). A refusal for any other
        reason is reported as 'skipped', never as closed."""
        _cli, ops = self._ops(s)
        _c, _l, _r, _s, _m = self._kit_modules()
        from contoso_onto import oac
        guide = tenant_settings_guide(s.group_name)
        try:
            ops.fab.call("GET", f"{ops.fab.api}/workspaces")
        except oac.FabricError as ex:
            status = "closed" if http_status(ex) in (401, 403) else "error"           # the tokens were issued a moment ago, so a refusal is the tenant setting
            return [Check("Fabric API access for the pipeline's identity", status, str(ex)[:240], guide["api"] if status == "closed" else "")]
        return [Check("Fabric API access for the pipeline's identity", "ok", "listing workspaces works"), self._probe_workspace_creation(ops, oac, guide, s.base.workspace)]

    def _probe_workspace_creation(self, ops: Any, oac: Any, guide: dict[str, str], workspace: str) -> Check:
        name = "Permission to create workspaces"
        listing: list[dict] = []
        try:
            listing = (ops.fab._json(ops.fab.call("GET", f"{ops.fab.api}/workspaces")) or {}).get("value", [])
        except oac.FabricError:
            pass                                                                      # the create below says whether the door is open
        if any(w.get("displayName") == workspace for w in listing):
            return Check(name, "ok", f"not needed: the workspace '{workspace}' already exists and the pipeline can see it (a workspace made by hand, with the pipeline's "
                                     "identity as Admin, is the way round a tenant that does not let service principals create workspaces)")
        try:                                                                          # what an earlier probe left behind, if its delete failed
            for w in listing:
                if str(w.get("displayName", "")).startswith(PROBE_WORKSPACE_PREFIX):
                    ops.delete_workspace(w["id"])
        except oac.FabricError:
            pass
        probe = f"{PROBE_WORKSPACE_PREFIX}{uuid.uuid4().hex[:8]}"
        try:
            resp = ops.fab.call("POST", f"{ops.fab.api}/workspaces", json={"displayName": probe, "description": "Contoso PromptOps check: empty, deleted at once (synthetic)."})
        except oac.FabricError as ex:
            if http_status(ex) in (401, 403):
                return Check(name, "closed", str(ex)[:300], guide["workspaces"])
            return Check(name, "skipped", f"could not be proven ({str(ex)[:200]}); the build checks it at step 8", guide["workspaces"])
        ws_id = (ops.fab._json(resp) or {}).get("id")
        if not ws_id:
            return Check(name, "error", f"Fabric created '{probe}' but returned no id: delete that empty workspace by hand")
        try:
            ops.delete_workspace(ws_id)
        except Exception as ex:  # noqa: BLE001 - a leftover is reported, with its name
            return Check(name, "error", f"the empty workspace '{probe}' was created but could not be deleted ({str(ex)[:160]}): delete it by hand")
        return Check(name, "ok", "an empty workspace was created and deleted")

    def inventory(self, s: PlatformSettings, storage: Optional[str]) -> dict:
        cap = self.capacity(s.base, "status")
        info: dict[str, Any] = {"capacity": {"exists": cap.get("exists"), "state": cap.get("state"), "sku": cap.get("sku")},
                                "storage": self.storage_exists(s, storage) if storage else None, "workspace": None, "lakehouse": None}
        if cap.get("exists"):
            cli, ops = self._ops(s)
            cfg = cli.load_config(None, env={**os.environ, **s.base.kit_env(self.state_dir, self.root)}, state_dir=self.state_dir)
            ws = ops.find_workspace(s.base.workspace)
            info["workspace"] = bool(ws)
            if ws:
                info["lakehouse"] = bool(ops.fab.find(ws["id"], "lakehouses", cfg["lakehouse"]["name"]))
        return info

    def find_workspaces(self, s: PlatformSettings, names: list[str]) -> list[tuple[str, str]]:
        """(id, name) of the named workspaces. Read-only, works while the capacity is paused."""
        _cli, ops = self._ops(s)
        body = ops.fab._json(ops.fab.call("GET", f"{ops.fab.api}/workspaces")) or {}
        return [(w["id"], w["displayName"]) for w in body.get("value", []) if w.get("displayName") in names]

    def purge_workspace(self, s: PlatformSettings, workspace_id: str) -> dict:
        """The kit's own purge, proven live by its wipe: hard-delete the items (dependants first), empty the recycle bin, rename the workspace so that its
        name is free for the next build, delete it. A bare delete would leave the items recoverable and the name reserved for the retention period."""
        _cli, ops = self._ops(s)
        return ops.purge_workspace(workspace_id)

    def delete_capacity(self, s: PlatformSettings) -> None:
        code = self.kit(s.base, "capacity", "delete", "--execute", timeout=cd.CAPACITY_TIMEOUT_S["resume"])
        if code != 0:
            raise CdError(f"the kit could not delete the capacity (exit {code}); its output is above")


# ---------------------------------------------------------------------------------------------------------------------------------------------
# preflight, build, status, destroy
# ---------------------------------------------------------------------------------------------------------------------------------------------
def preflight(s: PlatformSettings, io: Any, say: Callable[[str], None] = print) -> tuple[int, list[Check]]:
    """Free and read-only. Nothing is created that lasts (a probe workspace is deleted at once) and no capacity exists yet, so nothing bills."""
    checks: list[Check] = []

    def attempt(name: str, fn: Callable[[], Any]) -> tuple[bool, Any]:
        try:
            return True, fn()
        except Exception as ex:  # noqa: BLE001 - a failed check is a result, not a crash
            checks.append(Check(name, "error", str(ex)[:300]))
            return False, None

    ok, who = attempt("Sign-in", lambda: io.identity(s))
    if ok:
        checks.append(Check("Sign-in", "ok", f"signed in as {who.get('type') or 'an identity'}"))
        ok, _ = attempt("Tokens for every service", lambda: io.warm_tokens(s.base))
        if ok:
            checks.append(Check("Tokens for every service", "ok", "a token was issued for each service the pipeline talks to"))
            ok, fabric = attempt("Fabric access", lambda: io.probe_fabric(s))
            checks += fabric or []
            ok, counts = attempt("Infrastructure plan (what-if)", lambda: io.what_if(s, storage_account_name(who["subscriptionId"], s.base.resource_group)))
            if ok:
                checks.append(Check("Infrastructure plan (what-if)", "ok", ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "no changes"))
    lines = []
    for c in checks:
        mark = {"ok": "ok     ", "closed": "CLOSED ", "unknown": "unknown", "error": "ERROR  ", "skipped": "skipped"}[c.status]
        lines.append(f"  [{mark}] {c.name}" + (f": {c.detail}" if c.detail else ""))
        if c.guide:
            lines.append(("            make sure it is on (it cannot be checked yet): " if c.status == "skipped" else "            do this: ") + c.guide)
    say("\n".join(lines))
    if any(c.status == "error" for c in checks):
        return EXIT_FAILED, checks
    if any(c.status == "closed" for c in checks):
        return EXIT_GATE, checks
    return EXIT_OK, checks


@dataclass
class BuildReport:
    report: cd.Report = field(default_factory=cd.Report)
    outputs: dict = field(default_factory=dict)
    gate: Optional[GateState] = None


def describe_missing(missing: list[str], shown: int = 8) -> str:
    """The count and the first names, so that a log can show which tables made a run load the data again."""
    return f"{len(missing)} planned tables are missing ({', '.join(missing[:shown])}{', ...' if len(missing) > shown else ''})"


def ask_for_gate(s: PlatformSettings, io: Any, out: Path, say: Callable[[str], None], wait_min: float) -> GateState:
    """Is the ontology gate open? If not, show the admin what to click now. It does not wait: the settings need 7 to 11 minutes to apply, which is
    about how long the data load takes, so the admin clicks while the data loads."""
    state = io.gate_state(s, out)
    if state.status == "closed":
        state.request = approval_request(s, getattr(io, "capacity_fabric_id", None), wait_min, io.needs_cross_geo(s))
        say(state.request)
        out.mkdir(parents=True, exist_ok=True)
        (out / "ADMIN_APPROVAL.md").write_text("# Admin approval needed\n\n" + state.request + "\n", encoding="utf-8")
    return state


def wait_for_gate(s: PlatformSettings, io: Any, state: GateState, out: Path, say: Callable[[str], None], *, wait_min: float, budget: float,
                  clock: Callable[[], float], sleeper: Callable[[float], None]) -> GateState:
    """Only a closed gate is waited for: up to `wait_min` minutes, and never longer than the time that is left before the deadline."""
    if state.status != "closed" or wait_min <= 0:
        return state
    until = clock() + min(wait_min * 60.0, max(0.0, budget - 90.0))
    polls = 0
    while clock() < until:
        sleeper(min(GATE_POLL_S, max(0.0, until - clock())))
        polls += 1
        now = io.gate_state(s, out)
        if now.status != "closed":
            say(f"  gate: open after {polls} checks")
            now.request = ""
            return now
    say(f"  gate: still closed after {polls} checks; the capacity is paused next. Open the link above while the capacity runs, or run build again and click during the wait.")
    return state


def being_provisioned(st: Mapping[str, Any]) -> bool:
    """A capacity that is still being created or updated cannot be paused yet; an unknown provisioning state is treated the same way."""
    return st.get("provisioningState") not in PROVISIONED or st.get("state") in PROVISIONING_STATES


def pause_what_exists(s: PlatformSettings, io: Any, say: Callable[[str], None], settle: bool = False, *,
                      clock: Callable[[], float] = time.monotonic, sleeper: Callable[[float], None] = time.sleep) -> int:
    """The pause that is owed.

    1. When a capacity deployment may still be running on Azure's side, cancel it and wait for it to end. A cancel is not an undo: the capacity service can
       finish what it had already started and create an Active capacity after any pause was tried.
    2. Look at the capacity. One that is still being provisioned cannot be paused yet: wait for that, at most PROVISION_WAIT_S. After a cancel (or when
       the deployment could not be settled) a capacity that does not exist yet is looked for, LATE_APPEARANCE_S, before it is called absent.
    3. Pause it. A deployment that could not be settled with no capacity to pause is an error, not a success: the capacity may still appear and bill.
    A capacity that does not exist when no deployment was in doubt is nothing to pause, not an alarm."""
    unsettled: Optional[str] = None                       # the last state of a deployment that could not be settled
    cancelled = False                                     # a deployment that was still running was cancelled
    if settle:
        try:
            last = cd.run_bounded(lambda: io.settle_capacity_deployment(s), SETTLE_TOTAL_S)
            cancelled = bool(getattr(io, "deployment_cancelled", False))
            say(f"the capacity deployment: {last}")
            if last not in DEPLOYMENT_ENDED:
                unsettled = str(last)
        except Exception as ex:  # noqa: BLE001 - go on and pause whatever exists, with the doubt kept
            cancelled, unsettled = True, "Unknown"
            say(f"::warning::could not settle the capacity deployment ({str(ex)[:200]}); pausing whatever exists, and looking for a capacity that may still appear")
    began = clock()
    until = began + max(PROVISION_WAIT_S, LATE_APPEARANCE_S)
    late_until = began + LATE_APPEARANCE_S
    doubt = cancelled or unsettled is not None
    st: Mapping[str, Any] = {}
    announced = False
    while True:
        try:
            st = cd.run_bounded(lambda: io.capacity(s.base, "status"), cd.STATUS_TIMEOUT_S + cd.GRACE_S)
        except Exception as ex:  # noqa: BLE001 - cannot tell: try to pause, which says loudly if it cannot
            say(f"::warning::could not read the capacity ({str(ex)[:200]}); trying to pause it anyway")
            return cd.pause(s.base, io, say)
        if st.get("exists"):
            if not being_provisioned(st) or clock() >= until:
                break
            if not announced:
                say(f"capacity {s.base.capacity} is still being provisioned ({st.get('provisioningState')}/{st.get('state')}); it can be paused when that is over, waiting")
                announced = True
        elif not doubt or clock() >= late_until:
            break
        sleeper(PROVISION_POLL_S)
    if not st.get("exists"):
        if unsettled is not None:
            say(f"::error::THE CAPACITY MAY STILL APPEAR AND BILL: the capacity deployment could not be stopped (last state: {unsettled}) and capacity "
                f"{s.base.capacity} does not exist yet. Azure portal, resource group {s.base.resource_group}, Deployments, {CAPACITY_DEPLOYMENT}: cancel it, then look for "
                "the capacity and pause or delete it. The scheduled guard pauses an Active capacity within 3 hours.")
            return EXIT_STILL_RUNNING
        if cancelled:
            say(f"::error::THE CAPACITY MAY STILL APPEAR AND BILL: a capacity deployment was cancelled while it ran, and capacity {s.base.capacity} did not appear in "
                f"{int(LATE_APPEARANCE_S)} s. A cancel is not an undo: Azure can still finish what it had started, and nothing here can prove that it will not. Look at "
                f"resource group {s.base.resource_group} in the Azure portal in a few minutes, or run the platform workflow with action check. The scheduled guard pauses "
                "an Active capacity within 3 hours.")
            return EXIT_STILL_RUNNING
        say(f"capacity {s.base.capacity} does not exist, so there is nothing to pause")
        return EXIT_OK
    code = cd.pause(s.base, io, say)
    if code == EXIT_OK and unsettled is not None:
        say(f"::error::THE CAPACITY DEPLOYMENT IS STILL NOT OVER (last state: {unsettled}). The capacity is paused, but a deployment that is still running can change it. "
            f"Cancel it in the Azure portal (resource group {s.base.resource_group}, Deployments, {CAPACITY_DEPLOYMENT}) and look at the capacity again.")
        return EXIT_STILL_RUNNING
    return code


def rescue(s: PlatformSettings, io: Any, say: Callable[[str], None] = print, *, clock: Callable[[], float] = time.monotonic,
           sleeper: Callable[[float], None] = time.sleep) -> int:
    """The last line of defence of the platform workflow: settle a capacity deployment that may still be running, then pause the capacity if it exists.
    Safe to run any number of times, and when nothing was ever created."""
    return pause_what_exists(s, io, say, settle=True, clock=clock, sleeper=sleeper)


def build(s: PlatformSettings, io: Any, out: Path, *, reload_data: bool = False, keep_running: bool = False, gate_wait_min: float = DEFAULT_GATE_WAIT_MIN,
          say: Callable[[str], None] = print, clock: Callable[[], float] = time.monotonic, sleeper: Callable[[float], None] = time.sleep,
          deadline_s: Optional[float] = None, seed: Optional[Path] = None) -> tuple[int, BuildReport]:
    br = BuildReport()
    rep = br.report
    rc = EXIT_FAILED                                      # EXIT_OK only when every step ran; anything else, an interrupt too, must pause
    owes_pause_since: Optional[float] = None
    deployment_in_flight = False                          # a capacity deployment was started and has not returned: it may still finish on Azure's side
    started = clock()
    limit = s.base.deadline_min * 60.0 if deadline_s is None else deadline_s
    ctx: dict[str, Any] = {}

    def left() -> float:
        return limit - (clock() - started)

    def step(name: str, fn: Callable[..., Any], budgeted: bool = False) -> Any:
        say(f"\n== {name}")
        budget = left()
        t0 = clock()
        if budget < cd.MIN_STEP_S:
            rep.steps.append((name, "not started", 0.0))
            raise StepFailed(name, f"not started: {max(0, int(budget))} s are left of the {int(limit // 60)}-minute deadline (CD_DEADLINE_MIN)")
        try:
            res = cd.run_bounded((lambda: fn(budget)) if budgeted else fn, budget + cd.GRACE_S)
        except StepFailed:
            rep.steps.append((name, "failed", clock() - t0))
            raise
        except cd.Abandoned as ex:
            rep.steps.append((name, "abandoned", clock() - t0))
            raise StepFailed(name, f"{ex}: the {int(limit // 60)}-minute deadline (CD_DEADLINE_MIN) ended while it was running") from ex
        except Exception as ex:  # noqa: BLE001 - reported with the step that failed
            rep.steps.append((name, "failed", clock() - t0))
            raise StepFailed(name, str(ex)[:600]) from ex
        rep.steps.append((name, "ok", clock() - t0))
        return res

    def kit_step(name: str, *args: Any) -> None:
        def run(budget: float) -> None:
            code = io.kit(s.base, *args, timeout=budget)
            if code == cd.TIMEOUT_EXIT:
                raise StepFailed(name, f"stopped at the {int(limit // 60)}-minute deadline (CD_DEADLINE_MIN); the capacity is paused next. Run again, the kit resumes")
            if code != 0:
                raise StepFailed(name, f"the kit exited with {code}; its output is above")
        step(name, run, budgeted=True)

    def owe() -> None:
        nonlocal owes_pause_since
        if owes_pause_since is None:
            owes_pause_since = clock()                    # from here on the capacity may bill, so a pause is owed whatever happens

    try:
        step("check the sign-in and fetch the tokens", lambda: io.warm_tokens(s.base))
        who = step("find the subscription", lambda: io.identity(s))
        storage = storage_account_name(who["subscriptionId"], s.base.resource_group)
        ctx["storage"] = storage
        storage_out: dict = {}
        if s.data_source == "storage":
            storage_out = step("create the storage account, the data source", lambda: io.infra(s, storage, False)) or {}
            step("upload the seed data to the data source", lambda: io.upload_source(s, storage, seed or ROOT / "seed"))
            source = Path(tempfile.mkdtemp(prefix="platform-source-"))
            step("download it again and build the layers from that copy", lambda: (io.download_source(s, storage, source), io.build_from_source(s, source, out)))
        else:
            step("build the layers from the seed data in the repository", lambda: io.build_from_source(s, seed or ROOT / "seed", out))

        def create_capacity(budget: float) -> str:
            nonlocal deployment_in_flight
            if budget < MIN_CAPACITY_STEP_S:
                raise CdError(f"only {int(budget // 60)} minutes are left of the {int(limit // 60)}-minute deadline: a capacity created now could not be followed by a "
                              "safe pause, so nothing was created (raise CD_DEADLINE_MIN, at most 45)")
            facts = io.capacity(s.base, "status")
            action, why, admins = capacity_action(s, facts)
            if action == "refuse":
                raise CdError(why)
            if action == "keep":
                say(f"capacity {s.base.capacity} exists ({facts.get('sku') or 'size not reported'}, {facts.get('state')}): {why}; it is not deployed again")
                return "kept"
            say(f"capacity {s.base.capacity}: {action}: {why}")
            owe()
            if action == "update" and facts.get("state") != "Active":
                say(f"the capacity is {facts.get('state') or 'not running'}: it is resumed first, because Azure refuses to update a capacity that is not running")
                io.capacity(s.base, "resume", timeout=cd.CAPACITY_TIMEOUT_S["resume"])
            deployment_in_flight = True
            io.infra(s, storage, True, admins, timeout=min(INFRA_TIMEOUT_S, budget - cd.GRACE_S - 30))
            deployment_in_flight = False
            return "created" if action == "create" else "updated"
        rep.notes.append(f"capacity: {step('create the Fabric capacity, or keep the one that exists', create_capacity, budgeted=True)}")

        def resume() -> Any:
            owe()
            return io.capacity(s.base, "resume", timeout=cd.CAPACITY_TIMEOUT_S["resume"])
        step("make sure the capacity is running", resume)
        ws = step("create the workspace and the lakehouse", lambda: io.workspace_and_lakehouse(s, out))
        br.gate = step("check the ontology gate and ask the admin to open it", lambda: ask_for_gate(s, io, out, say, gate_wait_min))
        missing = step("check which tables the lakehouse already has", lambda: io.missing_tables(out))
        if reload_data or missing:
            say(f"data: {'reload requested' if reload_data else describe_missing(missing)}")
            kit_step("load the data", "load", "--execute", "--out", out)
        else:
            say("data: every planned table is already in the lakehouse; load skipped (use --reload-data to load again)")
            rep.notes.append("data load skipped: every planned table was already present")
        br.gate = step("wait for the ontology gate", lambda b: wait_for_gate(s, io, br.gate, out, say, wait_min=gate_wait_min, budget=b, clock=clock, sleeper=sleeper),
                       budgeted=True)
        br.outputs = {
            "environment": s.base.env_name, "tier": s.base.tier, "location": s.base.location, "resourceGroup": s.base.resource_group,
            "capacity": {"name": s.base.capacity, "sku": PLATFORM_SKU, "fabricId": ws.get("capacityId")},
            "dataSource": ({"kind": "storage", "storageAccount": storage, "container": SOURCE_CONTAINER, "url": storage_out.get("sourceUrl")}
                           if s.data_source == "storage" else {"kind": "repository", "path": "seed"}),
            "workspace": {"name": s.base.workspace, "id": ws.get("workspaceId")},
            "lakehouse": {"name": ws.get("lakehouseName"), "id": ws.get("lakehouseId"), "sqlEndpoint": ws.get("sqlEndpoint"), "sqlDatabase": ws.get("sqlDatabase")},
            "ontologyGate": {"status": br.gate.status, "detail": br.gate.detail},
        }
        step("write the outputs", lambda: write_outputs(out, br.outputs))
        rc = EXIT_FAILED if br.gate.warning else (EXIT_GATE if br.gate.status == "closed" else EXIT_OK)
        if br.gate.warning:
            rep.notes.append(f"the gate probe left something behind, so this build is not a success (and --keep-running is ignored): {br.gate.warning}")
        if br.gate.status == "closed":
            rep.notes.append("the ontology gate is still closed: open the link in ADMIN_APPROVAL.md while the capacity runs (docs/BOOTSTRAP.md), then run build again")
        elif br.gate.status == "unknown":
            rep.notes.append(f"the ontology gate could not be checked ({br.gate.detail}); the content repository will find out when it creates the ontology")
    except StepFailed as ex:
        say(f"\nFAILED at '{ex.step}': {ex}")
        rep.notes.append(f"failed at '{ex.step}'")
    finally:
        if owes_pause_since is None:
            say("\nthe capacity was never created or resumed by this run, so nothing is paused")
        elif keep_running and rc == EXIT_OK and not deployment_in_flight:
            say("\n--keep-running: the capacity stays on and bills until it is paused (the guard does it at its next run)")
            rep.notes.append("the capacity was left running on purpose (--keep-running)")
        else:
            if keep_running:
                say("\n--keep-running is ignored unless everything succeeded: the capacity is paused so that it cannot bill unseen")
                rep.notes.append("--keep-running was ignored; the capacity was paused")
            say("\n== pause the capacity")
            t0 = clock()
            code = pause_what_exists(s, io, say, settle=deployment_in_flight, clock=clock, sleeper=sleeper)
            rep.steps.append(("pause the capacity", "ok" if code == EXIT_OK else "FAILED", clock() - t0))
            rc = max(rc, code)
            rep.active_seconds = clock() - owes_pause_since
    return rc, br


def write_outputs(out: Path, outputs: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "platform-outputs.json").write_text(json.dumps(outputs, indent=2) + "\n", encoding="utf-8", newline="\n")


def render_summary(s: PlatformSettings, br: BuildReport, rc: int) -> str:
    b = s.base
    head = {EXIT_OK: "done", EXIT_GATE: "built; the ontology gate needs an admin click"}.get(rc, "FAILED")
    lines = [f"### Platform build ({b.env_name}): {head}", "", f"Capacity `{b.capacity}`, workspace `{b.workspace}`, tier `{b.tier}`.", "",
             "| Step | Result | Seconds |", "| --- | --- | --- |"]
    lines += [f"| {name} | {status} | {secs:.0f} |" for name, status, secs in br.report.steps]
    if br.report.active_seconds is not None:
        mins = br.report.active_seconds / 60
        lines += ["", f"The capacity existed or ran for about {mins:.0f} minutes, about USD {mins / 60 * b.usd_per_hour:.2f} at the list price (an estimate, not a bill)."]
    if br.outputs:
        o = br.outputs
        src = o["dataSource"]
        source = f"`{src['storageAccount']}/{src['container']}`" if src.get("kind", "storage") == "storage" else f"the seed data in the repository (`{src['path']}/`)"
        lines += ["", "| Output | Value |", "| --- | --- |", f"| Workspace | `{o['workspace']['name']}` |", f"| Lakehouse | `{o['lakehouse']['name']}` |",
                  f"| Data source | {source} |", f"| Ontology gate | {o['ontologyGate']['status']} |"]
    lines += [""] + [f"- {n}" for n in br.report.notes]
    if br.gate is not None and br.gate.request:
        lines += ["", "#### Action needed: open the ontology gate", "", "```", br.gate.request, "```"]
    return "\n".join(lines) + "\n"


def status(s: PlatformSettings, io: Any, say: Callable[[str], None] = print) -> int:
    who = io.identity(s)
    storage = storage_account_name(who["subscriptionId"], s.base.resource_group)
    info = io.inventory(s, storage)
    say(json.dumps({"storageAccount": storage, **info}, indent=2))
    cap = info["capacity"]
    if cap.get("exists") and cap.get("state") not in cd.NOT_BILLING:
        say(f"::notice::the capacity is {cap.get('state')}: it may be billing. Pause it with the capacity workflow (action pause).")
    return EXIT_OK


def destroy(s: PlatformSettings, io: Any, everything: bool = False, say: Callable[[str], None] = print) -> int:
    """Remove this environment's workspace, and with `everything` both workspaces, the capacity and the storage account. The workspaces are purged the way
    the kit's wipe does it (items hard-deleted, recycle bin emptied, workspace renamed and deleted), which needs a running capacity: it is resumed for that,
    and paused again at the end whatever happens. The resource group stays: the bootstrap made it and the pipeline's rights end at its edge."""
    rc = EXIT_OK
    owes_pause = False
    try:
        who = io.identity(s)
        storage = storage_account_name(who["subscriptionId"], s.base.resource_group)
        names = [s.base.workspace, *(s.other_workspaces if everything else ())]
        found = io.find_workspaces(s, names)
        say(f"workspaces found: {', '.join(name for _id, name in found) or 'none'}")
        if found:
            cap = io.capacity(s.base, "status")
            if cap.get("exists"):
                owes_pause = True
                if cap.get("state") != "Active":
                    say("the capacity is not running: resuming it so that the items can be deleted for real (it is paused again at the end)")
                    io.capacity(s.base, "resume", timeout=cd.CAPACITY_TIMEOUT_S["resume"])
            for workspace_id, name in found:
                try:
                    rep = io.purge_workspace(s, workspace_id)
                except Exception as ex:  # noqa: BLE001 - go on with the rest
                    say(f"::error::purging workspace {name} failed: {str(ex)[:300]}")
                    rc = EXIT_FAILED
                    continue
                problems = [*rep.get("errors", []), *[f"still in the workspace: {x}" for x in rep.get("itemsLeft", [])],
                            *[f"still in the recycle bin: {x}" for x in rep.get("recoverableLeft", [])]]
                if not rep.get("workspaceDeleted"):
                    problems.append("the workspace is still listed")
                if problems:
                    say(f"::error::workspace {name} was not fully removed: {'; '.join(problems)[:400]}")
                    rc = EXIT_FAILED
                else:
                    say(f"purged workspace {name}: {len(rep.get('itemsDeleted', []))} items deleted")
        if everything:
            for label, fn in (("the capacity", lambda: io.delete_capacity(s)), ("the storage account", lambda: io.delete_storage(s, storage))):
                try:
                    fn()
                    say(f"deleted {label}")
                except Exception as ex:  # noqa: BLE001
                    say(f"::error::deleting {label} failed: {str(ex)[:300]}")
                    rc = EXIT_FAILED
            left = io.resource_count(s)
            say(f"resources left in {s.base.resource_group}: {left}" + ("" if left == 0 else "  <- check the portal"))
            if left:
                rc = EXIT_FAILED
    except Exception as ex:  # noqa: BLE001 - reported; the pause below still runs
        say(f"::error::destroy stopped: {str(ex)[:300]}")
        rc = EXIT_FAILED
    finally:
        if owes_pause:
            say("\n== pause the capacity (if it still exists)")
            rc = max(rc, pause_what_exists(s, io, say, settle=False))
    return rc


def write_summary(path: Optional[str], text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(text)


def main(argv: Optional[list[str]] = None, environ: Optional[Mapping[str, str]] = None, io: Any = None) -> int:
    environ = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def env_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--env", choices=sorted(cd.WORKSPACE_VARS), required=True)
        p.add_argument("--summary", help="append a markdown summary to this file (GITHUB_STEP_SUMMARY)")
    for name in ("plan", "preflight", "status", "rescue"):
        env_arg(sub.add_parser(name))
    p = sub.add_parser("build")
    env_arg(p)
    p.add_argument("--reload-data", action="store_true")
    p.add_argument("--keep-running", action="store_true")
    p.add_argument("--gate-wait-min", type=float, default=DEFAULT_GATE_WAIT_MIN, help="how long to wait for the admin to open the ontology gate (0: do not wait)")
    p.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "platform-out"))
    p = sub.add_parser("destroy")
    env_arg(p)
    p.add_argument("--everything", action="store_true", help="also delete the other workspace, the capacity and the storage account")
    a = ap.parse_args(argv)
    try:
        people = a.cmd in ("plan", "preflight", "build")
        s = load_platform_settings(environ, a.env, export_fabric.read_tier(), need_pipeline=a.cmd in ("preflight", "build"), need_people=people)
        if a.cmd == "plan":
            print(describe_plan(s))
            return EXIT_OK
        io = io or Live()
        if a.cmd == "rescue":
            return rescue(s, io)
        if a.cmd == "preflight":
            rc, checks = preflight(s, io)
            lines = [f"### Platform pre-flight ({s.base.env_name}): " + {EXIT_OK: "every door is open", EXIT_GATE: "a door is closed", EXIT_FAILED: "failed"}[rc], ""]
            lines += [f"- **{c.name}**: {c.status}" + (f" ({c.detail})" if c.detail else "") + (f"\n  - Do this: {c.guide}" if c.guide else "") for c in checks]
            write_summary(a.summary, "\n".join(lines) + "\n")
            return rc
        if a.cmd == "status":
            return status(s, io)
        if a.cmd == "destroy":
            rc = destroy(s, io, a.everything)
            write_summary(a.summary, f"### Platform destroy ({s.base.env_name}{', everything' if a.everything else ''}): " + ("done" if rc == EXIT_OK else "FAILED") + "\n")
            return rc
        out = Path(a.out)
        if out.exists() and any(out.iterdir()):
            raise CdError(f"{out} is not empty; use a fresh --out folder")
        rc, br = build(s, io, out, reload_data=a.reload_data, keep_running=a.keep_running, gate_wait_min=a.gate_wait_min)
        write_summary(a.summary, render_summary(s, br, rc))
        return rc
    except CdError as ex:
        print(f"::error::{ex}")
        return EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())
