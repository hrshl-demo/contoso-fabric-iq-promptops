#!/usr/bin/env bash
# build.sh - build the Contoso bank ontology demo on this host's Azure context. Running it IS the authorisation: no prompts.
#
#   bash build.sh                  the whole cycle, as stages; resumable, every stage is idempotent:
#                                    1 BUILD    data (bronze seed files, silver and gold tables) -> brand-new resource group + F4 -> workspace -> lakehouse ->
#                                               bronze, then silver, then gold loaded (a layer that did not change is not loaded again) -> semantic model
#                                    2 APPROVE  ASKS a capacity admin for two delegated settings (prints the link), waits 15 minutes for them to
#                                               propagate, then polls every minute by trying to create the ontology; carries on by itself
#                                    3 DEPLOY   ontology, then the graph over REST (the portal's Configure graph step is not needed)
#                                    4 TEST     verify + the ontology agent answers its golden questions (reported, not fatal)
#   bash build.sh --fresh          stage 0 first: wipe whatever an earlier build of this kit left (older F4, workspace, resource group), then the stages
#   bash build.sh --until STAGE    stop after build, approve, deploy or test; a later bash build.sh continues with the next stage
#   bash build.sh --preflight      read-only: every check and the plan; creates nothing
#   bash build.sh --diagnose       read-only: ledger versus what exists, portal drift, MCP endpoint
#   bash build.sh --pause          pause the Fabric capacity (stops compute billing; OneLake storage still bills)
#   bash build.sh --resume         resume it
#   bash build.sh --pause-after    build, then pause the capacity when done
#   bash build.sh --helper CMD...  run another helper command against this host's state, for example:
#                                    --helper export --name ContosoBankOntology    save a portal-edited ontology as a seed
#                                    --helper verify --answers answers.json        score collected answers against the golden set
#                                    --helper agents                               show the Foundry IQ calls (add --execute to send them)
#   bash build.sh -h               this text
#
# Build options: --tier smoke|core|full   --seed DIR   --force-replace   --loader load-api|delta   --repo PATH   --allow-drift   --no-ont-annotations   --descriptions full|compact
#                The tier decides how much of each layer is built and loaded: that tier's gold tables (dim_*, fact_*, bridge_*, ref_*), the silver tables
#                they are built from (silver_*, plus two catalogs) and the seed JSON files those read (Files/bronze/<release>/...). The ontology and the
#                semantic model bind to gold only; bronze and silver stay in the lakehouse for lineage and audit.
#                --loader: load-api (default) loads each table with the Load Table API; a table whose load request that API itself refused (HTTP 400/415/422, or a failed operation whose
#                error code is not about the moment) is written with delta-rs, which a build installs into its own environment up front (throttling, a busy service, a failed
#                upload or a dropped connection never switch loader: the run backs off for at most 6 minutes per table, then stops, and a re-run resumes).
#                --loader delta writes every table with delta-rs.
#                --descriptions: the entity descriptions of the ontology, the semantic model and the data dictionary. full (default) = business meaning, standards sentence and
#                the architecture clause (star role, grain, lineage); compact = business meaning and standards sentence only, each line within 500 characters. If Fabric refuses
#                a definition with HTTP 400 the build retries ONCE in compact mode and remembers it (state key descriptionsMode); --descriptions full tries the full text again.
# Stage options: --gate-wait MIN (15)  --gate-poll SEC (60)  --gate-timeout MIN (120)  --no-gate-wait (stop with exit 3 at the gate instead of waiting)
#                --no-graph (leave Configure graph to the portal)  --golden none|smoke|full (smoke = 5 questions)  --golden-parallel N (2)
#                --strict-golden (a failed golden question fails the build)  --reload (load every layer again even if the lakehouse already holds the data)
# Settings (environment, all optional; or put them in ~/.local/state/contoso-bank-ontology-demo/config/local.json):
#   CONTOSO_CAPACITY_ADMINS   comma separated UPNs (default: the signed-in user)     CONTOSO_CAPACITY_SKU   F4 only (see CONTOSO_ALLOW_OTHER_SKU below)
#   CONTOSO_LOCATION          Azure region (default eastus2: the ontology agent    CONTOSO_TIER           smoke|core|full (default core)
#                             works there without the cross-geo AI setting)
#   CONTOSO_RESOURCE_GROUP    default rg-contoso-fabric-ontology                     CONTOSO_CAPACITY_NAME  default contosofabricf4
#   CONTOSO_WORKSPACE_NAME    default Contoso-Bank-Ontology-Demo                     CONTOSO_STATE_DIR      default ~/.local/state/contoso-bank-ontology-demo
#   CONTOSO_GATE_WAIT_MIN / CONTOSO_GATE_POLL_SEC / CONTOSO_GATE_TIMEOUT_MIN / CONTOSO_GOLDEN    defaults for the stage options above
#   CONTOSO_REPO              a checkout of contoso-retail-superapp4 (read only; default: ~/contoso-retail-superapp4 if it is at the pinned commit)
#   CONTOSO_DESCRIPTIONS      full|compact entity descriptions (default full, or what an earlier fallback to compact remembered); --descriptions beats it
# Exit codes: 0 done | 2 bad config | 3 blocked (fix, re-run) | 4 partial (re-run resumes) | 6 another build/wipe is running
# The only human step is stage 2: the override is per capacity (Fabric admin portal > Capacity settings > the capacity > Delegated tenant settings), so every
# new capacity needs it again. The log prints the direct link; it also stays in <state>/out/ADMIN_APPROVAL.md.
# Capacity: a Fabric F4 (4 capacity units, about $0.88/h). build.sh creates it, checks that Azure and Fabric both report F4 and that the
# workspace runs on it. Any other size is refused (exit 2) unless CONTOSO_ALLOW_OTHER_SKU=1 is set together with CONTOSO_CAPACITY_SKU.
# Every build runs in a brand-new resource group that it creates itself (an existing group blocks the build, exit 3), so that
# bash wipe.sh can delete the whole group and leave none behind. Everything it creates is recorded in an ownership ledger.

