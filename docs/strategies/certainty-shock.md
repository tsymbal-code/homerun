# Certainty Shock

## Сутність

Коли на ринку відбувся **різкий one-sided рух ≥ 22%** з мінімальним
retracement (< 8%) і це сталося в **межах 0.5–7 днів до резолюції**,
стратегія ставить на **продовження** руху до final certainty (≥ 96%).
Гіпотеза: коли ринок впевнено перецінив outcome, останні кілька
відсотків — це чисте «closing the gap», а не reversal.

Виключно non-crypto-ринки: keyword-фільтр відсікає BTC/ETH/SOL/XRP/
DOGE etc., бо crypto-ринки мають свої спеціалізовані стратегії.

## Контракт

- **Файл**: [`backend/services/strategies/certainty_shock.py`](../../backend/services/strategies/certainty_shock.py)
- **Клас**: `CertaintyShockStrategy`
- **slug**: `certainty_shock`
- **source_key**: `scanner`
- **Subscriptions**: `EventType.MARKET_DATA_REFRESH`
- **Ключові входи**: price history (до 100 snapshots, 6 годин
  lookback), deadline parsed з question text або `end_date`,
  YES/NO live prices

## Логіка детекції

1. Підтримує rolling history до 100 snapshots, 6 годин (`shock_lookback_seconds=21600`).
2. На кожен tick шукає shock: `|move| ≥ 0.22` за вікном.
3. **Retrace gate**: max retracement з peak ≤ 8%.
4. **Time gate**: 0.5 ≤ days_to_deadline ≤ 7.
5. **Entry price gate**: ≥ 0.65 (інакше «favorite» status сумнівний)
   AND ≤ 0.97 (інакше edge замало).
6. **Min favored price**: 0.65.
7. Direction = той самий, куди йде shock.
8. Target exit = `max(0.96, entry_price + move × 0.45)` — або 96%
   certainty, або 45% від руху до повної ясності.
9. **Expected move ≥ 3 cents** (`min_expected_move=0.03`).
10. Excludes crypto-ринки за keyword-list.

## Логіка виходу

TP 8% від entry, SL 4%. Default exit на резолюції.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `shock_lookback_seconds` | `21600` | 6 годин історії |
| `shock_min_abs_move` | `0.22` | 22% — поріг shock-у |
| `shock_max_retrace` | `0.08` | Max 8% pull-back |
| `shock_min_favored_price` | `0.65` | Min entry |
| `shock_target_certainty` | `0.96` | Target |
| `min_days_to_deadline` | `0.5` | Min 12 годин |
| `max_days_to_deadline` | `7.0` | Max тиждень |
| `take_profit_pct` | `8.0` | |
| `stop_loss_pct` | `4.0` | |
| `exclude_market_keywords` | `[bitcoin, btc, ethereum, eth, ...]` | Crypto-exclude |

## Коли НЕ працює

- **False shocks**. Тонкий ордер-бук + один великий sell → 22% drop,
  який потім миттєво відскочить. `shock_max_retrace=8%` лове прості
  reversal-и, але не toxic-flow.
- **Long-tail ринки**. Якщо deadline > 7 днів, ринок має час
  reversal-нути. Стратегія чесно skip-ить такі.
- **Election-/category-specific shock-и**. Іноді 22% move — це
  reaction на новину, яка ще буде «розкручуватися» в обидва боки.
- **Resolution overhang**. Деякі ринки на Polymarket резолвляться
  через UMA-disputes; «certainty» може ламатися останнім тиком.

## Посилання

- [Tail-End Carry](tail-end-carry.md) — близька гіпотеза для
  high-prob outcomes near deadline, без shock-вимоги.
- [Flash Crash Reversion](flash-crash-reversion.md) — реверсивна
  гіпотеза, гра проти різких рухів.
- [Sports Overreaction Fader](sports-overreaction-fader.md) — теж
  fade різких рухів, але на sports-ринках.
