import datetime as dt
import json
import re
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import cd, platform as pf
from tools.promptlib import ROOT

ADMIN = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
ADMIN2 = "aaaaaaaa-2222-4222-8222-aaaaaaaaaaaa"
UPN = "admin@demo.test"
UPN2 = "colleague@demo.test"
PIPE = "bbbbbbbb-1111-4111-8111-bbbbbbbbbbbb"
CONS = "cccccccc-1111-4111-8111-cccccccccccc"
ENV = {"CD_RESOURCE_GROUP": "rg-demo", "CD_CAPACITY": "democapf4", "CD_WORKSPACE_DEV": "Demo-Dev", "CD_WORKSPACE_PROD": "Demo-Prod",
       "CD_ADMIN_OBJECT_IDS": ADMIN, "CD_ADMIN_UPNS": UPN, "CD_PIPELINE_OBJECT_ID": PIPE}


def settings(env_name="dev", **over):
    return pf.load_platform_settings({**ENV, **over}, env_name, "core")


class FakeIo:
    """Every call the platform makes, recorded; each can be told to fail. The subscription starts blank: the capacity does not exist until it is created."""

    def __init__(self, fail=None, missing=(), gate=("open",), state="Paused", pause_error=None, kit_codes=None, interrupt_on=None, pause_state="Paused",
                 cap_exists=False, cap_sku="F4", cap_admins=None, settle_state="Succeeded", create_then_fail=False, settle_cancels=False, provisioning=(),
                 appears_after=None, gate_warning=""):
        self.fail, self.missing, self.gates, self.state = fail, list(missing), list(gate), state
        self.pause_error, self.kit_codes, self.interrupt_on, self.pause_state = pause_error, kit_codes or {}, interrupt_on, pause_state
        self.cap_exists, self.cap_sku, self.cap_admins, self.settle_state, self.create_then_fail = cap_exists, cap_sku, cap_admins, settle_state, create_then_fail
        self.settle_cancels, self.provisioning, self.appears_after, self.gate_warning = settle_cancels, list(provisioning), appears_after, gate_warning
        self.deployment_cancelled = False
        self.deployed_admins = None
        self.infra_timeouts: list = []
        self.status_reads = 0
        self.calls: list[str] = []
        self.capacity_fabric_id = "cap-guid-1"
        self.gate_checks = 0

    def _maybe_fail(self, name):
        self.calls.append(name)
        if self.interrupt_on == name:
            raise KeyboardInterrupt
        if self.fail == name:
            raise cd.CdError(f"{name} broke")

    def warm_tokens(self, base):
        self._maybe_fail("warm")

    def identity(self, s):
        self._maybe_fail("identity")
        return {"subscriptionId": "sub-1", "user": "app", "type": "servicePrincipal"}

    def infra(self, s, storage, deploy_capacity, admins=None, timeout=None):
        if deploy_capacity:
            self.infra_timeouts.append(timeout)
        if deploy_capacity and self.create_then_fail:                         # ARM accepted it, and the call still raised (a timeout, a cancelled run)
            self.calls.append("infra capacity")
            self.cap_exists, self.state = True, "Active"
            raise cd.CdError("the deployment client timed out")
        self._maybe_fail("infra capacity" if deploy_capacity else "infra storage")
        if deploy_capacity:
            self.cap_exists, self.state = True, "Active"
            self.deployed_admins = list(admins) if admins is not None else None
        return {"storageAccountName": storage, "sourceUrl": f"https://{storage}.dfs.core.windows.net/source"}

    def settle_capacity_deployment(self, s):
        self.calls.append("settle")
        self.deployment_cancelled = self.settle_cancels
        return self.settle_state

    def upload_source(self, s, storage, seed):
        self._maybe_fail("upload")

    def download_source(self, s, storage, dest):
        self._maybe_fail("download")

    def build_from_source(self, s, source, out):
        self._maybe_fail("build layers")

    def capacity(self, base, action, timeout=None):
        self.calls.append(f"capacity {action}")
        if action == "status":
            self.status_reads += 1
            if not self.cap_exists and self.appears_after is not None and self.status_reads > self.appears_after:
                self.cap_exists, self.state = True, "Active"                 # the capacity service went on after the cancel, and the capacity appeared
            if not self.cap_exists:
                return {"exists": False}
            prov = self.provisioning.pop(0) if self.provisioning else "Succeeded"
            return {"exists": True, "state": self.state, "sku": self.cap_sku, "admins": self.cap_admins, "provisioningState": prov}
        if not self.cap_exists:                                              # what the kit says for a capacity that is not there
            raise cd.CdError(f"capacity {action} failed: capacity does not exist in resource group rg-demo")
        if action == "pause":
            if self.pause_error:
                raise cd.CdError(self.pause_error)
            self.state = self.pause_state
        if action == "resume":
            self.state = "Active"
        return {"exists": True, "state": self.state}

    def workspace_and_lakehouse(self, s, out):
        self._maybe_fail("workspace")
        return {"workspaceId": "ws-1", "capacityId": "cap-guid-1", "lakehouseId": "lh-1", "lakehouseName": "ContosoBankLH", "sqlEndpoint": "x.fabric", "sqlDatabase": "sql-1"}

    def missing_tables(self, out):
        self._maybe_fail("missing")
        return self.missing

    def kit(self, base, *args, timeout=None):
        self.calls.append(f"kit {args[0]}")
        if self.interrupt_on == "kit " + args[0]:
            raise KeyboardInterrupt
        return self.kit_codes.get(args[0], 1 if self.fail == "kit " + args[0] else 0)

    def gate_state(self, s, out):
        self.calls.append("gate")
        self.gate_checks += 1
        step = self.gates[min(self.gate_checks - 1, len(self.gates) - 1)]
        return pf.GateState(step, f"fake {step}", warning=self.gate_warning)

    def needs_cross_geo(self, s):
        return False


def clock(step_s=1.0):
    t = [0.0]

    def tick():
        t[0] += step_s
        return t[0]
    return tick


class Time:
    """A clock that only moves when something sleeps, so that waiting costs nothing in a test."""

    def __init__(self):
        self.t, self.slept = 1000.0, []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


@pytest.fixture(autouse=True)
def no_real_temp_dirs(monkeypatch, tmp_path):
    counter = {"n": 0}

    def fake_mkdtemp(prefix="x"):
        counter["n"] += 1
        p = tmp_path / f"{prefix}{counter['n']}"
        p.mkdir()
        return str(p)
    monkeypatch.setattr(pf.tempfile, "mkdtemp", fake_mkdtemp)


def run_build(io, tmp_path, **kw):
    kw.setdefault("clock", clock())
    kw.setdefault("sleeper", lambda s: None)
    return pf.build(settings(), io, tmp_path / "out", **kw)


# ---- settings -------------------------------------------------------------------------------------------------

def test_settings_read_the_people_the_pipeline_and_the_consumers():
    s = settings(CD_ADMIN_OBJECT_IDS=f"{ADMIN}, {ADMIN2};{ADMIN}", CD_ADMIN_UPNS=f"{UPN}, {UPN2};{UPN}", CD_CONSUMER_OBJECT_IDS=CONS)
    assert s.admins == (ADMIN, ADMIN2) and s.admin_upns == (UPN, UPN2) and s.pipeline == PIPE and s.consumers == (CONS,)
    assert s.capacity_admins == (UPN, UPN2, PIPE, CONS)                  # a user by user principal name, a service principal by object id (Fabric's formats)
    assert ADMIN not in s.capacity_admins
    assert s.other_workspaces == ("Demo-Prod",) and settings("prod").other_workspaces == ("Demo-Dev",)
    members = s.workspace_members()
    assert {"id": ADMIN, "type": "User", "role": "Admin"} in members                                    # a workspace role takes the object id
    assert {"id": CONS, "type": "ServicePrincipal", "role": "Contributor"} in members and all(m["id"] != PIPE for m in members)


