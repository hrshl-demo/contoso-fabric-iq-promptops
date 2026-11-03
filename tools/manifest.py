"""manifest.lock.json: which prompt version produced which artifacts.

For every prompt it records the version, a hash of the prompt (frontmatter without the version, plus the body) and a tree hash
of the files its `outputs` globs match. A pull request that changes a prompt or an artifact shows the change in this file, and
the release notes are built from it. The file is deterministic (no timestamps, line endings normalised).

  python -m tools.manifest --write    refresh
  python -m tools.manifest --check    fail if it is stale
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Optional

import yaml

from .promptlib import ROOT, content_hash, load_prompts

MANIFEST = "manifest.lock.json"


def file_digest(path: Path) -> str:
    data = path.read_bytes()
    if b"\0" not in data[:8000]:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def tree_digest(root: Path, patterns: list[str]) -> dict:
    files: set[str] = set()
    for pat in patterns:
        pat = pat + "/*" if pat.endswith("/**") else pat
        for p in root.glob(pat):
            if p.is_file():
                files.add(p.relative_to(root).as_posix())
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(f"{rel}\0{file_digest(root / rel)}\n".encode("utf-8"))
    return {"files": len(files), "sha256": h.hexdigest()}


def build(root: Path = ROOT) -> dict:
    lock = yaml.safe_load((root / "skills.lock").read_text(encoding="utf-8")) or {}
    m = lock.get("marketplace") or {}
    prompts, _ = load_prompts(root)
    entries = {}
    for p in sorted(prompts, key=lambda x: x.id):
        entries[p.id] = {
            "version": p.version,
            "path": p.path,
            "promptSha256": content_hash(p),
            "outputs": tree_digest(root, [str(o) for o in (p.meta.get("outputs") or [])]),
        }
    return {
        "schema": 1,
        "skills": {"repo": m.get("repo"), "ref": m.get("ref"), "commit": m.get("commit")},
        "prompts": entries,
    }


def render(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    root = Path(a.root)
    text = render(build(root))
    target = root / MANIFEST
    if a.write:
        target.write_text(text, encoding="utf-8", newline="\n")
        print(f"manifest: wrote {MANIFEST}")
        return 0
    have = target.read_text(encoding="utf-8").replace("\r\n", "\n") if target.exists() else None
    if have != text:
        print(f"manifest: {MANIFEST} is {'missing' if have is None else 'stale'}; run python -m tools.manifest --write and commit it")
        return 1
    print("manifest: up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
