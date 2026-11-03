---
id: reports.monthly-brief
title: Monthly Business Brief (PDF built offline in CI)
version: 1.0.0
area: reports
stage: author
skills: []
inputs:
  seed: seed
outputs:
  - analysis/**
  - docs/reports/**
guardrails: [pr-only, preview-before-deploy, synthetic-data-only]
---
Plan first and show me the plan.

Analyse the Contoso retail-bank gold tables and produce a PDF called "Monthly Business Brief" for the head of retail banking.

- Data: build the gold layer offline with the kit (from `kit/`, `python -m contoso_onto.cli build --repo ../{{seed}} --tier full --out ../out`; the bank-operations tables are in the `full` tier) and read the Parquet files in `out/gold`: `dim_business_unit`, `dim_region`, `dim_campaign`, `fact_monthly_metric`, `fact_risk_alert` and `fact_fraud_signal`. Do not connect to Fabric or Azure.
- Content, one page per theme with a short plain-English headline above each chart: (1) business units: portfolio, customers, net interest margin, year-on-year growth and target attainment; (2) monthly revenue and portfolio trend per business unit over the months in `fact_monthly_metric`, with outliers marked and a 3-month forecast, saying which method you used and its limits; (3) campaigns: reach, engaged and converted, conversion and attributed revenue; (4) regions (deposits, advances, NPS, digital adoption) with the risk alerts and the fraud signals (detected, blocked, trend) by severity.
- Money is rupees: show crore INR with one decimal. Use the definitions in `kit/model/measures.yaml`. If a number needs a definition that no measure has, say so on the page.
- Put the code in `analysis/monthly_brief/` as one deterministic script (the same data gives the same PDF) and write the PDF to `out/reports/monthly-brief.pdf`. Do not commit the PDF; CI builds it and the release attaches it.

Do not deploy or publish anything. Open a pull request from a `copilot/report-monthly-brief` branch and quote this prompt's id and version in it.
