# Traders Copy Trade

> Спільні поняття (як живе бот, risk-limits, decision-gates, UI-вкладки
> Tune / Risk / Decisions / Performance, SQL-діагностика) винесені в
> [`_common-bot-parameters.md`](_common-bot-parameters.md). Цей файл
> описує **тільки те, що специфічне для стратегії
> `traders_copy_trade`** — pipeline сигналу, кожен параметр стратегії
> з ефектом, кожен `evaluate()`-чек, sizing-математика, інвентарний
> чек для SELL, поведінка резолюції токен → ринок.
>
> Last verified: 2026-05-10

## Сутність

Real-time mirror одного leader-wallet: коли tracked-гаманець робить
trade, стратегія **копіює його** — той самий market (token), та ж
сторона, sizing проpорційний leader-нотіоналу × leader_weight. На
відміну від [Traders Confluence](traders-confluence.md), яка
агрегує ≥ 2 wallet-сигнали в один opportunity, copy-trade відкриває
позиції за **одним leader-wallet за раз**.

Це найбільш raw-form behavioral edge: ставимо на те, що конкретний
гаманець знає більше за market. Risk control — `leader_weights`,
`max_leader_exposure_usd`, `max_copy_source_exposure_usd`,
`max_copy_drawdown_pct`, `max_copy_daily_loss_usd`, плюс орбітальні
risk-limits (див. спільний доку).

## Контракт

- **Файл**: [`backend/services/strategies/traders_copy_trade.py`](../../backend/services/strategies/traders_copy_trade.py)
- **Клас**: `TradersCopyTradeStrategy`
- **slug** (`strategy_type`): `traders_copy_trade`
- **`source_key`**: `traders`
- **Worker plane**: сигнали продукуються у `worker-trading` сервісом
  `traders_copy_trade_signal_service`; рішення оркеструються
  `trader_orchestrator_worker` (normal-tier) або
  `fast_trader_runtime` (fast-tier).
- **Subscriptions**: in-process callback від `wallet_ws_monitor`
  (НЕ через `event_bus`), деталі — нижче. Конвертується у
  `trade_signals.source='traders'`,
  `signal_type='copy_trade'`/`single_wallet_buy`/`single_wallet_sell`.
- **`accepted_signal_strategy_types`**: `["traders_copy_trade"]`
- **`allow_deduplication`**: `False` — кожен leader-event своя
  opportunity; дедуп робиться bridge-ом через
  `dedupe_key=(wallet, token_id, side)`.

## Pipeline сигналу: leader trade → opportunity → order

Канонічна схема в
[`docs/plans/architecture/copy-trade-pipeline.md`](../plans/architecture/copy-trade-pipeline.md).
Скорочена версія:

```
Polymarket leader wallet (1 з ~51 у scope)
    │ Polygon RPC + user-channel WS
    ▼
wallet_ws_monitor (services/wallet_ws_monitor.py)
    │ INSERT wallet_monitor_events
    │ in-process add_callback (не event_bus)
    ▼
traders_copy_trade_signal_service._on_wallet_trade
    │ asyncio.Queue (maxsize 20000) × 8 processor_loop tasks
    ▼
_process_wallet_trade_event
    │ resolve token_id → market (gamma + 300 с cache)
    │ build Opportunity з positions_to_take, strategy_context.copy_event
    ▼
TradersCopyTradeStrategy.detect_async (це backend/services/strategies/traders_copy_trade.py)
    │ повертає Opportunity у bridge
    ▼
bridge_opportunities_to_signals → INSERT trade_signals (status='pending', expires_at = +15 хв за замовч.)
    │
    ▼
runtime_signal_queue (asyncio) + Redis SIGNAL_EMISSION_CHANNEL
    │
    ▼
trader_orchestrator_worker._run_trader_once_inner (per trader, кожні interval_seconds)
    │ list_unconsumed_signals (filter source/strategy_type)
    │ build context (params, trader, mode, live_market, traders_scope_context,
    │   copy_risk_context, copy_allocation_context, copy_inventory_context)
    │ → ThreadPoolExecutor (8 worker, 10 с timeout) →
    ▼
TradersCopyTradeStrategy.evaluate(signal, context)
    │ повертає StrategyDecision(decision, reason, score, size_usd, checks, payload)
    ▼
apply_platform_decision_gates (size_cap, schedule, freshness, risk, stacking, ...)
    │ якщо selected ⇒ submit_execution_leg
    ▼
shadow: execution_simulator.simulate_execution (Cox-PH)
live:   live_execution_service.submit_order (CLOB)
    │
    ▼
trader_orders / trader_positions (open) → reconciliation worker (30 с) → should_exit()
```

