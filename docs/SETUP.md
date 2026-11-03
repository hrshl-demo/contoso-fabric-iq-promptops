# Turning on the deployment pipelines

> **Starting from a blank subscription? Use [BOOTSTRAP.md](BOOTSTRAP.md) instead.** The `platform` pipeline ([PLATFORM.md](PLATFORM.md)) creates the capacity, the workspaces, the lakehouse and the data (and a storage account, with `CD_DATA_SOURCE=storage`), and `python -m tools.bootstrap` scripts the identity, the roles and the secrets, so steps 1, 2 and 4 below are not typed by hand. This page stays for reference, for using a capacity that already exists, and for the cost and safety layers, which apply to every pipeline. One difference: the bootstrap gives the pipeline **Contributor on the resource group** (it must create the capacity), not the pause-only role described in step 2.

Everything up to a pull request, and the release files, run without Azure. This page turns on the rest: **`cd-dev`** (deploy to the dev workspace after a merge), **`capacity`** (status, resume, pause, and a guard that pauses the F4 every 3 hours if someone forgot) and **Deploy to prod** inside the `release` workflow.

It is a one-time setup. **Nothing here runs by itself**: the pipelines stay switched off (their jobs are skipped, which costs no Actions minutes) until the last step sets `CD_ENABLED`. Every step says who does it, what it changes and what it costs. The pipelines are drafts: they are tested with fakes and the kit's Fabric mock, and they have not run against a live tenant. Step 5 is a careful first run.

## What it costs

