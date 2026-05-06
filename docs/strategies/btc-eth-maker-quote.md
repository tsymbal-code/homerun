# BTC/ETH Maker Quote

## Сутність

«Інвентар-нейтральний» market-maker для BTC/ETH/SOL/XRP up-or-down
ринків: ставить **post-only buy** на **обидві сторони** (YES і NO)
на один тік нижче поточних asks. Заробляє bid-ask spread + maker-rebate
(0% taker fee на Polymarket для maker-ордерів). Якщо є свіжий
Chainlink — додає невеликий directional skew у бік оракула, не
порушуючи інвентар-нейтральності.

Це найбільш «пасивна» з crypto-стратегій. Не намагається передбачити
напрям, а заробляє на тому, що taker-flow з'їдає liquidity з обох
сторін.

## Контракт

- **Файл**: [`backend/services/strategies/btc_eth_maker_quote.py`](../../backend/services/strategies/btc_eth_maker_quote.py)
- **Клас**: `BtcEthMakerQuoteStrategy`
- **slug**: `btc_eth_maker_quote`
- **source_key**: `crypto`
- **Subscriptions**: `EventType.CRYPTO_UPDATE`
- **Ключові входи**: live order book (bid-ask по обох outcome),
  опціонально Chainlink для skew, market phase timing

## Логіка детекції

На кожен tick:

1. Identify BTC/ETH/SOL/XRP up-or-down ринок.
2. Read order book: best ask on YES, best ask on NO.
3. Place **post-only buy YES** at `yes_ask - 1_tick`, **post-only buy
   NO** at `no_ask - 1_tick`. Tick-розмір — 0.01 у Polymarket.
4. Якщо Chainlink свіжий (< 5s old): skew quote toward predicted
   winner (на 1–2 тіки вище за non-winner-сторону). Інвентар-нейтральне
   правило зберігається — обидві сторони активні, просто розмір трошки
   нерівний.
5. Cancel-and-replace при кожному значущому order-book-апдейті
   (стандартний crypto MM-цикл).

## Логіка виходу

Post-only-ордери самі скасовуються при таймауті або при перетині
тіку (race-condition). Filled позиція тримається до резолюції, де
автоматично закривається.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `post_only_both_sides` | `True` | Активні обидві сторони |
| `tick_below_ask` | `1` | На скільки тіків нижче asks ставимо |
| `oracle_skew_enabled` | `True` | Використовувати Chainlink для skew |
| `inventory_neutral` | `True` | Тримати збалансований інвентар |
| `timeframe_aware_gates` | varies | Адаптивні параметри по timeframe-у |

## Коли НЕ працює

- **Direction-trending ринок**. Якщо ціна стабільно рухається в одну
  сторону весь cycle, ваші YES-fill-и весь час програють, NO — весь
  час виграють, і інвентар стає односторонній. Maker quote не
  розрізняє trend vs noise; для trend-ринків краще
  [BTC/ETH Convergence](btc-eth-convergence.md) або
  [Directional Edge](btc-eth-directional-edge.md).
- **Тонкий ордер-бук на одній стороні**. Якщо `no_ask` повністю
  зник, ваша NO-сторона стає best-bid сама — fill-и переходять до
  toxic-flow.
- **Maker rebate змінюється**. Polymarket час від часу налаштовує
  rebate-програму. APY перевіряйте по `clob_rewards`.
- **Latency на cancel-replace**. Якщо лишається старий ордер на
  попередній ціні, можете зловити adverse selection.

## Посилання

- [Market Making](market-making.md) — узагальнений MM не для crypto-
  ринків (Avellaneda-Stoikov).
- [BTC/ETH Convergence](btc-eth-convergence.md), [BTC/ETH Directional
  Edge](btc-eth-directional-edge.md) — directional варіанти.
- [Crypto Entropy Maker](crypto-entropy-maker.md) — більш складний
  MM з entropy / cancel-recovery / orderflow filters.
