# News Edge

## Сутність

Стратегія шукає **інформаційні мисспрайси**: коли свіжі новинні
статті семантично зв'язані з ринком, але ринкова ціна ще не
відобразила їхній зміст. Pipeline такий: семантичний матчинг
статей до питань ринку (FAISS + sentence-transformers ембедінги),
LLM-оцінка ймовірності події з огляду на новину, порівняння
LLM-probability vs market price, edge → opportunity.

Це **async**-стратегія (`detect_async`), бо LLM-виклики мають
latency. Працює на `worker-news`, бо там живе тяжкий ML-стек.

## Контракт

- **Файл**: [`backend/services/strategies/news_edge.py`](../../backend/services/strategies/news_edge.py)
- **Клас**: `NewsEdgeStrategy`
- **slug**: `news_edge`
- **source_key**: `news`
- **Subscriptions**: `EventType.NEWS_UPDATE`
- **Ключові входи**: news articles з RSS / REST / Twitter,
  semantic embeddings (FAISS index), LLM-judge через
  `LLMManager.structured_output`, ринкові ціни

## Логіка детекції

1. На `NEWS_UPDATE`-event: семантичний матчер знаходить ринки,
   найближчі до тексту статті за cosine similarity.
2. Для кожної кандидата-пари (article × market) LLM (`opportunity_judge`
   або per-purpose model з `llm_model_assignments`) оцінює:
   - наскільки новина впливає на ймовірність outcome,
   - в яку сторону,
   - confidence власної оцінки.
3. Якщо `|llm_probability - market_price| ≥ min_edge_percent / 100`
   AND ≥ `min_supporting_articles` (2) AND ≥ `min_supporting_sources`
   (2) — фіксує opportunity на сторону, де LLM вище.
4. Гейтиться `max_signal_age_minutes=60` — стара новина не edge.

## Логіка виходу

`max_hold_minutes=60` за замовчуванням, або стандартний TP/SL (TP 70%
для агресивних news-trades). News-alpha decay-ить швидко: за годину
ринок зазвичай поглинає інформацію.

## Налаштування за замовчуванням

| Ключ | Значення | Сенс |
|---|---|---|
| `min_edge_percent` | `5.0` | Min |LLM_prob - market_price| у % |
| `min_confidence` | `0.45` | |
| `orchestrator_min_edge` | `10.0` | Вищий поріг для autotrader-execution |
| `require_verifier` | `True` | Перевірка через verifier-агента |
| `require_second_source` | `False` | За замовчуванням single-source ОК |
| `min_supporting_articles` | `2` | Min статей-підтверджень |
| `min_supporting_sources` | `2` | Min джерел |
| `max_signal_age_minutes` | `60` | News stale gate |

## Коли НЕ працює

- **LLM hallucinates**. Слабкі/маленькі моделі дають невірний
  imputed-probability. Тримайте `min_confidence` високим, перевіряйте
  через `verifier`-агента.
- **News вже включена в ціну**. Якщо новина годинами на головній
  сторінці CNN, ринок її давно зловив. `max_signal_age_minutes`
  частково захищає.
- **Single-source bias**. Один Twitter-account, навіть авторитетний,
  може помилятися. Дефолт `min_supporting_sources=2`.
- **Cost**. Кожна opportunity = LLM-call(s). Бюджет —
  `AppSettings.ai_max_monthly_spend`.

## Посилання

- [News Momentum Breakout](news-momentum-breakout.md) — ставить на
  продовження news-руху (без LLM).
- [LLM Provider Layer](../plans/architecture/llm-provider-layer.md) —
  як працює `LLMManager`, де налаштовуються моделі.
