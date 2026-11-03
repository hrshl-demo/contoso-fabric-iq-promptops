#!/usr/bin/env python
"""One-off migration: rewrite model/gold/*.yaml from raw seed sources to silver sources, mechanically, and write docs/GOLD_LINEAGE.csv.

    python tools/migrate_gold_to_silver.py                 # rewrite model/gold/*.yaml in place, write docs/GOLD_LINEAGE.csv
    python tools/migrate_gold_to_silver.py --dry-run       # print the report only
    python tools/migrate_gold_to_silver.py --lineage-only  # rebuild docs/GOLD_LINEAGE.csv from the current YAML

What it does to every gold table that silver serves (silver tables list the gold table under `serves:`):
  * sources   -> `{silver: <silver table>}` (a `where` on `_release_id` only where one silver table feeds several differing sources; `set`
                 constants only where a column still reads them: silver carries the rest)
  * columns   -> every `from:` and `tpl` placeholder names a silver column; `$release` becomes `_release_id`, the owner of a `t: owner` /
                 `t: superseded_by` column becomes `_owner_id`, `^.x`, `@`, `$index`, `$kind` become the silver columns that hold them
  * transforms follow the silver representation: money -> `minor_to_inr` / `minor_to_paise` (through the `money:` macro), rupee amounts
                 and percent text -> `minor_to_inr` / `dec_to_float`, a `join` / `len` / `facts_text` silver already applied is dropped
  * adds `kind`, `grain`, `physical` (the design's table of the 51 v1 tables) and `silver: [tables]`
  * a column no silver column carries becomes `none: "<reason>"` (printed in the report: check each against the seed)
Tables that read curated docs/config (laws, authority matrix, requirements, bank agents, service request types, journeys and their links,
the generated calendar) keep their sources and get `reference: true`. Comments, descriptions and everything else are kept verbatim
(the YAML is edited line by line, not re-dumped). The v1 equivalence fixtures (tests/test_gold_equivalence.py) are the arbiter of the result.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))

from contoso_onto import gold, silver  # noqa: E402

# the design's mapping of the 51 v1 tables (docs/MEDALLION_DESIGN.md section 6): logical id -> (physical name, kind, grain); seven physical names
# follow the naming standard instead (docs/NAMING_STANDARD.md: <role>_<snake(entity)>), as the committed model/gold/*.yaml has them
STAR: dict[str, tuple[str, str, str]] = {
    "customer": ("dim_customer", "dimension", "One customer identity per release."),
    "household": ("dim_household", "dimension", "One household."),
    "household_member": ("dim_household_member", "dimension", "One household member."),
    "account": ("dim_account", "dimension", "One account, with its balances as at the release business date."),
    "card": ("dim_card", "dimension", "One card."),
    "deposit": ("dim_deposit", "dimension", "One deposit."),
    "loan": ("dim_loan", "dimension", "One loan."),
    "loan_installment": ("fact_loan_installment", "fact", "One scheduled instalment of a loan."),
    "insurance_policy": ("dim_protection_policy", "dimension", "One insurance policy."),
    "protection_gap": ("fact_protection_gap", "fact", "One computed protection gap per customer and cover type at the business date (snapshot)."),
    "investment": ("dim_investment", "dimension", "One investment holding."),
    "investment_goal": ("bridge_investment_goal", "bridge", "One link between an investment and a goal it funds."),
    "transaction": ("fact_transaction", "fact", "One ledger posting."),
    "transaction_evidence": ("bridge_transaction_evidence", "bridge", "One link between a transaction and a document or case that evidences it."),
    "scheduled_obligation": ("fact_scheduled_obligation", "fact", "One scheduled obligation."),
    "income_record": ("fact_income_record", "fact", "One income record."),
    "goal": ("dim_goal", "dimension", "One goal."),
    "preference": ("dim_preference", "dimension", "One preference key per customer."),
    "consent": ("dim_consent", "dimension", "One consent."),
    "consent_resource": ("bridge_consent_resource", "bridge", "One document covered by a consent."),
    "access_grant": ("dim_household_access_grant", "dimension", "One access grant."),
    "grant_permission": ("bridge_grant_permission", "bridge", "One allowed or denied permission of an access grant."),
    "registered_agent": ("dim_registered_agent", "dimension", "One registered agent."),
    "agent_scope": ("bridge_agent_scope", "bridge", "One allowed or denied scope of a registered agent."),
    "mandate": ("dim_frontier_mandate", "dimension", "One mandate."),
    "mandate_event": ("fact_mandate_action", "fact", "One mandate action."),
    "incident": ("fact_incident", "fact", "One incident."),
    "service_record": ("fact_service_case", "fact", "One service case."),
    "proactive_event": ("fact_proactive_candidate", "fact", "One proactive candidate."),
    "service_request_type": ("dim_service_request_type", "dimension", "One service request type (curated reference source)."),
    "document": ("dim_document", "dimension", "One document."),
    "crm_case": ("fact_crm_case", "fact", "One CRM case."),
    "product": ("dim_product", "dimension", "One catalogue product."),
    "product_parameter": ("dim_product_parameter", "dimension", "One parameter of one product (outrigger of dim_product)."),
    "journey": ("dim_journey", "dimension", "One journey (curated reference source)."),
    "sop": ("dim_sop", "dimension", "One standard operating procedure."),
    "journey_sop": ("bridge_journey_sop", "bridge", "One link between a journey and an SOP it follows (curated reference source)."),
    "journey_product": ("bridge_journey_product", "bridge", "One link between a journey and a product it may discuss (curated reference source)."),
    "law": ("ref_law", "reference", "One design law."),
    "authority_rule": ("ref_authority_rule", "reference", "One authority rule: a capability and the actor's permission for it."),
    "frontier_requirement": ("ref_frontier_requirement", "reference", "One retail requirement."),
    "bank_agent": ("ref_bank_agent", "reference", "One bank agent."),
    "business_unit": ("dim_business_unit", "dimension", "One business unit."),
    "region": ("dim_region", "dimension", "One region."),
    "relationship_manager": ("dim_relationship_manager", "dimension", "One relationship manager."),
    "campaign": ("dim_campaign", "dimension", "One campaign."),
    "monthly_metric": ("fact_monthly_metric", "fact", "One metric row per month and business unit."),
    "risk_alert": ("fact_risk_alert", "fact", "One risk alert."),
    "fraud_signal": ("fact_fraud_signal", "fact", "One fraud signal."),
    "product_business_unit": ("bridge_product_business_unit", "bridge", "One link between a product and the business unit that owns it."),
    "calendar": ("dim_date", "dimension", "One calendar day (generated)."),
}
# silver transforms whose result a gold column must not apply a second time
SILVER_APPLIED = ("join", "len", "facts_text")
# post hooks (contoso_onto/hooks.py): output column -> the gold columns it is computed from, for the lineage report
HOOK_INPUTS = {"txn_signed": {"signed_amount_inr": ["amount_inr", "direction"]},
               "goal_progress": {"progress_pct": ["target_inr", "current_inr", "target_frequency"]},
               "consent_status": {"status": ["revoked_at"]},
               "product_names": {"display_name": ["product_id", "family"]}}
LINEAGE_HEADER = ["gold_table", "gold_column", "silver_table", "silver_column", "transform"]


# --------------------------------------------------------------------------------------------------
# YAML text: one flow mapping per line, edited in place so comments and descriptions survive
# --------------------------------------------------------------------------------------------------
def split_flow(line: str) -> tuple[str, list[str], str]:
    """`<prefix>{a: 1, b: [x, y]}<suffix>` -> (prefix incl. the brace, ['a: 1', 'b: [x, y]'], suffix from the closing brace)."""
    start = line.index("{")
    depth, quote, buf, items = 1, None, [], []
    i = start + 1
    while i < len(line):
        ch = line[i]
        if quote:
            buf.append(ch)
            if quote == '"' and ch == "\\" and i + 1 < len(line):
                buf.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                if quote == "'" and line[i + 1:i + 2] == "'":
                    buf.append("'")
                    i += 2
                    continue
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                items.append("".join(buf).strip())
                return line[:start + 1], [x for x in items if x], line[i:]
            buf.append(ch)
        elif ch == "," and depth == 1:
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    raise ValueError(f"unbalanced flow mapping: {line!r}")


def render(value: Any) -> str:
    """A YAML flow value the way the files write them: bare words, double-quoted anything else."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {render(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(render(v) for v in value) + "]"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    s = str(value)
    bare = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\-]*", s) and s.lower() not in ("true", "false", "null", "yes", "no", "on", "off")
    return s if bare else json.dumps(s, ensure_ascii=False)


