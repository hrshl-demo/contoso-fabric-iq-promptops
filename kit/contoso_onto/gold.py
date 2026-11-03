"""Silver tables (+ a few curated reference sources) -> typed, ontology-ready "gold" star schema, driven by model/gold/*.yaml.

One declarative model drives everything: the same table/column/relationship definitions produce the Parquet/Delta gold
layer here, the Direct Lake semantic model + ontology TMDL (spec.py), the data dictionary and the agent grounding.

Gold is built FROM SILVER: a table's `sources` are `{silver: <silver table>}` (optionally `where:`, `set:` and a `join:` that brings
parent columns to a child table) and its columns name silver columns. Reference tables (laws, authority matrix, requirements, bank
agents, service request types, journeys and their links, the generated calendar) keep reading their curated docs/config sources; the
raw readers are also what the engine's own unit tests exercise. Gold keeps the release-level facts silver deliberately does not carry:
context constants (`set:`), legacy-id namespacing (`owner`, `superseded_by`), duplicate-key precedence by source order and
`allow_duplicates`. Every table has a logical id (`table`, the v1 name that relationships, measures, rules and oracles use) and a
physical lakehouse name (`physical`, one registry: `physical_name()`), which only output boundaries use.

Design rules enforced here (each one is a known Fabric IQ ontology constraint):
  * entity keys are string / int, no decimals (Graph returns null for decimal) - money is int64 paise + double rupees
  * physical column names are snake_case ASCII (no column mapping)
  * dates are DATE, instants are UTC timestamps (+ a derived IST date), so "this month" questions are unambiguous
  * every table is flat; nested arrays become child tables, scalar arrays become delimited text or bridge tables
"""
from __future__ import annotations

import datetime as dt
import functools
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import pyarrow as pa
import yaml

from .snapshot import load_json

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ARROW = {"string": pa.string(), "int": pa.int64(), "double": pa.float64(), "bool": pa.bool_(),
         "date": pa.date32(), "datetime": pa.timestamp("us", tz="UTC")}
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
SNAKE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
TPL_RE = re.compile(r"\{([^{}:]+)(?::([^{}]*))?\}")
DEFAULT_REF_PREFIXES = {
    "member-": "household_member", "agent-": "agent", "mandate-": "mandate", "doc-": "document", "case-": "incident",
    "acc-": "account", "card-": "card", "loan-": "loan", "goal-": "goal", "txn-": "transaction", "dep-": "deposit",
    "employer-": "employer", "external-": "external_party", "consent-": "consent", "policy-": "insurance_policy",
    "pol-": "insurance_policy", "inv-": "investment", "obl-": "obligation", "home-": "property", "gap-": "protection_gap",
}


class GoldError(RuntimeError):
    pass


# the star-schema kinds and the lakehouse name prefix each one carries (design: dim_*, fact_*, bridge_*, ref_*)
KINDS = ("dimension", "fact", "bridge", "reference")
KIND_PREFIX = {"dimension": "dim_", "fact": "fact_", "bridge": "bridge_", "reference": "ref_"}
MODEL_DIR = Path(__file__).resolve().parent.parent / "model"


# --------------------------------------------------------------------------------------------------
# model loading
# --------------------------------------------------------------------------------------------------
def load_model(model_dir: Path) -> dict:
    """Merge model/*.yaml and model/gold/*.yaml into one dict (tables are concatenated, other keys are merged). The silver table specs
    (model/silver/*.yaml) ride along under `silver_tables`, read-only: the registry names the silver tables and bronze files behind each
    gold table from them. The silver builder still loads its own spec with silver.load_silver_spec."""
    model: dict[str, Any] = {"tables": [], "relationships": [], "measures": [], "rules": [], "golden": []}
    files = sorted((model_dir / "gold").glob("*.yaml")) + sorted(model_dir.glob("*.yaml"))
    for f in files:
        part = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for k, v in part.items():
            if k in ("tables", "relationships", "measures", "rules", "golden"):
                model[k].extend(v or [])
            elif isinstance(v, dict):
                model.setdefault(k, {}).update(v)
            else:
                model[k] = v
    model["silver_tables"] = [t for f in sorted((model_dir / "silver").glob("*.yaml"))
                              for t in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("silver") or []]
    model.setdefault("name", "ContosoBank")
    return model


def physical_names(model: dict) -> dict[str, str]:
    """logical id -> lakehouse table name: the one registry, the `physical` field of every gold table spec (default: the logical id)."""
    return {t["table"]: t.get("physical") or t["table"] for t in model["tables"]}


@functools.lru_cache(maxsize=1)
def _committed_registry() -> dict[str, str]:
    return physical_names(load_model(MODEL_DIR))


