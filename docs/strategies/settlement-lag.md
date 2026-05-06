# Settlement Lag

## Сутність

Класична Kroer et al. Type 3 mispricing: outcome ринку **визначений**
(матч закінчився, кандидата оголосили), але CLOB ще не оновив ціни до
$1/$0. Settlement Lag сидить, чекає і ставить на сторону, ціна якої
має ось-ось підстрибнути. Edge — у затримці між публікацією outcome
й оновленням таблиці ордерів.

## Контракт

- **Файл**: [`backend/services/strategies/settlement_lag.py`](../../backend/services/strategies/settlement_lag.py)
- **Клас**: `SettlementLagStrategy`
- **slug**: `settlement_lag`
- **source_key**: `manual`
- **Subscriptions**: `EventType.MARKET_DATA_REFRESH`
- **Ключові входи**: ціни YES/NO, дата резолюції з метаданих ринку,
  multi-outcome events (NegRisk теж охоплюються)

## Логіка детекції

Стратегія шукає сигнали трьох типів і фіксує opportunity, якщо хоча б
**два** з них одночасно тригерять:

1. **Overdue**: ринок проминув `end_date` більше ніж на свій тиковий
   buffer і досі live на CLOB.
2. **Sum deviation**: `|YES + NO - 1.0| > min_sum_deviation` (3¢) —
   обидві ноги в «подвідсумкованому» стані.
3. **Near-zero / near-one**: одна з цін уже в `< 0.02` або `> 0.95`,
   тобто ринок частково resolved, але другий бік ще не на $1.

Для NegRisk-подій перевірка адаптована: підсумовується YES по всіх
ногах і порівнюється зі $1.

## Логіка виходу

`resolve_only: true` — тримаємо до самої резолюції CLOB, бо саме на
останньому tick ціна стрибає. Виходити рано — означає віддати edge
тому, хто почекає.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_edge_percent` | `4.0` | Min очікуваний прибуток |
| `min_confidence` | `0.45` | |
| `max_risk_score` | `0.78` | |
| `min_liquidity` | `25.0` | Дуже низький поріг — settlement-lag ринки часто майже мертві |
| `max_days_to_resolution` | `14` | Не лізти на ринки > 14 днів від резолюції |
| `near_zero_threshold` | `0.02` | Ціна, яку трактуємо як resolved-NO |
| `near_one_threshold` | `0.95` | Ціна, яку трактуємо як resolved-YES |
| `min_sum_deviation` | `0.03` | Min гэп YES+NO від $1 |

## Коли НЕ працює

- **Ринки, що зависли через диспут**. Polymarket UMA-резолвери іноді
  затримують on-chain settlement на дні через диспути. Тут не
  «затримка ціни», а реальна неоднозначність — стратегія може
  потрапити на сторону, яка програє після диспуту.
- **Дрібна liquidity**. Ринки в settlement-lag часто майже без
  trade-flow; гейт `min_liquidity=25` низький саме тому. Можна
  потрапити в ситуацію, де книга порожня й ваш ордер сам стає
  best-price.
- **Резолюція нееквівалентна виграшу**. Для NegRisk-подій із
  off-resolver-логікою (custom oracles) формула sum YES = 1 не
  завжди точна.

## Посилання

- [Basic Arbitrage](basic.md) — формою схожа (sum YES + NO < 1), але
  edge зовсім інший (структурний vs затримка).
- [Holding Reward Yield](holding-reward-yield.md) — тримання довгих
  ринків заради іншого edge (rewards), не settlement-lag.
