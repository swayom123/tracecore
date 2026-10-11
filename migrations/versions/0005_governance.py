"""Add versioned governance policies, evaluations, violations, and history."""

from alembic import op

from tracecore import models  # noqa: F401
from tracecore.database import Base

revision = "0005_governance"
down_revision = "0004_agent_analytics"
branch_labels = None
depends_on = None

GOVERNANCE_TABLES = (
    "governance_policies",
    "policy_evaluations",
    "policy_violations",
    "policy_transitions",
)


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(
        bind=bind, tables=[Base.metadata.tables[name] for name in GOVERNANCE_TABLES]
    )


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(
        bind=bind, tables=[Base.metadata.tables[name] for name in reversed(GOVERNANCE_TABLES)]
    )
