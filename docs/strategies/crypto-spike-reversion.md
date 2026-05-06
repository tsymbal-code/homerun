# Crypto Spike Reversion

## Сутність

Реверсивна гіпотеза до [Crypto 5m Midcycle](crypto-5m-midcycle.md):
коли на 5-хвилинному вікні відбувся **різкий impulse ≥ 1.8%**, що
не підтверджується довшим контекстом (30m / 2h moves), стратегія
ставить **проти** цього руху. Edge — у тому, що короткі spike-и
часто є liquidity-events / forced-flow, а не справжніми signal-ами.

Reversion shape валідується трьома горизонтами: 5m impulse має
домінувати над 30m тренд, 2h рух не має перевищувати 14% (інакше
це справжній bull/bear regime, не spike).

## Контракт

- **Файл**: [`backend/services/strategies/crypto_spike_reversion.py`](../../backend/services/strategies/crypto_spike_reversion.py)
- **Клас**: `CryptoSpikeReversionStrategy`
- **slug**: `crypto_spike_reversion`
- **source_key**: `crypto`
- **Subscriptions**: `EventType.CRYPTO_UPDATE`
- **Ключові входи**: 5m / 30m / 2h price moves (з `crypto_update`-
  payload-у), оракул (Binance direct), price-to-beat, entry prices
  YES/NO, seconds-left, reversion_shape валідатор

## Логіка детекції

1. **Spike detection**: `|move_5m| ≥ 1.8%`.
2. **Reversion shape OK**:
   - 5m impulse domination over 30m trend.
   - `|move_2h| ≤ 14%` (захист від справжнього regime-shift-у).
3. **Direction = OPPOSITE** to spike: spike up → buy NO, spike down
   → buy YES.
4. Entry price ≤ 0.92.
5. Liquidity ≥ $2000.
6. Edge = `|move_5m| · 0.6 + oracle_diff` (якщо є). Confidence
   = `0.50 + move/12 · 0.20 + shape · 0.10 + elapsed · 0.10`.

## Логіка виходу

TP 8%, SL 4%, max hold 8 хвилин. IOC-taker entry; close near
resolution (за 20 с до cycle end).

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_edge_percent` | `2.8` | |
| `min_confidence` | `0.44` | |
| `min_abs_move_5m` | `1.8` | Поріг spike-у |
| `max_abs_move_2h` | `14.0` | Понад це — це trend, не spike |
| `require_reversion_shape` | `True` | Strict validation |
| `max_entry_price` | `0.92` | |
| `max_hold_minutes` | `8.0` | Швидкий вихід |
| `take_profit_pct` | `8.0` | |
| `stop_loss_pct` | `4.0` | |
| `kelly_fractional_scale` | `0.45` | Sub-Kelly sizing |
| `min_liquidity_usd` | `2000.0` | |

## Коли НЕ працює

- **Trending regime**. Bull-runs / capitulations — це не spike,
  це справжні рухи. `max_abs_move_2h=14%` — груба перевірка, але
  не покриває 4-год регіми. Перевіряйте news context.
- **Oracle lag підтверджує false-spike**. Якщо Chainlink ще не
  оновився, ціна ринку може шарпнутися «авансом», а потім вирівнятися.
  Це не reversion; це нормалізація. Stratery це частково лове через
  oracle_diff в edge-формулі.
- **Shape false-positive**. У noise-ринках 5m impulse регулярно
  домінує над 30m просто через випадковість.
- **Резолюція ось-ось**. У останні 60 с reversion не встигає
  спрацювати; max_hold=8 хв охоплює це.

## Посилання

- [Crypto 5m Midcycle](crypto-5m-midcycle.md) — continuation-гіпотеза,
  протилежний bias.
- [Flash Crash Reversion](flash-crash-reversion.md) — non-crypto
  аналог для скан-ринків.
- [Crypto Entropy Maker](crypto-entropy-maker.md) — нейтральна MM.