def test_settings_say_what_is_missing_or_wrong():
    with pytest.raises(cd.CdError, match="CD_ADMIN_OBJECT_IDS is empty"):
        settings(CD_ADMIN_OBJECT_IDS="")
    with pytest.raises(cd.CdError, match="CD_ADMIN_UPNS is empty.*UPN"):
        settings(CD_ADMIN_UPNS="")
    with pytest.raises(cd.CdError, match="CD_ADMIN_UPNS must be a comma separated list of user principal names"):
        settings(CD_ADMIN_UPNS=ADMIN)                                                                    # an object id is not what Fabric takes for a user
    with pytest.raises(cd.CdError, match="CD_PIPELINE_OBJECT_ID must hold exactly one"):
        settings(CD_PIPELINE_OBJECT_ID="")
    with pytest.raises(cd.CdError, match="CD_PIPELINE_OBJECT_ID must hold exactly one"):
        settings(CD_PIPELINE_OBJECT_ID=f"{PIPE},{ADMIN}")
    with pytest.raises(cd.CdError, match="not one"):
        settings(CD_CONSUMER_OBJECT_IDS="not-a-guid")
    with pytest.raises(cd.CdError, match="CD_RESOURCE_GROUP"):
        pf.load_platform_settings({}, "dev", "core")
    assert pf.load_platform_settings({**ENV, "CD_PIPELINE_OBJECT_ID": ""}, "dev", "core", need_pipeline=False).pipeline == ""


def test_the_commands_that_pause_or_destroy_need_no_list_of_people():
    """The last line of defence must not fail because a variable it never uses is missing."""
    base = {k: v for k, v in ENV.items() if k.startswith("CD_RESOURCE") or k in ("CD_CAPACITY", "CD_WORKSPACE_DEV", "CD_WORKSPACE_PROD")}
    s = pf.load_platform_settings(base, "dev", "core", need_people=False)
    assert s.admins == () and s.admin_upns == () and s.pipeline == "" and s.base.capacity == "democapf4"
    with pytest.raises(cd.CdError, match="CD_ADMIN_OBJECT_IDS is empty"):
        pf.load_platform_settings(base, "dev", "core")


def test_storage_account_names_are_valid_stable_and_distinct():
    name = pf.storage_account_name("sub-1", "rg-demo")
    assert re.fullmatch(r"[a-z0-9]{3,24}", name) and len(name) == 24 and name.startswith("stpromptops")
    assert name == pf.storage_account_name("SUB-1", "RG-Demo")                                         # case does not matter
    assert name != pf.storage_account_name("sub-2", "rg-demo") and name != pf.storage_account_name("sub-1", "rg-other")


def bicep_params():
    text = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
    return set(re.findall(r"^param (\w+) ", text, re.M)), text


def test_the_arm_parameters_match_the_bicep_file():
    declared, _text = bicep_params()
    doc = pf.arm_parameters(settings(CD_CONSUMER_OBJECT_IDS=CONS), "stx", True)
    assert set(doc["parameters"]) == declared                                                           # no parameter the template does not know, none left out
    p = {k: v["value"] for k, v in doc["parameters"].items()}
    assert p["capacitySku"] == "F4" and p["deployCapacity"] is True and p["capacityAdmins"] == [UPN, PIPE, CONS]
    assert ADMIN not in json.dumps(doc)                                                                  # the object id of a person never goes in as a capacity admin
    assert pf.arm_parameters(settings(), "stx", False)["parameters"]["deployCapacity"]["value"] is False
    assert pf.arm_parameters(settings(), "stx", True, ["x@y.test", PIPE])["parameters"]["capacityAdmins"]["value"] == ["x@y.test", PIPE]
    assert "secret" not in json.dumps(doc).lower() and "password" not in json.dumps(doc).lower()


def test_the_bicep_keeps_the_storage_closed_and_the_capacity_optional():
    _declared, text = bicep_params()
    for needle in ("isHnsEnabled: true", "minimumTlsVersion: 'TLS1_2'", "allowBlobPublicAccess: false", "allowSharedKeyAccess: false",
                   "publicAccess: 'None'", "if (deployCapacity)", "param capacitySku string = 'F4'", "Microsoft.Fabric/capacities@2023-11-01"):
        assert needle in text, needle
    assert "'F64'" not in text and "'F32'" not in text                                                  # the smallest sizes only: a typo cannot buy a big capacity


def test_the_plan_lists_every_step_and_says_when_billing_starts():
    text = pf.describe_plan(settings())
    assert "bills about USD 0.72 an hour from here until step 13" in text and "13. pause the capacity" in text
    assert text.index("create the storage account") < text.index("create the Fabric capacity")
    assert "never resized and never loses an administrator" in text


# ---- what to do about the capacity ----------------------------------------------------------------------------

def facts(**over):
    return {"exists": True, "sku": "F4", "state": "Paused", "admins": [UPN, PIPE], **over}


def test_a_capacity_that_does_not_exist_is_created_with_the_configured_administrators():
    action, why, admins = pf.capacity_action(settings(CD_CONSUMER_OBJECT_IDS=CONS), {"exists": False})
    assert action == "create" and admins == (UPN, PIPE, CONS)


def test_a_capacity_that_has_every_administrator_is_left_alone():
    action, _why, admins = pf.capacity_action(settings(), facts())
    assert action == "keep" and admins == (UPN, PIPE)
    action, *_ = pf.capacity_action(settings(), facts(admins=[UPN.upper(), PIPE.upper(), "someone@else.test"]))
    assert action == "keep"                                                                              # case does not matter, and extra people are not a difference


@pytest.mark.parametrize("sku", ["F2", "F8", "F64"])
def test_a_capacity_of_another_size_is_never_resized(sku):
    """An ARM deployment of an existing capacity is an update: it would set the size to F4 and change the bill."""
    action, why, admins = pf.capacity_action(settings(), facts(sku=sku))
    assert action == "refuse" and f"is {sku}" in why and "never resizes" in why and admins == ()


def test_missing_administrators_are_added_and_nobody_is_removed():
    action, why, admins = pf.capacity_action(settings(CD_CONSUMER_OBJECT_IDS=CONS), facts(admins=["manual@added.test", UPN, PIPE]))
    assert action == "update" and CONS in why
    assert admins == ("manual@added.test", UPN, PIPE, CONS)                                              # the person added by hand in the portal stays


def test_an_unreadable_administrator_list_is_refused_not_overwritten():
    """An update deploys only the configured members, so it would replace administrators the platform cannot see."""
    action, why, admins = pf.capacity_action(settings(), facts(admins=None))
    assert action == "refuse" and "could not be read" in why and "replace administrators" in why and admins == ()


def test_an_unreadable_size_is_refused_not_deployed_over():
    """A deployment sets the size to F4: over an F8 whose size could not be read that is a resize, and a change of the bill."""
    for missing in (None, ""):
        action, why, admins = pf.capacity_action(settings(), facts(sku=missing, admins=[UPN]))
        assert action == "refuse" and "size could not be read" in why and "change the bill" in why and admins == ()


# ---- build ----------------------------------------------------------------------------------------------------

