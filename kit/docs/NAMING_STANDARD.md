# Naming standard: one vocabulary from the CDM profile, BIAN as metadata

Status: the standard for the `medallion-cdm` branch. Every name below is read from the specs and checked by tests; nothing in this document is a
second source of truth. The two tables in sections 4 and 5 are generated (`python tools/naming_registry.py --write`) and a test fails when they drift.

The requirement behind it: the ontology must respect the BIAN-based Microsoft Common Data Model for its silver, with facts and dimensions in
place and the ontology created accordingly, and the naming must stay in sync with the Microsoft CDM and the BIAN architecture. The superapp's
approved CDM application profile (`data/banking-model/v1`, namespace `contosoBanking`) is the base for silver; official Microsoft CDM column
names are used where they genuinely match.

## 1. Principles

1. **One vocabulary.** Entity names come from the CDM application profile: PascalCase singular nouns, as in the Microsoft CDM, and the same names the
   superapp's Banking Data X-Ray shows (`Account`, `HouseholdAccessGrant`, `FrontierMandate`).
2. **Official Microsoft CDM names are references.** `FHAccount`, `FICard`, `FHSaving`, `FHLoan`, `Contact` and the rest appear only as
   `cdm_reference_entity` metadata, in descriptions as "semantic reference only", and as official-name alias columns in silver. The profile says
   its thin entities do not extend or instantiate the full official entity (`profile.json`, `cdmReference.limitation`), so a Contoso table or
   entity is never named after an official entity. A test derives the deny-list from the vendored official entity files.
3. **BIAN names are metadata.** A BIAN service domain (`Savings Account`) or control record (`SavingsAccountFacility`) appears only in catalog
   columns and in the standards sentence of a description, never as a table or entity name: the registry maps attributes with the status `custom`,
   `provisional` or `unmapped`, and some mappings are conditional (a card maps to Credit Card only for credit cards).
4. **Layer and role are visible in the lakehouse name.** `silver_<entity>`, `dim_<entity>`, `fact_<entity>`, `bridge_<entity>`, `ref_<entity>`;
   bronze is `Files/bronze/...` plus the table `bronze_file_manifest`. `<entity>` is the snake_case of the entity name. One documented exception:
   `dim_date`, the generated calendar (the Kimball standard name; neither the CDM nor BIAN defines a calendar entity).
5. **One registry, checked by tests.** Every name derives from the spec fields `entity`, `physical` and `profile_entity` (read through
   `contoso_onto/registry.py`). The logical id of a gold table (`table:` in `model/gold/*.yaml`, the v1 name such as `access_grant`) never changes:
   relationships, measures, rules, tiers, oracles and the frozen v1 fixtures key on it.

## 2. The rule per object

| Object | Rule | Example |
|---|---|---|
| Bronze | `Files/bronze/<release>/<seed path>` verbatim; manifest table `bronze_file_manifest` | `Files/bronze/retail-frontier-v1/accounts.json` |
| Silver table | `silver_` + snake(entity); the entity is the profile entity, or the business noun of an extension entity | `silver_household_access_grant` |
| Silver column | profile attribute: exact profile camelCase; official-name alias: the official name; extension column: `x` + PascalCase on a profile entity, plain camelCase on an extension entity; technical: `_release_id`, `_source_file`, `_source_row`, `_owner_id` | `postedBalanceMinor`, `balance`, `xAgeYears` |
| Gold table | role prefix from `kind` + snake(entity); logical id unchanged | `dim_frontier_mandate` for logical id `mandate` |
| Gold column | snake_case; rupees `_inr` (double) with a hidden exact `_paise` (int64); percent `_pct` | `posted_balance_inr` |
| Ontology entity | the profile entity name when the entity realises exactly one (at most 26 characters, an identifier); a Contoso extension entity keeps its PascalCase business noun and says so in its description | `ProtectionPolicy`, `HouseholdMember` |
| Ontology property | PascalCase of the gold column | `PostedBalanceInr` |
| Ontology relationship | a natural-language "Subject Verb Object" triple, unique (Fabric natural-language queries need it) | `Account Held By Customer` |
| Semantic model | table = entity name; column label from the property (`Posted Balance (INR)`); DAX names entities | `Account[Posted Balance (INR)]` |
| Standards metadata | `cdm_reference_entity`, BIAN domain, control record and status in `silver_entity_catalog`, `silver_attribute_catalog`, `model/standards.yaml` and the standards sentence | `Standards: BIAN 13.0.0 ... Demo mapping, not certification.` |

Every entity description reads in this order: what the entity is, its standards sentence (the BIAN and Microsoft CDM alignment, or the extension label
"Contoso extension entity: no CDM profile entity, BIAN status unmapped."), then one architecture clause built from the registry: star role, grain,
lakehouse lineage (`dim_account <- silver_account <- accounts.json`, or "Curated reference data") and the official-name aliases. Status words are
the registry's own and are never upgraded.

