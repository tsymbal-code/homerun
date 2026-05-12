# Спільні параметри ботів та діагностика

> **Аудиторія**: оператор / агент, який налаштовує будь-якого
> trader-бота в UI або через API. Цей файл описує **те, що
> однакове для всіх стратегій**: контракт `traders`-таблиці,
> risk-limits, decision-gates, UI-вкладки `Tune` / `Risk` /
> `Decisions` / `Performance`, і SQL-рецепти, як знайти, чому
> бот не відкриває позицію. Кожен `docs/strategies/<slug>.md`
> робить посилання сюди замість дублювання.
>
> Last verified: 2026-05-10

## Як живе бот у БД

Бот = один рядок у таблиці `traders` (`backend/models/database.py`,
__tablename__ `traders`, ~line 3784). Окремої таблиці
`bot_strategy_params` нема — стратегічні параметри лежать у JSON
полях `traders.source_configs_json` і `traders.risk_limits_json`.

| Колонка | Що означає |
|---|---|
| `id` | UUID-PK без дефісів. Світиться у логах і у `trader_id` query-параметрах. |
| `name` | Те, що видно в UI ("Sandbox - Traders Copy Trade"). |
| `mode` | `shadow` (симуляція через Cox-PH) або `live` (реальні ордери на CLOB). Перемикається в UI або через `PUT /api/traders/{id}`. |
| `latency_class` | `normal` (раз на `interval_seconds`, default 5–60 с) або `fast` (sub-секундний цикл; інший консьюмер `fast_trader_runtime`). |
| `is_enabled` | `false` → воркер навіть не починає цикл. |
| `is_paused` | `true` → цикли пропускаються. UI це малює іконкою паузи. |
| `block_new_orders` | `true` → стратегія може викликати `evaluate()`, але `decision='selected'` примусово конвертується в `skipped` перед сабмітом. |
| `interval_seconds` | Період циклу normal-tier. Default 5 с для traders-source. |
| `last_run_at` | Heartbeat — оновлюється кожен цикл. Якщо застиг > 2× `interval_seconds` → воркер не дишить. |
| `source_configs_json` | Список з конфігами джерел: `[{source_key, strategy_type, strategy_params: {...}, enabled}]`. Один бот може мати кілька стратегій (рідко). |
| `risk_limits_json` | JSON з `max_*` лімітами (див. нижче). |
| `metadata_json` | Дрібні позаконтрактні поля: cadence_profile, schedule, tags, last_loss_streak_reset. |

Рядок із live-стану дивимось так:

```bash
ssh <HOMERUN_HOST> 'cd /home/polyhome/homerun && docker compose exec -T \
  postgres psql -U homerun -d homerun -x -c \
  "SELECT id, name, mode, latency_class, is_enabled, is_paused, \
   block_new_orders, interval_seconds, last_run_at \
   FROM traders WHERE name ILIKE '\''%sandbox%'\''"'
```

Усі зміни конфігу логуються в `trader_config_revisions`
(`trader_before_json`, `trader_after_json`, `actor`, `created_at`).
Це канонічний "хто і коли поміняв" — не git, бо конфіги в БД, не у
файлах.

## `risk_limits_json` — повна схема

Дефолти живуть у `backend/services/strategy_sdk.py`
(`TRADER_RISK_LIMITS_DEFAULTS` на ~line 60). Всі ліміти opt-in
поверх стратегічних `evaluate()`-гейтів — тобто стратегія може
сказати `selected`, а ризик-менеджер потім скасувати на
`Risk blocked: ...`.

| Ключ | Default | Що блокує |
|---|---|---|
| `max_gross_exposure_usd` | `2000.0` | Сума `notional_usd` усіх відкритих позицій + pending-ордерів. Глобальний cap. |
| `max_position_notional_usd` | `5.0` | Розмір однієї позиції. Мікро-default; для копі-трейдингу зазвичай піднімають до $25–$100. |
| `max_trade_notional_usd` | `5.0` | Розмір одного ордера на сабмішені. Часто == `max_position_notional_usd`. |
| `max_per_market_exposure_usd` | derive (10% від gross) | Cap на ринок. |
| `max_orders_per_cycle` | `6` | Скільки ордерів за один цикл воркера. |
| `max_open_orders` | `20` | Скільки відкритих ордерів одночасно. **Може бути вторинно затиснений `live_risk_clamps.max_open_orders_cap`** — див. нижче. |
| `max_open_positions` | `12` | Те саме для позицій. |
| `max_daily_loss_usd` | `300.0` | Ліміт денного збитку (realized + unrealized). При перевищенні всі рішення блокуються до півночі UTC. |
| `max_consecutive_losses` | `4` | Серія програшів поспіль перед `halt_on_consecutive_losses`. |
| `halt_on_consecutive_losses` | `true` | Якщо `false`, попередній лічильник тільки інформує, не блокує. |
| `cooldown_after_loss_seconds` | `0` | Wait після збиткової угоди. |
| `max_market_data_age_ms` | global default ~30000 | Якщо ws-quote старіша за це → `Market data freshness gate (age>Xms)` блокує. |
| `allow_averaging` | `false` | Якщо `false`, на ринок з вже відкритою позицією **stacking guard** блокує другу. |
| `portfolio.enabled` | `false` | Вмикає portfolio-aware sizing (нестандартно для copy-trade). |

> **Важливо.** UI вкладка **Risk** малює саме ці поля; всі вони
> зберігаються в `traders.risk_limits_json`. Дефолти можна
> побачити в схемі віджета: `riskFormSchema.param_fields` у
> `frontend/src/components/RiskLimitsView.tsx` будується з
> `getRiskLimitsSchema()` бекенд-роуту.

### Live-risk clamps (одержують пріоритет над `risk_limits`)

`app_settings.settings_json.global_runtime.live_risk_clamps` —
**глобальні** обмежники, які запускаються в
`_apply_live_risk_clamps()`
(`backend/workers/trader_orchestrator_worker.py:3528-3607`).
Логіка проста: для кожного `*_cap` поля `effective = min(configured, cap)`.

| Ключ clamp | Що затискає |
|---|---|
| `max_open_orders_cap` | `risk_limits.max_open_orders` |
| `max_open_positions_cap` | `risk_limits.max_open_positions` |
| `max_consecutive_losses_cap` | `risk_limits.max_consecutive_losses` |
| `max_trade_notional_usd_cap` | `risk_limits.max_trade_notional_usd` |
| `max_per_market_exposure_usd_cap` | `risk_limits.max_per_market_exposure_usd` |
| `max_orders_per_cycle_cap` | `risk_limits.max_orders_per_cycle` |
| `min_cooldown_seconds` | мінімум для `cooldown_after_loss_seconds` |
| `enforce_allow_averaging_off` | `true` → `allow_averaging` примусово `false` (тільки в `live`-mode) |
| `enforce_halt_on_consecutive_losses` | `true` → `halt_on_consecutive_losses` примусово `true` (тільки в `live`-mode) |

> **Реальний кейс**, помічений на проді (2026-05-10): бот з
> `max_open_orders=300` у `risk_limits_json` фактично впирався
> у 100, бо `live_risk_clamps.max_open_orders_cap=100`. У
> `risk_snapshot_json` decision-checks: `max=100`. Якщо ваш бот
> сидить на стелі `max=N`, а в `risk_limits` стоїть більше —
> подивіться `app_settings.settings_json.global_runtime.live_risk_clamps`.

```bash
ssh <HOMERUN_HOST> 'cd /home/polyhome/homerun && docker compose exec -T \
  postgres psql -U homerun -d homerun -c \
  "SELECT settings_json->'\''global_runtime'\''->'\''live_risk_clamps'\'' \
   FROM app_settings WHERE id=1"'
```

### Legacy implicit clamps

Якщо бот **взагалі не має** `live_risk_clamps`-поля і його
`risk_limits` точно збігаються з історичним пресетом
(`backend/services/trader_orchestrator_state.py:154`,
`LEGACY_IMPLICIT_LIVE_RISK_CLAMPS`), то застосовуються
дефолти-«підкови»:

```
max_open_orders_cap=6, max_open_positions_cap=8,
max_consecutive_losses_cap=3, max_trade_notional_usd_cap=10,
max_per_market_exposure_usd_cap=10, max_orders_per_cycle_cap=3
```

Якщо хоч одне поле відрізняється — pre-set вважається не legacy і
clamps не застосовуються.

## Decision-gates: що може заблокувати рішення

Кожен сигнал у trader-бота проходить дві стадії: спочатку
**стратегія** (`evaluate()`-метод, перевіряє свої власні чеки), потім
**платформенні гейти**, які живуть у
`backend/services/trader_orchestrator/decision_gates.py`
функція `apply_platform_decision_gates`. Будь-який провал →
`decision='blocked'` чи `'skipped'` із вказаною причиною. Усі
чеки логуються у `trader_decisions.checks_summary_json` і
`trader_decision_checks` (per-row).

Повний інвентар платформенних гейтів (порядок виконання згори
вниз; усі — `decision_gates.py`):

| Гейт | Параметр | Default | Тільки live? | Що означає |
|---|---|---|---|---|
| `strategy_demoted` | `demoted_strategy_types` | none | ні | Health-guardrail: якщо стратегія в `demoted_*` → блок. |
| `signal_staleness` | `max_signal_age_seconds` | none (off) | ні | Якщо встановлено, signals старіші — `blocked`. |
| `trading_schedule` | `trading_schedule_utc` | disabled | ні | UTC-розклад: `enabled, days[], start_time, end_time, start_date, end_date`. |
| `size_cap` | `max_trade_notional_usd` | derived (~10% від gross) | ні | Стратегія дала `size_usd` > cap → `blocked`. |
| `execution_plan_token_conflict` | n/a | — | ні | Структурна валідація плану виконання. |
| `strict_ws_pricing` | `require_strict_ws_pricing`, `strict_ws_price_sources` | ws_strict only | ні (live-gated) | Жорстка ws-цінова політика. |
| `live_market_revalidation` | `require_live_market_revalidation`, `require_live_revalidation_for_sources` | `["crypto"]` | ні (live-gated) | Перед live-сабмітом перевалідовувати ws-ціну. |
| `market_data_freshness` | `max_market_data_age_ms` | derived per timeframe (5m default) | ні (live-gated) | Якщо ws-quote стара — `blocked: Market data freshness gate (age>Xms)`. |
| `directional_min_timeframe` | `enforce_live_directional_timeframe`, `directional_min_timeframe` | true; `5m` | ні | Захист від суб-таймфреймних directional-сигналів. |
| `max_risk_score` | `max_risk_score` | none (off) | ні | Стратегія повертає `risk_score` (0..1); якщо вище — блок. |
| `stop_loss_settlement_upside` | `enforce_stop_loss_upside_guard`, `max_stop_loss_to_upside_ratio` | true; `1.0` | ні (live-gated) | SL не повинен бути більший за апсайд. |
| `min_exit_notional` | `enforce_min_exit_notional`, `min_order_size_usd` | true; `1.0` | ні | Замалі екзит-ордери блокуються. |
| `portfolio` | callback | none | ні | Portfolio allocator (опціонально). |
| **`risk`** | усі `risk_limits_json` поля | див. таблицю вище | ні | Сюди впадає `trader_open_orders`, `trader_daily_loss`, `trader_loss_streak` тощо. |
| `pending_live_exit_guard` | `max_pending_exits` | `0` (off) | live | Захист від накопичення pending-екзитів. |
| `pending_live_exit_identity_guard` | `identity_guard_enabled` | true | live | Зв'язує pending-екзит з конкретною позицією. |
| **`stacking_guard`** | `allow_averaging` | `false` | в live завжди, в shadow при `allow_averaging=false` | Один ринок — одна активна позиція. Це найчастіший skip-reason для активних копі-трейд ботів. |

### Stacking guard детально

`decision_gates.py:2246-2306`. Перевіряє: чи є вже відкрита позиція
(або `pending` ордер) на цей `market_id` для цього trader_id. Якщо
так і `allow_averaging=false` — `Stacking guard: market already
occupied (pre-gate)`. У live-mode гейт **завжди** ввімкнений (через
`enforce_allow_averaging_off`-clamp), у shadow можна вимкнути,
поставивши `allow_averaging=true`.

Це per-`trader_id`, не глобально: різні боти можуть тримати позиції
на одному ринку.

### Risk gate: формат причини

`risk` — це насправді **група чеків**, які стрімлять усі
`risk_limits` поля. Провал першого з них перетворюється у
`Risk blocked: <field> next≈<X> max=<Y>`. Найчастіші:

- `trader_open_orders` — досягнуто `max_open_orders` (з врахуванням clamp)
- `trader_open_positions` — досягнуто `max_open_positions`
- `trader_market_exposure` — `max_per_market_exposure_usd` на цьому ринку
- `trader_daily_loss` — `max_daily_loss_usd`
- `trader_loss_streak` — `max_consecutive_losses` поспіль
- `global_gross_exposure` — global cap для всіх traders

## UI-вкладки: де що крутиться

### Tune

`frontend/src/components/AutoresearchView.tsx` (forceArMode="params"),
викликаний з `TradingPanel.tsx:11590-11617`. Рендерить
`StrategyConfigForm` за схемою, яку повертає
`<strategy>_config_schema()` (для traders-copy-trade —
`TRADERS_COPY_TRADE_CONFIG_SCHEMA` у самому файлі стратегії). Кожне
поле має `key`, `label`, `type`, опціонально `min`/`max`/`description`.

Save-action: `saveTuneParametersMutation.mutate()` →
`PUT /api/traders/{traderId}` з payload
`{dynamic_strategy_params: {...}}`. UI показує "UNSAVED" badge до
збереження; "Revert" повертає до останнього персистнутого стану.

### Risk

`frontend/src/components/RiskLimitsView.tsx`. Рендерить
`StrategyConfigForm` за схемою `riskFormSchema.param_fields`. Save
через `saveRiskLimitsMutation` → той самий
`PUT /api/traders/{traderId}` з полем `risk_limits`.

### Decisions

`TradingPanel.tsx:11633-11804`. Це найкращий діагностичний
інструмент для "чому не відкрилась позиція".

Структура:

- Ліва панель — список останніх рішень з фільтрами **Selected**,
  **Blocked**, **Skipped** (з лічильниками). Поле пошуку фільтрує
  за market label / strategy_key / reason.
- Права панель — деталі вибраного рішення: market question, source,
  strategy, direction, market_price, model_probability, edge%,
  confidence%, score, **reason**, та (найголовніше) розгорнутий
  список **Checks**: кожен чек має ✓/✗ і опис.

API:

- `GET /api/traders/decisions/all?trader_ids=<id>&decision=<state>&limit=...`
  — повертає список з `failed_checks[]`.
- `GET /api/traders/decisions/{decision_id}` — повний об'єкт із
  `checks[]` та `risk_checks[]`.

**Як швидко зрозуміти, що блокує бота:**

