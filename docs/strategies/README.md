# Стратегії Homerun

У системі живе **28 готових стратегій** — кожна як окремий Python-клас
у `backend/services/strategies/`. Усі вони успадковуються від
`BaseStrategy` і працюють через однаковий контракт: `detect_async`
(або `detect`) повертає список `Opportunity`, опціонально
`evaluate` уточнює розмір/ціну, а `should_exit` керує закриттям
позицій. Код стратегії зберігається в БД (`strategies` таблиця),
проходить AST-валідацію, і гаряче перезавантажується без
перезапуску воркерів — деталі див.
[`docs/plans/architecture/backend-architecture.md`](../plans/architecture/backend-architecture.md).

Кожна стратегія прив'язана до одного `source_key`, що визначає, який
worker plane її ганяє і на які події вона підписана:

| `source_key` | Worker plane | Що дає на вхід |
|---|---|---|
| `scanner` | `worker-trading` | `MARKET_DATA_REFRESH` від загального сканера ринків Polymarket/Kalshi |
| `crypto` | `worker-trading` | `CRYPTO_UPDATE` — sub-секундні апдейти від Chainlink + Binance + market structure для BTC/ETH/SOL/XRP |
| `news` | `worker-news` | `NEWS_UPDATE` — RSS/REST/Twitter/семантичний матчинг ↔ ринки |
| `weather` | `worker-news` | `WEATHER_UPDATE` — ансамблі, sibling-ринки температурних букетів |
| `traders` | `worker-discovery` | `TRADER_ACTIVITY` — сигнали від tracked-гаманців |
| `manual` / `sports` | де завгодно | Ручне керування або `MARKET_DATA_REFRESH` без vendor-spec фільтрів |

> **Важливо.** Не всі стратегії дають «гарантований» прибуток. Структурні
> арбітражі (basic, negrisk, ctf_basic_arb, holding_reward_yield) — так,
> у межах CTF-механіки і feed-цін. Решта — це або statistical edge
> (mean-reversion, momentum), або behavioral edge (copy-trade, news,
> traders confluence), і вони можуть програвати на хвостах. Завжди
> валідуйте через бектест перед `live`-режимом.

## Поточний стан на сервері

Знімок з `polyhome-1` станом на 2026-05-06. У таблицях категорій нижче
символ ✅ біля назви означає, що стратегія наразі ввімкнена
(`enabled=true` у БД). Відсутність символу означає, що оператор
вимкнув її — код залишається в `strategies` таблиці й документація
по стратегії — актуальна, просто воркер її не виконує.

**Активні (10 з 28):**
`basic`, `certainty_shock`, `ctf_basic_arb`, `manual_wallet_position`,
`market_making`, `negrisk`, `sports_overreaction_fader`,
`tail_end_carry`, `traders_confluence`, `traders_copy_trade`.

**Вимкнені (18 з 28):**
`btc_eth_convergence`, `btc_eth_directional_edge`, `btc_eth_maker_quote`,
`combinatorial`, `cross_platform`, `crypto_5m_midcycle`,
`crypto_entropy_maker`, `crypto_spike_reversion`,
`flash_crash_reversion`, `holding_reward_yield`, `news_edge`,
`news_momentum_breakout`, `prob_surface_arb`, `settlement_lag`,
`stat_arb`, `temporal_decay`, `vpin_toxicity`, `weather_distribution`.

Перевірити поточний стан самостійно:

```bash
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T \
  postgres psql -U homerun -d homerun -c \
  "select slug, enabled, status from strategies order by enabled desc, slug"'
```

Увімкнути / вимкнути стратегію — це або toggle у вкладці
**Strategies** UI, або `PUT /api/strategies/{id}` із `{"enabled": true|false}`.

## Категорії

### Структурний арбітраж (8)

«Купити дешевше, ніж математично доведено». Працюють на CTF-механіці,
fee-кривих, дублюванні ринків між майданчиками. Найближче до
guaranteed profit, але уразливі до slippage і скасування ордерів.

