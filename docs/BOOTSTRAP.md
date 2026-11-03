# Bootstrap: from an empty subscription to a built platform

This is the guided path for the one-time set-up. Almost everything is a command or a button in GitHub. Two things only a person can do, because they are tenant-wide choices in Fabric: **two tenant settings** (step 3) and **one capacity page with two or three switches** (step 6). The pipeline never changes them for you, and neither does any script here.

Nothing here touches the superapp repository or any tenant other than the one the `az` profile is signed in to. Synthetic data only.

| # | Step | Who | Time | Cost |
|---|---|---|---|---|
| 1 | Look at what the bootstrap would do (a dry run) | you | 2 min | free |
| 2 | Create the identity, the resource group and the settings | `bootstrap apply --execute` | 3 min | free |
| 3 | Two Fabric tenant settings | a Fabric administrator | 2 min, then up to 15 min to take effect | free |
| 4 | Switch the pipelines on | `bootstrap enable --execute` | 1 min | free |
| 5 | Check every door | Actions > platform > `check` | 5 min | free |
| 6 | Build the platform, then one capacity click while the data loads | Actions > platform > `build` | 35 to 45 min | about USD 0.4 to 0.6 of F4 time |
| 7 | Look at the outputs | you | 2 min | free |
| 8 | Later: onboard a content repository | `bootstrap consumer --execute` | 3 min | free |

## Before you start

- `az` signed in to the **demo** tenant, in its own profile so that your other work is untouched, and `gh` signed in as the repository owner:

  ```powershell
  $env:AZURE_CONFIG_DIR = "$HOME\.azure-contoso-demo"     # a separate profile, only for this demo
  az login                                                # first time only; pick the demo tenant
  az account show --query "{subscription:name, id:id}" -o table
  gh auth status
  ```

- The account you sign in with is **Owner** of the subscription (it registers resource providers, creates the resource group and assigns roles), may create applications and groups in Entra, and is a **Fabric administrator** (step 3). The build makes it a capacity administrator, which is all step 6 needs.
- The Python environment of this repository: `python -m pip install -r requirements-ci.txt`.

## 1. The dry run

```powershell
python -m tools.bootstrap apply --repo hrshl-demo/contoso-fabric-iq-promptops
```

