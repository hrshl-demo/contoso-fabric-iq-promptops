#!/usr/bin/env bash
# hostvm.sh - laptop-side helper: bring hostvm up, publish this kit to a PRIVATE GitHub repo, give hostvm read-only access to it,
# and clone it there. Run it where `az` and `gh` are signed in. Nothing here is needed on hostvm itself: there you only run
# build.sh and wipe.sh.
#
#   bash scripts/hostvm.sh up                     start hostvm if needed, publish the kit to the private repo, clone or fast-forward it on hostvm
#   bash scripts/hostvm.sh status                 power state, repo visibility, what is checked out on hostvm
#   bash scripts/hostvm.sh exec [--bg] -- CMD...  run CMD on hostvm in the checkout as the operator user
#                                                 (Run Command returns the last 4 KiB; --bg detaches it, read it with: logs)
#   bash scripts/hostvm.sh logs                   tail of the last 'exec --bg' output, and whether it is still running
#   bash scripts/hostvm.sh down                   deallocate hostvm (stops its compute billing). Runs only when you ask for it.
#
# Settings (environment, optional): HOSTVM_NAME (hostvm)  HOSTVM_RG (looked up)  HOSTVM_USER (looked up)
#   GH_REPO (hrshl-demo/contoso-bank-ontology-demo)  GH_BRANCH (main)  CHECKOUT_DIR (contoso-bank-ontology-demo, under the operator's home)
#
# Access model: one read-only GitHub deploy key per repo, created on hostvm (never copied anywhere), the same convention as the
# other repos on that machine. No token is ever written to hostvm, to a URL or to a log.

set -Eeuo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
KIT_DIR="$(cd -- "${HERE}/.." && pwd)"
HOSTVM_NAME="${HOSTVM_NAME:-hostvm}"
HOSTVM_RG="${HOSTVM_RG:-}"
OP_USER="${HOSTVM_USER:-}"
GH_REPO="${GH_REPO:-hrshl-demo/contoso-bank-ontology-demo}"
GH_BRANCH="${GH_BRANCH:-main}"
CHECKOUT_DIR="${CHECKOUT_DIR:-contoso-bank-ontology-demo}"
KEY_NAME="contoso_bank_ontology_deploy"
STATE_SUBDIR=".local/state/contoso-bank-ontology-demo"
REMOTE_CMD=""
REMOTE_OUT=""
REMOTE_RC=""
TMP=""

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"; }

valid() { # valid <regex> <value> <message>
  [[ "$2" =~ $1 ]] || die "$3"
}
valid '^[A-Za-z0-9._-]+$' "${HOSTVM_NAME}" "HOSTVM_NAME has unexpected characters"
valid '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$' "${GH_REPO}" "GH_REPO must look like owner/name"
valid '^[A-Za-z0-9._/-]+$' "${GH_BRANCH}" "GH_BRANCH has unexpected characters"
valid '^[A-Za-z0-9._-]+$' "${CHECKOUT_DIR}" "CHECKOUT_DIR must be a plain directory name"
if [[ -n "${OP_USER}" ]]; then
  valid '^[a-z_][a-z0-9_-]*$' "${OP_USER}" "HOSTVM_USER has unexpected characters"
fi

AZ=az
need_az() {
  if ! command -v az >/dev/null 2>&1; then
    if command -v az.cmd >/dev/null 2>&1; then
      AZ=az.cmd
    else
      die "az (Azure CLI) is required and must be signed in"
    fi
  fi
  TMP="$(mktemp -d)"
  trap 'rm -rf "${TMP}"' EXIT
}
az_raw() { # Git Bash must not rewrite @file arguments for az; scoped here so native git and gh keep their path translation
  MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' "${AZ}" "$@"
}
az_() { az_raw "$@" --only-show-errors | tr -d '\r'; }
native_path() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$1"
  else
    printf '%s' "$1"
  fi
}

