"""Bronze -> silver: the seed conformed to the superapp's CDM application profile, driven by model/silver/*.yaml.

Silver keeps both seed releases side by side and loses nothing: one row per source record per release, keyed on (_release_id, business
key); no cross-release merging and no duplicate dropping (survivorship happens in gold). Columns come in this order: the profile's
common columns, the profile attributes (exact camelCase names, types, units), official-name alias columns (cdm_crosswalk), Contoso
extension columns and four technical columns. All raw-shape work is resolved here: money object -> *Minor int64 paise + currency, array
-> child table (parent key + ordinal) or '; ' delimited text, rates -> canonical decimal text. Nothing is rounded, defaulted or
manufactured: a missing value stays null, and a record whose non-nullable common column cannot be derived fails the build. The common
columns sourceKind, state, label and historical are not invented here: each table's `common:` block reproduces the source app's own
projection rules (app/data_xray/projection.py) with the cited line ranges, in the app's closed SourceKind vocabulary.

The profile's mappings are the specification, not code (its README): every attribute is implemented here with kit transforms and the
conformance tests compare the output with the profile. Contoso extension entities (no profile entity) are labelled `profile: false`
and their BIAN status stays `unmapped`; a mapping status is never upgraded.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, Inexact, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from . import REPO_COMMIT, standards
from .gold import TPL_RE, GoldError, Repo, Row, apply_transform, coerce, render_tpl, source_rows

PROFILE_DIR = "data/banking-model/v1"
ENTITY_CATALOG = "silver_entity_catalog"
ATTRIBUTE_CATALOG = "silver_attribute_catalog"
BRONZE_MANIFEST_TABLE = "bronze_file_manifest"

COMMON_COLUMNS = ("id", "label", "state", "sourceKind", "historical", "sourceRecordId", "sourceVersion")
COMMON_TYPES = {"id": "string", "label": "string", "state": "string", "sourceKind": "string", "historical": "boolean",
                "sourceRecordId": "string", "sourceVersion": "string"}
TECHNICAL_COLUMNS = {"_release_id": ("string", True), "_source_file": ("string", False), "_source_row": ("integer", False),
                     "_owner_id": ("string", True)}
# the source app's closed vocabulary (app/data_xray/contracts.py SourceKind, 32-37) and its inactive states (projection.py _INACTIVE, 66),
# copied so a build needs no app source; the conformance tests compare both with the pinned files
SOURCE_KINDS = frozenset({"bank_record", "catalogue", "document_metadata", "document_read", "customer_stated", "customer_corrected", "calculated",
                          "application", "decision", "consent", "demo_effect", "history", "derived", "schema", "frontier"})
INACTIVE_STATES = frozenset({"closed", "matured", "renewed", "superseded", "cancelled", "expired", "withdrawn"})
RETIRED_LITERALS = frozenset({"seed", "derived:recorded"})
CURRENCY_COLUMN = "currency"

SILVER_TYPES = ("string", "integer", "boolean", "date", "datetime", "minor", "decimal")
# the crosswalk's alias column types -> silver type names
ALIAS_TYPES = {"string": "string", "int64": "integer", "bool": "boolean", "date32": "date", "timestamp_utc": "datetime",
               "decimal(38,4)": "decimal(38,4)"}
ARROW_TYPES: dict[str, pa.DataType] = {"string": pa.string(), "integer": pa.int64(), "boolean": pa.bool_(), "date": pa.date32(),
                                       "datetime": pa.timestamp("us", tz="UTC"), "minor": pa.int64(), "decimal": pa.string()}
CDM_TYPES = {"string": "string", "integer": "integer", "boolean": "boolean", "date": "date", "datetime": "dateTimeOffset",
             "minor": "contosoBanking.minorAmount", "decimal": "contosoBanking.exactDecimal"}
MINOR_UNIT = "currency minor units; INR paise, exponent 2"
ORIGINS = ("profile", "extension")

DECIMAL_TYPE_RE = re.compile(r"^decimal\((\d{1,2}),(\d{1,2})\)$")
CANONICAL_DECIMAL_RE = re.compile(r"^-?(0|[1-9]\d*)(\.\d*[1-9])?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TABLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,59}$")
COLUMN_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,59}$")
TECHNICAL_NAME_RE = re.compile(r"^_[a-z][a-z0-9_]{0,58}$")
SPECIAL_CHARS = set(",;{}()\n\t= ")
INT64_MIN, INT64_MAX = -(2 ** 63), 2 ** 63 - 1
MAX_ERRORS_PER_TABLE = 10

TABLE_KEYS = {"table", "entity", "profile", "profile_entity", "ontology_entity", "domain", "serves", "grain", "description", "cdm", "bian",
              "sources", "key", "record_id", "owner", "common", "version", "notes", "columns"}
COMMON_KEYS = {"cite", "source_kind", "state", "label", "historical"}
STATE_FORMS = ({"const"}, {"from", "default"}, {"when", "then", "default"})
CITE_RE = re.compile(r"^app/[A-Za-z0-9_./-]+\.py:\d+(?:-\d+)?$")
COLUMN_KEYS = {"col", "origin", "type", "from", "tpl", "const", "derive", "reads", "t", "none", "nullable", "unit", "desc", "pii",
               "restricted", "shared"}
BASE_TRANSFORMS = {"paise", "join", "len", "facts_text", "json", "first", "lower", "upper", "str"}


class SilverError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------------------
# value conversion
# --------------------------------------------------------------------------------------------------
def canonical_decimal(v: Any) -> Optional[str]:
    """Exact decimal text as the profile defines it: plain digits, no exponent, no redundant fractional zeroes, no signed zero."""
    if v is None:
        return None
    if isinstance(v, bool):
        raise SilverError("bool is not a decimal")
    try:
        if isinstance(v, str):
            d = Decimal(v.strip())
        elif isinstance(v, float):
            d = Decimal(repr(v))
        else:
            d = Decimal(v)
    except (InvalidOperation, ValueError, TypeError):
        raise SilverError(f"not a decimal: {v!r}") from None
    if not d.is_finite():
        raise SilverError(f"not a finite decimal: {v!r}")
    if d == 0:
        return "0"
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if len(text) > 100 or d.as_tuple().exponent < -30:
        raise SilverError(f"decimal too long: {v!r}")
    return text


def rupees_to_minor(v: Any) -> Optional[int]:
    """Whole or fractional rupees -> integer paise by exact decimal multiplication; fractional paise and overflow are rejected."""
    if v is None:
        return None
    if isinstance(v, bool):
        raise SilverError("bool is not an amount")
    try:
        d = Decimal(repr(v)) if isinstance(v, float) else Decimal(str(v).strip() if isinstance(v, str) else v)
    except (InvalidOperation, ValueError, TypeError):
        raise SilverError(f"not an amount: {v!r}") from None
    with localcontext() as ctx:                          # exact: the default 28 digits would round a long amount into a whole number of paise
        ctx.prec = 100
        ctx.traps[Inexact] = True
        try:
            minor = d * 100
        except (Inexact, InvalidOperation):
            raise SilverError(f"not an exact amount: {v!r}") from None
    if not minor.is_finite() or minor != minor.to_integral_value():
        raise SilverError(f"fractional paise: {v!r}")
    if not INT64_MIN <= int(minor) <= INT64_MAX:
        raise SilverError(f"paise overflow: {v!r}")
    return int(minor)


def _paise(v: Any) -> Optional[int]:
    """A seed money object -> its amountMinor as int64 paise. The seed holds an integer; a float, a numeric string, a bool or a value outside
    int64 stops the build instead of being converted quietly (the profile: fractional paise and overflow are rejected). A null amount stays null."""
    if v is None:
        return None
    if not isinstance(v, dict) or "amountMinor" not in v:
        raise SilverError(f"expected a money object with amountMinor, got {v!r}")
    m = v["amountMinor"]
    if m is None:
        return None
    if isinstance(m, bool) or not isinstance(m, int):
        raise SilverError(f"amountMinor must be an integer number of paise, got {m!r}")
    if not INT64_MIN <= m <= INT64_MAX:
        raise SilverError(f"paise overflow: {m!r}")
    return m


def _bps_to_pct_text(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise SilverError(f"not basis points: {v!r}")
    return canonical_decimal(Decimal(str(v)) / Decimal(100))


def _version_text(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        raise SilverError(f"not a version: {v!r}")
    return str(v)


def _iso_date(v: Any) -> Optional[str]:
    if v is None:
        return None
    if not isinstance(v, str) or not DATE_RE.match(v):
        raise SilverError(f"not a plain YYYY-MM-DD date: {v!r}")
    dt.date.fromisoformat(v)
    return v


def _json_text(v: Any) -> Optional[str]:
    """A scalar the way JSON writes it (true, 1.0, 9.5); lists are '; ' joined strings."""
    if v is None:
        return None
    if isinstance(v, list):
        return "; ".join(_json_text(x) or "" for x in v if x is not None) or None
    if isinstance(v, (dict, set)):
        raise SilverError("object in a scalar column")
    return v if isinstance(v, str) else json.dumps(v)


def _value_kind(v: Any) -> Optional[str]:
    if v is None:
        return None
    return ("list" if isinstance(v, list) else "boolean" if isinstance(v, bool) else "integer" if isinstance(v, int)
            else "decimal" if isinstance(v, float) else "string")


SILVER_TRANSFORMS: dict[str, Callable[[Any], Any]] = {
    "decimal_text": canonical_decimal,
    "paise": _paise,                                      # stricter than the gold row engine's transform of the same name
    "rupees_to_paise": rupees_to_minor,
    "bps_to_pct_text": _bps_to_pct_text,
    "version_text": _version_text,
    "iso_date": _iso_date,
    "json_text": _json_text,
    "value_kind": _value_kind,
    "only_int": lambda v: v if isinstance(v, int) and not isinstance(v, bool) else None,
    "only_float": lambda v: v if isinstance(v, float) else None,
    "only_bool": lambda v: v if isinstance(v, bool) else None,
}


def apply_t(spec: Optional[str], v: Any, row: Optional[Row] = None) -> Any:
    """Run a `|` separated transform pipeline: silver transforms first, the gold row engine's for everything else."""
    if not spec:
        return v
    for part in [p for p in spec.split("|") if p]:
        name = part.partition(":")[0]
        if name in SILVER_TRANSFORMS:
            v = SILVER_TRANSFORMS[name](v)
        else:
            try:
                v = apply_transform(part, v, {}, row)
            except GoldError as ex:
                raise SilverError(str(ex)) from None
    return v


