"""Command line for the Contoso bank ontology demo kit.  python -m contoso_onto <command> --help

Read-only and offline unless a command says otherwise. Anything that creates, changes or deletes something in Azure or
Fabric needs --execute; without it the command prints exactly what it would do.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

from . import REPO_COMMIT, __version__, agents as agents_mod, assess as assess_mod, azure_infra, fabric_ops, oac, oracles, silver as silver_mod, spec as spec_mod, standards as standards_mod
from .gold import load_model
from .layers import PlanError, load_plan
from .ledger import Ledger, Lock, LockBusy
from .pipeline import LayerOutput, build_gold, tier_selections, write_layers
from .snapshot import resolve_repo

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = {
    "resourceGroup": "rg-contoso-fabric-ontology",
    "location": "eastus2",
    "capacity": {"name": "contosofabricf4", "sku": "F4", "admins": []},
    "workspace": {"name": "Contoso-Bank-Ontology-Demo", "description": "Contoso bank ontology demo (synthetic data).", "members": []},
    "lakehouse": {"name": "ContosoBankLH"},
    "tags": {"project": "contoso-bank-ontology", "purpose": "demo", "autoPause": "true"},
    "ontology": {"tier": "core"},
    "onelake": {"stagingFolder": "Files/staging"},
    "foundryIq": {"searchEndpointEnv": "AZURE_AI_SEARCH_ENDPOINT", "knowledgeBase": "contoso-bank-ontology-kb", "knowledgeSource": "contoso-bank-ontology-ks"},
}


PROJECT = "contoso-bank-ontology-demo"
OPERATOR_CMDS = {"preflight", "up", "diagnose", "down"}


def default_state_dir() -> Path:
    return Path(os.environ.get("CONTOSO_STATE_DIR") or Path.home() / ".local" / "state" / PROJECT)


def _merge(a: dict, b: dict) -> None:
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _merge(a[k], v)
        else:
            a[k] = v


def env_overrides(env: Optional[dict] = None) -> dict:
    """Settings that are normally supplied on the command line of build.sh, for example CONTOSO_CAPACITY_ADMINS=me@contoso.com."""
    e = os.environ if env is None else env
    o: dict[str, Any] = {}
    if e.get("CONTOSO_RESOURCE_GROUP"):
        o["resourceGroup"] = e["CONTOSO_RESOURCE_GROUP"]
    if e.get("CONTOSO_LOCATION"):
        o["location"] = e["CONTOSO_LOCATION"]
    cap = {k: v for k, v in (("name", e.get("CONTOSO_CAPACITY_NAME")), ("sku", e.get("CONTOSO_CAPACITY_SKU"))) if v}
    if str(e.get(azure_infra.ALLOW_OTHER_SKU_ENV, "")).strip().lower() in ("1", "true", "yes"):
        cap["allowOtherSku"] = True
    if e.get("CONTOSO_CAPACITY_ADMINS"):
        cap["admins"] = [x.strip() for x in e["CONTOSO_CAPACITY_ADMINS"].replace(";", ",").split(",") if x.strip()]
    if cap:
        o["capacity"] = cap
    if e.get("CONTOSO_WORKSPACE_NAME"):
        o["workspace"] = {"name": e["CONTOSO_WORKSPACE_NAME"]}
    if e.get("CONTOSO_TIER"):
        o["ontology"] = {"tier": e["CONTOSO_TIER"]}
    return o


def load_config(path: Optional[str] = None, env: Optional[dict] = None, state_dir: Optional[Path] = None) -> dict:
    """defaults < config/demo.config.json < <state dir>/config/local.json < environment.  An explicit --config file replaces the two files."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    layers = [Path(path)] if path else [ROOT / "config" / "demo.config.json", (state_dir or default_state_dir()) / "config" / "local.json"]
    for p in layers:
        if p.exists():
            _merge(cfg, json.loads(p.read_text(encoding="utf-8-sig")))
    _merge(cfg, env_overrides(env))
    return cfg


def state_path(out: Path) -> Path:
    return out / ".state.json"


def read_state(out: Path) -> dict:
    p = state_path(out)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def write_state(out: Path, **kw: Any) -> dict:
    st = read_state(out)
    st.update({k: v for k, v in kw.items() if v is not None})
    out.mkdir(parents=True, exist_ok=True)
    state_path(out).write_text(json.dumps(st, indent=2), encoding="utf-8")
    return st


def write_tree(files: dict[str, str], folder: Path) -> None:
    for rel, text in files.items():
        p = folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")


def say(msg: str = "") -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------------------------------
# assess / build
# --------------------------------------------------------------------------------------------------
def cmd_assess(a: argparse.Namespace) -> int:
    repo = resolve_repo(a.repo, a.clone, Path(a.cache), a.allow_drift)
    res = assess_mod.assess(repo)
    out = Path(a.out) / "assessment"
    assess_mod.write_outputs(res, out)
    say(f"assessed {repo}: {res['files']} files, {res['collections']} collections, {res['rows']:,} rows")
    for f in res["findings"]:
        say(f"  {f['id']} [{f['severity']}] {f['title']}")
    say(f"wrote {out / 'assessment.json'} and {out / 'ASSESSMENT.generated.md'}")
    return 0


