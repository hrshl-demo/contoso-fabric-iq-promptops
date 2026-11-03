"""The three layers of the lakehouse and the plan that loads them (medallion: bronze -> silver -> gold).

    bronze  the seed JSON files the silver tables read, verbatim, under OneLake Files/bronze/<release>/<repo path>, plus the table bronze_file_manifest
    silver  silver_* tables conformed to the CDM application profile (official-name alias columns included) and the two silver catalogs
    gold    the star schema dim_* / fact_* / bridge_* / ref_*: the semantic model and the ontology bind to this layer only

A LoadPlan lists what one build puts in the lakehouse, in load order: bronze, then silver, then gold. The items of a layer are independent
(they load side by side); the order between layers exists because gold is built from silver and silver from bronze, and so that a failure
shows up at the first layer that has it. The plan is plain data read from the three manifests `build` writes: no network, no Fabric.
Its fingerprints are what the build stage compares with the ones it recorded after the last load to decide which layers to load again.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

LAYERS = ("bronze", "silver", "gold")                                   # load order
BRONZE_FOLDER = "Files/bronze"                                          # in OneLake, relative to the lakehouse root
NO_RELEASE = "no-release"                                               # the OneLake folder of the seed files that belong to no release
STAGED_BRONZE = "bronze/files"                                          # under the output folder: the verbatim copies the load stage uploads
PARQUET_FOLDER = {"bronze": "bronze", "silver": "silver", "gold": "gold"}        # under the output folder: <table>.parquet
MANIFEST = {"bronze": "bronze/manifest.json", "silver": "silver/manifest.json", "gold": "manifest.json"}     # under the output folder


class PlanError(RuntimeError):
    """The output folder does not describe a loadable set of layers (or a --layers / --only selection does not fit it)."""


def bronze_target(release: Optional[str], path: str) -> str:
    """Where a seed file lands in OneLake, relative to the lakehouse root: Files/bronze/<release>/<repo-relative POSIX path>.
    The few files that belong to no release go under `no-release`."""
    p = PurePosixPath(path)
    if not path or "\\" in path or p.is_absolute() or ".." in p.parts:
        raise PlanError(f"bronze path {path!r} must be a relative POSIX path without '..'")
    if release is not None and (not release or "/" in release or "\\" in release or release in (".", "..")):
        raise PlanError(f"bronze release {release!r} must be one folder name")
    return f"{BRONZE_FOLDER}/{release or NO_RELEASE}/{p.as_posix()}"


def staged_path(out: Path, release: Optional[str], path: str) -> Path:
    """Where `build` keeps the verbatim copy of a seed file that the load stage uploads: <out>/bronze/files/<release>/<repo path>, the same
    layout as OneLake's Files/bronze."""
    rel = PurePosixPath(bronze_target(release, path)).relative_to(BRONZE_FOLDER)
    return Path(out).joinpath(*STAGED_BRONZE.split("/"), *rel.parts)


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass
class PlannedFile:
    """One bronze file: a seed JSON file uploaded verbatim."""
    release: Optional[str]
    path: str                       # repo-relative POSIX path, as in bronze_file_manifest
    target: str                     # OneLake path relative to the lakehouse root (bronze_target)
    sha256: str
    bytes: int
    records: int
    local: Path                     # the verbatim copy `build` staged under <out>/bronze/files/


@dataclass
class PlannedTable:
    """One lakehouse table, loaded from a Parquet file."""
    layer: str
    name: str                       # the lakehouse table name = the Parquet file stem
    rows: int
    bytes: int
    sha256: str
    schema: list = field(default_factory=list)
    file: Optional[Path] = None


