---
id: ontology.enrich
title: Make the ontology AI-ready with descriptions, synonyms and PII context
version: 1.1.0
area: ontology
stage: enrich
skills: [fabriciq-ontology-cli]
inputs:
  workspace: Contoso-Bank-Ontology-Demo
  tier: core
outputs:
  - kit/model/**
  - fabric/**
  - docs/generated/**
guardrails: [pr-only, preview-before-deploy, synthetic-data-only]
---
Plan first and show me the plan.

Make the ontology in `kit/model/` AI-ready for the workspace `{{workspace}}`. Work only in the `description` and `synonyms` of each table in `kit/model/gold/*.yaml`, the column `desc` text there, and the measure `desc` text in `kit/model/measures.yaml`. For every entity type:

- Write a one-sentence `description` a banker would recognise, with no jargon. Keep what is already true; improve what is vague.
- Keep the existing `synonyms` and add the ones customers and staff actually use (for example FD, EMI, CASA, KYC, RM). Never remove one.
- End the `description` with `PII: <none|personal|sensitive-personal|financial>. Steward: <role>.` The steward is a business role such as "Retail Banking Data Office", never a person.
- Every column that holds personal data carries `pii: true` and a `desc` that says how it may be used, in the style already used (for example "Restricted: identity verification only").

Respect the kit's budgets: an entity description is at most 800 characters (500 in compact mode) after the kit adds its standards and architecture sentences; column and measure descriptions are at most 200 characters. The spec validator in the build reports every overrun; fix them, do not raise a budget.

Take rules and meanings only from `kit/model/rules.yaml`, the seed documents and the existing descriptions. Never invent a business rule; list what you were unsure about in the pull request instead.

Regenerate with `python -m tools.export_fabric --write --tier {{tier}}` (the build it runs must report `errors=0`), refresh the manifest with `python -m tools.manifest --write`, and run `python -m tools.check`. Show me a preview of the text that will be written: the new descriptions are in `fabric/`, and `docs/generated/ontology-brief.md` shows every entity with its synonyms and PII markers. Do not deploy. Open a pull request from a `copilot/ontology-enrich` branch and quote this prompt's id and version in it.