def cmd_standards(a: argparse.Namespace) -> int:
    """BIAN / Microsoft CDM alignment of the ontology, derived from the repo's own banking profile and registry (read only)."""
    import yaml
    repo = resolve_repo(a.repo, a.clone, Path(a.cache), a.allow_drift)
    data = standards_mod.generate(repo)
    ents = data["entities"]
    src = data["source"]
    say(f"standards from {repo}: profile {src['profileVersion']}, mapping {src['mappingVersion']}, BIAN {src['bianRelease']}")
    say(f"  {len(ents)} profile entities; {sum(1 for e in ents.values() if e['ontology_entity'])} realised in the ontology, "
        f"{sum(1 for e in ents.values() if e.get('cdm'))} with a Microsoft CDM reference, {sum(1 for e in ents.values() if e['bian'])} with a BIAN mapping, "
        f"{sum(1 for e in ents.values() if not e['bian'])} without one")
    for n, e in ents.items():
        if e["ontology_entity"]:
            say(f"  {n:<22} -> {e['ontology_entity']:<20} BIAN: {', '.join(b['domain'] for b in e['bian']) or 'none'}   CDM: {(e.get('cdm') or {}).get('entity', '-')}")
    if a.write:
        path = ROOT / "model" / "standards.yaml"
        header = ("# GENERATED by `python -m contoso_onto standards --repo <checkout> --write`; do not edit by hand (a test compares it with the repo's registry).\n"
                  "# Which profile entity the ontology realises is ONTOLOGY_ENTITY in contoso_onto/standards.py.\n")
        path.write_text(header + yaml.safe_dump({"standards": data}, sort_keys=False, allow_unicode=True, width=150), encoding="utf-8", newline="\n")
        say(f"wrote {path}")
    return 0


def build_tier(model: dict, tier: str, gold_tables: dict, out: Path, brief_only: bool = False, quiet: bool = False, say_fn: Any = None,
               descriptions: str = spec_mod.DEFAULT_DESCRIPTIONS) -> int:
    """Spec -> semantic model + ontology folders, validation, agent pack, golden answers. quiet=True hides the per-part validation
    chatter and only reports when something fails. `descriptions` is the mode of the entity descriptions (full or compact)."""
    if not quiet:
        return _build_tier(model, tier, gold_tables, out, brief_only, descriptions)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _build_tier(model, tier, gold_tables, out, brief_only, descriptions)
    if rc != 0:
        (say_fn or say)(buf.getvalue().rstrip())
    return rc


def _build_tier(model: dict, tier: str, gold_tables: dict, out: Path, brief_only: bool = False, descriptions: str = spec_mod.DEFAULT_DESCRIPTIONS) -> int:
    sp = spec_mod.make_spec(model, tier, descriptions)
    rep = spec_mod.check_spec(sp)
    rep.show(f"spec ({tier})")
    if not rep.ok:
        return 2
    st = read_state(out)
    ctx = oac.make_ctx(workspace_id=st.get("workspaceId"), lakehouse_id=st.get("lakehouseId"), workspace_name=st.get("workspaceName"),
                       sql_endpoint=st.get("sqlEndpoint"), sql_db=st.get("sqlDatabase"), lakehouse_name=sp["lakehouse"]["name"])
    sm = oac.build_semantic_model(sp, ctx)
    ont = oac.build_ontology(sp, ctx)
    tdir = out / tier
    for folder in ("semantic_model", "ontology", "payloads", "agents"):
        if (tdir / folder).exists():
            shutil.rmtree(tdir / folder)
    oac.write_parts(sm, tdir / "semantic_model")
    oac.write_parts(ont, tdir / "ontology")
    (tdir / "payloads").mkdir(parents=True, exist_ok=True)
    (tdir / "payloads" / "semantic_model.create.json").write_text(json.dumps({"displayName": sp["semanticModel"]["name"], "definition": oac.to_payload(sm, "TMDL")}), encoding="utf-8")
    (tdir / "payloads" / "ontology.create.json").write_text(json.dumps({"displayName": sp["ontology"]["name"], "definition": oac.to_payload(ont)}), encoding="utf-8")
    ok = True
    for title, parts in ((f"semantic model ({tier})", sm), (f"ontology ({tier})", ont)):
        r = oac.validate_parts(parts)
        r.show(title)
        ok &= r.ok
    golden = oracles.run_oracles(gold_tables, model.get("golden", []), tier="full" if tier == "full" else "core")
    files = agents_mod.build_pack(sp, golden, oac.build_brief(sp), st.get("workspaceId") or "<WORKSPACE_ID>", st.get("ontologyId") or "<ONTOLOGY_ID>")
    write_tree(files, tdir / "agents")
    bad = [g for g in golden if g.get("error")]
    for g in bad:
        say(f"  golden {g['id']} failed: {g['error']}")
    say(f"  {tier}: {rep.stats['entities']} entities, {rep.stats['properties']} properties, {rep.stats['ontologyRelationships']} ontology relationships, "
        f"{rep.stats['semanticRelationships']} semantic-model relationships, {rep.stats['measures']} measures, {rep.stats['rules']} rules, {len(golden)} golden questions")
    return 0 if ok and not bad else 1


def layer_lines(layers: LayerOutput, out: Optional[Path] = None) -> list[str]:
    """What `write_layers` wrote, one line per layer (with its folder when `out` is given)."""
    sel, bronze, silver, gold = layers.selection, layers.bronze, layers.silver, layers.gold

    def to(folder: str) -> str:
        return f" -> {Path(out) / folder}" if out is not None else ""
    return [f"layers for the {sel.tier} tier: {sel.summary()}; the ontology and the semantic model bind to gold only",
            f"gold: {len(gold['tables'])} tables, {sum(t['rows'] for t in gold['tables'].values()):,} rows{to('gold')}",
            f"silver: {len(silver['tables'])} tables (incl. 2 catalogs), {sum(t['rows'] for t in silver['tables'].values()):,} rows{to('silver')}",
            f"bronze: {len(bronze['files'])} seed files ({sum(f['bytes'] for f in bronze['files']):,} bytes, hashed and staged for upload) and their manifest table{to('bronze')}"]


