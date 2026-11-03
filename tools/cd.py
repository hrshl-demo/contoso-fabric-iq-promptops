"""Deploy to Fabric from a pipeline or a laptop: resume the capacity, deploy, verify, pause it again.

DRAFT. It drives the kit's documented commands and is tested with fakes; it has not run against a live tenant yet. The one-time setup
it needs and the first live run are in docs/SETUP.md. Whatever bills or changes Fabric happens in this file and in the kit; the
workflows only sign in and call it.

  python -m tools.cd plan     --env dev            show the settings and the steps; calls nothing
  python -m tools.cd deploy   --env dev            build, resume the F4, load if needed, deploy, graph, verify, pause the F4
  python -m tools.cd capacity status|resume|pause|guard

Settings come from environment variables (GitHub Actions variables): CD_RESOURCE_GROUP, CD_CAPACITY, CD_WORKSPACE_DEV, CD_WORKSPACE_PROD and
optionally CD_LOCATION (default eastus2), CD_TIER (default: the kit's tier), CD_USD_PER_HOUR (default 0.72, the East US 2 list price of an F4)
and CD_DEADLINE_MIN (default 45, 10 to 45).
The capacity is paused at the end of every deploy once it has been resumed, also when a step failed. --keep-running leaves it on only when every
step succeeded; after a failure it is paused anyway. Every step between sign-in and verify is bounded by the deadline (CD_DEADLINE_MIN): a kit command
is stopped, anything else that is still running is abandoned, and a step is not started with less than a minute left. The pause that follows is bounded
too (about 25 minutes at the very most), so the whole run fits the workflows' 80 minute timeout: about 6 minutes of set-up, the deadline (at most 45),
the pause, and a margin.
`guard` pauses a running capacity unless CD_GUARD_HOLD_UNTIL (an ISO time such as 2026-11-05T20:00:00Z, set before a demo) is still in the future.
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
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from . import export_fabric
from .promptlib import ROOT

EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_STILL_RUNNING = 0, 1, 2, 4
DEFAULT_LOCATION = "eastus2"
DEFAULT_USD_PER_HOUR = 0.72
DEFAULT_DEADLINE_MIN = 45
MAX_DEADLINE_MIN = 45                                      # about 6 minutes of set-up + this + the pause (about 25) + a margin must fit the 80 minute job timeout
STATUS_TIMEOUT_S = 120
PAUSE_TIMEOUT_S = 22 * 60                                  # one suspend call; the kit itself waits up to 20 minutes for the state to change
PAUSE_TOTAL_S = PAUSE_TIMEOUT_S + STATUS_TIMEOUT_S + 30    # the suspend call, the status call after it, and a little slack
CAPACITY_TIMEOUT_S = {"status": STATUS_TIMEOUT_S, "resume": 25 * 60, "pause": PAUSE_TIMEOUT_S}
MIN_STEP_S = 60                                            # a step is not started with less than this left before the deadline
GRACE_S = 15                                               # a step's own time limit (a kit command is killed) is given this head start over the outer one
TIMEOUT_EXIT = 124
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()\-]{0,127}$")
CAPACITY_RE = re.compile(r"^[a-z][a-z0-9]{2,62}$")
LOCATION_RE = re.compile(r"^[a-z][a-z0-9]*$")
WORKSPACE_VARS = {"dev": "CD_WORKSPACE_DEV", "prod": "CD_WORKSPACE_PROD"}
NOT_BILLING = {"Paused", "Suspended", "Deleting", "Deleted", "Failed"}                # any other state (Active, Resuming, Scaling, unknown) may be billing
TOKEN_AUDIENCES = ("https://management.azure.com", "https://api.fabric.microsoft.com", "https://analysis.windows.net/powerbi/api", "https://storage.azure.com")


class CdError(RuntimeError):
    """A setting is missing or a step cannot run; the message says what to change."""


class Abandoned(TimeoutError):
    """A call was still running when its time was up, and was left behind."""


def run_bounded(fn: Callable[[], Any], seconds: float) -> Any:
    """Run fn() and wait at most `seconds` for it. A call that is still running then is abandoned (its thread ends with the process), so that one stuck
    network call can never hold up the pause that follows it. Whatever fn raises, an interrupt included, is raised here."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as ex:  # noqa: BLE001 - handed to the caller's thread below
            box["error"] = ex
    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(max(0.0, seconds))
    if worker.is_alive():
        raise Abandoned(f"still running after {int(seconds)} s and left behind")
    if "error" in box:
        raise box["error"]
    return box.get("value")


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
    deadline_min: int = DEFAULT_DEADLINE_MIN

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
    try:
        deadline = int(get("CD_DEADLINE_MIN", str(DEFAULT_DEADLINE_MIN)))
        if not 10 <= deadline <= MAX_DEADLINE_MIN:
            raise ValueError
    except ValueError:
        bad.append(f"CD_DEADLINE_MIN (10 to {MAX_DEADLINE_MIN} minutes)")
        deadline = DEFAULT_DEADLINE_MIN
    if bad:
        raise CdError("invalid values for: " + ", ".join(bad) + " (see docs/SETUP.md)")
    return Settings(s.resource_group, s.capacity, s.location, s.workspace, s.tier, s.env_name, usd, deadline)


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
        "  4. find the workspace and the lakehouse (the lakehouse is created when missing); the workspace must be on this capacity",
        "  5. load the data when planned tables are missing, or with --reload-data",
        "  6. deploy the semantic model and the ontology (an existing item is replaced after a backup)",
        "  7. build the graph",
        "  8. verify the ontology and the tables",
        "  9. pause the capacity, also when a step failed",
        f"Deadline:    steps 1 to 8 are bounded to {s.deadline_min} minutes so that step 9 always runs (it is bounded to about 25 minutes)",
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

    def _kit(self, s: Settings, *args: Any, capture: bool = False, timeout: Optional[float] = None) -> Any:
        cmd = [sys.executable, "-m", "contoso_onto.cli", *[str(a) for a in args]]
        try:
            return self.runner(cmd, cwd=self.root / "kit", env=self._env(s), text=True, encoding="utf-8", capture_output=capture, timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, TIMEOUT_EXIT, "", f"timed out after {int(timeout or 0)} s and was stopped")

    def warm_tokens(self, s: Settings) -> None:
        """Sign-in check, and a token for every audience the kit uses, fetched now. A federated (OIDC) sign-in cannot mint tokens for new audiences once
        its assertion has expired, so a step that first asks for one 15 minutes into the job could fail; tokens fetched here stay in the az cache."""
        az = self.az or shutil.which("az") or shutil.which("az.cmd")
        if not az:
            raise CdError("the az command line is not installed")
        for resource in TOKEN_AUDIENCES:
            try:
                r = self.runner([az, "account", "get-access-token", "--resource", resource, "--query", "expiresOn", "-o", "tsv", "--only-show-errors"],
                                text=True, encoding="utf-8", capture_output=True, timeout=90)
            except subprocess.TimeoutExpired as ex:
                raise CdError(f"az did not answer within 90 s when asked for a token for {resource}") from ex
            if r.returncode != 0:
                raise CdError(f"the signed-in identity cannot get a token for {resource}: {(r.stderr or r.stdout).strip()[:300]}. "
                              f"Sign in first (azure/login in a workflow, az login on a laptop) and check docs/SETUP.md.")

    def kit(self, s: Settings, *args: Any, timeout: Optional[float] = None) -> int:
        return self._kit(s, *args, timeout=timeout).returncode

    def capacity(self, s: Settings, action: str, timeout: Optional[float] = None) -> dict:
        verb = {"status": "status", "resume": "resume", "pause": "suspend"}[action]
        limit = timeout if timeout is not None else CAPACITY_TIMEOUT_S[action]
        r = self._kit(s, "capacity", verb, *([] if action == "status" else ["--execute"]), capture=True, timeout=limit)
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
        try:
            cap = ops.find_capacity(s.capacity)
        except Exception as ex:  # noqa: BLE001 - one clear message, whatever the API said
            raise CdError(f"the identity cannot see capacity '{s.capacity}' through the Fabric API ({str(ex)[:200]}). Add the app's security group as a "
                          f"contributor of the capacity (docs/SETUP.md step 3)") from ex
        if ws["capacityId"] != cap.get("id"):
            raise CdError(f"workspace '{s.workspace}' is not on capacity '{s.capacity}' (it is on {ws['capacityId']}). This pipeline resumes and pauses "
                          f"'{s.capacity}' only, so it would leave the capacity that really bills running. Assign the workspace to '{s.capacity}', or fix CD_CAPACITY")
        lh = ops.ensure_lakehouse(ws["id"], cfg["lakehouse"]["name"])
        keep = {k: lh[k] for k in ("lakehouseId", "lakehouseName", "sqlEndpoint", "sqlDatabase")}
        cli.write_state(out, workspaceId=ws["id"], workspaceName=s.workspace, capacityName=s.capacity, resourceGroup=s.resource_group, **keep)
        return {"workspaceId": ws["id"], **keep}

    def missing_tables(self, out: Path) -> list[str]:
        cli, layers, *_ = self._kit_modules()
        st = cli.read_state(out)
        rep = cli.make_ops().layer_report(layers.load_plan(out), st["workspaceId"], st["lakehouseId"])
        return [t for layer in rep["layers"].values() for t in layer["missing"]]

    def graph(self, s: Settings, out: Path, budget: Optional[float] = None) -> dict:
        """The graph step runs in this process and cannot be killed, so its two waits are cut to fit the time that is left before the deadline."""
        cli, _layers, runner, spec, load_model = self._kit_modules()
        st = cli.read_state(out)
        sp = spec.make_spec(load_model(cli.ROOT / "model"), s.tier, spec.DEFAULT_DESCRIPTIONS)
        ws = {"workspaceId": st["workspaceId"], "workspaceName": st["workspaceName"]}
        lh = {"lakehouseId": st["lakehouseId"], "sqlEndpoint": st.get("sqlEndpoint"), "sqlDatabase": st.get("sqlDatabase")}
        wait_s = 300.0 if budget is None else max(10.0, min(300.0, budget * 0.25))
        timeout_s = 900.0 if budget is None else max(30.0, min(900.0, budget * 0.7))
        return runner.materialize_graph(cli.make_ops(), sp, ws, lh, out, print, wait_s=wait_s, timeout_s=timeout_s)


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
        st = run_bounded(lambda: io.capacity(s, "pause"), PAUSE_TOTAL_S)      # bounded too: a stuck call must not keep the job alive until its timeout
    except Exception as ex:  # noqa: BLE001 - whatever went wrong, the person must know the capacity may still be running
        say(f"::error::THE CAPACITY MAY STILL BE RUNNING ({str(ex)[:300]}). Pause it now: Azure portal, capacity {s.capacity}, Pause; or run the capacity workflow with action pause.")
        return EXIT_STILL_RUNNING
    if st.get("state") != "Paused":
        say(f"::error::THE CAPACITY IS NOT PAUSED (state: {st.get('state')}). Pause it now: Azure portal, capacity {s.capacity}, Pause.")
        return EXIT_STILL_RUNNING
    say(f"capacity {s.capacity} is Paused")
    return EXIT_OK


