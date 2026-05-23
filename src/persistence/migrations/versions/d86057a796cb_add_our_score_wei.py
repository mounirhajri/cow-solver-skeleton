"""add our_score_wei to shadow_solutions

Revision ID: d86057a796cb
Revises: f023af7ceed8
Create Date: 2026-05-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d86057a796cb"
down_revision = "f023af7ceed8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "shadow_solutions",
        sa.Column("our_score_wei", sa.Numeric(precision=40, scale=0), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("shadow_solutions", "our_score_wei")
