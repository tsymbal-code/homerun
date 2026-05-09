# Architecture: Settings & Secrets

The `AppSettings` row in Postgres is the single source of truth for
operator-tunable configuration in Homerun: API keys, provider
selection, scanner thresholds, notification preferences, UI lock,
and ~150 other knobs. The `.env` file feeds only **infra-level**
parameters (DB URL, Redis URL, the encryption key itself, log
level). Everything else lives in the database, behind the UI's
**Settings** tab, and is hot-reloadable.

This separation is load-bearing: it lets the operator change behaviour
without restarts, lets the desktop launcher and docker compose share
one configuration model, and keeps secrets encrypted at rest.

## Purpose

This layer owns:

1. The schema of operator-facing configuration (`AppSettings` table).
2. The encryption envelope for secrets (Fernet over Postgres `String`
   columns).
3. The Pydantic ↔ ORM mapping (`apply_update_request`) and the
   "what needs to be re-initialised when this changes" flags.
4. The hot-reload triggers fired after a save (LLM manager, trading
   proxy, runtime overrides, UI lock, Chainlink direct feed).

It does **not** own consumers — every service reads its own slice of
settings on demand, with the layer providing only `decrypt_secret(...)`
to unwrap encrypted columns.

## Key files

| Path | What it holds |
|---|---|
| [backend/models/database.py:1248+](../../../backend/models/database.py) | `class AppSettings(Base)` — every column is in one model, `id="default"` is the singleton row |
| [backend/utils/secrets.py](../../../backend/utils/secrets.py) | `encrypt_secret`, `decrypt_secret`, `is_encrypted`, `_ENC_PREFIX = "enc:v1:"`, Fernet derivation |
| [backend/api/routes_settings.py](../../../backend/api/routes_settings.py) | Pydantic models (`PolymarketSettings`, `KalshiSettings`, `OracleSettings`, `LLMSettings`, …), `GET/PUT /api/settings`, section-specific `PUT /api/settings/<section>`, `POST /api/settings/test/<service>`, `POST /api/settings/export` / `import` |
| [backend/api/settings_helpers.py](../../../backend/api/settings_helpers.py) | `apply_update_request(settings, request)` — single source of truth for "what changed", `set_encrypted_secret`, the `needs_*_reinit` flag dictionary |
| [backend/config.py:968+](../../../backend/config.py) | `apply_runtime_settings_overrides()` (line 968), `apply_events_settings()` (line 781), `apply_search_filters()` (line 872) — applies DB overrides over env defaults |
| [frontend/src/services/apiSettings.ts](../../../frontend/src/services/apiSettings.ts) | TS interfaces mirroring each Pydantic section |
| [frontend/src/components/SettingsPanel.tsx](../../../frontend/src/components/SettingsPanel.tsx), [AccountSettingsFlyout.tsx](../../../frontend/src/components/AccountSettingsFlyout.tsx), [ai/AIProvidersView.tsx](../../../frontend/src/components/ai/AIProvidersView.tsx) | Three UI surfaces that read/write settings |

## What goes where: `.env` vs database

A simple rule:

| Lives in `.env` | Lives in `AppSettings` |
|---|---|
| `APP_SECRETS_KEY` (the master key — without it nothing else can be read) | All API keys: `polymarket_*`, `kalshi_*`, `openai_api_key`, `anthropic_api_key`, every LLM provider |
| `DATABASE_URL`, `REDIS_URL` | All operational thresholds: scan interval, position size caps, slippage, daily loss limit |
| `LOG_LEVEL` | Notifications: telegram bot token + chat id, per-event toggles |
| Image tag, port bindings | Feature toggles: `ai_enabled`, per-feature LLM enable/disable |
| `HF_TOKEN` (one-time, optional) | UI lock password hash, session duration |
| `POLYGON_RPC_URL` (default works) | Trading proxy URL (encrypted), VPN required toggle |

The cardinal sins, both equally bad:

- Putting an LLM key into `.env` — the operator can't rotate without
  redeploying, and the key sits plaintext on the filesystem.
- Putting `APP_SECRETS_KEY` into the database — circular: the DB
  needs the key to be readable, the key would need the DB to be
  readable.

## The encryption envelope

[`utils/secrets.py`](../../../backend/utils/secrets.py) is small and
fixed:

- **Master key**: `APP_SECRETS_KEY` from `os.environ`. Required for
  the encryption layer to initialise; absent → secrets are stored
  plaintext **and a warning logs**. This fallback is intentional for
  bootstrap convenience; production deployments must set the key.
- **Key derivation**: `base64.urlsafe_b64encode(sha256(secret_key))`
  — Fernet wants a 32-byte URL-safe key, SHA-256 of arbitrary input
  is the safe normalisation.
- **Envelope**: `enc:v1:<fernet-token>`. Plaintext stored without
  prefix is treated as plaintext on read. This means values stored
  before the master key was set will keep working until they're
  next written.
