"""build.sh / wipe.sh flows end to end against the in-process Fabric mock and a stateful fake of `az`: first build, resumable
partial failures, drift protection, ownership-driven wipe, preflight and diagnose."""
import base64
import json
import re
from pathlib import Path

import pytest

from contoso_onto import azure_infra as A, fabric_ops, oac, pipeline, runner, silver as silver_mod, spec as spec_mod
from contoso_onto.gold import load_model, physical_names
from contoso_onto.layers import load_plan
from contoso_onto.ledger import Ledger, Lock
from fake_az import FakeAz, make_cfg
from mock_fabric import MockFabric, parts_b64

ROOT = Path(__file__).resolve().parent.parent

TOKENS = {oac.FABRIC_SCOPE: "tok-fabric", fabric_ops.STORAGE_SCOPE: "tok-storage", oac.PBI_SCOPE: "tok-pbi", fabric_ops.SEARCH_SCOPE: "tok-search"}


@pytest.fixture(autouse=True, scope="module")
def no_real_network():
    """Nothing in this module reaches Azure or Fabric. The build and diagnose call the live ontology MCP endpoint: here it answers from this
    process, never from the internet. The delta-rs fallback writes to abfss://...onelake...: tests that need it replace it. In module scope, so
    the module-scoped `built_world` build is covered as well."""
    real = fabric_ops.Ops.load_layer_delta

    def guard(self, layer_dir, ws_id, lh_id, only=None, uri_base=None, storage_options=None, layer=None):
        assert uri_base, "a test would write to the real OneLake with delta-rs"
        return real(self, layer_dir, ws_id, lh_id, only, uri_base, storage_options, layer)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fabric_ops, "mcp_tools_list", lambda url, token, timeout=60: {"ok": True, "status": 200, "tools": ["ask_ontology", "list_ontology_entities", "list_ontology_rules"]})
        mp.setattr(fabric_ops.Ops, "load_layer_delta", guard)
        yield


class Clock:
    """Virtual time for the approve stage: sleeping advances it, nothing really waits."""
    def __init__(self):
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class FakeReport:
    def __init__(self, asked: int, failed=(), unavailable=None):
        self.asked, self.failed, self.unavailable = asked, list(failed), unavailable

    @property
    def ok(self) -> bool:
        return not self.failed and not self.unavailable

    def summary(self) -> str:
        return f"golden: {self.asked - len(self.failed)}/{self.asked} passed, {len(self.failed)} failed"


class FakeGolden:
    """Stands in for the ontology agent in the TEST stage."""
    def __init__(self, asked=5, failed=(), unavailable=None):
        self.asked, self.failed, self.unavailable, self.calls = asked, list(failed), unavailable, []

    def __call__(self, ws_id, ontology_id, golden, mode, parallel, say):
        self.calls.append({"ws": ws_id, "ontology": ontology_id, "questions": len(golden), "mode": mode, "parallel": parallel})
        say(f"  (fake agent) asked {self.asked}")
        return FakeReport(self.asked, self.failed, self.unavailable)


class World:
    def __init__(self, tmp_path, repo, fake=None, cfg=None):
        self.mock = MockFabric()
        self.fake = fake or FakeAz()
        self.az = A.Az(runner=self.fake, execute=True)
        self.cfg = cfg or make_cfg()
        self.repo, self.state, self.out = repo, tmp_path / "state", tmp_path / "out"
        self.logs: list[str] = []
        self.clock, self.golden = Clock(), FakeGolden()

    def say(self, m=""):
        self.logs.append(m)

    @property
    def text(self) -> str:
        return "\n".join(self.logs)

    def make_ops(self):
        fab = oac.Fabric(lambda s: TOKENS[s], api_base=f"{self.mock.base}/v1", pbi_base=f"{self.mock.base}/v1.0/myorg", log=self.say, sleeper=lambda s: None)
        return fabric_ops.Ops(fab, lambda s: TOKENS[s], self.say, sleeper=lambda s: None, dfs_base=f"{self.mock.base}/dfs")

    def kw(self):
        return dict(make_ops=self.make_ops, state_dir=self.state, out=self.out, say=self.say)

    def pipeline(self, **over):
        return runner.Pipeline(golden_fn=self.golden, clock=self.clock.now, **over)

    def up(self, **extra):
        extra.setdefault("pipeline", self.pipeline())
        return runner.up(self.cfg, self.az, repo_fn=lambda: self.repo, sleeper=self.clock.sleep, price_fetch=lambda u: {"Items": []}, **self.kw(), **extra)

    def down(self, **extra):
        return runner.down(self.cfg, self.az, sleeper=lambda s: None, **self.kw(), **extra)

    def preflight(self):
        return runner.preflight(self.cfg, self.az, repo_fn=lambda: self.repo, price_fetch=lambda u: {"Items": []}, **self.kw())

    def diagnose(self):
        return runner.diagnose(self.cfg, self.az, **self.kw())

    @property
    def ledger(self) -> Ledger:
        return Ledger(runner.ledger_path(self.state))

    def posts(self, suffix):
        return [r for r in self.mock.requests if r["method"] == "POST" and r["path"].endswith(suffix)]

    def close(self):
        self.mock.close()


