#!/usr/bin/env bash
set -euo pipefail

# Run the backend pytest suite on the branch-derived remote host
# against the live Postgres container, without disturbing the running
# stack.
#
# Target resolution
# -----------------
# The remote target is derived from the current git branch, matching
# `deploy/sync_remote.sh`. See
# `docs/plans/architecture/deploy-targets.md` for the SSOT mapping.
# The `case` block below MUST stay in lock-step with the deploy
# script's resolver and the hook in `.claude/hooks/remind-ssh.sh`.
#
# Why this script exists
# ----------------------
# The Homerun backend image deliberately excludes `tests/` (see
# `backend/.dockerignore`) so the deployed runtime is lean.  That
# means the operator cannot just `docker compose exec backend pytest`
# — the test files literally aren't in the image.  Locally there is
# no Postgres / Redis / backend stack at all (see CLAUDE.md), so
# running pytest locally fails with `ConnectionRefusedError` or
# import-time errors well before any test executes.
#
# This script bridges both gaps: it SSHes to the branch-derived
# target and starts a throwaway backend container with the rsynced
# `backend/tests/` directory bind-mounted in, pointed at the running
# Postgres service inside the compose network.  The throwaway
# container shares the compose network so DNS resolves
# `postgres`/`redis` correctly, but `--no-deps` + `--rm` keep it from
# touching the long-running backend / worker containers and from
# leaving state behind.
#
# Throwaway databases (allocated by `build_postgres_session_factory`
# in tests that need a real Postgres) are created with the `homerun`
# user, which is a superuser + CREATEDB-able on the deployed
# instance, then dropped at test teardown.  The autouse fixtures in
# `backend/tests/conftest.py` block any accidental writes against
# the live `homerun` database for the canonical test wallet
# addresses (defence-in-depth).
#
# Usage
# -----
#   bash scripts/run_tests_remote.sh                        # full suite
#   bash scripts/run_tests_remote.sh tests/test_passwords.py
#   bash scripts/run_tests_remote.sh -k 'lifespan or alembic_roundtrip'
#   bash scripts/run_tests_remote.sh -m 'not slow'
#
# Override the SSH alias or remote path with env vars (deliberate,
# not workflow). Cross-target overrides require `FORCE_HOST=1`:
#   FORCE_HOST=1 SSH_HOST=polyhome-prod bash scripts/run_tests_remote.sh
#   REMOTE_PATH=/srv/homerun bash scripts/run_tests_remote.sh

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
case "${CURRENT_BRANCH}" in
  main) DERIVED_HOST="polyhome-prod" ;;
  dev)  DERIVED_HOST="polyhome-1"    ;;
  *)
    echo "REFUSING: branch '${CURRENT_BRANCH}' is not mapped to a deploy target." >&2
    echo "See docs/plans/architecture/deploy-targets.md for the mapping." >&2
    exit 1
    ;;
esac

SSH_HOST="${SSH_HOST:-${DERIVED_HOST}}"
REMOTE_PATH="${REMOTE_PATH:-/home/polyhome/homerun}"

if [[ "${SSH_HOST}" != "${DERIVED_HOST}" && "${FORCE_HOST:-0}" != "1" ]]; then
  echo "REFUSING: branch '${CURRENT_BRANCH}' is mapped to '${DERIVED_HOST}'" >&2
  echo "         but SSH_HOST is set to '${SSH_HOST}'." >&2
  echo "  To override deliberately: FORCE_HOST=1 SSH_HOST=${SSH_HOST} bash scripts/run_tests_remote.sh" >&2
  exit 1
fi

if [[ "${SSH_HOST}" != "${DERIVED_HOST}" ]]; then
  echo "WARN: cross-target test run — branch=${CURRENT_BRANCH} (${DERIVED_HOST}) -> SSH_HOST=${SSH_HOST}" >&2
fi

# Default arguments mirror the CI invocation when the caller passes
# nothing.  Once a caller supplies any positional, take theirs verbatim.
if [[ $# -eq 0 ]]; then
    PYTEST_ARGS=(tests/ -v --tb=short --timeout=60)
else
    PYTEST_ARGS=("$@")
fi

echo "[run_tests_remote] target=${SSH_HOST}:${REMOTE_PATH}" >&2
echo "[run_tests_remote] pytest args: ${PYTEST_ARGS[*]}" >&2

# Properly quote each pytest arg so things like `-k 'foo or bar'` survive
# the SSH transport.
quoted_args=""
for arg in "${PYTEST_ARGS[@]}"; do
    # printf %q emits a shell-safe quoted form.
    quoted_args+="$(printf '%q' "$arg") "
done

# The remote command:
#   - cd into the deployed checkout (rsynced by deploy/sync_remote.sh)
#   - `docker compose run --rm --no-deps`:
#       --rm   : remove container after exit (no leftover state)
#       --no-deps : do NOT spin up postgres/redis/etc. — they are
#                   already running as long-lived services
#   - bind-mount tests/ into the image's expected layout
#     (`/app/backend/tests`)
#   - DATABASE_URL points at the compose-network postgres alias
#   - `backend` is the service name from docker-compose.yml whose
#     image already has pytest installed
ssh "$SSH_HOST" "cd ${REMOTE_PATH} && \
    docker compose run --rm --no-deps \
        -v ./backend/tests:/app/backend/tests:ro \
        -v ./backend/pyproject.toml:/app/backend/pyproject.toml:ro \
        -v ./backend/alembic:/app/backend/alembic:ro \
        -v ./backend/alembic.ini:/app/backend/alembic.ini:ro \
        -v ./backend/alembic_helpers.py:/app/backend/alembic_helpers.py:ro \
        -e DATABASE_URL=postgresql+asyncpg://homerun:homerun@postgres:5432/homerun \
        -e PYTHONDONTWRITEBYTECODE=1 \
        backend pytest ${quoted_args}"

# Why we bind-mount these specific paths: the runtime image was built
# at the time of last redeploy and ships frozen copies.  Several
# things must take effect for test runs without forcing a full image
# rebuild:
#   - pyproject.toml carries [tool.pytest.ini_options] (markers,
#     timeout, asyncio_mode).
#   - alembic/, alembic.ini, alembic_helpers.py carry the migration
#     chain that the alembic round-trip + replay tests exercise.
# All bind mounts are read-only for safety.
