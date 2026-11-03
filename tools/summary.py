"""Markdown summary of the prompts and what they produce, for the GitHub job summary: python -m tools.summary >> $GITHUB_STEP_SUMMARY"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from . import manifest
from .promptlib import ROOT


def render(root: Path = ROOT) -> str:
    data = manifest.build(root)
    skills = data["skills"]
    lines = [
        "### Prompts in this change set",
        "",
        "| Prompt | Version | Output files | Output hash |",
        "|---|---|---:|---|",
    ]
    for pid, e in data["prompts"].items():
        lines.append(f"| `{pid}` | {e['version']} | {e['outputs']['files']} | `{e['outputs']['sha256'][:12]}` |")
    lines += ["", f"Skills bundle: `{skills['repo']}` {skills['ref']} (`{(skills['commit'] or '')[:7]}`)", ""]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    sys.stdout.write(render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