def physical_name(logical: str, model: Optional[dict] = None) -> str:
    """The lakehouse name of a gold table, from its logical id (`account` -> `dim_account`). Only output boundaries use it: Parquet file
    names, the manifest, TMDL partitions, the graph's Delta paths, the data dictionary and the load stage. `model` defaults to the
    committed model/ folder."""
    registry = physical_names(model) if model is not None else _committed_registry()
    try:
        return registry[logical]
    except KeyError:
        raise GoldError(f"unknown gold table {logical!r}") from None


def logical_name(physical: str, model: Optional[dict] = None) -> str:
    """The inverse lookup, for code that starts from a lakehouse table (a Parquet file, a Delta table the load stage listed)."""
    registry = physical_names(model) if model is not None else _committed_registry()
    for logical, phys in registry.items():
        if phys == physical:
            return logical
    raise GoldError(f"no gold table is called {physical!r} in the lakehouse")


def expand_columns(table: dict) -> list[dict]:
    """Expand column macros (money / rate / instant) into concrete columns. A table that reads silver (`silver:` list) takes its money
    from int64 minor units and its percentages from canonical decimal text; a raw reference table keeps the v1 transforms."""
    from_silver = bool(table.get("silver"))
    t_inr, t_paise = ("minor_to_inr", "minor_to_paise") if from_silver else ("inr", "paise")
    out: list[dict] = []
    for c in table.get("columns", []):
        c = dict(c)
        if "money" in c:
            base = c.pop("money")
            src = c.pop("from")
            desc = c.pop("desc", "")
            nullable = c.pop("nullable", True)
            ont_paise = c.pop("paise_in_ontology", False)
            label = c.pop("label", None)
            out.append({**c, "col": f"{base}_inr", "type": "double", "from": src, "t": t_inr, "nullable": nullable,
                        "desc": f"{desc} In rupees (INR).".strip(), **({"label": f"{label} (INR)"} if label else {})})
            out.append({**c, "col": f"{base}_paise", "type": "int", "from": src, "t": t_paise, "nullable": nullable,
                        "desc": f"{desc} Exact integer paise (1 INR = 100 paise); use for reconciliation only.".strip(),
                        "hidden": True, "ontology": ont_paise,
                        **({"label": f"{label} (paise)"} if label else {})})
        elif "rate_bps" in c:
            base = c.pop("rate_bps")
            src = c.pop("from")
            desc = c.pop("desc", "")
            nullable = c.pop("nullable", True)
            unit = c.pop("unit", "bps")          # what the source column holds: basis points, or the silver percent as decimal text
            if unit not in ("bps", "pct_text"):
                raise GoldError(f"{table.get('table')}.{base}: rate unit must be bps or pct_text")
            t_pct, t_bps = ("dec_to_float", "dec_to_bps") if unit == "pct_text" else ("pct_from_bps", "int")
            c.pop("label", None)
            out.append({**c, "col": f"{base}_pct", "type": "double", "from": src, "t": t_pct, "nullable": nullable,
                        "desc": f"{desc} As a percentage, for example 8.5 means 8.5%."})
            out.append({**c, "col": f"{base}_bps", "type": "int", "from": src, "t": t_bps, "nullable": nullable,
                        "desc": f"{desc} In basis points (100 bps = 1%).", "hidden": True, "ontology": False})
        elif "rate_pct" in c:
            base = c.pop("rate_pct")
            src = c.pop("from")
            desc = c.pop("desc", "")
            nullable = c.pop("nullable", True)
            c.pop("label", None)
            out.append({**c, "col": f"{base}_pct", "type": "double", "from": src, "t": "dec_to_float" if from_silver else "float",
                        "nullable": nullable, "desc": f"{desc} As a percentage, for example 7.75 means 7.75%."})
        elif "instant" in c:
            base = c.pop("instant")
            src = c.pop("from")
            desc = c.pop("desc", "")
            nullable = c.pop("nullable", True)
            date_col = c.pop("date_col", None)
            label = c.pop("label", None)
            out.append({**c, "col": base, "type": "datetime", "from": src, "t": "ts", "nullable": nullable,
                        "desc": f"{desc} Instant in UTC.".strip(), **({"label": label} if label else {})})
            if date_col:
                out.append({**c, "col": date_col, "type": "date", "from": src, "t": "ist_date", "nullable": nullable,
                            "desc": f"{desc} Calendar date in India time (IST), for 'today', 'this month' questions.".strip(),
                            "key": False, "default_label": False})
        else:
            out.append(c)
    for c in out:
        c.setdefault("type", "string")
        c.setdefault("nullable", not c.get("key", False))
    return out


