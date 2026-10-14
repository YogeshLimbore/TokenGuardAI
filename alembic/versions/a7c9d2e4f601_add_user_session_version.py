"""Add deterministic access-token session invalidation.

Revision ID: a7c9d2e4f601
Revises: 6bc4a121b721
"""

import sqlalchemy as sa
from alembic import op

revision = "a7c9d2e4f601"
down_revision = "6bc4a121b721"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "session_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.alter_column("users", "session_version", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "session_version")
