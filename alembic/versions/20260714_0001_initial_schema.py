"""initial database schema

Revision ID: 20260714_0001
Revises:
Create Date: 2026-07-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260714_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# PostgreSQL ENUM-типы создаём явно один раз. create_type=False запрещает
# op.create_table() пытаться выполнить повторный CREATE TYPE для той же схемы.
role_code = postgresql.ENUM(
    "BOSS", "MANAGER", "TEAMLEAD", "BUYER", "ADMIN",
    name="role_code",
    create_type=False,
)
report_status = postgresql.ENUM(
    "SUBMITTED", "MISSING", "LATE", "REPLACED",
    name="report_status",
    create_type=False,
)
sync_status = postgresql.ENUM(
    "RUNNING", "SUCCESS", "PARTIAL", "FAILED",
    name="sync_status",
    create_type=False,
)
sync_kind = postgresql.ENUM(
    "DAILY_REPORTS", "STATISTICS", "BUYERS", "MANUAL",
    name="sync_kind",
    create_type=False,
)
snapshot_kind = postgresql.ENUM(
    "TUESDAY", "THURSDAY", "SATURDAY", "MONTH_END", "MANUAL",
    name="snapshot_kind",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    role_code.create(bind, checkfirst=True)
    report_status.create(bind, checkfirst=True)
    sync_status.create(bind, checkfirst=True)
    sync_kind.create(bind, checkfirst=True)
    snapshot_kind.create(bind, checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=128), nullable=True),
        sa.Column("last_name", sa.String(length=128), nullable=True),
        sa.Column("display_name", sa.String(length=180), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("telegram_id", name="uq_users_telegram_id"),
    )
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"], unique=False)

    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("role", role_code, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_user_roles_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "role", name="pk_user_roles"),
    )

    op.create_table(
        "buyer_profiles",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("source_name", sa.String(length=180), nullable=False),
        sa.Column("external_code", sa.String(length=64), nullable=True),
        sa.Column("supervisor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("source_sheet_row", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "supervisor_user_id IS NULL OR supervisor_user_id <> user_id",
            name="supervisor_not_self",
        ),
        sa.ForeignKeyConstraint(["supervisor_user_id"], ["users.id"], name="fk_buyer_profiles_supervisor_user_id_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_buyer_profiles_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_buyer_profiles"),
        sa.UniqueConstraint("source_name", name="uq_buyer_profiles_source_name"),
        sa.UniqueConstraint("user_id", name="uq_buyer_profiles_user_id"),
    )
    op.create_index("ix_buyer_profiles_supervisor_user_id", "buyer_profiles", ["supervisor_user_id"], unique=False)

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sync_kind, nullable=False),
        sa.Column(
            "status",
            sync_status,
            server_default=sa.text("'RUNNING'::sync_status"),
            nullable=False,
        ),
        sa.Column("source_name", sa.String(length=255), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rows_received", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rows_saved", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rows_skipped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_sync_runs"),
    )
    op.create_index("ix_sync_runs_kind", "sync_runs", ["kind"], unique=False)
    op.create_index("ix_sync_runs_status", "sync_runs", ["status"], unique=False)

    op.create_table(
        "daily_reports",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("buyer_id", sa.BigInteger(), nullable=False),
        sa.Column("sync_run_id", sa.BigInteger(), nullable=True),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            report_status,
            server_default=sa.text("'SUBMITTED'::report_status"),
            nullable=False,
        ),
        sa.Column("launched_accounts", sa.Integer(), nullable=True),
        sa.Column("geo_raw", sa.Text(), nullable=True),
        sa.Column("geo_normalized", sa.Text(), nullable=True),
        sa.Column("issues", sa.Text(), nullable=True),
        sa.Column("report_text", sa.Text(), nullable=True),
        sa.Column("source_row", sa.Integer(), nullable=True),
        sa.Column("source_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["buyer_id"], ["buyer_profiles.id"], name="fk_daily_reports_buyer_id_buyer_profiles", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sync_run_id"], ["sync_runs.id"], name="fk_daily_reports_sync_run_id_sync_runs", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_daily_reports"),
        sa.UniqueConstraint("buyer_id", "report_date", name="uq_daily_reports_buyer_date"),
    )
    op.create_index("ix_daily_reports_buyer_id", "daily_reports", ["buyer_id"], unique=False)
    op.create_index("ix_daily_reports_report_date", "daily_reports", ["report_date"], unique=False)
    op.create_index("ix_daily_reports_report_date_status", "daily_reports", ["report_date", "status"], unique=False)
    op.create_index("ix_daily_reports_sync_run_id", "daily_reports", ["sync_run_id"], unique=False)

    op.create_table(
        "statistics_snapshots",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("buyer_id", sa.BigInteger(), nullable=False),
        sa.Column("sync_run_id", sa.BigInteger(), nullable=True),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("snapshot_kind", snapshot_kind, nullable=False),
        sa.Column("source_period", sa.String(length=7), nullable=False),
        sa.Column("revenue_total", sa.Numeric(18, 2), nullable=False),
        sa.Column("spend_total", sa.Numeric(18, 2), nullable=False),
        sa.Column("profit_total", sa.Numeric(18, 2), nullable=False),
        sa.Column("roi_total", sa.Numeric(9, 4), nullable=True),
        sa.Column("source_row", sa.Integer(), nullable=True),
        sa.Column("source_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("revenue_total >= 0", name="revenue_non_negative"),
        sa.CheckConstraint("spend_total >= 0", name="spend_non_negative"),
        sa.ForeignKeyConstraint(["buyer_id"], ["buyer_profiles.id"], name="fk_statistics_snapshots_buyer_id_buyer_profiles", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sync_run_id"], ["sync_runs.id"], name="fk_statistics_snapshots_sync_run_id_sync_runs", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_statistics_snapshots"),
        sa.UniqueConstraint("buyer_id", "snapshot_at", name="uq_statistics_snapshots_buyer_snapshot_at"),
    )
    op.create_index("ix_statistics_snapshots_buyer_business_date", "statistics_snapshots", ["buyer_id", "business_date"], unique=False)
    op.create_index("ix_statistics_snapshots_buyer_id", "statistics_snapshots", ["buyer_id"], unique=False)
    op.create_index("ix_statistics_snapshots_business_date", "statistics_snapshots", ["business_date"], unique=False)
    op.create_index("ix_statistics_snapshots_snapshot_at", "statistics_snapshots", ["snapshot_at"], unique=False)
    op.create_index("ix_statistics_snapshots_sync_run_id", "statistics_snapshots", ["sync_run_id"], unique=False)

    op.create_table(
        "sync_errors",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("sync_run_id", sa.BigInteger(), nullable=False),
        sa.Column("buyer_name", sa.String(length=180), nullable=True),
        sa.Column("source_row", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(length=100), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("raw_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["sync_run_id"], ["sync_runs.id"], name="fk_sync_errors_sync_run_id_sync_runs", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_sync_errors"),
    )
    op.create_index("ix_sync_errors_sync_run_error_type", "sync_errors", ["sync_run_id", "error_type"], unique=False)
    op.create_index("ix_sync_errors_sync_run_id", "sync_errors", ["sync_run_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_sync_errors_sync_run_id", table_name="sync_errors")
    op.drop_index("ix_sync_errors_sync_run_error_type", table_name="sync_errors")
    op.drop_table("sync_errors")

    op.drop_index("ix_statistics_snapshots_sync_run_id", table_name="statistics_snapshots")
    op.drop_index("ix_statistics_snapshots_snapshot_at", table_name="statistics_snapshots")
    op.drop_index("ix_statistics_snapshots_business_date", table_name="statistics_snapshots")
    op.drop_index("ix_statistics_snapshots_buyer_id", table_name="statistics_snapshots")
    op.drop_index("ix_statistics_snapshots_buyer_business_date", table_name="statistics_snapshots")
    op.drop_table("statistics_snapshots")

    op.drop_index("ix_daily_reports_sync_run_id", table_name="daily_reports")
    op.drop_index("ix_daily_reports_report_date_status", table_name="daily_reports")
    op.drop_index("ix_daily_reports_report_date", table_name="daily_reports")
    op.drop_index("ix_daily_reports_buyer_id", table_name="daily_reports")
    op.drop_table("daily_reports")

    op.drop_index("ix_sync_runs_status", table_name="sync_runs")
    op.drop_index("ix_sync_runs_kind", table_name="sync_runs")
    op.drop_table("sync_runs")

    op.drop_index("ix_buyer_profiles_supervisor_user_id", table_name="buyer_profiles")
    op.drop_table("buyer_profiles")
    op.drop_table("user_roles")

    op.drop_index("ix_users_telegram_id", table_name="users")
    op.drop_table("users")

    snapshot_kind.drop(op.get_bind(), checkfirst=True)
    sync_kind.drop(op.get_bind(), checkfirst=True)
    sync_status.drop(op.get_bind(), checkfirst=True)
    report_status.drop(op.get_bind(), checkfirst=True)
    role_code.drop(op.get_bind(), checkfirst=True)
