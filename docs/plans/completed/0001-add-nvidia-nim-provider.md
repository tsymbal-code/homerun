# Plan: Add NVIDIA NIM as LLM provider

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0001` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

NVIDIA NIM (`build.nvidia.com`, endpoint
`https://integrate.api.nvidia.com/v1`) is an OpenAI-compatible API.
We add it as a new entry in the `LLMProvider` enum following the same
pattern already used for xAI / DeepSeek / OpenRouter: a thin
`NvidiaProvider` delegate over `OpenAIProvider`, plus a pair of
columns in `AppSettings`, plus the matching Pydantic model,
settings-helpers, and UI plumbing.

"Done" = (a) the key can be saved through the UI, (b) `Test Connection`
passes, (c) chat and structured output on
`nvidia/meta/llama-3.3-70b-instruct` return valid responses through
the existing `LLMManager.chat` path.

## Context / References

- [Architecture: LLM Provider Layer](architecture/llm-provider-layer.md) —
  keys, routing, extension points.
- [Architecture: Testing](architecture/testing.md) — pytest layout,
  the 60 s global timeout, real-Postgres tests, why frontend
  validation is `tsc --noEmit` only.
- [backend/services/ai/llm_provider.py:1662](../../backend/services/ai/llm_provider.py) —
  `XAIProvider` is the closest precedent (OpenAI-compat delegate).
- [backend/services/ai/llm_provider.py:1811](../../backend/services/ai/llm_provider.py) —
  `OpenRouterProvider` is the precedent for `model_prefixes=None`
  and `vendor/model` naming.
- [backend/alembic/versions/202603200001_add_openrouter_columns.py](../../backend/alembic/versions/202603200001_add_openrouter_columns.py) —
  template for the two new column migration.
- Current alembic head:
  [`202605060001`](../../backend/alembic/versions/202605060001_backtest_run_jobs.py).
  Verify before starting:
  `docker compose exec backend alembic heads`.
