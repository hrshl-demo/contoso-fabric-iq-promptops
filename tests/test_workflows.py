"""Static checks on the workflow files: the properties that keep the capacity from billing unseen and keep the pipelines from running unasked."""
import re

import pytest
import yaml

from tools import cd, platform as pf
from tools.promptlib import ROOT

WORKFLOWS = ROOT / ".github" / "workflows"
FILES = sorted(WORKFLOWS.glob("*.yml"))


def load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def steps(doc, job):
    return doc["jobs"][job]["steps"]


def on(doc):
    return doc.get(True) or doc.get("on")                    # YAML reads the key `on` as the boolean True


LAST_LINE = [("cd-dev.yml", "deploy", "Last line of defence, pause the capacity", "python -m tools.cd capacity pause"),
             ("release.yml", "deploy-prod", "Last line of defence, pause the capacity", "python -m tools.cd capacity pause"),
             ("platform.yml", "build", "Last line of defence, settle the capacity deployment and make sure the capacity is paused", 'python -m tools.platform rescue --env "$TARGET"'),
             ("platform.yml", "destroy", "Last line of defence, settle the capacity deployment and make sure the capacity is paused", 'python -m tools.platform rescue --env "$TARGET"')]


@pytest.mark.parametrize("workflow, job, name, command", LAST_LINE)
def test_a_fresh_sign_in_comes_right_before_every_last_resort_pause(workflow, job, name, command):
    """A sign-in made with a federated credential lasts about an hour, and a long run plus its pause can outlast it."""
    seq = steps(load(workflow), job)
    i = next(n for n, s in enumerate(seq) if s.get("name") == name)
    login = seq[i - 1]
    assert login["uses"].startswith("azure/login@") and login.get("continue-on-error") is True and "Sign in again" in login["name"]
    assert login["if"] == seq[i]["if"] and "always()" in login["if"]
    assert seq[i]["run"] == command
    uploads = [n for n, s in enumerate(seq) if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert all(i < n for n in uploads)                                                                   # nothing else can use up the time the pause needs


def test_the_platform_rescue_step_is_the_only_thing_that_pauses_after_the_main_step():
    """`rescue` settles a capacity deployment that may still be running, so the plain guard must not be used instead."""
    for job in ("build", "destroy"):
        runs = " ".join(s.get("run", "") for s in steps(load("platform.yml"), job))
        assert "tools.platform rescue" in runs and "tools.cd capacity" not in runs


@pytest.mark.parametrize("workflow, job", [("cd-dev.yml", "deploy"), ("release.yml", "deploy-prod")])
def test_the_worst_case_first_pause_fits_inside_the_job_timeout(workflow, job):
    """About 6 minutes of set-up, the longest allowed deadline, the grace, and one full pause attempt, with a margin."""
    timeout = load(workflow)["jobs"][job]["timeout-minutes"]
    worst = 6 + cd.MAX_DEADLINE_MIN + (cd.GRACE_S + cd.PAUSE_TOTAL_S) / 60
    assert worst + 3 <= timeout, f"{worst:.1f} minutes in the worst case, the job allows {timeout}"


def test_the_platform_build_fits_the_settling_the_waiting_the_status_calls_and_the_pause_after_its_deadline():
    """Set-up, the longest deadline, then (in the worst case) cancelling and waiting for a capacity deployment, waiting for the capacity to be provisioned
    (or looking for a late one), the status calls, and a full pause."""
    timeout = load("platform.yml")["jobs"]["build"]["timeout-minutes"]
    worst = 6 + cd.MAX_DEADLINE_MIN + (cd.GRACE_S + pf.PAUSE_PREAMBLE_MAX_S + cd.PAUSE_TOTAL_S) / 60
    assert worst + 3 <= timeout, f"{worst:.1f} minutes in the worst case, the job allows {timeout}"


def test_the_platform_destroy_fits_a_resume_the_purge_the_deletes_and_the_pause():
    timeout = load("platform.yml")["jobs"]["destroy"]["timeout-minutes"]
    resume, purge, delete_capacity, delete_storage = cd.CAPACITY_TIMEOUT_S["resume"] / 60, 15, cd.CAPACITY_TIMEOUT_S["resume"] / 60, 10
    worst = 4 + resume + purge + delete_capacity + delete_storage + (pf.PAUSE_PREAMBLE_MAX_S + cd.PAUSE_TOTAL_S) / 60
    assert worst + 3 <= timeout, f"{worst:.1f} minutes in the worst case, the job allows {timeout}"


def test_the_capacity_guard_job_fits_a_status_call_and_a_full_pause():
    doc = load("capacity.yml")
    worst = 4 + (cd.STATUS_TIMEOUT_S + cd.PAUSE_TOTAL_S) / 60
    assert worst + 3 <= doc["jobs"]["capacity"]["timeout-minutes"]


def test_the_platform_passes_every_list_the_settings_need():
    env = load("platform.yml")["env"]
    for name in ("CD_RESOURCE_GROUP", "CD_CAPACITY", "CD_LOCATION", "CD_WORKSPACE_DEV", "CD_WORKSPACE_PROD", "CD_ADMIN_OBJECT_IDS", "CD_ADMIN_UPNS",
                 "CD_PIPELINE_OBJECT_ID", "CD_CONSUMER_OBJECT_IDS", "CD_SP_GROUP_NAME", "CD_DATA_SOURCE", "CD_DEADLINE_MIN"):
        assert env[name] == "${{ vars." + name + " }}", name


@pytest.mark.parametrize("workflow, job", [("platform.yml", "build"), ("platform.yml", "destroy"), ("cd-dev.yml", "deploy"), ("release.yml", "deploy-prod")])
def test_everything_that_touches_the_capacity_shares_one_queue(workflow, job):
    doc = load(workflow)
    group = doc["jobs"][job].get("concurrency") or doc.get("concurrency")
    assert group["group"] == "fabric-capacity" and group["cancel-in-progress"] is False                  # a running build is never cancelled by the next one


def test_the_platform_runs_only_when_someone_presses_the_button():
    doc = load("platform.yml")
    assert list(on(doc)) == ["workflow_dispatch"]                                                       # no push, no schedule
    inputs = on(doc)["workflow_dispatch"]["inputs"]
    assert inputs["action"]["default"] == "check" and inputs["action"]["options"] == ["check", "build", "destroy"]    # the default costs nothing
    assert inputs["keep_running"]["default"] is False and inputs["everything"]["default"] is False and inputs["reload_data"]["default"] is False
    assert doc["permissions"] == {"contents": "read"}


def test_the_platform_guards_refuse_before_anything_is_created():
    doc = load("platform.yml")
    script = steps(doc, "guards")[0]["run"]
    for needle in ('"$ENABLED" != "true"', 'refs/heads/main', 'is not an approver', "build) want=BUILD", "destroy) want=DESTROY", 'exit 1'):
        assert needle in script, needle
    assert doc["jobs"]["check"]["needs"] == "guards" and doc["jobs"]["destroy"]["needs"] == "guards"
    assert doc["jobs"]["build"]["needs"] == ["guards", "check"]                                          # the free check passes before the billable build starts
    assert doc["jobs"]["check"]["if"] == "${{ inputs.action != 'destroy' }}"
    assert doc["jobs"]["build"]["if"] == "${{ inputs.action == 'build' }}" and doc["jobs"]["destroy"]["if"] == "${{ inputs.action == 'destroy' }}"


def test_the_check_job_signs_in_and_creates_nothing_lasting():
    seq = steps(load("platform.yml"), "check")
    runs = " ".join(s.get("run", "") for s in seq)
    assert "tools.platform preflight" in runs and "tools.platform build" not in runs and "capacity resume" not in runs
    assert load("platform.yml")["jobs"]["check"]["permissions"] == {"contents": "read", "id-token": "write"}


def test_the_build_uploads_only_names_and_ids_not_the_data():
    upload = next(s for s in steps(load("platform.yml"), "build") if str(s.get("uses", "")).startswith("actions/upload-artifact@"))
    paths = upload["with"]["path"].split("\n")
    assert [p.strip().rsplit("/", 1)[-1] for p in paths if p.strip()] == ["platform-outputs.json", "ADMIN_APPROVAL.md"]
    assert upload["with"]["if-no-files-found"] == "ignore"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if "uses" in step and not step["uses"].startswith("./"):
                assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), f"{path.name}: {step['uses']} is not pinned to a commit"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_script_interpolates_an_expression(path):
    """Inputs and event data reach a script through `env:`, never through `${{ }}` inside `run:` (script injection)."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    for name, job in doc["jobs"].items():
        for step in job.get("steps", []):
            assert "${{" not in step.get("run", ""), f"{path.name}, job {name}, step {step.get('name')}: an expression inside run"
