# Manual Manage Hold

## Сутність

Не входить, **тільки керує** вже відкритими позиціями. Корисно, коли
оператор руками купив щось у Polymarket-app і хоче залишити в Homerun
під автоматичний risk control: hard-stop, breakeven protect,
backside peak (фіксація після різкого розвороту), near-resolution
hold/cut.

Це по суті — **exit-only контролер**, не trader. У `default_config`
явно `allow_new_entries=False`.

## Контракт

- **Файл**: [`backend/services/strategies/manual_manage_hold.py`](../../backend/services/strategies/manual_manage_hold.py)
- **Клас**: `ManualManageHoldStrategy`
- **slug у БД**: `manual_wallet_position` (той самий клас; історичний
  файл-slug `manual_manage_hold` зберігся в репозиторії, але БД-row
  зареєстрована як `manual_wallet_position`)
- **source_key**: `manual`
- **Subscriptions**: `—` (нічого не слухає; тільки реагує на periodic
  position-state-tick)
- **Ключові входи**: entry price, current price, peak price (tracked),
  position age, decline streak

## Логіка виходу (єдина «логіка», що працює)

**Six exit paths**, перевіряються в порядку:

1. **Hard stop loss**: PnL ≤ -18%. Закриваємо без думання.
2. **Breakeven protect**: якщо peak_gain ≥ 2.2% AND PnL ≤ 0.1%
   (тобто перейшли в нуль після плюса), закриваємо в плюс на
   buffer 0.1%.
3. **Backside peak**: якщо peak_gain ≥ 3% AND drawdown_from_peak
   ≥ 1.6% AND ≥ 2 послідовних cycle-tick-ів зниження → close.
   Логіка: top-pick-it-now, поки flop не став повним.
4. **Near-resolution** (≤ 300 c до резолюції):
   - Якщо PnL ≥ 4% — hold (близько до фінального outcome).
   - Якщо PnL ≤ -3% — close (втрачаємо менше).
5. **Neutral recycle**: age ≥ 120 хв AND |PnL| ≤ 0.25% — close
   (капітал-locked без edge-у, краще звільнити).
6. **Max hold time profit lock**: коли позиція досягла max-hold
   ліміту, закриваємо у поточному PnL.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_hold_minutes` | `2.0` | Min час перед першим exit-check |
| `hard_stop_loss_pct` | `18.0` | |
| `backside_activation_profit_pct` | `3.0` | Min peak gain для backside-логіки |
| `backside_drawdown_pct` | `1.6` | Drawdown від peak |
| `backside_confirm_cycles` | `2` | Cycles confirmation |
| `breakeven_arm_profit_pct` | `2.2` | Arm-поріг |
| `breakeven_buffer_pct` | `0.1` | Margin понад entry |
| `near_resolution_window_seconds` | `300.0` | 5 хв до резолюції |
| `near_resolution_hold_profit_pct` | `4.0` | Min для hold |
| `near_resolution_stop_loss_pct` | `3.0` | Cut-loss-cap |
| `neutral_exit_band_pct` | `0.25` | |

## Коли НЕ працює

- **Whipsaw markets**. Швидкий up-down-up may trigger backside-exit
  на «фальшивому» розвороті. Confirm_cycles=2 знижує false-positives,
  але не повністю.
- **Resolution dispute**. Якщо outcome визначений, але price не
  оновився, near-resolution-логіка може сказати «hold», а реальна
  resolution буде проти.
- **Початково проти-trade**. Якщо позиція з самого початку в
  мінусі та не вийшла за peak-gain ≥ 2.2%, спрацьовує тільки
  hard-stop (18%) — між цим bracket-ом захисту немає.

## Як використовувати

1. Купуємо ринок руками через Polymarket UI або через `BuyButton`
   у Homerun frontend.
2. Прив'язуємо позицію до бота, який має `traders_scope` з вашою
   wallet-адресою і завантажений `manual_manage_hold` стратегією.
3. Стратегія підхоплює open-position при наступному cycle-tick і
   починає stewart моніторити exit-conditions.

## Посилання

- [Tail-End Carry](tail-end-carry.md) — повний end-to-end
  high-prob carry strategy з власним exit (resolution-hold + smart
  TP), не потребує manual-manage.
- Detailed UI flow для manual buy: див.
  [`docs/UI_AND_DEMO_MODE.md`](../UI_AND_DEMO_MODE.md) (розділ про
  `BuyButton`).
