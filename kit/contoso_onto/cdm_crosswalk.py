"""Official-name alias crosswalk: the repo's CDM application profile -> the official Microsoft CDM reference entities.

Silver carries the repo's CDM application profile as its base (profile attribute names, data/banking-model/v1/profile.json).
Where a profile attribute genuinely matches an attribute of the official Microsoft CDM entity that the profile names in its
cdmReference, silver also carries a second column named exactly like the official attribute: an *official-name alias*.
The profile calls the official entities semantic references only. An alias is a reviewed statement that two attributes mean
the same thing; it is never a claim of CDM conformance (nor of BIAN certification). Say "official-name alias" and
"semantic reference" in every catalog, doc and ontology text.

model/cdm_crosswalk.yaml is the reviewed match list. Each profile entity has one entry (official_entity must equal the
profile's cdmReference.entityName) holding its attribute matches, and a top-level `skipped:` section accounts for every
official attribute that is not aliased, by category. tests/test_cdm_crosswalk.py checks the file against the vendored official
entity files (vendor/cdm/references, sha256-pinned in sources.json). A match needs semantic equality, not similar words, and
"no match" is a normal outcome: the vendored Contact defines only a GUID and CustomDocument only a loan-application document
category and a description, so Customer and Document carry no alias.

Public API: Alias, load_crosswalk, official_index, validate, apply_alias. No network, no cache, no global state.

Closed vocabularies (validate() rejects anything else)

  match - how the two attributes relate
    exact                the two descriptions state the same concept, scope and unit; the value is carried unchanged
    semantic             the same concept, worded differently or at a wider scope on the official side (the rationale says
                         how); the value is carried unchanged
    derived_major_units  the official attribute is a `currency` (decimal, major units) derived exactly from the profile's
                         int64 minor (paise) value
    type_narrowed        the same concept, but the profile type is narrower than the official one (a plain date for
                         dateTime); silver keeps the narrow type and type_note says what is not carried

  transform - what apply_alias() does to the base silver value (the profile column's value)
    identity                the base value unchanged (string, int64, bool, timestamp_utc; exact decimal text stays text)
    minor_to_major_decimal  int64 paise -> Decimal rupees, exactly (value / 100, two decimal places, sign kept); the
                            profile's money exponent is 2
    date_keep               a date stays a date (never a datetime): no time of day and no timezone are invented

  silver_type - the alias column's type
    string | int64 | bool | date32 | timestamp_utc | decimal(38,4)
    decimal(38,4) is only for official `currency` aliases (int64 paise / 100 needs 17 integral digits). An official
    `decimal` is carried as the profile's canonical decimal text (string), because the profile defines rates as exact
    decimal text: no float and no fixed-scale rounding is introduced. Either representation difference needs a type_note.

Skipped categories in the YAML (every official attribute is aliased or skipped exactly once)
    guid_or_lookup | system_column | option_set | converted_currency_twin | no_counterpart
"""

from __future__ import annotations

import datetime as dt
import json
import operator
import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

__all__ = ["Alias", "apply_alias", "load_crosswalk", "official_index", "validate"]

_KIT = Path(__file__).resolve().parent.parent
_DEFAULT_PATH = _KIT / "model" / "cdm_crosswalk.yaml"
_REFERENCES = ("data", "banking-model", "v1", "vendor", "cdm", "references")

_MATCHES = ("exact", "semantic", "derived_major_units", "type_narrowed")
_TRANSFORMS = ("identity", "minor_to_major_decimal", "date_keep")
_SILVER_TYPES = (
    "string",
    "int64",
    "bool",
    "date32",
    "timestamp_utc",
    "decimal(38,4)",
)
_CURRENCY_SILVER_TYPE = "decimal(38,4)"

