import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import bootstrap as bs, cd, pii_guard

SUB = "11111111-aaaa-4aaa-8aaa-111111111111"
TENANT = "22222222-bbbb-4bbb-8bbb-222222222222"
USER_ID = "33333333-cccc-4ccc-8ccc-333333333333"
USER_UPN = "admin@demo.test"
COLLEAGUE_ID = "44444444-dddd-4ddd-8ddd-444444444444"
COLLEAGUE_UPN = "colleague@demo.test"
PLATFORM = "demo-org/platform"
CONTENT = "demo-org/content"
SCOPE = f"/subscriptions/{SUB}/resourceGroups/rg-contoso-promptops"
CAPACITY_ID = f"{SCOPE}/providers/Microsoft.Fabric/capacities/contosopromptopsf4"


class FakeCloud:
    """A tenant in memory behind the same interface as bootstrap.Cloud. `changes` lists everything that was changed; a dry run must leave it empty."""

    def __init__(self, user_type="user"):
        self.acct = {"id": SUB, "tenantId": TENANT, "name": "Demo subscription", "user": USER_UPN, "userType": user_type}
        self.providers = {"Microsoft.Fabric": "NotRegistered", "Microsoft.Storage": "Registered"}
        self.rgs: dict[str, dict] = {}                                   # name -> {"tags": {...}, "resources": n}
        self.apps, self.sps, self.creds, self.groups = {}, {}, {}, {}
        self.members, self.roles, self.role_defs = set(), set(), {}
        self.role_scopes: list[str] = []                                  # where the role definition was looked up
        self.repos = {PLATFORM: {"admin": True}, CONTENT: {"admin": True}}
        self.prefixes = {PLATFORM: "repo:demo-org@1/platform@2", CONTENT: "repo:demo-org@1/content@3"}
        self.capacities: dict[str, str] = {}                              # name -> resource id
        self.secrets: dict[str, dict[str, str]] = {}
        self.vars: dict[str, dict[str, str]] = {}
        self.changes: list[tuple] = []
        self.readonly = False
        self.n = 0

    def _change(self, *what):
        assert not self.readonly, f"a dry run changed something: {what}"
        self.changes.append(what)

    def _id(self):
        self.n += 1
        return f"00000000-0000-4000-8000-{self.n:012d}"

    # Azure
    def account(self):
        return dict(self.acct)

    def provider_state(self, ns):
        return self.providers[ns]

    def register_provider(self, ns):
        self._change("provider", ns)
        self.providers[ns] = "Registered"

    def rg_state(self, name):
        return self.rgs.get(name)

    def create_rg(self, name, location, tags):
        self._change("rg", name, location, dict(tags))
        self.rgs[name] = {"tags": dict(tags), "resources": 0}

    def capacity_id(self, resource_group, name):
        return self.capacities.get(name)

    def has_role(self, oid, role, scope):
        return (oid, role, scope) in self.roles

    def assign_role(self, oid, role, scope):
        self._change("role", oid, role, scope)
        self.roles.add((oid, role, scope))

    def role_definition(self, name, scope):
        self.role_scopes.append(scope)
        return bs.role_shape(self.role_defs[name]) if name in self.role_defs else None

    def create_role_definition(self, doc):
        self._change("role definition", doc["Name"])
        self.role_defs[doc["Name"]] = doc

    # Entra
    def find_app(self, name):
        return self.apps.get(name)

    def create_app(self, name):
        self._change("app", name)
        self.apps[name] = {"appId": self._id(), "id": self._id()}
        return self.apps[name]

    def find_sp(self, app_id):
        return self.sps.get(app_id)

    def create_sp(self, app_id):
        self._change("sp", app_id)
        self.sps[app_id] = self._id()
        return self.sps[app_id]

    def credentials(self, app_object_id):
        return list(self.creds.get(app_object_id, []))

    def add_credential(self, app_object_id, name, subject, description):
        self._change("credential+", app_object_id, name, subject)
        self.creds.setdefault(app_object_id, []).append({"id": self._id(), "name": name, "subject": subject, "issuer": bs.ISSUER})

    def remove_credential(self, app_object_id, credential_id):
        self._change("credential-", app_object_id, credential_id)
        self.creds[app_object_id] = [x for x in self.creds[app_object_id] if x["id"] != credential_id]

    def find_entra_group(self, name):
        return self.groups.get(name)

    def create_entra_group(self, name):
        self._change("group", name)
        self.groups[name] = self._id()
        return self.groups[name]

    def is_member(self, group_id, member_id):
        return (group_id, member_id) in self.members

    def add_member(self, group_id, member_id):
        self._change("member", group_id, member_id)
        self.members.add((group_id, member_id))

    def user_identity(self, who):
        if who is None:
            return {"id": USER_ID, "upn": USER_UPN}
        return {COLLEAGUE_UPN: {"id": COLLEAGUE_ID, "upn": COLLEAGUE_UPN}, COLLEAGUE_ID: {"id": COLLEAGUE_ID, "upn": COLLEAGUE_UPN},
                COLLEAGUE_ID.upper(): {"id": COLLEAGUE_ID, "upn": COLLEAGUE_UPN}, USER_ID: {"id": USER_ID, "upn": USER_UPN}}[who]

    # GitHub
    def oidc_prefix(self, repo):
        return self.prefixes[repo]

    def repo_permissions(self, repo):
        return self.repos.get(repo)

    def secret_names(self, repo):
        return set(self.secrets.get(repo, {}))

    def set_secret(self, repo, name, value):
        self._change("secret", repo, name)
        self.secrets.setdefault(repo, {})[name] = value

    def variables(self, repo):
        return dict(self.vars.get(repo, {}))

    def set_variable(self, repo, name, value):
        self._change("variable", repo, name, value)
        self.vars.setdefault(repo, {})[name] = value


def cfg(**over):
    return bs.Config(repo=PLATFORM, execute=over.pop("execute", True), subscription=over.pop("subscription", SUB), **over)


