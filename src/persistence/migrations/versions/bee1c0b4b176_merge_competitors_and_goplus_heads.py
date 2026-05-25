"""merge competitors and goplus heads

Revision ID: bee1c0b4b176
Revises: 3b9f1c2e0a8e, 62c1205d4b78
Create Date: 2026-05-25 18:32:13.478173

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bee1c0b4b176'
down_revision: Union[str, Sequence[str], None] = ('3b9f1c2e0a8e', '62c1205d4b78')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
