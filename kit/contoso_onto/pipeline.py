"""Build the layers: silver -> gold star schema -> validate -> write Parquet + manifests + data-quality report, and cut every layer down to what a tier needs."""
from __future__ import annotations

import collections
import hashlib
import json
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Optional

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from . import REPO_COMMIT
from . import layers as layers_mod
from . import silver as silver_mod
from . import spec as spec_mod
from .gold import (DEFAULT_REF_PREFIXES, KIND_PREFIX, KINDS, MODEL_DIR, SNAKE_RE, GoldError, Repo, SilverFrames, TableResult, build_rows,
                   check_silver_paths, expand_columns, physical_names, to_arrow)
from .hooks import HOOKS
from .snapshot import git_head

# who the personas are: an ownerId that is a persona in more than one release is namespaced in every non-canonical release
DEFAULT_IDENTITY = {"table": "silver_customer", "key": "ownerId"}


@dataclass
class GoldBuild:
    results: dict[str, TableResult]
    tables: dict[str, pa.Table]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    silver: Any = None                       # the silver_mod.SilverBuild gold was built from (None when every table is a reference table)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_star(model: dict, silver_spec: Optional[dict] = None) -> list[str]:
    """The star metadata every gold table carries: kind, grain, physical name, and either the silver tables it is built from or
    `reference: true`. Pure spec checks, no data. With the silver spec, every named silver table must exist and serve the gold table."""
    errors: list[str] = []
    silver_by_name = {t["table"]: t for t in silver_spec["tables"]} if silver_spec else None
    logical = {t["table"] for t in model["tables"]}
    physical_of: dict[str, str] = {}
    for t in model["tables"]:
        n, kind, phys = t["table"], t.get("kind"), t.get("physical")
        if kind not in KINDS:
            errors.append(f"{n}: kind must be one of {', '.join(KINDS)} (got {kind!r})")
        if not isinstance(t.get("grain"), str) or not t["grain"].strip():
            errors.append(f"{n}: grain must be one sentence")
        if not isinstance(phys, str) or not SNAKE_RE.match(phys) or len(phys) > 60:
            errors.append(f"{n}: physical must be a snake_case lakehouse table name (<=60 characters), got {phys!r}")
        else:
            if kind in KIND_PREFIX and not phys.startswith(KIND_PREFIX[kind]):
                errors.append(f"{n}: a {kind} is called {KIND_PREFIX[kind]}* in the lakehouse (got {phys})")
            if phys in physical_of:
                errors.append(f"{n}: physical name {phys} is also used by {physical_of[phys]}")
            if phys in logical and phys != n:
                errors.append(f"{n}: physical name {phys} is the logical id of another table")
            physical_of.setdefault(phys, n)
        sources = t.get("sources") or []
        names, reference = t.get("silver"), t.get("reference")
        if bool(names) == bool(reference):
            errors.append(f"{n}: declare exactly one of `silver: [tables]` and `reference: true`")
        elif names:
            used = [s.get("silver") for s in sources]
            if not isinstance(names, list) or None in used:
                errors.append(f"{n}: `silver` is a list and every source of a silver-fed table is {{silver: <table>}}")
            elif set(used) != set(names):
                errors.append(f"{n}: `silver` {sorted(names)} differs from the silver tables its sources read {sorted(set(used))}")
            for s in names if isinstance(names, list) else []:
                if silver_by_name is not None and s not in silver_by_name:
                    errors.append(f"{n}: silver table {s} does not exist")
                elif silver_by_name is not None and n not in silver_by_name[s].get("serves", []):
                    errors.append(f"{n}: silver table {s} does not list it under `serves`")
        elif reference is not True or any("silver" in s for s in sources):
            errors.append(f"{n}: `reference: true` tables read curated docs/config sources, never silver")
        cols = {c["col"] for c in expand_columns(t)}
        errors.extend(f"{n}: grain_key column {c} does not exist" for c in t.get("grain_key") or [] if c not in cols)
        if t.get("expected_merged") is not None and not t.get("allow_duplicates"):
            errors.append(f"{n}: expected_merged needs allow_duplicates")
        errors.extend(f"{n}.{c['col']}: `none` needs the reason no silver column carries it" for c in expand_columns(t)
                      if "none" in c and not (isinstance(c["none"], str) and c["none"].strip()))
    return errors