def test_a_clean_build_creates_the_source_first_then_the_capacity_and_pauses(tmp_path):
    io = FakeIo()
    rc, br = run_build(io, tmp_path)
    assert rc == 0
    assert io.calls == ["warm", "identity", "infra storage", "upload", "download", "build layers", "capacity status", "infra capacity", "capacity resume", "workspace",
                        "gate", "missing", "capacity status", "capacity pause"]
    assert io.calls.index("upload") < io.calls.index("infra capacity")                                   # nothing bills by the hour before the capacity exists
    assert io.deployed_admins == [UPN, PIPE]                                                             # a person by UPN, the pipeline by object id
    assert [r[1] for r in br.report.steps][:-1] == ["ok"] * 12 and br.report.steps[-1][0] == "pause the capacity"
    assert "capacity: created" in br.report.notes
    assert br.outputs["workspace"] == {"name": "Demo-Dev", "id": "ws-1"} and br.outputs["lakehouse"]["name"] == "ContosoBankLH"
    assert br.outputs["dataSource"]["storageAccount"].startswith("stpromptops") and br.outputs["ontologyGate"]["status"] == "open"
    assert br.outputs["dataSource"]["url"].endswith(".dfs.core.windows.net/source")
    written = json.loads((tmp_path / "out" / "platform-outputs.json").read_text(encoding="utf-8"))
    assert written == br.outputs and "token" not in json.dumps(written).lower() and "secret" not in json.dumps(written).lower()


def test_missing_tables_or_a_reload_load_the_data_and_present_tables_do_not(tmp_path):
    io = FakeIo(missing=["dim_customer"])
    assert run_build(io, tmp_path)[0] == 0
    assert io.calls.index("kit load") > io.calls.index("missing") and io.calls.index("kit load") > io.calls.index("gate")      # the gate is asked about first
    io = FakeIo()
    _rc, br = run_build(io, tmp_path / "again")
    assert "kit load" not in io.calls and any("load skipped" in n for n in br.report.notes)
    io = FakeIo()
    run_build(io, tmp_path / "third", reload_data=True)
    assert "kit load" in io.calls


# ---- an existing capacity: never redeployed blindly, never resized ----------------------------------------------

def test_a_rerun_does_not_deploy_the_capacity_again_when_nothing_is_missing(tmp_path):
    io = FakeIo(cap_exists=True, cap_admins=[UPN, PIPE])
    rc, br = run_build(io, tmp_path)
    assert rc == 0 and "infra capacity" not in io.calls and "capacity: kept" in br.report.notes
    assert io.calls.index("capacity resume") < io.calls.index("workspace") and io.calls[-1] == "capacity pause"       # it is still resumed, used and paused


def test_a_rerun_adds_only_the_missing_administrator(tmp_path):
    io = FakeIo(cap_exists=True, cap_admins=["manual@added.test", UPN, PIPE])
    rc, br = pf.build(settings(CD_CONSUMER_OBJECT_IDS=CONS), io, tmp_path / "out", clock=clock(), sleeper=lambda s: None)
    assert rc == 0 and "capacity: updated" in br.report.notes
    assert io.deployed_admins == ["manual@added.test", UPN, PIPE, CONS]


def test_a_capacity_of_another_size_stops_the_build_before_anything_changes_it(tmp_path):
    """A re-run used to deploy an F4 over an F8 and so resize it. Now it stops, and pauses nothing it did not touch."""
    io = FakeIo(cap_exists=True, cap_sku="F8", cap_admins=[UPN, PIPE], state="Active")
    rc, br = run_build(io, tmp_path)
    assert rc == cd.EXIT_FAILED and "infra capacity" not in io.calls and "capacity resume" not in io.calls
    assert "capacity pause" not in io.calls and io.state == "Active"                                     # not touched: not paused either
    assert any("never resizes" in n or "failed at" in n for n in br.report.notes) and br.report.active_seconds is None


def test_the_capacity_is_not_started_when_there_is_too_little_time_to_pause_it_safely(tmp_path):
    io = FakeIo()
    rc, br = pf.build(settings(), io, tmp_path / "out", clock=clock(step_s=1.0), sleeper=lambda s: None, deadline_s=pf.MIN_CAPACITY_STEP_S - 300.0)
    assert rc == cd.EXIT_FAILED and "infra capacity" not in io.calls and "capacity status" not in io.calls and "capacity pause" not in io.calls
    assert br.report.active_seconds is None


# ---- a capacity deployment that is still running behind the pause ------------------------------------------------

def test_a_failed_capacity_deployment_is_settled_first_and_a_missing_capacity_is_nothing_to_pause(tmp_path, capsys):
    io = FakeIo(fail="infra capacity")
    rc, br = run_build(io, tmp_path)
    out = capsys.readouterr().out
    assert rc == cd.EXIT_FAILED and "settle" in io.calls and "capacity pause" not in io.calls
    assert io.calls.index("settle") < io.calls[::-1].index("capacity status") * -1 + len(io.calls)         # settled before the last status read
    assert "does not exist, so there is nothing to pause" in out and "MAY STILL BE RUNNING" not in out    # not a false alarm


def test_a_capacity_that_appeared_while_the_deployment_was_abandoned_is_paused(tmp_path):
    """The client gave up, ARM went on and created it: after the settle the pause finds it."""
    io = FakeIo(create_then_fail=True)
    rc, br = run_build(io, tmp_path)
    assert rc == cd.EXIT_FAILED and io.calls.index("settle") < io.calls.index("capacity pause") and io.state == "Paused"
    assert br.report.active_seconds is not None


def test_a_deployment_that_returned_normally_needs_no_settling(tmp_path):
    io = FakeIo()
    run_build(io, tmp_path)
    assert "settle" not in io.calls


def test_an_interrupt_during_the_capacity_deployment_settles_and_pauses(tmp_path):
    io = FakeIo(interrupt_on="infra capacity")
    with pytest.raises(KeyboardInterrupt):
        run_build(io, tmp_path)
    assert "settle" in io.calls                                                                          # cancelled, waited for, then whatever exists is paused


def test_the_last_line_of_defence_settles_then_pauses_and_is_harmless_when_nothing_exists(capsys):
    io = FakeIo(cap_exists=True, state="Active")
    assert pf.rescue(settings(), io) == 0 and io.calls == ["settle", "capacity status", "capacity pause"] and io.state == "Paused"
    io = FakeIo()
    assert pf.rescue(settings(), io) == 0 and io.calls == ["settle", "capacity status"]
    assert "nothing to pause" in capsys.readouterr().out
    io = FakeIo(cap_exists=True, state="Active", pause_error="forbidden")
    assert pf.rescue(settings(), io) == cd.EXIT_STILL_RUNNING and "MAY STILL BE RUNNING" in capsys.readouterr().out


def test_the_last_line_of_defence_pauses_when_it_cannot_settle_but_does_not_call_the_run_clean(capsys):
    io = FakeIo(cap_exists=True, state="Active")
    io.settle_capacity_deployment = lambda s: (_ for _ in ()).throw(cd.CdError("az hung"))
    rc, _t = rescue_with(io)
    out = capsys.readouterr().out
    assert rc == cd.EXIT_STILL_RUNNING and io.state == "Paused" and "could not settle" in out and "STILL NOT OVER" in out
    io = FakeIo(cap_exists=True, state="Active")
    real = io.capacity
    io.capacity = lambda base, action, timeout=None: (_ for _ in ()).throw(cd.CdError("status broke")) if action == "status" else real(base, action, timeout)
    assert pf.rescue(settings(), io) == 0 and io.state == "Paused" and "trying to pause it anyway" in capsys.readouterr().out


def test_the_last_line_of_defence_keeps_going_when_it_cannot_read_the_capacity(capsys):
    io = FakeIo(cap_exists=True, state="Active")
    real = io.capacity
    io.capacity = lambda base, action, timeout=None: (_ for _ in ()).throw(cd.CdError("status broke")) if action == "status" else real(base, action, timeout)
    assert pf.rescue(settings(), io) == 0 and io.state == "Paused" and "trying to pause it anyway" in capsys.readouterr().out


def rescue_with(io):
    t = Time()
    return pf.rescue(settings(), io, clock=t.now, sleeper=t.sleep), t


