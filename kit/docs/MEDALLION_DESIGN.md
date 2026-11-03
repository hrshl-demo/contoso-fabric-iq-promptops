# Medallion redesign: CDM/BIAN silver, star-schema gold, ontology on top

Status: design contract for the `medallion-cdm` branch. Offline work only. Nothing in this change may run `build.sh`, `deploy`,
`provision`, `load`, `up`, `down` or any Azure/Fabric call (the offline `python -m contoso_onto.cli build --out <temp>` is fine).

## 1. What the user asked for (verbatim intent)

"The ontology must respect the BIAN-based Microsoft Common Data Model for its silver. Facts and dimensions must be there and the
ontology created accordingly." Chosen interpretation (user confirmed): **the superapp's approved CDM profile is the base, plus the
official Microsoft CDM column names where they match.**

## 2. Target architecture

```
bronze (raw seed JSON, read-only repo)
  -> silver_*   conformed to the CDM profile (+ official-CDM alias columns, + BIAN lineage)   [Delta, lakehouse]
  -> dim_* / fact_* / bridge_* / ref_*   Kimball-style star, built FROM silver                [Delta, lakehouse]
  -> Direct Lake semantic model (gold only)  +  Gen 2 ontology (gold only)  +  graph  +  agents
```

Decisions already made (do not revisit):