def build_silver_for(model: dict, repo_root: Path, model_dir: Optional[Path] = None) -> Any:
    """Silver, built once from the committed silver specs (model/silver/*.yaml), for a gold build that was not handed one."""
    return silver_mod.build_silver(model, silver_mod.load_silver_spec(Path(model_dir) if model_dir else MODEL_DIR), repo_root)


def build_gold(model: dict, repo_root: Path, silver: Any = None, model_dir: Optional[Path] = None) -> GoldBuild:
    """Gold from silver. `silver` is a silver_mod.SilverBuild; without one the silver layer is built here, once, and every silver-fed
    table reads that one build. Reference tables read their curated sources from the repo."""
    repo = Repo(repo_root, model.get("canonical_release", "retail-frontier-v1"), model.get("release_short"))
    prefixes = {**DEFAULT_REF_PREFIXES, **(model.get("ref_prefixes") or {})}
    out = GoldBuild({}, {})
    frames: Optional[SilverFrames] = None
    if any("silver" in s for t in model["tables"] for s in t.get("sources", [])):
        silver = silver if silver is not None else build_silver_for(model, repo_root, model_dir)
        out.silver = silver
        if not silver.ok:
            out.errors.extend(f"silver: {e}" for e in silver.errors[:10])
            return out
        identity = model.get("identity") or DEFAULT_IDENTITY
        frames = SilverFrames.from_tables(silver.tables, model.get("canonical_release", "retail-frontier-v1"), model.get("release_short"),
                                          identity if identity["table"] in silver.tables else None)
    out.errors.extend(validate_star(model, silver.spec if silver is not None else None))
    names = set()
    for t in model["tables"]:
        if t["table"] in names:
            out.errors.append(f"duplicate table {t['table']}")
            continue
        names.add(t["table"])
        if not SNAKE_RE.match(t["table"]):
            out.errors.append(f"table name {t['table']} must be snake_case")
        if frames is not None:
            bad = check_silver_paths(t, frames)
            if bad:
                out.errors.extend(bad[:10])
                continue
        try:
            res = build_rows(t, repo, prefixes, HOOKS, frames)
        except (GoldError, KeyError, FileNotFoundError) as ex:
            out.errors.append(f"{t['table']}: {ex}")
            continue
        out.errors.extend(res.errors[:10])
        if len(res.errors) > 10:
            out.errors.append(f"{t['table']}: ... {len(res.errors) - 10} more extraction errors")
        if res.dropped_duplicates and not t.get("allow_duplicates"):
            out.errors.append(f"{t['table']}: {res.dropped_duplicates} duplicate key rows were dropped (set allow_duplicates only when expected)")
        elif res.dropped_duplicates:
            out.warnings.append(f"{t['table']}: {res.dropped_duplicates} duplicate key rows merged by precedence (expected)")
        expected = t.get("expected_merged")
        if t.get("allow_duplicates") and expected is not None and res.dropped_duplicates != expected:
            out.errors.append(f"{t['table']}: {res.dropped_duplicates} duplicate key rows were merged by precedence, the spec documents {expected}")
        if not res.rows:
            out.warnings.append(f"{t['table']}: no rows")
        for c in res.columns:
            if not SNAKE_RE.match(c["col"]) or len(c["col"]) > 60:
                out.errors.append(f"{t['table']}.{c['col']}: column must be snake_case ASCII (<=60)")
        if not res.errors:
            out.tables[t["table"]] = to_arrow(res)
        out.results[t["table"]] = res
    validate_gold(model, out)
    return out


def _col(res: TableResult, name: str) -> list:
    return [r[name] for r in res.rows]