- **Public API**: `encrypt_secret`, `decrypt_secret`, `is_encrypted`.
  Both read and write are no-ops when value is `None` or `""`.

Losing `APP_SECRETS_KEY` means losing **every** encrypted value in
the database. There is no key rotation primitive today; rotation
would require re-encrypt-on-read once and re-write under the new
key (a future plan, not implemented).

## Database column conventions

For each provider/integration, the convention is one or more of:

```python
<service>_api_key       = Column(String, nullable=True)   # encrypted
<service>_api_secret    = Column(String, nullable=True)   # encrypted
<service>_api_passphrase = Column(String, nullable=True)  # encrypted
<service>_private_key   = Column(String, nullable=True)   # encrypted
<service>_base_url      = Column(String, nullable=True)   # plaintext, optional
<service>_enabled       = Column(Boolean, default=False)  # plaintext
```

All `_api_*` and `_*_key`, `_*_secret`, `_*_password`,
`_*_passphrase`, `_*_token` columns are routed through
`set_encrypted_secret()` on write and `decrypt_secret()` on read.
Plaintext-friendly columns (URLs, booleans, ints, JSON) bypass that
helper.

The singleton row uses `id="default"`. Code never inserts a second
row; `_get_or_create_settings_row(session)` either returns the
existing row or creates the singleton.

## The save lifecycle

A typical save (e.g. updating an LLM key) follows this exact path —
the same one [LLM Provider Layer](llm-provider-layer.md) shows but
generalised:

```
UI (Settings tile)
   │ updateSettings({ <section>: { <field>: value, ... } })
   ▼
PUT /api/settings  (or section-specific PUT /api/settings/<section>)
   │ → routes_settings.update_settings
   │ → settings_helpers.apply_update_request(settings_row, request)
   │     for each provided section:
   │       set the plaintext columns directly
   │       call set_encrypted_secret() for secret columns
   │     return flags = {
   │       needs_llm_reinit: bool(request.llm),
   │       needs_proxy_reinit: bool(request.trading_proxy),
   │       needs_filter_reload: bool(request.scanner) or ...,
   │       needs_events_reload: request.events is not None,
   │       needs_ui_lock_reload: request.ui_lock is not None,
   │       reset_ui_lock_sessions: <password or enabled changed>,
   │       needs_chainlink_direct_rearm: <oracle creds touched>,
   │     }
   │ session.commit()
   ▼
After commit, OUTSIDE the DB session (so the next read sees fresh data):
   if needs_llm_reinit:        get_llm_manager().initialize()
   if needs_proxy_reinit:      reload_proxy_settings()
   if needs_filter_reload:     apply_runtime_settings_overrides()
   if needs_events_reload:     apply_runtime_settings_overrides()
   if needs_ui_lock_reload:    ui_lock_service.mark_settings_dirty()
   if reset_ui_lock_sessions:  ui_lock_service.invalidate_all_sessions()
   if needs_chainlink_direct_rearm:
                               reference_runtime.rearm_chainlink_direct()
```

Two important properties of this design:

1. **Hot reloads run after the commit, not inside the transaction.**
   That guarantees `LLMManager.initialize()` reads its own writes —
   a re-init happening inside the open session would see stale
   data.
2. **Failures of any reinit don't roll back the save.** The settings
   are persisted, the reinit is attempted, and any error is logged
   but doesn't surface as 500 to the UI. The tradeoff: a malformed
   key gets persisted; the operator sees the error in the **Test**
   button or in the next AI call.

## Pydantic ↔ ORM mapping

`apply_update_request` ([settings_helpers.py:687+](../../../backend/api/settings_helpers.py))
is intentionally one big function. It walks each Pydantic section
in turn, copying attributes onto the ORM row. Three patterns repeat:

```python
# (a) Plaintext field — copy when provided.
if scan.scan_interval_seconds is not None:
    settings.scan_interval_seconds = scan.scan_interval_seconds

# (b) Encrypted field — route through helper.
if pm.api_key is not None:
    set_encrypted_secret(settings, "polymarket_api_key", pm.api_key)

# (c) String field with empty-string-clears semantics.
settings.openrouter_base_url = (llm.openrouter_base_url or "").strip() or None
```

The semantics:

- `None` (omitted from payload) → leave column unchanged.
- `""` (empty string) → clear the column to `NULL`.
- Any other value → write it.

This contract is mirrored by every TS wrapper in
`services/apiSettings.ts` — sending `null` or omitting a field is a
no-op; sending an empty string clears.

The "masked secret" wart: when the GET returns settings, secrets
come back as the literal string `"********"` so the UI can show
"key configured" without leaking the value. The UI sends this same
mask back unchanged. `set_encrypted_secret` does not detect this
mask itself — the section-specific update routes
(e.g. polybacktest at `routes_providers.py:387`) do, and forward
"unchanged" through. New endpoints adding masked-secret handling
should follow that pattern (`if cleaned == _API_KEY_PRESENT_MASK:
pass`).

## Runtime overrides (`config.apply_runtime_settings_overrides`)