def arrow_type(typ: str) -> pa.DataType:
    if typ in ARROW_TYPES:
        return ARROW_TYPES[typ]
    m = DECIMAL_TYPE_RE.match(typ)
    if m:
        return pa.decimal128(int(m.group(1)), int(m.group(2)))
    raise SilverError(f"unknown silver type {typ!r}")


def convert(v: Any, typ: str, where: str) -> Any:
    """Coerce one value to its silver type, strictly: the answer is exact or an error, never a rounded, parsed or guessed value."""
    if v is None:
        return None
    try:
        if typ == "string":
            if not isinstance(v, str):
                raise SilverError(f"not text: {v!r}")
            return v
        if typ in ("integer", "minor"):
            if isinstance(v, bool) or not isinstance(v, int):
                raise SilverError(f"not a whole number: {v!r}")
            if not INT64_MIN <= v <= INT64_MAX:
                raise SilverError(f"int64 overflow: {v!r}")
            return v
        if typ == "boolean":
            if not isinstance(v, bool):
                raise SilverError(f"not a boolean: {v!r}")
            return v
        if typ == "date":
            return dt.date.fromisoformat(_iso_date(v.isoformat() if isinstance(v, dt.date) else v))
        if typ == "datetime":
            return coerce(v, "datetime", where)
        if typ == "decimal":
            text = canonical_decimal(v)
            if not CANONICAL_DECIMAL_RE.match(text):
                raise SilverError(f"not canonical decimal text: {text!r}")
            return text
        m = DECIMAL_TYPE_RE.match(typ)
        if m:
            precision, scale = int(m.group(1)), int(m.group(2))
            d = v if isinstance(v, Decimal) else Decimal(repr(v) if isinstance(v, float) else str(v))
            q = d.quantize(Decimal(1).scaleb(-scale))
            if q != d or len(q.as_tuple().digits) > precision:
                raise SilverError(f"{v!r} does not fit {typ} exactly")
            return q
    except (SilverError, GoldError, ValueError, InvalidOperation) as ex:
        raise SilverError(f"{where}: {ex}") from None
    raise SilverError(f"{where}: unknown type {typ}")


# --------------------------------------------------------------------------------------------------
# derived attributes: profile rules that need more than one seed field
# --------------------------------------------------------------------------------------------------
# app.agentic.bank._FAMILIES in the source repo: the families the bank policy knows, and the ones whose new facility needs an independent decision
KNOWN_FAMILIES = {"savings", "fixed_deposit", "recurring_deposit", "sweep", "goal_pocket", "credit_card", "debit_card", "prepaid_card",
                  "home_loan", "personal_loan", "education_loan", "health_cover", "term_cover", "protection"}
DECISION_FAMILIES = {"credit_card", "home_loan", "personal_loan", "education_loan", "health_cover", "term_cover", "protection"}


def _product_currency(row: Row) -> Optional[str]:
    cur = row.get("parameters.currency")
    if cur is not None:
        return cur
    return "INR" if isinstance(row.get("parameters"), dict) else None


def _product_max_term_months(row: Row) -> Optional[int]:
    months = row.get("parameters.maximumTermMonths")
    if months is not None:
        return months
    years = row.get("parameters.maximumTenureYears")
    if years is not None and row.get("family") == "home_loan":
        return int(years) * 12
    return None


def _product_requires_decision(row: Row) -> Optional[bool]:
    family = row.get("family")
    if family is None:
        return None
    if family not in KNOWN_FAMILIES:
        raise SilverError(f"product family {family!r} has no banking decision policy")
    return family in DECISION_FAMILIES


def _single_asset_value(row: Row, key: str) -> Optional[Any]:
    assets = row.get("assets")
    values = {a.get(key) for a in assets if isinstance(a, dict) and a.get(key) is not None} if isinstance(assets, list) else set()
    return next(iter(values)) if len(values) == 1 else None


