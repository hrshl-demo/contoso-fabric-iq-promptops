# Live checklist: from first build to a scored agent

Work top to bottom. Every command runs on hostvm in `~/contoso-bank-ontology-demo`. The capacity bills while it runs: pause it
(`bash build.sh --pause`) whenever you stop for more than a short break.

## 0. Preconditions

```bash
bash build.sh --preflight
```

Expect `PREFLIGHT OK`, including `[PASS] capacity sku: F4 (4 capacity units)`. Anything marked `[FAIL]` blocks the build; `[WARN]` is worth reading. Typical fixes:

| Preflight says | Fix |
|---|---|
| az is not signed in | sign in on hostvm yourself (the scripts never do) |
| tenant setting ... DISABLED | Fabric Admin portal: enable Fabric items for the user, and Ontology (preview) |
| tenant settings not readable | you are not a Fabric admin: confirm the settings by hand (Fabric items, Ontology, Data agent, cross-geo AI only if the capacity is outside the US and EU) |
| capacity name is not available | another subscription owns the name: `CONTOSO_CAPACITY_NAME=...` |
| capacity sku 'F8' is not the F4 this demo is built for | something sets `CONTOSO_CAPACITY_SKU` (environment or `~/.local/state/contoso-bank-ontology-demo/config/local.json`): remove it to get F4 |
| capacity ... is F8, but this build is configured for F4 | the capacity was resized after it was built: `bash build.sh --helper capacity scale --sku F4 --execute` |
| resource group ... already exists and this kit did not create it | build only uses a brand-new group (so wipe can delete it whole): `CONTOSO_RESOURCE_GROUP=<new name>`, or delete that group yourself |
| could not fetch the pinned commit | `CONTOSO_REPO=<checkout at 860058471>` or fix the checkout's deploy key |
| only N MiB free | free disk (`~/.cache`) or `bash wipe.sh --purge-local` after an old run |

## 1. Build

```bash
bash build.sh              # the whole cycle; add --fresh to wipe an earlier build first
```

Stages (each one idempotent, so a re-run resumes exactly where the last one stopped):

| Stage | What happens | Typical time on an F4 |
|---|---|---|
| 1 BUILD | the data layers for the tier, a brand-new resource group + F4, workspace, lakehouse, then bronze (the seed files and the manifest table), silver and gold loaded in that order (a layer that did not change and whose tables are in the lakehouse is skipped), Direct Lake semantic model | about 6 min on the core tier (live, 17 Oct 2026: bronze 28 s, silver 1 min 40 s, gold 1 min 48 s), see 1a |
| 2 APPROVE | tries to create the ontology; if Fabric says `403 FeatureNotAvailable` it **asks a capacity admin** for two delegated settings (direct link printed, also in `out/ADMIN_APPROVAL.md`), **waits 15 min**, then **polls every minute** and carries on by itself | 10 to 15 min after the admin clicks |
| 3 DEPLOY | the graph over REST: the definition is generated from the specification, written to the graph model Fabric created next to the ontology, refreshed, and checked (31 node types, 53 edge types on core) | about 3 min |
| 4 TEST | verify the ontology, check the MCP endpoint, then the ontology agent answers its golden questions (`--golden smoke` = 5, about 4 min; `full` = every MCP question; `none`) | about 4 min |

`--until build|approve|deploy|test` stops after a stage on purpose. `--no-gate-wait` stops at the gate with exit 3 instead of waiting.
`--no-graph` leaves Configure graph to the portal. `--strict-golden` makes a failed golden question fail the build (default: reported only).
Exit code `0` = done. `4` = stopped half way; read the last lines, fix, and run `bash build.sh` again (it resumes). `3` = blocked by something
outside the kit (for example the admin step timed out). If the Load Table call itself is refused (HTTP 400, 415 or 422, or its operation ends Failed with an error
code that is not about the moment) the run writes that table, and only that one, with the delta-rs writer by itself: every `build.sh` run installs `deltalake` into its
own Python environment up front (about 100 MB, one pinned line of `requirements-optional.txt`; pip gets one retry and a 10 s timeout, so an unreachable PyPI costs seconds),
so no re-run is needed. Throttling (429), a busy or unreachable service (5xx, a dropped connection or a timeout), a capacity that is short of room, a cancelled operation,
a failed upload of the staging file to OneLake, a table list that cannot be read and a refused sign-in (401/403) never switch the loader: the run waits and tries again with
a growing back-off that honours `Retry-After` (no table is retried for more than 6 minutes in all; when as many loads in a row fail for such a reason as there are workers, the
queued loads are cancelled), and if that does not help it stops with exit `4`, says the cause is the moment and not the data, and a re-run resumes. `--loader delta` writes
every table with delta-rs. Ctrl+C cancels the queued loads and says `interrupted: waiting for N running loads to finish (up to 15 min); queued loads were cancelled`: the loads
that are already running finish first.