| Item | Cost |
|---|---|
| F4 capacity, East US 2 | USD 0.72 an hour while it runs; paused costs no compute |
| A deploy without a data load | about 12 to 20 minutes of F4 time, so about USD 0.15 to 0.25 (estimate from the kit's measured stages: graph 2 to 3 minutes, checks a few more, resume and pause a few minutes) |
| The first deploy, with the data load | about 25 to 35 minutes, so about USD 0.3 to 0.4 (the kit's build stage took about 7 minutes) |
| Deploy to prod | the data is always loaded again, so as the first deploy: about USD 0.3 to 0.4 |
| A forgotten F4 | at most about USD 2.2: the guard pauses a running F4 every 3 hours (a scheduled run can start a few minutes late) |
| The two capacity settings (step 3) | free, but they take 7 to 11 minutes to apply |
| Actions minutes | the same minutes as the deploy; the guard is about 1.5 minutes per run while `CD_ENABLED` is on (240 a month); the Free plan has 2,000 a month |
| Entra app, role, federated credential | free |

The first real run replaces these estimates; the job summary shows the minutes the capacity was resumed and the cost at list price.

## How the F4 is kept from billing unseen

Five layers, each catching what the one before can miss:

1. **`tools.cd` pauses in a `finally`**, after a failed step, an interrupt or a pause that needs a second try. `--keep-running` is honoured only when every step succeeded; after a failure the capacity is paused anyway.
2. **A deadline** (`CD_DEADLINE_MIN`, default 45, at most 45): every step is bounded by the time that is left. A kit command is stopped, a call inside the pipeline that is still running is abandoned, and a step is not started with less than a minute left. The pause that follows is bounded too (about 25 minutes at the very most), so set-up (about 6 minutes), deadline and pause fit inside the job's 80 minute timeout; a test checks that arithmetic against the workflow files.
3. **A last workflow step** pauses again even when the deploy step was cancelled, timed out or crashed (pausing twice is harmless).
4. **The guard**, every 3 hours, pauses a running F4 when nothing is deploying (the deploy and the guard share one concurrency group, so the guard waits for a running deploy). `CD_GUARD_HOLD_UNTIL` holds it off for a demo.
5. **A pause that fails says so loudly**: "THE CAPACITY MAY STILL BE RUNNING", and the run fails.

What can still go wrong: the runner dies and GitHub starts the guard late, so a forgotten F4 bills for a few hours. Optional hardening below adds an Azure budget alert that tells you.

A deploy pauses the F4 when it ends, so **do not run one during a demo**.

## Who does what

| # | Step | Who | Changes | Needs your explicit go |
|---|---|---|---|---|
| 1 | The F4, resource group, dev workspace and prod workspace | you, or an agent you tell to | creates the F4 (billing until paused) | yes |
| 2 | Entra app, federated credential, capacity role | you, or an agent you tell to | one app registration and one role (on the one capacity) in the demo tenant | yes |
| 3 | Fabric settings | **you only** | one tenant setting for one security group; contributor access to the capacity; two capacity settings | yes, and an agent never does it |
| 4 | GitHub secrets and variables | you, or an agent you tell to | repository settings | yes |
| 5 | First runs | you press the buttons | the F4 runs for about 30 minutes in total | yes |

Use the demo profile for every `az` command: `$env:AZURE_CONFIG_DIR = "<your demo az profile folder>"`. Never `az login` into the shared default profile for this.

## 1. The F4, the resource group and the two workspaces

The pipelines need a **persistent** F4 (paused between deploys), one workspace per environment on it, and the F4 listed in two variables. The simplest way is the kit's own `provision`, which creates the resource group, the F4, a workspace and the lakehouse, and records them in a ledger on the machine that ran it (that ledger is what lets the kit's wipe delete them later, so keep that machine).

```powershell
$env:AZURE_CONFIG_DIR = "<your demo az profile folder>"
$env:CONTOSO_RESOURCE_GROUP = "rg-contoso-fabric-promptops"
$env:CONTOSO_CAPACITY_NAME = "contosofabricf4"
$env:CONTOSO_CAPACITY_ADMINS = "<your admin sign-in name>"
$env:CONTOSO_WORKSPACE_NAME = "Contoso-Bank-Ontology-Demo-Dev"
cd kit
python -m contoso_onto.cli provision --execute --state-dir "$HOME\promptops-state" --out "$HOME\promptops-out"
python -m contoso_onto.cli capacity suspend --execute --state-dir "$HOME\promptops-state"
```

Then, in the Fabric portal, create a second workspace named `Contoso-Bank-Ontology-Demo` and assign it to the same F4. Check with `python -m contoso_onto.cli capacity status --state-dir "$HOME\promptops-state"` that the capacity is `Paused`.

Names are your choice; they go into the variables in step 4.

## 2. The identity: an app that can only reach the F4 and the two workspaces

```powershell
$env:AZURE_CONFIG_DIR = "<your demo az profile folder>"
$app = az ad app create --display-name "contoso-fabric-iq-promptops-cd" --sign-in-audience AzureADMyOrg --query "{appId:appId, objectId:id}" -o json | ConvertFrom-Json
$sp = az ad sp create --id $app.appId --query id -o tsv
```

**The federated credential trusts runs on `main` only**, so a pull request or another branch can never sign in. Read the repository's real subject prefix first (repositories created after 15 July 2026 use an immutable format that contains numeric ids):

```powershell
gh api repos/hrshl-demo/contoso-fabric-iq-promptops/actions/oidc/customization/sub --jq .sub_claim_prefix
```

At the time of writing it is `repo:hrshl-demo@144888127/contoso-fabric-iq-promptops@1444536489`. The subject is that prefix plus `:ref:refs/heads/main`:

```powershell
$subject = "<the prefix above>:ref:refs/heads/main"
$json = @{ name = "github-main"; issuer = "https://token.actions.githubusercontent.com"; subject = $subject; audiences = @("api://AzureADTokenExchange") } | ConvertTo-Json -Compress
[System.IO.File]::WriteAllText("$PWD\fic.json", $json, (New-Object System.Text.UTF8Encoding $false))
az ad app federated-credential create --id $app.objectId --parameters "@fic.json"
Remove-Item fic.json
```

A role for **that one capacity**. Microsoft's page on pausing and resuming a capacity lists four actions: `read`, `write`, `suspend/action` and `resume/action`; without `write` the pause is refused. Create the role once (it can only be assigned inside the resource group), then assign it **on the capacity itself**, not on the group:

```powershell
$rg = "/subscriptions/<subscription id>/resourceGroups/rg-contoso-fabric-promptops"
$capacity = "$rg/providers/Microsoft.Fabric/capacities/contosofabricf4"
$role = @{ Name = "Fabric capacity operator (promptops demo)"; IsCustom = $true; Description = "Read, change, pause and resume Fabric capacities in this resource group"; Actions = @("Microsoft.Fabric/capacities/read", "Microsoft.Fabric/capacities/write", "Microsoft.Fabric/capacities/suspend/action", "Microsoft.Fabric/capacities/resume/action"); NotActions = @(); AssignableScopes = @($rg) } | ConvertTo-Json
[System.IO.File]::WriteAllText("$PWD\role.json", $role, (New-Object System.Text.UTF8Encoding $false))
az role definition create --role-definition "@role.json"
az role assignment create --assignee-object-id $sp --assignee-principal-type ServicePrincipal --role "Fabric capacity operator (promptops demo)" --scope $capacity
Remove-Item role.json
```

What the role allows: reading, pausing and resuming that capacity, and also changing it (`write` includes a resize). It cannot create or delete a capacity, and nothing in the pipelines resizes one. The residual risk is a workflow change merged to `main` that resizes the F4; that needs a human to merge it, and the credential works for runs on `main` only. The optional budget alert below would show the cost.

## 3. The Fabric settings (you only)

An agent never changes these. They decide who can use the Fabric APIs, so look at each one before you switch it on.

1. **One tenant setting, for one group.** Create a security group that contains only the app (`az ad group create --display-name contoso-fabric-cd --mail-nickname contoso-fabric-cd`, then `az ad group member add --group contoso-fabric-cd --member-id $sp`). In the Fabric admin portal, Tenant settings, Developer settings, switch **Service principals can call Fabric public APIs** on **for specific security groups** and name that group. Do not enable it for the whole organisation.
2. If *Users can create Ontology (preview) items* is limited to groups in your tenant, add the same group to it.
3. **Add the app to both workspaces as Admin** (workspace, Manage access, Add people or groups, search `contoso-fabric-iq-promptops-cd`).
4. **Let the app see the capacity.** In the Fabric admin portal, Capacity settings, your F4, Contributor permissions, add the same security group (or make it a capacity administrator). The Fabric API lists only capacities the caller administers or contributes to, and the pipeline uses that list to check that each workspace is on the capacity it pauses: a workspace on any other capacity would keep billing unseen, so the deploy refuses it.
5. **The two capacity settings**, once per capacity, as in the earlier builds: in the capacity's delegated settings tick *Override tenant admin selection*, switch on *Users can create Ontology (preview) items* and *Users can use Copilot and other features powered by Azure OpenAI*, press Apply. They need 7 to 11 minutes to apply. A persistent F4 keeps them, so this is not repeated.

## 4. GitHub secrets and variables

```powershell
cd <this repo>
gh secret set AZURE_CLIENT_ID --body "<the app id>"
gh secret set AZURE_TENANT_ID          # paste the demo tenant id when asked
gh secret set AZURE_SUBSCRIPTION_ID    # paste the demo subscription id when asked
gh variable set CD_RESOURCE_GROUP  --body "rg-contoso-fabric-promptops"
gh variable set CD_CAPACITY        --body "contosofabricf4"
gh variable set CD_WORKSPACE_DEV   --body "Contoso-Bank-Ontology-Demo-Dev"
gh variable set CD_WORKSPACE_PROD  --body "Contoso-Bank-Ontology-Demo"
```

Optional: `CD_LOCATION` (default `eastus2`), `CD_TIER` (default the kit's, `core`), `CD_USD_PER_HOUR` (default `0.72`), `CD_DEADLINE_MIN` (default and maximum `45`, minimum `10`; the 80 minute job timeout is set for the maximum), `RELEASE_APPROVERS` (default the repository owner), `CD_GUARD_HOLD_UNTIL` (see below).

The ids are secrets, not variables, so they are masked in logs and never in a file. **`CD_ENABLED` is the switch; set it last:**

```powershell
gh variable set CD_ENABLED --body "true"
```

## 5. First runs, safest order

1. **Actions, capacity, Run workflow, action `status`** (free, about 2 minutes). It proves the sign-in and the role, and changes nothing. Expect `Paused`.
2. **Actions, cd-dev, Run workflow, Reload data on** (about 30 minutes of F4 time, about USD 0.35). Watch the job summary: each step, its seconds and the cost at list price. The F4 is paused at the end, also when a step fails.
3. **Check independently**: the Azure portal or `az resource list --resource-type Microsoft.Fabric/capacities`, the capacity must be `Paused`; open the dev workspace and the ontology.
4. **Actions, release, Run workflow** with your version, Deploy to prod off, Publish off (the dry run from [RELEASING.md](RELEASING.md)).
5. Only then the real thing: Deploy to prod on and Publish on. It refuses a commit whose deployed files differ from the last dev deployment that really ran (a run whose deploy job was skipped does not count), and it loads the data again.

Before a demo, resume the F4 yourself (`capacity` workflow, action `resume`) and set the variable `CD_GUARD_HOLD_UNTIL` to the end of the demo, for example `2026-11-05T20:00:00Z`, so the guard does not pause it under you. Remove the variable afterwards. Without it the guard pauses a running F4 at its next run, every 3 hours.

## If something fails

| What you see | Cause and fix |
|---|---|
| `AADSTS700213` or "No matching federated identity record" | The subject does not match. Re-read the prefix (step 2); the credential must be for `ref:refs/heads/main` and the run must be on `main` |
| `capacity status failed` or `capacity suspend failed` with 403 | The role (step 2) is missing, is on the wrong scope, or lacks `write`; it must be assigned on the capacity itself |
| "the signed-in identity cannot get a token for ..." | Sign-in worked, but a service refused it: check the tenant setting and the group (step 3) |
| `workspace '...' was not found, or the identity has no role in it` | The app is not an Admin of that workspace (step 3) |
| "the identity cannot see capacity '...' through the Fabric API" | The group is not a contributor or administrator of the capacity (step 3, item 4) |
| "workspace '...' is not on capacity '...'" | The workspace is assigned to another capacity, which this pipeline would not pause. Assign it to the F4, or fix `CD_CAPACITY` |
| "stopped at the 45-minute deadline" or a step marked `abandoned` | A step took too long; the capacity was paused. Run again, the kit resumes where it stopped |
| Ontology create answers 403 `FeatureNotAvailable` | The two capacity settings are not applied yet; wait 7 to 11 minutes after Apply |
| "THE CAPACITY MAY STILL BE RUNNING" | Pause it now: the portal, or `capacity` workflow, action `pause`. Then read the log |

## If the app cannot create ontology items

The ontology and graph are in preview and it is not known yet whether a service principal may create them. If `cd-dev` fails only there, the fallback is a self-hosted runner on the VM `host`, signed in as the demo admin: install the runner on `host`, change `runs-on` in the three workflows to its label, and drop the `azure/login` step. That is a small change for a pull request; do not do it before you know it is needed.

## Turning it off

Delete the variable `CD_ENABLED` (or set it to anything but `true`): every Azure job is skipped again. To revoke access completely, delete the federated credential or the app (`az ad app delete --id <app id>`). The F4 itself is deleted with the kit's `wipe.sh` on the machine that created it, never by a pipeline.

## Optional hardening

- Repository setting *Require actions to be pinned to a full-length commit SHA* (all workflows here already are).
- `RELEASE_APPROVERS` with more than the owner when others may release.
- **An Azure budget alert on the resource group** (Azure portal, Cost Management, Budgets): a small monthly amount (for example USD 15) with an email at 50% and 100%. It is the net under the layers above and the only thing that tells you about a capacity that was resized or left running by a dead runner.
