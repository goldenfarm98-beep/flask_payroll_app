"""add pph21 and bpjs_kesehatan to payroll

Revision ID: c2d3e4f5a6b7
Revises: b7a4c1d2e3f4
Create Date: 2026-01-02 18:10:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c2d3e4f5a6b7'
down_revision = 'b7a4c1d2e3f4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('payroll', sa.Column('bpjs_kesehatan', sa.Float(), nullable=False, server_default=sa.text('0')))
    op.add_column('payroll', sa.Column('pph21', sa.Float(), nullable=False, server_default=sa.text('0')))


def downgrade():
    op.drop_column('payroll', 'pph21')
    op.drop_column('payroll', 'bpjs_kesehatan')
