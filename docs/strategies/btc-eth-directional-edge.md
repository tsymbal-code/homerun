# BTC/ETH Directional Edge

## Сутність

Taker-варіант [BTC/ETH Convergence](btc-eth-convergence.md). Замість
post-only-ордера на $0.85–$0.95, ця стратегія заходить **IOC-taker-ом**
на сторону, куди дивиться оракул, **тільки якщо edge перевищує
2× taker-fee**. Це fee-aware гейт: edge має бути достатнім, щоб
покрити taker-комісію в обидві сторони (вхід + вихід) і ще лишити
прибуток. Сильніше за convergence чуттєвий до точності оракула.

## Контракт

- **Файл**: [`backend/services/strategies/btc_eth_directional_edge.py`](../../backend/services/strategies/btc_eth_directional_edge.py)
- **Клас**: `BtcEthDirectionalEdgeStrategy`
- **slug**: `btc_eth_directional_edge`
- **source_key**: `crypto`
- **Subscriptions**: `EventType.CRYPTO_UPDATE`
- **Ключові входи**: Chainlink-оракул (real-time), price-to-beat
  на market-open, taker fee curve per entry price, regime-aware
  thresholds

## Логіка детекції

На кожен `CRYPTO_UPDATE`:

1. Filter: BTC або ETH up-or-down (`assets=[BTC,ETH]` дефолт),
   timeframe з певним `min_seconds_left`.
2. Compute oracle vs price-to-beat divergence.
3. **Fee-aware edge gate**: `edge ≥ 2 × taker_fee_at_entry`.
   Taker-fee curve залежить від ціни — вищі ціни → менші абсолютні
   fee, але і менший простір для edge.
4. Regime-scaled min_edge: `1.5%` early-cycle, `1.25%` mid-cycle,
   `1.1%` late-cycle (ближче до резолюції — менший edge достатній,
   бо менше часу на reversal).
5. IOC-taker-ордер на сторону переможця. Single directional position,
   single fill або скасування.

## Логіка виходу

IOC-taker = entry only. Position default exit-strategy — закриття за
TP/SL з конфігу або hold to resolution.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `assets` | `[BTC, ETH]` | Дефолтно без SOL/XRP (вища noise/lag) |
| `min_edge_early` | `1.5%` | Early-cycle threshold |
| `min_edge_mid` | `1.25%` | Mid-cycle |
| `min_edge_late` | `1.1%` | Late-cycle |
| `entry_type` | `IOC` taker | Не залишається в order book |
| `max_oracle_age_ms` | `2000` | Старіший оракул skip |
| `min_seconds_left` | varies by timeframe | Min час до резолюції |

## Коли НЕ працює

- **Оракул != market truth**. Chainlink на Polymarket — relayed,
  іноді з затримкою 500ms–2s. Для 5-хв ринку це 1–8% від cycle-у;
  стратегія легко може помилитися напрямом. Connecting Binance
  direct feed (див. `BinanceFeed`) знижує latency.
- **Edge < 2× fee**. Стратегія спокійно skip-ить більшість тиків —
  це by design. Якщо вам здається, що вона «нічого не робить» — це
  в нормі при стабільному ринку.
- **Reverse-correlated regime**. Іноді (наприклад, на новини) ринкова
  ціна випереджає оракул — і ваш taker-trade входить пізно. Шукайте
  «entry latency» у `ExecutionLatencyMetrics`.
- **Sub-second reversal**. У 5-хв ринку 30 c з 300 — це 10% циклу;
  у останні секунди volatility може перекинути виграшний бік.

## Посилання

- [BTC/ETH Convergence](btc-eth-convergence.md) — maker-варіант,
  безпечніше, але fill-rate нижчий.
- [BTC/ETH Maker Quote](btc-eth-maker-quote.md) — двостороннє quoting.
- Latency-моніторинг: `services/execution_latency_metrics.py`
  (rolling p50/p95/p99 через 9 pipeline-стадій).
