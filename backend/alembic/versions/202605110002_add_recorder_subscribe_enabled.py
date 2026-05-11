"""Add recorder_subscribe_enabled column to app_settings.

Plan: 0045 — replace the ``HOMERUN_RECORDER_ENABLED`` env var (from
commit ``bd5aa8a3``) with a DB-backed toggle so operators can flip
the recorder subscriber on/off from the Settings UI without
touching docker-compose env.

Revision ID: 202605110002
Revises: 202605110001
Create Date: 2026-05-11
"""

import sqlalchemy as sa
from alembic import op


revision = "202605110002"
down_revision = "202605110001"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {c["name"] for c in inspector.get_columns(table_name)}


def upgrade() -> None:
    existing = _column_names("app_settings")
    if "recorder_subscribe_enabled" not in existing:
        op.add_column(
            "app_settings",
            sa.Column(
                "recorder_subscribe_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    existing = _column_names("app_settings")
    if "recorder_subscribe_enabled" in existing:
        op.drop_column("app_settings", "recorder_subscribe_enabled")
