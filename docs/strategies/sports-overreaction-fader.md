# Sports Overreaction Fader

## Сутність

У live-sports-ринках реакція ціни на голи / heavy plays часто
перевищує справжній probability shift. Empirically (Moskowitz 2021)
~50% такого move реверсує протягом наступних кількох хвилин.
Стратегія ловить **різкі** рухи 5–40% за 300 секунд, валідує
favorite-status (60–94% prob) і ставить **проти** руху на
`reversion_fraction=50%` від магнітуди.

Окремо застосовується **Choi & Hui 2014 surprise metric**: якщо
favorite раптово drop-ить, surprise-component додає бонус до edge.

## Контракт

- **Файл**: [`backend/services/strategies/sports_overreaction_fader.py`](../../backend/services/strategies/sports_overreaction_fader.py)
- **Клас**: `SportsOverreactionFaderStrategy`
- **slug**: `sports_overreaction_fader`
- **source_key**: `sports`
- **Subscriptions**: `EventType.MARKET_DATA_REFRESH`
- **Ключові входи**: prices, trade tape, flow imbalance, sports
  metadata (kick-off time, in-play status)

## Логіка детекції

1. **Move detection**: 5% ≤ |move| ≤ 40% за 300 c (поза цим — або
   noise, або справжня regime-change).
2. **Favorite gate**: favorite_prob ∈ [0.60, 0.94] до руху.
3. **Time gate**: 0.05 ≤ hours_to_resolution ≤ 72 (від live-game до
   3 днів).
4. **Liquidity / spread**: ≥ $2000, ≤ 200 bps.
5. **Reversion target**: `current + reversion_fraction × move = 50%`
   назад.
6. **Min reversion edge**: 1.5%.
7. **Flow imbalance**: lookback 600 c, min volume $50.
8. Direction = OPPOSITE до руху.

## Логіка виходу

Reversion target reached, OR trailing stop 30%, OR max hold 120 хв,
**OR resolution-hold disallowed for sports** (на резолюції позиція
закривається примусово). TP 10%, SL 6%.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_move_pct` | `5.0` | Min absolute move |
| `max_move_pct` | `40.0` | Max — понад це справжній regime |
| `move_window_seconds` | `300.0` | |
| `min_favorite_prob` | `0.60` | |
| `max_favorite_prob` | `0.94` | |
| `min_hours_to_resolution` | `0.05` | 3 хвилини — мінімум для in-play |
| `max_hours_to_resolution` | `72.0` | |
| `min_liquidity` | `2000.0` | |
| `max_spread_bps` | `200.0` | |
| `reversion_fraction` | `0.50` | Target reversion |
| `min_reversion_edge` | `0.015` | Min absolute edge |
| `flow_lookback_seconds` | `600.0` | Window для flow analysis |
| `min_flow_volume_usd` | `50.0` | |
| `take_profit_pct` | `10.0` | |
| `stop_loss_pct` | `6.0` | |
| `max_hold_minutes` | `120.0` | |
| `trailing_stop_pct` | `30.0` | Sports-special: ширший ніж звичайний |

## Коли НЕ працює

- **Справжній momentum-shift**. Якщо ваш «overreaction» — це насправді
  goal + ankle-injury до того ж гравця, ринок мав рацію. Stratery
  не читає news/scores напряму.
- **Liquidity dry-up to game-end**. У останні 5 хв матчу spread
  розширюється, fill стає проблемою. `min_hours_to_resolution=0.05`
  (3 хв) теоретично дозволяє вхід, але на практиці складно.
- **Esports / niche sports**. Низька liquidity, шумний flow,
  unreliable favorite-prob.
- **Pre-game** (відсутні line-moves). Strategy працює in-play. Перед
  кик-офом використовуйте інші підходи.

## Посилання

- [Tail-End Carry](tail-end-carry.md) — protect-the-favorite
  стратегія, не fade.
- [Flash Crash Reversion](flash-crash-reversion.md) — non-sports
  аналог.
- [Stat Arb](stat-arb.md) — exclude-list містить sports тому, що
  вони мають свою спеціалізацію тут.