@pytest.fixture()
def world(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    yield w
    w.close()


@pytest.fixture(scope="module")
def built_world(tmp_path_factory, repo_path):
    """One real first build, shared by the read-only assertions below."""
    w = World(tmp_path_factory.mktemp("w"), repo_path)
    w.rc = w.up()
    yield w
    w.close()


# ---------------------------------------------------------------------------------------------- first build
def test_first_build_creates_everything_and_records_it_in_the_ledger(built_world):
    w = built_world
    assert w.rc == 0, w.text[-2000:]
    led = w.ledger
    assert {(e["kind"], e["owned"], e["state"]) for e in led.entries} == {
        ("resourceGroup", True, "present"), ("fabricCapacity", True, "present"), ("fabricWorkspace", True, "present"), ("fabricLakehouse", True, "present"),
        ("fabricSemanticModel", True, "present"), ("fabricOntology", True, "present")}
    assert "BUILD DONE" in w.text and "RUNNING" in w.text                                          # the cost reminder is part of the output
    assert (w.out / "NEXT_STEPS.md").exists() and json.loads((w.out / ".state.json").read_text())["ontologyId"]


def test_data_and_items_landed_in_fabric(built_world):
    w = built_world
    plan = load_plan(w.out)
    assert len(w.mock.table_loads) == len(plan.all_names()) and len(w.mock.workspaces) == 1         # the core tier's bronze, silver and gold tables: not all 51 gold tables any more
    loaded = {t["table"] for t in w.mock.table_loads}                                             # the lakehouse tables carry the physical names
    registry = physical_names(load_model(ROOT / "model"))
    core_gold = {registry[t["table"]] for t in spec_mod.make_spec(load_model(ROOT / "model"), "core")["tables"]}
    assert loaded == set(plan.all_names()) and set(plan.names("gold")) == core_gold and core_gold < set(registry.values())
    assert "dim_account" in loaded and not loaded & {"account", "customer"}
    assert {n for n in loaded if n.startswith("silver_")} >= {"silver_account", silver_mod.ENTITY_CATALOG, silver_mod.ATTRIBUTE_CATALOG} and silver_mod.BRONZE_MANIFEST_TABLE in loaded
    ws = next(iter(w.mock.workspaces))
    assert w.mock.workspaces[ws]["capacityId"] == "cap-guid-1"
    ont_id = w.ledger.get("fabricOntology", "ContosoBankOntology")["id"]
    assert len(w.mock.definitions[ont_id]["parts"]) == 92                                          # the core tier ends up live
    assert w.mock.definitions[w.ledger.get("fabricSemanticModel", "ContosoBankModel")["id"]]["format"] == "TMDL"


def test_the_default_build_creates_the_final_tier_directly_and_builds_the_graph_over_rest(built_world):
    """The smoke-tier detour is gone: the core import was proven live. The only definition update is the graph model's."""
    w = built_world
    created = [json.loads(r["body"]) for r in w.posts("/ontologies")]
    assert len(created) == 1 and len(created[0]["definition"]["parts"]) == 92                      # the 31-entity core tier, created once
    updates = [r["path"] for r in w.mock.requests if r["path"].endswith("/updateDefinition")]
    assert len(updates) == 1 and "/graphModels/" in updates[0]
    assert "smoke tier" not in w.text and "graph: refreshed and queryable (31 node types, 53 edge types)" in w.text


def _ontology_table_parts(world):
    ont_id = world.ledger.get("fabricOntology", "ContosoBankOntology")["id"]
    parts = {p["path"]: base64.b64decode(p["payload"]).decode("utf-8") for p in world.mock.definitions[ont_id]["parts"]}
    return [t for path, t in parts.items() if path.startswith("tables/")]


def test_the_ontology_is_built_with_the_graph_annotations_by_default(built_world):
    """Live finding (16 Oct 2026): without the eight ONT_* partition annotations every entity is graph-ineligible; with them all were eligible."""
    w = built_world
    tables = _ontology_table_parts(w)
    assert len(tables) == 31 and all(len(re.findall(r"annotation ONT_", t)) == 8 for t in tables)
    assert "annotation ONT_ItemKind = Lakehouse" in tables[0] and "graph-ineligible" not in w.text
    assert "built and refreshed by this kit over REST" in (w.out / "NEXT_STEPS.md").read_text(encoding="utf-8")


def test_the_graph_annotations_can_be_left_out(world):
    assert world.up(ont_annotations=False) == 0, world.text[-1500:]
    tables = _ontology_table_parts(world)
    assert len(tables) == 31 and not any("annotation ONT_" in t for t in tables)
    assert "graph-ineligible" in world.text and "--no-ont-annotations" in (world.out / "NEXT_STEPS.md").read_text(encoding="utf-8")


def test_the_pin_stamp_stays_the_same_across_runs_and_is_taken_over_from_the_live_ontology(tmp_path, repo_path):
    """ONT_PinnedAtUtc is part of the definition: a moving stamp would make every re-run replace the ontology."""
    w = World(tmp_path, repo_path)
    try:
        assert w.up() == 0
        state_file = w.out / ".state.json"
        stamp = json.loads(state_file.read_text())["pinnedAtUtc"]
        assert stamp and all(f"annotation ONT_PinnedAtUtc = {stamp}" in t for t in _ontology_table_parts(w))
        n_update = len([r for r in w.mock.requests if r["path"].endswith("/updateDefinition")])
        st = json.loads(state_file.read_text())
        st.pop("pinnedAtUtc")                                                                       # the state file is lost, the ontology is still live
        state_file.write_text(json.dumps(st))
        assert w.up() == 0
        assert json.loads(state_file.read_text())["pinnedAtUtc"] == stamp                           # taken over from the live definition
        assert len([r for r in w.mock.requests if r["path"].endswith("/updateDefinition")]) == n_update
    finally:
        w.close()


def test_capacity_was_created_by_arm_with_a_validated_template(built_world):
    kinds = [tuple(c[:3]) for c in built_world.fake.calls]
    assert kinds.index(("deployment", "group", "validate")) < kinds.index(("deployment", "group", "create"))
    assert built_world.fake.cap == {"sku": "F4", "state": "Active"}


def test_the_build_asked_for_f4_and_both_azure_and_fabric_confirmed_it(built_world):
    w = built_world
    assert w.fake.requested_skus == ["F4"]                                                          # the parameter file the deployment used said F4
    assert "capacity contosofabricf4: F4 Active" in w.text                                          # Azure's answer
    assert "runs on capacity contosofabricf4 (F4, Active), as Fabric reports it" in w.text         # Fabric's answer, and the workspace is on it
    assert "capacity  : contosofabricf4 (F4) RUNNING" in w.text                                    # the closing summary names the size
    assert "Capacity: contosofabricf4 (F4)" in (w.out / "NEXT_STEPS.md").read_text(encoding="utf-8")


def test_agent_pack_carries_the_real_workspace_and_ontology_ids(built_world):
    w = built_world
    ids = json.loads((w.out / ".state.json").read_text())
    texts = [p.read_text(encoding="utf-8") for p in (w.out / "core" / "agents").rglob("*") if p.is_file()]
    blob = "\n".join(texts)
    assert "<WORKSPACE_ID>" not in blob and "<ONTOLOGY_ID>" not in blob
    assert ids["workspaceId"] in blob and ids["ontologyId"] in blob
    assert len(json.loads((w.out / "eval" / "golden_expected.json").read_text())) == 29


def test_ledger_has_no_secrets(built_world):
    raw = (runner.ledger_path(built_world.state)).read_text()
    assert "tok-" not in raw and "Bearer" not in raw


# ---------------------------------------------------------------------------------------------- re-runs and drift
def test_rerun_changes_nothing_and_portal_edits_are_never_overwritten(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    try:
        assert w.up() == 0
        n_create, n_update = len(w.posts("/ontologies")), len([r for r in w.mock.requests if r["path"].endswith("/updateDefinition")])
        w.logs.clear()
        assert w.up() == 0
        assert len(w.posts("/ontologies")) == n_create and len([r for r in w.mock.requests if r["path"].endswith("/updateDefinition")]) == n_update
        assert w.text.count(": unchanged") == 3 and len(w.mock.workspaces) == 1, [ln for ln in w.logs if "unchanged" in ln]      # idempotent: model, ontology, graph

        ont_id = w.ledger.get("fabricOntology", "ContosoBankOntology")["id"]
        live = w.mock.definitions[ont_id]
        live["parts"].append(parts_b64({"metrics/DelinquencyRate.tmdl": "metric DelinquencyRate\n"})["parts"][0])      # somebody adds a metric in the portal
        w.logs.clear()
        assert w.up() == 0
        assert "portal edits" in w.text and len(w.mock.definitions[ont_id]["parts"]) == 93         # left alone
        assert len([r for r in w.mock.requests if r["path"].endswith("/updateDefinition")]) == n_update

        w.logs.clear()
        assert w.up(force_replace=True) == 0
        assert len(w.mock.definitions[ont_id]["parts"]) == 92 and "backed up" in w.text
        backups = list((w.out / "backup" / "fabricOntology").glob("*/metrics/DelinquencyRate.tmdl"))
        assert backups and "DelinquencyRate" in backups[0].read_text()                              # the portal work is recoverable
    finally:
        w.close()


def test_items_not_created_by_this_kit_are_left_alone_unless_forced(world):
    ws = world.make_ops().fab.call("POST", f"{world.mock.base}/v1/workspaces", json={"displayName": "W"}).json()["id"]
    fab = world.make_ops().fab
    mine = {"a.tmdl": "x"}
    led = Ledger(runner.ledger_path(world.state))
    theirs = fab.create(ws, "ontologies", {"displayName": "Existing", "definition": oac.to_payload({"b.tmdl": "theirs"})})
    r = runner.converge_item(fab, led, ws, "ontologies", "fabricOntology", "Existing", mine, None, say=world.say)
    assert r["action"] == "kept" and "not created by this kit" in r["reason"] and led.get("fabricOntology", "Existing") is None
    r = runner.converge_item(fab, led, ws, "ontologies", "fabricOntology", "Existing", mine, None, force=True, backup_dir=world.out / "bk", say=world.say)
    assert r["action"] == "replaced" and r["id"] == theirs and led.get("fabricOntology", "Existing")["owned"] is False   # replaced on request, still never wiped
    assert list((world.out / "bk" / "fabricOntology").glob("*/b.tmdl"))


def test_hash_ignores_platform_whitespace_and_line_endings():
    a = {".platform": "one", "x.tmdl": "line1\r\nline2   \r\n\r\n"}
    b = {".platform": "two", "x.tmdl": "line1\nline2"}
    assert runner.hash_parts(a) == runner.hash_parts(b)
    assert runner.hash_parts(a) != runner.hash_parts({"x.tmdl": "line1\nline3"})


# ---------------------------------------------------------------------------------------------- failures
def test_a_failure_midway_is_partial_resumable_and_already_in_the_ledger(world, monkeypatch):
    boom = {"on": True}
    real_api, real_delta = fabric_ops.Ops.load_layer, fabric_ops.Ops.load_layer_delta

    def flaky(real):
        def inner(self, *a, **k):
            if boom["on"]:
                raise RuntimeError("Load Table API said no")
            return real(self, *a, **k)
        return inner
    monkeypatch.setattr(fabric_ops.Ops, "load_layer", flaky(real_api))
    monkeypatch.setattr(fabric_ops.Ops, "load_layer_delta", flaky(real_delta))
    assert world.up() == runner.EXIT_PARTIAL
    assert "PARTIAL" in world.text and "wipe.sh" in world.text
    assert {e["kind"] for e in world.ledger.owned()} == {"resourceGroup", "fabricCapacity", "fabricWorkspace", "fabricLakehouse"}      # nothing leaked
    boom["on"] = False
    world.logs.clear()
    assert world.up() == 0                                                                          # re-run resumes
    assert len(world.mock.workspaces) == 1 and len(world.mock.table_loads) == len(load_plan(world.out).all_names())


def test_a_table_the_load_api_refuses_falls_back_to_the_delta_writer_for_that_table_only(world, monkeypatch, tmp_path):
    seen = []
    world.mock.load_rejections = {silver_mod.BRONZE_MANIFEST_TABLE: "400"}                          # the API says no to this one table's file

    def fake_delta(self, folder, ws, lh, only=None, uri_base=None, storage_options=None, layer=None):
        seen.append((layer, sorted(only)))
        self.fab.call("POST", f"{self.fab.api}/workspaces/{ws}/lakehouses/{lh}/tables/x/load", json={"relativePath": "x"})     # registers a table in the mock
        return {"loaded": [], "missingAfterLoad": []}
    monkeypatch.setattr(fabric_ops.Ops, "load_layer_delta", fake_delta)
    rc = world.up()
    assert rc == runner.EXIT_PARTIAL and "the Load Table API refused 1 of the 1 bronze tables" in world.text                  # the fallback ran for the first layer
    assert seen == [("bronze", [silver_mod.BRONZE_MANIFEST_TABLE])]                                # and stopped there: the table it wrote is not the one that is listed
    assert "not registered in the lakehouse yet" in (world.state / "runs" / "last-error.txt").read_text()


def test_throttling_never_falls_back_to_the_delta_writer_and_the_rerun_resumes(world, monkeypatch):
    monkeypatch.setattr(fabric_ops.Ops, "load_layer_delta", lambda self, *a, **k: pytest.fail("delta-rs was used for a failure that is not about the table"))
    world.mock.faults = [{"match": rf"/tables/{silver_mod.BRONZE_MANIFEST_TABLE}/load$", "method": "POST", "status": 429, "retry_after": 1, "count": 10**6}]
    assert world.up() == runner.EXIT_PARTIAL
    assert "ERROR the bronze layer stopped at table" in world.text and "the cause is the moment" in world.text and "PARTIAL" in world.text
    assert {e["kind"] for e in world.ledger.owned()} == {"resourceGroup", "fabricCapacity", "fabricWorkspace", "fabricLakehouse"}      # nothing leaked
    world.mock.faults.clear()
    world.logs.clear()
    assert world.up() == 0, world.text[-2000:]
    assert len(world.mock.table_loads) == len(load_plan(world.out).all_names())


def test_throttling_across_the_whole_silver_layer_stops_after_a_handful_of_tables_and_the_rerun_resumes(world, monkeypatch):
    monkeypatch.setattr(fabric_ops.Ops, "load_layer_delta", lambda self, *a, **k: pytest.fail("delta-rs was used for a failure that is not about the table"))
    world.mock.faults = [{"match": r"/tables/silver_[a-z0-9_]+/load$", "method": "POST", "status": 429, "retry_after": 30, "count": 10**6}]
    assert world.up() == runner.EXIT_PARTIAL
    silver = load_plan(world.out).names("silver")
    posts = [r for r in world.posts("/load") if "/tables/silver_" in r["path"]]
    assert len({r["path"] for r in posts}) <= 12 < len(silver) and len(posts) <= 12 * 6                # a handful of tables, one call ladder each: not every table, not every ladder five times over
    assert "loads in a row failed for a reason that is not about the tables" in world.text and "ERROR the silver layer stopped at table silver_" in world.text
    assert "gold was not started" in world.text and "the cause is the moment" in world.text and "PARTIAL" in world.text
    assert {e["kind"] for e in world.ledger.owned()} == {"resourceGroup", "fabricCapacity", "fabricWorkspace", "fabricLakehouse"}      # nothing leaked
    world.mock.faults.clear()
    world.logs.clear()
    assert world.up() == 0, world.text[-2000:]
    assert "load bronze: unchanged since its last load" in world.text                                   # the finished layer is not loaded again
    assert len(world.mock.table_loads) == len(load_plan(world.out).all_names())


def test_not_signed_in_is_blocked_before_anything_happens(tmp_path, repo_path):
    class NoLogin(FakeAz):
        def __call__(self, args):
            if args[:2] == ["account", "show"]:
                return 1, "", "Please run 'az login' to setup account."
            return super().__call__(args)
    w = World(tmp_path, repo_path, NoLogin())
    try:
        assert w.up() == runner.EXIT_BLOCKED and "never does" in w.text
        assert not [c for c in w.fake.calls if c[:2] in (["group", "create"],)] and not w.mock.requests
    finally:
        w.close()


def test_bad_config_and_lock_contention_have_their_own_exit_codes(world):
    world.cfg["capacity"]["sku"] = "F5"
    assert world.up() == runner.EXIT_BAD_CONFIG
    world.cfg["capacity"]["sku"] = "F4"
    with Lock(runner.lock_path(world.state)):
        assert world.up() == runner.EXIT_LOCKED and world.down() == runner.EXIT_LOCKED
    assert "BUSY" in world.text


def test_another_size_than_f4_is_refused_before_anything_happens(world):
    world.cfg["capacity"]["sku"] = "F8"
    assert world.up() == runner.EXIT_BAD_CONFIG
    assert "not the F4 this demo is built for" in world.text and "CONTOSO_ALLOW_OTHER_SKU=1" in world.text
    assert world.preflight() == runner.EXIT_BAD_CONFIG and "[FAIL] config" in world.text
    assert not world.fake.calls and not world.mock.requests                                         # not even a read: refused up front


def test_service_principal_login_without_capacity_admins_is_blocked(tmp_path, repo_path):
    w = World(tmp_path, repo_path, FakeAz(user_type="servicePrincipal"))
    try:
        assert w.up() == runner.EXIT_BLOCKED and "CONTOSO_CAPACITY_ADMINS" in w.text
    finally:
        w.close()


def test_unregistered_provider_is_registered_by_the_build(tmp_path, repo_path):
    w = World(tmp_path, repo_path, FakeAz(provider="NotRegistered"))
    try:
        assert w.up() == 0
        assert ["provider", "register", "-n", "Microsoft.Fabric", "--wait"] in w.fake.calls
    finally:
        w.close()


def test_a_paused_capacity_is_resumed_by_the_next_build_and_pause_after_stops_billing(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    try:
        assert w.up(pause_after=True) == 0
        assert w.fake.cap["state"] == "Paused" and "PAUSED" in w.text
        w.logs.clear()
        assert w.up() == 0                                                                           # resumes inside the group this kit created
        assert ("post", "resume") in w.fake.methods() and w.fake.cap["state"] == "Active"
    finally:
        w.close()


def test_a_build_refuses_a_resource_group_it_did_not_create(tmp_path, repo_path):
    w = World(tmp_path, repo_path, FakeAz(rg_exists=True, cap={"sku": "F4", "state": "Active"}, rg_tags={"owner": "someone-else"}))
    try:
        assert w.up() == runner.EXIT_BLOCKED
        assert "brand-new resource group" in w.text and "CONTOSO_RESOURCE_GROUP" in w.text
        assert not [c for c in w.fake.calls if c[:2] == ["group", "create"] or c[:3] == ["deployment", "group", "create"]]
        assert w.ledger.entries == [] and not w.mock.requests                                         # nothing recorded, nothing created in Fabric
        assert w.fake.rg_exists and w.fake.cap == {"sku": "F4", "state": "Active"}                    # their group and capacity are untouched
    finally:
        w.close()


def test_a_build_stops_with_clear_steps_when_the_tenant_does_not_allow_ontology_items(tmp_path, repo_path):
    """Live finding: with the tenant setting off Fabric answers 403 FeatureNotAvailable to every ontology create. With --no-gate-wait the build says what to
    enable (and where, for this capacity), keeps everything it built, and a re-run after the administrator enabled it resumes and finishes."""
    w = World(tmp_path, repo_path)
    w.mock.ontology_forbidden = True
    try:
        assert w.up(pipeline=w.pipeline(gate_timeout_min=0)) == runner.EXIT_BLOCKED
        assert "Users can create ontology (preview) items" in w.text and "docs/MANUAL_STEPS.md" in w.text and "re-run bash build.sh" in w.text
        assert "PARTIAL" not in w.text and "403" in w.text
        assert "/admin-portal/capacities/cap-guid-1/delegatedSettings" in w.text                       # the direct link for THIS capacity
        assert w.ledger.get("fabricSemanticModel", "ContosoBankModel")["state"] == "present"          # the semantic model was built before the ontology step
        assert w.fake.cap is not None and len(w.mock.workspaces) == 1                                 # nothing was torn down
        w.mock.ontology_forbidden = False                                                             # the administrator enables the setting
        w.logs.clear()
        assert w.up() == 0, w.text[-1500:]
        assert w.ledger.get("fabricOntology", "ContosoBankOntology")["state"] == "present" and "BUILD DONE" in w.text
    finally:
        w.close()


def test_the_approve_stage_asks_waits_fifteen_minutes_then_polls_and_carries_on_by_itself(tmp_path, repo_path):
    """The pipeline's one manual step: ask (link + both settings), wait for them to propagate, then poll every minute until ontology creation works."""
    w = World(tmp_path, repo_path)
    w.mock.ontology_forbidden = True
    stamps: list[float] = []

    def open_after_three_polls(mock):
        stamps.append(w.clock.now())
        if len(stamps) == 5:                                                                           # the probe + 4 polls were refused; the admin's settings have propagated
            mock.ontology_forbidden = False
    w.mock.on_forbidden_create = open_after_three_polls
    try:
        assert w.up() == 0, w.text[-2000:]
        assert "STAGE 2/4  APPROVE" in w.text and "ACTION NEEDED" in w.text
        assert "https://app.fabric.microsoft.com/admin-portal/capacities/cap-guid-1/delegatedSettings?experience=power-bi" in w.text
        assert "Users can create Ontology (preview) items" in w.text and "Users can use Copilot and other features powered by Azure OpenAI" in w.text
        assert "processed outside" not in w.text                                                       # eastus2 needs no cross-geo setting
        assert stamps[1] - stamps[0] == 15 * 60                                                         # the immediate probe, then the first poll after the 15 minutes
        assert [b - a for a, b in zip(stamps[1:], stamps[2:])] == [60, 60, 60]                         # then once a minute
        assert "gate: OPEN after" in w.text
        approval = (w.out / "ADMIN_APPROVAL.md").read_text(encoding="utf-8")
        assert approval.count("delegatedSettings") == 1
        assert f"with {load_plan(w.out).summary()} (bronze, silver and gold layers" in approval and "bind to gold only" in approval       # what is already built, by layer
        assert w.ledger.get("fabricOntology", "ContosoBankOntology")["state"] == "present"
        assert "STAGE 3/4  DEPLOY" in w.text and "STAGE 4/4  TEST" in w.text                           # and the later stages ran in the same run
    finally:
        w.close()


def test_the_approve_stage_lists_the_cross_geo_setting_outside_the_us_and_eu(tmp_path, repo_path):
    w = World(tmp_path, repo_path, cfg=make_cfg(location="southindia"))
    w.mock.ontology_forbidden = True
    try:
        assert w.up(pipeline=w.pipeline(gate_timeout_min=1)) == runner.EXIT_BLOCKED                   # the gate stays closed: only the request matters here
        assert "Data sent to Azure OpenAI can be processed outside" in w.text and "southindia is outside the US and EU" in w.text
    finally:
        w.close()


def test_the_approve_stage_gives_up_after_the_timeout_keeps_everything_and_a_rerun_resumes(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    w.mock.ontology_forbidden = True
    try:
        assert w.up(pipeline=w.pipeline(gate_wait_min=2, gate_poll_sec=30, gate_timeout_min=10)) == runner.EXIT_BLOCKED
        assert "still closed after 10 min" in w.text and "re-run bash build.sh" in w.text and "Override tenant admin selection" in w.text
        assert w.clock.now() >= 600 and w.fake.cap is not None and w.ledger.get("fabricSemanticModel", "ContosoBankModel")["state"] == "present"
        w.mock.ontology_forbidden = False
        w.logs.clear()
        assert w.up() == 0, w.text[-1500:]                                                             # resumes: nothing is rebuilt, the gate is open
        assert "gate: open" in w.text and len(w.mock.table_loads) == len(load_plan(w.out).all_names())            # nothing was loaded twice
    finally:
        w.close()


def test_until_stops_after_the_named_stage_and_the_next_run_continues(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    try:
        assert w.up(pipeline=w.pipeline(until="build")) == 0
        assert "STOPPED after the build stage" in w.text and "STAGE 2/4" not in w.text
        assert w.ledger.get("fabricSemanticModel", "ContosoBankModel") and w.ledger.get("fabricOntology", "ContosoBankOntology") is None
        w.logs.clear()
        assert w.up(pipeline=w.pipeline(until="approve")) == 0
        assert "STOPPED after the approve stage" in w.text and "STAGE 3/4" not in w.text and w.ledger.get("fabricOntology", "ContosoBankOntology")["state"] == "present"
        w.logs.clear()
        assert w.up() == 0 and "graph: refreshed and queryable" in w.text and "BUILD DONE" in w.text
        assert w.up(pipeline=w.pipeline(until="nonsense")) == runner.EXIT_BAD_CONFIG
    finally:
        w.close()


def test_the_graph_is_built_over_rest_refreshed_and_left_alone_on_a_rerun(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    try:
        assert w.up() == 0, w.text[-1500:]
        gm_id = next(iter(w.mock.items[(next(iter(w.mock.workspaces)), "graphModels")]))
        graph_type = json.loads(base64.b64decode(next(p["payload"] for p in w.mock.definitions[gm_id]["parts"] if p["path"] == "graphType.json")))
        assert len(graph_type["nodeTypes"]) == 31 and len(graph_type["edgeTypes"]) == 53
        assert "graph: refreshed and queryable (31 node types, 53 edge types)" in w.text
        st = json.loads((w.out / ".state.json").read_text())
        assert st["graphModelId"] == gm_id and st["graphHash"]
        n_updates = len([r for r in w.mock.requests if r["path"].endswith("/updateDefinition")])
        w.logs.clear()
        assert w.up() == 0
        assert "graph: unchanged (31 node types, 53 edge types)" in w.text
        assert len([r for r in w.mock.requests if r["path"].endswith("/updateDefinition")]) == n_updates     # no second write, no second refresh
        steps = (w.out / "NEXT_STEPS.md").read_text(encoding="utf-8")
        assert "built and refreshed by this kit over REST (31 node types, 53 edge types)" in steps
    finally:
        w.close()


def test_a_graph_that_fails_is_reported_and_the_rest_still_runs(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    w.mock.graph_update_error = "InvalidDefinition"
    try:
        assert w.up() == runner.EXIT_PARTIAL
        assert "ERROR graph:" in w.text and "InvalidDefinition" in w.text and "Configure graph" in w.text
        assert w.ledger.get("fabricOntology", "ContosoBankOntology")["state"] == "present"            # the ontology is fine
        assert "golden questions: skipped (the graph step failed" in w.text and (w.out / "NEXT_STEPS.md").exists()
        w.mock.graph_update_error = None
        w.logs.clear()
        assert w.up() == 0 and "graph: refreshed and queryable" in w.text                              # a re-run retries just the graph
    finally:
        w.close()


def test_no_graph_leaves_configure_graph_to_the_portal(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    try:
        assert w.up(pipeline=w.pipeline(graph=False)) == 0
        assert "graph: skipped (--no-graph)" in w.text
        assert not [r for r in w.mock.requests if "/graphModels/" in r["path"] and r["path"].endswith("/updateDefinition")]
        steps = (w.out / "NEXT_STEPS.md").read_text(encoding="utf-8")
        assert "The graph was not built by this run" in steps and "Manage graph" in steps
    finally:
        w.close()


def test_the_test_stage_asks_the_agent_and_reports_without_failing_the_build(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    w.golden = FakeGolden(asked=5, failed=["G03"])
    try:
        assert w.up() == 0
        assert w.golden.calls == [{"ws": next(iter(w.mock.workspaces)), "ontology": w.ledger.get("fabricOntology", "ContosoBankOntology")["id"], "questions": 27, "mode": "smoke", "parallel": 2}]      # core tier: the 2 extended questions are not asked
        assert "golden: 4/5 passed, 1 failed" in w.text and "agent     : golden: 4/5 passed" in w.text
        assert "Agent test (TEST stage): golden: 4/5 passed" in (w.out / "NEXT_STEPS.md").read_text(encoding="utf-8")
        w.logs.clear()
        assert w.up(pipeline=w.pipeline(strict_golden=True)) == runner.EXIT_PARTIAL and "--strict-golden is set" in w.text
        w.logs.clear()
        assert w.up(pipeline=w.pipeline(golden="none")) == 0 and "golden questions: skipped (--golden none)" in w.text
        assert w.up(pipeline=w.pipeline(golden="sometimes")) == runner.EXIT_BAD_CONFIG
    finally:
        w.close()


def test_an_agent_that_is_not_enabled_is_reported_with_its_reason(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    w.golden = FakeGolden(asked=1, failed=["G01"], unavailable="the ontology agent is disabled for this capacity (403): enable the Copilot delegated setting")
    try:
        assert w.up() == 0
        assert "note: the ontology agent is disabled for this capacity" in w.text
    finally:
        w.close()


def test_the_tables_are_not_loaded_again_while_the_lakehouse_holds_the_same_data(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    try:
        assert w.up() == 0
        plan = load_plan(w.out)
        n, uploads = len(plan.all_names()), len([r for r in w.mock.requests if r["method"] == "PUT" and "/Files/bronze/" in r["path"]])
        assert len(w.mock.table_loads) == n and uploads == len(plan.files) > 0
        w.logs.clear()
        assert w.up() == 0
        assert len(w.mock.table_loads) == n and f"already holds these {n} tables and {len(plan.files)} bronze files" in w.text            # the 4-minute load is skipped
        assert len([r for r in w.mock.requests if r["method"] == "PUT" and "/Files/bronze/" in r["path"]]) == uploads                   # and the seed files are not uploaded again
        assert w.up(pipeline=w.pipeline(reload=True)) == 0 and len(w.mock.table_loads) == 2 * n                                         # --reload loads every layer again
    finally:
        w.close()


def own_the_group(w, run_id="run-1"):
    """As after an earlier build that created the resource group."""
    w.ledger.record("resourceGroup", w.cfg["resourceGroup"], owned=True, state="present", subscriptionId="sub-1", location="southindia", runId=run_id)


def test_a_build_stops_when_the_capacity_is_not_the_f4_it_is_configured_for(tmp_path, repo_path):
    w = World(tmp_path, repo_path, FakeAz(rg_exists=True, cap={"sku": "F8", "state": "Active"}, rg_tags={A.RUN_TAG: "run-1"}))
    try:
        own_the_group(w)
        assert w.up() == runner.EXIT_BLOCKED
        assert "is F8, but this build is configured for F4" in w.text and "scale --sku F4" in w.text
        assert w.fake.cap == {"sku": "F8", "state": "Active"} and not w.fake.bodies                    # the kit never resizes a capacity by itself
        assert not w.mock.requests                                                                    # nothing was put on the wrong size
    finally:
        w.close()


def test_a_build_stops_when_fabric_reports_the_capacity_as_something_other_than_f4(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    w.mock.capacity_sku = "F8"
    try:
        assert w.up() == runner.EXIT_BLOCKED
        assert "Fabric reports capacity contosofabricf4 as F8, but this build is configured for F4" in w.text and "scale --sku F4" in w.text
        assert not w.mock.workspaces                                                                  # no workspace was created on the wrong size
        assert w.ledger.get("fabricCapacity", "contosofabricf4")["owned"] is True                    # the capacity exists, so wipe deletes it
    finally:
        w.close()


# ---------------------------------------------------------------------------------------------- wipe
def test_wipe_deletes_what_the_ledger_owns_and_verifies_it(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    try:
        assert w.up() == 0
        assert w.down(plan_only=True) == 0 and w.fake.cap is not None and len(w.mock.workspaces) == 1      # --plan deletes nothing
        assert "hard delete of the whole group" in w.text and "hard-deleted first" in w.text
        w.logs.clear()
        assert w.down() == 0, w.text
        assert w.fake.cap is None and w.fake.rg_exists is False and not w.mock.workspaces                  # no resource group left
        assert "VERIFIED" in w.text and "no resource group" in w.text and "HostVM" in w.text
        assert all(e["state"] == "deleted" for e in w.ledger.entries) and list((w.state / "tombstones").glob("ledger-*.json"))
        assert not (w.out / ".state.json").exists()                                                  # no stale workspace id left for a later deploy
        w.logs.clear()
        assert w.down() == 0 and "nothing" in w.text                                                 # idempotent
    finally:
        w.close()


def test_wipe_hard_deletes_every_item_and_leaves_only_an_empty_renamed_workspace_shell(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    try:
        assert w.up() == 0
        ws_id = next(iter(w.mock.workspaces))
        w.mock.recycle[ws_id] = {"old-1": {"id": "old-1", "displayName": "AnEarlierLakehouse", "type": "Lakehouse", "workspaceId": ws_id}}   # already soft-deleted
        w.mock.requests.clear()
        w.logs.clear()
        assert w.down() == 0, w.text
        deletes = [r for r in w.mock.requests if r["method"] == "DELETE"]
        hard = [r for r in deletes if "/items/" in r["path"] and r["query"].get("hardDelete") == ["true"]]
        assert {h["type"] for h in w.mock.hard_deleted} == {"Ontology", "GraphModel", "SemanticModel", "Lakehouse"}      # every kit item is permanently gone
        assert len(hard) >= 3 and not w.mock.recycle.get(ws_id)                                       # nothing sits in the recycle bin
        assert [i["displayName"] for i in w.mock.purged] == ["AnEarlierLakehouse"]                    # the pre-existing recycle-bin item was purged too
        shell = w.mock.deleted_workspaces[ws_id]
        assert shell["displayName"].startswith("zz-wiped-") and shell["itemsLeft"] == 0                # empty and renamed: the name is free again
        assert w.mock.renames and w.mock.renames[-1][0] == ws_id
        assert "NOTE Fabric has no API that permanently deletes a workspace" in w.text and "Permanently delete" in w.text
        assert "Azure has no soft delete" in w.text
    finally:
        w.close()


def test_items_are_deleted_dependants_first_and_a_type_without_soft_delete_falls_back(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    try:
        assert w.up() == 0
        assert w.mock.workspaces
        w.mock.requests.clear()
        assert w.down() == 0
        kinds = [h["type"] for h in w.mock.hard_deleted]
        assert kinds == ["Ontology", "GraphModel", "SemanticModel", "Lakehouse"]                       # ontology, its graph, the model, then the lakehouse
        sm = [r for r in w.mock.requests if r["method"] == "DELETE" and "/items/" in r["path"]]
        assert any(r["query"].get("hardDelete") == ["true"] for r in sm) and any(not r["query"] for r in sm)   # semantic model: hard attempt rejected, plain delete is permanent
        assert not any("sql-" in r["path"] for r in sm)                                               # the SQL endpoint child goes with its lakehouse
    finally:
        w.close()


def test_wipe_keeps_an_adopted_workspace_and_whatever_it_holds_that_the_kit_did_not_create(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    try:
        pre = w.make_ops().fab.call("POST", f"{w.mock.base}/v1/workspaces", json={"displayName": w.cfg["workspace"]["name"], "capacityId": "cap-guid-1"}).json()["id"]
        other = w.make_ops().fab.create(pre, "lakehouses", {"displayName": "SomebodyElsesLakehouse"})
        assert w.up() == 0
        kinds = {(e["kind"], e["owned"]) for e in w.ledger.entries}
        assert ("resourceGroup", True) in kinds and ("fabricCapacity", True) in kinds                  # the group and capacity are always brand-new
        assert ("fabricWorkspace", False) in kinds and ("fabricOntology", True) in kinds and ("fabricLakehouse", True) in kinds
        w.logs.clear()
        assert w.down() == 0, w.text
        assert "KEEP" in w.text and w.fake.rg_exists is False and w.fake.cap is None                   # Azure: everything the kit made is gone
        assert pre in w.mock.workspaces and other in w.mock.items[(pre, "lakehouses")]                  # their workspace and their lakehouse survive
        assert not w.mock.items[(pre, "ontologies")] and not w.mock.items[(pre, "semanticModels")]      # what the kit created inside it is gone
        assert not w.mock.items[(pre, "graphModels")]                                                  # and so is the graph model Fabric created next to the ontology
        assert w.make_ops().fab.find(pre, "lakehouses", w.cfg["lakehouse"]["name"]) is None
        assert {h["displayName"] for h in w.mock.hard_deleted} >= {w.cfg["lakehouse"]["name"], "ContosoBankOntology"}      # permanently, not into the recycle bin
    finally:
        w.close()


def test_the_resource_group_is_deleted_whole_and_foreign_resources_are_named_first(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    try:
        assert w.up() == 0
        w.fake.foreign = [{"name": "stray-storage", "type": "Microsoft.Storage/storageAccounts"}, {"name": "stray-kv", "type": "Microsoft.KeyVault/vaults"}]
        assert w.down() == 0, w.text
        assert w.fake.rg_exists is False and w.fake.cap is None                                       # no resource group left, whatever was in it
        assert "2 other resource(s) inside" in w.text and "Microsoft.Storage/storageAccounts/stray-storage" in w.text
        assert w.ledger.get("resourceGroup", w.cfg["resourceGroup"])["state"] == "deleted"
    finally:
        w.close()


def test_wipe_does_not_delete_a_resource_group_that_is_not_the_one_it_created(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    try:
        assert w.up() == 0
        w.fake.rg_tags[A.RUN_TAG] = "somebody-elses-group"                                            # the name now belongs to a different group
        w.logs.clear()
        assert w.down() == runner.EXIT_WIPE_FAILED
        assert "not the group this kit created" in w.text
        assert w.fake.rg_exists and w.fake.cap is not None                                            # nothing in Azure was touched
        assert not [m for m in w.fake.methods() if m[0] == "delete"] and ["group", "delete"] not in [c[:2] for c in w.fake.calls]
        assert not w.mock.workspaces                                                                  # the Fabric workspace is ours and is purged
    finally:
        w.close()


def test_wipe_resumes_a_paused_capacity_before_it_purges_fabric(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    try:
        assert w.up(pause_after=True) == 0 and w.fake.cap["state"] == "Paused"
        w.logs.clear()
        assert w.down() == 0, w.text
        assert "resuming it so the Fabric items can be deleted" in w.text and ("post", "resume") in w.fake.methods()
        assert w.fake.cap is None and w.fake.rg_exists is False
    finally:
        w.close()


def test_a_purge_that_cannot_finish_is_reported_but_billing_still_stops(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    try:
        assert w.up() == 0
        stuck = w.ledger.get("fabricOntology", "ContosoBankOntology")["id"]
        w.mock.stuck_item_ids.add(stuck)                                                              # Fabric refuses to delete this one
        w.logs.clear()
        assert w.down() == runner.EXIT_WIPE_FAILED
        assert "hard purge incomplete" in w.text and "Ontology:ContosoBankOntology" in w.text
        assert w.fake.cap is None and w.fake.rg_exists is False                                       # the capacity and group are gone regardless
        shell = next(iter(w.mock.deleted_workspaces.values()))
        assert shell["itemsLeft"] == 1 and shell["displayName"].startswith("zz-wiped-")
    finally:
        w.close()


def test_a_second_build_after_a_wipe_works_because_the_retained_shell_was_renamed(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    try:
        assert w.up() == 0
        first_run = w.ledger.get("resourceGroup", w.cfg["resourceGroup"])["details"]["runId"]
        assert w.down() == 0
        w.logs.clear()
        assert w.up() == 0, w.text                                                                    # a retained shell would reserve the name and answer 409
        assert len(w.mock.workspaces) == 1 and len(w.mock.deleted_workspaces) == 1
        assert w.ledger.get("resourceGroup", w.cfg["resourceGroup"])["details"]["runId"] != first_run   # a brand-new group with its own run id
        assert w.fake.rg_exists and w.fake.cap == {"sku": "F4", "state": "Active"}
    finally:
        w.close()


def test_wipe_keeps_going_after_a_failure_and_exits_5_then_converges(tmp_path, repo_path):
    w = World(tmp_path, repo_path, FakeAz(fail_delete=True))
    try:
        assert w.up() == 0
        assert w.down() == runner.EXIT_WIPE_FAILED
        assert not w.mock.workspaces                                                                   # the workspace delete still happened
        assert "WIPE INCOMPLETE" in w.text and "capacity is busy" in w.text
        assert w.fake.rg_exists is False                                                              # deleting the group removed the capacity regardless
        w.fake.fail_delete = False
        w.logs.clear()
        assert w.down() == 0 and w.fake.cap is None and "VERIFIED" in w.text                          # the re-run converges
    finally:
        w.close()


def test_wipe_ends_with_an_audit_of_the_fabric_capacities_that_remain(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    try:
        assert w.up() == 0
        w.logs.clear()
        assert w.down() == 0, w.text
        assert "audit: no Fabric capacity is left in this subscription" in w.text
    finally:
        w.close()


def test_the_audit_names_an_older_f4_it_did_not_create_and_never_deletes_it(tmp_path, repo_path):
    """An older F4 from some other run or host is not in this ledger: it is named (it bills while Active), never deleted automatically."""
    w = World(tmp_path, repo_path)
    w.fake.other_caps = [{"name": "contosofabricf4", "resourceGroup": "rg-older", "location": "southindia", "sku": {"name": "F4"}},
                         {"name": "teamcap", "resourceGroup": "rg-team", "location": "westus3", "sku": {"name": "F64"}}]
    try:
        assert w.up() == 0
        w.logs.clear()
        assert w.down() == 0, w.text
        assert "audit: 2 Fabric capacity(ies) remain" in w.text and "rg-older" in w.text and "teamcap" in w.text
        assert w.text.count("carries this kit's capacity name") == 1                                  # only the one that looks like this kit's own is flagged
        assert not [c for c in w.fake.calls if c[:2] == ["resource", "delete"]] and w.fake.other_caps       # nothing outside the ledger was touched
    finally:
        w.close()


def test_wipe_refuses_a_ledger_from_another_subscription(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    try:
        assert w.up() == 0
        led = w.ledger
        for kind, name in (("fabricCapacity", w.cfg["capacity"]["name"]), ("resourceGroup", w.cfg["resourceGroup"])):
            led.get(kind, name)["details"]["subscriptionId"] = "some-other-sub"
        led.save()
        assert w.down() == runner.EXIT_WIPE_FAILED and "never changes the subscription" in w.text
        assert w.fake.cap is not None and w.fake.rg_exists                                             # not deleted in the wrong place
    finally:
        w.close()


def test_wipe_with_an_empty_ledger_does_nothing(world):
    assert world.down() == 0 and "nothing" in world.text and not world.fake.calls[1:] and not world.mock.requests


# ---------------------------------------------------------------------------------------------- preflight / diagnose
MUTATING_REST = ("suspend", "resume")


def test_preflight_is_read_only(world):
    assert world.preflight() == 0, world.text
    assert "Nothing was created" in world.text and "PREFLIGHT OK" in world.text
    assert not [c for c in world.fake.calls if c[:2] == ["group", "create"] or c[:3] == ["deployment", "group", "create"]]
    assert not [m for m in world.fake.methods() if m[0] in ("patch", "delete") or m[1] in MUTATING_REST]
    assert all(r["method"] == "GET" for r in world.mock.requests)                                     # Fabric: reads only
    assert "51 gold tables" in world.text and "29/29" in world.text                                    # the source builds into all 51 gold tables
    sizes = pipeline.tier_selections(load_model(ROOT / "model"), silver_mod.load_silver_spec(ROOT / "model"))
    for tier in spec_mod.TIERS:
        assert f"[PASS] data layers ({tier}{', this build' if tier == 'core' else ''}): {sizes[tier].summary()}" in world.text
    assert "the ontology and the semantic model bind to gold only" in world.text and "[PASS] data layers (core, this build)" in world.text
    assert "does not exist yet: build.sh creates it brand-new" in world.text and "no soft delete" in world.text
    assert "[PASS] capacity sku: F4 (4 capacity units)" in world.text                                 # the size is stated, not assumed


def test_preflight_blocks_on_a_resource_group_this_kit_did_not_create(tmp_path, repo_path):
    w = World(tmp_path, repo_path, FakeAz(rg_exists=True, rg_tags={"owner": "someone-else"}))
    try:
        assert w.preflight() == runner.EXIT_BLOCKED
        assert "[FAIL] azure plan" in w.text and "did not create it" in w.text and "brand-new resource group" in w.text
        assert not [c for c in w.fake.calls if c[:2] == ["group", "create"]]                           # still read only
    finally:
        w.close()


def test_preflight_blocks_on_a_capacity_that_is_not_the_configured_f4(tmp_path, repo_path):
    w = World(tmp_path, repo_path, FakeAz(rg_exists=True, cap={"sku": "F8", "state": "Active"}, rg_tags={A.RUN_TAG: "run-1"}))
    try:
        own_the_group(w)
        assert w.preflight() == runner.EXIT_BLOCKED
        assert "[FAIL] capacity sku" in w.text and "scale --sku F4" in w.text
        assert not w.fake.bodies                                                                       # read only: no resize
    finally:
        w.close()


def test_preflight_fails_when_fabric_reports_another_size_for_the_capacity(world):
    world.mock.capacity_sku = "F8"
    assert world.preflight() == runner.EXIT_BLOCKED
    assert "[FAIL] fabric capacity" in world.text and "configured for F4" in world.text


def test_preflight_names_a_deliberate_other_size_instead_of_hiding_it(world):
    world.cfg["capacity"].update(sku="F8", allowOtherSku=True)
    world.mock.capacity_sku = "F8"
    assert world.preflight() == 0, world.text
    assert "[WARN] capacity sku: F8, not F4" in world.text


def test_preflight_fails_on_a_disabled_ontology_tenant_setting(world):
    world.mock.tenant_settings = [{"settingName": "OntologyItem", "title": "Ontology item (preview)", "enabled": False},
                                  {"settingName": "DataAgent", "title": "Fabric data agent", "enabled": True}]
    assert world.preflight() == runner.EXIT_BLOCKED and "DISABLED" in world.text


def test_preflight_warns_when_tenant_settings_are_not_readable(world):
    assert world.mock.tenant_settings is None
    assert world.preflight() == 0 and "needs Fabric admin" in world.text


def test_preflight_blocked_when_not_signed_in(tmp_path, repo_path):
    class NoLogin(FakeAz):
        def __call__(self, args):
            return (1, "", "az login") if args[:2] == ["account", "show"] else super().__call__(args)
    w = World(tmp_path, repo_path, NoLogin())
    try:
        assert w.preflight() == runner.EXIT_BLOCKED and "never does" in w.text
    finally:
        w.close()


def test_diagnose_compares_the_ledger_with_reality(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    try:
        assert w.diagnose() == 0 and "ledger is empty" in w.text
        assert w.up() == 0
        w.logs.clear()
        assert w.diagnose() == 0, w.text
        plan = load_plan(w.out)
        assert "unchanged since the last deploy" in w.text and "entities 31/31" in w.text and f"{plan.counts()['tables']} tables; planned: {plan.summary()}" in w.text
        assert f"[PASS] layer silver: {len(plan.names('silver'))}/{len(plan.names('silver'))} tables registered" in w.text and "files in OneLake" in w.text   # counts per layer
        assert "created by this kit" in w.text and "wipe.sh deletes it whole" in w.text                 # the resource group's ownership proof is shown
        assert "[PASS] capacity sku: F4, as configured" in w.text                                        # Azure still says F4
        assert "[PASS] workspace capacity: Contoso-Bank-Ontology-Demo runs on contosofabricf4 (F4, Active)" in w.text   # and the workspace is on it
        w.fake.cap["sku"] = "F8"                                                                         # somebody resized the capacity
        w.logs.clear()
        assert w.diagnose() == runner.EXIT_PARTIAL and "[FAIL] capacity sku" in w.text and "scale --sku F4" in w.text
        w.fake.cap["sku"] = "F4"
        next(iter(w.mock.workspaces.values()))["capacityId"] = "some-other-capacity"                     # or moved the workspace
        w.logs.clear()
        assert w.diagnose() == runner.EXIT_PARTIAL and "[FAIL] workspace capacity" in w.text and "do not run on the F4" in w.text
        next(iter(w.mock.workspaces.values()))["capacityId"] = "cap-guid-1"
        w.fake.rg_tags[A.RUN_TAG] = "somebody-elses-group"
        w.logs.clear()
        assert w.diagnose() == runner.EXIT_PARTIAL and "not the group this kit created" in w.text
        w.fake.rg_tags[A.RUN_TAG] = w.ledger.get("resourceGroup", w.cfg["resourceGroup"])["details"]["runId"]
        ont_id = w.ledger.get("fabricOntology", "ContosoBankOntology")["id"]
        w.mock.definitions[ont_id]["parts"].append(parts_b64({"metrics/x.tmdl": "m"})["parts"][0])
        w.logs.clear()
        assert w.diagnose() == 0 and "differs from the last deploy" in w.text
    finally:
        w.close()


# ---------------------------------------------------------------------------------------------- helpers
def test_workspace_members_are_resolved_by_upn_and_unknown_users_are_skipped():
    fake = FakeAz()
    az = A.Az(runner=lambda a: (1, "", "Resource does not exist") if a[:2] == ["ad", "user"] and "ghost@x.com" in a else fake(a), execute=True)
    cfg = make_cfg()
    cfg["capacity"]["admins"] = ["me@contoso.com", "boss@contoso.com", "ghost@x.com"]
    cfg["workspace"]["members"] = [{"upn": "analyst@contoso.com", "role": "Viewer"}, {"id": "11111111", "type": "Group", "role": "Member"}]
    logs = []
    members = runner.resolve_members(cfg, az, logs.append)
    assert {"id": "11111111", "type": "Group", "role": "Member"} in members
    assert {"id": "oid-analyst", "type": "User", "role": "Viewer"} in members and {"id": "oid-boss", "type": "User", "role": "Admin"} in members
    assert not [m for m in members if m["id"] == "oid-me"] and any("ghost@x.com" in ln for ln in logs)


def test_find_repo_prefers_the_operators_pinned_checkout_and_fetches_the_pinned_commit_otherwise(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "contoso-retail-superapp4" / "data" / "releases").mkdir(parents=True)
    (home / "contoso-retail-superapp4" / ".git").mkdir()
    pinned = runner.REPO_COMMIT
    monkeypatch.setattr(runner, "git_head", lambda p: pinned)
    assert runner.find_repo(None, tmp_path / "cache", home=home, say=lambda m: None) == home / "contoso-retail-superapp4"
    monkeypatch.setattr(runner, "git_head", lambda p: "deadbeef" + "0" * 32)                     # the operator's checkout is on another commit
    seen = {}
    monkeypatch.setattr(runner, "clone_pinned", lambda cache, sources=None: seen.update(cache=cache, sources=sources) or tmp_path / "fetched")
    monkeypatch.setattr(runner, "pinned_sources", lambda checkout: [("src", checkout)])
    logs = []
    assert runner.find_repo(None, tmp_path / "cache", home=home, say=logs.append) == tmp_path / "fetched"
    assert seen["sources"] == [("src", home / "contoso-retail-superapp4")] and "not touched" in logs[0]
    monkeypatch.setattr(runner, "resolve_repo", lambda repo, clone, cache, drift=False: (repo, clone, drift) and tmp_path / "explicit")
    assert runner.find_repo(str(home), tmp_path / "cache") == tmp_path / "explicit"
    no_checkout = runner.find_repo(None, tmp_path / "cache", home=tmp_path / "empty-home", say=lambda m: None)
    assert no_checkout == tmp_path / "fetched" and seen["sources"] == [("src", None)]               # nothing local: public URL only


# ---------------------------------------------------------------------------------------------- entity descriptions: full, and the compact fallback
ONTOLOGY_NAME, MODEL_NAME = "ContosoBankOntology", "ContosoBankModel"


def _state(world) -> dict:
    return json.loads((world.out / ".state.json").read_text())


def _live_lines(world, what: str) -> dict:
    """entity (ontology) or table (semantic model) -> the length of the description line in the definition the mock holds."""
    kind, name, folder = ("fabricOntology", ONTOLOGY_NAME, "entities/") if what == "ontology" else ("fabricSemanticModel", MODEL_NAME, "definition/tables/")
    lines = {}
    for p in world.mock.definitions[world.ledger.get(kind, name)["id"]]["parts"]:
        if p["path"].startswith(folder):
            first = base64.b64decode(p["payload"]).decode("utf-8").split("\n", 1)[0]
            lines[p["path"].rsplit("/", 1)[-1][:-5]] = len(first) - 4 if first.startswith("/// ") else 0
    return lines


def _limit(world, chars: int, *collections: str) -> None:
    world.mock.max_description_chars = chars
    world.mock.description_collections = set(collections)


def _updates(world, collection: str) -> int:
    return len([r for r in world.mock.requests if r["method"] == "POST" and f"/{collection}/" in r["path"] and r["path"].endswith("/updateDefinition")])


def test_full_descriptions_are_the_default_and_the_longest_accepted_line_is_reported(world):
    assert world.up() == 0, world.text[-1500:]
    st = _state(world)
    ont, model = _live_lines(world, "ontology"), _live_lines(world, "semanticModel")
    assert st["descriptionsMode"] == "full" and not st.get("descriptionsFallback") and max(ont.values()) > 500           # the long text is what went live
    assert st["descriptionsAccepted"] == {"semantic model": max(model.values()), "ontology": max(ont.values())}
    assert "DESCRIPTION FALLBACK" not in world.text and "descriptions: full mode (default)" in world.text
    assert f"Fabric accepted the ontology with entity description lines of up to {max(ont.values())} characters" in world.text
    assert f"descriptions: full; Fabric accepted semantic model description lines of up to {max(model.values())} characters, ontology description lines of up to {max(ont.values())}" in world.text
    steps = (world.out / "NEXT_STEPS.md").read_text(encoding="utf-8")
    assert "Entity descriptions: full; Fabric accepted" in steps and "Descriptions: COMPACT" not in steps
    assert json.loads((world.out / "core" / "agents" / "dictionary" / "data_dictionary.json").read_text())["descriptionsMode"] == "full"


def test_a_refused_ontology_falls_back_to_compact_retries_once_and_remembers_it(world):
    _limit(world, 500, "ontologies")
    assert world.up() == 0, world.text[-2500:]
    refusal = world.mock.description_refusals
    assert len(refusal) == 1 and refusal[0]["collection"] == "ontologies" and refusal[0]["characters"] > 500
    assert "DESCRIPTION FALLBACK: Fabric refused the ontology definition with HTTP 400: InvalidDefinition: The definition of the Ontology item is not valid. | Entity '" in world.text
    assert f"the description is {refusal[0]['characters']} characters long, the maximum is 500" in world.text                  # the service's own words are in the log
    assert "DESCRIPTION FALLBACK APPLIED: Fabric accepted the ontology with compact descriptions" in world.text
    assert len(world.posts("/ontologies")) == 2                                                                              # the refused create and ONE retry
    assert max(_live_lines(world, "ontology").values()) <= 500
    assert max(_live_lines(world, "semanticModel").values()) <= 500 and _updates(world, "semanticModels") == 1               # the semantic model followed the ontology
    st = _state(world)
    assert st["descriptionsMode"] == "compact" and st["descriptionsFallback"]["item"] == "ontology" and "the maximum is 500" in st["descriptionsFallback"]["error"]
    assert st["descriptionsFallback"]["fullLongest"] >= refusal[0]["characters"] > 500 and st["descriptionsFallback"]["compactLongest"] <= 500
    steps = (world.out / "NEXT_STEPS.md").read_text(encoding="utf-8")
    assert "## Descriptions: COMPACT" in steps and "the maximum is 500" in steps and "bash build.sh --descriptions full" in steps and "item 10" in steps
    assert "descriptions: COMPACT (Fabric refused the full text on" in world.text and "BUILD DONE" in world.text
    dictionary = json.loads((world.out / "core" / "agents" / "dictionary" / "data_dictionary.json").read_text())
    assert dictionary["descriptionsMode"] == "compact" and not any("Star role:" in e["description"] for e in dictionary["entities"])
    assert all(e["kind"] and e["grain"] for e in dictionary["entities"])                                                     # the facts stay in the dictionary as data
    assert "star role and lineage are in the data dictionary" in (world.out / "core" / "agents" / "data_agent" / "instructions.md").read_text(encoding="utf-8")


def test_a_rerun_after_the_fallback_stays_compact_and_does_not_repeat_the_refused_attempt(world):
    _limit(world, 500, "ontologies")
    assert world.up() == 0
    posts, ont_updates, sm_updates = len(world.posts("/ontologies")), _updates(world, "ontologies"), _updates(world, "semanticModels")
    world.logs.clear()
    assert world.up() == 0, world.text[-1500:]
    assert len(world.mock.description_refusals) == 1 and "DESCRIPTION FALLBACK" not in world.text                          # nothing refused, nothing retried
    assert (len(world.posts("/ontologies")), _updates(world, "ontologies"), _updates(world, "semanticModels")) == (posts, ont_updates, sm_updates)
    assert world.text.count(": unchanged") == 3 and "descriptions: compact mode (the build state (descriptionsMode))" in world.text
    assert "remembered from an earlier run: Fabric refused the full text on" in world.text and _state(world)["descriptionsMode"] == "compact"
    assert "## Descriptions: COMPACT" in (world.out / "NEXT_STEPS.md").read_text(encoding="utf-8")                           # the report keeps saying why


def test_asking_for_full_again_tries_the_full_text_and_falls_back_again(world):
    _limit(world, 500, "ontologies")
    assert world.up() == 0
    world.logs.clear()
    assert world.up(descriptions="full") == 0, world.text[-1500:]                                                           # --descriptions full beats the remembered compact
    assert len(world.mock.description_refusals) == 2 and world.text.count("DESCRIPTION FALLBACK: Fabric refused") == 1
    assert _state(world)["descriptionsMode"] == "compact" and max(_live_lines(world, "semanticModel").values()) <= 500        # and the semantic model is compact again
    world.mock.max_description_chars = None                                                                                # Fabric takes the long text after all
    world.logs.clear()
    assert world.up(descriptions="full") == 0, world.text[-1500:]
    st = _state(world)
    assert st["descriptionsMode"] == "full" and not st["descriptionsFallback"] and max(_live_lines(world, "ontology").values()) > 500
    assert "DESCRIPTION FALLBACK" not in world.text and "Descriptions: COMPACT" not in (world.out / "NEXT_STEPS.md").read_text(encoding="utf-8")


def test_a_refused_semantic_model_falls_back_before_the_ontology_is_built(world):
    _limit(world, 500, "semanticModels")
    assert world.up() == 0, world.text[-2500:]
    assert [r["collection"] for r in world.mock.description_refusals] == ["semanticModels"]
    assert "DESCRIPTION FALLBACK: Fabric refused the semantic model definition with HTTP 400" in world.text
    assert len(world.posts("/semanticModels")) == 2 and len(world.posts("/ontologies")) == 1                                  # the ontology went up once, already compact
    assert max(_live_lines(world, "ontology").values()) <= 500 and max(_live_lines(world, "semanticModel").values()) <= 500
    st = _state(world)
    assert st["descriptionsMode"] == "compact" and st["descriptionsFallback"]["item"] == "semantic model"


def test_a_refusal_through_a_failed_operation_falls_back_too(world):
    _limit(world, 500, "ontologies")
    world.mock.refuse_via_operation = True                                                                                   # 202, then the operation ends Failed with the error
    assert world.up() == 0, world.text[-2500:]
    assert "DESCRIPTION FALLBACK: Fabric refused the ontology definition with HTTP 400: InvalidDefinition:" in world.text
    assert _state(world)["descriptionsMode"] == "compact" and max(_live_lines(world, "ontology").values()) <= 500


def test_a_compact_text_that_is_refused_too_is_an_error_with_the_original_first(world):
    assert world.up(pipeline=world.pipeline(until="build")) == 0
    _limit(world, 100, "ontologies")                                                                                         # not even the compact text fits
    st = _state(world)
    ws = {"workspaceId": st["workspaceId"], "workspaceName": st["workspaceName"]}
    lh = {"lakehouseId": st["lakehouseId"], "sqlEndpoint": st.get("sqlEndpoint"), "sqlDatabase": st.get("sqlDatabase")}
    model = load_model(ROOT / "model")
    with pytest.raises(runner.DescriptionFallbackFailed) as raised:
        runner.deploy_tier(world.make_ops(), world.ledger, model, "core", ws, lh, world.out, None, False, world.say)
    err = raised.value
    assert isinstance(err, oac.FabricError) and isinstance(err.original, oac.FabricError) and isinstance(err.retry, oac.FabricError)
    assert err.__cause__ is err.original and str(err).startswith(str(err.original)) and str(err.retry) in str(err)               # the original error, the retry attached
    assert "-> 400" in str(err.original) and "-> 400" in str(err.retry) and "refused too" in str(err)
    assert len(world.mock.description_refusals) == 2 and "DESCRIPTION FALLBACK FAILED" in world.text                            # one retry, no more
    assert _state(world).get("descriptionsMode") != "compact" and not _state(world).get("descriptionsFallback")                 # nothing is remembered
    assert world.ledger.get("fabricOntology", ONTOLOGY_NAME)["state"] == "pending"                                              # the write-ahead entry: a wipe still finds it


def test_the_deploy_command_falls_back_too_and_remembers_the_mode(world):
    assert world.up(pipeline=world.pipeline(until="build")) == 0                                                              # the semantic model is live with the full text
    _limit(world, 500, "ontologies")
    st = _state(world)
    ws = {"workspaceId": st["workspaceId"], "workspaceName": st["workspaceName"]}
    lh = {"lakehouseId": st["lakehouseId"], "sqlEndpoint": st.get("sqlEndpoint"), "sqlDatabase": st.get("sqlDatabase")}
    res = runner.deploy_tier(world.make_ops(), world.ledger, load_model(ROOT / "model"), "core", ws, lh, world.out, None, False, world.say)
    assert res["descriptionsMode"] == "compact" and res["descriptions"].startswith("COMPACT (Fabric refused the full text on")
    assert res["ontology"]["action"] == "created" and res["semanticModel"]["action"] == "replaced"                            # the semantic model followed the ontology
    st = _state(world)
    assert st["descriptionsMode"] == "compact" and st["descriptionsFallback"]["item"] == "ontology" and "the maximum is 500" in st["descriptionsFallback"]["error"]
    assert max(_live_lines(world, "ontology").values()) <= 500 and max(_live_lines(world, "semanticModel").values()) <= 500


def test_compact_asked_for_and_refused_raises_the_original_error_without_a_retry(world):
    assert world.up(pipeline=world.pipeline(until="build")) == 0
    _limit(world, 100, "ontologies")
    st = _state(world)
    ws = {"workspaceId": st["workspaceId"], "workspaceName": st["workspaceName"]}
    lh = {"lakehouseId": st["lakehouseId"], "sqlEndpoint": st.get("sqlEndpoint"), "sqlDatabase": st.get("sqlDatabase")}
    with pytest.raises(oac.FabricError) as raised:
        runner.deploy_tier(world.make_ops(), world.ledger, load_model(ROOT / "model"), "core", ws, lh, world.out, None, False, world.say, descriptions="compact")
    assert not isinstance(raised.value, runner.DescriptionFallbackFailed) and "-> 400" in str(raised.value)
    assert len(world.mock.description_refusals) == 1 and "DESCRIPTION FALLBACK" not in world.text


def test_a_build_that_cannot_place_even_the_compact_text_is_partial_and_keeps_the_error(world):
    _limit(world, 100, "ontologies")
    assert world.up() == runner.EXIT_PARTIAL
    assert "PARTIAL" in world.text and "DESCRIPTION FALLBACK FAILED" in world.text and "wipe.sh" in world.text
    last = (world.state / "runs" / "last-error.txt").read_text()
    assert "DescriptionFallbackFailed" in last and "the retry with compact descriptions was refused too" in last and "InvalidDefinition" in last
    assert _state(world).get("descriptionsMode") != "compact" and world.ledger.get("fabricOntology", ONTOLOGY_NAME)["state"] == "pending"
    assert world.ledger.get("fabricSemanticModel", MODEL_NAME)["state"] == "present"                                            # the rest is kept; a re-run resumes


def test_the_403_gate_is_not_a_description_problem_and_the_fallback_waits_for_the_400(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    w.mock.ontology_forbidden = True
    _limit(w, 500, "ontologies")
    refused_creates: list[int] = []

    def open_after_three_polls(mock):
        refused_creates.append(len(mock.description_refusals))
        if len(refused_creates) == 4:
            mock.ontology_forbidden = False
    w.mock.on_forbidden_create = open_after_three_polls
    try:
        assert w.up() == 0, w.text[-2500:]
        assert refused_creates == [0, 0, 0, 0]                                                                                # the gate answered first, four times: no description was judged
        assert "gate: OPEN after" in w.text and w.text.index("gate: OPEN") > w.text.index("ACTION NEEDED")
        assert len(w.mock.description_refusals) == 1 and "DESCRIPTION FALLBACK APPLIED" in w.text                            # the 400 came once the gate was open
        assert _state(w)["descriptionsMode"] == "compact" and max(_live_lines(w, "ontology").values()) <= 500
    finally:
        w.close()


def test_a_closed_gate_alone_never_triggers_the_fallback(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    w.mock.ontology_forbidden = True
    _limit(w, 500, "ontologies")
    try:
        assert w.up(pipeline=w.pipeline(gate_timeout_min=0)) == runner.EXIT_BLOCKED
        assert "DESCRIPTION FALLBACK" not in w.text and w.mock.description_refusals == []
        assert _state(w)["descriptionsMode"] == "full" and not _state(w).get("descriptionsFallback")
    finally:
        w.close()


def test_a_service_that_judges_the_definition_before_the_gate_still_ends_compact_and_open(tmp_path, repo_path):
    """If Fabric answers 400 before 403: the full text is refused, the compact retry reaches the gate (so it passed), the run keeps polling with the
    compact text, and the semantic model follows the ontology once it exists."""
    w = World(tmp_path, repo_path)
    w.mock.ontology_forbidden = True
    w.mock.validate_before_gate = True
    _limit(w, 500, "ontologies")
    seen: list[int] = []

    def open_after_two_polls(mock):
        seen.append(1)
        if len(seen) == 3:
            mock.ontology_forbidden = False
    w.mock.on_forbidden_create = open_after_two_polls
    try:
        assert w.up() == 0, w.text[-2500:]
        assert len(w.mock.description_refusals) == 1 and "the compact retry reached the 403 gate" in w.text and "ACTION NEEDED" in w.text
        assert _state(w)["descriptionsMode"] == "compact" and max(_live_lines(w, "ontology").values()) <= 500
        assert max(_live_lines(w, "semanticModel").values()) <= 500 and "BUILD DONE" in w.text
    finally:
        w.close()


def test_other_refusals_and_errors_are_not_description_fallbacks(tmp_path, repo_path):
    w = World(tmp_path, repo_path)
    w.mock.graph_update_error = "InvalidDefinition"                                                                          # a 400 for the GRAPH definition: not an ontology or a semantic model
    try:
        assert w.up() == runner.EXIT_PARTIAL
        assert "DESCRIPTION FALLBACK" not in w.text and _state(w)["descriptionsMode"] == "full"
    finally:
        w.close()


def test_definition_rejection_recognises_a_refused_definition_and_nothing_else():
    ws = "http://x/v1/workspaces/w1"
    refused = ('{"requestId": "r", "errorCode": "InvalidDefinition", "message": "bad", "moreDetails": [{"errorCode": "E", "message": "Entity A is too long"}]}')
    for method, url in (("POST", f"{ws}/ontologies"), ("POST", f"{ws}/semanticModels"), ("POST", f"{ws}/ontologies/0f0f0f0f-aaaa-4bbb-8ccc-0123456789ab/updateDefinition?updateMetadata=false"),
                        ("POST", f"{ws}/semanticModels/0f0f0f0f-aaaa-4bbb-8ccc-0123456789ab/updateDefinition"), ("GET", "http://x/v1/operations/op-1/result")):
        text = runner.definition_rejection(oac.FabricError(f"{method} {url} -> 400: {refused}"))
        assert text == "InvalidDefinition: bad | Entity A is too long", (method, url)
    assert runner.definition_rejection(oac.FabricError('operation Failed: {"errorCode": "InvalidDefinition", "message": "bad"}')) == "InvalidDefinition: bad"
    assert runner.definition_rejection(oac.FabricError(f'POST {ws}/ontologies -> 400: not json at all')) == f"POST {ws}/ontologies -> 400: not json at all"
    for message in (f'POST {ws}/ontologies -> 403: {{"errorCode": "FeatureNotAvailable"}}', f"POST {ws}/ontologies -> 429: slow down", f"POST {ws}/ontologies -> 500: oops",
                    f"POST {ws}/ontologies -> 503: busy", f"POST {ws}/semanticModels -> 404: gone", f"POST {ws}/semanticModels -> 409: ItemOperationInProgress",
                    f"POST {ws}/lakehouses -> 400: {refused}", f"POST {ws}/graphModels/0f0f0f0f-aaaa-4bbb-8ccc-0123456789ab/updateDefinition -> 400: {refused}",
                    f"GET {ws}/ontologies -> 400: {refused}", 'operation Failed: {"errorCode": "CapacityNotActive", "message": "paused"}', "operation timed out", "202 without a Location header"):
        assert runner.definition_rejection(oac.FabricError(message)) is None, message
    assert runner.definition_rejection(RuntimeError(f"POST {ws}/ontologies -> 400: {refused}")) is None                       # only Fabric's own errors count
    assert runner.ontology_gate(oac.FabricError(f'POST {ws}/ontologies -> 403: {{"errorCode": "FeatureNotAvailable"}}')) and runner.definition_rejection(
        oac.FabricError(f'POST {ws}/ontologies -> 403: {{"errorCode": "FeatureNotAvailable"}}')) is None                       # the gate keeps its own handling


def test_a_table_the_endpoint_does_not_know_yet_is_not_taken_for_a_description_problem():
    ws = "http://x/v1/workspaces/w1"
    for said in ('{"errorCode": "InvalidDefinition", "message": "Table dim_account was not found in the SQL analytics endpoint"}',
                 '{"errorCode": "InvalidDefinition", "message": "Invalid object name dbo.dim_account"}',
                 '{"errorCode": "BadRequest", "message": "The bound table does not exist", "moreDetails": []}',
                 '{"errorCode": "InvalidDefinition", "message": "bad", "moreDetails": [{"errorCode": "E", "message": "Could not find table fact_transaction"}]}'):
        assert runner.definition_rejection(oac.FabricError(f"POST {ws}/ontologies -> 400: {said}")) is None, said
        assert runner.definition_rejection(oac.FabricError(f"POST {ws}/ontologies/0f0f0f0f-aaaa-4bbb-8ccc-0123456789ab/updateDefinition -> 400: {said}")) is None, said
    assert runner.definition_rejection(oac.FabricError('operation Failed: {"errorCode": "InvalidDefinition", "message": "table not found"}')) is None
    # a refusal that does not say a table is missing is still taken for what it may be: the definition (descriptions included)
    assert runner.definition_rejection(oac.FabricError(f'POST {ws}/ontologies -> 400: {{"errorCode": "InvalidDefinition", "message": "Description is too long"}}')) == \
        "InvalidDefinition: Description is too long"


def test_preflight_says_which_description_mode_applies_and_what_happens_if_fabric_refuses_it(world):
    assert world.preflight() == 0, world.text[-1500:]
    line = next(ln for ln in world.logs if "entity descriptions" in ln)
    assert line.startswith("  [PASS]") and "full mode (default)" in line and "longest line is 736 characters" in line and "509" in line and "retries once and remembers it" in line
    world.logs.clear()
    assert runner.preflight(world.cfg, world.az, repo_fn=lambda: world.repo, price_fetch=lambda u: {"Items": []}, descriptions="compact", **world.kw()) == 0
    line = next(ln for ln in world.logs if "entity descriptions" in ln)
    assert "compact mode (--descriptions)" in line and "last resort" in line and "longest line is 4" in line
