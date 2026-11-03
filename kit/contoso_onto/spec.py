"""gold model -> normalized spec (per tier) -> semantic model + ontology TMDL, with stricter checks than the Phase-1 kit."""
from __future__ import annotations

import collections
import re
from typing import Optional

from . import oac, registry, standards
from .gold import SNAKE_RE, expand_columns

TIERS = ("smoke", "core", "full")
# Description budgets, in characters. Fabric documents two limits that reach these texts: an item description is cut at 256 (oac.py and
# fabric_ops.py clip the ontology's with [:256], so anything longer was silently truncated) and Copilot reads only the first 200 characters of a
# semantic-model description (Field Guide, source S31). Microsoft's how-to for metadata asks for one to three sentences and documents no limit for
# an entity, table or column description: the longest entity line proven live was 509 characters (Product in v1: its 497 plus the ontology's
# "[Catalogue] " domain tag). The entity budget in the full mode is therefore the kit's own, 800 (the registry's architecture clause adds at most
# about 230); the compact mode, the fallback for a service that rejects the long text, stays within 500. A column or measure stays within the
# 200 every one of them already fits, and an entity description always starts with its business meaning.
ONTOLOGY_DESCRIPTION_MAX = 256
ENTITY_DESCRIPTION_MAX = 800
ENTITY_DESCRIPTION_MAX_COMPACT = 500
COLUMN_DESCRIPTION_MAX = 200
# Description modes. full = the business meaning, the standards sentence and the architecture clause (star role, grain, lakehouse lineage,
# official-name aliases). compact = what v1 carried: the business meaning and the standards sentence, no clause. The clause is the only difference;
# the data dictionary lists the same facts as data in both modes.
DESCRIPTION_MODES = ("full", "compact")
DEFAULT_DESCRIPTIONS = "full"
DESCRIPTIONS_ENV = "CONTOSO_DESCRIPTIONS"
# 13 tables that exercise every kind of ontology object (entities, FK relationships, derived/typed columns, rules, measures,
# a semantic-model-only table) so a first live deployment can be bisected before the 31-entity core goes in.
SMOKE_TABLES = {"customer", "household", "account", "card", "transaction", "goal", "consent", "access_grant", "registered_agent",
                "mandate", "mandate_event", "law", "calendar"}


def check_descriptions(mode: Optional[str], source: str = "descriptions") -> str:
    """The mode, or a ValueError that names where the bad value came from and the two there are."""
    if mode not in DESCRIPTION_MODES:
        raise ValueError(f"{source} must be one of {', '.join(DESCRIPTION_MODES)} (got {mode!r})")
    return mode


def resolve_descriptions(flag: Optional[str] = None, env: Optional[str] = None, remembered: Optional[str] = None) -> tuple[str, str]:
    """(mode, where it came from). An explicit choice, the --descriptions flag or CONTOSO_DESCRIPTIONS, beats what the build state remembers
    (`descriptionsMode`: a fallback to compact is remembered so that a re-run does not repeat the refused attempt); with none of the three the mode is full."""
    for value, source in ((flag, "--descriptions"), ((env or "").strip().lower() or None, DESCRIPTIONS_ENV), (remembered, "the build state (descriptionsMode)")):
        if value:
            return check_descriptions(value, source), source
    return DEFAULT_DESCRIPTIONS, "default"


def _in_tier(t: dict, tier: str) -> bool:
    if tier == "full":
        return True
    if tier == "smoke":
        return t["table"] in SMOKE_TABLES
    return t.get("tier", "core") == "core"


def _dax_tables(expr: str) -> set[str]:
    return {t for t, _ in oac._dax_refs(expr)[0]}


def describe(t: dict, standards_note: str, row: dict, mode: str = DEFAULT_DESCRIPTIONS) -> str:
    """An entity's description: what it is (the spec's own text, first, so the business meaning is what the first 200 characters hold), then
    its standards sentence (alignment with BIAN and Microsoft CDM, or the extension label), then, in the full mode, the architecture clause
    built from the registry. The compact mode stops after the standards sentence."""
    clause = registry.architecture_clause(row) if check_descriptions(mode) == "full" else ""
    return " ".join(p for p in ((t.get("description") or "").rstrip(), standards_note, clause) if p)


