from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _get_database_url() -> str:
    value = str(os.getenv("DATABASE_URL") or "").strip()
    if not value:
        raise RuntimeError(
            "DATABASE_URL не указан в .env. Пример: "
            "postgresql+asyncpg://postgres:password@localhost:5432/reports_bot"
        )

    url = make_url(value)
    if url.drivername != "postgresql+asyncpg":
        raise RuntimeError(
            "DATABASE_URL должен использовать драйвер postgresql+asyncpg, "
            f"сейчас указан: {url.drivername!r}"
        )

    return value


DATABASE_URL = _get_database_url()

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
)

AsyncSessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Dependency-style session provider.

    The caller decides when to commit. On an exception the pending transaction
    is rolled back before the session is closed.
    """
    async with AsyncSessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transaction scope for scripts and services.

    Commits on success and rolls back on failure.
    """
    async with AsyncSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