def run(cloud, config=None, quiet=True):
    lines = []
    rows = bs.apply(cloud, config or cfg(), lines.append)
    return rows, "\n".join(lines)


# ---- apply ------------------------------------------------------------------------------------------------------

def test_a_dry_run_reads_everything_and_changes_nothing():
    fake = FakeCloud()
    fake.readonly = True
    rows, text = run(fake, cfg(execute=False, subscription=""))
    assert rows.count("changed") == 0 and rows.count("would") >= 12 and "dry run" in text and fake.changes == []
    assert bs.NEW not in "".join(f"{r[1]}{r[2]}" for r in rows.rows)                                    # no placeholder leaks into what is said


def test_apply_builds_the_identity_the_trust_the_group_the_roles_and_the_settings_on_a_blank_tenant():
    fake = FakeCloud()
    rows, text = run(fake)
    assert fake.providers["Microsoft.Fabric"] == "Registered" and "rg-contoso-promptops" in fake.rgs
    app = fake.apps[bs.APP_NAME]
    sp = fake.sps[app["appId"]]
    assert [(c["name"], c["subject"]) for c in fake.creds[app["id"]]] == [("github-main", "repo:demo-org@1/platform@2:ref:refs/heads/main")]
    group = fake.groups[bs.SP_GROUP_DEFAULT]
    assert (group, sp) in fake.members
    assert fake.roles == {(sp, "Contributor", SCOPE), (sp, "Storage Blob Data Contributor", SCOPE)}
    assert fake.secrets[PLATFORM] == {"AZURE_CLIENT_ID": app["appId"], "AZURE_TENANT_ID": TENANT, "AZURE_SUBSCRIPTION_ID": SUB}
    v = fake.vars[PLATFORM]
    assert v["CD_ADMIN_OBJECT_IDS"] == USER_ID and v["CD_ADMIN_UPNS"] == USER_UPN                      # a workspace role takes the object id, a capacity administrator the UPN
    assert v["CD_PIPELINE_OBJECT_ID"] == sp and v["CD_RESOURCE_GROUP"] == "rg-contoso-promptops"
    assert v["CD_CAPACITY"] == "contosopromptopsf4" and v["CD_LOCATION"] == "eastus2" and v["CD_SP_GROUP_NAME"] == bs.SP_GROUP_DEFAULT
    assert v["CD_WORKSPACE_DEV"] == "Contoso-PromptOps-Dev" and v["CD_WORKSPACE_PROD"] == "Contoso-PromptOps-Prod"
    assert "CD_ENABLED" not in v and rows.count("would") == 0                                           # switching on is a separate, deliberate step
    assert app["appId"] not in text and TENANT not in text and SUB not in text                          # the ids that go into secrets are not printed


def test_the_roles_are_only_on_the_resource_group_and_nothing_wider():
    fake = FakeCloud()
    run(fake)
    assert {scope for (_o, _r, scope) in fake.roles} == {SCOPE}
    assert {r for (_o, r, _s) in fake.roles} == {"Contributor", "Storage Blob Data Contributor"}        # no Owner, no subscription-wide role


def test_running_it_again_changes_only_the_secrets_that_cannot_be_read_back():
    fake = FakeCloud()
    run(fake)
    before = len(fake.changes)
    rows, _ = run(fake)
    assert [c[0] for c in fake.changes[before:]] == ["secret"] * 3
    assert rows.count("would") == 0 and rows.count("changed") == 3


def trusted_elsewhere(fake, subject="repo:somebody/else:ref:refs/heads/main"):
    app = {"appId": fake._id(), "id": fake._id()}
    fake.apps[bs.APP_NAME] = app
    fake.sps[app["appId"]] = fake._id()
    fake.creds[app["id"]] = [{"id": "old", "name": "github-main", "subject": subject, "issuer": bs.ISSUER}]
    return app


def test_an_identity_that_trusts_another_repository_is_not_silently_rebound():
    """A typo in --repo must not hand a privileged identity to some other repository: it stops, in the dry run too, before anything changes."""
    fake = FakeCloud()
    app = trusted_elsewhere(fake)
    with pytest.raises(cd.CdError, match="already trusts 'repo:somebody/else:ref:refs/heads/main'.*--replace-trust.*Nothing was changed"):
        run(fake)
    with pytest.raises(cd.CdError, match="--replace-trust"):
        run(fake, cfg(execute=False, subscription=""))
    assert fake.changes == [] and [c["subject"] for c in fake.creds[app["id"]]] == ["repo:somebody/else:ref:refs/heads/main"]


def test_with_replace_trust_the_old_trust_is_replaced_not_added():
    fake = FakeCloud()
    app = trusted_elsewhere(fake)
    run(fake, cfg(replace_trust=True))
    assert [c["subject"] for c in fake.creds[app["id"]]] == ["repo:demo-org@1/platform@2:ref:refs/heads/main"]
    assert [c[0] for c in fake.changes if c[0].startswith("credential")] == ["credential-", "credential+"]


RIGHT_SUBJECT = "repo:demo-org@1/platform@2:ref:refs/heads/main"


def with_extra_credential(fake, name, subject="repo:somebody/else:ref:refs/heads/pwn", issuer=None):
    """The identity already trusts the right repository, and ALSO something else under another name than github-main."""
    app = {"appId": fake._id(), "id": fake._id()}
    fake.apps[bs.APP_NAME] = app
    fake.sps[app["appId"]] = fake._id()
    fake.creds[app["id"]] = [{"id": "right", "name": "github-main", "subject": RIGHT_SUBJECT, "issuer": bs.ISSUER},
                             {"id": "extra", "name": name, "subject": subject, "issuer": issuer or bs.ISSUER}]
    return app