def key_of(item: str) -> str:
    return item.partition(":")[0].strip()


def set_item(items: list[str], key: str, value: Any, after: Optional[str] = None) -> list[str]:
    """Replace `key`'s value (keeping its position), else insert it after the key `after` (else append)."""
    text = f"{key}: {render(value)}"
    for i, it in enumerate(items):
        if key_of(it) == key:
            return items[:i] + [text] + items[i + 1:]
    pos = next((i + 1 for i, it in enumerate(items) if key_of(it) == after), len(items))
    return items[:pos] + [text] + items[pos:]


def drop_item(items: list[str], key: str) -> list[str]:
    return [it for it in items if key_of(it) != key]


# --------------------------------------------------------------------------------------------------
# silver side
# --------------------------------------------------------------------------------------------------
@dataclass
class SilverTable:
    name: str
    spec: dict
    cols: dict[str, dict]                       # profile + extension columns by name
    paths: dict[str, list[str]]                 # seed path -> the columns that read it, in spec order
    owner: Optional[str]
    sources: list[dict]


def silver_tables(spec: dict) -> dict[str, SilverTable]:
    out: dict[str, SilverTable] = {}
    for t in spec["tables"]:
        cols = {c["col"]: c for c in t["columns"]}
        paths: dict[str, list[str]] = {}
        for c in t["columns"]:
            for p in silver.source_paths(c):
                paths.setdefault(p, []).append(c["col"])
        out[t["table"]] = SilverTable(t["table"], t, cols, paths, t.get("owner"), t["sources"])
    return out