# --------------------------------------------------------------------------------------------------
# row access, templates, transforms
# --------------------------------------------------------------------------------------------------
@dataclass
class Row:
    node: Any
    parent: Optional["Row"] = None
    ctx: dict = field(default_factory=dict)

    def get(self, path: str) -> Any:
        if path.startswith("$"):
            return self.ctx.get(path[1:])
        cur: Row = self
        while path.startswith("^"):
            if cur.parent is None:
                return None
            cur = cur.parent
            path = path[1:]
            if path.startswith("."):
                path = path[1:]
        if path in ("", "@"):
            return cur.node
        v = cur.node
        for seg in path.split("."):
            if v is None:
                return None
            if isinstance(v, dict):
                v = v.get(seg)
            elif isinstance(v, list) and seg.isdigit():
                i = int(seg)
                v = v[i] if i < len(v) else None
            else:
                return None
        return v


def _is_money(v: Any) -> bool:
    return isinstance(v, dict) and "amountMinor" in v


def _parse_ts(v: Any) -> Optional[dt.datetime]:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=IST)
    s = str(v)
    if not TS_RE.match(s):
        raise GoldError(f"not an RFC 3339 timestamp: {s!r}")
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _t_inr(v):
    if v is None:
        return None
    if not _is_money(v):
        raise GoldError(f"expected money object, got {v!r}")
    return int(v["amountMinor"]) / 100.0


def _t_paise(v):
    if v is None:
        return None
    if not _is_money(v):
        raise GoldError(f"expected money object, got {v!r}")
    return int(v["amountMinor"])


def _minor(v: Any, what: str) -> Optional[int]:
    """An int64 minor-unit amount as silver holds it; a bool, float or v1 money object here is a spec mistake, never a value."""
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise GoldError(f"{what}: expected integer minor units, got {v!r}")
    return v


def _t_minor_to_inr(v):
    """Silver paise -> rupees as a double: minor / 100.0, exactly the division v1 did on the seed's amountMinor."""
    m = _minor(v, "minor_to_inr")
    return None if m is None else m / 100.0


def _t_minor_to_paise(v):
    return _minor(v, "minor_to_paise")


def _decimal(v: Any, what: str) -> Optional[Decimal]:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (str, int, Decimal)):
        raise GoldError(f"{what}: expected canonical decimal text, got {v!r}")
    try:
        return v if isinstance(v, Decimal) else Decimal(str(v).strip())
    except InvalidOperation:
        raise GoldError(f"{what}: not a decimal: {v!r}") from None


def _t_dec_to_float(v):
    """Silver's canonical decimal text (or whole number) -> double. float() of the text is the correctly rounded double of that
    decimal, which is what v1 read from the seed number, so the result is bit for bit the v1 value."""
    d = _decimal(v, "dec_to_float")
    return None if d is None else float(d)


def _t_dec_to_bps(v):
    """A percent as canonical decimal text -> whole basis points (100 bps = 1 percent); a fractional basis point is an error."""
    d = _decimal(v, "dec_to_bps")
    if d is None:
        return None
    bps = d * 100
    if bps != bps.to_integral_value():
        raise GoldError(f"dec_to_bps: {v!r} percent is not a whole number of basis points")
    return int(bps)


def _t_join(v):
    if v is None:
        return None
    if not isinstance(v, list):
        return str(v)
    return "; ".join(str(x) for x in v if x is not None) or None


def _t_json(v):
    return None if v in (None, [], {}) else json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _t_facts_text(v):
    """Readable 'key: value; ...' text; money objects become 'INR 12,500.00' so paise never leak into prose."""
    if v is None:
        return None
    parts: list[str] = []

    def rec(prefix: str, node: Any) -> None:
        if _is_money(node):
            parts.append(f"{prefix}: INR {int(node['amountMinor']) / 100:,.2f}")
        elif isinstance(node, dict):
            for k in sorted(node):
                rec(f"{prefix}.{k}" if prefix else k, node[k])
        elif isinstance(node, list):
            items = [json.dumps(x, sort_keys=True, separators=(",", ":")) if isinstance(x, (dict, list)) else str(x) for x in node]
            parts.append(f"{prefix}: " + ", ".join(items))
        elif node is not None:
            parts.append(f"{prefix}: {node}")

    rec("", v)
    return "; ".join(parts) or None


