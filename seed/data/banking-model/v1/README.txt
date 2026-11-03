BANKING DATA X-RAY: contosoBanking v1
===================================

This is a custom, thin application profile, not Microsoft's full banking model,
BIAN certification, regulatory compliance, an SDK-conformance claim or evidence
of a completed banking operation. The application loads it lazily: damaged
metadata makes optional inspection unavailable, not the banking application.

Versions
--------
Profile: contoso-banking-v1
Mappings: contoso-bian13-v1
BIAN release and YAML info.version: 13.0.0
Microsoft CDM: dd21d715e05ebf740a11356c80b5c3b4c38a89c2
BIAN public: b58bf4c2c300dd018afc084e9bb24b7adaaa3500

sources.json records each upstream repository, exact path, pinned raw URL,
original-byte SHA-256, license and role. Vendor files are unmodified originals.
The loader anchors that inventory's own digest in app.data_xray.model and
checks every source hash before it claims validated references. Runtime and
tests do not retrieve anything from the network.

Profile and source semantics
----------------------------
banking.cdm.json contains 33 concrete business entities and two export-only
bookkeeping entities. RecordIndex and RecordLink are not extra customers,
accounts, products or holdings. There is no second FinancialHolding row per
owned product. Schemas and actual customer nodes are distinct graph records.

profile.types.cdm.json imports the authentic, locally vendored CDM foundations,
primitives and their required meanings closure. contosoBanking.stringIdentifier
extends string, NOT entityId/GUID. contosoBanking.minorAmount extends bigInteger:
signed 64-bit [-9223372036854775808,9223372036854775807]. The monetary contract
is INR, exponent 2 (paise), with canonical integer-string transport. Catalogue
parameters and compatibility fields with unit=inr are rupees; conversions use
exact decimal multiplication by 100 and reject fractional paise/overflow rather
than rounding. Credit limits, account balances, escrow, holds, coverage,
commitments, requested amounts, approved amounts and executed amounts differ.

Plain dates have no timezone or implied posting. Timestamps require an explicit
offset or Z. Rates/confidences are finite exact decimal strings, not floats.
Missing, unknown, absent, redacted and not-applicable values stay nullable and
are explained by source/record diagnostics; they are never manufactured zeroes.

The public model exposes this declared monetary contract as money:
  {"currency":"INR","exponent":2,"minimum":"-9223372036854775808",
   "maximum":"9223372036854775807","cdmBaseType":"bigInteger",
   "transport":"canonical signed integer string"}
Use that exponent for matching INR minor amounts, not a locale-derived guess.
An unsupported/unknown currency must keep its raw minor text and a diagnostic.
Each minor attribute also declares its unit; fieldMappings exposes the distinct
catalogue rupee source unit and exact conversion. Never coerce the amount to a
JavaScript Number in order to format it.

profile.json describes the business entities, typed attributes, defined
relationships, units and limitations. field-mappings.json names actual source
datasets, record/value JSON pointer templates, source identifiers/versions,
transforms, privacy/provenance and null/type limitations for every field.
These maps are descriptive, not an executable query or expression language.
Named typed projection adapters authorize the snapshot before lookup or joins.

Product.requiresDecision is derived from the approved product family by the
pure shared app.agentic.bank.product_requires_decision policy. It is not the
catalogue underwritingRequired flag: a new credit facility can require an
independent decision even when that catalogue flag is false. The flag describes
new-facility policy, not approval or a requirement for every servicing action.
Unsupported families are explicit errors rather than an assumed false value.

Compatibility application receipts are LifecycleEvent records, NOT
ExecutionReceipt. Stored applications/serviceRequests supply their actual
receiptId, submittedAt and public recordedInstruction. Scoped owner.receipts
entries supply their real dictionary receipt key, recordedAt and retained
result.application/result/review instruction, with the documented fallback
order. Supported statuses remain explicit legacy_instruction_<status> values:
application_prepared, application_created, service_request_created, submitted,
recorded or review_saved. Preparation/submission/saved-review history cannot
approve or execute an application's current terms. Exact mirrors deduplicate
by real receipt identity; conflicting copies fail. Missing times or references
remain unknown, and no new banking record is created by inspection.

Official references are semantic counterparts ONLY; they are not inherited:
Contact, Relationship, FHAccount, FHSaving, FHLoan, FICard and CustomDocument.
Additional preserved FinancialHolding/CustomerFinancialHolding references
explain the standard holding context without duplicating owned products.
LoanApplication/ApplicationDocument are preserved research evidence; a generic
application or a scalar review is NOT asserted to be an ApplicationDocument.
Retail banking Account means a business/company, not the deposit account.
LifeEvent is not a goal; CertificateOfDeposit is a process, not our fixed
deposit holding. No universal protection/payment meaning is borrowed from P&C.
Reference-only business files' large dependency graphs are deliberately not
imported or claimed to be independently resolved by this profile.

BIAN
----
bian-registry.json verifies references separately from application mappings.
referenceStatus=verified-reference means the original YAML contains the named
domain/control-record schema and version; mappingStatus=custom/provisional does
not mean BIAN approved the mapping. Numeric official domain IDs remain null.
bian13:* keys and business-area groupings are INTERNAL application identifiers.

Mappings are many-to-many with explicit subtype conditions. Debit/prepaid
cards do not map to Credit Card. Home/unknown loans do not map to Consumer
Loan. Payment proposals are not Payment Execution. Purchase escrow does not
prove conveyance; a demo settlement does not prove external rail execution.
Facts, consents, generic receipts, worksheet results and generic saved reviews
honestly retain unmapped entries. Missing subtype information is not a match.