def serving(spec: dict) -> dict[str, list[str]]:
    served: dict[str, list[str]] = {}
    for t in spec["tables"]:
        for g in t["serves"]:
            served.setdefault(g, []).append(t["table"])
    return served


def source_descriptor(src: dict) -> tuple:
    """What a source reads, ignoring `set`: gold and silver describe the same seed file the same way, except for custom flatteners."""
    if "custom" in src:
        return ("custom", src["custom"], src.get("release") if src["custom"] == "preference_unpivot" else None)
    return ("file", src.get("release"), src.get("file") or src.get("data"), src.get("list"), src.get("explode"))


def pick(candidates: list[str], t: SilverTable, path: str, gold_t: Optional[str] = None) -> tuple[str, bool]:
    """The silver column for a path several columns read: the one whose silver transform is the gold column's (a gold `join` or `len` is
    already applied in silver, in different columns), then the one that reads exactly `path`. Also says whether the choice stayed ambiguous."""
    want = {p.partition(":")[0] for p in (gold_t or "").split("|") if p} & set(SILVER_APPLIED)

    def applied(c: str) -> set[str]:
        return {p.partition(":")[0] for p in (t.cols[c].get("t") or "").split("|") if p}

    exact = [c for c in candidates if silver.source_paths(t.cols[c]) == [path]]
    ranked = sorted(candidates, key=lambda c: (-len(want & applied(c)), c not in exact, candidates.index(c)))
    best = ranked[0]
    ties = [c for c in ranked if (len(want & applied(c)), c in exact) == (len(want & applied(best)), best in exact)]
    return best, len(ties) > 1


@dataclass
class Plan:
    table: str
    silver: list[str]
    sources: list[dict]
    columns: dict[int, dict] = field(default_factory=dict)        # column line index -> rewrite instructions
    warnings: list[str] = field(default_factory=list)
    nones: list[str] = field(default_factory=list)
    referenced_set: set[str] = field(default_factory=set)


