"""Materialize what the kit generates as files in this repo, and check that they stay in sync with the spec.

The kit (kit/contoso_onto) generates the Direct Lake semantic model, the Gen 2 ontology and the agent grounding brief from the
YAML spec in kit/model/. This tool runs that build offline and writes the results as Fabric Git-format item folders:

  fabric/ontology/<OntologyName>.Ontology/               ontology item (TMDL), logicalId made stable
  fabric/semantic-model/<ModelName>.SemanticModel/       semantic model item (TMDL), with a generated .platform
  docs/generated/ontology-brief.md                        readable view of every entity, synonym and PII flag, for reviews

These three locations are owned by this tool: never edit them by hand, change the spec and regenerate.
fabric/reports/ is not touched (reports are authored with the Power BI tooling).

  python -m tools.export_fabric --write              build with the kit, then write
  python -m tools.export_fabric --check              build with the kit, then fail if the repo differs
  python -m tools.export_fabric --check --built out  compare with an existing kit build folder (the kit's --out) instead of building
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from .promptlib import ROOT

PLATFORM_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json"
ID_NAMESPACE = "urn:contoso-fabric-iq-promptops"
MANAGED_DIRS = ("fabric/ontology", "fabric/semantic-model", "docs/generated")
BRIEF_PATH = "docs/generated/ontology-brief.md"


class ExportError(RuntimeError):
    """The kit build failed or its output is not what this tool expects."""


def logical_id(kind: str, name: str) -> str:
    """A stable id per item, so deployment tooling can tell the ontology from the model and re-runs never change it."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{ID_NAMESPACE}:{kind}:{name}"))


def platform_text(kind: str, name: str) -> str:
    doc = {"$schema": PLATFORM_SCHEMA, "metadata": {"type": kind, "displayName": name},
           "config": {"version": "2.0", "logicalId": logical_id(kind, name)}}
    return json.dumps(doc, indent=2) + "\n"


def read_tier(root: Path = ROOT) -> str:
    cfg = json.loads((root / "kit" / "config" / "demo.config.json").read_text(encoding="utf-8"))
    return (cfg.get("ontology") or {}).get("tier") or "core"


def run_build(root: Path, tier: str, out: Path, hashseed: str = "0", repo: Optional[Path] = None) -> Path:
    """Run the kit's offline build into `out` and return the folder of the tier. `repo` is the seed folder to build from (default: seed/ in this repo;
    the platform pipeline passes the copy it downloaded from the data source)."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONHASHSEED": hashseed}
    cmd = [sys.executable, "-m", "contoso_onto.cli", "build", "--repo", str(repo or root / "seed"), "--tier", tier, "--out", str(out)]
    r = subprocess.run(cmd, cwd=root / "kit", env=env, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise ExportError(f"the kit build failed (exit {r.returncode}):\n{(r.stdout + r.stderr)[-3000:]}")
    return out / tier


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def expected_files(built: Path) -> dict[str, str]:
    """Repo-relative path -> text, from the kit build folder of one tier."""
    files: dict[str, str] = {}
    ont, sm = built / "ontology", built / "semantic_model"
    brief, payload = built / "agents" / "grounding_brief.md", built / "payloads" / "semantic_model.create.json"
    for needed in (ont / ".platform", sm / "definition.pbism", brief, payload):
        if not needed.is_file():
            raise ExportError(f"the kit build is missing {needed.relative_to(built.parent).as_posix()}; is {built} a build folder?")

    ont_name = json.loads(_read(ont / ".platform"))["metadata"]["displayName"]
    base = f"fabric/ontology/{ont_name}.Ontology"
    for f in sorted(p for p in ont.rglob("*") if p.is_file()):
        rel = f.relative_to(ont).as_posix()
        files[f"{base}/{rel}"] = platform_text("Ontology", ont_name) if rel == ".platform" else _read(f)

    sm_name = json.loads(_read(payload))["displayName"]
    base = f"fabric/semantic-model/{sm_name}.SemanticModel"
    files[f"{base}/.platform"] = platform_text("SemanticModel", sm_name)
    for f in sorted(p for p in sm.rglob("*") if p.is_file()):
        files[f"{base}/{f.relative_to(sm).as_posix()}"] = _read(f)

    files[BRIEF_PATH] = _read(brief)
    return files


def current_files(root: Path = ROOT) -> dict[str, str]:
    files: dict[str, str] = {}
    for d in MANAGED_DIRS:
        base = root / d
        for f in sorted(p for p in base.rglob("*") if p.is_file()) if base.is_dir() else []:
            files[f.relative_to(root).as_posix()] = _read(f)
    return files


def diff(expected: dict[str, str], current: dict[str, str]) -> dict[str, list[str]]:
    return {
        "missing": sorted(set(expected) - set(current)),
        "changed": sorted(p for p in set(expected) & set(current) if expected[p] != current[p]),
        "stray": sorted(set(current) - set(expected)),
    }


def write(root: Path, expected: dict[str, str]) -> None:
    for d in MANAGED_DIRS:
        base = root / d
        for f in [p for p in base.rglob("*") if p.is_file()] if base.is_dir() else []:
            if f.relative_to(root).as_posix() not in expected:
                f.unlink()
        for sub in sorted((p for p in base.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True) if base.is_dir() else []:
            if not any(sub.iterdir()):
                sub.rmdir()
    for rel, text in expected.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--tier", help="smoke | core | full (default: ontology.tier in kit/config/demo.config.json)")
    ap.add_argument("--built", help="the kit's build output folder (its --out); skips the build")
    a = ap.parse_args(argv)
    root = Path(a.root)
    tier = a.tier or read_tier(root)
    try:
        if a.built:
            expected = expected_files(Path(a.built) / tier)
        else:
            with tempfile.TemporaryDirectory(prefix="export-fabric-") as tmp:
                expected = expected_files(run_build(root, tier, Path(tmp)))
    except ExportError as ex:
        print(f"export_fabric: {ex}", file=sys.stderr)
        return 2

    if a.write:
        write(root, expected)
        print(f"export_fabric: wrote {len(expected)} file(s) for tier {tier}")
        return 0

    d = diff(expected, current_files(root))
    for label, paths in d.items():
        for p in paths[:20]:
            print(f"  {label}: {p}")
        if len(paths) > 20:
            print(f"  {label}: ... and {len(paths) - 20} more")
    n = sum(len(v) for v in d.values())
    if n:
        print(f"export_fabric: {n} difference(s) from the spec (tier {tier}). Never edit generated files by hand: change kit/model/ and run python -m tools.export_fabric --write")
        return 1
    print(f"export_fabric: in sync with the spec ({len(expected)} files, tier {tier})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