## 3. Deliberate deviations

1. **Money properties stay `...Inr` and `...Pct`.** Agents read rupees as a double; the exact int64 paise twin is hidden from the ontology. The profile
   names these attributes `...Minor` and says paise must not be silently renamed as major-unit fields, so `...Minor` lives in silver only.
2. **`dim_date`** is the one lakehouse name that is not role + entity (the entity is `Calendar`, a semantic-model-only table).
3. **`Consent` merges two profile entities**: the profile's `Consent` (legacy) and `FrontierConsent` rows are one ontology entity, fed by `silver_consent` and
   `silver_frontier_consent`. It is the only profile-aligned entity whose name differs from its profile entities'.
4. **Property, relationship and measure names keep the wording agents already use**: `ServiceCase.ServiceRecordId`, `MandateAction.EventId`, the
   relationship `Event Of Mandate`, the measure `Active Access Grants`. The old business words also stay findable: each renamed entity lists them as
   synonyms and names them in its description (a mandate, a mandate event, an access grant, an insurance policy, a service record, a proactive event).
5. **Contoso extension entities** (33 of the 51 tables: 24 silver extension entities and 9 curated reference tables) have no profile entity. The
   profile's `Commitment` is a mandate or repayment-schedule row, not the seed's scheduled obligation, so `ScheduledObligation` is one of them.

## 4. The registry (generated)

Read it row by row: the ontology entity, the profile entity it realises ("Contoso extension" when there is none) with the profile's family, the
silver table, the gold lakehouse table and its star role, the official Microsoft CDM entity that is a semantic reference, the BIAN service domains with
the registry's own status, and what was merged or renamed.

