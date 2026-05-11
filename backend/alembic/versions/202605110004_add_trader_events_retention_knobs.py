"""Add trader_events retention knobs to app_settings.

Plan: 0049 — DB-backed two-tier retention horizon for the
``trader_events`` audit table. ``firehose_evaluation`` rows
dominate (~99% of volume after Plan 0044 enabled cross-mode
firehose telemetry) and need a tight 7-day window; everything
else is a low-volume diagnostic/regulatory trail kept for 90 days.
The actual deletion runs in
``services.trader_events_retention_service`` on a 6h cadence.

Revision ID: 202605110004
Revises: 202605110003
Create Date: 2026-05-11
"""

import sqlalchemy as sa
from alembic import op


revision = "202605110004"
down_revision = "202605110003"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in set(inspector.get_table_names()):
        return set()
    return {col["name"] for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    existing = _column_names("app_settings")
    columns = [
        sa.Column(
            "trader_events_firehose_retention_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("7"),
        ),
        sa.Column(
            "trader_events_other_retention_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("90"),
        ),
    ]
    for col in columns:
        if col.name not in existing:
            op.add_column("app_settings", col)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {col["name"] for col in inspector.get_columns("app_settings")}
    for col_name in (
        "trader_events_other_retention_days",
        "trader_events_firehose_retention_days",
    ):
        if col_name in existing:
            op.drop_column("app_settings", col_name)
