"""Datetimes from the database may come back naive (SQLite) or aware (Postgres). Treat naive as UTC."""

from __future__ import annotations

from datetime import UTC, datetime


def utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