1. Відкрити Decisions → відфільтрувати **Blocked**.
2. Подивитись на `reason` у кількох останніх — якщо одне й те саме
   "Risk blocked: trader_open_orders ..." — впираємось у
   `max_open_orders` (з врахуванням clamp).
3. Якщо багато "Stacking guard ..." — бот вже сидить на цих ринках,
   або потрібен `allow_averaging=true` (в shadow).
4. Якщо `Market data freshness gate ...` — ws-feed застрягає, або
   `max_market_data_age_ms` занадто маленький.
5. Якщо **Skipped** з причиною типу `copy_trade_gate_failed:<keys>` —
   це сама стратегія відсіяла, читайте відповідний `<slug>.md`,
   секцію "Логіка детекції".

### Performance

`TradingPanel.tsx:11807-12520`. Не діагностує блокери напряму, але
показує:

- **Performance** субвкладка: realized PnL, ROI%, win-rate, profit
  factor, peak (best streak), max drawdown, кумулятивний PnL-чарт.
- **Latency** субвкладка: середні значення стадій
  armed→ws_release, context_ready→decision, decision→submit,
  submit→ack. Якщо одна стадія різко росте — там проблема.
- **Configuration** субвкладка: snapshot конфігу, з яким реально
  створювались ордери (тобто historical view, на випадок коли
  бот працював з минулим конфігом).

Дані будуються на клієнті з `getTraderOrdersSummary()`
(`/api/traders/orders/summary`) і деталей рішень — окремого
"performance"-роуту нема.

## SQL-рецепти для діагностики

> Завжди через SSH, бо постгрес тільки на віддаленому хості
> (`<HOMERUN_HOST>` — це `polyhome-prod` для `main` і `polyhome-1`
> для `dev`, див.
> [`docs/plans/architecture/deploy-targets.md`](../plans/architecture/deploy-targets.md)).

```bash
# Розподіл рішень за останні 24 год для конкретного бота
ssh <HOMERUN_HOST> 'cd /home/polyhome/homerun && docker compose exec -T \
  postgres psql -U homerun -d homerun -c \
  "SELECT decision, count(*) FROM trader_decisions \
   WHERE trader_id = '\''<TRADER_ID>'\'' \
     AND created_at > now() - interval '\''24 hours'\'' \
   GROUP BY decision ORDER BY count(*) DESC"'

# Топ причин blocked / skipped
ssh <HOMERUN_HOST> 'cd /home/polyhome/homerun && docker compose exec -T \
  postgres psql -U homerun -d homerun -c \
  "SELECT reason, count(*) FROM trader_decisions \
   WHERE trader_id='\''<TRADER_ID>'\'' \
     AND decision IN ('\''blocked'\'',  '\''skipped'\'') \
     AND created_at > now() - interval '\''1 hour'\'' \
   GROUP BY reason ORDER BY count(*) DESC LIMIT 20"'

# Поточні відкриті позиції + ордери
ssh <HOMERUN_HOST> 'cd /home/polyhome/homerun && docker compose exec -T \
  postgres psql -U homerun -d homerun -c \
  "SELECT (SELECT count(*) FROM trader_positions WHERE trader_id='\''<TID>'\'' AND status='\''open'\'') AS open_positions, \
          (SELECT count(*) FROM trader_orders    WHERE trader_id='\''<TID>'\'' AND status='\''open'\'') AS open_orders"'

# Детальний risk_snapshot з останнього blocked-рішення
ssh <HOMERUN_HOST> 'cd /home/polyhome/homerun && docker compose exec -T \
  postgres psql -U homerun -d homerun -x -c \
  "SELECT created_at, reason, risk_snapshot_json \
   FROM trader_decisions \
   WHERE trader_id='\''<TID>'\'' AND decision='\''blocked'\'' \
   ORDER BY created_at DESC LIMIT 1"'

# Останні revisions конфігу
ssh <HOMERUN_HOST> 'cd /home/polyhome/homerun && docker compose exec -T \
  postgres psql -U homerun -d homerun -c \
  "SELECT id, actor, created_at \
   FROM trader_config_revisions \
   WHERE trader_id='\''<TID>'\'' \
   ORDER BY created_at DESC LIMIT 5"'
```

Або через UI: Decisions → клік на рішення → секція **Risk Checks** і
**Checks** показує те саме, що `risk_snapshot_json`.

## Як стратегії емітять `positions_to_take` і де формується `direction`

Це не очевидно з першого погляду в код, але **майже всі стратегії
взагалі не мають поля `direction` в `positions_to_take`**. Поле
`direction` (`buy_yes`/`buy_no`/...) — це downstream-конструкція, а
не stratregy-emit.

### Дві форми emit-у

**Форма A — без `direction`** (basic, negrisk, market_making, news_edge,
weather_distribution, всі crypto-стратегії):

```python
# basic.py:298-315
{"action": "BUY", "outcome": "YES", "token_id": market.clob_token_ids[0], ...}
{"action": "BUY", "outcome": "NO",  "token_id": market.clob_token_ids[1], ...}

# negrisk.py:127-145 (_build_position helper)
{"action": "BUY", "outcome": outcome, "token_id": token_id}   # outcome ∈ {"YES","NO"}

# market_making.py:559-572
{"action": "LIMIT_BUY",  "outcome": "YES", "token_id": yes_token_id, ...}
{"action": "LIMIT_SELL", "outcome": "YES", "token_id": yes_token_id, ...}
```

Всі вони покладаються на нижчий шар, щоб побудувати канонічний
`direction`-стрінг.

**Форма B — з explicit `direction`** (тільки `traders_copy_trade`,
плюс legacy passthrough у `news_momentum_breakout`/`flash_crash_reversion`):

```python
# traders_copy_trade.py:499-503
"direction": (
    "buy_yes" if outcome == "YES"
    else "buy_no" if outcome == "NO"
    else "buy"     # ← джерело багу: для multi-outcome
),
```

### Резолвер: `_resolve_leg_direction`

Канонічна побудова `direction` живе в
[`backend/services/trader_orchestrator/session_engine.py:128`](../../backend/services/trader_orchestrator/session_engine.py:128):

```python
def _resolve_leg_direction(leg, fallback_direction):
    explicit = leg.get("direction") or ""
    if explicit:
        return explicit                      # Форма B — explicit перемагає
    side = leg.get("side")                   # "buy"/"sell" (з ExecutionLeg.side)
    outcome = leg.get("outcome")             # "yes"/"no"
    if side in {"buy","sell"} and outcome in {"yes","no"}:
        return f"{side}_{outcome}"           # → "buy_yes" / "buy_no"
    if side in {"buy","sell"}:
        return side                          # fallback — bare "buy"/"sell"
    return fallback_direction
```

Тобто:
- Форма A → канонічний `buy_yes`/`buy_no` через fallback (працює, бо
  outcome завжди `"yes"`/`"no"`).
- Форма B → беремо `direction` як є; якщо стратегія сама написала
  `"buy"` — ось він і поїде в DB.

`action="BUY"` → `side="buy"` мапінг робить `normalize_position_side`
у [`base.py:1616-1617`](../../backend/services/strategies/base.py:1616).

### Архітектурне припущення: всі ринки бінарні

Полimarket-ринки на CLOB-рівні **завжди binary** (два clob_token_ids:
[YES, NO]). Multi-outcome event = N окремих binary-ринків:

- "Хто виграє Champions League?" — це event,
- "Чи виграє Arsenal CL?" — окремий market_id з YES/NO,
- "Чи виграє Real Madrid CL?" — інший market_id з YES/NO,
- ...

Тому basic/negrisk природно бінарні: вони сканують catalog ринків
(кожен рядок — окрема binary-market) і завжди емітять
`outcome="YES"|"NO"`. NegRisk multi-leg бандл — це купа окремих
"BUY YES" на N бінарних ринків, не один multi-outcome ордер.

**Label vocabulary не має значення.** Gamma може повернути
`outcomes=["Yes","No"]`, `["Up","Down"]` (crypto), `["Arsenal","Field"]`
(candidate/field), `["Trump","Other"]` тощо. `traders_copy_trade`
signal-service канонікалізує **будь-який 2-token ринок** до
канонічних `Yes`/`No` за позицією token-у в `tokens[]` (idx 0 →
"Yes", idx 1 → "No"). Це plan 0023 розширення Plan 0018 normalisation,
яке раніше працювало тільки коли labels містили літеральне Yes/No.
Після канонікалізації стратегія завжди емітить `direction='buy_yes'`
або `'buy_no'` через існуючу гілку, і downstream шари (simulator,
lifecycle, fast-submit) працюють без defensive widening через
`token_id`.

**Виняток** — Polymarket-ринки виду "single market з N outcomes"
(не categorical-event): рідкі outright-формати (UFC fighter outright,
LoL series), де один `market_id` має `outcomes=["Fighter A","Fighter B","Fighter C"]`.
Тут немає канонічного "YES"/"NO". Це справжня multi-outcome
структура, і **жоден з downstream шарів** її не підтримує:

- `simulation._direction_to_position_side` (simulation.py:55-62) — кидає `ValueError`;
- `position_lifecycle._direction_outcome_index` (position_lifecycle.py:879) — повертає `None`;
- `polymarket.get_market_by_condition_id` (polymarket.py:934) — за замовчуванням приховує resolved-ринки.

`traders_copy_trade` — єдина стратегія, яка може зустріти такий
ринок (бо копіює leader без скан-фільтра), і єдина, що страждає від
цього обмеження.

### Що це означає для розробника нової стратегії

- **За замовчуванням НЕ ставте `direction` в `positions_to_take`.**
  Дайте `_resolve_leg_direction` побудувати його з `(side, outcome)`.
- Якщо ваша стратегія може зустріти не-бінарний ринок (рідко) —
  додавайте `selected_token_id` в payload, щоб downstream міг
  резолвити, але **не вигадуйте власний direction-стрінг**: це
  обходить fallback і ламає reconciliation.
- Якщо потрібно явно direction — використовуйте тільки
  `buy_yes`/`buy_no` (єдині, які працюють всюди в системі).

## Куди дивитись далі

- [`docs/plans/architecture/trader-pipeline.md`](../plans/architecture/trader-pipeline.md)
  — generic flow signal → order, fast vs normal tier.
- [`docs/plans/architecture/copy-trade-pipeline.md`](../plans/architecture/copy-trade-pipeline.md)
  — конкретно для `source='traders'`, історія plan 0008/0009.
- [`docs/plans/architecture/worker-trading.md`](../plans/architecture/worker-trading.md)
  — `worker-trading` процесна модель.
- [`docs/plans/architecture/execution-defense.md`](../plans/architecture/execution-defense.md)
  — захисні шари submit-side (price chaser, circuit breakers, monitors).
- `backend/services/trader_orchestrator/decision_gates.py` — канонічне
  джерело гейтів.
- `backend/services/trader_orchestrator/risk_manager.py` — канонічне
  джерело risk-чеків.

## Knob interaction matrix — CRITICAL tier

> **Призначення.** Перш ніж змінювати будь-який knob із цього
> розділу через UI / API / SQL — **прочитай відповідний entry**.
> Кожен запис показує, **які саме gate-и читають це поле**, з
> точною формулою та `file:line`, плюс **похідні метрики**, що
> самі залежать від цього knob-а й тригерять інші gate-и
> (compound effects). Без цього довідника аналіз залишається
> поверхневим і регулярно дає dimensionally-wrong результат
> (типові симптоми: «затиснули loss-cap → бот замовк цілком»;
> «ввімкнули halt → CB одразу зливає позиції»).
>
> Phase 2 (template `runtime-tweaks.md`-entry, що вимагає
> walkthrough на CRITICAL зміну) і Phase 3 (memory-rule, що
> змушує консультуватися саме з цим розділом перед PUT-ом) —
> окремі плани. Цей файл — Phase 1, **знання**.

**Tier-класифікація:**

- **CRITICAL** — state-flipping, wide blast radius, важко
  відкотити (loss caps, drawdown, halt-flags, position-size
  caps). **Зміна вимагає walkthrough.**
- **HIGH** — змінює поведінку (latency, decision filters), але
  не state-flipping (`max_market_data_age_ms`,
  `max_entry_drift_pct`, `slippage_bps`).
- **MEDIUM** — strategy-params що тонко тюнять детекцію
  (`min_probability`, `min_upside_percent`).
- **LOW** — UI flags, polling intervals, не на critical path.

Цей розділ покриває **21 CRITICAL knob**. HIGH/MEDIUM
розкидані по `docs/strategies/<slug>.md`-нотатках і у
[`trader-pipeline.md`](../plans/architecture/trader-pipeline.md).

> **Drift warning.** Усі формули нижче зафіксовані і **per-entry
> аудит-маркером підтверджені** станом на 2026-05-10 (Plan 0034 —
> 14 entries `clean`, 7 `drift corrected`). Якщо ви рефакторите
> будь-який цитований `file:line` — оновіть і цей розділ у тому
> ж commit-і та пересуньте відповідний `<!-- audited … -->`
> маркер. Без drift-тестів (Phase 4 candidate) матриця може
> стати misleading мовчки.

---

### CRITICAL — `max_position_notional_usd`

<!-- audited 2026-05-10: clean -->

**Default:** 5.0 USD ([`strategy_sdk.py:395`](../../backend/services/strategy_sdk.py))

#### Direct consumers

| Gate | Формула | File:line | reason-string |
|---|---|---|---|
| `trader_market_exposure` | `next_market = current_market_value + new_size`; `next_market <= min(global_per_market, max_position_notional_usd) → pass` | [`risk_manager.py:205-212`](../../backend/services/trader_orchestrator/risk_manager.py) | `Risk blocked: trader_market_exposure (next=X.XX max=Y.YY)` |

#### Indirect consumers

Жодних похідних метрик не читає цей knob.

#### Compound with

- **`max_gross_exposure_usd`:** ефективна максимальна
  кількість одночасно відкритих позицій ≈
  `floor(max_gross_exposure_usd / max_position_notional_usd)`
  на одному ринку. З default-ами 5/2000 = 400 на ринок —
  тобто `max_gross_exposure_usd` зазвичай binding-у, а не
  position-cap. Зменшення position-cap зменшує **розмір**
  позиції, не змінює загальний risk-budget.
- **`halt_on_consecutive_losses` + `max_consecutive_losses`:**
  менший `max_position_notional_usd` = бот більше робить
  спроб у тому самому risk-budget = **streak-counter може
  накручуватися швидше при тій же ринковій ситуації**.

---

### CRITICAL — `max_trade_notional_usd`

<!-- audited 2026-05-10: clean -->

**Default:** 5.0 USD ([`strategy_sdk.py:397`](../../backend/services/strategy_sdk.py))

#### Direct consumers

| Gate | Формула | File:line | reason-string |
|---|---|---|---|
| `trader_trade_notional` | `max(0.0, size_usd) <= max(1.0, max_trade_notional_usd) → pass` | [`risk_manager.py:156`](../../backend/services/trader_orchestrator/risk_manager.py) | `Risk blocked: trader_trade_notional (size=X.XX max=Y.YY)` |

#### Indirect consumers

Жодних.

