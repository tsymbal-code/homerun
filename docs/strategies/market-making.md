# Market Making

## Сутність

Класичний MM: стоїмо на обох сторонах bid/ask, заробляємо spread,
з захистом від інвентар-ризику через **Avellaneda-Stoikov reservation
price model**. Ідеальні кандидати — бінарні ринки з ціною біля 50/50,
широким spread (3–18%), високим volume і середньою liquidity. На
Polymarket maker-fee = 0%, що робить цей edge раціональним.

На відміну від [BTC/ETH Maker Quote](btc-eth-maker-quote.md), яка
прив'язана до crypto microstructure, ця стратегія працює на будь-якому
бінарному ринку.

## Контракт

- **Файл**: [`backend/services/strategies/market_making.py`](../../backend/services/strategies/market_making.py)
- **Клас**: `MarketMakingStrategy`
- **slug**: `market_making`
- **source_key**: `manual`
- **Subscriptions**: `EventType.MARKET_DATA_REFRESH`
- **Ключові входи**: ціни, bid-ask spreads, inventory state, order
  book liquidity

## Логіка детекції

1. Filter candidates: бінарний ринок, liquidity ≥ $5000,
   24h_volume ≥ $1000, spread ∈ [3%, 18%].
2. Compute inventory risk: distance ціни від 50/50 → adjusts how
   aggressively to skew quotes.
3. **Avellaneda-Stoikov reservation price**:
   ```
   r = mid_price - inventory × γ × σ²
   ```
   де γ — risk aversion (0.1), σ — volatility (rolling 20 periods).
4. Quote: `bid = r - spread/2`, `ask = r + spread/2`, де
   `spread = base_spread × vol_multiplier`.
5. Inventory cap: `max_inventory_usd=$500`. Якщо перевищено —
   **flatten** (close-out side).
6. **Reward score**: bonus якщо ринок входить у Polymarket-rewards
   програму і наша квота близько до midpoint.

## Логіка виходу

TP 8% (середньозваженої позиції), trailing stop, max_hold 240 хв,
flatten-trigger при `inventory_load ≥ 85%` від cap.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_spread` | `0.03` | Min spread для входу |
| `max_spread` | `0.18` | Max — понад це toxicity |
| `min_liquidity` | `5000.0` | |
| `min_volume_24h` | `1000.0` | |
| `gamma` | `0.1` | Risk aversion (Avellaneda-Stoikov) |
| `max_inventory_usd` | `500.0` | Per-market cap |
| `take_profit_pct` | `8.0` | |
| `inventory_skew_gamma` | `0.1` | Skew aggressiveness |
| `vol_spread_multiplier` | `2.0` | Multiplier on rolling vol |
| `vol_lookback_periods` | `20` | |
| `reward_midpoint_band` | `0.03` | Bonus zone для rewards-eligible |
| `flatten_inventory_trigger` | `0.85` | Flat at 85% load |
| `reward_score_weight` | `0.35` | |

## Коли НЕ працює

- **Adverse selection**. Toxic flow (informed traders) знімає
  liquidity з one-sided quotes. Avellaneda-Stoikov частково
  захищає через inventory penalty, але не повністю.
- **Wide spread = ділянка великих рухів**. spread 18% — це або
  ринок ще не «знайшов» ціну, або просто зустрічаються 2 toxic
  great-size buyers/sellers.
- **Inventory cap reached**. `max_inventory_usd=$500` cap швидко
  вичерпується на волатильних ринках. Підвищуйте обережно.
- **Maker rebate revoked**. Якщо Polymarket змінює fee-структуру,
  edge зникає. Це — не самостійна доходність, а функція 0% fee
  + spread.

## Посилання

- [BTC/ETH Maker Quote](btc-eth-maker-quote.md) — crypto-specialized
  MM.
- [Crypto Entropy Maker](crypto-entropy-maker.md) — крипто MM з
  microstructure-фільтрами.
- [VPIN Toxicity](vpin-toxicity.md) — окрема стратегія, що **слідує**
  за informed flow, не quote-ить проти.
