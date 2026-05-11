"""Plan 0050 — auto-resync SYSTEM strategies from disk on every container boot.

Pins five invariants of
:func:`services.opportunity_strategy_catalog.resync_system_strategies_with_disk`:

1. When ``md5(disk source) == md5(db source)`` the row is left alone
   (``unchanged_count`` increments).
2. When the disk source differs the DB row is rewritten and the slug
   appears in ``resynced`` with the right md5-before/after pair.
3. ``Strategy.is_system = False`` rows are NEVER touched, regardless
   of md5 mismatch (operator's own edit is sacred).
4. Each call appends one ``trader_events`` row with
   ``event_type='strategy_resync'`` and a payload matching the
   return value.
5. A per-slug ``reset_strategy_to_factory`` failure does NOT cascade
   — other slugs still process and the slug appears in ``errors``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select  # noqa: E402

from models.database import Base, Strategy, TraderEvent  # noqa: E402
from services import opportunity_strategy_catalog as catalog  # noqa: E402
from tests.postgres_test_db import build_postgres_session_factory  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FAKE_SEED_SLUG = "test_plan_0050_seed"


def _make_fake_seed_row(*, source_code: str) -> dict:
    """Minimal seed row shaped like ``build_system_opportunity_strategy_rows``
    output — only the fields ``reset_strategy_to_factory`` and the resync
    helper actually touch."""
    return {
        "slug": _FAKE_SEED_SLUG,
        "source_key": "plan_0050_test",
        "name": "Plan 0050 test strategy",
        "description": "Synthetic seed for the resync regression test.",
        "source_code": source_code,
        "class_name": "Plan0050TestStrategy",
        "config": {},
        "config_schema": {},
        "is_system": True,
        "sort_order": 9999,
        "enabled": False,
        "version": 1,
        "status": "active",
        "error_message": None,
        "aliases": [],
    }


@pytest.fixture()
async def session_factory():
    factory, managed_engine = await build_postgres_session_factory(Base.metadata)
    try:
        yield factory
    finally:
        await managed_engine.dispose()


# ---------------------------------------------------------------------------
# Test 1 — md5 match → unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resync_unchanged_when_md5_matches(session_factory, monkeypatch):
    """Boot-time resync must NOT touch a row whose source_code already
    matches the on-disk seed — only the unchanged-count tracker moves."""
    disk_source = "def hello(): return 'disk_v1'\n"
    seed_row = _make_fake_seed_row(source_code=disk_source)
    monkeypatch.setattr(
        catalog, "build_system_opportunity_strategy_rows", lambda *a, **k: [seed_row]
    )

    async with session_factory() as session:
        # Pre-seed: DB row identical to disk.
        session.add(Strategy(**seed_row))
        await session.commit()

        summary = await catalog.resync_system_strategies_with_disk(
            session, process_label="test"
        )

    assert summary["unchanged_count"] == 1
    assert summary["resynced"] == []
    assert summary["errors"] == []
    assert summary["total_seeds"] == 1


# ---------------------------------------------------------------------------
# Test 2 — disk differs → row rewritten + summary lists the slug
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resync_updates_when_disk_differs(session_factory, monkeypatch):
    """When md5(disk) != md5(db), the DB row's source_code is replaced
    and the slug appears in ``resynced`` with both md5 fingerprints."""
    old_source = "def hello(): return 'db_old'\n"
    new_source = "def hello(): return 'disk_new_longer_now'\n"
    seed_row = _make_fake_seed_row(source_code=new_source)
    monkeypatch.setattr(
        catalog, "build_system_opportunity_strategy_rows", lambda *a, **k: [seed_row]
    )

    async with session_factory() as session:
        existing_row = Strategy(
            **{**seed_row, "source_code": old_source, "version": 7}
        )
        session.add(existing_row)
        await session.commit()

        summary = await catalog.resync_system_strategies_with_disk(
            session, process_label="test"
        )

        # Assertions on the in-memory return value.
        assert summary["unchanged_count"] == 0
        assert len(summary["resynced"]) == 1
        entry = summary["resynced"][0]
        assert entry["slug"] == _FAKE_SEED_SLUG
        assert entry["db_md5_before"] == catalog._md5_of_str(old_source)
        assert entry["disk_md5"] == catalog._md5_of_str(new_source)
        assert entry["len_delta"] == len(new_source) - len(old_source)
        assert entry["reset_status"] == "reset"

        # Assertions on the DB side.
        refreshed = (
            await session.execute(
                select(Strategy).where(Strategy.slug == _FAKE_SEED_SLUG)
            )
        ).scalar_one()
        assert refreshed.source_code == new_source
        assert refreshed.version == 8  # reset_to_factory bumps version


# ---------------------------------------------------------------------------
# Test 3 — user-authored row is sacred
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resync_skips_user_authored(session_factory, monkeypatch):
    """When ``Strategy.is_system = False`` the resync must leave the row
    alone even if the on-disk seed differs — operator's edit is sacred."""
    disk_source = "def hello(): return 'disk_v1'\n"
    user_source = "def hello(): return 'user_authored_version'\n"
    seed_row = _make_fake_seed_row(source_code=disk_source)
    monkeypatch.setattr(
        catalog, "build_system_opportunity_strategy_rows", lambda *a, **k: [seed_row]
    )

    async with session_factory() as session:
        existing_row = Strategy(
            **{**seed_row, "source_code": user_source, "is_system": False}
        )
        session.add(existing_row)
        await session.commit()

        summary = await catalog.resync_system_strategies_with_disk(
            session, process_label="test"
        )

        # The slug must be in skipped_user_authored, NOT in resynced.
        assert summary["resynced"] == []
        assert {entry["slug"] for entry in summary["skipped_user_authored"]} == {
            _FAKE_SEED_SLUG
        }

        # And the DB row must still hold the user source.
        refreshed = (
            await session.execute(
                select(Strategy).where(Strategy.slug == _FAKE_SEED_SLUG)
            )
        ).scalar_one()
        assert refreshed.source_code == user_source
        assert refreshed.is_system is False


