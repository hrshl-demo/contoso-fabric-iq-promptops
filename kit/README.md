# Contoso bank ontology demo (Microsoft Fabric IQ)

One ontology that becomes the backbone for the conversational AI and voice use cases of the Contoso retail super-app.
This kit reads the seed data of [`contoso-retail-superapp4`](https://github.com/hrshl-demo/contoso-retail-superapp4)
(pinned commit `860058471`, **read only**, never edited), makes it AI-ready as a medallion (bronze, silver, gold), and builds everything in Fabric from code:

```
seed JSON (read only)
  -> bronze   Files/bronze/<release>/...    the seed JSON files the silver tables read, verbatim, plus the table bronze_file_manifest
  -> silver   silver_* Delta tables         the repo's CDM application profile (+ official-name alias columns); catalogs silver_entity_catalog, silver_attribute_catalog
  -> gold     dim_* fact_* bridge_* ref_*   the star schema built from silver: units + ids fixed (51 tables on the full tier)
  -> Direct Lake semantic model (ContosoBankModel, 39 measures)  -> Fabric IQ ontology (Gen 2, 31 entities on the core tier)  -> graph (REST: 31 node types, 53 edge types)
  -> agent pack + tests (data agent, MCP, voice, Foundry IQ)           the semantic model and the ontology bind to gold only
```

Synthetic data only. Nothing is created until you run `build.sh`; running it **is** the authorisation (no prompts), as with the
superapp4 scripts.

## The build is a CI/CD cycle

`bash build.sh` runs four stages; every stage is idempotent, so a re-run resumes exactly where the last one stopped.

| Stage | What happens |
|---|---|
| **1 BUILD** | the data layers for the tier (bronze seed files, silver and gold tables), brand-new resource group + F4, workspace, lakehouse, then **bronze, silver, gold loaded in that order** (a layer is skipped when it has not changed and its tables are in the lakehouse), Direct Lake semantic model |
| **2 APPROVE** | the one human step. The run tries to create the ontology; if Fabric answers `403 FeatureNotAvailable` it **asks a capacity admin** for two delegated settings (it prints the direct link for the new capacity), **waits 15 minutes** for them to propagate, then **polls every minute** (up to 120) and carries on by itself the moment it works |
| **3 DEPLOY** | the graph, over REST: the definition is generated from the specification, written to the graph model Fabric created next to the ontology, refreshed and checked. No portal wizard |
| **4 TEST** | verify the ontology, check the MCP endpoint, then the ontology agent answers its golden questions (5 by default, all of them with `--golden full`); reported, not fatal |

`bash build.sh --fresh` adds a stage 0: it wipes whatever an earlier build of this kit left (an older F4, workspace, resource group) first.
`--until build|approve|deploy|test` stops after a stage. Timing: `--gate-wait MIN` (15), `--gate-poll SEC` (60), `--gate-timeout MIN` (120),
`--no-gate-wait` (stop with exit 3 at the gate instead). The default region is `eastus2`: the ontology agent works there with only the two
capacity settings; outside the US and EU it also needs the cross-geo Azure OpenAI setting (the request lists it).

## Run it on hostvm

```bash
cd ~/contoso-bank-ontology-demo
bash build.sh --preflight     # read-only: checks + plan + cost, creates nothing
bash build.sh                 # the cycle above (resumable); the F4 capacity bills about $0.88/h while it runs
bash build.sh --fresh         # wipe an earlier build first, then the cycle: a clean run from nothing
bash build.sh --diagnose      # read-only: ledger vs what exists, portal drift, MCP endpoint
bash build.sh --pause         # stop compute billing (also: --resume)
bash wipe.sh                  # hard-delete everything build.sh created (the whole resource group too), verify it is gone, audit the subscription
```

From your laptop, `bash scripts/hostvm.sh up` starts hostvm, publishes this kit to the private repo, gives hostvm a read-only deploy
key and clones/fast-forwards it there (see [hostvm.sh](scripts/hostvm.sh); `status`, `exec`, `logs`, `down` are there too).

| Command | What it does |
|---|---|
| `bash build.sh` | stages BUILD, APPROVE, DEPLOY, TEST (above), then the agent pack and `NEXT_STEPS.md` |
| `--fresh` | stage 0: wipe an earlier build of this kit first |
| `--until STAGE` | stop after `build`, `approve`, `deploy` or `test` |
| `--gate-wait` / `--gate-poll` / `--gate-timeout` / `--no-gate-wait` | the APPROVE stage: minutes to wait, seconds between polls, minutes to give up, or do not wait |
| `--no-graph` | leave Configure graph to the portal |
| `--golden none\|smoke\|full`, `--golden-parallel N`, `--strict-golden` | the TEST stage: which golden questions, how many at once (2), and whether a failure fails the build |
| `--reload` | load every layer (bronze files, silver, gold) again even when the lakehouse already holds the same data |
| `--preflight` / `--diagnose` | read-only; safe at any time |
| `--pause` / `--resume` / `--pause-after` | capacity cost control (`--pause-after` pauses when a build finishes) |
| `--tier smoke\|core\|full` | ontology size: 12 / 31 / 50 entities (default `core`). It also sizes the data: a tier builds and loads only its own gold tables, the silver tables behind them and the seed files those read: smoke 18 bronze files, 15 silver tables, 13 gold tables; core 37 bronze files, 30 silver tables, 32 gold tables; full 44 bronze files, 45 silver tables, 51 gold tables |
| `--descriptions full\|compact` | entity descriptions of the ontology, the semantic model and the data dictionary: `full` (default) adds the architecture clause (star role, grain, lineage); `compact` is the business meaning and the standards sentence only, each line within 500 characters. If Fabric refuses a definition with HTTP 400 the build retries once in `compact` and remembers it; `--descriptions full` tries the full text again. See [docs/MEDALLION.md](docs/MEDALLION.md) |
| `--seed DIR` | carry a portal-authored ontology forward (see [docs/LIVE_CHECKLIST.md](docs/LIVE_CHECKLIST.md)) |
| `--force-replace` | overwrite portal edits or items this kit did not create (the live version is backed up first) |
| `--loader delta` | write every table with delta-rs instead of the Load Table API. A normal build already installs `deltalake` (about 100 MB) in its own environment and uses it, table by table, only for a table whose Load Table call the API itself refused (HTTP 400/415/422, or a failed operation whose error code is not about the moment); throttling, a busy capacity, a failed upload of the staging file or a dropped connection never switch loader (the run backs off for at most 6 minutes per table, then stops and a re-run resumes) |
| `--repo PATH`, `--allow-drift` | use a specific checkout of the source repo / accept a commit other than the pinned one |
| `--helper CMD...` | run another helper command against this host's state: `export`, `verify --answers FILE`, `agents` |
| `bash wipe.sh --plan` / `--purge-local` | list what would be deleted / afterwards also remove the private venv, cache and output |

Exit codes: `0` done, `2` bad config, `3` blocked (fix and re-run), `4` partial (re-run resumes), `5` wipe incomplete (re-run
retries), `6` another build/wipe is running.

## What it creates, and what it never touches

Every build runs in a **brand-new resource group that `build.sh` creates itself**. It refuses to use a group that already exists
(unless this kit created it earlier and is resuming), so that `wipe.sh` can delete the whole group and leave none behind.

| Object | Default name | Notes |
|---|---|---|
| Resource group | `rg-contoso-fabric-ontology` (eastus2) | created new, tagged `managedBy=contoso-bank-ontology-demo` and a unique `contosoRun=<id>`; **deleted whole by wipe** |
| Fabric capacity | `contosofabricf4` (**F4**, 4 capacity units) | ARM template `infra/fabric-capacity.json` (compiled from the Bicep), validated before it is created. The build checks that Azure and Fabric both report F4 and that the workspace runs on it |
| Workspace, lakehouse | `Contoso-Bank-Ontology-Demo`, `ContosoBankLH` | plain lakehouse (no schemas) with the tier's three layers: seed files under `Files/bronze/<release>/...`, and managed Delta tables `bronze_file_manifest`, `silver_*`, `dim_*` / `fact_*` / `bridge_*` / `ref_*` (core: 37 files and 63 tables; full: 44 files and 97 tables) |
| Semantic model | `ContosoBankModel` | Direct Lake, 39 measures |
| Ontology | `ContosoBankOntology` | 31 entities, 417 properties, 53 relationships, 23 advisory rules (core tier) |
| Graph model | `ContosoBankOntology_graph_<id>` | created by Fabric next to the ontology; the kit fills it (31 node types, 53 edge types) and removes it with the ontology |

Every object is written to an **ownership ledger** (`~/.local/state/contoso-bank-ontology-demo/inventory/ledger.json`) *before* it
is created. The resource group and the capacity are always owned; a workspace that already existed with the configured name is
adopted, and wipe then removes only what the kit created inside it and prints `KEEP` for the workspace. It never signs in or out
of `az`, never changes the subscription, and never deletes hostvm, its resource group or a repo.

## What lands in the lakehouse (bronze, silver, gold)

| Layer | Where | What | Built from | Used by |
|---|---|---|---|---|
| **bronze** | `Files/bronze/<release>/<repo path>` and the table `bronze_file_manifest` | the seed JSON files the silver tables read, uploaded verbatim (only JSON: never PDFs, images, SVGs or markdown), each checked against its manifest hash; the files that belong to no release go under `no-release` | the pinned source repo | lineage and audit |
| **silver** | tables `silver_*` and the catalogs `silver_entity_catalog`, `silver_attribute_catalog` | the repo's CDM application profile: money as int64 paise (`*Minor`), exact decimals as text, 16 official-name alias columns (7 of them `decimal(38,4)`, the only decimals in the lakehouse), BIAN lineage in the catalogs | bronze | gold, lineage and audit; **never bound to the ontology** |
| **gold** | tables `dim_*`, `fact_*`, `bridge_*`, `ref_*` | the star schema: money as `*_inr` (double) with a hidden `*_paise` (int64), no decimals | silver (and curated reference sources) | **the Direct Lake semantic model, the ontology and the graph bind here only** |

`build --tier smoke|core|full` writes, and the BUILD stage loads, only what the tier needs: its gold tables, exactly the silver tables their `silver:` lineage names
(plus the two catalogs, cut down to those tables) and the seed files those read. The load order is bronze, then silver, then gold, and a re-run loads again only the
layers that changed or miss a table. Every output folder (`out/bronze`, `out/silver`, `out/gold`) always matches its manifest: a smaller tier built over a larger
one removes what the larger one left. `python -m contoso_onto load --layers bronze,silver,gold --only <tables>` loads a part by hand. Lineage and the star: [docs/MEDALLION.md](docs/MEDALLION.md).

## What `wipe.sh` removes, and how hard

Order: Fabric content first (while the capacity is up; a paused capacity is resumed for this), then the capacity, then the group.

| Where | What happens | Soft delete? |
|---|---|---|
| Azure resource group | `az group delete` of the whole group, with anything else inside it (named in the log first). Only after its `contosoRun` tag is proven to match the ledger; a group that merely reuses the name is left alone | none exists for resource groups |
| Fabric capacity | deleted (verified gone) | none exists for Fabric capacities |
| Fabric items | every item in the workspace is deleted with `hardDelete=true` (dependants first: ontology, its graph model, semantic model, lakehouse), then the workspace recycle bin is emptied; the lakehouse takes its `Files/bronze` folder and all its bronze, silver and gold tables with it | skipped; OneLake keeps the bytes of hard-deleted items for 7 more days, but nobody can restore them |
| Fabric workspace | renamed to `zz-wiped-...` (so the name is free for the next build), then deleted | **Fabric has no API that permanently deletes a workspace**: the emptied shell stays in the tenant's workspace retention (7 days minimum) and then disappears. A tenant admin can remove it at once: Admin portal > OneLake catalog > Govern > Workspaces > Permanently delete |

So after `wipe.sh` there is no resource group, no capacity, no item and nothing in a recycle bin. The one thing Fabric keeps is an
empty, renamed workspace shell. If something could not be purged (for example Fabric refuses to delete an item) wipe says so, exits
`5`, and still deletes the capacity and the group so billing stops. The wipe ends with an **audit** that lists every Fabric capacity still
in the subscription (read only): an older F4 that this ledger did not create is named, so it cannot hide, but it is never deleted by the
script. The delegated capacity settings belong to the capacity and disappear with it, so the next build asks for them again (stage 2).

Settings, lowest to highest precedence: built-in defaults, `config/demo.config.json`, `~/.local/state/contoso-bank-ontology-demo/config/local.json`
(not in git), environment: `CONTOSO_CAPACITY_ADMINS` (comma separated UPNs; default is the signed-in user, who is also made workspace
Admin), `CONTOSO_CAPACITY_SKU`, `CONTOSO_LOCATION`, `CONTOSO_RESOURCE_GROUP`, `CONTOSO_CAPACITY_NAME`, `CONTOSO_WORKSPACE_NAME`,
`CONTOSO_TIER`, `CONTOSO_REPO`, `CONTOSO_STATE_DIR`, and for the stages `CONTOSO_GATE_WAIT_MIN`, `CONTOSO_GATE_POLL_SEC`, `CONTOSO_GATE_TIMEOUT_MIN`,
`CONTOSO_GOLDEN`. Workspace members go in `local.json` as `{"upn": "...", "role": "Contributor"}`. `CONTOSO_DESCRIPTIONS` (`full` or `compact`) is not a config-file setting: `--descriptions`
beats it, and it beats the mode an earlier fallback to `compact` left in the state.

**F4 is pinned.** The capacity size defaults to F4 and any other value is refused (exit `2`) unless you also set
`CONTOSO_ALLOW_OTHER_SKU=1`. `--preflight` prints the size, the build re-reads it from Azure and from Fabric after the capacity exists
and stops (exit `3`) if either says anything but the configured size, and `--diagnose` re-checks it later (and that the workspace runs on
it). A capacity Azure created a moment ago can take a few minutes to show up in Fabric: the build says so and waits up to 4 minutes.
The kit never resizes a capacity by itself; if someone resized it, the message says how to put it back
(`bash build.sh --helper capacity scale --sku F4 --execute`).

## Before the first build

* `az` is signed in on hostvm (the scripts reuse that context, they never sign in). The signed-in user becomes the capacity admin
  and a workspace Admin; for a service principal / managed identity login set `CONTOSO_CAPACITY_ADMINS` instead.
* Fabric tenant settings: users can create Fabric items. The two that gate the ontology and its agent (**Users can create Ontology (preview)
  items** and **Users can use Copilot and other features powered by Azure OpenAI**) can be overridden by the capacity admin on the capacity;
  stage 2 of the build asks for exactly that and waits (see [docs/MANUAL_STEPS.md](docs/MANUAL_STEPS.md)). Only a capacity outside the US
  and EU also needs the cross-geo Azure OpenAI processing setting (the default `eastus2` does not).
  `--preflight` reads the settings when you are a Fabric admin and says so when it cannot.
* Python 3.11+, about 1.5 GiB free disk on the first run (the private environment is about 0.4 GiB), network access to PyPI, GitHub,
  `prices.azure.com` and `api.fabric.microsoft.com`.
* Source data: if `~/contoso-retail-superapp4` is at the pinned commit it is read directly; otherwise the pinned commit is fetched
  (depth 1, by SHA) into the cache, using the checkout's own remote and `core.sshCommand`. **The checkout is never modified.**

## How the data is made AI-ready

The assessment ([docs/ASSESSMENT.md](docs/ASSESSMENT.md)) found that the seed is not ready for an ontology as-is. The gold layer
(`model/gold/*.yaml`, one declarative model) fixes that: money becomes `*_inr` (double, agent-facing) plus a hidden exact `*_paise`
(int64); rates become `*_pct` / hidden `*_bps`; timestamps become UTC instants plus an IST date column; arrays become child tables;
the Frontier and legacy generations of the same customer id are kept apart (`rhea` vs `rhea~legacy`) with a rule that defaults to the
Frontier release; restricted PII columns stay in the lakehouse and out of the ontology. 29 golden questions have answer keys
computed from the data, so the agents can be scored against facts, not impressions.

The agent pack (`~/.local/state/contoso-bank-ontology-demo/out/core/agents/`): data-agent instructions, data dictionary, voice profile
(speakable units, never-speak-in-full fields), MCP client configs, Foundry IQ knowledge source/base, grounding brief.

## Standards alignment (Microsoft CDM and BIAN)

Your repo carries its own banking model (`data/banking-model/v1`): a CDM application profile of 45 entities, Microsoft Financial Services
CDM references and a BIAN 13.0.0 mapping registry. The ontology is built from the repo's seed data, so it realises part of that profile
under the profile's own entity names. The alignment is made explicit rather than assumed:

* `model/standards.yaml` is generated from the repo's registry (`python -m contoso_onto standards --repo <checkout> --write`) and a test
  fails if it drifts from it. Which profile entity an ontology entity realises is `ONTOLOGY_ENTITY` in `contoso_onto/standards.py`.
* 19 profile entities map onto 18 ontology entities that carry the profile's names (Customer, Account, Card, Deposit, Loan, Transaction,
  Goal, Document, Product, ...; Consent holds the legacy and the Frontier consent rows). Each of those descriptions has a sentence such as
  *"Standards: BIAN 13.0.0 Customer Product and Service Directory (custom), Savings Account (custom); Microsoft CDM FHAccount (semantic
  reference only). Demo mapping, not certification."*, followed by the entity's star role, grain and lakehouse lineage; the data agent and
  the voice agent read it as part of the entity. The other 33 entities say they are Contoso extension entities with no CDM profile entity.
