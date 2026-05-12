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

## Як тюнити через offline-бектест

Plan 0046 додав offline-бектест для `crypto_update`-стратегій. Замість
того, щоб запускати паралельні shadow-трейдери і чекати тижні, можна
прогнати грід через історичні `firehose_evaluation` рядки і
`crypto_oracle_history` за обране вікно (за замовчуванням 24 години).

**Як це працює**:

- VWAP/staleness/oracle-age відтворюються з `firehose_evaluation`
  payload-у — той самий лог, що бачить firehose-tab UI. Тобто
  бектест ніколи не "вгадує" depth, він використовує ту depth, що
  була у живій eval-ы.
- Resolution-price для PnL береться з `crypto_oracle_history`
  (`order by timestamp_ms desc limit 1 where timestamp_ms <= end_ms`).
- Sweep робить cartesian-grid combinations і повертає
  `leaderboard` посортований за `composite_score = total_pnl_usd * win_rate`.

**Curl-приклад** (виконувати з віддаленого хоста, не локально;
`<HOMERUN_HOST>` — це `polyhome-prod` для `main` і `polyhome-1`
для `dev`, див.
[`docs/plans/architecture/deploy-targets.md`](../plans/architecture/deploy-targets.md)):

```bash
ssh <HOMERUN_HOST> 'curl -fsS -X POST http://127.0.0.1:8888/api/validation/code-backtest/optimize-strategy \
  -H "Content-Type: application/json" \
  -d "{
    \"strategy_slug\": \"crypto_5m_midcycle\",
    \"window_hours\": 168,
    \"grid\": {
      \"min_distance_bps\": [5, 10, 15, 20],
      \"max_entry_price\": [0.60, 0.65, 0.70]
    },
    \"top_k\": 12
  }"' | jq '.leaderboard'
```

**Як читати leaderboard**:

```json
[
  {
    "params": {"min_distance_bps": 10, "max_entry_price": 0.70},
    "emit_count": 38,
    "win_count": 31,
    "loss_count": 7,
    "total_pnl_usd": 184.20,
    "win_rate": 0.8158,
    "samples": 312,
    "composite_score": 150.31
  },
  ...
]
```

- `emit_count` — скільки можливостей би згенерувалось при цій
  конфігурації.
- `samples` — скільки циклів пройшло через replay (firehose-rows
  у вікні).
- `total_pnl_usd` — сума `(1 - vwap) * shares` для виграних і
  `-vwap * shares` для програних.
- `composite_score` — sort-key. Висока win-rate без об'єму = низький
  score, тож проблема "5 trades 100% win" не виграє у грід-серу.

**Caveats**:

- Якщо grid містить `bet_size_usd`, replayed slippage все одно
  відображає `bet_size_usd`, що був живим у момент логу — це
  фундаментальна межа replay-у. API повертає це у `caveats`.
- `firehose_evaluation` рядки логуються тільки за активних
  trader-binding-ів. Якщо стратегія була disabled у потрібний
  період, бектест поверне пусто.

## Посилання

- [Crypto Spike Reversion](crypto-spike-reversion.md) — протилежна
  гіпотеза для коротких рухів.
- [Crypto Entropy Maker](crypto-entropy-maker.md) — entropy-based
  альтернатива з більш строгими gates.
