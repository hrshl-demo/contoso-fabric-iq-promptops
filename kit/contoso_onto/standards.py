"""Alignment of the ontology with the standards the source repo cites: Microsoft's Financial Services CDM and BIAN 13.

The repo's banking model (data/banking-model/v1) is a CDM application profile of 45 entities, with a Microsoft CDM reference on 8 of them
and a BIAN 13.0.0 mapping registry (custom, provisional and unmapped attribute mappings). The ontology is built from the repo's seed data and
realises a subset of those entities under the profile's own names (ONTOLOGY_ENTITY says which; Consent is the one merge: the profile's Consent
and FrontierConsent rows are one ontology entity). Every other ontology entity is a Contoso extension entity with no profile entity. The repo
states that its mapping is a demo profile and not Microsoft endorsement, BIAN certification or regulatory compliance; this module keeps that
wording and never upgrades a status: provisional stays provisional and unmapped stays unmapped.

generate() derives the per-entity summary from the repo. It is committed as model/standards.yaml (a test compares it with the registry), so
building the spec needs no repo access. notes() turns it into the sentence appended to an entity's description in the ontology."""
from __future__ import annotations

import collections
import json
from pathlib import Path
from typing import Any

PROFILE = "data/banking-model/v1/profile.json"
REGISTRY = "data/banking-model/v1/bian-registry.json"
SOURCES = "data/banking-model/v1/sources.json"
DISCLAIMER = "Demo mapping carried over from the source repo's banking profile: not Microsoft endorsement, BIAN certification or regulatory compliance."
EXTENSION_NOTE = "Contoso extension entity: no CDM profile entity, BIAN status unmapped."

# Profile (CDM) entity -> the ontology entity that realises it. Only matches that are clear from the profile descriptions and the seed files.
# Every other profile entity is not modelled in the ontology (many exist only in the app's runtime session state, not in the seed).
# The ontology entity carries the profile entity's own name; Consent is the one merge. Commitment is deliberately absent: the profile's
# Commitment is a mandate or repayment-schedule row, not the seed's scheduled obligation, so ScheduledObligation is an extension entity.
ONTOLOGY_ENTITY = {
    "Customer": "Customer", "Product": "Product", "Account": "Account", "Card": "Card", "Deposit": "Deposit", "Loan": "Loan",
    "ProtectionPolicy": "ProtectionPolicy", "Transaction": "Transaction", "Goal": "Goal", "Preference": "Preference", "Document": "Document",
    "Consent": "Consent", "FrontierConsent": "Consent", "ServiceCase": "ServiceCase",
    "HouseholdAccessGrant": "HouseholdAccessGrant", "RegisteredAgent": "RegisteredAgent", "FrontierMandate": "FrontierMandate",
    "MandateAction": "MandateAction", "ProactiveCandidate": "ProactiveCandidate",
}


def _read(root: Path, rel: str) -> Any:
    return json.loads((Path(root) / rel).read_text(encoding="utf-8-sig"))


def generate(repo_root: Path) -> dict:
    """The `standards` section of the model, derived from the repo's profile and BIAN registry."""
    prof, reg, src = _read(repo_root, PROFILE), _read(repo_root, REGISTRY), _read(repo_root, SOURCES)
    domains = {d["id"]: d for d in reg["domains"]}
    per: dict[str, dict[str, dict[str, int]]] = {}
    unmapped: collections.Counter = collections.Counter()
    for m in reg["mappings"]:
        ent = m["entityType"]
        if m["mappingStatus"] == "unmapped" or not m.get("domainId"):
            unmapped[ent] += 1
            continue
        d = per.setdefault(ent, {}).setdefault(m["domainId"], {"custom": 0, "provisional": 0})
        d[m["mappingStatus"]] = d.get(m["mappingStatus"], 0) + 1
    entities: dict[str, dict] = {}
    for e in prof["entityDefinitions"]:
        name = e["name"]
        item: dict[str, Any] = {"family": e.get("family"), "attributes": len(e.get("attributes", [])), "ontology_entity": ONTOLOGY_ENTITY.get(name)}
        cdm = e.get("cdmReference") or {}
        if cdm:
            item["cdm"] = {"entity": cdm.get("entityName"), "status": cdm.get("mappingStatus"), "reference": cdm.get("referenceStatus")}
        item["bian"] = [{"domain": domains[dom]["name"], "control_record": domains[dom].get("controlRecord"), "custom": c["custom"], "provisional": c["provisional"]}
                        for dom, c in sorted(per.get(name, {}).items(), key=lambda kv: domains[kv[0]]["name"])]
        item["unmapped_attributes"] = unmapped.get(name, 0)
        entities[name] = item
    relationships = [f"{r['fromEntity']}.{r['fromAttribute']} -> {r['toEntity']}.{r['toAttribute']}" for r in prof.get("relationships", [])]
    return {
        "source": {"profileVersion": reg.get("profileVersion"), "mappingVersion": reg.get("mappingVersion"), "bianRelease": reg.get("release"),
                   "bianCommit": src.get("bianCommit") or reg.get("commit"), "cdmCommit": src.get("cdmCommit")},
        "disclaimer": DISCLAIMER,
        "entities": entities,
        "relationships": relationships,
    }


def _bian_status(b: dict) -> str:
    if b["custom"] and b["provisional"]:
        return "custom, partly provisional"
    return "provisional" if b["provisional"] else "custom"


def notes(model: dict) -> dict[str, str]:
    """ontology entity -> one sentence for its description: the BIAN / Microsoft CDM alignment of an entity that realises a profile entity, the
    extension label of every other entity. Empty when the model has no standards."""
    std = model.get("standards") or {}
    release = (std.get("source") or {}).get("bianRelease") or ""
    grouped: dict[str, list[dict]] = {}
    for e in (std.get("entities") or {}).values():
        if e.get("ontology_entity"):
            grouped.setdefault(e["ontology_entity"], []).append(e)
    out: dict[str, str] = {}
    for onto, items in grouped.items():
        bian: dict[str, str] = {}
        cdm: dict[str, str] = {}
        for e in items:
            for b in e.get("bian") or []:
                bian.setdefault(b["domain"], _bian_status(b))
            if e.get("cdm"):
                cdm.setdefault(e["cdm"]["entity"], "semantic reference only")
        b_txt = (f"BIAN {release} " + ", ".join(f"{d} ({s})" for d, s in bian.items())).strip() if bian else "no verified BIAN mapping (custom semantics)"
        c_txt = "Microsoft CDM " + ", ".join(f"{n} ({s})" for n, s in cdm.items()) if cdm else "no Microsoft CDM reference"
        out[onto] = f"Standards: {b_txt}; {c_txt}. Demo mapping, not certification."
    if std.get("entities"):
        for t in model.get("tables") or []:
            out.setdefault(t["entity"], EXTENSION_NOTE)             # not realised by a profile entity: no alignment is claimed, and the text says so
    return out
