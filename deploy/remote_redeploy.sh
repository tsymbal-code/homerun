#!/usr/bin/env bash
set -euo pipefail

# Redeploy the Homerun docker compose stack on the remote host.
# Called by deploy/sync_remote.sh after rsync, or directly via SSH.
#
# Migration flow:
#   docker-compose.yml has a one-shot `migrate` service that runs
#   init_database() (alembic upgrade head + seed). The `backend` and
#   `worker-*` services declare
#   `depends_on: { migrate: condition: service_completed_successfully }`,
#   so `docker compose up -d` blocks until migrations finish.  No
#   separate `make init-db` step is needed.
#
# Override via env:
#   REMOTE_PATH=/srv/homerun DEPLOY_USER=ubuntu BUILD_IMAGES=0 \
#     bash deploy/remote_redeploy.sh
#
# BUILD_IMAGES=0 skips local --build and pulls from GHCR instead.

REMOTE_PATH="${REMOTE_PATH:-/home/polyhome/homerun}"
DEPLOY_USER="${DEPLOY_USER:-polyhome}"
BUILD_IMAGES="${BUILD_IMAGES:-1}"

cd "${REMOTE_PATH}"

# Reset ownership on repo files only.  data/ is excluded because the
# postgres bind mount stores files under uid 70 (postgres user inside
# the container); chowning that to ${DEPLOY_USER} would break the DB
# on next start.  venv / node_modules / dist are excluded for speed.
echo "Normalising ownership to ${DEPLOY_USER}:${DEPLOY_USER} ..."
chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${REMOTE_PATH}"

echo "Redeploying in ${REMOTE_PATH} ..."
docker compose down --remove-orphans

if [[ "${BUILD_IMAGES}" != "0" ]]; then
  docker compose up -d --build
else
  docker compose pull
  docker compose up -d
fi

echo "--- compose status ---"
docker compose ps
