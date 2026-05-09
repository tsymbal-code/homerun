# Architecture: Frontend

The frontend is a React 19 + TypeScript single-page app served by an
nginx container that proxies `/api/*` and `/ws` to the backend. It is
built with Vite, styled with Tailwind + OpenUI + a stable of Radix
primitives, and held together by two state libraries: **Jotai** (for
local UI atoms) and **TanStack Query** (for server state).

For the wider topology see [System Overview](system-overview.md).

## Purpose

This layer is responsible for:

1. Rendering the operator console — opportunities, bots, positions,
   strategies, traders, AI, settings.
2. Keeping server state synchronised in near-real-time via a single
   WebSocket connection, with HTTP polling as a fallback.
3. Persisting per-user UI preferences in `localStorage` (theme,
   selected account, AI sub-tab, etc.) via `atomWithStorage`.
4. Embedding rich editors (CodeMirror for Python/JSON), a market map
   (MapLibre), candlestick charts (lightweight-charts) and an
   AI-chat surface.

It is **not** authoritative for any business state. Everything mutable
is owned by the backend; the frontend caches and projects.

## Source tree

```
frontend/
├── index.html
├── vite.config.ts            # Vite + dev /api,/ws proxy to :8000
├── tailwind.config.js
├── nginx.conf                # production proxy → backend:8000
├── package.json
├── public/                   # static assets
└── src/
    ├── main.tsx              # JotaiProvider → QueryClientProvider → App
    ├── App.tsx               # tab shell, global shortcuts, WS bootstrap
    ├── index.css             # Tailwind layers + theme variables
    ├── components/
    │   ├── ai/               # AITab, ChatView, ProvidersView, ModelsView
    │   ├── ui/               # Radix-based primitives (Button, Card, …)
    │   ├── *.tsx             # one panel per top-level concern
    │   └── tradingPanelFlyoutShared.tsx
    ├── hooks/                # useWebSocket, useRealtimeInvalidation, …
    ├── services/             # axios calls grouped per domain
    │   ├── apiClient.ts      # baseURL='/api', UTC normalisation
    │   ├── apiCore.ts        # opportunities, markets, simulation
    │   ├── apiTraders.ts     # bots, orchestrator
    │   ├── apiSettings.ts    # AppSettings CRUD
    │   ├── apiBacktest.ts
    │   ├── apiIntelligence.ts
    │   └── ...               # one module per backend route domain
    ├── store/
    │   └── atoms.ts          # Jotai atoms (theme, selected account, …)
    ├── lib/
    │   └── utils.ts          # cn(), classnames helpers, time utils
    └── data/                 # static JSON (preset markets etc.)
```

A few conventions to know upfront:

- **One panel per top-level concern** in `src/components/`. The file
  per panel is large (some 13k+ lines) on purpose — these are the
  operator's console screens, and locality matters more than
  refactoring into ten small files. Cross-panel reuse goes through
  `components/ui/` and `components/tradingPanelFlyoutShared.tsx`.
- **Services mirror backend route domains.** A new backend
  `routes_<domain>.py` gets a matching `services/api<Domain>.ts`
  with typed wrappers. `apiClient.ts` is the only place that owns
  the axios instance and base URL.

## Bootstrap (`main.tsx`, `App.tsx`)

[main.tsx](../../../frontend/src/main.tsx) wires three providers:

```tsx
<JotaiProvider>
  <QueryClientProvider client={queryClient}>
    <App />
  </QueryClientProvider>
</JotaiProvider>
```

The `QueryClient` is configured for a WebSocket-first model:

| Option | Value | Why |
|---|---|---|
| `retry` | `false` | A 4xx from the backend is usually meaningful; we don't want autosilent retries muddying the logs. |
| `refetchInterval` | `120000` ms | A safety net only. Fresh data normally arrives via WebSocket invalidation. |
| `refetchOnWindowFocus` | `false` | Same reason — focus is not a meaningful sync signal here. |
| `staleTime` | `30000` ms | A query stays "fresh" for 30 s; a WS push within that window simply updates the cache without refetch. |

`main.tsx` also polyfills `crypto.randomUUID` for non-secure HTTP
contexts (the desktop launcher and LAN deployments don't always have
TLS), which OpenUI relies on.

`App.tsx` is the shell. It owns:

- The active tab state (`activeTab: 'opportunities' | 'trading' |
  'positions' | 'performance' | 'accounts' | 'strategies' |
  'traders' | 'data' | 'ai' | 'settings'`).
- The single WebSocket subscription via `useWebSocket('/ws', ...)`.
- The cache-invalidation pump
  (`useRealtimeInvalidation(lastMessage, queryClient, ...)`).
