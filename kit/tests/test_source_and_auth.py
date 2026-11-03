"""Source-repo access and tokens: the pinned commit is fetched read-only, the operator's checkout is never modified, and tokens
come from the host's az context."""
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from contoso_onto import oac, snapshot as S


def git(cwd, *args, env=None):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env={**os.environ, **(env or {})})
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture()
def source(tmp_path):
    """A stand-in for the operator's checkout: pinned commit on a side branch, working tree left on a later 'main' commit."""
    src = tmp_path / "home" / "contoso-retail-superapp4"
    (src / "data" / "releases" / "rel").mkdir(parents=True)
    git(src.parent, "init", "-q", "-b", "main", str(src))
    git(src, "config", "user.email", "t@t")
    git(src, "config", "user.name", "t")
    git(src, "config", "commit.gpgsign", "false")
    (src / "data" / "releases" / "rel" / "release.json").write_text('{"v": 1}')
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "pinned")
    pinned = git(src, "rev-parse", "HEAD")
    (src / "data" / "releases" / "rel" / "release.json").write_text('{"v": 2}')
    (src / "uncommitted.txt").write_text("operator work in progress")
    git(src, "commit", "-q", "-am", "later")
    git(src, "config", "core.sshCommand", "ssh -i /home/op/.ssh/deploy_key -o IdentitiesOnly=yes")
    git(src, "remote", "add", "origin", "git@github.com:acme/private-repo.git")
    return src, pinned


def fingerprint(repo: Path) -> dict:
    """Everything that would change if something wrote to the checkout or its .git directory."""
    h = {}
    for p in sorted(repo.rglob("*")):
        if p.is_file() and ".git" + os.sep + "objects" not in str(p):
            h[str(p.relative_to(repo))] = hashlib.sha256(p.read_bytes()).hexdigest()
    h["__objects"] = git(repo, "count-objects", "-v")
    return h


