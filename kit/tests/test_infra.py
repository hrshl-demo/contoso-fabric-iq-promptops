"""F4 capacity automation over ARM REST: configuration, template contract, dry-run safety, idempotent lifecycle, pricing."""
import json
import re
from pathlib import Path

import pytest

from contoso_onto import azure_infra as A
from contoso_onto.cli import env_overrides, load_config
from contoso_onto.ledger import Ledger
from fake_az import FakeAz, make_cfg as cfg

KIT = Path(__file__).resolve().parent.parent


def test_default_config_is_valid_and_matches_the_ask():
    c = cfg()
    assert A.validate_config(c) == []
    assert c["capacity"]["sku"] == "F4" and c["location"] == "eastus2"


def test_f4_is_the_size_everywhere_it_is_decided(tmp_path):
    assert A.DEMO_SKU == "F4" and A.SKUS["F4"] == 4
    assert cfg()["capacity"]["sku"] == "F4"
    assert json.loads((KIT / "config" / "demo.config.json").read_text())["capacity"]["sku"] == "F4"
    assert load_config(None, env={}, state_dir=tmp_path)["capacity"]["sku"] == "F4"            # defaults + the shipped file, nothing overriding them
    bicep = (KIT / "infra" / "fabric-capacity.bicep").read_text()
    assert re.search(r"^param sku string = 'F4'$", bicep, re.M)
    assert json.loads(Path(A.TEMPLATE).read_text())["parameters"]["sku"]["defaultValue"] == "F4"
    assert json.loads(A.write_param_file(cfg(), ["me@contoso.com"], tmp_path).read_text())["parameters"]["sku"]["value"] == "F4"


@pytest.mark.parametrize("sku", ["F2", "F8", "F64"])
def test_any_other_size_is_refused_unless_it_is_deliberate(sku):
    c = cfg()
    c["capacity"]["sku"] = sku
    errs = A.validate_config(c)
    assert len(errs) == 1 and "F4" in errs[0] and A.ALLOW_OTHER_SKU_ENV in errs[0]
    c["capacity"]["allowOtherSku"] = True
    assert A.validate_config(c) == []


