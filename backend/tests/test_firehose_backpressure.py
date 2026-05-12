"""Plan 0054: firehose pre-budget verbosity floor + in-flight budget.

Plan 0054 added a min-verbosity floor evaluated **before**
``_fire_and_forget`` schedules a firehose emission. Sub-floor
verbosities are dropped at the call site without allocating an
asyncio task, and the resolved rank is cached for the process
lifetime. The budget=64 ceiling also stays as a defensive cap.

This module pins the load-bearing contract so a future refactor
cannot silently re-raise the budget or accept WHISPER below the
floor.

References:
- ``backend/services/strategies/_firehose.py`` — module under test.
- ``backend/config.py:FIREHOSE_MIN_VERBOSITY`` — env knob (default
  ``murmur``, process-startup-only).
- ``docs/plans/work-artifacts/0054-pre-fix-evidence.md`` — the
  pre-fix baseline these tests guard against regressing back to.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


def _reset_firehose_module(monkeypatch, *, min_verbosity: str | None = None):
    """Reset every cached / mutable global in ``_firehose.py``.

    Tests share a process; without an explicit reset the second test
    inherits the first test's ``_inflight_emission_tasks`` and the
    cached ``_MIN_VERBOSITY_RANK``.
    """
    from services.strategies import _firehose as fh

    monkeypatch.setattr(fh, "_inflight_emission_tasks", 0, raising=False)
    monkeypatch.setattr(fh, "_dropped_emission_tasks", 0, raising=False)
    monkeypatch.setattr(fh, "_below_floor_emission_drops", 0, raising=False)
    monkeypatch.setattr(fh, "_MIN_VERBOSITY_RANK", None, raising=False)
    # Bypass the binding cache — every test that triggers ``emit_*``
    # gets a synthetic "strategy alpha bound to trader xyz" entry so
    # ``_emit_should_fire`` returns True without hitting the DB.
    monkeypatch.setattr(fh, "_orchestrator_enabled", True, raising=False)
    monkeypatch.setattr(
        fh,
        "_strategy_to_trader_ids",
        {"alpha": ["trader-1"]},
        raising=False,
    )
    monkeypatch.setattr(fh, "_binding_cache_at", 1e18, raising=False)

    if min_verbosity is not None:
        from config import settings as _cfg

        monkeypatch.setattr(_cfg, "FIREHOSE_MIN_VERBOSITY", min_verbosity, raising=False)


def _market():
    """Minimal market dict matching ``_market_summary`` expectations."""
    return {
        "condition_id": "cond-1",
        "slug": "test-market",
        "question": "Will X happen?",
        "asset": "BTC",
        "timeframe": "5m",
    }


def _gate():
    from services.strategies._firehose import GateResult

    return GateResult(name="timeframe", label="Timeframe", passed=False)


# ---------------------------------------------------------------------------
# Test 1 — sub-floor sync emit drops at the call site (no asyncio task)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sub_floor_evaluation_nowait_drops_before_scheduling(monkeypatch):
    """``emit_evaluation_nowait(verbosity=WHISPER)`` with floor=MURMUR
    must:
      - NOT increment ``_inflight_emission_tasks``;
      - increment ``_below_floor_emission_drops`` by exactly 1;
      - NOT schedule any asyncio task.

    This is the central Plan 0054 contract — the pre-budget drop
    that reclaims event-loop slice from the WHISPER firehose.
    """
    _reset_firehose_module(monkeypatch, min_verbosity="murmur")

    from services.strategies import _firehose as fh

    async def _run() -> tuple[int, int, set]:
        before_tasks = set(asyncio.all_tasks())
        fh.emit_evaluation_nowait(
            strategy_slug="alpha",
            market=_market(),
            gates=[_gate()],
            outcome="rejected",
            verbosity=fh.WHISPER,
        )
        after_tasks = set(asyncio.all_tasks())
        new_tasks = after_tasks - before_tasks - {asyncio.current_task()}
        return fh._inflight_emission_tasks, fh._below_floor_emission_drops, new_tasks

    inflight, below_floor, new_tasks = asyncio.run(_run())

    assert inflight == 0, "in-flight counter must NOT tick on a sub-floor drop"
    assert below_floor == 1, "below-floor counter must tick exactly once"
    assert not new_tasks, f"no asyncio task may be scheduled, got {new_tasks!r}"


# ---------------------------------------------------------------------------
# Test 2 — at-floor verbosity is scheduled normally (boundary is >=, not >)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_at_floor_gate_nowait_schedules_and_drains(monkeypatch):
    """``emit_gate_nowait(verbosity=MURMUR)`` with floor=MURMUR MUST
    schedule the task (floor is "below" not "below-or-equal").

    Replaces ``buffer_trader_event`` with a sentinel awaitable so the
    test stays hermetic (no DB / no Redis). After the task drains,
    ``_inflight_emission_tasks`` returns to 0 via the
    ``_tracked_emission`` ``finally`` clause.
    """
    _reset_firehose_module(monkeypatch, min_verbosity="murmur")

    from services.strategies import _firehose as fh

    calls: list[dict[str, Any]] = []

    async def _stub_buffer(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(fh, "buffer_trader_event", _stub_buffer, raising=True)

    async def _run() -> tuple[int, int, int]:
        fh.emit_gate_nowait(
            strategy_slug="alpha",
            market=_market(),
            gate=_gate(),
            verbosity=fh.MURMUR,
        )
        inflight_immediate = fh._inflight_emission_tasks
        # Yield enough times for the scheduled task to actually run.
        for _ in range(5):
            await asyncio.sleep(0)
        inflight_drained = fh._inflight_emission_tasks
        return inflight_immediate, inflight_drained, fh._below_floor_emission_drops

    inflight_immediate, inflight_drained, below_floor = asyncio.run(_run())

    assert inflight_immediate == 1, "at-floor MURMUR must schedule a task"
    assert inflight_drained == 0, "_tracked_emission must decrement on completion"
    assert below_floor == 0, "at-floor emit must NOT count as below-floor"
    assert len(calls) == 1, f"buffer_trader_event must be called once, got {len(calls)}"
    assert calls[0]["event_type"] == "firehose_gate"
    assert calls[0]["verbosity"] == fh.MURMUR


# ---------------------------------------------------------------------------
# Test 3 — _MIN_VERBOSITY_RANK is resolved once across many emit calls
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_min_verbosity_rank_resolved_once(monkeypatch):
    """``_resolve_min_verbosity_rank`` may run at most once across N
    ``emit_*_nowait`` calls — strategies hit this code path
    hundreds of times per second and reading ``settings.*`` per call
    is the exact overhead Plan 0054 exists to avoid.
    """
    _reset_firehose_module(monkeypatch, min_verbosity="murmur")

    from services.strategies import _firehose as fh

    resolve_count = {"n": 0}
    original_resolve = fh._resolve_min_verbosity_rank

    def _counting_resolve():
        resolve_count["n"] += 1
        return original_resolve()

    monkeypatch.setattr(fh, "_resolve_min_verbosity_rank", _counting_resolve)

    async def _run():
        # All sub-floor — every one of these would call resolve if the
        # cache weren't working.
        for _ in range(5):
            fh.emit_evaluation_nowait(
                strategy_slug="alpha",
                market=_market(),
                gates=[_gate()],
                outcome="rejected",
                verbosity=fh.WHISPER,
            )

    asyncio.run(_run())

    assert resolve_count["n"] == 1, (
        f"settings.FIREHOSE_MIN_VERBOSITY must be read once, "
        f"got {resolve_count['n']} reads"
    )
    assert fh._below_floor_emission_drops == 5


# ---------------------------------------------------------------------------
# Test 4 — budget saturation does NOT conflate with below-floor counter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_budget_saturation_drops_into_dropped_counter_not_below_floor(monkeypatch):
    """At ``_inflight_emission_tasks == _INFLIGHT_TASK_BUDGET``, an
    above-floor emit must drop via the existing in-flight backpressure
    path (``_dropped_emission_tasks``) — NOT into
    ``_below_floor_emission_drops``. The two counters describe
    different failure modes and must stay disjoint.
    """
    _reset_firehose_module(monkeypatch, min_verbosity="murmur")

    from services.strategies import _firehose as fh

    monkeypatch.setattr(fh, "_inflight_emission_tasks", fh._INFLIGHT_TASK_BUDGET, raising=False)

    async def _run() -> tuple[int, int]:
        fh.emit_gate_nowait(
            strategy_slug="alpha",
            market=_market(),
            gate=_gate(),
            verbosity=fh.VOICE,  # above floor=MURMUR, so the floor must not apply
        )
        return fh._dropped_emission_tasks, fh._below_floor_emission_drops

    dropped, below_floor = asyncio.run(_run())

    assert dropped == 1, "saturated budget must drop into _dropped_emission_tasks"
    assert below_floor == 0, "saturation drop must NOT register as below-floor"


# ---------------------------------------------------------------------------
# Test 5 — async emitter symmetry: emit_gate(WHISPER) with floor=MURMUR
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_async_emit_gate_below_floor_skips_buffer_call(monkeypatch):
    """``await emit_gate(verbosity=WHISPER)`` with floor=MURMUR must
    short-circuit BEFORE calling ``buffer_trader_event``. The async
    API is rarely called directly today (most strategies use the
    ``*_nowait`` variant), but plan 0054 chose to keep the floor
    symmetric so test paths and future callers cannot bypass it.
    """
    _reset_firehose_module(monkeypatch, min_verbosity="murmur")

    from services.strategies import _firehose as fh

    calls: list[dict[str, Any]] = []

    async def _stub_buffer(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(fh, "buffer_trader_event", _stub_buffer, raising=True)

    async def _run() -> int:
        await fh.emit_gate(
            strategy_slug="alpha",
            market=_market(),
            gate=_gate(),
            verbosity=fh.WHISPER,
        )
        return fh._below_floor_emission_drops

    below_floor = asyncio.run(_run())

    assert not calls, f"buffer_trader_event must not run, got {calls!r}"
    assert below_floor == 1, "async path must increment below-floor counter too"


# ---------------------------------------------------------------------------
# Test 6 — unknown FIREHOSE_MIN_VERBOSITY defaults to MURMUR (rank 2)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unknown_min_verbosity_falls_back_to_murmur(monkeypatch):
    """``FIREHOSE_MIN_VERBOSITY=banana`` (or any unknown value) must
    silently default to MURMUR rank (= 2). The loader must never
    raise — strategy emit paths cannot afford a startup exception
    that takes the worker down.
    """
    _reset_firehose_module(monkeypatch, min_verbosity="banana")

    from services.strategies import _firehose as fh

    rank = fh._resolve_min_verbosity_rank()

    assert rank == fh._TIER_RANK[fh.MURMUR], (
        f"unknown verbosity must default to MURMUR rank "
        f"({fh._TIER_RANK[fh.MURMUR]}), got {rank}"
    )

    # And the floor must still bite at WHISPER:
    async def _run() -> int:
        fh.emit_evaluation_nowait(
            strategy_slug="alpha",
            market=_market(),
            gates=[_gate()],
            outcome="rejected",
            verbosity=fh.WHISPER,
        )
        return fh._below_floor_emission_drops

    below_floor = asyncio.run(_run())
    assert below_floor == 1, "fallback floor must still drop WHISPER"


# ---------------------------------------------------------------------------
# Test 7 — get_firehose_stats() surfaces every Plan 0054 counter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_firehose_stats_exposes_below_floor_counter(monkeypatch):
    """Operators rely on ``get_firehose_stats()`` to validate the
    floor is biting at expected volume (Task 6 of plan 0054). Pin
    the dict shape so the diagnostic command in the plan keeps
    working.
    """
    _reset_firehose_module(monkeypatch, min_verbosity="murmur")

    from services.strategies import _firehose as fh

    stats = fh.get_firehose_stats()

    assert set(stats.keys()) == {
        "inflight_emission_tasks",
        "dropped_emission_tasks_total",
        "below_floor_emission_drops",
        "inflight_budget",
    }
    assert stats["inflight_budget"] == 64, (
        f"plan 0054 lowered the budget to 64; got {stats['inflight_budget']}"
    )
