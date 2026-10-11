"""Create Phase 1 TraceCore tables."""

from alembic import op

from tracecore import models  # noqa: F401
from tracecore.database import Base

revision = "0001_phase1"
down_revision = None
branch_labels = None
depends_on = None

PHASE1_TABLES = (
    "projects",
    "applications",
    "environments",
    "agent_versions",
    "runs",
    "traces",
    "spans",
    "trace_events",
)


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(
        bind=bind, tables=[Base.metadata.tables[name] for name in PHASE1_TABLES]
    )


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(
        bind=bind, tables=[Base.metadata.tables[name] for name in reversed(PHASE1_TABLES)]
    )