* Naming follows one standard, [docs/NAMING_STANDARD.md](docs/NAMING_STANDARD.md): entity names from the profile, lakehouse names
  `silver_` / `dim_` / `fact_` / `bridge_` / `ref_` plus the entity, official CDM and BIAN names as metadata only.
* What the registry does **not** claim stays unclaimed: 8 of the 45 profile entities cite a Microsoft CDM entity (semantic references
  only, not extensions of the official entity); BIAN attribute mappings are 278 custom, 79 provisional (kept marked, for example the
  recurring-deposit analogy on Term Deposit) and 217 unmapped. The agent-authority layer (registered agents, mandates, access grants,
  consents) and the evidence/planning entities have **no verified BIAN mapping** and say so.
* The repo states that its profile is a demo mapping and not Microsoft endorsement, BIAN certification or regulatory compliance. The
  data-agent instructions repeat that, so neither agent will call the ontology BIAN-compliant.

## What stays manual (portal only)

A Fabric administrator has to enable **Users can create ontology (preview) items** (and the Copilot / data agent settings) once, or the
capacity admin can override it on the capacity (Capacity settings > Delegated tenant settings; verified live): see
[docs/MANUAL_STEPS.md](docs/MANUAL_STEPS.md), which lists every manual step in order. After that: the first graph materialization (Manage graph > Continue; eligibility itself is automated), metrics (only created by "generate from semantic model"), synonyms and additional metadata (ontology agent),
creating the data agent on the ontology. Step by step in [docs/LIVE_CHECKLIST.md](docs/LIVE_CHECKLIST.md). Background research:
[docs/Fabric_IQ_Ontology_Field_Guide.md](docs/Fabric_IQ_Ontology_Field_Guide.md).

