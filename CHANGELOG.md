# Changelog

Newest first. Every prompt version has one line of the form `` - `<prompt id>` <version>: <what changed> ``; `python -m tools.lint_prompts` checks it.

## v1.0.0 (baseline, not yet released)

### Prompts
- `ontology.inspect` 1.0.0: read-only inspection of the lakehouse before the ontology is built.
- `ontology.build` 1.0.0: build or extend the ontology spec, then regenerate the Gen 2 ontology and the semantic model.
- `ontology.enrich` 1.0.0: descriptions, synonyms, PII classification and glossary for every entity type.
- `ontology.consume` 1.0.0: business questions through the ontology agent and the graph, checked against the golden set.
- `reports.executive-overview` 1.0.0: three-page executive Power BI report.
- `reports.customer-financial-health` 1.0.0: three-page relationship-manager Power BI report.
- `reports.monthly-brief` 1.0.0: PDF brief built offline in CI from the seed data.

### Artifacts
- Engine and ontology spec: kit `contoso_onto` at commit 465d55c (branch `medallion-cdm`), vendored under `kit/`.
- Seed data: pinned snapshot 8600584 of the synthetic source repo, vendored under `seed/`.