Час від wallet trade до open position: **<1 с** на fast-tier,
**до `interval_seconds` (default 5 с)** на normal-tier.

## Що шукає `detect_async()` — побудова opportunity

`detect_async` тут тривіальний: він **не сканує ринки**, не
фільтрує — він просто конвертує payload, який уже зібрав
`traders_copy_trade_signal_service` (вхідний `events[]` має
`copy_event`, `source_trade`, `market` поля). Перевіряється:

- side ∈ `{BUY, SELL}` (інакше `None`);
- `token_id` присутній;
- `entry_price > 0`, `size > 0`;
- `market_id` присутній або синтетичний (`token:{token_id}`).

Усе сильніше фільтрування делегується в `evaluate()`.

> **Token → market resolution**: робиться в
> `traders_copy_trade_signal_service._resolve_market_snapshot()`
> через `polymarket_client.get_market_by_token_id(token_id)`. Якщо
> gamma відповідає None — будується синтетичний snapshot із
> `market_id="token:{token_id}"`, `liquidity=None`. Сигнал все одно
> публікується (це **firehose**, не filter); відсікати такі мають
> downstream-гейти. Multi-outcome ринки підтримуються — outcome не
> примусово YES/NO.

> **Scanner / quality filter / monitor / prioritizer / regime /
> category buffers НЕ застосовуються до `traders` source.** Стратегія
> оминає `market_catalog` повністю; її opportunity йде в bridge з
> `quality_filter_pipeline=None`, тому `trade_signals.quality_passed`
> завжди `NULL`. Якщо у вашому боті ввімкнено
> `firehose_require_qualified_source=true`, всі copy-trade сигнали
> будуть мовчки skipped — ця опція повинна бути `false` (default).

## Параметри стратегії: повна довідка

Дефолти живуть у `TRADERS_COPY_TRADE_DEFAULTS`
(`backend/services/strategies/traders_copy_trade.py:20-60`).
Валідатор `validate_traders_copy_trade_config` зажимає кожне поле
у валідний діапазон.

### Сигнально-фільтрувальні (фази `signal`)

| Ключ | Default | Діапазон | Чек у `evaluate()` | Що блокує |
|---|---|---|---|---|
| `min_confidence` | `0.45` | 0..1 | `confidence` | Confidence (з copy_event або 0.70 fallback) має бути ≥. |
| `min_source_notional_usd` | `10.0` | 0..1_000_000 | `min_notional` | leader-trade `notional_usd = price × size` має бути ≥. Знижуйте, щоб ловити мікро-трейди; підвищуйте, щоб не копіювати dust. |
| `max_entry_price` | `0.98` | 0..1 | `entry_price` | Захищає від copy на майже-резолвлених ринках (mean-reverting tail). |
| `max_signal_age_seconds` | `5` | 1..600 | `max_age` | Скільки секунд може бути сигнал, перш ніж стати марним. **Реальна стеля 600 с** — `max_signal_age_seconds_hard_ceiling=600.0`, ніщо вище не приймається. |
| `max_signal_age_seconds_hard_ceiling` | `600.0` | — | (clamp на `max_age`) | Архітектурна стеля. Підняти можна тільки правкою коду. |
| `min_live_liquidity_usd` | `150.0` | 0..1_000_000_000 | `live_liquidity` | Live ws-liquidity ринку має бути ≥. Якщо ws-data немає — гейт пропускається (skip-gate, не блокує). |
| `max_adverse_entry_drift_pct` | `2.0` | 0..100 | `entry_drift` | На скільки % live-ціна може відсунутись у несприятливий бік від leader-fill. Для BUY: `live > leader → adverse=positive`; для SELL: симетрично. Якщо drift-data немає — гейт skipped. |
| `copy_delay_seconds` | `0` | 0..300 | `copy_delay` | Опціональна затримка: сигнал відкидається, якщо `age < delay`. Корисно, щоб не випереджати leader. |
| `copy_existing_positions_on_start` | `False` | — | (signal-service) | Якщо `true`, при старті бота сервіс синтезує сигнали для всіх вже-відкритих позицій leader-ів. Default false: копіюємо тільки нові трейди. |
| `copy_buys` | `True` | — | `copy_side` | Якщо false — BUY-сигнали skipped. |
| `copy_sells` | `True` | — | `copy_side` | Якщо false — SELL-сигнали skipped. |
| `edge_midpoint` | `0.5` | 0..1 | (тільки для `roi_percent` сигналу) | Опорна ціна, від якої міряється "екстремальність" leader-входу. |
| `edge_multiplier` | `200.0` | 0..1000 | те саме | `edge_percent = abs(price - midpoint) × multiplier`. Default дає `edge=100%` для price=0 або 1.0, `edge=10` для 0.55/0.45. Це **не** реальна доходність, це лише `roi_percent` сигналу для скорінгу й сортування у Discovery. |