@pytest.mark.parametrize("name", ["backup", "pull-requests"])
def test_a_second_credential_under_any_name_is_refused_not_only_one_called_github_main(name):
    """Only a credential with the name github-main used to be looked at: any other name kept another repository's right to sign in as the identity."""
    fake = FakeCloud()
    app = with_extra_credential(fake, name)
    with pytest.raises(cd.CdError, match=rf"already trusts 'repo:somebody/else:ref:refs/heads/pwn' \(credential '{name}'\).*--replace-trust.*Nothing was changed"):
        run(fake)
    with pytest.raises(cd.CdError, match="--replace-trust"):
        run(fake, cfg(execute=False, subscription=""))
    assert fake.changes == [] and len(fake.creds[app["id"]]) == 2


def test_replace_trust_removes_every_other_credential_and_keeps_the_right_one_untouched():
    fake = FakeCloud()
    app = with_extra_credential(fake, "backup")
    run(fake, cfg(replace_trust=True))
    assert [(c["id"], c["name"]) for c in fake.creds[app["id"]]] == [("right", "github-main")]
    assert [c[0] for c in fake.changes if c[0].startswith("credential")] == ["credential-"]             # the right one is neither removed nor added again


def test_the_right_subject_from_another_issuer_is_not_the_right_trust():
    fake = FakeCloud()
    app = with_extra_credential(fake, "x", subject=RIGHT_SUBJECT, issuer="https://issuer.example/other")
    fake.creds[app["id"]].pop(0)                                                                       # only the look-alike is there
    with pytest.raises(cd.CdError, match="--replace-trust"):
        run(fake)
    run(fake, cfg(replace_trust=True))
    assert [(c["subject"], c["issuer"]) for c in fake.creds[app["id"]]] == [(RIGHT_SUBJECT, bs.ISSUER)]


def test_a_dry_run_with_replace_trust_says_what_it_would_remove_and_changes_nothing():
    fake = FakeCloud()
    with_extra_credential(fake, "backup")
    fake.readonly = True
    rows, _text = run(fake, cfg(execute=False, subscription="", replace_trust=True))
    assert fake.changes == [] and ("would", "trust in 'repo:somebody/else:ref:refs/heads/pwn' (credential 'backup')", "would remove") in rows.rows


def test_the_trust_split_separates_the_right_credential_from_all_others():
    right = {"id": "1", "name": "a", "subject": RIGHT_SUBJECT, "issuer": bs.ISSUER}
    other = {"id": "2", "name": "b", "subject": "repo:x/y:ref:refs/heads/main", "issuer": bs.ISSUER}
    lookalike = {"id": "3", "name": "c", "subject": RIGHT_SUBJECT, "issuer": "https://other"}
    assert bs.trust_split([right, other, lookalike], RIGHT_SUBJECT) == ([right], [other, lookalike])
    assert bs.trust_split([], RIGHT_SUBJECT) == ([], [])


def test_a_repository_that_does_not_exist_or_cannot_be_administered_is_refused_before_anything_is_created():
    """The repository is read first, so a typo cannot leave an app, a group and a resource group behind."""
    fake = FakeCloud()
    del fake.repos[PLATFORM]
    with pytest.raises(cd.CdError, match="does not exist or is not visible to gh.*Nothing was changed"):
        run(fake)
    fake.repos[PLATFORM] = {"admin": False}
    with pytest.raises(cd.CdError, match="does not administer demo-org/platform.*Nothing was changed"):
        run(fake)
    assert fake.changes == [] and not fake.apps and not fake.rgs and not fake.groups


def test_an_existing_resource_group_that_is_not_empty_and_not_ours_is_refused():
    fake = FakeCloud()
    fake.rgs["rg-contoso-promptops"] = {"tags": {}, "resources": 7}
    with pytest.raises(cd.CdError, match="holds 7 resources and was not made by this project.*Contributor over all of it.*--adopt.*Nothing was changed"):
        run(fake)
    with pytest.raises(cd.CdError, match="holds 7 resources"):
        run(fake, cfg(execute=False, subscription=""))
    assert fake.changes == []


def test_an_empty_or_our_own_resource_group_is_accepted_and_adopt_accepts_the_rest():
    for state in ({"tags": {}, "resources": 0}, {"tags": {"managedBy": "contoso-fabric-iq-promptops"}, "resources": 12}):
        fake = FakeCloud()
        fake.rgs["rg-contoso-promptops"] = state
        rows, _ = run(fake)
        assert not any(c[0] == "rg" for c in fake.changes) and any("already there" in r[2] for r in rows.rows if "resource group" in r[1])
    fake = FakeCloud()
    fake.rgs["rg-contoso-promptops"] = {"tags": {}, "resources": 7}
    run(fake, cfg(adopt=True))
    assert fake.apps


def test_extra_administrators_are_resolved_by_name_or_id_deduplicated_and_lowercased():
    fake = FakeCloud()
    run(fake, cfg(admins=(COLLEAGUE_UPN, COLLEAGUE_ID.upper(), USER_ID)))
    assert fake.vars[PLATFORM]["CD_ADMIN_OBJECT_IDS"] == f"{USER_ID},{COLLEAGUE_ID}"
    assert fake.vars[PLATFORM]["CD_ADMIN_UPNS"] == f"{USER_UPN},{COLLEAGUE_UPN}"


def test_a_signed_in_service_principal_needs_an_administrator_named_and_nothing_is_changed_without_one():
    fake = FakeCloud(user_type="servicePrincipal")
    with pytest.raises(cd.CdError, match="no administrator could be determined"):
        run(fake)
    assert fake.changes == []                                                                            # it checks before it changes anything
    run(fake, cfg(admins=(COLLEAGUE_UPN,)))
    assert fake.vars[PLATFORM]["CD_ADMIN_OBJECT_IDS"] == COLLEAGUE_ID and fake.vars[PLATFORM]["CD_ADMIN_UPNS"] == COLLEAGUE_UPN


# ---- the safety refusals ----------------------------------------------------------------------------------