A subset of `AppSettings` columns shadows values that also live in
`backend/config.py` (the `Settings` dataclass loaded from `.env`).
At startup and after relevant saves,
`apply_runtime_settings_overrides()` ([config.py:938](../../../backend/config.py))
copies non-null DB values onto the in-memory dataclass.

Precedence is deterministic and documented:

1. Non-null DB override (`AppSettings`).
2. Environment value (already loaded into `settings`).
3. Code default.

The runtime singletons that read this — scanner, events worker,
discovery worker, search filters — do `from config import settings`
and re-read on tick. There is no observer pattern; the contract is
"after a save, `apply_runtime_settings_overrides()` has run, so the
next tick reads the new value."

## Export / import

`POST /api/settings/export` and `POST /api/settings/import`
support backing up and restoring the operator's configuration.
The categories are explicit
([routes_settings.py:830+](../../../backend/api/routes_settings.py)):
`MARKET_CREDENTIALS`, `VPN_CONFIGURATION`, `LLM_CONFIGURATION`,
`TELEGRAM_CONFIGURATION` (more to come).

Two notes for plan authors:

- The export bundle decrypts secrets in transit (so the JSON
  contains plaintext keys). The operator is expected to handle that
  bundle as sensitive.
- Adding a new section to export/import requires updating both the
  enum and the `_apply_<section>_import` helper. Today these are
  not auto-generated from the Pydantic model; they're hand-mirrored.
  When adding a new LLM provider column (or any new credential),
  remember to thread it through both flows — an example is the
  NVIDIA NIM plan's Task 4.

## Dependencies (both directions)

**This layer depends on:**

- `models.database.AppSettings` schema.
- `APP_SECRETS_KEY` env var (degraded mode without it).
- `cryptography.Fernet` (transitively, via `pip install cryptography`).
- The Pydantic models in `routes_settings.py` for input validation.

**Depended on by:**

- Every service that reads a setting: `LLMManager`,
  `live_execution_service`, `kalshi_client`, `polymarket_client`,
  `notifier`, `trading_proxy`, the scanner/events workers,
  `ui_lock_service`, `chainlink_direct_feed`.
- The frontend Settings tab, AI Providers tile, Account Settings
  flyout, AI Models view.
- The export/import flow used by operators migrating between
  installations.

## Extension points

| When you want to… | Touch |
|---|---|
| Add a new credential column | Add `<service>_<field>` to `AppSettings`, generate an Alembic migration, extend the Pydantic section in `routes_settings.py`, add the encrypted write to `apply_update_request`'s `if <section>:` block, add the field to the matching TS interface in `apiSettings.ts`, render an input in the UI tile. |
| Add a new operator-tunable threshold (no secret) | Add the column with a sane default, extend the section's Pydantic model, copy in `apply_update_request`, expose in TS, render the input. **And** wire it into `apply_runtime_settings_overrides` if it shadows a `config.py` value. |
| Trigger something after a save | Add a `needs_<area>_reinit` flag to `apply_update_request`'s return dict, then act on it in `routes_settings.update_settings` outside the DB session. Don't fire side effects from inside the helper. |
| Add an export category | Extend `SettingsTransferCategory` enum, add `_apply_<category>_import` and the export branch in the exporter. |
| Add a "Test connection" button | Add `POST /api/settings/test/<service>`, mirror the result shape `{status: 'success'|'error'|'warning', message: str, ...}`, and call from the UI via `useMutation`. |

## Known footguns

- **Don't read settings in a hot loop.** Each call hits the DB.
  Cache locally and re-read after `needs_*_reinit` fires (the
  `LLMManager` does this; new services should too).
- **Don't reuse one Pydantic field for two purposes.** The `if X is
  not None:` pattern means there's no way to express "explicitly
  set to null" vs "omit from update" if both should be valid; pick
  one semantic and document it.
- **The encryption fallback is silent.** If `APP_SECRETS_KEY` isn't
  set, secrets are written plaintext and only a warning is logged.
  Any deployment script must verify the env var is present.
- **The `"********"` mask is a UI convenience, not a data contract.**
  Don't store it as a value, don't compare against it in business
  logic, only at the input boundary in dedicated PUT endpoints.
- **Field renames are hard.** Because the column name is the contract
  (`<service>_<field>` shows up in DB, Pydantic, TS, and UI), a
  rename is a 6-file diff plus an Alembic migration with `op.alter_column`.
  Prefer additions and mark old fields deprecated rather than rename.

Last verified: 2026-05-09 (Plan 0017: real-diff against `backend/models/database.py` AppSettings — line ref corrected from 1190+ → 1248+; `backend/api/settings_helpers.py:apply_update_request` corrected from 679+ → 687+; `backend/config.py` overrides — corrected 753+ → 968+ and removed the `_load_async_settings()` reference (function does not exist in the current code; the actual entry points are `apply_runtime_settings_overrides`, `apply_events_settings`, `apply_search_filters`). Encryption module, sandbox-account model, hot-reload semantics confirmed unchanged.)
