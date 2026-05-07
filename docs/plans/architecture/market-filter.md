# Architecture: Market Filter Pipeline

The market filter pipeline is the funnel that turns Polymarket's raw
~250 K-row gamma feed into the few-thousand-row trading universe each
worker plane sees. It runs **inside `worker-trading`** during every
`refresh_catalog` cycle and again during the cached-merged-scan path.
The same code path is reused by Kalshi (where the input volume is
already small).

## Purpose

This layer owns three responsibilities, applied in this order:

1. **Tag observation.** Record every `tag` string that appears on any
   raw market or event into `market_tags_seen`. The "raw" snapshot is
   the only place where dropped-tag information survives — once the
   filter prunes a market, the tag is gone for the rest of the cycle.
   This is what feeds the operator-facing tag chooser in
   `Settings → Scanner`.
2. **Tag whitelist.** When the operator has selected one or more tags,
   drop any market whose `(market.tags ∪ event.tags)` intersection
   with the whitelist is empty. OR-logic only — a market keeps as
   long as **at least one** tag matches.
3. **Tradability gate** (existing — see
   [`scanner.py`](../../../backend/services/scanner.py)
   `_is_market_tradable`). `accepting_orders=True`, `volume>0`,
   non-empty `condition_id` and `clob_token_ids`.

When the whitelist is empty (the default), step 2 is a no-op and the
behaviour matches what the scanner did before this layer existed.

## Funnel diagram

```
Polymarket gamma ──┐
                   │   /markets/keyset (paginated)
                   ▼
         get_all_markets / get_recent_markets
                   │   raw Market[], Event[]
                   ▼
   ┌───────────────────────────────────────┐
   │ market_tag_aggregator                 │
   │ record_tags_from_markets(             │
   │   session, events, markets)           │
   │ → INSERT … ON CONFLICT (tag) DO       │
   │   UPDATE SET last_seen=excluded.last_ │
   │   seen, occurrences=…+1               │
   └───────────────┬───────────────────────┘
                   │ same Market[], Event[] (no mutation)
                   ▼
   ┌───────────────────────────────────────┐
   │ scanner._filter_tradable_markets      │
   │  ├─ tag whitelist OR-match (if any)   │
   │  └─ _is_market_tradable per row       │
   │     ⇒ events without kept children    │
   │       are dropped                     │
   └───────────────┬───────────────────────┘
                   │ filtered Market[], Event[]
                   ▼
              market_catalog
              ↓
   scanner cache, opportunity dispatcher,
   strategy fan-out, downstream worker planes
```

## Key files

| Path | What it holds |
|---|---|
| [`backend/services/polymarket.py:597`](../../../backend/services/polymarket.py) | `get_all_markets` / `get_recent_markets` — raw entry point. Does **not** filter; emits `Market.from_gamma_response` rows verbatim. |
| [`backend/models/market.py`](../../../backend/models/market.py) | `Market.tags` and `Event.tags` are normalised here (`_extract_tags` semantics — string list, dict-`{label,name}` collapse, empty values dropped). The aggregator and the whitelist both consume these post-normalisation fields. |
| [`backend/services/market_tag_aggregator.py`](../../../backend/services/market_tag_aggregator.py) | One pure function `record_tags_from_markets(session, events, markets)`. Every ingest cycle calls it once with the raw lists, before any filter. Failure is logged and swallowed — never blocks ingest. |
| [`backend/services/scanner.py:937`](../../../backend/services/scanner.py) | `_is_market_tradable` — sync per-row tradability check. Extended with an optional `whitelist_tags` parameter that the caller passes in. |
| [`backend/services/scanner.py:980`](../../../backend/services/scanner.py) | `_filter_tradable_markets` — the funnel. Builds an `event-tags-by-slug` map, applies tag whitelist + tradability, drops events without surviving children. |
| [`backend/api/routes_settings.py`](../../../backend/api/routes_settings.py) | `GET /settings/market-filter/available-tags` (new) and the extended `GET/PUT /settings/scanner` carrying `market_filter_tags`. |
| [`backend/models/database.py`](../../../backend/models/database.py) | `MarketTagSeen` ORM, plus the new `app_settings.market_filter_tags` and `app_settings.market_filter_updated_at` columns. |
| [`frontend/src/components/SettingsPanel.tsx`](../../../frontend/src/components/SettingsPanel.tsx) | Scanner tab → "Market Tag Filter" section: chip-style multi-select bound to `scannerSettings.market_filter_tags`. |

## Contracts

### `MarketTagSeen` (ORM)

```python
class MarketTagSeen(Base):
    __tablename__ = "market_tags_seen"

    tag = Column(String, primary_key=True)
    first_seen = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    last_seen = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
    occurrences = Column(BigInteger, nullable=False, default=1)

    __table_args__ = (
        Index("idx_market_tags_seen_last_seen", "last_seen"),
    )
```

Retention policy:

- **Hot rows** — used by the API: `last_seen > now() - interval '24 hours'`.
- **Physical prune** — rows with `last_seen < now() - interval '7 days'`
  are deleted by the `worker-discovery` slow-cleanup loop. Physical
  prune is non-load-bearing: even without it the table grows by O(few
  hundred unique tags) per Polymarket epoch and stays small.