def plan_sources(gold_t: dict, tabs: dict[str, SilverTable], served: list[str], plan: Plan) -> list[tuple[dict, str, int]]:
    """Match every gold source with the silver table and silver source that read the same seed file: [(gold source, silver table, index)]."""
    matched: list[tuple[dict, str, int]] = []
    for g in gold_t["sources"]:
        hit = [(s, i) for s in served for i, ss in enumerate(tabs[s].sources) if source_descriptor(ss) == source_descriptor(g)]
        if len(hit) != 1:
            plan.warnings.append(f"source {source_descriptor(g)} matches {len(hit)} silver sources among {served}")
            continue
        matched.append((g, *hit[0]))
        gold_req, silver_req = g.get("require"), tabs[hit[0][0]].sources[hit[0][1]].get("require")
        if gold_req != silver_req:
            plan.warnings.append(f"source {source_descriptor(g)}: require differs (gold {gold_req}, silver {silver_req})")
    return matched


def build_sources(matched: list[tuple[dict, str, int]], tabs: dict[str, SilverTable], used_set: set[str]) -> list[dict]:
    """One `{silver: ...}` source per run of gold sources on the same silver table; consecutive gold sources that cover all that table's
    silver sources in order, with the same remaining `set`, collapse into one. Otherwise each keeps a `where` on its release."""
    runs: list[list[tuple[dict, str, int]]] = []
    for m in matched:
        if runs and runs[-1][0][1] == m[1]:
            runs[-1].append(m)
        else:
            runs.append([m])
    out: list[dict] = []
    for run in runs:
        table = run[0][1]
        sets = [{k: v for k, v in (g.get("set") or {}).items() if k in used_set} for g, _, _ in run]
        whole = [i for _, _, i in run] == list(range(len(tabs[table].sources)))
        if whole and all(s == sets[0] for s in sets):
            out.append({"silver": table, **({"set": sets[0]} if sets[0] else {})})
            continue
        for (g, _, i), s in zip(run, sets):
            ss = tabs[table].sources[i]
            rel = ss.get("release")
            if "release" in ss or "data" in ss or "file" in ss:
                where = {"_release_id": rel}
            else:
                where = {}
            out.append({"silver": table, **({"where": where} if len(tabs[table].sources) > 1 and where else {}), **({"set": s} if s else {})})
    return out


def map_path(path: str, serving_tabs: list[SilverTable], owner_path: bool, plan: Optional[Plan] = None, gold_t: Optional[str] = None) -> list[str]:
    """The silver column(s) a gold row path reads, one per serving silver table that has it (in table order, de-duplicated). Where several
    silver columns read the same seed field (a value kept as text, integer and decimal, say) the first is taken and the report says so."""
    found: list[str] = []
    for t in serving_tabs:
        if path == "$release":
            col = "_release_id"
        elif owner_path and t.owner and path == t.owner:
            col = "_owner_id"
        elif path in t.paths:
            col, ambiguous = pick(t.paths[path], t, path, gold_t)
            if ambiguous and plan is not None:
                plan.warnings.append(f"'{path}' is read by several columns of {t.name} {t.paths[path]}; took {col}: choose by hand")
        else:
            continue
        if col not in found:
            found.append(col)
    return found


def silver_col(name: str, serving_tabs: list[SilverTable]) -> Optional[dict]:
    for t in serving_tabs:
        if name in t.cols:
            return t.cols[name]
    return None


