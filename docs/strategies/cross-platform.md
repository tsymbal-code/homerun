# Cross-Platform Oracle Arbitrage

## Сутність

Polymarket і Kalshi часто пропонують ринки **на ту саму подію** з
різними цінами. Cross-Platform Oracle знаходить такі дублі через
fuzzy-match по тексту питань, перевіряє, що outcome-структура
сумісна (а не «WIN» vs «DRAW»), і будує арбітражний spread: купити
дешеву ногу на одній платформі, дешеву протилежну ногу на другій.
Це cross-venue version [Basic Arbitrage](basic.md) і єдиний спосіб
торгувати міжмайданчиковий dispersion у нашій системі.

## Контракт

- **Файл**: [`backend/services/strategies/cross_platform.py`](../../backend/services/strategies/cross_platform.py)
- **Клас**: `CrossPlatformStrategy`
- **slug**: `cross_platform`
- **source_key**: `scanner`
- **Subscriptions**: `EventType.MARKET_DATA_REFRESH`
- **Ключові входи**: Polymarket markets + prices,
  кеш Kalshi-ринків (TTL 180 с, paginated httpx),
  Jaccard text-similarity, fee-curves (`polymarket_taker_fee`,
  `kalshi_taker_fee`), детектор outcome-типу

## Логіка детекції

Стратегія йде **тільки** по Polymarket-ринках (Kalshi vs Kalshi не
має сенсу, бо платформа одна). Для кожного:

1. Підтягує / кешує Kalshi-ринки (TTL ~180 с — захист від rate-limit-ів).
2. Токенізує питання обох ринків (lowercase, split on whitespace,
   stopwords-стрип) і обчислює **Jaccard similarity** на множинах
   слів. Поріг — `match_threshold=0.60`.
3. **Sport-outcome guard**: якщо в Polymarket-питанні є «WIN»-семантика
   (binary), а Kalshi-ринок — 3-way (home/draw/away), стратегія
   відкидає (різні outcome-простори).
4. **Soccer-spec guard**: Polymarket «90-min» vs Kalshi «advance»
   (включає overtime/penalties) — теж не сумісні.
5. Для прийнятної пари рахує два можливих spread-и: (а) buy YES
   poly + buy NO kalshi, (б) buy NO poly + buy YES kalshi. Beresze
   найкращий, якщо after-fee profit ≥ `min_spread_after_fees=0.03`.

## Логіка виходу

Guaranteed spreads — тримаємо до резолюції обох ніг. Non-guaranteed
випадки (наприклад, через пограничну similarity) використовують
TP/SL з конфігу.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_edge_percent` | `5.0` | Min spread у % |
| `min_confidence` | `0.50` | |
| `max_risk_score` | `0.70` | |
| `min_spread_after_fees` | `0.03` | Min spread після обох taker-fee |
| `match_threshold` | `0.60` | Jaccard similarity gate |
| `kalshi_api_ttl` | `180s` | TTL Kalshi-ring-кешу |

## Коли НЕ працює

- **Тонкий Kalshi-бук**. Kalshi-liquidity сильно гірша за Polymarket
  на більшості подій. Ввід реально-екзекутабельного розміру може
  з'їсти весь edge slippage-ом.
- **Subtle outcome-divergence**. Іноді обидва питання звучать
  однаково, але фактично резолвляться по різних правилах
  (US president — «inauguration» vs «election»; soccer 90 vs ET).
  Sport / soccer гайди допомагають, але не покривають усе.
- **Kalshi auth fail**. Без сконфігурованого Kalshi API-ключа
  стратегія не отримує цін → silent skip. Перевірте Settings →
  Kalshi.
- **Stale кеш**. 180 с TTL — це багато для волатильних подій
  (sports near kick-off). Знижуйте до 30–60 с, якщо граєте in-game.

## Посилання

- [Basic Arbitrage](basic.md) — той самий принцип всередині Polymarket.
- Налаштування Kalshi: див. [Settings & Secrets](../plans/architecture/settings-and-secrets.md)
  (поля `kalshi_email`, `kalshi_password`, `kalshi_api_key`).
