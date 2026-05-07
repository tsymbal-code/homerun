"""Unit tests for ``services.market_tag_aggregator``.

Plan: 0005.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models.database import Base
from services.market_tag_aggregator import (
    _normalize_tag,
    record_tags_from_markets,
)
from tests.postgres_test_db import build_postgres_session_factory


def _market(tags: list[str]) -> SimpleNamespace:
    return SimpleNamespace(tags=list(tags))


def _event(tags: list[str]) -> SimpleNamespace:
    return SimpleNamespace(tags=list(tags))


def test_normalize_tag_lowercases_and_strips() -> None:
    assert _normalize_tag("  Crypto  ") == "crypto"
    assert _normalize_tag("POLITICS") == "politics"


def test_normalize_tag_drops_empty_and_none() -> None:
    assert _normalize_tag("") is None
    assert _normalize_tag(None) is None
    assert _normalize_tag("   ") is None


@pytest.mark.asyncio
async def test_record_tags_from_markets_upserts_and_advances(tmp_path):
    engine, session_factory = await build_postgres_session_factory(
        Base, "market_tag_aggregator_upsert"
    )
    try:
        markets = [
            _market(["Crypto", "BTC"]),
            _market(["bitcoin", "Crypto"]),
            _market([""]),
        ]
        events = [
            _event(["politics"]),
            _event(["Crypto", "memes"]),
        ]

        async with session_factory() as session:
            written = await record_tags_from_markets(session, events, markets)
        # crypto + btc + bitcoin + politics + memes = 5 distinct
        assert written == 5

        async with session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT tag, occurrences FROM market_tags_seen "
                        "ORDER BY tag"
                    )
                )
            ).all()
        seen = {row[0]: row[1] for row in rows}
        assert seen == {
            "bitcoin": 1,
            "btc": 1,
            "crypto": 1,
            "memes": 1,
            "politics": 1,
        }

        # Re-running advances last_seen and bumps occurrences for existing rows.
        async with session_factory() as session:
            written2 = await record_tags_from_markets(session, events, markets)
        assert written2 == 5

        async with session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT tag, occurrences FROM market_tags_seen "
                        "ORDER BY tag"
                    )
                )
            ).all()
        seen2 = {row[0]: row[1] for row in rows}
        assert seen2 == {
            "bitcoin": 2,
            "btc": 2,
            "crypto": 2,
            "memes": 2,
            "politics": 2,
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_record_tags_returns_zero_when_no_tags(tmp_path):
    engine, session_factory = await build_postgres_session_factory(
        Base, "market_tag_aggregator_empty"
    )
    try:
        async with session_factory() as session:
            written = await record_tags_from_markets(session, [], [])
        assert written == 0

        async with session_factory() as session:
            written = await record_tags_from_markets(
                session, [_event([])], [_market([])]
            )
        assert written == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_record_tags_respects_runtime_kill_switch(tmp_path, monkeypatch):
    from services import market_tag_aggregator as mta

    engine, session_factory = await build_postgres_session_factory(
        Base, "market_tag_aggregator_killswitch"
    )
    try:
        monkeypatch.setattr(mta.settings, "MARKET_TAG_AGGREGATOR_ENABLED", False)
        async with session_factory() as session:
            written = await record_tags_from_markets(
                session, [_event(["a"])], [_market(["b"])]
            )
        assert written == 0
    finally:
        await engine.dispose()
