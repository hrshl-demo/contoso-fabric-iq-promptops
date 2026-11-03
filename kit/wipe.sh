#!/usr/bin/env bash
# wipe.sh - delete everything build.sh created, hard (no soft delete), then verify it is gone and audit what is left. No prompts.
#
#   bash wipe.sh                 Fabric first: every item permanently deleted (hardDelete) and the recycle bin emptied, then the
#                                workspace (renamed first, so its name is free again); then the capacity (the F4: billing stops); then the
#                                WHOLE resource group build.sh created for this run, with anything inside it. No resource group is left behind.
#                                It ends with an AUDIT: every Fabric capacity still in the subscription is listed (read only), so an older F4
#                                that this ledger did not create can never hide; it is named, never deleted by this script.
#   bash wipe.sh --plan          read-only: list what would be deleted and what is kept
#   bash wipe.sh --purge-local   after a clean wipe (or when there is nothing to wipe), also remove this kit's private Python
#                                environment, cache and output under ~/.local/state/contoso-bank-ontology-demo (ledger, tombstones, logs stay)
#   bash wipe.sh -h              this text
#
# One clean cycle = bash build.sh --fresh (wipe an earlier build, then build, approve, deploy, test) or, by hand, bash wipe.sh then bash build.sh.
# Azure has no soft delete for resource groups or Fabric capacities. Fabric has no API that permanently deletes a *workspace*: the
# emptied shell stays in the tenant's workspace retention (7 days minimum) unless a tenant admin purges it in the admin portal.
# The delegated capacity settings (ontology items, Copilot) belong to the capacity, so they disappear with it: the next build asks again.
# A resource group is only deleted when its run tag proves it is the one this kit created.
# It never deletes HostVM, its resource group, a repo, or anything that existed before build.sh ran (those are marked KEEP).
# Exit codes: 0 verified clean | 3 blocked (az not signed in) | 5 wipe incomplete (re-run to retry) | 6 another build/wipe is running

set -Eeuo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${HERE}/scripts/lib.sh"

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"; }

PLAN=0
PURGE=0
while (($#)); do
  case "$1" in
    -h | --help)
      usage
      exit 0
      ;;
    --plan) PLAN=1 ;;
    --purge-local) PURGE=1 ;;
    *) die "${EX_CONFIG}" "unknown option '$1' (see: bash wipe.sh -h)" ;;
  esac
  shift
done
if [[ "${PLAN}" == "1" && "${PURGE}" == "1" ]]; then
  die "${EX_CONFIG}" "--plan is read-only and cannot be combined with --purge-local"
fi

LEDGER="${STATE_ROOT}/inventory/ledger.json"

purge_local() { # the private Python environment, cache and output only: never the ledger, tombstones or logs
  local d removed=0
  for d in venv cache out; do
    if [[ -e "${STATE_ROOT}/${d}" ]]; then
      rm -rf -- "${STATE_ROOT:?}/${d}"
      removed=1
    fi
  done
  if [[ "${removed}" == "1" ]]; then
    log "purged the private Python environment, cache and output under ${STATE_ROOT} (ledger, tombstones and logs kept)"
  else
    log "no local files to purge under ${STATE_ROOT}"
  fi
}

if [[ ! -s "${LEDGER}" ]]; then
  log "nothing to wipe: there is no ownership ledger at ${LEDGER} (build.sh has not created anything on this host)"
  if [[ "${PURGE}" == "1" ]]; then
    purge_local
  fi
  log "VERIFIED: nothing to remove. HostVM, its resource group and the repo were never touched."
  exit 0
fi

init_state
check_host
if [[ "${PLAN}" == "0" ]]; then
  require_az
fi
bootstrap_venv 0

rc=0
if [[ "${PLAN}" == "1" ]]; then
  run_kit down --plan || rc=$?
else
  run_kit down || rc=$?
fi

if [[ "${PURGE}" == "1" ]]; then
  if [[ "${rc}" == "0" ]]; then
    purge_local
  else
    warn "not purging local files: the wipe did not finish cleanly and needs them to retry"
  fi
fi
exit "${rc}"
