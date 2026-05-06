# Holding Reward Yield

## Сутність

Polymarket виплачує **holding rewards** (~4% APY) держателям певних
довгих ринків, як стимул лишати liquidity. Якщо одночасно тримати YES
і NO у рівних пропорціях (через CTF split), сума завжди $1 — капітал
збережено, а rewards-и капають. Це структурно безризикова дохідність,
схожа на DeFi-yield, але з market-resolution exit-ом.

## Контракт

- **Файл**: [`backend/services/strategies/holding_reward_yield.py`](../../backend/services/strategies/holding_reward_yield.py)
- **Клас**: `HoldingRewardYieldStrategy`
- **slug**: `holding_reward_yield`
- **source_key**: `scanner`
- **Subscriptions**: `EventType.MARKET_DATA_REFRESH`
- **Ключові входи**: `clob_rewards` поле з Gamma API
  (`annualizedReward`, `rewardsDailyRate`), `rewards_min_size`,
  `rewards_max_spread`, `end_date`, market liquidity

## Логіка детекції

1. Фільтр eligible-ринків: бінарний, Polymarket, `accepting_orders`,
   `enable_order_book`, liquidity ≥ $5000, days_to_resolution ≥ 30.
2. Перевіряє наявність `clob_rewards` (або `rewards_min_size`) —
   ознака того, що ринок входить у програму rewards.
3. Витягує APY: пріоритет — `annualizedReward`, fallback —
   `rewardsDailyRate * 365`, остання — дефолт `4.0%`.
4. Рахує `holding_period_yield = apy * (days_to_res / 365)`. Якщо
   ≥ `min_apy=2.0%` — фіксує opportunity.

Виконавча частина: спліт $X USDC у пару (X YES + X NO) через
`splitPosition` CTF-call. На резолюції одна з ніг зникає на $0,
друга стає $1, ми отримуємо назад $X (мінус slippage), плюс
накопичені rewards.

## Логіка виходу

Тримати, доки не настане `exit_buffer_days=7` до резолюції. Тоді
зробити `mergePositions` (відновлення USDC) і вийти. Алгоритм має
explicit-логіку для цього в `should_exit`. На резолюції merge не
обов'язковий — переможна нога все одно дасть $1.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_apy` | `2.0` | Min APY для входу, % |
| `min_liquidity` | `5000.0` | Min liquidity, $ |
| `min_days_to_resolution` | `30.0` | Не лізти у короткі ринки |
| `max_opportunities` | `20` | Max ринків на одного оператора |
| `default_holding_reward_apy` | `4.0` | Fallback, якщо metadata-APY відсутня |
| `exit_buffer_days` | `7.0` | За скільки днів до резолюції merge-ити |

## Коли НЕ працює

- **Зміни в rewards-програмі Polymarket**. APY оголошується в
  metadata кожного ринку; Polymarket може змінити правила, виключити
  ринок, обнулити rewards. Стратегія не моніторить runtime-зміни —
  робіть `Reload` стратегій після оголошень.
- **Ринки з шорт-fuse-резолюцією**. `min_days_to_resolution=30`
  захищає, але якщо ринок резолвиться достроково (наприклад, owner
  cancellation), rewards знулюються, а merge-fee лишаються.
- **Slippage на split/merge**. CTF-операції коштують gas + потенційно
  втрату на bid-ask spread, якщо вирішите вийти раніше.
- **Capital opportunity cost**. 4% APY — це менше за DeFi-yield на
  USDC у багатьох пулах. Стратегія має сенс, коли капітал уже
  «застрягає» в Polymarket-екосистемі для інших стратегій.

## Посилання

- [CTF Basic Arb](ctf-basic-arb.md) — та сама механіка
  split/merge, але для миттєвого арбітражу замість тримання.
- Polymarket rewards: див. метадані ринку в Gamma API
  (`clob_rewards.annualizedReward`).
