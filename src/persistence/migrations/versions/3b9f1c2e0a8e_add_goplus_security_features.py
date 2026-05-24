"""add GoPlus security feature columns to token_features

Adds 13 columns to `token_features` capturing GoPlus Security signals
extracted alongside the legit/scam verdict in scripts/auto_seed_labels.py.

These give the cold-start IsolationForest meaningful per-token-security
signal that the on-chain-only original features (decimals, TVL, holders,
age) cannot express on their own.

All columns are nullable so existing rows remain valid; auto_seed_labels
will populate them on its next pass.

Revision ID: 3b9f1c2e0a8e
Revises: 0a7be982cc8a
Create Date: 2026-05-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "3b9f1c2e0a8e"
down_revision = "0a7be982cc8a"
branch_labels = None
depends_on = None


_BOOL_COLUMNS = (
    "is_proxy",
    "is_mintable",
    "can_take_back_ownership",
    "hidden_owner",
    "slippage_modifiable",
    "transfer_pausable",
    "owner_change_balance",
    "external_call",
    "honeypot_with_same_creator",
    "anti_whale_modifiable",
)

# Fractional 0..1 (taxes can theoretically be >1.0 for absurd taxes; keep
# numeric so future extreme values don't overflow a smaller type).
_FRACTION_COLUMNS = (
    "creator_percent",
    "buy_tax",
    "sell_tax",
)


def upgrade() -> None:
    for col in _BOOL_COLUMNS:
        op.add_column(
            "token_features",
            sa.Column(col, sa.Boolean(), nullable=True),
        )
    for col in _FRACTION_COLUMNS:
        op.add_column(
            "token_features",
            sa.Column(col, sa.Numeric(precision=10, scale=6), nullable=True),
        )


def downgrade() -> None:
    for col in _FRACTION_COLUMNS:
        op.drop_column("token_features", col)
    for col in _BOOL_COLUMNS:
        op.drop_column("token_features", col)
