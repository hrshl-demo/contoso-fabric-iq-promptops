"""Operator flows behind build.sh / wipe.sh.

    preflight   read-only: what would build do, and is anything going to stop it
    up          build, as a CI/CD-style pipeline of four stages and a report:
                  BUILD    bronze files + silver and gold tables -> resource group + capacity -> workspace + lakehouse -> load (bronze, silver, gold) -> semantic model
                  APPROVE  an admin allows ontology items on the capacity: the run asks, waits, then polls until it works
                  DEPLOY   ontology -> graph (REST, no portal wizard)
                  TEST     verify + the ontology agent's golden questions
                  report   agent pack, NEXT_STEPS.md, summary
    diagnose    read-only: what exists right now, compared with the ledger
    down        wipe: hard-delete everything the ledger says this kit created (the whole resource group too), verify it is gone, audit

Exit codes (same meaning as the superapp4 operator scripts): 0 done, 2 bad config, 3 blocked (fix and re-run),
4 partial (re-run resumes), 5 delete/verify failed, 6 another build/wipe is running.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from . import REPO_COMMIT, agents as agents_mod, azure_infra, fabric_ops, oac, oracles, spec as spec_mod
from .azure_infra import AzError, Az
from .cli import ROOT, build_tier, data_layers, layer_lines, read_state, write_state
from .gold import load_model
from .layers import LAYERS, LoadPlan, PlanError, load_plan
from .ledger import Ledger, LedgerError, Lock, LockBusy
from .pipeline import build_gold, write_layers
from .snapshot import clone_pinned, git_head, pinned_sources, resolve_repo

EXIT_OK, EXIT_BAD_CONFIG, EXIT_BLOCKED, EXIT_PARTIAL, EXIT_WIPE_FAILED, EXIT_LOCKED = 0, 2, 3, 4, 5, 6
COLLECTION = {"fabricOntology": "ontologies", "fabricSemanticModel": "semanticModels", "fabricLakehouse": "lakehouses"}
Say = Callable[[str], None]
MakeOps = Callable[[], fabric_ops.Ops]

STAGES = ("build", "approve", "deploy", "test")
GATE_WAIT_MIN, GATE_POLL_SEC, GATE_TIMEOUT_MIN = 15.0, 60.0, 120.0      # the delegated settings need about 10 minutes (8 to 14 seen live)
GOLDEN_MODES = ("none", "smoke", "full")
CAPACITY_SETTINGS_URL = "https://app.fabric.microsoft.com/admin-portal/capacities/{capacity_id}/delegatedSettings?experience=power-bi"
GRAPH_PARTS = ("graphType.json", "dataSources.json", "graphDefinition.json", "stylingConfiguration.json", "graphSettings.json")
US_EU_REGIONS = {"eastus", "eastus2", "westus", "westus2", "westus3", "centralus", "northcentralus", "southcentralus", "westcentralus", "northeurope",
                 "westeurope", "francecentral", "germanywestcentral", "swedencentral", "norwayeast", "switzerlandnorth", "italynorth", "polandcentral",
                 "spaincentral"}


class Blocked(RuntimeError):
    """Something outside this kit has to change first (sign-in, tenant setting, name clash, data)."""


class BadConfig(RuntimeError):
    pass


@dataclass
class Pipeline:
    """How far and how patiently one build run goes (the build.sh stage flags)."""
    until: Optional[str] = None                      # stop after this stage: build, approve, deploy or test
    gate_wait_min: float = GATE_WAIT_MIN             # quiet time after asking for the admin action, before the first poll
    gate_poll_sec: float = GATE_POLL_SEC             # then try to create the ontology this often
    gate_timeout_min: float = GATE_TIMEOUT_MIN       # give up (exit 3, everything kept) after this long; 0 = do not wait at all
    graph: bool = True                               # build the graph over REST (False: leave Configure graph to the portal)
    golden: str = "smoke"                            # the ontology agent's golden questions in the TEST stage: none, smoke or full
    golden_parallel: int = 2                         # 4 in parallel made a third of the first attempts time out on an F4
    strict_golden: bool = False                      # a failed golden question fails the build (exit 4); default: reported only
    reload: bool = False                             # load the tables again even when the lakehouse already holds this data
    golden_fn: Optional[Callable[..., Any]] = None   # tests inject a fake; default: the live ontology MCP endpoint
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time            # the time of day (epoch seconds): when the last load finished is kept in the state file


def ledger_path(state_dir: Path) -> Path:
    return Path(state_dir) / "inventory" / "ledger.json"


def lock_path(state_dir: Path) -> Path:
    return Path(state_dir) / "lock" / "build.lock"


def hash_parts(parts: dict[str, str]) -> str:
    """Stable hash of a definition (ignores .platform, line endings and trailing blanks) used to detect portal edits."""
    h = hashlib.sha256()
    for path in sorted(parts):
        if path == ".platform":
            continue
        text = "\n".join(ln.rstrip() for ln in parts[path].replace("\r\n", "\n").split("\n")).strip("\n")
        h.update(path.encode("utf-8") + b"\0" + text.encode("utf-8") + b"\0")
    return h.hexdigest()


def find_repo(explicit: Optional[str], cache: Path, allow_drift: bool = False, home: Optional[Path] = None, say: Say = print) -> Path:
    """The source repo, read only: an explicit path, else the operator's own checkout when it is at the pinned commit, else the
    pinned commit fetched into the cache. The operator's checkout is never modified, whatever branch it is on."""
    if explicit:
        return resolve_repo(explicit, False, cache, allow_drift)
    cand = (home or Path.home()) / "contoso-retail-superapp4"
    checkout = cand if (cand / "data" / "releases").is_dir() else None
    if checkout is not None:
        head = git_head(checkout)
        if allow_drift or (head and head.startswith(REPO_COMMIT[:7])):
            say(f"  source repo: existing checkout {checkout} (read only)")
            return checkout
        say(f"  existing checkout {checkout} is at {(head or '?')[:7]}, not the pinned {REPO_COMMIT[:7]}; "
            f"fetching the pinned commit into the cache instead (your checkout is not touched)")
    return clone_pinned(Path(cache), sources=pinned_sources(checkout))


def resolve_members(cfg: dict, az: Az, say: Say) -> list[dict]:
    """Workspace role assignments: explicit object ids, UPNs resolved through Microsoft Graph (az ad), and the capacity admins."""
    members: list[dict] = []
    seen: set[str] = set()
    wanted: list[tuple[str, str]] = []
    for m in cfg["workspace"].get("members", []):
        if m.get("id"):
            if m["id"] not in seen:
                members.append(m)
                seen.add(m["id"])
        elif m.get("upn"):
            wanted.append((m["upn"], m.get("role", "Contributor")))
    me = az.who()["user"].lower()
    wanted += [(a, "Admin") for a in cfg["capacity"].get("admins", []) if a.lower() != me]
    for upn, role in wanted:
        try:
            oid = az.read(["ad", "user", "show", "--id", upn, "--query", "id", "-o", "tsv"], as_json=False)
        except AzError as ex:
            say(f"  warning: could not resolve {upn} ({str(ex)[:120]}); not added to the workspace")
            continue
        if oid and oid not in seen:
            members.append({"id": oid, "type": "User", "role": role})
            seen.add(oid)
    return members


# --------------------------------------------------------------------------------------------------
# convergence of one definition-bearing item (semantic model / ontology)
# --------------------------------------------------------------------------------------------------
def converge_item(fab: oac.Fabric, ledger: Ledger, ws_id: str, collection: str, kind: str, name: str, parts: dict[str, str], fmt: Optional[str],
                  create_extra: Optional[dict] = None, backup_dir: Optional[Path] = None, force: bool = False, say: Say = print) -> dict:
    """Create the item, or replace its definition ONLY while it is still exactly what this kit deployed last time.

    A live definition that differs from the hash recorded right after our last deploy means somebody edited it in the portal
    (metrics, synonyms, graph annotations). That work is never overwritten silently: the item is left alone unless force is set,
    and even then the live definition is saved to backup_dir first. Items this kit did not create are never touched without force."""
    desired = hash_parts(parts)
    payload = oac.to_payload(parts, fmt)
    item = fab.find(ws_id, collection, name)
    entry = ledger.get(kind, name)
    if item is None:
        ledger.record(kind, name, owned=True, state="pending", workspaceId=ws_id)            # write-ahead
        item_id = fab.create(ws_id, collection, {"displayName": name, **(create_extra or {}), "definition": payload})
        live = fab.get_definition(ws_id, collection, item_id, fmt)
        ledger.record(kind, name, owned=True, state="present", id=item_id, workspaceId=ws_id, liveHash=hash_parts(live), desiredHash=desired)
        say(f"  {name}: created ({item_id})")
        return {"id": item_id, "action": "created"}
    item_id = item["id"]
    owned = bool(entry and entry["owned"] and entry["state"] in ("pending", "present"))
    if not owned and not force:
        say(f"  {name}: exists and was not created by this kit; left untouched")
        return {"id": item_id, "action": "kept", "reason": "not created by this kit"}
    live = fab.get_definition(ws_id, collection, item_id, fmt)
    live_hash = hash_parts(live)
    last = ((entry or {}).get("details") or {}).get("liveHash")
    if entry and entry["details"].get("desiredHash") == desired and last == live_hash:
        say(f"  {name}: unchanged")
        return {"id": item_id, "action": "unchanged"}
    if owned and last is not None and live_hash != last and not force:
        say(f"  {name}: WARNING live definition changed since the last deploy (portal edits?); left untouched. "
            f"Re-run with --force-replace to overwrite (the live version is backed up first)")
        return {"id": item_id, "action": "kept", "reason": "live definition changed since the last deploy"}
    if backup_dir is not None:
        dest = Path(backup_dir) / kind / dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        oac.write_parts(live, dest)
        say(f"  {name}: live definition backed up to {dest}")
    fab.update_definition(ws_id, collection, item_id, payload)
    live2 = fab.get_definition(ws_id, collection, item_id, fmt)
    ledger.record(kind, name, owned=owned, state="present", id=item_id, workspaceId=ws_id, liveHash=hash_parts(live2), desiredHash=desired)
    say(f"  {name}: replaced")
    return {"id": item_id, "action": "replaced"}


def ensure_base(cfg: dict, az: Az, ops: fabric_ops.Ops, ledger: Ledger, out: Path, say: Say) -> tuple[dict, dict]:
    """Workspace (assigned to the capacity), role assignments and lakehouse, recorded in the ledger before they are created."""
    wsname, lhname = cfg["workspace"]["name"], cfg["lakehouse"]["name"]
    pre = ops.find_workspace(wsname)
    ledger.record("fabricWorkspace", wsname, owned=pre is None, state="pending", capacity=cfg["capacity"]["name"])      # write-ahead
    ws = ops.ensure_workspace(wsname, cfg["capacity"]["name"], cfg["workspace"].get("description", ""), sku=cfg["capacity"]["sku"])
    cap_sku, cap_state = ws.pop("capacitySku", None), ws.pop("capacityState", None)
    ledger.record("fabricWorkspace", wsname, owned=ws.pop("created"), state="present", id=ws["workspaceId"], capacity=cfg["capacity"]["name"])
    members = resolve_members(cfg, az, say)
    if members:
        ops.add_members(ws["workspaceId"], members)
    lh = ops.ensure_lakehouse(ws["workspaceId"], lhname)
    ledger.record("fabricLakehouse", lhname, owned=lh.pop("created"), state="present", id=lh["lakehouseId"], workspaceId=ws["workspaceId"])
    write_state(out, **ws, **lh, capacityName=cfg["capacity"]["name"], resourceGroup=cfg["resourceGroup"])
    say(f"  workspace {wsname} ({ws['workspaceId']}) runs on capacity {cfg['capacity']['name']}" + (f" ({cap_sku}, {cap_state}), as Fabric reports it" if cap_sku else "")
        + f"; lakehouse {lhname}")
    return ws, lh


