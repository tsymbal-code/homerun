#!/usr/bin/env bash
# UserPromptSubmit hook for the Homerun project.
#
# Reads a JSON payload {"prompt": "..."} from stdin (Claude Code contract).
# If the prompt mentions a localhost diagnostic command — psql, curl, docker
# compose up against localhost/127.0.0.1, etc. — emit a reminder pointing at
# CLAUDE.md and deploy/AGENTS.md so the agent doesn't waste a turn running
# commands against an empty local stack.
#
# The reminder names the branch-derived target host so the example commands
# the agent sees are already host-correct. The `case` block below mirrors
# the one in `deploy/sync_remote.sh` and `scripts/run_tests_remote.sh`;
# the authoritative branch → host mapping lives in
# `docs/plans/architecture/deploy-targets.md`.
#
# Side effects: stdout only. No file writes, no network calls. Designed to
# finish in well under 100 ms.
#
# To disable temporarily: chmod -x .claude/hooks/remind-ssh.sh.

set -euo pipefail

resolve_homerun_target() {
  # Writes "branch host env_label" to stdout, or "unknown - -" when the
  # branch is unmapped (detached HEAD, not a git repo, third environment).
  local branch
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  case "${branch}" in
    main) printf '%s %s %s\n' "${branch}" "polyhome-prod" "PRODUCTION" ;;
    dev)  printf '%s %s %s\n' "${branch}" "polyhome-1"    "STAGING" ;;
    *)    printf '%s %s %s\n' "${branch}" "-"             "-" ;;
  esac
}

# The payload is small JSON; if python3 is missing or the parse fails, just
# exit silently — the hook must never block the user prompt.
prompt=$(python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    print(data.get("prompt", ""))
except Exception:
    pass
' 2>/dev/null || true)

if [ -z "$prompt" ]; then
  exit 0
fi

# Case-insensitive match against the well-known footgun patterns.
pattern='localhost:[0-9]+|127\.0\.0\.1:[0-9]+|psql -h localhost|psql -h 127\.0\.0\.1|docker compose up|curl http://(localhost|127\.0\.0\.1)|alembic upgrade head'

if echo "$prompt" | grep -qiE "$pattern"; then
  read -r branch host env_label < <(resolve_homerun_target)

  if [[ "${host}" == "-" ]]; then
    cat <<EOF

[homerun reminder] Heads up: this project does NOT run on localhost.
The application stack lives on a branch-derived remote server under
\`/home/polyhome/homerun\`. Current branch \`${branch}\` is not mapped
to a known target — resolve it from the SSOT before running any
ssh/deploy command:

    docs/plans/architecture/deploy-targets.md

Then wrap diagnostic commands as:

    ssh <HOMERUN_HOST> 'cd /home/polyhome/homerun && docker compose ps'
    ssh <HOMERUN_HOST> 'cd /home/polyhome/homerun && docker compose logs --tail=200 backend'

Full catalog: deploy/AGENTS.md.
EOF
  else
    cat <<EOF

[homerun reminder] Heads up: this project does NOT run on localhost.
Current branch: ${branch} → target server: ${host} (${env_label}).
All diagnostic commands must be wrapped:

    ssh ${host} 'cd /home/polyhome/homerun && docker compose ps'
    ssh ${host} 'cd /home/polyhome/homerun && docker compose logs --tail=200 backend'
    ssh ${host} 'curl -fsS http://127.0.0.1:8888/api/...'

Full catalog: deploy/AGENTS.md. Mapping: docs/plans/architecture/deploy-targets.md.
EOF
  fi
fi

exit 0
