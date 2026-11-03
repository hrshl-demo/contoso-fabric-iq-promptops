"""Run every repo check in one go: python -m tools.check [--base origin/main]

Prompt lint, generated wrappers, manifest, CHANGELOG structure, PII/secret guard, Fabric artifact validation and, when the kit's requirements are
installed, that the generated Fabric items still match the spec. The kit's own tests run separately (see AGENTS.md).
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from typing import Optional

from . import export_fabric, lint_prompts, manifest, pii_guard, release, validate_fabric, wrappers


def kit_requirements_installed() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in ("pandas", "pyarrow", "requests"))


def generated_in_sync() -> int:
    if not kit_requirements_installed():
        print("skipped: the kit's requirements are not installed (pip install -r kit/requirements.txt); CI runs this check in the offline build")
        return 0
    return export_fabric.main(["--check"])


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", help="git ref to compare prompt versions against, for example origin/main")
    a = ap.parse_args(argv)
    steps = [
        ("lint prompts", lambda: lint_prompts.main(["--base", a.base] if a.base else [])),
        ("changelog", lambda: release.main(["lint"])),
        ("wrappers", lambda: wrappers.main(["--check"])),
        ("manifest", lambda: manifest.main(["--check"])),
        ("pii / secret guard", lambda: pii_guard.main([])),
        ("fabric items", lambda: validate_fabric.main([])),
        ("generated files match the spec", generated_in_sync),
    ]
    failed = []
    for name, fn in steps:
        print(f"\n== {name}")
        if fn() != 0:
            failed.append(name)
    print("\n" + ("FAILED: " + ", ".join(failed) if failed else "all checks passed"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