def ontology_gate(ex: Exception, settings_url: Optional[str] = None) -> Optional[str]:
    """The message for Fabric refusing to create ontology items (tenant setting off); None for any other error."""
    s = str(ex)
    if "/ontologies" in s and "-> 403" in s and "FeatureNotAvailable" in s:
        link = f" Direct link for this capacity: {settings_url} ." if settings_url else ""
        return ("Fabric refuses to create ontology items in this tenant (403 FeatureNotAvailable). A Fabric administrator has to enable the tenant "
                "setting 'Users can create ontology (preview) items' (Fabric Admin portal > Tenant settings), or the capacity admin can override it on "
                "the capacity (Capacity settings > the capacity > Delegated tenant settings; the capacity must be Active); it can take a few minutes up to an hour "
                f"to apply.{link} Everything built so far is kept and the capacity is still running. Steps: docs/MANUAL_STEPS.md. Then re-run bash build.sh: it resumes")
    return None


DEFINITION_CALL = re.compile(r"^(?:POST \S+/(?:ontologies|semanticModels)(?:/[0-9A-Za-z-]+/updateDefinition)?(?:\?\S*)?|GET \S+/operations/\S+/result) -> 400\b")
FAILED_DEFINITION_OPERATION = re.compile(r'^operation Failed: .*"errorCode": "[^"]*(?:Invalid|Definition|Validation|BadRequest)', re.S)
TABLE_NOT_THERE = re.compile(r"not ?found|does not exist|doesn't exist|no such table|invalid object name|(?:could not|cannot|can't|unable to) (?:be )?(?:find|resolve)|unknown (?:table|object)", re.I)


def service_error_text(message: str) -> str:
    """What the service said, from a FabricError message: 'errorCode: message | detail | ...' when the body is the usual JSON error object, else the message."""
    start = message.find("{")
    if start >= 0:
        try:
            body = json.loads(message[start:])
        except ValueError:
            body = None
        if isinstance(body, dict) and (body.get("errorCode") or body.get("message")):
            head = ": ".join(str(x) for x in (body.get("errorCode"), body.get("message")) if x)
            return " | ".join([head, *(str(d.get("message") or d.get("errorCode")) for d in body.get("moreDetails") or [] if isinstance(d, dict))])
    return message


def definition_rejection(ex: Exception) -> Optional[str]:
    """The service's own error text when `ex` is Fabric refusing the definition of an ontology or a semantic model: HTTP 400 on the create, the
    updateDefinition or the result of their long-running operation, or such an operation ending Failed with an invalid-definition error code.
    None for everything else: the 403 FeatureNotAvailable gate, throttling and server errors (429, 5xx), 404, 409, a reply that never came, and a
    refusal that says a table or object is not there: right after a load that is the SQL analytics endpoint still catching up (sql_sync_hint), and
    shorter descriptions would not cure it, but would be remembered as if they had."""
    if not isinstance(ex, oac.FabricError):
        return None
    text = str(ex)
    if not (DEFINITION_CALL.match(text) or FAILED_DEFINITION_OPERATION.match(text)):
        return None
    said = service_error_text(text)
    return None if TABLE_NOT_THERE.search(said) else said


class DescriptionFallbackFailed(oac.FabricError):
    """Fabric refused a definition and then refused the compact retry too. The message starts with the original error (`original`, also the
    `__cause__`); the retry's error (`retry`) follows it."""

    def __init__(self, original: Exception, retry: Exception):
        super().__init__(f"{original} | the retry with compact descriptions was refused too: {retry}", status=getattr(original, "status", None),
                         retry_after=getattr(original, "retry_after", None))
        self.original, self.retry = original, retry


class Descriptions:
    """The mode of the entity descriptions in one run, full or compact (spec.DESCRIPTION_MODES). It starts as the operator or the build state says and
    can fall back from full to compact once, for good, when Fabric refuses a definition (see TierDefinitions). The mode is kept in the state file
    (`descriptionsMode`; `descriptionsFallback` says why a fallback happened; `descriptionsAccepted` the longest description lines Fabric took)."""

    def __init__(self, out: Path, say: Say, mode: str = spec_mod.DEFAULT_DESCRIPTIONS, source: str = "default"):
        self.out, self.say, self.source = Path(out), say, source
        self.mode = spec_mod.check_descriptions(mode)
        saved = read_state(self.out).get("descriptionsFallback")
        self.fallback: Optional[dict] = saved if saved and self.mode == "compact" else None
        self.accepted: dict[str, int] = {}                  # item -> the longest description line Fabric accepted in this run (full mode)

    @classmethod
    def for_run(cls, out: Path, say: Say, requested: Optional[str] = None, source: Optional[str] = None) -> "Descriptions":
        """The mode of a run: what was asked for (the CLI has resolved flag and environment), else what the state remembers, else full."""
        mode, where = spec_mod.resolve_descriptions(requested, None, read_state(Path(out)).get("descriptionsMode"))
        return cls(out, say, mode, source if requested and source else where)

    def save(self) -> None:
        write_state(self.out, descriptionsMode=self.mode, descriptionsFallback=self.fallback or "", **({"descriptionsAccepted": dict(self.accepted)} if self.accepted else {}))

    def summary(self) -> str:
        taken = ", ".join(f"{k} description lines of up to {n} characters" for k, n in self.accepted.items())
        if self.mode == "compact":
            why = (f"Fabric refused the full text on {self.fallback['when']} ({self.fallback['item']}: {str(self.fallback['error'])[:160]})" if self.fallback
                   else f"chosen with {self.source}")
            return f"COMPACT ({why})" + (f"; in this run Fabric had accepted {taken} before that" if taken else "")
        return "full" + (f"; Fabric accepted {taken}" if taken else "")

    def next_steps(self) -> str:
        """The part of NEXT_STEPS.md about the descriptions (empty in the full mode, which needs no explanation)."""
        if self.mode != "compact":
            return ""
        fb = self.fallback
        why = (f"Fabric refused the full entity descriptions on {fb['when']} (the {fb['item']}, up to {fb['fullLongest']} characters; the longest it had accepted before was "
               f"{spec_mod.PROVEN_LIVE_ENTITY_LINE}) with: `{fb['error']}`. The kit built every definition again in the compact form (up to {fb['compactLongest']} characters) "
               "and Fabric accepted that, so the ontology and the semantic model now carry compact descriptions (business meaning and standards sentence, no architecture clause; "
               "the star role, grain and lineage are in the data dictionary). " if fb else f"The compact form was chosen with {self.source}. ")
        return ("\n## Descriptions: COMPACT\n" + why + f"The choice is kept in `{self.out / '.state.json'}` (`descriptionsMode`), so a re-run stays compact. To try the full text again run "
                "`bash build.sh --descriptions full` (or set `CONTOSO_DESCRIPTIONS=full`): if Fabric refuses it again the build falls back by itself. "
                "Paste the service error text into docs/LIVE_CHECKLIST.md, section 1a, item 10.\n")


def needs_cross_geo(location: str) -> bool:
    """The ontology agent is off by default on capacities outside the US and EU data boundaries (Microsoft Learn, ontology agent prerequisites)."""
    return (location or "").lower().replace(" ", "") not in US_EU_REGIONS


def approval_request(cfg: dict, ws: dict, pipe: Pipeline, rate: Optional[float] = None, built: Optional[str] = None) -> str:
    """The one human action in the pipeline: what to click, where, and what this run does meanwhile. `built` says what the lakehouse holds."""
    url = CAPACITY_SETTINGS_URL.format(capacity_id=ws.get("capacityId") or "<capacity id from the Fabric admin portal>")
    cap = cfg["capacity"]["name"]
    settings = ["Users can create Ontology (preview) items          (needed now: without it ontology creation answers 403)",
                "Users can use Copilot and other features powered by Azure OpenAI          (needed by the ontology agent and the TEST stage)"]
    if needs_cross_geo(cfg.get("location", "")):
        settings.append("Data sent to Azure OpenAI can be processed outside this capacity's geographic region, compliance boundary, or national cloud "
                        f"instance          (needed too: {cfg.get('location')} is outside the US and EU)")
    lines = [
        f"  ACTION NEEDED from a capacity admin of {cap} (about 2 minutes; no tenant admin required):",
        f"    1. Open {url}",
        f"       (Fabric admin portal > Capacity settings > {cap} > Delegated tenant settings; the capacity is Active now, a paused one is greyed out)",
        "    2. For EACH of these: tick 'Override tenant admin selection', switch it On, press Apply:",
        *[f"         - {s}" for s in settings],
        "    3. Nothing else: this run carries on by itself.",
        f"  This run now waits {pipe.gate_wait_min:g} min for the settings to propagate, then polls every {pipe.gate_poll_sec:g} s (up to {pipe.gate_timeout_min:g} min in all) by trying to "
        "create the ontology, and continues the moment that works.",
        *([f"  Built so far in workspace {ws.get('workspaceName') or cfg['workspace']['name']}: the lakehouse {cfg['lakehouse']['name']} with {built} "
           "(bronze, silver and gold layers; the ontology and the semantic model bind to gold only), and the semantic model. Only the ontology waits for this step."] if built else []),
        "  The override belongs to this capacity, so it is needed once per capacity (a wipe + rebuild needs it again).",
        f"  The capacity bills while waiting{f' (about ${rate}/h)' if rate else ''}. To stop instead: Ctrl+C (or stop the unit), then bash wipe.sh.",
    ]
    return "\n".join(lines)


def _minutes(seconds: float) -> str:
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


