# BTC/ETH Convergence

## Сутність

Polymarket пропонує бінарні «up-or-down» ринки на BTC/ETH/SOL/XRP, що
резолвляться кожні 5/15/60/240 хв за Chainlink-оракулом проти
зафіксованої «price-to-beat». У вікні **5–45 секунд до резолюції**
(коли решту таймера ринок уже відіграв) ціни обох сторін збігаються
до $1/$0. Convergence ставить **post-only maker-ордер на сторону
переможця** на рівні $0.85–$0.95: якщо ордер заповнюється, прибуток —
стискається спред у $1; якщо не заповнюється — він просто скасовується
без втрат (post-only = 0% taker fee на Polymarket).

## Контракт

- **Файл**: [`backend/services/strategies/btc_eth_convergence.py`](../../backend/services/strategies/btc_eth_convergence.py)
- **Клас**: `BtcEthConvergenceStrategy`
- **slug**: `btc_eth_convergence`
- **source_key**: `crypto`
- **Subscriptions**: `EventType.CRYPTO_UPDATE`
- **Ключові входи**: Chainlink-оракул, price-to-beat (зафіксований
  на market-open), market regime (opening / mid / closing),
  cycle end timestamp, slug-regex для timeframe-detection

## Логіка детекції

На кожен `CRYPTO_UPDATE`:

1. Ідентифікує тип ринку: BTC/ETH/SOL/XRP up-or-down з 5m/15m/1h/4h
   timeframe-ом.
2. Обчислює час до резолюції (`cycle_end - now`). Якщо в межах 5–45 с —
   йде далі.
3. Порівнює свіжий Chainlink price з price-to-beat. Сторона
   «переможця» = напрям, у який рухається оракул.
4. Розраховує post-only ціну: `entry_price ∈ [0.85, 0.95]`,
   адаптивно за regime (раніше — нижче, ближче до резолюції — вище).
5. Перевіряє gates: оракул-вік ≤ 2000 ms, regime threshold confidence,
   min_edge ~1.5%.
6. Виставляє post-only buy на стороні переможця. Якщо filled — продано
   на $1 при settlement; якщо unfilled — order скасовано.

## Логіка виходу

Specific: post-only maker не вимагає `should_exit` як такого —
позиція автоматично закривається на settlement через CTF-резолвер.
Якщо filled, тримаємо до resolution. Якщо ні — ордер видаляється.

## Налаштування за замовчуванням

Параметри regime-scaled (opening / mid / closing). Operator-relevant:

| Ключ | Значення | Сенс |
|---|---|---|
| `assets_enabled` | `[BTC, ETH, SOL, XRP]` | Які активи трейдити |
| `entry_price_min` | `0.85` | Нижня ціна post-only |
| `entry_price_max` | `0.95` | Верхня ціна post-only |
| `min_edge_percent` | `~1.5%` | Min очікуваний прибуток |
| `max_oracle_age_ms` | `2000` | Старіший оракул не використовується |
| `min_seconds_to_resolution` | `5` | Min час до settlement |
| `max_seconds_to_resolution` | `45` | Max час (раніше — direction ще не сталий) |

## Коли НЕ працює

- **Оракул запізнюється або падає**. Якщо Chainlink lag-ить, наша
  «прогноз переможця» — застарілий.
- **Sudden reversal у останні 5 секунд**. У 5-хв ринку це рідкість,
  у 4-год — частіше; для great-volatility-моментів (наприклад, FOMC)
  відключайте відповідний timeframe.
- **Order book порожній на стороні переможця**. Post-only-ордер
  стане best-bid сам, але якщо ніхто не taker-ить, fill не настає.
- **Polymarket WS user channel exclusivity**. Стратегія працює тільки
  у `worker-trading`; запуск двох trading-плейнів дропне обидва
  WS-сесії.

## Посилання

- [BTC/ETH Directional Edge](btc-eth-directional-edge.md) — taker-варіант
  з більш суворим gate на edge.
- [BTC/ETH Maker Quote](btc-eth-maker-quote.md) — двостороннє maker
  quoting замість одностороннього convergence.
- [Crypto 5m Midcycle](crypto-5m-midcycle.md) — той самий ринок, але
  на 2:30 циклу замість last-minute.
