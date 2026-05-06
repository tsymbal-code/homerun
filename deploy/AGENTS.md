# Homerun Deployment — Agent Guide

This file scopes to `deploy/` and is the source of truth for **where
Homerun actually runs and how to interact with it**. Read it the moment
your task touches deployment, server state, logs, the docker compose
stack, nginx, TLS, or "why doesn't X work on my laptop".

If you skip this file you will: try to `docker compose up` locally
against an empty `.env`, get confused that the database is empty, run
`alembic upgrade head` against nothing, "fix" working code based on a
fictional local stack, and waste the operator's time. Don't.

---

## Where Homerun runs

| Layer | Location |
|---|---|
| Application stack (postgres, redis, backend, 3 workers, frontend) | Remote server **`polyhome-1`** under `/home/polyhome/homerun` |
| Edge HTTPS + Basic Auth | Host nginx on `polyhome-1` (config: `deploy/nginx/homerun.conf`) |
| Source of truth for code | Operator's local checkout (this repo) |
| Source of truth for state (DB rows, settings, secrets, usage logs) | Server's `/home/polyhome/homerun/data/postgres` and `app_settings` table |

**Local =** code editor + git only. No running services. No `localhost:8888`.
No local postgres. The frontend Vite dev server isn't running either —
the operator looks at the prod URL through nginx, not at `localhost:3000`.

---

## SSH access

The operator has `polyhome-1` configured in `~/.ssh/config`. From there:

```bash
ssh polyhome-1                              # interactive shell as `polyhome`
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose ps'
```

Every diagnostic command in this file assumes you're either in an
`ssh polyhome-1` session or wrapping the command with `ssh polyhome-1
'... '`. There is no other way to reach the running stack.

---

## Server filesystem layout

```
/home/polyhome/homerun/                # repo root (rsynced from local)
├── docker-compose.yml                 # the live compose file
├── .env                                # PRODUCTION secrets — never overwrite blindly
├── deploy/
│   ├── sync_remote.sh                 # local helper, harmless on server
│   ├── remote_redeploy.sh             # server runs this from compose dir
│   ├── nginx/homerun.conf             # source for /etc/nginx/sites-available/homerun
│   └── AGENTS.md                      # ← you are here
├── data/                              # NEVER rsync over this from local
│   ├── postgres/                      # postgres bind mount (uid 70 inside container)
│   ├── cache/                         # backend warm caches
│   ├── runtime/                       # worker scratch / snapshots
│   └── machine_learning/              # ML model artifacts
├── backend/                           # synced from local
├── frontend/                          # synced from local
├── alembic/                           # migrations (run by `migrate` compose service)
└── ...
```

**Do not delete or `chown` `data/postgres`** from a shell. Inside the
container postgres runs as uid 70; the host bind mount must keep that
ownership or the next container start will refuse with
`"data directory ... has wrong ownership"`. The current
`remote_redeploy.sh` does `chown -R polyhome:polyhome ${REMOTE_PATH}`
which includes `data/`. That works only because postgres-alpine is
permissive enough for the operator's setup; if you ever see postgres
refusing to start with a permissions error after a redeploy, this is
the first place to look.

---

## Compose stack — what runs on the server

| Service | Container | Image | Host port | Purpose |
|---|---|---|---|---|
| `postgres` | `homerun-postgres` | `postgres:16-alpine` | `127.0.0.1:${POSTGRES_PORT:-5432}` | OLTP store |
| `redis` | `homerun-redis` | `redis:7-alpine` | `127.0.0.1:${REDIS_PORT:-6379}` | Pub/sub + caches |
| `migrate` | `homerun-migrate` | `homerun-backend` | — | One-shot: runs `init_database()` (alembic upgrade head + seed) and exits |
| `backend` | `homerun-backend` | `homerun-backend` | `127.0.0.1:${BACKEND_PORT:-8000}` | FastAPI / uvicorn |
| `worker-trading` | `homerun-worker-trading` | `homerun-backend` | — | Trading plane (orchestrator, executor) |
| `worker-news` | `homerun-worker-news` | `homerun-backend` | — | News + LLM workflows |
| `worker-discovery` | `homerun-worker-discovery` | `homerun-backend` | — | Trader discovery + wallet intelligence |
| `frontend` | `homerun-frontend` | `homerun-frontend` | `${FRONTEND_PORT:-3000}` (set this to `127.0.0.1:3000` in server `.env`!) | nginx serving SPA + reverse-proxy `/api` and `/ws` to `backend:8000` |