@pytest.mark.parametrize("config, message", [
    (dict(subscription=""), "--subscription is required with --execute"),
    (dict(subscription="99999999-0000-4000-8000-999999999999"), "different subscription than --subscription"),
])
def test_it_will_not_change_a_subscription_it_was_not_pointed_at(config, message):
    fake = FakeCloud()
    with pytest.raises(cd.CdError, match=message):
        run(fake, cfg(**config))
    assert fake.changes == []


def test_the_deny_list_is_for_files_so_it_does_not_choose_the_tenant(monkeypatch):
    """tools/denied_ids.sha256 lists the ids that must not appear in files, the demo's own among them. Using it as a 'never touch' list blocked the demo tenant."""
    monkeypatch.setattr(pii_guard, "load_denied", lambda: {hashlib.sha256(v.encode()).hexdigest() for v in (SUB, TENANT)})
    fake = FakeCloud()
    run(fake)
    assert fake.apps and fake.roles


@pytest.mark.parametrize("config, message", [
    (dict(capacity="Bad Name"), "CD_CAPACITY"),
    (dict(resource_group="rg."), "valid resource group name"),
    (dict(resource_group="a b"), "valid resource group name"),
    (dict(workspace_dev="Same", workspace_prod="Same"), "need different names"),
    (dict(group="ab"), "group name"),
    (dict(location="East US 2"), "CD_LOCATION"),
])
def test_bad_names_are_refused_before_anything_is_called(config, message):
    class Silent:
        def __getattr__(self, name):
            raise AssertionError(f"{name} was called")
    with pytest.raises(cd.CdError, match=message):
        bs.apply(Silent(), cfg(**config), lambda m: None)


# ---- enable -----------------------------------------------------------------------------------------------

def test_enable_refuses_until_apply_has_been_done_then_switches_on_once():
    fake = FakeCloud()
    with pytest.raises(cd.CdError, match="is not ready: variable CD_RESOURCE_GROUP.*secret AZURE_CLIENT_ID"):
        bs.enable(fake, cfg(), lambda m: None)
    run(fake)
    rows = bs.enable(fake, cfg(execute=False, subscription=""), lambda m: None)
    assert rows.count("would") == 1 and "CD_ENABLED" not in fake.vars[PLATFORM]
    bs.enable(fake, cfg(), lambda m: None)
    assert fake.vars[PLATFORM]["CD_ENABLED"] == "true"
    before = len(fake.changes)
    assert bs.enable(fake, cfg(), lambda m: None).count("ok") == 1 and len(fake.changes) == before


def test_enable_needs_the_pipeline_identity_and_the_administrators_not_just_the_names():
    fake = FakeCloud()
    run(fake)
    for name in ("CD_PIPELINE_OBJECT_ID", "CD_ADMIN_UPNS"):
        saved = fake.vars[PLATFORM].pop(name)
        with pytest.raises(cd.CdError, match=f"variable {name}"):
            bs.enable(fake, cfg(), lambda m: None)
        fake.vars[PLATFORM][name] = saved


# ---- consumer ---------------------------------------------------------------------------------------------

def consume(fake, repo=CONTENT, config=None):
    lines = []
    return bs.consumer(fake, config or cfg(), repo, lines.append)


def built(fake):
    """The platform workflow has run: the capacity exists."""
    fake.capacities["contosopromptopsf4"] = CAPACITY_ID


def test_a_consumer_needs_the_platform_first_and_an_existing_repository():
    fake = FakeCloud()
    with pytest.raises(cd.CdError, match="has no value for .*run `python -m tools.bootstrap apply"):
        consume(fake)
    run(fake)
    built(fake)
    saved = fake.repos.pop(CONTENT)
    with pytest.raises(cd.CdError, match="content repository demo-org/content does not exist or is not visible"):
        consume(fake)
    fake.repos[CONTENT] = {"admin": False}
    with pytest.raises(cd.CdError, match="does not administer demo-org/content"):
        consume(fake)
    fake.repos[CONTENT] = saved
    del fake.groups[bs.SP_GROUP_DEFAULT]
    with pytest.raises(cd.CdError, match="group .* does not exist"):
        consume(fake)


def test_a_consumer_needs_the_capacity_to_exist_and_nothing_is_created_without_it():
    """Its role is on the capacity itself, so it can run that capacity and cannot create another one."""
    fake = FakeCloud()
    run(fake)
    before = len(fake.changes)
    with pytest.raises(cd.CdError, match="capacity contosopromptopsf4 does not exist in rg-contoso-promptops yet.*build.*Nothing was changed"):
        consume(fake)
    assert len(fake.changes) == before and not fake.role_defs and CONTENT not in fake.secrets


def test_a_consumer_gets_its_own_identity_trust_role_on_the_capacity_and_settings():
    fake = FakeCloud()
    run(fake)
    built(fake)
    consume(fake)
    app = fake.apps[f"{bs.CONSUMER_APP_PREFIX}demo-org-content"]                                        # the owner is in the name: two repositories called 'content' cannot collide
    sp = fake.sps[app["appId"]]
    assert [c["subject"] for c in fake.creds[app["id"]]] == ["repo:demo-org@1/content@3:ref:refs/heads/main"]
    assert (fake.groups[bs.SP_GROUP_DEFAULT], sp) in fake.members
    assert {r for r in fake.roles if r[0] == sp} == {(sp, bs.ROLE_OPERATOR, CAPACITY_ID)}                # on the capacity itself, not on the resource group
    role = fake.role_defs[bs.ROLE_OPERATOR]
    assert role["Actions"] == list(bs.OPERATOR_ACTIONS) and role["AssignableScopes"] == [SCOPE] and role["NotActions"] == [] and role["DataActions"] == []
    assert not any("*" in a for a in role["Actions"]) and "delete" not in " ".join(role["Actions"])
    assert fake.vars[PLATFORM]["CD_CONSUMER_OBJECT_IDS"] == sp
    assert fake.secrets[CONTENT] == {"AZURE_CLIENT_ID": app["appId"], "AZURE_TENANT_ID": TENANT, "AZURE_SUBSCRIPTION_ID": SUB}
    assert {k: v for k, v in fake.vars[CONTENT].items()} == {k: fake.vars[PLATFORM][k] for k in bs.CONSUMER_VARIABLES}
    assert "CD_ENABLED" not in fake.vars[CONTENT]                                                       # the content repository decides when to switch on


