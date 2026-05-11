"""Plan 0044: firehose binding cache must include shadow traders.

Pre-Plan-0044 the firehose binding refresh hard-filtered to
``mode='live'``. A shadow trader bound to a strategy never appeared in
``_strategy_to_trader_ids``, so every ``emit_evaluation_nowait`` from
that strategy got dropped by ``_emit_should_fire`` — leaving operators
with zero per-gate visibility into shadow-mode iteration.

This module pins the cross-mode contract: both live and shadow traders
populate the firehose cache; only ``is_enabled=False`` traders and
per-strategy ``enabled=False`` configs are excluded.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Fake DB session — minimal surface the firehose refresh uses.
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


class _FakeSession:
    def __init__(self, traders, control):
        self._traders = traders
        self._control = control

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, _model, _key):
        return self._control

    async def execute(self, _stmt):
        return _FakeResult(self._traders)


def _trader(id: str, mode: str, source_configs, is_enabled: bool = True):
    return SimpleNamespace(
        id=id,
        mode=mode,
        is_enabled=is_enabled,
        source_configs_json=source_configs,
    )


def _control(*, enabled: bool = True, paused: bool = False, kill: bool = False):
    return SimpleNamespace(
        is_enabled=enabled,
        is_paused=paused,
        kill_switch=kill,
    )


def _install_fake_session(monkeypatch, traders, control):
    """Patch ``AsyncSessionLocal`` so calling it returns our fake session."""
    fake = lambda: _FakeSession(traders, control)
    # The firehose refresh imports inside the function — patch both the
    # source module and any cached re-export.
    import models.database as _db

    monkeypatch.setattr(_db, "AsyncSessionLocal", fake)


def _reset_firehose_cache(monkeypatch):
    """Drop any state leaked from prior tests in this process."""
    from services.strategies import _firehose as fh

    monkeypatch.setattr(fh, "_orchestrator_enabled", False)
    monkeypatch.setattr(fh, "_strategy_to_trader_ids", {})
    monkeypatch.setattr(fh, "_binding_cache_at", 0.0)
    monkeypatch.setattr(fh, "_binding_refresh_inflight", False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_refresh_includes_shadow_traders(monkeypatch):
    """A shadow trader bound to a strategy MUST appear in the binding
    map — this is the Plan 0044 contract."""
    _reset_firehose_cache(monkeypatch)
    traders = [
        _trader(
            id="live-1",
            mode="live",
            source_configs=[
                {"source_key": "crypto", "strategy_key": "alpha", "enabled": True},
            ],
        ),
        _trader(
            id="shadow-1",
            mode="shadow",
            source_configs=[
                {"source_key": "crypto", "strategy_key": "alpha", "enabled": True},
            ],
        ),
    ]
    _install_fake_session(monkeypatch, traders, _control())

    from services.strategies import _firehose as fh

    asyncio.run(fh._refresh_binding_cache())

    assert "alpha" in fh._strategy_to_trader_ids
    bound = fh._strategy_to_trader_ids["alpha"]
    assert "live-1" in bound, f"live trader must remain bound, got {bound}"
    assert "shadow-1" in bound, f"shadow trader must now be bound, got {bound}"


@pytest.mark.unit
def test_refresh_skips_disabled_traders(monkeypatch):
    """``is_enabled=False`` traders are excluded regardless of mode —
    this guards against widening the filter too much."""
    _reset_firehose_cache(monkeypatch)
    traders = [
        _trader(
            id="live-disabled",
            mode="live",
            is_enabled=False,
            source_configs=[
                {"source_key": "crypto", "strategy_key": "alpha", "enabled": True},
            ],
        ),
        _trader(
            id="shadow-disabled",
            mode="shadow",
            is_enabled=False,
            source_configs=[
                {"source_key": "crypto", "strategy_key": "alpha", "enabled": True},
            ],
        ),
        _trader(
            id="shadow-enabled",
            mode="shadow",
            is_enabled=True,
            source_configs=[
                {"source_key": "crypto", "strategy_key": "alpha", "enabled": True},
            ],
        ),
    ]
    _install_fake_session(monkeypatch, traders, _control())

    from services.strategies import _firehose as fh

    asyncio.run(fh._refresh_binding_cache())

    bound = fh._strategy_to_trader_ids.get("alpha", [])
    assert "shadow-enabled" in bound
    assert "live-disabled" not in bound
    assert "shadow-disabled" not in bound


@pytest.mark.unit
def test_refresh_skips_per_strategy_disabled_in_source_configs(monkeypatch):
    """An enabled trader whose source-config row has
    ``enabled=False`` for a slug is excluded for that slug only —
    other slug bindings on the same trader survive."""
    _reset_firehose_cache(monkeypatch)
    traders = [
        _trader(
            id="shadow-multi",
            mode="shadow",
            source_configs=[
                {"source_key": "crypto", "strategy_key": "alpha", "enabled": True},
                {"source_key": "crypto", "strategy_key": "beta", "enabled": False},
            ],
        ),
    ]
    _install_fake_session(monkeypatch, traders, _control())

    from services.strategies import _firehose as fh

    asyncio.run(fh._refresh_binding_cache())

    assert "shadow-multi" in fh._strategy_to_trader_ids.get("alpha", [])
    assert "beta" not in fh._strategy_to_trader_ids


@pytest.mark.unit
def test_emit_should_fire_returns_true_for_shadow_only_binding(monkeypatch):
    """End-to-end: after refresh, ``_emit_should_fire`` returns
    ``(True, [shadow_id])`` so downstream ``emit_evaluation_nowait``
    actually persists. Pre-Plan-0044 this returned ``(False, [])``
    for any shadow-only strategy and silenced all telemetry."""
    _reset_firehose_cache(monkeypatch)
    traders = [
        _trader(
            id="shadow-only",
            mode="shadow",
            source_configs=[
                {"source_key": "crypto", "strategy_key": "alpha", "enabled": True},
            ],
        ),
    ]
    _install_fake_session(monkeypatch, traders, _control())

    from services.strategies import _firehose as fh

    asyncio.run(fh._refresh_binding_cache())
    should_emit, bound = asyncio.run(fh._emit_should_fire("alpha"))

    assert should_emit is True
    assert bound == ["shadow-only"]


@pytest.mark.unit
def test_emit_should_fire_returns_false_when_orchestrator_disabled(monkeypatch):
    """Orchestrator kill-switch must still gate everything off, even
    with bindings present. This is unchanged from pre-Plan-0044 —
    locking it in so the cross-mode change can't accidentally fire
    telemetry while the orchestrator is meant to be silent."""
    _reset_firehose_cache(monkeypatch)
    traders = [
        _trader(
            id="shadow-x",
            mode="shadow",
            source_configs=[
                {"source_key": "crypto", "strategy_key": "alpha", "enabled": True},
            ],
        ),
    ]
    _install_fake_session(monkeypatch, traders, _control(paused=True))

    from services.strategies import _firehose as fh

    asyncio.run(fh._refresh_binding_cache())
    should_emit, bound = asyncio.run(fh._emit_should_fire("alpha"))

    assert should_emit is False
    assert bound == []
