import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import cd

ENV = {"CD_RESOURCE_GROUP": "rg-demo", "CD_CAPACITY": "democapf4", "CD_WORKSPACE_DEV": "Demo-Dev", "CD_WORKSPACE_PROD": "Demo-Prod"}
NOW = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.timezone.utc)


def settings(env_name="dev", **over):
    return cd.load_settings({**ENV, **over}, env_name, "core")


class FakeIo:
    def __init__(self, missing=(), fail=None, state="Paused", pause_state="Paused", pause_error=None, resume_error=None, exists=True, warm_error=None):
        self.missing, self.fail, self.state, self.pause_state = list(missing), fail, state, pause_state
        self.pause_error, self.resume_error, self.exists, self.warm_error = pause_error, resume_error, exists, warm_error
        self.calls: list[str] = []

    def warm_tokens(self, s):
        self.calls.append("warm")
        if self.warm_error:
            raise cd.CdError(self.warm_error)

    def build_offline(self, s, out):
        self.calls.append("build")

    def capacity(self, s, action):
        self.calls.append(f"capacity {action}")
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

    def kit(self, s, *args):
        self.calls.append(f"kit {args[0]}")
        return 1 if args[0] == self.fail else 0

    def graph(self, s, out):
        self.calls.append("graph")
        if self.fail == "graph":
            raise cd.CdError("graph refused")


def clock():
    t = [0.0]

    def tick():
        t[0] += 60.0
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
    assert rc == cd.EXIT_FAILED and "build" not in io.calls and "capacity resume" not in io.calls
    assert rep.active_seconds is None and rep.steps[0][1] == "failed"


def test_keep_running_leaves_the_capacity_on():
    io = FakeIo()
    rc, rep = cd.deploy(settings(), io, Path("out"), keep_running=True, clock=clock())
    assert rc == 0 and "capacity pause" not in io.calls and rep.active_seconds is None
    assert any("left running on purpose" in n for n in rep.notes)


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
    assert cd.guard(s, FakeIo(exists=False), None, NOW) == cd.EXIT_CONFIG


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


def test_live_kit_step_uses_the_execute_flag_only_when_asked(tmp_path):
    calls = []
    live = cd.Live(state_dir=tmp_path, runner=fake_runner(calls, {}))
    assert live.kit(settings(), "deploy", "--execute", "--replace", "--tier", "core", "--out", tmp_path / "o") == 0
    assert calls[0][0][3:] == ["deploy", "--execute", "--replace", "--tier", "core", "--out", str(tmp_path / "o")]
    assert calls[0][1]["capture_output"] is False


def test_live_warm_tokens_asks_for_every_audience_and_never_prints_a_token(tmp_path):
    calls = []
    live = cd.Live(state_dir=tmp_path, runner=fake_runner(calls, {}), az="az")
    live.warm_tokens(settings())
    asked = [c[0][c[0].index("--resource") + 1] for c in calls]
    assert asked == list(cd.TOKEN_AUDIENCES) and "https://api.fabric.microsoft.com" in asked and "https://storage.azure.com" in asked
    assert all(c[0][c[0].index("--query") + 1] == "expiresOn" for c in calls)


def test_live_warm_tokens_names_the_audience_that_failed(tmp_path):
    class Runner:
        def __call__(self, cmd, **kw):
            bad = "analysis.windows.net" in " ".join(cmd)
            return SimpleNamespace(returncode=1 if bad else 0, stdout="", stderr="AADSTS70011 not allowed" if bad else "")
    with pytest.raises(cd.CdError, match="analysis.windows.net.*AADSTS70011.*Sign in first"):
        cd.Live(state_dir=tmp_path, runner=Runner(), az="az").warm_tokens(settings())
