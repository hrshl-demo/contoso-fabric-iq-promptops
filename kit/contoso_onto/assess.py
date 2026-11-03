"""Read-only data-readiness assessment of the contoso-retail-superapp4 seed data for a Fabric IQ ontology.

Produces assessment.json (machine readable) and ASSESSMENT.generated.md (tables). All statistics are computed from the
repo files; nothing is guessed. qa/ folders are skipped on purpose: they are test-only answer keys that the repo says must
never feed a runtime.
"""
from __future__ import annotations

import collections
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from . import RELEASES
from .snapshot import load_json, release_dir

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
REF_NAME_RE = re.compile(r"(Id|Ids|Ref|Refs)$")
PII_RE = re.compile(r"(name|email|mobile|phone|contact|city|^age$|birth|policynumber|maskednumber|fingerprint|narrative|^bio$|beneficiar)", re.I)
TEXT_LEN = 120
EXTRA_TREE = ["journeys", "relationship", "service-requests.json"]


# --------------------------------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------------------------------
def collections_of(label: str, data: Any) -> dict[str, list[dict]]:
    """Split a parsed file into record collections: {collection-label: [records]}."""
    out: dict[str, list[dict]] = {}
    if isinstance(data, list):
        if data and all(isinstance(r, dict) for r in data):
            out[label] = data
        elif not data:
            out[label] = []
        return out
    if isinstance(data, dict):
        lists = {k: v for k, v in data.items() if isinstance(v, list) and v and all(isinstance(r, dict) for r in v)}
        for k, v in lists.items():
            out[f"{label}::{k}"] = v
        if not lists and data and all(isinstance(v, dict) for v in data.values()):
            out[label] = [dict(v, _key=k) for k, v in data.items()]
    return out


def load_tree(repo: Path) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for rel in RELEASES:
        root = release_dir(repo, rel)
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.json")):
            sub = p.relative_to(root).as_posix()
            if sub.startswith("qa/") or sub.startswith("assets/") or sub == "release.json":
                continue
            tree[f"releases/{rel}/{sub}"] = load_json(p)
    data = repo / "data"
    for item in EXTRA_TREE:
        p = data / item
        files = sorted(p.rglob("*.json")) if p.is_dir() else [p]
        for f in files:
            if f.exists():
                tree[f.relative_to(data).as_posix()] = load_json(f)
    return tree


# --------------------------------------------------------------------------------------------------
# value typing
# --------------------------------------------------------------------------------------------------
def is_money(v: Any) -> bool:
    return isinstance(v, dict) and "amountMinor" in v and "currency" in v and len(v) <= 3


