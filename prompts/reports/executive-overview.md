---
id: reports.executive-overview
title: Contoso Bank Executive Overview (Power BI)
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

Create a Power BI report called "Contoso Bank Executive Overview" on the semantic model `{{semantic_model}}` in the workspace `{{workspace}}`. The audience is the head of retail banking, who wants the bank-wide picture across all customers. Inspect the model first: use the approved measures already in it (for example Customers, Total Posted Balance (INR), Loan Principal Outstanding (INR), Credit Utilisation, Net Cash Flow (INR), Open CRM Cases, Recovered Amount (INR)), choose dimensions only from columns the model really has, and use only relationships that exist. Create a measure only if none exists, and tell me which. If a visual needs a relationship or a column that is missing, say so instead of inventing it.

Pages:
1. **Overview**: KPI cards for customers, total posted balance, loan principal outstanding, credit utilisation and open CRM cases; balances by account type; loans by product.
2. **Money movement**: total debits, total credits and net cash flow by month; monthly obligations; disputed transactions.
3. **Servicing and risk**: CRM cases by stage, incidents by type with the recovered amount, and documents.

Filter by Release ID before adding customers (the model's default story is the Frontier release). Make the report accessible (contrast, alt text, tab order), use one theme on every page, and keep each business measure in its display folder. Validate the PBIR, render a preview of every page, and keep the previews outside the PBIP folder. Do not publish. Open a pull request from a `copilot/report-executive-overview` branch and quote this prompt's id and version in it.