@pytest.mark.parametrize("state", ["Running", "Unknown", "Accepted"])
def test_a_deployment_that_could_not_be_stopped_and_no_capacity_yet_is_an_error_not_a_success(state, capsys):
    """Azure can still create the capacity after the job ends, and it bills from then on: 'nothing to pause' would be a false all-clear."""
    io = FakeIo(settle_state=state, settle_cancels=True)
    rc, t = rescue_with(io)
    out = capsys.readouterr().out
    assert rc == cd.EXIT_STILL_RUNNING and "THE CAPACITY MAY STILL APPEAR AND BILL" in out and pf.CAPACITY_DEPLOYMENT in out and "nothing to pause" not in out
    assert sum(t.slept) == pf.LATE_APPEARANCE_S and "capacity pause" not in io.calls                    # it looked for a late appearance first, for a bounded time


def test_a_settle_that_itself_fails_is_treated_as_a_deployment_in_doubt(capsys):
    io = FakeIo()
    io.settle_capacity_deployment = lambda s: (_ for _ in ()).throw(cd.CdError("az hung"))
    rc, t = rescue_with(io)
    assert rc == cd.EXIT_STILL_RUNNING and "MAY STILL APPEAR AND BILL" in capsys.readouterr().out and sum(t.slept) == pf.LATE_APPEARANCE_S


def test_a_deployment_that_could_not_be_stopped_but_whose_capacity_exists_is_paused_and_the_run_is_not_clean(capsys):
    """The capacity is paused, but a deployment that is still running can change it: exit 4 until someone has looked."""
    io = FakeIo(settle_state="Running", settle_cancels=True, cap_exists=True, state="Active")
    rc, _t = rescue_with(io)
    assert rc == cd.EXIT_STILL_RUNNING and io.state == "Paused" and "THE CAPACITY DEPLOYMENT IS STILL NOT OVER" in capsys.readouterr().out


def test_a_capacity_that_appears_after_a_cancel_is_found_and_paused():
    """ARM cancel is not an undo: the capacity service can finish what it had started."""
    io = FakeIo(settle_state="Canceled", settle_cancels=True, appears_after=3)
    rc, t = rescue_with(io)
    assert rc == 0 and io.state == "Paused" and io.calls[-1] == "capacity pause" and t.slept == [pf.PROVISION_POLL_S] * 3


def test_a_cancelled_deployment_with_no_capacity_in_the_window_is_an_error_because_nothing_can_prove_it_will_not_appear(capsys):
    io = FakeIo(settle_state="Canceled", settle_cancels=True)
    rc, t = rescue_with(io)
    out = capsys.readouterr().out
    assert rc == cd.EXIT_STILL_RUNNING and f"did not appear in {int(pf.LATE_APPEARANCE_S)} s" in out and "A cancel is not an undo" in out and "scheduled guard" in out
    assert "::error::THE CAPACITY MAY STILL APPEAR AND BILL" in out and sum(t.slept) == pf.LATE_APPEARANCE_S


def test_with_no_deployment_in_doubt_an_absent_capacity_is_not_waited_for():
    io = FakeIo()
    rc, t = rescue_with(io)
    assert rc == 0 and t.slept == [] and io.calls == ["settle", "capacity status"]


def test_a_deployment_that_was_cancelled_long_ago_and_no_capacity_is_not_an_alarm(capsys):
    """After a destroy, or a build that failed early, an old cancelled record must not turn every later run red: the age of the cancel decides (see Live)."""
    io = FakeIo(settle_state="Canceled", settle_cancels=False)
    rc, t = rescue_with(io)
    assert rc == 0 and t.slept == [] and "nothing to pause" in capsys.readouterr().out


def test_a_deployment_that_somebody_else_just_cancelled_is_looked_after_like_one_this_run_cancelled(capsys):
    io = FakeIo(settle_state="Canceled", settle_cancels=True)
    rc, t = rescue_with(io)
    assert rc == cd.EXIT_STILL_RUNNING and "MAY STILL APPEAR AND BILL" in capsys.readouterr().out and sum(t.slept) == pf.LATE_APPEARANCE_S


def test_a_capacity_that_is_still_being_provisioned_is_waited_for_before_it_is_paused(capsys):
    io = FakeIo(cap_exists=True, state="Active", provisioning=["Provisioning", "Updating", "Succeeded"])
    rc, t = rescue_with(io)
    assert rc == 0 and io.calls == ["settle", "capacity status", "capacity status", "capacity status", "capacity pause"] and t.slept == [pf.PROVISION_POLL_S] * 2
    assert "still being provisioned" in capsys.readouterr().out


def test_the_wait_for_provisioning_is_bounded_and_the_pause_is_tried_anyway():
    io = FakeIo(cap_exists=True, state="Active", provisioning=["Provisioning"] * 1000)
    rc, t = rescue_with(io)
    assert sum(t.slept) == pf.PROVISION_WAIT_S and io.calls[-1] == "capacity pause" and rc == 0


@pytest.mark.parametrize("st, expected", [({"provisioningState": "Succeeded", "state": "Active"}, False), ({"provisioningState": "Provisioning"}, True),
                                          ({"provisioningState": "Updating"}, True), ({"provisioningState": None, "state": "Paused"}, False),
                                          ({"provisioningState": "Accepted"}, True), ({"provisioningState": "Succeeded", "state": "Provisioning"}, True),
                                          ({"provisioningState": "Failed", "state": "Failed"}, False), ({}, False)])
def test_a_capacity_is_being_provisioned_unless_azure_says_it_is_done(st, expected):
    assert pf.being_provisioned(st) is expected


def test_the_worst_case_before_the_pause_is_what_the_job_timeout_is_sized_for():
    assert pf.PAUSE_PREAMBLE_MAX_S == pf.SETTLE_TOTAL_S + max(pf.PROVISION_WAIT_S, pf.LATE_APPEARANCE_S) + 2 * (cd.STATUS_TIMEOUT_S + cd.GRACE_S)


def test_a_failed_deployment_that_cannot_be_stopped_is_a_loud_failure_of_the_build(tmp_path, capsys):
    io = FakeIo(fail="infra capacity", settle_state="Running", settle_cancels=True)
    rc, _br = run_build(io, tmp_path)
    assert rc == cd.EXIT_STILL_RUNNING and "MAY STILL APPEAR AND BILL" in capsys.readouterr().out


def test_the_capacity_deployment_is_given_less_time_than_the_step_has(tmp_path):
    io = FakeIo()
    pf.build(settings(), io, tmp_path / "out", clock=clock(), sleeper=lambda s: None, deadline_s=1000.0)
    assert len(io.infra_timeouts) == 1 and io.infra_timeouts[0] <= 1000 - cd.GRACE_S - 30 and io.infra_timeouts[0] < pf.INFRA_TIMEOUT_S
    io = FakeIo()
    pf.build(settings(), io, tmp_path / "out2", clock=clock(), sleeper=lambda s: None)
    assert io.infra_timeouts == [pf.INFRA_TIMEOUT_S]                                                    # with all the time there is, the usual limit


def test_a_gate_probe_that_left_something_behind_fails_the_build_and_the_capacity_is_paused(tmp_path):
    io = FakeIo(gate_warning="left behind in the workspace, delete by hand: PromptOpsGateProbe")
    rc, br = run_build(io, tmp_path, keep_running=True)
    assert rc == cd.EXIT_FAILED and io.calls[-1] == "capacity pause" and io.state == "Paused"          # --keep-running is not honoured
    assert any("not a success" in n and "PromptOpsGateProbe" in n for n in br.report.notes) and (tmp_path / "out" / "platform-outputs.json").exists()
    assert "FAILED" in pf.render_summary(settings(), br, rc)


@pytest.mark.parametrize("failing", ["warm", "identity", "infra storage", "upload", "download", "build layers"])
def test_a_failure_before_the_capacity_exists_costs_nothing_and_pauses_nothing(failing, tmp_path):
    io = FakeIo(fail=failing)
    rc, br = run_build(io, tmp_path)
    assert rc == cd.EXIT_FAILED and "infra capacity" not in io.calls and "capacity pause" not in io.calls and "settle" not in io.calls
    assert br.report.active_seconds is None


