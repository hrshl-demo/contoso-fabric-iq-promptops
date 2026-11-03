# Architecture

## Three tiers

1. **Deterministic engine** (`kit/`): seed to bronze, silver (CDM and BIAN), gold (star schema), a Direct Lake semantic model, a Gen 2 ontology (TMDL), the graph and verification. The same input always gives the same output.
2. **Prompt-driven artifacts**: the ontology spec (`kit/model/`), the generated Fabric item folders (`fabric/`), Power BI reports and the PDF brief (`analysis/`). Copilot produces and changes them from the prompts in `prompts/`, with Microsoft's Fabric skills.
3. **GitHub Actions plumbing**: CI, CD to dev, release to prod, capacity control.

Artifacts are the source of truth. Prompts are versioned intent. CI checks meaning and contracts (valid TMDL and PBIR, golden questions, no PII), not byte-identical regeneration, because model output varies between runs.

## Lifecycle of a change

1. Someone edits a prompt (or writes a new one) and runs it in Copilot. The agent plans, you approve the plan.
2. The agent changes the spec or report, regenerates the artifacts and opens a pull request from `copilot/<intent>`.
3. CI runs: prompt lint (a changed prompt needs a higher version and a changelog line), generated wrappers, manifest, PII and secret scan, tooling tests, kit tests, an offline build of the ontology and semantic model, a drift check (the generated items in `fabric/` must equal what the spec produces), PBIR validation when reports exist, and a **deployment preview** comment that lists which Fabric items, entities, properties, relationships and measures the change would create, replace or remove. Nothing is deployed from a pull request.
4. Merge to `main` triggers CD to the dev workspace: resume the F4, deploy, verify with the golden questions, pause.
5. A version tag starts the release: a person approves, the pipeline promotes to prod and attaches report PDFs and PNGs and the release notes.

## Versioning

| What | How it is versioned |
|---|---|
| Prompt | `version` (semver) in its frontmatter and a line in `CHANGELOG.md` |
| Ontology and semantic model | spec in `kit/model/`, generated item folders in `fabric/` (`python -m tools.export_fabric --write`; deterministic, so a diff is a real change), and `docs/generated/ontology-brief.md` for human review |
| Reports | PBIR folders in `fabric/`, previews attached to the release (never committed) |
| Traceability | `manifest.lock.json`: prompt version, prompt hash and a tree hash of the files its `outputs` match |
| Skills | pinned in `skills.lock` (`microsoft/skills-for-fabric` tag and commit) |

## Environments

Dev and prod are two workspaces on one F4 in East US 2. The F4 is paused by default. The two capacity-level settings an admin must grant (create ontology items; Copilot and Azure OpenAI) are per capacity, so a paused and resumed F4 avoids repeating that step.

## Identity

GitHub-hosted runners sign in with OIDC (no stored secret) as a service principal that can resume and pause the capacity (a custom role with only `Microsoft.Fabric/capacities` read, write, suspend and resume) and is a Contributor on the pre-created workspaces. Repos created after 15 July 2026 use the immutable OIDC subject format, so the federated credential is created from the repo's real claim template. If the service principal cannot create ontology items, a self-hosted runner on the VM `host` signed in as the demo admin is the fallback (see [DECISIONS.md](DECISIONS.md)).

## Guardrails

- Agents propose, never deploy ([AGENTS.md](../AGENTS.md)).
- Nothing billable without an explicit go.
- Synthetic data only; `tools/pii_guard.py` and gitleaks run on every pull request.
- Tenant-wide settings are changed by a person, never by the pipeline.
- Third-party actions are pinned by commit SHA.
