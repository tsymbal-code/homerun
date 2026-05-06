# Traders Copy Trade

## Сутність

Real-time mirror: коли tracked-wallet робить trade, стратегія
**копіює його** — той самий market, та ж сторона, пропорційний size.
На відміну від [Traders Confluence](traders-confluence.md), яка
агрегує ≥ 2 wallet-сигнали в один opportunity, copy-trade відкриває
позиції за **одним leader-wallet**.

Це найбільш raw-form behavioral edge: ставимо на те, що конкретний
гаманець знає більше за market. Risk control — `leader_weights`,
`max_leader_exposure_usd`, drawdown caps, daily loss caps.

## Контракт

- **Файл**: [`backend/services/strategies/traders_copy_trade.py`](../../backend/services/strategies/traders_copy_trade.py)
- **Клас**: `TradersCopyTradeStrategy`
- **slug**: `traders_copy_trade`
- **source_key**: `traders`
- **Subscriptions**: async on_event / `DataEvent.trader_activity`
- **Ключові входи**: live trade-events від tracked-wallets
  (token_id + side + price + size), wallet scope, live market data
  (для drift / liquidity check)

## Логіка детекції

На кожен `trader_activity`-event:

1. Validate: `token_id`, `direction` (buy/sell), `size`, `price`
   присутні.
2. **Signal freshness**: ≤ `max_signal_age_seconds=5` (тут жорсткий
   gate — copy-trade на 30-секундному сигналі марний).
3. **Entry drift**: live ціна не далі за 2% від ціни leader-trade-у.
4. **Liquidity**: live market liquidity ≥ `min_live_liquidity_usd=$150`.
5. **Inventory check** (для sells): має бути ≥ `min_inventory_fraction
   = 25%` від leader-розміру в нашій позиції; інакше — partial-sell
   або skip.
6. **Risk budgets**:
   - `max_copy_drawdown_pct` — максимальний drawdown по copy-стратегії.
   - `max_copy_daily_loss_usd` — daily loss cap.
   - `max_copy_source_exposure_usd` — exposure на одного source-leader.
   - `leader_weights` × `leader_allocation_cap_pct` — per-leader
     allocation у портфелі.
7. **Sizing**: `proportional_sizing=True` × `proportional_multiplier
   =1.0` → копіюємо size leader-а 1:1, обмежено `max_position_size=
   $1000`.

Edge формула: `|entry_price - 0.5| × 200`. Тобто екстремальніша
ціна → вища confidence, що leader має edge.

## Логіка виходу

Стандартний TP/SL. Copy-trades inherently risky (leader може теж
помилятися, slippage між leader і нашим entry, stale-signal).

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_confidence` | `0.45` | |
| `min_source_notional_usd` | `10.0` | Мінімальний leader-trade розмір |
| `max_entry_price` | `0.98` | |
| `max_signal_age_seconds` | `5` | Жорсткий gate |
| `min_live_liquidity_usd` | `150.0` | Мінімум для безпечного fill |
| `max_adverse_entry_drift_pct` | `2.0` | Drift від leader-ціни |
| `copy_delay_seconds` | `0` | Без затримки за замовчуванням |
| `copy_buys` | `True` | |
| `copy_sells` | `True` | |
| `max_position_size` | `1000.0` | Cap per position |
| `proportional_sizing` | `True` | |
| `proportional_multiplier` | `1.0` | 1:1 sizing |
| `max_copy_drawdown_pct` | `100.0` | Дефолт без cap (operator має задати) |
| `max_copy_daily_loss_usd` | `1_000_000.0` | Дефолт без cap |
| `max_copy_source_exposure_usd` | `1_000_000.0` | Дефолт без cap |
| `default_leader_weight` | `1.0` | Per-leader weight (override через `leader_weights`) |
| `leader_allocation_cap_pct` | `100.0` | Max % портфеля на leader |
| `require_inventory_for_sells` | `True` | |
| `allow_partial_inventory_sells` | `True` | |
| `min_inventory_fraction` | `0.25` | |
| `traders_scope` | `{modes: [tracked, pool], individual_wallets: [], group_ids: []}` | Хто вважається leader-ами |

## Коли НЕ працює

- **Leader не має edge**. Wallet discovery ранжує за історичним P&L,
  але це часто mean-reverting. Consistently переглядайте leaderboard.
- **Front-running**. Якщо leader сам великий, ваша copy-trade входить
  після нього, slippage заїдає edge.
- **Insider trading**. Деякі tracked-wallets — це insider-ів, що
  закінчиться bans / clawbacks. Insider detector (27-point) частково
  попереджає.
- **Risk-budget defaults**. `max_copy_drawdown_pct=100`,
  `max_copy_daily_loss_usd=1_000_000` — це effectively no cap.
  Operator **обов'язково** має виставити realistic-значення перед
  live-режимом.

## Посилання

- [Traders Confluence](traders-confluence.md) — aggregated сигнал, з
  кращим signal-to-noise.
- Wallet pool / discovery: див. UI вкладки Traders → Pool / Tracked.
- Анти-бот фільтрація: `services/insider_detector.py`,
  `wallet_intelligence.py`.
