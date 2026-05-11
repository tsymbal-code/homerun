"""Regression tests for the projection-sweep grace window (Plan 0052).

``expire_source_signals_except`` force-expires every pending row whose
dedupe_key isn't in the caller-supplied ``keep_dedupe_keys`` set. The
projection sweep that calls it (``intent_runtime._project_status_batch``)
captures its snapshot from a point-in-time query, then runs the sweep
in a separate transaction. For event-driven sources (crypto, traders,
weather) where each emit is one self-contained INSERT, a sub-second
race between INSERT and snapshot kills the row before the trader
cursor can pick it up.

The fix is a ``min_signal_age_seconds`` guard (default 60 s) that
holds new pending rows past the next sweep. Scanner-source signals
tolerate the staleness because the next scan re-emits the same
dedupe_key, refreshing the row.

These tests pin the four behaviours the plan asked for: skip young,
keep old, keep-set overrides age, explicit-zero opts out.
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models.database import Base, TradeSignal
from services.signal_bus import (
    _EXPIRE_SOURCE_GRACE_SECONDS,
    _utc_now,
    _to_utc_naive,
    expire_source_signals_except,
)
from tests.postgres_test_db import build_postgres_session_factory


SOURCE = "crypto"


async def _insert_signal(
    session_factory,
    *,
    created_at_offset_seconds: float,
    dedupe_key: str | None = None,
) -> str:
    """INSERT one pending TradeSignal row with an explicit created_at
    offset relative to now (negative = in the past). Returns the id."""
    row_id = uuid.uuid4().hex
    dedupe = dedupe_key or uuid.uuid4().hex
    created_at = _to_utc_naive(_utc_now()) + timedelta(seconds=created_at_offset_seconds)
    async with session_factory() as session:
        session.add(
            TradeSignal(
                id=row_id,
                source=SOURCE,
                signal_type="opportunity",
                strategy_type="crypto_5m_last_outcome",
                market_id=f"market_{row_id[:8]}",
                direction="buy_yes",
                dedupe_key=dedupe,
                status="pending",
                created_at=created_at,
                updated_at=created_at,
            )
        )
        await session.commit()
    return row_id


async def _read_status(session_factory, row_id: str) -> str:
    async with session_factory() as session:
        row = (
            await session.execute(select(TradeSignal).where(TradeSignal.id == row_id))
        ).scalar_one()
        return str(row.status)


@pytest.mark.asyncio
async def test_expire_skips_signals_younger_than_grace(tmp_path):
    """Default grace = 60 s; a 5 s-old row must NOT be expired even
    when no keep_dedupe_keys protect it."""
    engine, session_factory = await build_postgres_session_factory(
        Base, "signal_bus_expire_skip_young"
    )
    try:
        row_id = await _insert_signal(session_factory, created_at_offset_seconds=-5.0)

        async with session_factory() as session:
            n_expired = await expire_source_signals_except(
                session,
                source=SOURCE,
                keep_dedupe_keys=set(),
            )

        assert n_expired == 0
        assert await _read_status(session_factory, row_id) == "pending"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expire_keeps_signals_older_than_grace(tmp_path):
    """A 120 s-old row not in keep set MUST be expired with default grace."""
    engine, session_factory = await build_postgres_session_factory(
        Base, "signal_bus_expire_keep_old"
    )
    try:
        row_id = await _insert_signal(session_factory, created_at_offset_seconds=-120.0)

        async with session_factory() as session:
            n_expired = await expire_source_signals_except(
                session,
                source=SOURCE,
                keep_dedupe_keys=set(),
            )

        assert n_expired == 1
        assert await _read_status(session_factory, row_id) == "expired"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expire_respects_keep_set_irrespective_of_age(tmp_path):
    """A 120 s-old row whose dedupe_key is in keep set must STAY pending —
    the keep set overrides the age check."""
    engine, session_factory = await build_postgres_session_factory(
        Base, "signal_bus_expire_keep_overrides_age"
    )
    try:
        dedupe = uuid.uuid4().hex
        row_id = await _insert_signal(
            session_factory,
            created_at_offset_seconds=-120.0,
            dedupe_key=dedupe,
        )

        async with session_factory() as session:
            n_expired = await expire_source_signals_except(
                session,
                source=SOURCE,
                keep_dedupe_keys={dedupe},
            )

        assert n_expired == 0
        assert await _read_status(session_factory, row_id) == "pending"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_zero_grace_restores_legacy_behavior(tmp_path):
    """Passing min_signal_age_seconds=0.0 reverts to the pre-Plan-0052
    behaviour where even a 1 s-old row gets nuked. Forward-defensive
    escape hatch — no production caller does this today."""
    engine, session_factory = await build_postgres_session_factory(
        Base, "signal_bus_expire_zero_grace"
    )
    try:
        row_id = await _insert_signal(session_factory, created_at_offset_seconds=-1.0)

        async with session_factory() as session:
            n_expired = await expire_source_signals_except(
                session,
                source=SOURCE,
                keep_dedupe_keys=set(),
                min_signal_age_seconds=0.0,
            )

        assert n_expired == 1
        assert await _read_status(session_factory, row_id) == "expired"
    finally:
        await engine.dispose()


def test_module_constant_is_60_seconds():
    """Pin the default grace value so a regression in the constant is
    caught by tests, not by reading the diff."""
    assert _EXPIRE_SOURCE_GRACE_SECONDS == 60.0
