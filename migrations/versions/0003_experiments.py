"""Add versioned experiment definitions and persisted comparisons."""

from alembic import op

from tracecore import models  # noqa: F401
from tracecore.database import Base

revision = "0003_experiments"
down_revision = "0002_evaluation"
branch_labels = None
depends_on = None

EXPERIMENT_TABLES = ("experiments", "experiment_runs", "experiment_results")


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(
        bind=bind, tables=[Base.metadata.tables[name] for name in EXPERIMENT_TABLES]
    )


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(
        bind=bind, tables=[Base.metadata.tables[name] for name in reversed(EXPERIMENT_TABLES)]
    )