- Global keyboard shortcuts (`useKeyboardShortcuts`).
- The `AccountModeSelector` and `AccountSettingsFlyout` mounts.

Tabs are lazy-loaded (`React.lazy` + `Suspense`) so that the initial
bundle stays under what nginx will gzip happily.

## State management

### Jotai (UI state)

[`src/store/atoms.ts`](../../../frontend/src/store/atoms.ts) holds a
small, deliberate set of atoms. Categories:

- **Theme** — `themePreferenceAtom` ('dark' | 'light' | 'system')
  via `atomWithStorage`, derived `themeAtom` and `themeClassAtom`.
- **Account selection** — `accountModeAtom` ('sandbox' | 'live'),
  `selectedAccountIdAtom` (UUID for sandbox, `live:polymarket` /
  `live:kalshi` for live). Both `atomWithStorage`. Read by
  `AccountModeSelector`, `TradingPanel`, `BuyButton`, anywhere
  execution mode matters.
- **UI panel state** — open/closed atoms for shortcuts dialog,
  command bar, AI copilot.
- **Draft fields** — `draftNameAtom`, `draftIntervalAtom`,
  `draftRiskValuesAtom`, `draftTradingScheduleAtom`. Owned by the
  inputs themselves so a single keystroke re-renders only the
  flyout, not the whole `TradingPanel` (which subscribes to ~20
  queries). The pattern: the panel reads these via `useStore().get()`
  on save, never via `useAtomValue`, so it doesn't subscribe.

The rule of thumb for Jotai vs react-query: **client-only state goes
to atoms; server-derived state goes to react-query.** There is no
Redux, no zustand.

### TanStack Query (server state)

Every backend GET goes through a typed wrapper in `services/api*.ts`
and is consumed via `useQuery({ queryKey, queryFn })`. Mutations
use `useMutation` and invalidate by query key on success.

Conventions:

- **Query keys are tuples** of strings: `['opportunities']`,
  `['simulation-accounts']`, `['settings']`,
  `['live-positions']`. They are typed as `QueryKey` in each call
  site; we don't centralise key constants.
- **Refetch intervals are explicit** when they matter:
  - 5–10 s for hot lists (opportunities, prices) when the panel is
    visible.
  - 15 s for balance/positions polling.
  - Disabled for static data (settings, model list, strategy code).
- **`enabled: false`** is used to gate dependent queries (e.g.
  `kalshi-positions` enabled only after `kalshi-status` reports
  `authenticated`).
- **`staleTime`** on per-query basis when defaults aren't right —
  e.g. 30 s for the traders list, 5 min for the AI model catalog.

## WebSocket pipeline

Two hooks make this work; they are the only WS-aware code.

### `useWebSocket` (the singleton client)

Source: [hooks/useWebSocket.ts](../../../frontend/src/hooks/useWebSocket.ts).

- One WebSocket per browser session, even with multiple components
  calling the hook (shared module-level state in lines 11-24).
- URL: dynamic — `${ws|wss}://${location.host}/ws`. In dev, Vite
  proxies that to `ws://localhost:8000` (`vite.config.ts:42`).
- Reconnect: exponential backoff, 1 s → 10 s ceiling.
- Keepalive: client sends `{type: 'ping'}` every 15 s.
- UI presence: `{type: 'ui_presence', visible}` on visibility
  change so the backend can stop pushing high-frequency updates to
  hidden tabs.
- Filtering: callers pass `messageTypes` to receive only those. The
  hook normalises UTC timestamps in place so consumers don't have
  to.

### `useRealtimeInvalidation`

Source: [hooks/useRealtimeInvalidation.ts](../../../frontend/src/hooks/useRealtimeInvalidation.ts).

Routes the latest WS message to react-query cache invalidation. Two
mechanics worth knowing:

1. **Debounce window** of 120 ms (`INVALIDATION_DEBOUNCE_MS`). Bursts
   of WS messages (e.g. order-book deltas) collapse into a single
   `invalidateQueries` per query key. Without this the UI thrashes.
2. **Context-aware invalidation.** It takes `{activeTab, opportunitiesView,
   dataView}` and decides which keys to refresh. A wallet-state
   message invalidates the bots list only if the trading tab is
   visible; otherwise it just queues for the next tab switch.

The combination — WebSocket + smart invalidation + 30 s staleTime —
gives sub-second UI updates without overflowing react-query's stale
counter.

## HTTP / axios

[`services/apiClient.ts`](../../../frontend/src/services/apiClient.ts)
exports the singleton `api`:

```ts
export const api = axios.create({ baseURL: '/api', timeout: 60_000 })
```

