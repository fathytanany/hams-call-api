from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from psycopg.rows import class_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

SCHEMA = Path(__file__).with_name("schema.sql")


@dataclass(frozen=True)
class Call:
    sid: str
    to_number: str
    from_number: str
    status: str
    stream_url: str
    stream_params: dict[str, str]
    status_callback: str | None
    status_callback_method: str
    status_callback_events: list[str]
    ring_timeout: int
    owner: str | None
    callback_seq: int
    date_created: datetime
    date_updated: datetime
    start_time: datetime | None
    end_time: datetime | None
    duration: int | None
    queue_time: int | None


async def apply_schema(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await conn.execute(SCHEMA.read_text())


async def insert_call(
    pool: AsyncConnectionPool,
    *,
    sid: str,
    to: str,
    from_: str,
    stream_url: str,
    stream_params: dict[str, str],
    status_callback: str | None,
    status_callback_method: str,
    status_callback_events: list[str],
    ring_timeout: int,
) -> Call:
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=class_row(Call))
        await cur.execute(
            """
            INSERT INTO calls (sid, to_number, from_number, stream_url, stream_params,
                               status_callback, status_callback_method,
                               status_callback_events, ring_timeout)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                sid,
                to,
                from_,
                stream_url,
                Jsonb(stream_params),
                status_callback,
                status_callback_method,
                status_callback_events,
                ring_timeout,
            ),
        )
        row = await cur.fetchone()
    assert row is not None  # INSERT ... RETURNING always yields the row
    return row


async def get_call(pool: AsyncConnectionPool, sid: str) -> Call | None:
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=class_row(Call))
        await cur.execute("SELECT * FROM calls WHERE sid = %s", (sid,))
        return await cur.fetchone()


async def list_calls(
    pool: AsyncConnectionPool,
    *,
    to: str | None,
    from_: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> list[Call]:
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=class_row(Call))
        await cur.execute(
            """
            SELECT * FROM calls
            WHERE (%(to)s::text IS NULL OR to_number = %(to)s)
              AND (%(from)s::text IS NULL OR from_number = %(from)s)
              AND (%(status)s::text IS NULL OR status = %(status)s)
            ORDER BY date_created DESC, sid DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {"to": to, "from": from_, "status": status, "limit": limit, "offset": offset},
        )
        return await cur.fetchall()


async def claim_calls(pool: AsyncConnectionPool, owner: str, limit: int) -> list[Call]:
    """Oldest queued calls first; SKIP LOCKED lets several servers claim without overlap."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=class_row(Call))
        await cur.execute(
            """
            UPDATE calls
            SET status = 'ringing', owner = %(owner)s, start_time = now(), date_updated = now(),
                queue_time = (extract(epoch FROM now() - date_created) * 1000)::int
            WHERE sid IN (
                SELECT sid FROM calls WHERE status = 'queued'
                ORDER BY date_created LIMIT %(limit)s
                FOR UPDATE SKIP LOCKED)
            RETURNING *
            """,
            {"owner": owner, "limit": limit},
        )
        return await cur.fetchall()


async def answer_call(pool: AsyncConnectionPool, sid: str, owner: str) -> datetime | None:
    """Marks the call in-progress; returns the answer time, or None if we lost ownership."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE calls SET status = 'in-progress', date_updated = now()
            WHERE sid = %s AND owner = %s AND status = 'ringing'
            RETURNING date_updated
            """,
            (sid, owner),
        )
        row = await cur.fetchone()
    return None if row is None else row[0]


async def complete_call(
    pool: AsyncConnectionPool, sid: str, owner: str, answered: datetime
) -> bool:
    """Ends an answered call; False if another server reaped it first (ADR-003 fencing)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE calls
            SET status = 'completed', end_time = now(), date_updated = now(),
                duration = round(extract(epoch FROM now() - %s))::int
            WHERE sid = %s AND owner = %s AND status = 'in-progress'
            """,
            (answered, sid, owner),
        )
    return cur.rowcount == 1
