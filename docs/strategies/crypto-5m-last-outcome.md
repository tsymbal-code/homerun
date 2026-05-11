# Crypto 5m Last-Outcome Follow

## Сутність

Найпростіша directional crypto-стратегія. На кожному новому
5-хвилинному Polymarket up-or-down циклі для увімкненого активу
**відкриваємо позицію на ту саму сторону, що виграла попередній
цикл**. Жодних oracle-distance, microstructure чи edge-фільтрів — лише
ті, без яких саме виконання неможливе (5m timeframe, asset у списку,
VWAP у заданому діапазоні, наявність свіжого order-book-depth для
обраної сторони).

Результат попереднього циклу обчислюється без необхідності ловити
закриваючий Chainlink-tick. Polymarket встановлює `price_to_beat`
нового циклу = Chainlink-ціна на момент старту циклу = Chainlink на
момент закриття попереднього. Тому щойно ми бачимо market з новим
`condition_id` для того ж активу:

```
prev_outcome = "YES" if price_to_beat_new > price_to_beat_old else "NO"
```

Перший цикл після cold-boot (або після довгої перерви) свідомо
пропускається — попередній результат невідомий, нема за чим
повторювати.

## Контракт

- **Файл**: [`backend/services/strategies/crypto_5m_last_outcome.py`](../../backend/services/strategies/crypto_5m_last_outcome.py)
- **Клас**: `Crypto5mLastOutcomeStrategy`
- **slug**: `crypto_5m_last_outcome`
- **source_key**: `crypto`
- **Subscriptions**: `EventType.CRYPTO_UPDATE`
- **Ключові входи**: `price_to_beat` поточного циклу (для виявлення
  rollover), market `condition_id`, end timestamp, VWAP з
  order-book depth (через StrategySDK), slug-regex для 5m-detection

## Логіка детекції

На кожен `CRYPTO_UPDATE` для кожного market у payload:

1. Це 5m-timeframe (slug-regex / `timeframe` поле).
2. `condition_id` + `end_time` парсаться.
3. Asset enabled у конфізі (default — лише BTC).
4. `price_to_beat` присутній і > 0.
5. **Оновлюється per-asset state**: якщо `condition_id` змінився
   відносно збереженого — попередній цикл щойно завершився,
   обчислюємо `prev_outcome` зі знаку `Δ price_to_beat`.
6. CycleTracker emit-ить milestone на **30-й секунді** циклу (default
   `entry_seconds_after_start=30`) — це дає WS-feed-у час
   заповнити cache.
7. `prev_outcome` має бути відомим (YES або NO).
8. Order-book depth для обраної сторони доступна та свіжа.
9. VWAP entry у межах `[min_entry_price, max_entry_price]`.

Side = `prev_outcome`. Entry = VWAP. Target = $1.00 (наслідування).
Розмір позиції — fixed `bet_size_usd=$15`.

## Логіка виходу

Single directional entry. Default exit-policy успадковується від
trader-binding-у (TP/SL з конфігу або hold to settlement). Стратегія
сама `should_exit` не реалізує.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `assets` | `[BTC]` | Дефолтно лише BTC — стартовий актив для оператора |
| `entry_seconds_after_start` | `30.0` | Скільки секунд від початку циклу чекати перед entry |
| `max_entry_price` | `0.95` | Max VWAP — захист від купівлі на 0.97+ |
| `min_entry_price` | `0.05` | Min VWAP — захист від degenerate fills |
| `bet_size_usd` | `15.0` | Fixed sizing — не Kelly |

`win_prob_estimate` усередині стратегії — **0.50** (coin-flip prior),
бо емпіричних даних ще нема. ROI / risk у firehose рахуються від
цього neutral prior; коли накопичиться live-PnL, prior можна
підняти.

## Коли НЕ працює

- **Перший цикл / cold start**. Стратегія тиха один цикл (300 с)
  після старту worker-trading. Це by design.
- **Markov-припущення хибне**. Логіка вважає, що результат
  попереднього циклу несе сигнал про наступний. У статистично-
  ефективному ринку це не так — стратегія тоді стане coin-flip
  мінус slippage. Запускайте у shadow і дивіться, чи win-rate
  стійко > 50%.
- **Rollover із рівним `price_to_beat`**. Якщо Polymarket
  округлив `price_to_beat` двох сусідніх циклів до однієї цифри —
  outcome ambiguous, стратегія пропускає той цикл.
- **VWAP > 0.95 (наприклад coupling із convergence-флоу)**. Гейт
  `vwap_in_range` зрізає, бо edge відсутній. Підняти `max_entry_price`
  можна через UI, але економічний сенс зникає швидко.
- **Регуляторні / oracle-проблеми**. Якщо Polymarket вирішить
  цикл не за Chainlink, або Chainlink дасть стрибок — наша
  оцінка outcome зіб'ється на цей один rollover. Стратегія
  само-коректується на наступному циклі.
- **Stale book**. `book_fresh` гейт зрізає, поки cache не
  оновився. На холодному cache буває довгий стрімінг — для цього є
  `_ensure_ws_subscribed_for_5m` (як у midcycle).

## Як це працює разом із midcycle

Midcycle і last-outcome — **дві ортогональні гіпотези на тому самому
активі/timeframe**:

| | Midcycle | Last-Outcome |
|---|---|---|
| Сигнал | Chainlink ≠ reference на 150-й сек | Сторона переможця попереднього циклу |
| Гейти | distance, oracle, VWAP, depth | VWAP, depth (мінімум) |
| Тайминг | 2:30 у цикл | 30 c у новий цикл |
| Cold-start | Працює одразу | Чекає 1 цикл |

Запуск у shadow паралельно дасть змогу порівняти, чи continuation-
сигнал (midcycle) або «momentum-of-outcomes» (this strategy) дає
кращий win-rate на конкретному ринку.

## Посилання

- [Crypto 5m Midcycle](crypto-5m-midcycle.md) — continuation-гіпотеза
  з фільтрами.
- [Crypto Spike Reversion](crypto-spike-reversion.md) — fade-гіпотеза
  на коротких 1.8%+ spike-ах.
- Plan: [`0047-crypto-5m-last-outcome-follow-strategy`](../plans/completed/0047-crypto-5m-last-outcome-follow-strategy.md).
