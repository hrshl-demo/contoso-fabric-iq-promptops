import datetime as dt
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tools import cd
from tools.promptlib import ROOT

ENV = {"CD_RESOURCE_GROUP": "rg-demo", "CD_CAPACITY": "democapf4", "CD_WORKSPACE_DEV": "Demo-Dev", "CD_WORKSPACE_PROD": "Demo-Prod"}
NOW = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.timezone.utc)


def settings(env_name="dev", **over):
    return cd.load_settings({**ENV, **over}, env_name, "core")


class FakeIo:
    def __init__(self, missing=(), fail=None, state="Paused", pause_state="Paused", pause_error=None, resume_error=None, exists=True, warm_error=None,
                 kit_codes=None, interrupt_on=None):
        self.missing, self.fail, self.state, self.pause_state = list(missing), fail, state, pause_state
        self.pause_error, self.resume_error, self.exists, self.warm_error = pause_error, resume_error, exists, warm_error
        self.kit_codes, self.interrupt_on = kit_codes or {}, interrupt_on
        self.calls: list[str] = []
        self.timeouts: dict[str, float] = {}

    def warm_tokens(self, s):
        self.calls.append("warm")
        if self.warm_error:
            raise cd.CdError(self.warm_error)

    def build_offline(self, s, out):
        self.calls.append("build")

    def capacity(self, s, action, timeout=None):
        self.calls.append(f"capacity {action}")
        self.timeouts[f"capacity {action}"] = timeout
        if action == "resume":
            if self.resume_error:
                raise cd.CdError(self.resume_error)
            self.state = "Active"
        if action == "pause":
            if self.pause_error:
                raise cd.CdError(self.pause_error)
            self.state = self.pause_state
        return {"exists": self.exists, "state": self.state}

    def bootstrap(self, s, out):
        self.calls.append("bootstrap")
        return {}

    def missing_tables(self, out):
        self.calls.append("missing")
        return self.missing

    def kit(self, s, *args, timeout=None):
        self.calls.append(f"kit {args[0]}")
        self.timeouts[f"kit {args[0]}"] = timeout
        if self.interrupt_on == args[0]:
            raise KeyboardInterrupt
        return self.kit_codes.get(args[0], 1 if args[0] == self.fail else 0)

    def graph(self, s, out, budget=None):
        self.calls.append("graph")
        self.timeouts["graph"] = budget
        if self.fail == "graph":
            raise cd.CdError("graph refused")


def clock(step_s=1.0):
    t = [0.0]

    def tick():
        t[0] += step_s
        return t[0]
    return tick


# ---- settings -------------------------------------------------------------------------------------------------

def test_settings_defaults_and_environments():
    s = settings()
    assert (s.workspace, s.location, s.tier, s.usd_per_hour) == ("Demo-Dev", "eastus2", "core", 0.72)
    assert settings("prod").workspace == "Demo-Prod"
    assert settings(None).workspace == ""
    assert settings(CD_LOCATION="australiaeast", CD_TIER="full", CD_USD_PER_HOUR="0.84").usd_per_hour == 0.84


def test_settings_say_what_is_missing():
    with pytest.raises(cd.CdError, match="CD_CAPACITY, CD_WORKSPACE_DEV"):
        cd.load_settings({"CD_RESOURCE_GROUP": "rg"}, "dev", "core")
    with pytest.raises(cd.CdError, match="CD_WORKSPACE_PROD"):
        cd.load_settings({k: v for k, v in ENV.items() if k != "CD_WORKSPACE_PROD"}, "prod", "core")
    cd.load_settings({"CD_RESOURCE_GROUP": "rg", "CD_CAPACITY": "democapf4"}, None, "core")


@pytest.mark.parametrize("over, name", [({"CD_CAPACITY": "Bad_Name"}, "CD_CAPACITY"), ({"CD_RESOURCE_GROUP": "rg;rm -rf"}, "CD_RESOURCE_GROUP"),
                                        ({"CD_LOCATION": "East US"}, "CD_LOCATION"), ({"CD_TIER": "huge"}, "CD_TIER"),
                                        ({"CD_USD_PER_HOUR": "free"}, "CD_USD_PER_HOUR"), ({"CD_WORKSPACE_DEV": "$(whoami)"}, "CD_WORKSPACE_DEV")])
def test_settings_reject_odd_values(over, name):
    with pytest.raises(cd.CdError, match=name):
        settings(**over)


