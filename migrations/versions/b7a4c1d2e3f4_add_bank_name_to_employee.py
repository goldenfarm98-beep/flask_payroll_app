"""add bank_name to employee

Revision ID: b7a4c1d2e3f4
Revises: 9f4a2b7c1d5e
Create Date: 2026-01-02 17:15:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7a4c1d2e3f4'
down_revision = '9f4a2b7c1d5e'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('employee', sa.Column('bank_name', sa.String(length=100), nullable=True))


def downgrade():
    op.drop_column('employee', 'bank_name')