**The lakehouse holds three layers, and a tier builds only its own.** Bronze: the seed JSON files the silver tables read, verbatim, under
`Files/bronze/<release>/...`, plus the table `bronze_file_manifest`. Silver: `silver_*` tables on the CDM profile (with the official-name alias
columns) plus `silver_entity_catalog` and `silver_attribute_catalog`. Gold: `dim_*`, `fact_*`, `bridge_*`, `ref_*`. The ontology and the semantic
model bind to gold only. A tier loads its own gold tables, the silver tables their lineage names and the seed files those read: smoke 18 bronze files,
15 silver tables, 13 gold tables (29 tables with the bronze manifest); core 37 bronze files, 30 silver tables, 32 gold tables (63 tables); full 44 bronze
files, 45 silver tables, 51 gold tables (97 tables). `bash build.sh --preflight` prints these sizes per tier. The load goes bronze, then silver, then gold,
each layer side by side inside, and stops before the next layer if one fails (a failure shows at the first layer that has it). A re-run loads again only
the layers whose data changed (a change in bronze or silver also changes the layers built from them) or that miss a table in the lakehouse; `--reload`
loads all three. The state is in `<state>/out/.state.json` (`loadedLayers`, one fingerprint per finished layer).

The log shows `STAGE n/4`, then `capacity contosofabricf4: F4 Active` (Azure's answer) and `workspace ... runs on capacity contosofabricf4 (F4,
Active), as Fabric reports it`. Right after the capacity is created you may see `capacity ... is not listed by Fabric yet; waiting`: that is
normal and lasts a few minutes (the build waits up to 4; if Fabric still does not list it, the build stops with exit `4` and a re-run resumes).

If the **ontology creation is rejected** for a reason other than the gate (HTTP 4xx with a validation message), keep the message from
`~/.local/state/contoso-bank-ontology-demo/runs/last-error.txt` and the build log; `--tier smoke` builds the 12-entity set to tell a syntax
problem from one that only the larger tiers have. An **HTTP 400 on the definition** of the ontology or the semantic model is handled by the build itself
while the entity descriptions are `full` (the default): it builds every definition again in the `compact` form (business meaning and standards sentence, each
line within 500 characters), retries once, remembers the choice (`descriptionsMode` in `<state>/out/.state.json`) and prints `DESCRIPTION FALLBACK` lines with
the service's own error text; a re-run stays compact until `bash build.sh --descriptions full` (or `CONTOSO_DESCRIPTIONS=full`) asks for the full text again.
If the compact retry is refused too, the build stops with the original error and the retry's attached (exit 4). Item 10 of section 1a says what to record.

## 1a. First live run of the layered load (17 Oct 2026): what was checked, and what is still unverified

**Result of the first live run, 17 Oct 2026 (core tier, F4, East US 2, kit commit `ecbfdf5`).** Items 1 to 10 below were written before the run; this is
what the run showed. BUILD, APPROVE, DEPLOY and TEST all completed; the environment was then wiped (`wipe.sh` ended `VERIFIED`).

| Item | Result |
|---|---|
| 1 decimal(38,4) | Loaded by the Load Table API with no refusal and no delta-rs fallback (`silver_account`, `silver_card`, `silver_deposit`, `silver_loan` are in the 30 silver tables). Not inspected: the column types in the lakehouse explorer and `SELECT TOP 5` through the SQL endpoint. |
| 2 underscore and camelCase names | Loaded with no refusal. Not queried through the SQL endpoint (the ontology and the semantic model bind to gold only). |
| 3 bronze upload | 37 files, 730,996 bytes, under `Files/bronze/`, each read back and checked against the manifest. |
| 4 tables registered, endpoint sync | Core: 63 tables registered (bronze 1, silver 30, gold 32); the endpoint refresh answered at once after each layer. The full tier (97) was not run. |
| 5 load time (4 workers) | Bronze 28 s including the upload, silver 1 min 40 s, gold 1 min 48 s. BUILD to semantic model about 6 min. |
| 6 layer order | A layer that cannot be verified stops the run before the next one, naming the layer and table: it did, once (see "Found live" below), and a re-run resumed. |
| 7 gold-only bindings | The ontology validated (31 of 31 entities) and the graph was built from it. The bindings were not inspected in the portal. |
| 8 graph with renamed entities | Refreshed and queryable: 31 node types, 53 edge types. |
| 9 golden questions | Smoke 5 of 5 (42 to 57 s). Full (`--golden full`, 26 questions: G12 is `data_agent` only): **23 of 26**. Misses: G04 (lacked `revoked`, `2026-09-10`), G06 (lacked `2026-09-30`; it passed in the smoke run), G24 (lacked `blocked`, `contacted`, `reversed`); G27 is judged manually (counted as a pass). Median 64 s, range 20 to 359 s (G25: 359 s, two tries), two questions at a time. v1: 20 of 27; the question sets and the keys differ, so this is not a controlled comparison. The answer text of a miss is not logged (only the missing terms): ask the failing question on its own to see it. |
| 10 description length | Accepted: the ontology took entity description lines of up to 736 characters, the semantic model up to 725. The compact fallback never fired. Not checked: how the portal shows a long description. |

**Found live (fixed in `ecbfdf5`).** The Load Table operation reported success a few seconds before the table showed in the lakehouse table list, and the
one-shot check stopped the first run at the bronze layer. Every layer now waits up to `register_wait_s` (180 s) for its tables, whichever loader wrote them.

**Also measured.** After a capacity admin applied the two delegated settings, the ontology could be created within about 7 minutes (the build settles 15
minutes, then polls every minute, so the wait could be shorter). The F4 ran 2 h 11 min in all (about USD 1.6 at the 0.72 USD per hour the build prices),
including an idle wait for the admin step. A re-run after a stop resumes (`git pull` on the VM, then `bash build.sh`).

Before this run everything about the layers had run only against the in-process mock (and the delta-rs writer on local disk). The text of items 1 to 10
is kept as written, for the next run on a different tier or region. v1 had been live-proven for the 51 gold tables: the OneLake create/append/flush calls,
the Load Table API (with its `OperationHasNoResult` quirk), the SQL endpoint, the semantic model, the ontology, the graph and the agent.

1. **The Load Table API with a Parquet file that has `decimal(38,4)` columns** (`silver_account`, `silver_card`, `silver_loan`: the official-name
   alias columns of the CDM crosswalk; `SELECT * FROM silver_attribute_catalog WHERE origin = 'cdm_official' AND cdm_type = 'currency'` lists them,
   and they are the only decimals in the lakehouse; gold has none because decimals come back null from Fabric Graph).
   *Check:* the BUILD log says `silver: N tables registered` and has no `the Load Table API refused` line; in the portal the lakehouse explorer
   shows those columns as `decimal(38,4)` and `SELECT TOP 5 * FROM silver_account` through the SQL endpoint returns their values.
   *If it fails:* the Load Table call being refused for a silver table (HTTP 400, 415 or 422, or an operation that ends Failed with an error code that is not about the
   moment) makes the run write that table, and only that one, with
   delta-rs (`the Load Table API refused N of the M silver tables (...): ...; writing only those with delta-rs, the other tables stay as the API loaded them`);
   `bash build.sh` installed `deltalake` (about 100 MB) in its own environment at the start of the build, so no re-run is needed. decimal128 columns keep
   their precision and scale through that writer (tested on local disk for every silver table). The delta-rs path is itself unverified live: after a fallback
   open the table in the lakehouse explorer, check its columns and that it is listed within a minute or two. If the delta-rs write fails too, or the columns
   show up as another type, keep `<state>/runs/last-error.txt`: the fix belongs to the silver specification (`model/cdm_crosswalk.yaml`), not to the loader.
   Throttling, a busy service, a dropped connection, a capacity that is short of room, a cancelled operation or a failed upload of the staging file (the error names the file)
   are not a refusal and never start delta-rs; they stop the run (exit 4) after the back-off, and a re-run resumes. Keep the `errorCode` of any failed operation here: the
   kit treats a code with `InternalError`, `Unavailable`, `Timeout`, `Cancel`, `Capacity`, `Throttl`, `TooManyRequests` or `ServerBusy` in it as the moment, and every other code as the file.
2. **Silver column names that start with an underscore or use camelCase**, on the SQL analytics endpoint and in the lakehouse table preview: the
   technical columns `_release_id`, `_source_file`, `_source_row`, `_owner_id` and business columns such as `postedBalanceMinor` and `xAgeYears`
   have never gone through either.
   *Check:* in the SQL analytics endpoint run `SELECT TOP 5 _release_id, postedBalanceMinor FROM silver_account;` and
   `SELECT TOP 5 _owner_id, xAgeYears FROM silver_customer;`, open both tables in the table preview (Lakehouse > Tables > the table > Preview) and read
   the headers; also run the first query with another letter case (`_RELEASE_ID, POSTEDBALANCEMINOR`) and note whether the endpoint is case sensitive.
   *If the endpoint or the preview rejects or mangles a name:* the fix is a rename in the silver specification (`model/silver/*.yaml` decides every
   physical column name), not in the kit's load code, which writes the Parquet column names as they are. Stop and report which names failed.
3. **The OneLake upload path and size of the bronze files.** Each seed file goes to `Files/bronze/<release>/<repo-relative path>`, for example
   `Files/bronze/<release>/data/releases/<release>/<file>.json`; the file that belongs to no release goes to
   `Files/bronze/no-release/data/relationship/products.json`. Only the JSON files the bronze manifest names are uploaded, never PDFs, images, SVGs or
   markdown. Each upload is read back and compared (size and sha256) with `out/bronze/manifest.json`. What is new against v1 is the folder layout, the
   read-back (`GET` of the file) and the folder listing (`GET <workspace id>?resource=filesystem&directory=<lakehouse id>/Files/bronze&recursive=true`).
   *Check:* the log says `uploaded N bronze files (...) to Files/bronze/; each read back and checked against the manifest`, and the line that closes the layer reads
   `bronze: 1 table registered, N seed files uploaded (N read back and checked) in XmYYs` with the same N twice; `bash build.sh --diagnose` prints `[PASS] layer bronze: 1/1 tables registered, N/N files in OneLake`; in
   the portal Lakehouse > Files > bronze has one folder per release (and `no-release`); download one file and compare its `sha256sum` with `files[].sha256`
   in `out/bronze/manifest.json`.
   *If it fails:* `OneLake create/append/flush <path>: <status>` means OneLake refused the upload (the signed-in user needs a workspace role that can
   write; the token is for `https://storage.azure.com/.default`); `OneLake holds X bytes at <path>, not the Y bytes` is a truncated upload (re-run: the
   file is created again); `could not read <path> back` for every file means the read-back is not permitted but the upload worked (compare the sizes in
   the portal); `OneLake did not list Files/bronze` or `files planned (OneLake did not list them)` means only the file count could not be checked.
4. **About 100 tables registered, and the SQL analytics endpoint metadata sync with that many tables** (core 63, full 97; v1 had 51). The kit asks the
   endpoint to refresh its metadata once per layer, after that layer's tables are verified as registered (`POST .../sqlEndpoints/<id>/refreshMetadata`, a preview API whose exact URL, `preview` parameter and result
   shape are not verified live; a refusal, throttling or a timeout is logged as `metadata refresh skipped` and never stops the load). After the **last** layer it
   follows that refresh (the long-running operation named by the `Location` header) for up to 10 minutes, logging `SQL endpoint sync: the refresh is still running
   after 1m00s` about once a minute and, when it ends, `the refresh finished after XmYYs; it reports N tables` (with the tables it lists as failed, or planned
   tables it does not list yet). It never fails the stage: `ended Failed`, `could not follow the refresh` and `still running after 10m00s; carrying on` are only
   reported. **It is the ontology that reads the lakehouse's tables through this endpoint** (`Sql.Database(...)` and the `ONT_Sql*` annotations of its entity
   bindings); the semantic model reads OneLake directly (`AzureStorage.DataLake`), so the endpoint's lag does not touch it. With about 100 tables, gold loaded last
   and the refresh asynchronous, the first ontology create can come minutes after the last load.
   *Check:* the log ends the load with `load done: N tables and M seed files in the lakehouse`; in the portal open the lakehouse's SQL analytics endpoint
   and run `SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo';`: it must equal N. Note how long after the last load it takes to
   get there, the `SQL endpoint sync:` lines (how long the refresh ran and what it reported) and whether the ontology was created at the first try.
   *If the ontology create or update is refused (HTTP 400, or a "not found" about a table or data binding) within 10 minutes of the last load:* the error output
   adds `note: the lakehouse tables were loaded N min N s ago. The ontology reads them through the SQL analytics endpoint ... probably still syncing`: wait a few
   minutes (or use the endpoint's metadata refresh action in the portal) and run `bash build.sh` again; it resumes at the ontology and does not load the tables
   again (`lastLoadAt` in `<state>/out/.state.json` is the time the note counts from). Keep the exact error text here: it is the first time the lag is seen, and it is
   not known whether the service words it as a 400 or as a "not found". If the same error comes back 10 or more minutes after the endpoint lists every table, it
   is not the endpoint: it is the definition. Keep the refused call's message for the refresh as well: it is a preview API.
5. **Load time for about 100 tables with 4 workers against v1's 51.** Four tables load at a time inside a layer; one that fails while the others run is tried
   again on its own, one at a time, and a throttled (`429`), busy (`5xx`) or conflicting (`409`) answer is waited out with a growing back-off (1-2 s, 2-4 s, 4-8 s ...)
   that never undercuts the service's `Retry-After`; a dropped connection or a timeout of a request gets the same back-off. One table (or file) is retried for 6 minutes at
   most, whatever the number of tries, and when as many loads in a row fail for a reason that is not about the tables as there are workers the queued ones are cancelled and
   the run stops with exit 4 (`N loads in a row failed for a reason that is not about the tables`) instead of spending the whole ladder on every table. v1 loaded the 51 gold tables in about 4 minutes (the figure the load-skip message was written around; replace it with
   your own v1 log).
   *Check:* the log prints the time of every layer (`silver: N tables registered ... in XmYYs`). Fill in:

   | layer | tables | seconds | tables retried on their own |
   |---|---|---|---|
   | bronze (files + manifest table) | | | |
   | silver | | | |
   | gold | | | |
   | total, and the v1 figure | | | |

   *If it is slow or throttled* (`429 from POST ...; retrying in Ns`, `ItemOperationInProgress`, `trying again in Ns` or `retrying on its own` lines): note how many and how
   long, build the smaller tier first (`--tier smoke` is 29 tables, `--tier core` 63), or simply re-run: finished layers are not loaded again. Throttling never makes
   the run write tables another way (delta-rs); if it does not clear the run stops with exit 4 and says the cause is the moment, not the data.
6. **The silver-before-gold bisect.** The layers load bronze, then silver, then gold, and a failure stops the run before the next layer: the log says
   `ERROR the silver layer stopped at table <name>: ...` and `gold was not started`, and the exit code is 4.
   *Check, on purpose, once:* run the helper for one layer, `bash build.sh --helper load --execute --layers silver --only <table>` (add `--loader
   delta` for the delta-rs writer); it loads exactly that table, prints the layer's `missingAfterLoad`, and exits 1 and names the table if it fails.
   *Procedure when a build stops:* read the table name; load just that table with the helper (above); when the layer loads, run `bash build.sh` again:
   finished layers are not loaded again. `bash build.sh --diagnose` lists the tables and files per layer and flags a missing table by layer. Gold does
   not read the silver tables of the lakehouse (it was built from them offline), so `--layers gold` also works while a silver table is stuck: only
   lineage and audit lack that table.
7. **The ontology and the semantic model bind only to gold physical names** (`dim_*`, `fact_*`, `bridge_*`, `ref_*`), never to a silver or bronze table.
   *Check:* in the portal open one entity of `ContosoBankOntology` and read its data binding (the table behind it, for example `dim_account`); then
   `bash build.sh --helper export --name ContosoBankOntology` and
   `grep -rhoE "entityName: .*" ~/.local/state/contoso-bank-ontology-demo/out/seed/ontology | sort -u` must list gold names only; the semantic model's tables
   and the graph's data sources point at the same names. (A mock-backed test checks the definitions the kit generates, not what Fabric stores.)
   *If it fails:* do not repair it in the portal. The generator is wrong (`contoso_onto/oac.py`, or a `physical:` name in `model/gold/*.yaml`); stop and
   report the entity.
8. **The graph refresh with the renamed entities.** The medallion work renamed six ontology entities and seven gold tables (the list is in
   `docs/NAMING_STANDARD.md`).
   *Check:* stage 3 logs `graph: refreshed and queryable (31 node types, 53 edge types)` on core; for each renamed entity run
   `MATCH (n:<Entity>) RETURN count(n)` in the graph and compare it with the row count of its gold table in `out/manifest.json`.
   *If it fails:* the generator checks every data-source path against the table's physical name before it writes the graph and the node and edge types
   after the refresh; read the refresh's `failureReason` in the log and run `bash build.sh` again (stage 3 leaves an unchanged, completed graph alone and
   applies it again otherwise). Do not edit the graph in the portal.
9. **The golden questions through MCP after the first successful build.** v1 reference: **20 of 27** passed strictly on the first full run (16 Oct 2026,
   East US 2): five answer-key wording misses (G03, G08, G13, G16, G24) and two real agent gaps (G25, G28).
   *Check:* `bash build.sh --until test --golden full` (about 25 minutes, two questions at a time), then compare question by question with v1 and write
   the new score here.
   *If the score drops:* ask the failing question once more on its own (about a third of first attempts time out when four run in parallel), then compare
   the answer with `out/eval/golden_expected.json`. A drop on a renamed entity points at the agent instructions or the data dictionary, not at the data:
   fix those, never the answer key (the keys are computed from the data).
10. **The longest entity description Fabric accepts.** Microsoft's metadata how-to asks for "concise but informative (one to three sentences)" and
    documents no limit. The longest entity description line ever accepted live is 509 characters (v1's Product: 497 plus the ontology's `[Catalogue] ` tag).
    The default `full` descriptions add the architecture clause (star role, grain, lakehouse lineage, official-name aliases) and run up to 736 characters on
    core (the semantic model's table lines up to 725; the ontology's carry the domain tag). Live, 17 Oct 2026: Fabric accepted both (ontology up to 736
    characters, semantic model up to 725) and the compact fallback never fired.
    *Check:* the BUILD log says `descriptions: full; Fabric accepted the semantic model with entity description lines of up to N characters` and, after the
    APPROVE stage, the same for the ontology; the closing summary repeats both and `<state>/out/.state.json` keeps them (`descriptionsAccepted`). In the
    portal open one long entity (Account is the longest) and check that its description is shown whole and readable (the ontology agent reads the first
    200 characters, so the business meaning comes first). Fill in: longest accepted ontology line ___ characters, semantic model line ___ characters.
    *If the fallback fired* (`DESCRIPTION FALLBACK: Fabric refused the ontology definition with HTTP 400: ...` in the log, `## Descriptions: COMPACT` in
    `NEXT_STEPS.md`, `descriptionsFallback` in the state file): paste the service error text here, with the item that was refused and the characters it
    reported; the build has already retried once with `compact` descriptions and Fabric accepted them. Paste: ______. The service's wording was never seen
    before this run (the tests rehearse a refusal with an invented body of the same shape), so the real text is the evidence for choosing a limit; to try
    the full text again later run `bash build.sh --descriptions full`.
    *If the compact retry is refused too* the build exits 4 with the original error and the retry's attached (also in `<state>/runs/last-error.txt`): that
    is not a length problem; keep both texts here and stop.

## 2. Check the result

```bash
bash build.sh --diagnose
```

Expect: the capacity and `capacity sku ... F4`, the workspace with `workspace capacity ... runs on contosofabricf4 (F4, Active)`, the lakehouse with
its tables per layer (`lakehouse ...: 63 tables; planned: 37 bronze files, 30 silver tables, 32 gold tables` on core, then one `layer bronze`, `layer silver` and
`layer gold` line each with `registered` counts, the bronze files found in OneLake, and any missing table named by layer), semantic model, ontology `entities 31/31 relationships 53 rules 23`, `ontology drift:
unchanged since the last deploy`, and an MCP endpoint answering. Open the workspace (`https://app.fabric.microsoft.com/groups/<id>`,
the id is in `out/NEXT_STEPS.md`) and look at the Direct Lake model: it must be framed (refresh from the portal if the build
logged a warning). In the portal the workspace settings page (License info) shows the same F4 capacity.

## 3. Graph: built by the kit (stage 3); eligibility comes from the annotations

Live findings (16 Oct 2026, F4):

* An ontology created over REST without the eight `ONT_*` partition annotations shows every entity as **Ineligible** in Manage graph (the
  data-source kind cannot be identified), even though the definition is valid. Redeploying with only those annotations (the `Sql.Database`
  expression and `schemaName: dbo` left as they were) turned all 31 entities **Eligible**. The kit therefore writes the annotations by default
  (`--no-ont-annotations` turns them off).
* The graph model that Fabric creates next to the ontology starts empty, and a REST refresh of it is cancelled until it has a definition.
  Writing the definition over REST (`POST .../graphModels/{id}/updateDefinition`, the same five files the portal's Configure graph wizard
  writes) works, makes Fabric start a refresh by itself (a second refresh request is answered `Deduped`), and after about 2 minutes the graph
  answers GQL (`MATCH (c:Customer) RETURN count(c)` returned 38). Stage 3 does exactly this from the specification, so **no portal step is
  needed**; it is checked afterwards (31 node types, 53 edge types) and a re-run leaves an unchanged graph alone.

If stage 3 reports a graph error, or you built with `--no-graph`:

1. Open `ContosoBankOntology` > **Manage graph**: all entities should say **Eligible**.
2. Select all entities and relationships > **Continue** and finish the wizard. The graph materializes (a few minutes on an F4).
3. Later refreshes can use the REST job (`POST .../graphModels/{id}/jobs/refreshGraph/instances`) or a schedule in the portal. The graph
   does not refresh by itself when the lakehouse tables change.

If the entities are still ineligible (for example a build with `--no-ont-annotations`), seed from the portal instead:

1. In the portal create a second ontology item by generating one from the `ContosoBankModel` semantic model; name it `Seed`.
2. Export its definition and rebuild from it. This keeps the portal's annotations and any metrics it generated:
   ```bash
   bash build.sh --helper export --name Seed
   bash build.sh --seed ~/.local/state/contoso-bank-ontology-demo/out/seed/ontology
   ```
   (`--seed` replaces the live ontology after backing it up under `out/backup/`.)
3. Delete the `Seed` item in the portal when you are done.

## 4. Metrics, synonyms, metadata (portal only)

* Metrics: generate them from the semantic model in the portal (that is the only way they arrive); they are copies of the DAX
  measures and are not refreshed automatically.
* Synonyms, additional metadata, resource links: let the ontology agent propose them, review, accept. Banking terms worth adding:
  "balance" / "available balance" / "posted balance", "EMI", "mandate", "earmark".
* Then export once so the work is not lost, and so later builds keep it:
  `bash build.sh --helper export --name ContosoBankOntology`. `build.sh` will not overwrite these portal edits (drift protection);
  `--force-replace` is the explicit override and backs up first.

**Next experiment (unverified; after the first successful live run, not part of it).** Microsoft's ontology-definition REST documentation lets an entity carry
`synonym <value>` lines and `annotation <name> = <value>` lines (the portal's *Synonyms* and *Additional metadata* key-value pairs), one line per entry,
the document's own example putting `synonym dashboard` at the END of an entity block, after its child blocks. The idea: behind an off-by-default flag
(`--metadata annotations`) write the spec's synonyms as `synonym` lines and the architecture facts (Standards, Star role, Grain, Lakehouse lineage, CDM profile
entity or Contoso extension, Official CDM name aliases; one key per entity) as `annotation` lines after the properties, and shrink every `///` description to
its business meaning; the kit's TMDL validator would accept the two line kinds, and a test would keep the default output byte-identical. Nothing of this is
implemented and nothing has been sent to a tenant: it waits for the live evidence of item 10 above (what Fabric accepts, what the portal shows) first.

## 5. The ontology agent, golden questions, data agent

The TEST stage of the build already asked the ontology agent its smoke questions through MCP. The agent needs the Copilot delegated setting
(stage 2 asks for it) and answers from the graph: on 16 Oct 2026 an F4 in East US 2 answered **20 of 27 golden questions** exactly and the
rest were wording or answer-key strictness (right answer, different words) except two real gaps (a net-position question missed assets held
in a different entity; a multi-hop SOP question stopped one hop early). One answer takes 30 to 90 s, and roughly a third of first attempts
time out when 4 are asked in parallel, so the kit asks 2 at a time and retries once; one at a time was clean. To ask the whole set:
`bash build.sh --until test --golden full`. A capacity outside the US and EU also needs the cross-geo Azure OpenAI setting, or the agent
answers `Something went wrong` / 403 (seen in South India).

Create the data agent on the ontology in the portal (a data agent bound to an ontology is a portal step). Paste
`~/.local/state/contoso-bank-ontology-demo/out/core/agents/data_agent/instructions.md` as the instructions and attach
`dictionary/data_dictionary.md`. Ask the 29 golden questions (`out/eval/golden_expected.json`), save the answers as
`{"G01": "...", "G02": "..."}` and score them:

```bash
bash build.sh --helper verify --answers answers.json
```

Failures name the missing numbers/strings. Fix the instructions or the ontology metadata, not the answer key: the keys are computed
from the data.

## 6. Conversational AI, voice, Foundry IQ

* MCP: `out/core/agents/mcp/mcp_configs.json` has the client configs for the ontology MCP endpoint (VS Code, Copilot Studio). The
  endpoint needs a user sign-in; it does not accept service principals.
* Voice: `out/core/agents/voice/voice_profile.json` lists, per property, how it is spoken (lakh/crore for rupees, never read masked
  numbers in full).
* Foundry IQ: `bash build.sh --helper agents` prints the Azure AI Search calls for the `fabricOntology` knowledge source and base.
  The API is a preview: confirm the field names against the current reference before adding `--execute`.

## 7. Stop paying, leave nothing behind

```bash
bash build.sh --pause      # keep everything, stop compute billing (resume with --resume)
bash wipe.sh               # hard-delete everything build.sh created, including the whole resource group, then audit the subscription
bash wipe.sh --purge-local # afterwards, also remove the private Python environment and caches
bash build.sh --fresh      # a clean cycle: the wipe above first, then BUILD, APPROVE, DEPLOY, TEST
```

`wipe.sh` ends with `VERIFIED: ... no resource group, no capacity, no Fabric items, no recycle bin` and an `audit:` line that lists every
Fabric capacity still in the subscription (read only; an older F4 that is not in this ledger is named, never deleted by the script). Azure has
no soft delete for
resource groups or Fabric capacities, and Fabric items are deleted with `hardDelete`. The one thing Fabric keeps is the emptied,
renamed workspace shell (`zz-wiped-...`) in the tenant's workspace retention (7 days minimum): there is no API that purges a
workspace. To remove it at once a tenant admin uses Admin portal > OneLake catalog > Govern > Workspaces > select the deleted
workspace > Permanently delete. To check the Azure side by hand: `az group exists -n rg-contoso-fabric-ontology` must print `false`, and
`az resource list --resource-type Microsoft.Fabric/capacities -o table` must not list `contosofabricf4`.

From the laptop, `bash scripts/hostvm.sh down` deallocates hostvm itself.