def data_layers(model: dict) -> dict:
    """The size of each tier's layers, computed from the specifications alone: the `dataLayers` part of `plan`."""
    sels = tier_selections(model, silver_mod.load_silver_spec(ROOT / "model"))
    return {**{t: {**s.counts(), "summary": s.summary()} for t, s in sels.items()},
            "note": "The ontology and the semantic model bind to gold only; bronze (the seed files, verbatim) and silver (CDM profile tables) stay in the lakehouse "
                    "for lineage and audit. A tier loads only the silver tables its gold tables are built from, and the seed files those read."}


def descriptions_line(a: argparse.Namespace) -> str:
    return f"descriptions: {a.descriptions} ({a.descriptions_source}); " + (
        "the architecture clause (star role, grain, lineage) is part of every entity description" if a.descriptions == "full"
        else "business meaning and standards sentence only, every entity description within 500 characters")


def cmd_build(a: argparse.Namespace) -> int:
    repo = resolve_repo(a.repo, a.clone, Path(a.cache), a.allow_drift)
    model = load_model(ROOT / "model")
    out = Path(a.out)
    say(descriptions_line(a))
    built = build_gold(model, repo, model_dir=ROOT / "model")                  # builds silver once, then gold from it
    for w in built.warnings:
        say(f"  warning: {w}")
    if not built.ok:
        say("SILVER BUILD FAILED" if built.silver is not None and not built.silver.ok else "GOLD BUILD FAILED")
        for e in built.errors:
            say(f"  ERROR {e}")
        return 1
    for line in layer_lines(write_layers(model, built, out, repo, ROOT / "model", a.tier), out):          # only what the tier needs, bronze -> silver -> gold
        say(line)
    tables = {k: v.to_pandas() for k, v in built.tables.items()}
    rc = 0
    for tier in (["core", "full"] if a.tier == "both" else [a.tier]):
        rc = max(rc, build_tier(model, tier, tables, out, descriptions=a.descriptions))
    exp = oracles.run_oracles(tables, model.get("golden", []))
    (out / "eval").mkdir(parents=True, exist_ok=True)
    (out / "eval" / "golden_expected.json").write_text(json.dumps(exp, indent=1, default=str), encoding="utf-8")
    return rc


# --------------------------------------------------------------------------------------------------
# azure / fabric
# --------------------------------------------------------------------------------------------------
def cmd_plan(a: argparse.Namespace) -> int:
    from . import runner
    cfg = load_config(a.config, state_dir=state_dir_of(a))
    try:
        led: Optional[Ledger] = None if a.offline else Ledger(runner.ledger_path(state_dir_of(a)))
    except Exception:  # noqa: BLE001 - a damaged ledger is reported by preflight; the plan only needs it for ownership hints
        led = None
    res = azure_infra.plan(cfg, azure_infra.Az(), offline=a.offline, out_dir=Path(a.out), ledger=led)
    if not res.get("errors"):
        model = load_model(ROOT / "model")
        res["dataLayers"] = data_layers(model)
        res.setdefault("steps", []).append("lakehouse: upload the bronze seed files, then load the silver tables, then the gold tables (sizes per tier under dataLayers)")
        stats = spec_mod.description_stats(model, cfg["ontology"]["tier"], a.descriptions)
        res["descriptions"] = {**stats, "source": a.descriptions_source}
        longest, accepted = stats["longestLine"][a.descriptions]["characters"], stats["longestAcceptedLive"]
        res["steps"].append(f"ontology and semantic model: entity descriptions in {a.descriptions} mode, the longest line {longest} characters (the longest Fabric has accepted is {accepted}); "
                            f"if Fabric refuses a definition with HTTP 400 while the mode is full, the build retries once in compact mode (at most "
                            f"{stats['longestLine']['compact']['characters']}) and remembers it")
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "plan.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    say(json.dumps(res, indent=1))
    return 1 if res.get("errors") else 0


def fabric_client() -> tuple[oac.Fabric, Any]:
    tf = oac.default_token_fn()
    return oac.Fabric(tf), tf


def make_ops() -> fabric_ops.Ops:
    fab, tf = fabric_client()
    return fabric_ops.Ops(fab, tf, say)


def state_dir_of(a: argparse.Namespace) -> Path:
    return Path(getattr(a, "state_dir", None) or default_state_dir())


def cmd_provision(a: argparse.Namespace) -> int:
    from . import runner
    cfg = load_config(a.config, state_dir=state_dir_of(a))
    az = azure_infra.Az(execute=a.execute)
    out = Path(a.out)
    bad = azure_infra.validate_config(cfg)
    if bad:
        say("BAD CONFIG: " + "; ".join(bad))
        return 2
    if not a.execute:
        try:
            azure_infra.provision(cfg, az, out, Ledger(runner.ledger_path(state_dir_of(a))))          # read only: nothing is recorded in a dry run
        except azure_infra.ExistingResourceGroup as ex:
            say(f"BLOCKED: {ex}")
            return 3
        say("\n".join(f"  {c}" for c in az.log))
        say("\nDRY RUN - nothing was created. Review `plan`, then re-run with --execute.")
        say(f"Then: workspace '{cfg['workspace']['name']}' + lakehouse '{cfg['lakehouse']['name']}' on capacity '{cfg['capacity']['name']}' (Fabric REST).")
        return 0
    try:
        with Lock(runner.lock_path(state_dir_of(a))):
            ledger = Ledger(runner.ledger_path(state_dir_of(a)))
            try:
                res = azure_infra.provision(cfg, az, out, ledger)
            except (azure_infra.ExistingResourceGroup, azure_infra.CapacitySkuMismatch) as ex:
                say(f"BLOCKED: {ex}")
                return 3
            say("\n".join(f"  {c}" for c in az.log))
            say(json.dumps(res, indent=1))
            if res.get("state") not in (None, "Active"):
                say("capacity is not Active yet; run `capacity resume` and retry")
                return 1
            try:
                ws, lh = runner.ensure_base(cfg, az, make_ops(), ledger, out, say)
            except fabric_ops.FabricCapacitySkuMismatch as ex:
                say(f"BLOCKED: {ex}")
                return 3
            say(json.dumps(read_state(out), indent=2))
            return 0
    except LockBusy as ex:
        say(f"BUSY: {ex}")
        return 6