def approve_stage(cfg: dict, ops: fabric_ops.Ops, ledger: Ledger, defs: "TierDefinitions", ws: dict, out: Path, force: bool, say: Say, pipe: Pipeline,
                  sleeper: Callable[[float], None], rate: Optional[float] = None, built: Optional[str] = None) -> dict:
    """Create the ontology; the create call is also the probe. Open: done at once. Closed (403 FeatureNotAvailable): ask for the admin action,
    wait, then try again every poll interval until it works or the time is up. Nothing is created while the gate is closed. A definition that
    Fabric refuses with HTTP 400 is not the gate: `defs` retries it once with compact descriptions (TierDefinitions)."""
    url = CAPACITY_SETTINGS_URL.format(capacity_id=ws["capacityId"]) if ws.get("capacityId") else None

    def attempt() -> dict:
        return defs.ontology(force)

    try:
        res = attempt()
        say("  gate: open (ontology items can be created on this capacity); nothing to approve")
        return res
    except oac.FabricError as ex:
        closed = ontology_gate(ex, url)
        if not closed:
            raise
    if pipe.gate_timeout_min <= 0:
        raise Blocked(closed) from None
    request = approval_request(cfg, ws, pipe, rate, built)
    say(request)
    out.mkdir(parents=True, exist_ok=True)
    (out / "ADMIN_APPROVAL.md").write_text("# Admin approval needed\n\n" + request + "\n", encoding="utf-8")
    clock = pipe.clock
    t0 = clock()
    deadline, quiet_until, beat, polls = t0 + pipe.gate_timeout_min * 60, t0 + pipe.gate_wait_min * 60, t0 + 300, 0
    while True:
        now = clock()
        if now >= deadline:
            raise Blocked(f"the ontology gate is still closed after {pipe.gate_timeout_min:g} min ({polls} polls). Everything built so far is kept and the capacity is "
                          f"still running (it bills). Check on the capacity page that the capacity is Active, both settings show 'Override tenant admin selection' ticked "
                          f"and On, and that Apply was pressed; then re-run bash build.sh: it resumes here and polls again. Steps: docs/MANUAL_STEPS.md")
        if now < quiet_until:
            if now >= beat:
                say(f"  approve: {_minutes(now - t0)} of {pipe.gate_wait_min:g} min settling time; polling starts after that")
                beat = now + 300
            sleeper(min(30.0, quiet_until - now))
            continue
        polls += 1
        try:
            res = attempt()
            say(f"  gate: OPEN after {_minutes(clock() - t0)} (poll {polls}); the ontology was created")
            return res
        except oac.FabricError as ex:
            if not ontology_gate(ex, url):
                raise
        if now >= beat:
            say(f"  approve: still closed after {_minutes(now - t0)} (poll {polls}); next poll in {pipe.gate_poll_sec:g} s")
            beat = now + 300
        sleeper(pipe.gate_poll_sec)


def pinned_at(ops: fabric_ops.Ops, workspace_id: str, ontology_name: str, out: Path) -> str:
    """The ONT_PinnedAtUtc stamp. It is part of the ontology definition, so it must be the same on every run, or every re-run would replace
    the ontology. First choice: the stamp kept in the state file; then the one already in the live definition; only then 'now'."""
    stamp = read_state(out).get("pinnedAtUtc")
    if not stamp:
        item = ops.fab.find(workspace_id, "ontologies", ontology_name)
        if item:
            for text in ops.fab.get_definition(workspace_id, "ontologies", item["id"]).values():
                m = re.search(r"annotation ONT_PinnedAtUtc = (\S+)", text)
                if m:
                    stamp = m.group(1)
                    break
        stamp = stamp or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_state(out, pinnedAtUtc=stamp)
    return stamp


def _tier_parts(ops: fabric_ops.Ops, model: dict, tier: str, ws: dict, lh: dict, out: Path, seed: Optional[oac.Seed], ont_annotations: bool,
                descriptions: str = spec_mod.DEFAULT_DESCRIPTIONS) -> tuple[dict, dict, dict]:
    sp = spec_mod.make_spec(model, tier, descriptions)
    ctx = oac.make_ctx(workspace_id=ws["workspaceId"], lakehouse_id=lh["lakehouseId"], workspace_name=ws["workspaceName"],
                       sql_endpoint=lh.get("sqlEndpoint"), sql_db=lh.get("sqlDatabase"), lakehouse_name=sp["lakehouse"]["name"],
                       pinned_at=pinned_at(ops, ws["workspaceId"], sp["ontology"]["name"], out) if ont_annotations and not seed else None)
    sm = oac.build_semantic_model(sp, ctx)
    ont = oac.build_ontology(sp, ctx, seed, ont_annotations)
    for title, parts in ((f"semantic model ({tier})", sm), (f"ontology ({tier})", ont)):
        rep = oac.validate_parts(parts, strict=True)
        if not rep.ok:
            rep.show(title)
            raise BadConfig(f"{title} failed validation")
    return sp, sm, ont


def _converge_semantic_model(ops: fabric_ops.Ops, ledger: Ledger, sp: dict, ws: dict, sm: dict[str, str], out: Path, force: bool, say: Say, refresh: bool) -> dict:
    res = converge_item(ops.fab, ledger, ws["workspaceId"], "semanticModels", "fabricSemanticModel", sp["semanticModel"]["name"], sm, "TMDL",
                        backup_dir=out / "backup", force=force, say=say)
    if refresh and res["action"] in ("created", "replaced"):
        try:
            ops.fab.refresh_model(ws["workspaceId"], res["id"])
            say("  semantic model refresh (framing) requested")
        except oac.FabricError as ex:
            say(f"  warning: refresh request failed ({str(ex)[:160]}); frame it from the portal")
    return res


def _converge_ontology(ops: fabric_ops.Ops, ledger: Ledger, sp: dict, ws: dict, ont: dict[str, str], out: Path, force: bool, say: Say) -> dict:
    return converge_item(ops.fab, ledger, ws["workspaceId"], "ontologies", "fabricOntology", sp["ontology"]["name"], ont, None,
                         create_extra={"description": sp["ontology"].get("description", "")[:256]}, backup_dir=out / "backup", force=force, say=say)


class TierDefinitions:
    """The semantic model and the ontology of one tier as generated parts, in the description mode `desc` holds, and the way they reach Fabric.

    Fabric documents no limit for an entity description, and the longest line it is known to have accepted is 509 characters (v1); the full
    descriptions run up to 736. If it refuses a definition with HTTP 400 while the mode is full, `semantic_model` and `ontology` build both
    definitions again in compact mode (at most 497), try the item once more, keep the mode in the state file and say so loudly. Only a second
    refusal is an error: the first one is raised with the second attached (DescriptionFallbackFailed). The 403 gate, throttling and server errors
    are none of this business, and neither is a definition that is refused in the compact mode already."""

    def __init__(self, ops: fabric_ops.Ops, ledger: Ledger, model: dict, tier: str, ws: dict, lh: dict, out: Path, seed: Optional[oac.Seed],
                 ont_annotations: bool, say: Say, desc: Descriptions):
        self.ops, self.ledger, self.model, self.tier, self.ws, self.lh = ops, ledger, model, tier, ws, lh
        self.out, self.seed, self.ont_annotations, self.say, self.desc = Path(out), seed, ont_annotations, say, desc
        self.results: dict[str, dict] = {}                   # item -> what converge_item answered last
        self._sm_stale = False                               # the semantic model may hold the full text while the mode is compact now
        self.build()

    def build(self) -> None:
        self.sp, self.sm, self.ont = _tier_parts(self.ops, self.model, self.tier, self.ws, self.lh, self.out, self.seed, self.ont_annotations, self.desc.mode)

    def longest_line(self, what: str) -> int:
        """The longest entity description line of an item's definition: the ontology's carries the domain tag, the semantic model's does not."""
        tables = self.sp["tables"]
        if what == "ontology":
            return max((len(oac.ontology_doc(t)) for t in tables if t.get("ontology", True)), default=0)
        return max((len(t["description"]) for t in tables if t.get("semantic", True)), default=0)

    def semantic_model(self, force: bool, refresh: bool = True) -> dict:
        return self._converge("semantic model", lambda: _converge_semantic_model(self.ops, self.ledger, self.sp, self.ws, self.sm, self.out, force, self.say, refresh))

    def ontology(self, force: bool) -> dict:
        res = self._converge("ontology", lambda: _converge_ontology(self.ops, self.ledger, self.sp, self.ws, self.ont, self.out, force, self.say))
        if self._sm_stale:                                   # the fallback fired on the ontology: the semantic model follows it to compact
            self._sm_stale = False
            self.semantic_model(force)
        return res

    def _converge(self, what: str, run: Callable[[], dict]) -> dict:
        try:
            res = run()
        except oac.FabricError as ex:
            refused = definition_rejection(ex) if self.desc.mode == "full" else None
            if refused is None:
                raise
            return self._fall_back(what, run, ex, refused)
        self.results[what] = res
        if self.desc.mode == "full" and res["action"] in ("created", "replaced"):
            n = self.longest_line(what)
            self.desc.accepted[what] = max(self.desc.accepted.get(what, 0), n)
            self.say(f"  descriptions: full; Fabric accepted the {what} with entity description lines of up to {n} characters")
        return res

    def _fall_back(self, what: str, run: Callable[[], dict], first: oac.FabricError, refused: str) -> dict:
        full_longest = self.longest_line(what)
        self.say(f"  DESCRIPTION FALLBACK: Fabric refused the {what} definition with HTTP 400: {refused}")
        self.say(f"  DESCRIPTION FALLBACK: its entity description lines run up to {full_longest} characters in the full mode (the longest Fabric has accepted is "
                 f"{spec_mod.PROVEN_LIVE_ENTITY_LINE}); building every definition again in compact mode and trying the {what} once more")
        self.desc.mode = "compact"
        try:
            self.build()
            res = run()
        except (oac.FabricError, BadConfig) as retry:
            if isinstance(retry, oac.FabricError) and ontology_gate(retry) is not None:      # the retry got as far as the gate: the compact text passed validation
                self._keep_compact(what, refused, full_longest)
                self._sm_stale = what == "ontology"
                self.say(f"  DESCRIPTION FALLBACK APPLIED: the compact retry reached the 403 gate, so Fabric validated the compact {what} definition. descriptionsMode=compact is kept in "
                         f"{self.out / '.state.json'}; the ontology is created with compact descriptions as soon as the gate opens")
                raise
            self.desc.mode = "full"
            self.say(f"  DESCRIPTION FALLBACK FAILED: the retry in compact mode was refused as well: {service_error_text(str(retry))}. The original error stands.")
            raise DescriptionFallbackFailed(first, retry) from first
        self.results[what] = res
        self._keep_compact(what, refused, full_longest)
        self._sm_stale = what == "ontology"
        self.say(f"  DESCRIPTION FALLBACK APPLIED: Fabric accepted the {what} with compact descriptions (lines of up to {self.desc.fallback['compactLongest']} characters). "
                 f"descriptionsMode=compact is kept in {self.out / '.state.json'}, so a re-run stays compact; pass --descriptions full (bash build.sh --descriptions full) to try the "
                 "full text again. Paste the service error above into docs/LIVE_CHECKLIST.md, section 1a, item 10.")
        return res

    def _keep_compact(self, what: str, refused: str, full_longest: int) -> None:
        self.desc.fallback = {"item": what, "error": refused, "when": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                              "fullLongest": full_longest, "compactLongest": self.longest_line(what)}
        self.desc.save()


def deploy_semantic_model(ops: fabric_ops.Ops, ledger: Ledger, model: dict, tier: str, ws: dict, lh: dict, out: Path, seed: Optional[oac.Seed], force: bool, say: Say,
                          refresh: bool = True, ont_annotations: bool = True, desc: Optional[Descriptions] = None) -> dict:
    defs = TierDefinitions(ops, ledger, model, tier, ws, lh, out, seed, ont_annotations, say, desc or Descriptions.for_run(out, say))
    say(f"  tier {tier}: {len(defs.sp['tables'])} tables")
    sm_res = defs.semantic_model(force, refresh)
    return {"spec": defs.sp, "semanticModel": sm_res}


