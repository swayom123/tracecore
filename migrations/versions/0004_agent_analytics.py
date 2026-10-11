"""Persist derived agent analytics and evidence."""

from alembic import op

from tracecore import models  # noqa: F401
from tracecore.database import Base

revision = "0004_agent_analytics"
down_revision = "0003_experiments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables["agent_analyses"]])


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind, tables=[Base.metadata.tables["agent_analyses"]])
