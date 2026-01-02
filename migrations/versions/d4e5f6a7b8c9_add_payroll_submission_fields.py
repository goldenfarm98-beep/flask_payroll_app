"""add payroll submission fields

Revision ID: d4e5f6a7b8c9
Revises: c2d3e4f5a6b7
Create Date: 2026-01-02 19:05:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4e5f6a7b8c9'
down_revision = 'c2d3e4f5a6b7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('payroll', sa.Column('submitted_by', sa.Integer(), nullable=True))
    op.add_column('payroll', sa.Column('submitted_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('payroll', 'submitted_at')
    op.drop_column('payroll', 'submitted_by')
