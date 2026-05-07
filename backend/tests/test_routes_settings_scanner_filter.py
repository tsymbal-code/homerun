"""API roundtrip tests for the market tag filter on Scanner settings.

Plan: 0005.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api import routes_settings
from api.routes_settings import ScannerSettingsModel
from api.settings_helpers import apply_update_request, scanner_payload
from models.database import AppSettings, Base
from tests.postgres_test_db import build_postgres_session_factory


def test_scanner_settings_model_normalizes_market_filter_tags():
    """The pydantic validator lowercases, trims, dedupes."""
    model = ScannerSettingsModel(market_filter_tags=["Crypto", "  POLITICS ", "crypto", ""])
    assert model.market_filter_tags == ["crypto", "politics"]


def test_scanner_settings_model_accepts_empty_list_default():
    model = ScannerSettingsModel()
    assert model.market_filter_tags == []


def test_scanner_settings_model_rejects_non_list_silently():
    """Non-list values normalise to []. Defensive only — pydantic will
    surface a strict type error at the route layer, but the validator
    must not raise before getting there."""
    model = ScannerSettingsModel.model_validate({"market_filter_tags": None})
    assert model.market_filter_tags == []


@pytest.mark.asyncio
async def test_apply_update_request_persists_market_filter_tags(tmp_path):
    engine, session_factory = await build_postgres_session_factory(
        Base, "scanner_filter_persist"
    )
    try:
        async with session_factory() as session:
            row = AppSettings(id="default")
            session.add(row)
            await session.commit()
            await session.refresh(row)

        async with session_factory() as session:
            from sqlalchemy import select

            settings_row = (
                await session.execute(select(AppSettings).where(AppSettings.id == "default"))
            ).scalar_one()
            request = routes_settings.UpdateSettingsRequest(
                scanner=ScannerSettingsModel(
                    market_filter_tags=[" Crypto ", "POLITICS"],
                )
            )
            apply_update_request(settings_row, request)
            await session.commit()

        async with session_factory() as session:
            from sqlalchemy import select

            settings_row = (
                await session.execute(select(AppSettings).where(AppSettings.id == "default"))
            ).scalar_one()
            assert list(settings_row.market_filter_tags or []) == ["crypto", "politics"]
            assert settings_row.market_filter_updated_at is not None

            payload = scanner_payload(settings_row)
            assert payload["market_filter_tags"] == ["crypto", "politics"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_market_filter_available_tags_returns_recent_tags(monkeypatch):
    """``/settings/market-filter/available-tags`` returns only the
    last-24h slice, ordered by occurrences DESC then last_seen DESC."""
    engine, session_factory = await build_postgres_session_factory(
        Base, "scanner_filter_available_tags"
    )
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO market_tags_seen (tag, first_seen, last_seen, occurrences) "
                    "VALUES (:tag, :seen, :seen, :occ)"
                ),
                [
                    {"tag": "crypto", "seen": now, "occ": 100},
                    {"tag": "politics", "seen": now - timedelta(hours=1), "occ": 50},
                    {"tag": "stale", "seen": now - timedelta(days=2), "occ": 999},
                ],
            )
            await session.commit()

        # Patch the endpoint's session factory to point at the test DB.
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _session_cm():
            async with session_factory() as session:
                yield session

        def _factory():
            return _session_cm()

        monkeypatch.setattr(routes_settings, "AsyncSessionLocal", _factory)

        response = await routes_settings.get_market_filter_available_tags()
        names = [entry["name"] for entry in response["tags"]]
        assert names == ["crypto", "politics"]  # stale is dropped
        assert response["total"] == 2
        assert response["tags"][0]["occurrences"] == 100
        assert response["tags"][0]["last_seen"].endswith("Z")
    finally:
        await engine.dispose()
