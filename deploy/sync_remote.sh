#!/usr/bin/env bash
set -euo pipefail

# Sync the local Homerun repo to the remote deployment host and
# (optionally) trigger a redeploy of the docker compose stack.
#
# Defaults: SSH alias "polyhome-1" (configure it in ~/.ssh/config) and
# remote path /home/polyhome/homerun owned by user "polyhome".
#
# Override via env:
#   SSH_HOST=other-host REMOTE_PATH=/srv/homerun ./deploy/sync_remote.sh
#   DEPLOY_AFTER_SYNC=0 ./deploy/sync_remote.sh   # sync only, no restart
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

SSH_HOST="${SSH_HOST:-polyhome-1}"
REMOTE_PATH="${REMOTE_PATH:-/home/polyhome/homerun}"
DEPLOY_AFTER_SYNC="${DEPLOY_AFTER_SYNC:-1}"

echo "Syncing to ${SSH_HOST}:${REMOTE_PATH} ..."

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