# ---------------------------------------------------------------------------------------------- the VM
vm_locate() {
  if [[ -z "${HOSTVM_RG}" ]]; then
    local rows n
    rows="$(az_ vm list --query "[?name=='${HOSTVM_NAME}'].resourceGroup" -o tsv)"
    n="$(printf '%s\n' "${rows}" | grep -c . || true)"
    if [[ "${n}" != "1" ]]; then
      die "found ${n} VMs named ${HOSTVM_NAME} in this subscription; set HOSTVM_RG"
    fi
    HOSTVM_RG="${rows}"
  fi
}

vm_power() {
  az_ vm get-instance-view -g "${HOSTVM_RG}" -n "${HOSTVM_NAME}" --query "instanceView.statuses[?starts_with(code,'PowerState/')].code | [0]" -o tsv
}

vm_agent() {
  az_ vm get-instance-view -g "${HOSTVM_RG}" -n "${HOSTVM_NAME}" --query "instanceView.vmAgent.statuses[0].displayStatus" -o tsv
}

vm_start() {
  local p agent="" i
  p="$(vm_power)"
  say "${HOSTVM_NAME} (${HOSTVM_RG}) power state: ${p:-unknown}"
  if [[ "${p}" != "PowerState/running" ]]; then
    say "starting ${HOSTVM_NAME} ..."
    az_ vm start -g "${HOSTVM_RG}" -n "${HOSTVM_NAME}" -o none
  fi
  for i in $(seq 1 30); do
    agent="$(vm_agent || true)"
    if [[ "${agent}" == "Ready" ]]; then
      break
    fi
    if [[ "${i}" == "1" ]]; then
      say "waiting for the VM agent (Run Command needs it) ..."
    fi
    sleep 10
  done
  [[ "${agent}" == "Ready" ]] || die "the VM agent on ${HOSTVM_NAME} is not Ready (${agent:-unknown})"
}

# run_remote <name>: the script body arrives on stdin and runs as root on hostvm. Prints its output; sets REMOTE_RC from the
# EXIT marker, because Run Command reports success whatever the script's own exit code was.
run_remote() {
  local name="$1" f msg rc i
  f="${TMP}/${name}.sh"
  {
    printf '#!/bin/bash\nexec 2>&1\ntrap '"'"'echo "__EXIT__:$?"'"'"' EXIT\n'
    printf 'OP_USER=%q\nGH_REPO=%q\nGH_BRANCH=%q\nCHECKOUT_DIR=%q\nKEY_NAME=%q\nSTATE_SUBDIR=%q\nREMOTE_CMD=%q\n' \
      "${OP_USER}" "${GH_REPO}" "${GH_BRANCH}" "${CHECKOUT_DIR}" "${KEY_NAME}" "${STATE_SUBDIR}" "${REMOTE_CMD}"
    cat
  } >"${f}"
  msg=""
  for i in $(seq 1 16); do
    rc=0
    msg="$(az_raw vm run-command invoke -g "${HOSTVM_RG}" -n "${HOSTVM_NAME}" --command-id RunShellScript \
      --scripts "@$(native_path "${f}")" --query 'value[0].message' -o tsv --only-show-errors 2>&1 | tr -d '\r')" || rc=$?
    if [[ "${msg}" == *"Run command extension execution is in progress"* ]]; then
      sleep "${HOSTVM_RETRY_SLEEP:-15}"
      continue
    fi
    break
  done
  REMOTE_OUT="$(printf '%s\n' "${msg}" | sed -n '/^\[stdout\]/,/^\[stderr\]/{/^\[stdout\]/d;/^\[stderr\]/d;p;}')"
  REMOTE_RC="$(printf '%s\n' "${REMOTE_OUT}" | sed -n 's/^__EXIT__://p' | tail -n 1)"
  if [[ -z "${REMOTE_RC}" ]]; then
    printf '%s\n' "${msg}" >&2
    die "no result came back from ${HOSTVM_NAME} (Run Command rc=${rc}); is the VM agent healthy?"
  fi
  printf '%s\n' "${REMOTE_OUT}" | grep -v '^__EXIT__:' || true
}

