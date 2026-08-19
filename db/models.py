from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin


class RoleCode(str, enum.Enum):
    BOSS = "BOSS"
    MANAGER = "MANAGER"
    TEAMLEAD = "TEAMLEAD"
    BUYER = "BUYER"
    ADMIN = "ADMIN"


class ReportStatus(str, enum.Enum):
    SUBMITTED = "SUBMITTED"
    MISSING = "MISSING"
    LATE = "LATE"
    REPLACED = "REPLACED"


class SyncStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class SyncKind(str, enum.Enum):
    DAILY_REPORTS = "DAILY_REPORTS"
    STATISTICS = "STATISTICS"
    BUYERS = "BUYERS"
    MANUAL = "MANUAL"


class SnapshotKind(str, enum.Enum):
    TUESDAY = "TUESDAY"
    THURSDAY = "THURSDAY"
    SATURDAY = "SATURDAY"
    MONTH_END = "MONTH_END"
    MANUAL = "MANUAL"


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(
        BigInteger,
        unique=True,
        index=True,
        nullable=True,
    )
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    display_name: Mapped[str] = mapped_column(String(180), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )

    roles: Mapped[list[UserRole]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    buyer_profile: Mapped[BuyerProfile | None] = relationship(
        back_populates="user",
        foreign_keys="BuyerProfile.user_id",
        uselist=False,
        cascade="all, delete-orphan",
    )


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[RoleCode] = mapped_column(
        Enum(RoleCode, name="role_code", validate_strings=True),
        primary_key=True,
    )

    user: Mapped[User] = relationship(back_populates="roles")


class BuyerProfile(TimestampMixin, Base):
    __tablename__ = "buyer_profiles"
    __table_args__ = (
        UniqueConstraint("source_name", name="uq_buyer_profiles_source_name"),
        CheckConstraint(
            "supervisor_user_id IS NULL OR supervisor_user_id <> user_id",
            name="supervisor_not_self",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    source_name: Mapped[str] = mapped_column(
        String(180),
        nullable=False,
        comment="Имя баера в промежуточной Google-таблице",
    )
    external_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Например номер в имени: 216",
    )
    supervisor_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
        comment="Непосредственный руководитель: руководитель или тимлид",
    )
    source_sheet_row: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )

    user: Mapped[User] = relationship(
        back_populates="buyer_profile",
        foreign_keys=[user_id],
    )
    supervisor: Mapped[User | None] = relationship(
        foreign_keys=[supervisor_user_id],
    )
    daily_reports: Mapped[list[DailyReport]] = relationship(
        back_populates="buyer",
        cascade="all, delete-orphan",
    )
    statistics_snapshots: Mapped[list[StatisticsSnapshot]] = relationship(
        back_populates="buyer",
        cascade="all, delete-orphan",
    )


class SyncRun(TimestampMixin, Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[SyncKind] = mapped_column(
        Enum(SyncKind, name="sync_kind", validate_strings=True),
        nullable=False,
        index=True,
    )
    status: Mapped[SyncStatus] = mapped_column(
        Enum(SyncStatus, name="sync_status", validate_strings=True),
        nullable=False,
        default=SyncStatus.RUNNING,
        server_default=SyncStatus.RUNNING.value,
        index=True,
    )
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    rows_received: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    rows_saved: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    rows_skipped: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    errors: Mapped[list[SyncError]] = relationship(
        back_populates="sync_run",
        cascade="all, delete-orphan",
    )


class DailyReport(TimestampMixin, Base):
    __tablename__ = "daily_reports"
    __table_args__ = (
        UniqueConstraint(
            "buyer_id",
            "report_date",
            name="uq_daily_reports_buyer_date",
        ),
        Index("ix_daily_reports_report_date_status", "report_date", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    buyer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("buyer_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sync_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("sync_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    status: Mapped[ReportStatus] = mapped_column(
        Enum(ReportStatus, name="report_status", validate_strings=True),
        nullable=False,
        default=ReportStatus.SUBMITTED,
        server_default=ReportStatus.SUBMITTED.value,
    )
    launched_accounts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    geo_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    geo_normalized: Mapped[str | None] = mapped_column(Text, nullable=True)
    issues: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_row: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    buyer: Mapped[BuyerProfile] = relationship(back_populates="daily_reports")


class StatisticsSnapshot(TimestampMixin, Base):
    __tablename__ = "statistics_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "buyer_id",
            "snapshot_at",
            name="uq_statistics_snapshots_buyer_snapshot_at",
        ),
        Index(
            "ix_statistics_snapshots_buyer_business_date",
            "buyer_id",
            "business_date",
        ),
        CheckConstraint("revenue_total >= 0", name="revenue_non_negative"),
        CheckConstraint("spend_total >= 0", name="spend_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    buyer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("buyer_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sync_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("sync_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    business_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    snapshot_kind: Mapped[SnapshotKind] = mapped_column(
        Enum(SnapshotKind, name="snapshot_kind", validate_strings=True),
        nullable=False,
    )
    source_period: Mapped[str] = mapped_column(
        String(7),
        nullable=False,
        comment="Месяц накопительной статистики, например 2026-07",
    )
    revenue_total: Mapped[Decimal] = mapped_column(
        Numeric(18, 2),
        nullable=False,
    )
    spend_total: Mapped[Decimal] = mapped_column(
        Numeric(18, 2),
        nullable=False,
    )
    profit_total: Mapped[Decimal] = mapped_column(
        Numeric(18, 2),
        nullable=False,
    )
    roi_total: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 4),
        nullable=True,
        comment="Общий ROI на момент снимка",
    )
    source_row: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    buyer: Mapped[BuyerProfile] = relationship(
        back_populates="statistics_snapshots"
    )


class SyncError(Base):
    __tablename__ = "sync_errors"
    __table_args__ = (
        Index("ix_sync_errors_sync_run_error_type", "sync_run_id", "error_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sync_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sync_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    buyer_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    source_row: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_type: Mapped[str] = mapped_column(String(100), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    raw_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    sync_run: Mapped[SyncRun] = relationship(back_populates="errors")
