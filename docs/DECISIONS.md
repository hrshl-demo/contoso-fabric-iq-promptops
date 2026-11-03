# Decisions

Dated 3 November 2026. Evidence is from Microsoft Learn and the Microsoft skills repo as of that date; the ontology and graph features are in preview and change often.

## Made

| # | Decision | Why | Revisit when |
|---|---|---|---|
| D0 | The F4 capacity runs in **East US 2** | It is the kit default, the ontology agent needs no cross-geo setting there, and F4 costs USD 0.72/h (Australia East 0.84, Central India 0.80, South India 0.88). India South Central has no Fabric capacities. The build VM size is restricted in Australia East | A region is chosen for latency or residency |
| D1 | **Private repo on GitHub Free**, with a manual-approval workflow in front of production | Required reviewers, wait timers and environments on a private repo need GitHub Enterprise Cloud, which a personal account cannot buy. Free keeps cost at zero. Branch protection and rulesets are not available either, so the PR-only rule lives in `AGENTS.md`, `CODEOWNERS` and the pipeline | You move to Pro (environments, branch protection), an organisation, or a public repo |
| D2 | **Pilot ADLS Gen2 shortcuts first, then decide where the medallion lives.** Databricks is tried only if that pilot passes. Fabric-native managed tables stay the proven fallback | See the evidence below: the docs disagree on whether the ontology binds to shortcut tables | The pilot finishes (plan and costs: [PILOT.md](PILOT.md)) |
| D3 | **OIDC service principal on GitHub-hosted runners** for CI/CD. The federated credential trusts runs on `main` only, and the app can pause, resume and change the one F4 (it cannot create or delete one) and administer two workspaces. Fallback: a self-hosted runner on the VM `host` signed in as the demo admin | Passwordless and the standard story. The pipelines are written and tested with fakes; the setup ([SETUP.md](SETUP.md)) and whether a service principal may create ontology (preview) items are not yet done or proven | The first live run, or the spike shows the service principal cannot create ontology or graph items |

Defaults assumed, change by pull request: repo `hrshl-demo/contoso-fabric-iq-promptops`; reports are Power BI PBIR plus PDF/PNG exports; the F4 is paused, not wiped, between sessions; the skills bundle is pinned in `skills.lock`.

## Open

| # | Question | How it gets decided |
|---|---|---|
| D4 | Should the ontology bind to **silver** (CDM and BIAN entities) instead of gold? | The pilot binds silver entities. Silver has `decimal(38,4)` columns (null in the graph), both seed releases side by side (use the unique `id` as key) and arrays as child tables. Gold stays the default until the pilot is clean |
| D5 | Deploy Fabric items with `fabric-cicd` or with the kit's own `deploy`? | A spike deploys the Gen 2 ontology parts with `fabric-cicd` 1.4.0 (it lists Ontology and GraphModel). The kit's `deploy` is proven live and stays the fallback |
| D6 | Native ontology metadata (PII class, steward) instead of description text | When the ontology definition API exposes it; until then `ontology.enrich` writes it into the description |

## Evidence behind D2

- The new (Gen 2) ontology is TMDL with Direct Lake partitions over a lakehouse SQL endpoint. The kit already emits it (`kit/contoso_onto/oac.py`).
- Learn, *What is ontology (preview)?* (16 Oct): binding sources include lakehouses, warehouses, SQL databases, mirrored databases and "supported OneLake constructs such as shortcuts, views, and materialized views". Learn, *Bind data* (21 Oct) lists lakehouse, mirrored database, semantic model, SQL database, warehouse, eventhouse and KQL database.
- Learn, *Materialize an ontology graph* (29 Sep): graph accepts "only delta tables from lakehouses or mirrored databases"; entities with several backing tables or semantic-model bindings are not eligible.
- Against: Learn, *Troubleshoot ontology* (1 Oct) and the skills repo say ontology supports **managed** lakehouse tables only, not external tables shown in the lakehouse. The skill documents the older JSON format; the old experience retires on 31 Jan 2027.
- Databricks routes: an ADLS Gen2 shortcut to Delta files, or a mirrored Azure Databricks catalog (Unity Catalog with external data access, `EXTERNAL USE SCHEMA` and `SELECT`; creates a mirrored item, a SQL endpoint and shortcuts; read-only).

## Costs (East US 2, Azure Retail Prices API, 3 Nov 2026)

| Item | Price |
|---|---|
| Fabric F4 | USD 0.72/h while running; paused costs no compute |
| Build VM `host` (Standard_D4ads_v6) | about USD 0.236/h while running |
| Databricks job compute | DBU USD 0.15 (Standard) or 0.30 (Premium) plus the VM; a 20-minute single-node build is about USD 0.11 to 0.15 |
| Databricks idle | secure-connectivity NAT gateway about USD 33/month while a workspace exists |
| GitHub Actions on Free | about 2,000 Linux minutes per month |

## Never without an explicit go

Create or resume a capacity, start a VM, create storage or Databricks, create the Entra app, change a tenant-wide Fabric setting, merge or push to `main`, wipe.