#### Compound with

- **`max_position_notional_usd`:** trade-notional обмежує
  одну операцію, position-notional — сумарну позицію на
  ринку. Якщо `max_trade_notional_usd > max_position_notional_usd`,
  effective limit = position-cap; trade-cap не binding.

---

### CRITICAL — `max_gross_exposure_usd`

<!-- audited 2026-05-10: clean -->

**Default:** 2000.0 USD ([`strategy_sdk.py:396`](../../backend/services/strategy_sdk.py))

#### Direct consumers

| Gate | Формула | File:line | reason-string |
|---|---|---|---|
| `global_gross_exposure` | `next_gross = max(0.0, gross_exposure_usd) + max(0.0, size_usd)`; `next_gross <= max_gross_exposure_usd → pass` | [`risk_manager.py:167-175`](../../backend/services/trader_orchestrator/risk_manager.py) | `Risk blocked: global_gross_exposure (next=X.XX max=Y.YY)` |
| `size_cap` (sizing) | `notional_default = max(50.0, max_gross_exposure_usd × 0.10)` | [`decision_gates.py:763-785`](../../backend/services/trader_orchestrator/decision_gates.py) | `Capped to max_trade_notional_usd=X.XX` |

#### Indirect consumers

Знос на дефолтний размер позиції через `decision_gates`:
коли strategy-параметри не виставили явний `notional_usd`,
`size_cap` бере 10% від gross як фолбек. Зміна
`max_gross_exposure_usd` опосередковано **зміщує дефолт
розміру**, навіть якщо `max_position_notional_usd` сам по
собі не змінився.

#### Compound with

- **`max_position_notional_usd`:** добуток обох — стеля
  одночасних відкритих позицій (див. вище).
- **`max_open_orders` / `max_open_positions`:** якщо count-cap
  binding, gross-cap може бути не binding-ом і навпаки.
  Перевір обидва перед зміною.

---

### CRITICAL — `min_exit_notional` (computed gate, not a knob)

<!-- audited 2026-05-10: clean -->

**Default:** обчислюється per-strategy ([`decision_gates.py:1810-1833`](../../backend/services/trader_orchestrator/decision_gates.py))
**Не лежить у `TRADER_RISK_DEFAULTS`.** Замість цього є
**boolean knob `enforce_min_exit_notional`** у
`source_configs[0].strategy_params`, default `True`
([`decision_gates.py:1666`](../../backend/services/trader_orchestrator/decision_gates.py)).

#### Direct consumers

| Gate | Формула | File:line | reason-string |
|---|---|---|---|
| `min_exit_notional` | `required_size_usd` обчислюється з `entry_price`, `stop_loss_pct`, `min_order_size_usd`; `size_usd + 1e-9 >= required_size_usd → pass` | [`decision_gates.py:1810-1833`](../../backend/services/trader_orchestrator/decision_gates.py) | `Min-exit-notional guard blocked: required size >= X.XX for min exit $Y.YY` |

#### Indirect consumers

Жодних.

#### Compound with

- **`max_position_notional_usd`:** якщо **ваш position-cap**
  менший за `required_size_usd`, бот блокується **на цьому**
  gate-і — не на position-cap. Симптом: low-frequency strategy
  з $5 cap-ом постійно бачить «Min-exit-notional guard
  blocked: required size >= 2.00».
- **`enforce_min_exit_notional=false`** у strategy_params
  обходить цей guard повністю — використовуй для дрібних
  positions де можна допустити часткові exits.

---

### CRITICAL — `max_open_orders`

<!-- audited 2026-05-10: clean -->

**Default:** 20 ([`strategy_sdk.py:392`](../../backend/services/strategy_sdk.py))

#### Direct consumers

| Gate | Формула | File:line | reason-string |
|---|---|---|---|
| `trader_open_orders` | `(trader_open_orders + 1) <= max_open_orders → pass` | [`risk_manager.py:182-193`](../../backend/services/trader_orchestrator/risk_manager.py) | `Risk blocked: trader_open_orders (next=X max=Y)` |

#### Indirect consumers

Жодних.

#### Compound with

- **`max_open_positions`:** обидва count-cap-и читаються
  паралельно. Перший, що binding, блокує. Якщо тільки
  `max_open_orders` встановлено — `max_open_positions` defaults
  до того ж значення.
- **«Stuck positions» pattern (2026-05-08):** коли positions
  не закриваються (немає resolution / strategy не emit-ить
  exit signal), open count росте до cap, потім **усі нові
  decisions блокуються `trader_open_orders`** і бот виглядає
  «замороженим». Симптом ≈ 90% decisions blocked + 0 нових
  orders.

---

### CRITICAL — `max_open_positions`

<!-- audited 2026-05-10: clean -->

**Default:** 12 ([`strategy_sdk.py:393`](../../backend/services/strategy_sdk.py))

#### Direct consumers

| Gate | Формула | File:line | reason-string |
|---|---|---|---|
| `trader_open_positions` | `(trader_open_positions + 1) <= max_open_positions → pass` | [`risk_manager.py:178-201`](../../backend/services/trader_orchestrator/risk_manager.py) | `Risk blocked: trader_open_positions (next=X max=Y)` |

#### Indirect consumers

Жодних.

#### Compound with

- **`max_open_orders`:** див. компаньйон-запис вище.
- **`max_position_notional_usd` × кількість позицій × ринкові
  умови:** якщо стратегія схильна відкривати **множинні
  позиції на одному ринку** (різні directions / lots),
  position-count росте швидше за market-count.

---

### CRITICAL — `max_daily_loss_usd`

<!-- audited 2026-05-10: clean -->

**Default:** 300.0 USD ([`strategy_sdk.py:398`](../../backend/services/strategy_sdk.py))

#### Direct consumers

| Gate | Формула | File:line | reason-string |
|---|---|---|---|
| `trader_daily_loss` | `trader_daily_realized_pnl > -max_daily_loss_usd → pass` | [`risk_manager.py:61-84`](../../backend/services/trader_orchestrator/risk_manager.py) | `Risk blocked: trader_daily_loss (realized=X.XX floor=-Y.YY)` |
| `trader_daily_total_loss` | `trader_total_daily_pnl = realized + unrealized`; `total > -max_daily_loss_usd → pass` | [`risk_manager.py:104-117`](../../backend/services/trader_orchestrator/risk_manager.py) | `Risk blocked: trader_daily_total_loss (realized=X unrealized=Y total=Z floor=-W)` |

#### Indirect consumers

| Derived metric | Формула | Where computed | Where read | Gate |
|---|---|---|---|---|
| `trader_drawdown_pct` | `(-trader_total_daily_pnl / max_daily_loss_usd) × 100` | [`trader_orchestrator_worker.py:6416-6432`](../../backend/workers/trader_orchestrator_worker.py) | [`traders_copy_trade.py:599, 797`](../../backend/services/strategies/traders_copy_trade.py) | `copy_drawdown` |

`trader_drawdown_pct` пропонується через `copy_risk_context`
у `traders_copy_trade` strategy. Gate `copy_drawdown` блокує
коли `trader_drawdown_pct > max_copy_drawdown_pct`
(strategy_param, default 100.0 — фактично disabled).

#### Compound with

- **`max_copy_drawdown_pct` (strategy_param, не risk_limit):**
  знижуючи `max_daily_loss_usd` у N разів, ви робите
  `trader_drawdown_pct` у **N разів чутливішим** (бо чисельник
  той самий, знаменник зменшився). `copy_drawdown` gate
  тригериться у N разів швидше при тих самих $-збитках.
  Класична пастка: оператор тисне «daily loss = 50 замість
  300», і Copy Trade бот замовкає цілком при ±$2 daily loss.
- **`circuit_breaker_drawdown_pct`:** **НЕЗАЛЕЖНА база** —
  цей CB-knob був би відсотком від `starting_capital`, не від
  `max_daily_loss_usd`. **АЛЕ зараз dead code** — див. запис
  нижче.

---

### CRITICAL — `circuit_breaker_drawdown_pct` (DEAD CODE — не впливає на runtime)

<!-- audited 2026-05-10: file:line drift corrected -->

**Default:** 12.0 % ([`strategy_sdk.py:410`](../../backend/services/strategy_sdk.py))
**Schema:** [`strategy_sdk.py:457`](../../backend/services/strategy_sdk.py)
**Validation:** [`strategy_sdk.py:1945-1946`](../../backend/services/strategy_sdk.py)

#### Direct consumers

**Жодного.** Перевірено `git grep` 2026-05-10 по
`backend/`: усі 4 згадки — лише `strategy_sdk.py` (defaults +
schema + coerce). **Жоден gate, жодний worker, жоден
decision-path не читає це поле.** Параметр відображається в
UI (вкладка Risk), валідується (0..100), персиститься в
`risk_limits_json` — але runtime його ігнорує.

#### Що насправді відіграє роль circuit breaker

Реальний break — **`halt_on_consecutive_losses` +
`max_consecutive_losses`** ([`risk_manager.py:119-130`](../../backend/services/trader_orchestrator/risk_manager.py),
[`trader_orchestrator_worker.py:5155-5240`](../../backend/workers/trader_orchestrator_worker.py)).
Він не дивиться на drawdown-percentage; рахує лише
послідовні збиткові закриття.

#### Compound with

Не має жодного на сьогодні. Якщо ви виставили
`circuit_breaker_drawdown_pct=12` і думаєте, що CB зрабить на
12% drawdown — **він не зрабить нічого**. Або підіймайте
`halt_on_consecutive_losses=True` + знижуйте
`max_consecutive_losses` (це справжній CB), або заводьте
плаг fix-плану на повноцінне підключення `circuit_breaker_drawdown_pct`.

---

### CRITICAL — `halt_on_consecutive_losses`

<!-- audited 2026-05-10: clean -->

**Default:** True ([`strategy_sdk.py:408`](../../backend/services/strategy_sdk.py))

#### Direct consumers

| Gate | Формула | File:line | reason-string |
|---|---|---|---|
| `trader_loss_streak` | `(not halt_on_losses) or trader_consecutive_losses < max_consecutive_losses → pass` | [`risk_manager.py:119-130`](../../backend/services/trader_orchestrator/risk_manager.py) | `Risk blocked: trader_loss_streak (streak=N max=M)` |
| Auto-pause + safe-exit | `if halt_on_losses and trader_loss_streak >= max_consecutive_losses_limit: → auto-pause + reconcile_live_positions(reason="circuit_breaker_safe_exit")` | [`trader_orchestrator_worker.py:5155-5240`](../../backend/workers/trader_orchestrator_worker.py) | event_type `circuit_breaker_pause` |

#### Indirect consumers

Жодних похідних метрик. Але **тригерить
`circuit_breaker_safe_exit` event**, що сам по собі
викликає [`reconcile_live_positions`](../../backend/services/trader_orchestrator/) →
**force-flatten усіх відкритих позицій трейдера**.

#### Compound with

- **`max_consecutive_losses`:** перший контролює
  ON/OFF самого механізму, другий — поріг. Перетин обох
  binding одночасно.
- **`max_position_notional_usd`:** менший cap → більше спроб
  на той самий budget → streak досягає ліміту швидше при
  тих самих ринкових умовах.
- **`max_open_orders`:** якщо до моменту halt-у відкрито N
  позицій, force-flatten реалізує всі N збитків одночасно.
  Може миттєво пробити `max_daily_loss_usd` cap, що
  в свою чергу заблокує перезапуск після ручного
  `is_paused=false`.

---

### CRITICAL — `max_consecutive_losses`

<!-- audited 2026-05-10: clean -->

**Default:** 4 ([`strategy_sdk.py:409`](../../backend/services/strategy_sdk.py))

#### Direct consumers

Той самий список, що й у `halt_on_consecutive_losses` вище —
обидва читаються спільно в `risk_manager.py:119-130` та
`trader_orchestrator_worker.py:5155-5240`.

#### Indirect consumers

Жодних.

#### Compound with

Те ж compound що й `halt_on_consecutive_losses`. Окреме
зауваження: **знизити `max_consecutive_losses` з 4 до 2 = у
~2× більш ймовірний halt** при тій самій win-rate стратегії.
Грубо: P(streak=N) ≈ (1 − win_rate)^N, тому 4→2 еквівалент
скорочення P(halt) інверсно. Перед зміною — порахуйте
очікувану win-rate бота і прикиньте, скільки halt-ів
очікувано на день.

---

### CRITICAL — `circuit_breaker_safe_exit` (event trigger, not numeric)

<!-- audited 2026-05-10: clean -->

**Не numeric knob.** Це **назва event-у**, який емітиться
коли `halt_on_consecutive_losses` тригериться.

#### Direct consumers (event handlers)

| Trigger | File:line | Action |
|---|---|---|
| `event_type=circuit_breaker_pause`, `subkind=circuit_breaker_safe_exit` | [`trader_orchestrator_worker.py:5155-5240`](../../backend/workers/trader_orchestrator_worker.py) | (1) trader auto-paused (`is_paused=true`); (2) `reconcile_live_positions(reason="circuit_breaker_safe_exit")` — force-flatten усіх відкритих позицій |

#### Indirect consumers

Жодних. Це terminal action, не gate.

#### Compound with

- **Force-flatten + `max_daily_loss_usd`:** N відкритих
  позицій на момент halt → одномоментна реалізація N
  збитків → миттєвий `daily_loss_cap` breach → блокує
  re-entry навіть після ручного `is_paused=false`.
  Спостерігалося в session 2026-05-08.
- **Live-mode vs shadow:** у live це реальні CLOB cancels +
  market-orders на закриття. У shadow — virtual flatten
  через `simulation_positions` updates. Семантика та сама,
  cost different.

---

### CRITICAL — `block_new_orders` (per-trader column)

<!-- audited 2026-05-10: file:line drift corrected -->

**Default:** False ([`backend/models/database.py:3796`](../../backend/models/database.py))

#### Direct consumers

| Gate | Формула | File:line | reason-string |
|---|---|---|---|
| `trader_block_new_orders` (implicit) | `if trader.block_new_orders: skip entire signal-processing cycle (return early)` | [`trader_orchestrator_worker.py:5268-5274`](../../backend/workers/trader_orchestrator_worker.py) | log-only: `block_new_orders active for trader %s — skipping all signal processing` |
| (fast-tier) | per-bot fast-runtime cursor advance + skip | [`fast_trader_runtime.py:1007-1018`](../../backend/workers/fast_trader_runtime.py) | reason `trader_block_new_orders` |

#### Indirect consumers

Жодних.

#### Set via

`PUT /api/traders/{trader_id}/block-new-orders`
([`routes_traders.py:1740-1771`](../../backend/api/routes_traders.py)).

#### Compound with

- **`is_paused`:** обидва зупиняють вхід. Різниця:
  `block_new_orders` тільки для нових orders, дозволяє
  exit/manage existing. `is_paused` — повне skip cycle (no
  signals, no exits via signal-loop).
- **Force-flatten triggers (CB safe-exit):** працюють
  **поверх** `block_new_orders` — навіть якщо blocked, CB
  все одно flatten-не позиції. Перевірено в коді
  reconcile path.

