"""add payroll status fields and audit_log table

Revision ID: 3c2e4f68e4c9
Revises: e0f9b7f1f6b2
Create Date: 2025-09-20 01:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3c2e4f68e4c9'
down_revision = 'e0f9b7f1f6b2'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Bersihkan temp table yang mungkin tertinggal dari batch alter gagal (SQLite)
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_payroll")
    with op.batch_alter_table('payroll', schema=None) as batch_op:
        batch_op.add_column(sa.Column('status', sa.String(length=20), nullable=True, server_default='draft'))
        batch_op.add_column(sa.Column('approved_by', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('approved_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('created_at', sa.DateTime(), nullable=True))
        batch_op.create_foreign_key('fk_payroll_approved_by', 'user', ['approved_by'], ['id'])

    # set existing payrolls as approved
    op.execute("UPDATE payroll SET status='approved' WHERE status IS NULL")

    if 'audit_log' not in inspector.get_table_names():
        op.create_table(
            'audit_log',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
            sa.Column('action', sa.String(length=100), nullable=False),
            sa.Column('entity_type', sa.String(length=100), nullable=False),
            sa.Column('entity_id', sa.Integer(), nullable=False),
            sa.Column('details', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True, server_default=sa.text("CURRENT_TIMESTAMP"))
        )


def downgrade():
    op.drop_table('audit_log')

    with op.batch_alter_table('payroll', schema=None) as batch_op:
        batch_op.drop_constraint('fk_payroll_approved_by', type_='foreignkey')
        batch_op.drop_column('created_at')
        batch_op.drop_column('approved_at')
        batch_op.drop_column('approved_by')
        batch_op.drop_column('status')
