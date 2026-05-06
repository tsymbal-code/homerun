# Probability Surface Arbitrage

## Сутність

Сім'ї ринків зі threshold-axis (наприклад, «BTC > $50K», «BTC > $60K»,
«BTC > $70K» на ту саму дату) мають **монотонне** обмеження:
ймовірність «BTC > $50K» завжди ≥ ймовірності «BTC > $60K». Якщо
ринкові ціни порушують монотонність — це арбітраж: купити
underpriced поріг, фейдити overpriced. Стратегія fits isotonic
regression (PAVA — pool adjacent violators algorithm) і трейдить
відхилення від цієї згладженої кривої.

> **Не безризиковий**. Якщо порушення монотонності правдиве (рідкісні
> regime-changes), стратегія програє. Edge — у припущенні, що ринок
> правильно називає shape, помилково ставить рівні.

## Контракт

- **Файл**: [`backend/services/strategies/prob_surface_arb.py`](../../backend/services/strategies/prob_surface_arb.py)
- **Клас**: `ProbSurfaceArbStrategy`
- **slug**: `prob_surface_arb`
- **source_key**: `scanner`
- **Subscriptions**: `EventType.MARKET_DATA_REFRESH`
- **Ключові входи**: ринки, що належать одному event-у з threshold-
  екстракцією зі slug-а / питання, поточні YES-ціни, depth, fee curve

## Логіка детекції

1. Групує ринки за event_id + threshold-axis. Threshold-екстракція з
   тексту (BTC > $X, score > N, votes > Y).
2. Перевіряє `min_family_size=3` — менше за 3 порогів shape не
   надійний.
3. Sorts by threshold ascending; fits isotonic regression (PAVA).
4. Detects deviation: `|market_price - fitted_prob| ≥ min_deviation_cents`
   (3¢) AND deviation ≥ `min_deviation_spread_multiple * spread`
   (1.2× — щоб не торгувати на noise всередині bid-ask).
5. Trade size розраховується так, щоб repricing-target (відновлення
   monotonicity) дав мінімум `min_edge_percent=2.0%`.

## Логіка виходу

Default TP/SL — стандартні 12% TP. Можна тримати або вийти, коли
deviation скорочується на 70%.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_edge_percent` | `2.0` | |
| `min_confidence` | `0.50` | |
| `max_risk_score` | `0.75` | |
| `min_family_size` | `3` | Min ринків у threshold-сім'ї |
| `min_deviation_cents` | `0.03` | Min гэп від isotonic curve |
| `min_liquidity` | `1000.0` | |
| `max_days_to_resolution` | `2.0` | Тільки short-dated (де shape стабільний) |
| `max_opportunities` | `20` | |
| `max_spread_cents` | `0.04` | Max spread для прийнятної ноги |
| `min_deviation_spread_multiple` | `1.2` | Deviation має перевищувати spread ×1.2 |
| `require_live_quote` | `True` | Лише з активним CLOB |
| `max_signal_age_seconds` | `5.0` | Сигнал — не старіший за 5 с |

## Коли НЕ працює

- **Regime-change**. Якщо ринок дізнався щось нове (наприклад, після
  earnings-call падає BTC — і ринок «BTC > $50K» миттєво падає, а
  «BTC > $40K» ще ні), це справжня дисконтинуальність. Stretchy
  isotonic-fit вирівняє це — а ваш «арбітраж» стане маршовим лонгом
  у зворотний бік.
- **Великий рознос дат у сім'ї**. Стратегія не має explicit-гарду
  на mixed резолюцію в межах family-у. Перевіряйте, що всі
  threshold-ринки в групі резолвяться однаково.
- **Дрібна liquidity на хвостах**. Низькі / високі threshold-ринки
  («BTC > $200K») мають тонший ордер-бук — `min_liquidity=$1000`
  може бути замало.
- **Дискретна threshold-вісь**. Стратегія припускає неперервний
  threshold-axis. Для дискретних виборів (виборчий vote count
  buckets) — це не зовсім isotonic-задача.

## Посилання

- [Combinatorial Arb](combinatorial.md) — узагальнення на довільні
  залежності, не тільки threshold-monotonic.
- [NegRisk Bundle Arb](negrisk.md) — для exclusive-наборів, не
  cumulative.
