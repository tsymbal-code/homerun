# Temporal Decay

## Сутність

Mean-reversion гіпотеза проти **sqrt-time decay curve**: ціна
будь-якого ринку в нормі затухає до 0 або до 1 у функції `expected
= initial × (days_remaining / total_days)^0.5`. Якщо реальна ціна
відхиляється від цієї кривої більше ніж на 7% — стратегія ставить
на повернення.

Свідомо позначена як **слабкий сигнал** у docstring-у: модель
heuristic, не має теоретичної бази, грає на noise, який ринок
іноді коректує.

## Контракт

- **Файл**: [`backend/services/strategies/temporal_decay.py`](../../backend/services/strategies/temporal_decay.py)
- **Клас**: `TemporalDecayStrategy`
- **slug**: `temporal_decay`
- **source_key**: `manual`
- **Subscriptions**: `EventType.MARKET_DATA_REFRESH`
- **Ключові входи**: ціни, price history (≥ 8 points), deadline з
  тексту питання або `end_date`

## Логіка детекції

1. Extract deadline: parse from question text або use `end_date`.
2. Maintain price history ≥ 8 points.
3. Compute total_days, days_remaining.
4. Fit decay curve:
   `expected_price = initial_price × (days_remaining / total_days)^0.5`.
5. Detect gap: `|current - expected| ≥ min_deviation=0.07`.
6. Direction = OPPOSITE до gap-у (ціна вище expected → buy NO).
7. Compute repricing target = mid-point між current і expected.
8. **Min expected move ≥ 4¢**.
9. **Realistic ROI cap**: 30% — anti-fantasy gate.
10. Excludes crypto markets за keyword.

## Логіка виходу

TP 10%, SL 5%, trailing 7%. Heuristic confidence — не тримати довго.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_edge_percent` | `5.0` | |
| `min_confidence` | `0.50` | |
| `max_risk_score` | `0.70` | |
| `max_days_to_deadline` | `30.0` | |
| `min_days_to_deadline` | `1.0` | |
| `min_deviation` | `0.07` | Min gap від decay curve |
| `min_history_points` | `8` | |
| `decay_rate` | `0.5` | sqrt-time exponent |
| `min_entry_price` | `0.10` | |
| `max_entry_price` | `0.90` | |
| `min_expected_move` | `0.04` | |
| `min_liquidity_hard` | `2000.0` | |
| `min_position_size` | `50.0` | |
| `max_realistic_roi_pct` | `30.0` | Sanity gate |
| `take_profit_pct` | `10.0` | |
| `stop_loss_pct` | `5.0` | |
| `trailing_stop_pct` | `7.0` | |
| `exclude_market_keywords` | `[bitcoin, btc, ...]` | Crypto exclusion |

## Коли НЕ працює

- **Heuristic не має фундаменту**. Sqrt-time decay — це
  pseudo-Brownian аналогія. Реальні ринки не decay-ять монотонно
  (є news, regime-shifts). Treat як weak signal, малі sizes.
- **Resolved-but-stale ринки**. Виглядають як «deviation», але це
  [Settlement Lag](settlement-lag.md) — інший edge.
- **Крайні ціни** (0.05 / 0.95). Decay curve тут перетинає ваш
  entry-range, і стратегія не входить by design.
- **Невелика історія**. min 8 points — груба перевірка. На ринках
  з low-trade-flow історія може бути 8 точок за тиждень, що для
  decay-fit недостатньо.

## Посилання

- [Certainty Shock](certainty-shock.md) — окрема гіпотеза для
  near-deadline руху.
- [Stat Arb](stat-arb.md) — ensemble-альтернатива для weak signals.
- [Settlement Lag](settlement-lag.md) — окрема гіпотеза для
  resolved-stale ринків.