| | Назва | Slug | Сутність |
|---|---|---|---|
| ✅ | [Basic Arbitrage](basic.md) | `basic` | YES + NO ask sum < $1.00 на одному бінарному ринку |
| ✅ | [NegRisk Bundle Arb](negrisk.md) | `negrisk` | Купити YES на всіх взаємовиключних outcome-ах, sum < $1 |
| ✅ | [CTF Basic Arb](ctf-basic-arb.md) | `ctf_basic_arb` | Split / merge через Polymarket CTF з бідами/асками вище / нижче за $1 |
|   | [Combinatorial Arb](combinatorial.md) | `combinatorial` | IP-solver на залежних ринках (IMPLIES / EXCLUDES / CUMULATIVE) |
|   | [Cross-Platform Oracle](cross-platform.md) | `cross_platform` | Polymarket vs Kalshi — той самий event, дві різні ціни |
|   | [Settlement Lag](settlement-lag.md) | `settlement_lag` | Outcome визначений, ціна ще не оновилась |
|   | [Holding Reward Yield](holding-reward-yield.md) | `holding_reward_yield` | ~4% APY на CTF-split на довгих ринках |
|   | [Probability Surface Arb](prob-surface-arb.md) | `prob_surface_arb` | Isotonic regression на сім'ях ринків зі threshold-axis |

### Crypto microstructure (6)

5m / 15m / 1h / 4h binary up-or-down ринки на BTC/ETH/SOL/XRP. Sub-second
execution, Chainlink + Binance feeds, post-only maker rebates.

| | Назва | Slug | Сутність |
|---|---|---|---|
|   | [BTC/ETH Convergence](btc-eth-convergence.md) | `btc_eth_convergence` | Post-only maker $0.85–$0.95 у вікні 5–45 с до резолюції |
|   | [BTC/ETH Directional Edge](btc-eth-directional-edge.md) | `btc_eth_directional_edge` | IOC-taker на бік оракула при diff ≥ 2× taker fee |
|   | [BTC/ETH Maker Quote](btc-eth-maker-quote.md) | `btc_eth_maker_quote` | Двостороннє post-only quoting із оракул-skew |
|   | [Crypto 5m Midcycle](crypto-5m-midcycle.md) | `crypto_5m_midcycle` | На 2:30 5-хв циклу — continuation у бік оракул-руху, VWAP ≤ 70¢ |
|   | [Crypto 5m Last-Outcome Follow](crypto-5m-last-outcome.md) | `crypto_5m_last_outcome` | На старті 5-хв циклу повторюємо сторону, що виграла попередній цикл |
|   | [Crypto Entropy Maker](crypto-entropy-maker.md) | `crypto_entropy_maker` | Entropy + spread quality + cancel recovery + flow imbalance |
|   | [Crypto Spike Reversion](crypto-spike-reversion.md) | `crypto_spike_reversion` | Reversion на 5m-spike ≥ 1.8% із shape-валідацією |

### Behavioral / signal-driven (5)

Edge не з мікроструктури, а з зовнішнього джерела: новини, погода,
поведінка smart-money. Decay швидкий, alpha не структурна.

| | Назва | Slug | Сутність |
|---|---|---|---|
|   | [News Edge](news-edge.md) | `news_edge` | LLM-probability vs market price на новинах + семантичний match |
|   | [Weather Distribution](weather-distribution.md) | `weather_distribution` | Ансамбль / нормальний CDF на температурних букетах vs market |
| ✅ | [Traders Confluence](traders-confluence.md) | `traders_confluence` | Конвергенція ≥2 tracked-гаманців на одну позицію |
| ✅ | [Traders Copy Trade](traders-copy-trade.md) | `traders_copy_trade` | Real-time копіювання ордерів tracked-wallets із sizing-поличі |
|   | [News Momentum Breakout](news-momentum-breakout.md) | `news_momentum_breakout` | Вхід у бік свіжого breakout-руху ≥ 10% за 300 с |

### Momentum / mean-reversion (6)