@dataclass
class LoadPlan:
    out: Path
    files: list[PlannedFile] = field(default_factory=list)
    tables: dict[str, list[PlannedTable]] = field(default_factory=lambda: {layer: [] for layer in LAYERS})

    @classmethod
    def from_manifests(cls, bronze: dict, silver: dict, gold: dict, out: Path) -> "LoadPlan":
        """The plan the three layer manifests describe. Names must be unique across layers (every table is staged under one flat folder)."""
        plan = cls(Path(out))
        for row in sorted(bronze.get("files") or [], key=lambda r: (r.get("release") or "", r["path"])):
            plan.files.append(PlannedFile(row.get("release"), row["path"], bronze_target(row.get("release"), row["path"]), row["sha256"], int(row["bytes"]),
                                          int(row.get("record_count") or 0), staged_path(plan.out, row.get("release"), row["path"])))
        seen: dict[str, str] = {}
        for layer, manifest in zip(LAYERS, (bronze, silver, gold)):
            if manifest.get("layer", layer) != layer:
                raise PlanError(f"the {layer} manifest says it describes the {manifest.get('layer')} layer")
            for name, t in sorted((manifest.get("tables") or {}).items()):
                if name in seen:
                    raise PlanError(f"table {name} is in the {seen[name]} layer and in the {layer} layer; lakehouse table names must be unique across layers")
                seen[name] = layer
                plan.tables[layer].append(PlannedTable(layer, name, int(t["rows"]), int(t["bytes"]), t["sha256"], list(t.get("schema") or []),
                                                       plan.out / PARQUET_FOLDER[layer] / f"{name}.parquet"))
        return plan

    # ---- what is in the plan ------------------------------------------------------------------
    def names(self, layer: str) -> list[str]:
        return [t.name for t in self.tables[layer]]

    def all_names(self) -> list[str]:
        """Every table, in load order."""
        return [n for layer in LAYERS for n in self.names(layer)]

    def folder(self, layer: str) -> Path:
        return self.out / PARQUET_FOLDER[layer]

    def layers(self) -> list[str]:
        """The layers that have something to load, in load order."""
        return [layer for layer in LAYERS if self.tables[layer] or (layer == "bronze" and self.files)]

    def table_bytes(self, layer: str) -> int:
        return sum(t.bytes for t in self.tables[layer]) + (sum(f.bytes for f in self.files) if layer == "bronze" else 0)

    def counts(self) -> dict[str, int]:
        return {"bronzeFiles": len(self.files), "bronzeTables": len(self.tables["bronze"]), "silverTables": len(self.tables["silver"]),
                "goldTables": len(self.tables["gold"]), "tables": len(self.all_names())}

    def summary(self) -> str:
        return f"{len(self.files)} bronze files, {len(self.tables['silver'])} silver tables, {len(self.tables['gold'])} gold tables"

    def select(self, layers: Optional[Iterable[str]] = None, only: Optional[Iterable[str]] = None) -> "LoadPlan":
        """The part of the plan a `--layers` / `--only` selection names (load order is kept). `only` is a set of lakehouse table names from any of
        the chosen layers; the bronze files travel with the bronze table that describes them. Fingerprints are only meaningful for a whole plan."""
        chosen = list(dict.fromkeys(layers)) if layers else list(LAYERS)
        bad = [layer for layer in chosen if layer not in LAYERS]
        if bad:
            raise PlanError(f"unknown layer {', '.join(map(repr, bad))}; the layers are {', '.join(LAYERS)}")
        wanted = set(only or ())
        unknown = sorted(wanted - {n for layer in chosen for n in self.names(layer)})
        if unknown:
            raise PlanError(f"no table named {', '.join(unknown)} in the {'/'.join(chosen)} layer(s) of {self.out}")
        sub = LoadPlan(self.out)
        for layer in LAYERS:
            if layer in chosen:
                sub.tables[layer] = [t for t in self.tables[layer] if not wanted or t.name in wanted]
        if "bronze" in chosen and (not wanted or sub.tables["bronze"]):
            sub.files = list(self.files)
        return sub

    # ---- what changed since the last load -----------------------------------------------------
    def fingerprints(self) -> dict[str, str]:
        """One fingerprint per layer. A layer's covers its own manifest (table names, row counts, sha256, schema; for bronze also every file's
        path, hash and size) and the fingerprint of the layer before it. So a change in bronze changes all three, a change in silver changes
        silver and gold (gold is built from silver), and a change in gold alone leaves bronze and silver as they were."""
        out: dict[str, str] = {}
        upstream = ""
        for layer in LAYERS:
            part: dict[str, Any] = {"layer": layer, "upstream": upstream,
                                    "tables": [[t.name, t.rows, t.sha256, t.schema] for t in self.tables[layer]]}
            if layer == "bronze":
                part["files"] = [[f.target, f.sha256, f.bytes, f.records] for f in self.files]
            out[layer] = upstream = _digest(part)
        return out

    def fingerprint(self) -> str:
        """One fingerprint for the whole plan (the state file's `loadedFingerprint`)."""
        fps = self.fingerprints()
        return _digest([fps[layer] for layer in LAYERS])


def load_plan(out: Path) -> LoadPlan:
    """Read the three manifests `build` wrote under `out` (bronze/manifest.json, silver/manifest.json, manifest.json) into a LoadPlan."""
    out = Path(out)
    manifests: list[dict] = []
    for layer in LAYERS:
        p = out / MANIFEST[layer]
        if not p.is_file():
            raise PlanError(f"no {layer} layer in {out}: {MANIFEST[layer]} is missing; run `build` first (it writes bronze, silver and gold)")
        manifests.append(json.loads(p.read_text(encoding="utf-8")))
    return LoadPlan.from_manifests(manifests[0], manifests[1], manifests[2], out)