@pytest.mark.parametrize("failing", ["workspace", "missing", "kit load", "gate"])
def test_a_failure_after_the_capacity_exists_still_pauses_it(failing, tmp_path):
    io = FakeIo(fail=failing, missing=["t"])
    if failing == "gate":
        io.gate_state = lambda s, out: (_ for _ in ()).throw(cd.CdError("gate probe broke"))
    rc, br = run_build(io, tmp_path)
    assert rc == cd.EXIT_FAILED and io.calls[-1] == "capacity pause" and io.state == "Paused" and "settle" not in io.calls
    assert br.report.active_seconds and any("failed at" in n for n in br.report.notes)


@pytest.mark.parametrize("keep, failing, expect_pause", [(True, None, False), (True, "workspace", True), (False, None, True)])
def test_keep_running_is_honoured_only_after_a_complete_success(keep, failing, expect_pause, tmp_path):
    io = FakeIo(fail=failing)
    rc, br = run_build(io, tmp_path, keep_running=keep)
    assert ("capacity pause" in io.calls) is expect_pause
    if failing:
        assert rc == cd.EXIT_FAILED and any("--keep-running was ignored" in n for n in br.report.notes)


def test_keep_running_is_ignored_when_the_gate_is_still_closed(tmp_path):
    io = FakeIo(gate=("closed",))
    rc, _br = run_build(io, tmp_path, keep_running=True, gate_wait_min=0)
    assert rc == pf.EXIT_GATE and io.calls[-1] == "capacity pause"


def test_an_interrupt_pauses_once_the_capacity_exists_and_not_before(tmp_path):
    io = FakeIo(interrupt_on="workspace")
    with pytest.raises(KeyboardInterrupt):
        run_build(io, tmp_path, keep_running=True)
    assert io.calls[-1] == "capacity pause"
    io = FakeIo(interrupt_on="upload")
    with pytest.raises(KeyboardInterrupt):
        run_build(io, tmp_path / "early")
    assert "capacity pause" not in io.calls and "settle" not in io.calls


def test_a_capacity_that_cannot_be_paused_is_loud(tmp_path, capsys):
    rc, _ = run_build(FakeIo(pause_error="forbidden"), tmp_path)
    assert rc == cd.EXIT_STILL_RUNNING and "MAY STILL BE RUNNING" in capsys.readouterr().out
    rc, _ = run_build(FakeIo(pause_state="Active"), tmp_path / "b")
    assert rc == cd.EXIT_STILL_RUNNING


def test_a_stuck_step_is_abandoned_and_the_capacity_is_still_paused(monkeypatch, tmp_path):
    real = cd.run_bounded
    monkeypatch.setattr(cd, "run_bounded", lambda fn, seconds: real(fn, 0.1))
    release = threading.Event()
    io = FakeIo()
    io.workspace_and_lakehouse = lambda s, out: release.wait(10)
    try:
        rc, br = run_build(io, tmp_path)
    finally:
        release.set()
    assert rc == cd.EXIT_FAILED and io.calls[-1] == "capacity pause" and any(r[1] == "abandoned" for r in br.report.steps)


def test_a_stuck_capacity_deployment_is_abandoned_then_settled_before_the_pause(monkeypatch, tmp_path):
    real = cd.run_bounded
    monkeypatch.setattr(cd, "run_bounded", lambda fn, seconds: real(fn, 0.2))
    release = threading.Event()
    io = FakeIo()
    original = io.infra

    def slow(s, storage, deploy_capacity, admins=None, timeout=None):
        if deploy_capacity:
            io.calls.append("infra capacity")
            release.wait(10)
        return original(s, storage, deploy_capacity, admins, timeout)
    io.infra = slow
    try:
        rc, br = run_build(io, tmp_path)
    finally:
        release.set()
    assert rc == cd.EXIT_FAILED and any(r[1] == "abandoned" for r in br.report.steps) and "settle" in io.calls


def test_steps_are_not_started_after_the_deadline(tmp_path):
    io = FakeIo()
    rc, br = pf.build(settings(), io, tmp_path / "out", clock=clock(step_s=100.0), sleeper=lambda s: None, deadline_s=1500.0)
    assert rc == cd.EXIT_FAILED and any(r[1] == "not started" for r in br.report.steps)
    assert "gate" not in io.calls and "infra capacity" not in io.calls and "capacity pause" not in io.calls   # too little time left to create the capacity safely


# ---- the ontology gate ----------------------------------------------------------------------------------------

def test_a_closed_gate_shows_the_admin_what_to_click_and_carries_on_when_it_opens(tmp_path):
    io = FakeIo(gate=("closed", "closed", "open"))
    sleeps = []
    rc, br = run_build(io, tmp_path, sleeper=sleeps.append, clock=clock(step_s=1.0))
    assert rc == 0 and br.gate.status == "open" and io.gate_checks == 3 and sleeps == [pf.GATE_POLL_S, pf.GATE_POLL_S]
    request = (tmp_path / "out" / "ADMIN_APPROVAL.md").read_text(encoding="utf-8")
    assert "capacities/cap-guid-1/delegatedSettings" in request and "Users can create Ontology (preview) items" in request
    assert "Users can use Copilot and other features powered by Azure OpenAI" in request and "Override tenant admin selection" in request


def test_the_admin_is_asked_while_the_data_loads_not_after_it(tmp_path, capsys):
    """The click needs 7 to 11 minutes to apply and the load takes about as long, so the request comes first and the wait comes last."""
    io = FakeIo(gate=("closed", "closed", "open"), missing=["dim_customer"])
    run_build(io, tmp_path)
    out = capsys.readouterr().out
    assert out.index("ACTION NEEDED") < out.index("== load the data") < out.index("== wait for the ontology gate") < out.index("gate: open after")
    assert io.calls.index("kit load") > io.calls.index("gate") and io.calls[-1] == "capacity pause"
    assert io.gate_checks == 3                                                                           # once up front, twice while waiting


def test_a_gate_that_is_open_at_once_is_not_asked_about_twice(tmp_path):
    io = FakeIo(gate=("open",), missing=["dim_customer"])
    run_build(io, tmp_path)
    assert io.gate_checks == 1 and not (tmp_path / "out" / "ADMIN_APPROVAL.md").exists()


def test_a_gate_that_never_opens_ends_in_exit_3_with_the_request_in_the_summary(tmp_path):
    io = FakeIo(gate=("closed",))
    rc, br = run_build(io, tmp_path, gate_wait_min=2, clock=clock(step_s=45.0), deadline_s=100000.0)
    assert rc == pf.EXIT_GATE and io.calls[-1] == "capacity pause"
    text = pf.render_summary(settings(), br, rc)
    assert "the ontology gate needs an admin click" in text and "Action needed: open the ontology gate" in text and "delegatedSettings" in text


def test_a_gate_check_that_cannot_tell_does_not_fail_the_build(tmp_path):
    rc, br = run_build(FakeIo(gate=("unknown",)), tmp_path)
    assert rc == 0 and any("could not be checked" in n for n in br.report.notes)


def test_the_wait_is_cut_to_the_time_that_is_left(tmp_path):
    io = FakeIo(gate=("closed",))
    sleeps = []
    pf.build(settings(), io, tmp_path / "out", gate_wait_min=30, sleeper=sleeps.append, clock=clock(step_s=60.0), deadline_s=2400.0)
    assert sum(sleeps) <= 2400 - 90                                                                      # never more than the deadline allows


