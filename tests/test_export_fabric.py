import json

import pytest

from tools import export_fabric as ef

ONT_PLATFORM = json.dumps({"metadata": {"type": "Ontology", "displayName": "DemoOntology"}, "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"}})


def fake_build(tmp_path, tier="core"):
    """A minimal kit build output folder: <out>/<tier>/{ontology,semantic_model,agents,payloads}."""
    out = tmp_path / "build"
    b = out / tier
    files = {
        "ontology/.platform": ONT_PLATFORM,
        "ontology/database.tmdl": "database\n\tcompatibilityLevel: 1000000\n",
        "ontology/entities/Account.tmdl": "entity Account\n\tkeyProperty: AccountId\n",
        "semantic_model/definition.pbism": '{"version": "4.2"}\n',
        "semantic_model/definition/model.tmdl": "model Model\n",
        "semantic_model/definition/tables/Account.tmdl": "table Account\n",
        "payloads/semantic_model.create.json": json.dumps({"displayName": "DemoModel"}),
        "agents/grounding_brief.md": "# brief\n",
    }
    for rel, text in files.items():
        p = b / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return out


@pytest.fixture
def built(tmp_path):
    return fake_build(tmp_path) / "core"


def test_paths_and_item_names(built):
    files = ef.expected_files(built)
    assert set(files) == {
        "fabric/ontology/DemoOntology.Ontology/.platform",
        "fabric/ontology/DemoOntology.Ontology/database.tmdl",
        "fabric/ontology/DemoOntology.Ontology/entities/Account.tmdl",
        "fabric/semantic-model/DemoModel.SemanticModel/.platform",
        "fabric/semantic-model/DemoModel.SemanticModel/definition.pbism",
        "fabric/semantic-model/DemoModel.SemanticModel/definition/model.tmdl",
        "fabric/semantic-model/DemoModel.SemanticModel/definition/tables/Account.tmdl",
        "docs/generated/ontology-brief.md",
    }


def test_logical_ids_are_stable_and_unique(built):
    a = ef.expected_files(built)
    assert a == ef.expected_files(built)
    ont = json.loads(a["fabric/ontology/DemoOntology.Ontology/.platform"])
    sm = json.loads(a["fabric/semantic-model/DemoModel.SemanticModel/.platform"])
    assert ont["metadata"] == {"type": "Ontology", "displayName": "DemoOntology"}
    assert sm["metadata"] == {"type": "SemanticModel", "displayName": "DemoModel"}
    assert ont["config"]["logicalId"] != sm["config"]["logicalId"] != "00000000-0000-0000-0000-000000000000"
    assert ont["config"]["logicalId"] == ef.logical_id("Ontology", "DemoOntology")


def test_write_then_check_is_clean(tmp_path, built, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "fabric" / "reports" / "R.Report").mkdir(parents=True)
    (root / "fabric" / "reports" / "R.Report" / "report.json").write_text("{}", encoding="utf-8")
    assert ef.main(["--root", str(root), "--write", "--tier", "core", "--built", str(built.parent)]) == 0
    assert ef.main(["--root", str(root), "--check", "--tier", "core", "--built", str(built.parent)]) == 0
    assert (root / "fabric" / "reports" / "R.Report" / "report.json").exists()       # not managed, never touched
    assert "in sync" in capsys.readouterr().out


def test_check_reports_changed_missing_and_stray_files(tmp_path, built, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    args = ["--root", str(root), "--tier", "core", "--built", str(built.parent)]
    ef.main(["--write", *args])
    item = root / "fabric" / "ontology" / "DemoOntology.Ontology"
    (item / "entities" / "Account.tmdl").write_text("entity Account\n\tkeyProperty: Hacked\n", encoding="utf-8")
    (item / "database.tmdl").unlink()
    (item / "entities" / "Extra.tmdl").write_text("entity Extra\n", encoding="utf-8")
    assert ef.main(["--check", *args]) == 1
    out = capsys.readouterr().out
    assert "changed: fabric/ontology/DemoOntology.Ontology/entities/Account.tmdl" in out
    assert "missing: fabric/ontology/DemoOntology.Ontology/database.tmdl" in out
    assert "stray: fabric/ontology/DemoOntology.Ontology/entities/Extra.tmdl" in out


def test_write_removes_stray_files_and_empty_folders(tmp_path, built):
    root = tmp_path / "repo"
    root.mkdir()
    args = ["--root", str(root), "--tier", "core", "--built", str(built.parent)]
    ef.main(["--write", *args])
    old = root / "fabric" / "ontology" / "Old.Ontology" / "entities"
    old.mkdir(parents=True)
    (old / "Gone.tmdl").write_text("entity Gone\n", encoding="utf-8")
    ef.main(["--write", *args])
    assert not (root / "fabric" / "ontology" / "Old.Ontology").exists()


def test_crlf_in_the_repo_is_not_a_difference(tmp_path, built):
    root = tmp_path / "repo"
    root.mkdir()
    args = ["--root", str(root), "--tier", "core", "--built", str(built.parent)]
    ef.main(["--write", *args])
    f = root / "docs" / "generated" / "ontology-brief.md"
    f.write_bytes(f.read_bytes().replace(b"\n", b"\r\n"))
    assert ef.main(["--check", *args]) == 0


def test_a_folder_that_is_not_a_build_is_reported(tmp_path, capsys):
    assert ef.main(["--root", str(tmp_path), "--check", "--tier", "core", "--built", str(tmp_path / "nothing")]) == 2
    assert "missing" in capsys.readouterr().err


def test_default_tier_comes_from_the_kit_config(tmp_path):
    cfg = tmp_path / "kit" / "config"
    cfg.mkdir(parents=True)
    (cfg / "demo.config.json").write_text(json.dumps({"ontology": {"tier": "full"}}), encoding="utf-8")
    assert ef.read_tier(tmp_path) == "full"
    (cfg / "demo.config.json").write_text("{}", encoding="utf-8")
    assert ef.read_tier(tmp_path) == "core"


def test_repo_generated_files_are_in_sync_when_the_kit_can_run():
    pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    assert ef.main(["--check"]) == 0, "run python -m tools.export_fabric --write and commit the result"


def test_kit_output_does_not_depend_on_the_hash_seed(tmp_path):
    pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    runs = [ef.expected_files(ef.run_build(ef.ROOT, "smoke", tmp_path / f"s{seed}", hashseed=seed)) for seed in ("1", "98765")]
    assert runs[0] == runs[1]
