"""The one-time identity set-up for the pipelines, scripted so that nobody types it by hand.

  python -m tools.bootstrap apply    --repo owner/name [--subscription ID] [--execute]
      the resource group (empty, made here), an identity for the pipeline, its trust in this repository's main branch, the group the two Fabric tenant
      settings are scoped to, the roles the pipeline needs on the resource group, and the repository's secrets and variables
  python -m tools.bootstrap enable   --repo owner/name [--subscription ID] [--execute]
      switch the pipelines on (CD_ENABLED=true) once apply has been done
  python -m tools.bootstrap consumer --repo owner/name --consumer owner/content [--subscription ID] [--execute]
      onboard a content repository: its own identity, its own trust, a role on the capacity itself (the capacity must exist), and its secrets and variables

It is a dry run unless --execute is given: it reads, says what it would change, and changes nothing. Run it again and it changes nothing that is already
right. No secret is created: the pipelines sign in with a federated credential that trusts runs on the main branch of the named repository only. The
identity ids go into the repository's secrets through standard input, never on a command line.

Everything that can be refused is checked first, read-only, in the dry run too: the repository exists and the signed-in gh account administers it, the
resource group is empty or was made by this project (--adopt to accept another), an existing app trusts nothing but the one repository it is for (any other
federated credential, whatever its name, lets that other repository sign in as the app: --replace-trust removes them), a role that already has the
consumer role's name is exactly the role this project defines, and the capacity exists for a consumer.

It acts on whatever subscription the `az` profile is signed in to. Because that profile can be signed in to a different tenant, --execute refuses to run
unless --subscription names the subscription you mean and it is the signed-in one.
It needs: Owner on the subscription (to register providers, create the resource group, assign roles and, for a consumer, define a role), a directory role
that can create applications and groups, and `gh` signed in with the right to set secrets and variables on the repositories. docs/BOOTSTRAP.md walks through it.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from . import cd
from .cd import CdError
from .platform import SP_GROUP_DEFAULT, TAGS, tenant_settings_guide

PROVIDERS = ("Microsoft.Fabric", "Microsoft.Storage")
APP_NAME = "contoso-promptops-platform"
CONSUMER_APP_PREFIX = "contoso-promptops-consumer-"
CREDENTIAL_NAME = "github-main"
ISSUER = "https://token.actions.githubusercontent.com"
AUDIENCE = "api://AzureADTokenExchange"
ROLE_CONTRIBUTOR = "Contributor"
ROLE_BLOB = "Storage Blob Data Contributor"
ROLE_OPERATOR = "Contoso PromptOps Capacity Operator"
OPERATOR_ACTIONS = ("Microsoft.Fabric/capacities/read", "Microsoft.Fabric/capacities/write", "Microsoft.Fabric/capacities/suspend/action",
                    "Microsoft.Fabric/capacities/resume/action")
SECRET_NAMES = ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID")
REQUIRED_VARIABLES = ("CD_RESOURCE_GROUP", "CD_CAPACITY", "CD_WORKSPACE_DEV", "CD_WORKSPACE_PROD", "CD_ADMIN_OBJECT_IDS", "CD_ADMIN_UPNS", "CD_PIPELINE_OBJECT_ID")
CONSUMER_VARIABLES = ("CD_RESOURCE_GROUP", "CD_CAPACITY", "CD_LOCATION", "CD_WORKSPACE_DEV", "CD_WORKSPACE_PROD")
DEFAULTS = {"resource_group": "rg-contoso-promptops", "capacity": "contosopromptopsf4", "location": "eastus2",
            "workspace_dev": "Contoso-PromptOps-Dev", "workspace_prod": "Contoso-PromptOps-Prod"}
NEW = "<new>"
PROPAGATION_TRIES = 8
PROPAGATION_WAIT_S = 15.0
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
RG_RE = re.compile(r"^[A-Za-z0-9_().\-]{0,88}[A-Za-z0-9_()\-]$")
GROUP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{2,62}$")


def role_shape(doc: Mapping[str, Any]) -> dict[str, list[str]]:
    """What a role definition allows, in one form that can be compared: lower case, sorted. It reads the shape `az role definition list` prints
    (`permissions`, `assignableScopes`) and the shape the role is created from (`Actions`, `NotActions`, ..., `AssignableScopes`)."""
    inner = doc.get("properties") if isinstance(doc.get("properties"), Mapping) else doc
    permissions = inner.get("permissions")
    blocks = list(permissions) if permissions is not None else [inner]

    def pick(*keys: str) -> list[str]:
        return sorted({str(x).lower() for b in blocks for k in keys for x in (b.get(k) or [])})
    scopes = inner.get("assignableScopes") or inner.get("AssignableScopes") or []
    return {"actions": pick("actions", "Actions"), "notActions": pick("notActions", "NotActions"), "dataActions": pick("dataActions", "DataActions"),
            "notDataActions": pick("notDataActions", "NotDataActions"), "assignableScopes": sorted({str(x).lower().rstrip("/") for x in scopes})}


def trust_split(existing: Sequence[Mapping[str, Any]], subject: str) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """(the credentials that are exactly the wanted trust, every other credential). Whatever its name, a federated credential that trusts something else
    lets that something sign in as the app."""
    def wanted(x: Mapping[str, Any]) -> bool:
        return x.get("subject") == subject and x.get("issuer") == ISSUER
    return [x for x in existing if wanted(x)], [x for x in existing if not wanted(x)]


# ---------------------------------------------------------------------------------------------------------------------------------------------
# az and gh, behind one small interface (tests use an in-memory stand-in)
# ---------------------------------------------------------------------------------------------------------------------------------------------
class Cloud:
    def __init__(self, runner: Callable[..., Any] = subprocess.run, az: Optional[str] = None, gh: Optional[str] = None,
                 sleeper: Callable[[float], None] = time.sleep):
        self.runner, self.az, self.gh, self.sleeper = runner, az, gh, sleeper

    # ---- plumbing ---------------------------------------------------------------------------------------------------------------------------
    def _run(self, tool: str, args: Sequence[str], *, input: Optional[str] = None, timeout: float = 300, check: bool = True) -> Any:
        exe = (self.az if tool == "az" else self.gh) or shutil.which(tool) or shutil.which(f"{tool}.cmd")
        if not exe:
            raise CdError(f"the {tool} command line is not installed")
        cmd = [exe, *args, *(["--only-show-errors"] if tool == "az" else [])]
        try:
            r = self.runner(cmd, input=input, text=True, encoding="utf-8", capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as ex:
            raise CdError(f"{tool} {' '.join(args[:3])} did not finish within {int(timeout)} s") from ex
        if check and r.returncode != 0:
            raise CdError(f"{tool} {' '.join(args[:3])} failed ({r.returncode}): {(r.stderr or r.stdout or '').strip()[-400:]}")
        return r

    def _json(self, tool: str, args: Sequence[str], **kw: Any) -> Any:
        r = self._run(tool, [*args, "-o", "json"] if tool == "az" else args, **kw)
        text = (r.stdout or "").strip()
        return json.loads(text) if text else None

    def _propagated(self, args: Sequence[str], wait_for: Sequence[str], already: Sequence[str]) -> None:
        """A new identity or role is not known everywhere at once: try again for about two minutes while the answer says so."""
        for attempt in range(PROPAGATION_TRIES):
            r = self._run("az", args, check=False, timeout=180)
            text = f"{r.stderr or ''} {r.stdout or ''}"
            low = text.lower()
            if r.returncode == 0 or any(x.lower() in low for x in already):
                return
            if attempt < PROPAGATION_TRIES - 1 and any(x.lower() in low for x in wait_for):
                self.sleeper(PROPAGATION_WAIT_S)
                continue
            raise CdError(f"az {' '.join(args[:4])} failed ({r.returncode}): {text.strip()[-400:]}")

    def _file_call(self, args: Sequence[str], flag: str, doc: dict) -> None:
        with tempfile.TemporaryDirectory(prefix="bootstrap-") as folder:
            path = Path(folder) / "definition.json"
            path.write_text(json.dumps(doc), encoding="utf-8", newline="\n")             # UTF-8 without a byte-order mark, which az rejects
            self._run("az", [*args, flag, f"@{path}"], timeout=180)

    # ---- Azure -------------------------------------------------------------------------------------------------------------------------------
    def account(self) -> dict:
        a = self._json("az", ["account", "show"], timeout=90) or {}
        user = a.get("user") or {}
        return {"id": a["id"], "tenantId": a["tenantId"], "name": a.get("name", ""), "user": user.get("name", ""), "userType": user.get("type", "")}

    def provider_state(self, namespace: str) -> str:
        return (self._json("az", ["provider", "show", "-n", namespace], timeout=120) or {}).get("registrationState", "")

    def register_provider(self, namespace: str) -> None:
        self._run("az", ["provider", "register", "-n", namespace, "--wait"], timeout=900)

    def rg_state(self, name: str) -> Optional[dict]:
        """None when the resource group does not exist, else its tags and how many resources it holds (read-only)."""
        if (self._run("az", ["group", "exists", "-n", name], timeout=120).stdout or "").strip().lower() != "true":
            return None
        group = self._json("az", ["group", "show", "-n", name], timeout=120) or {}
        resources = self._json("az", ["resource", "list", "-g", name], timeout=180) or []
        return {"tags": group.get("tags") or {}, "resources": len(resources)}

    def create_rg(self, name: str, location: str, tags: dict) -> None:
        self._run("az", ["group", "create", "-n", name, "-l", location, "--tags", *[f"{k}={v}" for k, v in tags.items()]], timeout=180)

    def capacity_id(self, resource_group: str, name: str) -> Optional[str]:
        """The Azure resource id of the Fabric capacity, or None when it does not exist (read-only)."""
        r = self._run("az", ["resource", "show", "-g", resource_group, "-n", name, "--resource-type", "Microsoft.Fabric/capacities", "--query", "id", "-o", "tsv"],
                      check=False, timeout=120)
        return (r.stdout or "").strip() or None if r.returncode == 0 else None

    def has_role(self, object_id: str, role: str, scope: str) -> bool:
        try:
            return bool(self._json("az", ["role", "assignment", "list", "--assignee", object_id, "--role", role, "--scope", scope], timeout=180))
        except CdError:
            return False                                         # a new identity or a missing group is not known yet; assigning will say if it is a real problem

    def assign_role(self, object_id: str, role: str, scope: str) -> None:
        self._propagated(["role", "assignment", "create", "--assignee-object-id", object_id, "--assignee-principal-type", "ServicePrincipal",
                          "--role", role, "--scope", scope],
                         wait_for=("PrincipalNotFound", "does not exist in the directory", "RoleDefinitionDoesNotExist", "role definition"),
                         already=("RoleAssignmentExists", "already exists"))

    def role_definition(self, name: str, scope: str) -> Optional[dict]:
        """What the custom role of this name allows (see role_shape), or None when there is none. It is looked up at `scope`, because a role that can be assigned
        only inside a resource group is not listed at the subscription."""
        found = [d for d in (self._json("az", ["role", "definition", "list", "--name", name, "--custom-role-only", "true", "--scope", scope], timeout=180) or [])
                 if d.get("roleName") == name]
        if len(found) > 1:
            raise CdError(f"{len(found)} custom roles are called '{name}'; delete the extra ones in the Azure portal so that the right one is used")
        return role_shape(found[0]) if found else None

    def create_role_definition(self, doc: dict) -> None:
        self._file_call(["role", "definition", "create"], "--role-definition", doc)

    # ---- Entra ---------------------------------------------------------------------------------------------------------------------------------
    def find_app(self, name: str) -> Optional[dict]:
        apps = [a for a in (self._json("az", ["ad", "app", "list", "--display-name", name], timeout=180) or []) if a.get("displayName") == name]
        if len(apps) > 1:
            raise CdError(f"{len(apps)} app registrations are called '{name}'; delete the extra ones in the Entra admin centre so that the right one is used")
        return {"appId": apps[0]["appId"], "id": apps[0].get("id") or apps[0]["objectId"]} if apps else None

    def create_app(self, name: str) -> dict:
        a = self._json("az", ["ad", "app", "create", "--display-name", name, "--sign-in-audience", "AzureADMyOrg"], timeout=180) or {}
        return {"appId": a["appId"], "id": a.get("id") or a["objectId"]}

    def find_sp(self, app_id: str) -> Optional[str]:
        r = self._run("az", ["ad", "sp", "show", "--id", app_id, "-o", "json"], check=False, timeout=120)
        if r.returncode != 0 or not (r.stdout or "").strip():
            return None
        sp = json.loads(r.stdout)
        return sp.get("id") or sp.get("objectId")

    def create_sp(self, app_id: str) -> str:
        sp = self._json("az", ["ad", "sp", "create", "--id", app_id], timeout=180) or {}
        return sp.get("id") or sp["objectId"]

    def credentials(self, app_object_id: str) -> list[dict]:
        return self._json("az", ["ad", "app", "federated-credential", "list", "--id", app_object_id], timeout=120) or []

    def add_credential(self, app_object_id: str, name: str, subject: str, description: str) -> None:
        self._file_call(["ad", "app", "federated-credential", "create", "--id", app_object_id], "--parameters",
                        {"name": name, "issuer": ISSUER, "subject": subject, "description": description, "audiences": [AUDIENCE]})

    def remove_credential(self, app_object_id: str, credential_id: str) -> None:
        self._run("az", ["ad", "app", "federated-credential", "delete", "--id", app_object_id, "--federated-credential-id", credential_id], timeout=120)

    def find_entra_group(self, name: str) -> Optional[str]:
        groups = [g for g in (self._json("az", ["ad", "group", "list", "--display-name", name], timeout=180) or []) if g.get("displayName") == name]
        if len(groups) > 1:
            raise CdError(f"{len(groups)} groups are called '{name}'; delete the extra ones in the Entra admin centre so that the right one is used")
        return groups[0].get("id") or groups[0]["objectId"] if groups else None

    def create_entra_group(self, name: str) -> str:
        g = self._json("az", ["ad", "group", "create", "--display-name", name, "--mail-nickname", name], timeout=180) or {}
        return g.get("id") or g["objectId"]

    def is_member(self, group_id: str, member_id: str) -> bool:
        r = self._run("az", ["ad", "group", "member", "check", "--group", group_id, "--member-id", member_id, "-o", "json"], timeout=120)
        return bool((json.loads(r.stdout or "{}") or {}).get("value"))

    def add_member(self, group_id: str, member_id: str) -> None:
        self._propagated(["ad", "group", "member", "add", "--group", group_id, "--member-id", member_id],
                         wait_for=("does not exist", "ResourceNotFound"), already=("already exist",))

    def user_identity(self, who: Optional[str]) -> dict:
        """{'id', 'upn'} of the signed-in user (who is None) or of the user with this user principal name or object id. Fabric wants the UPN for a capacity
        administrator and the object id for a workspace role."""
        args = ["ad", "signed-in-user", "show"] if who is None else ["ad", "user", "show", "--id", who]
        u = self._json("az", args, timeout=120) or {}
        return {"id": (u.get("id") or u["objectId"]).lower(), "upn": u["userPrincipalName"]}

    # ---- GitHub --------------------------------------------------------------------------------------------------------------------------------
    def oidc_prefix(self, repo: str) -> str:
        """The start of the subject GitHub puts in the sign-in token: repositories created since mid-July 2026 carry numeric ids in it."""
        body = self._json("gh", ["api", f"repos/{repo}/actions/oidc/customization/sub"], timeout=60) or {}
        return body.get("sub_claim_prefix") or f"repo:{repo}"

    def repo_permissions(self, repo: str) -> Optional[dict]:
        """None when the repository does not exist or is not visible; else whether the signed-in gh account administers it (needed to set secrets and variables)."""
        r = self._run("gh", ["api", f"repos/{repo}"], check=False, timeout=60)
        if r.returncode != 0:
            return None
        return {"admin": bool((json.loads(r.stdout or "{}").get("permissions") or {}).get("admin"))}

    def secret_names(self, repo: str) -> set[str]:
        return {s["name"] for s in (self._json("gh", ["secret", "list", "--repo", repo, "--json", "name"], timeout=60) or [])}

    def set_secret(self, repo: str, name: str, value: str) -> None:
        self._run("gh", ["secret", "set", name, "--repo", repo], input=value, timeout=60)

    def variables(self, repo: str) -> dict[str, str]:
        return {v["name"]: v["value"] for v in (self._json("gh", ["variable", "list", "--repo", repo, "--json", "name,value"], timeout=60) or [])}

    def set_variable(self, repo: str, name: str, value: str) -> None:
        self._run("gh", ["variable", "set", name, "--repo", repo, "--body", value], timeout=60)


# ---------------------------------------------------------------------------------------------------------------------------------------------
# what to do
# ---------------------------------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    repo: str
    execute: bool = False
    subscription: str = ""
    resource_group: str = DEFAULTS["resource_group"]
    capacity: str = DEFAULTS["capacity"]
    location: str = DEFAULTS["location"]
    workspace_dev: str = DEFAULTS["workspace_dev"]
    workspace_prod: str = DEFAULTS["workspace_prod"]
    group: str = SP_GROUP_DEFAULT
    admins: tuple[str, ...] = ()
    adopt: bool = False                      # accept an existing resource group that is not empty and was not made by this project
    replace_trust: bool = False              # let an app that trusts another repository trust this one instead

    @property
    def variables(self) -> dict[str, str]:
        return {"CD_RESOURCE_GROUP": self.resource_group, "CD_CAPACITY": self.capacity, "CD_LOCATION": self.location,
                "CD_WORKSPACE_DEV": self.workspace_dev, "CD_WORKSPACE_PROD": self.workspace_prod, "CD_SP_GROUP_NAME": self.group}


def validate(cfg: Config, consumer: Optional[str] = None) -> None:
    problems = []
    for what, repo in (("--repo", cfg.repo), ("--consumer", consumer)):
        if repo is not None and not REPO_RE.match(repo):
            problems.append(f"{what} must look like owner/name")
    if not RG_RE.match(cfg.resource_group):
        problems.append("CD_RESOURCE_GROUP is not a valid resource group name (letters, digits, - _ ( ) . and not ending in a period)")
    if not GROUP_RE.match(cfg.group):
        problems.append("the group name must be 3 to 63 letters, digits or hyphens")
    if cfg.workspace_dev == cfg.workspace_prod:
        problems.append("the dev and prod workspaces need different names")
    base = {k: v for k, v in cfg.variables.items() if k != "CD_SP_GROUP_NAME"}
    for env in ("dev", "prod"):
        try:
            cd.load_settings(base, env, "core")
        except CdError as ex:
            problems.append(str(ex))
    if problems:
        raise CdError("; ".join(dict.fromkeys(problems)))


def check_account(acct: dict, cfg: Config) -> None:
    """The az profile on this PC can be signed in to a different tenant, so --execute needs the subscription named, and it has to be the signed-in one.
    (tools/denied_ids.sha256 is no help here: it lists the ids that must not appear in files, which includes the demo's own.)"""
    if cfg.execute and not cfg.subscription:
        raise CdError("--subscription is required with --execute: name the subscription you mean, because the az profile on this PC can be signed in to a "
                      "different tenant. Nothing was changed")
    if cfg.subscription and cfg.subscription.lower() != acct["id"].lower():
        raise CdError("the az profile is signed in to a different subscription than --subscription. Nothing was changed")


class Rows:
    def __init__(self, say: Callable[[str], None]):
        self.rows: list[tuple[str, str, str]] = []
        self.say = say

    def add(self, state: str, what: str, detail: str = "") -> None:
        self.rows.append((state, what, detail))
        self.say(f"  {state:<8} {what}" + (f": {detail}" if detail else ""))

    def count(self, state: str) -> int:
        return sum(1 for r in self.rows if r[0] == state)


def ensure(rows: Rows, execute: bool, what: str, present: bool, make: Callable[[], Any], detail: str = "created") -> None:
    if present:
        rows.add("ok", what, "already there")
    elif not execute:
        rows.add("would", what, "would create")
    else:
        make()
        rows.add("changed", what, detail)


def ensure_identity(c: Any, cfg: Config, rows: Rows, name: str, repo: str, group_id: str, prefix: str) -> tuple[dict, str]:
    """The app registration, its service principal, its trust in the main branch of `repo` and its place in the group. Both ids are NEW in a dry run
    that would create them. `prefix` is the repository's sign-in subject prefix, read from GitHub before anything was changed."""
    check_trust(c, cfg, name, repo, prefix)                                 # also checked up front by apply and consumer; cheap, and it protects a direct caller
    app = c.find_app(name)
    subject = f"{prefix}:ref:refs/heads/main"
    existing = c.credentials(app["id"]) if app else []
    if app:
        rows.add("ok", f"app registration {name}", "already there")
    elif cfg.execute:
        app = c.create_app(name)
        rows.add("changed", f"app registration {name}", "created")
    else:
        app = {"appId": NEW, "id": NEW}
        rows.add("would", f"app registration {name}", "would create")
    real = app["id"] != NEW
    sp = c.find_sp(app["appId"]) if real else None
    if sp:
        rows.add("ok", "service principal", "already there")
    elif cfg.execute:
        sp = c.create_sp(app["appId"])
        rows.add("changed", "service principal", "created")
    else:
        sp = NEW
        rows.add("would", "service principal", "would create")
    what = f"trust in the main branch of {repo}"
    expected, foreign = trust_split(existing, subject)
    for stale in foreign:                                                   # only reached with --replace-trust (check_trust refused otherwise); first, so that a name is free again
        label = f"trust in '{stale.get('subject')}' (credential '{stale.get('name')}')"
        if cfg.execute:
            c.remove_credential(app["id"], stale["id"])
            rows.add("changed", label, "removed")
        else:
            rows.add("would", label, "would remove")
    if expected:
        rows.add("ok", what, "already there")
    elif not cfg.execute:
        rows.add("would", what, "would create")
    else:
        c.add_credential(app["id"], CREDENTIAL_NAME, subject, f"GitHub Actions on the main branch of {repo}")
        rows.add("changed", what, "created")
    member = real and sp != NEW and group_id != NEW and c.is_member(group_id, sp)
    ensure(rows, cfg.execute, f"{name} in the group", member, lambda: c.add_member(group_id, sp), "added")
    return app, sp


def ensure_role(c: Any, cfg: Config, rows: Rows, sp: str, role: str, scope_name: str, scope: str) -> None:
    has = sp != NEW and c.has_role(sp, role, scope)
    ensure(rows, cfg.execute, f"role '{role}' on {scope_name}", has, lambda: c.assign_role(sp, role, scope), "assigned")


def set_variables(c: Any, cfg: Config, rows: Rows, repo: str, wanted: dict[str, str]) -> None:
    current = c.variables(repo)
    for name, value in wanted.items():
        if current.get(name) == value:
            rows.add("ok", f"variable {name} on {repo}", "already set")
        elif not cfg.execute:
            rows.add("would", f"variable {name} on {repo}", "would set once the identity exists" if value == NEW else f"would set to {value}")
        else:
            c.set_variable(repo, name, value)
            rows.add("changed", f"variable {name} on {repo}", f"set to {value}")


def set_secrets(c: Any, cfg: Config, rows: Rows, repo: str, wanted: dict[str, str]) -> None:
    """A secret cannot be read back, so it is written again every time (the same value): harmless, and it repairs a stale one."""
    for name, value in wanted.items():
        if not cfg.execute:
            rows.add("would", f"secret {name} on {repo}", "would set")
        else:
            c.set_secret(repo, name, value)
            rows.add("changed", f"secret {name} on {repo}", "set")


def administrators(c: Any, acct: dict, cfg: Config) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(object ids, user principal names) of the people who administer the capacity and the workspaces: the signed-in user and every --admin."""
    people = [c.user_identity(None)] if acct.get("userType") == "user" else []
    people += [c.user_identity(who) for who in cfg.admins]
    if not people:
        raise CdError("no administrator could be determined: the az profile is not signed in as a user. Pass --admin <user principal name or object id>")
    ids = tuple(dict.fromkeys(p["id"].lower() for p in people))
    upns = tuple(dict.fromkeys(p["upn"] for p in people))
    return ids, upns


def check_repository(c: Any, repo: str, what: str) -> str:
    """Read-only, before anything is changed: the repository exists, the signed-in gh account administers it (it must set secrets and variables), and
    its sign-in subject prefix is known. Returns the prefix."""
    perms = c.repo_permissions(repo)
    if perms is None:
        raise CdError(f"{what} {repo} does not exist or is not visible to gh. Check the name (a typo here would bind an Azure identity to the wrong repository). Nothing was changed")
    if not perms["admin"]:
        raise CdError(f"the signed-in gh account does not administer {repo}, so it cannot set the repository's secrets and variables. Nothing was changed")
    return c.oidc_prefix(repo)


def resource_group_verdict(cfg: Config, state: Optional[dict]) -> str:
    """'new' | 'ours' | 'empty' | 'adopted'. The resource group is meant to be empty and made by this project: the pipeline's identity gets Contributor
    over all of it, so a group that is not empty and was not made here is refused unless --adopt says otherwise. Read-only."""
    if state is None:
        return "new"
    if state["tags"].get("managedBy") == TAGS["managedBy"]:
        return "ours"
    if state["resources"] == 0:
        return "empty"
    if cfg.adopt:
        return "adopted"
    raise CdError(f"resource group {cfg.resource_group} exists, holds {state['resources']} resources and was not made by this project. The pipeline's identity "
                  "would get Contributor over all of it. Choose another --resource-group, or pass --adopt if you are sure. Nothing was changed")


def check_trust(c: Any, cfg: Config, name: str, repo: str, prefix: str) -> None:
    """Read-only. An app that trusts anything besides the one subject this run would give it is not silently taken over, whatever the other credentials are
    called: each one lets another repository (or branch) sign in as the app, and the platform's app can change Azure. A typo in --repo or --consumer, or an app
    that was made by hand, would otherwise hand a privileged identity to the wrong place. --replace-trust says it is meant, and removes them."""
    app = c.find_app(name)
    if not app:
        return
    subject = f"{prefix}:ref:refs/heads/main"
    _expected, foreign = trust_split(c.credentials(app["id"]), subject)
    if foreign and not cfg.replace_trust:
        listed = ", ".join(f"'{x.get('subject')}' (credential '{x.get('name')}')" for x in foreign)
        raise CdError(f"the app '{name}' already trusts {listed}, but this run would make it trust '{subject}' only. If that is a mistake in --repo or --consumer, "
                      "fix it; if you mean it, run again with --replace-trust, which removes the other credentials. Nothing was changed")


def apply(c: Any, cfg: Config, say: Callable[[str], None] = print) -> Rows:
    validate(cfg)
    acct = c.account()
    check_account(acct, cfg)
    rows = Rows(say)
    sub = acct["id"]
    scope = f"/subscriptions/{sub}/resourceGroups/{cfg.resource_group}"
    # everything that can be refused is checked here, read-only, before anything is changed: a run that cannot finish should not start
    prefix = check_repository(c, cfg.repo, "the platform repository")
    admin_ids, admin_upns = administrators(c, acct, cfg)
    rg = c.rg_state(cfg.resource_group)
    rg_verdict = resource_group_verdict(cfg, rg)
    check_trust(c, cfg, APP_NAME, cfg.repo, prefix)
    say(f"signed in as {acct['user'] or 'an identity'} ({acct['userType']}), subscription '{acct['name']}' (ending {sub[-4:]})")
    say("This is a dry run: nothing is changed. Add --execute (with --subscription) to do it.\n" if not cfg.execute else "Executing:\n")
    for ns in PROVIDERS:
        ensure(rows, cfg.execute, f"resource provider {ns}", c.provider_state(ns) == "Registered", lambda ns=ns: c.register_provider(ns), "registered")
    what = f"resource group {cfg.resource_group} ({cfg.location})"
    if rg_verdict == "new" and cfg.execute:
        c.create_rg(cfg.resource_group, cfg.location, TAGS)
        rows.add("changed", what, "created, empty")
    elif rg_verdict == "new":
        rows.add("would", what, "would create, empty")
    else:
        rows.add("ok", what, "already there" + {"ours": "", "empty": " (empty, not made by this project)", "adopted": " (ADOPTED: not empty, not made by this project)"}[rg_verdict])
    what = f"group {cfg.group} (the Fabric tenant settings are scoped to it)"
    group_id = c.find_entra_group(cfg.group)
    if group_id:
        rows.add("ok", what, "already there")
    elif cfg.execute:
        group_id = c.create_entra_group(cfg.group)
        rows.add("changed", what, "created")
    else:
        group_id = NEW
        rows.add("would", what, "would create")
    app, sp = ensure_identity(c, cfg, rows, APP_NAME, cfg.repo, group_id, prefix)
    for role in (ROLE_CONTRIBUTOR, ROLE_BLOB):
        ensure_role(c, cfg, rows, sp, role, f"resource group {cfg.resource_group}", scope)
    set_secrets(c, cfg, rows, cfg.repo, {"AZURE_CLIENT_ID": app["appId"], "AZURE_TENANT_ID": acct["tenantId"], "AZURE_SUBSCRIPTION_ID": sub})
    set_variables(c, cfg, rows, cfg.repo, {**cfg.variables, "CD_ADMIN_OBJECT_IDS": ",".join(admin_ids), "CD_ADMIN_UPNS": ",".join(admin_upns), "CD_PIPELINE_OBJECT_ID": sp})
    return rows


def enable(c: Any, cfg: Config, say: Callable[[str], None] = print) -> Rows:
    validate(cfg)
    acct = c.account()
    check_account(acct, cfg)
    rows = Rows(say)
    have_vars, have_secrets = c.variables(cfg.repo), c.secret_names(cfg.repo)
    missing = [f"variable {v}" for v in REQUIRED_VARIABLES if not have_vars.get(v)] + [f"secret {s}" for s in SECRET_NAMES if s not in have_secrets]
    if missing:
        raise CdError(f"{cfg.repo} is not ready: {', '.join(missing)} not set. Run `python -m tools.bootstrap apply --repo {cfg.repo} --execute` first")
    if have_vars.get("CD_ENABLED") == "true":
        rows.add("ok", f"CD_ENABLED on {cfg.repo}", "already true")
    elif not cfg.execute:
        rows.add("would", f"CD_ENABLED on {cfg.repo}", "would set to true")
    else:
        c.set_variable(cfg.repo, "CD_ENABLED", "true")
        rows.add("changed", f"CD_ENABLED on {cfg.repo}", "set to true: the workflows run now, and the capacity guard (every 3 hours) pauses a forgotten capacity")
    return rows


def operator_role(subscription: str, resource_group: str) -> dict:
    return {"Name": ROLE_OPERATOR, "IsCustom": True, "Description": "Read, write, pause and resume a Fabric capacity (Azure needs write for pause and resume), "
                                                                     "for the content pipelines of the Contoso PromptOps demo. No delete, no data access. Assign it on the capacity itself.",
            "Actions": list(OPERATOR_ACTIONS), "NotActions": [], "DataActions": [], "NotDataActions": [],
            "AssignableScopes": [f"/subscriptions/{subscription}/resourceGroups/{resource_group}"]}


def consumer(c: Any, cfg: Config, consumer_repo: str, say: Callable[[str], None] = print) -> Rows:
    validate(cfg, consumer_repo)
    acct = c.account()
    check_account(acct, cfg)
    rows = Rows(say)
    platform = c.variables(cfg.repo)
    missing = [v for v in CONSUMER_VARIABLES if not platform.get(v)]
    if missing:
        raise CdError(f"{cfg.repo} has no value for {', '.join(missing)}: run `python -m tools.bootstrap apply --repo {cfg.repo} --execute` first")
    # everything that can be refused is checked here, read-only, before anything is changed
    check_repository(c, cfg.repo, "the platform repository")
    prefix = check_repository(c, consumer_repo, "the content repository")
    group_name = platform.get("CD_SP_GROUP_NAME") or cfg.group
    group_id = c.find_entra_group(group_name)
    if not group_id:
        raise CdError(f"the group {group_name} does not exist: run `python -m tools.bootstrap apply --repo {cfg.repo} --execute` first")
    sub = acct["id"]
    resource_group = platform["CD_RESOURCE_GROUP"]
    capacity = c.capacity_id(resource_group, platform["CD_CAPACITY"])
    if not capacity:
        raise CdError(f"capacity {platform['CD_CAPACITY']} does not exist in {resource_group} yet. The content repository's role is assigned on the capacity itself (so that "
                      f"it can run this capacity and cannot create another): run the platform workflow with action 'build' first, then this. Nothing was changed")
    wanted_role = operator_role(sub, resource_group)
    have_role = c.role_definition(ROLE_OPERATOR, f"/subscriptions/{sub}/resourceGroups/{resource_group}")
    if have_role is not None and have_role != role_shape(wanted_role):
        raise CdError(f"a custom role called '{ROLE_OPERATOR}' already exists, but it is not the role this project defines (it allows "
                      f"{', '.join(have_role['actions']) or 'nothing'}; assignable at {', '.join(have_role['assignableScopes']) or 'nowhere'}). It would be given to the "
                      "content repository's identity, so a role is not trusted by its name. Delete or rename it (Azure portal, Access control (IAM), Roles), or make it "
                      f"match: actions {', '.join(OPERATOR_ACTIONS)} and nothing else. Nothing was changed")
    say(f"onboarding {consumer_repo} to the platform of {cfg.repo}" + ("" if cfg.execute else " (dry run: nothing is changed)") + "\n")
    owner, repo_name = consumer_repo.split("/", 1)
    app_name = f"{CONSUMER_APP_PREFIX}{owner}-{repo_name}"
    check_trust(c, cfg, app_name, consumer_repo, prefix)                    # before anything is changed
    app, sp = ensure_identity(c, cfg, rows, app_name, consumer_repo, group_id, prefix)
    ensure(rows, cfg.execute, f"role definition '{ROLE_OPERATOR}'", have_role is not None, lambda: c.create_role_definition(wanted_role))
    ensure_role(c, cfg, rows, sp, ROLE_OPERATOR, f"capacity {platform['CD_CAPACITY']}", capacity)
    known = [x for x in platform.get("CD_CONSUMER_OBJECT_IDS", "").replace(";", ",").split(",") if x.strip()]
    if sp.lower() in (k.strip().lower() for k in known):
        rows.add("ok", f"{consumer_repo} in CD_CONSUMER_OBJECT_IDS", "already listed")
    elif not cfg.execute:
        rows.add("would", f"{consumer_repo} in CD_CONSUMER_OBJECT_IDS", "would add")
    else:
        c.set_variable(cfg.repo, "CD_CONSUMER_OBJECT_IDS", ",".join([*(k.strip() for k in known), sp]))
        rows.add("changed", f"{consumer_repo} in CD_CONSUMER_OBJECT_IDS", "added: the next platform build makes it a capacity administrator and a workspace contributor")
    set_secrets(c, cfg, rows, consumer_repo, {"AZURE_CLIENT_ID": app["appId"], "AZURE_TENANT_ID": acct["tenantId"], "AZURE_SUBSCRIPTION_ID": sub})
    set_variables(c, cfg, rows, consumer_repo, {k: platform[k] for k in CONSUMER_VARIABLES})
    return rows


# ---------------------------------------------------------------------------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------------------------------------------------------------------------
def next_steps(cmd: str, cfg: Config, consumer_repo: Optional[str] = None) -> str:
    sub = "--subscription <the subscription id>"
    if cmd == "apply":
        g = tenant_settings_guide(cfg.group)
        return "\n".join([
            "",
            "Next. A Fabric administrator makes two tenant-wide choices (about 2 minutes; the pipeline cannot, and must not, do this for you):",
            f"  1. {g['api']}",
            f"  2. {g['workspaces']}",
            f"  The group {cfg.group} holds the pipeline's identity. A change can take up to 15 minutes to reach Fabric.",
            "Then switch the pipelines on, and run the platform workflow with action 'check' (free) to see whether every door is open:",
            f"  python -m tools.bootstrap enable --repo {cfg.repo} {sub} --execute"]).replace("**", "")
    if cmd == "enable":
        return "\nNext: run the 'platform' workflow with action 'check' (free), then 'build' (starts the F4 and bills while it runs).\n"
    return "\n".join(["", f"Next: run the 'platform' workflow of {cfg.repo} with action 'build' once, so that the capacity administrators and the workspace roles include",
                     f"{consumer_repo}'s identity. After that the pipelines of {consumer_repo} can sign in."])


def main(argv: Optional[list[str]] = None, cloud: Any = None, say: Callable[[str], None] = print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--repo", required=True, help="the platform repository, owner/name")
        p.add_argument("--subscription", default="", help="the subscription id you mean; required with --execute")
        p.add_argument("--execute", action="store_true", help="change things; without it nothing is changed")
    p = sub.add_parser("apply")
    common(p)
    p.add_argument("--resource-group", default=DEFAULTS["resource_group"])
    p.add_argument("--capacity", default=DEFAULTS["capacity"])
    p.add_argument("--location", default=DEFAULTS["location"])
    p.add_argument("--workspace-dev", default=DEFAULTS["workspace_dev"])
    p.add_argument("--workspace-prod", default=DEFAULTS["workspace_prod"])
    p.add_argument("--group", default=SP_GROUP_DEFAULT, help="the Entra group the Fabric tenant settings are scoped to")
    p.add_argument("--admin", action="append", default=[], help="another person who administers the capacity and the workspaces (user principal name or object id)")
    p.add_argument("--adopt", action="store_true", help="accept an existing resource group that is not empty and was not made by this project (the identity gets Contributor over all of it)")
    p.add_argument("--replace-trust", action="store_true", help="let the app, which trusts another repository, trust this one instead")
    common(sub.add_parser("enable"))
    p = sub.add_parser("consumer")
    common(p)
    p.add_argument("--consumer", required=True, help="the content repository, owner/name")
    p.add_argument("--replace-trust", action="store_true", help="let the app, which trusts another repository, trust this one instead")
    a = ap.parse_args(argv)
    cfg = Config(repo=a.repo, execute=a.execute, subscription=a.subscription, replace_trust=getattr(a, "replace_trust", False))
    if a.cmd == "apply":
        cfg = Config(repo=a.repo, execute=a.execute, subscription=a.subscription, resource_group=a.resource_group, capacity=a.capacity, location=a.location,
                     workspace_dev=a.workspace_dev, workspace_prod=a.workspace_prod, group=a.group, admins=tuple(a.admin), adopt=a.adopt, replace_trust=a.replace_trust)
    try:
        c = cloud or Cloud()
        rows = apply(c, cfg, say) if a.cmd == "apply" else enable(c, cfg, say) if a.cmd == "enable" else consumer(c, cfg, a.consumer, say)
    except CdError as ex:
        say(f"\nERROR: {ex}")
        return cd.EXIT_CONFIG
    changed, would = rows.count("changed"), rows.count("would")
    say(f"\n{changed} changed, {would} would change, {rows.count('ok')} already right." if cfg.execute else f"\nDry run: {would} would change, {rows.count('ok')} already right. Nothing was changed.")
    say(next_steps(a.cmd, cfg, getattr(a, "consumer", None)))
    return cd.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
