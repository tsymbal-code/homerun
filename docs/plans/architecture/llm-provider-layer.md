# Architecture: LLM Provider Layer

This is the current state of the LLM provider abstraction in Homerun.
This document is a reference, not a proposal — it describes how the
layer works today and where to extend it. Plans that add providers or
change routing (`detect_provider`) link here from their
`## Context / References` section.

## Purpose

The layer is responsible for:

1. Unifying disparate LLM HTTP APIs behind a single
   `BaseLLMProvider` interface (`chat`, `chat_stream`,
   `structured_output`, `list_models`).
2. Storing and rotating API keys in the database, encrypted via
   `APP_SECRETS_KEY`.
3. Routing calls by model name (`detect_provider`).
4. Caching the model catalog in the database for fast UI dropdowns.
5. Cost accounting and a monthly budget cap.

The layer **does not** own business logic (chat sessions, strategies,
agents). That lives in higher-level modules in `services/ai/`.

## Key files

| Path | What it holds |
|---|---|
| [backend/services/ai/llm_provider.py](../../../backend/services/ai/llm_provider.py) | The whole abstraction: `BaseLLMProvider`, all provider classes, `LLMManager`, dataclasses (`LLMMessage`, `LLMResponse`, `ToolCall`, `TokenUsage`), the pricing table |
| [backend/services/ai/__init__.py](../../../backend/services/ai/__init__.py) | `get_llm_manager()` — process-wide singleton |
| [backend/models/database.py:1252](../../../backend/models/database.py) | `AppSettings` — columns for keys and provider settings |
| [backend/models/database.py:1899](../../../backend/models/database.py) | `LLMModelCache` — model-list cache |
| `LLMUsageLog` (same file) | Per-call token and cost ledger |
| [backend/api/routes_settings.py](../../../backend/api/routes_settings.py) | `LLMSettings` Pydantic, `PUT /api/settings/llm`, `POST /api/settings/test/llm`, `GET/POST /api/settings/llm/models` |
| [backend/api/settings_helpers.py:741](../../../backend/api/settings_helpers.py) | `apply_update_request` — payload-to-column mapping + `needs_llm_reinit` flag |
| [backend/utils/secrets.py](../../../backend/utils/secrets.py) | `encrypt_secret` / `decrypt_secret` (Fernet keyed off `APP_SECRETS_KEY`) |
| [frontend/src/services/apiSettings.ts:27](../../../frontend/src/services/apiSettings.ts) | `LLMSettings` TS interface — mirror of the server payload |
| [frontend/src/components/ai/AIProvidersView.tsx](../../../frontend/src/components/ai/AIProvidersView.tsx) | UI: `PROVIDERS` array (line 62) plus generic CRUD |

## Contracts

### `BaseLLMProvider` (minimal interface)

```python
class BaseLLMProvider(ABC):
    provider: LLMProvider           # enum identifier
    api_key: Optional[str]

    async def chat(self, messages, model, tools=None, temperature=0.0, max_tokens=4096) -> LLMResponse: ...
    async def structured_output(self, messages, schema, model, temperature=0.0) -> dict: ...
    async def chat_stream(self, messages, model, temperature=0.0, max_tokens=4096) -> AsyncGenerator[str, None]: ...
    async def list_models(self) -> list[dict[str, str]]: ...
```

Implementations:

- `OpenAIProvider` — full HTTP-protocol implementation, used directly
  and as a **delegate** for every OpenAI-compatible backend.
- `AnthropicProvider`, `GoogleProvider` — bespoke implementations for
  their non-OpenAI REST protocols.
- `XAIProvider`, `DeepSeekProvider`, `OpenRouterProvider`,
  `OllamaProvider`, `LMStudioProvider` — thin wrappers around
  `OpenAIProvider` with their own `base_url` and (where needed)
  custom `model_prefixes` or `structured_output_format`.

### `LLMProvider` enum

