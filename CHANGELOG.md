# Changelog

Newest first. Every prompt version has one line of the form `` - `<prompt id>` <version>: <what changed> ``; `python -m tools.lint_prompts` checks it. A release turns `## Unreleased` into `## vX.Y.Z — YYYY-MM-DD` (`python -m tools.release prepare`, see `docs/RELEASING.md`); `python -m tools.release lint` checks the structure.

## Unreleased

### Prompts
- `ontology.build` 1.1.0: writes the generated Fabric items to `fabric/` and a readable brief to `docs/generated/` with `python -m tools.export_fabric --write` instead of leaving them in the kit's scratch folder.
- `ontology.enrich` 1.1.0: same regeneration step; the synonyms and PII markers can be reviewed in `docs/generated/ontology-brief.md`.
- `ontology.inspect` 1.0.0: read-only inspection of the lakehouse before the ontology is built.
- `ontology.build` 1.0.0: build or extend the ontology spec, then regenerate the Gen 2 ontology and the semantic model.
- `ontology.enrich` 1.0.0: descriptions, synonyms, PII classification and glossary for every entity type.
- `ontology.consume` 1.0.0: business questions through the ontology agent and the graph, checked against the golden set.
- `reports.executive-overview` 1.0.0: three-page executive Power BI report.
- `reports.customer-financial-health` 1.0.0: three-page relationship-manager Power BI report.
- `reports.monthly-brief` 1.0.0: PDF brief built offline in CI from the seed data.

### Artifacts
- `analysis/monthly_brief`: the Monthly Business Brief, a six-page PDF built offline from the gold layer (prompt `reports.monthly-brief`), reproducible byte for byte on one platform; CI builds it on every pull request and keeps it as an artifact.
- `fabric/ontology/ContosoBankOntology.Ontology` and `fabric/semantic-model/ContosoBankModel.SemanticModel` (core tier), generated from `kit/model/`; CI fails if they drift from the spec.
- `tools/deploy_preview.py`: an offline preview of which Fabric items a change would create, replace or remove, posted on every pull request.
- Engine and ontology spec: kit `contoso_onto` at commit 465d55c (branch `medallion-cdm`), vendored under `kit/`.
- Seed data: pinned snapshot 8600584 of the synthetic source repo, vendored under `seed/`.

### Pipelines
- Release: `release` workflow (run by hand from the Actions tab) checks the rules, rebuilds the files from the commit, and publishes a GitHub release with the Monthly Business Brief, the ontology brief, the Fabric item definitions, the prompt library and checksums. A dry run is the default. CI builds the same files on every pull request (`release-preview` artifact). Optional **Deploy to prod** (with Publish) deploys only what the last dev deployment that really ran had (a run whose deploy job was skipped does not count) and always loads the data again.
- Deployment (draft, off until `CD_ENABLED` is set, see `docs/SETUP.md`): `cd-dev` deploys to the dev workspace after a merge that changes what is deployed; `capacity` gives status, resume, pause and a guard every 3 hours that pauses a forgotten F4 (a demo hold variable stops it). `tools/cd.py` runs the steps, stops them at a deadline, checks the workspace is on the capacity it pauses, and always pauses the capacity once it has been resumed, also after a failure; it is tested with fakes and against the kit's Fabric mock, not yet against a live tenant. The capacity role needs `write` as well as read, suspend and resume (Microsoft's pause and resume page) and is assigned on the capacity itself.
- Plans: `docs/SETUP.md` (the one-time identity, role, Fabric settings and first runs, each step marked as needing your go) and `docs/PILOT.md` (shortcut and Databricks pilot, costs and decision rules; not started).