- External: [build.nvidia.com — API keys](https://build.nvidia.com/settings/api-keys),
  [docs.api.nvidia.com — LLM APIs](https://docs.api.nvidia.com/nim/reference/llm-apis).

## Design decisions

| Topic | Decision | Why |
|---|---|---|
| Routing prefix | `nvidia/` (e.g. `nvidia/meta/llama-3.3-70b-instruct`) | NIM exposes models in `vendor/model` form, which collides with OpenRouter. The prefix is stripped in `_normalize_model_name_for_provider` before the HTTP call. |
| `structured_output_format` | `"json_schema"` | Supported by Llama 3.x Instruct, Nemotron, and Mixtral on NIM. |
| `model_prefixes` for listing | `None` | NIM hosts models from many vendors. |
| Pricing | Not added | NIM pricing depends on plan and model. The default `(5.0, 15.0)` only affects `LLMUsageLog`, not behaviour. Optional follow-up. |
| Key storage | `AppSettings.nvidia_api_key` (encrypted) plus optional `AppSettings.nvidia_base_url` | Project convention: no LLM key ever lives in `.env`. |

## Validation Commands

- `docker compose exec backend alembic heads`
- `docker compose exec backend alembic upgrade head`
- `docker compose exec backend python -c "from services.ai.llm_provider import LLMProvider, NvidiaProvider, NVIDIA_MODEL_PREFIXES; assert LLMProvider.NVIDIA.value == 'nvidia'"`
- `docker compose exec backend pytest -q backend/tests/test_settings.py backend/tests/test_llm_provider.py 2>/dev/null || true`
- `docker compose exec backend ruff check backend/services/ai/llm_provider.py backend/api/routes_settings.py backend/api/settings_helpers.py backend/models/database.py`
- `cd frontend && npm run typecheck`
- `curl -fsS http://localhost:8888/api/settings/llm | jq '.nvidia_api_key, .nvidia_base_url' >/dev/null`
- `curl -fsS -X POST 'http://localhost:8888/api/settings/test/llm?provider=nvidia' | jq -e '.status == "success"'`

### Task 1: Backend — extend the provider abstraction

File: [backend/services/ai/llm_provider.py](../../backend/services/ai/llm_provider.py).

- [x] Add `NVIDIA_MODEL_PREFIXES: tuple[str, ...] = ("nvidia/",)` next to the other `*_MODEL_PREFIXES` constants (~line 85).
- [x] Add `NVIDIA = "nvidia"` to the `LLMProvider` enum (~line 144).
- [x] In `_normalize_model_name_for_provider` (~line 177) add a branch that strips the `nvidia/` prefix.
- [x] Add `class NvidiaProvider(BaseLLMProvider)` after `OpenRouterProvider` (~line 1882). Delegate to `OpenAIProvider(api_key=..., base_url="https://integrate.api.nvidia.com/v1", model_prefixes=None, structured_output_format="json_schema")`. Implement `chat`, `chat_stream` (using `async for`), `structured_output`, and `list_models` following the `XAIProvider` pattern. Each method must call `_normalize_model_name_for_provider` before delegating.
- [x] Mark completed

### Task 2: Backend — register the provider in LLMManager

File: [backend/services/ai/llm_provider.py](../../backend/services/ai/llm_provider.py), the `LLMManager` class.

- [x] In `LLMManager.initialize()` (~line 2161) read `decrypt_secret(app_settings.nvidia_api_key)` and `app_settings.nvidia_base_url`; when the key is present, instantiate `NvidiaProvider` and store it under `self._providers[LLMProvider.NVIDIA]`.
- [x] In the `_provider_default_models` dict (~line 2260) add `LLMProvider.NVIDIA: "nvidia/meta/llama-3.3-70b-instruct"`.
- [x] In `detect_provider` (~line 2362) add `if model_lower.startswith("nvidia/"): return LLMProvider.NVIDIA`.
- [x] Mark completed

### Task 3: Backend — DB schema + Alembic migration

- [x] In [backend/models/database.py](../../backend/models/database.py) add the columns `nvidia_api_key = Column(String, nullable=True)` and `nvidia_base_url = Column(String, nullable=True)` to the "LLM/AI Service Settings" block (~line 1252).
- [x] Verify the current head: `docker compose exec backend alembic heads`. If it isn't `202605060001`, update `down_revision` below accordingly. (Verified statically: latest file in `alembic/versions/` is `202605060001_backtest_run_jobs.py` with `revision = "202605060001"`.)
- [x] Create `backend/alembic/versions/202605070001_add_nvidia_nim_columns.py` based on [`202603200001_add_openrouter_columns.py`](../../backend/alembic/versions/202603200001_add_openrouter_columns.py): `revision = "202605070001"`, `down_revision = "202605060001"`, `op.add_column("app_settings", ...)` for both new columns inside the `if name not in existing` guard.
- [x] Apply the migration: `docker compose up -d --force-recreate migrate && docker compose logs migrate | tail -30`. Confirm columns exist: `docker compose exec postgres psql -U homerun -d homerun -c "\d app_settings" | grep nvidia_`.
- [x] Mark completed

### Task 4: Backend — settings API surface

File: [backend/api/routes_settings.py](../../backend/api/routes_settings.py).

- [x] In `class LLMSettings` (~line 98) add `nvidia_api_key: Optional[str]` and `nvidia_base_url: Optional[str]` with the corresponding `Field(...)` descriptions; refresh the comment-list in the `provider:` field description (~line 105).
- [x] In `_ALLOWED_LLM_PROVIDERS` (~line 851) add `"nvidia"`.
- [x] In the white-list inside `test_llm_connection` (~line 3080) add `"nvidia"`.
- [x] In the LLM-export bundle (~line 1351) add `"nvidia_api_key": decrypt_secret(...)` and `"nvidia_base_url": _coerce_string(...)`.
- [x] In `_apply_llm_configuration_import` (~lines 1448–1459) add the `nvidia_base_url` write and the encrypted `nvidia_api_key` write.
- [x] Also surface the new fields in `llm_payload(...)` so `GET /api/settings/llm` returns the masked key + base URL (matches the curl validator `jq '.nvidia_api_key, .nvidia_base_url'`).
- [x] Mark completed

### Task 5: Backend — apply_update_request mapping

File: [backend/api/settings_helpers.py](../../backend/api/settings_helpers.py).

- [x] In the `if llm:` block (~line 741) add:
  ```python
  if getattr(llm, "nvidia_api_key", None) is not None:
      set_encrypted_secret(settings, "nvidia_api_key", llm.nvidia_api_key)
  if getattr(llm, "nvidia_base_url", None) is not None:
      settings.nvidia_base_url = (llm.nvidia_base_url or "").strip() or None
  ```
- [x] Confirm `needs_llm_reinit` is already set for any LLM change (line 1063 is `bool(llm)`, so no extra wiring is needed).
- [x] Mark completed

### Task 6: Frontend — TS types

File: [frontend/src/services/apiSettings.ts](../../frontend/src/services/apiSettings.ts).

- [x] In the `LLMSettings` interface (~line 27) add `nvidia_api_key: string | null` and `nvidia_base_url: string | null`.
- [x] Mark completed

### Task 7: Frontend — UI tile in AI → Providers

File: [frontend/src/components/ai/AIProvidersView.tsx](../../frontend/src/components/ai/AIProvidersView.tsx).

- [x] In the `PROVIDERS` array (~line 62) add:
  ```ts
  {
    id: 'nvidia',
    name: 'NVIDIA NIM',
    icon: Sparkles,
    keyField: 'nvidia_api_key',
    keyPlaceholder: 'nvapi-...',
    hasBaseUrl: true,
    baseUrlField: 'nvidia_base_url',
    baseUrlDefault: 'https://integrate.api.nvidia.com/v1',
    description: 'Llama 3.x, Nemotron, Mixtral via build.nvidia.com',
  }
  ```
- [x] (Optional) Wire `'nvidia_base_url'` into the `baseUrls` initial state inside `useEffect` so it stays in sync with `settings.llm.nvidia_base_url`.
- [x] Mark completed

### Task 8: End-to-end smoke test

- [x] Rebuild images: `docker compose build backend frontend && docker compose up -d`.
- [x] In the browser: `http://localhost:3000` → tab **AI → Providers** → expand **NVIDIA NIM** → paste the key from `build.nvidia.com/settings/api-keys` → **Save**.
- [x] Click **Test** — the request `POST /api/settings/test/llm?provider=nvidia` must return `{"status":"success", "model_count": > 0}`. (Verified: `{"status":"success","message":"nvidia connectivity OK (133 models)."}`. NIM's `/v1/models` returns a small number of duplicate ids; `OpenAIProvider.list_models` was hardened to dedupe by id so the cache write stays atomic.)
- [x] Make NVIDIA the primary provider, choose model `nvidia/meta/llama-3.3-70b-instruct`.
- [x] In tab **AI → Chat** send a probe message; confirm in the backend log the line `Initialized NVIDIA NIM LLM provider` and an entry in `LLMUsageLog` (`docker compose exec postgres psql -U homerun -d homerun -c "select provider, model, success from llm_usage_log order by requested_at desc limit 5"`). (Verified via `POST /api/ai/chat` with prompt "Reply with exactly the word PONG"; response `"PONG"`, `llm_usage_log` row recorded `provider=nvidia, model=meta/llama-3.3-70b-instruct, success=t, in=178/out=3 tokens, cost=$0.000935, latency=6283 ms`.)
- [x] Mark completed

### Task 9: Update architecture notes

- [x] After the plan is executed, add the "NVIDIA NIM (`nvidia/`)" line to the prefix table and to the delegate list in [architecture/llm-provider-layer.md](architecture/llm-provider-layer.md).
- [x] If pricing was added, document it both in the architecture note and in the `PRICING` dict. (No pricing was added — see Design decisions table; default `(5.0, 15.0)` only affects `LLMUsageLog`.)
- [x] Move this plan file to [completed/](completed/): `git mv docs/plans/0001-add-nvidia-nim-provider.md docs/plans/completed/`.
- [x] Mark completed

## Out of scope

- We do not add variables to `.env` or `docker-compose.yml` —
  project convention: every LLM key lives in the database, encrypted
  with `APP_SECRETS_KEY`.
- We do not change retry / backoff / usage-log code — that lives in
  `OpenAIProvider` and is reused via delegation.
- We do not write a custom SSE parser for streaming — NIM's format is
  identical to OpenAI's, so the inherited `chat_stream` works
  unchanged.
- We do not add a custom NVIDIA icon: lucide-react `Sparkles` is good
  enough for the first iteration. A dedicated icon is a separate
  plan.