def validate_gold(model: dict, build: GoldBuild) -> None:
    by_table = {t["table"]: t for t in model["tables"]}
    for rel in model.get("relationships", []):
        ft, fc = rel["from"].split(".")
        tt, tc = rel["to"].split(".")
        if ft not in build.results or tt not in build.results:
            continue
        try:
            fv = [v for v in _col(build.results[ft], fc) if v is not None]
            tv = set(_col(build.results[tt], tc))
        except KeyError as ex:
            build.errors.append(f"relationship {rel['name']}: unknown column {ex}")
            continue
        orphans = sorted({v for v in fv if v not in tv})
        if orphans:
            msg = f"relationship '{rel['name']}': {len(orphans)} orphan value(s) in {ft}.{fc}, e.g. {orphans[:3]}"
            (build.warnings if rel.get("allow_orphans") else build.errors).append(msg)
    for name, res in build.results.items():
        cols = {c["col"] for c in res.columns}
        for c in res.columns:
            if c["col"].endswith("_paise"):
                twin = c["col"][:-6] + "_inr"
                if twin in cols:
                    bad = [r for r in res.rows if r[c["col"]] is not None and r[twin] is not None and round(r[twin] * 100) != r[c["col"]]]
                    if bad:
                        build.errors.append(f"{name}.{twin}: {len(bad)} rows where rupees*100 != paise")
        key = next((c["col"] for c in res.columns if c.get("key")), None)
        if key is None:
            build.errors.append(f"{name}: no key column")
        elif any(v is None for v in _col(res, key)):
            build.errors.append(f"{name}: null key values")
    if not by_table:
        build.errors.append("model has no tables")


# --------------------------------------------------------------------------------------------------
# data-quality profile + outputs
# --------------------------------------------------------------------------------------------------
def dq_profile(res: TableResult) -> dict:
    cols = {}
    n = len(res.rows)
    for c in res.columns:
        vals = [r[c["col"]] for r in res.rows]
        nonnull = [v for v in vals if v is not None]
        info: dict[str, Any] = {"type": c["type"], "nullPct": round(100.0 * (n - len(nonnull)) / n, 1) if n else None,
                                "distinct": len(set(nonnull))}
        if nonnull and c["type"] in ("int", "double"):
            info.update(min=min(nonnull), max=max(nonnull))
        elif nonnull and c["type"] in ("date", "datetime"):
            info.update(min=str(min(nonnull)), max=str(max(nonnull)))
        elif nonnull and c["type"] == "string" and info["distinct"] <= 12:
            info["values"] = dict(collections.Counter(nonnull).most_common(12))
        if c.get("pii"):
            info["pii"] = True
        if c.get("ontology") is False:
            info["restricted"] = True
        cols[c["col"]] = info
    return {"rows": n, "columns": cols, "droppedDuplicates": res.dropped_duplicates, "sources": res.sources}


def file_sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def schema_of(tbl: pa.Table) -> list[dict]:
    return [{"name": f.name, "type": str(f.type), "nullable": f.nullable} for f in tbl.schema]