def deploy_tier(ops: fabric_ops.Ops, ledger: Ledger, model: dict, tier: str, ws: dict, lh: dict, out: Path, seed: Optional[oac.Seed], force: bool, say: Say,
                refresh: bool = True, ont_annotations: bool = True, descriptions: Optional[str] = None, descriptions_source: Optional[str] = None) -> dict:
    """Semantic model, then ontology (the `deploy` command). The build pipeline runs the two in separate stages. `descriptions` is the mode the caller
    asks for (None: what the state in `out` remembers, else full); the mode in force at the end, which a fallback may have changed, is in the answer."""
    desc = Descriptions.for_run(out, say, descriptions, descriptions_source)
    defs = TierDefinitions(ops, ledger, model, tier, ws, lh, out, seed, ont_annotations, say, desc)
    say(f" tier {tier}: {len(defs.sp['tables'])} tables")
    sm_res = defs.semantic_model(force, refresh)
    ont_res = defs.ontology(force)
    desc.save()
    return {"spec": defs.sp, "semanticModel": defs.results.get("semantic model", sm_res), "ontology": ont_res, "descriptionsMode": desc.mode, "descriptions": desc.summary()}


def _norm_json(text: Optional[str]) -> Any:
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def materialize_graph(ops: fabric_ops.Ops, sp: dict, ws: dict, lh: dict, out: Path, say: Say, wait_s: float = 300.0, timeout_s: float = 900.0) -> dict:
    """What the portal's Configure graph wizard does, over REST (live-verified 16 Oct 2026): write the graph definition generated from the spec to the
    GraphModel Fabric created next to the ontology, let Fabric refresh it, check the node and edge types are queryable. Idempotent: a graph that already
    holds exactly this definition and whose last refresh completed is left alone."""
    gm = ops.find_graph_model(ws["workspaceId"], sp["ontology"]["name"], wait_s=wait_s)
    if gm is None:
        raise RuntimeError(f"Fabric did not create the graph model next to ontology {sp['ontology']['name']} within {int(wait_s)} s")
    ctx = oac.make_ctx(workspace_id=ws["workspaceId"], lakehouse_id=lh["lakehouseId"], lakehouse_name=sp["lakehouse"]["name"])
    parts = oac.build_graph(sp, ctx)
    rep = oac.validate_graph(parts, sp, strict=True)
    if not rep.ok:
        rep.show("graph definition")
        raise BadConfig("the generated graph definition failed validation")
    want = {"nodeTypes": sum(1 for t in sp["tables"] if t.get("ontology", True)), "edgeTypes": sum(1 for r in sp["relationships"] if r.get("ontology", True))}
    desired = hash_parts(parts)
    st = read_state(out)
    jobs = [j for j in ops.graph_jobs(ws["workspaceId"], gm["id"]) if j.get("status") != "Deduped"]
    done = bool(jobs) and jobs[-1].get("status") == "Completed"
    live = ops.fab.get_definition(ws["workspaceId"], "graphModels", gm["id"])
    same_live = all(_norm_json(live.get(p)) == _norm_json(parts[p]) for p in GRAPH_PARTS)
    same_kit = st.get("graphHash") == desired and st.get("graphModelId") == gm["id"]
    if done and (same_live or same_kit) and ops.graph_counts(ws["workspaceId"], gm["id"]) == want:
        say(f"  graph: unchanged ({want['nodeTypes']} node types, {want['edgeTypes']} edge types)")
        return {"id": gm["id"], "action": "unchanged", **want}
    ops.apply_graph(ws["workspaceId"], gm["id"], parts)
    say("  graph: definition written (what the portal's Configure graph step does); refreshing")
    ops.refresh_graph(ws["workspaceId"], gm["id"], timeout_s=timeout_s)
    got = ops.graph_counts(ws["workspaceId"], gm["id"])
    write_state(out, graphModelId=gm["id"], graphHash=desired)
    if got != want:
        raise RuntimeError(f"the graph refreshed but exposes {got}, expected {want}")
    say(f"  graph: refreshed and queryable ({got['nodeTypes']} node types, {got['edgeTypes']} edge types)")
    return {"id": gm["id"], "action": "applied", **got}


# --------------------------------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------------------------------
class Checks:
    def __init__(self, say: Say):
        self.say, self.fail, self.warn = say, 0, 0

    def __call__(self, status: str, name: str, detail: str = "") -> None:
        self.fail += status == "FAIL"
        self.warn += status == "WARN"
        self.say(f"  [{status:<4}] {name}" + (f": {detail}" if detail else ""))


def preflight(cfg: dict, az: Az, *, make_ops: MakeOps, repo_fn: Callable[[], Path], state_dir: Path, out: Path, say: Say = print,
              price_fetch: Optional[Callable[[str], dict]] = None, descriptions: Optional[str] = None, descriptions_source: Optional[str] = None) -> int:
    """Read-only. Nothing is created in Azure or Fabric; only the local output folder is written."""
    c = Checks(say)
    say("PREFLIGHT (read-only)")
    errs = azure_infra.validate_config(cfg)
    for e in errs:
        c("FAIL", "config", e)
    if errs:
        return EXIT_BAD_CONFIG
    try:
        who = az.who()
        c("PASS", "azure sign-in", f"{who['user']} ({who['userType']}) on '{who['subscriptionName']}'")
    except AzError as ex:
        c("FAIL", "azure sign-in", f"az is not signed in on this host ({str(ex)[:160]}); run az login yourself, this kit never does")
        return EXIT_BLOCKED
    try:
        reg = az.read(["provider", "show", "-n", "Microsoft.Fabric", "--query", "registrationState", "-o", "tsv"], as_json=False)
        c("PASS" if reg == "Registered" else "WARN", "Microsoft.Fabric provider", reg or "unknown" + ("" if reg == "Registered" else " (build registers it)"))
    except AzError as ex:
        c("WARN", "Microsoft.Fabric provider", f"could not read ({str(ex)[:120]})")
    try:
        led: Optional[Ledger] = Ledger(ledger_path(state_dir))
    except LedgerError as ex:
        led = None
        c("FAIL", "ledger", str(ex))
    plan = azure_infra.plan(cfg, az, price_fetch, out_dir=out, ledger=led)
    for e in plan["errors"]:
        c("FAIL", "azure plan", e)
    rg = cfg["resourceGroup"]
    if plan.get("resourceGroupExists"):
        if plan.get("resourceGroupOwned"):
            c("PASS", "resource group", f"{rg} was created by this kit earlier; a build resumes in it, wipe.sh deletes it whole")
    else:
        c("PASS", "resource group", f"{rg} does not exist yet: build.sh creates it brand-new; wipe.sh deletes the whole group (hard delete, no soft delete)")
    cap = plan.get("capacity") or {}
    sku = cfg["capacity"]["sku"]
    if sku == azure_infra.DEMO_SKU:
        c("PASS", "capacity sku", f"{sku} ({azure_infra.SKUS[sku]} capacity units): the size this demo is built, tested and priced for")
    else:
        c("WARN", "capacity sku", f"{sku}, not {azure_infra.DEMO_SKU}: allowed only because {azure_infra.ALLOW_OTHER_SKU_ENV} is set; the cost below is for {sku}")
    if cap.get("exists"):
        c("PASS", "capacity", f"{cap['name']} exists in the group this kit created: {cap.get('sku')} {cap.get('state')} (a build resumes with it)")
        why = azure_infra.sku_mismatch(cfg, cap.get("sku"))
        if why:
            c("FAIL", "capacity sku", why)
    elif not plan["errors"]:
        c("PASS", "capacity", f"{cfg['capacity']['name']} ({cfg['capacity']['sku']}, {cfg['location']}) will be created in {cfg['resourceGroup']}; "
                              f"name available={plan.get('nameAvailable')}")
    cost = plan.get("cost") or {}
    if cost.get("usdPerHour"):
        c("PASS", "cost", f"~${cost['usdPerHour']}/h while the capacity is running (~${cost['usdPer8hDemoDay']} per 8 h day); pause or wipe when idle")
    for w in plan.get("whatIf", []):
        say(f"         what-if: {w}")
    if plan.get("whatIfNote"):
        say(f"         note: {plan['whatIfNote']}")
    # Fabric side
    try:
        ops = make_ops()
        caps = ops.fab._json(ops.fab.call("GET", f"{ops.fab.api}/capacities")) or {}
        c("PASS", "fabric api", f"reachable; {len(caps.get('value', []))} capacities visible to you")
        mine = next((x for x in caps.get("value", []) if (x.get("displayName") or "").lower() == cfg["capacity"]["name"].lower()), None)
        if mine:
            same = not mine.get("sku") or mine["sku"] == cfg["capacity"]["sku"]
            c("PASS" if same else "FAIL", "fabric capacity", f"{mine.get('displayName')} is {mine.get('sku')} {mine.get('state')} as Fabric reports it"
              + ("" if same else f"; this build is configured for {cfg['capacity']['sku']}"))
        elif cap.get("exists"):
            c("WARN", "fabric capacity", f"{cfg['capacity']['name']} exists in Azure but is not visible to you in Fabric (it needs you as a capacity admin)")
        ts = ops.tenant_settings()
        if ts is None:
            c("WARN", "tenant settings", "not readable (needs Fabric admin). Check by hand: Fabric items, Ontology (preview), Data agent, "
                                         "cross-geo Azure OpenAI (only when the capacity is outside the US and EU), service principals if you use them")
        else:
            for s in ts:
                c("PASS" if s["enabled"] else ("FAIL" if "ontology" in (s["name"] or "").lower() else "WARN"), f"tenant setting {s['title'] or s['name']}",
                  "enabled" if s["enabled"] else "DISABLED")
            if not any("ontology" in (s["name"] or "").lower() for s in ts):
                c("WARN", "tenant settings", "no ontology setting found in the list; confirm Ontology (preview) is enabled for you")
    except Exception as ex:  # noqa: BLE001
        c("FAIL", "fabric api", f"{str(ex)[:200]}")
    # data
    try:
        repo = repo_fn()
        model = load_model(ROOT / "model")
        built = build_gold(model, repo)
        if built.ok:
            c("PASS", "source data", f"{repo.name}@{(git_head(repo) or '?')[:7]}: {len(built.tables)} gold tables, {sum(t.num_rows for t in built.tables.values()):,} rows")
        else:
            for e in built.errors[:5]:
                c("FAIL", "source data", e)
        layers = data_layers(model)
        for t in spec_mod.TIERS:
            c("PASS", f"data layers ({t}{', this build' if t == cfg['ontology']['tier'] else ''})", layers[t]["summary"])
        say("         the ontology and the semantic model bind to gold only; bronze (the seed files, verbatim) and silver (CDM profile tables) stay in the lakehouse for lineage and audit")
        mode, where = spec_mod.resolve_descriptions(descriptions, None, read_state(Path(out)).get("descriptionsMode"))
        stats = spec_mod.description_stats(model, cfg["ontology"]["tier"], mode)
        c("PASS", "entity descriptions", f"{mode} mode ({descriptions_source or where}): the longest line is {stats['longestLine'][mode]['characters']} characters, the longest Fabric has "
          f"accepted is {stats['longestAcceptedLive']} (v1). "
          + (f"If Fabric refuses a definition with HTTP 400 the build builds it again in compact mode (up to {stats['longestLine']['compact']['characters']}), retries once and remembers it"
             if mode == "full" else "The compact form is the last resort: a refusal of it is an error"))
        tables = {k: v.to_pandas() for k, v in built.tables.items()} if built.ok else {}
        golden = oracles.run_oracles(tables, model.get("golden", [])) if built.ok else []
        bad = [g["id"] for g in golden if g.get("error")]
        c("PASS" if not bad else "FAIL", "golden questions", f"{len(golden) - len(bad)}/{len(golden)} answer keys computed" + (f"; failed {bad}" if bad else ""))
    except Exception as ex:  # noqa: BLE001
        c("FAIL", "source data", str(ex)[:300])
    if led is not None:
        live = led.live()
        c("PASS", "ledger", f"{len(live)} live entries ({len(led.owned())} owned by this kit)" if live else "empty (first run)")
    say(f"PREFLIGHT {'BLOCKED' if c.fail else 'OK'}: {c.fail} failed, {c.warn} warnings. Nothing was created.")
    return EXIT_BLOCKED if c.fail else EXIT_OK


