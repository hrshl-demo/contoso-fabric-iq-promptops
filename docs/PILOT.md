# Pilot: can the medallion live outside Fabric?

**Status: planned, not started.** Nothing in this page has been run. It needs the setup in [SETUP.md](SETUP.md) and your explicit go, because it runs the F4.

## The question

You asked whether a very small Databricks cluster, or else blob storage, could host the medallion (bronze, silver, gold) instead of Fabric's own lakehouse tables, and whether the ontology can sit on silver instead of gold.

The documents disagree on the one thing that decides it:

- Newer Learn pages (16 and 21 Oct) say an ontology can bind to lakehouses, mirrored databases, warehouses and "OneLake constructs such as shortcuts, views and materialized views", and that the graph takes "only delta tables from lakehouses or mirrored databases".
- Older pages (the 1 Oct troubleshooting page, and the Microsoft skills repo) say the ontology supports **managed** lakehouse tables only, not external tables shown through shortcuts.

The evidence is in [DECISIONS.md](DECISIONS.md) (D2). Only a test settles it, and the test is small.

## What the three options cost (East US 2, 3 Nov 2026)

| Option | Extra cost | Status |
|---|---|---|
| **Fabric-native managed tables** (today) | none | proven live three times; the fallback |
| **Blob or ADLS Gen2 with a OneLake shortcut** | storage for a few megabytes is cents a month | unproven for the ontology and graph: this pilot |
| **Databricks** | job compute about USD 0.11 to 0.15 for a 20 minute single-node build (jobs compute DBU USD 0.15 Standard or 0.30 Premium, plus the VM); an idle workspace has no compute fee, but secure connectivity adds a NAT gateway of about USD 33 a month while the workspace exists; Premium is needed for Unity Catalog and for mirroring; the free edition cannot do this | only worth trying if the shortcut path passes |

Databricks is not expensive to try; it is expensive to leave running. Delete the workspace the same day.

## What the pilot proves

For each spike, bind two silver entities (one customer-like, one account-like) in a **test** Gen 2 ontology in a **pilot workspace**, never the dev or prod ones:

1. The ontology item is created and the entity types load.
2. The entity counts equal the source row counts.
3. The graph is eligible, refreshes, and the node counts match.
4. Two graph queries return the expected rows.
5. The ontology agent answers two of the kit's golden questions (the ones that passed in every live run).
6. A Direct Lake semantic model over the same tables frames and queries.
7. A changed source row shows up after a refresh.

Tables are written as plain Delta: no column mapping, deletion vectors off. Silver has three known wrinkles to test on purpose: `decimal(38,4)` columns (null in the graph), both seed releases side by side (so the key is the unique `id`), and arrays kept as child tables.

## The spikes

| Spike | What | Extra cost | Time |
|---|---|---|---|
| **A** | A second lakehouse in the pilot workspace with a OneLake shortcut to two tables of the first. Tests "shortcut tables" with no storage account at all | none | about 30 minutes |
| **B** | An ADLS Gen2 storage account (Standard, locally redundant). The kit writes two Delta tables there (delta-rs, already an optional kit dependency); a OneLake shortcut points to them | storage, cents | about 45 minutes |
| **D** | Can the CI/CD app (service principal) create the ontology and graph items? Run the same steps as **A** with the app instead of you | none | about 20 minutes |
| **C** | Only if B passes and you want Databricks in the story: a Premium workspace, a Unity Catalog external location on the same storage, a job that writes the tables, a mirrored Azure Databricks catalog in Fabric (needs external data access, `EXTERNAL USE SCHEMA` and `SELECT`) | about USD 1 to 2 including the day's workspace | about 90 minutes |

The F4 runs only while a spike runs: about 2 to 3 hours of F4 time for A, B and D together, so **about USD 1.5 to 2.2 including storage** (C adds about USD 1 to 2). The earlier live sessions took 55 minutes to 2 hours 11 minutes and cost USD 0.7 to 1.6.

## How the result becomes a decision

| Result | Decision |
|---|---|
| A passes | Shortcuts to tables of another lakehouse are fine; D2 keeps native tables, with shortcuts allowed |
| B passes | The medallion can live in blob or ADLS; silver is bindable if the silver wrinkles are handled; Databricks (spike C) becomes optional |
| B fails, A passes | Keep the medallion in Fabric; use shortcuts only between lakehouses |
| Both fail | Fabric-native managed tables stay, the answer to your question is "not for the ontology today", and the docs are updated with the exact error |
| D fails | The CI/CD app cannot create ontology items: use the self-hosted runner fallback on `host` (SETUP.md) |

The result goes into [DECISIONS.md](DECISIONS.md) (D2, D4, D5) with the dates and the exact errors, and the Field Guide.

## Teardown and the independent check

When a spike ends, delete the pilot workspace items and the storage account (and the Databricks workspace for C), pause the F4, and then check with a fresh query, not from memory: the capacity is `Paused`, no storage account and no Databricks workspace remain in the resource group, and `az resource list --resource-type Microsoft.Fabric/capacities` shows only the F4. The kit's `wipe.sh` is for a whole build; the pilot items are deleted individually.

## Not decided here

Whether the live demo should show Databricks at all. Fabric-native tables are the path with three clean live builds; Databricks adds a story, not a need.
