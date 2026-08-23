"""production integrations: rename payment ledgers, add customers.email

Revision ID: c3d1a8f42b77
Revises: a1c7f2d94b10
Create Date: 2026-08-24

- mock_payments -> payment_events (+ gateway_payment_id)
- mock_retry_results -> retry_events (+ gateway_payment_id)
- customers.email (nullable) for real outbound delivery
"""
from alembic import op
import sqlalchemy as sa

revision = "c3d1a8f42b77"
down_revision = "a1c7f2d94b10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("email", sa.String(length=255), nullable=True))
    with op.batch_alter_table("mock_payments") as batch:
        batch.add_column(sa.Column("gateway_payment_id", sa.String(length=128), nullable=True))
    with op.batch_alter_table("mock_retry_results") as batch:
        batch.add_column(sa.Column("gateway_payment_id", sa.String(length=128), nullable=True))
    op.create_index("ix_payment_events_gateway_payment_id", "mock_payments", ["gateway_payment_id"])
    op.rename_table("mock_payments", "payment_events")
    op.rename_table("mock_retry_results", "retry_events")


def downgrade() -> None:
    op.rename_table("retry_events", "mock_retry_results")
    op.rename_table("payment_events", "mock_payments")
    op.drop_index("ix_payment_events_gateway_payment_id", table_name="mock_payments")
    with op.batch_alter_table("mock_retry_results") as batch:
        batch.drop_column("gateway_payment_id")
    with op.batch_alter_table("mock_payments") as batch:
        batch.drop_column("gateway_payment_id")
    op.drop_column("customers", "email")
