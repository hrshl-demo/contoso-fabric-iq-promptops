import json

from tools import validate_fabric, wrappers
from tools.promptlib import ROOT

from tests.conftest import GOOD, make_repo


def test_wrappers_cover_every_prompt_and_stay_in_sync(repo):
    assert wrappers.check(repo) == 1                       # nothing generated yet
    assert wrappers.write(repo) == 0
    assert wrappers.check(repo) == 0
    assert (repo / ".github/prompts/ontology.build.prompt.md").exists()
    assert (repo / ".github/skills/ontology-build/SKILL.md").exists()
    make_repo(repo, GOOD.replace("version: 1.0.0", "version: 1.1.0"))
    assert wrappers.check(repo) == 1                       # version is in the wrapper: stale


def test_orphans_are_detected_and_removed(repo):
    wrappers.write(repo)
    (repo / "prompts/ontology/02-build.md").unlink()
    assert wrappers.check(repo) == 1
    wrappers.write(repo)
    assert not (repo / ".github/prompts/ontology.build.prompt.md").exists()
    assert wrappers.check(repo) == 0


def test_hand_written_files_are_never_touched(repo):
    own = repo / ".github/prompts/mine.prompt.md"
    own.parent.mkdir(parents=True)
    own.write_text("my own prompt\n", encoding="utf-8")
    wrappers.write(repo)
    assert own.exists() and wrappers.check(repo) == 0


def test_repo_wrappers_are_current():
    assert wrappers.check(ROOT) == 0


def test_empty_fabric_folder_passes(tmp_path):
    assert validate_fabric.validate(tmp_path) == []


def test_report_item_needs_pbir_parts(tmp_path):
    item = tmp_path / "fabric" / "reports" / "R.Report"
    item.mkdir(parents=True)
    (item / ".platform").write_text(json.dumps({"metadata": {"type": "Report", "displayName": "R"}}), encoding="utf-8")
    problems = validate_fabric.validate(tmp_path)
    assert any("definition.pbir" in p for p in problems) and any("definition/" in p for p in problems)


def test_bad_platform_json_is_reported(tmp_path):
    item = tmp_path / "fabric" / "X.Ontology"
    item.mkdir(parents=True)
    (item / ".platform").write_text("{nope", encoding="utf-8")
    assert any("not valid JSON" in p for p in validate_fabric.validate(tmp_path))
