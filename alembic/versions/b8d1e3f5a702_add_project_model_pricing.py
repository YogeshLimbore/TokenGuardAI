"""Add project-level model pricing.

Revision ID: b8d1e3f5a702
Revises: a7c9d2e4f601
"""

import sqlalchemy as sa
from alembic import op

revision = "b8d1e3f5a702"
down_revision = "a7c9d2e4f601"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_model_prices",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("input_usd_per_1m_tokens", sa.Float(), nullable=False),
        sa.Column("output_usd_per_1m_tokens", sa.Float(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "project_id",
            "provider",
            "model",
            name="uq_project_provider_model_price",
        ),
    )
    op.create_index(
        "ix_project_model_prices_project_id",
        "project_model_prices",
        ["project_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_model_prices_project_id", table_name="project_model_prices"
    )
    op.drop_table("project_model_prices")
