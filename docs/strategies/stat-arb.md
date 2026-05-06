# Statistical Arbitrage

## Сутність

Ансамбль із **7 weak-сигналів**, що оцінюють «справедливу»
ймовірність outcome-у і порівнюють її з market-ціною. Кожен
сигнал — sub-5%-edge сам по собі, але об'єднання через weighted
ensemble дає 5%+ deviation, який тримається довше за хвильові
рухи. Не гарантує прибутку — це **statistical edge**, що працює в
середньому, не в кожному окремому випадку.

Свідомо excluds sports (bookmakers вже стискають spreads),
open-ended awards, elections, і всі crypto-ринки (вони мають свої
microstructure-стратегії).

## Контракт

- **Файл**: [`backend/services/strategies/stat_arb.py`](../../backend/services/strategies/stat_arb.py)
- **Клас**: `StatArbStrategy`
- **slug**: `stat_arb`
- **source_key**: `manual`
- **Subscriptions**: `EventType.MARKET_DATA_REFRESH`
- **Ключові входи**: ціни, category base rates, multi-market
  consensus, flow data, історичні snapshot-и

## 7 Weak Signals

| Сигнал | Вага | Що рахує |
|---|---|---|
| `anchoring` | 0.13 | Bias до округлених цін (50¢, 25¢, 75¢) |
| `category_base_rate` | 0.18 | Історичний win-rate схожих ринків (sports type, election type, ...) |
| `consensus` | 0.20 | Узгодження з ціною ринків-сусідів у тій самій сім'ї |
| `momentum` | 0.12 | Recent price velocity / direction |
| `volume_price` | 0.15 | Volume-weighted price stability |
| `favorite_longshot` | 0.10 | Класичний bookmaker-bias (favorite over-priced, longshot under) |
| `liquidity_imbalance` | 0.12 | Bid-ask depth asymmetry |

Підсумкова adjusted_price = market_price + Σ(weighted_signal_adjustments),
обмежено `signal_adjustment_scale=0.15` (тобто ±15¢).

Якщо |market_price - fair_price| ≥ `min_edge_percent=5%`, fires
opportunity.

## Логіка виходу

TP 12%, SL 6%, trailing stop 8%. Statistical confidence decays —
тримати довго не варто.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_edge_percent` | `5.0` | |
| `min_confidence` | `0.45` | |
| `max_risk_score` | `0.75` | |
| `enable_stat_signals` | `True` | Master toggle |
| `exclude_market_keywords` | `[bitcoin, btc, ethereum, eth, solana, sol, xrp, crypto, up or down, doge, ...]` | Crypto exclusion |
| `anchor_tolerance` | `0.02` | Window для anchoring detection |
| `signal_weights` | (table above) | Per-signal вага в ensemble |
| `signal_adjustment_scale` | `0.15` | Max sum adjustment, ±15¢ |
| `momentum_window_seconds` | `600.0` | 10-min momentum |
| `momentum_lookback_seconds` | `300.0` | Reference window |
| `confidence_floor` | `0.30` | |
| `confidence_ceiling` | `0.85` | |
| `confidence_agreement_weight` | `0.6` | Скільки бонусу за signal-agreement |
| `take_profit_pct` | `12.0` | |
| `stop_loss_pct` | `6.0` | |
| `trailing_stop_pct` | `8.0` | |

## Коли НЕ працює

- **Edge case ринки**. Sports / elections / open-ended awards
  виключаються. Якщо передаєте такий ринок (через manual exception),
  signals можуть давати noise.
- **Single-signal dominance**. Ваги дають balance, але іноді один
  сигнал (наприклад, momentum) — це 80% ваги в дельті. Перевіряйте
  decomposition.
- **Stat-edge vs informational shift**. Якщо ринок swing-нув на
  правдиву новину, fair_price теж посунувся, а consensus / momentum
  ще не reflect — стратегія купить «cheap» якраз перед справжнім
  падінням.
- **Cost з `category_base_rate`**. Потрібна історія схожих ринків.
  На свіжих категоріях (нові sports / нові політичні події) base
  rate noise.

## Посилання

- [Temporal Decay](temporal-decay.md) — окрема mean-reversion
  гіпотеза без ensemble-апроchu.
- [Tail-End Carry](tail-end-carry.md) — окрема high-prob гіпотеза.
- [Probability Surface Arb](prob-surface-arb.md) — для threshold-
  family-ринків.
