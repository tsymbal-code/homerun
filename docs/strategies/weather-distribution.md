# Weather Distribution

## Сутність

Polymarket має сім'ї **weather-ринків**: «Температура у NYC буде
[bucket]°F у такий-то день», де buckets — це сусідні температурні
діапазони. Стратегія будує модельну ймовірність по **всіх** buckets
(на базі ансамблю прогнозів або normal CDF консенсусу), нормалізує
sum до 1.0, порівнює з market-цінами кожного bucket-а і ставить на
найбільш underpriced.

Ключова ідея: cross-bucket нормалізація запобігає «inflated edge»,
який отримуєш, якщо рахуєш кожен bucket незалежно (тоді sum
ймовірностей може бути > 1, і edge стає numerically нечесний).

## Контракт

- **Файл**: [`backend/services/strategies/weather_distribution.py`](../../backend/services/strategies/weather_distribution.py)
- **Клас**: `WeatherDistributionStrategy`
- **slug**: `weather_distribution`
- **source_key**: `weather`
- **Subscriptions**: `EventType.WEATHER_UPDATE`
- **Ключові входи**: ensemble model outputs (від
  `weather_signal_engine`), розподіл по температурних bucket-ах,
  sibling-ринки тієї ж локації / metric-у, локація + metric

## Логіка детекції

На `WEATHER_UPDATE`:

1. Збирає всі sibling-buckets для тієї ж локації + metric.
2. Для кожного bucket рахує `model_prob`:
   - Якщо доступні ensemble members ≥ 10 → fraction що потрапили в
     bucket.
   - Інакше → normal CDF з `consensus`-середнього і `sigma_c=1.8°F`.
3. Нормалізує `sum(model_prob) = 1.0`.
4. Ranks buckets за edge = `model_prob - yes_price`.
5. Fires opportunity на поточний bucket, якщо
   `edge ≥ min_edge_percent=5%` і `confidence ≥ 50%`.
6. Обмежує `max_buckets_per_event=2` — захист від перегризання
   capital по всій сім'ї одразу.
7. Entry price `≤ 0.85` — інакше edge не виправдовує.

## Логіка виходу

`resolve_only: true`. Тримати до forecast-резолюції, бо edge — це
ансамблева ймовірність, що не залежить від поточних flow-events.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_edge_percent` | `5.0` | |
| `sigma_c` | `1.8` | Std dev (°F) для normal CDF, коли ансамблю немає |
| `min_ensemble_members` | `10` | Min для прийняття ensemble-fraction |
| `min_confidence` | `0.50` | |
| `max_entry_price` | `0.85` | |
| `max_buckets_per_event` | `2` | Max позицій на одну подію |
| `risk_base_score` | `0.30` | |

## Коли НЕ працює

- **Брак forecasts**. Якщо у `services/weather` ансамбль не
  отримано (API даун), стратегія не emit-ить нічого.
- **Bucket rounding noise**. Гранична температура між двома
  buckets — модель може ставити 50/50, а ринок різко преферує
  один. Edge — у розрахунку моделі, але не завжди у нашу користь.
- **Climate-anomaly**. Ансамбль = модель **минулого**. На дивних
  погодних подіях (heat wave, cold snap) consensus падає, а
  market-ціна може бути правильна.
- **Liquidity тонка**. Weather-ринки на Polymarket мають невеликий
  обсяг — `min_liquidity` не явно gate-ить тут, але через
  default-evaluator може bottleneck-нути.

## Посилання

- [News Edge](news-edge.md) — LLM-based external signal source.
- [Traders Confluence](traders-confluence.md) — behavioral signal,
  не модельний.
- Weather pipeline: `backend/services/weather/`.