def test_kit_environment_has_no_secrets_and_pins_f4(tmp_path):
    env = settings().kit_env(tmp_path)
    assert env["CONTOSO_CAPACITY_SKU"] == "F4" and env["CONTOSO_WORKSPACE_NAME"] == "Demo-Dev" and env["CONTOSO_STATE_DIR"] == str(tmp_path)
    assert not any(k for k in env if any(w in k for w in ("TOKEN", "SECRET", "KEY", "PASSWORD")))
    assert "CONTOSO_WORKSPACE_NAME" not in settings(None).kit_env(tmp_path)


def test_plan_lists_every_step_and_the_cost():
    text = cd.describe_plan(settings())
    assert "USD 0.72 an hour" in text and text.count("\n  ") >= 8
    assert "pause the capacity, also when a step failed" in text


# ---- deploy ---------------------------------------------------------------------------------------------------

def test_a_clean_deploy_skips_the_load_and_pauses():
    io = FakeIo()
    rc, rep = cd.deploy(settings(), io, Path("out"), clock=clock())
    assert rc == 0
    assert io.calls == ["warm", "build", "capacity resume", "bootstrap", "missing", "kit deploy", "graph", "kit verify", "capacity pause"]
    assert [r[1] for r in rep.steps] == ["ok"] * 9
    assert any("load skipped" in n for n in rep.notes)
    assert rep.active_seconds and rep.active_seconds > 0


def test_missing_tables_or_a_reload_request_load_the_data():
    io = FakeIo(missing=["dim_customer"])
    assert cd.deploy(settings(), io, Path("out"), clock=clock())[0] == 0
    assert "kit load" in io.calls and io.calls.index("kit load") < io.calls.index("kit deploy")
    io = FakeIo()
    cd.deploy(settings(), io, Path("out"), reload_data=True, clock=clock())
    assert "kit load" in io.calls


@pytest.mark.parametrize("failing, skipped", [("deploy", ["graph", "kit verify"]), ("graph", ["kit verify"]), ("verify", [])])
def test_a_failed_step_stops_the_rest_but_still_pauses(failing, skipped):
    io = FakeIo(fail=failing)
    rc, rep = cd.deploy(settings(), io, Path("out"), clock=clock())
    assert rc == cd.EXIT_FAILED
    assert io.calls[-1] == "capacity pause"
    assert all(s not in io.calls for s in skipped)
    assert any(r[1] == "failed" for r in rep.steps) and any("failed at" in n for n in rep.notes)


def test_a_failed_resume_still_tries_to_pause():
    io = FakeIo(resume_error="not allowed")
    rc, _ = cd.deploy(settings(), io, Path("out"), clock=clock())
    assert rc == cd.EXIT_FAILED and io.calls == ["warm", "build", "capacity resume", "capacity pause"]


def test_no_sign_in_stops_before_anything_is_built_or_resumed():
    io = FakeIo(warm_error="not signed in")
    rc, rep = cd.deploy(settings(), io, Path("out"), clock=clock())
    assert rc == cd.EXIT_FAILED and io.calls == ["warm"]                  # nothing was resumed, so nothing is paused (and no false alarm)
    assert rep.active_seconds is None and rep.steps[0][1] == "failed"


def test_a_run_that_never_resumed_does_not_touch_a_capacity_somebody_else_started():
    io = FakeIo(state="Active")
    rc, _ = cd.deploy(settings(), io, Path("out"), clock=clock())
    assert rc == 0 and io.state == "Paused"                                   # resumed (already Active), then paused: the documented behaviour
    io = FakeIo(state="Active")
    io.build_offline = lambda s, out: (_ for _ in ()).throw(cd.CdError("build broke"))
    rc, _ = cd.deploy(settings(), io, Path("out"), clock=clock())
    assert rc == cd.EXIT_FAILED and "capacity pause" not in io.calls and io.state == "Active"


def test_keep_running_leaves_the_capacity_on_after_a_success():
    io = FakeIo()
    rc, rep = cd.deploy(settings(), io, Path("out"), keep_running=True, clock=clock())
    assert rc == 0 and "capacity pause" not in io.calls and rep.active_seconds is None
    assert any("left running on purpose" in n for n in rep.notes)


