"""Deploy to Fabric from a pipeline or a laptop: resume the capacity, deploy, verify, pause it again.

DRAFT. It drives the kit's documented commands and is tested with fakes; it has not run against a live tenant yet. The one-time setup
it needs and the first live run are in docs/SETUP.md. Whatever bills or changes Fabric happens in this file and in the kit; the
workflows only sign in and call it.

  python -m tools.cd plan     --env dev            show the settings and the steps; calls nothing
  python -m tools.cd deploy   --env dev            build, resume the F4, load if needed, deploy, graph, verify, pause the F4
  python -m tools.cd capacity status|resume|pause|guard

Settings come from environment variables (GitHub Actions variables): CD_RESOURCE_GROUP, CD_CAPACITY, CD_WORKSPACE_DEV, CD_WORKSPACE_PROD and
optionally CD_LOCATION (default eastus2), CD_TIER (default: the kit's tier) and CD_USD_PER_HOUR (default 0.72, the East US 2 list price of an F4).
The capacity is paused at the end of every deploy, also when a step failed; --keep-running leaves it on and the guard pauses it later.
`guard` pauses an Active capacity unless CD_GUARD_HOLD_UNTIL (an ISO time such as 2026-11-05T20:00:00Z, set before a demo) is still in the future.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from . import export_fabric
from .promptlib import ROOT

EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_STILL_RUNNING = 0, 1, 2, 4
DEFAULT_LOCATION = "eastus2"
DEFAULT_USD_PER_HOUR = 0.72
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()\-]{0,127}$")
CAPACITY_RE = re.compile(r"^[a-z][a-z0-9]{2,62}$")
LOCATION_RE = re.compile(r"^[a-z][a-z0-9]*$")
WORKSPACE_VARS = {"dev": "CD_WORKSPACE_DEV", "prod": "CD_WORKSPACE_PROD"}
NOT_BILLING = {"Paused", "Suspended", "Deleting", "Deleted", "Failed"}                # any other state (Active, Resuming, Scaling, unknown) may be billing
TOKEN_AUDIENCES = ("https://management.azure.com", "https://api.fabric.microsoft.com", "https://analysis.windows.net/powerbi/api", "https://storage.azure.com")


class CdError(RuntimeError):
    """A setting is missing or a step cannot run; the message says what to change."""


class StepFailed(RuntimeError):
    def __init__(self, step: str, detail: str):
        super().__init__(f"{step}: {detail}")
        self.step = step


@dataclass(frozen=True)
class Settings:
    resource_group: str
    capacity: str
    location: str
    workspace: str
    tier: str
    env_name: str
    usd_per_hour: float

    def kit_env(self, state_dir: Path, root: Path = ROOT) -> dict[str, str]:
        env = {"CONTOSO_RESOURCE_GROUP": self.resource_group, "CONTOSO_LOCATION": self.location, "CONTOSO_CAPACITY_NAME": self.capacity,
               "CONTOSO_CAPACITY_SKU": "F4", "CONTOSO_TIER": self.tier, "CONTOSO_STATE_DIR": str(state_dir), "CONTOSO_REPO": str(root / "seed")}
        if self.workspace:
            env["CONTOSO_WORKSPACE_NAME"] = self.workspace
        return env


def load_settings(environ: Mapping[str, str], env_name: Optional[str], default_tier: str) -> Settings:
    def get(key: str, default: str = "") -> str:
        return (environ.get(key) or default).strip()

    ws_var = WORKSPACE_VARS.get(env_name or "")
    missing = [k for k in ["CD_RESOURCE_GROUP", "CD_CAPACITY"] + ([ws_var] if ws_var else []) if not get(k)]
    if missing:
        raise CdError("missing repository variables: " + ", ".join(missing) + " (see docs/SETUP.md)")
    s = Settings(resource_group=get("CD_RESOURCE_GROUP"), capacity=get("CD_CAPACITY"), location=get("CD_LOCATION", DEFAULT_LOCATION),
                 workspace=get(ws_var) if ws_var else "", tier=get("CD_TIER", default_tier), env_name=env_name or "", usd_per_hour=DEFAULT_USD_PER_HOUR)
    bad = [name for name, ok in (("CD_RESOURCE_GROUP", NAME_RE.match(s.resource_group)), ("CD_CAPACITY", CAPACITY_RE.match(s.capacity)),
                                 ("CD_LOCATION", LOCATION_RE.match(s.location)), (ws_var or "", not ws_var or NAME_RE.match(s.workspace)),
                                 ("CD_TIER", s.tier in ("smoke", "core", "full"))) if name and not ok]
    try:
        usd = float(get("CD_USD_PER_HOUR", str(DEFAULT_USD_PER_HOUR)))
        if usd < 0:
            raise ValueError
    except ValueError:
        bad.append("CD_USD_PER_HOUR")
        usd = DEFAULT_USD_PER_HOUR
    if bad:
        raise CdError("invalid values for: " + ", ".join(bad) + " (see docs/SETUP.md)")
    return Settings(s.resource_group, s.capacity, s.location, s.workspace, s.tier, s.env_name, usd)


def describe_plan(s: Settings) -> str:
    return "\n".join([
        f"Environment: {s.env_name or 'capacity only'}",
        f"Capacity:    {s.capacity} (F4) in resource group {s.resource_group}, {s.location}",
        f"Workspace:   {s.workspace or '-'}",
        f"Tier:        {s.tier}",
        "Steps:",
        "  1. check the sign-in and fetch a token for each service the kit talks to",
        "  2. build the data layers and the definitions offline (no Azure)",
        f"  3. resume the capacity (bills about USD {s.usd_per_hour:.2f} an hour until step 9)",
        "  4. find the workspace and the lakehouse (the lakehouse is created when missing)",
        "  5. load the data when planned tables are missing, or with --reload-data",
        "  6. deploy the semantic model and the ontology (an existing item is replaced after a backup)",
        "  7. build the graph",
        "  8. verify the ontology and the tables",
        "  9. pause the capacity, also when a step failed",
    ])


class Live:
    """The real thing: the kit's command line in a subprocess, the kit's Python API in this process."""

    def __init__(self, root: Path = ROOT, state_dir: Optional[Path] = None, runner: Callable[..., Any] = subprocess.run, az: Optional[str] = None):
        self.root = root
        self.state_dir = state_dir or Path(tempfile.mkdtemp(prefix="contoso-state-"))
        self.runner = runner
        self.az = az

    def _env(self, s: Settings) -> dict[str, str]:
        return {**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", **s.kit_env(self.state_dir, self.root)}

    def _kit(self, s: Settings, *args: Any, capture: bool = False) -> Any:
        cmd = [sys.executable, "-m", "contoso_onto.cli", *[str(a) for a in args]]
        return self.runner(cmd, cwd=self.root / "kit", env=self._env(s), text=True, encoding="utf-8", capture_output=capture)

    def warm_tokens(self, s: Settings) -> None:
        """Sign-in check, and a token for every audience the kit uses, fetched now. A federated (OIDC) sign-in cannot mint tokens for new audiences once
        its assertion has expired, so a step that first asks for one 15 minutes into the job could fail; tokens fetched here stay in the az cache."""
        az = self.az or shutil.which("az") or shutil.which("az.cmd")
        if not az:
            raise CdError("the az command line is not installed")
        for resource in TOKEN_AUDIENCES:
            r = self.runner([az, "account", "get-access-token", "--resource", resource, "--query", "expiresOn", "-o", "tsv", "--only-show-errors"],
                            text=True, encoding="utf-8", capture_output=True)
            if r.returncode != 0:
                raise CdError(f"the signed-in identity cannot get a token for {resource}: {(r.stderr or r.stdout).strip()[:300]}. "
                              f"Sign in first (azure/login in a workflow, az login on a laptop) and check docs/SETUP.md.")

    def kit(self, s: Settings, *args: Any) -> int:
        return self._kit(s, *args).returncode

    def capacity(self, s: Settings, action: str) -> dict:
        verb = {"status": "status", "resume": "resume", "pause": "suspend"}[action]
        r = self._kit(s, "capacity", verb, *([] if action == "status" else ["--execute"]), capture=True)
        if r.returncode != 0:
            raise CdError(f"capacity {verb} failed: {(r.stdout + r.stderr).strip()[-600:]}")
        if action != "status":
            return self.capacity(s, "status")
        try:
            return json.loads(r.stdout[r.stdout.index("{"):])
        except ValueError as ex:
            raise CdError(f"capacity status did not answer with JSON: {r.stdout.strip()[-300:]}") from ex

    def build_offline(self, s: Settings, out: Path) -> Path:
        try:
            return export_fabric.run_build(self.root, s.tier, out)
        except export_fabric.ExportError as ex:
            raise CdError(str(ex)) from ex

    def _kit_modules(self) -> Any:
        kit_dir = str(self.root / "kit")
        if kit_dir not in sys.path:
            sys.path.insert(0, kit_dir)
        from contoso_onto import cli, layers, runner, spec
        from contoso_onto.gold import load_model
        return cli, layers, runner, spec, load_model

    def bootstrap(self, s: Settings, out: Path) -> dict:
        cli, *_ = self._kit_modules()
        cfg = cli.load_config(None, env={**os.environ, **s.kit_env(self.state_dir, self.root)}, state_dir=self.state_dir)
        ops = cli.make_ops()
        ws = ops.find_workspace(s.workspace)
        if ws is None:
            raise CdError(f"workspace '{s.workspace}' was not found, or the identity has no role in it: create it and add the identity as Admin (docs/SETUP.md)")
        if not ws.get("capacityId"):
            raise CdError(f"workspace '{s.workspace}' is not assigned to a capacity: assign it to {s.capacity}")
        lh = ops.ensure_lakehouse(ws["id"], cfg["lakehouse"]["name"])
        keep = {k: lh[k] for k in ("lakehouseId", "lakehouseName", "sqlEndpoint", "sqlDatabase")}
        cli.write_state(out, workspaceId=ws["id"], workspaceName=s.workspace, capacityName=s.capacity, resourceGroup=s.resource_group, **keep)
        return {"workspaceId": ws["id"], **keep}

    def missing_tables(self, out: Path) -> list[str]:
        cli, layers, *_ = self._kit_modules()
        st = cli.read_state(out)
        rep = cli.make_ops().layer_report(layers.load_plan(out), st["workspaceId"], st["lakehouseId"])
        return [t for layer in rep["layers"].values() for t in layer["missing"]]

    def graph(self, s: Settings, out: Path) -> dict:
        cli, _layers, runner, spec, load_model = self._kit_modules()
        st = cli.read_state(out)
        sp = spec.make_spec(load_model(cli.ROOT / "model"), s.tier, spec.DEFAULT_DESCRIPTIONS)
        ws = {"workspaceId": st["workspaceId"], "workspaceName": st["workspaceName"]}
        lh = {"lakehouseId": st["lakehouseId"], "sqlEndpoint": st.get("sqlEndpoint"), "sqlDatabase": st.get("sqlDatabase")}
        return runner.materialize_graph(cli.make_ops(), sp, ws, lh, out, print)


@dataclass
class Report:
    steps: list[tuple[str, str, float]] = field(default_factory=list)
    active_seconds: Optional[float] = None
    notes: list[str] = field(default_factory=list)


def render_summary(s: Settings, rep: Report, rc: int) -> str:
    lines = [f"### Deploy to {s.env_name or 'capacity'}: " + ("done" if rc == EXIT_OK else "FAILED"), "",
             f"Capacity `{s.capacity}`, workspace `{s.workspace or '-'}`, tier `{s.tier}`.", "", "| Step | Result | Seconds |", "| --- | --- | --- |"]
    lines += [f"| {name} | {status} | {secs:.0f} |" for name, status, secs in rep.steps]
    if rep.active_seconds is not None:
        mins = rep.active_seconds / 60
        lines += ["", f"The capacity was resumed for about {mins:.0f} minutes, about USD {mins / 60 * s.usd_per_hour:.2f} at the list price (an estimate, not a bill)."]
    lines += [""] + [f"- {n}" for n in rep.notes]
    return "\n".join(lines) + "\n"


def pause(s: Settings, io: Any, say: Callable[[str], None]) -> int:
    try:
        st = io.capacity(s, "pause")
    except Exception as ex:  # noqa: BLE001 - whatever went wrong, the person must know the capacity may still be running
        say(f"::error::THE CAPACITY MAY STILL BE RUNNING ({str(ex)[:300]}). Pause it now: Azure portal, capacity {s.capacity}, Pause; or run the capacity workflow with action pause.")
        return EXIT_STILL_RUNNING
    if st.get("state") != "Paused":
        say(f"::error::THE CAPACITY IS NOT PAUSED (state: {st.get('state')}). Pause it now: Azure portal, capacity {s.capacity}, Pause.")
        return EXIT_STILL_RUNNING
    say(f"capacity {s.capacity} is Paused")
    return EXIT_OK


def deploy(s: Settings, io: Any, out: Path, reload_data: bool = False, keep_running: bool = False, say: Callable[[str], None] = print,
           clock: Callable[[], float] = time.monotonic) -> tuple[int, Report]:
    rep = Report()
    rc = EXIT_OK
    resume_started: Optional[float] = None

    def step(name: str, fn: Callable[[], Any]) -> Any:
        say(f"\n== {name}")
        t0 = clock()
        try:
            res = fn()
        except StepFailed:
            rep.steps.append((name, "failed", clock() - t0))
            raise
        except Exception as ex:  # noqa: BLE001 - reported with the step that failed
            rep.steps.append((name, "failed", clock() - t0))
            raise StepFailed(name, str(ex)[:600]) from ex
        rep.steps.append((name, "ok", clock() - t0))
        return res

    def kit_step(name: str, *args: Any) -> None:
        def run() -> None:
            code = io.kit(s, *args)
            if code != 0:
                raise StepFailed(name, f"the kit exited with {code}; its output is above")
        step(name, run)

    try:
        step("check the sign-in and fetch the tokens", lambda: io.warm_tokens(s))
        step("build the layers and the definitions offline", lambda: io.build_offline(s, out))
        resume_started = clock()
        step("resume the capacity", lambda: io.capacity(s, "resume"))
        step("find the workspace and the lakehouse", lambda: io.bootstrap(s, out))
        missing = step("check which planned tables the lakehouse already has", lambda: io.missing_tables(out))
        if reload_data or missing:
            say(f"data: {'reload requested' if reload_data else f'{len(missing)} planned tables are missing'}")
            kit_step("load the data", "load", "--execute", "--out", out)
        else:
            say("data: every planned table is already in the lakehouse; load skipped (use --reload-data to load again)")
            rep.notes.append("data load skipped: every planned table was already present")
        kit_step("deploy the semantic model and the ontology", "deploy", "--execute", "--replace", "--tier", s.tier, "--out", out)
        step("build the graph", lambda: io.graph(s, out))
        kit_step("verify the ontology and the tables", "verify", "--tier", s.tier, "--out", out)
    except StepFailed as ex:
        say(f"\nFAILED at '{ex.step}': {ex}")
        rep.notes.append(f"failed at '{ex.step}'")
        rc = EXIT_FAILED
    finally:
        if keep_running:
            say("\n--keep-running: the capacity stays on and bills until it is paused (the guard does it at its next run)")
            rep.notes.append("the capacity was left running on purpose (--keep-running)")
        else:
            say("\n== pause the capacity")
            t0 = clock()
            code = pause(s, io, say)
            rep.steps.append(("pause the capacity", "ok" if code == EXIT_OK else "FAILED", clock() - t0))
            rc = max(rc, code)
            if resume_started is not None:
                rep.active_seconds = clock() - resume_started
    return rc, rep


def parse_hold(value: str) -> Optional[dt.datetime]:
    if not value.strip():
        return None
    try:
        when = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as ex:
        raise CdError(f"CD_GUARD_HOLD_UNTIL '{value}' is not a time like 2026-11-05T20:00:00Z") from ex
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def guard(s: Settings, io: Any, hold_until: Optional[dt.datetime], now: dt.datetime, say: Callable[[str], None] = print) -> int:
    st = io.capacity(s, "status")
    if not st.get("exists", True):
        say(f"::error::capacity {s.capacity} was not found in resource group {s.resource_group}")
        return EXIT_CONFIG
    say(f"capacity {s.capacity}: {st.get('state')}")
    if st.get("state") in NOT_BILLING:
        say("nothing to do")
        return EXIT_OK
    if hold_until is not None and now < hold_until:
        say(f"the capacity is {st.get('state')} and stays on: CD_GUARD_HOLD_UNTIL is {hold_until.isoformat()}. Remove the variable when the demo is over.")
        return EXIT_OK
    say(f"the capacity is {st.get('state')} with no deploy running: pausing it")
    return pause(s, io, say)


def capacity_command(s: Settings, io: Any, action: str, hold: str, now: dt.datetime, say: Callable[[str], None] = print) -> int:
    if action == "status":
        say(json.dumps(io.capacity(s, "status"), indent=1))
        return EXIT_OK
    if action == "resume":
        st = io.capacity(s, "resume")
        say(f"capacity {s.capacity}: {st.get('state')}. It bills about USD {s.usd_per_hour:.2f} an hour until it is paused (the guard pauses it, or run action pause).")
        return EXIT_OK if st.get("state") == "Active" else EXIT_FAILED
    if action == "pause":
        return pause(s, io, say)
    return guard(s, io, parse_hold(hold), now, say)


def write_summary(path: Optional[str], text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(text)


def main(argv: Optional[list[str]] = None, environ: Optional[Mapping[str, str]] = None, io: Any = None) -> int:
    environ = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--env", choices=sorted(WORKSPACE_VARS), required=True)
    p = sub.add_parser("deploy")
    p.add_argument("--env", choices=sorted(WORKSPACE_VARS), required=True)
    p.add_argument("--reload-data", action="store_true")
    p.add_argument("--keep-running", action="store_true")
    p.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "cd-out"))
    p.add_argument("--summary", help="append a markdown summary to this file (GITHUB_STEP_SUMMARY)")
    p = sub.add_parser("capacity")
    p.add_argument("action", choices=["status", "resume", "pause", "guard"])
    p.add_argument("--summary")
    a = ap.parse_args(argv)
    try:
        s = load_settings(environ, getattr(a, "env", None), export_fabric.read_tier())
        if a.cmd == "plan":
            print(describe_plan(s))
            return EXIT_OK
        io = io or Live()
        if a.cmd == "deploy":
            out = Path(a.out)
            if out.exists() and any(out.iterdir()):
                raise CdError(f"{out} is not empty; use a fresh --out folder")
            rc, rep = deploy(s, io, out, a.reload_data, a.keep_running)
            write_summary(a.summary, render_summary(s, rep, rc))
            backup = out / "backup"
            if backup.exists():
                print(f"a backup of the replaced definitions is in {backup}")
            return rc
        rc = capacity_command(s, io, a.action, environ.get("CD_GUARD_HOLD_UNTIL", ""), dt.datetime.now(dt.timezone.utc))
        write_summary(a.summary, f"### Capacity {a.action}: " + ("done" if rc == EXIT_OK else "needs attention") + f"\n\n`{s.capacity}`\n")
        return rc
    except CdError as ex:
        print(f"::error::{ex}")
        return EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())
