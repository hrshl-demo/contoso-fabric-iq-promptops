import json
import subprocess

import pytest

from tests.conftest import git
from tools import deploy_preview as dp

ENTITY_A = """/// A thing.
entity A
	lineageTag: 1
	backingTable: A
	keyProperty: Id

	/// Id.
	property Id
		dataType: string
		lineageTag: 2

		backingConfiguration
			valueColumn: A.Id
"""

ENTITY_A_PLUS = ENTITY_A + """
	/// Name.
	property Name
		dataType: string
		lineageTag: 3

		backingConfiguration
			valueColumn: A.Name
"""

ENTITY_B = ENTITY_A.replace("entity A", "entity B").replace("A.Id", "B.Id").replace("backingTable: A", "backingTable: B")

RELS = """entityRelationship 'A Of B'
	lineageTag: 9
	fromEntity: A
	toEntity: B
"""

RULE = "rule R1\n\tstatement: Never add limits to balances.\n\tlineageTag: 7\n"
TABLE = "table T\n\n\tmeasure 'Total' = SUM(T[x])\n\t\tformatString: 0\n"
TABLE_PLUS = TABLE + "\n\tmeasure 'Average' = AVERAGE(T[x])\n\t\tformatString: 0\n"


def platform(kind, name):
    return json.dumps({"metadata": {"type": kind, "displayName": name}})


def write_items(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def base_files():
    o = "fabric/ontology/Demo.Ontology/"
    m = "fabric/semantic-model/Model.SemanticModel/"
    return {
        o + ".platform": platform("Ontology", "Demo"),
        o + "entities/A.tmdl": ENTITY_A,
        o + "rules/R1.tmdl": RULE,
        m + ".platform": platform("SemanticModel", "Model"),
        m + "definition/tables/T.tmdl": TABLE,
    }


@pytest.fixture
def repo(tmp_path):
    write_items(tmp_path, base_files())
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def run(repo, base="main"):
    return dp.preview(dp.find_items(repo / "fabric"), dp.base_items(repo, base), base)


def test_no_changes(repo):
    assert "No Fabric item changes (2 item(s) unchanged)" in run(repo)


def test_new_items_are_created_when_the_base_has_no_fabric_folder(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    write_items(tmp_path, base_files())
    out = run(tmp_path)
    assert "| Demo | Ontology | **create** | 3 files |" in out and "| Model | SemanticModel | **create** | 2 files |" in out


def test_unknown_base_is_stated_and_everything_is_new(repo):
    out = run(repo, "origin/nope")
    assert "was not found" in out and out.count("**create**") == 2


def test_ontology_changes_are_summarised_by_entity_property_relationship_and_rule(repo):
    o = "fabric/ontology/Demo.Ontology/"
    write_items(repo, {
        o + "entities/A.tmdl": ENTITY_A_PLUS.replace("/// A thing.", "/// A thing, described better."),
        o + "entities/B.tmdl": ENTITY_B,
        o + "entityRelationships.tmdl": RELS,
        o + "rules/R1.tmdl": RULE.replace("Never", "Do not ever"),
    })
    out = run(repo)
    row = next(ln for ln in out.splitlines() if ln.startswith("| Demo |"))
    assert "**update**" in row
    assert "entities +1 ~1" in row and "properties +2" in row and "relationships +1" in row and "rules ~1" in row
    assert "entity descriptions ~1" in row
    assert "+ entities: B" in out and "~ entities: A" in out and "+ properties: A.Name, B.Id" in out and "~ entity descriptions: A" in out


def test_a_description_only_change_is_visible(repo):
    o = "fabric/ontology/Demo.Ontology/entities/A.tmdl"
    (repo / o).write_text(ENTITY_A.replace("/// A thing.", "/// A different description."), encoding="utf-8")
    row = next(ln for ln in run(repo).splitlines() if ln.startswith("| Demo |"))
    assert "entities ~1" in row and "entity descriptions ~1" in row and "properties" not in row


def test_semantic_model_measures_and_tables(repo):
    m = "fabric/semantic-model/Model.SemanticModel/definition/"
    write_items(repo, {m + "tables/T.tmdl": TABLE_PLUS, m + "tables/U.tmdl": "table U\n"})
    row = next(ln for ln in run(repo).splitlines() if ln.startswith("| Model |"))
    assert "tables +1 ~1" in row and "measures +1" in row


def test_removed_items(repo):
    for f in (repo / "fabric" / "semantic-model").rglob("*"):
        if f.is_file():
            f.unlink()
    assert "| Model | SemanticModel | **remove** | 2 files |" in run(repo)


def test_report_pages_and_visuals(tmp_path):
    r = "fabric/reports/R.Report/"
    page = lambda name: json.dumps({"displayName": name})
    files = {r + ".platform": platform("Report", "R"), r + "definition/pages/p1/page.json": page("Overview"),
             r + "definition/pages/p1/visuals/v1/visual.json": "{}"}
    write_items(tmp_path, files)
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    write_items(tmp_path, {r + "definition/pages/p2/page.json": page("Lending"), r + "definition/pages/p2/visuals/v1/visual.json": "{}",
                           r + "definition/pages/p2/visuals/v2/visual.json": "{}"})
    row = next(ln for ln in run(tmp_path).splitlines() if ln.startswith("| R |"))
    assert "pages +1" in row and "visuals +2" in row


def test_cli_prints_markdown(repo, capsys):
    assert dp.main(["--root", str(repo), "--base", "main"]) == 0
    assert capsys.readouterr().out.startswith("## Deployment preview")


def test_the_real_repo_items_can_be_summarised():
    out = dp.preview(dp.find_items(dp.ROOT / "fabric"), dp.base_items(dp.ROOT, "HEAD"), "HEAD")
    assert out.startswith("## Deployment preview")