def map_transform(gold_col: dict, silver_cols: list[dict], plan: Plan) -> Optional[str]:
    """The gold `t:` for the silver representation of the column (None: no transform)."""
    t = gold_col.get("t")
    parts = [p for p in (t or "").split("|") if p]
    types = {c["type"] for c in silver_cols}
    stype = next(iter(types)) if len(types) == 1 else None
    if len(types) > 1:
        plan.warnings.append(f"{gold_col.get('col')}: reads silver columns of different types {sorted(types)}")
    applied = {p.partition(":")[0] for c in silver_cols for p in (c.get("t") or "").split("|") if p}
    out: list[str] = []
    for p in parts:
        name = p.partition(":")[0]
        if name in SILVER_APPLIED and name in applied:
            continue
        if name == "float" and stype == "minor":
            out.append("minor_to_inr")
        elif name == "float" and stype == "decimal":
            out.append("dec_to_float")
        else:
            out.append(p)
    if not parts and gold_col.get("type") == "double":
        if stype == "decimal":
            out.append("dec_to_float")
        elif stype == "minor":
            out.append("minor_to_inr")
    return "|".join(out) or None


def plan_column(c: dict, serving_tabs: list[SilverTable], plan: Plan) -> dict:
    """What to change on one column's flow mapping: {'from': value, 'tpl': text, 't': value|None, 'unit': 'pct_text', 'none': reason};
    only what differs from the v1 text is returned."""
    macro = next((k for k in ("money", "rate_bps", "rate_pct", "instant") if k in c), None)
    name = c.get("col") or c.get(macro, "?")
    change: dict[str, Any] = {}
    owner_path = (c.get("t") or "").split("|")[0].partition(":")[0] in ("owner", "superseded_by")
    if "tpl" in c:
        def sub(m: re.Match) -> str:
            cols = map_path(m.group(1), serving_tabs, False, plan)
            if not cols:
                plan.warnings.append(f"{name}: tpl placeholder {{{m.group(1)}}} has no silver column")
                return m.group(0)
            return "{" + cols[0] + (":" + m.group(2) if m.group(2) else "") + "}"
        new_tpl = gold.TPL_RE.sub(sub, c["tpl"])
        if new_tpl != c["tpl"]:
            change["tpl"] = new_tpl
        return change
    if "from" not in c:
        return change
    paths = c["from"] if isinstance(c["from"], list) else [c["from"]]
    cols: list[str] = []
    dropped: list[str] = []
    for p in paths:
        got = map_path(p, serving_tabs, owner_path, plan, c.get("t"))
        if not got and p.startswith("$"):
            plan.referenced_set.add(p[1:])                  # a context constant that the gold source `set:` still provides
            got = [p]
        if not got:
            dropped.append(p)
        cols.extend(g for g in got if g not in cols)
    if not cols:
        reason = f"no silver column carries the seed field {', '.join(dropped)}"
        change["none"] = f"{reason}: the column stays for v1 compatibility and is null (check the seed: no record has it)"
        plan.nones.append(f"{name}: {reason}")
        return change
    if dropped:
        plan.warnings.append(f"{name}: dropped fallback path(s) {dropped} that no silver column carries")
    scols = [sc for x in cols if (sc := silver_col(x, serving_tabs)) is not None]
    for sc in scols:                            # a silver column that merges the gold alternatives must keep their order
        own = silver.source_paths(sc)
        hits = [p for p in paths if p in own]
        if len(hits) > 1 and [p for p in own if p in hits] != hits:
            plan.warnings.append(f"{name}: gold prefers {hits} in that order, silver column {sc['col']} reads {own}")
    if len(paths) > 1 and len(cols) > 1 and len(serving_tabs) == 1:
        plan.warnings.append(f"{name}: alternatives {paths} became silver columns {cols}; the first one present wins, as in v1")
    new_from = cols[0] if len(cols) == 1 else cols
    if new_from != c["from"]:
        change["from"] = new_from
    if macro is None:
        new_t = map_transform(c, scols, plan) if scols else c.get("t")
        if new_t != c.get("t"):
            change["t"] = new_t
    elif macro == "rate_bps" and scols and {sc["type"] for sc in scols} == {"decimal"}:
        change["unit"] = "pct_text"
    elif macro == "rate_pct" and scols and {sc["type"] for sc in scols} != {"decimal"}:
        plan.warnings.append(f"{name}: rate_pct over silver columns that are not decimal text")
    elif macro == "money" and scols and {sc["type"] for sc in scols} != {"minor"}:
        plan.warnings.append(f"{name}: money over silver columns that are not minor units")
    return change


