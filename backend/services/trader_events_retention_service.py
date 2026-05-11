"""Two-tier retention housekeeper for ``trader_events``.

Plan 0049 — Plan 0044's cross-mode firehose telemetry made
``trader_events`` the highest-volume table in the schema (~262 k
``firehose_evaluation`` rows / hour ≈ 8.4 GB / day after Postgres
overhead). Without retention the table reaches 30 GB in 4 days and
the host's ``/dev/sda1`` partition (301 GB) runs out within a
month. Two tiers were chosen because the ``firehose_evaluation``
rows have a short useful life (the backtester reaches back at most
~24 h, an A/B-test window is days, not months) while the
low-volume audit trail (``decision`` / ``order`` /
``provider_health`` / ``circuit_breaker`` / etc.) is regulatory
and stays 90 days.

Architecture mirrors ``services.chainlink_feed._housekeeper_loop``:

    * Async background task.
    * 60 s startup grace (avoid colliding with worker bringup).
    * 6 h cadence (``TRADER_EVENTS_HOUSEKEEPER_INTERVAL_SECONDS``).
    * Each tier deletes in 50 k-row batches with a 100 ms pause
      between batches so we never lock the table for minutes on
      the first run that drains the firehose backlog.
    * Soft-fail Redis heartbeat (key
      ``trader_events_housekeeper_running``, 1 h TTL) prevents two
      worker planes racing if the operator runs ``all`` and
      ``news`` simultaneously.

The housekeeper reads its retention horizons from ``app_settings``
on every cycle so the operator can tune them via Settings → DB
Maintenance without restarting the worker plane.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, text

from config import settings as global_settings
from utils.logger import get_logger


logger = get_logger("services.trader_events_retention")


_REDIS_HEARTBEAT_KEY = "trader_events_housekeeper_running"
_REDIS_HEARTBEAT_TTL_SECONDS = 3600

_FIREHOSE_EVENT_TYPE = "firehose_evaluation"


@dataclass
class _RetentionHorizons:
    firehose_days: int
    other_days: int


def _coerce_positive_int(value: object, default: int) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    if coerced <= 0:
        return default
    return coerced


async def _load_retention_horizons() -> _RetentionHorizons:
    """Read the latest two-tier retention horizons from ``app_settings``.

    Falls back to ``settings.TRADER_EVENTS_*_RETENTION_DAYS`` (which
    in turn fall back to the code defaults 7 / 90) when the DB row
    is missing or the columns are NULL.
    """
    from models.database import AsyncSessionLocal, AppSettings

    firehose_default = _coerce_positive_int(
        getattr(global_settings, "TRADER_EVENTS_FIREHOSE_RETENTION_DAYS", 7), 7
    )
    other_default = _coerce_positive_int(
        getattr(global_settings, "TRADER_EVENTS_OTHER_RETENTION_DAYS", 90), 90
    )

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(
                AppSettings.trader_events_firehose_retention_days,
                AppSettings.trader_events_other_retention_days,
            ).where(AppSettings.id == "default")
        )
        row = result.first()

    if row is None:
        return _RetentionHorizons(
            firehose_days=firehose_default, other_days=other_default
        )

    firehose_days = _coerce_positive_int(row[0], firehose_default)
    other_days = _coerce_positive_int(row[1], other_default)
    return _RetentionHorizons(firehose_days=firehose_days, other_days=other_days)


def _to_naive_utc(value: datetime) -> datetime:
    """Convert an aware datetime to naive UTC.

    ``trader_events.created_at`` is a SQLAlchemy ``UTCDateTime``
    column whose type decorator strips tzinfo when binding via the
    ORM. Raw ``text()`` SQL bypasses that decorator, so asyncpg
    sees a tz-aware Python datetime against a ``timestamp without
    time zone`` column and raises
    ``can't subtract offset-naive and offset-aware datetimes``.
    Normalising every cutoff to naive UTC here keeps the raw-SQL
    path aligned with the ORM-managed values stored in the column.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