---

### CRITICAL — `traders.is_paused` / `traders.is_enabled`

<!-- audited 2026-05-10: file:line drift corrected -->

**Defaults:** `is_enabled=true`, `is_paused=false`
([`routes_workers.py:106-107`](../../backend/api/routes_workers.py))

#### Direct consumers

| Gate | Формула | File:line |
|---|---|---|
| Trader cycle gate | `is_running = bool(is_enabled) and not bool(is_paused)`; if not running → skip cycle | [`trader_orchestrator_worker.py:1133, 8443`](../../backend/workers/trader_orchestrator_worker.py) |
| Fast-tier per-bot gate | `if not is_enabled or is_paused: skip` (in `_run_loop` and `_drive_once`) | [`fast_trader_runtime.py:186, 223`](../../backend/workers/fast_trader_runtime.py) |

#### Indirect consumers

`traders_running` метрика на cycle dashboard
([`trader_orchestrator_worker.py:1053, 1074, 1130`](../../backend/workers/trader_orchestrator_worker.py)) —
візуальна, не gate-driving (sum-aggregation на line 1130).

#### Compound with

- **Worker_control.is_paused (orchestrator-wide):** окрема
  таблиця, окремий scope. `worker_control(trader_orchestrator).is_paused=true`
  зупиняє ВЕСЬ orchestrator незалежно від per-trader flags.
- **`block_new_orders`:** див. вище.

---

### CRITICAL — `worker_control.is_paused` / `worker_control.is_enabled` (orchestrator-wide)

<!-- audited 2026-05-10: clean -->

**Defaults:** `is_enabled=false`, `is_paused=true` на
свіжому деплої ([`routes_workers.py:420-421`](../../backend/api/routes_workers.py)).

#### Direct consumers

| Gate | Формула | File:line | reason-string |
|---|---|---|---|
| Orchestrator cycle | `if (not is_enabled) or is_paused or kill_switch: skip cycle (return [], 3.0)` | [`trader_orchestrator_worker.py:8408-8413`](../../backend/workers/trader_orchestrator_worker.py) | log-only: cycle silently skipped |

#### Indirect consumers

Жодних.

#### Set via

- `POST /api/workers/{worker}/start` → `is_enabled=true, is_paused=false`
- `POST /api/workers/{worker}/pause` → `is_paused=true`
- `PUT /api/orchestrator/control` для finer fields
  ([`routes_workers.py:420-467`](../../backend/api/routes_workers.py))

#### Compound with

- **`traders.is_paused`:** orchestrator-cycle skip має
  пріоритет. Якщо worker_control disabled — окремі
  `traders.is_paused=false` нічого не дають.
- **Auto-resume on startup (Plan 0021):** при запуску
  backend orchestrator default-flips на
  `is_enabled=true, is_paused=false` у shadow mode. Якщо
  ви ставите паузу в production, перезапуск backend може її
  скинути.

---

### CRITICAL — `allow_taker_limit_buy_above_signal`

<!-- audited 2026-05-10: file:line drift corrected -->

**Default:** False ([`strategy_sdk.py:413`](../../backend/services/strategy_sdk.py); також `order_manager.py:215`).

#### Direct consumers

| Path | Формула | File:line | Effect |
|---|---|---|---|
| `_allow_taker_limit_buy_above_signal` resolver | strategy_params alias check first; then `risk_limits.allow_taker_limit_buy_above_signal`; default `False` | [`order_manager.py:263-271`](../../backend/services/trader_orchestrator/order_manager.py) | Returns boolean |
| `_resolve_execution_price_bounds` (price-bound resolver) | for BUY+`taker_limit`: when `True` skip signal-price fallback if explicit cap exists (line 339-341) | [`order_manager.py:315-360`](../../backend/services/trader_orchestrator/order_manager.py) | Allows price ceiling above signal entry_price |
| Shadow simulator chase-up ceiling | leg resolver at line 864-868 → `_resolve_execution_price_bounds` call at 883-888 → shadow chase ceiling at 958-963 lifts `shadow_limit_price` above signal when toggle on (uses 1.0 fallback) | [`order_manager.py:864-963`](../../backend/services/trader_orchestrator/order_manager.py) | Shadow simulator may fill BUY at price > signal |

#### Indirect consumers

Жодних. Live mode price discipline не зачіпається — тільки
shadow simulator behavior.

#### Compound with

- **`max_entry_drift_pct`:** if `allow_taker_limit_buy_above_signal=true`
  but `max_entry_drift_pct` стиснуто (e.g. 2%), drift gate
  спрацює перед order_manager — chase-up не активується.
- **Shadow vs live:** **критично пам'ятати** — live mode
  ігнорує цей knob. Перемикаючи `mode=shadow → live`,
  стратегія почне rejected-ить orders, які раніше fill-ились.
- **`limit_price_not_executable` symptom:** дефолт `False`
  спричиняє цей reject коли market moved up since signal.
  Знаходиться в Step 7 trader-pipeline footguns.

---

### CRITICAL — `max_per_market_exposure_usd`

<!-- audited 2026-05-10: clean -->

**Default:** 500.0 USD ([`risk_manager.py:204`](../../backend/services/trader_orchestrator/risk_manager.py) `safe_float(..., 500.0)` fallback). Не присутній у `TRADER_RISK_DEFAULTS` — задається у `risk_limits` окремо (templates варіюють 250–1000 USD: [`templates.py:109,142,175,195,228,253,296,328,345`](../../backend/services/trader_orchestrator/templates.py)). У преамбулі вже згаданий як gate-name (рядки 63, 95, 186), але без формули і compound-effects.

#### Direct consumers

| Gate | Формула | File:line | reason-string |
|---|---|---|---|
| `trader_market_exposure` | `next_market = market_exposure_usd + size_usd; next_market <= min(max_per_market_exposure_usd, max_position_notional_usd)` | [`risk_manager.py:204-216`](../../backend/services/trader_orchestrator/risk_manager.py) | `Risk blocked: trader_market_exposure (next=… max=…)` |

#### Indirect consumers

- **Position lifecycle adopt path:** [`position_lifecycle.py:1774`](../../backend/services/trader_orchestrator/position_lifecycle.py) — копіюється з `risk_limits` у adoption metadata; downstream reconciliation бере його як authoritative cap при rebuild risk-snapshot.

#### Compound with

- **`max_position_notional_usd`:** на gate береться **min** з двох. Зменшення `max_position_notional_usd` **до значення нижче `max_per_market_exposure_usd`** робить per-market cap фактично мертвим (другий завжди жорсткіший). Зворотньо: lowering `max_per_market_exposure_usd` нижче `max_position_notional_usd` робить його ефективним.
- **`live_risk_clamps.max_per_market_exposure_usd_cap`:** у live mode додатково клампиться (default 10.0 USD у `LEGACY_IMPLICIT_LIVE_RISK_CLAMPS`). Бот, що в shadow має 600 USD, у live стає 10 USD — клампити навмисне, **це і є мета захисту**.
- **`MAX_PER_MARKET_USD` (config.py:397, default 500.0):** третій незалежний шар у `live_execution_service.py` — global per-market USD ceiling. Усі три шари — AND, мінімум перемагає.

---

### CRITICAL — `live_risk_clamps.*` (9 полів)

<!-- audited 2026-05-10: file:line + formula corrected -->

**Defaults:** `LEGACY_IMPLICIT_LIVE_RISK_CLAMPS` ([`trader_orchestrator_state.py:150-160`](../../backend/services/trader_orchestrator_state.py)) — 9 ключів: `enforce_allow_averaging_off=True`, `min_cooldown_seconds=60`, `max_consecutive_losses_cap=3`, `max_open_orders_cap=6`, `max_open_positions_cap=8`, `max_trade_notional_usd_cap=10.0`, `max_per_market_exposure_usd_cap=10.0`, `max_orders_per_cycle_cap=3`, `enforce_halt_on_consecutive_losses=True` (7 numeric caps + 2 enforce-flag bool-и). Description вже у преамбулі секції "Live-risk clamps" на початку файлу — тут CRITICAL-tier matrix-entry з file:line та compound-effects.

#### Direct consumers

| Path | Поведінка | File:line |
|---|---|---|
| `_normalize_live_risk_clamps` | per-key min/max валідація і запис у global_runtime.live_risk_clamps; `should_apply()` фільтр пропускає legacy-implicit defaults коли `live_risk_clamps_explicit=False` | [`trader_orchestrator_state.py:281-318`](../../backend/services/trader_orchestrator_state.py) |
| `_apply_live_risk_clamps` (orchestrator worker, primary apply path) | для кожного з 9 ключів: numeric `*_cap` → `effective = min(risk_limits[base], cap)`; `enforce_allow_averaging_off=True` → `allow_averaging=False`; `min_cooldown_seconds` → max з configured `cooldown_seconds`; `enforce_halt_on_consecutive_losses=True` → `halt_on_consecutive_losses=True`. Викликається у trader-cycle перед risk_manager. | [`trader_orchestrator_worker.py:3580-3659`](../../backend/workers/trader_orchestrator_worker.py) (call site `:5101`) |
| `_clamped_live_lifecycle_params` (reconciliation worker, partial subset) | вузька копія для lifecycle-params на 3 ключах: `max_trade_notional_usd_cap`, `max_per_market_exposure_usd_cap` (плюс **також клампить `max_position_notional_usd` тим самим cap-ом**) | [`trader_reconciliation_worker.py:301-330`](../../backend/workers/trader_reconciliation_worker.py) |

#### Indirect consumers

- **Усі CRITICAL знобі вище**, що мають `*_cap`-аналог: `max_open_orders`, `max_open_positions`, `max_trade_notional_usd`, `max_per_market_exposure_usd`, `max_orders_per_cycle`, `max_consecutive_losses`. Оскільки clamp перезаписує `risk_limits` **до** того, як risk_manager їх читає, gate бачить вже стиснуте значення — `Risk blocked: trader_open_orders` може спрацювати при 6, навіть якщо UI показує 20.
- **`enforce_allow_averaging_off=True`:** перезаписує `allow_averaging` у False незалежно від `TRADER_RISK_DEFAULTS`/UI.
- **`min_cooldown_seconds=60`:** піднімає `cooldown_seconds` до 60, якщо менший.
- **`enforce_halt_on_consecutive_losses=True`:** перезаписує `halt_on_consecutive_losses` у True.

#### Compound with

- **`live_risk_clamps_explicit`** (HIGH, нижче): коли `False` (default), legacy-implicit значення трактуються як "не override" — operator може встановити більш ліберальні `risk_limits` без зайвого клампу. Коли `True` — навіть legacy-defaults рахуються як explicit, override завжди в силі.
- **shadow → live перехід:** ботa, що у shadow набирав по `max_open_orders=20`, у live матиме 6. Це навмисний захист, але оператор часто бачить його як "бот замерз" (wave-2..4 stuck-positions від 2026-05-08).
- **CRITICAL-tier matrix entries без `*_cap`:** `max_daily_loss_usd`, `circuit_breaker_*`, `block_new_orders`, `kill_switch` — не клампляться live-clamps шаром.

---

### CRITICAL — `MAX_TRADE_SIZE_USD` / `MAX_DAILY_TRADE_VOLUME` / `MIN_ACCOUNT_BALANCE_USD`

<!-- audited 2026-05-10: clean -->

**Defaults:** 100.0 USD / 1000.0 USD / 0.0 USD ([`config.py:310-312`](../../backend/config.py)). AppSettings columns ([`database.py:1398-1402`](../../backend/models/database.py)) — UI Trading Safety Settings; ENV-overridable через `MAX_TRADE_SIZE_USD` / `MAX_DAILY_TRADE_VOLUME` / `MIN_ACCOUNT_BALANCE_USD`.

#### Direct consumers

| Gate | Формула | File:line | reject-message |
|---|---|---|---|
| `LiveExecutionService.check_order_safety` (size) | `size_usd > MAX_TRADE_SIZE_USD AND side=BUY → block` | [`live_execution_service.py:3997, 4007-4011`](../../backend/services/live_execution_service.py) | `Order size $X exceeds maximum $Y` |
| `LiveExecutionService.check_order_safety` (daily volume) | `_daily_volume + size_usd > MAX_DAILY_TRADE_VOLUME AND side=BUY → block` | [`live_execution_service.py:3998, 4015-4020`](../../backend/services/live_execution_service.py) | `Would exceed daily volume limit` |
| `LiveExecutionService` balance gate | `account_balance < MIN_ACCOUNT_BALANCE_USD → block buys` | [`live_execution_service.py:1715`](../../backend/services/live_execution_service.py) | n/a (early return) |

#### Indirect consumers

Жодних — ці три каpа application-level (live_execution_service), не дублюються у risk_manager. Орчестратор може дозволити signal, але live submission блокує його окремо.

#### Compound with

- **Per-trader `max_trade_notional_usd`:** AND з `MAX_TRADE_SIZE_USD`. Якщо trader має 5.0 (default), а ENV — 100.0, реальний cap = 5.0. Зворотньо — якщо trader 200.0, а ENV 100.0, cap = 100.0.
- **`MAX_DAILY_TRADE_VOLUME` vs `max_daily_loss_usd`:** orthogonal. Loss = realized PnL після resolution; volume = total notional placed. Можна впертися в один без іншого.
- **SELL exempt:** обидва size/volume gates скіпають SELL (exits завжди дозволені — щоб позицію можна було закрити).

---

### CRITICAL — `MAX_PER_MARKET_USD`

<!-- audited 2026-05-10: clean -->

**Default:** 500.0 USD ([`config.py:397`](../../backend/config.py)). ENV-overridable. Третій per-market cap-шар поверх `max_per_market_exposure_usd` (per-trader risk_limit) і `max_position_notional_usd` (per-trader risk_limit).

#### Direct consumers

| Gate | Формула | File:line | reject-message |
|---|---|---|---|
| `LiveExecutionService` per-market | `current_market_exposure + size_usd > MAX_PER_MARKET_USD AND side=BUY → block` | [`live_execution_service.py:558, 3999, 4023-4029`](../../backend/services/live_execution_service.py) | `Per-market limit: $X + $Y exceeds $Z` |

#### Indirect consumers

Жодних.

#### Compound with

- **`max_per_market_exposure_usd` (per-trader):** менший з двох перемагає. Будь-який bot з risk_limit > 500 USD де-факто отримує 500 USD ceiling від цього cap.
- **`live_risk_clamps.max_per_market_exposure_usd_cap`:** у live mode стиснення може бути ще жорсткішим (default 10 USD у legacy-implicit).
- **Сумарний effective cap:** `min(max_per_market_exposure_usd, max_position_notional_usd, MAX_PER_MARKET_USD, max_per_market_exposure_usd_cap)`. Tweaking у будь-якому шарі без перевірки решти трьох → нульовий effect (інший шар уже жорсткіший).

---

### CRITICAL — `worker_control.kill_switch`

<!-- audited 2026-05-10: file:line drift corrected -->