def cmd_capacity(a: argparse.Namespace) -> int:
    from . import runner
    cfg = load_config(a.config, state_dir=state_dir_of(a))
    az = azure_infra.Az(execute=a.execute)
    if a.action == "status":
        say(json.dumps(azure_infra.capacity_state(cfg, az), indent=1))
        return 0
    try:
        with Lock(runner.lock_path(state_dir_of(a))):
            azure_infra.lifecycle(cfg, az, a.action, a.sku)
    except LockBusy as ex:
        say(f"BUSY: {ex}")
        return 6
    say("\n".join(f"  {c}" for c in az.log))
    if not a.execute:
        say("DRY RUN - re-run with --execute")
    elif a.action == "scale" and a.sku and a.sku != azure_infra.DEMO_SKU:
        say(f"note: this demo is built for {azure_infra.DEMO_SKU}. build.sh refuses to continue on {a.sku} unless it is run with "
            f"{azure_infra.ALLOW_OTHER_SKU_ENV}=1 CONTOSO_CAPACITY_SKU={a.sku}; resize back with `capacity scale --sku {azure_infra.DEMO_SKU} --execute`")
    return 0


def csv_arg(value: Optional[str]) -> list[str]:
    return [x.strip() for x in (value or "").replace(";", ",").split(",") if x.strip()]


def cmd_load(a: argparse.Namespace) -> int:
    """Upload the bronze seed files and load the silver and gold tables the output folder describes, in layer order. --layers picks layers,
    --only picks tables (lakehouse names) out of the chosen layers."""
    cfg = load_config(a.config, state_dir=state_dir_of(a))
    out = Path(a.out)
    try:
        everything = load_plan(out)
    except PlanError as ex:
        say(f"{ex}")
        return 1
    try:
        plan = everything.select(csv_arg(a.layers) or None, csv_arg(a.only) or None)
    except PlanError as ex:
        say(f"BAD SELECTION: {ex}")
        return 2
    st = read_state(out)
    say(f"{plan.summary()}, {sum(plan.table_bytes(layer) for layer in plan.layers()):,} bytes -> {cfg['lakehouse']['name']} "
        f"(Files/bronze, Tables; staging {cfg['onelake']['stagingFolder']}); loaded in this order: {' -> '.join(plan.layers()) or 'nothing selected'}")
    for layer in plan.layers():
        say(f"  {layer}: " + (f"{len(plan.files)} seed files, " if layer == "bronze" else "") + f"{len(plan.names(layer))} tables, {plan.table_bytes(layer):,} bytes")
    if not a.execute:
        say("DRY RUN - nothing uploaded. Re-run with --execute (needs `provision --execute` first).")
        return 0
    if not st.get("workspaceId") or not st.get("lakehouseId"):
        say("no workspace/lakehouse in state; run `provision --execute` first")
        return 1
    ops = make_ops()
    try:
        res = ops.load_layers(plan, st["workspaceId"], st["lakehouseId"], staging=cfg["onelake"]["stagingFolder"], loader=a.loader, sql_endpoint_id=st.get("sqlDatabase"),
                              fallback=False)
    except fabric_ops.LayerLoadError as ex:
        say(f"load stopped in the {ex.layer} layer" + (f" at table {ex.table}" if ex.table else "") + f": {ex.detail}")
        say("the layers after it were not started; fix the cause and run `load` again (Overwrite: a re-run is safe)")
        return 1
    say(json.dumps(res, indent=1))
    return 0