def test_a_third_setting_is_asked_for_outside_the_us_and_eu():
    s = settings(CD_LOCATION="southindia")
    assert "outside this capacity's geographic region" in pf.approval_request(s, "cap", 20, cross_geo=True)
    assert "outside this capacity's geographic region" not in pf.approval_request(s, "cap", 20, cross_geo=False)


# ---- preflight ------------------------------------------------------------------------------------------------

class PreflightIo(FakeIo):
    def __init__(self, fabric=None, what_if=None, identity_error=None, tokens_error=None):
        super().__init__()
        guide = pf.tenant_settings_guide(settings().group_name)
        self._fabric = fabric if fabric is not None else [pf.Check("Fabric API access for the pipeline's identity", "ok", "x"),
                                                         pf.Check("Permission to create workspaces", "skipped", "cannot be proven before the capacity exists", guide["workspaces"])]
        self._what_if, self._identity_error, self._tokens_error = what_if if what_if is not None else {"Create": 2}, identity_error, tokens_error

    def identity(self, s):
        if self._identity_error:
            raise cd.CdError(self._identity_error)
        return super().identity(s)

    def warm_tokens(self, base):
        if self._tokens_error:
            raise cd.CdError(self._tokens_error)

    def probe_fabric(self, s):
        self.calls.append("probe fabric")
        return self._fabric

    def what_if(self, s, storage):
        self.calls.append("what-if")
        return self._what_if


def test_preflight_passes_when_every_door_that_can_be_checked_is_open_and_says_what_it_could_not_check():
    lines = []
    rc, checks = pf.preflight(settings(), PreflightIo(), lines.append)
    assert rc == 0 and [c.status for c in checks] == ["ok", "ok", "ok", "skipped", "ok"] and "2 Create" in lines[0]
    assert "make sure it is on (it cannot be checked yet)" in lines[0] and "Service principals can create workspaces, connections, and deployment pipelines" in lines[0]


def test_preflight_shows_a_closed_door_with_the_exact_setting_to_change():
    closed = [pf.Check("Fabric API access for the pipeline's identity", "closed", "403", pf.tenant_settings_guide(settings().group_name)["api"])]
    lines = []
    rc, _ = pf.preflight(settings(), PreflightIo(fabric=closed), lines.append)
    text = lines[0]
    assert rc == pf.EXIT_GATE and "[CLOSED ]" in text and "do this: " in text and "Service principals can call Fabric public APIs" in text
    assert "contoso-promptops-fabric-apps" in text and pf.TENANT_SETTINGS_URL in text


@pytest.mark.parametrize("kwargs, name", [({"identity_error": "not signed in"}, "Sign-in"), ({"tokens_error": "no token"}, "Tokens for every service"),
                                          ({"what_if": None}, None)])
def test_preflight_stops_at_a_real_error(kwargs, name):
    io = PreflightIo(**{k: v for k, v in kwargs.items() if v is not None})
    if kwargs.get("what_if", 1) is None:
        io.what_if = lambda s, storage: (_ for _ in ()).throw(cd.CdError("template invalid"))
        name = "Infrastructure plan (what-if)"
    rc, checks = pf.preflight(settings(), io, lambda m: None)
    assert rc == cd.EXIT_FAILED and any(c.status == "error" and c.name == name for c in checks)
    if name == "Sign-in":
        assert io.calls == [] and len(checks) == 1                                                       # nothing else is tried without a sign-in


# ---- destroy and status ---------------------------------------------------------------------------------------

class DestroyIo(FakeIo):
    """Workspaces that exist, a capacity that may be paused, and the purge the kit does."""

    def __init__(self, workspaces=("Demo-Dev", "Demo-Prod"), left=0, fail=None, purge_problem=None, **kw):
        super().__init__(cap_exists=kw.pop("cap_exists", True), **kw)
        self.workspaces, self.left, self.failing, self.purge_problem = list(workspaces), left, fail, purge_problem

    def find_workspaces(self, s, names):
        self.calls.append(f"find {','.join(names)}")
        return [(f"id-{n}", n) for n in names if n in self.workspaces]

    def purge_workspace(self, s, workspace_id):
        self.calls.append(f"purge {workspace_id}")
        if self.failing == "purge":
            raise cd.CdError("403")
        rep = {"itemsDeleted": ["Lakehouse:ContosoBankLH (hard)"], "recoverablePurged": [], "itemsLeft": [], "recoverableLeft": [], "errors": [], "workspaceDeleted": True}
        if self.purge_problem:
            rep["errors"] = [self.purge_problem]
        return rep

    def delete_capacity(self, s):
        self.calls.append("delete capacity")
        if self.failing == "capacity":
            raise cd.CdError("busy")
        self.cap_exists = False

    def delete_storage(self, s, storage):
        self.calls.append("delete storage")

    def resource_count(self, s):
        self.calls.append("count")
        return self.left


def test_destroy_purges_only_this_environments_workspace_and_pauses_what_it_resumed():
    io = DestroyIo(state="Paused")
    assert pf.destroy(settings(), io, False, lambda m: None) == 0
    assert io.calls == ["identity", "find Demo-Dev", "capacity status", "capacity resume", "purge id-Demo-Dev", "capacity status", "capacity pause"]
    assert io.state == "Paused" and "id-Demo-Prod" not in " ".join(io.calls)


def test_destroy_does_not_resume_a_capacity_that_is_not_needed():
    io = DestroyIo(workspaces=())
    assert pf.destroy(settings(), io, False, lambda m: None) == 0 and io.calls == ["identity", "find Demo-Dev"]


def test_destroy_everything_takes_both_workspaces_the_capacity_and_the_storage_and_checks_the_group_is_empty():
    io = DestroyIo(state="Paused")
    said = []
    assert pf.destroy(settings(), io, True, said.append) == 0
    assert io.calls == ["identity", "find Demo-Dev,Demo-Prod", "capacity status", "capacity resume", "purge id-Demo-Dev", "purge id-Demo-Prod", "delete capacity",
                        "delete storage", "count", "capacity status"]                                      # the capacity is gone, so nothing is paused
    assert any("resources left in rg-demo: 0" in m for m in said) and any("nothing to pause" in m for m in said)
    io = DestroyIo(left=1, state="Paused")
    assert pf.destroy(settings(), io, True, lambda m: None) == cd.EXIT_FAILED


@pytest.mark.parametrize("failing", ["purge", "capacity"])
def test_destroy_goes_on_after_a_failure_reports_it_and_still_pauses(failing):
    io = DestroyIo(fail=failing, state="Paused")
    said = []
    assert pf.destroy(settings(), io, True, said.append) == cd.EXIT_FAILED
    assert "delete storage" in io.calls and any("::error::" in m for m in said)
    if failing == "capacity":
        assert io.calls[-1] == "capacity pause" and io.state == "Paused"                                 # it could not be deleted, so it must not be left running


def test_destroy_says_when_a_workspace_was_not_fully_removed():
    io = DestroyIo(purge_problem="Ontology:X: 403", workspaces=("Demo-Dev",), state="Paused")
    said = []
    assert pf.destroy(settings(), io, False, said.append) == cd.EXIT_FAILED
    assert any("was not fully removed" in m and "Ontology:X: 403" in m for m in said) and io.calls[-1] == "capacity pause"


def test_destroy_pauses_even_when_it_is_interrupted():
    io = DestroyIo(state="Paused")
    io.purge_workspace = lambda s, wid: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        pf.destroy(settings(), io, False, lambda m: None)
    assert io.calls[-1] == "capacity pause" and io.state == "Paused"


def test_destroy_with_no_capacity_resumes_and_pauses_nothing():
    io = DestroyIo(cap_exists=False)
    assert pf.destroy(settings(), io, False, lambda m: None) == 0
    assert "capacity resume" not in io.calls and "capacity pause" not in io.calls and "purge id-Demo-Dev" in io.calls


