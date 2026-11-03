# Assessment: is the superapp4 seed ready for an ontology?

Source: `hrshl-demo/contoso-retail-superapp4`, branch `hrshl-demo-expert-barnacle`, commit `860058471`, read only. 65 JSON files become
81 record collections and 1,195 rows. `qa/` answer keys and `assets/` are excluded on purpose. Regenerate the raw numbers at any
time (read only): `python -m contoso_onto assess --repo <checkout> --out <dir>` writes `assessment.json` and `ASSESSMENT.generated.md`.

## Verdict

**Not ready as-is; ready after the gold transform that `build.sh` applies.** The seed is clean (both releases are `approved`, 167 files
hash-verified, 0 mismatches), but its shape works against a Fabric IQ ontology and two of its properties would make an agent
confidently wrong: one customer id that stands for two different people's data, and money in four unit conventions.

| ID | Severity | Finding | What the kit does about it |
|---|---|---|---|
| A07 | **critical** | `ownerId` `rhea` exists in both releases with different accounts, cards and 231 vs 114 transactions. Any aggregate by owner double counts. Product id `CB-SAV-SALARY` collides too. | Frontier ids stay raw; legacy copies are namespaced (`rhea~legacy`) with `superseded_by_customer_id` and a release id on every row; rule `DefaultToFrontierRelease`; stated in the agent instructions. |
| A09 | needs decision | Four money/rate conventions: ledger paise, product parameters in rupees ("INR_major unless suffixed Minor"), bank aggregates in crore, rates in basis points *and* percent. A paise balance compared with a rupee fee is wrong by 100x. | One suffix per unit: `*_inr` (rupees, agent-facing), `*_paise` (hidden, exact), `*_pct`, `*_bps` (hidden), `*_crore_inr`; rule `UnitsAndCurrency` explains them; the voice profile marks speakable units. |
| A13 | needs decision | The same rupee appears in two record types (deposit principal = investment value; goal "current" = investment value or reserve balance). Summing double counts. | Total assets use accounts + investments only; `Goal.CurrentInr` is documented as an earmark; rule `NoDoubleAllocation`; flagged in the agent instructions. |
| A01 | blocks the graph | 70 money fields are nested `{amountMinor, currency}` objects. Ontology properties must be scalars and Decimal returns null in the graph. | Flattened to `*_paise` (int64) + `*_inr` (double). No Decimal anywhere. |
| A02 | blocks the graph | 20 arrays of objects (loan repayment schedule, mandate history, consent resource refs, ...). | Child tables with a surrogate key and parent FK (`loan_installment`, `mandate_event`, `consent_resource`, ...). |
| A03 | reshape | 71 arrays of scalars (scopes, permissions, ...). | Core tier: delimited text columns; full tier: bridge entities. |
| A04 | reshape | Date-only fields and RFC 3339 timestamps with +05:30 offsets. | UTC instant plus an IST date column; date-only stays `DATE`; a calendar dimension with the Indian fiscal year. |
| A05 / A06 | reshape | 23 reference-like fields have no target table; `transactions.sourceRefs[]` mixes document and case ids (83% resolve). | Kept as text plus a `*_type` column; FK columns only where the target exists; evidence split into document and incident. |
| A10 | governance | 38 PII-like fields. The data is synthetic, but the model behaves as if it were not. | `date_of_birth`, `kyc_address`, `email`, `policy_number`, `kya_key_fingerprint` stay in the lakehouse, outside the ontology; masked fields are tagged `[PII]`; the voice profile never speaks them in full. Plan OneLake column-level security before real data. |
| A14 | needs decision | The repo's CDM profile has 45 entities; 26 are fed by seed data, 19 exist only as runtime session state. | The ontology is built on the seed-backed entities; runtime-only ones can be fed later from the app's CDM export into the same gold schema. |
| A16 | needs decision | The repo cites Microsoft's Financial Services CDM (8 of 45 entities, semantic references only) and BIAN 13.0.0 (574 attribute mappings: 278 custom, 79 provisional, 217 unmapped). v1 shared a profile name for only 12 of the 51 gold entities. | The naming now follows the profile ([NAMING_STANDARD.md](NAMING_STANDARD.md)): silver holds 19 profile entities, realised by 18 of the 51 gold tables (the profile's `Consent` and `FrontierConsent` merge into one `Consent`); the other 33 are Contoso extension entities (24 silver extension entities and 9 curated reference tables) and say so. `model/standards.yaml` (generated from the registry, checked by a test) carries the BIAN and CDM alignment, and every entity description reads what the entity is, its standards sentence (the alignment and the repo's own caveat, or the extension label), then the star clause (role, grain, lineage). The agent-authority layer (agents, mandates, grants, consents) has no verified BIAN mapping and is stated as such. See "Standards alignment" in the README. |
| A08 | info | 7 fields are null in every row; `social-profiles.json` is empty for the Frontier release. | Dropped from the ontology (runtime outcomes are written by the live app). |
| A11 | info | 54 long free-text fields. | Unstructured content stays in Foundry IQ; readable summary columns stay in the ontology. |
| A12 | info | The PRD says `retail-frontier-v1` stays `in_review`; `release.json` says `approved`. | Commit and content hash are pinned in the load manifest; the doc drift is the repo owner's to fix (out of scope: the repo is read only). |
| A15 | governance | `qa/` holds test-only answer keys that warn not to feed a runtime. | Never read. |

## Target model

| | smoke | core (default) | full |
|---|---|---|---|
| Ontology entities | 12 | 31 | 50 |
| Properties / relationships | 160 / 22 | 417 / 53 | more |
| Semantic-model relationships / measures | 12 / 20 | 36 / 39 | more |
| Advisory rules | 23 | 23 | 23 |

The semantic-model relationships include the semantic-only date-role relationships to `dim_date` (core 36, full 56; v1 had 35 and 54: the star added one for the
service case, and one for the proactive candidate in the full tier). The ontology counts did not move.

51 gold tables, about 2.5k rows, in one plain lakehouse (no schemas, managed Delta, no column mapping, no Decimal, string/int keys,
one static binding per entity, unique relationship names). Smoke is the bisect tier: if a deployment is rejected, the smallest set
that still exercises every construct is already live.

## Golden questions

29 questions with answer keys computed from the gold tables (`model/golden.yaml`, `oracles.py`): limits and authority
("up to Rs 20,000 per action"), balances as of the business date (2026-09-30), the Frontier-vs-legacy identity trap, earmarks vs
assets, units. `python -m contoso_onto verify --answers answers.json` (or `bash build.sh --helper verify --answers answers.json`)
scores collected agent answers with a number/string comparer that tolerates Indian digit grouping.
