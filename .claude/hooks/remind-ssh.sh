#!/usr/bin/env bash
# UserPromptSubmit hook for the Homerun project.
#
# Reads a JSON payload {"prompt": "..."} from stdin (Claude Code contract).
# If the prompt mentions a localhost diagnostic command — psql, curl, docker
# compose up against localhost/127.0.0.1, etc. — emit a reminder pointing at
# CLAUDE.md and deploy/AGENTS.md so the agent doesn't waste a turn running
# commands against an empty local stack.
#
# Side effects: stdout only. No file writes, no network calls. Designed to
# finish in well under 100 ms.
#
# To disable temporarily: chmod -x .claude/hooks/remind-ssh.sh.

set -euo pipefail

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
  cat <<'EOF'

[homerun reminder] Heads up: this project does NOT run on localhost.
The application stack lives on remote server `polyhome-1` under
`/home/polyhome/homerun`. Local Postgres / backend / workers / Vite
do not exist. Diagnostic commands must be wrapped:

    ssh polyhome-1 'cd /home/polyhome/homerun && docker compose ps'
    ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --tail=200 backend'
    ssh polyhome-1 'curl -fsS http://127.0.0.1:8888/api/...'
    ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "..."'

Full catalog: deploy/AGENTS.md. Rationale: CLAUDE.md § "The single most important fact".
EOF
fi

exit 0
