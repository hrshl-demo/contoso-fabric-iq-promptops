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