# profile attribute type -> the silver type of the base column (design doc section 4.2)
_BASE_SILVER = {
    "string": "string",
    "integer": "int64",
    "boolean": "bool",
    "date": "date32",
    "datetime": "timestamp_utc",
    "minor": "int64",
    "decimal": "string",
}
# identity aliases: official type -> the only silver type that keeps the base value unchanged
_IDENTITY_SILVER = {
    "string": "string",
    "integer": "int64",
    "boolean": "bool",
    "dateTime": "timestamp_utc",
    "decimal": "string",
}
# official type -> the silver type that needs no type_note
_PLAIN_SILVER = {
    "string": "string",
    "integer": "int64",
    "boolean": "bool",
    "dateTime": "timestamp_utc",
    "currency": _CURRENCY_SILVER_TYPE,
}
# official types that are never alias targets: row GUIDs and lookups, option sets
_NON_TARGET_TYPES = frozenset({"entityId", "customer", "listLookup"})
_SYSTEM_COLUMNS = frozenset(
    {
        "createdOn",
        "modifiedOn",
        "statecode",
        "statuscode",
        "importSequenceNumber",
        "overriddenCreatedOn",
        "timeZoneRuleVersionNumber",
        "UTCConversionTimeZoneCode",
    }
)
# converted-currency companions of a currency attribute: *_Base, *_default, *Default, *Exchangerate, *DefaultDisplayValue
_TWIN_RE = re.compile(r"(_base|_default|default|exchangerate|defaultdisplayvalue)$", re.IGNORECASE)
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_MONEY_EXPONENT = 2  # the profile's money contract: INR paise

_ENTRY_REQUIRED = ("profile_entity", "official_entity", "attributes")
_ENTRY_OPTIONAL = ("note",)
_ATTR_REQUIRED = (
    "profile",
    "official",
    "official_type",
    "match",
    "transform",
    "silver_type",
    "null_rule",
    "rationale",
    "evidence",
)
_ATTR_OPTIONAL = ("unit", "type_note")


@dataclass(frozen=True)
class Alias:
    """One reviewed official-name alias: silver carries `official_attribute` next to the profile column."""

    profile_entity: str
    profile_attribute: str
    official_entity: str
    official_attribute: str
    official_type: str  # the official CDM data type name exactly as in the vendored file
    match: str  # exact | semantic | derived_major_units | type_narrowed
    transform: str  # identity | minor_to_major_decimal | date_keep
    silver_type: str  # string | int64 | bool | date32 | timestamp_utc | decimal(38,4)
    rationale: str
    evidence: str
    unit: str | None
    null_rule: str
    type_note: str | None


# --------------------------------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------------------------------
def _check_keys(raw: Any, required: tuple[str, ...], optional: tuple[str, ...], where: str) -> None:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: expected a mapping, got {type(raw).__name__}")
    missing = [k for k in required if k not in raw]
    unknown = sorted(set(raw) - set(required) - set(optional))
    if missing or unknown:
        raise ValueError(f"{where}: missing keys {missing}, unknown keys {unknown}")


def load_crosswalk(path: Path | None = None) -> dict[str, list[Alias]]:
    """Read the reviewed match list (default model/cdm_crosswalk.yaml) as {profile entity: [Alias, ...]}.

    Entities reviewed without a match are present with an empty list. The file's order is kept. Structural problems
    (missing or unknown keys, a repeated entity) raise ValueError; use validate() for the content checks."""
    src = Path(path) if path is not None else _DEFAULT_PATH
    return _parse(yaml.safe_load(src.read_text(encoding="utf-8")), str(src))


def _parse(doc: Any, source: str) -> dict[str, list[Alias]]:
    entries = doc.get("crosswalk") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{source}: expected a top-level 'crosswalk' list")
    out: dict[str, list[Alias]] = {}
    for entry in entries:
        _check_keys(entry, _ENTRY_REQUIRED, _ENTRY_OPTIONAL, f"{source}: crosswalk entry")
        entity, official = entry["profile_entity"], entry["official_entity"]
        if entity in out:
            raise ValueError(f"{source}: profile entity {entity} appears twice")
        aliases = []
        for raw in entry["attributes"] or []:
            where = f"{source}: {entity}.{raw.get('profile') if isinstance(raw, dict) else raw}"
            _check_keys(raw, _ATTR_REQUIRED, _ATTR_OPTIONAL, where)
            aliases.append(
                Alias(
                    profile_entity=entity,
                    profile_attribute=raw["profile"],
                    official_entity=official,
                    official_attribute=raw["official"],
                    official_type=raw["official_type"],
                    match=raw["match"],
                    transform=raw["transform"],
                    silver_type=raw["silver_type"],
                    rationale=raw["rationale"],
                    evidence=raw["evidence"],
                    unit=raw.get("unit"),
                    null_rule=raw["null_rule"],
                    type_note=raw.get("type_note"),
                )
            )
        out[entity] = aliases
    return out


