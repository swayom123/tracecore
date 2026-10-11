"""Add immutable datasets, deterministic evaluator definitions, and evaluation results."""

from alembic import op

from tracecore import models  # noqa: F401
from tracecore.database import Base

revision = "0002_evaluation"
down_revision = "0001_phase1"
branch_labels = None
depends_on = None

EVALUATION_TABLES = (
    "evaluation_datasets",
    "evaluation_cases",
    "evaluator_definitions",
    "evaluation_runs",
    "evaluation_results",
)


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(
        bind=bind, tables=[Base.metadata.tables[name] for name in EVALUATION_TABLES]
    )


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(
        bind=bind, tables=[Base.metadata.tables[name] for name in reversed(EVALUATION_TABLES)]
    )
