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
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T \
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
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T \
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

> Завжди через SSH, бо постгрес тільки на `polyhome-1`.

```bash
# Розподіл рішень за останні 24 год для конкретного бота
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T \
  postgres psql -U homerun -d homerun -c \
  "SELECT decision, count(*) FROM trader_decisions \
   WHERE trader_id = '\''<TRADER_ID>'\'' \
     AND created_at > now() - interval '\''24 hours'\'' \
   GROUP BY decision ORDER BY count(*) DESC"'

# Топ причин blocked / skipped
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T \
  postgres psql -U homerun -d homerun -c \
  "SELECT reason, count(*) FROM trader_decisions \
   WHERE trader_id='\''<TRADER_ID>'\'' \
     AND decision IN ('\''blocked'\'',  '\''skipped'\'') \
     AND created_at > now() - interval '\''1 hour'\'' \
   GROUP BY reason ORDER BY count(*) DESC LIMIT 20"'

# Поточні відкриті позиції + ордери
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T \
  postgres psql -U homerun -d homerun -c \
  "SELECT (SELECT count(*) FROM trader_positions WHERE trader_id='\''<TID>'\'' AND status='\''open'\'') AS open_positions, \
          (SELECT count(*) FROM trader_orders    WHERE trader_id='\''<TID>'\'' AND status='\''open'\'') AS open_orders"'

# Детальний risk_snapshot з останнього blocked-рішення
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T \
  postgres psql -U homerun -d homerun -x -c \
  "SELECT created_at, reason, risk_snapshot_json \
   FROM trader_decisions \
   WHERE trader_id='\''<TID>'\'' AND decision='\''blocked'\'' \
   ORDER BY created_at DESC LIMIT 1"'

# Останні revisions конфігу
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T \
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
