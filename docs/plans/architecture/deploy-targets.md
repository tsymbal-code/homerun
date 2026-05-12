# Deploy targets — branch-derived host mapping

## Purpose

Homerun is deployed to two servers — production and staging — that
both run an **identical** docker compose stack. Until plan 0056 the
operator kept these two environments in two separate git repos, each
hard-coding its own hostname. That model breaks the moment both
environments live in one repo: every architecture note, every agent
rule, every deploy recipe would lie half the time, and every
`main ↔ dev` merge would produce mechanical conflicts on hostname
literals.

This note replaces that pattern. The git branch is now the only thing
that selects the deploy target. Every agent-facing doc and recipe uses
the placeholder `<HOMERUN_HOST>`; the agent resolves it to the
literal alias from the table below by running
`git branch --show-current` as the first command of any session. The
trade-off is one extra command at session start — accepted because it
eliminates an entire class of "agent SSHed into the wrong host" bugs
and removes hostname conflicts from the branch-merge process.

`<HOMERUN_HOST>` is a literal token agents recognise. It is never a
runtime variable: shell scripts that actually talk to the server
(`deploy/sync_remote.sh`, `scripts/run_tests_remote.sh`,
`.claude/hooks/remind-ssh.sh`) resolve the branch → host mapping
themselves through a shared `case` block. Documentation uses the
placeholder; code uses the resolved literal.

## Branch → host mapping

