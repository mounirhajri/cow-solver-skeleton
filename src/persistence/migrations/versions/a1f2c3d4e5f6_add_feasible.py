"""add feasible + revert_reason to shadow_solutions

Revision ID: a1f2c3d4e5f6
Revises: 7c4ad9e3b821
Create Date: 2026-06-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1f2c3d4e5f6"
down_revision = "7c4ad9e3b821"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "shadow_solutions",
        sa.Column("feasible", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "shadow_solutions",
        sa.Column("revert_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("shadow_solutions", "revert_reason")
    op.drop_column("shadow_solutions", "feasible")
