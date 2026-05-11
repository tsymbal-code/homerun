"""Add crypto_oracle_history table for offline crypto backtests.

Plan: 0046 — persistent Chainlink/Binance oracle price history so the
strategy backtester can resolve ``price_to_beat`` and cycle resolution
prices for any window in the retention horizon (~14 days). The live
``ChainlinkFeed`` keeps a 3h in-memory deque; this table extends the
window for backtest replay.

Revision ID: 202605110003
Revises: 202605110002
Create Date: 2026-05-11
"""

import sqlalchemy as sa
from alembic import op


revision = "202605110003"
down_revision = "202605110002"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return set(inspector.get_table_names())


def _index_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in set(inspector.get_table_names()):
        return set()
    return {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if "crypto_oracle_history" not in _table_names():
        op.create_table(
            "crypto_oracle_history",
            sa.Column("asset", sa.String(length=8), nullable=False),
            sa.Column("timestamp_ms", sa.BigInteger(), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("price", sa.Float(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.PrimaryKeyConstraint(
                "asset",
                "timestamp_ms",
                "source",
                name="pk_crypto_oracle_history",
            ),
        )

    existing_indexes = _index_names("crypto_oracle_history")
    if "idx_crypto_oracle_history_asset_ts_desc" not in existing_indexes:
        op.execute(
            sa.text(
                "CREATE INDEX IF NOT EXISTS "
                "idx_crypto_oracle_history_asset_ts_desc "
                "ON crypto_oracle_history (asset, timestamp_ms DESC)"
            )
        )


def downgrade() -> None:
    pass
