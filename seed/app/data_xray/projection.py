"""Pure, owner-first adapters over one captured banking-session snapshot.

Nothing here resolves document bytes, initializes relationship state, reprices a
quote, or synchronizes CRM. References are joined only after the authorized
records have been collected; a missing endpoint is a diagnostic, never a node.
"""

from __future__ import annotations

import heapq
import re
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.agentic.bank import BankError, product_requires_decision
from app.banking import SeedData, session_view
from app.chat.tools import redact_contact_context
from app.data_xray.contracts import (
    BIAN_RELEASE, ENTITY_FAMILIES, ENTITY_FIELDS, MAX_EDGES,
    MAX_NODES, PAGE_SIZE, PROJECTION_VERSION, Edge, InaccessibleNode,
    InvalidQuery, Projection, ProjectionError, Record, SnapshotMeta,
    SnapshotQuery, Source, StaleScope, concrete_row, decimal_string, entity_columns,
    digest, identifier, integer, major_to_minor, minor_string, timestamp, validate_concrete_rows,
)
from app.frontier.contracts import (
    CalculationOutput,
    ConsentRecord as FrontierConsentRecord,
    FrontierJourneyState,
    HouseholdAccessGrant,
    HumanDecision,
    HumanHandoff,
    MandateActionRecord,
    OutcomeRecord,
    PreparedAction,
    ProactiveCandidate,
    RegisteredAgent,
    SurplusToGoalMandate,
)
from app.frontier.state import frontier_view

_HEALTH_TEXT = re.compile(
    r"\b(?:diagnos(?:is|ed|tic)|medication|tobacco|diabet(?:es|ic)|cancer|"
    r"hiv|lupus|psychiatric|pregnan(?:t|cy)|declaredConditions|declaredHealth|"
    r"(?:medical|health)\s+(?:history|narrative|declaration|conditions?)|"
    r"ongoingMedication|condition\s*count|hospital\s+admissions?|"
    r"hospitalizations?|current\s+episode|clinical\s+(?:findings?|history))\b", re.I,
)
_PRIVATE_TEXT = re.compile(
    r"(?:[A-Za-z]:[\\/]|(?:^|\s)/(?:var|home|tmp|etc)/|Bearer\s+\S+|"
    r"(?:password|api[_ -]?key|csrf[_ -]?token|authorization)\s*[:=])", re.I,
)
_FINANCIAL_LABEL = re.compile(
    r"\b(?:amount|balance|costs?|price|fee|premium|cover|insured|deductible|"
    r"limit|income|salary|pension|payment|paid|contribution|reserve|cash|"
    r"budget|deposit|principal|interest|rate|instalment|installment|emi|"
    r"tenor|term|months?|expense|debt|obligation|shortfall|share|total|"
    r"allocation|settlement|copay|co-pay|sub-limit|refund|valuation|"
    r"spend|savings?|net|gross|proceeds|rent|estimates?)\b", re.I,
)
_FACT_TEXT_LABELS = frozenset({
    "currency", "payment frequency", "payout preference", "payout mode",
    "employment status", "income frequency", "account type", "product reference",
})
_INACTIVE = frozenset({"closed", "matured", "renewed", "superseded", "cancelled", "expired", "withdrawn"})
_PRIMARY = {entity: next(iter(fields)) for entity, fields in ENTITY_FIELDS.items()}
_HOLDINGS = ("Account", "Card", "Deposit", "Loan", "ProtectionPolicy")
_REFERENCE_TYPES = (
    *_HOLDINGS, "Goal", "Document", "Fact", "Transaction", "Application",
    "BankDecision", "Consent", "ExecutionReceipt", "ServiceCase", "Commitment",
    "SweepRule", "CollateralPledge", "Allocation", "Settlement", "Product",
    "PartyRelationship", "LifecycleEvent", "Calculation",
    "FrontierJourney", "ProactiveCandidate", "HouseholdAccessGrant",
    "FrontierConsent", "RegisteredAgent", "FrontierMandate", "PreparedAction",
    "MandateAction", "HumanHandoff", "HumanDecision", "FrontierOutcome",
    "AgentAction",
)
_FAMILY_ORDER = {
    value: index for index, value in enumerate((
        "customers", "holdings", "goals", "planning", "authority",
        "servicing", "evidence", "products", "money", "frontier",
    ))
}
_REVIEW_FIELDS = {
    "customer": "text", "applicationId": "text", "product": "text", "productId": "text",
    "productVersion": "text", "operation": "text", "sourceAccountId": "text",
    "amountMinor": "money_minor", "termMonths": "months", "payout": "text",
    "holdingId": "text", "targetId": "text", "option": "text",
    "protectedCashMinor": "money_minor", "monthlyBudgetMinor": "money_minor",
    "debitMinor": "money_minor", "monthlyCommitmentMinor": "money_minor",
    "feeMinor": "money_minor", "quoteExpiresAt": "text", "bankDecision": "text", "consent": "text",
}
_QUOTE_FIELDS = {
    "feeMinor": "money_minor", "debitMinor": "money_minor", "monthlyPremiumMinor": "money_minor",
    "sumInsuredMinor": "money_minor", "deductibleMinor": "money_minor", "principalMinor": "money_minor",
    "monthlyPaymentMinor": "money_minor", "monthlyCommitmentMinor": "money_minor",
    "downPaymentMinor": "money_minor", "closingCostsMinor": "money_minor", "processingFeeMinor": "money_minor",
    "purchasePriceMinor": "money_minor", "thresholdMinor": "money_minor", "multipleMinor": "money_minor",
    "ceilingMinor": "money_minor", "protectedCashMinor": "money_minor", "maximumAmountMinor": "money_minor",
    "ratePct": "percentage", "coPayPct": "percentage", "termMonths": "months",
    "rateType": "text", "maturesOn": "text", "payout": "text", "renewsOn": "text",
    "nextDueOn": "text", "futureCoverOnly": "boolean", "claimsApproved": "boolean",
    "collateralDepositId": "text", "serviceType": "text", "subjectId": "text",
    "mandateOption": "text", "initialWaitingDays": "number", "preExistingWaitingMonths": "months",
}
_LEGACY_FIELDS = {
    "amount", "principal", "depositAmount", "monthlyAmount", "requestedAmount",
    "requestedLimit", "creditLimit", "monthlyIncome", "monthlyNetIncome", "loanAmount",
    "termMonths", "termYears", "tenureMonths", "sourceAccountId", "accountId",
    "cardId", "depositId", "policyId", "productId", "payout", "payoutMode",
    "employmentStatus", "fundingAccountId", "sumInsured", "premium", "deductible",
}


