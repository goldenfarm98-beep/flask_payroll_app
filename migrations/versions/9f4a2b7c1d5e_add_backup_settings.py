"""add backup settings table

Revision ID: 9f4a2b7c1d5e
Revises: 8d6d3e2c5c1b
Create Date: 2025-12-31 23:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9f4a2b7c1d5e'
down_revision = '8d6d3e2c5c1b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'backup_settings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('interval_hours', sa.Integer(), nullable=False, server_default=sa.text('24')),
        sa.Column('retention_count', sa.Integer(), nullable=False, server_default=sa.text('7')),
        sa.Column('next_run_at', sa.DateTime(), nullable=True),
        sa.Column('last_run_at', sa.DateTime(), nullable=True),
        sa.Column('last_status', sa.String(length=20), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('last_backup_file', sa.String(length=255), nullable=True),
    )


def downgrade():
    op.drop_table('backup_settings')