TRANSFORMS: dict[str, Callable[[Any], Any]] = {
    "str": lambda v: None if v is None else str(v),
    "int": lambda v: None if v is None else int(v),
    "float": lambda v: None if v is None else float(v),
    "bool": lambda v: None if v is None else bool(v),
    "date": lambda v: v,
    "ts": lambda v: None if v is None else _parse_ts(v).astimezone(dt.timezone.utc),
    "ist_date": lambda v: None if v is None else _parse_ts(v).astimezone(IST).date(),
    "inr": _t_inr,
    "paise": _t_paise,
    "minor_to_inr": _t_minor_to_inr,
    "minor_to_paise": _t_minor_to_paise,
    "dec_to_float": _t_dec_to_float,
    "dec_to_bps": _t_dec_to_bps,
    "pct_from_bps": lambda v: None if v is None else int(v) / 100.0,
    "bps_from_pct": lambda v: None if v is None else int(round(float(v) * 100)),
    "join": _t_join,
    "len": lambda v: None if v is None else len(v),
    "json": _t_json,
    "facts_text": _t_facts_text,
    "first": lambda v: (v[0] if v else None) if isinstance(v, list) else v,
    "lower": lambda v: None if v is None else str(v).lower(),
    "upper": lambda v: None if v is None else str(v).upper(),
}


def _perm_class(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("no", "not exposed"):
        return "no"
    return "yes" if s.startswith("yes") else "qualified"


def apply_transform(spec: Optional[str], v: Any, prefixes: dict[str, str], row: Optional[Row] = None) -> Any:
    if not spec:
        return v
    ctx = row.ctx if row else {}
    for part in [p for p in spec.split("|") if p]:
        name, _, arg = part.partition(":")
        if name == "prefix":
            v = v if isinstance(v, str) and v.startswith(tuple(arg.split(","))) else None
        elif name == "ref_type":
            v = next((t for p, t in prefixes.items() if isinstance(v, str) and v.startswith(p)), "other" if v else None)
        elif name == "bool_not_null":
            v = v is not None
        elif name == "re":
            m = re.search(arg, v) if isinstance(v, str) else None
            v = (m.group(1) or None) if m and m.groups() else None
        elif name == "owner":
            # same ownerId in two seed releases = two different data generations of one person: namespace the legacy one
            if isinstance(v, str) and v in ctx.get("collisions", ()) and ctx.get("release") != ctx.get("canonical_release"):
                v = f"{v}~{ctx.get('release_short', ctx.get('release'))}"
        elif name == "superseded_by":
            v = v if isinstance(v, str) and v in ctx.get("collisions", ()) and ctx.get("release") != ctx.get("canonical_release") else None
        elif name == "perm_class":
            v = _perm_class(v)
        elif name == "num_or_null":
            v = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        elif name in TRANSFORMS:
            v = TRANSFORMS[name](v)
        else:
            raise GoldError(f"unknown transform '{name}'")
    return v


def render_tpl(tpl: str, row: Row) -> Optional[str]:
    missing = False

    def sub(m: re.Match) -> str:
        nonlocal missing
        val = row.get(m.group(1))
        if val is None:
            missing = True
            return ""
        return format(val, m.group(2)) if m.group(2) else str(val)

    out = TPL_RE.sub(sub, tpl)
    return None if missing else out


def coerce(v: Any, typ: str, where: str) -> Any:
    if v is None:
        return None
    try:
        if typ == "string":
            if isinstance(v, bool):
                return "true" if v else "false"
            if isinstance(v, (dict, list)):
                raise GoldError("object/array in string column")
            return str(v)
        if typ == "int":
            if isinstance(v, bool):
                raise GoldError("bool in int column")
            if isinstance(v, float):
                if not v.is_integer():
                    raise GoldError(f"non-integer {v}")
                return int(v)
            return int(v)
        if typ == "double":
            if isinstance(v, bool):
                raise GoldError("bool in double column")
            return float(v)
        if typ == "bool":
            if not isinstance(v, bool):
                raise GoldError(f"not a bool: {v!r}")
            return v
        if typ == "date":
            if isinstance(v, dt.datetime):
                return v.date()
            if isinstance(v, dt.date):
                return v
            return dt.date.fromisoformat(str(v))
        if typ == "datetime":
            if isinstance(v, dt.datetime):
                return v.astimezone(dt.timezone.utc)
            return _parse_ts(v).astimezone(dt.timezone.utc)
    except (ValueError, TypeError, GoldError) as ex:
        raise GoldError(f"{where}: cannot coerce {v!r} to {typ} ({ex})") from None
    raise GoldError(f"{where}: unknown type {typ}")


# --------------------------------------------------------------------------------------------------
# markdown table + custom sources
# --------------------------------------------------------------------------------------------------
def _clean_cell(s: str) -> str:
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = s.replace("**", "").replace("`", "").replace("<br>", " ").strip()
    return re.sub(r"\s+", " ", s)


def md_table(text: str, heading: str) -> list[dict[str, str]]:
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if re.match(r"^#{1,6}\s+", ln) and heading.lower() in ln.lower()), None)
    if start is None:
        raise GoldError(f"heading containing {heading!r} not found")
    i = start + 1
    while i < len(lines) and not lines[i].lstrip().startswith("|"):
        if re.match(r"^#{1,6}\s+", lines[i]):
            raise GoldError(f"no table directly under heading {heading!r}")
        i += 1
    rows = []
    while i < len(lines) and lines[i].lstrip().startswith("|"):
        cells = [_clean_cell(c) for c in lines[i].strip().strip("|").split("|")]
        rows.append(cells)
        i += 1
    if len(rows) < 3:
        raise GoldError(f"table under {heading!r} too short")
    header = rows[0]
    return [dict(zip(header, r)) for r in rows[2:] if len(r) == len(header)]