@pytest.mark.parametrize("failing", ["load", "deploy", "graph", "verify"])
def test_keep_running_is_ignored_after_a_failure(failing):
    io = FakeIo(missing=["dim_customer"], fail=failing)
    rc, rep = cd.deploy(settings(), io, Path("out"), keep_running=True, clock=clock())
    assert rc == cd.EXIT_FAILED and io.calls[-1] == "capacity pause" and io.state == "Paused"
    assert any("--keep-running was ignored" in n for n in rep.notes)


@pytest.mark.parametrize("keep", [False, True])
def test_an_interrupt_still_pauses_even_with_keep_running(keep):
    io = FakeIo(interrupt_on="deploy")
    with pytest.raises(KeyboardInterrupt):
        cd.deploy(settings(), io, Path("out"), keep_running=keep, clock=clock())
    assert io.calls[-1] == "capacity pause" and io.state == "Paused"


# ---- the deadline ---------------------------------------------------------------------------------------------

def test_every_long_step_gets_the_time_that_is_left():
    io = FakeIo(missing=["dim_customer"])
    rc, _ = cd.deploy(settings(), io, Path("out"), clock=clock(step_s=10.0), deadline_s=3000.0)
    assert rc == 0
    for key in ("kit load", "kit deploy", "kit verify", "graph"):
        assert 0 < io.timeouts[key] <= 3000, key
    assert io.timeouts["kit load"] > io.timeouts["kit deploy"] > io.timeouts["kit verify"]          # the budget shrinks as time passes
    assert io.timeouts["capacity resume"] <= cd.CAPACITY_TIMEOUT_S["resume"]
    assert io.timeouts["capacity pause"] is None                                                     # the pause is never limited by the deadline


def test_steps_are_not_started_after_the_deadline_but_the_capacity_is_still_paused():
    io = FakeIo(missing=["dim_customer"])
    rc, rep = cd.deploy(settings(), io, Path("out"), clock=clock(step_s=100.0), deadline_s=1500.0)
    assert rc == cd.EXIT_FAILED and io.calls[-1] == "capacity pause" and io.state == "Paused"
    assert "kit load" not in io.calls and "kit verify" not in io.calls
    assert any(r[1] == "not started" for r in rep.steps) and any("failed at" in n for n in rep.notes)


def test_a_kit_step_that_hits_the_deadline_is_reported_as_such(capsys):
    io = FakeIo(kit_codes={"deploy": cd.TIMEOUT_EXIT})
    rc, rep = cd.deploy(settings(), io, Path("out"), clock=clock(), deadline_s=3000.0)
    assert rc == cd.EXIT_FAILED and io.calls[-1] == "capacity pause"
    assert "stopped at the 50-minute deadline" in capsys.readouterr().out


def test_the_deadline_comes_from_the_settings():
    assert settings().deadline_min == 45 and settings(CD_DEADLINE_MIN="30").deadline_min == 30
    io = FakeIo()
    rc, rep = cd.deploy(settings(CD_DEADLINE_MIN="10"), io, Path("out"), clock=clock(step_s=100.0))
    assert rc == cd.EXIT_FAILED and rep.steps[-1][1] == "not started"                               # 10 minutes are used up by the clock
    assert "capacity resume" not in io.calls and "capacity pause" not in io.calls                     # the resume never started, so nothing is paused


@pytest.mark.parametrize("value", ["5", "46", "121", "soon", "-1"])
def test_the_deadline_must_be_a_sensible_number(value):
    with pytest.raises(cd.CdError, match="CD_DEADLINE_MIN"):
        settings(CD_DEADLINE_MIN=value)


def test_run_bounded_returns_reraises_and_abandons():
    assert cd.run_bounded(lambda: 42, 5) == 42
    with pytest.raises(ValueError, match="boom"):
        cd.run_bounded(lambda: (_ for _ in ()).throw(ValueError("boom")), 5)
    with pytest.raises(KeyboardInterrupt):
        cd.run_bounded(lambda: (_ for _ in ()).throw(KeyboardInterrupt()), 5)
    release = threading.Event()
    try:
        with pytest.raises(cd.Abandoned, match="still running after 0 s and left behind"):
            cd.run_bounded(lambda: release.wait(10), 0.05)
    finally:
        release.set()


def short_waits(monkeypatch):
    """Make every bounded call give up after a tenth of a second, so that a hang can be tested in no time."""
    real = cd.run_bounded
    monkeypatch.setattr(cd, "run_bounded", lambda fn, seconds: real(fn, 0.1))