### `AppSettings` extension

Two new nullable columns:

| Column | Type | Meaning |
|---|---|---|
| `market_filter_tags` | `JSON` (list of strings, may be `null` or `[]`) | Whitelist. Empty / null = filter inactive. Stored already-normalised (lowercased, trimmed, deduped). |
| `market_filter_updated_at` | `TIMESTAMPTZ` (nullable) | Last operator change. Used by the architecture journal and for cache invalidation hints. |

### `ScannerSettingsModel` (pydantic v2)

```python
class ScannerSettingsModel(BaseModel):
    # ... existing fields ...
    market_filter_tags: list[str] = Field(
        default_factory=list,
        description="Whitelist of Polymarket/Kalshi tags. Markets must "
                    "carry at least one matching tag (OR-logic). Empty "
                    "list = no filter.",
    )

    @field_validator("market_filter_tags", mode="before")
    @classmethod
    def _normalize_tags(cls, value):
        # trim, lowercase, drop empties, dedupe — order preserved
```

### `GET /settings/market-filter/available-tags`

Response:

```json
{
  "tags": [
    {"name": "crypto", "last_seen": "2026-05-07T17:54:11Z", "occurrences": 412},
    {"name": "politics", "last_seen": "2026-05-07T17:54:09Z", "occurrences": 287}
  ],
  "total": 2
}
```

Ordering: `occurrences DESC, last_seen DESC`. Filter: `last_seen > now() - interval '24 hours'`. Limit 1000 (the API caps return size; the actual table is small enough that the cap is defensive only).

### Aggregator hook contract

```python
async def record_tags_from_markets(
    session: AsyncSession,
    events: Iterable[Event],
    markets: Iterable[Market],
) -> int:
    """Upsert every distinct tag seen into market_tags_seen.

    Returns the number of distinct tags written/updated. Failures
    are logged at WARNING and re-raised as ``MarketTagAggregatorError``;
    the caller is responsible for catching and continuing the ingest.
    """
```

`MARKET_TAG_AGGREGATOR_ENABLED` (default `True`) in `config.Settings`
gates the call. Off-switch lives there so flipping it doesn't require
an `AppSettings` write.

## Dependencies (both directions)

**This layer depends on:**

- `models.database.MarketTagSeen` and `AppSettings` columns.
- `models.market.Market.tags` / `Event.tags` (already normalised).
- `services.scanner.ArbitrageScanner._filter_tradable_markets` as the
  single funnel. Other code paths must not bypass it for catalog
  hydration.
- `config.Settings.MARKET_TAG_AGGREGATOR_ENABLED` runtime flag.

**Depended on by:**

- `worker-trading` scanner / catalog maintenance.
- `routes_settings` (Scanner tab + new available-tags endpoint).
- The frontend Scanner tab. The chip picker is a thin client over the
  API contract above.

## Extension points

When you want to add a new filter axis, compose it inside
`_filter_tradable_markets` — same shape:

| Axis | Where | How |
|---|---|---|
| Category whitelist | `_filter_tradable_markets` | Build `category_set = set(...)`, drop markets whose `event.category` doesn't match. Treat empty whitelist as "no filter" — same convention as tags. |
| Liquidity floor (per-axis) | `_filter_tradable_markets` | Add a `min_liquidity` clause; the scalar already lives in `AppSettings.min_liquidity` for the whole catalog. |
| Question regex blocklist | new helper | Compile the regex once per cycle, drop on positive match. Keep it cheap — this runs over thousands of rows. |
| Volume floor differentiated by tag | `_filter_tradable_markets` | Treat `MARKET_UNIVERSE_MIN_VOLUME` as a fallback; per-tag overrides land as a JSON column on `app_settings`. |

The two invariants that **must** survive every extension:

1. **Empty filter = no filter.** Operators expect that "save with
   nothing selected" returns to today's behaviour. Enforce via
   `if filter_value:` early-out.
2. **Tag aggregator runs on the raw stream.** Always before any
   filter. Otherwise the chooser can't see tags the operator hasn't
   already whitelisted, and the system becomes self-locked.

## Known footguns

- **Don't filter inside `_is_market_tradable` alone.** That helper is
  per-market and doesn't see `Event.tags`. The whitelist must consult
  the union — wire it in `_filter_tradable_markets` or pass the union
  set down.
- **Don't run the aggregator after the tradability filter.** You'd
  hide tags carried by markets that fail tradability for unrelated
  reasons (zero volume, no order book) — the operator could never
  whitelist a sport during its pre-open window.
- **Don't trust user-typed tags as canonical.** Always normalise
  (`.strip().lower()`) before comparing or persisting; the chooser
  shows tags lowercase, but a future paste of `"Crypto"` must still
  match.
- **Don't grow `market_tags_seen` indefinitely.** The 7-day physical
  prune is intentional. Without it, dead one-off tags from old
  events accumulate forever and pollute the chooser's
  occurrence-sorted top.
- **Don't bypass `_filter_tradable_markets` from a cache-replay
  path.** New scan paths added in the future must call it (or
  duplicate its funnel) — a path that skips it would silently
  ignore the tag filter.
