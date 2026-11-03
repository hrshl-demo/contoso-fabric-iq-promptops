"""Validate the Fabric item folders under fabric/.

Every folder that holds a `.platform` file is an item (Fabric Git format). Ontologies and semantic models are checked with the kit's
own TMDL validator (contoso_onto.oac.validate_parts); reports need a PBIR definition and are validated with
`powerbi-report-author validate` when that npm tool is on PATH (CI installs it). An empty fabric/ passes.
  python -m tools.validate_fabric [--strict] [--root PATH]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .promptlib import ROOT


def find_items(fabric: Path) -> list[Path]:
    return sorted({p.parent for p in fabric.rglob(".platform")}) if fabric.is_dir() else []


def _oac(kit: Path):
    sys.path.insert(0, str(kit))
    from contoso_onto import oac  # noqa: PLC0415
    return oac


def validate_tmdl_item(item: Path, strict: bool, kit: Path) -> list[str]:
    oac = _oac(kit)
    rep = oac.validate_parts(oac.load_dir_parts(str(item)), strict=strict)
    rep.show(item.name)
    return [] if rep.ok else [f"{item.name}: TMDL validation failed"]


def validate_report_item(item: Path) -> list[str]:
    problems = []
    if not (item / "definition.pbir").exists():
        problems.append(f"{item.name}: definition.pbir is missing")
    if not (item / "definition").is_dir():
        problems.append(f"{item.name}: PBIR folder 'definition/' is missing (the legacy report.json format is not accepted)")
    tool = shutil.which("powerbi-report-author")
    if tool and not problems:
        r = subprocess.run([tool, "validate", str(item)], capture_output=True, text=True)
        print(r.stdout.strip())
        if r.returncode != 0:
            problems.append(f"{item.name}: powerbi-report-author validate failed")
    elif not tool:
        print(f"{item.name}: powerbi-report-author not installed, structural check only")
    return problems


def validate(root: Path = ROOT, strict: bool = False) -> list[str]:
    fabric = root / "fabric"
    items = find_items(fabric)
    if not items:
        print("validate_fabric: no items under fabric/ yet")
        return []
    problems: list[str] = []
    for item in items:
        try:
            meta = json.loads((item / ".platform").read_text(encoding="utf-8-sig"))
        except ValueError as ex:
            problems.append(f"{item.name}: .platform is not valid JSON ({ex})")
            continue
        kind = (meta.get("metadata") or {}).get("type")
        print(f"validate_fabric: {item.relative_to(root).as_posix()} ({kind})")
        if kind in ("Ontology", "SemanticModel"):
            problems += validate_tmdl_item(item, strict, root / "kit")
        elif kind == "Report":
            problems += validate_report_item(item)
        else:
            print(f"  no validator for item type {kind!r}")
    return problems


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--strict", action="store_true", help="treat unresolved <PLACEHOLDER> values as errors (use after parameter substitution)")
    a = ap.parse_args(argv)
    problems = validate(Path(a.root), a.strict)
    for line in problems:
        print(f"ERROR {line}")
    print(f"validate_fabric: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
