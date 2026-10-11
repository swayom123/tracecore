"""Add revocable database credentials and shared rate-limit buckets."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0009_operational_hardening"
down_revision: Union[str, None] = "0008_audit_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_credentials",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("role", sa.String(40), nullable=False),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("project_ids", sa.JSON(), nullable=False),
        sa.Column("environments", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_credentials_token_hash", "api_credentials", ["token_hash"], unique=True)
    op.create_index("ix_api_credentials_organization_id", "api_credentials", ["organization_id"])
    op.create_table(
        "rate_limit_buckets",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("window_start", sa.Integer(), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False),
    )
    op.create_index("ix_rate_limit_buckets_window", "rate_limit_buckets", ["window_start"])


def downgrade() -> None:
    op.drop_index("ix_rate_limit_buckets_window", table_name="rate_limit_buckets")
    op.drop_table("rate_limit_buckets")
    op.drop_index("ix_api_credentials_organization_id", table_name="api_credentials")
    op.drop_index("ix_api_credentials_token_hash", table_name="api_credentials")
    op.drop_table("api_credentials")