`backend` health gates `worker-*` startup. `migrate` health-gates
everything (`condition: service_completed_successfully`). So `docker
compose up -d` is the only command needed to bring the stack up — no
manual `alembic upgrade head`, no manual seeding.

---

## How to look at logs

All logs are container stdout, captured by Docker's `json-file` driver.
Application code logs as JSON via `backend/utils/logger.py`; uvicorn
prints its own access lines.

```bash
ssh polyhome-1
cd /home/polyhome/homerun

# All services, follow:
docker compose logs -f --tail=100

# One service, follow:
docker compose logs -f --tail=200 backend
docker compose logs -f --tail=200 worker-trading
docker compose logs -f --tail=200 worker-news
docker compose logs -f --tail=200 worker-discovery
docker compose logs -f --tail=200 frontend

# One service since a relative time:
docker compose logs --since 10m --tail=500 backend

# Filter to errors / warnings only:
docker compose logs --since 1h backend 2>&1 | rg -i 'level":"(ERROR|WARNING)|traceback'

# Look up a specific request flow by stable_id / opportunity id / order id:
docker compose logs --since 1h backend worker-trading | rg '<id-here>'
```

Operator-level edge nginx logs (TLS handshake errors, basic-auth
failures, 502s when the docker stack is down):

```bash
sudo tail -f /var/log/nginx/access.log
sudo tail -f /var/log/nginx/error.log
```

The healthcheck endpoint is `http://127.0.0.1:8888/health/live`
(through host loopback, behind the docker port mapping).

---

## Inspecting state on the server

Postgres is bound to loopback only. Use the container directly — much
easier than wrestling with the host port mapping:

```bash
ssh polyhome-1
cd /home/polyhome/homerun

# Open a psql shell inside the postgres container:
docker compose exec postgres psql -U homerun -d homerun

# One-shot SQL:
docker compose exec -T postgres psql -U homerun -d homerun -c \
  "select id, llm_provider, ai_default_model from app_settings"

# Most-recent LLM usage:
docker compose exec -T postgres psql -U homerun -d homerun -c \
  "select requested_at, provider, model, success, input_tokens, output_tokens, cost_usd \
   from llm_usage_log order by requested_at desc limit 20"

# Worker control / pause state:
docker compose exec -T postgres psql -U homerun -d homerun -c \
  "select worker_name, is_enabled, is_paused, interval_seconds from worker_control"
```

Redis (cheap to poke at):

```bash
docker compose exec redis redis-cli ping
docker compose exec redis redis-cli --scan --pattern 'homerun:*' | head -20
```

Run a Python snippet inside the live backend image (uses the same code
the running uvicorn loaded — pre-mounted env vars, same DB pool):

```bash
docker compose exec backend python -c "
import asyncio
from services.ai import initialize_ai, get_llm_manager
async def main():
    await initialize_ai()
    print(list(get_llm_manager()._providers.keys()))
asyncio.run(main())
"
```

Hit the API from the server (loopback only — basic auth bypassed):

```bash
curl -fsS http://127.0.0.1:8888/api/settings/llm | jq
curl -fsS -X POST 'http://127.0.0.1:8888/api/settings/test/llm?provider=nvidia'
```

---

## Deploy + restart cycle

Every loop runs from the **local** machine via `deploy/sync_remote.sh`.
Don't `git pull` on the server — there's nothing to pull from; the
server tree is rsynced, not cloned.

```bash
# Local: edit files, then:
./deploy/sync_remote.sh                 # rsync + remote redeploy (default)

# Sync only, no restart (e.g. patching a doc):
DEPLOY_AFTER_SYNC=0 ./deploy/sync_remote.sh

# Restart on server without rebuilding (use GHCR images instead):
ssh polyhome-1 'cd /home/polyhome/homerun && BUILD_IMAGES=0 bash deploy/remote_redeploy.sh'

# Restart a single container without full down/up:
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose restart backend'

# Force-rebuild only the backend image and recreate that one service:
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose up -d --build --no-deps backend'
```

After any redeploy:

```bash
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose ps'
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --tail=80 backend'
```