def test_a_step_that_hangs_is_abandoned_and_the_capacity_is_still_paused(monkeypatch):
    short_waits(monkeypatch)
    release = threading.Event()
    io = FakeIo()
    io.bootstrap = lambda s, out: release.wait(10)                                       # a Fabric call that never answers
    try:
        rc, rep = cd.deploy(settings(), io, Path("out"), clock=clock())
    finally:
        release.set()
    assert rc == cd.EXIT_FAILED and io.calls[-1] == "capacity pause" and io.state == "Paused"
    assert any(r[1] == "abandoned" for r in rep.steps) and "kit deploy" not in io.calls and "missing" not in io.calls


def test_a_pause_that_hangs_is_reported_not_waited_for(monkeypatch, capsys):
    short_waits(monkeypatch)
    release = threading.Event()
    io = FakeIo()
    original = io.capacity

    def capacity(s, action, timeout=None):
        if action == "pause":
            release.wait(10)
        return original(s, action, timeout)
    io.capacity = capacity
    try:
        rc, rep = cd.deploy(settings(), io, Path("out"), clock=clock())
    finally:
        release.set()
    assert rc == cd.EXIT_STILL_RUNNING and "MAY STILL BE RUNNING" in capsys.readouterr().out
    assert rep.steps[-1] == ("pause the capacity", "FAILED", rep.steps[-1][2])


@pytest.mark.parametrize("workflow, job", [("cd-dev.yml", "deploy"), ("release.yml", "deploy-prod")])
def test_the_worst_case_first_pause_fits_inside_the_job_timeout(workflow, job):
    """About 6 minutes of set-up, the longest allowed deadline, the grace, and one full pause attempt, with a margin."""
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8"))
    timeout = doc["jobs"][job]["timeout-minutes"]
    worst = 6 + cd.MAX_DEADLINE_MIN + (cd.GRACE_S + cd.PAUSE_TOTAL_S) / 60
    assert worst + 3 <= timeout, f"{worst:.1f} minutes in the worst case, the job allows {timeout}"


def test_the_guard_job_fits_a_status_call_and_a_full_pause():
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "capacity.yml").read_text(encoding="utf-8"))
    worst = 4 + (cd.STATUS_TIMEOUT_S + cd.PAUSE_TOTAL_S) / 60
    assert worst + 3 <= doc["jobs"]["capacity"]["timeout-minutes"]


def test_a_capacity_that_cannot_be_paused_is_loud_even_after_a_good_deploy(capsys):
    rc, rep = cd.deploy(settings(), FakeIo(pause_error="forbidden"), Path("out"), clock=clock())
    assert rc == cd.EXIT_STILL_RUNNING
    assert "MAY STILL BE RUNNING" in capsys.readouterr().out
    assert rep.steps[-1][1] == "FAILED"
    rc, _ = cd.deploy(settings(), FakeIo(pause_state="Active"), Path("out"), clock=clock())
    assert rc == cd.EXIT_STILL_RUNNING


def test_the_summary_shows_steps_minutes_and_cost():
    s = settings()
    rc, rep = cd.deploy(s, FakeIo(), Path("out"), clock=clock())
    text = cd.render_summary(s, rep, rc)
    assert text.startswith("### Deploy to dev: done") and "| deploy the semantic model and the ontology | ok |" in text
    assert "USD" in text and "an estimate, not a bill" in text
    assert cd.render_summary(s, rep, 1).startswith("### Deploy to dev: FAILED")


# ---- guard and capacity ---------------------------------------------------------------------------------------

def test_guard_pauses_an_active_capacity_only():
    s = settings(None)
    io = FakeIo(state="Active")
    assert cd.guard(s, io, None, NOW) == 0 and io.calls == ["capacity status", "capacity pause"]
    io = FakeIo(state="Paused")
    assert cd.guard(s, io, None, NOW) == 0 and io.calls == ["capacity status"]


def test_guard_with_no_capacity_yet_is_a_warning_not_a_failure():
    """Before the platform is built there is nothing to guard; the schedule must not go red every 3 hours."""
    said = []
    io = FakeIo(exists=False)
    assert cd.guard(settings(None), io, None, NOW, said.append) == cd.EXIT_OK and io.calls == ["capacity status"]
    assert said and said[0].startswith("::warning::") and "nothing to guard" in said[0] and "CD_CAPACITY" in said[0]


@pytest.mark.parametrize("state, pauses", [("Active", True), ("Resuming", True), ("Scaling", True), (None, True),
                                           ("Paused", False), ("Suspended", False), ("Failed", False), ("Deleting", False)])
