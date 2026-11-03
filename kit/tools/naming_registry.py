#!/usr/bin/env python
"""Generate the tables of docs/NAMING_STANDARD.md from the specs. The registry is not a file of its own: it is the `entity`, `physical`, `kind` and
`renamed_from` fields of model/gold/*.yaml, the silver `profile_entity` / `ontology_entity` fields, model/standards.yaml and model/cdm_crosswalk.yaml,
joined by contoso_onto/registry.py. This tool only renders that join, so the document cannot say something the specs do not.

    python tools/naming_registry.py            # print the generated blocks
    python tools/naming_registry.py --write    # replace the two generated blocks in docs/NAMING_STANDARD.md
    python tools/naming_registry.py --check    # exit 1 when the document is out of date (tests/test_naming_standard.py does the same)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))

from contoso_onto import registry, standards  # noqa: E402
from contoso_onto.gold import load_model  # noqa: E402

DOC = KIT / "docs" / "NAMING_STANDARD.md"
BLOCKS = {"entities": ("<!-- naming-registry:begin -->", "<!-- naming-registry:end -->"),
          "bian": ("<!-- bian-domains:begin -->", "<!-- bian-domains:end -->")}
ENTITY_HEADER = ["Ontology entity", "Profile entity", "Family", "Silver table", "Gold physical", "Kind", "Official CDM reference",
                 "BIAN service domains (status)", "Merge or rename"]


def _cell(text: str) -> str:
    return text.replace("|", "/") or "-"


def _note(row: dict) -> str:
    notes = []
    if len(row["profile_entities"]) > 1:
        notes.append("merges the profile entities " + " + ".join(row["profile_entities"]))
    old = row["renamed_from"]
    if old.get("entity"):
        notes.append(f"entity was {old['entity']}")
    if old.get("physical"):
        notes.append(f"lakehouse was {old['physical']}")
    if row["physical"] in registry.PHYSICAL_EXCEPTIONS.values():
        notes.append(f"{row['physical']} is the one documented exception to role + entity")
    if not row["ontology"]:
        notes.append("semantic model only")
    return "; ".join(notes)


def entity_table(model: dict) -> str:
    lines = ["| " + " | ".join(ENTITY_HEADER) + " |", "|" + "---|" * len(ENTITY_HEADER)]
    for r in registry.entity_rows(model):
        cdm = "; ".join(c["entity"] + (f" ({len(c['aliases'])} name aliases)" if c["aliases"] else "") for c in r["cdm"])
        bian = "; ".join(f"{b['domain']} ({b['status']})" for b in r["bian"]) or ("none verified" if r["profile_entities"] else "unmapped")
        silver = ", ".join(f"`{s}`" for s in r["silver"]) or "(curated reference data)"
        cells = [f"`{r['entity']}`", " + ".join(r["profile_entities"]) or "Contoso extension", r["family"] or "-", silver, f"`{r['physical']}`",
                 r["kind"], cdm, bian, _note(r)]
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    return "\n".join(lines)


def bian_table(model: dict) -> str:
    profile = (model.get("standards") or {}).get("entities") or {}
    domains: dict[str, dict] = {}
    for name, e in profile.items():
        for b in e.get("bian") or []:
            d = domains.setdefault(b["domain"], {"control": b["control_record"], "profile": [], "onto": []})
            d["profile"].append(f"{name} ({standards._bian_status(b)})")
            if e.get("ontology_entity") and e["ontology_entity"] not in d["onto"]:
                d["onto"].append(e["ontology_entity"])
    lines = ["| BIAN service domain | Control record | Profile entities mapped (status) | Ontology entities |", "|---|---|---|---|"]
    for dom in sorted(domains):
        d = domains[dom]
        lines.append(f"| {dom} | `{d['control']}` | {', '.join(d['profile'])} | {', '.join(d['onto']) or 'none (profile entities not in the ontology)'} |")
    return "\n".join(lines)


def render(model: dict) -> dict[str, str]:
    """block name -> generated markdown."""
    return {"entities": entity_table(model), "bian": bian_table(model)}


def block_of(text: str, name: str) -> Optional[str]:
    begin, end = BLOCKS[name]
    m = re.search(re.escape(begin) + r"\n(.*?)\n?" + re.escape(end), text, re.S)
    return m.group(1) if m else None


def replace_blocks(text: str, blocks: dict[str, str]) -> str:
    for name, body in blocks.items():
        begin, end = BLOCKS[name]
        if block_of(text, name) is None:
            raise SystemExit(f"{DOC} has no {begin} ... {end} block")
        text = re.sub(re.escape(begin) + r"\n.*?\n?" + re.escape(end), lambda _m: f"{begin}\n{body}\n{end}", text, flags=re.S)
    return text


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doc", default=str(DOC))
    ap.add_argument("--write", action="store_true", help="replace the generated blocks in the document")
    ap.add_argument("--check", action="store_true", help="exit 1 when the document differs from the generated blocks")
    a = ap.parse_args(argv)
    blocks = render(load_model(KIT / "model"))
    if not (a.write or a.check):
        print("\n\n".join(blocks.values()))
        return 0
    path = Path(a.doc)
    text = path.read_text(encoding="utf-8")
    new = replace_blocks(text, blocks)
    if a.check:
        print("up to date" if new == text else f"{path} is out of date: run python tools/naming_registry.py --write")
        return 0 if new == text else 1
    path.write_text(new, encoding="utf-8", newline="\n")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