# --------------------------------------------------------------------------------------------------
# the rewrite
# --------------------------------------------------------------------------------------------------
def table_blocks(lines: list[str]) -> list[tuple[str, int, int]]:
    """(logical id, first line, end line) of every `  - table: x` block."""
    starts = [(m.group(1), i) for i, ln in enumerate(lines) if (m := re.match(r"^  - table:\s*(\w+)\s*$", ln))]
    return [(n, s, starts[k + 1][1] if k + 1 < len(starts) else len(lines)) for k, (n, s) in enumerate(starts)]


def migrate_text(text: str, spec: dict, report: list[str]) -> str:
    """Rewrite one gold YAML file (its text) against the silver spec; the report collects warnings per table."""
    lines = text.split("\n")
    tabs, served = silver_tables(spec), serving(spec)
    out: list[str] = []
    cursor = 0
    for name, start, end in table_blocks(lines):
        out.extend(lines[cursor:start])
        cursor = end
        block = lines[start:end]
        doc = yaml.safe_load("\n".join(block))[0]
        if "silver" in doc or doc.get("reference"):
            out.extend(block)
            report.append(f"{name}: already migrated")
            continue
        star = STAR[name]
        head = [f"    kind: {star[1]}", f"    grain: {json.dumps(star[2])}", f"    physical: {star[0]}"]
        if name not in served:
            out.extend(_with_header(block, head + ["    reference: true"]))
            report.append(f"{name}: reference table, sources kept")
            continue
        out.extend(_migrate_block(name, block, doc, tabs, served[name], head, report))
    out.extend(lines[cursor:])
    return "\n".join(out)


def _with_header(block: list[str], head: list[str]) -> list[str]:
    i = next(k for k, ln in enumerate(block) if re.match(r"^    domain:", ln))
    return block[:i + 1] + head + block[i + 1:]


def _migrate_block(name: str, block: list[str], doc: dict, tabs: dict[str, SilverTable], served: list[str], head: list[str],
                   report: list[str]) -> list[str]:
    plan = Plan(name, served, [])
    matched = plan_sources(doc, tabs, served, plan)
    serving_tabs = [tabs[s] for s in dict.fromkeys(m[1] for m in matched)]       # in the order the gold sources read them
    columns_at = next(k for k, x in enumerate(block) if x.strip() == "columns:")
    plans: dict[int, dict] = {}                                  # pass 1: column plans, which also tell which `set` constants are still read
    for i, ln in enumerate(block):
        if i > columns_at and re.match(r"^      - \{", ln):
            plans[i] = plan_column(yaml.safe_load(ln.strip()[2:]), serving_tabs, plan)
    plan.sources = build_sources(matched, tabs, plan.referenced_set)
    silver_names = list(dict.fromkeys(s["silver"] for s in plan.sources))
    new: list[str] = []                                          # pass 2: emit
    in_sources = wrote_sources = False
    for i, ln in enumerate(block):
        if re.match(r"^    sources:\s*$", ln):
            in_sources = True
            new.append(ln)
            continue
        if in_sources:
            if re.match(r"^      - \{", ln):
                if not wrote_sources:
                    new.extend(f"      - {render(s)}" for s in plan.sources)
                    wrote_sources = True
                continue
            in_sources = False
        if i in plans:
            new.append(_rewrite_column(ln, plans[i]))
            continue
        new.append(ln)
        if re.match(r"^    domain:", ln):
            new.extend(head + [f"    silver: {render(silver_names)}"])
    report.append(f"{name}: silver {silver_names}" + "".join(f"\n    WARNING {w}" for w in dict.fromkeys(plan.warnings))
                  + "".join(f"\n    NONE    {w}" for w in plan.nones))
    return new


