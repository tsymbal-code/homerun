# NegRisk Bundle Arbitrage

## Сутність

NegRisk — Polymarket-механізм для multi-outcome подій, де **рівно
один** outcome виграє (наприклад, «Хто стане президентом США 2028?»).
Якщо просумувати YES-ask ціни всіх взаємовиключних outcome-ів і вийде
менше за $1, ми купуємо YES на кожному, гарантовано отримуємо $1
виплати від єдиного переможця, і прибуток — це різниця. На відміну від
[Basic Arbitrage](basic.md), який працює на одному бінарному ринку,
NegRisk скуповує кошик ринків.

## Контракт

- **Файл**: [`backend/services/strategies/negrisk.py`](../../backend/services/strategies/negrisk.py)
- **Клас**: `NegRiskStrategy`
- **slug**: `negrisk`
- **source_key**: `scanner`
- **Subscriptions**: `EventType.MARKET_DATA_REFRESH`
- **Ключові входи**: метадані події (NegRisk-flag або 3-way sports
  outcome), YES-ask ціни всіх ніг, fill-cost з depth, дати резолюції

## Логіка детекції

1. Збирає всі ринки події, пов'язані як взаємовиключні. Гейтиться двома
   шляхами: (а) явний `negRisk` флаг від Polymarket, (б) 3-way sports
   outcome (home / draw / away).
2. Перевіряє, що набір **повний** — є умова «жодного з перерахованих»
   (наприклад, «Other»), інакше виключність не гарантована.
3. Рахує total cost зі реальних ask-цін з достатньою liquidity. Якщо
   sum < $1.00 з мінімальним margin (`long_min_ask_priced_margin`
   = 0.02), і ринки укладаються в `max_resolution_spread_days=7`,
   фіксує opportunity.
4. Окремо відфільтровує election ринки (`election_min_total_yes=0.97`)
   через історичну shenanigan-волатильність.

## Логіка виходу

`resolve_only: true`. Кошик тримаємо до резолюції — рознос дат ≤ 7 днів
гарантує, що ризик stale-кошика мінімальний.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_edge_percent` | `3.0` | Min прибуток у % |
| `min_confidence` | `0.42` | |
| `max_risk_score` | `0.68` | |
| `min_markets` | `3` | Min ринків у кошику |
| `min_total_yes` | `0.95` | Min sum YES (на грані) |
| `warn_total_yes` | `0.97` | Warning-поріг (надто близько до $1) |
| `election_min_total_yes` | `0.97` | Більш суворий гейт для виборів |
| `max_resolution_spread_days` | `7` | Max рознос дат резолюції в кошику |
| `long_min_ask_priced_margin` | `0.02` | Margin понад reported price |
| `long_min_per_leg_liquidity` | `750.0` | Min liquidity у кожній нозі |

## Коли НЕ працює

- **Неповний кошик outcome-ів**. Якщо хтось «Other» забув додати — це
  не виключні події, а просто dependent. Стратегія таке відкидає.
- **Тонка liquidity на хоча б одній нозі**. $750 — це сирий поріг;
  глибокі позиції потребують більше. Liquidity-gate per-leg, не по
  всьому кошику.
- **Election-ринки**. Polymarket виборчі ринки історично ламали
  edge через NegRisk-resolver-події, тому стратегія тут підвищує
  поріг до 97%.
- **Open-ended / threshold-ринки** («BTC > $X»). Це cumulative, не
  exclusive. Для них див. [Probability Surface Arb](prob-surface-arb.md).

## Посилання

- [Combinatorial Arb](combinatorial.md) — узагальнення на ринки з
  довільними залежностями (IMPLIES / EXCLUDES / CUMULATIVE).
- [Basic Arbitrage](basic.md) — той самий принцип на одному ринку.
