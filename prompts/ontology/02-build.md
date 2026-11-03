---
id: ontology.build
title: Build the Contoso retail-bank ontology from the lakehouse
version: 1.0.0
area: ontology
stage: build
skills: [fabriciq-ontology-cli, e2e-medallion-architecture]
inputs:
  workspace: Contoso-Bank-Ontology-Demo
  lakehouse: ContosoBankLH
  tier: core
outputs:
  - kit/model/**
guardrails: [pr-only, preview-before-deploy, synthetic-data-only]
---
Plan first and show me the plan.

Build the Contoso retail-bank ontology from the CDM/BIAN-aligned lakehouse `{{lakehouse}}` in the workspace `{{workspace}}`, using the newest inspection report in `docs/inspection/` as input.

- The source of truth is the ontology spec in `kit/model/` (YAML). Change the spec, never generated files. Keep every entity, property and relationship that already exists; add only what the inspection shows is missing.
- The `{{tier}}` input sets the scope. `core` covers customers and households, accounts, cards, deposits, loans and instalments, investments, transactions and scheduled obligations, income, goals, delegated authority (consents, access grants, registered agents, mandates), servicing (incidents, service records, documents, CRM cases) and the catalogue with journeys, SOPs, laws and authority rules. `full` adds consent resources, grant permissions, agent scopes, proactive events, product parameters, journey links, requirements, bank agents and bank operations (business units, regions, relationship managers, campaigns, monthly metrics, risk alerts, fraud signals).
- Every entity type has a business key. Use only types the ontology supports (string, boolean, datetime, int64, double). Money is rupees as double, never decimal: decimals return null in Fabric Graph.
- Regenerate the Direct Lake semantic model and the Gen 2 ontology with the kit (`python -m contoso_onto.cli build --repo ../seed --tier {{tier}} --out ../out`, run from `kit/`). The build validates the spec, the semantic model and the ontology and must report `errors=0` for each. Then refresh the manifest with `python -m tools.manifest --write`.
- Before anything is deployed, show me a preview: the entity types, properties, relationships and bindings that would be created, replaced or removed. Ask me which workspace to use.

Do not deploy. Open a pull request from a `copilot/ontology-build` branch and quote this prompt's id and version in it.
