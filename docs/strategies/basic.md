# Basic Arbitrage

## Сутність

Найпростіший «купити дешевше за фундаменталку» арбітраж: на одному
бінарному ринку Polymarket купуємо одночасно YES та NO, якщо їхні
**ask-ціни в сумі стабільно нижчі за $1.00 після всіх fee**. Оскільки
на резолюції рівно один із токенів закриється на $1, а другий — на $0,
сумарна виплата гарантовано $1, а ми за пару заплатили менше. Це
найближча до guaranteed profit структура, але вразлива до slippage,
скасування ордерів і затримок виконання.

## Контракт

- **Файл**: [`backend/services/strategies/basic.py`](../../backend/services/strategies/basic.py)
- **Клас**: `BasicArbStrategy`
- **slug**: `basic`
- **source_key**: `scanner` — крутиться у `worker-trading`
- **Subscriptions**: `EventType.MARKET_DATA_REFRESH`
- **Ключові входи**: цільові YES/NO ціни (asks для входу), order-book
  depth, market liquidity, fee curve Polymarket-а

## Логіка детекції

На кожен tick сканера стратегія йде по бінарних ринках Polymarket
(тільки), для кожного тягне **ask-ціни обох ніг** із order-book-а
(бо саме за ask можна негайно купити taker-ордером), додає Polymarket
taker-fee, і перевіряє:

```
total_cost = yes_ask_with_fee + no_ask_with_fee
if total_cost < 1.0 - min_edge_percent / 100:
    surface opportunity
```

Додаткові гейти: ринок має `accepting_orders=True`,
`enable_order_book=True`, depth обох сторін ≥ `min_book_depth`,
liquidity ≥ `min_liquidity`, `max_leg_spread` обмежує надто широкий
spread у будь-якій нозі. Confidence рахується з spread, liquidity і
розміру edge.

## Логіка виходу

`should_exit` за замовчуванням повертає `resolve_only=True` —
тримаємо до резолюції, бо edge структурний і не залежить від
intermediate-цін. У `default_config` можна перевести на TP/SL, але це
ламає гарантію.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_edge_percent` | `0.75` | Поріг прибутку (0.75% від $1 = ~0.75¢) |
| `min_confidence` | `0.55` | Скільки впевнені, що executable |
| `max_risk_score` | `0.32` | Max risk |
| `min_liquidity` | `1000.0` | Min liquidity по ринку, $ |
| `min_book_depth` | `25.0` | Min depth у кожній нозі, $ |
| `max_leg_spread` | `0.03` | Max bid-ask spread однієї ноги |
| `require_accepting_orders` | `True` | Ринок має приймати ордери |
| `require_order_book` | `True` | Має бути активний CLOB |
| `require_polymarket` | `True` | Лише Polymarket (CTF доступний) |
| `retention_window` | `2m` | Скільки тримати opportunity у кеші |

## Коли НЕ працює

- **Ринок без активного order-book** — `enable_order_book=False`. Тоді
  жодних реальних ask-цін, та й ставити ордер ніяк.
- **Тонка глибина**. Edge у 0.75% з'їдається slippage-ом, якщо trade
  size > book depth. Гейт `min_book_depth=$25` обмежує дрібні позиції;
  для більших — підвищуйте.
- **Ринки в активній резолюції**. Якщо outcome визначений, але CLOB
  ще не закритий, обидві ноги можуть піти проти вас одночасно.
- **NegRisk-події**. Тут «binary» — це одна нога з трьох-чотирьох
  виключних. Для них використовуйте [NegRisk Bundle Arb](negrisk.md).

## Посилання

- Кубок прикладу: будь-який Polymarket binary з тонким, але двостороннім
  CLOB.
- Споріднені: [CTF Basic Arb](ctf-basic-arb.md) (split/merge замість
  купівлі обох), [NegRisk Bundle Arb](negrisk.md) (для multi-outcome),
  [Settlement Lag](settlement-lag.md) (схожа форма, але edge від
  затримки оновлення).