def test_two_repositories_with_the_same_name_get_two_identities():
    fake = FakeCloud()
    run(fake)
    built(fake)
    other = "someone-else/content"
    fake.repos[other] = {"admin": True}
    fake.prefixes[other] = "repo:someone-else@9/content@8"
    consume(fake)
    consume(fake, other)
    assert len({n for n in fake.apps if n.startswith(bs.CONSUMER_APP_PREFIX)}) == 2


def test_a_consumer_is_listed_once_and_a_second_one_is_added():
    fake = FakeCloud()
    run(fake)
    built(fake)
    consume(fake)
    first = fake.vars[PLATFORM]["CD_CONSUMER_OBJECT_IDS"]
    before = [c for c in fake.changes if c[0] != "secret"]
    consume(fake)
    assert fake.vars[PLATFORM]["CD_CONSUMER_OBJECT_IDS"] == first and [c for c in fake.changes if c[0] != "secret"] == before
    other = "demo-org/other-content"
    fake.repos[other] = {"admin": True}
    fake.prefixes[other] = "repo:demo-org@1/other-content@4"
    consume(fake, other)
    ids = fake.vars[PLATFORM]["CD_CONSUMER_OBJECT_IDS"].split(",")
    assert len(ids) == 2 and ids[0] == first and len(fake.role_defs) == 1                               # the role is defined once, for both


def test_a_consumer_app_that_trusts_another_repository_is_not_rebound_either():
    fake = FakeCloud()
    run(fake)
    built(fake)
    name = f"{bs.CONSUMER_APP_PREFIX}demo-org-content"
    app = {"appId": fake._id(), "id": fake._id()}
    fake.apps[name] = app
    fake.sps[app["appId"]] = fake._id()
    fake.creds[app["id"]] = [{"id": "old", "name": "github-main", "subject": "repo:somebody/else:ref:refs/heads/main", "issuer": bs.ISSUER}]
    with pytest.raises(cd.CdError, match="--replace-trust"):
        consume(fake)


def test_a_consumer_app_with_a_second_credential_of_another_name_is_refused_before_anything_changes():
    fake = FakeCloud()
    run(fake)
    built(fake)
    name = f"{bs.CONSUMER_APP_PREFIX}demo-org-content"
    app = {"appId": fake._id(), "id": fake._id()}
    fake.apps[name] = app
    fake.sps[app["appId"]] = fake._id()
    fake.creds[app["id"]] = [{"id": "right", "name": "github-main", "subject": "repo:demo-org@1/content@3:ref:refs/heads/main", "issuer": bs.ISSUER},
                             {"id": "extra", "name": "other", "subject": "repo:somebody/else:ref:refs/heads/main", "issuer": bs.ISSUER}]
    before = len(fake.changes)
    with pytest.raises(cd.CdError, match=r"\(credential 'other'\).*--replace-trust.*Nothing was changed"):
        consume(fake)
    assert len(fake.changes) == before and not fake.role_defs


def az_role(actions=bs.OPERATOR_ACTIONS, scopes=(SCOPE,), not_actions=(), data_actions=(), name=bs.ROLE_OPERATOR):
    """A custom role the way `az role definition list` prints it."""
    return {"Name": name, "Actions": list(actions), "NotActions": list(not_actions), "DataActions": list(data_actions), "NotDataActions": [], "AssignableScopes": list(scopes)}


def test_a_role_that_already_has_the_consumer_roles_name_but_allows_more_is_refused_not_assigned():
    """The role is given to the content repository's identity: a role is not trusted by its name."""
    fake = FakeCloud()
    run(fake)
    built(fake)
    fake.role_defs[bs.ROLE_OPERATOR] = az_role(actions=[*bs.OPERATOR_ACTIONS, "Microsoft.Fabric/capacities/delete", "Microsoft.Authorization/roleAssignments/write"])
    before = len(fake.changes)
    with pytest.raises(cd.CdError, match=r"already exists, but it is not the role this project defines.*roleassignments/write.*Nothing was changed"):
        consume(fake)
    assert len(fake.changes) == before and not any(r[1] == bs.ROLE_OPERATOR for r in fake.roles) and CONTENT not in fake.secrets
    assert not any(n.startswith(bs.CONSUMER_APP_PREFIX) for n in fake.apps)                                  # nothing was created before the refusal


@pytest.mark.parametrize("change", [
    {"actions": [*bs.OPERATOR_ACTIONS[:3]]},                                                                    # fewer actions: it would not work
    {"scopes": [f"/subscriptions/{SUB}"]},                                                                      # assignable over the whole subscription
    {"not_actions": ["Microsoft.Fabric/capacities/suspend/action"]},
    {"data_actions": ["Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read"]},
])
def test_any_difference_in_the_existing_role_is_refused(change):
    fake = FakeCloud()
    run(fake)
    built(fake)
    fake.role_defs[bs.ROLE_OPERATOR] = az_role(**change)
    with pytest.raises(cd.CdError, match="not the role this project defines"):
        consume(fake)


def test_the_same_role_in_the_shape_az_prints_is_accepted_and_not_created_again():
    fake = FakeCloud()
    run(fake)
    built(fake)
    fake.role_defs[bs.ROLE_OPERATOR] = {"roleName": bs.ROLE_OPERATOR, "roleType": "CustomRole", "assignableScopes": [SCOPE.upper()],
                                        "permissions": [{"actions": [a.lower() for a in bs.OPERATOR_ACTIONS], "notActions": [], "dataActions": [], "notDataActions": [],
                                                         "condition": None, "conditionVersion": None}]}
    consume(fake)
    assert not any(c[0] == "role definition" for c in fake.changes) and any(r[1] == bs.ROLE_OPERATOR for r in fake.roles)