`"openai" | "anthropic" | "google" | "xai" | "deepseek" | "openrouter" | "ollama" | "lmstudio"`.

The enum value is mirrored across:

- the `app_settings.llm_provider` column,
- the JSON key returned by `GET /api/settings/llm`,
- the dictionary key in `LLMManager._providers`,
- the `?provider=` query parameter in `test/llm` and `models/refresh`,
- the `provider` column in `LLMModelCache`.

Extending the enum always requires synchronously updating each of
those touch points.

### Model → provider routing

`LLMManager.detect_provider(model_name)`
([llm_provider.py:2362](../../../backend/services/ai/llm_provider.py))
matches by name prefix:

| Prefix | Provider |
|---|---|
| `gpt-`, `o1-`, `o3-`, `o4-`, `chatgpt-` | OpenAI |
| `claude-` | Anthropic |
| `gemini-` | Google |
| `grok-` | xAI |
| `deepseek-` | DeepSeek |
| `openrouter/` | OpenRouter |
| `ollama/` | Ollama |
| `lmstudio/` | LM Studio |
| (anything else) | `_preferred_provider` → first configured |

A new provider that hosts models from third-party namespaces (Llama,
Mixtral, Qwen on NIM/Together/Fireworks) **must** introduce its own
unique prefix — otherwise model IDs collide with OpenRouter/Ollama.
The prefix is stripped before the HTTP call inside
`_normalize_model_name_for_provider`
([llm_provider.py:177](../../../backend/services/ai/llm_provider.py)).

### DB columns for a new provider

The repeating template per API provider:

```python
<provider>_api_key = Column(String, nullable=True)        # encrypted
<provider>_base_url = Column(String, nullable=True)       # optional override
```

Local providers (Ollama, LM Studio) keep the API key optional. Hosted
proxies (OpenRouter, NIM, Together) require the key, base URL is
optional.

The most recent example of column addition is
[`alembic/versions/202603200001_add_openrouter_columns.py`](../../../backend/alembic/versions/202603200001_add_openrouter_columns.py).
The current migration head is `202605060001` (see
[`alembic/versions/202605060001_backtest_run_jobs.py`](../../../backend/alembic/versions/202605060001_backtest_run_jobs.py)).
The `migrate` service in [docker-compose.yml:150](../../../docker-compose.yml)
runs `alembic upgrade head` automatically on stack start.

## Dependencies (both directions)

**This layer depends on:**

- `models.database.AppSettings` / `LLMModelCache` / `LLMUsageLog` —
  schema.
- `utils.secrets` — encryption.
- `services.pause_state.global_pause_state` — global AI kill switch.
- External provider HTTP APIs (`httpx.AsyncClient`).

**Depended on by:**

- `services.ai.agent`, `chat_memory`, `market_analyzer`,
  `news_sentiment`, `opportunity_judge`, `resolution_analyzer`,
  `scratchpad` — primary AI consumers (call site:
  `get_llm_manager().chat(...)`).
- `services.ai.skills` / `services.ai.tools` — agent tool calling
  (Cortex, Copilot).
- `services.strategy_reverse_engineer` — separate llm-heavy pipeline.
- `services.autoresearch_service`, `services.strategy_tune_agent`.
- API routes in `routes_ai.py`, `routes_cortex.py`, `routes_agents.py`,
  `routes_autoresearch.py`, `routes_strategy_reverse_engineer.py`.

Changing the public signatures of `BaseLLMProvider` or
`LLMManager.chat / structured_output / chat_stream` ripples through
**every consumer above**. The recommended path for a new provider is
**add**, never change existing contracts.

## Key-save lifecycle