def _document_content_type(row: Row) -> Optional[str]:
    return row.get("contentType") or _single_asset_value(row, "mimeType")


def _document_source_sha256(row: Row) -> Optional[str]:
    return row.get("sourceSha256") or _single_asset_value(row, "sha256")


def _document_page_count_basis(row: Row) -> Optional[str]:
    return "declared_metadata" if row.get("pageCount") is not None else None             # the app's word (projection.py:674-677)


DERIVED: dict[str, Callable[[Row], Any]] = {
    "product_currency": _product_currency,
    "product_max_term_months": _product_max_term_months,
    "product_requires_decision": _product_requires_decision,
    "document_content_type": _document_content_type,
    "document_source_sha256": _document_source_sha256,
    "document_page_count_basis": _document_page_count_basis,
}


# --------------------------------------------------------------------------------------------------
# custom sources (seed-reading flatteners): (repo-relative file, record index in the file, ordinal, row)
# --------------------------------------------------------------------------------------------------
def _preference_unpivot(repo: Repo, src: dict) -> Iterable[tuple[str, int, int, dict]]:
    """The Frontier release stores preferences as one wide record, retail4 as field/value rows: unpivot the wide one."""
    rel = f"data/releases/{src['release']}/{src.get('file', 'preferences.json')}"
    for i, r in enumerate(repo.json(rel), start=1):
        n = 0
        for f in ("contactChannel", "debtPreference", "emergencyReserveMonths", "humanReviewFor"):
            v = r.get(f)
            if v is None:
                continue
            n += 1
            yield rel, i, n, {"preferenceId": f"{r['preferenceId']}#{f}", "ownerId": r["ownerId"], "field": f,
                              "value": "; ".join(v) if isinstance(v, list) else str(v), "recordedOn": None, "statedIn": None,
                              "releaseId": r["releaseId"]}


