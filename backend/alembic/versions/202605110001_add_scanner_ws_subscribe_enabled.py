"""Add scanner_ws_subscribe_enabled column to app_settings.

Plan: 0045 — root-cause fix for the ``book_depth`` strategy gate.
The shared Polymarket WS ``_subscribed_assets`` set was growing past
the per-connection cap and silently dropping the crypto lane's
freshest book streams. Operators running crypto-only setups don't
need the scanner's WS overlay; expose a toggle so they can switch
it off without code changes.

Revision ID: 202605110001
Revises: 202605070002
Create Date: 2026-05-11
"""

import sqlalchemy as sa
from alembic import op


revision = "202605110001"
down_revision = "202605070002"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {c["name"] for c in inspector.get_columns(table_name)}


def upgrade() -> None:
    existing = _column_names("app_settings")
    if "scanner_ws_subscribe_enabled" not in existing:
        op.add_column(
            "app_settings",
            sa.Column(
                "scanner_ws_subscribe_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    existing = _column_names("app_settings")
    if "scanner_ws_subscribe_enabled" in existing:
        op.drop_column("app_settings", "scanner_ws_subscribe_enabled")