def test_guard_treats_every_state_that_may_bill_as_running(state, pauses):
    io = FakeIo(state=state)
    cd.guard(settings(None), io, None, NOW)
    assert ("capacity pause" in io.calls) is pauses


def test_guard_respects_a_hold_for_a_demo():
    s = settings(None)
    io = FakeIo(state="Active")
    assert cd.guard(s, io, NOW + dt.timedelta(hours=2), NOW) == 0 and io.calls == ["capacity status"]
    io = FakeIo(state="Active")
    cd.guard(s, io, NOW - dt.timedelta(minutes=1), NOW)
    assert io.calls[-1] == "capacity pause"


def test_parse_hold():
    assert cd.parse_hold("") is None
    assert cd.parse_hold("2026-11-05T20:00:00Z") == dt.datetime(2026, 11, 5, 20, 0, tzinfo=dt.timezone.utc)
    assert cd.parse_hold("2026-11-05T20:00:00").tzinfo is not None
    with pytest.raises(cd.CdError):
        cd.parse_hold("tomorrow evening")


def test_capacity_commands():
    s = settings(None)
    assert cd.capacity_command(s, FakeIo(), "resume", "", NOW) == 0
    assert cd.capacity_command(s, FakeIo(state="Active"), "pause", "", NOW) == 0
    assert cd.capacity_command(s, FakeIo(state="Active"), "guard", "2026-11-05T20:00:00Z", NOW) == 0


# ---- the command line -----------------------------------------------------------------------------------------

def test_main_plan_calls_nothing(capsys):
    class Boom:
        def __getattr__(self, name):
            raise AssertionError(f"plan must not call {name}")
    assert cd.main(["plan", "--env", "prod"], environ=ENV, io=Boom()) == 0
    assert "Workspace:   Demo-Prod" in capsys.readouterr().out


def test_main_reports_missing_settings(capsys):
    assert cd.main(["plan", "--env", "dev"], environ={}) == cd.EXIT_CONFIG
    assert "::error::missing repository variables: CD_RESOURCE_GROUP, CD_CAPACITY, CD_WORKSPACE_DEV" in capsys.readouterr().out


def test_main_deploy_writes_the_summary(tmp_path):
    summary = tmp_path / "summary.md"
    rc = cd.main(["deploy", "--env", "dev", "--out", str(tmp_path / "o"), "--summary", str(summary)], environ=ENV, io=FakeIo())
    assert rc == 0 and summary.read_text(encoding="utf-8").startswith("### Deploy to dev: done")


def test_main_refuses_a_dirty_out_folder(tmp_path, capsys):
    (tmp_path / "o").mkdir()
    (tmp_path / "o" / "x").write_text("x")
    assert cd.main(["deploy", "--env", "dev", "--out", str(tmp_path / "o")], environ=ENV, io=FakeIo()) == cd.EXIT_CONFIG
    assert "not empty" in capsys.readouterr().out


def test_main_capacity_guard_uses_the_hold_variable(tmp_path):
    io = FakeIo(state="Active")
    env = {**ENV, "CD_GUARD_HOLD_UNTIL": "2999-01-01T00:00:00Z"}
    assert cd.main(["capacity", "guard"], environ=env, io=io) == 0 and io.calls == ["capacity status"]
    io = FakeIo(state="Active")
    assert cd.main(["capacity", "guard"], environ={**ENV, "CD_GUARD_HOLD_UNTIL": "nonsense"}, io=io) == cd.EXIT_CONFIG


# ---- the real adapter, with a fake process runner --------------------------------------------------------------

def fake_runner(calls, answers):
    def run(cmd, **kw):
        calls.append((cmd, kw))
        verb = " ".join(cmd[3:5])
        rc, out = answers.get(verb, (0, ""))
        return SimpleNamespace(returncode=rc, stdout=out, stderr="boom" if rc else "")
    return run