<!-- naming-registry:begin -->
| Ontology entity | Profile entity | Family | Silver table | Gold physical | Kind | Official CDM reference | BIAN service domains (status) | Merge or rename |
|---|---|---|---|---|---|---|---|---|
| `Customer` | Customer | customers | `silver_customer`, `silver_crm_case` | `dim_customer` | dimension | Contact | Party Reference Data Directory (custom) | - |
| `Household` | Contoso extension | - | `silver_household` | `dim_household` | dimension | - | unmapped | - |
| `HouseholdMember` | Contoso extension | - | `silver_household_member` | `dim_household_member` | dimension | - | unmapped | - |
| `Account` | Account | holdings | `silver_account` | `dim_account` | dimension | FHAccount (4 name aliases) | Customer Product and Service Directory (custom); Savings Account (custom) | - |
| `Card` | Card | holdings | `silver_card` | `dim_card` | dimension | FICard (3 name aliases) | Credit Card (custom); Customer Product and Service Directory (custom) | - |
| `Deposit` | Deposit | holdings | `silver_deposit` | `dim_deposit` | dimension | FHSaving (3 name aliases) | Customer Product and Service Directory (custom); Term Deposit (custom, partly provisional) | - |
| `Loan` | Loan | holdings | `silver_loan` | `dim_loan` | dimension | FHLoan (6 name aliases) | Consumer Loan (custom); Customer Product and Service Directory (custom) | - |
| `LoanInstallment` | Contoso extension | - | `silver_loan_installment` | `fact_loan_installment` | fact | - | unmapped | - |
| `ProtectionPolicy` | ProtectionPolicy | holdings | `silver_protection_policy` | `dim_protection_policy` | dimension | - | Customer Product and Service Directory (custom) | entity was InsurancePolicy; lakehouse was dim_insurance_policy |
| `ProtectionGap` | Contoso extension | - | `silver_protection_gap` | `fact_protection_gap` | fact | - | unmapped | - |
| `Investment` | Contoso extension | - | `silver_investment` | `dim_investment` | dimension | - | unmapped | - |
| `InvestmentGoal` | Contoso extension | - | `silver_investment_goal` | `bridge_investment_goal` | bridge | - | unmapped | - |
| `Transaction` | Transaction | money | `silver_transaction` | `fact_transaction` | fact | - | Payment Execution (custom); Position Keeping (custom) | - |
| `TransactionEvidence` | Contoso extension | - | `silver_transaction_evidence` | `bridge_transaction_evidence` | bridge | - | unmapped | - |
| `ScheduledObligation` | Contoso extension | - | `silver_scheduled_obligation` | `fact_scheduled_obligation` | fact | - | unmapped | - |
| `IncomeRecord` | Contoso extension | - | `silver_income_record` | `fact_income_record` | fact | - | unmapped | lakehouse was fact_income |
| `Goal` | Goal | goals | `silver_goal` | `dim_goal` | dimension | - | Customer Relationship Management (provisional) | - |
| `Preference` | Preference | goals | `silver_preference` | `dim_preference` | dimension | - | Customer Relationship Management (custom) | - |
| `Consent` | Consent + FrontierConsent | authority | `silver_frontier_consent`, `silver_consent` | `dim_consent` | dimension | - | none verified | merges the profile entities Consent + FrontierConsent |
| `ConsentResource` | Contoso extension | - | `silver_consent_resource` | `bridge_consent_resource` | bridge | - | unmapped | - |
| `HouseholdAccessGrant` | HouseholdAccessGrant | frontier | `silver_household_access_grant` | `dim_household_access_grant` | dimension | - | none verified | entity was AccessGrant; lakehouse was dim_access_grant |
| `GrantPermission` | Contoso extension | - | `silver_grant_permission` | `bridge_grant_permission` | bridge | - | unmapped | - |
| `RegisteredAgent` | RegisteredAgent | frontier | `silver_registered_agent` | `dim_registered_agent` | dimension | - | none verified | - |
| `AgentScope` | Contoso extension | - | `silver_agent_scope` | `bridge_agent_scope` | bridge | - | unmapped | - |
| `FrontierMandate` | FrontierMandate | frontier | `silver_frontier_mandate` | `dim_frontier_mandate` | dimension | - | none verified | entity was Mandate; lakehouse was dim_mandate |
| `MandateAction` | MandateAction | frontier | `silver_mandate_action` | `fact_mandate_action` | fact | - | none verified | entity was MandateEvent; lakehouse was fact_mandate_event |
| `Incident` | Contoso extension | - | `silver_incident` | `fact_incident` | fact | - | unmapped | - |
| `ServiceCase` | ServiceCase | servicing | `silver_service_case` | `fact_service_case` | fact | - | Customer Case (custom); Servicing Order (provisional) | entity was ServiceRecord; lakehouse was fact_service_record |
| `ProactiveCandidate` | ProactiveCandidate | frontier | `silver_proactive_candidate` | `fact_proactive_candidate` | fact | - | none verified | entity was ProactiveEvent; lakehouse was fact_proactive_event |
| `ServiceRequestType` | Contoso extension | - | (curated reference data) | `dim_service_request_type` | dimension | - | unmapped | - |
| `Document` | Document | evidence | `silver_document` | `dim_document` | dimension | CustomDocument | Document Directory (custom) | - |
| `CrmCase` | Contoso extension | - | `silver_crm_case` | `fact_crm_case` | fact | - | unmapped | - |
| `Product` | Product | products | `silver_product` | `dim_product` | dimension | - | Product Directory (custom) | - |
| `ProductParameter` | Contoso extension | - | `silver_product_parameter` | `dim_product_parameter` | dimension | - | unmapped | - |
| `Journey` | Contoso extension | - | (curated reference data) | `dim_journey` | dimension | - | unmapped | - |
| `Sop` | Contoso extension | - | `silver_sop` | `dim_sop` | dimension | - | unmapped | - |
| `JourneySop` | Contoso extension | - | (curated reference data) | `bridge_journey_sop` | bridge | - | unmapped | - |
| `JourneyProduct` | Contoso extension | - | (curated reference data) | `bridge_journey_product` | bridge | - | unmapped | - |
| `Law` | Contoso extension | - | (curated reference data) | `ref_law` | reference | - | unmapped | - |
| `AuthorityRule` | Contoso extension | - | (curated reference data) | `ref_authority_rule` | reference | - | unmapped | - |
| `FrontierRequirement` | Contoso extension | - | (curated reference data) | `ref_frontier_requirement` | reference | - | unmapped | - |
| `BankAgent` | Contoso extension | - | (curated reference data) | `ref_bank_agent` | reference | - | unmapped | - |
| `BusinessUnit` | Contoso extension | - | `silver_business_unit` | `dim_business_unit` | dimension | - | unmapped | - |
| `Region` | Contoso extension | - | `silver_region` | `dim_region` | dimension | - | unmapped | - |
| `RelationshipManager` | Contoso extension | - | `silver_relationship_manager` | `dim_relationship_manager` | dimension | - | unmapped | - |
| `Campaign` | Contoso extension | - | `silver_campaign` | `dim_campaign` | dimension | - | unmapped | - |
| `MonthlyMetric` | Contoso extension | - | `silver_monthly_metric` | `fact_monthly_metric` | fact | - | unmapped | - |
| `RiskAlert` | Contoso extension | - | `silver_risk_alert` | `fact_risk_alert` | fact | - | unmapped | - |
| `FraudSignal` | Contoso extension | - | `silver_fraud_signal` | `fact_fraud_signal` | fact | - | unmapped | - |
| `ProductBusinessUnit` | Contoso extension | - | `silver_product_business_unit` | `bridge_product_business_unit` | bridge | - | unmapped | - |
| `Calendar` | Contoso extension | - | (curated reference data) | `dim_date` | dimension | - | unmapped | dim_date is the one documented exception to role + entity; semantic model only |
<!-- naming-registry:end -->