Validation boundary and reproduction (repository root, Windows)
---------------------------------------------------------------
  python data\banking-model\v1\build_profile.py
  python -m app.data_xray.model
  python -m pytest -q tests\test_data_xray_model.py

Generation is deterministic and offline from this explicit source inventory
and the existing pinned vendor files. Do not edit generated JSON in isolation;
edit build_profile.py, regenerate, and run the validator/tests. An intentional
vendor refresh additionally requires reviewing sources.json and updating the
trusted inventory digest in model.py. Explicit network acquisition is a
separate maintainer command:
  python data\banking-model\v1\fetch_upstream.py --fetch
It only downloads public upstream artifacts at the fixed commits; it never
contacts banking/model/OCR services or changes a session.

Both generators write generated JSON as explicit UTF-8 bytes with LF newlines,
including the protected sources.json inventory. Original vendor downloads are
always written byte-for-byte, without newline normalization. Git attributes
preserve the entire banking-model tree's bytes. An intentional inventory-byte
change requires its protected hash to change coherently; a running Python
process must reload the validator before using such a new inventory.

The checker uses the authentic upstream Draft-06 definition/manifest schemas
(version 1.5.0), explicit format checks and a no-network reference registry.
It rejects duplicate JSON keys, unsupported emitted-profile constructs,
unresolved local types/purposes/traits, bad manifest paths and relationships,
unknown/missing source mappings, and altered upstream hashes. It follows real
foundations/primitives import cycles with a visited set.

Only this bounded flat profile is checked. There is no claim of a full CDM
SDK load, resolve, import of all official banking dependencies, Dataverse
integration, inheritance/projection/attribute-group support or round trip.
The shipped manifest is an export layout, not a false assertion that live
customer partitions exist on disk. Actual export CSV bytes are validated
separately for headers/order, strict scalar types, signed64 money, null
escaping, partition paths, unique keys and index/link integrity.

Export contract
---------------
Concrete columns, in order:
  id,label,state,sourceKind,historical,sourceRecordId,sourceVersion
followed by that entity's registered attributes in profile order, except that
Document/EvidenceReading's shared sourceVersion column is emitted only once.
Their source locator version and attribute sourceVersion must agree whenever
both are non-null; a known value may fill the other's null. The
coalesced_source_version() helper enforces this without silently overwriting
conflicting provenance. Fact/ReviewField likewise share one label column:
non-null attribute and display labels must agree. The common display label is
required; the JSON representation retains the full envelope/attribute split.
RecordIndex: id,entityType
RecordLink: id,sourceRecordId,targetRecordId,relation,reason

UTF-8 comma-separated CSV has an exact header, standard CSV double quoting,
boolean true/false, signed64 money as canonical integer text, and null token
\N. Empty text is not null. Text encoding is contoso-csv-text-v1:
prefix a literal leading backslash or apostrophe with backslash (so a literal
\N becomes \\N). Prefix formula-leading text (=,+,-,@ after optional Unicode
whitespace or Cc/Cf control/format characters), and text beginning with
tab/CR/LF, with an apostrophe. This includes form feed, vertical tab,
nonbreaking space and leading BOM/zero-width format characters; do not trim
these characters out of the source value.
Decode these prefixes exactly once, in text fields only. This is reversible
CSV transport protection, not a change to the underlying source value.
Typed integer, minor and decimal cells are never formula-escaped, even when
negative or larger than JavaScript's safe-integer range. In Fact,
CalculationValue and ReviewField, a value with unit=money_minor is validated
as canonical signed64 minor text; canonical values with
unit=number/percentage/months/years use exact decimal text. Other value strings
retain their original text and receive text protection. Unknown/null currency
is not silently filled in by this text codec.
For this value-column transport choice, canonical decimal means plain decimal
text of at most 100 characters, no redundant fractional zeroes or signed zero,
scale at most 30 and magnitude at most 2^63, matching the application's scalar
transport boundary. Noncanonical display strings such as "-2.50%" or "-2.50"
remain exact protected text, rather than being normalized or rounded.

snapshot.json remains the lossless source-of-truth export. The export adapter's
export-conventions.json describes the inverse transport rules. Spreadsheet
applications can still coerce numeric types/precision; machine readers should
use the declared schema and must not treat formatting as bank evidence.

app.data_xray.model.Metadata provides columns(), export_manifest(),
csv_kind(), validate_rows(), validate_csv() and validate_export() for the export
adapter. encode_csv_value(value, kind) implements the shared cell convention;
the validator rejects unprotected formula text and malformed/double escaping.
Partitions use partitions/EntityName.csv; record IDs are globally unique in
the selected export, index rows correspond exactly to concrete rows, and
every link endpoint exists in that index. Filtered exports omit links to
unselected records, never invent endpoints or expand to foreign owners.

Attribution and licenses
-----------------------
Microsoft Corporation and the Microsoft CDM contributors:
https://github.com/microsoft/CDM/tree/dd21d715e05ebf740a11356c80b5c3b4c38a89c2
CDM content/documentation is CC BY 4.0; code is MIT. Original complete texts:
  vendor\cdm\LICENSE-CONTENT.txt
  vendor\cdm\LICENSE-CODE.txt
No upstream original was modified. The generated contosoBanking definitions,
field mappings and bounded validation profile are application-specific
adaptations; semantic references are attributed, not endorsements.

BIAN and contributors, public release13.0.0 semantic APIs:
https://github.com/bian-official/public/tree/b58bf4c2c300dd018afc084e9bb24b7adaaa3500
Apache License 2.0, complete original text:
  vendor\bian\LICENSE.txt
No upstream BIAN YAML was modified. Internal mapping judgments are Contoso
demo additions, not official BIAN certification or compliance assessments.
No third-party slide images or unlicensed images are redistributed.