This is the **single source of truth** for the mapping. Every other
file in the repo either uses the `<HOMERUN_HOST>` placeholder and
points here, or carries one of the literal aliases below for one of
the four narrow reasons documented in
[Where the mapping is read](#where-the-mapping-is-read).

| Branch | Environment | SSH alias       | Remote path                | Role |
|--------|-------------|-----------------|----------------------------|------|
| `main` | production  | `polyhome-prod` | `/home/polyhome/homerun`   | Live trading, real PnL. Operator-visible UI at the public hostname. |
| `dev`  | staging     | `polyhome-1`    | `/home/polyhome/homerun`   | Shadow-trading soak host. No live submission. UI accessible via SSH tunnel only. |

Adding a third environment (preview, hotfix-soak) means appending one
row here, one branch arm in the `case` block of
[`deploy/sync_remote.sh`](../../../deploy/sync_remote.sh), and one
matching arm in
[`.claude/hooks/remind-ssh.sh`](../../../.claude/hooks/remind-ssh.sh).
No other doc needs to know.

## Determining the current target

Before running any `ssh ...` or `./deploy/sync_remote.sh` command:

```bash
git branch --show-current
```

…and substitute `<HOMERUN_HOST>` in any doc snippet with the alias
from the table above.

To confirm the deploy script agrees with the branch (no env override
in effect), run:

```bash
bash deploy/sync_remote.sh --dry-run-host
```

The script prints `branch=<X> env=<Y> host=<Z>` and exits 0 without
syncing. This is the canonical way to verify "if I ran the sync right
now, where would it go?"

### Override (`FORCE_HOST`)

The only legitimate reason to deploy a branch to a non-matching host
is a deliberate soak: e.g. push the `main` branch onto `polyhome-1`
to validate a release before flipping the public host. The recipe:

```bash
FORCE_HOST=1 SSH_HOST=polyhome-1 ./deploy/sync_remote.sh
```

Without `FORCE_HOST=1`, the script refuses any mismatch between the
branch-derived target and an explicit `SSH_HOST` env override —
intentional friction that catches `git checkout main; ./deploy/sync_remote.sh`
right after staging work on `dev`. Bare `SSH_HOST` overrides without
`FORCE_HOST` are a footgun, not a workflow.

`FORCE_HOST` is never used for "I'm not sure which branch I'm on" —
that is the moment to run `git branch --show-current`, not to bypass
the guard.

## Cross-branch identical surfaces

Stage and prod are stack-identical by design. The architecture note
exists in part to make this contract explicit so future plans don't
accidentally introduce per-environment divergence:

- **docker compose stack.** Same `docker-compose.yml`, same service
  names (`postgres`, `redis`, `migrate`, `backend`, `worker-trading`,
  `worker-news`, `worker-discovery`, `frontend`), same image tags on
  redeploy.
- **Container names.** `homerun-postgres`, `homerun-redis`,
  `homerun-backend`, `homerun-worker-trading`, etc. — identical on both
  hosts.
- **Port mappings.** All services bind to `127.0.0.1` on the host
  with the same ports (`8888` for the backend health/API loopback,
  `3000` for the frontend through edge nginx).
- **`.env` shape.** Same keys, same `APP_SECRETS_KEY`-encrypted
  envelope for secrets. Values may differ (different API keys,
  different basic-auth credentials), but the schema does not.
- **Postgres tuning.** The compose `command:` block in
  `docker-compose.yml` assumes identical host hardware (4 vCPU,
  ~8 GiB RAM, QEMU-backed SSD). If a host ever diverges in hardware,
  that divergence belongs in a per-environment overlay
  (`docker-compose.<env>.yml`) introduced by a separate plan; today
  no such overlay exists.
- **Alembic migrations.** Same chain head on both hosts after every
  redeploy (`migrate` one-shot service runs to completion before
  `backend` starts).

The only branch-derived thing in the repo is the SSH alias used by
the three scripts that talk to the server. Everything else is
identical.

## Where the mapping is read

These four files materialise the mapping at runtime. They are the
only places in `deploy/`, `.claude/`, `.cursor/`, and `scripts/`
where literal hostnames appear; every other agent-facing doc uses
`<HOMERUN_HOST>` and links here.

| File | What it does with the mapping |
|---|---|
| [`deploy/sync_remote.sh`](../../../deploy/sync_remote.sh) | Resolves branch → host in a `case` block, refuses cross-target syncs without `FORCE_HOST=1`, exposes `--dry-run-host` for verification. |
| [`scripts/run_tests_remote.sh`](../../../scripts/run_tests_remote.sh) | Same `case` block — picks the host before SSHing to run pytest against the live Postgres. Same `FORCE_HOST` guard. |
| [`.claude/hooks/remind-ssh.sh`](../../../.claude/hooks/remind-ssh.sh) | `UserPromptSubmit` hook. Resolves branch → host at hook-invocation time and writes the *resolved* alias into the reminder so the example commands the agent sees are already host-correct. |
| [`.claude/settings.json`](../../../.claude/settings.json) | Bash permissions allowlist. JSON-schema patterns don't support placeholders, so this file is the one exception — it mirrors both `polyhome-prod` and `polyhome-1` allow/deny entries side by side. |

If you find a fifth file that wants to know the host at runtime,
either factor the `case` block into a shared shell helper (and source
it from all four scripts) or — for Python code — read
`os.environ["HOMERUN_HOST"]` set by the calling shell. Do not
duplicate the `case` block again.

## Dependencies

**Incoming (files that depend on this note):**

- All four files in [Where the mapping is read](#where-the-mapping-is-read).
- Top-level agent guides ([`CLAUDE.md`](../../../CLAUDE.md),
  [`agents.md`](../../../agents.md),
  [`.cursor/rules/homerun.mdc`](../../../.cursor/rules/homerun.mdc),
  [`deploy/AGENTS.md`](../../../deploy/AGENTS.md)) — these point at
  this note as the canonical reference for the mapping and use
  `<HOMERUN_HOST>` in every command example.

**Outgoing (what this note depends on):**

- The operator's local `~/.ssh/config` defines both aliases. This is
  out of scope for the repo; the assumption is documented here.
- Both hosts have the same docker compose stack laid out at
  `/home/polyhome/homerun`. Enforced by
  [`deploy/sync_remote.sh`](../../../deploy/sync_remote.sh) and
  documented in [system-overview.md](system-overview.md).

## Extension points

| When you want to… | Touch |
|---|---|
| Add a new environment (preview, hotfix-soak) | One row in the table above + one arm in the `case` block of `deploy/sync_remote.sh`, `scripts/run_tests_remote.sh`, and `.claude/hooks/remind-ssh.sh` + one allowlist mirror in `.claude/settings.json`. |
| Diverge a setting between prod and stage | Introduce a `docker-compose.<env>.yml` overlay in a separate plan. The overlay is selected by the same branch-derived `<env>` value. Do not split the SSOT into multiple docs. |
| Wire host into Python code | Read `os.environ["HOMERUN_HOST"]` from the shell wrapper that invokes the Python process. Do not import or duplicate the `case` block in Python. |
| Migrate from rsync deploy to a CI push | Out of scope for this note. CI would resolve the target from the pushed ref, not from a local branch — the SSOT table still applies, the resolution path changes. |

Last verified: 2026-05-12