def _dict(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ProjectionError("Malformed source object.")
    return value


def _list(value: Any) -> list:
    if not isinstance(value, list):
        raise ProjectionError("Malformed source collection.")
    return value


def _optional_dict(value: Any) -> dict:
    return {} if value is None else _dict(value)


def _version(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is int:
        return str(integer(value, minimum=0))
    return identifier(value)


def _pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _hash(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ProjectionError("Malformed source digest.")
    return value


def _scope(snapshot: dict, owner_id: str) -> dict:
    identifier(owner_id)
    if snapshot.get("activeOwnerId") != owner_id:
        raise StaleScope()
    owner = _dict(_dict(snapshot.get("owners")).get(owner_id))
    identifier(snapshot.get("scenarioGeneration"))
    identifier(snapshot.get("sessionId"))
    integer(snapshot.get("revision"), minimum=0)
    timestamp(snapshot.get("updatedAt"))
    return owner


def build_meta(
    *, snapshot: dict, owner_id: str, seed: SeedData, metadata: Any,
    build_fingerprint: str, package_fingerprint: str | None = None,
    observed_at: str | None = None,
) -> SnapshotMeta:
    owner = _scope(snapshot, owner_id)
    release_id = seed.release.get("releaseId")
    if snapshot.get("seedReleaseId") not in (None, release_id) or owner.get("hydratedFrom") not in (None, release_id):
        raise ProjectionError("Snapshot and approved reference releases disagree.")
    view = session_view(seed, snapshot, owner_id)
    observed = observed_at or datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    timestamp(observed)
    values = {
        "ownerId": owner_id, "scenarioGeneration": snapshot["scenarioGeneration"],
        "revision": snapshot["revision"], "updatedAt": snapshot["updatedAt"],
        "businessAsOf": view.business_date.isoformat(),
        "seedReleaseId": identifier(snapshot.get("seedReleaseId") or view.release.get("releaseId")),
        "buildFingerprint": _hash(build_fingerprint),
        "packageFingerprint": _hash(package_fingerprint),
        "projectionVersion": PROJECTION_VERSION,
        "profileVersion": identifier(metadata.profile_version),
        "mappingVersion": identifier(metadata.mapping_version),
        "metadataFingerprint": _hash(metadata.fingerprint), "bianRelease": BIAN_RELEASE,
    }
    if not values["buildFingerprint"] or not values["metadataFingerprint"]:
        raise ProjectionError("Source version identity is unavailable.")
    identity = digest({
        "scope": digest([snapshot["sessionId"], owner_id]),
        "ownerContent": digest(owner), **values,
    })
    return SnapshotMeta(snapshotId=f"bxs-{identity}", observedAt=observed, **values)


class _Builder:
    def __init__(self, snapshot: dict, owner_id: str, seed: SeedData, metadata: Any, meta: SnapshotMeta):
        self.owner = _scope(snapshot, owner_id)
        self.owner_id = owner_id
        self.generation = snapshot["scenarioGeneration"]
        self.seed = session_view(seed, snapshot, owner_id)
        self.metadata = metadata
        self.meta = meta
        self.scope = digest([snapshot["sessionId"], owner_id, self.generation])
        self.secrets = {
            value for value in _optional_dict(snapshot.get("auth")).values()
            if isinstance(value, str) and len(value) >= 12
        } | {snapshot["sessionId"]}
        self.nodes: dict[str, dict] = {}
        self.edges: dict[str, Edge] = {}
        self.index: dict[tuple[str, str], list[str]] = {}
        self.mirrors: dict[tuple[str, str], str] = {}
        self.links: list[dict] = []
        self.warnings: set[str] = set()
        self.documents: dict[str, str] = {}
        self.readings: dict[str, tuple[dict, str, bool]] = {}
        self.facts: dict[str, list[tuple[dict, str, bool]]] = {}
        self.operations: dict[str, dict] = {}
        self.application_operations: dict[str, str] = {}
        self.current_applications: dict[str, str] = {}
        self.receipt_effects: dict[str, list[str]] = {}
        self.customer: str | None = None
        self.definitions = metadata.entities
        for entity in ENTITY_FIELDS:
            entity_columns(entity, self.definitions)

    def text(self, value: Any, *, health: bool = True) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 16000:
            raise ProjectionError("Malformed source text.")
        if (
            _PRIVATE_TEXT.search(value) or any(secret in value for secret in self.secrets)
            or (health and _HEALTH_TEXT.search(value))
        ):
            self.warnings.add("Restricted personal or internal source text was withheld.")
            return "[redacted]"
        return redact_contact_context(value)

    def texts(self, value: Any) -> str | None:
        if value is None:
            return None
        return "; ".join(self.text(item) or "" for item in _list(value))

    def scoped(self, row: Any, *, explicit: bool = False) -> bool:
        row = _dict(row)
        if explicit and "ownerId" not in row:
            raise ProjectionError("An owner-scoped record has no owner.")
        return (
            row.get("ownerId", self.owner_id) == self.owner_id
            and row.get("scenarioGeneration", self.generation) == self.generation
        )

    def rows(self, container: dict, name: str, *, explicit: bool = True):
        for index, row in enumerate(_list(container.get(name, []))):
            if self.scoped(row, explicit=explicit):
                yield index, row

    def identity(self, value: Any) -> str:
        result = identifier(value)
        if result in self.secrets:
            raise ProjectionError("A credential cannot be a record identity.")
        return result

    def mirror(self, kind: str, key: str, row: dict) -> bool:
        fingerprint = digest(row)
        previous = self.mirrors.get((kind, key))
        if previous is not None and previous != fingerprint:
            raise ProjectionError("Conflicting copies of the same retained record.")
        self.mirrors[(kind, key)] = fingerprint
        return previous is not None

    def money(self, row: dict, mapping: dict[str, str], *, direct: bool = False, currency: str | None = None) -> dict:
        result: dict[str, Any] = {}
        currencies = set()
        declared = row.get("currency")
        if declared is None:
            declared = currency
        if declared is not None:
            currencies.add(self.identity(declared))
        for target, source in mapping.items():
            value = row.get(source)
            if value is None:
                result[target] = None
                continue
            if direct:
                result[target] = minor_string(value)
            else:
                value = _dict(value)
                result[target] = minor_string(value.get("amountMinor"))
                currencies.add(self.identity(value.get("currency")))
        if len(currencies) > 1 or any(not re.fullmatch(r"[A-Z]{3}", item) for item in currencies):
            raise ProjectionError("Inconsistent monetary currency.")
        if direct and any(value is not None for value in result.values()) and not currencies:
            raise ProjectionError("A monetary source needs its declared currency.")
        result["currency"] = next(iter(currencies), None)
        return result

    def attrs(self, entity: str, row: dict, names: tuple[str, ...] | list[str]) -> dict:
        result = {}
        for name in names:
            value = row.get(name)
            kind = ENTITY_FIELDS[entity][name]
            if value is not None:
                if kind == "minor":
                    value = minor_string(value)
                elif kind == "decimal":
                    value = decimal_string(value)
                elif kind == "string":
                    value = self.text(value)
            result[name] = value
        return result

    def joined(self, values: Any) -> str | None:
        if values is None:
            return None
        return "; ".join(
            item for item in (self.text(value) for value in _list(values)) if item
        )

    def put(
        self, entity: str, key: Any, attributes: dict, *, dataset: str, path: str,
        record_id: str | None = None, version: Any = None, label: str | None = None,
        state: str = "recorded", source_kind: str = "bank_record",
        historical: bool = False, redacted: tuple[str, ...] = (), warnings: tuple[str, ...] = (),
        customer_link: bool = True,
    ) -> str:
        primary = attributes.get(_PRIMARY[entity])
        business_id = self.identity(record_id or primary)
        opaque = "bx-" + digest([self.scope, entity, key])[:40]
        attrs = {name: attributes.get(name) for name in ENTITY_FIELDS[entity]}
        if set(attributes) - ENTITY_FIELDS[entity].keys():
            raise ProjectionError("An adapter supplied an unregistered attribute.")
        safe_label = self.text(label or business_id) or business_id
        safe_state = self.text(state)
        if not safe_state:
            raise ProjectionError("A source lifecycle is missing.")
        record = {
            "id": opaque, "entityType": entity, "label": safe_label, "family": ENTITY_FAMILIES[entity],
            "state": safe_state, "sourceKind": source_kind, "historical": historical,
            "attributes": attrs,
            "source": Source(dataset=dataset, recordId=business_id, path=path, version=_version(version)).model_dump(),
            "definitionRef": f"banking.cdm.json/{entity}",
            "domainIds": list(self.metadata.domain_ids(entity, attrs)),
            "redactedFields": list(redacted), "warnings": list(warnings),
        }
        previous = self.nodes.get(opaque)
        if previous:
            if any(previous[field] != record[field] for field in ("entityType", "attributes", "state", "label")):
                raise ProjectionError("Conflicting projected records share an identity.")
            if previous["historical"] and not historical:
                self.nodes[opaque] = record
            return opaque
        self.nodes[opaque] = record
        if primary is not None:
            self.index.setdefault((entity, self.identity(primary)), []).append(opaque)
        if customer_link and self.customer and entity != "Product":
            self.edge(
                self.customer, opaque, "customer_record",
                "The record is contained in this customer's authorized source scope.", record["source"],
            )
        return opaque

    def edge(self, start: str, end: str, relation: str, reason: str, source: dict) -> None:
        if start not in self.nodes or end not in self.nodes:
            raise ProjectionError("A projected relationship has no endpoint.")
        edge_id = "bxe-" + digest([self.scope, start, end, relation, source["dataset"], source["recordId"]])[:40]
        self.edges[edge_id] = Edge(
            id=edge_id, from_id=start, to=end, relation=relation,
            label=relation.replace("_", " "), reason=reason, source=Source(**source),
        )

    def ref(
        self, node: str, value: Any, types: tuple[str, ...] = _REFERENCE_TYPES,
        *, relation: str = "references", field: str | None = None,
        reason: str = "The retained source explicitly names this record.",
        reverse: bool = False, match: dict | None = None,
    ) -> None:
        if value is None or value == "":
            return
        self.links.append({
            "node": node, "value": self.identity(value), "types": types, "relation": relation,
            "field": field, "reason": reason, "reverse": reverse, "match": match or {},
        })

    def refs(
        self,
        node: str,
        values: Any,
        *,
        relation: str = "references",
        field: str | None = None,
    ) -> None:
        if values is None:
            return
        for value in _list(values):
            self.ref(node, value, relation=relation, field=field)

    def resolve(self, value: str, types: tuple[str, ...], match: dict | None = None) -> str | None:
        matches = [
            node for entity in types for node in self.index.get((entity, value), [])
            if all(self.nodes[node]["attributes"].get(key) == item for key, item in (match or {}).items())
        ]
        current = [node for node in matches if not self.nodes[node]["historical"]]
        choices = current or matches
        return choices[0] if len(choices) == 1 else None

    def finish_links(self) -> None:
        for link in self.links:
            target = self.resolve(link["value"], link["types"], link["match"])
            source_node = self.nodes[link["node"]]
            if target is None:
                warning = "An explicit reference is unavailable, restricted or not version-resolvable in this snapshot."
                self.warnings.add(warning)
                if warning not in source_node["warnings"]:
                    source_node["warnings"].append(warning)
                field = link["field"]
                if field and field in source_node["attributes"]:
                    source_node["attributes"][field] = None
                    if field not in source_node["redactedFields"]:
                        source_node["redactedFields"].append(field)
                continue
            start, end = (target, link["node"]) if link["reverse"] else (link["node"], target)
            self.edge(start, end, link["relation"], link["reason"], source_node["source"])

    def bank_records(self) -> None:
        persona = self.seed.persona(self.owner_id)
        if not persona or persona.get("ownerId") != self.owner_id:
            raise ProjectionError("The selected customer has no approved source record.")
        self.customer = self.put(
            "Customer", self.owner_id, self.attrs("Customer", persona, ["ownerId", "displayName", "city", "cohort"]),
            dataset="seed.personas", path=f"/personas/{_pointer(self.owner_id)}",
            label=persona.get("displayName"), state="customer", customer_link=False,
        )
        for index, row in enumerate(_list(self.seed.data.get("products"))):
            row = _dict(row)
            key = self.identity(row.get("productId"))
            self.mirror("Product", key, row)
            parameters = _dict(row.get("parameters"))
            try:
                decision_required = product_requires_decision(row)
            except BankError as exc:
                raise ProjectionError("A catalogue product has no supported decision policy.") from exc
            maximum_months = parameters.get("maximumTermMonths")
            if maximum_months is None and parameters.get("maximumTenureYears") is not None:
                maximum_months = integer(
                    integer(parameters["maximumTenureYears"], minimum=0) * 12, minimum=0,
                )
            if maximum_months is not None:
                maximum_months = integer(maximum_months, minimum=0)
            values = self.attrs("Product", row, ["productId", "productVersion"])
            values.update(
                productFamily=self.text(row.get("family")), name=self.text(row.get("displayName")),
                currency=self.text(parameters.get("currency") or "INR"),
                requiresDecision=decision_required,
                ratePct=decimal_string(parameters["interestRatePct"]) if parameters.get("interestRatePct") is not None else None,
                minimumTermMonths=parameters.get("minimumTermMonths"), maximumTermMonths=maximum_months,
            )
            for target, source in {
                "annualFeeMinor": "annualFee", "monthlyFeeMinor": "monthlyFee",
                "monthlyPremiumMinor": "monthlyPremium", "sumInsuredMinor": "sumInsured",
                "minimumPrincipalMinor": "minimumPrincipal", "maximumPrincipalMinor": "maximumPrincipal",
            }.items():
                values[target] = major_to_minor(parameters[source]) if parameters.get(source) is not None else None
            self.put(
                "Product", key, values, dataset="catalogue.products", path=f"/products/{index}",
                version=row.get("productVersion"), label=row.get("displayName"),
                state=row.get("status", "catalogue"), source_kind="catalogue",
            )

        specs = (
            ("accounts", "Account", "accountId", ["productId", "openedOn", "version"],
             {"postedBalanceMinor": "postedBalance", "availableBalanceMinor": "availableBalance", "holdsMinor": "holds"}),
            ("cards", "Card", "cardId", ["productId", "linkedAccountId", "cardType", "maskedNumber", "expiry", "version"],
             {"limitMinor": "creditLimit", "outstandingMinor": "currentOutstanding", "availableCreditMinor": "availableCredit", "prepaidBalanceMinor": "prepaidBalance"}),
            ("deposits", "Deposit", "depositId", ["productId", "linkedAccountId", "depositType", "openedOn", "maturesOn", "payoutMode", "version"],
             {"principalMinor": "principal", "monthlyInstalmentMinor": "monthlyInstalment"}),
            ("liabilities", "Loan", "liabilityId", ["productId", "linkedAccountId", "ratePct", "termMonths", "openedOn", "maturesOn", "version"],
             {"principalMinor": "principal", "outstandingMinor": "outstanding"}),
            ("policies", "ProtectionPolicy", "policyId", ["productId", "linkedAccountId", "coverType", "premiumFrequency", "startedOn", "renewsOn", "futureCoverOnly", "claimsApproved", "version"],
             {"premiumMinor": "premium", "sumInsuredMinor": "sumInsured", "deductibleMinor": "deductible"}),
            ("transactions", "Transaction", "transactionId", ["accountId", "cardId", "applicationId", "direction", "category", "description", "valueDate", "postedAt", "recordedAt", "transferId"],
             {"amountMinor": "amount"}),
            ("goals", "Goal", "goalId", ["title", "targetDate"], {"targetAmountMinor": "targetAmount", "allocatedAmountMinor": "allocatedAmount"}),
        )
        for dataset, entity, key_field, names, amounts in specs:
            sources = [(self.seed.data, dataset, False)]
            if entity == "Deposit":
                sources.append((self.owner, "closedDeposits", True))
            for container, name, closed in sources:
                for index, row in self.rows(container, name):
                    key = self.identity(row.get(key_field))
                    self.mirror(entity, key, row)
                    linked_currency = None
                    if entity == "Card" and row.get("linkedAccountId"):
                        account = self.resolve(row["linkedAccountId"], ("Account",))
                        if account:
                            linked_currency = self.nodes[account]["attributes"]["currency"]
                    values = {
                        key_field: key, **self.attrs(entity, row, names),
                        **self.money(row, amounts, currency=linked_currency),
                    }
                    if entity == "Deposit":
                        rate = row.get("interestRatePct", row.get("ratePct"))
                        values["ratePct"] = decimal_string(rate) if rate is not None else None
                        if values["depositType"] is None:
                            product = self.resolve(values["productId"], ("Product",)) if values["productId"] else None
                            family = self.nodes[product]["attributes"]["productFamily"] if product else None
                            values["depositType"] = {"fixed_deposit": "fixed", "recurring_deposit": "recurring"}.get(family)
                    if entity == "Loan":
                        value = row.get("monthlyPaymentMinor")
                        values["monthlyPaymentMinor"] = minor_string(value) if value is not None else None
                    if entity == "Goal":
                        values["priority"] = self.text(row.get("statedPriority"))
                    state = row.get("status", "recorded")
                    historical = closed or state in _INACTIVE
                    path = f"/owners/{self.owner_id}/{name}/{index}"
                    node = self.put(
                        entity, key, values, dataset=f"banking.{name}", path=path, version=row.get("version"),
                        label=row.get("title") if entity == "Goal" else key,
                        state=state, historical=historical, source_kind="history" if historical else "bank_record",
                    )
                    for field, types, relation in (
                        ("productId", ("Product",), "uses_product"),
                        ("linkedAccountId", ("Account",), "linked_account"),
                        ("accountId", ("Account",), "posted_to"),
                        ("cardId", ("Card",), "card_entry"),
                        ("applicationId", ("Application",), "application_effect"),
                    ):
                        if field in values and field != key_field:
                            self.ref(node, values[field], types, relation=relation, field=field)
                    for ref in ("originApplicationId", "lastApplicationId", "lastControlApplicationId", "dispositionApplicationId"):
                        self.ref(node, row.get(ref), ("Application",), relation="application_effect")
                    self.refs(node, row.get("sourceRefs"))
                    self.refs(node, row.get("linkedSourceIds"))
                    self.refs(node, row.get("sourceDocumentIds"))
                    if entity == "Deposit":
                        self.ref(node, row.get("renewedFromDepositId"), ("Deposit",), relation="renewed_from")
                        self.ref(node, row.get("renewedIntoId"), ("Deposit",), relation="renewed_into")
                        self.pledge(row, node, path, values["currency"])
                    elif entity == "Loan":
                        self.repayments(row, node, path, values["currency"])
                    elif entity == "Goal":
                        self.allocations(row, node, path, values["currency"])

        for index, row in self.rows(self.seed.data, "preferences"):
            key = self.identity(row.get("preferenceId"))
            self.mirror("Preference", key, row)
            node = self.put(
                "Preference", key, self.attrs("Preference", row, list(ENTITY_FIELDS["Preference"])),
                dataset="banking.preferences", path=f"/owners/{self.owner_id}/preferences/{index}",
                label=row.get("field"), state=row.get("status", "recorded"), source_kind="customer_stated",
            )
            self.ref(node, row.get("statedIn"), ("Document",), relation="stated_source")
        self.money_records()

    def pledge(self, row: dict, deposit_node: str, path: str, currency: str | None) -> None:
        pledge = row.get("pledge")
        if pledge is None:
            return
        pledge = _dict(pledge)
        if not self.scoped(pledge):
            return
        facility = pledge.get("cardId") or pledge.get("liabilityId")
        if not facility:
            raise ProjectionError("A pledge has no retained facility identity.")
        key = f"pledge-{digest([row['depositId'], facility, pledge.get('applicationId')])[:24]}"
        values = {
            "pledgeId": key, "depositId": row["depositId"], "facilityId": self.identity(facility),
            **self.attrs("CollateralPledge", pledge, ["applicationId", "recordedAt"]),
            **self.money(pledge, {"amountMinor": "amountMinor"}, direct=True, currency=currency),
        }
        node = self.put(
            "CollateralPledge", key, values, dataset="banking.deposits", path=path + "/pledge",
            state=pledge.get("status", "recorded"), historical=pledge.get("status") in _INACTIVE,
        )
        self.edge(node, deposit_node, "pledges_deposit", "The holding contains this explicit pledge.", self.nodes[node]["source"])
        self.ref(node, facility, ("Card", "Loan"), relation="secures_facility", field="facilityId")
        self.ref(node, pledge.get("applicationId"), ("Application",), relation="application_effect", field="applicationId")

    def repayments(self, row: dict, loan_node: str, path: str, currency: str | None) -> None:
        for index, payment in self.rows(row, "repaymentSchedule", explicit=False):
            number = integer(payment.get("instalment"), minimum=1)
            key = f"{row['liabilityId']}:payment:{number}"
            self.mirror("Commitment", key, payment)
            values = {
                "commitmentId": key, "applicationId": row.get("applicationId"),
                "sourceAccountId": row.get("linkedAccountId"), "targetId": row["liabilityId"],
                "kind": "scheduled_loan_repayment", "nextDueOn": payment.get("dueOn"),
                "version": row.get("version"),
                **self.money(payment, {"amountMinor": "paymentMinor"}, direct=True, currency=currency),
            }
            node = self.put(
                "Commitment", key, values, dataset="banking.liabilities", path=f"{path}/repaymentSchedule/{index}",
                state=payment.get("status", "scheduled"), historical=payment.get("status") in _INACTIVE,
            )
            self.edge(node, loan_node, "scheduled_for", "This is a retained schedule row, not a posted payment.", self.nodes[node]["source"])
            self.ref(node, values["sourceAccountId"], ("Account",), relation="funded_by", field="sourceAccountId")
            self.ref(node, values["applicationId"], ("Application",), relation="application_effect", field="applicationId")

    def allocations(self, row: dict, goal_node: str, path: str, currency: str | None) -> None:
        for index, allocation in self.rows(row, "allocations", explicit=False):
            key = self.identity(allocation.get("allocationId"))
            self.mirror("Allocation", key, allocation)
            values = {
                "allocationId": key, "goalId": row["goalId"],
                **self.attrs("Allocation", allocation, ["sourceAccountId", "applicationId", "recordedAt"]),
                **self.money(allocation, {"amountMinor": "amountMinor"}, direct=True, currency=currency or "INR"),
            }
            node = self.put(
                "Allocation", key, values, dataset="banking.goals", path=f"{path}/allocations/{index}",
                state=allocation.get("status", "recorded"),
            )
            self.edge(node, goal_node, "allocated_to", "The goal retains this actual allocation.", self.nodes[node]["source"])
            self.ref(node, values["sourceAccountId"], ("Account",), relation="holds_funds_in", field="sourceAccountId")
            self.ref(node, values["applicationId"], ("Application",), relation="application_effect", field="applicationId")

    def money_records(self) -> None:
        for name, entity, key_field in (
            ("mandates", "Commitment", "mandateId"),
            ("sweepRules", "SweepRule", "sweepRuleId"),
            ("purchaseSettlements", "Settlement", "settlementId"),
            ("sharedSettlements", "Settlement", "settlementId"),
        ):
            for index, row in self.rows(self.owner, name):
                key = self.identity(row.get(key_field))
                self.mirror(entity, key, row)
                values = self.attrs(entity, row, [
                    field for field, kind in ENTITY_FIELDS[entity].items()
                    if kind != "minor" and field not in {"commitmentId", "currency"}
                ])
                amounts = {field: field for field, kind in ENTITY_FIELDS[entity].items() if kind == "minor"}
                if entity == "Commitment":
                    values["commitmentId"] = key
                    amounts["amountMinor"] = "monthlyAmountMinor"
                if entity == "Settlement":
                    values["kind"] = "purchase" if name == "purchaseSettlements" else "shared"
                values.update(self.money(row, amounts, direct=True, currency="INR"))
                state = row.get("status", "recorded")
                node = self.put(
                    entity, key, values, dataset=f"banking.{name}", path=f"/owners/{self.owner_id}/{name}/{index}",
                    state=state, historical=state in _INACTIVE, version=row.get("version"),
                )
                for field, types, relation in (
                    ("applicationId", ("Application",), "application_effect"),
                    ("sourceAccountId", ("Account",), "funded_by"),
                    ("productId", ("Product",), "uses_product"),
                    ("targetId", _HOLDINGS, "scheduled_for"),
                    ("liabilityId", ("Loan",), "loan_settlement"),
                    ("recipientId", ("PartyRelationship",), "settles_with"),
                ):
                    if field in values:
                        self.ref(node, values[field], types, relation=relation, field=field)
                self.refs(node, row.get("sourceRefs"))
                self.refs(node, row.get("transactionIds"), relation="posting")
                self.ref(node, row.get("transactionId"), ("Transaction",), relation="posting")
                self.ref(node, row.get("ledgerEvidenceId"), ("Document",), relation="settlement_source")
                self.ref(node, row.get("recipientEvidenceId"), ("Document",), relation="recipient_source")

    def document(
        self, row: dict, path: str, dataset: str, *, catalogue: bool = False,
        retained: bool = False, historical: bool = False,
    ) -> str:
        key = self.identity(row.get("documentId") or row.get("sourceId"))
        version = _version(row.get("sourceVersion", row.get("version")))
        assets = _list(row.get("assets", []))
        asset = next((item for item in assets if _dict(item).get("mimeType") == "application/pdf"), None)
        if asset is None and assets:
            asset = _dict(assets[0])
        asset = asset or {}
        content_type = row.get("contentType") or asset.get("mimeType") or ("text/plain" if "sourceId" in row else None)
        page_count = row.get("pageCount")
        basis = "retained_read_metadata" if retained and page_count is not None else "declared_metadata" if page_count is not None else None
        if asset.get("pages") is not None:
            page_count = integer(asset["pages"], minimum=1)
            basis = "retained_asset_metadata"
        if page_count is not None:
            integer(page_count, minimum=1)
        source_hash = _hash(row.get("sourceSha256") or row.get("sha256") or asset.get("sha256"))
        values = {
            "documentId": key, "title": self.text(row.get("title")) or "Source document",
            "documentKind": self.text(row.get("documentKind") or row.get("kind")),
            "contentType": self.text(content_type), "sourceVersion": version, "sourceSha256": source_hash,
            "pageCount": page_count, "pageCountBasis": basis,
            "issuedOn": row.get("issuedOn") or row.get("asOf"),
            "previewable": not historical and content_type in {"application/pdf", "image/png", "image/jpeg"}
            and (bool(assets) or dataset == "banking.uploads"),
        }
        node = self.put(
            "Document", [key, version], values, dataset=dataset, path=path, record_id=key, version=version,
            label=values["title"], state="retained_source" if historical else "available",
            source_kind="catalogue" if catalogue else "document_metadata", historical=historical,
            redacted=("contents", "authoring_metadata"), customer_link=not catalogue,
        )
        if not historical:
            previous = self.documents.get(key)
            if previous and previous != node:
                raise ProjectionError("Ambiguous current document versions.")
            self.documents[key] = node
        return node

    def document_records(self) -> None:
        for name in ("documents", "uploads"):
            container = self.seed.data if name == "documents" else self.owner
            for index, row in self.rows(container, name):
                if name == "documents":
                    visible = row.get("inCustomerDocumentSet", True)
                    if type(visible) is not bool:
                        raise ProjectionError("Invalid document visibility.")
                    if not visible:
                        continue
                key = self.identity(row.get("documentId"))
                self.mirror("available_document", key, row)
                self.document(row, f"/owners/{self.owner_id}/{name}/{index}", f"banking.{name}")
        events = {
            self.identity(row.get("eventId"))
            for _, row in self.rows(self.owner, "relationshipEvents")
        }
        for key, row in _dict(self.owner.get("relationshipSources", {})).items():
            if not self.scoped(row, explicit=True):
                continue
            if row.get("sourceId") != key:
                raise ProjectionError("Invalid applied source identity.")
            event = _optional_dict(row.get("provenance")).get("eventId")
            if event not in events:
                self.warnings.add("An unapplied source was excluded; only committed owner events are inspectable.")
                continue
            self.document(row, f"/owners/{self.owner_id}/relationshipSources/{_pointer(key)}", "banking.relationshipSources")
        for product_index, product in enumerate(self.seed.data["products"]):
            for index, row in enumerate(_list(product.get("sourceDocuments", []))):
                if not self.scoped(row):
                    continue
                node = self.document(row, f"/products/{product_index}/sourceDocuments/{index}", "catalogue.sources", catalogue=True)
                self.ref(node, product["productId"], ("Product",), relation="terms_for")
        for name in ("partyRelationships", "recipients"):
            for index, row in self.rows(self.owner, name):
                key = self.identity(row.get("relationshipId") or row.get("recipientId"))
                values = {
                    "relationshipId": key, "customerId": self.owner_id,
                    "relatedPartyRef": self.text(row.get("relatedPartyRef") or row.get("recipientPartyId")),
                    "role": self.text(row.get("role")) or "registered_recipient",
                    "evidenceId": self.text(row.get("evidenceId")),
                }
                node = self.put("PartyRelationship", key, values, dataset=f"banking.{name}",
                                path=f"/owners/{self.owner_id}/{name}/{index}", state=row.get("status", "recorded"))
                self.ref(node, values["evidenceId"], ("Document",), relation="relationship_source", field="evidenceId")
        for index, row in self.rows(self.owner, "relationshipEvidence"):
            if row.get("kind") != "verified_recipient":
                continue
            source_id = row.get("sourceId") or row.get("documentId")
            if source_id not in self.documents:
                continue
            key = self.identity(row.get("recipientId"))
            values = {
                "relationshipId": key, "customerId": self.owner_id,
                "relatedPartyRef": self.text(row.get("recipientPartyId")),
                "role": "registered_recipient", "evidenceId": self.identity(row.get("evidenceId")),
            }
            node = self.put(
                "PartyRelationship", key, values, dataset="banking.relationshipEvidence",
                path=f"/owners/{self.owner_id}/relationshipEvidence/{index}", state=row.get("status", "recorded"),
            )
            self.ref(
                node, source_id, ("Document",), relation="relationship_source",
                reason="The applied recipient evidence explicitly identifies this source document.",
            )

    def evidence_records(self, relationship: dict) -> None:
        evidence = relationship.get("evidence")
        if evidence is None:
            return
        evidence = _dict(evidence)
        if not self.scoped(evidence, explicit=True):
            raise ProjectionError("Evidence belongs to a different scope.")
        revision = integer(evidence.get("revision"), minimum=0)
        request = _optional_dict(evidence.get("request"))
        selected: dict[str, tuple[dict, str, bool]] = {}
        history = _list(evidence.get("history", []))
        for index, descriptor in self.rows(evidence, "selected", explicit=False):
            key = self.identity(descriptor.get("documentId"))
            self.mirror("selected_document", key, descriptor)
            doc_node = self.documents.get(key)
            if not doc_node:
                self.warnings.add("A retained selection has no currently available authorized document.")
                continue
            doc = self.nodes[doc_node]
            version = _version(descriptor.get("sourceVersion"))
            source_hash = _hash(descriptor.get("sourceSha256"))
            current = request.get("status") == "selected" and all(
                doc["attributes"][field] in (None, value)
                for field, value in (("sourceVersion", version), ("sourceSha256", source_hash))
            )
            if current:
                for field, value in (("sourceVersion", version), ("sourceSha256", source_hash)):
                    if doc["attributes"][field] is None:
                        doc["attributes"][field] = value
                if doc["attributes"]["pageCount"] is None and descriptor.get("pageCount") is not None:
                    doc["attributes"]["pageCount"] = integer(descriptor["pageCount"], minimum=1)
                    doc["attributes"]["pageCountBasis"] = "retained_selection_metadata"
            selected_at = descriptor.get("selectedAt")
            for event in reversed(history):
                event = _dict(event)
                if event.get("type") == "source_selection" and any(
                    _dict(item).get("documentId") == key for item in _list(event.get("selected", []))
                ):
                    selected_at = event.get("at")
                    break
            selection_id = f"selection-{digest([request.get('requestId'), key, version, source_hash])[:24]}"
            node = self.put(
                "EvidenceSelection", selection_id,
                {"selectionId": selection_id, "documentId": key, "purpose": self.text(request.get("purpose")),
                 "selectedAt": selected_at, "revision": revision},
                dataset="relationship.evidence", path=f"/owners/{self.owner_id}/relationship/evidence/selected/{index}",
                state="selected" if current else "stale_selection", source_kind="consent",
                warnings=() if current else ("The selected source no longer matches the current available metadata.",),
            )
            self.edge(node, doc_node, "selects_document", "This descriptor is explicitly retained in the source selection.", self.nodes[node]["source"])
            selected[key] = (descriptor, node, current)

        def reading(raw: dict, path: str, historical: bool) -> None:
            if not self.scoped(raw):
                return
            key = self.identity(raw.get("readId"))
            doc_id = self.identity(raw.get("documentId"))
            self.mirror("EvidenceReading", key, raw)
            descriptor, selection_node, chosen = selected.get(doc_id, ({}, None, False))
            source_hash = _hash(raw.get("sourceSha256"))
            version = _version(raw.get("sourceVersion"))
            available = self.documents.get(doc_id)
            if available:
                document = self.nodes[available]["attributes"]
                if document["sourceSha256"] in (None, source_hash) and document["sourceVersion"] in (None, version):
                    if document["sourceVersion"] is None:
                        document["sourceVersion"] = version
                    if document["sourceSha256"] is None:
                        document["sourceSha256"] = source_hash
                    if document["pageCount"] is None and raw.get("pageCount") is not None:
                        document["pageCount"] = integer(raw["pageCount"], minimum=1)
                        document["pageCountBasis"] = "retained_read_metadata"
            authorized = (
                not historical and chosen and raw.get("status") == "read" and raw.get("truncated", False) is False
                and source_hash is not None and descriptor.get("sourceSha256") == source_hash
                and _version(descriptor.get("sourceVersion")) == version
            )
            values = {
                "readId": key, "documentId": doc_id, "sourceVersion": version, "sourceSha256": source_hash,
                **self.attrs("EvidenceReading", raw, ["pageCount", "extractionMethod", "extractionVersion", "readAt", "reusedExtractionReadId"]),
                "error": "The retained reading failed; explicitly retry through the document workflow." if raw.get("error") else None,
            }
            node = self.put(
                "EvidenceReading", key, values, dataset="relationship.evidence", path=path,
                version=version, label=f"Reading {key}", state=raw.get("status", "recorded"),
                source_kind="history" if historical else "document_read", historical=historical,
                redacted=("pages", "fields", "provider_metadata"),
                warnings=() if authorized else ("Extracted contents are withheld because this is not a current selected successful reading.",),
            )
            if authorized and selection_node:
                self.edge(selection_node, node, "permits_reading", "This successful retained reading matches the selected source hash and version.", self.nodes[node]["source"])
            self.ref(node, doc_id, ("Document",), relation="read_from", field="documentId",
                     match={"sourceVersion": version, "sourceSha256": source_hash})
            self.ref(node, raw.get("reusedExtractionReadId"), ("EvidenceReading",), relation="reuses_extraction", field="reusedExtractionReadId")
            previous = self.readings.get(key)
            if previous is None or authorized:
                self.readings[key] = (raw, node, authorized)

        for key, raw in _dict(evidence.get("readings", {})).items():
            raw = _dict(raw)
            if raw.get("documentId") != key:
                raise ProjectionError("Inconsistent retained reading identity.")
            reading(raw, f"/owners/{self.owner_id}/relationship/evidence/readings/{_pointer(key)}", False)
        for index, event in enumerate(history):
            event = _dict(event)
            if not self.scoped(event):
                continue
            if event.get("type") == "superseded_read":
                reading(_dict(event.get("read")), f"/owners/{self.owner_id}/relationship/evidence/history/{index}/read", True)
            elif event.get("type") == "source_selection":
                for selection_index, descriptor in enumerate(_list(event.get("selected", []))):
                    descriptor = _dict(descriptor)
                    if not self.scoped(descriptor):
                        continue
                    key = self.identity(descriptor.get("documentId"))
                    if key in selected and selected[key][0] == descriptor:
                        continue
                    selection_id = f"selection-{digest([event.get('requestId'), event.get('at'), key, descriptor.get('sourceVersion')])[:24]}"
                    node = self.put(
                        "EvidenceSelection", selection_id,
                        {"selectionId": selection_id, "documentId": key, "purpose": self.text(event.get("purpose")),
                         "selectedAt": event.get("at"), "revision": event.get("revision")},
                        dataset="relationship.evidence", path=f"/owners/{self.owner_id}/relationship/evidence/history/{index}/selected/{selection_index}",
                        state="historical_selection", source_kind="history", historical=True,
                    )
                    self.ref(node, key, ("Document",), relation="selected_source", field="documentId",
                             match={"sourceVersion": _version(descriptor.get("sourceVersion"))})
        for key, raw in _dict(evidence.get("facts", {})).items():
            raw = _dict(raw)
            if raw.get("factId") != key:
                raise ProjectionError("Inconsistent retained fact identity.")
            self.fact(raw, f"/owners/{self.owner_id}/relationship/evidence/facts/{_pointer(key)}", historical=False)
        for index, event in enumerate(history):
            event = _dict(event)
            if event.get("type") in {"field_correction", "input_changed"} and self.scoped(event):
                raw = _dict(event.get("previous"))
                self.fact(raw, f"/owners/{self.owner_id}/relationship/evidence/history/{index}/previous", historical=True)
        for versions in self.facts.values():
            ordered = sorted(versions, key=lambda value: value[0].get("revision", 0))
            for previous, current in zip(ordered, ordered[1:]):
                if current[0]["revision"] != previous[0]["revision"] + 1:
                    self.warnings.add("Intermediate fact revisions were not retained; no direct correction link was invented.")
                    continue
                self.edge(
                    previous[1], current[1], "corrected_to",
                    "Retained fact revisions explicitly preserve the prior value; the original reading was not overwritten.",
                    self.nodes[current[1]]["source"],
                )

    def fact(self, raw: dict, path: str, *, historical: bool) -> None:
        if not self.scoped(raw):
            return
        key = self.identity(raw.get("factId"))
        revision = integer(raw.get("revision"), minimum=1)
        self.mirror("Fact", f"{key}:{revision}", raw)
        origin = raw.get("origin")
        if origin not in {"user_stated", "document_extracted", "customer_corrected"}:
            self.warnings.add("A fact without a supported explicit origin was excluded.")
            return
        source = _optional_dict(raw.get("source"))
        read_id = source.get("readId") or next((item for item in self.readings if key.startswith(item + ":")), None)
        read = self.readings.get(read_id)
        authorized = origin == "user_stated" or bool(
            read and read[2] and source.get("documentId") == read[0].get("documentId")
            and source.get("sourceSha256") == read[0].get("sourceSha256")
        )
        if not authorized and not historical:
            self.warnings.add("Unselected, unread or stale-source facts were excluded from the current projection.")
            return
        label = raw.get("label")
        if not isinstance(label, str):
            raise ProjectionError("A fact label is malformed.")
        numeric = raw.get("unit") in {"money_major", "money_minor", "INR", "inr", "percentage", "months", "number"}
        private_label = bool(_HEALTH_TEXT.search(label) or _PRIVATE_TEXT.search(label))
        monetary = raw.get("unit") in {"money_major", "money_minor", "INR", "inr"}
        permitted = (
            (not private_label or monetary) and
            (bool(_FINANCIAL_LABEL.search(label)) if numeric else label.casefold() in _FACT_TEXT_LABELS)
        )
        if not permitted:
            self.warnings.add("Non-financial or unrestricted-narrative evidence fields were withheld.")
            return
        value = raw.get("value")
        if value is not None and not isinstance(value, str):
            raise ProjectionError("A retained fact value must be explicitly typed text.")
        if value is not None and numeric:
            self.typed_value(value, raw["unit"])
        values = {
            "factId": key, "label": "Permitted monetary fact" if private_label else self.text(label) if authorized else "Retained financial fact",
            "value": self.text(value) if authorized else None, "unit": self.text(raw.get("unit")),
            "currency": self.text(raw.get("currency")), "origin": origin, "revision": revision,
            "requiresConfirmation": raw.get("requiresConfirmation"),
            "confidence": decimal_string(raw["confidence"]) if raw.get("confidence") is not None else None,
            "readId": read_id, "documentId": source.get("documentId"),
            "page": source.get("page"), "regionId": self.text(source.get("regionId")),
            "sourceSha256": _hash(source.get("sourceSha256")),
        }
        if values["page"] is not None:
            integer(values["page"], minimum=1)
            if not read or not read[0].get("pageCount") or values["page"] > read[0]["pageCount"]:
                raise ProjectionError("A fact references a page outside its retained reading.")
        if values["confidence"] is not None and not Decimal(0) <= Decimal(values["confidence"]) <= Decimal(1):
            raise ProjectionError("A confidence value is out of range.")
        node = self.put(
            "Fact", [key, revision], values, dataset="relationship.evidence", path=path,
            version=revision, label=values["label"], state="retained_revision" if historical else "current",
            source_kind="history" if historical else {
                "user_stated": "customer_stated", "customer_corrected": "customer_corrected",
                "document_extracted": "document_read",
            }[origin], historical=historical,
            redacted=("label",) if authorized and private_label else () if authorized else ("value", "label"),
        )
        versions = self.facts.setdefault(key, [])
        if not any(item[1] == node for item in versions):
            versions.append((raw, node, authorized))
        if read:
            self.edge(read[1], node, "supports_fact", "The retained fact identifies this reading and source hash.", self.nodes[node]["source"])
        elif origin != "user_stated":
            self.warnings.add("A retained fact revision has no resolvable reading; no provenance was invented.")
        if values["documentId"]:
            self.ref(node, values["documentId"], ("Document",), relation="fact_document", field="documentId",
                     match={"sourceSha256": values["sourceSha256"]})

    @staticmethod
    def typed_value(value: Any, unit: str) -> str:
        if unit in {"money_major", "INR", "inr"}:
            if not isinstance(value, (str, int, float, Decimal)) or isinstance(value, bool):
                raise ProjectionError("Invalid major-unit value.")
            cleaned = re.sub(r"^(?:INR|Rs\.?|₹)\s*", "", str(value).strip(), flags=re.I)
            if not re.fullmatch(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{2,3})+)(?:\.[0-9]+)?", cleaned):
                raise ProjectionError("Ambiguous typed monetary value.")
            return major_to_minor(cleaned.replace(",", ""))
        if unit == "money_minor":
            return minor_string(value, transported=True)
        if unit == "percentage":
            return decimal_string(str(value).strip().removesuffix("%").strip())
        if unit in {"months", "number"}:
            return decimal_string(value)
        raise ProjectionError("Unsupported typed result unit.")

    def retained_blocks(self):
        # Only named banking blocks are inspected; message text, traces and tool
        # payloads are deliberately not traversed.
        for channel, turns in _dict(self.owner.get("history", {})).items():
            for index, turn in enumerate(_list(turns)):
                turn = _dict(turn)
                if turn.get("role") != "assistant" or not self.scoped(turn):
                    continue
                for block_index, block in enumerate(_list(turn.get("blocks", []))):
                    block = _dict(block)
                    if block.get("kind") in {"calculation_panel", "relationship_plan"}:
                        yield block, f"/owners/{self.owner_id}/history/{_pointer(channel)}/{index}/blocks/{block_index}"
        for key, envelope in _dict(self.owner.get("turnEnvelopes", {})).items():
            envelope = _dict(envelope)
            if not self.scoped(envelope):
                continue
            result = envelope.get("result")
            if not isinstance(result, dict):
                continue
            for index, block in enumerate(_list(result.get("blocks", []))):
                block = _dict(block)
                if block.get("kind") in {"calculation_panel", "relationship_plan"}:
                    yield block, f"/owners/{self.owner_id}/turnEnvelopes/{_pointer(key)}/result/blocks/{index}"

    def calculations(self, relationship: dict, blocks: list[tuple[dict, str]]) -> None:
        collected: dict[str, tuple[dict, dict | None, str, bool]] = {}
        for key, result in _dict(relationship.get("analyses", {})).items():
            result = _dict(result)
            block = _dict(result.get("block", result))
            if block.get("kind") != "calculation_panel":
                continue
            calc_id = self.identity(block.get("calculationId"))
            self.mirror("CalculationBlock", calc_id, block)
            typed = result.get("result")
            if typed is not None:
                typed = _dict(typed)
                self.mirror("CalculationResults", calc_id, typed)
            collected[calc_id] = (block, typed, f"/owners/{self.owner_id}/relationship/analyses/{_pointer(key)}", False)
        for block, path in blocks:
            if block.get("kind") != "calculation_panel":
                continue
            calc_id = self.identity(block.get("calculationId"))
            self.mirror("CalculationBlock", calc_id, block)
            collected.setdefault(calc_id, (block, None, path, True))
        for key, (block, typed, path, historical) in collected.items():
            sources = _list(block.get("sources", []))
            rows = _list(block.get("rows", []))
            node = self.put(
                "Calculation", key,
                {"calculationId": key, "title": self.text(block.get("title")), "resultCount": len(rows),
                 "inputCount": len(sources), "assumptions": self.texts(block.get("assumptions", []))},
                dataset="relationship.calculations", path=path, label=block.get("title"),
                state="persisted", source_kind="calculated", historical=historical,
            )
            content_allowed = True
            for source in sources:
                source = _dict(source)
                reference = source.get("ref")
                if not isinstance(reference, str):
                    raise ProjectionError("Calculation source reference is malformed.")
                target = None
                if reference.startswith("fact:"):
                    versions = self.facts.get(reference[5:], [])
                    matching = []
                    for fact, fact_node, allowed in versions:
                        try:
                            if self.calculation_source_matches(source, fact):
                                matching.append((fact_node, allowed))
                        except ProjectionError:
                            continue
                    if len(matching) == 1:
                        target, allowed = matching[0]
                        content_allowed = content_allowed and allowed
                    else:
                        content_allowed = content_allowed and source.get("origin") in {"user_stated", "bank_record", "catalogue"}
                else:
                    parts = reference.split(":")
                    if len(parts) == 3:
                        kind, source_id, field = parts
                        spec = {
                            "account": ("Account", {"availableBalance": "availableBalanceMinor", "postedBalance": "postedBalanceMinor"}),
                            "deposit": ("Deposit", {"principal": "principalMinor"}),
                            "product": ("Product", {
                                "interestRatePct": "ratePct", "annualFee": "annualFeeMinor",
                                "monthlyFee": "monthlyFeeMinor", "monthlyPremium": "monthlyPremiumMinor",
                                "sumInsured": "sumInsuredMinor", "minimumPrincipal": "minimumPrincipalMinor",
                                "maximumPrincipal": "maximumPrincipalMinor",
                            }),
                        }.get(kind)
                        if spec and field in spec[1]:
                            candidate = self.resolve(source_id, (spec[0],))
                            if candidate:
                                current = self.nodes[candidate]["attributes"].get(spec[1][field])
                                stored = self.source_number(source)
                                if current is not None and Decimal(current) == Decimal(stored):
                                    target = candidate
                if target:
                    self.edge(target, node, "calculation_input",
                              "The persisted operand reference and value match this retained source version.",
                              self.nodes[node]["source"])
                else:
                    self.warnings.add("A historical calculation input cannot be bound to a retained source version; no current fact was substituted.")
            for index, result_row in enumerate(rows):
                result_row = _dict(result_row)
                label = result_row.get("label")
                if not isinstance(label, str):
                    raise ProjectionError("Calculation result label is malformed.")
                result = _dict(typed[label]) if typed is not None and label in typed else None
                if result is not None:
                    unit = result.get("unit")
                    value = self.typed_value(result.get("value"), unit)
                    currency = result.get("currency")
                else:
                    display = result_row.get("value")
                    if not isinstance(display, str):
                        raise ProjectionError("Calculation result is missing its retained display value.")
                    if re.match(r"^(?:INR|Rs\.?|₹)\s", display):
                        value, unit, currency = self.typed_value(display, "money_major"), "money_minor", "INR"
                    elif display.endswith("%"):
                        value, unit, currency = self.typed_value(display, "percentage"), "percentage", None
                    elif re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", display):
                        value, unit, currency = decimal_string(display), "number", None
                    else:
                        self.warnings.add("A calculation display row without supported retained units was excluded.")
                        continue
                if unit in {"money_major", "INR", "inr"}:
                    unit = "money_minor"
                child = self.put(
                    "CalculationValue", [key, label],
                    {"calculationId": key, "field": self.text(label), "value": value if content_allowed else None,
                     "unit": unit, "currency": self.text(currency)},
                    dataset="relationship.calculations", path=f"{path}/rows/{index}", record_id=key,
                    label=label, state="persisted", source_kind="calculated", historical=historical,
                    redacted=() if content_allowed else ("value",),
                    warnings=() if content_allowed else ("The retained output is withheld because its document input is no longer authorized.",),
                )
                self.edge(node, child, "calculation_result", "This result row is persisted with the calculation; it was not recomputed.", self.nodes[child]["source"])

    def source_number(self, source: dict) -> str:
        value = source.get("value")
        unit = source.get("unit")
        if unit == "money_minor" and isinstance(value, str) and re.match(r"^(?:INR|Rs\.?|₹)\s", value):
            return self.typed_value(value, "money_major")
        return self.typed_value(value, unit)

    def calculation_source_matches(self, source: dict, fact: dict) -> bool:
        return (
            Decimal(self.source_number(source)) == Decimal(self.typed_value(fact.get("value"), fact.get("unit")))
            and source.get("origin") == fact.get("origin")
            and source.get("documentId") == _optional_dict(fact.get("source")).get("documentId")
            and source.get("page") == _optional_dict(fact.get("source")).get("page")
        )

    def plan_records(self, relationship: dict, blocks: list[tuple[dict, str]]) -> None:
        current = relationship.get("plan")
        if current is not None:
            self.plan(_dict(current), f"/owners/{self.owner_id}/relationship/plan", historical=False)
        for index, entry in enumerate(_list(relationship.get("archivedItems", []))):
            entry = _dict(entry)
            if not self.scoped(entry, explicit=True):
                continue
            self.item(
                _dict(entry.get("item")), f"/owners/{self.owner_id}/relationship/archivedItems/{index}/item",
                plan_id=self.identity(entry.get("planId")), plan_node=None, historical=True,
                recorded_at=entry.get("archivedAt"),
            )
        for block, path in blocks:
            if block.get("kind") == "relationship_plan":
                self.plan(_dict(block.get("plan")), path + "/plan", historical=True)
        for key, request in _dict(relationship.get("applications", {})).items():
            request = _dict(request)
            if not self.scoped(request):
                continue
            app_id = self.identity(request.get("applicationId"))
            result = _optional_dict(request.get("result"))
            if result.get("plan") is not None:
                self.plan(_dict(result["plan"]), f"/owners/{self.owner_id}/relationship/applications/{_pointer(key)}/result/plan", historical=True)
            if result.get("decision") is not None:
                self.decision(_dict(result["decision"]), f"/owners/{self.owner_id}/relationship/applications/{_pointer(key)}/result/decision")
            request_id = f"assessment-request-{digest([key, app_id, request.get('fingerprint')])[:24]}"
            node = self.put(
                "LifecycleEvent", request_id,
                {"eventId": request_id, "eventType": "independent_decision_request",
                 "planId": request.get("planId"), "itemId": request.get("itemId"),
                 "applicationId": app_id, "decisionId": _optional_dict(result.get("decision")).get("decisionId"),
                 "at": request.get("at"), "revision": None,
                 "note": "A retained assessment request is not a banking execution."},
                dataset="relationship.applications", path=f"/owners/{self.owner_id}/relationship/applications/{_pointer(key)}",
                state=request.get("status", "recorded"), source_kind="application",
            )
            self.ref(node, app_id, ("Application",), relation="assessment_requested_for", field="applicationId")
            self.ref(
                node, request.get("planId"), ("RelationshipPlan",), relation="plan_event",
                field="planId", match={"revision": None},
                reason="Only an explicitly retained plan revision can attest the request's terms.",
            )
            self.ref(node, _optional_dict(result.get("decision")).get("decisionId"), ("BankDecision",), relation="assessment_answer", field="decisionId")
        for index, raw in enumerate(_list(relationship.get("decisions", []))):
            self.decision(_dict(raw), f"/owners/{self.owner_id}/relationship/decisions/{index}")

    def plan(self, raw: dict, path: str, *, historical: bool) -> None:
        if not self.scoped(raw, explicit=True):
            return
        key = self.identity(raw.get("planId"))
        revision = integer(raw.get("revision"), minimum=1)
        self.mirror("RelationshipPlan", f"{key}:{revision}", raw)
        values = self.attrs(
            "RelationshipPlan", raw, ["planId", "channel", "revision", "protectedCashMinor", "monthlyBudgetMinor",
                                      "riskContext", "createdAt", "updatedAt", "catalogueFingerprint"],
        )
        values.update(goals=self.texts(raw.get("goals", [])), currency="INR")
        node = self.put(
            "RelationshipPlan", [key, revision], values, dataset="relationship.plans", path=path,
            version=revision, label=f"Relationship plan {key}", state="recorded_plan",
            source_kind="application", historical=historical,
        )
        items = _list(raw.get("items"))
        item_nodes = {}
        for index, item in enumerate(items):
            item = _dict(item)
            item_nodes[_dict(item.get("spec")).get("key")] = self.item(
                item, f"{path}/items/{index}", plan_id=key, plan_node=node, historical=historical,
                recorded_at=raw.get("updatedAt"),
            )
        for item in items:
            spec = item["spec"]
            start = item_nodes[spec.get("key")]
            for dependency in _list(spec.get("dependsOn", [])):
                target = item_nodes.get(dependency)
                if start and target:
                    self.edge(start, target, "depends_on", "The persisted item explicitly declares this separate prerequisite.", self.nodes[start]["source"])
                else:
                    self.warnings.add("A retained prerequisite has no in-scope plan item.")

    def item(
        self, raw: dict, path: str, *, plan_id: str, plan_node: str | None,
        historical: bool, recorded_at: str | None,
    ) -> str | None:
        if not self.scoped(raw):
            return None
        app_id = self.identity(raw.get("applicationId"))
        item_id = self.identity(raw.get("itemId"))
        spec = _dict(raw.get("spec"))
        quote = _optional_dict(raw.get("quote"))
        retained_version = raw.get("reviewFingerprint") or quote.get("quoteId") or digest(spec)
        key = [app_id, item_id, retained_version]
        # State can change after review while the same financial identity is
        # retained. Distinct retained states are not overwritten by a chat mirror.
        key.append(raw.get("state"))
        self.mirror("PlanItem", digest(key), raw)
        values = {
            "applicationId": app_id, "itemId": item_id, "planId": plan_id,
            **self.attrs("Application", spec, ["productId", "operation", "sourceAccountId", "holdingId",
                                              "targetId", "amountMinor", "termMonths", "payout"]),
            "currency": "INR", "benefit": self.text(spec.get("customerBenefit")),
            "reviewFingerprint": _hash(raw.get("reviewFingerprint")), "legacy": False,
        }
        operation = spec.get("operation")
        previous_operation = self.application_operations.get(app_id)
        if previous_operation is not None and previous_operation != operation:
            raise ProjectionError("An application identity has conflicting operations.")
        self.application_operations[app_id] = operation
        node = self.put(
            "Application", key, values, dataset="relationship.items", path=path, record_id=app_id,
            version=retained_version, label=quote.get("title") or operation or app_id,
            state=raw.get("state", "proposed"), source_kind="application", historical=historical,
            redacted=("unregistered_review_content",),
        )
        if not historical:
            previous = self.current_applications.get(app_id)
            if previous is not None and previous != node:
                raise ProjectionError("Two current items claim one application identity.")
            self.current_applications[app_id] = node
        if plan_node:
            self.edge(plan_node, node, "plan_item", "The persisted plan version contains this item.", self.nodes[node]["source"])
        else:
            self.ref(node, plan_id, ("RelationshipPlan",), relation="archived_from_plan")
        for field, types, relation in (
            ("productId", ("Product",), "proposes_product"),
            ("sourceAccountId", ("Account",), "proposed_funding"),
            ("holdingId", _HOLDINGS, "references_holding"),
            ("targetId", (*_HOLDINGS, "Goal", "PartyRelationship", "ServiceCase", "SweepRule", "Transaction"), "requested_target"),
        ):
            self.ref(node, values[field], types, relation=relation, field=field)
        self.refs(node, spec.get("evidenceIds"), relation="application_source")
        quote_node = self.quote(quote, app_id, path + "/quote", historical) if quote else None
        if quote_node:
            self.edge(quote_node, node, "quotes_application", "The item retains this exact quote; inspection does not refresh it.", self.nodes[quote_node]["source"])
        form = raw.get("applicationForm")
        if form is not None:
            self.review(
                _dict(form), node, quote_node, path + "/applicationForm",
                historical=historical, version=retained_version,
            )
        if raw.get("decision") is not None:
            decision_node = self.decision(_dict(raw["decision"]), path + "/decision", historical=historical)
            if decision_node:
                self.edge(decision_node, node, "decides_application", "This item retains the independent decision for its application.", self.nodes[decision_node]["source"])
        if raw.get("receipt") is not None:
            receipt_node = self.receipt(_dict(raw["receipt"]), path + "/receipt", historical=historical)
            if receipt_node:
                self.edge(node, receipt_node, "application_receipt", "This item retains the receipt, distinct from its proposal and assessment.", self.nodes[receipt_node]["source"])
        if not historical:
            self.continuity(app_id, plan_id, f"CASE-{app_id}", raw.get("state", "proposed"),
                            path, recorded_at, operation=operation)
        return node

    def quote(self, raw: dict, app_id: str, path: str, historical: bool) -> str:
        key = self.identity(raw.get("quoteId"))
        self.mirror("Quote", key, raw)
        values = self.attrs("Quote", raw, [
            "quoteId", "debitMinor", "monthlyCommitmentMinor", "maximumAmountMinor", "requiresDecision",
            "policyVersion", "catalogueFingerprint", "evidenceFingerprint", "expiresAt",
        ])
        values.update(applicationId=app_id, currency="INR")
        warnings = self.expiry(raw.get("expiresAt"), "quote")
        return self.put(
            "Quote", key, values, dataset="relationship.quotes", path=path,
            label=raw.get("title") or key, state="quoted", source_kind="application", historical=historical,
            redacted=("unregistered_terms",), warnings=warnings,
        )

    def expiry(self, expiry: Any, kind: str) -> tuple[str, ...]:
        if expiry is None:
            return ()
        value = datetime.fromisoformat(timestamp(expiry).replace("Z", "+00:00"))
        observed = datetime.fromisoformat(self.meta.observedAt.replace("Z", "+00:00"))
        return (f"The recorded {kind} is expired at observation time; inspection grants no new authority.",) if value <= observed else ()

    def review(
        self, form: dict, application: str, quote_node: str | None, path: str,
        *, historical: bool, version: str, legacy: bool = False,
    ) -> str:
        app = self.nodes[application]
        app_id = app["attributes"]["applicationId"]
        review_id = f"review-{digest([application, version])[:24]}"
        quote = self.nodes[quote_node]["attributes"] if quote_node else {}
        fields = _list(form.get("fields", []))
        missing = _list(form.get("missingFields", []))
        node = self.put(
            "ApplicationReview", review_id,
            {"reviewId": review_id, "applicationId": app_id, "quoteId": quote.get("quoteId"),
             "missingCount": len(missing), "requirements": self.texts(form.get("requirements", [])),
             "expiresAt": quote.get("expiresAt")},
            dataset="relationship.reviews" if not legacy else "legacy.reviews",
            path=path, version=version, label=form.get("title") or "Saved application review",
            state=form.get("status", app["state"]), source_kind="application", historical=historical,
        )
        self.edge(node, application, "reviews_application", "This is a retained form, not a newly built or repriced review.", self.nodes[node]["source"])
        if quote_node:
            self.edge(quote_node, node, "reviewed_quote", "The item stores this quote with the reviewed form.", self.nodes[node]["source"])
        seen_fields = set()
        for index, raw in enumerate(fields):
            raw = _dict(raw)
            key = raw.get("key")
            if not isinstance(key, str) or key in seen_fields:
                raise ProjectionError("Review fields have a missing or duplicate identity.")
            seen_fields.add(key)
            if key == "customerInputs":
                for fact in _list(raw.get("value") or []):
                    fact = _dict(fact)
                    matching = [
                        target for stored, target, allowed in self.facts.get(fact.get("factId"), [])
                        if allowed and stored.get("revision") == fact.get("revision") and stored == fact
                    ]
                    if len(matching) == 1:
                        self.edge(matching[0], node, "review_input", "The form retains this exact fact and revision.", self.nodes[node]["source"])
                    else:
                        self.warnings.add("A saved review input is restricted or has no matching retained revision.")
                continue
            if key == "sourceDocuments":
                for descriptor in _list(raw.get("value") or []):
                    descriptor = _dict(descriptor)
                    self.ref(node, descriptor.get("documentId"), ("Document",), relation="review_source",
                             match={"sourceVersion": _version(descriptor.get("sourceVersion")),
                                    "sourceSha256": _hash(descriptor.get("sourceSha256"))})
                continue
            if key == "quotedTerms":
                terms = _optional_dict(raw.get("value"))
                for term, unit in _QUOTE_FIELDS.items():
                    if term in terms:
                        self.review_field(
                            node, f"quotedTerms.{term}", term, terms[term], unit, raw,
                            f"{path}/fields/{index}/value/{term}", historical,
                        )
                if set(terms) - _QUOTE_FIELDS.keys():
                    self.nodes[node]["redactedFields"].append("unregistered_quoted_terms")
                continue
            if key not in (_LEGACY_FIELDS if legacy else _REVIEW_FIELDS):
                self.nodes[node]["redactedFields"].append("unregistered_fields")
                continue
            unit = raw.get("unit") if legacy else _REVIEW_FIELDS[key]
            if legacy and unit not in {"inr", "INR", "money_major", "money_minor", "months", "years", "number"}:
                unit = "text"
            self.review_field(
                node, key, key.replace("Minor", "").replace("_", " "), raw.get("value"), unit, raw,
                f"{path}/fields/{index}", historical, legacy=legacy,
            )
        self.nodes[node]["redactedFields"] = sorted(set(self.nodes[node]["redactedFields"]))
        return node

    def review_field(
        self, review: str, key: str, label: str, value: Any, unit: str, raw: dict,
        path: str, historical: bool, *, legacy: bool = False,
    ) -> None:
        currency = None
        rendered = None
        withheld = False
        if legacy and raw.get("source") == "document_extracted":
            source_ref = raw.get("sourceRef")
            permitted_source = any(
                authorized and source_ref in {reading.get("documentId"), reading.get("readId")}
                for reading, _, authorized in self.readings.values()
            )
            if not permitted_source:
                value, withheld = None, True
                self.warnings.add("A compatibility form's document-extracted value is withheld without a current selected successful reading.")
        if value is not None:
            if unit in {"money_minor", "money_major", "inr", "INR"}:
                rendered = major_to_minor(value) if unit != "money_minor" else minor_string(value)
                currency, unit = "INR", "money_minor"
            elif unit in {"number", "months", "years"}:
                if type(value) is not int:
                    raise ProjectionError("An integer review field is not exact.")
                rendered = str(integer(value))
            elif unit == "percentage":
                rendered = decimal_string(value)
            elif unit == "boolean":
                if type(value) is not bool:
                    raise ProjectionError("A boolean review field is malformed.")
                rendered = "true" if value else "false"
            elif unit == "text":
                if not isinstance(value, str):
                    self.warnings.add("A non-scalar review field was withheld.")
                    return
                rendered = self.text(value)
            else:
                raise ProjectionError("A review field has unsupported declared units.")
        review_id = self.nodes[review]["attributes"]["reviewId"]
        node = self.put(
            "ReviewField", [review_id, key],
            {"reviewId": review_id, "key": key, "label": label, "value": rendered, "unit": unit,
             "currency": currency, "origin": self.text(raw.get("source") if legacy else raw.get("origin")),
             "editable": raw.get("editable"), "required": raw.get("required")},
            dataset="legacy.reviews" if legacy else "relationship.reviews", path=path, record_id=review_id,
            label=label, state="retained_field", source_kind="application", historical=historical,
            redacted=("value",) if withheld else (),
        )
        self.edge(node, review, "field_of_review", "The persisted form contains this allowlisted scalar field.", self.nodes[node]["source"])
        for reference in _list(raw.get("sourceRefs", [])):
            if reference in self.facts:
                self.warnings.add("An unversioned review fact reference was not linked to today's fact.")
            else:
                self.ref(node, reference, (*_HOLDINGS, "Product", "Quote", "Document", "Application", "BankDecision", "ExecutionReceipt"),
                         relation="field_source")

    def decision(self, raw: dict, path: str, historical: bool = False) -> str | None:
        if not self.scoped(raw):
            return None
        key = self.identity(raw.get("decisionId"))
        app_id = self.identity(raw.get("applicationId"))
        self.mirror("BankDecision", key, raw)
        model = _optional_dict(raw.get("modelRef"))
        response_id = model.get("responseId")
        if response_id is not None:
            response_id = self.identity(response_id)
        values = self.attrs("BankDecision", raw, [
            "decisionId", "applicationId", "outcome", "approvedAmountMinor", "policyVersion", "decidedAt", "expiresAt",
        ])
        values.update(
            operation=self.application_operations.get(app_id), currency="INR",
            reasons=self.texts(raw.get("reasons", [])), conditions=self.texts(raw.get("conditions", [])),
            providerMode="recorded_response" if response_id else None, responseId=response_id,
        )
        node = self.put(
            "BankDecision", key, values, dataset="relationship.decisions", path=path, label=f"Bank decision: {raw.get('outcome', 'recorded')}",
            state=raw.get("outcome", "recorded"), source_kind="decision", historical=historical,
            redacted=("modelRef",), warnings=self.expiry(raw.get("expiresAt"), "decision"),
        )
        self.ref(node, app_id, ("Application",), relation="decision_for", field="applicationId")
        self.refs(node, raw.get("evidenceRefs"), relation="decision_source")
        return node

    def receipt(self, raw: dict, path: str, historical: bool = False) -> str | None:
        if not self.scoped(raw):
            return None
        key = self.identity(raw.get("receiptId"))
        self.mirror("ExecutionReceipt", key, raw)
        effects = [self.identity(value) for value in _list(raw.get("effectIds", []))]
        if len(set(effects)) != len(effects):
            raise ProjectionError("A receipt duplicates an effect identity.")
        operation = self.operations.get(key)
        if operation and (
            operation.get("applicationId") != raw.get("applicationId")
            or operation.get("kind") != raw.get("operation")
            or operation.get("effectIds") != effects
        ):
            raise ProjectionError("The banking operation conflicts with its receipt.")
        values = self.attrs("ExecutionReceipt", raw, list(ENTITY_FIELDS["ExecutionReceipt"].keys() - {"currency"}))
        values["currency"] = "INR"
        node = self.put(
            "ExecutionReceipt", key, values, dataset="relationship.receipts", path=path, label=f"Receipt {key}",
            state="recorded" if operation else "operation_unresolved", source_kind="demo_effect",
            historical=historical,
            warnings=() if operation else ("No matching retained banking operation was found; execution links are not inferred.",),
        )
        self.ref(node, raw.get("applicationId"), ("Application",), relation="receipt_for", field="applicationId")
        if operation:
            self.receipt_effects[key] = effects
            for effect in effects:
                self.ref(node, effect, relation="recorded_effect",
                         reason="Receipt and owner-scoped operation retain the same effect identity.")
        return node

    def continuity(
        self, app_id: str | None, plan_id: str | None, case_id: str, state: str,
        path: str, recorded_at: str | None, *, operation: str | None = None, legacy: bool = False,
    ) -> str:
        key = self.identity(case_id)
        if legacy:
            next_action = "Retained compatibility instruction; bank fulfilment or service resolution is not implied."
            stage = "withdrawn" if state == "withdrawn" else "pending_review"
        elif state == "completed" and operation == "create_service_case":
            next_action, stage = "Await trusted service investigation; opening a case is not resolving it.", "pending"
        elif state == "completed":
            next_action, stage = "Recorded demo effect; no production execution or revenue is claimed.", "recorded"
        elif state in {"cancelled", "declined"}:
            next_action, stage = "Customer choice or bank refusal retained; no further banking action is implied.", state
        else:
            next_action, stage = "Continue the existing review or assessment before any separately consented banking action.", "in_progress"
        node = self.put(
            "CRMContinuity", key,
            {"caseId": key, "applicationId": app_id, "planId": plan_id, "nextAction": next_action,
             "recordedAt": recorded_at, "basis": "snapshot_application; no CRM synchronization"},
            dataset="derived.crmContinuity", path=path, state=stage, source_kind="derived",
            warnings=("Snapshot-derived continuity, not a read of a shared CRM cache.",),
        )
        self.ref(node, app_id, ("Application",), relation="continuity_for", field="applicationId")
        self.ref(node, plan_id, ("RelationshipPlan",), relation="plan_continuity", field="planId")
        return node

    def instruction_receipt(
        self, receipt_id: str, app_id: str | None, *, status: str,
        recorded_at: str | None, instruction: str | None, dataset: str, path: str,
    ) -> str:
        key = self.identity(receipt_id)
        values = {
            "eventId": key, "eventType": f"legacy_instruction_{status}",
            "applicationId": self.identity(app_id) if app_id is not None else None,
            "at": recorded_at, "note": self.text(instruction),
        }
        self.mirror("InstructionReceipt", key, values)
        node = self.put(
            "LifecycleEvent", ["instruction-receipt", key], values,
            dataset=dataset, path=path, label=f"Instruction receipt {key}",
            state=f"instruction_{status}", source_kind="application", historical=True,
            warnings=("Recorded preparation or submission only; this is not a banking execution receipt.",),
        )
        self.ref(
            node, app_id, ("Application",), relation="saved_instruction_for", field="applicationId",
            reason="The instruction names this logical application; it does not approve or execute its current terms.",
        )
        return node

    def legacy_records(self) -> None:
        for name in ("applications", "serviceRequests"):
            for index, raw in self.rows(self.owner, name):
                path = f"/owners/{self.owner_id}/{name}/{index}"
                if raw.get("family") not in {"product_application", "service_request"}:
                    if name == "serviceRequests":
                        self.service_case(raw, path)
                        continue
                    raise ProjectionError("Unknown compatibility application family.")
                key = self.identity(raw.get("applicationId"))
                if not re.fullmatch(r"app-[0-9a-f]{16}", key):
                    raise ProjectionError("A compatibility application has an invalid identity.")
                self.mirror("LegacyApplication", key, raw)
                binding = _optional_dict(raw.get("proposalBinding"))
                if binding and not self.scoped(binding):
                    continue
                amount = None
                for field in _list(raw.get("fields", [])):
                    field = _dict(field)
                    if field.get("key") in {"amount", "principal", "depositAmount", "requestedAmount", "loanAmount"}:
                        if field.get("value") is not None and field.get("unit") in {"inr", "INR", "money_major"}:
                            candidate = major_to_minor(field["value"])
                            if amount is not None and amount != candidate:
                                raise ProjectionError("A legacy application's explicitly typed amounts conflict.")
                            amount = candidate
                values = {
                    "applicationId": key, "productId": self.text(raw.get("productId")),
                    "operation": "legacy_service_instruction" if raw["family"] == "service_request" else "legacy_product_instruction",
                    "amountMinor": amount, "currency": "INR" if amount is not None else None, "legacy": True,
                }
                node = self.put(
                    "Application", key, values, dataset=f"legacy.{name}", path=path,
                    label=raw.get("title") or key, state=raw.get("state", "draft"), source_kind="application",
                    historical=raw.get("state") in {"withdrawn"},
                    warnings=("Compatibility instructions are not executed banking products or independent bank decisions.",),
                    redacted=("kyc", "unregistered_fields", "proposalBinding"),
                )
                self.ref(node, values["productId"], ("Product",), relation="proposes_product", field="productId")
                self.review(
                    {"title": raw.get("title"), "status": raw.get("state", "draft"), "fields": raw.get("fields", [])},
                    node, None, path, historical=raw.get("state") == "withdrawn",
                    version=digest({"updatedAt": raw.get("updatedAt"), "fields": raw.get("fields", [])}),
                    legacy=True,
                )
                if raw.get("receiptId") is not None:
                    receipt = self.instruction_receipt(
                        raw["receiptId"], key, status="submitted",
                        recorded_at=raw.get("submittedAt"), instruction=raw.get("recordedInstruction"),
                        dataset=f"legacy.{name}", path=path,
                    )
                    self.edge(
                        node, receipt, "instruction_receipt",
                        "The compatibility application retains this submitted instruction receipt, not a bank effect.",
                        self.nodes[receipt]["source"],
                    )
                if raw.get("caseId"):
                    self.continuity(key, None, raw["caseId"], raw.get("state", "draft"), path,
                                    raw.get("updatedAt"), legacy=True)
                if raw["family"] == "service_request":
                    self.service_case(raw, path, legacy=True)

        for key, raw in _dict(self.owner.get("receipts", {})).items():
            raw = _dict(raw)
            binding = _optional_dict(_optional_dict(raw.get("proposal")).get("binding"))
            if not binding or not self.scoped(binding, explicit=True):
                continue
            key = self.identity(key)
            result = _optional_dict(raw.get("result"))
            if any(
                receipt_id is not None and receipt_id != key
                for receipt_id in (raw.get("receiptId"), result.get("receiptId"))
            ):
                raise ProjectionError("A compatibility receipt has conflicting source identities.")
            status = result.get("status")
            if status not in {
                "review_saved", "submitted", "recorded", "application_created",
                "service_request_created", "application_prepared",
            }:
                continue
            review = _optional_dict(result.get("review"))
            application = _optional_dict(result.get("application"))
            app_id = application.get("applicationId") or result.get("applicationId")
            node = self.instruction_receipt(
                key, app_id, status=status, recorded_at=raw.get("recordedAt"),
                instruction=(
                    application.get("recordedInstruction") or result.get("recordedInstruction")
                    or review.get("recordedInstruction")
                ),
                dataset="legacy.receipts", path=f"/owners/{self.owner_id}/receipts/{_pointer(key)}",
            )
            if status == "review_saved" and review.get("caseId"):
                continuity = self.continuity(
                    app_id, None, review["caseId"], "review_saved",
                    f"/owners/{self.owner_id}/receipts/{_pointer(key)}/result/review", raw.get("recordedAt"),
                    legacy=True,
                )
                self.edge(node, continuity, "saved_review_continuity", "The saved review retains this case reference, without banking execution.", self.nodes[node]["source"])

    def service_case(self, raw: dict, path: str, *, legacy: bool = False) -> None:
        key = self.identity(raw.get("applicationId") if legacy else raw.get("serviceRequestId"))
        self.mirror("ServiceCase", key, raw)
        values = {
            "serviceRequestId": key, "caseId": self.text(raw.get("caseId")),
            "applicationId": self.text(raw.get("applicationId")),
            "serviceRequestType": self.text(raw.get("serviceTypeId") if legacy else raw.get("serviceRequestType")),
            "subjectId": self.text(raw.get("subjectId")), "resolutionStatus": "pending" if legacy else self.text(raw.get("resolutionStatus")),
            "createdAt": raw.get("createdAt"), "updatedAt": raw.get("updatedAt"),
            "summary": "Compatibility service instruction; resolution remains unconfirmed." if legacy else self.text(raw.get("summary")),
            "legacy": legacy,
        }
        node = self.put(
            "ServiceCase", key, values, dataset="legacy.serviceRequests" if legacy else "banking.serviceRequests",
            path=path, state=raw.get("state", "draft") if legacy else raw.get("status", "pending"),
            source_kind="application" if legacy else "bank_record",
        )
        self.ref(node, values["applicationId"], ("Application",), relation="service_application", field="applicationId")
        self.ref(node, values["subjectId"], (*_HOLDINGS, "Transaction"), relation="service_subject", field="subjectId")
        self.refs(node, raw.get("evidenceIds"), relation="service_evidence")
        self.ref(node, raw.get("settlementTransactionId"), ("Transaction",), relation="service_posting")

    def authority_records(self, relationship: dict) -> None:
        for index, raw in self.rows(self.seed.data, "consents"):
            key = self.identity(raw.get("consentId"))
            self.mirror("Consent", key, raw)
            values = self.attrs("Consent", raw, [
                "consentId", "applicationId", "planId", "itemId", "purpose", "reviewFingerprint", "receiptId", "expiresAt", "revokedAt",
            ])
            values.update(at=raw.get("at") or raw.get("grantedAt"), consentType=self.text(raw.get("type") or raw.get("purpose")))
            node = self.put(
                "Consent", key, values, dataset="banking.consents", path=f"/owners/{self.owner_id}/consents/{index}",
                state="revoked" if raw.get("revokedAt") else "recorded", source_kind="consent",
                warnings=self.expiry(raw.get("expiresAt"), "consent"),
            )
            for field, types, relation in (
                ("applicationId", ("Application",), "consent_for"),
                ("planId", ("RelationshipPlan",), "plan_consent"),
                ("receiptId", ("ExecutionReceipt",), "execution_consent"),
            ):
                self.ref(
                    node, values[field], types, relation=relation, field=field,
                    reason=(
                        "The consent names this logical plan; only its retained review fingerprint binds exact accepted terms."
                        if field == "planId" else "The retained source explicitly names this record."
                    ),
                )
            for resource in _list(raw.get("resourceRefs", [])):
                resource = _dict(resource)
                self.ref(node, resource.get("sourceId"), ("Document",), relation="evidence_consent",
                         match={"sourceVersion": _version(resource.get("sourceVersion"))})
            if raw.get("reviewFingerprint"):
                reviewed = [
                    record["id"] for record in self.nodes.values()
                    if record["entityType"] == "ApplicationReview"
                    and record["attributes"]["applicationId"] == raw.get("applicationId")
                    and record["source"]["version"] == raw["reviewFingerprint"]
                ]
                if len(reviewed) == 1:
                    self.edge(node, reviewed[0], "consents_to_review",
                              "The consent retains this exact application and review fingerprint.",
                              self.nodes[node]["source"])
                else:
                    self.warnings.add("The exact consented review version is not separately retained; no new review authority was inferred.")
        for name, container, explicit in (
            ("events", relationship, False), ("relationshipEvents", self.owner, True),
            ("eventHistory", self.owner, False),
        ):
            for index, raw in self.rows(container, name, explicit=explicit):
                event_id = raw.get("eventId")
                if event_id is None:
                    self.warnings.add("A retained event without a stable source identity was excluded.")
                    continue
                key = self.identity(event_id)
                self.mirror("LifecycleEvent", key, raw)
                result = _optional_dict(raw.get("result"))
                values = {
                    "eventId": key, "eventType": self.text(raw.get("type") or raw.get("eventType")),
                    "planId": self.text(raw.get("planId")), "itemId": self.text(raw.get("itemId")),
                    "applicationId": self.text(raw.get("applicationId")), "decisionId": self.text(raw.get("decisionId")),
                    "at": raw.get("at") or raw.get("recordedAt"), "revision": raw.get("revision"),
                    "note": self.text(raw.get("note")) if name == "events" else None,
                }
                path = f"/owners/{self.owner_id}/relationship/events/{index}" if name == "events" else f"/owners/{self.owner_id}/{name}/{index}"
                node = self.put(
                    "LifecycleEvent", key, values, dataset=f"banking.{name}", path=path,
                    state=raw.get("status", "recorded"), source_kind="history", historical=True,
                )
                for field, types, relation in (
                    ("planId", ("RelationshipPlan",), "plan_event"),
                    ("applicationId", ("Application",), "application_event"),
                    ("decisionId", ("BankDecision",), "decision_event"),
                ):
                    self.ref(
                        node, values[field], types, relation=relation, field=field,
                        match={"revision": values["revision"]} if field == "planId" else None,
                        reason=(
                            "The event's retained plan identifier and revision match this exact plan snapshot."
                            if field == "planId" else "The retained source explicitly names this record."
                        ),
                    )
                if raw.get("receipt") is not None:
                    receipt_node = self.receipt(_dict(raw["receipt"]), path + "/receipt", historical=True)
                    if receipt_node:
                        self.edge(node, receipt_node, "records_receipt", "This lifecycle event retains the same receipt identity.", self.nodes[node]["source"])
                self.refs(node, result.get("effectIds"), relation="event_effect")
                self.ref(node, result.get("transactionId"), ("Transaction",), relation="event_posting")
        for index, raw in self.rows(self.owner, "operations"):
            key = self.identity(raw.get("operationId"))
            node = self.put(
                "LifecycleEvent", ["operation", key],
                {"eventId": key, "eventType": self.text(raw.get("kind")), "applicationId": self.text(raw.get("applicationId")),
                 "at": raw.get("recordedAt"), "note": "Persisted demo banking operation; external execution is not implied."},
                dataset="banking.operations", path=f"/owners/{self.owner_id}/operations/{index}",
                state=raw.get("status", "recorded"), source_kind="demo_effect",
            )
            self.ref(node, raw.get("applicationId"), ("Application",), relation="operation_for", field="applicationId")
            self.ref(node, key, ("ExecutionReceipt",), relation="operation_receipt")
            self.refs(node, raw.get("effectIds"), relation="operation_effect")

    def frontier_records(self) -> None:
        state = frontier_view(self.owner)

        for index, raw in enumerate(state["calculations"]):
            calculation = CalculationOutput.model_validate(raw)
            if calculation.ownerId != self.owner_id:
                raise ProjectionError("A Frontier calculation is outside the authorized owner scope.")
            node = self.put(
                "Calculation",
                calculation.calculationId,
                {
                    "calculationId": calculation.calculationId,
                    "title": self.text(calculation.outputName),
                    "resultCount": len(calculation.values),
                    "inputCount": len(calculation.sourceRefs),
                    "assumptions": self.joined(sorted(calculation.assumptions)),
                },
                dataset="frontier.calculations",
                path=f"/owners/{self.owner_id}/frontier/calculations/{index}",
                version=calculation.journeyRevision,
                state="recorded",
                source_kind="calculated",
            )
            self.refs(node, calculation.sourceRefs, relation="calculation_source")

        for key, raw in state["journeys"].items():
            journey = FrontierJourneyState.model_validate(raw)
            if journey.ownerId != self.owner_id or journey.journeyInstanceId != key:
                raise ProjectionError("A Frontier journey is outside the authorized owner scope.")
            values = {
                "journeyInstanceId": journey.journeyInstanceId,
                "journeyId": journey.journeyId,
                "revision": journey.revision,
                "status": journey.status,
                "goals": self.joined(journey.goals),
                "calculationRefs": self.joined(journey.calculationRefs),
                "preparedActionRefs": self.joined(journey.preparedActionRefs),
                "handoffId": journey.handoffId,
                "createdAt": journey.createdAt,
                "updatedAt": journey.updatedAt,
            }
            node = self.put(
                "FrontierJourney",
                journey.journeyInstanceId,
                values,
                dataset="frontier.journeys",
                path=f"/owners/{self.owner_id}/frontier/journeys/{_pointer(key)}",
                version=journey.revision,
                state=journey.status,
                source_kind="frontier",
            )
            self.refs(node, journey.calculationRefs, relation="journey_calculation")
            self.refs(node, journey.preparedActionRefs, relation="journey_action")
            self.ref(
                node, journey.handoffId, ("HumanHandoff",),
                relation="journey_handoff", field="handoffId",
            )

        adapters = (
            (
                "candidates", ProactiveCandidate, "ProactiveCandidate", "candidateId",
                lambda row: {
                    "candidateId": row.candidateId, "journeyId": row.journeyId,
                    "kind": row.kind, "riskLevel": row.riskLevel,
                    "priorityScore": row.priorityScore, "sourceRefs": self.joined(row.sourceRefs),
                    "calculationRefs": self.joined(row.calculationRefs),
                    "detectedAt": row.detectedAt, "expiresAt": row.expiresAt,
                    "status": row.status, "suppressionReason": self.text(row.suppressionReason),
                },
            ),
            (
                "householdAccess", HouseholdAccessGrant, "HouseholdAccessGrant", "grantId",
                lambda row: {
                    "grantId": row.grantId, "subjectId": row.subjectId,
                    "subjectKind": row.subjectKind, "resourceScopes": self.joined(row.resourceScopes),
                    "permissions": self.joined(row.permissions), "validFrom": row.validFrom,
                    "expiresAt": row.expiresAt, "revokedAt": row.revokedAt,
                    "createdAt": row.createdAt,
                },
            ),
            (
                "consents", FrontierConsentRecord, "FrontierConsent", "consentId",
                lambda row: {
                    "consentId": row.consentId, "subjectKind": row.subjectKind,
                    "subjectId": row.subjectId, "purpose": self.text(row.purpose),
                    "resourceRefs": self.joined(row.resourceRefs),
                    "permissions": self.joined(row.permissions),
                    "recordedAt": row.recordedAt, "expiresAt": row.expiresAt,
                    "revokedAt": row.revokedAt, "evidenceRefs": self.joined(row.evidenceRefs),
                },
            ),
            (
                "registeredAgents", RegisteredAgent, "RegisteredAgent", "agentId",
                lambda row: {
                    "agentId": row.agentId, "displayName": self.text(row.displayName),
                    "identityRef": row.identityRef, "permittedData": self.joined(row.permittedData),
                    "permittedActions": self.joined(row.permittedActions),
                    "transactionLimitMinor": minor_string(row.transactionLimitMinor),
                    "currency": "INR",
                    "validFrom": row.validFrom, "expiresAt": row.expiresAt,
                    "status": row.status, "humanAccountability": self.text(row.humanAccountability),
                    "registeredAt": row.registeredAt, "revokedAt": row.revokedAt,
                },
            ),
            (
                "mandates", SurplusToGoalMandate, "FrontierMandate", "mandateId",
                lambda row: {
                    "mandateId": row.mandateId, "agentId": row.agentId,
                    "journeyId": row.journeyId, "consentId": row.consentId,
                    "sourceAccountId": row.sourceAccountId, "targetGoalId": row.targetGoalId,
                    "protectedCashMinor": minor_string(row.protectedCashMinor),
                    "perExecutionLimitMinor": minor_string(row.perExecutionLimitMinor),
                    "monthlyLimitMinor": minor_string(row.monthlyLimitMinor),
                    "currency": "INR",
                    "effectiveFrom": row.effectiveFrom, "expiresAt": row.expiresAt,
                    "status": row.status, "revision": row.revision,
                    "createdAt": row.createdAt, "updatedAt": row.updatedAt,
                    "revokedAt": row.revokedAt,
                },
            ),
            (
                "preparedActions", PreparedAction, "PreparedAction", "actionId",
                lambda row: {
                    "actionId": row.actionId,
                    "journeyInstanceId": self.text(row.parameters.get("journeyInstanceId")),
                    "journeyId": row.journeyId, "operation": row.operation,
                    "authorityRequired": row.authorityRequired,
                    "amountMinor": minor_string(row.amountMinor), "currency": row.currency,
                    "sourceRefs": self.joined(row.sourceRefs),
                    "calculationRefs": self.joined(row.calculationRefs),
                    "journeyRevision": row.journeyRevision,
                    "policyVersion": row.policyVersion, "preparedAt": row.preparedAt,
                    "expiresAt": row.expiresAt, "status": row.status,
                },
            ),
            (
                "mandateActions", MandateActionRecord, "MandateAction", "actionRecordId",
                lambda row: {
                    "actionRecordId": row.actionRecordId, "mandateId": row.mandateId,
                    "candidateId": row.candidateId, "preparedActionId": row.preparedActionId,
                    "amountMinor": minor_string(row.amountMinor),
                    "protectedCashBeforeMinor": minor_string(row.protectedCashBeforeMinor),
                    "protectedCashAfterMinor": minor_string(row.protectedCashAfterMinor),
                    "monthlyUsedBeforeMinor": minor_string(row.monthlyUsedBeforeMinor),
                    "monthlyUsedAfterMinor": minor_string(row.monthlyUsedAfterMinor),
                    "currency": "INR",
                    "status": row.status, "reasons": self.joined(row.reasons),
                    "receiptId": row.receiptId, "recordedAt": row.recordedAt,
                },
            ),
            (
                "handoffs", HumanHandoff, "HumanHandoff", "handoffId",
                lambda row: {
                    "handoffId": row.handoffId, "caseId": row.caseId,
                    "journeyInstanceId": row.journeyInstanceId, "journeyId": row.journeyId,
                    "specialistRole": row.specialistRole, "revision": row.revision,
                    "createdBy": row.createdBy, "consentRefs": self.joined(row.consentRefs),
                    "contextRefs": self.joined(row.contextRefs),
                    "calculationRefs": self.joined(row.calculationRefs),
                    "latestHumanDecisionId": row.latestHumanDecisionId,
                    "status": row.status, "createdAt": row.createdAt,
                    "updatedAt": row.updatedAt, "returnedAt": row.returnedAt,
                },
            ),
            (
                "humanDecisions", HumanDecision, "HumanDecision", "humanDecisionId",
                lambda row: {
                    "humanDecisionId": row.humanDecisionId, "handoffId": row.handoffId,
                    "journeyInstanceId": row.journeyInstanceId, "reviewerId": row.reviewerId,
                    "specialistRole": row.specialistRole, "handoffRevision": row.handoffRevision,
                    "commandId": row.commandId, "action": row.action,
                    "evidenceRefs": self.joined(row.evidenceRefs),
                    "relatedDecisionId": row.relatedDecisionId, "recordedAt": row.recordedAt,
                },
            ),
            (
                "outcomes", OutcomeRecord, "FrontierOutcome", "outcomeId",
                lambda row: {
                    "outcomeId": row.outcomeId, "journeyInstanceId": row.journeyInstanceId,
                    "journeyId": row.journeyId, "outcomeType": row.outcomeType,
                    "amountImpactMinor": minor_string(row.amountImpactMinor),
                    "currency": "INR",
                    "sourceRefs": self.joined(row.sourceRefs),
                    "recordedAt": row.recordedAt, "demoOnly": row.demoOnly,
                },
            ),
        )
        for collection, contract, entity, key_name, values_for in adapters:
            for index, raw in enumerate(state[collection]):
                row = contract.model_validate(raw)
                if row.ownerId != self.owner_id:
                    raise ProjectionError("A Frontier record is outside the authorized owner scope.")
                key = getattr(row, key_name)
                values = values_for(row)
                node = self.put(
                    entity,
                    key,
                    values,
                    dataset=f"frontier.{collection}",
                    path=f"/owners/{self.owner_id}/frontier/{collection}/{index}",
                    version=getattr(row, "revision", None),
                    state=str(getattr(row, "status", "recorded")),
                    source_kind="frontier",
                )
                if entity == "ProactiveCandidate":
                    self.refs(node, row.sourceRefs, relation="candidate_source", field="sourceRefs")
                    self.refs(node, row.calculationRefs, relation="candidate_calculation", field="calculationRefs")
                elif entity == "HouseholdAccessGrant":
                    self.refs(node, row.resourceScopes, relation="grant_scope", field="resourceScopes")
                elif entity == "FrontierConsent":
                    self.ref(
                        node, row.subjectId, ("RegisteredAgent",),
                        relation="consent_subject", field="subjectId",
                    )
                    self.refs(node, row.resourceRefs, relation="consent_scope", field="resourceRefs")
                    self.refs(node, row.evidenceRefs, relation="consent_evidence", field="evidenceRefs")
                elif entity == "RegisteredAgent":
                    self.refs(node, row.permittedData, relation="agent_data_scope", field="permittedData")
                elif entity == "FrontierMandate":
                    self.ref(
                        node, row.agentId, ("RegisteredAgent",),
                        relation="mandate_agent", field="agentId",
                    )
                    self.ref(
                        node, row.consentId, ("FrontierConsent",),
                        relation="mandate_consent", field="consentId",
                    )
                    self.ref(
                        node, row.sourceAccountId, ("Account",),
                        relation="mandate_source", field="sourceAccountId",
                    )
                    self.ref(
                        node, row.targetGoalId, ("Goal",),
                        relation="mandate_goal", field="targetGoalId",
                    )
                elif entity == "PreparedAction":
                    self.ref(
                        node, row.parameters.get("journeyInstanceId"), ("FrontierJourney",),
                        relation="prepared_for", field="journeyInstanceId",
                    )
                    self.refs(node, row.sourceRefs, relation="action_source", field="sourceRefs")
                    self.refs(node, row.calculationRefs, relation="action_calculation", field="calculationRefs")
                elif entity == "MandateAction":
                    self.ref(
                        node, row.mandateId, ("FrontierMandate",),
                        relation="assessed_mandate", field="mandateId",
                    )
                    self.ref(
                        node, row.candidateId, ("ProactiveCandidate",),
                        relation="candidate_action", field="candidateId",
                    )
                    self.ref(
                        node, row.preparedActionId, ("PreparedAction",),
                        relation="executes_action", field="preparedActionId",
                    )
                    self.ref(
                        node, row.receiptId, ("ExecutionReceipt",),
                        relation="execution_receipt", field="receiptId",
                    )
                elif entity == "HumanHandoff":
                    self.ref(
                        node, row.journeyInstanceId, ("FrontierJourney",),
                        relation="handoff_journey", field="journeyInstanceId",
                    )
                    self.refs(node, row.consentRefs, relation="handoff_consent", field="consentRefs")
                    self.refs(node, row.calculationRefs, relation="handoff_calculation", field="calculationRefs")
                    self.refs(node, row.contextRefs, relation="handoff_context", field="contextRefs")
                    self.ref(
                        node, row.latestHumanDecisionId, ("HumanDecision",),
                        relation="latest_decision", field="latestHumanDecisionId",
                    )
                elif entity == "HumanDecision":
                    self.ref(
                        node, row.handoffId, ("HumanHandoff",),
                        relation="decision_handoff", field="handoffId",
                    )
                    self.ref(
                        node, row.journeyInstanceId, ("FrontierJourney",),
                        relation="decision_journey", field="journeyInstanceId",
                    )
                    self.ref(
                        node, row.relatedDecisionId, ("HumanDecision",),
                        relation="returns_decision", field="relatedDecisionId",
                    )
                    self.refs(node, row.evidenceRefs, relation="decision_evidence", field="evidenceRefs")
                elif entity == "FrontierOutcome":
                    self.ref(
                        node, row.journeyInstanceId, ("FrontierJourney",),
                        relation="outcome_journey", field="journeyInstanceId",
                    )
                    self.refs(node, row.sourceRefs, relation="outcome_evidence", field="sourceRefs")

        for index, raw in enumerate(state["agentActions"]):
            if not isinstance(raw, dict):
                raise ProjectionError("A Frontier agent-action record is malformed.")
            if raw.get("ownerId") != self.owner_id:
                raise ProjectionError("A Frontier agent-action record is outside the authorized owner scope.")
            key = (
                raw.get("actionId") or raw.get("requestId") or raw.get("commandId")
                or f"faa-{digest([index, raw])[:24]}"
            )
            key = self.identity(str(key))
            values = {
                "agentActionId": key,
                "recordType": self.text(raw.get("recordType")),
                "agentId": self.text(raw.get("agentId")),
                "identityRef": self.text(raw.get("identityRef")),
                "traceRef": self.text(raw.get("traceId")),
                "toolName": self.text(raw.get("tool")),
                "commandId": self.text(raw.get("commandId")),
                "authority": self.text(raw.get("authority")),
                "recordedAt": raw.get("recordedAt"),
            }
            self.put(
                "AgentAction",
                key,
                values,
                dataset="frontier.agentActions",
                path=f"/owners/{self.owner_id}/frontier/agentActions/{index}",
                state="recorded",
                source_kind="frontier",
            )

    def build(self) -> Projection:
        relationship = _optional_dict(self.owner.get("relationship"))
        if relationship and not self.scoped(relationship):
            raise ProjectionError("The relationship state has a different scope.")
        for _, operation in self.rows(self.owner, "operations"):
            key = self.identity(operation.get("operationId"))
            self.mirror("BankOperation", key, operation)
            self.operations[key] = operation
        self.bank_records()
        self.document_records()
        self.evidence_records(relationship)
        blocks = list(self.retained_blocks())
        self.calculations(relationship, blocks)
        self.plan_records(relationship, blocks)
        self.legacy_records()
        self.authority_records(relationship)
        self.frontier_records()
        self.finish_links()
        records = tuple(sorted((Record(**value) for value in self.nodes.values()), key=_record_order))
        for entity in ENTITY_FIELDS:
            validate_concrete_rows(
                entity, [concrete_row(record) for record in records if record.entityType == entity],
                self.definitions,
            )
        edges = tuple(sorted(self.edges.values(), key=lambda edge: edge.id))
        return Projection(meta=self.meta, records=records, edges=edges, warnings=tuple(sorted(self.warnings)))


def project_snapshot(
    *, snapshot: dict, owner_id: str, seed: SeedData, metadata: Any,
    build_fingerprint: str, package_fingerprint: str | None = None,
    observed_at: str | None = None,
) -> Projection:
    """Project, without copying back or mutating any part of the captured input."""
    meta = build_meta(
        snapshot=snapshot, owner_id=owner_id, seed=seed, metadata=metadata,
        build_fingerprint=build_fingerprint, package_fingerprint=package_fingerprint,
        observed_at=observed_at,
    )
    return _Builder(snapshot, owner_id, seed, metadata, meta).build()


build_projection = project_snapshot


def check_expected(meta: SnapshotMeta, query: SnapshotQuery, *, required: bool = False) -> None:
    if required and not query.expected:
        raise InvalidQuery()
    if query.owner is not None and query.owner != meta.ownerId:
        raise StaleScope()
    if query.expected is not None and query.expected != meta.snapshotId:
        raise StaleScope()


def _record_order(record: Record) -> tuple:
    return (
        record.historical, _FAMILY_ORDER[record.family],
        record.entityType == "Transaction", record.entityType, record.label.casefold(), record.id,
    )


def _mode_edges(projection: Projection, query: SnapshotQuery) -> list[Edge]:
    excluded = {"customer_record", "uses_product", "linked_account", "posted_to", "card_entry"}
    return [
        edge for edge in projection.edges
        if query.mode != "lineage" or edge.relation not in excluded
    ]


def select_scope(projection: Projection, query: SnapshotQuery) -> tuple[list[Record], list[Edge], list[Record]]:
    check_expected(projection.meta, query)
    by_id = {record.id: record for record in projection.records}
    edges = _mode_edges(projection, query)
    ids = set(by_id)
    if query.focus:
        if query.focus not in by_id:
            raise InaccessibleNode()
        adjacency: dict[str, set[str]] = {}
        for edge in edges:
            adjacency.setdefault(edge.from_id, set()).add(edge.to)
            adjacency.setdefault(edge.to, set()).add(edge.from_id)
        ids, frontier = {query.focus}, {query.focus}
        for _ in range(query.depth):
            following = {neighbor for node in frontier for neighbor in adjacency.get(node, ())} - ids
            ids.update(following)
            frontier = following
    scope = [record for record in projection.records if record.id in ids]
    if query.q:
        needle = query.q.casefold()
        scope = [
            record for record in scope if needle in "\n".join(
                [record.id, record.entityType, record.label, record.state, record.source.recordId]
                + [str(value) for value in record.attributes.values() if value is not None]
            ).casefold()
        ]
    records = [
        record for record in scope
        if (not query.family or record.family == query.family)
        and (not query.entityType or record.entityType == query.entityType)
        and (not query.domain or query.domain in record.domainIds)
        and (not query.sourceKind or record.sourceKind == query.sourceKind)
        and (not query.state or record.state == query.state)
    ]
    selected = {record.id for record in records}
    return records, [edge for edge in edges if edge.from_id in selected and edge.to in selected], scope


def _facet(values: list[str]) -> list[dict]:
    return [{"value": value, "label": value.replace("_", " "), "count": count} for value, count in sorted(Counter(values).items())]


def snapshot_response(projection: Projection, query: SnapshotQuery) -> dict:
    records, edges, scope = select_scope(projection, query)
    if query.offset > len(records):
        raise InvalidQuery()
    page = records[query.offset:query.offset + query.limit]
    by_id = {record.id: record for record in records}
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge.from_id, set()).add(edge.to)
        adjacency.setdefault(edge.to, set()).add(edge.from_id)
    start = query.focus if query.focus in by_id else next(
        (row.id for row in records if row.entityType == "Customer"), records[0].id if records else None,
    )
    if query.mode == "lineage" and not query.focus:
        lineage_priority = {
            "EvidenceReading": 0, "Fact": 1, "Calculation": 2, "ApplicationReview": 3,
            "BankDecision": 4, "ExecutionReceipt": 5, "Document": 6,
        }
        candidates = [
            row for row in records if row.entityType in lineage_priority and adjacency.get(row.id)
        ]
        if candidates:
            start = min(candidates, key=lambda row: (row.historical, lineage_priority[row.entityType], _record_order(row))).id
    selected: list[str] = []
    pending = [(_record_order(by_id[start]), start)] if start else []
    queued = {start} if start else set()
    while pending and len(selected) < query.graphLimit:
        _, node = heapq.heappop(pending)
        selected.append(node)
        neighbors = sorted(
            (neighbor for neighbor in adjacency.get(node, ()) if neighbor not in queued),
            key=lambda identifier: _record_order(by_id[identifier]),
        )
        for neighbor in neighbors:
            heapq.heappush(pending, (_record_order(by_id[neighbor]), neighbor))
        queued.update(neighbors)
    if len(selected) < query.graphLimit:
        selected += [row.id for row in records if row.id not in set(selected)][:query.graphLimit - len(selected)]
    selected_ids = set(selected)
    graph_edges = [edge for edge in edges if edge.from_id in selected_ids and edge.to in selected_ids][:MAX_EDGES]
    warnings = list(projection.warnings)
    if query.mode == "lineage" and not any(
        row.entityType in {"EvidenceReading", "Fact", "Calculation", "ApplicationReview", "BankDecision", "ExecutionReceipt"}
        for row in records
    ):
        warnings.append("No retained reading, calculation, review, decision or receipt is present in this selected scope; no lineage was invented.")
    if len(records) > len(selected):
        warnings.append("The graph is bounded; use search, filters, focus or the paginated table to inspect remaining records.")
    if len(edges) > len(graph_edges):
        warnings.append("Some in-scope relationships are not rendered; counts and export retain the selected scope.")
    return {
        "meta": projection.meta.model_dump(),
        "records": [record.model_dump() for record in page],
        "graph": {"nodes": [by_id[key].model_dump() for key in selected],
                  "edges": [edge.model_dump(by_alias=True) for edge in graph_edges]},
        "counts": {
            "totalRecords": len(projection.records), "filteredRecords": len(records), "returnedRecords": len(page),
            "totalEdges": len(edges), "shownNodes": len(selected), "shownEdges": len(graph_edges),
            "omittedEdges": len(edges) - len(graph_edges),
        },
        "facets": {
            "families": _facet([record.family for record in scope]),
            "entityTypes": _facet([record.entityType for record in scope]),
            "domains": _facet([domain for record in scope for domain in record.domainIds]),
            "sourceKinds": _facet([record.sourceKind for record in scope]),
            "states": _facet([record.state for record in scope]),
        },
        "pagination": {"offset": query.offset, "limit": query.limit,
                       "nextOffset": query.offset + query.limit if query.offset + query.limit < len(records) else None},
        "limits": {"maxNodes": MAX_NODES, "maxEdges": MAX_EDGES, "pageSize": PAGE_SIZE},
        "warnings": warnings,
    }
