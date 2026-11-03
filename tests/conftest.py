import subprocess
import textwrap
from pathlib import Path

import pytest

GOOD = textwrap.dedent("""\
    ---
    id: ontology.build
    title: Build the ontology
    version: 1.0.0
    area: ontology
    stage: build
    skills: [fabriciq-ontology-cli]
    inputs:
      workspace: WS
    outputs:
      - kit/model/**
    guardrails: [pr-only, preview-before-deploy, synthetic-data-only]
    ---
    Plan first and show me the plan. Build it in `{{workspace}}`.

    Do not deploy. Open a pull request and quote this prompt's id and version.
    """)

SKILLS_LOCK = "marketplace: {repo: microsoft/skills-for-fabric, ref: v0, commit: abc}\nskills_used: [fabriciq-ontology-cli, powerbi-report-cli]\n"
CHANGELOG = "# Changelog\n- `ontology.build` 1.0.0: first version\n"


def make_repo(root: Path, text: str = GOOD, rel: str = "prompts/ontology/02-build.md", changelog: str = CHANGELOG) -> Path:
    (root / "skills.lock").write_text(SKILLS_LOCK, encoding="utf-8")
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    return root


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.email=ci@example.com", "-c", "user.name=ci", *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)