def _custom_monthly_metric(data: Any) -> Iterable[dict]:
    months = data["months"]
    for s in data["series"]:
        for i, m in enumerate(months):
            yield {"buId": s["buId"], "month": m, **{k: v[i] for k, v in s.items() if isinstance(v, list)}}


def _custom_calendar(start: str, end: str) -> Iterable[dict]:
    d, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    while d <= last:
        fy = d.year if d.month >= 4 else d.year - 1
        yield {"date": d, "year": d.year, "month": d.month, "monthName": d.strftime("%B"), "yearMonth": d.strftime("%Y-%m"),
               "quarter": f"Q{(d.month - 1) // 3 + 1}", "weekday": d.strftime("%A"), "isWeekend": d.weekday() >= 5,
               "fiscalYear": f"FY{fy}-{str(fy + 1)[-2:]}"}
        d += dt.timedelta(days=1)


def _custom_preference_unpivot(repo: "Repo", src: dict) -> Iterable[dict]:
    """Frontier stores preferences as one wide record, retail4 as field/value rows: unpivot the wide one."""
    for r in repo.json(f"data/releases/{src['release']}/preferences.json"):
        for f in ("contactChannel", "debtPreference", "emergencyReserveMonths", "humanReviewFor"):
            v = r.get(f)
            if v is None:
                continue
            yield {"preferenceId": f"{r['preferenceId']}#{f}", "ownerId": r["ownerId"], "field": f,
                   "value": "; ".join(v) if isinstance(v, list) else str(v), "recordedOn": None, "statedIn": None,
                   "releaseId": r["releaseId"]}