def test_status_warns_about_a_capacity_that_may_be_billing():
    class StatusIo(FakeIo):
        def inventory(self, s, storage):
            return {"capacity": {"exists": True, "state": "Active", "sku": "F4"}, "storage": True, "workspace": True, "lakehouse": True}
    said = []
    assert pf.status(settings(), StatusIo(), said.append) == 0
    assert any("::notice::" in m and "may be billing" in m for m in said)


# ---- the command line -----------------------------------------------------------------------------------------

def test_main_plan_and_settings_errors(capsys):
    assert pf.main(["plan", "--env", "dev"], environ=ENV, io=object()) == 0 and "Environment: dev" in capsys.readouterr().out
    assert pf.main(["plan", "--env", "dev"], environ={}, io=object()) == cd.EXIT_CONFIG
    assert "::error::missing repository variables" in capsys.readouterr().out


def test_main_rescue_needs_only_the_basic_variables(capsys):
    basic = {k: v for k, v in ENV.items() if k in ("CD_RESOURCE_GROUP", "CD_CAPACITY", "CD_WORKSPACE_DEV", "CD_WORKSPACE_PROD")}
    io = FakeIo(cap_exists=True, state="Active")
    assert pf.main(["rescue", "--env", "dev"], environ=basic, io=io) == 0 and io.state == "Paused" and io.calls[0] == "settle"
    assert pf.main(["rescue", "--env", "dev"], environ={}, io=io) == cd.EXIT_CONFIG                      # only the group, the capacity and the workspace are required


def test_main_build_writes_the_summary_and_refuses_a_dirty_folder(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pf.time, "sleep", lambda s: None)
    summary = tmp_path / "summary.md"
    assert pf.main(["build", "--env", "dev", "--out", str(tmp_path / "o"), "--summary", str(summary)], environ=ENV, io=FakeIo()) == 0
    assert summary.read_text(encoding="utf-8").startswith("### Platform build (dev): done")
    assert pf.main(["build", "--env", "dev", "--out", str(tmp_path / "o")], environ=ENV, io=FakeIo()) == cd.EXIT_CONFIG
    assert "not empty" in capsys.readouterr().out


def test_main_preflight_writes_the_guided_steps_into_the_summary(tmp_path):
    closed = [pf.Check("Fabric API access for the pipeline's identity", "closed", "403", "do the first thing")]
    summary = tmp_path / "s.md"
    rc = pf.main(["preflight", "--env", "dev", "--summary", str(summary)], environ=ENV, io=PreflightIo(fabric=closed))
    text = summary.read_text(encoding="utf-8")
    assert rc == pf.EXIT_GATE and "a door is closed" in text and "Do this: do the first thing" in text


# ---- the real adapter, with a fake process runner --------------------------------------------------------------

def az_runner(calls, answers=None, fail_times=0):
    state = {"failed": 0}

    def run(cmd, **kw):
        calls.append((cmd, kw))
        key = " ".join(cmd[1:4])
        if "upload-batch" in cmd and state["failed"] < fail_times:
            state["failed"] += 1
            return SimpleNamespace(returncode=1, stdout="", stderr="AuthorizationPermissionMismatch")
        rc, out = (answers or {}).get(key, (0, ""))
        return SimpleNamespace(returncode=rc, stdout=out, stderr="boom" if rc else "")
    return run


def live(tmp_path, runner, **kw):
    return pf.Live(state_dir=tmp_path, runner=runner, az="az", sleeper=kw.pop("sleeper", lambda s: None), **kw)


def test_infra_runs_the_two_phases_with_a_parameters_file_and_returns_the_outputs(tmp_path):
    calls = []
    out = json.dumps({"properties": {"outputs": {"storageAccountName": {"type": "String", "value": "stx"}, "sourceUrl": {"value": "https://stx.dfs/source"}}}})
    io = live(tmp_path, az_runner(calls, {"deployment group create": (0, out)}))
    assert io.infra(settings(), "stx", False) == {"storageAccountName": "stx", "sourceUrl": "https://stx.dfs/source"}
    io.infra(settings(), "stx", True)
    first, second = calls[0][0], calls[1][0]
    assert first[1:6] == ["deployment", "group", "create", "-g", "rg-demo"] and pf.STORAGE_DEPLOYMENT in first and pf.CAPACITY_DEPLOYMENT in second
    assert first[first.index("-f") + 1].endswith("infra\\main.bicep") or first[first.index("-f") + 1].endswith("infra/main.bicep")
    params_file = first[first.index("-p") + 1].lstrip("@")
    assert "-p" in first and params_file.endswith("parameters.json") and "--only-show-errors" in first and calls[0][1]["timeout"] == pf.INFRA_TIMEOUT_S
    assert json.loads(Path(params_file).read_text(encoding="utf-8"))["parameters"]["deployCapacity"]["value"] is False
    io.infra(settings(), "stx", True, None, timeout=600)
    assert calls[-1][1]["timeout"] == 600                                                                  # the build gives the deployment less time than the step has


def test_the_administrators_to_deploy_reach_the_parameters_file(tmp_path):
    calls = []
    io = live(tmp_path, az_runner(calls, {"deployment group create": (0, "{}")}))
    io.infra(settings(), "stx", True, ["manual@added.test", UPN, PIPE])
    params = json.loads(Path(calls[0][0][calls[0][0].index("-p") + 1].lstrip("@")).read_text(encoding="utf-8"))["parameters"]
    assert params["capacityAdmins"]["value"] == ["manual@added.test", UPN, PIPE] and params["capacitySku"]["value"] == "F4"


def test_what_if_counts_the_changes(tmp_path):
    changes = json.dumps({"changes": [{"changeType": "Create"}, {"changeType": "Create"}, {"changeType": "NoChange"}]})
    io = live(tmp_path, az_runner([], {"deployment group what-if": (0, changes)}))
    assert io.what_if(settings(), "stx") == {"Create": 2, "NoChange": 1}


# ---- settling a capacity deployment that may still be running -----------------------------------------------------