detect_user() {
  if [[ -n "${OP_USER}" ]]; then
    return 0
  fi
  run_remote detect_user >/dev/null <<'EOS'
for d in /home/*/; do
  [ -d "$d" ] && echo "__USER__:$(basename "$d")"
done
EOS
  local users n
  users="$(printf '%s\n' "${REMOTE_OUT}" | sed -n 's/^__USER__://p')"
  n="$(printf '%s\n' "${users}" | grep -c . || true)"
  if [[ "${n}" != "1" ]]; then
    die "could not pick the operator user on ${HOSTVM_NAME} (found: $(printf '%s' "${users}" | tr '\n' ' ')); set HOSTVM_USER"
  fi
  OP_USER="${users}"
  valid '^[a-z_][a-z0-9_-]*$' "${OP_USER}" "unexpected operator user name on ${HOSTVM_NAME}"
  say "operator user on ${HOSTVM_NAME}: ${OP_USER}"
}

# ---------------------------------------------------------------------------------------------- GitHub
repo_publish() {
  command -v gh >/dev/null 2>&1 || die "gh (GitHub CLI) is required and must be signed in"
  git -C "${KIT_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "${KIT_DIR} is not a git repository (git init -b main && git add -A && git commit)"
  git -C "${KIT_DIR}" rev-parse --verify -q HEAD >/dev/null || die "the kit has no commit yet"
  if [[ -n "$(git -C "${KIT_DIR}" status --porcelain)" ]]; then
    warn "uncommitted changes are not published; commit them first to include them"
  fi
  local vis
  if vis="$(gh repo view "${GH_REPO}" --json isPrivate --jq .isPrivate 2>/dev/null)"; then
    [[ "${vis}" == "true" ]] || die "${GH_REPO} exists and is PUBLIC; refusing to publish this kit there"
    say "repo ${GH_REPO}: exists, private"
  else
    say "creating the private repo ${GH_REPO}"
    gh repo create "${GH_REPO}" --private --description "Contoso bank Fabric IQ ontology demo: build.sh and wipe.sh for the HostVM (synthetic data only)" >/dev/null
  fi
  if ! git -C "${KIT_DIR}" remote get-url origin >/dev/null 2>&1; then
    git -C "${KIT_DIR}" remote add origin "https://github.com/${GH_REPO}.git"
  fi
  # one-off credential helper: no global git config is changed and no token ends up in a URL or a process list
  git -C "${KIT_DIR}" -c credential.helper= -c 'credential.helper=!gh auth git-credential' push -q "https://github.com/${GH_REPO}.git" "HEAD:refs/heads/${GH_BRANCH}"
  local local_sha remote_sha
  local_sha="$(git -C "${KIT_DIR}" rev-parse HEAD)"
  remote_sha="$(gh api "repos/${GH_REPO}/branches/${GH_BRANCH}" --jq .commit.sha | tr -d '\r')"
  [[ "${local_sha}" == "${remote_sha}" ]] || die "pushed, but ${GH_REPO}@${GH_BRANCH} is ${remote_sha} and the local HEAD is ${local_sha}"
  say "published ${GH_REPO}@${GH_BRANCH} = ${local_sha:0:9}"
}

deploy_key() {
  say "ensuring a read-only deploy key for ${HOSTVM_NAME} ..."
  run_remote key >/dev/null <<'EOS'
cd "/home/${OP_USER}" || exit 1
sudo -u "${OP_USER}" -H env KEY_NAME="${KEY_NAME}" GH_REPO="${GH_REPO}" bash -s <<'USER_EOF'
set -u
key="$HOME/.ssh/${KEY_NAME}"
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
if [ ! -f "$key" ]; then
  ssh-keygen -q -t ed25519 -N '' -C "hostvm ${GH_REPO} (read-only deploy key)" -f "$key" || { echo "keygen failed"; exit 1; }
  echo "key: created $key"
else
  echo "key: reusing $key"
fi
echo "__PUBKEY__:$(cat "$key.pub")"
USER_EOF
EOS
  [[ "${REMOTE_RC}" == "0" ]] || die "could not prepare the deploy key on ${HOSTVM_NAME}: ${REMOTE_OUT}"
  local pub blob existing
  pub="$(printf '%s\n' "${REMOTE_OUT}" | sed -n 's/^__PUBKEY__://p' | head -n 1)"
  [[ "${pub}" == ssh-ed25519\ * ]] || die "unexpected public key from ${HOSTVM_NAME}"
  blob="$(printf '%s' "${pub}" | awk '{print $2}')"
  existing="$(gh api "repos/${GH_REPO}/keys" --jq '.[].key' | tr -d '\r' | awk '{print $2}')"
  if printf '%s\n' "${existing}" | grep -qxF "${blob}"; then
    say "deploy key: already registered on ${GH_REPO}"
  else
    printf '%s\n' "${pub}" >"${TMP}/hostvm_deploy.pub"
    gh repo deploy-key add "$(native_path "${TMP}/hostvm_deploy.pub")" --repo "${GH_REPO}" --title "${HOSTVM_NAME} (read-only)" >/dev/null
    say "deploy key: registered on ${GH_REPO} (read-only)"
  fi
}

checkout() {
  say "cloning / fast-forwarding ${GH_REPO} on ${HOSTVM_NAME} ..."
  run_remote checkout <<'EOS'
cd "/home/${OP_USER}" || exit 1
sudo -u "${OP_USER}" -H env KEY_NAME="${KEY_NAME}" GH_REPO="${GH_REPO}" GH_BRANCH="${GH_BRANCH}" CHECKOUT_DIR="${CHECKOUT_DIR}" bash -s <<'USER_EOF'
set -u
dir="$HOME/${CHECKOUT_DIR}"
ssh_cmd="ssh -i $HOME/.ssh/${KEY_NAME} -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
export GIT_SSH_COMMAND="$ssh_cmd" GIT_TERMINAL_PROMPT=0
url="git@github.com:${GH_REPO}.git"
if ! git ls-remote --exit-code "$url" "refs/heads/${GH_BRANCH}" >/dev/null 2>&1; then
  echo "ACCESS: hostvm cannot read ${GH_REPO} (deploy key not active yet?)"
  exit 3
fi
if [ -d "$dir/.git" ]; then
  cd "$dir" || exit 1
  if [ -n "$(git status --porcelain)" ]; then
    echo "SKIP: $dir has local changes; left alone"
    exit 4
  fi
  git fetch -q origin "${GH_BRANCH}" && git merge --ff-only -q "origin/${GH_BRANCH}" || { echo "SKIP: cannot fast-forward $dir"; exit 4; }
else
  git clone -q --branch "${GH_BRANCH}" "$url" "$dir" || exit 1
fi
git -C "$dir" config core.sshCommand "$ssh_cmd"
echo "CHECKOUT: $dir @ $(git -C "$dir" log -1 --format='%h %s')"
USER_EOF
EOS
  [[ "${REMOTE_RC}" == "0" ]] || die "the checkout on ${HOSTVM_NAME} did not finish (exit ${REMOTE_RC})"
}

# ---------------------------------------------------------------------------------------------- commands
cmd_up() {
  need_az
  vm_locate
  vm_start
  repo_publish
  detect_user
  deploy_key
  checkout
  say ""
  say "ready. On ${HOSTVM_NAME}, as ${OP_USER}:"
  say "  cd ~/${CHECKOUT_DIR}"
  say "  bash build.sh --preflight   # read-only: checks and plan, creates nothing"
  say "  bash build.sh               # build in a brand-new resource group (the capacity bills ~\$0.88/h while it runs)"
  say "  bash wipe.sh                # hard-delete everything build.sh created, the whole resource group included"
  say "${HOSTVM_NAME} is running and bills until you deallocate it (bash scripts/hostvm.sh down)."
}

cmd_status() {
  need_az
  vm_locate
  say "${HOSTVM_NAME} (${HOSTVM_RG}): $(vm_power), agent $(vm_agent || echo unknown)"
  if command -v gh >/dev/null 2>&1; then
    say "repo ${GH_REPO}: $(gh repo view "${GH_REPO}" --json isPrivate,pushedAt --jq '"private=" + (.isPrivate|tostring) + " pushed=" + .pushedAt' 2>/dev/null || echo 'not found')"
  fi
  if [[ "$(vm_power)" == "PowerState/running" ]]; then
    detect_user
    run_remote status <<'EOS'
cd "/home/${OP_USER}" || exit 1
sudo -u "${OP_USER}" -H env CHECKOUT_DIR="${CHECKOUT_DIR}" STATE_SUBDIR="${STATE_SUBDIR}" bash -s <<'USER_EOF'
d="$HOME/${CHECKOUT_DIR}"
if [ -d "$d/.git" ]; then
  echo "checkout: $d @ $(git -C "$d" log -1 --format='%h %s') (local changes: $(git -C "$d" status --porcelain | wc -l))"
else
  echo "checkout: $d not present"
fi
s="$HOME/${STATE_SUBDIR}"
if [ -d "$s" ]; then
  echo "state: $s ($(du -sm "$s" | cut -f1) MiB); ledger: $([ -s "$s/inventory/ledger.json" ] && echo present || echo none)"
else
  echo "state: none yet"
fi
echo "disk free on /: $(df -h / | awk 'NR==2{print $4}')"
USER_EOF
EOS
  fi
}

cmd_exec() {
  local bg=0
  if [[ "${1:-}" == "--bg" ]]; then
    bg=1
    shift
  fi
  if [[ "${1:-}" == "--" ]]; then
    shift
  fi
  [[ $# -ge 1 ]] || die "usage: hostvm.sh exec [--bg] -- COMMAND..."
  need_az
  vm_locate
  [[ "$(vm_power)" == "PowerState/running" ]] || die "${HOSTVM_NAME} is not running (bash scripts/hostvm.sh up)"
  detect_user
  REMOTE_CMD="$*"
  if [[ "${bg}" == "1" ]]; then
    run_remote exec_bg <<'EOS'
cd "/home/${OP_USER}" || exit 1
unit="contoso-bank-exec-$(date +%s)"
logdir="/home/${OP_USER}/${STATE_SUBDIR}/runs"
sudo -u "${OP_USER}" -H mkdir -p "${logdir}"
: >"${logdir}/hostvm-exec.log"
chown "${OP_USER}" "${logdir}/hostvm-exec.log"
systemd-run --quiet --collect --unit="${unit}" --uid="${OP_USER}" --working-directory="/home/${OP_USER}/${CHECKOUT_DIR}" \
  --setenv=HOME="/home/${OP_USER}" --property=StandardOutput="append:${logdir}/hostvm-exec.log" --property=StandardError=inherit \
  /bin/bash -lc "${REMOTE_CMD}"
echo "started ${unit}; read it with: bash scripts/hostvm.sh logs"
EOS
  else
    run_remote exec <<'EOS'
cd "/home/${OP_USER}" || exit 1
sudo -u "${OP_USER}" -H env CHECKOUT_DIR="${CHECKOUT_DIR}" REMOTE_CMD="${REMOTE_CMD}" bash -lc 'cd "$HOME/${CHECKOUT_DIR}" && timeout 240 bash -c "$REMOTE_CMD"'
EOS
  fi
  return "${REMOTE_RC}"
}

cmd_logs() {
  need_az
  vm_locate
  detect_user
  run_remote logs <<'EOS'
cd "/home/${OP_USER}" || exit 1
f="/home/${OP_USER}/${STATE_SUBDIR}/runs/hostvm-exec.log"
if systemctl list-units 'contoso-bank-exec-*' --state=running --no-legend 2>/dev/null | grep -q .; then
  echo "[still running]"
else
  echo "[not running]"
fi
if [ -f "$f" ]; then tail -c 3300 "$f"; else echo "(no exec --bg log yet)"; fi
EOS
}

cmd_down() {
  need_az
  vm_locate
  say "deallocating ${HOSTVM_NAME} (stops compute billing; the disk is kept; a dynamic public IP may change) ..."
  az_ vm deallocate -g "${HOSTVM_RG}" -n "${HOSTVM_NAME}" -o none
  say "${HOSTVM_NAME}: $(vm_power)"
}

case "${1:-}" in
  up) cmd_up ;;
  status) cmd_status ;;
  exec)
    shift
    cmd_exec "$@"
    ;;
  logs) cmd_logs ;;
  down) cmd_down ;;
  -h | --help | "") usage ;;
  *) die "unknown command '$1' (see: bash scripts/hostvm.sh -h)" ;;
esac
