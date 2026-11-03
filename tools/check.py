"""Run every repo check in one go: python -m tools.check [--base origin/main]

Prompt lint, generated wrappers, manifest, PII/secret guard and Fabric artifact validation. The kit's own tests run separately
(see AGENTS.md).
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from . import lint_prompts, manifest, pii_guard, validate_fabric, wrappers


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", help="git ref to compare prompt versions against, for example origin/main")
    a = ap.parse_args(argv)
    steps = [
        ("lint prompts", lambda: lint_prompts.main(["--base", a.base] if a.base else [])),
        ("wrappers", lambda: wrappers.main(["--check"])),
        ("manifest", lambda: manifest.main(["--check"])),
        ("pii / secret guard", lambda: pii_guard.main([])),
        ("fabric items", lambda: validate_fabric.main([])),
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
