#!/usr/bin/env python3
"""ontology_as_code.py - spec-driven Fabric semantic model (Direct Lake) + Fabric IQ ontology (Gen 2 / TMDL).

  build     spec -> semantic model parts, ontology parts, REST payloads, agent grounding brief
  validate  static checks on a folder of TMDL parts (ontology or semantic model)
  apply     create / update the Fabric items through the REST API (dry-run unless --execute)
  export    getDefinition of an existing item to a folder (portal seed, backup, Git)

Only syntax proven by the official WindFarm ontology sample and Microsoft's TMDL guidelines is emitted.
Synonyms, additional metadata, top-level metrics and resource links are NOT emitted: seed them with the
portal / ontology agent, then round-trip with `export`.
"""
from __future__ import annotations

import argparse
import base64
import collections
import contextlib
import datetime as dt
import email.utils
import json
import math
import os
import random
import re
import shutil
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import yaml

FABRIC_API = "https://api.fabric.microsoft.com/v1"
PBI_API = "https://api.powerbi.com/v1.0/myorg"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
PBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"

NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9_-]{0,24}[A-Za-z0-9])?$")  # 1-26 chars (documented for the original experience)
ONT_ITEM_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,88}$")            # the ontology ITEM name (live: letters, digits, underscores only)
IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
SPECIAL_COL_CHARS = set(",;{}()\n\t= ")
TMDL_TYPE = {"string": "string", "int": "int64", "long": "int64", "double": "double", "float": "double",
             "bool": "boolean", "boolean": "boolean", "datetime": "dateTime", "timestamp": "dateTime", "date": "dateTime"}
GRAPH_TYPE = {"string": "STRING", "int": "INT", "long": "INT", "double": "FLOAT", "float": "FLOAT",     # spec type -> GraphModel property type
              "bool": "BOOLEAN", "boolean": "BOOLEAN", "datetime": "ZONED DATETIME", "timestamp": "ZONED DATETIME", "date": "ZONED DATETIME"}
GRAPH_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/graphIndex/definition/{}/1.0.0/schema.json"
GRAPH_PARTS = ("graphType.json", "dataSources.json", "graphDefinition.json", "stylingConfiguration.json", "graphSettings.json")
KEY_TYPES = {"string", "int", "long"}
PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_]+>")
ONT_DATATYPES = {"string", "int64", "double", "boolean", "dateTime", "decimal"}
FLAGS = {"isHidden", "isDefaultLabel", "isKey", "discourageImplicitMeasures"}
ONT_KINDS = {"entity", "entityRelationship", "rule", "namespace"}


# --------------------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------------------
@dataclass
class Report:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)

    def err(self, code: str, msg: str) -> None:
        self.errors.append(f"{code}: {msg}")

    def warn(self, code: str, msg: str) -> None:
        self.warnings.append(f"{code}: {msg}")

    @property
    def ok(self) -> bool:
        return not self.errors

    def show(self, title: str) -> None:
        print(f"[{title}] errors={len(self.errors)} warnings={len(self.warnings)} stats={self.stats}")
        for e in self.errors:
            print("  ERROR  ", e)
        for w in self.warnings:
            print("  warning", w)