def write_gold(model: dict, build: GoldBuild, out_dir: Path, repo_root: Optional[Path] = None, model_dir: Optional[Path] = None,
               only: Optional[Iterable[str]] = None) -> dict:
    """Write the gold layer under their lakehouse (physical) names: out/gold/<physical>.parquet, and out/manifest.json (keyed by physical
    name, every entry also carrying the logical `table` id), out/dq_report.json. `only` (logical ids) writes just those tables, which is how
    a tier gets its own gold; the default is every table. A Parquet file that is not part of this build (for example one a v1 build left under
    its old name, or a table of a larger tier) is removed, because the load stage loads what the manifest lists."""
    chosen = None if only is None else set(only)
    if chosen is not None and chosen - set(build.tables):
        raise ValueError(f"cannot write gold table(s) that were not built: {', '.join(sorted(chosen - set(build.tables)))}")
    gold_dir = out_dir / "gold"
    gold_dir.mkdir(parents=True, exist_ok=True)
    by_table = {t["table"]: t for t in model["tables"]}
    manifest: dict[str, Any] = {"layer": "gold", "name": model["name"], "tables": {}, "repoCommitPinned": REPO_COMMIT}
    if repo_root:
        manifest["repoHead"] = git_head(Path(repo_root))
        manifest["releases"] = {}
        for rel in sorted((Path(repo_root) / "data" / "releases").iterdir()):
            rj = rel / "release.json"
            if rj.exists():
                meta = json.loads(rj.read_text(encoding="utf-8-sig"))
                manifest["releases"][rel.name] = {"status": meta.get("releaseStatus"), "businessDate": meta.get("businessDate"),
                                                  "contentManifestSha256": (meta.get("approval") or {}).get("contentManifestSha256")}
    if model_dir:
        manifest["modelFiles"] = {p.relative_to(model_dir).as_posix(): file_sha256(p) for p in sorted(Path(model_dir).rglob("*.yaml"))}
    dq = {}
    registry = physical_names(model)
    for name, tbl in build.tables.items():
        if chosen is not None and name not in chosen:
            continue
        spec = by_table[name]
        phys = registry[name]
        p = gold_dir / f"{phys}.parquet"
        pq.write_table(tbl, p, compression="snappy", version="2.6")
        lineage = {"silver": list(spec["silver"])} if spec.get("silver") else {"reference": True} if spec.get("reference") else {}
        manifest["tables"][phys] = {"table": name, "physical": phys, "kind": spec.get("kind"), "grain": spec.get("grain"), **lineage,
                                    "rows": tbl.num_rows, "columns": tbl.num_columns, "sha256": file_sha256(p), "bytes": p.stat().st_size,
                                    "schema": schema_of(tbl)}
        dq[phys] = {"table": name, **dq_profile(build.results[name])}
    for stale in gold_dir.glob("*.parquet"):
        if stale.stem not in manifest["tables"]:
            stale.unlink()
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    (out_dir / "dq_report.json").write_text(json.dumps({"errors": build.errors, "warnings": build.warnings, "tables": dq},
                                                       indent=1, default=str), encoding="utf-8")
    return manifest


# --------------------------------------------------------------------------------------------------
# layers: what a tier needs of each, and writing it
# --------------------------------------------------------------------------------------------------
@dataclass
class LayerSelection:
    """What a tier needs of each layer, computed from the specifications alone (no data is read). gold: the tables the tier's specification
    holds (logical ids in spec order, and their lakehouse names); silver: exactly the silver tables the `silver:` lineage of those gold tables
    names, in silver spec order (the two catalogs come on top); bronze: the seed JSON files those silver tables read."""
    tier: str
    gold: list[str]
    gold_physical: list[str]
    silver: list[str]
    bronze: list[str]

    @property
    def silver_tables(self) -> list[str]:
        """Everything written and loaded for silver: the lineage tables and the two catalogs."""
        return [*self.silver, silver_mod.ENTITY_CATALOG, silver_mod.ATTRIBUTE_CATALOG]

    def counts(self) -> dict[str, int]:
        return {"bronzeFiles": len(self.bronze), "silverTables": len(self.silver_tables), "goldTables": len(self.gold)}

    def summary(self) -> str:
        c = self.counts()
        return f"{c['bronzeFiles']} bronze files, {c['silverTables']} silver tables, {c['goldTables']} gold tables"


def layers_for(tier: str, gold_tables: list[dict], silver_spec: dict) -> LayerSelection:
    """The layers behind `gold_tables` (the tier's table specs: `table`, `physical`, and `silver`, the list of silver tables the table is built
    from; reference tables have none). A silver table the lineage names but the silver spec does not define is an error."""
    by_name = {t["table"]: t for t in silver_spec["tables"]}
    wanted = {s for t in gold_tables for s in t.get("silver") or []}
    unknown = sorted(wanted - set(by_name))
    if unknown:
        raise ValueError(f"the silver lineage of the gold tables names silver table(s) the silver spec does not define: {', '.join(unknown)}")
    silver = [name for name in by_name if name in wanted]
    bronze = silver_mod.bronze_paths({**silver_spec, "tables": [by_name[n] for n in silver]})
    return LayerSelection(tier, [t["table"] for t in gold_tables], [t.get("physical") or t["table"] for t in gold_tables], silver, bronze)


def select_layers(model: dict, silver_spec: dict, tier: str) -> LayerSelection:
    """The layers one tier needs. The gold tables are the tier's own (spec.make_spec); `both` (the build default) is core plus full, which is full."""
    tier = "full" if tier == "both" else tier
    return layers_for(tier, spec_mod.make_spec(model, tier)["tables"], silver_spec)