def _members(node: Any, where: str) -> Iterator[dict]:
    """The attribute definitions under a hasAttributes node, with inline attribute groups resolved."""
    if isinstance(node, list):
        for item in node:
            yield from _members(item, where)
    elif isinstance(node, dict) and "attributeGroupReference" in node:
        group = node["attributeGroupReference"]
        if not isinstance(group, dict):
            raise ValueError(f"{where}: attribute group {group!r} is referenced, not defined inline")
        yield from _members(group.get("members", []), where)
    elif isinstance(node, dict) and "name" in node and "dataType" in node:
        yield node
    else:
        shape = sorted(node) if isinstance(node, dict) else type(node).__name__
        raise ValueError(f"{where}: unsupported attribute shape {shape}")


def _type_name(data_type: Any, where: str) -> str:
    if isinstance(data_type, str):
        return data_type
    if isinstance(data_type, dict) and isinstance(data_type.get("dataTypeReference"), str):
        return data_type["dataTypeReference"]  # an option set: listLookup with its traits
    raise ValueError(f"{where}: unsupported dataType {data_type!r}")


def official_index(repo_root: Path) -> dict[str, dict[str, str]]:
    """Official entity -> {attribute: official data type} for every file in vendor/cdm/references (read only).

    The type is the name used in the vendored file (currency, dateTime, string, integer, decimal, boolean, entityId,
    listLookup, ...). Attribute groups are resolved; files are read in name order, attributes in file order."""
    refs = Path(repo_root).joinpath(*_REFERENCES)
    if not refs.is_dir():
        raise FileNotFoundError(f"no official CDM reference files at {refs}")
    index: dict[str, dict[str, str]] = {}
    for file in sorted(refs.glob("*.cdm.json")):
        doc = json.loads(file.read_text(encoding="utf-8-sig"))
        for definition in doc.get("definitions", []):
            if "entityName" not in definition:
                continue  # constant entities (option-set values) are not entities of the model
            name = definition["entityName"]
            where = f"{file.name}:{name}"
            if name in index:
                raise ValueError(f"{where}: entity defined twice")
            attributes: dict[str, str] = {}
            for member in _members(definition.get("hasAttributes", []), where):
                if member["name"] in attributes:
                    raise ValueError(f"{where}: attribute {member['name']} defined twice")
                attributes[member["name"]] = _type_name(member["dataType"], f"{where}.{member['name']}")
            index[name] = attributes
    return index


# --------------------------------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------------------------------
def _shape_errors(a: Alias, profile_type: str | None, exponent: Any) -> list[str]:
    """Consistency of match, transform, types and the base column; the vocabularies are already known to be valid."""
    out: list[str] = []
    base = _BASE_SILVER.get(profile_type or "")
    if a.transform == "identity":
        if a.match not in ("exact", "semantic"):
            out.append("identity keeps the value unchanged, so match must be exact or semantic")
        if a.silver_type != _IDENTITY_SILVER.get(a.official_type):
            out.append(f"an identity alias of an official {a.official_type} cannot have silver_type {a.silver_type}")
        if base is not None and a.silver_type != base:
            out.append(f"identity needs the alias type to equal the base column type {base}, not {a.silver_type}")
    elif a.transform == "minor_to_major_decimal":
        if a.match != "derived_major_units":
            out.append("minor_to_major_decimal is the derived_major_units transform")
        if profile_type is not None and profile_type != "minor":
            out.append(f"minor_to_major_decimal needs a minor profile attribute, not {profile_type}")
        if a.official_type != "currency" or a.silver_type != _CURRENCY_SILVER_TYPE:
            out.append(f"minor_to_major_decimal makes a currency stored as {_CURRENCY_SILVER_TYPE}")
        if exponent != _MONEY_EXPONENT:
            out.append(f"the profile's money exponent is {exponent!r}; the transform divides by 10**{_MONEY_EXPONENT}")
    elif a.transform == "date_keep":
        if a.match != "type_narrowed":
            out.append("date_keep is the type_narrowed transform")
        if profile_type is not None and profile_type != "date":
            out.append(f"date_keep needs a date profile attribute, not {profile_type}")
        if a.official_type != "dateTime" or a.silver_type != "date32":
            out.append("date_keep keeps a date (date32) under an official dateTime")
    if a.match == "derived_major_units" and a.transform != "minor_to_major_decimal":
        out.append("derived_major_units needs the minor_to_major_decimal transform")
    if a.match == "type_narrowed" and a.transform != "date_keep":
        out.append("type_narrowed needs the date_keep transform")
    if a.silver_type.startswith("decimal") and a.official_type != "currency":
        out.append("decimal silver types are only for official currency attributes")
    if a.official_type == "currency" and a.transform != "minor_to_major_decimal":
        out.append("an official currency is only reachable through minor_to_major_decimal")
    if a.silver_type != _PLAIN_SILVER.get(a.official_type) and not (a.type_note or "").strip():
        out.append(f"silver_type {a.silver_type} differs from the plain silver type of an official {a.official_type}: type_note is required")
    return out


