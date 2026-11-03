# ContosoBankOntology - grounding brief (generated from the spec)

Business-context backbone for Contoso bank conversational AI and voice: customers, households, holdings, money movement, goals, delegated authority (consents, access grants, agents, mandates), servicing, catalogue and bank laws. Synthetic data only.

Use these names exactly. Never speak or write properties marked [PII] in full.

## Entities

### Customer (key: CustomerId)
A person the bank serves or tracks: Frontier primary customer Rhea Bansal (CustomerId rhea), legacy personas (Nivedita, Ravi, and the older copy of Rhea as rhea~legacy) and CRM-only parties. Never merge rows across ReleaseId; Frontier supersedes legacy. Standards: BIAN 13.0.0 Party Reference Data Directory (custom); Microsoft CDM Contact (semantic reference only). Demo mapping, not certification. Star role: dimension; grain: one customer identity per release. Lakehouse: dim_customer <- silver_customer + silver_crm_case <- personas.json, bank/crm-cases.json.
Synonyms: client, caller, user, account holder, banking customer, persona
Properties: CustomerId, DisplayName [PII], Age [PII], City [PII], EmploymentStatus, Segment, HouseholdId, IsPrimaryOwner, PhoneMasked [PII], ProfileSummary, SourceKind, SupersededByCustomerId, IsSynthetic, ReleaseId
Relationships: Customer Belongs To Household; Household Owned By Customer; Household Member Of Customer; Account Held By Customer; Card Issued To Customer; Deposit Held By Customer; Loan Granted To Customer; Policy Held By Customer; Protection Gap Of Customer; Investment Held By Customer; Transaction Of Customer; Obligation Owed By Customer; Income Earned By Customer; Goal Owned By Customer; Preference Stated By Customer; Consent Given By Customer; Access Grant Of Customer; Agent Registered For Customer; Mandate Granted By Customer; Incident Affects Customer; Service Record For Customer; Document Owned By Customer; Case Opened For Customer

### Household (key: HouseholdId)
A household that shares goals, obligations and delegated access. Frontier has one: household-bansal (Rhea, spouse Vivek, child Anika, parent Savitri). Access inside a household is resource-specific and asymmetric. Contoso extension entity: no CDM profile entity, BIAN status unmapped. Star role: dimension; grain: one household. Lakehouse: dim_household <- silver_household <- personas.json.
Synonyms: family, family unit
Properties: HouseholdId, DisplayName, PrimaryCustomerId, ReleaseId
Relationships: Customer Belongs To Household; Household Member Belongs To Household; Household Owned By Customer; Access Grant In Household

### HouseholdMember (key: MemberId)
A person in a customer's household (spouse, child, parent). Members are not customers: they only hold the access the owner explicitly granted. A minor has no inferred data access. Contoso extension entity: no CDM profile entity, BIAN status unmapped. Star role: dimension; grain: one household member. Lakehouse: dim_household_member <- silver_household_member <- household.json.
Synonyms: family member, dependent, spouse, beneficiary
Properties: MemberId, HouseholdId, PrimaryCustomerId, DisplayName [PII], RelationToOwner, AgeBand [PII], IsDependent, ReleaseId
Relationships: Household Member Belongs To Household; Household Member Of Customer; Income Contributed By Member; Consent About Household Member; Access Grant For Household Member

### Account (key: AccountId)
A deposit account (salary, reserve, savings). Frontier Rhea has acc-rhea-primary and acc-rhea-reserve. Balances are INR; the *_paise twins are exact integer paise. Posted balance is what the customer owns; available balance is net of holds. Standards: BIAN 13.0.0 Customer Product and Service Directory (custom), Savings Account (custom); Microsoft CDM FHAccount (semantic reference only). Demo mapping, not certification. Star role: dimension; grain: one account, with its balances as at the release business date. Lakehouse: dim_account <- silver_account <- accounts.json. Official CDM name aliases: FHAccount.integrationKey, FHAccount.balance, FHAccount.availableBalance, FHAccount.blockedAmount (semantic reference only).
Synonyms: bank account, savings account, salary account, current account, a/c, balance
Properties: AccountId, CustomerId, ProductId, AccountType, Status, Currency, OpenedOn, HistoryStartOn, BalanceAsOn, OpeningBalanceInr, PostedBalanceInr, AvailableBalanceInr, HoldsAmountInr, ReleaseId
Relationships: Account Held By Customer; Account Opened Under Product; Card Linked To Account; Deposit Linked To Account; Policy Premium Paid From Account; Transaction Posted To Account; Obligation Paid From Account; Mandate Draws On Account; Mandate Funds Account

