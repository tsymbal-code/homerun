# Tail-End Carry

## Сутність

«Carry-trade» на high-probability outcomes near deadline. Якщо ціна
85–90% (`sports_min_probability=90%`), до резолюції залишилось менше
доби, liquidity достатня, spread тонкий — то ймовірність переможної
ноги стискається до $1, і ми отримуємо 10–18% upside за короткий час.

Найбільш «сторожкова» зі стратегій: десятки гейтів проти live games,
spread markets, panic-drops, exotic-esports. Stratery, яку оператори
найчастіше калібрують перед live-режимом.

## Контракт

- **Файл**: [`backend/services/strategies/tail_end_carry.py`](../../backend/services/strategies/tail_end_carry.py)
- **Клас**: `TailEndCarryStrategy`
- **slug**: `tail_end_carry`
- **source_key**: `manual`
- **Subscriptions**: `EventType.MARKET_DATA_REFRESH`
- **Ключові входи**: ціни, game timestamps, bid-ask spreads, recent
  price-history window для panic-detection

## Логіка детекції

Багатоступеневі гейти (всі мають пройти):

1. **Probability**: 0.85 ≤ P ≤ 0.905 (sports: ≥ 0.90).
2. **Time-to-resolution**: 0 ≤ days ≤ 1.0 (sports: ≤ 0.25 = 6 годин).
3. **Min upside**: ≥ 10% (тобто від 0.85 → 0.935+).
4. **Liquidity**: ≥ $1500.
5. **Spread**: ≤ 5%.
6. **Repricing buffer**: ≥ 1.5% — захист від «P уже на верхній межі».
7. **Panic-drop guard**: останні 6 точок історії не показують
   > 8% drop із slow-recovery (`panic_recovery_ratio_max=0.20`).
8. **Spread markets blocked** (handicap, point-spread).
9. **Live games skipped** (`skip_live_games=True`,
   `live_game_buffer_minutes=15`) — не входити в активний матч.
10. **Esports keyword exclude**: LoL, CS, Dota, Valorant, esports —
    через високу volatility.

## Логіка виходу

Складна exit-policy:

- **Resolution hold**: 360 хв (6 годин) для regular, 150 хв для
  sports — навіть якщо TP не зачепили, тримаємо до самої резолюції.
- **Inversion stop** at 50%: якщо ціна перелетіла нижче 50%,
  закриваємо (sports: disabled, бо game-flow може повернути).
- **Trailing stop**: 12% (sports: 30% — ширше, бо in-game
  volatility).
- **Smart take-profit**: TP кикає тільки якщо PnL ≥ 10% AND
  remaining headroom < 3¢ (тобто ціна вже близько до $1).
- **Immediate break-even stop**: ввімкнено, buffer 0.5%.
- **Max hold**: 1440 хв (24 години).

## Налаштування за замовчуванням

Operator-relevant ключі (повний `default_config` має 50+ ключів):

| Ключ | Значення | Сенс |
|---|---|---|
| `min_probability` | `0.85` | |
| `max_probability` | `0.905` | |
| `sports_min_probability` | `0.90` | |
| `min_upside_percent` | `10.0` | |
| `sports_max_days_to_resolution` | `0.25` | 6 годин |
| `min_days_to_resolution` | `0.0` | |
| `max_days_to_resolution` | `1.0` | |
| `min_liquidity` | `1500.0` | |
| `max_spread` | `0.05` | |
| `min_repricing_buffer` | `0.015` | |
| `repricing_weight` | `0.45` | |
| `block_spread_markets` | `True` | |
| `panic_drop_threshold` | `0.08` | |
| `panic_window_points` | `6` | |
| `panic_recovery_ratio_max` | `0.20` | |
| `take_profit_pct` | `10.0` | |
| `smart_take_profit_enabled` | `True` | |
| `smart_take_profit_min_pnl_pct` | `10.0` | |
| `smart_take_profit_max_price_headroom` | `0.03` | |
| `inversion_stop_enabled` | `True` | |
| `inversion_price_threshold` | `0.50` | |
| `trailing_stop_enabled` | `True` | |
| `trailing_stop_pct` | `12.0` | |
| `sports_inversion_stop_enabled` | `False` | |
| `sports_trailing_stop_pct` | `30.0` | |
| `sports_sizing_multiplier` | `0.45` | Sub-Kelly для sports |
| `skip_live_games` | `True` | |
| `live_game_buffer_minutes` | `15.0` | |
| `resolution_hold_enabled` | `True` | |
| `resolution_hold_minutes` | `360.0` | |
| `sports_resolution_hold_minutes` | `150.0` | |
| `resolution_hold_max_loss_pct` | `25.0` | Cap на losses при hold |
| `max_hold_minutes` | `1440.0` | 24 години |
| `price_policy` | `taker_limit` | |
| `time_in_force` | `IOC` | |
| `immediate_break_even_stop_enabled` | `True` | |
| `immediate_break_even_stop_buffer_pct` | `0.5` | |
| `max_market_data_age_ms` | `15000` | 15 секунд stale-cutoff |
| `require_strict_ws_pricing` | `True` | |
| `strict_ws_price_sources` | `[ws_strict, redis_strict]` | |

## Коли НЕ працює

- **Disputes after resolution**. UMA-disputes можуть скасувати
  outcome navit після формального завершення матчу. `resolution_hold_max_loss_pct
  =25%` обмежує downside, але не повністю.
- **Tail-events**. 90% probability ≠ 100%. Раз на 10 trades втрата
  буде ~$0.85. Sub-Kelly sizing (`sports_sizing_multiplier=0.45`)
  для sports важливий.
- **Volatility regime change**. Один матч in-overtime може швидко
  flip 90% → 50%. `inversion_stop` для non-sports закриває; для
  sports disabled — у них ширший trailing.
- **Stale price gate**. `max_market_data_age_ms=15s` суворий —
  іноді WS-feed lag-ить, і стратегія skip-ить opportunity. Це by
  design.

## Посилання

- [Certainty Shock](certainty-shock.md) — споріднена high-prob
  гіпотеза, але з shock-вимогою.
- [Sports Overreaction Fader](sports-overreaction-fader.md) —
  reverse hypothesis для sports overreactions.
- [Manual Manage Hold](manual-manage-hold.md) — кінцева управлінська
  стратегія, якщо позиція вже відкрита.