def _flatten(node: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    """Dotted path -> leaf for every scalar or list inside nested objects."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _flatten(v, f"{prefix}.{k}" if prefix else k)
    else:
        yield prefix, node


def _product_parameter(repo: Repo, src: dict) -> Iterable[tuple[str, int, int, dict]]:
    """One row per scalar catalogue parameter (nested names dotted); the value keeps the type the seed holds."""
    for rel in src["files"]:
        for i, p in enumerate(repo.json(rel), start=1):
            n = 0
            for name, v in _flatten(p.get("parameters") or {}):
                if v is None or name in ("displayName", "family"):
                    continue
                n += 1
                yield rel, i, n, {"productId": p["productId"], "name": name, "value": v}


def _monthly_metric(repo: Repo, src: dict) -> Iterable[tuple[str, int, int, dict]]:
    rel = f"data/releases/{src['release']}/{src['file']}"
    data = repo.json(rel)
    for i, s in enumerate(data["series"], start=1):
        for j, m in enumerate(data["months"]):
            yield rel, i, j + 1, {"buId": s["buId"], "month": m, **{k: v[j] for k, v in s.items() if isinstance(v, list)}}


SILVER_CUSTOM: dict[str, Callable[[Repo, dict], Iterable[tuple[str, int, int, dict]]]] = {
    "preference_unpivot": _preference_unpivot, "product_parameter": _product_parameter, "monthly_metric": _monthly_metric}


def repo_path(src: dict) -> str:
    """Repo-relative path of the file a plain source reads."""
    if "release" in src and "file" in src:
        return f"data/releases/{src['release']}/{src['file']}"
    if "data" in src:
        return f"data/{src['data']}"
    raise SilverError(f"source has no single file: {src}")


def release_of(path: str) -> Optional[str]:
    """The release a seed file belongs to (None for release-less files such as data/relationship/products.json)."""
    m = re.match(r"^data/releases/([^/]+)/", path)
    return m.group(1) if m else None


def silver_source_rows(src: dict, repo: Repo) -> list[Row]:
    """The gold row engine's rows, plus the silver flatteners; every row remembers its file and record index."""
    name = src.get("custom")
    if name is None:
        return source_rows(src, repo)
    if name not in SILVER_CUSTOM:
        raise SilverError(f"unknown silver custom source {name}")
    base: dict[str, Any] = {"collisions": repo.collisions(), "canonical_release": repo.canonical_release, "release": src.get("release"),
                            "file": src.get("file") or name}
    if src.get("release"):
        base.update(business_date=repo.release_meta(src["release"]).get("businessDate"),
                    release_short=repo.release_short.get(src["release"], src["release"]))
    rows = []
    for seq, (rel, rec, ordinal, node) in enumerate(SILVER_CUSTOM[name](repo, src), start=1):
        rows.append(Row(node, None, {**base, **(src.get("set") or {}), "index": seq, "silver_file": rel, "silver_row": rec, "ordinal": ordinal}))
    return rows


# --------------------------------------------------------------------------------------------------
# the CDM profile, its field mappings and the BIAN registry (read only)
# --------------------------------------------------------------------------------------------------
class Profile:
    """The source repo's banking profile: entity definitions, common columns (banking.cdm.json), field mappings and BIAN registry."""

    def __init__(self, repo_root: Path):
        base = Path(repo_root) / PROFILE_DIR
        read = lambda name: json.loads((base / name).read_text(encoding="utf-8-sig"))  # noqa: E731
        prof, fm, reg, cdm = read("profile.json"), read("field-mappings.json"), read("bian-registry.json"), read("banking.cdm.json")
        self.version = prof["profileVersion"]
        self.mapping_version = prof["mappingVersion"]
        self.bian_release = reg["release"]
        self.entities: dict[str, dict] = {e["name"]: e for e in prof["entityDefinitions"]}
        self.common_names: list[str] = list(prof["commonEntityColumns"])
        self.mappings: dict[tuple[str, str], dict] = {(f["entityType"], f["attribute"]): f for f in fm["fields"]}
        self.domains: dict[str, dict] = {d["id"]: d for d in reg["domains"]}
        self.bian: dict[tuple[str, str], list[dict]] = {}
        for m in reg["mappings"]:
            self.bian.setdefault((m["entityType"], m["attribute"]), []).append(m)
        for rows in self.bian.values():
            rows.sort(key=lambda m: int(m["id"].rsplit(":", 1)[1]))
        self.cdm_common: dict[str, dict[str, tuple[str, bool]]] = {}
        for d in cdm["definitions"]:
            attrs = {a["name"]: (a["dataType"], bool(a["isNullable"])) for a in d.get("hasAttributes", []) if a["name"] in self.common_names}
            if attrs:
                self.cdm_common[d["entityName"]] = attrs
        self.relationships: list[dict] = prof.get("relationships", [])
        self.raw = prof

    def attributes(self, entity: str) -> list[dict]:
        return self.entities[entity]["attributes"]

    def bian_of(self, entity: str, attribute: str) -> tuple[Optional[str], Optional[str], str]:
        """(domain names, control records, statuses): '; ' joined in registry order, so the i-th entry of each belongs to one mapping."""
        rows = self.bian.get((entity, attribute), [])
        if not rows:
            return None, None, "unmapped"
        names = [self.domains[m["domainId"]]["name"] if m.get("domainId") else None for m in rows]
        controls = [m.get("controlRecord") for m in rows]
        join = lambda xs: "; ".join(x for x in xs if x) or None  # noqa: E731
        return join(names), join(controls), "; ".join(m["mappingStatus"] for m in rows)


# --------------------------------------------------------------------------------------------------
# the common columns the app derives itself: sourceKind, state, label and historical (app/data_xray/projection.py)
# --------------------------------------------------------------------------------------------------
def _paths(v: Any) -> list[str]:
    return [v] if isinstance(v, str) else [p for p in v if isinstance(p, str)] if isinstance(v, list) else []


def _present(row: Row, paths: list[str]) -> Any:
    """The first seed value among the paths that is neither null nor an empty string."""
    for p in paths:
        v = row.get(p)
        if v is not None and v != "":
            return v
    return None


def _text(v: Any, what: str) -> str:
    if not isinstance(v, str):
        raise SilverError(f"{what} is not text: {v!r}")
    return v


def common_state(rule: dict, row: Row) -> str:
    """`state` as the app's put() receives it: a constant, the seed's own lifecycle field with the app's fallback, or a flag field's switch."""
    if "const" in rule:
        return rule["const"]
    if "from" in rule:
        v = _present(row, _paths(rule["from"]))
        return rule["default"] if v is None else _text(v, "state")
    return rule["then"] if _present(row, _paths(rule["when"])) else rule["default"]


def common_label(rule: Optional[dict], row: Row, record: Optional[str]) -> Optional[str]:
    """`label`: the first present display field, else the rule's literal, else the business key (the app's `label or business_id`)."""
    if rule:
        v = _present(row, _paths(rule["from"]))
        if v is not None:
            return _text(v, "label")
        if rule.get("default"):
            return rule["default"]
    return record


def common_kind(common: dict, state: str) -> tuple[bool, str]:
    """(historical, sourceKind): a record in an inactive state is retained history and carries the history kind, as in the app's holdings loop."""
    rule = common.get("historical")
    if rule and state in INACTIVE_STATES:
        return True, rule["source_kind"]
    return False, common["source_kind"]


def common_paths(t: dict) -> list[str]:
    """Seed paths the state and label rules read: lineage only, a real column must expose them too (the superset test checks it)."""
    c = t.get("common") or {}
    state = c.get("state") or {}
    return list(dict.fromkeys([*_paths(state.get("from")), *_paths(state.get("when")), *_paths((c.get("label") or {}).get("from"))]))


def validate_common(t: dict) -> list[str]:
    """The `common:` block: the app's closed SourceKind vocabulary, an app source citation, a state that cannot be empty, and none of the
    retired placeholders ('seed', 'derived:recorded')."""
    n = t.get("table", "?")
    c = t.get("common")
    if not isinstance(c, dict):
        return [f"{n}: missing `common` (the sourceKind and state rules)"]
    errors = [f"{n}: unknown common key {k!r}" for k in sorted(set(c) - COMMON_KEYS)]
    cites = [c["cite"]] if isinstance(c.get("cite"), str) else c.get("cite")
    if not cites or not isinstance(cites, list) or not all(isinstance(x, str) and CITE_RE.match(x) for x in cites):
        errors.append(f"{n}: common.cite must name the app source lines, for example app/data_xray/projection.py:516-523")

    def literal(where: str, v: Any) -> None:
        if not isinstance(v, str) or not v.strip():
            errors.append(f"{n}: common.{where} must be non-empty text")
        elif v in RETIRED_LITERALS:
            errors.append(f"{n}: common.{where} {v!r} is a retired placeholder; use the app's vocabulary")

    def seed_path(where: str, v: Any) -> None:
        if not _paths(v) or (isinstance(v, list) and len(_paths(v)) != len(v)):
            errors.append(f"{n}: common.{where} needs a seed path or a list of seed paths")

    if c.get("source_kind") not in SOURCE_KINDS:
        errors.append(f"{n}: common.source_kind {c.get('source_kind')!r} is not in the app's SourceKind vocabulary")
    state = c.get("state")
    if not isinstance(state, dict) or set(state) not in STATE_FORMS:
        errors.append(f"{n}: common.state is {{const}}, {{from, default}} or {{when, then, default}}, so a state is never empty")
    else:
        for k, v in state.items():
            (seed_path if k in ("from", "when") else literal)(f"state.{k}", v)
    label = c.get("label")
    if label is not None:
        if not isinstance(label, dict) or "from" not in label or set(label) - {"from", "default"}:
            errors.append(f"{n}: common.label is {{from[, default]}}")
        else:
            seed_path("label.from", label["from"])
            if "default" in label:
                literal("label.default", label["default"])
    hist = c.get("historical")
    if hist is not None:
        if not isinstance(hist, dict) or set(hist) != {"state_in", "source_kind"} or hist.get("state_in") != "inactive":
            errors.append(f"{n}: common.historical is {{state_in: inactive, source_kind: <kind>}}")
        elif hist["source_kind"] not in SOURCE_KINDS:
            errors.append(f"{n}: common.historical.source_kind {hist['source_kind']!r} is not in the app's SourceKind vocabulary")
    return errors


# --------------------------------------------------------------------------------------------------
# spec loading and validation
# --------------------------------------------------------------------------------------------------
def load_silver_spec(model_dir: Path) -> dict:
    """Merge model/silver/*.yaml (one file per business area) into {'tables': [...]} in file order."""
    tables: list[dict] = []
    for f in sorted((Path(model_dir) / "silver").glob("*.yaml")):
        tables.extend((yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("silver") or [])
    return {"tables": tables}


def snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def validate_name(name: str, kind: str) -> Optional[str]:
    """One physical-name rule: camelCase columns are fine, the gold snake_case rule does not apply; no special characters."""
    pattern = {"table": TABLE_NAME_RE, "column": COLUMN_NAME_RE, "technical": TECHNICAL_NAME_RE}[kind]
    if not isinstance(name, str) or not pattern.match(name) or set(name) & SPECIAL_CHARS:
        return f"{kind} name {name!r} must match {pattern.pattern} and contain none of ',;{{}}()=', whitespace"
    return None


def validate_physical_names(tables: Iterable[str], columns: dict[str, list[str]]) -> list[str]:
    """Table and column names: allowed characters, case-insensitive uniqueness (Delta/SQL endpoint are case-insensitive)."""
    errors: list[str] = []
    seen: dict[str, str] = {}
    for t in tables:
        err = validate_name(t, "table")
        if err:
            errors.append(err)
        if t.lower() in seen:
            errors.append(f"table {t!r} collides case-insensitively with {seen[t.lower()]!r}")
        seen.setdefault(t.lower(), t)
    for t, cols in columns.items():
        cseen: dict[str, str] = {}
        for c in cols:
            err = validate_name(c, "technical" if c.startswith("_") else "column")
            if err:
                errors.append(f"{t}: {err}")
            if c.lower() in cseen:
                errors.append(f"{t}: column {c!r} collides case-insensitively with {cseen[c.lower()]!r}")
            cseen.setdefault(c.lower(), c)
    return errors


def is_extension(t: dict) -> bool:
    return not t.get("profile_entity")


def column_origin_prefix(t: dict) -> str:
    """'x' on a profile entity's extension columns; extension entities use plain camelCase."""
    return "" if is_extension(t) else "x"


def emitted_columns(t: dict, aliases: Optional[list] = None) -> list[dict]:
    """Every physical column of a silver table, in order: common, profile, official aliases, extension, technical."""
    spec_cols = t.get("columns") or []
    shared = next((c for c in spec_cols if c.get("shared") == "common"), None)
    out: list[dict] = []
    for name in COMMON_COLUMNS:
        out.append({"col": name, "origin": "common", "type": COMMON_TYPES[name], "nullable": name == "sourceVersion",
                    **({"profile_attribute": name} if shared and name == shared["col"] else {})})
    out.extend({**c, "nullable": c.get("nullable", True)} for c in spec_cols if c.get("origin") == "profile" and c.get("shared") != "common")
    for a in aliases or []:
        out.append({"col": a.official_attribute, "origin": "cdm_official", "type": ALIAS_TYPES.get(a.silver_type, a.silver_type),
                    "nullable": True, "alias": a})
    out.extend({**c, "nullable": c.get("nullable", True)} for c in spec_cols if c.get("origin") == "extension")
    for name, (typ, nullable) in TECHNICAL_COLUMNS.items():
        out.append({"col": name, "origin": "technical", "type": typ, "nullable": nullable})
    return out


def source_paths(c: dict) -> list[str]:
    """Seed paths a column exposes: its from alternatives and tpl placeholders (a derived column's `reads` are lineage only)."""
    paths: list[str] = []
    if "from" in c:
        paths.extend(c["from"] if isinstance(c["from"], list) else [c["from"]])
    if "tpl" in c:
        paths.extend(m.group(1) for m in TPL_RE.finditer(c["tpl"]))
    return paths


def validate_spec(spec: dict) -> list[str]:
    """Structural checks that need no repo: shapes, names, keys, types, transforms, the common-column rules, currency pairing, the
    extension-entity claim."""
    errors: list[str] = []
    tables = spec.get("tables") or []
    if not tables:
        return ["silver spec has no tables"]
    counts: dict[str, int] = {}
    for t in tables:
        counts[str(t.get("table"))] = counts.get(str(t.get("table")), 0) + 1
    errors.extend(f"duplicate silver table {n}" for n, k in counts.items() if k > 1)
    errors.extend(validate_physical_names(list(counts), {}))
    for t in tables:
        n = t.get("table", "?")
        errors.extend(f"{n}: unknown table key {k!r}" for k in sorted(set(t) - TABLE_KEYS))
        errors.extend(f"{n}: missing {k}" for k in ("table", "entity", "domain", "serves", "grain", "sources", "key", "columns") if not t.get(k))
        errors.extend(validate_common(t))
        if not t.get("columns"):
            continue
        if bool(t.get("profile_entity")) != (t.get("profile", True) is not False):
            errors.append(f"{n}: profile_entity and `profile: false` disagree")
        if t.get("profile_entity"):
            if "cdm" not in t or not isinstance(t.get("bian"), list):
                errors.append(f"{n}: a profile entity carries its cdm and bian summary")
        elif t.get("bian") or t.get("cdm"):
            errors.append(f"{n}: an extension entity makes no CDM or BIAN claim")
        by = {c.get("col"): c for c in t["columns"]}
        if len(by) != len(t["columns"]):
            errors.append(f"{n}: duplicate column names")
        errors.extend(validate_physical_names([], {n: [c["col"] for c in emitted_columns(t)]}))
        prefix = column_origin_prefix(t)
        for c in t["columns"]:
            where = f"{n}.{c.get('col')}"
            errors.extend(f"{where}: unknown column key {k!r}" for k in sorted(set(c) - COLUMN_KEYS))
            if c.get("origin") not in ORIGINS:
                errors.append(f"{where}: origin must be one of {ORIGINS}")
            if c.get("type") not in SILVER_TYPES:
                errors.append(f"{where}: type must be one of {SILVER_TYPES}")
            kinds = [k for k in ("from", "tpl", "const", "derive", "none") if k in c]
            if len(kinds) != 1:
                errors.append(f"{where}: needs exactly one of from/tpl/const/derive/none (has {kinds})")
            if c.get("derive") and c["derive"] not in DERIVED:
                errors.append(f"{where}: unknown derive {c['derive']!r}")
            if c.get("shared") not in (None, "common") or (c.get("shared") and c["col"] != "sourceVersion"):
                errors.append(f"{where}: only the profile's sourceVersion can be shared with the common column")
            for part in [p for p in (c.get("t") or "").split("|") if p]:
                if part.partition(":")[0] not in SILVER_TRANSFORMS | dict.fromkeys(BASE_TRANSFORMS):
                    errors.append(f"{where}: unknown transform {part.partition(':')[0]!r}")
            if c.get("origin") == "extension":
                ok = c["col"][:1] == "x" and c["col"][1:2].isupper() if prefix else c["col"][:1].islower()
                if not ok:
                    errors.append(f"{where}: extension columns of a {'profile' if prefix else 'extension'} entity are "
                                  f"{'x + PascalCase' if prefix else 'plain camelCase'}")
            if c.get("origin") == "profile" and not t.get("profile_entity"):
                errors.append(f"{where}: profile column on an extension entity")
        errors.extend(f"{n}: key column {k!r} is not a column" for k in t.get("key", []) if k not in by)
        if any("paise" in (c.get("t") or "") for c in t["columns"]) and CURRENCY_COLUMN not in by:
            errors.append(f"{n}: money columns need a '{CURRENCY_COLUMN}' column")
    return errors


# --------------------------------------------------------------------------------------------------
# building tables
# --------------------------------------------------------------------------------------------------
@dataclass
class SilverResult:
    name: str
    rows: list[dict]
    columns: list[dict]
    errors: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


@dataclass
class SilverBuild:
    spec: dict
    results: dict[str, SilverResult]
    tables: dict[str, pa.Table]
    bronze: list[dict]
    profile: Optional[Profile] = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _first(row: Row, paths: list[str]) -> Any:
    for p in paths:
        v = row.get(p)
        if v is not None:
            return v
    return None


def _column_value(c: dict, row: Row, currencies: set[str]) -> Any:
    where = c["col"]
    if "const" in c:
        v = c["const"]
    elif "tpl" in c:
        v = render_tpl(c["tpl"], row)
    elif "derive" in c:
        v = DERIVED[c["derive"]](row)
    elif "from" in c:
        v = _first(row, c["from"] if isinstance(c["from"], list) else [c["from"]])
    else:
        return None
    if isinstance(v, dict) and "amountMinor" in v and "paise" in (c.get("t") or ""):
        if v.get("currency"):
            currencies.add(v["currency"])
    return convert(apply_t(c.get("t"), v, row), c["type"], where)


def _build_table(t: dict, repo: Repo, aliases: list) -> SilverResult:
    cols = emitted_columns(t, aliases)
    res = SilverResult(t["table"], [], cols)
    data_cols = [c for c in t["columns"] if c["origin"] in ORIGINS]
    money_cols = [c["col"] for c in data_cols if c["type"] == "minor"]
    has_currency_col = any(c["col"] == CURRENCY_COLUMN for c in data_cols)
    shared = next((c for c in t["columns"] if c.get("shared") == "common"), None)
    errors = res.errors
    for src in t["sources"]:
        path = repo_path(src) if "custom" not in src else None
        for row in silver_source_rows(src, repo):
            where = f"{t['table']} ({path or src['custom']} row {row.ctx.get('index')})"
            out: dict[str, Any] = {}
            currencies: set[str] = set()
            for c in data_cols:
                try:
                    out[c["col"]] = _column_value(c, row, currencies)
                except (SilverError, GoldError) as ex:
                    errors.append(f"{where}: {c['col']}: {ex}")
                    out[c["col"]] = None
            cur = out.get(CURRENCY_COLUMN)
            if currencies and (cur is None or currencies != {cur}):
                errors.append(f"{where}: money currencies {sorted(currencies)} disagree with the currency column {cur!r}")
            elif has_currency_col and cur is None and any(out.get(m) is not None for m in money_cols):
                errors.append(f"{where}: a money amount is present but no currency is recorded (the currency column is null)")
            for a in aliases:
                try:
                    out[a.official_attribute] = convert(_apply_alias(a, out.get(a.profile_attribute)),
                                                        ALIAS_TYPES.get(a.silver_type, a.silver_type), a.official_attribute)
                except (SilverError, GoldError, ValueError, TypeError) as ex:
                    errors.append(f"{where}: alias {a.official_attribute}: {ex}")
                    out[a.official_attribute] = None
            _add_common(t, row, out, shared, errors, where)
            file_rel = row.ctx.get("silver_file") or path
            out["_release_id"] = row.ctx.get("release") or release_of(file_rel)
            out["_source_file"] = file_rel
            out["_source_row"] = row.ctx.get("silver_row") or (row.parent.ctx["index"] if row.parent else row.ctx["index"])
            out["_owner_id"] = None if not t.get("owner") or row.get(t["owner"]) is None else str(row.get(t["owner"]))
            if file_rel not in res.files:
                res.files.append(file_rel)
            res.rows.append(out)
            if len(errors) >= MAX_ERRORS_PER_TABLE:
                return res
    _finish_table(t, repo, res)
    return res


def _apply_alias(alias: Any, base: Any) -> Any:
    from . import cdm_crosswalk
    return cdm_crosswalk.apply_alias(alias, base)


def _add_common(t: dict, row: Row, out: dict, shared: Optional[dict], errors: list[str], where: str) -> None:
    if t.get("record_id"):
        record = render_tpl(t["record_id"], row)
    else:
        parts = [out.get(k) for k in t["key"]]
        record = None if any(p is None for p in parts) else "#".join(str(p) for p in parts)
    if record is None:
        errors.append(f"{where}: the business key {t['key']} cannot be derived (non-nullable sourceRecordId)")
    common = t["common"]
    try:
        state = common_state(common["state"], row)
        label = common_label(common.get("label"), row, record)
    except SilverError as ex:
        errors.append(f"{where}: {ex}")
        state, label = None, record
    historical, kind = common_kind(common, state) if state else (False, common["source_kind"])
    out.update({"id": None if record is None else f"{t['entity']}:{record}", "label": label, "state": state, "sourceKind": kind,
                "historical": historical, "sourceRecordId": record})
    try:
        if shared is not None:
            out["sourceVersion"] = out.get(shared["col"])
        else:
            out["sourceVersion"] = _version_text(row.get(t["version"])) if t.get("version") else None
    except SilverError as ex:
        errors.append(f"{where}: sourceVersion: {ex}")
        out["sourceVersion"] = None


def _finish_table(t: dict, repo: Repo, res: SilverResult) -> None:
    """Key and id uniqueness. A business key seen in several releases keeps its raw form for the canonical release and gets
    '~<short>' elsewhere in the opaque `id` (the same legacy namespacing as gold's customer ids)."""
    errors = res.errors
    short = repo.release_short
    seen_key: dict[tuple, int] = {}
    seen_rec: dict[str, set] = {}
    for r in res.rows:
        key = (r["_release_id"], *[r.get(k) for k in t["key"]])
        if any(v is None for v in key[1:]):
            errors.append(f"{t['table']}: null business key {t['key']} in {r['_source_file']} row {r['_source_row']}")
        seen_key[key] = seen_key.get(key, 0) + 1
        seen_rec.setdefault(r["sourceRecordId"], set()).add(r["_release_id"])
    for key, n in seen_key.items():
        if n > 1:
            errors.append(f"{t['table']}: {n} rows share the business key {key} (silver never drops a source row)")
    for r in res.rows:
        if len(seen_rec.get(r["sourceRecordId"], ())) > 1 and r["_release_id"] != repo.canonical_release:
            tag = short.get(r["_release_id"], r["_release_id"] or "norelease")
            r["id"] = f"{r['id']}~{tag}"
    ids = [r["id"] for r in res.rows]
    if len(set(ids)) != len(ids):
        errors.append(f"{t['table']}: the opaque id is not unique after namespacing")
    for r in res.rows:
        bad = [c["col"] for c in res.columns if not c["nullable"] and r.get(c["col"]) is None]
        if bad:
            errors.append(f"{t['table']}: non-nullable {bad} is null in {r['_source_file']} row {r['_source_row']}")
            break


def to_arrow(res: SilverResult) -> pa.Table:
    fields = [pa.field(c["col"], arrow_type(c["type"]), nullable=c["nullable"]) for c in res.columns]
    arrays = [pa.array([r[c["col"]] for r in res.rows], type=arrow_type(c["type"])) for c in res.columns]
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))


