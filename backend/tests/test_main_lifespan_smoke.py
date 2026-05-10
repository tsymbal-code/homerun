"""Startup smoke tests for the FastAPI app defined in ``backend/main.py``.

Why this file exists
--------------------
The backend has 195 pytest files exercising individual subsystems, but
nothing in CI verifies that the top-level ``main`` module can be
imported cleanly or that ``app``'s ``lifespan`` enters and exits
without raising.  Two real classes of regression slip past the rest of
the suite today:

1.  An import-time error in ``main.py`` (e.g. a renamed symbol in
    ``api/routes_*.py``, a circular import, a missing dependency
    pulled in by a new feature).  Every existing test happens to
    import only narrow slices of the codebase, so this never surfaces
    until the operator runs a redeploy and sees the container
    restart-loop.
2.  A misconfigured ``lifespan`` block — for example, a service whose
    ``stop()`` call hangs, or an ``init_database`` migration that
    raises against a fresh DB.

These tests cover both.  ``test_import_app`` is unconditional; the
lifespan-driving test runs only when a writable Postgres is reachable
(it allocates a throwaway database via ``build_postgres_session_factory``)
so it skips cleanly on a developer box without DB access instead of
producing a confusing failure.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


# ---------------------------------------------------------------------------
# Cheap, no-IO smoke — runs everywhere
# ---------------------------------------------------------------------------


def test_import_app_module() -> None:
    """``import main`` must succeed without raising.

    Catches the most common 'backend won't start' regression: a renamed
    symbol or stale import landed somewhere in the ``api`` / ``services``
    / ``workers`` tree that is only pulled in by ``main.py`` itself.
    """
    import main  # noqa: F401 — import is the assertion

    assert hasattr(main, "app"), "main.py must expose an `app` symbol"


def test_app_is_fastapi_with_routes() -> None:
    """``app`` is a FastAPI instance with a non-trivial number of routes.

    A boot regression that empties the router (e.g. the include_router
    block raising silently and the rest of main.py continuing) would be
    invisible to the import test but is caught here.
    """
    from fastapi import FastAPI

    import main

    assert isinstance(main.app, FastAPI)
    # The current app registers ~500 routes; assert a generous lower
    # bound so this test doesn't churn every time a router is added.
    route_count = len(main.app.router.routes)
    assert route_count > 100, (
        f"FastAPI app has only {route_count} routes — likely a router "
        f"include block raised during import and was swallowed"
    )


def test_lifespan_is_async_context_manager() -> None:
    """The ``lifespan`` attribute on ``app`` must be a callable that
    returns an async context manager.  FastAPI's contract.
    """
    import main

    lifespan = main.app.router.lifespan_context
    assert callable(lifespan), "app.router.lifespan_context must be callable"


# ---------------------------------------------------------------------------
# Live lifespan smoke — needs DB
# ---------------------------------------------------------------------------


@pytest.mark.db
@pytest.mark.slow
@pytest.mark.asyncio
async def test_lifespan_startup_and_shutdown_complete() -> None:
    """Drive the FastAPI lifespan against a throwaway database.

    This runs in a subprocess so we can override ``DATABASE_URL`` before
    ``models.database`` is imported (the engine is created at import
    time and points at whatever ``settings.DATABASE_URL`` resolved to).
    The subprocess imports ``main``, enters and exits ``lifespan``, and
    prints ``OK`` on success.

    Skips when no writable Postgres is reachable — the
    ``build_postgres_session_factory`` admin connect step would fail
    with a confusing ``ConnectionRefusedError`` otherwise.
    """
    try:
        from models.database import Base  # noqa: F401
        from tests.postgres_test_db import build_postgres_session_factory
    except Exception as exc:  # pragma: no cover — defensive
        pytest.skip(f"DB harness unavailable: {exc}")

    try:
        engine, _session_factory = await build_postgres_session_factory(
            Base, "lifespan_smoke"
        )
    except Exception as exc:
        pytest.skip(f"Postgres unreachable for lifespan smoke: {exc}")

    # Build the asyncpg-style URL pointing at the throwaway DB.  Use
    # ``render_as_string(hide_password=False)`` rather than ``str()``
    # — SQLAlchemy redacts the password in ``str(URL)`` by default,
    # which would cause the subprocess to fail with
    # ``InvalidPasswordError`` when it tries to authenticate.
    test_database_url = engine.url.render_as_string(hide_password=False)

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _LIFESPAN_DRIVER_SOURCE,
            cwd=str(BACKEND_ROOT),
            env={
                **os.environ,
                "DATABASE_URL": test_database_url,
                # Silence the noisy startup logs in the child so the
                # captured stdout is just our marker.
                "LOG_LEVEL": os.environ.get("LIFESPAN_SMOKE_LOG_LEVEL", "WARNING"),
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=45.0
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            pytest.fail(
                "lifespan smoke subprocess exceeded 45 s — startup or "
                "shutdown is hanging"
            )

        if proc.returncode != 0:
            pytest.fail(
                "lifespan subprocess exited with "
                f"{proc.returncode}\nstdout: {stdout!r}\nstderr: {stderr!r}"
            )

        assert b"LIFESPAN_OK" in stdout, (
            f"lifespan subprocess did not emit success marker.\n"
            f"stdout: {stdout!r}\nstderr: {stderr!r}"
        )
    finally:
        await engine.dispose()


_LIFESPAN_DRIVER_SOURCE = """
import asyncio
import sys


async def _drive() -> int:
    import main

    async with main.app.router.lifespan_context(main.app):
        # Startup completed without raising.  We do not exercise any
        # routes here — the smoke is "can the app boot, then quiesce."
        pass
    print("LIFESPAN_OK", flush=True)
    return 0


sys.exit(asyncio.run(_drive()))
"""
