"""Strict, lossless contracts for the read-only banking projection.

The registry below is an allowlist, not a mapping-expression interpreter. Source
adapters must explicitly cross the bank-minor/catalogue-major unit boundary.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

PROJECTION_VERSION = "banking-data-v1"
BIAN_RELEASE = "13.0.0"
MAX_NODES = 150
MAX_EDGES = 400
PAGE_SIZE = 100
MIN_MINOR = -(2**63)
MAX_MINOR = 2**63 - 1
MIN_INTEGER = -(2**31)
MAX_INTEGER = 2**31 - 1
FAMILIES = (
    "customers", "products", "holdings", "money", "goals", "evidence",
    "planning", "authority", "servicing", "frontier",
)
SourceKind = Literal[
    "bank_record", "catalogue", "document_metadata", "document_read",
    "customer_stated", "customer_corrected", "calculated", "application",
    "decision", "consent", "demo_effect", "history", "derived", "schema",
    "frontier",
]
Scalar = StrictStr | StrictInt | StrictBool | None
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MINOR = re.compile(r"^(?:0|-[1-9][0-9]*|[1-9][0-9]*)$")


class XRayError(ValueError):
    status = 503
    code = "banking_data_unavailable"
    public_message = "Banking Data X-Ray is unavailable. Refresh or ask the operator to validate its data and model."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.public_message)


class ProjectionError(XRayError):
    """Never expose the source value or exception text at the HTTP boundary."""


class InvalidQuery(XRayError):
    status = 400
    code = "invalid_query"
    public_message = "Use the documented filters and bounds; pagination, focus and export require the current snapshotId as expected."


class StaleScope(XRayError):
    status = 409
    code = "stale_snapshot"
    public_message = "The customer, scenario or source version changed. Clear the old view and refresh before continuing."


class InaccessibleNode(XRayError):
    status = 404
    code = "node_not_found"
    public_message = "This record is not available in the current customer snapshot. Refresh the view."


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def digest(value: Any) -> str:
    return sha256(canonical_json(value)).hexdigest()


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ProjectionError("Invalid source identity.")
    return value


def integer(value: Any, *, minimum: int = MIN_INTEGER, maximum: int = MAX_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProjectionError("Invalid strict integer.")
    return value


def minor_string(value: Any, *, transported: bool = False) -> str:
    if transported:
        if not isinstance(value, str) or not _MINOR.fullmatch(value) or len(value) > 20:
            raise ProjectionError("Invalid canonical minor-unit string.")
        value = int(value)
    return str(integer(value, minimum=MIN_MINOR, maximum=MAX_MINOR))


def decimal_string(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ProjectionError("Invalid decimal.")
    if isinstance(value, str) and (not value or len(value) > 100 or value != value.strip()):
        raise ProjectionError("Invalid decimal.")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProjectionError("Invalid decimal.") from exc
    if not number.is_finite() or abs(number) > Decimal(2**63) or number.as_tuple().exponent < -30:
        raise ProjectionError("Invalid decimal range.")
    result = format(number, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if number == 0 else result


def major_to_minor(value: Any) -> str:
    with localcontext() as context:
        context.prec = 100
        number = Decimal(decimal_string(value)) * 100
    if number != number.to_integral_value():
        raise ProjectionError("Major-unit value contains a fraction of a minor unit.")
    return minor_string(int(number))


def timestamp(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value,
    ):
        raise ProjectionError("Invalid timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectionError("Invalid timestamp.") from exc
    if parsed.tzinfo is None or "T" not in value:
        raise ProjectionError("Timestamp requires an explicit timezone.")
    return value


def _fields(spec: str) -> dict[str, str]:
    return dict(part.split(":") for part in spec.split())


ENTITY_FIELDS: dict[str, dict[str, str]] = {
    "Customer": _fields("ownerId:string displayName:string city:string cohort:string"),
    "PartyRelationship": _fields("relationshipId:string customerId:string relatedPartyRef:string role:string evidenceId:string"),
    "Product": _fields("productId:string productVersion:string productFamily:string name:string currency:string ratePct:decimal annualFeeMinor:minor monthlyFeeMinor:minor monthlyPremiumMinor:minor sumInsuredMinor:minor minimumPrincipalMinor:minor maximumPrincipalMinor:minor minimumTermMonths:integer maximumTermMonths:integer requiresDecision:boolean"),
    "Account": _fields("accountId:string productId:string currency:string postedBalanceMinor:minor availableBalanceMinor:minor holdsMinor:minor openedOn:date version:integer"),
    "Card": _fields("cardId:string productId:string linkedAccountId:string cardType:string maskedNumber:string currency:string limitMinor:minor outstandingMinor:minor availableCreditMinor:minor prepaidBalanceMinor:minor expiry:string version:integer"),
    "Deposit": _fields("depositId:string productId:string linkedAccountId:string currency:string principalMinor:minor monthlyInstalmentMinor:minor depositType:string openedOn:date maturesOn:date ratePct:decimal payoutMode:string version:integer"),
    "Loan": _fields("liabilityId:string productId:string linkedAccountId:string currency:string principalMinor:minor outstandingMinor:minor monthlyPaymentMinor:minor ratePct:decimal termMonths:integer openedOn:date maturesOn:date version:integer"),
    "ProtectionPolicy": _fields("policyId:string productId:string linkedAccountId:string coverType:string currency:string premiumMinor:minor sumInsuredMinor:minor deductibleMinor:minor premiumFrequency:string startedOn:date renewsOn:date futureCoverOnly:boolean claimsApproved:boolean version:integer"),
    "Transaction": _fields("transactionId:string accountId:string cardId:string applicationId:string currency:string amountMinor:minor direction:string category:string description:string valueDate:date postedAt:datetime recordedAt:datetime transferId:string"),
    "Goal": _fields("goalId:string title:string currency:string targetAmountMinor:minor allocatedAmountMinor:minor targetDate:date priority:string"),
    "Preference": _fields("preferenceId:string field:string value:string recordedOn:date"),
    "Document": _fields("documentId:string title:string documentKind:string contentType:string sourceVersion:string sourceSha256:string pageCount:integer pageCountBasis:string issuedOn:date previewable:boolean"),
    "EvidenceSelection": _fields("selectionId:string documentId:string purpose:string selectedAt:datetime revision:integer"),
    "EvidenceReading": _fields("readId:string documentId:string sourceVersion:string sourceSha256:string pageCount:integer extractionMethod:string extractionVersion:string readAt:datetime reusedExtractionReadId:string error:string"),
    "Fact": _fields("factId:string label:string value:string unit:string currency:string origin:string revision:integer requiresConfirmation:boolean confidence:decimal readId:string documentId:string page:integer regionId:string sourceSha256:string"),
    "Calculation": _fields("calculationId:string title:string resultCount:integer inputCount:integer assumptions:string"),
    "CalculationValue": _fields("calculationId:string field:string value:string unit:string currency:string"),
    "RelationshipPlan": _fields("planId:string channel:string revision:integer goals:string protectedCashMinor:minor monthlyBudgetMinor:minor currency:string riskContext:string createdAt:datetime updatedAt:datetime catalogueFingerprint:string"),
    "Application": _fields("applicationId:string planId:string itemId:string productId:string operation:string sourceAccountId:string holdingId:string targetId:string amountMinor:minor currency:string termMonths:integer payout:string benefit:string reviewFingerprint:string legacy:boolean"),
    "ApplicationReview": _fields("reviewId:string applicationId:string quoteId:string missingCount:integer requirements:string expiresAt:datetime"),
    "ReviewField": _fields("reviewId:string key:string label:string value:string unit:string currency:string origin:string editable:boolean required:boolean"),
    "Quote": _fields("quoteId:string applicationId:string currency:string debitMinor:minor monthlyCommitmentMinor:minor maximumAmountMinor:minor requiresDecision:boolean policyVersion:string catalogueFingerprint:string evidenceFingerprint:string expiresAt:datetime"),
    "BankDecision": _fields("decisionId:string applicationId:string operation:string outcome:string approvedAmountMinor:minor currency:string policyVersion:string decidedAt:datetime expiresAt:datetime reasons:string conditions:string providerMode:string responseId:string"),
    "Consent": _fields("consentId:string applicationId:string planId:string itemId:string purpose:string consentType:string reviewFingerprint:string receiptId:string at:datetime expiresAt:datetime revokedAt:datetime"),
    "ExecutionReceipt": _fields("receiptId:string applicationId:string operation:string currency:string amountMinor:minor recordedAt:datetime attribution:string summary:string demoOnly:boolean"),
    "ServiceCase": _fields("serviceRequestId:string caseId:string applicationId:string serviceRequestType:string subjectId:string resolutionStatus:string createdAt:datetime updatedAt:datetime summary:string legacy:boolean"),
    "Commitment": _fields("commitmentId:string applicationId:string sourceAccountId:string targetId:string kind:string currency:string amountMinor:minor nextDueOn:date remainingPayments:integer version:integer"),
    "SweepRule": _fields("sweepRuleId:string applicationId:string productId:string sourceAccountId:string currency:string thresholdMinor:minor multipleMinor:minor ceilingMinor:minor protectedCashMinor:minor monthlyFeeMinor:minor version:integer"),
    "CollateralPledge": _fields("pledgeId:string depositId:string facilityId:string applicationId:string currency:string amountMinor:minor recordedAt:datetime"),
    "Allocation": _fields("allocationId:string goalId:string sourceAccountId:string applicationId:string currency:string amountMinor:minor recordedAt:datetime"),
    "Settlement": _fields("settlementId:string applicationId:string kind:string sourceAccountId:string recipientId:string liabilityId:string currency:string amountMinor:minor recipientCreditMinor:minor escrowBalanceMinor:minor loanPrincipalMinor:minor ownContributionMinor:minor closingCostsMinor:minor titleStatus:string recordedAt:datetime externalRailExecuted:boolean externalConveyanceExecuted:boolean"),
    "LifecycleEvent": _fields("eventId:string eventType:string planId:string itemId:string applicationId:string decisionId:string at:datetime revision:integer note:string"),
    "CRMContinuity": _fields("caseId:string applicationId:string planId:string nextAction:string recordedAt:datetime basis:string"),
    "FrontierJourney": _fields("journeyInstanceId:string journeyId:string revision:integer status:string goals:string calculationRefs:string preparedActionRefs:string handoffId:string createdAt:datetime updatedAt:datetime"),
    "ProactiveCandidate": _fields("candidateId:string journeyId:string kind:string riskLevel:string priorityScore:integer sourceRefs:string calculationRefs:string detectedAt:datetime expiresAt:datetime status:string suppressionReason:string"),
    "HouseholdAccessGrant": _fields("grantId:string subjectId:string subjectKind:string resourceScopes:string permissions:string validFrom:datetime expiresAt:datetime revokedAt:datetime createdAt:datetime"),
    "FrontierConsent": _fields("consentId:string subjectKind:string subjectId:string purpose:string resourceRefs:string permissions:string recordedAt:datetime expiresAt:datetime revokedAt:datetime evidenceRefs:string"),
    "RegisteredAgent": _fields("agentId:string displayName:string identityRef:string permittedData:string permittedActions:string transactionLimitMinor:minor currency:string validFrom:datetime expiresAt:datetime status:string humanAccountability:string registeredAt:datetime revokedAt:datetime"),
    "FrontierMandate": _fields("mandateId:string agentId:string journeyId:string consentId:string sourceAccountId:string targetGoalId:string protectedCashMinor:minor perExecutionLimitMinor:minor monthlyLimitMinor:minor currency:string effectiveFrom:datetime expiresAt:datetime status:string revision:integer createdAt:datetime updatedAt:datetime revokedAt:datetime"),
    "PreparedAction": _fields("actionId:string journeyInstanceId:string journeyId:string operation:string authorityRequired:string amountMinor:minor currency:string sourceRefs:string calculationRefs:string journeyRevision:integer policyVersion:string preparedAt:datetime expiresAt:datetime status:string"),
    "MandateAction": _fields("actionRecordId:string mandateId:string candidateId:string preparedActionId:string amountMinor:minor protectedCashBeforeMinor:minor protectedCashAfterMinor:minor monthlyUsedBeforeMinor:minor monthlyUsedAfterMinor:minor currency:string status:string reasons:string receiptId:string recordedAt:datetime"),
    "HumanHandoff": _fields("handoffId:string caseId:string journeyInstanceId:string journeyId:string specialistRole:string revision:integer createdBy:string consentRefs:string contextRefs:string calculationRefs:string latestHumanDecisionId:string status:string createdAt:datetime updatedAt:datetime returnedAt:datetime"),
    "HumanDecision": _fields("humanDecisionId:string handoffId:string journeyInstanceId:string reviewerId:string specialistRole:string handoffRevision:integer commandId:string action:string evidenceRefs:string relatedDecisionId:string recordedAt:datetime"),
    "FrontierOutcome": _fields("outcomeId:string journeyInstanceId:string journeyId:string outcomeType:string amountImpactMinor:minor currency:string sourceRefs:string recordedAt:datetime demoOnly:boolean"),
    "AgentAction": _fields("agentActionId:string recordType:string agentId:string identityRef:string traceRef:string toolName:string commandId:string authority:string recordedAt:datetime"),
}
ENTITY_FAMILIES = {
    entity: family
    for family, entities in {
        "customers": ("Customer", "PartyRelationship"),
        "products": ("Product",),
        "holdings": ("Account", "Card", "Deposit", "Loan", "ProtectionPolicy"),
        "money": ("Transaction", "Commitment", "SweepRule", "CollateralPledge", "Allocation", "Settlement"),
        "goals": ("Goal", "Preference"),
        "evidence": ("Document", "EvidenceSelection", "EvidenceReading", "Fact"),
        "planning": ("Calculation", "CalculationValue", "RelationshipPlan", "Application", "ApplicationReview", "ReviewField", "Quote"),
        "authority": ("BankDecision", "Consent", "ExecutionReceipt", "LifecycleEvent"),
        "servicing": ("ServiceCase", "CRMContinuity"),
        "frontier": (
            "FrontierJourney", "ProactiveCandidate", "HouseholdAccessGrant",
            "FrontierConsent", "RegisteredAgent", "FrontierMandate",
            "PreparedAction", "MandateAction", "HumanHandoff", "HumanDecision",
            "FrontierOutcome", "AgentAction",
        ),
    }.items()
    for entity in entities
}
COMMON_COLUMNS = ("id", "label", "state", "sourceKind", "historical", "sourceRecordId", "sourceVersion")
INDEX_COLUMNS = ("id", "entityType")
LINK_COLUMNS = ("id", "sourceRecordId", "targetRecordId", "relation", "reason")


def validate_scalar(value: Any, kind: str) -> Any:
    from datetime import date

    if value is None:
        return None
    if kind == "integer":
        return integer(value)
    if kind == "minor":
        return minor_string(value, transported=True)
    if kind == "decimal":
        if not isinstance(value, str) or decimal_string(value) != value:
            raise ProjectionError("Decimal transport must be a canonical string.")
    elif kind == "boolean":
        if type(value) is not bool:
            raise ProjectionError("Invalid boolean.")
    elif kind in {"string", "date", "datetime"}:
        if not isinstance(value, str) or len(value) > 16000 or "\x00" in value:
            raise ProjectionError("Invalid scalar text.")
        if kind == "datetime":
            timestamp(value)
        elif kind == "date":
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except ValueError as exc:
                raise ProjectionError("Invalid ISO date.") from exc
    else:
        raise ProjectionError("Unsupported attribute type.")
    return value


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, populate_by_name=True)


class Source(Contract):
    dataset: StrictStr
    recordId: StrictStr
    path: StrictStr
    version: StrictStr | None = None

    @model_validator(mode="after")
    def safe_locator(self) -> Source:
        identifier(self.recordId)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,100}", self.dataset):
            raise ProjectionError("Invalid logical dataset.")
        if not self.path.startswith("/") or "\\" in self.path or "\x00" in self.path or ".." in self.path:
            raise ProjectionError("Invalid logical source pointer.")
        if self.version is not None and len(self.version) > 256:
            raise ProjectionError("Invalid source version.")
        return self


class Record(Contract):
    id: StrictStr
    entityType: StrictStr
    label: StrictStr
    family: StrictStr
    state: StrictStr
    sourceKind: SourceKind
    historical: StrictBool
    attributes: dict[str, Scalar]
    source: Source
    definitionRef: StrictStr
    domainIds: list[StrictStr] = Field(default_factory=list)
    redactedFields: list[StrictStr] = Field(default_factory=list)
    warnings: list[StrictStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def registered_attributes(self) -> Record:
        fields = ENTITY_FIELDS.get(self.entityType)
        if fields is None or set(self.attributes) - fields.keys():
            raise ProjectionError("Unregistered record attributes.")
        if self.family != ENTITY_FAMILIES[self.entityType]:
            raise ProjectionError("Incorrect record family.")
        if self.definitionRef != f"banking.cdm.json/{self.entityType}":
            raise ProjectionError("Incorrect definition reference.")
        if not re.fullmatch(r"bx-[0-9a-f]{40}", self.id):
            raise ProjectionError("Invalid opaque record identity.")
        for name, value in self.attributes.items():
            validate_scalar(value, fields[name])
        return self


class Edge(Contract):
    id: StrictStr
    from_id: StrictStr = Field(alias="from")
    to: StrictStr
    relation: StrictStr
    label: StrictStr
    reason: StrictStr
    source: Source


class SnapshotMeta(Contract):
    snapshotId: StrictStr
    ownerId: StrictStr
    scenarioGeneration: StrictStr
    revision: Annotated[StrictInt, Field(ge=0, le=MAX_INTEGER)]
    updatedAt: StrictStr
    businessAsOf: StrictStr
    observedAt: StrictStr
    seedReleaseId: StrictStr
    buildFingerprint: StrictStr
    packageFingerprint: StrictStr | None
    projectionVersion: StrictStr = PROJECTION_VERSION
    profileVersion: StrictStr
    mappingVersion: StrictStr
    metadataFingerprint: StrictStr
    bianRelease: StrictStr = BIAN_RELEASE


class SnapshotQuery(Contract):
    expected: StrictStr | None = None
    owner: StrictStr | None = None
    mode: Literal["customer", "lineage"] = "customer"
    q: Annotated[StrictStr, Field(max_length=200)] = ""
    family: Annotated[StrictStr, Field(max_length=128)] = ""
    entityType: Annotated[StrictStr, Field(max_length=128)] = ""
    domain: Annotated[StrictStr, Field(max_length=128)] = ""
    sourceKind: Annotated[StrictStr, Field(max_length=128)] = ""
    state: Annotated[StrictStr, Field(max_length=128)] = ""
    focus: Annotated[StrictStr, Field(max_length=128)] = ""
    depth: Annotated[StrictInt, Field(ge=1, le=3)] = 1
    offset: Annotated[StrictInt, Field(ge=0, le=1_000_000)] = 0
    limit: Annotated[StrictInt, Field(ge=1, le=PAGE_SIZE)] = PAGE_SIZE
    graphLimit: Annotated[StrictInt, Field(ge=1, le=MAX_NODES)] = 60

    @model_validator(mode="after")
    def query_shape(self) -> SnapshotQuery:
        for value in (self.q, self.family, self.entityType, self.domain, self.sourceKind, self.state, self.focus):
            if any(ord(char) < 32 for char in value):
                raise InvalidQuery()
        if self.expected is not None and (not self.expected or len(self.expected) > 128):
            raise InvalidQuery()
        if self.owner is not None:
            identifier(self.owner)
        if self.family and self.family not in FAMILIES:
            raise InvalidQuery()
        if self.entityType and self.entityType not in ENTITY_FIELDS:
            raise InvalidQuery()
        if self.sourceKind and self.sourceKind not in get_args(SourceKind):
            raise InvalidQuery()
        if (self.offset or self.focus or self.depth != 1) and not self.expected:
            raise InvalidQuery()
        return self


class Projection(Contract):
    meta: SnapshotMeta
    records: tuple[Record, ...]
    edges: tuple[Edge, ...]
    warnings: tuple[StrictStr, ...] = ()


def concrete_row(record: Record | dict) -> dict[str, Scalar]:
    """One concrete business row; shared CSV columns are coalesced explicitly."""
    value = record.model_dump() if isinstance(record, Record) else record
    attributes = value["attributes"]
    row = {
        "id": value["id"], "label": value["label"], "state": value["state"],
        "sourceKind": value["sourceKind"], "historical": value["historical"],
        "sourceRecordId": value["source"]["recordId"], "sourceVersion": value["source"]["version"],
    }
    for name, attribute in attributes.items():
        if name in COMMON_COLUMNS:
            if name not in {"label", "sourceVersion"}:
                raise ProjectionError("An entity collides with an export bookkeeping column.")
            if row[name] is not None and attribute is not None and row[name] != attribute:
                raise ProjectionError("A shared concrete column has conflicting values.")
            if row[name] is None:
                row[name] = attribute
        else:
            row[name] = attribute
    return row


def entity_columns(entity: str, definitions: Mapping[str, dict]) -> list[str]:
    if entity == "RecordIndex":
        return list(INDEX_COLUMNS)
    if entity == "RecordLink":
        return list(LINK_COLUMNS)
    definition = definitions.get(entity)
    if not isinstance(definition, dict) or definition.get("family") != ENTITY_FAMILIES.get(entity):
        raise ProjectionError("The model does not describe the installed entity family.")
    attributes = definition.get("attributes")
    if not isinstance(attributes, list):
        raise ProjectionError("The model's attribute registry is malformed.")
    fields = ENTITY_FIELDS[entity]
    declared = {}
    for attribute in attributes:
        if not isinstance(attribute, dict):
            raise ProjectionError("The model's attribute registry is malformed.")
        name = attribute.get("name")
        if (
            not isinstance(name, str) or name in declared or name not in fields
            or attribute.get("type") != fields[name] or type(attribute.get("nullable")) is not bool
        ):
            raise ProjectionError("The model's scalar contract differs from the installed projection.")
        declared[name] = attribute
    if set(declared) != set(fields):
        raise ProjectionError("The model's scalar contract is incomplete.")
    return [*COMMON_COLUMNS, *(name for name in declared if name not in COMMON_COLUMNS)]


def validate_concrete_rows(
    entity: str, rows: Iterable[Mapping[str, Scalar]], definitions: Mapping[str, dict],
) -> None:
    columns = entity_columns(entity, definitions)
    bookkeeping = entity in {"RecordIndex", "RecordLink"}
    attributes = {} if bookkeeping else {row["name"]: row for row in definitions[entity]["attributes"]}
    seen = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != set(columns):
            raise ProjectionError("An exported row differs from its registered columns.")
        for name in columns:
            if bookkeeping or name in COMMON_COLUMNS:
                kind = "boolean" if name == "historical" else "string"
                nullable = not bookkeeping and name == "sourceVersion"
            else:
                kind, nullable = attributes[name]["type"], attributes[name]["nullable"]
            value = row[name]
            if value is None and not nullable:
                raise ProjectionError("A required exported value is absent.")
            validate_scalar(value, kind)
        key = row["id"]
        if not isinstance(key, str) or not key or key in seen:
            raise ProjectionError("An exported key is empty or duplicated.")
        seen.add(key)
        if entity == "RecordIndex":
            if row["entityType"] not in ENTITY_FIELDS:
                raise ProjectionError("An index row references an undefined concrete entity.")
        elif entity == "RecordLink":
            if not row["sourceRecordId"] or not row["targetRecordId"] or not row["reason"]:
                raise ProjectionError("An exported link is missing its endpoints or provenance.")
            identifier(row["relation"])
        else:
            if not row["sourceRecordId"] or row["sourceKind"] not in get_args(SourceKind):
                raise ProjectionError("An exported row is missing valid source provenance.")
            if any(row.get(name) is not None for name, kind in ENTITY_FIELDS[entity].items() if kind == "minor"):
                if row.get("currency") != "INR":
                    raise ProjectionError("The installed banking profile requires explicit INR minor units.")