### Card (key: CardId)
A debit or credit card linked to an account. Frontier Rhea's credit card card-rhea-frontier is temporarily locked after a dispute. Only masked numbers exist. Credit limit is not cash. Standards: BIAN 13.0.0 Credit Card (custom), Customer Product and Service Directory (custom); Microsoft CDM FICard (semantic reference only). Demo mapping, not certification. Star role: dimension; grain: one card. Lakehouse: dim_card <- silver_card <- cards.json. Official CDM name aliases: FICard.integrationKey, FICard.cardnumber, FICard.purchasingLimit (semantic reference only).
Synonyms: credit card, debit card, plastic
Properties: CardId, CustomerId, AccountId, ProductId, CardType, Status, MaskedNumber [PII], ExpiryMmYy, CreditLimitInr, AvailableCreditInr, OutstandingBalanceInr, MinimumDueInr, AnnualRatePct, StatementDay, PaymentDueDay, PaymentDueOn, StatementFrom, StatementTo, InternationalEnabled, OnlineEnabled, ContactlessEnabled, ReleaseId
Relationships: Card Linked To Account; Card Issued To Customer; Card Of Product; Transaction Paid By Card

### Deposit (key: DepositId)
A fixed deposit. Frontier Rhea holds dep-rhea-liquid-001 (liquid, withdrawable with a penalty). Maturity proceeds are not spendable cash until a maturity posting exists. The same money can also be an Investment: for net position or total assets count it once, through Investment (Law L5). Standards: BIAN 13.0.0 Customer Product and Service Directory (custom), Term Deposit (custom, partly provisional); Microsoft CDM FHSaving (semantic reference only). Demo mapping, not certification. Star role: dimension; grain: one deposit. Lakehouse: dim_deposit <- silver_deposit <- deposits.json. Official CDM name aliases: FHSaving.integrationKey, FHSaving.maturitydate, FHSaving.interestrate (semantic reference only).
Synonyms: fixed deposit, FD, term deposit, time deposit
Properties: DepositId, CustomerId, ProductId, LinkedAccountId, Status, PrincipalInr, AnnualRatePct, OpenedOn, MaturesOn, LiquidityClass, MaturityInstruction, PrematurePenaltyPct, ReleaseId
Relationships: Deposit Held By Customer; Deposit Of Product; Deposit Linked To Account

### Loan (key: LoanId)
A loan facility. Frontier Rhea has a floating-rate home loan (loan-home-001) and an education loan. Current principal is what is still owed; EMI is the monthly instalment. A disbursed loan is a liability, not income. Standards: BIAN 13.0.0 Consumer Loan (custom), Customer Product and Service Directory (custom); Microsoft CDM FHLoan (semantic reference only). Demo mapping, not certification. Star role: dimension; grain: one loan. Lakehouse: dim_loan <- silver_loan <- loans.json. Official CDM name aliases: FHLoan.integrationKey, FHLoan.principalAmount, FHLoan.balanceDisplayValue, FHLoan.installmentAmount, FHLoan.interestrate, FHLoan.maturitydate (semantic reference only).
Synonyms: home loan, mortgage, EMI, credit facility, borrowing
Properties: LoanId, CustomerId, ProductId, Purpose, RateType, AnnualRatePct, OpeningPrincipalInr, CurrentPrincipalInr, EmiInr, OriginationFeeInr, PrepaymentAppliesTo, PrepaymentChargePct, PrepaymentMinimumInr, PrepaymentNoticeDays, ScheduledInstallments, ReleaseId
Relationships: Loan Granted To Customer; Loan Of Product; Installment Of Loan

### LoanInstallment (key: InstallmentId)
One scheduled or paid EMI of a loan, split into principal, interest and fees. Use it for questions about interest paid so far or the next instalment. Contoso extension entity: no CDM profile entity, BIAN status unmapped. Star role: fact; grain: one scheduled instalment of a loan. Lakehouse: fact_loan_installment <- silver_loan_installment <- loans.json.
Synonyms: EMI payment, instalment, repayment
Properties: InstallmentId, LoanId, InstallmentNumber, DueOn, Status, OpeningPrincipalInr, PrincipalPaidInr, InterestPaidInr, FeesPaidInr, TotalPaidInr, ClosingPrincipalInr, PaymentDescription
Relationships: Installment Of Loan

