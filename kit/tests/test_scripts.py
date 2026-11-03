"""build.sh / wipe.sh / hostvm.sh: syntax, lint, the operator contract, and their control flow against stub `az` and `python`.
No test here touches Azure, Fabric, GitHub or PyPI."""
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parent.parent
SCRIPTS = ["build.sh", "wipe.sh", "scripts/lib.sh", "scripts/hostvm.sh"]


def _find_bash():
    cands = []
    if os.name == "nt":                                   # the WSL launcher in System32 is not a working bash; prefer Git Bash
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            if os.environ.get(var):
                cands.append(Path(os.environ[var]) / "Git" / "bin" / "bash.exe")
    if shutil.which("bash"):
        cands.append(Path(shutil.which("bash")))
    for c in cands:
        if c.exists():
            try:
                r = subprocess.run([str(c), "-c", "echo ok"], capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.SubprocessError):
                continue
            if r.returncode == 0 and r.stdout.strip() == "ok":
                return str(c)
    return None


BASH = _find_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="no working bash on this machine")

STUB_PY = """#!/usr/bin/env bash
# stub for the venv python: records its arguments and never touches the network
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then echo "pip $*" >> "${STUB_LOG:-/dev/null}"; exit "${STUB_PIP_RC:-0}"; fi
echo "python $*" >> "${STUB_LOG:-/dev/null}"
echo "stub output"
exit "${STUB_RC:-0}"
"""
STUB_AZ = """#!/usr/bin/env bash
echo "az $*" >> "${STUB_LOG:-/dev/null}"
exit "${STUB_AZ_RC:-0}"
"""


