# Manual steps: what a script cannot do

`build.sh` is a CI/CD-style cycle: **BUILD** (resource group, F4 capacity, workspace, lakehouse with its three layers: the bronze seed files, the silver and the
gold tables, loaded in that order; then the Direct Lake semantic model, which binds to gold only),
**APPROVE** (the one human step, below), **DEPLOY** (the ontology with its BIAN / Microsoft CDM notes, then the graph over REST) and **TEST**
(verify, then the ontology agent answers its golden questions), followed by the agent pack. Only step 1 is needed for the pipeline to finish;
the portal-only steps after it (metrics, synonyms, the data agent) are optional demo polish. In this order:

## 1. The capacity admin step: stage 2 of the build (APPROVE). Without it Fabric refuses to create the ontology

`build.sh` runs into this gate on its own. It first tries to create the ontology (that call is also the probe). On `403 FeatureNotAvailable`
it prints **ACTION NEEDED** with the direct link for the new capacity, **waits 15 minutes** for the settings to propagate, then **polls every
minute** (up to 120 minutes in all) by trying again, and carries on by itself the moment it works. Nothing is created while the gate is
closed, and everything built so far is kept. Options: `--gate-wait MIN` (15), `--gate-poll SEC` (60), `--gate-timeout MIN` (120),
`--no-gate-wait` (stop with exit 3 at the gate and re-run later, the previous behaviour). The request is also saved in `out/ADMIN_APPROVAL.md`.
If the settings are already on (a tenant-wide setting, or an override that is still in place) the gate is open at once and the stage takes
no time.

Two routes open it; the capacity route needs no tenant admin and is the one the build prints:

Where (tenant route): <https://app.fabric.microsoft.com> > Settings (gear, top right) > **Admin portal** > **Tenant settings**. The same page is under
OneLake catalog > Govern > Configurations > Tenant settings. Search the setting by name, expand it, switch it to **Enabled**, apply it to
the entire organisation (or to a security group that contains the account running `build.sh`), then **Apply**. A change can take a few
minutes up to an hour to reach the service.

| Setting | Why | When |
|---|---|---|
| Users can create Fabric items | every Fabric item | already on (the lakehouse and semantic model were created) |
| **Users can create ontology (preview) items** | without it Fabric answers `403 FeatureNotAvailable` to every ontology create, even an empty one | **stage 2** |
| Users can create and share Data agent item types | the data agent on the ontology; the 2026 tenant-settings index no longer lists it, so enable it only if your portal still shows it | step 4 |
| **Users can use Copilot and other features powered by Azure OpenAI** | the ontology agent (portal pane and MCP `ask_ontology`), data agent; can also be managed per capacity | **stage 2** (so the TEST stage can ask the agent), step 4 |
| Capacities can be designated as Fabric Copilot capacities | data agent | step 4 |
| Data sent to Azure OpenAI can be processed outside your capacity's geographic region, compliance boundary, or national cloud instance | only when the capacity is outside the US and EU (the default `eastus2` is inside; South India is not) | stage 2 / step 4 |
| Data sent to Azure OpenAI can be stored outside your capacity's geographic region, compliance boundary, or national cloud instance | same | step 4 |

The kit never changes tenant settings: they affect everybody in the tenant.

**Not a Fabric administrator? Use the capacity (verified live, twice).** The account that creates the F4 is its capacity admin, and the
capacity page lists *Delegated tenant settings*, which include **Users can create Ontology (preview) items** and the Copilot settings.
A capacity admin can override them for that capacity only: open the link the build prints (Admin portal > **Capacity settings** > **Fabric
capacity** tab > the gear next to `contosofabricf4` > **Delegated tenant settings**; the capacity must be Active, a paused one is greyed
out) > for each of the two settings: expand it > tick **Override tenant admin selection** > **Enabled** > apply to all users in the capacity >
**Apply**. In the demo tenant (the account held only *Global Reader* in Entra) this opened ontology creation 10 to 15 minutes later, with no
tenant-wide change, and the ontology agent answered from the same capacity (East US 2, 16 Oct 2026). The override is scoped to that
capacity only, so it goes away when `wipe.sh` deletes the capacity: every new build creates a new capacity and the override has to be applied
again, which is exactly what stage 2 asks for. If the setting is not listed there, only the Entra roles
*Fabric Administrator* or *Global Administrator* can change it; then run the kit in a tenant where you hold one of them. The kit reuses
the `az` sign-in of the host and never signs in, signs out or switches subscription: sign in to that tenant on the host, run
`bash wipe.sh` first if a previous build is still recorded for the old subscription, then `bash build.sh`.

