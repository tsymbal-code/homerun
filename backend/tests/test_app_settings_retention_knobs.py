"""Plan 0049 Task 1 — DB-backed trader_events retention knobs.

Pins the four invariants from the plan:

1. ``maintenance_payload`` returns the documented defaults (7 / 90)
   when the AppSettings row hasn't been populated yet.
2. ``apply_update_request`` round-trips PUT values back into the
   ORM row.
3. ``MaintenanceSettings`` Pydantic schema accepts both knobs and
   surfaces the documented defaults.
4. ``apply_search_filters`` populates
   ``config.settings.TRADER_EVENTS_FIREHOSE_RETENTION_DAYS`` /
   ``TRADER_EVENTS_OTHER_RETENTION_DAYS`` from the DB row at
   FastAPI startup.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import config  # noqa: E402
from api.routes_settings import MaintenanceSettings  # noqa: E402
from api.settings_helpers import apply_update_request, maintenance_payload  # noqa: E402
from models.database import AppSettings, Base  # noqa: E402
from tests.postgres_test_db import build_postgres_session_factory  # noqa: E402


def _make_request(maintenance_section):
    return SimpleNamespace(
        polymarket=None,
        kalshi=None,
        oracle=None,
        llm=None,
        notifications=None,
        scanner=None,
        live_execution=None,
        redeemer=None,
        maintenance=maintenance_section,
        discovery=None,
        search_filters=None,
        trading_proxy=None,
        events=None,
        ui_lock=None,
    )


def test_maintenance_settings_pydantic_defaults_match_plan():
    payload = MaintenanceSettings()
    assert payload.trader_events_firehose_retention_days == 7
    assert payload.trader_events_other_retention_days == 90


def test_maintenance_settings_pydantic_validates_bounds():
    with pytest.raises(Exception):
        MaintenanceSettings(trader_events_firehose_retention_days=0)
    with pytest.raises(Exception):
        MaintenanceSettings(trader_events_other_retention_days=0)
    with pytest.raises(Exception):
        MaintenanceSettings(trader_events_firehose_retention_days=400)
    with pytest.raises(Exception):
        MaintenanceSettings(trader_events_other_retention_days=4000)


def test_maintenance_payload_defaults_when_columns_unset():
    settings = AppSettings(id="default")
    settings.trader_events_firehose_retention_days = None
    settings.trader_events_other_retention_days = None

    payload = maintenance_payload(settings)

    assert payload["trader_events_firehose_retention_days"] == 7
    assert payload["trader_events_other_retention_days"] == 90


def test_maintenance_payload_reflects_db_row():
    settings = AppSettings(id="default")
    settings.trader_events_firehose_retention_days = 14
    settings.trader_events_other_retention_days = 180

    payload = maintenance_payload(settings)

    assert payload["trader_events_firehose_retention_days"] == 14
    assert payload["trader_events_other_retention_days"] == 180


def test_apply_update_request_round_trips_both_knobs():
    settings = AppSettings(id="default")
    maintenance = MaintenanceSettings(
        trader_events_firehose_retention_days=10,
        trader_events_other_retention_days=120,
    )
    request = _make_request(maintenance)

    apply_update_request(settings, request)

    assert settings.trader_events_firehose_retention_days == 10
    assert settings.trader_events_other_retention_days == 120


@pytest.mark.asyncio
async def test_apply_search_filters_populates_settings_singleton(monkeypatch):
    """Smoke: a non-default DB row hot-loads into ``config.settings``.

    Mirrors the FastAPI startup invocation from
    ``backend/main.py``'s lifespan and the worker plane's
    ``WorkerHost._initialize_services`` (both call
    ``apply_runtime_settings_overrides()`` which fans out to
    ``apply_search_filters()``).
    """
    engine, session_factory = await build_postgres_session_factory(
        Base, "trader_events_retention_knobs"
    )
    try:
        import models.database as models_database

        monkeypatch.setattr(models_database, "AsyncSessionLocal", session_factory)

        async with session_factory() as session:
            row = AppSettings(id="default")
            row.trader_events_firehose_retention_days = 21
            row.trader_events_other_retention_days = 365
            session.add(row)
            await session.commit()

        await config.apply_search_filters()

        assert config.settings.TRADER_EVENTS_FIREHOSE_RETENTION_DAYS == 21
        assert config.settings.TRADER_EVENTS_OTHER_RETENTION_DAYS == 365
    finally:
        # Restore safe defaults so other tests don't see leaked values.
        object.__setattr__(config.settings, "TRADER_EVENTS_FIREHOSE_RETENTION_DAYS", 7)
        object.__setattr__(config.settings, "TRADER_EVENTS_OTHER_RETENTION_DAYS", 90)
        await engine.dispose()