### ProtectionPolicy (key: PolicyId)
An insurance policy (protection policy) held by a customer (term life, family health, individual health). Sum insured is cover, not cash. New cover is never retroactive and never guaranteed reimbursement. Standards: BIAN 13.0.0 Customer Product and Service Directory (custom); no Microsoft CDM reference. Demo mapping, not certification. Star role: dimension; grain: one insurance policy. Lakehouse: dim_protection_policy <- silver_protection_policy <- insurance.json, policies.json.
Synonyms: insurance policy, policy, cover, health cover, term cover, life insurance, health insurance
Properties: PolicyId, CustomerId, ProductId, PolicyType, Status, Insurer, PremiumInr, PremiumCadence, SumInsuredInr, BeneficiaryMemberIds, CoveredMemberIds, StartedOn, RenewsOn, LinkedAccountId, CoPayPct, InitialWaitingDays, PreExistingWaitingMonths, ReleaseId
Relationships: Policy Held By Customer; Policy Of Product; Policy Premium Paid From Account

### ProtectionGap (key: ProtectionGapId)
A planning gap between existing and target protection (for example life cover). It is a synthetic planning figure, not a recommendation, quote or underwriting outcome. Contoso extension entity: no CDM profile entity, BIAN status unmapped. Star role: fact; grain: one computed protection gap per customer and cover type at the business date (snapshot). Lakehouse: fact_protection_gap <- silver_protection_gap <- insurance.json.
Synonyms: insurance gap, cover gap, protection shortfall
Properties: ProtectionGapId, CustomerId, GapType, ExistingProtectionInr, TargetProtectionInr, GapInr, IncomeReplacementInr, LoanAndEducationNeedsInr, OffsetLiquidAssetsInr, Disclosure, ReleaseId
Relationships: Protection Gap Of Customer

### Investment (key: InvestmentId)
An investment holding (fixed deposit, index fund, reserve). Current value is a market-style figure from the seed, not a guaranteed return. Linked to the goals it funds. For net position or total assets use Investment value: a fixed deposit that is also an Investment is one asset, so never add Deposit principal on top of it (Law L5). Contoso extension entity: no CDM profile entity, BIAN status unmapped. Star role: dimension; grain: one investment holding. Lakehouse: dim_investment <- silver_investment <- investments.json.
Synonyms: holding, mutual fund, SIP, portfolio item
Properties: InvestmentId, CustomerId, ProductId, InvestmentType, Liquidity, CurrentValueInr, GoalIds, ReleaseId
Relationships: Investment Held By Customer; Investment Of Product

### Transaction (key: TransactionId)
A posted account or card transaction. Amount is always positive; Direction says debit or credit and SignedAmountInr carries the sign. Frontier covers Apr-Sep 2026 for Rhea; legacy covers three personas. Do not add transactions across ReleaseId. Standards: BIAN 13.0.0 Payment Execution (custom), Position Keeping (custom); no Microsoft CDM reference. Demo mapping, not certification. Star role: fact; grain: one ledger posting. Lakehouse: fact_transaction <- silver_transaction <- transactions.json.
Synonyms: payment, transfer, txn, debit, credit, spend, statement line
Properties: TransactionId, CustomerId, AccountId, CardId, Description, Direction, Category, Instrument, Status, AmountInr, SignedAmountInr, PostedAt, PostedDateIst, ValueDate, CounterpartyRef, CounterpartyType, DisputeIncidentId, PurchaseReference, SettlementReference, EvidenceRefs, EvidenceCount, ReleaseId
Relationships: Transaction Posted To Account; Transaction Of Customer; Transaction Paid By Card; Transaction Disputed In Incident

### ScheduledObligation (key: ObligationId)
A recurring commitment that leaves an account on a fixed day: loan EMI, school fee, card payment, SIP, premium. Obligations are committed cash: they reduce what can safely be moved or invested. Contoso extension entity: no CDM profile entity, BIAN status unmapped. Star role: fact; grain: one scheduled obligation. Lakehouse: fact_scheduled_obligation <- silver_scheduled_obligation <- scheduled-obligations.json.
Synonyms: standing instruction, recurring payment, commitment, bill, EMI due
Properties: ObligationId, CustomerId, Name, SourceAccountId, PayeeRef, PayeeType, Cadence, DayOfMonth, Status, AmountInr, ReleaseId
Relationships: Obligation Paid From Account; Obligation Owed By Customer

### IncomeRecord (key: IncomeId)
Monthly income evidence for a customer: gross and net, linked to the evidencing salary slip. Only posted, evidenced income counts; offers, refunds and variable targets are not income. Contoso extension entity: no CDM profile entity, BIAN status unmapped. Star role: fact; grain: one income record. Lakehouse: fact_income_record <- silver_income_record <- income.json.
Synonyms: salary, pay, earnings, income evidence
Properties: IncomeId, CustomerId, IncomeType, Period, GrossInr, NetInr, IsRecurring, EvidenceDocumentId, ContributorMemberId, Narration, ReleaseId
Relationships: Income Earned By Customer; Income Evidenced By Document; Income Contributed By Member

