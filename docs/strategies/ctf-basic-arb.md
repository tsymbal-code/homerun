# CTF Basic Arbitrage

## Сутність

CTF (Conditional Token Framework) — смарт-контракт Polymarket, що
дозволяє **split** $1 USDC на пару (1 YES + 1 NO) і **merge** пари
назад у $1. CTF Basic Arb експлуатує дві структурні нерівності:

- **Split + Sell**: якщо `YES_bid + NO_bid > $1`, ми робимо split
  $1 USDC у пару, негайно продаємо обидві ноги по бідах, отримуємо
  більше за $1 — різниця наша.
- **Buy + Merge**: якщо `YES_ask + NO_ask < $1`, купуємо обидві ноги
  по аск-цінах, мерджимо в $1, заробляємо різницю.

Відрізняється від [Basic Arbitrage](basic.md) тим, що замість
тримання обох ніг до резолюції тут **миттєвий** виконавчий цикл
через split/merge — позиція закривається в межах одного блоку.

## Контракт

- **Файл**: [`backend/services/strategies/ctf_basic_arb.py`](../../backend/services/strategies/ctf_basic_arb.py)
- **Клас**: `CTFBasicArbStrategy`
- **slug**: `ctf_basic_arb`
- **source_key**: `scanner`
- **Subscriptions**: `EventType.MARKET_DATA_REFRESH`
- **Ключові входи**: condition_id (формат `0x...`), YES/NO bids
  або asks з depth, gas budget (USD), assumed trade size, fee curve

## Логіка детекції

Для кожного eligible-ринку (Polymarket, condition_id формату `0x`,
`accepting_orders`, `enable_order_book`):

```
# Split / Sell
edge_split = (YES_bid + NO_bid - gas_per_share - fees) - 1.0

# Buy / Merge
edge_merge = 1.0 - (YES_ask + NO_ask + gas_per_share + fees)
```

`gas_per_share = gas_buffer_usd / assumed_trade_size_usd`. Стратегія
відкидає, якщо ні один edge не перевищує `min_edge_percent / 100`.
Liquidity-гейт — мінімальна depth у кожній нозі (`min_liquidity`).
Виконання робиться **bundle-ом**: 3 ноги (split + sell_yes + sell_no
або buy_yes + buy_no + merge) одним IOC-blockchain-tx через
PAIR_LOCK execution policy.

## Логіка виходу

Не існує — позиція не залишається відкритою. Виконавчий цикл — це
один atomic bundle, або заходить, або скасовується.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_edge_percent` | `0.60` | Min edge у % |
| `min_confidence` | `0.45` | |
| `max_risk_score` | `0.55` | |
| `min_liquidity` | `500.0` | Min liquidity, $ |
| `gas_buffer_usd` | `0.30` | Gas reserve на 3-leg bundle, $ |
| `assumed_trade_size_usd` | `25.0` | База для gas-per-share |
| `allow_split_sell` | `True` | Дозволити split/sell |
| `allow_buy_merge` | `True` | Дозволити buy/merge |

## Коли НЕ працює

- **Високий gas**. Edge 0.6% на $25 — це ~$0.15. Якщо реальний gas на
  3-leg bundle перевищує `gas_buffer_usd=$0.30`, стратегія в мінусі.
  Підіймайте `assumed_trade_size_usd` для більших позицій (gas-per-share
  падає).
- **Bundle-фейли**. Якщо одна нога bundle-а fail-иться (наприклад,
  одна сторона у block-у вже зникла), весь bundle відкочується. На
  Polygon це бере менше секунди, але в hot moment кілька ретраїв
  з'їдять опію.
- **Тонкий ордер-бук**. На малих ринках depth у `min_liquidity=$500`
  занадто м'який. Підіймайте до thousands для серйозних позицій.
- **Не-Polymarket ринки**. CTF — це Polymarket-only механіка. Якщо
  condition_id не починається з `0x`, стратегія skip-ить.

## Посилання

- [Basic Arbitrage](basic.md) — non-CTF варіант (тримання до
  резолюції замість instant-bundle).
- Опис CTF механізму:
  [Polymarket CTF docs](https://docs.polymarket.com/).