Healthy = all services `Up (healthy)` except `migrate` (which is `Exited (0)` — that's success).

---

## Things to be aware of

1. **`sync_remote.sh` does NOT exclude `.env`, `data/`, or
   `homerun-settings-*.json`.** That means an `rsync --delete` will
   overwrite the server's production `.env` and `data/` with whatever
   exists locally. The operator chose this trade-off — to avoid a
   wipeout, ensure your local `.env` is identical to the server's
   before syncing, and never have a local `data/postgres/` directory.
   If you're about to sync and you're unsure, do a dry run first:
   ```bash
   rsync -avzn --delete --exclude '.git/' ./ polyhome-1:/home/polyhome/homerun/
   ```

2. **Frontend port binding.** `docker-compose.yml` declares the
   frontend port as `"${FRONTEND_PORT:-3000}:3000"` (no loopback
   prefix). If the server `.env` says `FRONTEND_PORT=3000`, the
   container listens on `0.0.0.0:3000` and bypasses the host nginx
   basic-auth edge. The server `.env` MUST set
   `FRONTEND_PORT=127.0.0.1:3000`. Every other service is already
   loopback-bound in the compose file.

3. **TLS certificate is self-signed** (see chat history with the
   operator for the openssl command used). The browser will prompt
   once per device. To switch to Let's Encrypt, point a public DNS
   name at the server, set `server_name` in
   `deploy/nginx/homerun.conf`, and run
   `sudo certbot --nginx -d <name>`.

4. **There is no CI deploying to the server.** Nothing magical happens
   when you push to git. The only way code reaches the server is the
   operator running `./deploy/sync_remote.sh`. Plan your work
   accordingly — if you commit but don't sync, the server is unchanged.

5. **Workers are paused by default.** A fresh stack boots with
   `global_pause_state` paused. To unpause for a smoke test:
   ```bash
   curl -fsS -X POST 'http://127.0.0.1:8888/api/workers/resume-all' | jq
   ```
   The frontend's "Pause" / "Resume" toggle in the header does the same.

6. **Image pulls vs local build.** `remote_redeploy.sh` defaults to
   `BUILD_IMAGES=1` (build from synced source). For a quick rollback to
   the last GHCR-published image: `BUILD_IMAGES=0 bash
   deploy/remote_redeploy.sh`. This is much faster than a rebuild and
   useful if a local edit broke something.

---

## Common "I broke something" recipes

| Symptom | First check |
|---|---|
| `502 Bad Gateway` from nginx | `docker compose ps` — is `frontend` up? Is `backend` healthy? `sudo tail /var/log/nginx/error.log` |
| Browser "connection refused" | nginx itself is down: `sudo systemctl status nginx`, `sudo nginx -t` |
| `migrate` container failed | `docker compose logs migrate` — almost always a SQL conflict from a hand-edited migration. Fix the migration locally, sync, redeploy. |
| Backend container restart-loops | `docker compose logs --tail=200 backend` — usually missing `APP_SECRETS_KEY` or unreachable postgres. Check `.env`. |
| Postgres won't start after redeploy | Permissions on `data/postgres/`. Inside container postgres needs uid 70: `sudo chown -R 70:70 data/postgres` (only if the chown in `remote_redeploy.sh` clobbered it). |
| Frontend serves stale assets | Browser cached `/assets/<hash>.js`. Hard reload (Ctrl+Shift+R). The hash invalidates on rebuild, so this is rare. |
| WebSocket disconnects every 60s | Edge nginx `proxy_read_timeout` — bumped to 3600 in `deploy/nginx/homerun.conf`. If you see this, confirm the live config matches: `sudo nginx -T \| grep -A2 proxy_read_timeout`. |
| LLM "No models returned" | See the conversation that produced this deploy: NVIDIA NIM `/v1/models` returns duplicate ids; `OpenAIProvider.list_models` already dedupes. If reappears for another provider, apply the same fix there. |

---

## What NOT to do

- Do not run `docker compose up` locally and treat its output as
  representative of production. Local is empty / dev-only.
- Do not `psql`, `redis-cli`, `alembic`, or `curl` against `localhost`
  expecting the prod stack — there's nothing there.
- Do not `git push` and assume the server picks it up. There's no
  webhook, no CI deploy, no agent watching the repo. `sync_remote.sh`
  is the only deploy path.
- Do not delete the server's `data/postgres/` to "reset". That's the
  live database with all positions, settings, encrypted keys, and
  usage history. If you need a clean slate, snapshot it first:
  `pg_dump` inside the postgres container.
- Do not edit files directly on the server (`vim`/`nano` over SSH).
  The next `sync_remote.sh` will overwrite them with `rsync --delete`.
  Edit locally, sync, redeploy. Only exception: `.env` (operator's
  call), `/etc/nginx/sites-available/homerun` (host config, not in
  compose stack), and `/etc/letsencrypt/...` (managed by certbot).
- Do not run `chown -R` on `data/postgres/` to anything other than
  `70:70` or `polyhome:polyhome` (and even the latter is risky — see
  above).