### Goal (key: GoalId)
A financial goal (emergency reserve, school fee, home down payment, retirement income). Frontier goals carry target, current amount and a priority rank; legacy goals carry stated intent and priority. Goals compete for the same cash: never count one rupee twice. Standards: BIAN 13.0.0 Customer Relationship Management (provisional); no Microsoft CDM reference. Demo mapping, not certification. Star role: dimension; grain: one goal. Lakehouse: dim_goal <- silver_goal <- goals.json.
Synonyms: savings goal, objective, target, plan
Properties: GoalId, CustomerId, Name, PriorityRank, StatedPriority, Status, TargetInr, CurrentInr, ProgressPct, TargetDate, TargetFrequency, IntentSummary, FundingProductIds, ReleaseId
Relationships: Goal Owned By Customer; Mandate Funds Goal

### Preference (key: PreferenceId)
A stated customer preference as a field/value pair: contact channel, debt preference, emergency reserve months, which decisions need a human, communication style. Honour them before suggesting anything. Standards: BIAN 13.0.0 Customer Relationship Management (custom); no Microsoft CDM reference. Demo mapping, not certification. Star role: dimension; grain: one preference key per customer. Lakehouse: dim_preference <- silver_preference <- preferences.json.
Synonyms: setting, customer preference, stated preference
Properties: PreferenceId, CustomerId, Field, PreferenceValue, RecordedOn, StatedIn, Note, ReleaseId
Relationships: Preference Stated By Customer

### Consent (key: ConsentId)
A recorded customer consent: who may see or do what, for which purpose, until when. Consent is specific and revocable: a conversation, upload or earlier consent never creates a new permission. Agent read consent and mandate consent are separate consents. Standards: no verified BIAN mapping (custom semantics); no Microsoft CDM reference. Demo mapping, not certification. Star role: dimension; grain: one consent. Lakehouse: dim_consent <- silver_frontier_consent + silver_consent <- consents.json.
Synonyms: permission, authorisation, authorization, grant of consent
Properties: ConsentId, CustomerId, Purpose, Status, GrantedAt, ExpiresAt, RevokedAt, SubjectRef, SubjectType, SubjectMemberId, RecipientScope, Revision, ResourceCount, ReleaseId
Relationships: Consent Given By Customer; Consent About Household Member; Access Grant Backed By Consent; Agent Authorised By Consent; Mandate Backed By Consent

### HouseholdAccessGrant (key: GrantId)
A household access grant (permission grant): what a household member (or agent) may see or do on the owner's data. Frontier: Vivek can view goals, obligations and budget only; Anika (minor) has no access; Savitri sees the care schedule only. Denied permissions always win; a relationship never implies permission. Standards: no verified BIAN mapping (custom semantics); no Microsoft CDM reference. Demo mapping, not certification. Star role: dimension; grain: one access grant. Lakehouse: dim_household_access_grant <- silver_household_access_grant <- access-grants.json.
Synonyms: access grant, household access, permission grant, sharing grant, viewer access
Properties: GrantId, CustomerId, HouseholdId, ConsentId, SubjectRef, SubjectMemberId, Status, Permissions, DeniedPermissions, ReleaseId
Relationships: Access Grant Of Customer; Access Grant Backed By Consent; Access Grant For Household Member; Access Grant In Household

### RegisteredAgent (key: AgentId)
A customer-side AI agent registered with the bank through Know Your Agent (KYA), for example Rhea's personal agent. KYA is registration, not consent or a mandate. An agent cannot register itself, widen its scopes or approve its own prepared action. Standards: no verified BIAN mapping (custom semantics); no Microsoft CDM reference. Demo mapping, not certification. Star role: dimension; grain: one registered agent. Lakehouse: dim_registered_agent <- silver_registered_agent <- agents.json.
Synonyms: personal agent, customer agent, delegate, AI agent, KYA agent
Properties: AgentId, CustomerId, HouseholdId, ConsentId, RegistrationType, DelegationMode, Status, Scopes, DeniedScopes, KyaProviderName, KyaRegistrationId, KyaVerificationMethod, KyaVerifiedAt, ReleaseId
Relationships: Agent Registered For Customer; Agent Authorised By Consent; Mandate Bound To Agent

