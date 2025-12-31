"""add payment_id to PayrollLoan

Revision ID: 73ea04d0f1ad
Revises: 4a7f62f672c0
Create Date: 2025-07-30 21:50:16.763646

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers …
revision = "73ea04d0f1ad"
down_revision = "4a7f62f672c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("payroll_loan", schema=None) as batch_op:
        batch_op.add_column(sa.Column("payment_id", sa.Integer(), nullable=True))

        # ===== tambahkan NAMA constraint =====
        batch_op.create_foreign_key(
            "fk_payrollloan_payment_id",   # ← beri nama
            "payment",                     # tabel referensi
            ["payment_id"], ["id"]
        )

    # (opsional) isi kolom payment_id lama → NULL → jadikan NOT NULL
    # op.execute("UPDATE payroll_loan SET payment_id = 0 WHERE payment_id IS NULL")
    # with op.batch_alter_table("payroll_loan") as batch_op:
    #     batch_op.alter_column("payment_id", nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("payroll_loan", schema=None) as batch_op:
        batch_op.drop_constraint("fk_payrollloan_payment_id", type_="foreignkey")
        batch_op.drop_column("payment_id")

