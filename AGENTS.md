# AGENTS.md: rules for any AI agent working in this repo

This repo turns versioned prompts into a Fabric IQ ontology and Power BI reports, and ships them through GitHub CI/CD. **Agents propose; humans approve.** These rules apply to Copilot CLI, the Copilot app, VS Code and the GitHub coding agent alike.

## Hard rules

1. **Propose, never deploy.** Work on a branch named `copilot/<intent>`, open a pull request, stop. Never push to `main`. Never run or dispatch the `cd-dev`, `release`, `capacity` or `wipe` workflows. Never call a Fabric or Azure write API from your own session.
2. **Preview before any remote change.** Anything that would write to Fabric or Azure is first shown as a preview (created, replaced, deleted) and waits for an explicit "go" from the human.
3. **No billable action without an explicit go.** Creating or resuming a Fabric capacity, starting a VM, Databricks, storage: ask first and state the cost.
4. **Synthetic data only.** No real customer data, no credentials, tokens, connection strings or keys. Tenant, subscription and workspace IDs live in GitHub Actions variables, not in files.
5. **Never change tenant-wide Fabric or Entra settings.** Say what is needed; a human does it.
6. **Do not hand-edit generated or vendored files.** Change the source (spec or prompt), regenerate, commit both.

## Where things live

| Path | What | Who edits |
|---|---|---|
| `prompts/` | Versioned prompts (the intent) | You, through a PR |
| `kit/model/` | Ontology and semantic-model spec (YAML), the source of truth | You, through a PR |
| `fabric/ontology/`, `fabric/semantic-model/`, `docs/generated/` | Generated from the spec by `python -m tools.export_fabric --write`; CI fails if they drift | The tool, never by hand |
| `fabric/reports/` | Power BI report definitions (PBIR) | The report tooling |
| `kit/contoso_onto/` | The deterministic engine (seed to bronze, silver, gold, semantic model, ontology, graph, verify) | Only for engine changes, with tests |
| `seed/` | Synthetic seed data, read-only | Nobody |
| `analysis/` | Analyses run by prompts (the PDF brief); `docs/reports/` documents them | You, through a PR |
| `tools/`, `.github/workflows/` | Repo tooling and pipelines | Pipeline changes only |

## Prompt discipline

- Changing a prompt's body or inputs needs a `version` bump (semver) and a line in `CHANGELOG.md`. `python -m tools.lint_prompts` enforces it.
- After a run that changes artifacts, regenerate and refresh: `python -m tools.export_fabric --write`, then `python -m tools.manifest --write`.
- Quote the prompt id and version in the PR description.
- Releases: `python -m tools.release prepare` (in a pull request) turns `## Unreleased` into a dated section. Never create tags or releases yourself; a human runs the `release` workflow. See `docs/RELEASING.md`.

## Skills

Microsoft's Fabric skills are pinned in `skills.lock`. Use `fabriciq-ontology-cli` to explore and explain an ontology and `powerbi-report-cli` for reports. Our ontology uses the **new (Gen 2, TMDL) experience**; the ontology skill documents the older JSON format, so generate with `python -m tools.export_fabric --write` (it runs the kit's offline build) and never hand-write old-format JSON. Every Fabric REST call carries the header `x-ms-fabric-skill: <skill>`.

## Before you open a PR

```powershell
python -m tools.check
cd kit
python -m pytest -q -m "not slow"     # fast suite; the full suite runs on release
```

Report validation screenshots go outside the PBIP folder and are never committed; the release attaches them.
