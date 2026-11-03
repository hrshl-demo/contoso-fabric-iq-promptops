# Prompts

Each file is one versioned prompt: YAML frontmatter plus a natural-language body. The body is what you give to Copilot; the frontmatter is what the pipeline checks.

```yaml
---
id: ontology.build                 # area.slug, unique; the area is also the folder name
title: Build the Contoso retail-bank ontology from the lakehouse
version: 1.0.0                     # semver; any change to the body or settings needs a higher version
area: ontology                     # ontology | reports
stage: build                       # inspect | build | enrich | consume | author | change
skills: [fabriciq-ontology-cli]    # must be listed in skills.lock
inputs:                            # {{name}} placeholders used in the body, with their defaults
  workspace: Contoso-Bank-Ontology-Demo
outputs:                           # repo paths a run may create or change
  - kit/model/**
guardrails: [pr-only, preview-before-deploy, synthetic-data-only]
---
```

## Prompts in this repo

| Id | Stage | Produces |
|---|---|---|
| `ontology.inspect` | inspect | `docs/inspection/<date>.md` (read-only) |
| `ontology.build` | build | ontology spec changes in `kit/model/`, regenerated parts in `fabric/` |
| `ontology.enrich` | enrich | descriptions, synonyms, PII class and glossary in the spec |
| `ontology.consume` | consume | `docs/consume/<date>.md` (read-only) |
| `reports.executive-overview` | author | Power BI report in `fabric/reports/` |
| `reports.customer-financial-health` | author | Power BI report in `fabric/reports/` |
| `reports.monthly-brief` | author | PDF brief script in `analysis/` (the PDF is built in CI) |

The flow follows the Microsoft Fabric skills demo: **Inspect, Build, Enrich, Consume**. Ask the agent to plan first (Shift+Tab in Copilot CLI), review the plan, then let it run.

## How to run one

- **Copilot CLI or the Copilot app:** ask for the skill (for example "run the ontology-build skill"), or paste the body of the prompt.
- **VS Code:** type `/ontology.build` in Copilot Chat (the wrapper in `.github/prompts/`).
- Every run ends in a pull request. Nothing is deployed from a prompt run.

## Changing a prompt

1. Edit the prompt, bump `version`, add a line to `CHANGELOG.md`.
2. `python -m tools.wrappers --write` and `python -m tools.manifest --write`.
3. `python -m tools.check`, then open the PR. CI repeats the checks and compares versions against `main`.