### Sizing та обмеження експозиції

| Ключ | Default | Що робить |
|---|---|---|
| `max_position_size` | `1000.0` | Cap на одну copy-позицію (USD). Орбітальний `risk_limits.max_position_notional_usd` зазвичай нижчий і кладеться зверху. |
| `proportional_sizing` | `True` | Якщо `true` — `target_size = leader_notional × proportional_multiplier × leader_weight`. Якщо `false` — `target_size = leader_notional` (fallback на `_trader_size_limits` `base_size`, default $25). |
| `proportional_multiplier` | `1.0` | 1:1 з leader. Знижуйте до 0.1 для "копіюємо 10%", підвищуйте з обережністю. |
| `default_leader_weight` | `1.0` | Per-leader weight, якщо leader не у `leader_weights` mapping. |
| `leader_weights` | `{}` | `{wallet_addr: weight}`. Weight 0 → leader **виключений** (`leader_weight` чек не пройде). |
| `leader_allocation_cap_pct` | `100.0` | Cap (%) від leader_notional, який цей бот алокує. 100 = повний 1:1. |
| `max_leader_exposure_usd` | `1_000_000.0` | Поточна сумарна copy-експозиція **по цьому leader-у**. Дефолт = no cap. **Обов'язково знизьте перед live**. |
| `max_copy_source_exposure_usd` | `1_000_000.0` | Сумарна copy-експозиція **по всьому traders source** (всі leader-и разом). Дефолт = no cap. |
| `max_copy_drawdown_pct` | `100.0` | Drawdown limit для copy-стратегії (%). Дефолт = no cap. |
| `max_copy_daily_loss_usd` | `1_000_000.0` | Daily loss cap (USD). Дефолт = no cap. |

> **Всі чотири `max_copy_*` дефолти ефективно вимикають захист.**
> Це навмисно, щоб у shadow можна було тестувати; перед live —
> виставляйте реалістичні значення (наприклад `max_copy_drawdown_pct=10`,
> `max_copy_daily_loss_usd=200`, `max_copy_source_exposure_usd=500`,
> `max_leader_exposure_usd=100`).

### Інвентар (тільки для SELL у live-mode)

| Ключ | Default | Що робить |
|---|---|---|
| `require_inventory_for_sells` | `True` | Якщо `true`, SELL без open-позиції на цей token — `skipped`. У shadow **не застосовується**. |
| `allow_partial_inventory_sells` | `True` | Якщо є частина токена — продаємо тільки наявне. Якщо false — або повний sell, або skip. |
| `min_inventory_fraction` | `0.25` | Мінімум `available_shares / requested_shares = 25%`. Менше — `sell_inventory_fraction` чек fail. |

### Wallet scope