def entity_doc(t: dict) -> str:
    """The description line a table gets in the generated parts: the ontology entity's (with its domain tag) when the table is an ontology
    entity, else the semantic-model table's. Its length is what the entity budget measures."""
    return oac.ontology_doc(t) if t.get("ontology", True) else t["description"]


# The longest entity description line Fabric has accepted so far: Product in v1, 497 characters plus the ontology's "[Catalogue] " tag (live, 16 Oct 2026).
PROVEN_LIVE_ENTITY_LINE = 509


def description_stats(model: dict, tier: str, mode: str) -> dict:
    """What the plan and the preflight say about the entity descriptions of a tier: the mode in force, the longest description line in each mode, the
    budget of each mode and the longest line Fabric has accepted so far."""
    longest = {}
    for m in DESCRIPTION_MODES:
        n, entity = max((len(entity_doc(t)), t["entity"]) for t in make_spec(model, tier, m)["tables"])
        longest[m] = {"characters": n, "entity": entity}
    return {"mode": mode, "tier": tier, "longestLine": longest, "budget": {"full": ENTITY_DESCRIPTION_MAX, "compact": ENTITY_DESCRIPTION_MAX_COMPACT},
            "longestAcceptedLive": PROVEN_LIVE_ENTITY_LINE}


def make_spec(model: dict, tier: str = "core", descriptions: str = DEFAULT_DESCRIPTIONS) -> dict:
    """Select the tier, expand column macros, prune relationships / measures / rules that point at excluded objects. `descriptions` is the mode of
    the entity descriptions (DESCRIPTION_MODES): full unless the caller asks for compact."""
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}")
    mode = check_descriptions(descriptions)
    tables = []
    alignment = standards.notes(model)                                 # BIAN / Microsoft CDM sentence per ontology entity (empty without model/standards.yaml)
    rows = {r["table"]: r for r in registry.entity_rows(model)}
    for t in model["tables"]:
        if not _in_tier(t, tier):
            continue
        t = dict(t)
        t["description"] = describe(t, alignment.get(t.get("entity"), ""), rows[t["table"]], mode)
        t["registry"] = registry.architecture(rows[t["table"]])
        t["columns"] = [dict(c) for c in expand_columns(t)]
        tables.append(t)
    have = {t["table"]: t for t in tables}
    rels = []
    for r in model.get("relationships", []):
        ft, tt = r["from"].split(".")[0], r["to"].split(".")[0]
        if ft not in have or tt not in have:
            continue
        r = dict(r)
        for flag, key in (("ontology", "ontology"), ("semantic", "semantic")):
            if not have[ft].get(key, True) or not have[tt].get(key, True):
                r[flag] = False
        for side in ("from", "to"):
            tn, cn = r[side].split(".")
            col = next((c for c in have[tn]["columns"] if c["col"] == cn), None)
            if col is not None and col.get("ontology") is False:
                r["ontology"] = False
        rels.append(r)
    entity_of = {t["entity"]: t for t in tables}
    for t in tables:
        dom = re.sub(r"[^a-z0-9_]", "", str(t.get("domain") or "").lower())
        t["namespace"] = t.get("namespace") or dom or None            # domain -> ontology namespace (used with namespaces=True)
    measures = [dict(m) for m in model.get("measures", []) if m["table"] in have]
    changed = True
    while changed:
        names = {m["name"] for m in measures}
        keep = []
        for m in measures:
            tabs, bare = oac._dax_refs(m["expr"])
            ok = all(t in entity_of and entity_of[t].get("semantic", True) for t, _ in tabs) and all(b in names or any(
                b == c.get("label") for t in tables for c in t["columns"]) for b in bare)
            keep.append(m) if ok else None
        changed = len(keep) != len(measures)
        measures = keep
    ont_props = {f"{t['entity']}.{c.get('prop') or ''}" for t in tables if t.get("ontology", True) for c in t["columns"]}
    raw = {
        "version": model.get("version", 1), "name": model["name"], "lakehouse": model.get("lakehouse", {}),
        "semanticModel": model.get("semanticModel", {}), "ontology": model.get("ontology", {}), "descriptionsMode": mode,
        "tables": tables, "relationships": rels, "measures": measures,
        "rules": [dict(r) for r in model.get("rules", [])],
    }
    spec = oac.normalize_spec(raw)
    ont_props = {f"{t['entity']}.{c['prop']}" for t in spec["tables"] if t["ontology"] for c in t["columns"] if c["ontology"]}
    ont_rels = {r["name"] for r in spec["relationships"] if r["ontology"]}
    for rule in spec["rules"]:
        rule["properties"] = [p for p in rule.get("properties", []) or [] if p in ont_props]
        rule["relationships"] = [x for x in rule.get("relationships", []) or [] if x in ont_rels]
    return spec


