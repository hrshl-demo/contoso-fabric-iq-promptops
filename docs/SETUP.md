# Turning on the deployment pipelines

Everything up to a pull request, and the release files, run without Azure. This page turns on the rest: **`cd-dev`** (deploy to the dev workspace after a merge), **`capacity`** (status, resume, pause, and a guard that pauses the F4 twice a day if someone forgot) and **Deploy to prod** inside the `release` workflow.

It is a one-time setup. **Nothing here runs by itself**: the pipelines stay switched off (their jobs are skipped, which costs no Actions minutes) until the last step sets `CD_ENABLED`. Every step says who does it, what it changes and what it costs. The pipelines are drafts: they are tested with fakes and the kit's Fabric mock, and they have not run against a live tenant. Step 5 is a careful first run.

## What it costs

| Item | Cost |
|---|---|
| F4 capacity, East US 2 | USD 0.72 an hour while it runs; paused costs no compute |
| A deploy without a data load | about 12 to 20 minutes of F4 time, so about USD 0.15 to 0.25 (estimate from the kit's measured stages: graph 2 to 3 minutes, checks a few more, resume and pause a few minutes) |
| The first deploy, with the data load | about 25 to 35 minutes, so about USD 0.3 to 0.4 (the kit's build stage took about 7 minutes) |
| The two capacity settings (step 3) | free, but they take 7 to 11 minutes to apply |
| Actions minutes | the same minutes as the deploy; the Free plan has 2,000 a month |
| Entra app, role, federated credential | free |

The first real run replaces these estimates; the job summary shows the minutes the capacity was resumed and the cost at list price.

## Who does what

| # | Step | Who | Changes | Needs your explicit go |
|---|---|---|---|---|
| 1 | The F4, resource group, dev workspace and prod workspace | you, or an agent you tell to | creates the F4 (billing until paused) | yes |
| 2 | Entra app, federated credential, capacity role | you, or an agent you tell to | one app registration and one role in the demo tenant | yes |
| 3 | Fabric settings | **you only** | one tenant setting for one security group; two capacity settings | yes, and an agent never does it |
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

A role that can only read, pause and resume capacities in that one resource group (it cannot create or delete anything):

```powershell
$scope = "/subscriptions/<subscription id>/resourceGroups/rg-contoso-fabric-promptops"
$role = @{ Name = "Fabric capacity operator (promptops demo)"; IsCustom = $true; Description = "Read, pause and resume Fabric capacities in this resource group"; Actions = @("Microsoft.Fabric/capacities/read", "Microsoft.Fabric/capacities/suspend/action", "Microsoft.Fabric/capacities/resume/action"); NotActions = @(); AssignableScopes = @($scope) } | ConvertTo-Json
[System.IO.File]::WriteAllText("$PWD\role.json", $role, (New-Object System.Text.UTF8Encoding $false))
az role definition create --role-definition "@role.json"
az role assignment create --assignee-object-id $sp --assignee-principal-type ServicePrincipal --role "Fabric capacity operator (promptops demo)" --scope $scope
Remove-Item role.json
```

## 3. The Fabric settings (you only)

An agent never changes these. They decide who can use the Fabric APIs, so look at each one before you switch it on.

1. **One tenant setting, for one group.** Create a security group that contains only the app (`az ad group create --display-name contoso-fabric-cd --mail-nickname contoso-fabric-cd`, then `az ad group member add --group contoso-fabric-cd --member-id $sp`). In the Fabric admin portal, Tenant settings, Developer settings, switch **Service principals can call Fabric public APIs** on **for specific security groups** and name that group. Do not enable it for the whole organisation.
2. If *Users can create Ontology (preview) items* is limited to groups in your tenant, add the same group to it.
3. **Add the app to both workspaces as Admin** (workspace, Manage access, Add people or groups, search `contoso-fabric-iq-promptops-cd`).
4. **The two capacity settings**, once per capacity, as in the earlier builds: in the capacity's delegated settings tick *Override tenant admin selection*, switch on *Users can create Ontology (preview) items* and *Users can use Copilot and other features powered by Azure OpenAI*, press Apply. They need 7 to 11 minutes to apply. A persistent F4 keeps them, so this is not repeated.

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

Optional: `CD_LOCATION` (default `eastus2`), `CD_TIER` (default the kit's, `core`), `CD_USD_PER_HOUR` (default `0.72`), `RELEASE_APPROVERS` (default the repository owner), `CD_GUARD_HOLD_UNTIL` (see below).

The ids are secrets, not variables, so they are masked in logs and never in a file. **`CD_ENABLED` is the switch; set it last:**

```powershell
gh variable set CD_ENABLED --body "true"
```

## 5. First runs, safest order

1. **Actions, capacity, Run workflow, action `status`** (free, about 2 minutes). It proves the sign-in and the role, and changes nothing. Expect `Paused`.
2. **Actions, cd-dev, Run workflow, Reload data on** (about 30 minutes of F4 time, about USD 0.35). Watch the job summary: each step, its seconds and the cost at list price. The F4 is paused at the end, also when a step fails.
3. **Check independently**: the Azure portal or `az resource list --resource-type Microsoft.Fabric/capacities`, the capacity must be `Paused`; open the dev workspace and the ontology.
4. **Actions, release, Run workflow** with your version, Deploy to prod off, Publish off (the dry run from [RELEASING.md](RELEASING.md)).
5. Only then the real thing: Deploy to prod on and Publish on. It refuses a commit whose deployed files differ from the last successful dev deployment.

Before a demo, resume the F4 yourself (`capacity` workflow, action `resume`) and set the variable `CD_GUARD_HOLD_UNTIL` to the end of the demo, for example `2026-11-05T20:00:00Z`, so the guard does not pause it under you. Remove the variable afterwards. Without it the guard pauses a running F4 at 14:00 and 02:00 India time.

## If something fails

| What you see | Cause and fix |
|---|---|
| `AADSTS700213` or "No matching federated identity record" | The subject does not match. Re-read the prefix (step 2); the credential must be for `ref:refs/heads/main` and the run must be on `main` |
| `capacity status failed` with 403 | The role (step 2) is missing or on another resource group |
| "the signed-in identity cannot get a token for ..." | Sign-in worked, but a service refused it: check the tenant setting and the group (step 3) |
| `workspace '...' was not found, or the identity has no role in it` | The app is not an Admin of that workspace (step 3) |
| Ontology create answers 403 `FeatureNotAvailable` | The two capacity settings are not applied yet; wait 7 to 11 minutes after Apply |
| "THE CAPACITY MAY STILL BE RUNNING" | Pause it now: the portal, or `capacity` workflow, action `pause`. Then read the log |

## If the app cannot create ontology items

The ontology and graph are in preview and it is not known yet whether a service principal may create them. If `cd-dev` fails only there, the fallback is a self-hosted runner on the VM `host`, signed in as the demo admin: install the runner on `host`, change `runs-on` in the three workflows to its label, and drop the `azure/login` step. That is a small change for a pull request; do not do it before you know it is needed.

## Turning it off

Delete the variable `CD_ENABLED` (or set it to anything but `true`): every Azure job is skipped again. To revoke access completely, delete the federated credential or the app (`az ad app delete --id <app id>`). The F4 itself is deleted with the kit's `wipe.sh` on the machine that created it, never by a pipeline.

## Optional hardening

- Repository setting *Require actions to be pinned to a full-length commit SHA* (all workflows here already are).
- `RELEASE_APPROVERS` with more than the owner when others may release.