def load_aliases() -> dict[str, list]:
    """Official-name alias columns per profile entity from the CDM crosswalk (model/cdm_crosswalk.yaml)."""
    from . import cdm_crosswalk
    return cdm_crosswalk.load_crosswalk()


def check_aliases(spec: dict, aliases: dict[str, list], profile: Profile, repo_root: Path) -> list[str]:
    """The crosswalk is valid against the vendored official entity files, and every alias sits on a silver profile column."""
    from . import cdm_crosswalk
    errors = cdm_crosswalk.validate(aliases, cdm_crosswalk.official_index(Path(repo_root)), profile.raw)
    for t in spec["tables"]:
        have = {c["col"] for c in t["columns"] if c.get("origin") == "profile"}
        for a in aliases.get(t.get("profile_entity") or "", []):
            if a.profile_attribute not in have:
                errors.append(f"{t['table']}: alias {a.official_attribute} names profile attribute {a.profile_attribute} that the table lacks")
        errors.extend(validate_physical_names([], {t["table"]: [c["col"] for c in emitted_columns(t, aliases.get(t.get("profile_entity") or "", []))]}))
    return errors


def build_silver(model: dict, spec: dict, repo_root: Path, profile: Optional[Profile] = None,
                 aliases: Optional[dict[str, list]] = None) -> SilverBuild:
    """Build every silver table plus the two catalogs and the bronze file manifest rows. `aliases` defaults to the committed
    crosswalk; pass {} to build without official-name alias columns."""
    repo = Repo(repo_root, model.get("canonical_release", "retail-frontier-v1"), model.get("release_short"))
    out = SilverBuild(spec, {}, {}, [])
    out.errors.extend(validate_spec(spec))
    if out.errors:
        return out
    out.profile = profile = profile or Profile(repo_root)
    aliases = load_aliases() if aliases is None else aliases
    out.errors.extend(check_aliases(spec, aliases, profile, repo_root))
    if out.errors:
        return out
    for t in spec["tables"]:
        try:
            res = _build_table(t, repo, aliases.get(t.get("profile_entity") or "", []))
        except (SilverError, GoldError, KeyError, FileNotFoundError) as ex:
            out.errors.append(f"{t['table']}: {type(ex).__name__}: {ex}")
            continue
        out.errors.extend(res.errors[:MAX_ERRORS_PER_TABLE])
        if not res.rows:
            out.warnings.append(f"{t['table']}: no rows")
        out.results[t["table"]] = res
        if not res.errors:
            out.tables[t["table"]] = to_arrow(res)
    if out.errors:
        return out
    out.bronze = bronze_files(spec, repo)
    out.tables[ENTITY_CATALOG] = entity_catalog(spec, out.results)
    out.tables[ATTRIBUTE_CATALOG] = attribute_catalog(spec, out.results, profile)
    return out