It reads and prints; it changes nothing. **Before it changes anything (and in the dry run too) it checks, read-only, everything it could refuse**: that the repository exists and you administer it, that the resource group does not exist or is empty or was made by this project (anything else is refused unless you pass `--adopt`, because the pipeline's identity gets Contributor over the whole group), and that an existing app trusts nothing but the one repository it is for. Any other federated credential on the app, **whatever its name**, lets that other repository (or branch) sign in as the app, so it is refused unless you pass `--replace-trust`, which removes them (a typo cannot hand a privileged identity to the wrong repository, and an app that was made by hand with extra credentials is not adopted silently). For a content repository it also checks that a role that already has the name of the capacity role is exactly the role this project defines (the same actions, the same assignable scope, no condition, one permission block): a role is not trusted by its name. A credential with the right issuer and subject but another audience than `api://AzureADTokenExchange` is not the right trust either: it could not sign in, so it is replaced (with `--replace-trust`) rather than taken for the real thing. On a blank subscription it lists about 20 things it would create:

| It creates | Why |
|---|---|
| The `Microsoft.Fabric` resource provider registration | Fabric capacities are an Azure resource type |
| An **empty** resource group `rg-contoso-promptops` (East US 2) | The pipeline's rights end at its edge; creating resource groups would need rights on the whole subscription |
| An Entra app and service principal `contoso-promptops-platform` | The pipeline's identity. It has no password and no secret |
| A trust (federated credential) for the **main** branch of this repository only | A pull request or another branch can never sign in |
| An Entra group `contoso-promptops-fabric-apps` with the identity in it | The two Fabric tenant settings are scoped to this group, not to the whole tenant |
| Two roles on the resource group: **Contributor** and **Storage Blob Data Contributor** | Create the capacity (and, with `CD_DATA_SOURCE=storage`, the storage account) there; the second role is used only for that storage account. Nothing wider: no role on the subscription |
| Three GitHub secrets (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`) and the variables `CD_RESOURCE_GROUP`, `CD_CAPACITY`, `CD_LOCATION`, `CD_WORKSPACE_DEV`, `CD_WORKSPACE_PROD`, `CD_SP_GROUP_NAME`, `CD_ADMIN_OBJECT_IDS`, `CD_ADMIN_UPNS`, `CD_PIPELINE_OBJECT_ID` | The workflows read them. The ids go in through standard input, never on a command line. Your object id is used for the workspace role, your **user principal name** for the capacity (Fabric takes a UPN for a user and an object id for a service principal) |

The names are defaults. Change any of them with `--resource-group`, `--capacity`, `--location`, `--workspace-dev`, `--workspace-prod`, `--group`, and add other administrators with `--admin <user principal name or object id>`. The platform **adds** capacity administrators and never removes one: someone you add by hand in the portal stays.

## 2. Do it

```powershell
python -m tools.bootstrap apply --repo hrshl-demo/contoso-fabric-iq-promptops --subscription <the subscription id from az account show> --execute
```

`--subscription` is required with `--execute` and must be the signed-in subscription. That is the guard against running it in the wrong tenant. Run it again at any time: it changes nothing that is already right (it only writes the three secrets again, because a secret cannot be read back).

## 3. Two Fabric tenant settings (a Fabric administrator, about 2 minutes)

Open the [Fabric admin portal, Tenant settings](https://app.fabric.microsoft.com/admin-portal/tenantSettings), expand **Developer settings**, and for each of these:

1. **Service principals can call Fabric public APIs**: Enabled, *Specific security groups*, add `contoso-promptops-fabric-apps`, Apply.
2. **Service principals can create workspaces, connections, and deployment pipelines**: Enabled, *Specific security groups*, add `contoso-promptops-fabric-apps`, Apply.

Only the members of that group are affected. A change can take up to 15 minutes to reach Fabric. If you cannot see these settings you are not a Fabric administrator: ask one, or assign the role to yourself in the Microsoft 365 admin centre.

## 4. Switch the pipelines on

```powershell
python -m tools.bootstrap enable --repo hrshl-demo/contoso-fabric-iq-promptops --subscription <id> --execute
```

This sets `CD_ENABLED=true`. From then on the capacity guard (every 3 hours) runs and pauses a capacity that someone forgot. It does nothing when there is no capacity yet.

## 5. Check every door (free)

[Actions > platform > Run workflow](https://github.com/hrshl-demo/contoso-fabric-iq-promptops/actions/workflows/platform.yml): leave **action** on `check`, target `dev`. It signs in, asks Azure for the plan of the infrastructure (what-if) and lists Fabric workspaces as the pipeline's identity (the first tenant setting). Nothing is created and nothing bills. The job summary says what is open and, for a closed door, the exact setting to change. Repeat until it is green.

**What `check` proves about the second setting:** it creates an empty workspace without a capacity (named `PromptOpsProbe-…`) and deletes it at once. If Fabric answers 401 or 403, the summary shows **CLOSED** with the exact setting to change, and a `build` stops before it creates anything. Any other answer is shown as *skipped* (it could not be proven), and the build checks it again at step 8. A change in the Fabric admin portal can take up to 15 minutes to reach the pipeline's identity, so run `check` again until it is green: it is free.

## 6. Build

Same page: **action** `build`, target `dev`, and type `BUILD` in the confirm box. Only the repository owner (or the people in `RELEASE_APPROVERS`) can do this, and only from `main`. The run:

| About | What happens |
|---|---|
| 0 min | Guards, then the free check again. If a door is closed the run stops here, before anything is created |
| 3 min | The bronze, silver and gold layers are built from the seed data in the repository (with `CD_DATA_SOURCE=storage`: first through a storage account, see [PLATFORM.md](PLATFORM.md)) |
| 10 min | **The F4 capacity is created (or the one that exists is kept: it is never resized, and only missing administrators are added). It bills about USD 0.72 an hour from here until the pause** |
| 14 min | The workspace and the lakehouse are created and the roles are given. **The run now checks the ontology gate. If it is closed it prints `ACTION NEEDED` with a link: that is your click** |
| 14 to 22 min | The data is loaded into the lakehouse (while you click, see below) |
| 22 to 36 min | If the gate is still closed the run checks every minute for up to `gate_wait_min` (20) minutes |
| 36 min | The outputs are written and **the capacity is paused** |

The times are estimates for the first run. The click, while the data loads:

1. Open the link in the log of the step *check the ontology gate and ask the admin to open it* (or in the job summary, or in the `platform-outputs` artifact, file `ADMIN_APPROVAL.md`). It looks like `https://app.fabric.microsoft.com/admin-portal/capacities/<capacity id>/delegatedSettings?experience=power-bi`.
2. For each of these settings: tick **Override tenant admin selection**, switch it **On**, press **Apply**:
   - **Users can create Ontology (preview) items**
   - **Users can use Copilot and other features powered by Azure OpenAI**
   - only when the capacity is outside the US and the EU: **Data sent to Azure OpenAI can be processed outside this capacity's geographic region, compliance boundary, or national cloud instance**
3. Nothing else. The settings take 7 to 11 minutes to apply and the run carries on by itself. A paused capacity greys these settings out, which is why the run asks while the capacity is running.

If the gate is still closed when the wait ends the run ends with **exit 3** (yellow in the summary), after pausing the capacity. Run `build` again: it resumes the capacity, finds the data already loaded, checks the gate and, now that you have clicked, carries on.

## 7. The outputs

The `platform-outputs` artifact holds `platform-outputs.json`: the workspace, the lakehouse (with its SQL endpoint), the data source and the state of the gate. No secret is in it. The content repository's contract is in [CONSUMING.md](CONSUMING.md).

## 8. Onboarding a content repository (later)

When the content repository exists (private), from this checkout:

```powershell
python -m tools.bootstrap consumer --repo hrshl-demo/contoso-fabric-iq-promptops --consumer <owner>/<content repo> --subscription <id> --execute
```

It gives the content repository its own identity (its own trust, in the same Entra group, in an app named with the owner and the repository so that two repositories called `content` cannot collide), its own secrets and variables copied from this repository, and one **custom role limited to reading, writing, pausing and resuming a capacity** (Azure needs *write* for pause and resume; there is no delete right and no data right on the storage), **assigned on the capacity itself**, so that it can run that capacity and cannot create another. So **the capacity must exist: run `platform` > `build` first.** It adds the identity to `CD_CONSUMER_OBJECT_IDS`. Then run `platform` > `build` once more (it is fast and cheap when everything exists: the capacity is kept and only the missing administrator is added): that makes the new identity a capacity administrator and a workspace contributor, which Fabric requires before a service principal can create items there.

After a `destroy` with **everything** and a new build the capacity is a new resource, so the role assignment on it is gone: run this command again.

## Costs, in one place

| Item | When | Cost |
|---|---|---|
| Storage account (the data source, a few MB) | always | cents a month |
| F4 capacity | from creation until paused, only during a build or a content deployment | USD 0.72 an hour (East US 2). A first build is about USD 0.4 to 0.6 |
| A forgotten running F4 | until the guard pauses it, at most 3 hours | at most about USD 2.2 |
| GitHub Actions (Free plan, private repo) | per run | a build is about 45 Linux minutes of the 2,000 a month |

## If something fails

| Symptom | Cause and fix |
|---|---|
| `--subscription is required` or `different subscription` | You are signed in to another profile. Set `AZURE_CONFIG_DIR` to the demo profile, check `az account show`, and pass its id |
| `exists, holds N resources and was not made by this project` | The resource group name is in use by something else. Pick another `--resource-group`, or `--adopt` if you are sure |
| `already trusts '...'` | The app trusts something besides the one repository it is for (any credential, whatever its name). Check `--repo` / `--consumer` for a typo; `--replace-trust` removes the other credentials if you mean it |
| `a custom role called 'Contoso PromptOps Capacity Operator' already exists, but it is not the role this project defines` | A role with that name allows something else than reading, writing, pausing and resuming a capacity, is assignable somewhere else, or has a condition or several permission blocks. It would be given to the content repository's identity, so it is refused. Delete or rename it in the Azure portal (Access control (IAM), Roles) and run the command again |
| `check` says *Fabric API access* is closed | Setting 1 is not on yet, or the group has no members: wait 15 minutes, check the group in Entra |
| The build stops at step 8 with *Fabric refused to create the workspace (401)* (or 403) | Setting 2 is not on for the group, or not effective yet (up to 15 minutes), or the pipeline's identity is not an administrator of the capacity. The message says both. The free `check` now proves the tenant setting before anything is created: run it again until it is green. The capacity is paused and costs a few cents for this try |
| *capacity ... is F8, but the platform is built for F4* | Someone resized the capacity. The platform never resizes one (it would change the bill): resize it back, or pick another `CD_CAPACITY` |
| The what-if or the capacity creation fails | The error is Azure's. Typical causes: the region has no F4 (set `CD_LOCATION` to another region; outside the US and EU you also need the third capacity setting), a quota, or a capacity administrator that Fabric does not accept (a person needs a user principal name, a service principal an object id). Nothing bills until the capacity exists |
| The build ends with **exit 3** | The ontology gate is still closed. Do the click above, then run `build` again |
| The build ends with **exit 4** or says *THE CAPACITY MAY STILL BE RUNNING* or *THE CAPACITY MAY STILL APPEAR AND BILL* | The pause failed, or a capacity deployment could not be stopped and no capacity exists yet. Run the `capacity` workflow with action `pause`, or look in the Azure portal (resource group, Deployments, `platform-capacity`: cancel it; then the capacity: pause or delete it), now |
| A run was cancelled | The last step of the job (`platform rescue`) cancels a capacity deployment that may still be running, waits for it to end, and pauses the capacity. Check with the `capacity` workflow, action `status` |
| The storage upload fails with an authorization error right after creation | The role needs a few minutes; the run already retries for about three minutes |
| The upload says *The request may be blocked by network rules of storage account* (only with `CD_DATA_SOURCE=storage`) | A policy in the subscription turned off the storage account's public network access, so GitHub's runners cannot reach it (`az storage account show --query publicNetworkAccess` says `Disabled`). Leave `CD_DATA_SOURCE` unset (the default, `repository`): the seed data is loaded straight into the lakehouse. Found in the first live run; nothing was billed, the capacity did not exist yet |

## Tearing down

`platform` > `destroy`, type `DESTROY`: purges this target's workspace the way the kit's wipe does (the items are deleted for real, the recycle bin is emptied, the workspace is renamed and deleted, so that its name can be used again). That needs a running capacity: it is resumed for the purge (a few minutes, about USD 0.1) and paused again at the end, also when the destroy fails. With **everything** it also purges the other workspace, deletes the capacity and the storage account, and checks the resource group is empty. The resource group, the identity and the group stay (the bootstrap made them); delete them by hand in the portal if you want them gone.
