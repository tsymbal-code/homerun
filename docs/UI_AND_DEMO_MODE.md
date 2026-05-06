# Homerun UI та демо-режим (Sandbox / Shadow)

Цей документ пояснює, як влаштована UI-частина Homerun, що саме означає
«демо-режим», як він інтегрований у проєкт, і як локально протестувати
сценарій роботи з Polymarket без реального акаунта і реальних коштів.

---

## 1. Архітектура UI

### 1.1 Стек і запуск

- **Frontend** — React 19 + TypeScript + Vite, стейт через Jotai
  (`atomWithStorage` для persist у `localStorage`), запити через
  `@tanstack/react-query`. Білд віддається з контейнера `homerun-frontend`
  (nginx, порт `3000`).
- **Backend** — FastAPI + async SQLAlchemy (Postgres) + Redis. У docker-compose
  розгортається трьома планами воркерів (`worker-trading`, `worker-news`,
  `worker-discovery`), окремий контейнер `migrate` (Alembic) і `backend`
  (uvicorn).
- nginx у фронтенд-контейнері (`frontend/nginx.conf`) проксіює `/api/`
  і `/ws` на `http://backend:8000` всередині docker-мережі. Хост-порт
  бекенду налаштовується змінною `BACKEND_PORT` у `.env` (у вашому випадку
  `8888`), фронтенду — `FRONTEND_PORT` (за замовчуванням `3000`).

### 1.2 Навігація

Головна навігація живе в [App.tsx:160](../frontend/src/App.tsx) у
константі `NAV_ITEMS`:

| Tab | Файл | Призначення |
|---|---|---|
| `opportunities` | `OpportunityTable.tsx` / `OpportunityCard.tsx` | Лента можливостей зі сканера; кнопка **Buy** робить запит до `BuyButton.tsx` |
| `trading` (Bots) | `TradingPanel.tsx` | Список ботів-«трейдерів», конфігурація, запуск shadow/live циклу |
| `positions` | `PositionsPanel.tsx` | Відкриті позиції — sandbox + live |
| `performance` | `PerformancePanel.tsx` | Equity-крива, ROI, drawdown |
| `accounts` | `AccountsPanel.tsx` | Огляд акаунтів (Sandbox Desk / Live Desk) |
| `strategies` | `UnifiedStrategiesManager.tsx` | Стратегії як Python-класи в БД, hot-reload |
| `traders` | `TradersNetworkPanel.tsx` та ін. | Discovery, копі-трейдинг, аналіз гаманців |
| `data` | `DataLab.tsx`, `WorldMap.tsx` | Джерела даних (RSS, REST, X, Chainlink…) |
| `ai` | `AITab.tsx` | LLM-копайлот, агенти |
| `settings` | `SettingsPanel.tsx` | Глобальні параметри |

Над контентом — глобальний контрол-бар із селектором акаунта
[`AccountModeSelector.tsx`](../frontend/src/components/AccountModeSelector.tsx)
([App.tsx:1861](../frontend/src/App.tsx)) і кнопкою, що відкриває
flyout [`AccountSettingsFlyout.tsx`](../frontend/src/components/AccountSettingsFlyout.tsx)
([App.tsx:3187](../frontend/src/App.tsx)).

### 1.3 Глобальний стан акаунта

Уся UI-частина працює навколо двох атомів у
[`store/atoms.ts`](../frontend/src/store/atoms.ts):

```ts
// 'sandbox' = демо-режим (симуляція), 'live' = реальні гроші
export const accountModeAtom = atomWithStorage<AccountMode>('accountMode', 'sandbox')

// Для sandbox — UUID симуляційного акаунта,
// Для live — спецзначення 'live:polymarket' або 'live:kalshi'
export const selectedAccountIdAtom = atomWithStorage<string | null>('selectedAccountId', null)

export const isLiveAccountAtom = atom((get) => get(selectedAccountIdAtom)?.startsWith('live:') ?? false)
```

Селектор `AccountModeSelector` пише в обидва атоми; усі інші панелі
читають їх і відповідно змінюють поведінку (наприклад, `BuyButton`
показує бейдж **Live** червоним, додає крок підтвердження
[`BuyButton.tsx:585`](../frontend/src/components/BuyButton.tsx)).

---

## 2. Що таке «демо-режим» у Homerun