Three responsibilities live there:

- **Request/response interceptor** — logs in dev, tags the
  `X-Request-ID` if present.
- **UTC normalisation** — every ISO timestamp coming back from the
  server is normalised so the frontend renders it via the user's
  locale without timezone surprises.
- **Pagination unwrap** (`unwrapApiData`) — the backend returns
  `{ items, total, ... }` for paginated endpoints; this unwraps to
  just `items` when the call site asked for the bare list.
- **UI-lock 423 handling** — when the operator has locked the UI
  (Settings → UI Lock), API responses come back as 423 Locked. The
  interceptor surfaces a friendly modal instead of letting react-query
  treat it as a fetch error.

In production nginx serves the SPA from `:3000` and proxies `/api/*`
to `backend:8000`. In dev, Vite does the same proxy itself
(`vite.config.ts:36-57`). Either way, the frontend never has to
know what host the backend lives on — same-origin everything.

## Styling and component primitives

- **Tailwind CSS** is the styling primitive. Theme tokens are HSL
  CSS variables defined per `.theme-dark` / `.theme-light` in
  `index.css`.
- **OpenUI** (`@openuidev/react-ui`) provides the chart components
  and a small set of layout primitives. Its CSS must be loaded
  **before** Tailwind, otherwise our theme variables get overridden;
  `main.tsx:23-25` enforces the order.
- **Radix primitives** (`@radix-ui/*`) are the basis for accessible
  dialogs, popovers, scroll areas, tabs, tooltips — wrapped under
  `components/ui/` with our own styling.
- **Icons** come from `lucide-react`. Stick with it; no custom SVGs
  for first iterations of new providers/strategies.
- **Code editors** use CodeMirror 6 (Python and JSON langs configured
  in `CodeEditor.tsx`).
- **Charts** use `lightweight-charts` for price candles and
  `framer-motion` for transitions.

## Dependencies (both directions)

**This layer depends on:**

- The backend's `/api/*` and `/ws` contracts (no other endpoints).
- Browser APIs: `localStorage`, `WebSocket`, `crypto.randomUUID`
  (polyfilled), `IntersectionObserver` (for virtualisation).
- Build tooling: Vite, TypeScript 5.3.

**Depended on by:**

- The operator. There is no other consumer; this is not a public API
  surface.

## Extension points

| When you want to… | Touch |
|---|---|
| Add a new top-level tab | Extend `Tab` union and `NAV_ITEMS` in `App.tsx`, add a lazy import for the new panel component, and wire up its content in the `activeTab === '...'` branch. |
| Add a new server query | Add a typed function in the matching `services/api*.ts`, then `useQuery({queryKey: ['...'], queryFn})` at the call site. |
| React to a new WebSocket message type | Extend the `messageTypes` filter passed to `useWebSocket`, then handle it inside `useRealtimeInvalidation` (or the panel that cares). Don't add ad-hoc WS subscriptions — there is one client. |
| Add a UI atom | Append to `store/atoms.ts`. Use `atomWithStorage` for things that should survive a page reload (theme, selected account); plain `atom` for ephemeral UI state. |
| Add a settings tile | Pattern-match against `AccountSettingsFlyout.tsx` (creds) or `AIProvidersView.tsx` (provider config). Both consume `getSettings`/`updateSettings` from `apiSettings.ts`. |
| Add a new modal/dialog | Use `@radix-ui/react-dialog` (or our `components/ui/dialog.tsx` wrapper). Don't create your own focus-trap logic. |

## Known footguns

- **Don't open a second WebSocket.** Calling `new WebSocket(...)`
  outside the hook will work in dev but multiplies backend load and
  duplicates push handlers. Always go through `useWebSocket`.
- **`atomWithStorage` reads localStorage on first render.** If the
  stored value's shape changes, write a one-time migration in the
  atom's initializer or it'll stay broken until the user clears
  storage. The selected-account atom does this for the `live:` /
  UUID transition.
- **Don't subscribe to draft atoms in `TradingPanel`.** They exist to
  prevent panel re-renders during typing; subscribing through
  `useAtomValue` defeats that. Use `useStore().get()` inside save
  closures, `useSetAtom` to write.
- **`refetchInterval` in a query** still polls when the tab is in the
  background — the OS doesn't pause Promise scheduling. Pair it with
  `enabled: tabIsVisible` if the endpoint is expensive.
- **The backend may push large bursts** (price coalescing collapses
  100 ms windows). The 120 ms invalidation debounce in
  `useRealtimeInvalidation` is what keeps react-query from running
  in a tight loop. Don't lower it lightly.

Last verified: 2026-05-08
