import asyncio
import json
import os
import struct
import time
import uuid
import wave
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import AsyncConnectionPool
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

# Tests get their own database so they never wipe calls made with `make run` / compose.
ADMIN_URL = "postgresql://callapi:callapi@localhost:5432/callapi"
os.environ.setdefault("DATABASE_URL", "postgresql://callapi:callapi@localhost:5432/callapi_test")
# The API tests' app runs no calls, so a created call stays `queued`; runner tests start
# their own Supervisor.
os.environ.setdefault("MAX_CALLS", "0")

from fastapi.testclient import TestClient  # noqa: E402 - must follow the env setup

from callapi import config, db  # noqa: E402
from callapi.main import app  # noqa: E402
from callapi.runner import Supervisor  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def test_database() -> None:
    # Recreated every run, so schema.sql edits apply without migrations.
    name = sql.Identifier(str(psycopg.conninfo.conninfo_to_dict(config.DATABASE_URL)["dbname"]))
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(name))
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute(db.SCHEMA.read_text())


@pytest.fixture
def clean_db() -> None:
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute("TRUNCATE calls, servers")


@pytest.fixture
def client(clean_db: None) -> Iterator[TestClient]:
    with TestClient(app) as c:
        c.auth = (config.ACCOUNT_SID, config.AUTH_TOKEN)
        yield c


@pytest.fixture
async def pool(clean_db: None) -> AsyncIterator[AsyncConnectionPool]:
    async with AsyncConnectionPool(config.DATABASE_URL, open=False) as p:
        yield p


@dataclass
class FakeApp:
    """A Media Streams app that records every message with its arrival time."""

    url: str
    received: list[tuple[float, dict[str, Any]]] = field(default_factory=list)
    # Set by a test to answer messages, e.g. echo audio or send marks.
    on_message: Callable[[ServerConnection, dict[str, Any]], Awaitable[None]] | None = None
    closed_at: float | None = None

    def first(self, event: str) -> tuple[float, dict[str, Any]]:
        return next((t, m) for t, m in self.received if m["event"] == event)

    def events(self) -> list[str]:
        return [m["event"] for _, m in self.received]


@pytest.fixture
async def fake_app() -> AsyncIterator[FakeApp]:
    app = FakeApp("")

    async def handler(ws: ServerConnection) -> None:
        with suppress(ConnectionClosed):
            async for raw in ws:
                msg = json.loads(raw)
                app.received.append((time.monotonic(), msg))
                if app.on_message:
                    await app.on_message(ws, msg)
        app.closed_at = time.monotonic()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        app.url = f"ws://127.0.0.1:{port}/stream"
        yield app


TO = "+15550001"


def write_wav(path: Path, seconds: float, rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<h", 1000) * int(rate * seconds))


async def insert(
    pool: AsyncConnectionPool, url: str, to: str = TO, ring_timeout: int = 60
) -> db.Call:
    return await db.insert_call(
        pool,
        sid=f"CA{uuid.uuid4().hex}",
        to=to,
        from_="+15550000",
        stream_url=url,
        stream_params={"mode": "normal"},
        status_callback=None,
        status_callback_method="POST",
        status_callback_events=["completed"],
        ring_timeout=ring_timeout,
    )


async def wait_for_status(pool: AsyncConnectionPool, sid: str, status: str) -> db.Call:
    async with asyncio.timeout(10):
        while (call := await db.get_call(pool, sid)) is None or call.status != status:  # noqa: ASYNC110 - status lives in Postgres
            await asyncio.sleep(0.05)
    return call


@pytest.fixture
async def supervisor(pool: AsyncConnectionPool, tmp_path: Path) -> AsyncIterator[Supervisor]:
    sup = Supervisor(
        pool, server_id="test-server", max_calls=10, media_dir=tmp_path, recordings_dir=tmp_path
    )
    task = asyncio.create_task(sup.run())
    yield sup
    for t in (task, *sup.calls):
        t.cancel()
    await asyncio.gather(task, *sup.calls, return_exceptions=True)