# --------------------------------------------------------------------------------------------------
# catalogs (the machine-readable standards traceability) and the bronze manifest
# --------------------------------------------------------------------------------------------------
def bian_label(b: dict) -> str:
    return f"{b['domain']} ({standards._bian_status(b)})"


def _source_pointer(c: dict) -> Optional[str]:
    if "none" in c:
        return f"none: {c['none']}"
    if "const" in c:
        return f"const: {c['const']}"
    if "tpl" in c:
        return f"tpl: {c['tpl']}"
    paths = source_paths(c) if "derive" not in c else list(c.get("reads") or [])
    text = " | ".join("/" + p.replace(".", "/") if not p.startswith(("$", "^")) else p for p in paths) if paths else ""
    return f"derive:{c['derive']} reads {text}" if "derive" in c else text or None


def entity_catalog(spec: dict, results: dict[str, SilverResult]) -> pa.Table:
    rows = []
    for t in spec["tables"]:
        cdm = t.get("cdm") or {}
        rows.append({"entity": t["entity"], "silver_table": t["table"], "profile_entity": t.get("profile_entity"),
                     "extension": is_extension(t), "cdm_reference_entity": cdm.get("entity"), "cdm_reference_status": cdm.get("status"),
                     "bian_domains": "; ".join(bian_label(b) for b in t.get("bian") or []) or None,
                     "source_files": "; ".join(results[t["table"]].files), "grain": t["grain"],
                     "row_count": len(results[t["table"]].rows)})
    schema = pa.schema([pa.field(n, pa.bool_() if n == "extension" else pa.int64() if n == "row_count" else pa.string(),
                                 nullable=n in ("profile_entity", "cdm_reference_entity", "cdm_reference_status", "bian_domains"))
                        for n in ("entity", "silver_table", "profile_entity", "extension", "cdm_reference_entity", "cdm_reference_status",
                                  "bian_domains", "source_files", "grain", "row_count")])
    return pa.Table.from_pylist(rows, schema=schema)


