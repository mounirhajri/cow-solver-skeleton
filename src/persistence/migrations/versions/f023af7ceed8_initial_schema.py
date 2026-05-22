"""initial schema

Revision ID: f023af7ceed8
Revises: 
Create Date: 2026-05-23 00:38:41.634617

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f023af7ceed8'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "shadow_auctions",
        sa.Column("auction_id", sa.BigInteger(), primary_key=True),
        sa.Column("chain", sa.String(20), nullable=False, server_default="arbitrum_one"),
        sa.Column("polled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True)),
        sa.Column("n_orders", sa.Integer(), nullable=False),
        sa.Column("raw_competition", sa.JSON(), nullable=False),
        sa.Column("raw_auction", sa.JSON(), nullable=False),
    )
    op.create_index("ix_shadow_auctions_polled_at", "shadow_auctions", ["polled_at"])

    op.create_table(
        "shadow_solutions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "auction_id",
            sa.BigInteger(),
            sa.ForeignKey("shadow_auctions.auction_id"),
            nullable=False,
        ),
        sa.Column("strategy", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("solution", sa.JSON()),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_shadow_solutions_auction", "shadow_solutions", ["auction_id"])

    op.create_table(
        "shadow_winners",
        sa.Column(
            "auction_id",
            sa.BigInteger(),
            sa.ForeignKey("shadow_auctions.auction_id"),
            primary_key=True,
        ),
        sa.Column("winner_solver", sa.Text(), nullable=False),
        sa.Column("score", sa.Numeric(40, 0)),
        sa.Column("raw_solution", sa.JSON(), nullable=False),
    )

    op.create_table(
        "token_outcomes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("token_address", sa.String(42), nullable=False),
        sa.Column(
            "auction_id",
            sa.BigInteger(),
            sa.ForeignKey("shadow_auctions.auction_id"),
            nullable=False,
        ),
        sa.Column("appeared_in_winner", sa.Boolean(), nullable=False),
        sa.Column("appeared_in_ours", sa.Boolean(), nullable=False),
        sa.Column(
            "caused_revert",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_token_outcomes_token", "token_outcomes", ["token_address"])
    op.create_index("ix_token_outcomes_observed", "token_outcomes", ["observed_at"])

    op.create_table(
        "token_features",
        sa.Column("token_address", sa.String(42), primary_key=True),
        sa.Column("decimals", sa.Integer()),
        sa.Column("contract_verified", sa.Boolean()),
        sa.Column("has_transfer_tax", sa.Boolean()),
        sa.Column("bridge_canonical", sa.Boolean()),
        sa.Column("tvl_usd", sa.Numeric(20, 2)),
        sa.Column("volume_24h_usd", sa.Numeric(20, 2)),
        sa.Column("pool_count_v2", sa.Integer()),
        sa.Column("pool_count_v3", sa.Integer()),
        sa.Column("pool_count_camelot", sa.Integer()),
        sa.Column("holder_count", sa.Integer()),
        sa.Column("top10_concentration", sa.Numeric(5, 4)),
        sa.Column("age_blocks", sa.Integer()),
        sa.Column("on_arbitrum_token_list", sa.Boolean()),
        sa.Column("on_coingecko", sa.Boolean()),
        sa.Column(
            "last_refreshed",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("token_features")
    op.drop_table("token_outcomes")
    op.drop_table("shadow_winners")
    op.drop_table("shadow_solutions")
    op.drop_table("shadow_auctions")
