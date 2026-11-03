---
id: reports.customer-financial-health
title: Customer Financial Health (Power BI)
version: 1.0.0
area: reports
stage: author
skills: [powerbi-report-cli, semantic-model-authoring]
inputs:
  workspace: Contoso-Bank-Ontology-Demo
  semantic_model: ContosoBankModel
outputs:
  - fabric/reports/**
  - docs/reports/**
guardrails: [pr-only, preview-before-deploy, synthetic-data-only]
---
Plan first, show me the plan, and lock the report spec with me before you build anything.

Create a Power BI report called "Customer Financial Health" on the semantic model `{{semantic_model}}` in the workspace `{{workspace}}`. The audience is a relationship manager preparing a customer conversation. Reuse the approved measures in the model; create a measure only if none exists, and tell me which.

Pages:
1. **Customer 360**: a customer slicer, household members, accounts and balances, and net position (assets, liabilities and the difference) with a visible note on what is not included.
2. **Obligations and goals**: monthly obligations, monthly EMI, debt service to income, and goal funding by goal.
3. **Servicing**: open CRM cases, incidents with recovered amount, and documents.

Respect the model's rules: filter by Release ID before adding customers, and never add deposits and investments together because a Frontier deposit is already inside investments. Make it accessible, keep one theme, validate the PBIR, render a preview of every page outside the PBIP folder, and do not publish. Open a pull request from a `copilot/report-customer-financial-health` branch and quote this prompt's id and version in it.