def test_pinned_commit_is_taken_from_the_operators_checkout_without_touching_it(source, tmp_path):
    src, pinned = source
    before = fingerprint(src)
    sources = S.pinned_sources(src, commit=pinned, public_url="https://example.invalid/none.git", env={})
    assert sources[0] == (src.resolve().as_uri(), None)                                            # cheapest first: its own object store
    dest = S.clone_pinned(tmp_path / "cache", commit=pinned, sources=sources)
    assert S.git_head(dest) == pinned
    assert json.loads((dest / "data" / "releases" / "rel" / "release.json").read_text()) == {"v": 1}    # the pinned data, not the later commit
    assert not (dest / "uncommitted.txt").exists()
    assert fingerprint(src) == before                                                              # the checkout is byte for byte unchanged
    assert git(src, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_a_matching_cache_is_reused_and_a_stale_one_is_refused(source, tmp_path):
    src, pinned = source
    cache = tmp_path / "cache"
    first = S.clone_pinned(cache, commit=pinned, sources=[(src.resolve().as_uri(), None)])
    assert S.clone_pinned(cache, commit=pinned, sources=[]) == first                                # no source needed any more
    with pytest.raises(RuntimeError, match="not at"):
        S.clone_pinned(cache, commit="f" * 40, sources=[])


def test_sources_fall_back_in_order_and_errors_never_leak_credentials(source, tmp_path):
    src, pinned = source
    bad = "https://user:s3cr3t-token@example.invalid/x.git"
    dest = S.clone_pinned(tmp_path / "c1", commit=pinned, sources=[(bad, None), (src.resolve().as_uri(), None)])
    assert S.git_head(dest) == pinned                                                              # the second source worked
    with pytest.raises(RuntimeError) as ei:
        S.clone_pinned(tmp_path / "c2", commit=pinned, sources=[(bad, None)])
    msg = str(ei.value)
    assert "s3cr3t-token" not in msg and "<credentials>" in msg and "--repo PATH" in msg
    assert not (tmp_path / "c2" / ".fetch-tmp").exists()                                           # no half-fetched leftovers


def test_a_wrong_commit_is_an_error_not_silent_drift(source, tmp_path):
    src, pinned = source
    with pytest.raises(RuntimeError, match="could not fetch"):
        S.clone_pinned(tmp_path / "c", commit="0" * 40, sources=[(src.resolve().as_uri(), None)])


def test_sources_reuse_the_checkouts_own_remote_and_ssh_command(source):
    src, pinned = source
    s = S.pinned_sources(src, commit="0" * 40, public_url="https://github.com/o/r", env={})          # the checkout lacks this commit: no file:// source
    assert s == [("git@github.com:acme/private-repo.git", "ssh -i /home/op/.ssh/deploy_key -o IdentitiesOnly=yes"), ("https://github.com/o/r", None)]
    s = S.pinned_sources(src, commit=pinned, env={"CONTOSO_REPO_URL": "git@github.com:me/mirror.git", "CONTOSO_REPO_SSH_COMMAND": "ssh -i /k"})
    assert s[0] == ("git@github.com:me/mirror.git", "ssh -i /k") and s[1][0].startswith("file:")
    assert S.pinned_sources(None, env={}) == [(S.REPO_URL, None)]


def test_a_vendored_copy_inside_another_repository_is_identified_by_its_marker(tmp_path):
    outer = tmp_path / "outer"
    seed = outer / "seed"
    seed.mkdir(parents=True)
    git(outer, "init", "-q")
    git(outer, "-c", "user.email=ci@example.com", "-c", "user.name=ci", "commit", "-q", "--allow-empty", "-m", "outer")
    assert S.git_head(seed) is None                                                                # not the enclosing repository's HEAD
    (seed / ".source-commit").write_text("abc1234\n", encoding="utf-8")
    assert S.git_head(seed) == "abc1234"
    assert S.git_head(outer) == git(outer, "rev-parse", "HEAD")                                    # a real top level still reports its HEAD


def test_credentials_in_urls_are_masked():
    assert S.mask("fatal: https://me:ghp_abc@github.com/o/r.git not found") == "fatal: https://<credentials>@github.com/o/r.git not found"
    assert S.mask("git@github.com:o/r.git") == "git@github.com:o/r.git"                             # ssh style URLs have no embedded secret


def test_has_commit_and_checkout_remote_are_read_only(source):
    src, pinned = source
    before = fingerprint(src)
    assert S.has_commit(src, pinned) and not S.has_commit(src, "0" * 40)
    assert S.checkout_remote(src) == ("git@github.com:acme/private-repo.git", "ssh -i /home/op/.ssh/deploy_key -o IdentitiesOnly=yes")
    assert S.checkout_remote(src.parent) == (None, None)                                           # not a repo: no answer, no error
    assert fingerprint(src) == before


# ---------------------------------------------------------------------------------------------- tokens
class FakeCli:
    def __init__(self, rc=0, expires_on=None):
        self.calls, self.rc, self.expires_on = [], rc, expires_on

    def __call__(self, args):
        self.calls.append(args)
        if self.rc:
            return self.rc, "", "ERROR: Please run 'az login' to setup account."
        body = {"accessToken": f"tok-{len(self.calls)}", "tokenType": "Bearer", "tenant": "t", "subscription": "s"}
        if self.expires_on is not None:
            body["expires_on"] = self.expires_on
        else:
            body["expiresOn"] = "2099-01-01 00:00:00.000000"
        return 0, json.dumps(body), ""


def test_tokens_come_from_the_az_context_per_audience_and_are_cached(monkeypatch):
    monkeypatch.delenv("FABRIC_TOKEN", raising=False)
    monkeypatch.delenv("PBI_TOKEN", raising=False)
    monkeypatch.delenv("CONTOSO_ONTO_AUTH", raising=False)
    cli, now = FakeCli(expires_on=10_000), [1000.0]
    get = oac.default_token_fn(cli, clock=lambda: now[0])
    assert get(oac.FABRIC_SCOPE) == "tok-1" and get(oac.FABRIC_SCOPE) == "tok-1"                   # cached
    assert cli.calls[0] == ["account", "get-access-token", "--resource", "https://api.fabric.microsoft.com", "-o", "json"]
    assert get("https://storage.azure.com/.default") == "tok-2"
    assert cli.calls[1][3] == "https://storage.azure.com"                                          # '/.default' stripped: az takes a resource, not a scope
    now[0] = 9_800.0                                                                               # within 5 minutes of expiry: refreshed
    assert get(oac.FABRIC_SCOPE) == "tok-3"


def test_expiry_falls_back_to_the_local_time_string(monkeypatch):
    monkeypatch.delenv("FABRIC_TOKEN", raising=False)
    cli = FakeCli()
    get = oac.default_token_fn(cli, clock=lambda: 1.0)
    assert get(oac.FABRIC_SCOPE) == "tok-1" and get(oac.FABRIC_SCOPE) == "tok-1" and len(cli.calls) == 1


def test_signed_out_az_gives_an_actionable_error(monkeypatch):
    monkeypatch.delenv("FABRIC_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="never signs in"):
        oac.default_token_fn(FakeCli(rc=1))(oac.FABRIC_SCOPE)


def test_static_environment_tokens_win(monkeypatch):
    monkeypatch.setenv("FABRIC_TOKEN", "from-env")
    cli = FakeCli()
    assert oac.default_token_fn(cli)(oac.FABRIC_SCOPE) == "from-env" and cli.calls == []


def test_default_credential_mode_needs_the_optional_package(monkeypatch):
    monkeypatch.delenv("FABRIC_TOKEN", raising=False)
    monkeypatch.setenv("CONTOSO_ONTO_AUTH", "default")
    import builtins
    real = builtins.__import__

    def deny(name, *a, **k):
        if name.startswith("azure.identity"):
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", deny)
    with pytest.raises(RuntimeError, match="requirements-optional.txt"):
        oac.default_token_fn(FakeCli())(oac.FABRIC_SCOPE)
