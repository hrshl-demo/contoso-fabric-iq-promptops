"""The registry of names: one row per gold table, that is per ontology / semantic-model entity, joined from the specs and from nothing else.

There is no file of names to keep in step. The registry is these fields: `entity`, `physical`, `kind`, `grain`, `silver` and `renamed_from` of
model/gold/*.yaml; `profile_entity` of model/silver/*.yaml (read through `silver_tables`) and its sources, the bronze files; the profile family,
Microsoft CDM reference and BIAN domains of model/standards.yaml; the official-name aliases of model/cdm_crosswalk.yaml. entity_rows() joins
them, and everything that shows a name or a lineage derives from its rows: the architecture clause of every entity description (spec.make_spec),
the data dictionary (agents.data_dictionary), docs/NAMING_STANDARD.md (tools/naming_registry.py) and the naming tests. To rename something, edit
those fields (docs/NAMING_STANDARD.md, "How to change a name"); nothing is spelled twice in code."""
from __future__ import annotations

import re

from . import standards
from .gold import KIND_PREFIX
from .silver import snake

# The one entity whose lakehouse name is not <role>_<snake(entity)>: the generated calendar. Neither Microsoft CDM nor BIAN defines a calendar
# entity, and dim_date is the Kimball standard name for it (docs/NAMING_STANDARD.md, deliberate deviations).
PHYSICAL_EXCEPTIONS = {"Calendar": "dim_date"}
ARCHITECTURE_KEYS = ("profile_entities", "extension", "family", "cdm", "bian", "bronze_files", "renamed_from")


def expected_physical(entity: str, kind: str) -> str:
    """The lakehouse name the naming standard gives an entity of a star role: dim_ / fact_ / bridge_ / ref_ plus snake_case of the entity name."""
    return PHYSICAL_EXCEPTIONS.get(entity) or KIND_PREFIX[kind] + snake(entity)


def bronze_files(silver_table: dict) -> list[str]:
    """The seed files a silver table reads, without the `data/releases/<release>/` prefix, in the order the spec lists its sources."""
    out: list[str] = []
    for src in silver_table.get("sources") or []:
        for path in [src.get("file"), src.get("data"), *(src.get("files") or [])]:
            if path:
                out.append(re.sub(r"^data/(releases/[^/]+/)?", "", path))
    return list(dict.fromkeys(out))


def _aliases(model: dict) -> dict[str, list[str]]:
    """profile entity -> its official-name aliases as 'OfficialEntity.officialAttribute', in the crosswalk's (profile attribute) order."""
    out: dict[str, list[str]] = {}
    for entry in model.get("crosswalk") or []:
        out[entry["profile_entity"]] = [f"{entry['official_entity']}.{a['official']}" for a in entry.get("attributes") or []]
    return out


def entity_rows(model: dict) -> list[dict]:
    """One row per gold table, in model order. `profile_entities` is empty for a Contoso extension entity; `cdm` and `bian` are what the
    registry says about the profile entities the entity realises (never upgraded: the status words are the registry's own)."""
    std = model.get("standards") or {}
    profile = std.get("entities") or {}
    realises: dict[str, list[str]] = {}
    for name, e in profile.items():
        if e.get("ontology_entity"):
            realises.setdefault(e["ontology_entity"], []).append(name)
    silver = {s["table"]: s for s in model.get("silver_tables") or []}
    aliases = _aliases(model)
    rows = []
    for t in model["tables"]:
        names = realises.get(t["entity"], [])
        bian: dict[str, dict] = {}
        cdm: dict[str, list[str]] = {}
        for n in names:
            for b in profile[n].get("bian") or []:
                bian.setdefault(b["domain"], {"domain": b["domain"], "control_record": b.get("control_record"), "status": standards._bian_status(b)})
            if profile[n].get("cdm"):
                cdm.setdefault(profile[n]["cdm"]["entity"], []).extend(aliases.get(n, []))
        sources = [silver[s] for s in t.get("silver") or [] if s in silver]
        rows.append({
            "table": t["table"], "entity": t["entity"], "physical": t.get("physical") or t["table"], "kind": t.get("kind"), "grain": t.get("grain"),
            "tier": t.get("tier", "core"), "domain": t.get("domain"), "ontology": t.get("ontology", True),
            "silver": list(t.get("silver") or []), "reference": bool(t.get("reference")),
            "bronze_files": list(dict.fromkeys(f for s in sources for f in bronze_files(s))),
            "profile_entities": names, "extension": not names, "family": profile[names[0]].get("family") if names else None,
            "cdm": [{"entity": e, "aliases": a} for e, a in cdm.items()], "bian": list(bian.values()),
            "renamed_from": dict(t.get("renamed_from") or {}),
        })
    return rows


def _lower_first(text: str) -> str:
    return text[0].lower() + text[1:] if len(text) > 1 and text[1].islower() else text


def architecture_clause(row: dict) -> str:
    """The one compact clause every entity description carries after its standards sentence, built from the registry row: star role and grain,
    the lakehouse lineage (a reference table says it is curated reference data instead of naming a silver table) and the official-name aliases."""
    kind, grain = row.get("kind"), (row.get("grain") or "").strip().rstrip(".")
    if not kind or not grain:
        return ""
    lineage = "Curated reference data" if row["reference"] else " <- ".join(p for p in (" + ".join(row["silver"]), ", ".join(row["bronze_files"])) if p)
    head = f"Star role: {kind}; grain: {_lower_first(grain)}. Lakehouse: {row['physical']}"
    out = f"{head} <- {lineage}." if lineage else f"{head}."
    aliases = [a for c in row["cdm"] for a in c["aliases"]]
    if aliases:
        out += f" Official CDM name aliases: {', '.join(aliases)} (semantic reference only)."
    return out


def architecture(row: dict) -> dict:
    """The part of a registry row that travels with the spec table (for the data dictionary), as plain JSON-able data."""
    return {k: row[k] for k in ARCHITECTURE_KEYS}
