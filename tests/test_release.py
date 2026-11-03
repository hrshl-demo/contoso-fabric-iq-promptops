import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from tests.conftest import git
from tools import release
from tools.promptlib import ROOT

PENDING = """# Changelog

## Unreleased

### Prompts
- `ontology.build` 1.1.0: a change

## v1.0.0 — 2026-11-01

### Prompts
- `ontology.build` 1.0.0: first
"""

PREPARED = """# Changelog

## Unreleased

(nothing yet)

## v1.1.0 — 2026-11-04

### Prompts
- `ontology.build` 1.1.0: a change

## v1.0.0 — 2026-11-01

- `ontology.build` 1.0.0: first
"""


def make_root(tmp_path: Path, changelog: str = PREPARED) -> Path:
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    (tmp_path / "manifest.lock.json").write_text(json.dumps({"prompts": {
        "ontology.build": {"version": "1.1.0", "promptSha256": "a" * 64, "outputs": {"files": 3, "sha256": "b" * 64}},
        "reports.monthly-brief": {"version": "1.0.0", "promptSha256": "c" * 64, "outputs": {"files": 2, "sha256": "d" * 64}}}}), encoding="utf-8")
    (tmp_path / "skills.lock").write_text("marketplace: {repo: x, ref: y, commit: z}\n", encoding="utf-8")
    (tmp_path / "docs" / "generated").mkdir(parents=True)
    (tmp_path / "docs" / "generated" / "ontology-brief.md").write_bytes(b"# Brief\r\nline\r\n")
    (tmp_path / "fabric" / "ontology" / "X.Ontology").mkdir(parents=True)
    (tmp_path / "fabric" / "ontology" / "X.Ontology" / "definition.tmdl").write_bytes(b"entity A\r\n  key id\r\n")
    (tmp_path / "fabric" / "ontology" / "X.Ontology" / ".platform").write_text("{}\n", encoding="utf-8")
    (tmp_path / "fabric" / "ontology" / "__pycache__").mkdir()
    (tmp_path / "fabric" / "ontology" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (tmp_path / "prompts" / "ontology").mkdir(parents=True)
    (tmp_path / "prompts" / "ontology" / "02-build.md").write_text("---\nid: ontology.build\n---\nbody\n", encoding="utf-8")
    (tmp_path / "brief.pdf").write_bytes(b"%PDF-1.4\n%fake\n")
    return tmp_path


def test_parse_version():
    assert release.parse_version("v1.12.3") == (1, 12, 3)
    for bad in ("1.2.3", "v1.2", "v1.2.3-rc1", "V1.2.3", "", "v1.2.3 "):
        with pytest.raises(release.ReleaseError):
            release.parse_version(bad)


def test_lint_accepts_a_good_changelog():
    assert release.lint(PENDING) == []
    assert release.lint(PREPARED) == []
    assert release.lint("# Changelog\n- plain line\n") == []


@pytest.mark.parametrize("text, fragment", [
    ("# C\n## v1.0.0 — 2026-11-01\n- a\n## Unreleased\n- b\n", "must be the first section"),
    ("# C\n## Unreleased\n- a\n## Next up\n- b\n", "must be '## Unreleased' or"),
    ("# C\n## v1.0.0 — 2026-11-01\n- a\n## v1.1.0 — 2026-11-02\n- b\n", "newest first"),
    ("# C\n## v1.0.0 — 2026-11-01\n- a\n## v1.0.0 — 2026-10-01\n- b\n", "appears twice"),
    ("# C\n## v1.0.0 — 2026-11-01\ntext only\n", "no '- ' entries"),
    ("# C\n## v1.0.0 — 2026-13-45\n- a\n", "impossible date"),
    ("# C\n## Unreleased\n- a\n## Unreleased\n- b\n", "must be the first section"),
])
def test_lint_reports(text, fragment):
    assert any(fragment in p for p in release.lint(text)), release.lint(text)


def test_prepare_turns_unreleased_into_the_release_section():
    out = release.prepare(PENDING, "v1.1.0", "2026-11-04")
    assert out.index("## Unreleased") < out.index("## v1.1.0 — 2026-11-04") < out.index("## v1.0.0 — 2026-11-01")
    assert release.lint(out) == []
    assert release.unreleased(out).entries == 0
    assert release.find_section(out, "v1.1.0").entries == 1
    assert "`ontology.build` 1.1.0: a change" in release.find_section(out, "v1.1.0").body
    assert release.find_section(out, "v1.0.0").entries == 1


def test_prepare_handles_windows_line_endings():
    out = release.prepare(PENDING.replace("\n", "\r\n"), "v1.1.0", "2026-11-04")
    assert "\r" not in out and release.lint(out) == []


@pytest.mark.parametrize("text, version, date, fragment", [
    (PENDING, "v1.1", "2026-11-04", "must look like v1.2.3"),
    (PENDING, "v1.1.0", "04/11/2026", "must look like 2026-11-04"),
    (PREPARED, "v1.2.0", "2026-11-05", "no '- ' entries"),
    (PENDING, "v1.0.0", "2026-11-04", "already has a section"),
    (PENDING, "v0.9.0", "2026-11-04", "not newer"),
    ("# C\n## v1.0.0 — 2026-11-01\n- a\n", "v1.1.0", "2026-11-04", "no '## Unreleased'"),
])
def test_prepare_refuses(text, version, date, fragment):
    with pytest.raises(release.ReleaseError, match=fragment):
        release.prepare(text, version, date)


def test_gate_passes_for_a_prepared_release(tmp_path):
    root = make_root(tmp_path)
    assert release.gate(root, "v1.1.0", ["v1.0.0"]) == []
    assert release.gate(root, "v1.1.0", []) == []


def test_gate_reasons(tmp_path):
    root = make_root(tmp_path)
    assert any("already exists" in p for p in release.gate(root, "v1.1.0", ["v1.1.0"]))
    assert any("older than the newest tag" in p for p in release.gate(root, "v1.0.0", ["v1.1.0"]))
    assert any("no '## v1.2.0" in p for p in release.gate(root, "v1.2.0", ["v1.1.0"]))
    assert any("must look like" in p for p in release.gate(root, "1.1.0", []))
    (root / "CHANGELOG.md").write_text(PENDING, encoding="utf-8")
    problems = release.gate(root, "v1.1.0", ["v1.0.0"])
    assert any("no '## v1.1.0" in p for p in problems)
    assert any("still has 1 entries" in p for p in problems)
    (root / "manifest.lock.json").unlink()
    assert any("manifest.lock.json is missing" in p for p in release.gate(root, "v1.1.0", []))


def test_gate_reports_a_broken_changelog(tmp_path):
    root = make_root(tmp_path, PREPARED + "\n## Misc\n- x\n")
    assert any(p.startswith("CHANGELOG.md:") for p in release.gate(root, "v1.1.0", []))


def test_notes_for_a_release(tmp_path):
    root = make_root(tmp_path)
    text = release.notes(root, "v1.1.0", commit="abc123", run_url="https://example.test/run/1")
    assert text.startswith("## v1.1.0 — 2026-11-04")
    assert "`ontology.build` 1.1.0: a change" in text
    assert "| `ontology.build` | 1.1.0 | `aaaaaaaaaaaa` | 3 |" in text
    assert "| `reports.monthly-brief` | 1.0.0 | `cccccccccccc` | 2 |" in text
    assert "monthly-business-brief-v1.1.0.pdf" in text and "SHA256SUMS" in text
    assert "Commit: `abc123`" in text and "https://example.test/run/1" in text
    assert "## Unreleased" not in text


def test_notes_for_the_pending_changes(tmp_path):
    root = make_root(tmp_path, PENDING)
    text = release.notes(root, None)
    assert text.startswith("## Pending changes (preview, not released)")
    assert "`ontology.build` 1.1.0: a change" in text and "Attached files" not in text


def test_notes_need_the_section(tmp_path):
    with pytest.raises(release.ReleaseError):
        release.notes(make_root(tmp_path), "v2.0.0")


def test_assets_are_named_checksummed_and_reproducible(tmp_path):
    root = make_root(tmp_path)
    first = release.assets(root, "v1.1.0", root / "brief.pdf", tmp_path / "out1")
    assert [f.name for f in first] == ["monthly-business-brief-v1.1.0.pdf", "ontology-brief-v1.1.0.md", "fabric-items-v1.1.0.zip",
                                       "prompts-v1.1.0.zip", "SHA256SUMS"]
    sums = dict(line.split("  ")[::-1] for line in (tmp_path / "out1" / "SHA256SUMS").read_text().splitlines())
    assert set(sums) == {f.name for f in first[:-1]}
    for f in first[:-1]:
        assert hashlib.sha256(f.read_bytes()).hexdigest() == sums[f.name]
    second = release.assets(root, "v1.1.0", root / "brief.pdf", tmp_path / "out2")
    for a, b in zip(first, second):
        assert a.read_bytes() == b.read_bytes(), a.name


def test_asset_zips_are_clean(tmp_path):
    root = make_root(tmp_path)
    files = release.assets(root, "preview", root / "brief.pdf", tmp_path / "out")
    with zipfile.ZipFile(files[2]) as z:
        names = z.namelist()
        assert names == sorted(names) == ["fabric/ontology/X.Ontology/.platform", "fabric/ontology/X.Ontology/definition.tmdl"]
        assert {i.date_time for i in z.infolist()} == {release.ZIP_TIME}
        assert z.read("fabric/ontology/X.Ontology/definition.tmdl") == b"entity A\n  key id\n"
    with zipfile.ZipFile(files[3]) as z:
        assert z.namelist() == ["manifest.lock.json", "prompts/ontology/02-build.md", "skills.lock"]
    assert (tmp_path / "out" / "ontology-brief-preview.md").read_bytes() == b"# Brief\nline\n"


def test_assets_refuse_bad_input(tmp_path):
    root = make_root(tmp_path)
    (root / "not.pdf").write_bytes(b"hello")
    with pytest.raises(release.ReleaseError, match="not a PDF"):
        release.assets(root, "v1.1.0", root / "not.pdf", tmp_path / "o")
    with pytest.raises(release.ReleaseError, match="must look like"):
        release.assets(root, "latest", root / "brief.pdf", tmp_path / "o")
    (root / "docs" / "generated" / "ontology-brief.md").unlink()
    with pytest.raises(release.ReleaseError, match="ontology-brief"):
        release.assets(root, "v1.1.0", root / "brief.pdf", tmp_path / "o")


def test_cli_round_trip(tmp_path, capsys):
    root = make_root(tmp_path, PENDING)
    assert release.main(["--root", str(root), "lint"]) == 0
    assert release.main(["--root", str(root), "gate", "--version", "v1.1.0", "--tags", "v1.0.0"]) == 1
    assert release.main(["--root", str(root), "prepare", "--version", "v1.1.0", "--date", "2026-11-04"]) == 0
    assert release.main(["--root", str(root), "gate", "--version", "v1.1.0", "--tags", "v1.0.0"]) == 0
    assert release.main(["--root", str(root), "notes", "--version", "v1.1.0", "--out", str(tmp_path / "n" / "notes.md")]) == 0
    assert (tmp_path / "n" / "notes.md").read_text(encoding="utf-8").startswith("## v1.1.0 — 2026-11-04")
    assert release.main(["--root", str(root), "assets", "--version", "v1.1.0", "--brief", str(root / "brief.pdf"), "--out", str(tmp_path / "d")]) == 0
    assert release.main(["--root", str(root), "prepare", "--version", "v1.1.0"]) == 1
    out = capsys.readouterr().out
    assert "release gate v1.1.0: ok" in out and "nothing to release" in out


def test_this_repos_changelog_is_well_formed():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert release.lint(text) == []
    assert release.unreleased(text) is not None
    assert release.notes(ROOT, None).startswith("## Pending changes")


# ---- promotion: prod gets only what dev has had ---------------------------------------------------------------

@pytest.fixture
def history(tmp_path):
    """A repo with three commits: a deployable file, then a docs-only change, then a deployable change."""
    def head():
        return subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    (tmp_path / "fabric").mkdir()
    (tmp_path / "fabric" / "item.txt").write_text("v1", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "one")
    first = head()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text("words", encoding="utf-8")
    (tmp_path / "kit" / "tests").mkdir(parents=True)
    (tmp_path / "kit" / "tests" / "t.py").write_text("x", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "two")
    second = head()
    (tmp_path / "fabric" / "item.txt").write_text("v2", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "three")
    return tmp_path, first, second, head()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_promotion_ignores_changes_that_deploy_nothing(history):
    root, first, second, _third = history
    assert release.deployed_changes(root, first, second) == []
    assert release.deployed_changes(root, second, second) == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_promotion_lists_what_dev_has_not_had(history):
    root, first, second, third = history
    assert release.deployed_changes(root, first, third) == ["fabric/item.txt"]
    assert release.deployed_changes(root, second, third) == ["fabric/item.txt"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_promotion_refuses_odd_input(history):
    root, first, _second, third = history
    for bad in ("--output=x", "HEAD~1", "", "zzzzzzz"):
        with pytest.raises(release.ReleaseError, match="not a commit id"):
            release.deployed_changes(root, bad, third)
    with pytest.raises(release.ReleaseError, match="could not compare"):
        release.deployed_changes(root, "0" * 40, third)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_promotion_command_line(history, capsys):
    root, first, second, third = history
    assert release.main(["--root", str(root), "promotion", "--dev-sha", second, "--sha", second]) == 0
    assert release.main(["--root", str(root), "promotion", "--dev-sha", first, "--sha", third]) == 1
    assert release.main(["--root", str(root), "promotion", "--dev-sha", "", "--sha", third]) == 1
    out = capsys.readouterr().out
    assert "prod would get exactly what dev had" in out and "fabric/item.txt" in out and "no successful dev deployment yet" in out


# ---- the last dev deployment that really ran ------------------------------------------------------------------

def fake_api(runs, jobs_by_run):
    """A stand-in for `gh api`: the workflow runs (newest first) and the jobs of each run."""
    seen = []

    def api(path):
        seen.append(path)
        if "/workflows/" in path:
            return {"workflow_runs": runs}
        run_id = int(path.split("/runs/")[1].split("/")[0])
        return {"jobs": jobs_by_run[run_id]}
    api.seen = seen
    return api


def job(name, conclusion):
    return {"name": name, "conclusion": conclusion}


def test_a_skipped_deploy_job_is_not_a_deployment():
    """With CD_ENABLED unset the workflow run concludes 'success' although its only job was skipped; that must not license a prod deployment."""
    api = fake_api([{"id": 3, "head_sha": "c" * 40}, {"id": 2, "head_sha": "b" * 40}, {"id": 1, "head_sha": "a" * 40}],
                   {3: [job("Deploy to dev", "skipped")], 2: [job("Deploy to dev", "skipped")], 1: [job("Deploy to dev", "success")]})
    assert release.last_dev_deployment("hrshl-demo/some-repo", api) == "a" * 40


def test_the_newest_real_deployment_wins_and_nothing_else_is_fetched():
    api = fake_api([{"id": 3, "head_sha": "c" * 40}, {"id": 2, "head_sha": "b" * 40}],
                   {3: [job("Deploy to dev", "success")], 2: [job("Deploy to dev", "success")]})
    assert release.last_dev_deployment("o/r", api) == "c" * 40
    assert len(api.seen) == 2 and "branch=main" in api.seen[0] and "status=success" in api.seen[0] and "cd-dev.yml" in api.seen[0]


@pytest.mark.parametrize("jobs", [[], [job("Deploy to dev", "failure")], [job("Deploy to dev", None)], [job("Some other job", "success")]])
def test_no_deployment_when_the_deploy_job_did_not_succeed(jobs):
    assert release.last_dev_deployment("o/r", fake_api([{"id": 1, "head_sha": "a" * 40}], {1: jobs})) == ""
    assert release.last_dev_deployment("o/r", fake_api([], {})) == ""


@pytest.mark.parametrize("repo", ["", "no-slash", "a/b/c", "o/r; rm -rf", "o/r name", "../x/y"])
def test_the_repository_name_is_checked(repo):
    with pytest.raises(release.ReleaseError, match="owner/name"):
        release.last_dev_deployment(repo, fake_api([], {}))


def test_gh_api_errors_are_readable():
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "HTTP 404: Not Found")
    with pytest.raises(release.ReleaseError, match="gh api repos/o/r/actions/workflows/cd-dev.yml/runs failed: HTTP 404"):
        release.gh_api("repos/o/r/actions/workflows/cd-dev.yml/runs?branch=main", runner)

    def junk(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, "not json", "")
    with pytest.raises(release.ReleaseError, match="did not answer with JSON"):
        release.gh_api("repos/o/r", junk)


def test_dev_sha_command_prints_only_the_answer(monkeypatch, capsys):
    monkeypatch.setattr(release, "last_dev_deployment", lambda repo: "d" * 40)
    assert release.main(["dev-sha", "--repo", "o/r"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "d" * 40 + "\n" and captured.err == ""

    def boom(repo):
        raise release.ReleaseError("gh failed")
    monkeypatch.setattr(release, "last_dev_deployment", boom)
    assert release.main(["dev-sha", "--repo", "o/r"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "gh failed" in captured.err                                       # a shell capturing stdout must not swallow the reason


# ---- when must the data be loaded again -----------------------------------------------------------------------

@pytest.fixture
def data_history(tmp_path):
    def head():
        return subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    def commit(message, **files):
        for rel, text in files.items():
            f = tmp_path / rel.replace("__", "/")
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
        git(tmp_path, "add", "-A")
        git(tmp_path, "commit", "-q", "-m", message)
        return head()
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    base = commit("base", seed__a="1", fabric__x="1", docs__d="1")
    only_fabric = commit("fabric only", fabric__x="2")
    seed_change = commit("seed", seed__a="2")
    engine_change = commit("engine", kit__contoso_onto__e="2")
    return tmp_path, base, only_fabric, seed_change, engine_change


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_data_reload_decisions(data_history):
    root, base, only_fabric, seed_change, engine_change = data_history
    assert release.needs_data_reload(root, base, only_fabric) == (False, f"no data file changed since the last dev deployment ({base[:7]})")
    reload, why = release.needs_data_reload(root, only_fabric, seed_change)
    assert reload and "seed/a" in why
    assert release.needs_data_reload(root, only_fabric, engine_change)[0]                                  # the engine decides the tables too
    assert release.needs_data_reload(root, "", engine_change) == (True, "no successful dev deployment is known yet")
    reload, why = release.needs_data_reload(root, "f" * 40, engine_change)
    assert reload and "cannot be compared" in why


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_a_data_change_is_not_hidden_by_a_failed_run_in_between(data_history):
    """Push 1 changes the seed (its deployment failed), push 2 changes only fabric/: compared with the last deployment that really ran, the data is stale."""
    root, base, only_fabric, seed_change, engine_change = data_history
    assert release.needs_data_reload(root, base, engine_change)[0]
    assert not release.needs_data_reload(root, seed_change, seed_change)[0]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_reload_data_command_keeps_stdout_clean(data_history, capsys):
    root, base, only_fabric, seed_change, _engine = data_history
    assert release.main(["--root", str(root), "reload-data", "--since", base, "--sha", only_fabric]) == 0
    out, err = capsys.readouterr()
    assert out == "false\n" and "not needed" in err
    assert release.main(["--root", str(root), "reload-data", "--since", "", "--sha", seed_change]) == 0
    out, err = capsys.readouterr()
    assert out == "true\n" and "no successful dev deployment" in err
