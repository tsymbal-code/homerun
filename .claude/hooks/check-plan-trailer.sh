#!/usr/bin/env bash
# PreToolUse hook for the Homerun project.
#
# Reads a JSON payload {"tool_name": "...", "tool_input": {...}}
# from stdin (Claude Code contract). When the agent is about to run
# a `git commit ...` Bash command and the message body contains
# neither `Plan: <NNNN>` nor the explicit `[no-plan]` opt-out, emit
# a one-paragraph reminder.
#
# Behaviour: warn only. Always exits 0 — does NOT block the commit.
# Project policy (CLAUDE.md, docs/plans/README.md) is to nudge, not
# gate, the trailer convention.
#
# Quiet by default for non-Bash tools, non-`git commit` commands,
# `--amend` (different rules), and messages that already carry
# `Plan: <NNNN>` or `[no-plan]`.
#
# To disable temporarily: chmod -x .claude/hooks/check-plan-trailer.sh.

set -euo pipefail

input=$(cat)

tool=$(python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("tool_name", ""))
except Exception:
    pass
' <<<"$input" 2>/dev/null || true)

[ "$tool" = "Bash" ] || exit 0

command=$(python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    print(data.get("tool_input", {}).get("command", ""))
except Exception:
    pass
' <<<"$input" 2>/dev/null || true)

# Only fire on git commit (skip git commit --amend; amend rewrites
# an existing commit and trailers are usually inherited).
if ! echo "$command" | grep -qE '\bgit commit\b'; then
  exit 0
fi
if echo "$command" | grep -qE '\bgit commit\b.*--amend'; then
  exit 0
fi

# `[no-plan]` anywhere in the command silences the warning. Used for
# typo fixes, deps bumps, and emergency hotfixes — see
# docs/plans/README.md § Commits and traceability.
if echo "$command" | grep -q '\[no-plan\]'; then
  exit 0
fi

# `Plan: <NNNN>` (four digits) anywhere in the command means the
# trailer is in place.
if echo "$command" | grep -qE 'Plan:[[:space:]]*[0-9]{4}'; then
  exit 0
fi

cat <<'EOF'

[homerun reminder] This `git commit` does not appear to carry a
`Plan: <NNNN>` trailer. Per docs/plans/README.md § Commits and
traceability, every plan-driven commit needs one — that's how
`git log --grep='Plan: NNNN'` finds it later.

Add it to the commit message body, on its own line:

    Plan: 0014

If this commit is genuinely off-plan (typo fix, deps bump from a
script, emergency hotfix), add the literal string `[no-plan]`
anywhere in the message to silence this warning.

This is a nudge, not a block — the commit proceeds either way.
EOF

exit 0