async def _delete_tier(
    *,
    tier_label: str,
    cutoff: datetime,
    firehose: bool,
    batch_size: int,
    batch_pause_seconds: float,
) -> tuple[int, int]:
    """Delete one tier's rows in fixed-size batches.

    Returns ``(rows_deleted, batches_executed)``. Loops until a
    DELETE returns 0 rows, which means the tier is drained for this
    cycle. Each batch uses ``DELETE ... WHERE ctid IN (SELECT ctid
    ... LIMIT N)`` so Postgres can pick the cheapest scan and only
    touch ``batch_size`` rows under one lock acquisition.
    """
    from models.database import AsyncSessionLocal

    operator = "=" if firehose else "<>"
    cutoff_naive = _to_naive_utc(cutoff)

    rows_deleted = 0
    batches = 0
    while True:
        statement = text(
            f"""
            DELETE FROM trader_events
            WHERE ctid IN (
                SELECT ctid FROM trader_events
                WHERE event_type {operator} :firehose_event_type
                  AND created_at < :cutoff
                LIMIT :batch_size
            )
            """
        )
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                statement,
                {
                    "firehose_event_type": _FIREHOSE_EVENT_TYPE,
                    "cutoff": cutoff_naive,
                    "batch_size": batch_size,
                },
            )
            await session.commit()
            deleted_in_batch = int(getattr(result, "rowcount", 0) or 0)

        if deleted_in_batch <= 0:
            break

        rows_deleted += deleted_in_batch
        batches += 1

        if deleted_in_batch < batch_size:
            # Short final batch — drained, no need to keep looping.
            break

        if batch_pause_seconds > 0:
            try:
                await asyncio.sleep(batch_pause_seconds)
            except asyncio.CancelledError:
                logger.info(
                    "trader_events housekeeper cancelled mid-tier",
                    tier=tier_label,
                    rows_deleted=rows_deleted,
                    batches=batches,
                )
                raise

    return rows_deleted, batches


async def _try_acquire_heartbeat() -> bool:
    """Set the Redis heartbeat key with NX semantics.

    Returns True when the key was newly set (we hold the lease),
    False when another worker already holds it. Soft-fails to True
    when Redis is unavailable — the housekeeper still wants to run
    in single-worker deployments without Redis.
    """
    try:
        from services import redis_client
    except Exception:
        return True

    client = redis_client.get_client_or_none()
    if client is None:
        return True

    try:
        ok = await client.set(
            redis_client._ns(_REDIS_HEARTBEAT_KEY),
            str(int(time.time())),
            ex=_REDIS_HEARTBEAT_TTL_SECONDS,
            nx=True,
        )
    except Exception:
        return True

    return bool(ok)


async def _release_heartbeat() -> None:
    """Best-effort heartbeat release."""
    try:
        from services import redis_client
    except Exception:
        return

    client = redis_client.get_client_or_none()
    if client is None:
        return

    try:
        await client.delete(redis_client._ns(_REDIS_HEARTBEAT_KEY))
    except Exception:
        pass


async def _housekeeper_once() -> dict[str, int]:
    """Run one full housekeeper sweep across both retention tiers.

    Returns a structured summary suitable for log lines and tests:
    ``{"firehose_rows_deleted": int, "firehose_batches": int,
       "other_rows_deleted": int, "other_batches": int,
       "elapsed_ms_total": int, "skipped": 0|1}``. ``skipped=1``
    means another worker holds the Redis heartbeat — the caller is
    expected to log a warn and move on.
    """
    started_at = time.monotonic()

    if not await _try_acquire_heartbeat():
        logger.warning(
            "trader_events housekeeper skipped — another worker holds the lease"
        )
        return {
            "firehose_rows_deleted": 0,
            "firehose_batches": 0,
            "other_rows_deleted": 0,
            "other_batches": 0,
            "elapsed_ms_total": 0,
            "skipped": 1,
        }

    try:
        horizons = await _load_retention_horizons()
        batch_size = max(
            1,
            _coerce_positive_int(
                getattr(global_settings, "TRADER_EVENTS_HOUSEKEEPER_BATCH_SIZE", 50_000),
                50_000,
            ),
        )
        try:
            batch_pause_seconds = float(
                getattr(
                    global_settings,
                    "TRADER_EVENTS_HOUSEKEEPER_BATCH_PAUSE_SECONDS",
                    0.1,
                )
            )
        except (TypeError, ValueError):
            batch_pause_seconds = 0.1
        if batch_pause_seconds < 0:
            batch_pause_seconds = 0.0

        now = datetime.now(timezone.utc)

        firehose_cutoff = now - timedelta(days=horizons.firehose_days)
        firehose_started = time.monotonic()
        firehose_deleted, firehose_batches = await _delete_tier(
            tier_label="firehose_evaluation",
            cutoff=firehose_cutoff,
            firehose=True,
            batch_size=batch_size,
            batch_pause_seconds=batch_pause_seconds,
        )
        firehose_elapsed_ms = int((time.monotonic() - firehose_started) * 1000)
        logger.info(
            "trader_events housekeeper tier complete",
            tier="firehose_evaluation",
            retention_days=horizons.firehose_days,
            cutoff=firehose_cutoff.isoformat(),
            rows_deleted=firehose_deleted,
            batches=firehose_batches,
            elapsed_ms=firehose_elapsed_ms,
        )

        other_cutoff = now - timedelta(days=horizons.other_days)
        other_started = time.monotonic()
        other_deleted, other_batches = await _delete_tier(
            tier_label="other",
            cutoff=other_cutoff,
            firehose=False,
            batch_size=batch_size,
            batch_pause_seconds=batch_pause_seconds,
        )
        other_elapsed_ms = int((time.monotonic() - other_started) * 1000)
        logger.info(
            "trader_events housekeeper tier complete",
            tier="other",
            retention_days=horizons.other_days,
            cutoff=other_cutoff.isoformat(),
            rows_deleted=other_deleted,
            batches=other_batches,
            elapsed_ms=other_elapsed_ms,
        )

        elapsed_ms_total = int((time.monotonic() - started_at) * 1000)
        return {
            "firehose_rows_deleted": firehose_deleted,
            "firehose_batches": firehose_batches,
            "other_rows_deleted": other_deleted,
            "other_batches": other_batches,
            "elapsed_ms_total": elapsed_ms_total,
            "skipped": 0,
        }
    finally:
        await _release_heartbeat()


