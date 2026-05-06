# VPIN Toxicity

## Сутність

**VPIN** (Volume-synchronized Probability of Informed Trading,
Easley-López de Prado-O'Hara 2012) — мірило order flow toxicity. Ідея
проста: trade volume партиціонується на bucket-и фіксованого розміру
(не часу), і в кожному рахується imbalance buy_volume vs sell_volume.
Якщо розкид великий — це сигнал, що в ринок зайшли informed traders;
ставимо в той самий бік, що і вони.

Це детектор-стратегія: вона не quote-ить (як [Market Making](market-making.md)),
а скоріше **слідує** за toxic flow в той момент, коли він тільки
стартує.

## Контракт

- **Файл**: [`backend/services/strategies/vpin_toxicity.py`](../../backend/services/strategies/vpin_toxicity.py)
- **Клас**: `VPINToxicityStrategy`
- **slug**: `vpin_toxicity`
- **source_key**: `manual`
- **Subscriptions**: `EventType.MARKET_DATA_REFRESH`,
  `EventType.TRADE_EXECUTION`
- **Ключові входи**: trade tape (executed trades) — volume, side
  (explicit flag або tick rule), price

## Логіка детекції

1. Накопичує trades у volume-synchronized buckets ($500 кожен).
2. Class-ифікує сторону trade-у:
   - Якщо є explicit-flag (buy/sell) — використовує його.
   - Інакше **tick rule**: trade above midpoint → buy, below → sell.
3. Coмпьютить VPIN:
   ```
   VPIN = Σ |buy_vol - sell_vol| / (num_buckets × bucket_size)
   ```
   за rolling window 20 buckets.
4. Trigger: `VPIN ≥ 0.70`.
5. Direction: дивимось у який бік flow imbalance (більше buy → buy
   YES; більше sell → buy NO).
6. Min liquidity $1000, min entry edge 2.5%.

## Логіка виходу

Exit when VPIN drops below `0.70 × 0.6 = 0.42` (informed flow
dissipated). Standard TP 12% / SL.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_edge_percent` | `2.5` | |
| `min_confidence` | `0.50` | |
| `max_risk_score` | `0.75` | |
| `bucket_size_usd` | `500.0` | Volume per bucket |
| `num_buckets` | `20` | Total bucket window |
| `vpin_threshold` | `0.70` | Trigger threshold |
| `vpin_lookback_buckets` | `20` | Rolling window |
| `min_liquidity` | `1000.0` | |
| `take_profit_pct` | `12.0` | |

## Коли НЕ працює

- **Дрібні ринки, де $500-bucket — це 30+ хвилин**. VPIN-сигнал
  стає laggy; до моменту тригеру flow вже відіграно.
- **Tick rule помилки**. Без explicit side-flag, midpoint не завжди
  відображає реальну домінанту.
- **Reverse-direction informed flow**. Іноді informed знає, що ринок
  переоцінений, і йде проти sentiment-у. VPIN покаже imbalance,
  ваш copy буде в той самий бік — програш.
- **Stop-loss race**. У момент VPIN-spike-у багато учасників закривають
  позиції одночасно; ваш copy може не встигнути на best price.

## Посилання

- [Market Making](market-making.md) — quote-ить **проти** flow,
  захист через inventory penalty.
- [Traders Confluence](traders-confluence.md) — інша смуга детекції
  informed-flow (через wallet-tracking).
- [Crypto Entropy Maker](crypto-entropy-maker.md) — flow-aware MM
  для crypto ринків.