def snake_to_pascal(s: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[_\s-]+", s) if p)


ACRONYMS = {"Id": "ID", "Kya": "KYA", "Crm": "CRM", "Emi": "EMI", "Sla": "SLA", "Bu": "BU", "Rm": "RM", "Nim": "NIM",
            "Nps": "NPS", "Ltv": "LTV", "Sop": "SOP", "Mm": "MM", "Yy": "YY", "Ist": "IST", "Utc": "UTC", "Fd": "FD", "Ai": "AI"}
UNIT_SUFFIX = {"Inr": "INR", "Pct": "%", "Bps": "bps", "Paise": "paise"}


def pascal_to_label(s: str) -> str:
    """CustomerId -> Customer ID; PostedBalanceInr -> Posted Balance (INR); InterestRatePct -> Interest Rate (%)."""
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", s)
    unit = UNIT_SUFFIX.get(words[-1]) if len(words) > 1 else None
    if unit:
        words = words[:-1]
    label = " ".join(ACRONYMS.get(w, w) for w in words)
    return f"{label} ({unit})" if unit else label


def snake(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def q(name: str) -> str:
    """Quote a TMDL identifier when needed."""
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else "'" + name.replace("'", "''") + "'"


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def lineage_tag(spec: dict, kind: str, name: str) -> str:
    """Stable id of an ontology object (kind: entity, entityRelationship, ...). The graph aliases reuse the entity and relationship tags."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"urn:ontology-as-code:{spec['name']}:{kind}:{name}"))


class W:
    """Tab-indented TMDL writer."""

    def __init__(self) -> None:
        self.lines: List[str] = []

    def add(self, level: int = 0, text: str = "") -> None:
        self.lines.append(("\t" * level + text) if text else "")

    def doc(self, level: int, text: str) -> None:
        for ln in (text or "").strip().splitlines():
            if ln.strip():
                self.add(level, "/// " + ln.strip())

    def text(self) -> str:
        return "\n".join(self.lines).rstrip("\n") + "\n"


# --------------------------------------------------------------------------------------------------
# spec
# --------------------------------------------------------------------------------------------------
def normalize_spec(raw: dict) -> dict:
    spec = json.loads(json.dumps(raw))
    spec.setdefault("version", 1)
    spec.setdefault("name", "BankPoc")
    lh = spec.setdefault("lakehouse", {})
    lh.setdefault("name", "BankLH")
    lh.setdefault("schema", "dbo")
    sm = spec.setdefault("semanticModel", {})
    sm.setdefault("name", f"{spec['name']}Model")
    sm.setdefault("culture", "en-US")
    ont = spec.setdefault("ontology", {})
    ont.setdefault("name", f"{spec['name']}Ontology")
    ont.setdefault("description", "")
    for key in ("tables", "relationships", "measures", "rules"):
        spec.setdefault(key, [])
    for t in spec["tables"]:
        t.setdefault("entity", snake_to_pascal(t.get("table", "")))
        t.setdefault("physical", t.get("table", ""))   # the lakehouse table behind the entity; the logical id (`table`) unless the spec names one
        t.setdefault("description", "")
        t.setdefault("synonyms", [])
        t.setdefault("ontology", True)     # False: semantic-model-only table
        t.setdefault("semantic", True)     # False: ontology-only table
        t.setdefault("tier", "core")
        t.setdefault("domain", "")
        t.setdefault("namespace", None)    # ontology namespace (used only with namespaces=True); None = default namespace
        for c in t.get("columns", []):
            c.setdefault("prop", snake_to_pascal(c.get("col", "")))
            c["type"] = str(c.get("type", "string")).lower()
            c["label"] = c.get("label") or pascal_to_label(c["prop"])
            for flag in ("pii", "hidden", "key", "default_label"):
                c.setdefault(flag, False)
            c.setdefault("ontology", True)  # False: column stays in the lakehouse + semantic model, hidden from the ontology
            c.setdefault("desc", "")
    for rel in spec["relationships"]:
        rel.setdefault("semantic", True)
        rel.setdefault("ontology", True)
    return spec


def load_spec(path: str) -> dict:
    return normalize_spec(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def _dax_refs(expr: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    qualified = re.findall(r"(?:'([^']+)'|\b([A-Za-z_][A-Za-z0-9_]*))\[([^\]]+)\]", expr)
    tables = [((a or b), c) for a, b, c in qualified]
    bare = re.findall(r"(?<![A-Za-z0-9_'\]])\[([^\]]+)\]", expr)
    return tables, bare


def check_spec(spec: dict) -> Report:
    r = Report()
    tables = spec.get("tables") or []
    if not tables:
        r.err("S001", "spec has no tables")
        return r
    by_table: Dict[str, dict] = {}
    by_entity: Dict[str, dict] = {}
    prop_types: Dict[str, Tuple[str, str]] = {}
    for t in tables:
        tn, en = t.get("table"), t.get("entity")
        if not tn:
            r.err("S002", "a table entry has no 'table'")
            continue
        if tn in by_table:
            r.err("S003", f"duplicate table '{tn}'")
        by_table[tn] = t
        if not en or not IDENT_RE.match(en):
            r.err("S004", f"table '{tn}': entity '{en}' must be an identifier (letters, digits, underscore)")
        elif not NAME_RE.match(en):
            r.warn("S005", f"entity '{en}' exceeds the documented 1-26 char / alphanumeric-hyphen-underscore rule")
        if en in by_entity:
            r.err("S006", f"duplicate entity '{en}'")
        by_entity[en] = t
        cols = t.get("columns") or []
        if not cols:
            r.err("S007", f"table '{tn}' has no columns")
        keys = [c for c in cols if c["key"]]
        if len(keys) != 1:
            r.err("S008", f"table '{tn}' must declare exactly one key column (found {len(keys)})")
        elif keys[0]["type"] not in KEY_TYPES:
            r.err("S009", f"table '{tn}': key column must be string or int (found {keys[0]['type']})")
        if sum(1 for c in cols if c["default_label"]) > 1:
            r.warn("S010", f"table '{tn}' declares more than one default_label column")
        seen_cols, seen_labels, seen_props = set(), set(), set()
        for c in cols:
            cn, pn = c.get("col"), c["prop"]
            if not cn:
                r.err("S011", f"table '{tn}': a column has no 'col'")
                continue
            if cn in seen_cols:
                r.err("S012", f"table '{tn}': duplicate column '{cn}'")
            seen_cols.add(cn)
            bad = sorted(set(cn) & SPECIAL_COL_CHARS)
            if bad:
                r.err("S013", f"column '{tn}.{cn}' has characters {bad!r}: Fabric enables column mapping, which ontology graph does not support")
            if c["type"] == "decimal":
                r.err("S014", f"column '{tn}.{cn}': decimal returns null in Fabric Graph; cast to double in the gold layer")
            elif c["type"] not in TMDL_TYPE:
                r.err("S015", f"column '{tn}.{cn}': unsupported type '{c['type']}' (use {sorted(TMDL_TYPE)})")
            if not IDENT_RE.match(pn):
                r.err("S016", f"column '{tn}.{cn}': prop '{pn}' must be an identifier")
            elif not NAME_RE.match(pn):
                r.warn("S017", f"prop '{pn}' exceeds the documented 1-26 char rule")
            if pn in seen_props:
                r.err("S018", f"table '{tn}': duplicate prop '{pn}'")
            seen_props.add(pn)
            if c["label"] in seen_labels:
                r.err("S019", f"table '{tn}': duplicate label '{c['label']}'")
            seen_labels.add(c["label"])
            prev = prop_types.get(pn)
            if prev and prev[0] != c["type"]:
                r.err("S020", f"property '{pn}' is {prev[0]} in {prev[1]} but {c['type']} in {tn}: duplicates must share a type")
            prop_types.setdefault(pn, (c["type"], tn))
    rel_names = set()
    for rel in spec.get("relationships", []):
        n = rel.get("name", "")
        if not n:
            r.err("S030", "relationship without a name")
        if n in rel_names:
            r.err("S031", f"duplicate relationship name '{n}' (breaks natural-language queries)")
        rel_names.add(n)
        ends = []
        for side in ("from", "to"):
            ref = str(rel.get(side, ""))
            tn, _, cn = ref.partition(".")
            t = by_table.get(tn)
            c = next((x for x in (t or {}).get("columns", []) if x.get("col") == cn), None)
            if not t or not c:
                r.err("S032", f"relationship '{n}': {side} '{ref}' does not resolve to table.column")
            ends.append((t, c))
        if all(e[0] and e[1] for e in ends):
            if ends[0][1]["type"] != ends[1][1]["type"]:
                r.err("S033", f"relationship '{n}': column types differ ({ends[0][1]['type']} vs {ends[1][1]['type']})")
            if not ends[1][1]["key"]:
                r.warn("S034", f"relationship '{n}': 'to' column is not the key of {ends[1][0]['table']}")
    all_measure_names = [m.get("name") for m in spec.get("measures", [])]
    if len(set(all_measure_names)) != len(all_measure_names):
        r.err("S040", "duplicate measure names")
    sm_cols = {t["entity"]: {c["label"] for c in t["columns"]} for t in tables}
    for m in spec.get("measures", []):
        t = by_table.get(m.get("table"))
        if not t:
            r.err("S041", f"measure '{m.get('name')}': unknown table '{m.get('table')}'")
            continue
        if m.get("name") in sm_cols[t["entity"]]:
            r.err("S042", f"measure '{m['name']}' collides with a column of {t['entity']}")
        if not str(m.get("expr", "")).strip():
            r.err("S043", f"measure '{m['name']}' has no expr")
            continue
        qualified, bare = _dax_refs(m["expr"])
        for tbl, col in qualified:
            if tbl not in sm_cols:
                r.err("S044", f"measure '{m['name']}': unknown table '{tbl}' in DAX")
            elif col not in sm_cols[tbl] and col not in all_measure_names:
                r.err("S045", f"measure '{m['name']}': '{tbl}[{col}]' is not a column label or measure")
        for ref in bare:
            if ref not in all_measure_names:
                r.err("S046", f"measure '{m['name']}': [{ref}] is not a measure")
    props = {f"{t['entity']}.{c['prop']}" for t in tables for c in t["columns"]}
    seen_rules = set()
    for rule in spec.get("rules", []):
        rn = rule.get("name", "")
        if not IDENT_RE.match(rn):
            r.err("S050", f"rule name '{rn}' must be an identifier")
        if rn in seen_rules:
            r.err("S051", f"duplicate rule '{rn}'")
        seen_rules.add(rn)
        if not str(rule.get("statement", "")).strip():
            r.err("S052", f"rule '{rn}' has no statement")
        for e in rule.get("entities", []) or []:
            if e not in by_entity:
                r.err("S053", f"rule '{rn}': unknown entity '{e}'")
        for p in rule.get("properties", []) or []:
            if p not in props:
                r.err("S054", f"rule '{rn}': unknown property '{p}' (use Entity.Property)")
        for rel in rule.get("relationships", []) or []:
            if rel not in rel_names:
                r.err("S055", f"rule '{rn}': unknown relationship '{rel}'")
    r.stats = {"tables": len(tables), "relationships": len(spec.get("relationships", [])),
               "measures": len(spec.get("measures", [])), "rules": len(spec.get("rules", []))}
    return r


# --------------------------------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------------------------------
def make_ctx(**kw: Optional[str]) -> dict:
    ctx = dict(workspace_id="<WORKSPACE_ID>", workspace_name="<WORKSPACE_NAME>", lakehouse_id="<LAKEHOUSE_ID>",
               lakehouse_name="<LAKEHOUSE_NAME>", sql_endpoint="<SQL_ENDPOINT_HOST>", sql_db="<SQL_ENDPOINT_ID>")
    ctx.update({k: v for k, v in kw.items() if v})
    return ctx


def _fmt(c: dict) -> Optional[str]:
    t = TMDL_TYPE[c["type"]]
    if t == "int64":
        return "0"
    if t == "dateTime":
        return "Short Date" if c["type"] == "date" else "General Date"
    if t == "double":
        return "#,0.00"
    return None


def _entity_of(spec: dict, table: str) -> dict:
    return next(t for t in spec["tables"] if t["table"] == table)


def physical_of(t: dict) -> str:
    """The lakehouse table behind a spec table: its `physical` name, else its logical id. TMDL partitions and the graph's Delta paths use
    this and nothing else; relationships, measures and rules keep using logical ids and entity names."""
    return t.get("physical") or t["table"]


def ontology_doc(t: dict) -> str:
    """The description line of an ontology entity: its description with the domain as a tag in front. The semantic-model table writes the bare description."""
    return (f"[{t['domain']}] " if t.get("domain") else "") + t["description"]


def _col_of(t: dict, col: str) -> dict:
    return next(c for c in t["columns"] if c["col"] == col)


def _ont_cols(t: dict) -> List[dict]:
    return [c for c in t["columns"] if c.get("ontology", True)]


def build_semantic_model(spec: dict, ctx: dict) -> Dict[str, str]:
    sm, lh = spec["semanticModel"], spec["lakehouse"]
    expr_name = f"DirectLake - {sm['name']}"
    parts: Dict[str, str] = {}
    parts["definition.pbism"] = json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.2", "settings": {"qnaEnabled": True}}, indent=4) + "\n"
    parts["definition/database.tmdl"] = f"database {q(sm['name'])}\n\tcompatibilityLevel: 1702\n\tcompatibilityMode: powerBI\n"
    m = W()
    m.add(0, "model Model")
    m.add(1, f"culture: {sm['culture']}")
    m.add(1, "defaultPowerBIDataSourceVersion: powerBI_V3")
    m.add(1, "discourageImplicitMeasures")
    m.add(0)
    sem_tables = [t for t in spec["tables"] if t.get("semantic", True)]
    for t in sem_tables:
        m.add(0, f"ref table {q(t['entity'])}")
    parts["definition/model.tmdl"] = m.text()
    e = W()
    e.add(0, f"expression {q(expr_name)} =")
    e.add(2, "let")
    e.add(3, f'Source = AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/{ctx["workspace_id"]}/{ctx["lakehouse_id"]}", [HierarchicalNavigation=true])')
    e.add(2, "in")
    e.add(3, "Source")
    parts["definition/expressions.tmdl"] = e.text()
    for t in sem_tables:
        w = W()
        w.doc(0, t["description"])
        w.add(0, f"table {q(t['entity'])}")
        for ms in [x for x in spec["measures"] if x["table"] == t["table"]]:
            w.add(0)
            w.doc(1, ms.get("desc", ""))
            w.add(1, f"measure {q(ms['name'])} = {ms['expr']}")
            w.add(2, f"formatString: {ms.get('format', '#,0')}")
            if ms.get("folder"):
                w.add(2, f"displayFolder: {ms['folder']}")
        for c in t["columns"]:
            w.add(0)
            w.doc(1, ("[PII] " if c["pii"] else "") + c["desc"])
            w.add(1, f"column {q(c['label'])}")
            w.add(2, f"dataType: {TMDL_TYPE[c['type']]}")
            fmt = _fmt(c)
            if fmt:
                w.add(2, f"formatString: {fmt}")
            if c["hidden"]:
                w.add(2, "isHidden")
            if c["default_label"]:
                w.add(2, "isDefaultLabel")
            w.add(2, "summarizeBy: none")
            w.add(2, f"sourceColumn: {c['col']}")
        w.add(0)
        w.add(1, f"partition {q(t['entity'])} = entity")
        w.add(2, "mode: directLake")
        w.add(2, "source")
        w.add(3, f"entityName: {physical_of(t)}")
        if lh.get("schema"):
            w.add(3, f"schemaName: {lh['schema']}")
        w.add(3, f"expressionSource: {q(expr_name)}")
        parts[f"definition/tables/{t['entity']}.tmdl"] = w.text()
    r = W()
    for rel in spec["relationships"]:
        if not rel.get("semantic", True):
            continue
        ft, fc = rel["from"].split(".")
        tt, tc = rel["to"].split(".")
        f_t, t_t = _entity_of(spec, ft), _entity_of(spec, tt)
        r.add(0, f"relationship rel_{snake(rel['name'])}")             # no /// here: a TOM relationship has no description property (Fabric rejects it)
        r.add(1, f"fromColumn: {q(f_t['entity'])}.{q(_col_of(f_t, fc)['label'])}")
        r.add(1, f"toColumn: {q(t_t['entity'])}.{q(_col_of(t_t, tc)['label'])}")
        r.add(0)
    parts["definition/relationships.tmdl"] = r.text()
    return parts


OWNED_PARTS = {".platform", "database.tmdl", "expressions.tmdl", "model.tmdl", "relationships.tmdl", "entityRelationships.tmdl"}
OWNED_PREFIXES = ("namespaces/", "entities/", "tables/", "rules/")
OWNED_REF_KINDS = {"table", "entity", "namespace", "rule"}


@dataclass
class Seed:
    expressions_text: Optional[str] = None
    expression_source: Optional[str] = None
    include_schema: Optional[bool] = None
    annotations: List[str] = field(default_factory=list)
    # Portal-authored content this kit does not generate (for example imported metrics). Kept verbatim on --replace so a
    # redeploy never destroys what the portal wrote that we do not understand.
    extra_parts: Dict[str, str] = field(default_factory=dict)
    extra_refs: List[str] = field(default_factory=list)


def load_seed(folder: str) -> Seed:
    parts = load_dir_parts(folder)
    seed = Seed(expressions_text=parts.get("expressions.tmdl"))
    for path, text in sorted(parts.items()):
        if path not in OWNED_PARTS and not path.startswith(OWNED_PREFIXES):
            seed.extra_parts[path] = text
    for ln in parts.get("model.tmdl", "").splitlines():
        m = re.match(r"^ref\s+(\w+)\s+\S", ln)
        if m and m.group(1) not in OWNED_REF_KINDS:
            seed.extra_refs.append(ln.strip())
    for path in sorted(parts):
        if not path.startswith("tables/"):
            continue
        in_part = False
        found = False
        schema = False
        for ln in parts[path].splitlines():
            if re.match(r"^\tpartition\s", ln):
                in_part = True
                continue
            if not in_part:
                continue
            if ln.strip() and not ln.startswith("\t\t"):
                break
            s = ln.strip()
            if s.startswith("expressionSource:"):
                seed.expression_source = s.split(":", 1)[1].strip()
                found = True
            elif s.startswith("schemaName:"):
                schema = True
            elif s.startswith("annotation ONT_"):
                seed.annotations.append(s)
        if found:
            seed.include_schema = schema
            break
    return seed


def ont_annotation_lines(ctx: dict) -> List[str]:
    """Partition annotations the portal writes (see skills-for-fabric issue #107). Verified live: with these eight, every entity is graph-eligible.
    ONT_PinnedAtUtc is part of the definition, so the caller passes the same ctx['pinned_at'] on every run (a moving stamp would make every re-run replace the ontology)."""
    pinned = ctx.get("pinned_at") or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pairs = [("ONT_WorkspaceId", ctx["workspace_id"]), ("ONT_ItemId", ctx["lakehouse_id"]), ("ONT_ItemKind", "Lakehouse"),
             ("ONT_ItemName", ctx["lakehouse_name"]), ("ONT_WorkspaceName", ctx["workspace_name"]),
             ("ONT_SqlEndpoint", ctx["sql_endpoint"]), ("ONT_SqlDatabase", ctx["lakehouse_name"]), ("ONT_PinnedAtUtc", pinned)]
    return [f"annotation {k} = {v}" for k, v in pairs]


def build_ontology(spec: dict, ctx: dict, seed: Optional[Seed] = None, ont_annotations: bool = False, namespaces: bool = False) -> Dict[str, str]:
    """Gen-2 ontology parts. namespaces=True puts each entity type in its domain namespace using the `entity ns#Name` form
    that the official WindFarm sample shows; relationships and rules reference such entities by the same qualified name
    (inferred from that pattern, not shown in the sample: deploy the smoke tier first)."""
    name = spec["ontology"]["name"]
    lh = spec["lakehouse"]

    def tag(kind: str, n: str) -> str:
        return lineage_tag(spec, kind, n)

    ent_ns: Dict[str, Optional[str]] = {t["entity"]: (t.get("namespace") if namespaces else None) for t in spec["tables"] if t.get("ontology", True)}

    def qual(entity: str) -> str:
        ns = ent_ns.get(entity)
        return f"{ns}#{q(entity)}" if ns else q(entity)

    parts: Dict[str, str] = {}
    parts[".platform"] = json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "Ontology", "displayName": name},
        "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"}}, indent=2) + "\n"
    parts["database.tmdl"] = "database\n\tcompatibilityLevel: 1000000\n"
    seeded = bool(seed and seed.expression_source)
    if seed and seed.expressions_text:
        parts["expressions.tmdl"] = seed.expressions_text if seed.expressions_text.endswith("\n") else seed.expressions_text + "\n"
    else:
        e = W()
        e.add(0, "expression DatabaseQuery =")
        e.add(2, "let")
        e.add(3, f'database = Sql.Database("{ctx["sql_endpoint"]}", "{ctx["sql_db"]}")')
        e.add(2, "in")
        e.add(3, "database")
        e.add(1, f"lineageTag: {tag('expression', 'DatabaseQuery')}")
        parts["expressions.tmdl"] = e.text()
    expr_source = seed.expression_source if seeded else "DatabaseQuery"
    include_schema = seed.include_schema if seeded else True                    # the official WindFarm sample names the schema on every table
    annotations = list(seed.annotations) if seeded else (ont_annotation_lines(ctx) if ont_annotations else [])
    parts["namespaces/default.tmdl"] = "namespace default\n\tlineageTag: default\n"
    used_ns = sorted({ns for ns in ent_ns.values() if ns})
    for ns in used_ns:
        parts[f"namespaces/{ns}.tmdl"] = f"namespace {ns}\n\tlineageTag: {tag('namespace', ns)}\n"
    ont_tables = [t for t in spec["tables"] if t.get("ontology", True)]
    for t in ont_tables:
        en = t["entity"]
        cols = [c for c in t["columns"] if c.get("ontology", True)]
        w = W()
        w.add(0, f"table {q(en)}")
        w.add(1, f"lineageTag: {tag('table', en)}")
        for c in cols:
            w.add(0)
            w.add(1, f"column {q(c['prop'])}")
            w.add(2, f"dataType: {TMDL_TYPE[c['type']]}")
            fmt = _fmt(c)
            if fmt and TMDL_TYPE[c["type"]] != "double":
                w.add(2, f"formatString: {fmt}")
            w.add(2, f"lineageTag: {tag('column', en + '.' + c['prop'])}")
            w.add(2, f"sourceColumn: {c['col']}")
        w.add(0)
        w.add(1, f"partition {q(en)} = entity")
        w.add(2, "mode: directLake")
        w.add(2, "source")
        w.add(3, f"entityName: {physical_of(t)}")
        if include_schema:
            w.add(3, f"schemaName: {lh.get('schema') or 'dbo'}")                  # the SQL endpoint of a schema-less lakehouse exposes dbo
        w.add(3, f"expressionSource: {expr_source}")
        for a in annotations:
            w.add(0)
            w.add(2, a)
        parts[f"tables/{en}.tmdl"] = w.text()
        x = W()
        x.doc(0, ontology_doc(t))
        x.add(0, f"entity {qual(en)}")
        x.add(1, f"lineageTag: {tag('entity', en)}")
        x.add(1, f"backingTable: {q(en)}")
        x.add(1, f"keyProperty: {q(next(c for c in cols if c['key'])['prop'])}")
        for c in cols:
            x.add(0)
            x.doc(1, ("[PII] " if c["pii"] else "") + c["desc"])
            x.add(1, f"property {q(c['prop'])}")
            x.add(2, f"dataType: {TMDL_TYPE[c['type']]}")
            x.add(2, f"lineageTag: {tag('property', en + '.' + c['prop'])}")
            x.add(0)
            x.add(2, "backingConfiguration")
            x.add(3, f"valueColumn: {q(en)}.{q(c['prop'])}")
        parts[f"entities/{ent_ns[en] + '#' if ent_ns.get(en) else ''}{en}.tmdl"] = x.text()
    rel_w, er_w = W(), W()
    for rel in spec["relationships"]:
        if not rel.get("ontology", True):
            continue
        ft, fc = rel["from"].split(".")
        tt, tc = rel["to"].split(".")
        f_t, t_t = _entity_of(spec, ft), _entity_of(spec, tt)
        rname = f"rel_{snake(rel['name'])}"
        rel_w.add(0, f"relationship {rname}")
        rel_w.add(1, f"fromColumn: {q(f_t['entity'])}.{q(_col_of(f_t, fc)['prop'])}")
        rel_w.add(1, f"toColumn: {q(t_t['entity'])}.{q(_col_of(t_t, tc)['prop'])}")
        rel_w.add(0)
        er_w.add(0, f"entityRelationship {q(rel['name'])}")
        er_w.add(1, f"lineageTag: {tag('entityRelationship', rel['name'])}")
        er_w.add(1, f"fromEntity: {qual(f_t['entity'])}")
        er_w.add(1, f"toEntity: {qual(t_t['entity'])}")
        er_w.add(0)
        er_w.add(1, "backingConfiguration")
        er_w.add(2, f"relationship: {rname}")
        er_w.add(0)
    parts["relationships.tmdl"] = rel_w.text()
    parts["entityRelationships.tmdl"] = er_w.text()
    for rule in spec["rules"]:
        w = W()
        w.add(0, f"rule {q(rule['name'])}")
        w.add(1, f"statement: {rule['statement']}")
        w.add(1, f"lineageTag: {tag('rule', rule['name'])}")
        grouped: Dict[str, List[str]] = {}
        for e_name in rule.get("entities", []) or []:
            grouped.setdefault(e_name, [])
        for ref in rule.get("properties", []) or []:
            e_name, p_name = ref.split(".", 1)
            grouped.setdefault(e_name, []).append(p_name)
        for e_name, props in grouped.items():
            w.add(0)
            w.add(1, f"ruleReferencedEntity {qual(e_name)}")
            if props:
                w.add(2, "propertyScope: specific")
                for p_name in props:
                    w.add(0)
                    w.add(2, f"ruleReferencedProperty {q(p_name)}")
        for rel_name in rule.get("relationships", []) or []:
            w.add(0)
            w.add(1, f"ruleReferencedRelationship {q(rel_name)}")
        parts[f"rules/{rule['name']}.tmdl"] = w.text()
    mw = W()
    mw.add(0, "model Model")
    mw.add(0)
    for t in ont_tables:
        mw.add(0, f"ref table {q(t['entity'])}")
    mw.add(0)
    for t in ont_tables:
        mw.add(0, f"ref entity {qual(t['entity'])}")
    mw.add(0)
    mw.add(0, "ref namespace default")
    for ns in used_ns:
        mw.add(0, f"ref namespace {ns}")
    if spec["rules"]:
        mw.add(0)
        for rule in spec["rules"]:
            mw.add(0, f"ref rule {q(rule['name'])}")
    if seed and seed.extra_refs:
        mw.add(0)
        for ref in seed.extra_refs:
            mw.add(0, ref)
    parts["model.tmdl"] = mw.text()
    if seed:
        for path, text in seed.extra_parts.items():
            parts.setdefault(path, text if text.endswith("\n") else text + "\n")
    return parts


def build_brief(spec: dict) -> str:
    out = [f"# {spec['ontology']['name']} - grounding brief (generated from the spec)", "",
           spec["ontology"].get("description", ""), "",
           "Use these names exactly. Never speak or write properties marked [PII] in full.", "", "## Entities", ""]
    for t in [x for x in spec["tables"] if x.get("ontology", True)]:
        cols = [c for c in t["columns"] if c.get("ontology", True)]
        key = next(c for c in cols if c["key"])
        out.append(f"### {t['entity']} (key: {key['prop']})")
        out.append(t["description"])
        if t["synonyms"]:
            out.append(f"Synonyms: {', '.join(t['synonyms'])}")
        out.append("Properties: " + ", ".join(f"{c['prop']}{' [PII]' if c['pii'] else ''}" for c in cols))
        rels = [r for r in spec["relationships"] if r.get("ontology", True) and
                (r["from"].startswith(t["table"] + ".") or r["to"].startswith(t["table"] + "."))]
        if rels:
            out.append("Relationships: " + "; ".join(r["name"] for r in rels))
        out.append("")
    out.append("## Metrics (semantic model measures)")
    out.extend(f"- {m['name']}: {m.get('desc', '')}" for m in spec["measures"])
    out += ["", "## Business rules (advisory context, not enforced by the ontology)"]
    out.extend(f"- {r['name']}: {r['statement']}" for r in spec["rules"])
    return "\n".join(out).rstrip() + "\n"


def build_graph(spec: dict, ctx: dict) -> Dict[str, str]:
    """Definition of the GraphModel Fabric creates next to the ontology (what the portal's Configure graph wizard writes): a node type per
    ontology entity and an edge type per ontology relationship, backed by the lakehouse Delta tables. Aliases are the ontology lineage tags.
    Apply with updateDefinition?updateMetadata=false on the graph model; the service then refreshes the graph."""
    root = f"abfss://{ctx['workspace_id']}@onelake.pbidedicated.windows.net/{ctx['lakehouse_id']}/Tables"        # plain lakehouse: no schema folder
    node_types, node_tables, sources, edge_types, edge_tables = [], [], [], [], []
    for t in [x for x in spec["tables"] if x.get("ontology", True)]:
        en, cols = t["entity"], _ont_cols(t)
        alias = lineage_tag(spec, "entity", en)
        node_types.append({"primaryKeyProperties": [next(c for c in cols if c["key"])["prop"]], "alias": alias, "labels": [en],
                           "properties": [{"name": c["prop"], "type": GRAPH_TYPE[c["type"]]} for c in cols]})
        sources.append({"name": en, "type": "DeltaTable", "properties": {"path": f"{root}/{physical_of(t)}"}})
        node_tables.append({"nodeTypeAlias": alias, "id": alias, "dataSourceName": en,
                            "propertyMappings": [{"propertyName": c["prop"], "sourceColumn": c["col"]} for c in cols]})
    for rel in [x for x in spec["relationships"] if x.get("ontology", True)]:
        (ft, fc), (tt, _) = rel["from"].split("."), rel["to"].split(".")
        f_t, t_t = _entity_of(spec, ft), _entity_of(spec, tt)
        alias = lineage_tag(spec, "entityRelationship", rel["name"])
        edge_types.append({"additionalKeyProperties": [], "alias": alias, "sourceNodeType": {"alias": lineage_tag(spec, "entity", f_t["entity"])},
                           "labels": [rel["name"].replace(" ", "_")],
                           "destinationNodeType": {"alias": lineage_tag(spec, "entity", t_t["entity"])}, "properties": []})
        edge_tables.append({"edgeTypeAlias": alias, "id": alias, "edgeIdMapping": [], "dataSourceName": f_t["entity"],
                            "sourceNodeKeyColumns": [next(c["col"] for c in _ont_cols(f_t) if c["key"])], "propertyMappings": [],
                            "destinationNodeKeyColumns": [fc]})            # the row's own key is the edge source, its foreign key the destination
    bodies = {"graphType": {"nodeTypes": node_types, "edgeTypes": edge_types}, "dataSources": {"dataSources": sources},
              "graphDefinition": {"nodeTables": node_tables, "edgeTables": edge_tables},
              "stylingConfiguration": {"modelLayout": None, "visualFormat": None},
              "graphSettings": {"scalingConfiguration": {"sku": {"tier": "small"}}}}
    return {f"{n}.json": json.dumps({"$schema": GRAPH_SCHEMA.format(n), **b}, indent=2) + "\n" for n, b in bodies.items()}


def validate_graph(parts: Dict[str, str], spec: dict, strict: bool = False) -> Report:
    """Static checks on build_graph output against the spec: valid JSON, unique aliases, every reference resolves, exactly one node table and
    data source per node type, mappings and key columns exist in the spec's table. strict: unresolved <PLACEHOLDER> values are errors."""
    rep = Report()
    docs: Dict[str, dict] = {}
    for name in GRAPH_PARTS:
        if name not in parts:
            rep.err("G001", f"{name} is missing")
    for path, text in sorted(parts.items()):
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
        if not isinstance(doc, dict):
            rep.err("G002", f"{path} is not a valid JSON object")
            continue
        docs[path] = doc
        for ph in sorted(set(PLACEHOLDER_RE.findall(text))):
            (rep.err if strict else rep.warn)("G003", f"{path}: unresolved placeholder {ph}")
    gt, ds, gd = (docs.get(p) for p in ("graphType.json", "dataSources.json", "graphDefinition.json"))
    if gt is None or ds is None or gd is None:
        return rep
    node_types, edge_types = gt.get("nodeTypes") or [], gt.get("edgeTypes") or []
    node_tables, edge_tables = gd.get("nodeTables") or [], gd.get("edgeTables") or []
    sources = ds.get("dataSources") or []
    tables = {t["entity"]: t for t in spec["tables"] if t.get("ontology", True)}
    n_sources = collections.Counter(s.get("name") for s in sources)

    def label(x: dict) -> str:
        return (x.get("labels") or ["?"])[0]

    for name, k in n_sources.items():
        if k > 1:
            rep.err("G030", f"duplicate data source '{name}'")
        if name not in tables:
            rep.err("G031", f"data source '{name}' is not an ontology entity of the spec")
    for s in sources:
        t = tables.get(s.get("name"))
        path = str((s.get("properties") or {}).get("path") or "")
        if t is not None and path.rsplit("/", 1)[-1] != physical_of(t):
            rep.err("G032", f"data source '{s.get('name')}': path '{path}' does not end in the lakehouse table '{physical_of(t)}'")
    nodes: Dict[str, dict] = {}
    for n in node_types:
        a, props = n.get("alias"), {p.get("name"): p.get("type") for p in n.get("properties") or []}
        if a in nodes:
            rep.err("G010", f"duplicate node type alias {a} ('{label(n)}')")
        nodes[a] = n
        if not n.get("primaryKeyProperties"):
            rep.err("G011", f"node type '{label(n)}' has no primary key property")
        for pk in n.get("primaryKeyProperties") or []:
            if pk not in props:
                rep.err("G011", f"node type '{label(n)}': primary key '{pk}' is not one of its properties")
        for pn, pt in props.items():
            if pt not in GRAPH_TYPE.values():
                rep.err("G012", f"node type '{label(n)}': property '{pn}' has unsupported type '{pt}'")
    edges: Dict[str, dict] = {}
    for e in edge_types:
        a = e.get("alias")
        if a in edges or a in nodes:
            rep.err("G020", f"duplicate alias {a} (edge type '{label(e)}')")
        edges[a] = e
        for side in ("sourceNodeType", "destinationNodeType"):
            ref = (e.get(side) or {}).get("alias")
            if ref not in nodes:
                rep.err("G021", f"edge type '{label(e)}': {side} alias {ref} is not a node type")
    per_node = collections.Counter(t.get("nodeTypeAlias") for t in node_tables)
    for a, n in nodes.items():
        if per_node[a] != 1:
            rep.err("G040", f"node type '{label(n)}' has {per_node[a]} node tables (expected exactly 1)")
    for t in node_tables:
        n, src = nodes.get(t.get("nodeTypeAlias")), t.get("dataSourceName")
        if n is None:
            rep.err("G041", f"node table {t.get('id')} points at unknown node type alias {t.get('nodeTypeAlias')}")
            continue
        if n_sources[src] != 1:
            rep.err("G042", f"node type '{label(n)}': {n_sources[src]} data sources named '{src}' (expected exactly 1)")
        cols = {c["col"] for c in _ont_cols(tables[src])} if src in tables else None
        props = {p.get("name") for p in n.get("properties") or []}
        maps = t.get("propertyMappings") or []
        for m in maps:
            if m.get("propertyName") not in props:
                rep.err("G043", f"node type '{label(n)}': mapping of '{m.get('propertyName')}', which is not one of its properties")
            if cols is not None and m.get("sourceColumn") not in cols:
                rep.err("G044", f"node type '{label(n)}': source column '{m.get('sourceColumn')}' is not an ontology column of table '{tables[src]['table']}'")
        mapped = collections.Counter(m.get("propertyName") for m in maps)
        for p in sorted(props, key=str):
            if mapped[p] != 1:
                rep.err("G045", f"node type '{label(n)}': property '{p}' has {mapped[p]} mappings (expected exactly 1)")
    per_edge = collections.Counter(t.get("edgeTypeAlias") for t in edge_tables)
    for a, e in edges.items():
        if per_edge[a] != 1:
            rep.err("G050", f"edge type '{label(e)}' has {per_edge[a]} edge tables (expected exactly 1)")
    for t in edge_tables:
        e, src = edges.get(t.get("edgeTypeAlias")), t.get("dataSourceName")
        if e is None:
            rep.err("G051", f"edge table {t.get('id')} points at unknown edge type alias {t.get('edgeTypeAlias')}")
            continue
        if n_sources[src] != 1:
            rep.err("G052", f"edge type '{label(e)}': {n_sources[src]} data sources named '{src}' (expected exactly 1)")
        have = {c["col"] for c in tables[src]["columns"]} if src in tables else None
        for side in ("sourceNodeKeyColumns", "destinationNodeKeyColumns"):
            keys = t.get(side) or []
            if not keys:
                rep.err("G053", f"edge type '{label(e)}': {side} is empty")
            for col in keys:
                if have is not None and col not in have:
                    rep.err("G053", f"edge type '{label(e)}': {side} '{col}' is not a column of table '{tables[src]['table']}'")
    rep.stats = {"parts": len(parts), "nodeTypes": len(node_types), "edgeTypes": len(edge_types), "dataSources": len(sources),
                 "properties": sum(len(n.get("properties") or []) for n in node_types)}
    return rep


def to_payload(parts: Dict[str, str], fmt: Optional[str] = None, skip: Tuple[str, ...] = ()) -> dict:
    d: dict = {"parts": [{"path": p, "payload": b64(parts[p]), "payloadType": "InlineBase64"} for p in sorted(parts) if p not in skip]}
    if fmt:
        d["format"] = fmt
    return d


def write_parts(parts: Dict[str, str], folder: Path) -> None:
    for path, text in parts.items():
        target = folder.joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")


def load_dir_parts(folder: str) -> Dict[str, str]:
    base = Path(folder)
    parts: Dict[str, str] = {}
    for f in sorted(base.rglob("*")):
        if f.is_file():
            parts[f.relative_to(base).as_posix()] = f.read_text(encoding="utf-8-sig")
    return parts


# --------------------------------------------------------------------------------------------------
# TMDL parser + validator
# --------------------------------------------------------------------------------------------------
@dataclass
class Node:
    kind: str
    name: str = ""
    value: str = ""
    indent: int = 0
    line: int = 0
    props: Dict[str, str] = field(default_factory=dict)
    children: List["Node"] = field(default_factory=list)
    raw: List[str] = field(default_factory=list)

    def kids(self, kind: str) -> List["Node"]:
        return [c for c in self.children if c.kind == kind]


PROP_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
OBJ_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\s+(.*))?$")
NAMEVAL_RE = re.compile(r"^(?P<name>'(?:[^']|'')*'|[^\s=]+)\s*(?:=\s*(?P<value>.*))?$")


def unquote(s: str) -> str:
    s = s.strip()
    return s[1:-1].replace("''", "'") if len(s) >= 2 and s[0] == "'" and s[-1] == "'" else s


def parse_tmdl(text: str) -> List[Node]:
    roots: List[Node] = []
    stack: List[Node] = []
    raw_owner: Optional[Node] = None
    in_fence = False
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip("\t"))
        content = line.lstrip("\t")
        if indent == 0 and line.startswith("    "):
            indent = (len(line) - len(line.lstrip(" "))) // 4
            content = line.lstrip(" ")
        content = content.rstrip()
        if content.startswith("///"):
            continue
        if raw_owner is not None:
            if in_fence:
                raw_owner.raw.append(content)
                if content.strip().endswith("```"):
                    in_fence, raw_owner = False, None
                continue
            if indent >= raw_owner.indent + 2:
                raw_owner.raw.append(content)
                continue
            raw_owner = None
        while stack and stack[-1].indent >= indent:
            stack.pop()
        if content in FLAGS and stack:
            stack[-1].props[content] = "true"
            continue
        pm = PROP_RE.match(content)
        if pm and stack:
            stack[-1].props[pm.group(1)] = pm.group(2).strip()
            continue
        om = OBJ_RE.match(content)
        if not om:
            continue
        kind, rest = om.group(1), (om.group(2) or "").strip()
        node = Node(kind=kind, indent=indent, line=i)
        if kind == "ref":
            node.name = rest
        elif rest:
            nv = NAMEVAL_RE.match(rest)
            if nv:
                node.name = unquote(nv.group("name"))
                node.value = (nv.group("value") or "").strip()
                if nv.group("value") is not None and rest.rstrip().endswith("=") and not node.value:
                    node.value = ""
            else:
                node.name = rest
        (stack[-1].children if stack else roots).append(node)
        stack.append(node)
        if kind in ("expression", "measure") and rest.rstrip().endswith("=") and not node.value:
            raw_owner = node
        elif node.value.startswith("```") and not (len(node.value) > 3 and node.value.endswith("```")):
            in_fence, raw_owner = True, node
    return roots


def _split_ref(ref: str) -> Tuple[str, str]:
    """'Table.Column' or "Table.'Col name'" -> (table, column)."""
    m = re.match(r"^(?P<t>'(?:[^']|'')*'|[^.']+)\.(?P<c>.+)$", ref.strip())
    if not m:
        return "", ref.strip()
    return unquote(m.group("t")), unquote(m.group("c"))


def validate_parts(parts: Dict[str, str], strict: bool = False) -> Report:
    rep = Report()
    norm = {(p[len("definition/"):] if p.startswith("definition/") else p): t for p, t in parts.items()}
    is_ont = any(p.startswith("entities/") or p == "entityRelationships.tmdl" for p in norm) or \
        ".platform" in norm and '"Ontology"' in norm[".platform"]
    kind = "ontology" if is_ont else "semantic-model"
    tables: Dict[str, dict] = {}
    rels: Dict[str, dict] = {}
    exprs: set = set()
    entities: Dict[str, dict] = {}
    erels: List[dict] = []
    rules: List[dict] = []
    namespaces: set = set()
    refs: List[Tuple[str, str]] = []
    have_db = have_model = False
    for path, text in sorted(norm.items()):
        if path == ".platform":
            try:
                meta = json.loads(text)
                if is_ont and meta.get("metadata", {}).get("type") != "Ontology":
                    rep.err("V001", ".platform metadata.type must be 'Ontology'")
                # live finding: Fabric answers 400 BadArtifactCreateRequest to any other ontology item name
                if is_ont and not ONT_ITEM_NAME_RE.match(str(meta.get("metadata", {}).get("displayName", ""))):
                    rep.err("V006", ".platform displayName must start with a letter and hold only letters, digits and underscores (under 90 chars): "
                                    "Fabric rejects other ontology item names")
            except ValueError:
                rep.err("V002", ".platform is not valid JSON")
            continue
        if path == "definition.pbism":
            try:
                json.loads(text)
            except ValueError:
                rep.err("V003", "definition.pbism is not valid JSON")
            continue
        if not path.endswith(".tmdl"):
            continue
        if path == "relationships.tmdl" and any(ln.lstrip().startswith("///") for ln in text.splitlines()):
            # live finding: Fabric rejects the import with "Property 'description' is unknown" (a TOM relationship has no description)
            rep.err("V005", f"{path}: a /// description comment is not allowed on a relationship")
        for ph in sorted(set(PLACEHOLDER_RE.findall(text))):
            (rep.err if strict else rep.warn)("V004", f"{path}: unresolved placeholder {ph}")
        for n in parse_tmdl(text):
            if n.kind == "database":
                have_db = True
            elif n.kind == "model":
                have_model = True
                refs += [("ref", c.name) for c in n.kids("ref")]
            elif n.kind == "ref":
                refs.append(("ref", n.name))
            elif n.kind == "table":
                if n.name in tables:
                    rep.err("V010", f"{path}: duplicate table '{n.name}'")
                cols = {c.name: c for c in n.kids("column")}
                tables[n.name] = {"node": n, "cols": cols, "measures": {c.name: c for c in n.kids("measure")}, "file": path}
            elif n.kind == "relationship":
                rels[n.name] = {"from": n.props.get("fromColumn", ""), "to": n.props.get("toColumn", ""), "node": n}
            elif n.kind == "expression":
                exprs.add(n.name)
            elif n.kind == "entity":
                entities[n.name] = {"node": n, "file": path}
            elif n.kind == "entityRelationship":
                erels.append({"name": n.name, "node": n})
            elif n.kind == "rule":
                rules.append({"name": n.name, "node": n, "file": path})
            elif n.kind == "namespace":
                namespaces.add(n.name)
    if not have_db:
        rep.err("V020", "database.tmdl with a 'database' declaration is required")
    if kind == "semantic-model" and not have_model:
        rep.err("V021", "model.tmdl with a 'model' declaration is required")
    if not tables:
        rep.err("V022", "no tables found")
    for tname, t in tables.items():
        parts_ = t["node"].kids("partition")
        if not parts_:
            rep.err("V030", f"table '{tname}' has no partition")
        for p in parts_:
            if p.props.get("mode", "").lower() != "directlake":
                rep.warn("V031", f"table '{tname}': partition mode is '{p.props.get('mode')}', expected directLake")
            src = p.kids("source")
            sp = src[0].props if src else {}
            if not sp.get("entityName"):
                rep.err("V032", f"table '{tname}': partition has no entityName")
            es = unquote(sp.get("expressionSource", ""))
            if es and exprs and es not in exprs:
                rep.err("V033", f"table '{tname}': expressionSource '{es}' is not defined in expressions.tmdl")
        for cname, c in t["cols"].items():
            if "sourceColumn" not in c.props:
                rep.warn("V034", f"{tname}.{cname}: no sourceColumn")
            dtp = c.props.get("dataType", "")
            if dtp == "decimal":
                rep.warn("V035", f"{tname}.{cname}: decimal is returned as null by Fabric Graph; prefer double")
            sc = c.props.get("sourceColumn", "")
            if set(sc) & SPECIAL_COL_CHARS:
                rep.warn("V036", f"{tname}.{cname}: sourceColumn '{sc}' has characters that force delta column mapping")
        if kind == "semantic-model":
            for mname, m in t["measures"].items():
                if "formatString" not in m.props:
                    rep.warn("V037", f"measure '{mname}' has no formatString")
    for rname, r in rels.items():
        for side in ("from", "to"):
            tn, cn = _split_ref(r[side])
            if tn not in tables or cn not in tables[tn]["cols"]:
                rep.err("V040", f"relationship '{rname}': {side} '{r[side]}' does not resolve to an existing table.column")
    for rk, name in refs:
        if name.startswith("table "):
            if unquote(name[6:]) not in tables:
                rep.err("V050", f"model ref '{name}' has no table definition")
        elif name.startswith("entity "):
            en = unquote(name[7:])
            if en not in entities:
                rep.err("V051", f"model ref '{name}' has no entity definition")
        elif name.startswith("rule "):
            if unquote(name[5:]) not in {r["name"] for r in rules}:
                rep.err("V052", f"model ref '{name}' has no rule definition")
        elif name.startswith("namespace "):
            if unquote(name[10:]) not in namespaces and not any(p.startswith("namespaces/") for p in norm):
                rep.warn("V053", f"model ref '{name}' has no namespace file")
    if kind == "ontology":
        _validate_ontology(rep, tables, rels, entities, erels, rules, namespaces, norm)
    rep.stats = {"parts": len(norm), "tables": len(tables), "relationships": len(rels), "entities": len(entities),
                 "entityRelationships": len(erels), "rules": len(rules)}
    rep.stats["kind_is_ontology"] = int(is_ont)
    return rep


def _validate_ontology(rep: Report, tables: dict, rels: dict, entities: dict, erels: List[dict], rules: List[dict],
                       namespaces: set, norm: Dict[str, str]) -> None:
    if "namespaces/default.tmdl" not in norm and "default" not in namespaces:
        rep.warn("V060", "namespaces/default.tmdl is missing (the service synthesizes it on write)")
    prop_types: Dict[str, Tuple[str, str]] = {}
    ent_props: Dict[str, Dict[str, Node]] = {}
    ent_tables: Dict[str, set] = {}
    for ename, e in entities.items():
        n: Node = e["node"]
        bare = ename.split("#", 1)[-1]
        if not NAME_RE.match(bare):
            rep.warn("V061", f"entity '{ename}' exceeds the documented 1-26 char / alphanumeric-hyphen-underscore rule")
        bt = unquote(n.props.get("backingTable", ""))
        if bt not in tables:
            rep.err("V062", f"entity '{ename}': backingTable '{bt}' not found")
        allowed = {bt}
        for ab in n.kids("additionalBackingTable"):
            at = unquote(ab.props.get("table", ""))
            if at not in tables:
                rep.err("V063", f"entity '{ename}': additionalBackingTable '{at}' not found")
            rn = unquote(ab.props.get("relationship", ""))
            if rn not in rels:
                rep.err("V064", f"entity '{ename}': additionalBackingTable relationship '{rn}' not found")
            allowed.add(at)
        ent_tables[ename] = allowed
        props = {p.name: p for p in n.kids("property")}
        ent_props[ename] = props
        key = unquote(n.props.get("keyProperty", ""))
        if not key:
            rep.warn("V065", f"entity '{ename}' has no keyProperty (keyless entities are not graph-eligible)")
        elif key not in props:
            rep.err("V066", f"entity '{ename}': keyProperty '{key}' is not a property")
        elif props[key].props.get("dataType") not in ("string", "int64"):
            rep.err("V067", f"entity '{ename}': key property '{key}' must be string or int64")
        for pname, p in props.items():
            dtp = p.props.get("dataType", "")
            base = re.sub(r"^TimeSeries<(.+)>$", r"\1", dtp)
            if base not in ONT_DATATYPES:
                rep.err("V068", f"{ename}.{pname}: unsupported dataType '{dtp}'")
            prev = prop_types.get(pname)
            if prev and prev[0] != base:
                rep.err("V069", f"property '{pname}' is {prev[0]} in {prev[1]} but {base} in {ename}: duplicates must share a type")
            prop_types.setdefault(pname, (base, ename))
            bc = p.kids("backingConfiguration")
            vc = bc[0].props.get("valueColumn", "") if bc else ""
            if not vc:
                rep.warn("V070", f"{ename}.{pname}: unbound property (no valueColumn)")
                continue
            tn, cn = _split_ref(vc)
            if tn not in allowed:
                rep.err("V071", f"{ename}.{pname}: valueColumn table '{tn}' is not a backing table of the entity")
            elif cn not in tables.get(tn, {}).get("cols", {}):
                rep.err("V072", f"{ename}.{pname}: valueColumn '{vc}' does not exist")
            if bc and bc[0].props.get("type") == "timeSeries":
                oc = bc[0].props.get("orderingColumn", "")
                ot, ocn = _split_ref(oc)
                if ot not in tables or ocn not in tables[ot]["cols"]:
                    rep.err("V073", f"{ename}.{pname}: orderingColumn '{oc}' does not exist")
    seen_names: Dict[str, int] = {}
    for er in erels:
        n = er["node"]
        seen_names[er["name"]] = seen_names.get(er["name"], 0) + 1
        fe, te = unquote(n.props.get("fromEntity", "")), unquote(n.props.get("toEntity", ""))
        for side, en in (("fromEntity", fe), ("toEntity", te)):
            if en not in entities:
                rep.err("V080", f"entityRelationship '{er['name']}': {side} '{en}' not found")
        bc = n.kids("backingConfiguration")
        rn = unquote(bc[0].props.get("relationship", "")) if bc else ""
        if not rn:
            rep.warn("V081", f"entityRelationship '{er['name']}' has no backing relationship (unbound)")
        elif rn not in rels:
            rep.err("V082", f"entityRelationship '{er['name']}': backing relationship '{rn}' not found")
        elif fe in ent_tables and te in ent_tables:
            ft, _ = _split_ref(rels[rn]["from"])
            tt, _ = _split_ref(rels[rn]["to"])
            if ft not in ent_tables[fe] or tt not in ent_tables[te]:
                rep.err("V083", f"entityRelationship '{er['name']}': relationship '{rn}' links {ft}->{tt}, not the backing tables of {fe}->{te}")
    for name, cnt in seen_names.items():
        if cnt > 1:
            rep.err("V084", f"duplicate entityRelationship name '{name}' (breaks natural-language queries)")
    rel_names = set(seen_names)
    for r in rules:
        n = r["node"]
        if not n.props.get("statement", "").strip():
            rep.err("V090", f"rule '{r['name']}' has no statement")
        for re_ in n.kids("ruleReferencedEntity"):
            if re_.name not in entities:
                rep.err("V091", f"rule '{r['name']}': entity '{re_.name}' not found")
                continue
            for rp in re_.kids("ruleReferencedProperty"):
                if rp.name not in ent_props.get(re_.name, {}):
                    rep.err("V092", f"rule '{r['name']}': property '{re_.name}.{rp.name}' not found")
        for rr in n.kids("ruleReferencedRelationship"):
            if rr.name not in rel_names:
                rep.err("V093", f"rule '{r['name']}': relationship '{rr.name}' not found")


# --------------------------------------------------------------------------------------------------
# REST client (Fabric items API, Power BI refresh) with LRO + throttling
# --------------------------------------------------------------------------------------------------
RETRY_STATUS = (429, 502, 503, 504)           # Fabric asks the caller to come back later: throttled, or a gateway or server that is busy
MAX_RETRY_AFTER_S = 120.0                     # a longer Retry-After is honoured for 2 minutes only: the next try then reports the failure instead of the run sleeping on


def retry_after_seconds(value: object, now: Callable[[], float] = time.time) -> Optional[float]:
    """What a Retry-After header asks for, in seconds: a number of seconds or an HTTP date. None when it is absent, empty or unreadable."""
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        seconds = (when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)).timestamp() - now()
    return max(seconds, 0.0) if math.isfinite(seconds) else None


def backoff_delay(attempt: int, retry_after: Optional[float] = None, *, base: float = 2.0, cap: float = 60.0,
                  rng: Callable[[], float] = random.random) -> float:
    """Seconds to wait before the next try; `attempt` is 0 after the first failure. With a Retry-After the wait is never shorter than it asked for
    (at most MAX_RETRY_AFTER_S) plus up to a quarter more, so callers that were all told the same thing do not come back together. Without one it grows
    exponentially, base * 2**attempt up to `cap`, and half of it is random ("equal jitter"): 1-2 s, 2-4 s, 4-8 s, ..."""
    if retry_after is not None and retry_after > 0:
        asked = min(retry_after, MAX_RETRY_AFTER_S)
        return asked + rng() * 0.25 * asked
    ceiling = min(cap, base * 2 ** max(attempt, 0))
    return ceiling / 2 + rng() * ceiling / 2


class FabricError(RuntimeError):
    """An error from Fabric (or OneLake). `status` is the HTTP status of the reply that caused it, when there was one, and `retry_after` the seconds
    its Retry-After header asked for (None: none)."""

    def __init__(self, message: object = "", status: Optional[int] = None, retry_after: Optional[float] = None):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after


class OperationFailed(FabricError):
    """The request was accepted, but its long-running operation ended Failed or Cancelled."""


class Fabric:
    def __init__(self, token_fn: Callable[[str], str], api_base: str = FABRIC_API, pbi_base: str = PBI_API,
                 log: Callable[[str], None] = print, sleeper: Optional[Callable[[float], None]] = None, max_wait: int = 900,
                 rng: Callable[[], float] = random.random, clock: Callable[[], float] = time.monotonic):
        self.token_fn, self.api, self.pbi, self.log, self.max_wait = token_fn, api_base.rstrip("/"), pbi_base.rstrip("/"), log, max_wait
        self.sleep = sleeper or (lambda s: time.sleep(s))
        self.rng = rng
        self.clock = clock                                                            # monotonic time for retry deadlines; a test passes a virtual one
        self._limit = threading.local()

    @contextlib.contextmanager
    def retry_until(self, at: Optional[float]):
        """Within the block the calls of this thread stop retrying once waiting would run past `at` (a time of self.clock()): the failure that was about to
        be waited out is raised instead. Nothing else changes: a call that succeeds, or is slow, is never cut short. The earlier of nested limits holds."""
        before = getattr(self._limit, "at", None)
        self._limit.at = before if at is None else at if before is None else min(before, at)
        try:
            yield
        finally:
            self._limit.at = before

    def out_of_time(self, wait: float) -> bool:
        """True when waiting `wait` more seconds would run past the limit set by retry_until for this thread."""
        at = getattr(self._limit, "at", None)
        return at is not None and self.clock() + wait > at

    def call(self, method: str, url: str, scope: str = FABRIC_SCOPE, ok: Tuple[int, ...] = (200, 201, 202, 204), **kw):
        import requests
        for attempt in range(6):
            headers = {"Authorization": f"Bearer {self.token_fn(scope)}", "Content-Type": "application/json", "x-ms-client-request-id": str(uuid.uuid4())}
            try:
                resp = requests.request(method, url, headers=headers, timeout=120, **kw)
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as ex:     # the same ladder as a 503, for every method
                wait = backoff_delay(attempt, None, rng=self.rng)
                if attempt >= 5 or self.out_of_time(wait):
                    raise                                                                                          # the last error as it was, text and all
                self.log(f"  {type(ex).__name__} from {method} {url.split('?')[0]}: {str(ex)[:120]}; retrying in {wait:.0f}s")
                self.sleep(wait)
                continue
            if resp.status_code in RETRY_STATUS and attempt < 5:
                wait = backoff_delay(attempt, retry_after_seconds(resp.headers.get("Retry-After")), rng=self.rng)
                if not self.out_of_time(wait):
                    self.log(f"  {resp.status_code} from {method} {url.split('?')[0]}; retrying in {wait:.0f}s")
                    self.sleep(wait)
                    continue
            if resp.status_code not in ok:
                raise FabricError(f"{method} {url} -> {resp.status_code}: {resp.text[:600]}", status=resp.status_code,
                                  retry_after=retry_after_seconds(resp.headers.get("Retry-After")))
            return resp
        raise FabricError(f"{method} {url}: too many retries")

    @staticmethod
    def _json(resp) -> Optional[dict]:
        try:
            return resp.json() if resp.content else None
        except ValueError:
            return None

    def lro(self, resp, scope: str = FABRIC_SCOPE) -> Optional[dict]:
        if resp.status_code != 202:
            return self._json(resp)
        loc = resp.headers.get("Location")
        if not loc:
            raise FabricError("202 without a Location header")
        deadline = time.time() + self.max_wait
        asked = retry_after_seconds(resp.headers.get("Retry-After"))
        wait = min(5.0 if asked is None else asked, 5)               # Retry-After is advisory (often 30 s); the LRO quota is 500 calls/min
        while True:
            self.sleep(min(max(wait, 1), 5))
            state = self._json(self.call("GET", loc, scope)) or {}
            status = state.get("status")
            if status == "Succeeded":
                break
            if status in ("Failed", "Cancelled"):
                raise OperationFailed(f"operation {status}: {json.dumps(state.get('error', state))[:600]}")
            if time.time() > deadline:
                raise FabricError("operation timed out")
        res = self.call("GET", loc.rstrip("/") + "/result", scope, ok=(200, 204, 400, 404))
        if res.status_code == 200:
            return self._json(res)
        if res.status_code == 400 and "OperationHasNoResult" not in res.text:      # an operation can succeed without producing a result (Load Table, ...)
            raise FabricError(f"GET {loc.rstrip('/')}/result -> 400: {res.text[:600]}")
        return None

    def list_items(self, ws: str, collection: str) -> List[dict]:
        items, token = [], None
        while True:
            url = f"{self.api}/workspaces/{ws}/{collection}" + (f"?continuationToken={urllib.parse.quote(token, safe='')}" if token else "")
            body = self._json(self.call("GET", url)) or {}
            items += body.get("value", [])
            token = body.get("continuationToken")
            if not token:
                return items

    def find(self, ws: str, collection: str, display_name: str) -> Optional[dict]:
        return next((i for i in self.list_items(ws, collection) if i.get("displayName") == display_name), None)

    def resolve_lakehouse(self, ws: str, name: str) -> dict:
        lh = self.find(ws, "lakehouses", name)
        if not lh:
            raise FabricError(f"lakehouse '{name}' not found in workspace {ws}")
        detail = self._json(self.call("GET", f"{self.api}/workspaces/{ws}/lakehouses/{lh['id']}")) or {}
        sql = (detail.get("properties") or {}).get("sqlEndpointProperties") or {}
        wsd = self._json(self.call("GET", f"{self.api}/workspaces/{ws}")) or {}
        return {"lakehouse_id": lh["id"], "lakehouse_name": name, "sql_endpoint": sql.get("connectionString"),
                "sql_db": sql.get("id"), "workspace_name": wsd.get("displayName")}

    def create(self, ws: str, collection: str, body: dict) -> str:
        resp = self.call("POST", f"{self.api}/workspaces/{ws}/{collection}", json=body)
        result = self.lro(resp) or {}
        item_id = result.get("id")
        if not item_id:
            found = self.find(ws, collection, body["displayName"])
            item_id = found["id"] if found else None
        if not item_id:
            raise FabricError(f"created {body['displayName']} but could not resolve its id")
        return item_id

    def get_definition(self, ws: str, collection: str, item_id: str, fmt: Optional[str] = None) -> Dict[str, str]:
        url = f"{self.api}/workspaces/{ws}/{collection}/{item_id}/getDefinition" + (f"?format={fmt}" if fmt else "")
        result = self.lro(self.call("POST", url, json={})) or {}
        return {p["path"]: base64.b64decode(p["payload"]).decode("utf-8-sig") for p in result.get("definition", {}).get("parts", [])}

    def update_definition(self, ws: str, collection: str, item_id: str, definition: dict, update_metadata: bool = False) -> None:
        url = f"{self.api}/workspaces/{ws}/{collection}/{item_id}/updateDefinition" + ("?updateMetadata=true" if update_metadata else "?updateMetadata=false")
        self.lro(self.call("POST", url, json={"definition": definition}))

    def refresh_model(self, ws: str, model_id: str) -> None:
        self.call("POST", f"{self.pbi}/groups/{ws}/datasets/{model_id}/refreshes", PBI_SCOPE, json={"notifyOption": "NoNotification"})


def default_token_fn(az_runner: Optional[Callable[[List[str]], Tuple[int, str, str]]] = None,
                     clock: Callable[[], float] = time.time) -> Callable[[str], str]:
    """Access tokens for Fabric, Power BI, OneLake and Search.

    Default: the signed-in Azure CLI context (`az account get-access-token`), so the host's existing `az login` is reused and
    a managed identity that happens to be attached to a VM can never silently take over. This also keeps the Python
    environment small (no azure-identity / msal / cryptography). CONTOSO_ONTO_AUTH=default switches to DefaultAzureCredential
    (managed identity, service principal environment variables, ...; needs `pip install azure-identity`).
    FABRIC_TOKEN / PBI_TOKEN environment variables override the two Fabric audiences (handy for tests and one-off runs)."""
    static = {FABRIC_SCOPE: os.environ.get("FABRIC_TOKEN"), PBI_SCOPE: os.environ.get("PBI_TOKEN")}
    cache: Dict[str, Tuple[str, float]] = {}
    cred = None

    def run_az(args: List[str]) -> Tuple[int, str, str]:
        if az_runner is not None:
            return az_runner(args)
        import shutil
        import subprocess
        exe = shutil.which("az") or shutil.which("az.cmd")
        if not exe:
            return 127, "", "az CLI not found on PATH"
        p = subprocess.run([exe, *args, "--only-show-errors"], capture_output=True, text=True, timeout=120)
        return p.returncode, p.stdout, p.stderr

    def get(scope: str) -> str:
        nonlocal cred
        if static.get(scope):
            return static[scope]
        if os.environ.get("CONTOSO_ONTO_AUTH", "cli").lower() == "default":
            if cred is None:
                try:
                    from azure.identity import DefaultAzureCredential
                except ImportError:
                    raise RuntimeError("CONTOSO_ONTO_AUTH=default needs azure-identity: pip install -r requirements-optional.txt") from None
                cred = DefaultAzureCredential()
            return cred.get_token(scope).token
        hit = cache.get(scope)
        if hit and hit[1] - clock() > 300:
            return hit[0]
        resource = scope[:-len("/.default")] if scope.endswith("/.default") else scope
        rc, out, err = run_az(["account", "get-access-token", "--resource", resource, "-o", "json"])
        if rc != 0:
            raise RuntimeError(f"az could not issue a token for {resource}: {err.strip()[:300] or 'no output'}. "
                               "Sign in on this host yourself (az login); this kit never signs in or out.")
        body = json.loads(out)
        exp = body.get("expires_on")
        if exp is None:
            try:
                exp = dt.datetime.strptime(str(body.get("expiresOn", ""))[:19], "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                exp = clock() + 1800
        cache[scope] = (body["accessToken"], float(exp))
        return body["accessToken"]

    return get


# --------------------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------------------
def _prepare(args, ctx: dict):
    spec = load_spec(args.spec)
    srep = check_spec(spec)
    srep.show("spec")
    if not srep.ok:
        raise SystemExit(2)
    seed = load_seed(args.seed) if getattr(args, "seed", None) else None
    sm = build_semantic_model(spec, ctx)
    ont = build_ontology(spec, ctx, seed, getattr(args, "ont_annotations", False))
    return spec, sm, ont


def cmd_build(args) -> int:
    spec = load_spec(args.spec)
    ctx = make_ctx(workspace_id=args.workspace_id, lakehouse_id=args.lakehouse_id, workspace_name=args.workspace_name,
                   sql_endpoint=args.sql_endpoint, sql_db=args.sql_db,
                   lakehouse_name=args.lakehouse_name or spec["lakehouse"]["name"])
    spec_, sm, ont = _prepare(args, ctx)
    out = Path(args.out)
    for folder in ("semantic_model", "ontology"):
        if (out / folder).exists():
            shutil.rmtree(out / folder)
    write_parts(sm, out / "semantic_model")
    write_parts(ont, out / "ontology")
    pay = out / "payloads"
    pay.mkdir(parents=True, exist_ok=True)
    (pay / "semantic_model.create.json").write_text(json.dumps({
        "displayName": spec_["semanticModel"]["name"],
        "definition": to_payload(sm, "TMDL")}, indent=2), encoding="utf-8")
    (pay / "ontology.create.json").write_text(json.dumps({
        "displayName": spec_["ontology"]["name"], "description": spec_["ontology"].get("description", "")[:256],
        "definition": to_payload(ont)}, indent=2), encoding="utf-8")
    grounding = out / "grounding"
    grounding.mkdir(parents=True, exist_ok=True)
    (grounding / "agent_brief.md").write_text(build_brief(spec_), encoding="utf-8")
    ok = True
    for title, parts in (("semantic model", sm), ("ontology", ont)):
        rep = validate_parts(parts)
        rep.show(title)
        ok &= rep.ok
    print(f"built into {out.resolve()}")
    return 0 if ok else 1


def cmd_validate(args) -> int:
    rep = validate_parts(load_dir_parts(args.folder), strict=args.strict)
    rep.show(args.folder)
    return 0 if rep.ok else 1


def cmd_export(args) -> int:
    fab = Fabric(default_token_fn(), args.api_base or FABRIC_API)
    collection = {"ontology": "ontologies", "semanticModel": "semanticModels"}[args.item]
    item = {"id": args.id} if args.id else fab.find(args.workspace_id, collection, args.name)
    if not item:
        raise SystemExit(f"{args.item} '{args.name}' not found")
    parts = fab.get_definition(args.workspace_id, collection, item["id"], "TMDL" if args.item == "semanticModel" else None)
    write_parts(parts, Path(args.out))
    rep = validate_parts(parts)
    rep.show("exported definition")
    print(f"exported {len(parts)} parts to {Path(args.out).resolve()}")
    return 0


def cmd_apply(args) -> int:
    spec = load_spec(args.spec)
    ctx = make_ctx(workspace_id=args.workspace_id, lakehouse_id=args.lakehouse_id, workspace_name=args.workspace_name,
                   sql_endpoint=args.sql_endpoint, sql_db=args.sql_db, lakehouse_name=spec["lakehouse"]["name"])
    fab: Optional[Fabric] = None
    if args.execute:
        if not args.workspace_id:
            raise SystemExit("--workspace-id is required with --execute")
        fab = Fabric(default_token_fn(), args.api_base or FABRIC_API, args.pbi_base or PBI_API)
        if not args.lakehouse_id or not args.sql_endpoint:
            ctx.update({k: v for k, v in fab.resolve_lakehouse(args.workspace_id, spec["lakehouse"]["name"]).items() if v})
    spec, sm, ont = _prepare(args, ctx)
    for title, parts in (("semantic model", sm), ("ontology", ont)):
        rep = validate_parts(parts, strict=args.execute)
        rep.show(title)
        if not rep.ok:
            return 1
    sm_def = to_payload(sm, "TMDL")
    ont_def = to_payload(ont)
    print("\nPLAN")
    print(f"  1. semantic model '{spec['semanticModel']['name']}'  parts={len(sm_def['parts'])}  POST /workspaces/<ws>/semanticModels")
    print(f"  2. refresh (frame) Direct Lake model: {'yes' if args.refresh else 'no (use --refresh)'}")
    print(f"  3. ontology '{spec['ontology']['name']}'  parts={len(ont_def['parts'])}  POST /workspaces/<ws>/ontologies")
    print("  4. portal checks: Manage graph eligibility, 'Test the ontology', then `export` the golden definition")
    if not args.execute:
        print("\nDRY RUN - nothing was sent. Re-run with --execute --workspace-id <guid> to apply.")
        return 0
    assert fab is not None
    ws = args.workspace_id
    state: Dict[str, str] = {"workspaceId": ws, "lakehouseId": ctx["lakehouse_id"]}
    sm_item = fab.find(ws, "semanticModels", spec["semanticModel"]["name"])
    if not sm_item:
        sm_id = fab.create(ws, "semanticModels", {"displayName": spec["semanticModel"]["name"], "definition": sm_def})
        print(f"  created semantic model {sm_id}")
    else:
        sm_id = sm_item["id"]
        if args.replace:
            fab.update_definition(ws, "semanticModels", sm_id, to_payload(sm, "TMDL"))
            print(f"  replaced semantic model definition {sm_id}")
        else:
            print(f"  semantic model exists ({sm_id}); skipped (use --replace)")
    state["semanticModelId"] = sm_id
    if args.refresh:
        fab.refresh_model(ws, sm_id)
        print("  refresh requested")
    ont_item = fab.find(ws, "ontologies", spec["ontology"]["name"])
    if not ont_item:
        ont_id = fab.create(ws, "ontologies", {"displayName": spec["ontology"]["name"],
                                               "description": spec["ontology"].get("description", "")[:256], "definition": ont_def})
        print(f"  created ontology {ont_id}")
    else:
        ont_id = ont_item["id"]
        if args.replace:
            backup = Path(args.out) / "backup" / dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            write_parts(fab.get_definition(ws, "ontologies", ont_id), backup)
            print(f"  backed up live ontology definition to {backup}")
            fab.update_definition(ws, "ontologies", ont_id, ont_def)
            print(f"  replaced ontology definition {ont_id}")
        else:
            print(f"  ontology exists ({ont_id}); skipped (use --replace; portal-authored content is overwritten)")
    state["ontologyId"] = ont_id
    Path(args.out).mkdir(parents=True, exist_ok=True)
    (Path(args.out) / ".state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(json.dumps(state, indent=2))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, build_like=True):
        p.add_argument("--spec", default=str(Path(__file__).parent / "spec" / "bank_poc.yaml"))
        p.add_argument("--out", default=str(Path(__file__).parent / "out"))
        p.add_argument("--seed", help="folder with a portal-seeded ontology export (expressions + partition annotations)")
        p.add_argument("--ont-annotations", action="store_true", help="emit the undocumented ONT_* partition annotations (experimental)")
        p.add_argument("--workspace-id")
        p.add_argument("--workspace-name")
        p.add_argument("--lakehouse-id")
        p.add_argument("--sql-endpoint")
        p.add_argument("--sql-db")

    b = sub.add_parser("build")
    common(b)
    b.add_argument("--lakehouse-name")
    b.set_defaults(fn=cmd_build)
    v = sub.add_parser("validate")
    v.add_argument("folder")
    v.add_argument("--strict", action="store_true", help="treat unresolved <PLACEHOLDER> values as errors")
    v.set_defaults(fn=cmd_validate)
    a = sub.add_parser("apply")
    common(a)
    a.add_argument("--execute", action="store_true", help="actually call the Fabric REST API (default is a dry run)")
    a.add_argument("--replace", action="store_true", help="overwrite existing item definitions (ontology is backed up first)")
    a.add_argument("--refresh", action="store_true", help="refresh the Direct Lake semantic model after creation")
    a.add_argument("--api-base")
    a.add_argument("--pbi-base")
    a.set_defaults(fn=cmd_apply)
    e = sub.add_parser("export")
    e.add_argument("--item", choices=["ontology", "semanticModel"], required=True)
    e.add_argument("--workspace-id", required=True)
    e.add_argument("--name")
    e.add_argument("--id")
    e.add_argument("--out", required=True)
    e.add_argument("--api-base")
    e.set_defaults(fn=cmd_export)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except FabricError as ex:
        print(f"FABRIC ERROR: {ex}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