def test_live_capacity_runs_the_kits_commands(tmp_path):
    calls = []
    status = json.dumps({"exists": True, "state": "Paused"}, indent=1)
    live = cd.Live(state_dir=tmp_path, runner=fake_runner(calls, {"capacity status": (0, status), "capacity suspend": (0, "  az rest ...\nDONE")}))
    assert live.capacity(settings(), "status") == {"exists": True, "state": "Paused"}
    assert live.capacity(settings(), "pause")["state"] == "Paused"
    verbs = [" ".join(c[0][3:]) for c in calls]
    assert verbs == ["capacity status", "capacity suspend --execute", "capacity status"]
    cmd, kw = calls[1]
    assert cmd[1:4] == ["-m", "contoso_onto.cli", "capacity"] and kw["cwd"].name == "kit"
    assert kw["env"]["CONTOSO_CAPACITY_NAME"] == "democapf4" and kw["env"]["CONTOSO_STATE_DIR"] == str(tmp_path)
    assert kw["capture_output"] is True


def test_live_capacity_errors_are_clear(tmp_path):
    live = cd.Live(state_dir=tmp_path, runner=fake_runner([], {"capacity resume": (1, "")}))
    with pytest.raises(cd.CdError, match="capacity resume failed"):
        live.capacity(settings(), "resume")
    live = cd.Live(state_dir=tmp_path, runner=fake_runner([], {"capacity status": (0, "no json here")}))
    with pytest.raises(cd.CdError, match="did not answer with JSON"):
        live.capacity(settings(), "status")


def test_live_capacity_calls_have_their_own_time_limits(tmp_path):
    calls = []
    status = json.dumps({"exists": True, "state": "Paused"})
    live = cd.Live(state_dir=tmp_path, runner=fake_runner(calls, {"capacity status": (0, status)}))
    live.capacity(settings(), "pause")
    live.capacity(settings(), "resume", timeout=90)
    by_verb = [(c[0][4], c[1]["timeout"]) for c in calls]
    assert by_verb == [("suspend", cd.PAUSE_TIMEOUT_S), ("status", cd.CAPACITY_TIMEOUT_S["status"]), ("resume", 90), ("status", cd.CAPACITY_TIMEOUT_S["status"])]


def test_a_stuck_kit_process_is_stopped_and_reported_not_waited_for(tmp_path):
    import subprocess

    class Hangs:
        def __call__(self, cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
    live = cd.Live(state_dir=tmp_path, runner=Hangs())
    assert live.kit(settings(), "deploy", "--execute", timeout=30) == cd.TIMEOUT_EXIT
    with pytest.raises(cd.CdError, match="capacity suspend failed.*timed out after 20 s"):
        live.capacity(settings(), "pause", timeout=20)


def test_live_kit_step_uses_the_execute_flag_only_when_asked(tmp_path):
    calls = []
    live = cd.Live(state_dir=tmp_path, runner=fake_runner(calls, {}))
    assert live.kit(settings(), "deploy", "--execute", "--replace", "--tier", "core", "--out", tmp_path / "o", timeout=120) == 0
    assert calls[0][0][3:] == ["deploy", "--execute", "--replace", "--tier", "core", "--out", str(tmp_path / "o")]
    assert calls[0][1]["capture_output"] is False and calls[0][1]["timeout"] == 120


def test_live_warm_tokens_asks_for_every_audience_and_never_prints_a_token(tmp_path):
    calls = []
    live = cd.Live(state_dir=tmp_path, runner=fake_runner(calls, {}), az="az")
    live.warm_tokens(settings())
    asked = [c[0][c[0].index("--resource") + 1] for c in calls]
    assert asked == list(cd.TOKEN_AUDIENCES) and "https://api.fabric.microsoft.com" in asked and "https://storage.azure.com" in asked
    assert all(c[0][c[0].index("--query") + 1] == "expiresOn" for c in calls)
    assert all(c[1]["timeout"] == 90 for c in calls)                                              # a silent az cannot hold the step up


def test_live_warm_tokens_gives_up_on_a_silent_az(tmp_path):
    import subprocess

    class Silent:
        def __call__(self, cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
    with pytest.raises(cd.CdError, match="did not answer within 90 s.*management.azure.com"):
        cd.Live(state_dir=tmp_path, runner=Silent(), az="az").warm_tokens(settings())


def test_live_warm_tokens_names_the_audience_that_failed(tmp_path):
    class Runner:
        def __call__(self, cmd, **kw):
            bad = "analysis.windows.net" in " ".join(cmd)
            return SimpleNamespace(returncode=1 if bad else 0, stdout="", stderr="AADSTS70011 not allowed" if bad else "")
    with pytest.raises(cd.CdError, match="analysis.windows.net.*AADSTS70011.*Sign in first"):
        cd.Live(state_dir=tmp_path, runner=Runner(), az="az").warm_tokens(settings())
