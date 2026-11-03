#!/usr/bin/env bash
# shellcheck shell=bash
# lib.sh - helpers shared by build.sh and wipe.sh. Sourced, never run on its own.
#
# Operator contract (same shape as the superapp4 build.sh / wipe.sh on the HostVM):
#   - running the command IS the authorisation: no prompts
#   - strict mode, refuses to run under xtrace, reuses the host's az login, never signs in/out or changes the subscription
#   - state lives under ~/.local/state/contoso-bank-ontology-demo:
#       config/ inventory/ (ownership ledger) runs/ (logs) tombstones/ lock/ cache/ out/ venv/
#   - exit codes: 0 done | 2 bad config | 3 blocked, fix and re-run | 4 partial, re-run resumes
#                 5 wipe incomplete | 6 another build/wipe holds the lock
#   - the lock, the ledger and every Azure / Fabric call live in the Python helper (exactly one owner of the lock)

set -Eeuo pipefail

PROJECT="contoso-bank-ontology-demo"
LIB_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
KIT_DIR="$(cd -- "${LIB_DIR}/.." && pwd)"
STATE_ROOT="${CONTOSO_STATE_DIR:-${HOME:-}/.local/state/${PROJECT}}"
VENV_DIR="${STATE_ROOT}/venv"
RUNS_DIR="${STATE_ROOT}/runs"
VENV_PY=""

EX_CONFIG=2
EX_BLOCKED=3
EX_PARTIAL=4
# shellcheck disable=SC2034  # documented exit codes: the Python helper returns 5 (wipe incomplete) and 6 (lock held)
EX_WIPE=5
# shellcheck disable=SC2034
EX_BUSY=6
MIN_FREE_MIB_FIRST=1536   # first run: private Python environment (about 0.35 GiB) plus the wheels while they install
MIN_FREE_MIB=512

log() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
  local code="$1"
  shift
  printf 'ERROR: %s\n' "$*" >&2
  exit "${code}"
}

refuse_xtrace() {
  case "$-" in
    *x*) printf 'ERROR: refusing to run under xtrace (bash -x / set -x): tracing would write identifiers and tokens into logs.\n' >&2
         exit "${EX_CONFIG}" ;;
  esac
}

on_err() {
  local rc="$1" line="$2"
  trap - ERR
  printf 'ERROR: %s stopped unexpectedly at line %s (exit %s). Every step is idempotent: re-run to resume.\n' "${0##*/}" "${line}" "${rc}" >&2
  exit "${EX_PARTIAL}"
}

on_signal() {
  trap - INT TERM
  printf '\ninterrupted: nothing is lost, everything created so far is in the ledger. Re-run to resume.\n' >&2
  exit "${EX_PARTIAL}"
}

trap 'on_err $? $LINENO' ERR
trap on_signal INT TERM

init_state() {
  if [[ -z "${CONTOSO_STATE_DIR:-}" && -z "${HOME:-}" ]]; then
    die "${EX_CONFIG}" "HOME is not set; set CONTOSO_STATE_DIR to a writable directory"
  fi
  umask 077
  mkdir -p "${STATE_ROOT}"/{config,inventory,runs,tombstones,lock,cache,out} || die "${EX_BLOCKED}" "cannot create ${STATE_ROOT}"
  chmod 700 "${STATE_ROOT}" 2>/dev/null || true
}

venv_python() {
  if [[ -x "${VENV_DIR}/bin/python" ]]; then
    printf '%s' "${VENV_DIR}/bin/python"
  elif [[ -x "${VENV_DIR}/Scripts/python.exe" ]]; then
    printf '%s' "${VENV_DIR}/Scripts/python.exe"
  fi
}