def test_neither_the_environment_nor_a_stale_local_file_can_change_the_size_by_accident(tmp_path):
    assert A.validate_config(load_config(None, env={"CONTOSO_CAPACITY_SKU": "F8"}, state_dir=tmp_path))                          # refused
    deliberate = load_config(None, env={"CONTOSO_CAPACITY_SKU": "F8", "CONTOSO_ALLOW_OTHER_SKU": "1"}, state_dir=tmp_path)
    assert deliberate["capacity"]["sku"] == "F8" and A.validate_config(deliberate) == []
    assert A.validate_config(load_config(None, env={"CONTOSO_ALLOW_OTHER_SKU": "0", "CONTOSO_CAPACITY_SKU": "F8"}, state_dir=tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "local.json").write_text(json.dumps({"capacity": {"sku": "F2"}}))                                   # left over from an experiment
    assert A.validate_config(load_config(None, env={}, state_dir=tmp_path))


@pytest.mark.parametrize("name,sku,loc,frag", [("Bad_Name", "F4", "southindia", "capacity name"), ("ok123", "F5", "southindia", "sku"),
                                               ("ok123", "F4", "South India", "location"), ("1abc", "F4", "southindia", "capacity name")])
def test_config_validation(name, sku, loc, frag):
    c = cfg(location=loc)
    c["capacity"].update(name=name, sku=sku)
    assert any(frag in e for e in A.validate_config(c))


def test_parameter_file_matches_the_bicep_parameters(tmp_path):
    p = A.write_param_file(cfg(), ["me@contoso.com"], tmp_path)
    body = json.loads(p.read_text())
    bicep = (KIT / "infra" / "fabric-capacity.bicep").read_text()
    declared = set(re.findall(r"^param (\w+) ", bicep, re.M))
    assert set(body["parameters"]) == declared
    assert body["parameters"]["adminMembers"]["value"] == ["me@contoso.com"] and body["parameters"]["sku"]["value"] == "F4"
    assert set(re.findall(r"^\s+'(F\d+)'$", bicep, re.M)) == set(A.SKUS)
    assert "Microsoft.Fabric/capacities@" in bicep and "tier: 'Fabric'" in bicep


def test_compiled_template_is_the_bicep_the_scripts_deploy():
    """build.sh deploys the compiled JSON (no Bicep install needed on the host); it must not drift from the Bicep source."""
    arm = json.loads(Path(A.TEMPLATE).read_text())
    bicep = (KIT / "infra" / "fabric-capacity.bicep").read_text()
    assert set(arm["parameters"]) == set(re.findall(r"^param (\w+) ", bicep, re.M))
    res = arm["resources"]
    res = list(res.values()) if isinstance(res, dict) else res
    assert [r["type"] for r in res] == ["Microsoft.Fabric/capacities"]
    api = re.search(r"Microsoft\.Fabric/capacities@([\d-]+)", bicep).group(1)
    assert res[0]["apiVersion"] == api == A.CAP_API
    assert set(arm["parameters"]["sku"]["allowedValues"]) == set(A.SKUS)


def test_mutating_commands_are_dry_run_by_default():
    fake = FakeAz()
    az = A.Az(runner=fake)
    out = A.provision(cfg(), az, Path("out"))
    assert out["dryRun"] and all(c[:2] != ["group", "create"] for c in fake.calls)               # nothing was created
    assert [c for c in az.log if c.startswith("[dry-run]")] and any("deployment group create" in c for c in az.log)
    assert [c[:2] for c in fake.calls] == [["account", "show"], ["group", "exists"]]             # only read calls ran


def test_execute_runs_group_then_validate_then_deployment_then_reads_state(tmp_path):
    fake = FakeAz()
    az = A.Az(runner=fake, execute=True)
    state = A.provision(cfg(), az, tmp_path)
    kinds = [tuple(c[:3]) for c in fake.calls]
    assert kinds.index(("group", "create", "-n")) < kinds.index(("deployment", "group", "validate")) < kinds.index(("deployment", "group", "create"))
    assert state["exists"] and state["name"] == "contosofabricf4" and state["sku"] == "F4" and state["state"] == "Active"
    dep = next(c for c in fake.calls if c[:3] == ["deployment", "group", "create"])
    assert any(a.startswith("@") and a.endswith(".parameters.json") for a in dep)               # parameters by file, not shell-quoted JSON
    assert Path(A.TEMPLATE).name in " ".join(dep)


def test_policy_denial_stops_before_the_capacity_is_created(tmp_path):
    fake = FakeAz(validate_rc=1)
    with pytest.raises(RuntimeError, match="RequestDisallowedByPolicy"):
        A.provision(cfg(), A.Az(runner=fake, execute=True), tmp_path)
    assert ["deployment", "group", "create"] not in [c[:3] for c in fake.calls]               # nothing billable was created


RG = "rg-contoso-fabric-ontology"


def owning_ledger(tmp_path, run_id="run-1", state="present"):
    """A ledger that says this kit created the resource group (as it does after a build that created it)."""
    led = Ledger(tmp_path / "ledger.json")
    led.record("resourceGroup", RG, owned=True, state=state, subscriptionId="sub-1", location="southindia", runId=run_id)
    return led


def test_an_existing_resource_group_this_kit_did_not_create_is_refused(tmp_path):
    fake = FakeAz(rg_exists=True, cap={"sku": "F4", "state": "Active"})
    led = Ledger(tmp_path / "ledger.json")
    with pytest.raises(A.ExistingResourceGroup, match="brand-new resource group"):
        A.provision(cfg(), A.Az(runner=fake, execute=True), tmp_path, led)
    assert not [c for c in fake.calls if c[:2] == ["group", "create"] or c[:3] == ["deployment", "group", "create"]]
    assert led.entries == []                                                                      # nothing was recorded, so wipe can never touch it
    with pytest.raises(A.ExistingResourceGroup):                                                  # a dry run says the same
        A.provision(cfg(), A.Az(runner=fake), tmp_path, led)
    deleted = Ledger(tmp_path / "l2.json")
    deleted.record("resourceGroup", RG, owned=True, state="present")
    deleted.mark("resourceGroup", RG, "deleted")                                                  # wiped earlier; somebody else's group now has the name
    with pytest.raises(A.ExistingResourceGroup):
        A.provision(cfg(), A.Az(runner=fake, execute=True), tmp_path, deleted)


def test_the_deployment_asks_azure_for_f4_and_azure_reports_f4(tmp_path):
    fake = FakeAz()
    state = A.provision(cfg(), A.Az(runner=fake, execute=True), tmp_path, Ledger(tmp_path / "ledger.json"))
    assert fake.requested_skus == ["F4"] and fake.cap["sku"] == "F4" and state["sku"] == "F4"


def test_resuming_in_the_group_this_kit_created_converges_without_redeploying(tmp_path):
    fake = FakeAz(rg_exists=True, cap={"sku": "F4", "state": "Paused"}, rg_tags={A.RUN_TAG: "run-1"})
    led = owning_ledger(tmp_path)
    state = A.provision(cfg(), A.Az(runner=fake, execute=True), tmp_path, led)
    assert state["sku"] == "F4" and state["state"] == "Paused"                                  # untouched
    assert not [c for c in fake.calls if c[:2] == ["group", "create"] or c[:3] == ["deployment", "group", "create"]]
    assert led.get("fabricCapacity", "contosofabricf4")["owned"] is True and led.get("resourceGroup", RG)["details"]["runId"] == "run-1"   # same run id


def test_a_capacity_that_is_not_f4_stops_the_build_and_is_never_resized_by_the_kit(tmp_path):
    fake = FakeAz(rg_exists=True, cap={"sku": "F8", "state": "Active"}, rg_tags={A.RUN_TAG: "run-1"})
    led = owning_ledger(tmp_path)
    with pytest.raises(A.CapacitySkuMismatch, match="is F8, but this build is configured for F4.*scale --sku F4"):
        A.provision(cfg(), A.Az(runner=fake, execute=True), tmp_path, led)
    assert fake.cap == {"sku": "F8", "state": "Active"} and not fake.bodies                    # no PATCH, no deployment: it was left as it was
    assert led.get("fabricCapacity", "contosofabricf4")["owned"] is True                       # still recorded, so wipe still deletes it
    keep = cfg()
    keep["capacity"].update(sku="F8", allowOtherSku=True)                                       # keeping F8 on purpose is allowed
    assert A.provision(keep, A.Az(runner=fake, execute=True), tmp_path, led)["sku"] == "F8"


def test_a_fresh_deployment_that_comes_back_with_another_size_is_recorded_then_refused(tmp_path):
    class Wrong(FakeAz):
        def __call__(self, args):
            rc, out, err = super().__call__(args)
            if args[:3] == ["deployment", "group", "create"]:
                self.cap["sku"] = "F2"                                                           # Azure answered with a different size
            return rc, out, err
    fake, led = Wrong(), Ledger(tmp_path / "ledger.json")
    with pytest.raises(A.CapacitySkuMismatch, match="is F2"):
        A.provision(cfg(), A.Az(runner=fake, execute=True), tmp_path, led)
    assert led.get("fabricCapacity", "contosofabricf4")["state"] == "present"                  # it bills, so wipe must know about it


def test_a_group_whose_run_tag_differs_from_the_ledger_is_not_ours(tmp_path):
    fake = FakeAz(rg_exists=True, rg_tags={A.RUN_TAG: "someone-else"})
    with pytest.raises(A.ExistingResourceGroup, match="not the group this kit created"):
        A.provision(cfg(), A.Az(runner=fake, execute=True), tmp_path, owning_ledger(tmp_path))
    untagged = FakeAz(rg_exists=True)
    with pytest.raises(A.ExistingResourceGroup, match="None"):
        A.provision(cfg(), A.Az(runner=untagged, execute=True), tmp_path, owning_ledger(tmp_path))


def test_the_group_is_created_with_the_ownership_and_unique_run_tags(tmp_path):
    fake = FakeAz()
    led = Ledger(tmp_path / "ledger.json")
    A.provision(cfg(), A.Az(runner=fake, execute=True), tmp_path, led)
    entry = led.get("resourceGroup", RG)
    assert entry["owned"] is True and entry["state"] == "present" and len(entry["details"]["runId"]) == 12
    assert fake.rg_tags[A.OWNER_TAG[0]] == A.OWNER_TAG[1] and fake.rg_tags[A.RUN_TAG] == entry["details"]["runId"]      # the live tag matches the ledger
    assert fake.rg_tags["project"] == "contoso-bank-ontology"                                     # the configured tags are still applied
    other = Ledger(tmp_path / "other.json")
    A.provision(cfg(), A.Az(runner=FakeAz(), execute=True), tmp_path, other)
    assert other.get("resourceGroup", RG)["details"]["runId"] != entry["details"]["runId"]       # every build is a new run


def test_the_run_id_survives_a_crash_so_the_resume_reuses_it(tmp_path):
    class Boom(FakeAz):
        def __call__(self, args):
            if args[:3] == ["deployment", "group", "validate"]:
                raise KeyboardInterrupt
            return super().__call__(args)
    fake = Boom()
    led = Ledger(tmp_path / "ledger.json")
    with pytest.raises(KeyboardInterrupt):
        A.provision(cfg(), A.Az(runner=fake, execute=True), tmp_path, led)
    first = Ledger(tmp_path / "ledger.json").get("resourceGroup", RG)
    assert first["state"] == "pending" and fake.rg_tags[A.RUN_TAG] == first["details"]["runId"]
    A.provision(cfg(), A.Az(runner=FakeAz(rg_exists=True, rg_tags=dict(fake.rg_tags)), execute=True), tmp_path, Ledger(tmp_path / "ledger.json"))
    assert Ledger(tmp_path / "ledger.json").get("resourceGroup", RG)["details"]["runId"] == first["details"]["runId"]


def test_ledger_marks_what_this_run_creates_as_owned(tmp_path):
    fake = FakeAz()
    led = Ledger(tmp_path / "ledger.json")
    A.provision(cfg(), A.Az(runner=fake, execute=True), tmp_path, led)
    assert [(e["kind"], e["name"]) for e in led.wipe_plan()] == [("fabricCapacity", "contosofabricf4"), ("resourceGroup", "rg-contoso-fabric-ontology")]
    assert all(e["state"] == "present" for e in led.entries) and led.get("fabricCapacity", "contosofabricf4")["details"]["resourceGroup"] == "rg-contoso-fabric-ontology"


def test_ledger_entry_exists_before_the_capacity_does(tmp_path):
    """Write-ahead: if the deployment dies half way, wipe still knows about the capacity."""
    class Boom(FakeAz):
        def __call__(self, args):
            if args[:3] == ["deployment", "group", "create"]:
                raise KeyboardInterrupt
            return super().__call__(args)
    led = Ledger(tmp_path / "ledger.json")
    with pytest.raises(KeyboardInterrupt):
        A.provision(cfg(), A.Az(runner=Boom(), execute=True), tmp_path, led)
    again = Ledger(tmp_path / "ledger.json")
    cap = again.get("fabricCapacity", "contosofabricf4")
    assert cap and cap["owned"] and cap["state"] == "pending"


def test_plan_runs_what_if_when_the_resource_group_is_the_one_this_kit_created(tmp_path):
    fake = FakeAz(rg_exists=True, rg_tags={A.RUN_TAG: "run-1"})
    res = A.plan(cfg(), A.Az(runner=fake), price_fetch=lambda url: {"Items": []}, out_dir=tmp_path, ledger=owning_ledger(tmp_path))
    assert res["errors"] == [] and res["resourceGroupOwned"] is True
    assert res["whatIf"] == ["Create Microsoft.Fabric/capacities/contosofabricf4"]               # 'Ignore' noise filtered out
    assert any(c[:4] == ["deployment", "group", "what-if", "-g"] for c in fake.calls)
    assert any("validate" in s for s in res["steps"]) and res["nameAvailable"] is True
    assert any("resuming the group this kit created" in s for s in res["steps"])


def test_plan_reports_a_foreign_resource_group_as_an_error_and_a_new_one_as_the_plan(tmp_path):
    res = A.plan(cfg(), A.Az(runner=FakeAz(rg_exists=True)), price_fetch=lambda url: {"Items": []}, out_dir=tmp_path)
    assert res["resourceGroupOwned"] is False and any("did not create it" in e and "brand-new" in e for e in res["errors"])
    fresh = A.plan(cfg(), A.Az(runner=FakeAz()), price_fetch=lambda url: {"Items": []}, out_dir=tmp_path)
    assert fresh["errors"] == [] and any("a brand-new group" in s and A.RUN_TAG in s for s in fresh["steps"])


def test_plan_reports_an_existing_capacity(tmp_path):
    fake = FakeAz(rg_exists=True, cap={"sku": "F4", "state": "Active"}, rg_tags={A.RUN_TAG: "run-1"})
    res = A.plan(cfg(), A.Az(runner=fake), price_fetch=lambda url: {"Items": []}, out_dir=tmp_path, ledger=owning_ledger(tmp_path))
    assert res["capacity"]["exists"] and res["capacity"]["state"] == "Active" and "nameAvailable" not in res


def test_service_principal_without_admins_is_refused():
    with pytest.raises(ValueError, match="capacity admin"):
        A.provision(cfg(), A.Az(runner=FakeAz(user_type="servicePrincipal")), Path("out"))
    res = A.plan(cfg(), A.Az(runner=FakeAz(user_type="servicePrincipal")), price_fetch=lambda url: {"Items": []})
    assert any("CONTOSO_CAPACITY_ADMINS" in e for e in res["errors"])


def test_plan_is_read_only_and_estimates_cost(tmp_path):
    pages = [{"Items": [{"type": "Consumption", "meterName": "Compute Pool Capacity Usage CU", "unitOfMeasure": "1 Hour", "retailPrice": 0.22},
                        {"type": "Consumption", "meterName": "Capacity Overage Capacity Usage CU", "unitOfMeasure": "1 Hour", "retailPrice": 0.66}],
              "NextPageLink": "next"},
             {"Items": [{"type": "Consumption", "meterName": "Ontology AI Capacity Usage CU", "unitOfMeasure": "1 Hour", "retailPrice": 0.22},
                        {"type": "Reservation", "meterName": "Fabric Capacity CU", "unitOfMeasure": "1 Hour", "retailPrice": 1146.0}]}]
    it = iter(pages)
    fake = FakeAz()
    res = A.plan(cfg(), A.Az(runner=fake), price_fetch=lambda url: next(it), out_dir=tmp_path)
    assert res["errors"] == [] and res["nameAvailable"] is True and res["resourceGroupExists"] is False
    assert res["cost"]["usdPerHour"] == 0.88 and res["cost"]["usdPer8hDemoDay"] == 7.04 and res["cost"]["usdPerMonthIfNeverPaused"] == 642.4
    assert not [c for c in fake.calls if c[:2] == ["group", "create"]]
    assert any("deployment group create" in s for s in res["steps"])


def test_plan_flags_a_taken_capacity_name(tmp_path):
    class Taken(FakeAz):
        def __call__(self, args):
            if args[0] == "rest" and "checkNameAvailability" in args[args.index("--url") + 1]:
                return 0, json.dumps({"nameAvailable": False, "message": "already exists"}), ""
            return super().__call__(args)
    res = A.plan(cfg(), A.Az(runner=Taken()), price_fetch=lambda url: {"Items": []}, out_dir=tmp_path)
    assert any("not available" in e for e in res["errors"])


def test_pricing_lookup_failure_never_blocks_the_plan():
    def boom(url):
        raise OSError("offline")
    assert A.retail_rate_per_cu_hour("southindia", boom) is None
    assert "unavailable" in A.cost_estimate(cfg(), None)["note"]


@pytest.mark.parametrize("action,sku,method,tail", [("suspend", None, "post", "suspend"), ("resume", None, "post", "resume"),
                                                    ("scale", "F8", "patch", "contosofabricf4"), ("delete", None, "delete", "contosofabricf4")])
def test_lifecycle_dry_run_records_the_rest_call_and_runs_nothing(action, sku, method, tail):
    fake = FakeAz()
    az = A.Az(runner=fake)
    A.lifecycle(cfg(), az, action, sku)
    assert not [c for c in fake.calls if c[0] == "rest"]                                          # nothing was sent
    line = next(ln for ln in az.log if ln.startswith("[dry-run] az rest"))
    assert f"--method {method}" in line and "Microsoft.Fabric/capacities/contosofabricf4" in line and A.CAP_API in line
    if action == "scale":
        assert '"name":"F8"' in line and '"tier":"Fabric"' in line


def test_suspend_and_resume_are_idempotent():
    fake = FakeAz(rg_exists=True, cap={"sku": "F4", "state": "Active"})
    az = A.Az(runner=fake, execute=True)
    sl = lambda s: None  # noqa: E731
    assert A.lifecycle(cfg(), az, "resume", sleeper=sl)["state"] == "Active"
    assert ("post", "resume") not in fake.methods()                                               # already active: no call
    assert A.lifecycle(cfg(), az, "suspend", sleeper=sl)["state"] == "Paused"
    assert A.lifecycle(cfg(), az, "suspend", sleeper=sl)["state"] == "Paused"
    assert fake.methods().count(("post", "suspend")) == 1
    assert A.lifecycle(cfg(), az, "resume", sleeper=sl)["state"] == "Active"


def test_scale_and_delete():
    fake = FakeAz(rg_exists=True, cap={"sku": "F4", "state": "Active"})
    az = A.Az(runner=fake, execute=True)
    sl = lambda s: None  # noqa: E731
    assert A.lifecycle(cfg(), az, "scale", "F8", sleeper=sl)["sku"] == "F8"
    assert fake.bodies[-1] == {"sku": {"name": "F8", "tier": "Fabric"}}
    n = len(fake.methods())
    A.lifecycle(cfg(), az, "scale", "F8", sleeper=sl)
    assert len(fake.methods()) == n + 1                                                           # only the read; no second PATCH
    assert A.lifecycle(cfg(), az, "delete", sleeper=sl) == {"exists": False}
    assert A.lifecycle(cfg(), az, "delete", sleeper=sl) == {"exists": False}                      # already gone: fine
    with pytest.raises(A.AzError, match="does not exist"):
        A.lifecycle(cfg(), az, "suspend", sleeper=sl)


def test_scale_rejects_unknown_sku():
    with pytest.raises(ValueError):
        A.lifecycle(cfg(), A.Az(runner=FakeAz()), "scale", "F5")


def test_waiting_for_a_state_times_out_with_the_last_state(monkeypatch):
    fake = FakeAz(rg_exists=True, cap={"sku": "F4", "state": "Provisioning"})
    ticks = iter(range(0, 10_000, 400))
    monkeypatch.setattr(A.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError, match="Provisioning"):
        A.wait_capacity(cfg(), A.Az(runner=fake), lambda s: s.get("state") == "Active", "Active", timeout=1000, sleeper=lambda s: None)


def test_config_file_overrides_defaults(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"capacity": {"name": "mycap", "admins": ["a@b.com"]}, "location": "centralindia"}))
    c = load_config(str(p), env={})
    assert c["capacity"] == {"name": "mycap", "sku": "F4", "admins": ["a@b.com"]} and c["location"] == "centralindia"
    assert c["lakehouse"]["name"] == "ContosoBankLH"


