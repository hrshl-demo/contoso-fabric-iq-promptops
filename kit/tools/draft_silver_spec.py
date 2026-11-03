"""One-off drafting aid for model/silver/*.yaml: gold column definitions + the CDM profile -> silver table drafts.

The committed YAML under model/silver is hand-curated from this draft and is the source of truth: nothing at runtime reads this
script, and re-running it would overwrite curation, so it writes to a directory you name and refuses to touch model/silver.

    python tools/draft_silver_spec.py --repo <snapshot of contoso-retail-superapp4> --out <empty dir>

A profile attribute is mapped only where the seed field means exactly what the profile says (PROFILE maps below); every other profile
attribute stays null and is labelled `none:`. Every other seed field a served gold table reads becomes an extension column (`x` +
PascalCase on profile entities, plain camelCase on Contoso extension entities), so silver is a superset of what gold needs. The
`common:` block it writes is only the app's put() default skeleton: the per-entity sourceKind/state/label/historical rules are curated by
hand from app/data_xray/projection.py and cited line by line in the committed YAML.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))

from contoso_onto import standards  # noqa: E402
from contoso_onto.gold import TPL_RE, Repo, load_model  # noqa: E402
from contoso_onto.silver import COMMON_COLUMNS, Profile, silver_source_rows  # noqa: E402

FR, L4 = "retail-frontier-v1", "retail4-v1"
NONE = "not recorded in the seed"
PAISE = {"t": "paise"}
RUPEES = {"t": "rupees_to_paise"}
BPS = {"t": "bps_to_pct_text"}
DEC = {"t": "decimal_text"}
MINOR_UNIT = "currency minor units; INR paise, exponent 2"
SEED_CONTEXT = {"$release", "$index", "$source_kind", "$segment", "$kind", "$is_denied", "$data_role", "$ordinal"}
NOT_RECORDED_OUTCOME = "not recorded (the fixtures carry no runtime outcome)"
REASONS = {
    ("Card", "prepaidBalanceMinor"): "no prepaid card in the seed",
    ("Deposit", "monthlyInstalmentMinor"): "no recurring deposit in the seed",
    ("Deposit", "depositType"): "not recorded; the profile's catalogue-family derivation needs the product catalogue (a gold-side join)",
    ("Loan", "linkedAccountId"): "loans.json names no servicing account",
    ("Loan", "principalMinor"): "openingPrincipal is the principal at the start of the seeded history, not the original disbursed principal (kept as xOpeningPrincipalMinor)",
    ("Loan", "termMonths"): "not recorded; the seeded repayment schedule is only a window of the term",
    ("Loan", "version"): "the Frontier seed records no revision",
    ("ProtectionPolicy", "futureCoverOnly"): "not recorded (absence is neither true nor false)",
    ("ProtectionPolicy", "claimsApproved"): "not recorded (absence is neither a denial nor an approval)",
    ("Transaction", "recordedAt"): "not recorded; never fabricated from valueDate",
    ("FrontierConsent", "subjectKind"): "the seed records the subject id only; gold derives the kind from the id prefix",
    ("HouseholdAccessGrant", "subjectKind"): "the seed records the subject id only; gold derives the kind from the id prefix",
    ("HouseholdAccessGrant", "validFrom"): "grants carry no timestamps in the seed",
    ("HouseholdAccessGrant", "expiresAt"): "grants carry no timestamps in the seed",
    ("HouseholdAccessGrant", "revokedAt"): "grants carry no timestamps in the seed",
    ("HouseholdAccessGrant", "createdAt"): "grants carry no timestamps in the seed",
    ("RegisteredAgent", "displayName"): "not recorded (the registration type is kept as xRegistrationType)",
    ("RegisteredAgent", "permittedData"): "the seed lists combined action:resource scopes (xScopes and silver_agent_scope), not separate data and action lists",
    ("RegisteredAgent", "permittedActions"): "the seed lists combined action:resource scopes (xScopes and silver_agent_scope), not separate data and action lists",
    ("RegisteredAgent", "transactionLimitMinor"): "no per-agent limit in the seed (limits live on the mandate)",
    ("RegisteredAgent", "currency"): "the agent carries no money field",
    ("RegisteredAgent", "humanAccountability"): "not recorded",
    ("RegisteredAgent", "registeredAt"): "the seed records only the KYA verification time (xKyaVerifiedAt), which is not the registration time",
    ("FrontierMandate", "createdAt"): "not a field; the creation time is only implied by the created event in the history",
    ("FrontierMandate", "revokedAt"): "not a field; the revocation time is only implied by the revoked event in the history",
    ("MandateAction", "status"): "the seed event types (created, action_executed, action_skipped, revoked) are not the profile's executed, refused or referred (kept as xEventType)",
    ("ServiceCase", "serviceRequestType"): "the seed records the contact channel, not a request type",
    ("ServiceCase", "resolutionStatus"): "no resolution recorded: a recorded action is not a completed effect",
    ("ProactiveCandidate", "detectedAt"): "the seed has a business date (xAsOf), not a detection instant",
    ("ProactiveCandidate", "expiresAt"): NOT_RECORDED_OUTCOME,
    ("ProactiveCandidate", "status"): NOT_RECORDED_OUTCOME,
    ("ProactiveCandidate", "suppressionReason"): NOT_RECORDED_OUTCOME,
    ("Document", "previewable"): "viewer capability is not recorded in the seed",
}

# silver table -> definition. `gold` lists the served v1 gold tables (the first one drives sources and extension columns).
TABLES: dict[str, dict] = {
    "silver_customer": dict(entity="Customer", profile="Customer", gold=["customer"], sources=slice(0, 2), key=["ownerId"], owner="ownerId",
                            label=["displayName", "name"], grain="One persona record per seed release.",
                            pmap={"ownerId": "ownerId", "displayName": ["displayName", "name"], "city": "city", "cohort": "cohort"},
                            rename={"age": "AgeYears"}, unit={"age": "years"},
                            notes="CRM-only parties (a name on a CRM case with no persona record) are not profile Customer records: they come from "
                                  "silver_crm_case (customerId, customerName) and gold applies the same precedence by source order as v1."),
    "silver_household": dict(entity="Household", profile=None, gold=["household"], key=["householdId"], owner="ownerId", keep_owner=True,
                             grain="One household per Frontier primary-owner persona record.",
                             rename={"name": "primaryOwnerName", "ownerId": "primaryOwnerId"}, flags={"name": {"pii": True}},
                             notes="Derived from the Frontier persona record that names the household: the seed has no household file "
                                   "(its members are in silver_household_member)."),
    "silver_household_member": dict(entity="HouseholdMember", profile=None, gold=["household_member"], key=["memberId"],
                                    owner="primaryOwnerId", keep_owner=True, label=["name"], grain="One household member record."),
    "silver_account": dict(entity="Account", profile="Account", gold=["account"], key=["accountId"], owner="ownerId", state="status",
                           version="version", grain="One account record per seed release.",
                           pmap={"accountId": "accountId", "productId": "productId",
                                 "currency": ["currency", "postedBalance.currency", "availableBalance.currency"],
                                 "postedBalanceMinor": ("postedBalance", PAISE), "availableBalanceMinor": ("availableBalance", PAISE),
                                 "holdsMinor": ("holds", PAISE), "openedOn": "openedOn", "version": "version"},
                           desc={"balanceAsOf": "Date the balances are valid for, as the seed records it (Frontier release only); see xReleaseBusinessDate."}),
    "silver_card": dict(entity="Card", profile="Card", gold=["card"], key=["cardId"], owner="ownerId", state="status", version="version",
                        grain="One card record per seed release.",
                        pmap={"cardId": "cardId", "productId": "productId", "linkedAccountId": ["linkedAccountId", "accountId"],
                              "cardType": "cardType", "maskedNumber": "maskedNumber",
                              "currency": ["creditLimit.currency", "currentOutstanding.currency", "currentBalance.currency",
                                           "availableCredit.currency"],
                              "limitMinor": ("creditLimit", PAISE), "outstandingMinor": (["currentOutstanding", "currentBalance"], PAISE),
                              "availableCreditMinor": ("availableCredit", PAISE), "prepaidBalanceMinor": None, "expiry": "expiry",
                              "version": "version"}),
    "silver_deposit": dict(entity="Deposit", profile="Deposit", gold=["deposit"], key=["depositId"], owner="ownerId", state="status",
                           version="version", grain="One deposit record per seed release.",
                           pmap={"depositId": "depositId", "productId": "productId", "linkedAccountId": "linkedAccountId",
                                 "currency": "principal.currency", "principalMinor": ("principal", PAISE), "monthlyInstalmentMinor": None,
                                 "depositType": None, "openedOn": "openedOn", "maturesOn": "maturesOn",
                                 "ratePct": ("annualRateBps", BPS), "payoutMode": None, "version": "version"}),
    "silver_loan": dict(entity="Loan", profile="Loan", gold=["loan"], key=["liabilityId"], owner="ownerId",
                        grain="One loan facility per Frontier seed record.",
                        pmap={"liabilityId": "loanId", "productId": "productId", "linkedAccountId": None,
                              "currency": ["currentPrincipal.currency", "openingPrincipal.currency", "emi.currency"],
                              "principalMinor": None, "outstandingMinor": ("currentPrincipal", PAISE), "monthlyPaymentMinor": ("emi", PAISE),
                              "ratePct": ("annualRateBps", BPS), "termMonths": None, "openedOn": None, "maturesOn": None, "version": None}),
    "silver_loan_installment": dict(entity="LoanInstallment", profile=None, gold=["loan_installment"], key=["loanId", "installmentNumber"],
                                    owner="^.ownerId", state="status", record_id="{^.loanId}#{$index:02d}",
                                    grain="One scheduled or paid instalment of a loan (element of repaymentSchedule).",
                                    child={"loanId": "^.loanId", "ordinal": "$index"},
                                    notes="The closest seed analogue of the profile Commitment repayment-schedule row (liabilities.repaymentSchedule), "
                                          "but it splits principal, interest and fees, which the profile row does not, so it stays an extension entity."),
    "silver_protection_policy": dict(entity="ProtectionPolicy", profile="ProtectionPolicy", gold=["insurance_policy"], key=["policyId"],
                                     owner="ownerId", state="status", version="version",
                                     grain="One insurance policy record per seed release.",
                                     pmap={"policyId": "policyId", "productId": "productId", "linkedAccountId": "linkedAccountId",
                                           "coverType": ["coverType", "policyType"], "currency": ["premium.currency", "sumInsured.currency"],
                                           "premiumMinor": ("premium", PAISE), "sumInsuredMinor": ("sumInsured", PAISE),
                                           "deductibleMinor": None, "premiumFrequency": ["premiumFrequency", "premiumCadence"],
                                           "startedOn": "startedOn", "renewsOn": "renewsOn", "futureCoverOnly": None, "claimsApproved": None,
                                           "version": "version"}),
    "silver_protection_gap": dict(entity="ProtectionGap", profile=None, gold=["protection_gap"], key=["protectionGapId"], owner="ownerId",
                                  grain="One computed protection gap per customer and cover type at the Frontier business date."),
    "silver_investment": dict(entity="Investment", profile=None, gold=["investment"], key=["investmentId"], owner="ownerId",
                              grain="One investment holding."),
    "silver_investment_goal": dict(entity="InvestmentGoal", profile=None, gold=["investment_goal"], key=["investmentId", "goalId"],
                                   owner="^.ownerId", record_id="{^.investmentId}>{@}",
                                   grain="One goal funded by an investment (element of goalRefs).",
                                   child={"investmentId": "^.investmentId", "goalId": "@", "ordinal": "$index"}),
    "silver_transaction": dict(entity="Transaction", profile="Transaction", gold=["transaction"], key=["transactionId"], owner="ownerId",
                               state="status", label=["description"], grain="One ledger posting per seed release.",
                               pmap={"transactionId": "transactionId", "accountId": "accountId", "cardId": "cardId", "applicationId": None,
                                     "currency": "amount.currency", "amountMinor": ("amount", PAISE), "direction": "direction",
                                     "category": "category", "description": "description", "valueDate": "valueDate",
                                     "postedAt": "postedAt", "recordedAt": None, "transferId": None}),
    "silver_transaction_evidence": dict(entity="TransactionEvidence", profile=None, gold=["transaction_evidence"],
                                        key=["transactionId", "sourceRef"], owner="^.ownerId", record_id="{^.transactionId}>{@}",
                                        grain="One document or case that evidences a transaction (element of sourceRefs).",
                                        child={"transactionId": "^.transactionId", "sourceRef": "@", "ordinal": "$index"}),
    "silver_scheduled_obligation": dict(entity="ScheduledObligation", profile=None, gold=["scheduled_obligation"], key=["obligationId"],
                                        owner="ownerId", state="status", label=["name"], grain="One scheduled obligation.",
                                        notes="standards.ONTOLOGY_ENTITY maps the profile Commitment to this entity, but the profile's Commitment is 'an actual "
                                              "mandate or repayment-schedule row' read from the app's mandates and liabilities.repaymentSchedule datasets. "
                                              "These Frontier obligations are recurring commitments (cadence, day of month, payee reference, amount rule), so "
                                              "mapping them would mislabel them; modelled as an extension entity until that mapping is decided."),
    "silver_income_record": dict(entity="IncomeRecord", profile=None, gold=["income_record"], key=["incomeId"], owner="ownerId",
                                 grain="One monthly income record."),
    "silver_goal": dict(entity="Goal", profile="Goal", gold=["goal"], key=["goalId"], owner="ownerId", state="status", label=["title", "name"],
                        grain="One goal record per seed release.",
                        pmap={"goalId": "goalId", "title": ["title", "name"], "currency": ["targetAmount.currency", "target.currency", "current.currency"],
                              "targetAmountMinor": (["targetAmount", "target"], PAISE), "allocatedAmountMinor": ("current", PAISE),
                              "targetDate": "targetDate", "priority": "statedPriority"}, rename={"priority": "PriorityRank"}),
    "silver_preference": dict(entity="Preference", profile="Preference", gold=["preference"], key=["preferenceId"], owner="ownerId",
                              grain="One stated preference (field/value) per seed release.",
                              pmap={"preferenceId": "preferenceId", "field": "field", "value": "value", "recordedOn": "recordedOn"}),
    "silver_consent": dict(entity="Consent", profile="Consent", gold=["consent"], sources=slice(1, 2), key=["consentId"], owner="ownerId",
                           version="revision", grain="One legacy (retail4) consent record.",
                           notes="Two profile entities realise the ontology Consent: legacy consents (Consent, this table) and Frontier consents "
                                 "(FrontierConsent, silver_frontier_consent). Gold unions them.",
                           pmap={"consentId": "consentId", "applicationId": None, "planId": None, "itemId": None, "purpose": "purpose",
                                 "consentType": None, "reviewFingerprint": None, "receiptId": None, "at": ["at", "grantedAt"],
                                 "expiresAt": "expiresAt", "revokedAt": "revokedAt"}),
    "silver_frontier_consent": dict(entity="FrontierConsent", profile="FrontierConsent", gold=["consent"], sources=slice(0, 1),
                                    key=["consentId"], owner="ownerId", state="status", grain="One Frontier consent record.",
                                    desc={"status": "Status the seed records: granted or revoked."},
                                    notes="See silver_consent: the profile keeps Frontier consents distinct from legacy application consents.",
                                    pmap={"consentId": "consentId", "subjectKind": None, "subjectId": "subjectRef", "purpose": "purpose",
                                          "resourceRefs": None, "permissions": None, "recordedAt": "grantedAt", "expiresAt": "expiresAt",
                                          "revokedAt": "revokedAt", "evidenceRefs": None}),
    "silver_consent_resource": dict(entity="ConsentResource", profile=None, gold=["consent_resource"], key=["consentId", "sourceId"],
                                    owner="^.ownerId", record_id="{^.consentId}>{sourceId}",
                                    grain="One document a legacy consent covers (element of resourceRefs).",
                                    child={"consentId": "^.consentId", "ordinal": "$index"}, rename={"sourceVersion": "resourceVersion"}),
    "silver_household_access_grant": dict(entity="HouseholdAccessGrant", profile="HouseholdAccessGrant", gold=["access_grant"],
                                          key=["grantId"], owner="ownerId", state="status", grain="One household access grant.",
                                          pmap={"grantId": "grantId", "subjectId": "subjectRef", "subjectKind": None, "resourceScopes": None,
                                                "permissions": ("permissions", {"t": "join"}), "validFrom": None, "expiresAt": None,
                                                "revokedAt": None, "createdAt": None}),
    "silver_grant_permission": dict(entity="GrantPermission", profile=None, gold=["grant_permission"],
                                    key=["grantId", "permissionKind", "ordinal"], owner="^.ownerId",
                                    record_id="{^.grantId}#{$kind}#{$index:02d}",
                                    grain="One allowed or denied permission of an access grant (element of permissions or deniedPermissions).",
                                    child={"grantId": "^.grantId", "permission": "@", "permissionKind": "$kind", "isDenied": "$is_denied",
                                           "ordinal": "$index"}),
    "silver_registered_agent": dict(entity="RegisteredAgent", profile="RegisteredAgent", gold=["registered_agent"], key=["agentId"],
                                    owner="ownerId", state="status", grain="One registered (KYA) agent.",
                                    pmap={"agentId": "agentId", "displayName": None, "identityRef": None, "permittedData": None,
                                          "permittedActions": None, "transactionLimitMinor": None, "currency": None, "validFrom": None,
                                          "expiresAt": None, "status": "status", "humanAccountability": None, "registeredAt": None,
                                          "revokedAt": None}),
    "silver_agent_scope": dict(entity="AgentScope", profile=None, gold=["agent_scope"], key=["agentId", "scopeKind", "ordinal"],
                               owner="^.ownerId", record_id="{^.agentId}#{$kind}#{$index:02d}",
                               grain="One allowed or denied scope of a registered agent (element of scopes or deniedScopes).",
                               child={"agentId": "^.agentId", "scope": "@", "scopeKind": "$kind", "isDenied": "$is_denied", "ordinal": "$index"}),
    "silver_frontier_mandate": dict(entity="FrontierMandate", profile="FrontierMandate", gold=["mandate"], key=["mandateId"], owner="ownerId",
                                    state="status", grain="One bounded surplus-to-goal mandate.",
                                    pmap={"mandateId": "mandateId", "agentId": "agentId", "journeyId": None, "consentId": "consentRef",
                                          "sourceAccountId": "sourceAccountId", "targetGoalId": "goalId",
                                          "protectedCashMinor": ("limits.minimumSourceBalanceAfterAction", PAISE),
                                          "perExecutionLimitMinor": ("limits.perAction", PAISE),
                                          "monthlyLimitMinor": ("limits.monthlyAggregate", PAISE),
                                          "currency": ["currency", "limits.perAction.currency"], "effectiveFrom": "limits.validFrom",
                                          "expiresAt": "limits.validUntil", "status": "status", "revision": None, "createdAt": None,
                                          "updatedAt": None, "revokedAt": None}),
    "silver_mandate_action": dict(entity="MandateAction", profile="MandateAction", gold=["mandate_event"], key=["actionRecordId"],
                                  owner="^.ownerId", state="eventType", grain="One recorded event of a mandate (element of history).",
                                  notes="Mapped by standards.ONTOLOGY_ENTITY. Only executed and skipped events resemble the profile's mandate action; "
                                        "created and revoked events are kept as rows too (xEventType), with no amount or reason.",
                                  pmap={"actionRecordId": "eventId", "mandateId": "^.mandateId", "candidateId": None, "preparedActionId": None,
                                        "amountMinor": ("amount", PAISE), "protectedCashBeforeMinor": None,
                                        "protectedCashAfterMinor": ("sourceBalanceAfterAction", PAISE), "monthlyUsedBeforeMinor": None,
                                        "monthlyUsedAfterMinor": None,
                                        "currency": ["amount.currency", "sourceBalanceAfterAction.currency", "^.currency"], "status": None,
                                        "reasons": "reason", "receiptId": None, "recordedAt": "occurredAt"},
                                  extra=[dict(col="xOrdinal", origin="extension", type="integer", **{"from": "$index"},
                                              desc="1-based position of the event in the mandate's history.")]),
    "silver_incident": dict(entity="Incident", profile=None, gold=["incident"], key=["incidentId"], owner="ownerId", state="status",
                            grain="One fraud, scam or dispute case."),
    "silver_service_case": dict(entity="ServiceCase", profile="ServiceCase", gold=["service_record"], key=["serviceRequestId"],
                                owner="ownerId", grain="One servicing contact record.",
                                notes="Mapped by standards.ONTOLOGY_ENTITY (ServiceCase -> ServiceRecord): the closest profile entity for a Frontier "
                                      "servicing contact. Most profile attributes have no seed counterpart and stay null.",
                                pmap={"serviceRequestId": "serviceRecordId", "caseId": "caseRef", "applicationId": None,
                                      "serviceRequestType": None, "subjectId": None, "resolutionStatus": None, "createdAt": "openedAt",
                                      "updatedAt": None, "summary": None, "legacy": None}),
    "silver_proactive_candidate": dict(entity="ProactiveCandidate", profile="ProactiveCandidate", gold=["proactive_event"],
                                       key=["candidateId"], grain="One proactive-event fixture.",
                                       pmap={"candidateId": "variantId", "journeyId": None, "kind": "eventType", "riskLevel": None,
                                             "priorityScore": None, "sourceRefs": ("evidenceRefs", {"t": "join"}), "calculationRefs": None,
                                             "detectedAt": None, "expiresAt": None, "status": None, "suppressionReason": None}),
    "silver_document": dict(entity="Document", profile="Document", gold=["document"], key=["documentId"], owner="ownerId", label=["title"],
                            grain="One document descriptor per seed release.", skip={"facts", "declaredFacts"},
                            desc={"issuer": "Issuer of the document (retail4 release).",
                                  "source": "Source or issuer text of the document (Frontier release)."},
                            notes="The profile's sourceVersion attribute and the common sourceVersion column are one column (the profile's shared-column "
                                  "rule). contentType and sourceSha256 come from the document's asset only when it has exactly one distinct value "
                                  "(12 retail4 documents carry a PDF and a PNG), otherwise they stay null. The nested facts object is flattened "
                                  "to readable text in xFactsText (money as 'INR 12,500.00').",
                            extra=[dict(col="xFactsText", origin="extension", type="string", **{"from": ["facts", "declaredFacts"]}, t="facts_text",
                                        desc="Key facts stated in the document as readable text: amounts in INR, lists comma separated.")],
                            pmap={"documentId": "documentId", "title": "title", "documentKind": ["documentKind", "documentType"],
                                  "contentType": ("derive:document_content_type", ["assets", "contentType"]),
                                  "sourceVersion": ("sourceVersion", {"t": "version_text", "shared": "common"}),
                                  "sourceSha256": ("derive:document_source_sha256", ["assets", "sourceSha256"]),
                                  "pageCount": "pageCount", "pageCountBasis": ("derive:document_page_count_basis", ["pageCount"]),
                                  "issuedOn": "issuedOn", "previewable": None}),
    "silver_crm_case": dict(entity="CrmCase", profile=None, gold=["crm_case", "customer"], key=["caseId"], owner="customerId", keep_owner=True,
                            state="stage", label=["title"], grain="One CRM case (also the source of CRM-only customer parties).",
                            extra_paths=["customerName"],
                            notes="The profile's CRMContinuity is a derived continuity view of the app's CRM state, not a CRM case record, so the seed's "
                                  "CRM cases stay an extension entity. customerId and customerName also feed gold's customer table."),
    "silver_product": dict(entity="Product", profile="Product", gold=["product"], key=["productId"], state="status", label=["displayName"],
                           version="productVersion", grain="One catalogue, relationship-proposal or reference-stub product record.",
                           notes="Both releases and the relationship package stay side by side: CB-SAV-SALARY is a catalogue row (retail4) and a "
                                 "reference stub (Frontier); gold keeps the catalogue row (precedence by source order).",
                           pmap={"productId": "productId", "productVersion": "productVersion", "productFamily": "family",
                                 "name": "displayName", "currency": ("derive:product_currency", ["parameters.currency", "parameters"]),
                                 "ratePct": ("parameters.interestRatePct", DEC), "annualFeeMinor": ("parameters.annualFee", RUPEES),
                                 "monthlyFeeMinor": ("parameters.monthlyFee", RUPEES),
                                 "monthlyPremiumMinor": ("parameters.monthlyPremium", RUPEES),
                                 "sumInsuredMinor": ("parameters.sumInsured", RUPEES),
                                 "minimumPrincipalMinor": ("parameters.minimumPrincipal", RUPEES),
                                 "maximumPrincipalMinor": ("parameters.maximumPrincipal", RUPEES),
                                 "minimumTermMonths": "parameters.minimumTermMonths",
                                 "maximumTermMonths": ("derive:product_max_term_months",
                                                       ["parameters.maximumTermMonths", "parameters.maximumTenureYears", "family"]),
                                 "requiresDecision": ("derive:product_requires_decision", ["family"])},
                           skip={"dataRole"},
                           desc={"productType": "Product type of a Frontier reference stub (the stub carries no family or display name)."},
                           extra=[dict(col="xDataRole", origin="extension", type="string", **{"from": ["dataRole", "$data_role"]},
                                       desc="Role of the record: catalogue (full terms), relationship_proposal (planner proposal, not an approved offer) "
                                            "or reference_only (stub with no terms). The Frontier stubs state it; the other two come from the file "
                                            "they were read from.")]),
    "silver_product_parameter": dict(entity="ProductParameter", profile=None, gold=["product_parameter"], key=["productId", "parameterName"],
                                     record_id="{productId}.{name}",
                                     grain="One scalar catalogue parameter of a product (nested names dotted).",
                                     explicit=[dict(col="productId", origin="extension", type="string", nullable=False,
                                                    **{"from": "productId"}, desc="Product the parameter belongs to."),
                                               dict(col="parameterName", origin="extension", type="string", nullable=False,
                                                    **{"from": "name"}, desc="Parameter name; nested names are dotted (eligibility.minimumAge)."),
                                               dict(col="valueKind", origin="extension", type="string", **{"from": "value"}, t="value_kind",
                                                    desc="What the seed holds: string, integer, decimal, boolean or list."),
                                               dict(col="valueText", origin="extension", type="string", **{"from": "value"}, t="json_text",
                                                    desc="The value as JSON writes it (true, 1.0, 9.5); lists are '; ' joined."),
                                               dict(col="valueInteger", origin="extension", type="integer", **{"from": "value"}, t="only_int",
                                                    desc="The value when the seed holds an integer."),
                                               dict(col="valueDecimal", origin="extension", type="decimal", **{"from": "value"}, t="only_float|decimal_text",
                                                    desc="The value when the seed holds a fractional number (canonical decimal text)."),
                                               dict(col="valueBoolean", origin="extension", type="boolean", **{"from": "value"}, t="only_bool",
                                                    desc="The value when the seed holds a boolean."),
                                               dict(col="ordinal", origin="extension", type="integer", **{"from": "$ordinal"},
                                                    desc="1-based position of the parameter within the product's flattened parameters.")]),
    "silver_sop": dict(entity="Sop", profile=None, gold=["sop"], key=["sopId"], label=["title"], version="sourceVersion",
                       grain="One standard operating procedure record.", rename={"sourceVersion": "sopVersion"}),
    "silver_business_unit": dict(entity="BusinessUnit", profile=None, gold=["business_unit"], key=["buId"], label=["name"],
                                 grain="One business unit."),
    "silver_region": dict(entity="Region", profile=None, gold=["region"], key=["regionId"], grain="One bank region.",
                          rename={"state": "stateName"}),
    "silver_relationship_manager": dict(entity="RelationshipManager", profile=None, gold=["relationship_manager"], key=["rmId"],
                                        label=["name"], grain="One relationship manager."),
    "silver_campaign": dict(entity="Campaign", profile=None, gold=["campaign"], key=["campaignId"], state="status", label=["name"],
                            grain="One marketing campaign."),
    "silver_monthly_metric": dict(entity="MonthlyMetric", profile=None, gold=["monthly_metric"], key=["buId", "month"],
                                  record_id="{buId}/{month}", grain="One reporting metric set per business unit and month.",
                                  child={"ordinal": "$ordinal"}),
    "silver_risk_alert": dict(entity="RiskAlert", profile=None, gold=["risk_alert"], key=["alertId"], label=["title"],
                              grain="One bank-level risk alert."),
    "silver_fraud_signal": dict(entity="FraudSignal", profile=None, gold=["fraud_signal"], key=["signalId"], label=["label"],
                                grain="One fraud-signal type with 30-day counts.", rename={"label": "signalLabel"}),
    "silver_product_business_unit": dict(entity="ProductBusinessUnit", profile=None, gold=["product_business_unit"],
                                         key=["buId", "productId"], record_id="{^.buId}>{@}",
                                         grain="One product owned by a business unit (element of productIds).",
                                         child={"buId": "^.buId", "productId": "@", "ordinal": "$index"}),
}

# the custom flatteners that moved from gold, and the sources whose gold `set:` constants silver still needs
SOURCES_OVERRIDE = {
    "silver_preference": [{"custom": "preference_unpivot", "release": FR, "file": "preferences.json"},
                          {"release": L4, "file": "preferences.json"}],
    "silver_product_parameter": [{"custom": "product_parameter", "files": ["data/releases/retail4-v1/products.json",
                                                                          "data/relationship/products.json"]}],
    "silver_monthly_metric": [{"custom": "monthly_metric", "release": L4, "file": "bank/monthly-metrics.json"}],
}
KEEP_SET = {"silver_product", "silver_grant_permission", "silver_agent_scope"}


def pascal(path: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[.]", path.lstrip("^.")) if p)


def camel(path: str) -> str:
    s = pascal(path)
    return s[:1].lower() + s[1:]


def first_value(path: str, rows):
    return next((v for v in (r.get(path) for r in rows) if v is not None), None)


def gold_columns(g: dict):
    """Unexpanded gold columns as (kind, [paths], t, column)."""
    for c in g["columns"]:
        if "const" in c:
            continue
        paths = c["from"] if isinstance(c.get("from"), list) else [c["from"]] if "from" in c else []
        if "money" in c:
            yield "money", paths, None, c
        elif "rate_bps" in c:
            yield "bps", paths, None, c
        elif "rate_pct" in c:
            yield "pct", paths, None, c
        elif "instant" in c:
            yield "instant", paths, None, c
        elif "tpl" in c:
            yield "tpl", [m.group(1) for m in TPL_RE.finditer(c["tpl"])], None, c
        else:
            yield "plain", paths, c.get("t"), c


def ext_spec(kind: str, path: str, t: str | None, c: dict, rows) -> tuple[str, dict, str | None]:
    """(silver type, extra column keys, unit) for an extension column fed by a gold column."""
    sample = first_value(path, rows)
    if isinstance(sample, dict) and "amountMinor" in sample:
        return "minor", dict(PAISE), MINOR_UNIT
    if kind == "bps":
        return "integer", {}, "basis points"
    if kind == "pct":
        return "decimal", dict(DEC), "percentage points"
    if kind == "instant":
        return "datetime", {}, None
    parts = (t or "").split("|")
    if "join" in parts:
        return "string", {"t": "join"}, None
    if "len" in parts:
        return "integer", {"t": "len"}, None
    if "facts_text" in parts:
        return "string", {"t": "facts_text"}, None
    gt = c.get("type", "string")
    if gt == "double":
        name = c.get("col", "")
        if name.endswith("_inr") and "crore" not in name:
            return "minor", dict(RUPEES), MINOR_UNIT
        unit = "INR crore" if "crore" in name else "percentage points" if name.endswith("_pct") else None
        return "decimal", dict(DEC), unit
    return {"int": "integer", "bool": "boolean"}.get(gt, gt), {}, None


def describe(d: dict, std: dict) -> str:
    if d["profile"]:
        e = std["entities"][d["profile"]]
        ref = f"; official reference {e['cdm']['entity']} (semantic reference only)" if e.get("cdm") else ""
        return (f"{d['grain']} Conformed to the CDM profile entity {d['profile']} (realises the ontology entity "
                f"{ONTOLOGY[d['profile']]}{ref}). Attributes without a seed counterpart stay null; other seed fields the ontology needs are x columns.")
    return f"{d['grain']} Contoso extension entity: no CDM profile entity, BIAN status unmapped; every column is an extension column."


ONTOLOGY = standards.ONTOLOGY_ENTITY


def draft(name: str, d: dict, gold_by: dict, repo: Repo, profile: Profile, std: dict) -> dict:
    primary = gold_by[d["gold"][0]]
    sources = SOURCES_OVERRIDE.get(name) or [
        {k: v for k, v in s.items() if k != "set" or name in KEEP_SET} for s in primary["sources"]]
    if isinstance(d.get("sources"), slice):
        sources = sources[d["sources"]]
    rows = [r for s in sources for r in silver_source_rows(s, repo)]
    profile_entity = d["profile"]
    prefix = "x" if profile_entity else ""
    cols: list[dict] = []
    consumed_raw: set[str] = set()                       # paths a profile/child column already carries losslessly
    consumed_t: set[tuple[str, str]] = set()             # (path, join|len|facts_text) already carried
    gold_flags: dict[str, dict] = {}
    for kind, paths, t, c in gold_columns(primary):
        for p in paths:
            flags = gold_flags.setdefault(p, {})
            if c.get("ontology") is False:
                flags["restricted"] = True
            elif c.get("pii"):
                flags["pii"] = True

    def consume(col: dict) -> None:
        paths = col.get("from")
        for p in paths if isinstance(paths, list) else [paths] if isinstance(paths, str) else []:
            if col.get("t") in ("join", "len", "facts_text"):
                consumed_t.add((p, col["t"]))
            else:
                consumed_raw.add(p)

    if d.get("explicit"):
        cols.extend(d["explicit"])
    if profile_entity:
        for a in profile.attributes(profile_entity):
            spec = d["pmap"].get(a["name"])
            col: dict = {"col": a["name"], "origin": "profile", "type": a["type"]}
            if spec is None:
                col["none"] = REASONS.get((profile_entity, a["name"]), NONE)
            else:
                extra: dict | list | None = None
                if isinstance(spec, tuple):
                    spec, extra = spec
                if isinstance(spec, str) and spec.startswith("derive:"):
                    col["derive"] = spec.split(":", 1)[1]
                    col["reads"] = extra
                else:
                    col["from"] = spec
                    if isinstance(extra, dict):
                        extra = dict(extra)
                        if "shared" in extra:
                            col["shared"] = extra.pop("shared")
                        col.update(extra)
                    paths = spec if isinstance(spec, list) else [spec]
                    for p in paths:
                        col.update(gold_flags.get(p, {}))
                    consume(col)
            cols.append(col)
    for cname, path in (d.get("child") or {}).items():
        typ = "integer" if cname in ("ordinal", "installmentNumber") else "boolean" if cname == "isDenied" else "string"
        col = {"col": cname, "origin": "extension", "type": typ, "from": path}
        if cname == "ordinal":
            col.update(nullable=False, desc="1-based position within the array (the order the seed lists it).")
        cols.append(col)
        consumed_raw.add(path)
    seen_names = {c["col"].lower() for c in cols} | {n.lower() for n in COMMON_COLUMNS}
    rename = d.get("rename", {})
    if not d.get("explicit"):
        for kind, paths, t, c in gold_columns(primary):
            if c.get("key") and kind == "tpl":
                continue
            for p in paths:
                if p == "$business_date":
                    cname = (prefix + "ReleaseBusinessDate") if prefix else "releaseBusinessDate"
                    if cname.lower() not in seen_names:
                        cols.append({"col": cname, "origin": "extension", "type": "date", "from": "$business_date",
                                     "desc": "Business date of the release the record comes from (release.json)."})
                        seen_names.add(cname.lower())
                    continue
                if p in SEED_CONTEXT or p.startswith(("$", "^")) or p == "@":
                    continue
                if p == d.get("owner") and not d.get("keep_owner"):
                    continue                                    # carried by the technical _owner_id column
                if p in d.get("skip", ()) or first_value(p, rows) is None:
                    continue
                typ, extra, unit = ext_spec(kind, p, t, c, rows)
                parts = (t or "").split("|")
                carried = ((p, "join") in consumed_t and "join" in parts) or ((p, "len") in consumed_t and "len" in parts) \
                    or ((p, "facts_text") in consumed_t and "facts_text" in parts)
                if carried or (p in consumed_raw and not {"join", "len", "facts_text"} & set(parts)):
                    continue
                base = rename.get(p) or pascal(p)
                suffix = "Count" if extra.get("t") == "len" else "Minor" if typ == "minor" else ""
                core = base + (suffix if suffix and not base.endswith(suffix) else "")
                cname = (prefix + core[:1].upper() + core[1:]) if prefix else core[:1].lower() + core[1:]
                if cname.lower() in seen_names:
                    continue
                col = {"col": cname, "origin": "extension", "type": typ, "from": p, **extra}
                if d.get("unit", {}).get(p) or unit:
                    col["unit"] = d.get("unit", {}).get(p) or unit
                if d.get("desc", {}).get(p) or (c.get("desc") and kind != "tpl"):
                    col["desc"] = d.get("desc", {}).get(p) or c["desc"].strip()
                col.update(gold_flags.get(p, {}))
                col.update(d.get("flags", {}).get(p, {}))
                cols.append(col)
                seen_names.add(cname.lower())
                consume(col)
    for p in d.get("extra_paths", []):
        cols.append({"col": camel(p) if not prefix else prefix + pascal(p), "origin": "extension", "type": "string", "from": p})
    cols.extend(d.get("extra", []))
    if not profile_entity and any(c.get("t") == "paise" for c in cols) and not any(c["col"] == "currency" for c in cols):
        money = [c["from"] for c in cols if c.get("t") == "paise"]
        cols.append({"col": "currency", "origin": "extension", "type": "string", "from": [f"{m}.currency" for m in money],
                     "desc": "ISO currency of every money amount in the row (checked at build time)."})
    if not profile_entity:
        order = {k: i for i, k in enumerate(d["key"])}
        cols.sort(key=lambda c: order.get(c["col"], 100))
        for c in cols:
            if c["col"] in order:
                c["nullable"] = False
    out = {"table": name, "entity": d["entity"], "profile": bool(profile_entity)}
    if profile_entity:
        out["profile_entity"] = profile_entity
    out.update(ontology_entity=primary["entity"], domain=primary["domain"], serves=d["gold"], grain=d["grain"], description=describe(d, std))
    if d.get("notes"):
        out["notes"] = d["notes"]
    if profile_entity:
        e = std["entities"][profile_entity]
        out["cdm"] = e.get("cdm")
        out["bian"] = e.get("bian") or []
    out.update(sources=sources, key=d["key"])
    if d.get("record_id"):
        out["record_id"] = d["record_id"]
    state = {"from": d["state"], "default": "recorded"} if d.get("state") else {"const": "recorded"}
    common = {"cite": "app/data_xray/projection.py:322-338", "source_kind": "bank_record", "state": state}
    if d.get("label"):
        common["label"] = {"from": d["label"]}
    out.update(owner=d.get("owner"), common=common, version=d.get("version"), columns=cols)
    return out


def flow(v) -> str:
    text = yaml.safe_dump(v, default_flow_style=True, width=10 ** 6, sort_keys=False, allow_unicode=True).strip()
    return text[:-3].rstrip() if text.endswith("\n...") else text


def emit(t: dict) -> str:
    lines = [f"  - table: {t['table']}"]
    for k in ("entity", "profile", "profile_entity", "ontology_entity", "domain", "serves", "grain", "description", "notes"):
        if t.get(k) is not None:
            lines.append(f"    {k}: {flow(t[k])}")
    if "cdm" in t:
        lines.append(f"    cdm: {flow(t['cdm'])}")
    if "bian" in t:
        if t["bian"]:
            lines.append("    bian:")
            lines.extend(f"      - {flow(b)}" for b in t["bian"])
        else:
            lines.append("    bian: []")
    lines.append("    sources:")
    lines.extend(f"      - {flow(s)}" for s in t["sources"])
    for k in ("key", "record_id", "owner", "common", "version"):
        if t.get(k) is not None:
            lines.append(f"    {k}: {flow(t[k])}")
    lines.append("    columns:")
    lines.extend(f"      - {flow(c)}" for c in t["columns"])
    return "\n".join(lines)


GROUPS = {"10_party": ["silver_customer", "silver_household", "silver_household_member"],
          "20_holdings": ["silver_account", "silver_card", "silver_deposit", "silver_loan", "silver_loan_installment", "silver_protection_policy",
                          "silver_protection_gap", "silver_investment", "silver_investment_goal"],
          "30_money_goals": ["silver_transaction", "silver_transaction_evidence", "silver_scheduled_obligation", "silver_income_record",
                             "silver_goal", "silver_preference"],
          "40_authority": ["silver_consent", "silver_frontier_consent", "silver_consent_resource", "silver_household_access_grant",
                           "silver_grant_permission", "silver_registered_agent", "silver_agent_scope", "silver_frontier_mandate",
                           "silver_mandate_action"],
          "50_servicing": ["silver_incident", "silver_service_case", "silver_proactive_candidate", "silver_document", "silver_crm_case"],
          "60_catalogue": ["silver_product", "silver_product_parameter", "silver_sop"],
          "70_bank_ops": ["silver_business_unit", "silver_region", "silver_relationship_manager", "silver_campaign", "silver_monthly_metric",
                          "silver_risk_alert", "silver_fraud_signal", "silver_product_business_unit"]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out).resolve()
    if out == KIT / "model" or KIT / "model" in out.parents:
        raise SystemExit("write the draft outside model/: the committed YAML is curated by hand")
    out.mkdir(parents=True, exist_ok=True)
    model = load_model(KIT / "model")
    gold_by = {t["table"]: t for t in model["tables"]}
    repo = Repo(Path(a.repo), model["canonical_release"], model.get("release_short"))
    profile = Profile(Path(a.repo))
    std = standards.generate(Path(a.repo))
    drafts = {n: draft(n, d, gold_by, repo, profile, std) for n, d in TABLES.items()}
    for fname, names in GROUPS.items():
        text = "silver:\n" + "\n".join(emit(drafts[n]) for n in names) + "\n"
        (out / f"{fname}.yaml").write_text(text, encoding="utf-8", newline="\n")
    ungrouped = set(TABLES) - {n for names in GROUPS.values() for n in names}
    print(f"drafted {len(drafts)} tables into {out}" + (f"; not in any group: {sorted(ungrouped)}" if ungrouped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