def _alias_errors(
    a: Alias,
    reference: str | None,
    profile_attrs: dict[str, dict],
    reserved: set[str],
    official: dict[str, dict[str, str]],
    exponent: Any,
) -> list[str]:
    where = f"{a.profile_entity}.{a.profile_attribute} -> {a.official_entity}.{a.official_attribute}"
    out: list[str] = []
    if reference is not None and a.official_entity != reference:
        out.append(f"official entity must be the profile's cdmReference.entityName {reference}")
    profile_attr = profile_attrs.get(a.profile_attribute)
    if profile_attr is None:
        out.append("the profile entity has no such attribute")
    entity_attrs = official.get(a.official_entity)
    actual_type = None
    if entity_attrs is None:
        out.append(f"official entity {a.official_entity} is not in the official index")
    elif a.official_attribute not in entity_attrs:
        out.append(f"{a.official_entity} defines no attribute {a.official_attribute}")
    else:
        actual_type = entity_attrs[a.official_attribute]
        if actual_type != a.official_type:
            out.append(f"official_type {a.official_type} but the vendored file says {actual_type}")
    if {a.official_type, actual_type} & _NON_TARGET_TYPES:
        out.append("GUIDs, lookups and option sets (entityId, customer, listLookup) are never aliased")
    name = str(a.official_attribute)  # a YAML value may not be text; report it instead of crashing
    if name in _SYSTEM_COLUMNS:
        out.append("Dataverse system columns are never aliased")
    if _TWIN_RE.search(name):
        out.append("converted-currency companions (_Base, _default, exchange rates) are never aliased")
    if not _NAME_RE.match(name):
        out.append("not a valid silver column name (letters, digits, underscore; starts with a letter)")
    elif name.lower() in reserved:
        out.append("collides, ignoring case, with a profile or common column of the same silver table")
    for field in ("rationale", "evidence", "unit", "null_rule"):
        value = getattr(a, field)
        if not isinstance(value, str) or not value.strip():
            out.append(f"{field} must be a non-empty text")
    vocab_ok = True
    for field, allowed in (("match", _MATCHES), ("transform", _TRANSFORMS), ("silver_type", _SILVER_TYPES)):
        if getattr(a, field) not in allowed:
            out.append(f"{field} {getattr(a, field)!r} is not one of {', '.join(allowed)}")
            vocab_ok = False
    if vocab_ok:
        profile_type = profile_attr.get("type") if profile_attr else None
        out.extend(_shape_errors(a, profile_type, exponent))
    return [f"{where}: {message}" for message in out]


