# Combinatorial Arbitrage

## Сутність

Найскладніша зі структурних: знаходить арбітраж між **залежними**
ринками, де перемога одного логічно впливає на інший. Наприклад: якщо
ринок A — «X виграє праймериз» і B — «X виграє вибори», то $1 на
NO у A автоматично виключає YES у B (логічна `IMPLIES`-залежність).
Combinatorial виявляє такі залежності евристиками + LLM-валідацією,
формулює integer programming задачу й знаходить спосіб купити
портфель за < $1, який гарантовано принесе $1 при будь-яких outcome-ах.

З коду в репозиторії: «17 218 conditions examined, 1 576 dependent
market pairs, $95 634 extracted» — реальний P&L підтверджує, що ці
залежності існують і ринки їх не завжди коректно ціняють.

## Контракт

- **Файл**: [`backend/services/strategies/combinatorial.py`](../../backend/services/strategies/combinatorial.py)
- **Клас**: `CombinatorialStrategy`
- **slug**: `combinatorial`
- **source_key**: `scanner`
- **Subscriptions**: `EventType.MARKET_DATA_REFRESH`
- **Ключові входи**: пари бінарних ринків, dependency-detector
  (евристики + опціонально LLM-judge), integer programming solver

## Логіка детекції

Багатоступеневий пайплайн на пари ринків (з кешем до 2 000 пар):

1. **Heuristic dependency detection** — патерни-кандидати:
   `candidate→party` («Trump виграє → Republicans виграють»),
   `price thresholds` («BTC > $50K» → «BTC > $40K»),
   `championship→playoffs`, date-orderings.
2. **Multi-source validation** — пріоритет:
   known patterns > structural checks > price consistency >
   contradiction detection.
3. **LLM judge** (опціонально) — на неоднозначних кейсах
   `min_llm_confidence=0.85` остаточно класифікує тип залежності
   (IMPLIES / EXCLUDES / CUMULATIVE).
4. **IP solver** — `constraint_solver.detect_cross_market_arbitrage()`
   формулює систему: купити X одиниць YES/NO у кожному ринку так,
   щоб у будь-якому outcome портфель платив ≥ $1, а total cost був
   < $1. Якщо така комбінація знаходиться — opportunity.

## Логіка виходу

Гарантований spread, тому `resolve_only`. Тримаємо до резолюції всіх
ринків у комбінації.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_edge_percent` | `3.0` | |
| `min_confidence` | `0.42` | |
| `max_risk_score` | `0.68` | |
| `min_markets` | `2` | Min ринків у комбінації |
| `low_confidence_thresholds` | `0.60` | LLM low |
| `medium_confidence_thresholds` | `0.75` | |
| `high_confidence_thresholds` | `0.90` | LLM high |
| `min_llm_confidence` | `0.85` | Min для прийняття LLM-судження |
| `max_dependency_cache` | `2000` | Розмір кешу проаналізованих пар |

## Коли НЕ працює

- **Хибно ідентифікована залежність**. Якщо детектор сказав
  `EXCLUDES`, а ринки насправді independent — IP-розвʼязок
  нелегітимний, і реальна виплата може бути < $1. LLM-валідація
  знижує цей ризик, але не до нуля.
- **Розрив дат резолюції**. Якщо один ринок резолвиться через місяць,
  а другий через рік — ваш капітал замкнений на цей рік. Stratery
  не має явного `max_resolution_spread_days`; додавайте через
  `default_config`-override.
- **Ринки на різних платформах**. Combinatorial працює тільки
  всередині Polymarket. Cross-platform залежності — це
  [Cross-Platform Oracle](cross-platform.md).
- **LLM-витрати**. На 17K ринків × попарне порівняння — економічно
  важливо обмежити LLM-запити (тому `max_dependency_cache=2000` і
  пріоритет heuristic-перш).

## Посилання

- [NegRisk Bundle Arb](negrisk.md) — okремий випадок Combinatorial
  для повних exclusive-наборів. Дешевше і надійніше, коли flag є.
- [Probability Surface Arb](prob-surface-arb.md) — для ринків зі
  threshold-axis (BTC > $X), де залежність монотонна.
