"""add status column to employee

Revision ID: e0f9b7f1f6b2
Revises: 04716cbed9d6
Create Date: 2025-09-20 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e0f9b7f1f6b2'
down_revision = '04716cbed9d6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('employee', schema=None) as batch_op:
        batch_op.add_column(sa.Column('status', sa.String(length=20), nullable=True, server_default='active'))

    op.execute("UPDATE employee SET status = 'active' WHERE status IS NULL")


def downgrade():
    with op.batch_alter_table('employee', schema=None) as batch_op:
        batch_op.drop_column('status')