### FrontierMandate (key: MandateId)
A mandate: a bounded standing authority for an agent to move surplus cash into a goal (surplus_to_goal) within a per-action cap, a monthly cap and a minimum balance that must remain in the source account. Frontier mandate-surplus-001 has been revoked; a revoked, paused or expired mandate cannot execute. Standards: no verified BIAN mapping (custom semantics); no Microsoft CDM reference. Demo mapping, not certification. Star role: dimension; grain: one mandate. Lakehouse: dim_frontier_mandate <- silver_frontier_mandate <- mandates.json.
Synonyms: mandate, delegated mandate, surplus-to-goal mandate, standing authority, delegated authority, auto-transfer rule, surplus rule
Properties: MandateId, CustomerId, AgentId, ConsentId, SourceAccountId, DestinationAccountId, GoalId, Status, Currency, PerActionLimitInr, MonthlyLimitInr, MinimumSourceBalanceInr, ValidFrom, ValidFromDateIst, ValidUntil, ValidUntilDateIst, EventCount, ReleaseId
Relationships: Mandate Granted By Customer; Mandate Bound To Agent; Mandate Backed By Consent; Mandate Draws On Account; Mandate Funds Account; Mandate Funds Goal; Event Of Mandate

### MandateAction (key: EventId)
A recorded mandate action (mandate event) in a mandate's life: created, action executed, action skipped (with the reason, for example obligations protected) or revoked. It is the audit trail of what a delegated agent actually did. Standards: no verified BIAN mapping (custom semantics); no Microsoft CDM reference. Demo mapping, not certification. Star role: fact; grain: one mandate action. Lakehouse: fact_mandate_action <- silver_mandate_action <- mandates.json.
Synonyms: mandate event, mandate action, executed or skipped action, mandate history, delegated action, agent action log
Properties: EventId, MandateId, EventType, OccurredAt, OccurredDateIst, ActorRef, ActorType, AmountInr, SourceBalanceAfterInr, Reason, Narration
Relationships: Event Of Mandate

### Incident (key: IncidentId)
A fraud, scam or dispute case on a customer's money, for example an authorised-payment scam and its recovery. Service first: while an incident is unresolved, no product is sold to the customer. Recovery amount is what was credited back. Contoso extension entity: no CDM profile entity, BIAN status unmapped. Star role: fact; grain: one incident. Lakehouse: fact_incident <- silver_incident <- incidents.json.
Synonyms: scam case, fraud case, dispute, complaint, loss event
Properties: IncidentId, CustomerId, IncidentType, Status, DetectedAt, DetectedDateIst, AmountInr, RecoveryAmountInr, RecoveryEvidenceDocumentId, EvidenceDocumentIds, Narration, ReleaseId
Relationships: Transaction Disputed In Incident; Incident Affects Customer; Service Record About Incident

### ServiceCase (key: ServiceRecordId)
A service case (service record): a servicing contact on a customer's behalf: channel, actions taken and, when applicable, the consented handoff to a human team. A recorded action is not a completed effect: never claim a card is blocked or money returned unless the case says so. Standards: BIAN 13.0.0 Customer Case (custom), Servicing Order (provisional); no Microsoft CDM reference. Demo mapping, not certification. Star role: fact; grain: one service case. Lakehouse: fact_service_case <- silver_service_case <- service-records.json.
Synonyms: service record, service request, servicing contact, case record, handoff, support contact
Properties: ServiceRecordId, CustomerId, IncidentId, Channel, OpenedAt, OpenedDateIst, Actions, HandoffTeam, HandoffReason, HandoffAt, ReleaseId
Relationships: Service Record For Customer; Service Record About Incident

### ServiceRequestType (key: ServiceTypeId)
A kind of service request the bank can record (card freeze, dispute, ...), with the honest outcome wording and the things that must never be claimed (card actually blocked, reversal done, issuer contacted). Recording an instruction is not executing it. Contoso extension entity: no CDM profile entity, BIAN status unmapped. Star role: dimension; grain: one service request type (curated reference source). Lakehouse: dim_service_request_type <- Curated reference data.
Synonyms: service type, request type, service request
Properties: ServiceTypeId, Title, Summary, BuId, JourneyIds, Priority, CaseTitle, StageLadder, ActivationMode, RecordedOutcome, NeverClaim

### Document (key: DocumentId)
A customer document: salary slip, school fee circular, scam message, card statement, FD renewal notice, loan offer. Documents are evidence only when the customer selected them and consented; FactsText holds the key facts in readable form. Content itself is in Foundry IQ, not here. Standards: BIAN 13.0.0 Document Directory (custom); Microsoft CDM CustomDocument (semantic reference only). Demo mapping, not certification. Star role: dimension; grain: one document. Lakehouse: dim_document <- silver_document <- documents.json.
Synonyms: evidence, statement, letter, notice, receipt, payslip, file
Properties: DocumentId, CustomerId, Title, DocumentType, Issuer, IssuedOn, PageCount, Purpose, Visibility, SourceVersion, SupersededByDocumentId, SupersedesDocumentId, FactsText, ReleaseId
Relationships: Income Evidenced By Document; Document Owned By Customer