**Default:** False ([`trader_orchestrator_state.py:4762`](../../backend/services/trader_orchestrator_state.py)). Поле у row `worker_control`; tooglable через UI **і Telegram** ([`notifier.py:850, 1718, 2036-2041`](../../backend/services/notifier.py) — slash-команда `/kill_switch on|off`).

#### Direct consumers

| Path | Поведінка | File:line |
|---|---|---|
| `trader_orchestrator_worker._process_trader_cycle` | `if control.kill_switch: log + heartbeat + return 0` (skip усі signals, **до** signal-fetch) | [`trader_orchestrator_worker.py:5256-5267`](../../backend/workers/trader_orchestrator_worker.py) |
| `trader_orchestrator_worker._cycle_loop` | `manage_only_reasons.append("kill_switch")` — режим без entries, тільки reconciliation | [`trader_orchestrator_worker.py:8711-8713`](../../backend/workers/trader_orchestrator_worker.py) |
| `trader_orchestrator_worker` global cycle gate | `if not is_enabled or is_paused or kill_switch: return [], 3.0` — short-circuits roster build | [`trader_orchestrator_worker.py:8408-8413`](../../backend/workers/trader_orchestrator_worker.py) |
| `fast_trader_runtime` (BTC/ETH fast lane) | `if not enabled or paused or kill_switch: skip roster refresh` | [`fast_trader_runtime.py:1655-1659`](../../backend/workers/fast_trader_runtime.py) |
| `_firehose` (cross-strategy bus) | `control and is_enabled and not is_paused and not kill_switch` — signal pub gating | [`_firehose.py:121`](../../backend/services/strategies/_firehose.py) |

#### Indirect consumers

- **Telegram audit trail:** [`notifier.py:2036-2041`](../../backend/services/notifier.py) — toggle подія дублюється як `event_type="kill_switch"` у lifecycle log. Видно у `/status` — рядок `Kill Switch: ON|OFF`.

#### Compound with

- **`is_paused` / `is_enabled` (orchestrator):** kill_switch — **жорсткіший** з трьох. is_paused/is_enabled — кооперативні, кожен trader перевіряє у початку циклу. kill_switch — гарантована "STOP-ALL" семантика з Telegram.
- **`block_new_orders` (per-trader):** scope-narrower. kill_switch = global; block_new_orders = single-trader manage-only.
- **Force-flatten vs kill:** kill_switch не закриває відкриті позиції. Якщо потрібен force-exit, після kill — окремо `resume_policy=flatten_then_start` або manual close.

---

### CRITICAL — `runtime_metadata.resume_policy`

<!-- audited 2026-05-10: formula corrected -->

**Default:** `"resume_full"` ([`trader_orchestrator_state.py:3835-3836, 4165`](../../backend/services/trader_orchestrator_state.py)). Per-trader runtime_metadata field. Дозволені значення: `"resume_full"`, `"manage_only"`, `"flatten_then_start"` ([`strategy_sdk.py:1875`](../../backend/services/strategy_sdk.py); [`trader_orchestrator_worker.py:930`](../../backend/workers/trader_orchestrator_worker.py)). Раніше у цьому записі бракувало `"manage_only"`.

#### Direct consumers

| Path | Поведінка | File:line |
|---|---|---|
| `trader_orchestrator_worker` resume path | `force_flatten = resume_policy == "flatten_then_start"`; передається у `force_mark_to_market=force_flatten` під час trader resume; reason = `"worker_flatten_then_start"` коли flatten, інакше `"worker_lifecycle"` | [`trader_orchestrator_worker.py:4574-4581`](../../backend/workers/trader_orchestrator_worker.py) |
| `trader_orchestrator_worker` block-entries gate | `manage_only` → block_entries; `flatten_then_start` + open positions → block_entries з reason `Resume policy flatten_then_start waiting/blocked …` | [`trader_orchestrator_worker.py:4861-4871`](../../backend/workers/trader_orchestrator_worker.py) |

#### Indirect consumers

- **`session_engine` mark-to-market path:** `force_mark_to_market=True` тригерить batch-realization усіх відкритих позицій по поточному mid (event reason `worker_flatten_then_start`).
- **`block_entries_event_type="trader_resume_policy"`:** event-type, який пишеться у lifecycle log при `manage_only`/`flatten_then_start`-block; UI відображає це у Decisions як `block_entries`.

#### Compound with

- **`max_daily_loss_usd`:** flatten може реалізувати втрати, що відразу відкрить `daily_loss_cap_breached` gate → новий entry block.
- **`circuit_breaker_safe_exit`:** обидва — exit-mode triggers. CB exit тригериться автоматично; resume_policy — manual operator choice через API/UI.
- **`is_paused → resume`:** policy зчитується тільки на pause→start transition. Зміна policy на запущеному боті не має ефекту до наступного pause cycle.

---

### How to use this matrix

