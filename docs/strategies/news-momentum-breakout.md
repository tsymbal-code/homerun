# News Momentum Breakout

## Сутність

Дзеркало до [Flash Crash Reversion](flash-crash-reversion.md): коли
ринок **різко зростає на новинах** (≥ 10% за 300 секунд), ставимо
**в той самий бік** — у припущенні, що news-shock ще не повністю
учтений у ціні. Виконавчо це не «ловлю момент-розворот», а
trend-following із breakout-stop, який скасовується при перелому.

На відміну від [News Edge](news-edge.md), не використовує LLM —
просто реагує на price-action як на news-proxy.

## Контракт

- **Файл**: [`backend/services/strategies/news_momentum_breakout.py`](../../backend/services/strategies/news_momentum_breakout.py)
- **Клас**: `NewsMomentumBreakoutStrategy`
- **slug**: `news_momentum_breakout`
- **source_key**: `manual`
- **Subscriptions**: `EventType.MARKET_DATA_REFRESH`
- **Ключові входи**: price history (5-min window, до 1800 с),
  bid-ask spread, liquidity, market metadata для exclude-фільтрів

## Логіка детекції

1. Тримає rolling history останніх 1800 с цін на ринку.
2. Detect breakout: `move_5m ≥ 10%` upward.
3. **Shape validation**:
   - 5m share of 30m move ≥ 45% (вибух свіжий).
   - 2h move ≤ 80% (не parabolic-blowoff).
   - retracement from peak ≤ 35%.
4. **Exclude crypto / sports / стороні keyword-ринки** (overlap із
   спеціалізованими стратегіями).
5. Entry price ∈ [0.18, 0.78], spread ≤ 6%, liquidity ≥ $3000.
6. Target = repricing-точка через `target_distance_to_one_fraction
   = 0.55` (55% дистанції до $1).

## Логіка виходу

**Scale-out** на 30/60 базисних пунктах прибутку, **trailing stop
120 bps**, **momentum-stall exit** через 45 хв без нових highs,
**max hold 240 хв**. Take-profit 70%, stop-loss 25%.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `lookback_seconds` | `300.0` | Вікно breakout-detection |
| `breakout_threshold` | `0.10` | 10% move trigger |
| `target_distance_to_one_fraction` | `0.55` | Target = 55% дистанції до $1 |
| `min_entry_price` | `0.18` | |
| `max_entry_price` | `0.78` | |
| `max_spread` | `0.06` | |
| `min_liquidity` | `3000.0` | |
| `max_retrace_from_peak_fraction` | `0.35` | Захист від pull-back |
| `min_5m_share_of_30m` | `0.45` | Свіжість breakout-у |
| `max_abs_move_2h_pct` | `80.0` | Anti-parabolic |
| `require_breakout_shape` | `True` | Strict validation |
| `require_breakout_alignment` | `True` | Узгодження 5m/30m напрямку |
| `min_abs_move_5m` | `4.0` | Min absolute move |
| `exclude_crypto_markets` | `True` | |
| `exclude_sports_markets` | `True` | |
| `take_profit_pct` | `70.0` | Aggressive |
| `stop_loss_pct` | `25.0` | |
| `trailing_stop_pct` | `18.0` | |
| `momentum_stall_minutes` | `45.0` | Stall exit |

## Коли НЕ працює

- **Pump-and-dump**. Іноді 10% move — це organic news, іноді —
  manipulator-pump. Stratery не розрізняє. Trailing stop 18%
  частково захищає.
- **Mean-reversion regime**. Якщо ринок історично flips після кожного
  breakout-у, news-momentum дає негативний edge.
- **Slippage на тонкому buke**. Entry size > liquidity: ваш taker
  trade сам стає breakout, не reaction.
- **News false-positive**. Стратегія не читає новини напряму. Коли
  reason для move — не fundamental (наприклад, low-liquidity-spike),
  edge відсутній.

## Посилання

- [News Edge](news-edge.md) — LLM-based news analysis (більш
  інформований, але дорожчий).
- [Flash Crash Reversion](flash-crash-reversion.md) — реверсивна
  гіпотеза для падінь.
- [Crypto Spike Reversion](crypto-spike-reversion.md) — спайк
  reversion для crypto.
