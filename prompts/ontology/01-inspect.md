---
id: ontology.inspect
title: Inspect the lakehouse before building the ontology
version: 1.0.0
area: ontology
stage: inspect
skills: [onelake-catalog-cli, fabriciq-ontology-cli]
inputs:
  workspace: Contoso-Bank-Ontology-Demo
  lakehouse: ContosoBankLH
  date: today, YYYY-MM-DD
outputs:
  - docs/inspection/**
guardrails: [pr-only, preview-before-deploy, synthetic-data-only]
---
Plan first and show me the plan. Then, read-only, inspect the lakehouse `{{lakehouse}}` in the Fabric workspace `{{workspace}}`:

1. List every table by layer (bronze files, `silver_*`, and gold `dim_*`, `fact_*`, `bridge_*`, `ref_*`) with row counts, columns, types and when each was last loaded.
2. Show the lineage bronze to silver to gold from `silver_entity_catalog` and `silver_attribute_catalog`.
3. Flag anything that will cause trouble for an ontology: decimal columns (they return null in Fabric Graph), column names with spaces or special characters (column mapping), tables that are shortcuts or external instead of managed, keys that are not unique, and tables with no relationship to any other table.
4. For each business concept, say whether the ontology should bind to silver or gold, and why.

Write the result to `docs/inspection/{{date}}.md`. Do not create, change or delete anything in Fabric or Azure. Open a pull request that contains only that file, and quote this prompt's id and version in it.