1. One plain lakehouse `ContosoBankLH` (no schemas). Layers are told apart by name prefix and by OneLake `Files/bronze/...`.
2. The ontology and the semantic model bind to **gold only**. Silver is never bound to the ontology.
3. Ontology entity names, property names, relationship names, rules, measures and golden questions **do not change**. Only the
   lakehouse table names behind them change (`customer` -> `dim_customer`). Ontology counts stay frozen: core 31 entities / 417 properties /
   53 relationships / 23 rules / 39 measures; full 50 / 534 / 78 / 23 / 42. The only approved addition is **semantic-only date-role relationships**
   (`ontology: false`, from a fact's date column to `dim_date`) where the check `S060` (no ambiguous filter paths) still passes and no measure result
   changes; the new semantic-relationship counts are then recorded in docs and tests (v1: core 35, full 54). Any other deviation needs a documented reason.
4. Natural business keys are the conformed keys (the ontology binds on them). Surrogate keys only where SCD2 would need them (none here).
5. Gold is built from silver. Documented exceptions (not CDM banking data): `dim_date` (generated) and curated reference sources
   (laws, authority matrix, requirements, bank agents, service request types, journeys, journey links), which come from repo docs/config.
6. Release handling is unchanged: both seed releases stay side by side, legacy ids are namespaced (`rhea~legacy`) in gold exactly as today.
7. **Logical id vs physical name.** Every table keeps its v1 name as the stable *logical id* (`account`). Relationships, measures, rules, tiers
   (`SMOKE_TABLES`), oracles (`T["account"]`), most tests and the equivalence fixtures keep using logical ids. A new `physical` field holds the
   lakehouse name (`dim_account`). Physical names are used only at output boundaries: Parquet file names, Load Table names, TMDL partition
   `entityName`, SQL/GQL snippets in the agent pack and data dictionary, ledger/state keys, docs. A test proves no v1 logical name leaks as a
   lakehouse object name in any generated artifact (`tests/test_physical_names.py`).
8. The profile's mappings are the **specification, not code** (`README.txt`: "descriptive, not an executable query or expression language").
   The silver builder implements them with kit transforms; conformance tests check the result against the profile.

## 3. Bronze

- Raw files are the pinned seed JSON (the files `gold.Repo` reads today, per release). No transformation.
- Output `out/bronze/manifest.json` and a Delta table `bronze_file_manifest` (release, path, sha256, bytes, record_count).
- Pipeline uploads the used JSON files verbatim to `Files/bronze/<release>/<relative path>` (no PDFs/PNGs/SVGs/markdown).

## 4. Silver (CDM profile base + official alias columns + BIAN lineage)

Sources of truth (read-only, pinned in the repo snapshot, all under `data/banking-model/v1/`): `profile.json` (45 entity definitions,
`commonEntityColumns`, `relationships`, `money`), `field-mappings.json` (459 attribute mappings with `valuePointer`, `transform`,
units, privacy), `bian-registry.json` (BIAN 13.0.0, 17 domains, 574 attribute mappings: 278 custom / 79 provisional / 217 unmapped),
`vendor/cdm/references/*.cdm.json` (official Microsoft CDM entity files, sha-pinned).

### 4.1 Tables

- One table per profile entity that has seed data, named `silver_<snake(entity)>` (for example `silver_account`, `silver_frontier_mandate`).
- One table per kit entity that has no profile entity but has seed data (for example `Household`, `Investment`, `LoanInstallment`,
  `IncomeRecord`, `Incident`, `CrmCase`, bank-ops tables, link tables). These are **Contoso extension entities**: entity-level flag
  `profile: false`, every column an extension column, honestly labelled, BIAN status `unmapped` unless the registry says otherwise.
  Never upgrade a mapping status (provisional stays provisional, unmapped stays unmapped).
- Two catalog tables, queryable through the SQL endpoint:
  - `silver_entity_catalog`: entity, silver_table, profile_entity (nullable), extension (bool), cdm_reference_entity, cdm_reference_status,
    bian_domains, source_files, grain, row_count.
  - `silver_attribute_catalog`: silver_table, column_name (not `column`: a reserved word in T-SQL), origin (`profile` | `common` | `cdm_official` | `extension` | `technical`),
    profile_attribute, cdm_type, unit, nullable, privacy, bian_domain, bian_control_record, bian_mapping_status, official_entity,
    official_attribute, match_type, source_pointer, transform.
  These two tables are the machine-readable standards traceability (replaces the idea of a hand-written mapping page).

### 4.2 Columns (in this order)

1. Profile common columns from `commonEntityColumns`. Per the profile (`build_profile.py`) `id`, `label`, `state`, `sourceKind`, `historical` and
   `sourceRecordId` are **non-nullable**; only `sourceVersion` is nullable. Derive them deterministically and document the rule per entity in the
   spec (the table's `common:` block, with the cited app lines): `sourceRecordId` = the record's own business key; `sourceVersion` = the seed record's
   version if it has one, else null; `id` = `<Entity>:<businessKey>` (opaque, unique within the entity, with the same legacy namespacing as gold);
   `sourceKind`, `state`, `label` and `historical` follow the **source app's own projection rules** (`app/data_xray/projection.py`, closed `SourceKind`
   vocabulary in `app/data_xray/contracts.py`; see "Silver implementation notes"), never a placeholder of our own (no `seed`, no `derived:recorded`).
   The builder must fail (not null-fill) if a non-nullable common column cannot be derived.
2. **Profile attributes, exact names and meaning**: camelCase as in `profile.json`; `string`->string, `integer`->int64, `boolean`->bool,
   `date`->date, `datetime`->timestamp (UTC), `minor`->int64 **paise** (never doubles, never renamed to major units), `decimal` (exactDecimal, used
   for rates/confidences)-> **canonical decimal text (string)**, because the profile defines exact decimal text and unbounded precision. Respect
   each attribute's `nullable`, `unit` and the transform semantics in `field-mappings.json`. Missing/unknown values stay null, never manufactured
   zeros. Where the kit's v1 gold rule and the profile rule disagree, follow the profile in silver and record the difference in
   `tests/fixtures/gold_v1/ALLOWED_DIFFS.yaml` only if gold output would change. Silver needs its **own** physical-name validator (camelCase
   allowed, case-insensitive uniqueness, no special characters); the gold `SNAKE_RE` does not apply to silver.
3. **Official CDM alias columns** (see section 5) for matched attributes, named exactly as in the official CDM entity file.
4. **Extension columns** `x` + PascalCase (for example `xAgeYears`) for seed fields the kit needs downstream that the profile lacks. For
   extension entities (no profile entity) ordinary camelCase, no prefix.
5. **Technical columns**: `_release_id`, `_source_file`, `_source_row` (int), `_owner_id` (the record's owner scope when the seed has one).
   No wall-clock timestamps (builds must stay deterministic).

Rules: nested objects are flattened, arrays become child silver tables with parent key + ordinal (the v1 gold already splits them); column names
unique case-insensitively; no special characters (Fabric column mapping); restricted PII columns (date of birth, KYC address, email, ...) stay in
silver and keep `ontology: false` downstream exactly like v1; silver rows are source-faithful per release, no cross-release merging, key unique on
(`_release_id`, business key).

### 4.3 Silver-to-gold source contract (what makes "gold from silver" executable)

The v1 row engine (`gold.py`: `source_rows`, `build_rows`, `CUSTOM_SOURCES`) reads raw nested seed records (money objects, parent rows `^`, `@`,
`$index`, `$release`, `t: owner`) and a set of custom flatteners (`calendar`, `preference_unpivot`, `product_parameter`, `monthly_metric`,
`authority_matrix`, `frontier_journeys`). Required changes:

1. **Silver is a superset of what gold needs.** Every seed field a gold column uses must exist in silver as a profile, official-alias or extension
   column in flat, typed form. If a gold column needs a seed field that silver lacks, add the extension column to silver, never read bronze from gold.
2. **The bronze-to-silver step owns all raw-shape work**: money object -> `*Minor` int64 + currency, array -> child silver table with parent key +
   ordinal (`^`, `@`, `$index` live here), `_custom_*` flatteners that read seed (preference unpivot inputs, product parameters, monthly metrics) move
   here. `release` becomes `_release_id`.
3. **Gold sources** become `{silver: <silver_table>}` (optionally with `join:` on parent keys for child-to-parent attributes). Gold columns reference
   silver column names; money transforms become `minor_to_inr` / `minor_to_paise` / `minor_to_pct`-style transforms over the int64 minor value.
   The legacy-id namespacing (`t: owner`, `superseded_by`) stays in gold and reads `_release_id` and `_owner_id` from silver.
4. **Reference sources** (laws, authority matrix, requirements, bank agents, service request types, journeys, journey links, calendar) stay on their
   current docs/config readers and are labelled `kind: reference` in the spec.
5. A migration script (kept in `tools/`, with a unit test) rewrites the gold specs mechanically and writes a per-gold-column silver-input report; the
   v1 equivalence fixtures are the arbiter. Nothing is accepted by eye.

### 4.4 Conformed-key policy (releases)

Silver keeps both releases side by side with no loss (key unique on `_release_id` + business key). The v1 gold already applies survivorship; gold v2
must apply **exactly the same** survivorship: legacy-id namespacing for known owner collisions, "catalogue product wins" for duplicate product ids
(`allow_duplicates` and precedence by source order), and the same FK rewrites. A test proves **no silver row is dropped on the way to gold except rows
named in a documented survivorship list with expected counts** (for example the 6 duplicate customer keys and 1 duplicate product key that the v1 build
already reports as "merged by precedence").

## 5. Official CDM crosswalk (`model/cdm_crosswalk.yaml`)

Official reference entities (vendored, sha-pinned): FHAccount (Account), FICard (Card), FHSaving (Deposit), FHLoan (Loan), Contact (Customer),
Relationship (PartyRelationship), CustomDocument (Document, EvidenceReading). The profile calls them semantic references only; this file adds
**attribute-level matches** so silver can carry the official column name next to the profile column.

```yaml
crosswalk:
  - profile_entity: Account
    official_entity: FHAccount            # must equal profile.cdmReference.entityName
    attributes:
      - {profile: postedBalanceMinor, official: balance, match: derived_major_units, official_type: currency,
         transform: "minor/100 as decimal(19,4)", note: "posted balance; official 'balance' is the holding value"}
      - {profile: accountId, official: integrationKey, match: semantic, official_type: string, note: "business id; official FH_AccountId is a GUID"}
```

Rules: a match needs semantic equality, not just similar words. Every alias entry carries `rationale` (why the meanings are equal), `evidence` (the
official attribute's own description, quoted from the vendored file), `unit` and `null_rule`. `match` in {`exact`, `semantic`,
`derived_major_units`, `type_narrowed`}. Never fabricate precision or timezone (a date-only source stays a date even if the official attribute is
`dateTime`; record `type_note`). **Unsupported official types are skipped and listed** in the file under `skipped:` with a reason (GUID/`entityId`,
`listLookup` option sets, `*_Base` / `*_default` converted-currency twins, Dataverse system columns such as `createdOn`, `modifiedOn`, `statecode`,
`importSequenceNumber`) unless the seed truly supplies the value. Official `currency` values are decimals in major units, stored **only in silver** as
`decimal(38,4)` (int64 paise / 100 needs 17 integral digits, so `decimal(19,4)` is too small); decimals return null in Fabric Graph, so gold and the
ontology never see them. The first live load of a decimal column is an unverified item in `docs/LIVE_CHECKLIST.md`.
Tests: (a) every `official` name exists in the vendored official entity file for that `official_entity` and the declared official type equals the
file's data type; (b) every alias has non-empty `rationale`, `evidence`, `unit`, `null_rule`; (c) **row-level**: for every alias column the value
equals the base profile column after the declared transform for every silver row (no drift). Wording everywhere (silver catalog, docs, ontology
text): "official-name alias" and "semantic reference", never "CDM conformance" or "BIAN certified".

## 6. Gold: star schema

Naming: `dim_*` (descriptive, conformed), `fact_*` (events and periodic snapshots with measures and dimension keys), `bridge_*` (many-to-many
links), `ref_*` (curated reference/policy data). Every gold table spec gains `kind`, `grain` (one sentence) and `silver` (source silver table(s))
or `reference` (source), and a `physical` name (design decision 7). Every fact has a documented grain, a unique grain key, foreign keys that resolve
to dimensions, and a date column typed `date`; a semantic-only date-role relationship to `dim_date` is added where decision 3 allows it (v1 already has
five: transaction, loan instalment, CRM case, mandate event, incident). Dimensions have a unique business key. Gold may keep the v1 current-state
columns on dimensions (balances, limits) as type-1 attributes as of the release business date; document this in the table description.

Mapping of the 51 v1 tables (1:1, no splits or merges; the implementer may refine and must document any change):

| v1 table | v2 table | kind | grain |
|---|---|---|---|
| customer | dim_customer | dimension | one customer identity per release |
| household | dim_household | dimension | one household |
| household_member | dim_household_member | dimension | one household member |
| account | dim_account | dimension | one current account |
| card | dim_card | dimension | one card |
| deposit | dim_deposit | dimension | one deposit |
| loan | dim_loan | dimension | one loan |
| loan_installment | fact_loan_installment | fact | one scheduled instalment |
| insurance_policy | dim_insurance_policy | dimension | one policy |
| protection_gap | fact_protection_gap | fact (snapshot) | one computed gap per customer and cover type at the business date |
| investment | dim_investment | dimension | one investment holding |
| investment_goal | bridge_investment_goal | bridge | investment x goal |
| transaction | fact_transaction | fact | one ledger posting |
| transaction_evidence | bridge_transaction_evidence | bridge | transaction x evidence |
| scheduled_obligation | fact_scheduled_obligation | fact | one scheduled obligation |
| income_record | fact_income | fact | one income record |
| goal | dim_goal | dimension | one goal |
| preference | dim_preference | dimension | one preference key per customer |
| consent | dim_consent | dimension | one consent |
| consent_resource | bridge_consent_resource | bridge | consent x resource |
| access_grant | dim_access_grant | dimension | one access grant |
| grant_permission | bridge_grant_permission | bridge | grant x permission |
| registered_agent | dim_registered_agent | dimension | one registered agent |
| agent_scope | bridge_agent_scope | bridge | agent x scope |
| mandate | dim_mandate | dimension | one mandate |
| mandate_event | fact_mandate_event | fact | one mandate action |
| incident | fact_incident | fact | one incident |
| service_record | fact_service_record | fact | one service case |
| proactive_event | fact_proactive_event | fact | one proactive candidate |
| service_request_type | dim_service_request_type | dimension (reference source) | one request type |
| document | dim_document | dimension | one document |
| crm_case | fact_crm_case | fact | one CRM case |
| product | dim_product | dimension | one catalogue product |
| product_parameter | dim_product_parameter | dimension (outrigger) | one product parameter |
| journey | dim_journey | dimension (reference source) | one journey |
| sop | dim_sop | dimension | one SOP |
| journey_sop | bridge_journey_sop | bridge (reference source) | journey x SOP |
| journey_product | bridge_journey_product | bridge (reference source) | journey x product |
| law | ref_law | reference | one law |
| authority_rule | ref_authority_rule | reference | one authority rule |
| frontier_requirement | ref_frontier_requirement | reference | one requirement |
| bank_agent | ref_bank_agent | reference | one bank agent |
| business_unit | dim_business_unit | dimension | one business unit |
| region | dim_region | dimension | one region |
| relationship_manager | dim_relationship_manager | dimension | one relationship manager |
| campaign | dim_campaign | dimension | one campaign |
| monthly_metric | fact_monthly_metric | fact | one metric per month and business unit |
| risk_alert | fact_risk_alert | fact | one risk alert |
| fraud_signal | fact_fraud_signal | fact | one fraud signal |
| product_business_unit | bridge_product_business_unit | bridge | product x business unit |
| calendar | dim_date | dimension (generated) | one calendar day |

**Equivalence rule (the safety net).** `tests/fixtures/gold_v1/` holds the v1 gold output (51 tables, 2,501 rows, frozen from commit c421458) and
`golden_expected_full.json` (the 29 expected answers). The v2 gold, mapped through the table above, must reproduce the v1 columns with the same names,
types and values (floats within 1e-9), the same row counts and the same golden expectations. Differences are allowed only if listed with a reason in
`tests/fixtures/gold_v1/ALLOWED_DIFFS.yaml` (default: empty).

## 7. Semantic model and ontology

- Tables, relationships, measures, rules and golden oracles are re-pointed to the new gold table names through one data-driven rename map.
  Semantic-model table names and DAX references keep using entity names (`Account[Posted Balance (INR)]`), so DAX is unchanged.
- `kind` and `grain` are recorded in the semantic-model/ontology descriptions (one short clause) and in the generated data dictionary.
- Entity descriptions already end with the BIAN/Microsoft CDM sentence (`standards.notes`). Extend it with a short lineage clause
  (`dim_account <- silver_account <- accounts.json`; official CDM entity where one exists). Respect the existing description-length validators.
- Conformance: every profile relationship between two entities that the ontology models exists in the ontology (otherwise it is on a documented
  exception list in `model/standards.yaml`); every ontology key equals the dimension/fact business key.

## 8. Pipeline (offline code and mocks only)

`python -m contoso_onto.cli build` writes `out/bronze`, `out/silver`, `out/gold` (each with its own `manifest.json`: table, physical name, rows,
sha256, schema), the catalogs, the TMDL parts and the eval files. Today `runner._up`, `cli.cmd_load` and `fabric_ops.load_gold` load only
`out/gold`, and `loadedFingerprint` hashes only the gold manifest; all three must become layer-aware: upload bronze files, load **silver first, then
gold** (each layer parallel inside itself; the order only helps bisecting a failure), and compute the fingerprint over **all layer manifests**. Tiers
(`smoke`, `core`, `full`) keep their meaning on gold; a tier loads only the silver tables its gold tables need (computed from the spec's `silver`
lineage) plus the two catalogs and the bronze manifest. Offline tests assert the Parquet schemas (int64 paise, string decimals, `decimal(38,4)` only on
official currency aliases, no decimal in gold). Wipe code is unchanged (it deletes the whole workspace and resource group). Mock-Fabric tests are
updated; no live run. `mock_fabric` is generic and will not catch a schema or case error, so add explicit schema assertions.

## 9. Acceptance

1. Full pytest suite green (baseline: 437 passed, 1 skipped) plus the new tests below; `ruff` clean if it was clean.
2. `tests/test_gold_equivalence.py` (v1 baseline), `tests/test_silver_conformance.py` (profile names, types, nullability, units; non-null common
   columns; BIAN/CDM catalog equals the registry), `tests/test_gold_star.py` (kinds, grain uniqueness, FK integrity, date columns, dimension key
   uniqueness, no decimals), `tests/test_cdm_crosswalk.py` (rules in section 5, including row-level alias values), `tests/test_lineage.py` (every gold
   table traces to silver or a reference source; no silver row dropped except the documented survivorship list), `tests/test_physical_names.py`
   (decision 7), `tests/test_layer_load.py` (layer manifests, silver-before-gold plan, all-layer fingerprint, Parquet schemas).
3. Ontology counts and golden expectations identical to the baseline; semantic counts differ from v1 only by approved date-role relationships.
4. Docs updated: README, ASSESSMENT, MANUAL_STEPS, LIVE_CHECKLIST, `docs/MEDALLION.md` (Mermaid lineage + star), Field Guide.

## 10. Explicit non-goals

No live Azure/Fabric action, no edits to the source repo, no official CDM SDK/corpus resolution, no Spark notebooks, no ontology entity renames,
no change to the pinned commit or the tier meanings.

## Crosswalk implementation notes

Implemented in `contoso_onto/cdm_crosswalk.py` (public API: `Alias`, `load_crosswalk`, `official_index`, `validate`, `apply_alias`); the reviewed
list is `model/cdm_crosswalk.yaml`; the checks are `tests/test_cdm_crosswalk.py` (the row-level alias test is added with the silver tests).

**Result: 16 official-name aliases**, fewer than the 25-45 that section 5 guessed. The vendored official files are thin where the profile is rich:
`Contact` defines only a GUID (`contactId`), and `CustomDocument` only a loan-application request category (`name`) and a `description`. Nothing was
invented to reach a number; "no match" is a result.

| profile entity -> official entity | aliases |
|---|---|
| Customer -> Contact | 0 (reviewed: the only attribute is a GUID; `ownerId` is declared "not a GUID") |
| Account -> FHAccount | 4: `accountId`->`integrationKey`, `postedBalanceMinor`->`balance`, `availableBalanceMinor`->`availableBalance`, `holdsMinor`->`blockedAmount` |
| Card -> FICard | 3: `cardId`->`integrationKey`, `maskedNumber`->`cardnumber`, `limitMinor`->`purchasingLimit` |
| Deposit -> FHSaving | 3: `depositId`->`integrationKey`, `maturesOn`->`maturitydate`, `ratePct`->`interestrate` |
| Loan -> FHLoan | 6: `liabilityId`->`integrationKey`, `principalMinor`->`principalAmount`, `outstandingMinor`->`balanceDisplayValue`, `monthlyPaymentMinor`->`installmentAmount`, `ratePct`->`interestrate`, `maturesOn`->`maturitydate` |
| Document -> CustomDocument | 0 (reviewed: `documentKind` covers any owner document, `title` is a title; neither is a loan-application request category) |
| PartyRelationship, EvidenceReading | listed under `skipped.entities`: runtime-only in the profile, no seed rows, so no silver table |

By `match`: exact 1, semantic 6, derived_major_units 7, type_narrowed 2. Every official attribute of the referenced entities is either aliased or listed
under `skipped.official_attributes` exactly once (categories `guid_or_lookup`, `system_column`, `option_set`, `converted_currency_twin`, `no_counterpart`),
and a test enforces the partition.

**Notable rejected candidates** (each has its reason in the YAML):
- `Card.expiry` -> `expiryDate`: the profile value is month/year text, "deliberately not an invented calendar date"; filling a date would invent a day.
- `Loan.outstandingMinor` -> `balance`: the same official file defines the display value of a liability as positive when the customer owes the bank
  ("a loan with an accounting value of -500 will appear as 500"), so by that description the stored accounting value `balance` of a loan is negative. The
  positive profile amount is aliased to `balanceDisplayValue` instead of silently inverting a sign.
- `Deposit.principalMinor` -> `balance`: principal stays recorded on closed (historical) deposits that no longer hold a balance.
- `Loan.openedOn` -> `disbursementDate` / `loanStartDate`: origination is neither the disbursement nor the first repayment (and escrow-funded loans are not disbursed).
- `Loan.linkedAccountId` -> `repaymentAccount`: "servicing account" is not stated to be the repayment source, and the seed loans carry no linked account.
- `Deposit.payoutMode` -> `maturityInstructionsDetails`: payout frequency during the term is not the handling instruction at maturity. (The retail4 seed field
  `maturityInstruction` is the real counterpart but is not a profile attribute; it would be an extension column with no alias.)
- `Card.productId` -> `productName` (an identifier is not a marketing name); `Card.cardType` and `Deposit.depositType` -> option sets (`listLookup`).
- `Customer` name and city -> `Contact`: the vendored Contact file has no such attributes.
- The weakest accepted judgement, flagged in its rationale: `Card.limitMinor` -> `purchasingLimit` (the official attribute does not separate a credit line
  from a spending sub-limit).

**Transform vocabulary** (closed; `validate()` rejects anything else): `identity` (base value unchanged; string, int64, bool, timestamp_utc, and exact
decimal text stays text), `minor_to_major_decimal` (int64 paise -> exact `Decimal` rupees, `value / 100`, two decimal places, sign kept, refuses bool, float
and anything outside int64), `date_keep` (a `date` stays a `date`; a `datetime` is refused, so no time or timezone is ever invented). Matching vocabulary for
`match`: `exact`, `semantic`, `derived_major_units` (only with `minor_to_major_decimal`, official `currency`, `decimal(38,4)`), `type_narrowed` (only with
`date_keep`, official `dateTime`, `date32`, and a `type_note`). An official `decimal` (the two rates) is carried as the profile's canonical decimal text
(`string`); `decimal(38,4)` is only for official `currency` aliases (a test shows `decimal(19,4)` overflows at the int64 extremes).

**Using it from silver**: `load_crosswalk()` gives `{profile entity: [Alias]}` (all six reviewed entities, two of them empty); the alias column value is
`apply_alias(alias, base_value)`, with missing values passed as `None`. `validate(crosswalk, official_index(repo_root), profile)` must return `[]` at build time.
`gold.load_model` merges every `model/*.yaml`, so the file's two top-level keys (`crosswalk`, `skipped`) end up in the merged model dict; nothing reads them
there, and a test keeps them from colliding with another model file.

**Wording**: use "official-name alias" and "semantic reference" in the silver catalog, docs and ontology text; never "CDM conformance", "CDM compliant" or
"BIAN certified". A test fails if the crosswalk's own texts say "conform" or "certif".

## Silver implementation notes

Code: `contoso_onto/silver.py` (builder, catalogs, manifests, validators), curated specs `model/silver/10_party ... 70_bank_ops.yaml` (one file per business
area, mirroring `model/gold/`; each table's `common:` block holds its sourceKind / state / label / historical rules with the cited app lines),
`python -m contoso_onto.cli build` (writes `out/bronze` and `out/silver` after gold; gold output is unchanged), tests
`test_silver_units.py`, `test_silver_build.py`, `test_silver_conformance.py`, `test_silver_superset.py`. `tools/draft_silver_spec.py` is the one-off drafting
aid that produced the first YAML; the committed YAML was curated by hand afterwards and is the only source of truth (nothing imports the tool).

**Inventory.** 43 silver tables: **19 profile-aligned** (Customer, Product, Account, Card, Deposit, Loan, ProtectionPolicy, Transaction, Goal, Preference, Document,
Consent, FrontierConsent, ServiceCase, HouseholdAccessGrant, RegisteredAgent, FrontierMandate, MandateAction, ProactiveCandidate; exactly the `ONTOLOGY_ENTITY`
keys minus `Commitment`) and **24 Contoso extension entities** (`profile: false`: Household, HouseholdMember, LoanInstallment, ProtectionGap, Investment,
InvestmentGoal, TransactionEvidence, ScheduledObligation, IncomeRecord, ConsentResource, GrantPermission, AgentScope, Incident, CrmCase, ProductParameter, Sop,
BusinessUnit, Region, RelationshipManager, Campaign, MonthlyMetric, RiskAlert, FraudSignal, ProductBusinessUnit), plus `silver_entity_catalog` (43 rows),
`silver_attribute_catalog` (1,020 rows) and `bronze_file_manifest` (44 seed files). 1,440 silver rows; 2,503 with the catalogs. Every non-reference v1 gold
table (42) is served by at least one silver table (`serves:`); `silver_crm_case` serves `crm_case` and `customer`, `silver_consent` and
`silver_frontier_consent` both serve `consent`. Columns: 206 profile attributes (71 without a seed counterpart stay null with a written reason in the spec),
16 official-name aliases (Account 4, Card 3, Deposit 3, Loan 6; 7 are `decimal(38,4)`, the only decimals in the layer), 326 extension columns (325 when silver was
first built; the gold migration added `silver_product.xEffectiveTo`, see the Gold implementation notes).

**Common columns** (all non-null except `sourceVersion`; a record that cannot supply one fails the build). `sourceRecordId` = the business key (composite keys
joined with `#`; array children use a `record_id` template such as `{^.loanId}#{$index:02d}`), `id` = `<Entity>:<sourceRecordId>`, `sourceVersion` = the seed
record's version text else null (account, card, deposit, protection_policy `version`; consent `revision`; product `productVersion`; sop `sourceVersion`;
document: the profile's shared `sourceVersion`, emitted once in the common position). `sourceKind`, `state`, `label` and `historical` are **the source app's
projection rules**: each table's `common:` block copies the arguments that `app/data_xray/projection.py` passes to `put()` for that entity (same vocabulary,
same fallbacks) and cites the lines. The conformance tests re-read the cited lines from the pinned repo, compare every block with an independent rule table, and
recompute each row's values from the raw columns silver exposes. The app's closed `SourceKind` literal (`app/data_xray/contracts.py` 32-37) and `_INACTIVE`
(`projection.py` 66) are copied as `silver.SOURCE_KINDS` / `silver.INACTIVE_STATES` and tested against the pinned files (the committed copies are the offline
fallback); the spec validator rejects `seed`, `derived:recorded` and any kind outside the vocabulary, and a `state` rule always has a constant or a fallback, so
it is never empty. Where a record came from stays in `_source_file` / `_release_id`.

| entity group (silver tables) | `sourceKind` | `state` | `label` | `historical` | app lines (`projection.py` unless noted) |
|---|---|---|---|---|---|
| Customer | `bank_record` | `customer` | `displayName` (Frontier persona: `name`), else ownerId | false | 431-435; frontier/data.py 93-100 |
| Product | `catalogue` | `status`, else `catalogue` | `displayName`, else productId | false (the app passes no flag) | 466-470 |
| Account, Card, Deposit, Loan, ProtectionPolicy, Transaction, Goal | `bank_record`; `history` when historical | `status`, else `recorded` | the key; Goal: `title` (Frontier: `name`), else goalId | `state` in `_INACTIVE`: closed, matured, renewed, superseded, cancelled, expired, withdrawn | 516-523, 66 |
| Preference | `customer_stated` | `status`, else `recorded` | `field`, else preferenceId | false | 550-554 |
| Document | `document_metadata` | `available` | `title`, else the literal `Source document` | false | 681-695 |
| Consent (retail4) | `consent` | `revoked` if `revokedAt` is set, else `recorded` | the key | false | 1701-1705 |
| ServiceCase | `bank_record` | `status`, else `pending` | the key | false | 1683-1687 |
| HouseholdAccessGrant, FrontierConsent | `frontier` | `recorded` (their contracts declare no `status`) | the key | false | 1985-1994; frontier/contracts.py 72-107 |
| RegisteredAgent, FrontierMandate, MandateAction, ProactiveCandidate | `frontier` | `status`, else `recorded` (the seed has a `status` on the agent and the mandate only) | the key | false | 1985-1994; frontier/contracts.py 109-260 |
| 17 extension tables of bank data | `bank_record` | the seed's own `status` / `stage` (loan_installment, scheduled_obligation, incident, crm_case), else `recorded` | the first display field, else the key | false | 322-338 |
| 7 extension tables of catalogue and reference data (product_parameter, sop, business_unit, region, relationship_manager, campaign, product_business_unit) | `catalogue` | `status` (campaign), else `recorded` | the display field, else the key | false | 322-338, 466-470 |

Consequences worth knowing. No seed status is in `_INACTIVE`, so no row is historical today (the rule is covered by unit tests: `closed` gives `historical` and `history`).
The 12 Frontier documents have no `title`, so their label is the app's literal `Source document`. The seed's own lifecycle values are never lost to the common
`state`: `xStatus` keeps `granted` / `revoked` on the Frontier consents and `active` / `inactive_minor_no_access` on the access grants, `xEventType` keeps the mandate event
types, and the profile `status`, `stage`, `revokedAt`, `title`, `field` columns carry the rest, so **gold never decodes `state` or `label`** (`test_silver_superset.py`
proves it for the 25 lifecycle fields v1 gold reads and for every rule path some record holds). The Frontier seed files are not what the app projects (it reads runtime
state), so for those entities the app's `put()` arguments are applied to the seed record.

**Survivorship and namespacing facts.** Silver keeps both releases side by side and never drops a row (key unique on `_release_id` + business key; the build fails
on a repeat). Only two rows get a legacy suffix, in the opaque `id` only: `Customer:rhea~legacy` and `Product:CB-SAV-SALARY~legacy` (a non-canonical release row whose
record id also exists in another release); business-key columns and `_owner_id` stay raw, so gold keeps applying `t: owner` from `_release_id`. The five products of
`data/relationship/products.json` have no release (`_release_id` null). Gold's survivorship is reproduced exactly by counts: customer 44 silver rows (4 persona + 40 CRM
case) -> 38 (6 merged), product 37 -> 36 (1 merged); every other served table is row for row (`test_silver_row_counts_reproduce_gold_except_the_survivorship_list`).

**Raw shapes resolved in silver.** Money object -> `*Minor` int64 paise + `currency` (the build checks every money object of a row against the `currency` column);
rupee catalogue amounts -> exact paise; rates and fractions -> canonical decimal text; dates -> `date32`; instants -> UTC `timestamp[us]`; no doubles anywhere.
Arrays that v1 gold already splits are child tables with parent key and ordinal (LoanInstallment, InvestmentGoal, TransactionEvidence, ConsentResource,
GrantPermission, AgentScope, MandateAction, ProductBusinessUnit); the three seed-reading flatteners (preference unpivot, product parameters, monthly metrics) live in
`silver.SILVER_CUSTOM` and a test pins them to `gold.CUSTOM_SOURCES` row for row (gold keeps its copies until the gold migration).

**Superset check.** `test_silver_superset.py` proves every seed field a v1 gold column reads (`from`, `tpl`, `t`, plus the `owner` scope and `$business_date`) is exposed by
the serving silver tables; the exception list is empty. It is data aware: 26 fallback paths that v1 gold tries on every source of a table (for example
`customerName` on persona files) exist in no record, so there is nothing to lose. A negative control drops a column and expects the gap. Two further tests cover the
lifecycle fields: every `status` / `stage` / `eventType` / `revokedAt` / `state` field that gold reads (25 gold-table and seed-field pairs) is a real column, and so is every seed field a
`state` or `label` rule reads when some record holds it (30 rule paths), because the common columns are derived, not a source for gold.

**Deviations from this contract.**
1. Arrays become child tables only where v1 gold already splits them; the other arrays stay `; `-joined text, which is what the profile's `plain-lines` transform specifies.
2. `sourceKind`, `state`, `label` and `historical` follow the app's projection rules (table above), not this contract's first wording (`seed`, `derived:recorded`, label =
   the first display field). Where the two differ the app wins: a Transaction's label is its id, not its description; a Document without a title is `Source document`, not its
   key; a Customer or Product is never historical. Two differences to the app are deliberate: the app redacts private or health text in a label (`[redacted]`) and raises on a
   null lifecycle, while silver is source-faithful and treats a null or empty status as absent (the rule's fallback applies).
3. `Commitment` (mandate or repayment-schedule row, datasets liabilities / repaymentSchedule / mandates) is not the seed's scheduled obligation, so
   `ScheduledObligation` is an extension entity although `ONTOLOGY_ENTITY` maps it; `CRMContinuity` is a derived view, so `CrmCase` is an extension entity too.
   CRM-only customer parties are not profile Customer records: `silver_customer` holds personas, `silver_crm_case` carries `customerId` and `customerName`.
4. A profile attribute is mapped only where the seed field has the same meaning, otherwise it is `none: <reason>` (for example `Loan.principalMinor`: the seed's
   `openingPrincipal` is the principal at the start of the history, so it lives in `xOpeningPrincipalMinor` and the alias `principalAmount` stays null).
5. Catalog encodings the contract leaves open: BIAN domain, control record and status are `"; "`-joined lists aligned in registry order (`unmapped` when the registry
   has no row; extension columns are `unmapped`); `privacy` is the kit's `restricted`/`pii` tag plus the profile's text.
6. `write_gold`'s manifest hashes every `model/**/*.yaml`, so it now also lists `model/silver/*.yaml` and `model/cdm_crosswalk.yaml`; nothing reads that list.
7. `_release_id` is nullable (the release-less products file); keys are unique including that null release.

**Next phase (gold from silver).** Gold must rebuild what silver deliberately does not carry: context constants set by gold sources (`segment`, `source_kind`,
`data_role`, `$kind`, `$is_denied`) and the `$business_date` fallback (silver exposes it as `xReleaseBusinessDate`); legacy owner namespacing from `_release_id` /
`_owner_id`; precedence for the customer (persona rows before `silver_crm_case`) and product duplicates; `silver_consent` / `silver_frontier_consent` as two sources of
`consent`. `state` is not an input to gold at all: every raw status is a column (`status`, `xStatus`, `stage`, `revokedAt`, `xEventType`), so nothing has to be decoded or
nulled. Silver is not tier-aware yet: `build` writes all 43 tables whatever `--tier` says.

## Pipeline implementation notes

Offline only (mocks, no live run). `build`, `up`, `load`, `verify`, `diagnose` and `plan` now treat the three layers as one plan, by tier. The sentence above that silver is
not tier-aware yet is superseded by this section.

**Code.**
- `contoso_onto/layers.py` (pure data): `LoadPlan` lists the bronze files, the silver tables and the gold tables in load order (bronze, silver, gold). `load_plan(out)` reads
  `out/bronze/manifest.json`, `out/silver/manifest.json` and `out/manifest.json`; the runner, the CLI, `diagnose`, `verify` and the mocks all use it. Table names must be unique across layers.
- `pipeline.write_layers` is the one writer behind `build` and `up`. `select_layers` takes the tier's gold tables (`spec.make_spec`), the silver tables their `silver:` lineage names plus the two
  catalogs, and the bronze files those silver tables read (`silver.bronze_paths`); `slice_silver` cuts the catalogs and `bronze_file_manifest` to what is written; `stage_bronze` copies the seed
  JSON verbatim to `out/bronze/files/<release|no-release>/<repo path>` (hash-checked). `--tier both`, the default, is `full`.
- `fabric_ops`: `upload_bronze` (to `Files/bronze/<release>/<repo path>`; local size and sha256 check, then a read-back check), `load_layer` (`load_gold` is a wrapper), `load_layer_delta`
  (`load_gold_delta` is a wrapper; decimal128 stays intact), `load_layers` (order, one SQL-endpoint refresh per layer and the last one followed, registered check, delta-rs for the tables the API
  refused inside `up`), `layer_report`. `runner.load_stage` decides what to load; `cli load --layers bronze,silver,gold --only <tables>` works across layers.

| Tier | Bronze files | Silver tables (with the 2 catalogs) | Gold tables | Tables to register (adds `bronze_file_manifest`) |
|---|---|---|---|---|
| smoke | 18 | 15 | 13 | 29 |
| core | 37 | 30 | 32 | 63 |
| full | 44 | 45 | 51 | 97 |

**What reloads when.** `LoadPlan.fingerprints()` gives one hash per layer: the layer's own manifest (table names, row counts, sha256, schema; for bronze also every file's target, sha256, size)
chained with the hash of the layer before it. A bronze change therefore reloads all three layers, a silver change reloads silver and gold (gold is built from silver), a gold change reloads gold
only. A new tier is a different table set, so it reloads the layers whose set changed; `--reload` loads everything; a planned table missing from the lakehouse reloads its own layer. The state
file keeps `loadedLayers` (layer -> fingerprint; a layer is unrecorded when its load starts and recorded when it finishes) and `loadedFingerprint` (the whole plan; empty while a load runs), so a
rerun after a failure resumes and leaves the finished layers alone.

**Order and failure.** Bronze files, then silver, then gold; each layer loads in parallel inside itself, and every table of a layer must be registered before the next layer starts. A silver (or
bronze) failure stops before gold: the runner names the layer and its first failing table and says which layers were not started. What a failure says decides what happens next (review round 2):
- A table that fails while others run is tried again on its own, one at a time. A failure about the moment (429, 5xx, a dropped connection or timeout, a conflict) is waited out with an exponential back-off with
  jitter that never undercuts `Retry-After` (up to 5 tries; OneLake requests 4); any other failure gets one more try. Every table (and every bronze file) has **one retry budget of 6 minutes** in all, from its
  first try, whatever the number of tries: when the next wait would end past it the failure is final (`Fabric.retry_until` makes the back-off inside each call honour the same deadline; the clock is
  injectable). A failure that is not the work being refused ends the run (a circuit breaker), in the parallel round too: when as many loads in a row as there are workers fail for a reason that is not
  about the tables, the queued ones are cancelled, the rest of the layer is reported as not tried and the run stops (a conflict, `409`, neither trips it nor resets it).
- `Fabric.call` retries `ConnectionError`, `Timeout` (read, connect) and `ChunkedEncodingError` like a 503, for every method (a replayed POST that had been executed answers a loud 409 and the ledger
  heals it); the last error is raised as it was, text and all.
- delta-rs writes **only the tables whose Load Table call itself was refused**: HTTP 400, 415 or 422 on the load request, or an operation that ended Failed with an `errorCode` that is not about the moment.
  An operation that ended Cancelled, with no code, or with a code containing `InternalError`, `Unavailable`, `Timeout`, `Cancel`, `Capacity`, `Throttl`, `TooManyRequests` or `ServerBusy` is the moment, not
  the file. The other tables stay as the API loaded them. A failure of the staging upload to OneLake (the Load Table call was never made) never falls back: the error names the file. Throttling, a busy
  or unreachable service, a table list that cannot be read and a refused sign-in never switch loader: `LayerLoadError.transient` says so and a re-run resumes.
  `build.sh` installs `deltalake` (the one pinned line of `requirements-optional.txt`, pip with `--retries 1 --timeout 10`: an unreachable PyPI costs seconds) into its own environment on every build;
  `--loader delta` writes every table with delta-rs; the `load` command never falls back by itself.
- A table listing that fails for a moment is retried until **one deadline of 3 minutes** (the waits inside each call count against it; at most 40 tries) before anything is called missing. Ctrl+C cancels the
  queued tasks of a layer; the pool threads are not daemons, so the process lives until the loads already running finish (the longest wait of an operation is 15 minutes), and it says so:
  `interrupted: waiting for N running loads to finish (up to 15 min); queued loads were cancelled`.
- After the last layer the SQL endpoint's refresh (a long-running operation) is followed for up to 10 minutes, best effort: it never fails the stage. For a layer written with delta-rs the refresh is
  requested after its tables are listed (a delta-rs table shows up late), like the other layers. The **ontology** reads the lakehouse tables through
  that endpoint (the semantic model reads OneLake directly); the end of the last load is kept as `lastLoadAt` in the state file, and an ontology 400 or "not found" within 10 minutes of it gets a note
  that the endpoint may still be syncing and that a re-run resumes without loading again.

**Writers prune.** `write_gold`, the silver `_write_tables` and `stage_bronze` delete what their manifest does not list, so a smaller tier built into the same folder leaves only that tier.
The load is manifest-driven as well (`only=` names), so a stale file could not be loaded in any case.

**Tests.** `tests/test_layer_load.py` covers the selection logic, pruning, plan and fingerprints, the Parquet schemas of all three layers (int64 paise, string rates, `decimal(38,4)` only on
official-name alias columns, no decimal in gold, column and table name rules), load order, bronze paths, resume, the delta fallback across layers, tier switching and a wipe that verifies clean
after a layered build. `mock_fabric` serves OneLake file reads and listings. Table names come from the spec and the manifests, never from the tests.

**Not verified live** (docs/LIVE_CHECKLIST.md, section 1a): the Load Table API with `decimal(38,4)` columns (and the delta-rs path after a refusal); silver column names that start with `_` or are camelCase on the SQL analytics
endpoint; the OneLake bronze path and size; about 100 tables registered and the endpoint refresh (URL, `preview` parameter and result shape of the preview API); load time at 4 workers; the silver-first bisect; gold-only bindings; graph refresh; the golden questions.

**Known limits (found in review, not changed).** (1) Gold's `where`, `require`, `absent` and `post` hooks drop rows with no build-time accounting: nothing counts the silver rows a gold table did not
take, so a dropped row is invisible unless a test sees it; today `tests/test_gold_equivalence.py` and `tests/test_lineage.py` are the guard. (2) The gold `customer` table names two `_release_id` values
(`retail-frontier-v1` and `retail4-v1`, `model/gold/10_party.yaml`) in its `where` filters, so a third release would vanish from it silently; the same two tests would flag it only for the pinned snapshot.
(3) A description-length 400 and an endpoint-lag 400 on the ontology create look alike; the description fallback (`TierDefinitions`) acts on any 400, so within 10 minutes of a load it can switch to
`compact` for a reason that is not the descriptions. `runner.sql_sync_hint` tells whether the lag window is open; a guard in `TierDefinitions._converge` that consults it is not in place.

**Changes in `silver.py` (additive).** `bronze_paths(spec)` returns the files a spec's tables read, and `_write_tables` prunes `*.parquet` files that are not in the written set.

## Gold implementation notes

Code: `contoso_onto/gold.py` (engine), `contoso_onto/pipeline.py` (`build_gold`, `validate_star`, `write_gold`), `contoso_onto/oac.py` (`physical_of`), `contoso_onto/spec.py`
(checks S071, S072), `contoso_onto/agents.py` (data dictionary), curated specs `model/gold/10_party ... 70_bank_ops.yaml`, the one-off `tools/migrate_gold_to_silver.py`
(rewrites the specs mechanically and writes `docs/GOLD_LINEAGE.csv`: 639 rows, gold column -> silver table and column -> transform). Tests: `test_gold_equivalence.py`,
`test_gold_star.py`, `test_lineage.py`, `test_physical_names.py`, `test_migrate_gold_to_silver.py`, plus the engine tests in `test_gold_engine.py`. Commit `5a9aa7f`.

**How silver feeds gold.** 42 of the 51 tables read silver: every `sources` entry is `{silver: <table>}`. A source may carry `where:` (row filter; `customer` splits `silver_customer`
by `_release_id`), `set:` (context constants a column still reads: `segment`, `source_kind`), `require:` / `absent:` (as in v1) and `join: {silver: <parent>, on: {child column: parent
column}}` (reads the parent as `^.column`, always within the same `_release_id`). The engine and its unit tests support `join:` and `none: "<reason>"` (a column no silver column
carries stays null), but the committed model needs neither: silver already carries the parent keys, and every gold column found a silver column. `check_silver_paths` rejects a column that
names a silver column which does not exist, so a typo is a build error, not a silent null. Money and rates follow the silver representation: `money:` -> `minor_to_inr` (minor / 100.0)
plus the hidden `minor_to_paise`, `rate_*` -> `dec_to_float` / `dec_to_bps`; the 9 reference tables keep the v1 transforms. Owner namespacing (`owner`, `superseded_by`: 24 columns in 23
tables) reads `_owner_id` and `_release_id` from silver, but its result depends on more than those columns: which ids are namespaced is decided by `silver_customer.ownerId` across the two
releases (an `ownerId` that is a persona in more than one release is a collision: model.yaml `identity: {table: silver_customer, key: ownerId}`), and how by `release_short` and `canonical_release`
in model.yaml, so the non-canonical row becomes `rhea~legacy`. `docs/GOLD_LINEAGE.csv` does not list that dependency (it shows `_owner_id` and the transform `owner`, never the persona table
or the three model.yaml keys behind them), so a change to any of them changes gold ids with no lineage row pointing at it. Duplicate keys are merged by source order (`allow_duplicates`), and
`expected_merged` makes any other count fail the build.
The 9 reference tables (`reference: true`) keep their curated docs/config sources: law, authority_rule, frontier_requirement, bank_agent, service_request_type, journey,
journey_sop, journey_product and the generated calendar. Their `kind` stays dimension, bridge or reference: `reference: true` says where the rows come from, not what they are.

**Physical names (one registry).** The `physical` field of each gold table spec is the only place a lakehouse name is written: `physical_names(model)`, `physical_name(logical)` and
`logical_name(physical)` in `gold.py`. The logical id (`account`) stays the key for relationships, measures, rules, tiers (`SMOKE_TABLES`), oracles and the v1 fixtures. The output
boundaries that use the physical name, and only these: the Parquet file `out/gold/<physical>.parquet` and the keys of `manifest.json` and `dq_report.json` (each entry also carries `table`,
`kind`, `grain`, `silver` or `reference`, and `schema`; `write_gold` deletes a stale Parquet file a v1 build left behind); the semantic-model partition `entityName` and the ontology
table partition `entityName` (`oac.physical_of`; the ontology entity binds its own table, named by entity, which binds the lakehouse table); the graph `dataSources.json` DeltaTable
path (validator G032 checks that it ends in the entity's physical table); the data dictionary (`table` = physical name, `logicalId`, `kind`, `grain`); and the load stage, which names
tables by Parquet file stem. `validate_star` (at build) and the spec checks S071 / S072 enforce the shape, the prefix that goes with the `kind`, uniqueness, and that a physical name is
never another table's logical id. `test_physical_names.py` proves that no logical id is a lakehouse object name in any generated artifact.

**Equivalence.** `tests/test_gold_equivalence.py` compares gold built from silver with the frozen v1 output (`tests/fixtures/gold_v1`, commit `c421458`) by logical id: 51 tables, 2,501
rows, the same columns in the same order with the same Arrow types, the same value for every key (floats compared bit for bit, not just within 1e-9: rupees are minor / 100.0 and
percentages are `float(decimal text)`, the same correctly rounded doubles v1 made), the same 29 golden answers, and the ontology and semantic-model counts. `ALLOWED_DIFFS.yaml` is empty,
and a test fails an allowed entry that covers no difference.

**Survivorship.** Silver keeps every row per release; gold merges two groups by precedence, exactly as v1 did: 6 customer rows (silver 44 = 4 personas + 40 CRM-case parties -> gold 38: the
CRM-case parties whose key is a persona's or an earlier case's) and 1 product row (37 -> 36: `CB-SAV-SALARY` is a catalogue product and a Frontier stub, and the catalogue row wins). Both are
declared as `expected_merged` and `test_lineage.py` proves that silver rows minus gold rows is exactly that list.

**Star metadata.** Every table has `kind` (26 dimension, 13 fact, 8 bridge, 4 reference), a one-sentence `grain`, `physical`, and `silver: [...]` or `reference: true`; `grain_key` names the
columns of a composite grain. `test_gold_star.py`: a fact's and a bridge's grain key is unique, a dimension's business key is unique and never null, the foreign keys resolve to dimensions
(four documented fact-to-fact links, two facts with no dimension key), gold has no decimal column, and the date columns are typed dates.

**Deviations from the contract.** (1) `join:` and `none:` exist in the engine and are unused (above). (2) Two spec fields the contract did not name: `grain_key` and `expected_merged`
(`allow_duplicates` is v1's). (3) Grain sentences are more specific than the section 6 table (the account grain says its balances are as at the release business date, as the contract asked for
type-1 attributes). (4) The migration added one silver column, `silver_product.xEffectiveTo`, because a gold column needed it (see the Silver implementation notes). (5) `build_gold`
builds silver itself when it is not handed one, and `cli build` hands it the build it already made, so a build reads the seed into silver once. (6) The first physical names followed the
section 6 table to the letter (including `dim_insurance_policy` and `fact_income`); the naming phase later aligned seven of them with the entity names (see the Ontology and naming
implementation notes).

**Known limits.**
- `join:` is unused in the committed specs; as the engine implements it, the first matching parent wins, a missing parent keeps the row with NULLs, and a NULL child key matches a NULL parent key.
- A timestamp with more than microsecond fractions is truncated to microseconds (Python `datetime`, Arrow `timestamp[us]`), not rounded.
- The v1 equivalence fixtures record column names and types, not nullability: `test_gold_equivalence.py` compares values and types, so a column that became nullable would not fail it.

## Ontology and naming implementation notes

Code: `contoso_onto/registry.py` (the one join of the specs), `contoso_onto/spec.py` (`describe()`, the description budgets, checks S073 / S074), `contoso_onto/standards.py` with `model/standards.yaml`,
`contoso_onto/agents.py` (data dictionary, instructions), `tools/naming_registry.py` (renders the generated tables of `docs/NAMING_STANDARD.md`; `--write` / `--check`) and `model/standards_exceptions.yaml`.
The standard itself is `docs/NAMING_STANDARD.md`. New tests: `test_naming_standard.py` (13), `test_entity_descriptions.py` (21), `test_standards_conformance.py` (10); changed: `test_gold_star.py` (date roles),
`test_gold_equivalence.py`, `test_graph.py`, `test_physical_names.py`, `test_spec.py`, `test_standards.py`, `test_silver_conformance.py`. The frozen v1 fixtures and the `graph_core` portal capture are untouched.

**What changed.** One vocabulary, as the user chose (the approved CDM application profile is the base; official Microsoft CDM names are references): an ontology entity that realises a profile entity carries
the profile's name (the one the Banking Data X-Ray shows); official CDM names appear only as `cdm_reference_entity` metadata, as "semantic reference only" in descriptions and as alias columns in silver; BIAN
service-domain and control-record names are metadata only (catalog columns and one description sentence), never a table or entity name. Six entities and seven lakehouse names were renamed. Logical ids
(`table:` in `model/gold/*.yaml`) are unchanged, so `relationships.yaml`, the measures' `table:`, the oracles, `SMOKE_TABLES` and the v1 fixtures needed no edit; property, relationship and measure names, rule ids,
the golden questions and their expected answers are unchanged (a measure's DAX and a rule's property reference name the new entity).

| logical id | entity | lakehouse table |
|---|---|---|
| insurance_policy | InsurancePolicy -> ProtectionPolicy | dim_insurance_policy -> dim_protection_policy |
| service_record | ServiceRecord -> ServiceCase | fact_service_record -> fact_service_case |
| access_grant | AccessGrant -> HouseholdAccessGrant | dim_access_grant -> dim_household_access_grant |
| mandate | Mandate -> FrontierMandate | dim_mandate -> dim_frontier_mandate |
| mandate_event | MandateEvent -> MandateAction | fact_mandate_event -> fact_mandate_action |
| proactive_event | ProactiveEvent -> ProactiveCandidate | fact_proactive_event -> fact_proactive_candidate |
| income_record | IncomeRecord (same) | fact_income -> fact_income_record |

**The registry.** `registry.entity_rows(model)` joins, per gold table, the gold spec (`entity`, `physical`, `kind`, `grain`, `silver:`, `renamed_from`), the silver spec (`profile_entity`, `ontology_entity`, key, seed
files), `model/standards.yaml` (BIAN domain, control record and status; official CDM reference) and `model/cdm_crosswalk.yaml` (alias columns); no other list holds a name. 51 rows: 26 dimension, 13 fact, 8 bridge,
4 reference; **18 profile-aligned** entities (19 profile entities: Consent realises the profile's Consent and FrontierConsent rows) and **33 Contoso extension** entities (24 with a silver table, 9 curated
reference tables, one of them the calendar, which is a semantic-model table without an ontology entity). The rule: `silver_<snake(entity)>`; `<role>_<snake(entity)>` with the role from `kind` (dim, fact, bridge, ref);
one exception, `dim_date`. `test_naming_standard.py` proves the rule, two deny-lists (the 11 official CDM entity names the vendored files define; the 17 BIAN service domains and their control records from
`bian-registry.json`), the ontology name limits, and that the generated tables in the standard equal the tool's output. To change a name: edit the spec field (`entity` / `physical` in `model/gold`,
`ontology_entity` / `profile_entity` in `model/silver`), run `python tools/naming_registry.py --write` and `python -m contoso_onto standards --repo <repo> --write`, then the tests the standard lists.

**Standards honesty.** `Commitment` left `ONTOLOGY_ENTITY` (19 entries, identity-mapped, FrontierConsent -> Consent): the profile's Commitment describes mandate and repayment-schedule rows, not the seed's scheduled
obligations, so the remark in the Silver implementation notes that `ONTOLOGY_ENTITY` maps ScheduledObligation is history. All 33 extension entities say "Contoso extension entity: no CDM profile entity, BIAN
status unmapped." and claim no alignment. A realised entity lists its BIAN domains with the registry's own status words (custom, provisional, unmapped; none upgraded) and "Demo mapping, not certification.";
`standards.yaml` also carries the profile's 65 relationships for the conformance test.

**Descriptions.** `describe()` = the business meaning first (Copilot reads the first 200 characters), then the standards or extension sentence, then a clause built from the registry: star role, grain and lineage
(`dim_account <- silver_account <- accounts.json`; a reference table says "Curated reference data") and the official-name aliases "(semantic reference only)". The validators set no limit for entity or table text,
so the kit has its own budgets in `spec.py`: ontology item 256 (the code clips at 256; the old text was 268 and cut mid-word, now 249), entity and semantic table 800, column and measure 200; S073 / S074 fail a
build above them. The longest entity text proven live is 497 characters; the longest now is 725 (Account). A renamed entity keeps its old business words in `synonyms` and in the description, the spec keeps
`renamed_from`, the agent instructions carry a "formerly called" hint, and no generated text names an entity by its old name (tested). The data dictionary gains `kind`, `grain`, `silver`, `bronzeFiles`,
`profileEntity`, `extension`, `family`, `cdmReference`, `bianDomains` and `renamedFrom` per entity, shows them in its markdown, and carries a generated example lakehouse query on physical names (tested).

**Date roles (design decision 3).** Two semantic-only relationships (`ontology: false`, to `calendar.date`; S060 / S061 pass; no measure uses the calendar, so no result moves): "Service Case Opened On Date"
(`service_record.opened_date_ist`) and "Proactive Candidate Evaluated On Date" (`proactive_event.as_of_date`; the entity is extended-tier). The 7 facts with a date column now each have one (the choice per fact is in
the relationship `desc`; a transaction hangs on `posted_date_ist`, not `value_date`); the other 6 facts have no date column (protection_gap, scheduled_obligation, income_record, monthly_metric, risk_alert, fraud_signal).

**Exceptions** (`model/standards_exceptions.yaml`; an entry that is stale, unknown or without a reason fails). 17 of the profile's 65 relationships join two realised entities; 2 have no ontology relationship:
Loan.linkedAccountId -> Account.accountId and MandateAction.candidateId -> ProactiveCandidate.candidateId (both silver columns are `none:`: the seed carries no value). 44 ontology keys were compared with the
silver business key; 2 differ in form: Customer for a CRM-only party (built from `silver_crm_case.customerId`) and LoanInstallment (loan id + ordinal, equal to the seed's `installmentNumber` in every row).

**Deviations.** Money properties keep `...Inr` / `...Pct` (rupees as doubles for agents; the exact int64 paise twin is hidden) instead of the profile's `...Minor` names (documented, not a mapping); `dim_date` is
the one lakehouse name outside the rule; Consent merges two profile entities; extension entities keep business nouns; relationship names stay natural-language triples; table ids stay the v1 logical ids.

**Final counts** (offline build, `--tier core|full`, spec errors 0, ontology and semantic validators errors 0; two builds byte-identical):

| tier | entities | properties | ontology relationships | semantic relationships | measures | rules |
|---|---|---|---|---|---|---|
| core | 31 | 417 | 53 | 36 (v1: 35) | 39 | 23 |
| full | 50 | 534 | 78 | 56 (v1: 54) | 42 | 23 |

The whole suite passes (1,007 tests, 1 skipped for POSIX file modes) and `ALLOWED_DIFFS.yaml` is still empty. **Not verified live:** the six renamed entities through the portal and the data agent after a re-publish;
entity descriptions up to 725 characters in the portal; the two new date roles in the Direct Lake semantic model; the "formerly called" instruction line in agent answers.

## Description modes and the compact fallback (A10)

Why. Microsoft's how-to for ontology metadata asks for descriptions that are "concise but informative (one to three sentences)" and documents no limit. The longest entity line ever accepted live
is 509 characters (v1's Product: its 497 plus the ontology's `[Catalogue] ` tag); the full descriptions of the naming phase reach 736 on core (Account) and 725 in the semantic model. A refusal for
length must not cost a live run, so there is a compact form and an automatic, remembered, one-time fallback to it.

**Modes** (`spec.DESCRIPTION_MODES`, `make_spec(model, tier, descriptions)`; `full` is the default and unchanged). `full` = business meaning, standards sentence, architecture clause. `compact` = v1's text:
business meaning and standards sentence, no clause. The mode reaches the ontology entity lines, the semantic-model table lines, the data dictionary's `description`, the grounding brief and the agent
instructions (their sentence about the clause follows the mode); the dictionary keeps `kind`, `grain`, `silver` and the rest as data in both modes. The entity budget belongs to the mode and is measured on the
line that is written (`oac.ontology_doc`: the ontology puts `[Domain] ` in front): 800 in full (the kit's own), 500 in compact (S073). Only Product exceeded 500 in compact (509 written; its hand-written text
lost 13 characters, same meaning); the longest compact line is now 497 (Deposit).

**Choice** (`spec.resolve_descriptions`). `--descriptions full|compact` on build, plan, deploy, agents, verify, preflight and up (and `build.sh --descriptions`), else `CONTOSO_DESCRIPTIONS`, else `descriptionsMode`
in the state of the output folder, else `full`: an explicit choice beats the remembered one, so `--descriptions full` (or the variable) tries the long text again. The CLI resolves it once and passes it down; there is no
global default. `plan` prints the mode and the longest line of each mode, `preflight` says it in a PASS line, and `up` checks that the compact specification is valid too before it creates anything.

**Fallback** (`runner.TierDefinitions`, `definition_rejection`). In the full mode an HTTP 400 on the create, the updateDefinition or the result of the ontology or the semantic model (or their long-running operation
ending Failed with an invalid-definition error code) makes the run build BOTH definitions again in compact, try the item once more, keep `descriptionsMode: compact` and `descriptionsFallback` (item, the service's
error text, time, longest full and compact line) in the state, and print `DESCRIPTION FALLBACK` lines with that text. The semantic model follows the ontology to compact, so the two never disagree. The 403 gate
(`FeatureNotAvailable`), 429, 5xx, 404, 409 and a 400 on other items (lakehouse, graph model) are not rejections. A retry that is refused as well raises `DescriptionFallbackFailed` (an `oac.FabricError`; the original
error first, the retry's after it; the run ends partial, exit 4); a retry that reaches the 403 gate counts as validated. A re-run starts in the remembered mode and does not repeat the refused attempt.
`NEXT_STEPS.md` (section "Descriptions: COMPACT") and the closing report say which mode is in force and why; in the full mode the state keeps the longest line each item accepted (`descriptionsAccepted`): the number
that `docs/LIVE_CHECKLIST.md`, section 1a, item 10 asks for. A `synonym` / `annotation` metadata mode (`--metadata annotations`) is written down there as a next experiment, not built.

**Tests.** `test_descriptions_mode.py` (34): both modes on smoke, core and full against the generated parts and the 500 budget, the option and its resolution on seven commands, `plan`, a dry-run `deploy`, a compact build
against a full one. `test_runner.py` (16) against a mock hook that refuses a description line over N characters with a Fabric-shaped body: full accepted and its longest line reported; a refused ontology, semantic
model or failed operation falls back once; a compact text refused as well (original error plus retry), compact asked for and refused; the gate unaffected, also when the service judges before the gate; a re-run is
idempotent; `--descriptions full` asks again; `deploy`; preflight; the rejection table. One test in `test_scripts.py`. The whole suite passes (1,061 tests, 1 skipped for POSIX file modes).

**Not verified live**: the real wording of a refusal (the mock's body is invented, of the right shape), whether a long line is refused at all, and the long-running-operation form of a refusal (assumed from the
error-code family, never seen).