check_host() {
  local os free_kib free_mib need="${MIN_FREE_MIB}"
  os="$(uname -s)"
  if [[ "${os}" != "Linux" && "${CONTOSO_ALLOW_NON_LINUX:-0}" != "1" ]]; then
    die "${EX_BLOCKED}" "run this on the Linux HostVM (this is ${os}); CONTOSO_ALLOW_NON_LINUX=1 overrides it for development"
  fi
  command -v git >/dev/null 2>&1 || die "${EX_BLOCKED}" "git is required"
  if [[ -z "$(venv_python)" ]]; then
    need="${MIN_FREE_MIB_FIRST}"
  fi
  free_kib="$(df -Pk "${STATE_ROOT}" 2>/dev/null | awk 'NR==2 {print $4}')" || free_kib=""
  if [[ "${free_kib}" =~ ^[0-9]+$ ]]; then
    free_mib=$((free_kib / 1024))
    if ((free_mib < need)); then
      die "${EX_BLOCKED}" "only ${free_mib} MiB free under ${STATE_ROOT}; at least ${need} MiB are needed. Free some space (for example ~/.cache) and re-run"
    fi
  fi
  if [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)" == "no" ]]; then
    warn "the system clock is not NTP-synchronised; Azure token validation can fail with clock skew"
  fi
}

require_az() {
  local rc=0
  command -v az >/dev/null 2>&1 || die "${EX_BLOCKED}" "Azure CLI (az) was not found on PATH"
  timeout 90 az account show --only-show-errors -o none >/dev/null 2>&1 || rc=$?
  case "${rc}" in
    0) ;;
    124) die "${EX_BLOCKED}" "'az account show' timed out; that is not the same as signed out, re-run" ;;
    *) die "${EX_BLOCKED}" "az is not signed in on this host. Sign in yourself; this kit never signs in or out and never changes the subscription" ;;
  esac
}

find_python() {
  local c
  for c in python3.13 python3.12 python3.11 python3 python; do
    if command -v "${c}" >/dev/null 2>&1 && "${c}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      command -v "${c}"
      return 0
    fi
  done
  return 1
}

stamp_of() { # stamp_of <python> <requirements file>: content hash plus the python minor version
  "$1" - "$2" <<'PY'
import hashlib
import sys

data = open(sys.argv[1], "rb").read()
print(hashlib.sha256(data).hexdigest()[:16] + "-py%d.%d" % sys.version_info[:2])
PY
}

pip_install() { # pip_install <venv python> <requirements file>
  if ! "$1" -m pip install --quiet --no-cache-dir --disable-pip-version-check --no-input -r "$2"; then
    die "${EX_BLOCKED}" "pip could not install $(basename "$2") (network access to PyPI, or disk space). Fix that and re-run; nothing in Azure was touched"
  fi
}

delta_requirement() { # the pinned deltalake line of requirements-optional.txt, which stays the one place the pin lives
  sed -n 's/^\(deltalake==[^[:space:]#]*\).*/\1/p' "${KIT_DIR}/requirements-optional.txt"
}

