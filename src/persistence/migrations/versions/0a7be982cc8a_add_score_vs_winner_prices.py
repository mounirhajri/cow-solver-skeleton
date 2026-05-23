"""add score_vs_winner_prices_wei to shadow_solutions

Revision ID: 0a7be982cc8a
Revises: d86057a796cb
Create Date: 2026-05-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0a7be982cc8a"
down_revision = "d86057a796cb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "shadow_solutions",
        sa.Column("score_vs_winner_prices_wei", sa.Numeric(precision=40, scale=0), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("shadow_solutions", "score_vs_winner_prices_wei")