set -Eeuo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${HERE}/scripts/lib.sh"

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"; }
need_value() { [[ $# -ge 2 && -n "${2:-}" ]] || die "${EX_CONFIG}" "$1 needs a value (see: bash build.sh -h)"; }

MODE=build
WANT_OPTIONAL=0
WANT_DELTA=0
BUILD_ONLY=0
PASS=()
HELPER=()
while (($#)); do
  case "$1" in
    -h | --help)
      usage
      exit 0
      ;;
    --preflight) MODE=preflight ;;
    --diagnose) MODE=diagnose ;;
    --pause) MODE=pause ;;
    --resume) MODE=resume ;;
    --helper)
      MODE=helper
      shift
      HELPER=("$@")
      break
      ;;
    --pause-after)
      PASS+=(--pause)
      BUILD_ONLY=1
      ;;
    --force-replace | --fresh | --no-gate-wait | --no-graph | --strict-golden | --reload)
      PASS+=("$1")
      BUILD_ONLY=1
      ;;
    --allow-drift) PASS+=("$1") ;;
    --no-ont-annotations)
      PASS+=("$1")
      BUILD_ONLY=1
      ;;
    --repo | --config | --descriptions)
      need_value "$@"
      PASS+=("$1" "$2")
      shift
      ;;
    --tier | --seed | --until | --gate-wait | --gate-poll | --gate-timeout | --golden | --golden-parallel)
      need_value "$@"
      PASS+=("$1" "$2")
      BUILD_ONLY=1
      shift
      ;;
    --loader)
      need_value "$@"
      PASS+=("$1" "$2")
      BUILD_ONLY=1
      if [[ "$2" == "delta" ]]; then
        WANT_DELTA=2
      fi
      shift
      ;;
    *) die "${EX_CONFIG}" "unknown option '$1' (see: bash build.sh -h)" ;;
  esac
  shift
done

if [[ "${MODE}" != "build" && "${BUILD_ONLY}" == "1" ]]; then
  die "${EX_CONFIG}" "--tier, --seed, --force-replace, --loader, --no-ont-annotations, --pause-after and the stage options (--fresh, --until, --gate-*, --no-graph, --golden*, --strict-golden, --reload) only apply to a build (see: bash build.sh -h)"
fi
if [[ "${MODE}" == "helper" && ${#HELPER[@]} -eq 0 ]]; then
  die "${EX_CONFIG}" "--helper needs a command, for example: bash build.sh --helper verify --answers answers.json"
fi
if [[ "${CONTOSO_ONTO_AUTH:-}" == "default" ]]; then
  WANT_OPTIONAL=1
fi
# The delta-rs writer (deltalake) is the per-table fallback of the Load Table API: a build installs it up front so that a table the API refuses is finished in
# the same run instead of after a re-run. --loader delta (also after --helper load) makes it required.
if [[ "${MODE}" == "build" && "${WANT_DELTA}" == "0" ]]; then
  WANT_DELTA=1
fi
if [[ "${MODE}" == "helper" && "${HELPER[0]:-}" == "load" ]]; then
  WANT_DELTA=1
  if [[ " ${HELPER[*]} " == *" --loader delta "* ]]; then
    WANT_DELTA=2
  fi
fi

init_state
check_host
require_az
bootstrap_venv "${WANT_OPTIONAL}" "${WANT_DELTA}"

rc=0
case "${MODE}" in
  preflight) run_kit preflight ${PASS[@]+"${PASS[@]}"} || rc=$? ;;
  diagnose) run_kit diagnose ${PASS[@]+"${PASS[@]}"} || rc=$? ;;
  pause) run_kit capacity suspend --execute || rc=$? ;;
  resume) run_kit capacity resume --execute || rc=$? ;;
  helper) run_kit "${HELPER[@]}" --out "${STATE_ROOT}/out" || rc=$? ;;
  build)
    log "build.sh: running the cycle on this host's Azure context (the existing sign-in, never changed): BUILD -> APPROVE -> DEPLOY -> TEST"
    log "  creates only what is missing: resource group, Fabric capacity, workspace, lakehouse (bronze seed files, silver tables, gold tables), semantic model, ontology, graph"
    log "  stage 2 needs ONE human action (two capacity settings); the run prints the link, waits, polls, and carries on by itself"
    log "  the capacity bills while it runs (F4 is about \$0.88/h): bash build.sh --pause stops it, bash wipe.sh deletes what this kit created"
    run_kit up ${PASS[@]+"${PASS[@]}"} || rc=$?
    ;;
esac
exit "${rc}"