bootstrap_venv() { # bootstrap_venv [want_optional] [want_delta]
  # want_optional=1: also install requirements-optional.txt (deltalake and azure-identity).
  # want_delta=1: only its deltalake line, the per-table fallback loader; a pip failure is a warning (the base environment is enough to build, a table
  # the Load Table API refuses would then stop the build). want_delta=2: the same, but a pip failure is fatal (--loader delta needs it).
  local want_optional="${1:-0}" want_delta="${2:-0}" py base_stamp opt_stamp vpy spec
  py="$(find_python)" || die "${EX_BLOCKED}" "Python 3.11 or newer was not found (the HostVM has python3.12)"
  base_stamp="$(stamp_of "${py}" "${KIT_DIR}/requirements.txt")"
  vpy="$(venv_python)"
  if [[ -z "${vpy}" || "$(cat "${VENV_DIR}/.stamp-base" 2>/dev/null || true)" != "${base_stamp}" ]]; then
    log "preparing the private Python environment (${VENV_DIR}); the first run takes a minute or two"
    case "${VENV_DIR}" in
      "${STATE_ROOT}"/*) rm -rf "${VENV_DIR}" ;;
      *) die "${EX_CONFIG}" "refusing to remove ${VENV_DIR}: it is not under ${STATE_ROOT}" ;;
    esac
    "${py}" -m venv "${VENV_DIR}" || die "${EX_BLOCKED}" "could not create a virtual environment (Debian/Ubuntu: sudo apt-get install -y python3-venv)"
    vpy="$(venv_python)"
    pip_install "${vpy}" "${KIT_DIR}/requirements.txt"
    printf '%s' "${base_stamp}" >"${VENV_DIR}/.stamp-base"
    rm -f "${VENV_DIR}/.stamp-optional" "${VENV_DIR}/.stamp-delta"
  fi
  opt_stamp="$(stamp_of "${py}" "${KIT_DIR}/requirements-optional.txt")"
  if [[ "${want_optional}" == "1" ]]; then
    if [[ "$(cat "${VENV_DIR}/.stamp-optional" 2>/dev/null || true)" != "${opt_stamp}" ]]; then
      log "installing the optional packages (deltalake fallback loader, azure-identity)"
      pip_install "${vpy}" "${KIT_DIR}/requirements-optional.txt"
      printf '%s' "${opt_stamp}" >"${VENV_DIR}/.stamp-optional"
    fi
  elif [[ "${want_delta}" != "0" ]]; then
    if [[ "$(cat "${VENV_DIR}/.stamp-delta" 2>/dev/null || true)" != "${opt_stamp}" && "$(cat "${VENV_DIR}/.stamp-optional" 2>/dev/null || true)" != "${opt_stamp}" ]]; then
      spec="$(delta_requirement)"
      [[ -n "${spec}" ]] || die "${EX_CONFIG}" "requirements-optional.txt has no pinned deltalake line"
      log "installing the delta-rs fallback loader (${spec}, about 100 MB); the build writes a table with it only if the Load Table API refuses that table"
      if "${vpy}" -m pip install --quiet --no-cache-dir --disable-pip-version-check --no-input --retries 1 --timeout 10 "${spec}"; then      # short: an unreachable PyPI costs seconds on every run, not minutes
        printf '%s' "${opt_stamp}" >"${VENV_DIR}/.stamp-delta"
      elif [[ "${want_delta}" == "2" ]]; then
        die "${EX_BLOCKED}" "pip could not install ${spec} (network access to PyPI, or disk space), and --loader delta needs it. Fix that and re-run; nothing in Azure was touched"
      else
        warn "pip could not install ${spec}: the build goes on without the delta-rs fallback (a table the Load Table API refuses would stop it); the next run tries again"
      fi
    fi
  fi
  VENV_PY="${vpy}"
}

prune_logs() { # keep the 30 newest run logs
  local f n=0
  while IFS= read -r f; do
    n=$((n + 1))
    if ((n > 30)); then
      rm -f -- "${f}"
    fi
  done < <(ls -1t "${RUNS_DIR}"/*.log 2>/dev/null || true)
}

run_kit() { # run_kit <subcommand> [args...]: runs the Python helper, tees a private per-run log, returns its exit code
  local name="$1" stamp logfile rc
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  logfile="${RUNS_DIR}/${name}-${stamp}.log"
  : >"${logfile}"
  chmod 600 "${logfile}" 2>/dev/null || true
  set +e
  PYTHONPATH="${KIT_DIR}" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    "${VENV_PY}" -m contoso_onto "$@" --state-dir "${STATE_ROOT}" 2>&1 | tee -a "${logfile}"
  rc="${PIPESTATUS[0]}"
  set -e
  prune_logs
  case "${rc}" in
    130 | 143) rc="${EX_PARTIAL}" ;;
  esac
  log "log: ${logfile}"
  return "${rc}"
}

refuse_xtrace