def tier_selections(model: dict, silver_spec: dict) -> dict[str, LayerSelection]:
    return {tier: select_layers(model, silver_spec, tier) for tier in spec_mod.TIERS}


def slice_silver(build: Any, keep: Iterable[str]) -> Any:
    """The silver build cut down to the tables `keep`: each catalog keeps only the rows that describe those tables and the bronze rows only the
    seed files they read, so every manifest describes exactly what is written."""
    keep = set(keep)
    if keep - set(build.tables):
        raise ValueError(f"silver tables that were not built: {', '.join(sorted(keep - set(build.tables)))}")
    catalogs = (silver_mod.ENTITY_CATALOG, silver_mod.ATTRIBUTE_CATALOG)
    spec = {**build.spec, "tables": [t for t in build.spec["tables"] if t["table"] in keep]}
    tables = {n: t for n, t in build.tables.items() if n in keep or n in catalogs}
    value_set = pa.array(sorted(keep), type=pa.string())
    for name in catalogs:
        tables[name] = tables[name].filter(pc.is_in(tables[name]["silver_table"], value_set=value_set))
    paths = set(silver_mod.bronze_paths(spec))
    return replace(build, spec=spec, tables=tables, results={n: r for n, r in build.results.items() if n in keep},
                   bronze=[r for r in build.bronze if r["path"] in paths])


def stage_bronze(out_dir: Path, rows: list[dict], repo_root: Path) -> None:
    """Copy the seed files the bronze manifest names, byte for byte, to <out>/bronze/files/<release>/<repo path> (the layout of OneLake's
    Files/bronze) so that the load stage uploads exactly what was hashed, whatever happens to the source checkout. Each copy is checked against
    the manifest hash; staged files that are no longer part of the build are removed."""
    root = Path(out_dir).joinpath(*layers_mod.STAGED_BRONZE.split("/"))
    keep: set[Path] = set()
    for row in rows:
        dest = layers_mod.staged_path(out_dir, row["release"], row["path"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(repo_root) / row["path"], dest)
        if file_sha256(dest) != row["sha256"]:
            raise GoldError(f"{row['path']} changed while it was being staged (its hash is not the one in the bronze manifest)")
        keep.add(dest)
    for p in sorted(root.rglob("*"), reverse=True):                    # deepest first, so emptied folders go too
        if p.is_file() and p not in keep:
            p.unlink()
        elif p.is_dir() and not any(p.iterdir()):
            p.rmdir()


@dataclass
class LayerOutput:
    """The three manifests `write_layers` wrote (bronze/manifest.json, silver/manifest.json, manifest.json) and the selection behind them."""
    selection: LayerSelection
    bronze: dict
    silver: dict
    gold: dict


def write_layers(model: dict, built: GoldBuild, out_dir: Path, repo_root: Path, model_dir: Optional[Path] = None, tier: str = "both") -> LayerOutput:
    """Write the three layers one tier needs, and nothing else: the tier's gold tables (out/gold, out/manifest.json), exactly the silver tables
    their `silver:` lineage names plus the two catalogs cut down to those tables (out/silver), and the bronze manifest and verbatim seed files
    those silver tables read (out/bronze). Every writer removes what an earlier, larger build left in its own folder (write_gold, silver's
    write_silver and write_bronze for the Parquet files, stage_bronze for the seed files), so each folder always matches its manifest and a
    load that globs a folder cannot find a table or a file of another tier."""
    out_dir = Path(out_dir)
    silver_build = built.silver or build_silver_for(model, repo_root, model_dir)
    selection = select_layers(model, silver_build.spec, tier)
    gold = write_gold(model, built, out_dir, repo_root, model_dir, only=selection.gold)
    sliced = slice_silver(silver_build, selection.silver)
    bronze = silver_mod.write_bronze(sliced, out_dir)
    silver = silver_mod.write_silver(sliced, out_dir, model_dir)
    stage_bronze(out_dir, sliced.bronze, repo_root)
    return LayerOutput(selection, bronze, silver, gold)