def deploy(s: Settings, io: Any, out: Path, reload_data: bool = False, keep_running: bool = False, say: Callable[[str], None] = print,
           clock: Callable[[], float] = time.monotonic, deadline_s: Optional[float] = None) -> tuple[int, Report]:
    rep = Report()
    rc = EXIT_FAILED                                       # becomes EXIT_OK only when every step has run; anything else (an interrupt too) must pause
    resume_started: Optional[float] = None
    started = clock()
    limit = s.deadline_min * 60.0 if deadline_s is None else deadline_s

    def left() -> float:
        return limit - (clock() - started)

    def step(name: str, fn: Callable[..., Any], budgeted: bool = False) -> Any:
        say(f"\n== {name}")
        budget = left()
        t0 = clock()
        if budget < MIN_STEP_S:
            rep.steps.append((name, "not started", 0.0))
            raise StepFailed(name, f"not started: {max(0, int(budget))} s are left of the {int(limit // 60)}-minute deadline (CD_DEADLINE_MIN)")
        try:
            # Whatever the step is, it gets at most the time that is left: a step that is stuck is abandoned, so the pause below always runs.
            res = run_bounded((lambda: fn(budget)) if budgeted else fn, budget + GRACE_S)
        except StepFailed:
            rep.steps.append((name, "failed", clock() - t0))
            raise
        except Abandoned as ex:
            rep.steps.append((name, "abandoned", clock() - t0))
            raise StepFailed(name, f"{ex}: the {int(limit // 60)}-minute deadline (CD_DEADLINE_MIN) ended while it was running; the capacity is paused next") from ex
        except Exception as ex:  # noqa: BLE001 - reported with the step that failed
            rep.steps.append((name, "failed", clock() - t0))
            raise StepFailed(name, str(ex)[:600]) from ex
        rep.steps.append((name, "ok", clock() - t0))
        return res

    def kit_step(name: str, *args: Any) -> None:
        def run(budget: float) -> None:
            code = io.kit(s, *args, timeout=budget)
            if code == TIMEOUT_EXIT:
                raise StepFailed(name, f"stopped at the {int(limit // 60)}-minute deadline (CD_DEADLINE_MIN); the capacity is paused next. Run again, the kit resumes")
            if code != 0:
                raise StepFailed(name, f"the kit exited with {code}; its output is above")
        step(name, run, budgeted=True)

    try:
        step("check the sign-in and fetch the tokens", lambda: io.warm_tokens(s))
        step("build the layers and the definitions offline", lambda: io.build_offline(s, out))

        def resume(budget: float) -> Any:
            nonlocal resume_started
            resume_started = clock()                       # from here on the capacity may bill, so a pause is owed whatever happens
            return io.capacity(s, "resume", timeout=min(budget, CAPACITY_TIMEOUT_S["resume"]))
        step("resume the capacity", resume, budgeted=True)
        step("find the workspace and the lakehouse", lambda: io.bootstrap(s, out))
        missing = step("check which planned tables the lakehouse already has", lambda: io.missing_tables(out))
        if reload_data or missing:
            say(f"data: {'reload requested' if reload_data else f'{len(missing)} planned tables are missing'}")
            kit_step("load the data", "load", "--execute", "--out", out)
        else:
            say("data: every planned table is already in the lakehouse; load skipped (use --reload-data to load again)")
            rep.notes.append("data load skipped: every planned table was already present")
        kit_step("deploy the semantic model and the ontology", "deploy", "--execute", "--replace", "--tier", s.tier, "--out", out)
        step("build the graph", lambda b: io.graph(s, out, budget=b), budgeted=True)
        kit_step("verify the ontology and the tables", "verify", "--tier", s.tier, "--out", out)
        rc = EXIT_OK
    except StepFailed as ex:
        say(f"\nFAILED at '{ex.step}': {ex}")
        rep.notes.append(f"failed at '{ex.step}'")
    finally:
        if resume_started is None:
            say("\nthe capacity was never resumed by this run, so it is left as it is")
        elif keep_running and rc == EXIT_OK:
            say("\n--keep-running: the capacity stays on and bills until it is paused (the guard does it at its next run)")
            rep.notes.append("the capacity was left running on purpose (--keep-running)")
        else:
            if keep_running:
                say("\n--keep-running is ignored after a failure: the capacity is paused so that it cannot bill unseen")
                rep.notes.append("--keep-running was ignored because a step failed; the capacity was paused")
            say("\n== pause the capacity")
            t0 = clock()
            code = pause(s, io, say)
            rep.steps.append(("pause the capacity", "ok" if code == EXIT_OK else "FAILED", clock() - t0))
            rc = max(rc, code)
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
        # Before the platform has been built there is nothing to guard, and a red run every 3 hours would only teach people to ignore it. The warning stays
        # visible on the run, so a mistyped CD_CAPACITY is still noticed.
        say(f"::warning::capacity {s.capacity} was not found in resource group {s.resource_group}: nothing to guard. If the platform has been built, check CD_CAPACITY.")
        return EXIT_OK
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