```
UI (AIProvidersView)
   │ updateSettings({ llm: { provider, <provider>_api_key, ... } })
   ▼
PUT /api/settings/llm
   │ → routes_settings.update_settings
   │ → settings_helpers.apply_update_request   ← block: if llm:
   │     set_encrypted_secret(settings, "<provider>_api_key", value)
   │ → commit
   │ flags["needs_llm_reinit"] = True
   ▼
manager = get_llm_manager(); await manager.initialize()
   │ reads AppSettings, decrypt_secret, instantiates <provider>Provider
   ▼
self._providers[LLMProvider.<NEW>] = ...
```

`needs_llm_reinit` is set for **any** change inside the LLM block
([settings_helpers.py:1063](../../../backend/api/settings_helpers.py)),
so callers don't need to trigger a reload separately.

## Model-listing lifecycle

```
UI: refreshLLMModels(provider)
   │
POST /api/settings/llm/models/refresh?provider=<id>
   │ → manager.fetch_and_cache_models(provider_name)
   │     await provider.list_models()
   │     DELETE FROM llm_model_cache WHERE provider=<id>
   │     INSERT new rows (provider, model_id, display_name)
   ▼
GET /api/settings/llm/models?provider=<id>
   │ → manager.get_cached_models(provider_name)
   ▼
UI: ModelCombobox renders the list
```

## Extension points

### Add a new API provider (OpenAI-compatible)

Minimum diff:

1. In `llm_provider.py` — `<NAME>_MODEL_PREFIXES`, enum value, branch
   in `_normalize_model_name_for_provider`, new delegate class,
   registration in `LLMManager.initialize`, default in
   `_provider_default_models`, branch in `detect_provider`.
2. In `models/database.py` — pair of `*_api_key`, `*_base_url`
   columns.
3. Alembic migration with `down_revision = <current head>`.
4. In `routes_settings.py` — `LLMSettings` fields,
   `_ALLOWED_LLM_PROVIDERS`, white-list in `test_llm_connection`,
   export/import in `_apply_llm_configuration_import`.
5. In `settings_helpers.py` — `if llm:` block for the new fields.
6. In `apiSettings.ts` — fields on `LLMSettings`.
7. In `AIProvidersView.tsx` — entry in the `PROVIDERS` array.

Everything else (UI CRUD, retry, usage log, pricing fallback) needs
no changes.

### Add a new API provider with a bespoke protocol

Skip the delegate; implement `chat`, `chat_stream`,
`structured_output`, `list_models` directly. See `AnthropicProvider`
(line 982) and `GoogleProvider` (1349) for the pattern. The other
steps above are unchanged.

### Add pricing for an existing provider

Append entries to the `PRICING` dict at
`llm_provider.py:65`. Without an entry the conservative default
`(5.0, 15.0)` per 1M tokens is used. Pricing affects only the
`LLMUsageLog` rollup, not call behaviour.

### Add a per-purpose model override (e.g. dedicated for news sentiment)

The layer already supports this via `AppSettings.llm_model_assignments`
(JSON). Consumers read their assignment and pass `model=...` to
`manager.chat()`. The provider layer itself needs no changes.

## Known footguns

- **Don't confuse `routes_providers.py` with LLM providers.** That
  file covers **external data providers** (Polybacktest for backtests),
  not LLM. LLM is driven by `routes_settings.py`.
- **Pick `structured_output_format` carefully.** `"json_schema"` is
  the newest format and works on OpenAI, xAI, and most NIM models.
  `"json_object"` is the universal fallback (DeepSeek). `"text"` is
  for providers that support neither — we then rely on
  `_parse_structured_json_content` heuristics.
- **`list_models()` returns `[]` instead of raising** when a provider
  responds with 4xx/5xx. This is intentional: one provider's outage
  must not poison the cache for others.
- **OpenRouter's `vendor/model` collisions.** If a new provider also
  exposes models in `vendor/model` form, the routing prefix
  (`nvidia/`, `together/`, …) is placed **before** the pair, not in
  place of it: e.g. `nvidia/meta/llama-3.3-70b-instruct`. Otherwise
  `detect_provider` confuses it with OpenRouter.
