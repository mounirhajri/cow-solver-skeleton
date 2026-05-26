"""add ghost_orders

Revision ID: 7c4ad9e3b821
Revises: bee1c0b4b176
Create Date: 2026-05-26

Tracks order UIDs identified as ghost-orders: seen in many auctions but never
settled by any live solver. Populated by scripts/refresh_ghost_set.py as a
periodic refresh job and read by edge.matching.bipartite via the
DynamicGhostDetector to filter pre-pair-matching.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "7c4ad9e3b821"
down_revision = "bee1c0b4b176"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ghost_orders",
        sa.Column("uid", sa.String(length=114), nullable=False),
        sa.Column("owner", sa.String(length=42), nullable=False),
        sa.Column("sell_token", sa.String(length=42), nullable=False),
        sa.Column("buy_token", sa.String(length=42), nullable=False),
        sa.Column("n_auctions_seen", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("uid"),
    )
    op.create_index(
        op.f("ix_ghost_orders_owner"),
        "ghost_orders",
        ["owner"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ghost_orders_last_refreshed_at"),
        "ghost_orders",
        ["last_refreshed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_ghost_orders_last_refreshed_at"), table_name="ghost_orders"
    )
    op.drop_index(op.f("ix_ghost_orders_owner"), table_name="ghost_orders")
    op.drop_table("ghost_orders")