ATTRIBUTE_CATALOG_COLUMNS = ("silver_table", "column_name", "origin", "profile_attribute", "cdm_type", "unit", "nullable", "privacy",   # COLUMN is reserved in T-SQL
                             "bian_domain", "bian_control_record", "bian_mapping_status", "official_entity", "official_attribute",
                             "match_type", "source_pointer", "transform")


def _kit_privacy(c: dict) -> list[str]:
    return ["restricted"] if c.get("restricted") else ["pii"] if c.get("pii") else []


def attribute_catalog(spec: dict, results: dict[str, SilverResult], profile: Profile) -> pa.Table:
    rows = []
    for t in spec["tables"]:
        by = {c["col"]: c for c in t["columns"]}
        shared = next((c for c in t["columns"] if c.get("shared") == "common"), None)
        entity = t.get("profile_entity")
        for c in results[t["table"]].columns:
            origin, name = c["origin"], c["col"]
            spec_col = by.get(name, {})
            row: dict[str, Any] = {"silver_table": t["table"], "column_name": name, "origin": origin, "profile_attribute": None,
                                   "cdm_type": CDM_TYPES.get(c["type"], c["type"]), "unit": None, "nullable": c["nullable"], "privacy": None,
                                   "bian_domain": None, "bian_control_record": None, "bian_mapping_status": None, "official_entity": None,
                                   "official_attribute": None, "match_type": None, "source_pointer": None, "transform": None}
            if origin == "common":
                row["source_pointer"] = _common_pointer(t, name, shared)
                if name in ("label", "state", "sourceKind", "historical"):
                    row["transform"] = f"app rule: {_cites(t)}"
                if entity:
                    row["cdm_type"] = profile.cdm_common.get(entity, {}).get(name, (row["cdm_type"], None))[0]
                if shared and name == shared["col"]:
                    spec_col = shared
                    row["profile_attribute"] = name
            if origin == "profile" or (origin == "common" and spec_col is shared and shared):
                attr = next(a for a in profile.attributes(entity) if a["name"] == name)
                dom, ctl, status = profile.bian_of(entity, name)
                m = profile.mappings[(entity, name)]
                row.update(profile_attribute=name, cdm_type=attr["cdmType"], unit=attr.get("unit"), nullable=bool(attr["nullable"]) if origin == "profile" else c["nullable"],
                           bian_domain=dom, bian_control_record=ctl, bian_mapping_status=status)
                row["privacy"] = "; ".join(_kit_privacy(spec_col) + [m["privacy"]])
            if origin in ("profile", "extension"):
                row["source_pointer"] = _source_pointer(spec_col)
                row["transform"] = spec_col.get("t") or (f"derive:{spec_col['derive']}" if "derive" in spec_col else "copy" if "none" not in spec_col else None)
            if origin == "extension":
                row["unit"] = spec_col.get("unit") or (MINOR_UNIT if c["type"] == "minor" else None)
                row["bian_mapping_status"] = "unmapped"
                row["privacy"] = "; ".join(_kit_privacy(spec_col)) or None
            if origin == "technical":
                row["source_pointer"] = {"_release_id": "context: the release the record was read from", "_source_file": "context: repo-relative file",
                                         "_source_row": "context: 1-based record position in the file (parent record for array children)",
                                         "_owner_id": f"/{t['owner']}" if t.get("owner") else None}[name]
            if origin == "cdm_official":
                a = c["alias"]
                row.update(profile_attribute=a.profile_attribute, cdm_type=a.official_type, unit=a.unit, official_entity=a.official_entity,
                           official_attribute=a.official_attribute, match_type=a.match, source_pointer=f"silver column {a.profile_attribute}",
                           transform=a.transform)
            rows.append(row)
    string_cols = [n for n in ATTRIBUTE_CATALOG_COLUMNS if n != "nullable"]
    schema = pa.schema([pa.field(n, pa.string()) if n in string_cols else pa.field(n, pa.bool_(), nullable=False) for n in ATTRIBUTE_CATALOG_COLUMNS])
    return pa.Table.from_pylist(rows, schema=schema)


def _seed_pointer(paths: Any) -> str:
    return " | ".join("/" + p.replace(".", "/") for p in _paths(paths))


def _cites(t: dict) -> str:
    cite = t["common"]["cite"]
    return "; ".join([cite] if isinstance(cite, str) else cite)


