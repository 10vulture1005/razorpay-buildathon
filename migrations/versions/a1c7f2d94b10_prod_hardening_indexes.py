"""Production hardening: hot-query indexes + nullable webhook ledger column.

Revision ID: a1c7f2d94b10
Revises: b84090e665c2
Create Date: 2026-08-23

- audit_log(case_id), audit_log(event_type): case detail / feed queries
- cases(status): metrics scans filter on status constantly
- tool_executions(case_id): per-case idempotency lookups
- tool_executions.case_id becomes nullable: payment-gateway webhooks record
  ledger rows for unmatched invoices (replay dedupe without a FK target).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c7f2d94b10"
down_revision: str | None = "b84090e665c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_audit_log_case_id", "audit_log", ["case_id"])
    op.create_index("ix_audit_log_event_type", "audit_log", ["event_type"])
    op.create_index("ix_cases_status", "cases", ["status"])
    op.create_index("ix_tool_executions_case_id", "tool_executions", ["case_id"])
    op.alter_column("tool_executions", "case_id", existing_type=sa.String(64), nullable=True)


def downgrade() -> None:
    op.alter_column("tool_executions", "case_id", existing_type=sa.String(64), nullable=False)
    op.drop_index("ix_tool_executions_case_id", table_name="tool_executions")
    op.drop_index("ix_cases_status", table_name="cases")
    op.drop_index("ix_audit_log_event_type", table_name="audit_log")
    op.drop_index("ix_audit_log_case_id", table_name="audit_log")
