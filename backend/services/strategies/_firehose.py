"""Crypto strategy firehose — terminal-visible per-gate evaluation events.

The trader Terminal tab only renders events emitted via
``buffer_trader_event``.  Crypto strategies historically silently
``return None`` at each rejection point; this helper makes those
decisions visible to the user, tagged with a verbosity tier so the
volume can be tuned in the UI.

Tiers (lowest → loudest, matched to the UI volume dial):

* ``WHISPER`` — every market evaluated every cycle, even ones that
  fail the cheapest gates (timeframe match, asset list, milestone
  not yet crossed).  Hundreds of events per minute under load.
* ``MURMUR``  — only markets that passed the cheap gates and died
  on a meaningful one (oracle freshness, distance, VWAP, book depth).
* ``VOICE``   — an Opportunity was emitted; passed every gate.
* ``SHOUT``   — orders / executions (used by the execution layer,
  not strategies).
* ``ALARM``   — errors / exceptions; emitted as ``severity="error"``
  and always shown regardless of the user's volume setting.

Firehose events use ``event_type="firehose_gate"`` (single-gate
rejections), ``event_type="firehose_evaluation"`` (full gate-by-gate
summary), and ``event_type="firehose_emit"`` (opportunity emitted).
All carry ``source="crypto"`` so the UI can route them to the Crypto
bot's Terminal tab.

Trader-id is intentionally ``None`` — these events describe global
strategy state, not a specific trader's decision flow.  The UI
matches them to a trader by ``source_key`` + the trader's enabled
strategies.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Iterable

import time as _time

from services.trader_hot_state import buffer_trader_event
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Strategy-emit eligibility cache
# ---------------------------------------------------------------------------
#
# Pre-fix, every strategy that ran on_event for a tick fired firehose
# evaluation events to the trader-events stream.  That meant:
#   * Strategies loaded but bound to no active trader (e.g. spike_
#     reversion when no trader has it in source_configs) still spammed
#     the Live Pulse feed with rejections.
#   * Events fired even when the orchestrator was disabled.
#   * Per-bot terminals leaked events from strategies the bot doesn't
#     run (because the events carried no trader_id).
#
# This cache resolves all three:
#   * ``_orchestrator_enabled`` → if False, suppress emit entirely.
#   * ``_strategy_to_trader_ids`` → the set of trader_ids (live AND
#     shadow, per Plan 0044) that have ``strategy_slug`` in their
#     ``source_configs``.  If empty, suppress emit (no one consumes
#     this strategy's signals).  Otherwise, tag the event payload
#     with the list so per-bot terminals can filter by
#     ``trader_id IN bound_trader_ids``.
#
#   Plan 0044: pre-fix this cache filtered to ``mode='live'`` only,
#   which silenced firehose telemetry for shadow traders — leaving
#   shadow operators with zero visibility into why their strategy
#   wasn't emitting opportunities. We now include shadow bindings
#   so the Terminal tab fires for both tiers. Live behaviour for the
#   live-only case is unchanged because the live trader stays in the
#   binding map.
#
# TTL is short (3 s) — orchestrator enable / trader-config edits show
# up to firehose within a single refresh.

_BINDING_TTL_SECONDS = 3.0
# Hard ceiling on the cache age before we *must* block to refresh.
# Between TTL_SECONDS and STALE_HARD_SECONDS we serve the stale value
# and kick off a background refresh — that prevents a thundering herd
# of evaluation tasks from queueing behind one DB roundtrip when the
# DB momentarily slows down.  Above the hard ceiling we resume blocking
# behaviour so we never return a wildly outdated binding map.
_BINDING_STALE_HARD_SECONDS = 30.0
_orchestrator_enabled: bool = False
_strategy_to_trader_ids: dict[str, list[str]] = {}
_binding_cache_at: float = 0.0
_binding_refresh_lock: asyncio.Lock | None = None
_binding_refresh_inflight: bool = False

# ---------------------------------------------------------------------------
# Plan 0054 — Firehose emission backpressure (tightened).
#
# Original observation (kept for historical context): stall dumps
# showed 1000+ tasks parked at ``_firehose.py:emit_evaluation`` and
# ``emit_emit``.  Every crypto_update tick, six strategies each emit
# several gate/eval/emit events per market they consider, and the
# fire-and-forget scheduling overhead alone was filling the event
# loop.  Plan 0054 measured steady-state at ~150 in-flight peak
# (2026-05-12), so the original 256 ceiling was unnecessarily
# generous and the budget never bit in steady-state.
#
# Firehose events are observability, not load-bearing.  Drop them
# under pressure rather than letting them gum up the event loop.
# Two layers of backpressure now apply, in order:
#
# 1. **Min-verbosity floor (Plan 0054).** ``emit_*_nowait`` /
#    ``emit_*`` short-circuit BEFORE building or scheduling the
#    coroutine when ``tier_rank(verbosity) <
#    _MIN_VERBOSITY_RANK``.  This is a pre-budget drop and bumps
#    ``_below_floor_emission_drops``, not ``_dropped_emission_tasks``.
#    Default floor is ``MURMUR`` (rank 2), which matches the UI's
#    default volume dial and drops the WHISPER-tier per-gate
#    evaluations that dominate the firehose load.
# 2. **In-flight budget.** When the budget is saturated,
#    ``_fire_and_forget`` closes the coroutine without scheduling
#    and bumps ``_dropped_emission_tasks``.  64 is the post-Plan-0054
#    ceiling — 2.5× the peak measured against a 5 strategy × 16
#    market × 12 gate fan-out with the WHISPER stream already
#    suppressed by the floor.
_INFLIGHT_TASK_BUDGET = 64
_inflight_emission_tasks: int = 0
_dropped_emission_tasks: int = 0
_below_floor_emission_drops: int = 0


async def _refresh_binding_cache() -> None:
    """Pull orchestrator state + strategy→trader binding map from DB."""
    global _orchestrator_enabled, _strategy_to_trader_ids, _binding_cache_at
    try:
        from sqlalchemy import select
        from models.database import (
            AsyncSessionLocal,
            Trader,
            TraderOrchestratorControl,
        )

        async with AsyncSessionLocal() as session:
            control = await session.get(TraderOrchestratorControl, "default")
            orchestrator_enabled = bool(
                control and control.is_enabled and not control.is_paused and not control.kill_switch
            )
            traders = (
                (
                    await session.execute(
                        select(Trader).where(Trader.is_enabled.is_(True))
                    )
                )
                .scalars()
                .all()
            )
        new_map: dict[str, list[str]] = {}
        for trader in traders:
            # Plan 0044: cross-mode binding. Live AND shadow traders both
            # populate the firehose binding map so the Terminal tab and
            # `trader_events` firehose_evaluation rows fire for shadow
            # operators too. Previously a `mode != "live"` continue lived
            # here, silencing every shadow-bound strategy's telemetry.
            cfgs = getattr(trader, "source_configs_json", None) or []
            if isinstance(cfgs, str):
                try:
                    import json as _json
                    cfgs = _json.loads(cfgs)
                except Exception:
                    cfgs = []
            if not isinstance(cfgs, list):
                continue
            for cfg in cfgs:
                if not isinstance(cfg, dict):
                    continue
                if not cfg.get("enabled", True):
                    continue
                slug = str(cfg.get("strategy_key") or "").strip().lower()
                if slug:
                    new_map.setdefault(slug, []).append(str(trader.id))
        _orchestrator_enabled = orchestrator_enabled
        _strategy_to_trader_ids = new_map
        _binding_cache_at = _time.monotonic()
    except Exception as exc:
        logger.debug("firehose binding cache refresh failed", exc_info=exc)


def _binding_cache_fresh() -> bool:
    return (_time.monotonic() - _binding_cache_at) < _BINDING_TTL_SECONDS


def _binding_cache_hard_stale() -> bool:
    return (_time.monotonic() - _binding_cache_at) >= _BINDING_STALE_HARD_SECONDS


async def _refresh_binding_cache_guarded() -> None:
    """Refresh the cache but never let two coroutines hit the DB at once.

    On contention this returns immediately — the in-flight refresh
    will update the shared globals when it completes.
    """
    global _binding_refresh_inflight
    if _binding_refresh_inflight:
        return
    _binding_refresh_inflight = True
    try:
        await _refresh_binding_cache()
    finally:
        _binding_refresh_inflight = False


async def _ensure_binding_cache() -> None:
    global _binding_refresh_lock
    if _binding_cache_fresh():
        return
    if _binding_refresh_lock is None:
        _binding_refresh_lock = asyncio.Lock()
    # Soft-stale: serve the cached value, schedule a background refresh
    # if one isn't already running.  This breaks the thundering-herd
    # behaviour where every concurrent evaluation task queues behind
    # one DB roundtrip on cache expiry.
    if not _binding_cache_hard_stale():
        if not _binding_refresh_inflight:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_refresh_binding_cache_guarded())
            except RuntimeError:
                pass
        return
    # Hard-stale: cache is too old to trust, block on the refresh.
    async with _binding_refresh_lock:
        if _binding_cache_fresh():
            return
        await _refresh_binding_cache()


async def _emit_should_fire(strategy_slug: str) -> tuple[bool, list[str]]:
    """Return (should_emit, bound_trader_ids).

    ``False, []`` means: drop the event entirely — either the
    orchestrator is off or the strategy has no live consumer.
    ``True, [trader_ids…]`` means: emit and tag the payload so per-
    bot terminals can filter.
    """
    await _ensure_binding_cache()
    if not _orchestrator_enabled:
        return False, []
    bound = _strategy_to_trader_ids.get(str(strategy_slug or "").strip().lower(), [])
    if not bound:
        return False, []
    return True, list(bound)


# Verbosity tiers — frontend's volume dial selects a minimum tier and
# everything at-or-louder is shown.  Order matters: WHISPER < MURMUR <
# VOICE < SHOUT.  ALARM is severity, not verbosity.
WHISPER = "whisper"
MURMUR = "murmur"
VOICE = "voice"
SHOUT = "shout"

_TIER_RANK = {WHISPER: 1, MURMUR: 2, VOICE: 3, SHOUT: 4}


def tier_rank(verbosity: str | None) -> int:
    if not verbosity:
        return 0
    return _TIER_RANK.get(str(verbosity).strip().lower(), 0)


# Plan 0054 — Min-verbosity floor (pre-budget, pre-task drop).
#
# ``_MIN_VERBOSITY_RANK`` is resolved lazily on the first emit call
# and cached for the lifetime of the process.  Strategies fire
# ``emit_*_nowait`` hundreds of times per second; reading
# ``settings.FIREHOSE_MIN_VERBOSITY`` per call would itself
# contribute to the load this floor exists to reduce.
#
# The knob is process-startup-only by design: changing
# ``FIREHOSE_MIN_VERBOSITY`` in env requires a worker-trading
# restart to take effect.  Live tuning via ``app_settings`` would
# race with the cached read.  See Task 3 of plan 0054 in
# ``docs/plans/completed/0054-cap-firehose-emission-load.md``.
_MIN_VERBOSITY_RANK: int | None = None


def _resolve_min_verbosity_rank() -> int:
    """Read ``settings.FIREHOSE_MIN_VERBOSITY`` once and convert to rank.

    Falls back to MURMUR (rank 2) when unset, unknown, or unreadable.
    Imported lazily to avoid an import cycle if a strategy is loaded
    before ``config`` finishes initialising.
    """
    try:
        from config import settings  # local import — see docstring
        raw = getattr(settings, "FIREHOSE_MIN_VERBOSITY", MURMUR)
    except Exception:
        raw = MURMUR
    rank = tier_rank(raw)
    return rank if rank > 0 else _TIER_RANK[MURMUR]


def _below_floor(verbosity: str | None) -> bool:
    """Return True when this verbosity is below the configured floor.

    Side-effect: bumps ``_below_floor_emission_drops`` on True so the
    counter surfaced via ``get_firehose_stats()`` reflects every
    pre-budget drop.  Single-threaded asyncio context — no lock
    needed on the increment.
    """
    global _MIN_VERBOSITY_RANK, _below_floor_emission_drops
    if _MIN_VERBOSITY_RANK is None:
        _MIN_VERBOSITY_RANK = _resolve_min_verbosity_rank()
    if tier_rank(verbosity) < _MIN_VERBOSITY_RANK:
        _below_floor_emission_drops += 1
        return True
    return False


@dataclass(slots=True)
class GateResult:
    """One gate evaluated for one market.

    ``passed`` → True/False/None (None = "not evaluated; earlier gate
    short-circuited and skipped this one").  ``score`` is whatever
    numeric the gate measures (distance bps, oracle age ms, VWAP
    price, etc.) — frontend renders it raw.
    """

    name: str          # short slug, e.g. "timeframe", "asset_enabled", "min_distance"
    label: str         # human-readable label for the UI
    passed: bool | None
    score: float | None = None
    detail: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "passed": self.passed,
            "score": self.score,
            "detail": self.detail,
        }


def _market_summary(market: dict[str, Any] | Any) -> dict[str, Any]:
    """Best-effort identifying fields for a market.

    Accepts the dict form crypto strategies see in ``crypto_update``
    payloads, or any object with the same attributes.
    """
    if isinstance(market, dict):
        get = market.get
    else:
        get = lambda k, default=None: getattr(market, k, default)  # noqa: E731
    return {
        "market_id": str(get("condition_id") or get("id") or ""),
        "slug": str(get("slug") or ""),
        "question": str(get("question") or ""),
        "asset": str(get("asset") or get("symbol") or get("coin") or "").upper() or None,
        "timeframe": str(get("timeframe") or "") or None,
    }


def _fire_and_forget(coro) -> None:
    """Schedule an emission without blocking the caller.

    Strategies run inside the market_runtime dispatch loop; we don't
    want gate emissions to add latency to the hot path.  If no event
    loop is available (sync test path), drop the event silently.

    Fix OO — drop emissions when the in-flight budget is saturated.
    Firehose events are debug observability and must never queue
    enough tasks to saturate the event loop or stall the orchestrator.
    """
    global _inflight_emission_tasks, _dropped_emission_tasks
    if _inflight_emission_tasks >= _INFLIGHT_TASK_BUDGET:
        try:
            coro.close()
        except Exception:
            pass
        _dropped_emission_tasks += 1
        # Log every 1000th drop so the situation is visible without
        # spamming the log itself when the firehose is over-budget.
        if _dropped_emission_tasks % 1000 == 1:
            logger.warning(
                "firehose dropping emissions (in-flight budget exhausted)",
                extra={
                    "inflight": _inflight_emission_tasks,
                    "budget": _INFLIGHT_TASK_BUDGET,
                    "total_dropped": _dropped_emission_tasks,
                },
            )
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            coro.close()
        except Exception:
            pass
        return
    _inflight_emission_tasks += 1
    task = loop.create_task(_tracked_emission(coro))
    # Avoid 'Task was destroyed but it is pending' warnings if the
    # loop tears down before the task runs.
    task.add_done_callback(lambda _t: None)


async def _tracked_emission(coro) -> None:
    global _inflight_emission_tasks
    try:
        await coro
    finally:
        _inflight_emission_tasks -= 1


def get_firehose_stats() -> dict[str, int]:
    """Expose in-flight / dropped counters for observability."""
    return {
        "inflight_emission_tasks": _inflight_emission_tasks,
        "dropped_emission_tasks_total": _dropped_emission_tasks,
        "below_floor_emission_drops": _below_floor_emission_drops,
        "inflight_budget": _INFLIGHT_TASK_BUDGET,
    }


async def emit_gate(
    *,
    strategy_slug: str,
    market: dict[str, Any] | Any,
    gate: GateResult,
    verbosity: str = MURMUR,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit a single-gate decision (typically a rejection)."""
    if _below_floor(verbosity):
        return
    should_emit, bound_trader_ids = await _emit_should_fire(strategy_slug)
    if not should_emit:
        return
    market_info = _market_summary(market)
    pass_word = "passed" if gate.passed else ("skipped" if gate.passed is None else "rejected")
    msg = (
        f"{strategy_slug} • {market_info.get('slug') or market_info.get('market_id') or '?'} • "
        f"{gate.label}: {pass_word}"
    )
    if gate.detail:
        msg += f" — {gate.detail}"
    payload: dict[str, Any] = {
        "strategy_slug": strategy_slug,
        "source_key": "crypto",
        "market": market_info,
        "gate": gate.to_payload(),
        "bound_trader_ids": bound_trader_ids,
    }
    if extra:
        payload.update(extra)
    try:
        await buffer_trader_event(
            event_type="firehose_gate",
            severity="info",
            verbosity=verbosity,
            source="crypto",
            message=msg,
            payload=payload,
        )
    except Exception as exc:  # never let firehose break a strategy
        logger.debug("firehose emit_gate failed: %s", exc)


def emit_gate_nowait(
    *,
    strategy_slug: str,
    market: dict[str, Any] | Any,
    gate: GateResult,
    verbosity: str = MURMUR,
    extra: dict[str, Any] | None = None,
) -> None:
    """Sync convenience — schedule emission and return immediately.

    Use from sync code paths (most strategy gates).  Hot path is
    unaffected.
    """
    if _below_floor(verbosity):
        return
    _fire_and_forget(
        emit_gate(
            strategy_slug=strategy_slug,
            market=market,
            gate=gate,
            verbosity=verbosity,
            extra=extra,
        )
    )


async def emit_evaluation(
    *,
    strategy_slug: str,
    market: dict[str, Any] | Any,
    gates: Iterable[GateResult],
    outcome: str,                 # "emitted" | "rejected" | "skipped"
    verbosity: str = WHISPER,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit a full gate-by-gate evaluation summary for one market.

    Use this for WHISPER mode when you want to record the entire
    decision tree, including gates that didn't run because an
    earlier one short-circuited.
    """
    if _below_floor(verbosity):
        return
    should_emit, bound_trader_ids = await _emit_should_fire(strategy_slug)
    if not should_emit:
        return
    market_info = _market_summary(market)
    gate_list = [g.to_payload() for g in gates]
    failed = [g for g in gate_list if g.get("passed") is False]
    summary = (
        f"{strategy_slug} • {market_info.get('slug') or market_info.get('market_id') or '?'} • "
        f"{outcome.upper()}"
    )
    if outcome == "rejected" and failed:
        summary += f" at {failed[0].get('label') or failed[0].get('name')}"
    payload: dict[str, Any] = {
        "strategy_slug": strategy_slug,
        "source_key": "crypto",
        "market": market_info,
        "outcome": outcome,
        "gates": gate_list,
        "bound_trader_ids": bound_trader_ids,
    }
    if extra:
        payload.update(extra)
    try:
        await buffer_trader_event(
            event_type="firehose_evaluation",
            severity="info",
            verbosity=verbosity,
            source="crypto",
            message=summary,
            payload=payload,
        )
    except Exception as exc:
        logger.debug("firehose emit_evaluation failed: %s", exc)


def emit_evaluation_nowait(
    *,
    strategy_slug: str,
    market: dict[str, Any] | Any,
    gates: Iterable[GateResult],
    outcome: str,
    verbosity: str = WHISPER,
    extra: dict[str, Any] | None = None,
) -> None:
    if _below_floor(verbosity):
        return
    _fire_and_forget(
        emit_evaluation(
            strategy_slug=strategy_slug,
            market=market,
            gates=gates,
            outcome=outcome,
            verbosity=verbosity,
            extra=extra,
        )
    )


async def emit_emit(
    *,
    strategy_slug: str,
    market: dict[str, Any] | Any,
    detail: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """An Opportunity was produced (passed every gate).  VOICE tier."""
    if _below_floor(VOICE):
        return
    should_emit, bound_trader_ids = await _emit_should_fire(strategy_slug)
    if not should_emit:
        return
    market_info = _market_summary(market)
    msg = (
        f"{strategy_slug} • {market_info.get('slug') or market_info.get('market_id') or '?'} • "
        f"OPPORTUNITY EMITTED"
    )
    if detail:
        msg += f" — {detail}"
    payload: dict[str, Any] = {
        "strategy_slug": strategy_slug,
        "source_key": "crypto",
        "market": market_info,
        "detail": detail,
        "bound_trader_ids": bound_trader_ids,
    }
    if extra:
        payload.update(extra)
    try:
        await buffer_trader_event(
            event_type="firehose_emit",
            severity="info",
            verbosity=VOICE,
            source="crypto",
            message=msg,
            payload=payload,
        )
    except Exception as exc:
        logger.debug("firehose emit_emit failed: %s", exc)


def emit_emit_nowait(
    *,
    strategy_slug: str,
    market: dict[str, Any] | Any,
    detail: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    if _below_floor(VOICE):
        return
    _fire_and_forget(
        emit_emit(
            strategy_slug=strategy_slug,
            market=market,
            detail=detail,
            extra=extra,
        )
    )