def _common_pointer(t: dict, name: str, shared: Optional[dict]) -> str:
    c = t["common"]
    if name == "sourceKind":
        h = c.get("historical")
        return f"{h['source_kind']} when historical, else {c['source_kind']}" if h else f"const: {c['source_kind']}"
    if name == "historical":
        return "state in {" + ", ".join(sorted(INACTIVE_STATES)) + "}" if c.get("historical") else "const: false"
    if name == "id":
        return f"{t['entity']}:<sourceRecordId> (~<release> when the key repeats in a non-canonical release)"
    if name == "sourceRecordId":
        return f"tpl: {t['record_id']}" if t.get("record_id") else "key: " + "#".join(t["key"])
    if name == "label":
        label = c.get("label")
        if not label:
            return "sourceRecordId"
        return f"{_seed_pointer(label['from'])} else " + (f"'{label['default']}'" if label.get("default") else "sourceRecordId")
    if name == "state":
        s = c["state"]
        if "const" in s:
            return f"const: {s['const']}"
        if "from" in s:
            return f"{_seed_pointer(s['from'])} else '{s['default']}'"
        return f"'{s['then']}' when {_seed_pointer(s['when'])} is set, else '{s['default']}'"
    if shared is not None:
        return _source_pointer(shared) or "none"
    return f"/{t['version']}" if t.get("version") else "none"


def _json_files(spec: dict) -> dict[str, dict]:
    """Repo-relative JSON file -> {release, lists} for every file a silver source reads, plus each used release's release.json."""
    files: dict[str, dict] = {}

    def add(path: str, release: Optional[str], lst: Optional[str] = None) -> None:
        info = files.setdefault(path, {"release": release, "lists": []})
        if lst and lst not in info["lists"]:
            info["lists"].append(lst)

    for t in spec["tables"]:
        for src in t["sources"]:
            rel = src.get("release")
            if src.get("custom") == "product_parameter":
                for f in src["files"]:
                    add(f, f.split("/")[2] if f.startswith("data/releases/") else None)
            elif "file" in src or "data" in src:
                add(repo_path(src), rel, src.get("list") or src.get("records"))
            if rel:
                add(f"data/releases/{rel}/release.json", rel)
    return files


def bronze_paths(spec: dict) -> list[str]:
    """The repo-relative seed JSON files the tables of `spec` read, sorted: the bronze files of exactly those silver tables (no file is opened;
    pass a spec cut down to some tables to get the files that slice needs)."""
    return sorted(_json_files(spec))


def bronze_files(spec: dict, repo: Repo) -> list[dict]:
    """bronze_file_manifest rows: the raw seed files the silver layer reads, verbatim and hashed."""
    rows = []
    for path, info in sorted(_json_files(spec).items()):
        p = repo.root / path
        data = json.loads(p.read_bytes().decode("utf-8-sig"))
        if info["lists"]:
            n = sum(len(data[k]) for k in info["lists"])
        else:
            n = len(data) if isinstance(data, list) else 1
        rows.append({"release": info["release"], "path": path, "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                     "bytes": p.stat().st_size, "record_count": n})
    return rows


def bronze_table(rows: list[dict]) -> pa.Table:
    schema = pa.schema([pa.field("release", pa.string()), pa.field("path", pa.string(), nullable=False),
                        pa.field("sha256", pa.string(), nullable=False), pa.field("bytes", pa.int64(), nullable=False),
                        pa.field("record_count", pa.int64(), nullable=False)])
    return pa.Table.from_pylist(rows, schema=schema)


# --------------------------------------------------------------------------------------------------
# outputs
# --------------------------------------------------------------------------------------------------
def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _schema_of(tbl: pa.Table) -> list[dict]:
    return [{"name": f.name, "type": str(f.type), "nullable": f.nullable} for f in tbl.schema]


def _write_tables(tables: dict[str, pa.Table], folder: Path, serves: Optional[dict[str, list[str]]] = None) -> dict[str, dict]:
    folder.mkdir(parents=True, exist_ok=True)
    out: dict[str, dict] = {}
    for name, tbl in tables.items():
        p = folder / f"{name}.parquet"
        pq.write_table(tbl, p, compression="snappy", version="2.6")
        out[name] = {"physical": name, "rows": tbl.num_rows, "columns": tbl.num_columns, "bytes": p.stat().st_size, "sha256": _sha256(p),
                     "schema": _schema_of(tbl), **({"serves": serves[name]} if serves and name in serves else {})}
    for stale in folder.glob("*.parquet"):                    # a table of an earlier, larger build: whatever globs this folder must find only what the manifest lists
        if stale.stem not in out:
            stale.unlink()
    return out


def write_bronze(build: SilverBuild, out_dir: Path) -> dict:
    folder = Path(out_dir) / "bronze"
    tables = _write_tables({BRONZE_MANIFEST_TABLE: bronze_table(build.bronze)}, folder)
    manifest = {"layer": "bronze", "repoCommitPinned": REPO_COMMIT, "files": build.bronze, "tables": tables}
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    return manifest


def write_silver(build: SilverBuild, out_dir: Path, model_dir: Optional[Path] = None) -> dict:
    folder = Path(out_dir) / "silver"
    serves = {t["table"]: list(t["serves"]) for t in build.spec["tables"]}
    manifest: dict[str, Any] = {"layer": "silver", "repoCommitPinned": REPO_COMMIT,
                                "profile": {"profileVersion": build.profile.version, "mappingVersion": build.profile.mapping_version,
                                            "bianRelease": build.profile.bian_release},
                                "tables": _write_tables(build.tables, folder, serves)}
    if model_dir:
        manifest["specFiles"] = {p.name: _sha256(p) for p in sorted((Path(model_dir) / "silver").glob("*.yaml"))}
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    return manifest


# --------------------------------------------------------------------------------------------------
# lineage helpers: which seed fields a table reads (silver superset check, gold migration)
# --------------------------------------------------------------------------------------------------
def source_key(src: dict) -> str:
    if "custom" in src:
        return f"custom:{src['custom']}"
    if "release" in src and "file" in src:
        return f"{src['release']}/{src['file']}"
    if "data" in src:
        return f"data/{src['data']}"
    return f"doc:{src.get('doc') or src.get('data_glob')}"


def normalise_path(path: str, src: dict) -> Optional[tuple[str, str]]:
    """(array level, dotted field) a row path reads. Level '' is the record itself; ^ climbs to it from an exploded element;
    context variables ($release, $index, ...) are not seed fields except $business_date, which is release.json's businessDate."""
    if path.startswith("$"):
        return ("", "$business_date") if path == "$business_date" else None
    ups = 0
    while path.startswith("^"):
        ups += 1
        path = path[1:].lstrip(".")
    if path in ("", "@"):
        return ("" if ups else src.get("explode", ""), "@")
    return ("" if ups else src.get("explode", ""), path)


def read_fields(table: dict, columns: list[dict]) -> set[tuple[str, str, str]]:
    """(source, level, field) triples the given columns of a table read, over every source of the table."""
    out: set[tuple[str, str, str]] = set()
    for src in table["sources"]:
        for c in columns:
            for p in source_paths(c):
                n = normalise_path(p, src)
                if n:
                    out.add((source_key(src), *n))
    return out