У проєкті є дві різні (але пов’язані) симуляції — і важливо їх не плутати:

### 2.1 Sandbox-акаунт (UI-рівень)

Це і є «тестовий акаунт користувача»: окрема сутність у БД
(`SimulationAccount`,
[`backend/models/database.py:595`](../backend/models/database.py)) з
балансом, лімітами і власним списком трейдів/позицій
(`SimulationTrade`, `SimulationPosition`).

Під капотом — звичайний CRUD-сервіс
[`services/simulation.py`](../backend/services/simulation.py) і REST
ендпоінти у [`api/routes_simulation.py`](../backend/api/routes_simulation.py):

| Метод | Шлях | Призначення |
|---|---|---|
| `POST` | `/api/simulation/accounts` | Створити sandbox-акаунт |
| `GET` | `/api/simulation/accounts` | Список усіх sandbox-акаунтів |
| `GET` | `/api/simulation/accounts/{id}` | Детальна стата |
| `DELETE` | `/api/simulation/accounts/{id}` | Видалити |
| `GET` | `/api/simulation/accounts/{id}/positions` | Відкриті позиції |
| `GET` | `/api/simulation/accounts/{id}/trades` | Історія трейдів |
| `GET` | `/api/simulation/accounts/{id}/equity-history` | Точки equity-кривої |
| `POST` | `/api/simulation/accounts/{id}/execute` | Виконати opportunity у симуляції |
| `POST` | `/api/simulation/trades/{id}/resolve` | Вручну закрити трейд (для тестів) |

Жоден із цих ендпоінтів не торкається реального CLOB Polymarket — він
лише списує/нараховує `current_capital` у локальній таблиці
([`simulation.py:266`](../backend/services/simulation.py)).

### 2.2 Shadow-mode (виконавчий рівень)

Кожен бот / стратегія має поле `mode: 'shadow' | 'live'`. У `shadow`
ордер не йде в Polymarket CLOB — натомість його «виконує»
microstructure-aware симулятор:

- `services/fill_simulator/` — Cox proportional hazards модель
  ймовірності філу (з ансамблем pessimistic / realistic / optimistic);
- `services/simulation/execution_simulator.py` — пайплайн «розрахунок
  затримок → filling → запис фейкового філу»;
- `simulation_service.record_orchestrator_shadow_fill(...)`
  ([`simulation.py:284`](../backend/services/simulation.py)) — записує
  результат виконання в той самий `SimulationAccount`, який вибраний
  у UI.

Тобто **sandbox-акаунт = «гаманець» для shadow-режиму**: коли бот у
shadow-режимі «купує» позицію, гроші списуються з обраного
sandbox-акаунта, P&L пишеться в його ledger, equity-крива оновлюється
для нього ж.

### 2.3 Як ці два рівні пов’язані з вибором у UI

```
┌──────────────────────┐    selected_account_id    ┌──────────────────────┐
│ AccountModeSelector  │ ────────────────────────► │ TradingPanel /        │
│  - mode: sandbox     │                            │ trader_orchestrator  │
│  - account_id: <sim> │                            │  mode='shadow'       │
└──────────────────────┘                            └──────────┬───────────┘
                                                                │
                                                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ services/simulation/execution_simulator.py  (Cox fill model + slippage) │
│                                                                          │
│  → simulation_service.record_orchestrator_shadow_fill(                   │
│        account_id=selected_account_id, ... )                             │
└─────────────────────────────────────────────────────────────────────────┘
                                                                │
                                                                ▼
                                       SimulationTrade + SimulationPosition
                                       у Postgres (зміна current_capital)
```

Ключові точки в коді:

- [`AccountModeSelector.tsx:107`](../frontend/src/components/AccountModeSelector.tsx) — сетер `setMode('sandbox')` / `'live'` залежно від префікса `live:`;
- [`TradingPanel.tsx:6524`](../frontend/src/components/TradingPanel.tsx) — `startTraderOrchestrator({ mode: 'shadow', selected_account_id })` (не запустить shadow-цикл, якщо sandbox-акаунт не вибраний);
- [`trader_orchestrator/session_engine.py:392`](../backend/services/trader_orchestrator/session_engine.py) — гілка коду, що обходить реальне розміщення ордера, якщо `mode != 'live'`;
- [`simulation.py:284`](../backend/services/simulation.py) — `record_orchestrator_shadow_fill` (фактичне «виконання» в shadow-режимі).

