#!/usr/bin/env bash
set -euo pipefail

# Run the backend pytest suite on the remote `polyhome-1` host against
# the live Postgres container, without disturbing the running stack.
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
# This script bridges both gaps: it SSHes to polyhome-1 and starts a
# throwaway backend container with the rsynced `backend/tests/`
# directory bind-mounted in, pointed at the running Postgres service
# inside the compose network.  The throwaway container shares the
# compose network so DNS resolves `postgres`/`redis` correctly, but
# `--no-deps` + `--rm` keep it from touching the long-running
# backend / worker containers and from leaving state behind.
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
# Override the SSH alias or remote path with env vars:
#   SSH_HOST=other-host REMOTE_PATH=/srv/homerun bash scripts/run_tests_remote.sh

SSH_HOST="${SSH_HOST:-polyhome-1}"
REMOTE_PATH="${REMOTE_PATH:-/home/polyhome/homerun}"

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
        -e DATABASE_URL=postgresql+asyncpg://homerun:homerun@postgres:5432/homerun \
        -e PYTHONDONTWRITEBYTECODE=1 \
        backend pytest ${quoted_args}"

# Why we also bind-mount pyproject.toml: the runtime image was built
# at the time of last redeploy.  pytest config (markers, timeout,
# asyncio_mode) lives in [tool.pytest.ini_options] in pyproject.toml,
# and changes to it MUST take effect for test runs without forcing a
# full image rebuild.  The bind mount is read-only for safety.
