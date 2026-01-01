"""add compensation master tables

Revision ID: 8d6d3e2c5c1b
Revises: 3c2e4f68e4c9
Create Date: 2025-09-20 03:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '8d6d3e2c5c1b'
down_revision = '3c2e4f68e4c9'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'compensation_component' not in inspector.get_table_names():
        op.create_table(
            'compensation_component',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('code', sa.String(length=50), nullable=False, unique=True),
            sa.Column('name', sa.String(length=120), nullable=False),
            sa.Column('comp_type', sa.String(length=20), nullable=False),
            sa.Column('calc_type', sa.String(length=20), nullable=True),
            sa.Column('default_value', sa.Float(), nullable=True),
            sa.Column('active', sa.Boolean(), nullable=True, server_default=sa.text("true")),
            sa.Column('created_at', sa.DateTime(), nullable=True, server_default=sa.text("CURRENT_TIMESTAMP"))
        )

    if 'employee_compensation' not in inspector.get_table_names():
        op.create_table(
            'employee_compensation',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('employee_id', sa.Integer(), sa.ForeignKey('employee.id'), nullable=False),
            sa.Column('component_id', sa.Integer(), sa.ForeignKey('compensation_component.id'), nullable=False),
            sa.Column('value', sa.Float(), nullable=True),
            sa.Column('start_period', sa.String(length=7), nullable=True),
            sa.Column('active', sa.Boolean(), nullable=True, server_default=sa.text("true"))
        )


def downgrade():
    op.drop_table('employee_compensation')
    op.drop_table('compensation_component')