def _flatten_scalars(node: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _flatten_scalars(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(node, list):
        yield prefix, "; ".join(str(x) for x in node)
    else:
        yield prefix, node


def _custom_product_parameter(repo: "Repo", src: dict) -> Iterable[dict]:
    for rel in src["files"]:
        for p in repo.json(rel):
            for name, v in _flatten_scalars(p.get("parameters") or {}):
                if v is None or name in ("displayName", "family"):
                    continue
                yield {"productId": p["productId"], "name": name, "value": v}


def _custom_authority_matrix(repo: "Repo", src: dict) -> Iterable[dict]:
    rows = md_table(repo.text(src["doc"]), src["heading"])
    for r in rows:
        cap = r.get("Capability")
        for actor, text in r.items():
            if actor != "Capability":
                yield {"capability": cap, "actor": actor, "text": text}


def _custom_frontier_journeys(repo: "Repo", src: dict) -> Iterable[dict]:
    text = repo.text(src["doc"])
    titles: dict[str, str] = {}
    for r in md_table(text, src["crosswalk_heading"]):
        m = re.match(r"^(F\d\d)\s*(.*)$", r.get("Journey", ""))
        if m and m.group(2):
            titles.setdefault(m.group(1), m.group(2))
    for r in md_table(text, src["behavior_heading"]):
        jid = r.get("Journey", "").strip()
        if re.fullmatch(r"F\d\d", jid):
            yield {"journeyId": jid, "title": titles.get(jid), "primarySpecialists": r.get("Primary specialists"),
                   "autonomyCeiling": int(r["Autonomy ceiling"]) if r.get("Autonomy ceiling", "").isdigit() else None,
                   "allowedOperations": r.get("Allowed prepared operations")}


CUSTOM_SOURCES: dict[str, Callable[["Repo", dict], Iterable[dict]]] = {
    "monthly_metric": lambda repo, src: _custom_monthly_metric(repo.json("data/releases/retail4-v1/bank/monthly-metrics.json")),
    "calendar": lambda repo, src: _custom_calendar(src["start"], src["end"]),
    "preference_unpivot": _custom_preference_unpivot,
    "product_parameter": _custom_product_parameter,
    "authority_matrix": _custom_authority_matrix,
    "frontier_journeys": _custom_frontier_journeys,
}


# --------------------------------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------------------------------
class Repo:
    def __init__(self, root: Path, canonical_release: str = "retail-frontier-v1", release_short: Optional[dict] = None):
        self.root = Path(root)
        self.canonical_release = canonical_release
        self.release_short = release_short or {}
        self._cache: dict[Path, Any] = {}
        self._text: dict[Path, str] = {}
        self._collisions: Optional[set[str]] = None

    def json(self, rel: str) -> Any:
        p = self.root / rel
        if p not in self._cache:
            self._cache[p] = load_json(p)
        return self._cache[p]

    def text(self, rel: str) -> str:
        p = self.root / rel
        if p not in self._text:
            self._text[p] = p.read_text(encoding="utf-8-sig")
        return self._text[p]

    def glob(self, pattern: str) -> list[str]:
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.glob(pattern))

    def releases(self) -> list[str]:
        return sorted(p.name for p in (self.root / "data" / "releases").iterdir() if (p / "release.json").exists())

    def collisions(self) -> set[str]:
        """ownerIds that appear as a persona in more than one release."""
        if self._collisions is None:
            seen: dict[str, int] = {}
            for rel in self.releases():
                f = self.root / "data" / "releases" / rel / "personas.json"
                if f.exists():
                    for oid in {r["ownerId"] for r in self.json(f"data/releases/{rel}/personas.json")}:
                        seen[oid] = seen.get(oid, 0) + 1
            self._collisions = {k for k, n in seen.items() if n > 1}
        return self._collisions

    def release_meta(self, release: str) -> dict:
        return self.json(f"data/releases/{release}/release.json")


def _select(data: Any, src: dict) -> list[Any]:
    if src.get("list"):
        data = data[src["list"]]
    if isinstance(data, dict) and src.get("keyed"):
        return [dict(v, _key=k) for k, v in data.items()]
    if isinstance(data, dict) and src.get("single"):
        return [data]
    if not isinstance(data, list):
        raise GoldError(f"source {src} did not yield a list")
    return data


def _filter_rows(rows: list[Row], src: dict) -> list[Row]:
    """The source's row filters, shared by raw and silver sources: where {path: value}, require <path(s)>, absent <path(s)>."""
    for path, want in (src.get("where") or {}).items():
        rows = [r for r in rows if r.get(path) == want]
    for path in ([src["require"]] if isinstance(src.get("require"), str) else src.get("require") or []):
        rows = [r for r in rows if r.get(path) is not None]
    for path in ([src["absent"]] if isinstance(src.get("absent"), str) else src.get("absent") or []):
        rows = [r for r in rows if r.get(path) is None]
    return rows


def source_rows(src: dict, repo: Repo) -> list[Row]:
    base_ctx: dict[str, Any] = {"collisions": repo.collisions(), "canonical_release": repo.canonical_release}
    if "custom" in src:
        base_ctx.update(release=src.get("release"), file=src["custom"])
        if src.get("release"):
            base_ctx.update(business_date=repo.release_meta(src["release"]).get("businessDate"),
                            release_short=repo.release_short.get(src["release"], src["release"]))
        if src["custom"] not in CUSTOM_SOURCES:
            raise GoldError(f"unknown custom source {src['custom']}")
        nodes = list(CUSTOM_SOURCES[src["custom"]](repo, src))
    elif "release" in src:
        rel = f"data/releases/{src['release']}/{src['file']}"
        meta = repo.release_meta(src["release"])
        base_ctx.update(release=src["release"], file=src["file"], business_date=meta.get("businessDate"),
                        release_short=repo.release_short.get(src["release"], src["release"]))
        nodes = _select(repo.json(rel), src)
    elif "data" in src:
        base_ctx.update(release=None, file=src["data"])
        nodes = _select(repo.json(f"data/{src['data']}"), src)
    elif "data_glob" in src:
        nodes = []
        base_ctx.update(release=None, file=src["data_glob"])
        for f in repo.glob("data/" + src["data_glob"]):
            nodes.extend(_select(repo.json(f), {**src, "single": src.get("single", True)}))
    elif "doc" in src:
        base_ctx.update(release=None, file=src["doc"])
        nodes = md_table(repo.text(src["doc"]), src["heading"])
    else:
        raise GoldError(f"source needs release/data/data_glob/doc/custom/silver: {src}")
    rows = [Row(n, None, {**base_ctx, **(src.get("set") or {}), "index": i + 1}) for i, n in enumerate(nodes)]
    if src.get("explode"):
        exploded: list[Row] = []
        for parent in rows:
            kids = parent.get(src["explode"]) or []
            if not isinstance(kids, list):
                raise GoldError(f"explode path {src['explode']} is not a list")
            for j, k in enumerate(kids):
                exploded.append(Row(k, parent, {**parent.ctx, "index": j + 1}))
        rows = exploded
    return _filter_rows(rows, src)


# --------------------------------------------------------------------------------------------------
# silver sources
# --------------------------------------------------------------------------------------------------
@dataclass
class SilverFrames:
    """The silver tables one gold build reads, as plain rows (silver table -> list of {column: value}), and the release facts a silver
    row cannot carry on its own: which release is canonical, the short release names, and the ownerIds that are personas of more than
    one release (two data generations of one person, namespaced '<id>~<short>' in every non-canonical release)."""
    rows: dict[str, list[dict]]
    canonical_release: str = "retail-frontier-v1"
    release_short: dict[str, str] = field(default_factory=dict)
    collisions: set[str] = field(default_factory=set)
    columns: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, rows in self.rows.items():
            self.columns.setdefault(name, list(rows[0]) if rows else [])

    @classmethod
    def from_tables(cls, tables: dict[str, pa.Table], canonical_release: str = "retail-frontier-v1", release_short: Optional[dict] = None,
                    identity: Optional[dict] = None) -> "SilverFrames":
        """Frames over Arrow tables (what silver writes). `identity` = {table, key} names the persona table whose key repeating across
        releases is a collision (model.yaml `identity`)."""
        frames = cls({n: t.to_pylist() for n, t in tables.items()}, canonical_release, dict(release_short or {}), set(),
                     {n: list(t.column_names) for n, t in tables.items()})
        if identity:
            frames.collisions = collisions_of(frames.rows[identity["table"]], identity["key"])
        return frames


def collisions_of(rows: list[dict], key: str) -> set[str]:
    """Keys that appear in more than one release among persona rows (silver's `_release_id`)."""
    releases: dict[Any, set] = {}
    for r in rows:
        releases.setdefault(r.get(key), set()).add(r.get("_release_id"))
    return {k for k, rels in releases.items() if k is not None and len(rels) > 1}


def _parent_lookup(join: dict, frames: SilverFrames) -> Callable[[dict], Optional[Row]]:
    """`join: {silver: <parent table>, on: {<child column>: <parent column>}}` -> record -> its parent as a Row (so `^.column` reads the
    parent), or None. Parent and child must come from the same release, so `_release_id` always takes part in the match."""
    parent, on = join["silver"], join["on"]
    if parent not in frames.rows:
        raise GoldError(f"join: unknown silver table {parent}")
    index: dict[tuple, dict] = {}
    for p in frames.rows[parent]:
        index.setdefault((p.get("_release_id"), *[p.get(pc) for pc in on.values()]), p)
    child_cols = list(on)

    def lookup(rec: dict) -> Optional[Row]:
        p = index.get((rec.get("_release_id"), *[rec.get(cc) for cc in child_cols]))
        return None if p is None else Row(p, None, {"release": p.get("_release_id")})

    return lookup


def silver_source_rows(src: dict, frames: SilverFrames) -> list[Row]:
    """The rows of a `{silver: <table>}` source in silver's own order, each with the context a v1 row had: release (`_release_id`),
    release_short, collisions, canonical_release, the source's `set:` constants and its position. `where`, `require`, `absent` filter."""
    name = src["silver"]
    if name not in frames.rows:
        raise GoldError(f"unknown silver table {name}")
    parent_of = _parent_lookup(src["join"], frames) if src.get("join") else None
    base = {"collisions": frames.collisions, "canonical_release": frames.canonical_release}
    rows = []
    for i, rec in enumerate(frames.rows[name]):
        rel = rec.get("_release_id")
        ctx = {**base, "release": rel, "release_short": frames.release_short.get(rel, rel), "file": rec.get("_source_file"),
               **(src.get("set") or {}), "index": i + 1}
        rows.append(Row(rec, parent_of(rec) if parent_of else None, ctx))
    return _filter_rows(rows, src)


def _paths_of(c: dict) -> list[str]:
    """Every row path a column reads: its `from` alternatives and its tpl placeholders."""
    paths = list(c["from"] if isinstance(c.get("from"), list) else [c["from"]] if "from" in c else [])
    paths.extend(m.group(1) for m in TPL_RE.finditer(c.get("tpl") or ""))
    return paths


ENGINE_CONTEXT = {"release", "release_short", "file", "index", "collisions", "canonical_release"}


def check_silver_paths(table: dict, frames: SilverFrames) -> list[str]:
    """A gold column that names a silver column which does not exist would silently be null: reject it. The row paths of a silver
    table are its silver columns, `$name` context (the engine's own, or a source `set:` key) and `^.column` where a source joins a
    parent. Columns are checked against the union of the table's silver sources (a CRM party has a name but no age, a persona both);
    each source's own where/require/absent are checked against that source."""
    errors: list[str] = []
    name = table["table"]
    silver_sources = [s for s in table.get("sources", []) if "silver" in s]
    if not silver_sources:
        return errors                       # a reference table reads curated docs/config, not silver
    have: set[str] = set()
    ctx = set(ENGINE_CONTEXT)
    parents: set[str] = set()
    for src in silver_sources:
        if src["silver"] not in frames.columns:
            errors.append(f"{name}: unknown silver table {src['silver']}")
            continue
        own = set(frames.columns[src["silver"]])
        have |= own
        ctx |= set(src.get("set") or {})
        join = src.get("join")
        if join and join["silver"] not in frames.columns:
            errors.append(f"{name}: join names unknown silver table {join['silver']}")
        elif join:
            parents |= set(frames.columns[join["silver"]])
        flt = [*(src.get("where") or {}), *([src["require"]] if isinstance(src.get("require"), str) else src.get("require") or []),
               *([src["absent"]] if isinstance(src.get("absent"), str) else src.get("absent") or [])]
        errors.extend(f"{name}: source filter reads '{p}', which is not a column of {src['silver']}" for p in flt if p not in own)
    if errors:
        return errors
    for c in expand_columns(table):
        for p in _paths_of(c):
            ok = p[1:] in ctx if p.startswith("$") else p.lstrip("^").lstrip(".") in parents if p.startswith("^") else p in have
            if not ok:
                errors.append(f"{name}.{c['col']}: reads '{p}', which is no column of the table's silver sources")
    return list(dict.fromkeys(errors))


@dataclass
class TableResult:
    name: str
    rows: list[dict]
    columns: list[dict]
    dropped_duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    rows_read: int = 0                       # rows the sources produced, before duplicate keys were merged by precedence


def build_rows(table: dict, repo: Optional[Repo], prefixes: dict[str, str], hooks: dict[str, Callable],
               silver: Optional[SilverFrames] = None) -> TableResult:
    cols = expand_columns(table)
    res = TableResult(table["table"], [], cols)
    if "sources" not in table:
        raise GoldError(f"table {table['table']} has no sources")
    for src in table["sources"]:
        if "silver" in src:
            if silver is None:
                raise GoldError(f"table {table['table']} reads silver but no silver build was given")
            res.sources.append("silver/" + src["silver"])
            rows = silver_source_rows(src, silver)
        else:
            if repo is None:
                raise GoldError(f"table {table['table']} reads the repo but no repo was given")
            res.sources.append(src.get("release", "") + "/" + (src.get("file") or src.get("data") or src.get("data_glob") or src.get("doc") or src.get("custom")))
            rows = source_rows(src, repo)
        res.rows_read += len(rows)
        for row in rows:
            out: dict[str, Any] = {}
            for c in cols:
                where = f"{table['table']}.{c['col']}"
                try:
                    if "const" in c:
                        v = c["const"]
                    elif "none" in c:
                        v = None                        # no silver column carries it: the column stays (v1 name, type) and is null
                    elif "tpl" in c:
                        v = render_tpl(c["tpl"], row)
                    else:
                        paths = c["from"] if isinstance(c["from"], list) else [c["from"]]
                        v = None
                        for p in paths:
                            v = row.get(p)
                            if v is not None:
                                break
                    v = apply_transform(c.get("t"), v, prefixes, row)
                    if v is None and "default" in c:
                        v = c["default"]
                    out[c["col"]] = coerce(v, c["type"], where)
                except (GoldError, KeyError) as ex:
                    res.errors.append(f"{where}: {ex} (source {res.sources[-1]} row {row.ctx.get('index')})")
                    out[c["col"]] = None
                if out[c["col"]] is None and not c.get("nullable", True):
                    res.errors.append(f"{where}: null in a non-nullable column (source {res.sources[-1]} row {row.ctx.get('index')})")
            res.rows.append(out)
    for hook in table.get("post", []) or []:
        if hook not in hooks:
            raise GoldError(f"unknown post hook {hook}")
        res.rows = hooks[hook](res.rows)
    key = next((c["col"] for c in cols if c.get("key")), None)
    if key and table.get("unique_by", True):
        seen: set = set()
        kept = []
        for r in res.rows:
            k = r[key]
            if k in seen:
                res.dropped_duplicates += 1
                continue
            seen.add(k)
            kept.append(r)
        res.rows = kept
    return res


def to_arrow(res: TableResult) -> pa.Table:
    fields = [pa.field(c["col"], ARROW[c["type"]], nullable=c.get("nullable", True)) for c in res.columns]
    arrays = [pa.array([r[c["col"]] for r in res.rows], type=ARROW[c["type"]]) for c in res.columns]
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))