def test_the_role_is_looked_up_where_a_role_that_is_assignable_only_in_the_resource_group_can_be_seen():
    fake = FakeCloud()
    run(fake)
    built(fake)
    consume(fake)
    assert fake.role_scopes == [SCOPE]


def test_a_consumer_dry_run_changes_nothing():
    fake = FakeCloud()
    run(fake)
    built(fake)
    fake.readonly = True
    rows = consume(fake, config=cfg(execute=False, subscription=""))
    assert rows.count("changed") == 0 and rows.count("would") >= 8


# ---- the command line ---------------------------------------------------------------------------------------

def test_the_command_line_dry_run_ends_with_the_two_settings_a_fabric_admin_must_make(capsys):
    fake = FakeCloud()
    lines = []
    assert bs.main(["apply", "--repo", PLATFORM], cloud=fake, say=lines.append) == 0
    text = "\n".join(lines)
    assert "Dry run:" in text and "Nothing was changed" in text and fake.changes == []
    assert "Service principals can call Fabric public APIs" in text and "Service principals can create workspaces, connections, and deployment pipelines" in text
    assert bs.SP_GROUP_DEFAULT in text and "**" not in text and "--subscription <the subscription id>" in text


def test_the_command_line_refuses_to_execute_without_a_subscription():
    fake = FakeCloud()
    lines = []
    assert bs.main(["apply", "--repo", PLATFORM, "--execute"], cloud=fake, say=lines.append) == cd.EXIT_CONFIG
    assert "ERROR: --subscription is required" in "\n".join(lines) and fake.changes == []


def test_the_command_line_runs_all_three_commands():
    fake = FakeCloud()
    lines = []
    assert bs.main(["apply", "--repo", PLATFORM, "--subscription", SUB, "--execute", "--admin", COLLEAGUE_UPN], cloud=fake, say=lines.append) == 0
    assert bs.main(["enable", "--repo", PLATFORM, "--subscription", SUB, "--execute"], cloud=fake, say=lines.append) == 0
    built(fake)
    assert bs.main(["consumer", "--repo", PLATFORM, "--consumer", CONTENT, "--subscription", SUB, "--execute"], cloud=fake, say=lines.append) == 0
    assert fake.vars[PLATFORM]["CD_ENABLED"] == "true" and fake.vars[PLATFORM]["CD_ADMIN_OBJECT_IDS"] == f"{USER_ID},{COLLEAGUE_ID}"
    assert "action 'build' once" in lines[-1] and "changed" in "\n".join(lines)


def test_the_command_line_passes_the_new_flags_through():
    fake = FakeCloud()
    trusted_elsewhere(fake)
    fake.rgs["rg-contoso-promptops"] = {"tags": {}, "resources": 3}
    lines = []
    assert bs.main(["apply", "--repo", PLATFORM], cloud=fake, say=lines.append) == cd.EXIT_CONFIG and "holds 3 resources" in "\n".join(lines)
    assert bs.main(["apply", "--repo", PLATFORM, "--adopt"], cloud=fake, say=lines.append) == cd.EXIT_CONFIG and "--replace-trust" in "\n".join(lines)
    assert bs.main(["apply", "--repo", PLATFORM, "--adopt", "--replace-trust", "--subscription", SUB, "--execute"], cloud=fake, say=lines.append) == 0
    assert fake.vars[PLATFORM]["CD_PIPELINE_OBJECT_ID"]


@pytest.mark.parametrize("argv", [["apply", "--repo", "not a repo"], ["consumer", "--repo", PLATFORM, "--consumer", "nope"]])
def test_a_malformed_repository_is_a_configuration_error(argv):
    lines = []
    assert bs.main(argv, cloud=FakeCloud(), say=lines.append) == cd.EXIT_CONFIG and "must look like owner/name" in "\n".join(lines)


# ---- the real az and gh calls, with a recording runner ---------------------------------------------------------

class Runner:
    def __init__(self, *answers):
        self.answers, self.calls, self.files = list(answers), [], []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        for arg in cmd:
            if arg.startswith("@") and Path(arg[1:]).exists():
                self.files.append(Path(arg[1:]).read_bytes())                                            # the temporary file is gone after the call
        rc, out, err = self.answers.pop(0) if self.answers else (0, "", "")
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def cloud(*answers, **kw):
    sleeps = []
    c = bs.Cloud(runner=Runner(*answers), az="az", gh="gh", sleeper=sleeps.append, **kw)
    c.sleeps = sleeps
    return c


def test_az_gets_only_show_errors_and_gh_does_not():
    c = cloud((0, "true\n", ""), (0, "{}", ""), (0, "[]", ""), (0, "[]", ""))
    assert c.rg_state("rg") == {"tags": {}, "resources": 0}
    c.secret_names("o/r")
    az_cmd, gh_cmd = c.runner.calls[0][0], c.runner.calls[3][0]
    assert az_cmd == ["az", "group", "exists", "-n", "rg", "--only-show-errors"] and "--only-show-errors" not in gh_cmd


def test_a_resource_group_reports_its_tags_and_how_many_resources_it_holds_or_that_it_is_not_there():
    assert cloud((0, "false\n", "")).rg_state("rg") is None
    group = json.dumps({"name": "rg", "tags": {"managedBy": "x"}})
    resources = json.dumps([{"id": "1"}, {"id": "2"}, {"id": "3"}])
    c = cloud((0, "true\n", ""), (0, group, ""), (0, resources, ""))
    assert c.rg_state("rg") == {"tags": {"managedBy": "x"}, "resources": 3}
    assert c.runner.calls[2][0][:5] == ["az", "resource", "list", "-g", "rg"]                           # read-only calls only
    assert cloud((0, "true\n", ""), (0, json.dumps({"name": "rg"}), ""), (0, "[]", "")).rg_state("rg") == {"tags": {}, "resources": 0}