# --------------------------------------------------------------------------------------------------
# up (build)
# --------------------------------------------------------------------------------------------------
def up(cfg: dict, az: Az, *, make_ops: MakeOps, repo_fn: Callable[[], Path], state_dir: Path, out: Path, say: Say = print, tier: Optional[str] = None,
       seed: Optional[str] = None, force_replace: bool = False, loader: str = "load-api", pause_after: bool = False, ont_annotations: bool = True,
       sleeper: Callable[[float], None] = time.sleep, price_fetch: Optional[Callable[[str], dict]] = None, pipeline: Optional[Pipeline] = None,
       descriptions: Optional[str] = None, descriptions_source: Optional[str] = None) -> int:
    """The build. `descriptions` is the entity-description mode the caller asks for (full or compact); None means what the state in `out` remembers
    (a fallback to compact is remembered), else full."""
    errs = azure_infra.validate_config(cfg)
    pipe = pipeline or Pipeline()
    if pipe.until is not None and pipe.until not in STAGES:
        errs.append(f"--until {pipe.until!r} is not one of {', '.join(STAGES)}")
    if pipe.golden not in GOLDEN_MODES:
        errs.append(f"--golden {pipe.golden!r} is not one of {', '.join(GOLDEN_MODES)}")
    if descriptions is not None and descriptions not in spec_mod.DESCRIPTION_MODES:
        errs.append(f"--descriptions {descriptions!r} is not one of {', '.join(spec_mod.DESCRIPTION_MODES)}")
    if errs:
        for e in errs:
            say(f"config error: {e}")
        return EXIT_BAD_CONFIG
    try:
        with Lock(lock_path(state_dir)):
            return _up(cfg, az, make_ops, repo_fn, Path(state_dir), Path(out), say, tier or cfg["ontology"]["tier"], seed, force_replace or bool(seed),
                       loader, pause_after, ont_annotations, sleeper, price_fetch, pipe, Descriptions.for_run(Path(out), say, descriptions, descriptions_source))
    except LockBusy as ex:
        say(f"BUSY: {ex}")
        return EXIT_LOCKED


def stage_banner(say: Say, n: int, name: str, what: str) -> None:
    say(f"\n== STAGE {n}/{len(STAGES)}  {name.upper():<8} {what} " + "=" * 8)


STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
SYNC_WINDOW_S = 600.0                  # how long after the last load the SQL analytics endpoint can still be catching up with the tables
ONTOLOGY_TABLE_ERROR = re.compile(r"-> 400\b|not ?found|does not exist|no such table|invalid object name|could not find", re.I)


def sql_sync_hint(out: Path, ex: BaseException, now: Optional[float] = None) -> str:
    """The sentence to add to an ontology error that may be the SQL analytics endpoint still catching up with the tables that were just loaded. The
    ontology reads the lakehouse's tables through that endpoint (the semantic model reads OneLake directly), refreshing the endpoint is asynchronous, and with
    about 100 tables the first ontology can be created minutes after the last load. Empty unless `ex` is a Fabric error about an ontology that is a 400 or a
    'not found', and the last load (state key lastLoadAt) finished at most SYNC_WINDOW_S ago."""
    text = str(ex)
    if not isinstance(ex, oac.FabricError) or not re.search(r"ontolog", text, re.I) or not ONTOLOGY_TABLE_ERROR.search(text):
        return ""
    stamp = read_state(Path(out)).get("lastLoadAt")
    try:
        loaded_at = dt.datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=dt.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return ""
    age = (time.time() if now is None else now) - loaded_at
    if not 0 <= age <= SYNC_WINDOW_S:
        return ""
    return (f"  note: the lakehouse tables were loaded {int(age // 60)} min {int(age % 60)} s ago. The ontology reads them through the SQL analytics endpoint, which can take "
            "several minutes to pick up about a hundred new tables (the semantic model reads OneLake directly and is not affected). If the error above is about a table or a "
            "data binding, the endpoint is probably still syncing: wait a few minutes and run bash build.sh again. It resumes at this step and does not load the tables again")


def load_stage(ops: fabric_ops.Ops, plan: LoadPlan, ws: dict, lh: dict, out: Path, staging: str, loader: str, pipe: Pipeline, say: Say) -> None:
    """Put the plan in the lakehouse, layer by layer (bronze, silver, gold), and only the layers that need it. A layer is loaded when --reload says
    so, when this build has not loaded it yet, when its fingerprint differs from the one recorded after its last load (its own manifest changed, or
    a layer before it did: gold is built from silver and silver from bronze), or when a table of it is missing from the lakehouse. Each finished layer
    is recorded in the state file at once, so a run that fails half way resumes with the finished layers left alone; a layer whose load starts is
    unrecorded first, so a crash in the middle of it is never mistaken for a finished load."""
    fps = plan.fingerprints()
    recorded: dict[str, str] = dict(read_state(out).get("loadedLayers") or {})
    have = ops.registered_tables(ws["workspaceId"], lh["lakehouseId"])
    todo: dict[str, str] = {}
    for layer in plan.layers():
        missing = [n for n in plan.names(layer) if n not in have]
        if pipe.reload:
            todo[layer] = "--reload"
        elif layer not in recorded:
            todo[layer] = "not loaded by this build yet"
        elif recorded[layer] != fps[layer]:
            todo[layer] = "its data, or a layer before it, changed since its last load"
        elif missing:
            todo[layer] = f"{len(missing)} of its tables are missing from the lakehouse: {', '.join(missing[:3])}"
    if not todo:
        say(f"  load: the lakehouse already holds these {len(plan.all_names())} tables and {len(plan.files)} bronze files (same data as the last load); skipped (--reload loads again)")
        write_state(out, loadedFingerprint=plan.fingerprint())
        return

    def what(layer: str) -> str:
        n = len(plan.names(layer))
        return f"{len(plan.files)} seed files and {n} table{'s' if n != 1 else ''}" if layer == "bronze" else f"{n} tables"

    for layer in plan.layers():
        say(f"  load {layer}: {what(layer)} ({todo[layer]})" if layer in todo else f"  load {layer}: unchanged since its last load and its tables are in the lakehouse; skipped")

    def started(layer: str) -> None:
        recorded.pop(layer, None)
        write_state(out, loadedLayers=dict(recorded), loadedFingerprint="")

    def finished(layer: str, res: dict) -> None:
        recorded[layer] = fps[layer]
        write_state(out, loadedLayers=dict(recorded))
        up = res.get("uploaded")
        files = (f", {len(up['uploaded'])} seed files uploaded ({up['verified']} read back and checked" + (f", {len(up['unverified'])} could not be read back" if up["unverified"] else "") + ")") if up else ""
        say(f"  {layer}: {len(res['loaded'])} table{'s' if len(res['loaded']) != 1 else ''} registered{files} in {_minutes(res['seconds'])}")

    try:
        ops.load_layers(plan, ws["workspaceId"], lh["lakehouseId"], staging=staging, loader=loader, sql_endpoint_id=lh.get("sqlDatabase"),
                        skip=lambda layer: layer not in todo, started=started, finished=finished, clock=pipe.clock)
    except fabric_ops.LayerLoadError as ex:
        later = [layer for layer in plan.layers() if LAYERS.index(layer) > LAYERS.index(ex.layer) and layer in todo]
        say(f"  ERROR the {ex.layer} layer stopped" + (f" at table {ex.table}" if ex.table else "") + f": {ex.detail[:300]}")
        say("  the layers load in order (bronze, silver, gold) so that a failure shows at the first layer that has it; "
            + (f"{' and '.join(later)} {'was' if len(later) == 1 else 'were'} not started. " if later else "nothing after it was pending. ")
            + "Finished layers are kept: fix the cause and run build.sh again, it resumes here")
        if ex.transient:
            say("  the cause is the moment (throttling, a service that is busy or unreachable), not the data: no table was written another way. Waiting a little before the run starts again helps")
        raise
    write_state(out, loadedFingerprint=plan.fingerprint(), lastLoadAt=dt.datetime.fromtimestamp(pipe.wall(), dt.timezone.utc).strftime(STAMP_FORMAT))
    say(f"  load done: {len(plan.all_names())} tables and {len(plan.files)} seed files in the lakehouse ({plan.summary()})")