## Safety properties (tested)

* `build.sh` and `wipe.sh` are the explicit "go" commands; the helper's individual commands (`provision`, `load`, `deploy`, `capacity`,
  `agents`) are dry-run unless you add `--execute`, and `--preflight`, `--diagnose` and `wipe.sh --plan` change nothing.
* The ARM template is validated (ARM + Azure Policy) before the capacity is created; a denial stops the run with nothing billable.
* A build only ever uses a resource group it created: an existing group blocks the build (exit `3`) and the preflight says why. Wipe
  deletes a group only when its run tag matches the ledger; a same-named group that is not the one the kit created is never touched.
* Resumable: a failure after the capacity exists exits `4`; everything created so far is in the ledger; re-run continues.
* Portal edits are never overwritten silently: the live definition is hashed right after each deploy; if it changes later,
  `build.sh` leaves it alone until you pass `--force-replace` (backup first). Items this kit did not create are never replaced unless forced.
* The first deployment proves the pipeline with the 12-entity smoke tier before growing to the target tier.
* One run at a time (lock); logs are private and the newest 30 are kept; no secrets are written to the repo, the ledger or the logs.
* `scripts/lib.sh` refuses to run under `bash -x`, requires Linux (override: `CONTOSO_ALLOW_NON_LINUX=1`), and blocks when free disk is low.

