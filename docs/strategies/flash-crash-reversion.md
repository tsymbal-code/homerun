# Flash Crash Reversion

## Сутність

Ціна рухається швидко в одну сторону, але без супровідного
fundamental-а — це часто **liquidity flash crash**, а не справжній
move. Стратегія детектить **падіння ≥ 8% за 240 секунд**, перевіряє
spread / liquidity, і ставить на reversal до часткового відновлення
(`min_rebound_fraction=45%` від просадки).

На відміну від [News Momentum Breakout](news-momentum-breakout.md),
яка ставить **за** рухом, flash crash reversion ставить **проти** —
ключове припущення: якщо move без news, він fundamentally
unsubstantiated.

## Контракт

- **Файл**: [`backend/services/strategies/flash_crash_reversion.py`](../../backend/services/strategies/flash_crash_reversion.py)
- **Клас**: `FlashCrashReversionStrategy`
- **slug**: `flash_crash_reversion`
- **source_key**: `scanner`
- **Subscriptions**: `EventType.MARKET_DATA_REFRESH`
- **Ключові входи**: rolling deque до 180 snapshots
  `(timestamp, YES, NO, YES_bid, YES_ask, NO_bid, NO_ask)`,
  spread з order book, liquidity

## Логіка детекції

1. Зберігає історію до 1800 секунд (`stale_history_seconds`).
2. Для кожного outcome знаходить baseline-ціну на cutoff
   (`now - 240 секунд`).
3. Якщо `drop ≥ 8%` AND `spread ≤ 7%` AND `entry ≤ 82¢`:
   - Target rebound = `max(1.5%, drop × 45%)`.
   - Direction = OPPOSITE до drop (drop YES → buy YES knowing price
     went down too far).
4. **Tight margin reject**: якщо current > old_price - 0.001,
   тобто rebound уже почався, edge фактично з'їдений.
5. Excludes crypto markets за keyword.

## Логіка виходу

Close at reversion target (current ≥ target), OR time decay
(> 2 години default), OR trailing stop. Default exit на резолюції.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `lookback_seconds` | `240.0` | 4-хвилинне вікно для drop-detection |
| `stale_history_seconds` | `1800.0` | Stale-cutoff (30 хв) |
| `drop_threshold` | `0.08` | 8% — поріг flash crash |
| `min_rebound_fraction` | `0.45` | Target = 45% від drop |
| `min_target_move` | `0.015` | Min absolute move |
| `max_entry_price` | `0.82` | |
| `max_spread` | `0.07` | |
| `min_liquidity` | `2500.0` | |
| `exclude_crypto_markets` | `True` | |

## Коли НЕ працює

- **Real news drop**. Якщо новина за 240 c знизила outcome
  ймовірність на 8%, це не flash crash, це нова інформація.
  Стратегія не має news-feed-у; на news-heavy подіях відключайте.
- **Liquidity vacuum**. Іноді crash і rebound не настає бо ринок
  вже мертвий. Min_liquidity=$2500 захищає, але не гарантує.
- **Wider spread = більший adverse selection**. spread > 7% gate
  частково виключає, але pre-resolution spreads природно ширші.
- **Затягнутий drop**. 240-секундне вікно — це короткий-термін
  flash; для повільніших падінь (наприклад, через 1 годину)
  стратегія не reagune.

## Посилання

- [News Momentum Breakout](news-momentum-breakout.md) — протилежна
  гіпотеза (continuation замість reversion).
- [Crypto Spike Reversion](crypto-spike-reversion.md) — crypto
  аналог.
- [Sports Overreaction Fader](sports-overreaction-fader.md) — fade
  на sports-ринках.