def settle_runner(calls, states):
    """`deployment group show` answers the given provisioning states in turn; `cancel` is recorded."""
    queue = list(states)

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[1:4] == ["deployment", "group", "show"]:
            if not queue:
                return SimpleNamespace(returncode=0, stdout="Succeeded\n", stderr="")
            nxt = queue.pop(0)
            return SimpleNamespace(returncode=1, stdout="", stderr="DeploymentNotFound") if nxt is None else SimpleNamespace(returncode=0, stdout=nxt + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


@pytest.mark.parametrize("state", ["Succeeded", "Failed", None])
def test_a_deployment_that_has_ended_or_never_existed_is_not_cancelled(tmp_path, state):
    calls = []
    io = live(tmp_path, settle_runner(calls, [state]))
    assert io.settle_capacity_deployment(settings()) == (state or "NotFound")
    assert not any(c[1:4] == ["deployment", "group", "cancel"] for c in calls) and io.deployment_cancelled is False
    assert len(calls) == 1                                                                               # and the time of the end is not even asked for


NOW = dt.datetime(2026, 11, 4, 1, 0, 0, tzinfo=dt.timezone.utc)


def cancelled_runner(calls, stamp, state="Canceled", stamp_rc=0, hang=False):
    """`deployment group show` answers the provisioning state, or (when asked for `properties.timestamp`) the time the deployment ended."""
    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[1:4] == ["deployment", "group", "show"]:
            if "properties.timestamp" in cmd:
                if hang:
                    raise subprocess.TimeoutExpired(cmd, kw["timeout"])
                return SimpleNamespace(returncode=stamp_rc, stdout=(stamp or "") + "\n", stderr="" if stamp_rc == 0 else "boom")
            return SimpleNamespace(returncode=0, stdout=state + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


@pytest.mark.parametrize("state", ["Canceled", "Cancelled"])
@pytest.mark.parametrize("stamp", ["2026-11-04T00:55:00.123456+00:00", "2026-11-04T00:55:00Z", "2026-11-04T00:55:00.1234567Z", "2026-11-04T00:55:00", "2026-11-04T00:30:01+00:00",
                                   "2026-11-04T06:30:00+05:30"])
def test_a_deployment_that_somebody_cancelled_a_moment_ago_is_still_in_doubt(tmp_path, state, stamp):
    """A cancel is not an undo: the capacity service can finish what it had started, whoever cancelled (a person in the portal, an earlier run)."""
    calls = []
    io = live(tmp_path, cancelled_runner(calls, stamp, state), utcnow=lambda: NOW)
    assert io.settle_capacity_deployment(settings()) == state and io.deployment_cancelled is True
    assert not any(c[1:4] == ["deployment", "group", "cancel"] for c in calls)                          # it had ended already: nothing to cancel again


@pytest.mark.parametrize("stamp", ["2026-11-03T22:00:00.000000+00:00", "2026-11-04T00:29:59+00:00", "2026-11-04T05:59:00+05:30"])
def test_a_deployment_cancelled_long_ago_is_no_longer_in_doubt(tmp_path, stamp):
    """Otherwise one old cancelled record would turn every later destroy, and every build that failed early, red."""
    io = live(tmp_path, cancelled_runner([], stamp), utcnow=lambda: NOW)
    assert io.settle_capacity_deployment(settings()) == "Canceled" and io.deployment_cancelled is False


@pytest.mark.parametrize("stamp, rc, hang", [("", 0, False), ("not a time", 0, False), ("2026-13-45T99:00:00Z", 0, False), ("", 1, False), ("", 0, True)])
def test_a_cancel_time_that_cannot_be_read_counts_as_recent(tmp_path, stamp, rc, hang):
    io = live(tmp_path, cancelled_runner([], stamp, stamp_rc=rc, hang=hang), utcnow=lambda: NOW)
    assert io.settle_capacity_deployment(settings()) == "Canceled" and io.deployment_cancelled is True


def test_a_cancel_time_in_the_future_counts_as_recent(tmp_path):
    io = live(tmp_path, cancelled_runner([], "2026-11-04T03:00:00+00:00"), utcnow=lambda: NOW)           # a clock that is off must not switch the doubt off
    assert io.settle_capacity_deployment(settings()) == "Canceled" and io.deployment_cancelled is True


def test_a_running_deployment_is_cancelled_and_waited_for(tmp_path):
    calls, sleeps = [], []
    io = live(tmp_path, settle_runner(calls, ["Running", "Running", "Canceled"]), sleeper=sleeps.append)
    assert io.settle_capacity_deployment(settings()) == "Canceled"
    cancel = next(c for c in calls if c[1:4] == ["deployment", "group", "cancel"])
    assert cancel[cancel.index("-n") + 1] == pf.CAPACITY_DEPLOYMENT and cancel[cancel.index("-g") + 1] == "rg-demo"
    assert sleeps == [pf.SETTLE_POLL_S, pf.SETTLE_POLL_S] and io.deployment_cancelled is True            # a cancel is not an undo: the pause looks for a late capacity


def test_a_deployment_that_never_ends_is_waited_for_only_so_long(tmp_path):
    calls, sleeps = [], []
    io = live(tmp_path, settle_runner(calls, ["Running"] * 100), sleeper=sleeps.append)
    assert io.settle_capacity_deployment(settings()) == "Running" and sum(sleeps) == pf.SETTLE_TIMEOUT_S and io.deployment_cancelled is True


def test_a_status_call_that_hangs_does_not_stop_the_settling(tmp_path):
    calls, sleeps = [], []

    def run(cmd, **kw):
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])
    io = live(tmp_path, run, sleeper=sleeps.append)
    assert io.settle_capacity_deployment(settings()) == "Unknown" and sum(sleeps) == pf.SETTLE_TIMEOUT_S


def test_a_failed_status_call_is_not_taken_for_a_deployment_that_does_not_exist(tmp_path):
    """A sign-in or network failure says nothing about the deployment: only Azure's own 'not found' means that there is none."""
    calls, sleeps = [], []

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[1:4] == ["deployment", "group", "show"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="ERROR: Please run 'az login' to setup account.")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    io = live(tmp_path, run, sleeper=sleeps.append)
    assert io.settle_capacity_deployment(settings()) == "Unknown" and io.deployment_cancelled is True
    assert any(c[1:4] == ["deployment", "group", "cancel"] for c in calls) and sum(sleeps) == pf.SETTLE_TIMEOUT_S


@pytest.mark.parametrize("error", ["(DeploymentNotFound) Deployment 'platform-capacity' could not be found.", "(ResourceGroupNotFound) Resource group 'rg-demo' could not be found."])
def test_azures_own_not_found_means_there_is_no_deployment(tmp_path, error):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr=error)
    io = live(tmp_path, run)
    assert io.settle_capacity_deployment(settings()) == "NotFound" and io.deployment_cancelled is False and len(calls) == 1


def test_upload_uses_entra_sign_in_and_retries_a_fresh_storage_account(tmp_path):
    calls, sleeps = [], []
    io = live(tmp_path, az_runner(calls, fail_times=2), sleeper=sleeps.append)
    io.upload_source(settings(), "stx", tmp_path)
    cmd = calls[-1][0]
    assert len(calls) == 3 and sleeps == [30.0, 60.0]
    assert "--auth-mode" in cmd and cmd[cmd.index("--auth-mode") + 1] == "login" and "--account-key" not in cmd and "--connection-string" not in cmd
    assert cmd[cmd.index("--destination") + 1] == "source" and "--sas-token" not in cmd


def test_upload_gives_up_after_three_tries_with_the_reason(tmp_path):
    io = live(tmp_path, az_runner([], fail_times=9))
    with pytest.raises(cd.CdError, match="AuthorizationPermissionMismatch"):
        io.upload_source(settings(), "stx", tmp_path)


def test_download_checks_that_it_really_got_the_seed(tmp_path):
    dest = tmp_path / "src"
    io = live(tmp_path, az_runner([]))
    with pytest.raises(cd.CdError, match="data/releases is missing"):
        io.download_source(settings(), "stx", dest)
    (dest / "data" / "releases").mkdir(parents=True)
    assert io.download_source(settings(), "stx", dest) == dest


def test_identity_remembers_the_subscription_and_a_silent_az_is_reported(tmp_path):
    io = live(tmp_path, az_runner([], {"account show -o": (0, json.dumps({"id": "sub-9", "user": {"name": "app", "type": "servicePrincipal"}}))}))
    assert io.identity(settings())["subscriptionId"] == "sub-9" and io.subscription == "sub-9"

    class Silent:
        def __call__(self, cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
    with pytest.raises(cd.CdError, match="did not finish within 90 s"):
        live(tmp_path, Silent()).identity(settings())


def test_delete_helpers_and_the_resource_count(tmp_path):
    calls = []
    io = live(tmp_path, az_runner(calls, {"resource list -g": (0, json.dumps([{"id": "a"}, {"id": "b"}]))}))
    io.delete_storage(settings(), "stx")
    assert calls[0][0][1:8] == ["storage", "account", "delete", "-n", "stx", "-g", "rg-demo"] and "--yes" in calls[0][0]
    assert io.resource_count(settings()) == 2


def test_the_capacity_is_deleted_through_the_kits_own_command(tmp_path):
    calls = []

    def runner(cmd, **kw):
        calls.append((cmd, kw))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    pf.Live(state_dir=tmp_path, runner=runner).delete_capacity(settings())
    cmd, kw = calls[0]
    assert cmd[1:] == ["-m", "contoso_onto.cli", "capacity", "delete", "--execute"] and kw["env"]["CONTOSO_CAPACITY_NAME"] == "democapf4"