def test_config_layers_defaults_then_state_local_json_then_environment(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "local.json").write_text(json.dumps({"capacity": {"admins": ["from-file@x.com"], "sku": "F8"}, "workspace": {"name": "FromFile"}}))
    c = load_config(None, env={}, state_dir=tmp_path)
    assert c["capacity"]["admins"] == ["from-file@x.com"] and c["capacity"]["sku"] == "F8" and c["workspace"]["name"] == "FromFile"
    c = load_config(None, env={"CONTOSO_CAPACITY_ADMINS": "a@x.com; b@x.com", "CONTOSO_CAPACITY_SKU": "F2", "CONTOSO_LOCATION": "centralindia",
                               "CONTOSO_RESOURCE_GROUP": "rg-x", "CONTOSO_CAPACITY_NAME": "capx", "CONTOSO_WORKSPACE_NAME": "WS", "CONTOSO_TIER": "full"},
                    state_dir=tmp_path)
    assert c["capacity"] == {"name": "capx", "sku": "F2", "admins": ["a@x.com", "b@x.com"]}
    assert (c["location"], c["resourceGroup"], c["workspace"]["name"], c["ontology"]["tier"]) == ("centralindia", "rg-x", "WS", "full")
    assert env_overrides({}) == {}


def test_no_az_extension_is_needed():
    """The host needs only a signed-in az: no `az extension` dependency anywhere in the infra module."""
    src = (KIT / "contoso_onto" / "azure_infra.py").read_text()
    assert "fabric capacity" not in src and '"extension"' not in src