def cmd_deploy(a: argparse.Namespace) -> int:
    from . import runner
    cfg = load_config(a.config, state_dir=state_dir_of(a))
    out = Path(a.out)
    model = load_model(ROOT / "model")
    tier = a.tier or cfg["ontology"]["tier"]
    say(descriptions_line(a))
    sp = spec_mod.make_spec(model, tier, a.descriptions)
    rep = spec_mod.check_spec(sp)
    rep.show(f"spec ({tier})")
    if not rep.ok:
        return 2
    st = read_state(out)
    if a.execute and not (st.get("workspaceId") and st.get("lakehouseId")):
        say("no workspace/lakehouse in state; run `provision --execute` and `load --execute` first")
        return 1
    ctx = oac.make_ctx(workspace_id=st.get("workspaceId"), lakehouse_id=st.get("lakehouseId"), workspace_name=st.get("workspaceName"),
                       sql_endpoint=st.get("sqlEndpoint"), sql_db=st.get("sqlDatabase"), lakehouse_name=sp["lakehouse"]["name"])
    seed = oac.load_seed(a.seed) if a.seed else None
    sm = oac.build_semantic_model(sp, ctx)
    ont = oac.build_ontology(sp, ctx, seed, a.ont_annotations)
    for title, parts in ((f"semantic model ({tier})", sm), (f"ontology ({tier})", ont)):
        r = oac.validate_parts(parts, strict=a.execute)
        r.show(title)
        if not r.ok:
            return 1
    say("\nPLAN")
    say(f"  1. semantic model '{sp['semanticModel']['name']}' ({len(sm)} parts, Direct Lake on OneLake)")
    say(f"  2. refresh/frame: {'yes' if a.refresh else 'when created or replaced'}")
    say(f"  3. ontology '{sp['ontology']['name']}' ({len(ont)} parts, Gen 2 TMDL), seed={'yes' if seed else 'no'}, ont-annotations={'yes' if a.ont_annotations else 'no'}, descriptions={a.descriptions}")
    say("  4. an existing item is replaced only while it is unchanged since this kit last deployed it (or with --replace, after a backup)")
    say("  5. then in the portal: Manage graph eligibility, generate metrics from the semantic model, export the golden definition (see docs/LIVE_CHECKLIST.md)")
    if not a.execute:
        say("\nDRY RUN - nothing was sent. Re-run with --execute.")
        return 0
    try:
        with Lock(runner.lock_path(state_dir_of(a))):
            ledger = Ledger(runner.ledger_path(state_dir_of(a)))
            ws = {"workspaceId": st["workspaceId"], "workspaceName": st.get("workspaceName") or cfg["workspace"]["name"]}
            lh = {"lakehouseId": st["lakehouseId"], "sqlEndpoint": st.get("sqlEndpoint"), "sqlDatabase": st.get("sqlDatabase")}
            res = runner.deploy_tier(make_ops(), ledger, model, tier, ws, lh, out, seed, a.replace or bool(seed), say, refresh=True, ont_annotations=a.ont_annotations,
                                     descriptions=a.descriptions, descriptions_source=a.descriptions_source)
    except LockBusy as ex:
        say(f"BUSY: {ex}")
        return 6
    new = {"semanticModelId": res["semanticModel"]["id"], "ontologyId": res["ontology"]["id"]}
    write_state(out, **new, tier=tier)
    say(f"descriptions: {res['descriptions']}")
    say(json.dumps({"semanticModel": res["semanticModel"], "ontology": res["ontology"]}, indent=2))
    return 0


def cmd_export(a: argparse.Namespace) -> int:
    out = Path(a.out)
    st = read_state(out)
    ws = a.workspace_id or st.get("workspaceId")
    if not ws:
        say("need --workspace-id or a state file")
        return 1
    fab, _ = fabric_client()
    collection = {"ontology": "ontologies", "semanticModel": "semanticModels"}[a.item]
    item = {"id": a.id} if a.id else fab.find(ws, collection, a.name)
    if not item:
        say(f"{a.item} '{a.name}' not found")
        return 1
    parts = fab.get_definition(ws, collection, item["id"], "TMDL" if a.item == "semanticModel" else None)
    dest = out / "seed" / a.item
    oac.write_parts(parts, dest)
    oac.validate_parts(parts).show("exported definition")
    say(f"exported {len(parts)} parts to {dest}. Use: deploy --seed {dest} --replace --execute")
    return 0