## 5. The 17 BIAN service domains (generated)

These are the BIAN 13.0.0 service domains and control records the profile's registry uses, and the ontology entities that cite them. They are
metadata. The mapping is a demo mapping carried over from the source repo, not certification, and its attribute-level status (custom, provisional,
unmapped) is shown wherever a domain is named.

<!-- bian-domains:begin -->
| BIAN service domain | Control record | Profile entities mapped (status) | Ontology entities |
|---|---|---|---|
| Consumer Loan | `ConsumerLoanFacility` | Loan (custom) | Loan |
| Credit Card | `CreditCardFacility` | Card (custom) | Card |
| Customer Case | `CustomerCaseProcedure` | ServiceCase (custom), CRMContinuity (provisional) | ServiceCase |
| Customer Offer | `CustomerOfferProcedure` | Application (custom) | none (profile entities not in the ontology) |
| Customer Product and Service Directory | `CustomerProductAndServiceDirectoryEntry` | Account (custom), Card (custom), Deposit (custom), Loan (custom), ProtectionPolicy (custom) | Account, Card, Deposit, Loan, ProtectionPolicy |
| Customer Relationship Management | `CustomerRelationshipManagementPlan` | Goal (provisional), Preference (custom), RelationshipPlan (custom), CRMContinuity (custom) | Goal, Preference |
| Document Directory | `DocumentDirectoryEntry` | Document (custom), EvidenceReading (provisional) | Document |
| Party Reference Data Directory | `PartyReferenceDataDirectoryEntry` | Customer (custom), PartyRelationship (custom) | Customer |
| Payment Execution | `PaymentExecutionProcedure` | Transaction (custom), Settlement (custom) | Transaction |
| Payment Order | `PaymentOrderProcedure` | Application (custom), Commitment (custom) | none (profile entities not in the ontology) |
| Position Keeping | `FinancialPositionLog` | Transaction (custom), CollateralPledge (provisional), Allocation (provisional) | Transaction |
| Product Directory | `ProductDirectoryEntry` | Product (custom) | Product |
| Savings Account | `SavingsAccountFacility` | Account (custom), SweepRule (provisional) | Account |
| Servicing Event History | `ServicingEventLog` | LifecycleEvent (provisional) | none (profile entities not in the ontology) |
| Servicing Order | `ServicingOrderProcedure` | Application (custom), ServiceCase (provisional) | ServiceCase |
| Term Deposit | `TermDepositFacility` | Deposit (custom, partly provisional) | Deposit |
| Underwriting | `UnderwritingAssessment` | BankDecision (custom) | none (profile entities not in the ontology) |
<!-- bian-domains:end -->

## 6. How to change a name

The registry is the specs, so a rename is an edit of the specs plus the places that spell an entity name out. The tests name what you missed.

1. `model/gold/*.yaml`: set `entity` and `physical` (`<role>_<snake(entity)>`), keep `table` (the logical id) as it is, record the old names in
   `renamed_from: {entity: ..., physical: ...}` (delete the field to revert), and keep the old business words in `description` and `synonyms`.
2. `model/silver/*.yaml`: the silver table's `ontology_entity` (and the entity in its description).
3. `contoso_onto/standards.py`: `ONTOLOGY_ENTITY`, then regenerate `model/standards.yaml` with
   `python -m contoso_onto standards --repo <checkout> --write`.
4. `model/measures.yaml` (the entity inside DAX, `Entity[Column label]`) and `model/rules.yaml` (`Entity.Property` and the entity in the statement).
5. `contoso_onto/agents.py` and the text it generates name entities from the spec; nothing else there needs an edit.
6. `python tools/naming_registry.py --write` refreshes the two tables above; `docs/MEDALLION.md` names a few tables in its diagrams.
7. Run `python -m pytest tests/test_naming_standard.py tests/test_standards.py tests/test_standards_conformance.py tests/test_spec.py
   tests/test_physical_names.py tests/test_graph.py tests/test_silver_conformance.py tests/test_gold_equivalence.py`.

A missed DAX or rule reference fails the spec checks (`S044`, `S054`); a lakehouse name that breaks the rule fails `test_naming_standard.py`; the graph
test derives its renamed variant of the portal capture from `renamed_from`, so a rename only needs that field kept right.

## 7. What this standard does not claim

The mapping is a demo mapping. It is not Microsoft endorsement, not BIAN certification and not regulatory compliance, and silver is not a full CDM
SDK implementation. The wording to use is "official-name alias", "semantic reference" and "demo mapping, not certification". A mapping status is never
upgraded: provisional stays provisional and unmapped stays unmapped.
