"""add reject reason to payroll

Revision ID: e7f8a9b0c1d2
Revises: d4e5f6a7b8c9
Create Date: 2026-01-02 19:45:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e7f8a9b0c1d2'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('payroll', sa.Column('reject_reason', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('payroll', 'reject_reason')
