# Crypto Entropy Maker

## Сутність

Канонічна crypto microstructure-стратегія: оцінює якість моменту
для входу через **бінарну ентропію** ціни (`H = -p·log2(p) -
(1-p)·log2(1-p)` — мірило «невизначеності»), якість spread-у, темп
скасувань ордерів і flow imbalance. Високий H + помірний spread +
recovery після cancel-storm + здорова liquidity → maker-вхід на
30+ bp upside до резолюції.

З усіх crypto-стратегій ця має **найскладніший набір gates** і
відсіює більшість тіків. Edge — у тому, що вона не входить, поки
microstructure не «правильна».

## Контракт

- **Файл**: [`backend/services/strategies/crypto_entropy_maker.py`](../../backend/services/strategies/crypto_entropy_maker.py)
- **Клас**: `CryptoEntropyMakerStrategy`
- **slug**: `crypto_entropy_maker`
- **source_key**: `crypto`
- **Subscriptions**: `EventType.CRYPTO_UPDATE`
- **Ключові входи**: up/down prices, ентропія, spread, 30s cancel
  rate, prior peak cancel rate, flow imbalance, recent move zscore,
  оракул (Binance direct preferred), price-to-beat

## Логіка детекції

Багатоступінчасте gating:

1. **Entropy gate**: `H(yes) ≥ 0.82` (тобто ціна досить близько до
   50/50, де є реальна uncertainty, а не deterministic outcome).
2. **Spread quality**: spread ∈ `[0.6%, 6.5%]`. Нижче — ринок
   «mature», менше edge; вище — toxicity-signal.
3. **Cancel-rate gate**: `cancel_rate_30s ≤ 75%`. Нормальна liquidity
   має скасування, але якщо книга «стирається» миттєво — це quote-
   dumping, не торгувати.
4. **Cancel recovery**: `peak_cancel_rate - current ≥ 14%`. Тобто
   ринок щойно пережив cancel-storm і відновився.
5. **Spread widening guard**: `spread_widening ≤ 22 bps` за recent
   window — захист від flash-toxicity.
6. **Liquidity gate**: ≥ $1000 USD.
7. **Entry price**: `[0.80, 0.92]`.
8. Direction: oracle diff % або price skew (Chainlink vs reference).

Edge-формула:
```
edge = |diff_pct| · entropy_multiplier
     + cancel_recovery · 5
     + flow_imbalance · 1.5
     + recent_move_zscore · 0.4
```

Confidence додає бонусом entropy, spread quality, cancel recovery,
flow imbalance.

## Логіка виходу

Take-profit 6.5%, stop-loss 4.0%, max hold 16 хвилин. Якщо ввели на
maker-rest, додатково 20s буферу до resolution gate.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_edge_percent` | `1.0` | Базовий edge |
| `min_confidence` | `0.40` | |
| `min_entropy` | `0.82` | Високий H gate |
| `min_spread_pct` | `0.006` | 0.6% |
| `max_spread_pct` | `0.065` | 6.5% |
| `max_cancel_rate_30s` | `0.75` | 75% — поріг toxic |
| `min_entry_price` | `0.80` | |
| `max_entry_price` | `0.92` | |
| `min_liquidity_usd` | `1000.0` | |
| `take_profit_pct` | `6.5` | |
| `stop_loss_pct` | `4.0` | |
| `maker_rest_includes_timeout` | `True` | Додає 20s до резолюції-gate |

## Коли НЕ працює

- **Stale microstructure-фічі**. Якщо `crypto_update`-payload не
  містить cancel rate / flow imbalance, gate-и фейлять silently і
  стратегія skip-ить.
- **Низько-волатильні ринки**. На стабільних ринках H часто < 0.82,
  стратегія взагалі не входить.
- **Pre-resolution toxic-flow**. У останні 30 c часто всі починають
  cancel-spam-ити; cancel-recovery gate захищає, але не повністю.
- **Maker-rest fail**. Якщо post-only-ордер не enters CLOB через
  race-condition, edge-розрахунок недійсний.

## Посилання

- [BTC/ETH Maker Quote](btc-eth-maker-quote.md) — проста версія MM
  без microstructure-фільтрів.
- [VPIN Toxicity](vpin-toxicity.md) — теж flow-based, але для
  non-crypto ринків.
- [Crypto Spike Reversion](crypto-spike-reversion.md) — реверсивна
  гіпотеза проти моменту, не за.