def _rewrite_column(line: str, change: dict) -> str:
    if not change:
        return line
    prefix, items, suffix = split_flow(line)
    if "none" in change:
        i = next(k for k, it in enumerate(items) if key_of(it) == "from")
        items = drop_item(items[:i] + [f"none: {render(change['none'])}"] + items[i + 1:], "t")
    if "from" in change:
        items = set_item(items, "from", change["from"])
    if "tpl" in change:
        items = set_item(items, "tpl", change["tpl"])
    if "unit" in change:
        items = set_item(items, "unit", change["unit"], after="from")
    if "t" in change:
        anchor = "from" if any(key_of(x) == "from" for x in items) else "tpl"
        items = drop_item(items, "t") if change["t"] is None else set_item(items, "t", change["t"], after=anchor)
    return prefix + ", ".join(items) + suffix


# --------------------------------------------------------------------------------------------------
# lineage: gold column -> silver table + column -> transform
# --------------------------------------------------------------------------------------------------
def lineage_rows(model: dict, spec: dict) -> list[list[str]]:
    """One row per gold column and per silver column it reads (alternatives are separate rows). Constants, `none` columns, context
    values and post-hook columns say so in the transform; a reference table names its curated source instead of a silver table."""
    columns = {t["table"]: {c["col"] for c in silver.emitted_columns(t)} for t in spec["tables"]}
    rows: list[list[str]] = []
    for t in model["tables"]:
        sources = t["sources"]
        names = list(dict.fromkeys(s["silver"] for s in sources if "silver" in s))
        reference = "; ".join(f"reference: {s.get('doc') or s.get('data') or s.get('data_glob') or 'custom ' + str(s.get('custom'))}" for s in sources)
        hook_of = {col: (h, ins) for h in t.get("post") or [] for col, ins in HOOK_INPUTS.get(h, {}).items()}
        own: dict[str, list[list[str]]] = {}
        order = []
        for c in gold.expand_columns(t):
            order.append(c["col"])
            base, tr = [t["table"], c["col"]], c.get("t") or ""
            got = own.setdefault(c["col"], [])
            if "none" in c:
                got.append(base + ["", "", f"none: {c['none']}"])
            elif "const" in c and "from" not in c:
                got.append(base + ["", "", f"const: {json.dumps(c['const'])}"])
            for p in gold._paths_of(c):
                if not names:
                    got.append(base + [reference, p, tr])
                elif p.startswith("$"):
                    consts = sorted({str(s["set"][p[1:]]) for s in sources if p[1:] in (s.get("set") or {})})
                    got.append(base + ["", "", f"context {p}" + (f" = {', '.join(consts)}" if consts else "")])
                else:
                    hits = [s for s in names if p in columns[s]]
                    if hits:
                        got.extend(base + [s, p, tr] for s in hits)
                    else:
                        got.append(base + ["", p, "UNRESOLVED"])
        for col in order:
            rows.extend(own[col])
            if col in hook_of:
                hook, inputs = hook_of[col]
                rows.extend([t["table"], col, r[2], r[3], f"post hook {hook} via {r[1]}"] for i in inputs for r in own.get(i, []) if r[2])
    return rows


def lineage_csv(rows: list[list[str]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(LINEAGE_HEADER)
    w.writerows(rows)
    return buf.getvalue()


# --------------------------------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model-dir", default=str(KIT / "model"))
    ap.add_argument("--lineage", default=str(KIT / "docs" / "GOLD_LINEAGE.csv"))
    ap.add_argument("--dry-run", action="store_true", help="print the report, write nothing")
    ap.add_argument("--lineage-only", action="store_true", help="only rebuild the lineage CSV from the current YAML")
    a = ap.parse_args(argv)
    model_dir = Path(a.model_dir)
    spec = silver.load_silver_spec(model_dir)
    if not a.lineage_only:
        report: list[str] = []
        for f in sorted((model_dir / "gold").glob("*.yaml")):
            text = f.read_text(encoding="utf-8")
            new = migrate_text(text, spec, report)
            if new != text and not a.dry_run:
                f.write_text(new, encoding="utf-8", newline="\n")
        print("\n".join(report))
    if not a.dry_run:
        rows = lineage_rows(gold.load_model(model_dir), spec)
        Path(a.lineage).write_text(lineage_csv(rows), encoding="utf-8", newline="\n")
        print(f"wrote {a.lineage}: {len(rows)} lineage rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
