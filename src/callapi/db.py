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