### CrmCase (key: CaseId)
A CRM case that continues a conversation with a human relationship manager: stage, priority, sentiment, SLA, owning RM, journey and products discussed. Legacy release only. Cases belong to ReleaseId retail4-v1. Contoso extension entity: no CDM profile entity, BIAN status unmapped. Star role: fact; grain: one CRM case. Lakehouse: fact_crm_case <- silver_crm_case <- bank/crm-cases.json.
Synonyms: case, ticket, service request, lead, follow-up
Properties: CaseId, CustomerId, Title, Summary, Stage, Priority, Sentiment, Channel, Origin, BuId, JourneyId, OwnerRmId, ProductIds, Interactions, ObjectionReason, ObjectionProductId, OpenedAt, OpenedDateIst, UpdatedAt, SlaDueAt, ReleaseId
Relationships: Case Opened For Customer; Case Follows Journey

### Product (key: ProductId)
A bank product: savings, fixed and recurring deposit, sweep, credit and debit card, home loan, health cover. 24 catalogue products carry full terms; products named only by Frontier records (card, floating home loan, education term cover, index fund) are reference stubs with no terms: do not quote their rates or fees. Rupee parameters are whole rupees, rates are percent. Standards: BIAN 13.0.0 Product Directory (custom); no Microsoft CDM reference. Demo mapping, not certification. Star role: dimension; grain: one catalogue product. Lakehouse: dim_product <- silver_product <- products.json, relationship/products.json.
Synonyms: offering, scheme, plan, card type, deposit scheme
Properties: ProductId, DisplayName, Family, DataRole, Status, Issuer, CatalogueVersion, EffectiveFrom, EffectiveTo, Audience, MainTradeoff, WhyNotSuitable, RateTerms, LiquidityTerms, BenefitsText, FeesText, LimitationsText, RequiredEvidence, SupportedActions, UnderwritingRequired, MinAge, MinMonthlyIncomeInr, MinPrincipalInr, MaxPrincipalInr, InterestRatePct, AnnualFeeInr, MonthlyFeeInr, MinimumBalanceInr, ProcessingFeePct, PrepaymentChargePct, EarlyWithdrawalPenaltyPct, MinTermMonths, MaxTermMonths, MaxTenureYears, MaxLtvPct, CashbackPct, ForexMarkupPct, MonthlyPremiumInr, SumInsuredInr, RateType, PayoutMode, Disclosure, ReleaseId
Relationships: Account Opened Under Product; Card Of Product; Deposit Of Product; Loan Of Product; Policy Of Product; Investment Of Product

### Journey (key: JourneyId)
A conversation journey the assistant runs. Legacy J01-J12 are goal-led playbooks (retirement income, scam check, card fit, ...) with triggers, must-establish facts and never-do rules. Frontier F01-F06 are the household journeys, each with specialists, an autonomy ceiling and the operations it may only prepare. Contoso extension entity: no CDM profile entity, BIAN status unmapped. Star role: dimension; grain: one journey (curated reference source). Lakehouse: dim_journey <- Curated reference data.
Synonyms: playbook, use case, conversation flow, scenario, intent flow
Properties: JourneyId, Title, JourneyKind, OneLiner, Goal, BuId, SalesPosture, TurnCap, AutonomyCeiling, PrimarySpecialists, AllowedPreparedOperations, TriggersText, MustEstablishText, NeverDoText, ToolHints, OpenerExamples
Relationships: Case Follows Journey

### Sop (key: SopId)
A standard operating procedure the assistant follows (retirement income review, suspected fraud, card controls, request handling). The procedure text lives in Foundry IQ; this entity says which SOP exists, its scope and which case it applies to. Contoso extension entity: no CDM profile entity, BIAN status unmapped. Star role: dimension; grain: one standard operating procedure. Lakehouse: dim_sop <- silver_sop <- sops.json.
Synonyms: procedure, runbook, bank procedure, policy document
Properties: SopId, Title, Scope, Audience, AppliesToCase, KnowledgePath, SourceVersion, ReleaseId

### Law (key: LawId)
One of the bank's twelve non-negotiable design laws for the conversational and agentic experience (no figure without authority, no consent by implication, no execution by language, no double allocation, no future money as cash, no stale authority, no self-expanding agent, no sales during harm, ...). The same laws are loaded as ontology rules. Contoso extension entity: no CDM profile entity, BIAN status unmapped. Star role: reference; grain: one design law. Lakehouse: ref_law <- Curated reference data.
Synonyms: rule, principle, guardrail, constraint, policy
Properties: LawId, Name, RequiredBehavior

