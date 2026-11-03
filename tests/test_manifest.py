from tests.conftest import GOOD, make_repo
from tools import manifest
from tools.promptlib import ROOT


def test_manifest_is_deterministic_and_complete(repo):
    a = manifest.build(repo)
    assert a == manifest.build(repo)
    entry = a["prompts"]["ontology.build"]
    assert entry["version"] == "1.0.0" and entry["outputs"]["files"] == 0
    assert a["skills"]["repo"] == "microsoft/skills-for-fabric"


def test_output_changes_move_the_tree_hash(repo):
    before = manifest.build(repo)["prompts"]["ontology.build"]["outputs"]
    spec = repo / "kit" / "model" / "a.yaml"
    spec.parent.mkdir(parents=True)
    spec.write_text("x: 1\n", encoding="utf-8")
    after = manifest.build(repo)["prompts"]["ontology.build"]["outputs"]
    assert after["files"] == 1 and after["sha256"] != before["sha256"]


def test_line_endings_do_not_change_hashes(repo):
    spec = repo / "kit" / "model" / "a.yaml"
    spec.parent.mkdir(parents=True)
    spec.write_bytes(b"x: 1\ny: 2\n")
    lf = manifest.build(repo)["prompts"]["ontology.build"]["outputs"]["sha256"]
    spec.write_bytes(b"x: 1\r\ny: 2\r\n")
    assert manifest.build(repo)["prompts"]["ontology.build"]["outputs"]["sha256"] == lf


def test_version_does_not_change_prompt_hash_but_body_does(tmp_path):
    make_repo(tmp_path)
    base = manifest.build(tmp_path)["prompts"]["ontology.build"]["promptSha256"]
    make_repo(tmp_path, GOOD.replace("version: 1.0.0", "version: 1.2.0"))
    assert manifest.build(tmp_path)["prompts"]["ontology.build"]["promptSha256"] == base
    make_repo(tmp_path, GOOD.replace("Build it", "Build it twice"))
    assert manifest.build(tmp_path)["prompts"]["ontology.build"]["promptSha256"] != base


def test_check_and_write(repo, capsys):
    assert manifest.main(["--root", str(repo), "--check"]) == 1
    assert manifest.main(["--root", str(repo), "--write"]) == 0
    assert manifest.main(["--root", str(repo), "--check"]) == 0
    make_repo(repo, GOOD.replace("Build it", "Build it differently"))
    assert manifest.main(["--root", str(repo), "--check"]) == 1


def test_repo_manifest_is_current():
    assert manifest.main(["--check"]) == 0, f"run python -m tools.manifest --write ({ROOT})"