### 2.4 Чому Polymarket-ключі НЕ потрібні для демо-режиму

Перевірка автентифікації в Polymarket виконується тільки коли вмикається
`live`-режим: див. `live_execution_service._resolve_polymarket_credentials`
([`routes_orchestrator_live.py:528`](../backend/api/routes_orchestrator_live.py)).
У `shadow`/sandbox код обирає гілку без реального CLOB, а Polymarket-клієнт
використовується **тільки для читання публічних маркетів і
order-book-ів** — це не потребує приватного ключа чи API-credentials.

Тому в `.env` поля `POLYMARKET_*` можна (і потрібно) залишити порожніми
для демо-сценарію — саме так у вас зараз і налаштовано.

---

## 3. Як створити тестовий (sandbox) акаунт

> **Важливо**: на момент написання UI-форму створення sandbox-акаунта
> з кнопкою у фронтенді ще не реалізовано. У `AccountsPanel.tsx:807`
> присутній текст «No sandbox accounts yet. Create one in Sandbox Desk.»,
> але сам ендпоінт `POST /api/simulation/accounts` поки викликається
> тільки через REST (підтвердив пошук — використань
> `createSimulationAccount` з `apiCore.ts` у компонентах немає).
> Тому акаунт треба створити вручну через API, після чого UI його
> підхопить автоматично через `getSimulationAccounts` (poll кожні 10 с).

### 3.1 Через Swagger UI (найпростіше)

1. Відкрийте `http://localhost:8888/docs` (порт відповідає `BACKEND_PORT`
   у вашому `.env`).
2. Знайдіть секцію **simulation** → `POST /api/simulation/accounts`.
3. **Try it out** і відправте, наприклад, такий тіло:

   ```json
   {
     "name": "Demo PM Account",
     "initial_capital": 10000,
     "max_position_pct": 10,
     "max_positions": 10
   }
   ```

4. У відповіді буде `account_id` (UUID).

### 3.2 Через `curl`

```bash
curl -X POST http://localhost:8888/api/simulation/accounts \
  -H 'Content-Type: application/json' \
  -d '{"name":"Demo PM Account","initial_capital":10000,"max_position_pct":10,"max_positions":10}'
```

Параметри (валідація у
[`routes_simulation.py:15`](../backend/api/routes_simulation.py)):

- `name` — 1..100 символів;
- `initial_capital` — від `100` до `10_000_000` (за замовчуванням `10000`);
- `max_position_pct` — % капіталу на одну позицію (`1..100`, дефолт `10`);
- `max_positions` — максимум одночасно відкритих (`1..100`, дефолт `10`).

### 3.3 Перевірка в UI

Після створення:

1. Відкрийте `http://localhost:3000`.
2. У верхній панелі натисніть кнопку селектора акаунта (іконка `Shield` /
   `Zap`). Розділ **Sandbox Accounts** має містити свіжостворений акаунт.
3. Виберіть його — `accountModeAtom` стане `'sandbox'`,
   `selectedAccountIdAtom` — UUID цього акаунта.
4. Перейдіть на вкладку **Accounts** — побачите Sandbox Desk із
   нульовими позиціями та повним балансом.

`AccountModeSelector` на старті автоматично вибере перший доступний
sandbox-акаунт (
[`AccountModeSelector.tsx:50`](../frontend/src/components/AccountModeSelector.tsx)),
тому вам не доведеться щоразу його перемикати.

---

## 4. Що потрібно для локального тестування демо-сценарію

### 4.1 Передумови

- Docker-стек уже піднятий (з вашого `.env` видно, що так).
- `APP_SECRETS_KEY` заповнений — **обов’язково** (без нього бекенд не
  стартує). У вашому `.env` він є.
- Поля `POLYMARKET_*` залишаються порожніми — ми тестуємо саме демо.
- Відкриті порти: `3000` (UI), `8888` (API). Postgres `5432` і Redis `6379`
  слухають тільки на `127.0.0.1`.

### 4.2 Перевірка стеку

```bash
docker compose ps
docker compose logs -f backend          # лайв-лог API
curl http://localhost:8888/health/live  # повинно повернути 200
curl http://localhost:8888/api/simulation/accounts   # порожній масив [] на свіжій БД
```