Класичні поведінкові патерни без зовнішніх сигналів. Edge — у моделі
ціни (decay-curve, мікро-spike, certainty-shock).

| | Назва | Slug | Сутність |
|---|---|---|---|
| ✅ | [Certainty Shock](certainty-shock.md) | `certainty_shock` | Швидкий 22% one-sided рух near-deadline → ставимо на продовження |
|   | [Flash Crash Reversion](flash-crash-reversion.md) | `flash_crash_reversion` | Падіння ≥ 8% за 240 с із spread/liquidity-гейтами |
| ✅ | [Sports Overreaction Fader](sports-overreaction-fader.md) | `sports_overreaction_fader` | Fade різких рухів 5–40% у live-sports |
|   | [Statistical Arbitrage](stat-arb.md) | `stat_arb` | Ансамбль 7 weak-сигналів (anchoring, base-rate, momentum, ...) |
| ✅ | [Tail-End Carry](tail-end-carry.md) | `tail_end_carry` | High-prob outcomes 85–90% близько до резолюції |
|   | [Temporal Decay](temporal-decay.md) | `temporal_decay` | Mean-reversion до sqrt-time decay-curve |

### Liquidity provision і toxicity detection (2)

Інфраструктурні: одна ставить liquidity, друга — детектить
informed-flow.

| | Назва | Slug | Сутність |
|---|---|---|---|
| ✅ | [Market Making](market-making.md) | `market_making` | Avellaneda-Stoikov reservation price на двосторонніх квотах |
|   | [VPIN Toxicity](vpin-toxicity.md) | `vpin_toxicity` | Volume-bucketed flow imbalance ≥ 70% — слідуємо за informed |

### Position management (1)

Не входить, тільки керує вже відкритими позиціями. Корисно, коли ви
руками купили щось і хочете залишити в системі під автоматичний risk
control.

| | Назва | Slug | Сутність |
|---|---|---|---|
| ✅ | [Manual Manage Hold](manual-manage-hold.md) | `manual_wallet_position` ¹ | Hard stop / breakeven / backside peak / near-resolution lock |

¹ В БД ця стратегія має slug `manual_wallet_position`. Файл документа
називається `manual-manage-hold.md` за історичною назвою — зміст
відповідає обом slug-ам, це одна й та сама стратегія.

## Спільний контракт усіх стратегій

Кожна стратегія має такі обов'язкові атрибути класу:

```python
class FooStrategy(BaseStrategy):
    strategy_type = "foo"          # унікальний slug; також PK у БД
    name = "Foo"                   # human-readable
    description = "..."            # одна-два речення
    source_key = "scanner"         # який worker plane її ганяє
    subscriptions = [EventType.MARKET_DATA_REFRESH]  # на що реагує
    default_config = {...}         # параметри + правила exit
```

І один із цих методів:

- `detect(events, markets, prices) -> list[Opportunity]` — sync;
- `detect_async(events, markets, prices) -> list[Opportunity]` — async,
  частіше використовується;

Опціонально:

- `evaluate(opportunity) -> Opportunity | None` — уточнення sizing /
  ціни в момент перед armed-фазою;
- `should_exit(position, market_state) -> ExitDecision | None` —
  кастомна логіка виходу (в більшості — TP/SL з `default_config`);
- `on_fill / on_partial_fill / on_cancel` — pure-notification hooks.

## Куди дивитись далі

- Як стратегія завантажується і валідується:
  [`docs/plans/architecture/backend-architecture.md`](../plans/architecture/backend-architecture.md)
  (розділ «Plug-in patterns»).
- Як додати нову стратегію: створіть файл
  `backend/services/strategies/<slug>.py` із `BaseStrategy`-нащадком.
  Воркер той, який збігається з `source_key`, її підхопить
  при `strategy_loader.refresh_all_from_db()` (UI: Strategies →
  Reload).
- Як писати чи запускати backtest: див. розділ
  «Backtesting & Shadow Simulation» у [README репозиторію](../../README.md).