def test_the_capacity_is_found_by_name_or_reported_missing():
    c = cloud((0, CAPACITY_ID + "\n", ""))
    assert c.capacity_id("rg", "cap") == CAPACITY_ID
    cmd = c.runner.calls[0][0]
    assert cmd[:3] == ["az", "resource", "show"] and cmd[cmd.index("--resource-type") + 1] == "Microsoft.Fabric/capacities" and "-n" in cmd and "cap" in cmd
    assert cloud((1, "", "ResourceNotFound")).capacity_id("rg", "cap") is None and cloud((0, "\n", "")).capacity_id("rg", "cap") is None


def test_a_user_is_identified_by_object_id_and_by_user_principal_name():
    me = json.dumps({"id": USER_ID.upper(), "userPrincipalName": USER_UPN})
    c = cloud((0, me, ""), (0, me, ""))
    assert c.user_identity(None) == {"id": USER_ID, "upn": USER_UPN}
    assert c.user_identity(COLLEAGUE_UPN) == {"id": USER_ID, "upn": USER_UPN}
    assert c.runner.calls[0][0][:5] == ["az", "ad", "signed-in-user", "show", "-o"] or c.runner.calls[0][0][:4] == ["az", "ad", "signed-in-user", "show"]
    assert c.runner.calls[1][0][:6] == ["az", "ad", "user", "show", "--id", COLLEAGUE_UPN]


def test_repository_permissions_say_missing_read_only_or_admin():
    assert cloud((1, "", "HTTP 404")).repo_permissions("o/r") is None
    assert cloud((0, json.dumps({"permissions": {"admin": False, "push": True}}), "")).repo_permissions("o/r") == {"admin": False}
    assert cloud((0, json.dumps({"permissions": {"admin": True}}), "")).repo_permissions("o/r") == {"admin": True}
    assert cloud((0, "{}", "")).repo_permissions("o/r") == {"admin": False}


def test_a_secret_goes_in_through_standard_input_never_on_the_command_line():
    c = cloud()
    c.set_secret("o/r", "AZURE_CLIENT_ID", "the-secret-value")
    cmd, kw = c.runner.calls[0]
    assert cmd == ["gh", "secret", "set", "AZURE_CLIENT_ID", "--repo", "o/r"] and kw["input"] == "the-secret-value"
    assert not any("the-secret-value" in part for part in cmd)


def test_the_credential_file_is_plain_utf8_json_with_the_right_subject_and_no_byte_order_mark():
    c = cloud()
    c.add_credential("app-object-id", "github-main", "repo:o@1/r@2:ref:refs/heads/main", "GitHub Actions")
    cmd = c.runner.calls[0][0]
    assert cmd[:6] == ["az", "ad", "app", "federated-credential", "create", "--id"] and cmd[6] == "app-object-id" and cmd[7] == "--parameters"
    raw = c.runner.files[0]
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert json.loads(raw) == {"name": "github-main", "issuer": "https://token.actions.githubusercontent.com", "subject": "repo:o@1/r@2:ref:refs/heads/main",
                               "description": "GitHub Actions", "audiences": ["api://AzureADTokenExchange"]}
    assert not Path(cmd[8][1:]).exists()                                                                 # and it is removed afterwards


def test_a_role_assignment_waits_for_a_new_identity_to_be_known_and_treats_exists_as_done():
    c = cloud((1, "", "PrincipalNotFound: not in the directory"), (1, "", "Principal does not exist in the directory"), (0, "{}", ""))
    c.assign_role("oid", "Contributor", "/scope")
    assert len(c.runner.calls) == 3 and c.sleeps == [bs.PROPAGATION_WAIT_S] * 2
    cmd = c.runner.calls[0][0]
    assert cmd[:4] == ["az", "role", "assignment", "create"] and "--assignee-object-id" in cmd and cmd[cmd.index("--assignee-principal-type") + 1] == "ServicePrincipal"
    c = cloud((1, "", "RoleAssignmentExists: The role assignment already exists."))
    c.assign_role("oid", "Contributor", "/scope")
    assert c.sleeps == []


def test_a_role_assignment_gives_up_after_the_tries_and_on_any_other_error():
    c = cloud(*[(1, "", "PrincipalNotFound")] * bs.PROPAGATION_TRIES)
    with pytest.raises(cd.CdError, match="PrincipalNotFound"):
        c.assign_role("oid", "Contributor", "/scope")
    assert len(c.runner.calls) == bs.PROPAGATION_TRIES
    c = cloud((1, "", "AuthorizationFailed: you are not the owner"))
    with pytest.raises(cd.CdError, match="AuthorizationFailed"):
        c.assign_role("oid", "Contributor", "/scope")
    assert c.sleeps == []


def test_has_role_is_false_when_the_scope_or_the_identity_is_not_known_yet():
    assert cloud((1, "", "ResourceGroupNotFound")).has_role("oid", "Contributor", "/scope") is False
    assert cloud((0, '[{"id": "x"}]', "")).has_role("oid", "Contributor", "/scope") is True
    assert cloud((0, "[]", "")).has_role("oid", "Contributor", "/scope") is False


def test_a_group_member_is_added_again_without_error_and_a_missing_one_is_waited_for():
    c = cloud((1, "", "One or more added object references already exist for the following modified properties: 'members'."))
    c.add_member("g", "m")
    c = cloud((1, "", "Request_ResourceNotFound: resource does not exist"), (0, "", ""))
    c.add_member("g", "m")
    assert c.sleeps == [bs.PROPAGATION_WAIT_S]