def _paths(edges: dict[str, list[tuple[str, str]]]) -> tuple[dict[str, dict[str, int]], list[str]]:
    """Count directed paths between every pair of tables; report cycles. edges[a] = [(b, relationship name)]."""
    errors: list[str] = []
    state: dict[str, int] = {}
    order: list[str] = []

    def visit(n: str, stack: list[str]) -> None:
        if state.get(n) == 2:
            return
        if state.get(n) == 1:
            errors.append("directed cycle: " + " -> ".join(stack[stack.index(n):] + [n]))
            return
        state[n] = 1
        for m, _ in edges.get(n, []):
            visit(m, stack + [m])
        state[n] = 2
        order.append(n)

    for n in list(edges):
        visit(n, [n])
    counts: dict[str, dict[str, int]] = {}
    for n in order:                      # post-order: successors first
        c: dict[str, int] = collections.defaultdict(int)
        for m, _ in edges.get(n, []):
            c[m] += 1
            for k, v in counts.get(m, {}).items():
                c[k] += v
        counts[n] = dict(c)
    return counts, errors


def check_spec(spec: dict) -> oac.Report:
    """Phase-1 rules plus: keys only for ontology tables, FK columns visible to the ontology, DAX filter ambiguity."""
    r = oac.Report()
    tables = spec["tables"]
    if not tables:
        r.err("S001", "spec has no tables")
        return r
    by_table, by_entity = {}, {}
    by_physical: dict[str, str] = {}
    prop_types: dict[str, tuple[str, str]] = {}
    for t in tables:
        tn, en = t["table"], t["entity"]
        if tn in by_table:
            r.err("S003", f"duplicate table '{tn}'")
        by_table[tn] = t
        phys = oac.physical_of(t)
        if not SNAKE_RE.match(phys) or len(phys) > 128:
            r.err("S071", f"table '{tn}': lakehouse name '{phys}' must be snake_case ASCII")
        if phys in by_physical:
            r.err("S072", f"tables '{by_physical[phys]}' and '{tn}' both map to the lakehouse table '{phys}'")
        by_physical.setdefault(phys, tn)
        if not oac.IDENT_RE.match(en):
            r.err("S004", f"table '{tn}': entity '{en}' must be an identifier")
        elif not oac.NAME_RE.match(en):
            r.warn("S005", f"entity '{en}' exceeds the documented 1-26 char / alphanumeric-hyphen-underscore rule")
        mode = spec.get("descriptionsMode") or DEFAULT_DESCRIPTIONS
        budget = ENTITY_DESCRIPTION_MAX_COMPACT if mode == "compact" else ENTITY_DESCRIPTION_MAX
        if len(entity_doc(t)) > budget:
            r.err("S073", f"entity '{en}': description is {len(entity_doc(t))} characters ({mode} mode, the ontology adds its domain tag), the budget is {budget}; shorten its wording")
        if en in by_entity:
            r.err("S006", f"duplicate entity '{en}'")
        by_entity[en] = t
        cols = t["columns"]
        if not cols:
            r.err("S007", f"table '{tn}' has no columns")
        ont = t["ontology"]
        ns = t.get("namespace")
        if ns and (not re.fullmatch(r"[a-z][a-z0-9_]*", ns) or ns == "default"):
            r.err("S070", f"table '{tn}': namespace '{ns}' must be lowercase letters, digits or underscore, and not 'default'")
        if ont:
            keys = [c for c in cols if c["key"]]
            if len(keys) != 1:
                r.err("S008", f"table '{tn}' must declare exactly one key column (found {len(keys)})")
            elif keys[0]["type"] not in oac.KEY_TYPES:
                r.err("S009", f"table '{tn}': key column must be string or int (found {keys[0]['type']})")
            elif not keys[0]["ontology"]:
                r.err("S009", f"table '{tn}': key column is hidden from the ontology")
            if sum(1 for c in cols if c["default_label"] and c["ontology"]) > 1:
                r.warn("S010", f"table '{tn}' declares more than one default_label column")
        seen_cols, seen_labels, seen_props = set(), set(), set()
        for c in cols:
            cn, pn = c["col"], c["prop"]
            if cn in seen_cols:
                r.err("S012", f"table '{tn}': duplicate column '{cn}'")
            seen_cols.add(cn)
            if set(cn) & oac.SPECIAL_COL_CHARS:
                r.err("S013", f"column '{tn}.{cn}' has special characters: Fabric would enable column mapping, which the ontology graph does not support")
            if c["type"] == "decimal":
                r.err("S014", f"column '{tn}.{cn}': decimal returns null in Fabric Graph; use double or int64")
            elif c["type"] not in oac.TMDL_TYPE:
                r.err("S015", f"column '{tn}.{cn}': unsupported type '{c['type']}'")
            if c["label"] in seen_labels:
                r.err("S019", f"table '{tn}': duplicate label '{c['label']}'")
            seen_labels.add(c["label"])
            if len(c["desc"]) > COLUMN_DESCRIPTION_MAX:
                r.err("S074", f"column '{tn}.{cn}': description is {len(c['desc'])} characters, the budget is {COLUMN_DESCRIPTION_MAX}")
            if not (ont and c["ontology"]):
                continue
            if not oac.IDENT_RE.match(pn):
                r.err("S016", f"column '{tn}.{cn}': prop '{pn}' must be an identifier")
            elif not oac.NAME_RE.match(pn):
                r.warn("S017", f"prop '{pn}' exceeds the documented 1-26 char rule")
            if pn in seen_props:
                r.err("S018", f"table '{tn}': duplicate prop '{pn}'")
            seen_props.add(pn)
            prev = prop_types.get(pn)
            if prev and prev[0] != c["type"]:
                r.err("S020", f"property '{pn}' is {prev[0]} in {prev[1]} but {c['type']} in {tn}: duplicates must share a type")
            prop_types.setdefault(pn, (c["type"], tn))
    onames: set[str] = set()
    edges: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for rel in spec["relationships"]:
        n = rel.get("name", "")
        ends = []
        for side in ("from", "to"):
            tn, _, cn = str(rel.get(side, "")).partition(".")
            t = by_table.get(tn)
            c = next((x for x in (t or {}).get("columns", []) if x["col"] == cn), None)
            if not t or not c:
                r.err("S032", f"relationship '{n}': {side} '{rel.get(side)}' does not resolve to table.column")
            ends.append((t, c))
        if not all(e[0] and e[1] for e in ends):
            continue
        if ends[0][1]["type"] != ends[1][1]["type"]:
            r.err("S033", f"relationship '{n}': column types differ ({ends[0][1]['type']} vs {ends[1][1]['type']})")
        if not ends[1][1]["key"]:
            r.warn("S034", f"relationship '{n}': 'to' column is not the key of {ends[1][0]['table']}")
        if rel["ontology"]:
            if n in onames:
                r.err("S031", f"duplicate ontology relationship name '{n}' (breaks natural-language queries)")
            onames.add(n)
            if not (ends[0][0]["ontology"] and ends[1][0]["ontology"] and ends[0][1]["ontology"] and ends[1][1]["ontology"]):
                r.err("S035", f"relationship '{n}' is in the ontology but one end is hidden from it")
        if rel["semantic"]:
            if not (ends[0][0]["semantic"] and ends[1][0]["semantic"]):
                r.err("S036", f"relationship '{n}' is in the semantic model but one table is not")
            edges[ends[1][0]["table"]].append((ends[0][0]["table"], n))
            edges.setdefault(ends[0][0]["table"], [])
    counts, cyc = _paths(edges)
    for c in cyc:
        r.err("S061", f"semantic model has a {c}")
    for a, targets in counts.items():
        for b, k in targets.items():
            if k > 1:
                r.err("S060", f"semantic model: {k} filter paths from {a} to {b}; mark one relationship semantic: false (DAX would be ambiguous)")
    names = [m["name"] for m in spec["measures"]]
    if len(set(names)) != len(names):
        r.err("S040", "duplicate measure names")
    for m in spec["measures"]:
        if len(m.get("desc", "")) > COLUMN_DESCRIPTION_MAX:
            r.err("S074", f"measure '{m['name']}': description is {len(m['desc'])} characters, the budget is {COLUMN_DESCRIPTION_MAX}")
    if len(spec["ontology"].get("description", "")) > ONTOLOGY_DESCRIPTION_MAX:
        r.err("S073", f"the ontology item description is {len(spec['ontology']['description'])} characters; Fabric keeps {ONTOLOGY_DESCRIPTION_MAX} and the rest is cut")
    sm_cols = {t["entity"]: {c["label"] for c in t["columns"]} for t in tables if t["semantic"]}
    for m in spec["measures"]:
        t = by_table.get(m["table"])
        if not t:
            r.err("S041", f"measure '{m['name']}': unknown table '{m['table']}'")
            continue
        if m["name"] in sm_cols.get(t["entity"], set()):
            r.err("S042", f"measure '{m['name']}' collides with a column of {t['entity']}")
        qualified, bare = oac._dax_refs(m["expr"])
        for tbl, col in qualified:
            if tbl not in sm_cols:
                r.err("S044", f"measure '{m['name']}': unknown table '{tbl}' in DAX")
            elif col not in sm_cols[tbl] and col not in names:
                r.err("S045", f"measure '{m['name']}': '{tbl}[{col}]' is not a column label or measure")
        for ref in bare:
            if ref not in names:
                r.err("S046", f"measure '{m['name']}': [{ref}] is not a measure")
    props = {f"{t['entity']}.{c['prop']}" for t in tables if t["ontology"] for c in t["columns"] if c["ontology"]}
    seen_rules = set()
    for rule in spec["rules"]:
        rn = rule.get("name", "")
        if not oac.IDENT_RE.match(rn):
            r.err("S050", f"rule name '{rn}' must be an identifier")
        if rn in seen_rules:
            r.err("S051", f"duplicate rule '{rn}'")
        seen_rules.add(rn)
        if not str(rule.get("statement", "")).strip():
            r.err("S052", f"rule '{rn}' has no statement")
        for p in rule.get("properties", []) or []:
            if p not in props:
                r.err("S054", f"rule '{rn}': unknown property '{p}'")
        for x in rule.get("relationships", []) or []:
            if x not in onames:
                r.err("S055", f"rule '{rn}': unknown relationship '{x}'")
    ont_tables = [t for t in tables if t["ontology"]]
    r.stats = {"tables": len(tables), "entities": len(ont_tables),
               "properties": sum(1 for t in ont_tables for c in t["columns"] if c["ontology"]),
               "ontologyRelationships": len(onames), "semanticRelationships": sum(1 for x in spec["relationships"] if x["semantic"]),
               "measures": len(spec["measures"]), "rules": len(spec["rules"])}
    return r