def kind(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        if DATE_RE.match(v):
            return "date"
        if TS_RE.match(v):
            return "timestamp"
        return "text" if len(v) > TEXT_LEN else "str"
    if is_money(v):
        return "money"
    if isinstance(v, dict):
        return "object"
    if isinstance(v, list):
        return "array"
    return type(v).__name__


def walk(value: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    """Yield (path, value) for every node. Lists add '[]' to the path; money objects are leaves."""
    if is_money(value):
        yield path, value
        return
    if isinstance(value, dict):
        yield path, value
        for k, v in value.items():
            yield from walk(v, f"{path}.{k}" if path else k)
    elif isinstance(value, list):
        yield path, value
        for v in value:
            yield from walk(v, path + "[]")
    else:
        yield path, value


# --------------------------------------------------------------------------------------------------
# profiling
# --------------------------------------------------------------------------------------------------
NOT_A_KEY = {"ownerId", "releaseId", "householdId", "primaryOwnerId"}


def _singular(word: str) -> str:
    w = word.lower()
    if w.endswith("ies"):
        return w[:-3] + "y"
    return w[:-1] if w.endswith("s") and not w.endswith("ss") else w


def _unique_ids(rows: list[dict], field: str) -> bool:
    vals = [r.get(field) for r in rows]
    return bool(rows) and all(isinstance(x, str) and x for x in vals) and len(set(vals)) == len(vals)


def pick_key(label: str, rows: list[dict]) -> str | None:
    """Primary key = the unique <thing>Id that matches the file name, else the first unique *Id in record order."""
    if not rows:
        return None
    stem = label.split("::")[1] if "::" in label else Path(label).stem
    words = [w for w in re.split(r"[-_]|(?<=[a-z])(?=[A-Z])", stem) if w]
    singular = [w.lower() for w in words[:-1]] + [_singular(words[-1])]
    wanted = {"".join(w.capitalize() if i else w for i, w in enumerate(singular)) + "Id", singular[-1] + "Id"}
    for f in rows[0]:
        if f in wanted and _unique_ids(rows, f):
            return f
    for f in rows[0]:
        if f.endswith("Id") and f not in NOT_A_KEY and _unique_ids(rows, f):
            return f
    return None


def profile_collection(label: str, rows: list[dict]) -> dict:
    fields: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    examples: dict[str, Any] = {}
    max_len: dict[str, int] = collections.defaultdict(int)
    for r in rows:
        for path, v in walk(r):
            if not path:
                continue
            fields[path][kind(v)] += 1
            if isinstance(v, list):
                max_len[path] = max(max_len[path], len(v))
            if path not in examples and v not in (None, [], {}):
                examples[path] = v if not isinstance(v, (dict, list)) else None
    key = pick_key(label, rows)
    nested_obj = sorted(p for p, c in fields.items() if set(c) == {"object"} or ("object" in c and "[]" not in p))
    obj_arrays = sorted(p for p, c in fields.items() if "array" in c and any(
        q.startswith(p + "[]") and fields[q].get("object") for q in fields))
    scalar_arrays = sorted(p for p, c in fields.items() if "array" in c and p not in obj_arrays and max_len[p] > 0)
    leaf = {p: dict(c) for p, c in fields.items() if not (set(c) <= {"object", "array"})}
    money = sorted(p for p, c in fields.items() if "money" in c)
    return {
        "label": label, "rows": len(rows), "keyField": key,
        "fieldCount": len(leaf),
        "fields": {p: {"kinds": dict(c), "example": examples.get(p), **({"maxArrayLen": max_len[p]} if p in max_len else {})}
                   for p, c in sorted(fields.items())},
        "moneyFields": money,
        "objectArrays": obj_arrays,
        "scalarArrays": scalar_arrays,
        "nestedObjects": [p for p in nested_obj if p not in money],
        "alwaysNull": sorted(p for p, c in fields.items() if set(c) == {"null"}),
        "piiLike": sorted({p for p in leaf if PII_RE.search(p.split(".")[-1].replace("[]", ""))}),
        "longText": sorted(p for p, c in fields.items() if "text" in c),
        "dateOnly": sorted(p for p, c in fields.items() if "date" in c),
        "timestamps": sorted(p for p, c in fields.items() if "timestamp" in c),
        "nullableFields": sorted(p for p, c in fields.items() if "null" in c and set(c) != {"null"}),
        "status": "empty" if not rows else None,
    }


def ref_values(rows: list[dict], path: str) -> list[Any]:
    """Collect scalar values at a dotted/[]-path (strings only)."""
    vals: list[Any] = []
    parts = path.split(".")

    def rec(node: Any, i: int) -> None:
        if i == len(parts):
            if isinstance(node, str):
                vals.append(node)
            return
        seg = parts[i]
        many = seg.endswith("[]")
        name = seg[:-2] if many else seg
        if not isinstance(node, dict) or name not in node:
            return
        nxt = node[name]
        if many:
            if isinstance(nxt, list):
                for x in nxt:
                    rec(x, i + 1)
        else:
            rec(nxt, i + 1)

    for r in rows:
        rec(r, 0)
    return vals


def foreign_keys(profiles: dict[str, dict], colls: dict[str, list[dict]]) -> list[dict]:
    """Heuristic: a ref-like field whose values (>=80%) fall in some collection's unique *Id universe."""
    universes: dict[str, set[str]] = {}
    for label, rows in colls.items():
        for f in {p for p in profiles[label]["fields"] if "." not in p and "[]" not in p and p.endswith("Id")}:
            if _unique_ids(rows, f) and (f == profiles[label]["keyField"] or f in NOT_A_KEY):
                universes[f"{label}.{f}"] = {r[f] for r in rows}
    out = []
    for label, rows in colls.items():
        for path in profiles[label]["fields"]:
            leaf = path.split(".")[-1].replace("[]", "")
            if not REF_NAME_RE.search(leaf) or leaf == "releaseId" or "authoringProvenance" in path:
                continue
            vals = ref_values(rows, path)
            if not vals:
                continue
            own = profiles[label]["keyField"]
            if path == own:
                continue
            best, best_hit, best_sim = None, 0, -1
            for uname, uset in universes.items():
                if uname == f"{label}.{path}":
                    continue
                hit = sum(1 for v in vals if v in uset)
                sim = 1 if leaf.lower().replace("ref", "id")[:-2] in uname.lower() else 0
                if (hit, sim) > (best_hit, best_sim):
                    best, best_hit, best_sim = uname, hit, sim
            ratio = best_hit / len(vals)
            orphans = sorted({v for v in vals if best and v not in universes[best]})[:5] if best else sorted(set(vals))[:5]
            out.append({"from": f"{label}.{path}", "to": best if ratio >= 0.8 else None, "values": len(vals),
                        "resolved": round(ratio, 3), "orphanSample": orphans if ratio < 1 else []})
    return out


# --------------------------------------------------------------------------------------------------
# release integrity, overlaps, CDM coverage
# --------------------------------------------------------------------------------------------------
def release_integrity(repo: Path) -> list[dict]:
    out = []
    for rel in RELEASES:
        rj = release_dir(repo, rel) / "release.json"
        if not rj.exists():
            continue
        meta = load_json(rj)
        mism, checked = [], 0
        for name, info in (meta.get("files") or {}).items():
            p = release_dir(repo, rel) / name
            if not p.exists():
                mism.append(f"{name}: missing")
                continue
            raw = p.read_bytes()
            ok = hashlib.sha256(raw).hexdigest() == info.get("sha256") or hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest() == info.get("sha256")
            checked += 1
            if not ok:
                mism.append(f"{name}: sha256 differs")
        out.append({"release": rel, "status": meta.get("releaseStatus"), "businessDate": meta.get("businessDate"),
                    "contractVersion": meta.get("contractVersion"), "approval": (meta.get("approval") or {}).get("note"),
                    "filesChecked": checked, "mismatches": mism, "counts": meta.get("counts")})
    return out


def id_overlaps(colls: dict[str, list[dict]], profiles: dict[str, dict]) -> list[dict]:
    """Same logical file in both releases: do primary keys collide?"""
    by_stem: dict[str, dict[str, set[str]]] = collections.defaultdict(dict)
    for label, rows in colls.items():
        m = re.match(r"releases/([^/]+)/(.+)\.json", label.split("::")[0])
        key = profiles[label]["keyField"]
        if m and key:
            by_stem[m.group(2)][m.group(1)] = {r[key] for r in rows}
    out = []
    for stem, rels in by_stem.items():
        if len(rels) > 1:
            names = sorted(rels)
            inter = rels[names[0]] & rels[names[1]]
            out.append({"file": stem, "releases": names, "sizes": [len(rels[n]) for n in names], "collisions": sorted(inter)[:5], "collisionCount": len(inter)})
    return out


# Dataset names used in banking-model field-mappings -> seed collections that can feed them.
SEED_DATASETS = {
    "personas", "accounts", "cards", "deposits", "liabilities", "policies", "transactions", "goals", "preferences", "documents",
    "consents", "ProductPackage.products", "serviceRequests", "frontier.householdAccess", "frontier.consents",
    "frontier.registeredAgents", "frontier.mandates", "frontier.mandateActions", "frontier.candidates", "frontier.handoffs",
    "liabilities.repaymentSchedule", "goals.allocations",
}


def cdm_coverage(repo: Path) -> dict:
    prof_p = repo / "data" / "banking-model" / "v1" / "profile.json"
    map_p = repo / "data" / "banking-model" / "v1" / "field-mappings.json"
    if not (prof_p.exists() and map_p.exists()):
        return {}
    prof, mp = load_json(prof_p), load_json(map_p)
    ents = prof.get("entityDefinitions") or {}
    names = list(ents) if isinstance(ents, dict) else [e.get("name") for e in ents]
    by_entity: dict[str, set[str]] = collections.defaultdict(set)
    attrs: collections.Counter = collections.Counter()
    for f in mp["fields"]:
        attrs[f["entityType"]] += 1
        for s in f["sources"]:
            by_entity[f["entityType"]].add(s.get("dataset"))
    seed_backed, runtime_only = [], []
    for n in names:
        ds = by_entity.get(n, set())
        (seed_backed if ds and ds & SEED_DATASETS else runtime_only).append(n)
    rels = prof.get("relationships")
    return {"entities": len(names), "attributes": sum(attrs.values()), "relationships": len(rels or []),
            "seedBacked": sorted(seed_backed), "runtimeOnly": sorted(runtime_only)}


# --------------------------------------------------------------------------------------------------
# cross-cutting checks
# --------------------------------------------------------------------------------------------------
def identity_collisions(colls: dict[str, list[dict]]) -> list[dict]:
    """Same ownerId in more than one release = one person, two non-reconciled data generations."""
    owners: dict[str, dict[str, dict[str, int]]] = collections.defaultdict(lambda: collections.defaultdict(dict))
    for label, rows in colls.items():
        m = re.match(r"releases/([^/]+)/([^/]+)\.json$", label)
        if not m or not rows or "ownerId" not in rows[0]:
            continue
        rel, stem = m.groups()
        for r in rows:
            oid = r.get("ownerId")
            if oid:
                owners[oid][rel][stem] = owners[oid][rel].get(stem, 0) + 1
    out = []
    for oid, rels in owners.items():
        if len(rels) > 1:
            out.append({"ownerId": oid, "releases": {rel: stems for rel, stems in sorted(rels.items())}})
    return out


def unit_conventions(profiles: dict[str, dict], repo: Path) -> dict:
    fields = [(p["label"], f) for p in profiles.values() for f in p["fields"]]
    last = lambda f: f.split(".")[-1].replace("[]", "")  # noqa: E731
    pol = repo / "data" / "relationship" / "policy.json"
    param_unit = None
    if pol.exists():
        param_unit = (load_json(pol).get("money") or {}).get("productParameterUnit")
    return {
        "ledgerMoneyObjects": sum(len(p["moneyFields"]) for p in profiles.values()),
        "bpsFields": sorted({last(f) for _, f in fields if re.search(r"Bps$", last(f))}),
        "pctFields": sorted({last(f) for _, f in fields if re.search(r"(Pct|Percent)$", last(f))}),
        "croreFields": sorted({last(f) for _, f in fields if re.search(r"crore", last(f), re.I)}),
        "productParameterUnit": param_unit,
    }


def overlap_candidates(colls: dict[str, list[dict]]) -> list[dict]:
    """Equal money amounts held by different record types of the same release: likely the same rupee seen twice."""
    watch = {"accounts": ("postedBalance",), "deposits": ("principal",), "investments": ("currentValue",), "goals": ("current",)}
    seen: dict[tuple[str, int], list[str]] = collections.defaultdict(list)
    for label, rows in colls.items():
        m = re.match(r"releases/([^/]+)/([^/]+)\.json$", label)
        if not m or m.group(2) not in watch:
            continue
        rel, stem = m.groups()
        pk = pick_key(label, rows)
        for r in rows:
            for fld in watch[stem]:
                v = r.get(fld)
                if is_money(v) and v["amountMinor"] > 0:
                    seen[(rel, v["amountMinor"])].append(f"{stem}:{r.get(pk)}.{fld}")
    return [{"release": rel, "amountMinor": amt, "amountInr": amt / 100, "records": recs}
            for (rel, amt), recs in sorted(seen.items()) if len({x.split(":")[0] for x in recs}) > 1]


def excluded_files(repo: Path) -> list[dict]:
    out = []
    for rel in RELEASES:
        for p in sorted((release_dir(repo, rel) / "qa").glob("*.json")) if (release_dir(repo, rel) / "qa").is_dir() else []:
            try:
                warn = load_json(p).get("_warning")
            except Exception:  # noqa: BLE001
                warn = None
            out.append({"file": f"releases/{rel}/qa/{p.name}", "repoWarning": warn})
    return out


def doc_drift(repo: Path, integ: list[dict]) -> list[str]:
    drift = []
    prd = repo / "docs" / "Frontier-Retail-Strategy-PRD-Architecture-Handover.md"
    if prd.exists() and "in_review" in prd.read_text(encoding="utf-8-sig"):
        for i in integ:
            if i["release"] == "retail-frontier-v1" and i["status"] != "in_review":
                drift.append(f"PRD says retail-frontier-v1 stays `in_review`; release.json says `{i['status']}`")
    return drift


# --------------------------------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------------------------------
def build_findings(profiles: dict[str, dict], fks: list[dict], integ: list[dict], overlaps: list[dict], cdm: dict,
                   collisions: list[dict], units: dict, money_overlaps: list[dict], excluded: list[dict], drift: list[str]) -> list[dict]:
    p_all = list(profiles.values())
    money_n = sum(len(p["moneyFields"]) for p in p_all)
    obj_arrays = [(p["label"], a) for p in p_all for a in p["objectArrays"]]
    sc_arrays = [(p["label"], a) for p in p_all for a in p["scalarArrays"]]
    ts_n = sum(len(p["timestamps"]) for p in p_all)
    d_n = sum(len(p["dateOnly"]) for p in p_all)
    soft = sorted({f["from"].split(".", 1)[1] if "::" not in f["from"] else f["from"].split("::")[1] for f in fks
                   if f["to"] is None and f["from"].startswith("releases/") and "authoringProvenance" not in f["from"]})
    orphan = [f for f in fks if f["to"] and f["resolved"] < 1.0]
    never_set = sorted({a for p in p_all for a in p["alwaysNull"]})
    empty = [p["label"] for p in p_all if p["rows"] == 0]
    pii_n = sum(len(p["piiLike"]) for p in p_all)
    findings = [
        dict(id="A01", severity="blocker-for-graph", title="Money is a nested {amountMinor,currency} object",
             detail=f"{money_n} money fields across {len([p for p in p_all if p['moneyFields']])} collections. Ontology properties must be scalars and Decimal returns null in Fabric Graph.",
             action="Flatten to <name>_paise (int64, exact, hidden) and <name>_inr (double, rupees, agent-facing)."),
        dict(id="A02", severity="blocker-for-graph", title="Arrays of objects need child tables",
             detail=f"{len(obj_arrays)} object-array fields, e.g. " + ", ".join(f"{l.split('/')[-1]}:{a}" for l, a in obj_arrays[:6]),
             action="Explode into child tables with a surrogate key + parent FK (loan_installment, mandate_event, consent_resource ...)."),
        dict(id="A03", severity="needs-reshape", title="Arrays of scalars (scopes, permissions, refs)",
             detail=f"{len(sc_arrays)} scalar-array fields, e.g. " + ", ".join(f"{l.split('/')[-1]}:{a}" for l, a in sc_arrays[:6]),
             action="Core tier: semicolon-delimited text columns. Extended tier: bridge entities (agent_scope, grant_permission, investment_goal)."),
        dict(id="A04", severity="needs-reshape", title="Two time conventions",
             detail=f"{d_n} date-only fields (YYYY-MM-DD) and {ts_n} RFC 3339 timestamp fields with +05:30 offsets.",
             action="Instants -> UTC timestamp plus a derived IST date column; date-only fields stay DATE; add a Calendar dimension (India fiscal year)."),
        dict(id="A05", severity="needs-reshape", title="Soft references that do not resolve to a table",
             detail=f"{len(soft)} distinct ref-like fields have no target table, e.g. " + ", ".join(soft[:8]) + ".",
             action="Keep as text plus a derived *_type column (counterparty_type, subject_type, actor_type, payee_type); create FK columns only where the target exists."),
        dict(id="A06", severity="info", title="Partially resolving references",
             detail=f"{len(orphan)} reference(s) resolve <100%: " + "; ".join(f"{f['from'].split('/')[-1]} ({f['resolved']:.0%}, e.g. {', '.join(f['orphanSample'][:2])})" for f in orphan[:3]),
             action="Transaction.sourceRefs mixes document ids and case ids: split into document_id / incident_id in transaction_evidence."),
    ]
    if collisions:
        c = collisions[0]
        per_rel = "; ".join(f"{rel}: " + ", ".join(f"{n} {s}" for s, n in sorted(stems.items())) for rel, stems in c["releases"].items())
        findings.append(dict(id="A07", severity="critical-identity", title="Same ownerId, two non-reconciled data generations",
                             detail=f"ownerId `{c['ownerId']}` is a persona in both releases ({len(collisions)} colliding owner id(s)): {per_rel}. "
                                    "Different accounts, cards and transaction histories share one customer id: any aggregate by ownerId double counts. Also product id collisions: "
                                    + (", ".join(f"{o['file']}={o['collisions']}" for o in overlaps if o["collisionCount"]) or "none") + ".",
                             action="Keep the Frontier id raw; namespace legacy copies (rhea~legacy), add superseded_by_customer_id and ReleaseId everywhere, and add the rule DefaultToFrontierRelease."))
    findings += [
        dict(id="A08", severity="info", title="Never-populated fields",
             detail=f"{len(never_set)} distinct fields are null in every row ({', '.join(never_set[:6])}); empty collections: {', '.join(empty) or 'none'}.",
             action="Drop from the ontology: runtime outcomes are written by the live app, not by the seed."),
        dict(id="A09", severity="needs-decision", title="Four money and rate unit conventions in one repo",
             detail=f"Ledger money = integer paise ({units['ledgerMoneyObjects']} objects); product parameters = {units['productParameterUnit'] or 'whole rupees'} "
                    f"(for example annualFee 499); bank aggregates = crore INR floats ({len(units['croreFields'])} fields); rates = basis points ({len(units['bpsFields'])} fields) in holdings but percent floats "
                    f"({len(units['pctFields'])} fields) in the catalogue. An agent that compares a paise balance with a rupee fee is wrong by 100x.",
             action="One suffix per unit in the gold layer: *_inr (rupees), *_paise (hidden), *_pct, *_bps (hidden), *_crore_inr; the UnitsAndCurrency rule explains them."),
        dict(id="A10", severity="governance", title="PII-like and sensitive fields",
             detail=f"{pii_n} PII-like leaf fields (names, city, age, contact, KYC address and date of birth, policy numbers, KYA fingerprint). Data is synthetic, but the model should behave as if it is not.",
             action="Restricted columns (date_of_birth, kyc_address, email, policy_number, kya_key_fingerprint) stay in the lakehouse and out of the ontology; masked fields are tagged [PII]; plan OneLake CLS before any real data."),
        dict(id="A11", severity="info", title="Long free-text fields",
             detail=f"{sum(len(p['longText']) for p in p_all)} text fields > {TEXT_LEN} chars (benefits, fees, narratives, summaries).",
             action="Keep the unstructured content in Foundry IQ (already ingested by the repo); keep readable summary columns (benefits_text, facts_text) in the ontology."),
        dict(id="A12", severity="info", title="Release integrity",
             detail="; ".join(f"{i['release']}: status={i['status']}, {i['filesChecked']} files hash-verified, {len(i['mismatches'])} mismatches" for i in integ) + ("; " + "; ".join(drift) if drift else ""),
             action="Pin commit + release content hash in the load manifest; fix the doc drift in the repo (out of scope here)." if drift else "Pin commit + release content hash in the load manifest."),
    ]
    if money_overlaps:
        findings.append(dict(id="A13", severity="needs-decision", title="The same rupee appears in two record types",
                             detail="Equal amounts held by different record types: " + "; ".join(
                                 f"INR {o['amountInr']:,.0f} in {' = '.join(o['records'])}" for o in money_overlaps[:5]) +
                                    ". Deposits, investments, goal balances and reserve accounts overlap; summing them double counts.",
                             action="Total Assets uses accounts + investments only; Goal.CurrentInr is documented as an earmark; rule NoDoubleAllocation; flag in agent instructions."))
    if cdm:
        findings.append(dict(id="A14", severity="needs-decision", title="The CDM profile is richer than the seed",
                             detail=f"The repo's CDM profile defines {cdm['entities']} entities / {cdm['attributes']} attributes / {cdm['relationships']} relationships; "
                                    f"{len(cdm['seedBacked'])} are fed by committed seed data, {len(cdm['runtimeOnly'])} exist only in runtime session state (evidence, quotes, decisions, receipts, lifecycle events ...).",
                             action="Build the ontology on seed-backed entities now; feed runtime-only ones later from the app's Data X-Ray cdm-pack export into the same gold schema."))
    if excluded:
        findings.append(dict(id="A15", severity="governance", title="Test-only answer keys are excluded on purpose",
                             detail=f"{len(excluded)} qa/ files ({', '.join(e['file'].split('/')[-1] for e in excluded)}) carry the repo's own warning not to feed a runtime.",
                             action="The kit never reads qa/ or assets/. Keep it that way."))
    return findings


# --------------------------------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------------------------------
def assess(repo: Path) -> dict:
    tree = load_tree(repo)
    colls: dict[str, list[dict]] = {}
    for label, data in tree.items():
        colls.update(collections_of(label, data))
    profiles = {label: profile_collection(label, rows) for label, rows in colls.items()}
    fks = foreign_keys(profiles, colls)
    integ = release_integrity(repo)
    overlaps = id_overlaps(colls, profiles)
    cdm = cdm_coverage(repo)
    collisions = identity_collisions(colls)
    units = unit_conventions(profiles, repo)
    money_overlaps = overlap_candidates(colls)
    excluded = excluded_files(repo)
    drift = doc_drift(repo, integ)
    return {
        "files": len(tree), "collections": len(colls), "rows": sum(p["rows"] for p in profiles.values()),
        "profiles": profiles, "foreignKeys": fks, "releaseIntegrity": integ, "idOverlaps": overlaps, "cdm": cdm,
        "identityCollisions": collisions, "unitConventions": units, "moneyOverlaps": money_overlaps, "excludedFiles": excluded,
        "docDrift": drift,
        "findings": build_findings(profiles, fks, integ, overlaps, cdm, collisions, units, money_overlaps, excluded, drift),
    }


def render_markdown(a: dict) -> str:
    L = ["# Data readiness - generated tables", "",
         f"{a['files']} JSON files -> {a['collections']} record collections, {a['rows']:,} rows. qa/ answer keys and assets/ excluded.", "",
         "## Findings", "", "| ID | Severity | Finding | Detail | Action |", "|---|---|---|---|---|"]
    for f in a["findings"]:
        L.append(f"| {f['id']} | {f['severity']} | {f['title']} | {f['detail']} | {f['action']} |")
    L += ["", "## Collections", "", "| Collection | Rows | Key | Leaf fields | Money | Object arrays | Scalar arrays | Null-always | PII-like |", "|---|---:|---|---:|---:|---:|---:|---:|---:|"]
    for p in a["profiles"].values():
        L.append(f"| {p['label']} | {p['rows']} | {p['keyField'] or '-'} | {p['fieldCount']} | {len(p['moneyFields'])} | {len(p['objectArrays'])} | {len(p['scalarArrays'])} | {len(p['alwaysNull'])} | {len(p['piiLike'])} |")
    L += ["", "## Reference resolution (heuristic)", "", "| From | To | Values | Resolved | Orphan sample |", "|---|---|---:|---:|---|"]
    for f in sorted(a["foreignKeys"], key=lambda x: (x["to"] is None, x["from"])):
        L.append(f"| {f['from']} | {f['to'] or '(no target table)'} | {f['values']} | {f['resolved']:.0%} | {', '.join(f['orphanSample'])} |")
    L += ["", "## Release integrity", ""]
    for i in a["releaseIntegrity"]:
        L.append(f"- **{i['release']}**: status `{i['status']}`, business date {i['businessDate']}, contract {i['contractVersion']}; {i['filesChecked']} files hashed, {len(i['mismatches'])} mismatches {i['mismatches'][:3]}")
    for d in a.get("docDrift", []):
        L.append(f"- documentation drift: {d}")
    if a.get("identityCollisions"):
        L += ["", "## Identity collisions across releases", ""]
        for c in a["identityCollisions"]:
            L.append(f"- ownerId `{c['ownerId']}`: " + "; ".join(f"{rel} -> {stems}" for rel, stems in c["releases"].items()))
    if a.get("moneyOverlaps"):
        L += ["", "## Same amount in different record types (possible double counting)", ""]
        for o in a["moneyOverlaps"]:
            L.append(f"- {o['release']}: INR {o['amountInr']:,.2f} = " + " = ".join(o["records"]))
    if a.get("unitConventions"):
        u = a["unitConventions"]
        L += ["", "## Unit conventions found", "",
              f"- ledger money objects (paise): {u['ledgerMoneyObjects']}",
              f"- product parameter unit (policy.json): {u['productParameterUnit']}",
              f"- basis-point fields: {', '.join(u['bpsFields'])}",
              f"- percent fields: {', '.join(u['pctFields'])}",
              f"- crore fields: {', '.join(u['croreFields'])}"]
    if a.get("excludedFiles"):
        L += ["", "## Excluded on purpose", ""] + [f"- {e['file']}" for e in a["excludedFiles"]]
    if a["cdm"]:
        c = a["cdm"]
        L += ["", "## CDM profile coverage", "", f"- Seed-backed ({len(c['seedBacked'])}): {', '.join(c['seedBacked'])}", f"- Runtime-only ({len(c['runtimeOnly'])}): {', '.join(c['runtimeOnly'])}"]
    return "\n".join(L) + "\n"


def write_outputs(a: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "assessment.json").write_text(json.dumps(a, indent=1, default=str), encoding="utf-8")
    (out_dir / "ASSESSMENT.generated.md").write_text(render_markdown(a), encoding="utf-8")
