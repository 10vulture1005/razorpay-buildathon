"""r0 eval integrity: cases.will_self_cure + self_cure_day_offset

Revision ID: d4e2b9a06f11
Revises: c3d1a8f42b77
Create Date: 2026-08-24

Self-cure behavior moves from the eval runner (run_full_batch.py pre-inserted
payments) to the synthetic case itself, so experiment arms / runs see the same
underlying customer behavior and recovery metrics aren't pre-baked.
"""
from alembic import op
import sqlalchemy as sa

revision = "d4e2b9a06f11"
down_revision = "c3d1a8f42b77"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("cases") as batch:
        batch.add_column(sa.Column("will_self_cure", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))
        batch.add_column(sa.Column("self_cure_day_offset", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("cases") as batch:
        batch.drop_column("self_cure_day_offset")
        batch.drop_column("will_self_cure")
