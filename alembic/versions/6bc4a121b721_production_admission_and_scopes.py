"""Atomic admission, scoped keys and organization-local project names.

Revision ID: 6bc4a121b721
Revises: 1a75f2dabe4b
"""

import sqlalchemy as sa
from alembic import op

revision = "6bc4a121b721"
down_revision = "1a75f2dabe4b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("usage_reservations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(100), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    for column in ("request_id", "project_id", "created_at"):
        op.create_index(f"ix_usage_reservations_{column}", "usage_reservations", [column])
    op.add_column("project_api_keys", sa.Column("scopes_json", sa.Text(), nullable=False, server_default='["chat","analytics:read"]'))
    op.alter_column("project_api_keys", "scopes_json", server_default=None)
    op.add_column("llm_request_logs", sa.Column("tokens_avoided", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("llm_request_logs", sa.Column("cost_avoided_usd", sa.Float(), nullable=False, server_default="0"))
    op.alter_column("llm_request_logs", "tokens_avoided", server_default=None)
    op.alter_column("llm_request_logs", "cost_avoided_usd", server_default=None)
    op.drop_index("ix_projects_name", table_name="projects")
    op.create_index("ix_projects_name", "projects", ["name"], unique=False)
    op.create_unique_constraint("uq_project_org_name", "projects", ["organization_id", "name"])
    op.create_index("uq_project_legacy_name", "projects", ["name"], unique=True, postgresql_where=sa.text("organization_id IS NULL"))


def downgrade() -> None:
    # Refuse an unsafe downgrade if organizations now share a project name.
    duplicate = op.get_bind().execute(sa.text("SELECT name FROM projects GROUP BY name HAVING COUNT(*) > 1 LIMIT 1")).first()
    if duplicate:
        raise RuntimeError("Rename duplicate project names before downgrading.")
    op.drop_index("uq_project_legacy_name", table_name="projects")
    op.drop_constraint("uq_project_org_name", "projects", type_="unique")
    op.drop_index("ix_projects_name", table_name="projects")
    op.create_index("ix_projects_name", "projects", ["name"], unique=True)
    op.drop_column("llm_request_logs", "cost_avoided_usd")
    op.drop_column("llm_request_logs", "tokens_avoided")
    op.drop_column("project_api_keys", "scopes_json")
    op.drop_table("usage_reservations")
