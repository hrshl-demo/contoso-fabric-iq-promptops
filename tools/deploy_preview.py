"""Preview of what deploying fabric/ would create, replace or remove, compared with a base git ref.

Offline: no credentials and no Fabric call. Used by CI to show the effect of a pull request before anything is deployed.

  python -m tools.deploy_preview --base origin/main

Ontology and semantic-model items are summarised structurally (entities, properties, relationships, rules, tables, measures);
reports by pages and visuals; anything else by files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Optional

from .promptlib import ROOT

Items = dict[tuple[str, str], dict[str, str]]       # (type, name) -> {path relative to the item: text}
MAX_NAMES = 12


def _oac():
    sys.path.insert(0, str(ROOT / "kit"))
    from contoso_onto import oac  # noqa: PLC0415
    return oac


def find_items(fabric: Path) -> Items:
    items: Items = {}
    if not fabric.is_dir():
        return items
    for plat in sorted(fabric.rglob(".platform")):
        folder = plat.parent
        try:
            meta = json.loads(plat.read_text(encoding="utf-8-sig")).get("metadata") or {}
        except ValueError:
            continue
        key = (str(meta.get("type", "Unknown")), str(meta.get("displayName", folder.name)))
        files: dict[str, str] = {}
        for f in sorted(p for p in folder.rglob("*") if p.is_file()):
            raw = f.read_bytes()
            text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n") if b"\0" not in raw[:8000] else f"<binary {hashlib.sha256(raw).hexdigest()}>"
            files[f.relative_to(folder).as_posix()] = text
        items[key] = files
    return items


def base_items(root: Path, base: str) -> Optional[Items]:
    """The items at a git ref, or None when the ref does not exist. A ref without fabric/ gives no items."""
    ok = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], capture_output=True, text=True)
    if ok.returncode != 0:
        return None
    with tempfile.TemporaryDirectory(prefix="deploy-preview-") as tmp:
        tar_path = Path(tmp) / "fabric.tar"
        r = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", f"--output={tar_path}", base, "fabric"], capture_output=True, text=True)
        if r.returncode != 0:
            return {}
        with tarfile.open(tar_path) as tf:
            tf.extractall(tmp, filter="data")                                         # our own archive of our own repo
        return find_items(Path(tmp) / "fabric")


def _sig(*parts: object) -> str:
    return hashlib.sha1(repr(parts).encode("utf-8")).hexdigest()[:16]


def _node_sig(node) -> str:
    return _sig(node.kind, node.name, node.value, sorted(node.props.items()), [_node_sig(c) for c in node.children], node.raw)


def _entity_doc(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if ln.startswith("///"))


def structure(kind: str, files: dict[str, str]) -> dict[str, dict[str, str]]:
    """category -> {name: signature}; a changed signature means the object changed."""
    cats: dict[str, dict[str, str]] = defaultdict(dict)
    if kind in ("Ontology", "SemanticModel"):
        parse = _oac().parse_tmdl
        for rel, text in files.items():
            if not rel.endswith(".tmdl"):
                continue
            rel = rel[len("definition/"):] if rel.startswith("definition/") else rel
            if rel.startswith("entities/"):
                for n in parse(text):
                    if n.kind == "entity":
                        cats["entities"][n.name] = _sig(text)
                        cats["entity descriptions"][n.name] = _sig(_entity_doc(text))
                        for p in n.kids("property"):
                            cats["properties"][f"{n.name}.{p.name}"] = _node_sig(p)
            elif rel == "entityRelationships.tmdl":
                for n in parse(text):
                    if n.kind == "entityRelationship":
                        cats["relationships"][n.name] = _node_sig(n)
            elif rel.startswith("rules/"):
                for n in parse(text):
                    if n.kind == "rule":
                        cats["rules"][n.name] = _sig(text)
            elif rel.startswith("tables/") and kind == "SemanticModel":
                for n in parse(text):
                    if n.kind == "table":
                        cats["tables"][n.name] = _sig(text)
                        for m in n.kids("measure"):
                            cats["measures"][f"{n.name}.{m.name}"] = _node_sig(m)
            elif rel == "relationships.tmdl" and kind == "SemanticModel":
                for n in parse(text):
                    if n.kind == "relationship":
                        cats["relationships"][n.name] = _node_sig(n)
    elif kind == "Report":
        for rel, text in files.items():
            parts = rel.split("/")
            if len(parts) >= 4 and parts[0] == "definition" and parts[1] == "pages" and parts[-1] == "page.json":
                try:
                    name = json.loads(text).get("displayName", parts[2])
                except ValueError:
                    name = parts[2]
                cats["pages"][str(name)] = _sig(text)
            elif len(parts) >= 6 and parts[1] == "pages" and parts[3] == "visuals" and parts[-1] == "visual.json":
                cats["visuals"][f"{parts[2]}/{parts[4]}"] = _sig(text)
    for rel, text in files.items():
        cats["files"][rel] = _sig(text)
    return cats


def _delta(old: dict[str, str], new: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(k for k in set(old) & set(new) if old[k] != new[k])
    return added, removed, changed


def describe(kind: str, old_files: dict[str, str], new_files: dict[str, str]) -> tuple[str, list[str]]:
    """(one-line summary, detail lines) for one item."""
    old, new = structure(kind, old_files), structure(kind, new_files)
    cats = [c for c in new.keys() | old.keys() if c not in ("files", "entity descriptions")]
    order = ["entities", "properties", "relationships", "rules", "tables", "measures", "pages", "visuals"]
    cats.sort(key=lambda c: order.index(c) if c in order else len(order))
    bits: list[str] = []
    details: list[str] = []
    for c in cats:
        a, r, ch = _delta(old.get(c, {}), new.get(c, {}))
        if not (a or r or ch):
            continue
        bits.append(f"{c} " + " ".join(x for x in (f"+{len(a)}" if a else "", f"-{len(r)}" if r else "", f"~{len(ch)}" if ch else "") if x))
        for sign, names in (("+", a), ("-", r), ("~", ch)):
            if names:
                shown = ", ".join(names[:MAX_NAMES]) + (f" and {len(names) - MAX_NAMES} more" if len(names) > MAX_NAMES else "")
                details.append(f"{sign} {c}: {shown}")
    if "entities" in new or "entities" in old:
        _, _, desc_changed = _delta(old.get("entity descriptions", {}), new.get("entity descriptions", {}))
        if desc_changed:
            bits.append(f"entity descriptions ~{len(desc_changed)}")
            details.append("~ entity descriptions: " + ", ".join(desc_changed[:MAX_NAMES]) + (f" and {len(desc_changed) - MAX_NAMES} more" if len(desc_changed) > MAX_NAMES else ""))
    if not bits:
        a, r, ch = _delta(old.get("files", {}), new.get("files", {}))
        bits.append("files " + " ".join(x for x in (f"+{len(a)}" if a else "", f"-{len(r)}" if r else "", f"~{len(ch)}" if ch else "") if x))
    return " · ".join(bits), details


def preview(head: Items, base: Optional[Items], base_ref: str) -> str:
    lines = ["## Deployment preview", ""]
    if base is None:
        lines += [f"Base ref `{base_ref}` was not found, so every item is shown as new.", ""]
        base = {}
    lines += [f"Compared with `{base_ref}`. **Nothing is deployed from a pull request.** A merge to `main` deploys to the dev workspace; a release tag promotes to prod after approval.", ""]
    rows: list[str] = []
    detail_blocks: list[str] = []
    unchanged = 0
    for key in sorted(set(head) | set(base)):
        kind, name = key
        if key in head and key not in base:
            action, summary, details = "create", f"{len(head[key])} files", []
        elif key in base and key not in head:
            action, summary, details = "remove", f"{len(base[key])} files", []
        elif head[key] == base[key]:
            unchanged += 1
            continue
        else:
            action = "update"
            summary, details = describe(kind, base[key], head[key])
        rows.append(f"| {name} | {kind} | **{action}** | {summary} |")
        if details:
            detail_blocks.append(f"**{name}** ({kind})\n\n" + "\n".join(f"- {d}" for d in details))
    if not rows:
        lines.append(f"No Fabric item changes ({unchanged} item(s) unchanged).")
        return "\n".join(lines) + "\n"
    lines += ["| Item | Type | Action | What changes |", "|---|---|---|---|", *rows, ""]
    if unchanged:
        lines += [f"{unchanged} item(s) unchanged.", ""]
    if detail_blocks:
        lines += ["<details><summary>Details</summary>", "", *[b + "\n" for b in detail_blocks], "</details>", ""]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="origin/main", help="git ref to compare with (default origin/main)")
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    root = Path(a.root)
    sys.stdout.write(preview(find_items(root / "fabric"), base_items(root, a.base), a.base))
    return 0


if __name__ == "__main__":
    sys.exit(main())
