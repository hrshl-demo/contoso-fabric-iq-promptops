"""Authority and state contracts for the Frontier Retail experience."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue, StrictInt, model_validator

from app.agentic.contracts import Contract, Identifier, Minor

FrontierJourneyId = Literal["F01", "F02", "F03", "F04", "F05", "F06"]
SpecialistId = Literal[
    "financial_health",
    "home_family_lending",
    "debt_optimization",
    "wealth_protection_retirement",
    "fraud_dispute_recovery",
    "consent_mandate_handoff",
]
SpecialistRole = Literal[
    "fraud_specialist",
    "mortgage_specialist",
    "financial_adviser",
    "service_specialist",
]
HumanAction = Literal["accept", "modify", "reject", "request_information", "return_to_customer"]
HandoffContextSection = Literal[
    "situation",
    "goal",
    "calculations",
    "assumptions",
    "documents",
    "attempts",
    "risk_and_compliance",
    "consents",
    "decision_required",
]
Permission = Literal[
    "view_summary",
    "view_details",
    "prepare_action",
    "approve_action",
    "execute_action",
    "emergency_only",
]
AutonomyLevel = Annotated[StrictInt, Field(ge=1, le=6)]
SignedMinor = Annotated[StrictInt, Field(ge=-100_000_000_000, le=100_000_000_000)]


class FrontierJourneyDefinition(Contract):
    journeyId: FrontierJourneyId
    title: Annotated[str, Field(min_length=1, max_length=120)]
    oneLiner: Annotated[str, Field(min_length=1, max_length=300)]
    mandatoryOutcomes: Annotated[list[str], Field(min_length=1, max_length=8)]
    specialists: Annotated[list[SpecialistId], Field(min_length=1, max_length=4)]
    autonomyCeiling: AutonomyLevel
    serviceFirst: bool = False
    riskContext: Literal["ordinary", "fraud", "distress"] = "ordinary"
    openers: Annotated[list[str], Field(min_length=1, max_length=6)]

    @model_validator(mode="after")
    def unique_values(self) -> FrontierJourneyDefinition:
        if len(set(self.mandatoryOutcomes)) != len(self.mandatoryOutcomes):
            raise ValueError("Frontier journey outcomes must be unique.")
        if len(set(self.specialists)) != len(self.specialists):
            raise ValueError("Frontier journey specialists must be unique.")
        if self.riskContext != "ordinary" and not self.serviceFirst:
            raise ValueError("Fraud or distress journeys must be service-first.")
        return self


class HouseholdAccessGrant(Contract):
    grantId: Identifier
    ownerId: Identifier
    subjectId: Identifier
    subjectKind: Literal["household_member", "caregiver", "external_agent"]
    resourceScopes: Annotated[list[Identifier], Field(min_length=1, max_length=32)]
    permissions: Annotated[list[Permission], Field(min_length=1, max_length=6)]
    validFrom: str
    expiresAt: str | None = None
    revokedAt: str | None = None
    createdAt: str

    @model_validator(mode="after")
    def bounded_authority(self) -> HouseholdAccessGrant:
        if len(set(self.resourceScopes)) != len(self.resourceScopes):
            raise ValueError("Household resource scopes must be unique.")
        if len(set(self.permissions)) != len(self.permissions):
            raise ValueError("Household permissions must be unique.")
        if "execute_action" in self.permissions and "approve_action" not in self.permissions:
            raise ValueError("Execution authority requires approval authority.")
        return self


class ConsentRecord(Contract):
    consentId: Identifier
    ownerId: Identifier
    subjectKind: Literal["bank_agent", "household_member", "external_agent", "human_specialist"]
    subjectId: Identifier
    purpose: Annotated[str, Field(min_length=1, max_length=300)]
    resourceRefs: Annotated[list[Identifier], Field(min_length=1, max_length=32)]
    permissions: Annotated[list[Permission], Field(min_length=1, max_length=6)]
    recordedAt: str
    expiresAt: str | None = None
    revokedAt: str | None = None
    evidenceRefs: Annotated[list[Identifier], Field(max_length=16)] = Field(default_factory=list)


class RegisteredAgent(Contract):
    agentId: Identifier
    ownerId: Identifier
    displayName: Annotated[str, Field(min_length=1, max_length=120)]
    agentCardUrl: Annotated[str, Field(min_length=1, max_length=1000)]
    identityRef: Identifier
    permittedData: Annotated[list[Identifier], Field(min_length=1, max_length=32)]
    permittedActions: Annotated[list[Identifier], Field(max_length=16)]
    transactionLimitMinor: Minor = 0
    validFrom: str
    expiresAt: str
    status: Literal["active", "suspended", "revoked"]
    humanAccountability: Annotated[str, Field(min_length=1, max_length=300)]
    registeredAt: str
    revokedAt: str | None = None

    @model_validator(mode="after")
    def active_agent_is_not_revoked(self) -> RegisteredAgent:
        if self.status == "active" and self.revokedAt is not None:
            raise ValueError("An active agent cannot carry a revocation timestamp.")
        if self.status == "revoked" and self.revokedAt is None:
            raise ValueError("A revoked agent requires a revocation timestamp.")
        return self


class SurplusToGoalMandate(Contract):
    mandateId: Identifier
    ownerId: Identifier
    agentId: Identifier
    journeyId: Literal["F01", "F06"]
    consentId: Identifier
    sourceAccountId: Identifier
    targetGoalId: Identifier
    protectedCashMinor: Minor
    perExecutionLimitMinor: Minor
    monthlyLimitMinor: Minor
    effectiveFrom: str
    expiresAt: str
    status: Literal["draft", "active", "paused", "expired", "revoked"]
    revision: Annotated[StrictInt, Field(ge=1)]
    createdAt: str
    updatedAt: str
    revokedAt: str | None = None

    @model_validator(mode="after")
    def enforce_limits(self) -> SurplusToGoalMandate:
        if self.perExecutionLimitMinor <= 0 or self.monthlyLimitMinor <= 0:
            raise ValueError("A mandate requires positive execution and monthly limits.")
        if self.perExecutionLimitMinor > self.monthlyLimitMinor:
            raise ValueError("Per-execution authority cannot exceed the monthly authority.")
        if self.status == "revoked" and self.revokedAt is None:
            raise ValueError("A revoked mandate requires a revocation timestamp.")
        if self.status == "active" and self.revokedAt is not None:
            raise ValueError("An active mandate cannot carry a revocation timestamp.")
        return self


class ProactiveCandidate(Contract):
    candidateId: Identifier
    ownerId: Identifier
    journeyId: FrontierJourneyId
    kind: Identifier
    riskLevel: Literal["low", "medium", "high", "critical"]
    priorityScore: Annotated[StrictInt, Field(ge=0, le=100)]
    sourceRefs: Annotated[list[Identifier], Field(min_length=1, max_length=32)]
    calculationRefs: Annotated[list[Identifier], Field(max_length=16)] = Field(default_factory=list)
    assumptionRefs: Annotated[list[Identifier], Field(max_length=16)] = Field(default_factory=list)
    detectedAt: str
    expiresAt: str
    status: Literal["active", "suppressed", "dismissed", "completed"]
    suppressionReason: Annotated[str | None, Field(max_length=300)] = None


class CalculationOutput(Contract):
    calculationId: Identifier
    ownerId: Identifier
    journeyInstanceId: Identifier
    journeyId: FrontierJourneyId
    journeyRevision: Annotated[StrictInt, Field(ge=1)]
    outputName: Identifier
    values: dict[str, JsonValue]
    assumptions: dict[str, JsonValue] = Field(default_factory=dict)
    sourceRefs: Annotated[list[Identifier], Field(min_length=1, max_length=128)]
    policyVersion: Identifier
    calculatedAt: str


class AssumptionChange(Contract):
    assumptionChangeId: Identifier
    ownerId: Identifier
    journeyInstanceId: Identifier
    fromRevision: Annotated[StrictInt, Field(ge=0)]
    toRevision: Annotated[StrictInt, Field(ge=1)]
    beforeAssumptions: dict[str, JsonValue] = Field(default_factory=dict)
    afterAssumptions: dict[str, JsonValue] = Field(default_factory=dict)
    changedKeys: Annotated[list[Identifier], Field(max_length=32)] = Field(default_factory=list)
    revertedFromRevision: Annotated[StrictInt | None, Field(ge=1)] = None
    recordedAt: str


class PreparedAction(Contract):
    actionId: Identifier
    ownerId: Identifier
    journeyId: FrontierJourneyId
    operation: Identifier
    authorityRequired: Literal["customer_approval", "active_mandate", "human_review"]
    amountMinor: Minor = 0
    currency: Literal["INR"] = "INR"
    sourceRefs: Annotated[list[Identifier], Field(min_length=1, max_length=32)]
    calculationRefs: Annotated[list[Identifier], Field(max_length=16)] = Field(default_factory=list)
    journeyRevision: Annotated[StrictInt, Field(ge=1)] = 1
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    policyVersion: Identifier
    preparedAt: str
    expiresAt: str
    status: Literal["prepared", "approved", "executed", "expired", "cancelled", "referred"]


class CustomerDecisionRecord(Contract):
    decisionId: Identifier
    ownerId: Identifier
    journeyInstanceId: Identifier
    preparedActionId: Identifier
    decision: Literal["approve", "reject"]
    actionJourneyRevision: Annotated[StrictInt, Field(ge=1)]
    recordedAt: str


class MandateActionRecord(Contract):
    actionRecordId: Identifier
    ownerId: Identifier
    mandateId: Identifier
    candidateId: Identifier
    preparedActionId: Identifier
    amountMinor: Minor
    protectedCashBeforeMinor: Minor
    protectedCashAfterMinor: Minor
    monthlyUsedBeforeMinor: Minor
    monthlyUsedAfterMinor: Minor
    status: Literal["executed", "refused", "referred"]
    reasons: Annotated[list[str], Field(min_length=1, max_length=8)]
    receiptId: Identifier | None = None
    recordedAt: str

    @model_validator(mode="after")
    def executed_action_has_receipt(self) -> MandateActionRecord:
        if self.status == "executed" and self.receiptId is None:
            raise ValueError("Executed mandate actions require a receipt.")
        if self.monthlyUsedAfterMinor < self.monthlyUsedBeforeMinor:
            raise ValueError("A mandate action cannot reduce recorded monthly use.")
        return self


class FrontierJourneyState(Contract):
    journeyInstanceId: Identifier
    ownerId: Identifier
    journeyId: FrontierJourneyId
    revision: Annotated[StrictInt, Field(ge=1)]
    status: Literal["active", "waiting_customer", "waiting_human", "completed", "cancelled"]
    goals: Annotated[list[str], Field(min_length=1, max_length=8)]
    assumptions: dict[str, JsonValue] = Field(default_factory=dict)
    missingFields: Annotated[list[Identifier], Field(max_length=16)] = Field(default_factory=list)
    calculationRefs: Annotated[list[Identifier], Field(max_length=32)] = Field(default_factory=list)
    preparedActionRefs: Annotated[list[Identifier], Field(max_length=16)] = Field(default_factory=list)
    handoffId: Identifier | None = None
    createdAt: str
    updatedAt: str


class HandoffDocument(Contract):
    documentId: Identifier
    title: Annotated[str, Field(min_length=1, max_length=200)]
    sourceVersion: Annotated[str, Field(min_length=1, max_length=128)]
    status: Literal["available", "verified", "customer_supplied", "unavailable"]
    summary: Annotated[str, Field(min_length=1, max_length=800)]


class HandoffAttempt(Contract):
    attemptId: Identifier
    actorKind: Literal["bank_agent", "customer", "human_specialist", "bank_system"]
    actorId: Identifier
    action: Identifier
    outcome: Literal["completed", "blocked", "failed", "information_supplied"]
    detail: Annotated[str, Field(min_length=1, max_length=1200)]
    recordedAt: str


class HandoffContextPackage(Contract):
    situation: Annotated[str, Field(min_length=1, max_length=2000)]
    goal: Annotated[str, Field(min_length=1, max_length=800)]
    calculations: Annotated[dict[Identifier, JsonValue], Field(min_length=1, max_length=24)]
    assumptions: Annotated[dict[Identifier, JsonValue], Field(max_length=24)] = Field(default_factory=dict)
    documents: Annotated[list[HandoffDocument], Field(max_length=24)] = Field(default_factory=list)
    attempts: Annotated[list[HandoffAttempt], Field(min_length=1, max_length=24)]
    contextRefs: Annotated[list[Identifier], Field(min_length=1, max_length=48)]
    calculationRefs: Annotated[list[Identifier], Field(min_length=1, max_length=24)]
    consentRefs: Annotated[list[Identifier], Field(min_length=1, max_length=16)]
    riskFlags: Annotated[list[Identifier], Field(max_length=16)] = Field(default_factory=list)
    complianceFlags: Annotated[list[Identifier], Field(max_length=16)] = Field(default_factory=list)
    modelRecommendationRefs: Annotated[list[Identifier], Field(max_length=16)] = Field(default_factory=list)
    bankDecisionRefs: Annotated[list[Identifier], Field(max_length=16)] = Field(default_factory=list)
    sharedSections: Annotated[list[HandoffContextSection], Field(min_length=9, max_length=9)]
    decisionRequired: Annotated[str, Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def complete_context(self) -> HandoffContextPackage:
        if len(set(self.contextRefs)) != len(self.contextRefs):
            raise ValueError("Handoff context references must be unique.")
        if len(set(self.calculationRefs)) != len(self.calculationRefs):
            raise ValueError("Handoff calculation references must be unique.")
        if len(set(self.consentRefs)) != len(self.consentRefs):
            raise ValueError("Handoff consent references must be unique.")
        expected = {
            "situation", "goal", "calculations", "assumptions", "documents",
            "attempts", "risk_and_compliance", "consents", "decision_required",
        }
        if set(self.sharedSections) != expected:
            raise ValueError("A handoff must explicitly permission every required context section.")
        if not set(self.calculationRefs) <= set(self.contextRefs):
            raise ValueError("Calculation references must also be present in the shared context references.")
        return self


class HumanHandoff(Contract):
    schemaVersion: Literal["frontier-human-handoff-v1"] = "frontier-human-handoff-v1"
    handoffId: Identifier
    caseId: Identifier
    ownerId: Identifier
    journeyInstanceId: Identifier
    journeyId: FrontierJourneyId
    specialistRole: SpecialistRole
    revision: Annotated[StrictInt, Field(ge=1)]
    createdBy: Identifier
    situationSummary: Annotated[str, Field(min_length=1, max_length=2000)]
    goal: Annotated[str, Field(min_length=1, max_length=800)]
    decisionRequired: Annotated[str, Field(min_length=1, max_length=500)]
    contextRefs: Annotated[list[Identifier], Field(min_length=1, max_length=48)]
    calculations: Annotated[dict[Identifier, JsonValue], Field(min_length=1, max_length=24)]
    calculationRefs: Annotated[list[Identifier], Field(min_length=1, max_length=24)]
    assumptions: Annotated[dict[Identifier, JsonValue], Field(max_length=24)] = Field(default_factory=dict)
    documents: Annotated[list[HandoffDocument], Field(max_length=24)] = Field(default_factory=list)
    attempts: Annotated[list[HandoffAttempt], Field(min_length=1, max_length=24)]
    consentRefs: Annotated[list[Identifier], Field(min_length=1, max_length=16)]
    riskFlags: Annotated[list[Identifier], Field(max_length=16)] = Field(default_factory=list)
    complianceFlags: Annotated[list[Identifier], Field(max_length=16)] = Field(default_factory=list)
    modelRecommendationRefs: Annotated[list[Identifier], Field(max_length=16)] = Field(default_factory=list)
    bankDecisionRefs: Annotated[list[Identifier], Field(max_length=16)] = Field(default_factory=list)
    sharedSections: Annotated[list[HandoffContextSection], Field(min_length=9, max_length=9)]
    requestedInformation: Annotated[list[str], Field(max_length=16)] = Field(default_factory=list)
    informationResponses: dict[str, JsonValue] = Field(default_factory=dict)
    latestHumanDecisionId: Identifier | None = None
    status: Literal["open", "information_requested", "returned", "closed"]
    createdAt: str
    updatedAt: str
    returnedAt: str | None = None


class HumanDecision(Contract):
    schemaVersion: Literal["frontier-human-decision-v1"] = "frontier-human-decision-v1"
    recordType: Literal["human_decision"] = "human_decision"
    humanDecisionId: Identifier
    handoffId: Identifier
    ownerId: Identifier
    journeyInstanceId: Identifier
    reviewerId: Identifier
    specialistRole: SpecialistRole
    handoffRevision: Annotated[StrictInt, Field(ge=1)]
    commandId: Identifier
    action: HumanAction
    explanation: Annotated[str, Field(min_length=1, max_length=1200)]
    modifications: dict[str, JsonValue] = Field(default_factory=dict)
    requestedInformation: Annotated[list[str], Field(max_length=16)] = Field(default_factory=list)
    evidenceRefs: Annotated[list[Identifier], Field(max_length=16)] = Field(default_factory=list)
    relatedDecisionId: Identifier | None = None
    recordedAt: str

    @model_validator(mode="after")
    def action_payload_matches_disposition(self) -> HumanDecision:
        if self.action == "modify" and not self.modifications:
            raise ValueError("A modified human decision requires explicit modifications.")
        if self.action != "modify" and self.modifications:
            raise ValueError("Only a modified human decision can carry modifications.")
        if self.action == "request_information" and not self.requestedInformation:
            raise ValueError("An information request must name the information required.")
        if self.action != "request_information" and self.requestedInformation:
            raise ValueError("Only an information request can carry requested information.")
        if self.action == "return_to_customer" and self.relatedDecisionId is None:
            raise ValueError("Returning a journey requires the human decision being returned.")
        return self


class OutcomeRecord(Contract):
    outcomeId: Identifier
    ownerId: Identifier
    journeyInstanceId: Identifier
    journeyId: FrontierJourneyId
    outcomeType: Literal[
        "financial_health_improved",
        "goal_progressed",
        "debt_cost_reduced",
        "risk_mitigated",
        "service_resolved",
        "authority_revoked",
        "no_action",
    ]
    amountImpactMinor: SignedMinor = 0
    metricValues: dict[str, JsonValue] = Field(default_factory=dict)
    sourceRefs: Annotated[list[Identifier], Field(min_length=1, max_length=32)]
    recordedAt: str
    demoOnly: Literal[True] = True


FrontierToolName = Literal[
    "context.read",
    "calculation.run",
    "journey.read",
    "journey.create",
    "journey.update",
    "action.prepare",
    "decision.request",
    "consent.record",
    "action.execute",
    "mandate.execute",
    "case.escalate",
    "case.read",
    "authority.read",
    "outcome.record",
]


class AgentToolRequest(Contract):
    requestId: Identifier
    tool: FrontierToolName
    ownerId: Identifier
    journeyInstanceId: Identifier | None = None
    householdScopeId: Identifier
    expectedRevision: Annotated[StrictInt | None, Field(ge=1)] = None
    idempotencyKey: Identifier
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    traceId: Identifier


class AgentToolResponse(Contract):
    requestId: Identifier
    status: Literal["ok", "conflict", "forbidden", "needs_approval", "referred", "error"]
    result: dict[str, JsonValue] = Field(default_factory=dict)
    revision: Annotated[StrictInt | None, Field(ge=1)] = None
    sourceRefs: Annotated[list[Identifier], Field(max_length=32)] = Field(default_factory=list)
    auditId: Identifier
    message: Annotated[str, Field(max_length=500)] = ""


class CompanionCitation(Contract):
    title: Annotated[str, Field(min_length=1, max_length=300)]
    source: Annotated[str, Field(min_length=1, max_length=1000)]


class CompanionResponse(Contract):
    answer: Annotated[str, Field(min_length=1, max_length=4000)]
    journeyId: FrontierJourneyId
    specialistsConsulted: Annotated[list[Identifier], Field(max_length=8)]
    citations: Annotated[list[CompanionCitation], Field(max_length=24)]
    calculationRefs: Annotated[list[Identifier], Field(max_length=24)] = Field(default_factory=list)
    evidenceRefs: Annotated[list[Identifier], Field(max_length=24)] = Field(default_factory=list)
    preparedActionRefs: Annotated[list[Identifier], Field(max_length=12)] = Field(default_factory=list)
    consentState: Literal["not-required", "required", "captured", "declined"]
    nextSteps: Annotated[list[str], Field(max_length=8)]
    handoffRequested: bool = False
    handoffReason: Annotated[str | None, Field(max_length=500)] = None

    @model_validator(mode="after")
    def handoff_reason_matches_request(self) -> CompanionResponse:
        if self.handoffRequested and not self.handoffReason:
            raise ValueError("A requested human handoff requires a reason.")
        if not self.handoffRequested and self.handoffReason is not None:
            raise ValueError("A handoff reason is valid only when handoff is requested.")
        return self