### AuthorityRule (key: RuleId)
Who may do what: for each capability (ask, read, calculate, retrieve policy, prepare, approve, execute, review, expand consent) the permission of each actor (customer, Financial Companion, specialist, Rhea agent, active mandate, human specialist, deterministic bank). Analysis, preparation and effect are separate; the default agent mode is prepare-only. Contoso extension entity: no CDM profile entity, BIAN status unmapped. Star role: reference; grain: one authority rule: a capability and the actor's permission for it. Lakehouse: ref_authority_rule <- Curated reference data.
Synonyms: authority matrix, permission matrix, who can approve, who can execute
Properties: RuleId, Capability, Actor, PermissionClass, PermissionText

## Metrics (semantic model measures)
- Customers: Number of customers and parties.
- Frontier Customers: Customers in the current Frontier release.
- Total Posted Balance (INR): Sum of posted account balances in rupees. Filter ReleaseId before adding customers.
- Total Available Balance (INR): Sum of balances available after holds, in rupees.
- Total Deposits (INR): Sum of fixed deposit principal in rupees. A Frontier deposit also appears as an investment: do not add both.
- Total Investments (INR): Sum of current investment value in rupees, including the liquid fixed deposit.
- Loan Principal Outstanding (INR): Principal still owed on loans, in rupees.
- Card Outstanding (INR): Amount currently owed on cards, in rupees.
- Card Credit Limit (INR): Approved card credit limits in rupees. A limit is borrowing capacity, not cash.
- Credit Utilisation: Card outstanding as a share of credit limit.
- Total Liabilities (INR): Loan principal plus card outstanding, in rupees.
- Total Assets (INR): Posted balances plus investments in rupees. Deposits are inside investments for Frontier, so they are not added again.
- Net Position (INR): Assets minus liabilities in rupees. Excludes the home's market value: the seed holds no property valuation, so it is negative while a home loan is outstanding.
- Transaction Count: Number of transactions.
- Total Debits (INR): Money out in rupees.
- Total Credits (INR): Money in in rupees.
- Net Cash Flow (INR): Credits minus debits in rupees.
- Disputed Transactions: Transactions tied to a dispute or scam incident.
- Monthly Obligations (INR): Active monthly commitments in rupees.
- Monthly EMI (INR): Monthly loan instalments in rupees.
- Interest Paid To Date (INR): Interest paid in instalments already paid, in rupees.
- Average Monthly Net Salary (INR): Average net salary credit per month, in rupees.
- Debt Service To Income: Monthly EMI as a share of average net salary.
- Lump Sum Goal Target (INR): Targets of goals with a lump-sum target, in rupees.
- Goal Current (INR): Amount set aside across goals, in rupees.
- Goal Funding: Amount set aside as a share of lump-sum targets.
- Active Consents: Consents that are granted and not revoked.
- Active Access Grants: Household access grants that are active.
- Revoked Mandates: Mandates that were revoked.
- Mandate Executed (INR): Total moved by executed mandate actions, in rupees.
- Mandate Skipped Actions: Mandate actions skipped to protect obligations.
- Incidents: Fraud, scam and dispute incidents.
- Recovered Amount (INR): Amount recovered across incidents, in rupees.
- CRM Cases: Number of CRM cases.
- Open CRM Cases: CRM cases not yet resolved or declined.
- Average Interactions Per Case: Mean interactions per CRM case.
- Documents: Number of customer documents.
- Catalogue Products: Products with full approved terms.
- Highest Fixed Deposit Rate (%): Highest fixed deposit interest rate in the catalogue, percent.