def _up(cfg, az, make_ops, repo_fn, state_dir, out, say, tier, seed_dir, force, loader, pause_after, ont_annotations, sleeper, price_fetch, pipe, desc) -> int:
    ledger = None
    try:
        ledger = Ledger(ledger_path(state_dir))
        stage_banner(say, 1, "build", "data layers, capacity, workspace, lakehouse, tables, semantic model")
        say("  data: bronze seed files, silver and gold tables from the pinned source snapshot")
        say(f"  descriptions: {desc.mode} mode ({desc.source})" + (f"; remembered from an earlier run: Fabric refused the full text on {desc.fallback['when']}" if desc.fallback else ""))
        try:
            who = az.who()
        except AzError as ex:
            raise Blocked(f"az is not signed in on this host ({str(ex)[:160]}); sign in yourself with az login, this kit never does") from None
        say(f"  signed in as {who['user']} on '{who['subscriptionName']}' (existing az context, never changed)")
        repo = repo_fn()
        model = load_model(ROOT / "model")
        built = build_gold(model, repo)                                       # silver is built once inside; gold is built from it
        for w in built.warnings[:3]:
            say(f"  warning: {w}")
        if not built.ok:
            for e in built.errors[:10]:
                say(f"  ERROR {e}")
            raise Blocked("the source data did not build into silver and gold tables (see errors above)")
        for line in layer_lines(write_layers(model, built, out, repo, ROOT / "model", tier)):         # only what this tier needs
            say(f"  {line}")
        plan = load_plan(out)
        gold_tables = {k: v.to_pandas() for k, v in built.tables.items()}
        for mode in ("full", "compact") if desc.mode == "full" else ("compact",):                  # the fallback has to be valid before anything is created
            for t in dict.fromkeys(["smoke", tier]):
                rep = spec_mod.check_spec(spec_mod.make_spec(model, t, mode))
                if not rep.ok:
                    raise BadConfig(f"the {t} ontology specification ({mode} descriptions) is invalid: {rep.errors[:3]}")
        golden = oracles.run_oracles(gold_tables, model.get("golden", []))
        bad_golden = [g["id"] for g in golden if g.get("error")]
        if bad_golden:
            raise BadConfig(f"golden answer keys failed to compute: {bad_golden}")
        (out / "eval").mkdir(parents=True, exist_ok=True)
        (out / "eval" / "golden_expected.json").write_text(json.dumps(golden, indent=1, default=str), encoding="utf-8")
        say(f"  specification checks passed (smoke + {tier}); {len(golden)} golden answer keys computed from the data")
        seed_obj = oac.load_seed(seed_dir) if seed_dir else None

        say(f"  azure: a brand-new resource group + {cfg['capacity']['sku']} capacity")
        if cfg["capacity"]["sku"] != azure_infra.DEMO_SKU:
            say(f"  note: building on {cfg['capacity']['sku']}, not the {azure_infra.DEMO_SKU} this demo is built for ({azure_infra.ALLOW_OTHER_SKU_ENV} is set)")
        rate = (azure_infra.cost_estimate(cfg, azure_infra.retail_rate_per_cu_hour(cfg["location"], price_fetch)) or {}).get("usdPerHour")
        say(f"  target: resource group {cfg['resourceGroup']}, capacity {cfg['capacity']['name']} ({cfg['capacity']['sku']}, {cfg['location']})"
            + (f", about ${rate}/h while it runs" if rate else "") + "; wipe.sh deletes the whole group")
        if az.read(["provider", "show", "-n", "Microsoft.Fabric", "--query", "registrationState", "-o", "tsv"], as_json=False) != "Registered":
            az.mutate(["provider", "register", "-n", "Microsoft.Fabric", "--wait"], as_json=False)
        admins = azure_infra.capacity_admins(cfg, who)
        if not admins:
            raise Blocked("no capacity admin: you are signed in with a service principal or managed identity; set CONTOSO_CAPACITY_ADMINS "
                          "(comma separated UPNs)")
        try:
            cap = azure_infra.provision(cfg, az, out, ledger)
        except (azure_infra.ExistingResourceGroup, azure_infra.CapacitySkuMismatch) as ex:
            raise Blocked(str(ex)) from None
        if cap.get("state") == "Paused":
            say("  capacity is paused; resuming")
            cap = azure_infra.lifecycle(cfg, az, "resume", sleeper=sleeper) or cap
        say(f"  capacity {cap.get('name')}: {cap.get('sku')} {cap.get('state')}")
        why = azure_infra.sku_mismatch(cfg, cap.get("sku"))
        if why:
            raise Blocked(why)

        say("  fabric: workspace + lakehouse")
        ops = make_ops()
        try:
            ws, lh = ensure_base(cfg, az, ops, ledger, out, say)
        except fabric_ops.FabricCapacitySkuMismatch as ex:
            raise Blocked(str(ex)) from None
        load_stage(ops, plan, ws, lh, out, cfg["onelake"]["stagingFolder"], loader, pipe, say)

        sm_stage = deploy_semantic_model(ops, ledger, model, tier, ws, lh, out, seed_obj, force, say, ont_annotations=ont_annotations, desc=desc)
        write_state(out, semanticModelId=sm_stage["semanticModel"]["id"], tier=tier)
        desc.save()
        if pipe.until == "build":
            return _stopped(say, cfg, cap, rate, pause_after, az, sleeper, "build")

        stage_banner(say, 2, "approve", "an admin allows ontology items on the capacity (once per capacity); this run waits, then polls")
        defs = TierDefinitions(ops, ledger, model, tier, ws, lh, out, seed_obj, ont_annotations, say, desc)
        approved = approve_stage(cfg, ops, ledger, defs, ws, out, force, say, pipe, sleeper, rate, built=plan.summary())
        final = {"spec": defs.sp, "ontology": approved, "semanticModel": sm_stage["semanticModel"]}
        write_state(out, semanticModelId=final["semanticModel"]["id"], ontologyId=approved["id"], tier=tier)
        desc.save()
        if pipe.until == "approve":
            return _stopped(say, cfg, cap, rate, pause_after, az, sleeper, "approve")

        stage_banner(say, 3, "deploy", f"graph on the ontology ({tier}); the ontology itself was created when the gate opened")
        graph: Optional[dict] = None
        graph_error: Optional[str] = None
        if not pipe.graph:
            say("  graph: skipped (--no-graph); finish it in the portal: ontology > Configure graph")
        elif final["ontology"]["action"] == "kept":
            say("  graph: skipped (the ontology was left untouched, see above)")
        else:
            try:
                graph = materialize_graph(ops, final["spec"], ws, lh, out, say)
            except Exception as ex:  # noqa: BLE001 - reported, then the TEST stage and the report still run
                graph_error = f"{type(ex).__name__}: {str(ex)[:300]}"
                say(f"  ERROR graph: {graph_error}\n  the ontology is fine; re-run build.sh to retry the graph, or use the portal (ontology > Configure graph)")
        if pipe.until == "deploy":
            return _stopped(say, cfg, cap, rate, pause_after, az, sleeper, "deploy")

        stage_banner(say, 4, "test", "ontology check, graph, agent golden questions")
        v = ops.verify_ontology(ws["workspaceId"], final["ontology"]["id"], final["spec"])
        say(f"  ontology: valid={v['valid']} entities {v['entitiesLive']}/{v['entitiesExpected']} relationships {v['relationshipsLive']} rules {v['rulesLive']}")
        if v["graphHint"]:
            say(f"  note: {v['graphHint']}")
        golden_report = qa_stage(ops, ws, final, out, say, pipe, graph_ok=graph_error is None, tier=tier)

        say("\n== REPORT ==========================================================")
        if build_tier(model, tier, gold_tables, out, quiet=True, say_fn=say, descriptions=desc.mode) != 0:        # tier folders + agent pack, now with real ids
            raise BadConfig(f"building the {tier} agent pack failed")
        say(f"  agent pack: {out / tier / 'agents'}")
        write_next_steps(cfg, out, ws, final, v, cap, tier, graph, golden_report, plan, desc)
        exit_code = EXIT_OK
        if final["ontology"]["action"] in ("created", "replaced", "unchanged") and (not v["valid"] or v["missingEntities"]):
            say(f"  ERROR the live ontology does not match the specification: errors={v['errors'][:3]} missing={v['missingEntities'][:5]}")
            exit_code = EXIT_PARTIAL
        if graph_error:
            exit_code = EXIT_PARTIAL
        if pipe.strict_golden and golden_report is not None and not golden_report.ok:
            say("  ERROR the golden questions did not all pass and --strict-golden is set")
            exit_code = EXIT_PARTIAL
        if pause_after:
            say("  pausing the capacity as requested (--pause)")
            azure_infra.lifecycle(cfg, az, "suspend", sleeper=sleeper)
        say(f"\nBUILD {'DONE' if exit_code == EXIT_OK else 'FINISHED WITH ERRORS'}")
        say(f"  workspace : {cfg['workspace']['name']}  https://app.fabric.microsoft.com/groups/{ws['workspaceId']}")
        say(f"  ontology  : {final['spec']['ontology']['name']} ({final['ontology']['action']}), semantic model {final['semanticModel']['action']}"
            + (f", graph {graph['action']} ({graph['nodeTypes']} node types, {graph['edgeTypes']} edge types)" if graph else ""))
        say(f"  descriptions: {desc.summary()}")
        if desc.accepted or desc.fallback:
            say("              record this in docs/LIVE_CHECKLIST.md, section 1a, item 10" + ("; the service error text is in NEXT_STEPS.md and the state file" if desc.fallback else ""))
        if golden_report is not None:
            say(f"  agent     : {golden_report.summary()}" + (f" ({golden_report.unavailable})" if golden_report.unavailable else ""))
        say(f"  capacity  : {cfg['capacity']['name']} ({cap.get('sku') or cfg['capacity']['sku']}) {'PAUSED' if pause_after else 'RUNNING'}"
            + ("" if pause_after or not rate else f" at about ${rate}/h until paused or wiped"))
        say(f"  next      : {out / 'NEXT_STEPS.md'}   |   pause: bash build.sh --pause   |   delete everything this kit created: bash wipe.sh")
        return exit_code
    except Blocked as ex:
        say(f"\nBLOCKED: {ex}")
        return EXIT_BLOCKED
    except BadConfig as ex:
        say(f"\nBAD CONFIG: {ex}")
        return EXIT_BAD_CONFIG
    except LedgerError as ex:
        say(f"\nBLOCKED: {ex}")
        return EXIT_BLOCKED
    except KeyboardInterrupt:
        say("\nINTERRUPTED: nothing is lost, every object created so far is in the ledger. Re-run build.sh to resume, or run wipe.sh.")
        return EXIT_PARTIAL
    except Exception as ex:  # noqa: BLE001
        say(f"\nPARTIAL: {type(ex).__name__}: {str(ex)[:600]}")
        hint = sql_sync_hint(out, ex)
        if hint:
            say(hint)
        say("  nothing is lost: every object created so far is in the ledger. Fix the cause and re-run build.sh (it resumes), or run wipe.sh.")
        (state_dir / "runs").mkdir(parents=True, exist_ok=True)
        (state_dir / "runs" / "last-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        return EXIT_PARTIAL


def _stopped(say: Say, cfg: dict, cap: dict, rate: Optional[float], pause_after: bool, az: Az, sleeper: Callable[[float], None], stage: str) -> int:
    """--until: the run ends after a stage on purpose; a later bash build.sh continues from there."""
    if pause_after:
        say("  pausing the capacity as requested (--pause)")
        azure_infra.lifecycle(cfg, az, "suspend", sleeper=sleeper)
    say(f"\nSTOPPED after the {stage} stage as requested (--until {stage}). Run bash build.sh again to continue with the next stage.")
    say(f"  capacity  : {cfg['capacity']['name']} ({cap.get('sku') or cfg['capacity']['sku']}) {'PAUSED' if pause_after else 'RUNNING'}"
        + ("" if pause_after or not rate else f" at about ${rate}/h until paused or wiped"))
    return EXIT_OK


def qa_stage(ops: fabric_ops.Ops, ws: dict, final: dict, out: Path, say: Say, pipe: Pipeline, graph_ok: bool, tier: str = "core") -> Any:
    """The TEST stage: the MCP endpoint answers, then the ontology agent answers the golden questions and the answers are scored. Reported, not fatal
    (unless --strict-golden): the agent needs the Copilot delegated setting, which a build cannot prove beforehand. Questions marked extended need
    the full tier; the 12-entity smoke tier cannot answer the set at all."""
    mcp = fabric_ops.mcp_tools_list(agents_mod.FABRIC_MCP.format(ws=ws["workspaceId"], ont=final["ontology"]["id"]), ops.token_fn(oac.FABRIC_SCOPE))
    say(f"  mcp endpoint: {len(mcp.get('tools', []))} tools" if mcp.get("ok") else f"  mcp endpoint: not answering yet ({str(mcp.get('error') or mcp.get('status'))[:100]})")
    if pipe.golden == "none":
        say("  golden questions: skipped (--golden none)")
        return None
    if tier == "smoke":
        say("  golden questions: skipped (the 12-entity smoke tier is too small to answer them; build the core tier)")
        return None
    if not graph_ok:
        say("  golden questions: skipped (the graph step failed above; the agent answers from the graph)")
        return None
    path = out / "eval" / "golden_expected.json"
    golden = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if tier != "full":
        golden = [g for g in golden if g.get("tier", "core") == "core"]
    try:
        if pipe.golden_fn is not None:
            report = pipe.golden_fn(ws["workspaceId"], final["ontology"]["id"], golden, pipe.golden, pipe.golden_parallel, say)
        else:
            from . import golden_live
            ids = golden_live.select_ids(golden, pipe.golden)
            say(f"  golden questions: asking {len(ids)} ({pipe.golden}) through the ontology agent; one answer takes 30 to 90 s")
            report = golden_live.run_golden(golden_live.build_client(ws["workspaceId"], final["ontology"]["id"], ops.token_fn), golden, ids,
                                            parallel=pipe.golden_parallel, say=say)
    except Exception as ex:  # noqa: BLE001 - the test stage never takes the build down
        say(f"  golden questions: could not run ({type(ex).__name__}: {str(ex)[:200]})")
        return None
    say(f"  {report.summary()}")
    if report.unavailable:
        say(f"  note: {report.unavailable}")
    return report


def write_next_steps(cfg: dict, out: Path, ws: dict, final: dict, verify: dict, cap: dict, tier: str, graph: Optional[dict] = None, golden: Any = None,
                     plan: Optional[LoadPlan] = None, desc: Optional[Descriptions] = None) -> None:
    url = f"https://app.fabric.microsoft.com/groups/{ws['workspaceId']}"
    mcp = agents_mod.FABRIC_MCP.format(ws=ws["workspaceId"], ont=final["ontology"]["id"])
    if graph:
        graph_step = (f"The graph was built and refreshed by this kit over REST ({graph['nodeTypes']} node types, {graph['edgeTypes']} edge types); nothing to do in Manage graph. "
                      "If the portal still shows the entities as ineligible, open Manage graph once and press Continue.")
        if not verify["graphAnnotationsPresent"]:
            graph_step += (" This build used --no-ont-annotations, so the portal's Manage graph lists the entities as ineligible even though the graph exists; "
                           "rebuild without that flag if you want the portal view to agree.")
    elif verify["graphAnnotationsPresent"]:
        graph_step = ("The ONT_* partition annotations are written, which made every entity graph-eligible in the live check (16 Oct 2026). The graph was not built by this "
                      "run (--no-graph, or it failed: see the build output): re-run bash build.sh, or open the ontology -> **Manage graph**, select the entities and "
                      "relationships, Continue, and let the graph materialize.")
    else:
        graph_step = ("No ONT_* annotations (--no-ont-annotations): the entities will show as ineligible. Rebuild without that flag, or seed once (Generate ontology from the "
                      "Direct Lake semantic model), `python -m contoso_onto export`, then `bash build.sh --seed <folder>`.")
    agent_line = f"* Agent test (TEST stage): {golden.summary()}\n" if golden is not None else ""
    desc_line = f"* Entity descriptions: {desc.summary()}\n" if desc is not None else ""
    desc_section = desc.next_steps() if desc is not None else ""
    data_line = (f"* Lakehouse {cfg['lakehouse']['name']} ({tier} tier), three layers: bronze = {len(plan.files)} seed JSON files, verbatim, under Files/bronze/<release>/ plus the table "
                 f"bronze_file_manifest; silver = {len(plan.names('silver'))} tables (silver_*: the CDM profile with official-name alias columns, plus two catalogs); "
                 f"gold = {len(plan.names('gold'))} tables (dim_*, fact_*, bridge_*, ref_*: the star schema). The semantic model and the ontology bind to gold only; "
                 "bronze and silver are there for lineage and audit.\n") if plan is not None else ""
    text = f"""# Next steps (generated {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})

* Workspace: {cfg['workspace']['name']} - {url}
* Ontology item: {final['spec']['ontology']['name']} ({final['ontology']['id']}), semantic model {final['spec']['semanticModel']['name']} ({final['semanticModel']['id']})
* Capacity: {cfg['capacity']['name']} ({cap.get('sku')}). It bills while running. `bash build.sh --pause` stops compute billing, `bash wipe.sh` deletes what this kit created.
* Ontology MCP endpoint: {mcp}
{data_line}* Agent pack (data agent instructions, data dictionary, voice profile, MCP configs, Foundry IQ knowledge base): {out / tier / 'agents'}
* Golden answer keys: {out / 'eval' / 'golden_expected.json'}  (score collected answers with `python -m contoso_onto verify --answers answers.json`)
{agent_line}{desc_line}
## Graph
{graph_step}
{desc_section}
## Still manual (portal only; see docs/LIVE_CHECKLIST.md)
1. Generate metrics from the semantic model (they only arrive that way) and let the ontology agent enrich synonyms and metadata.
2. Create the data agent on the ontology (portal), then run the golden questions.
"""
    (out / "NEXT_STEPS.md").write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------------------------------
# diagnose
# --------------------------------------------------------------------------------------------------
def diagnose(cfg: dict, az: Az, *, make_ops: MakeOps, state_dir: Path, out: Path, say: Say = print) -> int:
    """Read-only inventory: what the ledger says versus what actually exists."""
    c = Checks(say)
    say("DIAGNOSE (read-only)")
    try:
        ledger = Ledger(ledger_path(state_dir))
    except LedgerError as ex:
        c("FAIL", "ledger", str(ex))
        return EXIT_BLOCKED
    if not ledger.entries:
        say("  ledger is empty: this kit has not created anything on this host (nothing to diagnose; run build.sh)")
    for e in ledger.entries:
        say(f"  ledger: {e['kind']:<20} {e['name']:<34} {e['state']:<8} {'owned' if e['owned'] else 'adopted'}")
    try:
        who = az.who()
        c("PASS", "azure sign-in", f"{who['user']} on '{who['subscriptionName']}'")
        rg = cfg["resourceGroup"]
        rg_e = ledger.get("resourceGroup", rg)
        if rg_e and rg_e["owned"] and rg_e["state"] in ("pending", "present"):
            if not azure_infra.resource_group_exists(az, rg):
                c("WARN", "resource group", f"{rg} is in the ledger but does not exist (deleted already?)")
            else:
                why = azure_infra.verify_run_tag(az, rg, rg_e)
                c("PASS" if not why else "FAIL", "resource group",
                  f"{rg}: created by this kit ({azure_infra.RUN_TAG} matches the ledger); wipe.sh deletes it whole" if not why else why)
        elif azure_infra.resource_group_exists(az, rg):
            c("WARN", "resource group", f"{rg} exists but this kit did not create it; build.sh will refuse to use it")
        st = azure_infra.capacity_state(cfg, az)
        c("PASS" if st.get("exists") else "WARN", "capacity", f"{st.get('name', cfg['capacity']['name'])} {st.get('sku', '')} {st.get('state', 'not found')}".strip())
        if st.get("exists"):
            why = azure_infra.sku_mismatch(cfg, st.get("sku"))
            c("PASS" if not why else "FAIL", "capacity sku", f"{st.get('sku')}, as configured ({azure_infra.DEMO_SKU} is what this demo is built for)" if not why else why)
    except AzError as ex:
        c("FAIL", "azure", str(ex)[:200])
    state = read_state(out)
    try:
        ops = make_ops()
        wsid = state.get("workspaceId") or next((e.get("id") for e in ledger.live() if e["kind"] == "fabricWorkspace"), None)
        ws = ops.find_workspace(cfg["workspace"]["name"])
        if not ws:
            c("WARN", "workspace", f"'{cfg['workspace']['name']}' not found")
        else:
            c("PASS", "workspace", f"{ws['displayName']} ({ws['id']})" + ("" if not wsid or wsid == ws["id"] else f" - differs from the recorded id {wsid}"))
            wsid = ws["id"]
            try:
                fcap = ops.find_capacity(cfg["capacity"]["name"])
                on = ws.get("capacityId") == fcap["id"]
                c("PASS" if on else "FAIL", "workspace capacity",
                  f"{ws['displayName']} runs on {fcap['displayName']} ({fcap.get('sku')}, {fcap.get('state')})" if on
                  else f"{ws['displayName']} is on capacity {ws.get('capacityId') or 'none'}, not {fcap['displayName']}: its items do not run on the {cfg['capacity']['sku']}")
            except oac.FabricError as ex:
                c("WARN", "workspace capacity", str(ex)[:200])
            lh = ops.fab.find(wsid, "lakehouses", cfg["lakehouse"]["name"])
            if lh:
                try:
                    plan = load_plan(out)
                except PlanError:
                    plan = None
                if plan is None:
                    tables = ops.list_tables(wsid, lh["id"])
                    c("PASS" if tables else "WARN", "lakehouse", f"{lh['displayName']}: {len(tables)} tables (no layer manifests in {out}: build.sh writes them)")
                else:
                    rep = ops.layer_report(plan, wsid, lh["id"])
                    gone = sum(len(r["missing"]) for r in rep["layers"].values())
                    c("PASS" if rep["tables"] and not gone else "WARN", "lakehouse",
                      f"{lh['displayName']}: {rep['tables']} tables; planned: {plan.summary()}" + (f"; {len(rep['other'])} more are from another tier or build" if rep["other"] else ""))
                    for layer, r in rep["layers"].items():
                        files = r.get("files") or {}
                        c("PASS" if not r["missing"] and files.get("present") in (None, files.get("expected")) else "WARN", f"layer {layer}", fabric_ops.describe_layer(r))
            else:
                c("WARN", "lakehouse", f"'{cfg['lakehouse']['name']}' not found")
            sp = spec_mod.make_spec(load_model(ROOT / "model"), cfg["ontology"]["tier"])
            sm = ops.fab.find(wsid, "semanticModels", sp["semanticModel"]["name"])
            c("PASS" if sm else "WARN", "semantic model", sp["semanticModel"]["name"] + ("" if sm else " not found"))
            ont = ops.fab.find(wsid, "ontologies", sp["ontology"]["name"])
            if not ont:
                c("WARN", "ontology", f"{sp['ontology']['name']} not found")
            else:
                v = ops.verify_ontology(wsid, ont["id"], sp)
                c("PASS" if v["valid"] and not v["missingEntities"] else "FAIL", "ontology",
                  f"valid={v['valid']} entities {v['entitiesLive']}/{v['entitiesExpected']} relationships {v['relationshipsLive']} rules {v['rulesLive']} "
                  f"graph annotations={v['graphAnnotationsPresent']}")
                entry = ledger.get("fabricOntology", sp["ontology"]["name"])
                last = ((entry or {}).get("details") or {}).get("liveHash")
                if last:
                    live = hash_parts(ops.fab.get_definition(wsid, "ontologies", ont["id"]))
                    c("PASS" if live == last else "WARN", "ontology drift", "unchanged since the last deploy" if live == last
                      else "live definition differs from the last deploy (portal edits); build.sh will not overwrite it without --force-replace")
                mcp = fabric_ops.mcp_tools_list(agents_mod.FABRIC_MCP.format(ws=wsid, ont=ont["id"]), ops.token_fn(oac.FABRIC_SCOPE))
                c("PASS" if mcp.get("ok") else "WARN", "ontology MCP endpoint", f"{len(mcp.get('tools', []))} tools" if mcp.get("ok") else str(mcp.get("error") or mcp.get("status"))[:120])
    except Exception as ex:  # noqa: BLE001
        c("FAIL", "fabric", str(ex)[:240])
    say(f"DIAGNOSE {'FOUND PROBLEMS' if c.fail else 'OK'}: {c.fail} failed, {c.warn} warnings.")
    return EXIT_PARTIAL if c.fail else EXIT_OK


# --------------------------------------------------------------------------------------------------
# down (wipe)
# --------------------------------------------------------------------------------------------------
def audit_capacities(cfg: dict, az: Az, say: Say) -> None:
    """After a wipe: the Fabric capacities that are left in this subscription (read only: only what the ledger owns is ever deleted)."""
    try:
        caps = azure_infra.list_fabric_capacities(az)
    except Exception as ex:  # noqa: BLE001 - informational only
        say(f"  audit: could not list the Fabric capacities of this subscription ({str(ex)[:120]})")
        return
    if not caps:
        say("  audit: no Fabric capacity is left in this subscription")
        return
    say(f"  audit: {len(caps)} Fabric capacity(ies) remain in this subscription (only what this ledger created is ever deleted):")
    for c in caps:
        same = str(c.get("name") or "").lower() == cfg["capacity"]["name"].lower()
        say(f"    {str(c.get('name')):<28} {str(c.get('sku') or '?'):<5} {str(c.get('location') or '?'):<12} resource group {c.get('resourceGroup')}"
            + ("   <- carries this kit's capacity name: if it is an old demo F4, delete it yourself (it bills while Active)" if same else ""))
def down(cfg: dict, az: Az, *, make_ops: MakeOps, state_dir: Path, out: Path, say: Say = print, plan_only: bool = False,
         sleeper: Callable[[float], None] = time.sleep) -> int:
    try:
        with Lock(lock_path(state_dir)):
            return _down(cfg, az, make_ops, Path(state_dir), Path(out), say, plan_only, sleeper)
    except LockBusy as ex:
        say(f"BUSY: {ex}")
        return EXIT_LOCKED


def _down(cfg, az, make_ops, state_dir, out, say, plan_only, sleeper) -> int:
    try:
        ledger = Ledger(ledger_path(state_dir))
    except LedgerError as ex:
        say(f"BLOCKED: {ex}")
        return EXIT_BLOCKED
    todo = ledger.wipe_plan()
    kept = [e for e in ledger.live() if not e["owned"]]
    say("WIPE PLAN" if plan_only else "WIPE")
    hints = {"fabricWorkspace": "every item hard-deleted first (the lakehouse with its bronze files and its silver and gold tables included) and the recycle bin emptied; "
                                "the empty shell then sits in Fabric's workspace retention",
             "fabricCapacity": "hard delete",
             "resourceGroup": "hard delete of the whole group, with everything in it (Azure has no soft delete for resource groups)"}
    for e in todo:
        say(f"  delete  {e['kind']:<20} {e['name']}" + (f"   ({hints[e['kind']]})" if e["kind"] in hints else ""))
    for e in kept:
        say(f"  KEEP    {e['kind']:<20} {e['name']}   (not created by this kit)")
    if not todo:
        say("  nothing recorded as created by this kit; nothing to delete")
        say("VERIFIED: nothing to remove. HostVM, its resource group and the repo were never touched.")
        return EXIT_OK
    if plan_only:
        return EXIT_OK
    failures: list[str] = []
    ops: Optional[fabric_ops.Ops] = None
    ws_owned = {e["name"]: e for e in todo if e["kind"] == "fabricWorkspace"}
    sub = None
    try:
        sub = az.who()["subscriptionId"]
    except AzError as ex:
        say(f"  ERROR az is not signed in ({str(ex)[:120]}); Azure objects cannot be deleted")

    def fabric() -> fabric_ops.Ops:
        nonlocal ops
        if ops is None:
            ops = make_ops()
        return ops

    # The resource group is only deleted when it is provably the one this kit created (run tag from the ledger). If not, nothing in
    # Azure is touched: the capacity name could belong to somebody else's group that merely reuses our name.
    azure_blocked: Optional[str] = None
    rg_entry = next((e for e in todo if e["kind"] == "resourceGroup"), None)
    if rg_entry is not None and sub is not None:
        rd = rg_entry.get("details") or {}
        try:
            if rd.get("subscriptionId") and rd["subscriptionId"] != sub:
                azure_blocked = f"recorded in subscription {rd['subscriptionId']}, current az context is {sub}; this kit never changes the subscription"
            elif azure_infra.resource_group_exists(az, rg_entry["name"]):
                azure_blocked = azure_infra.verify_run_tag(az, rg_entry["name"], rg_entry)
        except AzError as ex:
            azure_blocked = f"could not verify the resource group ({str(ex)[:160]})"
        if azure_blocked:
            say(f"  BLOCKED Azure deletes: {azure_blocked}")
            failures.append(f"resourceGroup {rg_entry['name']}: {azure_blocked}")
    cap_entry = next((e for e in todo if e["kind"] == "fabricCapacity"), None)
    if cap_entry is not None and ws_owned and sub is not None and not azure_blocked:
        ccfg0 = {"resourceGroup": (cap_entry.get("details") or {}).get("resourceGroup", ""), "capacity": {"name": cap_entry["name"]}}
        try:
            if azure_infra.capacity_state(ccfg0, az).get("state") == "Paused":
                say("  the capacity is paused: resuming it so the Fabric items can be deleted properly")
                azure_infra.lifecycle(ccfg0, az, "resume", sleeper=sleeper)
        except Exception as ex:  # noqa: BLE001 - best effort; the purge below reports what it could not do
            say(f"  warning: could not resume the capacity ({str(ex)[:120]})")
    shells: list[tuple[str, Optional[str]]] = []
    deleted_ws_ids: list[str] = []
    for e in todo:
        kind, name, d = e["kind"], e["name"], e.get("details") or {}
        try:
            if kind in COLLECTION:
                ws_id = d.get("workspaceId")
                if d.get("workspaceId") and any((w.get("id") == ws_id) for w in ws_owned.values()):
                    say(f"  {kind} {name}: goes away with its workspace")
                    continue
                item_id = e.get("id") or (fabric().fab.find(ws_id, COLLECTION[kind], name) or {}).get("id")
                if item_id:
                    fabric().delete_item(ws_id, COLLECTION[kind], item_id)
                    if kind == "fabricOntology":                                                    # the graph model Fabric created next to it
                        for gm in [i for i in fabric().list_items(ws_id) if i.get("type") == "GraphModel" and str(i.get("displayName", "")).startswith(f"{name}_graph_")]:
                            fabric().delete_item(ws_id, "graphModels", gm["id"])
                if fabric().fab.find(ws_id, COLLECTION[kind], name):
                    raise RuntimeError("still present after delete")
                ledger.mark(kind, name, "deleted")
                say(f"  deleted {kind} {name}")
            elif kind == "fabricWorkspace":
                ws = e.get("id") and {"id": e["id"]} or fabric().find_workspace(name)
                problems: list[str] = []
                if ws:
                    rep = fabric().purge_workspace(ws["id"])
                    say(f"  workspace {name}: {len(rep['itemsDeleted'])} item(s) hard-deleted, {len(rep['recoverablePurged'])} purged from the recycle bin")
                    problems = rep["errors"] + [f"still in the workspace: {x}" for x in rep["itemsLeft"]] + [f"still in the recycle bin: {x}" for x in rep["recoverableLeft"]]
                    if not rep["workspaceDeleted"]:
                        raise RuntimeError("the workspace is still listed after the delete" + (f" ({'; '.join(problems)[:300]})" if problems else ""))
                    deleted_ws_ids.append(ws["id"])
                    shells.append((name, rep["renamedTo"]))
                ledger.mark(kind, name, "deleted", "hard purge incomplete" if problems else None)    # the workspace is gone, whatever else happened
                for child in ledger.entries:
                    if child["kind"] in COLLECTION and (child.get("details") or {}).get("workspaceId") == (ws or {}).get("id") and child["state"] in ("pending", "present"):
                        ledger.mark(child["kind"], child["name"], "deleted", "with workspace")
                if problems:
                    raise RuntimeError("hard purge incomplete, the contents may be recoverable from the workspace retention: " + "; ".join(problems)[:400])
                say(f"  deleted workspace {name}")
            elif kind == "fabricCapacity":
                if azure_blocked:
                    say(f"  capacity {name}: skipped (see the BLOCKED line above); nothing in Azure was touched")
                    continue
                if sub is None or (d.get("subscriptionId") and d["subscriptionId"] != sub):
                    raise RuntimeError(f"recorded in subscription {d.get('subscriptionId')}, current az context is {sub}; this kit never changes the subscription")
                ccfg = {"resourceGroup": d["resourceGroup"], "capacity": {"name": name}}
                azure_infra.lifecycle(ccfg, az, "delete", sleeper=sleeper)
                if azure_infra.capacity_state(ccfg, az)["exists"]:
                    raise RuntimeError("still exists after delete")
                ledger.mark(kind, name, "deleted")
                say(f"  deleted capacity {name}")
            elif kind == "resourceGroup":
                if azure_blocked:
                    continue                                    # reported above; the group is not provably ours, so it stays
                if sub is None:
                    raise RuntimeError("az is not signed in")
                if not azure_infra.resource_group_exists(az, name):
                    ledger.mark(kind, name, "deleted", "already gone")
                    say(f"  resource group {name}: already gone")
                    continue
                others = [r for r in azure_infra.resource_group_resources(az, name) if str(r.get("type", "")).lower() != "microsoft.fabric/capacities"]
                if others:
                    say(f"  note: {len(others)} other resource(s) inside {name} are deleted with it: "
                        + ", ".join(f"{r.get('type')}/{r.get('name')}" for r in others[:8]) + (" ..." if len(others) > 8 else ""))
                azure_infra.delete_resource_group(az, name)
                if azure_infra.resource_group_exists(az, name):
                    raise RuntimeError("still exists after delete")
                ledger.mark(kind, name, "deleted")
                say(f"  deleted resource group {name} (hard delete)")
        except Exception as ex:  # noqa: BLE001 - keep going: stopping the billing capacity matters more than a failed workspace delete
            failures.append(f"{kind} {name}: {str(ex)[:240]}")
            say(f"  ERROR {kind} {name}: {str(ex)[:240]}")
    st = read_state(out)
    if st.get("workspaceId") in deleted_ws_ids:
        (out / ".state.json").unlink(missing_ok=True)
    (state_dir / "tombstones").mkdir(parents=True, exist_ok=True)
    (state_dir / "tombstones" / f"ledger-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json").write_text(
        json.dumps(ledger.data, indent=2), encoding="utf-8")
    say("")
    for e in ledger.entries:
        if e["kind"] in ("fabricCapacity", "fabricWorkspace", "resourceGroup"):
            label = {"deleted": "DELETED", "released": "KEPT (still holds other resources)"}.get(e["state"]) or ("KEPT (not created by this kit)" if not e["owned"] else "STILL PRESENT")
            say(f"  {e['kind']:<16} {e['name']}: {label}")
    if sub is not None:
        audit_capacities(cfg, az, say)
    if shells:
        say("\nNOTE Fabric has no API that permanently deletes a workspace. The emptied workspace stays in the tenant's workspace retention "
            f"(7 days minimum) as '{shells[-1][1] or shells[-1][0]}', then disappears on its own. Nothing recoverable is left inside it (items are hard-deleted; "
            "OneLake keeps the bytes of hard-deleted items for 7 more days, unrecoverable). To remove the shell now, a tenant admin can use "
            "Admin portal > OneLake catalog > Govern > Workspaces > Permanently delete. Azure has no soft delete for resource groups or Fabric capacities.")
    if failures:
        say(f"\nWIPE INCOMPLETE ({len(failures)} failed): re-run wipe.sh to retry. Billing: check `bash build.sh --diagnose`.")
        return EXIT_WIPE_FAILED
    say("\nVERIFIED: everything this kit created is gone: no resource group, no capacity, no Fabric items, no recycle bin. "
        "HostVM, its resource group and the repo were never touched.")
    return EXIT_OK
