"""add shadow_competitors

Revision ID: 62c1205d4b78
Revises: 0a7be982cc8a
Create Date: 2026-05-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "62c1205d4b78"
down_revision = "0a7be982cc8a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shadow_competitors",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("auction_id", sa.BigInteger(), nullable=False),
        sa.Column("solver_name", sa.String(length=50), nullable=False),
        sa.Column("solver_address", sa.String(length=42), nullable=False),
        sa.Column("score", sa.Numeric(precision=40, scale=0), nullable=True),
        sa.Column("ranking", sa.Integer(), nullable=False),
        sa.Column("is_winner", sa.Boolean(), nullable=False),
        sa.Column("filtered_out", sa.Boolean(), nullable=False),
        sa.Column("clearing_prices", sa.JSON(), nullable=False),
        sa.Column("orders", sa.JSON(), nullable=False),
        sa.Column("polled_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["auction_id"],
            ["shadow_auctions.auction_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "auction_id",
            "solver_address",
            name="uq_shadow_competitors_auction_solver",
        ),
    )
    op.create_index(
        op.f("ix_shadow_competitors_auction_id"),
        "shadow_competitors",
        ["auction_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_shadow_competitors_solver_name"),
        "shadow_competitors",
        ["solver_name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_shadow_competitors_solver_name"), table_name="shadow_competitors"
    )
    op.drop_index(
        op.f("ix_shadow_competitors_auction_id"), table_name="shadow_competitors"
    )
    op.drop_table("shadow_competitors")
