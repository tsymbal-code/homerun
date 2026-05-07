# Claude Code instructions for Homerun

This file is the entry point Claude Code reads first when opened in
this repo. It is intentionally short — its job is to point at the
canonical sources, not to re-state them.

**Read these in order before doing anything that touches code, tests,
deployment, the database, logs, or "why doesn't X work":**

1. [`agents.md`](agents.md) — full agent codex: core principles
   (ship-complete, no-stubs, no-back-compat), architecture overview,
   tech stack, code patterns, naming conventions, footguns, and the
   **Deployment Topology** section near the top.
2. [`deploy/AGENTS.md`](deploy/AGENTS.md) — source of truth for **where
   Homerun actually runs and how to interact with it** (SSH access,
   logs, diagnostics, redeploy cycle, recipes for common breakages).
3. [`docs/plans/README.md`](docs/plans/README.md) — plan format,
   commit-trailer convention, lifecycle. Every commit produced for a
   plan carries `Plan: <NNNN>`.
4. [`docs/plans/architecture/system-overview.md`](docs/plans/architecture/system-overview.md)
   — runtime topology, three worker planes, cross-process messaging.

## The single most important fact

**This project does NOT run on your local machine.** The application
stack lives on remote server `polyhome-1` (SSH alias resolved via the
operator's `~/.ssh/config`) under `/home/polyhome/homerun`. Locally is
**editor + git only** — no postgres, no backend, no scanner, no Vite
dev server.

Concretely:

- `localhost:8888`, `localhost:3000`, `localhost:5432` — **nothing
  there**. Don't `curl` them, don't `psql` against them.
- `docker compose up` locally — produces an empty stack disconnected
  from prod state. Don't treat its output as representative.
- `alembic upgrade head` locally — runs against an empty SQLite/PG, not
  the production schema.

Every diagnostic command must be wrapped in SSH:

```bash
# Stack status
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose ps'

# Logs (one-shot)
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --tail=200 backend'

# Stream logs
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs -f backend'

# Hit the API (loopback only — basic auth bypassed)
ssh polyhome-1 'curl -fsS http://127.0.0.1:8888/api/strategies' | jq

# Inspect the live database
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres \
  psql -U homerun -d homerun -c "select id, llm_provider from app_settings"'

# Run a Python snippet inside the live backend image
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend python -c "..."'
```

The full command catalog is in [`deploy/AGENTS.md`](deploy/AGENTS.md).

## Deploy cycle

There is **no CI** that pushes to the server. Code reaches `polyhome-1`
only when the operator runs `./deploy/sync_remote.sh` from this local
checkout. `git push` does nothing for the running stack.

```bash
# Local: edit, commit, then deploy
./deploy/sync_remote.sh                    # rsync + remote redeploy
DEPLOY_AFTER_SYNC=0 ./deploy/sync_remote.sh   # sync without restart

# Restart on server without rebuild (use GHCR images)
ssh polyhome-1 'cd /home/polyhome/homerun && BUILD_IMAGES=0 bash deploy/remote_redeploy.sh'

# Restart a single container
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose restart backend'
```

## Documentation conventions

- **Documents in this repo are written in English.** Plans
  (`docs/plans/`), architecture notes
  (`docs/plans/architecture/`), all `.md` under `docs/`, code
  comments, commit messages — all English. The single exception is
  `docs/strategies/` (operator-facing strategy reference, in
  Ukrainian by explicit operator request).
- **Conversation with the operator is in Ukrainian** when running
  interactively. Status updates, summaries, questions back — Ukrainian.
- Plans follow Ralphex format. See
  [`docs/plans/README.md`](docs/plans/README.md) for the skeleton and
  the mandatory **Plan policy header**.
- Every commit landing work for a plan carries a `Plan: <NNNN>` git
  trailer. Retrieval: `git log --grep='Plan: 0001'`.

## Things to never do

- Don't `git pull` on the server. The tree is rsynced, not cloned —
  there is no upstream to pull from.
- Don't edit files directly on the server (vim/nano over SSH). The
  next `sync_remote.sh --delete` will overwrite them.
- Don't delete or `chown` `/home/polyhome/homerun/data/postgres/` —
  that's the live database (positions, settings, encrypted keys,
  usage history). Postgres inside the container runs as uid 70.
- Don't `rm -rf data/` locally either — `sync_remote.sh` does not
  exclude `data/`, so an `rsync --delete` will then wipe the server's
  bind mount.
- Don't add stubs, TODOs, or "implement later" comments. This is a
  financial app; partial code causes silent losses. See `agents.md`
  Core Principles.

## Where to find more

| Topic | File |
|---|---|
| Strategies (28 of them, one doc each) | [`docs/strategies/`](docs/strategies/) |
| Backend internals (FastAPI lifespan, workers, plug-ins) | [`docs/plans/architecture/backend-architecture.md`](docs/plans/architecture/backend-architecture.md) |
| Frontend internals (Jotai, react-query, WS) | [`docs/plans/architecture/frontend-architecture.md`](docs/plans/architecture/frontend-architecture.md) |
| Settings & secret encryption | [`docs/plans/architecture/settings-and-secrets.md`](docs/plans/architecture/settings-and-secrets.md) |
| Database schema, Alembic conventions | [`docs/plans/architecture/database-and-migrations.md`](docs/plans/architecture/database-and-migrations.md) |
| Test layout (pytest + 60 s timeout, no frontend tests) | [`docs/plans/architecture/testing.md`](docs/plans/architecture/testing.md) |
| **AI & LLM end-to-end (which decisions, where, how a "winning market" is identified)** | [`docs/plans/architecture/ai-and-llm.md`](docs/plans/architecture/ai-and-llm.md) |
| LLM provider layer (transport: providers, routing, keys) | [`docs/plans/architecture/llm-provider-layer.md`](docs/plans/architecture/llm-provider-layer.md) |
| **Trader pipeline + "why isn't bot X opening positions"** | [`docs/plans/architecture/trader-pipeline.md`](docs/plans/architecture/trader-pipeline.md) |
| **`worker-trading` process model + GIL bottleneck options** | [`docs/plans/architecture/worker-trading.md`](docs/plans/architecture/worker-trading.md) |
| **Runtime knob-twists (DB-only, not in git): rollback recipes** | [`docs/operational/runtime-tweaks.md`](docs/operational/runtime-tweaks.md) |
| Active plan queue and ordering | [`docs/plans/plan-control-index.md`](docs/plans/plan-control-index.md) |
| UI walkthrough, sandbox/demo mode | [`docs/UI_AND_DEMO_MODE.md`](docs/UI_AND_DEMO_MODE.md) |