## Business rules (advisory context, not enforced by the ontology)
- NoFigureWithoutAuthority: Law L1. A balance, rate, forecast, affordability result or executable amount that did not come from the bank's own records or a registered deterministic calculation must not be presented as banking truth. Quote money only from the Inr properties, with the date they are valid for (Account.BalanceAsOn).
- NoInvisibleSourceUse: Law L2. Customer documents may be used as evidence only after the customer selected them and consented to their reading. Cite the Document and the Consent that covers it; a document the customer did not select is not a source.
- NoConsentByImplication: Law L3. A conversation, upload, recommendation, earlier consent or personal-agent request never creates a new permission. A household role (spouse, child, parent) gives no access by itself: only an active HouseholdAccessGrant that lists the permission, backed by a granted Consent, does.
- NoExecutionByLanguage: Law L4. Saying 'do it', an agent tool call or a specialist recommendation is not authority to execute. Only the deterministic bank, on the current revision and with a customer decision or an active FrontierMandate, can execute. The assistant may analyse and prepare only; see AuthorityRule for who may do what.
- NoDoubleAllocation: Law L5. Cash, collateral, commitments and goal balances must never be counted twice across plans or goals. A deposit that also appears as an Investment is one asset, not two: do not add Account balances, Deposit principal and Investment value together without removing the overlap.
- NoFutureMoneyAsCash: Law L6. Forecast income, offers, unpaid invoices, promised refunds, unused credit limits, unposted deposit maturities and loan proceeds are not spendable cash. Available cash is the posted, available balance of owned accounts, less protected cash and committed obligations.
- NoStaleAuthority: Law L7. Revalidate when sources, balances, policy, consent or mandate changed. A FrontierMandate that is revoked, paused or past ValidUntil cannot execute; a Consent that is revoked or past ExpiresAt gives no access.
- NoSelfExpandingAgent: Law L8. A RegisteredAgent cannot register itself, widen its scopes, grant consent, create a mandate or approve its own prepared action. Anything listed in DeniedScopes is denied even if a scope seems to allow it. KYA registration is not consent and not a mandate.
- NoSalesDuringHarm: Law L9. While a customer has an unresolved Incident (fraud, scam, dispute, coercion or vulnerability) do not sell or recommend new products. Stay service-first, explain next steps and offer a human handoff.
- NoSyntheticToLive: Law L10. Every record is synthetic demo data. Never present it as live bank data, as production evidence or as a real provider's response, and keep the disclosure when asked what the data is.
- NoCrossHouseholdLeakage: Law L11. Keep owner, household, release, document access and CRM context aligned. Never reveal one customer's or household's data to another person, and never combine records across ReleaseId.
- DefaultToFrontierRelease: Unless a persona is named otherwise, answer from ReleaseId retail-frontier-v1 (the Rhea Bansal household). Customer rhea~legacy is an older, non-reconciled copy of the same person from retail4-v1: it is superseded and must never be merged with rhea or added to rhea's totals.
- HonestRecording: Recording a service instruction is not executing it. Never say a card is blocked, a transaction reversed, an issuer or network contacted, or money returned unless the ServiceCase or Incident shows it. Use ServiceRequestType.NeverClaim for the statements that must not be made.
- SelectionIsNotApplication: Selecting a product is not applying, applying is not approval, approval is not acceptance and acceptance is not execution. A recorded 'no' closes the case: do not re-offer the same product.
- CreditLimitIsNotCash: A card's CreditLimitInr and AvailableCreditInr are borrowing capacity, not an asset or a cash balance. Never add them to account balances or net position.
- LoanProceedsAreLiability: A loan disbursement creates an equal liability. It is not income and not net wealth: Loan.CurrentPrincipalInr is what is still owed.
- UnitsAndCurrency: Properties ending Inr are rupees. Properties ending Paise are exact integer paise (100 paise = 1 rupee) and are for reconciliation only. 'Crore' means 10,000,000 rupees. Properties ending Pct are percentages (8.5 = 8.5%). Product rupee parameters such as AnnualFeeInr are whole rupees.
- MaskSensitiveIdentifiers: Never speak or write masked card numbers, mobile numbers, policy numbers, addresses or KYA fingerprints in full. Reveal at most the last four characters, and in voice never read an identifier digit by digit unless the customer is verifying it.
- HonourHumanReviewPreference: Honour the customer's stated preferences. When Preference field humanReviewFor lists new_credit, insurance_purchase, mandate_change or household_access, those decisions always need a human specialist.
- PrepareOnlyByDefault: Agents act prepare-only by default. The only delegated money movement is a bounded surplus_to_goal FrontierMandate, valid only if the current balance, protected cash, per-action cap, monthly cap, minimum source balance, validity dates and consent all still pass at execution time.
- HouseholdAccessIsAsymmetric: Household access is resource-specific. A spouse's view grant does not allow transactions, mandate changes or access changes; a minor has no inferred access; a dependent parent sees the care schedule only. Private messages, accounts and transactions stay outside spouse and dependant grants unless explicitly added.
- ProtectEmergencyReserveFirst: Protect the emergency reserve before suggesting prepayment, investment or a mandate transfer. The reserve target is the customer's emergencyReserveMonths preference applied to essential outgoings; a goal gap is not a reason to spend the reserve.
- SpeakMoneyAndDatesForVoice: In voice, say amounts in rupees with Indian grouping (lakh, crore), round only for speech and never in the source figure, state the date a balance is valid for, and read dates as day, month, year in India time.
