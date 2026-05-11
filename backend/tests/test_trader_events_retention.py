"""Plan 0049 Task 2 — trader_events two-tier retention housekeeper.

Pins the four invariants from the plan:

1. Firehose rows older than ``firehose_retention_days`` are deleted.
2. Firehose rows younger than ``firehose_retention_days`` survive.
3. Non-firehose rows are kept under the longer ``other_retention_days``
   horizon and only purged when they cross it.
4. Two structured ``info`` log lines land per cycle (one per tier),
   carrying ``rows_deleted`` / ``tier`` / ``elapsed_ms`` so an
   operator tail can correlate first-run drains with disk pressure.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models.database import AppSettings, Base, TraderEvent  # noqa: E402
from tests.postgres_test_db import build_postgres_session_factory  # noqa: E402


def _naive_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None, microsecond=0)


async def _seed_event(
    session_factory,
    *,
    event_id: str,
    event_type: str,
    age_days: float,
) -> None:
    created_at = _naive_utc(
        datetime.now(timezone.utc) - timedelta(days=age_days)
    )
    async with session_factory() as session:
        session.add(
            TraderEvent(
                id=event_id,
                trader_id=None,
                event_type=event_type,
                severity="info",
                verbosity="whisper",
                source="trader_runtime",
                operator=None,
                message=None,
                trace_id=None,
                payload_json={"age_days": age_days},
                created_at=created_at,
            )
        )
        await session.commit()


async def _seed_app_settings(session_factory) -> None:
    async with session_factory() as session:
        session.add(AppSettings(id="default"))
        await session.commit()


async def _existing_event_ids(session_factory) -> set[str]:
    async with session_factory() as session:
        rows = (await session.execute(select(TraderEvent.id))).all()
    return {str(row[0]) for row in rows}


def _patch_isolated_session_factory(monkeypatch, session_factory) -> None:
    import models.database as models_database
    import services.trader_events_retention_service as svc

    monkeypatch.setattr(models_database, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(svc, "_try_acquire_heartbeat", _always_acquire, raising=False)
    monkeypatch.setattr(svc, "_release_heartbeat", _noop_release, raising=False)


async def _always_acquire() -> bool:
    return True


async def _noop_release() -> None:
    return None


@pytest.mark.asyncio
async def test_housekeeper_deletes_old_firehose_and_old_other(
    monkeypatch, caplog
):
    engine, session_factory = await build_postgres_session_factory(
        Base, "trader_events_retention_full"
    )
    try:
        _patch_isolated_session_factory(monkeypatch, session_factory)
        await _seed_app_settings(session_factory)

        # Firehose rows aged 1..20 days. With the default 7-day
        # firehose horizon, ages 8..20 must be deleted (13 rows);
        # ages 1..7 must survive (7 rows).
        for day in range(1, 21):
            await _seed_event(
                session_factory,
                event_id=f"firehose-{day}d",
                event_type="firehose_evaluation",
                age_days=day,
            )

        # Non-firehose audit rows aged 30..120 days. With the
        # default 90-day other horizon, only ages 91..120 must
        # be deleted (30 rows); 30..90 (61 rows) survive.
        for day in range(30, 121):
            await _seed_event(
                session_factory,
                event_id=f"order-{day}d",
                event_type="order",
                age_days=day,
            )

        # Sanity: 20 + 91 = 111 rows present pre-sweep.
        assert len(await _existing_event_ids(session_factory)) == 111

        from services.trader_events_retention_service import _housekeeper_once

        with caplog.at_level(logging.INFO, logger="services.trader_events_retention"):
            summary = await _housekeeper_once()

        assert summary["skipped"] == 0
        assert summary["firehose_rows_deleted"] == 13
        assert summary["other_rows_deleted"] == 30
        assert summary["firehose_batches"] >= 1
        assert summary["other_batches"] >= 1

        remaining = await _existing_event_ids(session_factory)
        assert len(remaining) == 7 + 61

        # Surviving firehose rows are exactly those aged 1..7 days.
        for day in range(1, 8):
            assert f"firehose-{day}d" in remaining
        for day in range(8, 21):
            assert f"firehose-{day}d" not in remaining

        # Surviving order rows are exactly those aged 30..90 days.
        for day in range(30, 91):
            assert f"order-{day}d" in remaining
        for day in range(91, 121):
            assert f"order-{day}d" not in remaining

        tier_logs = [
            record for record in caplog.records
            if record.name == "services.trader_events_retention"
            and "tier complete" in record.getMessage()
        ]
        # Structured logger packs context kwargs into ``record.extra_data``;
        # see ``backend/utils/logger.py``'s ``ContextLogger._log``.
        tiers_seen = {
            (getattr(record, "extra_data", None) or {}).get("tier")
            for record in tier_logs
        }
        assert tiers_seen == {"firehose_evaluation", "other"}, (
            f"expected one log per tier, got tiers={tiers_seen}"
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_housekeeper_respects_db_backed_retention_overrides(
    monkeypatch,
):
    engine, session_factory = await build_postgres_session_factory(
        Base, "trader_events_retention_overrides"
    )
    try:
        _patch_isolated_session_factory(monkeypatch, session_factory)

        # Tighter firehose horizon (2 days), wider other horizon
        # (200 days) — exercises the per-cycle DB read.
        async with session_factory() as session:
            row = AppSettings(id="default")
            row.trader_events_firehose_retention_days = 2
            row.trader_events_other_retention_days = 200
            session.add(row)
            await session.commit()

        await _seed_event(
            session_factory,
            event_id="firehose-1d",
            event_type="firehose_evaluation",
            age_days=1,
        )
        await _seed_event(
            session_factory,
            event_id="firehose-3d",
            event_type="firehose_evaluation",
            age_days=3,
        )
        await _seed_event(
            session_factory,
            event_id="order-150d",
            event_type="order",
            age_days=150,
        )
        await _seed_event(
            session_factory,
            event_id="order-250d",
            event_type="order",
            age_days=250,
        )

        from services.trader_events_retention_service import _housekeeper_once

        summary = await _housekeeper_once()

        assert summary["firehose_rows_deleted"] == 1
        assert summary["other_rows_deleted"] == 1

        remaining = await _existing_event_ids(session_factory)
        assert remaining == {"firehose-1d", "order-150d"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_housekeeper_idempotent_on_drained_table(monkeypatch):
    engine, session_factory = await build_postgres_session_factory(
        Base, "trader_events_retention_idempotent"
    )
    try:
        _patch_isolated_session_factory(monkeypatch, session_factory)
        await _seed_app_settings(session_factory)

        await _seed_event(
            session_factory,
            event_id="firehose-fresh",
            event_type="firehose_evaluation",
            age_days=0.1,
        )

        from services.trader_events_retention_service import _housekeeper_once

        first = await _housekeeper_once()
        second = await _housekeeper_once()

        assert first["firehose_rows_deleted"] == 0
        assert second["firehose_rows_deleted"] == 0
        assert second["other_rows_deleted"] == 0
        # Heartbeat path always reports 0 batches when nothing to delete.
        assert first["firehose_batches"] == 0
        assert second["firehose_batches"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_housekeeper_skipped_when_lease_is_held(monkeypatch, caplog):
    engine, session_factory = await build_postgres_session_factory(
        Base, "trader_events_retention_lease"
    )
    try:
        import models.database as models_database
        import services.trader_events_retention_service as svc

        monkeypatch.setattr(models_database, "AsyncSessionLocal", session_factory)

        async def _refuse_lease() -> bool:
            return False

        async def _noop() -> None:
            return None

        monkeypatch.setattr(svc, "_try_acquire_heartbeat", _refuse_lease)
        monkeypatch.setattr(svc, "_release_heartbeat", _noop)

        await _seed_app_settings(session_factory)
        await _seed_event(
            session_factory,
            event_id="firehose-30d",
            event_type="firehose_evaluation",
            age_days=30,
        )

        with caplog.at_level(
            logging.WARNING, logger="services.trader_events_retention"
        ):
            summary = await svc._housekeeper_once()

        assert summary["skipped"] == 1
        assert summary["firehose_rows_deleted"] == 0
        assert summary["other_rows_deleted"] == 0
        assert await _existing_event_ids(session_factory) == {"firehose-30d"}
        assert any(
            "another worker holds the lease" in record.getMessage()
            for record in caplog.records
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_housekeeper_batched_delete_drains_large_backlog(monkeypatch):
    """Verify the loop keeps issuing DELETEs until the table is drained.

    Forces ``BATCH_SIZE = 5`` so a 23-row backlog needs five batches
    (5 + 5 + 5 + 5 + 3) — pins the ``while True / break on
    deleted_in_batch < batch_size`` exit condition without seeding
    50k rows on every test run.
    """
    engine, session_factory = await build_postgres_session_factory(
        Base, "trader_events_retention_batches"
    )
    try:
        _patch_isolated_session_factory(monkeypatch, session_factory)

        from config import settings as global_settings
        import services.trader_events_retention_service as svc

        original_batch = global_settings.TRADER_EVENTS_HOUSEKEEPER_BATCH_SIZE
        original_pause = global_settings.TRADER_EVENTS_HOUSEKEEPER_BATCH_PAUSE_SECONDS
        object.__setattr__(global_settings, "TRADER_EVENTS_HOUSEKEEPER_BATCH_SIZE", 5)
        object.__setattr__(
            global_settings, "TRADER_EVENTS_HOUSEKEEPER_BATCH_PAUSE_SECONDS", 0.0
        )
        try:
            await _seed_app_settings(session_factory)
            for idx in range(23):
                await _seed_event(
                    session_factory,
                    event_id=f"firehose-old-{idx}",
                    event_type="firehose_evaluation",
                    age_days=14,
                )

            summary = await svc._housekeeper_once()
        finally:
            object.__setattr__(
                global_settings,
                "TRADER_EVENTS_HOUSEKEEPER_BATCH_SIZE",
                original_batch,
            )
            object.__setattr__(
                global_settings,
                "TRADER_EVENTS_HOUSEKEEPER_BATCH_PAUSE_SECONDS",
                original_pause,
            )

        assert summary["firehose_rows_deleted"] == 23
        assert summary["firehose_batches"] == 5
        assert await _existing_event_ids(session_factory) == set()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dry_run_summary_reports_pending_deletions(monkeypatch):
    engine, session_factory = await build_postgres_session_factory(
        Base, "trader_events_retention_dry_run"
    )
    try:
        _patch_isolated_session_factory(monkeypatch, session_factory)
        await _seed_app_settings(session_factory)

        await _seed_event(
            session_factory,
            event_id="firehose-aged",
            event_type="firehose_evaluation",
            age_days=10,
        )
        await _seed_event(
            session_factory,
            event_id="firehose-fresh",
            event_type="firehose_evaluation",
            age_days=1,
        )
        await _seed_event(
            session_factory,
            event_id="order-aged",
            event_type="order",
            age_days=120,
        )

        from services.trader_events_retention_service import dry_run_summary

        summary = await dry_run_summary()

        assert summary["firehose_retention_days"] == 7
        assert summary["other_retention_days"] == 90
        assert summary["firehose"]["rows_to_delete"] == 1
        assert summary["other"]["rows_to_delete"] == 1
        assert summary["firehose"]["bytes_estimate"] > 0
        assert summary["firehose"]["oldest_row_iso"] is not None
        assert summary["other"]["oldest_row_iso"] is not None

        # No mutation.
        assert await _existing_event_ids(session_factory) == {
            "firehose-aged",
            "firehose-fresh",
            "order-aged",
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_housekeeper_loop_exits_on_stop_event(monkeypatch):
    """``_housekeeper_loop`` honors the cooperative stop event.

    Used by the worker plane shutdown path to drain background tasks.
    """
    engine, session_factory = await build_postgres_session_factory(
        Base, "trader_events_retention_stop"
    )
    try:
        _patch_isolated_session_factory(monkeypatch, session_factory)
        await _seed_app_settings(session_factory)

        from config import settings as global_settings
        import services.trader_events_retention_service as svc

        original_delay = global_settings.TRADER_EVENTS_HOUSEKEEPER_STARTUP_DELAY_SECONDS
        object.__setattr__(
            global_settings, "TRADER_EVENTS_HOUSEKEEPER_STARTUP_DELAY_SECONDS", 0
        )

        try:
            stop_event = asyncio.Event()
            stop_event.set()
            await asyncio.wait_for(svc._housekeeper_loop(stop_event=stop_event), timeout=5)
        finally:
            object.__setattr__(
                global_settings,
                "TRADER_EVENTS_HOUSEKEEPER_STARTUP_DELAY_SECONDS",
                original_delay,
            )
    finally:
        await engine.dispose()
