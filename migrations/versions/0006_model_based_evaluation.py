"""Track model-assisted assessments separately from pass/fail results."""

import sqlalchemy as sa
from alembic import op

revision = "0006_model_based_evaluation"
down_revision = "0005_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("evaluation_runs")}
    if "assessed" not in columns:
        op.add_column(
            "evaluation_runs",
            sa.Column("assessed", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("evaluation_runs")}
    if "assessed" in columns:
        op.drop_column("evaluation_runs", "assessed")