def validate(crosswalk: dict, index: dict, profile: dict) -> list[str]:
    """Human-readable problems with a crosswalk (as load_crosswalk returns it); [] when it is valid.

    `index` is official_index(repo_root) and `profile` the parsed data/banking-model/v1/profile.json. Checks: every
    official attribute exists in its vendored entity with the declared type; the official entity is the profile's
    cdmReference; no GUID, lookup, option set, converted-currency companion or system column is targeted; no profile
    attribute is mapped twice and no official attribute is targeted twice per entity; the alias name is a valid silver
    column name that does not collide with a profile or common column; rationale, evidence, unit and null_rule are
    present; match, transform and silver_type are in the closed vocabularies and consistent with each other and with
    the base profile attribute (decimals only for currency, type_note where the representation differs)."""
    errors: list[str] = []
    entities = {e.get("name"): e for e in profile.get("entityDefinitions", [])}
    common = {str(c).lower() for c in profile.get("commonEntityColumns", [])}
    exponent = (profile.get("money") or {}).get("exponent")
    for entity, aliases in crosswalk.items():
        definition = entities.get(entity)
        if definition is None:
            errors.append(f"{entity}: not an entity of the profile")
            continue
        reference = (definition.get("cdmReference") or {}).get("entityName")
        if reference is None and aliases:
            errors.append(f"{entity}: the profile entity has no cdmReference, so it cannot carry official aliases")
        profile_attrs = {a["name"]: a for a in definition.get("attributes", [])}
        reserved = {name.lower() for name in profile_attrs} | common
        profile_seen: dict[str, str] = {}
        official_seen: dict[str, str] = {}
        for alias in aliases:
            if alias.profile_entity != entity:
                errors.append(f"{entity}: holds an alias that names profile entity {alias.profile_entity}")
            errors.extend(_alias_errors(alias, reference, profile_attrs, reserved, index, exponent))
            if alias.profile_attribute in profile_seen:
                errors.append(f"{entity}.{alias.profile_attribute}: mapped twice ({profile_seen[alias.profile_attribute]} and {alias.official_attribute})")
            profile_seen.setdefault(alias.profile_attribute, alias.official_attribute)
            target = str(alias.official_attribute).lower()
            if target in official_seen:
                errors.append(f"{entity}: official attribute {alias.official_attribute} is targeted twice ({official_seen[target]} and {alias.profile_attribute})")
            official_seen.setdefault(target, alias.profile_attribute)
    return errors


# --------------------------------------------------------------------------------------------------
# applying an alias to a base value
# --------------------------------------------------------------------------------------------------
def _as_int64(value: Any, what: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{what} must be an integer, not bool")
    try:
        number = operator.index(value)
    except TypeError:
        raise TypeError(f"{what} must be an integer, not {type(value).__name__}") from None
    if not _INT64_MIN <= number <= _INT64_MAX:
        raise ValueError(f"{what} {number} is outside the signed 64-bit range")
    return number


def _minor_to_major(value: Any) -> Decimal:
    number = _as_int64(value, "a minor amount")
    digits = tuple(int(c) for c in str(abs(number)))
    # built from the digits, so the result is exact whatever the active decimal context says
    return Decimal((1 if number < 0 else 0, digits, -_MONEY_EXPONENT))


def _identity(alias: Alias, value: Any) -> Any:
    kind = alias.silver_type
    if kind == "string":
        ok = isinstance(value, str)
    elif kind == "int64":
        return _as_int64(value, "an int64 value")
    elif kind == "bool":
        ok = isinstance(value, bool)
    elif kind == "timestamp_utc":
        ok = isinstance(value, dt.datetime) and value.tzinfo is not None
    else:
        raise ValueError(f"an identity alias cannot have silver_type {kind}")
    if not ok:
        raise TypeError(f"{alias.profile_entity}.{alias.profile_attribute}: a {kind} value is required, got {type(value).__name__}")
    return value


def _date_keep(alias: Alias, value: Any) -> dt.date:
    if isinstance(value, dt.datetime) or not isinstance(value, dt.date):
        raise TypeError(f"{alias.profile_entity}.{alias.profile_attribute}: a plain date (no time, no timezone) is required, got {type(value).__name__}")
    return value


def apply_alias(alias: Alias, base_value: Any) -> Any:
    """The alias column's value for the profile column's value `base_value`; None stays None.

    minor_to_major_decimal: an int64 paise value (not bool, not float) -> the exact Decimal rupees, two decimal places,
    sign kept. date_keep: a datetime.date stays that date (a datetime is refused: no time or timezone is invented).
    identity: the value unchanged after a type check against alias.silver_type. Missing values must be passed as None
    (not NaN or pandas NA). ValueError for an unknown transform or an out-of-range int64, TypeError for a wrong type."""
    if base_value is None:
        return None
    if alias.transform == "identity":
        return _identity(alias, base_value)
    if alias.transform == "minor_to_major_decimal":
        return _minor_to_major(base_value)
    if alias.transform == "date_keep":
        return _date_keep(alias, base_value)
    raise ValueError(f"{alias.profile_entity}.{alias.profile_attribute}: unknown transform {alias.transform!r}")