## Honest limits

* **Not yet exercised against a live Fabric tenant.** Syntax follows the official WindFarm ontology sample; the REST calls are
  tested against an in-process mock. The smoke-first deployment, `--preflight`, and exit code `4` resumption exist because the first live
  run is where surprises will show.
* **The layered load has only run against the mock.** Seed files in OneLake under `Files/bronze`, `decimal(38,4)` silver columns through the Load Table
  API (and the delta-rs per-table fallback for a table it refuses), silver column names with a leading underscore or in camelCase on the SQL analytics endpoint,
  the refresh of that endpoint (a preview API; the ontology, not the semantic model, reads the tables through it) and a load of 63 (core) to 97 (full) tables have not
  met a live tenant yet. [docs/LIVE_CHECKLIST.md](docs/LIVE_CHECKLIST.md) step 1a lists what the first live run must check, with what to do when it fails.
* A REST-created ontology may be ineligible for the graph until a portal seed supplies its partition annotations (checklist step 3).
* Ontology rules are advisory (not executed); enforce limits in the gateway. Metrics are copies of semantic-model DAX and are not auto-refreshed.
* The ontology MCP endpoint does not accept service principals; use a user sign-in. The Foundry IQ `fabricOntology` knowledge source is
  a preview API (`2026-08-01-preview`): field names must be confirmed before the first PUT (`--helper agents` shows the calls first).
* F4 is what this kit builds on and is enough for this data. Microsoft suggests more headroom (F8) only for heavy ontology-agent generation in
  the portal; if you want that, scale deliberately (`bash build.sh --helper capacity scale --sku F8 --execute`) and scale back afterwards, since
  `build.sh` refuses a capacity that is not the configured size.
* "No soft delete" is complete in Azure and for Fabric items, but not for the Fabric workspace shell (see "What `wipe.sh` removes"): that is
  a Fabric platform limit, not something a script can change. Do not lower the tenant's workspace or item retention settings for this:
  they are tenant-wide and affect everybody's content.

## Development

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
python -m pytest                      # 200+ tests; repo-backed ones need CONTOSO_REPO=<checkout at the pinned commit>
python -m contoso_onto --help         # the helper behind build.sh / wipe.sh (assess, build, plan, provision, deploy, verify, ...)
```

Layout: `build.sh`, `wipe.sh`, `scripts/` (lib, hostvm) | `contoso_onto/` (helper) | `model/` (declarative model) | `infra/` (Bicep + ARM) |
`config/` | `docs/` | `tests/` (includes a Fabric REST mock and the official TMDL sample as a fixture).
