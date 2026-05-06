# Crypto 5m Midcycle

## Сутність

«Не передбачуй — спостерігай». На 5-хвилинних crypto up-or-down
ринках, на **2:30 у цикл** (рівно середина), ціна вже мала пів-цикл
показати напрям. Якщо Chainlink за цей час відійшов на ≥ 15 базисних
пунктів від price-to-beat, **ставимо на продовження** руху —
emiрically winrate ≈ 80% при entry ≤ 70¢ (документується у
docstring-у класу).

Це найпростіша і найвідверто-edge crypto-стратегія: жодних
microstructure-фільтрів, тільки direction + entry-price gate.

## Контракт

- **Файл**: [`backend/services/strategies/crypto_5m_midcycle.py`](../../backend/services/strategies/crypto_5m_midcycle.py)
- **Клас**: `Crypto5mMidcycleStrategy`
- **slug**: `crypto_5m_midcycle`
- **source_key**: `crypto`
- **Subscriptions**: `EventType.CRYPTO_UPDATE`
- **Ключові входи**: Chainlink-оракул, price-to-beat, VWAP з
  order-book depth (через StrategySDK), cycle end timestamp,
  slug-regex для 5m-detection

## Логіка детекції

CycleTracker per-market emit-ить milestone **рівно на 150-й секунді**
у 300-секундний 5m-цикл. На цій події gates:

1. Це 5m-timeframe (slug-regex).
2. Asset enabled (за замовчуванням SOL, XRP — найчіткіший edge).
3. Cycle midpoint щойно crossed.
4. Min 90 секунд лишилось до резолюції (буфер на entry + settlement).
5. Reference price > 0, Chainlink свіжий (< 5s old).
6. **Distance gate**: `|chainlink - reference| ≥ 15bps` від reference.
7. Order-book depth для VWAP-розрахунку доступна.
8. VWAP entry в межах `[0.05, 0.70]` — інакше edge замало.

Side = direction, у якому Chainlink вийшов від reference. Entry
= VWAP. Target = $1.00 (continuation hypothesis). Розмір позиції —
fixed `bet_size_usd=$15`.

## Логіка виходу

Single directional entry. Default exit: TP 6.5%, SL 4.0%, або hold
to settlement. У docstring-у — empirical 80% win rate при entry ≤ 70¢.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `assets` | `[SOL, XRP]` | Дефолтно без BTC/ETH (вищий noise on midpoint) |
| `min_distance_bps` | `15.0` | Min Chainlink offset від reference |
| `max_entry_price` | `0.70` | Max entry — за вищу ціну edge не виправдовує |
| `min_entry_price` | `0.05` | Захист від edge-of-cycle випадкових entries |
| `bet_size_usd` | `15.0` | Fixed sizing — не Kelly |
| `midcycle_seconds` | `150.0` | Тригер midpoint-у |
| `min_seconds_to_resolution` | `90.0` | Захист від запізнілого entry |
| `max_oracle_age_ms` | `5000` | Старіший оракул skip |

## Коли НЕ працює

- **Reverse-correlated mid-cycle**. Іноді мікро-bull-run у першу
  половину циклу заверш-ається mean-reversion-ом у другу. 80% win-rate
  — historical, regime-dependent.
- **Низька liquidity на VWAP-розрахунку**. Якщо depth тонка, VWAP
  не репрезентативна, і entry може бути далеко від реальної ціни.
- **15m / 1h / 4h ринки**. Стратегія жорстко прив'язана до 5m.
  Аналогічну логіку для довших timeframe-ів треба окремо
  калібрувати.
- **High-news periods**. На FOMC / earnings / major пресс-релізи
  emperal-edge ламається.

## Посилання

- [Crypto Spike Reversion](crypto-spike-reversion.md) — протилежна
  гіпотеза для коротких рухів.
- [Crypto Entropy Maker](crypto-entropy-maker.md) — entropy-based
  альтернатива з більш строгими gates.