| Ключ | Default | Що робить |
|---|---|---|
| `traders_scope.modes` | `["tracked", "pool"]` | Які множини гаманців об'єднуємо у scope. Доступні: `tracked` (`tracked_wallets` таблиця), `pool` (`discovered_wallets WHERE in_top_pool=true`), `individual` (з `individual_wallets`), `group` (з `group_ids` через `trader_group_members`). |
| `traders_scope.individual_wallets` | `[]` | Список гаманців (адреси чи Polymarket-username-и). Resolve username→адреса робить `polymarket_client.resolve_wallet_identifier` async. |
| `traders_scope.group_ids` | `[]` | Список UUID-груп з `trader_groups`. |

Резолюція scope live-кешується **на 60 секунд**
(`_TRADERS_SCOPE_CONTEXT_CACHE_TTL_SECONDS`,
`trader_orchestrator_worker.py:3610`). Якщо ви додали wallet у
tracked — він почне ловитися ботом протягом ≤ 60 с. Виконавчий
гаманець бота автоматично виключається зі scope.

## Чеки `evaluate()`: список у порядку виконання

Усі чеки повертаються в `StrategyDecision.checks` і йдуть у
`trader_decisions.checks_summary_json`. Кожен fail → `decision='skipped'`,
`reason='copy_trade_gate_failed:<keys, comma-sep>'`. У UI вкладка
**Decisions** малює їх з ✓/✗ і `detail`-полем.

| `key` | Що перевіряє | Звідки беруться вхідні дані |
|---|---|---|
| `source` | `signal.source == "traders"` | сигнал |
| `strategy_type` | `signal.strategy_type ∈ accepted_strategy_types` | сигнал |
| `traders_scope` | leader-wallet у поточному scope | `traders_scope_context` (60-с кеш) |
| `source_trade` | у payload є `tx_hash` | copy_event |
| `source_wallet` | wallet присутній і нормалізований | copy_event |
| `token` | `selected_token_id` або `token_id` присутній | payload |
| `entry_price_available` | `entry_price > 0`, прийнятий або з live-quote, або з signal | `live_market.live_selected_price` має пріоритет |
| `confidence` | ≥ `min_confidence` | сигнал |
| `entry_price` | ≤ `max_entry_price` | сигнал/live |
| `min_notional` | `source_notional_usd ≥ min_source_notional_usd` | source_trade |
| `live_liquidity` | `live_market.liquidity_usd ≥ min_live_liquidity_usd` | live_market_context_builder |
| `entry_drift` | adverse drift % ≤ `max_adverse_entry_drift_pct` | `live_market.entry_price_delta_pct` |
| `signal_timestamp` | `detected_at` присутній | copy_event |
| `max_age` | `now - detected_at ≤ max_signal_age_seconds` | clamp до `max_signal_age_seconds_hard_ceiling=600` |
| `copy_delay` | `age ≥ copy_delay_seconds` | те саме |
| `copy_drawdown` | `trader_drawdown_pct ≤ max_copy_drawdown_pct` | `copy_risk_context` (skip-gate якщо None) |
| `copy_daily_loss` | `trader_daily_loss_usd ≤ max_copy_daily_loss_usd` | `copy_risk_context` |
| `copy_source_exposure` | сумарна по `source` ≤ `max_copy_source_exposure_usd` | `copy_allocation_context` (`get_trader_source_exposure`) |
| `copy_leader_exposure` | сумарна по leader ≤ `max_leader_exposure_usd` | `copy_allocation_context` (`get_trader_copy_leader_exposure`) |
| `leader_weight` | `leader_weights[wallet]` (або default) > 0 | `params.leader_weights` |
| `copy_side` | BUY → `copy_buys=true`; SELL → `copy_sells=true` | сигнал |

Після цього блоку розраховується `target_size`:

```
target_size = max( source_notional × max(0.01, proportional_multiplier),
                   _trader_size_limits.base_size if not proportional )
target_size = min(target_size, max_position_size, _trader_size_limits.max_size)
target_size = target_size × max(0.0, leader_weight)
target_size = min(target_size, leader_notional × leader_allocation_cap_pct/100)
target_size = min(target_size, max_position_size, _trader_size_limits.max_size)
```

Далі додаткові чеки залежно від обчисленого `target_size` і
контексту:

| `key` | Що перевіряє |
|---|---|
| `copy_source_capacity` | `max_copy_source_exposure_usd - current_source_exposure ≥ 1.0`, далі обмежує `target_size` |
| `copy_leader_capacity` | те саме для leader; обмежує `target_size` |
| `sell_inventory` | (live + SELL + `require_inventory_for_sells`) `available_shares > 0` |
| `sell_inventory_fraction` | (якщо available < requested) `available/requested ≥ min_inventory_fraction` |
| `sell_inventory_partial` | (якщо available < requested і `allow_partial_inventory_sells=false`) — fail; інакше `target_size = available × price` |
| `size_floor` | `target_size ≥ 1.0` USD |

Якщо все пройшло — `decision='selected'`, `score = confidence × 70 +
min(30, source_notional/100)`, payload містить
`copy_trade.source_wallet`, `leader_weight`, `source_notional_usd`,
`source_tx_hash`, `target_size_usd`.

Далі сигнал іде в `apply_platform_decision_gates` —
див. [спільний документ](_common-bot-parameters.md#decision-gates-що-може-заблокувати-рішення).

## Логіка виходу: `should_exit()`

Стратегія делегує у `BaseStrategy.default_exit_check`
(`backend/services/strategies/base.py:~1334`). Послідовність:

1. **Market resolved** → `close` за `winning_outcome` (1.0 або 0.0).
2. **Resolve-only mode** (config) → `hold`.
3. **Min hold period** — `hold` поки `age < min_hold_minutes`.
4. **Scale-out targets** — `reduce` на milestone-PnL.
5. **Trailing stop** — `close` коли ціна провалилась нижче
   `peak × (1 - trailing_stop_bps/10000)`.
6. **Near-resolution exit** — `close` якщо < N годин до резолюції
   й spread розширився.
7. **Take profit** — `close` при `pnl_pct ≥ take_profit_pct`.
8. **Stop loss** — `close` при `pnl_pct ≤ -stop_loss_pct`.

**Дефолти стратегії copy-trade не встановлюють** `take_profit_pct`,
`stop_loss_pct`, `max_hold_minutes`. Це означає, що позиції копі-трейду
тримаються до резолюції ринку (або поки оператор не вийде вручну /
не виставить ці поля через **Tune** вкладку). Trailing stop за
замовчуванням `80 bps` з `ScaleOutConfig`, але працює тільки якщо
`peak_price` піднявся вище entry — інакше рівень нижче входу.

`should_exit` зовуть:

- кожні **30 секунд** з `trader_reconciliation_worker`
  (`position_lifecycle.reconcile_live_positions`);
- щоцикл orchestrator-у для позицій бота.

## Дефолти, які треба перекрити перед live

| Поле | Default | Recommended live | Чому |
|---|---|---|---|
| `max_signal_age_seconds` | 5 | 5–15 | На leader зі стабільним flow можна тримати 5; для волатильних → 10–15. |
| `max_adverse_entry_drift_pct` | 2 | 1–3 | 2% на binary ринку — це багато; знижувати агресивно. |
| `max_entry_price` | 0.98 | 0.85–0.92 | Tail mean-reverting. |
| `max_copy_drawdown_pct` | **100** | **5–15** | Без цього drawdown не контролюється стратегією. |
| `max_copy_daily_loss_usd` | **1_000_000** | **100–500** | Те саме. |
| `max_leader_exposure_usd` | **1_000_000** | **50–200** | Per-leader cap. |
| `max_copy_source_exposure_usd` | **1_000_000** | **300–1000** | Total exposure cap. |
| `max_position_size` | 1000 | 25–100 | Стратегічна стеля; орбітальний `risk_limits.max_position_notional_usd` зазвичай ще нижчий. |

Плюс орбітальні поля з `risk_limits_json` — див.
[спільний документ](_common-bot-parameters.md#risk_limits_json--повна-схема).

## Коли НЕ працює

- **Leader не має edge.** Wallet discovery ранжує за історичним
  P&L, але це часто mean-reverting. Periodically переоцінюйте
  `discovered_wallets.in_top_pool` через UI Discovery / Wallets.
- **Front-running.** Якщо leader сам великий, ваша copy-trade
  входить після нього, і market вже з'їв edge. У логах це буде
  `entry_drift` skip або негативні стопи.
- **Insider trading.** Деякі leader-и — це інсайдери, що
  закінчиться clawbacks. Insider detector
  (`services/insider_detector.py`, 27-point) частково попереджає;
  але якщо wallet попав у ваш scope — copy виконається.
- **Stacking.** При нашому default-режимі (`allow_averaging=false`)
  бот, який вже сидить на ринку, не зможе додати позицію навіть
  якщо leader-и підтверджують — `Stacking guard` skip буде
  основним джерелом noise.
- **`max_open_orders` / `max_open_positions` clamp.** Дивіться
  [спільний документ → Live-risk clamps](_common-bot-parameters.md#live-risk-clamps-одержують-пріоритет-над-risk_limits) — фактична стеля може бути нижчою за
  ваш `risk_limits`-конфіг.
- **Дефолтні `max_copy_*`-поля = no-op.** Якщо забули виставити
  перед live — копі-стратегія не має власного drawdown-контролю
  (тільки orchestrator-level).

## Як швидко знайти, що блокує конкретний бот

Алгоритм:

1. UI → бот → **Decisions** → фільтр **Blocked**, потім **Skipped**
   (порівняйте лічильники).
2. Якщо домінує `Risk blocked: trader_open_orders ... max=N` →
   впираємось у max_open_orders. Перевірити
   `app_settings.global_runtime.live_risk_clamps.max_open_orders_cap`
   (див. [спільний документ](_common-bot-parameters.md#live-risk-clamps-одержують-пріоритет-над-risk_limits)).
3. Якщо домінує `Stacking guard: market already occupied` →
   або підняти `allow_averaging=true` (тільки в shadow), або
   перевірити, скільки в боті відкритих позицій / pending-ордерів
   (`trader_positions`, `trader_orders`).
4. Якщо `copy_trade_gate_failed:min_notional` → leader-и ловлять
   мікро-трейди, нижче `min_source_notional_usd`. Знизити, якщо
   так і задумано.
5. Якщо `copy_trade_gate_failed:entry_drift` → ринок різко рухається
   між leader-fill і вашим evaluate. Або підняти
   `max_adverse_entry_drift_pct`, або зменшити
   `max_signal_age_seconds`/`copy_delay_seconds`, щоб ловити свіжіші.
6. Якщо `copy_trade_gate_failed:size_floor` (`target_size < 1.0`) —
   або `leader_weight` занадто маленький, або
   `proportional_multiplier`, або
   `leader_allocation_cap_pct`, або capacity-ліміти ріжуть до нуля.
7. Якщо `copy_trade_gate_failed:max_age` → leader signal приходить
   із затримкою (ws-feed або queue lag).
   `traders_copy_trade_signal_service._processor_loop` контестується;
   подивіться `worker-trading` логи.
8. Якщо `Market data freshness gate (age>Xms)` → ws-quote
   застряг для цього токена. Це загальний health-issue, не
   стратегія.

SQL-рецепти у [`_common-bot-parameters.md` → SQL-рецепти для
діагностики](_common-bot-parameters.md#sql-рецепти-для-діагностики).

## Посилання

- [`_common-bot-parameters.md`](_common-bot-parameters.md) — спільні
  bot-поняття: risk-limits, decision-gates, UI-вкладки, SQL.
- [Traders Confluence](traders-confluence.md) — aggregated сигнал з
  ≥ 2 wallets, кращий signal-to-noise.
- [`docs/plans/architecture/copy-trade-pipeline.md`](../plans/architecture/copy-trade-pipeline.md)
  — канонічний end-to-end pipeline для `source='traders'`,
  включно з історією plan 0008/0009 (дефер-гейт).
- [`docs/plans/architecture/trader-pipeline.md`](../plans/architecture/trader-pipeline.md)
  — generic flow signal → order для всіх traders-source стратегій.
- [`docs/plans/architecture/wallet-intelligence.md`](../plans/architecture/wallet-intelligence.md)
  — wallet scoring, insider/anomaly detection (Discovery UI backbone).
- [`docs/plans/architecture/worker-trading.md`](../plans/architecture/worker-trading.md)
  — process model, fast vs normal latency tier.