# ---------------------------------------------------------------------------
# Test 4 — trader_events row is appended every call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resync_emits_trader_event_row(session_factory, monkeypatch):
    """A ``trader_events`` row with ``event_type='strategy_resync'``
    must be appended on every call, even when nothing changed —
    the UI banner reads from this stream."""
    disk_source = "def hello(): return 'v'\n"
    seed_row = _make_fake_seed_row(source_code=disk_source)
    monkeypatch.setattr(
        catalog, "build_system_opportunity_strategy_rows", lambda *a, **k: [seed_row]
    )

    async with session_factory() as session:
        session.add(Strategy(**seed_row))
        await session.commit()

        summary = await catalog.resync_system_strategies_with_disk(
            session, process_label="test"
        )

        events = (
            await session.execute(
                select(TraderEvent).where(
                    TraderEvent.event_type == "strategy_resync"
                )
            )
        ).scalars().all()

    assert len(events) == 1
    event = events[0]
    assert event.source == "opportunity_strategy_catalog"
    payload = event.payload_json or {}
    assert payload.get("total_seeds") == 1
    assert payload.get("unchanged_count") == summary["unchanged_count"]
    assert payload.get("resynced") == summary["resynced"]


# ---------------------------------------------------------------------------
# Test 5 — fail-open: a single-slug error does NOT cascade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resync_fail_open_on_single_slug_error(session_factory, monkeypatch):
    """If ``reset_strategy_to_factory`` raises for one slug, the resync
    must record the error and continue processing other slugs without
    propagating the exception. The error slug must appear in
    ``errors`` and absent from ``resynced``."""
    happy_old = "def happy(): return 'old'\n"
    happy_new = "def happy(): return 'new'\n"
    sad_old = "def sad(): return 'old'\n"
    sad_new = "def sad(): return 'new'\n"

    happy_seed = _make_fake_seed_row(source_code=happy_new)
    happy_seed["slug"] = "plan_0050_happy"
    sad_seed = _make_fake_seed_row(source_code=sad_new)
    sad_seed["slug"] = "plan_0050_sad"

    monkeypatch.setattr(
        catalog,
        "build_system_opportunity_strategy_rows",
        lambda *a, **k: [happy_seed, sad_seed],
    )

    original_reset = catalog.reset_strategy_to_factory

    async def _flaky_reset(session, slug):
        if slug == "plan_0050_sad":
            raise RuntimeError("simulated reset failure")
        return await original_reset(session, slug)

    monkeypatch.setattr(catalog, "reset_strategy_to_factory", _flaky_reset)

    async with session_factory() as session:
        session.add(Strategy(**{**happy_seed, "source_code": happy_old}))
        session.add(Strategy(**{**sad_seed, "source_code": sad_old}))
        await session.commit()

        summary = await catalog.resync_system_strategies_with_disk(
            session, process_label="test"
        )

    # The happy slug still resynced; the sad slug is in errors.
    resynced_slugs = {entry["slug"] for entry in summary["resynced"]}
    assert resynced_slugs == {"plan_0050_happy"}

    error_slugs = {entry["slug"] for entry in summary["errors"]}
    assert error_slugs == {"plan_0050_sad"}
    assert "simulated reset failure" in summary["errors"][0]["error"]
