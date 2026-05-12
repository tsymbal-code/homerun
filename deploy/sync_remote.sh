#!/usr/bin/env bash
set -euo pipefail

# Sync the local Homerun repo to the branch-derived deployment host
# and (optionally) trigger a redeploy of the docker compose stack.
#
# The deploy target is selected by the current git branch — see
# `docs/plans/architecture/deploy-targets.md` for the authoritative
# branch → host mapping. The `case` block below is one of the four
# files in the repo that materialises that mapping at runtime; it
# must stay in lock-step with the SSOT table in the architecture
# note and with the mirror `case` block in
# `.claude/hooks/remind-ssh.sh` and `scripts/run_tests_remote.sh`.
#
# Override via env (deliberate, not workflow):
#   SSH_HOST=other-host REMOTE_PATH=/srv/homerun ./deploy/sync_remote.sh
#   FORCE_HOST=1 SSH_HOST=polyhome-prod ./deploy/sync_remote.sh   # deliberate cross-target soak
#   DEPLOY_AFTER_SYNC=0 ./deploy/sync_remote.sh                   # sync only, no restart
#
# Pre-flight (no rsync, prints resolved target and exits 0):
#   bash deploy/sync_remote.sh --dry-run-host
#
# .env handling: .env IS synced — the local .env is the canonical
# source of truth for production secrets in this deployment. Edit
# the local .env, then run this script; the server's .env is
# overwritten by design. Backups (.env.bak.*) are excluded so a
# stray local backup cannot stomp the server.
#
# Critical exclusions (do NOT remove):
#   - data/ — postgres bind mount, ML caches, runtime artifacts.
#     Pushing a local data/ over rsync --delete would corrupt the
#     server's database state.

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
case "${CURRENT_BRANCH}" in
  main) DERIVED_HOST="polyhome-prod"; DERIVED_ENV="prod" ;;
  dev)  DERIVED_HOST="polyhome-1";    DERIVED_ENV="stage" ;;
  *)
    echo "REFUSING: branch '${CURRENT_BRANCH}' is not mapped to a deploy target." >&2
    echo "See docs/plans/architecture/deploy-targets.md for the mapping." >&2
    exit 1
    ;;
esac

SSH_HOST="${SSH_HOST:-${DERIVED_HOST}}"
REMOTE_PATH="${REMOTE_PATH:-/home/polyhome/homerun}"
DEPLOY_AFTER_SYNC="${DEPLOY_AFTER_SYNC:-1}"

if [[ "${SSH_HOST}" != "${DERIVED_HOST}" && "${FORCE_HOST:-0}" != "1" ]]; then
  echo "REFUSING: branch '${CURRENT_BRANCH}' is mapped to '${DERIVED_HOST}'" >&2
  echo "         but SSH_HOST is set to '${SSH_HOST}'." >&2
  echo "  To override deliberately: FORCE_HOST=1 SSH_HOST=${SSH_HOST} ./deploy/sync_remote.sh" >&2
  exit 1
fi

if [[ "${SSH_HOST}" != "${DERIVED_HOST}" ]]; then
  echo "WARN: cross-target deploy — branch=${CURRENT_BRANCH} (${DERIVED_HOST}) -> SSH_HOST=${SSH_HOST}" >&2
  echo "      Proceeding because FORCE_HOST=1 was set." >&2
fi

if [[ "${1:-}" == "--dry-run-host" ]]; then
  echo "branch=${CURRENT_BRANCH} env=${DERIVED_ENV} host=${SSH_HOST}"
  exit 0
fi

echo "Syncing to ${SSH_HOST}:${REMOTE_PATH} (branch=${CURRENT_BRANCH}, env=${DERIVED_ENV}) ..."

rsync -avz --delete \
  --exclude '.git/' \
  --exclude '.env.bak.*' \
  --exclude 'data/' \
  --exclude '.mypy_cache/' \
  --exclude '.pytest_cache/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '*.log' \
  --exclude 'logs/' \
  --exclude '.DS_Store' \
  --exclude 'node_modules/' \
  --exclude 'frontend/dist/' \
  --exclude 'output/' \
  --exclude '.oneshot/' \
  --exclude '.playwright-cli/' \
  --exclude '.cutover-snapshots/' \
  ./ "${SSH_HOST}:${REMOTE_PATH}/"

echo "Sync complete."

echo "Setting +x on remote scripts ..."
ssh "${SSH_HOST}" "chmod +x ${REMOTE_PATH}/deploy/*.sh ${REMOTE_PATH}/scripts/infra/*.sh 2>/dev/null || true"

if [[ "${DEPLOY_AFTER_SYNC}" != "0" ]]; then
  echo "Triggering remote redeploy ..."
  ssh "${SSH_HOST}" "cd ${REMOTE_PATH} && bash deploy/remote_redeploy.sh"
else
  echo "DEPLOY_AFTER_SYNC=0 -> skipping remote redeploy."
fi