def write_lf(path: Path, text: str, mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
    path.chmod(mode)
    return path


@pytest.fixture(scope="session")
def lf_kit(tmp_path_factory) -> Path:
    """A copy of the scripts with LF line endings. The working copy may be CRLF on Windows; git normalises it (.gitattributes)."""
    dst = tmp_path_factory.mktemp("lfkit")
    for rel in SCRIPTS + ["requirements.txt", "requirements-optional.txt"]:
        write_lf(dst / rel, (KIT / rel).read_bytes().decode("utf-8"))
    return dst


def sh(args, cwd, env=None, timeout=180):
    e = {**os.environ, **(env or {})}
    return subprocess.run([BASH, *args], cwd=str(cwd), env=e, capture_output=True, text=True, timeout=timeout)


class World:
    def __init__(self, tmp_path: Path, kit: Path, venv: bool = True):
        self.kit, self.root = kit, tmp_path
        self.state = tmp_path / "state"
        self.bin = tmp_path / "bin"
        self.log = tmp_path / "stub.log"
        write_lf(self.bin / "az", STUB_AZ)
        if venv:
            self.seed_venv()

    def seed_venv(self):
        write_lf(self.state / "venv" / "bin" / "python", STUB_PY)
        r = sh(["-c", f'source "{self.kit.as_posix()}/scripts/lib.sh"; stamp_of "$(find_python)" "{self.kit.as_posix()}/requirements.txt"'], self.kit,
               {"CONTOSO_STATE_DIR": self.state.as_posix()})
        assert r.returncode == 0 and r.stdout.strip(), r.stderr
        (self.state / "venv" / ".stamp-base").write_text(r.stdout.strip())

    def env(self, **extra):
        e = {"CONTOSO_STATE_DIR": self.state.as_posix(), "CONTOSO_ALLOW_NON_LINUX": "1", "HOME": self.root.as_posix(),
             "PATH": os.pathsep.join([str(self.bin), os.environ["PATH"]]), "STUB_LOG": self.log.as_posix()}
        e.update(extra)
        return e

    def run(self, *args, **env):
        return sh(list(args), self.kit, self.env(**env))

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def python_calls(self) -> list[str]:
        return [c for c in self.calls() if c.startswith("python ")]


@pytest.fixture()
def world(tmp_path, lf_kit):
    return World(tmp_path, lf_kit)


# ---------------------------------------------------------------------------------------------- static
@pytest.mark.parametrize("rel", SCRIPTS)
def test_syntax(lf_kit, rel):
    r = sh(["-n", rel], lf_kit)
    assert r.returncode == 0, r.stderr


def test_shellcheck_is_clean(lf_kit):
    exe = shutil.which("shellcheck")
    if not exe:
        pytest.skip("shellcheck is not installed (pip install shellcheck-py)")
    r = subprocess.run([exe, "-x", "-S", "style", *SCRIPTS], cwd=str(lf_kit), capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_git_will_normalise_line_endings_to_lf():
    attrs = (KIT / ".gitattributes").read_text()
    assert "eol=lf" in attrs and "*.sh text eol=lf" in attrs


@pytest.mark.parametrize("rel", SCRIPTS)
def test_operator_contract_in_every_script(rel):
    text = (KIT / rel).read_text(encoding="utf-8")
    assert "set -Eeuo pipefail" in text
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    body = "\n".join(code)
    assert not re.search(r"^\s*set\s+-[a-zA-Z]*x", body, re.M) and not re.search(r"^\s*(bash|sh)\s+-[a-zA-Z]*x", body, re.M)   # never traces
    assert not re.search(r"^\s*az\s+(login|logout|account\s+set)\b", body, re.M)                  # never signs in/out or switches subscription
    assert not re.search(r"ghp_|gho_|github_pat_|-----BEGIN|AccountKey=|sig=", text)              # no secrets
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text)   # no tenant / subscription ids


def test_python_never_signs_in_or_switches_subscription():
    for p in (KIT / "contoso_onto").glob("*.py"):
        src = p.read_text(encoding="utf-8")
        assert not re.search(r"\[\s*[\"']login[\"']|\[\s*[\"']logout[\"']|\[\s*[\"']account[\"']\s*,\s*[\"']set[\"']", src), p.name


# ---------------------------------------------------------------------------------------------- help and option handling
def test_help_texts(lf_kit):
    b = sh(["build.sh", "-h"], lf_kit)
    assert b.returncode == 0 and all(x in b.stdout for x in ("--preflight", "--diagnose", "--pause", "wipe.sh", "Exit codes", "CONTOSO_CAPACITY_ADMINS"))
    assert all(x in b.stdout for x in ("BUILD", "APPROVE", "DEPLOY", "TEST", "--fresh", "--until", "--gate-wait", "--no-gate-wait", "--golden", "15 minutes"))
    w = sh(["wipe.sh", "--help"], lf_kit)
    assert w.returncode == 0 and "--plan" in w.stdout and "never deletes HostVM" in w.stdout and "AUDIT" in w.stdout and "build.sh --fresh" in w.stdout
    h = sh(["scripts/hostvm.sh", "-h"], lf_kit)
    assert h.returncode == 0 and "up" in h.stdout and "deallocate" in h.stdout and "private" in h.stdout.lower()
    assert sh(["scripts/hostvm.sh"], lf_kit).stdout == h.stdout                                  # no argument prints the usage as well


@pytest.mark.parametrize("args", [["--bogus"], ["--tier"], ["--preflight", "--tier", "core"], ["--diagnose", "--force-replace"], ["--pause", "--loader", "delta"],
                                  ["--diagnose", "--no-ont-annotations"], ["--until"], ["--preflight", "--until", "build"], ["--diagnose", "--fresh"],
                                  ["--pause", "--gate-wait", "5"], ["--resume", "--no-graph"], ["--preflight", "--golden", "none"], ["--gate-poll"]])
def test_bad_build_options_exit_2_before_anything_runs(world, args):
    r = world.run("build.sh", *args)
    assert r.returncode == 2, r.stdout + r.stderr
    assert world.calls() == []                                                                    # neither az nor python was called


def test_bad_wipe_options(world):
    assert world.run("wipe.sh", "--nope").returncode == 2
    assert world.run("wipe.sh", "--plan", "--purge-local").returncode == 2


def test_xtrace_is_refused(lf_kit):
    r = sh(["-x", "build.sh", "-h"], lf_kit)
    assert r.returncode == 2 and "xtrace" in r.stderr
    assert sh(["-x", "wipe.sh", "-h"], lf_kit).returncode == 2


def test_hostvm_validates_its_inputs(lf_kit):
    assert sh(["scripts/hostvm.sh", "up"], lf_kit, {"GH_REPO": "not a repo"}).returncode == 1
    assert sh(["scripts/hostvm.sh", "up"], lf_kit, {"GH_REPO": "o/r;rm -rf /"}).returncode == 1
    assert sh(["scripts/hostvm.sh", "up"], lf_kit, {"HOSTVM_USER": "Bad User"}).returncode == 1
    r = sh(["scripts/hostvm.sh", "frobnicate"], lf_kit)
    assert r.returncode == 1 and "unknown command" in r.stderr


# ---------------------------------------------------------------------------------------------- build.sh control flow
def test_preflight_runs_the_helper_and_returns_its_exit_code(world):
    r = world.run("build.sh", "--preflight", STUB_RC="3")
    assert r.returncode == 3, r.stdout + r.stderr                                                 # the helper's exit code passes through
    assert "stub output" in r.stdout and "log: " in r.stdout
    assert world.python_calls() == [f"python -m contoso_onto preflight --state-dir {world.state.as_posix()}"]
    assert any(c.startswith("az account show") for c in world.calls())                            # az was probed, never logged in
    logs = list((world.state / "runs").glob("preflight-*.log"))
    assert len(logs) == 1 and "stub output" in logs[0].read_text()


def test_diagnose_pause_and_resume_map_to_the_helper_commands(world):
    assert world.run("build.sh", "--diagnose").returncode == 0
    assert world.run("build.sh", "--pause").returncode == 0
    assert world.run("build.sh", "--resume").returncode == 0
    s = world.state.as_posix()
    assert world.python_calls() == [f"python -m contoso_onto diagnose --state-dir {s}", f"python -m contoso_onto capacity suspend --execute --state-dir {s}",
                                    f"python -m contoso_onto capacity resume --execute --state-dir {s}"]


def test_helper_passthrough_targets_this_hosts_state_and_swallows_everything_after_it(world):
    r = world.run("build.sh", "--helper", "verify", "--answers", "a.json", "--tier", "smoke")
    assert r.returncode == 0, r.stdout + r.stderr
    s = world.state.as_posix()
    assert world.python_calls() == [f"python -m contoso_onto verify --answers a.json --tier smoke --out {s}/out --state-dir {s}"]
    assert world.run("build.sh", "--helper").returncode == 2                                      # nothing to run
    assert len(world.python_calls()) == 1


def test_build_passes_its_options_and_prints_the_cost_reminder(world, tmp_path):
    r = world.run("build.sh", "--tier", "smoke", "--pause-after", "--force-replace", "--repo", "/some/checkout", "--allow-drift")
    assert r.returncode == 0, r.stdout + r.stderr
    assert world.python_calls() == [f"python -m contoso_onto up --tier smoke --pause --force-replace --repo /some/checkout --allow-drift "
                                    f"--state-dir {world.state.as_posix()}"]
    assert "bills while it runs" in r.stdout and "wipe.sh" in r.stdout and "existing sign-in" in r.stdout


def test_build_can_switch_the_graph_annotations_off(world):
    r = world.run("build.sh", "--no-ont-annotations")
    assert r.returncode == 0, r.stdout + r.stderr
    assert world.python_calls() == [f"python -m contoso_onto up --no-ont-annotations --state-dir {world.state.as_posix()}"]


def test_build_and_preflight_pass_the_descriptions_option_and_it_needs_a_value(world):
    r = world.run("build.sh", "--descriptions", "compact")
    assert r.returncode == 0, r.stdout + r.stderr
    assert world.python_calls() == [f"python -m contoso_onto up --descriptions compact --state-dir {world.state.as_posix()}"]
    r = world.run("build.sh", "--preflight", "--descriptions", "full")                                  # a read-only check may say which mode it assumes
    assert r.returncode == 0, r.stdout + r.stderr
    assert world.python_calls()[-1] == f"python -m contoso_onto preflight --descriptions full --state-dir {world.state.as_posix()}"
    assert world.run("build.sh", "--descriptions").returncode == 2 and len(world.python_calls()) == 2   # no value, nothing ran
    assert "--descriptions full|compact" in world.run("build.sh", "-h").stdout


def test_build_passes_the_stage_options_to_the_helper(world):
    r = world.run("build.sh", "--fresh", "--until", "approve", "--gate-wait", "5", "--gate-poll", "30", "--gate-timeout", "60", "--no-gate-wait", "--no-graph",
                  "--golden", "none", "--golden-parallel", "1", "--strict-golden", "--reload")
    assert r.returncode == 0, r.stdout + r.stderr
    assert world.python_calls() == [f"python -m contoso_onto up --fresh --until approve --gate-wait 5 --gate-poll 30 --gate-timeout 60 --no-gate-wait --no-graph "
                                    f"--golden none --golden-parallel 1 --strict-golden --reload --state-dir {world.state.as_posix()}"]
    assert "APPROVE" in r.stdout and "ONE human action" in r.stdout                                  # the run says up front that there is exactly one manual step


def test_partial_and_interrupted_runs_exit_4_and_other_codes_pass_through(world):
    assert world.run("build.sh", **{"STUB_RC": "4"}).returncode == 4
    assert world.run("build.sh", **{"STUB_RC": "130"}).returncode == 4                            # Ctrl-C = partial, re-run resumes
    assert world.run("build.sh", **{"STUB_RC": "143"}).returncode == 4
    assert world.run("build.sh", **{"STUB_RC": "6"}).returncode == 6                              # lock held by another run
    assert world.run("build.sh", **{"STUB_RC": "2"}).returncode == 2


def test_signed_out_az_blocks_before_python_runs(world):
    r = world.run("build.sh", **{"STUB_AZ_RC": "1"})
    assert r.returncode == 3 and "never signs in or out" in r.stderr
    assert world.python_calls() == []


def test_missing_az_is_blocked(tmp_path, lf_kit):
    w = World(tmp_path, lf_kit)
    (w.bin / "az").unlink()
    r = w.run("build.sh", "--preflight", PATH=os.pathsep.join([str(w.bin), str(Path(BASH).parent)]))
    assert r.returncode == 3 and "az" in r.stderr


def test_non_linux_hosts_are_refused_unless_overridden(world):
    snippet = f'source "{world.kit.as_posix()}/scripts/lib.sh"; uname() {{ echo Darwin; }}; init_state; check_host; echo passed'
    r = sh(["-c", snippet], world.kit, world.env(CONTOSO_ALLOW_NON_LINUX="0"))
    assert r.returncode == 3 and "Linux HostVM" in r.stderr and "passed" not in r.stdout
    r = sh(["-c", snippet], world.kit, world.env(CONTOSO_ALLOW_NON_LINUX="1"))
    assert r.returncode == 0 and "passed" in r.stdout                                              # the development override works


def check_host_with_free_mib(w: World, free_mib: int):
    """Run lib.sh's check_host with `df` replaced by a bash function (commands in MSYS /usr/bin cannot be shadowed through PATH)."""
    snippet = (f'source "{w.kit.as_posix()}/scripts/lib.sh"; '
               f"df() {{ printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\nstub 9999999 1 {free_mib * 1024} 1%% /\\n'; }}; "
               "init_state; check_host; echo passed")
    return sh(["-c", snippet], w.kit, w.env())


def test_free_space_guard_is_stricter_for_the_first_run_than_for_later_runs(tmp_path, lf_kit):
    first = World(tmp_path / "a", lf_kit, venv=False)                                             # no environment yet: wheels + install need ~1.5 GiB
    r = check_host_with_free_mib(first, 600)
    assert r.returncode == 3 and "MiB free" in r.stderr and "1536" in r.stderr and "passed" not in r.stdout
    assert check_host_with_free_mib(first, 2000).returncode == 0
    later = World(tmp_path / "b", lf_kit)                                                          # environment already there: 512 MiB is enough
    assert check_host_with_free_mib(later, 600).returncode == 0
    r = check_host_with_free_mib(later, 100)
    assert r.returncode == 3 and "512" in r.stderr


def test_run_logs_are_private_and_pruned_to_the_newest_30(world):
    runs = world.state / "runs"
    runs.mkdir(parents=True)
    now = time.time()
    for i in range(35):
        f = runs / f"old-{i:02d}.log"
        f.write_text("x")
        os.utime(f, (now - 10_000 + i, now - 10_000 + i))
    assert world.run("build.sh", "--diagnose").returncode == 0
    left = sorted(p.name for p in runs.glob("*.log"))
    assert len(left) == 30 and any(n.startswith("diagnose-") for n in left) and "old-00.log" not in left and "old-34.log" in left


def pip_calls(world) -> list[str]:
    return [c for c in world.calls() if c.startswith("pip ")]


def test_a_build_installs_the_delta_rs_fallback_once_and_azure_identity_only_when_asked(world):
    assert world.run("build.sh", "--preflight").returncode == 0
    assert world.run("build.sh", "--diagnose").returncode == 0
    assert not pip_calls(world)                                                                   # the base environment is already stamped; read-only commands install nothing
    r = world.run("build.sh")
    assert r.returncode == 0 and "installing the delta-rs fallback loader (deltalake==" in r.stdout + r.stderr
    pips = pip_calls(world)
    assert len(pips) == 1 and re.search(r"install .* deltalake==\S+$", pips[0]) and "--no-cache-dir" in pips[0] and "-r" not in pips[0].split()      # only the one pinned line
    assert "--retries 1 --timeout 10" in pips[0]                                                  # short: an unreachable PyPI must cost seconds on every run, not minutes
    assert (world.state / "venv" / ".stamp-delta").exists() and not (world.state / "venv" / ".stamp-optional").exists()
    assert world.run("build.sh").returncode == 0 and world.run("build.sh", "--loader", "delta").returncode == 0
    assert len(pip_calls(world)) == 1                                                             # the stamp prevents a reinstall; --loader delta needs nothing more
    assert world.run("build.sh", "--preflight", CONTOSO_ONTO_AUTH="default").returncode == 0      # azure-identity mode installs the whole optional file
    pips = pip_calls(world)
    assert len(pips) == 2 and "-r" in pips[1].split() and pips[1].endswith("requirements-optional.txt")
    assert (world.state / "venv" / ".stamp-optional").exists()
    assert world.run("build.sh").returncode == 0 and len(pip_calls(world)) == 2                   # the full optional install already holds deltalake


def test_the_delta_rs_fallback_is_pinned_in_one_place(lf_kit):
    pin = re.search(r"^(deltalake==\S+)", (lf_kit / "requirements-optional.txt").read_text(), re.M)
    assert pin
    r = sh(["-c", f'source "{lf_kit.as_posix()}/scripts/lib.sh"; delta_requirement'], lf_kit, {"CONTOSO_STATE_DIR": (lf_kit / "state").as_posix()})
    assert r.returncode == 0 and r.stdout.strip() == pin.group(1)


def test_helper_load_installs_the_delta_rs_fallback_and_the_other_helpers_do_not(world):
    assert world.run("build.sh", "--helper", "verify", "--answers", "a.json").returncode == 0
    assert not pip_calls(world)
    assert world.run("build.sh", "--helper", "load", "--layers", "silver").returncode == 0
    assert len(pip_calls(world)) == 1 and "deltalake==" in pip_calls(world)[0]


def test_a_failed_install_of_the_fallback_is_a_warning_for_a_build_and_fatal_for_the_delta_loader(world):
    r = world.run("build.sh", STUB_PIP_RC="1")
    assert r.returncode == 0 and "goes on without the delta-rs fallback" in r.stderr and len(world.python_calls()) == 1      # the build still ran
    assert not (world.state / "venv" / ".stamp-delta").exists()                                  # so the next run tries again
    r = world.run("build.sh", "--loader", "delta", STUB_PIP_RC="1")
    assert r.returncode == 3 and "--loader delta needs it" in r.stderr and len(world.python_calls()) == 1      # blocked before anything ran
    assert world.run("build.sh", "--helper", "load", "--loader", "delta", STUB_PIP_RC="1").returncode == 3


# ---------------------------------------------------------------------------------------------- wipe.sh control flow
def test_wipe_without_a_ledger_does_nothing_and_creates_nothing(tmp_path, lf_kit):
    state = tmp_path / "fresh-state"
    r = sh(["wipe.sh"], lf_kit, {"CONTOSO_STATE_DIR": state.as_posix(), "CONTOSO_ALLOW_NON_LINUX": "1"})
    assert r.returncode == 0 and "VERIFIED" in r.stdout and "nothing to wipe" in r.stdout
    assert not state.exists()


def test_purge_local_also_works_after_a_read_only_preflight_when_there_is_no_ledger(world):
    """A preflight creates only the private environment; --purge-local must be able to give that disk space back."""
    for d in ("cache", "out", "runs"):
        (world.state / d).mkdir(parents=True, exist_ok=True)
        (world.state / d / "f.txt").write_text("x")
    r = world.run("wipe.sh", "--purge-local")
    assert r.returncode == 0 and "purged" in r.stdout and "VERIFIED" in r.stdout
    assert not (world.state / "venv").exists() and not (world.state / "cache").exists() and not (world.state / "out").exists()
    assert (world.state / "runs" / "f.txt").exists()                                              # logs stay
    assert world.python_calls() == [] and not [c for c in world.calls() if c.startswith("az ")]  # needed neither python nor az
    again = world.run("wipe.sh", "--purge-local")
    assert again.returncode == 0 and "no local files to purge" in again.stdout


def seed_ledger(world: World):
    (world.state / "inventory").mkdir(parents=True, exist_ok=True)
    (world.state / "inventory" / "ledger.json").write_text("{}")
    for d in ("cache", "out"):
        (world.state / d).mkdir(parents=True, exist_ok=True)
        (world.state / d / "f.txt").write_text("x")


def test_wipe_plan_is_read_only_and_does_not_even_need_az(world):
    seed_ledger(world)
    r = world.run("wipe.sh", "--plan", STUB_AZ_RC="1")
    assert r.returncode == 0
    assert world.python_calls() == [f"python -m contoso_onto down --plan --state-dir {world.state.as_posix()}"]
    assert not [c for c in world.calls() if c.startswith("az ")]


def test_wipe_requires_a_signed_in_az(world):
    seed_ledger(world)
    r = world.run("wipe.sh", STUB_AZ_RC="1")
    assert r.returncode == 3 and world.python_calls() == []


def test_wipe_runs_the_helper_and_keeps_the_ledger_and_local_files_by_default(world):
    seed_ledger(world)
    r = world.run("wipe.sh")
    assert r.returncode == 0
    assert world.python_calls() == [f"python -m contoso_onto down --state-dir {world.state.as_posix()}"]
    assert (world.state / "venv").exists() and (world.state / "out" / "f.txt").exists() and (world.state / "inventory" / "ledger.json").exists()


def test_purge_local_removes_only_the_private_environment_cache_and_output_after_a_clean_wipe(world):
    seed_ledger(world)
    r = world.run("wipe.sh", "--purge-local")
    assert r.returncode == 0 and "purged" in r.stdout
    assert not (world.state / "venv").exists() and not (world.state / "cache").exists() and not (world.state / "out").exists()
    assert (world.state / "inventory" / "ledger.json").exists() and (world.state / "runs").is_dir()            # the record of what happened stays


def test_an_incomplete_wipe_keeps_everything_needed_to_retry(world):
    seed_ledger(world)
    r = world.run("wipe.sh", "--purge-local", STUB_RC="5")
    assert r.returncode == 5 and "not purging" in r.stderr
    assert (world.state / "venv").exists() and (world.state / "out" / "f.txt").exists()


# ---------------------------------------------------------------------------------------------- hostvm.sh (Run Command plumbing)
STUB_AZ_HOSTVM = r"""#!/usr/bin/env bash
echo "az $*" >> "${STUB_LOG}"
case "$1 $2" in
  "vm list") echo "RG-TEST" ;;
  "vm get-instance-view")
    case "$*" in
      *PowerState*) echo "${STUB_POWER:-PowerState/running}" ;;
      *) echo "Ready" ;;
    esac ;;
  "vm start" | "vm deallocate") ;;
  "vm run-command")
    if [ -n "${STUB_CONFLICT_ONCE:-}" ] && [ ! -e "${STUB_LOG}.conflict" ]; then
      : > "${STUB_LOG}.conflict"
      echo "ERROR: (Conflict) Run command extension execution is in progress. Please wait for completion before invoking a run command." >&2
      exit 1
    fi
    script=""; prev=""
    for a in "$@"; do
      if [ "$prev" = "--scripts" ]; then script="${a#@}"; fi
      prev="$a"
    done
    mkdir -p "${STUB_LOG}.d"
    n="$(ls "${STUB_LOG}.d" | wc -l | tr -d ' ')"
    src="$(cygpath -u "$script" 2>/dev/null || echo "$script")"
    cp "$src" "${STUB_LOG}.d/script-${n}.sh"
    echo "Enable succeeded: "
    echo "[stdout]"
    if grep -q '__USER__' "$src"; then echo "__USER__:azureuser"
    elif grep -q 'systemd-run' "$src"; then echo "started contoso-bank-exec-1; read it with: bash scripts/hostvm.sh logs"
    elif grep -q 'hostvm-exec.log' "$src"; then echo "[not running]"; echo "last line of the log"
    elif grep -q 'checkout:' "$src"; then echo "checkout: /home/azureuser/x @ abc1234 test (local changes: 0)"
    else echo "hello from hostvm"; fi
    if [ -z "${STUB_NO_MARKER:-}" ]; then echo "__EXIT__:${STUB_REMOTE_RC:-0}"; fi
    echo ""
    echo "[stderr]"
    ;;
  *) echo "unexpected az call: $*" >&2; exit 2 ;;
esac
"""
STUB_GH = """#!/usr/bin/env bash
echo "private=true pushed=2026-01-01T00:00:00Z"
"""


@pytest.fixture()
def hv(tmp_path, lf_kit):
    w = World(tmp_path, lf_kit, venv=False)
    write_lf(w.bin / "az", STUB_AZ_HOSTVM)
    write_lf(w.bin / "gh", STUB_GH)
    return w


def hv_run(w: World, *args, **env):
    return sh(["scripts/hostvm.sh", *args], w.kit, w.env(HOSTVM_RETRY_SLEEP="0", **env))


def remote_scripts(w: World) -> list[Path]:
    d = Path(str(w.log) + ".d")
    return sorted(d.glob("script-*.sh"), key=lambda p: int(p.stem.split("-")[1])) if d.exists() else []


def test_hostvm_status_reports_power_repo_and_checkout(hv):
    r = hv_run(hv, "status")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PowerState/running" in r.stdout and "private=true" in r.stdout and "checkout: /home/azureuser/x @ abc1234" in r.stdout
    assert "az vm list" in hv.calls()[0]                                                           # the resource group was looked up, not assumed


def test_hostvm_status_does_not_touch_a_stopped_vm(hv):
    r = hv_run(hv, "status", STUB_POWER="PowerState/deallocated")
    assert r.returncode == 0 and "PowerState/deallocated" in r.stdout
    assert not [c for c in hv.calls() if "run-command" in c or " vm start" in c]                  # nothing was started or run on it


def test_hostvm_exec_runs_as_the_operator_user_and_returns_the_remote_exit_code(hv):
    r = hv_run(hv, "exec", "--", "bash", "build.sh", "--preflight", STUB_REMOTE_RC="7")
    assert r.returncode == 7 and "hello from hostvm" in r.stdout and "__EXIT__" not in r.stdout     # the marker is parsed, not shown
    text = remote_scripts(hv)[-1].read_text()
    assert "OP_USER=azureuser" in text and "timeout 240" in text and "sudo -u" in text
    assert "trap 'echo \"__EXIT__:$?\"' EXIT" in text and text.startswith("#!/bin/bash")


def test_hostvm_exec_quotes_the_command_as_data(hv):
    nasty = "echo $(touch /tmp/pwned); rm -rf / # 'single' \"double\" `id` ${HOME}"
    r = hv_run(hv, "exec", "--", nasty)
    assert r.returncode == 0, r.stdout + r.stderr
    text = remote_scripts(hv)[-1].read_text()
    carrying = [ln for ln in text.splitlines() if "pwned" in ln]
    assert len(carrying) == 1 and carrying[0].startswith("REMOTE_CMD=")                           # one quoted assignment, never a command line
    # evaluate the generated assignment with real bash: the value must round-trip exactly and nothing may run
    probe = carrying[0] + '\nprintf "%s" "$REMOTE_CMD"\nif [ -e /tmp/pwned ]; then echo " PWNED"; fi\n'
    p = sh(["-c", probe], hv.kit, hv.env())
    assert p.returncode == 0 and p.stdout == nasty, p.stdout


def test_hostvm_exec_bg_detaches_through_systemd_run(hv):
    r = hv_run(hv, "exec", "--bg", "--", "bash build.sh --preflight")
    assert r.returncode == 0 and "started contoso-bank-exec" in r.stdout
    text = remote_scripts(hv)[-1].read_text()
    assert "systemd-run" in text and "--uid=" in text and "--collect" in text and "StandardOutput" in text and "append:" in text


def test_hostvm_logs_down_and_a_vm_that_is_not_running(hv):
    assert "[not running]" in hv_run(hv, "logs").stdout
    r = hv_run(hv, "exec", "--", "true", STUB_POWER="PowerState/deallocated")
    assert r.returncode == 1 and "is not running" in r.stderr
    r = hv_run(hv, "down")
    assert r.returncode == 0 and any(c.startswith("az vm deallocate") for c in hv.calls())


def test_hostvm_waits_out_a_busy_run_command_extension_and_reports_a_missing_result(hv):
    r = hv_run(hv, "exec", "--", "true", STUB_CONFLICT_ONCE="1")
    assert r.returncode == 0 and "hello from hostvm" in r.stdout                                    # retried after the Conflict
    assert len([c for c in hv.calls() if "run-command" in c]) >= 3                                  # detect user, conflict, real run
    r = hv_run(hv, "exec", "--", "true", STUB_NO_MARKER="1")
    assert r.returncode == 1 and "no result came back" in r.stderr