async def _housekeeper_loop(stop_event: Optional[asyncio.Event] = None) -> None:
    """Background task entry-point — call from a worker plane lifespan.

    Honors ``stop_event`` for cooperative shutdown when provided;
    otherwise loops forever until cancelled. The first sleep absorbs
    the startup-grace window so the first DELETE doesn't fight worker
    bringup.
    """
    startup_delay = max(
        0,
        _coerce_positive_int(
            getattr(
                global_settings,
                "TRADER_EVENTS_HOUSEKEEPER_STARTUP_DELAY_SECONDS",
                60,
            ),
            60,
        ),
    )
    interval_seconds = max(
        60,
        _coerce_positive_int(
            getattr(
                global_settings,
                "TRADER_EVENTS_HOUSEKEEPER_INTERVAL_SECONDS",
                21600,
            ),
            21600,
        ),
    )

    try:
        await asyncio.sleep(startup_delay)
    except asyncio.CancelledError:
        return

    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            await _housekeeper_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "trader_events housekeeper cycle failed",
                exc_info=exc,
            )
        # Cooperative sleep — wake immediately if shutdown is
        # signalled. Without this, plane shutdown waits up to
        # ``interval_seconds`` (default 6 h) for the housekeeper to
        # exit. Falls back to plain sleep when no ``stop_event`` is
        # supplied (legacy entrypoints).
        if stop_event is None:
            try:
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                return
        else:
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=interval_seconds
                )
                return  # stop_event fired — exit loop
            except asyncio.TimeoutError:
                pass  # interval elapsed normally — run next cycle
            except asyncio.CancelledError:
                return


async def dry_run_summary() -> dict[str, object]:
    """Operator-friendly preview of what the next housekeeper run will delete.

    Returns ``{"firehose": {...}, "other": {...},
               "firehose_retention_days": int,
               "other_retention_days": int}`` where each tier dict
    carries ``rows_to_delete``, ``oldest_row_iso``, and
    ``bytes_estimate``. No DELETE is issued.
    """
    from models.database import AsyncSessionLocal

    horizons = await _load_retention_horizons()
    now = datetime.now(timezone.utc)
    firehose_cutoff = _to_naive_utc(now - timedelta(days=horizons.firehose_days))
    other_cutoff = _to_naive_utc(now - timedelta(days=horizons.other_days))

    async with AsyncSessionLocal() as session:
        firehose_row = (
            await session.execute(
                text(
                    """
                    SELECT COUNT(*), MIN(created_at)
                    FROM trader_events
                    WHERE event_type = :firehose_event_type
                      AND created_at < :cutoff
                    """
                ),
                {
                    "firehose_event_type": _FIREHOSE_EVENT_TYPE,
                    "cutoff": firehose_cutoff,
                },
            )
        ).first()
        other_row = (
            await session.execute(
                text(
                    """
                    SELECT COUNT(*), MIN(created_at)
                    FROM trader_events
                    WHERE event_type <> :firehose_event_type
                      AND created_at < :cutoff
                    """
                ),
                {
                    "firehose_event_type": _FIREHOSE_EVENT_TYPE,
                    "cutoff": other_cutoff,
                },
            )
        ).first()

    avg_row_bytes = 1400  # observed mean payload incl. JSON / index overhead

    def _summarise(row: object) -> dict[str, object]:
        if row is None:
            return {"rows_to_delete": 0, "oldest_row_iso": None, "bytes_estimate": 0}
        count = int(row[0] or 0)
        oldest = row[1]
        oldest_iso = oldest.isoformat() if isinstance(oldest, datetime) else None
        return {
            "rows_to_delete": count,
            "oldest_row_iso": oldest_iso,
            "bytes_estimate": count * avg_row_bytes,
        }

    return {
        "firehose_retention_days": horizons.firehose_days,
        "other_retention_days": horizons.other_days,
        "firehose": _summarise(firehose_row),
        "other": _summarise(other_row),
    }