The walkthrough template is now live in
[`runtime-tweaks.md` — Walkthrough template for CRITICAL knob
changes](../operational/runtime-tweaks.md#walkthrough-template-for-critical-knob-changes).
Every new entry that touches a CRITICAL-tier knob from this
matrix must fill the 5-step skeleton (direct gate impact,
indirect-metric impact, live-data SQL simulation,
compound-effect checklist, < 30 s rollback). Prose-only
sections (`"no impact expected"`) are rejected at audit;
numeric values or `n/a — verified against matrix` answers are
required, and `n/a` is acceptable **only when this matrix
entry confirms zero impact**.

Phase 3 (agent memory rule) closes the loop by making the
matrix consultation mandatory before any PUT/UPDATE on a
CRITICAL field.

**Plan-design layer.** When the change is wrapped in a
Ralphex plan, the plan itself must satisfy the
[CRITICAL knob touch policy](../plans/README.md#critical-knob-touch-policy)
in `docs/plans/README.md` — link the relevant matrix entries
from `## Context / References`, drop the walkthrough check-box
into every applying Task, and disclose any HIGH/MEDIUM-tier
knobs co-touched in `## Out of scope`. The `plan-validator`
agent (`.claude/agents/plan-validator.md`) flags violations.

---

## Knob interaction matrix — HIGH tier

> **Призначення.** HIGH-tier knobs змінюють runtime-поведінку
> матеріально, але не state-flipping. Walkthrough template
> (Phase 2) їх не вимагає, **але** перед зміною все одно
> рекомендовано прочитати відповідний entry — формули і
> compound effects тут точно такі ж, просто blast-radius
> менший. CRITICAL запис у журналі без walkthrough = audit
> fail; HIGH запис без walkthrough = тільки рекомендація,
> не блокер.

### Group A — `TRADER_RISK_DEFAULTS` (per-trader, alive)

#### HIGH — `max_orders_per_cycle`
<!-- audited 2026-05-10: clean -->

**Default:** 6 ([`strategy_sdk.py:391`](../../backend/services/strategy_sdk.py))

| Gate | Формула | File:line | reason-string |
|---|---|---|---|
| `trader_orders_per_cycle` | `(cycle_orders_placed + 1) <= max_orders_per_cycle → pass` | [`risk_manager.py:141-148`](../../backend/services/trader_orchestrator/risk_manager.py) | `Risk blocked: trader_orders_per_cycle (next=X max=Y)` |

**Compound:** з `global_risk.max_orders_per_cycle` (orchestrator-wide cap, default 50). Менший з двох — binding.

#### HIGH — `position_cap_scope`
<!-- audited 2026-05-10: clean -->

**Default:** `"market_direction"` ([`strategy_sdk.py:394`](../../backend/services/strategy_sdk.py))

Enum: `market_direction` | `market` | `asset_timeframe`. Контролює як `max_position_notional_usd` aggregates positions при перевірці cap.

| Consumer | Behaviour | File:line |
|---|---|---|
| Risk-cap aggregation | `market_direction` рахує BUY/SELL на одному ринку як 2 окремі позиції; `market` — як 1 спільну (для cap-логіки); `asset_timeframe` (crypto) — group by `(asset, timeframe)` | [`decision_gates.py`](../../backend/services/trader_orchestrator/decision_gates.py) (через `_trader_size_limits`) |

**Compound:** перемикання scope міняє ефективну кількість слотів; з `market` half розміру cap-лімітів повертається оператору (BUY+SELL = 1 cap-slot замість 2).

#### HIGH — `cooldown_seconds`
<!-- audited 2026-05-10: clean -->

**Default:** 0 ([`strategy_sdk.py:400`](../../backend/services/strategy_sdk.py))

| Gate | Формула | File:line | reason-string |
|---|---|---|---|
| `trader_cooldown` | `not bool(cooldown_active) → pass` (cooldown триває після loss event протягом `cooldown_seconds`) | [`risk_manager.py:132-138`](../../backend/services/trader_orchestrator/risk_manager.py) | `Risk blocked: trader_cooldown (resume_at=…)` |

**Compound:** з `halt_on_consecutive_losses` (CRITICAL) — cooldown stagger-ить recovery після streak. Якщо обидва активні: streak hit → halt → resume → cooldown → first new entry.

#### HIGH — `slippage_bps`
<!-- audited 2026-05-10: confirmed dead -->

**Default:** 35.0 bps ([`strategy_sdk.py:402`](../../backend/services/strategy_sdk.py))

**Status:** Schema-only поле у `TRADER_RISK_DEFAULTS` (`strategy_sdk.py:402, 444, 1943`). Жоден runtime gate не читає `risk_limits["slippage_bps"]`. Поле, що знаходиться у [`order_manager.py:1047`](../../backend/services/trader_orchestrator/order_manager.py), — це лише **обчислений** Cox-PH slippage_bps на shadow-payload, не acceptance-cap. Реальний live slippage cap — це Group F `MAX_SLIPPAGE_PERCENT` (різний pipeline, у відсотках, default 2.0%); per-trader bps-cap відсутній. Якщо потрібен — додати consumer у `risk_manager` або `order_manager` через окремий план.

**Compound:** жодного — gate не існує. Розрахунковий `estimate.slippage_bps` (Cox PH) → пишеться у `shadow_simulation_payload` лише для звіту/UI, не блокує submit.

#### HIGH — `max_spread_bps`
<!-- audited 2026-05-10: confirmed dead -->

**Default:** 75.0 bps ([`strategy_sdk.py:403`](../../backend/services/strategy_sdk.py))

**Status:** Schema-only поле у `TRADER_RISK_DEFAULTS` (`strategy_sdk.py:403, 447, 1944`). У `decision_gates.py` та `order_manager.py` жодного `risk_limits["max_spread_bps"]` consumer **не існує** (grep'нуто 2026-05-10). Окремі **per-strategy** params з тим самим іменем живі (наприклад, [`sports_overreaction_fader.py:80, 354`](../../backend/services/strategies/sports_overreaction_fader.py) — там default 200.0, не 75.0; гейт `if spread_bps > max_spread_bps`), але ті — інший шар (`strategy_params`, не `risk_limits`). Per-trader risk_limit version не плагнуто у жоден gate.

**Compound:** немає (gate не існує). Якщо потрібен per-trader spread-cap незалежний від strategy_params — додати consumer окремим планом.

#### HIGH — `allow_averaging`
<!-- audited 2026-05-10: clean -->

**Default:** False ([`strategy_sdk.py:406`](../../backend/services/strategy_sdk.py))

| Gate | Формула | File:line | reason-string |
|---|---|---|---|
| `stacking_guard` (pre-gate) | `if final_decision == "selected" and market_id in occupied_market_ids and not allow_averaging: → block` | [`decision_gates.py:2247, 2259, 2301-2302`](../../backend/services/trader_orchestrator/decision_gates.py) | `Stacking guard: market already occupied (pre-gate)` |

**Compound:** з `max_position_notional_usd` (CRITICAL) і `position_cap_scope`. Коли `False` (default) — один market = одна позиція, незалежно від cap-розміру. `True` дозволяє DCA-стиль кілька entry на тому ж ринку, але обмежений `position_cap_scope` aggregation.

#### HIGH — `use_dynamic_sizing`
<!-- audited 2026-05-10: confirmed dead -->

**Default:** True ([`strategy_sdk.py:407`](../../backend/services/strategy_sdk.py))

**Status:** Schema-only поле у `TRADER_RISK_DEFAULTS` (`strategy_sdk.py:407, 457, 1948`). `_trader_size_limits` ([`strategies/base.py:219-245`](../../backend/services/strategies/base.py)) **жодного разу не звертається** до `risk_limits["use_dynamic_sizing"]` — base_size hardcoded як `max(1.0, max_trade * 0.40)`. У `decision_gates.py` consumer відсутній. Цей флаг ніяк не змінює sizing-logic.

**Compound:** немає (gate не існує). Sizing ефективно завжди працює як `max_trade_notional_usd × 0.40` для base_size та `max_trade_notional_usd` як ceiling, незалежно від цього флага.

#### HIGH — `max_entry_drift_pct`
<!-- audited 2026-05-10: file:line drift corrected -->

**Default:** 10.0 % ([`strategy_sdk.py:411`](../../backend/services/strategy_sdk.py))

| Gate | Формула | File:line | reason-string |
|---|---|---|---|
| `live_entry_drift` | `drift_score is None or abs(drift_score) <= max_drift → pass`, де `drift_score = live_context["entry_price_delta_pct"]` (per-trader limit, fallback 10.0%) | [`trader_orchestrator_worker.py:6648-6671`](../../backend/workers/trader_orchestrator_worker.py) | `drift={drift_score:.2f}%` (check_label "Entry drift from signal") |

**Compound:** з `allow_taker_limit_buy_above_signal` (CRITICAL). Якщо drift gate fail-ить раніше — chase-up shadow toggle вже не доходить до своєї логіки. Тобто строгий drift = chase-up де-факто disabled.

#### HIGH — `max_market_data_age_ms`
<!-- audited 2026-05-10: clean -->

**Default:** None (fall-through на env `EXECUTION_MARKET_DATA_MAX_AGE_MS`) ([`strategy_sdk.py:412`](../../backend/services/strategy_sdk.py))

| Gate | Формула | File:line | reason-string |
|---|---|---|---|
| `market_data_freshness` | `age_ms <= resolved_max_age → pass`. Resolution chain: per-bot risk_limits → strategy_params → env default | [`decision_gates.py:213-217`](../../backend/services/trader_orchestrator/decision_gates.py) | `Market data freshness gate blocked: source=X age_ms=… max=…` |

**Compound:** з `live_market_context.max_market_data_age_ms` (orchestrator-global) — той ставить ceiling; per-bot може ТІЛЬКИ робити tight-er, не loose-r. Подивитись який реально active під час debugging — через resolution chain у логах.

#### HIGH — `portfolio.*` (nested)
<!-- audited 2026-05-10: clean -->

**Defaults:** `enabled=False, target_utilization_pct=100.0, max_source_exposure_pct=100.0, min_order_notional_usd=10.0`
([`strategy_sdk.py:414-419`](../../backend/services/strategy_sdk.py))

| Gate | Формула | File:line |
|---|---|---|
| `portfolio_allocator` | `if portfolio.enabled and not allocation_allowed → block`; також підрізає `size_usd` до `allocator.allocated_size`; min-floor `>= portfolio.min_order_notional_usd` | [`decision_gates.py:1931-1972`](../../backend/services/trader_orchestrator/decision_gates.py) |

**Compound:** з `max_gross_exposure_usd` (CRITICAL) — allocator використовує його як total budget; `target_utilization_pct=80` означає use тільки 80% gross як real cap. `max_source_exposure_pct=30` обмежує per-source exposure (e.g. max 30% gross на `traders` source).

### Group B — Orchestrator global_runtime / global_risk / live_market_context

#### HIGH — `run_interval_seconds`
<!-- audited 2026-05-10: file:line drift corrected -->

**Default:** 30 ([`trader_orchestrator_state.py:101`](../../backend/services/trader_orchestrator_state.py))
**API min/max:** [1, 300] ([`routes_trader_orchestrator.py:164`](../../backend/api/routes_trader_orchestrator.py))

| Consumer | Behaviour | File:line |
|---|---|---|
| Orchestrator main loop sleep | `int(control.get("run_interval_seconds") or ORCHESTRATOR_DEFAULT_RUN_INTERVAL_SECONDS)` визначає інтервал між циклами | [`trader_orchestrator_worker.py:8644, 9140`](../../backend/workers/trader_orchestrator_worker.py) |

**Compound:** з `trader_cycle_timeout_seconds` (cycle hard-stop) — interval **повинен** бути ≥ typical cycle duration, інакше overlapping cycles. Якщо interval < typical cycle: orchestrator entering "always-busy" state, signal lag накопичується.

#### HIGH — `trader_cycle_timeout_seconds`
<!-- audited 2026-05-10: file:line drift corrected -->

**Default:** None (60s ефективно) ([`routes_trader_orchestrator.py:159`](../../backend/api/routes_trader_orchestrator.py))
**API range:** [30, 180]; orchestrator-state clamps to [3, 120] ([`trader_orchestrator_state.py:386`](../../backend/services/trader_orchestrator_state.py))

| Consumer | Behaviour | File:line |
|---|---|---|
| Per-trader cycle wrapper (per-trader override) | `_run_trader_once_with_timeout` зчитує `trader_limits["trader_cycle_timeout_seconds"]` (або з `source_configs[*].strategy_params`) і використовує як `effective_timeout` для `_run_trader_once`; пропагується далі як `cycle_timeout_seconds` budget у внутрішні `asyncio.wait_for` (4615, 4644, 5564, 6610) та `SET LOCAL statement_timeout` (4335-4336) | [`trader_orchestrator_worker.py:8107-8131, 8260`](../../backend/workers/trader_orchestrator_worker.py) |

**Footgun:** UI declares `min=3, max=120` ([`TradingPanel.tsx:12751`](../../frontend/src/components/TradingPanel.tsx)) ≠ API `[30, 180]`. Safe range: `[30, 120]`. Documented у trader-pipeline.md footguns.

**Compound:** з `runtime_trigger_cycle_timeout_seconds` (sibling, для lightweight cycles).

#### HIGH — `runtime_trigger_cycle_timeout_seconds`
<!-- audited 2026-05-10: clean -->

**Default:** None (10s ефективно) ([`routes_trader_orchestrator.py:160`](../../backend/api/routes_trader_orchestrator.py))
**API range:** [3, 60] (matches UI)

| Consumer | Behaviour | File:line |
|---|---|---|
| Lightweight runtime-trigger cycle clamp/normalize | normalize у [3.0, 60.0] | [`trader_orchestrator_state.py:387-390`](../../backend/services/trader_orchestrator_state.py) |
| Worker timeout vector | зчитується як final timeout для runtime-triggered cycle (clamped to [3.0, 60.0]) | [`trader_orchestrator_worker.py:8435-8440`](../../backend/workers/trader_orchestrator_worker.py) |

**Compound:** з `trader_cycle_timeout_seconds` — два різні timeout-и для двох різних cycle-types. Cyclical maintenance vs reactive event handling.

#### HIGH — `global_risk.max_gross_exposure_usd`
<!-- audited 2026-05-10: clean -->

**Default:** 5000.0 USD ([`templates.py:7`](../../backend/services/trader_orchestrator/templates.py))

| Gate | Формула | File:line |
|---|---|---|
| `global_gross_exposure` | те саме порівняння що per-trader cap, але summed over ALL traders | [`risk_manager.py:167-176`](../../backend/services/trader_orchestrator/risk_manager.py) |

**Compound:** з per-trader `max_gross_exposure_usd` (CRITICAL) — system-wide ceiling. Якщо сума всіх traders' caps > global cap → global stops them collectively. Дефолт 10× per-trader cap (2000), залишає room для 5 active traders.

#### HIGH — `global_risk.max_daily_loss_usd`
<!-- audited 2026-05-10: clean -->

**Default:** 500.0 USD ([`templates.py:8`](../../backend/services/trader_orchestrator/templates.py))

| Gate | Формула | File:line |
|---|---|---|
| `global_daily_loss` | `global_daily_realized_pnl_usd > -max_daily_loss_usd → pass`; стопить ALL trading | [`risk_manager.py:60-68`](../../backend/services/trader_orchestrator/risk_manager.py) |

**Compound:** з per-trader `max_daily_loss_usd` (CRITICAL) і всіх derived `trader_drawdown_pct`. Global cap зрабить раніше якщо ∑ losses across traders → cascading silence для всіх ботів. Reset у midnight UTC.

#### HIGH — `global_risk.max_orders_per_cycle`
<!-- audited 2026-05-10: clean -->

**Default:** 50 ([`templates.py:9`](../../backend/services/trader_orchestrator/templates.py))

Те саме порівняння що per-trader `max_orders_per_cycle`, але summed across traders ([`risk_manager.py:141-148`](../../backend/services/trader_orchestrator/risk_manager.py)). Менший з двох — binding.

#### HIGH — `live_market_context.enabled`
<!-- audited 2026-05-10: file:line drift corrected -->

**Default:** True ([`trader_orchestrator_state.py:325, 137`](../../backend/services/trader_orchestrator_state.py))

| Consumer | Behaviour | File:line |
|---|---|---|
| Worker per-cycle gate | `enable_live_market_context = bool(live_market_context_settings.get("enabled", True))` — `False` пропускає аґрегацію rolling history (`backend/services/trader_orchestrator/live_market_context.py`), залишаючи лише instant quote | [`trader_orchestrator_worker.py:5037`](../../backend/workers/trader_orchestrator_worker.py) |

**Compound:** з усіма `live_market_context.*` нижче — глобальний switch. Disable означає всі sub-knobs ефективно ignored.

#### HIGH — `live_market_context.history_window_seconds` / `.history_fidelity_seconds`
<!-- audited 2026-05-10: file:line drift corrected -->

**Defaults:** window=7200 (2h), fidelity=300 (5min) ([`trader_orchestrator_state.py:326-342`](../../backend/services/trader_orchestrator_state.py); ranges `window ∈ [300, 21600]`, `fidelity ∈ [30, 1800]`)

| Consumer | Behaviour | File:line |
|---|---|---|
| VWAP / rolling history aggregation | sample price points at `fidelity` interval over `window` span | [`backend/services/trader_orchestrator/live_market_context.py:1154-1155, 1523-1527`](../../backend/services/trader_orchestrator/live_market_context.py) |

**Compound:** менший window = reactive signals (миттєвий VWAP); більший = smoothed. Fidelity tight (60s) = більше CPU/memory; loose (1800s) = sparse rolling.

#### HIGH — `live_market_context.max_history_points` / `.timeout_seconds` / `.strict_ws_pricing_only`
<!-- audited 2026-05-10: clean -->

**Defaults:** max_history_points=120, timeout_seconds=4.0, strict_ws_pricing_only=True
([`trader_orchestrator_state.py:343-353`](../../backend/services/trader_orchestrator_state.py))

| Consumer | Behaviour | File:line |
|---|---|---|
| Decision-gate strict-WS check | `strict_ws_pricing_only=True` → reject signals не за WS quote | [`decision_gates.py:877-888`](../../backend/services/trader_orchestrator/decision_gates.py) |

**Compound:** strict-WS=True видобуває signal сорсі без WS feed (наприклад, REST-only data sources). Гарний guard для live mode, може блокувати dev signals у shadow.

#### HIGH — `pending_live_exit_guard.max_pending_exits` / `.identity_guard_enabled` / `.terminal_statuses`
<!-- audited 2026-05-10: clean -->

**Defaults:** `0` (off) / `True` / sorted `PENDING_LIVE_EXIT_TERMINAL_STATUSES` ([`trader_orchestrator_state.py:131-134, 270-278`](../../backend/services/trader_orchestrator_state.py)). У global_runtime, normalize range `max_pending_exits ∈ [0, 1000]`.

| Consumer | Behaviour | File:line |
|---|---|---|
| `decision_gates.run_platform_gates` (count gate) | `if max_pending_exits>0 and pending_count > max_pending_exits → block "Pending live exit guard"` | [`decision_gates.py:2091-2139`](../../backend/services/trader_orchestrator/decision_gates.py) |
| `decision_gates.run_platform_gates` (identity gate) | `if identity_guard_enabled and pending_exit on same (market_id, direction, signal_id) → block` | [`decision_gates.py:2145-2240`](../../backend/services/trader_orchestrator/decision_gates.py) |
| Cycle context | використовує `terminal_statuses` для materialize `pending_live_exit_summary` (фільтрує: status NOT IN terminal) | [`trader_orchestrator_worker.py:4790-4838`](../../backend/workers/trader_orchestrator_worker.py) |

**Compound:**
- **`max_pending_exits=0` (default) ≠ "disabled повністю"** — count-gate скіпається, але identity-guard працює завжди (заявка на той же market+direction блокується, поки попередній exit не resolve'ниться). Розповсюджена помилка — припускати, що 0 = "вимкнено".
- **Race condition fix:** identity-guard з'явився після інциденту 2026-04-XX, де UPSERT race на uq_trader_position_identity створювала дві паралельні exits на одну позицію.

#### HIGH — `live_provider_health.window_seconds` / `.min_errors` / `.block_seconds`
<!-- audited 2026-05-10: clean -->

**Defaults:** 180s / 2 / 120s ([`trader_orchestrator_state.py:145-149, 364-379`](../../backend/services/trader_orchestrator_state.py)). У global_runtime, ranges: `window_seconds ∈ [30, 900]`, `min_errors ∈ [1, 20]`, `block_seconds ∈ [15, 3600]`. Раніше явно out-of-scope у Plan 0029, тепер confirmed alive.

| Consumer | Behaviour | File:line |
|---|---|---|
| Cycle context provider-failure projection | `_ctx_provider_window_seconds` контролює rolling window для `live_provider_failure_snapshot` | [`trader_orchestrator_worker.py:4799-4825`](../../backend/workers/trader_orchestrator_worker.py) |
| Live-mode entries gate | `if snapshot.count >= min_errors: blocked_until = now + block_seconds` → `block_entries_event_type="live_provider_health_block"` | [`trader_orchestrator_worker.py:4882-4984`](../../backend/workers/trader_orchestrator_worker.py) |

**Compound:**
- **`TradingProxySettings.timeout`:** повторні timeouts → infra-failure events → провокують provider-health block. Tighter timeout → більше false positives → провадить block частіше.
- **`window_seconds × min_errors × block_seconds`:** трикутник sensitivity. window=30 + min_errors=2 = block спрацьовує на двох помилках за 30s; window=900 + min_errors=20 = практично вимкнено.
- **Тільки live mode:** shadow ботів не зачіпає (gate всередині `if run_mode == "live"`).

#### HIGH — `live_risk_clamps_explicit`
<!-- audited 2026-05-10: clean -->

**Default:** False ([`trader_orchestrator_state.py:292-319, 396-398`](../../backend/services/trader_orchestrator_state.py)). Bool flag у global_runtime; також пропагується у reconciliation worker ([`trader_reconciliation_worker.py:311`](../../backend/workers/trader_reconciliation_worker.py)).

| Consumer | Behaviour | File:line |
|---|---|---|
| `_normalize_live_risk_clamps` | `should_apply(key)` повертає True або коли source_is_explicit, або коли value ≠ legacy-implicit default | [`trader_orchestrator_state.py:292-319`](../../backend/services/trader_orchestrator_state.py) |

**Compound:**
- **CRITICAL `live_risk_clamps.*`:** flag керує тим, чи legacy-defaults трактуються як override. False (default) — operator може встановити, наприклад, `max_open_orders_cap=6` (= legacy default), і це **не зарахується** як override → бот може мати ліберальніший `max_open_orders=20`. True — навіть legacy-значення primum override.

#### HIGH — `live_market_context.max_market_data_age_ms`
<!-- audited 2026-05-10: clean -->

**Default:** 10000 ms ([`trader_orchestrator_state.py:354-360`](../../backend/services/trader_orchestrator_state.py); range `[25, 30000]`)

| Consumer | Behaviour | File:line |
|---|---|---|
| Decision-gate freshness fallback | використовується якщо per-bot `max_market_data_age_ms` не set | [`decision_gates.py:867`](../../backend/services/trader_orchestrator/decision_gates.py) |

**Compound:** ceiling для per-bot override. Per-bot можна тільки tighten нижче цього global; не loose.

### Group C — `traders_copy_trade` strategy_params (source-config level)

#### HIGH — `min_confidence`
<!-- audited 2026-05-10: file:line drift corrected -->

**Default:** 0.45 ([`traders_copy_trade.py:21`](../../backend/services/strategies/traders_copy_trade.py))

| Consumer | Формула | File:line |
|---|---|---|
| `evaluate()` DecisionCheck "confidence" | `confidence >= safe_float(params.get("min_confidence"), 0.45) → pass`; reason `copy_trade_gate_failed:confidence` | [`traders_copy_trade.py:725-731`](../../backend/services/strategies/traders_copy_trade.py) |

**Compound:** з `trader_tier` filter і wallet pool quality scoring. Високий поріг = fewer noisy copy-trades.

#### HIGH — `max_signal_age_seconds` / `max_signal_age_seconds_hard_ceiling`
<!-- audited 2026-05-10: file:line drift corrected -->

**Defaults:** age=5, hard_ceiling=600 ([`traders_copy_trade.py:24, 29`](../../backend/services/strategies/traders_copy_trade.py))

| Consumer | Формула | File:line |
|---|---|---|
| Compute effective max age | `max_signal_age_seconds = min(hard_ceiling, requested)`; `age_seconds = (now_utc - detected_at).total_seconds()` | [`traders_copy_trade.py:614-628`](../../backend/services/strategies/traders_copy_trade.py) |
| `evaluate()` DecisionCheck "max_age" | `age_seconds <= max_signal_age_seconds → pass`; reason `copy_trade_gate_failed:max_age` | [`traders_copy_trade.py:777-786`](../../backend/services/strategies/traders_copy_trade.py) |

**Compound:** з worker-trading event-loop latency. Tight age (5s) під 100% CPU → signals expire до того як cycle їх обробить. Документовано у `runtime-tweaks.md` 2026-05-07 entry — підняли з 5 до 60 саме тому.

#### HIGH — `min_source_notional_usd`
<!-- audited 2026-05-10: file:line drift corrected -->

**Default:** 10.0 USD ([`traders_copy_trade.py:22`](../../backend/services/strategies/traders_copy_trade.py))

| Consumer | Формула | File:line |
|---|---|---|
| `evaluate()` DecisionCheck "min_notional" | `source_notional >= safe_float(params.get("min_source_notional_usd"), 10.0) → pass`; reason `copy_trade_gate_failed:min_notional` | [`traders_copy_trade.py:742-748`](../../backend/services/strategies/traders_copy_trade.py) |

**Compound:** з `proportional_sizing` — фільтр невеликих source trades. Полігаркет leaders іноді делять малі трейди < $10 (тестові); high threshold (50) — тільки мажорні copies.

#### HIGH — `copy_delay_seconds`
<!-- audited 2026-05-10: file:line drift corrected -->

**Default:** 0 ([`traders_copy_trade.py:32`](../../backend/services/strategies/traders_copy_trade.py))

| Consumer | Behaviour | File:line |
|---|---|---|
| `evaluate()` DecisionCheck "copy_delay" | `age_seconds >= copy_delay_seconds → pass` (синхронний вік-чек на cycle, без `sleep`); reason `copy_trade_gate_failed:copy_delay` | [`traders_copy_trade.py:671, 787-793`](../../backend/services/strategies/traders_copy_trade.py) |

**Compound:** з `max_signal_age_seconds`. Якщо `copy_delay > max_signal_age` — кожен signal expires до того як його обробити. UI повинен валідувати delay < age.

#### HIGH — `proportional_sizing` / `proportional_multiplier`
<!-- audited 2026-05-10: file:line drift corrected -->

**Defaults:** sizing=True, multiplier=1.0 ([`traders_copy_trade.py:37-38`](../../backend/services/strategies/traders_copy_trade.py))

| Consumer | Behaviour | File:line |
|---|---|---|
| Position-size calculation | `True` + `source_notional > 0` → `target_size = source_notional * max(0.01, proportional_multiplier)`; `False` → fallback `source_notional` (>0) або `base_size = max_trade_notional_usd × 0.40`; завжди clamp `min(max_position_size, max_size_from_risk_limits)` | [`traders_copy_trade.py:864-880`](../../backend/services/strategies/traders_copy_trade.py) |

**Compound:** з `max_position_size` (strategy_param) і CRITICAL `max_trade_notional_usd`. Risk-cap binding виграє над proportional, якщо source_size × multiplier > cap.

#### HIGH — Inventory controls: `require_inventory_for_sells` / `allow_partial_inventory_sells` / `min_inventory_fraction`
<!-- audited 2026-05-10: clean -->

**Defaults:** require=True, allow_partial=True, min_fraction=0.25 ([`traders_copy_trade.py:46-48`](../../backend/services/strategies/traders_copy_trade.py))

| Consumer | Формула | File:line |
|---|---|---|
| Sell-copy gate ladder | `if run_mode == "live" and side == "SELL" and require_inventory_for_sells:` додає DecisionCheck "sell_inventory" (`available_shares > 0`) і "sell_inventory_fraction" (`available_fraction >= min_inventory_fraction`); якщо `allow_partial_inventory_sells=False` і available < requested → "sell_inventory_partial" fail | [`traders_copy_trade.py:918-960`](../../backend/services/strategies/traders_copy_trade.py) |

**Compound:** для sell-copy. `require=True, allow_partial=False` → must sell only full inventory; `True, True, 0.25` → must hold ≥ 25% intent-size to sell.

#### HIGH — `default_leader_weight` / `leader_weights` (dict)
<!-- audited 2026-05-10: file:line drift corrected -->

**Defaults:** default=1.0, dict empty ([`traders_copy_trade.py:42-43`](../../backend/services/strategies/traders_copy_trade.py))

| Consumer | Behaviour | File:line |
|---|---|---|
| Lookup leader weight | `default_leader_weight = safe_float(params.get("default_leader_weight"), 1.0)`; `leader_weight = safe_float(leader_weights.get(source_wallet), default_leader_weight)` | [`traders_copy_trade.py:592-598`](../../backend/services/strategies/traders_copy_trade.py) |
| Apply weight to sizing | `target_size = target_size * max(0.0, leader_weight)` (після proportional sizing, перед leader_allocation cap) | [`traders_copy_trade.py:875`](../../backend/services/strategies/traders_copy_trade.py) |
| DecisionCheck "leader_weight" | `leader_weight is not None and leader_weight > 0.0` → pass (weight=0 → fail з reason `copy_trade_gate_failed:leader_weight`) | [`traders_copy_trade.py:838-844`](../../backend/services/strategies/traders_copy_trade.py) |

**Compound:** з `max_leader_exposure_usd` (per-leader cap), `leader_allocation_cap_pct`. Weight=0 → leader gate fails (НЕ просто silently mute) — для повного muting без gate-fail треба прибрати wallet із scope.

#### HIGH — `traders_scope.modes` / `.individual_wallets` / `.group_ids`
<!-- audited 2026-05-10: file:line drift corrected -->

**Defaults:** modes=`["tracked","pool"]`, individual=`[]`, groups=`[]` ([`traders_copy_trade.py:55-59`](../../backend/services/strategies/traders_copy_trade.py))

| Consumer | Behaviour | File:line |
|---|---|---|
| `evaluate()` runtime scope match | `runtime_scope_context` (передається у context payload) → `StrategySDK.match_trader_signal_scope(signal, runtime_scope_context)`; fallback на explicit `params.get("traders_scope")` коли runtime context відсутній | [`traders_copy_trade.py:547-667`](../../backend/services/strategies/traders_copy_trade.py) |
| DecisionCheck "traders_scope" | `scope_passed → pass`; reason `copy_trade_gate_failed:traders_scope` | [`traders_copy_trade.py:698-704`](../../backend/services/strategies/traders_copy_trade.py) |
| Validation на configure | `cfg["traders_scope"] = StrategySDK.validate_trader_scope_config(...)` | [`traders_copy_trade.py:239`](../../backend/services/strategies/traders_copy_trade.py) |

**Compound:** з `min_confidence` і `trader_tier`. Звужує signal pool. `["tracked"]` only = high-quality discovered wallets; `["pool"]` only = leaderboard pool; combos працюють як OR.

### Group D — Scanner app-settings

#### HIGH — `scan_interval_seconds`
<!-- audited 2026-05-10: file:line drift corrected -->

**Default:** 60 ([`routes_settings.py:184`](../../backend/api/routes_settings.py); AppSettings column [`database.py:1332`](../../backend/models/database.py))

| Consumer | Behaviour | File:line |
|---|---|---|
| Scanner loop frequency | `self._interval_seconds = settings_row.scan_interval_seconds` керує `await sleep(...)` між market scan-cycle-ами | [`scanner.py:2613, 2625, 2642`](../../backend/services/scanner.py) |

**Compound:** з `max_markets_to_scan` — добуток ефективна throughput (markets/min).

#### HIGH — `min_profit_threshold`
<!-- audited 2026-05-10: file:line drift corrected -->

**Default:** 2.5 % ([`routes_settings.py:185`](../../backend/api/routes_settings.py); ENV mirror `MIN_PROFIT_THRESHOLD=0.025` у [`config.py:148`](../../backend/config.py))

| Consumer | Behaviour | File:line |
|---|---|---|
| BaseStrategy `self.min_profit` | `self.min_profit = settings.MIN_PROFIT_THRESHOLD` — кожна стратегія через `create_opportunity` фільтрує по цьому полю; opportunities з `roi < min_profit` discarded | [`strategies/base.py:675`](../../backend/services/strategies/base.py) |
| QualityFilter min_roi fallback | `min_roi → settings.MIN_PROFIT_THRESHOLD * 100` коли operator не задав override | [`quality_filter.py:162`](../../backend/services/quality_filter.py) |

**Compound:** з per-strategy `min_upside_percent`. Strategy може tighten ще, не loosen.

#### HIGH — `max_markets_to_scan`
<!-- audited 2026-05-10: file:line drift corrected -->

**Default:** 0 (no limit) ([`routes_settings.py:186-190`](../../backend/api/routes_settings.py); ENV mirror `MAX_MARKETS_TO_SCAN=0` у [`config.py:157`](../../backend/config.py))

| Consumer | Behaviour | File:line |
|---|---|---|
| Scanner enumeration cap | `market_cap = int(getattr(settings, "MAX_MARKETS_TO_SCAN", 0) or 0)` — apply cap after universe enumeration | [`scanner.py:1225`](../../backend/services/scanner.py) |
| Polymarket fetch cap | `cap = settings.MAX_MARKETS_TO_SCAN` — limit upstream pull when set | [`polymarket.py:607`](../../backend/services/polymarket.py) |

**Compound:** з `market_filter_tags` (plan 0005). Filter знижує count перед cap; cap — fallback safety.

#### HIGH — `min_liquidity`
<!-- audited 2026-05-10: file:line drift corrected -->

**Default:** 1000.0 USD ([`routes_settings.py:204`](../../backend/api/routes_settings.py); AppSettings column [`database.py:1338`](../../backend/models/database.py))

| Consumer | Behaviour | File:line |
|---|---|---|
| Opportunity filter (UI/list) | `if filter.min_liquidity > 0: opps = [o for o in opps if o.min_liquidity >= filter.min_liquidity]` | [`scanner.py:5083-5084`](../../backend/services/scanner.py) |

**Compound:** з per-strategy `min_liquidity` strategy_param. Той може tighten ще per-bot.

#### HIGH — `scanner_max_opportunities_total` / `scanner_max_opportunities_per_strategy`
<!-- audited 2026-05-10: confirmed dead -->

**Defaults:** 500 / 120 ([`database.py:1339-1340`](../../backend/models/database.py); ENV mirror `SCANNER_MAX_OPPORTUNITIES_TOTAL` / `SCANNER_MAX_OPPORTUNITIES_PER_STRATEGY` у [`config.py:146-147, 937-938`](../../backend/config.py); UI у [`routes_settings.py:205-211`](../../backend/api/routes_settings.py); persistence у [`settings_helpers.py:342-344, 825-826`](../../backend/api/settings_helpers.py)).

**Status:** Schema-only поля у app_settings. Жоден consumer у `backend/services/` чи `backend/workers/` не читає `settings.SCANNER_MAX_OPPORTUNITIES_TOTAL` або `_PER_STRATEGY` (grep'нуто 2026-05-10 — нуль hits поза config translation/UI). Раніше задокументоване як «hard caps на opportunities published per scan cycle» — насправді не enforced. Якщо потрібен top-N cut — додати consumer у `scanner.py` через окремий план.

**Compound:** немає (gate не існує).

#### HIGH — `scanner_skipped_signal_reactivation_cooldown_seconds`
<!-- audited 2026-05-10: file:line drift corrected -->

**Default:** 180s ([`database.py:1341`](../../backend/models/database.py); ENV mirror `SCANNER_SKIPPED_SIGNAL_REACTIVATION_COOLDOWN_SECONDS=0` у [`config.py:93`](../../backend/config.py) — note: ENV default 0 ≠ DB default 180; AppSettings персистенс перекриває). UI label «Skipped Signal Reactivation Cooldown».

| Consumer | Behaviour | File:line |
|---|---|---|
| Signal-bus reactivation cooldown | `raw = getattr(settings, "SCANNER_SKIPPED_SIGNAL_REACTIVATION_COOLDOWN_SECONDS", 0.0)` — мінімальний інтервал перед повторним publish скіпнутого сигналу (material price/quote-changes реактивують сигнали без cooldown) | [`signal_bus.py:45`](../../backend/services/signal_bus.py) |

#### HIGH — `scanner_strict_ws_max_age_ms`
<!-- audited 2026-05-10: file:line drift corrected -->

**Default:** 30000ms ([`database.py:1342`](../../backend/models/database.py); ENV `SCANNER_STRICT_WS_MAX_AGE_MS` у [`config.py:92`](../../backend/config.py)). Окремий scanner-side counterpart до `EXECUTION_MARKET_DATA_MAX_AGE_MS`.

| Consumer | Behaviour | File:line |
|---|---|---|
| Scanner WS staleness gate | `max(100.0, float(settings.SCANNER_STRICT_WS_MAX_AGE_MS) or 30000.0) / 1000.0` → seconds budget | [`scanner.py:828`](../../backend/services/scanner.py) |
| Intent-runtime cross-check | `scanner_max_age_ms = float(settings.SCANNER_STRICT_WS_MAX_AGE_MS or 30000)` для validate-перед-execution | [`intent_runtime.py:117`](../../backend/services/intent_runtime.py) |
| Live-market-context fallback | scanner-side counterpart fed into orchestrator's per-trader live context | [`live_market_context.py:109`](../../backend/services/trader_orchestrator/live_market_context.py) |
| Worker fallback | `getattr(settings, "SCANNER_STRICT_WS_MAX_AGE_MS", 30000)` коли інші per-trader/strategy/global timestamps null | [`trader_orchestrator_worker.py:5066`](../../backend/workers/trader_orchestrator_worker.py) |

**Compound:** з `EXECUTION_MARKET_DATA_MAX_AGE_MS` — на boundary scanner→execution два пороги стискаються AND. Якщо tick свіжий для scanner (≤30s) але вже застарів для execution (>10s) — opportunity ігнорується далі.

### Group E — Live-trading proxy

#### HIGH — `TradingProxySettings.timeout`
<!-- audited 2026-05-10: file:line drift corrected -->

**Default:** 30.0 s, range [5, 120] ([`routes_settings.py:661`](../../backend/api/routes_settings.py); AppSettings column persisted у `trading_proxy_timeout`)

| Consumer | Behaviour | File:line |
|---|---|---|
| ProxyConfig load | `timeout = row.trading_proxy_timeout or 30.0` — наповнює клієнтську конфігурацію HTTP-проксі | [`trading_proxy.py:109`](../../backend/services/trading_proxy.py) |

**Compound:** з `live_provider_health.window_seconds` — повторні timeouts тригерять provider-health blocker.

#### HIGH — `TradingProxySettings.require_vpn`
<!-- audited 2026-05-10: file:line drift corrected -->

**Default:** True ([`routes_settings.py:662`](../../backend/api/routes_settings.py); AppSettings column persisted у `trading_proxy_require_vpn`)

| Consumer | Behaviour | File:line |
|---|---|---|
| ProxyConfig load | `require_vpn=row.trading_proxy_require_vpn if row.trading_proxy_require_vpn is not None else True` | [`trading_proxy.py:110`](../../backend/services/trading_proxy.py) |
| VPN-gate (allowed/reason) | `if not cfg.require_vpn: return (True, "VPN check disabled")`; else викликає `verify_vpn_active(cfg)` і блокує live trades коли VPN не активний | [`trading_proxy.py:307-330`](../../backend/services/trading_proxy.py) |

**Compound:** geo-location / compliance. Disable тільки в dev — production live trading **must** require VPN.

---

### Group F — Live-execution / redeemer / fill-simulator (config.py + AppSettings)

#### HIGH — `EXECUTION_MARKET_DATA_MAX_AGE_MS`
<!-- audited 2026-05-10: clean -->

**Default:** 10000ms ([`config.py:87`](../../backend/config.py)). ENV-overridable. Останній fallback freshness gate, коли per-trader `max_market_data_age_ms` і strategy_params version обидва null.

| Consumer | Behaviour | File:line |
|---|---|---|
| `decision_gates` market-data-age budget | `default_budget = max(50, settings.EXECUTION_MARKET_DATA_MAX_AGE_MS)` коли downstream значення відсутні | [`decision_gates.py:212`](../../backend/services/trader_orchestrator/decision_gates.py) |

**Compound:** з CRITICAL `max_market_data_age_ms` (per-trader). Той перекриває цей як explicit override; цей — final ENV-fallback. Не плутати зі scanner `scanner_strict_ws_max_age_ms` (інший pipeline, інша AppSettings column).

#### HIGH — `MAX_SLIPPAGE_PERCENT`
<!-- audited 2026-05-10: clean -->

**Default:** 2.0% ([`config.py:371`](../../backend/config.py); AppSettings column [`database.py:1401`](../../backend/models/database.py)).

| Consumer | Behaviour | File:line |
|---|---|---|
| `PriceChaser` slippage cap | `max_slippage = original_price * (max_slippage_percent / 100.0)` — `chase_price` ніколи не виходить за `original_price ± max_slippage` | [`price_chaser.py:71, 146`](../../backend/services/price_chaser.py) |
| LiveExecutionService chase invocation | `max_slippage_percent=settings.MAX_SLIPPAGE_PERCENT` передається у chase loop | [`live_execution_service.py:5025`](../../backend/services/live_execution_service.py) |

**Compound:** з per-trader `slippage_bps` (Group A — наразі `confirmed dead`, gate відсутній). На практиці `MAX_SLIPPAGE_PERCENT` — єдиний реально активний slippage cap. Якщо потрібен per-trader bps acceptance gate (поверх chase ceiling) — додати consumer окремим планом.

#### HIGH — `MIN_ORDER_SIZE_USD`
<!-- audited 2026-05-10: clean -->

**Default:** 1.0 USD ([`config.py:313`](../../backend/config.py)).

| Consumer | Behaviour | File:line |
|---|---|---|
| LiveExecutionService size floor | `min_order_floor = StrategySDK.resolve_min_order_size_usd(..., fallback=settings.MIN_ORDER_SIZE_USD)`; `if size_usd < min_order_floor → block` | [`live_execution_service.py:3992-4005`](../../backend/services/live_execution_service.py) |

**Compound:** з `portfolio.min_order_notional_usd` (per-trader, default 10.0 USD) — другий бере приоритет якщо trader має portfolio config; інакше падаємо у ENV-fallback.

#### HIGH — `STALE_ORDER_AGE_HOURS` / `STALE_ORDER_PRICE_DRIFT_MULTIPLE` / `STALE_ORDER_RESIDUAL_SHARES`
<!-- audited 2026-05-10: clean -->

**Defaults:** 2.0h / 2.5× / 1.0 share ([`config.py:364-366`](../../backend/config.py)). `STALE_ORDER_AGE_HOURS_NO_MID` (sibling, default 8h) — fallback hard cutoff коли mid lookup fails.

| Consumer | Behaviour | File:line |
|---|---|---|
| Reconciliation worker stale-order sweep | старші за `STALE_ORDER_AGE_HOURS` ордери оцінюються; cancel якщо `limit ≥ STALE_ORDER_PRICE_DRIFT_MULTIPLE × current_mid` (BUY: інверс) або `residual_shares < STALE_ORDER_RESIDUAL_SHARES` | [`trader_reconciliation_worker.py:1005-1013`](../../backend/workers/trader_reconciliation_worker.py) |
| LiveExecutionService stale-log header | `STALE_ORDER_AGE_HOURS=%.1fh` у sweep-cadence info log | [`live_execution_service.py:1418-1433`](../../backend/services/live_execution_service.py) |

**Compound:** state-changing — sweep cancel-ить ордер. Зміна цих знобів **впливає на open-orders count** → опосередковано взаємодіє з CRITICAL `max_open_orders` (sweep звільняє слоти).

#### HIGH — `REDEEMER_MIN_PAYOUT_USD` / `REDEEMER_MAX_GAS_PRICE_GWEI` / `redeemer_force_including_losers`
<!-- audited 2026-05-10: clean -->

**Defaults:** 0.10 USD / 200.0 gwei / null ([`config.py:323-324, 947-949`](../../backend/config.py); AppSettings columns [`database.py:1405-1407`](../../backend/models/database.py)). `REDEEMER_FORCE_INCLUDING_LOSERS` ENV mirror у [`config.py:949`](../../backend/config.py).

| Consumer | Behaviour | File:line |
|---|---|---|
| CTF redemption policy | `min_payout_usd = float(getattr(settings, "REDEEMER_MIN_PAYOUT_USD", 0.10) or 0.0)`; `max_gas_price_gwei = float(getattr(settings, "REDEEMER_MAX_GAS_PRICE_GWEI", 200.0) or 0.0)`; `if expected_payout_usd < min_payout_usd: skip`; `if gas_price_gwei > max_gas_price_gwei: defer`; `force_including_losers=True` — redeem навіть програшні позиції (default off — only winning legs) | [`ctf_execution.py:1428-1429`](../../backend/services/ctf_execution.py) |

**Compound:** onchain blast — кожен redeem це onchain TX. Lowering `MIN_PAYOUT_USD` → більше gas spent на dust. Raising `MAX_GAS_PRICE_GWEI` → готовність redeem'ити при перегрітому network.

#### HIGH — `latency_fallback_p50_ms` / `latency_fallback_p95_ms` / `latency_fallback_p99_ms`
<!-- audited 2026-05-10: file:line drift corrected -->

**Defaults:** null → module-level constants 200/600/1500 ms ([`database.py:1420-1422`](../../backend/models/database.py)).

| Consumer | Behaviour | File:line |
|---|---|---|
| Cox-PH fill simulator latency envelope | `getattr(row, "latency_fallback_p50_ms"/p95/p99, None)` — використовується коли real submit/cancel latency не виміряні за останні 15 хв | [`fill_simulator/latency.py:76-78`](../../backend/services/fill_simulator/latency.py) |
| Persistence (UI updates) | `row.latency_fallback_p50_ms = _coerce(p50_ms)` (and p95/p99) | [`routes_fill_model.py:293-297`](../../backend/api/routes_fill_model.py) |

**Compound:** з shadow vs live divergence. Якщо fallback значення нижче real production latency, shadow-результати overestimate fill rate; навпаки — underestimate. Заломлення → calibration drift.

---

### Dead code in `TRADER_RISK_DEFAULTS`

Поля, які UI exposes, validation passes, БД персистить, **але runtime ігнорує**. Зміна цих knob-ів **не має жодного ефекту**. Усі підтверджені grep-ом 2026-05-10.

**UI marker.** З Plan 0031 (2026-05-10) форма Bot → Risk Limits
рендерить ці поля з **red-tinted background** і `⚠ deprecated · no
runtime effect` banner-ом над input-ом. Hover-tooltip каже коротку
ту саму інформацію плюс посилання на цю секцію. Plan 0038
(2026-05-10) розширив список з 5 до 8 полів (додав `slippage_bps`,
`max_spread_bps`, `use_dynamic_sizing` за результатами Plan 0036
HIGH-tier audit). Schema flag `"dead_code": True` лежить у
[`backend/services/strategy_sdk.py:437+`](../../backend/services/strategy_sdk.py)
(в `TRADER_RISK_FIELDS_SCHEMA`); single source of truth — backend
schema, frontend його тільки рендерить. Щоб **прибрати флаг**
(тобто реально wire-нути consumer-а), видалити `dead_code: True`
з schema, додати gate / decision / execution path, оновити
matrix-entry нижче.

- **`circuit_breaker_drawdown_pct`** (default 12.0, schema-only) —
  [`strategy_sdk.py:410, 463, 1951-1952`](../../backend/services/strategy_sdk.py).
  Описано детально у CRITICAL section вище. Реальний CB — це
  `halt_on_consecutive_losses + max_consecutive_losses`.

- **`max_daily_spend_usd`** (default 2000.0, schema-only) —
  [`strategy_sdk.py:399, 437, 1938-1939`](../../backend/services/strategy_sdk.py).
  UI label «Max Daily Spend (USD)», валідація `[1, 100M]`. **Жоден gate не читає.**
  Якщо вам потрібен daily-spend cap (orthogonal до loss-cap, бо loss = realized PnL,
  spend = total notional placed), потрібен B-плану щоб додати consumer у `risk_manager`.

- **`retry_limit`** (default 2, schema-only) —
  [`strategy_sdk.py:404, 450, 1945`](../../backend/services/strategy_sdk.py).
  UI label «Retry Limit», валідація `[0, 50]`. **Жоден submission consumer не читає.**
  Live execution path має власний retry-logic, що не звертається до цього поля.

- **`retry_backoff_ms`** (default 250, schema-only) —
  [`strategy_sdk.py:405, 453, 1946`](../../backend/services/strategy_sdk.py).
  Companion to `retry_limit`, той самий стан — без consumer-а.

- **`order_ttl_seconds`** (default 1200, schema-only) —
  [`strategy_sdk.py:401, 441, 1637, 1942`](../../backend/services/strategy_sdk.py).
  UI label «Order TTL (seconds)», валідація `[1, 86400]`. Line 1637 — це
  лише список полів, не consumer. **Жодний lifecycle / cancel scheduler
  не читає.** TTL/cancel керується іншими механізмами (e.g.
  `session_engine` cancel-on-cycle-end, `terminal_market_watchdog`).

- **`slippage_bps`** (default 35.0 bps, schema-only) —
  [`strategy_sdk.py:402, 444, 1943`](../../backend/services/strategy_sdk.py).
  UI label «Slippage Guard (bps)», валідація `[0, 10000]`. Surfaced у
  HIGH-tier matrix як `confirmed dead` (Plan 0036 audit) — детальніше
  у [HIGH § `slippage_bps`](#high--slippage_bps). Поле `order_manager.py:1047`,
  яке іноді з'являється у grep-результатах, — це **обчислений** Cox-PH
  slippage_bps, що пишеться у shadow-payload для звіту, не gate.
  Реальний live slippage cap — це Group F `MAX_SLIPPAGE_PERCENT`
  (різний pipeline, у відсотках, default 2.0%); per-trader bps-cap
  відсутній. Якщо потрібен — додати consumer у `risk_manager` /
  `order_manager` через окремий B-план.

- **`max_spread_bps`** (default 75.0 bps, schema-only) —
  [`strategy_sdk.py:403, 447, 1944`](../../backend/services/strategy_sdk.py).
  UI label «Max Spread (bps)», валідація `[0, 10000]`. Surfaced у
  HIGH-tier matrix як `confirmed dead` (Plan 0036 audit) — детальніше
  у [HIGH § `max_spread_bps`](#high--max_spread_bps). У `decision_gates.py`
  та `order_manager.py` жодного `risk_limits["max_spread_bps"]`
  consumer **не існує**. Окремі **per-strategy** params з тим самим
  іменем живі (наприклад,
  [`sports_overreaction_fader.py:80, 354`](../../backend/services/strategies/sports_overreaction_fader.py),
  default 200.0; gate `if spread_bps > max_spread_bps`), але ті —
  інший шар (`strategy_params`, не `risk_limits`). Per-trader
  risk_limit version не плагнуто у жоден gate.

- **`use_dynamic_sizing`** (default `True`, schema-only) —
  [`strategy_sdk.py:407, 457, 1948`](../../backend/services/strategy_sdk.py).
  UI label «Dynamic Position Sizing». Surfaced у HIGH-tier matrix як
  `confirmed dead` (Plan 0036 audit) — детальніше у
  [HIGH § `use_dynamic_sizing`](#high--use_dynamic_sizing).
  `_trader_size_limits`
  ([`strategies/base.py:219-245`](../../backend/services/strategies/base.py))
  **жодного разу не звертається** до `risk_limits["use_dynamic_sizing"]` —
  base_size hardcoded як `max(1.0, max_trade × 0.40)`. У
  `decision_gates.py` consumer відсутній. Цей флаг ніяк не змінює
  sizing-logic; sizing ефективно завжди працює як
  `max_trade_notional_usd × 0.40` для base_size та
  `max_trade_notional_usd` як ceiling, незалежно від цього флага.

**Підсумок:** 8 з 25 `TRADER_RISK_DEFAULTS` полів = **32% dead code**
(3 нові додано Plan 0038 на основі Plan 0036 HIGH-tier audit).
Якщо оператор хоче UI cleanup — окремий B/R план (приховати з UI
або додати реальний consumer).

### Dead code in `config.py` (Settings)

Module-level налаштування у `backend/config.py`, які **не мають runtime consumer** окрім `config_validator` (який лише range-checks). Підтверджено grep-ом 2026-05-10.

- **`PORTFOLIO_MAX_EXPOSURE_USD`** (default 5000.0) — [`config.py:409`](../../backend/config.py). Жодного consumer-а у `services/`/`workers/`. Реальні portfolio caps у `risk_limits.portfolio.*` (per-trader nested) і `max_gross_exposure_usd` (per-trader CRITICAL).
- **`PORTFOLIO_MAX_PER_CATEGORY_USD`** (default 2000.0) — [`config.py:410`](../../backend/config.py). Аналогічно. Category-level cap не реалізовано — реальний рівень scope = market (`max_per_market_exposure_usd`).
- **`PORTFOLIO_MAX_PER_EVENT_USD`** (default 1000.0) — [`config.py:411`](../../backend/config.py). Event-level cap не реалізовано.
- **`PORTFOLIO_MAX_KELLY_FRACTION`** (default 0.05) — [`config.py:413`](../../backend/config.py). Kelly bound читається з per-strategy `kelly_fractional_scale` (templates), не з ENV.
- **`CB_TRIP_DURATION_SECONDS`** (default 120) — [`config.py:390`](../../backend/config.py). Тільки `config_validator.py:81-82`. Реальний CB — `live_provider_health.block_seconds` + `halt_on_consecutive_losses`.
- **`MIN_DEPTH_USD`** (default 200.0) — [`config.py:384`](../../backend/config.py). У `depth_analyzer.py:33` дублюється module-level (`MIN_DEPTH_USD = 200.0`) і використовує локальну копію — config-side значення не plumbed. Зміна `settings.MIN_DEPTH_USD` не впливає на runtime depth-аналіз.

**Підсумок:** 6 ENV/AppSettings налаштувань — orphan'и. Зміна не дає ефекту. Cleanup — окремий B/R план.