## 2. If the script's ontology create is rejected for another reason (portal fallback)

This only helps when Fabric accepts the ontology item but rejects the definition the kit sends; the tenant setting in step 1 is needed for
the portal route as well. The semantic model `ContosoBankModel` exists by then (the build creates it first).

1. Workspace `Contoso-Bank-Ontology-Demo` > **New item** > **Ontology (preview)** > name it `Seed` > **Generate from semantic model**
   (docs: "Generate an ontology (preview) from semantic models") > choose `ContosoBankModel`, select the tables > create.
2. On hostvm: `bash build.sh --helper export --name Seed`, then
   `bash build.sh --seed ~/.local/state/contoso-bank-ontology-demo/out/seed/ontology`. The kit rebuilds the full ontology reusing the
   portal's annotations (the live version is backed up first).
3. Delete the `Seed` item in the portal.

## 3. After the build: metrics, synonyms (portal only); the graph is automatic

The build creates and refreshes the graph itself (stage 3, over REST, from the same specification as the ontology): 31 node types and 53 edge
types on the core tier, queryable straight away; `--no-graph` leaves it to the portal. Details in [LIVE_CHECKLIST.md](LIVE_CHECKLIST.md)
steps 3 and 4. If the portal still shows entities as ineligible, open Manage graph once and press Continue. Then generate metrics from the
semantic model (the only way they arrive), let the ontology agent propose synonyms and metadata, review, accept, then export so later
builds keep the edits.

## 4. Data agent, conversational AI, voice

[LIVE_CHECKLIST.md](LIVE_CHECKLIST.md) steps 5 and 6: create the data agent on the ontology in the portal (paste
`out/core/agents/data_agent/instructions.md`, attach the data dictionary). The TEST stage already asks the ontology agent 5 golden
questions through MCP (`--golden full` asks all 27 MCP questions, about 25 minutes, `--golden none` skips it); to score answers you
collected yourself use `bash build.sh --helper verify --answers answers.json`. MCP configs, the voice profile and the Foundry IQ calls are
in the agent pack. The data agent needs the Copilot and Azure OpenAI settings from step 1; the ontology MCP endpoint needs a user sign-in.

## 5. Cost control and clean cycles

```bash
bash build.sh --pause      # stop compute billing (F4 is about $0.88/h); `bash build.sh --resume` or the next build resumes it
bash wipe.sh               # hard-delete everything build.sh created, including the whole resource group, then audit the subscription
bash build.sh --fresh      # one clean cycle: wipe what an earlier build left (an older F4 too), then build, approve, deploy, test
```

`wipe.sh` ends with an **audit**: every Fabric capacity still in the subscription is listed (read only). One that carries this kit's
capacity name but is not in the ledger (an older F4 from another run or host) is flagged; it is never deleted automatically. The wipe covers
all three data layers: the workspace and every item in it are deleted, and the lakehouse takes its `Files/bronze/...` folder and its silver and gold
tables with it.

## 6. If the SQL analytics endpoint lags behind the tables (portal only)

The load asks the lakehouse's SQL analytics endpoint to refresh its metadata once per layer (a preview API; the call is best effort and never stops the load),
and after the last layer it follows that refresh for up to 10 minutes. The **ontology** reads the lakehouse's tables through that endpoint (the semantic model
reads OneLake directly, so it is not affected). If the ontology cannot be created right after the load (the error output then carries a `note: the lakehouse tables
were loaded N min N s ago ... probably still syncing`), or the table count in the endpoint stays below the `load done: N tables` line of the build log after a few
minutes (the check is in [LIVE_CHECKLIST.md](LIVE_CHECKLIST.md) step 1a, item 4): open the workspace > the lakehouse > its SQL analytics endpoint, use the
endpoint's metadata refresh action, wait until all N tables are listed, then run `bash build.sh` again. The tables are not loaded a second time (an unchanged
layer whose tables are in the lakehouse is skipped), so the run goes straight on to the ontology.
