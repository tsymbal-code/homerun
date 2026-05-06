# Traders Confluence

## Сутність

«Smart money» convergence: коли ≥ 2 tracked-гаманці одночасно
відкривають позиції в одному напрямку на одному ринку, це сигнал, що
вони бачать спільну інформацію, недоступну market-у. Стратегія
агрегує сигнали від wallet-discovery + tracked traders і фіксує
opportunity, коли confluence перевищує поріг.

Це **behavioral edge**, не структурний. Decay швидкий — alpha
розчиняється, коли решта ринку теж побачить flow.

## Контракт

- **Файл**: [`backend/services/strategies/traders_confluence.py`](../../backend/services/strategies/traders_confluence.py)
- **Клас**: `TradersConfluenceStrategy`
- **slug**: `traders_confluence`
- **source_key**: `traders`
- **Subscriptions**: `EventType.TRADER_ACTIVITY`
- **Ключові входи**: tracked-wallets, confluence signals від
  `traders_firehose_pipeline`, wallet-tier classification (low /
  medium / high / extreme), source flags

## Логіка детекції

Filtering pipeline на trader signals:

1. **Confidence ≥ 0.45**.
2. **Wallet count ≥ 2** — confluence by definition.
3. **Tier ≥ low** (можна підняти до medium/high/extreme).
4. **Entry price ≤ 0.85**.
5. **Source qualified** — wallet прошов discovery-валідацію
   (signal quality, не bot, не toxic).
6. **Active status** — wallet не paused.
7. **Market tradeable** — accepting_orders, enable_order_book.
8. **Crypto exclusion** — false дефолтно (crypto має свої стратегії).
9. **Signal age ≤ 720 хв** (12 годин).
10. **Confluence strength ≥ 50%** — нормалізована метрика
    «скільки tracked-гаманців у тому напрямку vs всі активні».

Якщо всі gates пройшли, opportunity йде у execution з
REPRICE_LOOP-policy (taker_limit IOC, до 2 reprice-attempts).

## Логіка виходу

Default TP/SL — TP 12%. Behavioral alpha decays, тому max hold і
trailing stop важливі (із `default_config` чи orchestrator-overrides).

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_edge_percent` | `3.0` | |
| `min_confidence` | `0.45` | |
| `min_confluence_strength` | `0.50` | Поріг confluence |
| `min_tier` | `low` | low/medium/high/extreme |
| `min_wallet_count` | `2` | Min wallets-учасників |
| `max_entry_price` | `0.85` | |
| `take_profit_pct` | `12.0` | |
| `firehose_max_age_minutes` | `720` | 12 годин stale-cutoff |
| `firehose_source_scope` | `all` | tracked / pool / individual / group |
| `firehose_side_filter` | `all` | buy / sell / all |
| `execution_policy` | `REPRICE_LOOP` | |
| `price_policy` | `taker_limit` | |
| `time_in_force` | `IOC` | |

## Коли НЕ працює

- **Smart money не такі smart**. Tracked-гаманці підбираються через
  on-chain аналіз (wallet_intelligence, anomaly_detector), але
  історичний edge ≠ майбутній. Periodically переоцінюйте pool.
- **Insider activity confluence**. Іноді confluence — це reaction
  на одну новину; alpha вже у ціні.
- **Stale signal**. 12 годин — багато; для intraday-ринків це майже
  весь cycle. Знижуйте `firehose_max_age_minutes` для короткострокових.
- **Wallet copy-paste bots**. Якщо два «гаманці» — це насправді один
  оператор за двома адресами, confluence штучна. Wallet discovery
  лове частину таких через clustering.

## Посилання

- [Traders Copy Trade](traders-copy-trade.md) — пряме копіювання
  ордерів, не aggregated confluence.
- Wallet discovery: `services/wallet_discovery.py`,
  `wallet_intelligence.py`, `insider_detector.py` (27-point anomaly
  score).