### 4.3 Кроки тесту

1. **Створити sandbox-акаунт** — як у розділі 3.
2. **Відкрити UI**: `http://localhost:3000`. Селектор акаунта вгорі
   має показати назву акаунта та поточний капітал
   ([`AccountModeSelector.tsx:88`](../frontend/src/components/AccountModeSelector.tsx)).
3. **Зачекати на сканер**. Воркер `worker-discovery` + сканер у
   `worker-trading` поступово наповнюють `Opportunity` із публічних
   ринків Polymarket. На вкладці **Opportunities** з’являться картки.
   *Зауваження*: на холодному старті це може зайняти декілька хвилин,
   доки воркери підтягнуть universe маркетів і кеш.
4. **Виконати ручний трейд**:
   - Натисніть **Buy** на будь-якій картці opportunity →
     `BuyButton.tsx` відкриє модалку.
   - У списку **Execute Via** мають бути боти, які створюються через
     **Bots** (вкладка `trading`) — створіть бота в `mode: shadow` і
     прив’яжіть до sandbox-акаунта.
   - Підтвердіть Buy → виклик `traderManualBuy` → бекенд проганяє ордер
     через shadow-симулятор → запис у `simulation_trades` /
     `simulation_positions` для вашого акаунта.
5. **Запустити автоматичний цикл (необов’язково)**: на вкладці
   **Bots** → Start. Атомік-перевірка
   ([`TradingPanel.tsx:6507`](../frontend/src/components/TradingPanel.tsx))
   не пропустить запуск без вибраного sandbox-акаунта; з вибраним —
   запускається `trader_orchestrator` у shadow.
6. **Подивитися результат**:
   - **Positions** — відкриті позиції;
   - **Performance / Sandbox Desk** — equity-крива, ROI, win-rate;
   - `GET /api/simulation/accounts/{id}/trades` — повний лог.
7. **(Опційно) Закрити трейд вручну** для тесту лайфциклу:
   ```bash
   curl -X POST "http://localhost:8888/api/simulation/trades/<TRADE_ID>/resolve?winning_outcome=YES"
   ```
   (Цей ендпоінт призначений саме для ручних тестів —
   [`routes_simulation.py:344`](../backend/api/routes_simulation.py).)

### 4.4 Що НЕ робити в демо-сценарії

- Не вмикайте режим `Live` у селекторі акаунтів і не натискайте
  «Confirm Live Order» у `BuyButton` — без `POLYMARKET_*` ключів запит
  все одно не пройде, але краще не плутати UI-стан.
- Не задавайте `POLYMARKET_PRIVATE_KEY` у `.env`, якщо не плануєте
  лайв-трейдинг: достатньо помилкове налаштування — і
  `live_execution_service` спробує підключитись до CLOB.
- Не плутайте **Backtest** (вкладка Strategies → Research → Backtest
  Suite, файли в `services/backtest/`) із sandbox-режимом. Backtest —
  історична reply з персистнутих L2-снепшотів, sandbox — поточні живі
  ціни + симуляція виконання у тому самому ledger, що зберігається
  між перезапусками.

### 4.5 Очищення між тестами

```bash
# видалити sandbox-акаунт (разом з усіма його трейдами/позиціями)
curl -X DELETE http://localhost:8888/api/simulation/accounts/<ACCOUNT_ID>

# або повний reset стеку (включно з Postgres-волюмом)
docker compose down -v
docker compose up -d
```

---

## 5. Швидкий чек-ліст

- [ ] `.env`: `APP_SECRETS_KEY` заповнено, `POLYMARKET_*` — порожні.
- [ ] `docker compose ps` показує `homerun-backend`, `homerun-frontend`,
      три воркери, `postgres`, `redis` як `running` / `healthy`.
- [ ] `curl http://localhost:8888/health/live` → `200`.
- [ ] `POST /api/simulation/accounts` створив акаунт.
- [ ] У UI селектор показує цей акаунт у розділі **Sandbox Accounts**.
- [ ] Через декілька хвилин у вкладці **Opportunities** з’являються
      картки.
- [ ] Buy на shadow-боті проходить, у `Sandbox Desk` оновлюються
      позиції / equity.
