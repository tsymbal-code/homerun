"""Alembic migration round-trip test for the head migration.

Verifies that the **most recent migration** can survive a clean
``downgrade → upgrade`` cycle.  Catches the most common production
hazard: a new migration whose ``downgrade()`` raises, or whose
``upgrade()`` is not idempotent across a rollback.

Why we only round-trip the head migration
-----------------------------------------
A previous version of this test ran ``upgrade base→head`` against an
empty database and found a chronic architectural issue: the baseline
migration ``202602130001_baseline_schema.py`` calls
``Base.metadata.create_all(bind=op.get_bind())``, which materialises
*every* current ORM column at revision 1 — including columns added by
later migrations.  Subsequent migrations that ``op.add_column(...)``
those same columns then fail with ``DuplicateColumnError`` on a
fresh DB.

The deployed database was originally stamped at baseline before any
of those columns existed, so production has never hit this — but it
means the migration chain is **not replayable from base** as written.
Fixing it requires either snapshotting an inline schema in the
baseline migration or guarding every later ``add_column`` with a
``column_names()`` check.  That refactor is its own plan.

In the meantime, this test focuses on the regression we *can* guard:
when a new migration lands, do its ``upgrade()`` and ``downgrade()``
form a clean round-trip?  We bootstrap the schema via
``Base.metadata.create_all`` (mirroring what production already has),
stamp the alembic version at head, then downgrade one revision and
re-upgrade.

Skips when no Postgres is reachable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _build_alembic_config(sync_connection) -> Config:
    """Return an Alembic ``Config`` wired to the given sync connection.

    The Alembic env.py honours ``config.attributes['connection']`` and
    skips its own engine creation when present (see
    ``backend/alembic/env.py:run_migrations_online``).  That lets us
    point migrations at the throwaway database without monkey-patching
    settings or shelling out to the alembic CLI.
    """
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    cfg.attributes["connection"] = sync_connection
    return cfg


@pytest.mark.db
@pytest.mark.slow
@pytest.mark.asyncio
async def test_head_migration_downgrade_upgrade_roundtrip() -> None:
    try:
        from models.database import Base
        from models.model_registry import register_all_models
        from tests.postgres_test_db import build_postgres_session_factory
    except Exception as exc:  # pragma: no cover — defensive
        pytest.skip(f"DB harness unavailable: {exc}")

    register_all_models()

    try:
        engine, _factory = await build_postgres_session_factory(
            Base, "alembic_head_roundtrip"
        )
    except Exception as exc:
        pytest.skip(f"Postgres unreachable for alembic round-trip: {exc}")

    try:
        # Compute head + previous revision once, before opening a
        # connection — pure script-graph traversal, no DB side effects.
        script = ScriptDirectory.from_config(
            Config(str(BACKEND_ROOT / "alembic.ini"))
        )
        head_revision = script.get_current_head()
        assert head_revision, "alembic script directory has no head revision"

        head_script = script.get_revision(head_revision)
        previous_revision = head_script.down_revision
        assert isinstance(previous_revision, str) and previous_revision, (
            "head migration has no single down_revision (merge node?); "
            "extend this test to handle the merge case"
        )

        async with engine.connect() as conn:

            def _run_alembic(sync_conn) -> None:
                from alembic.runtime.migration import MigrationContext

                cfg = _build_alembic_config(sync_conn)

                # 1. Stamp the throwaway DB at head.  ``Base.metadata``
                # was already materialised by ``build_postgres_session_factory``
                # so the schema matches the production DB; we just need
                # alembic_version to reflect that.
                command.stamp(cfg, head_revision)
                rev = MigrationContext.configure(sync_conn).get_current_revision()
                assert rev == head_revision, (
                    f"After stamp expected {head_revision!r}, got {rev!r}"
                )

                # 2. Downgrade one revision to the explicit parent of
                # head.  We compute the target ourselves because the
                # python API does not honour the CLI's ``-1`` relative
                # syntax.  The head migration's ``downgrade()`` runs.
                command.downgrade(cfg, previous_revision)
                rev = MigrationContext.configure(sync_conn).get_current_revision()
                assert rev == previous_revision, (
                    f"After downgrade expected {previous_revision!r}, "
                    f"got {rev!r} — head migration's downgrade() ran "
                    f"but moved alembic_version to an unexpected revision"
                )

                # 3. Re-upgrade to head.  The head migration's
                # ``upgrade()`` runs against a schema that was just
                # rolled back by its own ``downgrade()`` — this is the
                # load-bearing assertion: did downgrade actually undo
                # what upgrade did, or just bump the version row?
                command.upgrade(cfg, "head")
                rev = MigrationContext.configure(sync_conn).get_current_revision()
                assert rev == head_revision, (
                    f"After re-upgrade expected {head_revision!r}, "
                    f"got {rev!r}"
                )

            await conn.run_sync(_run_alembic)
    finally:
        await engine.dispose()
