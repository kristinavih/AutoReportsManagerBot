"""Database package with lazy session exports.

Importing ``db`` or ``db.models`` must not open a database connection.  Session
objects remain available through lazy attributes for backward compatibility.
"""

from .base import Base

__all__ = ["Base", "engine", "AsyncSessionFactory", "get_session", "session_scope"]


def __getattr__(name: str):
    if name in {"engine", "AsyncSessionFactory", "get_session", "session_scope"}:
        from . import session

        return getattr(session, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
