"""Tag aggregator hook for the Polymarket / Kalshi ingest pipeline.

Every ingest cycle, ``record_tags_from_markets`` is called once with
the **raw** events + markets list (before any filter is applied) and
upserts every distinct tag string into ``market_tags_seen``. The
``Settings → Scanner`` tag chooser queries that table for tags seen
in the last 24 hours; without this hook the chooser would have no
data and the operator could not whitelist anything.

See ``docs/plans/architecture/market-filter.md`` for the surrounding
funnel, retention policy, and the contract this hook honours.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from utils.logger import get_logger


logger = get_logger(__name__)


def _normalize_tag(value: object) -> str | None:
    """Lowercase, strip, drop empties. Mirrors the chooser's contract."""
    if value is None:
        return None
    text_value = str(value).strip().lower()
    if not text_value:
        return None
    return text_value


def _collect_tags(events: Iterable[Any], markets: Iterable[Any]) -> set[str]:
    """Union of normalised tag strings across raw markets and their events."""
    tags: set[str] = set()
    for market in markets:
        for raw in list(getattr(market, "tags", None) or []):
            normalised = _normalize_tag(raw)
            if normalised:
                tags.add(normalised)
    for event in events:
        for raw in list(getattr(event, "tags", None) or []):
            normalised = _normalize_tag(raw)
            if normalised:
                tags.add(normalised)
    return tags


async def record_tags_from_markets(
    session: AsyncSession,
    events: Iterable[Any],
    markets: Iterable[Any],
) -> int:
    """Upsert every distinct tag observed on the raw stream.

    Returns the number of distinct tags written/updated. The caller
    is responsible for catching exceptions — aggregator failure must
    never block ingest. The runtime kill-switch is
    ``settings.MARKET_TAG_AGGREGATOR_ENABLED``.
    """
    if not bool(getattr(settings, "MARKET_TAG_AGGREGATOR_ENABLED", True)):
        return 0

    tags = _collect_tags(events, markets)
    if not tags:
        return 0

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    payload = [{"tag": tag, "now": now} for tag in tags]

    stmt = text(
        """
        INSERT INTO market_tags_seen (tag, first_seen, last_seen, occurrences)
        VALUES (:tag, :now, :now, 1)
        ON CONFLICT (tag) DO UPDATE SET
            last_seen = EXCLUDED.last_seen,
            occurrences = market_tags_seen.occurrences + 1
        """
    )
    await session.execute(stmt, payload)
    await session.commit()
    return len(tags)


async def prune_stale_tags(
    session: AsyncSession,
    *,
    max_age_days: int = 7,
) -> int:
    """Delete rows whose ``last_seen`` is older than ``max_age_days``.

    Slow-cleanup helper called from the discovery worker. Returns the
    number of rows deleted. Non-load-bearing — the table stays small
    even without this; the prune just keeps the chooser's bottom
    long-tail clean.
    """
    if max_age_days <= 0:
        return 0
    stmt = text(
        """
        DELETE FROM market_tags_seen
        WHERE last_seen < NOW() - make_interval(days => :days)
        """
    )
    result = await session.execute(stmt, {"days": int(max_age_days)})
    await session.commit()
    return int(result.rowcount or 0)