def cmd_agents(a: argparse.Namespace) -> int:
    cfg = load_config(a.config, state_dir=state_dir_of(a))
    out = Path(a.out)
    st = read_state(out)
    model = load_model(ROOT / "model")
    tier = a.tier or cfg["ontology"]["tier"]
    sp = spec_mod.make_spec(model, tier, a.descriptions)
    golden = json.loads((out / "eval" / "golden_expected.json").read_text(encoding="utf-8")) if (out / "eval" / "golden_expected.json").exists() else []
    files = agents_mod.build_pack(sp, golden, oac.build_brief(sp), st.get("workspaceId") or "<WORKSPACE_ID>", st.get("ontologyId") or "<ONTOLOGY_ID>")
    write_tree(files, out / tier / "agents")
    pack = agents_mod.foundry_iq_pack(st.get("workspaceId") or "<WORKSPACE_ID>", st.get("ontologyId") or "<ONTOLOGY_ID>",
                                      cfg["foundryIq"]["knowledgeSource"], cfg["foundryIq"]["knowledgeBase"])
    say(f"agent pack written to {out / tier / 'agents'}")
    ep = os.environ.get(cfg["foundryIq"]["searchEndpointEnv"], "").rstrip("/")
    say("\nFoundry IQ (Azure AI Search) calls:")
    for line in pack["rest"]:
        say("  " + line.replace("{AZURE_AI_SEARCH_ENDPOINT}", ep or "<search endpoint>"))
    if not a.execute:
        say("DRY RUN - nothing sent. Set the search endpoint env var, create the ontology first, then --execute.")
        return 0
    if not (ep and st.get("ontologyId")):
        say(f"need {cfg['foundryIq']['searchEndpointEnv']} and a deployed ontology (state)")
        return 1
    import requests
    tok = oac.default_token_fn()(fabric_ops.SEARCH_SCOPE)
    for kind, body in (("knowledgesources", pack["knowledge_source"]), ("knowledgebases", pack["knowledge_base"])):
        r = requests.put(f"{ep}/{kind}/{body['name']}?api-version={pack['api_version']}", json=body, timeout=120,
                         headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
        say(f"  PUT {kind}/{body['name']} -> {r.status_code} {r.text[:200] if r.status_code >= 300 else ''}")
        if r.status_code >= 300:
            return 1
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    cfg = load_config(a.config, state_dir=state_dir_of(a))
    out = Path(a.out)
    st = read_state(out)
    model = load_model(ROOT / "model")
    sp = spec_mod.make_spec(model, a.tier or cfg["ontology"]["tier"], a.descriptions)
    rc = 0
    if a.answers:
        exp = {g["id"]: g for g in json.loads((out / "eval" / "golden_expected.json").read_text(encoding="utf-8"))}
        ans = json.loads(Path(a.answers).read_text(encoding="utf-8"))
        ok = 0
        for gid, text in ans.items():
            g = exp.get(gid)
            if not g or "expected" not in g:
                say(f"  {gid}: unknown question")
                continue
            res = oracles.compare(g["expected"], text)
            miss = [m for m in g.get("must_include", []) if m.lower() not in text.lower()] + [m for m in g.get("must_not_include", []) if m.lower() in text.lower()]
            passed = res["pass"] and not miss
            ok += passed
            say(f"  {gid}: {'PASS' if passed else 'FAIL'}" + (f" missing={res['missing']} guard={miss}" if not passed else "") + (" (judge manually)" if g.get("judge") == "manual" else ""))
        say(f"golden: {ok}/{len(ans)} passed")
        return 0 if ok == len(ans) else 1
    if not (st.get("workspaceId") and st.get("ontologyId")):
        say("no deployed ontology in state; run `deploy --execute` first (or use --answers FILE to score collected answers)")
        return 1
    ops = make_ops()
    v = ops.verify_ontology(st["workspaceId"], st["ontologyId"], sp)
    say(json.dumps(v, indent=1))
    rc |= 0 if v["valid"] and not v["missingEntities"] else 1
    try:
        plan = load_plan(out)
    except PlanError as ex:
        plan = None
        say(f"lakehouse tables: {len(ops.list_tables(st['workspaceId'], st['lakehouseId']))} present; not compared with a plan ({ex})")
    if plan is not None:
        rep = ops.layer_report(plan, st["workspaceId"], st["lakehouseId"])
        say(f"lakehouse tables: {rep['tables']} present ({plan.summary()} planned" + (f", {len(rep['other'])} more from other builds" if rep["other"] else "") + ")")
        for layer, layer_rep in rep["layers"].items():
            say(f"  {layer}: {fabric_ops.describe_layer(layer_rep)}")
            rc |= 1 if layer_rep["missing"] else 0
    url = agents_mod.FABRIC_MCP.format(ws=st["workspaceId"], ont=st["ontologyId"])
    mcp = fabric_ops.mcp_tools_list(url, ops.token_fn(oac.FABRIC_SCOPE))
    say(f"mcp: {json.dumps(mcp)}")
    return rc


def _repo_fn(a: argparse.Namespace):
    from . import runner
    return lambda: runner.find_repo(a.repo or os.environ.get("CONTOSO_REPO"), Path(a.cache), a.allow_drift, say=say)


def cmd_preflight(a: argparse.Namespace) -> int:
    from . import runner
    cfg = load_config(a.config, state_dir=state_dir_of(a))
    return runner.preflight(cfg, azure_infra.Az(), make_ops=make_ops, repo_fn=_repo_fn(a), state_dir=state_dir_of(a), out=Path(a.out), say=say,
                            descriptions=a.descriptions, descriptions_source=a.descriptions_source)


def _env_number(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number (got {raw!r})") from None


def runner_stages() -> list[str]:
    from . import runner
    return list(runner.STAGES)


def pipeline_from_args(a: argparse.Namespace) -> Any:
    from . import runner
    return runner.Pipeline(until=a.until, gate_wait_min=a.gate_wait, gate_poll_sec=a.gate_poll, gate_timeout_min=0.0 if a.no_gate_wait else a.gate_timeout,
                           graph=a.graph, golden=a.golden, golden_parallel=a.golden_parallel, strict_golden=a.strict_golden, reload=a.reload)


def cmd_up(a: argparse.Namespace) -> int:
    from . import runner
    cfg = load_config(a.config, state_dir=state_dir_of(a))
    az = azure_infra.Az(execute=True)
    if a.fresh:
        say("--fresh: stage 0, clean up. Wiping what an earlier build of this kit left (capacity, workspace, resource group), then building from nothing.\n")
        rc = runner.down(cfg, az, make_ops=make_ops, state_dir=state_dir_of(a), out=Path(a.out), say=say)
        if rc != runner.EXIT_OK:
            say("\n--fresh: the wipe did not finish cleanly, so nothing new is built. Fix that first (bash wipe.sh shows what is left), then run build.sh again.")
            return rc
        say("\n--fresh: clean. Building from scratch.\n")
    return runner.up(cfg, az, make_ops=make_ops, repo_fn=_repo_fn(a), state_dir=state_dir_of(a), out=Path(a.out), say=say,
                     tier=a.tier, seed=a.seed, force_replace=a.force_replace, loader=a.loader, pause_after=a.pause, ont_annotations=a.ont_annotations,
                     pipeline=pipeline_from_args(a), descriptions=a.descriptions, descriptions_source=a.descriptions_source)


def cmd_diagnose(a: argparse.Namespace) -> int:
    from . import runner
    cfg = load_config(a.config, state_dir=state_dir_of(a))
    return runner.diagnose(cfg, azure_infra.Az(), make_ops=make_ops, state_dir=state_dir_of(a), out=Path(a.out), say=say)


def cmd_down(a: argparse.Namespace) -> int:
    from . import runner
    cfg = load_config(a.config, state_dir=state_dir_of(a))
    return runner.down(cfg, azure_infra.Az(execute=not a.plan), make_ops=make_ops, state_dir=state_dir_of(a), out=Path(a.out), say=say, plan_only=a.plan)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="contoso_onto", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser, repo: bool = False, execute: bool = False) -> None:
        p.add_argument("--out", help="output folder (default: kit/out; for preflight/up/diagnose/down: <state dir>/out)")
        p.add_argument("--config", help="config JSON (replaces config/demo.config.json and <state dir>/config/local.json)")
        p.add_argument("--state-dir", help=f"ledger, lock, logs (default: $CONTOSO_STATE_DIR or ~/.local/state/{PROJECT})")
        if repo:
            p.add_argument("--repo", help="path to a checkout of contoso-retail-superapp4 (read only)")
            p.add_argument("--clone", action="store_true", help=f"shallow-clone the pinned commit {REPO_COMMIT[:7]} into --cache")
            p.add_argument("--cache", help="clone cache (default: kit/.cache; for operator commands: <state dir>/cache)")
            p.add_argument("--allow-drift", action="store_true", help="accept a repo HEAD other than the pinned commit")
        if execute:
            p.add_argument("--execute", action="store_true", help="actually change Azure / Fabric (default is a dry run)")

    def descriptions(p: argparse.ArgumentParser) -> None:
        p.add_argument("--descriptions", choices=list(spec_mod.DESCRIPTION_MODES), metavar="|".join(spec_mod.DESCRIPTION_MODES),
                       help="entity descriptions of the ontology, the semantic model and the data dictionary. full (default): business meaning, standards sentence and the "
                            f"architecture clause (star role, grain, lineage). compact: business meaning and standards sentence only, each line within "
                            f"{spec_mod.ENTITY_DESCRIPTION_MAX_COMPACT} characters, the fallback for a Fabric that refuses the long text: a build that fell back "
                            f"remembers it (state key descriptionsMode) until --descriptions full is passed. Environment: {spec_mod.DESCRIPTIONS_ENV}")

    p = sub.add_parser("assess", help="read-only data-readiness assessment of the repo")
    common(p, repo=True)
    p.set_defaults(fn=cmd_assess)
    p = sub.add_parser("standards", help="BIAN / Microsoft CDM alignment of the ontology, derived from the repo's banking profile (--write updates model/standards.yaml)")
    common(p, repo=True)
    p.add_argument("--write", action="store_true", help="write model/standards.yaml")
    p.set_defaults(fn=cmd_standards)
    p = sub.add_parser("build", help="bronze manifest and seed files, silver (CDM profile) tables, gold tables, semantic model, ontology, agent pack, golden answers (offline); only what the tier needs")
    common(p, repo=True)
    descriptions(p)
    p.add_argument("--tier", choices=["smoke", "core", "full", "both"], default="both",
                   help="both = core + full; smoke = 12-entity bisect set. Each layer is written for the tier's tables only: gold = the tier's tables, silver = the tables their lineage names, bronze = the seed files those read")
    p.set_defaults(fn=cmd_build)
    p = sub.add_parser("plan", help="F4 capacity plan, name check and cost estimate")
    common(p)
    descriptions(p)
    p.add_argument("--offline", action="store_true", help="do not call az or the pricing API")
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("provision", help="resource group + F4 capacity (Bicep) + workspace + lakehouse")
    common(p, execute=True)
    p.set_defaults(fn=cmd_provision)
    p = sub.add_parser("capacity", help="capacity lifecycle: status | suspend | resume | scale | delete")
    common(p, execute=True)
    p.add_argument("action", choices=["status", "suspend", "resume", "scale", "delete"])
    p.add_argument("--sku", help="target SKU for scale, for example F8")
    p.set_defaults(fn=cmd_capacity)
    p = sub.add_parser("load", help="upload the bronze seed files and load the silver and gold tables into the lakehouse, in that order (bronze -> silver -> gold)")
    common(p, execute=True)
    p.add_argument("--loader", choices=["load-api", "delta"], default="load-api", help="load-api = Fabric Load Table API (default); delta = delta-rs direct write (fallback)")
    p.add_argument("--layers", default="bronze,silver,gold", help="comma separated layers to load: bronze, silver, gold (default: all three; they always load in that order)")
    p.add_argument("--only", help="comma separated table names (lakehouse names, from any chosen layer) to load instead of every table of the chosen layers; naming bronze_file_manifest also uploads the seed files")
    p.set_defaults(fn=cmd_load)
    p = sub.add_parser("deploy", help="create/update the Direct Lake semantic model and the Gen 2 ontology")
    common(p, execute=True)
    descriptions(p)
    p.add_argument("--tier", choices=["smoke", "core", "full"])
    p.add_argument("--replace", action="store_true", help="overwrite existing item definitions (live ontology is backed up first)")
    p.add_argument("--refresh", action="store_true", help="refresh (frame) the semantic model after creation")
    p.add_argument("--seed", help="folder of an exported, portal-authored ontology: reuse its binding expression and annotations")
    p.add_argument("--ont-annotations", action=argparse.BooleanOptionalAction, default=True,
                   help="write the ONT_* partition annotations the portal writes (default on: without them every entity is graph-ineligible, verified live); --no-ont-annotations turns them off")
    p.set_defaults(fn=cmd_deploy)
    p = sub.add_parser("export", help="export a live item definition (portal seed / backup)")
    common(p)
    p.add_argument("--item", choices=["ontology", "semanticModel"], default="ontology")
    p.add_argument("--name", default=None)
    p.add_argument("--id")
    p.add_argument("--workspace-id")
    p.set_defaults(fn=cmd_export)
    p = sub.add_parser("agents", help="agent pack + Foundry IQ knowledge source/base for the ontology")
    common(p, execute=True)
    descriptions(p)
    p.add_argument("--tier", choices=["smoke", "core", "full"])
    p.set_defaults(fn=cmd_agents)
    p = sub.add_parser("verify", help="post-deploy checks, or score collected answers against the golden set")
    common(p)
    descriptions(p)
    p.add_argument("--tier", choices=["smoke", "core", "full"])
    p.add_argument("--answers", help="JSON {questionId: answer text} to score offline")
    p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("preflight", help="read-only: what build would do and whether anything blocks it (build.sh --preflight)")
    common(p, repo=True)
    descriptions(p)
    p.set_defaults(fn=cmd_preflight)
    p = sub.add_parser("up", help="the whole build, resumable (build.sh), as stages: BUILD -> APPROVE (admin: ask, wait, poll) -> DEPLOY (ontology + graph) -> TEST (agent questions)")
    common(p, repo=True)
    descriptions(p)
    p.add_argument("--tier", choices=["smoke", "core", "full"],
                   help="ontology tier to end on (default: config ontology.tier, core); it also decides what is built and loaded: that tier's gold tables, the silver tables behind them and the seed files those read")
    p.add_argument("--seed", help="folder of an exported, portal-authored ontology to carry forward (implies --force-replace)")
    p.add_argument("--force-replace", action="store_true", help="overwrite portal edits or items this kit did not create (the live version is backed up first)")
    p.add_argument("--loader", choices=["load-api", "delta"], default="load-api")
    p.add_argument("--pause", action="store_true", help="pause the capacity when the build finishes (stops compute billing)")
    p.add_argument("--ont-annotations", action=argparse.BooleanOptionalAction, default=True,
                   help="write the ONT_* partition annotations the portal writes (default on: without them every entity is graph-ineligible, verified live); --no-ont-annotations turns them off")
    p.add_argument("--fresh", action="store_true", help="stage 0: wipe what an earlier build of this kit left (capacity, workspace, resource group), then build from nothing")
    p.add_argument("--until", choices=runner_stages(), help="stop after this stage (a later build.sh continues with the next one)")
    p.add_argument("--gate-wait", type=float, default=_env_number("CONTOSO_GATE_WAIT_MIN", 15.0), metavar="MIN",
                   help="APPROVE stage: minutes to wait after asking for the admin action before polling (settings need about 10; default 15; env CONTOSO_GATE_WAIT_MIN)")
    p.add_argument("--gate-poll", type=float, default=_env_number("CONTOSO_GATE_POLL_SEC", 60.0), metavar="SEC",
                   help="APPROVE stage: seconds between polls once polling starts (default 60; env CONTOSO_GATE_POLL_SEC)")
    p.add_argument("--gate-timeout", type=float, default=_env_number("CONTOSO_GATE_TIMEOUT_MIN", 120.0), metavar="MIN",
                   help="APPROVE stage: give up (exit 3, everything kept) after this many minutes (default 120; env CONTOSO_GATE_TIMEOUT_MIN)")
    p.add_argument("--no-gate-wait", action="store_true", help="APPROVE stage: do not wait; stop with exit 3 as soon as the gate is closed (the previous behaviour)")
    p.add_argument("--graph", action=argparse.BooleanOptionalAction, default=True,
                   help="DEPLOY stage: build the graph over REST (default on); --no-graph leaves Configure graph to the portal")
    p.add_argument("--golden", choices=["none", "smoke", "full"], default=os.environ.get("CONTOSO_GOLDEN", "smoke"),
                   help="TEST stage: ask the ontology agent its golden questions: smoke = 5 (default), full = every MCP question (about 25 minutes), none")
    p.add_argument("--golden-parallel", type=int, default=2, help="TEST stage: questions asked at the same time (4 made a third of the first attempts time out on an F4)")
    p.add_argument("--strict-golden", action="store_true", help="TEST stage: a failed golden question fails the build (exit 4); default: reported only")
    p.add_argument("--reload", action="store_true", help="BUILD stage: load every layer (bronze files, silver, gold) again even when the lakehouse already holds this data")
    p.set_defaults(fn=cmd_up)
    p = sub.add_parser("diagnose", help="read-only: ledger versus what exists (build.sh --diagnose)")
    common(p)
    p.set_defaults(fn=cmd_diagnose)
    p = sub.add_parser("down", help="hard-delete everything the ledger says this kit created (the whole resource group too), then verify (wipe.sh)")
    common(p)
    p.add_argument("--plan", action="store_true", help="read-only: list what would be deleted")
    p.set_defaults(fn=cmd_down)
    a = ap.parse_args(argv)
    sd = state_dir_of(a)
    operator = a.cmd in OPERATOR_CMDS
    if getattr(a, "out", None) is None:
        a.out = str(sd / "out" if operator else ROOT / "out")
    if hasattr(a, "cache") and a.cache is None:
        a.cache = str(sd / "cache" if operator else ROOT / ".cache")
    if a.cmd == "export" and not a.name and not a.id:
        a.name = load_config(a.config, state_dir=sd)["ontology"].get("name") or load_model(ROOT / "model")["ontology"]["name"]
    if hasattr(a, "descriptions"):                    # the commands that write entity descriptions: --descriptions, else CONTOSO_DESCRIPTIONS, else what the build state remembers, else full
        try:
            a.descriptions, a.descriptions_source = spec_mod.resolve_descriptions(
                a.descriptions, os.environ.get(spec_mod.DESCRIPTIONS_ENV), read_state(Path(a.out)).get("descriptionsMode"))
        except ValueError as ex:
            say(f"BAD CONFIG: {ex}")
            return 2
    try:
        return a.fn(a)
    except KeyboardInterrupt:
        say("\ninterrupted. Nothing is lost: everything created so far is in the ledger; re-run to resume (or wipe.sh to remove it).")
        return 4


if __name__ == "__main__":
    sys.exit(main())