def test_apps_and_groups_are_matched_by_their_exact_name_and_duplicates_are_refused():
    listing = json.dumps([{"displayName": "contoso-promptops-platform-old", "appId": "x", "id": "y"}, {"displayName": "contoso-promptops-platform", "appId": "a", "id": "b"}])
    assert cloud((0, listing, "")).find_app("contoso-promptops-platform") == {"appId": "a", "id": "b"}
    assert cloud((0, "[]", "")).find_app("nothing") is None
    twice = json.dumps([{"displayName": "n", "appId": "a", "id": "b"}, {"displayName": "n", "appId": "c", "id": "d"}])
    with pytest.raises(cd.CdError, match="2 app registrations are called 'n'"):
        cloud((0, twice, "")).find_app("n")
    groups = json.dumps([{"displayName": "g1", "id": "i1"}, {"displayName": "g10", "id": "i10"}])
    assert cloud((0, groups, "")).find_entra_group("g1") == "i1"


def test_a_missing_service_principal_is_none_not_an_error():
    assert cloud((1, "", "Resource does not exist")).find_sp("app") is None
    assert cloud((0, '{"id": "sp-1"}', "")).find_sp("app") == "sp-1"


def test_the_oidc_prefix_comes_from_github_and_falls_back_to_the_classic_form():
    assert cloud((0, '{"use_default": true, "sub_claim_prefix": "repo:o@1/r@2"}', "")).oidc_prefix("o/r") == "repo:o@1/r@2"
    assert cloud((0, '{"use_default": true}', "")).oidc_prefix("o/r") == "repo:o/r"
    c = cloud()
    c.oidc_prefix("o/r")
    assert c.runner.calls[0][0] == ["gh", "api", "repos/o/r/actions/oidc/customization/sub"]


def test_the_resource_group_is_created_with_the_demo_tags_and_a_missing_cli_is_said(monkeypatch):
    c = cloud()
    c.create_rg("rg", "eastus2", {"purpose": "demo", "autoPause": "true"})
    assert c.runner.calls[0][0][:9] == ["az", "group", "create", "-n", "rg", "-l", "eastus2", "--tags", "purpose=demo"]
    monkeypatch.setattr(bs.shutil, "which", lambda name: None)
    with pytest.raises(cd.CdError, match="the az command line is not installed"):
        bs.Cloud(runner=Runner(), az=None, gh=None).rg_state("rg")
    with pytest.raises(cd.CdError, match="the gh command line is not installed"):
        bs.Cloud(runner=Runner(), az=None, gh=None).repo_permissions("o/r")


def test_a_stuck_az_is_reported_not_waited_for_forever():
    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])
    with pytest.raises(cd.CdError, match="did not finish within 90 s"):
        bs.Cloud(runner=hang, az="az", gh="gh").account()


def test_the_account_is_read_from_az():
    body = json.dumps({"id": SUB, "tenantId": TENANT, "name": "Demo", "user": {"name": "admin@demo.test", "type": "user"}})
    assert cloud((0, body, "")).account() == {"id": SUB, "tenantId": TENANT, "name": "Demo", "user": "admin@demo.test", "userType": "user"}


def listed_role(actions=bs.OPERATOR_ACTIONS, scopes=(SCOPE,), name=bs.ROLE_OPERATOR):
    """One entry of `az role definition list`, as az 2.91 prints it."""
    return {"roleName": name, "roleType": "CustomRole", "assignableScopes": list(scopes),
            "permissions": [{"actions": list(actions), "notActions": [], "dataActions": [], "notDataActions": [], "condition": None, "conditionVersion": None}]}


def test_a_role_definition_is_looked_up_by_name_at_the_given_scope_among_custom_roles_only():
    c = cloud((0, json.dumps([listed_role()]), ""))
    shape = c.role_definition(bs.ROLE_OPERATOR, SCOPE)
    cmd = c.runner.calls[0][0]
    assert cmd[:4] == ["az", "role", "definition", "list"] and cmd[cmd.index("--name") + 1] == bs.ROLE_OPERATOR
    assert cmd[cmd.index("--custom-role-only") + 1] == "true" and cmd[cmd.index("--scope") + 1] == SCOPE
    assert shape == bs.role_shape(bs.operator_role(SUB, "rg-contoso-promptops"))


def test_a_missing_role_definition_is_none_and_only_the_exact_name_counts():
    assert cloud((0, "[]", "")).role_definition(bs.ROLE_OPERATOR, SCOPE) is None
    assert cloud((0, json.dumps([listed_role(name=bs.ROLE_OPERATOR + " (old)")]), "")).role_definition(bs.ROLE_OPERATOR, SCOPE) is None
    with pytest.raises(cd.CdError, match="2 custom roles are called"):
        cloud((0, json.dumps([listed_role(), listed_role()]), "")).role_definition(bs.ROLE_OPERATOR, SCOPE)


def test_a_role_shape_is_the_same_whichever_way_the_role_is_written_down():
    made = bs.role_shape(bs.operator_role(SUB, "rg-contoso-promptops"))
    printed = bs.role_shape(listed_role(actions=[a.upper() for a in reversed(bs.OPERATOR_ACTIONS)], scopes=[SCOPE.lower() + "/"]))
    assert made == printed and made["actions"] == sorted(a.lower() for a in bs.OPERATOR_ACTIONS) and made["assignableScopes"] == [SCOPE.lower()]
    assert made["notActions"] == made["dataActions"] == made["notDataActions"] == []
    assert bs.role_shape({"properties": listed_role()}) == made                                          # a nested `properties` envelope reads the same
    two_blocks = {"assignableScopes": [SCOPE], "permissions": [{"actions": list(bs.OPERATOR_ACTIONS[:2])}, {"actions": list(bs.OPERATOR_ACTIONS[2:]), "notActions": ["x/y"]}]}
    assert bs.role_shape(two_blocks)["notActions"] == ["x/y"] and bs.role_shape(two_blocks) != made
