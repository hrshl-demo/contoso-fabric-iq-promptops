import shutil

import pytest

from tests.conftest import CHANGELOG, GOOD, git, make_repo
from tools import lint_prompts
from tools.promptlib import ROOT


def codes(findings, level="error"):
    return sorted(f.code for f in findings if f.level == level)


def test_valid_prompt_is_clean(repo):
    assert lint_prompts.lint(repo) == []


def test_repo_prompts_are_clean():
    assert [f for f in lint_prompts.lint(ROOT) if f.level == "error"] == []


def test_missing_key(tmp_path):
    make_repo(tmp_path, GOOD.replace("stage: build\n", ""))
    assert codes(lint_prompts.lint(tmp_path)) == ["P002"]


def test_frontmatter_missing(tmp_path):
    make_repo(tmp_path, "no frontmatter here\n")
    assert codes(lint_prompts.lint(tmp_path)) == ["P001"]


def test_id_area_folder_and_file_name(tmp_path):
    make_repo(tmp_path, GOOD.replace("id: ontology.build", "id: Ontology.Build"))
    assert "P003" in codes(lint_prompts.lint(tmp_path))
    make_repo(tmp_path, GOOD, rel="prompts/reports/02-build.md")
    assert "P003" in codes(lint_prompts.lint(tmp_path))
    make_repo(tmp_path, GOOD, rel="prompts/ontology/02-other.md")
    assert "P004" in codes(lint_prompts.lint(tmp_path))


def test_duplicate_id(tmp_path):
    make_repo(tmp_path)
    other = tmp_path / "prompts/ontology/03-build.md"
    other.write_text(GOOD, encoding="utf-8")
    assert "P005" in codes(lint_prompts.lint(tmp_path))


@pytest.mark.parametrize("bad", ["1.0", "v1.0.0", "01.0.0", "1.0.0-beta"])
def test_version_must_be_semver(tmp_path, bad):
    make_repo(tmp_path, GOOD.replace("version: 1.0.0", f"version: {bad}"))
    assert "P006" in codes(lint_prompts.lint(tmp_path))


def test_stage_skill_outputs_guardrails(tmp_path):
    make_repo(tmp_path, GOOD.replace("stage: build", "stage: deploy"))
    assert codes(lint_prompts.lint(tmp_path)) == ["P007"]
    make_repo(tmp_path, GOOD.replace("[fabriciq-ontology-cli]", "[unknown-skill]"))
    assert codes(lint_prompts.lint(tmp_path)) == ["P008"]
    for bad in ("../secrets/**", "/etc/**", "secrets/**", "docs\\\\x"):
        make_repo(tmp_path, GOOD.replace("- kit/model/**", f"- {bad}"))
        assert codes(lint_prompts.lint(tmp_path)) == ["P009"], bad
    make_repo(tmp_path, GOOD.replace("[pr-only, preview-before-deploy, synthetic-data-only]", "[pr-only]"))
    assert codes(lint_prompts.lint(tmp_path)) == ["P010"]


def test_body_rules(tmp_path):
    make_repo(tmp_path, GOOD.replace("Plan first", "Start"))
    assert codes(lint_prompts.lint(tmp_path)) == ["P011"]
    make_repo(tmp_path, GOOD.replace("pull request", "merge"))
    assert codes(lint_prompts.lint(tmp_path)) == ["P011"]
    make_repo(tmp_path, GOOD.replace("Do not deploy.", "Be quick."))
    assert codes(lint_prompts.lint(tmp_path)) == ["P011"]


def test_placeholders(tmp_path):
    make_repo(tmp_path, GOOD.replace("{{workspace}}", "{{workspace}} {{other}}"))
    assert codes(lint_prompts.lint(tmp_path)) == ["P012"]
    make_repo(tmp_path, GOOD.replace("{{workspace}}", "WS"))
    f = lint_prompts.lint(tmp_path)
    assert codes(f) == [] and codes(f, "warning") == ["P012"]


def test_changelog_line_required(tmp_path):
    make_repo(tmp_path, changelog="# Changelog\n")
    assert codes(lint_prompts.lint(tmp_path)) == ["P013"]
    make_repo(tmp_path, changelog="- `ontology.build` 2.0.0: other version\n")
    assert codes(lint_prompts.lint(tmp_path)) == ["P013"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_version_bump_against_base(tmp_path):
    make_repo(tmp_path)
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    path = tmp_path / "prompts/ontology/02-build.md"

    assert lint_prompts.lint(tmp_path, "main") == []                               # unchanged

    path.write_text(GOOD.replace("Build it", "Build it carefully"), encoding="utf-8")
    assert codes(lint_prompts.lint(tmp_path, "main")) == ["P014"]                  # changed, same version

    path.write_text(GOOD.replace("Build it", "Build it carefully").replace("version: 1.0.0", "version: 1.1.0"), encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG + "- `ontology.build` 1.1.0: more care\n", encoding="utf-8")
    assert lint_prompts.lint(tmp_path, "main") == []                               # bumped and logged

    path.write_text(GOOD.replace("version: 1.0.0", "version: 0.9.0"), encoding="utf-8")
    assert "P014" in codes(lint_prompts.lint(tmp_path, "main"))                    # downgrade is not a bump


def test_unknown_base_ref_is_a_warning(repo):
    f = lint_prompts.lint(repo, "origin/main")
    assert codes(f) == [] and codes(f, "warning") == ["P015"]
