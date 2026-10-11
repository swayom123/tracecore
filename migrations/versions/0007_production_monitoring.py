"""Persist budgets, alert rules, occurrences and delivery attempts."""

from alembic import op

from tracecore import models  # noqa: F401
from tracecore.database import Base

revision = "0007_production_monitoring"
down_revision = "0006_model_based_evaluation"
branch_labels = None
depends_on = None

TABLES = (
    "monitoring_budgets",
    "monitor_alert_rules",
    "alert_occurrences",
    "alert_delivery_attempts",
)


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables[name] for name in TABLES])


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(
        bind=bind, tables=[Base.metadata.tables[name] for name in reversed(TABLES)]
    )
