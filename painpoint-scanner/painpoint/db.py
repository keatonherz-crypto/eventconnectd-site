"""Storage layer.

Postgres is the intended production target, but every query in this project is
written in a portable subset so the whole pipeline also runs against a local
SQLite file. That is what makes the test suite runnable with no services and no
network, and it lets you try the pipeline end to end before provisioning
anything.

SQL is written with `?` placeholders throughout and rewritten to `%s` when the
connection is Postgres. Consequence: never put a literal `?` inside a SQL
string literal.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .schema import DDL_POSTGRES, DDL_SQLITE


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: Any) -> datetime | None:
    """Read a timestamp back out of either dialect.

    Postgres hands back a datetime; SQLite hands back the ISO string we wrote.
    """
    if value is None or isinstance(value, datetime):
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def ts_param(value: datetime | None) -> str | None:
    """Write a timestamp in a form both dialects accept."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class Database:
    def __init__(self, connection, dialect: str):
        self.conn = connection
        self.dialect = dialect

    # -- connection -----------------------------------------------------

    @classmethod
    def connect(cls, url: str | None = None) -> "Database":
        url = url or os.environ.get("DATABASE_URL") or "sqlite:///painpoints.db"

        if url.startswith(("postgres://", "postgresql://")):
            import psycopg
            from psycopg.rows import dict_row

            return cls(psycopg.connect(url, row_factory=dict_row), "postgres")

        if url.startswith("sqlite://"):
            path = url[len("sqlite://") :]
            path = path[1:] if path.startswith("//") else path.lstrip("/") or ":memory:"
        else:
            path = url

        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return cls(conn, "sqlite")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        self.close()

    # -- dialect plumbing -----------------------------------------------

    def _render(self, sql: str) -> str:
        return re.sub(r"\?", "%s", sql) if self.dialect == "postgres" else sql

    def json_param(self, value: Any) -> Any:
        """Wrap a Python object for insertion into the scores/competitors columns."""
        if value is None:
            return None
        if self.dialect == "postgres":
            from psycopg.types.json import Jsonb

            return Jsonb(value)
        return json.dumps(value, sort_keys=True)

    @staticmethod
    def load_json(value: Any, default: Any = None) -> Any:
        """Read a JSON column back. Postgres decodes jsonb for us; SQLite does not."""
        if value is None:
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    # -- execution ------------------------------------------------------

    @contextmanager
    def _cursor(self):
        cur = self.conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._cursor() as cur:
            cur.execute(self._render(sql), tuple(params))

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        batch = [tuple(r) for r in rows]
        if not batch:
            return 0
        with self._cursor() as cur:
            cur.executemany(self._render(sql), batch)
        return len(batch)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(self._render(sql), tuple(params))
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.query_one(sql, params)
        return next(iter(row.values())) if row else None

    def commit(self) -> None:
        self.conn.commit()

    # -- schema ---------------------------------------------------------

    def init_schema(self) -> None:
        ddl = DDL_POSTGRES if self.dialect == "postgres" else DDL_SQLITE
        for statement in filter(None, (s.strip() for s in ddl.split(";\n"))):
            self.execute(statement)
        self.commit()
